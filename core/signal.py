"""External signal mechanism —— 任何 driver / 外部进程都能跟跑中的 agent loop 通信。

设计动机（v0.7）：
  chat.py 的 stdin queue 让 user 中途打断 orchestrator —— 但这是 chat.py 特例。
  e2e_dogfood.py / 后台 cron / 自动化场景**没有任何渠道**跟跑中的 agent 说话，
  v4 dogfood 实测：experiment 节点 LLM 自我 scale up 跑了 2.5h，但没有任何机制
  能告诉它"够了，wrap up"。

  解法：基于文件的 control signal。任何进程都能写一个 JSON 文件，agent loop
  的 hook 每轮 turn_start 自动 check 文件，把内容当 system message 注入 messages
  + 删文件。

API:
  write_signal(project_id, action, content=None)  ← 外部进程调
  read_signal(project_id) -> dict | None           ← hook 调
  clear_signal(project_id)                          ← 消费后删

文件位置:
  <project_root>/control_signal.json  (project-scoped；orchestrator 自动看本项目)

action 类型（语义层 —— 全靠 LLM 解读，framework 不硬卡）：
  - "inject"  : 普通 system message 注入；LLM 自行决定怎么用
  - "pause"   : 注入一段提示"调 request_human_input 等下一条 user 指令"
  - "abort"   : 注入"立刻 wrap up，写 final artifact 然后退"

  这三个都是同一机制（注入 system message）的语义糖；区别只在 content 模板。

跨 driver:
  - chat.py 仍可用 stdin queue（旧路径不动）；signal file 是新增的旁路
  - e2e_dogfood.py / run_node.py / cron / 任何外部进程：写 signal file 即可
  - 同一项目 signal 是排他的（同时只 1 个 pending；先到先得）

CLI helper:
  python -m core.signal inject <project_id> "<message>"
  python -m core.signal pause <project_id>
  python -m core.signal abort <project_id>
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.paths import project_dir as _paths_project_dir

log = logging.getLogger("signal")

SignalAction = Literal["inject", "pause", "abort"]


_PAUSE_TEMPLATE = (
    "外部 signal action='pause'。请**立刻**调 "
    "`request_human_input(question='我已暂停。等你下一条指示', "
    "context='current state: <一两句总结你正在干啥>')` 进入暂停状态。"
    "user 答复后会通过 driver resume 接着跑。"
)

_ABORT_TEMPLATE = (
    "外部 signal action='abort'。**立刻 wrap up**：\n"
    "  1. 如果有未完成的 child run / pending 决策，stop 起新子节点\n"
    "  2. 用现有的 artifact / KB 写一段简短的 final 汇报\n"
    "  3. 然后**直接** return 给 user（不再调 run_node / 起任何新工作）\n"
    "不要继续 scale / 重试任何东西。够用就行。"
)


def _signal_path(project_id: str) -> Path:
    """signal 文件位置：<project_root>/control_signal.json。

    依赖 paths.project_dir 统一项目根路径解析（跟 KB / memory 一致）。
    """
    return _paths_project_dir(project_id) / "control_signal.json"


def write_signal(
    project_id: str,
    action: SignalAction,
    content: str = "",
    *,
    overwrite: bool = False,
) -> dict:
    """外部进程写入 signal。

    overwrite=False（默认）→ 如果已有 pending signal 则 raise，防止意外覆盖。
    overwrite=True → 强制覆盖（一般用于 abort 这种紧急 signal）。
    """
    if action not in ("inject", "pause", "abort"):
        raise ValueError(
            f"action 必须 ∈ {{'inject','pause','abort'}}, got {action!r}"
        )

    # 把语义 action 转成实际 inject content
    if action == "pause":
        effective_content = _PAUSE_TEMPLATE
    elif action == "abort":
        effective_content = _ABORT_TEMPLATE
    else:  # inject
        if not content or not content.strip():
            raise ValueError("action='inject' 时必须传非空 content")
        effective_content = content

    path = _signal_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not overwrite:
        existing = read_signal(project_id)
        raise FileExistsError(
            f"已有 pending signal at {path}: "
            f"action={existing.get('action')} at {existing.get('written_at')}。"
            f"调 clear_signal() 先清或用 overwrite=True 强写。"
        )

    record = {
        "action": action,
        "content": effective_content,
        "raw_user_content": content,    # 即使是 pause/abort 也留 user 原意
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    tmp.replace(path)
    return record


def read_signal(project_id: str) -> dict | None:
    """Hook 调：读 signal 文件。返 dict 或 None（没文件 / 坏 JSON）。"""
    path = _signal_path(project_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("control_signal.json 解析失败 (%s)：%s", path, e)
        return None


def clear_signal(project_id: str) -> bool:
    """Hook 消费 signal 后清；返 True 表示真删了，False 表示没文件。"""
    path = _signal_path(project_id)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        log.warning("control_signal.json 删除失败 (%s)：%s", path, e)
        return False


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli_main(argv: list[str]) -> int:
    """python -m core.signal <action> <project_id> [<content>]"""
    if len(argv) < 3 or argv[1] not in ("inject", "pause", "abort", "show", "clear"):
        print(__doc__.strip().splitlines()[-5:][0])
        print("\nUsage:")
        print("  python -m core.signal inject <project_id> '<message>'")
        print("  python -m core.signal pause <project_id>")
        print("  python -m core.signal abort <project_id>")
        print("  python -m core.signal show <project_id>")
        print("  python -m core.signal clear <project_id>")
        return 1

    cmd = argv[1]
    project_id = argv[2]

    if cmd == "show":
        sig = read_signal(project_id)
        if sig is None:
            print(f"(no pending signal for project {project_id!r})")
            return 0
        print(json.dumps(sig, ensure_ascii=False, indent=2))
        return 0

    if cmd == "clear":
        if clear_signal(project_id):
            print(f"✓ cleared signal for {project_id}")
            return 0
        print(f"(no signal to clear for {project_id})")
        return 0

    content = argv[3] if len(argv) > 3 else ""
    try:
        rec = write_signal(project_id, cmd, content, overwrite=True)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    path = _signal_path(project_id)
    print(f"✓ wrote signal action={cmd!r} to {path}")
    print(f"  content preview: {rec['content'][:140]}...")
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv))
