"""429 要按"并发位被别人占着"的量级退避，不是按"服务端打了个嗝"。

node20 实测：zju GPUStack 回
  {"message":"Concurrency limit exceeded for user, please retry later"}
整个 run 当场判死。查下来那个时间段平台只有这 1 个 run —— 不是我们自己并发
太多，是实验室里别人在用同一个账号，他们一次几十秒到几分钟的生成占着并发位。

而我们的退避是 1s/2s/4s，三次加起来等 7 秒。7 秒对上几分钟，必然三次全撞墙。

所以 429 单独一套参数：更长的退避（5/10/20/40/80s，封顶 120s）+ 更多轮次；
5xx / 网络抖动维持原样（秒级重试确实够）。
"""

import pytest

from core import llm


class _Resp:
    def __init__(self, retry_after=None):
        self.headers = {"retry-after": retry_after} if retry_after else {}


# ── 退避时长 ──────────────────────────────────────────────────────────────


def test_both_transient_families_wait_on_the_same_time_scale():
    """429 和 5xx 现在同量级 —— 这条断言 2026-08-10 被推翻过一次。

    原来这里断言 `limited > ordinary * 3`，背后的信念是"5xx 只是服务端打了个
    嗝，秒级重试就够"。**那个信念错了**：5xx 的真实成因是节点重启 / 模型
    重载 / OOM 后拉起，和"并发位被别人的长生成占着"是同一个时间尺度。

    代价是实测出来的：一轮跑了 3 小时、做完 3 个真实 LAMMPS 生产模拟的 E2E，
    死在后端一次 HTTP 503 上 —— 当时 5xx 的阶梯是 1/2/4，总共扛 7 秒。
    事后直接探那个端点：200，2.1 秒，回 pong。它只是短暂不可用。
    """
    ordinary = llm._compute_backoff(0, 1.0, None, rate_limited=False)
    limited = llm._compute_backoff(0, 1.0, None, rate_limited=True)
    assert ordinary >= 5.0, "5xx 起步不能再回到秒级"
    assert limited >= 5.0
    # 仍有差别，但在**上限**而不是起步：并发位可能被占几分钟。
    assert llm._RATE_LIMIT_MAX_BACKOFF_SECONDS > llm._TRANSIENT_MAX_BACKOFF_SECONDS


def test_rate_limited_backoff_grows_and_is_capped():
    delays = [llm._compute_backoff(i, 1.0, None, rate_limited=True) for i in range(8)]
    assert delays == sorted(delays)                       # 单调不降
    assert delays[3] >= 40.0                              # 第 4 次已到几十秒量级
    assert max(delays) <= llm._RATE_LIMIT_MAX_BACKOFF_SECONDS


def test_ordinary_errors_can_ride_out_a_backend_restart():
    """5xx 累计要扛得过一次后端重启。

    这条测试原名 `test_ordinary_errors_keep_the_old_short_backoff`，断言
    `delays[0] < 5.0` —— 保护的正是那个被证伪的信念。**保留这段历史**：
    下次有人觉得"5xx 等太久了"，先看这里为什么当初等太短。
    """
    delays = [llm._compute_backoff(i, 1.0, None, rate_limited=False) for i in range(6)]
    assert delays == sorted(delays)
    assert max(delays) <= llm._TRANSIENT_MAX_BACKOFF_SECONDS
    assert sum(delays[:5]) >= 100.0, "扛不过一次节点重启，等于没重试"


# ── Retry-After ───────────────────────────────────────────────────────────


def test_retry_after_is_honoured_beyond_the_old_30s_ceiling():
    """provider 说等 60 秒，原来卡在 30s 上限 —— 30 秒就冲上去等于没听。"""
    assert llm._compute_backoff(0, 1.0, "60", rate_limited=True) == 60.0


def test_retry_after_still_capped_at_the_rate_limit_ceiling():
    delay = llm._compute_backoff(0, 1.0, "99999", rate_limited=True)
    assert delay == llm._RATE_LIMIT_MAX_BACKOFF_SECONDS


def test_garbage_retry_after_falls_back_to_computed_backoff():
    """HTTP-date 形式或垃圾值不能让退避崩掉。"""
    delay = llm._compute_backoff(0, 1.0, "Wed, 21 Oct 2026 07:28:00 GMT",
                                 rate_limited=True)
    assert delay >= 5.0


# ── 重试轮数 ──────────────────────────────────────────────────────────────


def test_rate_limited_gets_more_attempts():
    assert llm._effective_max_retries(3, rate_limited=True) >= 5


def test_ordinary_errors_keep_their_budget():
    """次数上限尊重调用方 —— 放宽只体现在**默认值**上，不偷改传进来的数。"""
    assert llm._effective_max_retries(3, rate_limited=False) == 3


def test_disabled_retry_stays_disabled():
    """显式设 0 = 关掉重试，429 也不许偷偷把它打开。"""
    assert llm._effective_max_retries(0, rate_limited=True) == 0


# ── 走真入口：429 真的会被重试到成功 ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_retries_429_then_succeeds(monkeypatch):
    """回放 node20 现场：先连着 429，随后放行 → 不应该整轮判死。"""
    calls = {"n": 0}

    class _R:
        def __init__(self, status, body="", headers=None):
            self.status_code = status
            self.text = body
            self.headers = headers or {}

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            # 连着 5 次 429：**老代码只有 4 次机会**（range(max_retries+1)），
            # 必然放弃。新代码把 429 的预算放大到 >=5 次重试才等得到这一下。
            if calls["n"] <= 5:
                return _R(429, '{"error":{"message":"Concurrency limit exceeded"}}')
            return _R(200)

    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(llm.asyncio, "sleep", _sleep)

    client = llm.LLMClient(api_key="k", base_url="http://x", model="m")
    data = await client._post_with_retry({}, timeout=1.0, max_retries=3)

    assert data["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 6          # 老代码在第 4 次就放弃了
    assert all(s >= 5.0 for s in slept), f"429 退避太短：{slept}"


@pytest.mark.asyncio
async def test_non_retryable_4xx_still_fails_fast(monkeypatch):
    """400/401 不该被这套放大策略拖成几分钟。"""
    calls = {"n": 0}

    class _R:
        status_code = 401
        text = "unauthorized"
        headers: dict = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            return _R()

    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    client = llm.LLMClient(api_key="k", base_url="http://x", model="m")
    with pytest.raises(RuntimeError, match="401"):
        await client._post_with_retry({}, timeout=1.0, max_retries=3)
    assert calls["n"] == 1


# ── 限的到底是什么，provider 说了，我们得留下来（issue #490）──────────────────

def test_a_rate_limit_reason_is_parsed_not_just_the_status():
    """429 只记 "429" 时，分不清限的是 TPM、RPM 还是并发位 —— 三种处置不同。"""
    detail = llm.parse_provider_error(429, (
        '{"error": {"type": "rate_limit_exceeded", "code": "tpm_limit", '
        '"message": "Requested 12000 tokens, limit 8000 TPM"}}'), retry_after="30")

    assert detail["status"] == 429
    assert detail["error_type"] == "rate_limit_exceeded"
    assert detail["error_code"] == "tpm_limit"
    assert "8000 TPM" in detail["error_message"]
    assert detail["retry_after"] == "30"


def test_the_gateway_shape_is_parsed_too():
    """GPUStack 那种顶层 message/code 的形状（node20 实测原文）也要认。"""
    detail = llm.parse_provider_error(429, (
        '{"message": "Concurrency limit exceeded for user, please retry later", '
        '"code": 429}'))

    assert "Concurrency limit exceeded" in detail["error_message"]
    assert detail["error_code"] == "429"


def test_an_unparseable_body_degrades_instead_of_guessing():
    """解析不了就只留状态码 + 截断原文，不猜。"""
    detail = llm.parse_provider_error(503, "<html>502 Bad Gateway</html>")
    assert detail["status"] == 503
    assert "Bad Gateway" in detail["error_message"]
    assert "error_type" not in detail


def test_the_error_carries_the_reason_the_model_and_a_size_estimate():
    """异常自己带着这些字段 —— 上层不必再去正则解析人话。"""
    exc = llm.LLMHTTPError(
        429, "LLM API HTTP 429: busy",
        body='{"error": {"type": "rate_limit_exceeded", "code": "rpm_limit"}}',
        retry_after="12", model="glm-5.1",
        payload={"messages": [{"role": "user", "content": "x" * 4000}]})

    assert exc.detail["error_type"] == "rate_limit_exceeded"
    assert exc.detail["model"] == "glm-5.1"
    assert exc.detail["est_prompt_tokens"] > 500, "限流常和这次请求多大直接相关"
    rendered = llm.describe_provider_error(exc)
    assert "rate_limit_exceeded" in rendered and "rpm_limit" in rendered
    assert "12" in rendered
    # 老消息格式逐字不变（平台侧 run_failures 按文案解析）
    assert str(exc).startswith("LLM API HTTP 429: ")


def test_no_credentials_are_ever_parsed_into_the_record():
    """只解析 body 与 Retry-After，永远不碰请求头 —— API key 不进日志。"""
    detail = llm.parse_provider_error(
        401, '{"error": {"message": "Incorrect API key provided: sk-abc123"}}')
    # 我们不主动去别处捞凭据；body 里 provider 自己回显的内容按原样截断保留，
    # 但记录里不得出现任何我们自己持有的 header 字段。
    assert set(detail) <= {"status", "error_type", "error_code", "error_message",
                           "retry_after", "model", "est_prompt_tokens"}
