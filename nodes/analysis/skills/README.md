# nodes/<my_node>/skills/

节点专属 skill 放这里。这个目录由 `core/skill_loader.py` 自动扫描。

**完整规范见 [`docs/skill-spec.md`](../../../docs/skill-spec.md)** —— frontmatter 字段 / 工作流写作 / step-by-step / 完整 dummy 例子 / tool 对照表。这里只是 quick reference。

**Skill 不是 Python 代码** —— 是 markdown 指引。folder + SKILL.md 形态。

## 加新 skill（2 步）

**1. 建 skill 目录** + **写 SKILL.md**：

```bash
mkdir -p nodes/<my_node>/skills/my_recipe
cp templates/SKILL.md.template nodes/<my_node>/skills/my_recipe/SKILL.md
# 编辑：frontmatter + 工作流 + 输入输出 + pitfalls + 例子
```

**2. 在 harness.yaml 的 skills 白名单加 skill 名**：

```yaml
skills:
  - my_recipe         # 跟 SKILL.md frontmatter 的 name: 一致
```

bootstrap 启动时 skill_loader 自动扫描 `nodes/<my_node>/skills/*/SKILL.md` 注册。**不需要 `__init__.py`**。

## Skill 写作要点（template 里有完整模板）

- `frontmatter.applies_when` 写具体（什么场景启用 —— LLM 看到会触发）
- 步骤是**动作**不是描述（"调 search_kb 找 X" 而不是 "考虑现有 KB"）
- 写完整例子 —— LLM 看例子比看抽象描述准
- pitfalls 列踩过的坑（"不要直接写 KB，先 propose"）
- `frontmatter.node_type` 可填来限定只本节点可用（不填则 shared）

## 引用 shared/skills/ 现成 skill

`shared/skills/` 下面有 4 个共享 skill 可直接用，不必复制到本节点：

| skill 名 | 用途 |
|---|---|
| `systematic_literature_search` | 系统化文献检索 |
| `falsifiability_pretest` | 可证伪性预检 |
| `claim_evidence_link` | claim ↔ evidence 链路追踪 |
| `persistent_directive_promotion` | user 持久偏好识别 → PROFILE 升级 |

在 harness.yaml 的 skills 白名单直接写名字即可。
