"""Data's reasoning controls on both supported shared-client interfaces.

The legacy client builds its payload internally. Keep its transport, retry,
parsing and cancellation behavior; extend only the request payload on a copy.
No shared client or dispatcher instance is modified.
"""
from copy import copy
import inspect

from core.llm import LLMClient


def reasoning_options(client, effort):
    return ({"reasoning_effort": effort} if effort and
            "reasoning_effort" in inspect.signature(client.chat).parameters else {})


def data_model_client(client):
    if type(client) is LLMClient and "reasoning_effort" not in inspect.signature(client.chat).parameters:
        adapted = _LegacyReasoningClient.__new__(_LegacyReasoningClient)
        adapted.__dict__ = client.__dict__.copy()
        return adapted
    return client


class _LegacyReasoningClient(LLMClient):
    async def chat(self, messages, *, reasoning_effort=None, **kwargs):
        call = copy(self)
        call._data_reasoning_effort = reasoning_effort
        if reasoning_effort:
            # The legacy cache key does not include this request parameter.
            kwargs["_no_cache"] = True
        return await super(_LegacyReasoningClient, call).chat(messages, **kwargs)

    def _data_payload(self, payload):
        effort = getattr(self, "_data_reasoning_effort", None)
        return {**payload, "reasoning_effort": effort} if effort else payload

    async def _stream_with_retry(self, payload, **kwargs):
        return await super()._stream_with_retry(self._data_payload(payload), **kwargs)

    async def _post_with_retry(self, payload, **kwargs):
        return await super()._post_with_retry(self._data_payload(payload), **kwargs)
