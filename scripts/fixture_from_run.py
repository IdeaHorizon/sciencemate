"""Generate a fixture YAML from a real run.

Use case: 同事在 sandbox 跑出满意结果 / 排查回归用例时，想把当时的 input
（node_inputs + upstream artifacts + memory）冻成一个可复跑的 fixture，留给后人用。

用法：
    python scripts/fixture_from_run.py <run_id> --output nodes/<x>/fixtures/<name>.yaml

  或显式指定 home：
    python scripts/fixture_from_run.py <run_id> --home /tmp/hf-sandbox-... --output ...

启发：
  - 从 ~/.harness-framework/runs/<run_id>/summary.json 拿 node_type / node_inputs
  - 从 ~/.harness-framework/runs/<run_id>/artifacts/ 拿上游 artifact（filter to required_input_artifact_types if known）
  - 从 transcript.jsonl 反推 memory entries（开始时 inject 的那些）
  - 输出符合 fixture schema 的 YAML（跟 nodes/<x>/fixtures/minimal.yaml 同格式）

注意：
  - 默认会跳过非 frozen 的 transient artifact（fixture 一般留稳定的）
  - chunk_then_drop 类（experiment_log）会保留（fixture 需要它喂下游）
  - 真大 artifact 内容会被截到 8000 chars（避免 yaml 文件爆炸）—— 加 --full 跳过截断
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from core.paths import runs_root


_TRUNCATE_AT = 8000     # max chars per artifact content in fixture


def _read_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _truncate(s: str, full: bool) -> str:
    if full or not s or len(s) <= _TRUNCATE_AT:
        return s
    return s[:_TRUNCATE_AT] + f"\n\n# ... [truncated at {_TRUNCATE_AT} chars; pass --full to keep all] ..."


def build_fixture(run_id: str, *, home_override: Path | None = None,
                   full: bool = False) -> dict:
    if home_override:
        os.environ["HARNESS_FRAMEWORK_HOME"] = str(home_override)
    rd = runs_root() / run_id
    if not rd.exists():
        raise FileNotFoundError(f"run dir not found: {rd}")

    summary = _read_json(rd / "summary.json") or {}
    node_type = summary.get("node_type", "unknown")
    project_id = summary.get("project_id")

    # node_inputs: try summary then transcript first user message
    node_inputs = summary.get("node_inputs") or {}
    if not node_inputs:
        transcript = _read_jsonl(rd / "transcript.jsonl")
        for ev in transcript:
            if ev.get("event") == "node_inputs_received":
                node_inputs = ev.get("inputs") or {}
                break

    # upstream artifacts (from run's artifacts/)
    upstream_artifacts: list[dict] = []
    art_dir = rd / "artifacts"
    if art_dir.exists():
        for art_file in sorted(art_dir.glob("*.json")):
            rec = _read_json(art_file) or {}
            meta = rec.get("metadata") or {}
            upstream_artifacts.append({
                "type": rec.get("type"),
                "name": rec.get("name"),
                "content": _truncate(rec.get("content", ""), full),
                **({"metadata": meta} if meta else {}),
            })

    # memory entries injected at start: 取 transcript 中早期 add_memory 调用结果
    memory_entries: list[dict] = []
    for ev in _read_jsonl(rd / "transcript.jsonl"):
        if ev.get("event") == "tool_call_start" and ev.get("tool_name") == "add_memory":
            args = ev.get("tool_args") or {}
            if args.get("kind") in ("directive", "observation", "decision"):
                memory_entries.append({
                    "kind": args["kind"],
                    "text": args.get("text", ""),
                    **({"tags": args["tags"]} if args.get("tags") else {}),
                    **({"applies_to_node": args["applies_to_node"]}
                       if args.get("applies_to_node") else {}),
                })
        # 截短，避免 100 条 directive 全进
        if len(memory_entries) >= 8:
            break

    fixture = {
        "_generated_from_run": run_id,
        "_generated_node_type": node_type,
    }
    if project_id:
        fixture["project_id"] = project_id
    if node_inputs:
        fixture["node_inputs"] = node_inputs
    fixture["upstream_artifacts"] = upstream_artifacts
    fixture["memory_entries"] = memory_entries
    return fixture


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_id", help="~/.harness-framework/runs/<run_id>/")
    ap.add_argument("--home", type=Path, default=None,
                     help="HARNESS_FRAMEWORK_HOME 覆盖（如 sandbox 路径）")
    ap.add_argument("--output", "-o", type=Path, default=None,
                     help="输出 yaml 路径；缺省打到 stdout")
    ap.add_argument("--full", action="store_true",
                     help="不截短 artifact content（默认截到 8000 字符）")
    args = ap.parse_args()

    try:
        fx = build_fixture(args.run_id, home_override=args.home, full=args.full)
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    out_text = yaml.safe_dump(fx, allow_unicode=True, sort_keys=False,
                                default_flow_style=False, width=100)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out_text, encoding="utf-8")
        print(f"✓ fixture 写到 {args.output}")
        print(f"  node_type: {fx.get('_generated_node_type')}")
        print(f"  artifacts: {len(fx.get('upstream_artifacts', []))}")
        print(f"  memory:    {len(fx.get('memory_entries', []))}")
        print()
        print(f"试跑：python run_node.py --harness {fx.get('_generated_node_type')} "
              f"--sandbox --fixture {args.output}")
    else:
        sys.stdout.write(out_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
