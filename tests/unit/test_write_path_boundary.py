"""SPEC-17 §17.5 (1): only booking/ and api/ may call client write methods."""

import ast
import subprocess
from pathlib import Path
from typing import List

import pytest

WRITE_METHODS = {
    "reserve_sessions",
    "cancel_reservation",
    "associate_favorites",
    "disassociate_favorite",
    "create_personal_time",
    "update_personal_time",
    "delete_personal_time",
}
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "reinvent_concierge"
PRE_SPEC_17_COMMIT = "47d2713"


# The one exemption: the MCP server's `agent.cancel_reservation(...)` calls the approval-gated
# tool, not the client. Exact file, method and bare receiver name only.
EXEMPT_FILE = Path("agent") / "server.py"
EXEMPT_METHOD = "cancel_reservation"
EXEMPT_RECEIVER = "agent"


def _is_agent_tool_call(relpath: Path, func: ast.Attribute) -> bool:
    return (
        relpath == EXEMPT_FILE
        and func.attr == EXEMPT_METHOD
        and isinstance(func.value, ast.Name)
        and func.value.id == EXEMPT_RECEIVER
    )


def _violations(root: Path) -> List[str]:
    allowed = {root / "booking", root / "api"}
    found: List[str] = []
    for path in root.rglob("*.py"):
        if any(a in path.parents for a in allowed):
            continue
        relpath = path.relative_to(root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_METHODS
                and not _is_agent_tool_call(relpath, node.func)
            ):
                found.append(f"{relpath}:{node.lineno} {node.func.attr}")
    return found


def test_only_booking_and_api_call_write_methods():
    assert _violations(SRC) == []


def _write_module(root: Path, relpath: str, body: str) -> None:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"async def f(agent, client):\n    {body}\n", encoding="utf-8")


def test_real_server_cancel_tool_call_is_exempt(tmp_path):
    _write_module(tmp_path, "agent/server.py", "await agent.cancel_reservation('x')")
    assert _violations(tmp_path) == []


def test_other_write_method_on_agent_in_server_is_flagged(tmp_path):
    _write_module(tmp_path, "agent/server.py", "await agent.reserve_sessions(['x'])")
    assert _violations(tmp_path) == ["agent/server.py:2 reserve_sessions"]


def test_agent_cancel_outside_the_server_is_flagged(tmp_path):
    _write_module(tmp_path, "agent/tools.py", "await agent.cancel_reservation('x')")
    assert _violations(tmp_path) == ["agent/tools.py:2 cancel_reservation"]


def test_server_cancel_on_another_receiver_is_flagged(tmp_path):
    _write_module(tmp_path, "agent/server.py", "await client.cancel_reservation('x')")
    assert _violations(tmp_path) == ["agent/server.py:2 cancel_reservation"]


def test_checker_flags_write_calls_outside_booking_and_api(tmp_path):
    (tmp_path / "schedule").mkdir()
    (tmp_path / "booking").mkdir()
    (tmp_path / "schedule" / "bad.py").write_text(
        "async def f(client):\n    await client.cancel_reservation('x')\n", encoding="utf-8"
    )
    (tmp_path / "booking" / "ok.py").write_text(
        "async def f(client):\n    await client.cancel_reservation('x')\n", encoding="utf-8"
    )
    assert _violations(tmp_path) == ["schedule/bad.py:2 cancel_reservation"]


@pytest.mark.parametrize(
    "relpath",
    ["schedule/conflicts.py", "snapper/worker.py"],
)
def test_checker_flags_pre_spec_17_modules(tmp_path, relpath):
    """Evidence the check can fail: the pre-SPEC-17 modules call write methods directly."""
    try:
        source = subprocess.run(  # noqa: S603
            ["git", "show", f"{PRE_SPEC_17_COMMIT}:src/reinvent_concierge/{relpath}"],  # noqa: S607
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pytest.skip("pre-SPEC-17 history not available")
    target = tmp_path / relpath
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    assert _violations(tmp_path)
