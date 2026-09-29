"""这份代码是哪一版。

自更新、`doctor`、会话诊断包、组织协议对表读的都是**同一个标记**
（`self_update.VERSION_MARKER`，打包器写在 harness 目录里）。开发时的源码
checkout 没有这个标记，答 None：那不是一个发出去的版本。
"""
from __future__ import annotations


def installed_version() -> str | None:
    from app.launcher import find_the_harness
    from app.services.self_update import version_of

    return version_of(find_the_harness())
