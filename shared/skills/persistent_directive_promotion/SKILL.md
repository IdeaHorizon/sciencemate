---
name: persistent_directive_promotion
description: 当 user 在对话中说出"以后 / 始终 / 每次 / 我偏好"类持久偏好时，识别 + 即时写 directive 进 directives.md + 同时提议升级到 PROFILE.md (user 全局) 或 PROJECT.md (项目级)
applies_when:
  - 你是 _orchestrator 节点
  - user 在自然对话中说出持久性约束信号
  - 要把临时约束沉淀成 user 显式确认过的稳定偏好
tools_used:
  - memory_write(section='law')
  - read_profile
  - propose_profile_update
status: validated
relevant_concepts: []
---

## 触发信号（user 自然语言里出现以下表述时）

- **绝对持续**：以后 / 始终 / 每次 / 永远 / 一直
- **偏好声明**：我偏好 / 我喜欢 / 我习惯 / 别给我
- **规则化**：作为规则 / 默认就 / 默认 / 标准做法 / 默认情况下
- **项目级**：我的项目里 / 这个项目 / 本仓库 / 本课题

⚠️ **反例（不算持久信号，仍可即时写 directive 但不升级）**：
- "这次..."、"现在..."、"先..."、"本轮..." → session/run 级，写 directive 即可
- "请..."、"麻烦..." → 单次请求，可能根本不该入 memory

## 工作流（4 步）

### 1. 即时写 directive（无论是否升级都做）

立刻把 user 说的话翻成 directive，**即时**追加到 `memory/directives.md`。下一轮 LLM
call 自动重注入 system prompt：

```
memory_write(section='law', 
  text='<提炼后的一句指令>',
  applies_to_node='<某节点>' 或 None   # None=全局，对所有节点生效
)
```

这步**必做**：即使后面 propose 被 user 拒了，directive 也保留作为短期约束。如果
长期没升级 / 反复触发，curator dreaming 后续会建议归档或升 PROFILE/PROJECT。

### 2. 判 scope（user 全局 vs 项目级）

| 信号 | scope | 写到 |
|---|---|---|
| "以后做所有项目都..." / 跨领域偏好（语言、写作风格、术语） | `user` | PROFILE.md |
| "本项目..."/ "我们这次研究..." / 跟项目主题强绑定的偏好 | `project` | PROJECT.md |
| 不确定 → 默认 `user`（PROFILE 比 PROJECT 通用） |

### 3. 判 section（写到 .md 文件的哪个 markdown 标题下）

常见 section：

- `## 交互偏好` —— 回复风格、语言、长度
- `## 写作偏好` —— 论文 / 报告 / 文档写法（引用风格、章节结构）
- `## 工具偏好` —— 喜欢用什么库 / 框架
- `## 项目约束` —— 仅 PROJECT.md：数据集 / 方法范围
- `## 节点级指令` —— 仅 PROJECT.md：特定节点的额外规则（用 `### <node_type>` 子段）

如果不确定 / 找不到合适的：用最贴近的；append_to_section 找不到 header 会自动建。

### 4. 调 propose_profile_update（不是直接写文件）

```
propose_profile_update(
  scope='user' 或 'project',
  section='## <你判断的 section>',
  new_content='<想 append 的一行 / 一段>',
  reasoning='<非空；说清楚 user 什么时候、说什么话才有这个提议>',
  operation='append'   # 默认 append；replace_section / replace_all 极少用
)
```

返回 `{"proposal_id": "prop_xxx"}`。**user 必须 accept 才会真正写入文件**。

## 完整例子

**例 1（user 全局偏好）**：

user: "以后回复都用中文，别用英文"

→
```
memory_write(section='law', text='总是用中文回复 user')
propose_profile_update(
  scope='user',
  section='## 交互偏好',
  new_content='- 默认用中文回复（user 2026-05-13 明确要求）',
  reasoning='user 在 chat 中说：以后回复都用中文，别用英文'
)
```

回话给 user：
> 收到。已即时写入 directives.md，下一轮起所有节点都看到；同时提议加到你的
> PROFILE.md `## 交互偏好`。用 `/status` 看 inbox，或直接说"接受 prop_xxx"。

**例 2（项目级 + 节点级）**：

user: "我们这个项目调研的时候，重点关注有实验数据的论文"

→
```
memory_write(section='law', text='调研时优先含实验数据的论文',
                       applies_to_node='literature')
propose_profile_update(
  scope='project',
  section='## 节点级指令',
  target_node='literature',
  new_content='- 优先关注含实验数据的论文（vs 纯理论 / 纯综述）',
  reasoning='user 2026-05-13 在 chat 中说：本项目调研重点关注实验数据论文'
)
```

**例 3（session 临时，仍即时写 directive 但不升级）**：

user: "这次先帮我看下 GAP 这条线"

→
```
memory_write(section='law', text='本轮聚焦 GAP 方向调研',
                       applies_to_node='literature')
```

不调 propose_profile_update —— "这次"是临时性信号。后续 curator dreaming 看到这条
没复现 / 没升级，会建议归档。

## Pitfalls

- ❌ 跳过 step 1（写 directive）：如果 user 拒 propose，约束就消失了。directives.md 是兜底。
- ❌ scope 选错：跨项目的偏好（语言、术语）写到 PROJECT.md，下次开新项目就丢了。
- ❌ reasoning 太短 / 没提 user 原话：未来 user 看 inbox 不知道为啥提的。最少 10 字符。
- ❌ 一次 propose 多条：每条偏好独立 propose，user accept 粒度才合理。
- ❌ 把"observation"伪装成 directive：user 在陈述事实（"GAP 不收敛"）不是给指令，
  应该用 `memory_note(category='observation')` 而不是 memory_write。
