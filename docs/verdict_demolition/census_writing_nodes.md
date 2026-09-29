# 普查：nodes/{writing,hypothesis,literature,_reviewer,observation,derivation}（2026-08-31）

计数：510 拒绝点（含 errors.append/failures[]/reasons[] 累积式）→
A=39 / B=100 / C=223 / **D=148**。writing/tools/validation.py 单文件 D=68。

## D 类逐条（裁决对象）

### writing/tools/validation.py（68）
736 preflight plan 必须带 metadata.input_audit ｜ 748 必须带每个 REQUIRED_PLAN_METADATA 键 ｜
771 manuscript metadata 必须带每个 REQUIRED_MANUSCRIPT_METADATA 键 ｜ 777 format 只能是 latex ｜
789 input_audit 必须带三键 ｜ 804 passed 稿必须带 handoff_audit ｜ 822 plan 的 input_audit 必须带 handoff_audit ｜
830 evidence_inventory 必须带每个 REQUIRED_EVIDENCE_FIELDS ｜ 884 必须用掉派发的每一件产物 ｜
890 plan.upstream_artifacts_used 必须用掉每一件 ｜ 989/991/996/1001/1006/1011/1035/1040/1045/1050/1055/1060
引用（kb claim/upstream/外部文献/分析结论/图表）必须是 preflight plan 预列清单的子集且列表非空 ｜
1018 passed 稿至少记一件 upstream artifact ｜ 1110 pdf_path 必须指 review 变体 ｜
1174 pdf_variants 的 line_numbers 必须等于变体名约定值 ｜ 1180 variant status 必须 compiled ｜
1218 clean PDF 不得可见渲染内部 claim id ｜ 1225 LaTeX 源不得含未解析标记 ｜
1228/1233/1240 blocked 稿不得含引用命令/claim_id/thebibliography ｜ 1256 template_used 必须与 venue 一致 ｜
1298 metadata 必须带 venue 要求的每个点分路径字段 ｜ 1317 writing_qc_summary 必须在正文前 600 字符内 ｜
1322 qc_summary 必须列出每个必需字段 ｜ 1488 portability.status 必须 PASS ｜ 1567 word_count.status 必须 counted ｜
1569 pdf_status 必须 compiled_review_and_clean ｜ 1574 bundle 必须含 submission_manifest.json ｜
1607 投稿包必须含每张字面图形且不得绝对路径 ｜ 1661/1667/1673/1678 面向发表必须至少一张有证据图（plan/inventory/LaTeX/bundle 四处各查一遍） ｜
**1683 至少一张图必须来自 approved 的 publication figure_package** ｜ 1711 journal_fit 必须带每个字段 ｜
1748 scope_fit 判弱必须同时记 blocking 或 revision items ｜ 1787/1794/1799 submission_readiness 必须带必需字段/checked_items 列表/含每个必查项 ｜
1814 blocked readiness 必须列 blocking_items ｜ 1856 project_layout 必须匹配 venue 布局 ｜
1872 human_submission_checks 必须非空列表 ｜ 1886 venue 要求 DOCX 时 docx_status 必须 generated 或 missing_required_conversion ｜
1924 bundle 必须含每个 PDF 变体 ｜ 2019 output_layout 必须匹配 venue/template ｜ 2025 该 venue 必须有 generated bundle ｜
2037 required_project_files_present 必须 true ｜ 2042 必须有结构化 sci_structure_check ｜
2050/2058 reference_hygiene 必须存在且带每个必需键 ｜ 2124/2127 unresolved_latex_references/unresolved_markers 必须为空 ｜
2133 仅 kb 内部书目不得标 submission_ready ｜ 2142 外部文献 0 时必须记 revision_items ｜
2160 submission_ready 稿硬校验错误必须为零 ｜ 2485 冻结要求存在绑定本 content+双 PDF hash 的 passed 校验报告 ｜
2526 冻结要求存在绑定同 hash 的独立 approve/proceed 评审

### writing 其余（10）
| file:line | 强制的要求 |
|---|---|
| submission.py:187 | figure_package 必须过 validate_publication_figure_package（publication+approved） |
| submission.py:220 | 裸 source_path 只能 static_asset；科学图必须走 figure_package_id |
| submission.py:278 | 资产后缀必须在白名单内 |
| compact_delivery.py:498 | 自动建 plan 时 KB claim <5 条不给建 |
| compact_delivery.py:608 | 紧凑交付只接受 preflight_status=passed |
| compact_delivery.py:617 | plan claim <5 条拒绝生成 |
| compact_delivery.py:855 | 校验未 passed 整个交付 error |
| material_gap_delivery.py:459 | 材料缺口交付要求 ≥5 真实 KB claim |
| input_audit.py:773 | 冻结 prereg 闭合条目零兑现判 blocked「没有可报告的研究结论」 |
| scripts/build_submission_bundle.py:325 | portability 审计不 PASS 不出包 |

### hypothesis（11）
research_state.py:176 hypotheses 非空 ｜ 200 withdrawn 必须写 reason ｜ 204 有父版本 change_reason 必填 ｜
232 ready_candidate 不得留未裁决条目 ｜ research_questions.py:131 必须有研究问题 ｜ 230 必须有 research_plan ｜
242 每个问题必须被某步认领 ｜ 315 问题不得疑似复述已知结论 ｜ 335 必须存在 hypothesis_innovation_report ｜
356 每个问题必须有价值/可信性评估 ｜ threshold_grounding.py:324 必须找到 structured falsifier 才判过

### literature（3）
classify_papers.py:87 主题分类至少 2 篇 ｜ finalize_evidence_package.py:34 只在三种模式下可用 ｜
49 included_papers 空时必须填 unknowns

### _reviewer/critique_builder.py（10）
123 评审 manuscript 必须同时有 review+clean 两个 PDF ｜ 294/296 两个 summary 非空 ｜
314 每条 step 的 why/how 非空 ｜ 345 redirect_upstream 必须给 target_node ｜
357 finalize 前 verdict 与 recommended_action 必须已设 ｜ 370 必须用 source_node_type='_project' ｜
381 必须已设 project_verdict 与 scores ｜ 389 ready_to_write 不得同时有 blocks_writing step ｜
395 iterate 必须至少一条 blocks_writing step

### observation/observation_contract.py（26）
180/182 finding 必须有 statement/evidence ｜ 185 abductive 必须列 competing_explanations ｜
212 metadata 必须齐全部取样纪律字段 ｜ 224/233 exploratory 不许写 closure_discharges/measured_metrics ｜
252 findings 非空 ｜ 310 confirmatory 还要齐 CONFIRMATORY_REQUIRED_PATHS ｜
505/510 结果表必须给齐语义角色/semantic_contract 非空 ｜ 533 每行必须可核验 source_ref ｜
545 图内 label ≤64 字符 ｜ 553/557/561 event_timeline 必须显式 lane_order/覆盖每个 lane/声明 time_label ｜
583/585 矩阵必须显式 claim_order/dimension_order ｜ 607 单元格不得重复（上游先裁决） ｜
616 矩阵必须完整，无证据格显式 verdict=missing ｜ 626 composition 必须显式 component_order ｜
644 diverging 必须上游给带符号差值且 reference_value=0 ｜ 654 零点两侧都要有值除非 allow_one_sided ｜
660/662/666 雷达 ≥3 轴/value_range 只接受 [0,1]/轴方向一致 ｜ 684 每 group 恰含每轴一次

### derivation/derivation_contract.py（20）
203 steps 非空 ｜ 216/220 每步必须有 claim/justification ｜ 224 justification 剥套话后必须剩实质（反"显然"闸） ｜
248 每步要么带 verification 要么标免验类别 ｜ 330/334 assumption 必须有 statement/discharged_by ｜
427 main_result 必须有 expression+statement ｜ 434 expression 不得含中文（sympy 可解析） ｜
**516 派发要求的 rigor_level 必须兑现，或在 credibility 里如实降级** ｜ 599 audit_verdict 必须有 reasoning ｜
607 flawed 必须点名 flawed_steps ｜ 638 被判缺陷步骤必须挂证据 ｜ 678 metadata 必须齐必需路径 ｜
684/691 exploratory 不许写 closure_discharges/measured_metrics ｜ 722 confirmatory/audit 要齐各自路径 ｜
**737 验证结论 failed 的步骤不得留在链上** ｜ 762 只有数值支持的步骤必须在 credibility 交代 ｜ 796 findings 非空

## 骑墙判例
1. validation.py:1218 clean PDF 含内部 claim id → D（泄的是平台自造 id 非密钥）。
2. derivation_contract.py:737 failed 步骤留链上 → D（章是真的，结论不该建其上——与伪造验证章 B 类完全不同）。
3. podsys_safe_source.py 全 33 处 → B（包自我断言冻结事实，放行即包自己说谎），0 D。
4. critique_builder.py:389 评审意见自相矛盾 → D（意见矛盾如实记录账仍真；事实台账矛盾才是 B）。
5. submission.py:220 图必须走 figure_package_id → D（与 283 hash 对不上的 B 同循环不同性质）。
