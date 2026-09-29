"""FastAPI middleware — request logging, correlation IDs, request body budget."""

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.core.logging import get_logger, request_id_var

logger = get_logger("app.http")

#: multipart 边界 + 表单字段的余量。粗筛用，不是业务上限。
_MULTIPART_ENVELOPE_ALLOWANCE = 1024 * 1024


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with timing and correlation ID."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())[:8]
        token = request_id_var.set(request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "%s %s → %d (%.0fms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
            )
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            logger.exception(
                "%s %s → 500 (%.0fms)", request.method, request.url.path, elapsed
            )
            raise
        finally:
            request_id_var.reset(token)


class RequestBodyCapMiddleware:
    """把请求体的**实际字节数**卡在单份材料上限内 —— 在 multipart 解析、落临时文件之前。

    为什么必须在中间件里而不是在端点里：`UploadFile = File(...)` 意味着
    Starlette 已经把整个 multipart body 收完（大于 1MB 就落到磁盘临时文件）
    才轮到端点函数执行。端点里再看体积只能省下入池和 Git 的功夫，省不下那
    3 GiB 的传输和落盘 —— 那不是"提前拒绝"，只是拒绝得比较客气。

    为什么不能只看 `Content-Length`：它是可选的。chunked 传输根本没有这个头，
    只按声明拒绝等于"不声明就不设防"。所以两种框架走同一个预算：声明超限的
    当场 413（一个字节都不收）；其余的在 `receive()` 边界上逐块计数，超过预算
    就在流中间 413，multipart 解析器与临时文件都收不到超出的那部分。

    判据不写路径名单：`material_max_bytes` 是这台部署上**任何**合法请求体的
    上限（上传是唯一大体积入口），所以这条闸对全应用一视同仁。写名单的话，
    以后新增一个上传入口就默认不设防，而且没人会发现。multipart 信封（边界、
    表单字段）另给余量；精确到文件本身字节数的判据在 `core.materials.place`。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        from starlette.exceptions import HTTPException

        maximum = settings.material_max_bytes + _MULTIPART_ENVELOPE_ALLOWANCE

        def detail(size: int) -> dict:
            return {
                "code": "request_body_too_large",
                "message": (
                    f"请求体超过本部署上限 {maximum} 字节。更大的数据集别走上传："
                    "让 data 节点在计算侧就地取用，或在部署上给这个项目挂一个数据集绑定。"
                ),
                "sizeBytes": size,
                "maxBytes": maximum,
            }

        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > maximum:
            logger.warning(
                "%s %s → 413 (declared %s bytes > cap %d)",
                scope.get("method"), scope.get("path"), declared, maximum,
            )
            response = JSONResponse(status_code=413, content={"detail": detail(int(declared))})
            return await response(scope, receive, send)

        received = 0
        response_started = False

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    logger.warning(
                        "%s %s → 413 (received %d bytes > cap %d, mid-stream)",
                        scope.get("method"), scope.get("path"), received, maximum,
                    )
                    raise HTTPException(413, detail(received))
            return message

        async def track_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            return await self.app(scope, bounded_receive, track_send)
        except HTTPException as exc:
            # 正常情况下 FastAPI 在解析请求时就把这个异常变成了 413 响应。这里
            # 是给在它的异常层之外读请求体的 ASGI 消费者兜底，让边界照样成立。
            if received <= maximum or response_started or exc.status_code != 413:
                raise
            response = JSONResponse(status_code=413, content={"detail": detail(received)})
            return await response(scope, receive, send)
