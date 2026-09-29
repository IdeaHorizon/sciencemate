"""StructuredFormatter 必须保留异常通道。

以前这个 formatter 只拼 key=value、把 record.exc_info 整个丢掉，于是全后端
每一处 logger.exception() 都只留一行标题。E2E v7 的后台执行挂掉后日志里
只有 "Local execution failed"，没有任何可定位的信息。
"""

import logging

from app.core.logging import StructuredFormatter


def _record_with_exception() -> logging.LogRecord:
    logger = logging.getLogger("test.exc")
    try:
        raise ValueError("boom-from-worker")
    except ValueError:
        return logger.makeRecord(
            logger.name, logging.ERROR, "", 0, "Local execution failed", (),
            __import__("sys").exc_info(),
        )


def test_exception_traceback_is_kept() -> None:
    out = StructuredFormatter().format(_record_with_exception())
    assert "Local execution failed" in out
    assert "ValueError: boom-from-worker" in out
    assert "Traceback (most recent call last)" in out


def test_plain_record_has_no_trailing_newline() -> None:
    logger = logging.getLogger("test.plain")
    record = logger.makeRecord(logger.name, logging.INFO, "", 0, "hello", (), None)
    out = StructuredFormatter().format(record)
    assert out.endswith("message=hello")
    assert "\n" not in out
