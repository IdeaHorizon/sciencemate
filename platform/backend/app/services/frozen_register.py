"""记录账本的判读 —— 平台侧镜像实现（纯 stdlib，零 app 依赖）。

## 为什么这是一份镜像

权威定义在 harness 的 `core/ledger.RecordStore.pinned`。后端进程**不**
import harness core（harness 跑在子进程里，两边依赖树独立），所以 commit 咽喉
需要自己的一份判读。两份实现分叉的后果是静默的：一边认为路径已解除钉死、
另一边照旧回滚 —— 合法的修订被闸吃掉，而且报错指向假原因。
`tests/test_frozen_register_contract.py` 把两边钉在同一份 fixture 上。

## 为什么单独一个模块

它此前长在 `project_repository` 里，而那个模块 `from app.config import settings`
→ 拉起 pydantic_settings。于是**契约测试在 harness 的 CI job 里根本 import 不进来**
（那个 job 不装后端依赖）。账本判读是纯文本解析，没有任何理由依赖应用配置；
拆出来之后两边的 CI 都跑得动它 —— 让守卫真的被执行，而不是 skip 掉。

## 行语义（与 core/ledger 逐字对应）

账本是 `.research/ledger/records.jsonl`（RFC 2026-09-12 §6），每个身份折账：

- `save`：head 换成这一版（`path` / `sha256`）；带 `amendment` 的 save 同时解除
  上一版的钉死（冻结的字节在 git 历史里，checkpoint 改不了历史）。
- `freeze`：把 head 钉死在 `path@sha256`。
- `retire`：head 撤下，什么都不钉。

返回 `{相对路径: sha256}` —— 冻结且尚未修订的 head。
"""
from __future__ import annotations

import json
from pathlib import Path

#: 账本相对工作区根的位置（与 core/ledger.LEDGER_RELATIVE 一致）。
LEDGER_RELATIVE = ".research/ledger/records.jsonl"


def apply_rows(registry: dict[str, str], lines: list[str]) -> dict[str, str]:
    """把账本的行按语义折到 registry 上（就地更新并返回 {path: sha256}）。"""
    heads: dict[str, dict] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        artifact_id = str(row.get("id") or "").strip()
        event = str(row.get("event") or "")
        if not artifact_id:
            continue
        if event == "save":
            heads[artifact_id] = {
                "path": str(row.get("path") or "").strip(),
                "sha256": str(row.get("sha256") or "").strip(),
                "version": int(row.get("version") or 1),
                "frozen_version": 0,
            }
        elif event == "freeze":
            head = heads.get(artifact_id)
            if head is not None:
                head["frozen_version"] = int(row.get("version") or head["version"])
                head["sha256"] = str(row.get("sha256") or head["sha256"]).strip() or head["sha256"]
        elif event == "retire":
            heads.pop(artifact_id, None)
    for head in heads.values():
        if (head["frozen_version"] == head["version"] and head["path"]
                and len(head["sha256"]) == 64):
            registry[head["path"]] = head["sha256"]
    return registry


def read_register(register_path: Path) -> dict[str, str]:
    """一份账本 → {被钉死的相对路径: 冻结时正文 sha256}。"""
    try:
        lines = Path(register_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return apply_rows({}, lines)


def scan_worktree(root: Path) -> dict[str, str]:
    """工作区账本 → 被钉死的路径。一个工作区一本账，没有第二处要扫。"""
    return read_register(Path(root) / LEDGER_RELATIVE)
