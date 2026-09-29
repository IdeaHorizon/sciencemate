# `nodes/` —— 节点 owner 的工作空间

每个节点是一个独立 owner 负责的研究 agent。Framework 提供运行时和契约；
owner 在自己节点目录内自由组合。

## Ownership

| 节点 | 性质 | Owner |
|---|---|---|
| `literature` | 文献调研 | TBD |
| `hypothesis` | 提假设 | TBD |
| `data` | 数据准备 | TBD |
| `experiment` | 实验执行 + per-hypothesis verdict + sediment（v2.0 起合并老 analysis 职责）| TBD |
| `observation` | 检视式取证（系统性地看世界已留下的记录） | TBD |
| `derivation` | 演绎式取证（从已承诺的前提推出新命题）★ 2026-08-22 新增 | TBD |
| `postprocess` | 实验后处理 | TBD |
| `writing` | 论文写作 | TBD |
| `_curator` ★ | KB 整合（framework 基础设施） | **wangd** |
| `_orchestrator` ★ | pipeline 路由（framework 基础设施） | **wangd** |
| `_reviewer` ★ | 单 artifact 审稿 + 项目级宏观综合（v2.0 加 scope='project_synthesis'，兜底原 analysis 的项目级思考职责）| **wangd** |

下划线 `_` 前缀 = framework 基础设施节点，**不开放给同事改**。

## 同事**能改 / 不该碰**

```
✅ 你能改：nodes/<your_assigned_node>/  ← 你的节点目录内
   ├── harness.yaml          ← 主战场（占 95% 工作）
   ├── tools/                ← 节点专属工具（少数情况）
   ├── skills/<name>/        ← 节点专属 skill recipe（少数）
   ├── fixtures/             ← 测试 fixture
   ├── hooks.py              ← (可选) 节点专属 loop hook
   ├── summarizer.py         ← (可选) 节点专属 context 压缩
   └── agent_loop.py         ← ★ (高风险，慎用) 自定 agent loop

❌ 你不该碰：
   • core/                   ← framework 心脏，要改走 framework PR + wangd review
   • shared/                 ← 跨节点共享代码
   • nodes/_curator/         ← framework 基础设施，wangd 保留
   • nodes/_orchestrator/    ← framework 基础设施，wangd 保留
   • 别人的 nodes/<other>/   ← 别人的活，看可以，改要 PR
```

## 想做 X，该怎么改？决策树

```
┌── 想改 prompt / rules / tools 白名单 / context 配置数值
│       → 改 harness.yaml 完事
│
├── 想加节点专属工具
│       → cp templates/tool.py.template nodes/<my>/tools/<tool>.py
│       → 在 tools/__init__.py 加 import
│       → harness.yaml.tools 加白名单
│
├── 想加节点专属 skill (操作 recipe)
│       → cp templates/SKILL.md.template nodes/<my>/skills/<name>/SKILL.md
│       → harness.yaml.skills 加白名单
│
├── 想每轮 LLM 前/后做点啥（注入消息 / 观察 / 检查）
│       → cp templates/hooks.py.template nodes/<my>/hooks.py
│       → 实现 4 个钩子点之一：on_turn_start / on_llm_response /
│         on_turn_end / on_end
│       → harness.yaml.loop_hooks 加启用
│       → 参考实现：core/loop_hooks_builtin.py 有 6 个内置 hook
│
├── 想自定 context 压缩策略
│       → cp templates/summarizer.py.template nodes/<my>/summarizer.py
│       → 自动启用（无需 yaml 配置）
│
├── 想要 outer retry / async 调度 / multi-persona 等结构性变化
│       → ★ 包装模式：cp templates/agent_loop.py.template nodes/<my>/agent_loop.py
│       → 默认就调 framework run_loop，前后加自定行为
│       → **看 templates/agent_loop.py.template 顶部警告**
│
└── 想改 build_messages 顺序 / KB 检索算法 / agent_loop 主循环本身
        → ★ framework 修改：开 Forgejo issue 跟 wangd 商量
        → 不要私下改 core/ 或 shared/
        → 90% 的情况其实是上面某档能解决，先回看
```

## Extension Point 详细参考

### `harness.yaml` —— 主战场

```yaml
node_type: my_node
version: "0.1"
risk_level: low
system_prompt: |
  ...                         # LLM 看到的 system 消息
rules: [...]                  # 硬约束（每轮强提示）
guidelines: [...]             # 软建议
tools: [...]                  # 工具白名单（必须 ∈ tool registry）
skills: [...]                 # skill 白名单（必须 ∈ skill registry）
context_config:
  memory_query: "..."         # memory 检索 query
  kb_query: "..."             # KB 检索 query
  max_context_tokens: 80000
  max_output_tokens: 16384    # reasoning model（GLM/o1/R1）chain-of-thought 占大头；4096 易截断
  temperature: 0.7
required_input_artifact_types: [pre_registration]
required_output_artifact_types: [analysis_report]
loop_hooks: [my_custom_hook]   # 启用 hooks.py 注册的 hook
hook_config:
  my_custom_hook: { threshold: 3 }
```

### `tools/__init__.py` + `tools/<x>.py` —— 节点专属工具

bootstrap 自动 import `nodes/<your>/tools/__init__.py`；在里头 `from . import xxx` 触发工具自注册。

### `skills/<name>/SKILL.md` —— 操作手艺 recipe

framework 自动 load。harness.yaml 加 skill 名启用。

### `hooks.py` —— 4 个 loop 钩子点

```python
from core.loop_hooks import HookContext, LoopHook, register_loop_hook

def my_hook(ctx: HookContext): return None
register_loop_hook(LoopHook(name="my_hook", on_turn_start=my_hook))
```

参考 6 个内置：`core/loop_hooks_builtin.py`（memory_delta / kb_delta / scratchpad / reflection / dreaming_due_reminder / producing_integration_reminder）。

### `summarizer.py` —— 节点专属 context 压缩

替换 framework 默认 LLM-based summarizer。

### `agent_loop.py` ★ —— 自定主循环（高风险，慎用）

90% 不需要。详见 `templates/agent_loop.py.template`。`hf doctor` 会列出哪些节点用了 custom loop。

## 不够用时的 escalation 路径

1. 先看本 README 决策树 → 9 成可解
2. 看 `templates/` 里对应模板的 docstring（含完整 example）
3. 看 `core/loop_hooks_builtin.py` 真 hook 实现
4. 还不够：在 Forgejo 开 issue「framework：需要 X 能力」+ tag wangd
5. **不要** 私下改 `core/` 或 `shared/`

## 节点 owner 的工作流（典型）

```bash
# Day 1：看本 README + own 一个节点 + 跑通 fixture
hf status                    # 看本机项目状态
cd nodes/<my_node>
cat README.md               # 看本节点专属说明
vim harness.yaml            # 改 prompt / rules / tools
python run_node.py --harness <my_node> --sandbox --fixture fixtures/minimal.yaml

# Day 2-N：迭代
hf log <run_id>             # 看 transcript
hf last-error               # 找问题
hf kb stats                 # 看 KB 写入
hf doctor                   # 检查是否偏离 default

# 满意 → 提 PR
git add nodes/<my_node>/
git commit -m "tweak <my_node>: ..."
git push                    # CI 自动跑 pytest
```

## 完整文档索引

- [`docs/owner-guide.md`](../docs/owner-guide.md) —— Owner 完整指南
- [`docs/node-iteration-guide.md`](../docs/node-iteration-guide.md) —— Fixture 测试详解（附录）
- [`docs/dev-workflow.md`](../docs/dev-workflow.md) —— hf CLI + sandbox + LLM cache
- [`docs/architecture.md`](../docs/architecture.md) —— framework 整体架构
- [`templates/`](../templates/) —— 所有可拷贝模板（含完整 docstring）
