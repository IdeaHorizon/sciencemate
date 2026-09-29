"""核心框架：harness schema、loader、注册表、context、agent loop、executor。"""

# 自动注册内置 loop hooks（memory_delta 永远启用；scratchpad opt-in 可用）
from . import loop_hooks_builtin  # noqa: F401
