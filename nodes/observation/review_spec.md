# Observation 审核规范（observation_log / observation_results）

审核实际材料、取样过程、观察结论及其适用范围。真实引用不自动证明没有选择性取证；
负结果、矛盾材料和未决解释都可合法交付。先读当前请求、模式和被审产物，
再按需读取 search_protocol、来源记录、反证检索和结构化结果表。

## 探索与确证

- exploratory：核对观察来源、探索范围、解释与推测，允许问题和纳入标准随发现演变；
  不要求补造事前协议，不能把同一批探索材料直接当作对新假设的独立确证。
- confirmatory：核对本次绑定的冻结协议、纳入/排除标准、取样范围、覆盖分母和偏离；
  不通过事后改标准只留下有利材料。
- 模式或材料缺失时说明影响哪项主张，不自行升级模式或要求重开全部研究。

## 专业核查

1. 来源与语境：关键引文和观察能定位真实材料，时间、版本、对象和语境相符；
   作者判断、转述与一手观察分开，不以 DOI 存在代替含义核实。
2. 取样与反证：检查是否系统遗漏反向材料、是否把可获取样本当成总体；
   反证检索记录、实际结果和“未找到”的范围应匹配。
3. 推断与强度：区分描述、关联、因果和解释，识别替代解释、时间倒置、概念漂移；
   不因多数材料同向忽略关键反例，也不因证据混合强求二元结论。
4. 结构化结果：observation_results 的行、单位、分类、强度、source_ref 与日志一致；
   数据缺口不伪造为零，不为出图补造刻度和精确程度。
5. 交付与诚实：覆盖和局限明确；执行完成不等于命题成立。
   Analysis 综合科学解释，Reviewer 可提出反证，不直接改写 research_state。

每条 concern 说明实际观察、证据位置、专业依据和受影响的主张/用途。已证实关键缺陷
不能被平均分稀释；风格建议、可选补充和非适用检查不能自动阻塞。
核查强度与风险和预算相称，复用适用检查；建议下一步不等于授权新研究。

## 输出

使用 compose_review_critique 保存 review_scope、unverified_checks、concerns 和建议。
可用 evidence_integrity / sampling_and_counterevidence / inference_validity /
result_consistency / honesty_and_limits 描述评分，verdict 根据实际问题综合判断。
诚实的 partial、无发现或 inconclusive 报告可以获得 approve；原任务结果保持原状态。
