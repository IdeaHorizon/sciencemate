"""冻结 pre_registration 必须显式声明这批实验算不算正式证据。

E2E v14 实测：Analysis 冻结 prereg 时 metadata 只有 capital_basis / frozen /
frozen_at / freeze_reason。experiment 的规矩是"未声明 → secondary"
（nodes/experiment/harness.yaml），于是该课题
**每一次实验都不是正式证据**，verdict 恒为 inconclusive；发现时 prereg 已冻结，
不可逆，7.4M tokens 全部作废。

规则本身是对的（防挑数据：不能"跑出了数字"就算证据）。坏在两点，都在这里堵上：
  1. 要求只写在下游 experiment 的 harness 里，Analysis 那边一个字都没有
     （`grep -rn "run_role" nodes/hypothesis/` 零命中）
  2. freeze 工具压根没有这两个参数，知道了也没地方写

2026-09-21（#979）：这道门原先还要求同时申报 `analysis_eligible`，而它唯一的消费方
（`nodes/experiment/tools/preflight.py`）2026-09-11 已经改读 `requires_hypothesis_verdict`
——一道为不存在的下游强制的申报，模型每次都要答，答案没人读。字段收掉，门留下：
门真正管住的是 `run_role` 与 `expected_params`，这两样至今仍被 experiment 读。

夹具照现行记录模型：正文是节点目录下的原生文件，类型 / 出处 / 冻结在一本工作区
账本（`core/ledger`）里。冻结是账本上带 `metadata_patch` 的一行 —— experiment
读到的"冻结后的 metadata"就是 save 行 metadata 折进这个补丁的结果。
"""

from pathlib import Path

import pytest

from core.artifact_provenance import produced
from core.ledger import workspace_store
from core.project_workspace import _NODE_WORKSPACES as _DIRS
from shared.tools.library.artifacts_extra import (
    _RUN_ROLES,
    _freeze_artifact,
    _run_role_declaration_violations,
)

_STUB_CONTENT = (
    "## Research Questions\n\n### Q1: stub question for freeze-path tests\n"
    "- output_kind: 一个数\n```yaml\n- statement: \"stub closure condition\"\n```\n"
)


def _prereg(**metadata):
    return {"type": "pre_registration", "name": "p", "content": _STUB_CONTENT,
            "metadata": metadata}


# ── 纯函数判据 ────────────────────────────────────────────────────────────


def test_missing_declaration_is_rejected():
    violations, declaration = _run_role_declaration_violations(_prereg(), None)
    assert violations
    assert declaration == {}


def test_error_lists_both_legal_answers():
    """报错必须给出正确答案——否则只能靠猜，而这一步不可逆。"""
    violations, _ = _run_role_declaration_violations(_prereg(), None)
    text = "\n".join(violations)
    assert 'run_role="primary"' in text and 'run_role="secondary"' in text
    # 后果要写清楚：不可逆 + 此后实验都不算正式证据
    assert "不可逆" in text and "inconclusive" in text


def test_the_error_does_not_ask_for_a_field_nobody_reads():
    """报错不许再点名 `analysis_eligible`（#979）。

    模型照着报错填的每一个字段，都应该有人读。点名一个没有消费方的字段，
    和"问过了、有人在看"长得一模一样 —— 而这一步不可逆，填错没有补救。
    """
    violations, _ = _run_role_declaration_violations(_prereg(), None)
    assert "analysis_eligible" not in "\n".join(violations)


def test_primary_declaration_is_recorded():
    violations, declaration = _run_role_declaration_violations(
        _prereg(), "primary", {"n_particles": 4000})
    assert not violations
    assert declaration["run_role"] == "primary"
    assert "analysis_eligible" not in declaration


def test_explicit_exploratory_is_legal():
    """探索性 prereg 显式声明 secondary 是合法出口，不该被逼着谎报 primary。"""
    violations, declaration = _run_role_declaration_violations(_prereg(), "secondary")
    assert not violations
    assert declaration == {"run_role": "secondary"}


def test_unknown_role_error_lists_legal_values():
    violations, _ = _run_role_declaration_violations(_prereg(), "formal")
    assert violations
    for role in _RUN_ROLES:
        assert role in violations[0]


def test_already_declared_in_metadata_passes_without_params():
    """metadata 里已经写好了就不必再传参（比如 save_artifact 时就带上了）。"""
    record = _prereg(run_role="primary", expected_params={"n_particles": 4000})
    violations, declaration = _run_role_declaration_violations(record, None)
    assert not violations
    assert declaration["run_role"] == "primary"


# ── 走真入口 _freeze_artifact ────────────────────────────────────────────


class _FakeState:
    """够 _freeze_artifact 跑完的最小 state：一本账（core/ledger）+ 它管的目录。

    与真 State 同名同义的三个入口都问账本：`find_artifact_path` / `read_artifact`
    跨节点读，`mark_frozen` 落一行 freeze、文件一个字节不动。
    """

    node_type = "hypothesis"

    def __init__(self, tmp_path, *, records_dir=None):
        self.root = Path(tmp_path)
        self.store = workspace_store(self.root)
        self.records_dir = Path(records_dir) if records_dir else self.root
        self.hook_state: dict = {}
        self.project_id = None
        self.run_id = "run-test"
        self.transcript: list = []

    def append_transcript(self, kind, **kw):
        self.transcript.append((kind, kw))

    def list_kb(self, _kind):
        return []

    def find_artifact_path(self, artifact_id):
        """跨节点查找 —— 与真 State 同名同义。

        假对象缺这个方法本身就是信号：真实实现里 freeze 必须能找到**别的节点**
        产出的 artifact（预注册在 plan/，freeze 常由 orchestrator 调）。账本是
        整个工作区一本，找不到返回 None —— 语义与真 State 一致。
        """
        head = self.store.head(artifact_id)
        return self.store.abs_path(head) if head is not None else None

    def read_artifact(self, artifact_id):
        return self.store.record(artifact_id)

    def mark_frozen(self, artifact_id, metadata_patch=None):
        return self.store.freeze(artifact_id, metadata_patch=dict(metadata_patch or {}),
                                 by_node=self.node_type, by_run=self.run_id)


def _write(state, artifact_id, record, *, directory=None):
    """把一份记录落进账本：正文是 `<directory>/<artifact_id>.<ext>`，账本一行 save。"""
    return state.store.save(
        artifact_id=artifact_id, artifact_type=record["type"], name=record["name"],
        content=record["content"], metadata=dict(record.get("metadata") or {}),
        directory=Path(directory) if directory else state.records_dir,
        created_at="2026-08-20T00:00:00+00:00", provenance=produced("hypothesis", "r-hyp"),
        produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
        by_node="hypothesis", by_run="r-hyp",
    )


def _meta(state, artifact_id) -> dict:
    """experiment 读到的那份 metadata：save 行的 metadata 折进 freeze 行的补丁。"""
    return dict(state.read_artifact(artifact_id)["metadata"])


@pytest.mark.asyncio
async def test_freeze_without_declaration_freezes_and_records_it(tmp_path):
    """判决拆除：缺 run_role 声明不再拒冻——冻结照做，冻结件如实写
    freeze_warnings + run_role_undeclared，且**不替作者编一份声明**（experiment
    对未声明的 prereg 按 secondary 记，账是真的）。墙若被加回来这条转红。"""
    state = _FakeState(tmp_path)
    _write(state, "pre_registration__x", _prereg())

    result = await _freeze_artifact(state=state, artifact_id="pre_registration__x")

    assert result["status"] == "success", result
    assert result["run_role_undeclared"] is True
    assert any("run_role" in w for w in result["freeze_warnings"])
    meta = _meta(state, "pre_registration__x")
    assert meta["frozen"] is True
    assert meta["run_role_undeclared"] is True
    assert any("run_role" in w for w in meta["freeze_warnings"])
    assert "run_role" not in meta and "analysis_eligible" not in meta


@pytest.mark.asyncio
async def test_freeze_writes_declaration_alongside_frozen(tmp_path):
    """声明必须和 frozen 同一次落盘：experiment 读的是冻结后的 metadata。"""
    state = _FakeState(tmp_path)
    _write(state, "pre_registration__x", _prereg())

    result = await _freeze_artifact(
        state=state, artifact_id="pre_registration__x",
        run_role="primary",
        expected_params={"n_particles": 4000},
    )

    assert result["status"] == "success"
    meta = _meta(state, "pre_registration__x")
    assert meta["frozen"] is True
    assert meta["run_role"] == "primary"
    assert "analysis_eligible" not in meta, "收掉的字段又被写回冻结件了（#979）"


@pytest.mark.asyncio
async def test_non_prereg_freeze_is_unaffected(tmp_path):
    """门只管 prereg。别的 artifact 不该被这条要求连坐。"""
    state = _FakeState(tmp_path)
    _write(state, "research_plan__x",
           {"type": "research_plan", "name": "p", "content": _STUB_CONTENT, "metadata": {}})

    result = await _freeze_artifact(state=state, artifact_id="research_plan__x")

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_gate_disabled_does_not_crash(tmp_path, monkeypatch):
    """应急关掉门禁时不能 NameError —— 声明变量只在 prereg 分支里算。"""
    monkeypatch.setenv("HARNESS_PREREG_GATE", "0")
    state = _FakeState(tmp_path)
    _write(state, "pre_registration__x", _prereg())

    result = await _freeze_artifact(state=state, artifact_id="pre_registration__x")

    assert result["status"] == "success"


# ── expected_params：声明"算正式证据"就得交出要比对的参数 ────────────────
#
# 这是上面那条的**同一个坑再往下一格**。experiment 的 preflight 对 primary run
# 要求 frozen prereg 的 metadata.expected_params，缺了在 safe_bash.py:3108 直接
# 硬拒（"禁止执行"）；而 nodes/experiment/hooks.py:1989 的注释写着"目前尚无节点
# 写它"。只修 run_role 的话，局面会从"跑得动但不算数"变成"根本跑不动"。


def test_primary_without_expected_params_is_rejected():
    violations, _ = _run_role_declaration_violations(_prereg(), "primary")
    assert violations
    assert "expected_params" in violations[0]


def test_primary_error_explains_it_blocks_execution_not_just_records():
    """报错要说清后果是"不让跑"，不是"记一笔"——否则没人会认真填。"""
    violations, _ = _run_role_declaration_violations(_prereg(), "primary")
    text = violations[0]
    assert "拒绝执行" in text or "不让跑" in text
    assert "不可逆" in text
    assert "secondary" in text          # 给出合法的退出口


def test_primary_with_expected_params_passes_and_records_them():
    params = {"n_particles": 4000, "timestep": 0.005}
    violations, declaration = _run_role_declaration_violations(
        _prereg(), "primary", params)
    assert not violations
    assert declaration["expected_params"] == params


def test_expected_params_may_come_from_metadata():
    record = _prereg(expected_params={"n_particles": 4000})
    violations, declaration = _run_role_declaration_violations(record, "primary")
    assert not violations
    assert declaration["expected_params"] == {"n_particles": 4000}


def test_empty_expected_params_is_not_enough():
    """空 dict 通不过下游比对，等于没填 —— 别让它假装满足了契约。"""
    violations, _ = _run_role_declaration_violations(_prereg(), "primary", {})
    assert violations


def test_secondary_does_not_need_expected_params():
    """探索性 run 的 preflight 本来就跳过比对，不该被这条连坐。"""
    violations, declaration = _run_role_declaration_violations(_prereg(), "secondary")
    assert not violations
    assert "expected_params" not in declaration


@pytest.mark.asyncio
async def test_freeze_writes_expected_params_into_frozen_metadata(tmp_path):
    """experiment 读的是**冻结后**的 metadata —— 必须同一次写进去。"""
    state = _FakeState(tmp_path)
    _write(state, "pre_registration__x", _prereg())
    params = {"n_particles": 4000, "cooling_rates": [5e-5, 5e-3]}

    result = await _freeze_artifact(
        state=state, artifact_id="pre_registration__x",
        run_role="primary", expected_params=params,
    )

    assert result["status"] == "success"
    meta = _meta(state, "pre_registration__x")
    assert meta["expected_params"] == params
    assert "analysis_eligible" not in meta, "收掉的字段又被写回冻结件了（#979）"


# ── freeze 必须找得到别的节点产出的 artifact ─────────────────────────────────
#
# E2E v24 实测活死锁：experiment 拒绝启动（"pre_registration 未 freeze"），
# orchestrator 去 freeze 却得到"找不到 artifact"，于是反复重派 —— 两边都在等
# 对方，而报错互相矛盾。原因是 freeze 只 glob 自己的记录目录，而预注册
# 是 hypothesis 节点的产物。
#
# 这与 v14/v15/v16 连栽三轮的那个 bug 是同一个：当时修了 `load_run_contract`，
# 这一处没跟着改 —— 同一个 bug 的另一份拷贝。现在账本是整个工作区一本，
# 这条钉的是 freeze 走账本、不走自己目录。


class _CrossNodeState(_FakeState):
    """两个节点目录：自己的 + 别人的，同一本工作区账。模拟 v2.1 的工作区布局。"""

    def __init__(self, tmp_path):
        own = Path(tmp_path) / _DIRS["experiment"]
        other = Path(tmp_path) / _DIRS["hypothesis"]
        own.mkdir(parents=True)
        other.mkdir(parents=True)
        super().__init__(tmp_path, records_dir=own)
        self.other_dir = other


@pytest.mark.asyncio
async def test_freeze_finds_an_artifact_owned_by_another_node(tmp_path) -> None:
    """预注册在 plan/，freeze 由别处调 —— 必须找得到。"""
    state = _CrossNodeState(tmp_path)
    _write(state, "pre_registration__X",
           {"type": "pre_registration", "name": "X", "content": _STUB_CONTENT, "metadata": {}},
           directory=state.other_dir)
    assert state.find_artifact_path("pre_registration__X").parent == state.other_dir

    result = await _freeze_artifact(
        state, artifact_id="pre_registration__X",
        run_role="primary",
        expected_params={"n_particles": 1000},
    )

    assert result.get("status") != "error", result
    assert state.store.head("pre_registration__X").frozen is True
    assert _meta(state, "pre_registration__X").get("frozen") is True


@pytest.mark.asyncio
async def test_missing_artifact_error_says_where_it_looked(tmp_path) -> None:
    """真的找不到时，报错要说清找过哪里、下一步做什么 —— 否则调用方只能猜。"""
    state = _CrossNodeState(tmp_path)
    result = await _freeze_artifact(
        state, artifact_id="pre_registration__ghost",
        run_role="secondary",
    )
    assert result["status"] == "error"
    assert "全部节点目录" in result["error"]
    assert "list_artifacts" in result["error"]


# ── 冻结是所有者对自己承诺的签字，不能代签 ──────────────────────────────────
#
# E2E v25 实测：orchestrator 为了解开 experiment 的门禁，去 freeze hypothesis
# 的预注册。写被边界守卫拦下并回滚，报错是"Node '_orchestrator' may only write
# .research/orchestration" —— 技术上没错，但**没说该找谁**，于是它撞了 3 次。
#
# 语义上也该拦：预注册是作者开工前锁定意图，冻结 = 他签字。调度器代签之后，
# "这条判据是谁承诺的"就答不清了。


class _OwnedState(_FakeState):
    """带 worktree 的 state：能分辨产物属于哪个节点（按目录反查 `_NODE_WORKSPACES`）。"""

    def __init__(self, tmp_path, *, node_type):
        root = Path(tmp_path)
        own = root / _DIRS[node_type]
        own.mkdir(parents=True, exist_ok=True)
        (root / _DIRS["hypothesis"]).mkdir(parents=True, exist_ok=True)
        super().__init__(root, records_dir=own)
        self.project_worktree = root
        self.node_type = node_type


def _put_prereg(state) -> Path:
    """hypothesis 的预注册：落 plan/，返回正文文件路径。"""
    _write(state, "pre_registration__P",
           {"type": "pre_registration", "name": "P", "content": _STUB_CONTENT, "metadata": {}},
           directory=state.project_worktree / _DIRS["hypothesis"])
    return state.find_artifact_path("pre_registration__P")


@pytest.mark.asyncio
async def test_another_node_cannot_freeze_your_commitment(tmp_path) -> None:
    state = _OwnedState(tmp_path, node_type="_orchestrator")
    target = _put_prereg(state)

    result = await _freeze_artifact(
        state, artifact_id="pre_registration__P",
        run_role="primary",
        expected_params={"n": 1},
    )

    assert result["status"] == "error"
    assert result.get("owner_node") == "hypothesis"
    # 报错必须指名下一步，否则调用方只能重试
    assert "run_node" in result["error"]
    assert "hypothesis" in result["error"]
    # 而且**没有动过**那份产物 —— 不是写了再回滚：账本上没有 freeze 行，文件原样
    assert state.store.head("pre_registration__P").frozen is False
    assert _meta(state, "pre_registration__P").get("frozen") is not True
    assert target.read_text(encoding="utf-8") == _STUB_CONTENT


@pytest.mark.asyncio
async def test_the_owner_can_still_freeze_its_own(tmp_path) -> None:
    """闸门不能只会挡：所有者自己冻必须照常放行。"""
    state = _OwnedState(tmp_path, node_type="hypothesis")
    target = _put_prereg(state)

    result = await _freeze_artifact(
        state, artifact_id="pre_registration__P",
        run_role="primary",
        expected_params={"n": 1},
    )

    assert result.get("status") != "error", result
    assert state.store.head("pre_registration__P").frozen is True
    assert _meta(state, "pre_registration__P").get("frozen") is True
    # 冻结不碰文件
    assert target.read_text(encoding="utf-8") == _STUB_CONTENT
