# 判决书：nodes/experiment + nodes/data（87 裁决行覆盖 ~121 拒绝点；删 27 / 降格 47 / 保留升格 13 / 呈裁 5）

四份专审是本域的骨架，判决表按其结论落条。

## 专审一：prereg 一致性闸群（1946/1951/1977、3512/3517/3521）——出口不可走，降格
1. **amendment 出口在本节点不存在**：全仓 grep，experiment 侧只有报错文案；amend 原语住在
   hypothesis（写不跨节点），文案指了一条本节点走不到的路。
2. **走出口撞第二堵墙**：hypothesis 开修订草稿 → run_contract:171 进 pending_amendments →
   preflight:126-152 以 prereg_amendment_pending 判 False → 1951/1977 再触发。真解都在 run 外
   （orchestrator 重派/显式给 prereg id）→ fire_data 死路墙判据成立。
3. **代码库自己记录过死锁**（run_contract:100-105 注释：E2E v20「没有 amendment 工具可自救」）。
4. **走通也不等于义务形态**：修订成功后墙沉默，账本不留「本 run 曾偏离 prereg vN」痕迹——
   S4 要的恰是那条痕迹。现状=「申报制被换成偏离不可能+承诺无痕改写」。
→ 1951/3521/683 降格 O1：mismatched/missing/unexpected 三张表进 run manifest+提交记录+偏离义务；
算力不可逆性由其后的 HITL 提交确认（rm:1985+）承担，不受影响。

## 专审二：预测失败闸（safe_bash 2093/2109/2121）——删
- 三行只在**本机前台 bash** 路径（唯一调用点 3669），HPC 提交路径无 ABI 预检——「烧机=A类」不成立。
- 放行后果：链接器立刻失败（成本≈0，C 类兜住）；混族 MPI 最坏本机 600s 超时，timeout_escalation:402
  （A 类保留）接管。有界、可逆、账不假。
- 同函数分支③（2129-2136）同族异前缀只 warning——自证是阈值判决非安全边界。
- 「只 export 单个目录」的领域知识挂到失败 hint 上零成本保留。

## 专审三：plan 审批链的真假分界
**真墙（存活）**：`epp:1674`/`pc:350` allowed_node_types（B，节点能力边界=写不跨节点）；
`pc:353` risk_level=high 网关禁令、`pc:358` 内部工具递归禁令（A/B）；`epp:1667/1671` 注册表
validity（C）；`install:301/305/309/317` 供应链 pin（A）；`install:297` 许可证（A，且已是
needs_human_approval 正确形态）。
**假墙（全部只查「某文档有没有点过名」）**：store:468（根）、epp:1668、pc:342/396、sp:8575、
ws:214/247、install:462 审批半边。判据：拿掉它们，上述真墙一条不动——审批链不承载安全职能。

## 专审四：mesh_generator 五处 Tecplot——删（补两条独立理由）
1. 闸自己在造假状态：.msh+quality report 成功落盘且列在同一 return 的 written_files 里，status
   却写 error——S1 反向违反。
2. 零替代成立：tecplot:{status:error} 在成功路径本来就返回，删 if 块披露一字不少。

## 判决表（要点；完整逐条见裁决原文）

### experiment
| file:line | 判决 | 义务组 | 一句话 |
|---|---|---|---|
| rm:1005 | 降格 | O5 | 机械默认到 build_root+披露 |
| rm:1931 | 降格 | O4 | 框架按权威源（contract.run_role）取值+披露不一致；与 1951 同批 |
| rm:1946 / sb:3517 | 降格 | O2 | 照跑+input_delivery:unverified+如实记一条执行前提见证（**不翻任何门**，见下方 2026-09-21 注） |
| rm:1951 / sb:3521 / rc:683 | 降格 | O1 | 专审一 |
| rm:1977 / sb:3512 | 降格 | O3 | 聚合闸拆开全是记录完备性；stage_invalid 留 C |
| rm:2560 | **保留·升 B** | — | finalize 成功即销毁容器+control dir（2576-2584）——降格=允许先毁证后补析；且 auto log 撑 analyzed_success=出处伪造。事实待核见呈裁 1 |
| rm:2563 | 降格 | O6 | 到此 frozen 非 auto log 已存在，剩「正文含 job_id 子串」的字符串代理 |
| sb:2093/2109/2121 | **删 ×3** | — | 专审二 |
| sb:3112 | **删** | — | 校验器自己 import 不出→拒绝构建；违反同文件「预检自身异常降级放行」惯例（3667/3668/3697） |
| sb:3153 | 降格 | O12 | contract 校验真价值；build 照跑+errors 进记录 |
| sb:3341 | **删** | — | 预测失败+同函数自认 DAG 模型不可靠（3352 降级放行）+注释自证实测后果是绕行 |
| ca:1077 | **删** | — | 纯仪式：四字段逐字抄 true 框架不验；真验收在下游 verify |
| ca:1244/1269 | 降格 ×2 | O6 | 拿兄弟节点报告措辞当状态转移许可=死路；照记+corroborated:false |
| ca:1272 | 降格 | O1 | **关键设计**：fallback 事前许可→使用即如实记一条执行前提见证，前置从许可变效应（落地形态见下方 2026-09-21 注） |
| ca:1285 | **删** | — | 顺序仪式；1272 降格后无独立内容 |
| sed:151/636 | **删 ×2** | — | 字数闸且加在让步出口上（双重错误） |
| sed:156/167/639/648 | 降格 ×4 | O6 | freeze-gates 同型（fire_data：命中即照文案修复成功）；auto_generated 可机械标记 |
| oc:128 | 降格 | O4 | 压制真实发生过的收据=S2 反向违反（prereg 写 scientific 做了运维即无处记账） |
| oc:136/138/170/176 | 降格 ×4 | O6 | 同函数已有 verified{passed,evidence} 机制；缺证据→passed=False 的 check；176 的 next_step≥8 字数子句删 |
| oc:178 | **删** | — | 重复抄件：收据 outcome=blocked 本身就是 blocker 声明 |
| rc:646 | **删** | — | 字数闸 |
| te:402 | **保留·升 A** | — | 算力熔断且形态是范本（明列两出口+「这是执行方式限制不是不可行判定」）——立为降格改写模板 |
| rf:282 | 保留（改判，单列） | — | 工具作用域=注册表职责，运行时留 assert（呈裁 2） |
| rc:670 | 降格（补列） | O4 | 「不得改道」与 oc:128 合成死路；允许改道+记 scope_redeclared |

### data
| file:line | 判决 | 义务组 | 一句话 |
|---|---|---|---|
| mg:504 / cp:205 | **删 ×2** | — | ≥20 点任意阈值；保留相邻真 validity（chord≤1e-12、<2 点） |
| mg:1996/2375/4609/5027/6427 | **删 ×5** | — | 专审四 |
| epp:223/300 | 降格 ×2 | O5 | 相邻分支已有默认根，不自洽；走默认+记账 |
| epp:355 | 降格 | O10 | 缺**必需**交付物（非附属）；照发+manifest 列缺+下游照读照报 |
| epp:1663 | 降格 | O8 | plan 是承诺不是牢笼；超 scope 照跑记偏离 |
| epp:1668 | 降格 | O8 | 假墙（只查点名）；deviation 唯一登记点在 executor dispatch |
| epp:1674 / pc:350 | **保留·升 B ×2** | — | allowed_node_types=节点能力边界（沙箱不是审批） |
| inst:316 | 降格 | O13 | URL 只验形状不验内容；改 needs_human_approval（297 已有正确形态） |
| inst:462 | 降格（带替代） | O13 | **严禁裸删**：pin 记录（version+sha256+license）必需，Designer/Critic 批准不必需 |
| inst:467 | 降格 | O4 | plan 记录是权威（带 sha）；按权威取值+披露 |
| planner:286/288/327 | 降格 ×3 | O10 | 自相矛盾是真信号；作废整份分析=重试循环；缺字段填显式 unknown+义务 |
| planner:331 | **删** | — | S2 最纯粹违反：惩罚诚实的「分不了类」 |
| planner:354 | 降格 | O5 | 已有单消费者自动推断，扩默认即可 |
| pc:342/396 / sp:8575 | **删 ×3** | — | 审批链重复抄件（同一规则 2、3 处抄写） |
| pc:557 | 降格 | O8 | 理由自陈是出处归属→ad_hoc stage+outside_plan:true |
| pc:576 | **删（限定）** | — | purpose/evidence/acceptance 散文自证删；format+output_paths 留 C |
| ws:2402 | 降格 | O11 | 丢弃已下载字节让 agent 无法诊断；落 quarantine+不计交付物 |
| ws:2421 | **删** | — | 正则判「像不像 CFD」双料档一 |
| pp:112 | 降格 | O10 | manifest 缺失照发+记账；.previous 可逆（层数见呈裁 4） |

### blocked/needs_* 家族
| file:line | 判决 | 一句话 |
|---|---|---|
| **store:468** | **降格 ★根 → O9** | 审批链唯一根（6 直调+22 返回点）；Designer/Critic 评审照跑照记，不再是通行许可；产物打 plan_approval_status:unapproved+义务。~30 拒绝点收敛为 1 collector |
| pc:333/410 | 降格 ×2→O8 | 出处归属；410 触发条件是归属歧义=「记未归属」教科书场景 |
| pc:601 | **删** | 预测失败（正则猜二进制）+736 事后真校验覆盖同一问题 |
| pc:736 | 降格→O11 | 事后校验=档二标准件 |
| epp:1356/1365 | 降格 ×2→O9 | 随根收敛 |
| epp:1372 | 保留（C） | plan 记录读不出=现实自己拒绝 |
| epp:1586/1609 | **保留·升 A ×2** | 确定性重放抑制=算力熔断；1609 条件=store:468 同批（否则出口不可走） |
| planner:8267 | 降格（半）→O14 | 检索预算熔断半保留；「外部证据不可得即锁死生成」半降格（补救不在力内） |
| planner:8499 | 降格（半）→O8 | scope 半降格；缺 asset_kind 无法路由半留 C |
| planner:11557 | 保留 | 不把 Critic 调用花在 schema 坏件上+直接回 schema_errors（S6 token 经济） |
| planner:11849/12219/12315 | **保留·升 A ×3** | 轮次/零变化/停滞熔断；store:468 降格后自动只剩「不再花预算」，改措辞即可 |
| inst:297 | 保留·升 A | 许可证→needs_human_approval 已是正确形态 |
| ws:214/247 | **删 ×2** | 「委员会批准前不许查资料」S3 逐字；检索只读零账变全可逆 |

## 收敛地图（13 个 collector）
O1 prereg_deviation_declared ｜ O2 input_package_unverified（记见证，不降资格）｜
O3 experiment_preflight_incomplete ｜ O4 declaration_conflict_resolved_by_authority ｜
O5 routing_defaulted ｜ O6 closure_evidence_weak ｜ O8 plan_deviation（唯一登记点 executor dispatch）｜
O9 plan_unreviewed（根=store:468）｜ O10 deliverable_incomplete ｜ O11 artifact_contract_violation ｜
O12 build_contract_invalid ｜ O13 install_provenance_incomplete（needs_human_approval+pin 必需）｜
O14 evidence_gap_deferred。

## 跨批依赖（owner 必读）
1. O9（store:468）必须先落，否则五条保留的熔断出口不可走会退化成死路墙。
2. rm:1931 与 O1 同批（否则纠正 stage 后立刻撞 1951）。
3. oc 四条转 check 后流进 ~172 行「outcome=success 但验证未通过」真 B——实现须机械降 outcome=partial 并披露，不得在那里新造拒绝墙。
4. install:462 严禁裸删（pin 替代条款）。

## 呈裁清单
1. rm:2560：owner 核「容器销毁前 stdout/输出是否已全量落宿主 run_root」——是则随 2563 降格，否则保留。
2. 工具作用域（rf:282、oc:128）是否纳入 D 账本——建议排除，归注册表+assert。
3. ~~ca:1272 降格让框架单方面改写 run 科学地位（analysis_eligible）——治理项，wangd/owner 拍板。~~ **已裁（2026-09-11 owner + 2026-09-21 框架，#979）：见下。**
4. pp:112 降格提高发布不完整包频率，.previous 只有一层——owner 决定是否加深或显式接受。
5. 五条熔断的措辞改写（照 te:402 范本）走本战役批次还是 owner 自行安排。

## 统计
删 27（11 条有同文件自证）/ 降格 47 / 保留升格 13 / 呈裁 5；121 裁决对象 → 13 collector；
store:468 一条吃掉 ~30 拒绝点。


## 2026-09-21 补记：O1/O2 的落地形态与本文写的不一样（#979）

本文多处把 O1/O2 的降格形态记成「机械降 `analysis_eligible=False`」。**实现不是
这样，而且那个字段已经不存在了。**

- 2026-09-11 owner 裁决：机械降格见证改名 `execution_precondition_witnesses`，
  **不再翻任何资格门** —— 结论有瑕疵不等于没有结论，被记过见证的运行仍然欠裁决。
- 同批 `analysis_eligible` 从 experiment 的目标契约整个删除
  （`nodes/experiment/AGENTS.md:143`，不得以任何名义重建）；正式证据资格现在由
  `requires_hypothesis_verdict` 从 `execution_mode` + `run_role` 现算。
- 2026-09-21（本条）：框架侧的最后两处尾巴一起收掉 —— 仓库根
  `tests/test_run_contract_cross_node_read.py` 的三处断言改读
  `requires_hypothesis_verdict`（它们正是把节点侧那个派生别名钉住不能删的东西），
  `shared/tools/library/artifacts_extra.py` 的 prereg 冻结门不再要求申报该字段
  （它的理由写的是"与消费方 preflight 同源"，而那个消费方已经改判据了）。

所以「呈裁 3」不再是一个待裁项：框架不再单方面改写 run 的科学地位，因为那条
效应整个不存在了。留在上面表格里的是**当时的决议原文**，这一节是它的现状。
