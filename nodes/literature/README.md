# literature 节点

**Owner**: TBD

研究 pipeline 的入口节点。给定 `research_question`，跑文献调研，产出 `survey_report`。

## I/O 契约

| 方向 | artifact_type | 备注 |
|---|---|---|
| Input | （无 required） | 入口节点，从 `node_inputs.research_question` 起步 |
| Output | `survey_report` | 后续 hypothesis 节点的 ground 信息源 |

## 关键工具

- `semantic_scholar_search` / `arxiv_search` —— 真文献搜
- `save_artifact` (type=survey_report) —— 写产出
- 不直写 KB（curator 后续整合）

## 常见自定方向

- 改 `system_prompt` 让 LLM 按特定领域知识聚焦
- 加节点专属工具调本地论文库 / 私有 PDF
- 加 `loop_hooks: [reflection]` 让每 N 轮自检"有没漏重要工作"
- 加 quality_check：survey 必含 ≥ N 篇 paper + 必有 open_question 段

## 跑通示范

```bash
python run_node.py --harness literature --sandbox \
  --fixture nodes/literature/fixtures/minimal.yaml
```

## 不该做

- 写 KB（curator 的活）
- 跑实验（experiment 的活）
