"""read_reference_paper — 从 fixtures 读取参考论文 PDF，供 hypothesis 节点基于原文发散。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

_FRAMEWORK_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_FIXTURES_DIR = _FRAMEWORK_ROOT / "nodes" / "hypothesis" / "fixtures"
_MAX_CHARS_HARD = 48_000
_DEFAULT_MAX_CHARS = 12_000


def doi_to_fixture_filename(doi: str) -> str:
    """DOI → fixtures 命名：`doi.org10.1039D2TA00652A.pdf`。"""
    normalized = doi.strip()
    if normalized.lower().startswith("doi:"):
        normalized = normalized[4:].strip()
    if normalized.lower().startswith("https://doi.org/"):
        normalized = normalized[len("https://doi.org/"):]
    elif normalized.lower().startswith("http://doi.org/"):
        normalized = normalized[len("http://doi.org/"):]
    slug = normalized.replace("/", "")
    return f"doi.org{slug}.pdf"


def resolve_paper_pdf_path(
    doi: str,
    *,
    explicit_path: str | None = None,
    search_dirs: list[Path] | None = None,
) -> Path | None:
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            return p.resolve()
        for base in search_dirs or [_DEFAULT_FIXTURES_DIR]:
            cand = (base / explicit_path).resolve()
            if cand.is_file():
                return cand

    filename = doi_to_fixture_filename(doi)
    for base in search_dirs or [_DEFAULT_FIXTURES_DIR]:
        cand = (base / filename).resolve()
        if cand.is_file():
            return cand
    return None


def _extract_pdf_pages(
    path: Path, page_start: int, page_limit: int,
) -> tuple[str, int, int, int]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "缺少 pypdf 依赖，无法读取 PDF。请 `pip install pypdf`。"
        ) from e

    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    start = max(0, min(page_start, total_pages))
    end = min(total_pages, start + max(1, page_limit))
    parts: list[str] = []
    for idx in range(start, end):
        page_text = reader.pages[idx].extract_text() or ""
        parts.append(f"--- page {idx + 1}/{total_pages} ---\n{page_text}")
    return "\n\n".join(parts), start, end, total_pages


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n\n[…truncated…]", True


def _slice_section(text: str, section: str) -> str:
    if section == "abstract":
        m = re.search(r"\b(?:1\.?\s*)?introduction\b", text, flags=re.IGNORECASE)
        if m:
            return text[: m.start()].strip()
        return text[:4000].strip()

    if section == "conclusions":
        m = re.search(
            r"\b(?:conclusions?|summary\s+and\s+outlook)\b",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            return text[m.start():].strip()
        return text[-4000:].strip()

    return text


def get_reference_papers_from_state(state: State) -> list[dict[str, Any]]:
    inputs = state.hook_state.get("node_inputs") or {}
    papers = inputs.get("reference_papers") or []
    if isinstance(papers, list):
        return [p for p in papers if isinstance(p, dict)]
    return []


def cache_paper_text(state: State, doi: str, text: str) -> None:
    cache = state.hook_state.setdefault("reference_paper_texts", {})
    if isinstance(cache, dict):
        cache[doi] = text


def get_cached_paper_texts(state: State) -> dict[str, str]:
    cache = state.hook_state.get("reference_paper_texts") or {}
    if isinstance(cache, dict):
        return {k: v for k, v in cache.items() if isinstance(k, str) and isinstance(v, str)}
    return {}


def extract_paper_audit_phrases(text: str) -> list[str]:
    """从论文原文提取用于结论复述检测的短语（摘要 + 结论段 + 数值型 finding）。"""
    cleaned = re.sub(r"--- page \d+/\d+ ---\s*", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    phrases: list[str] = []
    abstract = _slice_section(cleaned, "abstract")
    conclusions = _slice_section(cleaned, "conclusions")
    for block in (abstract, conclusions):
        if len(block) >= 80:
            phrases.append(block)
        for sent in re.split(r"(?<=[.!?。！？])\s+", block):
            s = re.sub(r"\s+", " ", sent).strip()
            if len(s) >= 40:
                phrases.append(s)

    metric_pattern = re.compile(
        r"(?:capacity|barrier|voltage|volume change|reversible|mA\s*h|eV|nodal net|Dirac)[^.;\n]{8,220}",
        flags=re.IGNORECASE,
    )
    for m in metric_pattern.finditer(cleaned):
        phrase = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(phrase) >= 25:
            phrases.append(phrase)

    seen: set[str] = set()
    out: list[str] = []
    for p in phrases:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out[:40]


def extract_paper_metric_tokens(text: str) -> set[str]:
    """提取论文中的关键数值 token，用于检测 hypothesis 是否复述原文定量结论。"""
    cleaned = _slice_section(text, "abstract") + " " + _slice_section(text, "conclusions")
    tokens: set[str] = set()
    for m in re.finditer(
        r"\b\d+(?:\.\d+)?\s*(?:%|mA\s*h\s*g(?:\s*[-−]\s*1|⁻¹)?|eV|V)\b",
        cleaned,
        flags=re.IGNORECASE,
    ):
        tokens.add(re.sub(r"\s+", " ", m.group(0)).lower())
    for m in re.finditer(r"\b(?:303|0\.05|0\.43|2\.0)\b", cleaned):
        tokens.add(m.group(0))
    return tokens


async def _read_reference_paper(
    state: State,
    doi: str,
    section: str = "full",
    page_start: int = 0,
    page_limit: int = 20,
    max_chars: int = _DEFAULT_MAX_CHARS,
    **_: Any,
) -> dict[str, Any]:
    # doi 非空由 parameters_schema 的 minLength=1 声明，派发口核取值。
    doi = (doi or "").strip()

    explicit_path: str | None = None
    for paper in get_reference_papers_from_state(state):
        if paper.get("doi") == doi:
            explicit_path = paper.get("pdf_fixture") or paper.get("pdf_path")
            break

    pdf_path = resolve_paper_pdf_path(doi, explicit_path=explicit_path)
    if pdf_path is None:
        expected = doi_to_fixture_filename(doi)
        return {
            "status": "error",
            "error": (
                f"找不到 DOI {doi!r} 对应的 PDF。"
                f"请将论文放到 nodes/hypothesis/fixtures/{expected}，"
                f"或在 fixture reference_papers 里指定 pdf_fixture。"
            ),
            "expected_filename": expected,
        }

    try:
        raw_text, start_page, end_page, total_pages = _extract_pdf_pages(
            pdf_path, page_start, max(1, page_limit),
        )
    except Exception as e:
        return {"status": "error", "error": f"读取 PDF 失败：{type(e).__name__}: {e}"}

    section_text = _slice_section(raw_text, section)
    max_chars = min(max(1000, int(max_chars)), _MAX_CHARS_HARD)
    content, truncated = _truncate(section_text, max_chars)

    cache_paper_text(state, doi, raw_text)

    return {
        "status": "success",
        "doi": doi,
        "source_uri": f"doi:{doi}",
        "pdf_path": str(pdf_path),
        "section": section,
        "page_start": start_page + 1,
        "page_end": end_page,
        "total_pages": total_pages,
        "content": content,
        "chars_returned": len(content),
        "truncated": truncated,
        "next_page_start": end_page if end_page < total_pages else None,
        "message": (
            f"已读取 {doi}（{section}，pages {start_page + 1}-{end_page}）。"
            "基于原文结论做 audit_hypothesis_vs_conclusions，避免复述 paper finding。"
        ),
    }


register_tool(
    ToolDefinition(
        name="read_reference_paper",
        description=(
            "读取 fixture 中的参考论文 PDF（命名：`doi.org<DOI去斜杠>.pdf`），"
            "返回可引用的原文片段。\n\n"
            "**Use when**（hypothesis 节点 workflow 第 1 步，在读 survey 后）：\n"
            "  - fixture / node_inputs 提供了 reference_papers 或 primary_paper_doi\n"
            "  - 需要基于论文原文（而非 survey 摘要）发散可证伪假设\n"
            "  - audit_hypothesis_vs_conclusions 需要对照原文结论\n\n"
            "**Do NOT use when**：\n"
            "  - 只有 survey_report、没有 reference paper\n"
            "  - 已经读过同一 DOI 且 content 仍在上下文\n\n"
            "**参数**：section=abstract|conclusions|full；大 PDF 用 page_start/page_limit 分段。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "doi": {
                    "type": "string",
                    "minLength": 1,
                    "description": "论文 DOI，如 10.1039/D2TA00652A",
                },
                "section": {
                    "type": "string",
                    "enum": ["full", "abstract", "conclusions"],
                    "default": "full",
                    "description": "返回片段：摘要区 / 结论区 / 当前页段全文",
                },
                "page_start": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "从第几页（0-indexed）开始读",
                },
                "page_limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "最多读多少页",
                },
                "max_chars": {
                    "type": "integer",
                    "default": _DEFAULT_MAX_CHARS,
                    "minimum": 1000,
                    "maximum": _MAX_CHARS_HARD,
                    "description": "返回文本字符上限",
                },
            },
            "required": ["doi"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _read_reference_paper,
)
