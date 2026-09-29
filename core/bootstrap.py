"""框架启动时的工具 / skill 自动注册。

调用 `bootstrap()` 一次（runner / chat.py 启动时），它会：
  1. import `shared.tools`（注册全部共享工具）
  2. import `shared.skills`（注册全部共享 skill）
  3. 遍历 `nodes/*/tools/` 和 `nodes/*/skills/`，import 存在的子包（注册节点专属工具/skill）

这样 owner 加新工具只需要在自己节点目录下写代码 + 在节点的 __init__.py 里
追加 import 行，不用动框架。

⚠️ 节点专属的工具如果设置了 `allowed_node_types=[<本节点>]`，那么 LLM 在别的
   节点 yaml 的 tools 白名单里写它也调不到 —— tool_registry.list_tools_for_node
   会按 allowed_node_types 拦截。
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

log = logging.getLogger("bootstrap")

NODES_DIR = Path(__file__).parent.parent / "nodes"

_bootstrapped = False

#: bootstrap 期间没 import 起来的节点模块：{node: {module: "错误摘要"}}。
#:
#: 为什么要记账而不是只 log.warning（2026-08-21）：
#: `nodes/hypothesis/tools/artifact_save.py` 里一个 py3.12-only 的 f-string 在
#: CI 的 py3.11 上是 SyntaxError —— 整个 `nodes.hypothesis.tools` 和
#: `nodes.hypothesis.hooks` 都没 import 成功，于是 hypothesis 节点**一件工具都没注册**。
#: 而这件事在日志里只是两行 WARNING，节点照常启动、照常跑，模型对着 harness.yaml
#: 里点名的 11 个工具（audit_* 全套 / get_research_goal / update_research_state /
#: validate_hypothesis_outputs）一个都调不到 —— 正是"prompt 点名了不存在的工具，
#: 模型会照着去找、找不到、然后跳过那一步"。
#:
#: 失效方向必须翻转：一个节点的代码没加载起来，它就**不是一个能跑的节点**，
#: 而不是"一个少了点东西的节点"。别的节点照常 boot（一个节点坏不该拖垮全框架）。
_node_import_failures: dict[str, dict[str, str]] = {}


def node_import_failures(node: str | None = None) -> dict[str, dict[str, str]] | dict[str, str]:
    """bootstrap 期间的节点模块 import 失败账本。

    不传 node 返回全部；传 node 返回该节点的 {module: error}（没有就是空 dict）。
    """
    if node is None:
        return {k: dict(v) for k, v in _node_import_failures.items()}
    return dict(_node_import_failures.get(node, {}))


def _record_node_import_failure(node: str, module: str, exc: BaseException) -> None:
    """记下来，并且照旧 log —— 日志给人看，账本给机制用。"""
    _node_import_failures.setdefault(node, {})[module] = f"{type(exc).__name__}: {exc}"
    log.warning("%s import 失败：%s", module, exc)


def bootstrap(force: bool = False) -> None:
    """注册全部工具 + skill。重复调安全（用 flag 防止重复 import 噪音）。

    Skills 改成 folder/SKILL.md 形态（v2.1 重构）后，加载流程：
      1. shared.tools 包（工具自注册）
      2. nodes/<node>/tools/ 包（节点专属工具自注册）
      3. nodes/<node>/summarizer.py（节点专属 summarizer）
      4. nodes/<node>/hooks.py（节点专属 loop hook，v2.1+）
      5. shared/skills/<name>/SKILL.md（framework-shipped）
      6. nodes/<node>/skills/<name>/SKILL.md（节点本地）
      7. $HARNESS_FRAMEWORK_HOME/org/skills/<name>/SKILL.md（导入/自发现）

    自留地原则：owner 只动 nodes/<my_node>/ 下的：
      - harness.yaml（system_prompt / 工具白名单 / context_config / hooks 参数）
      - tools/<*>.py + tools/__init__.py（节点专属工具）
      - skills/<*>/SKILL.md（节点专属 skill）
      - summarizer.py（节点专属 summarizer 覆盖）
      - hooks.py（节点专属 loop hook 注册）
      - fixtures/<*>.yaml（本地测试 fixture）
    框架代码（core/、shared/）不动。
    """
    global _bootstrapped
    if _bootstrapped and not force:
        return
    # 重跑就重记：留着上一轮的账会让"已经修好了"仍然被判死。
    _node_import_failures.clear()

    # 1. 共享工具池
    try:
        importlib.import_module("shared.tools")
    except Exception as e:
        log.warning("shared.tools import 失败：%s", e)

    # 2. 节点专属 tools（先于 skills 加载，因为有些 tools 注册涉及 skill）
    if NODES_DIR.exists():
        for node_path in sorted(NODES_DIR.iterdir()):
            if not node_path.is_dir():
                continue
            node = node_path.name
            init = node_path / "tools" / "__init__.py"
            if init.exists():
                mod = f"nodes.{node}.tools"
                try:
                    importlib.import_module(mod)
                except Exception as e:
                    _record_node_import_failure(node, mod, e)
            # summarizer
            summ = node_path / "summarizer.py"
            if summ.exists():
                mod = f"nodes.{node}.summarizer"
                try:
                    importlib.import_module(mod)
                except Exception as e:
                    _record_node_import_failure(node, mod, e)
            # 节点专属 loop hooks（owner 在自己目录写自己的 hook）
            hooks = node_path / "hooks.py"
            if hooks.exists():
                mod = f"nodes.{node}.hooks"
                try:
                    importlib.import_module(mod)
                except Exception as e:
                    _record_node_import_failure(node, mod, e)
            # 节点专属 custom agent_loop（opt-in override，详见 templates/agent_loop.py.template）
            custom_loop = node_path / "agent_loop.py"
            if custom_loop.exists():
                mod = f"nodes.{node}.agent_loop"
                try:
                    importlib.import_module(mod)
                    log.warning(
                        "node %s uses CUSTOM agent_loop (减弱 framework 保证；"
                        "见 templates/agent_loop.py.template)", node,
                    )
                except Exception as e:
                    _record_node_import_failure(node, mod, e)

    # 3. 加载所有 SKILL.md folder（shared + nodes + org）
    try:
        from core.skill_loader import load_all_skills
        load_all_skills()
    except Exception as e:
        log.warning("skill_loader 加载失败：%s", e)

    _bootstrapped = True


def list_known_nodes() -> list[str]:
    """返回所有有 harness.yaml 的节点名（按字母序）。"""
    if not NODES_DIR.exists():
        return []
    return sorted(
        p.name for p in NODES_DIR.iterdir()
        if p.is_dir() and (p / "harness.yaml").exists()
    )
