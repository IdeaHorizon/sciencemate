"""「要不要无人值守」和「预授权哪些高危类别」必须来自同一次配置读取。

## 为什么这条要钉死

两个事实分开读，就会出现**半开状态**：模式开着、授权范围读成空。症状是
"自主模式跑着，但每个高危点都停下等一个不存在的人" —— 正是 2026-08-10 那次
静默挂两小时的形状。而分叉时两边都不报错。

所以它们打包成 `AutonomyPolicy`，由 `project_autonomy_policy` 一次读出。

## 顺带钉住的：默认必须是"都停"

授权范围只能由人显式给出。框架替他推断"你既然开了自主，那大概什么都同意"
—— 那就是把 `bypass_dangerous=True` 换个名字重来一遍。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.models.project import OperationMode

from app.services import local_execution


def test_policy_comes_from_a_single_config_read() -> None:
    source = inspect.getsource(local_execution.project_autonomy_policy)
    # 一次查询，两个事实。这一条是**结构性**的，所以按源码判：分成两次读就是
    # 两个真相源，而那件事在行为上只有分叉的那一刻才看得见。
    assert source.count("select(ProjectConfig)") == 1
    assert "autonomous_authorized_risk_classes" in source


@pytest.mark.asyncio
async def test_autonomous_mode_yields_an_unattended_policy(monkeypatch) -> None:
    """自主模式 + 一台守得住的机器 = 真的无人值守。

    从前这一条写成 `assert "AutonomyPolicy(unattended=True" in source` —— 一个
    字符串比对：加一个参数换一行写法它就红，而行为一点没变（2026-09-05 实测）。
    判据落在返回值上。
    """
    config = SimpleNamespace(
        operation_mode=OperationMode.AUTONOMOUS,
        autonomous_authorized_risk_classes=["compute"],
    )

    class _Db:
        async def scalar(self, _query):
            return config

    monkeypatch.setattr(local_execution, "_machine_missing_for_unattended", lambda: [])
    policy = await local_execution.project_autonomy_policy(
        _Db(), SimpleNamespace(id="p1")
    )
    assert policy.unattended is True
    assert policy.authorized_risk_classes == ("compute",)
    assert not policy.blocked_reason


@pytest.mark.asyncio
async def test_a_machine_without_a_boundary_downgrades_the_policy(monkeypatch) -> None:
    """守不住写边界的机器上，自主档降回一步步跑，并说清楚为什么（issue #798）。"""
    config = SimpleNamespace(
        operation_mode=OperationMode.AUTONOMOUS,
        autonomous_authorized_risk_classes=["compute"],
    )

    class _Db:
        async def scalar(self, _query):
            return config

    monkeypatch.setattr(
        local_execution, "_machine_missing_for_unattended", lambda: ["write_boundary"]
    )
    policy = await local_execution.project_autonomy_policy(_Db(), SimpleNamespace(id="p1"))
    assert policy.unattended is False, "算出来的判据必须真的改变行为"
    assert "不会自己开下一轮" in policy.blocked_reason


def test_not_autonomous_means_no_authorization() -> None:
    """非自主模式下，授权范围一律为空。

    否则一个项目从 autonomous 切回 assisted 之后，那份声明还在配置里躺着，
    下次不知怎么就生效了。授权跟着模式走。
    """
    source = inspect.getsource(local_execution.project_autonomy_policy)
    idx_guard = source.index("operation_mode == OperationMode.AUTONOMOUS")
    idx_read = source.index("autonomous_authorized_risk_classes")
    assert idx_guard < idx_read, "必须先确认是自主模式，再读授权范围"
    assert "return AutonomyPolicy()" in source


def test_failure_to_read_config_does_not_change_failure_shape() -> None:
    """读一个**可选设置**失败，绝不能顶掉真正的错误。

    这条是既有教训（第一版把查询放在包住模型调用的 try 里，一抛异常就把
    failure["code"] 吃掉了），换成 dataclass 之后要继续成立。
    """
    source = inspect.getsource(local_execution.project_autonomy_policy)
    assert "except Exception" in source
    assert source.count("return AutonomyPolicy()") >= 2


def test_every_request_carries_the_current_authorization() -> None:
    """声明必须一路送到 harness —— 而且**每一条**请求都带，不是某几个 op 带。

    原来这条断言的是源码里有 `authorized_risk_classes=authorized` 这个字面串。
    那种判据把写法冻在测试里：2026-08-13 把接收点从"每个 op 各自解析"挪到
    "唯一收发口"之后，行为**变好了**，这条测试却红了 —— 一个测不到被测系统的
    失败。同事在 `04552d7` 点过这个毛病。

    现在看事实：拿一个假的 stdin/stdout 起一个会话，发一条请求，去读**真正
    写进管道的那一行 JSON**。哪个 op 都一样 —— 这正是"漏不掉"的含义。
    """
    import asyncio
    import json

    from app.services.harness_sessions import AppRunBinding, _ProjectHarnessSession

    written: list[bytes] = []

    class _Stdin:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

    class _Stdout:
        answered = False

        async def readline(self) -> bytes:
            # 立刻给一个终止事件，让 RPC 收工 —— 我们只关心**发出去**的那一行。
            #
            # 答完就 EOF：读者是一个**长活的**任务（P1-1 起一个会话一个读者），
            # 一个永远吐同一行的假 stdout 会让它空转到测试超时。真管道在这里
            # 是阻塞的 —— 替身也得表现得像真的。
            if self.answered:
                return b""
            self.answered = True
            payload = json.loads(written[-1].decode())
            return (
                json.dumps(
                    {"type": "result", "request_id": payload["request_id"], "data": {"status": "completed"}}
                ) + "\n"
            ).encode()

    class _Process:
        returncode = None
        stdin = _Stdin()
        stdout = _Stdout()

    # 走**真的构造器**，只把 process 换成假的。
    #
    # 原来这里是 `__new__` + 手工补七八个字段。那是一份"会话长什么样"的抄件，
    # 而抄件会各自演化：P1-1 给会话加了多路复用的四个字段，这条测试当场
    # AttributeError —— 红的原因跟被测的性质（每条请求都带授权）毫无关系。
    # 「子集按构造方选，不按改动文件选」的同款：漏的总是别处手工造这个对象
    # 的地方。
    session = _ProjectHarnessSession(
        project_id="p", session_id="s", owner_user_id="u",
        backend_id="b", backend_fingerprint="f",
        platform_context_hash=None,
        process=_Process(), stderr_task=None, provider_secrets=("secret",),
    )

    async def _noop(_event: dict) -> None:
        return None

    class _Policy:
        unattended = True
        authorized_risk_classes = ("真实外部作业提交",)

    session._authorize(_Policy())

    async def _go() -> None:
        await session.turn(
            binding=AppRunBinding("u", "c", "r", "s"),
            message="跑",
            on_progress=_noop,
            on_protocol_event=_noop,
            unattended=True,
        )

    asyncio.run(_go())

    sent = json.loads(written[-1].decode())
    assert sent["authorized_risk_classes"] == ["真实外部作业提交"], (
        f"写进管道的那一行没带上当下的授权范围：{sent}"
    )
    assert sent["op"] == "run_unattended"


def test_the_authorization_is_a_session_property_not_an_op_argument() -> None:
    """刷新一次，之后**所有** op 都按新范围走 —— 包括将来新增的 op。

    这是 2026-08-13 死循环的根因判据：当时只有 `run_unattended` 认这个字段，
    `answer` 不认，于是空授权起跑的 run 拿不到后来给的授权，「连续」永远
    兑现不了。逐个 op 补字段是"护栏写成名单"，所以它必须是会话属性。
    """
    from app.services.harness_sessions import _ProjectHarnessSession

    session = _ProjectHarnessSession.__new__(_ProjectHarnessSession)
    session.authorized_risk_classes = []

    class _Policy:
        authorized_risk_classes = ("*",)

    session._authorize(_Policy())
    assert session.authorized_risk_classes == ["*"]

    # None = 生命周期类操作（terminate 之类），不该把授权悄悄清掉。
    session._authorize(None)
    assert session.authorized_risk_classes == ["*"], "一次无关操作把授权收回去了"
