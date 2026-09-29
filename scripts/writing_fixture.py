#!/usr/bin/env python3
"""夹具运行器：把一份用户材料包 + 原话交给 writing 节点跑一次，收成品与六把尺子。

用法（在 harness-framework 根目录）：

    python scripts/writing_fixture.py --fixture ../../research_runs/astra-sim-yuankk-20260918 \
        --inputs prod            # prod = 生产时调度器派给 writing 的 node_inputs 原样
                                 # user = 用户原话直接作为 research_question
        [--max-turns 300] [--project-id astra-yuankk-xxx] [--lab DIR] [--seed-only]

它做的事：
  1. 在 `--lab`（默认 <fixture>/lab）下建一个干净的 HARNESS_FRAMEWORK_HOME 与项目工作区；
  2. 把材料包按平台的方式落进 `sources/`（zip + 解压目录 + .ref），并按调度器的方式
     `import_artifact` 两份外来件（草稿、数据表）；
  3. 以一个绑定了工作区的 orchestrator State 作父，调用 `execute_node("writing")`；
     节点若 pause 提问，用脚本化答复继续（作者缺失 / 刊物未定）；
  4. 结束后从 transcript、cost ledger、工作区里算六把尺子，把成品拷到
     <fixture>/results/<stamp>/，写 metrics.json。

尺子只在这里算，不改被测系统；硬线 v0 也先在这里实现，批 1 再搬进框架。
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(line_buffering=True)  # 后台跑时日志实时可见
except Exception:  # noqa: BLE001
    pass

PROCESS_VOCAB = [
    "artifact", "上游", "revision item", "revision_items", "机械审计", "用户交来",
    "本平台", "not provided in upstream", "占位", "待补充", "投稿前需补充", "列入",
    "adequacy", "receipt", "preflight",
]
EMPTY_REF_PATTERNS = [r"第\s+节", r"第\s*节的", r"Section\s+\.", r"\?\?", r"图\s+所示", r"表\s+所示"]


def _load_env(env_file: Path | None) -> None:
    from dotenv import load_dotenv
    if env_file and env_file.is_file():
        load_dotenv(env_file, override=False)


def _scripted_answer(question: str) -> str:
    q = question or ""
    if any(k in q for k in ("高危", "批准", "approve", "危险")):
        return "批准执行"
    if any(k in q for k in ("作者", "author", "机构", "affiliation")):
        return "作者与机构信息暂无，标题块只出标题，作者信息留给作者在备注里补。"
    if any(k in q for k in ("期刊", "venue", "journal", "投")):
        return "目标期刊未定，按通用中文 SCI 研究论文体裁写；其余按你的判断继续。"
    return "按你的最佳判断继续，不要等我。"


def seed_worktree(fixture: Path, wt: Path, zip_name: str) -> dict:
    """把材料包按平台方式放进 sources/，返回可导入的外来件相对路径。"""
    from core.materials import place
    from core.project_bootstrap import _git

    zip_path = fixture / "inputs" / zip_name
    if not zip_path.is_file():
        # 夹具里 zip 可能在 outputs/sources 下（从 node20 拷来的）
        alt = fixture / "outputs" / "sources" / zip_name
        zip_path = alt if alt.is_file() else zip_path
    if not zip_path.is_file():
        raise SystemExit(f"找不到材料包：{zip_path}")
    ref, to_commit = place(wt, zip_name, zip_path, uploaded_by="fixture", note="夹具材料包")
    extracted = wt / "sources" / f"{zip_name}.extracted"
    if not extracted.exists():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)
    # 平台把解压目录留在 sources/ 下且不进 git（sources/.gitignore 由 materials 维护）
    _git(wt, "add", "-A")
    try:
        _git(wt, "-c", "user.name=fixture", "-c", "user.email=fixture@local",
             "commit", "-q", "-m", "fixture: seed user materials")
    except Exception as exc:  # noqa: BLE001 — 已提交过就算幂等
        if "nothing to commit" not in str(exc):
            raise
    found = {}
    for p in extracted.rglob("*"):
        if p.suffix == ".md" and "draft" in p.name:
            found["draft"] = p.relative_to(wt).as_posix()
        elif p.suffix == ".md" and "matrix" in p.name:
            found["dataset"] = p.relative_to(wt).as_posix()
    return found


async def run_once(args: argparse.Namespace) -> dict:
    fixture = args.fixture.resolve()
    lab = (args.lab or (fixture / "lab")).resolve()
    home = lab / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_FRAMEWORK_HOME"] = str(home)
    os.environ.setdefault("HARNESS_RUNTIME_PROFILE", "dev")
    _load_env(args.env)

    from core.bootstrap import bootstrap
    bootstrap()

    from core import paths
    from core.executor import execute_node
    from core.loader import load_harness
    from core.pause_driver import drive_pause_chain
    from core.project_bootstrap import ensure_project_worktree
    from core.project_workspace import bind_project_workspace
    from core.state import State, _project_root

    project_id = args.project_id or f"astra-yuankk-{time.strftime('%m%d-%H%M%S')}"
    wt = ensure_project_worktree(project_id)
    assert wt is not None
    print(f"[fixture] project={project_id}\n[fixture] worktree={wt}")

    found = seed_worktree(fixture, wt, args.zip)
    print(f"[fixture] seeded: {found}")

    # 父 state：一个绑定了工作区的 orchestrator（与 chat.py / 平台同构）
    runs_parent = paths.runs_parent(project_id)
    runs_parent.mkdir(parents=True, exist_ok=True)
    parent_run_id = f"orchestrator__{project_id}__session__fixture"
    parent_root = runs_parent / parent_run_id
    parent_root.mkdir(parents=True, exist_ok=True)
    (parent_root / "artifacts").mkdir(exist_ok=True)
    parent = State(
        run_id=parent_run_id, node_type="_orchestrator", root=parent_root,
        tenant_id="local-tenant", project_id=project_id, session_id="fixture",
        project_root=_project_root(project_id),
    )
    bind_project_workspace(parent, wt)

    # 调度器在生产里做的两次 import_artifact
    from shared.tools.library.artifact_intake import _import_artifact
    forwarded: list[str] = []
    if args.import_upstream and found:
        r1 = await _import_artifact(
            parent, "manuscript", "moe_topology_draft_cn_v6", found["draft"],
            note="用户交来的已有中文论文草稿（v6），作为改写参考底稿，非本平台产出。")
        r2 = await _import_artifact(
            parent, "dataset", "four_model_full_matrix_tables_cn_v3", found["dataset"],
            note="用户交来的四模型全组合 Wall/GPU/Comm 明细数据（768 单元），用于重新绘制论文图表。")
        for r in (r1, r2):
            if r.get("status") != "success":
                print("[fixture] import failed:", r)
            else:
                forwarded.append(r.get("artifact_id") or r.get("id") or "")
        forwarded = [x for x in forwarded if x]
        print(f"[fixture] imported: {forwarded}")

    if args.inputs == "prod":
        spec = json.loads((fixture / "inputs" / "prod_node_inputs.json").read_text(encoding="utf-8"))
        node_inputs = spec["node_inputs"]
    else:
        prompt = (fixture / "inputs" / "user_prompt.txt").read_text(encoding="utf-8").strip()
        node_inputs = {"research_question": prompt}
        if args.target_venue:
            node_inputs["target_venue"] = args.target_venue

    if args.seed_only:
        return {"project_id": project_id, "worktree": str(wt), "forwarded": forwarded}

    harness = load_harness("writing")
    if args.max_turns:
        harness.max_turns = int(args.max_turns)

    t0 = time.time()
    summary = await execute_node(
        "writing",
        state_dir=runs_parent,
        project_id=project_id,
        node_inputs=node_inputs,
        parent_state=parent,
        selected_input_ids=forwarded or None,
        sub_run_id="fixture->writing@d1",
        harness_override=harness,
    )
    rounds = 0
    while summary.get("status") == "paused" and rounds < 6:
        rounds += 1
        print(f"[fixture] paused (round {rounds}) → scripted answer")

        async def _ask(ev):
            q = getattr(ev, "question", None) or getattr(ev, "prompt", None) or str(ev)
            print("[fixture] Q:", str(q)[:400])
            a = _scripted_answer(str(q))
            print("[fixture] A:", a)
            return a

        from core.executor import finalize_run
        await drive_pause_chain(ask_fn=_ask, finalize_fn=finalize_run)
        # drive_pause_chain 结束后 summary.json 已被 finalize 更新
        sd = Path(summary.get("state_dir") or "")
        if sd and (sd / "summary.json").is_file():
            summary = json.loads((sd / "summary.json").read_text(encoding="utf-8"))
        else:
            break
    elapsed = time.time() - t0
    summary["_elapsed_s"] = round(elapsed)
    summary["_project_id"] = project_id
    summary["_worktree"] = str(wt)
    return summary


# ── 尺子 ────────────────────────────────────────────────────────────────────

def _pdf_text(pdf: Path) -> str:
    try:
        return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True,
                              text=True, timeout=120).stdout
    except Exception:  # noqa: BLE001
        return ""


def hard_lines(pdf: Path | None, tex_dir: Path | None) -> dict:
    out: dict = {"pdf": str(pdf) if pdf else None}
    if not pdf or not pdf.is_file():
        out["title_block"] = False
        out["reason"] = "no pdf"
        return out
    text = _pdf_text(pdf)
    pages = text.split("\f")
    first = pages[0] if pages else ""
    lines = [l.strip() for l in first.splitlines() if l.strip()]
    # 首页在「Abstract/摘要」之前要有至少一行非空且不是 Abstract 本身
    head = []
    for l in lines:
        if re.match(r"^(Abstract|摘\s*要)\b", l):
            break
        head.append(l)
    out["title_block"] = len(head) >= 1
    out["first_lines"] = lines[:4]
    body = text
    hits = {kw: len(re.findall(re.escape(kw), body)) for kw in PROCESS_VOCAB}
    out["process_vocab_hits"] = {k: v for k, v in hits.items() if v}
    out["process_vocab_ok"] = not out["process_vocab_hits"]
    ers = {p: len(re.findall(p, body)) for p in EMPTY_REF_PATTERNS}
    out["empty_ref_hits"] = {k: v for k, v in ers.items() if v}
    out["empty_refs_ok"] = not out["empty_ref_hits"]
    figs = sorted({int(n) for n in re.findall(r"(?:Figure|图)\s*(\d{1,2})\b", body)})
    out["figure_numbers_seen"] = figs
    out["pages"] = len([p for p in pages if p.strip()])
    return out


def metrics_from_run(run_dir: Path, home: Path, project_id: str) -> dict:
    m: dict = {}
    tr = run_dir / "transcript.jsonl"
    turns = 0; tools = collections.Counter(); compress = 0; writes = 0
    if tr.is_file():
        for line in tr.open(encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            e = d.get("event")
            if e == "llm_response":
                turns += 1
                for tc in d.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("name"):
                        tools[tc["name"]] += 1
            elif e == "summarizer_compress":
                compress += 1
    writing_tools = {"draft_section", "revise_section", "write_outline", "write_brief", "write_bibliography",
                     "write_author_notes", "write_file", "edit_file"}
    writes = sum(v for k, v in tools.items() if k in writing_tools)
    m["turns"] = turns
    m["compressions"] = compress
    m["tool_histogram"] = dict(tools.most_common())
    m["writing_turn_share"] = round(writes / turns, 3) if turns else None
    # tokens：cost ledger
    tokens = 0; calls = 0
    for ledger in list(home.rglob("llm_cost.jsonl")):
        for line in ledger.open(encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("node") == "writing":
                tokens += int(d.get("total_tokens") or 0); calls += 1
    m["tokens_writing"] = tokens
    m["llm_calls_writing"] = calls
    return m


def collect(summary: dict, fixture: Path, lab: Path) -> dict:
    wt = Path(summary["_worktree"])
    run_dir = Path(summary.get("state_dir") or "")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = fixture / "results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(wt.rglob("*.pdf"), key=lambda p: p.stat().st_mtime)
    pdfs = [p for p in pdfs if "font_probe" not in p.name and "/figures/" not in p.as_posix()]
    clean = [p for p in pdfs if "clean" in p.name] or pdfs
    pdf = clean[-1] if clean else None
    tex_dirs = [p for p in wt.glob("paper/*") if p.is_dir()]
    hl = hard_lines(pdf, tex_dirs[0] if tex_dirs else None)
    metrics = metrics_from_run(run_dir, lab / "home", summary["_project_id"]) if run_dir.is_dir() else {}
    result = {
        "stamp": stamp, "project_id": summary["_project_id"], "worktree": str(wt),
        "run_dir": str(run_dir), "status": summary.get("status"),
        "elapsed_s": summary.get("_elapsed_s"), "hard_lines": hl, "metrics": metrics,
        "final_text_preview": (summary.get("final_text_preview") or "")[:800],
    }
    if pdf and pdf.is_file():
        shutil.copy2(pdf, out_dir / pdf.name)
    for p in wt.glob("paper/**/*.tex"):
        rel = p.relative_to(wt)
        dst = out_dir / "tex" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    if run_dir.is_dir():
        for name in ("summary.json", "transcript.jsonl"):
            src = run_dir / name
            if src.is_file():
                shutil.copy2(src, out_dir / name)
    (out_dir / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--fixture", type=Path, required=True)
    p.add_argument("--lab", type=Path, default=None)
    p.add_argument("--inputs", choices=("prod", "user"), default="prod")
    p.add_argument("--target-venue", default=None)
    p.add_argument("--zip", default="moe_paper_v7_with_figures.zip")
    p.add_argument("--env", type=Path, default=ROOT.parent.parent / "harness-framework" / ".env")
    p.add_argument("--project-id", default=None)
    p.add_argument("--max-turns", type=int, default=0)
    p.add_argument("--import-upstream", action="store_true", default=True)
    p.add_argument("--no-import-upstream", dest="import_upstream", action="store_false")
    p.add_argument("--seed-only", action="store_true")
    args = p.parse_args()
    summary = asyncio.run(run_once(args))
    if args.seed_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    lab = (args.lab or (args.fixture / "lab")).resolve()
    result = collect(summary, args.fixture.resolve(), lab)
    print("\n================ RESULT ================")
    print(json.dumps({k: v for k, v in result.items() if k != "final_text_preview"}, ensure_ascii=False, indent=2))
    print("\nfinal_text_preview:", result["final_text_preview"][:600])


if __name__ == "__main__":
    main()
