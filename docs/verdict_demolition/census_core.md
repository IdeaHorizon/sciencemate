# 普查：core/（2026-08-31）

计数（真判决点）：A≈117（含 sandbox.py ~99 沙箱墙）/ B≈21 / C≈180 / **D≈45**。
D 集中于：obligations(7)、tasks(7)、closure(5)、verdict_authority(4)、
prereg_commitments(4)、memory(4)、executor(3)、kb_promotion(3)、dispatch_gate(2)。

## D 类逐条（裁决对象）

### 派发闸 / 收尾闸
| file:line | 强制的要求 |
|---|---|
| core/dispatch_gate.py:202 | 上游产物指纹与 node_inputs 指纹都逐字节没变 → 拒绝再次派发该节点 |
| core/dispatch_gate.py:210 | 同一判决的拒绝文案渲染 |
| core/closure.py:123 | producing 子 run 的 status 必须逐字等于 completed 才算闭环 |
| core/closure.py:231 | 五类来源任一非空即"有未闭环工作" |
| core/closure.py:448 | 条目还挂在 pending_post_node_flow 里就算 review→decision flow 没走完 |
| core/closure.py:465 | 项目 TaskList 里任何 pending/in_progress/blocked 的 task 都算未闭环 |
| core/closure.py:490 | 未了结 blocking 义务计入未闭环 |
| core/executor.py:1337 | required_outputs_for 里任一 artifact type 没产出 → run 判 incomplete |
| core/executor.py:1358 | workspace 模式下自己 Git 作用域零未提交改动 → completed 降级 incomplete |
| core/executor.py:1390 | closure 判定 downgrades_status → summary.json 的 completed 降级 incomplete |

### 义务闸
| file:line | 强制的要求 |
|---|---|
| core/obligations.py:882 | 还挂着任一 blocking 义务就不许报 complete |
| core/obligations.py:72 | 被点名上游节点在申诉之后没有成功跑过 → blocking 义务 |
| core/obligations.py:113 | 同一 producing 节点连挂 ≥2 次同组 check → blocking 义务 + 禁止原样重启 |
| core/obligations.py:148 | 冻结预注册里的 metric 没测 → 义务（blocking=False 只渲染） |
| core/obligations.py:334 | experiment 跑出需裁决结果而 Analysis 没看过 → blocking 义务 |
| core/obligations.py:543 | 论文审过了却没冻结 → blocking 义务 |
| core/obligations.py:742 | 预注册声明的设计没人交代兑现 → blocking 义务 |

### 裁决权 / 预注册闸
| file:line | 强制的要求 |
|---|---|
| core/verdict_authority.py:137 | 没绑 worktree 读不到 research_state → 不许翻 hypothesis claim 状态 |
| core/verdict_authority.py:154 | 已有 research_state 时翻状态必须指名 hypothesis_id |
| core/verdict_authority.py:161 | research_state 里没这条假说 → 不许翻 |
| core/verdict_authority.py:170 | research_state 记的状态与要翻方向不一致 → 不许翻（取证者不裁决自己的取证） |
| core/prereg_commitments.py:816 | 有冻结预注册时翻 hypothesis 状态必须带 hypothesis_id |
| core/prereg_commitments.py:825 | hypothesis_id 不在冻结预注册里 → 只能停 provisional |
| core/prereg_commitments.py:835 | 闭合条件合取，任一条没合格兑现记录 → 不许判 validated/refuted |
| core/prereg_commitments.py:541 | 一条闭合条件都没写的研究问题 → 冻结闸拒绝 |

### 晋升 / 审批 / 阈值
| file:line | 强制的要求 |
|---|---|
| core/kb_promotion.py:487 | 候选任一 Check 不过就不许晋升进 org |
| core/kb_promotion.py:175 | 项目无已冻结 manuscript/证据记录或有未闭环 flow → 不许晋升 |
| core/kb_promotion.py:231 | 知识卡字段不齐/缺 applicability/正文含项目指代 → 不许晋升 |
| core/domain_registry.py:573 | 注册新叶必须带非空 approved_by（人批背书） |
| core/tool_registry.py:215 | 工具同名冲突必须在 framework_exemptions.yaml 登记豁免 |
| core/session_driver.py:236 | 有未答复的 pause 时不许开新一轮 |

### TaskList 状态机 / 内容阈值
| file:line | 强制的要求 |
|---|---|
| core/tasks.py:154 | task title 不能为空 |
| core/tasks.py:160 | parent 已 completed 不能加子 task |
| core/tasks.py:183 | 已 completed 的 task 不能 re-start |
| core/tasks.py:185 | blocked 的 task 必须先 unblock 才能 start |
| core/tasks.py:193 | 同 owner_node 同时最多 1 个 in_progress |
| core/tasks.py:221 | block reason 必须 ≥8 字符 |
| core/tasks.py:223 | 已 completed 的 task 不能 block |
| core/state.py:1551 | claim status 转换必须落在 _CLAIM_STATUS_TRANSITIONS 边上 |
| core/state.py:725 | memory text 不能为空 |
| core/state.py:1555 | status 转换必须带 ≥10 字符 reasoning |
| core/memory.py:327 | 铁律正文至少 MIN_ENTRY_CHARS |
| core/memory.py:527 | 手册条目正文至少 MIN_ENTRY_CHARS |
| core/memory.py:533 | memory_note 必须给 applies_to |
| core/memory.py:539 | memory_note 必须给 evidence |
| core/memory_forget.py:191 | 合并后正文至少 MIN_ENTRY_CHARS |
| core/memory_forget.py:214 | 同节至少匹配 2 条才允许合并 |
| core/whiteboard.py:98 | 白板 content 不能为空 |
| core/org_canon.py:168 | 活综述正文不能为空 |

## 骑墙判例（普查时的难判记录）
1. tool_registry.py:633/665 守卫自己炸了拒绝工具调用 → 判 B（证人不在场则无法如实报告越界）。
2. state.py:354 冻结产物必须带 amendment_reason 才能覆写 → 判 B（无 amend 行则版本账变假）。
3. state.py:1551 转换表判 D（转换进 revision_history 后账仍真），同块 reasoning≥10 判 D。
4. verdict_authority.py:137 「读不到 research_state」形似 C 实为 D 闸的 fail-closed 分支（职责分离规则）。
5. whiteboard.py:98 空内容判 D 与同函数容量超限判 C 并排，拆除需分开。

## obligations 机制现状（拆除的接收端）
Obligation{kind/what/owed_by/acceptance/blocking/...}；6 个 collector 从 run 历史现推，
无 open/discharged 状态机，清偿自动消失；消费者 3 处：render()（每轮给 orchestrator）、
terminal_block()（终态门禁）、closure（blocking 义务并入 OpenWork）。加义务 = 加 collector。
