"""工具失败的**性质**：谁的锅，人要不要看。

一句话判据：**这次失败是"框架按设计说不"，还是"我们的代码崩了"？**

前者是 ReAct 循环的正常一步 —— 模型调错了/时机不对，工具带着指导驳回，
下一轮它改对。人对此做不了任何事，也不该在界面上看到红色。后者是缺陷：
模型改多少次参数都没用，它只能绕开，绕不过就永久丢掉这个能力。

判据必须在**产生失败的那一层**定下来，不能留给下游猜。此前平台没有这个
字段，前端只好按字符串长相猜（"含 snake_case 就算技术细节"），实测把 87%
的错误正文换成了一句"The tool stopped before producing a usable result."——
包括那些写得最好、最该被看到的。

  ⚠️ 这些常量会进事件流并被前端按值分支。改名 = 改契约。
"""

# 框架按设计拒绝了这次调用（契约不满足 / 护栏 / 前置缺失 / 权限）。
# 正常的 ReAct 一步，不是事故。
REJECTED = "rejected"

# 工具代码抛了未捕获异常 —— 我们的 bug。
TOOL_EXCEPTION = "tool_exception"

# 工具没注册 / 缺必填参数 / 返回值不合规范：调用侧或工具契约的问题，
# 模型改调用方式就能过，因此归 rejected 家族但保留可分辨的 code。
NOT_REGISTERED = "tool_not_registered"
MISSING_PARAMETERS = "missing_parameters"
CAPABILITY_DENIED = "capability_denied"
NON_DICT_RESULT = "non_dict_result"

# 子进程/外部命令非零退出：不是我们崩了，也不完全是模型调错。
COMMAND_FAILED = "command_failed"

# 这台机器上**根本没有**干这件事所需的外部程序（编译器 / 求解器 / 转换器）。
#
# 这一类必须与 COMMAND_FAILED 分开，因为**下一步该谁做**完全不同：命令非零退出
# 是模型能改的（改源码、改参数），工具不在场则模型改多少次都没用 —— 要人去装。
# 2026-09-09 实测：writing 连撞 3 轮 `bwrap: execvp latexmk: No such file or
# directory`，界面统一显示"LaTeX 编译失败 / Review the document source"，于是
# 模型三轮都在查一份完全正确的稿子。归属被吞掉的形状同
# [[项目-模型服务故障的归属]]：**不是研究出问题，也不是我们的代码崩了**。
#
# 因此它**不进** LOOP_LEVEL_CODES：这不是 ReAct 的正常一步，是这台机器缺东西。
TOOLCHAIN_MISSING = "toolchain_missing"

# 模型服务侧的故障/限额（断流、429、上下文超限）。要人看，但**不是我们的代码
# 崩了** —— 记成 tool_exception 会让它混进 bug 清单，而它该去的是"这次请求太大 /
# 对面挂了"。是谁的锅要在能分辨的那一层说清楚（见 [[项目-模型服务故障的归属]]）。
PROVIDER_ERROR = "provider_error"

#: 属于"循环自纠"、默认不摆到人面前的 code。
LOOP_LEVEL_CODES = frozenset({
    REJECTED, NOT_REGISTERED, MISSING_PARAMETERS, CAPABILITY_DENIED,
})

#: 本模块定义的**全部** code。显示层各有一份路由表，两份词表必须相等 ——
#: 上游加一个 code、下游没跟上，那个 code 会静默落进兜底分支，正是这次要修的
#: 病（见 tests/test_tool_error_vocabulary_is_one_table.py）。
ALL_CODES = frozenset({
    REJECTED, TOOL_EXCEPTION, NOT_REGISTERED, MISSING_PARAMETERS,
    CAPABILITY_DENIED, NON_DICT_RESULT, COMMAND_FAILED, TOOLCHAIN_MISSING,
    PROVIDER_ERROR,
})


class ToolRejection(Exception):
    """**故意**用 raise 实现的驳回（不是 bug）。

    绝大多数驳回应该 `return {"status": "error", ...}`。但有些护栏埋在深层
    工具函数里（路径边界、冻结产物），那里 raise 比一路往上传返回值现实。
    继承这个类，dispatch 就知道它是"说不"而不是"崩了"——否则消息会带上
    `TypeError:` 这种前缀，读起来像代码错误，实测本机 39 条"真异常"里有
    19 条其实是这类故意的拒绝。
    """


def command_failure_note(returncode: int | None, stderr_tail: str | None) -> str:
    """非零退出的一句人话摘要，放进 envelope 的 `error`。

    子进程类工具（run_bash / execute_python）历来把失败写在 `returncode` +
    `stderr_tail` 里，envelope 没有 `error` —— 下游只好兜底成
    "Tool execution failed"，stderr 整个丢掉。本机库里 27 条就是这么来的。
    工具面向模型的返回不用改（它读得懂结构化字段），补的是给人看的那一句。
    """
    tail = (stderr_tail or "").strip().splitlines()
    last = next((line.strip() for line in reversed(tail) if line.strip()), "")
    head = f"命令退出码 {returncode}"
    return f"{head}：{last[:300]}" if last else f"{head}（stderr 为空，见 stdout）"
