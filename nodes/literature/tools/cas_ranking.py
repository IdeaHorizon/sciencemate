"""中科院期刊分区查询。

运行时只依赖 Python 标准库读取 xlsx，避免检索 worker 因未安装 pandas 而把
整个分区表静默降级为空。分区表是用户提供的参考数据，不写入论文元数据源。
"""
from __future__ import annotations

import html
import json
import posixpath
import re
from pathlib import Path
from typing import Iterator, Optional, Tuple
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from core import paths as _paths

_cas_map: dict[str, Tuple[int, bool]] = {}
_cas_lookup_cache: dict[str, Tuple[Optional[int], bool]] = {}
_loaded = False
_load_error = ""
_loaded_path = ""

_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "reference"
    / "cas_journal_ranking_2025.json"
)


def _normalize(name: str) -> tuple[str, str]:
    """标准化期刊名：解码HTML实体、去冠词、标点和空格。"""
    normalized = html.unescape(str(name or "")).lower().strip()
    normalized = re.sub(r"^the\s+", "", normalized)
    normalized = re.sub(r"[^a-z0-9]", "", normalized)
    stripped = (
        normalized[:-1]
        if normalized.endswith("s") and not normalized.endswith("ss")
        else normalized
    )
    return normalized, stripped


def _candidate_keys(name: str) -> list[str]:
    """生成来源常见名称变体，包括PubMed附加的地区括号。"""
    decoded = html.unescape(str(name or "")).strip()
    variants = [decoded]
    without_parentheses = re.sub(r"\s*\([^)]*\)\s*", " ", decoded).strip()
    if without_parentheses and without_parentheses != decoded:
        variants.append(without_parentheses)
    keys: list[str] = []
    for variant in variants:
        for key in _normalize(variant):
            if key and key not in keys:
                keys.append(key)
    return keys


def excel_path() -> Path | None:
    """当前生效的分区表路径；支持显式reference和节点file目录。"""
    configured = _paths.cas_journal_ranking_path()
    if configured is not None:
        return configured
    node_file_dir = Path(__file__).resolve().parents[1] / "file"
    candidates = sorted(node_file_dir.glob("*.xlsx"))
    return candidates[0] if candidates else None


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.text or "" for node in item.findall(".//{*}t")) for item in root.findall("{*}si")]


def _first_sheet_path(archive: ZipFile) -> str:
    """解析workbook关系获取首张工作表，不假定文件一定叫sheet1.xml。"""
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find(".//{*}sheet")
    if sheet is None:
        raise ValueError("xlsx中没有工作表")
    rel_id = next((value for key, value in sheet.attrib.items() if key.endswith("}id")), "")
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = ""
    for relationship in relationships.findall("{*}Relationship"):
        if relationship.attrib.get("Id") == rel_id:
            target = relationship.attrib.get("Target", "")
            break
    if not target:
        raise ValueError("xlsx首张工作表关系缺失")
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def _xlsx_rows(path: Path) -> Iterator[list[str]]:
    """以流式方式读取首张xlsx工作表，保留空单元格位置。"""
    with ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        with archive.open(sheet_path) as stream:
            for _event, row in ET.iterparse(stream, events=("end",)):
                if not row.tag.endswith("}row"):
                    continue
                values: dict[int, str] = {}
                for cell in row.findall("{*}c"):
                    index = _column_index(cell.attrib.get("r", "A1"))
                    cell_type = cell.attrib.get("t", "")
                    if cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.findall(".//{*}t"))
                    else:
                        node = cell.find("{*}v")
                        value = node.text if node is not None and node.text is not None else ""
                        if cell_type == "s" and value:
                            try:
                                value = shared[int(value)]
                            except (IndexError, ValueError):
                                value = ""
                    values[index] = value
                if values:
                    width = max(values) + 1
                    yield [values.get(index, "") for index in range(width)]
                row.clear()


def _load_snapshot(path: Path) -> dict[str, Tuple[int, bool]]:
    """读取构建时生成的紧凑快照；避免每个 worker 重复解析 xlsx。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("不支持的 CAS 快照版本")
    journals = payload.get("journals")
    if not isinstance(journals, dict):
        raise ValueError("CAS 快照缺少 journals 对象")
    result: dict[str, Tuple[int, bool]] = {}
    for key, raw_value in journals.items():
        if not isinstance(key, str) or not isinstance(raw_value, list) or len(raw_value) != 2:
            continue
        try:
            quartile = int(raw_value[0])
        except (TypeError, ValueError):
            continue
        if quartile in {1, 2, 3, 4}:
            result[key] = (quartile, bool(raw_value[1]))
    if not result:
        raise ValueError("CAS 快照没有有效期刊记录")
    return result


def _load_xlsx(path: Path) -> dict[str, Tuple[int, bool]]:
    result: dict[str, Tuple[int, bool]] = {}
    rows = iter(_xlsx_rows(path))
    headers = [str(value).strip() for value in next(rows)]
    journal_index = next(
        (i for i, name in enumerate(headers) if "期刊" in name or "journal" in name.lower()), 0
    )
    quartile_index = next(
        (i for i, name in enumerate(headers) if "分区" in name or "quartile" in name.lower()), 1
    )
    top_index = next((i for i, name in enumerate(headers) if name.lower() == "top"), None)
    for row in rows:
        journal = row[journal_index].strip() if journal_index < len(row) else ""
        raw_quartile = row[quartile_index].strip() if quartile_index < len(row) else ""
        if not journal or journal.lower() == "journal" or not raw_quartile:
            continue
        try:
            quartile = int(float(raw_quartile))
        except ValueError:
            continue
        if quartile not in {1, 2, 3, 4}:
            continue
        raw_top = (
            row[top_index].strip().lower()
            if top_index is not None and top_index < len(row)
            else ""
        )
        is_top = raw_top in {"是", "yes", "true", "1"}
        for key in _candidate_keys(journal):
            if key not in result or quartile < result[key][0]:
                result[key] = (quartile, is_top)
    if not result:
        raise ValueError("分区表读取成功但没有得到任何有效期刊记录")
    return result


def _load() -> None:
    global _cas_map, _loaded, _load_error, _loaded_path
    if _loaded:
        return
    try:
        # 用户显式配置的表优先；默认部署读取快照，缺失时才回退解析 xlsx。
        configured = _paths.cas_journal_ranking_path()
        if configured is not None:
            _cas_map = _load_xlsx(configured)
            _loaded_path = str(configured)
        elif _SNAPSHOT_PATH.exists():
            _cas_map = _load_snapshot(_SNAPSHOT_PATH)
            _loaded_path = str(_SNAPSHOT_PATH)
        else:
            fallback = excel_path()
            if fallback is None:
                raise FileNotFoundError(_paths.cas_journal_ranking_hint())
            _cas_map = _load_xlsx(fallback)
            _loaded_path = str(fallback)
        print(f"[CAS] 已从 {_loaded_path} 加载 {len(_cas_map)} 个标准化期刊键")
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        print(f"[CAS] 加载失败，跳过CAS分区加权: {_load_error}")
    finally:
        _loaded = True


def diagnostics() -> dict[str, object]:
    """提供可观测加载状态，避免运行时依赖问题再次被误诊为期刊未收录。"""
    _load()
    return {
        "path": _loaded_path,
        "loaded_keys": len(_cas_map),
        "error": _load_error,
    }


def lookup_cas(venue: str) -> Tuple[Optional[int], bool]:
    """查询期刊的CAS分区；未收录返回(None, False)。"""
    _load()
    if not venue:
        return (None, False)
    keys = _candidate_keys(venue)
    cache_key = keys[0] if keys else ""
    if cache_key in _cas_lookup_cache:
        return _cas_lookup_cache[cache_key]
    for key in keys:
        if key in _cas_map:
            _cas_lookup_cache[cache_key] = _cas_map[key]
            return _cas_map[key]
    # 只接受覆盖率较高的长名称子串，兼容期刊副标题但避免短名误配。
    for query_key in keys:
        for stored_key, value in _cas_map.items():
            if query_key in stored_key:
                overlap = len(query_key)
            elif stored_key in query_key:
                overlap = len(stored_key)
            else:
                continue
            max_len = max(len(query_key), len(stored_key))
            if max_len >= 8 and overlap / max_len >= 0.6:
                _cas_lookup_cache[cache_key] = value
                return value
    _cas_lookup_cache[cache_key] = (None, False)
    return (None, False)


def score_cas(venue: str) -> float:
    """1区1.0、2区0.8、3区0.5、4区0.3；Top额外0.1。"""
    quartile, is_top = lookup_cas(venue)
    if quartile is None:
        return 0.35
    base = {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.3}.get(quartile, 0.35)
    return min(1.0, base + (0.1 if is_top else 0.0))


def cas_quartile_label(venue: str) -> str:
    quartile, is_top = lookup_cas(venue)
    if quartile is None:
        return "—"
    return f"CAS{quartile}" + ("🔝" if is_top else "")
