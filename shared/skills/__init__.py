"""shared/skills —— 跨节点通用 skill 池（v2.1：folder-based SKILL.md）。

各 skill 是一个 folder（必含 SKILL.md），由 bootstrap 自动扫描加载：
  - shared/skills/<name>/SKILL.md           ← framework-shipped
  - nodes/<node>/skills/<name>/SKILL.md     ← 节点本地
  - $HARNESS_FRAMEWORK_HOME/org/skills/<name>/SKILL.md  ← 导入 / 自发现

加载入口：core/skill_loader.py:load_all_skills()
注册表：core/skill_registry.py
"""
