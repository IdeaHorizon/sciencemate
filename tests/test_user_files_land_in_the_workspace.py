"""用户交来的文件必须落在工作区里，而且模型必须知道它在。

## 缺口（2026-09-04）

同事从界面传了一个 800MB 的日志包，agent 全盘 `find` 找不到，于是让用户
"把文件放到固定地址"—— 而用户手上没有 shell。逐层核实后有两件事：

  1. 那次上传**根本没成功**（会话附件上限 64 MiB，413），而前端那个 mutation
     只写了 onSuccess，没有 onError —— 界面一声不响。
  2. 即使传成功也没用：文件落 `.research/runtime/attachments/`（gitignored、
     只此会话、publish 带不走），或者落 canonical main（**已存在的会话**读
     不到，分支早就分出去了）。用户在会话里传的文件正好落进两者中间那个洞。
     而且模型没有任何机制知道盘上多了个文件 —— 唯一的告知通道是前端往用户
     的输入草稿里塞一行「📎 附件：路径」，用户一删就没了。

所以这里钉的不是"上传能不能成功"，是**落点的形状**和**到达模型的那一步**。

## 闸

- G1 全仓只有一条落盘的上传路径
- G2 落点形状：字节在池里、worktree 里是硬链接（或同 sha 的拷贝）、gitignored
- G3 新会话 worktree 拿得到字节（`.ref` 过来了、字节没有 → materialize 补齐）
- G4 模型看得见：注入里出现**绝对路径**
- G6 上限只有一个数，且送到调用方手上
（G5 在前端：`platform/frontend/src/features/sessions/*.test.ts`）
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import materials

REPO = Path(__file__).resolve().parents[1]


# ── 夹具：一个真 Git 项目 + 一个真会话 worktree ─────────────────────────────
#
# 不绑真 worktree 的测试看不见这一层的缺陷：写方读方恰好同锚，绿得毫无意义
# （2026-08-09 配图闸 18 天全红而测试全绿就是这么来的）。


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repositories" / "p1"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "research-platform@localhost")
    _git(root, "config", "user.name", "Research Platform")
    (root / "PROJECT.md").write_text("# p1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _session(project: Path, name: str = "s1") -> Path:
    path = project.parent.parent / "worktrees" / "p1" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(project, "worktree", "add", "-b", f"session/{name}", str(path), "main")
    return path


def _commit_material(root: Path, paths) -> None:
    _git(root, "add", "--", *paths)
    _git(root, "commit", "-m", "materials: add", "--", *paths)


# ── G2 落点形状 ─────────────────────────────────────────────────────────────


def test_the_bytes_live_in_the_pool_and_the_worktree_holds_a_hardlink(project, tmp_path):
    source = tmp_path / "podsys_asc_log.tar.gz"
    source.write_bytes(b"log" * 100_000)
    reference, _ = materials.place(project, source.name, source, uploaded_by="u1")

    entity = project / reference.path
    pooled = materials.pool_root(project) / reference.sha256[:2] / reference.sha256
    assert entity.is_file(), "用户交来的文件必须是工作区里一个真实文件"
    assert pooled.is_file(), "字节必须在池里，池才是跨 worktree 共用的那份"
    assert entity.stat().st_ino == pooled.stat().st_ino, (
        "worktree 里的实体应当是池对象的硬链接 —— 拷贝会让一份 800MB 的材料"
        "在每个会话里各占一份盘"
    )
    assert hashlib.sha256(entity.read_bytes()).hexdigest() == reference.sha256


def test_the_pool_is_shared_by_every_worktree_of_the_project(project):
    session = _session(project)
    assert materials.pool_root(session) == materials.pool_root(project), (
        "池的位置必须能从 worktree 自己推出来并且答案唯一 —— 两个进程各算出"
        "一个池，就是同一个问题两个真相源"
    )


def test_the_byte_file_is_gitignored_but_the_pointer_is_tracked(project, tmp_path):
    source = tmp_path / "big.csv"
    source.write_bytes(b"a,b\n" * 50_000)
    reference, paths = materials.place(project, source.name, source, uploaded_by="u1")
    _commit_material(project, paths)

    ignored = subprocess.run(
        ["git", "-C", str(project), "check-ignore", "-q", reference.path], check=False
    )
    assert ignored.returncode == 0, (
        "字节必须 gitignored。没有这条忽略，一个 800MB 的未跟踪文件会让 "
        "`_worktree_has_blocking_changes` 永久为真，产物写入与 checkpoint 全部停摆"
    )
    tracked = _git(project, "ls-files", materials.MATERIALS_RELATIVE).splitlines()
    assert reference.ref_path in tracked, "指针必须进版本，否则跨会话就没有出处"
    assert reference.path not in tracked, "字节不进版本"


def test_adding_a_file_leaves_the_worktree_clean(project, tmp_path):
    """提交后工作树必须干净 —— 否则平台自己的写入闸会被自己顶住。"""
    source = tmp_path / "notes.md"
    source.write_bytes(b"# notes\n")
    _, paths = materials.place(project, source.name, source, uploaded_by="u1")
    _commit_material(project, paths)
    assert _git(project, "status", "--porcelain") == ""


def test_the_pointer_carries_the_whole_proposition(project, tmp_path):
    """记录要记下整个命题：谁、何时、多大、什么内容、哪来的、备注。"""
    source = tmp_path / "runs.jsonl"
    source.write_bytes(b'{"x":1}\n')
    reference, _ = materials.place(
        project, source.name, source, uploaded_by="user-42",
        uploaded_at="2026-09-04T10:00:00+00:00", note="第三方给的原始日志",
        material_source="upload",
    )
    document = json.loads((project / reference.ref_path).read_text(encoding="utf-8"))
    assert document == {
        "schema_version": 1,
        "sha256": reference.sha256,
        "size_bytes": 8,
        "uploaded_by": "user-42",
        "uploaded_at": "2026-09-04T10:00:00+00:00",
        "source": "upload",
        "note": "第三方给的原始日志",
    }


def test_replaying_the_same_file_is_idempotent(project, tmp_path):
    source = tmp_path / "same.bin"
    source.write_bytes(b"same")
    first, paths = materials.place(project, source.name, source, uploaded_by="u1")
    _commit_material(project, paths)
    second, _ = materials.place(project, source.name, source, uploaded_by="u2")
    assert second.sha256 == first.sha256
    assert second.uploaded_by == "u1", "幂等命中不改写第一次上传的事实"
    assert _git(project, "status", "--porcelain") == ""


def test_the_same_name_with_different_bytes_is_refused(project, tmp_path):
    one, two = tmp_path / "a", tmp_path / "b"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    materials.place(project, "report.pdf", one, uploaded_by="u1")
    with pytest.raises(materials.MaterialNameConflictError) as raised:
        materials.place(project, "report.pdf", two, uploaded_by="u1")
    assert "换个文件名" in str(raised.value), "报错必须说出下一步该怎么做"


def test_a_pointer_shaped_name_is_refused(project, tmp_path):
    source = tmp_path / "x"
    source.write_bytes(b"x")
    with pytest.raises(materials.MaterialError):
        materials.place(project, f"data{materials.REF_SUFFIX}", source, uploaded_by="u1")


def test_an_oversized_file_leaves_nothing_behind(project, tmp_path):
    source = tmp_path / "huge.bin"
    source.write_bytes(b"x" * 5_000)
    with pytest.raises(materials.MaterialTooLargeError) as raised:
        materials.place(project, source.name, source, uploaded_by="u1", max_bytes=1_000)
    assert raised.value.max_bytes == 1_000
    assert not (project / materials.MATERIALS_RELATIVE / source.name).exists()
    incoming = materials.pool_root(project) / "incoming"
    assert not any(incoming.iterdir()), (
        "中途拒掉不能在池里留下一个名字正确、内容不全的对象 —— 那种对象"
        "事后无法与正确的区分开"
    )


# ── G3 新会话拿得到字节 ─────────────────────────────────────────────────────


def test_a_new_session_worktree_gets_the_bytes_back(project, tmp_path):
    """`.ref` 随 Git 分支过来，字节不会。materialize 是那半步。"""
    source = tmp_path / "corpus.tar.gz"
    payload = b"corpus" * 20_000
    source.write_bytes(payload)
    reference, paths = materials.place(project, source.name, source, uploaded_by="u1")
    _commit_material(project, paths)

    session = _session(project)
    assert (session / reference.ref_path).is_file(), "指针随分支过来了"
    assert not (session / reference.path).exists(), "字节不进版本，所以此刻还没有"

    restored, missing = materials.materialize(session)
    assert [item.name for item in restored] == [reference.name]
    assert missing == []
    assert (session / reference.path).read_bytes() == payload
    assert _git(session, "status", "--porcelain") == "", "补字节不该弄脏会话工作树"


def test_materialize_is_idempotent(project, tmp_path):
    source = tmp_path / "d.bin"
    source.write_bytes(b"d")
    _, paths = materials.place(project, source.name, source, uploaded_by="u1")
    _commit_material(project, paths)
    session = _session(project)
    materials.materialize(session)
    restored, missing = materials.materialize(session)
    assert (restored, missing) == ([], []), "第二次调用什么都不该做"


def test_a_pointer_without_bytes_is_reported_not_swallowed(project, tmp_path):
    """池里没有这个 sha：如实说"缺"，别装作没这份材料。

    "我查了，没有"和"我没查"对模型是天差地别的两件事。
    """
    directory = project / materials.MATERIALS_RELATIVE
    directory.mkdir(parents=True, exist_ok=True)
    materials.ensure_gitignore(project)
    (directory / f"ghost.csv{materials.REF_SUFFIX}").write_text(
        json.dumps({"schema_version": 1, "sha256": "0" * 64, "size_bytes": 7,
                    "uploaded_by": "u1", "uploaded_at": "2026-09-04T00:00:00+00:00",
                    "source": "upload", "note": ""}),
        encoding="utf-8",
    )
    restored, missing = materials.materialize(project)
    assert restored == []
    assert [item.name for item in missing] == ["ghost.csv"]
    [listed] = materials.inventory(project)
    assert listed.present is False
    assert "字节不在本工作区" in materials.describe([listed])[0]


# ── G4 模型看得见 ───────────────────────────────────────────────────────────


def test_the_injection_carries_an_absolute_path(project, tmp_path):
    """给绝对路径。相对路径锚在**节点自己的目录**上，给它等于把"锚点是谁"
    这个已经错过一次的问题再交给模型去猜。"""
    source = tmp_path / "podsys_asc_log.tar.gz"
    source.write_bytes(b"z" * 1024)
    reference, _ = materials.place(project, source.name, source, uploaded_by="u1")
    [line] = materials.describe([reference])
    absolute = str((project / reference.path).resolve())
    assert absolute in line
    assert line.count(reference.path) == 1, (
        "相对路径只该作为绝对路径的尾巴出现一次 —— 同一行里给两个坐标系"
        "就是给了两个答案，而模型此刻正是在锚点上出错"
    )
    assert materials.human_size(reference.size_bytes) in line, "大小要给人看的数"


def test_the_hook_hands_the_model_the_files_it_did_not_upload_itself(project, tmp_path):
    """`user_files` hook 是"上传了 agent 却不知道"的根治那一步。"""
    from core.loop_hooks import HookContext, get_loop_hook

    class _Harness:
        node_type = "_orchestrator"

    class _State:
        def __init__(self, worktree):
            self.project_worktree = worktree
            self.hook_state: dict = {}
            self.transcript: list = []

        def append_transcript(self, event, **fields):
            self.transcript.append((event, fields))

    source = tmp_path / "podsys_asc_log.tar.gz"
    source.write_bytes(b"z" * 2048)
    reference, _ = materials.place(project, source.name, source, uploaded_by="u1")

    hook = get_loop_hook("user_files")
    assert hook is not None and hook.on_turn_start is not None
    state = _State(project)
    context = HookContext(harness=_Harness(), state=state, messages=[], turn=1)

    injected = hook.on_turn_start(context)
    assert injected, "第一轮必须把盘上已有的用户文件全量递过去"
    content = injected[0].content
    assert str((project / reference.path).resolve()) in content
    assert injected[0].role == "user", (
        "中段以 system 注入实测会让模型复述注入文本、输出膨胀 3×（PR#462）"
    )
    assert any(event == "user_files_injected" for event, _ in state.transcript)

    assert hook.on_turn_start(context) is None, "没有新文件就不再重复注入"

    second = tmp_path / "extra.csv"
    second.write_bytes(b"a,b\n")
    materials.place(project, second.name, second, uploaded_by="u1")
    again = hook.on_turn_start(context)
    assert again is not None
    assert "extra.csv" in again[0].content
    assert reference.name not in again[0].content, "第二次只给新增的那份"


def test_the_situation_section_keeps_saying_the_files_are_there(project, tmp_path):
    """局面段每条用户消息前重算 —— 注入被压缩掉之后，只有它还记得。

    一个模型忘掉用户传过文件，跟没传过没有区别。
    """
    from core import research_situation as rs

    source = tmp_path / "data.csv"
    source.write_bytes(b"a,b\n")
    materials.place(project, source.name, source, uploaded_by="u1")

    class _State:
        project_worktree = project
        project_root = None

    situation = rs.compute_situation(_State())
    assert situation.user_file_count == 1
    rendered = "\n".join(rs.render_situation_facts(situation))
    assert "用户交来 1 份文件" in rendered
    assert str((project / materials.MATERIALS_RELATIVE).resolve()) in rendered


def test_the_orchestrator_actually_enables_the_hook(project=None):
    """声明了的 hook 必须真的挂在节点上 —— 否则机制存在但没接到路径。"""
    import yaml

    harness = yaml.safe_load(
        (REPO / "nodes/_orchestrator/harness.yaml").read_text(encoding="utf-8")
    )
    assert "user_files" in (harness.get("loop_hooks") or [])


# ── G1 只有一条落盘的上传路径 ───────────────────────────────────────────────


def _upload_endpoints() -> list[tuple[str, str]]:
    """全仓所有"收 UploadFile 并落盘"的端点。

    扫盘，不写名单：写名单的话，以后新增一个上传入口就默认漏过，而且 CI 全绿
    没人会知道（`feedback_guardrails_must_scan_not_list`）。
    """
    found: list[tuple[str, str]] = []
    for path in sorted((REPO / "platform/backend/app").rglob("*.py")):
        if "/tests/" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            annotations = [
                ast.unparse(argument.annotation)
                for argument in [*node.args.args, *node.args.kwonlyargs]
                if argument.annotation is not None
            ]
            if any("UploadFile" in text for text in annotations):
                found.append((str(path.relative_to(REPO)), node.name))
    return found


def test_there_is_exactly_one_upload_entry_point():
    endpoints = _upload_endpoints()
    assert endpoints == [
        ("platform/backend/app/api/v1/sessions.py", "add_file_to_session"),
    ], (
        "上传只能有一条落盘路径。老实现有两条（会话附件 + 项目材料），落在两个"
        f"地方、各覆盖一半场景，用户在会话里传的文件谁都读不到。现在有：{endpoints}"
    )


def test_nothing_still_points_at_the_deleted_upload_paths():
    """删了就得删干净：路由、UI、清单，一处不留。"""
    stale = ("attachments/promote", "MANIFEST.jsonl", "ProjectMaterialsPanel",
             "attachToSession", "write_material", "list_materials")
    offenders: list[str] = []
    for root, patterns in (
        (REPO / "platform/backend/app", ("*.py",)),
        (REPO / "platform/frontend/src", ("*.ts", "*.tsx", "*.css")),
    ):
        for pattern in patterns:
            for path in root.rglob(pattern):
                text = path.read_text(encoding="utf-8", errors="replace")
                for needle in stale:
                    if needle in text:
                        offenders.append(f"{path.relative_to(REPO)}: {needle}")
    assert offenders == [], f"还有代码指着已经删掉的那两条路：{offenders}"


# ── G6 上限只有一个数，且送到调用方手上 ─────────────────────────────────────


def test_the_size_limit_reaches_the_caller():
    """契约必须送到调用方：上限多少、超了走哪条路，都在报错里说全，而且
    前端读的是**同一个数**（`session.materialMaxBytes`），不是自己写的常量。"""
    sessions = (REPO / "platform/backend/app/api/v1/sessions.py").read_text(encoding="utf-8")
    assert "settings.material_max_bytes" in sessions
    assert '"maxBytes"' in sessions, "报错必须带上限的数字"

    service = (REPO / "platform/backend/app/services/sessions.py").read_text(encoding="utf-8")
    assert '"materialMaxBytes": settings.material_max_bytes' in service, (
        "上限要由后端发给前端 —— 前端自己写常量，调上限那天它会继续按老数字放行"
    )

    composer = (
        REPO / "platform/frontend/src/features/sessions/components/SessionComposerBar.tsx"
    ).read_text(encoding="utf-8")
    assert "maxFileBytes" in composer and "file.size > maxFileBytes" in composer, (
        "前端要在**开始传**之前判上限，别让用户传完 3 GiB 才收到 413"
    )


def test_the_backend_never_imports_core_at_module_level() -> None:
    """App Server 进程里 `core` 不在 import path 上，而 pytest 里在。

    2026-09-04 真起一次 uvicorn 才照出来：我把 `from core import materials` 写在
    两个后端模块顶部，全套测试（935 条）**全绿**，而真进程 `ModuleNotFoundError:
    No module named 'core'` 当场起不来 —— 部署 cwd 是 `platform/backend`，pytest
    却把仓库根加进了 sys.path。测试和生产在同一个问题上有两个答案。

    正解是走 `harness_contract`（那个模块本来就是为此存在的：`sys.modules['core']`
    是进程全局的，来路不明的同名 `core` 会静默赢）。判据扫盘不写名单。
    """
    offenders: list[str] = []
    for path in sorted((REPO / "platform/backend/app").rglob("*.py")):
        if "/tests/" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:              # 只看**模块顶层**
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "core":
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "core":
                        offenders.append(
                            f"{path.relative_to(REPO)}:{node.lineno} import {alias.name}"
                        )
    assert offenders == [], (
        "后端不许在模块顶层导入 harness 的 `core` —— 真部署起不来，而测试全绿。"
        "改走 app.services.harness_contract 的访问器：\n  " + "\n  ".join(offenders)
    )
