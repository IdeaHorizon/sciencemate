---
name: cross_project_pattern_detection
description: |
  curator Mode 2 dreaming 的跨项目部分：扫 org scope KB，找跨项目反复出现的 pattern。
  3 类目标：(a) 同一方法在多项目都 dead_end → 警告新项目别走（失败复利）；
  (b) 同一 claim 在多项目独立复现 → propose 升级到 org（成功复利）；
  (c) 跨项目 concept 高频引用 → propose 升级为 methodological synthesis。
  不要用在：单项目内部分析（用 research_direction_exploration）。
applies_when:
  - 跨项目 ORG_MANIFEST.md 生成前
  - 新项目 intake 阶段（提醒用户参考已有 org 知识）
  - curator dreaming 周期 ≥ 30 天检查 org 复利情况
tools_used:
  - search_kb
  - get_kb_record
  - curator_scan
  - draft_knowledge_card
  - propose
expected_outcome: ORG_MANIFEST.md 更新 + 若干终态晋升候选 + 跨项目 pattern 报告
status: validated
relevant_concepts: []
---

## 工作流

### Pattern A：失败复利 (dead_end 跨项目)

```
search_kb(entity_type='claims', claim_type='dead_end', scope='org', limit=200)
```

对每条 dead_end：
- 看它涉及的 `concept_ids`（method / dataset / etc.）
- 这些 concept 在新项目里是否高频引用？
- 是 → propose。**`propose` 没有 `payload` 参数**，五个必填是
  `proposal_type / target_entity / target_id / proposed_action / reasoning`：
  ```
  propose(proposal_type='dead_end_warning',
          target_entity='claims',
          target_id='<那条 dead_end claim 的真实 id>',
          proposed_action='提醒本项目：此路已验证不通，再做需要新论据',
          reasoning='项目 P 已验证此路不通，本项目正高频引用同一 concept（≥ 10 字符）')
  ```
  target_id 必须在 KB 里真存在，凭空造会被直接驳回。

### Pattern B：成功复利 (project claim → org promotion)

```
search_kb(entity_type='claims', scope='project', confidence_min=0.85, limit=500)
```

对每条已 validated 的 project claim：
- 看其它项目是否独立得出相同/相近 claim（claim_text 字面 / concept_ids 重叠）
- 若 ≥ 2 个项目独立支持，且 replication_count 累计 ≥ 3
  → curator_scan(scan_type='org_promotion_candidates')  # 终态扫盘是晋升的唯一入口，
    #   它内部自动发提议。候选缺卡片的，先 draft_knowledge_card。

### Pattern C：synthesis 跨项目（methodological / theoretical 沉淀）

对每个 concept_type='method'：
```
search_kb(entity_type='claims', concept_ids=[method_id], scope='org')
```
看是否有跨项目共同的"决策 pattern" —— 比如多个项目都得出"该方法在数据量 < 100 时不可用"。
有 → 在**本项目** KB 写一条 claim_type='methodological' 的沉淀：
```
create_claim(claim_text='Method X 在 data_size < 100 时不可用',
             claim_type='methodological',
             rationale='Synthesized from claims [...] in projects [...]',
             sources=[claim_id_1, claim_id_2, ...])
```
**`create_claim` 传不了 scope='org'**（scope 只接受 'project'）。org 级知识只有一条
出生通道：项目终态的批量晋升 —— `curator_scan(scan_type='org_promotion_candidates')`
+ `draft_knowledge_card`，由人审。别在这里试图直接写 org。

### Pattern D：concept gap 跨项目（探索方向）

**没有 `find_concept_pair_gaps` 这种工具** —— gap 是你在结果上做的判断，不是某个工具的返回。
对全 org KB 自己搭矩阵：
```
search_kb(entity_type='concepts', concept_type='method', limit=100)
search_kb(entity_type='concepts', concept_type='dataset', limit=100)
search_kb(entity_type='claims', scope='org', limit=200)
```
看哪些 (method, dataset) 组合从来没有 claim 提到过。
若某 gap 在多个项目都被"绕过"（如同一 method 在多 dataset 都未测）→ propose 一个跨项目实验研究方向
（同样按上面 Pattern A 的五个必填参数写，target 挂到那个 method concept 上）。

## 产出

1. 跨项目 propose 列表（org_promotion / dead_end_warning / methodological synthesis）
2. 更新 ORG_MANIFEST.md（手动 trigger 或由 manifest auto-gen skill 接管）
3. 写一份 cross_project_report artifact 记录扫描结果

## Pitfalls

- 跨项目 promotion 要 **保守**：confidence ≥ 0.85 + replication ≥ 3 是软门槛，越强越好
- 不要把 project-specific empirical（带 seed/run_id 的）误判为 universal → smart_default_scope 应已挡住，但人工 review 再确认
- dead_end_warning 是高价值但易刷屏；每个项目针对一个 concept 只 propose 一次
- 跨项目数据量大，扫的频率不用高 —— 每月 1 次即可；不要每次 dreaming 都重跑
