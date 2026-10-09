"""Checks that read the platform's own source code.

S2 (spec §2.2): no numeric literal in engine code outside a named constant.
S3 (spec §2.3): a metric is implemented only in mgrm/metrics.py.
Layers (spec §3.1): a layer imports only the layers below it.
L1 at source: no bare dollar sign in any string the code could emit.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

from mgrm.checks.text import find_bare_dollars

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../mgrm

LAYER_OF_PACKAGE: dict[str, int] = {
    "data": 1,
    "rebates": 2,
    "web": 7,
    "api": 7,
    "__main__": 7,  # the command line is presentation, like the screens
}

# 0 and 1 are identities (an empty sum, a whole), not values that could go stale.
PERMITTED_BARE_NUMBERS = (0, 1)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    module: str  # e.g. "mgrm.engine.revenue"
    tree: ast.Module


def source_files(root: Path = PACKAGE_ROOT) -> list[SourceFile]:
    files = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        files.append(SourceFile(path, ".".join(parts), ast.parse(path.read_text(encoding="utf-8"), str(path))))
    return files


def layer_of(module: str) -> int | None:
    parts = module.split(".")
    return LAYER_OF_PACKAGE.get(parts[1]) if len(parts) > 1 and parts[0] == "mgrm" else None


def _is_upper_constant_assignment(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return False
    return all(isinstance(t, ast.Name) and t.id.isupper() for t in targets)


def numeric_literals_outside_constants(file: SourceFile) -> list[str]:
    """S2 for one file."""
    permitted: set[int] = set()
    for node in file.tree.body:
        if _is_upper_constant_assignment(node):
            permitted.update(id(child) for child in ast.walk(node))
    findings = []
    for node in ast.walk(file.tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float, complex))
            and not isinstance(node.value, bool)
            and id(node) not in permitted
            and node.value not in PERMITTED_BARE_NUMBERS
        ):
            findings.append(f"{file.module} line {node.lineno}: numeric literal {node.value!r} outside a named constant")
    return findings


def metric_definitions_outside_metrics_module(files: list[SourceFile], metric_ids: tuple[str, ...]) -> list[str]:
    """S3: a function or class named after a metric, anywhere but mgrm.metrics."""
    findings = []
    for file in files:
        if file.module == "mgrm.metrics":
            continue
        for node in ast.walk(file.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name.lower()
                for metric in metric_ids:
                    if metric in name:
                        findings.append(
                            f"{file.module} line {node.lineno}: '{node.name}' looks like a second definition of "
                            f"{metric}; metrics are defined only in mgrm/metrics.py"
                        )
    return findings


def _imported_modules(tree: ast.Module) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def upward_layer_imports(files: list[SourceFile]) -> list[str]:
    """A module importing from a layer above its own."""
    findings = []
    for file in files:
        own = layer_of(file.module)
        for lineno, target in _imported_modules(file.tree):
            target_layer = layer_of(target)
            # A shared package (no layer) must not reach into any layer either.
            if target_layer is not None and (own is None or target_layer > own):
                findings.append(
                    f"{file.module} line {lineno} imports {target}: layer {target_layer} is above "
                    f"{'a shared module' if own is None else f'layer {own}'}"
                )
    return findings


def _regex_pattern_ids(tree: ast.Module) -> set[int]:
    """String literals passed straight to re.compile: search patterns, never shown to anyone."""
    ids = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            ids.add(id(node.args[0]))
    return ids


def bare_dollars_in_string_literals(files: list[SourceFile]) -> list[str]:
    findings = []
    for file in files:
        patterns = _regex_pattern_ids(file.tree)
        for node in ast.walk(file.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in patterns:
                findings += [f"{file.module} line {node.lineno}: {f}" for f in find_bare_dollars(node.value)]
    return findings
