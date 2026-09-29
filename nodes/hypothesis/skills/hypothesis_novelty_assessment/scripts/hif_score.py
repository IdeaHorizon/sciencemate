"""HIF CLI — thin wrapper around nodes.hypothesis.tools.hif_core."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[5]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nodes.hypothesis.tools.hif_core import (  # noqa: E402
    HIFDimensions,
    compute_hif,
    score_from_dict,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute HIF (Hypothesis Innovation Formula) score")
    parser.add_argument("--g", type=int, help="Gap coverage 0-5")
    parser.add_argument("--d", type=int, help="Conceptual departure 0-5")
    parser.add_argument("--m", type=int, help="Mechanism novelty 0-5")
    parser.add_argument("--p", type=int, help="Predictive surprise 0-5")
    parser.add_argument("--q", type=int, help="Plausibility 0-5 (gate: Q<=1 rejects)")
    parser.add_argument("--i", type=int, help="Impact 0-5 (extended formula)")
    parser.add_argument("--json", type=str, help="JSON file: single object or list of cases")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    if args.json:
        with open(args.json, encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            results = []
            for i, case in enumerate(payload):
                try:
                    res = score_from_dict(case)
                    entry = res.to_dict()
                    if "name" in case:
                        entry["name"] = case["name"]
                    results.append(entry)
                except (ValueError, KeyError) as e:
                    results.append({"index": i, "error": str(e), "input": case})
            out = results
        else:
            out = score_from_dict(payload).to_dict()
            if "name" in payload:
                out["name"] = payload["name"]
        print(json.dumps(out, indent=2 if args.pretty else None, ensure_ascii=False))
        return 0

    required = (args.g, args.d, args.m, args.p, args.r)
    if any(v is None for v in required):
        parser.error("Provide --g --d --m --p --r, or --json")

    result = compute_hif(
        HIFDimensions(
            G=args.g, D=args.d, M=args.m, P=args.p, R=args.r,
            Q=args.q, I=args.i,
        )
    )
    print(
        f"HIF={result.hif} tier={result.tier} "
        f"paradigm_flag={result.paradigm_flag} "
        f"plausibility_reject={result.plausibility_reject}"
    )
    if result.caps_applied:
        print(f"caps_applied={result.caps_applied}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
