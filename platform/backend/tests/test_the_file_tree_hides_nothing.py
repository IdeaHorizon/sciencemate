"""文件树不许让任何一个目录凭空消失，到顶了必须说出来。

## 钉住的现场（2026-09-09，qinp 的 Podsys 论文项目）

会话工作区里 9171 个文件：`.research/` 4542、`observation/extracted/` 4520、
`paper/` 50。`file_tree` 当时把整棵树排成一个**平坦**列表、按路径字母序切
前 5000 条 —— `.research` 和 `observation` 就把配额吃光，于是字母序排在 `o`
后面的每一个顶层目录（`figures/` `project.yaml` `resources/` `reviews/`
`runs/` `paper/`）**整个从界面上消失**，包括用户当时正在找的那篇论文。

而且切得没有声音：响应里没有任何字段说"还有 4171 个没给你"，界面照旧显示
"5000 Project files"。用户和 agent 都只能猜是不是界面折叠了 —— 会话记录里
agent 反复说"可能是界面只展示了部分目录"，它没有任何办法知道真相。

判据落在**效果**上：给一棵同样形状的树，问根这一层，`writing` 必须在。
"""
from __future__ import annotations

from pathlib import Path

from app.services.project_repository import GitProjectRepository


def _repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")


def _workspace(tmp_path: Path) -> tuple[GitProjectRepository, Path]:
    repository = _repository(tmp_path)
    repository.initialize_project(
        project_id="p1",
        name="File tree",
        description="Whether every directory survives the listing.",
        research_domain="materials science",
        owner_id="owner",
    )
    workspace = repository.ensure_session_workspace(
        project_id="p1", session_id="s1", base_commit=None,
        title="First", created_by="owner",
    )
    return repository, Path(workspace.path)


def _fill(root: Path, relative: str, count: int, *, payload: bytes = b"x\n") -> None:
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"G{index:05d}_install.log").write_bytes(payload)


def test_a_big_directory_cannot_push_a_later_one_off_the_listing(tmp_path: Path) -> None:
    """字母序排在后面的目录不会因为前面的目录太大而消失。"""
    repository, root = _workspace(tmp_path)
    # 真实形状：两个几千文件的目录排在字母序前面，writing 排在最后。
    _fill(root, ".research/orchestration/runs", 2_600)
    _fill(root, "observation/extracted/podsys_asc_log", 2_600)
    (root / "paper/sci_manuscript").mkdir(parents=True, exist_ok=True)
    (root / "paper/sci_manuscript/main.tex").write_text("\\documentclass{article}\n")

    listing = repository.file_tree("p1", session_id="s1")

    names = {str(entry["name"]) for entry in listing["entries"]}
    assert "paper" in names, (
        "paper/ 从根这一层消失了 —— 这正是 2026-09-09 用户看到的那个界面。"
        f" 实际列出：{sorted(names)}"
    )
    assert {".research", "observation"} <= names
    assert listing["truncated"] is False, "根这一层只有十几行，不该到顶"
    assert listing["totalFiles"] > 5_200, (
        "totalFiles 要回答'这个项目一共有多少文件'，而不是'我这次给了多少'"
    )


def test_a_directory_row_carries_what_is_inside_it(tmp_path: Path) -> None:
    """目录行自己带递归计数和大小 —— 不展开也知道那里面有多少东西。"""
    repository, root = _workspace(tmp_path)
    _fill(root, "observation/extracted", 40, payload=b"0123456789\n")

    [row] = [
        entry
        for entry in repository.file_tree("p1", session_id="s1")["entries"]
        if entry["name"] == "observation"
    ]
    assert row["kind"] == "directory"
    assert row["fileCount"] == 41, "40 个日志 + 建仓时那份 README"
    assert int(row["sizeBytes"]) >= 40 * 11
    assert row["owner"] == "observation"
    assert row["changedCount"] == 40, "未提交的改动数要能在不展开的情况下看见"


def test_going_one_level_down_lists_that_level(tmp_path: Path) -> None:
    repository, root = _workspace(tmp_path)
    _fill(root, "observation/extracted/podsys", 3)

    listing = repository.file_tree("p1", session_id="s1", path="observation/extracted")
    assert listing["path"] == "observation/extracted"
    assert [entry["name"] for entry in listing["entries"]] == ["podsys"]
    assert listing["entries"][0]["fileCount"] == 3


def test_hitting_the_cap_says_so(tmp_path: Path) -> None:
    """单层真的到顶时，响应里必须**说出来**：静默截断是这个 bug 的本体。"""
    repository, root = _workspace(tmp_path)
    _fill(root, "observation/extracted", 30)

    listing = repository.file_tree(
        "p1", session_id="s1", path="observation/extracted", max_entries=10,
    )
    assert len(listing["entries"]) == 10
    assert listing["truncated"] is True
    assert listing["totalEntries"] == 30, (
        "到顶时必须报出这一层真实有多少行 —— 界面不许把'我只拿到这么多'"
        "显示成'一共就这么多'"
    )


def test_the_owner_of_a_file_and_of_its_directory_come_from_one_place(
    tmp_path: Path,
) -> None:
    """建仓写 README 用的所有权判断，和树里显示的，必须是同一个函数。"""
    from app.services import project_repository as module

    assert module._owner_of("MEMORY.md") == "memory_curator"
    assert module._owner_of("paper") == "writing"
    assert module._owner_of("paper/sci_manuscript/main.tex") == "writing"
    assert module._owner_of("reviews") == "reviewer"
    assert module._owner_of("notes") == "orchestrator"
    assert module._owner_of("notes/latex_build/main.pdf") == "orchestrator"
    # `.research/orchestration/` 里只剩平台写的会话/lane 记账 —— 归平台。
    assert module._owner_of(".research/orchestration/sessions") == "platform"
    assert module._owner_of("sources/podsys_logs.zip") == "platform"
    assert module._owner_of("runs/extensions/experiment/run-7") == "experiment"
    assert module._owner_of("project.yaml") == "platform"


def test_a_nodes_output_is_never_bookkeeping_just_because_of_where_it_lives(
    tmp_path: Path,
) -> None:
    """节点产出的论文不许因为所在位置被折进「平台记账」。

    2026-09-09 现场：那篇 11 页的论文在调度器当时的作用域
    `.research/orchestration/latex_build/sci_manuscript/main.pdf` 下。界面按"路径
    里有没有点开头的段"判记账，于是调度器**产出的每一样东西**整片被折起来。
    目录名带点是它所在位置的事，不是它是什么的事。调度器 2026-09-12 起写
    `notes/`，`.research/orchestration/` 只剩平台记账 —— 那一片折起来就对了。
    """
    from app.services import project_repository as module

    # 节点工作区根摘掉之后没有点 → 研究产出。记录正文就是原生文件
    # （`paper/manuscript__podsys.tex`），它和它旁边的 PDF 一样是产出。
    assert module._is_bookkeeping("notes/latex_build/main.pdf") is False
    assert module._is_bookkeeping("paper/sci_manuscript/main.tex") is False
    assert module._is_bookkeeping("paper/manuscript__podsys.tex") is False
    assert module._is_bookkeeping("plan/pre_registration__Q1.md") is False
    assert module._is_bookkeeping("MEMORY.md") is False
    assert module._is_bookkeeping("sources/podsys_logs.zip") is False
    assert module._is_bookkeeping(".research/orchestration/sessions/s1.yaml") is True
    # 记录的事实（类型 / 版本 / 冻结）在账本里，账本是平台记账
    assert module._is_bookkeeping(".research/ledger/records.jsonl") is True
    # 摘掉根之后仍有点开头的段 → 记账（建仓时的占位文件）
    assert module._is_bookkeeping("paper/.gitkeep") is True
    # 不属于任何节点、且带点 → 记账
    assert module._is_bookkeeping(".research/contracts/artifacts/abc.json") is True
    assert module._is_bookkeeping(".gitignore") is True


def test_the_tree_carries_that_verdict_so_the_ui_does_not_guess(tmp_path: Path) -> None:
    """判决跟着行走 —— 界面不该自己拿路径形状再猜一遍。"""
    repository, root = _workspace(tmp_path)
    pdf = root / "notes/latex_build/sci_manuscript"
    pdf.mkdir(parents=True, exist_ok=True)
    (pdf / "main.pdf").write_bytes(b"%PDF-1.7\n")

    listing = repository.file_tree(
        "p1", session_id="s1", path="notes/latex_build/sci_manuscript",
    )
    [row] = listing["entries"]
    assert row["name"] == "main.pdf"
    assert row["bookkeeping"] is False, (
        "调度器编出来的论文被判成了平台记账 —— 这正是用户找不到它的那条路"
    )
    assert row["owner"] == "orchestrator"


def test_a_directory_is_bookkeeping_only_if_everything_in_it_is(tmp_path: Path) -> None:
    """容器是不是内务，看它**装着什么**，不看它叫什么。

    调度器的目录 `notes/` 里既有它存的记录（原生文件）和它编出来的论文，也有
    建仓时的 `.gitkeep`。按名字判、或按"里面有没有记账"判，都会把论文一起折进
    「平台记账」—— 2026-09-09 用户找不到论文走的正是这条路（当时它的目录还叫
    `.research/orchestration`）。反过来，`.research/` 如今只装平台自己的账 ——
    记录账本 `.research/ledger/records.jsonl` 也在那里 —— 整个折起来才对。
    """
    from app.services.harness_contract import ledger_module

    repository, root = _workspace(tmp_path)
    contracts = root / ".research" / "contracts" / "artifacts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "abc.json").write_text("{}", encoding="utf-8")
    # 调度器存一份记录：正文落 notes/，事实进 .research/ledger/records.jsonl。
    ledger_module().write_record(
        root, artifact_type="research_intent", name="podsys", content="# intent\n",
        directory="notes", produced_by_node_type="_orchestrator", produced_by_run_id="r1",
    )
    pdf = root / "notes" / "latex_build" / "summary"
    pdf.mkdir(parents=True, exist_ok=True)
    (pdf / "summary.pdf").write_bytes(b"%PDF-1.4\n")

    research = {
        entry["name"]: entry
        for entry in repository.file_tree("p1", session_id="s1", path=".research")["entries"]
    }
    assert research["ledger"]["bookkeeping"] is True, "里面只有账本 → 记账"
    assert research["contracts"]["bookkeeping"] is True

    notes = {
        entry["name"]: entry
        for entry in repository.file_tree("p1", session_id="s1", path="notes")["entries"]
    }
    assert notes["latex_build"]["bookkeeping"] is False
    assert notes["research_intent__podsys.md"]["bookkeeping"] is False, (
        "记录正文是原生文件，是研究产出 —— 它的账在 .research/ledger 里，不在它身上"
    )

    top = {
        entry["name"]: entry
        for entry in repository.file_tree("p1", session_id="s1")["entries"]
    }
    assert top["notes"]["bookkeeping"] is False, (
        "里面装着调度器存的记录和编出来的论文，这个容器就不该整个折起来"
    )
    assert top[".research"]["bookkeeping"] is True, (
        "调度器搬去 notes/ 之后，.research/ 里只剩平台自己的账（含记录账本）"
    )
