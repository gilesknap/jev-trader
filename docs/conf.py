"""Sphinx configuration, following the DLS Python Copier template layout.

Build locally (in any copy, private or public) with:

    uv run --group docs sphinx-build -W --keep-going docs build/html

The library-features table and the source map are generated from the code at build time
(into docs/_generated/, which git ignores), so they can't drift from it.
"""

import ast
import os
from pathlib import Path

# The CLI reference prints argument defaults. Pin the standard install layout, so the
# docs don't show the build machine's checkout path. Set before trader is imported.
os.environ.setdefault("TRADER_STRATEGIST_ROOT", "/srv/trading/strategist")

project = "jev-trader"
author = "Giles Knap"
copyright = "2026, Giles Knap"
extensions = [
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_design",
    "sphinxcontrib.mermaid",
    "sphinxarg.ext",
]
myst_enable_extensions = ["colon_fence"]
myst_heading_anchors = 3
myst_fence_as_directive = ["mermaid"]
nitpicky = True
exclude_patterns = ["_build", "_generated"]
html_theme = "pydata_sphinx_theme"
html_title = project
html_theme_options = {
    "logo": {"text": project},
    "github_url": "https://github.com/gilesknap/jev-trader",
    "use_edit_page_button": True,
    "navbar_end": ["theme-switcher", "icon-links"],
    "show_toc_level": 2,
}
html_context = {
    "github_user": "gilesknap",
    "github_repo": "jev-trader",
    "github_version": "main",
    "doc_path": "docs",
}

HERE = Path(__file__).parent
SRC = HERE.parent / "src" / "trader"
GENERATED = HERE / "_generated"


def _features_table() -> str:
    from trader import features as F

    rows = ["| Feature | What it measures |", "|---|---|"]
    for name in sorted(F.REGISTRY):
        doc = (F.REGISTRY[name].__doc__ or "").strip().splitlines()
        rows.append(f"| `{name}` | {doc[0] if doc else ''} |")
    return "\n".join(rows) + "\n"


def _source_map() -> str:
    rows = ["| Module | What it does |", "|---|---|"]
    for path in sorted(SRC.rglob("*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text()))
        if not doc:
            continue
        first = " ".join(doc.split("\n\n")[0].split())
        rel = path.relative_to(SRC.parent).as_posix()
        url = f"https://github.com/gilesknap/jev-trader/blob/main/src/{rel}"
        rows.append(f"| [`{rel}`]({url}) | {first.replace('|', '/')} |")
    return "\n".join(rows) + "\n"


def _write_generated(app) -> None:
    GENERATED.mkdir(exist_ok=True)
    for name, text in (("features.md", _features_table()), ("source-map.md", _source_map())):
        path = GENERATED / name
        if not path.exists() or path.read_text() != text:  # unchanged files don't force a rebuild
            path.write_text(text)


def setup(app):
    app.connect("builder-inited", _write_generated)
