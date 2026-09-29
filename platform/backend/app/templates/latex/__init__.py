"""LaTeX template loader.

Templates are .tex files in this directory. Each template defines the
document class, packages, and section structure for a target venue.

To add a new template:
  1. Create a .tex file (e.g., nature.tex, acs_nano.tex)
  2. Use the same TEMPLATE: markers as base.tex
  3. Reference by name in project config: latex_template: "nature"
"""

from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent


def load_template(name: str = "base") -> str:
    """Load a LaTeX template by name. Returns the full .tex content."""
    path = TEMPLATE_DIR / f"{name}.tex"
    if not path.exists():
        path = TEMPLATE_DIR / "base.tex"
    return path.read_text()


def list_templates() -> list[str]:
    """List available template names."""
    return [p.stem for p in TEMPLATE_DIR.glob("*.tex")]
