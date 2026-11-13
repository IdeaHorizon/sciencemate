"""The article skeleton shared by manuscript rendering and toolchain probes."""
from pathlib import Path


def render_template(renderer: str, language: str) -> str:
    template = (Path(__file__).parent / renderer / "main.tex.tmpl").read_text(encoding="utf-8")
    english = str(language).strip().lower() in {"en", "english"}
    values = {
        "DOCUMENT_CLASS": (r"\documentclass[12pt,a4paper]{article}" if english
                           else r"\documentclass[scheme=chinese,12pt,a4paper]{ctexart}"),
        "SECTION_FORMAT": ("" if english else
                           r"\ctexset{section/format=\Large\bfseries,"
                           r"subsection/format=\large\bfseries,"
                           r"subsubsection/format=\normalsize\bfseries}"),
        "REFERENCES_TITLE": "References" if english else "参考文献",
    }
    for key, value in values.items():
        template = template.replace(f"<<{key}>>", value)
    return template
