"""实验数据溯源 —— 机械回答"这份产物依赖的数据是本次跑出来的吗"。

## 为什么有这个模块

E2E-3（2026-07-28）产出了一篇 13 页论文，12 项 QC 全绿、62 项 preflight 全过。
审稿时发现：**它一个 episode 都没跑。**

    轨迹文件 created  2026-07-26 07:04:55 / 07:06:41 (+0800)
    e2e3 项目起始     2026-07-27 14:56    (+0800)

那两个文件是**上一轮 E2E（另一个项目）**跑 τ-bench 时留下的日志。τ-bench 装在
`<平台>/workspace/tau-bench/`（共享安装，本身是对的 —— 平台工具不该每个项目装
一份），但它按默认行为把日志写进**安装目录**下的 `logs/experiment/`。于是那个
目录成了跨项目共享的落盘池。E2E-3 的 experiment 节点 `ls` 一下，看到两个现成的
trajectory，就直接拿来分析了。

论文 Methods 写的是 "The agent trajectories were collected using deepseek-v4-pro"
—— 读起来像本研究采集的。样本量（tasks 0–9）实际是被那个旧日志的文件名
`..._range_0-10_...` 决定的，不是被预注册（`dataset: τ-bench (all tasks)`）或功效
分析决定的。

**注意它甚至不需要"越界"**：从节点视角，那就是"平台上现成的 τ-bench 日志"，
没有任何标记说明那是别的项目的、32 小时前的。

## 为什么是留痕 + 判据，不是围墙

读侧硬拒会打断大量合法用法（读平台数据集、读共享软件、读参考实现），而且节点有
python，围墙绕得过去。可靠的是：

  **产物依赖的文件，mtime 早于 run 起始 → 必须显式声明来源，否则 fail。**

这条判据不关心节点怎么读到的，只关心"你用了比自己还老的数据却没说"。绕不过去，
因为它检查的是结果不是手段。

## 覆盖范围（诚实说明）

路径是从**工具调用参数**里抽的（shell 命令文本、python 代码、read_file 的 path）。
覆盖字面绝对路径 —— E2E-3 这次正是字面路径，会被抓到。抓不到的：运行时拼接出来
的路径、子进程再派生的读取。要全覆盖得上 strace 级别的追踪，代价不成比例。
所以这是**下界**不是上界：报出来的一定是真外部依赖，没报的不保证没有。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

# 绝对路径字面量。覆盖 shell / python 源码 / JSON 参数里的路径。POSIX `/a/b/c`，也认
# Windows 盘符路径 `C:\a\b` / `C:/a/b`（真机实测：Windows 上模型命令里全是盘符路径，
# 旧的只认 `/…` 的正则一个都不匹配 → 溯源在 Windows 上全瞎、外部数据复用一律漏报）。
_ABS_PATH_RE = re.compile(
    r"(?<![\w.])("
    r"[A-Za-z]:[\\/](?:[\w.@+-]+[\\/])*[\w.@+-]+"    # Windows 盘符路径
    r"|/(?:[\w.@+-]+/)*[\w.@+-]+"                      # POSIX 路径
    r")"
)


_WINDOWS = os.name == "nt"


def _norm(path: str) -> str:
    """路径比较用的规范形：Windows 上大小写不敏感 + 分隔符归一到 ``/``（模型可能写
    ``C:\\a`` 也可能写 ``C:/a``，own_run/系统前缀却是另一种写法，不归一就比不上）。
    POSIX 上原样返回 —— 逐字无副作用。（用模块级 ``_WINDOWS`` 而不是现读 ``os.name``：
    测试要模拟 Windows 时改这个标志即可，不会连累 ``pathlib.Path`` 去建 WindowsPath。）"""
    if _WINDOWS:
        return path.replace("\\", "/").lower()
    return path


# 读这些不算"数据复用"：代码、系统、解释器、包。读参考实现和读别人的实验结果
# 是两回事，别把前者也报出来淹没信号。
_CODE_AND_SYSTEM_PREFIXES = (
    "/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/opt/homebrew",
    "/proc", "/sys", "/dev", "/var/run", "/private/var/run",
    "/Applications", "/System", "/Library",
    # Windows 系统 / 程序目录（比较走 _norm，大小写与分隔符已归一）
    r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData",
)
_CODE_DIR_MARKERS = ("site-packages", "dist-packages", "node_modules",
                     "/.venv/", "/venv/", "/.git/", "__pycache__")

# 这些后缀是代码/配置，不是实验数据
_CODE_SUFFIXES = {".py", ".pyc", ".pyi", ".sh", ".bash", ".zsh", ".c", ".h",
                  ".cpp", ".hpp", ".rs", ".go", ".java", ".js", ".ts",
                  ".toml", ".cfg", ".ini", ".lock", ".md", ".rst", ".txt",
                  ".yaml", ".yml"}

_MAX_RECORDED = 200


def _framework_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _is_code_or_system(path: str) -> bool:
    n = _norm(path)
    if n.startswith(tuple(_norm(p) for p in _CODE_AND_SYSTEM_PREFIXES)):
        return True
    if any(m in n for m in _CODE_DIR_MARKERS):
        return True
    if n.startswith(_norm(_framework_root())):
        return True          # 框架自己的代码
    return Path(path).suffix.lower() in _CODE_SUFFIXES


def _own_territory(state: Any) -> tuple[str, str | None]:
    """(run 目录, 项目根)。这两棵树下的东西是本 run / 本项目自己的领地。"""
    root = getattr(state, "root", None)
    proj = getattr(state, "project_root", None)
    return (str(root) if root else ""), (str(proj) if proj else None)


def run_started_at(state: Any) -> float:
    """本 run 的起始时刻（epoch 秒）。executor 在 run 开始时写进 hook_state。"""
    try:
        v = state.hook_state.get("_run_started_at")
        return float(v) if v else 0.0
    except Exception:
        return 0.0


def mark_run_start(state: Any, at: float | None = None) -> None:
    try:
        state.hook_state["_run_started_at"] = float(at if at is not None else time.time())
    except Exception:
        pass


def _candidate_paths(payload: Any, _depth: int = 0) -> set[str]:
    """从任意工具参数里抽绝对路径字面量。"""
    if _depth > 6:
        return set()
    out: set[str] = set()
    if isinstance(payload, str):
        out.update(_ABS_PATH_RE.findall(payload))
    elif isinstance(payload, dict):
        for v in payload.values():
            out |= _candidate_paths(v, _depth + 1)
    elif isinstance(payload, list | tuple):
        for v in payload:
            out |= _candidate_paths(v, _depth + 1)
    return out


def record_tool_paths(state: Any, tool_name: str, kwargs: dict) -> None:
    """工具调用后登记它碰过的外部数据文件。在 tool_registry.execute 里统一调。

    只记：**存在的普通文件** + 不在本 run / 本项目领地内 + 不是代码/系统文件。
    """
    try:
        started = run_started_at(state)
        own_run, own_proj = _own_territory(state)
        seen: dict = state.hook_state.setdefault("_external_reads", {})
        if len(seen) >= _MAX_RECORDED:
            return
        for raw in _candidate_paths(kwargs):
            if raw in seen:
                continue
            nraw = _norm(raw)
            if own_run and nraw.startswith(_norm(own_run)):
                continue
            if own_proj and nraw.startswith(_norm(own_proj)):
                continue
            if _is_code_or_system(raw):
                continue
            try:
                st = os.stat(raw)
            except OSError:
                continue
            if not os.path.isfile(raw):
                continue
            seen[raw] = {
                "path": raw,
                "mtime": st.st_mtime,
                "size": st.st_size,
                "first_touched_by": tool_name,
                # 关键判据：比本 run 还老 = 不是本次跑出来的
                "predates_run": bool(started and st.st_mtime < started),
            }
            if len(seen) >= _MAX_RECORDED:
                break
    except Exception:
        pass          # 溯源是 advisory 采集，绝不能弄挂工具调用


def external_reads(state: Any) -> list[dict]:
    try:
        return sorted(state.hook_state.get("_external_reads", {}).values(),
                      key=lambda r: r["path"])
    except Exception:
        return []


def _same_project_run(state: Any, path: str) -> str | None:
    """path 若在**本项目某个 run 的目录树**内，返回那个 run_id；否则 None。

    E2E-5b 实测（16 次 writing 重试的死因之一）：被拦的
    `…/agent-step-routing-e2e5b/1785604768-d342eb/sci_manuscript/…/fig_cost_success.tex`
    是**本项目更早的 writing run 自己产的**。框架完全知道它是哪个 run 产的
    （路径就在本项目 runs 目录下），却把它当"外部数据"要求 agent 手工声明，
    忘了声明整个 run 报废 —— 框架能机械回答的问题让 agent 承担，还设了死刑。

    同项目前序 run 的产物是**科研迭代的正常延续**，provenance 框架自己就能出具。
    真正要人声明的是：别的项目的东西、平台上现成的数据集、任何本项目历史之外
    的文件 —— 那些框架无从知道来历。
    """
    try:
        base = state.root.parent.resolve()
        p = Path(path).resolve()
        rel = p.relative_to(base)
    except (ValueError, OSError):
        return None
    run_id = rel.parts[0] if rel.parts else None
    if not run_id or run_id == state.root.name:
        return None          # 本 run 自己的文件根本不算 external
    return run_id


def stale_external_inputs(state: Any) -> list[dict]:
    """比本 run 还老、且**框架无法自证来历**的外部数据文件 —— 需要显式声明的那些。

    同项目前序 run 目录树内的文件不在此列：它们的 provenance 框架机械可知，
    由 auto_provenance() 自动出具，不劳 agent 手工声明。
    """
    out = []
    for r in external_reads(state):
        if not r.get("predates_run"):
            continue
        if _same_project_run(state, r["path"]):
            continue
        out.append(r)
    return out


def auto_provenance(state: Any) -> list[dict]:
    """框架替 agent 出具的同项目复用记录 —— 机械可知的不需要声明，但要留痕。

    审计时"这个图/数据是哪来的"仍然答得上：来自本项目 run X。
    """
    out = []
    for r in external_reads(state):
        if not r.get("predates_run"):
            continue
        rid = _same_project_run(state, r["path"])
        if rid:
            out.append({"path": r["path"], "source_run_id": rid,
                        "source": "same_project_prior_run"})
    return out


# ── 声明侧 ──────────────────────────────────────────────────────────────────
# 节点声明"我确实复用了这些，来源是 X，理由是 Y"的方式：在产物 metadata 里写
# `reused_inputs`。判据只要求**声明存在且指向被用到的文件**，不评判理由好坏 ——
# 那是 reviewer 的活。

_DECL_KEYS = ("reused_inputs", "data_provenance", "reused_data")


def declared_reuse_paths(artifact: dict) -> set[str]:
    """从一个 artifact 里抽出它声明复用了哪些路径。"""
    md = (artifact or {}).get("metadata") or {}
    out: set[str] = set()
    for key in _DECL_KEYS:
        v = md.get(key)
        if isinstance(v, str):
            out |= _candidate_paths(v)
        elif isinstance(v, list | tuple):
            for item in v:
                if isinstance(item, str):
                    out |= _candidate_paths(item)
                elif isinstance(item, dict):
                    out |= _candidate_paths(item)
        elif isinstance(v, dict):
            out |= _candidate_paths(v)
    return out


def load_full_artifacts(state: Any) -> list[dict]:
    """本 run 的产物**完整记录**（含 metadata）。

    `state.list_artifacts()` 只给 `{id, type, name}` —— 声明写在 metadata 里，
    拿列表版去查永远查不到（第一版就踩了，测试直接照出来）。
    """
    out: list[dict] = []
    try:
        for a in state.list_artifacts() or []:
            rec = None
            try:
                rec = state.read_artifact(a.get("id"))
            except Exception:
                rec = None
            out.append(rec if isinstance(rec, dict) else a)
    except Exception:
        pass
    return out


def undeclared_stale_inputs(state: Any, artifacts: list[dict] | None = None) -> list[dict]:
    """本 run 用了、但没有任何产物声明来源的"比 run 还老的外部数据"。

    这就是 E2E-3 那篇论文的情形：13 episodes 全部来自 32 小时前另一个项目跑
    τ-bench 留下的日志，论文里一个字没提。
    """
    stale = stale_external_inputs(state)
    if not stale:
        return []
    if artifacts is None:
        artifacts = load_full_artifacts(state)
    declared: set[str] = set()
    for a in artifacts or []:
        declared |= declared_reuse_paths(a)
    return [r for r in stale if r["path"] not in declared]


def format_violation(rows: list[dict], *, started_at: float = 0.0) -> str:
    """给节点看的错误文案 —— 说清是什么、为什么拦、怎么合规。"""
    import datetime as _dt

    def _ts(v: float) -> str:
        try:
            return _dt.datetime.fromtimestamp(v).isoformat(timespec="seconds")
        except Exception:
            return str(v)

    lines = [
        f"⛔ 本 run 的产物依赖了 {len(rows)} 个**比本 run 还老的外部数据文件**，"
        "但没有任何产物声明它们的来源。",
    ]
    if started_at:
        lines.append(f"（本 run 起始：{_ts(started_at)}）")
    for r in rows[:10]:
        lines.append(f"  - `{r['path']}`（mtime {_ts(r['mtime'])}，{r['size']} bytes）")
    if len(rows) > 10:
        lines.append(f"  …… 另有 {len(rows) - 10} 个")
    lines.append(
        "这些文件不是本次跑出来的。要么是别的项目/更早的 run 留下的，要么是平台上"
        "现成的数据。**沉默复用等于把别人的结果写成自己的**。"
    )
    lines.append(
        "合规做法二选一：\n"
        "  ① 真的要复用 → 在产物 metadata 里写 `reused_inputs`："
        "`[{\"path\": \"...\", \"source\": \"哪个 run / 项目 / 数据集\", "
        "\"reason\": \"为什么复用而不是重跑\"}]`，并在正文里如实交代；\n"
        "  ② 本该自己跑 → 就去跑，把输出写进本项目的 workspace，别用现成的。"
    )
    return "\n".join(lines)
