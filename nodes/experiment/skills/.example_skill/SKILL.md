---
name: .example_skill
description: dummy skill 示例 —— 演示 save_artifact 前的 3 件 check 事
applies_when:
  - safe_run_bash 跑完真实模拟 / 计算
  - 准备 save_artifact 一个长 log 类 artifact
  - returncode == 0 但你不确定 log 内容是否完整
tools_used:
  - read_file
  - save_artifact
  - freeze_artifact
  - memory_note
expected_outcome: experiment_log 写完并 frozen；可疑情况写 memory candidate 让下游 / curator 加工后看到
status: validated
relevant_concepts: []
---

## 工作流（save log 前 3 段 check）

1. **读 log 全文**：`read_file(path='<your log path>')` —— **不要**只看 safe_run_bash 返回的 stdout_tail
2. **三段 check**：
   - 包含完成标记（如 LAMMPS 的 "Total wall time"）
   - 没有 ERROR / WARN（除了已知可忽略的）
   - 最后一段输出数据合理（不是 NaN / Inf / 空）
3. **正常路径** → `save_artifact` 把完整 log + 实验参数 + cmd + wall_time 一起存
4. **freeze** → `freeze_artifact(artifact_id)` —— log 立即不可改
5. **异常路径** → 不要 freeze，改写 memory candidate：
   `memory_note(text='<原因：什么条件下会出什么后果>', category='pitfall', nodes=['experiment'], tools=['safe_run_bash'])`
   （**没有 `add_memory_candidate` 这个工具**；category 只有 `pitfall` / `method`，
   适用面走 `nodes` / `tools`，没有 `tags`）
   curator dreaming 会 consolidate 进 known_pitfalls.md / workflows.md，下游 _reviewer
   节点 / 后续 experiment run 通过 MEMORY.md 索引看到

## Pitfalls

- ❌ 只看 stdout_tail 就 save（LAMMPS 错误写 log 不写 stdout）
- ❌ returncode==0 但 log 缺完成标记也保存（子进程可能被 kill）
- ❌ 把 ERROR 当 OK 存进 KB（污染数据）
- ✅ 不确定时写 memory candidate 而非 freeze

## 完整例子

```python
# 假设 safe_run_bash 跑完了
result = await safe_run_bash(cmd="lmp_serial -in in.lammps -log log.lammps", cwd=...)

# 即使 returncode==0 也读全文
log = await read_file(path="log.lammps")
ok_marker = "Total wall time" in log["content"]
has_error = "ERROR:" in log["content"]

if ok_marker and not has_error:
    art = await save_artifact(
        artifact_type="experiment_log",
        name="LJ_NVT_run01",
        content=f"cmd: {result['cmd']}\nwall_time: {result.get('wall_time')}\n\n{log['content']}",
        metadata={"returncode": 0},
    )
    await freeze_artifact(artifact_id=art["id"])
else:
    await memory_note(
        text=f"LAMMPS suspicious: ok_marker={ok_marker} has_error={has_error}",
        category="pitfall",
        nodes=["experiment"],
        tools=["safe_run_bash"],
    )
```

## 为什么是 skill 不是 tool

这是**多步工作流模板**，不是新能力。工具组合（read_file + save_artifact +
freeze_artifact + memory_note）框架都有 —— 只是 LLM 不知道该按这个顺序 + 这种条件分支用。
skill 把"领域有经验的人会这么做"显式化。

## 跟 hook 的区别

hook 是**强制每轮注入**。skill 是**LLM 在 system_prompt 看到，按需套用**。
本 skill 用 skill 路径 —— 不是每次实验都强制走，是 LLM 判断到适用场景时套用。
