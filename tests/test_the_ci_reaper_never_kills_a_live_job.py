"""runner 主机上收幽灵容器的脚本，不许收掉还活着的 job。

它从前不在仓库里，阈值手写 10 分钟，而 job 的 `timeout-minutes` 是 15–30 分钟：
2026-08-20 到 10-31 它 `docker rm -f` 了 25 个正在跑的 job，每一个在 CI 上都是一次
「偶发红、复现不出来」。现在它在 `scripts/ci-reap-orphan-containers.sh`，阈值从
workflow 现读。这里用**另一种读法**（YAML 解析，不是脚本里那几行 awk/grep）核对：
算出来的阈值必须比每个 job 的超时都大；有 job 没声明超时就必须拒绝干活。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-reap-orphan-containers.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="要 bash 和 git")


def _threshold(repo: Path, ref: str = "HEAD") -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), "--print-threshold", str(repo)],
                          capture_output=True, text=True, env={"CI_REAP_REF": ref, "PATH": _path()})


def _path() -> str:
    import os
    return os.environ.get("PATH", "")


def _job_timeouts(repo: Path, ref: str) -> dict[str, int | None]:
    names = subprocess.run(["git", "ls-tree", "--name-only", ref, ".gitea/workflows/"],
                           cwd=repo, capture_output=True, text=True, check=True).stdout.split()
    out: dict[str, int | None] = {}
    for name in names:
        if not name.endswith((".yml", ".yaml")):
            continue
        body = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout
        for job, spec in (yaml.safe_load(body).get("jobs") or {}).items():
            out[f"{name}:{job}"] = spec.get("timeout-minutes")
    return out


def _repo_with(tmp_path: Path, workflows: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    (repo / ".gitea" / "workflows").mkdir(parents=True)
    for name, body in workflows.items():
        (repo / ".gitea" / "workflows" / name).write_text(body, encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "wf"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return repo


def test_the_threshold_outlives_every_job_in_this_repository() -> None:
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True).returncode:
        pytest.skip("不是 git checkout")
    timeouts = _job_timeouts(ROOT, "HEAD")
    assert timeouts, "没读到任何 CI job"
    missing = [job for job, minutes in timeouts.items() if not minutes]
    assert not missing, f"这些 job 没声明 timeout-minutes，收容器的脚本就不知道活 job 能活多久：{missing}"

    result = _threshold(ROOT)
    assert result.returncode == 0, result.stderr
    threshold = int(result.stdout.strip())
    assert threshold > max(timeouts.values()), (threshold, timeouts)


WORKFLOW = """\
on: push
jobs:
  quick:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - run: echo hi
  slow:
    runs-on: ubuntu-latest
    timeout-minutes: {slow}
    steps:
      - run: echo hi
"""


def test_raising_a_job_timeout_raises_the_threshold(tmp_path) -> None:
    """判据跟着 workflow 走：有人把超时调到 55，阈值就得跟到 55 以上。"""
    repo = _repo_with(tmp_path, {"a.yaml": WORKFLOW.format(slow=55)})
    result = _threshold(repo)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) > 55


def test_a_job_without_a_timeout_stops_the_reaper(tmp_path) -> None:
    """不知道活 job 能活多久（runner 默认几个小时），就不能按猜的阈值去杀。"""
    body = WORKFLOW.format(slow=20).replace("    timeout-minutes: 15\n", "")
    repo = _repo_with(tmp_path, {"a.yaml": body})
    result = _threshold(repo)
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "timeout-minutes" in result.stderr
