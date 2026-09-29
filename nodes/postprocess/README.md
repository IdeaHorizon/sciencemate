# Scientific Visualization node

`postprocess` 是保留兼容名称的 Scientific Visualization 服务。它负责 Workflow
中全部科学图片与可视化，不解析实验日志，也不生成 `clean_results`。

判决拆除 B 刀（2026-09-01，docs/verdict_demolition/FIGURE_SUBSYSTEM_REBUILD.md）
之后的形状：**科学家出图 = 数据 → 画图代码 → 自己看/证人看 → 改 → 交审稿人**。

## 边界

Experiment 负责日志解析、清洗、聚合、异常检测、统计推断和 `clean_results`。
本节点只读取上游 artifact，自己写渲染代码，在沙箱执行，检查、按需视觉
review，并输出唯一完成契约：`figure` 记录。禁止隐式清洗、聚合、平滑、插值、
异常值删除或不确定性计算；允许的显示变换必须写在渲染代码里（referee 会重跑）。

## 执行模型

1. agent 用 `execute_python` 在沙箱里迭代画图（自由文件，不落账）；
2. 图型/排版拿不准时读 `nodes/postprocess/skills/` 下的图型家族 skill
   （generic `list_skills` / `load_skill`）；
3. 定稿调用 `render_figure`：沙箱执行最终代码、逐字节冻结脚本、机械录入
   出处绑定（源数据 artifact hash ↔ 渲染代码 hash ↔ 输出文件 hash）、恒跑
   图像级机械审计（文字出界/碰撞/分辨率/豆腐块/文件有效性）、平台配置
   `visual_review` 角色时跑一次 VLM 证人 —— 全部进记录的 findings；
4. `inspect_figure` 可按需再请 VLM 看一眼（角色缺席时该工具不出现）。

没有 status/quality_mode/verdict：证据可持久化，判决不可以。改不改归 agent，
终审归 referee 与用户。

## 防伪（不可压缩核 #2）

`figure` 记录只能在可视化属主节点铸出（produced_by_node_type 由框架盖章）；
`metadata.figure_hash` 由消费端（`shared/lib/publication_figures.py`）对出处
绑定核心逐字节重算；输出文件必须由铸记录那次沙箱执行真实写出。writing 端
staging 再逐文件核对交付字节指纹。

## Reviewer（VLM 证人）

见 `vlm_witness.py` 与 `reviewer/rubrics/`。谁来当审图模型是平台配置
（模型角色 `visual_review`），协议/重试/复现规则归节点 owner。VLM 只报告
可见现象；rubric 映射与严重度由确定性代码标注，观察进 findings —— 它不是
盖章岗。

review 细则见 [review_spec.md](review_spec.md)。
