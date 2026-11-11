"""Cross-node contract for consuming rendered figure records.

判决拆除 B 刀：八种 typed 产物收敛为一种 `figure` 记录（figure_package 已
删除）。消费端只有一种形态：**引用图记录（id+hash）+ 披露其 findings**。

这里只验**账本真实性**（身份/指纹/血缘，B 类）—— 伪造记录被机械识别：

1. 记录必须由可视化属主节点铸出（produced_by_node_type 由框架盖章，模型
   写不了别人的章）；
2. ``metadata.figure_hash`` 必须等于对出处绑定核心（源数据 hash ↔ 渲染代码
   hash ↔ 输出文件 hash ↔ caption/alt_text）逐字节重算的值 —— 事后改写任何
   一环都会让指纹对不上；
3. 记录认领的输出文件，盘上的字节必须还是铸记录时那份（``metadata.files``
   的 content_hash）—— 在交付收集点传 ``resolve_file`` 时逐文件重验。

## 谁来调（#1200，wangd 2026-10-21 裁决）

writing 09-18 重建删掉了旧的 stage 图工具 —— 那是 :func:`validate_figure_record`
唯一的生产调用方，此后伪造与指纹检查**哪儿都不发生**。裁决：挂在**交付收集点**，
不在铸图时拦（cuikl 的约定：伪造记录照样落账、事后被抓，见
nodes/postprocess/tests/test_render_figure.py 的防伪段）。两个收集点共用
:func:`audit_figure_files`，判据只有一份：

  · writing 读图与图题那一步（nodes/writing/tools/w_dossier.py ``build_dossier``）：
    没过的图不进图清单、图题不采用，原因摆给模型；
  · run 收尾（core/executor.py ``finalize_run``）：本 run 交出去的路径里有没过的图
    → 一条结构化 blocker（哪张图、哪道检查、怎么修）。

记录**形状**（源绑定数组、渲染代码锚、输出文件清单）是一份声明式 schema
（``_FIGURE_METADATA_SCHEMA``），一次校验。判决词表（status/quality_mode/verdict）
不许复活这件事钉在**铸造侧**（nodes/postprocess/tools/figure.py 的
reject_verdict_fields）与测试上，不在消费端运行时因一个键名拒掉整张图 ——
无机器消费者读那些键，放行账不假（判决拆除第三波：publication_figures:100 删）。

充分性事实（findings、审计结果、「视觉检查未运行」这类缺席事实）一律进
``_adequacy_findings`` 随 payload 返回，由消费端披露进稿件局限节、referee
终审 —— 证据可持久化，判决不可以。

图文数值对账（#439）另有一个**铸造侧**入口 :func:`value_check_at_mint`，由
``core.state.State.save_artifact`` 在落账前调 —— 那是每张图都走的唯一一条路
（理由见该函数）。两个入口共用 :func:`_check_annotated_values`，判据只有一份。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from shared.lib.schema_check import schema_errors

_SHA256 = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}

#: 图记录 metadata 的形状契约（C 类）。缺它 133 行的指纹对空数组也能对上，故必要。
_FIGURE_METADATA_SCHEMA: dict = {
    "type": "object",
    "required": ["source_artifact_ids", "source_hashes", "render_code", "files"],
    "properties": {
        "source_artifact_ids": {"type": "array"},
        "source_hashes": {"type": "array", "items": _SHA256},
        "render_code": {
            "type": "object",
            "required": ["path", "content_hash"],
            "properties": {"path": {"type": "string", "minLength": 1},
                           "content_hash": _SHA256},
        },
        "files": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "content_hash"],
                "properties": {"path": {"type": "string", "minLength": 1},
                               "content_hash": _SHA256},
            },
        },
    },
}


#: 能铸 figure 记录的节点（produced_by_node_type 由框架盖章，模型写不了别人的章）。
_VISUALIZATION_OWNERS = frozenset({"postprocess", "scientific_visualization"})


class PublicationFigureError(ValueError):
    """A figure record cannot authorize publication use.

    ``check`` 是**哪一道**没过的机械代码（见 :data:`CHECK_MEANING`）。交付收集点
    要把它和记录 id 一起交给模型 —— 「伪造」「文件事后被改」「图上的数对不上出处」
    修法各不相同，只给一句英文原话，模型分不出该重画还是该找出处。
    """

    check: str = "record"

    def __init__(self, message: str, *, check: str | None = None) -> None:
        super().__init__(message)
        if check:
            self.check = check


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _hash_json(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def figure_binding_hash(metadata: dict[str, Any]) -> str:
    """出处绑定核心的指纹 —— 与铸造侧（nodes/postprocess/tools/figure.py）
    同一套字段口径；两边分叉即伪造检测失效，由测试钉住。"""

    payload: dict[str, Any] = {
        "source_artifact_ids": list(metadata.get("source_artifact_ids") or []),
        "source_hashes": list(metadata.get("source_hashes") or []),
        "render_code": {
            "path": (metadata.get("render_code") or {}).get("path"),
            "content_hash": (metadata.get("render_code") or {}).get("content_hash"),
        },
        "files": [
            {"format": item.get("format"), "content_hash": item.get("content_hash")}
            for item in metadata.get("files") or []
        ],
        "caption": metadata.get("caption"),
        "alt_text": metadata.get("alt_text"),
    }
    # 图合同也是这张图声称的一部分（它声明了图里必须成立的结构事实），所以
    # 必须被绑进指纹，否则事后改断言不留痕。
    #
    # **只在有合同时加这个键**：无条件加会让所有存量记录的 payload 多一个
    # `None`，指纹整体变化 —— 线上已铸的记录会在消费端集体验不过。加了键的
    # 记录与没加的记录各自自洽，互不影响。
    contract_hash = metadata.get("contract_hash")
    if contract_hash:
        payload["contract_hash"] = str(contract_hash)
    if metadata.get("annotated_values_hash_version") == 1:
        payload["annotated_values_hash_version"] = 1
        payload["annotated_values"] = metadata.get("annotated_values")
    return _hash_json(payload)



# ── 图上印的数，必须等于它自己声称的出处（#439）──────────────────────────────


class FigureContradictsItsSource(PublicationFigureError):
    """图上印的数值与它自己声称的出处对不上 —— 不是充分性问题，是自相矛盾。"""

    check = "contradicts_source"


def _resolve_field_path(blob: Any, path: str) -> Any:
    """按 ``a/b/0/c`` 取值；取不到返回 :data:`_MISSING`。"""
    node = blob
    for part in str(path).split("/"):
        if part == "":
            continue
        if isinstance(node, dict):
            if part not in node:
                return _MISSING
            node = node[part]
        elif isinstance(node, (list, tuple)):
            if not part.lstrip("-").isdigit():
                return _MISSING
            try:
                node = node[int(part)]
            except IndexError:
                return _MISSING
        else:
            return _MISSING
    return node


_MISSING = object()


def annotated_value_conflicts(
    metadata: dict[str, Any],
    sources: Mapping[str, Any] | None = None,
) -> list[str]:
    """图上印的每个数值 vs 它声称的出处。返回不一致清单（空 = 没有矛盾）。

    E2E v26 现场：论文中心结论和它自己嵌的图**互相矛盾，而且矛盾正好跨过预注册
    的 2σ 门** —— 正文 1.17σ 判 supported，图上印的 2.8σ 该判 refuted。
    同一个头条数字被 experiment 和 postprocess 各算一次（不加权 vs 加权拟合），
    两边都不报错。**四轮 reviewer 全部没抓到，是人工审出来的。**

    源哈希校验抓不到这一类：两边读的**确实是同一份源**，分叉在算法里。
    图上那些数字此前只以像素存在，下游没有任何一方能把它和正文的数放在一起比。

    这道闸要求图记录声明 ``annotated_values``：图上印了哪些数、每个数声称来自
    哪份产物的哪个字段路径、容差是多少。声明了就逐条比对；**没声明就跳过**
    —— 见 :func:`value_check_at_mint` / :func:`validate_figure_record`，缺席本身
    会被落账、被披露，而不是悄悄放行。

    ``sources`` 是 ``{artifact_id: 已解析的产物内容}``。拿不到某份出处**不算
    通过**，记一条"够不到出处"；够不到和对得上必须分开
    （相关：观测不到不许当成对得上）。
    """
    declared = metadata.get("annotated_values")
    if not isinstance(declared, list) or not declared:
        return []
    available = dict(sources or {})
    conflicts: list[str] = []
    for index, item in enumerate(declared):
        if not isinstance(item, dict):
            conflicts.append(f"annotated_values[{index}] 不是对象，无法对账")
            continue
        label = str(item.get("label") or f"#{index}").strip()
        artifact_id = str(item.get("source_artifact_id") or "").strip()
        field_path = str(item.get("source_field") or "").strip()
        if not artifact_id or not field_path:
            conflicts.append(
                f"{label}：声明了印在图上，却没说来自哪份产物的哪个字段 —— "
                "无出处的数值无法对账，等于没声明")
            continue
        try:
            printed = float(item.get("value"))
        except (TypeError, ValueError):
            conflicts.append(f"{label}：图上的值不是数字（{item.get('value')!r}）")
            continue
        try:
            tolerance = abs(float(item.get("tolerance", 0.0)))
        except (TypeError, ValueError):
            tolerance = 0.0
        if artifact_id not in available:
            conflicts.append(
                f"{label}：够不到出处 {artifact_id} —— 没有对上账，"
                "不是对上了（观测不到不许当成一致）")
            continue
        found = _resolve_field_path(available[artifact_id], field_path)
        if found is _MISSING:
            conflicts.append(f"{label}：出处 {artifact_id} 里没有字段 {field_path}")
            continue
        try:
            actual = float(found)
        except (TypeError, ValueError):
            conflicts.append(
                f"{label}：出处 {artifact_id}/{field_path} 不是数字（{found!r}）")
            continue
        if not all(math.isfinite(number) for number in (printed, actual, tolerance)):
            conflicts.append(f"{label}：图面、出处和容差必须是有限数值")
            continue
        if abs(printed - actual) > tolerance:
            conflicts.append(
                f"{label}：图上印的是 {printed!r}，而它声称的出处 "
                f"{artifact_id}/{field_path} 是 {actual!r}"
                f"（容差 {tolerance!r}，实差 {abs(printed - actual)!r}）")
    return conflicts


#: 「没声明 annotated_values」的缺席事实 —— 铸造侧落账与消费端披露用同一句。
VALUE_CHECK_NOT_RUN = (
    "图文数值对账未运行（记录没有声明 annotated_values —— "
    "图上印了哪些数只以像素存在，下游无法与正文比对）")


def _check_annotated_values(
    metadata: dict[str, Any], sources: Mapping[str, Any] | None,
) -> str | None:
    """图文对账的唯一判据：对不上就抛；没声明返回缺席事实；对上了返回 None。

    对不上不是充分性问题：图上印的数和它自己声称的出处不一致，是**自相矛盾**
    （记录完整性，B），所以抛而不是披露。没有声明的按缺席处理 —— postprocess
    还没开始填这个字段，闸不该因此拦住所有图；但它的缺席必须**看得见**，
    否则和"查过了、没问题"长得一模一样。
    """
    conflicts = annotated_value_conflicts(metadata, sources)
    if conflicts:
        raise FigureContradictsItsSource(
            "图上印的数值与它声称的出处对不上：" + "；".join(conflicts))
    declared = metadata.get("annotated_values")
    # 与 annotated_value_conflicts 同一个「声明了」口径：非空数组。不是数组的
    # 真值（比如一个 dict）那边不比，这边就不许当成「比过了」。
    if not (isinstance(declared, list) and declared):
        return VALUE_CHECK_NOT_RUN
    return None


def _field_path_view(record: dict[str, Any]) -> Any:
    """一份出处产物里「字段路径」指向的那个结构：正文是 JSON 就按 JSON 解析。

    v26 那份 clean_results 的正文就是 JSON（``fss_extrapolation/T_c_est`` 在正文里，
    metadata 里只有一个四舍五入过的 ``T_c_est``）。正文不是 JSON 时原样返回 ——
    字段路径解析不到，会被如实报成「出处 X 里没有字段 Y」，不会被当成对上了。
    """
    content = record.get("content")
    if isinstance(content, (dict, list)):
        return content
    try:
        return json.loads(str(content or ""))
    except ValueError:
        return content


def declared_sources(
    metadata: Any,
    read_artifact: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """这张图声称的那几份出处：``source_artifact_ids`` ∪ 各条 annotated_values 的
    ``source_artifact_id``，按 ``read_artifact`` 读当前版本、取字段路径视图。

    铸造侧（:func:`value_check_at_mint`）与交付收集点（:func:`audit_figure_files`）
    读出处只走这一条：两边各拼一份，同一张图就会在一处对得上、另一处够不到。
    读不到的不放进来，由 :func:`annotated_value_conflicts` 记成「够不到出处」。
    """
    if not isinstance(metadata, dict):
        return {}
    declared = metadata.get("annotated_values")
    wanted = [str(item) for item in metadata.get("source_artifact_ids") or []]
    if isinstance(declared, list):
        wanted += [str(item.get("source_artifact_id") or "").strip()
                   for item in declared if isinstance(item, dict)]
    sources: dict[str, Any] = {}
    for artifact_id in dict.fromkeys(a for a in wanted if a):
        record = read_artifact(artifact_id)
        if isinstance(record, dict):
            sources[artifact_id] = _field_path_view(record)
    return sources


def value_check_at_mint(
    metadata: dict[str, Any],
    read_artifact: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """铸 figure 记录那一刻做图文对账；返回要落进记录的对账账（``ran`` …）。

    ## 为什么挂在铸造侧（#439 第二次落地）

    PR#1007 把对账挂在 :func:`validate_figure_record` 上，而它唯一的生产调用方是
    旧 writing 的 stage 图工具 —— writing 重建（6ba051b14，2026-09-18）把它整个删了，
    此后对账与「未运行」的披露**哪儿都不发生**。那个调用方还不传 ``sources``，
    任何一条声明都会被判成「够不到出处」。

    重挂之前量过两条候选路：research_runs/ 下 16 个真跑工作区（yuankk 夹具循环 10 轮 +
    node20 快照 6 个）共 195 份 figure 记录，**0 份冻结过**
    —— 冻结门挂上去仍是一道没人走的闸；writing 重建后只扫盘上的图文件、读
    ``figure__*.md`` 里的 Caption 行，不读记录。每张图都走、又归框架的只有这一处：
    ``core.state.State.save_artifact``（render_figure 在那里落账）。

    出处读的是这张图声称的那几份：``source_artifact_ids`` ∪ 各条声明的
    ``source_artifact_id``，按 ``read_artifact`` 读当前版本 —— 铸造那一刻它就是
    render_figure 刚算完 ``source_hashes`` 的那一版。读不到的不放进来，由
    :func:`annotated_value_conflicts` 记成「够不到出处」。

    返回 ``{"ran": False, "note": …}``（没声明：缺席落账，不拦）或
    ``{"ran": True, "checked": [label…]}``；对不上抛 :class:`FigureContradictsItsSource`。
    """
    declared = metadata.get("annotated_values")
    absent = _check_annotated_values(metadata, declared_sources(metadata, read_artifact))
    if absent:
        return {"ran": False, "note": absent}
    return {
        "ran": True,
        "checked": [str(item.get("label") or f"#{index}")
                    for index, item in enumerate(declared)],
    }


def validate_figure_record(
    record: dict[str, Any],
    sources: Mapping[str, Any] | None = None,
    *,
    resolve_file: Callable[[str], Path | None] | None = None,
) -> dict[str, Any]:
    """Return hash-verified figure metadata (raising on forgery), findings disclosed.

    返回的 dict 是记录的 metadata 加 ``_adequacy_findings``（消费端披露用）。
    没过时抛 :class:`PublicationFigureError`，``check`` 说是哪一道。

    ``resolve_file`` 把 ``metadata.files[].path`` 解析成盘上的文件；给了就逐个重算
    字节指纹 —— 记录之后图文件被换过（重画了却没重铸、手改了 PNG）只有这里看得见：
    绑定指纹算的是记录里写的 hash，文件换了它照样自洽。盘上找不到的文件不算伪造
    （交不出去的东西谈不上冒充真图），缺席进 ``_adequacy_findings``。
    """

    if record.get("type") != "figure":
        raise PublicationFigureError("artifact is not a figure record", check="not_a_figure")
    if record.get("produced_by_node_type") not in _VISUALIZATION_OWNERS:
        raise PublicationFigureError(
            "figure record was not produced by a visualization owner",
            check="not_visualization_owner")
    if "content_hash" in record:
        from core.ledger import sha256_text

        if record.get("content_hash") != sha256_text(str(record.get("content") or "")):
            raise PublicationFigureError(
                "figure record artifact content_hash is stale", check="record_content_hash")
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        raise PublicationFigureError("figure record has no metadata", check="not_rendered_figure")
    if metadata.get("record") != "rendered_figure":
        raise PublicationFigureError(
            "figure record was not minted by render_figure (no rendered_figure marker); "
            "stage it as asset_role=figure instead — the delivery will disclose that no "
            "mechanical audit ran on it",
            check="not_rendered_figure",
        )

    # ── 记录形状（C）：一份 schema、一次校验 ────────────────────────────────
    shape_errors = schema_errors(_FIGURE_METADATA_SCHEMA, metadata, "metadata")
    if len(metadata.get("source_artifact_ids") or []) != len(metadata.get("source_hashes") or []):
        shape_errors.append("metadata.source_artifact_ids 与 source_hashes 长度不等")
    if shape_errors:
        raise PublicationFigureError(
            "figure record shape is invalid: " + "；".join(shape_errors), check="shape")

    # ── 出处绑定完整性（B）────────────────────────────────────────────────
    if metadata.get("figure_hash") != figure_binding_hash(metadata):
        raise PublicationFigureError(
            "figure record figure_hash is stale or forged", check="figure_hash")

    adequacy: list[str] = []
    # ── 输出文件的字节（B，#1200）：盘上的还是不是记录里那份 ──────────────
    if resolve_file is not None:
        for item in metadata.get("files") or []:
            claimed = str(item.get("path") or "")
            target = resolve_file(claimed)
            if target is None or not target.is_file():
                adequacy.append(f"记录认领的文件 {claimed} 不在盘上，字节无从重验")
                continue
            actual = sha256_file(target)
            if actual != item.get("content_hash"):
                raise PublicationFigureError(
                    f"figure file {claimed} changed after the record was minted "
                    f"(record: {item.get('content_hash')}, disk: {actual})",
                    check="file_bytes",
                )

    # ── 充分性事实：披露，不判决 ──────────────────────────────────────────
    audit = metadata.get("audit")
    if not (isinstance(audit, dict) and audit.get("ran") is True):
        adequacy.append("图像级机械审计未运行（记录缺 audit 账）")
    for finding in metadata.get("findings") or []:
        if isinstance(finding, dict):
            message = str(finding.get("message") or finding.get("observation") or "").strip()
            collector = str(finding.get("collector") or "").strip()
            if message:
                adequacy.append(f"[{collector or 'finding'}] {message}")
        elif isinstance(finding, str) and finding.strip():
            adequacy.append(finding.strip())
    # ── 图文对账（#439）：声明了就比，没声明就让缺席可见（判据见
    # _check_annotated_values；铸造侧同一份判据在 value_check_at_mint）────
    absent = _check_annotated_values(metadata, sources)
    if absent:
        adequacy.append(absent)

    vlm_review = metadata.get("vlm_review")
    if not isinstance(vlm_review, dict):
        adequacy.append("视觉检查未运行（平台未配置 visual_review 角色）")
    elif not vlm_review.get("completed"):
        adequacy.append("视觉检查未完成（记录里有如实分类的失败条目）")

    payload = dict(metadata)
    payload["_adequacy_findings"] = adequacy
    return payload


# ── 交付收集点：读图那一步 + 收尾审计（#1200）──────────────────────────────


#: ``PublicationFigureError.check`` → 给模型看的一句话：哪里不对。
CHECK_MEANING: dict[str, str] = {
    "not_a_figure": "账本上这份不是 figure 记录",
    "not_visualization_owner": "这份图记录不是图表服务（postprocess）铸的 —— 别的节点铸不了图记录",
    "record_content_hash": "图记录正文在落账之后被改过（账本指纹对不上，常见于手改了 figure__*.md 里的图题）",
    "not_rendered_figure": "这份图记录不是 render_figure 铸的（缺 rendered_figure 标记）",
    "shape": "这份图记录缺出处绑定（源数据 / 渲染代码 / 输出文件清单）",
    "figure_hash": "出处绑定指纹对不上 —— 记录是伪造的，或铸出之后改过图题、出处、文件清单",
    "file_bytes": "图文件在铸记录之后被换过，盘上的字节不是记录里的那份",
    "contradicts_source": "图上印的数值与它声称的出处对不上（或够不到出处）",
    "record_unreadable": "账本上有这份图记录，盘上读不到它的正文",
}

#: 修法只有一条路：重新铸。手改文件或记录只会让指纹再对不上一次。
REMEDY = (
    "修法：由图表服务（postprocess）用 render_figure 重新出这张图 —— 同一 output_name "
    "存成新版本，记录与文件一起换新；writing 侧用 request_figures 重派这张图，再 "
    "read_dossier(part='figures', rebuild=true) 取新文件。不要手改图文件或图记录：指纹会再对不上。"
)


@dataclass(frozen=True)
class FigureIntegrityFinding:
    """一张没过的图：哪个文件、哪份记录、哪道检查、原话。"""

    path: str        # 出事的那个文件（工作区相对）：记录正文或图文件
    record_id: str
    check: str
    detail: str

    def message(self) -> str:
        meaning = CHECK_MEANING.get(self.check, "图记录完整性没过")
        return (f"图 {self.record_id}（{self.path}）不能当真图用：{meaning}"
                f"【{self.check}】。原话：{self.detail}。{REMEDY}")

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "record_id": self.record_id, "check": self.check,
                "detail": self.detail, "message": self.message()}


@dataclass
class FigureIntegrityAudit:
    """一次收集点审计的账：核过哪些（``checked`` 空 ≠ 没核）、哪些没过。"""

    checked: list[str] = field(default_factory=list)
    findings: list[FigureIntegrityFinding] = field(default_factory=list)

    @property
    def refused(self) -> dict[str, FigureIntegrityFinding]:
        """{工作区相对路径: 没过的原因} —— 收集点据此不把它当真图。"""
        out: dict[str, FigureIntegrityFinding] = {}
        for finding in self.findings:
            out.setdefault(finding.path, finding)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"checked": list(self.checked),
                "findings": [finding.as_dict() for finding in self.findings]}


def _posix(value: str) -> str:
    return str(value or "").strip().replace("\\", "/")


def _workspace_file(root: Path, claimed: str) -> Path | None:
    value = _posix(claimed)
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else root / value


def _version_is_bound(row: Mapping[str, Any]) -> bool:
    """旧版记录（正文多半已不在盘上）只能验绑定那一半：属主、标记、指纹。"""
    meta = row.get("metadata")
    return (isinstance(meta, dict)
            and str(row.get("produced_by_node_type") or "") in _VISUALIZATION_OWNERS
            and meta.get("record") == "rendered_figure"
            and meta.get("figure_hash") == figure_binding_hash(meta))


def audit_figure_files(worktree: Path | str | None, rel_paths: Iterable[str]) -> FigureIntegrityAudit:
    """收集点上的完整 :func:`validate_figure_record`：这些路径里哪些是图、哪些不能当真图。

    ``rel_paths`` 是收集点**正要读 / 正要交**的文件（工作区相对）。只看工作区账本上的
    figure 记录；一个路径归下面三类之一才算「图」，别的不归这里管（没有记录认领的图走
    「未经机械审计」的披露路，那不是伪造）：

      · **图记录正文**（``figures/figure__x.md``）：整份记录走完整校验 —— 属主、标记、
        形状、绑定指纹、它认领的输出文件在盘上的字节、图上印的数对出处。没过，这份记录
        说的图题与出处就都不作数。
      · **记录原位认领的图文件**（当前版 ``metadata.files`` 的 path）：字节必须是记录
        里那份，或者是另一份真记录里的那份；都不是 → ``file_bytes``（铸完被换过）。
      · **真图的副本**（writing 拷进 ``paper/manuscript/figures/`` 的那种）：文件名被某份
        记录认领过、**且字节等于某份记录认领的字节** —— 只有字节对得上才认得出它是副本；
        同名不同字节的是另一个文件（experiment 自己也会画 ``demo.png``），不许误判成改过的图。

    给图文件作保的记录只看**绑定**那一半（属主、标记、形状、指纹、出处），不看它别的文件
    在盘上变没变：一份真渲染的 PNG 副本，不因为原位的 PDF 后来被换过就成了假图。当前版
    过绑定校验才作保；旧版（正文多半已不在盘上）过属主 + 标记 + 指纹才作保 —— 上一版真
    渲染的副本是陈旧的真图，不是假图。
    """
    audit = FigureIntegrityAudit()
    if not worktree:
        return audit
    from core.ledger import workspace_store

    root = Path(worktree)
    store = workspace_store(root)
    figures = {h.artifact_id: h for h in store.heads().values() if h.artifact_type == "figure"}
    if not figures:
        return audit

    by_body = {_posix(head.path): head for head in figures.values() if head.path}
    named: set[str] = set()
    # 当前版原位认领：路径 → (记录 id, 记录里的字节指纹)
    exact: dict[str, tuple[str, str]] = {}
    # 字节 → [(记录 id, 是不是当前版, 旧版是否过绑定校验)]
    by_hash: dict[str, list[tuple[str, bool, bool]]] = {}
    for head in figures.values():
        for row in head.saves:
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            is_head = int(row.get("version") or 1) == head.version
            bound = is_head or _version_is_bound(row)
            for item in meta.get("files") or []:
                if not isinstance(item, dict):
                    continue
                claimed = _posix(str(item.get("path") or ""))
                digest = str(item.get("content_hash") or "")
                if claimed:
                    named.add(PurePosixPath(claimed).name)
                if is_head and claimed:
                    exact[claimed] = (head.artifact_id, digest)
                if digest:
                    by_hash.setdefault(digest, []).append((head.artifact_id, is_head, bound))

    verdicts: dict[tuple[str, bool], PublicationFigureError | None] = {}

    def verdict(artifact_id: str, *, files_on_disk: bool) -> PublicationFigureError | None:
        key = (artifact_id, files_on_disk)
        if key not in verdicts:
            record = store.record(artifact_id)
            error: PublicationFigureError | None = None
            if record is None:
                error = PublicationFigureError(
                    "the ledger has this figure record but its body cannot be read",
                    check="record_unreadable")
            else:
                try:
                    validate_figure_record(
                        record,
                        declared_sources(record.get("metadata"), store.record),
                        resolve_file=(lambda claimed: _workspace_file(root, claimed))
                        if files_on_disk else None,
                    )
                except PublicationFigureError as exc:
                    error = exc
            verdicts[key] = error
        return verdicts[key]

    def refuse(rel: str, artifact_id: str, error: PublicationFigureError) -> None:
        audit.findings.append(FigureIntegrityFinding(
            path=rel, record_id=artifact_id, check=error.check, detail=str(error)[:600]))

    for rel in dict.fromkeys(_posix(p) for p in rel_paths):
        if not rel or not (root / rel).is_file():
            continue
        head = by_body.get(rel)
        if head is not None:
            audit.checked.append(rel)
            error = verdict(head.artifact_id, files_on_disk=True)
            if error is not None:
                refuse(rel, head.artifact_id, error)
            continue
        owner = exact.get(rel)
        if owner is None and PurePosixPath(rel).name not in named:
            continue
        digest = sha256_file(root / rel)
        vouchers = by_hash.get(digest, [])
        if owner is None and not vouchers:
            continue   # 同名不同字节：不是任何一份记录的副本
        audit.checked.append(rel)
        if any((verdict(aid, files_on_disk=False) is None) if is_head else bound
               for aid, is_head, bound in vouchers):
            continue
        failed = next((aid for aid, is_head, _ in vouchers if is_head), None)
        if failed is not None:
            refuse(rel, failed, verdict(failed, files_on_disk=False) or PublicationFigureError(
                "figure record failed its binding check", check="figure_hash"))
        elif vouchers:
            refuse(rel, vouchers[0][0], PublicationFigureError(
                f"only record versions that fail the binding check vouch for {rel}",
                check="figure_hash"))
        else:
            artifact_id, claimed = owner  # type: ignore[misc]
            refuse(rel, artifact_id, PublicationFigureError(
                f"figure file {rel} changed after the record was minted "
                f"(record: {claimed}, disk: {digest})", check="file_bytes"))
    return audit
