---
name: adversarial_evidence_search
description: |
  对抗确认偏差的推理框架。在你即将把一条 claim 标 validated（或在 manuscript 引用它做结论）
  之前，按本指引主动构造**反向 query** 找反证 paper / 反例数据，确认真的没漏。
  不要用在：刚写下 claim 还没数据支撑时 —— 那时该用 falsifiability_pretest 先设计反驳判据，
  不是搜反例。
applies_when:
  - analysis 节点 finalize 一条 claim 的 status=validated 前
  - review 节点检查 manuscript 是否做过对抗扫描
  - hypothesis 节点 finalize prereg 前，确认 claim 真的可证伪 + 有反例搜过
tools_used:
  - search_papers
  - arxiv_search
expected_outcome: 至少跑过 3-5 个 adversarial query；读过 abstract 判断是否真矛盾；记 observation memory
status: validated
relevant_concepts: []
---

## 工作流（5 步）

1. **明确要审的 claim**：一句话写出准备 validate 的 claim 全文。例：
   > "Foundation MLIP MACE-MP-0 在 OC20 OOD subset 上 force MAE > 100 meV/Å"

2. **构造 4-5 个 adversarial query**：直接基于关键词加反向 modifier：
   - `{claim} limitations`
   - `{claim} counterexample`
   - `{claim} failure mode`
   - `{claim} disagreement`
   - `contradicting {claim}`

3. **依次跑 query**：对每个 query 各调一次：
   ```
   search_papers(query="<adversarial_query>", max_results=5)
   arxiv_search(query="<adversarial_query>", max_results=5)
   ```
   按 title 去重。

4. **读 abstract 真判断**：候选 paper 列表只是**候选**。LLM 必须真读 abstract，判断：
   - 真的跟 claim 矛盾？→ 反证
   - 跟 claim 无关 / 弱相关？→ 噪声
   - 跟 claim 部分一致部分矛盾？→ 边界条件，记下来

5. **落地决策**：
   - 找到真反证 → 不要 update_claim_status to validated；写 candidate：
     `memory_note(text='<反证 paper + 简述>', category='pitfall')`
   - 找不到反证 → 可以走 validate，但 memory_note 记一句"已做对抗扫描跑了 N 个 query"作 audit trail

## Pitfalls

- ❌ 只跑 1-2 个 query 就说没找到反证（confirmation bias 死灰复燃）
- ❌ 看到 paper 标题没读 abstract 就当反证
- ❌ 把"没找到反证"等同于"claim 一定对"（adversarial search 是必要不充分）
- ✅ 至少 4 个不同 angle 的 query 才算扫过
- ✅ 找到反证就**老实**，不 validate

## 完整例子

```
# analysis 节点准备 finalize 一条 claim
claim = "MACE-MP-0 在 OC20 OOD subset 上 force MAE > 100 meV/Å"

# Step 2-3: 跑 5 个 adversarial query
for q in [
    f"{claim} limitations",
    f"{claim} counterexample",
    f"contradicting MACE OOD performance",
    "MACE-MP-0 OOD generalization success",
    "foundation MLIP OOD better than expected",
]:
    r1 = await search_papers(query=q, max_results=5)
    r2 = await arxiv_search(query=q, max_results=5)

# Step 4: 读 abstract，发现一篇 "MACE-MP-0 with fine-tuning achieves 40 meV/Å on OC20-OOD"
# Step 5: 这是边界条件（fine-tuning vs base），不算完全反证。验证 → 但记 candidate：
await memory_note(
    text="MACE-MP-0 在 OC20-OOD base 模型 MAE > 100 meV/Å，但 fine-tuned 可降到 40 meV/Å (paper:smith2025)",
    category="observation",
    tags=["adversarial-scan", "claim:abc123", "boundary-condition"],
)
await update_claim_status(claim_id="abc123", new_status="validated",
                           reasoning="adversarial scan 5 query 后只找到 fine-tuning 边界条件，base 模型 claim 成立")
```

## 为什么是 skill 不是 tool

构造 query + 调 2 个工具 + 去重，**没有任何原子能力是新的**。这是工作流模板，LLM 看到
SKILL.md 完全能自己执行。做成 tool 会把 query 模板硬编码到 Python 里，反而不灵活。
