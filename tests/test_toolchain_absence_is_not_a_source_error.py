"""缺工具链 ≠ 稿子有问题。四类失败，四个不同的"下一步该谁做"。

## 现场（2026-09-09）

writing 要出两份 PDF，连撞三轮：

    bwrap: execvp latexmk: No such file or directory

界面三次都是同一句：**"LaTeX 编译失败 / Review the document source."**
于是模型三轮都在查一份**完全正确**的稿子，换 engine、改 main.tex、翻 build 目录，
最后自己推断出"这是环境问题"——它推对了，但那是它自己想出来的，不是我们告诉它的。

病根在 `_run_process`：它把 spawn 的 status 压成一个退出码（spawn_failed → **127**），
于是下游只剩"退出码非零"一种事实。三件毫不相干的事从此在界面上是同一句话：

  · 稿子里有 TeX 错误   → 模型改源码就能过
  · 这台机器没装 latexmk → 模型改多少次都没用
  · 人按了停止           → 谁都没错

判据必须由**知道真相的那一层**给（同 core/tool_errors.py 的总纲）。这套测试钉的
就是那一层：status 原样上来，`_launch_failure` 在失败当时现算归属。

## 现场二（2026-09-15，node20）：同一病、下一层

PATH 缺 `~/.local/bin`，Landlock 启动器自己起来了、`os.execvp latexmk` 才 ENOENT。
启动器以 traceback + 非零码退出 —— 咽喉记成 `done / rc≠0`，`_run_process` 再怎么原样
交出去，`_compile_latex` 看到的还是"编译器跑完返回非零"→「LaTeX 编译失败 / 按
stderr_tail 改源码」，stderr_tail 是一段 `_landlock_exec.py line 163 FileNotFoundError`。

上面那批 stub 造的形状（`spawn_failed` + `bwrap: execvp …`）**在真机上从来不会出现**：
bwrap / sandbox-exec / Landlock 启动器自己 execvp 载荷失败，一律是普通非零码退出，
不是 spawn_failed。09-09 那三轮其实也是 `done / rc=1`。所以 §6 真起沙箱、不 stub
任何一层：从裸 `latexmk` 交给真后端，到工具返回的 error_code，整条链一次走完。
"载荷没起来"这句话现在由墙的最内层说（`core.isolation.LAUNCHER_ERROR_MARKER`），
咽喉据此报 spawn_failed —— 契约测试在 test_launcher_says_the_payload_never_started.py。
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from core import tool_errors as _errs
from core.state import State
from shared.tools.library import latex


def _stub_spawn(monkeypatch, status: str, returncode, stderr: bytes = b""):
    async def fake(state, command, cwd, timeout, **_):
        return status, returncode, b"", stderr

    monkeypatch.setattr(latex, "_run_process", fake)


def _no_compiler_anywhere(monkeypatch):
    monkeypatch.setattr(latex.shutil, "which", lambda name: None)


async def _compile(tmp_path: Path) -> dict:
    state = State.new(node_type="writing", base_dir=tmp_path)
    return await latex._compile_latex(
        state=state, tex_source="\\documentclass{article}\\begin{document}x\\end{document}",
        output_name="paper",
    )


# ── 0. 洗白发生的那一行 ─────────────────────────────────────────────────────
#
# 下面每一条都 stub 掉 `_run_process`——那正好把**出问题的那一行**替身掉了
# （替身遮住被测实现）：把 `spawn_failed → 127` 加回去，下面 7 条全绿。
# 所以这一条必须打在真正的接缝上：`spawn_and_wait` 的返回值。

@pytest.mark.asyncio
async def test_the_spawn_status_is_not_laundered_into_an_exit_code(monkeypatch, tmp_path):
    """`_run_process` 必须把 spawn 的 status 原样交出来。

    压成退出码就等于对下游说谎："编译器跑过了，它返回 127" —— 而实际上编译器
    一次都没起来。下游拿不到真相就只能把它当编译失败处理，这就是整条 bug 的第一环。
    """
    from shared.lib import cancellable_subprocess

    async def fake_spawn(*args, **kwargs):
        return "spawn_failed", None, b"", b"execvp latexmk: No such file or directory"

    monkeypatch.setattr(cancellable_subprocess, "spawn_and_wait", fake_spawn)
    status, returncode, _out, err = await latex._run_process(
        None, ["latexmk", "x.tex"], tmp_path, 10.0)

    assert status == "spawn_failed", "spawn 的 status 被压平了 —— 下游再也分不出'没起来'"
    assert returncode != 127, "127 是编造的退出码：编译器根本没跑过，别假装它跑了"
    assert b"latexmk" in err, "stderr 里的真实原因必须原样带上"


@pytest.mark.asyncio
async def test_a_missing_binary_survives_the_whole_way_to_the_verdict(monkeypatch, tmp_path):
    """端到端走一遍真 `_run_process`：从 spawn 的 status 一直到工具返回的 error_code。

    上一条钉接缝，这一条钉**整条链**——中间任何一层再把它压平，这里就红。
    """
    from shared.lib import cancellable_subprocess

    async def fake_spawn(*args, **kwargs):
        return "spawn_failed", None, b"", b"bwrap: execvp latexmk: No such file or directory"

    monkeypatch.setattr(cancellable_subprocess, "spawn_and_wait", fake_spawn)
    _no_compiler_anywhere(monkeypatch)
    result = await _compile(tmp_path)

    assert result["error_code"] == _errs.TOOLCHAIN_MISSING
    assert result["error"] != "LaTeX 编译失败"


# ── 1. 工具链不在场：归环境，不归稿件 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_toolchain_is_attributed_to_the_machine(tmp_path, monkeypatch):
    _no_compiler_anywhere(monkeypatch)
    _stub_spawn(monkeypatch, "spawn_failed", None,
                b"bwrap: execvp latexmk: No such file or directory")
    result = await _compile(tmp_path)

    assert result["status"] == "error"
    # 章盖在这里，下游才分得开。压回 127 走"编译失败"分支的话，这一条就红。
    assert result["error_code"] == _errs.TOOLCHAIN_MISSING, \
        "缺工具链被记成了别的类别——下游又只能按'编译失败'处理"
    assert result["missing_compilers"] == ["latexmk", "tectonic"]
    # 正文必须**主动否掉**那条错误的下一步，否则模型还是会去查稿子（实测三轮）。
    assert "与稿件内容无关" in result["error"]
    assert "LaTeX 编译失败" != result["error"]


@pytest.mark.asyncio
async def test_missing_toolchain_tells_the_human_what_to_install(tmp_path, monkeypatch):
    """`recovery` 是给**能解决它的那个人**的。这里必须点名要装什么。"""
    _no_compiler_anywhere(monkeypatch)
    _stub_spawn(monkeypatch, "spawn_failed", None, b"execvp latexmk: not found")
    result = await _compile(tmp_path)

    recovery = result["recovery"]
    assert "texlive" in recovery or "tectonic" in recovery
    # 显示层对超过 400 字的 recovery 会退回兜底文案（safeRecovery）——写长了等于没写。
    assert len(recovery) <= 400


@pytest.mark.asyncio
async def test_missing_toolchain_is_not_a_loop_level_step(tmp_path, monkeypatch):
    """不是 ReAct 的正常一步：模型重试多少次都过不去，必须摆到人面前。"""
    _no_compiler_anywhere(monkeypatch)
    _stub_spawn(monkeypatch, "spawn_failed", None, b"not found")
    result = await _compile(tmp_path)
    assert result["error_code"] not in _errs.LOOP_LEVEL_CODES


# ── 2. 有编译器却起不来：归平台，不归稿件，也不谎称缺工具链 ──────────────────

@pytest.mark.asyncio
async def test_sandbox_launch_failure_is_not_reported_as_a_missing_toolchain(
        tmp_path, monkeypatch):
    """latexmk 明明在 PATH 上 —— 那就不是"这台机器没装 LaTeX"，别乱扣帽子。"""
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/usr/bin/latexmk" if n == "latexmk" else None)
    _stub_spawn(monkeypatch, "spawn_failed", None,
                b"bwrap: cannot create namespaces here")
    result = await _compile(tmp_path)

    assert result["error_code"] == _errs.COMMAND_FAILED
    assert result["available_compilers"] == ["latexmk"]
    assert "不是稿件问题" in result["error"]


# ── 3. 编译器真跑了、真拒绝了源码：这才该让模型改稿 ──────────────────────────

@pytest.mark.asyncio
async def test_a_real_tex_error_still_points_the_model_at_the_source(
        tmp_path, monkeypatch):
    """修完不能把这一类也带走：编译器起来了、跑完了、拒绝了源码 —— 下一步在模型手里。"""
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/usr/bin/latexmk" if n == "latexmk" else None)
    _stub_spawn(monkeypatch, "done", 1, b"! Undefined control sequence.")
    result = await _compile(tmp_path)

    assert result["error"] == "LaTeX 编译失败"
    assert "改源码" in result["recovery"]
    assert result.get("error_code") is None, \
        "这一类不该自带 code —— 由 _wrap_result 记成 rejected（ReAct 的正常一步）"


# ── 4. 人按了停止：不是失败 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_compile_is_not_called_a_compile_failure(tmp_path, monkeypatch):
    """压平 status 的代价之一：取消会长得像"编译失败"，模型回头去改没毛病的稿子。"""
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/usr/bin/latexmk" if n == "latexmk" else None)
    _stub_spawn(monkeypatch, "cancelled", -9, b"")
    result = await _compile(tmp_path)
    assert "取消" in result["error"] and "LaTeX 编译失败" not in result["error"]


# ── 5. 结构：不许再有恒为 None 的槽位 ───────────────────────────────────────

def test_compile_run_has_no_vestigial_slot():
    """`sequence_error` 恒为 None、`if sequence_error:` 永不执行 —— 空槽比没有更糟：
    读代码的人以为"起不来"已经有人处理了。具名字段藏不住空槽。"""
    import ast

    tree = ast.parse(Path(latex.__file__).read_text(encoding="utf-8"))
    # 查**代码**，不查源文件字符串：注释里提到这个名字（本次修复就写了）不算复活。
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "sequence_error" not in names, "死槽位又回来了"
    assert "status" in latex.CompileRun._fields, "spawn 的 status 必须原样带上来"


# ── 6. 真起沙箱：从裸 latexmk 到 error_code，一层都不 stub ──────────────────────
#
# §0-§2 stub 的边界包不住这次要修的那几行（咽喉的归属、启动器的那一句话）——
# 把它们改坏，§0-§2 照样全绿。所以这两条真走后端：PATH 里没有 latexmk / tectonic，
# 裸名交给真沙箱链，launcher exec 失败，一路到工具返回值。

def _native_wall_or_skip(monkeypatch) -> None:
    from core import isolation

    name = isolation.native_backend_name()
    if name is None:
        pytest.skip(f"no native backend for {sys.platform}")
    monkeypatch.setenv(isolation.EXECUTOR_ENV, name)
    isolation._reset_for_tests()
    backend = isolation.select_backend(name)
    if isolation.Invariant.WRITE_BOUNDARY not in backend.capabilities():
        isolation._reset_for_tests()
        pytest.skip(f"native backend {name} cannot enforce the write boundary here: "
                    f"{getattr(backend, 'unavailable_reason', '')}")


@pytest.mark.asyncio
async def test_real_sandbox_missing_compiler_is_toolchain_missing_not_a_source_error(
        tmp_path, monkeypatch):
    """node20 09-15 的原样复现：PATH 里没有 latexmk，裸名交给真沙箱。"""
    _native_wall_or_skip(monkeypatch)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("latexmk") is None and shutil.which("tectonic") is None

    result = await _compile(tmp_path / "run")

    assert result["error_code"] == _errs.TOOLCHAIN_MISSING, result
    assert result["missing_compilers"] == ["latexmk", "tectonic"]
    assert "与稿件内容无关" in result["error"]
    assert result["error"] != "LaTeX 编译失败"
    assert "改源码" not in result["recovery"], "又把模型叫去改稿子了"
    # 现场那段 `_landlock_exec.py line 163 FileNotFoundError` 不该再出现在给模型看的尾巴里。
    assert "Traceback" not in result["stderr_tail"], result["stderr_tail"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="shebang 是 POSIX 的事")
async def test_real_sandbox_compiler_on_path_but_unstartable_is_a_platform_fault(
        tmp_path, monkeypatch):
    """latexmk 在 PATH 上（which 找得到）却起不来（shebang 指的解释器不在）：
    这不是"这台机器没装 LaTeX"，也不是稿子的错 —— 归平台。"""
    _native_wall_or_skip(monkeypatch)
    bad_bin = tmp_path / "bad-bin"
    bad_bin.mkdir()
    fake = bad_bin / "latexmk"
    fake.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bad_bin))
    assert shutil.which("latexmk") == str(fake) and shutil.which("tectonic") is None

    result = await _compile(tmp_path / "run")

    assert result["error_code"] == _errs.COMMAND_FAILED, result
    assert result["available_compilers"] == ["latexmk"]
    assert "不是稿件问题" in result["error"]
    assert result["error"] != "LaTeX 编译失败"

