# Review spec for writing 节点产物（`manuscript`）

> 当 _reviewer 节点审 writing 产的 manuscript 时，按本 spec 走 7 维度审稿。
> writing 节点 2026-09-18 重建（docs/WRITING_NODE_REBUILD_PLAN_20260918.md）：交付物是渲染好的 PDF +
> LaTeX 源 + 作者备注；稿件只面向期刊读者，账本类内容全在作者备注里。审的是 PDF，不是源码。

## 审稿前必做

1. read_artifact 拿 manuscript 全文
2. read_external_artifact 拉 lineage：experiment_log（含 verdict reasoning）、pre_registration、survey_report（如有）
3. 读 manuscript metadata 里的机械事实：`hard_lines_passed`（12 条机械硬线：标题块、过程词汇、空引用、图表编号与图题、
   参考文献闭合、简报硬性要求、版面、图内语言、图内字号、乱码、图文件对账、成品字号）、`referee_verdict` / `referee_round` /
   `referee_pdf_matches`（写作节点内部审读官对**这一版 PDF** 的结论）、`pages`、`pdf_path`、`author_notes_path`、`forced` / `force_reason`。
   `hard_lines_passed=false` 或 `referee_pdf_matches=false` 的稿不是可交付状态，先记 critical 再读正文
4. **重建"用户到底要什么"**（判 Responsiveness 维度的材料，缺了就是无材料空判）：
   - `read_artifact('research_state__research_state')` 拿注册的研究问题（Q1/Q2…）及各自裁决；
   - `read_file` 读项目根的 `PROJECT.md`（原始 brief）；
   - `read_profile` + `memory_recall`（查"用户意图"类记忆）—— **用户中途会修正诉求，以最新为准**；
   综合成 ≤200 字 `user_requirement_summary`，含用户原话片段。这一步给的是第 7 维的判据来源。
5. **读展开后的全文，不是装配入口**：manuscript 的 content 是 `main.tex` 加全部
   分节 `.tex` 的快照（`% === included file: <path> ===` 分隔）。只读 main shell
   等于没读正文 —— 这次事故里 `results.tex` 的"图内英文"制作说明就在分节文件里。
   逐个分节都要读到；有 run-local 项目目录时对照读一遍。
6. **读作者备注**（`author_notes_path`）：数据与稿件来源、待作者补充、缺口与未兑现、图表事项、未采纳的审读意见、AI 参与说明。
   机械硬线**没有**核的东西（图画得对不对、数字口径是否自洽、论证是否成立）要么你在本次审稿里补上，要么在报告里
   原样记为"未执行" —— 没执行的检查和执行了没发现问题，在报告里长得一模一样。
7. 备注里「缺口与未兑现」列出的项，核正文是否如实把它们当缺口交付（正文用读者语言写限制，不写过程话）。
8. 投稿型研究论文（体裁 sci_article_*）执行下面的科研图片门槛

## 科研图片门槛

投稿型研究论文至少要有一张由上游结果生成的科研图片。表格不能替代图片。

reviewer 必须逐项核对：

1. PDF 里有图（不是表），每张图有编号、图题、正文引用（硬线 H4）；
2. 图文件与提纲图键对账通过（H11），图内文字语言与字号过线（H8/H9）—— 这些在 `hard_lines_passed` 里；
3. 图题说的与图里画的是同一张图：对照 figures/ 下图表服务的记录（figure__*.md 的 Caption）与稿件图题；
4. 作者备注「图表事项」说明了每张图的来源与重画情况；用户交来的原图不得原样进稿（语言、命名、可读性都要按简报重画）。

任一项缺失都记为 critical concern，最终 action 不得为 `proceed`：

- 图表服务画错、画不出：`recommended_action=revise`，target_node=`writing`，反馈中写清需要
  绘制的数据、结构与证据来源。
  出图是 writing 自己调的服务（它的 `callable_nodes` 里就是图表服务），重画由 writing 这一轮
  自己发起；图表服务是 `post_run_flow: none` 的服务节点，退回去那条审查义务永远关不掉（#1061）；
- 上游已有图片，writing 没有嵌入或解释：`recommended_action=revise`，target_node=`writing`；
- 技术报告、基金本子这类体裁不执行这条投稿论文门槛。

## 多视角审稿流程

reviewer 需要先按以下 5 个视角独立形成检查意见，再汇总到后面的 6 维度评分中。各视角可以在同一 agent 内顺序执行，但报告中必须保留清晰来源。

1. **Evidence Reviewer**：检查 KB claim、引用、数字、图表和作者备注「数据与稿件来源」是否一致；重点找不存在的 claim、占位 citation、正文引用但 metadata 未记录的证据。
2. **Methods Reviewer**：**正文里出现的每一个方法参数，都要能在上游 artifact 找到
   出处** —— 判据的主语是"论文写了什么"，不是"某张清单上的字段在不在"。分子模拟
   写 timestep / cutoff / thermostat，系统仿真写软件版本 / 拓扑 / workload / 并行
   映射 / 测量口径，深度学习写数据划分 / 超参 / 评测集：领域不同，判据同一条。
   重点找 writing 自行补全、上游从未提供的实验细节。
3. **Logic Reviewer**：检查 Introduction → Methods → Results → Discussion 的 claim-evidence-conclusion 链条；重点找 Results 没支持但 Discussion 扩大的结论。
4. **Devil's Advocate**：提出最强反驳、替代解释、过度声明和可能的 cherry-picking；若发现 critical issue，最终 verdict 不得为 approve。
5. **Format / Production Reviewer**：检查 LaTeX、PDF、metadata、引用格式、figure/table 引用、venue 格式；确认 PDF 是本轮 compile_latex 产物。
   对投稿型研究论文，缺少科研图片时该视角必须标为 `critical`。
   另查三件这次漏掉的：
   - **交付物本身**（不是源码）：clean PDF 的首页（标题/作者/机构/摘要/关键词，
     不该有模板占位符）、图表页、文后声明的位置与重复、异常分页。
   - **图注声称的数量与结构**是否与图里画出来的一致（这次 TikZ 源码循环生成 4 个
     switch、16 个 GPU，图注写 8 个 switch、32 个 GPU）。图记录带 `contract` 时按
     合同逐条对；没有合同就如实记"无结构声明可对账"。
   - **完成摘要有没有越权**：机械校验 `passed` 只代表构建与声明对账；完成摘要把它
     表述成"图文事实已核验/五张图已对账"而 `not_verified_by_this_check` 里明明写着
     未执行 → honesty 维 critical。

## 7 维度

> 这是一次**真正的审稿**，不是合规检查：你要回答"这篇东西够不够格、回答了用户
> 问的那件事吗"，而不只是"格式/引用对不对"。忠实性（3/4/6 维）是底线，不是全部。

### 1. 新颖性 (novelty)

跟 KB 已有 claim / synthesis 比，本 manuscript 有真贡献吗？
- 用 `search_kb` 查相关 claim（不传 entity_type = 全景）
- 若主要 claim 已在 KB 里被 validated → 标 novelty=低
- 若提出了新 synthesis 或抓住了 KB 没记录的 dead_end → 标 novelty=高

### 2. 可证伪性对账 (falsifiability_alignment)

prereg 的 falsification_criteria 在 manuscript 里有显式回应吗？
- 每条 prereg H 必须在 results 段对应回应
- **核心警惕**：promise-delivery gap —— prereg 写要测 N 个种子但 manuscript 只跑 1 个

### 3. 数据完整性 (data_provenance)

claim 是否都引了 chunk_id / claim_id？数字和具体实验细节是否能从上游 artifact 或 KB 追溯？metadata evidence inventory 是否与正文一致？
**关键声明全查，其余抽查**（抽 3-5 个数字不足以发现这次的错误：768 行原始结果里
`0.105609 s` 属于 case5，图注写成 case6，全表最大值 `5.102144 s`，正文却把
`0.6721 s` 称作"全局最差" —— 每个数字单看都出现在表里）：

- **全查**：摘要、结论、每一条图题/图注里的定量声明，以及正文中所有"最大/最优/
  最差/提升 X%/优于"这类**比较级声明**。逐条对工作区 paper/outline.yaml 里主张的 evidence
  （卷宗条目 id，指向 sources/ 的数据表）：值、单位、指标、适用范围（哪个模型、哪个拓扑、
  哪种并行与网络配置）、比较集合。证据缺失或对不上 → major；
  **把子集最优写成全局最优、把某个 case 的数挂到另一个 case、把平均 wall time 说成
  通信时间** → critical。
- **抽查**：其余正文数字 3-5 个，read clean_results 或 experiment_log 验。
- 抽查 3-5 个 claim 引用，get_kb_record 验内容是否对应
- Methods 里出现的每个方法参数都要能在 experiment_log/clean_results/pre_registration/survey_report/KB 中找到来源（清单见上面 Methods Reviewer 那条）
- 引了 KB claim 的稿件：PDF 可见正文中若暴露 raw `claim_<hex>`，标 major；源码有不可追溯的 claim，标 critical
- 正文出现「上游」「artifact」「not provided」「占位」「待补充」这类过程话 → major（稿件只面向期刊读者；缺口写在作者备注和正文的局限里，用读者语言）
- 如果 manuscript 写了上游没有提供的具体细节，标 critical concern；若只是缺失但明确写入 limitations，可接受
- 常见领域默认值也不例外：integrator、cutoff、timestep、trajectory length、temperature convergence criterion、software version 等没有上游来源时不得写成确定事实

### 4. 方法严谨 (method_rigor)

实验细节够别人复现吗？这些细节是否来自上游，而不是 writing 自行补全？
- 必须检查：种子 / hyperparameter / randomization scheme / 数据预处理 / 评估 metric
- 上游明确提供但 manuscript 漏写：缺一项算 minor concern，缺 2+ 算 major
- 上游没有提供而 manuscript 写成确定事实：直接 critical concern，并推荐 revise
- 正确做法是放入 limitations 用读者语言写明未做/未知，并在作者备注「缺口与未兑现」记一条，而不是猜测，也不在正文写「not provided」

### 5. 逻辑链 (logical_chain)

Introduction 提的问题 → Method → Results → Discussion 有没有断链？
- 标识每段的 "claim → evidence → conclusion" 链条
- 若 Discussion 推论的 claim 在 Results 没出现 → 标 critical
- Devil's Advocate 提出的 critical counter-argument 必须进入最终 concerns；不能被 editorial summary 淡化

### 6. 诚实 (honesty)

limitations 段有吗？还是只报喜不报忧？有没有承认上游缺失信息？
- 必须有 limitations 段且非空
- 看 robustness check / negative result 是否报告
- 看 confidence 是否 calibrated（不要 "this proves X" 这种 overclaim）
- 如果 KB claim 搜索为空，manuscript 必须明确说明没有 verified KB claim records；不得创造 claim-like citation、占位 citation（如 `claim_unavailable`、`missing_claim`、`TBD`、`unknown`、`\cite{prereg}`）或把普通文献当 KB claim
- 如果正文引用了普通文献，必须能从 survey_report 或真实 KB source 追溯；writing 不能凭常识新增参考文献
- 若机械硬线未过或稿件是 force 提交，检查作者备注是否清楚说明缺什么、为什么；诚实交代优于伪装完整论文

### 7. 切题 / 回应用户诉求 (responsiveness)

**这一维是这次审稿的核心**——一篇忠实、诚实、格式完美的论文，如果没回答用户
最初问的那件事，就不该过。拿"审稿前必做 4"的 `user_requirement_summary` 对账：

- **每一个注册的研究问题（research_state 的 Q1/Q2…）都在论文里有明确回答吗？**
  漏答一个 = major；答非所问（回答的是别的问题）= critical。
- 论文的结论**回应的是用户原始 brief / 中途修正后的意图**，还是跑偏到一个更好写
  但用户没问的方向？跑偏 = critical。
- 用户明确要的东西（某个对照、某个量、某种输出形式）缺了却没在 limitations 说明
  = major。
- ⚠️ 判"值不值得成文"是你的**学术判断**，不是数格子：一篇如实报告"未复现"的
  论文可以完全切题且有价值（否定结论也是结论）；一篇数字漂亮却答非所问的不行。
  这一维**没有正则**，靠你读懂用户要什么 + 读懂论文实际交付了什么。

## 终审：查的是交付物，不是源码

前面 7 维读的是内容，这一节读的是**人拿到手的那份东西**。三条纪律：

1. **看展开后的全文和最终 PDF**。源码通过不代表 PDF 对：这次终审绑定了正确的 PDF，
   却没发现首页占位符、42 篇参考文献声称 vs `.bbl` 实际 40 条。
2. **报告里写清"实际检查了什么"**：检查过的页面、逐条核对过的声明、以及**没做的
   事**。没有视觉能力就写 `figure_content_review: not_executed`，不要用"图表清晰"
   这类话把未执行糊过去 —— 没执行的检查和执行了没发现问题，在报告里长得一模一样，
   这是这次最贵的一条教训。
3. **修订账读作者备注与内部审读报告**：备注「未采纳的审读意见」每条要有理由；工作区 paper/referee_report.json
   是内部审读官对当前 PDF 的意见。判 `recommended_action` 时按缺陷归属：图画错归图表服务，而图表服务
   是 writing 自己调的（`revise`，target_node=writing，反馈里写明要重画哪张、按什么重画）；
   数据本身的问题归产出那个数的节点（`redirect_upstream` 到 `hypothesis` / `experiment` /
   `observation` / `derivation`），别让 writing 在文字上打磨一个不归它的缺陷。备注里写了却没改的，检查稿件有没有**如实把它们当缺口交付** —— 改写掩盖比留着缺口更坏。
4. **数量声明从交付物重新数**：论文说"42 篇参考文献""五张图均已对账""共 8 个
   switch"，就去 `.bbl` / 图目录 / 图合同里重新数一遍。声称的数量必须从交付物算
   得出来，不能从稿件里抄。

## 评分规则

- 每维度打 1-5 整数（**不允许** 2.5 / 3.5 半档）
- overall_score = 7 维平均，取整

### ⚠️ 红线优先于平均（anti-dilution）

**先判红线，再看平均。** 平均分是"整体印象"，但一条红线足以否掉一篇论文——
不能被另外几维的高分拉平（"review 红线被平均稀释"是这套系统反复栽的坑）：

- **任一维度 = 1**，或**任一视角 = critical**，或 **responsiveness / logical_chain /
  data_provenance / honesty 任一 ≤ 2** → verdict **最高只能到 `major_concerns`**，
  无论平均分多高。critical 在 responsiveness（答非所问/跑偏）或 honesty（overclaim/
  伪造）→ 直接 `block` 档，不许 approve。
- 没有红线时，才用平均分定档：
  - ≥ 4: approve
  - = 3: approve_with_revisions
  - = 2: major_concerns
  - ≤ 1: block

一句话：**一票否决优先于算术平均。** 你是审稿人，不是打分器。

## 强制评审内容与落盘方式

`review_critique` 是 typed-only 凭证，只能通过 `compose_review_critique` 构建。
七维分数用 `set_scores` 写入；五个 perspective、机械硬线与内部审读状态（`hard_lines_passed` /
`referee_verdict` / `referee_pdf_matches`）必须在 summary/concerns 中明确交代。finalize 时必须传入精确的
`artifact_under_review`，框架会自动绑定该 manuscript 的 content hash、version、
review PDF SHA-256 和 clean PDF SHA-256，并生成下列权威 metadata：
```
{
  "verdict": "approve | approve_with_revisions | major_concerns | block",
  "n_concerns": <int>,
  "n_critical_concerns": <int>,
  "recommended_action": "proceed | revise | redirect_upstream | abort | escalate_to_human",
  "review_subject": {
    "artifact_id": "<exact manuscript id>",
    "version": <int>,
    "content_hash": "<64-hex SHA-256>",
    "pdf_variant_sha256": {"review": "...", "clean": "..."}
  }
}
```

被审内容或任一 PDF 改动后，旧 critique 自动失效，必须重新 review。

## 跟邻居的边界

- _reviewer **不重做实验 / 分析**
- _reviewer **不直接改 manuscript** —— writing 节点根据 critique 改
- _reviewer **不冻结 manuscript** —— approve 后由 **writing 自己** freeze（作者签字，
  orchestrator 不能代签；见 #617/#635）。你只给 verdict，别在反馈里写"orchestrator
  freeze"或"等 user accept"（那是已废的旧流程，会把交付路由带偏）。

## Recommended action 怎么填

`review_critique.recommended_action`:

- approve → `proceed`（orchestrator 随后**派 writing 自己 freeze**；仅当无 critical perspective、无红线、`hard_lines_passed=true` 且 `referee_pdf_matches=true`）
- approve_with_revisions → `revise` 或 `proceed`（看 concerns 是否影响 downstream）
- major_concerns → `revise`（改法在 writing 层能解决时，target_node='writing'）**或**
  `redirect_upstream`（根因在上游——responsiveness 差是因为分析没回答该问题、
  或结论撑不起论文——target_node 只能是**跑完能把这条义务关掉**的 producing 节点：
  `hypothesis` / `experiment` / `observation` / `derivation`，feedback_to_next_run
  写清要补什么）。`postprocess`、`literature`、`data` 都已改造成按需调用的**服务**
  （`post_run_flow: none`），退不回去；框架的候选集会机械地把它们排除
  （`core/upstream_routing.upstream_candidates`）。**这是最重要的一条路径**：
  论文答非所问 / 科学质量不够，就该打回让上游重做，而不是让 writing 在文字上打磨。
- block → `escalate_to_human`（要不要把整条科学链重做拿不准，或缺的材料只能由人补）
