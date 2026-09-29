"""节点专属工具注册入口（bootstrap 启动时自动 import）。

每个工具模块都必须在这里 import，模块加载时触发 register_tool(...）。
不在这里 import 的工具，即使写了 register_tool，也不会进入 agent 可用工具表。
"""

from . import search_papers  # noqa: F401
from . import classify_papers  # noqa: F401
from . import archive_papers  # noqa: F401
from . import finalize_evidence_package  # noqa: F401
