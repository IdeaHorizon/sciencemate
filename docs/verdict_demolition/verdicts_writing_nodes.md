# 判决书：writing / hypothesis / literature / _reviewer / observation / derivation（148 条；删 35 / 降格 92 / 保留升格 21 / 呈裁 6）

## 判决依赖的机制事实（先摆明）
1. `validation.py:2331 "passed": not errors`——68 条 errors.append 都是一票否决；下游冻结门/
   hooks/发布面都读 passed。降格的机械含义唯一且现成：**errors → warnings/义务收集器**。
2. `hypothesis/tools/output_validator.py:92-99` 已有 `_ADVISORY_CHECKS` 两层机制（2026-08-08
   因同类事故建立）。hypothesis 全部降格 = 加 check 名进 frozenset，**零新机械**。
3. observation/derivation 的 contract 闸注册的是 **save gate**——拦的是存盘本身，比「拒绝渲染」
   更重一档：科学家连草稿都存不下。
4. compact_delivery/material_gap_delivery 带 `WRITING_FIXTURE_DELIVERY_CAPABILITY`（E2E fixture
   生成器），不在科学家路径——除 855 外改判 C。
5. `deliverable_publishing.py:171` 发布面（真正不可逆边界）已独立要求 approve critique——
   validation 2485/2526 的审批半边是第三份抄件。

## 判决表（要点）

### validation.py（68：删 25 / 降格 39 / 保留升格 4）
- **删 25**：
  - **991/1001/1011/1040/1050/1060「引用必须是 plan 预列子集」六连删**——plan 是同一 agent
    几分钟前的工作纸不是冻结承诺（冒牌 S4）；写作中补引是科研常态，墙只伤诚实场景。真承重的
    引用诚信（幽灵 claim 968、ref 解析 1028、正文与 inventory 不符 980、占位引用 975）不在
    D 名单，一条不动。
  - **1018**「至少记一件 upstream」数量下限，且让综述型论文不可能存在；同文件 959-967 wangd
    2026-08-22 已把同族 ≥N 降为 warning——这是漏网。
  - **1228/1233/1240**「blocked 稿不得含引用/claim_id/参考文献」——禁止诚实的缺材报告履行
    职能（报告的全部工作就是点名缺什么）。
  - 1110/1174/1180/1488/1567/1569/2124/2127「自述字段必须抄固定值」家族——框架已独立验真
    （编译收据/重算 zip/直接扫 content），向模型索要口供是重复抄件+纯仪式。
  - 1317（qc_summary 必须在前 600 字符——正是 qinp 锚点事故的形态）、1799（逐字抄清单）、
    1872（非空闸写 "ok" 即过）、2019（与 1856 同一事实第二字段）、1661/1667/1673（同一要求
    三处查，留 1678 一条）。
- **降格 39** → 义务组 W-OB1..W-OB9（见收敛地图）。要点：
  - 884/890「必须用掉每一件产物」方向反了（强制使用制造引用注水），但探测的是**静默丢弃=
    选择性报告**（S4 真关切）→ 义务=逐条申报未使用产物及理由。
  - **1683「必须 approved 图包」= qinp 事故近因**→ 降格：无 approved 图包照样出稿，如实标注
    「本图未经图评审」。血缘（package_hash 等）由 submission.py:281-284 承重，不动。
  - 2133/2142/2160 → **框架机械改写状态**（submission_ready→revision_needed+未过项清单）——
    这恰是档二「让步改不了 status」的 S1 兜底本身。
- **保留/升 B（拆条）**：**2485/2526**——(a) hash 绑定升 B 保留（校验/评审挂另一份字节=账变假）；
  (b) `passed is True`/`verdict in {approve...}` 前置删（S3 审批链；发布面已有独立审批）。

### writing 其余（10）
- 855 **删**（部分成功判全废：文件全产出却因附属校验判 error；864 行已有 validation_passed 载体）。
- 187/220 降格→W-OB8（220 现状**逼 agent 谎标 static_asset 才能过=墙制造假血缘**；给第三种
  如实取值 unverified_figure）。
- 278 改判 C；fixture 四条改判 C（对生产开放时须同批降格）；325 降格→W-OB6（SystemExit 形态
  尤重；portability 报告随包交付，人裁）。
- **773 改判「非墙」**：它只加 blocking_items+置 blocked_missing_required_upstream，写作照常走
  material-gap 形态——**全域少见的正确形态，应作范式**。

### hypothesis（11：删 1 / 降格 9 / 升 B 1）
- 176 删（非空闸，且与 2026-08-16「假设非必需」演进矛盾）。
- **200 拆条**：冻结 prereg 约束的假说撤回必须写 reason——**升 B**（withdrawn_reason 就是 S4
  申报机制，不申报账变假）；未冻结工作假说→降格 H-OB1。
- 204/232/131/230/242/315/335/356/324 降格→H-OB1，执行=全部加进 `_ADVISORY_CHECKS`（315 的
  0.72 阈值只出信号；324 须把「解析失败」与「真无 falsifier」分开报）。

### literature（3）：87 改判 C；34 删（模式适用域审批）；49 降格→L-OB1（意图是 S2 本身）。

### _reviewer（10：删 2 / 降格 7 / 保留 1）
- 123 删（重复抄件+让同行评审做不成）；**395 删**（强迫 referee 凭空造阻塞项=墙制造它要防的
  东西；与 389 一起是开火榜第一 writing-gate 的上游供给端）。
- 294/296/314/357/370/381/389 降格→R-OB1（意见矛盾如实入账，referee 终审）。345 改判 C。

### observation（26：删 4 / 降格 19 / 保留升格 3）
- 545 删（≤64 字符，档一逐字点名）；644/662/666 删（抄固定字面量框架不验：reference=0、
  [0,1]、direction_consistent=true）；660 删（雷达 ≥3 轴：用图型美学没收合法数据，正解换 bar）。
- **224/233 升 B**：exploratory 勾除预注册闭合条目=anti-HARKing 账本闸，放行即账假。
- 654 保留（已带 allow_one_sided 显式声明出口=档二形态）。
- 其余降格→D-OB1/D-OB2（616 有开火佐证：5 次命中全部合规重算——转义务零损失；607 附注：
  义务形态应允许如实保留冲突单元格）。

### derivation（20：删 1 / 降格 14 / 保留升格 5）
- 224 删（对散文做关键词黑名单，写句假「by Lemma 3」即过；文案并入提示语）。
- **684/691 升 B**（同 observation 224/233）；**722 拆条**：audit_target 内容指纹升 B（审一份
  贴另一份），其余降格。
- **737 降格（决定性理由）**：墙**奖励删除反例**——链上留 failed 验证是最诚实记录，拒绝冻结
  等于教 agent 删掉那次失败；转义务后激励反转。derived["failed_steps"] 已在算，见证现成。
- **516 保留为范式样板 + 一处修**：「兑现 rigor_level 或在 credibility 如实降级」已是完整义务
  形态——但出口判定靠中文关键词匹配（英文诚实降级过不去）。**义务出口不得由关键词把守**
  （638/762/654 同病同修）→ 结构化 concessions[] 字段。
- 434 改判 C（sympy 解析契约）；248/599/607/638/762 降格→D-OB3。

## 收敛地图（13 个 collector，W-OB4→W-OB6、W-OB3→W-OB7 合并后）
W-OB1 元数据齐备 ｜ W-OB2 证据链一致性（申报未使用产物）｜ W-OB5 期刊版式 ｜ W-OB6 投稿包完整性 ｜
W-OB7 QC 摘要披露（S2 落点，优先级最高）｜ W-OB8 图证据链（跨 postprocess）｜ W-OB9 状态自洽
（框架机械改写）｜ H-OB1 研究问题纪律（=_ADVISORY_CHECKS）｜ L-OB1 空结果披露 ｜ R-OB1 评审
完整性 ｜ D-OB1 记录结构（save gate 一律不再拒存盘）｜ D-OB2 绘图语义契约 ｜ D-OB3 严格度与
可信度披露（516 形态推广）。

**跨组统一修项**：deriv 516/638/762 + obs 654 的关键词出口 → 结构化让步字段，框架佐证，referee 终审。

## 呈裁清单
① 2485/2526 拆条（hash 绑定升 B / 审批前置删）——发布面二次防线已核有（deliverable_publishing:171），
须平台 owner 确认后落刀。② 991 系列六连删——若存在「先冻 plan 再写」的真实流程，该路径升 S4
申报形态而非全删。③ 1683+187/220 的 unverified_figure 取值命名与下游消费，postprocess owner
共同定义。④ research_state 200/232 的 B/D 分界一并拍板。⑤ deriv:722 的 audit_target carve-out
结构改动归属。⑥ fixture 四条：WRITING_FIXTURE_DELIVERY_CAPABILITY 是否有开放计划。

## 统计
删 35 / 降格 92 / 保留升格 21（升 B 6：obs 224/233、deriv 684/691、validation 2485/2526 的 (a)
半、research_state 200 冻结半）/ 呈裁 6。35 条删全部过边界复核；触及出处真伪的检查
（968/975/980/1028/1161/1491/281-284）均不在 D 名单，一条未动。
