"""路径角色的授权边界与破坏性命令的 containment 判定（2026-08-04 重构）。

锁住三条不变量：

1. **权限只能由有权者声明。** artifact（包括 agent 自己 save_artifact 存的
   declared_route / pre_registration）不产生任何角色。改之前，一份完全普通的
   in-source 构建契约 `{source_path: S, build_dir: S/build}` 会把 S 映射成
   不可变 baseline、让契约重叠，从此 run 内**每一条**写命令都返回不可授权的
   invalid_path_role_contract —— agent 自己把自己锁死且无法恢复。镜像形态
   `{build_dir: /任意路径}` 则是静默自授权。

2. **判定极性是"证明无害才放"。** `rm -rf *`、`rm -rf build`、
   `find . -delete`、`rsync --delete` 改之前都提取到零个目标 → 被当作"没有写
   操作"静默放行（在 baseline 的 cwd 里也一样）。现在提取不出可证明的路径就是
   UNRESOLVED，走询问。

3. **人工批准登记为受限能力，而不是消费一次命令 hash。** 高危命令的授权 key
   曾是整条命令原文，所以改一个 CMake 参数就要重新批准一次。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools.path_roles import (
    collect_path_roles,
    validate_path_roles,
)
from nodes.experiment.tools.subprocess_policy import (
    bash_sandbox_roots,
    register_approved_subprocess_write_root,
)


class _FakeState:
    """最小 state：只提供角色收集与拦截判定用到的表面。"""

    node_type = "experiment"
    run_id = "run-test"

    def __init__(self, tmp_path: Path, roles=None, artifacts=None,
                 node_inputs=None):
        self.root = tmp_path / "run"
        self.root.mkdir(parents=True, exist_ok=True)
        self.hook_state = {}
        if roles is not None:
            self.hook_state["path_roles"] = roles
        if node_inputs is not None:
            self.hook_state["node_inputs"] = node_inputs
        self._artifacts = artifacts or []
        self.transcript = []

    def list_artifacts(self):
        return [{"id": f"a{i}", "type": t}
                for i, (t, _) in enumerate(self._artifacts)]

    def read_artifact(self, artifact_id):
        payload = self._artifacts[int(artifact_id[1:])][1]
        return {"metadata": {}, "content": json.dumps(payload)}

    def append_transcript(self, event, **fields):
        self.transcript.append((event, fields))


@pytest.fixture
def outside_tmp(tmp_path_factory):
    """/tmp 是 _classify_write_path 的无条件白名单。

    在 /tmp 下建 workspace，所有"未声明路径应被拦"的断言都会假通过。
    """
    import os
    import shutil
    import tempfile

    override = os.getenv("HF_TEST_NON_TMP_DIR")
    base = Path(override) if override else Path.home() / ".cache"
    try:
        base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="hf-authority-", dir=str(base)))
    except OSError:
        pytest.skip("无 /tmp 之外的可写目录（设 HF_TEST_NON_TMP_DIR 以启用）")
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def workspace(outside_tmp):
    ws = outside_tmp / "workspace"
    (ws / "gromacs-2024.6" / "src").mkdir(parents=True)
    (ws / "gromacs-2024.6-build").mkdir(parents=True)
    return ws


# ── 1. artifact 不得授予或锁定任何角色 ──────────────────────────────────────

def test_agent_declared_route_grants_no_roles(tmp_path, workspace):
    """agent 存的 in-source 契约既不产生 baseline，也不产生 build_root。"""
    src = str(workspace / "gromacs-2024.6")
    state = _FakeState(tmp_path, artifacts=[
        ("declared_route", {"source_path": src, "build_dir": f"{src}/build"}),
    ])
    roles = collect_path_roles(state)
    assert not [r for r in roles if r.path.startswith(str(workspace))]
    # 而且契约仍然有效 —— 这正是原先 brick 的那一步
    assert validate_path_roles(state)["valid"] is True


def test_agent_cannot_self_grant_write_access(tmp_path, workspace):
    """`{build_dir: <任意路径>}` 不再静默变成可写 build_root。"""
    target = workspace / "agent-selected"
    state = _FakeState(tmp_path, artifacts=[
        ("declared_route", {"build_dir": str(target)}),
    ])
    scope, _ = sb._classify_write_path(str(target / "x"), state)
    assert scope == "unknown_absolute"


def test_pre_registration_grants_no_roles(tmp_path, workspace):
    """pre_registration 也是 agent 可写的（save_artifact 无类型白名单）。"""
    state = _FakeState(tmp_path, artifacts=[
        ("pre_registration", {"source_path": str(workspace / "gromacs-2024.6")}),
    ])
    assert not [r for r in collect_path_roles(state)
                if r.path.startswith(str(workspace))]


def test_fixture_source_is_immutable_and_requires_out_of_source_build(tmp_path, workspace):
    """可信 fixture 的 source_path 是 baseline；嵌套 build 必须被拒绝。"""
    src = str(workspace / "gromacs-2024.6")
    nested = _FakeState(tmp_path, node_inputs={
        "source_path": src, "build_dir": f"{src}/build"})
    assert validate_path_roles(nested)["valid"] is False

    state = _FakeState(tmp_path, node_inputs={
        "source_path": src,
        "build_dir": str(workspace / "gromacs-2024.6-build"),
    })
    assert validate_path_roles(state)["valid"] is True
    assert {r.role for r in collect_path_roles(state)} >= {
        "source_baseline_root", "build_root"}
    assert sb._classify_write_path(f"{src}/CMakeLists.txt", state)[0] == \
        "protected_source_tree"


def test_explicit_baseline_still_immutable_and_out_of_source_only(tmp_path,
                                                                  workspace):
    """显式 canonical baseline 保持不可写，且仍禁止 in-source build。"""
    src = str(workspace / "gromacs-2024.6")
    state = _FakeState(tmp_path, roles={"source_baseline_root": src})
    scope, _ = sb._classify_write_path(f"{src}/CMakeLists.txt", state)
    assert scope == "protected_source_tree"
    assert scope not in sb._SCOPE_APPROVAL_SCOPES

    nested = _FakeState(tmp_path, roles={
        "source_baseline_root": src, "build_root": f"{src}/build"})
    assert validate_path_roles(nested)["valid"] is False


def test_container_nesting_is_not_a_conflict(tmp_path, workspace):
    """experiment_root 只是容器，baseline 落在其内不构成冲突。"""
    state = _FakeState(tmp_path, roles={
        "experiment_root": str(workspace),
        "source_baseline_root": str(workspace / "gromacs-2024.6"),
    })
    assert validate_path_roles(state)["valid"] is True


def test_workspace_owner_nesting_is_not_a_baseline_conflict(tmp_path, workspace):
    """workspace_root is an ownership boundary; the baseline stays read-only."""
    state = _FakeState(tmp_path, roles={
        "source_baseline_root": str(workspace / "gromacs-2024.6"),
        "run_root": str(workspace / "runtime"),
        "build_root": str(workspace / "build"),
    })
    state.workspace_root = workspace
    report = validate_path_roles(state)
    assert report["valid"], report


def test_contract_conflict_is_localized(tmp_path, workspace):
    """契约冲突只毒化涉事的树，不相关的 run-local 写入照常。"""
    src = str(workspace / "gromacs-2024.6")
    state = _FakeState(tmp_path, roles={
        "source_baseline_root": src,
        "build_root": f"{src}/build",
        "run_root": str(workspace / "runtime"),
    })
    assert sb._classify_write_path(f"{src}/build/x", state)[0] == \
        "invalid_path_role_contract"
    assert sb._classify_write_path(
        str(workspace / "runtime" / "log.txt"), state)[0] == "safe"


def test_sandbox_projection_uses_concrete_roles_and_preserves_baseline(tmp_path, workspace):
    """The broad workspace is readonly; only concrete application roles write."""
    source = workspace / "gromacs-2024.6"
    run = workspace / "runtime"
    build = workspace / "build"
    (run / "source").mkdir(parents=True)
    build.mkdir()
    state = _FakeState(tmp_path, roles={
        "source_baseline_root": str(source),
        "managed_source_root": str(run / "source"),
        "run_root": str(run),
        "build_root": str(build),
    })
    state.workspace_root = workspace
    # Path roles describe meaning; these external fixture roots become OS
    # capabilities only after the test consumes an explicit local approval.
    register_approved_subprocess_write_root(state, str(run))
    register_approved_subprocess_write_root(state, str(build))
    writable, readonly = bash_sandbox_roots(
        state,
        str(build),
        authorized_targets=[str(run / "result.dat")],
    )
    assert writable is not None and readonly is not None
    assert set(map(Path, writable)) >= {run, build}
    assert Path(workspace) not in writable
    assert set(map(Path, readonly)) >= {state.root, source}


# ── 2. 目标解析：证明无害才放 ────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "rm -rf *",
    "rm -rf build",
    "rm -rf ./build",
    "find . -delete",
    "find . -name '*.o' -exec rm -rf {} ;",
    "rsync -a --delete /elsewhere/ .",
    "git clean -xfd",
    "git reset --hard",
])
def test_destructive_commands_are_seen_inside_baseline(tmp_path, workspace, cmd):
    """基线内的破坏性目标必须由生产路径门在执行前拒绝。"""
    src = workspace / "gromacs-2024.6"
    state = _FakeState(tmp_path, roles={"source_baseline_root": str(src)})
    blocked = sb._bash_path_effects_guard(
        state,
        f"cd {src} && {cmd}",
        cwd=str(src),
        allow_authorization=False,
    )
    assert blocked is not None and blocked["status"] == "error"
    assert blocked["blocker"]["scope"] in {
        "protected_source_tree",
        "unresolved_target",
    }

@pytest.mark.parametrize("cmd,expect_unresolved", [
    ("rm -rf $UNSET_BUILD_DIR", True),
    ("rm -rf $(cat targets.txt)", True),
    ("source ./env.sh && rm -rf $BUILD", True),
    ("ls dirs | xargs rm -rf", True),
    ("rm -rf build/*/CMakeCache.txt", True),
    ("rm -rf build", False),
])
def test_unprovable_targets_become_unresolved(tmp_path, workspace, cmd,
                                              expect_unresolved):
    base = str(workspace / "gromacs-2024.6-build")
    _, targets, _ = sb._extract_write_targets(cmd, base)
    assert (sb.UNRESOLVED in targets) is expect_unresolved, targets


@pytest.mark.parametrize("command,relative_target", [
    ("touch file", "file"),
    ("mkdir -p child", "child"),
    ("cp source destination", "destination"),
    ("tee output", "output"),
    ("sed -i s/a/b/ config", "config"),
])
def test_shell_path_effects_extract_bare_relative_write_targets(
    workspace, command, relative_target,
):
    base = str(workspace / "run")
    effects = sb._analyze_shell_path_effects(command, base)
    assert effects[-1][1] == str(Path(base) / relative_target)


def test_shell_path_effects_use_explicit_out_of_source_cmake_build_dir(workspace):
    source = workspace / "gromacs-2024.6"
    build = workspace / "gromacs-2024.6-build"

    effects = sb._analyze_shell_path_effects(
        f"cmake -S {source} -B {build}",
        str(source),
    )

    assert effects == [("cmake", str(build))]


def test_shell_path_effects_ignore_redirection_character_inside_quotes(workspace):
    base = str(workspace / "run")
    command = "python -c \"print(1 > 0)\""
    assert sb._analyze_shell_path_effects(command, base) == []


def test_unresolved_target_is_rejected_before_running(tmp_path, workspace):
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path, roles={"build_root": str(build)})
    blocked = sb._bash_path_effects_guard(
        state,
        f"cd {build} && rm -rf $(cat stale.txt)",
        cwd=str(build),
        allow_authorization=False,
    )
    assert blocked is not None and blocked["status"] == "error"
    assert blocked["blocker"]["scope"] == "unresolved_target"

def test_lexical_cd_resolves_not_yet_created_directory(tmp_path, workspace):
    """mkdir -p X && cd X && rm -rf *：X 尚不存在也必须解析到 X，而不是回退旧 cwd。"""
    fresh = workspace / "gromacs-2024.6-build" / "attempt-01"
    assert not fresh.exists()
    base = sb._command_base_dir(f"mkdir -p {fresh} && cd {fresh}", str(tmp_path))
    assert base == str(fresh)


def test_glob_containment_target_is_the_directory(tmp_path, workspace):
    build = str(workspace / "gromacs-2024.6-build")
    _, targets, _ = sb._extract_write_targets("rm -rf *", build)
    assert targets == [build]
    _, nested, _ = sb._extract_write_targets("rm -rf sub/*", build)
    assert nested == [f"{build}/sub"]


# ── 3. 人工批准 → 登记为受限能力 ────────────────────────────────────────────

def test_approval_registers_directory_and_stops_asking(tmp_path, workspace):
    """批准一次后，同目录下的后续写入不再询问；父目录仍要问。"""
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path)
    target = str(build / "CMakeCache.txt")

    assert sb._classify_write_path(target, state)[0] == "unknown_absolute"
    registered = sb._register_approved_write_root(state, target, op="rm")
    assert registered == str(build)

    assert sb._classify_write_path(target, state)[0] == "safe"
    assert sb._classify_write_path(str(build / "deep" / "x"), state)[0] == "safe"
    # 兄弟目录与父目录不因此获得权限
    assert sb._classify_write_path(
        str(workspace / "gromacs-install" / "x"), state)[0] == "unknown_absolute"
    assert sb._classify_write_path(
        str(workspace / "other.txt"), state)[0] == "unknown_absolute"


def test_approved_root_allows_clearing_contents_not_removing_itself(tmp_path,
                                                                    workspace):
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path)
    sb._register_approved_write_root(state, str(build), op="rm")

    assert sb._contained_cleanup(state, f"cd {build} && rm -rf *", None) == [
        str(build)]
    # 删除目录本身超出 cleanup="contents"
    assert sb._contained_cleanup(state, f"rm -rf {build}", None) is None


def test_contained_cleanup_survives_changed_build_flags(tmp_path, workspace):
    """放行依据是包含关系，不是命令串 —— 换 CMake 参数不该重新询问。"""
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path, roles={"build_root": str(build)})
    for flags in ("-DGMX_GPU=CUDA", "-DGMX_GPU=OFF -DGMX_MPI=ON"):
        cmd = f"cd {build} && rm -rf * && cmake {flags} ../gromacs-2024.6"
        assert sb._contained_cleanup(state, cmd, None) == [str(build)]


def test_cleanup_outside_declared_root_is_not_released(tmp_path, workspace):
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path, roles={"build_root": str(build)})
    assert sb._contained_cleanup(
        state, f"cd {build} && rm -rf ../gromacs-2024.6", None) is None
    assert sb._contained_cleanup(
        state, f"cd {build} && rm -rf $STALE", None) is None


def test_baseline_cleanup_never_released(tmp_path, workspace):
    src = workspace / "gromacs-2024.6"
    state = _FakeState(tmp_path, roles={"source_baseline_root": str(src)})
    assert sb._contained_cleanup(state, f"cd {src} && rm -rf *", None) is None


def test_run_root_may_be_removed_and_recreated(tmp_path, workspace):
    run = workspace / "runtime"
    state = _FakeState(tmp_path, roles={"run_root": str(run)})
    assert sb._contained_cleanup(state, f"rm -rf {run}", None) == [str(run)]


# ── 3b. containment 放行只中和"删除"这一种危险 ──────────────────────────────

@pytest.mark.parametrize("cmd", [
    "sudo rm -rf {build}/*",
    "cd {build} && sudo rm -rf *",
    "cd {build} && rm -rf * && sudo apt-get install -y libfoo",
    "cd {build} && pkexec rm -rf x",
    "cd {build} && rm -rf * ; shutdown -h now",
    "cd {build} && rm -rf * && dd if=/dev/zero of=/dev/sda",
])
def test_containment_never_releases_non_deletion_risk(tmp_path, workspace, cmd):
    """提权/关机/块设备写入不因"删除目标可证明"而被放行。

    两个 matcher 都是命中即返回，而 `sudo rm -rf x` 先命中删除类；
    再加上 _strip_write_wrappers 会剥掉 sudo 才读目标，
    只看"上报的那一个 category"就会让提权搭着受控删除溜过去。
    """
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path, roles={"build_root": str(build)})
    assert sb._contained_cleanup(state, cmd.format(build=build), None) is None


def test_containment_releases_pure_contained_deletion(tmp_path, workspace):
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path, roles={"build_root": str(build)})
    assert sb._contained_cleanup(
        state, f"cd {build} && rm -rf * && cmake ..", None) == [str(build)]


@pytest.mark.parametrize("cmd", [
    "pip install foo $(cat requirements.txt)",
    "echo find",
    "cmake --install . $(echo prefix)",
])
def test_non_destructive_commands_are_not_forced_unresolved(tmp_path, cmd):
    """不透明上下文里的 install/find 只是参数，不该被当成破坏性动词。"""
    assert sb._mentions_destructive(cmd) is False


# ── 4. 系统根与共享盘不得被声明为可写 ───────────────────────────────────────

@pytest.mark.parametrize("root", ["/home", "/mnt", "/beegfs", "/scratch", "/"])
def test_machine_wide_roots_cannot_be_made_writable(tmp_path, root):
    state = _FakeState(tmp_path, roles={
        "dependency_root": {"path": root, "writable": True}})
    declared = [r for r in collect_path_roles(state) if r.path == root]
    assert declared and not declared[0].writable


def test_subtree_of_shared_mount_is_still_declarable(tmp_path):
    state = _FakeState(tmp_path, roles={
        "dependency_root": {"path": "/beegfs/proj/deps", "writable": True}})
    declared = [r for r in collect_path_roles(state)
                if r.path == "/beegfs/proj/deps"]
    assert declared and declared[0].writable


# ── 5. 声明被静默丢弃的三种形态 ─────────────────────────────────────────────

def test_list_form_declares_every_path(tmp_path, workspace):
    a, b = str(workspace / "b-cuda"), str(workspace / "b-cpu")
    state = _FakeState(tmp_path, roles={"build_root": [a, b]})
    paths = {r.path for r in collect_path_roles(state) if r.role == "build_root"}
    assert {a, b} <= paths


def test_relative_declaration_is_reported_not_dropped(tmp_path):
    state = _FakeState(tmp_path, roles={"build_root": "relative/x"})
    warnings = validate_path_roles(state)["warnings"]
    assert any("not an absolute path" in w for w in warnings)


def test_explicit_build_root_keeps_run_local_default(tmp_path, workspace):
    """声明 workspace 构建目录不应让 run-local 默认 build_root 消失。"""
    state = _FakeState(tmp_path, roles={
        "build_root": str(workspace / "gromacs-2024.6-build")})
    build_paths = {r.path for r in collect_path_roles(state)
                   if r.role == "build_root"}
    assert len(build_paths) == 2
    assert any(str(state.root) in p for p in build_paths)


# ── 6. 端到端：原始 GROMACS 序列的批准次数 ──────────────────────────────────



def test_removing_the_approved_directory_itself_still_asks(tmp_path, workspace):
    """登记授予的是"可写 + 可清空内容"，删除目录本身仍需再次确认。"""
    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path)
    sb._register_approved_write_root(state, str(build), op="rm")

    assert sb._contained_cleanup(state, f"cd {build} && rm -rf *", None)
    gate = sb._unified_highrisk_gate(
        state, f"rm -rf {build}", mode="shell", tool="safe_run_bash",
        kind="bash")
    assert gate is not None and gate["status"] == "pause"


# ── 7. 授权的存活范围 ───────────────────────────────────────────────────────



def test_grant_is_recorded_in_run_manifest(tmp_path, workspace):
    """hook_state 不落盘，所以授权必须进 manifest 才可审计、才可被显式续用。"""
    from nodes.experiment.tools.run_contract import _human_write_grants

    build = workspace / "gromacs-2024.6-build"
    state = _FakeState(tmp_path)
    sb._register_approved_write_root(state, str(build), op="rm")

    grants = _human_write_grants(state)
    assert grants == [{
        "path": str(build), "authority": "human", "writable": True,
        "cleanup": "contents", "survives_resume": True,
    }]


def test_carried_grant_declares_a_limited_role(tmp_path, workspace):
    """续跑携带的授权走 node_inputs，语义与人工批准一致（可写、可清内容）。"""
    build = str(workspace / "gromacs-2024.6-build")
    state = _FakeState(tmp_path, node_inputs={
        "path_roles": {"approved_write_root": [
            {"path": build, "writable": True, "cleanup": "contents"}]}})
    roles = [r for r in collect_path_roles(state)
             if r.role == "approved_write_root"]
    assert roles and roles[0].path == build
    assert roles[0].allows_cleanup(delete_root=False) is True
    assert roles[0].allows_cleanup(delete_root=True) is False


# ── 8. 随附 fixture 的路径角色契约必须有效 ──────────────────────────────────

def _shipped_fixtures():
    import yaml
    root = Path(__file__).resolve().parents[1] / "fixtures"
    for path in sorted(root.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        yield path.name, (doc.get("node_inputs") or {})


@pytest.mark.parametrize("name,node_inputs", list(_shipped_fixtures()))
def test_shipped_fixture_contracts_are_valid(tmp_path, name, node_inputs):
    """随附 fixture 不得声明出会毒化整棵树的契约。

    契约冲突不再全局阻断，但仍会让涉事目录下的所有写入失败 —— 对
    --no-interactive 的 E2E fixture 而言等同于跑不完。
    """
    state = _FakeState(tmp_path, node_inputs=node_inputs)
    report = validate_path_roles(state)
    assert report["valid"], f"{name}: {report['errors']}"


@pytest.mark.parametrize("name,node_inputs", list(_shipped_fixtures()))
def test_shipped_fixture_declarations_all_take_effect(tmp_path, name,
                                                      node_inputs):
    """声明被静默丢弃是最难查的故障：fixture 里不允许出现。"""
    state = _FakeState(tmp_path, node_inputs=node_inputs)
    dropped = [w for w in validate_path_roles(state)["warnings"]
               if "ignored" in w or "forced read-only" in w]
    assert not dropped, f"{name}: {dropped}"


def test_build_gate_fixture_supports_in_source_build(tmp_path):
    """e2e_build_gate 在源码树内构建，声明后必须合法且可写。"""
    import yaml
    path = (Path(__file__).resolve().parents[1]
            / "fixtures" / "e2e_build_gate.yaml")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    state = _FakeState(tmp_path, node_inputs=doc["node_inputs"])
    assert validate_path_roles(state)["valid"]
    build = Path("~/experiments/E3SM_Ocean_GPU/ParallelIO-pio2_6_7/build"
                 ).expanduser()
    assert sb._classify_write_path(str(build / "CMakeCache.txt"), state)[0] == \
        "safe"
