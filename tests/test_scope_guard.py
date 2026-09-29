"""scripts/forgejo_pre_receive_hook.py 的 scope guard 单测。

覆盖 2026-07 audit 修的两个盲区 + 原有行为回归：
  - self-lift：author 在自己分支改 .scope_map.yaml 给自己加 '*' → 必须拒
    （map 信任 main 上的版本 + 权限元文件保护）
  - evil merge：借 merge commit 夹带任一 parent 都没有的越权改动 → 必须拒
    （对 merge commit 校验 combined diff，不再 `--no-merges` 整体跳过）
  - 诚实 merge main（拉别人已评审的改动）不误报（nidy 2026-06-18 回归）
  - 普通单 parent push 的 in-scope / out-of-scope 行为不变
  - `_anyone` 公共区（shared/contrib/**）任何 author 可改

实现方式：tmp 目录里搭真 git repo，直接调 hook 的 `_check_one_ref`。
hook 的 git 调用依赖 CWD，所以每个 test 里 monkeypatch.chdir 进 repo。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

hook = pytest.importorskip(
    "scripts.forgejo_pre_receive_hook",
    reason="hook 依赖 PyYAML（跟 core.loader 同款依赖，正常都有）",
)

SCOPE_YAML = """\
wangd:
  - "*"
nidy:
  - "nodes/hypothesis/**"
_anyone:
  - "shared/contrib/**"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, stderr=subprocess.PIPE,
    ).decode()


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit_all(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """main 上有 scope map + core/ + nodes/hypothesis/ 的最小 repo。"""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "test@test")
    _git(r, "config", "user.name", "test")
    _write(r, ".scope_map.yaml", SCOPE_YAML)
    _write(r, "core/framework.py", "# framework code\n")
    _write(r, "nodes/hypothesis/h.py", "# hypothesis node\n")
    _commit_all(r, "init main")
    # hook 的 _run 在 CWD 跑 git —— 测试期间站到 repo 里
    monkeypatch.chdir(r)
    return r


def _as_author(monkeypatch, name: str) -> None:
    monkeypatch.setattr(hook, "AUTHOR", name)


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 1. 普通单 parent push（既有行为不许被 merge 改动破坏）
# ─────────────────────────────────────────────────────────────────────────────

def test_in_scope_change_passes(repo, monkeypatch):
    _as_author(monkeypatch, "nidy")
    main = _sha(repo, "main")
    _git(repo, "checkout", "-b", "feat/in-scope")
    _write(repo, "nodes/hypothesis/h.py", "# edited by owner\n")
    new = _commit_all(repo, "edit own node")
    assert hook._check_one_ref(main, new, "refs/heads/feat/in-scope") is True


def test_out_of_scope_change_rejected(repo, monkeypatch, capsys):
    _as_author(monkeypatch, "nidy")
    main = _sha(repo, "main")
    _git(repo, "checkout", "-b", "feat/oops")
    _write(repo, "core/framework.py", "# sneaky core edit\n")
    new = _commit_all(repo, "edit core")
    assert hook._check_one_ref(main, new, "refs/heads/feat/oops") is False
    assert "core/framework.py" in capsys.readouterr().err


def test_anyone_can_touch_shared_contrib(repo, monkeypatch):
    _as_author(monkeypatch, "someone_not_in_map")
    main = _sha(repo, "main")
    _git(repo, "checkout", "-b", "feat/contrib")
    _write(repo, "shared/contrib/util.py", "def helper(): ...\n")
    new = _commit_all(repo, "add contrib helper")
    assert hook._check_one_ref(main, new, "refs/heads/feat/contrib") is True


def test_main_ref_always_passes(repo, monkeypatch):
    _as_author(monkeypatch, "nidy")
    main = _sha(repo, "main")
    assert hook._check_one_ref(main, main, "refs/heads/main") is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. self-lift 盲区（audit 2026-07）
# ─────────────────────────────────────────────────────────────────────────────

def test_self_lift_via_scope_map_rejected(repo, monkeypatch, capsys):
    """nidy 在自己分支改 map 给自己加 '*' → 必须拒。

    双保险都在测：(a) map 从 main（trusted）读，分支上的自封 '*' 无效；
    (b) `.scope_map.yaml` 是权限元文件，非 owner 改就拒。"""
    _as_author(monkeypatch, "nidy")
    main = _sha(repo, "main")
    _git(repo, "checkout", "-b", "feat/lift")
    _write(repo, ".scope_map.yaml", SCOPE_YAML.replace(
        '  - "nodes/hypothesis/**"', '  - "*"', 1))
    new = _commit_all(repo, "give myself *")
    assert hook._check_one_ref(main, new, "refs/heads/feat/lift") is False
    assert ".scope_map.yaml" in capsys.readouterr().err


def test_framework_exemptions_also_protected(repo, monkeypatch):
    _as_author(monkeypatch, "nidy")
    main = _sha(repo, "main")
    _git(repo, "checkout", "-b", "feat/exempt")
    _write(repo, "framework_exemptions.yaml", "nidy: everything\n")
    new = _commit_all(repo, "add exemptions")
    assert hook._check_one_ref(main, new, "refs/heads/feat/exempt") is False


def test_owner_may_modify_scope_map(repo, monkeypatch):
    """wangd（main 上 map 里的 '*' owner）改 map 当然可以。"""
    _as_author(monkeypatch, "wangd")
    main = _sha(repo, "main")
    _git(repo, "checkout", "-b", "feat/map-update")
    _write(repo, ".scope_map.yaml", SCOPE_YAML + "\nnewbie:\n  - \"docs/**\"\n")
    new = _commit_all(repo, "add newbie scope")
    assert hook._check_one_ref(main, new, "refs/heads/feat/map-update") is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. evil merge 盲区（audit 2026-07）
# ─────────────────────────────────────────────────────────────────────────────

def _setup_diverged(repo) -> tuple[str, str]:
    """feat 分支上有合法 commit；之后 main 前进一格。返 (feat_tip, main_tip)。"""
    _git(repo, "checkout", "-b", "feat/merge-case")
    _write(repo, "nodes/hypothesis/h.py", "# legit node edit\n")
    feat_tip = _commit_all(repo, "legit node work")
    _git(repo, "checkout", "main")
    _write(repo, "core/main_feature.py", "# landed via someone's PR\n")
    main_tip = _commit_all(repo, "main advances")
    _git(repo, "checkout", "feat/merge-case")
    return feat_tip, main_tip


def test_honest_merge_of_main_passes(repo, monkeypatch):
    """老实 `git merge main`（拉别人已评审的 core 改动）不许误报。"""
    _as_author(monkeypatch, "nidy")
    feat_tip, _ = _setup_diverged(repo)
    _git(repo, "merge", "main", "--no-ff", "-m", "merge main")
    merged = _sha(repo)
    assert hook._check_one_ref(feat_tip, merged, "refs/heads/feat/merge-case") is True


def test_evil_merge_smuggle_rejected(repo, monkeypatch, capsys):
    """merge commit 里夹带任一 parent 都没有的 core/ 改动 → 必须拒。

    老实现 `--no-merges` 把 merge commit 整体跳过，这条路能把越权改动
    一路带进 PR；现在对 merge commit 校验 combined diff。"""
    _as_author(monkeypatch, "nidy")
    feat_tip, _ = _setup_diverged(repo)
    _git(repo, "merge", "main", "--no-ff", "--no-commit")
    # 趁 merge 未提交，顺手把越权改动混进 merge commit
    _write(repo, "core/framework.py", "# smuggled through merge\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "merge main (with a passenger)")
    merged = _sha(repo)
    assert hook._check_one_ref(feat_tip, merged, "refs/heads/feat/merge-case") is False
    err = capsys.readouterr().err
    assert "core/framework.py" in err
    assert "merge" in err  # 报错里标注了经 merge 夹带


def test_evil_merge_smuggling_scope_map_rejected(repo, monkeypatch, capsys):
    """evil merge + self-lift 组合拳：merge commit 里改 .scope_map.yaml → 拒。"""
    _as_author(monkeypatch, "nidy")
    feat_tip, _ = _setup_diverged(repo)
    _git(repo, "merge", "main", "--no-ff", "--no-commit")
    _write(repo, ".scope_map.yaml", SCOPE_YAML.replace(
        '  - "nodes/hypothesis/**"', '  - "*"', 1))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "merge main (map passenger)")
    merged = _sha(repo)
    assert hook._check_one_ref(feat_tip, merged, "refs/heads/feat/merge-case") is False
    assert ".scope_map.yaml" in capsys.readouterr().err


def test_new_branch_push_with_merge_checked(repo, monkeypatch):
    """oldrev 全 0（新分支首推）也要跑 merge 校验（base 落到 main）。"""
    _as_author(monkeypatch, "nidy")
    feat_tip, _ = _setup_diverged(repo)
    _git(repo, "merge", "main", "--no-ff", "--no-commit")
    _write(repo, "core/framework.py", "# smuggled on first push\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "merge main (passenger)")
    merged = _sha(repo)
    zero = "0" * 40
    assert hook._check_one_ref(zero, merged, "refs/heads/feat/merge-case") is False


def _advance_main_many(repo, n: int) -> str:
    """main 连推 n 个别人的 commit（模拟 owner 分支落后 main 很多，如 v3.1）。"""
    _git(repo, "checkout", "main")
    for i in range(n):
        _write(repo, f"core/mod_{i}.py", f"# landed via someone's PR {i}\n")
        _write(repo, f"nodes/data/d_{i}.py", f"# cuib work {i}\n")
        _commit_all(repo, f"main advances {i}")
    return _sha(repo)


def test_stale_branch_merges_far_ahead_main_passes(repo, monkeypatch):
    """nidy 生产 bug 复刻：owner 分支停在旧位置，main 前进了几十个别人的 commit，
    owner `git merge main` 拉进这一大坨后 push —— 只有他自己的 hypothesis 改动
    该被算数。旧代码用**分支旧位置**作 base，把 merge 进来的整个 main 判越权。"""
    _as_author(monkeypatch, "nidy")
    # owner 分支在旧 main 上做自己的活
    _git(repo, "checkout", "-b", "hypothesis")
    _write(repo, "nodes/hypothesis/runtime_trace.py", "# nidy runtime\n")
    _write(repo, "nodes/hypothesis/.gitignore", "*.local\n")
    stale_tip = _commit_all(repo, "nidy: hypothesis 改动")
    # main 前进一大坨（别人的 core / data 改动）
    _advance_main_many(repo, 12)
    # owner merge main 再补一笔
    _git(repo, "checkout", "hypothesis")
    _git(repo, "merge", "main", "--no-ff", "-m", "merge v3.1 main")
    _write(repo, "nodes/hypothesis/fixtures.yaml", "cases: []\n")
    merged = _commit_all(repo, "nidy: fixtures")
    # base = 分支旧位置（正是 pre-receive 收到的 oldrev）
    assert hook._check_one_ref(stale_tip, merged,
                                "refs/heads/hypothesis") is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. 部署版 .sh 与 .py 行为一致（堵实现漂移 —— nidy 生产 bug 的根因是
#    .py 修了、部署的 .sh 没同步，且 .sh 从没被测过）
# ─────────────────────────────────────────────────────────────────────────────

_SH_HOOK = Path(__file__).resolve().parent.parent / "scripts" / "forgejo_pre_receive_hook.sh"


def _run_sh_hook(repo: Path, oldrev: str, newrev: str, refname: str,
                 author: str) -> bool:
    """跑部署版 shell hook。返 True=通过（exit 0）/ False=拒（exit≠0）。"""
    import os
    env = dict(os.environ, GITEA_PUSHER_NAME=author)
    p = subprocess.run(
        ["bash", str(_SH_HOOK)],
        input=f"{oldrev} {newrev} {refname}\n".encode(),
        cwd=repo, env=env, capture_output=True,
    )
    return p.returncode == 0


@pytest.mark.skipif(not _SH_HOOK.exists(), reason="部署版 .sh 不在")
def test_sh_honest_merge_of_far_ahead_main_passes(repo, monkeypatch):
    """部署版 .sh 必须和 .py 一样：owner merge 落后很多的 main 后 push 放行。
    这条测试是 nidy 生产 bug 的直接守卫 —— 旧 .sh 会 REJECT。"""
    _git(repo, "checkout", "-b", "hypothesis")
    _write(repo, "nodes/hypothesis/x.py", "# nidy\n")
    stale = _commit_all(repo, "nidy work")
    _advance_main_many(repo, 8)
    _git(repo, "checkout", "hypothesis")
    _git(repo, "merge", "main", "--no-ff", "-m", "merge main")
    _write(repo, "nodes/hypothesis/more.py", "# nidy more\n")
    merged = _commit_all(repo, "nidy more")
    assert _run_sh_hook(repo, stale, merged, "refs/heads/hypothesis", "nidy") is True


@pytest.mark.skipif(not _SH_HOOK.exists(), reason="部署版 .sh 不在")
def test_sh_out_of_scope_rejected(repo, monkeypatch):
    """部署版 .sh：真越权（改 core/）仍要拒。"""
    _git(repo, "checkout", "-b", "hypothesis")
    _write(repo, "core/framework.py", "# nidy sneaks core\n")
    tip = _commit_all(repo, "sneaky")
    base = _sha(repo, "main")
    assert _run_sh_hook(repo, base, tip, "refs/heads/hypothesis", "nidy") is False


@pytest.mark.skipif(not _SH_HOOK.exists(), reason="部署版 .sh 不在")
def test_sh_evil_merge_rejected(repo, monkeypatch):
    """部署版 .sh：evil merge 夹带 core/ 改动要拒（三点 diff 看最终 tree）。"""
    _git(repo, "checkout", "-b", "hypothesis")
    _write(repo, "nodes/hypothesis/x.py", "# nidy\n")
    stale = _commit_all(repo, "nidy work")
    _advance_main_many(repo, 3)
    _git(repo, "checkout", "hypothesis")
    _git(repo, "merge", "main", "--no-ff", "--no-commit")
    _write(repo, "core/framework.py", "# smuggled\n")
    merged = _commit_all(repo, "merge with passenger")
    assert _run_sh_hook(repo, stale, merged, "refs/heads/hypothesis", "nidy") is False


@pytest.mark.skipif(not _SH_HOOK.exists(), reason="部署版 .sh 不在")
def test_sh_in_scope_passes(repo, monkeypatch):
    """部署版 .sh：纯 scope 内改动放行。"""
    _git(repo, "checkout", "-b", "hypothesis")
    _write(repo, "nodes/hypothesis/h.py", "# edit\n")
    tip = _commit_all(repo, "node edit")
    base = _sha(repo, "main")
    assert _run_sh_hook(repo, base, tip, "refs/heads/hypothesis", "nidy") is True
