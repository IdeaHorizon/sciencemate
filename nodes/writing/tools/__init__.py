"""writing 节点的工具面（重建后，2026-09-18；方案 docs/WRITING_NODE_REBUILD_PLAN_20260918.md）。

模型只写分节片段与几份声明文件；导言区、装配、编译、抽页、机械检查、派图政策归框架。
需要理解的事做成一次有界 LLM 调用（起草一节、审读一遍），主循环只装收据。

想加工具：抄 templates/tool.py.template 到 nodes/writing/tools/w_<feature>.py，在这里 import，
再在 harness.yaml 的 tools 白名单加名字。LaTeX 编译走 shared/tools/library/latex.py，这里不重复注册。
"""

from . import (  # noqa: F401
    w_bib,
    w_brief,
    w_deliver,
    w_dossier,
    w_draft,
    w_figures,
    w_referee,
    w_render,
)
