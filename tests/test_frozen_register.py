"""P6：冻结即登记 —— freeze 是工作区账本上的一行（`.research/ledger/records.jsonl`），
平台 commit 咽喉据此执法。

冻结的全部意义是"从此改不了"。此前它只是 metadata 里一个 `frozen: true`，
执法散在各工具的自查里：谁绕开工具直接写文件（或者像 E2E v19 里那样把产物
抄到别处再冻副本），闸就不存在。P6 把执法收到唯一咽喉 —— 平台持有 Git
commit 权，checkpoint 时冻结路径的任何内容改动都进不了库。

freeze 行钉的是 `path@sha256`；文件一个字节不动，所以文件哈希 = 正文哈希 =
账本钉死的哈希（`workspace_store(root).pinned()` 就是咽喉读的那张表）。

这里测 harness 侧的登记动作；咽喉执法在
platform/backend/tests/test_project_repository.py::test_frozen_* 里测。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from core.ledger import LEDGER_RELATIVE, workspace_store
from core.project_workspace import _NODE_WORKSPACES, bind_project_workspace
from core.state import State
from shared.tools.library.artifacts_extra import _freeze_artifact

NODES = ("literature", "hypothesis", "data", "experiment", "postprocess", "writing")


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@e.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    for node in NODES:
        (root / _NODE_WORKSPACES[node]).mkdir(parents=True)
        (root / _NODE_WORKSPACES[node] / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return root


def _hypothesis_state(tmp_path: Path, root: Path) -> State:
    state = State.new(node_type="hypothesis", base_dir=tmp_path / "runs",
                      project_id="p6")
    bind_project_workspace(state, root)
    return state


def _freeze_rows(root: Path, artifact_id: str) -> list[dict]:
    rows = [json.loads(line) for line in
            (root / LEDGER_RELATIVE).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "freeze" and r.get("id") == artifact_id]


@pytest.mark.asyncio
async def test_freeze_appends_register_line_with_content_hash(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    state = _hypothesis_state(tmp_path, root)
    state.save_artifact("survey_report", "X", "# survey\n", metadata={})

    result = await _freeze_artifact(state=state, artifact_id="survey_report__X")
    assert result["status"] == "success", result

    rows = _freeze_rows(root, "survey_report__X")
    assert rows, "冻结没有落账本 —— 咽喉执法就无从谈起"
    row = rows[-1]
    frozen_file = root / row["path"]
    assert frozen_file.exists()
    # 冻结不碰文件：账本钉的哈希 = 冻结后文件的哈希 = 正文哈希。
    assert row["sha256"] == hashlib.sha256(frozen_file.read_bytes()).hexdigest()
    assert frozen_file.read_text(encoding="utf-8") == "# survey\n"
    assert row["by_node"] == "hypothesis"
    assert workspace_store(root).pinned() == {row["path"]: row["sha256"]}, \
        "咽喉读的钉死表要正好是这一条"


@pytest.mark.asyncio
async def test_register_path_is_worktree_relative(tmp_path: Path) -> None:
    """咽喉在平台侧、以 worktree 根为基准比对 —— 绝对路径它认不出来。"""
    root = _worktree(tmp_path)
    state = _hypothesis_state(tmp_path, root)
    state.save_artifact("survey_report", "Y", "# survey\n", metadata={})
    await _freeze_artifact(state=state, artifact_id="survey_report__Y")

    row = _freeze_rows(root, "survey_report__Y")[-1]
    assert not Path(row["path"]).is_absolute(), row["path"]
    assert row["path"] == "plan/survey_report__Y.md"


@pytest.mark.asyncio
async def test_refreezing_does_not_duplicate_register_lines(tmp_path: Path) -> None:
    """重复 freeze 是 no-op（already_frozen），不许把账本刷成流水账。"""
    root = _worktree(tmp_path)
    state = _hypothesis_state(tmp_path, root)
    state.save_artifact("survey_report", "Z", "# survey\n", metadata={})
    await _freeze_artifact(state=state, artifact_id="survey_report__Z")
    again = await _freeze_artifact(state=state, artifact_id="survey_report__Z")
    assert again.get("already_frozen") is True

    assert len(_freeze_rows(root, "survey_report__Z")) == 1
