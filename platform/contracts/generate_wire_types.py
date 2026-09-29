#!/usr/bin/env python3
"""从后端 Pydantic 模型**生成**前端线类型 —— 跨语言的契约只有一份手写。

## 为什么要有这个文件（2026-09-03）

`ChatRequest` 曾经有两份手写定义：后端 `app/api/v1/chat.py` 的 Pydantic 模型，
前端 `src/lib/api.ts` 的 TS interface。`min_length=1` 只存在于后端那份，前端
不知道；于是"什么算一次合法提交"在两边各自演化，前端为此又长出三道各自的
守卫。分叉时两边都不报错 —— 这是同一族事故第四次发生的直接土壤。

现在只有后端那份是手写的。本脚本把它的 JSON Schema 翻成 TS，写进
`frontend/src/lib/generated/chat-request.ts`；`--check` 模式比对已提交文件是否
与当前模型一致，pytest（`tests/test_wire_types_are_generated.py`）在 CI 里跑它。
手改生成文件、或改了模型没重新生成，都是红。

用法（在 `platform/backend` 下，那里有依赖）：

    uv run python ../contracts/generate_wire_types.py           # 重新生成
    uv run python ../contracts/generate_wire_types.py --check   # 只校验

## 支持的 schema 子集

只翻译这些模型实际用到的形状：object / string / number / boolean / null / array /
`$ref` / `const` / `enum` / `anyOf`（可空）/ `oneOf`（判别联合）。遇到别的形状
直接报错，不猜 —— 猜出来的类型和手抄没有区别。
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parent
PLATFORM_DIR = CONTRACTS_DIR.parent
BACKEND_DIR = PLATFORM_DIR / "backend"
OUTPUT = PLATFORM_DIR / "frontend" / "src" / "lib" / "generated" / "chat-request.ts"

HEADER = """// GENERATED FILE — do not edit by hand.
//
// Source of truth: platform/backend/app/api/v1/chat.py (Pydantic models).
// Regenerate:      cd platform/backend && uv run python ../contracts/generate_wire_types.py
// Enforced by:     platform/backend/tests/test_wire_types_are_generated.py
//
// 这份文件是后端线契约的投影。前端不许再手写第二份 —— 两份手写定义正是
// 「什么算一次合法提交」在四层各自演化的土壤（2026-09-03）。
"""


def models() -> list[type]:
    """要投影到前端的模型。加模型只改这里。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from app.api.v1.chat import ChatRequest

    return [ChatRequest]


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _ts_type(schema: dict[str, Any], *, aliases: dict[str, str]) -> str:
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"])
    if "oneOf" in schema:
        union = " | ".join(_ts_type(member, aliases=aliases) for member in schema["oneOf"])
        title = schema.get("title")
        if isinstance(title, str) and title:
            aliases.setdefault(title, union)
            return title
        return union
    if "anyOf" in schema:
        return " | ".join(_ts_type(member, aliases=aliases) for member in schema["anyOf"])
    kind = schema.get("type")
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        return f"Array<{_ts_type(schema.get('items', {}), aliases=aliases)}>"
    raise ValueError(f"unsupported schema fragment (extend the generator, do not hand-write): {schema}")


def _doc_comment(schema: dict[str, Any]) -> list[str]:
    description = schema.get("description")
    if not isinstance(description, str) or not description.strip():
        return []
    lines = ["/**"]
    for line in description.strip().splitlines():
        lines.append(f" * {line}".rstrip())
    lines.append(" */")
    return lines


def _interface(name: str, schema: dict[str, Any], *, aliases: dict[str, str]) -> str:
    required = set(schema.get("required", []))
    lines = _doc_comment(schema)
    lines.append(f"export interface {name} {{")
    for prop_name, prop in schema.get("properties", {}).items():
        optional = "" if prop_name in required else "?"
        lines.append(f"  {prop_name}{optional}: {_ts_type(prop, aliases=aliases)};")
    lines.append("}")
    return "\n".join(lines)


def emit(model_list: list[type]) -> str:
    chunks = [HEADER]
    for model in model_list:
        schema = model.model_json_schema()
        aliases: dict[str, str] = {}
        definitions = schema.get("$defs", {})
        bodies = [
            _interface(name, definitions[name], aliases=aliases)
            for name in sorted(definitions)
        ]
        root = _interface(model.__name__, schema, aliases=aliases)
        for alias, union in aliases.items():
            chunks.append(f"export type {alias} = {union};\n")
        chunks.extend(body + "\n" for body in bodies)
        chunks.append(root + "\n")
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="只校验已提交文件是否最新")
    args = parser.parse_args(argv)
    fresh = emit(models())
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if current == fresh:
            print(f"up to date: {OUTPUT.relative_to(PLATFORM_DIR)}")
            return 0
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                fresh.splitlines(keepends=True),
                fromfile="committed",
                tofile="generated",
            )
        )
        print(f"\nstale: {OUTPUT.relative_to(PLATFORM_DIR)} — regenerate it", file=sys.stderr)
        return 2
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(fresh, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(PLATFORM_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
