"""Archive literature search results and locally cache obtainable full text."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from types import SimpleNamespace
import time
import sqlite3
import pickle

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .scihub_fetcher import get_full_text, paper_cache_dir, fulltext_capabilities
from .paper_images import extract_paper_image, fetch_paper_image_url, generate_title_card, materialize_image_url
from .literature_catalog import retry_asset_records, upsert_paper, upsert_papers

# 全文获取涉及多个来源和浏览器回退；90 秒通常只够处理少量论文。
# 默认放宽到 10 分钟，仍可用 HARNESS_FULLTEXT_BUDGET_SECONDS 覆盖。
DEFAULT_FULLTEXT_BUDGET_SECONDS = 600.0
logger = logging.getLogger(__name__)


def _parse_papers_json(value: str) -> list[dict[str, Any]]:
    """解析模型传入的论文数组，并容忍一次可识别的尾部右括号。"""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        text = str(value or "").lstrip()
        try:
            parsed, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            raise exc
        trailing = text[end:].strip()
        # 某些模型工具调用会把合法数组错误地序列化成数组后多一个右括号。
        # 只容忍这一个明确模式，不能吞掉任意尾部垃圾。
        if trailing != "}" or not isinstance(parsed, list):
            raise exc
    if not isinstance(parsed, list):
        raise ValueError("papers_json 必须是论文数组")
    return parsed


def _arxiv_id(paper: dict[str, Any]) -> str:
    for value in (paper.get("url", ""), paper.get("pdf_url", ""), paper.get("doi", "")):
        match = re.search(
            r"(?:arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(\d{4}\.\d{4,5})(?:v\d+)?",
            str(value),
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
    return ""


def _restore_original_metadata(paper: dict[str, Any]) -> bool:
    """按 DOI 从本地原始检索索引恢复 source 和完整摘要。

    archive_papers 只接受模型的论文选择结果；摘要必须来自检索源，不能采用
    模型在整理清单时生成的短概述。跨来源有多个摘要时取最长的非空版本。
    """
    # The model may provide a shortened/paraphrased abstract while selecting papers.
    # Never trust that value: only a source cache/database abstract may enter the index.
    paper["abstract"] = ""
    doi = str(paper.get("doi") or "").strip()
    if not doi:
        return False
    try:
        # 优先读取原始搜索缓存：其中是来源 API 返回的 Paper 对象，
        # 不经过模型摘要压缩。跨来源同 DOI 取最长非空摘要。
        from core.paths import literature_cache_dir
        cached = []
        for cache_file in literature_cache_dir().glob("*.pkl"):
            try:
                entries = pickle.loads(cache_file.read_bytes())
            except Exception:
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                entry_doi = str(getattr(entry, "doi", "") or "").strip()
                if entry_doi.lower() == doi.lower():
                    cached.append((str(getattr(entry, "source", "") or ""),
                                   str(getattr(entry, "abstract", "") or "").strip()))
        cached = [(src, abstract) for src, abstract in cached if abstract]
        if cached:
            source, original = max(cached, key=lambda item: len(item[1]))
            paper["abstract"] = original
            if source:
                paper["source"] = source
            return True

        from .local_index import INDEX_DB
        with sqlite3.connect(str(INDEX_DB)) as conn:
            rows = conn.execute(
                "SELECT source, abstract FROM papers WHERE lower(doi) = lower(?) "
                "AND (source IS NOT NULL OR abstract IS NOT NULL)", (doi,)
            ).fetchall()
        rows = [(str(src or "").strip(), str(abs_ or "").strip()) for src, abs_ in rows]
        rows = [(src, abs_) for src, abs_ in rows if src or abs_]
        if not rows:
            return False
        originals = [(src, abs_) for src, abs_ in rows if abs_]
        if originals:
            # 以实际提供最长原始摘要的来源作为字段 provenance。
            original_source, original = max(originals, key=lambda item: len(item[1]))
            if not paper.get("source") and original_source:
                paper["source"] = original_source
            # 原始摘要优先，覆盖模型可能写入的短概述。
            paper["abstract"] = original
        elif not paper.get("source"):
            for src, _ in rows:
                if src:
                    paper["source"] = src
                    break
        return bool(originals)
    except Exception:
        # 元数据恢复失败不阻断归档；审计可通过字段完整性发现。
        return False


def _text_sidecar(pdf_path: str, text: str) -> str:
    if not pdf_path or not text or not pdf_path.lower().endswith(".pdf"):
        return ""
    path = Path(pdf_path).with_suffix(".txt")
    path.write_text(text, encoding="utf-8")
    return str(path)


def _paper_key(paper: dict[str, Any]) -> tuple[str, str]:
    doi = str(paper.get("doi") or "").strip().lower()
    title = re.sub(r"\W+", "", str(paper.get("title") or "").lower())
    return doi, title


def _reconcile_classification(classification: Any, indexed: list[dict[str, Any]]) -> Any:
    """让分类统计与最终 literature_index 使用同一论文集合。"""
    if not isinstance(classification, dict):
        return classification
    allowed = {_paper_key(p) for p in indexed}
    allowed_dois = {k[0] for k in allowed if k[0]}
    allowed_titles = {k[1] for k in allowed if k[1]}
    clusters = classification.get("clusters")
    if isinstance(clusters, list):
        for cluster in clusters:
            if not isinstance(cluster, dict) or not isinstance(cluster.get("papers"), list):
                continue
            kept = []
            for paper in cluster["papers"]:
                if isinstance(paper, dict):
                    key = _paper_key(paper)
                    matches = ((key[0] and key[0] in allowed_dois)
                               or (key[1] and key[1] in allowed_titles)
                               or key in allowed)
                    if matches:
                        kept.append(paper)
            cluster["papers"] = kept
            cluster["paper_count"] = len(kept)
        classification["total_papers"] = len(indexed)
    return classification


def _article_dir_name(paper: dict[str, Any]) -> str:
    """Return a stable, human-readable directory name for one paper."""
    import hashlib
    doi = str(paper.get("doi") or "").strip()
    doi = re.sub(r"^https?://doi.org/", "", doi, flags=re.I).strip().lower()
    if doi:
        name = doi.replace("/", "__")
        name = re.sub(r"[^a-z0-9._-]+", "_", name).strip("._")
        return name or "doi-unknown"
    title = re.sub(r"\s+", " ", str(paper.get("title") or "").strip().lower())
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"no-doi-{digest}"


def _article_layout(root: str, paper: dict[str, Any]) -> dict[str, Path]:
    """Create the shared cross-run layout for one paper."""
    article = Path(root) / _article_dir_name(paper)
    layout = {"article": article, "index": article / "index",
              "paper": article / "paper", "figure": article / "figure"}
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def _write_article_index(record: dict[str, Any], index_dir: Path) -> str:
    """Write per-paper metadata; the run artifact remains the audit manifest."""
    path = index_dir / "index.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    image = record.get("image") if isinstance(record.get("image"), dict) else {}
    figure_status = str(record.get("figure_status") or image.get("status") or "not_requested")
    manifest = {
        "schema_version": 1,
        "paper_id": _article_dir_name(record),
        "components": {
            "index": {
                "status": str(record.get("index_status") or "acquired"),
                "retry_eligible": str(record.get("index_status") or "acquired") != "acquired",
            },
            "paper": {
                "status": str(record.get("fulltext_status") or "not_requested"),
                "retry_eligible": str(record.get("fulltext_status") or "not_requested")
                in {"not_requested", "fetch_failed", "deferred", "oa_unknown", "unavailable"},
            },
            "figure": {
                "status": figure_status,
                "retry_eligible": figure_status in {"not_requested", "failed", "unavailable"}
                or image.get("fallback") == "title_card",
            },
        },
        "updated_at": time.time(),
    }
    (index_dir.parent / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # JSON is the auditable source of truth; SQLite is only a rebuildable lookup catalog.
    # A shared filesystem lock/permission problem must never block literature delivery.
    try:
        upsert_paper(index_dir.parent.parent, record)
    except Exception as exc:  # pragma: no cover - filesystem-specific failures
        logger.warning("literature catalog update skipped: %s", exc)
    return str(path.resolve())


def _archive_one(
    paper: dict[str, Any],
    output_dir: str,
    deadline: float | None = None,
    *,
    restore_original_metadata: bool = True,
) -> dict[str, Any]:
    record = dict(paper)
    layout = _article_layout(output_dir, record)
    article_dir, index_dir, paper_dir, figure_dir = (layout["article"], layout["index"], layout["paper"], layout["figure"])
    if restore_original_metadata:
        abstract_restored = _restore_original_metadata(record)
        record["abstract_provenance"] = "source_cache" if abstract_restored else "unavailable"
    else:
        record["abstract_provenance"] = str(
            record.get("abstract_provenance")
            or ("source_index" if record.get("abstract") else "unavailable")
        )
    record.update({
        "fulltext_status": "unavailable", "fulltext_source": "",
        "pdf_path": "", "text_path": "", "fulltext_chars": 0,
        "fetch_attempted": [],
        "is_oa": bool(paper.get("is_oa", False)),
        "oa_status": paper.get("oa_status", ""),
        "oa_url": paper.get("oa_url", ""),
        "image": {
            "schema_version": 1, "status": "unavailable", "path": "", "url": "",
            "source": "", "method": "", "page": None, "figure_label": "",
            "score": None, "width": None, "height": None,
            "sha256": "", "reason": "not_processed",
        },
    })
    if deadline is not None and time.monotonic() >= deadline:
        record["fulltext_status"] = "deferred"
        record["fulltext_reason"] = "fulltext_budget_exhausted"
        record["image"]["reason"] = "fulltext_budget_exhausted"
        record.update({"article_dir": str(article_dir.resolve()), "index_dir": str(index_dir.resolve()),
                       "paper_dir": str(paper_dir.resolve()), "figure_dir": str(figure_dir.resolve())})
        record["index_path"] = _write_article_index(record, index_dir)
        return record

    # archive_papers and get_full_text now share one legal-first strategy chain.
    result = get_full_text(SimpleNamespace(**record), output_dir=str(paper_dir))
    pdf_path = str(result.get("pdf_path", "") or "")
    text = str(result.get("text", "") or "")
    # 将 OA 判定写回最终 index，即使没有成功下载 PDF。
    if "is_oa" in result:
        record["is_oa"] = bool(result.get("is_oa"))
    if result.get("oa_status"):
        record["oa_status"] = result.get("oa_status")
    if result.get("oa_url"):
        record["oa_url"] = result.get("oa_url")
    record["fetch_attempted"] = list(result.get("attempted_strategies", []))
    record["fetch_failure_reasons"] = dict(result.get("failure_reasons", {}))
    # A PDF may exist even when text extraction is incomplete; image enrichment
    # can still use it without changing fulltext semantics.
    if pdf_path.lower().endswith(".pdf") and Path(pdf_path).exists():
        record["pdf_path"] = str(Path(pdf_path).resolve())
    if deadline is not None and time.monotonic() >= deadline:
        remote_image = {"status": "unavailable", "reason": "fulltext_budget_exhausted"}
    else:
        remote_image = fetch_paper_image_url(record, str(index_dir))
    local_remote = materialize_image_url(remote_image, str(article_dir), record, image_subdir="figure") if remote_image.get("status") == "available" else remote_image
    pdf_image = extract_paper_image(record, str(article_dir), deadline=deadline, image_subdir="figure")
    if local_remote.get("status") == "available":
        record["image"] = {
            "schema_version": 1, "status": "available", "path": local_remote.get("path", ""), "url": local_remote.get("url", ""),
            "source": local_remote.get("source", "remote"), "method": local_remote.get("method", ""),
            "page": None, "figure_label": "", "score": None, "width": None, "height": None,
            "sha256": local_remote.get("sha256", ""), "reason": "", "pdf_fallback": pdf_image,
        }
    else:
        record["image"] = pdf_image
        record["image"]["remote_attempt"] = local_remote
        if pdf_image.get("status") == "unavailable":
            # Always leave a visible, explicitly labelled card when neither
            # remote nor PDF extraction produced a real paper image.
            record["image"] = generate_title_card(record, str(article_dir), image_subdir="figure")
            record["image"]["remote_attempt"] = local_remote
            record["image"]["pdf_attempt"] = pdf_image
    record.update({"article_dir": str(article_dir.resolve()), "index_dir": str(index_dir.resolve()),
                   "paper_dir": str(paper_dir.resolve()), "figure_dir": str(figure_dir.resolve())})
    if result.get("source") == "not_oa":
        record["fulltext_status"] = "not_oa"
        record["fulltext_reason"] = "unpaywall_reports_not_oa"
    elif result.get("source") == "oa_unknown":
        record["fulltext_status"] = "oa_unknown"
        record["fulltext_reason"] = result.get("oa_reason", "oa_status_unavailable")
    elif pdf_path.lower().endswith(".pdf") and text and len(text) > 500:
        text_path = _text_sidecar(pdf_path, text)
        if text_path:
            record.update({
                "fulltext_status": "downloaded",
                "fulltext_source": result.get("source", ""),
                "pdf_path": str(Path(pdf_path).resolve()),
                "text_path": str(Path(text_path).resolve()),
                "fulltext_chars": len(text),
            })
    elif paper.get("url") or paper.get("pdf_url") or paper.get("doi"):
        record["fulltext_status"] = "fetch_failed"
    # Set the path before writing so the SQLite catalog receives the complete
    # index location in the same operation (rather than an empty placeholder).
    record["index_path"] = str((index_dir / "index.json").resolve())
    _write_article_index(record, index_dir)
    return record


def complete_missing_assets(
    output_dir: str,
    *,
    limit: int = 20,
    budget_seconds: float = DEFAULT_FULLTEXT_BUDGET_SECONDS,
) -> dict[str, int]:
    """补齐统一 catalog 中尚未尝试或抓取失败的 paper/figure。

    调用方负责七天调度；本函数有批量上限和总时间预算，单篇失败不会阻断
    后续记录。已有 Index 摘要被视为来源元数据，不经过模型也不会被清空。
    """
    candidates = retry_asset_records(output_dir, limit=limit)
    deadline = time.monotonic() + max(1.0, float(budget_seconds))
    totals = {"candidates": len(candidates), "completed": 0, "failed": 0, "deferred": 0}
    for record in candidates:
        if time.monotonic() >= deadline:
            totals["deferred"] += 1
            continue
        try:
            _archive_one(
                record,
                output_dir,
                deadline=deadline,
                restore_original_metadata=False,
            )
            totals["completed"] += 1
        except Exception:
            totals["failed"] += 1
            logger.warning(
                "Periodic literature asset completion failed for %r",
                record.get("title", ""),
                exc_info=True,
            )
    return totals


def complete_missing_figures(
    output_dir: str,
    *,
    limit: int = 20,
    budget_seconds: float = DEFAULT_FULLTEXT_BUDGET_SECONDS,
) -> dict[str, int]:
    """Retry only real figures for feed papers; never enter the PDF chain."""
    scanned = retry_asset_records(output_dir, limit=max(20, int(limit) * 5))
    candidates: list[dict[str, Any]] = []
    for record in scanned:
        image = record.get("image") if isinstance(record.get("image"), dict) else {}
        status = str(record.get("figure_status") or image.get("status") or "not_requested")
        if status in {"", "not_requested", "failed", "unavailable"} or image.get(
            "fallback"
        ) == "title_card":
            candidates.append(record)
        if len(candidates) >= max(1, int(limit)):
            break

    deadline = time.monotonic() + max(1.0, float(budget_seconds))
    totals = {"candidates": len(candidates), "completed": 0, "failed": 0, "deferred": 0}
    for record in candidates:
        if time.monotonic() >= deadline:
            totals["deferred"] += 1
            continue
        try:
            archive_index_and_figure(record, output_dir)
            totals["completed"] += 1
        except Exception:
            totals["failed"] += 1
            logger.warning(
                "Periodic literature figure completion failed for %r",
                record.get("title", ""),
                exc_info=True,
            )
    return totals


def archive_indexes_only(
    papers: list[dict[str, Any]], output_dir: str
) -> list[dict[str, Any]]:
    """保存学术搜索 Index，不进行 PDF 或图片网络请求。

    三个资产目录仍会同时创建；未尝试的 paper/figure 用结构化状态记录，供
    后台补齐任务识别，不能与真实抓取失败混为一谈。
    """
    archived: list[dict[str, Any]] = []
    for paper in papers:
        incoming = dict(paper)
        layout = _article_layout(output_dir, incoming)
        article_dir, index_dir = layout["article"], layout["index"]
        index_path = index_dir / "index.json"
        existing: dict[str, Any] = {}
        if index_path.is_file():
            try:
                parsed = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
            except (OSError, ValueError):
                pass

        record = dict(existing)
        for key, value in incoming.items():
            if value not in (None, "", [], {}):
                if (
                    key == "abstract"
                    and len(str(value).strip())
                    < len(str(record.get(key) or "").strip())
                ):
                    continue
                record[key] = value
            elif key not in record:
                record[key] = value
        record.setdefault("abstract", "")
        record.setdefault("authors", [])
        record.update({
            "article_dir": str(article_dir.resolve()),
            "index_dir": str(index_dir.resolve()),
            "paper_dir": str(layout["paper"].resolve()),
            "figure_dir": str(layout["figure"].resolve()),
            "index_path": str(index_path.resolve()),
            "index_status": "acquired" if record.get("title") else "partial",
            "index_failure_reason": "" if record.get("title") else "missing_title",
            "index_attempted_at": time.time(),
        })
        record.setdefault("fulltext_status", "not_requested")
        record.setdefault("fulltext_reason", "academic_search_index_only")
        record.setdefault("pdf_path", "")
        record.setdefault("text_path", "")
        record.setdefault("figure_status", "not_requested")
        record.setdefault("figure_failure_reason", "academic_search_index_only")
        record.setdefault("image", {
            "schema_version": 1,
            "status": "not_requested",
            "path": "",
            "url": "",
            "source": "",
            "method": "",
            "reason": "academic_search_index_only",
        })
        index_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 1,
            "paper_id": _article_dir_name(record),
            "components": {
                "index": {
                    "status": record["index_status"],
                    "retry_eligible": record["index_status"] != "acquired",
                },
                "paper": {
                    "status": record["fulltext_status"],
                    "retry_eligible": record["fulltext_status"]
                    in {"not_requested", "fetch_failed", "deferred", "oa_unknown"},
                },
                "figure": {
                    "status": record["figure_status"],
                    "retry_eligible": record["figure_status"]
                    in {"not_requested", "failed", "unavailable"},
                },
            },
            "updated_at": time.time(),
        }
        (article_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        archived.append(record)
    upsert_papers(output_dir, archived)
    return archived


def archive_index_and_figure(paper: dict[str, Any], output_dir: str) -> dict[str, Any]:
    """归档后台增量采集所需的两项资产，不触发全文下载。

    E2E 的 ``_archive_one`` 仍负责 PDF/全文；后台 harvester 使用本函数，避免
    用户并不需要的全文下载占满网络和来源配额。figure 优先走公开落地页/图像
    URL，失败后生成明确标注的标题卡片；所有尝试和失败原因写入 index.json。
    """
    incoming = dict(paper)
    layout = _article_layout(output_dir, incoming)
    article_dir, index_dir, paper_dir, figure_dir = (layout["article"], layout["index"], layout["paper"], layout["figure"])
    existing: dict[str, Any] = {}
    old_index = index_dir / "index.json"
    if old_index.is_file():
        try:
            parsed = json.loads(old_index.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing = parsed
        except (OSError, ValueError):
            existing = {}

    # 已完成的真实插图下轮直接跳过；标题卡只是明确标注的临时兜底，下一轮
    # 仍要重试远程插图。否则一次网络抖动会把兜底永久固化。
    existing_image = existing.get("image") if isinstance(existing.get("image"), dict) else {}
    existing_real_figure = (
        existing_image.get("status") == "available"
        and existing_image.get("fallback") != "title_card"
        and "_title_card" not in Path(str(existing_image.get("path") or "")).name
    )
    record = {**existing, **incoming}
    # 同一论文可能由期刊采集和项目画像检索同时发现。来源路线取并集，
    # 不能由后到的一条覆盖先到的一条，否则资讯分栏会随机变化。
    record["feed_acquisition_routes"] = list(dict.fromkeys([
        *list(existing.get("feed_acquisition_routes") or []),
        *list(incoming.get("feed_acquisition_routes") or []),
    ]))
    record["profile_user_ids"] = list(dict.fromkeys([
        *list(existing.get("profile_user_ids") or []),
        *list(incoming.get("profile_user_ids") or []),
    ]))
    record["second_level_domains"] = list(dict.fromkeys([
        *list(existing.get("second_level_domains") or []),
        *list(incoming.get("second_level_domains") or []),
    ]))
    if existing.get("index_status") == "acquired" and existing_real_figure:
        # 资产可以跳过，但新发现路线、用户归属和学科映射仍必须持久化。
        # 否则一篇先被期刊路线发现的论文，之后命中Project画像时会被提前
        # return 掉，永远进不了该用户的小红书候选池。
        record["harvest_skip_reason"] = "index_and_figure_already_acquired"
        record["index_path"] = str((index_dir / "index.json").resolve())
        _write_article_index(record, index_dir)
        return record
    record.setdefault("abstract", "")
    record.setdefault("authors", [])
    record["index_status"] = "acquired" if record.get("title") or record.get("doi") else "failed"
    record["index_failure_reason"] = "" if record["index_status"] == "acquired" else "source_returned_incomplete_metadata"
    record["index_attempted_at"] = time.time()
    record["fulltext_status"] = existing.get("fulltext_status", "not_requested")
    record["fulltext_reason"] = existing.get("fulltext_reason", "periodic_harvest_does_not_fetch_fulltext")
    record.setdefault("pdf_path", existing.get("pdf_path", ""))
    record.setdefault("text_path", existing.get("text_path", ""))
    record.setdefault("fetch_attempted", existing.get("fetch_attempted", []))

    # 首先复用既有 figure；没有才做一次轻量网页图像尝试。
    figure_attempted: list[str] = []
    if existing_real_figure and existing_image.get("path"):
        image = existing_image
    else:
        remote = fetch_paper_image_url(record, str(index_dir))
        figure_attempted.append(str(remote.get("method") or remote.get("source") or "publisher_landing"))
        image = materialize_image_url(remote, str(article_dir), record, image_subdir="figure") if remote.get("status") == "available" else remote
        if image.get("status") != "available":
            failure = str(image.get("reason") or "image_not_found")
            image = generate_title_card(record, str(article_dir), image_subdir="figure")
            image["fallback"] = "title_card"
            image["failure_reason"] = failure
    record["figure_attempted"] = figure_attempted
    record["figure_status"] = "acquired" if image.get("status") == "available" else "failed"
    record["figure_failure_reason"] = str(image.get("failure_reason") or "") if record["figure_status"] == "acquired" else str(image.get("reason") or "image_not_found")
    record["image"] = image
    record.update({"article_dir": str(article_dir.resolve()), "index_dir": str(index_dir.resolve()),
                   "paper_dir": str(paper_dir.resolve()), "figure_dir": str(figure_dir.resolve())})
    # Set the path before writing so the SQLite catalog receives the complete
    # index location in the same operation.
    record["index_path"] = str((index_dir / "index.json").resolve())
    _write_article_index(record, index_dir)
    return record


async def _archive_papers(
    state: State,
    papers_json: str,
    name: str,
    classification_json: str = "",
    **_: Any,
) -> dict:
    """Persist a project literature index and cache arXiv/Sci-Hub full text locally."""
    try:
        papers = _parse_papers_json(papers_json)
    except Exception as exc:
        return {"status": "error", "error": f"papers_json 解析失败: {exc}"}
    # 零篇论文是如实的空结果（判决拆除三波，archive_papers:382 D 删）：
    # manifest/metadata 的 paper_count=0 就是这份账，不逼模型硬塞论文。

    classification: Any = None
    if classification_json:
        try:
            classification = json.loads(classification_json)
        except Exception as exc:
            return {"status": "error", "error": f"classification_json 解析失败: {exc}"}

    output_dir = paper_cache_dir()
    budget_env = os.environ.get("HARNESS_FULLTEXT_BUDGET_SECONDS")
    budget = float(budget_env) if budget_env is not None else DEFAULT_FULLTEXT_BUDGET_SECONDS
    deadline = time.monotonic() + budget if budget > 0 else None
    indexed = [_archive_one(dict(p), output_dir, deadline) for p in papers if isinstance(p, dict)]
    downloaded_count = sum(p["fulltext_status"] == "downloaded" for p in indexed)
    deferred_count = sum(p["fulltext_status"] == "deferred" for p in indexed)
    classification = _reconcile_classification(classification, indexed)
    manifest = {
        "schema_version": 1,
        "paper_count": len(indexed),
        "classification": classification,
        "papers": indexed,
        "storage": {
            "paper_cache_dir": str(Path(output_dir).resolve()),
            "layout": "<doi>/{index,paper,figure}",
            "catalog": str((Path(output_dir) / "literature_catalog.sqlite3").resolve()),
            "scope": "cross_project_local_cache",
        },
        "fulltext_budget_seconds": budget,
        "fulltext_capabilities": fulltext_capabilities(),
    }
    artifact = state.save_artifact(
        artifact_type="literature_index",
        name=name,
        content=json.dumps(manifest, ensure_ascii=False, indent=2),
        metadata={
            "paper_count": len(indexed),
            "downloaded_count": downloaded_count,
            "deferred_count": deferred_count,
            "classified": classification is not None,
        },
    )
    return {
        "status": "success",
        "artifact_id": artifact["id"],
        "paper_count": len(indexed),
        "downloaded_count": downloaded_count,
        "fetch_failed_count": sum(p["fulltext_status"] == "fetch_failed" for p in indexed),
        "not_oa_count": sum(p["fulltext_status"] == "not_oa" for p in indexed),
        "oa_unknown_count": sum(p["fulltext_status"] == "oa_unknown" for p in indexed),
        "deferred_count": deferred_count,
        "paper_cache_dir": str(Path(output_dir).resolve()),
        "fulltext_capabilities": fulltext_capabilities(),
    }


register_tool(
    ToolDefinition(
        name="archive_papers",
        description=(
            "把纳入调研的论文保存为 literature_index artifact，并将可获得的全文"
            "保存到本地 literature/papers/<doi>/{index,paper,figure}/。每篇论文一个跨项目共享目录；保留每篇论文的 source 来源字段；优先按 arXiv ID 下载；非 arXiv DOI "
            "通过 Sci-Hub 尝试。PDF 成功后同时保存抽取文本 .txt，并从本地 PDF 提取代表性图片到"
            "literature/papers/<doi>/figure/；每篇的 index/index.json 记录全部元数据和文件路径。图片失败只记录在 image 元数据中，不阻断索引或全文状态。每篇都会记录"
            "元数据、全文状态、来源和本地路径。分类过的论文必须把 classify_papers "
            "完整返回作为 classification_json 传入。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "papers_json": {"type": "string", "description": "最终纳入调研的完整论文 JSON 数组；每篇必须保留 search_papers 返回的 source 字段（如 arxiv、semantic_scholar、openalex、crossref）。"},
                "name": {"type": "string", "description": "索引名称；建议与 survey_report 名称一致。"},
                "classification_json": {
                    "type": "string",
                    "description": "classify_papers 的完整 JSON 返回；未分类时留空。",
                    "default": "",
                },
            },
            "required": ["papers_json", "name"],
        },
        allowed_node_types=["literature"],
        risk_level="low",
    ),
    _archive_papers,
)
