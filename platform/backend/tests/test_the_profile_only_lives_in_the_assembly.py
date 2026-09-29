"""档位只在装配处生效（RFC_RESEARCH_BUDDY §3 R2）。

## 为什么这是一道扫盘闸而不是一条约定

一套代码要能起成两种形态。做法有两种：业务代码到处 `if profile == "personal"`，
或者业务代码根本不知道有档位这回事、只看自己被装配成了什么样。

第一种每加一句就多一处分叉，而**没有任何一层能发现它分叉了** —— 两种档位各自
能跑，差异只在某个具体请求上显形。本仓库为同一形状的问题付过很多次学费
（同一个问题几处各答、跨节点词表静默分叉）。

所以：允许读 `settings.profile` 的只有两个文件。`config.py` 算默认值（数据根、
库地址必须在任何代码跑起来之前就有答案），`assembly.py` 接线。别处一处都不许。
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: 例外：加一条要写清楚为什么这一处必须自己知道档位，以及为什么装配层做不了。
ALLOWED = {"config.py", "assembly.py"}


def _reads_the_profile(source: str) -> list[int]:
    """`settings.profile` / `Settings.profile` 的读取位置（行号）。

    扫的是属性访问，不是字符串 —— 判据写成 `"profile" in source` 会被一句
    注释绊倒，也会漏掉 `getattr(settings, "profile")` 之外的每一种写法。
    """
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "profile":
            base = node.value
            name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            if name in {"settings", "Settings", "self"}:
                hits.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr" and len(node.args) >= 2:
            key = node.args[1]
            if isinstance(key, ast.Constant) and key.value == "profile":
                hits.append(node.lineno)
    return hits


def test_only_configuration_and_assembly_know_about_the_profile() -> None:
    offenders: dict[str, list[int]] = {}
    for path in sorted(APP.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        lines = _reads_the_profile(path.read_text(encoding="utf-8"))
        if lines:
            offenders[str(path.relative_to(APP))] = lines
    assert not offenders, (
        f"这些地方自己读了档位：{offenders}。业务代码只看自己被装配成了什么样"
        "（鉴权依赖是谁、后台任务起没起、数据根在哪），不看档位名字。"
        "要按档位分叉，请在 app/assembly.py 里接线。"
    )


def test_the_assembly_is_the_one_that_wires_authentication() -> None:
    """装配层必须真的在接线 —— 一个空壳装配层同样能让上面那条闸全绿。"""
    source = (APP / "assembly.py").read_text(encoding="utf-8")
    assert "dependency_overrides" in source, (
        "个人档的鉴权靠依赖覆盖装上；装配层里找不到它，说明接线在别处"
    )
    assert "implicit_local_user" in source
