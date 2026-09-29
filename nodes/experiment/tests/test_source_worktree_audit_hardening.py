"""来源 worktree 审计查询的加固回归。

``_record_source_worktree_diffs`` 在每条改动型命令之后对 ``source_worktree_root``
跑几条只读 git 查询。那棵树连同它的 ``.git/config`` 都是外部输入（同事给的源码包、
共享目录），而 Git 会照配置执行外部 diff、textconv、clean 过滤器和 fsmonitor ——
全是任意命令。2026-09-07 本机实测：加固前这四条都能打通，并且子进程继承了 harness
的完整环境，凭据随之泄露。

本文件把每条向量钉成回归：既证明攻击不再执行，也证明正常仓库的审计输出没被削弱
（这条同样重要 —— 审计记录消失正是这段代码存在要防的事）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.experiment.tools.safe_bash import (  # noqa: E402
    _git_audit_env,
    _git_config_neutralizers,
    _git_text,
)

_STATUS = ("status", "--porcelain=v1", "--untracked-files=normal", "--", ".")
_DIFF_TRACKED = ("diff", "--binary", "--no-ext-diff", "--no-textconv", "--", ".")
_DIFF_NEW = ("diff", "--binary", "--no-index", "--no-ext-diff", "--no-textconv",
             "--", "/dev/null", "new.c")


def _repo(tmp_path: Path, name: str = "src") -> Path:
    """一个有一次提交、工作区有改动、并新增了一个未跟踪文件的仓库。"""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    (root / "f.c").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.c"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=root, check=True)
    (root / "f.c").write_text("b\n", encoding="utf-8")
    (root / "new.c").write_text("z\n", encoding="utf-8")
    return root


def _arm(root: Path, config: dict[str, str], attributes: str | None,
         marker: Path) -> None:
    payload = f"sh -c 'echo HIT > {marker}; echo K=$ANTHROPIC_API_KEY >> {marker}'"
    for key, value in config.items():
        subprocess.run(["git", "config", key, value.format(evil=payload)],
                       cwd=root, check=True)
    if attributes:
        (root / ".gitattributes").write_text(attributes + "\n", encoding="utf-8")


# 每条都是加固前实测能打通的向量；args 用调用点的原样参数。
_VECTORS = [
    pytest.param({"diff.external": "{evil}"}, None, _DIFF_NEW, id="diff.external"),
    pytest.param({"diff.evil.textconv": "{evil}"}, "f.c diff=evil", _DIFF_NEW,
                 id="diff.textconv"),
    pytest.param({"filter.evil.clean": "{evil}"}, "f.c filter=evil", _STATUS,
                 id="filter.clean"),
    pytest.param({"filter.evil.clean": "{evil}", "filter.evil.required": "true"},
                 "f.c filter=evil", _STATUS, id="filter.clean.required"),
    pytest.param({"core.fsmonitor": "{evil}"}, None, _STATUS, id="core.fsmonitor"),
    pytest.param({"diff.evil.command": "{evil}"}, "f.c diff=evil", _DIFF_TRACKED,
                 id="diff.command"),
]


@pytest.mark.parametrize("config,attributes,args", _VECTORS)
def test_worktree_config_cannot_execute_commands(tmp_path, monkeypatch,
                                                 config, attributes, args):
    """来源仓库配置里的任意命令一律不得执行，且审计输出不得因此变空。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-topsecret")
    marker = tmp_path / "pwned.txt"
    root = _repo(tmp_path)
    _arm(root, config, attributes, marker)

    out = _git_text(str(root), *args)

    assert not marker.exists(), (
        f"worktree config executed a command: {marker.read_text()}")
    # required=true 的过滤器在只置空 clean 时会让 git fatal 退出、审计记录变空；
    # 中和器补了 required=false，所以这里必须仍拿得到输出。
    assert out.strip(), "audit query returned nothing after hardening"


def test_neutralizers_cover_declared_filters_and_drivers(tmp_path):
    root = _repo(tmp_path)
    for key, value in [("filter.evil.clean", "true"),
                       ("filter.evil.required", "true"),
                       ("diff.mine.textconv", "true"),
                       ("core.fsmonitor", "true"),
                       ("user.name", "harmless")]:
        subprocess.run(["git", "config", key, value], cwd=root, check=True)

    flags = _git_config_neutralizers(str(root))
    joined = " ".join(flags)

    assert "filter.evil.clean=" in joined
    assert "diff.mine.textconv=" in joined
    assert "core.fsmonitor=" in joined
    assert "filter.evil.required=false" in joined
    assert "user.name" not in joined, "neutralizer must not touch harmless keys"


def test_audit_env_drops_credentials_and_pins_config_sources(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-topsecret")
    monkeypatch.setenv("HARNESS_HARMLESS", "keep-me")

    env = _git_audit_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("HARNESS_HARMLESS") == "keep-me"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.parametrize("args,expected", [
    (_DIFF_TRACKED, "diff --git a/f.c b/f.c"),
    (_STATUS, "M f.c"),
    (("ls-files", "--others", "--exclude-standard", "--", "."), "new.c"),
    (_DIFF_NEW, "diff --git a/new.c b/new.c"),
])
def test_clean_worktree_audit_output_is_unchanged(tmp_path, args, expected):
    """加固不得削弱正常仓库的审计记录 —— 记录消失是这段代码要防的头号后果。"""
    root = _repo(tmp_path)
    assert expected in _git_text(str(root), *args)


def test_sandboxed_path_blocks_execution_and_keeps_output(tmp_path, monkeypatch):
    """走原生沙箱时同样拿得到正确输出，且攻击不执行（未枚举到的向量的兜底）。"""
    from core import isolation

    try:
        isolation.select_backend()
    except Exception as exc:  # pragma: no cover - 取决于宿主能力
        pytest.skip(f"native isolation backend unavailable: {exc}")

    from core.state import State

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-topsecret")
    marker = tmp_path / "pwned.txt"
    root = _repo(tmp_path)
    _arm(root, {"filter.evil.clean": "{evil}"}, "f.c filter=evil", marker)

    state = State.new("experiment", tmp_path / "runs")
    scratch = Path(state.root)
    scratch.mkdir(parents=True, exist_ok=True)

    out = _git_text(str(root), *_STATUS, state=state, sandbox_root=scratch)

    assert not marker.exists()
    assert "M f.c" in out


def test_call_sites_keep_the_hardening_flags():
    """防止有人在维护中把 --no-ext-diff / --no-textconv 顺手删掉。

    2026-09-07 的实测正是发现 `diff --no-index` 少了 --no-ext-diff：防御打了一半
    比没有防御更危险，因为看代码会以为已经挡住了。
    """
    source = Path(__file__).resolve().parents[1] / "tools" / "safe_bash.py"
    text = source.read_text(encoding="utf-8")
    body = text[text.index("def _record_source_worktree_diffs"):]
    body = body[:body.index("\ndef ", 1)]

    for call in [seg for seg in body.split('_git_text(')[1:]]:
        head = call[:400]
        if '"diff"' in head:
            assert '"--no-ext-diff"' in head and '"--no-textconv"' in head, (
                f"a git diff audit call lost its hardening flags: {head[:120]}")


def test_sandbox_degradation_is_disclosed_once(tmp_path, monkeypatch):
    """沙箱起不来时命令照跑，但降级必须进 transcript —— 少一层防线是事实。"""
    from core.state import State

    root = _repo(tmp_path)
    state = State.new("experiment", tmp_path / "runs")
    Path(state.root).mkdir(parents=True, exist_ok=True)

    import core.sandbox as core_sandbox

    def _boom(*_a, **_k):
        raise RuntimeError("no isolation backend here")

    monkeypatch.setattr(core_sandbox, "prepare_attempt_command", _boom)

    events: list[tuple] = []
    monkeypatch.setattr(state, "append_transcript",
                        lambda name, **kw: events.append((name, kw)))

    for _ in range(3):
        out = _git_text(str(root), *_STATUS, state=state,
                        sandbox_root=Path(state.root))
        assert "M f.c" in out, "degraded path must still produce the audit record"

    disclosures = [e for e in events
                   if e[0] == "source_worktree_audit_sandbox_unavailable"]
    assert len(disclosures) == 1, "disclose exactly once per run, not per query"
    assert "no isolation backend here" in disclosures[0][1]["reason"]
