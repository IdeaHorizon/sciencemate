# Scientific Visualization tools

判决拆除 B 刀之后只有两个入口（figure.py）：

- `render_figure`: 定稿入口。执行 agent 的绘图代码（复用 execute_python 的
  强制沙箱）、冻结脚本、机械录入出处绑定（数据/代码/输出三重 hash）、恒跑
  图像级机械审计、VLM 在场则跑证人，铸唯一的 `figure` 记录。
  生成式像素必须声明 `evidence_bearing: false`，否则拒绝录入。
- `inspect_figure`: 按需 VLM 观察一张已铸 figure（证人，不铸记录、不给
  判决）。声明 `required_runtime_capability=model_role:visual_review` ——
  平台没配审图角色时压根不出现。

迭代画图用通用 `execute_python`；图型知识在 `../skills/`（`list_skills` /
`load_skill` 可读）。八种 typed 产物链（brief/source_profile/plan/derived/
validation_report/review/package/image_transform）与 DSL 强制通道已删除。
