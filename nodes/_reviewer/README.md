# `_reviewer` — 独立科学审稿节点

## 它是什么

每个 producing 节点产出 artifact 后，框架**自动**调起 `_reviewer` 跑一次独立审查，产 `review_critique` 给 orchestrator 综合成 decision package 问 user。

## 为什么是独立节点

- 同 model 自己审自己有 confirmation bias → 独立 agent loop 破 bias
- quality_checks 的 LLM-as-judge 看不到 metadata values + 没工具调用 → 太弱
- 中段 artifact（prereg / experiment_log）当前没人审，等到 manuscript 才被 review 节点抓 → 太晚

## 它**不**领域化

reviewer 节点本身没有任何具体节点的 review 标准（不写"hypothesis 应该这样审"）。它启动时按 `source_node_type` 去 `nodes/<source>/review_spec.md` 加载 owner 写的标准。

→ **producing 节点 owner 在自己 folder 自由迭代审稿标准，完全不碰 reviewer node**。

## Owner 怎么配自己节点的 review spec

在你节点 folder 加一份 markdown：

```
nodes/<your_node>/review_spec.md
```

内容自由（reviewer LLM 当 prompt 读），举例：

```markdown
# Review spec for hypothesis 节点产物

审 pre_registration artifact 时重点检查：

1. 每个 H 必须有 falsification_criteria_structured，criteria 机械可判
2. predicted_outcome 量化（不要 "expects improvement"）
3. N seeds ≥ 5（否则 H underpowered）
4. about_concept_ids ≥ 1

警惕：
- predicted_outcome 跟 criteria 不一致
- 用 "should" / "expect" hedge 词替代具体阈值
```

可选：在 `nodes/<your_node>/skills/review_*.md` 写结构化 review skill（procedural checklist），reviewer 加载时一起拿。

如果你**啥都不写**，reviewer 用 fallback baseline：诚实性 / 内部一致性 / 数据 traceability 三条。

## Owner 怎么选择性 skip reviewer

如果你节点产物简单不值得每次都 review（比如 `data` 节点产的 dataset），在你 harness.yaml 加：

```yaml
skip_post_node_review: true
```

框架自动跳过 reviewer，仍跑 curator + decision package（package 里标 `Review: skipped (owner opt-out)`）。默认 false。

## Verdict + recommended_action

reviewer 输出的 `verdict` 4 档：
- `approve` — 没问题或全 minor
- `approve_with_revisions` — 有 major concerns 但 downstream 可推进
- `major_concerns` — 强烈建议 revise
- `block` — fundamental 错误，不可推进

`recommended_action` 4 个可执行选项给 orchestrator decision package 用：
- `proceed` / `revise` / `abort` / `escalate_to_human`

**verdict 不直接 block pipeline** —— 走 advisory 路径，user 在 decision package 看 verdict + recommended action 后自己选（或开 `--auto-approve` 默认走 recommended）。

## reviewer 失败怎么办

reviewer agent 自己挂掉（LLM 超时 / max_turns / schema 错）→ orchestrator 给 user 个 fallback decision package（只有 producing + curator，没 review 那段，标 `⚠️ review failed, proceeding without`）。**reviewer 故障不 block pipeline**。

## 与现有 review 节点的关系

`nodes/review/` 已删（manuscript 审稿归 `_reviewer` 统一处理；原 6 维 rubric 迁移到 `nodes/writing/review_spec.md`）。
