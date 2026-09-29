"""项目知道的，住在项目里 —— 一个项目一份项目层，成员共用（`docs/RFC_PROJECT_HOME_20260924.md`）。

平台给每个人一个 harness home；项目层从前默认住在 home 底下，于是一个项目几个成员就有几份：
B 的会话看不到 A 攒下的结论，同一个项目谁来看就读到谁那一份。现在平台**告诉** worker 与桥项目层
在哪（`projects_home`，和组织层同一个做法）。

判据落在行为上：两个人、同一个项目、各自的 home，写下的和读到的是不是同一份。
"""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

from core.llm import LLMResponse
from core.pause import clear_all
from platform_runtime import _temporary_home, serve_jsonl
from tests.test_platform_runtime import _write_instruction_files

REPO = Path(__file__).resolve().parents[1]
PROJECT = "project-shared"


def _a_claim(text: str) -> dict:
    return {"claim_text": text, "claim_type": "empirical", "concept_ids": ["c"],
            "orphan_reason": "测试", "sources": []}


def test_two_members_of_a_project_read_and_write_one_copy(tmp_path: Path, monkeypatch) -> None:
    from core.state import State

    projects, org = tmp_path / "projects", tmp_path / "org"
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")

    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "alice")
    with _temporary_home(tmp_path / "users" / "alice", org, projects):
        a = State.new(node_type="experiment", base_dir=projects / PROJECT / "runs", project_id=PROJECT)
        written, _ = a.write_kb("claims", _a_claim("30 GPa 以上偏差超出常压基准"))

    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "bob")
    with _temporary_home(tmp_path / "users" / "bob", org, projects):
        b = State.new(node_type="hypothesis", base_dir=projects / PROJECT / "runs", project_id=PROJECT)
        seen = {r["id"]: r for r in b.list_kb("claims")}

    assert written["id"] in seen, "同一个项目，另一个成员的会话读不到这条结论"
    assert seen[written["id"]]["created_by_user_id"] == "alice", "合成一份之后，谁写的还得认得出"
    assert not (tmp_path / "users" / "alice" / "projects").exists(), "项目层又落回了说话人的 home"


def test_untold_is_this_homes_own_and_everything_is_put_back(tmp_path: Path, monkeypatch) -> None:
    from core.paths import projects_root

    monkeypatch.setenv("HARNESS_FRAMEWORK_PROJECTS_HOME", "/somewhere/left/over")
    with _temporary_home(tmp_path / "home"):
        untold = projects_root()
    with _temporary_home(tmp_path / "home", None, tmp_path / "told"):
        told = projects_root()

    assert untold == tmp_path / "home" / "projects", "没说就该是这个 home 自己的 —— 不从环境里嗅"
    assert told == tmp_path / "told"
    assert projects_root() == Path("/somewhere/left/over"), "绑完没把环境还回去"


class _Watching:
    """一个只答一句的模型，答之前记下这一刻 harness 眼里的项目层。"""

    def __init__(self) -> None:
        self.projects_while_working: list[Path] = []
        self.stream_display = None

    async def chat(self, messages, **kwargs):
        from core.paths import projects_root

        self.projects_while_working.append(projects_root())
        return LLMResponse(content="好。", tool_calls=[], finish_reason="stop", usage={"total_tokens": 3})


@pytest.mark.asyncio
async def test_the_worker_works_in_the_project_home_it_was_told(tmp_path: Path) -> None:
    clear_all()
    home = tmp_path / "users" / "u1"
    _write_instruction_files(home, PROJECT)
    requests = [
        {"op": "init", "request_id": "init-1", "tenant_id": "tenant-test", "project_id": PROJECT,
         "session_id": "session-1", "home_dir": str(home), "projects_home": str(tmp_path / "projects")},
        {"op": "turn", "request_id": "turn-1", "message": "开始"},
        {"op": "terminate", "request_id": "terminate-1"},
    ]
    stream = io.StringIO("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
    events: list[dict] = []
    llm = _Watching()
    await serve_jsonl(stream, lambda kind, **payload: events.append({"type": kind, **payload}), llm=llm)
    clear_all()

    assert any(e["type"] == "result" for e in events), events
    assert llm.projects_while_working and set(llm.projects_while_working) == {tmp_path / "projects"}, (
        f"worker 干活时的项目层不是平台说的那一份：{llm.projects_while_working}")


def test_a_job_says_who_started_it(tmp_path: Path, monkeypatch) -> None:
    """账本合成一份之后，「谁在跑」不能再靠账本在谁的 home 里推。"""
    from core import jobs
    from core.state import State

    monkeypatch.setenv("HARNESS_FRAMEWORK_USER_ID", "alice")
    with _temporary_home(tmp_path / "users" / "alice", None, tmp_path / "projects"):
        st = State.new(node_type="experiment", base_dir=tmp_path / "projects" / PROJECT / "runs",
                       project_id=PROJECT)
        jobs.declare(st, purpose="高压 MD 扫描", resources="2×A100")
        loaded = jobs.load(st)
    observed = jobs.observe(tmp_path / "projects" / PROJECT)

    assert [j.by_user_id for j in loaded] == ["alice"]
    assert [j.by_user_id for j in observed] == ["alice"], "两条读法认出来的不是同一个人"


# ── 一处构造 ────────────────────────────────────────────────────────────────

#: 允许拼出项目层路径的地方 —— 各有理由。别处拼就绕开了平台说的那一份。
_ALLOWED = {
    ("core/paths.py", "projects_root"): "唯一来源：没被告知时的默认",
    ("platform_runtime.py", "_projects_of"): "桥上没被告知时的同一个默认",
    ("platform/backend/app/config.py", "the_projects_home"): "平台说项目层在哪 —— 一处回答",
    ("platform/backend/app/services/one_home_per_project.py", "merge_the_per_person_copies"):
        "读旧的按人分存的布局，把它合过来",
}


def _joins_projects(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return isinstance(node.right, ast.Constant) and node.right.value == "projects"
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        return name.endswith(("joinpath", "path.join", "Path")) and any(
            isinstance(a, ast.Constant) and a.value == "projects" for a in node.args)
    return False


def test_the_project_layer_is_built_in_one_place() -> None:
    files = [p for root in ("core", "shared", "platform/backend/app") for p in (REPO / root).rglob("*.py")]
    files.append(REPO / "platform_runtime.py")
    found = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not _joins_projects(node):
                continue
            where = node
            while where in parents and not isinstance(where, (ast.FunctionDef, ast.AsyncFunctionDef)):
                where = parents[where]
            function = getattr(where, "name", "<module>")
            key = (str(path.relative_to(REPO)), function)
            if key not in _ALLOWED:
                found.append(f"{key[0]}:{node.lineno} in {function}: {ast.unparse(node)}")
    assert not found, (
        "项目层路径在别处拼了 —— 走 core.paths.projects_root()（平台会告诉它在哪）：\n" + "\n".join(found))
