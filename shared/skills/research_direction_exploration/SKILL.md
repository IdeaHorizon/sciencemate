---
name: research_direction_exploration
description: |
  curator Mode 2 dreaming 主流程：用 search_kb 的各种切法 + curator_scan 扫 KB，
  找候选研究方向，LLM 判断哪些值得 propose 给 user。
  不要用在：响应式（agent 在跑某个具体节点时找 hypothesis） —— 那时直接 search_kb。
  也不要写死成机械跑固定 query 列表；以下 starter list 是参考，鼓励 LLM 找新模式。
applies_when:
  - curator 跑 Mode 2 dreaming（周期 / on-demand）
  - 用户在 chat 问"我们项目接下来该做什么？"
  - 跨项目 ORG_MANIFEST.md 生成前找 cross-project pattern
tools_used:
  - search_kb
  - get_kb_record
  - curator_scan
  - list_proposals
  - propose
  - save_artifact
expected_outcome: 一份 research_direction_proposals artifact，含 5-20 条带 reasoning 的候选方向，每条 propose 给 user inbox
status: validated
relevant_concepts: []
---

## 工作流

### 阶段 1：跑切片拿候选 (mechanical, fast)

按以下 starter patterns 各跑一遍。**鼓励你跳出这个列表想新 pattern** —— 这些是起点不是终点。

**没有 `find_*` 系列切片器这种工具**。所有切片都是 `search_kb` 换参数 +
`curator_scan` 换 scan_type；gap / 轨迹这类"两个维度交叉看空白"是**你自己在结果上做的判断**，
不是某个工具的返回。

1. **concept × concept gap（method × dataset / method × phenomenon /
   theory × phenomenon / tool × task）**：
   ```
   search_kb(entity_type='concepts', concept_type='method', limit=100)
   search_kb(entity_type='concepts', concept_type='dataset', limit=100)
   search_kb(entity_type='claims', limit=200)
   ```
   自己在脑子里搭矩阵：哪些 (method, dataset) 组合从来没有 claim 提到过？
   那格空白就是候选方向。theory × phenomenon 是"解释空白"，tool × task 同理。

2. **大牛 / lab 轨迹观察**：
   ```
   search_kb(entity_type='concepts', concept_type='person', limit=100)
   search_kb(entity_type='chunks', query='<person 名字或 group 名>', limit=50)
   ```
   或按 concept 反查：`search_kb(entity_type='claims', concept_ids=['<person/group concept_id>'])`。
   看他们方向演进，找他们没做但邻接的工作。
   （chunk 的 `author_concept_ids` wiring 是这条路能不能走通的前提；断链了先补 wiring。）

3. **争议区战场**：
   ```
   search_kb(entity_type='claims', confidence_min=0.4, confidence_max=0.7, limit=100)
   search_kb(entity_type='claims', status_filter='needs_review', limit=100)
   ```
   信心悬在中间、或上游被 refute 等复审的 claim = 下个实验最有价值的目标。
   要看有没有互相矛盾的 review，对具体 id 用 `get_kb_record`。

4. **过期 validated 复审**：
   ```
   search_kb(entity_type='claims', status_filter='validated', limit=200)
   ```
   没有"按天数筛 stale"的工具 —— 拉回来自己看时间戳判断哪些超过 180 天没被再问过。
   科学不是一次性的：老结论也该被新数据再问一次。
   已知失败路径那一侧有现成的：`curator_scan(scan_type='stale_dead_end', days=180)`。

5. **跨项目 dead_end 借鉴**：
   ```
   search_kb(entity_type='claims', claim_type='dead_end', scope='org')
   ```
   别项目踩过的坑，本项目可能正要踩 → 提醒 user 看一眼。

6. **synthesis 候选**：
    ```
    curator_scan(scan_type='synthesis_candidates', auto_propose=True)
    search_kb(entity_type='claims', claim_type='empirical', confidence_min=0.6, limit=50)
    ```
    前者机械扫"同 concept ≥ N 条 claim 却没人写 synthesis"的洼地；
    后者自己找主题相近的 N 条立得住的 claim，看是否能升一条 synthesis claim。

7. **失败转生**：
    ```
    search_kb(entity_type='claims', claim_type='dead_end')
    ```
    每条 dead_end 看 `next_try` —— 是否值得在本项目重试？

8. **未答 open question**：
    ```
    search_kb(entity_type='claims', claim_type='hypothesis', status_filter='open')
    ```
    KB 里还没被证据定论的 hypothesis —— 哪些现在 testable 了？
    （claim_type 只有 5 种：empirical / methodological / hypothesis / synthesis / dead_end。
    没有 `conjecture`，别写。）

9. **methodological 跨域迁移**：
    ```
    search_kb(entity_type='claims', claim_type='methodological', scope='org')
    ```
    某领域的"决策"在另一领域是否适用？

### 阶段 2：LLM 判断 + 写 propose

对每条候选：
- 用 search_kb 拉相关上下文（已有 claim / 已 propose 类似的 / user 历史否决记录）
- 判断价值 / 可行性 / 新颖性
- 值得 → propose。**`propose` 没有 `payload` 参数**，五个必填是
  `proposal_type / target_entity / target_id / proposed_action / reasoning`：
  ```
  propose(proposal_type='research_direction',
          target_entity='concepts',          # 或 'claims'
          target_id='<触发这条方向的那个真实 KB id>',   # 必须在 KB 里真存在
          proposed_action='<一句话：接下来该做什么>',
          reasoning='<为啥这条值得做（非空，说清依据）>',
          confidence=0.7)
  ```
  target_id 是**锚**：先 `search_kb` / `get_kb_record` 拿到真实 id，
  再把方向挂到它上面。凭空造 id 会被直接驳回。

### 阶段 3：汇总

```
save_artifact('research_direction_proposals', date,
  content='# Dreaming run <ts>
  ## Tier 1 (high value): N proposals
  ## Tier 2 (worth checking): N
  ## Tier 3 (low priority but novel): N')
```

## Pitfalls

- **不要堆 100 条 propose** 把 inbox 淹了。每次 dreaming 控制在 5-20 条高价值。
- **去重**：跑前先 `list_proposals(type_filter='research_direction', status='pending')`
  看最近已 propose 过哪些；不重复刷屏。（参数是 `type_filter` / `status`，没有 `filter` / `age_days`。）
- **包含 reasoning**：每条 propose 必须带 "为啥这条值得做"，让 user 一句话能判断采纳/否决。
- **不要写死这几条**：这是 starter list；如果你想到新 pattern（比如 metric × dataset 矩阵），直接跑。
- 跨项目模式建议交给 `cross_project_pattern_detection` skill，本 skill 主要关注 project-internal。
