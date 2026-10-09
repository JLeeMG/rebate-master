"""Label and disclosure scanners L1, L3, L4 (spec §10.4)."""

import pytest

from mgrm.checks.text import find_bare_dollars, find_formula_like_text, find_working_markers


@pytest.mark.parametrize("text", ["Revenue $1.2m", "$268,000", "cost (in $)", "C$40"])
def test_l1_bare_dollar_is_found(text):
    assert find_bare_dollars(text)


@pytest.mark.parametrize("text", ["A$268,000", "NZ$1,763,495", "US$5,000,000", "−A$92,224", "no money here"])
def test_l1_qualified_dollar_is_clean(text):
    assert find_bare_dollars(text) == []


@pytest.mark.parametrize(
    "text",
    ["Finance charges are based on final BNZ rates [CHECK]", "[TBD]", "[ tbc ]", "[TODO: tie to TB]", "[??]"],
)
def test_l3_working_marker_is_found(text):
    assert find_working_markers(text)


def test_l3_ordinary_brackets_are_clean():
    assert find_working_markers("Rebates [note 4] and accounts [42010, 42020]") == []


def test_l4_text_beginning_with_equals_is_found():
    assert find_formula_like_text(["=SUM(H93:R93)+18664", " =A1", "EBITDA"]) == [
        "Text begins with '=': =SUM(H93:R93)+18664",
        "Text begins with '=':  =A1",
    ]
