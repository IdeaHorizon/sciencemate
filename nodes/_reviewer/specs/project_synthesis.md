# Review spec for project-level synthesis（项目级宏观综合评估）

> 触发：`_reviewer(node_inputs={'source_node_type': '_project', 'project_id': '<id>'})`。
> 这是 `_reviewer` 的**第二种 scope**（区别于 single_artifact 单 artifact 审稿）。
> 用途：进入 `writing` 节点前的硬 gate + loop-internal 复盘。

## 为什么 reviewer 干这事

老 `analysis` 节点删了（v2.0 后期）。它的 per-experiment verdict 责任移给
experiment 节点自判；它原本可能承担的"项目级宏观综合"职责本来就空缺 ——
现在由本 spec 兜底。reviewer 在这层做"PI 视角的科研中枢"：综合所有 hypothesis
verdict、survey、KB sediment，对项目当前状态下**统一判**该不该写论文了。

## 两种 mode（自动判别，不需要 caller 传）

### Research mode（默认）

触发条件：项目 KB 中**存在 frozen `pre_registration` artifact + 至少 1 个
`experiment_log` artifact**（即真做过研究 loop）。

跑完整 6 维评估 + 给详细 actionable_next_steps。

### Material mode（轻量）

触发条件：**没有 frozen prereg + experiment_log**（user 拎一堆材料直接想
write 的场景）。

只做 3 件事：
- scope_check：user 想 write 什么 vs 提供的材料能不能支撑（quick coverage 检查）
- evidence_chain_check：材料里 cited claim 是否真存在、方向是否一致
- writing readiness verdict（依然是 4 选 1 的 verdict）

不跑 6 维全量评，2-3 min 内完成。

**判别代码**（伪）：
```
prereg = list_artifacts(type='pre_registration', frozen=True)
exp_log = list_artifacts(type='experiment_log')
if prereg and exp_log:
    mode = 'research'
else:
    mode = 'material'
```

## 审稿前必做

### 1. 重建"user 真实要求"（research / material 都要做）

读以下来源，**综合**出一段 ≤ 300 字的 `user_requirement_summary`：

```
read_file('PROFILE.md')                     # user 全局偏好
read_file('PROJECT.md')                     # 项目级设置（target_venue 等）
memory_recall(query='directives')             # 当下 runtime directive
memory_recall(query='user_preferences')       # curator 沉淀的偏好
list_artifacts(type='research_intake')       # 项目初始 intake（user 原话）
list_artifacts(type='research_plan')         # 计划 artifact 如有
memory_recall()                              # 目标/铁律/叙事/手册 —— 看意图漂移
```

这段 summary **必须**：
- 含 user 原话片段（≥ 1 处引用，可追溯）
- 含具体科学目标（不是抽象"做好科研"）
- 含 target_venue / 论文 scope 等具体约束

### 2. 现状盘点

读以下来源，综合出 ≤ 500 字的 `current_state_summary`：

```
search_kb()                                  # 不传 entity_type = 4 entity + claim_type 分布
search_kb(claims, claim_type='hypothesis', status_filter='all')
                                             # 全 hypothesis verdict 状态
search_kb(claims, claim_type='methodological')
                                             # methodological sediment
search_kb(claims, claim_type='dead_end')     # 失败教训
search_kb(claims, claim_type='synthesis')    # 已有 synthesis
list_artifacts(frozen=True)                  # 全 frozen artifact
read_artifact() × N                           # 关键 artifact 内容（按需）
read_producer_transcript(...)                 # 看 run 流程（transcript 是权威）
```

要点：盘清"我们到底产出了什么 evidence、它支持什么"，**不是**简单列工件。

### 3. Research mode 才做的额外步骤

- **跨项目检查**：`search_kb(scope='org', query='<topic>')` 看 org 层是否类似工作已存在 → `kb_evidence_check.duplicate_work_risk`
- **覆盖度审计**：列出 user_requirement 隐含的 hypothesis 集 vs 实际测过的 → gap
- **统计严谨度抽查**：随机抽 2-3 个 hypothesis verdict，看 experiment_log 的 reasoning 是否站得住（reviewer 可重算）

### 4. Material mode 才做的额外步骤

- **scope_check**：user 提的写作 scope（write_request）vs 实际材料能不能支撑
- **evidence_chain spot check**：抽 5-10 个材料里的 cited claim_id / chunk_id，验真存在 + 方向

## 评分维度

### Research mode（6 维，每维 1-5 整数；**不允许** 2.5/3.5）

#### 1. Narrative coherence

所有 hypothesis verdict + KB sediment 组合起来讲了**一个清楚的故事**吗？还是
散点没主线？

- 5: 主线清楚 + 子论点 1-3 个互相支持 + 没有 contradictions
- 1: 一堆独立 verdict 拼不出叙事

#### 2. Evidence sufficiency

关键 claim 都有足够 evidence 支持吗？写出来 reviewer (peer) 会不会要补数据？

- 5: 每条 published-grade claim ≥ 2 个独立 source + robustness checked
- 1: 关键 claim 只有 1 trial / no robustness / cherry-picked

#### 3. Direction validity

`user_requirement` 的问题这些 verdict 真**回答了**吗？还是答非所问？

- 5: verdict 集精准 map 到 user 的科学问题
- 1: 跑了一堆但 user 原始问题没碰到

#### 4. Coverage

该测的 hypothesis 都测了吗？有没有显著 confound 没排？

- 5: hypothesis tree 完整 + 主要 confound 都有对照实验
- 1: 关键 confound 没测 / 关键 baseline 缺

#### 5. Methodological return

本项目有**跨项目可复用** finding 吗？（methodological / dead_end claim 数 + 质量）

- 5: ≥ 3 条 high-confidence methodological claim，或 ≥ 1 条 well-documented dead_end
- 1: 0 个 methodological / dead_end，全是 project-bound empirical

#### 6. Publishability

假设投目标 venue（从 PROJECT.md / user_requirement 提取），这个故事**够**吗？

- 5: 故事完整 + 严谨度过线 + novelty 在 venue 期待范围
- 1: 故事缺关键支撑 / novelty 不足 / 严谨度过差

### Material mode（2 维，每维 1-5）

#### 1. Scope match

user 想 write 的 scope vs 材料覆盖：
- 5: 材料完全覆盖 user 描述的 scope
- 1: scope 跟材料严重错位（user 想写 A，材料只支持 B）

#### 2. Evidence chain integrity

材料里 cited claim / chunk / paper 是否真存在 + 方向一致：
- 5: 抽查 5-10 个全 verify pass
- 1: 出现 phantom citation / 引用方向反 (critical 红线)

## 强制 metadata（reviewer 存 review_critique 时必填）

**4-verdict 紧凑设计**：所有"为啥这个 verdict / 该怎么办"全在 `actionable_next_steps[]`，
不在 verdict 字段里塞细节。

```json
{
  "scope": "project_synthesis",
  "mode": "research" | "material",

  "project_verdict": "ready_to_write" | "iterate" | "pivot" | "abort",

  "user_requirement_summary": "...（≤ 300 字，含 user 原话片段）",
  "current_state_summary": "...（≤ 500 字）",

  "actionable_next_steps": [
    {
      "action": "redo_experiment | new_hypothesis | deeper_literature | fix_evidence_gap | rescope | flag_duplicate_work",
      "target_node": "experiment | hypothesis | literature | writing | _curator | null",
      "target_artifact": "<artifact_id or null>",
      "why": "...（具体到具体 hypothesis / metric / 段）",
      "how": "...（actionable 指令，可直接喂给 target_node）",
      "blocks_writing": true | false
    }
  ],

  "scores": {
    "narrative_coherence": <int>,
    "evidence_sufficiency": <int>,
    "direction_validity": <int>,
    "coverage": <int>,
    "methodological_return": <int>,
    "publishability": <int>
  }
}
```

material mode 时 `scores` 只填 `scope_match` + `evidence_chain_integrity`。

这些字段必须通过 typed builder 分步提交，不能塞进普通 `summary` 后指望门禁解析：

```
compose_review_critique(action='set_verdict', verdict='approve', confidence=0.9, summary='...')  # 普通审稿 4 枚举之一
compose_review_critique(action='set_project_synthesis', project_verdict='ready_to_write', mode='research', user_requirement_summary='...', current_state_summary='...')  # 项目级 4 枚举之一
compose_review_critique(action='set_scores', scores={...})
compose_review_critique(action='add_actionable_next_step', step_action='...', step_target_node='...', step_target_artifact='...', step_why='...', step_how='...', blocks_writing=true)  # 一条一调
compose_review_critique(action='set_recommended_action', recommended_action='...', feedback_to_next_run='...')
compose_review_critique(action='finalize', name='project_synthesis_<short_id>', artifact_under_review='<artifact_id>', source_node_type='_project')
```

`finalize` 会机械地把 project synthesis 字段同步铸入 content 与 metadata；模型无须、
也不能用通用 `save_artifact` 补写 typed-only 凭证。

## verdict 决策规则

4 选 1。**严格按下表**：

| verdict | 触发条件 |
|---|---|
| `ready_to_write` | research mode: 所有维度 ≥ 4 + 无 blocks_writing=true step。<br>material mode: scope_match ≥ 4 + 0 critical evidence_chain 问题。 |
| `iterate` | 有 blocks_writing=true 的 step（无论几个）但**方向对**。这些 step 完成后可重新评估。这是**最常见**的 mid-project 输出。 |
| `pivot` | 任一维度 ≤ 2 + 修复需要**改变研究方向**（不是补实验）。比如 direction_validity=1 → user_requirement 跟实际工作严重错位，得 rescope。 |
| `abort` | duplicate_work_risk 严重（org KB 已有相同 claim）/ 数据本身不可救（fabricated 嫌疑）/ user_requirement 内在矛盾。需 request_human_input。 |

## orchestrator 怎么用这份 verdict

`actionable_next_steps` 是 orchestrator dispatch 的工作清单。`project_verdict`
选分支，`actionable_next_steps[]` 给细节：

```
verdict = metadata.project_verdict
steps = metadata.actionable_next_steps

if verdict == "ready_to_write":
    → run_node(node_type="writing", node_inputs={...})

elif verdict == "iterate":
    → for step in [s for s in steps if s.blocks_writing]:
          task(action='create', title=step.how, owner_node=step.target_node)
      → 起 target_node 跑 step.how，blocked-writing 完成后回头复评

elif verdict == "pivot":
    → run_node(node_type="hypothesis", node_inputs={...new direction...})

elif verdict == "abort":
    → request_human_input(question='项目终止建议，see project_synthesis_<id>')
```

## 跟 single_artifact scope 的区别

| 维度 | single_artifact | project_synthesis |
|---|---|---|
| 输入 | 单个 artifact + lineage | 全项目 artifact + KB + memory |
| 输出 | review_critique （per-artifact verdict）| review_critique（项目级 verdict + next_steps）|
| 用 hook | 自动 post-producing 3-step flow | orchestrator 显式触发（writing-gate / N runs since last） |
| 加载 spec | `nodes/<source>/review_spec.md` | 本文件 |
| 工具白名单 | read-only KB / 仅写 review_critique + memory_candidate + propose | 同上 + create_claim（写 synthesis claim_type）|

## 跟 _curator dreaming 的区别

curator dreaming 做**跨项目 KB 维护**（找 org promotion 候选 / synthesis 候选 / stale claim 复审）；
project_synthesis 做**本项目内 narrative 综合**（这些 verdict 拼出什么故事 / 能不能写论文了）。

两者方向相反：curator dreaming 输出是给 KB 用的；project_synthesis 输出是给
orchestrator 用的（决定起 writing 还是 iterate）。

## Anti-patterns（容易犯的错）

- ❌ 不读 PROFILE/PROJECT/directives 就直接判 readiness —— user 真实要求是判断基准
- ❌ 把 6 维全 5 分套用："故事 perfect"但 actionable_next_steps 列了 5 条 blocks_writing → 自相矛盾
- ❌ verdict=iterate 但 actionable_next_steps[] 为空 → orchestrator 不知道做啥
- ❌ verdict=ready_to_write 但还有 blocks_writing=true 的 step → 互斥
- ❌ material mode 跑 6 维全量评 → 浪费时间
- ❌ 不引 user 原话进 user_requirement_summary → 后续 audit 不知道依据啥
