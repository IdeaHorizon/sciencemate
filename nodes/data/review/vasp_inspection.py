"""Narrow VASP syntax checks shared by generation and delivery."""
from __future__ import annotations

import re


def validate_kpoints_header(content: str) -> str | None:
    """Check physical header lines without stripping standalone comments.

    https://vasp.at/wiki/KPOINTS defines one title line followed by an integer
    point count and a mode line. This is not a full k-point/physics validator;
    explicit lists, line mode and automatic meshes retain semantic review.
    """
    lines = content.splitlines()
    if len(lines) < 3 or not re.fullmatch(r"\s*[+-]?\d+\s*(?:[!#].*)?", lines[1]):
        return ("KPOINTS requires an integer point count on physical line 2, after exactly one title line. "
                "Do not prepend documentation or strip comment lines to make validation pass; "
                "put explanations in the title, legal inline comments or a separate report. "
                "Reference: https://vasp.at/wiki/KPOINTS")
    if not lines[2].strip() or lines[2].lstrip().startswith(("!", "#")):
        return "KPOINTS physical line 3 must specify the coordinate or mesh mode, not a standalone comment."
    return None
