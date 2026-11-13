"""HIF (Hypothesis Innovation Formula) core — shared by CLI script and score_hypothesis_innovation tool."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

WEIGHTS_LEGACY = {"G": 0.25, "D": 0.20, "M": 0.25, "P": 0.20, "N": 0.10}
WEIGHTS_EXTENDED = {"G": 0.22, "D": 0.18, "M": 0.22, "P": 0.18, "N": 0.10, "I": 0.10}

TIER_THRESHOLDS: list[tuple[int, str]] = [
    (80, "transformative"),
    (65, "high"),
    (45, "moderate"),
    (25, "low"),
    (0, "minimal"),
]

TIER_LABELS_ZH = {
    "minimal": "极低",
    "low": "低",
    "moderate": "中",
    "high": "高",
    "transformative": "颠覆性",
}


@dataclass(frozen=True)
class HIFDimensions:
    G: int
    D: int
    M: int
    P: int
    R: int
    Q: int | None = None  # Plausibility 0-5 (Co-Scientist Reflection gate)
    I: int | None = None  # Impact 0-5 (Co-Scientist expert rubric)

    def __post_init__(self) -> None:
        for name in ("G", "D", "M", "P", "R"):
            val = getattr(self, name)
            if not isinstance(val, int) or not 0 <= val <= 5:
                raise ValueError(f"{name} must be integer 0-5, got {val!r}")
        for name in ("Q", "I"):
            val = getattr(self, name)
            if val is not None and (not isinstance(val, int) or not 0 <= val <= 5):
                raise ValueError(f"{name} must be integer 0-5 or None, got {val!r}")

    @property
    def N(self) -> int:
        return 5 - self.R


@dataclass(frozen=True)
class HIFResult:
    dimensions: HIFDimensions
    N: int
    raw_weighted: float
    hif: int
    tier: str
    tier_zh: str
    paradigm_flag: bool
    plausibility_reject: bool
    caps_applied: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dimensions"] = asdict(self.dimensions)
        return d


def tier_from_hif(hif: int) -> str:
    for threshold, label in TIER_THRESHOLDS:
        if hif >= threshold:
            return label
    return "minimal"


def compute_hif(d: HIFDimensions) -> HIFResult:
    n = d.N
    caps: list[str] = []
    plausibility_reject = d.Q is not None and d.Q <= 1

    if d.I is not None:
        raw = (
            WEIGHTS_EXTENDED["G"] * d.G
            + WEIGHTS_EXTENDED["D"] * d.D
            + WEIGHTS_EXTENDED["M"] * d.M
            + WEIGHTS_EXTENDED["P"] * d.P
            + WEIGHTS_EXTENDED["N"] * n
            + WEIGHTS_EXTENDED["I"] * d.I
        )
    else:
        raw = (
            WEIGHTS_LEGACY["G"] * d.G
            + WEIGHTS_LEGACY["D"] * d.D
            + WEIGHTS_LEGACY["M"] * d.M
            + WEIGHTS_LEGACY["P"] * d.P
            + WEIGHTS_LEGACY["N"] * n
        )

    hif = round(100 * raw / 5)
    paradigm_flag = d.M >= 4 and d.P >= 4 and n >= 3

    if plausibility_reject:
        caps.append("plausibility_reject")
        if hif > 24:
            caps.append("plausibility_cap_24")
        hif = min(hif, 24)

    if n <= 1:
        if hif > 40:
            caps.append("redundancy_cap_40")
        hif = min(hif, 40)

    if d.G <= 1 and d.D <= 2:
        if hif > 35:
            caps.append("no_gap_low_departure_cap_35")
        hif = min(hif, 35)

    tier = tier_from_hif(hif)
    if plausibility_reject:
        tier = "minimal"

    return HIFResult(
        dimensions=d,
        N=n,
        raw_weighted=raw,
        hif=hif,
        tier=tier,
        tier_zh=TIER_LABELS_ZH.get(tier, tier),
        paradigm_flag=paradigm_flag and not plausibility_reject,
        plausibility_reject=plausibility_reject,
        caps_applied=caps,
    )


def score_from_dict(data: dict[str, Any]) -> HIFResult:
    if "R" in data:
        r = int(data["R"])
    elif "N" in data:
        r = 5 - int(data["N"])
    else:
        raise ValueError("Need R or N in input dict")

    q = data.get("Q")
    i = data.get("I")
    return compute_hif(
        HIFDimensions(
            G=int(data["G"]),
            D=int(data["D"]),
            M=int(data["M"]),
            P=int(data["P"]),
            R=r,
            Q=int(q) if q is not None else None,
            I=int(i) if i is not None else None,
        )
    )


def render_report_markdown(assessments: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Hypothesis Innovation Report (HIF)",
        "",
        "## Summary",
        f"- assessed: {summary.get('n_assessed', 0)}",
        f"- max_hif: {summary.get('max_hif')} ({summary.get('max_label', '?')})",
        f"- min_hif: {summary.get('min_hif')} ({summary.get('min_label', '?')})",
    ]
    if summary.get("any_plausibility_reject"):
        lines.append("- ⚠️ any_plausibility_reject: true")
    lines.append("")

    for a in assessments:
        tier_zh = TIER_LABELS_ZH.get(a["tier"], a["tier"])
        lines.extend([
            f"## {a.get('label', 'hypothesis')}",
            "",
        ])
        if a.get("claim_text"):
            lines.append(f"**Claim**: {a['claim_text']}")
            lines.append("")
        dims = a.get("dimensions") or {}
        q_i = ""
        if dims.get("Q") is not None:
            q_i += f" Q={dims.get('Q')}"
        if dims.get("I") is not None:
            q_i += f" I={dims.get('I')}"
        lines.extend([
            f"- scores: G={dims.get('G')} D={dims.get('D')} M={dims.get('M')} "
            f"P={dims.get('P')} R={dims.get('R')} N={a.get('N')}{q_i}",
            f"- **HIF={a['hif']}** tier={a['tier']} ({tier_zh})",
            f"- paradigm_flag: {a.get('paradigm_flag', False)}",
            f"- plausibility_reject: {a.get('plausibility_reject', False)}",
        ])
        if a.get("caps_applied"):
            lines.append(f"- caps_applied: {a['caps_applied']}")
        if a.get("kb_overlap"):
            lines.append(f"- kb_overlap: {a['kb_overlap']}")
        if a.get("rationale"):
            lines.append(f"- rationale: {a['rationale']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
