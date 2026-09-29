"""节点专属工具注册入口（bootstrap 启动时自动 import）。

判决拆除 B 刀之后 postprocess 的工具面只剩两个入口（见 figure.py）：

- `render_figure` —— agent 在沙箱写 matplotlib，框架机械录入出处绑定
  （数据/代码/输出三重 hash）、恒跑图像级机械审计、VLM 在场则跑证人，
  铸唯一的 `figure` 记录；
- `inspect_figure` —— 按需 VLM 观察（仅当平台配置 visual_review 角色时
  出现在工具面上）。

图型领域知识活在 nodes/postprocess/skills/（generic `list_skills` /
`load_skill` 工具可读）；沙箱执行复用 shared/tools/library/python_exec。
"""

from . import figure  # noqa: F401
