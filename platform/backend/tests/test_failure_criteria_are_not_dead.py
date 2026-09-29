"""故障判据不能是死的 —— 扫盘，不写名单。

## 现场（wangd 2026-08-21）

用户跑轮中问「怎么样了？」，读到的是：

    这一轮没能完成. 平台在记录这次运行时撞上了内部错误。

真实原因是 `The project Harness session is busy`（一个 unattended worker 停靠
在 4 小时的复查间隔上，攥着会话的操作锁）。而 `run_failures._COPY` 里**早就
写着**一条完全正确的文案（`session_busy`：「上一轮还占着这个会话的工作区」）
—— 它一次都没到过用户面前。

两条判据同时是死的：

1. `_BY_TYPE` 里写着 `("ProjectBusyError", "session_busy")`，而
   `ProjectBusyError` 只存在于 **worker 进程**（`platform_runtime`）。跨进程
   到达 App Server 时它是一个 `HarnessSessionError`，MRO 里永远没有那个名字。
   判据看的是 `type(exc).__mro__`，**证据只看了一半** —— 真身份在
   `error_type` 字段里躺着，没人读。
2. worker 出海时声明的 code 是 `project_busy`，而文案表的键叫 `session_busy`。
   同一件事两个名字，两边都不报错。

已有的测试 `test_it_classifies_by_type_not_by_wording` 是绿的 —— 因为它自己
`class ProjectBusyError(Exception)` 造了一个**进程内**的类。测试没写错，它只是
没问真实的那个问题（跨进程的身份怎么到达）。**自己造被测对象，就只能验证
自己想到的那条路**。

## 这里的判据

三条，全是扫盘（遍历数据结构自身），零豁免名单：

- A. `_BY_TYPE` 点名的类型必须真实存在（进程内可导入，或仓库里有 `class X`）；
- B. `_BY_TYPE` 的每一条都必须**真的能匹配上** —— 逐条构造它在生产里会有的
     形状，断言 `describe()` 落到它声明的那个 code。这条是有牙的：死判据在这里
     必红。
- C. `_COPY` 的每个键都必须**产得出来**（有人声明这个 code / 或是 `_BY_TYPE`
     的目标 / 或是兜底键）。防的是反向的同一个病：写了文案而没人叫这个名字。

⚠️ 如果哪天这三条需要豁免名单，说明判据选错了 —— 名单化正是它们要防的东西
（「护栏要扫盘，不要写名单」）。回来重想判据，别喂名单。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.services.run_failures import _BY_TYPE, _COPY, _GENERIC, describe

#: 仓库根（platform/backend/tests → 上溯三级）。
_REPO_ROOT = Path(__file__).resolve().parents[3]


#: 走不进去的目录 —— 判据是**结构性**的，不是业务名单。
#:
#: 点名的三样都不可能装仓库源码：点开头（`.git` / `.venv` / 工具目录）、
#: 字节码缓存、npm 依赖。虚拟环境另外靠 `pyvenv.cfg` 认（它可以叫任何名字）。
#: 业务目录一个都不点名 —— 那种名单必然漏掉新增的那个。
_SKIP_DIRS = {"__pycache__", "node_modules"}


def _is_an_installed_interpreter(directory: Path) -> bool:
    """这个目录是不是**一份装好的 Python**（而不是我们的源码）。

    两种形状，判据都是结构性的，不是名字：

    - 虚拟环境：有 `pyvenv.cfg`。它可以叫任何名字，所以不能按名字认。
    - 完整的解释器安装：同时有 `bin/python*` 和 `lib/python*`。Mac 安装包里
      那份随包分发的 CPython 就是这一种 —— 它没有 `pyvenv.cfg`，2026-09-06 打
      包之后，`dist/…app/Contents/Resources/python/` 里的 6000 多个标准库文件
      一下子全被当成了"仓库源码"。

    按名字把 `dist` 加进跳过名单也能让这条测试变绿，但那正是这个文件顶上警告
    过的做法：下一个装好的解释器出现在别的目录里时，名单不会知道。
    """
    if (directory / "pyvenv.cfg").exists():
        return True
    return any((directory / "bin").glob("python*")) and any((directory / "lib").glob("python*"))


def _repository_sources() -> dict[str, str]:
    """仓库里的 .py 源码（排除测试）。

    ## 为什么不问 git（2026-08-23 改）

    这里原来是 `git ls-files "*.py"` + `check=True`，理由写得很好：跟踪状态
    不会骗人。可它把一道**产品代码的扫盘闸**挂到了"git 此刻能不能在这个
    工作区里跑"上面 —— 而那是运行环境的事。

    实测代价：CI 的 workspace 与 job 进程的 uid 对不上时，git 抛
    `fatal: detected dubious ownership`、退出码 128，`check=True` 把它变成
    CalledProcessError，这两条闸当场红 —— 而被测的东西一个字没坏。
    「判据不许依赖运行环境」。

    换成走文件树。**不是**退而求其次：在一个干净的 checkout 上，这个走法与
    `git ls-files` 给出的是**逐字节相同**的 1040 个文件（下面那条测试每次
    都在核对）。所以不是"两个真相源"，是同一个答案的两种算法，而其中一种
    不需要外部进程配合。
    """
    from tests.repository_sources import source_files

    sources: dict[str, str] = {}
    for relative, text in source_files("*.py"):
        rel = relative.as_posix()
        if relative.name.startswith("test_") or "/tests/" in f"/{rel}":
            continue
        sources[rel] = text
    return sources


def test_the_scan_actually_sees_the_sources() -> None:
    """闸自己要能看见东西 —— 扫到空集时下面两条会空转通过。"""
    sources = _repository_sources()
    assert len(sources) > 500, f"只扫到 {len(sources)} 个源文件，判据多半失效了"
    assert any(rel.endswith("app/services/run_failures.py") for rel in sources)


def test_every_named_exception_type_actually_exists() -> None:
    """A. 点名的类型必须真实存在 —— 幽灵名字等于这道判据不存在。"""
    import builtins

    sources = _repository_sources()
    ghosts = []
    for type_name, _code in _BY_TYPE:
        if hasattr(builtins, type_name):
            continue  # TimeoutError 之类的内建
        defined_here = any(
            re.search(rf"^\s*class\s+{re.escape(type_name)}\b", text, re.M)
            for text in sources.values()
        )
        if defined_here:
            continue
        # 第三方（sqlalchemy / asyncpg / json）—— 真能 import 出来就算数。
        if _importable_third_party(type_name):
            continue
        ghosts.append(type_name)
    assert not ghosts, (
        f"_BY_TYPE 点名了不存在的类型 {ghosts} —— 这条判据永远匹配不上，"
        "等于那类故障没有文案。改名字或删条目，别留着。"
    )


def _importable_third_party(type_name: str) -> bool:
    for module_path in (
        "sqlalchemy.exc", "asyncpg.exceptions", "json", "json.decoder",
    ):
        try:
            module = __import__(module_path, fromlist=["_"])
        except ImportError:
            continue
        if hasattr(module, type_name):
            return True
    return False


@pytest.mark.parametrize("type_name,expected_code", _BY_TYPE)
def test_every_type_criterion_can_actually_match(type_name: str, expected_code: str) -> None:
    """B. 逐条证明判据是活的 —— 两条到达路径各验一遍。

    进程内：异常类自己就叫这个名字（sqlalchemy 的 IntegrityError 那类）。
    跨进程：worker 的异常到达时是 `HarnessSessionError`，真身份在 `error_type`。

    死判据在这里必红：`ProjectBusyError` 在 2026-08-21 之前跨进程那条是红的。
    """
    in_process = type(type_name, (Exception,), {})("boom")
    assert describe(in_process).code == expected_code, (
        f"进程内的 {type_name} 没被认出来"
    )

    from app.services.harness_sessions import HarnessSessionError

    over_the_wire = HarnessSessionError("boom", error_type=type_name)
    assert describe(over_the_wire).code == expected_code, (
        f"跨进程的 {type_name}（error_type 字段）没被认出来 —— "
        "判据只看了 MRO，而 worker 的类型名永远不在 MRO 里"
    )


def test_every_copy_entry_is_reachable() -> None:
    """C. 反向：写了文案，就得有人叫得出这个名字。

    产出路径有四条，都算数：
      · 有代码声明这个 code（`code="…"` / `RequestError("…"` / `known_cause`）；
      · 它是某条 `_BY_TYPE` 的目标（按异常类型认，不经 code）；
      · 它是某条 `_BY_UPSTREAM_STATUS` 的目标（按我们自己写的 HTTP 码认）；
      · 它是 `_exit_code_answer` 的目标（按进程退出码认 —— 2026-09-16 加，
        「执行进程退出了」按退出码拆成自己走的 / 被要求停的 / 被强杀的三条）。

    第三条 2026-08-22 之前不在这份判据里 —— `upstream_unavailable` 只是**碰巧**
    因为 harness 侧也用了同一个名字才通过。而新加的 `upstream_rejected` 没有
    外部声明方，于是护栏把一条活着的路径判成了死文案。补的不是豁免：下面
    真的 `describe()` 一次，证明每个状态码都到得了它自己那条文案。
    """
    from app.services.run_failures import (
        _BY_UPSTREAM_STATUS,
        _exit_code_answer,
        describe,
    )

    # 第四条路径同样**真的走一遍**，不是加豁免：每个退出码都要到得了它自己
    # 那条文案，而且三条文案不许折回同一段话（那就等于没拆）。
    class _Exited(Exception):
        code = "harness_process_exited"

        def __init__(self, exit_code: int) -> None:
            super().__init__(f"exited (exit code {exit_code})")
            self.exit_code = exit_code

    exit_code_targets = set()
    said = {}
    for exit_code in (0, -15, -9):
        target = _exit_code_answer(_Exited(exit_code))
        assert target in _COPY, f"退出码 {exit_code} 指向了一条不存在的文案 {target}"
        exit_code_targets.add(target)
        said[exit_code] = describe(_Exited(exit_code))
    assert len({f.title for f in said.values()}) == 3, (
        "三种退出码折回了同一段文案 —— 拆了等于没拆"
    )

    for statuses, code in _BY_UPSTREAM_STATUS:
        for status in statuses:
            assert describe(RuntimeError(f"LLM API HTTP {status}: x")).code == code, (
                f"HTTP {status} 到不了 {code} —— 这条内容判据是死的"
            )

    sources = _repository_sources()
    by_type_targets = {code for _name, code in _BY_TYPE}
    by_status_targets = {code for _statuses, code in _BY_UPSTREAM_STATUS}
    unreachable = []
    for key in _COPY:
        if (
            key == _GENERIC
            or key in by_type_targets
            or key in by_status_targets
            or key in exit_code_targets
        ):
            continue
        declared_somewhere = any(
            rel != "platform/backend/app/services/run_failures.py"
            and re.search(rf"""["']{re.escape(key)}["']""", text)
            for rel, text in sources.items()
        )
        if not declared_somewhere:
            unreachable.append(key)
    assert not unreachable, (
        f"这些文案没有任何产出路径 {unreachable} —— 要么名字和产出方对不上"
        "（`project_busy` vs `session_busy` 就是这么漏的），要么它已经死了。"
    )


def test_a_worker_side_busy_reaches_its_own_copy_not_the_generic_one() -> None:
    """现场回放：8-21 那条错误，每一种形状都不许再落到兜底文案。

    ## 形状一（App Server 自己判的"会话被占用"）2026-08-23 删除了

    那三处 raise 随 RFC D10 删除清单一起走了 —— "会话被占着"不再是拒收理由，
    占用是**排队**的理由。所以这里不再造那个形状：造一个产品代码里永远不会
    出现的异常来测它的文案，测的是测试自己。

    剩下的两种形状都来自 worker 抢不到工作区 flock（**所有权**那一维，
    D10 三拆里唯一没变的那个）—— 它们仍然存在，也仍然不许掉进兜底。
    """
    from app.services.harness_sessions import HarnessSessionError

    # 形状二：worker 出海的 ProjectBusyError（code=project_busy + error_type）。
    from_worker = HarnessSessionError(
        "another chat process owns this project session",
        code="project_busy",
        error_type="ProjectBusyError",
    )
    # 形状三：**只有 code**，没有 error_type。
    #
    # 变异验证逼出来的（2026-08-21）：删掉 `project_busy` 别名，上面两条依然全绿
    # —— 因为形状二被 `error_type` 那条路救了。两条到达路径互相遮挡，就等于其中
    # 一条从此没有测试。worker 今天两处 emit 都带 error_type，但那是**它的**实现
    # 细节，不是我们能依赖的契约（换个 emit 点就没了）。各钉各的。
    code_only = HarnessSessionError(
        "another chat process owns this project session", code="project_busy"
    )

    for exc in (from_worker, code_only):
        failure = describe(exc, reference="run-1")
        assert failure.code != _GENERIC, "又掉回兜底文案了"
        assert "内部错误" not in failure.body, (
            "会话被占用不是内部错误 —— 这正是 8-21 用户读到的那句话"
        )
        assert failure.retryable is True
        assert "The project Harness session is busy" not in failure.body, (
            "异常原文只能进 detail"
        )
