"""The leads package must stay pure: standard library only, no I/O or frameworks."""

import ast
from pathlib import Path

import pytest

import leadflow_ai.leads

LEADS_DIR = Path(leadflow_ai.leads.__file__).parent
ALLOWED_TOP_LEVEL_IMPORTS = {
    "collections",
    "dataclasses",
    "enum",
    "re",
    "typing",
    "unicodedata",
    "leadflow_ai",
}
FORBIDDEN_CALLS = {"open", "print", "input", "exec", "eval", "__import__"}
SOURCE_FILES = sorted(LEADS_DIR.glob("*.py"))


def test_source_files_found() -> None:
    names = {path.name for path in SOURCE_FILES}
    assert {"validation.py", "phones.py", "emails.py", "text.py", "models.py"} <= names


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda path: path.name)
def test_only_allowed_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            top_level = module.split(".")[0]
            assert top_level in ALLOWED_TOP_LEVEL_IMPORTS, f"{path.name} imports {module}"
            if top_level == "leadflow_ai":
                assert module.startswith("leadflow_ai.leads"), f"{path.name} imports {module}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda path: path.name)
def test_no_io_calls(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN_CALLS, f"{path.name} calls {node.func.id}()"
