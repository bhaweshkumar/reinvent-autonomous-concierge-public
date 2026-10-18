"""Tool-invocation runtime shared by every MCP tool group (SPEC-18 §18.1).

``_ToolRunner`` holds construction, the uniform ``_run_tool`` audit wrapper, and the
private lookups every tool group needs. The tool groups themselves (``tools_catalog``,
``tools_schedule``, ``tools_preferences``, ``tools_plans``) are mixins over this base;
``agent.tools.MCPAgent`` composes all of them into the concrete class every surface uses.
"""

import logging
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    Optional,
    Protocol,
    Type,
    TypeVar,
)
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ..audit import AuditLogger
from ..booking.approvals import ChangeSetApprovals
from ..booking.changes import PlanChange
from ..booking.reader import ScheduleReader, ScheduleSource
from ..config import Config
from ..exceptions import RateLimitError
from ..ingestion import IngestionService
from ..models import AttendeeSchedule, Session
from ..planner import (
    ApplyResult,
    CandidateBuilder,
    ItineraryPlanner,
    ItineraryPlanStore,
    PlanApplicationService,
    PreferenceProfileStore,
)
from ..schedule import ConflictManager
from ..transit import TransitEngine
from ..util.time import event_zone
from ..waitlist import WaitlistStore
from .audit_policy import audit_arguments, audit_result

logger = logging.getLogger(__name__)

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class ScheduleAndCatalogClient(ScheduleSource, Protocol):
    """What ``_ToolRunner`` itself calls directly on ``client``: the real
    ``AWSEventsRestClient`` in live mode, a ``SimulatedScheduleClient`` in the DEMO profile
    (SPEC-21)."""

    async def get_session(self, session_id: str) -> Session: ...


class _ToolRunner:
    """Construction, audited dispatch, and lookups shared by every tool group."""

    def __init__(
        self,
        config: Config,
        client: ScheduleAndCatalogClient,
        ingestion: IngestionService,
        transit: TransitEngine,
        conflict_manager: ConflictManager,
        audit: AuditLogger,
        waitlist: WaitlistStore,
        preference_profiles: Optional[PreferenceProfileStore] = None,
        candidate_builder: Optional[CandidateBuilder] = None,
        itinerary_planner: Optional[ItineraryPlanner] = None,
        itinerary_plans: Optional[ItineraryPlanStore] = None,
        plan_application: Optional[PlanApplicationService] = None,
        *,
        approvals: Optional[ChangeSetApprovals] = None,
        reader: Optional[ScheduleReader] = None,
    ):
        self.config = config
        self.client = client
        self.ingestion = ingestion
        self.transit = transit
        self.conflict_manager = conflict_manager
        self.audit = audit
        self.waitlist = waitlist
        self.preference_profiles = preference_profiles
        self.candidate_builder = candidate_builder
        self.itinerary_planner = itinerary_planner
        self.itinerary_plans = itinerary_plans
        self.plan_application = plan_application
        self.approvals = approvals
        self.reader = reader

    async def _run_tool(
        self,
        tool_name: str,
        input_model: Type[InputT],
        arguments: Dict[str, Any],
        handler: Callable[[InputT], Awaitable[OutputT]],
    ) -> OutputT:
        dry_run = not self.config.write_access_active
        session_id = arguments.get("session_id")
        audit_arguments_ = audit_arguments(tool_name, arguments)
        try:
            result = await handler(input_model(**arguments))
        except RateLimitError as exc:
            # No output contract carries a THROTTLED field (SPEC-20 §20.3 deviation: adding one
            # to every tool's Literal["..."] status would break the schema-stability guarantee
            # from SPEC-18 AC2). The exception's message already names retry_after; the audit
            # trail distinguishes throttling from a real failure.
            details = f"{type(exc).__name__}: {exc}"
            logger.warning("%s throttled: %s", tool_name, details)
            self.audit.record(
                tool_name,
                session_id=session_id,
                status="THROTTLED",
                arguments=audit_arguments_,
                details=details,
                dry_run=dry_run,
            )
            raise
        except Exception as exc:
            details = f"{type(exc).__name__}: {exc}"
            token = arguments.get("approval_token")
            if token:  # e.g. a validation error echoes its input; tokens never reach the log
                details = details.replace(str(token), "[REDACTED]")
            logger.error("%s failed: %s", tool_name, details)
            self.audit.record(
                tool_name,
                session_id=session_id,
                status="FAILED",
                arguments=audit_arguments_,
                details=details,
                dry_run=dry_run,
            )
            raise
        self.audit.record(
            tool_name,
            session_id=session_id,
            status=getattr(result, "status", "OK"),
            arguments=audit_arguments_,
            result=audit_result(result),
            dry_run=dry_run,
        )
        return result

    async def _lookup_session(self, session_id: str) -> Session:
        """Local catalog first (no API quota), GetSession as fallback."""
        return await self.ingestion.get_session_local(session_id) or await self.client.get_session(
            session_id
        )

    async def _schedule(
        self, include_favorites: bool = True, include_personal_time: bool = True
    ) -> AttendeeSchedule:
        return await self.client.get_schedule(
            include_favorites=include_favorites,
            include_personal_time=include_personal_time,
            session_resolver=self.ingestion.get_session_local,
        )

    def _preference_store(self) -> PreferenceProfileStore:
        if self.preference_profiles is None:
            raise RuntimeError("preference profiles require a configured planner state store")
        return self.preference_profiles

    def _zone(self) -> ZoneInfo:
        return event_zone(self.config.event_timezone, "America/Los_Angeles")

    def _event_id(self, event_id: Optional[str]) -> str:
        return event_id or self.config.aws_events_event_id

    def _itinerary_services(self) -> tuple[CandidateBuilder, ItineraryPlanner, ItineraryPlanStore]:
        if (
            self.candidate_builder is None
            or self.itinerary_planner is None
            or self.itinerary_plans is None
        ):
            raise RuntimeError("itinerary planning requires configured local planner services")
        return self.candidate_builder, self.itinerary_planner, self.itinerary_plans

    def _write_path(self) -> tuple[ChangeSetApprovals, ScheduleReader]:
        if self.approvals is None or self.reader is None:
            raise RuntimeError("schedule writes require the configured approval write path")
        return self.approvals, self.reader

    async def _must_attend(self) -> FrozenSet[str]:
        """The active profile's must-attend sessions, read fresh for every decision."""
        if self.preference_profiles is None:
            return frozenset()
        profile = await self.preference_profiles.get(self.config.aws_events_event_id)
        return frozenset(profile.must_attend_session_ids) if profile else frozenset()

    async def _apply_now(self, changes: list[PlanChange]) -> ApplyResult:
        approvals, _reader = self._write_path()
        return await approvals.apply_immediately(self.config.aws_events_event_id, changes)

    def _application_service(self) -> PlanApplicationService:
        if self.plan_application is None:
            raise RuntimeError("plan application requires configured planner services")
        return self.plan_application
