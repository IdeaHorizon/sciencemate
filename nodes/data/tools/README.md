# nodes/<my_node>/tools/

## Data planning and execution

- `analyze_preprocessing_requirements`: extract stages from a caller request and optional upstream contract,
  derive required files, and request an exact official-source asset search when uncertain. It does not
  formulate a research plan.
- Internal Designer/Critic functions produce and review the versioned plan behind
  `run_preprocessing_planning_loop`; they are not separately registered tools.
- Runtime environments are prepared by Experiment. Data invokes only registered preprocessing tools and
  reports a missing Python package, executable, compiler, or licensed runtime as `externally_blocked`.
- `execute_preprocessing_plan`: execute the approved dependency graph and persist step results.
- `recover_atomic_structure`: recover CIF/POSCAR from local assets, repositories, papers, or supporting
  information; reconstruct only from complete crystallographic coordinates, otherwise return
  `externally_blocked`.

Application-specific file sets are inferred per task and stored in RequirementAnalysis; they are not
implemented as one skill per simulation application.

## Single package publisher

All generators, converters, and reviewers write only below the current run's
`.data_node_work/`. `package_publisher.py` is the sole owner of the visible
`data_preprocessing/<delivery-name>__<run-id>/` path. Request identity, spec hash, and parameters remain in
`manifest.json`. The publisher promotes a reviewed staging tree atomically;
review failure removes staging and retains only a compact run-level `audit/preprocessing_review.json`.
Do not add direct writes, copies, or mesh case paths targeting the final package from another tool.

## Representation-driven assets

`scientific_assets.py` is the discipline-neutral entry for local and downloaded data. It classifies by
content signature and representation (`table`, `tensor`, `spatial_field`, `mesh`, `particle_structure`,
`graph`, `spectrum`, `simulation_case`, or `multimodal_bundle`) and records minimum readiness gates.
Add a new file standard to this registry when possible; do not add a weather, computer-science, or other
application-specific route merely because a new task uses that format.

Approved downloads are tagged as `dataset`, `parameters`, `archive`, `structure`, or
`geometry_or_mesh`. The execution layer persists every typed downloaded asset across replanning while the
unified geometry path serves existing mesh workflows.
Public discovery and acquisition are separate: `data_web_search` may inspect leads within the approved
objective, while only `data_web_download` may materialize a selected URL and promote its hash/provenance
into the reference ledger.

节点专属工具放这里。`core/bootstrap.py` 导入包级 `__init__.py`；大型领域实现应通过轻量代理
按需加载，不应为了注册一个兼容工具而在每次节点启动时全部 import。

**完整规范见 [`docs/tool-spec.md`](../../../docs/tool-spec.md)** —— 接口契约 / step-by-step / MCP 接入 / 完整 dummy 例子。这里只是 quick reference。

## 加新工具（3 步）

**1. 写工具文件**（建议每组功能一个文件）：

```bash
cp templates/tool.py.template nodes/<my_node>/tools/my_feature.py
# 编辑：实现 async def _my_feature(state, ...) -> dict + register_tool(...)
```

**2. 在 `__init__.py` 注册公开工具**：

```python
# nodes/<my_node>/tools/__init__.py
from . import my_feature   # noqa: F401
```

大型或低频领域工具应参考 `lazy_domain_tools.py`，只为统一公开能力按需加载实现。

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

## Data 节点网格工具分层

`harness.yaml` 只公开一个网格工具：`prepare_scientific_mesh`。

- `operation=prepare`：自动识别学科，完成参数路由、生成和审核。
- `operation=resolve`：处理缺参、公开资料检索结果和 HITL 分流。
- `operation=build_profile`：把多个二维坐标段合成为闭合 profile。
- `operation=generate`：在参数已经完整时直接调用底层生成器。
- `operation=convert`：把已有 Gmsh/几何网格资产转换为 OpenFOAM `polyMesh` 并复核。

`cfd_case_router.py`、`mesh_iteration_advisor.py`、`coordinate_profile.py` 和
`mesh_generator.py` 是内部实现层。旧的公开工具别名已删除，不进入计划能力清单。CFD、结构力学、
计算电磁、传热、MD 和多物理场均从
`prepare_scientific_mesh` 进入，避免模型在多个相近工具之间错误路由。

## 为啥这个目录里有 `__pycache__/`

Python 自动生成的字节码缓存，已经在 `.gitignore` 里。可以忽略。
