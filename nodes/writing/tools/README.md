# writing 工具面

| 工具 | 做什么 | 机械闸 |
|---|---|---|
| write_brief | 登记体裁、标题、语言政策、用户硬性要求（must_haves） | 规则语法；重命名类必须 forbid；用户原话有编号要求时不能为空 |
| read_dossier | 读卷宗（用户材料、数据表、图文件、参考文献） | 文档与图编号按路径持久化 |
| write_outline | 论证提纲 | 主张必须有卷宗证据；图来源必须存在；图内文字预扫 forbid |
| read_bibliography / write_bibliography | BibTeX，DOI/arXiv 联网核 | 每条可解析 |
| draft_section / revise_section | 一节一次有界 LLM 调用，正文落盘 | 收据：过程词汇、未知 cite key、未知图文件、未定义 label、图键↔文件 |
| request_figures | 派图给图表服务，按简报灌入语言/宽度/字号/格式/禁用词/重命名 | 示意图必须有 spec |
| render_manuscript | 装配模板 + xelatex/biber，抽页，12 条硬线 H1–H12 | 见 w_render.hard_lines |
| read_pages | 读渲染出的逐页文字 | — |
| referee_review | 独立审读官读 PDF 文字按体裁 rubric 出报告，≤3 轮 | 结论绑定 PDF 指纹 |
| write_author_notes | 作者备注（不进 PDF） | 六个小节必须都有 |
| submit_manuscript | 登记交付物 | 硬线全过、审读结论针对当前 PDF、有备注 |
