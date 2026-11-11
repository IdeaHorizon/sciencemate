---
name: kb_integrity_audit
description: |
  curator Mode 2 dreaming 跑全 KB 一致性扫描。检查 7 类 integrity issue：
  孤儿 claim、循环引用、provenance 缺失、过期 validated、矛盾 review、scope 错配、author wiring 断链。
  发现问题 propose 修复或写 audit log。
  不要用在：日常 KB 写入路径（这是离线扫描，不是 hot path）；新 claim 写入前的快速检查（用 search_kb 即可）。
applies_when:
  - curator 节点跑 Mode 2 dreaming
  - 项目 KB 累积 > 50 claim 后定期跑（每周或每 1k 条写入）
  - 跨项目 ORG_MANIFEST.md 生成前
tools_used:
  - search_kb
  - get_kb_record
  - curator_scan
  - propose
  - curator_audit
expected_outcome: 持久化的修正 proposals 与 handoff 中的 issue 清单；实际 KB 写入由现有 curator audit 留痕
status: validated
relevant_concepts: []
---

## 工作流（7 类检查）

### 1. 孤儿 claim 检查
```
search_kb(entity_type='claims', limit=200)
```
对每条 claim：
- 如果 `concept_ids == [] and not orphan_reason`：违反 v3 schema（应该 validation 早就挡住，但旧数据可能有）
  → propose(proposal_type='kb_other', target_entity='claims', target_id='<真实 claim id>', proposed_action='add concept_ids or orphan_reason', reasoning='<问题与依据>')
- 如果 `sources == [] and claim_type not in ('dead_end','conjecture')`：缺证据
  → propose 同上

### 2. 循环引用检查（synthesis 防自引）
对 `claim_type='synthesis'`：sources 里的 claim_id 是否含 self.id？
含 → propose 修复。

### 3. Provenance 缺失
对每条 entity：`created_by_role` 是否为空 / 不合法？  → audit log 标记需 backfill。

### 4. 过期 validated（定期复审）
```
search_kb(entity_type='claims', status_filter='validated', limit=200)
```
没有"按天数筛 stale"的工具 —— 拉回来后**自己看时间戳**判断哪些超过 180 天没复审
（要全量字段时对具体 id 用 `get_kb_record`）。对每条判定为 stale 的 claim：
- replication_count ≥ 3 → propose 标记需要重新 sample evidence
- replication_count < 3 → propose 降级 confidence + status → provisional

顺带扫已知失败路径是否也过期：
```
curator_scan(scan_type='stale_dead_end', days=180)
```

### 5. 高争议 claim
```
search_kb(entity_type='claims', confidence_min=0.35, confidence_max=0.7, limit=200)
search_kb(entity_type='claims', status_filter='needs_review', limit=200)
```
第一条拉"信心悬在中间"的，第二条拉"上游被 refute 等复审"的。
没有 review_history 过滤器 —— 命中后自己 `get_kb_record` 看有没有互相矛盾的 review。
对每条判定为争议的 claim：
- 在 handoff 里列为 "active dispute"，并用 `propose` 记录具体修正建议；
  `curator_audit(action='log')` 只读已有操作日志，不是保存报告的入口。
- 如果争议持续 > 30 天且无新 evidence：propose 标记 needs_evidence

### 6. Scope 错配
对每条 `scope='org'` 的 claim：
- 如果 `scope_dimensions` 含 seed/run_id/project_id → 不该升 org，propose 降级 project
对每条 `scope='project'` 的 `claim_type='dead_end'`：
- propose 升级到 org（dead_end 应跨项目共享）

### 7. Author wiring 断链
扫所有 chunks `where source startswith 'arxiv:' or 'doi:'`：
- `author_concept_ids == []` → 缺作者 wiring
  → propose 跑作者抽取（external lookup）

## 产出

每个需要修正的 issue 用 `propose` 挂到真实 KB entity，记录依据与建议；handoff 汇总
issue、proposal id、扫描范围和无法检查的部分，随 run transcript 保存。无 issue 时如实报告。
_curator 不产 artifact；不请求通用保存工具。实际 KB 写入的审计仍由现有机制记录，
事后回查用 `curator_audit(action='log')`；Mode 1 的整合收据由现有 integration hook 产生。

## Pitfalls

- 不要直接修，**全部走 propose**：audit 是诊断，不是治疗。user 在 inbox 决定哪些采纳。
- 不要每次 dreaming 都全量扫；可按 `last_audit_at` 增量扫（新写入 + 老 stale）
- 扫描范围与时间写入 handoff；没有可消费的历史范围时明确说明，不假定存在 metadata 写入接口。
