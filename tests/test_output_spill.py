"""工具大输出落盘。

一条不变量：**截断永远给逃生通道**。超限的内容可以不在返回值里，
但必须"没丢"且模型知道怎么拿 —— 否则等于把"输出太长"换成"模型不知道
还有别的内容"。
"""
from __future__ import annotations

from pathlib import Path

from shared.lib.output_spill import (
    DEFAULT_INLINE_LIMIT,
    attach,
    spill_if_large,
)


class _State:
    def __init__(self, tmp_path):
        self.run_dir = tmp_path
        self.workspace_root = tmp_path
        self.project_root = tmp_path


def test_short_output_is_returned_whole(tmp_path):
    out = spill_if_large("hello", state=_State(tmp_path), label="t")
    assert out["truncated"] is False
    assert out["text"] == "hello"


def test_large_output_is_spilled_and_recoverable(tmp_path):
    text = "".join(f"line {i}\n" for i in range(20_000))
    out = spill_if_large(text, state=_State(tmp_path), label="t")

    assert out["truncated"] is True and out["spilled"] is True
    assert out["full_chars"] == len(text)

    # 全文真的在盘上，且逐字节一致 —— "没丢"必须是可验证的
    assert Path(out["path"]).read_text(encoding="utf-8") == text

    # 头尾都在：报错常在开头，结论常在结尾
    assert text.startswith(out["head"])
    assert text.endswith(out["tail"])

    # 取回办法必须具体到可执行
    assert "read_file(" in out["note"] and out["path"] in out["note"]


def test_beginning_survives_which_is_the_whole_point(tmp_path):
    """旧行为只留尾部 —— 求解器的参数校验错误在开头，正是被丢掉的那段。"""
    text = "FATAL: mesh file not found\n" + "progress...\n" * 50_000
    out = spill_if_large(text, state=_State(tmp_path), label="t")
    assert "FATAL: mesh file not found" in out["head"]


def test_no_writable_dir_says_content_is_lost(tmp_path):
    """落不了盘时不能假装全文还在 —— 说明它丢了，比默默截断诚实。"""
    class Bare:
        pass

    text = "x" * 10_000
    out = spill_if_large(text, state=Bare(), label="t")
    assert out["truncated"] is True and out["spilled"] is False
    assert "丢失" in out["note"]
    assert len(out["text"]) == DEFAULT_INLINE_LIMIT


def test_attach_keeps_old_readers_working(tmp_path):
    """老读者读 stdout_tail 仍拿得到尾部，新字段是增量。"""
    text = "y" * 10_000
    r = {}
    attach(r, "stdout_tail", spill_if_large(text, state=_State(tmp_path), label="t"))

    assert r["stdout_tail"].endswith("y")          # 尾部还在原字段
    assert "stdout_tail_head" in r
    assert "stdout_tail_path" in r
    assert r["stdout_tail_full_chars"] == 10_000
    assert Path(r["stdout_tail_path"]).is_file()


def test_attach_short_output_adds_no_noise(tmp_path):
    r = {}
    attach(r, "stdout_tail", spill_if_large("ok", state=_State(tmp_path), label="t"))
    assert r == {"stdout_tail": "ok"}


def test_run_bash_description_matches_behavior():
    """文案许诺的能力，API 得给得出 —— 描述不能再说"更早的输出会被截断"。"""
    from core import tool_registry
    import shared.tools.builtin  # noqa: F401  触发注册

    t = tool_registry.get_tool("run_bash")
    if t is None:                       # 注册表按节点装配时可能取不到
        import pathlib
        desc = pathlib.Path("shared/tools/builtin.py").read_text(encoding="utf-8")
        assert "更早的输出会被截断" not in desc
        return
    assert "更早的输出会被截断" not in t.description


# ── 子进程大输出：完整文件保留（真正的修法）────────────────────────────────

def test_attach_stream_points_at_preserved_full_file(tmp_path):
    full = tmp_path / "stdout.txt"
    full.write_text("FATAL at head\n" + "x\n" * 100_000, encoding="utf-8")

    from shared.lib.output_spill import attach_stream
    r = {}
    attach_stream(r, "stdout_tail", "x\n" * 100, str(full))

    assert r["stdout_tail_path"] == str(full)
    assert r["stdout_tail_full_bytes"] == full.stat().st_size
    assert "没有丢" in r["stdout_tail_note"]
    assert "开头" in r["stdout_tail_note"]        # 明说别只看结尾


def test_attach_stream_without_preserved_file_admits_loss(tmp_path):
    """没保留成功时不能假装完整输出还在。"""
    from shared.lib.output_spill import attach_stream
    r = {}
    attach_stream(r, "stdout_tail", "y" * 10_000, None)
    assert "stdout_tail_path" not in r
    assert "未能保留" in r["stdout_tail_note"]


def test_run_bash_preserves_beginning_of_huge_output(tmp_path):
    """端到端：开头的报错必须能拿回来。

    旧行为是只读输出文件的最后 256KB 然后把文件删掉 —— 求解器的参数校验
    错误在开头，正是被丢掉的那段。
    """
    import asyncio

    class S:
        run_dir = tmp_path
        root = tmp_path
        workspace_root = tmp_path
        project_root = tmp_path
        project_worktree = None
        hook_state: dict = {}
        run_id = "r"
        node_type = "postprocess"

        def append_transcript(self, *a, **k):
            pass

    from shared.tools.builtin import _run_bash

    cmd = ("echo 'FATAL: mesh file not found'; "
           "for i in $(seq 1 20000); do echo \"progress line $i\"; done")
    r = asyncio.run(_run_bash(S(), cmd=cmd, timeout=180))

    assert r["status"] == "success"
    p = r.get("stdout_tail_path")
    assert p, "超大输出必须保留完整文件"
    full = Path(p).read_text(encoding="utf-8")
    assert "FATAL: mesh file not found" in full          # 开头没丢
    assert full.count("\n") == 20_001                     # 一行都没少
    assert r["stdout_tail"].strip().endswith("progress line 20000")
