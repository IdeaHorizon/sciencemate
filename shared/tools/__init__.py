"""shared/tools —— 跨节点通用的工具池。

import 时触发 register_tool 调用。runner / chat.py 启动时通过
core.bootstrap.bootstrap() 来 import 本模块，把所有共享工具注册进 registry。

工具分类（v0.3.3 cleanup 后）：
  builtin             —— 内置工具（artifact / memory / 文件 / shell / budget / human_input / scratchpad）
  papers              —— 文献搜索（semantic_scholar + arxiv，含 query cache）
  run_node            —— sub-agent 调度
  library.python_exec —— execute_python（subprocess 跑 Python，跨节点常用）
  library.derivation_check —— check_step / find_counterexample / dimensional_check /
                             limit_check（推导验证四件套；工具面全节点共享，
                             证据所有权归 derivation 节点独占）
  library.kb          —— ★ KB v3 (4 entity × 10 claim_type) 工具集
  library.proposals   —— propose / list_proposals / resolve_proposal (统一 inbox)
  library.audit       —— curator_log / curator_revert (audit trail)
  library.runtime_control —— inject_into_node / cancel_node / list_active_child_runs
  library.disagreement_scan —— scan_artifact_disagreements (Phase H)
  library.profile_tools —— read_profile / propose_profile_update
  library.skill_tools —— list_skills / record_skill_usage / skill_usage_stats / deprecate_skill
  library.artifacts_extra —— freeze_artifact + freeze_and_register
"""
from . import builtin                          # noqa: F401
from . import papers                           # noqa: F401
from . import web                              # noqa: F401  web_search + web_fetch (Bing)
from . import run_node                         # noqa: F401
from .library import python_exec               # noqa: F401
from .library import derivation_check           # noqa: F401  推导验证四件套（共享工具面）
from .library import kb                        # noqa: F401  KB CRUD + search + slicers
from .library import proposals                 # noqa: F401  propose / list / resolve
from .library import audit                     # noqa: F401  curator_log / curator_revert
from .library import runtime_control           # noqa: F401  inject_into_node / cancel_node
from .library import job_registry              # noqa: F401  declare_job / job_progress
from .library import artifact_intake           # noqa: F401  import_artifact（外部材料入口）
from .library import file_digests             # noqa: F401  hash_files（摘要归框架算）
from .library import materials_extract        # noqa: F401  extract_material（归档解到材料池）
from .library import disagreement_scan         # noqa: F401  Phase H
from .library import profile_tools             # noqa: F401  PROFILE / PROJECT
from .library import skill_tools               # noqa: F401  Skill mgmt
from .library import artifacts_extra           # noqa: F401  freeze_artifact + freeze_and_register
from .library import producer_transcript       # noqa: F401  read_producer_transcript (used by _reviewer)
from .library import decision_package          # noqa: F401  present_decision_package (used by _orchestrator)
from .library import writing_gate              # noqa: F401  resolve_project_synthesis（纯函数，不注册工具）
from .library import latex                     # noqa: F401  compile_latex（全节点可用，writing 主用）
from .library import cross_model               # noqa: F401  list_alternative_models + consult_other_model (v1.5)
from .library import tasks                     # noqa: F401  task(action=...) first-class TaskList (v2.0)
from .library import blockers                  # noqa: F401  generic node -> orchestrator blocker reports
from .library import concede                   # noqa: F401  对 blocking 义务的公开让步（判决拆除批 0）
from .library import memory_tools             # noqa: F401  memory_write / memory_note / memory_maintain / memory_recall
