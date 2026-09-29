"""Artifact 类型能力注册表。

每个 artifact_type 在这里声明**它是什么**，各机制查表推导**该怎么待它**。
`save_artifact` / `freeze_artifact` / `gc_runs` 查 retention 面；闭合账本、
引用诚信闸、writing 输入门查科学证据面。

## 为什么类型属性必须集中声明

这张表原来只管 retention（模块名也还叫 policy），但 `tamper_evident` 早就不是
retention —— 它是科学诚信属性。而**同一层级的另外三个属性**当时散落在各机制
里，各写了一份硬编码名单：

  - `core/prereg_commitments._RESULT_TYPES`          —— 谁能勾除闭合条目
  - `core/loop_hooks_builtin._CITATION_CHECK_TARGET_TYPES` —— 谁要被查引用诚信
  - `nodes/writing/artifact_expectations.yaml`        —— writing 的硬性上游

三份名单回答的都是"这个 artifact 类型是什么性质"，却分散在三处、各自演化。
后果是机械的：**新增一种证据类型时，漏改哪份名单，哪道闸就对它默认失效** ——
而失效的方向全是放行（不查引用、不算勾账、不受保护）。这正是"护栏要扫盘不要
写名单"那条教训的形状。

对照组就在隔壁：`shared/tools/builtin._producing_output_owners()` 遍历所有
harness 的 `required_output_artifact_types` 推导所有权，从不硬编码 —— 新节点
一声明就自动受保护。本模块把同一个模式补给类型属性。

3 类 retention：

  permanent          —— 永久交付物（B 类）。冻结即交付：账本钉死这一版，没有第二份拷贝
                        （曾经的 deliverables/ 与 org/_archive/ 抄件、prereg manifest
                        都已删除，它们各自的策略位 deliverable_dir / also_archive_org /
                        tamper_evident 随之消失 —— 零读者的字段就是假承诺）
  chunk_then_drop    —— 已 chunk 进 KB 后原文件可删（C 类）
  transient          —— 短期 debug（D 类，默认）。run_dir 整体到 ttl_days 可清
"""
from __future__ import annotations

import re
from typing import Literal, TypedDict


Retention = Literal["permanent", "chunk_then_drop", "transient"]


class ArtifactPolicy(TypedDict, total=False):
    # ── 保管面 ────────────────────────────────────────────────────────────
    retention: Retention
    ttl_days: int              # transient 类的 TTL（默认 30）

    # ── 科学证据面 ────────────────────────────────────────────────────────
    # 三个属性各自回答一个机制的问题，都只在这里声明一次。
    evidence_record: bool
    """一次研究执行的**权威冻结记录** —— 带裁决理由与可信度评估的那一份。

    这是 writing 的硬性上游所认的东西：论文必须建立在至少一份研究执行记录上。
    刻意不等于 `discharge_ledger`：结果表能携带勾账数据，却没有裁决理由和可信度
    段落，单凭它写论文就是拿一张数字表当研究记录。

    ⚠️ 每个 evidence_record 类型**必须有且只有一个 producing 节点 owner**
    （见 `_validate_registry`）。无主的证据类型 = 谁都能凭空造证据。
    """

    discharge_ledger: bool
    """metadata 可携带闭合条目的兑现记录（`measured_metrics` / `closure_discharges`）。

    闭合账本只从这类工件读兑现（`core/prereg_commitments`）。范围比
    `evidence_record` 宽：权威记录必然能勾账，但支撑性的结果表也能。
    """

    cites_kb_claims: bool
    """正文会引用 KB claim id，因此必须过引用诚信闸（每 turn 末扫，幻觉引用 fail）。

    与上面两个正交：manuscript 引用但不是证据记录也不勾账；结果表勾账但是纯数据、
    不写引用。
    """

    # metadata 契约**不在这里** —— 它在类型 owner 的契约模块里，按两道门分工
    # 声明（写入门 = 结构纪律、与模式无关；冻结门 = 承诺兑现、与模式相关）。
    #
    # 2026-08-23 删除 `required_metadata` / `present_but_may_be_empty`：
    # 它们是**模式盲**的类型级近似，而 observation / derivation 的 metadata
    # 契约本来就**取决于 mode**。近似必然对至少一种模式是错的，实测三例：
    #
    #   · derivation 的 `assumptions: []` 被判缺字段 —— 纯代数恒等式不需要额外假设
    #   · observation 的 `verdicts` 全模式非空，而 exploratory run 被硬禁勾账、
    #     天然无闭合条件可裁决 → 框架先禁止模型做，再罚它没做
    #   · derivation 的 credibility / verdicts / counterexample_search 同款全模式
    #     非空，而节点自己的冻结门把它们放在 confirmatory-only
    #
    # 给近似加第二档（"必须在场、可以为空"）只是让它对某几个字段无害，病根还在：
    # **同一份契约两处声明，其中一处表达不了 mode**。唯一声明权归 owner 的契约
    # 模块，两道门共用同一批判据函数（判据原语见 shared/lib/metadata_contract）。

    # ── 流程角色面 ────────────────────────────────────────────────────────
    exemplar_candidate: bool
    """这类产物可以当**范例**（供后来的项目照着写）。

    判据是"它示范了一种可模仿的手艺"：预注册示范怎么把问题写成可证伪的，
    实验/观察记录示范怎么如实记录执行与裁决，综述示范怎么组织证据。
    纯数据表（clean_results）与运行内务不在此列 —— 照着它们学不到判断。

    谁在用：`core.kb_promotion.exemplar_candidates` 的机械预筛。
    真正的选拔在人批与使用验证两关（RFC §19.2）。
    """

    singleton: bool
    """一个项目里这类产物只该有一份「当前版」（研究计划、综述概览这类）。

    save 原语据此归一名字（剥 _v2/_rev3 后缀）并清掉未冻结的旧兄弟 ——
    此前这套语义只活在 hypothesis 节点的私有包装工具里，别的节点存这些类型
    照样能堆出 plan_v1/plan_v2/plan_final 三份"当前版"。
    """

    framework_internal: bool
    """框架运行时**自己**落盘的内务件，不是任何节点的交付物。

    种差是**谁写的**：summarizer / hooks 在节点背后写它们，节点的模型从不产出。
    不是"谁声明过" —— 那条判据试过，是错的：`hypothesis_cluster_report` 是
    hypothesis 的真实产物却不在任何 `required_output_artifact_types` 里（全仓只有
    16 个类型是**必需**产出）。「必需产出」只是「合法产物」的子集，拿它当判据会把
    可选的科学产物一起漏出 KB 整合。

    谁在用：KB 整合目标过滤、下游预览排序（`integration_targets` /
    `rank_for_downstream`）。

    2026-08-19 实测死锁：`compression_log__compression_turn_21` 被框架自己列进
    curator 的整合目标，而 `scan_artifact_disagreements` 结构上扫不到这种内务件
    → `n_unintegrated` 恒为 1 → `curator_state` 恒 pending → 下游永远被拦，最后
    靠模型自己猜出来要把它剔掉才解开。
    """

    run_local: bool
    """随 run 生、随 run 死的**运行时记录** —— 落 run 自己的目录，不进研究记录。

    与 `framework_internal` 是两个问题：那个答"谁写的"（框架在节点背后写），
    这个答"要不要活过这个 run"。二者可以分开：`run_manifest` 也是框架在节点
    背后写的，但它把实验绑到预注册的版本和哈希上，后面的 run 要读它
    （core/obligations 按它判 requires_hypothesis_verdict）—— 它是记录，不是
    运行时。而压缩摘要、资源画像、环境快照、修复账没有任何一个后续 run 读它们
    （2026-09-12 逐类型 grep 核过），存在的意义只是事后审计这一个 run。

    2026-09-10 之前所有 artifact 都落 `<node>/artifacts/`：一个真项目里 180 份
    信封 89 份是压缩日志，进 git、进版本历史、进目录页、进收尾清单、进派发闸
    的上游指纹（每压缩一次指纹就变）。写入口按这一位路由（core/state.py
    `_store_for`：run_local 进 run 本地账本），它们就都不在了。

    不变量（`_validate_registry`）：run_local ⇒ framework_internal —— 运行时记录
    不可能是某个节点的交付物。
    """


_DEFAULT: ArtifactPolicy = {"retention": "transient", "ttl_days": 30}


# 标准声明 —— owner 加新 artifact_type 时在这里补
POLICY: dict[str, ArtifactPolicy] = {
    # ─── B 类：永久交付物 ────────────────────────────────────────────────
    "survey_report":     {"retention": "permanent", "exemplar_candidate": True},
    # v2.1 literature 定向模式（targeted_lookup / contradiction_check /
    # method_lookup）的交付物。未注册会落到默认 transient(30d)，而它是可核验
    # 证据、下游 Analysis 会引用 —— 必须是永久交付物。
    "literature_evidence_package":
                         {"retention": "permanent"},
    "research_plan":     {"retention": "permanent", "singleton": True, "exemplar_candidate": True},
    "hypothesis_research_overview":  {"retention": "transient", "singleton": True},
    "hypothesis_innovation_report":  {"retention": "transient", "singleton": True},
    "hypothesis_output_validation":  {"retention": "transient", "singleton": True},
    "pre_registration":  {"retention": "permanent", "exemplar_candidate": True},
    # 检索协议对取样，等同预注册对假设：冻结后必须能核验没被改过，否则"协议
    # 先于取证"这条纪律就只是约定。
    "search_protocol":   {"retention": "permanent"},
    "manuscript":        {"retention": "permanent", "cites_kb_claims": True},
    "accepted_paper":    {"retention": "permanent"},
    "analysis_report":   {"retention": "permanent"},
    "review_report":     {"retention": "permanent"},
    "research_intent":   {"retention": "permanent"},
    "clean_results":     {"retention": "permanent", "discharge_ledger": True},
    # Observation 的 display-ready 结果表：与 clean_results 一样是支撑数据，不是
    # 带裁决理由的权威执行记录，也不能单凭它勾账。逐行来源和表语义由 owner 的
    # save/freeze gate 校验，Writing/Postprocess 无需从 observation_log prose 重建。
    "observation_results": {"retention": "permanent"},
    "dataset_snapshot":  {"retention": "permanent"},
    # B 刀后唯一图产物与完成契约：figure 记录（render_figure 铸造，出处三重
    # hash 绑定）。figure_package 已亡（typed 链八收一，见
    # docs/verdict_demolition/FIGURE_SUBSYSTEM_REBUILD.md）。
    "figure":            {"retention": "permanent"},

    # ─── C 类：chunk 后可丢原本 ────────────────────────────────────────
    "experiment_log":    {"retention": "chunk_then_drop",
                          "evidence_record": True, "discharge_ledger": True,
                          "cites_kb_claims": True, "exemplar_candidate": True},
    # 检视式取证的权威记录 —— 与 experiment_log 完全对称：能勾账、要查引用、
    # writing 认它做上游。两者的差别在**闸**（防伪造执行 vs 防摘樱桃），
    # 不在它们作为证据的地位。
    # 五个必需字段原本是 observation 节点的五条 quality_check（frozen /
    # credibility / verdict / sampling_discipline / findings）—— 全是"这份记录里
    # 有没有某一段"，全部搬到写入面。
    "observation_log":   {"retention": "chunk_then_drop",
                          "evidence_record": True, "discharge_ledger": True,
                          "cites_kb_claims": True},
    # 演绎式取证（2026-08-22）。第三种证据模态：不接触世界，从已承诺的前提
    # 推出新命题。它的原罪与前两种都不同 —— experiment 防"伪造执行"、
    # observation 防"摘樱桃"，推导防的是**无效步骤伪装成有效**（跳步、偷换
    # 假设、适用域静默扩大、近似不带误差控制）。前两套闸一条也拦不住它：
    # 一条 50 步的链每步都真跑过、引的定理也都真实存在，照样能在第 23 步
    # 偷换一个假设。
    "derivation_log":    {"retention": "chunk_then_drop",
                          "evidence_record": True, "discharge_ledger": True,
                          "cites_kb_claims": True},
    "raw_data_dump":     {"retention": "chunk_then_drop"},
    "tool_output_log":   {"retention": "chunk_then_drop"},

    # ─── D 类：短期 debug（默认） ──────────────────────────────────────
    # 没写的 artifact_type 都 fallback 到 _DEFAULT

    # ─── E 类：框架内务 —— 运行时自己落盘，不是谁的交付物 ────────────────
    # 此前这些类型靠"没注册 → 默认 transient"隐式表达，说得出"存多久"，说不出
    # "它是什么"。于是「是不是交付物」这条性质只好在别处另立名单（曾经是
    # `loop_hooks_builtin._INTERNAL_ARTIFACT_PREFIXES` 前缀名单，后来是
    # `core/artifact_roles.FRAMEWORK_INTERNAL_TYPES`）—— 正是本文件要消灭的形状。
    # run_local：随 run 生灭，落 run 目录（见 `run_local` 字段说明）。
    "compression_log":   {"retention": "transient", "framework_internal": True, "run_local": True},
    "resource_profile":  {"retention": "transient", "framework_internal": True, "run_local": True},
    "environment_snapshot": {"retention": "transient", "framework_internal": True, "run_local": True},
    "repair_ledger":     {"retention": "transient", "framework_internal": True, "run_local": True},
    # run_manifest 是实验对预注册 (id, version, content_hash) 的绑定凭据，后面的
    # run 要读 —— 它留在研究记录里（experiment/artifacts/）。
    "run_manifest":      {"retention": "transient", "framework_internal": True},
    # `resource_recommendation` 曾在此登记但全仓没有写者（resource_manager 只写
    # hook_state），2026-09-12 删除：策略表里只登记真会落盘的类型。
}


def get_policy(artifact_type: str) -> ArtifactPolicy:
    """查 policy。未声明的类型 → 默认 transient。"""
    return POLICY.get(artifact_type, _DEFAULT)


def is_permanent(artifact_type: str) -> bool:
    return get_policy(artifact_type)["retention"] == "permanent"


def is_chunk_then_drop(artifact_type: str) -> bool:
    return get_policy(artifact_type)["retention"] == "chunk_then_drop"


def is_transient(artifact_type: str) -> bool:
    return get_policy(artifact_type)["retention"] == "transient"


def ttl_days(artifact_type: str) -> int:
    return int(get_policy(artifact_type).get("ttl_days", 30))


# ── 科学证据面：访问器 ──────────────────────────────────────────────────────
#
# 各机制**查这些函数**，不再各自维护名单。加一种新证据类型 = 在上面 POLICY 里
# 声明一行，闭合账本 / 引用诚信闸 / writing 输入门同时生效；漏声明则一处都不
# 生效 —— 失效方向从"静默放行"翻转成"整类不被承认"，是响的。

def is_evidence_record(artifact_type: str) -> bool:
    return bool(get_policy(artifact_type).get("evidence_record"))


def carries_discharge_ledger(artifact_type: str) -> bool:
    return bool(get_policy(artifact_type).get("discharge_ledger"))


def is_exemplar_candidate(artifact_type: str) -> bool:
    """这类产物能当范例？"""
    return bool(get_policy(artifact_type).get("exemplar_candidate"))


def exemplar_candidate_types() -> tuple[str, ...]:
    return tuple(sorted(t for t in POLICY if is_exemplar_candidate(t)))


def is_singleton(artifact_type: str) -> bool:
    """这类产物一个项目只该有一份当前版？"""
    return bool(get_policy(artifact_type).get("singleton"))


#: singleton 名字里要剥掉的版本后缀（Plan_v2 / Overview_rev3 → 同一身份）
SINGLETON_VERSION_SUFFIX = re.compile(r"(?i)(?:[-_ ]?v\d+|[-_ ]?rev\d+)$")


def is_framework_internal(artifact_type: str) -> bool:
    """框架自己落盘的内务件？"""
    return bool(get_policy(artifact_type).get("framework_internal"))


def framework_internal_types() -> tuple[str, ...]:
    return tuple(sorted(t for t in POLICY if is_framework_internal(t)))


def is_run_local(artifact_type: str) -> bool:
    """随 run 生灭的运行时记录？（落 run 目录，不进研究记录 —— 见字段说明）"""
    return bool(get_policy(artifact_type).get("run_local"))


def run_local_types() -> tuple[str, ...]:
    return tuple(sorted(t for t in POLICY if is_run_local(t)))


def singleton_types() -> tuple[str, ...]:
    """所有 singleton 类型（供工具 enum / 报错文案取值，别再各自抄名单）。"""
    return tuple(sorted(t for t in POLICY if is_singleton(t)))


def artifact_type_of(artifact_id: str) -> str:
    """`clean_results__UK_Analysis` → `clean_results`（id 形状是 `{type}__{slug}`）。"""
    return str(artifact_id or "").split("__", 1)[0]


def is_integration_target(artifact_id: str) -> bool:
    """该不该进 KB 整合 / 该不该被下游当交付物看待。

    默认 True，只排除框架内务。方向由代价不对称决定：误收一个内务件 = 门禁
    fail-closed 的**无声**死锁（实测发生过）；误漏一个科学产物 = 下游 reviewer /
    闭合账本会喊。所以不确定时收。
    """
    return not is_framework_internal(artifact_type_of(artifact_id))


def integration_targets(artifact_ids: list[str]) -> list[str]:
    """挑出可整合的，保持原顺序。"""
    return [a for a in (artifact_ids or []) if is_integration_target(a)]


def rank_for_downstream(artifact_ids: list[str]) -> list[str]:
    """预览排序：交付物在前、内务件在后。与"该不该整合"同一个判据。

    E2E v13 实测：预览按字母序取前三，显示的全是 compression_log，把
    pre_registration / research_state 挤掉了。
    """
    external = [a for a in artifact_ids if is_integration_target(a)]
    internal = [a for a in artifact_ids if not is_integration_target(a)]
    return external + internal


def cites_kb_claims(artifact_type: str) -> bool:
    return bool(get_policy(artifact_type).get("cites_kb_claims"))


def evidence_record_types() -> tuple[str, ...]:
    """所有权威研究执行记录类型（排序，便于稳定比较与报错文案）。"""
    return tuple(sorted(t for t in POLICY if is_evidence_record(t)))


def discharge_ledger_types() -> tuple[str, ...]:
    """所有可携带兑现记录的类型。"""
    return tuple(sorted(t for t in POLICY if carries_discharge_ledger(t)))


def citation_checked_types() -> tuple[str, ...]:
    """所有要过引用诚信闸的类型。"""
    return tuple(sorted(t for t in POLICY if cites_kb_claims(t)))


def _validate_registry() -> None:
    """启动即校验的类型不变量。

    声明表是**静态配置**，配错了不该等到运行时才以别的面目出现（"闭合条目怎么
    勾不上"、"这份记录怎么没被查引用"）—— 那种症状离病因隔着好几层。所以在
    import 时就炸，且把违反的类型逐个点名。
    """
    problems: list[str] = []

    # 权威记录必然能勾账：反过来不成立（结果表能勾账但不是权威记录）。
    # 若某类型声明成 evidence_record 却不能勾账，闭合账本就读不到它 ——
    # writing 认它当上游、账本不认它的兑现，两道闸对同一份材料给出相反结论。
    for artifact_type in POLICY:
        # 运行时记录不可能是节点交付物：run_local 却不 framework_internal，
        # 等于说"某个节点的产出随 run 一起消失" —— 那是丢产物，不是策略。
        if is_run_local(artifact_type) and not is_framework_internal(artifact_type):
            raise ValueError(
                f"{artifact_type}: run_local 必须同时 framework_internal —— "
                "节点的产出不能随 run 一起消失"
            )
        if is_framework_internal(artifact_type) and (
            is_evidence_record(artifact_type)
            or carries_discharge_ledger(artifact_type)
            or cites_kb_claims(artifact_type)
        ):
            raise ValueError(
                f"{artifact_type}: framework_internal 与科学证据面互斥 —— "
                "框架内务件不能同时是证据记录/勾账凭据/引用主体"
            )
        if is_evidence_record(artifact_type) and not carries_discharge_ledger(artifact_type):
            problems.append(
                f"{artifact_type}: evidence_record=True 却没有 discharge_ledger "
                "—— writing 会认它做上游，闭合账本却读不到它的兑现记录"
            )

    if problems:
        raise ValueError(
            "artifact_policy 类型声明违反不变量：\n  - " + "\n  - ".join(problems)
        )


_validate_registry()
