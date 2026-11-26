# writing 节点测试

`test_w_rebuild.py`：重建后节点的机械判据，每条对应夹具里一轮真跑出来的缺陷；样本在 fixtures/rebuild/（iter9 成品）。
`test_compile_latex_tool.py`：shared/tools/library/latex.py 的编译工具（writing 的渲染器调它）。
`test_argument_focus.py`：在私有材料副本上走真实简报、提纲、起草、派图和既有渲染入口；只替换模型与子节点执行，核对论文/项目总结的输入差异、明确多问、证据去向、原件字节不变及旧稿兼容。
