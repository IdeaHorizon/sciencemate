#!/usr/bin/env python3
"""Start an experiment continuation run without changing framework core.

This is deliberately not a true turn-level resume. The framework's true
checkpoint resume needs a CLI/core entry point. Node owners should not patch
that layer. This helper instead:

1. Reads a previous run's summary/transcript from a chosen
   HARNESS_FRAMEWORK_HOME.
2. Builds a concise continuation directive from the last useful evidence.
3. Starts a new experiment run with the same home/project and injects that
   directive through node_inputs.

The shared HPC workdir remains the durable state for builds/runs.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _find_run_dir(home: Path, run_id_or_path: str) -> Path:
    cand = Path(run_id_or_path).expanduser()
    if cand.exists():
        return cand.resolve()

    run_id = run_id_or_path
    candidates = [
        home / "runs" / run_id,
        home / "runs_anon" / run_id,
    ]
    projects = home / "projects"
    if projects.exists():
        candidates.extend(project / "runs" / run_id for project in projects.iterdir())

    for path in candidates:
        if path.exists():
            return path.resolve()
    raise SystemExit(f"run not found under {home}: {run_id_or_path}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON: {path}: {exc}") from exc


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-limit * 4:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events[-limit:]


def _latest_logs(workdir: Path | None, limit: int) -> list[tuple[str, str]]:
    if workdir is None:
        return []
    logs_dir = workdir / "logs"
    if not logs_dir.exists():
        return []
    files = sorted(
        (p for p in logs_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    out: list[tuple[str, str]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        tail = "\n".join(lines[-80:])
        out.append((str(path), tail[-6000:]))
    return out


def _event_brief(ev: dict[str, Any]) -> str:
    event = ev.get("event", "?")
    turn = ev.get("turn")
    prefix = f"turn {turn} {event}" if turn is not None else str(event)
    if event == "tool_result":
        tool = ev.get("tool") or ev.get("name") or "tool"
        rc = ev.get("returncode")
        tail = ev.get("stderr_tail") or ev.get("stdout_tail") or ev.get("result_preview")
        return f"- {prefix}: {tool} rc={rc}; tail={str(tail or '')[:500]}"
    if event in {"llm_response", "assistant_message"}:
        text = ev.get("content") or ev.get("text") or ev.get("final_text") or ""
        return f"- {prefix}: {str(text)[:500]}"
    if event in {"artifact_saved", "run_end", "hook_injection"}:
        return f"- {prefix}: {json.dumps(ev, ensure_ascii=False, default=str)[:500]}"
    return f"- {prefix}: {json.dumps(ev, ensure_ascii=False, default=str)[:300]}"


def _build_directive(
    *,
    run_dir: Path,
    summary: dict[str, Any],
    events: list[dict[str, Any]],
    workdir: Path | None,
    logs: list[tuple[str, str]],
    extra: str,
) -> str:
    lines = [
        "Continuation run directive.",
        "",
        "This is a new run continuing a previous experiment run. Do not restart",
        "from scratch unless the evidence below is invalid. First inspect the",
        "cited prior logs/workdir, then continue from the latest blocker.",
        "",
        f"previous_run_dir: {run_dir}",
        f"previous_status: {summary.get('status', 'unknown')}",
        f"previous_missing_required_outputs: {summary.get('missing_required_outputs', [])}",
        f"previous_outputs: {[a.get('type') for a in summary.get('artifacts', []) if isinstance(a, dict)]}",
    ]
    if workdir is not None:
        lines.append(f"shared_hpc_workdir: {workdir}")
    if summary.get("final_text_preview"):
        lines.extend(["", "previous_final_text_preview:", str(summary["final_text_preview"])[:1200]])

    if events:
        lines.append("")
        lines.append("recent_transcript_events:")
        lines.extend(_event_brief(ev) for ev in events)

    if logs:
        lines.append("")
        lines.append("latest_scheduler_or_build_logs:")
        for path, tail in logs:
            lines.append(f"--- {path} ---")
            lines.append(tail)

    if extra:
        lines.extend(["", "user_continuation_instruction:", extra])

    lines.extend([
        "",
        "Rules for continuation:",
        "- Do not modify upstream application source files (.F/.F90/.c/.h/.py).",
        "- Environment/setup fixes come before configuration fixes; source edits are last resort and need human approval.",
        "- Write only within the canonical writable path roles recorded for this run; "
        "the framework state root itself is not an application workspace.",
        "- If the latest blocker is a missing build helper or include path, fix the environment/config rather than patching the official build system.",
        "- Freeze experiment_log when the run reaches a real smoke result or a well-evidenced blocker.",
    ])
    return "\n".join(lines)


def _prior_write_grants(run_dir: Path) -> list[dict[str, Any]]:
    """Directories a human approved for writing in the prior run.

    They are read from that run's ``run_manifest.json``, not from its
    ``hook_state``: hook_state is never serialized, so a scope approval lives
    only as long as the process that received it.  Within one run that is
    enough — a pause and its resume share the State object — but this helper
    starts a *new* run, where nothing carries over implicitly.
    """
    manifest = _read_json(
        run_dir / "outputs" / "experiment" / "repro" / "run_manifest.json")
    grants = manifest.get("human_write_grants") or []
    out: list[dict[str, Any]] = []
    for grant in grants:
        path = grant.get("path") if isinstance(grant, dict) else None
        if path:
            out.append({"path": path, "writable": True,
                        "cleanup": grant.get("cleanup", "contents")})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start an experiment continuation run using prior run evidence."
    )
    parser.add_argument("--home", type=Path, default=None,
                        help="HARNESS_FRAMEWORK_HOME that contains the prior run.")
    parser.add_argument("--run-id", required=True,
                        help="Prior run id or absolute prior run directory.")
    parser.add_argument("--fixture", type=Path, required=True,
                        help="Fixture for the new continuation run.")
    parser.add_argument("--project-id", required=True,
                        help="Project id for shared memory/KB.")
    parser.add_argument("--harness", default="experiment")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="Shared HPC workdir whose logs should be summarized.")
    parser.add_argument("--extra", default="",
                        help="Extra user instruction to append to the continuation directive.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the generated command/directive without running.")
    parser.add_argument("--no-interactive", action="store_true",
                        help="Pass --no-interactive through to run_node.py.")
    parser.add_argument("--carry-write-grants", action="store_true",
                        help="Re-declare the directories a human approved for "
                             "writing in the prior run, so the continuation "
                             "does not ask for each of them again. Off by "
                             "default: this starts a NEW run, and an approval "
                             "given in another run is not automatically an "
                             "approval here.")
    args = parser.parse_args()

    home = args.home or Path(os.environ.get("HARNESS_FRAMEWORK_HOME", "~/.harness-framework")).expanduser()
    home = home.resolve()
    run_dir = _find_run_dir(home, args.run_id)
    summary = _read_json(run_dir / "summary.json")
    events = _tail_jsonl(run_dir / "transcript.jsonl", 14)
    logs = _latest_logs(args.workdir.resolve() if args.workdir else None, 4)

    directive = _build_directive(
        run_dir=run_dir,
        summary=summary,
        events=events,
        workdir=args.workdir.resolve() if args.workdir else None,
        logs=logs,
        extra=args.extra,
    )
    inputs: dict[str, Any] = {"continuation_context": directive}
    grants = _prior_write_grants(run_dir) if args.carry_write_grants else []
    if grants:
        # node_inputs is the fixture/orchestration channel, which is the only
        # non-human way to declare a role.  Routing the grants through it —
        # rather than trying to restore hook_state — keeps the authority model
        # intact: the operator running this command is the one re-authorizing.
        inputs["path_roles"] = {"approved_write_root": grants}

    repo = _repo_root()
    cmd = [
        sys.executable,
        str(repo / "run_node.py"),
        "--harness",
        args.harness,
        "--fixture",
        str(args.fixture),
        "--project-id",
        args.project_id,
        "--inputs",
        json.dumps(inputs, ensure_ascii=False),
    ]
    if args.no_interactive:
        cmd.append("--no-interactive")

    env = dict(os.environ)
    env["HARNESS_FRAMEWORK_HOME"] = str(home)
    env.setdefault("HARNESS_FRAMEWORK_ORG_HOME", str(home / "org"))

    print("HARNESS_FRAMEWORK_HOME=" + str(home))
    print("continuing_from=" + str(run_dir))
    for grant in grants:
        print("carried_write_grant=" + grant["path"]
              + f" (cleanup={grant['cleanup']})")
    print("command=" + " ".join(cmd))
    if args.dry_run:
        print("\n--- continuation_context ---")
        print(directive)
        return 0

    return subprocess.run(cmd, cwd=repo, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
