"""read_external_artifact 只读本项目（issue #720）。

lujy 在 E-12 诉求单里代码级证实：`shared/tools/run_node.py` 把模型给的
run_id / artifact_id 直接拼进路径，sibling 未命中就落 `core.paths.find_run_dir`
——而那个函数按设计扫 `runs_anon → projects/<any>/runs → 旧 flat → STATE_DIR`。
拿到任意 run_id 即可读任意项目的产物；含 `..` / 绝对路径成分的参数还能穿越。
持有面：experiment(:430) / _reviewer(:415) / _curator(:349)。

修法是**删掉那条回退**：子 run 由 run_node 以 `base_dir = state.root.parent`
创建，永远是同级目录，所以那条回退在本工具上没有合法用途——它只提供了洞。
删完之后穿越与越权是同一个不变量：读到的文件必须在我自己的 runs 目录里。

判据落在**效果**上（把目标文件真摆在攻击路径的终点，断言内容拿不到），
不落在"返回了 error"上——那和"文件恰好不存在"分不清。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.artifact_provenance import produced
from core.ledger import RecordStore
from core.state import State
from shared.tools.run_node import _read_external_artifact


def _state(base: Path | None = None) -> State:
    return State.new(node_type="experiment",
                     base_dir=base or Path(tempfile.mkdtemp()),
                     project_id="p720")


def _artifact(root: Path, name: str, content: str) -> Path:
    """在 run 目录 `root` 的 run 本地账本上落一份记录，返回正文文件路径。

    记录 = 原生文件（`<run>/artifacts/<id>.<ext>`）+ `<run>/records.jsonl` 一行；
    读方按账本找文件，所以"目标真摆在攻击路径终点"必须连账本一起摆。
    """
    store = RecordStore(root / "artifacts", root / "records.jsonl")
    store.save(
        artifact_id=name, artifact_type="x", name=name, content=content,
        metadata={}, directory=root / "artifacts",
        created_at="2026-09-12T00:00:00+00:00",
        provenance=produced("experiment", root.name),
        produced_by_node_type="experiment", produced_by_run_id=root.name,
        by_node="experiment", by_run=root.name,
    )
    head = store.head(name)
    assert head is not None
    return store.abs_path(head)


async def _read(st, run_id, artifact_id):
    return await _read_external_artifact(st, run_id=run_id, artifact_id=artifact_id)


@pytest.mark.asyncio
async def test_sibling_run_in_this_project_still_reads(tmp_path):
    """回归：现网 id 字符集（epoch 前缀 run_id、双下划线 artifact_id）零误伤。"""
    st = _state(tmp_path / "runs")
    _artifact(st.root.parent / "1786507374-6faf0d",
              "preprocessing_blocked_report__execution_failed_gen_f1", "ok")
    out = await _read(st, "1786507374-6faf0d",
                      "preprocessing_blocked_report__execution_failed_gen_f1")
    assert out["status"] == "success" and out["artifact"]["content"] == "ok"


@pytest.mark.asyncio
async def test_cross_project_run_is_unreachable_even_though_it_exists(tmp_path, monkeypatch):
    """别的项目：文件真的在、find_run_dir 真的找得到，仍然读不到。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    victim = tmp_path / "home" / "projects" / "victim" / "runs" / "1786000000-aaaaaa"
    _artifact(victim, "secret_report__x", "OTHERPROJECT")

    from core.paths import find_run_dir
    assert find_run_dir("1786000000-aaaaaa") == victim, (
        "前提没成立：find_run_dir 找不到它，那这条测试就是空过的"
    )

    st = _state(tmp_path / "mine" / "runs")
    out = await _read(st, "1786000000-aaaaaa", "secret_report__x")
    assert out["status"] == "error"
    assert "OTHERPROJECT" not in json.dumps(out, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [
    "../other-project/runs/1786000000-aaaaaa",   # lujy 点名的注入样本
    "../../../../etc", "/etc", "a/b", "..", "",
])
async def test_run_id_cannot_leave_my_runs_dir(tmp_path, run_id):
    st = _state(tmp_path / "runs")
    # 把目标真摆在穿越路径的终点
    outside = tmp_path / "runs" / ".." / "other-project" / "runs" / "1786000000-aaaaaa"
    _artifact(outside.resolve(), "secret_report__x", "TRAVERSED")
    out = await _read(st, run_id, "secret_report__x")
    assert out["status"] == "error"
    assert "TRAVERSED" not in json.dumps(out, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_id", ["../../secrets", "../summary", "/etc/passwd"])
async def test_artifact_id_cannot_leave_the_artifacts_dir(tmp_path, artifact_id):
    st = _state(tmp_path / "runs")
    sibling = st.root.parent / "1786507374-6faf0d"
    _artifact(sibling, "real", "ok")
    (st.root.parent / "secrets.json").write_text(
        json.dumps({"type": "x", "content": "SIBLINGLEAK"}), encoding="utf-8")
    (sibling / "summary.json").write_text(
        json.dumps({"type": "x", "content": "SUMMARYLEAK"}), encoding="utf-8")

    out = await _read(st, "1786507374-6faf0d", artifact_id)
    assert out["status"] == "error"
    body = json.dumps(out, ensure_ascii=False)
    assert "SIBLINGLEAK" not in body and "SUMMARYLEAK" not in body


@pytest.mark.asyncio
async def test_symlinks_cannot_escape(tmp_path):
    """消毒挡参数，containment 还要挡**目录树**：合法名字被 symlink 指到别处。"""
    st = _state(tmp_path / "runs")
    outside = tmp_path / "outside"
    _artifact(outside, "leak", "LEAKED")

    # ① run 目录整个是个 symlink
    (st.root.parent / "1786000000-linked").symlink_to(outside)
    out = await _read(st, "1786000000-linked", "leak")
    assert out["status"] == "error" and "LEAKED" not in json.dumps(out, ensure_ascii=False)

    # ② run 目录正常，账本上合法登记的 artifact 文件被换成指向外面的 symlink
    sibling = st.root.parent / "1786507374-6faf0d"
    _artifact(sibling, "real", "ok")
    leak_file = _artifact(sibling, "leak", "ok")
    leak_file.unlink()
    leak_file.symlink_to(outside / "artifacts" / "leak.md")
    out = await _read(st, "1786507374-6faf0d", "leak")
    assert out["status"] == "error" and "LEAKED" not in json.dumps(out, ensure_ascii=False)


def test_the_cross_layout_fallback_is_gone():
    """结构判据：这个工具不许再够得着 find_run_dir。

    时序/边界类缺陷优先找结构判据 —— 只测行为的话，谁把回退加回来、再配一个
    "看起来挡住了"的 if，测试可能照样绿。
    """
    import ast
    src = Path(__file__).resolve().parents[1] / "shared" / "tools" / "run_node.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_read_external_artifact")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
              for a in n.names}
    assert "find_run_dir" not in names, (
        "跨布局回退被加回来了 —— 它会扫 projects/<any>/runs，"
        "等于把跨项目读重新打开（issue #720）"
    )
