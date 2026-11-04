"""执行上下文是平台的一等事实，不该让模型花 1200 万 token 去探。

## 现场（2026-10-29 lujy 诊断包，#1263）

Windows 0.5.8 个人版，用户问「你能连接本机 ubuntu 子系统吗」。experiment 节点在受管
上下文里跑了两趟共 11 条 ``wsl.exe`` 探针，两趟 reviewer 各审一遍，整段约 1200 万 token，
得出的结论是进程起来之前就确定的事：受限完整性级别够不着 WSL 服务。

这些测试守的是「契约必须送到调用方」：事实段落必须出现在开局说明里，win32 下必须
明说 wsl.exe 不可达且不要探；读不到的事实写成未知而不是静默省掉。
"""
from __future__ import annotations

from core import execution_context_facts as ecf


def _win32_snapshot() -> dict:
    return {
        "backend": "win32",
        "enforced": ["write_boundary", "git_unwritable", "walltime", "group_kill", "mem_cap"],
        "missing_for_unattended": ["net_deny"],
        "missing_for_attended": ["net_deny"],
    }


def test_win32_tells_the_model_wsl_is_unreachable_and_not_to_probe() -> None:
    facts = ecf.gather(snapshot=_win32_snapshot(), platform="win32",
                       shell=r"C:\Program Files\Git\usr\bin\bash.exe",
                       python=r"D:\ScienceMate\Resources\python\python.exe",
                       os_text="Windows-11-10.0.26200")
    text = ecf.render_section(facts)
    assert "wsl.exe" in text and "不可达" in text, "诊断包里 11 条探针要发现的事，这里一句话说清"
    assert "不要探测" in text and "不要重试" in text
    assert "UTF-16LE" in text and "CP936" in text, "原生程序输出编码是模型逐条 od 出来的，该告诉它"
    assert "已转成 UTF-8" in text, "咽喉已经替它转了编码 —— 还叫它 iconv 就是叫它再试一遍"
    assert "MSYS2_ARG_CONV_EXCL" in text, "路径改写是模型试到第三次才发现的"
    assert r"C:\Program Files\Git\usr\bin\bash.exe" in text
    assert "`win32`" in text and "`net_deny`" in text
    assert facts["path_sep"] == "\\"


def test_darwin_does_not_inherit_windows_notes() -> None:
    facts = ecf.gather(
        snapshot={"backend": "darwin",
                  "enforced": ["write_boundary", "git_unwritable", "net_deny",
                               "walltime", "group_kill"],
                  "missing_for_unattended": ["mem_cap", "pids_cap"],
                  "missing_for_attended": []},
        platform="darwin", shell="/bin/bash", python="/usr/bin/python3", os_text="macOS 15")
    text = ecf.render_section(facts)
    assert "wsl.exe" not in text and "MSYS" not in text
    assert "`darwin`" in text and "`mem_cap`" in text and "`pids_cap`" in text
    assert "人在场还缺：—" in text, "空集要显式写成「—」，不是省掉"
    assert "seatbelt" in text


def test_unavailable_backend_is_said_not_hidden() -> None:
    facts = ecf.gather(snapshot={"backend": None, "enforced": [],
                                 "missing_for_unattended": ["write_boundary"],
                                 "missing_for_attended": ["write_boundary"],
                                 "unavailable_reason": "HARNESS_EXECUTOR='nope' is not a backend"},
                       platform="linux", shell="/bin/bash", python="/usr/bin/python3",
                       os_text="Linux")
    text = ecf.render_section(facts)
    assert "起不来" in text and "is not a backend" in text


def test_gather_reads_each_fact_from_its_own_source(monkeypatch) -> None:
    """生产路径不传参：每个事实从真相源读，读不到写未知，不抛。"""
    import core.isolation as isolation
    import shared.lib.hostinfo as hostinfo
    import shared.lib.shell as shell

    monkeypatch.setattr(isolation, "enforcement_snapshot", lambda: _win32_snapshot())
    monkeypatch.setattr(hostinfo, "os_description", lambda: "OS-FROM-HOSTINFO")

    def _no_shell() -> str:
        raise FileNotFoundError("Git for Windows Bash is unavailable")

    monkeypatch.setattr(shell, "bash_shell", _no_shell)
    facts = ecf.gather()
    assert facts["os"] == "OS-FROM-HOSTINFO"
    assert facts["backend"] == "win32"
    assert "找不到受管 shell" in facts["shell"] and "unavailable" in facts["shell"]
    text = ecf.render_section(facts)
    assert "OS-FROM-HOSTINFO" in text and "找不到受管 shell" in text


def test_render_without_args_never_raises() -> None:
    text = ecf.render_section()
    assert text.startswith("## ⚙️ 这一轮的执行上下文")
    assert "- 系统：" in text and "- 模型 shell：" in text
