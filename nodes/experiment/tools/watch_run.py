#!/usr/bin/env python3
"""Pretty-tail the latest harness transcript for experiment debugging.

This is a standalone observer script. It does not import core modules and does
not change run state; it only reads transcript.jsonl and prints a compact view.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable


HOOK_TAGS = (
    "[strategic_review]",
    "[execution_control]",
    "[corruption_sentinel]",
    "[health_tick]",
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pretty-tail a harness transcript.jsonl")
    p.add_argument("--path", type=Path, help="Specific transcript.jsonl to read.")
    p.add_argument("--home", type=Path, help="HARNESS_FRAMEWORK_HOME or sandbox root.")
    p.add_argument("--project-id", help="Prefer runs under projects/<project-id>/runs.")
    p.add_argument("--run-id", help="Prefer a specific run id.")
    p.add_argument("--no-follow", action="store_true", help="Print current contents and exit.")
    p.add_argument("--show-hooks", action="store_true", help="Also print hook_injection events.")
    p.add_argument("--max-chars", type=int, default=1200, help="Max chars per printed field.")
    p.add_argument("--wait", type=float, default=0.5, help="Polling interval while following.")
    return p.parse_args()


def _roots(home: Path | None) -> list[Path]:
    if home:
        return [home.expanduser()]
    roots: list[Path] = []
    env_home = os.getenv("HARNESS_FRAMEWORK_HOME")
    if env_home:
        roots.append(Path(env_home).expanduser())
    from core.paths import default_home  # 「默认根在哪」一处回答（含 Windows 分支）

    roots.append(default_home())
    for p in glob.glob("/tmp/hf-sandbox-experiment-*"):
        roots.append(Path(p))
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen and r.exists():
            out.append(r)
            seen.add(key)
    return out


def _candidate_transcripts(root: Path, project_id: str | None, run_id: str | None) -> Iterable[Path]:
    if run_id:
        patterns = [
            root / "runs" / run_id / "transcript.jsonl",
            root / "runs_anon" / run_id / "transcript.jsonl",
            root / "projects" / "*" / "runs" / run_id / "transcript.jsonl",
            root / run_id / "transcript.jsonl",
        ]
        if project_id:
            patterns.insert(0, root / "projects" / project_id / "runs" / run_id / "transcript.jsonl")
        for pat in patterns:
            for p in glob.glob(str(pat)):
                yield Path(p)
        return

    if project_id:
        yield from (root / "projects" / project_id / "runs").glob("*/transcript.jsonl")
    yield from root.glob("runs/*/transcript.jsonl")
    yield from root.glob("runs_anon/*/transcript.jsonl")
    yield from root.glob("projects/*/runs/*/transcript.jsonl")
    yield from root.glob("*/transcript.jsonl")


def _latest_transcript(args: argparse.Namespace) -> Path | None:
    if args.path:
        p = args.path.expanduser()
        return p if p.exists() else None
    cands: list[Path] = []
    for root in _roots(args.home):
        cands.extend(p for p in _candidate_transcripts(root, args.project_id, args.run_id) if p.exists())
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def _clean_text(text: Any) -> str:
    s = "" if text is None else str(text)
    s = re.sub(r'<span class="emoji [^"]+"></span>', "", s)
    # If a hook/system block was echoed before the real answer, keep only the
    # visible answer after the reasoning delimiter. Do this before deleting the
    # delimiter; otherwise the hook prefix and answer become indistinguishable.
    if any(tag in s for tag in HOOK_TAGS):
        if "</think>" in s:
            tail = s.rsplit("</think>", 1)[1].strip()
            if tail:
                s = tail
        for marker in ("响应", "Response:", "回答：", "回答:"):
            if marker in s:
                s = s.split(marker, 1)[1]
                break
    s = s.replace("</think>", "")
    s = re.sub(r"\s+", " ", s).strip()
    if _is_hook_only(s):
        return ""
    return s


def _is_hook_only(text: str) -> bool:
    if not text:
        return True
    if not any(tag in text for tag in HOOK_TAGS):
        return False
    # Common echoed hook blocks have no actual action after the instructions.
    hook_phrases = (
        "DO NOT QUOTE THIS BLOCK",
        "Use it only to choose the next tool/action",
        "例行路线复盘",
        "简单自检",
        "repetition_lock detected",
        "Do not output another hook block",
    )
    return any(phrase in text for phrase in hook_phrases)


def _short(value: Any, max_chars: int) -> str:
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    else:
        text = "" if value is None else str(value)
    text = _clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " ...[truncated]"


def _tool_result_summary(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    for key in ("stdout_tail", "stderr_tail", "error", "content", "status"):
        val = result.get(key)
        if val:
            return val
    return result


def _print_event(event: dict[str, Any], *, show_hooks: bool, max_chars: int) -> None:
    ev = event.get("event")
    turn = event.get("turn", "?")

    if ev == "llm_response":
        text = _clean_text(event.get("content_preview"))
        if text:
            print(f"\n──── Turn {turn} ────")
            print(_short(text, max_chars))
        calls = event.get("tool_calls") or []
        for call in calls:
            name = call.get("name")
            args = call.get("args_preview") or call.get("args") or ""
            print(f"  ▶ planned {name}({_short(args, max_chars // 2)})")
    elif ev == "tool_call":
        print(f"  ▶ {event.get('name')}({_short(event.get('args'), max_chars // 2)})")
    elif ev == "tool_result":
        print(f"  ✓ {event.get('name')}: {_short(_tool_result_summary(event.get('result_preview')), max_chars)}")
    elif ev == "tool_exception":
        print(f"  ✗ {event.get('tool_name')}: {event.get('exc_type')}: {event.get('exc_msg')}")
    elif ev == "hook_injection" and show_hooks:
        previews = event.get("previews") or []
        print(f"  ⚙ hook turn={turn}: {_short(previews, max_chars)}")
    elif ev == "run_end":
        print("\n==== RUN END ====")
        print(f"status={event.get('status')} missing={event.get('missing_required_outputs')}")
        if event.get("state_dir"):
            print(f"state={event.get('state_dir')}")
    elif ev == "repro_snapshot_saved":
        print(f"  ⧉ repro_snapshot_saved run={event.get('run_id')}")


def _follow(path: Path, args: argparse.Namespace) -> None:
    print(f"watching: {path}", flush=True)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        while True:
            line = f.readline()
            if not line:
                if args.no_follow:
                    break
                time.sleep(args.wait)
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            _print_event(event, show_hooks=args.show_hooks, max_chars=args.max_chars)
            sys.stdout.flush()


def main() -> int:
    args = _parse_args()
    path = _latest_transcript(args)
    while path is None and not args.no_follow:
        print("waiting for transcript.jsonl ...", file=sys.stderr, flush=True)
        time.sleep(max(args.wait, 1.0))
        path = _latest_transcript(args)
    if path is None:
        print("No transcript.jsonl found. Pass --home, --project-id, --run-id, or --path.", file=sys.stderr)
        return 2
    _follow(path, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
