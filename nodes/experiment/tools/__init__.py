"""节点专属工具注册入口（bootstrap 启动时自动 import）。

experiment 节点提供 1 个通用 Python 模块，通过 safe_execute_python 调用：
- diagnose  — 通用诊断 + 源码修复引擎（基于 diagnose_patterns.yaml 中的 51 条错误模式）

领域知识由按条件加载的 skill、官方文档、FermiLink 和项目知识库提供；
diagnose_patterns 只保留跨应用的故障分类与处置方向。

节点注册的工具：safe_bash 提供 safe_run_bash / safe_write_file /
safe_execute_python（builtin 同款 + 高危事前拦截，v0.11 起替代同名覆盖），
resource_manager 提供资源发现/推荐/作业提交；其余白名单条目引用
shared/tools/builtin.py 的内置工具（save_artifact / read_artifact / 等）。
用法（在 safe_execute_python 调用块中）：
    from tools.diagnose import DiagnoseEngine, SourceFixer

    report = DiagnoseEngine.from_yaml_patterns().analyze_log("/path/to/build.log")
    fixer = SourceFixer()
"""

from . import diagnose  # noqa: F401
# Phase 3: resource discovery, resource recommendation, and scheduler submission
# for local/SLURM/PBS/Kubernetes backends.
from . import resource_manager  # noqa: F401
# submit intent -> scheduler identity 的崩溃恢复；只按 route attempt/nonce 对账。
from . import external_submission_recovery  # noqa: F401
from . import probe_toolchain  # noqa: F401
from . import build_contract  # noqa: F401
from . import execution_envelope  # noqa: F401  frozen route-step execution evidence
from . import execution_route  # noqa: F401
from . import env_provision  # noqa: F401
from . import build_graph  # noqa: F401
from . import build_state  # noqa: F401
from . import run_contract  # noqa: F401
from . import preflight  # noqa: F401
from . import sediment  # noqa: F401
from . import operation_completion  # noqa: F401
from . import resource_fetch  # noqa: F401  controlled file/Git acquisition
# safe_bash 必须在框架 builtin 注册之后 import —— 它基于 builtin 工具 def 注册
# safe_run_bash / safe_write_file / safe_execute_python（v0.11 起不再同名覆盖），
# 加高危命令事前拦截（提权/递归删除/磁盘覆写/系统控制/进程杀除/git 强推）。
from . import safe_bash  # noqa: F401
