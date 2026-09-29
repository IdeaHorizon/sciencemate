"""提交权限在**闸口**守，不靠事后取证。

## 现场（2026-08-11，157 个文件的实验数据被删）

    01:04:56  orchestrator 派发 experiment
    02:20:42  experiment 请求 checkpoint（162 文件，prefix=experiment）
    02:20:42  平台提交 0d275be = `node(experiment): checkpoint`
              —— 157 个文件全在 experiment/ 下，**平台一步没错**
    02:34:55  harness 守卫 reset 到 52d37d2（**会话起点**），删 6 份 LAMMPS 生产日志

守卫用的基线是绑定时那个值，一次都没被抬起来。

## 为什么 `reset --mixed` 会删文件

注释说「A hard reset would destroy legitimate work」，所以用了 `--mixed`。
但两段各自无害的逻辑组合成了硬删除：

    reset --mixed  → 157 个文件从「已提交」变回**未跟踪**
    同一函数下半段 → 扫未提交改动，157 个都不在 permitted 里 → 未跟踪 = 删除

## 三层里有两层是重复的，而且抄件更差

    谁            用什么事实                 失效模式
    平台          自己 DB 记的 head + 锁      fail-closed，拒绝写
    harness 守卫  **内存里的私有基线**        ← 删数据
    命令闸口      —— 不存在 ——

v2.1 声称「提交权限专属平台，harness 只能*请求* checkpoint」，但在**行使权限
的地方**一道门都没有：`git commit` / `reset --hard` / `rebase` / `checkout`
全部畅通。唯一写着这条红线的地方是 `nodes/experiment/harness.yaml` 的 prompt
（"必须先 request_human_input"）—— prompt 里的「必须」不是机制。

## 改法

  1. 补上缺的那道门：改写历史的 git → `match_boundary_violation` 硬拒。
     **只读子命令走白名单**，新出现的子命令默认拒（名单的方向要对）。
  2. harness 守卫不再回退提交：记录 + 抬基线 + 大声报告，判决留给拥有事实的
     那一方（平台的 fail-closed 检查）。
  3. 未提交的越界写：一个字不动。

代价不对称，这是它该往哪边倒的理由：
  误删一个合法 commit → 不可再生的实验数据没了，DB 与磁盘永久分叉
  放过一个非法 commit → git log 里看得见，平台下次 checkpoint 拒绝写
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.project_workspace import _DELEGATED_KEY, enforce_after_tool, note_delegated_workspace
from shared.lib.dangerous_commands import match_boundary_violation


# ─────────────────────────────────────────────────────────────────────────
# 1. 闸口：改写历史的 git 硬拒，只读 git 放行
# ─────────────────────────────────────────────────────────────────────────

MUTATING = [
    "git commit -m 'x'",
    "git commit -am wip",
    "git reset --hard HEAD~1",
    "git reset --mixed HEAD",
    "git checkout main",
    "git switch -c feature",
    "git restore --staged .",
    "git rebase -i HEAD~3",
    "git merge origin/main",
    "git cherry-pick abc123",
    "git revert HEAD",
    "git stash",
    "git branch -D old",
    "git tag v1",
    "git update-ref refs/heads/main abc",
    "git push origin main",
    "git pull",
    "git add -A",
    "git clean -fd",
    "git filter-branch --all",
    "git -C /some/path commit -m x",          # 全局选项在子命令之前
    "cd /tmp && git commit -m x",             # 前面挂了别的命令
    "git   commit   -m x",                    # 多空格
]

# 这些**不是**只读，但它们碰不到 Project 仓库的历史 —— 拒了就是误伤。
# 复刻别人的工作要 clone 外部代码，这是正当科研操作（`repro_snapshot.py`
# 自己就有正则在检测 `git clone` / `git submodule`）。
#   clone      建的是**另一个**仓库，动不了本仓 HEAD
#   submodule  不移动 superproject 的 HEAD；写进工作区的文件由工作区守卫管
#   apply      改工作区，不提交；同上
# 明确**不**放行的：fetch（`git fetch . HEAD:branch` 能改本地分支）、
# config（`core.hooksPath` 能让平台的提交去跑任意钩子）。
DOES_NOT_TOUCH_PROJECT_HISTORY = [
    "git clone https://github.com/foo/bar.git /tmp/bar",
    "git clone --depth 1 https://example.com/pkg/GSW-Fortran.git /tmp/gsw || true",
    "git submodule status",
    "git submodule update --init --recursive",
    "git apply /tmp/fix.patch",
]

REFUSED_EVEN_THOUGH_THEY_LOOK_HARMLESS = [
    "git fetch origin main:main",     # 能直接改本地分支
    "git config core.hooksPath /tmp/x",  # 能让平台的提交跑任意钩子
]


@pytest.mark.parametrize("cmd", DOES_NOT_TOUCH_PROJECT_HISTORY)
def test_external_repo_work_is_not_blocked(cmd: str) -> None:
    """闸口守的是**本仓的提交权限**，不是"凡 git 皆拦"。"""
    assert match_boundary_violation(cmd) is None, f"误拦：{cmd}"


@pytest.mark.parametrize("cmd", REFUSED_EVEN_THOUGH_THEY_LOOK_HARMLESS)
def test_the_two_that_look_safe_but_are_not(cmd: str) -> None:
    assert match_boundary_violation(cmd) is not None, f"未拦：{cmd}"


READ_ONLY = [
    "git log --oneline -5",
    "git show HEAD",
    "git diff HEAD~1",
    "git status --porcelain",
    "git rev-parse HEAD",
    "git ls-files",
    "git cat-file -p HEAD",
    "git blame foo.py",
    "git describe --tags",
    "git grep TODO",
    "git -C /some/path log",
    "git --version",
]


@pytest.mark.parametrize("cmd", MUTATING)
def test_history_mutating_git_is_refused(cmd: str) -> None:
    """提交权限专属平台 —— 节点工具连做的能力都不该有。"""
    assert match_boundary_violation(cmd) is not None, f"未拦：{cmd}"


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_read_only_git_stays_open(cmd: str) -> None:
    """节点读 Git 历史是正当需求（查冻结、看 diff）—— 不能连坐。"""
    assert match_boundary_violation(cmd) is None, f"误拦：{cmd}"


def test_an_unknown_git_subcommand_defaults_to_refused() -> None:
    """名单的方向：白名单只读，其余默认拒。

    反过来（黑名单改写）意味着 git 新增的任何子命令默认放行 ——
    「护栏要扫盘，不要写名单」说的就是这个方向问题。
    """
    assert match_boundary_violation("git some-future-subcommand --force") is not None


def test_python_subprocess_git_is_refused_too() -> None:
    """从 execute_python 绕过去一样不行。"""
    assert match_boundary_violation(
        "subprocess.run(['git', 'commit', '-m', 'x'])", mode="python"
    ) is not None
    assert match_boundary_violation(
        "subprocess.run(['git', 'log', '--oneline'])", mode="python"
    ) is None


def test_the_refusal_points_at_the_real_cause() -> None:
    """报错不许指向假原因。

    Git 权限刚接进 boundary 类时套用了 artifact 那套文案 —— 模型被告知
    "请改用 save_artifact"，而它其实是跑了 `git commit`。指向假原因的报错
    最贵：它让人（和模型）去修一个不存在的问题。
    """
    from shared.lib.dangerous_commands import BOUNDARY_DENY_MESSAGE, boundary_deny_message

    git_msg = boundary_deny_message("Git 权限越界（git commit）")
    assert "提交权限专属平台" in git_msg
    assert "save_artifact" not in git_msg, "别把人指去 artifact 工具"
    assert "git log" in git_msg, "要说清什么还能做（只读子命令）"

    artifact_msg = boundary_deny_message("shell 重定向写入框架状态")
    assert "save_artifact" in artifact_msg
    assert "提交权限专属平台" not in artifact_msg

    # 三个调用点（其中一个在同事的节点里）都写 `.format(category=…)`，
    # 这条兼容路径必须给出同样对的文案。
    assert BOUNDARY_DENY_MESSAGE.format(category="Git 权限越界（git commit）") == git_msg


# ─────────────────────────────────────────────────────────────────────────
# 2. 守卫：提交只报告，不回退
# ─────────────────────────────────────────────────────────────────────────

def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class _State:
    def __init__(self, worktree: Path) -> None:
        self.project_worktree = worktree
        self.workspace_relative_path = "notes"
        self.node_type = "_orchestrator"
        self.hook_state: dict = {
            "_project_workspace_expected_head": _git(worktree, "rev-parse", "HEAD"),
        }
        self.transcript: list = []

    def append_transcript(self, event: str, **fields) -> None:
        self.transcript.append((event, fields))


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for node in ("experiments", "plan", "notes"):
        (root / node).mkdir(parents=True, exist_ok=True)
        (root / node / ".keep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "session: initialize")
    return root


def test_an_unattributable_commit_is_reported_not_destroyed(worktree: Path) -> None:
    """守卫解释不了的提交 —— 留证据，别毁数据。

    这是 2026-08-11 那 157 个文件的形状：守卫拿着陈旧基线，把一个**完全合法**
    的 checkpoint 判成越界。改判之后代价不对称是显式选择的：
    误删不可逆，放过看得见。
    """
    state = _State(worktree)

    # 一个守卫无从归因的提交（没派发过任何子节点，却出现了碰 experiment/ 的提交）
    (worktree / "experiments/prod_T0.60_r1.log").write_text("LAMMPS\n" * 900, encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "someone committed")
    head = _git(worktree, "rev-parse", "HEAD")

    enforce_after_tool(state, "safe_run_bash")

    assert _git(worktree, "rev-parse", "HEAD") == head, "提交不该被回退"
    assert (worktree / "experiments/prod_T0.60_r1.log").exists(), (
        "实验数据必须还在 —— reset --mixed 会把它变成未跟踪，"
        "然后被同一函数下半段当越界写删掉"
    )
    # 2026-08-13：分类只看快进与否。模型做不出提交（闸口硬拒 git 写命令 +
    # 沙箱可写面不含 .git），快进提交按构造就是平台的 —— 记 advanced，
    # 路径清单照留。"unattributed" 只剩非快进（reset / 换分支）一种。
    events = dict((n, f) for n, f in state.transcript)
    assert "workspace_head_advanced" in events, "必须留证据"
    assert "experiments/prod_T0.60_r1.log" in events["workspace_head_advanced"]["paths"]


def test_a_legitimate_checkpoint_advances_the_baseline_quietly(worktree: Path) -> None:
    """能归因的（派发过的子节点自己的目录）照旧安静放行并抬基线。"""
    state = _State(worktree)
    note_delegated_workspace(state, "experiment")
    (worktree / "experiments/result.json").write_text("{}\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "node(experiment): checkpoint")
    head = _git(worktree, "rev-parse", "HEAD")

    enforce_after_tool(state, "run_node")

    assert state.hook_state["_project_workspace_expected_head"] == head
    events = [name for name, _ in state.transcript]
    assert "workspace_head_advanced" in events
    assert "workspace_head_changed_unattributed" not in events


def test_a_stale_baseline_can_no_longer_delete_anything(worktree: Path) -> None:
    """把 2026-08-11 的真实序列原样跑一遍：陈旧基线 + 新 permitted 集合。

    这条用例在修复前会删掉 6 份日志。
    """
    state = _State(worktree)

    # 派发 experiment；checkpoint 落在恢复暂停的路径上（不经过守卫）
    note_delegated_workspace(state, "experiment")
    logs = [f"experiments/prod_T{t}_r{r}.log" for t in ("0.60", "0.70", "0.80") for r in (1, 2)]
    for rel in logs:
        (worktree / rel).write_text("LAMMPS production run\n" * 900, encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "node(experiment): checkpoint 1786410296-fc9b0b")
    checkpoint = _git(worktree, "rev-parse", "HEAD")
    state.hook_state.pop(_DELEGATED_KEY, None)     # 跨进程恢复丢掉了派发记录

    # 下一次工具调用：派发别人
    note_delegated_workspace(state, "hypothesis")
    enforce_after_tool(state, "run_node")

    assert _git(worktree, "rev-parse", "HEAD") == checkpoint
    for rel in logs:
        assert (worktree / rel).exists(), f"{rel} 被守卫删了"


def test_a_non_fast_forward_is_the_loud_case(worktree: Path) -> None:
    """回退 / 换分支不是"合法推进" —— 这才是 unattributed 要吵的形状。"""
    (worktree / "experiments/x.txt").write_text("x\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "second")
    state = _State(worktree)                        # 基线 = second
    _git(worktree, "reset", "--hard", "HEAD~1")     # 非快进：HEAD 往回走

    enforce_after_tool(state, "safe_run_bash")

    events = [name for name, _ in state.transcript]
    assert "workspace_head_changed_unattributed" in events
    assert "workspace_head_advanced" not in events


def test_uncommitted_out_of_bounds_writes_are_reported_not_reverted(worktree: Path) -> None:
    """未提交的越界写：报告，不回退（2026-08-13 断言翻转）。

    上一版这里断言"必须被删掉"。同款"删掉"在 2026-08-13 把 orchestrator
    写进**自己目录**的三份产物送进了隔离区 —— 事后层分不出作者，它的任何
    破坏性动作都建立在推断上。墙已移到写的那一刻（沙箱）；这里只留证据。
    """
    from core.project_workspace import capture_before_tool

    state = _State(worktree)
    capture_before_tool(state)
    (worktree / "experiments/shell_wrote_this.txt").write_text("x\n", encoding="utf-8")

    report = enforce_after_tool(state, "safe_run_bash")

    assert report is not None and "experiments/shell_wrote_this.txt" in report["paths"]
    assert (worktree / "experiments/shell_wrote_this.txt").exists(), "见证层不许动文件"
