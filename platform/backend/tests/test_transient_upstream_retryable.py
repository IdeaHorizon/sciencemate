"""上游瞬时故障导致的失败要如实标成可重发。

node20 实测：GPUStack 回 429（"Concurrency limit exceeded for user, please
retry later"），整个 run 被记成普通 failed，前端只显示"没有可用回复"，用户
看不出这到底是"我的请求有问题"还是"对面忙，等会儿再来就行"。

而失败信息自己就写着 **before an agent response was produced** —— 一个字都
没产出，重发同一条请求必然安全。这个事实是现成的，只是没被标出来。

边界：只标 `retryable`，**不动 `resumable`**。后者管的是 harness 子进程能不能
接着跑，这次失败之后那个进程的状态不可信；谎报会把恢复机制指向一个死进程。
"""

import pytest

from app.services.local_execution import _sanitized_platform_failure


def _failure(message: str) -> dict:
    return _sanitized_platform_failure(RuntimeError(message))


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_upstream_status_is_retryable(status):
    failure = _failure(f"LLM API HTTP {status}: upstream said no")
    assert failure["retryable"] is True


def test_node20_concurrency_limit_is_retryable():
    """现场原文回放。"""
    failure = _failure(
        'LLM API HTTP 429: {"error":{"message":"Concurrency limit exceeded '
        'for user, please retry later","type":"rate_limit_error"}}'
    )
    assert failure["retryable"] is True
    # 这里断言的是**事实**，不是某一句措辞：用户要能看出「对面忙」而不是
    # 「我的请求有问题」，并且知道一个字都没产出所以重发安全。
    #
    # 原来断的是 `"before an agent response was produced" in message` ——
    # 那是当时拼字符串的产物。文案现在集中在 `run_failures._COPY`，把散文
    # 逐字复刻进测试等于把文案冻在测试里，改一个错别字都要红一次。
    assert failure["code"] == "upstream_unavailable"
    # 上面那句"不逐字复刻散文"当时只做到一半：`"provider" in body` /
    # `"again" in recovery` 仍然是**措辞**断言，只是换成了英文关键词 ——
    # 2026-08-20 把这条文案改写成中文（点名"是模型服务的问题，不是平台"）
    # 就红了，而用户可见的事实一个都没变坏。
    #
    # 这里守的是**路由**：这次失败被判成了 upstream_unavailable 那一条，
    # 且如实标了可重发。文案说了什么由文案表那份测试守
    # （test_failures_reach_the_user_as_product_copy 里的
    # `test_provider_failure_names_the_model_service`）—— 一个问题一个真相源，
    # 两处各断一半就会在改文案时同时红两处、又都不是真问题。
    # 而原始的 429 正文仍然留得住 —— 只是在折叠区，不在正文。
    assert "429" in failure["detail"]


@pytest.mark.parametrize("status", [400, 404, 422])
def test_unclassified_client_errors_stay_unmarked(status):
    """认不出这是哪一类 4xx 就不表态（fail-closed）——不能给用户假希望。

    2026-08-22 从这条的参数里拆走了 401/403：它们不再是"不知道"，见下一条。
    留下的这几个仍然是第三态 —— 我们说不出「同一条请求重发会不会不一样」。
    """
    failure = _failure(f"LLM API HTTP {status}: bad request")
    assert "retryable" not in failure


@pytest.mark.parametrize("status", [401, 402, 403])
def test_upstream_rejection_is_a_real_no_not_an_unknown(status):
    """上游**明确拒绝**（额度/凭据）：这次我们真的知道，而且知道的是 False。

    「不知道」和「知道重发没用」在界面上是两种完全不同的东西：
    前者渲染成一行灰字「发下一条消息接着跑」，后者才会摆出"现在能做什么"
    （换后端 / 处理额度）。2026-08-22 现场，403 落在前者上 —— 用户照着那句话
    点「继续」，5 秒后又是同一条。

    所以这里从三态里的"缺省"升级成明确的 `False`。判据是它满不满足 False 的
    定义：**同一个空额度、同一个密钥，重发一万次都是这个结果** —— 满足。
    """
    failure = _failure(f"LLM API HTTP {status}: nope")
    assert failure["code"] == "upstream_rejected"
    assert failure["retryable"] is False


def test_unknown_failures_stay_unmarked():
    """判不出来就不标（fail-closed）：用户照旧可以自己重发，但我们不承诺。"""
    failure = _failure("something exploded in a way we do not recognise")
    assert "retryable" not in failure


def test_explicit_exception_attribute_still_wins():
    """异常自己说了算 —— 不能被这条推断覆盖掉。"""

    class _Exc(RuntimeError):
        retryable = False

    failure = _sanitized_platform_failure(_Exc("LLM API HTTP 429: busy"))
    assert failure["retryable"] is False


def test_resumable_is_not_touched_here():
    """这层只产 failure dict，绝不声称 harness 会话可续。"""
    failure = _failure("LLM API HTTP 429: busy")
    assert "resumable" not in failure
