"""所有节点都可用的内置工具。

这些覆盖一个研究节点最基本的需要：读写 artifact、读写 memory、跑命令行。
owner 可以在自己的模块里加更多工具。

⚠️ `run_bash` 是 **任意 shell 执行**。它的存在让 owner 可以一行不写就让 agent
跑 OpenFOAM / LAMMPS / Gaussian / 任何 CLI，但也意味着 agent 拼错命令能
真的删掉你的文件。请只在你不介意被弄脏的开发机 / 容器 / HPC 沙箱里用。
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.state import State, _slug
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.artifact_text import artifact_text, as_text
from shared.lib.cancellable_subprocess import spawn_and_wait


def _resolve_path(state: State, path: str, *, write: bool = False) -> Path:
    """把传入的 path 解析成绝对 Path。相对路径按 state.root 解释。"""
    from core.project_workspace import resolve_tool_path

    return resolve_tool_path(state, path, write=write)


# ── save_artifact ────────────────────────────────────────────────────────────

# issue #202：哪些**架构节点**的产出也要 owner 保护。
#
# 原设计只保护 producing 节点的 deliverable，把 `_` 开头的架构节点整类跳过 ——
# 于是 orchestrator 自己写 manuscript 会被当场拒绝，写 review_critique（审查
# 它自己那份）却畅通无阻。**漏的恰恰是最该防的那个**：review_critique 是整个
# review 门的凭据，被审查方能自产它，独立性就只是约定而非机制。
# qinp 2026-07-28 实测：5 次审稿全失败后 orchestrator 自产 critique，门开了。
#
# 这里只收 review 门凭据这一类（curator 的整合产物由别的机制管，不在本 issue）。
_ARCH_OWNED_TYPES: dict[str, set[str]] = {
    "review_critique": {"_reviewer"},
}


@lru_cache(maxsize=1)
def _producing_output_owners() -> dict:
    """artifact_type -> {node_type, ...} 有明确 owner、不许他人凭空新建的产出。

    包含两部分：
      1. producing 节点（不以 `_` 开头）声明的 required_output —— 真正的
         deliverable，owner 唯一。
      2. `_ARCH_OWNED_TYPES`：需要 owner 保护的架构节点产出（目前只有
         review_critique，见上方 issue #202 注释）。
    """
    from core.loader import list_harnesses, load_harness
    owners: dict[str, set[str]] = {t: set(v) for t, v in _ARCH_OWNED_TYPES.items()}
    for nt in list_harnesses():
        if nt.startswith("_"):
            continue
        try:
            h = load_harness(nt)
        except Exception:
            continue
        for t in (h.required_output_artifact_types or []):
            owners.setdefault(t, set()).add(nt)
    return owners


def _phantom_citations(state: Any, artifact_type: str, content: str) -> list[str]:
    """正文里引了但 KB 里没有的 claim id（类型没声明 cites_kb_claims 则恒空）。"""
    from shared.lib.artifact_policy import cites_kb_claims

    if not content or not cites_kb_claims(str(artifact_type or "")):
        return []
    try:
        from shared.lib.citation_integrity import find_phantom_citations

        return sorted(find_phantom_citations(content, state)["phantom"])
    except Exception:                              # noqa: BLE001
        # 判定层自己崩了不该把写入判死 —— 证据可持久化，判决不由这里做出。
        return []


def _materialize_content_sources(
    state: Any, content_from_file: Any,
) -> tuple[str, list[str], dict | None]:
    """把 content_from_file（单路径或**有序路径列表**）机械读成一份内容。

    返回 (content, 解析后的来源清单, 错误)；错误非 None 时前两个无意义。
    每个路径独立做边界检查 —— 列表形态不能变成批量任意文件读取的后门。
    任一元素失败则整个调用失败并**指名是哪一个**：拼一份带洞的产物比报错贵
    得多（洞要到下游读的时候才发现，而那时已经没人记得来源）。
    """
    from pathlib import Path as _P

    from core.project_workspace import working_directory

    sources = (content_from_file if isinstance(content_from_file, list)
               else [content_from_file])
    if not sources or not all(isinstance(s, str) and s.strip() for s in sources):
        return "", [], {
            "status": "error",
            "error": ("content_from_file 必须是一个路径，或非空字符串路径的"
                      f"有序列表（收到：{content_from_file!r}）"),
        }
    allowed = [state.root.resolve()]
    if getattr(state, "project_root", None) is not None:
        allowed.append(state.project_root.resolve())
    if getattr(state, "project_worktree", None) is not None:
        allowed.append(state.project_worktree.resolve())
    parts: list[str] = []
    resolved: list[str] = []
    for raw in sources:
        src = _P(raw)
        if not src.is_absolute():
            src = working_directory(state) / raw
        try:
            src = src.resolve()
        except OSError as e:
            return "", [], {
                "status": "error",
                "error": (f"content_from_file 路径无法解析（{raw}）："
                          f"{type(e).__name__}: {e}"),
            }
        if not any(src == a or a in src.parents for a in allowed):
            return "", [], {
                "status": "error",
                "error": (f"content_from_file 越界：{src} 不在本 run / "
                          "本项目目录树内（不许读任意文件）"),
            }
        try:
            part = src.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return "", [], {
                "status": "error",
                "error": (f"读 content_from_file 失败（{raw}）："
                          f"{type(e).__name__}: {e}"),
            }
        # 空文件是合法内容（判决拆除：145 删）—— 「上一步没写成」是判断，归模型；
        # 拼装清单 content_assembled_from 如实记下它，读者看得见哪一段是空的。
        parts.append(part)
        resolved.append(str(src))
    # 段与段之间补一个换行：分段写的人几乎不会记得在每段末尾留一个，而少了它
    # 两段会黏成一行 —— `\end{abstract}` 直接接上 `\section{Introduction}`，
    # 对 LaTeX / markdown 都是实质性损坏，而且损坏点只在下游编译或阅读时才暴露。
    # 已经有换行的不重复加；最后一段不动（不给内容凭空加尾巴）。
    joined = "".join(
        part if part.endswith("\n") or index == len(parts) - 1 else part + "\n"
        for index, part in enumerate(parts)
    )
    return joined, resolved, None


async def _save_artifact(state: State, artifact_type: str, name: str,
                         content: str = "",
                         content_from_file: str | list = "",
                         metadata: dict | None = None,
                         amendment_reason: str = "", **_: Any) -> dict:
    # artifact_type / name 非空 = schema required + minLength:1，派发口核。
    # content 与 content_from_file 二选一是 oneOf 契约，派发口不核，这里只写一次。
    if bool(content) == bool(content_from_file):
        return {"status": "error",
                "error": "content 和 content_from_file 必须给一个、也只能给一个"}
    # 冻结是 freeze_artifact 的专属动作 —— 2026-08-22 实测模型在 freeze_artifact
    # 被机械拒之后，改用这个工具重写一遍同一份产物、metadata 里直接带
    # frozen:true 绕了过去。详见 core/artifact_capabilities 里的说明。
    from core.artifact_capabilities import reject_freeze_forgery

    try:
        reject_freeze_forgery(metadata)
    except PermissionError as exc:
        # 这是**驳回**，不是崩了 —— 按本工具既有风格回错误结果，让模型读到理由。
        return {"status": "error", "error": str(exc)}
    # schema 声明 content 是字符串，但工具参数来自 LLM 的 JSON —— 模型给一个
    # 对象/数组是常事。以前这种调用一路走到 `sha256_text(content)` 才炸
    # `AttributeError: 'dict' object has no attribute 'encode'`：一句指不出
    # 病因的 traceback，模型看了也不知道该改什么。这里当场规范化成 JSON 文本
    # （机械可判的事归框架，别让模型猜），存储形状因此永远是文本。
    if not isinstance(content, str):
        content = as_text(content)

    # ── 内容按引用（E2E-5b：16 次 writing 重试的死因之一）─────────────────
    # `content` 是工具参数 → 大产物必须由模型逐 token 吐出来 → 产物大小被
    # token 上限物理封顶（#184 实测 ~6KB 起截断；40KB 手稿必死）。类级解法：
    # 内容留在磁盘上，模型只点名 —— 单个路径，或**有序路径列表**（框架按
    # 列表顺序机械拼接）。一个字节都不经过输出通道。
    #
    # 列表形态是 2026-08-22 补的。此前只收单路径，而 write_file 是整文件
    # **覆盖**语义（没有追加），于是"内容已在盘上、只是分在几个文件里"的
    # 产物没有任何合法合成路径 —— writing 实测在 12k 输出上限处反复探测一个
    # 不存在的 append 原语，十几分钟空转（本文件旧注释声称"分块写文件的工具
    # write_file 本来就有"，那句是假的）。模型不搬运字节：盘上的字节由框架
    # 移动，模型只负责点名和排序。
    #
    # 本块必须在引用诚信 / 必需字段两道闸**之前**把 content 落定：闸查的是
    # 真正要落盘的内容。此前本块在两道闸之后，走文件引用的内容（恰恰是最长、
    # 最需要查的 manuscript）从来没被引用闸看过一眼。
    assembled_from: list[str] = []
    if content_from_file:
        content, assembled_from, _err = _materialize_content_sources(
            state, content_from_file)
        if _err is not None:
            return _err

    # ── singleton 类型：名字归一 + 清未冻结旧兄弟（性质在 artifact_policy）──
    #
    # 此前这套语义只活在 hypothesis 的私有包装工具（save_hypothesis_artifact，
    # 已删）里 —— 别的节点存这些类型照样堆得出 plan_v1/plan_v2/plan_final
    # 三份"当前版"。性质跟类型走：注册表声明 singleton，唯一的 save 执行。
    from shared.lib.artifact_policy import SINGLETON_VERSION_SUFFIX, is_singleton
    if is_singleton(artifact_type):
        cleaned = SINGLETON_VERSION_SUFFIX.sub("", name.strip()).strip("_- ")
        if cleaned:
            name = cleaned

    # ── 引用诚信：在写入面拦，不在 turn 末扫 ──────────────────────────────
    #
    # `cites_kb_claims: True` 这个能力**早就声明在类型上**（artifact_policy），
    # 注释原文："正文会引用 KB claim id，因此必须过引用诚信闸"。只是执法点被放在
    # 了三跳之外：写产物 → turn 末 hook 扫 → 落 transcript 事件 → QC 读事件。
    #
    # 三跳每一跳都可能断（hook 没跑、事件被压缩掉、QC 判据改了），而且最后那跳
    # 什么也决定不了（QC 不判 run 死活）。声明在类型上的契约，就该在类型的写入口
    # **检测** —— 判决拆除（builtin:237 降格）：检测照跑，但不再拒写。放行后 KB
    # 一字不变，产物如实带 `phantom_claim_ids` 进 metadata（框架观测，模型自述
    # 不算）+ transcript 事件，引用完整性义务交 referee 终审（宪法 S2：「引用
    # 清单不一致，记录即可」）。检测结果本身可持久化，判决不必。
    _phantom = _phantom_citations(state, artifact_type, content)

    # Provider tool-call payloads occasionally encode an object argument as a
    # JSON string.  Accept that one interoperable form, but never persist an
    # arbitrary string/list/scalar as artifact metadata: downstream framework
    # code relies on metadata being a mapping.
    _metadata_given = metadata is not None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "error": "metadata string is not valid JSON",
            }
    if _metadata_given and not isinstance(metadata, dict):
        # schema 声明 type:object；JSON 字符串解出来也要是对象（"null" 也不算）——
        # 同一条件只答一次。
        return {"status": "error", "error": "metadata must be an object"}

    # 拼装清单是**框架的观测**，随产物走：这份内容由哪些文件按什么顺序构成，
    # 读者不用信模型自述（provenance-first）。模型自己也传了同名 key 时以框架
    # 观测为准 —— 这不是模型能声明的事实。
    if assembled_from:
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["content_assembled_from"] = assembled_from
    if _phantom:
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["phantom_claim_ids"] = list(_phantom)
        state.append_transcript(
            "phantom_citations_recorded",
            artifact_type=artifact_type, name=name,
            phantom_claim_ids=list(_phantom)[:50], n_phantom=len(_phantom),
        )

    # ── 这里曾有一道「架构节点不得新建 producing 节点专属类型」的闸（v3.2）──
    # 判决拆除（builtin:312 降格→O1）删了：那是资格/流程判决（S3）。放行后
    # `produced_by_node_type` 由框架盖章（模型写不了别人的章），账不假 —— 一份由
    # `_orchestrator` 名下产出的 manuscript 就是 orchestrator 代笔的 manuscript，
    # 「产物由非属主产出」由 referee 终审。唯一的 B 子项 review_critique 由
    # core/artifact_capabilities.TYPED_ONLY_ARTIFACT_OWNERS 单独守住（下面的
    # run_save_gate 会走到它），此处对它是重复抄件。

    # ── 类型自己声明的写入门（结构纪律，与冻结门对称）───────────────────────
    #
    # 放在这里而不是更早：门查的必须是**真正要落盘的那份** —— metadata 已归一化
    # 成 dict、走文件引用的 content 已经拼好（与上面引用诚信闸同一条理由）。
    #
    # 为什么类型契约不能只挂在冻结门上（2026-08-23 实测）：冻结是模型**自愿调用**
    # 的一个动作。一次真跑里模型 save_artifact 两次、从没调 freeze_artifact，
    # 于是 derivation_log 那道查"验证章只认工具落的"的门一次都没跑，带着
    # 自己手写的 `verification: "verified"` 交付，run 判 completed。
    # 门禁跟类型走、且要站在**必经之路**上 —— 写入是必经的，冻结不是。
    from shared.tools.library.artifacts_extra import run_save_gate

    _gate_error = await run_save_gate(
        state, {"type": artifact_type, "name": name,
                "metadata": metadata if isinstance(metadata, dict) else {},
                "content": content})
    if _gate_error is not None:
        return _gate_error

    try:
        result = state.save_artifact(
            artifact_type, name, content, metadata=metadata,
            amendment_reason=(amendment_reason or "").strip() or None,
        )
    except (ValueError, PermissionError) as exc:
        # 冻结修订的契约（ValueError）与 typed-only 产物的属主（PermissionError，
        # core/artifact_capabilities 的 B 墙）都必须送到调用方：报错原文已写明
        # 合法路径，照原样交回。
        return {"status": "error", "error": str(exc)}

    # ── 交付投影（2026-08-31）：用户点名的文件由框架写，模型不搬字节 ────────
    # run_node(deliverable={artifact_type, path}) 把契约放进 hook_state；
    # 这里是 save 的必经之路（与写入门同一条理由：冻结是自愿的，写入不是）。
    # 每次同 type 保存都重写投影 —— head 即当前，与 artifact 版本语义一致。
    # 投影失败要吵不要吞：账照落（success），失败原因进结果与 transcript。
    proj = (getattr(state, "hook_state", {}) or {}).get("_deliverable_projection")
    if proj and str(proj.get("artifact_type")) == str(artifact_type):
        _root = getattr(state, "project_worktree", None)
        if _root is None:
            result = {**result, "deliverable_projection_skipped":
                      "本 run 没绑 Project 工作区，投影无处可落"}
        else:
            try:
                from core.project_workspace import validate_deliverable_projection

                _rel = validate_deliverable_projection(
                    _root, str(state.node_type), str(proj.get("path") or ""))
                _target = Path(_root) / _rel
                _target.parent.mkdir(parents=True, exist_ok=True)
                _target.write_text(content, encoding="utf-8")
                state.append_transcript(
                    "deliverable_projected",
                    artifact=result.get("id"), path=_rel)
                result = {**result, "deliverable_projected_to": _rel}
            except Exception as exc:                       # noqa: BLE001
                state.append_transcript(
                    "deliverable_projection_failed",
                    artifact=result.get("id"),
                    error=f"{type(exc).__name__}: {exc}")
                result = {**result, "deliverable_projection_error":
                          f"{type(exc).__name__}: {exc}"}
    return {"status": "success", **result}


register_tool(
    ToolDefinition(
        name="save_artifact",
        description=(
            "保存一份 legacy typed artifact 记录。\n\n"
            "**Project v2**：这不是 Git 操作、不是节点交接门，也不是完成条件。"
            "正式产出、脚本、图表和说明应直接写进你拥有的 Project 目录；"
            "下游节点会直接读取这些文件，Platform 会在节点结束时统一 checkpoint。"
            "只有兼容旧流程或用户明确要求一份 typed artifact 记录时才调用本工具。\n\n"
            "**非 Project/legacy 运行**：仍可按 harness 声明保存 typed output，"
            "供 read_artifact 和旧 summary 流程使用。\n\n"
            "**Do NOT use when**：\n"
            "  - 跑过程的临时文件（log / plot / 中间 dump）→ 用 write_file 到任意路径\n"
            "  - user 个人偏好持久化 → 走 propose_profile_update\n"
            "  - 结构化研究主张 → 用 create_claim（KB）；**假设的承诺本体**是冻结的 "
            "pre_registration artifact，KB 里的 hypothesis claim 由 _curator 采纳时写入\n"
            "  - 短期工作笔记 → write_scratchpad；可复用的 observation/pitfall → memory_note\n"
            "  - **你是 _orchestrator（或其它架构节点）、想新建一份 producing 节点专属"
            "的 deliverable**（如 manuscript / survey_report / experiment_log / "
            "clean_results）→ 别代笔。这是 producing 节点的专属产出，"
            "`run_node(node_type=<对应节点>, ...)` 让它自己产。框架不拦，但记录上"
            "会如实盖你的章（produced_by_node_type），代笔的产物不过对应节点的 "
            "harness/qc/review，referee 终审看得见。\n\n"
            "**关键参数**：\n"
            "  - artifact_type：legacy 类型标签（如 'survey_report'）\n"
            "  - name：人类可读标签（会进文件名 <type>__<slug>.json），snake_case 短描述\n"
            "  - content：完整正文（markdown / JSON / 任意文本）。大二进制建议 write_file 后这里只存路径 + metadata\n\n"
            "**版本语义**：同 type+name = 同一个 artifact 身份。每次保存版本号 +1，"
            "旧版自动快照（可 /undo）。**修订已冻结的 artifact**：带 amendment_reason "
            "重发同一调用 —— 框架保留冻结版不可变快照、记录逐字段差异，修订稿为"
            "未冻结新版本，需重新 freeze_artifact 才能治理新实验。"
            "**不要换名另存修订稿** —— 那会制造平行身份，下游无法判断哪份有效。\n\n"
            "**返回**：`{status, id, path, version}`。**最常见 error**：覆盖已 frozen 的 "
            "artifact 且没带 amendment_reason → error（按报错提示带上理由重发）。"
            "正文引了 KB 里不存在的 claim id 不拒写，但 metadata.phantom_claim_ids "
            "会如实记下（引用完整性由 referee 终审）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_type": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "legacy snake_case 类型标签。"
                        "例：'survey_report' / 'pre_registration' / 'experiment_log' / 'analysis_report' / "
                        "'manuscript' / 'review_verdict' / 'dataset' / 'clean_results'。"
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "简短可读名字（会变文件名 slug）。例：'LJ_NVT_Best_Practices' / 'H1_thermal_steady_state'。"
                        "同一 run 内同 type+name 会覆盖（除非 frozen）。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "artifact 完整主体（markdown / JSON / 任意文本）。下游 LLM 会 read_artifact 直接读这一段。**长内容（>几 KB）别走这里** —— 用 content_from_file。",
                },
                "content_from_file": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": (
                        "**长产物的正确路径**：单个文件路径，或**有序路径列表**"
                        "（框架按列表顺序把各文件机械拼接成一份内容，拼装清单自动"
                        "记进 metadata.content_assembled_from）。内容由框架从磁盘"
                        "读入，一个字节不经过输出通道 —— 产物多大都与单轮输出 "
                        "token 上限无关。⚠️ write_file 是整文件**覆盖**语义，不能对"
                        "同一文件分段追加：要合成大内容，把各部分写成**多个文件**"
                        "再按顺序列在这里，不要试图往一个文件里续写。路径限本 run "
                        "/ 本项目目录树内。与 content 二选一。"
                    ),
                },
                "metadata": {
                    "type": "object",
                    "description": (
                        "可选 dict。常用 key：n_atoms / wall_time / source_paper_ids 等领域字段。"
                        "**不要直接设 frozen=true**，用 freeze_artifact 工具。"
                    ),
                },
                "amendment_reason": {
                    "type": "string",
                    "description": (
                        "仅在修订**已冻结**的 artifact 时必填：一句话说明为什么要改"
                        "已经承诺过的内容（如 'review 指出 H2 阈值缺乏文献依据，"
                        "改为 X'）。会与框架计算的逐字段差异一起进不可抵赖账本。"
                        "普通保存/覆盖不要传。"
                    ),
                },
            },
            "required": ["artifact_type", "name"],
        },
    ),
    _save_artifact,
)


#: 单次读**输出**上限（对 content 生效，无条件）。read_file / read_artifact
#: 共用同一条不变量：工具输出不随被读对象的大小无界增长。
_MAX_READ_BYTES = 256_000

#: read_artifact 的上限单独收紧。read_file 是模型**指名读哪几行**，256KB 是它
#: 自己要的；read_artifact 只给一个 id，模型事先并不知道会拿到多大的东西 ——
#: 按 256KB 放行，一次调用就是约 85k tokens，两次就把 262k 的窗口吃光。
#: 产物大的时候，这个工具的职责是让模型**看清它是什么**（结构、前几行、总量），
#: 然后把整份数据交给 execute_python 去算，而不是把它搬进对话。
_MAX_ARTIFACT_CHARS = 20_000


# ── read_artifact ────────────────────────────────────────────────────────────

async def _read_artifact(
    state: State, artifact_id: str, offset: int = 0, **_: Any,
) -> dict:
    """读一个 artifact，**输出有界**。

    工具输出不得随被读对象的大小无界增长 —— 这是和 KB 读取、`read_file` 同一条
    不变量，只是 read_artifact 一直没接上：一个 1.8 MB 的 V-T 平表就能让
    postprocess 的第一次 LLM 调用直接 HTTP 400（253953 input tokens，超 262144
    上限），整个 run 当场 error，而且报错指向"模型上下文超了"，看不出是哪个工具
    把它撑爆的（E2E v20 实测）。

    超限时不静默截断：明说截了多少、还剩多少、下一段的 offset，并且**指名**大
    数据的正确处理方式是把文件交给 execute_python，而不是先读进对话。
    """
    rec = state.read_artifact(artifact_id)
    if rec is None:
        return {"status": "error", "error": f"找不到 artifact {artifact_id!r}"}

    # 这行原来是内联的抄件，另外五处读 content 的地方都没有它 —— 现在同一个实现。
    # 这份产物能不能当确证证据用（#1097 第 5 条）。
    #
    # 挂在**读**这一侧，不是挂在各个下游节点里：一来判据只有一份（让每个节点各写
    # 一句 `metadata["upstream_goal_effect"] == …` 就是 N 份会各自演化的抄件），
    # 二来它能覆盖到我们改不了的那些节点 —— 谁读这份产物，谁就同时拿到这句话。
    from shared.lib.evidence_use import confirmatory_use_note

    _use_note = confirmatory_use_note(rec)

    text = artifact_text(rec)
    total = len(text)
    start = max(0, int(offset or 0))
    window = text[start:start + _MAX_ARTIFACT_CHARS]
    if start == 0 and total <= _MAX_ARTIFACT_CHARS:
        out = {"status": "success", "artifact": rec, "truncated": False}
        if _use_note:
            out["evidence_use"] = _use_note
        return out

    path = state.find_artifact_path(artifact_id)
    file_path = str(path) if path is not None else ""
    end = start + len(window)
    return {
        "status": "success",
        "artifact": {**rec, "content": window},
        **({"evidence_use": _use_note} if _use_note else {}),
        "truncated": True,
        "total_chars": total,
        "offset": start,
        "returned_chars": len(window),
        "next_offset": end if end < total else None,
        "file_path": file_path,
        "instruction": (
            f"这个 artifact 有 {total} 字符，本次只返回了 [{start}, {end})。"
            "**不要**靠反复调本工具把它读完 —— 那样只会把上下文撑爆。"
            + (
                f"要整份处理，请用 execute_python 直接读文件 {file_path}"
                "（文件本身就是正文，不再包 JSON 信封），在脚本里做聚合/抽样/绘图，"
                "只把结论带回对话。"
                if file_path else
                "要整份处理，请用 execute_python 读它的落盘文件，只把结论带回对话。"
            )
        ),
    }


register_tool(
    ToolDefinition(
        name="read_artifact",
        replayable_read=True,
        description=(
            "按 id 读取一个 artifact 的内容（id 来自 `list_artifacts` 的输出）。"
            "用来查看上游节点产出的内容。"
            f"**输出有上限**（{_MAX_ARTIFACT_CHARS} 字符）：超出会截断并返回 file_path，"
            "大数据请改用 execute_python 直接处理那个文件，不要读进对话。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "来自 list_artifacts 的 artifact id。"},
                "offset": {
                    "type": "integer",
                    "description": "从第几个字符开始读（默认 0）。仅用于定位，不要用它把大产物整份读完。",
                },
            },
            "required": ["artifact_id"],
        },
    ),
    _read_artifact,
)


# ── list_artifacts ───────────────────────────────────────────────────────────

async def _list_artifacts(state: State, artifact_type: str | None = None, **_: Any) -> dict:
    return {"status": "success", "artifacts": state.list_artifacts(artifact_type)}


register_tool(
    ToolDefinition(
        name="list_artifacts",
        replayable_read=True,
        description="列出当前 run 中所有 artifact，可按 type 过滤。",
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_type": {"type": "string", "description": "可选过滤项。"},
            },
        },
    ),
    _list_artifacts,
)


# v2.0：add_memory / search_memory / promote_memory / archive_memory /
# supersede_memory tool 全部已删除。详情见文件尾的迁移注释。
# Internal Python API (state.save_memory / state.search_memory) 暂保留供
# recall.py / executor.py 使用；update_memory_lifecycle 与 #375 迁移脚本已随
# 记忆系统重建退场（判决拆除·第三波）。


# ── run_bash ─────────────────────────────────────────────────────────────────

#: 模型 shell 里"我要去外面取东西"的形状。名单会漏新写法，所以它只用来**加一段
#: 解释**，从不用来拦截 —— 漏掉的那条照旧按普通失败返回，不会因此被误放行。
_ACQUISITION_SHAPES = (
    "curl ", "wget ", "git clone", "git fetch", "git ls-remote", "git pull",
    "pip install", "pip3 install", "npm install", "apt-get install", "apt install",
    "conda install", "uv pip install", "wget\t", "curl\t",
)

#: 隔离层拒绝连接时，这条通道上会出现的话。
_NETWORK_REFUSAL_SHAPES = (
    "permission denied", "couldn't connect", "could not connect",
    "connection refused", "network is unreachable", "operation not permitted",
    "failed to connect", "temporary failure in name resolution",
)


def _network_refusal_hint(cmd: str, out: str, err: str) -> dict | None:
    """这条命令是不是撞上了「模型 shell 永久无网」那堵墙 —— 是就说清楚。

    两个条件都要：命令看起来在往外取东西，且输出里有隔离层拒绝连接的形状。
    只命中一个不加解释 —— 一个本地 git 失败被贴上"去起 experiment"的建议，
    比不给建议更糟。
    """
    lowered = f"{cmd}".lower()
    if not any(shape in lowered for shape in _ACQUISITION_SHAPES):
        return None
    text = f"{out}\n{err}".lower()
    if not any(shape in text for shape in _NETWORK_REFUSAL_SHAPES):
        return None
    return {
        "error_code": "network_unavailable_in_model_shell",
        "network_access": False,
        # 这一条是关键：**没有**任何证据说明宿主断网。模型上一次就是在这里拐错的。
        "host_network_inferred": False,
        "recommended_node": "experiment",
        "recommended_tool": "fetch_resource",
        "safe_to_retry_same_tool": False,
        "error": (
            "这条 shell 按设计永远没有外网（模型命令的网络与项目数据不同框）——"
            "DNS 解析得开、TCP 连不上，所以 curl / wget / pip install / 远端 git "
            "在这里一定失败。**这不说明宿主没有外网**，换个写法、重试、换镜像都没用。\n"
            "要把源码 / 数据 / 权重 / 二进制落盘：起 experiment 节点，由它走 "
            "`describe_acquisition_capabilities` → `fetch_resource`（一次性边界里开网、"
            "核验 hash 后原子导入）。把 URL、tag/commit、hash 和验收要求作为 node_inputs "
            "交给它。\n"
            "只是想读网页内容：用 `web_fetch`。"
        ),
    }


async def _run_bash(state: State, cmd: str, timeout: int = 600,
                    cwd: str | None = None, **options: Any) -> dict:
    # cmd 非空 = schema minLength:1，派发口核（experiment 的 safe_bash 包装版
    # 自己先过注册表，同一道）。

    # v3.2（2026-07）：框架级高危命令拦截，覆盖所有用框架版 run_bash 的节点
    # （experiment 节点自制的 safe_bash.py 覆盖版不受影响，独立维护）。
    from shared.lib import dangerous_commands as _dc

    # ── 第 0 道：越界写入 = 硬拒（deny，不问人、不受 /bypass 影响）─────────
    # 与"高危确认"性质不同：shell 直写 artifacts/KB 没有合法场景（正确姿势
    # 永远是 save_artifact / create_claim），所以不是权限问题，是 validity
    # 错误 —— 直接报错指路。详见 dangerous_commands.py 顶部注释。
    boundary = _dc.match_boundary_violation(cmd, mode="shell")
    if boundary:
        state.append_transcript(
            "boundary_write_blocked", tool="run_bash",
            cmd_preview=cmd[:200], category=boundary)
        return {"status": "error",
                "error": _dc.BOUNDARY_DENY_MESSAGE.format(category=boundary)}

    # ── 第 0.5 道：探查专用模式（harness.shell_probe_only，v3.3）= 见证 ─────
    # 判决拆除（builtin:645 降格）：这里曾硬拒协调类节点的写文件/内联脚本/装软件。
    # 「协调者只许只读探查」是角色/资格判决（S3）；写边界真正的守卫在 spawn 层
    # 沙箱（core/sandbox：只可写本节点目录，OS 层面写不进别处），剩下的只是
    # 「不许替 producing 节点干活」的流程规则 —— 放行，如实见证
    # `probe_only_shell_violation` 进 transcript，由 referee 终审。
    _probe_witness: str | None = None
    if state.hook_state.get("_shell_probe_only"):
        _probe_witness = _dc.match_probe_only_violation(cmd)
        if _probe_witness:
            state.append_transcript(
                "probe_only_shell_violation", tool="run_bash",
                cmd_preview=cmd[:200], category=_probe_witness)

    category = _dc.match_high_risk(cmd, mode="shell")
    if category:
        if _dc.bypass_enabled():
            state.append_transcript(
                "highrisk_bash_bypass", cmd_preview=cmd[:200], category=category)
        elif _dc.is_confirmed(state, cmd):
            _dc.consume_confirmation(state, cmd)   # 一次性：用掉这次批准
            state.append_transcript(
                "highrisk_bash_confirmed_run", cmd_preview=cmd[:200], category=category)
        else:
            state.append_transcript(
                "highrisk_bash_blocked_pending_confirm",
                cmd_preview=cmd[:200], category=category)
            return _dc.build_pause_payload(
                state, tool="run_bash", text=cmd, category=category, preview=cmd)

    # 等**进程退出**、输出落文件 —— 不接管道。后台子孙攥着管道会让
    # communicate() 永远等不到 EOF（E2E-5 实测：服务 30 秒起好，调用方等了
    # 15.5 小时）。判据必须是"直接子进程退没退"，不是"管道断没断"。
    from shared.lib import output_spill as _spill
    from core.project_workspace import validate_tool_cwd

    try:
        effective_cwd = str(validate_tool_cwd(state, cwd))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    # 模型的 shell 关进写沙箱：只有本节点目录 + run-local + scratch 可写，
    # 别的节点目录在操作系统层面写不进去（core/sandbox.py，写时失败而非事后取证）。
    from core.sandbox import limits_for_profile, model_tool_roots

    _writable, _readonly = model_tool_roots(state)
    # 大输出不再只读尾部然后删文件 —— 完整输出本来就在临时文件里，超限时把它
    # 保留到 run 目录并把路径带回来（开头那段报错才拿得回）。
    _spill_paths: dict = {}
    status, rc, out_b, err_b = await spawn_and_wait(
        cmd, state=state, timeout=timeout, cwd=effective_cwd, shell=True,
        writable_roots=_writable, readonly_roots=_readonly,
        spill_dir=_spill.spill_root(state), spill=_spill_paths,
        sandbox_limits=limits_for_profile(
            options.get("resource_profile"), walltime_seconds=timeout
        ))
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    if status == "spawn_failed":
        return {"status": "error", "error": f"启动 shell 失败：{err[:300]}"}
    if status == "timeout":
        _r = {"status": "timeout", "cmd": cmd[:200], "timeout_s": timeout}
        _spill.attach_stream(_r, "stdout_tail", out, _spill_paths.get("stdout_path"))
        _spill.attach_stream(_r, "stderr_tail", err, _spill_paths.get("stderr_path"),
                             inline_limit=1500)
        _r.update({
            "safe_to_retry": False,
            "retry_guidance": (
                "The command may have partial writes. Inspect outputs before an explicit retry."
            ),
        })
        if "HARNESS_SANDBOX_LIMIT walltime" in err:
            _r.update({
                "error_code": "sandbox_resource_exhausted",
                "resource": "walltime",
            })
        return _r
    if status == "cancelled":
        _r = {"status": "cancelled", "cmd": cmd[:200], "returncode": rc}
        _spill.attach_stream(_r, "stdout_tail", out, _spill_paths.get("stdout_path"))
        _spill.attach_stream(_r, "stderr_tail", err, _spill_paths.get("stderr_path"),
                             inline_limit=1500)
        return _r

    result = {
        "status": "success" if rc == 0 else "error",
        "cmd": cmd[:200],
        "returncode": rc,
    }
    if _probe_witness:
        result["probe_only_shell_violation"] = _probe_witness
    # 超限不丢：全文落盘，回显头尾 + 取回路径。求解器的报错常在**开头**
    # （参数校验 / 网格读取失败），只留尾部等于把最有用的一段扔了。
    _spill.attach_stream(result, "stdout_tail", out, _spill_paths.get("stdout_path"))
    _spill.attach_stream(result, "stderr_tail", err, _spill_paths.get("stderr_path"),
                         inline_limit=1500)
    # envelope 的 `error` 是**给人看的那一句**。不写的话下游只有
    # "Tool execution failed"，命令到底为什么挂的没人看得到（见 core/tool_errors）。
    if rc != 0:
        from core import tool_errors as _errs

        result["error_code"] = _errs.COMMAND_FAILED
        result["error"] = _errs.command_failure_note(rc, result.get("stderr_tail"))
    if rc in {125, 126, 137, 138} and "HARNESS_SANDBOX_LIMIT" in err:
        resource = {
            125: "storage",
            126: "output",
            137: "memory",
            138: "pids",
        }[int(rc)]
        result.update({
            "error_code": "sandbox_resource_exhausted",
            "resource": resource,
            "safe_to_retry": False,
            "retry_guidance": (
                "Do not rerun this command automatically: it may have partial writes. "
                "Inspect outputs, then explicitly resume or choose a larger initial resource_profile."
            ),
        })
    # ── 撞上"模型 shell 永久无网"那堵墙时，说清楚是哪堵墙（#997）────────
    #
    # 活体：用户说"安装 WRF"，父编排器用 run_bash 做 curl / git ls-remote 预检，
    # 得到 DNS 解析正常但 TCP 443 `Permission denied`。它据此判定**这台机器没有
    # 外网**，反复要用户上传源码或提供镜像；而宿主控制面同一时刻 `git ls-remote`
    # 成功。它不是判断失误 —— 返回值里没有任何东西能让它分辨「宿主断网」和
    # 「这条通道按设计无网」。
    #
    # 只给 `http_code=000` 或一个非零退出码，模型只能靠猜，而猜错的方向恰好是
    # 「让人去旁路」。把能力事实和合法下一步结构化地放回去。
    if rc != 0:
        _egress = _network_refusal_hint(cmd, out, err)
        if _egress:
            result.update(_egress)
            state.append_transcript(
                "model_shell_network_refusal", cmd_preview=cmd[:200])

    # 起了后台进程却没登记作业 -> 提醒进**工具返回值**（模型下一轮必然看到）。
    # 只提醒不拦截：起临时 http server / tail -f 这类合法用途很多，一刀切会误伤。
    # 但提醒必须走这条路 —— 只写在 harness 里没人读的规则，今天已经数出七次了。
    try:
        from core import jobs as _jobs
        hint = _jobs.undeclared_launch_hint(state, cmd)
        if hint:
            result["job_registry_reminder"] = hint
            state.append_transcript("undeclared_background_launch", cmd=cmd[:200])
    except Exception:      # 登记表出任何问题都不该影响命令本身
        pass
    return result


register_tool(
    ToolDefinition(
        name="run_bash",
        description=(
            "执行一条 shell 命令并返回 stdout / stderr 的尾部 + returncode。"
            "适合调任何命令行工具（OpenFOAM / LAMMPS / Gaussian / 本地 git / sed / awk / ...）。"
            "\n\n⛔ **这条 shell 永远没有外网**（按设计；模型命令的网络与项目数据不同框）。"
            "DNS 解析得开，TCP 连接会被隔离层拒绝 —— 所以 `curl` / `wget` / "
            "`pip install` / `git clone|fetch|ls-remote` 远端仓库在这里**一定失败**，"
            "而且失败长得像「这台机器断网了」。它不是：宿主有网，是这条通道没有。"
            "\n· 只想读网页内容 → 用 `web_fetch`。"
            "\n· 要把源码 / 数据 / 权重 / 二进制**落盘** → 起 experiment 节点，由它走"
            "`describe_acquisition_capabilities` → `fetch_resource`（那条路在一次性边界里"
            "开网、下载后核验 hash 再原子导入）。把 URL、tag/commit、hash 和验收要求"
            "作为 node_inputs 交给它，**别先自己 curl 一把再说**。"
            "\n· 本地 git（status / log / diff / 本地仓库操作）照常可用。"
            "默认 timeout 600 秒；可改 cwd 限定工作目录。"
            "同一 RunAttempt 内前台命令严格 FIFO 串行；每次 cwd 都是显式权威，"
            "上一条命令的 cd/export/后台进程不会成为下一条命令的隐藏状态。"
            "可在首次调用选择 resource_profile；运行中接近资源上限会自动扩容到"
            "冻结天花板，绝不自动重跑已有部分写入的命令。"
            "拼错或失败时返回非零 returncode 和 stderr_tail —— 据此自行纠错。"
            "输出较短时全文返回；超长时自动全文落盘，返回开头+结尾摘录和 "
            "`stdout_tail_path`，用 read_file 读该路径可取回完整输出（不会丢）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要执行的 shell 命令（会以 /bin/sh -c 执行；可以用管道、重定向、&& 等）。",
                },
                "timeout": {
                    "type": "integer",
                    "default": 600,
                    "minimum": 1,
                    "maximum": 14400,
                    "description": "墙钟超时秒数。超时会 kill 子进程。",
                },
                "cwd": {
                    "type": "string",
                    "description": "可选：命令的工作目录。不传则用 runner 的 cwd。",
                },
                "resource_profile": {
                    "type": "string",
                    "enum": ["small", "standard", "large", "xlarge"],
                    "default": "standard",
                    "description": (
                        "首次命令的初始容量选择；small=1GiB/1CPU，standard=4GiB/2CPU，"
                        "large=16GiB/4CPU，xlarge=32GiB/8CPU。接近上限时框架自动扩容，"
                        "但不会在资源失败后盲目重跑。"
                    ),
                },
            },
            "required": ["cmd"],
        },
        allowed_node_types=None,            # 任何节点都可用（但 harness yaml 的 tools 白名单决定实际授权）
        risk_level="high",                   # 任意 shell 执行 —— 提醒 owner 谨慎
    ),
    _run_bash,
)


# ── query_budget ─────────────────────────────────────────────────────────────

async def _query_budget(state: State, **_: Any) -> dict:
    used = state.tokens_used
    limit = state.tokens_limit
    if limit <= 0:
        # 无限制模式（开发阶段默认）
        return {
            "status": "success",
            "tokens_used": used,
            "tokens_limit": "unlimited",
            "tool_calls_made": state.tool_calls_made,
            "note": "本次 run 未设 token 上限（开发模式）。生产环境用 state.tokens_limit > 0 来启用软上限。",
        }
    pct = used / limit * 100
    return {
        "status": "success",
        "tokens_used": used,
        "tokens_limit": limit,
        "tokens_remaining": max(0, limit - used),
        "pct_used": round(pct, 1),
        "tool_calls_made": state.tool_calls_made,
        "warning": (
            "已超预算！应当尽快收尾并 save_artifact。"
            if pct >= 100 else
            "接近预算上限（>80%）—— 注意节流。"
            if pct >= 80 else
            None
        ),
    }


register_tool(
    ToolDefinition(
        name="query_budget",
        description=(
            "查询本次 run 已用的 LLM token 数 + 上限 + 百分比。"
            "当你不确定还能跑多少轮、或者节点出现循环倾向时调用。"
            "上限默认 1M tokens；超 80% 会返回 warning 提示。"
        ),
        parameters_schema={"type": "object", "properties": {}},
        risk_level="low",
    ),
    _query_budget,
)


# ── request_human_input ──────────────────────────────────────────────────────

def _normalize_options(options: Any) -> tuple[list[dict], str]:
    """把 options 规范成 [{label, description}]；返回 (规范化结果, 错误说明)。

    兼容旧的纯字符串数组（历史 harness 还这么写），但**新契约要求带
    description** —— 没有它，人在 UI 上只能看见几个词，判断不了后果。
    """
    if options is None:
        return [], ""
    if not isinstance(options, list):
        return [], "options 必须是数组"     # 非空归 schema minItems，派发口核
    out: list[dict] = []
    for i, item in enumerate(options):
        # id 是选项的身份。调用方给了就用它（语义 id 更好读），没给就按位次派一个
        # 稳定值 —— 位次在**这一次呈递内**足以作为身份：选项集一变 offer_id 就变，
        # 对着旧菜单的答复会被拒绝而不是被沿用。
        #
        # 此前这里产出的是 {label, description}，没有 id —— 那是"选项只有文案、
        # 没有身份"的源头之一（decision_offer 模块头列的抄件 #1）。
        if isinstance(item, str):
            out.append({"id": f"option_{i + 1}", "label": item, "description": ""})
        elif isinstance(item, dict) and str(item.get("label") or "").strip():
            given = str(item.get("id") or "").strip()
            out.append({
                "id": given or f"option_{i + 1}",
                "label": str(item["label"]).strip(),
                "description": str(item.get("description") or "").strip(),
            })
        else:
            return [], f"options[{i}] 需要 {{label, description}}"
    return out, ""


async def _request_human_input(state: State, question: str,
                               context: str = "",
                               options: Any = None,
                               header: str = "",
                               recommended_option_index: int | None = None,
                               **_: Any) -> dict:
    """生成 pause 事件，让 agent_loop 检测到后 unwind 给上层（chat.py）。

    本工具**不阻塞 stdin**：返回结构化 pause 事件，由调用方决定怎么问 user
    （CLI 用 stdin / web 用 WebSocket / autonomous 模式用预设答案）。

    返回 `{"status": "pause", "pause_event": {...}}`：
        agent_loop 看到 status=="pause" 就 unwind 到调用方；
        run_node 工具看到子 run 是 pause 状态也跟着 unwind 冒泡。

    工具本身只负责事件**生成**；I/O 处理在调用方。

    bypass 模式（`--bypass-permissions` / `HARNESS_BYPASS_DANGEROUS_COMMANDS=1`）
    下不 pause——bypass 的语义是"完全无人值守"，如果这里仍然 pause，无人值守
    的 run 照样会卡住等人（issue #111）。bypass 时直接给出 best-judgment 默认
    答案（有 options 选第一个），status=success 放行，同时写审计事件，事后
    可追溯 agent 在哪些问题上是"被代答"的，而非真人拍板。
    """
    # 判决拆除（builtin:957 降格）：这里曾在「有正式 decision package 等人」时拒绝
    # 通用提问。提问本身不改 package 的状态（它仍是 awaiting_human，REVISE→PROCEED
    # 的风险由 package 自身状态守住，不靠拒绝提问）。现在放行，但把等人的
    # package 清单附进 pause 事件与 transcript，人和 referee 都看得见。
    pending_decisions = [{
        "producing_node": entry.get("producing_node"),
        "producing_run_id": entry.get("producing_run_id"),
        "recommended_action": entry.get("decision_recommended_action"),
    } for entry in (state.hook_state.get("pending_post_node_flow") or [])
        if isinstance(entry, dict) and entry.get("decision_state") == "awaiting_human"]
    if pending_decisions:
        state.append_transcript(
            "human_input_requested_while_decision_pending",
            question_preview=(question or "")[:200],
            pending_decisions=pending_decisions,
        )

    normalized, opt_error = _normalize_options(options)
    if opt_error:
        return {"status": "error", "error": opt_error}

    # 契约校验：给了选项就必须给推荐项。**报错要给合法出口**，别只说"不许"。
    if normalized and recommended_option_index is None:
        return {
            "status": "error",
            "error": (
                "给了 options 就必须给 recommended_option_index（0 起）。"
                "无人值守模式按它自动作答 —— 没有推荐项的提问在无人值守下"
                "只能靠猜，等于让研究停摆。\n"
                "两条出路：① 想清楚哪个最优，带上 recommended_option_index 重问；"
                "② 如果确实没有能推进的选项，那就不是提问而是阻塞 —— 改调 "
                "report_blocker 如实登记。"
            ),
            "options_given": len(normalized),
        }
    if normalized:
        if not 0 <= int(recommended_option_index) < len(normalized):
            return {
                "status": "error",
                "error": (
                    f"recommended_option_index={recommended_option_index} 越界，"
                    f"合法范围 0..{len(normalized) - 1}"
                ),
            }
        # 这里曾有一张「等待/wait/hold」关键词表替模型判推荐项算不算推进
        # （_looks_stalling）。判决拆除（builtin:1000 D）删了：硬编码审美阈值，
        # 「wait for job then analyze」会误伤；推荐什么是判断，判断归模型。

    from shared.lib import dangerous_commands as _dc
    if _dc.bypass_enabled():
        idx = int(recommended_option_index) if normalized else None
        default_answer = normalized[idx]["label"] if normalized else (
            "bypass mode: no human response available; proceed with best judgment.")
        state.append_transcript(
            "human_input_bypassed", question=question,
            n_options=len(normalized),
            selected_option_index=idx,
            response_preview=default_answer[:200])
        return {
            "status": "success",
            "response": default_answer,
            "bypassed": True,
            "asked_by": state.node_type,
            **({"pending_decisions": pending_decisions} if pending_decisions else {}),
        }

    state.append_transcript("human_input_requested", question=question,
                              n_options=len(normalized),
                              recommended_option_index=recommended_option_index)

    # 有选项 = 一次呈递。走 Offer，而不是在这里第二次构造选项集 —— 这条路此前
    # 产出无 id 的 {label, description}，于是同一个平台上存在两种 pause：一种
    # 选项有身份（decision package），一种没有（这里）。下游只好两种都兼容，
    # 兼容代码就是漏字段的地方。
    if normalized:
        from uuid import uuid4

        from core.decision_offer import Choice, Offer, PAUSE_OFFER_KEY

        choices = tuple(
            Choice(id=o["id"], label=o["label"], description=o.get("description") or "")
            for o in normalized
        )
        offer = Offer(
            decision_id=f"{state.run_id}:q{uuid4().hex[:8]}",
            kind="structured_question",
            question=question,
            choices=choices,
            context=context,
            recommended_id=choices[recommended_option_index].id,
            facts={"header": (header or "")[:12]},
        )
        pause_payload = offer.to_pause_payload()
        pause_event = {
            **pause_payload,                 # 整份摊开，不挑字段
            PAUSE_OFFER_KEY: pause_payload,  # 可整体搬运的那份
            "header": (header or "")[:12],
            "asking_node_type": state.node_type,
            "asking_run_id": state.run_id,
            "metadata": {
                "type": "structured_question",
                "header": (header or "")[:12],
            },
        }
    else:
        # 自由文本问答：没有选项集，也就没有呈递可言。
        pause_event = {
            "question": question,
            "context": context,
            "header": (header or "")[:12],
            "asking_node_type": state.node_type,
            "asking_run_id": state.run_id,
            "metadata": {"type": "structured_question", "header": (header or "")[:12]},
        }
    if pending_decisions:
        pause_event["pending_decisions"] = pending_decisions
    return {"status": "pause", "pause_event": pause_event}


register_tool(
    ToolDefinition(
        name="request_human_input",
        description=(
            "在节点执行中暂停，向人类用户提问并等待回答。"
            "适合：研究方向决策、超出节点权限的判断、需要领域专家确认。"
            "不适合：日常进度更新、能自己决定的事 —— 那些应该自己做。"
            "给 options 时**必须**同时给 recommended_option_index —— 无人值守模式"
            "按它自动作答；给不出推荐说明问题本身没想清楚。"
            "选项必须都是能推进的动作，不要放「等待/先不动」——那会让无人值守停摆。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "具体的决策问题。"},
                "context": {"type": "string", "description": "给人类看的背景。"},
                "header": {
                    "type": "string",
                    "description": "≤12 字的短标签，UI 上做 chip 用（如「采样方案」「预算」）。",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {
                        # 历史 harness 传纯字符串数组，代码兼容；契约照实声明
                        "type": ["object", "string"],
                        "properties": {
                            "label": {"type": "string", "description": "1-5 个词的选项名。"},
                            "description": {
                                "type": "string",
                                "description": "选它意味着什么、代价是什么 —— 人要靠这句判断。",
                            },
                        },
                        "required": ["label", "description"],
                    },
                    "description": (
                        "2-5 个**具体动作**选项。每项必须带 description 说明后果。"
                        "不要放「等待」「先不动」这类不推进的选项 —— 无人值守时它们"
                        "会让研究停摆；真的无路可走就别问，直接 report_blocker。"
                        "（人始终可以自由作答，不必受选项限制。）"
                    ),
                },
                "recommended_option_index": {
                    "type": "integer",
                    "description": (
                        "**给了 options 就必须给**：你推荐第几个（0 起）。"
                        "无人值守模式按它自动作答 —— 给不出推荐，说明这个问题你"
                        "自己也没想清楚，先想清楚再问。"
                    ),
                },
            },
            "required": ["question"],
        },
        risk_level="medium",
    ),
    _request_human_input,
)


# ── 出网授权：撞墙时问人，别只打印一句"去改环境变量"（#770）──────────────────


async def _request_network_access(state: State, host: str = "", reason: str = "",
                                  what_for: str = "", redirect_of: str = "",
                                  **_: Any) -> dict:
    # `**_`：工具注册表会扫这件事 —— 没有兜底的话，LLM 多传一个字段就是 TypeError，
    # 而那会把一次本来能成的授权变成一次工具崩溃。
    from core.capability_grants import is_granted, request_network_grant

    host = str(host or "").strip()
    if not host:
        return {"status": "error",
                "error": "request_network_access 需要 host（或一整条 URL）"}
    if is_granted(state, host):
        return {
            "status": "success",
            "already_granted": True,
            "message": f"{host} 在本次 run 里已经批过了，直接取即可。",
        }
    return request_network_grant(state, host, reason, what_for=what_for,
                                 redirect_of=redirect_of)


register_tool(
    ToolDefinition(
        name="request_network_access",
        description=(
            "撞到出网白名单时，把「要连哪个主机、为什么」呈给用户，等他点允许。"
            "批准**只在本次 run 内有效**：不写环境变量、不落盘、不含子域。"
            "适合：需要某个科学数据源 —— 内置白名单里一个都没有，全是装软件用的。"
            "不适合：装依赖（pypi / github 本来就在白名单里）。"
            "被拒绝就换一条路或如实 report_blocker，不要反复问同一个域。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "主机名或一整条 URL（会自己取主机部分）。",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "为什么这一步需要连它 —— 人就靠这句判断。"
                        "别写「需要访问网络」这种没有信息的话。"
                    ),
                },
                "what_for": {
                    "type": "string",
                    "description": "要取的具体东西（数据集名、文件、API 端点）。",
                },
                "redirect_of": {
                    "type": "string",
                    "description": (
                        "如果是被某个**已授权**的域 302 到这里的，填那个域。"
                        "实测活例：huggingface.co 的权重文件会重定向到 "
                        "us.aws.cdn.hf.co —— 声明得再完整也只有跑到那一步才知道。"
                    ),
                },
            },
            "required": ["host", "reason"],
        },
        risk_level="medium",
    ),
    _request_network_access,
)



# ── read_file ────────────────────────────────────────────────────────────────

_DEFAULT_LIMIT_LINES = 2000


def _dir_listing(directory, *, limit: int = 40) -> str:
    """把一个目录的内容渲染成一行 —— **空目录也必须有话说**。

    2026-08-21 事故的点火点就在这里的旧写法 `if entries:` 上：curator 去读
    `.research/orchestration/artifacts/curator_dreaming_report.md`，那个目录当时
    是**空的**，于是这条提示被静默跳过，模型拿到的是光秃秃一句"找不到文件"——
    连"这个目录是空的"都没告诉它。它只能继续猜同一个名字，连猜 64 次。

    "该目录存在且为空"是一个**确定的、可行动的事实**：它一次就排除掉整棵子树。
    把它咽下去，等于把模型唯一的破局信息扣住了。
    """
    try:
        names = sorted(x.name + ("/" if x.is_dir() else "") for x in directory.iterdir())
    except Exception as e:
        return f"{directory} 无法列出（{type(e).__name__}）"
    if not names:
        return f"{directory} **存在但是空的**（0 个条目）—— 你要找的东西不在这里，换个地方找。"
    shown = names[:limit]
    more = f"…（共 {len(names)} 项，只列前 {limit} 项）" if len(names) > limit else ""
    return f"{directory} 下现有（共 {len(names)} 项）：{', '.join(shown)}{more}"


def _missing_file_hint(state, abs_path) -> str:
    """文件不存在时，把**真实存在的邻居**列出来。

    E2E v18 实测：agent 反复去 `.research/orchestration/experiment/README.md`、
    `.research/orchestration/MEMORY.md` 找东西 —— 真实位置是工作区根下的
    `experiments/README.md` 和 `MEMORY.md`。报错只说"找不到 <路径>"，它只能
    继续猜，连挂 5 次。

    项目铁律是"报错必须列出正确答案"。这里照做：给出最近的**存在的**祖先目录
    下现有什么，以及同名文件在工作区里的真实位置。

    ⚠️ 三个出口都不许静默（2026-08-21 事故）：空目录要说"空"，找不到同名文件要
    说"整个工作区都没有"。**"我查了，没有"和"我没查"对模型是天差地别的两件事**，
    而旧实现把前者渲染成了后者 —— 两个 `if xxx:` 一言不发地跳过去。
    """
    from pathlib import Path

    hints: list[str] = []
    try:
        ancestor = abs_path.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if ancestor.exists() and ancestor.is_dir():
            hints.append(_dir_listing(ancestor))
    except Exception:
        pass

    # ⚠️ 这里必须用 project_worktree，不能用 workspace_root。
    #
    # `state.workspace_root` 的名字骗人：它是**节点自己的目录**
    # （core/project_workspace.bind_project_workspace 把它设成 root/<owned>），
    # 真正的工作区根是 `project_worktree`。
    #
    # 这条提示的职责是**把模型从错误的坐标系里拽出来** —— 它触发的时刻，
    # 恰恰是"按自己目录解析"已经出错的时刻。若搜索也限定在自己目录里，
    # 输出就是同义反复。v22 实测原话：`工作区里叫 artifacts 的文件在：artifacts`。
    # 89 次路径失败一次都没被纠正过，根因就是这里：**唯一能破框的机制自己在框里。**
    #
    # 给绝对路径，不给相对路径：相对于谁，正是模型此刻搞错的那件事。
    try:
        root = getattr(state, "project_worktree", None) or getattr(state, "workspace_root", None)
        if root:
            found = [str(m) for m in list(Path(root).rglob(abs_path.name))[:8]
                     if ".git" not in m.parts and m != abs_path]
            if found:
                hints.append(f"工作区里叫 {abs_path.name} 的在（绝对路径，可直接用）："
                             + ", ".join(found))
            else:
                # 空结果是**结论**，不是"没查到东西所以别说话"。说出来，模型才
                # 知道这个名字整棵树都没有，不必再换个目录试同一个名字。
                hints.append(f"整个工作区里**没有任何**叫 {abs_path.name} 的文件 —— "
                             f"这个名字是错的（或者这个文件还没被创建），"
                             f"换个名字或先用 list_files 看清楚有什么。")
    except Exception:
        pass

    return ("\n" + "\n".join(hints)) if hints else ""


async def _read_file(state: State, path: str, offset: int = 0,
                     limit: int | None = None, **_: Any) -> dict:
    """读文件，输出**有界**。

    有界读原则（同 KB 的 find/read 分离）：工具输出的大小必须与被读对象的
    大小无关。行寻址保证不了这一点 —— 2026-08-04 实测：模型对一个
    1,999,210 bytes / 0 换行的 JSON 调 `read_file(limit=5)`（要 5 行），
    旧实现返回全部 2MB，context 冲到 625k；压缩把 85 条消息压成 13 条却只
    降到 610k，因为体积全在**一条**消息里，压缩只能删消息不能切消息。

    所以字节上限对 content 无条件生效，不再"给了 limit 就关掉"。截断时
    `bytes_capped=True` 且 `partial_line` 说明最后一行是否被切断 —— 模型
    必须能分辨"读完了"和"读到上限了"。
    """
    abs_path = _resolve_path(state, path)
    # issue #279：/proc/self/environ 之类是"整个进程的秘密"的直通车，不是研究
    # 数据。这是第一道；真正的兜底是 tool_registry 出口的值脱敏（换个工具
    # `cat` 一下就绕过路径检查了）。拒绝理由里绝不带文件内容。
    from core.secrets import is_sensitive_path
    denied = is_sensitive_path(abs_path)
    if denied:
        state.append_transcript("sensitive_path_read_denied",
                                path=str(abs_path), tool="read_file")
        return {"status": "error", "error": denied}
    if not abs_path.exists():
        return {"status": "error",
                "error": f"找不到文件：{abs_path}{_missing_file_hint(state, abs_path)}"}
    if not abs_path.is_file():
        # 2026-08-21 事故里这条撞了 18 次，每次烧掉一整轮 LLM：框架此刻**已经知道
        # 答案**（它就是个目录，一次 iterdir 的事），却只回一句"不是文件"，让模型
        # 再花一轮去猜子项名字。报错必须列出正确答案 —— 这里把清单直接给足，
        # 模型下一轮就能读对的路径。
        #
        # 仍然盖 error 章：它要的那次"读文件"确实没做成，本层无权把它说成成功。
        return {
            "status": "error",
            "error": (f"{abs_path} 是**目录**，不是文件。read_file 只能读文件。\n"
                      f"{_dir_listing(abs_path)}\n"
                      f"要读其中某个文件，直接 read_file 那个文件的完整路径；"
                      f"要继续往下逛用 list_files。"),
        }

    start = max(0, int(offset))
    max_lines = _DEFAULT_LIMIT_LINES if limit is None else max(1, int(limit))

    try:
        size = abs_path.stat().st_size
        selected: list[str] = []
        out_bytes = 0
        total_lines = 0
        bytes_capped = False
        partial_line = False
        # 逐行流式读，不再 read_text() 整个吞进内存 —— 18MB 的 jsonl 也只在
        # 内存里留下最多 _MAX_READ_BYTES 的输出。
        with abs_path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, raw_line in enumerate(fh):
                total_lines = idx + 1
                if idx < start or bytes_capped or len(selected) >= max_lines:
                    continue          # 继续扫只为把 total_lines 数完
                line = raw_line.rstrip("\n")
                prefix = f"{idx + 1}\t"
                sep = 1 if selected else 0          # join 用的 "\n" 也占字节
                cost = sep + len(prefix) + len(line.encode("utf-8"))
                if out_bytes + cost > _MAX_READ_BYTES:
                    if selected:
                        # 已经有整行了 —— 别把这行切一半，留给下一页整着读。
                        # 切开会让 next_offset 指向下一行，这行的后半永远读不到。
                        bytes_capped = True
                        continue
                    # 第一行自己就超预算：只能切，并如实说"这行没读完"
                    room = _MAX_READ_BYTES - len(prefix)
                    line = line.encode("utf-8")[:max(0, room)].decode("utf-8", errors="ignore")
                    bytes_capped = partial_line = True
                    cost = len(prefix) + len(line.encode("utf-8"))
                selected.append(prefix + line)
                out_bytes += cost
    except Exception as e:
        return {"status": "error", "error": f"读文件失败：{type(e).__name__}: {e}"}

    # 带行号返回（Claude Code 风格：`{N}\t{content}`），方便 LLM 引用行号做 edit
    numbered = "\n".join(selected)
    end = start + len(selected)

    # 标记为已读，write/edit 才能继续
    state.files_read.add(str(abs_path))

    result = {
        "status": "success",
        "path": str(abs_path),
        "content": numbered,
        "lines_returned": len(selected),
        "total_lines": total_lines,
        "size_bytes": size,
        "truncated": end < total_lines or bytes_capped,
        "next_offset": end if end < total_lines else None,
    }
    if bytes_capped:
        result["bytes_capped"] = True
        result["partial_line"] = partial_line
        if partial_line:
            # 单行超上限：行寻址翻不了页。说清"这行的后半你再也拿不到了"，
            # 否则模型会以为 next_offset 能把它补齐 —— 那是静默丢内容。
            result["note"] = (
                f"第 {end} 行本身超过单次输出上限 {_MAX_READ_BYTES} bytes，已截断"
                f"（文件共 {size} bytes）。**这一行的剩余部分不会出现在后续任何"
                f"分页里** —— 行寻址翻不动这种文件。要完整内容请用 run_bash 里的 "
                f"jq/python 抽字段，或先把它拆成多行。"
            )
        else:
            result["note"] = (
                f"输出到达单次上限 {_MAX_READ_BYTES} bytes，只返回了 {len(selected)} 行。"
                f"用 offset={end} 接着读。"
            )
    return result


register_tool(
    ToolDefinition(
        name="read_file",
        replayable_read=True,
        description=(
            "读文件内容（带行号），按需 offset + limit 分段。"
            "默认返回前 2000 行，且**输出恒 ≤ 256KB**（与文件大小无关）。"
            "到上限时 truncated/bytes_capped=true；partial_line=true 表示最后一行"
            "本身就超上限（单行巨型 JSON），这种文件行寻址翻不动，改用 run_bash 里的 jq/python。"
            "返回的 content 每行格式 `{行号}\\t{内容}`，方便后续 edit_file 引用。"
            "路径相对 state.root（即 output/{run_id}/）解释；可传绝对路径。"
            "⚠️ write_file / edit_file 对已存在文件**要求**先 read_file 一次。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对 state.root 或绝对）。"},
                "offset": {"type": "integer", "default": 0, "minimum": 0,
                           "description": "从第几行（0-indexed）开始读。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000,
                          "description": "最多读多少行。默认 2000。"},
            },
            "required": ["path"],
        },
        risk_level="low",
    ),
    _read_file,
)


# ── list_files / search_files（P6：通用文件工具，artifact CRUD 的替身）────────
#
# P6 前提：不是每个节点都有 bash。删 list_artifacts / search_artifacts 之前，
# 枚举和检索必须有通用工具兜住，边界与 read_file 完全一致（读全 worktree、
# 输出有界、敏感路径拒绝）。

#: 遍历时剪掉的目录名。与 project_repository._frozen_register 同一份判断：
#: 这些目录里没有模型该找的东西，逐层爬只会烧 IO 和输出预算。
_TRAVERSAL_PRUNE = frozenset({".git", "cache", "node_modules", "__pycache__", ".venv"})


def _iter_files_bounded(root, glob_pattern: str | None, cap: int):
    """有界遍历：最多返回 cap 个文件路径，剪掉 _TRAVERSAL_PRUNE。

    返回 (paths, scanned_all)。scanned_all=False 表示还有没看完的 —— 调用方
    必须把这一点告诉模型，"列到上限"和"列完了"是两个不同的事实。
    """
    import fnmatch as _fn

    out: list = []
    scanned_all = True
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in _TRAVERSAL_PRUNE:
                    stack.append(entry.path)
                continue
            rel = os.path.relpath(entry.path, root)
            if glob_pattern and not _fn.fnmatch(rel, glob_pattern):
                continue
            if len(out) >= cap:
                scanned_all = False
                return out, scanned_all
            out.append(entry.path)
    return out, scanned_all


async def _list_files(state: State, path: str = ".", glob: str | None = None,
                      max_entries: int = 200, **_: Any) -> dict:
    """列目录 / 按 glob 找文件。输出有界（max_entries 封顶）。"""
    abs_path = _resolve_path(state, path)
    from core.secrets import is_sensitive_path
    denied = is_sensitive_path(abs_path)
    if denied:
        return {"status": "error", "error": denied}
    if not abs_path.exists():
        return {"status": "error",
                "error": f"找不到目录：{abs_path}{_missing_file_hint(state, abs_path)}"}
    cap = max(1, min(int(max_entries), 1000))
    if abs_path.is_file():
        st = abs_path.stat()
        return {"status": "success", "entries": [
            {"path": str(abs_path), "size_bytes": st.st_size}], "truncated": False}
    if glob:
        files, scanned_all = _iter_files_bounded(abs_path, glob, cap)
        entries = []
        for f in files:
            try:
                entries.append({"path": os.path.relpath(f, abs_path),
                                "size_bytes": os.path.getsize(f)})
            except OSError:
                continue
        return {"status": "success", "root": str(abs_path),
                "entries": entries, "truncated": not scanned_all}
    try:
        children = sorted(os.scandir(abs_path), key=lambda e: e.name)
    except OSError as e:
        return {"status": "error", "error": f"读目录失败：{type(e).__name__}: {e}"}
    entries = []
    truncated = False
    for entry in children:
        if len(entries) >= cap:
            truncated = True
            break
        if entry.is_dir(follow_symlinks=False):
            entries.append({"path": entry.name + "/", "dir": True})
        else:
            try:
                entries.append({"path": entry.name,
                                "size_bytes": entry.stat().st_size})
            except OSError:
                continue
    return {"status": "success", "root": str(abs_path),
            "entries": entries, "truncated": truncated}


async def _search_files(state: State, pattern: str, path: str = ".",
                        glob: str | None = None, max_matches: int = 50,
                        ignore_case: bool = False, **_: Any) -> dict:
    """在文件里按正则找行。输出有界：命中数、单行长度、总字节都封顶。"""
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return {"status": "error", "error": f"正则不合法：{e}"}
    abs_path = _resolve_path(state, path)
    from core.secrets import is_sensitive_path
    if not abs_path.exists():
        return {"status": "error",
                "error": f"找不到路径：{abs_path}{_missing_file_hint(state, abs_path)}"}
    cap = max(1, min(int(max_matches), 500))
    files, scanned_all = (
        ([str(abs_path)], True) if abs_path.is_file()
        else _iter_files_bounded(abs_path, glob, 2000)
    )
    matches: list[dict] = []
    out_bytes = 0
    skipped_sensitive = 0
    for f in files:
        fp = Path(f)
        if is_sensitive_path(fp):
            skipped_sensitive += 1
            continue
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if len(matches) >= cap or out_bytes > _MAX_READ_BYTES:
                        scanned_all = False
                        break
                    if rx.search(line):
                        text = line.rstrip("\n")[:400]
                        rel = os.path.relpath(f, abs_path) if abs_path.is_dir() else fp.name
                        matches.append({"file": rel, "line": lineno, "text": text})
                        out_bytes += len(text.encode("utf-8", errors="replace")) + 40
        except OSError:
            continue
        if len(matches) >= cap or out_bytes > _MAX_READ_BYTES:
            scanned_all = False
            break
    result = {"status": "success", "root": str(abs_path), "pattern": pattern,
              "matches": matches, "truncated": not scanned_all}
    if skipped_sensitive:
        result["skipped_sensitive_files"] = skipped_sensitive
    return result


register_tool(
    ToolDefinition(
        name="list_files",
        replayable_read=True,
        description=(
            "列目录内容，或按 glob 模式找文件（如 `glob=\"**/*.json\"`）。"
            "输出有界（默认 200 条封顶，truncated=true 表示还有没列完的）。"
            "路径相对 state.root 解释；项目工作区内可读任何节点的目录"
            "（读跨节点、写不跨节点）。.git / cache 类目录不进遍历。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": ".",
                         "description": "目录或文件路径（相对 state.root 或绝对）。"},
                "glob": {"type": "string",
                         "description": "可选 glob 模式，相对 path 匹配（如 `experiments/artifacts/*.json`）。"},
                "max_entries": {"type": "integer", "default": 200, "minimum": 1,
                                "maximum": 1000},
            },
            "required": ["path"],
        },
        risk_level="low",
    ),
    _list_files,
)

register_tool(
    ToolDefinition(
        name="search_files",
        replayable_read=True,
        description=(
            "在文件里按正则搜行（相当于 grep -rn）。返回 file/line/text 三元组，"
            "输出有界（默认 50 条命中封顶，truncated=true 表示没搜完）。"
            "可用 glob 限定文件范围。项目工作区内可搜任何节点的目录。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "description": "正则表达式。"},
                "path": {"type": "string", "default": ".",
                         "description": "起点目录或单个文件。"},
                "glob": {"type": "string", "description": "可选 glob 限定（如 `*.md`）。"},
                "max_matches": {"type": "integer", "default": 50, "minimum": 1,
                                "maximum": 500},
                "ignore_case": {"type": "boolean", "default": False},
            },
            "required": ["pattern"],
        },
        risk_level="low",
    ),
    _search_files,
)


# ── write_file ───────────────────────────────────────────────────────────────

async def _write_file(state: State, path: str, content: str,
                      create_dirs: bool = True, **_: Any) -> dict:
    abs_path = _resolve_path(state, path, write=True)
    if abs_path.exists():
        if not abs_path.is_file():
            return {"status": "error", "error": f"{abs_path} 已存在但不是文件"}
        if str(abs_path) not in state.files_read:
            return {
                "status": "error",
                "error": (
                    f"文件 {abs_path} 已存在但本次 run 未 read 过。"
                    f"先调 read_file 看一遍现有内容，再 write_file 覆盖。"
                    f"这是防止盲写覆盖的安全约束。"
                ),
            }
    if create_dirs:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        abs_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"status": "error", "error": f"写文件失败：{type(e).__name__}: {e}"}

    # 写完后视为已读（再 edit 不必再 read）
    state.files_read.add(str(abs_path))

    return {
        "status": "success",
        "path": str(abs_path),
        "bytes_written": len(content.encode("utf-8")),
    }


register_tool(
    ToolDefinition(
        name="write_file",
        description=(
            "把完整内容写入文件（创建新的或整体覆盖现有的）。"
            "路径相对 state.root 解释；可传绝对路径。create_dirs=True（默认）会自动建父目录。"
            "⚠️ 对**已存在**的文件：本次 run 必须先 read_file 一次，否则 write 被拒。"
            "（新建文件不要求 read。）这是防止盲写覆盖的安全约束。"
            "想做局部修改而非整体覆盖，用 edit_file 更高效。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件路径。"},
                "content": {"type": "string", "description": "完整文件内容。"},
                "create_dirs": {"type": "boolean", "default": True,
                                "description": "自动建父目录。"},
            },
            "required": ["path", "content"],
        },
        risk_level="medium",       # 能覆盖文件
    ),
    _write_file,
)


# ── edit_file ────────────────────────────────────────────────────────────────

async def _edit_file(state: State, path: str, old_string: str, new_string: str,
                     replace_all: bool = False, **_: Any) -> dict:
    abs_path = _resolve_path(state, path, write=True)
    if not abs_path.exists():
        return {"status": "error", "error": f"找不到文件：{abs_path}"}
    if not abs_path.is_file():
        return {"status": "error", "error": f"{abs_path} 不是文件"}
    # 这里曾要求「本次 run 先 read_file 过」（判决拆除 builtin:1637 删）：顺序仪式。
    # old_string 精确唯一匹配（下面两道 C）已机械证明模型知道文件内容，盲改不可能
    # 发生 —— 与 write_file 的整体覆盖不同，有界替换不需要这道仪式。
    # old_string 非空 = schema minLength:1，派发口核。
    if old_string == new_string:
        # 没什么可保护的：如实回 0 次替换即可（判决拆除 builtin:1646 删）。
        return {"status": "success", "path": str(abs_path), "replacements_made": 0,
                "note": "old_string 与 new_string 相同，文件未改动"}

    try:
        raw = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"status": "error", "error": f"读文件失败：{e}"}

    count = raw.count(old_string)
    if count == 0:
        return {
            "status": "error",
            "error": "old_string 在文件中没找到。检查空白 / 缩进 / 大小写是否精确一致。",
        }
    if count > 1 and not replace_all:
        return {
            "status": "error",
            "error": (
                f"old_string 在文件中出现 {count} 次，无法精确替换。"
                f"要么让 old_string 更长以包含上下文锁定唯一一处，"
                f"要么传 replace_all=True 替换所有。"
            ),
            "occurrences": count,
        }

    new_raw = raw.replace(old_string, new_string) if replace_all else raw.replace(old_string, new_string, 1)
    try:
        abs_path.write_text(new_raw, encoding="utf-8")
    except Exception as e:
        return {"status": "error", "error": f"写文件失败：{e}"}

    return {
        "status": "success",
        "path": str(abs_path),
        "replacements_made": count if replace_all else 1,
    }


register_tool(
    ToolDefinition(
        name="edit_file",
        description=(
            "在文件中做精确字符串替换。old_string 必须在文件里**精确匹配**（含空白、缩进、换行）。"
            "默认要求 old_string 唯一（出现 1 次）；若不唯一，把 old_string 加长到包含上下文锁定唯一一处。"
            "想替换所有出现：传 replace_all=True。"
            "比 write_file 更高效（不用回传整文件）。"
            "适合：改配置 dict 文件的一行、改 YAML / TOML 字段、改源码一处。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要修改的文件路径。"},
                "old_string": {"type": "string", "minLength": 1,
                                "description": "要被替换的精确字符串（含空白）。"},
                "new_string": {"type": "string",
                                "description": "替换成什么。可以为空字符串（=删除）。"},
                "replace_all": {"type": "boolean", "default": False,
                                "description": "True 替换所有出现；False 要求唯一。"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        risk_level="medium",
    ),
    _edit_file,
)


# ── write_node_readme ────────────────────────────────────────────────────────
# 六个产出节点的 harness 都写着「交接自述（可选但有回报）：你的目录里可以维护
# 一个 README.md，定向层会把它自动带给所有后续节点」。读端是真的
# （core/loop_hooks_builtin.py 读 <node_dir>/README.md 的首行塞进交接面）。
#
# 但 2026-09-01 本机 E2E 实拍：hypothesis 节点想写这个 README，发现自己**没有
# 任何能写文件的工具**，于是把最后几轮烧在「write_file 不在白名单内……让我确认
# 一下：README 是可选的，不写不拦我」的自我说服上，而那段独白最后成了呈给人的
# 「本节点产出了什么」。查下来六个挂着这条引导的节点里，hypothesis / literature /
# data / postprocess 四个都写不了 —— 引导许诺了一个工具面给不出的能力。
#
# 修法不是给它们开 write_file（写边界是故意收紧的），是给一个**只能写这一个
# 文件**的窄工具：承诺兑现，写面不扩。
async def _write_node_readme(state: State, content: str = "", **_: Any) -> dict:
    """写本节点目录下的 README.md（交接自述）。整块覆盖，不能写别的路径。"""
    text = str(content or "").strip()   # 非空 = schema required + minLength:1，派发口核

    target = getattr(state, "workspace_records_dir", None)
    if target is None:
        return {"status": "error",
                "error": "本 run 没有绑定项目工作区（非 v2 / CLI 模式），"
                         "节点目录不存在，交接自述无处可写也无人会读。"}

    readme = Path(target) / "README.md"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    first = next((ln.strip() for ln in text.splitlines()
                  if ln.strip() and not ln.startswith("#")), "")
    return {
        "status": "success",
        "path": str(readme),
        "bytes": len(text),
        "handoff_line": first[:150],
        "note": "首行（非标题）会被带给所有后续节点开局看。",
    }


register_tool(
    ToolDefinition(
        name="write_node_readme",
        description=(
            "写你自己节点目录下的 `README.md` —— **交接自述**。\n"
            "第一段一句话说清「本轮做了什么、什么悬而未决」：定向层会把这一行"
            "自动带给**所有后续节点**开局看。写了就有人看，不写也不拦你。\n"
            "⚠️ **整块覆盖**：你传的 content 就是 README 的新全文。\n"
            "机械事实（交付了哪些产物）框架自己扫，不用你写 —— 这里写叙事："
            "为什么这么做、哪条路堵死了、下游要当心什么。\n"
            "只能写这一个文件，写不了别的路径。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "README 的**新全文**（Markdown）。第一段（非 # 标题行）"
                        "就是带给下游的那句话：本轮做了什么、什么悬而未决。"
                    ),
                },
            },
            "required": ["content"],
        },
        risk_level="low",
    ),
    _write_node_readme,
)


# ── write_scratchpad ─────────────────────────────────────────────────────────

async def _write_scratchpad(state: State, content: str | None = None, **kwargs: Any) -> dict:
    """整块改写白板。语义与容量见 core/whiteboard.py。

    没有 `content` 的调用只可能来自**框架合成**的那两处
    （`nodes/experiment/hooks.py` 的 external_job_closure_gate / empty_stop_guard：
    用一次无害的 scratchpad 写入换一轮继续，参数名是旧的 `note`）。它们要的是
    "这一轮不空 + 提醒送到模型眼前"，而提醒本身就在 assistant 的 tool_call 参数里、
    模型读得到 —— **所以它不该动模型的板子**。板子是模型的。

    签名只声明 `content`（与 schema 一致）：多声明一个 LLM 看不见的参数，注册表
    会（正确地）警告"executor 接受它但 schema 没声明"。
    """
    from core import whiteboard

    if content is None:
        legacy = str(kwargs.get("note") or "").strip()
        return {
            "status": "success",
            "reminder_delivered": legacy[:2000],
            "board_unchanged": True,
            "hint": "框架提醒已送达；白板未改动。要改写白板请传 content。",
        }

    turn = int((getattr(state, "hook_state", None) or {}).get("_current_turn") or 0)
    return whiteboard.write(state, content, turn=turn)


register_tool(
    ToolDefinition(
        name="write_scratchpad",
        description=(
            "改写你的**白板** —— run 内唯一跨轮存活、由你自己掌握的工作状态。"
            "messages 历史会被压缩，白板不会；它每轮原样注入回来。\n"
            "⚠️ **整块覆盖**：你传的 content 就是白板的新全文，旧内容被替换。"
            "这不是追加日志，没有'第 N 条笔记'——是一块板子，写满了要自己擦。\n"
            "容量有硬上限，超了会被**拒绝写入且原板不动**，由你决定删什么。\n"
            "该写：我在哪 / 下一步做什么 / 哪些路已经堵死（附原因，防止下轮重试）。\n"
            "不该写（各有归宿）：可复用的经验→memory_note；"
            "结论与发现→save_artifact / research_state；用户立的规矩→memory_write(section='law')。\n"
            "⚠️ 要让这工具有效果，必须在 harness yaml 启用 scratchpad hook："
            "  loop_hooks: [scratchpad]"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "白板的**新全文**（会替换现有内容）。写给下一轮的自己："
                        "他只能看到压缩摘要 + 这块板子。"
                    ),
                },
            },
            "required": ["content"],
        },
        risk_level="low",
    ),
    _write_scratchpad,
)


# v2.0：update_memory + add_memory + search_memory + promote_memory + archive_memory
# + supersede_memory 全部已删除。
#
# 记忆重建（2026-08-21）后的工具面 —— 共四个，各对一层：
#   `memory_write`     宪法（用户逐字 + 引文核对）/ 叙事（调度器覆写）
#   `memory_note`      手册 **直接入册**（没有候选队列，没有人审核）
#   `memory_maintain`  维护（扫遗忘提议 / 合并 / 退休）—— 不是准入
#   `memory_recall`    主动深查（开工时框架已按适用面送过一批）
# 用户立的规矩走 `memory_write(section='law')`；跨项目稳定偏好走
# `propose_profile_update`。
#
# state.save_memory / state.search_memory 暂保留作为内部 API（recall.py /
# executor.py 仍读）。update_memory_lifecycle 与 #375 迁移脚本已删（第三波）。
