"""writing 重建 —— 各工具共用的底座：目录、体裁包、技艺文本、有界 LLM 调用。

原则（docs/WRITING_NODE_REBUILD_PLAN_20260918.md）：
- 模型只写 `paper/manuscript/sections/*.tex` 与几份小的声明文件；导言区、装配、
  编译、抽取、机械检查全归框架。
- 需要理解的事做成一次**有界**的 LLM 调用（起草一节、审读一遍），主循环的上下文
  只装收据，不装正文。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import yaml

from core.project_workspace import working_directory

NODE_DIR = Path(__file__).resolve().parents[1]
GENRES_DIR = NODE_DIR / "genres"
CRAFT_DIR = NODE_DIR / "craft"
RENDERERS_DIR = NODE_DIR / "renderers"

PROCESS_VOCAB: tuple[str, ...] = (
    "artifact", "上游", "revision item", "revision_items", "机械审计", "用户交来",
    "本平台", "not provided", "占位", "待补充", "投稿前需", "列入修订", "预检",
    "preflight", "receipt", "adequacy", "模板", "调度器", "orchestrator",
    "审计", "暂由", "留供", "供审", "生产状态", "待冻结", "还需冻结",
)


# ── 目录 ────────────────────────────────────────────────────────────────────

def paper_dir(state: Any) -> Path:
    """writing 节点的作用域目录（项目模式下是 <worktree>/paper/）。"""
    return working_directory(state)


def manuscript_dir(state: Any) -> Path:
    d = paper_dir(state) / "manuscript"
    (d / "sections").mkdir(parents=True, exist_ok=True)
    (d / "figures").mkdir(exist_ok=True)
    return d


def worktree_root(state: Any) -> Path | None:
    root = getattr(state, "project_worktree", None)
    return Path(root) if root else None


# ── 小文件 ──────────────────────────────────────────────────────────────────

def read_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clip(text: str, n: int, marker: str = "\n…[截断]") -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + marker


# ── 体裁包与技艺 ────────────────────────────────────────────────────────────

def list_genres() -> list[str]:
    return sorted(p.name for p in GENRES_DIR.iterdir() if (p / "structure.yaml").is_file())


def load_genre(genre_id: str) -> dict[str, Any]:
    d = GENRES_DIR / genre_id
    if not (d / "structure.yaml").is_file():
        raise ValueError(f"未知体裁 {genre_id!r}；可用：{', '.join(list_genres())}")
    structure = read_yaml(d / "structure.yaml") or {}
    return {
        "id": genre_id,
        "structure": structure,
        "reader": (d / "reader.md").read_text(encoding="utf-8") if (d / "reader.md").is_file() else "",
        "style": (d / "style.md").read_text(encoding="utf-8") if (d / "style.md").is_file() else "",
        "rubric": (d / "rubric.md").read_text(encoding="utf-8") if (d / "rubric.md").is_file() else "",
    }


def craft(name: str) -> str:
    p = CRAFT_DIR / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


PART_CRAFT = {
    "abstract": "abstract_title",
    "introduction": "introduction",
    "background": "introduction",
    "methods": "methods",
    "results": "results",
    "discussion": "discussion_conclusion",
    "conclusion": "discussion_conclusion",
}


def genre_brief_summary(genre: dict[str, Any], variant: str | None = None) -> str:
    """给主循环看的体裁摘要：部件、动作、预算。不给全文，全文在起草调用里用。"""
    s = genre["structure"]
    variants = s.get("variants") or {}
    v = variant if variant in variants else next(iter(variants), None)
    parts = variants.get(v, {}).get("parts") if v else s.get("required_parts")
    lines = [f"体裁 {genre['id']}（骨架变体 {v}）；部件顺序：{', '.join(parts or [])}"]
    for pid in parts or []:
        spec = (s.get("parts") or {}).get(pid) or {}
        moves = spec.get("moves") or []
        chars = spec.get("chars")
        lines.append(f"- {pid}（{spec.get('heading') or '无标题'}，{chars or '长度不限'} 字）：" + "；".join(m for m in moves[:5]))
    budget = s.get("budget") or {}
    if budget:
        lines.append(f"预算：正文 {budget.get('total_chars')} 字，图 ≤{budget.get('figures_max')}，表 ≤{budget.get('tables_max')}")
    refs = s.get("references") or {}
    if refs:
        lines.append(f"参考文献：≥{refs.get('min_count')} 篇，{refs.get('style')}，每篇被引用且可解析")
    return "\n".join(lines)


# ── 有界 LLM 调用 ───────────────────────────────────────────────────────────

async def bounded_llm(state: Any, *, phase: str, system: str, user: str,
                      max_tokens: int = 6000, temperature: float = 0.4) -> str:
    """一次不带工具、不进主循环上下文的调用；花费记进同一本账。"""
    from core import cost_ledger
    from core.llm import LLMClient, LLMMessage, opening_system_prompt

    client = LLMClient()
    messages = [opening_system_prompt(system), LLMMessage(role="user", content=user)]
    t0 = time.monotonic()
    resp = await client.chat(messages, tools=None, max_tokens=int(max_tokens),
                             temperature=float(temperature))
    elapsed = round(time.monotonic() - t0, 1)
    usage = getattr(resp, "usage", None) or {}
    try:
        cost_ledger.record(
            project_root=getattr(state, "project_root", None),
            run_id=getattr(state, "run_id", None),
            node_type="writing",
            provider=cost_ledger.provider_label(getattr(client, "base_url", None)),
            model=getattr(client, "model", None),
            usage=usage,
            extra={"bounded_phase": phase},
        )
    except Exception:  # noqa: BLE001 — 观测不打断主流程
        pass
    try:
        state.append_transcript(
            "writing_bounded_call", phase=phase, elapsed_s=elapsed,
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            system_chars=len(system), user_chars=len(user),
        )
    except Exception:  # noqa: BLE001
        pass
    content = (resp.content or "").strip()
    return strip_code_fence(content)


def figure_text_sample(stem: str, dirs: list[Path], limit: int = 220) -> str:
    """没有图表服务记录的图（示意图路径只交 svg/png/pdf）：从同名 svg 或 pdf 抽图内文字，当「这张图画了什么」的证据。"""
    import subprocess
    for d in dirs:
        if not d or not d.is_dir():
            continue
        svg = d / f"{stem}.svg"
        if svg.is_file():
            raw = svg.read_text(encoding="utf-8", errors="replace")
            words = [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r"<text[^>]*>(.*?)</text>", raw, re.S)]
            words = [w for w in words if w]
            if words:
                seen: list[str] = []
                for w in words:
                    if w not in seen:
                        seen.append(w)
                return clip(" | ".join(seen), limit, "…")
        pdf = d / f"{stem}.pdf"
        if pdf.is_file():
            try:
                txt = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, timeout=30).stdout
            except Exception:  # noqa: BLE001
                txt = ""
            words = [w for w in re.split(r"\s+", txt) if w]
            if words:
                seen = []
                for w in words:
                    if w not in seen:
                        seen.append(w)
                return clip(" | ".join(seen), limit, "…")
    return ""


def normalize_math_letters(text: str) -> tuple[str, int]:
    """把 Unicode 数学字母（U+1D400–U+1D7FF，如 𝑇 𝐴）换成普通字母；字体没有这些字，渲染成 U+FFFD。
    只碰这一段码位，不做整体 NFKC（那会把全角标点也改掉）。"""
    import unicodedata
    out: list[str] = []
    n = 0
    for ch in text:
        if 0x1D400 <= ord(ch) <= 0x1D7FF:
            base = unicodedata.normalize("NFKC", ch)
            out.append(base if base and base != ch else "?")
            n += 1
        else:
            out.append(ch)
    return "".join(out), n


def strip_code_fence(text: str) -> str:
    """模型常把 LaTeX 包在 ```latex … ``` 里；剥掉外层围栏。"""
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", text.strip(), re.S)
    return m.group(1).strip() if m else text.strip()


def extract_json(text: str) -> Any:
    """从模型回复里取出第一个 JSON 对象（容忍围栏与前后文字）。"""
    t = strip_code_fence(text)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None
    return None


# ── LaTeX 片段的机械检查 ────────────────────────────────────────────────────

FORBIDDEN_IN_FRAGMENT = (
    r"\\documentclass", r"\\usepackage", r"\\begin\{document\}", r"\\end\{document\}",
    r"\\maketitle", r"\\title\{", r"\\author\{", r"\\printbibliography", r"\\bibliography\{",
    r"\\section\*?\{参考文献\}", r"\\begin\{abstract\}",
)


def fragment_problems(tex: str) -> list[str]:
    problems = []
    for pat in FORBIDDEN_IN_FRAGMENT:
        if re.search(pat, tex):
            problems.append(f"片段里不该出现 {pat}")
    for kw in PROCESS_VOCAB:
        if kw.lower() in tex.lower():
            problems.append(f"正文出现过程词汇「{kw}」")
    return problems


def cite_keys(tex: str) -> list[str]:
    keys: list[str] = []
    for m in re.finditer(r"\\(?:cite|citep|citet|supercite|parencite|textcite)\{([^}]*)\}", tex):
        keys.extend(k.strip() for k in m.group(1).split(",") if k.strip())
    return keys


def graphics_files(tex: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", tex)]


def bib_keys(bib_text: str) -> dict[str, str]:
    """key → 题名（粗解析）。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@|\Z)", bib_text, re.S):
        key = m.group(1)
        body = m.group(2)
        t = re.search(r"title\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", body, re.S | re.I)
        out[key] = re.sub(r"\s+", " ", t.group(1)).strip("{} ") if t else ""
    return out
