"""``_ToolRunner._run_tool``'s audit wrapper, including the SPEC-20 §20.3 THROTTLED path."""

import pytest
from pydantic import BaseModel

from reinvent_concierge.agent.runner import _ToolRunner
from reinvent_concierge.exceptions import RateLimitError
from tests.factories import read_audit


class _Input(BaseModel):
    session_id: str = "S1"


async def _raise_rate_limit(_params: _Input) -> None:
    raise RateLimitError(12.5)


async def _raise_value_error(_params: _Input) -> None:
    raise ValueError("boom")


def _runner(config, audit, waitlist) -> _ToolRunner:
    return _ToolRunner(
        config=config,
        client=None,
        ingestion=None,
        transit=None,
        conflict_manager=None,
        audit=audit,
        waitlist=waitlist,
    )


async def test_rate_limit_error_is_audited_as_throttled_and_reraised(config, audit, waitlist):
    runner = _runner(config, audit, waitlist)

    with pytest.raises(RateLimitError):
        await runner._run_tool("fake_tool", _Input, {"session_id": "S1"}, _raise_rate_limit)

    entries = read_audit(config.planner_db_path)
    assert entries[-1]["action"] == "[DRY_RUN] fake_tool"  # config.write_access_active defaults off
    assert entries[-1]["status"] == "THROTTLED"
    assert "12.5" in entries[-1]["details"]


async def test_other_errors_are_still_audited_as_failed(config, audit, waitlist):
    runner = _runner(config, audit, waitlist)

    with pytest.raises(ValueError):
        await runner._run_tool("fake_tool", _Input, {"session_id": "S1"}, _raise_value_error)

    entries = read_audit(config.planner_db_path)
    assert entries[-1]["status"] == "FAILED"
