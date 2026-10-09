"""Label and disclosure scanners (spec §10.4) for any generated text.

Each returns a list of findings; an empty list means clean. They are applied
to every rendered screen (tests/test_web.py) and, from Phase 5, to every
exported cell before the export is allowed to write.
"""

import re

# L1: a dollar sign not immediately preceded by A, NZ or US.
BARE_DOLLAR = re.compile(r"(?<!A)(?<!NZ)(?<!US)\$")

# L3: working markers such as [CHECK], [TBD], [TODO], [??].
WORKING_MARKER = re.compile(
    r"\[\s*(?:CHECK|TBD|TBC|TODO|FIXME|XXX+|CONFIRM|PLACEHOLDER|INSERT|\?+)[^\]]*\]",
    re.IGNORECASE,
)


def _context(text: str, start: int, end: int, width: int = 30) -> str:
    return text[max(0, start - width) : end + width].replace("\n", " ")


def find_bare_dollars(text: str) -> list[str]:
    return [f"Bare dollar sign: ...{_context(text, m.start(), m.end())}..." for m in BARE_DOLLAR.finditer(text)]


def find_working_markers(text: str) -> list[str]:
    return [f"Working marker {m.group(0)}: ...{_context(text, m.start(), m.end())}..." for m in WORKING_MARKER.finditer(text)]


def find_formula_like_text(cells: list[str]) -> list[str]:
    """L4: a text cell beginning with '=' would be read by Excel as a formula."""
    return [f"Text begins with '=': {cell[:60]}" for cell in cells if cell.lstrip().startswith("=")]


def scan_generated_text(text: str) -> list[str]:
    return find_bare_dollars(text) + find_working_markers(text)
