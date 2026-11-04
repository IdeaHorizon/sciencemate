"""派发即授权：冻结预注册里显式声明的数据源主机，批准派发时一并落成出网授权（#1244，RFC D7）。

判据（issue 原文）：一个预注册声明了 NOAA 的 run，在无人值守下第一次 `fetch_resource` 不触发 pause。
边界：只认这一趟 **exact 绑定**的那份冻结预注册（id + 版本 + 哈希）；项目里别的预注册、运行中新冒出来
的主机都不算；授权只在内存里。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from core import capability_grants as cg
from core.bootstrap import bootstrap
from core.executor import _grant_declared_sources
from core.ledger import record_version
from core.prereg_sources import data_source_section, declared_hosts
from core.sandbox import effective_egress_policy
from core.state import State
from core.tool_registry import execute as execute_tool

PREREG = """# 海温异常的年际变化

## Q1 问题
用 NOAA OISST 判断 2023 年海温异常是否超出 2 个标准差。

## 数据源、许可与前提
- NOAA PSL 的 OISST v2.1：https://psl.noaa.gov/data/gridded/data.noaa.oisst.v2.highres.html
- 镜像：downloads.psl.noaa.gov（同一机构）
- 文件 sst.mnmean.nc、requirements.txt 不是主机

## 分析
alpha=0.05
"""


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@example.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text("schema_version: 2\nname: dispatch grants\n", encoding="utf-8")
    for node in ("hypothesis", "experiment"):
        (root / node / "artifacts").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"], check=True, capture_output=True)
    return root


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf-home"))
    monkeypatch.delenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", raising=False)
    bootstrap()
    worktree = _worktree(tmp_path)
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                       project_id="dispatch-grants", project_worktree=worktree)
    producer = State.new(node_type="hypothesis", base_dir=tmp_path / "producer-runs",
                         project_id="dispatch-grants", project_worktree=worktree)
    return tmp_path, worktree, parent, producer


def _bound_child(project, prereg: str = PREREG) -> State:
    tmp_path, worktree, parent, producer = project
    saved = producer.save_artifact("pre_registration", "sst", prereg)
    frozen = producer.mark_frozen(saved["id"])

    async def contract():
        created = await execute_tool("task", parent, action="create", title="海温异常")
        task_id = created["task"]["id"]
        out = await execute_tool(
            "task", parent, action="contract", task_id=task_id, target_node="experiment",
            prereg_artifact_id=saved["id"], prereg_version=str(record_version(frozen)),
            prereg_content_hash=frozen["content_hash"])
        assert out["status"] == "success", out
        return out

    out = asyncio.run(contract())
    rev = out.get("revision") or out.get("contract") or {}
    uuid = rev.get("task_instance_uuid") or out.get("task_instance_uuid")
    digest = rev.get("digest") or rev.get("task_contract_digest") or out.get("task_contract_digest")
    assert uuid and digest, out
    return State.new(node_type="experiment", base_dir=tmp_path / "child-runs",
                     project_id="dispatch-grants", project_worktree=worktree,
                     task_instance_uuid=uuid, task_contract_digest=digest)


def test_only_the_declared_section_counts_and_files_are_not_hosts() -> None:
    hosts = declared_hosts(data_source_section(PREREG))
    assert hosts == ["psl.noaa.gov", "downloads.psl.noaa.gov"], hosts


def test_a_bound_run_can_reach_its_declared_sources_without_asking(project) -> None:
    child = _bound_child(project)
    _grant_declared_sources(child, None)
    assert cg.is_granted(child, "https://psl.noaa.gov/data/x.nc")
    assert "psl.noaa.gov" in effective_egress_policy(child)["entries"], (
        "授权落下了，生效白名单却不含它 —— 第一次 fetch_resource 照样会停下来问")
    ledger = {g["host"]: g for g in cg.grants_ledger(child)}
    assert ledger["psl.noaa.gov"]["source"] == "dispatch"
    assert ledger["psl.noaa.gov"]["basis"].startswith("prereg:")
    events = [line for line in child.transcript_path.read_text(encoding="utf-8").splitlines()
              if "capability_granted_at_dispatch" in line]
    assert events, "派发时授予了什么要记在 run 自己的账上"


def test_an_unbound_run_gets_nothing(project) -> None:
    tmp_path, worktree, _parent, _producer = project
    loose = State.new(node_type="experiment", base_dir=tmp_path / "loose",
                      project_id="dispatch-grants", project_worktree=worktree)
    _grant_declared_sources(loose, None)
    assert cg.granted_hosts(loose) == ()


def test_a_data_run_dispatched_by_it_inherits_only_dispatch_grants(project) -> None:
    parent = _bound_child(project)
    _grant_declared_sources(parent, None)
    assert cg.grant_from_answer(parent, "repo.msys2.org", "允许")     # 人在卡片上批的
    tmp_path, worktree, _o, _p = project
    data = State.new(node_type="data", base_dir=tmp_path / "data-runs",
                     project_id="dispatch-grants", project_worktree=worktree)
    _grant_declared_sources(data, parent)
    assert cg.is_granted(data, "psl.noaa.gov")
    assert not cg.is_granted(data, "repo.msys2.org"), (
        "人在卡片上批的是「这个 run 能不能连」，不外推到别的 run")


def test_the_experiment_preflight_reads_the_same_rules() -> None:
    """experiment 第一轮的可达性预检与派发授权读的是同一份解析（一个出处）。"""
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nodes" / "experiment"))
    hooks = importlib.import_module("nodes.experiment.hooks")
    assert hooks._extract_declared_hosts is declared_hosts
    assert hooks._data_source_section is data_source_section
