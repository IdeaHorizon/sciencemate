# 判决书：shared/（62 条；删 20 / 降格 28 / 保留升格 12 / 呈裁 2）

## 判决表

### kb.py（13）
| file:line | 判决 | 义务组 | 理由 |
|---|---|---|---|
| 192 | 降格 | O1 出处与容器归属 | 角色事前审批（S3）；created_by_role 如实落盘账真、claim 可 supersede 可逆 |
| 264 | 降格 | O1 | 缺 hypothesis_id 只是退回按措辞寻址（注释自认）；如实标 unanchored（S2） |
| 286 | 降格 | O1 | 决定性事实：chunk 已带 origin_artifact_frozen/version/content_hash——出处已防篡改且如实，S1 不需要这道拒绝兜底 |
| 547 | **删** | — | 字数闸 ≥10 |
| 649 | 降格 | O2 判据证据不足 | 接错点：字数子句随 kb_schema:452 删，证据子句随 465 降格 |
| 676 | 降格 | O3 空转但无新信息判据 | 裸计数器：第 4 次翻转可能正是新实验刚出结果；review_history append-only 留痕 |
| 986 | **删** | — | 内部函数不在模型工具面上=对 agent 不可达的死重；已有 require_frozen=false 半开门 |
| 1262 | **删** | — | 字数闸 ≥30，且在提案通道上（人审就在下一步，S3） |
| 2028 | **删** | — | 字数闸 ≥10 |
| 2042 | 降格（子句删） | O1 | 「文献转述不起草卡」子句删（框架替人判信息量）；「empirical 须关联实验」降格 |
| 2057 | 降格 | O8 知识卡完备性 | 无 trigger 的死路卡是无效不是造假；标 dormant 更有信息量（S2） |
| 2067 | 降格 | O8 | 探测真（去项目化机械可判）但拦错位置：守的是起草，org 污染关口在晋升人批；且「上下文热时才写得出 why」与「当场要终态质量」自相矛盾 |
| 2084 | 保留 | — | 草稿位配额=注意力预算（上下文窗口真实有限，S6）；env 可调、出口一步可走 |

### run_node.py（14）
| file:line | 判决 | 义务组 | 理由 |
|---|---|---|---|
| 770 | 保留（改判 C） | — | 多候选时索取尚未做出的判断，非否决已做判断；附全候选+一步出口 |
| 919 | **升 A** | — | 双指纹逐字节比对任一变即放行=README 档三点名的算力熔断原型 |
| 1218 | 降格（收编） | O4 阶段欠账与范畴偏离 | 带 modality_rationale 即放行——**让步机制雏形**，只缺理由进永久账本+免一次往返 |
| 1313 | 降格（收编） | O4 | 同上；docstring 自己在讲 S3（「不拦死只要一句理由」「判断归模型」） |
| 1354 | 保留（改判 C） | — | user_note=非空必填参数，产品沟通契约非科学判决 |
| 1516 | **呈裁** | — | callable_nodes 白名单：能力域配置归 owner 还是死路墙？建议保留但补让步出口（越权照跑+记 unauthorized_callee+呈报） |
| 1605 | 保留 | — | 已是降格后的样子：一次性呈报账本、拦一次即放行；判据（双向 redirect）是客观无共识事实 |
| 1690 | 降格 | O5 发表状态披露 | **28 次命中/单 run 连撞 24 次/仅 1 次走 override**=骚扰墙；检查信息真（「从未做过综合评估」正是稿件必须披露的），拒绝死 |
| 1725 | 降格 | O5 | 同族；30 行外已有现成降格形态（conservative_with_limitations+局限披露）——把 override 分支变默认分支 |
| 1839 | 保留（改判 C） | — | background+post_run_flow:none 语义上无交货口=接口类型错误 |
| 2005 | 降格 | O4 | 预测失败正中靶心：子节点可产 gap report；收编成 1313 的形状 |
| 2035 | 保留 A（附修正） | — | 同一 flow 空转熔断（实测 11 圈 5.5h）；修正：action_last_failure 变则断链，否则是裸计数器 |
| 3649/3655 | **删 ×2** | — | 字数闸 ≥15 ×2 |

### skill_tools.py（8）
| file:line | 判决 | 义务组 | 理由 |
|---|---|---|---|
| 233 | 保留（改判 C） | — | 可见性=取数范围，报错列合法值+出口 |
| 467/553 | **删 ×2** | — | 字数闸，且 553 是 467 的重复抄件（转调连查两遍） |
| 667/669 | **删 ×2** | — | 字数闸（长度当思想成熟度代理） |
| 675/694/704 | 降格 ×3 | O9 提案证据强度 | propose 通道下一步就是人审 inbox；机械准入=剥夺同行裁量（S3）。704 降格比拒绝更强：unknown_tools 钉在提案上人审一眼可见 |

### artifacts_extra.py（4）
| file:line | 判决 | 义务组 | 理由 |
|---|---|---|---|
| 478/524 | 降格 ×2 | O6 类型门未过 | **判据的执行机构**：改这两处宿主=一次性把所有 typed save/freeze gate 转义务；fire_data 证转换零损失 |
| 580 | **呈裁** | — | 冻结不可逆+防「REVISE 洗成定稿」（B 边界）vs amendment 链仍在（降格）。裁决前置事实：冻结路径是否机械把未闭合 review_state 写进产物 metadata；没写之前保留 |
| 730 | 降格 | O7 预注册偏离申报 | 预测未来必然偏离而拒冻（档一预测失败+S4）；已有三选一半开门 |

### 其余 return 侧（15）
| file:line | 判决 | 理由 |
|---|---|---|
| builtin.py:313 | 降格→O1 | created_by 如实；实测命中后 agent 打算重跑已做完的实验=骚扰 |
| builtin.py:945 | 保留 | 无人值守无推荐=卡死（弱 A），出口具体在力内 |
| builtin.py:969 | 降格→O10 | 文本启发式替模型判「算不算推进」；探测价值有实证，检查存活拒绝死 |
| tasks.py:142 / proposals.py:185/524 / runtime_control.py:431 / cross_model.py:113 / profile_tools.py:116 / audit.py:59 | **删 ×7** | 字数闸全家 |
| proposals.py:245 | 降格（收编）→O3 | 有 new_evidence 判据的半开门；只删 ≥30 字数子句 |
| job_registry.py:35 | 保留 | expected_duration_s 有真实机械消费者（超时判据） |
| writing_gate.py:228/233 | **删 ×2** | 拦「向人提问」这个动作本身；233=只有同行已同意才准申请同行同意（循环授权）。1690/1725 降格后整个 override 工具冗余退场 |
| decision_package.py:1087 | **升 A** | 判据=在途状态非计数；重呈递把「已起了」抹成「没起」=账本损坏 |

### raise 侧（8）
| file:line | 判决 | 理由 |
|---|---|---|
| kb_schema.py:452 | **删** | 字数闸 ≥30 |
| kb_schema.py:465/474 | 降格 ×2→O2 | evidence_ids=[] 明写账不假；claims_only 如实标注（弱结论≠假记录，S2） |
| kb_schema.py:734 | 保留（改判 C） | synthesis 须 ≥2 来源=类型定义非阈值 |
| publication_figures.py:52/56/71/73 | 降格 ×4→O5 | 审批链（S3）；56 的理由「没做过审计」正是 S2 要写明的第三态；73 依赖 VLM 角色=qinp 那堵墙，必须有让步出口。本文件 hash/防伪 37 处 B 类真墙不动 |
| memory_tools.py:43 | **升 A** | 整节覆写不可逆抹掉别人教训；逐条追加出口在力内 |
| latex.py:514 | **删（最高优先级）** | **全域唯一主动销毁证据的墙**：`pdf_path.unlink()` 删刚编出的 PDF；layout_audit 已如实返回——检测本已诚实，拒绝只多做了毁尸；现状的不可逆在拒绝侧 |

## 收敛地图（10 组）
O1 出处与容器归属（kb 192/264/286/2042 半、builtin 313）→ provenance.unanchored/container_mismatch/qc_coverage:none ｜ O2 判据证据不足（kb 649、kb_schema 465/474）→ verdict_evidence: none|claims_only 进 KB 与局限节 ｜ O3 空转但无新信息判据（kb 676、proposals 245）→ thrash_signal 只记不拦 ｜ O4 阶段欠账与范畴偏离（run_node 1218/1313/2005）→ stage_debt/modality_deviation/missing_inputs+理由 ｜ O5 发表状态披露（run_node 1690/1725、pub_figures 52/56/71/73）→ publication_readiness 全部落局限节 ｜ O6 类型门未过宿主（artifacts_extra 478/524）→ gate_failures 进 metadata ｜ O7 预注册偏离申报（artifacts_extra 730）｜ O8 知识卡完备性（kb 2057/2067）→ 晋升人批检查清单 ｜ O9 提案证据强度（skill_tools 675/694/704）→ 提案上的 evidence_strength/unknown_tools ｜ O10 无人值守可执行性（builtin 969）→ stalling 信号。

## 熔断统一判据（裁定普查两刀不对称）
合法熔断（A）必须：(i) 数**同一信号重复**非次数；(ii) 局面变化能**机械断链**；(iii) 出口在力内且逐条写明。
→ run_node:919 升 A ｜ 1430 维持 A ｜ decision_package:1087 升 A ｜ run_node:2035 保留 A（补断链）｜ kb:676 降格（裸计数器）｜ proposals:245 降格收编（有 new_evidence 判据）。

## 半开门定性
1218/1313/245/730 不是待拆的墙，是**自发长出的让步机制原型**（「带理由即放行、出口永远可用、判断归模型」写在 docstring 里）；与目标形态只差：理由进永久账本/收尾清单/局限节 + 免强制往返。作为通用原语的参考实现。

## 呈裁清单
1. **run_node:1516 callable_nodes 白名单**：能力域配置归 owner（保留+让步出口）还是死路墙（删）？
2. **artifacts_extra:580 冻结顺序闸**：先补「未闭合 review_state 机械写进冻结件 metadata」，再降格；补上之前保留。

## 统计
删 20（17 条字数闸全灭+kb 986+writing_gate 228/233）/ 降格 28 → 10 组 / 保留升格 12（升 A 3：919、1087、memory_tools 43）/ 呈裁 2。改判 C 共 5 条（770/1354/1839/233/734）。
