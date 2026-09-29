"""把按人分存的项目层合成「一个项目一份」（`docs/RFC_PROJECT_HOME_20260924.md`）。

## 为什么需要这一步

项目层从前住在每个人的 harness home 底下（`state/users/<uid>/projects/<项目>`）：一个项目几个成员
就有几份。改成一个项目一份（`config.the_projects_home()`）之后，**旧的那些目录不会自己搬家** ——
不合过来，升级之后项目的知识、记忆、作业账本、历史 run 在界面上和 agent 眼里就都没了。

## 为什么在启动时自动做（`org_layer_merge` 是手动命令，这里不是）

组织层的合并跨**所有人**（「这些人的知识从此互相可见」），是运维该拍的板。这里只在**同一个项目的
成员**之间合 —— 他们本来就在一起做这个项目，这正是这件事要的结果。而且没有人会来跑一条命令：
个人版的用户不开终端，组织服务器是桌面替人装、自己升级的。

## 合并规则（与 `org_layer_merge` 同一套纪律）

- **抄过去，不搬走。** 源目录一个字节都不动；确认没问题之后由人自己删。
- **按 id 并。** KB 与记忆的 jsonl 一行一条、带 id：同 id 同内容跳过；同 id 不同内容（一个人改过它的
  lifecycle），留 `updated_at` 新的那条，另一份仍在源目录里，记一行日志。
- **作业账本按行并**（它是追加的操作日志，同一个作业有多行）；从前「谁跑的」靠账本在谁的 home 里推，
  这里把来源那个人记到登记行上（`by_user_id`），账本合成一份之后还认得出。
- **历史 run 用硬链接抄。** 记录里有指向 run 目录的绝对路径，搬走会断；整份复制会让磁盘翻倍。
  硬链接两边都在、不多占空间；跨文件系统时退回复制。
- **按形态合，不按名单**：jsonl 按记录并、run 一类的目录按整个子目录抄、别的目录递归、别的文件
  没有就抄（两边不一样留先到的那份、记一笔）。项目层底下的东西还会再长，按名单合就会在升级时
  悄悄丢掉新加的那种。**派生的不抄**（`PROJECT_MANIFEST.md`、向量索引、锁文件）：它们自己会重建。
- **幂等。** 每个源合过之后记下它当时的样子（`.merged_from.json`）；没变就不再碰。旧版本的 worker 在
  升级后还写了几行（停靠的 worker 跨升级活着），下一次启动只把新的那几行合过来。
- 同一个项目在盘上两种拼法（带连字符的 uuid / 32 位 hex）合成一个（`str(UUID)`）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: 合过哪些源、合的时候它们是什么样子。
LEDGER = ".merged_from.json"

#: 按 id 并的 jsonl（KB 各实体、项目记忆、别的一行一条的记录）。
_ID_FIELDS = ("id", "claim_id", "concept_id", "chunk_id", "proposal_id", "entry_id")
#: 按行并的 jsonl：追加的操作日志，同一个 id 本来就有多行。
_BY_LINE = ("jobs.jsonl",)
#: 整目录一份份抄过去的（id 全局唯一）。
_TREES = ("runs", "sessions")
#: 派生的、会自己重建的 —— 不抄。
_DERIVED = ("PROJECT_MANIFEST.md", "kb_embeddings", LEDGER)


@dataclass
class Outcome:
    projects: int = 0
    records: int = 0
    runs: int = 0
    conflicts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def the_same_project(name: str) -> str:
    """同一个项目的两种拼法 → 一个名字。不是 uuid 的原样留着。"""
    try:
        return str(uuid.UUID(name))
    except ValueError:
        return name


def merge_the_per_person_copies(state_root: Path, projects_home: Path) -> Outcome:
    """把 `state_root/users/*/projects/*` 合进 `projects_home/<项目>`。"""
    outcome = Outcome()
    people = state_root / "users"
    if not people.is_dir():
        return outcome
    projects_home.mkdir(parents=True, exist_ok=True)
    ledger_path = projects_home / LEDGER
    ledger = _read_ledger(ledger_path)
    for person in sorted(p for p in people.iterdir() if p.is_dir()):
        theirs = person / "projects"
        if not theirs.is_dir() or theirs.resolve() == projects_home.resolve():
            continue
        who = the_same_project(person.name)      # 人的 id 也有两种拼法
        for source in sorted(p for p in theirs.iterdir() if p.is_dir() and not p.name.startswith(".")):
            looks = _how_it_looks(source)
            if ledger.get(str(source)) == looks:
                continue
            target = projects_home / the_same_project(source.name)
            target.mkdir(parents=True, exist_ok=True)
            _merge_one(source, target, who=who, outcome=outcome)
            ledger[str(source)] = looks
            outcome.projects += 1
            # 每合完一个就记一笔：中途停了，下次从没合的那个接着来。
            _write_ledger(ledger_path, ledger)
    if outcome.projects:
        logger.info("项目层合成一个项目一份：%d 个来源、%d 条记录、%d 个历史 run；%d 处同 id 不同内容取了新的",
                    outcome.projects, outcome.records, outcome.runs, len(outcome.conflicts))
    for line in outcome.conflicts:
        logger.warning("项目层合并：%s", line)
    for line in outcome.skipped:
        logger.info("项目层合并跳过：%s", line)
    return outcome


def _merge_one(source: Path, target: Path, *, who: str, outcome: Outcome) -> None:
    """一个目录合进另一个 —— 项目层里有什么就合什么，不按名单挑。

    项目层底下的东西比看上去多（`tasks/` 任务合同、`.harness/` 花费账、`kb_edges.jsonl`、
    `research_intake.json`……），而且还会再长。按名单只合认识的，新加的那种就在升级时悄悄丢了；
    所以规则按**形态**定，不按名字：jsonl 按记录并，run 一类的目录按整个子目录抄，别的目录递归，
    别的文件没有就抄、两边不一样就留先到的那份（另一份还在源目录里）并记一笔。
    """
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        name = item.name
        if name in _DERIVED or name.endswith((".lock", ".merging")):
            continue
        there = target / name
        if item.is_dir():
            if name in _TREES:
                _merge_trees(item, there, outcome=outcome)
            else:
                _merge_one(item, there, who=who, outcome=outcome)
            continue
        if name in _BY_LINE:
            outcome.records += _merge_lines(item, there, who=who)
        elif name.endswith(".jsonl"):
            outcome.records += _merge_by_id(item, there, outcome=outcome)
        elif not there.exists():
            shutil.copy2(item, there)
        elif there.read_bytes() != item.read_bytes():
            outcome.conflicts.append(f"{there} 两份不一样，留了先合进来的那份（另一份在 {item}）")


def _identity(record: dict) -> str | None:
    for key in _ID_FIELDS:
        if record.get(key):
            return f"{key}:{record[key]}"
    return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _merge_by_id(source: Path, target: Path, *, outcome: Outcome) -> int:
    have = _read_jsonl(target)
    index = {key: i for i, rec in enumerate(have) if (key := _identity(rec))}
    added = 0
    changed = False
    for rec in _read_jsonl(source):
        key = _identity(rec)
        if key is None:
            if rec not in have:
                have.append(rec)
                added += 1
                changed = True
            continue
        if key not in index:
            index[key] = len(have)
            have.append(rec)
            added += 1
            changed = True
            continue
        mine = have[index[key]]
        if mine == rec:
            continue
        if str(rec.get("updated_at") or "") > str(mine.get("updated_at") or ""):
            have[index[key]] = rec
            changed = True
        outcome.conflicts.append(f"{target.name} 里 {key} 两份不一样，留了 updated_at 新的（另一份在 {source}）")
    if changed:
        _write_jsonl(target, have)
    return added


def _merge_lines(source: Path, target: Path, *, who: str) -> int:
    have = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    seen = set(have)
    added = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        stamped = _stamp_who(line, who)
        if line in seen or stamped in seen:
            continue
        seen.add(stamped)
        added.append(stamped)
    if added:
        with target.open("a", encoding="utf-8") as fh:
            fh.write("".join(f"{line}\n" for line in added))
    return len(added)


def _stamp_who(line: str, who: str) -> str:
    """登记一个作业的那一行，记上是谁的会话起的 —— 从前这件事靠账本在谁的 home 里推。"""
    try:
        record = json.loads(line)
    except ValueError:
        return line
    if not isinstance(record, dict) or record.get("_op") != "declare" or record.get("by_user_id"):
        return line
    return json.dumps({**record, "by_user_id": who}, ensure_ascii=False)


def _merge_trees(source: Path, target: Path, *, outcome: Outcome) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(p for p in source.iterdir() if p.is_dir()):
        there = target / child.name
        if there.exists():
            continue
        try:
            shutil.copytree(child, there, copy_function=_link_or_copy, symlinks=True)
        except OSError as exc:
            outcome.skipped.append(f"{child}（抄不过去：{exc}）")
            continue
        outcome.runs += 1


def _link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".merging")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    tmp.replace(path)


def _how_it_looks(source: Path, prefix: str = "") -> dict[str, list[int]]:
    """合的时候它是什么样子：每个文件的大小与修改时间（递归），run 一类的目录里有几个 run。"""
    looks: dict[str, list[int]] = {}
    for item in sorted(source.iterdir()):
        key = f"{prefix}{item.name}"
        if item.is_file():
            stat = item.stat()
            looks[key] = [stat.st_size, stat.st_mtime_ns]
        elif item.is_dir() and item.name in _TREES:
            looks[key] = [sum(1 for _ in item.iterdir())]
        elif item.is_dir() and item.name not in _DERIVED:
            looks.update(_how_it_looks(item, f"{key}/"))
    return looks


def _read_ledger(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_ledger(path: Path, ledger: dict) -> None:
    tmp = path.with_suffix(".merging")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
