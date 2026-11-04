"""内部故障变成用户能读的东西 —— 只在这里发生一次。

## 现场（wangd 2026-08-11 试用）

会话页面上，用户看到的是这个：

    Research stopped before completion
    Execution failed before an agent response was produced:
    (sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) … duplicate key value
    violates unique constraint "uq_events_sequence" … [SQL: INSERT INTO
    execution_events (id, tenant_id, workspace_id, …) VALUES ($1::VARCHAR, …)]
    [parameters: ('b145e65e63be…', 'local-tenant', …)]

整条 INSERT 语句、全部列名、全部参数值，摆在会话正文里。

## 不是"忘了美化"，是没有边界

原来的做法：

    failure = {"code": …, "message": f"…: {str(exc)}"}      # 后端
    …
    if (/Harness session operation timed out/i.test(message)) {…}  # 前端正则认领

两个问题叠在一起：

1. **后端手里有异常对象**（类型、code、上下文全都在），却把这些信息压成一个
   字符串扔出去；前端再用正则去猜回来。**同一个问题两个真相源**，而且前端那份
   永远比后端少知道一些。
2. 前端那套 if-链是**名单**：认得的给人话，认不得的原样显示。于是每出现一个
   新异常，默认行为就是把它的 `str()` 糊到用户脸上 —— 护栏要扫盘，不要写名单。

## 重写的话会怎么写

一条到得了用户面前的错误是**产品对象**，不是字符串。它要回答三件事：
出了什么事（用户的语言）、现在能做什么、以及怎么找到真正的细节。

    UserFacingFailure(code, title, body, recovery, retryable, detail)

关键的不变量：**title / body / recovery 只能来自本文件的文案表，`str(exc)` 只能
进 `detail`**。这不是靠"记得脱敏"或者"扫一遍有没有 SQL 关键字"来保证的 ——
是构造上就没有第二条路：`describe()` 从表里取文案，异常原文只经过 `detail` 参数。
表里没有的 code 落到 `execution_failed`，照样是一句人话 + 一个参考号。

于是"新异常泄露原文"这件事从**默认发生**变成**不可能发生**。

## 脱敏和翻译是两件事

原函数叫 `_sanitized_platform_failure` —— 它做的是脱敏（去掉密钥），产出的却是
用户要读的正文。两件事一条规则：脱敏干净的 SQL 转储，仍然是 SQL 转储。这里把
它们分开：脱敏管 `detail` 能不能存，文案表管用户看到什么。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.services.redaction import DEFAULT_REDACTION_POLICY

logger = logging.getLogger(__name__)

#: `detail` 的上限。它进的是「Technical details」折叠区，不是正文，但也没必要
#: 把一条 8KB 的 SQL 参数转储整个存进 run summary。
_DETAIL_MAX_CHARS = 2000


@dataclass(frozen=True)
class _Copy:
    """一类故障对用户的说法。

    `retryable` 是**三态**：`True` = 重发安全，`False` = 重发一定还是这个结果，
    `None` = 我们不知道。三态不能压成两态 —— "不知道"报成"不可重试"是给用户
    一个我们没有的结论，报成"可重试"是给假希望。判不出来就不说（fail-closed），
    用户照旧可以自己重发，只是平台不承诺。
    """

    title: dict[str, str]
    body: dict[str, str]
    recovery: dict[str, str]
    retryable: bool | None = None


#: 兜底 code。用平台既有的名字，不另造一个 —— 同一个概念两个名字，日志和
#: 前端就会各自演化出一套判据。
_GENERIC = "execution_failed"

#: **故意**走兜底的 code（RFC_WORKER_START_IS_NOT_A_GATE §3.7 的文案闸）。
#:
#: 每个运行时会声明的 code 必须有去处：要么在 `_COPY` 里有自己的文案，要么登记在这里并说明
#: 为什么兜底就够。两张表都没有的 code，`tests/test_every_failure_code_has_a_home.py` 红。
#: 登记的判据和加文案的判据是同一条：**这类故障用户能做的事和别的不一样吗？** 下面这些
#: 全是调用方（前端 / 后端 / 协议）的 bug 或协议误用 —— 用户除了「再发一次 + 报 reference」
#: 没有第二种动作，单独一句文案不会让任何人多做对一件事。
#:
#: 这里**只许登记已经存在的 code**：闸同时反向查，表里登记了、代码里却没人声明的，一样红。
_FALLS_TO_GENERIC: dict[str, str] = {
    # ── worker 协议层：请求本身不合法（后端拼错了）──
    "invalid_request": "请求不是对象 / 缺字段：后端拼错了，用户改不了",
    "invalid_request_id": "后端拼错了",
    "invalid_tenant_id": "后端拼错了",
    "invalid_session_id": "后端拼错了",
    "invalid_project_id": "后端拼错了",
    "invalid_message": "空消息：前端已经挡了，这里只是第二道",
    "invalid_operation": "op 不在词表里：后端版本错配",
    "invalid_json": "协议行不是 JSON：后端版本错配",
    "empty_request": "协议空行：后端版本错配",
    "duplicate_request_id": "RPC 重放被拒：后端 bug",
    "already_initialized": "重复 init：后端 bug",
    "not_initialized": "没 init 就发 op：后端 bug",
    "already_rejoined": "重复接回：后端 bug",
    "control_socket_unavailable": "命令面地址起不来：部署问题，兜底文案 + 日志够查",
    "runtime_error": "worker 兜底：类型与原文在 detail 里，分不出更细的类",
    # ── 对话面：参数不合法（前端拼错了）──
    "invalid_answer": "前端拼错了",
    "invalid_choice": "前端拼错了",
    "invalid_pause_id": "前端拼错了",
    "invalid_max_turns": "前端拼错了",
    "invalid_text": "前端拼错了",
    "invalid_action": "前端拼错了",
    "invalid_autonomy_mode": "前端拼错了",
    "invalid_authorized_risk_classes": "前端拼错了",
    "invalid_sandbox_manifest": "后端拼错了",
    "sandbox_authorization_mismatch": "后端与 worker 对不上：部署问题",
    # 执行目标（本机 / wsl:<发行版>）：值在接口里先核过才存，到 worker 还拼错 = 后端 bug；
    # 在跑的 attempt 不能原地换目标，前端状态过期时撞到，等这一轮结束再换即可。
    "invalid_execution_target": "后端拼错了",
    "sandbox_target_mismatch": "后端与 worker 对不上：在跑的一轮不能原地换执行位置",
    "no_pause_pending": "没有待答的 pause：前端状态过期，重发即可",
    "pause_conflict": "pause 版本冲突：前端状态过期，重发即可",
    "pause_not_addressable": "pause 不可寻址：前端状态过期，重发即可",
    # ── 知识 / 组织面：参数不合法（后端拼错了）──
    "invalid_entity": "后端拼错了",
    "invalid_query": "后端拼错了",
    "invalid_items": "后端拼错了",
    "invalid_projects": "后端拼错了",
    "invalid_candidates": "后端拼错了",
    "invalid_proposal_id": "后端拼错了",
    "invalid_decision": "后端拼错了",
    "invalid_reasoning": "后端拼错了",
    "invalid_by": "后端拼错了",
    "invalid_org_home": "后端拼错了",
    "invalid_org_id": "后端拼错了",
    "invalid_grants": "后端拼错了",
    "invalid_machines": "后端拼错了",
    "resolve_failed": "组织层解析失败：detail 里有原文",
}

#: 文案表 —— **用户看到的每一个字都出自这里**。
#:
#: 加新条目的判据：这类故障用户能做的事和别的不一样。如果 recovery 和
#: `execution_failed` 一模一样，那就别加 —— 多一个条目只是多一句同义的话，
#: 不会让任何人多做对一件事。
_COPY: dict[str, _Copy] = {
    "strategy_generation_failed": _Copy(
        title={"zh": "生成检索计划失败", "en": "Could not create a search plan"},
        body={"zh": "模型没有返回有效的检索计划，本次检索已停止。",
              "en": "The model did not return a valid search plan, so the search stopped."},
        recovery={"zh": "请重试；若持续失败，请检查模型连接和额度，或缩短检索问题。",
                  "en": "Try again. If it keeps failing, check the model connection and quota, or shorten the search question."},
        retryable=True,
    ),
    "strategy_translation_failed": _Copy(
        title={"zh": "生成检索词失败", "en": "Could not generate search terms"},
        body={"zh": "模型没有返回有效的英文检索词，检索已停止。",
              "en": "The model did not return valid English search terms, so the search stopped."},
        recovery={"zh": "请重试。如果仍然失败，请检查模型的连接和额度，或直接输入英文检索词。",
                  "en": "Try again. If it keeps failing, check the model connection and quota, or enter English search terms directly."},
        retryable=True,
    ),
    "empty_curation": _Copy(
        title={"zh": "生成项目检索词失败", "en": "Could not generate search terms for the project"},
        body={"zh": "资讯挖掘模型没有返回有效的检索词。",
              "en": "The feed curation model returned no valid search terms."},
        recovery={"zh": "请确认项目名称和描述足够具体，并检查资讯挖掘模型的连接和额度后重试。",
                  "en": "Make sure the project title and description are specific, and check the feed curation model's connection and quota, then try again."},
        retryable=True,
    ),
    "execution_failed": _Copy(
        # 「研究停止了」这个说法是错的（wangd 2026-08-18）：停的是**这一轮**，
        # 研究是磁盘上那份记录，它没有"停"这个状态。措辞按事实来。
        title={"zh": "这次执行未完成", "en": "This run did not finish"},
        # ⚠️ 这句话是**兜底**，也就是"我们认不出这次是怎么回事"。原文写的是
        # 「平台在记录这次运行时撞上了内部错误」——它把一句无知说成了一个诊断，
        # 而且诊断还是错的：绝大多数掉进这里的失败既不在记录阶段，也不是平台
        # 内部的事（2026-08-22 现场是上游 403 额度用尽）。
        #
        # wangd 2026-08-22：「尽量别出现红色的字，然后他妈的内部错误，这些东西
        # 用户不应该看到的」。兜底文案只说**我们确实知道的那部分**：这一轮停了、
        # 记录还在、下一条消息接着跑。归因留给日志（reference 指得到）。
        body={
            "zh": "这次执行中途停止了，已记录的内容都还在。",
            "en": "This run stopped partway. Everything recorded so far is still here.",
        },
        # 「如果连着几次都停在这里，把 reference 报出来」那句删了（RFC_WORKER_START_IS_NOT_A_GATE §5）：
        # 「连着几次」现在是机械判据 —— 同一会话同一 code 连续第二次，后端自动跑 doctor，结果在会话
        # 顶部的条幅里。把人当重试循环的那半句没有读者了。
        recovery={
            "zh": "发送新消息即可从中断处继续。如果同样的问题反复出现，会话顶部会显示自动诊断结果。",
            "en": (
                "Send a new message to continue from where it stopped. If the same problem keeps "
                "happening, an automatic diagnosis appears at the top of the Session."
            ),
        },
    ),
    "upstream_rejected": _Copy(
        # 上游**明确拒绝**，不是抖动。和 `upstream_unavailable` 分开是因为
        # 用户要做的事完全相反：那个是"等一下再发"，这个是"发一万次也一样，
        # 去换后端 / 处理额度"。
        #
        # 2026-08-22 现场：积算返 403 `User quota is exhausted`，整条判据链
        # （`_BY_TYPE` 认不出 LLMHTTPError、声明的 code 是 `runtime_error`、
        # `_is_transient_upstream` 只认 429/5xx）一个都没接住，掉进兜底 →
        # 用户读到「平台内部错误 / 再发一次即可」，照着点「继续」，5 秒后
        # 又是同一条。错误归因把人按在原地空转。
        title={"zh": "模型服务拒绝了请求", "en": "The model service rejected the request"},
        body={
            "zh": "模型服务账号的额度不足或密钥无效（HTTP 401/402/403）。服务返回的原始信息见「技术细节」。",
            "en": (
                "The model service account is out of quota or its key is invalid (HTTP "
                "401/402/403). The service's original message is under Technical details."
            ),
        },
        recovery={
            "zh": "点输入框下方的模型名称，换一个可用的模型；或处理好额度和密钥后重新发送。已记录的内容都还在。",
            "en": (
                "Click the model name below the message box to switch to a working model, or "
                "sort out the quota or key and send again. Everything recorded is still here."
            ),
        },
        # 三态里的 False：同一个空额度、同一个密钥，重发一万次都是这个结果。
        # 这是真结论，不是"不知道"。
        retryable=False,
    ),
    "storage_conflict": _Copy(
        title={"zh": "这次执行未完成", "en": "This run did not finish"},
        body={
            "zh": "会话记录同时被两处写入，发生了冲突。",
            "en": "Two processes tried to write this Session's history at the same time.",
        },
        recovery={
            "zh": "请重新发送，已生成的内容都还在。",
            "en": "Send it again. Nothing already produced was lost.",
        },
        retryable=True,
    ),
    "project_busy": _Copy(
        # ## 这条曾经叫 `session_busy`，而"忙"是三个问题（RFC D10）
        #
        # 2026-08-23：`session_busy` 那三处 raise 全部删除 —— "会话被占着"
        # 不再是拒收理由（占用是排队的理由）。产生它的代码路径没有了，文案
        # 也就跟着走。
        #
        # 剩下的这一个是**另一件事**：`ProjectBusyError` 由 worker 抢不到工作区
        # 的 flock 抛出，答的是 D10 三拆里的**所有权**那一维 —— 有另一个进程
        # 攥着这个 session 的工作区。这件事没有消失，也不该消失。
        #
        # 措辞按事实来：**不承诺"稍等一下"**。攥着它的那个进程可能正在跑一个
        # 几小时的节点，也可能是 unattended 停靠在按小时退避的复查间隔上
        # （8-21 现场：睡 4 小时）。给一个我们没有的时间尺度，比不给更糟。
        title={"zh": "这次执行未完成", "en": "This run did not finish"},
        body={
            "zh": "这个会话的工作区正被另一次执行占用，它可能正在运行，也可能在等待。",
            "en": "Another run is still using this Session's workspace. It may be running or waiting.",
        },
        recovery={
            "zh": "已记录的内容都在下方。等它结束后重新发送即可继续；如需立即接管，请点击「停止执行」。",
            "en": (
                "Everything recorded so far is below. Send again once it finishes to continue, "
                "or click Stop run to take over now."
            ),
        },
        retryable=True,
    ),
    # ── worker 启动的三种硬失败（RFC_WORKER_START_IS_NOT_A_GATE §3.1）──────────────
    # 调模型之前只剩这几件事能把一轮杀掉；其余持久化前提都降级跑。文案各说各的：
    # 模型不可达说模型，身份冲突说"这个家是别人的"，不说"读不到"。
    "provider_not_configured": _Copy(
        title={"zh": "模型未配置", "en": "The model is not configured"},
        body={
            "zh": "这次执行没有开始：所选模型缺少地址或模型名称。",
            "en": "This run did not start: the selected model has no endpoint or model name.",
        },
        recovery={
            "zh": "请在「设置 → 模型与密钥」中补全地址和模型名称，然后重新发送。",
            "en": "Fill in the endpoint and model name under Settings → Models & credentials, then send again.",
        },
        retryable=False,
    ),
    "tenant_home_conflict": _Copy(
        title={"zh": "数据目录属于其他用户", "en": "This data folder belongs to another user"},
        body={
            "zh": "这个会话的数据目录登记的是另一位用户。为避免混入他人的知识和记忆，这次执行没有开始。",
            "en": (
                "This Session's data folder is registered to another user. To avoid mixing in "
                "their knowledge and memory, this run did not start."
            ),
        },
        recovery={
            "zh": "这是部署问题，请把「技术细节」中的参考编号发给管理员。",
            "en": "This is a deployment problem. Send the reference number under Technical details "
                  "to your administrator.",
        },
        retryable=False,
    ),
    "runtime_identity_conflict": _Copy(
        title={"zh": "运行目录属于其他会话", "en": "This run folder belongs to another Session"},
        body={
            "zh": "这个会话的运行目录登记的是另一个会话。为避免两个会话的对话和记录混在一起，这次执行没有开始。",
            "en": (
                "This Session's run folder is registered to another Session. To avoid mixing the "
                "two conversations and records, this run did not start."
            ),
        },
        recovery={
            "zh": "请新建一个会话继续，并把「技术细节」中的参考编号发给管理员。",
            "en": "Continue in a new Session, and send the reference number under Technical details "
                  "to your administrator.",
        },
        retryable=False,
    ),
    "runtime_environment_broken": _Copy(
        # 与 harness_process_exited 分开的理由：那条是"进程中途死了（原因
        # 未知）"，这条是"进程根本起不来，而且我们知道为什么"——执行环境
        # （解释器/依赖/checkout）坏了。给用户的话完全不同：前者重发多半能
        # 接着跑，后者重发一百次都是这个，得先修环境。
        title={"zh": "执行环境异常", "en": "The execution environment is broken"},
        body={
            "zh": "运行助手所需的程序或依赖缺失。这是部署问题，你的研究记录不受影响。",
            "en": (
                "Programs or dependencies the assistant needs are missing. This is a "
                "deployment problem; your research records are not affected."
            ),
        },
        recovery={
            "zh": (
                "请联系管理员修复执行环境（在 harness 目录运行 `uv sync --frozen`）"
                "或重新部署。修复后重新发送即可从中断处继续。"
            ),
            "en": (
                "Ask an administrator to repair the execution environment (run "
                "`uv sync --frozen` in the harness directory) or redeploy. Once fixed, "
                "send again to continue from where it stopped."
            ),
        },
        # 环境修好之前，重发一定还是这个结果 —— 别给假希望。
        retryable=False,
    ),
    "app_server_restarted": _Copy(
        # 与 `harness_process_exited` 分开的理由：那条说的是"运行时自己没了"
        # （原因未知，可能真是研究出了问题）；这条说的是"**我们**把它掐了"。
        # 给用户的话完全不同 —— 前者值得看一眼发生了什么，后者不用看，接着做。
        title={"zh": "平台重启，这次执行被中断", "en": "A platform restart interrupted this run"},
        body={
            "zh": "重启前记录的内容都还在。",
            "en": "Everything recorded before the restart is still here.",
        },
        recovery={
            "zh": "发送新消息即可从中断处继续，不会从头开始。",
            "en": "Send a new message to continue from where it stopped; it will not start over.",
        },
        retryable=True,
    ),
    # ── 「执行进程退出了」是**三件事**，按退出码分（2026-09-16 全库普查）──────
    #
    #   exit 0   4 次   它自己干净地走了（无人看守的宽限到点：60.7s / 49.5s / 0.2s…）
    #   exit -15 4 次   收到 SIGTERM —— 其中两对同秒（08-21 08:52:40、08-27 05:48:02），
    #                   那是后端关机 `terminate_all` 的签名
    #   exit -9  4 次   被 SIGKILL 强杀，跑了 6663s / 6908s 的那两条是 OOM 的形状
    #
    # 三件事共用一句「发下一条消息就从断点接着跑」。对 -9 那四条它是**错的**：
    # 同一份内存压力，重发一万次还是同一个结果。文案表的加条判据就是"用户能做
    # 的事不一样"，这里不一样得很彻底。
    "harness_process_exited": _Copy(
        # exit 0：这是**可续的**那一种。worker 手上没活、也没人在看，于是按
        # 设计自己退场（`platform_runtime._NO_WATCHER_GRACE_S`）——会话状态在
        # 盘上，下一条消息 respawn 一个接着来。「缓存未命中不是错误」。
        title={"zh": "执行进程空闲后已自动退出", "en": "The execution process exited after being idle"},
        body={
            "zh": "进程在没有任务、也无人查看时会自动退出。已记录的内容都还在。",
            "en": (
                "The process exits on its own when it has no work and nobody is watching. "
                "Everything recorded is still here."
            ),
        },
        recovery={
            "zh": "发送新消息会重新启动，并从中断处继续，不会从头开始。",
            "en": "Send a new message to restart and continue from where it stopped; it will not start over.",
        },
        retryable=True,
    ),
    "harness_process_signalled": _Copy(
        # exit < 0 且是 SIGTERM：**有人要它停**。绝大多数情况下那个人是我们
        # 自己（部署 / 关机的 `terminate_all`）。与 `app_server_restarted` 的
        # 区别：那条是我们**知道**自己正在关机时说的话；这条是事后从退出码
        # 认出来的，说不出是谁发的信号，所以措辞只说事实。
        title={"zh": "执行进程被终止", "en": "The execution process was stopped"},
        body={
            "zh": "进程收到了终止信号（SIGTERM），通常是因为平台正在更新或重启。已记录的内容都还在。",
            "en": (
                "The process received a termination signal (SIGTERM), usually because the "
                "platform was updating or restarting. Everything recorded is still here."
            ),
        },
        recovery={
            "zh": "发送新消息即可从中断处继续。",
            "en": "Send a new message to continue from where it stopped.",
        },
        retryable=True,
    ),
    "harness_process_killed": _Copy(
        # exit < 0 且不是 SIGTERM（实测全是 -9/SIGKILL）：**它是被强杀的**。
        # SIGKILL 拦不住、也不留遗言，所以进程自己什么都没能记下来。
        #
        # 为什么不跟上面两条共用「发下一条消息接着跑」：普查里 -9 的两条跑了
        # 6663s / 6908s，是内存压力的形状。同一份压力重发一次还是同一个结果 ——
        # 这是三态里的 False，不是"不知道"。
        title={"zh": "执行进程被系统强制终止", "en": "The execution process was force-killed by the system"},
        body={
            "zh": "进程被系统强制结束（SIGKILL），没有留下原因，最常见的原因是内存不足。已记录的内容都还在。",
            "en": (
                "The process was force-killed by the system (SIGKILL) and left no reason. The "
                "most common cause is running out of memory. Everything recorded is still here."
            ),
        },
        recovery={
            "zh": (
                "直接重发很可能再次失败。请把任务拆小后再试，或把「技术细节」中的参考编号"
                "发给管理员，检查这台机器的内存和资源限制。"
            ),
            "en": (
                "Sending the same request again will likely fail the same way. Split the task "
                "into smaller steps, or send the reference number under Technical details to "
                "your administrator to check this machine's memory and resource limits."
            ),
        },
        # 同一份内存压力、同一份工作量，重发不会有别的结果。
        retryable=False,
    ),
    "harness_worker_unreachable": _Copy(
        # 与 `harness_process_exited` 分开的理由：那条是"进程中途死了"，这条是
        # "进程起了（或还在），可平台没能和它接上话" —— 连到的是上一代还没退干净
        # 的监听、一个死掉的地址，或者它答不出自己是谁（2026-09-15 node20，
        # respawn 时连进了垂死的上一代）。研究记录一点没动，重发就会重新起一个。
        title={"zh": "无法连接执行进程", "en": "Could not connect to the execution process"},
        body={
            "zh": "执行进程已启动，但平台无法与它建立连接。已记录的内容都在下方。",
            "en": (
                "The execution process started, but the platform could not connect to it. "
                "Everything recorded so far is below."
            ),
        },
        recovery={
            "zh": "重新发送会启动新的进程，并从中断处继续。",
            "en": "Send again to start a new process and continue from where it stopped.",
        },
        retryable=True,
    ),
    "harness_operation_timeout": _Copy(
        # 这条文案原来长在前端的正则 if-链里（`/Harness session operation
        # timed out/i`）。搬到这里 —— 认领故障的知识只该有一份，而后端是那个
        # **握着异常对象**的一方。
        title={"zh": "执行超时", "en": "The run timed out"},
        body={
            "zh": "这次执行等待响应超时，已停止。",
            "en": "This run stopped after waiting too long for a response.",
        },
        recovery={
            "zh": "中断前记录的内容都在下方，发送新消息即可继续。",
            "en": "Work recorded before the interruption is below. Send a new message to continue.",
        },
    ),
    "upstream_unavailable": _Copy(
        # 点名"模型服务"：2026-08-20 实测（积算网关 ReadTimeout），这类失败
        # 顶着"平台内部错误"的文案过河，调度器跟着转述成"框架错误"，用户的
        # 怒火全部指向平台自己 —— 而平台和研究这次一件事都没做错。
        title={"zh": "模型服务不可用", "en": "The model service is unavailable"},
        body={
            "zh": "模型服务商拒绝或中断了这次请求。",
            "en": "The model provider refused or dropped the request.",
        },
        recovery={
            "zh": "已记录的内容都还在。点击「继续」或发送新消息即可从中断处继续。",
            "en": (
                "Everything recorded is still here. Click Continue or send a new message "
                "to continue from where it stopped."
            ),
        },
        retryable=True,
    ),
    "runtime_protocol_error": _Copy(
        title={"zh": "这次执行未完成", "en": "This run did not finish"},
        body={
            "zh": "平台内部通信出错。",
            "en": "An internal communication error occurred in the platform.",
        },
        recovery={
            "zh": "这是平台的问题，修改请求无法解决。反馈时请附上「技术细节」中的参考编号。",
            "en": (
                "This is a platform problem that changing the request will not fix. "
                "Include the reference number under Technical details when you report it."
            ),
        },
    ),
    "pause_pending": _Copy(
        # 这条不是故障 —— 它是**守卫成功了**：run 正停在一个决策点上等人，
        # 有别的东西又推了一次。研究本身既没失败也没停在半路。
        #
        # 掉进 `execution_failed` 时用户读到的是"平台内部错误 / 请重新发送"，
        # 三样全错：不是内部错误（是流程重入），活儿没丢（已产出且已过审），
        # 重发只会把整件事重跑一遍。
        title={"zh": "这次执行在等你做决定", "en": "This run is waiting for your decision"},
        body={
            "zh": "有一个问题在等你回答，回答之前无法开始新的执行。",
            "en": "A question is waiting for your answer; a new run cannot start until you answer it.",
        },
        recovery={
            "zh": "回答这个问题后即可继续。",
            "en": "Answer it to continue.",
        },
        # 不答复就重发，结果一模一样 —— 这是真结论，不是"不知道"。
        retryable=False,
    ),
    "no_pause_to_answer": _Copy(
        # 守卫成功：人点的那张卡背后已经没有停着的 pause（答过了 / 跑起来了 /
        # 后端换过）。不是故障，重发同一个选择只会再撞一次。
        title={"zh": "这个问题已无需回答", "en": "This question no longer needs an answer"},
        body={
            "zh": "这张卡片显示后，执行已经继续了。",
            "en": "The run has moved on since this card was shown.",
        },
        recovery={
            "zh": "请刷新查看会话的最新状态；如果有新的卡片，请回答新的那张。",
            "en": "Refresh to see the Session's latest state, and answer the new card if there is one.",
        },
        retryable=False,
    ),
    "offer_superseded": _Copy(
        # 守卫成功：答复指向的是上一次呈递，运行时此刻停在一次新的呈递上
        # （2026-09-09 node20：observation 重跑后是新的一张卡，人点的是旧的）。
        title={"zh": "这张卡片已过期", "en": "This card is out of date"},
        body={
            "zh": "在你回答之前，又出现了一个新的问题。",
            "en": "A newer question appeared before you answered this one.",
        },
        recovery={
            "zh": "请回答最新的卡片。",
            "en": "Answer the latest card.",
        },
        retryable=False,
    ),
    "request_too_large": _Copy(
        title={"zh": "请求内容过大", "en": "The request is too large"},
        body={
            "zh": "这次发送的内容超过了单条消息的大小上限。",
            "en": "This message exceeds the size limit for a single message.",
        },
        recovery={
            "zh": "请分成几条发送，或把长内容作为文件上传。",
            "en": "Split it into several messages, or upload long material as a file.",
        },
        # 同一条请求再发一次必然还是太大 —— 这里的 False 是真结论，不是"不知道"。
        retryable=False,
    ),
}

# harness 侧对同一类事实用的类别名是 `provider_unavailable`（executor 的
# failure_category 词表，兼容性硬约束不能改）；worker/backend 声明的 code 是
# `upstream_unavailable`。同一件事两个名字，各自都到得了这里 —— 都指向同一份
# 文案，而不是让其中一个名字掉进"平台内部错误"的兜底。
_COPY["provider_unavailable"] = _COPY["upstream_unavailable"]

# 这里从前有一行 `_COPY["project_busy"] = _COPY["session_busy"]` —— 同一件事
# 两个名字。2026-08-23 随那三处 raise 一起删了：`session_busy` 那一维（会话
# 被占用）不再是拒收理由，产生它的代码路径没有了，名字也跟着走。
#
# 剩下的 `project_busy` 是 worker 抢不到 flock 时**自己声明**的 code —— 跨进程
# 那头也是这个名字，所以没有换代期的别名要留。少一张别名表，就少一处会分叉
# 的抄件。
#: 异常类型 → code。只在异常自己没带**我们认得的** `code` 时才用。
#:
#: 按**类型**认而不是按消息文本认：类型是异常自己声明的身份，消息文本是给人看的
#: 散文，随时会被上游改。
_BY_TYPE: tuple[tuple[str, str], ...] = (
    # (类型全名的片段, code)。用字符串匹配类型名而不是 import 那些类 ——
    # 这里不该为了认一个错误名字去 import sqlalchemy / asyncpg / harness 的内部。
    ("IntegrityError", "storage_conflict"),
    ("UniqueViolationError", "storage_conflict"),
    # `ProjectBusyError` 只存在于 **worker 进程**（platform_runtime）。它跨进程
    # 到达这里时是一个 `HarnessSessionError`，MRO 是
    # HarnessSessionError → RuntimeError → Exception —— 按 `type(exc).__mro__`
    # 找这个名字**永远找不到**。2026-08-21 之前这条判据是死的，等于不存在。
    #
    # 修法不是删掉它，是让判据够得着它真正到达的地方：异常自己带着
    # `error_type`（worker 出海时写的 `type(exc).__name__`），`_classify` 现在
    # 连同 MRO 一起认。跨进程的类型身份从此和进程内的一样有效。
    ("ProjectBusyError", "project_busy"),
    ("TimeoutError", "harness_operation_timeout"),
    ("JSONDecodeError", "runtime_protocol_error"),
)

#: 上游瞬时故障。判据从**我们自己生成的错误格式**里取 —— `core/llm.py` 抛的是
#: `"LLM API HTTP <code>: <body>"`，那个状态码是我们自己写进去的，不会随 provider
#: 改文案而失效。去猜 provider 的措辞（"concurrency limit exceeded"…）等于给每一家
#: 上游各维护一份名单，换一家就默认漏过。
_UPSTREAM_HTTP_RE = re.compile(r"HTTP (\d{3})")
_RETRYABLE_UPSTREAM_STATUS = {429, 500, 502, 503, 504}

#: 上游**明确拒绝**。和上面那组是两件事，不能并进同一个集合：那组说"上游此刻
#: 不行，等等再来"，这组说"上游看懂了并且不给"，重发不会改变任何东西。
#:
#: 2026-08-22 之前这一类**在整条判据链上没有任何归属** —— `_is_transient_upstream`
#: 返 False，于是 403 和一条 SQLAlchemy 崩溃走同一个兜底。「上游拒绝」和
#: 「我们认不出这是什么」被当成了一件事，代价是给用户一句反向的行动建议。
_REJECTED_UPSTREAM_STATUS = {401, 402, 403}

#: **按内容认**的那条产出路径，写在一处。
#:
#: `_COPY` 的条目一共有三种到达方式：有人声明这个 code、`_BY_TYPE` 按异常类型
#: 认、以及这里 —— 从我们自己写进错误串的 HTTP 状态码认。前两条
#: `test_every_copy_entry_is_reachable` 都点得到名，第三条以前它不知道，于是
#: `upstream_unavailable` 只是**碰巧**因为 harness 侧也用了同一个名字才没被判死。
#: 碰巧不是判据：把这条路径显式化，护栏才扫得到它。
_BY_UPSTREAM_STATUS: tuple[tuple[frozenset[int], str], ...] = (
    (frozenset(_RETRYABLE_UPSTREAM_STATUS), "upstream_unavailable"),
    (frozenset(_REJECTED_UPSTREAM_STATUS), "upstream_rejected"),
)


def _upstream_status_codes(detail: str) -> tuple[int, ...]:
    """从**我们自己写的**错误格式里取状态码（`core/llm.py` 的 `LLM API HTTP <code>`）。

    不去猜 provider 的措辞（"quota is exhausted" / "insufficient_quota" /
    "欠费"…）—— 那等于给每一家上游各维护一份名单，换一家就默认漏过。
    """
    codes: list[int] = []
    for match in _UPSTREAM_HTTP_RE.finditer(detail or ""):
        try:
            codes.append(int(match.group(1)))
        except ValueError:
            continue
    return tuple(codes)


def _is_transient_upstream(detail: str) -> bool:
    return any(code in _RETRYABLE_UPSTREAM_STATUS for code in _upstream_status_codes(detail))


#: POSIX 把"被信号杀死"表达成负的退出码（`-signum`）。SIGTERM=15 是**有人
#: 要它停**，其余（实测全是 SIGKILL=9）是**被强杀**——用户能做的事完全不同。
_SIGTERM = 15


def _exit_code_answer(exc: BaseException) -> str | None:
    """进程退出码说得出这是哪一种"退出"吗？说不出就返回 None（别猜）。"""
    exit_code = getattr(exc, "exit_code", None)
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    if exit_code == 0:
        return "harness_process_exited"
    if exit_code < 0:
        return (
            "harness_process_signalled" if -exit_code == _SIGTERM
            else "harness_process_killed"
        )
    # 正的非零退出码 = 进程自己以错误码退出，它的 stderr 才是答案；
    # 那条路归既有的类型/内容判据，这里不抢。
    return None


def _classify(exc: BaseException, raw: str) -> str:
    # 进程退出码排在**声明的 code 之前**：`HarnessSessionProcessError` 声明的
    # `harness_process_exited` 说的是**家族**（"执行进程退出了"），而它自己带着
    # 的退出码说的是**哪一种**（自己走的 / 被要求停的 / 被强杀的）。粗的那个
    # 先 return，细的那个就永远跑不到 —— 这正是 2026-09-16 普查里 12 次退出
    # 共用一句「发下一条消息就从断点接着跑」的成因，而对 SIGKILL 那四条它是
    # 假承诺。
    #
    # 不会抢别人的活：只有真带着整数 `exit_code` 的异常才答得出来。
    signalled = _exit_code_answer(exc)
    if signalled is not None:
        return signalled
    declared = getattr(exc, "code", None)
    if isinstance(declared, str) and declared in _COPY:
        return declared
    # 运行时声明了一个我们还没写文案的 code（比如 `runtime_error`）——
    # 那只说明**它的名字**我们不认得，不说明这件事我们判不出来。以前这里
    # 直接 return 通用文案，于是下面那两道判据（异常类型 / HTTP 状态码）
    # 一次都跑不到。
    #
    # 2026-08-17 实测：GPUStack 返 503，异常带着 `code="runtime_error"` ——
    # `_is_transient_upstream` 本来一眼认得出这是上游不可用，却被这个提前
    # return 挡在外面。用户读到的是"平台内部错误 / 记录这个 run 时出错"：
    # 不是内部错误（是模型服务挂了）、也不在记录阶段、"quote the reference
    # 报给我们"更是把人指向了错的方向。
    #
    # 声明的 code 仍然照原样进记录（见 `describe` 的 code 字段），这里只决定
    # **文案**：认不出名字就继续按内容判，别提前收工。
    for type_marker, code in _BY_TYPE:
        if type_marker in _exception_type_names(exc):
            return code
    # 按内容认：走那张表，不再各写一条 if —— 表和判据分成两处必然分叉，
    # 而分叉时两边都不报错。
    statuses = _upstream_status_codes(raw)
    for accepted, code in _BY_UPSTREAM_STATUS:
        if any(status in accepted for status in statuses):
            return code
    # 登记过「故意走兜底」的不吵；两张表都没有的才是漏网 —— 吵，而且闸会红。
    if isinstance(declared, str) and declared and declared not in _FALLS_TO_GENERIC:
        # 运行时**说得出**这是什么，而我们一句对应的话都没有 —— 于是用户读到
        # 「平台内部错误」，而平台其实被明确告知了原因。这不是判决（兜底文案
        # 照发，用户该看到什么不变），是**留证据**：漏掉的名字必须在日志里
        # 有名有姓，否则下一个 `project_busy` 还要靠一次事故才被发现。
        #
        # 为什么不做成静态"每个 code 都必须有文案"的闸：`_COPY` 的加条目判据是
        # 「用户能做的事和别的不一样」，强求全覆盖只会逼出一堆同义文案。
        # 覆盖不覆盖是产品判断，**漏了没人知道**才是缺陷。
        logger.warning(
            "run failure code %r has no user-facing copy; fell back to %r "
            "(exc_type=%s error_type=%r)",
            declared, _GENERIC, type(exc).__name__, getattr(exc, "error_type", ""),
        )
    return _GENERIC


def _exception_type_names(exc: BaseException) -> tuple[str, ...]:
    """这个异常声称自己是什么类型 —— 进程内的 MRO **加上**跨进程带过来的名字。

    只看 `type(exc).__mro__` 会漏掉半个系统：worker 是另一个进程，它那边的
    `ProjectBusyError` 到达这里时一律是
    `HarnessSessionError`，真实身份在 `error_type` 字段里（worker 出海时写的
    `type(exc).__name__`）。MRO 里找不到它们不是因为判据没写，是因为**判据只
    看了一半的证据**。
    """
    names = [klass.__name__ for klass in type(exc).__mro__]
    declared_type = getattr(exc, "error_type", "")
    if isinstance(declared_type, str) and declared_type:
        names.append(declared_type)
    return tuple(names)


def _sanitize(text: str) -> str:
    """去掉密钥。逐词过一遍 —— 整串一次性过会漏掉夹在句子里的令牌。"""
    whole = DEFAULT_REDACTION_POLICY.sanitize({"message": text}).value
    cleaned = str(whole.get("message") or "") if isinstance(whole, dict) else str(text)
    return "".join(
        str(DEFAULT_REDACTION_POLICY.sanitize(part).value) if part and not part.isspace() else part
        for part in re.split(r"(\s+)", cleaned)
    )


@dataclass(frozen=True)
class UserFacingFailure:
    """一条 run 失败对用户的完整说法。

    `title` / `body` / `recovery` 一律来自 `_COPY`；`detail` 是唯一装原始文本的
    地方，前端把它放进「Technical details」折叠区。构造上就没有第二条路让
    `str(exc)` 走到正文里去。
    """

    code: str
    title: str
    body: str
    recovery: str
    retryable: bool | None
    reference: str
    detail: str
    #: 底层异常原文（worker 错误事件的 `cause`）。和 `detail` 一样只进「技术细节」，不进正文。
    cause: str = ""

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "code": self.code,
            "title": self.title,
            "body": self.body,
            "recovery": self.recovery,
            "reference": self.reference,
            "detail": self.detail,
            # `message` 保留：老 run 的 summary 里只有它，前端对两代数据都要能
            # 渲染。**内容是人话**（title + body），不是异常原文 —— 老字段名
            # 继续存在，但装的东西已经换成对的了。
            "message": f"{self.title}. {self.body}",
        }
        if self.cause:
            record["cause"] = self.cause
        if self.retryable is not None:
            # 字段**在不在**本身就是信息：不在 = 我们没有结论。写一个 False
            # 进去等于把"不知道"伪装成"知道"。
            record["retryable"] = self.retryable
        return record


def _say(phrase: dict[str, str], lang: str) -> str:
    """缺哪种语言就退回中文 —— 少一句翻译不该让一条失败说不出话来。"""
    return phrase.get(lang) or phrase["zh"]


def copy_for(code: str, lang: str = "zh") -> dict[str, object]:
    """一类已知结论对用户的说法，整份摊开 —— 给不是从异常来的拒绝用（入口 409）。

    前端按 `title/body/recovery/retryable` 认结构化失败（`presentStructuredChatFailure`）；
    这里给的形状与 `describe()` 一致，文案只在 `_COPY` 一份。
    """
    copy = _COPY[code]
    return {
        "code": code,
        "title": _say(copy.title, lang),
        "body": _say(copy.body, lang),
        "recovery": _say(copy.recovery, lang),
        "retryable": copy.retryable,
    }


def describe(
    exc: BaseException,
    *,
    reference: str = "",
    known_cause: str | None = None,
    lang: str = "zh",
) -> UserFacingFailure:
    """把一个内部异常翻译成用户读得懂的失败。

    `reference` 传 run id —— 用户手里有它，日志里才找得到真正的东西。

    `known_cause` 给**调用方比异常本身更清楚原因**的情况：子进程被 SIGTERM 掐掉
    时只看得见一个 `-15`，而"这是我们自己重启造成的"这个事实只有 App Server
    有。异常说不出来的，让知道的人说。
    """
    raw = _sanitize(str(exc) or type(exc).__name__)
    code = known_cause if known_cause in _COPY else _classify(exc, raw)
    copy = _COPY[code]
    declared = getattr(exc, "code", None)
    truncated = _say({"zh": " …（已截断）", "en": " … (truncated)"}, lang)
    detail = raw if len(raw) <= _DETAIL_MAX_CHARS else raw[:_DETAIL_MAX_CHARS] + truncated
    retryable = copy.retryable
    if _is_transient_upstream(raw) and retryable is None:
        retryable = True
    if isinstance(getattr(exc, "retryable", None), bool):
        # 异常自己说了能不能重试就听它的 —— 它比文案表更懂这一次发生了什么。
        retryable = bool(exc.retryable)
    return UserFacingFailure(
        # `code` 记异常自己声明的那个（哪怕还没有专属文案），这样日志和文案
        # 可以各自演化，不互相绑架。但 `known_cause` 优先 —— 调用方说得出
        # 原因时它就是最权威的那份，让 code 和文案指向同一件事。
        code=(
            known_cause if known_cause in _COPY
            else str(declared) if isinstance(declared, str) and declared
            else code
        ),
        title=_say(copy.title, lang),
        body=_say(copy.body, lang),
        recovery=_say(copy.recovery, lang),
        retryable=retryable,
        reference=reference,
        detail=detail,
        cause=_cause_text(exc),
    )


def _cause_text(exc: BaseException) -> str:
    """异常带着的底层原因（`HarnessSessionError.cause`），过一遍同一个清洗、同一个上限。"""
    cause = getattr(exc, "cause", None)
    if not isinstance(cause, str) or not cause.strip():
        return ""
    text = _sanitize(cause)
    return text if len(text) <= _DETAIL_MAX_CHARS else text[:_DETAIL_MAX_CHARS] + " …"
