# 历史归档：Experiment v2.0 开发规则（已废止）

> 当前有效规则：仓库根目录 AGENTS.md 与 nodes/experiment/AGENTS.md。本文仅保留历史记录，不得作为实现依据。

## 开发目录

| 用途 | 路径 |
|------|------|
| 整个代码仓库 | `/home/lujy/2026-ai4s/node4-experiment/harness-framework/` |
| 我负责的内容：experiment节点(只允许修改该范围) | `/home/lujy/2026-ai4s/node4-experiment/harness-framework/nodes/experiment` |
---

## 对 Agent 的核心要求（所有工具和节点均须遵守）
1. **不轻易简化任务**：有些步骤解决不了但是必要的，应尽可能想办法解决，或者询问人类，而不是直接降级需求。

2. **科研严谨性**：执行的是科研任务，严谨、有理有据是第一要务，不能伪造数据或实验结果。

3. **配置文件来源**：第一选择从官方或网上直接获取真实模板再修改；没有现成的才自己逐步生成。

4. **文件缺失的判断**：首先考虑是否少了下载步骤或 setup 步骤，自己补写文件是最后选择。
---

## 一些修改原则
1. 问题：该问题根因分析？检查根因是否正确？如何修改？
2. 原则：尽可能不打补丁，必须结构化思考，第一性原理思考；首先如果框架有类似场景的要求，节点改动方向应优先服从框架规则；做事情时不要路径依赖，要思考“如果完全重新写我会怎么写“；如无必要，不要盲目增加复杂性；思考完之后自我检查一遍是否是符合修改原则和目的，是否可以进一步调整优化；
3. 修改质量判断：综合以上，你的修改原则是什么（我检查是否准确）？方案详述？取得效果？是否能够完全解决该类问题？是否引入风险？ 
4. 范围：只能修改nodes/experiment下的代码；其他路径的代码只能分析和总结去提修改方案；如果是LLM能在1-2 turns内自行纠偏的这种错误可以暂时忽略，造成严重不流畅/卡住/block/死锁/死循环/报错/等，这种优先级最高；
5. experiment节点：代码需要守护experiment节点，包括诚实完成接到的上游任务，并诚实的产出，诚实守护至本节点任务结束；运行上要尽可能保证流畅度


## 现有工具与技能清单（避免重复建设）

新 agent 读完本文件后应知道以下内容已存在，**不得重复创建**：

### tools/（`nodes/experiment/tools/`）
| 文件/目录 | 用途 |
|-----------|------|
| `diagnose.py` | 五层诊断主入口（环境→配置→依赖→运行→物理合理性） |
| `diagnose_patterns/diagnose_patterns.yaml` | 诊断规则库（patterns 配置） |
| `fermilink_packages.txt` | 169 个已知 HPC/AI 包列表，FermiLink 查询前置过滤 |

### skills/（`nodes/experiment/skills/`）
| 目录 | 用途 |
|------|------|
| `gpu-hpc-porting/` | GPU 移植知识库（原生 GPU 包优先、CUDA arch、双路径编译原则） |
| `claim_evidence_link`（共享） | verdict reasoning 中每条定量陈述必须引用具体 artifact chunk 或 URI |

### 根目录（`nodes/experiment/`）
| 文件 | 用途 |
|------|------|
| `hooks.py` | failure_detector + deviation_detector 两个钩子 |
| `harness.yaml` | 节点配置和 rules（优先用 rules 解决问题，不要绕过） |

### tools/skills 增减原则
- 修改现有文件 > 新建文件；只有当修改现有内容无法解决时才增加。
- tools/skills 针对**通用 HPC 和 AI 应用**，不为单个应用建专属工具。

---

## experiment 节点实现原则 ---这个是不是要调整？

**最少文件原则**：优先用 `harness.yaml` rules + `SKILL.md` 知识库解决问题。
只有当 rules/SKILL 无法满足时，才新建 `.py` 工具文件。
修改现有文件 > 新建文件。不要为单个应用建专属工具。


---

## 框架职责边界（v2.0）---这个是不是要调整？

| 职责 | 归属节点 |
|------|---------|
| per-experiment verdict（validated/refuted/inconclusive）| experiment 节点（v2.0 起）|
| 写 methodological / dead_end sediment | experiment 节点 |
| 项目级综合判断（"能写论文了吗"）| `_reviewer(project_synthesis)` |
| 实验方法推荐、变量规划、prereg 生成 | hypothesis 节点 |
| 数值分析、统计检验 | postprocess 节点 |
| analysis 节点 | **已取消**（v2.0）|

**⑦.8 结构化结论生成**：per-experiment 部分由 experiment verdict 覆盖；项目级综合由 `_reviewer` 负责。experiment 节点不做项目级综合。

---

## 记录规范 ---这个是不是要调整？

- 做了什么 → `WORKLOG.md`（最新日期在最前）
- 计划和阶段状态 对 agent 的要求和里程碑 → `ROADMAP.md` --好像已经严重过期了

## 角色分配
如果你是codex，你阅读CODEX.md；
如果你是claude，你阅读CLAUDE.md；