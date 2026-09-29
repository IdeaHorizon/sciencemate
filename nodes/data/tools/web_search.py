"""Lightweight public web search tool for the data node.

This is intentionally scoped to reference discovery.  It returns titles,
links, and short snippets so the node can decide whether a public geometry or
benchmark parameter source is worth using.  It does not scrape full pages.
"""
from __future__ import annotations

import asyncio
import html
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from core.state import State
from shared.tools.web import _web_search as _shared_web_search
from nodes.data.planning.schemas import canonical_filename_identity
from nodes.data.planning.store import PlanningStore, approved_plan
from nodes.data.progress import emit_progress

from .scientific_assets import (
    ARCHIVE_SUFFIXES,
    DATASET_SUFFIXES,
    GEOMETRY_OR_STRUCTURE_SUFFIXES,
    PARAMETER_SUFFIXES,
    SCIENTIFIC_ASSET_SUFFIXES,
    inspect_scientific_asset_path,
    infer_scientific_suffix,
    remember_downloaded_scientific_asset,
)


_DOWNLOADABLE_EXTENSIONS = set(SCIENTIFIC_ASSET_SUFFIXES)
_MESH_USEFUL_EXTENSIONS = (
    set(GEOMETRY_OR_STRUCTURE_SUFFIXES)
    | set(ARCHIVE_SUFFIXES)
    | {".dat", ".txt", ".xy", ".csv", ".tsv"}
)
_SCIENTIFIC_DATA_EXTENSIONS = set(DATASET_SUFFIXES)

_ARCHIVE_EXTENSIONS = set(ARCHIVE_SUFFIXES)
_REUSABLE_COMPUTATIONAL_MESH_EXTENSIONS = {
    ".msh", ".vtk", ".vtu", ".cgns", ".foam", ".cas",
    ".med", ".unv", ".exo", ".ex2", ".e",
}
_PARAMETER_EXTENSIONS = set(PARAMETER_SUFFIXES) | {".json"}
_MESH_USEFUL_RE = re.compile(
    r"(coordinate|coordinates|coord|profile|geometry|geom|mesh|grid|cad|surface|"
    r"gmsh|polyMesh|dat\b|iges|step|stl|msh|坐标|几何|网格|叶片|轮廓)",
    flags=re.I,
)
_REFERENCE_SOURCE_RE = re.compile(
    r"(benchmark|test case|case data|dataset|repository|download|open data|database|算例|基准|数据集|下载)",
    flags=re.I,
)
_LOW_VALUE_DOCUMENT_RE = re.compile(
    r"(\.pdf\b|\.docx?\b|\.pptx?\b|paper|article|abstract|citation|论文|摘要)",
    flags=re.I,
)
def _witness_reference_plan_context(state: State, operation: str) -> None:
    """判决拆除（ws:214/247 删 ×2，2026-08-31）。

    「委员会批准前不许查资料」是 S3 的逐字违反 —— 检索只读、零账变、全可逆。
    审批不再是通行许可：不在批准 plan 的对应步骤里跑，只如实留痕，不拦截。
    """
    store = PlanningStore(state)
    active_step = state.hook_state.get("_active_preprocessing_step")
    if not isinstance(active_step, dict) or active_step.get("tool_name") != operation:
        try:
            state.append_transcript(
                "preprocessing_reference_operation_unplanned",
                operation=operation,
                reason="not running as the matching step of a plan")
        except Exception:
            pass
        return None
    plan_kind = str(active_step.get("plan_kind") or "preprocessing_generation")
    status = store.approval_status(plan_kind)
    if (
        status.get("approved")
        and str(active_step.get("plan_id") or "")
        == str(status.get("approved_plan_id") or "")
    ):
        return None
    try:
        state.append_transcript(
            "preprocessing_reference_operation_unplanned",
            operation=operation, reason=status.get("reason"))
    except Exception:
        pass
    return None


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _plan_reference_requests(plan: dict[str, Any], tool_name: str) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for source in (
        plan.get("targeted_search_requests"),
        (plan.get("requirement_analysis") or {}).get("targeted_search_requests"),
        (plan.get("requirement_analysis") or {}).get("search_policy", {}).get("tool_arguments_by_request"),
    ):
        for item in source or []:
            if isinstance(item, dict) and str(item.get("tool") or tool_name) == tool_name:
                requests.append({"source": "targeted_search_requests", **item})
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict) or str(step.get("tool_name") or "") != tool_name:
            continue
        arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
        requests.append({
            "source": "generation_step",
            "step_id": step.get("id"),
            **arguments,
        })
    return requests




def _plan_authorizes_reference_download(plan: dict[str, Any], url: str) -> tuple[bool, str, dict[str, Any] | None]:
    normalized_url = _norm_text(url)
    for request in _plan_reference_requests(plan, "data_web_download"):
        planned_url = _norm_text(request.get("url"))
        if planned_url and planned_url == normalized_url:
            return True, "", request
    return False, "url is not listed in an approved data_web_download generation step", None


_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<label>.*?)</a>',
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]*>")


def _mesh_useful_score(url: str, label: str = "", snippet: str = "") -> int:
    haystack = f"{url} {label} {snippet}"
    ext = Path(urlparse(url or "").path.lower()).suffix
    score = 0
    useful_match = bool(_MESH_USEFUL_RE.search(haystack))
    embedded_file_match = bool(re.search(
        r"\.(dat|geo|msh|cas|stl|step|stp|iges|igs|brep|obj|ply|vtk|vtu|cgns|foam|"
        r"med|unv|exo|ex2|e|zip|tar|tgz|gz)\b",
        haystack,
        flags=re.I,
    ))
    if ext in _MESH_USEFUL_EXTENSIONS:
        score += 8
    elif ext in _PARAMETER_EXTENSIONS:
        score += 4
    if useful_match:
        score += 4
    if embedded_file_match:
        score += 6
    if re.search(r"(github|gitlab|zenodo|figshare|data|dataset|repository|raw|download)", haystack, flags=re.I):
        score += 2
    if _REFERENCE_SOURCE_RE.search(haystack):
        score += 2
    if ext in _REUSABLE_COMPUTATIONAL_MESH_EXTENSIONS:
        score += 12
    if ext in {".zip", ".tar", ".tgz", ".gz"} and re.search(
        r"(openfoam|fluent|reference mesh|case|benchmark|test case|polyMesh|算例|基准)", haystack, flags=re.I
    ):
        score += 10
    if ext in {".step", ".stp", ".iges", ".igs", ".brep", ".stl"}:
        score += 4
    if ext == ".dat" and re.search(r"(processed|raw|measurement|experimental|rds|piv|field)", haystack, flags=re.I):
        score -= 8
    if _LOW_VALUE_DOCUMENT_RE.search(haystack) and ext not in _MESH_USEFUL_EXTENSIONS and not embedded_file_match:
        score -= 6
    return score


def _is_mesh_useful_link(url: str, label: str = "", snippet: str = "", include_parameter_files: bool = True) -> bool:
    ext = Path(urlparse(url or "").path.lower()).suffix
    if ext in _MESH_USEFUL_EXTENSIONS:
        return True
    if include_parameter_files and ext in _PARAMETER_EXTENSIONS and _MESH_USEFUL_RE.search(f"{url} {label} {snippet}"):
        return True
    return _mesh_useful_score(url, label, snippet) >= 4


def _scientific_asset_score(url: str, label: str = "", snippet: str = "", asset_kind: str = "dataset") -> int:
    """Score direct scientific assets without assuming a discipline or solver."""
    haystack = f"{url} {label} {snippet}"
    ext = Path(urlparse(url or "").path.lower()).suffix
    score = 0
    if ext in _SCIENTIFIC_DATA_EXTENSIONS:
        score += 12
    elif ext in _PARAMETER_EXTENSIONS:
        score += 8
    elif ext in _ARCHIVE_EXTENSIONS:
        score += 5
    elif ext in _DOWNLOADABLE_EXTENSIONS:
        score += 4
    if re.search(r"(download|dataset|data repository|zenodo|figshare|dataverse|raw|数据集|下载)", haystack, flags=re.I):
        score += 3
    if asset_kind == "dataset" and re.search(
        r"(netcdf|grib|zarr|hdf5|parquet|csv|time series|spatial field|tensor|table|"
        r"时序|空间场|张量|表格)",
        haystack,
        flags=re.I,
    ):
        score += 4
    if _LOW_VALUE_DOCUMENT_RE.search(haystack) and ext not in _SCIENTIFIC_DATA_EXTENSIONS:
        score -= 8
    return score


def _is_scientific_asset_link(url: str, label: str = "", snippet: str = "", asset_kind: str = "dataset") -> bool:
    ext = Path(urlparse(url or "").path.lower()).suffix
    if asset_kind in {"official_file", "reference_file"}:
        # The caller has an exact filename contract.  At discovery time the
        # repository may expose an application-defined extension (or none),
        # so a generic dataset/parameter suffix score cannot decide whether it
        # is relevant.  Keep the URL as a candidate; the shared exact-file
        # matcher applies the filename/revision contract before acquisition.
        return bool(urlparse(url).scheme in {"http", "https"})
    if asset_kind == "dataset" and ext in _SCIENTIFIC_DATA_EXTENSIONS:
        return True
    if asset_kind == "parameters" and ext in _PARAMETER_EXTENSIONS:
        return True
    if asset_kind == "archive" and ext in _ARCHIVE_EXTENSIONS:
        return True
    return _scientific_asset_score(url, label, snippet, asset_kind) >= 8




def _allowed_extensions_for_asset_kind(asset_kind: str) -> set[str]:
    if asset_kind in {"geometry", "geometry_or_mesh", "mesh", "structure"}:
        return set(_MESH_USEFUL_EXTENSIONS) | set(_PARAMETER_EXTENSIONS)
    if asset_kind == "parameters":
        return set(_PARAMETER_EXTENSIONS) | {".csv", ".tsv", ".txt", ".dat"}
    if asset_kind == "archive":
        return set(_ARCHIVE_EXTENSIONS)
    # Exact official files are selected by an approved URL and may use an
    # application-defined or extensionless filename.  Their content is still
    # constrained below to a bounded, non-HTML response and is recorded with
    # URL/hash provenance; applying a dataset suffix whitelist here rejects
    # valid solver tables and mapping files before that contract can run.
    if asset_kind in {"official_file", "reference_file"}:
        return set()
    return set(_SCIENTIFIC_DATA_EXTENSIONS) | set(_ARCHIVE_EXTENSIONS) | set(_PARAMETER_EXTENSIONS)


def _clean_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value or "")
    text = html.unescape(text)
    return " ".join(text.split())


def _safe_download_dir(state: State) -> Path:
    root = Path(str(state.root)).expanduser().resolve()
    # Downloads are run-local intermediates.  Keep the public data-node root
    # reserved for the atomically published preprocessing package.
    download_dir = root / ".data_node_work" / "downloads"
    return download_dir


def _safe_filename(value: str, default: str = "downloaded_file") -> str:
    candidate = unquote((value or "").rsplit("/", 1)[-1].split("?", 1)[0]).strip()
    candidate = re.sub(r"[^A-Za-z0-9._+-]+", "_", candidate).strip("._")
    if not candidate:
        candidate = default
    return candidate[:160]


def _extract_mesh_archive(archive: Path) -> dict[str, Any]:
    """Safely extract a ZIP and identify reusable computational mesh assets."""
    if archive.suffix.lower() != ".zip":
        return {"status": "skipped"}
    target = archive.parent / archive.stem
    target.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_size = 0
    with zipfile.ZipFile(archive) as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        if len(members) > 2000:
            return {"status": "error", "error": "archive contains too many files"}
        for member in members:
            parts = Path(member.filename).parts
            if not parts or "__MACOSX" in parts or parts[-1] == ".DS_Store":
                continue
            # 判决拆除三波（ws:346 并入 352，2026-09-02）：绝对路径 / `..` 成员
            # 的判据由下方 resolve() 包含性检查完整覆盖（同样在写盘前），一题一答。
            total_size += int(member.file_size)
            if total_size > 250_000_000:
                return {"status": "error", "error": "archive expands beyond 250 MB safety limit"}
            output = (target / Path(*parts)).resolve()
            if target.resolve() not in output.parents:
                return {"status": "error", "error": "archive member escapes extraction directory"}
            output.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, output.open("wb") as destination:
                destination.write(source.read())
            extracted.append(output)

    priority = {
        ".cif": 0, ".vasp": 0, ".poscar": 0, ".contcar": 0, ".xyz": 1,
        ".cas": 0, ".cgns": 1, ".foam": 2, ".med": 3, ".unv": 3,
        ".exo": 3, ".ex2": 3, ".e": 3, ".msh": 4, ".vtk": 4, ".vtu": 4,
        ".geo": 5, ".step": 6, ".stp": 6, ".iges": 7, ".igs": 7,
        ".brep": 8, ".stl": 9,
    }
    assets = sorted(
        (path for path in extracted if path.suffix.lower() in priority),
        key=lambda path: (priority[path.suffix.lower()], -path.stat().st_size, str(path)),
    )
    return {
        "status": "success",
        "extracted_dir": str(target),
        "extracted_file_count": len(extracted),
        "extracted_files": [str(path) for path in extracted[:200]],
        "mesh_assets": [str(path) for path in assets[:20]],
        "preferred_geometry_file": str(assets[0]) if assets else None,
    }


def _scientific_archive_assets(archive_result: dict[str, Any], asset_kind: str) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for value in archive_result.get("extracted_files") or []:
        path = Path(str(value))
        if not path.is_file():
            continue
        profile = inspect_scientific_asset_path(path)
        kind = str(profile.get("asset_kind") or "")
        if asset_kind == "dataset" and kind != "dataset":
            continue
        if asset_kind == "parameters" and kind not in {"parameters", "dataset"}:
            continue
        assets.append({
            "path": str(path.resolve()),
            "format": profile.get("format"),
            "asset_kind": kind,
            "data_model_kind": profile.get("data_model_kind"),
            "size_bytes": profile.get("size_bytes"),
        })
    return sorted(assets, key=lambda item: (-int(item.get("size_bytes") or 0), str(item.get("path"))))


def _filename_from_response(url: str, response: httpx.Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?(?P<name>[^"\';]+)', disposition, flags=re.I)
    if match:
        return _safe_filename(match.group("name"))
    path = urlparse(str(response.url or url)).path
    return _safe_filename(path)


def _url_is_allowed(url: str) -> tuple[bool, str]:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False, "only http/https URLs are allowed"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL hostname is missing"
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return False, "localhost/private download targets are not allowed"
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return False, "private network download targets are not allowed"
    return True, ""


def _extract_download_links(
    page_url: str,
    text: str,
    limit: int = 30,
    include_reference_documents: bool = False,
    asset_kind: str = "geometry_or_mesh",
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _LINK_RE.finditer(text or ""):
        href = html.unescape(match.group("href") or "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        absolute = urljoin(page_url, href)
        path = urlparse(absolute).path.lower()
        ext = Path(path).suffix
        label = _clean_html(match.group("label") or "")
        reference_document = bool(
            include_reference_documents
            and (
                ext in {".pdf", ".doc", ".docx"}
                or re.search(r"(supporting|supplementary|esi|article|paper|full text|补充材料|论文)", label, flags=re.I)
            )
        )
        useful = (
            _is_mesh_useful_link(absolute, label)
            if asset_kind in {"geometry", "geometry_or_mesh", "mesh", "structure"}
            else _is_scientific_asset_link(absolute, label, asset_kind=asset_kind)
        )
        if not reference_document and not useful:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append({
            "url": absolute,
            "label": label,
            "extension": ext,
            "mesh_useful_score": str(_mesh_useful_score(absolute, label)),
            "scientific_asset_score": str(_scientific_asset_score(absolute, label, asset_kind=asset_kind)),
        })
    score_key = "mesh_useful_score" if asset_kind in {"geometry", "geometry_or_mesh", "mesh", "structure"} else "scientific_asset_score"
    links.sort(key=lambda item: int(item.get(score_key) or "0"), reverse=True)
    del links[limit:]
    return links


def _looks_like_html(response: httpx.Response, data: bytes) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    prefix = data[:300].lstrip().lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")


def _parse_github_url(url: str) -> dict[str, str] | None:
    parsed = urlparse(url or "")
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
        return {
            "kind": parts[2],
            "owner": owner,
            "repo": repo,
            "ref": parts[3],
            "path": "/".join(parts[4:]),
        }
    return {
        "kind": "repo",
        "owner": owner,
        "repo": repo,
        "ref": "",
        "path": "",
    }


async def _github_download_or_discover(
    *,
    client: httpx.AsyncClient,
    state: State,
    url: str,
    output_name: str,
    max_bytes: int,
    discover_links: bool,
    download_first_matching_link: bool,
    link_pattern: str,
    headers: dict[str, str],
    authorized_by_approved_search: bool = False,
    asset_kind: str = "geometry_or_mesh",
    expected_filename: str = "",
    expected_revision: str = "",
) -> dict[str, Any] | None:
    info = _parse_github_url(url)
    if not info:
        return None
    owner = info["owner"]
    repo = info["repo"]
    if info["kind"] == "blob":
        # A branch name is mutable and cannot close an exact-file evidence
        # gap. Resolve the approved repository ref to its immutable commit in
        # the provider adapter, then download that exact raw object. Failure to
        # resolve leaves the candidate auditable but unverified downstream.
        requested_ref = str(expected_revision or info["ref"]).strip()
        resolved_ref = requested_ref
        try:
            revision_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{requested_ref}",
                headers={**headers, "Accept": "application/vnd.github+json"},
            )
            revision_response.raise_for_status()
            candidate_sha = str(revision_response.json().get("sha") or "").strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", candidate_sha):
                resolved_ref = candidate_sha.lower()
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{resolved_ref}/{info['path']}"
        if discover_links:
            # Link resolution must not fetch the final file. The reference
            # ledger separately authorizes acquisition of this raw URL.
            return {
                "status": "needs_download_selection",
                "url": url,
                "download_links": [{
                    "url": raw_url,
                    "label": Path(info["path"]).name,
                    "extension": Path(info["path"]).suffix,
                }],
                "download_link_count": 1,
            }
        downloaded = await data_web_download(
            state=state,
            url=raw_url,
            output_name=output_name,
            max_bytes=max_bytes,
            discover_links=False,
            asset_kind=asset_kind,
            expected_filename=expected_filename,
            expected_revision=expected_revision,
            _authorized_by_approved_search=authorized_by_approved_search,
        )
        if isinstance(downloaded, dict) and downloaded.get("status") == "success":
            immutable_commit = bool(re.fullmatch(r"[0-9a-f]{40}", resolved_ref, flags=re.I))
            downloaded = {
                **downloaded,
                "revision": resolved_ref,
                "requested_revision": requested_ref,
                **({"commit": resolved_ref} if immutable_commit else {"tag": resolved_ref}),
            }
        return downloaded

    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    if info["path"]:
        api_url += f"/{info['path']}"
    params = {"ref": info["ref"]} if info["ref"] else {}
    try:
        response = await client.get(api_url, params=params, headers={**headers, "Accept": "application/vnd.github+json"})
        response.raise_for_status()
    except Exception:
        return None
    data = response.json()
    items = data if isinstance(data, list) else [data]
    links: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        name = str(item.get("name") or "")
        download_url = str(item.get("download_url") or "")
        html_url = str(item.get("html_url") or "")
        ext = Path(name.lower()).suffix
        if item_type == "file" and download_url:
            useful = (
                _is_mesh_useful_link(download_url, name)
                if asset_kind in {"geometry", "geometry_or_mesh", "mesh", "structure"}
                else _is_scientific_asset_link(download_url, name, asset_kind=asset_kind)
            )
            if useful:
                links.append({
                    "url": download_url,
                    "label": name,
                    "extension": ext,
                    "mesh_useful_score": str(_mesh_useful_score(download_url, name)),
                    "scientific_asset_score": str(_scientific_asset_score(download_url, name, asset_kind=asset_kind)),
                })
        elif item_type == "dir" and html_url:
            if _is_mesh_useful_link(html_url, name):
                links.append({
                    "url": html_url,
                    "label": f"{name}/",
                    "extension": "",
                    "mesh_useful_score": str(_mesh_useful_score(html_url, name)),
                    "scientific_asset_score": str(_scientific_asset_score(html_url, name, asset_kind=asset_kind)),
                })
    # A repository root or directory is a source page, not the asset itself.
    # For an approved exact-file request, use GitHub's existing tree API to
    # resolve one uniquely named file below that page.  The planner's later
    # filename/revision contract still decides whether it may be acquired.
    target_name = canonical_filename_identity(expected_filename)
    if discover_links and target_name and not any(
        canonical_filename_identity(link.get("label")) == target_name for link in links
    ):
        reference = info["ref"]
        try:
            if not reference:
                # An exact-file request may already carry the target tag even
                # when the search result only identifies the repository root.
                # Resolve the tree at that revision instead of silently using
                # the mutable default branch.
                reference = str(expected_revision or "").strip()
            if not reference:
                repository = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}",
                    headers={**headers, "Accept": "application/vnd.github+json"},
                )
                repository.raise_for_status()
                reference = str(repository.json().get("default_branch") or "")
            if reference:
                # Semantic versions are commonly tagged either ``4.4`` or
                # ``v4.4``.  Try the approved revision and its transport-level
                # v-prefix equivalent, without issuing another web search.
                references = [reference]
                unprefixed = reference.lstrip("vV")
                prefixed = f"v{unprefixed}" if unprefixed else ""
                if prefixed and prefixed not in references:
                    references.append(prefixed)
                if unprefixed and unprefixed not in references:
                    references.append(unprefixed)
                prefix = f"{info['path'].strip('/')}/" if info["path"] else ""
                for tree_reference in references:
                    try:
                        tree = await client.get(
                            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{tree_reference}",
                            params={"recursive": "1"},
                            headers={**headers, "Accept": "application/vnd.github+json"},
                        )
                        tree.raise_for_status()
                    except httpx.HTTPError:
                        continue
                    matches = [
                        str(item.get("path") or "")
                        for item in tree.json().get("tree") or []
                        if isinstance(item, dict)
                        and item.get("type") == "blob"
                        and canonical_filename_identity(item.get("path")) == target_name
                        and (not prefix or str(item.get("path") or "").startswith(prefix))
                    ]
                    if len(matches) == 1:
                        path = matches[0]
                        links.append({
                            "url": f"https://raw.githubusercontent.com/{owner}/{repo}/{tree_reference}/{path}",
                            "label": Path(path).name,
                            "extension": Path(path).suffix,
                            "scientific_asset_score": "0",
                        })
                        break
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # The source-page result remains usable as search evidence; a
            # failed optional repository listing must not turn it into a
            # transport failure or bypass the approved acquisition contract.
            pass
    # Exact-file discovery must not promote unrelated repository files.  The
    # contents API commonly returns README/.gitignore at the root; retain only
    # the one filename requested by the approved reference contract.
    if asset_kind in {"official_file", "reference_file"} and target_name:
        links = [
            link for link in links
            if canonical_filename_identity(link.get("label")) == target_name
        ]
    score_key = "mesh_useful_score" if asset_kind in {"geometry", "geometry_or_mesh", "mesh", "structure"} else "scientific_asset_score"
    links.sort(key=lambda item: int(item.get(score_key) or "0"), reverse=True)
    if download_first_matching_link and links:
        pattern = re.compile(link_pattern, flags=re.I) if link_pattern else None
        for link in links:
            haystack = f"{link.get('url', '')} {link.get('label', '')}"
            if pattern is None or pattern.search(haystack):
                return await data_web_download(
                    state=state,
                    url=link["url"],
                    output_name=output_name,
                    max_bytes=max_bytes,
                    discover_links=False,
                    asset_kind=asset_kind,
                    expected_filename=expected_filename,
                    expected_revision=expected_revision,
                    _authorized_by_approved_search=authorized_by_approved_search,
                )
    return {
        "status": "needs_download_selection",
        "message": (
            "GitHub 路径已解析。已通过 GitHub contents API 提取候选文件/目录；"
            "请选择一个可下载文件 URL 再次调用 data_web_download，或设置 download_first_matching_link=true。"
        ),
        "url": url,
        "content_type": "application/vnd.github+json",
        "download_links": links,
        "download_link_count": len(links),
        "source": "github_contents_api",
    }








async def data_web_search(
    *,
    state: State,
    query: str,
    limit: int = 5,
    site: str | None = None,
    auto_discover_downloads: bool = True,
    max_discovery_pages: int = 2,
    search_mode: str = "generic",
    asset_kind: str = "reference",
    expected_filename: str = "",
    expected_revision: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Discover public references; materialization remains a separate download step."""
    # 判决拆除三波（ws:751 → schema，2026-09-02）：query 非空由注册 schema 的
    # minLength:1 声明、派发口核一次；这里只做空白归一。
    query = " ".join((query or "").split())
    _witness_reference_plan_context(state, "data_web_search")

    if site:
        query = f"site:{site} {query}"
    limit = max(1, min(int(limit or 5), 20))
    asset_kind = str(asset_kind or "reference").strip().lower()
    search_mode = str(search_mode or "generic").strip().lower()
    emit_progress(state, "web_search", query[:120], provider="shared", search_mode=search_mode)

    search_task = asyncio.create_task(_shared_web_search(state, query=query, limit=limit))
    elapsed = 0
    while True:
        try:
            provider_result = await asyncio.wait_for(
                asyncio.shield(search_task), timeout=15
            )
            break
        except asyncio.TimeoutError:
            elapsed += 15
            emit_progress(
                state,
                "web_search_progress",
                "public search providers are still responding",
                elapsed_seconds=elapsed,
                query=query[:120],
            )
    if provider_result.get("status") != "success":
        emit_progress(state, "web_search_done", "deferred_dependency", result_count=0)
        return {
            "status": "deferred_dependency",
            "status_class": "provider_unavailable",
            "error": provider_result.get("error") or "public search provider unavailable",
            "query": query,
            "search_mode": search_mode,
            "asset_kind": asset_kind,
            "providers_tried": [{
                "provider": provider_result.get("backend") or "shared",
                "status": provider_result.get("status") or "error",
                "error": provider_result.get("error"),
            }],
            "results": [],
            "result_count": 0,
        }

    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in provider_result.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        allowed, _reason = _url_is_allowed(url)
        if not allowed or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({
            "title": str(item.get("title") or url).strip(),
            "url": url,
            "snippet": str(item.get("snippet") or "").strip(),
            "provider": provider_result.get("backend") or "shared",
        })

    # Discovery may inspect a small number of result pages for links, but it
    # never promotes those leads to scientific inputs. The next planning pass
    # creates an exact data_web_download step, where URL/content/hash checks
    # remain mandatory.
    discovered_downloads: list[dict[str, Any]] = []
    discovery_limit = max(0, min(int(max_discovery_pages or 0), 3))
    if auto_discover_downloads and discovery_limit:
        for item in list(results)[:discovery_limit]:
            url = str(item.get("url") or "")
            if Path(urlparse(url).path).suffix.lower() in _DOWNLOADABLE_EXTENSIONS:
                continue
            emit_progress(state, "web_search_discovery", url[:120])
            discovery = await data_web_download(
                state=state,
                url=url,
                max_bytes=10_000_000,
                discover_links=True,
                download_first_matching_link=False,
                allow_reference_documents=asset_kind in {"reference", "parameters"},
                asset_kind=asset_kind,
                timeout_seconds=15.0,
                _authorized_by_approved_search=True,
                _discovery_only=True,
            )
            links = [
                link for link in discovery.get("download_links") or []
                if isinstance(link, dict) and str(link.get("url") or "").strip()
            ]
            if not links:
                continue
            discovered_downloads.append({
                "source_result": item,
                "url": discovery.get("url") or url,
                "download_links": links[:10],
            })
            for link in links:
                link_url = str(link.get("url") or "").strip()
                allowed, _reason = _url_is_allowed(link_url)
                if not allowed or link_url in seen_urls:
                    continue
                seen_urls.add(link_url)
                results.append({
                    "title": str(
                        link.get("label")
                        or Path(unquote(urlparse(link_url).path)).name
                        or link_url
                    ),
                    "url": link_url,
                    "snippet": f"Discovered from {url}",
                    "provider": "approved_page_discovery",
                })

    # Keep acquisition intent visible for compatibility, but do not download
    # inside discovery even if an older plan still carries auto-download flags.
    direct_candidates = [
        item for item in results
        if Path(urlparse(str(item.get("url") or "")).path).suffix.lower()
        in _DOWNLOADABLE_EXTENSIONS
    ]
    if expected_filename:
        identity = canonical_filename_identity(expected_filename)
        direct_candidates.sort(
            key=lambda item: canonical_filename_identity(
                Path(unquote(urlparse(str(item.get("url") or "")).path)).name
            ) == identity,
            reverse=True,
        )

    emit_progress(
        state,
        "web_search_done",
        "success",
        result_count=len(results),
        provider_count=1,
    )
    return {
        "status": "success",
        "outcome_kind": "results" if results else "empty_success",
        "query": query,
        "source": "shared_web_search",
        "providers_tried": [{
            "provider": provider_result.get("backend") or "shared",
            "status": "success",
            "result_count": len(provider_result.get("results") or []),
        }],
        "results": results[: max(limit, len(direct_candidates))],
        "result_count": len(results),
        "search_mode": search_mode,
        "asset_kind": asset_kind,
        "expected_filename": expected_filename,
        "expected_revision": expected_revision,
        "discovered_downloads": discovered_downloads,
        "acquisition_candidates": direct_candidates[:10],
        "discovery_contract": {
            "scope": "public_read_only",
            "queries_used": [query],
            "materialization_requires_approved_download": True,
        },
        "usage_note": (
            "Search results are discovery leads. Use a separately approved data_web_download "
            "step before treating a remote asset as a local scientific input."
        ),
    }
async def data_web_download(
    *,
    state: State,
    url: str,
    output_name: str = "",
    max_bytes: int = 50_000_000,
    discover_links: bool = True,
    download_first_matching_link: bool = False,
    link_pattern: str = "",
    allow_non_mesh_file: bool = False,
    allow_reference_documents: bool = False,
    asset_kind: str = "geometry_or_mesh",
    expected_filename: str = "",
    expected_revision: str = "",
    timeout_seconds: float = 30.0,
    **_: Any,
) -> dict[str, Any]:
    """Download a public file into the current run state root.

    If the URL points to an HTML page, the tool returns candidate downloadable
    geometry/data links.  With download_first_matching_link=true it downloads
    the first candidate whose URL/label matches link_pattern, or the first
    candidate if no pattern is provided.
    """
    url = (url or "").strip()
    asset_kind = str(asset_kind or "geometry_or_mesh").strip().lower()
    _authorized_by_approved_search = bool(_.get("_authorized_by_approved_search"))
    discovery_only = bool(_.get("_discovery_only"))
    if not _authorized_by_approved_search:
        _witness_reference_plan_context(state, "data_web_download")
        store = PlanningStore(state)
        reference_status = store.approval_status("reference_evidence_only")
        reference_record = store.get_plan(reference_status.get("approved_plan_id")) if reference_status.get("approved") else None
        plan = reference_record.get("plan") if reference_record else (approved_plan(state) or {})
        authorized, reason, _authorized_request = _plan_authorizes_reference_download(plan, url)
        if not authorized:
            emit_progress(state, "web_download_blocked", "not authorized by approved plan", reason=reason)
            return {
                "status": "not_authorized_by_plan",
                "error": (
                    "Execution may only download URLs that were selected during planning. "
                    "Add a data_web_download generation step to the approved plan before downloading."
                ),
                "url": url,
                "reason": reason,
                "download_skipped": True,
                "approved_download_requests": _plan_reference_requests(plan, "data_web_download"),
            }
        if _authorized_request:
            asset_kind = str(_authorized_request.get("asset_kind") or asset_kind).strip().lower()
            expected_filename = str(
                _authorized_request.get("expected_filename") or expected_filename
            ).strip()
            expected_revision = str(
                _authorized_request.get("expected_revision") or expected_revision
            ).strip()
            output_name = str(
                _authorized_request.get("output_name") or output_name or expected_filename
            ).strip()
        # The approved URL may be a repository file page.  Provider adapters
        # deterministically resolve that page to its raw-content URL inside
        # this same tool call. Preserve the authorization already granted to
        # the approved step so the internal resolution is not misclassified
        # as a second, unplanned download request.
        _authorized_by_approved_search = True
    emit_progress(state, "web_download", url[:120])
    allowed, reason = _url_is_allowed(url)
    if not allowed:
        emit_progress(state, "web_download_done", "blocked_url")
        return {"status": "error", "error": reason, "url": url}
    download_dir = _safe_download_dir(state)
    index_path = download_dir / "download_index.json"
    try:
        download_index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        download_index = {}
    cached = download_index.get(url)
    if isinstance(cached, dict):
        cached_path = Path(str(cached.get("saved_path") or ""))
        if cached_path.is_file() and cached_path.stat().st_size > 0:
            hook_state = getattr(state, "hook_state", None)
            if isinstance(hook_state, dict) and cached_path.suffix.lower() in _MESH_USEFUL_EXTENSIONS:
                preferred = (cached.get("archive") or {}).get("preferred_geometry_file") or str(cached_path)
                hook_state["scientific_mesh_pending_asset_evaluation"] = {
                    "path": preferred,
                    "downloaded_path": str(cached_path),
                    "url": url,
                    "sha256": cached.get("sha256"),
                    "extension": Path(str(preferred)).suffix.lower(),
                }
            cached_result = {**cached, "status": "success", "cache_hit": True}
            remember_downloaded_scientific_asset(state, cached_result)
            return cached_result
    max_bytes = max(1024, min(int(max_bytes or 50_000_000), 250_000_000))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,application/octet-stream,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    timeout_seconds = max(5.0, min(float(timeout_seconds or 30.0), 60.0))
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        github_result = await _github_download_or_discover(
            client=client,
            state=state,
            url=url,
            output_name=output_name,
            max_bytes=max_bytes,
            discover_links=discover_links,
            download_first_matching_link=download_first_matching_link,
            link_pattern=link_pattern,
            headers=headers,
            authorized_by_approved_search=_authorized_by_approved_search,
            asset_kind=asset_kind,
            expected_filename=expected_filename,
            expected_revision=expected_revision,
        )
        if github_result is not None:
            return github_result
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            return {
                "status": "error",
                # A missing/invalid object is a reference-contract failure,
                # not a transport fault.  Retrying a 404 only repeats an
                # unverified candidate URL and hides the actionable cause.
                "status_class": "rate_limited" if status_code in {429, 503} else ("transient" if status_code in {408, 425} or status_code >= 500 else "permanent"),
                "error": f"HTTP {status_code}",
                "url": url,
            }
        except httpx.RequestError as exc:
            return {
                "status": "error",
                "status_class": "transient",
                "error": f"{type(exc).__name__}: {exc}",
                "url": url,
            }
        data = response.content
        if len(data) > max_bytes:
            return {
                "status": "error",
                "error": f"download exceeded max_bytes={max_bytes}",
                "url": str(response.url),
                "content_length": len(data),
            }

        if _looks_like_html(response, data):
            text = data.decode(response.encoding or "utf-8", errors="replace")
            links = _extract_download_links(
                str(response.url),
                text,
                limit=10,
                include_reference_documents=allow_reference_documents,
                asset_kind=asset_kind,
            )
            if discover_links and download_first_matching_link and links:
                pattern = re.compile(link_pattern, flags=re.I) if link_pattern else None
                chosen = None
                for link in links:
                    haystack = f"{link.get('url', '')} {link.get('label', '')}"
                    if pattern is None or pattern.search(haystack):
                        chosen = link
                        break
                if chosen:
                    return await data_web_download(
                        state=state,
                        url=chosen["url"],
                        output_name=output_name,
                        max_bytes=max_bytes,
                        discover_links=False,
                        allow_non_mesh_file=allow_non_mesh_file or allow_reference_documents,
                        allow_reference_documents=allow_reference_documents,
                        asset_kind=asset_kind,
                        expected_filename=expected_filename,
                        expected_revision=expected_revision,
                        timeout_seconds=timeout_seconds,
                        _authorized_by_approved_search=_authorized_by_approved_search,
                    )
            return {
                "status": "needs_download_selection" if discover_links else "error",
                "status_class": None if discover_links else "not_downloadable_asset",
                "message": (
                    "URL 是 HTML 页面。已提取候选几何/数据下载链接；请选择一个链接再次调用 data_web_download，"
                    "或设置 download_first_matching_link=true。"
                    if discover_links
                    else "URL 返回的是 HTML 页面而不是真实几何/网格文件，已拒绝保存。"
                ),
                "error": None if discover_links else "download target returned HTML instead of a mesh/geometry asset",
                "url": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "download_links": links,
                "download_link_count": len(links),
            }

        if discovery_only:
            resolved_url = str(response.url)
            return {
                "status": "needs_download_selection",
                "url": resolved_url,
                "download_links": [{
                    "url": resolved_url,
                    "label": Path(unquote(urlparse(resolved_url).path)).name or resolved_url,
                    "extension": Path(urlparse(resolved_url).path).suffix.lower(),
                }],
                "download_link_count": 1,
            }

        filename = _safe_filename(output_name) if output_name else _filename_from_response(url, response)
        content_type = response.headers.get("content-type", "").lower()
        head_text = data[:2048].decode("latin-1", errors="ignore").lower()
        detected_suffix = ""
        if "iges" in content_type or "acis data in iges format" in head_text:
            detected_suffix = ".iges"
        elif "step" in content_type or re.search(r"iso-10303-21", head_text):
            detected_suffix = ".step"
        if "." not in filename:
            suffix = Path(urlparse(str(response.url)).path).suffix
            if suffix:
                filename += suffix
        if detected_suffix and Path(filename).suffix.lower() != detected_suffix:
            filename = f"{Path(filename).stem}{detected_suffix}"
        if not Path(filename).suffix:
            scientific_suffix = infer_scientific_suffix(data, content_type)
            if scientific_suffix:
                filename = f"{filename}{scientific_suffix}"
        final_ext = Path(filename.lower()).suffix
        geometry_expected = asset_kind in {"geometry", "geometry_or_mesh", "mesh", "structure"}
        allowed_extensions = _allowed_extensions_for_asset_kind(asset_kind)
        # Solver inputs, mapping tables, and control vocabularies often have
        # no conventional or recognizable suffix.  A plan-approved parameter download may
        # therefore be accepted as text when the response is non-HTML,
        # bounded, and explicitly classified as parameters.  The check stays
        # narrow: datasets and geometry still require their recognized data
        # or mesh formats.
        approved_text_parameter = (
            asset_kind == "parameters"
            and content_type.startswith("text/")
            and not re.search(r"<\s*(?:html|!doctype)\b", head_text, flags=re.I)
        )
        # An ``official_file`` is already bound to one exact URL by the
        # approved acquisition step.  Accept its non-HTML payload regardless
        # of suffix (for example a solver's Vtable or lookup table), then let
        # the executor persist its filename, hash, revision and provenance.
        # This is intentionally not a general relaxation for datasets or
        # geometry downloads.
        approved_official_file = (
            asset_kind in {"official_file", "reference_file"}
            and bool(output_name or Path(urlparse(str(response.url)).path).name)
            and not re.search(r"<\s*(?:html|!doctype)\b", head_text, flags=re.I)
        )
        recognized_for_kind = (
            final_ext in allowed_extensions
            or approved_text_parameter
            or approved_official_file
        )
        if not allow_non_mesh_file and not recognized_for_kind:
            # 判决拆除 O11（ws:2402 降格，2026-08-31）：丢弃已下载字节让 agent
            # 无法诊断 —— 落 quarantine（字节保留、可检视），不计交付物、
            # 不进 download index、不注册为科学资产。
            quarantine_dir = download_dir / "quarantine"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            quarantine_path = quarantine_dir / filename
            if quarantine_path.exists():
                digest8 = hashlib.sha256(str(response.url).encode()).hexdigest()[:8]
                quarantine_path = quarantine_path.with_name(
                    f"{quarantine_path.stem}_{digest8}{quarantine_path.suffix}")
            quarantine_path.write_bytes(data)
            try:
                state.append_transcript(
                    "web_download_quarantined",
                    url=str(response.url), quarantine_path=str(quarantine_path),
                    asset_kind=asset_kind, content_type=response.headers.get("content-type", ""))
            except Exception:
                pass
            return {
                "status": "quarantined",
                "status_class": "unrecognized_scientific_asset",
                "message": (
                    "download target is not a recognized file for the approved scientific asset kind; "
                    "bytes were preserved under quarantine for diagnosis and are not counted as a deliverable"
                ),
                "url": str(response.url),
                "filename": filename,
                "quarantine_path": str(quarantine_path),
                "bytes": len(data),
                "content_type": response.headers.get("content-type", ""),
                "asset_kind": asset_kind,
                "allowed_extensions": sorted(allowed_extensions),
            }
        # 判决拆除（ws:2421 删，2026-08-31）：正则判「像不像 CFD」是任意审美
        # 阈值 + 预测失败双料 —— 文件照存，取舍归模型。
        download_dir.mkdir(parents=True, exist_ok=True)
        destination = download_dir / filename
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            digest = hashlib.sha256(str(response.url).encode()).hexdigest()[:8]
            destination = destination.with_name(f"{stem}_{digest}{suffix}")
        sha256 = hashlib.sha256(data).hexdigest()
        for existing in download_dir.iterdir():
            if not existing.is_file() or existing == index_path or existing.stat().st_size != len(data):
                continue
            try:
                if hashlib.sha256(existing.read_bytes()).hexdigest() == sha256:
                    result = {
                        "status": "success",
                        "url": str(response.url),
                        "saved_path": str(existing),
                        "filename": existing.name,
                        "bytes": len(data),
                        "sha256": sha256,
                        "content_type": response.headers.get("content-type", ""),
                        "archive": _extract_mesh_archive(existing),
                        "cache_hit": True,
                        "deduplicated_by_sha256": True,
                    }
                    download_index[url] = result
                    index_path.write_text(json.dumps(download_index, indent=2, ensure_ascii=False), encoding="utf-8")
                    hook_state = getattr(state, "hook_state", None)
                    if isinstance(hook_state, dict) and existing.suffix.lower() in _MESH_USEFUL_EXTENSIONS:
                        preferred = (result.get("archive") or {}).get("preferred_geometry_file") or str(existing)
                        hook_state["scientific_mesh_pending_asset_evaluation"] = {
                            "path": preferred,
                            "downloaded_path": str(existing),
                            "url": str(response.url),
                            "sha256": sha256,
                            "extension": Path(str(preferred)).suffix.lower(),
                        }
                    emit_progress(state, "web_download_done", "success_cache_hit", bytes=len(data))
                    result["asset_profile"] = inspect_scientific_asset_path(existing)
                    result["asset_kind"] = result["asset_profile"].get("asset_kind") or asset_kind
                    remember_downloaded_scientific_asset(state, result)
                    return result
            except OSError:
                continue
        destination.write_bytes(data)
        archive_result = _extract_mesh_archive(destination)
        archive_assets = (
            _scientific_archive_assets(archive_result, asset_kind)
            if asset_kind in {"dataset", "parameters"}
            else []
        )
        preferred_asset_file = archive_assets[0]["path"] if len(archive_assets) == 1 else None
        result = {
            "status": "success",
            "url": str(response.url),
            "saved_path": str(destination),
            "filename": destination.name,
            "bytes": len(data),
            "sha256": sha256,
            "content_type": response.headers.get("content-type", ""),
            "archive": archive_result,
            "preferred_geometry_file": archive_result.get("preferred_geometry_file") or str(destination),
            "scientific_archive_assets": archive_assets,
            "preferred_asset_file": preferred_asset_file,
            "asset_kind": asset_kind,
            "asset_profile": inspect_scientific_asset_path(preferred_asset_file or destination),
            "usage_note": (
                "For Gmsh/OpenFOAM mesh generation, pass saved_path as geometry_file/cad_file/"
                "stl_file/step_file/geo_file/msh_file according to the downloaded format. "
                "Record url and sha256 in manifest/source_trace."
            ),
        }
        download_index[url] = result
        download_index[str(response.url)] = result
        index_path.write_text(json.dumps(download_index, indent=2, ensure_ascii=False), encoding="utf-8")
        hook_state = getattr(state, "hook_state", None)
        if isinstance(hook_state, dict) and final_ext in _MESH_USEFUL_EXTENSIONS:
            preferred = archive_result.get("preferred_geometry_file") or str(destination)
            hook_state["scientific_mesh_pending_asset_evaluation"] = {
                "path": preferred,
                "downloaded_path": str(destination),
                "url": str(response.url),
                "sha256": sha256,
                "extension": Path(str(preferred)).suffix.lower(),
            }
        remember_downloaded_scientific_asset(state, result)
        emit_progress(state, "web_download_done", "success", bytes=len(data), filename=destination.name)
        return result
