"""SPEC-09 runtime lease, independent scheduling, and graceful shutdown behavior."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
import truststore
from click.testing import CliRunner

from reinvent_concierge import cli as cli_module
from reinvent_concierge.app_context import create_app_context
from reinvent_concierge.planner import PlannerStateStore
from reinvent_concierge.runtime import (
    RuntimeCoordinator,
    RuntimeLeaseError,
    SeatSnapperJob,
)
from tests.factories import FakeEmbedder


class RecordingJob:
    def __init__(self, name: str, interval_seconds: float, failure: bool = False) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.failure = failure
        self.calls = 0

    async def run_once(self) -> None:
        self.calls += 1
        if self.failure:
            raise RuntimeError("expected test failure")


async def test_second_runtime_lease_is_rejected_without_stopping_first(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    first = RuntimeCoordinator(store, config.aws_events_event_id)
    second = RuntimeCoordinator(store, config.aws_events_event_id)

    await first.start()
    try:
        with pytest.raises(RuntimeLeaseError, match="Runtime already running"):
            await second.start()
        assert first._started is True
    finally:
        await first.aclose()

    await second.start()
    await second.aclose()


async def test_jobs_schedule_independently_and_isolate_failures(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    fast = RecordingJob("fast", 0.01)
    slow = RecordingJob("slow", 0.03)
    failing = RecordingJob("failing", 0.01, failure=True)
    coordinator = RuntimeCoordinator(store, config.aws_events_event_id, [fast, slow, failing])

    await coordinator.start()
    await asyncio.sleep(0.07)
    await coordinator.aclose()

    assert fast.calls > slow.calls >= 1
    assert failing.calls >= 1


async def test_close_waits_for_active_job_cycle(config):
    store = PlannerStateStore(config.planner_db_path)
    await store.initialize()
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingJob:
        name = "blocking"
        interval_seconds = 60.0

        async def run_once(self) -> None:
            entered.set()
            await release.wait()

    coordinator = RuntimeCoordinator(store, config.aws_events_event_id, [BlockingJob()])
    await coordinator.start()
    await entered.wait()
    closing = asyncio.create_task(coordinator.aclose())
    await asyncio.sleep(0)
    assert not closing.done()

    release.set()
    await closing


async def test_snapper_command_takes_the_runtime_lease(config):
    context = await create_app_context(config, embedder=FakeEmbedder())
    try:
        first = context.build_snapper_runtime(3600)
        second = context.build_snapper_runtime(3600)
        [job] = first.jobs
        assert isinstance(job, SeatSnapperJob) and job.interval_seconds == 3600

        await first.start()
        try:
            with pytest.raises(RuntimeLeaseError, match="Runtime already running"):
                await second.start()
        finally:
            await first.aclose()
    finally:
        await context.aclose()


def test_snapper_command_reports_a_held_lease(monkeypatch):
    coordinator = Mock()
    coordinator.start = AsyncMock(side_effect=RuntimeLeaseError("Runtime already running"))
    coordinator.aclose = AsyncMock()
    context = Mock()
    context.build_snapper_runtime.return_value = coordinator
    context.aclose = AsyncMock()

    async def fake_context():
        return context

    monkeypatch.setattr(cli_module, "create_app_context", fake_context)
    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: None)

    result = CliRunner().invoke(cli_module.cli, ["snapper", "--interval", "7"])

    assert result.exit_code != 0
    assert "Error: Runtime already running" in result.output
    context.build_snapper_runtime.assert_called_once_with(7)
    coordinator.aclose.assert_awaited_once()
    context.aclose.assert_awaited_once()


def test_snapper_command_rejects_a_non_positive_interval(monkeypatch):
    async def must_not_start():
        raise AssertionError("an invalid interval must be rejected before startup")

    # Never let a CLI test reach the real app home.
    monkeypatch.setattr(cli_module, "create_app_context", must_not_start)
    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: None)

    result = CliRunner().invoke(cli_module.cli, ["snapper", "--interval", "0"])

    assert result.exit_code == 2
    assert "Invalid value for '--interval'" in result.output
