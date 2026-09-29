"""这台执行主机提供什么算力软件 —— 平台的一等事实，不该由 agent 猜。

## 为什么需要它（2026-08-10 E2E v25 实测）

experiment 节点要跑 LAMMPS，它是这么探的：

    which lammps 2>/dev/null || which lmp 2>/dev/null || echo "LAMMPS not in PATH"

二进制叫 `lmp_serial`，**两个名字都没中**。于是它判定"没装"，转头去
`conda install -y -c conda-forge lammps`（改用户的 conda 环境，被审批门拦住）。
而 LAMMPS 一直好好地在 `/opt/homebrew/bin/lmp_serial` —— **上一轮会话还用它
跑成功过**。

追下去发现的不是一个探测 bug：

    project_resources 表（项目绑定的外部资源+凭据）  0 行，且不管软件
    discover_resources 工具                          只探调度器和硬件
                                                     （local/slurm/k8s、核数、GPU）
    build_platform_context_snapshot                  项目 id / 版本 / 模型后端
                                                     —— 一个字不提算力软件
    全仓                                             不存在"主机装了什么"这个概念

**平台不知道自己有什么。** 于是每一轮会话都得靠模型猜二进制名，而猜是名单式
的 —— 新装的东西、非常规命名，默认漏过。这就是"护栏要扫盘不要写名单"那条
教训在**发现**这一侧的同款形态。

## 为什么放这里，而不是 project_resources / MEMORY.md

- `project_resources` 是**项目**绑定的外部资源（集群、数据库、对象存储 +
  `secret_ref` 凭据）。"这台机器上 lmp_serial 在哪"不是项目属性 —— 同一台
  主机上每个项目都一样。放进去等于每个项目抄一份。
- 每项目 `MEMORY.md` 同理：把部署事实写进项目记忆，换个项目又要重新发现。

它是**部署属性**，所以由部署声明、被所有会话共享。

## 声明而不是探测

登记表由人写（`HARNESS_HOST_CAPABILITIES` 指向一个 YAML）。理由：

1. 科研软件的调用方式往往不只是一个二进制（module load、conda env、
   license server、MPI 启动器）。这些**只有部署的人知道**。
2. 商业软件将来要作为注册工具接进来（见项目决策），那本来就是登记制。

**但登记表必须自证**：加载时逐条核对声明的可执行文件真的存在。一份说
"LAMMPS 在 X"而 X 不存在的登记表，比没有登记表更糟 —— 它让模型信一个假事实。
核不上的条目照样交出去，但**带着醒目的 `⚠️ 登记的路径不存在`**，让错误可见
而不是静默。

空登记表也**明说**"平台没有登记任何软件"，并告诉模型正确的探测姿势 ——
沉默会被读成"这里什么都没有"。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "HARNESS_HOST_CAPABILITIES"


@dataclass(frozen=True)
class HostSoftware:
    """一件已登记的算力软件。"""

    name: str
    invoke: str
    version: str = ""
    notes: str = ""
    #: 声明的可执行文件当前是否真的找得到。None = 没法判断（invoke 不是一个
    #: 可直接检查的路径，比如 `module load lammps && lmp`）。
    resolvable: bool | None = None

    def render(self) -> str:
        head = f"- **{self.name}**：`{self.invoke}`"
        if self.version:
            head += f"（{self.version}）"
        if self.resolvable is False:
            head += "  ⚠️ 登记的路径当前不存在，用之前先自行确认"
        if self.notes:
            head += f" —— {self.notes}"
        return head


def _resolvable(invoke: str) -> bool | None:
    """能不能确认这条 invoke 现在真的可用。

    只对"一个裸命令 / 一个绝对路径"给结论。带 `&&`、管道、module load 这类
    组合命令返回 None —— **不知道就说不知道**，别把"我判断不了"渲染成
    "它坏了"，那会让人对真正的红色警告脱敏。
    """
    token = (invoke or "").strip()
    if not token or any(ch in token for ch in "|&;<>$`\n"):
        return None
    if Path(token).is_file():
        return os.access(token, os.X_OK)
    import shlex
    try:
        first = shlex.split(token, posix=os.name != "nt")[0].strip('"')
    except (ValueError, IndexError):
        return None
    if Path(first).anchor or first.startswith("./"):
        return Path(first).is_file() and os.access(first, os.X_OK)
    return shutil.which(first) is not None


def load(path: str | os.PathLike | None = None) -> list[HostSoftware]:
    """读取登记表。没配置 / 读不出来 → 空列表（不抛）。

    读一份**可选**的部署声明失败，绝不能改变 run 的形状 —— 这条教训在
    `project_autonomy_policy` 上付过一次学费（把可选查询放进包住主流程的 try 里，
    它一抛就顶掉了真正的 failure）。
    """
    target = str(path or os.getenv(ENV_VAR) or "").strip()
    if not target:
        return []
    file = Path(target).expanduser()
    if not file.is_file():
        return []
    try:
        import yaml

        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    entries = data.get("software") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[HostSoftware] = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        invoke = str(row.get("invoke") or "").strip()
        if not name or not invoke:
            continue
        out.append(
            HostSoftware(
                name=name,
                invoke=invoke,
                version=str(row.get("version") or "").strip(),
                notes=str(row.get("notes") or "").strip(),
                resolvable=_resolvable(invoke),
            )
        )
    return out


def render_section(path: str | os.PathLike | None = None) -> str:
    """开局说明里的"这台主机有什么"那一段。

    空登记表也要出这一段：沉默会被读成"这里什么都没有"，而模型接下来会去
    `which <猜一个名字>`，猜不中就装软件。所以空的时候明说是"平台没登记"，
    并给出**扫盘式**的探测姿势，而不是让它继续猜两个名字。
    """
    items = load(path)
    lines = ["## 🧰 这台执行主机提供的算力软件（平台登记，机器生成）"]
    if items:
        lines.extend(item.render() for item in items)
        lines.append(
            "以上是平台**登记过**的部分，不代表全集；要用别的东西，先按下面的"
            "方式探测，别假设它不存在。"
        )
    else:
        lines.append(
            "平台**没有登记任何算力软件**（这不等于机器上没有）。"
        )
    lines.append(
        "探测姿势：`ls /opt/homebrew/bin /usr/local/bin 2>/dev/null | grep -i <关键词>`、"
        "`compgen -c | grep -i <关键词>`、`conda list | grep -i <关键词>` —— "
        "**扫一遍再下结论**。只试两三个猜出来的名字然后判定'没装'，是 2026-08-10 "
        "实测过的真实事故：LAMMPS 装在 `lmp_serial`，`which lammps || which lmp` "
        "两个都没中，节点转头去装了一遍。"
    )
    lines.append(
        "探到了有用的环境事实（某软件的真实调用方式、版本、坑），用 "
        "`memory_note(text=...)` 记下来 —— "
        "否则下一轮会话要重新发现一次。"
    )
    return "\n".join(lines)
