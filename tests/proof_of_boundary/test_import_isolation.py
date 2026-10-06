# Import isolation: this template reaches the platform only through the
# framework package. A direct platform-SDK import bypasses the layer that owns
# the trust gate, the input masking and the output scan, so it is not a style
# preference — it is a hole in every guarantee the framework provides.

from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"

PROHIBITED_PREFIXES = ("agenticstar", "agents.base")
# The Python standard library also has a module called `platform`; only the
# platform SDK's own package is prohibited.
PROHIBITED_EXACT = ("agenticstar_platform",)

_PY_FILES = sorted(_SRC.rglob("*.py"))


def _module_names(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


class TestImportIsolation:
    @pytest.mark.parametrize("py_file", _PY_FILES, ids=[str(p.relative_to(_ROOT)) for p in _PY_FILES])
    def test_no_platform_sdk_import(self, py_file):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for lineno, module in _module_names(tree):
            assert not module.startswith(PROHIBITED_PREFIXES), f"{py_file.relative_to(_ROOT)}:{lineno} imports {module}"
            assert module not in PROHIBITED_EXACT, f"{py_file.relative_to(_ROOT)}:{lineno} imports {module}"

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=[str(p.relative_to(_ROOT)) for p in _PY_FILES])
    def test_no_traversal_outside_the_package(self, py_file):
        for line in py_file.read_text(encoding="utf-8").splitlines():
            assert not line.strip().startswith(
                "from .."
            ), f"{py_file.relative_to(_ROOT)}: relative import escapes the package: {line}"

    def test_no_deprecated_framework_shims(self):
        """``framework.llm`` and ``framework.security`` are deprecated no-ops in
        the released framework; the model is injected and the release checks live
        in the output node."""
        for py_file in _PY_FILES:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for lineno, module in _module_names(tree):
                assert module not in (
                    "framework.llm",
                    "framework.security",
                ), f"{py_file.relative_to(_ROOT)}:{lineno} imports the deprecated {module}"
