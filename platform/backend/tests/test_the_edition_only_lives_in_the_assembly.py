"""发行（personal / pro）只在装配处生效 —— 档位那条规矩（R2）的孪生。

两种发行只差两个烧进包里的值（EXEC_PLAN_TWO_EDITIONS §1）。要是业务代码到处
`if edition == "pro"`，每加一句就多一处分叉，而没有任何一层能发现它分叉了 ——
和档位一模一样的病。所以：`app/edition.py` 只有 `app/assembly.py` 能 import；
业务代码看能力集合里有没有 `connections`，不看发行名字。

第二件要守的是**读不到就是 personal**：源码 checkout 与 0.5.0 之前装出去的个人版
包都没有 `edition.json`。缺省往"少画一个入口"的方向倒是安全的；反过来会在一份
个人版上画出「连接组织服务器」，而它的更新源还指错。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import assembly
from app import edition as edition_mod
from app.config import settings
from tests.test_local_runtime_api import runtime_client  # noqa: F401

APP = Path(__file__).resolve().parents[1] / "app"

#: 例外：加一条要写清楚为什么这一处必须自己知道发行，以及为什么装配层做不了。
ALLOWED = {"assembly.py", "edition.py"}


def _imports_the_edition(source: str) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.edition"):
            hits.append(node.lineno)
        elif isinstance(node, ast.Import) and any(a.name.startswith("app.edition") for a in node.names):
            hits.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in {"read_edition", "edition_file"}:
            hits.append(node.lineno)
    return hits


def test_only_the_assembly_reads_the_edition() -> None:
    offenders: dict[str, list[int]] = {}
    for path in sorted(APP.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        lines = _imports_the_edition(path.read_text(encoding="utf-8"))
        if lines:
            offenders[str(path.relative_to(APP))] = lines
    assert not offenders, (
        f"这些地方自己读了发行：{offenders}。业务代码只看能力集合（有没有 connections），"
        "不看发行名字。要按发行分叉，请在 app/assembly.py 里改能力集合。"
    )


# ── 读文件的语义 ──────────────────────────────────────────────────────────


def test_no_file_means_personal(tmp_path: Path) -> None:
    assert edition_mod.read_edition(tmp_path / "edition.json") == edition_mod.Edition("personal")


def test_a_broken_file_is_personal_and_says_why(tmp_path: Path) -> None:
    target = tmp_path / "edition.json"
    target.write_text("{not json", encoding="utf-8")
    got = edition_mod.read_edition(target)
    assert got.name == "personal"
    assert got.problem, "读不懂却没说原因 —— 一份专业版会悄悄变回个人版而指不到病因"


def test_an_unknown_edition_is_personal(tmp_path: Path) -> None:
    target = tmp_path / "edition.json"
    target.write_text('{"edition": "enterprise", "update_source": "https://x"}', encoding="utf-8")
    got = edition_mod.read_edition(target)
    assert got == edition_mod.Edition("personal", problem=got.problem)
    assert got.update_source == "", "不认识的发行不该把它的更新源带进来"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    edition_mod.write_edition(tmp_path, "pro", "https://h/o/r-pro ")
    assert edition_mod.read_edition(tmp_path / "edition.json") == edition_mod.Edition("pro", "https://h/o/r-pro")


def test_the_pro_edition_must_name_its_update_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="更新源"):
        edition_mod.write_edition(tmp_path, "pro", "")


def test_the_file_sits_next_to_the_interpreter() -> None:
    """锚同 `launcher._bundled_binary`：`sys.prefix` 的上一层。打包器写哪、这里读哪。"""
    import sys

    assert edition_mod.edition_file() == Path(sys.prefix).parent / "edition.json"


# ── 装配：谁能拿到 connections ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_pro_desktop_offers_the_connection_entry(
    runtime_client, monkeypatch, tmp_path: Path,
) -> None:
    client, _factory = runtime_client
    edition_mod.write_edition(tmp_path, "pro", "https://h/o/r-pro")
    monkeypatch.setattr(edition_mod, "edition_file", lambda: tmp_path / "edition.json")

    monkeypatch.setattr(settings, "profile", "personal")
    payload = (await client.get("/api/v1/capabilities")).json()
    assert payload["edition"] == "pro"
    assert "connections" in payload["features"], "专业版桌面没画出「连接组织服务器」"

    # 组织服务器自己不画：它是被连接的那一端。
    monkeypatch.setattr(settings, "profile", "org")
    payload = (await client.get("/api/v1/capabilities")).json()
    assert "connections" not in payload["features"]

    # 个人版（没有 edition.json）一个组织概念都不画。
    monkeypatch.setattr(edition_mod, "edition_file", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(settings, "profile", "personal")
    payload = (await client.get("/api/v1/capabilities")).json()
    assert payload["edition"] == "personal"
    assert "connections" not in payload["features"]


def test_the_update_source_prefers_configuration_then_the_bundle(monkeypatch, tmp_path: Path) -> None:
    edition_mod.write_edition(tmp_path, "pro", "https://bundle")
    monkeypatch.setattr(edition_mod, "edition_file", lambda: tmp_path / "edition.json")

    monkeypatch.setattr(settings, "update_source", "")
    assert assembly.update_source() == "https://bundle"
    monkeypatch.setattr(settings, "update_source", "https://explicit")
    assert assembly.update_source() == "https://explicit"

    # 个人版：什么都没烧、什么都没配 → 空串，self_update 落到出厂值（公开仓库）。
    monkeypatch.setattr(edition_mod, "edition_file", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(settings, "update_source", "")
    assert assembly.update_source() == ""
