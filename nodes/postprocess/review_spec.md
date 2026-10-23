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

## 4. SVG 轨（illustration 家族，AutoFigure 移植）

纯文字描述的示意图请求（无上游数据、无机器可读 spec）走 `render_illustration`
铸造，审查面与 render_figure 同构，差异点：

- **合同 = 宣言**：`family: "illustration"` 的合同（nodes/edges/groups/
  assertions，无 bands）由作者手写 SVG 兑现；断言对 **SVG DOM 标记**求值
  （`svg_vocab` 六属性词表），不是对对象模型。`contract_checks` 里看得到
  每条断言在 DOM 上的实得值。
- **渲染源**：`render_code.generator == "frozen-svg"` —— 冻结脚本是框架生成
  的重放器，SVG 字节内嵌其中；`figure_src/<name>.svg` 是同一份的可读副本。
  replay 重跑 = 用 cairosvg 把同一份 SVG 重新栅格化。
- **metadata.track == "svg"**（另有 `genre`）；交付 `files[]` 只挂 PNG。
- **审计账** `audit.engine == "svg_audit+cairosvg"`：XML/标记完整性/文字政策/
  印刷字号换算/缺字形为矛盾级（拒绝项），几何出界与重叠是启发式估计
  （如实标注 est）。
- **annotated_values 不声明是特性**：图上数字来自文字描述，没有 artifact
  出处；`annotated_values_check.ran == False` 的缺席披露是预期形态。
- **enhancement 产物是独立 asset**（`asset_role: "figure_enhancement"`，
  `evidence_bearing: false`，`enhanced_from` 指回源 figure）：位图文字未经
  机械复核，源 figure 永远是机械核过的交付品。
