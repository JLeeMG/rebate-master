"""The layer rule and L1 at source, over the rebate master's own code."""

import ast
from pathlib import Path

from mgrm.checks.code_scan import SourceFile, bare_dollars_in_string_literals, source_files, upward_layer_imports

ALL_FILES = source_files()


def planted(module: str, code: str) -> SourceFile:
    return SourceFile(Path(f"{module}.py"), module, ast.parse(code))


def test_scanner_reaches_the_whole_package():
    modules = {f.module for f in ALL_FILES}
    assert {"mgrm.data", "mgrm.rebates", "mgrm.web", "mgrm.api"} <= modules and len(ALL_FILES) > 15


def test_layer_detector_catches_an_upward_import():
    assert upward_layer_imports([planted("mgrm.data.registers", "from mgrm.rebates import service\n")])
    assert upward_layer_imports([planted("mgrm.domain.thing", "import mgrm.web.app\n")])


def test_layers_import_only_downwards():
    assert upward_layer_imports(ALL_FILES) == []


def test_no_bare_dollar_in_any_string_the_code_can_emit():
    assert bare_dollars_in_string_literals([planted("mgrm.x", "LABEL = 'Rebate $'")])
    assert bare_dollars_in_string_literals(ALL_FILES) == []
