"""App Server 与 harness 进程之间**按契约共享**的模块入口。

两边是不同的解释器、不同的 venv，默认互相 import 不到。凡是"两个进程必须对
同一份格式达成一致"的东西（比如 worker 的活动自报格式），只有两种做法：

  1. 各写一份 —— 那就是两个真相源，格式一改必然分叉，且分叉时两边都不报错
     （写的人写新格式，读的人按旧格式解析，读到空就当没有）。
  2. **共享同一个模块** —— 本文件做的事。

选 2。代价是 App Server 要把 harness checkout 放进 sys.path；收益是格式永远
只有一处定义。`harness_root` 本来就是 App Server 的既有配置（它靠这个 spawn
子进程），并且在 `harness_sessions._paths` 里已经校验过是一个合法 checkout。
"""

from __future__ import annotations

import importlib
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from app.config import settings


class HarnessContractUnavailable(RuntimeError):
    """harness checkout 不可用 —— 契约模块拿不到，不许猜。"""


@lru_cache(maxsize=1)
def _harness_root() -> Path:
    raw = (settings.harness_root or "").strip()
    if not raw:
        raise HarnessContractUnavailable("HARNESS_ROOT is not configured")
    root = Path(raw).expanduser().resolve()
    # 与 harness_sessions._paths 同一条判据：认这个文件才算合法 checkout。
    if not (root / "core" / "agent_loop.py").is_file():
        raise HarnessContractUnavailable("HARNESS_ROOT is not a valid harness checkout")
    return root


def _contract_module(dotted_suffix: str, why: str) -> ModuleType:
    """从**当前 harness 根**加载一个契约模块 —— 不靠 sys.path 顺序去猜。

    `sys.modules` 是进程全局的。只要有别处先 import 过一个同名 `core`
    （另一个 checkout、或者测试里造出来的假 harness 根），后面每一次
    `from core import ...` 拿到的都是那一个 —— 而失败的表现是 ImportError →
    契约"不可用" → **静默回退 stdio**。那正是这个模块存在的理由：地址规则
    分叉时两边都不报错。

    实测（2026-08-19，xdist 把测试打散到同一个 worker 之后必现）：一条造假
    harness 根的测试先跑，`sys.modules['core']` 从此指向假根，同进程里后面
    每一条真 socket 测试拿到的 socket 地址都是 None。串行时只是顺序碰巧对。

    所以这里做两件 `from core import ...` 不做的事：
      1. 发现在册的 `core` 不属于这个 root，就把它连同子模块**逐出**再重装；
      2. 装完**核对**模块文件确实在 root 底下，不对就 fail loud —— 拿到一个
         来路不明的同名模块，比拿不到更危险。
    """
    root = _harness_root()
    _evict_core_from_elsewhere(root)
    # 只判"在不在 sys.path"不够：假根可能排在它前面，那假根照样赢。
    while str(root) in sys.path:
        sys.path.remove(str(root))
    sys.path.insert(0, str(root))
    name = f"core.{dotted_suffix}"
    try:
        module = importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - 环境损坏才会走到
        raise HarnessContractUnavailable(
            f"{name} is not importable from {root}（{why}）"
        ) from exc
    where = getattr(module, "__file__", None)
    if not where or root not in Path(where).resolve().parents:
        raise HarnessContractUnavailable(
            f"{name} 解析到了 {where}，不在 {root} 底下 —— 契约模块必须来自"
            f"配置的 harness 根，否则两个进程各按各的格式来。"
        )
    return module


def _evict_core_from_elsewhere(root: Path) -> None:
    """在册的 `core` 不属于这个 root 就逐出，让下一次 import 重新解析。

    生产环境只有一个 harness 根，这个分支永远不触发。它是给"同一个进程见过
    多个 root"的场合准备的 —— 测试进程就是。
    """
    existing = sys.modules.get("core")
    if existing is None:
        return
    where = getattr(existing, "__file__", None)
    if where is None:
        paths = list(getattr(existing, "__path__", []) or [])
        where = paths[0] if paths else None
    if where and root in Path(where).resolve().parents:
        return
    for name in [n for n in sys.modules if n == "core" or n.startswith("core.")]:
        del sys.modules[name]


def worker_addressing() -> ModuleType:
    """worker 的地址规则（App Server 生成、harness 进程绑定）。

    与收件箱同一个理由：地址规则抄一份就会分叉，而分叉时两边都不报错
    （一边往 A 绑、一边往 B 连，连不上就当"worker 没起来"）。
    """
    return _contract_module("worker_addressing", "worker 的 socket 地址规则")


def worker_activity() -> ModuleType:
    """worker 的活动自报格式（harness 进程写、App Server 读）。

    与地址规则同一个理由，只是这次分叉的代价更贵：读侧解析不出来 = "这个
    worker 没自报过活动" = 接不回来 = 一次正在跑的研究被当成尸体回收。
    """
    return _contract_module("worker_activity", "worker 的活动自报格式")


def sandbox_module() -> ModuleType:
    """RunAttempt capability/runtime contract shared with the harness checkout."""
    return _contract_module("sandbox", "RunAttempt 沙盒能力与容器协议")


def isolation_module() -> ModuleType:
    """执行器分档：哪个后端守 attempt、守到了哪几条不变量（core.isolation）。"""
    return _contract_module("isolation", "执行器后端选择与不变量记账")


def event_blobs() -> ModuleType:
    """事件日志里大 payload 的外置/提货规则（P0-7，worker 写、App Server 读）。

    接回在飞那一轮（#785）要从 events.jsonl 取终止 result，里面的大字段是
    引用；提货单的形状只有 `core.event_blobs` 一份定义，不在这边抄一份。
    """
    return _contract_module("event_blobs", "事件日志大 payload 的外置引用格式")


def domain_registry_module() -> ModuleType:
    """域词表（harness 定义，App Server 只读着用）。

    与前两个同一个理由，只是这次分叉的症状特别安静：词表抄一份到平台，
    harness 那边改了分类之后，两边都照常工作 —— 只是用户在选择界面里看到
    的是一份旧词表，而他选出来的域再也匹配不上新沉淀的知识。
    """
    return _contract_module("domain_registry", "科研资讯流与晋升共用的域词表")


def materials_module() -> ModuleType:
    """用户交来的文件：落点、字节池、指针（harness 定义，App Server 调它）。

    与其它契约同一个理由，而这一个分叉起来特别安静：平台按 A 规则把字节写进
    池、harness 侧按 B 规则去找，两边都不报错 —— 用户看到的就是"传上去了，
    agent 说找不到"，也就是 2026-09-04 那个形状本身。

    ⚠️ 这里**不能**写成模块顶部的 `from core import materials`。App Server 的
    进程里 `core` 不在 import path 上（部署时 cwd 是 platform/backend），而
    pytest 会把仓库根加进去 —— 于是顶层 import 在测试里全绿、在真部署上
    直接起不来。2026-09-04 一次真起进程照出来的。
    """
    return _contract_module("materials", "用户交来文件的落点、字节池与指针")


def catalog_module() -> ModuleType:
    """「这个项目产出了什么」—— harness 现算，App Server 只是端出去。

    这一层存在的全部意义就是**不要有第二个答案**：交付物判定（冻结 + 策略表的
    retention）、伴随文件怎么找（扫 metadata 的值，三种路径形状）、哪些是框架
    内务 —— 每一条在 harness 侧都已经有唯一出处。后端把这些规则抄一份过来，
    就会长出第七个各说各话的入口，而且分叉不报错：用户只会发现"这个页面里
    有、那个页面里没有"。

    ⚠️ 与 `materials_module` 同一条注意事项：**不能**写成模块顶部的
    `from core import catalog`。App Server 的 cwd 是 platform/backend，`core`
    不在 import path 上；pytest 会把仓库根加进去 —— 顶层 import 在测试里全绿、
    真部署上直接起不来。
    """
    return _contract_module("catalog", "这个项目产出了什么（交付物 / 产出 / 工作过程）")



def jobs_module() -> ModuleType:
    """受管作业登记表（core/jobs）：谁在跑、终态是什么、调度器作业号多少。

    观测面走 `jobs.observe()` 而不是 `jobs.load()` —— 两者只差一件事：
    **读不出来时抛，而不是返回空清单**（#941）。把读失败降级成空，
    看起来和「这个项目一个作业都没跑过」一模一样，而调用方会据此判 PASS。

    ⚠️ 同 `catalog_module`：不能写成模块顶部的 `from core import jobs`。
    """
    return _contract_module("jobs", "受管作业的身份与终态（谁在跑 / 怎么结束的）")


def ledger_module() -> ModuleType:
    """研究记录账本（core/ledger）：正文是原生文件，事实在账本。

    后端读记录（发布链找 writing 的验证报告与 referee 批准、研究状态页读
    research_state 的 metadata）走这一个入口，不再各自 glob 文件：账本怎么折
    （head 是哪版、冻没冻、metadata 里冻结补丁怎么合）只该有一份实现。
    """
    return _contract_module("ledger", "研究记录账本：正文原生文件 + .research/ledger")


def task_contract_module() -> ModuleType:
    """任务合同账本（core/task_contract）：只追加，按 digest 精确取（#1080 / #1097）。

    观测面要回答"这一趟绑的是哪份预注册"，而那个答案的权威在这本账上，**不在**
    "此刻项目里有几份 prereg"。和其它几个入口同理：后端不在自己这边重拼一次
    `<worktree>/tasks/…` 的路径与折账规则 —— 抄一份就会分叉，而分叉时两边都不报错。
    """
    return _contract_module("task_contract", "任务合同账本：只追加、按 digest 精确取")


def dreaming_scheduler() -> ModuleType:
    """dreaming pending 的读写（harness 定义存储位置，App Server 只是调它）。

    不在 App Server 里另拼一次 `<project_root>/dreaming_pending.json` 的路径 ——
    路径抄一份就会分叉，而分叉时两边都不报错（一边写新位置，一边读旧位置，
    读到空就当"没有 pending"）。
    """
    return _contract_module("dreaming_scheduler", "dreaming pending 的存储位置")


def record_migration_module() -> ModuleType:
    """信封记录 → 原生文件的一次性迁移器（core/record_migration.py）。"""
    return _contract_module("record_migration", "native research record migration")
