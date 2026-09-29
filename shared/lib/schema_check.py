"""一份声明式 schema、一次通用校验（判决拆除第三波·B 组）。

kb_schema 的四个 validate_*（27 处手写 raise）与 publication_figures 的图记录形状
检查（7 处）说的是同一件事：记录必须长这样。契约声明一次、核一次；这里是那
「一次」。刻意小：JSON-schema 的一个子集 —— type / required / enum / minimum /
maximum / minLength / maxLength / pattern / minItems / items / properties，递归。
不支持 oneOf / if-then（条件必填由调用方用一张小表声明，见 kb_schema）。
没有第三方依赖。

`minLength: 1` 按「非空」核（去空白），与 core/tool_registry 派发口口径一致。
返回人话错误清单（空 = 合规），每条都带字段路径，调用方决定怎么报。
"""
from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "number": (int, float), "integer": (int,), "boolean": (bool,),
    "object": (dict,), "array": (list, tuple), "null": (type(None),),
}


def _type_ok(value: Any, declared: str | list) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        if isinstance(value, bool) and name in ("number", "integer"):
            continue                       # bool 是 int 的子类，没人 declare integer 想要 True
        if isinstance(value, _TYPES.get(name, ())):
            return True
    return False


def schema_errors(schema: dict, value: Any, path: str = "") -> list[str]:
    """按 `schema` 核 `value`；返回违规清单（空 = 合规）。"""
    out: list[str] = []
    here = path or "记录"
    if "type" in schema and not _type_ok(value, schema["type"]):
        return [f"{here} 类型不对：须是 {schema['type']}，收到 {type(value).__name__}"]
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if value.get(name) is None:
                out.append(f"{path + '.' if path else ''}{name} 必填")
        for name, sub in (schema.get("properties") or {}).items():
            if value.get(name) is not None:
                out += schema_errors(sub, value[name], f"{path}.{name}" if path else name)
        return out
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{here}={value!r} 不合法。必须 ∈ {tuple(schema['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{here}={value!r} 小于最小值 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{here}={value!r} 大于最大值 {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value.strip() if schema["minLength"] == 1 else value) < schema["minLength"]:
            out.append(f"{here} 不能为空" if schema["minLength"] == 1
                       else f"{here} 至少 {schema['minLength']} 个字符")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{here} 超过 {schema['maxLength']} 个字符")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{here}={value[:80]!r} 不匹配 {schema['pattern']!r}")
    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{here} 至少 {schema['minItems']} 项（给了 {len(value)} 项）")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                out += schema_errors(schema["items"], item, f"{here}[{i}]")
    return out
