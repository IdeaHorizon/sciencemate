"""Bounded streaming transport for the two-message academic planning request."""
import asyncio
import json
import time

import httpx


async def request_completion(url, headers, body, diagnostics, *, read_timeout, total_timeout):
    started = time.perf_counter()
    payload = {**body, "stream": True, "stream_options": {"include_usage": True}}
    timeout = httpx.Timeout(connect=10, write=10, read=read_timeout, pool=5)
    try:
        async with asyncio.timeout(total_timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                # A compatibility fallback shares this request's deadline. Never
                # retry authentication/rate-limit errors here.
                for transport_attempt in range(2):
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        diagnostics.update(http_status=response.status_code,
                                           response_headers_elapsed=round(time.perf_counter()-started, 3))
                        if response.status_code >= 400:
                            await response.aread()
                            error = response.text.lower()
                            if (transport_attempt == 0 and response.status_code in {400, 422}
                                    and "stream" in error and any(term in error for term in
                                        ("unsupported", "not support", "unknown", "extra", "not permitted"))):
                                if "stream_options" in error:
                                    payload.pop("stream_options", None)
                                    diagnostics["stream_fallback"] = "without_stream_options"
                                else:
                                    payload = dict(body)
                                    diagnostics["stream_fallback"] = "nonstream"
                                continue
                            response.raise_for_status()
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            raw = await response.aread()
                            diagnostics.update(transport="json", response_bytes=len(raw))
                            return json.loads(raw)
                        diagnostics["transport"] = "sse"
                        content, usage, finish_reason = [], {}, None
                        reasoning_chars = content_chars = events = response_bytes = 0
                        done = False
                        last_data = asyncio.get_running_loop().time()
                        lines = response.aiter_lines().__aiter__()
                        while True:
                            # HTTP read timeouts are reset by keepalive bytes.
                            # Only real model output resets this separate timer.
                            try:
                                async with asyncio.timeout_at(last_data + read_timeout):
                                    line = await anext(lines)
                            except StopAsyncIteration:
                                break
                            except TimeoutError as exc:
                                diagnostics["timeout_phase"] = "stream_idle"
                                raise httpx.ReadTimeout("Model stream produced no new data") from exc
                            response_bytes += len(line.encode())
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                done = True
                                break
                            try:
                                event = json.loads(data)
                            except ValueError:
                                continue
                            if event.get("error"):
                                raise httpx.ReadError("Model stream returned an error event")
                            events += 1
                            if event.get("usage"):
                                usage = event["usage"]
                            for choice in event.get("choices") or []:
                                if choice.get("index", 0) != 0:
                                    continue
                                delta = choice.get("delta") or {}
                                text = delta.get("content") or ""
                                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                                if text or reasoning or choice.get("finish_reason"):
                                    last_data = asyncio.get_running_loop().time()
                                    diagnostics.setdefault("first_data_elapsed", round(time.perf_counter()-started, 3))
                                if text:
                                    diagnostics.setdefault("first_content_elapsed", round(time.perf_counter()-started, 3))
                                    content.append(text)
                                    content_chars += len(text)
                                reasoning_chars += len(reasoning)
                                if choice.get("finish_reason"):
                                    finish_reason = choice["finish_reason"]
                            diagnostics.update(stream_events=events, content_chars=content_chars,
                                               reasoning_chars=reasoning_chars)
                        if not done and not finish_reason:
                            raise httpx.ReadError("Model stream ended before completion")
                        diagnostics.update(response_bytes=response_bytes, stream_complete=True)
                        return {"choices": [{"finish_reason": finish_reason, "message": {"content": "".join(content)}}],
                                "usage": usage, "_reasoning_chars": reasoning_chars}
    except TimeoutError as exc:
        diagnostics["timeout_phase"] = "request_deadline"
        raise httpx.ReadTimeout("Model request exceeded its total deadline") from exc
