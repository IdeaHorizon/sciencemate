# 生产开火数据（2026-08-01 起，200 run / 6,456 次工具调用 / 441 次拒绝）

## 开火签名榜（前列）

| 次数 | 签名 | 普查分类 |
|---|---|---|
| 45 | [blocked]（空 reason） | 待细分 |
| 42 | 命令退出码 N | C |
| **28** | **writing-gate：还没有 project_synthesis 评估，不能起 writing** | **D 顺序闸** |
| **27** | **mechanical validation must pass before VLM review** | **D 顺序闸** |
| 25 | HTTP N | C |
| 15 | observation/experiment log cannot be frozen before gates pass | D 审批链 |
| 6 | ModelRoleUnavailable visual_review | 配置缺席（qinp 事故） |
| 6 | falsifiability 分值不是数字 'N/A' | C/D 边界 |
| 5 | 矩阵必须完整；无证据格显式 verdict=missing | D 清单闸 |
| 5 | scope_conflicts_with_bound_prereg | D prereg |
| 4 | LaTeX 版面审计失败未提升为交付物 | D |
| 3 | prereg 可行性门 / 正文含项目内指代 / stage=simulation 声明 | D |
| ~10 | 「文件已存在但本次 run 未 read 过」 | A（防盲写） |

**620 堵 D 墙中 30 天开过火的不足 30 堵——约 95% 为零命中死重。**

## 命中后果取证（撞墙之后 agent 干了什么）

### writing-gate（28 次命中，其中 24 次集中在同一个 run！）
后续模式：`run_node > run_node > run_node`（14 次）等——**单 run 连撞 24 次，
典型空转**。仅 1 次走了 present_writing_gate_override → request_human_input。
判读：**骚扰墙**。补救（跑 _reviewer 拿 synthesis）名义上在 agent 力所能及内，
但实际行为是反复重试派发——墙文案没能转化为有效行动。

### mechanical-before-VLM（27 次命中，单 run 最多 11 次）
后续模式：10 次直接 run 结束、8 次 report_blocker、其余 list_files/memory_note
调查——**几乎零合规成功，全是死路或空转**。判读：**死路墙**（当机械校验失败
的根因不在 agent 力内时——比如当时的锚点 bug——这道顺序闸把 agent 锁死）。

### freeze gates（15 次命中，14 个 run 各命中 1 次）
后续模式：`save_artifact > freeze_artifact`（补上缺的 log 再冻结）——**命中一次、
照文案修复、成功前进**。判读：**有效检查**——但注意：起作用的是它的检查+指路
文案，不是"拒绝"本身；转义务后同样的文案照样指路，且不再有锁死风险。

### 矩阵完整性（5 次）
后续 `execute_python > save_artifact`——合规重算。同 freeze gates 判读。

## 对裁决的校准结论

区分墙的不是意图而是**补救是否在 agent 力所能及内且文案指路是否具体**：
- 补救在力内 + 指路具体 → agent 合规（freeze/矩阵）——这类转义务零损失；
- 补救不在力内（依赖其他节点/配置/用户）→ 空转或死路（writing-gate、
  mech-before-VLM、ModelRoleUnavailable）——这类墙必须有让步出口，否则
  就是 qinp 事故的批量复制。
