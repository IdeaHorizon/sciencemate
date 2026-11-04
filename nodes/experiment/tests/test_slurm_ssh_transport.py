"""SSH transport tests use real local children, never an SSH server."""
from __future__ import annotations

import io
import json
from pathlib import Path
import shlex
import sys
import subprocess
import time

import pytest

from nodes.experiment.tools import slurm_ssh as ssh


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    from shared.lib.shell import bash_shell

    bash = bash_shell()
    fake = tmp_path / "fake_ssh.py"
    log = tmp_path / "ssh-calls.jsonl"
    fake.write_text(r'''
import json, os, pathlib, shlex, subprocess, sys, time
bash, log, mode = sys.argv[1:4]
args = sys.argv[4:]
with open(log, 'a', encoding='utf-8') as file:
    file.write(json.dumps(args) + '\n')
if mode == 'timeout':
    time.sleep(3)
    raise SystemExit(0)
if mode == 'refused':
    sys.stderr.write('Host key verification failed.\n')
    raise SystemExit(255)
if mode == 'early-zero':
    sys.stdout.write('login ended early\n')
    raise SystemExit(0)
remote = shlex.split(args[-1])
assert remote[:2] in (['bash', '-lc'], ['bash', '-c'])
if mode == 'banner':
    sys.stdout.buffer.write(b'Welcome \xff\x00 login banner\n')
    sys.stdout.buffer.flush()
prefix = 'export PATH=/usr/bin:/bin:$PATH\n'
prefix += 'python3() { ' + shlex.quote(sys.executable.replace('\\', '/')) + ' "$@"; }; export -f python3;\n'
if mode in ('probe', 'probe-cluster-unknown'):
    prefix += "id() { printf 'remote-user\\n'; }; export -f id;\n"
    prefix += "scontrol() { printf 'ClusterName = cluster-a\\n'; }; export -f scontrol;\n"
    for tool in ('sbatch', 'squeue', 'sacct', 'scancel', 'sinfo'):
        prefix += tool + '() { :; }; export -f ' + tool + ';\n'
    if mode == 'probe-cluster-unknown':
        prefix += 'scontrol() { return 1; }; export -f scontrol;\n'
# Use an actual Bash, with controlled startup: no developer profile is sourced.
result = subprocess.call([bash, '-c', prefix + remote[2]],
                         stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
raise SystemExit(0 if mode == 'wrong-rc' else result)
''', encoding="utf-8")
    original = ssh._ssh_argv
    real_which = ssh.shutil.which
    monkeypatch.setattr(ssh.shutil, "which", lambda name: sys.executable if name == "ssh" else real_which(name))
    config = {"mode": "normal"}
    def launch(target, command):
        args = original(target, command)
        return [sys.executable, str(fake), bash, str(log), config["mode"], *args[1:]]
    monkeypatch.setattr(ssh, "_ssh_argv", launch)
    config["calls"] = lambda: [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return config


def test_shell_quoting_and_ssh_controls_do_not_execute_argument_text(fake_ssh, tmp_path):
    sentinel = tmp_path / "unexpected.txt"
    word = "space 'quote' $(touch " + shlex.quote(sentinel.as_posix()) + "); <x>"
    result = ssh.run({"host": "cluster-alias", "user": "remote-user", "port": 2222}, ["printf", "%s", word])
    assert result["ok"], result
    assert result["stdout"] == word
    assert not sentinel.exists()
    calls = fake_ssh["calls"]()
    assert len(calls) == 1
    args = calls[0]
    assert args[-3:-1] == ["--", "cluster-alias"]
    assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
    assert "ForwardAgent=no" in args and "ClearAllForwardings=yes" in args
    assert "ForwardX11=no" in args and "Tunnel=no" in args
    assert "PermitLocalCommand=no" in args and "ControlPath=none" in args
    assert args[args.index("-p") + 1] == "2222"
    assert args[args.index("-l") + 1] == "remote-user"
    assert shlex.split(args[-1])[:2] == ["bash", "-lc"]


def test_native_msys_argv_keeps_backslashes_quotes_and_globs_literal():
    from shared.lib.shell import bash_shell
    words = [r"a\\b", "a'quoted'word", "*", "{x,y}", "@response-file", "~", "line\nnext", ""]
    args = [bash_shell(), "-c", 'printf "%s\\0" "$@"', "_", *words]
    result = subprocess.run(ssh._native_argv(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"".join(word.encode() + b"\0" for word in words)


def test_login_banner_does_not_become_scheduler_output(fake_ssh):
    fake_ssh["mode"] = "banner"
    result = ssh.run({"host": "cluster"}, ["printf", "%s", "12345;cluster-a\n"])
    assert result["ok"] and result["stdout"] == "12345;cluster-a\n", result
    assert result["login_output_bytes"] > 0


def test_nonzero_remote_command_retains_real_status_and_diagnostics(fake_ssh):
    result = ssh.run({"host": "cluster", "login_shell": False}, ["bash", "-c", "printf failed >&2; exit 17"])
    assert not result["ok"] and result["returncode"] == 17
    assert result["command_completed"] is True and result["stderr"] == "failed"
    assert "transport_error" not in result
    assert shlex.split(fake_ssh["calls"]()[0][-1])[:2] == ["bash", "-c"]


@pytest.mark.parametrize("mode,code", [("early-zero", "completion_unobserved"), ("refused", "completion_unobserved"), ("wrong-rc", "completion_mismatch")])
def test_transport_failure_is_not_success_or_local_fallback(fake_ssh, mode, code):
    fake_ssh["mode"] = mode
    result = ssh.run({"host": "cluster"}, ["bash", "-c", "printf remote-only; exit 19"])
    assert not result["ok"] and result["transport_error"] == code, result
    assert result["stdout"] == "" and result["remote_effects"] == "unknown"
    assert len(fake_ssh["calls"]()) == 1


def test_timeout_has_one_attempt_and_no_completion_claim(fake_ssh):
    fake_ssh["mode"] = "timeout"
    started = time.monotonic()
    result = ssh.run({"host": "cluster"}, ["printf", "payload"], timeout=0.4)
    assert time.monotonic() - started < 2
    assert result["transport_error"] == "timeout" and not result["ok"]
    assert result["returncode"] is None and result["remote_effects"] == "unknown"
    assert len(fake_ssh["calls"]()) == 1


def test_binary_stream_retains_exact_bytes_and_strips_banner(fake_ssh, tmp_path):
    fake_ssh["mode"] = "banner"
    data = bytes(range(256)) * 16384
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    with source.open("rb") as infile, (tmp_path / "result.bin").open("wb") as outfile:
        result = ssh.run_stream({"host": "cluster"}, ["cat"], stdin_file=infile, stdout_file=outfile,
                                max_output_bytes=len(data) + 1)
    assert result["ok"] and result["stdout"] == "" and result["stdout_bytes"] == len(data), result
    assert (tmp_path / "result.bin").read_bytes() == data


def test_stream_limit_does_not_publish_partial_file(fake_ssh, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * 10000)
    destination = io.BytesIO(b"existing")
    with source.open("rb") as infile:
        result = ssh.run_stream({"host": "cluster"}, ["cat"], stdin_file=infile, stdout_file=destination,
                                max_output_bytes=100)
    assert not result["ok"] and result["transport_error"] == "output_limit", result
    assert destination.getvalue() == b"existing"


def test_text_input_is_exact_and_text_output_is_bounded(fake_ssh, monkeypatch):
    result = ssh.run({"host": "cluster"}, ["cat"], input_bytes=b"hello\r\n")
    assert result["ok"] and result["stdout"] == "hello\r\n", result
    monkeypatch.setattr(ssh, "MAX_OUTPUT_BYTES", 256)
    result = ssh.run({"host": "cluster"}, ["cat"], input_bytes=b"x" * 10000)
    assert not result["ok"] and result["transport_error"] == "output_limit", result


def test_long_command_frame_preserves_argv_and_following_binary_input(fake_ssh):
    import hashlib
    argument = "space 'quote' \\ $(literal)\n" * 4096
    binary = bytes(range(256)) * 16
    result = ssh.run({"host": "cluster"}, [
        "python3", "-c", "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()); sys.stdout.buffer.flush(); print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())",
        argument,
    ], input_bytes=binary)
    assert result["ok"], result
    assert set(result["stdout"].splitlines()) == {
        hashlib.sha256(argument.encode()).hexdigest(), hashlib.sha256(binary).hexdigest()}
    assert len(fake_ssh["calls"]()[0][-1]) < 8192


def test_stderr_limit_keeps_destination_unpublished(fake_ssh, tmp_path, monkeypatch):
    monkeypatch.setattr(ssh, "MAX_OUTPUT_BYTES", 256)
    source = tmp_path / "stderr.bin"
    source.write_bytes(b"x" * 10000)
    destination = io.BytesIO(b"existing")
    with source.open("rb") as infile:
        result = ssh.run_stream({"host": "cluster"}, ["bash", "-c", "cat >&2"],
                                stdin_file=infile, stdout_file=destination)
    assert result["transport_error"] == "output_limit" and not result["ok"]
    assert destination.getvalue() == b"existing"


def test_missing_ssh_does_not_launch_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(ssh.shutil, "which", lambda _: None)
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *a, **kw: calls.append(a))
    result = ssh.run({"host": "cluster"}, ["echo", "unexecuted"])
    assert result["transport_error"] == "ssh_unavailable" and not calls


def test_unavailable_platform_encoder_does_not_fall_back(fake_ssh, monkeypatch):
    def unavailable(_args):
        raise RuntimeError("MSYS SSH encoder unavailable")
    monkeypatch.setattr(ssh, "_native_argv", unavailable)
    result = ssh.run({"host": "cluster"}, ["printf", "payload"])
    assert not result["ok"] and result["transport_error"] == "ssh_error"
    assert not fake_ssh["calls"]()


def test_probe_identity_is_remote_observation_not_host_alias(fake_ssh):
    fake_ssh["mode"] = "probe"
    result = ssh.probe({"host": "chosen-login"})
    assert result["ok"], result
    assert result["user"] == "remote-user" and result["cluster"] == "cluster-a"
    assert result["tools"]["sbatch"] == "sbatch" and result["python3"] == "python3"
    assert result["identity_sources"]["user"] == "id -un"
    assert result["target"] == {"host": "chosen-login", "login_shell": True}


def test_probe_unknown_cluster_is_not_inferred_from_host(fake_ssh):
    fake_ssh["mode"] = "probe-cluster-unknown"
    result = ssh.probe({"host": "not-a-cluster-name"})
    assert result["ok"] and result["cluster"] is None, result


def test_expected_identity_runs_payload_in_the_same_connection(fake_ssh):
    fake_ssh["mode"] = "probe"
    result = ssh.run({"host": "cluster"}, ["printf", "%s", "payload"],
                     expected_identity={"user": "remote-user", "cluster": "cluster-a"})
    assert result["ok"] and result["stdout"] == "payload", result
    assert len(fake_ssh["calls"]()) == 1


@pytest.mark.parametrize("mode,user,cluster", [
    ("probe", "other-user", "cluster-a"), ("probe", "remote-user", "other-cluster"),
    ("probe-cluster-unknown", "remote-user", "cluster-a"),
])
def test_changed_or_unknown_identity_never_reaches_payload(fake_ssh, tmp_path, mode, user, cluster):
    fake_ssh["mode"] = mode
    sentinel = tmp_path / "not-created"
    result = ssh.run({"host": "cluster"}, ["touch", sentinel.as_posix()],
                     expected_identity={"user": user, "cluster": cluster})
    assert not result["ok"] and result["returncode"] == 125, result
    assert result["transport_error"] == "target_identity_mismatch"
    assert result["remote_effects"] == "not_sent" and result["payload_spawned"] is False
    assert not result["command_completed"] and not sentinel.exists()
    assert len(fake_ssh["calls"]()) == 1


def test_identity_refusal_does_not_publish_binary_output(fake_ssh):
    fake_ssh["mode"] = "probe"
    destination = io.BytesIO(b"existing")
    result = ssh.run_stream({"host": "cluster"}, ["printf", "payload"], stdout_file=destination,
                            expected_identity={"user": "remote-user", "cluster": "other-cluster"})
    assert result["transport_error"] == "target_identity_mismatch"
    assert destination.getvalue() == b"existing"


def test_real_exit_125_is_not_an_identity_refusal(fake_ssh):
    fake_ssh["mode"] = "probe"
    result = ssh.run({"host": "cluster"}, ["bash", "-c", "exit 125"],
                     expected_identity={"user": "remote-user", "cluster": "cluster-a"})
    assert not result["ok"] and result["returncode"] == 125 and result["command_completed"]
    assert "transport_error" not in result


@pytest.mark.parametrize("raw", [
    {"host": "-oProxyCommand=bad"}, {"host": "user@server"}, {"host": "x\nwhoami"},
    {"host": "server", "setup": "echo bad"}, {"host": "server", "ssh_args": ["-F", "unsafe"]},
    {"host": "server", "port": True}, {"host": "server", "port": 65536},
    {"host": "server", "login_shell": "yes"}, {"host": "server", "user": "-root"},
])
def test_target_rejects_secret_options_and_injection_without_spawn(raw, monkeypatch):
    monkeypatch.setattr(ssh.subprocess, "Popen", lambda *a, **kw: pytest.fail("invalid target spawned"))
    with pytest.raises(ValueError):
        ssh.run(raw, ["true"])


def test_linux_path_normalization_is_independent_of_local_drive(monkeypatch):
    monkeypatch.setenv("HOME", "C:\\Users\\different")
    assert ssh.normalize_path("inputs/../outputs/result.dat", "/scratch/user/run") == "/scratch/user/run/outputs/result.dat"
    assert ssh.normalize_path("/scratch/space dir/./x") == "/scratch/space dir/x"
    assert ssh.normalize_target({"host": "::1"})["host"] == "::1"


@pytest.mark.parametrize("path", ["C:\\work\\data", "C:/work", "//server/share", "~/run", "$HOME/run", "/x/${USER}", "/tmp/\nfile", "relative"])
def test_remote_path_does_not_guess_windows_or_unexpanded_locations(path):
    with pytest.raises(ValueError):
        ssh.normalize_path(path)
