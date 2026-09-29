"""LLM call cache —— dev 阶段省钱 + 提速。

启用：环境变量 `HARNESS_LLM_CACHE=on`（默认 off，prod 绝不开）。

机制：
  cache_key = sha256(model + temperature + messages + tools 的 normalized JSON)
  store path = $HARNESS_FRAMEWORK_HOME/cache/llm/<key>.json

只在 **temperature==0** 时缓存（确保同 prompt 同结果）。其它温度透传不缓存
（temperature>0 缓存会让 dev 拿到第一次的 dice roll，永远只见一种回答，违反
dev 想看模型多样性的初衷）。

不缓存的边界情况：
  - 工具调用结果含 nondeterministic 外部 state（这里不区分；调用方自己负责）
  - 用户传 `_no_cache=True` 显式 opt-out 单次调用
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.paths import home


def _enabled() -> bool:
    return os.getenv("HARNESS_LLM_CACHE", "").lower() in ("on", "1", "true", "yes")


def _cache_dir() -> Path:
    d = home() / "cache" / "llm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_messages(messages: list) -> list[dict]:
    """LLMMessage / dict 都规范成 sorted-key dict。"""
    out = []
    for m in messages:
        if hasattr(m, "role"):    # LLMMessage
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                d["tool_calls"] = m.tool_calls
            if m.tool_call_id:
                d["tool_call_id"] = m.tool_call_id
            if m.name:
                d["name"] = m.name
        else:
            d = dict(m)
        out.append(d)
    return out


def cache_key(*, model: str, temperature: float, messages: list,
              tools: list | None = None) -> str:
    payload = {
        "model": model,
        "temperature": float(temperature),
        "messages": _normalize_messages(messages),
        "tools": tools or None,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def should_cache(temperature: float) -> bool:
    """只在 temperature==0 时缓存（确定性）。"""
    if not _enabled():
        return False
    return abs(float(temperature)) < 1e-9


def get(key: str) -> dict | None:
    if not _enabled():
        return None
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def put(key: str, response_dict: dict) -> None:
    if not _enabled():
        return
    path = _cache_dir() / f"{key}.json"
    try:
        path.write_text(json.dumps(response_dict, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def clear() -> int:
    """删全部 cache 文件。返回删了几个。"""
    n = 0
    if not _cache_dir().exists():
        return 0
    for p in _cache_dir().glob("*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def stats() -> dict:
    """返回 {count, size_bytes}。"""
    d = _cache_dir()
    if not d.exists():
        return {"count": 0, "size_bytes": 0, "path": str(d)}
    files = list(d.glob("*.json"))
    return {
        "count": len(files),
        "size_bytes": sum(f.stat().st_size for f in files),
        "path": str(d),
        "enabled": _enabled(),
    }
