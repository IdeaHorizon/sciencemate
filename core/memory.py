"""项目记忆 —— Git worktree 里的一个 Markdown 文件，没有第二个。

## 四层模型（见 docs/RFC_MEMORY_REBUILD_20260821.md）

    层      变化速率    权威            落盘           送达
    ────────────────────────────────────────────────────────────────
    宪法    ~从不      用户            本模块          每轮冻结前缀
    局面    每 run     盘面事实         **不落盘**      现算注入
    手册    缓慢积累    任意节点         本模块          切片 + 首用附单
    日志    追加       框架            已存在三份      从不注入

本模块只管**落盘的那两层**：宪法与手册。局面由 `core.research_situation`
现算（写下来的"现状"必然漂移）；日志已经有 transcript / Git / 决策账本
三份权威载体，不再写第四份。

## 为什么手册没有候选队列

「这条经验值不值得留」的判据，在写下它的那一刻**不具备**。设在出生处的门
只能拦掉一部分放过一部分，两边都不对 —— 实测代价是 47% 的候选从未被加工、
最老积压 49 天。所以手册是**零门禁 + 机械约束 + 惰性维护**：
写入即入册，去重/结构由机械保证，curator 只做维护（合并、退休、呈现矛盾），
不做准入。这与 project 层 KB 是同一条判断。

## 节级所有制

每一节有且只有一个写者，且是**机械核对**的（不是 prompt 约定）：

    研究目标 / 科研铁律   用户（经调度器转录，须引文逐字核对）
    叙事                 调度器（覆写语义，白板心智模型）
    手册·坑 / 手册·方法   任意节点，但只经 `append_manual` 这一个入口

通用 `write_file` 对 MEMORY.md **对所有节点拒绝**（含 curator）——
写入口只有本模块，否则所有制就只是注释。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from shared.lib import filelock

log = logging.getLogger(__name__)

MEMORY_FILENAME = "MEMORY.md"

# ── 节 ──────────────────────────────────────────────────────────────────────

SECTION_GOAL = "goal"                  # 研究目标（用户原话）
SECTION_LAW = "law"                    # 科研铁律（用户立的规矩）
SECTION_NARRATIVE = "narrative"        # 叙事（为什么走这条路）
SECTION_PITFALL = "manual_pitfall"     # 手册·坑
SECTION_METHOD = "manual_method"       # 手册·方法

#: 写者归属。`user` 表示须引文核对；`_orchestrator` 表示单节点覆写；
#: `*` 表示任意节点，但只能经 `append_manual`（结构化追加，非自由覆写）。
SECTION_OWNER = {
    SECTION_GOAL: "user",
    SECTION_LAW: "user",
    SECTION_NARRATIVE: "_orchestrator",
    SECTION_PITFALL: "*",
    SECTION_METHOD: "*",
}

SECTION_TITLE = {
    SECTION_GOAL: "研究目标",
    SECTION_LAW: "科研铁律",
    SECTION_NARRATIVE: "叙事",
    SECTION_PITFALL: "手册 · 坑",
    SECTION_METHOD: "手册 · 方法",
}

#: 手册两节 —— 结构化条目，只能追加，不能自由覆写。
MANUAL_SECTIONS = (SECTION_PITFALL, SECTION_METHOD)

#: 铁律也是结构化条目：每条要能**单独撤销**，并且带自己的出处。
#: 整节覆写会让一条新律顺手抹掉旧律，而铁律恰恰是最不该被顺手抹掉的东西。
ENTRY_SECTIONS = MANUAL_SECTIONS + (SECTION_LAW,)

SECTIONS = (SECTION_GOAL, SECTION_LAW, SECTION_NARRATIVE,
            SECTION_PITFALL, SECTION_METHOD)

# ── 预算 ────────────────────────────────────────────────────────────────────
#
# 手册**不设条数上限**：它是参考区，靠 applies_to 切片送达，不全文注入。
# 真正要守的是各送达通道的常数预算（见 core.memory_delivery）。
# 拒绝新教训来保住文件大小是本末倒置 —— 总量压力由遗忘作业消化。

#: 宪法两节的注入预算。长于这个数说明写的不是宪法。
CONSTITUTION_BYTE_CAP = 2_048
#: 叙事节预算（白板语义：一块板子，不是一本日志）。
NARRATIVE_BYTE_CAP = 2_048

#: 手册条目的最短长度 —— 比这更短的不构成一条教训。
MIN_ENTRY_CHARS = 8
#: 近似去重阈值：词集 Jaccard ≥ 此值视为同一条。
NEAR_DUP_JACCARD = 0.75
#: 复发多少次判定为系统性缺陷（复发不是知识，是待修的 bug）。
DEFECT_RECURRENCE = int(os.getenv("HARNESS_MEMORY_DEFECT_RECURRENCE", "5"))

_OPEN = "<!-- section:{name} -->"
_CLOSE = "<!-- /section:{name} -->"
#: 手册条目的机读尾注。人读正文，机器读这一行。
_META_RE = re.compile(
    r"<!--\s*@\s*(?P<body>.*?)\s*-->", re.DOTALL)


class MemoryError_(RuntimeError):
    """记忆写入被机械约束拒绝。"""


# ── 路径：全框架唯一解析点 ──────────────────────────────────────────────────
#
# 上一代这个不变量只是注释，被绕开了 5 处，造成过"两份互不相干的记忆"。
# 现在它是唯一路径 —— 没有 project_root 分支可绕。


def memory_path(state: Any) -> Path | None:
    """本项目的 MEMORY.md。没绑 worktree 的匿名 run 没有项目记忆。"""
    wt = getattr(state, "project_worktree", None)
    return (Path(wt) / MEMORY_FILENAME) if wt else None


# ── 文档级读写 ──────────────────────────────────────────────────────────────


def read_document(state: Any) -> str:
    p = memory_path(state)
    if p is None or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("read MEMORY.md failed: %s", e)
        return ""


def _block_re(name: str) -> re.Pattern[str]:
    return re.compile(
        re.escape(_OPEN.format(name=name)) + r"\n?(?P<body>.*?)\n?"
        + re.escape(_CLOSE.format(name=name)), re.DOTALL)


def read_section(state: Any, name: str) -> str:
    """读一节正文。节不存在返回空串 —— 空节与不存在对读者是同一件事。"""
    m = _block_re(name).search(read_document(state))
    return m.group("body").strip() if m else ""


def _prologue(document: str) -> str:
    """所有分节之外的正文 —— 人手写的、或本机制之前的历史内容。

    迁移不是删除：老 MEMORY.md 整份是无标记正文，必须原样留着，
    否则第一次分节写入就把历史吃掉了（2026-08-18 真出过这个事故）。
    """
    rest = document or ""
    for n in SECTIONS:
        rest = _block_re(n).sub("", rest)
    return rest.strip()


def _render(document: str, name: str, body: str) -> str:
    """把 name 那一节换成 body，其余（含别的节与无标记正文）原样保留。"""
    block = f"{_OPEN.format(name=name)}\n{body.strip()}\n{_CLOSE.format(name=name)}"
    pat = _block_re(name)
    if pat.search(document or ""):
        return pat.sub(lambda _m: block, document, count=1).strip() + "\n"
    kept: list[str] = []
    pro = _prologue(document or "")
    if pro:
        kept.append(pro)
    for other in SECTIONS:
        if other == name:
            continue
        m = _block_re(other).search(document or "")
        if m and m.group("body").strip():
            kept.append(f"{_OPEN.format(name=other)}\n{m.group('body').strip()}\n"
                        f"{_CLOSE.format(name=other)}")
    kept.append(block)
    return "\n\n".join(kept).strip() + "\n"


def _write_document(state: Any, text: str) -> None:
    p = memory_path(state)
    if p is None:
        raise MemoryError_("本 run 没有绑定 Project worktree —— 没有项目记忆可写")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


class _Lock:
    """MEMORY.md 的写锁。

    上一代无锁：read-modify-write 整表重写 vs 并发 append，实测会**静默吞掉**
    记录。并行 subagent 是常态，所以锁不是可选项。
    """

    #: 锁落在项目自己的锁目录里 —— 那里已经是 gitignored 的（建仓时就写死了
    #: `.research/locks/`）。
    #:
    #: 以前锁是 `MEMORY.md.lock`，就摆在 MEMORY.md 边上：一个 0 字节的文件，
    #: **未跟踪、不被忽略**，于是它出现在用户的文件树里、出现在"这一轮改了
    #: 什么"的见证里、也占着 checkpoint 的一行。锁是框架的内务，不是研究产出
    #: —— 内务有自己的地方，把它放到那儿去，比在展示层写一条"别显示这个名字"
    #: 的规则要短，而且新的锁自动落对。
    _LOCKS_RELATIVE = (".research", "locks")

    def __init__(self, path: Path) -> None:
        self._lock_path = path.parent.joinpath(*self._LOCKS_RELATIVE) / f"{path.name}.lock"
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._lock_path.open("a+")
        filelock.acquire(self._fh)
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            filelock.release(self._fh)
            self._fh.close()
            self._fh = None


def _locked(state: Any):
    p = memory_path(state)
    if p is None:
        raise MemoryError_("本 run 没有绑定 Project worktree —— 没有项目记忆可写")
    return _Lock(p)


# ── 节写入（所有制核对在调用方 core.memory_tools，这里是机制）────────────────


def write_section(state: Any, name: str, body: str) -> dict:
    """覆写一节。**手册两节不走这里** —— 它们只能经 append_manual 追加。"""
    if name not in SECTIONS:
        raise MemoryError_(f"未知节 {name!r}，合法值：{list(SECTIONS)}")
    if name in MANUAL_SECTIONS:
        raise MemoryError_(
            f"{name!r} 是手册节，只能用 memory_note 逐条追加 —— "
            "整节覆写会把别人写的教训一次抹掉")
    if name == SECTION_LAW:
        raise MemoryError_(
            "科研铁律逐条追加（memory_write 会走 append_law），不整节覆写 —— "
            "一条新律不该顺手抹掉旧律")
    cap = CONSTITUTION_BYTE_CAP if name in (SECTION_GOAL, SECTION_LAW) \
        else NARRATIVE_BYTE_CAP
    n = len(body.encode("utf-8"))
    if n > cap:
        raise MemoryError_(
            f"{SECTION_TITLE[name]} 写入 {n} 字节，超预算 {cap}。"
            f"这一节长到这个程度说明写进了不属于它的东西："
            f"经过程细节属手册（memory_note），现状属局面（框架现算，不落盘）。")
    with _locked(state):
        _write_document(state, _render(read_document(state), name, body))
    return {"status": "success", "section": name, "bytes": n}


# ── 铁律条目 ────────────────────────────────────────────────────────────────


@dataclass
class LawEntry:
    """一条科研铁律：**模型抽象的措辞** + **用户说过的出处**。

    为什么正文不是用户原话：用户的话往往零散、口语、跨好几轮，而且他常常
    不会说"你要长期遵守"，只是**反复强调**同一件事。把它变成一条能在 review
    时逐条回答的规矩，需要抽象、需要写得干练明确 —— 那是模型的活。

    为什么必须带出处：如果正文由模型写、又不要求出处，模型就能凭空立法，
    而铁律会被机械地在每次审查里执行。所以机械层核验的是**出处真实存在**
    （逐条在本 session 的用户消息里能找到），不是正文逐字相符。

    这跟 org 知识卡是同一条判断：晋升是**改写**不是搬运，改写后靠
    `promoted_from` 保住可审计性。判据搞混的代价这个仓库付过 ——
    「出自用户」和「逐字等于用户说的话」是两件事。
    """

    text: str
    derived_from: tuple[str, ...] = ()
    at: str = ""

    def meta_line(self) -> str:
        srcs = " ; ".join(s.replace("|", "/")[:60] for s in self.derived_from)
        bits = [f"derived={len(self.derived_from)}"]
        if self.at:
            bits.append(f"at={self.at}")
        if srcs:
            bits.append(f"src={srcs}")
        return "<!-- @ " + " | ".join(bits) + " -->"

    def render(self) -> str:
        return f"- {self.text.strip()}\n  {self.meta_line()}"

    def as_dict(self) -> dict:
        return {"text": self.text, "derived_from": list(self.derived_from),
                "at": self.at}


def parse_laws(body: str) -> list[LawEntry]:
    out: list[LawEntry] = []
    cur: str | None = None
    for line in (body or "").splitlines():
        meta = _META_RE.search(line)
        if meta and cur:
            m = _parse_meta(meta.group("body"))
            out.append(LawEntry(
                text=cur, at=m.get("at", ""),
                derived_from=tuple(s.strip() for s in (m.get("src") or "").split(";")
                                   if s.strip())))
            cur = None
            continue
        if line.strip().startswith("- "):
            if cur:
                out.append(LawEntry(text=cur))
            cur = line.strip()[2:].strip()
    if cur:
        out.append(LawEntry(text=cur))
    return [e for e in out if e.text]


def laws(state: Any) -> list[LawEntry]:
    return parse_laws(read_section(state, SECTION_LAW))


def append_law(state: Any, *, text: str, derived_from: Iterable[str]) -> dict:
    """立一条铁律。正文由调用方抽象，出处由框架核验（核验在工具层）。"""
    text = " ".join((text or "").split())
    srcs = tuple(dict.fromkeys(s.strip() for s in derived_from if s and s.strip()))
    with _locked(state):
        doc = read_document(state)
        existing = parse_laws(read_section(state, SECTION_LAW))
        norm = _norm(text)
        for e in existing:
            if _norm(e.text) == norm:
                return {"status": "success", "created": False,
                        "note": "这条铁律已经在了，没有重复添加。"}
        entry = LawEntry(text=text, derived_from=srcs, at=_now()[:10])
        existing.append(entry)
        body = "\n".join(x.render() for x in existing)
        if len(body.encode("utf-8")) > CONSTITUTION_BYTE_CAP:
            raise MemoryError_(
                f"铁律总量超预算 {CONSTITUTION_BYTE_CAP} 字节。"
                "铁律多到这个程度就不是铁律了 —— 先撤掉不再适用的"
                "（memory_write(section='law', retire=<正文前缀>)），"
                "或者把过程细节挪去手册（memory_note）。")
        _write_document(state, _render(doc, SECTION_LAW, body))
    return {"status": "success", "created": True, "entry": entry.as_dict()}


def retire_law(state: Any, prefix: str) -> dict:
    """撤一条铁律 —— 只有用户能撤，判据在工具层。

    定位判据是**唯一匹配**，不是前缀长度。长度阈值是个 Latin 中心的坏判据：
    中文五个字往往已经唯一，而英文二十个字符可能还匹配三条。真正要防的是
    "一个前缀撤掉了好几条"，那就直接判它。
    """
    p = _norm(prefix)
    if not p:
        return {"status": "error", "code": "empty_prefix",
                "error": "给一段正文前缀"}
    with _locked(state):
        existing = parse_laws(read_section(state, SECTION_LAW))
        hits = [e for e in existing if _norm(e.text).startswith(p)]
        if len(hits) > 1:
            return {"status": "error", "code": "ambiguous_prefix",
                    "error": f"{prefix[:40]!r} 匹配到 {len(hits)} 条铁律 —— "
                             "给更长的前缀，撤错一条比撤不掉贵得多",
                    "matches": [e.text[:60] for e in hits]}
        keep = [e for e in existing if not _norm(e.text).startswith(p)]
        if len(keep) == len(existing):
            return {"status": "error", "code": "not_found",
                    "error": f"没有以 {prefix[:40]!r} 开头的铁律",
                    "current": [e.text[:60] for e in existing]}
        _write_document(state, _render(read_document(state), SECTION_LAW,
                                       "\n".join(x.render() for x in keep)))
    return {"status": "success", "retired": len(existing) - len(keep)}


# ── 手册条目 ────────────────────────────────────────────────────────────────


@dataclass
class ManualEntry:
    """一条手册条目。

    `applies_to` 是**送达的地址**，也是**遗忘的判据** —— 一个字段解两个问题。
    写不出适用面的教训，与写不出 why 的知识卡同理：还没想清楚，不配入册。
    """

    text: str
    tools: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    run_id: str = ""
    commit: str = ""
    seen: int = 1
    last_seen: str = ""
    defect: bool = False
    section: str = SECTION_PITFALL

    def meta_line(self) -> str:
        bits = []
        if self.tools:
            bits.append("tools=" + ",".join(self.tools))
        if self.nodes:
            bits.append("nodes=" + ",".join(self.nodes))
        if self.run_id:
            bits.append("run=" + self.run_id)
        if self.commit:
            bits.append("commit=" + self.commit)
        bits.append(f"seen={self.seen}")
        if self.last_seen:
            bits.append("last=" + self.last_seen)
        if self.defect:
            bits.append("defect=1")
        return "<!-- @ " + " | ".join(bits) + " -->"

    def render(self) -> str:
        head = "- " + self.text.strip().replace("\n", " ")
        return f"{head}\n  {self.meta_line()}"

    def as_dict(self) -> dict:
        return {"text": self.text, "tools": list(self.tools),
                "nodes": list(self.nodes), "run_id": self.run_id,
                "commit": self.commit, "seen": self.seen,
                "last_seen": self.last_seen, "defect": self.defect,
                "section": self.section}


def _parse_meta(raw: str) -> dict:
    out: dict[str, Any] = {}
    for part in raw.split("|"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def parse_entries(body: str, section: str) -> list[ManualEntry]:
    """把一节正文解析回条目。解析不了的行**不丢** —— 当作无元信息的条目留着。"""
    entries: list[ManualEntry] = []
    cur_text: list[str] = []
    cur_meta: dict | None = None

    def flush() -> None:
        if not cur_text:
            return
        text = " ".join(t.strip() for t in cur_text).strip()
        if not text:
            return
        m = cur_meta or {}
        entries.append(ManualEntry(
            text=text,
            tools=tuple(t for t in (m.get("tools") or "").split(",") if t),
            nodes=tuple(t for t in (m.get("nodes") or "").split(",") if t),
            run_id=m.get("run", ""), commit=m.get("commit", ""),
            seen=int(m.get("seen") or 1), last_seen=m.get("last", ""),
            defect=bool(m.get("defect")), section=section,
        ))

    for line in (body or "").splitlines():
        meta = _META_RE.search(line)
        if meta:
            cur_meta = _parse_meta(meta.group("body"))
            continue
        if line.strip().startswith("- "):
            flush()
            cur_text = [line.strip()[2:]]
            cur_meta = None
        elif line.strip() and cur_text:
            cur_text.append(line)
    flush()
    return entries


def manual_entries(state: Any, *, section: str | None = None) -> list[ManualEntry]:
    out: list[ManualEntry] = []
    for name in (MANUAL_SECTIONS if section is None else (section,)):
        out += parse_entries(read_section(state, name), name)
    return out


# ── 去重（整体平移自上一代 —— 审计认定这是全系统唯一扎实的一段）────────────


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _shingles(norm: str) -> set[str]:
    """分词用的指纹集：拉丁按词，中文按**整条字符流**的 bigram。

    关键是"整条流"而不是"按空格分段后各自取 bigram"：中文里空格位置是
    任意的，同一句话有没有空格、空格打在哪都不改变意思。按段取会让边界处的
    bigram 凭空消失 —— 实测「声明所有检查通过前必须…」与「声明 所有检查通过
    之前 必须 …」Jaccard 只有 0.571（差的全是"前必""明所""过前"这种跨空格
    的字对），近似去重整个不响。

    修法是消除空格的影响，不是降阈值 —— 降阈值会让真正不同的教训被误合并。
    """
    latin = [t for t in re.split(r"[^a-z0-9]+", norm) if t]
    stream = "".join(re.findall(r"[一-鿿]", norm))
    grams = ([stream] if len(stream) == 1
             else [stream[i:i + 2] for i in range(len(stream) - 1)])
    return set(latin + grams)


def _prefix(norm: str) -> tuple[str, ...]:
    return tuple(norm.split()[:8])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_manual(state: Any, *, text: str, section: str,
                  tools: Iterable[str] = (), nodes: Iterable[str] = (),
                  run_id: str = "", commit: str = "") -> dict:
    """零门禁入册 —— 但过机械约束。

    近似重复不新增条目，而是给已有条目的 `seen` 计数 +1；到
    `DEFECT_RECURRENCE` 次标 `defect` —— 复发不是知识，是待修的系统性缺陷，
    应该停止记录、推动修根因。
    """
    text = (text or "").strip()
    if section not in MANUAL_SECTIONS:
        raise MemoryError_(f"section 必须 ∈ {list(MANUAL_SECTIONS)}")
    tools = tuple(dict.fromkeys(t.strip() for t in tools if t and t.strip()))
    nodes = tuple(dict.fromkeys(n.strip() for n in nodes if n and n.strip()))
    if not tools and not nodes:
        raise MemoryError_(
            "applies_to 必填：至少给 tools 或 nodes 其一。"
            "它是这条教训的**送达地址**（决定它在谁开工时、调哪个工具前弹出），"
            "也是**遗忘判据**（适用面失效即可机械退休）。"
            "写不出适用面，说明还没想清楚这条教训在什么情况下成立。")
    if not run_id and not commit:
        raise MemoryError_(
            "evidence 必填：至少给 run_id 或 commit 其一 —— "
            "陈旧扫描要能回查这条当年是被什么咬出来的。")

    with _locked(state):
        doc = read_document(state)
        body = _block_re(section).search(doc)
        existing = parse_entries(body.group("body") if body else "", section)

        norm = _norm(text)
        toks = _shingles(norm)
        pre = _prefix(norm)
        for e in existing:
            en = _norm(e.text)
            if en == norm:
                return _bump(state, section, existing, e, doc)
            et = _shingles(en)
            if not et or not toks:
                continue
            jac = len(toks & et) / max(1, len(toks | et))
            if jac >= NEAR_DUP_JACCARD or _prefix(en) == pre:
                return _bump(state, section, existing, e, doc)

        entry = ManualEntry(text=text, tools=tools, nodes=nodes,
                            run_id=run_id, commit=commit, seen=1,
                            last_seen=_now()[:10], section=section)
        existing.append(entry)
        _write_document(state, _render(
            doc, section, "\n".join(x.render() for x in existing)))
    return {"status": "success", "section": section, "created": True,
            "entry": entry.as_dict()}


def _bump(state: Any, section: str, entries: list[ManualEntry],
          hit: ManualEntry, doc: str) -> dict:
    hit.seen += 1
    hit.last_seen = _now()[:10]
    note = f"同一问题第 {hit.seen} 次出现（近似匹配已聚合，未新增条目）。"
    if hit.seen >= DEFECT_RECURRENCE and not hit.defect:
        hit.defect = True
    if hit.defect:
        note = (f"⚠️ 第 {hit.seen} 次记录同一问题，已标记为系统性 defect。"
                f"继续记录不产生任何价值 —— 请推动修根因（修 QC / 工具 / "
                f"上游节点），或经 orchestrator 上报用户。")
    _write_document(state, _render(
        doc, section, "\n".join(x.render() for x in entries)))
    return {"status": "success", "section": section, "created": False,
            "merged_into": hit.text[:80], "seen": hit.seen,
            "defect": hit.defect, "note": note}


def replace_manual(state: Any, section: str, entries: list[ManualEntry]) -> None:
    """维护作业专用：整节按给定条目重写（合并 / 退休的落地）。"""
    if section not in MANUAL_SECTIONS:
        raise MemoryError_(f"section 必须 ∈ {list(MANUAL_SECTIONS)}")
    with _locked(state):
        _write_document(state, _render(
            read_document(state), section,
            "\n".join(e.render() for e in entries)))


# ── 骨架 ────────────────────────────────────────────────────────────────────


def ensure_skeleton(state: Any) -> bool:
    """新项目落一份空骨架。返回是否真的写了。"""
    p = memory_path(state)
    if p is None or p.is_file():
        return False
    doc = "# 项目记忆\n\n"
    for name in SECTIONS:
        doc += (f"## {SECTION_TITLE[name]}\n\n"
                f"{_OPEN.format(name=name)}\n\n{_CLOSE.format(name=name)}\n\n")
    _write_document(state, doc)
    return True
