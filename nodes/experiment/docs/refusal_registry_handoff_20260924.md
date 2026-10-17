# Experiment v3 拒绝点登记交接（2026-09-24）

基线：`lujy/impl-experiment-v3 @ 86be10944f6b92c6c1cbdd881782f0c16101bdd4`。
目标文件 `docs/verdict_demolition/refusal_registry.yaml` 与 `refusal_baseline.json`
由 framework owner 维护；Experiment owner 为 lujy。本件只提供当前代码核对和建议，
**不是 owner 已批准的登记，也不修改共享文件或抬高 baseline**。

本次仅对下面七个文件复用仓库已有 AST 谓词作只读核算，没有运行全量门禁。
“净欠”是当前扫描值减 baseline 和根表已登记条目数，不等于所有新增控制条件，
也不能证明既有条目与现存代码语义逐点一致。此前固定 `05a29555` 根单文件测试为
1 failed（信箱141号）；本件不把它改称当前全框架测试结果。

## 1. 当前计数快照与复现

以下文件均位于 `nodes/experiment/tools/`。

| 文件 | 现扫描值 | baseline | 根表现登记 | 净欠 |
|---|---:|---:|---:|---:|
| action_boundary.py | 3 | 0 | 0 | 3 |
| delivery_transaction.py | 1 | 0 | 0 | 1 |
| finish.py | 11 | 0 | 0 | 11 |
| input_admission.py | 1 | 0 | 0 | 1 |
| run_bootstrap.py | 3 | 0 | 0 | 3 |
| operation_completion.py | 40 | 26 | 12 | 2 |
| resource_manager.py | 107 | 96 | 10 | 1 |
| **净欠合计** | | | | **22** |

在上述固定检出中运行，标准库即可；不 import 产品运行时，不写缓存或 baseline：

```bash
git rev-parse HEAD
python3 -B - <<'PY'
import ast
import collections
import json
import re
from pathlib import Path
from scripts.scan_refusal_sites import (
    BASELINE, scan_file, _return_is_refusal, _raise_is_refusal,
)

# 当前根表以两空格缩进的 "- file:" 存储 entries；这里只计该已核实形状。
# owner 若改变 YAML 形状，应改用仓库测试环境中的 yaml.safe_load 读取 entries。
text = Path('docs/verdict_demolition/refusal_registry.yaml').read_text()
registered = collections.Counter(re.findall(r'^  - file: (\S+)\s*$', text, re.M))
baseline = json.loads(BASELINE.read_text())
new = ('action_boundary', 'delivery_transaction', 'finish', 'input_admission', 'run_bootstrap')
files = (*new, 'operation_completion', 'resource_manager')
total = 0
for name in files:
    path = Path(f'nodes/experiment/tools/{name}.py')
    current = scan_file(path)
    base, registered_count = baseline.get(str(path), 0), registered[str(path)]
    missing = max(0, current - base - registered_count)
    total += missing
    print(f'{path}: current={current} baseline={base} registered={registered_count} net_missing={missing}')
    if name not in new:
        continue
    tree = ast.parse(path.read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in sorted(ast.walk(tree), key=lambda item: getattr(item, 'lineno', 0)):
        is_site = (isinstance(node, ast.Return) and _return_is_refusal(node)) or (
            isinstance(node, ast.Raise) and _raise_is_refusal(node))
        if not is_site:
            continue
        scope = node
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        codes = []
        for value in ast.walk(node):
            if isinstance(value, ast.Dict):
                for key, item in zip(value.keys, value.values):
                    if isinstance(key, ast.Constant) and key.value == 'error_code':
                        codes.append(ast.unparse(item))
        print(' ', getattr(scope, 'name', '<module>'), node.lineno,
              ', '.join(codes) or ast.unparse(node)[:100])
print(f'net_missing_total={total}')
PY
```

本扫描器按 return/raise 的固定 AST 形状计数，而非按每个 error code 计数。
例如动态错误信封工厂计一个 return，内部自定义异常的各个 raise 不一定被计入。
不能改 helper 名、拆返回形状或把充分性判断藏进异常来使门禁变绿。

## 2. 五个新模块：19 个扫描位点逐点建议

行号固定于上述基线，后续迁移按“函数 + code/条件”重新定位。
A=安全/资源，B=记录完整性，C=物理/协议，collector=事实/义务收集。
以下均为建议，由 owner 按当前语义审核后决定；不能仅凭计数相等批量复制。
工厂条目只登记当前信封构造位点，不授权以后增加未经审查的拒绝条件。

### action_boundary.py（3）

| 函数与行号 | code / 触发条件 | 建议 class 与依据 | 恢复出口及副作用边界 |
|---|---|---|---|
| `BoundaryRefusal.as_result:110` | `bootstrap_incomplete`；v3 尚在 initializing | C：启动协议未完成，不是任务类别判断 | 未 spawn/提交；runtime 下一轮修复或重放 bootstrap，再重试原动作，不补分类手续。 |
| `BoundaryRefusal.as_result:120` | `action_not_durably_recorded`；动作意图未可靠落账 | B：副作用前缺少持久动作身份 | 未 spawn/提交；runtime 修复 ledger 后重试。不能让模型凭口头承诺执行。 |
| `record_completed_effect:345` | `bootstrap_incomplete`；副作用之后的记账入口发现 initializing | B：已有副作用无法作为正常 v3 事实交付 | 返回 `side_effect_committed=true`、`payload_must_not_rerun=true`；先保留现场，由 runtime 恢复或接管记账，不能重跑载荷。不同于上一条“尚未执行”。 |

### delivery_transaction.py（1）

| 函数与行号 | code / 触发条件 | 建议 class 与依据 | 恢复出口及副作用边界 |
|---|---|---|---|
| `DeliveryTransactionError.as_result:75` | 动态 `self.code`；manifest/三件身份、冻结、commit 或交付封套不满足既有契约 | B：已有 C 事实的完整性错误信封 | 纯调用形状错误可纠正；中断事务复用原 manifest 恢复缺失阶段；摘要/身份冲突交当前 owner，不能覆盖已冻结事实或重跑物理作业。这是工厂，不是每个 code 都新增一次额度。 |

### finish.py（11）

| 函数与行号 | code / 触发条件 | 建议 class 与依据 | 恢复出口及副作用边界 |
|---|---|---|---|
| `_FinishAssemblyError.as_result:634` | 动态 `self.code`；当前 A 投影、正式输入/封套或 blocker 参数不一致 | B：组装 C 的身份/记录错误信封；参数子类属于 C 语义，工厂整体分类请 owner 确认 | 参数可纠正后重试；A/身份损坏交其 owner。不能重写 A、恢复 scope 门或补平行事实；不授权未来新增调用点。 |
| `_owner_ledger:1201` | `ledger_unavailable`；打开持久账本异常 | B：无法读取/写入唯一执行事实源 | 修复账本访问/损坏后再收尾，不重跑已经发生的动作。 |
| `_owner_ledger:1204` | `not_run_owner`；账本只读，调用方不拥有本 run 写权 | C：写入所有权协议 | 由现 owner 完成，或经受支持的正式接管恢复；不伪造 ownership。 |
| `_finish:1245` | `finish_parameters_invalid`；未知 kwargs | C：接口形状不匹配 | 按当前 schema 去除/改正参数后重试，不添加新任务要求。 |
| `_finish:1249` | 同 code；outcome 不在词表 | C：终态词表协议 | 选择与事实相符的合法 outcome，不把 failed 改称 success。 |
| `_finish:1252` | 同 code；summary 非非空字符串 | C：必要交付摘要字段形状 | 给真实摘要后重试，无需重新执行。 |
| `_finish:1257` | 同 code；deliverables 非对象数组 | C：交付对象表示形状 | 修正数组/对象形状，仍交付真实已有对象。 |
| `_finish:1262` | 同 code；limitations 非字符串数组 | C：限制字段形状 | 修正形状，不删除真实限制来取悦判据。 |
| `_finish:1265` | 同 code；next_step 非字符串 | C：交接字段形状 | 修正形状后重试；不要求额外执行步骤。 |
| `_finish:1269` | `bootstrap_incomplete`；未建立 ready 的当前 A | C：新 C 缺少规范启动协议 | runtime 恢复 A/启动；已有 manifest 恢复分支先于此检查，不要求调用者重填或改写旧交付。 |
| `_finish:1287` | `finish_parameters_invalid`；`VerificationError`，核验方法字段不合法 | C：方法 schema，不是对科学价值作判决 | 修正 file/glob/check 字段后重新 finish；prepare 在执行检查前校验形状，不能借修正方法降低 A。 |

`success_unsupported`、`clarification_required` 等不在该扫描器的拒绝 status 集合中，
不因本表没有计它们就声称“没有行为约束”。本件没有给这些行为新增登记额度或改变其语义。

### input_admission.py（1）

| 函数与行号 | code / 触发条件 | 建议 class 与依据 | 恢复出口及副作用边界 |
|---|---|---|---|
| `check_consumed_input:89` | `formal_input_not_ready`；本动作实际选择的正式输入未核验通过或审计不可读 | B：真实消费输入的身份/交付一致性 | 未启动/提交；按明细修复实际绑定或包结构、重试本动作；无法取得则诚实 blocked。无关准备、诊断、取消与 finish 仍可达；不得删掉真实消费 selector 绕过。 |

### run_bootstrap.py（3）

| 函数与行号 | code / 触发条件 | 建议 class 与依据 | 恢复出口及副作用边界 |
|---|---|---|---|
| `_startup_blocker:44` | 动态 `code`；返回启动事实/输入缺口描述 | collector：构造 A 中 blocker 事实，本身不执行或决定全局成功 | 合法来源补齐/纠正输入，或只继续不消费缺失输入的动作；可诚实 non-success。不得把 helper 当成恢复分类门的额度。 |
| `materialize_writable_roots:268` | 无独立 error_code；读取 path roles 异常 | collector：汇报本次目录物化无法完成，未在此启动载荷 | 修复角色来源再幂等重试；不扩张可写范围，也不由模型猜路径授权。 |
| `materialize_writable_roots:293` | 无独立 error_code；一个或多个可写根创建失败，条件式 status=error | collector：返回 created/skipped/failures，保留实际物化事实 | 已建目录不假记回滚；修复具体权限/路径问题后幂等补缺，不能删除已有目录或重做载荷清账。 |

## 3. 遗留两文件：仍需 owner 对账，不能按净数凑条目

- `operation_completion.py` 净欠 2。相对公共基点 `e4f0b3f3` 的 diff 明确新增
  `_record_operation_completion` 的 `v3_finish_required` 与 `delivery_protocol_unreadable`
  两个 return，分别阻止退役 writer 写 v3 三件套、阻止未知协议退回旧 writer。
  可供 owner 按 C/B 审核；出口分别为统一 finish、由当前 run owner 修复/接管协议。
  两者都未产生该次写入副作用。这只能定位本批新增候选，不能替代对根表现有12项的语义去重。
- `resource_manager.py` 净欠 1。源历史含 `_submit_sync` 的
  `bootstrap_incomplete` 和 `submission_intent_not_durably_recorded` 两条登记理由；
  当前扫描器只直接计前者，后者通过 helper 展开，不是第二个同形 return。
  仍须 owner 对照根表现有10项与本批 diff 唯一匹配，**不把两条一起追加以制造余量**。
  前者建议 C，启动未就绪时不提交；修复 bootstrap 后重试。提交意图未持久化仍应
  保留其真实保护与恢复说明，不能因为 scanner 看不见就删除或改写它。

相关只读对照：

```bash
git diff e4f0b3f3 86be1094 -- nodes/experiment/tools/operation_completion.py
git diff e4f0b3f3 86be1094 -- nodes/experiment/tools/resource_manager.py
git diff 86be1094 15960115 -- docs/verdict_demolition/refusal_registry.yaml
```

## 4. 为什么不能直接沿用旧交接/旧源历史

既有 `refusal_registry_015_entries.yaml`、`refusal_registry_017_operation_completion_entries.yaml`、
`refusal_registry_044_p0a_prereg_assignment_entries.yaml` 和
`refusal_registry_batch_0922_entries.yaml` 是此前批次材料，部分条目已在根表。
其中0922文件替代044的两节，不是可与044累加的另一笔额度；不要重复追加。

保留源提交 `15960115` 相对当前根表还有80行历史登记，但不能原样搬回：

- 包含已退役的 `scientific_closure_not_supported`、`scope_undecided`、
  旧 finish 内逐件冻结和 `closure_recovery_required` 解释；当前事实与恢复入口已变。
- action_boundary 第三条写 `begin()`，当前扫描点实际为副作用后的
  `record_completed_effect`；“尚未执行”和“已发生、禁止重跑”不能混用。
- finish 恰好仍是11个扫描点，不代表旧11个条目与当前11点同义。
- resource_manager 的两个旧理由也不能直接抵当前净欠1；代码形状与根表重叠须先核对。

Owner 落地时按当前函数、条件和退出路径审核上述建议，核实遗留条目是否仍成立，再修改
根 registry；只在真实删除旧位点时按仓库规则收紧基线，绝不提高或新增 baseline。
最后由 owner 在合并后的固定树运行 `tests/test_refusal_sites_are_classified.py`，记录基线与结果。
节点开发可继续，不把这笔跨 owner 登记等待扩成全部工作的阻塞。
