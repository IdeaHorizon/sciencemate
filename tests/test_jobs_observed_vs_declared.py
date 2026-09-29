"""登记表存先验，nvidia-smi 才是证据。以及：半条记录比没有记录更坏。

2026-08-05 e2e9 首次真跑作业登记表，暴露两个洞：

① **登记的是意图，不是事实。**
   登记表：`2×A100-80GB (GPU 4,5)`
   nvidia-smi：**GPU 0** 占 80.5GB，1–7 全空
   它以为自己在 4、5 号卡上，实际跑在 0 号卡上，而且一张卡就吃满了
   （vLLM 默认预占 90% 显存）。调度器照着登记表做资源决策会撞车。

② **无头作业。**
   `declare_job` 调了 2 次，`job_progress` 调了 10 次 —— 有人只报进度没登记，
   登记表里凭空长出一条 `purpose=None resources=None expected_duration_s=None`
   的记录。而 expected_duration_s 是 overrun_ratio 的唯一依据，没有它
   「该不该干预」就不可判。

两条都是同一个原则的两面：**配置是先验，观测是证据**；而「查不出来」既不是
「对得上」，也不是「没问题」。
"""
from __future__ import annotations

import asyncio

import pytest

from core import jobs as _jobs
from core.state import State


@pytest.fixture()
def state(tmp_path) -> State:
    return State.new(node_type="experiment", base_dir=tmp_path, project_id="p1")


# ── ① 声明 vs 实测 ──────────────────────────────────────────────────────

def test_declared_gpu_indices_parsed():
    f = _jobs.declared_gpu_indices
    assert f("2×A100-80GB (GPU 4,5)") == [4, 5]
    assert f("gpu 0") == [0]
    assert f("4 张卡: 0,1,2,3") == [0, 1, 2, 3]


def test_declared_gpu_indices_does_not_guess():
    """抠不出卡号就返回空 —— 不许从 '8×A100' 里编出 0..7。"""
    assert _jobs.declared_gpu_indices("8×A100-80GB") == []
    assert _jobs.declared_gpu_indices("") == []
    assert _jobs.declared_gpu_indices("整机独占") == []


def _job(**kw) -> _jobs.JobRecord:
    base = dict(job_id="j1", purpose="vLLM", resources="2×A100 (GPU 4,5)",
                expected_duration_s=3600.0, pid=1234, status="running")
    base.update(kw)
    return _jobs.JobRecord(**base)


def test_mismatch_is_reported_loudly(monkeypatch):
    """事故本体：声明 4,5 实测 0。必须说出来。"""
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [(1234, 1, "vllm serve")])
    msg = _jobs.gpu_reconciliation(_job(), {1234: [0]})
    assert "声明 GPU 4,5" in msg and "实测在 GPU 0" in msg
    assert "撞车" in msg


def test_match_is_confirmed(monkeypatch):
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [(1234, 1, "vllm serve")])
    msg = _jobs.gpu_reconciliation(_job(), {1234: [4, 5]})
    assert "与声明一致" in msg
    assert "⚠️" not in msg


def test_unprobeable_is_silent_not_a_green_light(monkeypatch):
    """查不到 ≠ 对得上。没有 nvidia-smi 时不许渲染成"一致"。"""
    assert _jobs.gpu_reconciliation(_job(), None) == ""
    assert _jobs.observed_gpus_for(_job(), None) is None


def test_no_pid_means_unknown_not_zero(monkeypatch):
    """没记 pid 就查不到 —— 返回 None，不是"占了 0 张卡"。"""
    assert _jobs.observed_gpus_for(_job(pid=None), {1234: [0]}) is None


def test_declared_but_holding_nothing_is_flagged(monkeypatch):
    """进程**认出来了**、但它一张卡没占 —— 这是有证据的否定，得说。
    （认不出跑在哪的情况是"不可判"，走另一条路，见 ③ 那组。）"""
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [(1234, 1, "vllm serve")])
    msg = _jobs.gpu_reconciliation(_job(), {9999: [0]})
    assert "未占任何 GPU" in msg


def test_worker_children_are_counted(monkeypatch):
    """vLLM/sglang 真正占卡的是 worker 子进程，登记的往往是启动器 pid。
    只看启动器会永远查不到卡 —— 进程树没断时要顺着看。"""
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [
        (1234, 1, "vllm launcher"), (5001, 1234, "worker0"), (5002, 1234, "worker1")])
    got = _jobs.observed_gpus_for(_job(), {5001: [4], 5002: [5]})
    assert got == [4, 5]


def test_observed_gpus_returns_none_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert _jobs.observed_gpus() is None


def test_render_includes_reconciliation(state, monkeypatch):
    _jobs.declare(state, purpose="vLLM 服务", resources="2×A100 (GPU 4,5)",
                  expected_duration_s=3600.0, pid=1234)
    monkeypatch.setattr(_jobs, "observed_gpus", lambda: {1234: [0]})
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [(1234, 1, "vllm serve")])
    out = _jobs.render_for_orchestrator(state)
    assert "实测在 GPU 0" in out


# ── ② 无头作业 ──────────────────────────────────────────────────────────

def _progress(st, **kw) -> dict:
    from shared.tools.library.job_registry import _job_progress
    return asyncio.run(_job_progress(st, **kw))


def _declare(st, **kw) -> dict:
    from shared.tools.library.job_registry import _declare_job
    return asyncio.run(_declare_job(st, **kw))


def test_progress_for_undeclared_job_is_refused(state):
    """只报进度不登记 → 登记表长出一条什么都不知道的半截记录。"""
    res = _progress(state, job_id="never-declared", progress="41/118")
    assert res["status"] == "error"
    assert "declare_job" in res["error"]


def test_refusal_explains_why_it_matters(state):
    """报错要说清代价，不是只说"不许"。"""
    res = _progress(state, job_id="ghost", progress="x")
    assert "overrun_ratio" in res["error"] or "干预" in res["error"]


def test_undeclared_progress_does_not_pollute_the_ledger(state):
    """被拒之后登记表里不许留下任何痕迹。"""
    _progress(state, job_id="ghost", progress="x")
    assert [j.job_id for j in _jobs.load(state)] == []


def test_progress_after_declare_still_works(state):
    d = _declare(state, purpose="跑 bootstrap", resources="CPU",
                 expected_duration_s=120.0)
    res = _progress(state, job_id=d["job_id"], progress="B=10000, 60s 已过")
    assert res["status"] == "success"
    rec = next(j for j in _jobs.load(state) if j.job_id == d["job_id"])
    assert "B=10000" in rec.last_progress
    assert rec.expected_duration_s == 120.0


def test_single_open_job_still_allows_omitting_id(state):
    """只有一个在跑时省略 job_id 的便利保留 —— 拒的是"没登记"，不是"没写 id"。"""
    _declare(state, purpose="唯一作业", resources="CPU", expected_duration_s=60.0)
    res = _progress(state, progress="半程")
    assert res["status"] == "success"


# ── ③ pid 会变：重启 / 孤儿化 ────────────────────────────────────────────
#
# e2e9 追根因追出来的第二层，而且是**我自己第一版的 bug**：
#   · turn 15 起了一次 vLLM，turn 28 换 conda 环境同端口又起一次 —— 新 pid 从没登记
#   · `nohup ... &` 的守护进程父 shell 一退出就被 init 收养（实测 117273 ← 117117 ← 1）
# 两种情况下顺着登记 pid 走进程树都找不到它。而登记表存在的意义恰恰就是记录
# 这种活得比 run 长的守护进程 —— 用"启动时的关系"去认"运行时的事实"，方向就错了。

def _job_sig(sig: str, **kw) -> _jobs.JobRecord:
    base = dict(job_id="j1", purpose="vLLM", resources="2×A100 (GPU 4,5)",
                expected_duration_s=3600.0, pid=113848, status="running",
                raw={"process_signature": sig})
    base.update(kw)
    return _jobs.JobRecord(**base)


_VLLM = "/opt/conda/envs/vllm/bin/vllm serve /models/Qwen2.5-7B --port 8200"


def test_orphaned_daemon_is_found_by_signature(monkeypatch):
    """孤儿化：117273 的父链断到 init，进程树走不到，签名能认出来。"""
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(1, 0, "systemd"), (117273, 1, _VLLM)])
    pids, how = _jobs.resolve_live_pids(_job_sig(_VLLM))
    assert how == "signature" and 117273 in pids


def test_relaunched_with_new_pid_is_found(monkeypatch):
    """重启换 pid：同端口同模型，认得出是同一个服务。"""
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(1, 0, "systemd"),
                                 (999001, 1, _VLLM.replace("envs/vllm", "envs/vllm311"))])
    pids, how = _jobs.resolve_live_pids(_job_sig(_VLLM))
    assert how == "signature" and 999001 in pids


def test_live_declared_pid_wins_over_signature(monkeypatch):
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(113848, 1, _VLLM), (999001, 1, _VLLM)])
    _, how = _jobs.resolve_live_pids(_job_sig(_VLLM))
    assert how == "declared"


def test_dead_pid_no_successor_is_unknown_not_gone(monkeypatch):
    """找不到继任者 = 不可判，**不是**"作业已结束"。"""
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [(1, 0, "systemd")])
    pids, how = _jobs.resolve_live_pids(_job_sig(_VLLM))
    assert how == "unknown" and pids == []


def test_unknown_pid_means_gpu_unjudgeable(monkeypatch):
    """认不出跑在哪 → GPU 归属 None（不可判），不许说"一张卡没占"。"""
    monkeypatch.setattr(_jobs, "_ps_table", lambda: [(1, 0, "systemd")])
    assert _jobs.observed_gpus_for(_job_sig(_VLLM), {117273: [0]}) is None
    assert _jobs.gpu_reconciliation(_job_sig(_VLLM), {117273: [0]}) == ""


def test_signature_match_then_gpu_mismatch_is_reported(monkeypatch):
    """完整链路：签名找到继任者 → 它在 GPU 0 → 声明 4,5 → 报不一致。"""
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(1, 0, "systemd"), (117273, 1, _VLLM)])
    msg = _jobs.gpu_reconciliation(_job_sig(_VLLM), {117273: [0]})
    assert "声明 GPU 4,5" in msg and "实测在 GPU 0" in msg


def test_signature_requires_anchors_not_substring(monkeypatch):
    """签名匹配得靠端口/模型这类锚点，不能拿整串做模糊比对乱认。"""
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(1, 0, "systemd"),
                                 (888, 1, "/opt/conda/envs/vllm/bin/vllm serve /models/OTHER --port 9999")])
    _, how = _jobs.resolve_live_pids(_job_sig(_VLLM))
    assert how == "unknown"


def test_no_signature_recorded_falls_back_to_unknown(monkeypatch):
    """老记录没存签名 —— 退化成不可判，不是乱认一个。"""
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(1, 0, "systemd"), (117273, 1, _VLLM)])
    _, how = _jobs.resolve_live_pids(_job_sig(""))
    assert how == "unknown"


def test_declare_records_signature(state, monkeypatch):
    monkeypatch.setattr(_jobs, "process_signature", lambda p: _VLLM)
    _jobs.declare(state, purpose="vLLM", resources="GPU 4,5",
                  expected_duration_s=3600.0, pid=113848)
    rec = _jobs.load(state)[0]
    assert rec.raw.get("process_signature") == _VLLM


def test_render_says_pid_changed_not_process_gone(state, monkeypatch):
    """事故本体的渲染：不许再说"进程已不在"了事。"""
    monkeypatch.setattr(_jobs, "process_signature", lambda p: _VLLM)
    _jobs.declare(state, purpose="vLLM 服务", resources="2×A100 (GPU 4,5)",
                  expected_duration_s=3600.0, pid=113848)
    monkeypatch.setattr(_jobs, "_ps_table",
                        lambda: [(1, 0, "systemd"), (117273, 1, _VLLM)])
    monkeypatch.setattr(_jobs, "observed_gpus", lambda: {117273: [0]})
    out = _jobs.render_for_orchestrator(state)
    assert "继任 pid 117273" in out
    assert "进程已不在" not in out
    assert "实测在 GPU 0" in out


def test_overrun_ratio_still_computable_after_fix(state):
    """整条链的意义：登记 → 报进度 → 算得出 overrun_ratio。"""
    d = _declare(state, purpose="x", resources="CPU", expected_duration_s=100.0)
    _progress(state, job_id=d["job_id"], progress="跑着")
    rec = next(j for j in _jobs.load(state) if j.job_id == d["job_id"])
    assert rec.overrun_ratio is not None
