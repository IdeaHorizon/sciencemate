"""Generate the static HTML docs site at docs-html/.

Self-contained generator. Reads:
  - existing markdown docs in docs/
  - inline content blocks below
  - SVG diagrams from docs-html/assets/diagrams/
  - live introspection of framework (tools list, harness yamls)

Outputs:
  - docs-html/<page>.html × 10 pages
  - All share a common header + sidebar + footer template
  - Internal links + per-page TOC

Run:
  python scripts/build_docs_html.py
  # then open docs-html/index.html
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

import markdown  # python-markdown
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DOCS = ROOT / "docs"
OUT = ROOT / "docs-html"
ASSETS = OUT / "assets"


# ── Sidebar nav structure ──────────────────────────────────────────────

NAV = [
    ("开始", [
        ("index.html", "Home —— 这套系统在做什么"),
        ("quick-start.html", "Quick Start（15 分钟跑通）"),
    ]),
    ("设计总纲", [
        ("design-principles.html", "五条判据（全仓通用）"),
        ("architecture.html", "架构总览"),
    ]),
    ("研究循环", [
        ("research-loop.html", "研究循环：节点全景 + verdict 驱动"),
        ("evidence-modalities.html", "证据的三种模态（干预 / 检视 / 演绎）"),
        ("node-lifecycle.html", "节点生命周期 + post-run flow"),
    ]),
    ("承诺与证据", [
        ("commitment-and-evidence.html", "预注册 · 冻结账本 · 闭合账本"),
        ("artifact-model.html", "Artifact 三原语 + 类型能力注册表"),
    ]),
    ("知识与记忆", [
        ("kb-system.html", "KB 两层（project 工作记忆 / org 机构资产）"),
        ("memory-system.html", "Memory 四层（宪法·局面·手册·日志）"),
    ]),
    ("人在回路", [
        ("human-in-the-loop.html", "介入通道 · 决策呈递 · 停止 · propose gate"),
    ]),
    ("运行时", [
        ("runtime.html", "脊柱 · 送达 · 韧性 · 故障归属 · 写边界"),
        ("context-hooks.html", "Context 引擎 + Hooks 解剖"),
    ]),
    ("Owner 手册", [
        ("owner-guide.html", "接手一个节点"),
        ("node-iteration-guide.html", "节点迭代 cookbook + fixture 测试"),
        ("dev-workflow.html", "Dev Workflow（hf CLI / sandbox / cache）"),
    ]),
    ("目录 · 自动生成", [
        ("nodes.html", "节点目录"),
        ("tools.html", "Tools 全清单"),
        ("skills.html", "Skills 全清单"),
        ("tool-spec.html", "Tool 接口规范"),
        ("skill-spec.html", "Skill 规范"),
    ]),
    ("决策档案", [
        ("decision-archive.html", "RFC 索引 + 事故账本"),
    ]),
]


#: 节点的**显示身份**。判身份看 harness 自述与调用链，不看目录名。
#: `nodes/hypothesis/` 的 harness.yaml 第一句就是「你是 **Analysis**」，
#: `core/obligations.py` 也写着 `_ANALYSIS_NODE = "hypothesis"` —— 目录名与
#: node_type 还没跟上而已（改名是独立的一条线，会碰 48 处按名寻址 + 活项目
#: 工作区目录）。站上只显示一个身份，旧名生成跳转页，站外链接不断。
NODE_DISPLAY_NAME = {
    "hypothesis": "analysis",
}

#: 旧名 → 新名的跳转页。删掉一个节点后，它的旧页也从这里退场（见 main() 的扫盘）。
NODE_ALIAS_REDIRECTS = {
    "hypothesis": "analysis",
}


def display_node_name(node_type: str) -> str:
    return NODE_DISPLAY_NAME.get(node_type, node_type)


def sidebar_html(active: str, rel: str = "") -> str:
    parts = ['<div class="sidebar" id="sidebar">']
    for section, links in NAV:
        parts.append(f'<h3>{section}</h3><ul>')
        for href, label in links:
            cls = ' class="active"' if href == active else ''
            parts.append(f'<li><a href="{rel}{href}"{cls}>{html.escape(label)}</a></li>')
        parts.append('</ul>')
    parts.append('</div>')
    return "\n".join(parts)


# ── Common shell template ─────────────────────────────────────────────

def render_page(*, active: str, title: str, body_html: str,
                  toc_html: str = "", extra_head: str = "",
                  depth: int = 0) -> str:
    """Render a full page.

    depth=0 → root page (assets/style.css)
    depth=1 → subdir page (../assets/style.css), nav links go ../<page>
    """
    rel = "../" * depth
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — harness-framework</title>
<link rel="stylesheet" href="{rel}assets/style.css">
{extra_head}
</head>
<body>
<header class="topbar">
  <button class="menu-toggle" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
  <a href="{rel}index.html" class="brand"><span class="logo">⚓</span> harness-framework</a>
  <span class="ver">harness-framework</span>
  <nav>
    <a href="{rel}index.html">Home</a>
    <a href="{rel}architecture.html">Architecture</a>
    <a href="{rel}owner-guide.html">Owner Guide</a>
    <a href="{rel}tools.html">Tools</a>
    <a href="https://github.com" target="_blank">Repo ↗</a>
  </nav>
</header>
<div class="layout">
{sidebar_html(active, rel=rel)}
<main class="content">
{body_html}
</main>
<aside class="toc">
{toc_html}
</aside>
</div>
<footer class="footer">
  harness-framework docs · 内容由 <code>scripts/build_docs_html.py</code> 生成 ·
  <a href="{rel}../docs/">原始 markdown 在 docs/</a>
</footer>
</body>
</html>
"""


def md_to_html(md_text: str) -> tuple[str, str]:
    """Render markdown → (body_html, toc_html). Heading slugs are auto."""
    md = markdown.Markdown(
        extensions=[
            "extra",            # tables / fenced_code / footnotes / etc.
            "toc",
            "sane_lists",
            "admonition",
            "codehilite",
        ],
        extension_configs={
            "toc": {"toc_depth": "2-3", "anchorlink": True},
            "codehilite": {"guess_lang": False, "noclasses": False},
        },
    )
    body = md.convert(md_text)
    toc = md.toc
    return body, f"<h4>本页目录</h4>\n{toc}" if toc.strip() else ""


# ── Inline diagram helper ─────────────────────────────────────────────

def diagram(svg_name: str, caption: str = "") -> str:
    path = ASSETS / "diagrams" / svg_name
    svg = path.read_text(encoding="utf-8") if path.exists() else f"<p>(diagram {svg_name} not found)</p>"
    cap = f'<div class="diagram-caption">{html.escape(caption)}</div>' if caption else ""
    return f'<div class="diagram">{svg}{cap}</div>'


# ── Live data introspection ───────────────────────────────────────────

def introspect_tools_and_nodes() -> dict:
    """Bootstrap framework and pull live tool/skill/node info."""
    from core.bootstrap import bootstrap
    bootstrap()
    import inspect
    from core.tool_registry import _REGISTRY
    from core.skill_registry import _SKILLS
    from core.loader import load_harness

    tools_by_module: dict[str, list[dict]] = {}
    tools_index: dict[str, dict] = {}      # name → full tool info

    # 站的内容必须是**仓库**的函数，不是 build 这台机器的函数。
    #
    # 能力闸控的工具（check_lean 要 Lean 工具链、interval_check 要 python-flint）
    # 在探不到能力的机器上不进 _REGISTRY.executors —— 那是运行时该有的样子
    # （[[能力缺席就不给工具]]），但照搬到文档站上就变成：谁来 build 决定了站上
    # 有哪些页。实测三台机器三个站（331 / 330 / 329 页），而每个工具页还列同侪
    # 工具，所以少一个工具改掉上百页 —— 结果是谁都重建不出提交的那份。
    #
    # 所以这里读**目录**（register_capability_gated_tool 登记的），不可用的照样
    # 出页，页上标明"需要 X"。
    gated = getattr(_REGISTRY, "capability_gated", {})
    executors = dict(_REGISTRY.executors)
    for gname, gentry in gated.items():
        executors.setdefault(gname, gentry["executor"])

    for name, ex in executors.items():
        try:
            mod = inspect.getmodule(ex)
            modname = mod.__name__ if mod else "?"
            source_file = inspect.getsourcefile(ex) or ""
            try:
                source_lines, source_lineno = inspect.getsourcelines(ex)
                source_code = "".join(source_lines)
            except (OSError, TypeError):
                source_code = ""
                source_lineno = 0
        except Exception:
            modname = "?"
            source_file = ""
            source_code = ""
            source_lineno = 0
        modname_short = modname.replace("shared.tools.library.", "lib/")\
                                .replace("shared.tools.", "")
        t = _REGISTRY.tools.get(name) or gated[name]["definition"]
        desc = t.description.replace("\n", " ").strip()
        for stop in ["。", ". "]:
            i = desc.find(stop)
            if 0 < i < 110:
                desc = desc[: i + 1]
                break
        # Relative source path for display
        try:
            src_rel = str(Path(source_file).relative_to(ROOT)) if source_file else ""
        except (ValueError, TypeError):
            src_rel = source_file
        info = {
            "name": name,
            "description": desc[:200],
            "risk_level": t.risk_level,
            "full_description": t.description,
            "parameters": t.parameters_schema,
            "module": modname_short,
            "source_file": src_rel,
            "source_lineno": source_lineno,
            "source_code": source_code,
            "executor_name": ex.__name__ if hasattr(ex, "__name__") else "(closure)",
            "allowed_node_types": t.allowed_node_types,
            # 这个工具需要本机装什么外部能力；None = 无条件可用。
            # 注意只取 capability，**不取 available** —— available 是"这台机器
            # 现在有没有"，把它写进站里，站就又变成机器的函数了。
            "requires_capability": (gated.get(name) or {}).get("capability"),
        }
        tools_by_module.setdefault(modname_short, []).append(info)
        tools_index[name] = info

    nodes = []
    for hp in sorted((ROOT / "nodes").glob("*/harness.yaml")):
        n = hp.parent.name
        h = load_harness(n)
        with open(hp) as f:
            raw = yaml.safe_load(f) or {}
        sp = (raw.get("system_prompt") or "").strip()
        first_para = sp.split("\n\n")[0].split("\n")[0][:200]
        flow = raw.get("post_run_flow") or "full"
        if n.startswith("_"):
            role = "架构"
        elif flow == "none":
            role = "服务"
        else:
            role = "producing"
        nodes.append({
            "name": n,
            "flow": flow,
            "role": role,
            "version": h.version,
            "risk_level": h.risk_level,
            "role_line": first_para,
            "tools": sorted(h.tools),
            "skills": list(h.skills),
            "required_input": h.required_input_artifact_types,
            "required_output": h.required_outputs,
            "expected_inputs": (raw.get("expected_inputs") or {}),
            "expected_outputs": (raw.get("expected_outputs") or {}),
            "callable_nodes": h.callable_nodes,
            "max_turns": h.max_turns,
            "system_prompt": sp,
            "rules": list(h.rules),
            "guidelines": list(h.guidelines),
        })

    skills = []
    for name, s in sorted(_SKILLS.items()):
        # which nodes reference this skill
        used_by = [n["name"] for n in nodes if name in n["skills"]]
        skills.append({
            "name": s.name,
            "description": s.description,
            "applies_when": s.applies_when,
            "tools_used": s.tools_used,
            "expected_outcome": s.expected_outcome,
            "status": s.status,
            "origin": s.origin,
            "body_markdown": s.body_markdown,
            "source_dir": str(s.source_dir).replace(str(ROOT) + "/", "") if s.source_dir else "",
            "used_by_nodes": used_by,
        })

    # Cross-reference: per tool, which nodes use it
    for tname, tinfo in tools_index.items():
        tinfo["used_by_nodes"] = [n["name"] for n in nodes if tname in n["tools"]]

    # Built-in hooks introspection
    from core.loop_hooks import _HOOKS as HOOK_REGISTRY
    hooks = []
    for hname, h in sorted(HOOK_REGISTRY.items()):
        hook_info = {"name": h.name, "description": h.description, "callbacks": {}}
        for phase_name in ("on_turn_start", "on_llm_response", "on_turn_end", "on_end"):
            cb = getattr(h, phase_name, None)
            if cb is None:
                continue
            try:
                src_lines, lineno = inspect.getsourcelines(cb)
                src = "".join(src_lines)
                src_file = inspect.getsourcefile(cb) or ""
                try:
                    src_rel = str(Path(src_file).relative_to(ROOT)) if src_file else ""
                except (ValueError, TypeError):
                    src_rel = src_file
            except (OSError, TypeError):
                src = ""
                lineno = 0
                src_rel = ""
            hook_info["callbacks"][phase_name] = {
                "source_code": src,
                "source_file": src_rel,
                "source_lineno": lineno,
                "func_name": cb.__name__ if hasattr(cb, "__name__") else "(closure)",
            }
        hooks.append(hook_info)

    return {
        "tools": tools_by_module,
        "tools_index": tools_index,
        "nodes": nodes,
        "skills": skills,
        "hooks": hooks,
    }


# ── Page builders ─────────────────────────────────────────────────────

#: 决策档案的命名约定 —— 大写前缀的 md 自动进档案（扫盘，不写名单）。
_ARCHIVE_PREFIXES = ("RFC_", "PROPOSAL_", "KB_", "DEADLOCK_", "UI_",
                     "V21_", "ORCHESTRATOR_", "DOCS_")

#: 不按前缀命名、但性质是历史记录 / 工作笔记的几份。
#: 判据：它记录的是**某一刻的判断或现场**，不随实现更新。
_ARCHIVE_EXTRA = {
    "v21-collaboration-model",
    "qc-dimensions",
    "compression-fate-table",
    "e2e-failure-analysis-20260807",
    "maintainer-playbook",
    "platform-runtime",
    "ui-deployment",
}


def _is_archive_doc(stem: str) -> bool:
    return stem.startswith(_ARCHIVE_PREFIXES) or stem in _ARCHIVE_EXTRA


def tool_ref(name: str, data: dict, rel: str = "", *, style: str = "") -> str:
    """渲染一个工具引用。

    ⚠️ 工具**不存在**时不出链接，改出一个显式警告标记 —— 不是静默降级。
    skill 的 `tools_used` 会被 `core/skill_registry` 渲进**送给模型的正文**
    （`**涉及工具：** ...`），所以一个幽灵名字不只是死链，是在教模型去调一个
    不存在的工具。站上把它标红，是让这个缺陷有人看得见。
    """
    code = f'<code{(" style=" + chr(34) + style + chr(34)) if style else ""}>{html.escape(name)}</code>'
    if name in data["tools_index"]:
        return f'<a href="{rel}tools/{html.escape(name)}.html">{code}</a>'
    return (f'{code}<span class="badge high" title="这个工具在注册表里不存在'
            f'——skill 正文会把它送给模型" style="font-size:10px;margin-left:3px">⚠ 不存在</span>')


def _archive_docs() -> list[tuple[str, str]]:
    """决策档案里要出原文页的 md：(stem, 标题)。

    扫盘得来，不写名单 —— 新加一份 RFC_*.md 就自动上站。
    """
    out = []
    for md in sorted(DOCS.glob("*.md")):
        if _is_archive_doc(md.stem):
            first = md.read_text(encoding="utf-8").lstrip().split("\n", 1)[0]
            title = first.lstrip("# ").strip() or md.stem
            out.append((md.stem, title[:90]))
    return out


#: 档案条目的落地状态。**人维护这一行**，好过在正文里写状态 —— 正文里的状态
#: 没人会回来改，而「提案」被读成「现状」是这份档案最贵的失败模式。
ARCHIVE_STATUS: dict[str, tuple[str, str]] = {
    "RFC_ARTIFACT_IDENTITY_AND_VERSIONING_20260818": ("已实施", "PR#500"),
    "RFC_KB_TWO_TIERS_20260820": ("已实施", "PR#568 / #569"),
    "RFC_MEMORY_REBUILD_20260821": ("已实施", "PR#572"),
    "RFC_RUNTIME_RESILIENCE_20260818": ("部分实施", "第 0 步 PR#571；D10/D12 PR#573；Track 1 已否决"),
    "RFC_RUNTIME_PIPELINE_20260819": ("待拍板", ""),
    "RFC_ARTIFACT_COMMITMENT_LAYER_20260820": ("部分实施", "PR#567：承诺归预注册"),
    "PROPOSAL_RETIRE_QC_AS_A_VERDICT_LAYER_20260810": ("已实施", "PR#381：QC 降级成检测"),
    "ORCHESTRATOR_SITUATION_ROUTER_20260817": ("已实施", "PR#456 / #529"),
    "KB_SYSTEM_STATE_20260821": ("现状快照", ""),
    "KB_CLOSURE_PROOF_20260821": ("实验记录", "复利闭环首证"),
    "KB_INJECTION_AB_PREREG_20260821": ("预注册", ""),
    "KB_INJECTION_AB_RESULT_20260821": ("实验结果", "预注册判据判为不支持"),
    "KB_INJECTION_AB_PLATFORM_20260821": ("实验记录", ""),
    "DEADLOCK_INCIDENT_LEDGER": ("事故账本", ""),
    "UI_PATH_GAP_AUDIT": ("审计记录", ""),
    "V21_NODE_OWNER_HANDOFF": ("交接文档", ""),
    "DOCS_SITE_REBUILD_PLAN_20260821": ("已实施", "本次文档站重建"),
    "v21-collaboration-model": ("历史论述", "v2.0 → v2.1 迁移对照"),
    "qc-dimensions": ("设计笔记", "四维聚合阶段 2/3 是提案"),
    "compression-fate-table": ("参考表", "改 summarizer 前必读"),
    "e2e-failure-analysis-20260807": ("事故分析", ""),
    "maintainer-playbook": ("维护手册", ""),
    "platform-runtime": ("平台侧", "harness 站只作参考"),
    "ui-deployment": ("平台侧", "harness 站只作参考"),
}

_STATUS_COLOR = {
    "已实施": "low", "现状快照": "low", "实验结果": "low", "实验记录": "low",
    "部分实施": "medium", "预注册": "medium", "交接文档": "medium",
    "审计记录": "medium", "事故账本": "medium", "历史论述": "medium",
    "设计笔记": "medium", "参考表": "medium", "事故分析": "medium",
    "维护手册": "medium", "平台侧": "medium",
    "待拍板": "high", "已否决": "high", "未标注": "high",
}


def page_decision_archive(data: dict) -> str:
    """决策档案 —— 设计逻辑住在这里，不在提交信息里。"""
    groups: dict[str, list] = {"RFC": [], "提案与参考": [], "实验与事故": []}
    for stem, title in _archive_docs():
        status, note = ARCHIVE_STATUS.get(stem, ("未标注", ""))
        if stem.startswith("RFC_"):
            g = "RFC"
        elif stem.startswith(("PROPOSAL_", "UI_", "ORCHESTRATOR_", "V21_", "DOCS_")) \
                or stem in ("v21-collaboration-model", "qc-dimensions",
                            "maintainer-playbook", "platform-runtime",
                            "ui-deployment", "compression-fate-table"):
            g = "提案与参考"
        else:
            g = "实验与事故"
        groups[g].append((stem, title, status, note))

    parts = [
        "<h1>决策档案</h1>",
        '<p class="muted">这套系统的<strong>设计逻辑</strong>住在这里。'
        "上面每一页讲的是「现在是什么样」，这里讲的是「为什么是这样、"
        "当初的替代方案是什么、哪一条被否决了」。</p>",
        '<div class="callout"><strong>读之前先看状态。</strong>'
        "档案里同时有<strong>已实施</strong>的设计与<strong>待拍板</strong>的提案，"
        "措辞都一样笃定。把提案读成现状是这份档案最贵的失败模式，所以每篇顶上"
        "机械插了一条状态横幅 —— 状态由生成器的 <code>ARCHIVE_STATUS</code> 维护，"
        "不写在正文里（正文里的状态没人会回来改）。</div>",
    ]
    for gname, items in groups.items():
        if not items:
            continue
        parts.append('<h2 id="archive-%s">%s</h2><table>' % (len(parts), gname))
        parts.append("<tr><th>文档</th><th>状态</th><th>落地</th></tr>")
        for stem, title, status, note in items:
            cls = _STATUS_COLOR.get(status, "medium")
            parts.append(
                '<tr><td><a href="rfc/%s.html">%s</a><br>'
                '<span class="muted" style="font-size:12px"><code>docs/%s.md</code></span></td>'
                '<td><span class="badge %s">%s</span></td>'
                '<td class="muted" style="font-size:13px">%s</td></tr>'
                % (stem, html.escape(title), stem, cls, status, html.escape(note))
            )
        parts.append("</table>")
    return "\n".join(parts)


def page_archive_doc(stem: str, title: str) -> str:
    """档案原文页 —— 顶部机械插状态横幅。"""
    status, note = ARCHIVE_STATUS.get(stem, ("未标注", ""))
    cls = _STATUS_COLOR.get(status, "medium")
    note_html = ("　·　落地：" + html.escape(note)) if note else ""
    banner = (
        '<div class="callout"><span class="badge %s">%s</span>%s<br>'
        '<span class="muted">这是一份<strong>决策档案</strong>，'
        "记录写作当时的判断，不随实现更新。现状以左侧对应的机制页为准；"
        '返回 <a href="../decision-archive.html">决策档案索引</a>。</span></div>'
        % (cls, status, note_html)
    )
    return banner + _md_only_page(stem + ".md")


def page_research_loop(data: dict) -> str:
    """研究循环 —— 论述来自 md，节点表从 harness 现算接在后面。"""
    body = _md_only_page("research-loop.md")
    rows = ""
    for n in sorted(data["nodes"], key=lambda x: (x["role"], display_node_name(x["name"]))):
        disp = display_node_name(n["name"])
        outs = ", ".join("<code>%s</code>" % o for o in n["required_output"]) or "—"
        rows += ('<tr><td><a href="nodes/%s.html">%s</a></td><td>%s</td>'
                 "<td><code>%s</code></td><td>%s</td></tr>"
                 % (disp, disp, n["role"], n["flow"], outs))
    return body + (
        '<h2 id="xian-suan-biao">现算对照表</h2>'
        '<p class="muted">下表由 <code>nodes/*/harness.yaml</code> 现算 —— '
        "正文若与它冲突，以它为准。</p><table>"
        "<tr><th>节点</th><th>角色</th><th>post_run_flow</th><th>required_output</th></tr>"
        "%s</table>" % rows
    )


def page_index(data: dict) -> str:
    """Home。

    ⚠️ 这一页**不许出现手写的计数或分档**。2026-08-21 站上写着
    「Tools 172 / Skills 44 / Nodes 9（3 framework + 7 producing）」，
    而现算是 171 / 49 / 10（4 producing / 3 服务 / 3 架构）—— 括号里那个
    手写切分连重建都躲得过，因为它不是数据。
    """
    n_tools = sum(len(v) for v in data["tools"].values())
    n_skills = len(data["skills"])
    n_hooks = len(data["hooks"])
    nodes = data["nodes"]
    by_role: dict[str, list[str]] = {}
    for n in nodes:
        by_role.setdefault(n["role"], []).append(display_node_name(n["name"]))
    role_line = " · ".join(
        "%d %s（%s）" % (len(v), k, "/".join(sorted(v)))
        for k, v in sorted(by_role.items())
    )
    n_archive = len(_archive_docs())

    return """
<h1>harness-framework</h1>
<p class="muted" style="font-size:1.15em">给科研的每一步配一个 agent，
并把「这个结论可不可信」做成<strong>机械可查的闸</strong>，而不是 prompt 里的一句「必须」。</p>

<div class="callout">
<strong>只读一页的话，读这一页。</strong>
这套系统真正的主张只有一条：<em>科研可信度的绝大部分，可以从「要求模型自觉」
搬到「让不合格的状态根本表示不出来」。</em>
预注册冻结成哈希链、勾账的键必须逐字对上、证据记录缺字段就写不出文件、
做实验的人不许裁决自己的实验 —— 这些都不是流程规范，是代码里的闸。
展开见 <a href="design-principles.html">五条判据</a>。
</div>

<h2 id="cong-na-li-kai-shi">从哪里开始</h2>
<div class="cards">
  <div class="card" style="border-color: var(--color-accent); border-width: 2px">
    <h3><a href="design-principles.html">① 五条判据</a></h3>
    <p>全仓通用的设计判断，每一条都是拿事故换来的。读完再看别的页，
    你会发现机制长成那样都是有原因的。</p>
  </div>
  <div class="card" style="border-color: var(--color-accent); border-width: 2px">
    <h3><a href="research-loop.html">② 研究循环</a></h3>
    <p>%d 个节点怎么协作。<strong>不是管线，是 verdict 驱动的循环</strong>；
    取证有<a href="evidence-modalities.html">三种模态</a>，干预式、检视式与演绎式。</p>
  </div>
  <div class="card" style="border-color: var(--color-accent); border-width: 2px">
    <h3><a href="commitment-and-evidence.html">③ 承诺与证据</a></h3>
    <p>平台的科学内核：预注册怎么冻、闭合条件怎么勾账、
    为什么「勾账的键写错一个前缀」会让 12 条兑现一条都不算数。</p>
  </div>
</div>

<h2 id="xian-suan-pan-mian">现算盘面</h2>
<p class="muted">下表每个数字都是 build 时从代码现算的，没有一个是手写的。</p>
<table style="margin-bottom:1.5em">
<tr><td><strong>节点</strong></td><td>%d 个 —— %s</td></tr>
<tr><td><strong>工具</strong></td><td><a href="tools.html">%d 个</a>（框架内置 + 节点专属）</td></tr>
<tr><td><strong>Skills</strong></td><td><a href="skills.html">%d 个</a></td></tr>
<tr><td><strong>Loop hooks</strong></td><td>%d 个（见 <a href="context-hooks.html">Context &amp; Hooks</a>）</td></tr>
<tr><td><strong>决策档案</strong></td><td><a href="decision-archive.html">%d 份</a> RFC / 提案 / 事故账本</td></tr>
</table>

<h2 id="zhe-tao-dong-xi-de-xing-zhuang">这套东西的形状</h2>
<table>
<tr><th>层</th><th>装什么</th><th>细读</th></tr>
<tr><td>研究循环</td><td>节点、派发、裁决权边界、post-run flow</td><td><a href="research-loop.html">研究循环</a> · <a href="node-lifecycle.html">生命周期</a></td></tr>
<tr><td>承诺与证据</td><td>预注册、冻结账本、闭合账本、产物身份与版本</td><td><a href="commitment-and-evidence.html">承诺与证据</a> · <a href="artifact-model.html">Artifact 模型</a></td></tr>
<tr><td>知识与记忆</td><td>KB 两层（世界断言）、Memory 四层（怎么工作）</td><td><a href="kb-system.html">KB</a> · <a href="memory-system.html">Memory</a></td></tr>
<tr><td>人在回路</td><td>决策呈递、停止、阻塞上报、propose gate</td><td><a href="human-in-the-loop.html">人在回路</a></td></tr>
<tr><td>运行时</td><td>会话脊柱、送达通道、韧性、故障归属、写边界</td><td><a href="runtime.html">运行时</a></td></tr>
<tr><td>你的自留地</td><td>harness.yaml / 节点工具 / skills / hooks</td><td><a href="owner-guide.html">Owner 手册</a></td></tr>
</table>

<h2 id="wu-fen-zhong-qi-dong">五分钟启动</h2>
<pre><code>git clone &lt;repo&gt; &amp;&amp; cd harness-framework
cp .env.example .env             # 填 LLM_API_KEY
pip install -e .
python -m pytest tests/ -q       # 应全绿

# 跑你自己那个节点
python run_node.py --harness &lt;my_node&gt; --sandbox --fixture nodes/&lt;my_node&gt;/fixtures/minimal.yaml
</code></pre>
<p>完整版见 <a href="quick-start.html">Quick Start</a>；接手一个节点见
<a href="owner-guide.html">Owner 手册</a>。</p>
""" % (len(nodes), len(nodes), role_line, n_tools, n_skills, n_hooks, n_archive)


def page_architecture(data: dict) -> str:
    return _md_only_page("architecture.md")


def page_owner_guide(data: dict) -> str:
    return _md_only_page("owner-guide.md")


def page_nodes(data: dict) -> str:
    """节点目录 —— 全部从 harness.yaml 现算，没有一个数字是手写的。"""
    nodes = data["nodes"]
    by_flow: dict[str, list[dict]] = {}
    for n in nodes:
        by_flow.setdefault(n["flow"], []).append(n)

    rows = ""
    for n in sorted(nodes, key=lambda x: (x["role"], display_node_name(x["name"]))):
        disp = display_node_name(n["name"])
        alias = "" if disp == n["name"] else f' <span class="muted" style="font-size:12px">（node_type: <code>{n["name"]}</code>）</span>'
        callable_str = ", ".join(f"<code>{display_node_name(c)}</code>" for c in n["callable_nodes"]) or "—"
        rows += (
            f'<tr><td><a href="nodes/{disp}.html"><strong>{disp}</strong></a>{alias}</td>'
            f'<td>{n["role"]}</td><td><code>{n["flow"]}</code></td>'
            f'<td>{len(n["tools"])}</td><td>{len(n["skills"])}</td>'
            f'<td>{callable_str}</td>'
            f'<td><span class="badge {n["risk_level"]}">{n["risk_level"]}</span></td></tr>'
        )

    cards = ""
    for n in sorted(nodes, key=lambda x: (x["role"], display_node_name(x["name"]))):
        disp = display_node_name(n["name"])
        cards += f'''
<div class="card">
  <h3><a href="nodes/{disp}.html">{disp}</a> <span class="badge {n["risk_level"]}">{n["risk_level"]}</span></h3>
  <p>{html.escape(n["role_line"])}</p>
  <p class="muted" style="font-size:12px">{n["role"]} · flow=<code>{n["flow"]}</code> · {len(n["tools"])} 工具 · v{n["version"]}</p>
</div>'''

    return f'''
<h1>节点目录</h1>
<p class="muted">本页整页由 <code>nodes/*/harness.yaml</code> 现算生成 —— 计数、分档、
可调子节点都不是手写的。设计论述见
<a href="research-loop.html">研究循环</a> 与
<a href="evidence-modalities.html">证据模态</a>。</p>

<p><strong>显示名以 harness 自述为准，不按目录名。</strong>
<code>nodes/hypothesis/</code> 的 harness 第一句是「你是 <strong>Analysis</strong>」，
所以站上叫 analysis；<code>node_type</code> 与目录名的改名是独立的一条线。</p>

<h2 id="quan-jing">全景（{len(nodes)} 个）</h2>
<table>
<tr><th>节点</th><th>角色</th><th>post_run_flow</th><th>工具</th><th>skills</th><th>可调子节点</th><th>risk</th></tr>
{rows}
</table>

<h2 id="ka-pian">卡片</h2>
<div class="cards">{cards}</div>
'''


def page_tools(data: dict) -> str:
    tools_by_mod = data["tools"]
    n_total = sum(len(v) for v in tools_by_mod.values())

    # group display order
    group_meta = {
        "builtin": ("框架内置（默认全节点可用）", "shared/tools/builtin.py"),
        "run_node": ("子节点调度", "shared/tools/run_node.py"),
        "papers": ("文献搜索", "shared/tools/papers.py"),
        "lib/kb": ("KB v3 CRUD（concepts/claims/experiments/chunks + 10 claim_type）",
                    "shared/tools/library/kb.py"),
        "lib/audit": ("Curator audit log + revert", "shared/tools/library/audit.py"),
        "lib/proposals": ("Proposal 统一 inbox", "shared/tools/library/proposals.py"),
        "lib/runtime_control": ("inject / cancel child node",
                                  "shared/tools/library/runtime_control.py"),
        "lib/disagreement_scan": ("artifact 间分歧扫描",
                                    "shared/tools/library/disagreement_scan.py"),
        "lib/profile_tools": ("PROFILE / PROJECT", "shared/tools/library/profile_tools.py"),
        "lib/skill_tools": ("Skill 系统维护", "shared/tools/library/skill_tools.py"),
        "lib/artifacts_extra": ("Artifact freeze", "shared/tools/library/artifacts_extra.py"),
        "lib/python_exec": ("Python subprocess", "shared/tools/library/python_exec.py"),
        "lib/latex": ("LaTeX 编译", "shared/tools/library/latex.py"),
    }

    body = f"""
<h1>Tools 全清单</h1>
<p class="muted">共 <strong>{n_total} 个工具</strong>跨 <strong>{len(tools_by_mod)} 个文件</strong>。每节点 yaml <code>tools:</code> 白名单从这里选。</p>

<div class="callout info">
  <strong>规则</strong>
  默认全节点可用的：<code>builtin</code> + <code>run_node</code> + <code>papers</code>（被 <code>shared/tools/__init__.py</code> 默认 import）。
  其它 opt-in：节点 <code>tools/__init__.py</code> 显式 import 才注册。详见 <a href="tool-spec.html">tool-spec.html</a>。
</div>
"""

    for modkey, tools in tools_by_mod.items():
        label, path = group_meta.get(modkey, (modkey, modkey))
        body += f'<h2>{label} <span class="muted" style="font-size:14px">· <code>{path}</code> · {len(tools)} 个</span></h2>'
        body += '<table><tr><th style="width:25%">工具（点击看详情 + 源码）</th><th>说明</th><th style="width:8%">风险</th></tr>'
        for t in sorted(tools, key=lambda x: x["name"]):
            body += f'<tr><td><a href="tools/{t["name"]}.html"><code>{t["name"]}</code></a></td><td>{html.escape(t["description"])}</td><td><span class="badge {t["risk_level"]}">{t["risk_level"]}</span></td></tr>'
        body += "</table>"

    # Per-node whitelist summary
    body += '<h2>每节点工具白名单（点工具看详情）</h2>'
    body += '<table><tr><th>节点</th><th>数</th><th>工具</th></tr>'
    for n in data["nodes"]:
        tools_inline = "".join(
            tool_ref(t, data, style="display:inline-block;margin:2px;padding:1px 5px;background:var(--color-bg-elevated);border-radius:3px;font-size:11px")
            for t in n["tools"]
        )
        body += f'<tr><td><a href="nodes/{n["name"]}.html"><code>{n["name"]}</code></a></td><td>{len(n["tools"])}</td><td>{tools_inline}</td></tr>'
    body += "</table>"

    # Owner action space — dev-time vs runtime are TWO ORTHOGONAL AXES
    body += "<h2>Dev-time —— Node Owner 编辑空间（2 档）</h2>"
    body += diagram("ownership.svg", "17 项自留地 vs 9 类框架契约")
    body += """
<p>详细规范见 <a href="owner-guide.html">owner-guide.html</a>。</p>
<table>
<tr><th>类别</th><th>数量</th><th>谁能改</th></tr>
<tr><td>自留地操作位置（每节点）</td><td>17</td><td>✅ owner 全权</td></tr>
<tr><td>框架契约（架构组管）</td><td>9 类</td><td>❌ owner 不动</td></tr>
</table>

<h2>Runtime —— Agent 改持久状态的路径</h2>
<p class="muted">完全独立于上面的 dev-time 编辑空间。</p>
<table>
<tr><th>路径</th><th>谁有权</th><th>延迟</th><th>典型操作</th></tr>
<tr><td>直接写</td><td>白名单含该工具的节点</td><td>立即</td><td>save_artifact / add_memory / 多数 KB 写</td></tr>
<tr><td><strong>propose gate</strong>（7 种）</td><td>所有节点（agent 仅提议）</td><td>跨 session（user accept 后）</td><td>PROFILE / PROJECT / shared skill / KB 高风险翻转</td></tr>
</table>
<p>→ <a href="human-in-the-loop.html">人在回路 §7</a> 讲完整的 propose gate：哪些状态走、哪些不走、两条车道。</p>
"""
    return body


def page_skills(data: dict) -> str:
    skills = data["skills"]
    body = f"""
<h1>Shared Skills</h1>
<p class="muted">共 <strong>{len(skills)} 个</strong> shared skill。每个 = 一个 folder + <code>SKILL.md</code>。framework auto-load。</p>

<div class="callout info">
  <strong>Skill vs Tool</strong>
  Tool 是<em>单步原子能力</em>（Python 函数），Skill 是<em>多步工作流模板</em>（markdown 指引）。详见 <a href="skill-spec.html">skill-spec.html</a>。
</div>

<div class="cards">
"""
    for s in skills:
        used = ", ".join(s["used_by_nodes"]) or "—"
        body += f"""
<div class="card">
  <h3><a href="skills/{s["name"]}.html">{s["name"]}</a> <span class="badge low">{s["status"]}</span></h3>
  <p>{html.escape(s["description"])}</p>
  <p class="muted" style="font-size:12px">用在节点：{used}</p>
</div>
"""
    body += "</div>"

    body += "<h2>详细参考（点击 card 进详情页看完整 SKILL.md）</h2>"
    for s in skills:
        # applies_when 条目可能是结构化 dict（owner 的 SKILL.md frontmatter
        # 演化了）—— 渲染器如实显示，不假设形状。
        applies_list = "".join(
            f"<li>{html.escape(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))}</li>"
            for a in s["applies_when"]
        )
        tools_list = ", ".join(
            tool_ref(t, data) for t in s["tools_used"]
        )
        used_by = ", ".join(
            f'<a href="nodes/{n}.html"><code>{n}</code></a>' for n in s["used_by_nodes"]
        ) or "—"
        body += f"""
<h3 id="{s["name"]}"><a href="skills/{s["name"]}.html">{s["name"]}</a> <span class="badge low">{s["status"]}</span></h3>
<p><strong>{html.escape(s["description"])}</strong></p>
<table>
<tr><td><strong>何时启用</strong></td><td><ul style="margin:0">{applies_list}</ul></td></tr>
<tr><td><strong>用到的工具</strong></td><td>{tools_list}</td></tr>
<tr><td><strong>预期产出</strong></td><td>{html.escape(s["expected_outcome"])}</td></tr>
<tr><td><strong>引用此 skill 的节点</strong></td><td>{used_by}</td></tr>
</table>
<p><a href="skills/{s["name"]}.html">→ 查看完整 SKILL.md + 详细工作流</a></p>
"""
    return body


def page_tool_spec(data: dict) -> str:
    md_text = (DOCS / "tool-spec.md").read_text(encoding="utf-8")
    body, _ = md_to_html(md_text)
    return body


def page_skill_spec(data: dict) -> str:
    md_text = (DOCS / "skill-spec.md").read_text(encoding="utf-8")
    body, _ = md_to_html(md_text)
    return body


def page_kb_system(data: dict) -> str:
    return _md_only_page("kb-system.md")

def page_memory_system(data: dict) -> str:
    return _md_only_page("memory-system.md")

def page_node_lifecycle(data: dict) -> str:
    return _md_only_page("node-lifecycle.md")

def _md_only_page(filename: str) -> str:
    """通用：纯 markdown → html，不带 diagram。"""
    md_text = (DOCS / filename).read_text(encoding="utf-8")
    body, _ = md_to_html(md_text)
    return body


def page_quick_start(data: dict) -> str:
    return _md_only_page("quick-start.md")

def page_node_iteration_guide(data: dict) -> str:
    md_text = (DOCS / "node-iteration-guide.md").read_text(encoding="utf-8")
    body, _ = md_to_html(md_text)
    diagram_html = diagram("node-iteration-loop.svg", "快反馈环 (~3 min/¥0.05) + 慢验证环 (~25 min/¥10) + 三条铁律")
    body = body.replace("<h2", diagram_html + "<h2", 1)
    return body




def _highlight_python(code: str) -> str:
    """Render python code as highlighted HTML via markdown.codehilite."""
    md = markdown.Markdown(extensions=["fenced_code", "codehilite"],
                            extension_configs={"codehilite": {"noclasses": False}})
    fenced = f"```python\n{code}\n```"
    return md.convert(fenced)


def _highlight_yaml(code: str) -> str:
    md = markdown.Markdown(extensions=["fenced_code", "codehilite"],
                            extension_configs={"codehilite": {"noclasses": False}})
    fenced = f"```yaml\n{code}\n```"
    return md.convert(fenced)


def _params_table_html(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "<p class='muted'>(no parameters_schema)</p>"
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not props:
        return "<p class='muted'>(此工具无参数。)</p>"
    rows = ""
    for pname, pdef in props.items():
        req = "✓" if pname in required else ""
        ptype = pdef.get("type", "—")
        enum = pdef.get("enum")
        if enum:
            ptype = f"{ptype} ∈ {{{', '.join(repr(e) for e in enum[:5])}}}"
        default = pdef.get("default", "")
        default_str = repr(default) if default != "" else ""
        desc = pdef.get("description", "")
        rows += (
            f"<tr><td><code>{html.escape(pname)}</code></td>"
            f"<td>{req}</td>"
            f"<td><code>{html.escape(str(ptype))}</code></td>"
            f"<td><code>{html.escape(default_str)}</code></td>"
            f"<td>{html.escape(desc)}</td></tr>"
        )
    return (
        f"<table><tr><th>参数</th><th>必填</th><th>类型</th><th>默认</th><th>说明</th></tr>"
        f"{rows}</table>"
    )


def page_tool_detail(tool: dict, data: dict) -> str:
    """单个工具的详情页（depth=1，位于 docs-html/tools/<name>.html）"""
    used_by_links = "".join(
        f'<a href="../nodes.html#{n}"><code>{n}</code></a>, '
        for n in tool["used_by_nodes"]
    )
    used_by_links = used_by_links.rstrip(", ") or "<span class='muted'>(暂无节点白名单列入)</span>"

    # Sibling tools (same module)
    sibling_tools = [t for t in data["tools_index"].values()
                      if t["module"] == tool["module"] and t["name"] != tool["name"]]
    sibling_links = "".join(
        f'<a href="{s["name"]}.html"><code>{s["name"]}</code></a>, '
        for s in sorted(sibling_tools, key=lambda x: x["name"])
    )
    sibling_links = sibling_links.rstrip(", ") or "<span class='muted'>(此模块只有这一个工具)</span>"

    # 能力闸控的工具：把"要装什么"写在页上。说的是**条件**不是某台机器的
    # 探测结果 —— 写成"本机没有"就等于把 build 机器的状态腌进站里。
    if tool.get("requires_capability"):
        requires_row = (
            "<dt>前置能力</dt><dd>⚠️ 需要 <b>%s</b>。"
            "探测不到时这个工具<b>不会注册</b>，agent 看不到它、"
            "手写调用会被当场拒掉（能力缺席就不给工具）。</dd>"
            % html.escape(tool["requires_capability"])
        )
    else:
        requires_row = ""

    src_html = _highlight_python(tool["source_code"]) if tool["source_code"] else \
                "<p class='muted'>(source not available)</p>"

    body = f"""
<div class="breadcrumb">
  <a href="../index.html">Home</a><span class="sep">›</span>
  <a href="../tools.html">Tools</a><span class="sep">›</span>
  <code>{html.escape(tool["name"])}</code>
</div>

<h1>{html.escape(tool["name"])} <span class="badge {tool["risk_level"]}">{tool["risk_level"]}</span></h1>

<dl class="meta-grid">
  <dt>风险等级</dt><dd><span class="badge {tool["risk_level"]}">{tool["risk_level"]}</span></dd>
  <dt>模块</dt><dd><code>{html.escape(tool["module"])}</code></dd>
  <dt>源文件</dt><dd><code>{html.escape(tool["source_file"])}:{tool["source_lineno"]}</code></dd>
  <dt>Executor</dt><dd><code>{html.escape(tool["executor_name"])}</code></dd>
  <dt>Allowed nodes</dt><dd>{'<span class="muted">无限制（任何节点都能用）</span>' if tool["allowed_node_types"] is None else ", ".join(f"<code>{n}</code>" for n in tool["allowed_node_types"])}</dd>
  <dt>使用此工具的节点</dt><dd>{used_by_links}</dd>
  <dt>同模块兄弟工具</dt><dd>{sibling_links}</dd>
  {requires_row}
</dl>

<h2>描述</h2>
<p>{html.escape(tool["full_description"]).replace(chr(10), '<br>')}</p>

<h2>参数（来自 parameters_schema）</h2>
{_params_table_html(tool["parameters"])}

<h2>源码实现</h2>
<div class="source-block">
  <div class="source-header">📄 <code>{html.escape(tool["source_file"])}:{tool["source_lineno"]}</code></div>
  {src_html}
</div>

<h2>调用示例</h2>
<p>LLM 看到此工具的 JSON Schema 后会自己构造调用。框架内部分发：</p>
<pre><code>from core.tool_registry import execute
result = await execute({tool["name"]!r}, state, ...)</code></pre>
<p>返回值约定见 <a href="../tool-spec.html">tool-spec.html § 2</a>。
此工具的返回必须含 <code>status</code> 字段（success / error / pause）。</p>

<h2>启用此工具</h2>
<p>在节点 <code>harness.yaml</code> 的 <code>tools:</code> 白名单加 <code>{html.escape(tool["name"])}</code>。
{("opt-in 工具，节点 <code>tools/__init__.py</code> 还得 import 触发注册：" if "library" in tool["source_file"] else "默认全节点可用（已被 <code>shared/tools/__init__.py</code> import）。")}
{f'''
<pre><code># nodes/&lt;my_node&gt;/tools/__init__.py
from shared.tools.library import {tool["module"].replace("lib/", "")}   # noqa: F401</code></pre>''' if "lib/" in tool["module"] else ""}</p>

<p class="muted" style="margin-top: 32px"><a href="../tools.html">← 回到工具索引</a></p>
"""
    return body


def page_skill_detail(skill: dict, data: dict) -> str:
    used_by_links = "".join(
        f'<a href="../nodes/{display_node_name(n)}.html"><code>{display_node_name(n)}</code></a>, '
        for n in skill["used_by_nodes"]
    )
    used_by_links = used_by_links.rstrip(", ") or "<span class='muted'>(暂无节点引用)</span>"
    body_md_rendered, _ = md_to_html(skill["body_markdown"])

    applies = "".join(
        f"<li>{html.escape(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))}</li>"
        for a in skill["applies_when"]
    )
    tools_used = "".join(
        tool_ref(t, data, rel="../") + ", "
        for t in skill["tools_used"]
    )
    tools_used = tools_used.rstrip(", ") or "—"

    body = f"""
<div class="breadcrumb">
  <a href="../index.html">Home</a><span class="sep">›</span>
  <a href="../skills.html">Skills</a><span class="sep">›</span>
  <code>{html.escape(skill["name"])}</code>
</div>

<h1>{html.escape(skill["name"])} <span class="badge low">{skill["status"]}</span></h1>
<p class="muted">{html.escape(skill["description"])}</p>

<dl class="meta-grid">
  <dt>状态</dt><dd><span class="badge low">{skill["status"]}</span></dd>
  <dt>Origin</dt><dd><code>{html.escape(skill["origin"])}</code></dd>
  <dt>源 folder</dt><dd><code>{html.escape(skill["source_dir"])}</code></dd>
  <dt>用到的工具</dt><dd>{tools_used}</dd>
  <dt>引用此 skill 的节点</dt><dd>{used_by_links}</dd>
</dl>

<h2>何时启用（applies_when）</h2>
<ul>{applies}</ul>

<h2>预期产出</h2>
<p>{html.escape(skill["expected_outcome"])}</p>

<h2>完整 SKILL.md body</h2>
<div class="source-block">
  <div class="source-header">📄 <code>{html.escape(skill["source_dir"])}/SKILL.md</code></div>
  <div style="border: 1px solid var(--color-border); border-top: none; padding: 16px; border-radius: 0 0 6px 6px">
  {body_md_rendered}
  </div>
</div>

<p class="muted" style="margin-top: 32px"><a href="../skills.html">← 回到 Skills 索引</a></p>
"""
    return body


def page_hook_detail(hook: dict) -> str:
    callbacks_html = ""
    phase_titles = {
        "on_turn_start": "🔌 on_turn_start（每轮 LLM call 前）",
        "on_llm_response": "🔌 on_llm_response（LLM 刚返回，观察用）",
        "on_turn_end": "🔌 on_turn_end（一轮 tool 调度完）",
        "on_end": "🔌 on_end（loop 整体结束）",
    }
    for phase, cb in hook["callbacks"].items():
        callbacks_html += f"""
<h2>{phase_titles.get(phase, phase)}</h2>
<dl class="meta-grid">
  <dt>函数</dt><dd><code>{html.escape(cb["func_name"])}</code></dd>
  <dt>源文件</dt><dd><code>{html.escape(cb["source_file"])}:{cb["source_lineno"]}</code></dd>
</dl>
<div class="source-block">
  <div class="source-header">📄 <code>{html.escape(cb["source_file"])}:{cb["source_lineno"]}</code></div>
  {_highlight_python(cb["source_code"])}
</div>
"""

    body = f"""
<div class="breadcrumb">
  <a href="../index.html">Home</a><span class="sep">›</span>
  <a href="../context-hooks.html">Context &amp; Hooks</a><span class="sep">›</span>
  <code>{html.escape(hook["name"])}</code>
</div>

<h1>Hook: {html.escape(hook["name"])}</h1>
<p class="muted">{html.escape(hook["description"])}</p>

<div class="callout info">
  <strong>启用方式</strong>
  在 <code>harness.yaml</code> 的 <code>loop_hooks: [...]</code> 列表加 <code>{html.escape(hook["name"])}</code>。
  注：<code>memory_delta</code> 和 <code>kb_delta</code> 总是启用（内置于 <code>agent_loop.py:_ALWAYS_ON_HOOKS</code>），不需要列入。
</div>

{callbacks_html}

<p class="muted" style="margin-top: 32px"><a href="../context-hooks.html">← 回到 Context &amp; Hooks 解剖</a></p>
"""
    return body


def page_node_detail(node: dict, data: dict) -> str:
    """单节点详情页 docs-html/nodes/<name>.html"""
    # 工具列表交叉链接到 per-tool 页
    tools_html = "".join(
        tool_ref(t, data, rel="../", style="display:inline-block;margin:2px 4px;padding:3px 8px;background:var(--color-bg-elevated);border-radius:4px;font-size:12px")
        for t in node["tools"]
    )
    skills_html = "".join(
        f'<a href="../skills/{s}.html"><code>{s}</code></a>, '
        for s in node["skills"]
    )
    skills_html = skills_html.rstrip(", ") or "—"
    rules_list = "".join(f"<li>{html.escape(r)}</li>" for r in node["rules"])
    guidelines_list = "".join(f"<li>{html.escape(g)}</li>" for g in node["guidelines"])
    expected_in = "".join(
        f"<li><code>{k}</code>: {html.escape(str(v)[:200])}</li>"
        for k, v in node["expected_inputs"].items()
    )
    expected_out = "".join(
        f"<li><code>{k}</code>: {html.escape(str(v)[:200])}</li>"
        for k, v in node["expected_outputs"].items()
    )

    # Load harness yaml raw
    yaml_path = ROOT / "nodes" / node["name"] / "harness.yaml"
    yaml_raw = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""

    body = f"""
<div class="breadcrumb">
  <a href="../index.html">Home</a><span class="sep">›</span>
  <a href="../nodes.html">Nodes</a><span class="sep">›</span>
  <code>{html.escape(node["name"])}</code>
</div>

<h1><code>{html.escape(node["name"])}</code> <span class="badge {node["risk_level"]}">{node["risk_level"]}</span> <span class="muted" style="font-size:18px">v{node["version"]}</span></h1>
<p><strong>{html.escape(node["role_line"])}</strong></p>

<dl class="meta-grid">
  <dt>风险等级</dt><dd><span class="badge {node["risk_level"]}">{node["risk_level"]}</span></dd>
  <dt>版本</dt><dd>v{node["version"]}</dd>
  <dt>必需输入 artifact</dt><dd>{', '.join(f'<code>{t}</code>' for t in node["required_input"]) or '<span class="muted">— (无 hard 依赖)</span>'}</dd>
  <dt>必需输出 artifact</dt><dd>{', '.join(f'<code>{t}</code>' for t in node["required_output"]) or '—'}</dd>
  <dt>callable_nodes</dt><dd>{', '.join(f'<code>{c}</code>' for c in node["callable_nodes"]) if node["callable_nodes"] else '<span class="muted">— (leaf)</span>'}</dd>
  <dt>max_turns</dt><dd>{"无限（开发模式）" if node["max_turns"] == 0 else node["max_turns"]}</dd>
</dl>

<h2>调用参数（expected_inputs）</h2>
<ul>{expected_in or '<li class="muted">无</li>'}</ul>

<h2>产出契约（expected_outputs）</h2>
<ul>{expected_out or '<li class="muted">无</li>'}</ul>

<h2>启用的 skills</h2>
<p>{skills_html}</p>

<h2>工具白名单（{len(node["tools"])} 个，可点击看详情）</h2>
<p style="line-height:2.4">{tools_html}</p>

<h2>硬约束（rules）</h2>
<ul>{rules_list or '<li class="muted">无</li>'}</ul>

<h2>建议（guidelines）</h2>
<ul>{guidelines_list or '<li class="muted">无</li>'}</ul>

<h2>完整 system_prompt</h2>
<details>
<summary>展开查看（{len(node["system_prompt"])} 字符）</summary>
<pre style="max-height: 500px; overflow: auto">{html.escape(node["system_prompt"])}</pre>
</details>

<h2>完整 harness.yaml</h2>
<details>
<summary>展开查看（{len(yaml_raw.splitlines())} 行）</summary>
{_highlight_yaml(yaml_raw)}
</details>

<p class="muted" style="margin-top: 32px"><a href="../nodes.html">← 回到 Nodes 索引</a></p>
"""
    return body


def page_context_hooks(data: dict) -> str:
    """Context Engine + Hooks 详解 + 2 张图 + 5 个内置 hook 卡片"""
    md_text = (DOCS / "context-and-hooks.md").read_text(encoding="utf-8")
    body, _ = md_to_html(md_text)

    # 5 个内置 hook 卡片
    hook_cards = ""
    for h in data["hooks"]:
        phases = ", ".join(f"<code>{p}</code>" for p in h["callbacks"].keys())
        hook_cards += f"""
<div class="card">
  <h3><a href="hooks/{h["name"]}.html">{h["name"]}</a></h3>
  <p>{html.escape(h["description"][:200])}</p>
  <p class="muted" style="font-size:12px">钩子点：{phases}</p>
</div>
"""

    intro = f"""
<p class="muted">LLM 在你节点里 <strong>看到什么</strong> + 每轮 <strong>被打断在哪几个点</strong>。同事 debug 行为 / 加自己的注入 / 写 hook 时**必读**。</p>

<div class="callout info">
  <strong>这页解答 4 个问题</strong>
  <ol style="margin:6px 0;font-size:13px">
    <li>LLM 每次 call 看到的 system / user / inject message 到底有哪些？</li>
    <li>每段从哪儿来 / 谁能改 / 怎么改？</li>
    <li>agent loop 在哪 4 个点能插入 hook，每个 hook 拿到什么、能干什么、不能干什么？</li>
    <li>{len(data["hooks"])} 个内置 hook 是干啥的 + 怎么写自己的？</li>
  </ol>
</div>

{diagram("context-anatomy.svg", "LLM 每次 call 看到的 context 完整解剖 —— 13 段（系统 8 + user 5）+ 每轮 hook 注入")}

{diagram("hook-points.svg", "Loop 4 个钩子点 —— 各能拿什么、能注入什么、典型用例")}

<h2>内置 Hook 速览（点 card 看源码）</h2>
<div class="cards">{hook_cards}</div>
"""
    return intro + body


# ── 生成器自身的闸 ────────────────────────────────────────────────────
#
# 这个站会烂成 2026-08-21 那个样子（已删节点的页面还活着、首页计数差 5、
# 六份 md 点名的工具全部不存在），根因不是没人写文档，是**生成器没有任何一条
# 闸能发现自己在输出过期内容**。下面三条全部机械，失败即 build 失败。


class BuildError(RuntimeError):
    pass


def sweep_stale_outputs(expected: set[str]) -> list[str]:
    """扫盘删除不该存在的输出文件。

    判据是「应有文件集」，不是一张要人维护的删除名单 —— 删名单的形状是
    「新东西默认漏过」，而这里要防的正是「旧东西默认留下」。
    """
    removed = []
    for path in sorted(OUT.rglob("*.html")):
        rel = path.relative_to(OUT).as_posix()
        if rel not in expected:
            path.unlink()
            removed.append(rel)
    return removed


def check_internal_links(expected: set[str]) -> list[str]:
    """扫全部内部 href，指向不存在的文件即失败。"""
    bad = []
    for path in sorted(OUT.rglob("*.html")):
        rel_dir = path.relative_to(OUT).parent
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'href="([^"#?]+\.html)(?:#[^"]*)?"', text):
            href = m.group(1)
            if href.startswith(("http://", "https://", "//")):
                continue
            target = (rel_dir / href).as_posix()
            # 规范化 ../
            parts: list[str] = []
            for seg in target.split("/"):
                if seg == "..":
                    if parts:
                        parts.pop()
                elif seg not in (".", ""):
                    parts.append(seg)
            target = "/".join(parts)
            if target not in expected:
                bad.append(f"{path.relative_to(OUT)} → {href}")
    return sorted(set(bad))


def check_tool_names_in_docs(live_tools: set[str]) -> list[str]:
    """docs/*.md 里点名的工具必须真的存在。

    与 PR#510 给 prompt 装的那条扫盘闸同一个形状：文档点名一个不存在的工具，
    读的人会照着调，然后发现整条路走不通。
    """
    # 只认「明确写成工具」的形状：`name(` 反引号包裹，或 markdown 链接到 tools/
    pat_code = re.compile(r"`([a-z][a-z0-9_]{3,})\((?:[^`]*)?`")
    pat_link = re.compile(r"\[`([a-z][a-z0-9_]{3,})`\]\(tools/")
    bad = []
    for md in sorted(DOCS.glob("*.md")):
        if _is_archive_doc(md.stem):
            continue  # 决策档案是历史记录，保留当时的措辞
        if md.name in _DOC_GATE_EXEMPT:
            continue
        text = md.read_text(encoding="utf-8")
        for m in list(pat_code.finditer(text)) + list(pat_link.finditer(text)):
            name = m.group(1)
            if name in live_tools or name in _NOT_A_TOOL:
                continue
            bad.append(f"{md.name}: {name}()")
    return sorted(set(bad))


#: 长得像工具名但不是工具的 token（Python 函数、CLI、示例代码里的自定义函数）。
#: 白名单只在这里长，且每一条都该说得出为什么。
_NOT_A_TOOL = {
    "load_harness", "build_messages", "register_loop_hook", "register_summarizer",
    "resume_loop", "drive_pause_chain", "run_node_sync", "bootstrap",
    "read_register", "closure_tally", "last_dreaming_at", "iter_version_records",
    "my_tool", "extract_keywords", "some_tool", "your_tool", "example_tool",
    "checkpoint_session_workspace", "exemplar_candidates", "find_stale_suspects",
    # 会话驱动 / 注册期的 Python API，不是 agent 能调的工具
    "run_turn", "answer", "register_tool", "register_loop_hook",
    "compute_orchestration_closure", "brief_interrupted_child_runs",
    "load_messages_checkpoint", "print", "open", "len", "range", "sorted",
    # 模型角色：节点内部的 Python 函数（审图协议）与平台后端的治理入口，
    # 都不是 agent 能调的工具面
    "review_image", "select_effective_backend",
    # 活性判决是后端服务层的 Python 函数（app/services/run_liveness.py），
    # 不是 agent 能调的工具面 —— 同 select_effective_backend。
    "observed_status", "observed_status_map",
    # 个人版这一轮的平台侧 Python 函数：装配层的能力集（app/assembly.py）、
    # 「谁在开」的现算（app/services/driving.py）、组织连接的桌面侧入口。
    # `complete_text` 是 WP-05 删掉的那个后端自有 LLM 客户端方法 —— 计划文档
    # 里点它的名字是为了说"这个删了"，读的人不会照着调一个不存在的东西。
    "capabilities", "who_is_driving", "connect", "complete_text",
    # 两种发行那份派工单（EXEC_PLAN_TWO_EDITIONS）点名的路径函数：`org_root` 是
    # core/paths 里 org 层的家、`data_root` 是平台 app/config 里数据根底下的四类
    # 布局、`the_org_home` 是计划里要新写的那一个（与 the_data_root 同形）。
    # 三个都是 Python 函数，不是 agent 能调的工具面。
    "data_root", "org_root", "the_org_home",
}

#: 豁免整份文件的名单（历史迁移记录一类）。空着最好。
_DOC_GATE_EXEMPT: set[str] = set()


def main():
    OUT.mkdir(exist_ok=True)
    ASSETS.mkdir(exist_ok=True)
    (ASSETS / "diagrams").mkdir(exist_ok=True)

    print("Bootstrapping framework + collecting live data...")
    data = introspect_tools_and_nodes()
    n_tools = sum(len(v) for v in data["tools"].values())
    print(f"  tools: {n_tools}  nodes: {len(data['nodes'])}  skills: {len(data['skills'])}")

    # ── 闸 3：文档点名的工具必须存在（在写任何文件之前就该失败）────────
    dead_tools = check_tool_names_in_docs(set(data["tools_index"]))
    if dead_tools:
        raise BuildError(
            "docs 里点名了不存在的工具（读的人会照着调）：\n  "
            + "\n  ".join(dead_tools)
            + "\n\n合法工具名见 tools.html；确实不是工具的加进 _NOT_A_TOOL。"
        )

    pages = {
        "index.html": ("Home", page_index(data)),
        "quick-start.html": ("Quick Start", page_quick_start(data)),

        "design-principles.html": ("设计判据", _md_only_page("design-principles.md")),
        "architecture.html": ("Architecture", page_architecture(data)),

        "research-loop.html": ("研究循环", page_research_loop(data)),
        "evidence-modalities.html": ("证据模态", _md_only_page("evidence-modalities.md")),
        "node-lifecycle.html": ("Node Lifecycle", page_node_lifecycle(data)),

        "commitment-and-evidence.html": ("承诺与证据", _md_only_page("commitment-and-evidence.md")),
        "artifact-model.html": ("Artifact 模型", _md_only_page("artifact-model.md")),

        "kb-system.html": ("KB System", page_kb_system(data)),
        "memory-system.html": ("Memory System", page_memory_system(data)),

        "human-in-the-loop.html": ("Human-in-the-Loop", _md_only_page("human-in-the-loop.md")),

        "runtime.html": ("Runtime", _md_only_page("runtime.md")),
        "context-hooks.html": ("Context & Hooks", page_context_hooks(data)),

        "owner-guide.html": ("Owner Guide", page_owner_guide(data)),
        "node-iteration-guide.html": ("Node 迭代 + 测试指南", page_node_iteration_guide(data)),
        "dev-workflow.html": ("Dev Workflow", _md_only_page("dev-workflow.md")),

        "nodes.html": ("节点目录", page_nodes(data)),
        "tools.html": ("Tools", page_tools(data)),
        "skills.html": ("Skills", page_skills(data)),
        "tool-spec.html": ("Tool Spec", page_tool_spec(data)),
        "skill-spec.html": ("Skill Spec", page_skill_spec(data)),

        "decision-archive.html": ("决策档案", page_decision_archive(data)),
    }

    # ── Detail pages（子目录）─────────────────────────────────────────
    for sub in ("tools", "skills", "hooks", "nodes", "rfc"):
        (OUT / sub).mkdir(exist_ok=True)

    detail_pages: dict[str, tuple[str, str]] = {}
    for tname, tinfo in data["tools_index"].items():
        detail_pages[f"tools/{tname}.html"] = (f"Tool: {tname}", page_tool_detail(tinfo, data))
    for s in data["skills"]:
        detail_pages[f"skills/{s['name']}.html"] = (f"Skill: {s['name']}", page_skill_detail(s, data))
    for h in data["hooks"]:
        detail_pages[f"hooks/{h['name']}.html"] = (f"Hook: {h['name']}", page_hook_detail(h))
    for n in data["nodes"]:
        disp = display_node_name(n["name"])
        detail_pages[f"nodes/{disp}.html"] = (f"Node: {disp}", page_node_detail(n, data))

    # 旧节点名 → 新名的跳转页（站外链接不断）
    for old, new in NODE_ALIAS_REDIRECTS.items():
        detail_pages[f"nodes/{old}.html"] = (
            f"Node: {old} → {new}",
            f'''<h1>{old} 现在叫 <a href="{new}.html">{new}</a></h1>
<p class="muted">这个节点的身份以 harness 自述为准。目录名与 <code>node_type</code>
仍是 <code>{old}</code>（改名是独立的一条线），站上只显示一个身份。</p>
<p><a href="{new}.html">→ 前往 {new}</a></p>
<meta http-equiv="refresh" content="0; url={new}.html">''',
        )

    # RFC / 决策档案原文页
    for rel, title in _archive_docs():
        detail_pages[f"rfc/{rel}.html"] = (title, page_archive_doc(rel, title))

    print(f"\n--- Detail pages: {len(detail_pages)} ---")
    for fname, (title, body) in detail_pages.items():
        out = render_page(active=fname.split("/")[0] + ".html", title=title,
                          body_html=body, depth=1)
        (OUT / fname).write_text(out, encoding="utf-8")
    print(f"  ✓ wrote {len(detail_pages)} detail pages")

    print("\n--- Root pages ---")
    for fname, (title, body) in pages.items():
        toc_items = []
        seen_anchors: set[str] = set()
        for m in re.finditer(r'<h([23])(?:\s+id="([^"]+)")?[^>]*>(.+?)</h\1>',
                             body, re.DOTALL):
            level, anchor, text = m.group(1), m.group(2), m.group(3)
            txt = re.sub(r'<[^>]+>', '', text).strip()[:60]
            if not anchor:
                base = re.sub(r'[^a-zA-Z0-9-]', '-', txt.lower())[:40].strip('-') or "s"
                anchor = base
                k = 2
                while anchor in seen_anchors:
                    anchor = f"{base}-{k}"
                    k += 1
                body = body.replace(m.group(0),
                                    f'<h{level} id="{anchor}">{text}</h{level}>', 1)
            seen_anchors.add(anchor)
            toc_items.append((level, anchor, txt))
        toc_html = ""
        if toc_items:
            toc_html = "<h4>本页目录</h4><ul>"
            for level, anchor, txt in toc_items:
                cls = "" if level == "2" else ' class="h3"'
                toc_html += f'<li{cls}><a href="#{anchor}">{html.escape(txt)}</a></li>'
            toc_html += "</ul>"
        out = render_page(active=fname, title=title, body_html=body, toc_html=toc_html)
        (OUT / fname).write_text(out, encoding="utf-8")
        print(f"  ✓ {fname} ({len(out)} chars)")

    # ── 闸 1：扫盘清理 ────────────────────────────────────────────────
    expected = set(pages) | set(detail_pages)
    removed = sweep_stale_outputs(expected)
    if removed:
        print(f"\n--- 扫盘：删除 {len(removed)} 个过期输出 ---")
        for r in removed:
            print(f"  ✗ {r}")

    # ── 闸 2：死链 ────────────────────────────────────────────────────
    bad_links = check_internal_links(expected)
    if bad_links:
        raise BuildError(
            "站内死链（生成器写出了指向不存在页面的链接）：\n  "
            + "\n  ".join(bad_links)
        )

    print(f"\n✅ Done. {len(expected)} pages. Open: file://{OUT}/index.html")


if __name__ == "__main__":
    main()
