"""后台计算作业登记表（E2E-7 2026-08-04 实测的那个黑洞）。

## 现场

experiment 节点连着三次做不出 `experiment_log`，累计 10+ 小时、30M+ token。
查下来**它一个错都没报** —— vLLM 服务健康、推理作业在跑、进度在动，它只是在

    sleep 1500; tail -4 h3_run2.log      # 睡 25 分钟看一眼
    sleep 3000; tail -6 h3_rerun.log     # 睡 50 分钟看一眼

一觉一觉地等。而**每次醒来消耗一个 turn**，`max_turns=200` 的寿命全喂给了 sleep，
撞线时 run 结束、产出没落盘、全废。

而框架这边：`ActiveRunInfo` 登记的是**子 run**（"experiment 在跑"），至于它手底下
挂着两个吃 4 张 A100 的进程 —— **完全不知道**。调度器想知道得自己想起来去调
`runtime_control(action="progress")` 翻 transcript 文本猜。

## 这张表补的是哪一层

    已有：子 run（谁在跑）    本模块：子作业（跑的是什么、占什么、还要多久）

三件事一起解：
  1. 调度器不用翻 transcript 猜 —— `runtime_control(action="jobs")` 直接看
  2. **"该不该干预"第一次有了机械判据** —— `overrun_ratio` = 实际/预计。
     预计 1 小时跑了 6 小时才该报警；预计 6 小时跑了 5 小时就该安心睡。
  3. 作业**活得比 run 长** —— 登记表持久化到项目级，下一个 run 接手时知道
     上一轮起的作业还在跑，不用重复起
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from core import jobs
from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import execute

bootstrap()


def _state(project_id: str = "p_jobs") -> State:
    return State.new(node_type="experiment",
                     base_dir=Path(tempfile.mkdtemp()), project_id=project_id)


def _call(tool, st, **kw):
    return asyncio.run(execute(tool, st, **kw))


# ── 登记与查询 ──────────────────────────────────────────────────────────────

def test_declare_then_visible_to_orchestrator():
    st = _state()
    r = _call("declare_job", st, purpose="H3 235B 路由推理",
              expected_duration_s=6 * 3600, resources="4×A100 (GPU4-7)",
              progress_probe="wc -l results/h3/steps_*.jsonl", pid=12345)
    assert r["status"] == "success"
    rendered = jobs.render_for_orchestrator(st)
    assert "H3 235B 路由推理" in rendered
    assert "4×A100" in rendered, "调度器要看得到占了什么算力"


def test_expected_duration_is_mandatory():
    """没有预计时长 = 没有"该不该干预"的判据。这是这张表的存在理由，必须硬性。"""
    st = _state()
    r = _call("declare_job", st, purpose="x", expected_duration_s=0)
    # 契约归 schema：expected_duration_s 的 exclusiveMinimum:0 在 parameters_schema，
    # 派发口核一次（工具体内不再手写）；schema description 说清为什么必须 > 0。
    assert r["status"] == "error"
    assert "expected_duration_s" in r["error"]
    assert "机械判据" in r["parameters_schema"]["properties"]["expected_duration_s"]["description"]


def test_overrun_ratio_is_the_intervention_criterion():
    """预计 1h 跑了 6h 才该报警；预计 6h 跑了 5h 该安心睡。"""
    st = _state()
    _call("declare_job", st, purpose="慢作业", expected_duration_s=3600)
    rec = jobs.load(st)[0]
    object.__setattr__(rec, "started_at", time.time() - 6 * 3600)
    assert rec.overrun_ratio > 5, "严重超期必须算得出来"

    rec2 = jobs.JobRecord(job_id="j2", expected_duration_s=6 * 3600,
                          started_at=time.time() - 5 * 3600)
    assert rec2.overrun_ratio < 1, "按计划走的不该报警"


def test_no_estimate_means_undecidable_not_fine():
    """没声明预计时长 → overrun_ratio 是 None（不可判），不是 0（没问题）。

    "查不出来不是跑成了" —— 今天已经在别处栽过好几次的同一个形状。
    """
    rec = jobs.JobRecord(job_id="j", started_at=time.time() - 99999)
    assert rec.overrun_ratio is None
    assert rec.eta_s is None


# ── 跨 run 存活：这是整张表的核心事实 ───────────────────────────────────────

def test_registry_outlives_the_run_that_started_it():
    """run 撞 max_turns 结束了，那个 vLLM 还在吃着 4 张卡。

    所以登记表必须持久化到**项目级**而不是 state.hook_state（随 run 消亡）——
    下一个 run 接手时才知道"上一轮起的作业还在跑，别重复起"。
    """
    st1 = _state("p_cross")
    _call("declare_job", st1, purpose="长作业", expected_duration_s=7200)

    st2 = State.new(node_type="experiment",
                    base_dir=Path(tempfile.mkdtemp()), project_id="p_cross")
    seen = jobs.load(st2, only_open=True)
    assert [j.purpose for j in seen] == ["长作业"], \
        "新 run 必须看得到上一个 run 起的作业"


def test_progress_updates_are_append_only():
    """状态变更追加新记录，历史可审计（与 transcript 同构）。"""
    st = _state()
    jid = _call("declare_job", st, purpose="p", expected_duration_s=100)["job_id"]
    _call("job_progress", st, job_id=jid, progress="10/118")
    _call("job_progress", st, job_id=jid, progress="41/118")
    rec = next(j for j in jobs.load(st) if j.job_id == jid)
    assert rec.last_progress == "41/118", "读取取最后一条"
    raw = (Path(st.project_root) / "jobs.jsonl").read_text(encoding="utf-8")
    assert raw.count("10/118") == 1 and raw.count("41/118") == 1, "历史不许被抹掉"


def test_done_jobs_drop_out_of_open_list():
    st = _state()
    jid = _call("declare_job", st, purpose="p", expected_duration_s=100)["job_id"]
    _call("job_progress", st, job_id=jid, status="done")
    assert jobs.load(st, only_open=True) == []
    assert len(jobs.load(st)) == 1, "结束了仍在账上（审计要看得到）"


def test_single_open_job_lets_you_omit_the_id():
    st = _state()
    _call("declare_job", st, purpose="唯一作业", expected_duration_s=100)
    r = _call("job_progress", st, progress="半截了")
    assert r["status"] == "success"


# ── 机械识别：起了作业却没登记 ──────────────────────────────────────────────

@pytest.mark.parametrize("cmd,expected", [
    ("nohup bash serve_235b.sh > log 2>&1 &", True),
    ("setsid python train.py &", True),
    ("python run.py &", True),
    ("cd /w && nohup ./job.sh &", True),
    ("sleep 1500; tail -4 h3_run2.log", False),      # 这是轮询，不是起作业
    ("wc -l results/*.jsonl", False),
    ("grep -c foo bar.txt", False),
])
def test_background_launch_detection(cmd, expected):
    assert jobs.looks_like_background_launch(cmd) is expected


def test_undeclared_launch_gets_a_hint():
    """提醒必须进**工具返回值** —— 写在 harness 里没人读的规则，今天数出七次了。"""
    st = _state()
    hint = jobs.undeclared_launch_hint(st, "nohup bash long_job.sh &")
    assert hint and "declare_job" in hint


def test_no_hint_once_declared():
    st = _state()
    _call("declare_job", st, purpose="已登记", expected_duration_s=100)
    assert jobs.undeclared_launch_hint(st, "nohup bash x.sh &") is None


def test_polling_never_triggers_the_hint():
    """别把轮询误判成起作业 —— 那会变成每次查进度都被唠叨一遍。"""
    st = _state()
    assert jobs.undeclared_launch_hint(st, "sleep 3000; tail -6 run.log") is None


# ── 真接缝：run_bash 与 runtime_control ─────────────────────────────────────

def test_run_bash_actually_carries_the_reminder():
    """本周两次教训（tail_file 的 schema 缝、finalize_run 的打桩缝）都是
    "测了内部函数、没测接缝"。这条走真的 run_bash。"""
    st = _state()
    r = _call("run_bash", st, cmd="nohup sleep 0.1 & echo started")
    assert "job_registry_reminder" in r, "提醒没接到 run_bash 的返回值上"


def test_runtime_control_exposes_jobs_to_the_orchestrator():
    st = _state()
    _call("declare_job", st, purpose="给调度器看的作业",
          expected_duration_s=3600, resources="2×A100")
    r = _call("runtime_control", st, action="jobs")
    assert r["status"] == "success"
    assert any(j["purpose"] == "给调度器看的作业" for j in r["jobs"])
    assert "2×A100" in r["summary"]


def test_include_done_is_declared_in_schema():
    """参数不进 schema = LLM 看不到、不会传（本周已栽过一次）。"""
    from core.tool_registry import _REGISTRY
    props = _REGISTRY.tools["runtime_control"].parameters_schema["properties"]
    assert "include_done" in props
    assert "jobs" in _REGISTRY.tools["runtime_control"].description
