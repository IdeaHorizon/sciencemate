"""scheduler preflight 的两条不变量 —— 都用真执行验证，不看返回值措辞。

这两条测试对应同一类病根：**框架把"测不出来"当成了"测出来是坏的"**。
一条在 identity 探测上（没有 getent ≠ 身份有问题），一条在 preview 上
（preamble 变长 ≠ payload 可以不显示）。两条都只能靠真跑一次抓到 ——
mock 掉 subprocess 的写法会把 exit 86 一起 mock 掉。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.experiment.tools.resource_manager import (  # noqa: E402
    _identity_preflight_lines, _script_preview,
)


def _script(workdir: Path | None) -> str:
    return ("#!/usr/bin/env bash\nset -eo pipefail\n"
            + "\n".join(_identity_preflight_lines(str(workdir) if workdir else None))
            + "\necho PAYLOAD_RAN\n")


def _run(script: str, *, fake_getent: str | None, tmp: Path) -> subprocess.CompletedProcess:
    """跑真脚本。fake_getent=None 模拟没有 getent 的平台（macOS / 精简容器）。"""
    bindir = tmp / f"bin-{abs(hash(fake_getent)) % 10**8}"
    bindir.mkdir(parents=True, exist_ok=True)
    if fake_getent is not None:
        binary = bindir / "getent"
        binary.write_text(fake_getent, encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    path = f"{bindir}:/usr/bin:/bin"
    script_path = bindir / "job.sh"
    script_path.write_text(script, encoding="utf-8")
    return subprocess.run(["/bin/bash", str(script_path)], capture_output=True,
                          text=True, env={**os.environ, "PATH": path})


# ── 不变量 1：没有目录服务查询工具 ≠ 身份有问题 ────────────────────────────

def test_payload_runs_on_a_host_without_getent(tmp_path):
    """病根回归：getent 不存在时整个 job 在 payload 之前 exit 86。

    这不是 macOS 专属的洁癖 —— 精简容器镜像同样没有 getent。判据是
    **payload 到底跑没跑**，不是返回值里有没有 'unavailable' 这个词。
    """
    workdir = tmp_path / "run"
    workdir.mkdir()
    result = _run(_script(workdir), fake_getent=None, tmp=tmp_path)

    assert result.returncode == 0, (
        f"payload 没跑成：rc={result.returncode} stderr={result.stderr!r}")
    assert "PAYLOAD_RAN" in result.stdout
    # 查不到不等于查出来是坏的：如实报告来源，而不是假装验过。
    assert "status=pass" in result.stdout
    assert "reason=nss_lookup" not in result.stderr


def test_directory_service_that_denies_the_user_still_fails_closed(tmp_path):
    """闸门不能因为可移植就失去牙齿：查得到机制、但机制说查不到人 → 仍 exit 86。"""
    workdir = tmp_path / "run"
    workdir.mkdir()
    result = _run(_script(workdir), fake_getent="#!/bin/sh\nexit 2\n", tmp=tmp_path)

    assert result.returncode == 86
    assert "reason=nss_lookup" in result.stderr
    assert "PAYLOAD_RAN" not in result.stdout


def test_uid_mismatch_still_fails_closed(tmp_path):
    """exit 87 是这道闸真正要抓的东西（HPC 计算节点身份错配），不可放宽。"""
    workdir = tmp_path / "run"
    workdir.mkdir()
    fake = f'#!/bin/sh\necho "me:x:99999:20:me:{Path.home()}:/bin/sh"\n'
    result = _run(_script(workdir), fake_getent=fake, tmp=tmp_path)

    assert result.returncode == 87
    assert "reason=uid_mismatch" in result.stderr
    assert "PAYLOAD_RAN" not in result.stdout


def test_unwritable_workdir_still_fails_closed_without_getent(tmp_path):
    """workdir 检查与平台无关；没有 getent 也不能顺带把它一起放过。"""
    missing = tmp_path / "does-not-exist"
    result = _run(_script(missing), fake_getent=None, tmp=tmp_path)

    assert result.returncode == 88
    assert "reason=workdir_access" in result.stderr
    assert "PAYLOAD_RAN" not in result.stdout


# ── 不变量 2：preview 里永远看得见 payload ────────────────────────────────

def test_preview_keeps_the_payload_when_preamble_overflows_the_budget():
    """dry_run 是为了核对要跑什么；preamble 再长也不能把 payload 挤出视野。

    钉的是**结构**不是**当前长度**：下一次 preamble 再涨几百字符，这条
    仍然为真。原来的 script[:2000] 只在"preamble 恰好够短"时偶然为真。
    """
    payload = "mpirun -np 64 ./solver --input case.in > run.log"
    script = "#!/usr/bin/env bash\n" + ("# generated preamble line\n" * 400) + payload + "\n"
    assert len(script) > 2000

    preview = _script_preview(script)

    assert len(preview) <= 2000
    assert payload in preview, "payload 被截掉了 —— dry_run 等于没核对"
    assert "elided" in preview, "截断必须显式，否则残缺的 preview 读起来像完整脚本"


def test_submission_preview_preserves_a_payload_longer_than_the_preview_budget():
    """The actual submit path passes command explicitly, so it is never clipped."""
    payload = "echo " + ("x" * 2500)
    script = "#!/usr/bin/env bash\n" + ("# generated preamble line\n" * 400) + payload + "\n"

    preview = _script_preview(script, payload)

    assert payload in preview
    assert "complete payload follows" in preview



def test_short_script_is_returned_whole():
    script = "#!/usr/bin/env bash\necho hi\n"
    assert _script_preview(script) == script
