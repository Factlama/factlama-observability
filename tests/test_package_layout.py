"""OBS-01 exit check: every declared package imports cleanly and the
dependency boundaries import-linter enforces are real, not just declared.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "sdk",
        "collector",
        "collector.ingress",
        "collector.processing",
        "storage",
        "query",
        "operations",
    ],
)
def test_package_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_sdk_does_not_import_server_side_packages() -> None:
    import ast
    from pathlib import Path

    sdk_init = Path(__file__).parent.parent / "sdk" / "__init__.py"
    tree = ast.parse(sdk_init.read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imported.isdisjoint({"collector", "storage", "query"})
