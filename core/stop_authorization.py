"""「这个任务允许中途停下作业吗」—— 由派发方给出的、带身份的授权事实。

## 为什么必须是派发方给的

Experiment 的 `expected_termination.planned_stop` 现在只核对一件事：模型引的那句话
是不是逐字出自任务正文。而「任务正文里有这句话」和「任务授权了停止」是两回事 ——
`"完整跑完大约 10 分钟"` 也逐字出自任务正文，引它一样通过。于是子 run 的模型可以
给任意本地作业声明 planned_stop，把一次取消记成「按计划停止」，收尾写 success；
没有这个声明时同样的局面要求收尾写 blocked。

节点**刻意不**去用关键词或语义正则猜「这句话是不是在授权停止」——那等于让执行者
自己给自己发授权（#1084 第二节 owner 原话）。所以授权只能由**派发方**显式给出，
Core 在子 run 开工前把它冻住，子 run 只能引用，不能新建。

Core 不判断自然语言：它只负责「派发方说了没有」这件事有没有一个可引用的身份。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: 授权身份的字段名 —— 节点按这个名字引用它。
POLICY_ID_KEY = "planned_stop_policy_id"


def _path(state: Any):
    """授权冻在 run 根下这一个文件里。

    文件名写成字面量：`state.root / <变量>` 会被「别手拼 run 根解析存下来的相对
    路径」那道扫盘闸拦下，而它拦的是另一件事（把别人存的相对路径解回文件）。
    这里是本 run 自己的一份固定产物，和 `pause_pending.json` 同类。
    """
    return state.root / "stop_authorization.json"


def mint_policy_id(run_id: str, note: str) -> str:
    """身份由 (run_id, 授权说明) 决定：同一次派发算出同一个 id，换一次派发就换一个。

    它是**这一趟**的授权，不是一张可以到处用的通行证。
    """
    raw = f"{run_id}\x00{note}".encode()
    return "psp_" + hashlib.sha256(raw).hexdigest()[:16]


def freeze(state: Any, *, authorized: bool, note: str = "",
           authorized_by_run_id: str | None = None) -> dict | None:
    """在子 run 开工前把授权冻住。已经有了就**不覆盖**（授权不能中途变宽）。

    `authorized=False` 时什么都不写：没有授权这件事不需要一份记录来证明，
    而写一份 `authorized: false` 只会让"没授权"和"授权被撤了"混成一件事。
    """
    path = _path(state)
    if path.exists():
        return read(state)
    if not authorized:
        return None
    record = {
        POLICY_ID_KEY: mint_policy_id(str(getattr(state, "run_id", "")), note or ""),
        "authorized_by_run_id": authorized_by_run_id,
        "note": str(note or "")[:500],
        "scope": "run",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        state.append_transcript("planned_stop_authorized", **record)
    except Exception:
        pass
    return record


def read(state: Any) -> dict | None:
    """读回本 run 的停止授权；没有就是 `None`（= 没授权）。"""
    if getattr(state, "root", None) is None:
        return None
    path = _path(state)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return record if record.get(POLICY_ID_KEY) else None


def planned_stop_policy_id(state: Any) -> str | None:
    """本 run 的停止授权身份；没授权就是 `None`。"""
    record = read(state)
    return record.get(POLICY_ID_KEY) if record else None


def authorizes_planned_stop(state: Any, policy_id: str | None) -> bool:
    """模型引的那个 id 到底是不是本 run 真有的那一个。

    空 id 一律为假 —— "没引" 和 "引对了" 不能落到同一个答案上。
    """
    if not policy_id:
        return False
    return policy_id == planned_stop_policy_id(state)
