# RFC：判决拆除 —— 充分性判决全量三档裁决与让步原语（2026-08-31）

- 状态：裁决完成，待 wangd 批 0 拍板 + 各 owner 认领批次
- 前置文档：README.md（判据宪法 S1-S6）、census_*.md（普查）、fire_data.md（开火数据）、
  verdicts_*.md（五份判决书，逐条 file:line + 理由）

## 1. 总判决

普查 ~2,400 个拒绝点中的 **596 条充分性判决（D 类）裁决行**（覆盖 ~620 个拒绝点，
store.py:468 一条根吃掉 ~30 点）：

| 档 | 条数 | 占比 | 含义 |
|---|---|---|---|
| **删** | **167** | 28% | 检查与拒绝一起死，零替代（全部逐条过边界复核：账真+可逆） |
| **降格为义务** | **354** | 59% | 检查存活为见证，拒绝分支死，未过项进义务，referee 终审 |
| **保留/升格** | **65** | 11% | 二审认定实为 A/B/C 误分（升 A 熔断 12、升 B 账本墙 14、改判 C 契约 ~20），或形态已正确 |
| **呈裁** | **21** | 3.5% | 治理设计项 + 事实待核项，逐条附两边论点与建议（§5） |

删档内部：字数闸 **41 条全灭**、逐字断言/自述抄写闸 ~45、重复抄件 ~20、任意审美阈值 ~30、
预测失败 8、部分成功判全废 12、事前审批仪式 ~15。

## 2. 收敛：620 堵墙 → 约 28 个平台级 collector

五域 55 个提名义务组按效应归并（同族跨域合并）：

| 平台族 | 合并自 | 一句话 |
|---|---|---|
| 偏离申报族（3）| core-prereg、exp-O1/O4/O8、pp-OB-DEVIATION、wr-W-OB2 | 对冻结承诺/调用方契约/plan 的偏离必须申报，申报即合法（S4） |
| 审批解除族（2）| exp-O9（store:468 根）、wr-O5/图包审批、pub_figures | 评审照跑照记，不再是通行许可；未审产物打如实标记 |
| 完备性族（5）| OB-COMPLETENESS、D-OB1、W-OB1、O10、O3 | 缺行/缺字段/缺交付物照做+逐项披露（图面/manifest/局限节） |
| 语义正确族（3）| OB-SEMANTIC/RANGE、D-OB2 | 违例照画+违例位置标在图上+进 validation 报告 |
| 契约元数据族（2）| OB-CONTRACT-META、O5 | 缺单位/标签/路由→占位照做+义务派给上游 |
| 证据强度族（4）| O2/O6、kb-O2、R-OB1、D-OB3 | 弱证据/弱收尾如实分级（none/claims_only/uncorroborated），referee 裁 |
| 终态清偿族（2）| core-终态清偿、收尾记账口径 | 欠账必须被应答（补/让步/呈报），应答即清偿 |
| 补偿渲染族（4）| OB-CAPACITY/DEFAULTED-SELECTION/LABEL/BACKEND-FIDELITY | 框架自动补偿（截断/默认/引线/降 hybrid）+补偿量记账 |
| 记录治理族（3）| 账本状态机如实迁移、记忆送达、工作区留痕 | 反常迁移留痕不禁止 |

## 3. 批 0：让步原语（唯一的新机制，其余全是删）

**样板已在仓库里，不发明只统一**：`prereg_commitments:835`（estimated+basis+degraded_reason
申报即放行）、`run_node:1218/1313`（waiver 字段）、`derivation:516`（如实降级出口）、
`timeout_escalation:402`（出口文案范本）、`input_audit:773`（blocked 形态照常前进）、
`obligations:148`（blocking=False 只渲染）。

设计约束（五路裁决共同推出，违反任一条即重蹈覆辙）：

1. **结构化让步字段 `concessions[]`**，出口**不得由关键词匹配把守**（deriv:516 实测：英文
   写的诚实降级过不去——有关键词出口等于没有出口）。
2. **让步改不了 status/出处**：产物永远如实写明哪些检查没过（S1 兜底）；框架能机械佐证的
   理由必须佐证（能力注册表、指纹、账本），佐证结果随让步同记。
3. **总闸 `chat.py:1224` 接受申报式让步**：blocking 义务可被 conceded（理由+佐证入永久账本+
   收尾清单+稿件局限节），complete 放行；referee 终审逐条裁决 conceded 项。当前
   「blocking 义务在无人值守下没有任何合法终点」是 qinp 事故的批量复制器，此闸不开，
   354 条降格全部白做。
4. **档位渲染**：assisted → 决策呈递给用户；autonomous/unattended → agent 按自己的推荐让步
   并继续，异步呈报。同一决策点，一条渲染规则。
5. **删除自证闸必须同批修记账**（postprocess 呈裁 §4）：relational_rendering/rendering 把
   "upstream_computed"/"data" 硬编码写进 manifest——只删闸不改账，产物变成框架无依据的出处
   断言。改为记「上游声明值/not_declared」+ provenance_verified:false。
6. **义务不新增拒绝墙**：exp 跨批依赖 §3——oc 四条转 check 后不得在下游「outcome=success 但
   验证未通过」处新造一堵墙，机械降 outcome=partial 并披露。

## 4. 执行批次与依赖

- **批 0**（wangd 拍板后，框架面）：让步原语 + chat.py:1224 总闸 + obligations collector 骨架
  + 防复发闸。
- **批 1**（框架面，可与批 0 同 PR 序列）：core 12 删 + shared 20 删（首刀 `latex.py:514`——
  全域唯一销毁证据的墙）+ 两域 44 条降格接进 collector。
- **批 2**（postprocess，owner 协调/wangd 授权，PR#707 先例）：73 删 + 171 降格。先行三刀：
  lifecycle:1401、2543、relational_grammar:71（前置=manifest 记账修正）。
- **批 3**（各 owner）：writing 148（qinp）、experiment+data 121（含依赖序：store:468 先落，
  rm:1931 与 O1 同批，install:462 带 pin 替代）、hypothesis（=加 `_ADVISORY_CHECKS`，零新机械）。
- **防复发闸**（照奥卡姆规矩）：新增拒绝分支必须声明属 A/B/C 哪类并登记；声明不了只能写
  collector。AST 扫盘护栏（扫 `return {"status":"error"` 与 raise 型拒绝分支的新增），不写名单。
- **验收判据**（照 RFC 规矩，真实回放不是测试全绿）：qinp 会话历史回放——铸图包链路上零硬拒；
  fire_data 复测——顺序闸类签名开火归零、义务应答率与让步率进入 referee 视野；变异——把任一
  删掉的墙加回去，对应回归测试转红。

## 5. 呈裁汇总（21 条 → 你与 owner 的决策点）

**wangd（治理/框架）**：
1. 裁决权分离（verdict_authority:161/170）——建议：翻转落 provisional+claimed_by+背书义务。
2. chat.py:1224 让步语义（批 0 核心）——建议：申报式让步+referee 终审。
3. ~~ca:1272 让 fallback 使用即机械降 analysis_eligible（框架单方改写 run 科学地位）。~~
   **已裁并落地（owner 2026-09-11 + 框架 2026-09-21，#979）**：改成如实记一条
   `execution_precondition_witnesses`，**不翻任何门**；`analysis_eligible` 已从目标契约
   删除，正式证据资格由 `requires_hypothesis_verdict` 现算。详见
   `verdicts_experiment_data.md` 末尾「2026-09-21 补记」。
4. reviewer（VLM 审图员）判决字段剥离改提示词约束+下游剥离。
5. 工具作用域类（rf:282、oc:128）是否出 D 账本归注册表。
**owner**：
6. KB org 晋升三查（三查降格为人批呈报材料）+ domain_registry 人批（provisional_leaf）。
7. run_node:1516 callable_nodes（保留+让步出口 vs 删）。
8. artifacts_extra:580（先补 review_state 进冻结件 metadata，再降格）。
9. rm:2560（事实待核：容器销毁前证据是否全量落盘）。
10. pp:112 的 .previous 层数。
11. lifecycle:217（外部改写 hash 不符建议升 B）/ 721（constraint_locks 拆 locked_by）。
12. relational_grammar:390（不实现厚度补偿则升 B）。
13. validation 2485/2526 拆条（发布面二次防线已核有，owner 确认）。
14. 「引用须 plan 预列子集」六连删——是否存在先冻 plan 的真实流程。
15. unverified_figure 取值命名（writing×postprocess 共同）。
16. fixture 四条（WRITING_FIXTURE_DELIVERY_CAPABILITY 开放计划）。
17. 五条 A 类熔断的措辞改写归属（照 te:402 范本）。
18. hypothesis research_state 200/232 的 B/D 分界。
19. deriv:722 audit_target carve-out 的常量结构改动。
20. exp 专审一的 orchestrator 侧配套（prereg 重派/显式传 id 路径的顺畅化）。
21. 熔断统一判据（§verdicts_shared）升格为平台判例：合法熔断=同一信号重复+机械断链+出口在力内。

## 6. 一句话

框架从此对 agent 只说三种话：**这不许（安全/账本）、这是事实（契约/物理）、这没过检查——
你决定怎么办，你的决定会被记录并被终审**。第三种话取代了 620 堵墙。

## 7. 执行期拍板记录（wangd 授权「按最优判断确认」，2026-08-31）

呈裁 21 条全部定案，其中两处**有意偏离**裁决书原判，理由如下：

1. **prereg:816 不删（原判：删并入 verdict_authority:154）**。两条是同一 B 规则
   （裁决必须可归档核对）在**两个真相源**上的各自执行：154 查 research_state、
   816 查冻结预注册。删任一条都会在「只有另一源在场」的路径上丢覆盖；合并则
   引入模块耦合。应删尽删不删覆盖。二者均改判 B 保留。
2. **prereg:835 随裁决权组一并降落（原判：升 B 保留拒绝）**。落点降级
   （validated→provisional 如实入账）**比拒绝更强地**满足其 B 保护——机器可读
   status 从不落假值，同时死路消失。B 的判据是「放行会让账变假」；降落式放行
   账不变假，故拒绝形态非必需。申报式兑现（estimated+basis+degraded_reason）
   仍是升格出口，原样保留。

统一落地形态（裁决权归属组八墙 → 一条规则，仅改 kb.py 一个调用点）：
**翻 validated/refuted 的资格核验不再拒绝任何调用——够格落申请状态，不够格
如实降落 provisional**；差额原因全文进 transcript
（scientific_verdict_downgraded_to_provisional）与工具返回（authority_note），
referee 终审可见。

其余 19 条按裁决书原判执行；owner 域改动以「wangd 2026-08-31 全量授权」为据
（PR#707 先例），各域独立成 commit 供 owner 事后复核。

## 8. 第三波执行记录（2026-09-02，wangd「推进落地」）

起因：二审普查（census2_residual_844.md）答 wangd 三问「剩下的全符合第一性原理 / 最优设计 /
勿增实体吗」——三个不：D 59 / X 64 仍在；一审判了降格但执行批次报「已落地」实未动 ≈20 处
（writing-gate 是 fire_data 冠军墙）；注册表派发口无 schema 校验器致 176 处手写参数检查；
基线 844 只是两种句法的棘轮读数（另 ≈450 处其它形态）。

四刀、五条规则（契约归 schema / 判决归义务 / 一题一答 / 文案与判据同源 / 零调用方即删）：

| 刀 | 内容 | 结果 |
|---|---|---|
| 1 | `core/tool_registry._execute_dispatch` 按 schema 核取值（required/enum/区间/非空/pattern/条目数；**不查 type**，有工具刻意宽松） | 一个校验器替 ~150 处手写检查；kb_schema 27 处 raise → `_ENTITY_SCHEMAS` 声明 + `shared/lib/schema_check.py`；scheduler/stage 词表各收成一个常量 |
| 2 | writing-gate 整套退场（run_node 门 + writing_gate 工具 + pause 类型 + 4 处 yaml + 2 个注入输入）；一审未落的 ≈20 处 D 全部落地；legacy 转发分支删；forward_artifact 删 | 六域按规则 2 降格，每处配「墙加回去必转红」的测试 |
| 3 | X 64 清扫：vendored 模板安装器三个 scripts/ 目录（19 文件）、memory_migrate、update_memory_lifecycle、不可达尾巴、重复抄件；假文案「≥N 字符」全部改与判据同源 | — |
| 4 | 扫描器 AST 化：跳过 docstring，认 tuple 返回、`_error()/_err()` 助手、五个专用异常类；ValueError/RuntimeError 明确不计 | 同一把新尺：刀前 933 → 刀后 **639**（−31.5%）；老尺 844 → 556 |

规模：175 文件，+5,569 / −11,547 行。全量 4,615 绿，85 红与刀前基线逐条同集（sympy/沙箱
Landlock/`/private/var`，环境）。各域变异（墙加回去）：C 5、D 7、E 8 全部转红；集成层另抽三处。

执行期判例（新增，供后续引用）：
- **校验器不查 type**：`save_artifact.metadata` 声明 object 却刻意收 JSON 字符串——类型的「刻意宽松」
  在 178 个工具里不知道还有多少；取值（enum/区间/非空/pattern）不存在刻意宽松，可在派发口一刀核。
- **手写检查只在两种情况下保留**：校验器不支持的形态（oneOf/XOR、嵌套 required、anyOf、minProperties）
  和「函数被仓库其它代码直接调用且传的是模型给的值」（python_exec:191 被 safe_bash 直调转发）。
- **sandbox:1952 不是重复**：新建容器路径用 initial_limits 直接 `docker run`，不经 1865 的 admits；
  二审判 X 是误判，保留。判「重复」必须沿真实调用路径走一遍，不能只看两处条件长得一样。
- **删了角色闸，身份契约仍在**：hypothesis 节点写 hypothesis claim 现在被 `hypothesis_id` 必填
  （原地更新身份锚，C）挡住而不是被「你不是 curator」挡住——同一条路，拒绝的理由从身份换成契约。
- **建议不落地的**：kb_promotion:487 / domain_registry:573（org 晋升三查与人批，README 档三治理项）、
  builtin:1576 write_file 先读再覆盖（落覆盖前快照后再拆）、figure:282 生成式像素声明措辞、
  writing 的 `podsys_safe_source.py`（归 qinp）。

未登记待下一轮普查：mesh_generator 5918/5962/6363/6544、cfd_case_router 365/374/551、
scientific_preprocessor 7364/7370/7419 的「原意图不允许」意图判决；`nodes/*/tests` 缺
HARNESS_FRAMEWORK_HOME 隔离（org KB 从真 home 读进测试）。
