"""PROFILE.md / PROJECT.md curated 稳定指令层测试。

覆盖：
  - directives_loader: read/write PROFILE.md + PROJECT.md
  - per-node 子段 extract（## 节点级指令 → ### <node_type>）
  - load_directives_for_node 同时返 profile + project（+ node-specific 段）
  - context_engine 把 PROFILE/PROJECT 注入 system_prompt
  - propose_profile_update → 进 inbox（不直接写文件）
  - resolve_proposal accept → apply_profile_update 真正落盘
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import pytest

from core.harness import NodeHarness
from core.state import State

# ── 共享 setup ────────────────────────────────────────────────────────────

_HARNESS_HOME_ENV = "HARNESS_FRAMEWORK_HOME"


@contextlib.contextmanager
def _harness_home_env(home: Path):
    """临时设 HARNESS_FRAMEWORK_HOME。directives_loader._user_dir() 读这个。"""
    old = os.environ.get(_HARNESS_HOME_ENV)
    os.environ[_HARNESS_HOME_ENV] = str(home)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(_HARNESS_HOME_ENV, None)
        else:
            os.environ[_HARNESS_HOME_ENV] = old


def _isolated_state(profile_text: str | None = None, project_text: str | None = None):
    """构造一个隔离 state + 预置 PROFILE.md / PROJECT.md。

    返回 (state, harness_home, user_root, proj_root)。
    user_root = harness_home/user（即 PROFILE.md 的实际位置）。
    使用 _harness_home_env(harness_home) 让 directives_loader 找到它。
    """
    td = Path(tempfile.mkdtemp())
    harness_home = td / "harness_home"
    user_root = harness_home / "user"
    user_root.mkdir(parents=True)
    proj_root = td / "proj_root"
    proj_root.mkdir()

    if profile_text is not None:
        (user_root / "PROFILE.md").write_text(profile_text, encoding="utf-8")
    if project_text is not None:
        (proj_root / "PROJECT.md").write_text(project_text, encoding="utf-8")

    state = State.new(node_type="literature", base_dir=td / "runs", project_id=None)
    state.project_root = proj_root

    return state, harness_home, user_root, proj_root


# ── read PROFILE / PROJECT ───────────────────────────────────────────────


def test_read_profile_returns_content():
    state, home, user_root, _ = _isolated_state(
        profile_text="## 交互偏好\n- 用中文回复\n",
    )
    with _harness_home_env(home):
        from core.directives_loader import read_profile_md

        content = read_profile_md()
        assert "用中文回复" in content


def test_read_profile_returns_empty_when_missing():
    state, home, _, _ = _isolated_state()
    with _harness_home_env(home):
        from core.directives_loader import read_profile_md

        # 不存在时返 None（不是空 string）
        assert read_profile_md() in (None, "")


def test_read_project_md():
    state, _, _, proj_root = _isolated_state(
        project_text="## 项目约束\n- 用 PyTorch\n",
    )
    from core.directives_loader import read_project_md

    assert "用 PyTorch" in read_project_md(proj_root)


# ── per-node section extract ─────────────────────────────────────────────


def test_per_node_section_extracted():
    project_md = """## 项目约束

- target_venue: npj_comp_mat

## 节点级指令

### literature

- 重点关注 2023 后的 paper

### experiment

- 优先 LAMMPS
"""
    from core.directives_loader import extract_relevant_project_directives

    result = extract_relevant_project_directives(project_md, "literature")
    # 应包含项目约束 + literature 段，不含 experiment 段
    assert "target_venue: npj_comp_mat" in result
    assert "重点关注 2023 后的 paper" in result
    assert "优先 LAMMPS" not in result

    canonical = (
        "# Project\n\n"
        "<!-- platform:node-instructions:v1 -->\n\n"
        "## Node-specific instructions\n\n"
        "### Node: literature\nLITERATURE-CANARY\n\n"
        "### Node: experiments\nEXPERIMENTS-CANARY\n\n"
        "<!-- /platform:node-instructions:v1 -->"
    )
    literature = extract_relevant_project_directives(canonical, "literature")
    experiment = extract_relevant_project_directives(canonical, "experiment")
    assert "LITERATURE-CANARY" in literature
    assert "EXPERIMENTS-CANARY" not in literature
    assert "EXPERIMENTS-CANARY" in experiment
    assert "LITERATURE-CANARY" not in experiment
    with pytest.raises(RuntimeError, match="one complete managed"):
        extract_relevant_project_directives(
            f"{canonical}\n<!-- platform:node-instructions:v1 -->",
            "literature",
        )


def test_load_directives_for_node_full_combo():
    state, home, _, _ = _isolated_state(
        profile_text="## 交互偏好\n- 用中文\n",
        project_text=("## 项目约束\n- target: npj\n## 节点级指令\n### literature\n- 看实验论文\n"),
    )
    with _harness_home_env(home):
        from core.directives_loader import load_directives_for_node

        out = load_directives_for_node(state, "literature")
        assert "用中文" in out["profile"]
        assert "target: npj" in out["project"]
        assert "看实验论文" in out["project"]


# ── context_engine 注入 ──────────────────────────────────────────────────


def test_context_engine_injects_profile_and_project():
    state, home, _, _ = _isolated_state(
        profile_text="## 交互偏好\n- 总是用中文回复\n",
        project_text="## 项目约束\n- 数据集仅用 QM9\n",
    )
    h = NodeHarness(
        node_type="literature",
        version="0.1",
        system_prompt="literature 节点",
        rules=[],
        guidelines=[],
        skills=[],
        expected_outputs={},
        kb_query="_disable",
    )
    with _harness_home_env(home):
        from core.context_engine import build_messages

        msgs = build_messages(h, state)
        sys_content = msgs[0].content
        # 每层指令**恰好**进一次：重复注入不会报错，只会每轮静默烧掉一份 token
        # （PROFILE.md 曾被逐字注入两遍，见 fix/profile-injected-once）。
        assert sys_content.count("总是用中文回复") == 1
        assert sys_content.count("数据集仅用 QM9") == 1
        # 标头存在
        assert "PROFILE.md" in sys_content
        assert "PROJECT.md" in sys_content


def test_the_worktree_project_md_is_the_project_layer():
    """平台上项目层的权威是**会话 worktree 里的 `PROJECT.md`**。

    这条判据钉的是 RFC X3 之前那道静默缝：平台的「项目指令」编辑器写数据根
    底下的 `projects/<id>/PROJECT.md`，而加载器读 worktree 里那一份 ——
    用户改完 agent 一个字都读不到，两边都不报错。现在只有一个权威，
    所以「另一处写的内容不该出现」本身就是判据。
    """
    from core.directives_loader import load_directives_for_node

    state, home, _, proj_root = _isolated_state(project_text="ELSEWHERE-CANARY\n")
    worktree = proj_root.parent / "worktree"
    worktree.mkdir()
    (worktree / "PROJECT.md").write_text(
        "# Project\n\n"
        "<!-- platform:node-instructions:v1 -->\n\n"
        "## Node-specific instructions\n\n"
        "### Node: literature\nLITERATURE-ONLY-CANARY\n\n"
        "### Node: experiments\nEXPERIMENTS-ONLY-CANARY\n\n"
        "<!-- /platform:node-instructions:v1 -->",
        encoding="utf-8",
    )
    state.project_worktree = worktree

    with _harness_home_env(home):
        out = load_directives_for_node(state, "literature")

    assert "LITERATURE-ONLY-CANARY" in out["project"]
    assert "EXPERIMENTS-ONLY-CANARY" not in out["project"]
    assert "ELSEWHERE-CANARY" not in (out["project"] or "")


def test_the_personal_layer_carries_the_platform_written_settings():
    """个人层 = 用户写的 PROFILE.md + 平台写的 RESEARCH_SETTINGS.md。

    研究设置从前拼进指令快照的 personal 层随请求下发，**同时**还有一个
    `research_profile_snapshot` 字段也随请求下发 —— 后者零消费者。一个事实
    两条通道、其中一条是死的；现在只剩文件这一条。
    """
    from core.directives_loader import RESEARCH_SETTINGS_FILENAME, load_directives_for_node

    state, home, user_root, _ = _isolated_state(profile_text="USER-WROTE-THIS\n")
    (user_root / RESEARCH_SETTINGS_FILENAME).write_text(
        "PLATFORM-WROTE-THIS\n", encoding="utf-8"
    )

    with _harness_home_env(home):
        out = load_directives_for_node(state, "literature")

    assert "USER-WROTE-THIS" in out["profile"]
    assert "PLATFORM-WROTE-THIS" in out["profile"]
    assert out["digests"]["personal"] is not None


def test_changing_the_file_changes_the_next_turn():
    """不冻结：改了文件，下一轮就生效 —— 而且指纹跟着变。

    冻结那一列买的是「开跑之后指令不再变」。项目层本来就有这个性质（会话有
    自己的 git 分支）；个人层**不该有** —— 用户改了偏好还要等三个星期才生效，
    那不是特性。
    """
    from core.directives_loader import load_directives_for_node

    state, home, user_root, _ = _isolated_state(profile_text="FIRST\n")
    with _harness_home_env(home):
        first = load_directives_for_node(state, "literature")
        (user_root / "PROFILE.md").write_text("SECOND\n", encoding="utf-8")
        second = load_directives_for_node(state, "literature")

    assert "FIRST" in first["profile"] and "SECOND" not in first["profile"]
    assert "SECOND" in second["profile"] and "FIRST" not in second["profile"]
    assert first["digests"]["personal"] != second["digests"]["personal"]


# ── propose_profile_update → inbox → resolve_proposal accept ────────────


@pytest.mark.asyncio
async def test_propose_profile_update_writes_proposal_not_file():
    """propose 不直接改 PROFILE.md，只入 inbox。"""
    import shared.tools.builtin  # noqa - registers
    import shared.tools.library.profile_tools  # noqa - registers
    import shared.tools.library.proposals  # noqa

    state, home, user_root, proj_root = _isolated_state(profile_text="")
    state.project_id = "proj_test"
    profile_path = user_root / "PROFILE.md"

    with _harness_home_env(home):
        from shared.tools.library.profile_tools import _propose_profile_update

        result = await _propose_profile_update(
            state,
            scope="user",
            section="## 交互偏好",
            new_content="总是用中文回复",
            reasoning="user 2026-05-12 在 chat 里说：以后都用中文",
            operation="append",
        )
        assert result["status"] == "success"
        prop_id = result["proposal_id"]

        # PROFILE.md 应该还是空（propose 不直接写）
        if profile_path.exists():
            assert profile_path.read_text(encoding="utf-8") == ""

        # 现在 accept → 应该真正写入
        from shared.tools.library.proposals import (
            _list_proposals,
            _resolve_proposal,
        )

        listed = await _list_proposals(state, status="pending")
        assert listed["count"] >= 1
        accepted = await _resolve_proposal(
            state,
            proposal_id=prop_id,
            decision="accepted",
            reasoning="user 明确说要用中文",
        )
        assert accepted["status"] == "success"
        assert profile_path.exists()
        content = profile_path.read_text(encoding="utf-8")
        assert "总是用中文回复" in content
        assert "## 交互偏好" in content


@pytest.mark.asyncio
async def test_propose_profile_update_reject_path():
    """reject 不应该写入文件。"""
    import shared.tools.builtin  # noqa
    import shared.tools.library.profile_tools  # noqa
    import shared.tools.library.proposals  # noqa

    state, home, user_root, _ = _isolated_state(profile_text="")

    with _harness_home_env(home):
        from shared.tools.library.profile_tools import _propose_profile_update
        from shared.tools.library.proposals import _resolve_proposal

        result = await _propose_profile_update(
            state,
            scope="user",
            section="## 交互偏好",
            new_content="奇怪的偏好",
            reasoning="just testing reject path",
        )
        prop_id = result["proposal_id"]
        rej = await _resolve_proposal(
            state,
            proposal_id=prop_id,
            decision="rejected",
            reasoning="user 不要这个",
        )
        assert rej["status"] == "success"
        profile_path = user_root / "PROFILE.md"
        # 文件不存在或为空都可以（reject 不应写入）
        if profile_path.exists():
            assert profile_path.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_propose_profile_update_validates_reasoning_length():
    """reasoning 空 → 拒；短 → 放行。契约归 schema：非空由 parameters_schema 的
    minLength:1 声明、派发口核一次（工具体内不再手写），所以走 execute。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute

    bootstrap()
    state, home, _, _ = _isolated_state()

    with _harness_home_env(home):
        result = await execute(
            "propose_profile_update", state,
            scope="user", section="## X", new_content="X", reasoning="   ",
        )
    assert result["status"] == "error"          # 判决拆除：空拒、短放行
    assert result["parameter_violations"]
    assert "reasoning" in result["error"]
