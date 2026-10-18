"""Composition root: builds the object graph shared by the CLI, server and worker."""

import hashlib
import logging
from dataclasses import dataclass
from typing import Literal, Optional, Union, cast

from .agent import MCPAgent
from .api import AWSEventsRestClient
from .audit import AuditLogger
from .booking import ScheduleReader
from .booking.approvals import ChangeSetApprovals
from .booking.service import ScheduleWriteService
from .config import Config
from .credentials import CredentialManager
from .demo import DemoCatalogClient, SimulatedScheduleClient
from .ingestion import Embedder, IngestionService
from .legacy_migration import (
    backup_and_drop_unused_tables,
    import_legacy_audit_log,
    import_legacy_waitlist,
)
from .planner import (
    ApplyJournalStore,
    CandidateBuilder,
    IngestionCandidateRepository,
    ItineraryPlanner,
    ItineraryPlanStore,
    OperationQuotaGovernor,
    PlanApplicationService,
    PlannerStateStore,
    PreferenceProfileStore,
    ScheduleSnapshotService,
    SQLiteOperationQuotaGovernor,
)
from .runtime import CatalogSyncJob, RuntimeCoordinator, SeatSnapperJob
from .schedule import ConflictManager
from .snapper import SeatSnapperWorker
from .transit import TransitEngine
from .util.time import is_valid_zone
from .waitlist import WaitlistStore

logger = logging.getLogger(__name__)


def _demo_token_hash() -> str:
    """Stand-in token hash for the DEMO profile, which never holds a credential to hash."""
    return hashlib.sha256(b"demo").hexdigest()


@dataclass
class AppContext:
    config: Config
    credentials: Optional[CredentialManager]
    client: Union[AWSEventsRestClient, SimulatedScheduleClient]
    catalog_client: Union[AWSEventsRestClient, DemoCatalogClient]
    planner_store: PlannerStateStore
    preference_profiles: PreferenceProfileStore
    itinerary_plans: ItineraryPlanStore
    itinerary_planner: ItineraryPlanner
    plan_application: PlanApplicationService
    approvals: ChangeSetApprovals
    reader: ScheduleReader
    operation_quota_governor: OperationQuotaGovernor
    ingestion: IngestionService
    transit: TransitEngine
    conflict_manager: ConflictManager
    audit: AuditLogger
    waitlist: WaitlistStore
    agent: MCPAgent

    def build_snapper(self) -> SeatSnapperWorker:
        return SeatSnapperWorker(
            config=self.config,
            ingestion=self.ingestion,
            conflict_manager=self.conflict_manager,
            audit=self.audit,
            waitlist=self.waitlist,
            approvals=self.approvals,
            reader=self.reader,
        )

    def build_snapper_runtime(self, interval: Optional[float] = None) -> RuntimeCoordinator:
        """The seat snapper alone, under the same event lease as ``concierge run``."""
        snapper_interval = interval or float(self.config.snapper_poll_interval_seconds)
        snapper = self.build_snapper()
        snapper.poll_interval = snapper_interval
        return RuntimeCoordinator(
            self.planner_store,
            self.config.aws_events_event_id,
            [SeatSnapperJob(snapper, snapper_interval)],
        )

    def build_runtime_coordinator(
        self, poll_interval: Optional[float] = None, catalog_interval: float = 300.0
    ) -> RuntimeCoordinator:
        """Build the one local runtime responsible for recurring background work."""
        snapper_interval = poll_interval or float(self.config.snapper_poll_interval_seconds)
        snapper = self.build_snapper()
        snapper.poll_interval = snapper_interval
        return RuntimeCoordinator(
            self.planner_store,
            self.config.aws_events_event_id,
            [
                CatalogSyncJob(self.ingestion, catalog_interval),
                SeatSnapperJob(snapper, snapper_interval),
            ],
        )

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.catalog_client is not self.client:
            await self.catalog_client.aclose()


async def create_app_context(
    config: Optional[Config] = None,
    embedder: Optional[Embedder] = None,
    profile: Literal["LIVE", "DEMO"] = "LIVE",
) -> AppContext:
    """Wire all services and prepare the local catalog.

    ``profile="DEMO"`` (SPEC-21) never constructs a ``CredentialManager`` or an
    authenticated ``AWSEventsRestClient``: catalog reads go through the hard-allowlisted,
    anonymous ``DemoCatalogClient``, and every schedule read/write goes through a
    ``SimulatedScheduleClient`` that never calls the network.
    """
    config = config or Config.from_env_or_file()
    planner_store = PlannerStateStore(config.planner_db_path)
    preference_profiles = PreferenceProfileStore(planner_store)
    itinerary_plans = ItineraryPlanStore(planner_store)
    operation_quota_governor = SQLiteOperationQuotaGovernor(
        planner_store, config.aws_events_event_id
    )

    credentials: Optional[CredentialManager]
    catalog_client: Union[AWSEventsRestClient, DemoCatalogClient]
    if profile == "DEMO":
        credentials = None
        token_hash_provider = _demo_token_hash
        catalog_client = DemoCatalogClient(config)
    else:
        credentials = CredentialManager(config.aws_events_credentials_path)
        token_hash_provider = credentials.hash_token
        catalog_client = AWSEventsRestClient(
            config, credentials, operation_quota_governor=operation_quota_governor
        )
    client: Union[AWSEventsRestClient, SimulatedScheduleClient]
    try:
        backup_and_drop_unused_tables(config.planner_db_path)
        await planner_store.initialize()
        if profile == "LIVE":
            import_legacy_waitlist(config.planner_db_path, config.waitlist_path)
            import_legacy_audit_log(config.planner_db_path, config.audit_log_path)
        ingestion = IngestionService(config, catalog_client, embedder=embedder)
        await ingestion.initialize()
        learned_zone = await ingestion.event_timezone()
        if learned_zone and not is_valid_zone(learned_zone):
            # A stale bad value must never brick startup.
            logger.warning(
                "Ignoring stored event timezone %r; using the configured zone", learned_zone
            )
        elif learned_zone and learned_zone != config.event_timezone:
            # GetEvent is the source of truth for event-local display and calendar math.
            config = config.model_copy(update={"event_timezone": learned_zone})
            ingestion.config = config
            catalog_client.config = config
        if profile == "DEMO":
            client = SimulatedScheduleClient(ingestion.get_session_local)
        else:
            client = cast(AWSEventsRestClient, catalog_client)
        transit = TransitEngine()
        itinerary_planner = ItineraryPlanner(
            transit,
            solve_timeout_seconds=config.planner_solve_timeout_seconds,
            beam_width=config.planner_beam_width,
            timezone_name=config.event_timezone,
        )
        conflict_manager = ConflictManager(
            transit, reserved_seat_lead_minutes=config.reserved_seat_lead_minutes
        )
        reader = ScheduleReader(client, ingestion.get_session_local)
        journal = ApplyJournalStore(planner_store)
        writer = ScheduleWriteService(journal, client, reader)
        snapshots = ScheduleSnapshotService(planner_store)
        approvals = ChangeSetApprovals(journal, snapshots, reader, writer)
        plan_application = PlanApplicationService(
            itinerary_plans,
            snapshots,
            journal,
            reader,
            writer,
            preference_profiles,
            catalog=ingestion,
            dry_run=not config.write_access_active,
        )
        audit = AuditLogger(config.planner_db_path, token_hash_provider)
        waitlist = WaitlistStore(config.planner_db_path)
        agent = MCPAgent(
            config,
            client,
            ingestion,
            transit,
            conflict_manager,
            audit,
            waitlist,
            preference_profiles,
            CandidateBuilder(
                IngestionCandidateRepository(ingestion),
                max_candidates=config.planner_max_candidates,
                candidate_scan_limit=config.planner_candidate_scan_limit,
            ),
            itinerary_planner,
            itinerary_plans,
            plan_application,
            approvals=approvals,
            reader=reader,
        )
        return AppContext(
            config,
            credentials,
            client,
            catalog_client,
            planner_store,
            preference_profiles,
            itinerary_plans,
            itinerary_planner,
            plan_application,
            approvals,
            reader,
            operation_quota_governor,
            ingestion,
            transit,
            conflict_manager,
            audit,
            waitlist,
            agent,
        )
    except BaseException:
        # `catalog_client` always holds the real resource (an httpx session); `client` in
        # the DEMO profile is a separate in-memory `SimulatedScheduleClient` with nothing to
        # close, and in the LIVE profile is the same object as `catalog_client`.
        await catalog_client.aclose()
        raise
