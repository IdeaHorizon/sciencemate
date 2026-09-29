# IEIT Research Platform — product acceptance suite

This suite is a product-quality gate, not a connectivity smoke test. A case passes
only when the scientific result is useful, the durable record is correct, and the
Session UI remains understandable to a researcher who has not read a trace.

## Test environment

- Dedicated Project: `Platform Acceptance Lab`
- Project id: `b5bc3797-73b6-44ef-8da7-71558966bdfa`
- One new Session per case. Cases never share conversational state.
- Real model cases use the selected environment-backed provider. Deterministic
  fixtures verify protocol and rendering, but cannot satisfy content acceptance.
- Every result is reviewed in both the canonical APIs and the browser UI.

## Cross-cutting acceptance rules

1. A plain answer shows the answer, not an execution dashboard.
2. Completed technical work collapses to one human sentence; a user may expand it.
3. Internal lifecycle events (`loop_start`, `hook_injection`, scratchpad writes,
   request plumbing) never appear as user-facing steps.
4. Tool arguments, JSON payloads, stack traces, provider responses, Run ids, token
   counts, and retry diagnostics are Trace details, not the conversation hierarchy.
5. A failure appears once, states what was and was not produced, and offers one safe
   next action. The UI never combines `Failed`, `Stale Unknown`, a raw `409`, and a
   second generic error for the same incident.
6. Artifact labels are typed and truthful. A PNG previews as an image; a PDF reports
   its page count and downloads as a PDF; text wrappers are not accepted substitutes.
7. Generated claims, citations, parameters, seeds, environments, and provenance are
   inspectable. “Completed” is not evidence that the content is correct.
8. Settings identify their scope, owner, enforcement semantics, and when changes take
   effect. Controls without a server contract must be explicitly unavailable.

## Cases

### QA-01 — concise instruction following

Prompt:

> 请只用一句简洁中文回答，不要调用任何工具：为什么科研报告必须区分观测与推断？

Pass criteria:

- Exactly one Chinese sentence answers the question.
- No Tool or Activity group is rendered.
- A durable user/assistant message pair and completed Run exist.

### CTX-01 — authorized Project information

Prompt:

> 只根据当前项目上下文，告诉我项目名称、当前模型后端和基线修订号；不要猜测缺失信息。

Pass criteria:

- The three values match the canonical Project/Session records.
- At most one collapsed “Read project context” activity is visible.
- Missing values are labelled unavailable rather than invented.

### LIT-01 — targeted literature research

Prompt:

> 调研 heterogeneous Mixture-of-Experts（异构 MoE）的专家并行、路由与负载均衡。给出至少 5 篇主题真正相关且可核验的论文，区分已核验事实与综合判断，形成简短调研报告和文献索引。没有全文不等于没有可核验元数据，不要因为缺少全文停下来问我。

Pass criteria:

- At least five identifiable papers with title plus DOI/arXiv/URL.
- Manual title inspection yields at least 80% topical precision; matching only the
  Chinese word “异构” fails.
- Claims distinguish source-backed observations from synthesis.
- `survey_report` and `literature_index` are staged, openable, and readable.
- UI shows one research activity and two artifact links, not every internal call.

### CALC-01 — transparent calculation

Prompt:

> 用 Python 计算 [1,2,3,4,5] 的均值和总体标准差，明确公式、标准差约定和精确结果；不要生成无关文件。

Pass criteria:

- Mean is `3.0`; population standard deviation is `sqrt(2)` (approximately
  `1.4142135624`).
- The response states that population, not sample, standard deviation was used.
- One collapsed compute row exposes code/output on demand.

### EXP-01 — reproducible small experiment

Prompt:

> 做一个固定随机种子 42、样本数 10000 的 Monte Carlo 圆周率估计实验。记录方法、参数、结果、绝对误差、Python 版本和依赖环境，并保存可复现实验记录。

Pass criteria:

- The recorded seed and sample count match the request.
- Result and absolute error are numerically consistent with the saved data.
- `experiment_log` contains method, parameters, environment, outputs, and provenance.
- UI summarizes the result; parameters and environment are expandable.

### FIG-01 — real figure artifact

Prompt:

> 基于本 Session 的 Monte Carlo 实验，绘制样本数量增加时圆周率估计的收敛图。坐标轴、单位、图例和中文图注完整，保存为 PNG 并说明数据来源。

Pass criteria:

- A valid PNG signature and image MIME type are stored; no JSON/text wrapper.
- Axes and caption explain the series and provenance.
- UI provides a real preview and download only after the binary contract succeeds.

### PDF-01 — readable report artifact

Prompt:

> 把本 Session 的实验方法、结果、收敛图、局限性和可复现信息整理成一份简洁技术报告 PDF。只引用实际存在的实验产物，不编造参考文献。

Pass criteria:

- A valid PDF signature, PDF MIME type, and working download are present.
- Rendered pages have no clipping, missing glyphs, empty figure, or unreadably small text.
- Report claims and figure link back to the experiment artifacts.

### ENV-01 — bounded environment deployment

Prompt:

> 在隔离目录创建一个最小 Python 虚拟环境，运行只依赖标准库的健康检查，记录 Python 版本、平台、命令、退出码和环境清单；不要启动持久服务。

Pass criteria:

- Work occurs only in an authorized, isolated directory.
- Health check exits zero and recorded metadata matches the actual runtime.
- UI shows one deployment summary with manifest and health details on expansion.

### REC-01 — safe recovery after process loss

Procedure:

1. Start a case that pauses for a human Decision.
2. Restart the App Server, preserving the database and Session workspace.
3. Reopen the Session and attempt to continue.

Pass criteria:

- The old Run becomes one honest non-resumable state; it never appears Running.
- One primary explanation says the in-memory executor was lost while durable outputs
  remain intact.
- One action creates a new linked Session from the last durable point. No raw `409`
  is shown in the conversation.

### PERM-01 — roles and settings semantics

Procedure:

- Inspect the same Project as institution administrator, group administrator, and
  researcher. Inspect model, personal research, and Project settings.

Pass criteria:

- Each role sees only authorized actions.
- Model scope is shown as personal, group, or institution, including who is affected.
- Personal research guidance says it is frozen when a new Session starts.
- Institution/group policy is not presented as editable personal prompt text.
- Placebo or not-yet-connected controls are disabled with a plain explanation.

## Run report format

Record for every case:

- Session URL and Run id
- provider/model and start/end timestamps
- final status and retry count
- artifact ids, types, MIME types, hashes, and download verification
- content checks with expected versus observed values
- UI review: hierarchy, labels, collapsed state, error/recovery behavior
- verdict: `PASS`, `FAIL`, or `BLOCKED`, with the exact repair commit

The platform release gate is all ten cases passing. A deterministic fixture pass
plus a failed live-model case is still a failed release.
