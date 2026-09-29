"""子 run 不继承盖不住自己根的冻结 manifest（#769）。

lujy 在 node20 wrf-compile 实测：hardened sandbox 下 orchestrator 派出的 experiment
子 run 连 `echo probe-ok` 都被拒 ——

    sandbox_capability_not_frozen
    manifest_run_id = orchestrator__wrf-compile     ← 父的
    state_run_id    = 1788314878-16b7e9             ← 子的
    missing_writable_roles: ["workspace_root", "run_root"]

因果链：CLI 下父的本地 manifest 只冻它自己的两个根（`write_roots_for`）；
`execute_node` 把它原样复制给子；`manifest_for` 见 state 上已有 manifest 就复用、
hash 钉死；子的 workspace_root / run_root 永远不在里面。两条 core 自有不变量
（一份 manifest 一个 attempt / spawn 时原样复制）互相矛盾，且没有重签 API。

判据落在事实上：父 manifest **盖得住**子的可写根才继承（平台 attempt 把整个
worktree 冻成 rw，就是这种），盖不住就让子按自己的根铸自己的。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from core import sandbox
from core.executor import _inherit_sandbox_capability
from core.project_workspace import bind_project_workspace
from core.state import State


def _worktree(tmp_path: Path) -> Path:
    """bind_project_workspace 要一个有 HEAD 的仓库（它读 rev-parse HEAD）。"""
    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=root, check=True, env=env)
    return root


def _state(tmp_path: Path, worktree: Path, node_type: str) -> State:
    st = State.new(node_type=node_type, base_dir=tmp_path / "runs", project_id="p769")
    bind_project_workspace(st, worktree)
    return st


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    monkeypatch.setattr(sandbox, "trusted_image_id", lambda: "sha256:" + "b" * 64)
    monkeypatch.setattr(sandbox, "effective_security_profile", lambda: "portable")


def _events(st: State, name: str) -> list[dict]:
    if not st.transcript_path.exists():
        return []
    return [json.loads(l) for l in st.transcript_path.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("event") == name]


# ── CLI：父的本地 manifest 盖不住子 → 子铸自己的 ──────────────────────────────


def test_cli_child_does_not_inherit_a_manifest_that_cannot_cover_it(tmp_path):
    wt = _worktree(tmp_path)
    parent = _state(tmp_path, wt, "_orchestrator")
    # 父在第一次 shell 调用时铸的本地 manifest：只有它自己的两个根
    parent_manifest = sandbox.manifest_for(parent, writable_roots=sandbox.write_roots_for(parent))
    assert parent.sandbox_manifest_hash == parent_manifest.sha256

    child = _state(tmp_path, wt, "experiment")
    # 前提：子的根确实不在父 manifest 里（这就是事故的形状）
    assert sandbox.uncovered_write_roots(parent.sandbox_manifest, child), "前提没成立"

    _inherit_sandbox_capability(child, parent)

    assert child.sandbox_manifest is None, "盖不住还继承 = 子 run 全部 shell 判死"
    assert child.sandbox_manifest_hash is None
    # 子按自己的根铸自己的 attempt，从此 echo 能跑
    own = sandbox.manifest_for(child, writable_roots=sandbox.write_roots_for(child))
    assert own.attempt_id != parent_manifest.attempt_id
    assert own.run_id == child.run_id
    # 事实进 transcript
    reissued = _events(child, "sandbox_capability_reissued")
    assert len(reissued) == 1 and reissued[0]["parent_run_id"] == parent.run_id
    assert any(str(child.root) in p for p in reissued[0]["uncovered_write_roots"])


def test_the_old_behaviour_is_the_incident(tmp_path):
    """把父 manifest 原样复制给子，就是 lujy 看到的那条错误。"""
    wt = _worktree(tmp_path)
    parent = _state(tmp_path, wt, "_orchestrator")
    sandbox.manifest_for(parent, writable_roots=sandbox.write_roots_for(parent))
    child = _state(tmp_path, wt, "experiment")
    child.sandbox_manifest = dict(parent.sandbox_manifest)
    child.sandbox_manifest_hash = parent.sandbox_manifest_hash
    with pytest.raises(sandbox.SandboxContractError, match="writable root was not frozen"):
        sandbox.manifest_for(child, writable_roots=sandbox.write_roots_for(child))


# ── 平台：attempt manifest 把整个 worktree 冻成 rw → 子照旧继承，同一个 attempt ──


def test_platform_child_inherits_the_attempt_manifest_that_covers_it(tmp_path):
    wt = _worktree(tmp_path)
    parent = _state(tmp_path, wt, "_orchestrator")
    child = _state(tmp_path, wt, "experiment")
    # dispatcher 签的形状：会话 worktree 整个 rw（_freeze_attempt_sandbox_manifest）
    # —— 子的 run_root 在 runs/ 下不在 worktree 里，也一并冻进去（平台上它在
    # worktree/.research/cache/runtime/runs 里；这里用 tmp 目录模拟同一事实）。
    attempt = sandbox.SandboxManifest(
        attempt_id="attempt-platform-1", run_id=parent.run_id,
        mounts=((str(tmp_path.resolve()), "rw"),),        # 整个会话根 rw，worktree 与 runs 都在里面
        image_id="sha256:" + "b" * 64,
        security_profile="portable",
    )
    attempt.validate()
    parent.sandbox_manifest = attempt.canonical_payload()
    parent.sandbox_manifest_hash = attempt.sha256
    parent.platform_attempt_id = "attempt-platform-1"

    _inherit_sandbox_capability(child, parent)

    assert child.sandbox_manifest_hash == attempt.sha256, "盖得住就该共用同一个 attempt"
    assert child.platform_attempt_id == "attempt-platform-1"
    assert sandbox.manifest_for(child, writable_roots=sandbox.write_roots_for(child)).sha256 == attempt.sha256
    assert _events(child, "sandbox_capability_reissued") == []


# ── 接线：execute_node 走的是这个判断，不是直接赋值 ────────────────────────────


def test_execute_node_routes_inheritance_through_the_coverage_check():
    src = Path(__file__).resolve().parents[1] / "core" / "executor.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_node")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_inherit_sandbox_capability"]
    assert calls, "execute_node 没有经 _inherit_sandbox_capability 决定子 run 的沙箱能力"
    direct = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Attribute) and t.attr == "sandbox_manifest" for t in n.targets)]
    assert not direct, "execute_node 里又出现了对 sandbox_manifest 的直接赋值（第二份判断）"
