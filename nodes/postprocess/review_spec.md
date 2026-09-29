# Scientific Visualization reviewer specification

判决拆除 B 刀（2026-09-01）后本节点的审查面：`_reviewer` 审的是 **figure
记录**（唯一 typed 产物）与它的出处绑定，不是 producer 的推理过程。

## 1. Review object and authority

Review 的对象是具体 `figure` 记录。每条记录机械携带：

- `source_artifact_ids` + `source_hashes`（上游数据绑定）；
- `render_code.path` + `render_code.content_hash`（逐字节冻结的渲染脚本）；
- `files[].content_hash`（输出文件指纹）；
- `figure_hash`（对以上核心逐字节重算的指纹，消费端会重验）；
- `findings[]`（图像级机械审计 + VLM 证人观察 + 缺席事实）；
- `audit`（机械审计账）与 `vlm_review`（证人是否运行、被问的是谁）；
- `replay`（referee 可重跑的命令）。

记录里**没有** status / quality_mode / verdict —— 证据可持久化，判决不可以。
reviewer 判断质量时读 findings 与图本身；「够不够发表」由 reviewer/referee/
用户裁，不由任何铸记录时算出的字段代答。

## 2. Reviewer checklist

1. **出处绑定**：记录的 figure_hash 是否通过 `shared/lib/publication_figures.
   validate_figure_record`（伪造/事后改写在这里露馅）；源数据是否真的是上游
   产物（不是手抄小 CSV）。
2. **可复现性**：按 `replay.command` 重跑冻结脚本，输出字节是否复现
   `files[].content_hash`。
3. **findings 是否被诚实对待**：机械审计与 VLM 证人的 findings 还开着的，
   要么已在图中修复（新记录），要么应在交付中披露 —— 静默丢弃 = revise。
4. **科学语义**：显示变换是否在渲染代码里可见、是否越界做了聚合/清洗/
   统计（那是 Experiment 的活）；Sankey/network/tree 是否只消费上游拓扑。
5. **视觉质量**：最终尺寸可读性、色盲安全、图型惯例 —— 参照
   `nodes/postprocess/skills/` 各图型家族的语义自查清单。

## 3. VLM witness（节点内部，供 reviewer 参考）

平台配置 `visual_review` 角色时，`render_figure` 恒跑一次证人 pass，
`inspect_figure` 可按需再看。协议见 `vlm_witness.py`：VLM 只返回
panel/region/observation 的 JSON 观察，确定性规则（`map_observations`）负责
rubric 映射；复现协议（两次独立判读 + 有界消歧）原样保留。角色缺席时
findings 里自然没有 VLM 条目，`vlm_review` 为空 —— 这是可读的缺席事实，
消费端据此披露「视觉检查未运行」。

rubric 文件在 `reviewer/rubrics/`。VLM 的机器角色是证人
（supplementary），不是盖章岗：它不能单独放行图片，也没有科学正确性权限。
