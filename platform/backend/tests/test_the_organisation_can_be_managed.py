"""组织级的三件事在界面上走得通：知识、算力授权、忘了密码。

09-17 盘点专业版时的实情：
  - **组织级 KB**：机制 09-16 就做完了（一台安装一个 org 层，晋升上去的结论全组织
    共读），但界面上一页都没有 —— 前端零调用 KB 接口，后端所有读都按项目问。
    人看不见也管不了。
  - **算力管理**：`grants.yaml` 有两个消费方（agent 提示注入、预注册冻结门禁），
    却**没有任何写入方** —— 唯一的办法是登到那台机器上手写 YAML。
  - **忘了密码**：一条路都没有（改密码要旧密码，create-admin 刻意不动密码）。

这里锁住这三条路的边界。真读真写要一台 harness（D 类证据在 PR 里）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


# ── 组织层：读得到，且和"按项目读"是两个问题 ────────────────────────────────

def test_the_bridge_can_read_the_organisation_layer() -> None:
    """`core.api` 早就支持 `project_id=None` → org 层，卡在桥那里要求非空。"""
    runtime = (REPO / "platform_runtime.py").read_text(encoding="utf-8")
    assert 'scope = str(request.get("scope") or "project")' in runtime, (
        "桥不认 scope —— 组织层还是读不到")
    assert 'if scope == "org":\n        project_id = None' in runtime


def test_an_empty_project_id_is_not_how_you_ask_for_the_org_layer() -> None:
    """用显式 scope，不用"project_id 空就读 org"。

    后者会让一次漏传（前端某处忘了带 id）悄悄变成"读整个组织的知识"，而那是
    一件完全不同的事，且不报错。
    """
    runtime = (REPO / "platform_runtime.py").read_text(encoding="utf-8")
    assert 'elif not isinstance(project_id, str) or not project_id.strip():' in runtime, (
        "project_id 空不再被拒 —— 漏传会静默读成组织层")


def test_the_org_endpoints_exist_and_ask_for_the_org_scope() -> None:
    kb = (REPO / "platform/backend/app/api/v1/kb.py").read_text(encoding="utf-8")
    for path in ("/organisation/stats", "/organisation/claims", "/organisation/concepts"):
        assert path in kb, f"组织层少了 {path}"
    assert kb.count('scope="org"') >= 3, "有端点没说要读组织层 —— 它会去读项目层"


def test_memory_and_proposals_stay_project_level() -> None:
    """项目记忆和待审提案挂在项目上，组织层没有这两样 —— 要说出来而不是返空。"""
    runtime = (REPO / "platform_runtime.py").read_text(encoding="utf-8")
    assert "memory 是项目层的，组织层没有这一项" in runtime
    assert "待审提案挂在项目上，按项目问" in runtime


# ── 算力授权：写的那一半 ────────────────────────────────────────────────────

def test_grants_finally_have_a_writer() -> None:
    """读它的有两处，写它的一处都没有 —— 那正是"算力管理不存在"的真身。"""
    capabilities = (REPO / "core/capabilities.py").read_text(encoding="utf-8")
    assert "def write_grants_file(" in capabilities, "grants.yaml 仍然只能手写"
    assert "def read_grants_file(" in capabilities


def test_the_writer_lives_next_to_the_reader() -> None:
    """格式的规则只有一个模块知道。App Server 自己拼 YAML = 第二份会分叉的知识。

    判据扫**代码**，不扫注释 —— 那份文件的文档里当然会提到 YAML（它正在解释
    为什么自己不碰）。扫文本的话，写清楚理由反而会把闸打红。
    """
    import ast

    source = (REPO / "platform/backend/app/services/compute_grants.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name.split(".")[0] for n in ast.walk(tree)
              if isinstance(n, ast.Import) for a in n.names}
    names |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom)}
    assert "yaml" not in {n.lower() for n in names}, (
        "App Server 自己碰 YAML 了 —— 格式知识分成了两份")
    assert "compute_grants_set" in source


def test_writing_grants_is_atomic() -> None:
    """半份 YAML 会被读成"解析失败 → 按无授权处理" —— 也就是所有人的算力突然全没。"""
    capabilities = (REPO / "core/capabilities.py").read_text(encoding="utf-8")
    assert "scratch.replace(target)" in capabilities, "直接往原文件上写 —— 中途挂了就是半份"


def test_changing_grants_drops_the_cache() -> None:
    """改完授权，下一次注入/门禁就该看到新的。"""
    capabilities = (REPO / "core/capabilities.py").read_text(encoding="utf-8")
    writer = capabilities[capabilities.index("def write_grants_file("):]
    assert "_cache = None" in writer[:writer.index("\n\n\n")], "缓存没清 —— 新授权最多等一个 TTL"


def test_only_an_institution_admin_changes_who_may_compute() -> None:
    resources = (REPO / "platform/backend/app/api/v1/resources.py").read_text(encoding="utf-8")
    block = resources[resources.index('@compute_router.put("/grants")'):]
    assert "UserRole.INSTITUTION_ADMIN.value" in block[:900], (
        "谁都能改算力授权 —— 那台机器是整个组织共用的")


def test_grants_are_not_a_project_thing() -> None:
    """算力授权是全组织的，不属于任何一个项目（曾经错挂在 /projects 前缀下）。"""
    resources = (REPO / "platform/backend/app/api/v1/resources.py").read_text(encoding="utf-8")
    assert '@compute_router.get("/grants")' in resources
    assert '@router.get("/compute/grants")' not in resources


def test_live_status_is_never_written_to_the_file() -> None:
    """探得到的事实永远不进文件：写进去就会腐坏，还多一个真相源。"""
    runtime = (REPO / "platform_runtime.py").read_text(encoding="utf-8")
    write = runtime[runtime.index("def run_compute_grants_set("):]
    head = write[:write.index("\n\n\n")]
    assert "_probe" not in head, "写回时把探针结果一起写进去了"
