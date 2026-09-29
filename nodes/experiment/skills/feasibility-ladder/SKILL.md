---
name: feasibility-ladder
description: |
  预注册要求的资源（算力 / 软件 / 数据 / 凭证 / 人工环节）拿不到时，按四级阶梯
  处理并在穷尽后合法声明 infeasible。任何"这一项好像做不了"的时刻使用。
applies_when:
  - prereg 的某项资源需求在本环境不可得或疑似不可得
  - 撞上平台限制（egress 白名单、配额、可写根、凭证缺失）
  - 已经在考虑"换一个能跑的做法""缩小体系""找替代数据源"
tools_used:
  - safe_run_bash
  - search_kb
  - fetch_resource
  - request_human_input
  - save_artifact
  - report_blocker
expected_outcome: 资源被合法获取并继续执行，或产出一份能触发框架强制 REDIRECT 的 infeasible 声明
status: validated
---

# 可行性阶梯

对 prereg 的**每一项**资源需求逐级上升，**不许跳级，更不许静默换一个能跑的实验**。

## ① probe —— 先探测，别凭直觉说"没有"

`safe_run_bash` 查硬件（`nproc` / `free -h` / `nvidia-smi` / `df -h`）、查软件
（`which` / `pip show` / `conda list`）、查数据文件在不在。"没有 benchmark 环境"
这类结论必须有 probe 证据 —— 多数开源 benchmark/工具本机就能装能跑。

## ② acquire —— 先分清缺口是哪一类，再决定走法

判据只有一条：**现实世界里这东西拿得到吗？**

- **客观缺失**（软件真没有 / 数据真不存在 / 站点真的没了）
  → 装开源软件；缺上游数据先看本 run 转发的 artifact 或 `search_kb`；
  只有 prereg 明确声明的输入生成步骤才可执行。
  注意：生成"实验**输入**"合法，捏造"实验**结果**/ground truth"不合法。

- **平台限制**（egress 白名单 / 配额上限 / 权限与可写根 / 凭证缺失
  —— 现实世界拿得到，是平台挡的）
  → **不绕开、不改科学设计，直接请求拆墙**。向 owner 说清四件事：
  哪道墙、需要什么（具体域名 / 配额值 / 路径 / 账号）、为什么需要（对应 prereg 哪一条）、
  以及现成可执行的解除操作。

  找替代源与改预注册是**最后手段**，理由必须是科学的，不能是"平台不让我拿"。
  因为一堵墙就把 Q1 的对照从 A 降级成 B，是配置缺口吃掉了科学设计。

### ② 的前置事实：执行沙箱按设计无网

`safe_run_bash`、`safe_execute_python` 和本地 job 所在的 RunAttempt **没有网络**。
`curl` / `wget` / `git clone` 在里面必然失败，反复试探只是烧轮次；宿主机有网
**不等于**当前 Attempt 有网，不要据此判断"网络没问题"。

外部文件或公开 Git 源码需要落盘时，唯一通道是 `fetch_resource`：它在只挂空
staging 的受控下载容器里联网，再把校验后的文件/源码导入获准写根。
`fetch_resource` 被 egress 策略挡住 → 那是**平台限制**，走上面的请求拆墙，
不是"数据源不可达"，更不是改科学设计的理由。

## ③ ask —— 拿不准就问

"能不能简化 / 人工环节可否跳过 / 要不要花钱装环境" → `request_human_input`。
用户在，问一句比猜便宜得多。一次问全：带 options + 推荐项 + 无应答默认。

## ④ declare infeasible —— 合法终态，但必须按格式声明

①②③ 穷尽后确实执行不了，这是**合法终态**。声明格式是硬要求，框架按字段识别：

experiment_log 的 content 里写一个独立段：

```
## Feasibility
verdict: infeasible
缺什么：<具体到域名 / 数据集 / 软件版本 / 配额值>
试过什么：<probe 证据、acquire 尝试、ask 的结果，各带路径或工具输出>
建议上游怎么降级：<给 hypothesis 一个可执行的修改方向>
```

同时在 `save_artifact` 的 `metadata` 里带：

```
infeasible: true                       # 必须，布尔真值
infeasible_reason: "<一句话：缺什么、试过什么、为什么不行>"
redirect_target: "hypothesis"          # 可选，默认 hypothesis
```

框架读到声明会**机械强制 REDIRECT** 回上游降级设计（优先级盖过 reviewer 推荐和
fail-closed），你不用自己纠结"交不了差怎么办"。段落标题写 `## 可行性` 也认，
判定词写"不可执行/无法执行/不可行"也认。

声明 infeasible 的 run **禁止**翻 claim status —— `validated`/`refuted` 会被框架
拒绝：没真做实验就没有证伪权。

## ⛔ 绝对红线

发现任务执行不了时，把实验**偷偷换成另一个能跑的**（例如造一批按假设设计好
ground truth 的 synthetic 数据当真实验跑）＝ 循环验证 ＝ 与数据造假同级的违规。
要么走 ② 合法获取，要么走 ④ 声明 infeasible。

本 Skill 只管"这件事能不能做、做不了怎么合法收场"。它不授权修改冻结 prereg，
也不代替 verdict：**输入齐但数据判不动**（confound / 缺 metric / N 太小）走
`verdict: inconclusive`，不是 infeasible。
