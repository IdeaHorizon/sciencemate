# writing 节点

用户要什么体裁，就交付那个体裁的成品（PDF + LaTeX 源 + 作者备注）。方案与四条不变量见
docs/WRITING_NODE_REBUILD_PLAN_20260918.md；工具面见 tools/README.md；体裁包在 genres/，
写作技艺在 craft/，渲染模板在 renderers/。测试 tests/test_w_rebuild.py 的样本是夹具真跑出来的成品。

夹具运行器：`scripts/writing_fixture.py --fixture <夹具目录> --inputs user --project-id iterN`（夹具数据不进仓库）。

研究论文用 `sci_article_zh`，逐项回顾项目可用 `project_summary`，共用现有渲染与验收路径。
简报保留原写作请求和用户明确的 `required_questions`；提纲可用 `editorial_focus` 记录主张、
必要子问及证据去向，`question_coverage` 指向必答问题的实际部件。选材不改变研究状态或项目原件，
旧简报/提纲不补新字段也可继续起草。图按比较目标与读者所得信息安排，证明或反例不必配图。
