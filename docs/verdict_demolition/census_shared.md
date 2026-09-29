# 普查：shared/（2026-08-31）

计数：348 拒绝点 → A=16 / B=22 / C=244 / **D=62**。

## D 类逐条（裁决对象）

### tools/library/kb.py（13）
| file:line | 强制的要求 |
|---|---|
| kb.py:192 | claim_type='hypothesis' 只有 _curator 能写入 KB |
| kb.py:264 | 立 hypothesis claim 必须显式传 hypothesis_id |
| kb.py:286 | 引用的 pre_registration chunk 必须 origin_artifact_frozen=True |
| kb.py:547 | status 转换 reasoning ≥10 字符 |
| kb.py:649 | validate_status_flip：reasoning ≥30 + validated/refuted 必须挂证据 |
| kb.py:676 | 同一 claim 单 session flip 次数达上限即拒（thrash 判定） |
| kb.py:986 | artifact 未 freeze 不许摘成 chunk（可显式 require_frozen=false 绕过） |
| kb.py:1262 | org promote 提案 reasoning ≥30 字符 |
| kb.py:2028 | 撤知识卡草稿必须给 reason ≥10 字符 |
| kb.py:2042 | 文献转述 claim 不许起草卡；自产 empirical 必须关联实验 |
| kb.py:2057 | dead_end 卡必须写 trigger |
| kb.py:2067 | check_deprojectified 未过（域/why/practice/confidence_basis 不齐）即拒 |
| kb.py:2084 | 项目草稿位配额（12）已满且不指定 discard_claim_id 就拒 |

### tools/run_node.py（14）
| file:line | 强制的要求 |
|---|---|
| run_node.py:770 | 同类型上游产物多版本候选 → 必须 forward_artifact_ids 指名 |
| run_node.py:919 | blocked_situation_unchanged：报过阻塞局面没变，不许再派同一节点 |
| run_node.py:1218 | 范畴闸：待闭合条目全是另一类，除非带 modality_rationale 否则拒 |
| run_node.py:1313 | data 阶段欠账未清，除非带 dataset_waiver_reason 否则拒 |
| run_node.py:1354 | 派发前必须带 user_note |
| run_node.py:1516 | 父 harness callable_nodes 白名单未授权该被调节点 |
| run_node.py:1605 | 两节点间 redirect 踢皮球，须人工仲裁归属后才准再派 |
| run_node.py:1690 | writing 门：没有 project_synthesis 评估不许起 writing |
| run_node.py:1725 | writing 门：最近 synthesis verdict != ready_to_write 即拒 |
| run_node.py:1839 | 服务节点不许 background 异步调起 |
| run_node.py:2005 | 缺 required_input_artifact_types 声明的上游产物类型即拒派发 |
| run_node.py:2035 | 同一 flow 起同一节点次数超 _MAX_ACTION_ATTEMPTS |
| run_node.py:3649 | request_upstream_fix 的 missing ≥15 字符 |
| run_node.py:3655 | request_upstream_fix 的 acceptance ≥15 字符 |

### tools/library/skill_tools.py（8）
| file:line | 强制的要求 |
|---|---|
| skill_tools.py:233 | node-local skill 只对属主可见，须传 for_node |
| skill_tools.py:467 | _deprecate_skill reasoning ≥10 字符 |
| skill_tools.py:553 | skill_admin deprecate reasoning ≥10 字符 |
| skill_tools.py:667 | 新 skill description ≥20 字符 |
| skill_tools.py:669 | 新 skill body_markdown ≥120 字符 |
| skill_tools.py:675 | 必须给 source_entries（只能从手册真实经验升级） |
| skill_tools.py:694 | 复发证据不足：需 ≥2 条不同 candidate 或单条复发计数 ≥2 |
| skill_tools.py:704 | body 里 backtick 引用的工具名必须全在注册表 |

### tools/library/artifacts_extra.py（4）
| file:line | 强制的要求 |
|---|---|
| artifacts_extra.py:478 | 类型声明的写入门 failures 非空即拒 save |
| artifacts_extra.py:524 | 类型声明的冻结门 failures 非空即拒 freeze |
| artifacts_extra.py:580 | reviewer→curator→decision flow 未闭合不许 freeze |
| artifacts_extra.py:730 | prereg 承诺了资源登记里不存在的资源即拒 freeze |

### 其余 return 侧（15）
| file:line | 强制的要求 |
|---|---|
| builtin.py:313 | artifact_type 属别的节点专属且本命名空间无旧版即不许新建 |
| builtin.py:945 | 给了 options 就必须给 recommended_option_index |
| builtin.py:969 | 推荐项不许是"等待/不动"类 |
| tasks.py:142 | block 的 blocked_reason ≥8 字符 |
| proposals.py:185 | propose reasoning ≥10 字符 |
| proposals.py:245 | 同 target 同类提议曾被 rejected，须 new_evidence ≥30 字符才准重提 |
| proposals.py:524 | resolve reasoning ≥5 字符 |
| runtime_control.py:431 | cancel reasoning ≥8 字符 |
| job_registry.py:35 | expected_duration_s 必填且 >0 |
| cross_model.py:113 | reason ≥8 字符 |
| profile_tools.py:116 | profile 更新提案 reasoning ≥10 字符 |
| audit.py:59 | revert reasoning ≥10 字符 |
| writing_gate.py:228 | override 只受理 project_verdict='iterate' |
| writing_gate.py:233 | reviewer 必须已 recommended_action='proceed' 才准呈递 override |
| decision_package.py:1087 | 本轮裁决已作出且已派发，拒绝重新呈递 |

### raise 侧（8）
| file:line | 强制的要求 |
|---|---|
| lib/kb_schema.py:452 | status 翻转 reasoning ≥30 字符 |
| lib/kb_schema.py:465 | 翻到 validated/refuted 必须 evidence_ids ≥1 |
| lib/kb_schema.py:474 | hypothesis verdict 证据必须含 chunk_id 或 experiment_id |
| lib/kb_schema.py:734 | synthesis 的 sources 必须含 ≥2 claim_id |
| lib/publication_figures.py:52 | 出版图必须 quality_mode=publication 且 status=approved |
| lib/publication_figures.py:56 | flow='direct' 不许用于出版稿件图 |
| lib/publication_figures.py:71 | 4 个 summary_flags 必须全 True |
| lib/publication_figures.py:73 | 必须绑定 figure_review_ids |

### 辅助函数展开（2，计在所属行）
memory_tools.py:43 手册节 append-only；latex.py:514 layout audit 未过删掉刚编出的 PDF。

## 骑墙判例
1. builtin.py:1545/1606「读过才准写」→ 判 A（盲写覆盖不可逆）。
2. run_node.py:1430 连续失败熔断 → 判 A（算力熔断，5×50 轮燃烧证据）；kb.py:676 同形判 D（烧的是记账不是算力）——两刀判据不对称，二审需统一。
3. run_node.py:1839 服务节点禁 background → 判 D（真实动机是防占位图定稿，注释自证）。
4. kb_schema.py:465 无证据翻 verdict → 判 D（evidence_ids=[] 明写，账薄不假）。
5. builtin.py:313 别人的 artifact_type → 判 D（created_by 如实，拒的是"不过 QC 门"）。
