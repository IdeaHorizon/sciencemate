# S2-min 准备切片：执行上下文进入已有 B 记录

## 范围与结论

基线：`codex/060-r3a-c @ 2937f70a4c6192e60756f1fe8469a555ba2f1689`。
本切片只补科学执行所需的可恢复事实，不启用 scientific success，不引入新账本、
工具、授权协议、路线或收尾状态机。不改 Core；不推产品分支，不提 main PR。

旧问题是：v3 移除路线后，旧科学审计的 route applicability 不再成立；
参数与输入见证还主要留在 transcript 或可变 hook_state，不能直接说明
“哪个动作当时看见了哪些正式输入、申报了哪些参数”。

改动复用同一入口：

- 从精确 prereg metadata 提取纯参数比较器，旧 preflight 也调用它，不复制比较算法。
- `action_started` 与提交前持久化的 `intent` 直接保存 `execution_context`。
- 上下文引用当时 A 的事件序号、选中的 Core 合同及精确 prereg 绑定；
  保存本动作的输入 selector、已有 admission 的结果、工具参数见证及比较结果。
- bash、Python 和受管提交使用同一上下文读取器；`project_actions` 如实投影，
  因此已有 raw/clean 渲染无需再从缓存猜科学参数。
- 输入审计只执行原来那一次；未选择输入记 `not_selected`，没有适用的正式验收
  记录则记 `not_performed`，不把两者包装成 verified。
  明示来源为工具入口 preflight 及其观测时间，不声称等待授权后输入字节仍被原子锁定。

新增字段的 owner 是执行工具边界；事实写入既有 append-only B 记录后不可变。
后续动作另记自己的上下文，不覆盖先前参数，不在 run 内合并参数。
消费者是既有 action 投影、raw/clean 交付和后续 S2 参数/偏离核验。
恢复读取同一事件；清理策略沿用 B 账本，不另建生命周期。

## 不可越过的语义边界

`execution_params` 标为 `tool_argument_witness`：它是调用参数，不是从进程内部
测得的实际参数。比较通过只证明该见证与冻结承诺相等，不证明载荷确实使用它，
也不批准偏离。实际使用情况仍需任务约定的配置、日志、结果属性或机械检查作证。

本片不按参数或命令文本推断“消费了正式输入”，也不把 scientific run 中的
准备、诊断、编译一概当科学执行。无参数声明时记未观察，不伪造 match。
已有输入 admission 不改变；存在但未验收的父转交包仍是明确的剩余集成缺口。

比较器保留旧语义，不在抽取时顺便改变参数类型规则：Python 相等性下 `True == 1`
及旧 version 判断等问题仍待独立修正。不能据此声称具备严格类型级参数保证。

## 验证

- 原基线真实 bash 的四个反例全部在缺失 `execution_context` 处转红，
  不是因执行器 mock 或无效 fixture 转红。
- 接线后上下文、输入准入、记账边界及真实 CMake 正反例：35 passed。
- 纯参数比较器及完整旧 preflight：30 passed。
- bash/Python 用真实沙箱短进程；提交意图测试用显式模拟的调度器，
  模拟不确定回执仍保持 unknown，不声称跑过真实 Slurm。
- 节点回归（Python 3.12）：3764 passed、20 skipped、5 既有 xfailed。
  首次发现 10 条旧测试替身不接受新增的执行器内部参数；只同步该替身的两个
  可选参数，未降低其原有断言、未为测试添加生产兼容分支，复跑通过。
- S2 成功规格单独运行：2 failed、3 passed；两个正例均在真实执行与产物检查
  通过后被现存 `scientific_success_not_supported` 拒绝。完整五格保存在信箱
  `060-guided-short-loop/65-pending-scientific-delivery-spec.py`，下一片放回
  `tests/test_060_s2_scientific_delivery.py`，不加 xfail、不改为期望拒绝。
  它不属于本片已实现功能，不能以本片回归通过声称完成 S2。
- 独立只读审查未发现本轮接线可复现的 P0/P1；明确 job 上下文目前保存在
  intent 原始记录，尚未添加到 job 展示投影。

## 下一片，而非本片已完成的能力

1. 精确界定当前动作实际消费的正式约束，保留已知实质冲突的授权边界；
   不把 `record_prereg_deviation` 当批准，也不新造节点批准协议。
2. 在同一 finish 中接入 scientific 正例、与交付 lineage 有关的偏离与缺口，
   保留所有诚实终态，不恢复旧 verdict/sediment 通关要求。
3. 清理并实际加载 scientific skill，形成与新工具面一致的专业方法引导。
4. 真实模型/UI、最终树 Slurm 与 Core 全路径完成判定仍未验收。

Claude 本日离线不影响此切片；设计、实施由主 Codex 推进，子代理分别做
纯比较器、scientific 规格和只读独立审查。信箱用于可恢复交接，不作为进度前提。
