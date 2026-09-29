# writing 节点

用户要什么体裁，就交付那个体裁的成品（PDF + LaTeX 源 + 作者备注）。方案与四条不变量见
docs/WRITING_NODE_REBUILD_PLAN_20260918.md；工具面见 tools/README.md；体裁包在 genres/，
写作技艺在 craft/，渲染模板在 renderers/。测试 tests/test_w_rebuild.py 的样本是夹具真跑出来的成品。

夹具运行器：`scripts/writing_fixture.py --fixture <夹具目录> --inputs user --project-id iterN`（夹具数据不进仓库）。
