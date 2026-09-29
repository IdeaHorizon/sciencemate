"""修订别人的证据，不等于产出了那份证据。

2026-09-01 本机 E2E 实拍：一趟「只修订上游账本」的 run 被焊死。

  experiment_log__q3_phase_diagram_n10_v3
    v1–v4  produced_by_run_id = 1788167191-c211cc   ← 真做实验那趟
    v5     produced_by_run_id = 1788253570-08ac26   ← 只来改了一行 not_run 的那趟

两个后果：
  1. 修订方名下凭空多出一份它没做过的 canonical experiment_log，叠加自己的
     收尾 log → audit_experiment_log_integrity 判「one run may have only one
     canonical experiment_log」→ run 被焊死，而它要做的事其实已经做完了。
  2. **真做实验那趟名下的 experiment_log 归零** —— 证据被静默改嫁。
     这一条比死锁坏，因为它不报错。

消费方 contract_audit.current_run_artifacts 的注释里明写它依赖的是
"the **immutable** provenance stamped by State.save_artifact"。
平台两半对同一个字段的理解正相反。

这条路径是 2026-08-04 那次修复漏掉的一条：那次把转发路径的产出方保住了，
修订路径没跟上 —— 同一个缺陷换一条到达路径又活了一次。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from core.state import State

PRODUCER_RUN = "1788167191-c211cc"
AMENDER_RUN = "1788253570-08ac26"
LOG_ID = "experiment_log__q3"


BASE: dict[str, Path] = {}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def _worktree() -> Path:
    root = Path(tempfile.mkdtemp()) / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    (root / "experiments").mkdir()
    (root / "experiments" / "README.md").write_text("# experiments\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _bound(run_id: str) -> State:
    """两个 run 绑同一个项目工作区 —— 真实形态：产物是**节点目录**里的原生文件
    （`experiments/experiment_log__q3.md`）+ 工作区账本，不同 run 看到同一份。"""
    st = State.new(node_type="experiment", base_dir=Path(tempfile.mkdtemp()),
                   project_id="p1", project_worktree=BASE["worktree"])
    st.run_id = run_id
    return st


def _producer() -> State:
    BASE["worktree"] = _worktree()
    return _bound(PRODUCER_RUN)


def _amender() -> State:
    return _bound(AMENDER_RUN)


def _make_log(st: State) -> str:
    """产出并**冻结** —— 生产里的 q3 log 就是冻结件，修订路径只对冻结件生效。"""
    st.save_artifact("experiment_log", "q3", "Q3#3: not_run", metadata={})
    st.mark_frozen(LOG_ID, {})
    return LOG_ID


def _read(st: State) -> dict:
    return st.read_artifact(LOG_ID)


def test_amending_keeps_the_original_producer() -> None:
    prod = _producer()
    _make_log(prod)
    assert _read(prod)["produced_by_run_id"] == PRODUCER_RUN

    amender = _amender()
    amender.save_artifact("experiment_log", "q3", "Q3#3: not_applicable",
                          amendment_reason="Q4 撤销，条件失去对象")

    rec = _read(amender)
    assert rec["version"] == 2
    assert rec["content"] == "Q3#3: not_applicable"
    assert rec["produced_by_run_id"] == PRODUCER_RUN, (
        "修订把作者身份改嫁给了修订者 —— 真做实验那趟名下的证据凭空消失")


def test_the_amender_is_still_recorded() -> None:
    """保留原产出方**不能**以丢失"谁改的"为代价 —— 两个事实都要在。"""
    prod = _producer()
    _make_log(prod)
    amender = _amender()
    amender.save_artifact("experiment_log", "q3", "改过了",
                          amendment_reason="理由")

    amendment = _read(amender).get("amendment") or {}
    assert amendment.get("by_run_id") == AMENDER_RUN, f"修订者没被记下：{amendment}"
    assert amendment.get("reason") == "理由"


def test_the_run_that_only_amended_owns_no_evidence_it_did_not_make() -> None:
    """这是死锁的机械判据：按 run 归属清点，修订方名下不该有这份 log。"""
    from nodes.experiment.tools.contract_audit import current_run_artifacts

    prod = _producer()
    _make_log(prod)
    amender = _amender()
    amender.save_artifact("experiment_log", "q3", "改过了", amendment_reason="r")

    amender_owned = [a["id"] for a in current_run_artifacts(amender, "experiment_log")]
    assert LOG_ID not in amender_owned, (
        f"修订方名下多出一份它没做过的 canonical log：{amender_owned}")

    producer_owned = [a["id"] for a in current_run_artifacts(prod, "experiment_log")]
    assert LOG_ID in producer_owned, (
        f"真产出方名下的证据被改嫁走了：{producer_owned}")


def test_an_explicit_provenance_still_wins() -> None:
    """转发/导入路径显式传 provenance 时，优先级不变（别把上一条修复覆盖掉）。"""
    from core.artifact_provenance import produced

    prod = _producer()
    _make_log(prod)
    amender = _amender()
    amender.save_artifact("experiment_log", "q3", "改过了", amendment_reason="r",
                          provenance=produced("literature", "run-xyz"))
    assert _read(amender)["produced_by_run_id"] == "run-xyz"


def test_a_first_write_is_authored_by_its_writer() -> None:
    """反向：首次写入当然归写它的人 —— 别把保留写成了永不署名。"""
    prod = _producer()
    _make_log(prod)
    assert _read(prod)["produced_by_run_id"] == PRODUCER_RUN
    assert _read(prod)["version"] == 1
