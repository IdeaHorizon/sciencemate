# nodes/<my_node>/tools/

节点专属工具放这里。这个目录由 `core/bootstrap.py` 自动 import（如果 `__init__.py` 存在）。

**完整规范见 [`docs/tool-spec.md`](../../../docs/tool-spec.md)** —— 接口契约 / step-by-step / MCP 接入 / 完整 dummy 例子。这里只是 quick reference。

## 加新工具（3 步）

**1. 写工具文件**（建议每组功能一个文件）：

```bash
cp templates/tool.py.template nodes/<my_node>/tools/my_feature.py
# 编辑：实现 async def _my_feature(state, ...) -> dict + register_tool(...)
```

**2. 在 `__init__.py` import** —— 触发 register：

```python
# nodes/<my_node>/tools/__init__.py
from . import my_feature   # noqa: F401
```

**3. 在 harness.yaml 的 tools 白名单加工具名**：

```yaml
tools:
  - my_feature_tool_name   # 跟 register_tool 时的 name= 一致
```

## 引用 shared library 里的现成工具

`shared/tools/library/*.py` 里有 KB / proposals / profile / freeze_artifact / latex 等 opt-in 工具。
节点要用就在 `__init__.py` import 对应模块即可：

```python
from shared.tools.library import kb              # noqa: F401  KB CRUD
from shared.tools.library import proposals       # noqa: F401  propose/list/resolve
from shared.tools.library import profile_tools   # noqa: F401  read_profile/propose_profile_update
from shared.tools.library import artifacts_extra # noqa: F401  freeze_artifact
from shared.tools.library import latex           # noqa: F401  compile_latex
from shared.tools.library import skill_tools     # noqa: F401  skill 维护
```

然后在 harness.yaml tools 白名单加具体工具名。

## 工具实现纪律

- `async def`，第一个 kwarg `state: State`
- 永远返 `{"status": "success" | "error" | "pause", ...}` dict，**永不抛异常**
- 框架自动注入 `state`，其它参数从 harness.yaml 的 `parameters_schema` 里 LLM 传
- CPU 密集放 `await asyncio.to_thread(...)`

详见 `templates/tool.py.template`。

## 为啥这个目录里有 `__pycache__/`

Python 自动生成的字节码缓存，已经在 `.gitignore` 里。可以忽略。
