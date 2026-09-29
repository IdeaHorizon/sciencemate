"""hypothesis 节点本职是产假设。

v3 起 create_claim / create_concept / search_kb 等都在 `shared.tools.library.kb`
（由 shared/tools/__init__.py 全局加载）；本文件额外加 freeze_artifact + HIF 评分工具。
"""
from shared.tools.library import artifacts_extra  # noqa: F401 —— freeze_artifact（hypothesis pre-registration）

from . import artifact_save  # noqa: F401 —— stage_hypothesis_draft（singleton 命名归一）
from . import conclusion_audit  # noqa: F401 —— audit_hypothesis_vs_conclusions
from . import hif_scorer  # noqa: F401 —— score_hypothesis_innovation
from . import hypothesis_cluster  # noqa: F401 —— cluster_hypothesis_candidates
from . import hypothesis_evolve  # noqa: F401 —— evolve_hypothesis
from . import output_validator  # noqa: F401 —— validate_hypothesis_outputs
from . import goal_alignment  # noqa: F401 —— audit_user_goal_alignment（防任务退化）
from . import definition_lock  # noqa: F401 —— audit_definition_fidelity（防改写分类定义）
from . import threshold_grounding  # noqa: F401 —— audit_threshold_grounding（阈值依据）
from . import comparison_protocol  # noqa: F401 —— audit_comparison_protocol（跨任务可比协议）
from . import resource_feasibility  # noqa: F401 —— audit_resource_feasibility（资源可执行性）
from . import cost_instrumentation  # noqa: F401 —— audit_cost_instrumentation（成本采集）
from . import paper_reader  # noqa: F401 —— read_reference_paper
from . import research_goal  # noqa: F401 —— get_research_goal（含 dialogue grounding）
from . import research_state  # noqa: F401 —— update/read_research_state（Analysis 版本化研究状态）
from . import dialogue_context  # noqa: F401 —— user prompt / 对话上下文辅助
from . import workflow_audit  # noqa: F401 —— audit_computational_workflow
from . import research_questions  # noqa: F401 —— 研究问题视图（解析权在 core.prereg_commitments）
from . import analysis_mode  # noqa: F401 —— plan / revise 模式解析（无工具，供 hook 与 validator 用）
