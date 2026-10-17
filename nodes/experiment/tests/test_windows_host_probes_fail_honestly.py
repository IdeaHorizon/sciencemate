"""探针失败就是没有答案，不是一个答案（#943，Windows 真机 0.4.3）。

两处在 Windows 上「失败了却留下一个值 / 一声不吭地没写」：

* ``repro_snapshot._run`` 是 ``shell=True``、不看返回码、stdout 为空就退回 stderr。
  Windows 的 shell 是 cmd.exe，它不认 ``2>/dev/null``：打不开重定向目标、整条命令
  不跑、往 stderr 写「The system cannot find the path specified.」。于是这句话成了
  git root，一路拼进 ``experiment/repro/rerun.sh`` 的 ``cd``。
* ``resource_manager._persist_execution_environment_evidence`` 调 ``os.uname()``，
  Windows 没有；AttributeError 被调用方的 except 吞掉，本地作业的执行环境证据从来没写过。

Mac/Linux 上复现 Windows 的办法：把 repro_snapshot 看到的 ``subprocess`` 换成一个
**按 cmd.exe 的规矩回答 shell 命令**的替身（含 ``/dev/null`` 的 shell 命令 → rc 1 +
那句报错、命令不跑），不经 shell 的 argv 原样交给真 subprocess；``os.uname`` 直接删掉。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.state import State
from nodes.experiment.tools import repro_snapshot
from nodes.experiment.tools import resource_manager as rm

CMD_EXE_REPLY = "The system cannot find the path specified.\r\n"


def _speak_like_cmd_exe(monkeypatch) -> list:
    """repro_snapshot 里的 shell 命令按 cmd.exe 的规矩得到回答；argv 走真 subprocess。"""
    real_run = subprocess.run
    seen: list = []

    def run(cmd, *args, **kwargs):
        seen.append(cmd)
        if kwargs.get("shell") and "/dev/null" in str(cmd):
            return subprocess.CompletedProcess(cmd, 1, "", CMD_EXE_REPLY)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(repro_snapshot, "subprocess", SimpleNamespace(run=run))
    return seen


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return Path(os.path.realpath(path))


def _py(code: str) -> str:
    """一条 sh 与 cmd.exe 都读得懂的 shell 命令：用当前解释器跑一小段 python。"""
    return f'"{sys.executable}" -c "{code}"'


def test_a_failed_probe_yields_nothing_not_its_error_text():
    # 真 shell：stderr 有话、退出非零 —— 就是 cmd.exe 对 2>/dev/null 的那种回答。
    assert repro_snapshot._run(_py(
        "import sys; sys.stderr.write('The system cannot find the path specified.'); "
        "sys.exit(1)")) == ""
    # 退出非零时 stdout 里写了什么也不算数。
    assert repro_snapshot._run(_py(
        "import sys; sys.stdout.write('half-an-answer'); sys.exit(3)")) == ""
    # 成功的探针照旧拿到 stdout。
    assert repro_snapshot._run(_py("print('ok')")) == "ok"


def test_git_root_is_found_without_a_posix_shell_and_absent_outside_a_repo(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path / "src")
    (repo / "sub").mkdir()
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    _speak_like_cmd_exe(monkeypatch)

    assert repro_snapshot._git_root(repo / "sub") == str(repo)
    assert repro_snapshot._git_root(plain) is None


def _run_bundle(tmp_path: Path, workdir: Path) -> tuple[str, dict]:
    state = State.new("experiment", tmp_path / "runs")
    transcript = json.dumps({
        "event": "tool_call", "name": "safe_run_bash", "turn": 1,
        "args": {"cmd": f"cd {workdir} && make", "cwd": str(workdir)},
    })
    Path(state.transcript_path).write_text(transcript + "\n", encoding="utf-8")
    bundle = repro_snapshot.create_repro_bundle(
        state, snap={}, result_info={"credibility": "questionable"},
        transcript_text=transcript, force=True,
    )
    assert bundle is not None
    rerun = (Path(state.root) / "outputs" / "experiment" / "repro" / "rerun.sh")
    return rerun.read_text(encoding="utf-8"), bundle


def _cd_lines_before_the_replayed_commands(script: str) -> list[str]:
    """rerun.sh 自己写的 cd（后面照抄的模型命令里也会有 cd，不算）。"""
    header = script.split("# The complete tool command log")[0]
    return [ln for ln in header.splitlines() if ln.startswith("cd ")]


def test_rerun_script_never_cds_into_a_shell_error_message(tmp_path, monkeypatch):
    workdir = tmp_path / "experiment-runtime"
    workdir.mkdir()
    _speak_like_cmd_exe(monkeypatch)

    script, bundle = _run_bundle(tmp_path, workdir)

    assert "cannot find the path" not in script
    assert _cd_lines_before_the_replayed_commands(script) == [], script
    assert "No source repository (git root) was identified" in script
    assert bundle["source_repos"] == []
    assert subprocess.run(["bash", "-n", "-c", script]).returncode == 0


def test_rerun_script_cds_into_the_real_source_repo_without_a_posix_shell(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path / "model-src")
    _speak_like_cmd_exe(monkeypatch)

    script, bundle = _run_bundle(tmp_path, repo)

    assert _cd_lines_before_the_replayed_commands(script) == [
        f"cd {repro_snapshot._q(str(repo))}"], script
    assert [r["path"] for r in bundle["source_repos"]] == [str(repo)]


def test_local_job_environment_evidence_is_written_on_a_host_without_os_uname(
    tmp_path, monkeypatch,
):
    state = State.new("experiment", tmp_path / "runs")
    monkeypatch.delattr(os, "uname", raising=False)
    monkeypatch.setattr(
        rm, "_execution_probe",
        lambda argv, timeout=12: {"ok": False, "returncode": None,
                                  "stdout": "", "stderr": "not found"},
    )
    monkeypatch.setattr(
        rm, "_scheduler_environment_snapshot",
        lambda *_a, **_k: {"backend_identity": {"kind": "native_managed_job"}},
    )

    rm._persist_execution_environment_evidence(
        state,
        {"scheduler": "local", "job_id": "hf-job-943", "job_name": "experiment_job",
         "command": "python ../mc_pi.py", "script_path": ""},
        "submitted",
    )

    artifacts = state.list_artifacts("execution_environment_evidence")
    assert len(artifacts) == 1
    payload = json.loads(state.read_artifact(artifacts[0]["id"])["content"])
    assert payload["host"]["hostname"]
    assert "kernel" in payload["host"]


if os.name == "nt":  # pragma: no cover - 只在 Windows 真机上说话
    def test_real_cmd_exe_reply_is_not_a_git_root(tmp_path):
        """真 cmd.exe：旧写法那条带 2>/dev/null 的 shell 命令拿不到东西，argv 的拿得到。"""
        repo = _git_repo(tmp_path / "src")
        assert repro_snapshot._run(
            f"git -C {repo} rev-parse --show-toplevel 2>/dev/null") == ""
        assert repro_snapshot._git_root(tmp_path / "plain-does-not-exist") is None
        assert Path(repro_snapshot._git_root(repo)) == repo
