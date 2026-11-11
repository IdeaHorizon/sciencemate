---
name: claim_evidence_link
description: |
  防止幻觉式叙述的硬规则。当你在研究状态、证据记录或 manuscript 里
  下定量结论时按本指引做：每个定量陈述必须能指到一个具体 chunk_id（artifact 走
  freeze_artifact，冻结返回里带 chunk_id）或外部 URI（doi:/arxiv:/https:）作为 source。
  不要用在：写 hypothesis / 设计文档 / 主观心得 —— 那些非定量陈述用
  按本节点的设计产物或 memory_note 记录，不由本 skill 授予 KB 写权。
applies_when:
  - 写研究状态、观察记录、writing 节点的论文或任何含定量结论的产出
  - 上游有 artifact 可读，需要把结论锚定到证据
tools_used:
  - list_artifacts
  - read_artifact
expected_outcome: 产出中**每一条**定量 claim 都标注了来源 artifact + 定位
status: validated
relevant_concepts: []
---

## 工作流

只读当前节点可见产物，用 `list_artifacts` / `read_artifact` 获取；已绑定工作区文件可用
`read_file`。`read_external_artifact` 仅在本节点实际授予该能力时使用。不可见的来源让调用方
转交，或在获准调用 Literature 时委派查证；不要绕项目边界，也不要求开放额外读写权限。
Observation 只写自己的证据记录；KB 的 create_claim 由 _curator 采纳时执行。

1. **拿 artifact 全景**：用 `list_artifacts` 列出本节点可见的上游 artifact id
2. **起草 claim**：写下你的断言（"X 比 Y 高 23%"，"p < 0.05"，"effect size = 0.42 ± 0.05"）
3. **逐条溯源**：对**每一条** claim，找出：
   - 来自哪个 artifact（artifact id）
   - 哪一段、哪一个数值（具体定位，e.g., table 2, row 3）
4. **标注来源**：在 claim 末尾用 markdown 行内括号或脚注注明：
   - 例：`X 比 Y 高 23%（see experiment_log__run42, table 2, row 3）`
5. **无证据的处理**：找不到证据的 claim → 要么删掉，要么显式标注 `unsupported — derived from inference`。**绝不**让 unsupported claim 静默通过
6. **（可选）KB 入库**：如果项目启用了 KB，让 _curator 节点之后把每条 claim 用 `create_claim(claim_text=..., sources=[...])` 入 KB

## Pitfalls

- **claim 的 source 字段不能是 run-local artifact id**（v2 强制）—— 要先 `freeze_artifact`
  把 artifact 内容固化，冻结返回里带 `chunk_id`，用它作 source（没有单独的登记工具）
- 抽象 claim（"该方法有效"）必须能分解到具体数值/观察，否则不算 evidence-linked
- 引用工具结果而非"我记得"
- 一个 claim 跨多个 source 时全部列出来，单 source 的弱 claim 在 report 里要标 confidence=low
