---
name: scientific-schematic
description: Build evidence-faithful workflows, mechanisms, experimental setups, anatomical diagrams, and conceptual schematics from structured nodes, edges, labels, and constraints.
---

# Scientific Schematic

Represent the figure as structured components: nodes, edges, groups, labels, direction, hierarchy, and declared icon or asset sources. Use layout to reveal mechanism or sequence, not to decorate it.

**这个表示法有地方放**：`declare_figure_contract(asset_kind="schematic", ...)` 收的就是
nodes / groups / bands / edges / assertions，框架按它算几何并渲染。所以本 skill 说的
「structured components」不是一种写法建议，而是这个家族**唯一**的出图路径 —— 手写坐标
的代码不会被接受。schema 与可照抄的例子用 `figure_contract_schema(asset_kind="schematic")` 取。

Do not invent quantities, causal arrows, molecular structures, apparatus details, or anatomical features. Visually distinguish measured, modeled, hypothesized, and procedural elements when they coexist. Keep text concise and connections unambiguous.

Prefer deterministic vector drawing. Generative imagery may be used only for non-evidence decorative context when the request explicitly allows it; it must never synthesize scientific evidence or replace a domain-faithful diagram.

Load `references/schematic-grammar.md` for mechanism, apparatus, workflow, hierarchy,
feedback-loop, compartment, or measured-versus-hypothesized encoding decisions.

## 视觉工艺（54 轮真跑量出来的，不是审美口味）

框架已经替你定死了配色、字号、线宽 —— 那些不归你选（色相=类别、亮度=层级、
尺寸=分量，三条编码规则不许串台）。**归你选的只有三件事**，每一件都有量出来的后果：

### 一、规格写在组件上，还是写在图例里
两者都不算「漏」（事实都在纸上），但读者的代价不同：**写在组件上能一眼读完，
写在图例里要来回对照**。用户给的参考图 17/19 个节点带副标题（GPU 上写
`RTX PRO 6000`、网卡上写 `400 Gbps`）—— 这是它「看起来更专业」的主要来源之一。
判断依据：**这条规格是读者看这个盒子时就想知道的吗**？是 → `sublabel`；
只是分类信息 → 交给 `role` 和图例。

### 二、谁是主角（`emphasis`）
标 **一两个**，别的不写。实测 agent 自己标对过：一张「两个 switch 各挂 4 张 GPU」
的图，把两个 switch 标成 `primary`，全图立刻有了视觉重心。
全体 primary 会被当作自相矛盾**拒绝录入** —— 强调是相对关系，需要安静的多数做底。

### 三、画幅不是你能直接拧的旋钮
这一条我写错过两次，两次都是编的因果，留在这里当教训：

1. 「3 块≈画幅 1.0、4 块≈0.86」—— 从 45 个数据点里挑了两个拼的。
   全量核：3 块 n=12 均值 0.86（0.67–1.04）、4 块 n=33 均值 0.81（0.70–1.04），
   **范围几乎完全重叠**。
2. 「减少最宽一行的元件数就能横过来」—— **方向还是反的**：
   实测 r(最宽行, aspect) = **+0.40**，元件多反而更横。

第三次不猜了。全量 11 张图核过：最宽行 r=+0.40、行数 r=+0.11、两者之比 r=−0.03，
**没有一个解释画幅**。画幅是布局里许多件事相互作用的结果。

所以：**别花轮次去猜怎么把画幅调横。** 声明完读 `render_figure` 回的
`layout.digest` 拿真实的数；想比较两种编排就都声明一次看哪个好，不要照经验法则调。

**量过的事实只有一条**：一句话的注记不要单独成块 —— 分栏边框加标题条一块固定
55pt，而一行注记内容只有 17pt（效率 22%）。框架会把没标题的 note 块降级成页脚。

### 还有一件事：这张图印出来多大
`medium`（single_column 84mm / double_column 170mm / slide / poster）决定画布的
预算。调用方在 `constraints.width` 里说了就按调用方的；没说按 purpose 推。画布的
pt 数**就是**物理英寸数 —— 一张 420mm 宽的图放进单栏，11pt 的标签只剩 2.2pt。
`declare_figure_contract` 在声明那一刻就算出印出来最小的字还剩几 pt（印刷下限
7pt），不够就拒绝，并带回**算过的**改法：独立单元几个一行（六面板 3×2 而不是
2×3）、拆成两张图、去掉最小那一档的文字。把字号调大没有用 —— 整张图一起缩；
有用的是让一行里并排的东西变少。多个分栏（blocks）没写 row 时框架自己按版心
选并排方式（一行最多 3 栏）。同一个 request_id 最多渲染 3 次。

## 语义自查清单（渲染后逐条过）

- 示意图**不得走私定量语义**：出现坐标轴、误差棒、带刻度的 y 轴或"成本/性能"这类量纲标签时，它就不再是 schematic —— 改用定量图型并绑定真实数据。
- 每条箭头/连线都对应合同 `edges` 里声明的一条（框架按声明画，声明漏了图上就没有；
  没声明的也不会冒出来）。没有自己发明的因果箭头、组件或装置细节。
- 没有孤立组件：画在图上却不连任何东西的节点会被机械拒绝，除非显式 `isolated: true`。
- measured / modeled / hypothesized / procedural 元素共存时有视觉区分（线型/填充/标注），且图例说明该区分。
- 阅读顺序无歧义：用 `bands`（顶层的行）与 `ranks`（组内的行）表达层次；交叉与避让由
  框架的布线器处理，绕不开时它会记一条 OB-LAYOUT finding 给你看。
- 全部为确定性矢量绘制；生成式像素只允许非证据装饰且请求显式允许（render_figure 要求 evidence_bearing=false 声明）。
