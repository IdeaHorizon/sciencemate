"""framework_exemptions.yaml 的加载器（v3.1）。

运行时治理收紧后的过渡豁免登记表：
  - tool_name_collisions：同名工具注册在迁移期内 WARN 而非硬错误
  - hook_mutable_response：指定 node_type 的 on_llm_response 仍收可变原对象
  - custom_agent_loops：允许 ship 自定义 agent_loop.py 的节点白名单

文件在 repo 根（与 .scope_map.yaml 同级保护，仅 framework owner 可改）。
文件缺失 = 空豁免（全部走 strict 默认），不报错 —— 部署环境可能没带它。
"""
from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("exemptions")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EXEMPTIONS_FILE = _REPO_ROOT / "framework_exemptions.yaml"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not _EXEMPTIONS_FILE.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(_EXEMPTIONS_FILE.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001 — 豁免表坏了按空表处理（fail-strict）
        log.warning("framework_exemptions.yaml 解析失败（按无豁免处理）：%s", e)
        return {}


def _expired(entry: dict) -> bool:
    d = entry.get("deadline")
    if not d:
        return False
    try:
        return date.fromisoformat(str(d)) < date.today()
    except ValueError:
        return False


def tool_collision_exemption(tool_name: str, module: str | None) -> dict | None:
    """同名工具注册是否在豁免期内。返回登记条目（含 deadline 过期标记）或 None。"""
    for entry in _load().get("tool_name_collisions") or []:
        if entry.get("tool") != tool_name:
            continue
        om = entry.get("owner_module") or ""
        if module and om and not module.startswith(om):
            continue
        entry = dict(entry)
        entry["expired"] = _expired(entry)
        return entry
    return None


def hook_response_mutable(node_type: str) -> bool:
    """该 node_type 的 on_llm_response 是否仍收可变原对象（迁移期豁免）。"""
    for entry in _load().get("hook_mutable_response") or []:
        if entry.get("node_type") == node_type and not _expired(entry):
            return True
    return False


def allowed_custom_loops() -> set[str]:
    """登记过的 custom agent_loop 节点集合。"""
    return {
        e.get("node_type")
        for e in (_load().get("custom_agent_loops") or [])
        if e.get("node_type")
    }


def reload() -> None:
    """测试用：清缓存。"""
    _load.cache_clear()
