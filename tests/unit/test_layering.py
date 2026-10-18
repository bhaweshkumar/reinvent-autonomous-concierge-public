"""SPEC-18 §18.2/§18.7: regression guard for the five named layering violations.

A full import-linter contract needs a new dependency and a generalized layer map for
every module in the package, most of which the layering rule in the README never
names. Rather than invent buckets for modules the spec doesn't categorize, this checks
exactly the five violations SPEC-18 §18.2 identified and fixed, by import-depth AST
inspection (not string matching, so it is not fooled by a docstring mentioning a name).
"""

import ast
from pathlib import Path
from typing import Iterable, Set

SRC = Path(__file__).resolve().parents[2] / "src" / "reinvent_concierge"


def _imported_top_level_names(path: Path) -> Set[str]:
    """Every top-level package/module name this file imports, absolute or relative."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 1:
                # A `from ..x import y` one level above this file's own package.
                if node.module:
                    names.add(node.module.split(".")[0])
            elif node.level == 1:
                continue  # sibling import within the same package: never a violation here
            elif node.module:
                parts = node.module.split(".")
                if parts[0] == "reinvent_concierge":
                    names.add(parts[1] if len(parts) > 1 else "")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "reinvent_concierge" and len(parts) > 1:
                    names.add(parts[1])
    names.discard("")
    return names


def _files(*relative_dirs: str) -> Iterable[Path]:
    for relative in relative_dirs:
        yield from (SRC / relative).rglob("*.py")


def test_api_client_does_not_import_planner():
    """The OperationQuotaGovernor Protocol moved to api/ports.py (SPEC-18 §18.2)."""
    forbidden = {"planner"}
    for path in _files("api"):
        hit = forbidden & _imported_top_level_names(path)
        assert not hit, f"{path} imports {hit}, which api/ must not depend on"


def test_planner_does_not_import_ingestion_or_snapper():
    """The runtime coordinator moved to the top-level runtime.py (SPEC-18 §18.2)."""
    forbidden = {"ingestion", "snapper"}
    for path in _files("planner"):
        hit = forbidden & _imported_top_level_names(path)
        assert not hit, f"{path} imports {hit}, which planner/ must not depend on"


def test_ui_does_not_import_waitlist_directly():
    """The waitlist is read and changed through MCP tools now (SPEC-18 §18.4)."""
    for path in _files("ui"):
        hit = {"waitlist"} & _imported_top_level_names(path)
        assert not hit, f"{path} imports {hit}; the UI must go through MCP tools instead"


def test_audit_does_not_import_credentials():
    """AuditLogger takes an injected token-hash provider instead (SPEC-18 §18.2)."""
    path = SRC / "audit.py"
    hit = {"credentials"} & _imported_top_level_names(path)
    assert not hit, f"{path} imports {hit}; inject a token-hash provider instead"


def test_only_ui_app_uses_absolute_package_imports():
    """Streamlit runs ui/app.py and ui/demo_app.py as scripts, not as package modules, so
    only those two need absolute imports (documented at the top of each file); everything
    else is relative."""
    exempt = {SRC / "ui" / "app.py", SRC / "ui" / "demo_app.py"}
    for path in SRC.rglob("*.py"):
        if path in exempt:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert not node.module.startswith("reinvent_concierge"), (
                    f"{path} uses an absolute import of {node.module!r}; use a relative import"
                )
