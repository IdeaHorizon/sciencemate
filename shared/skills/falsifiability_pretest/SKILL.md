---
name: falsifiability_pretest
description: |
  Analysis预注册之前的可证伪性自检框架。对每条 hypothesis 先想清楚"什么观察会反驳它"，
  写成结构化 falsification_criteria（metric / op / threshold），然后 freeze artifact 防回头改。
  不要用在：实验已跑完想反推证伪条件 —— 那是确认偏差的最大温床；该时机用
  adversarial_evidence_search 找反例而不是 retroactively 改 falsifier。
applies_when:
  - 设计新 hypothesis 准备测试
  - 写 pre-registration artifact 时
  - analysis 节点的核心工作流
expected_outcome: Analysis 的预注册判据；消费方交付适用性检查与缺口，不另写预注册或科学终态
status: validated
relevant_concepts: []
---

## 工作流

**按 owner 消费**：只有 Analysis保存/修订预注册与研究计划。
Observation / Experiment 用本 skill 检查冻结判据是否能被观察或测量，并把歧义、能力缺口交回
调用方；不另写一份预注册，不创建 hypothesis claim，不更改判据来适配已有结果。
探索任务没有待检验命题时此预检不适用，不为启动取证强设预注册。
科学终态以 `nodes/analysis/harness.yaml` 的“轮次与裁决权”为准。

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
6. **Analysis 保存 + 冻结**：
   - `save_artifact(artifact_type='pre_registration', ...)`
   - `freeze_artifact(artifact_id=<刚保存的>)` （必须 —— prereg 不可改）
     → 冻结返回里带 `chunk_id`，那就是 prereg 的 KB 锚点。**没有单独的登记工具**，
       不要再去找 register/ingest 那一步。
7. **仅 _curator 在 Analysis 采纳后写 KB hypothesis**（其他节点不执行）：
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

- **冻结版本不可改写**。Analysis 按自己的修订契约开新版本并说明变更；旧执行证据继续绑定旧协议。
- 判据应可观察或可测量；机械比较是证据，不授权 curator 自行下科学终态。
- 不要把"如果实验没跑成"作为反驳判据 —— 那是 failure，不是 refutation
- 假设数 ≤ 3：贪多嚼不烂
