"""到得了用户面前的错误必须是产品文案，不是异常的 `str()`。

## 现场（wangd 2026-08-11 试用）

会话正文里出现了这个：

    Research stopped before completion
    Execution failed before an agent response was produced:
    (sqlalchemy…IntegrityError) … duplicate key value violates unique constraint
    "uq_events_sequence" … [SQL: INSERT INTO execution_events (id, tenant_id,
    workspace_id, …) VALUES ($1::VARCHAR, …)] [parameters: ('b145e65e…', …)]

整条 INSERT、全部列名、全部参数值。

## 为什么会这样

后端 `_sanitized_platform_failure` 拼的是 `f"…: {str(exc)}"`；前端一条 if-链
（`/Harness session operation timed out/i`）认得的给人话、认不得的原样显示。

两件事叠在一起：**认领故障的知识有两份**（后端手里有异常对象却压成字符串，
前端再用正则猜回来），而且前端那份是**名单**——名单认不得的默认落到"原样显示"。

## 这些测试守的是什么

不是"输出好不好看"，是一条构造上的不变量：

    title / body / recovery 只能来自文案表；str(exc) 只能进 detail。

所以下面不去扫"有没有 SQL 关键字"（那又是一份名单，且新的泄露形式默认漏过），
而是直接断言：**正文三件套一字不差地等于文案表里的条目**。任何异常原文想进正文，
都得先把自己写进文案表 —— 那是人写的、评审得到的。
"""
from __future__ import annotations

import json

import pytest


class _Weird(Exception):
    """一个谁也没见过的异常 —— 默认行为必须是安全的。"""


def test_every_headline_comes_from_the_copy_table() -> None:
    """随便扔什么异常，正文三件套都必须是文案表里的原话。

    这一条是整个设计的锚：过了它，"新异常泄露原文"就不是"记得处理"的问题，
    而是结构上做不到的事。
    """
    from app.services.run_failures import _COPY, describe

    # 文案表现在是双语的：每一条都要两种语言各自都出自表里 —— 只查一种语言的话，
    # 另一种语言可以偷偷塞进 str(exc) 而判据不红。
    known_titles = {text for c in _COPY.values() for text in c.title.values()}
    known_bodies = {text for c in _COPY.values() for text in c.body.values()}
    known_recoveries = {text for c in _COPY.values() for text in c.recovery.values()}

    for exc in (
        _Weird("[SQL: INSERT INTO execution_events (id, tenant_id) VALUES ($1)]"),
        ValueError("token=sk-abcdef parameters: ('local-tenant', 613)"),
        RuntimeError(""),
        KeyError("uq_events_sequence"),
    ):
        for lang in ("zh", "en"):
            failure = describe(exc, reference="run-1", lang=lang)
            assert failure.title in known_titles, f"标题不是文案表里的：{failure.title!r}"
            assert failure.body in known_bodies, f"正文不是文案表里的：{failure.body!r}"
            assert failure.recovery in known_recoveries


def test_the_raw_text_only_ever_lands_in_detail() -> None:
    """异常原文进得了 detail，进不了别的任何地方。"""
    from app.services.run_failures import describe

    marker = "ZZQQXX-uniquely-identifiable-raw-text"
    record = describe(_Weird(f"boom {marker} boom"), reference="run-1").as_record()

    assert marker in str(record["detail"]), "细节丢了 —— 那用户就彻底没法排查了"
    for field in ("title", "body", "recovery", "message", "code"):
        assert marker not in str(record[field]), f"原文漏进了 {field}"


def test_an_unknown_failure_still_says_something_actionable() -> None:
    """认不出来 ≠ 没话说。用户要拿到：出了什么事、能做什么、参考号。"""
    from app.services.run_failures import describe

    failure = describe(_Weird("???"), reference="run-abc")
    assert failure.title and failure.body and failure.recovery
    assert failure.reference == "run-abc", "没有参考号，日志里就找不回真正的细节"


def test_it_classifies_by_type_not_by_wording() -> None:
    """按异常类型认，不按消息措辞认 —— 措辞随时会被上游改。"""
    from app.services.run_failures import describe

    class IntegrityError(Exception):      # 名字与 sqlalchemy 的一致
        pass

    class ProjectBusyError(Exception):
        pass

    # 消息文本故意写得毫无线索：判据不该依赖它。
    assert describe(IntegrityError("x")).code == "storage_conflict"
    assert describe(ProjectBusyError("x")).code == "project_busy"


def test_a_declared_code_wins_over_the_type() -> None:
    """运行时自己声明的 code 最权威 —— 它比我们更清楚这次发生了什么。"""
    from app.services.run_failures import describe

    exc = TimeoutError("x")
    exc.code = "request_too_large"        # type: ignore[attr-defined]
    assert describe(exc).code == "request_too_large"
    assert "too large" in describe(exc, lang="en").title.lower()
    assert "太大" in describe(exc).title


def test_an_unknown_declared_code_is_kept_but_gets_generic_copy() -> None:
    """还没写文案的 code：如实记下来，文案用通用那条。

    丢掉它等于把"运行时到底报了什么"这个事实也一起抹掉 —— 排查时最想要的
    就是这个。
    """
    from app.services.run_failures import _COPY, _GENERIC, describe

    exc = RuntimeError("x")
    exc.code = "some_new_code_nobody_wrote_copy_for"   # type: ignore[attr-defined]
    failure = describe(exc)
    assert failure.code == "some_new_code_nobody_wrote_copy_for"
    # 锚的是"落到了兜底那一条"，不是它当时的措辞 —— 措辞本来就该能改，
    # 而按原文断言会让每次改写用户可见的话都红一次。
    assert failure.body == _COPY[_GENERIC].body["zh"]


def test_no_user_facing_copy_ever_says_internal_error() -> None:
    """「内部错误」这四个字不许出现在任何一条给用户的话里（wangd 2026-08-22）。

    它从来不是一个诊断，是一句无知 —— 而且几乎每次都是错的：掉进兜底的失败
    绝大多数既不在平台内部，也不在"记录"阶段（2026-08-22 现场是上游 403 额度
    用尽）。用户读到它之后能多做对的事：零。

    **扫盘不写名单**：判据是"`_COPY` 里的每一条"，不是"我们记得的那几条"。
    写名单的护栏对新加的条目默认漏过，而这里加条目正是常态。
    """
    from app.services.run_failures import _COPY

    offenders = [
        (code, field)
        for code, copy in _COPY.items()
        for field in ("title", "body", "recovery")
        if "内部错误" in getattr(copy, field)
    ]
    assert not offenders, f"这些文案还在对用户说「内部错误」：{offenders}"


def test_transient_upstream_is_marked_retryable() -> None:
    """上游 429/5xx：本次一个字都没产出，重发是安全的，要如实标出来。

    判据取的是**我们自己生成的**格式（`core/llm.py` 抛 `LLM API HTTP <code>`），
    不是猜 provider 的措辞 —— 后者等于给每一家上游各维护一份名单。
    """
    from app.services.run_failures import describe

    assert describe(RuntimeError("LLM API HTTP 429: slow down")).retryable is True
    assert describe(RuntimeError("LLM API HTTP 503: upstream")).retryable is True
    # 400 不是"不可重试"，是"我们不知道" —— 三态里的第三态。断成 False 等于
    # 把一个我们没有的结论说给用户听。
    assert describe(RuntimeError("LLM API HTTP 400: bad request")).retryable is None


def test_secrets_never_reach_the_detail_either() -> None:
    """detail 是折叠区，不是垃圾桶 —— 脱敏照做。"""
    from app.services.run_failures import describe

    failure = describe(RuntimeError("Authorization: Bearer sk-live-0123456789abcdefghij"))
    assert "sk-live-0123456789abcdefghij" not in failure.detail


def test_a_huge_dump_is_capped() -> None:
    """8KB 的参数转储没必要整个存进 run summary。"""
    from app.services.run_failures import describe

    failure = describe(RuntimeError("x" * 50_000))
    assert len(failure.detail) < 2_500
    assert "截断" in failure.detail


@pytest.mark.asyncio
async def test_what_the_platform_stores_is_what_the_user_reads(db_session) -> None:
    """接线：走真实的 `_sanitized_platform_failure`，落库的记录就是产品文案。

    没有这一条，上面全部都只是在测一个可能没人调用的模块（今天已经在
    "写了没人调" 上栽过四次）。
    """
    from app.services.local_execution import _sanitized_platform_failure

    record = _sanitized_platform_failure(
        _Weird("[SQL: INSERT INTO execution_events …] [parameters: ('local-tenant', 613)]"),
        run_id="run_357d167b",
    )
    assert "INSERT INTO" not in str(record["message"]), "正文还在漏 SQL"
    assert "INSERT INTO" in str(record["detail"]), "细节该留的没留"
    assert record["reference"] == "run_357d167b"
    # 老字段 `message` 仍然存在（老前端和老数据都靠它），但装的是人话。
    assert json.dumps(record)   # 可序列化 —— 它要进 JSONB


def test_a_restart_is_not_the_same_as_a_failure() -> None:
    """平台重启打断 ≠ 研究失败。给用户的话完全不同。

    ## 现场（2026-08-12）

    重启 App Server，在跑的那条 run 落成：

        status  = failed
        failure = "Harness runtime process exited before replying (exit code -15)"
        → "Review the request, then continue or revise the request."

    `-15` 是 SIGTERM，**我们自己发的**。研究一点问题没有，而用户被建议去改一件
    没错的东西。

    子进程只看得见一个 `-15`（也可能来自 OOM killer）—— 区分它们的信息只有
    App Server 有，所以由调用方用 `known_cause` 说出来。
    """
    from app.services.run_failures import describe

    exc = RuntimeError("Harness runtime process exited before replying (exit code -15)")
    exc.code = "harness_process_exited"      # type: ignore[attr-defined]

    normal = describe(exc, reference="run-1")
    interrupted = describe(exc, reference="run-1", known_cause="app_server_restarted")

    assert normal.code == "harness_process_exited"
    assert interrupted.code == "app_server_restarted", "code 和文案要指向同一件事"
    assert normal.title != interrupted.title
    assert "重启" in interrupted.title
    assert "研究本身没有失败" in interrupted.body, "得明说研究本身没问题"
    assert "revise" not in interrupted.recovery.lower(), "别让用户去改一件没错的东西"


def test_an_unknown_known_cause_falls_back_instead_of_crashing() -> None:
    """调用方给了个没有文案的 cause —— 退回正常分类，不是 KeyError。

    这条路径在**关机**时走，最不该是它把关机流程打断的。
    """
    from app.services.run_failures import describe

    failure = describe(RuntimeError("x"), known_cause="no_such_copy_entry")
    assert failure.title


def test_the_platform_knows_it_is_shutting_down() -> None:
    """接线：`interrupted_by_our_own_shutdown` 只在关机时为真，且只认进程退出。"""
    from app.services import lifecycle
    from app.services.harness_sessions import HarnessSessionProcessError
    from app.services.local_execution import interrupted_by_our_own_shutdown

    process_gone = HarnessSessionProcessError("exit -15", exit_code=-15)
    other = RuntimeError("LLM API HTTP 503")

    lifecycle.reset_for_tests()
    assert interrupted_by_our_own_shutdown(process_gone) is False, "没关机就不该算"
    try:
        lifecycle.begin_shutdown()
        assert interrupted_by_our_own_shutdown(process_gone) is True
        assert interrupted_by_our_own_shutdown(other) is False, (
            "关机时别的异常照旧是失败 —— 不能借关机把真失败一起洗白"
        )
    finally:
        lifecycle.reset_for_tests()


def test_the_shutdown_flag_is_set_the_moment_the_signal_arrives() -> None:
    """旗要在**信号到达那一刻**立，不是等 lifespan 收尾段。

    ## 现场（2026-08-12，两个进程交错写同一份日志）

        00:55:26.857  新 App Server 启动扫描并回收了老 worker
        00:55:26.863  老 App Server 的在途 run 记成 failed      ← 伤害在这
        00:55:26.927  老 App Server：Shutting down…             ← 旗到这才立

    uvicorn 先停止接受连接、等在途连接排空，**最后**才跑 lifespan 收尾段。
    立在收尾段 = 立在伤害之后。
    """
    import signal

    from app.services import lifecycle

    lifecycle.reset_for_tests()
    seen: list[int] = []
    original = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: seen.append(signum))
        lifecycle.watch_for_shutdown_signal()
        assert lifecycle.is_shutting_down() is False, "装 handler 本身不该立旗"

        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert lifecycle.is_shutting_down() is True, "信号到了旗没立"
        assert seen == [signal.SIGTERM], (
            "把 uvicorn 的 handler 抢掉了 —— 服务器会不退出，"
            "修一个观测问题的代价不能是把关机本身弄坏"
        )
    finally:
        signal.signal(signal.SIGTERM, original)
        lifecycle.reset_for_tests()


def test_every_place_that_writes_a_terminal_status_goes_through_one_decision() -> None:
    """异常收尾时给 run 赋终态的地方，判据只能有一个。这条**扫盘**，不认名单。

    ## 现场（2026-08-12）

    "重启打断 → 可恢复态"这条区分，我接上了 `is_answer` 分支和 fail-loud 记账，
    实测**仍然落成 failed**。真正写下终态的是第三处：记账自身失败时的兜底路径，
    而它不知道有这回事。

    加防线之前先问"覆盖全不全"。三处各判一次 = 三份会各自演化的判据，分叉时
    谁都不报错。

    ## 判据的边界

    只扫 **`except` 块里** 给 Run 行（`run` / `dead`）赋 `status` 的地方 ——
    那才是"异常结束了这条 run"这件事。别的赋值有别的语义，不该被这条绑住：

    · `_validate_resume_binding` 里那处：pause 活过了它的 attempt，是一致性
      修复，不是异常收尾；
    · `attempt.status`：attempt 是另一条生命周期，它跟着 run 走但不是 run。

    列名单会漏掉将来新加的第四处；扫盘不会。
   
    ## `ast.unparse` 的使用边界

    这里只 unparse **单个赋值的右值 / 集合里的一个元素** —— 极浅的子树。
    不要扩大到整个函数：`ast.unparse` 是递归的，CPython 3.11 在大函数上
    会间歇性抛 `SystemError: AST constructor recursion depth mismatch`
    （2026-08-12 实测把 CI 打成时红时绿，见
    `tests/test_unattended_emits_one_terminal.py` 的重写说明）。
    """
    import ast
    import inspect

    from app.services import local_execution

    tree = ast.parse(inspect.getsource(local_execution))
    offenders: list[str] = []
    allowed = ("terminal_state_for", "terminal_status", "rescue_status")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            for inner in ast.walk(handler):
                if not isinstance(inner, ast.Assign):
                    continue
                for target in inner.targets:
                    if not (isinstance(target, ast.Attribute) and target.attr == "status"):
                        continue
                    if not (isinstance(target.value, ast.Name) and target.value.id in {"run", "dead"}):
                        continue
                    value = ast.unparse(inner.value)
                    if not any(marker in value for marker in allowed):
                        offenders.append(f"line {inner.lineno}: {ast.unparse(inner)}")

    assert not offenders, (
        "这些地方在异常收尾时自己决定了 run 的终态，绕过了 `terminal_state_for`：\n  "
        + "\n  ".join(offenders)
    )


def test_a_run_records_which_drive_it_actually_used() -> None:
    """每条 run 要记下它**实际**是无人值守还是助理模式跑的。

    ## 现场（2026-08-12）

    wangd 问「你没开 continuous？」—— 我答不上来。平台哪儿都没记：只能去翻
    `project_configs`，而那张表是**现在**的配置，不是**当时**跑的那个。配置改过
    之后，历史 run 到底是自主还是助理跑的，就再也没人知道了。

    决定行为的事实必须跟着它影响的那次执行一起落盘。判决可以现算，事实不行。

    这条扫的是**写 summary 的每一处**：漏一处，那条路径上跑的 run 就永远说不清
    自己是怎么跑的 —— 而那正是"两条路只有一条接了记账"的老形状。
    """
    import ast
    import inspect

    from app.services import local_execution

    tree = ast.parse(inspect.getsource(local_execution))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        # run.summary 的标志：它一定带 executionKernel（哪个内核跑的）。
        if "executionKernel" not in keys:
            continue
        if "drive" not in keys:
            offenders.append(f"line {node.lineno}: summary 少了 drive，键={sorted(keys)}")

    assert not offenders, (
        "这些 run summary 没记下实际驱动模式：\n  " + "\n  ".join(offenders)
    )


def test_an_unknown_declared_code_does_not_block_content_classification() -> None:
    """认不出 code 的名字 ≠ 判不出这是什么事（2026-08-17 实测）。

    GPUStack 返 503，异常带 `code="runtime_error"`。旧实现在"声明了一个没有
    文案的 code"那一支直接 return 通用文案 —— 下面认得 HTTP 状态码的判据
    一次都没跑到。用户读到"平台内部错误 / 记录这个 run 时出错"：不是内部
    错误、不在记录阶段、给的下一步也是错的（该重发，而不是来报 bug）。
    """
    from app.services.run_failures import _COPY, describe

    class DeclaredUnknown(Exception):
        code = "runtime_error"

    failure = describe(
        DeclaredUnknown('LLM API HTTP 503: {"error":{"message":"Service temporarily unavailable"}}'),
        reference="run_x",
    )
    # 断言的是"分类跑到了 upstream_unavailable 那一条"，不是它当时的措辞 ——
    # 锚文案原文会让每次改写用户可见的话都红一次（措辞本来就该能改）。
    assert failure.title == _COPY["upstream_unavailable"].title["zh"]
    assert "模型服务" in failure.title
    assert failure.retryable is True
    # 声明的 code 照原样进记录 —— 文案和日志各自演化，不互相绑架。
    assert failure.code == "runtime_error"
    assert "internal error" not in failure.body


def test_an_unknown_declared_code_with_no_content_signal_still_falls_back() -> None:
    """没有任何内容信号时仍然落到通用文案 —— 不许凭空猜一个分类。"""
    from app.services.run_failures import describe

    class DeclaredUnknown(Exception):
        code = "some_new_thing"

    failure = describe(DeclaredUnknown("something went sideways"), reference="run_y")
    # 「研究停止了」这个说法 2026-08-18 撤掉了：停的是这一轮，不是研究。
    assert failure.title == "这一轮没能完成"
    assert failure.code == "some_new_thing"


def test_an_old_format_workspace_is_told_it_will_be_upgraded_not_to_start_over():
    """旧格式工作区 ≠ 平台内部错误，也**不再**是"重建项目"（2026-09-15 现场）。

    harness 侧的 `UnmigratedWorkspaceError` 继承 RuntimeError，不点名就落进
    兜底文案（"平台在记录这次运行时撞上了内部错误"）。点了名之后还错过第二次：
    文案停在 2026-08-18「没有迁移工具」那一版，而 09-14 迁移器回来了 —— 同一个
    弹窗上半句叫用户重建项目、删 `research_state__v*.json`，下半句的技术细节说
    会自动升级。yuankk 照上半句做就是把自己的 Analysis 历史删掉。

    判据落在**用户会做的事**上：不许出现"重建项目"，必须出现"别删文件"。
    """
    from app.services.run_failures import describe

    class UnmigratedWorkspaceError(RuntimeError):
        pass

    failure = describe(
        UnmigratedWorkspaceError("这个项目的科研记录还是旧格式（figure__schematic.json）"),
        reference="run_x")

    assert failure.code == "workspace_records_need_upgrade"
    assert failure.retryable is True, "升级会在下一轮开跑前自动做完 —— 这正是重发能解决的"
    assert "内部错误" not in failure.body
    assert "新建一个项目" not in failure.recovery, "迁移器 09-14 就回来了，别再叫人从头再来"
    assert "删除" not in failure.body
    assert "不要删除" in failure.recovery, "必须挡住「删掉那些文件」这个动作"
    assert "发下一条消息" in failure.recovery, "必须给出还能怎么继续"


def test_a_refused_record_upgrade_keeps_the_migrator_reason_and_forbids_deleting():
    """迁移器拒绝了历史 —— 用户要读到"为什么"，而不是一句"内部错误"。

    这条和上一条分开的理由：上一条再发一次就好，这条发一百次都一样（同一份
    历史、同一个核验）。它曾经共用一条文案，于是"还没升"和"升不上去"给的
    下一步是同一句错话。
    """
    from app.services.research_migration import RecordUpgradeFailed
    from app.services.run_failures import describe

    failure = describe(
        RecordUpgradeFailed("RecordMigrationError: Frozen envelope checksum mismatch: a/artifacts/x.json"),
        reference="run_z")

    assert failure.code == "workspace_record_upgrade_failed"
    assert failure.retryable is False, "同一份历史、同一个核验，重发不会有别的结果"
    assert "内部错误" not in failure.body
    assert "不要删除 artifacts" in failure.recovery
    # 归因留给技术细节，正文不许泄露异常原文（这个模块的老毛病）。
    assert "Frozen envelope checksum mismatch" in failure.as_record()["detail"]
    assert "Frozen envelope checksum mismatch" not in failure.body


def test_a_worker_holding_the_workspace_reuses_the_busy_copy_instead_of_a_new_one():
    """升级被活 worker 挡住时，用户要做的事和 `project_busy` 一模一样。

    文案表的加条判据是"用户能做的事不一样"。这里不一样的只有我们内部的原因，
    多一个条目只是多一句同义的话。
    """
    from app.services.research_migration import RecordUpgradeBlocked
    from app.services.run_failures import describe

    failure = describe(RecordUpgradeBlocked("A research worker still holds this workspace"))

    assert failure.code == "project_busy"
    assert failure.retryable is True


def test_provider_failure_names_the_model_service() -> None:
    """模型服务挂了就说模型服务 —— 别让"平台内部错误"替供应商背锅。

    现场（2026-08-20，积算网关 ReadTimeout）：worker 兜底把一切异常标成
    `runtime_error`，文案表认不出 → 用户读到"平台内部错误"，调度器跟着
    转述成"框架错误"。三方里唯一没错的平台把锅全背了。
    """
    from app.services.run_failures import describe

    class _HarnessErr(Exception):
        def __init__(self, msg: str, code: str) -> None:
            super().__init__(msg)
            self.code = code

    failure = describe(_HarnessErr("ReadTimeout after retry budget", "upstream_unavailable"))
    assert failure.code == "upstream_unavailable"
    assert "模型服务" in failure.title
    assert "不是平台" in failure.body
    assert failure.retryable is True

    # harness 侧对同一类事实的类别名（executor failure_category 词表，兼容性
    # 硬约束不能改）—— 两个名字必须落到同一份文案，不许一个掉进兜底。
    alias = describe(_HarnessErr("stream dropped", "provider_unavailable"))
    assert alias.title == failure.title
    assert alias.retryable is True


def test_an_upstream_rejection_is_not_retryable_and_not_ours() -> None:
    """上游**明确拒绝**（额度/凭据）≠ 上游抖了一下，更 ≠ 平台内部错误。

    现场（2026-08-22，积算 403 `User quota is exhausted`）：整条判据链一个都
    没接住 —— `_BY_TYPE` 认不出 `LLMHTTPError`，运行时声明的 code 是
    `runtime_error`，`_is_transient_upstream` 只认 429/5xx。于是掉进兜底，
    用户读到「平台在记录这次运行时撞上了内部错误 / 再发一次即可」，照做点
    「继续」，5 秒后同一条又来一次。**错误的归因把人按在原地空转。**

    这里断的是三件事：归到自己那条文案、`retryable is False`（重发无用是
    真结论，不是"不知道"）、以及上游原话只在 detail 里。
    """
    from app.services.run_failures import describe

    class _HarnessErr(Exception):
        def __init__(self, msg: str, code: str) -> None:
            super().__init__(msg)
            self.code = code
            self.error_type = "LLMHTTPError"

    failure = describe(
        _HarnessErr(
            'LLM API HTTP 403: {"error":{"message":"User quota is exhausted, '
            'please recharge","type":"Iapi_error"}}',
            "runtime_error",
        ),
        reference="run_z",
    )

    assert failure.title == _copy_of("upstream_rejected").title["zh"]
    assert failure.retryable is False, "同一个空额度，重发一万次都是这个结果"
    assert "再发一次即可" not in failure.recovery
    # 上游那句话是**证据**，归 detail；正文一律取自文案表。
    assert "quota is exhausted" in failure.detail
    assert "quota is exhausted" not in failure.body
    # 声明的 code 照原样进记录 —— 文案和日志各自演化。
    assert failure.code == "runtime_error"


def test_401_402_403_all_reach_the_rejection_copy() -> None:
    """判据取**我们自己写的** HTTP 码，不猜 provider 的措辞。

    每家上游对"没钱了"的说法都不一样（quota exhausted / insufficient_quota /
    欠费…）。按措辞认 = 给每一家各维护一份名单，换一家就默认漏过。
    """
    from app.services.run_failures import describe

    for status in (401, 402, 403):
        failure = describe(RuntimeError(f"LLM API HTTP {status}: nope"))
        assert failure.code == "upstream_rejected", status
        assert failure.retryable is False, status

    # 隔壁那一类不能被拖下水：429/5xx 仍然是"等一下再发"。
    assert describe(RuntimeError("LLM API HTTP 429: slow down")).retryable is True
    assert describe(RuntimeError("LLM API HTTP 503: upstream")).retryable is True


def _copy_of(code: str):
    from app.services.run_failures import _COPY

    return _COPY[code]


# ── 「执行进程退出了」是三件事，按退出码分 ──────────────────────────────────


class _ExitedProcess(Exception):
    """`HarnessSessionProcessError` 的形状：消息 + 它自己带着的退出码。"""

    code = "harness_process_exited"

    def __init__(self, exit_code: int) -> None:
        super().__init__(f"Harness runtime process exited before replying (exit code {exit_code})")
        self.exit_code = exit_code


def test_a_clean_exit_says_it_stood_down_and_can_be_resumed():
    """exit 0 = worker 手上没活、也没人在看，按设计收摊（无人看守宽限到点）。

    会话状态在盘上，下一条消息 respawn 一个接着来 —— 缓存未命中不是错误。
    """
    from app.services.run_failures import describe

    failure = describe(_ExitedProcess(0), reference="run_a")

    # `code` 按设计保留异常声明的那个（日志与文案各自演化）——判据落在**用户
    # 读到的话**上，那才是这次要修的东西。
    assert failure.code == "harness_process_exited"
    assert failure.retryable is True, "可续的那一种却标着不可重试 —— 和文案互相拆台"
    assert "按设计" in failure.body, "它没崩 —— 要说清这是设计行为，不是故障"


def test_being_asked_to_stop_is_not_the_same_as_being_force_killed():
    """exit -15（SIGTERM）= 有人要它停，绝大多数是我们自己在部署/关机。"""
    from app.services.run_failures import describe

    failure = describe(_ExitedProcess(-15), reference="run_b")

    assert failure.retryable is True
    assert "SIGTERM" in failure.body
    assert "要求停止" in failure.title


def test_a_force_kill_does_not_promise_that_sending_again_helps():
    """exit -9（SIGKILL）= 被强杀，普查里跑了 6663s / 6908s 的那两条是 OOM 形状。

    这条以前和上面两条共用「发下一条消息就从断点接着跑」——同一份内存压力，
    那句话是假承诺。三态里的 False，不是"不知道"。
    """
    from app.services.run_failures import describe

    failure = describe(_ExitedProcess(-9), reference="run_c")

    assert failure.retryable is False, "同一份内存压力，重发不会有别的结果"
    assert "强制终止" in failure.title
    assert "内存" in failure.body
    assert "接着跑" not in failure.recovery, "别再许诺重发就能继续"


def test_all_three_exits_get_different_words_not_one_label():
    """判据落在**用户读到的话**上：三种退出码不许折回同一段文案。"""
    from app.services.run_failures import describe

    said = {code: describe(_ExitedProcess(code)) for code in (0, -15, -9)}
    assert len({f.title for f in said.values()}) == 3, "三种退出共用了同一个标题"
    assert len({f.recovery for f in said.values()}) == 3, "三种退出共用了同一条下一步"


def test_an_exit_code_we_cannot_read_still_falls_back_instead_of_guessing():
    """退出码缺席 / 是正的非零 —— 那时该由类型和 stderr 说话，不许瞎猜。"""
    from app.services.run_failures import describe

    class _NoExitCode(Exception):
        code = "harness_process_exited"

    assert describe(_NoExitCode("gone")).code == "harness_process_exited"
    # 正的非零 = 进程自己以错误码退出，答案在它的 stderr 里，不归退出码判。
    assert describe(_ExitedProcess(2)).code == "harness_process_exited"
