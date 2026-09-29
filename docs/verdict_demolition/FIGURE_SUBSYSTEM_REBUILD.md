# 图子系统重建规格：从官僚流水线到「代码+数据+证人」（2026-09-01，wangd 拍板）

wangd：「只要不影响第一性原理、科学流程，应删尽删。」本文件是执行宪法。

## 第一性原理

科学家出图 = 数据 → 画图代码 → 自己看/证人看 → 改 → 交审稿人。
科学要求的只有：**出处可追（哪份数据+哪段代码）、结果可复现、检查如实记录、
终审有人裁**。其余一切（typed 产物链、审批状态、质量档位、专有 DSL 强制通道）
都是替"看不见自己图的文本模型"发明的脚手架，脚手架不许锁死科学家。

## 不可压缩核（删到此为止，一字不动）

1. 沙箱与销毁确认（A）；
2. **出处绑定**：figure 记录 ↔ 输入数据 artifact hash ↔ 渲染代码 ↔ 输出文件 hash，
   记录不许伪造、生成式像素不得充当证据图（B）；
3. 预注册偏离须申报（S4）；
4. run log 全保真；
5. referee 终审。

## A 刀：删判决层（先行，解锁 qinp）

- **删 status 阶梯**：figure_package 的 `status`（draft/reviewed/approved/incomplete）、
  `PACKAGE_STATUS_BY_MODE`、`quality_gate_valid_for_mode`、`review_valid_for_mode`
  等合成旗标全部删除。包（暂存期间）只带：跑了哪些检查、各自 findings、血缘
  hash、交付文件。**「够不够发表」永远不是铸包时算出的字段**（证据可持久化，
  判决不可以）。
- **删三轮修订循环**（lifecycle 的 review→revise→review 编排）：框架不替模型
  决定返工。VLM 观察进 findings，改不改、怎么改归 agent。
- **删三档 quality_mode 的身份语义**：机械审计便宜，恒跑；VLM 是否运行由
  **能力注册表**机械决定（有就跑，没有 findings 里自然没有这类条目——
  这不是异常，不写任何特例分支）。`quality_mode` 字段整个删除。
- **VLM 从盖章岗降为证人**：review_image 的观察以 findings 形式并入图记录；
  verdict 词表（approve/minor/major）删除——观察就是观察。
  `model_roles.require` 在此路径不再被调用（用 resolve 判在场）。
- writing 消费端：读 findings 并披露（批 3w 已是 findings 形态，删掉残余的
  approved/unreviewed_figure 二分——只剩「引用图记录+披露其 findings」一种形态）。
- 瞬态审图失败照旧如实分类记录（不静默降级），但它只影响 findings 完整性，
  不再影响任何"身份"。

## B 刀：删产物链与 DSL 强制通道

- **八种 typed 产物收敛为一种**：`figure` 记录（含：源数据 artifact ids+hash、
  渲染代码路径+hash、输出文件+hash、findings[]、caption/alt_text、replay 信息）。
  brief/source_profile/plan/derived_data/validation_report/review/package 作为
  **独立 typed 产物与 schema 校验器全部删除**——工作笔记归 agent 的工作区，
  不 typed、不 hash 链、不 schema。
- **渲染主路径 = agent 在沙箱写 matplotlib**（execute_python 已有）；框架机械
  录入出处绑定（不可压缩核 #2）。**可复现性是像素诚信的锚点，不是 DSL**。
- **DSL 降为技能库**：v2 各图型渲染器中的领域知识（出版排版、字体、图型惯例）
  收敛为 skills/模板与可选辅助函数；作为强制通道的入口、校验、词表、阈值全删。
- **图像级机械审计保留为恒跑证人**（与渲染方式无关的那部分：文字出界、
  碰撞、分辨率、字体缺字/豆腐块）→ findings。绑定 DSL 数据结构才跑得动的
  语义检查随 DSL 一起降级进对应 skill（用该 skill 渲染则附带跑）。
- lifecycle 工具面收敛：9 个 → 2 个左右（渲染+录入为一体；VLM inspect 按需）。

## 检查存留三条门槛（横扫所有节点的二次清洗尺）

留下一条机械检查必须**三条全中**：
1. 机械事实（不含审美/充分性判断）；
2. 模型自己看不见（文本模型看不见像素/编译产物才需要证人）；
3. 对账本真实性或可复现性有后果。
不过线的连 warning 都删——warning 噪音也是成本。

## 执行纪律

- 分两个 commit：A 刀（判决层）先独立成 commit 并全绿（qinp 解锁点）；B 刀随后。
- 钉旧行为的测试改写为钉新不变量；变异抽查各 3 条。
- 出处绑定/防伪的 B 类检查在每一步之后必须还在（grep+测试双确认）。
- 防复发基线随删收紧。
