---
name: falsifiability_pretest
description: |
  Hypothesis pre-registration 之前的可证伪性自检框架。当你要 save_artifact(type='pre_registration')
  或调 create_claim(claim_type='hypothesis') 时按本指引做：对每条 hypothesis 先想清楚"什么观察会反驳它"，
  写成结构化 falsification_criteria（metric / op / threshold），然后 freeze artifact 防回头改。
  不要用在：实验已跑完想反推证伪条件 —— 那是确认偏差的最大温床；该时机用
  adversarial_evidence_search 找反例而不是 retroactively 改 falsifier。
applies_when:
  - 设计新 hypothesis 准备测试
  - 写 pre-registration artifact 时
  - hypothesis 节点的核心工作流
tools_used:
  - save_artifact
  - freeze_artifact
  - create_claim
expected_outcome: 一个 frozen pre_registration artifact + 每个 hypothesis 都有结构化 falsification_criteria
status: validated
relevant_concepts: []
---

## 工作流

1. **逐条思考反驳条件**：对**每条** hypothesis 写出至少一个能反驳它的可观察结果：
   - 例："如果 force_MAE > 100 meV/Å on OOD subset，则 H1 被反驳"
   - 例："如果 effect size < 0.3 with p > 0.1，则 H2 被反驳"
2. **结构化判据**：写成机械可判的 dict（KB v2 关键）：
   ```yaml
   falsification_criteria_structured:
     metric: force_MAE
     comparison: ">"
     threshold: 100
     dataset: OC20_OOD
     regime: OOD
   ```
3. **不可证伪 = 重写**：如果某条 hypothesis 你想不出任何能反驳它的观察 —— **它不可证伪**，必须重写
4. **自由文本备份**：把反驳判据也写一份自由文本（`falsification_criteria_text`）
5. **预测**：写下你的 `predicted_outcome` —— 你期望观察到什么
6. **保存 + 冻结**：
   - `save_artifact(artifact_type='pre_registration', ...)`
   - `freeze_artifact(artifact_id=<刚保存的>)` （必须 —— prereg 不可改）
     → 冻结返回里带 `chunk_id`，那就是 prereg 的 KB 锚点。**没有单独的登记工具**，
       不要再去找 register/ingest 那一步。
7. **写 KB hypothesis**（★ 仅 _curator 采纳时写入）：
   ```
   create_claim(
     claim_type="hypothesis",
     claim_text="...",
     hypothesis_id="H1",              # 预注册里的问题/假说 id，是身份锚
     falsification_criteria_structured={...},
     falsification_criteria_text="...",
     prereg_chunk_id=<第 6 步返回的 chunk_id>,
     predicted_outcome="..."
   )
   ```

## Pitfalls

- **prereg 一旦 freeze 不可改**。要"修订 hypothesis"是开一个新 hypothesis 标 supersedes 旧的
- 反驳判据应该**机械可判**（数值 + 阈值 + dataset）—— 这让 curator 能 AUTO 翻 verdict
- 不要把"如果实验没跑成"作为反驳判据 —— 那是 failure，不是 refutation
- 假设数 ≤ 3：贪多嚼不烂
