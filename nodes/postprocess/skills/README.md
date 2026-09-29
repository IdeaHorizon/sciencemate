# Scientific Visualization skills

Harness 常驻五个短 core skills：

- `visual-intent-router`
- `scientific-visual-integrity`
- `visual-provenance`
- `publication-accessibility`
- `review-response-protocol`

其余 skills 由 `match_visual_skills` 选择、`load_visual_skill` 按需加载，避免每个简单请求都携带完整领域知识。每个 skill 可在 `references/` 提供一层按需知识；首次加载返回文件名，只有当前决策需要时才以 `reference` 参数读取：

- `generic-quantitative-figure`
- `clinical-diagnostic-and-survival`
- `omics-genomics-and-enrichment`
- `event-neural-and-behavioral`
- `ordination-and-multivariate`
- `analytical-chemistry-and-diffraction`
- `time-frequency-and-signals`
- `relational-networks-flows-and-trees`
- `time-series-and-trajectories`
- `uncertainty-and-statistical-graphics`
- `spatial-geospatial-fields`
- `scientific-imaging`
- `mesh-volume-particle-visualization`
- `molecular-material-visualization`
- `scientific-schematic`
- `multi-panel-composition`

每个目录使用 kebab-case 名称，并包含仅有 `name` 与 `description` frontmatter 的 `SKILL.md`。新增或修改后运行：

```bash
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py nodes/postprocess/skills/<skill-name>
```

skill 描述科学与设计约束；实际 capability 仍由 tool/backend manifest 决定。skill 不能把未安装的 adapter 变成“已支持”。
