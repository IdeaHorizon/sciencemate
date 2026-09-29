# 调度器路由：派得对，但"该不该派"没有依据

2026-08-23 · 三个真 orchestrator 会话（本机 `run_node --harness _orchestrator --sandbox`）

## 为什么要单独测这个

此前 derivation 的全部测试都是 benchmark driver 直接调
`run_node(derivation)` —— **绕过了调度器**。「节点自己能不能推理」测过了
（21 题三臂 + 若干真跑），「调度器会不会在合适的时候派它」一次都没测过。

三个课题，prompt 里**绝不出现节点名**（那是喂答案）。

## 结果

| 课题 | 派给 | 判断 |
|---|---|---|
| 纯推导：量子简谐振子热容闭式解 + 高温极限，明说"纯解析、不要新数据、要每步核验" | **derivation** | ✅ 对 |
| 要数据：Ising L=16 跑蒙特卡洛测 Tc、看 Binder 交叉 | hypothesis | ✅ 对（正路，没误派）|
| 模糊：「帮我看看这个不等式对不对：∀实数 a,b, √(a+b) ≤ √a+√b」 | **不派，自己答** | ⚠️ 见下 |

### 第一个：派发质量出乎意料地好

```
node_type: "derivation"
mode: "confirmatory"
rigor_level: "L1.5"          ← 主动选了区间认证档
propositions: [P1..P5]        ← 结构化、编号、含极限论证与收敛阶要求
known_results: [K1..K3]       ← 带外部锚点（Pathria、Einstein 1907）
```

子节点跑出 21 步链、账本 50 条（5 verified / 42 numerically_supported /
1 failed / 2 inconclusive）、P1–P5 每条都有 status + evidence 指向步骤区间，
**并且主动用上了新接的 `interval_check`**。

### 第三个才是真问题

调度器 `turns: 1, tools: 0`，一轮答完，数学上**完全正确**：非负时两边平方、
负数时 √ 无定义并给出 a=1,b=−3、复数无序关系。

同一道题在 benchmark 里直接派 derivation 跑，得到的是 17 轮、**6 条账本可
反查的验证章**、冻结产物、完整假设账本、`verdict=refuted`。

**两个答案一样对。差别是有没有可核验的记录。**

调度器的判断在「效率」维度上没错 —— 这道题起一个 producing run 确实是杀鸡
用牛刀。但如果用户要的是**可审计的科研记录**，这个判断就错了。而它没有任何
依据去做这个区分。

## 根因：机制在，引导不在

调度器**能**派 derivation，靠的是框架扫盘注入的「🔌 可调子节点契约」
（`core/loop_hooks_builtin._callee_contracts_on_turn_start`：
`list_harnesses()` + 各节点 `expected_inputs`）。这条路是机械的、扫盘的，
所以新节点自动出现 —— 设计是对的。

但引导侧是空的：

| 节点 | `nodes/_orchestrator/harness.yaml` 里被点名 |
|---|---|
| writing | 20 |
| experiment | 17 |
| hypothesis | 14 |
| literature | 9 |
| data | 7 |
| postprocess | 3 |
| **observation** | **1**（还是讲 memory 的那句）|
| **derivation** | **0** |

契约注入告诉它「这个节点存在、参数这么传」，**没有任何东西告诉它「什么时候
该派」**。第一个课题派对了，是因为用户自己在 prompt 里写足了信号
（"纯解析工作"、"不需要模拟数据"、"要一条每一步都站得住的推导链"）。
把这些信号拿掉（第三个课题），它就不派了。

**一次成功不能证明这条路稳** —— 它现在依赖用户替调度器把判断做完。

## 顺带：两个较新的证据模态都缺席

observation 也是 1 次（而且不是讲调度的）。这不是 derivation 一个节点的问题，
是**三种证据模态的选择逻辑从来没进过调度器引导** —— 老的六个节点是按流程
顺序（hypothesis → literature/data → experiment → writing）被反复点名的，
而 experiment / observation / derivation 之间「这个问题该用哪种取证方式」
这个判断，引导里一个字都没有。

## 待拍板：要不要改 orchestrator 引导

改动本身不大（补一段「三种证据模态怎么选」+ 一条「什么时候值得留下可核验
记录」），但 `_orchestrator/harness.yaml` 影响**全平台每一次调度**，
不该顺手改。两个设计问题需要先定：

1. **"值得留下可审计记录"的判据是什么？** 不能是"题目难不难" ——
   第三个课题不难，但如果它是论文里要引用的一条引理，就该有记录。
   可能的判据：这条结论会不会被下游引用 / 进 KB / 写进论文。
2. **会不会矫枉过正？** 引导写重了，调度器可能对每个随口问题都起
   producing run —— 那比现在更糟（`turns:1` 的直答本身是对的行为）。

我的倾向：判据挂在**「这条结论要不要被引用」**上，而不是挂在问题类型上。
用户随口问 → 直答；进了 project、要被 writing/KB 消费 → 派 derivation。
但这需要 wangd 拍板，我没动。
