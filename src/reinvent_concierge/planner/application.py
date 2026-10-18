"""Controlled, journaled application of immutable itinerary plans (SPEC-12)."""

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import (
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    cast,
)
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

# ApplyResult, ChangeKind and PlanChange are re-exported from here for one release.
from ..booking.approvals import (
    APPROVAL_TTL,
    ApprovalRejectedError,
    apply_result,
    as_utc,
    compensation_incomplete,
    digest,
    execute_and_settle,
    new_token,
    token_matches,
)
from ..booking.changes import (
    ApplyResult,
    ChangeKind,
    PlanChange,
)
from ..booking.reader import ScheduleReader
from ..booking.service import ScheduleWriteService
from ..booking.transit_blocks import (
    is_concierge_transit,
    same_route,
    transit_description,
)
from ..models import (
    AttendeeSchedule,
    PersonalTimeBlock,
    Session,
    utc_now,
)
from .apply_journal import ApplyJournalStore, InvalidStateTransition
from .itinerary import (
    ItineraryPlan,
    ItineraryPlanStore,
    PlanItem,
    PlanItemKind,
)
from .models import (
    ApplyRun,
    ApplyRunState,
)
from .preferences import AttendeePreferenceProfile, PreferenceProfileStore
from .snapshots import ScheduleSnapshotService


class ApplicationMode(str, Enum):
    PREVIEW_ONLY = "PREVIEW_ONLY"
    FAVORITES_ONLY = "FAVORITES_ONLY"
    RESERVE_NON_CONFLICTING = "RESERVE_NON_CONFLICTING"
    APPLY_APPROVED_PLAN = "APPLY_APPROVED_PLAN"


RequiredValueT = TypeVar("RequiredValueT")

_HANDS_ON_MARKERS = ("workshop", "hands-on", "builders", "lab")


class CatalogLookup(Protocol):
    """Local catalog reads needed to order reservations by scarcity."""

    async def get_session_local(self, session_id: str) -> Optional[Session]: ...

    async def repeat_group_counts(self, groups: Sequence[str]) -> Dict[str, int]: ...


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanPreview(_Model):
    apply_run_id: UUID
    plan_id: UUID
    requested_mode: ApplicationMode
    effective_mode: ApplicationMode
    source_fingerprint: str
    plan_fingerprint: str
    changes: Tuple[PlanChange, ...]
    risk_text: Tuple[str, ...]
    quota_wait_seconds: int = Field(ge=0)
    expires_at: datetime
    approval_token: str = Field(repr=False)


class PreviewPayload(_Model):
    plan_id: str
    requested_mode: str
    effective_mode: Optional[str] = None
    source_fingerprint: Optional[str] = None
    plan_fingerprint: str
    approval_digest: str
    expires_at: datetime
    changes: Tuple[Dict[str, JsonValue], ...] = ()


class PlanApplicationService:
    """Creates a bound preview, applies it once, and reconciles the AWS source of truth."""

    def __init__(
        self,
        itinerary_plans: ItineraryPlanStore,
        snapshots: ScheduleSnapshotService,
        journal: ApplyJournalStore,
        reader: ScheduleReader,
        writer: ScheduleWriteService,
        profiles: PreferenceProfileStore,
        *,
        catalog: CatalogLookup,
        dry_run: bool,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._plans = itinerary_plans
        self._snapshots = snapshots
        self._journal = journal
        self._reader = reader
        self._writer = writer
        self._profiles = profiles
        self._dry_run = dry_run
        self._now = now
        self._catalog = catalog

    async def preview(self, plan_id: UUID | str, mode: ApplicationMode) -> PlanPreview:
        plan = await self._plan(plan_id)
        profile = await self._profile(plan)
        schedule = await self._schedule()
        snapshot = await self._snapshots.capture(plan.event_id, schedule)
        effective_mode = self._effective_mode(mode)
        scarcity = await self._scarcity(plan)
        changes = self._changes(plan, profile, schedule, scarcity)
        token = new_token()
        expires_at = as_utc(self._now()) + APPROVAL_TTL
        run = await self._journal.create_run(
            plan.event_id, profile.profile_id, snapshot.fingerprint, plan.plan_id
        )
        payload = PreviewPayload(
            plan_id=str(plan.plan_id),
            requested_mode=mode.value,
            effective_mode=effective_mode.value,
            source_fingerprint=snapshot.fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            approval_digest=digest(token),
            expires_at=expires_at,
            changes=tuple(
                cast(Dict[str, JsonValue], change.model_dump(mode="json")) for change in changes
            ),
        )
        await self._journal.save_preview_payload(
            run.run_id,
            cast(Dict[str, JsonValue], payload.model_dump(mode="json")),
        )
        return PlanPreview(
            apply_run_id=run.run_id,
            plan_id=plan.plan_id,
            requested_mode=mode,
            effective_mode=effective_mode,
            source_fingerprint=snapshot.fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            changes=changes,
            risk_text=tuple(sorted({change.risk for change in changes if change.risk})),
            quota_wait_seconds=0,
            expires_at=expires_at,
            approval_token=token,
        )

    async def apply(
        self, plan_id: UUID | str, mode: ApplicationMode, approval_token: str
    ) -> ApplyResult:
        plan = await self._plan(plan_id)
        run, payload = await self._matching_preview(plan, mode, approval_token)
        changes = tuple(PlanChange.model_validate(item) for item in payload.changes)
        await self._check_must_attend(plan, changes)  # rejects without consuming the token
        current = await self._snapshots.capture(plan.event_id, await self._schedule())
        source_fingerprint = self._required(payload.source_fingerprint, "source_fingerprint")
        if current.fingerprint != source_fingerprint:
            await self._journal.transition_run(run.run_id, "PREVIEWED", "STALE")
            raise ApprovalRejectedError("schedule changed after preview; create a new preview")
        try:
            await self._journal.transition_run(run.run_id, "PREVIEWED", "APPROVED")
            await self._journal.transition_run(run.run_id, "APPROVED", "APPLYING")
        except InvalidStateTransition as error:
            raise ApprovalRejectedError("approval token was already consumed") from error

        executable_changes = tuple(
            self._executable_changes(
                changes,
                ApplicationMode(self._required(payload.effective_mode, "effective_mode")),
            )
        )
        return await execute_and_settle(
            self._journal, self._writer, run.run_id, executable_changes, allow_destructive=True
        )

    async def _check_must_attend(self, plan: ItineraryPlan, changes: Sequence[PlanChange]) -> None:
        """Re-read the active profile: a release previewed earlier may now be must-attend."""
        profile = await self._profiles.get(plan.event_id)
        if profile is None or profile.profile_id != plan.profile_id:
            raise ApprovalRejectedError(
                "the preference profile changed after preview; create a new preview"
            )
        must_attend = set(profile.must_attend_session_ids)
        released: Set[str] = set()
        for change in changes:
            if change.kind is ChangeKind.SWAP:
                released.update(change.blocking_session_ids)
            elif change.kind is ChangeKind.CANCEL and change.session_id is not None:
                released.add(change.session_id)
        held = sorted(released & must_attend)
        if held:
            raise ApprovalRejectedError(
                f"{', '.join(held)} is now must-attend and is never released; create a new preview"
            )

    async def status(self, apply_run_id: UUID | str) -> ApplyResult:
        return await apply_result(self._journal, apply_run_id)

    async def resume_recovery(self, apply_run_id: UUID | str, approval_token: str) -> ApplyResult:
        run = await self._journal.get_run(UUID(str(apply_run_id)))
        if run is None:
            raise ValueError(f"apply run {apply_run_id} was not found")
        if run.plan_id is None:
            raise ApprovalRejectedError("only itinerary plan runs can be recovered here")
        payload = await self._journal.preview_payload(run.run_id)
        if payload is None:
            raise ApprovalRejectedError("approval token does not match this apply run")
        try:
            preview = PreviewPayload.model_validate(payload)
        except ValidationError:  # from None: the raw error would echo the payload
            raise ApprovalRejectedError("preview payload is unavailable") from None
        if not token_matches(approval_token, preview.approval_digest):
            raise ApprovalRejectedError("approval token does not match this apply run")
        await self._writer.reconcile_unknown(run.run_id)
        if run.state == "APPLYING":
            await self._settle_recovered(run.run_id, preview)
        return await self.status(run.run_id)

    async def _settle_recovered(self, run_id: UUID, preview: PreviewPayload) -> None:
        """Finish a run left APPLYING (e.g. the process stopped) from the reconciled journal."""
        if compensation_incomplete(await self._journal.operations(run_id)):
            await self._journal.transition_run(run_id, "APPLYING", "COMPENSATING")
            await self._journal.transition_run(run_id, "COMPENSATING", "COMPENSATION_INCOMPLETE")
            return
        mode = ApplicationMode(self._required(preview.effective_mode, "effective_mode"))
        changes = tuple(
            self._executable_changes(
                tuple(PlanChange.model_validate(item) for item in preview.changes), mode
            )
        )
        await self._journal.transition_run(run_id, "APPLYING", "VERIFYING")
        refused, unexpected = await self._writer.verify(run_id, changes)
        target: ApplyRunState = "APPLIED" if not refused and not unexpected else "PARTIAL"
        await self._journal.transition_run(run_id, "VERIFYING", target)

    async def _plan(self, plan_id: UUID | str) -> ItineraryPlan:
        plan = await self._plans.get(plan_id)
        if plan is None:
            raise ValueError(f"itinerary plan {plan_id} was not found")
        return plan

    async def _profile(self, plan: ItineraryPlan) -> AttendeePreferenceProfile:
        profile = await self._profiles.get(plan.event_id)
        if profile is None or profile.profile_id != plan.profile_id:
            raise ValueError("the active preference profile does not match the itinerary plan")
        return profile

    async def _scarcity(self, plan: ItineraryPlan) -> Dict[str, Tuple[int, int]]:
        """Map each target session to (offerings in its repeat group, 0 if hands-on else 1)."""
        session_ids = sorted(
            {
                item.session_id
                for day in plan.day_items
                for item in day
                if item.kind is PlanItemKind.SESSION and item.session_id is not None
            }
        )
        sessions: Dict[str, Session] = {}
        for session_id in session_ids:
            session = await self._catalog.get_session_local(session_id)
            if session is not None:
                sessions[session_id] = session
        groups = sorted({s.repeat_group for s in sessions.values() if s.repeat_group})
        counts = await self._catalog.repeat_group_counts(groups) if groups else {}
        return {
            session_id: (
                counts.get(session.repeat_group, 1) if session.repeat_group else 1,
                0 if self._hands_on(session) else 1,
            )
            for session_id, session in sessions.items()
        }

    @staticmethod
    def _hands_on(session: Session) -> bool:
        texts = [session.session_type or "", *session.features]
        return any(marker in text.casefold() for text in texts for marker in _HANDS_ON_MARKERS)

    async def _schedule(self) -> AttendeeSchedule:
        return await self._reader.read()

    def _effective_mode(self, requested: ApplicationMode) -> ApplicationMode:
        if not self._dry_run:
            return requested
        return (
            ApplicationMode.PREVIEW_ONLY
            if requested is ApplicationMode.PREVIEW_ONLY
            else ApplicationMode.FAVORITES_ONLY
        )

    async def _matching_preview(
        self, plan: ItineraryPlan, mode: ApplicationMode, token: str
    ) -> Tuple[ApplyRun, PreviewPayload]:
        run = await self._journal.latest_preview_for_plan(plan.plan_id)
        if run is None:
            raise ApprovalRejectedError("no unconsumed preview exists for this plan")
        payload = await self._journal.preview_payload(run.run_id)
        if payload is None:
            raise ApprovalRejectedError("preview payload is unavailable")
        typed = PreviewPayload.model_validate(payload)
        if typed.plan_id != str(plan.plan_id):
            raise ApprovalRejectedError("approval is bound to another plan")
        if typed.plan_fingerprint != plan.plan_fingerprint:
            raise ApprovalRejectedError("plan changed after preview")
        if typed.requested_mode != mode.value:
            raise ApprovalRejectedError("approval is bound to another application mode")
        if not token_matches(token, typed.approval_digest):
            raise ApprovalRejectedError("approval token is invalid")
        now = as_utc(self._now())
        expires_at = as_utc(typed.expires_at)
        if expires_at < now or expires_at > now + APPROVAL_TTL:
            await self._journal.transition_run(run.run_id, "PREVIEWED", "STALE")
            raise ApprovalRejectedError("approval token has expired")
        return run, typed

    @staticmethod
    def _required(value: Optional[RequiredValueT], field_name: str) -> RequiredValueT:
        if value is None:
            raise ValueError(f"{field_name} is required for this plan change")
        return value

    def _changes(
        self,
        plan: ItineraryPlan,
        profile: AttendeePreferenceProfile,
        schedule: AttendeeSchedule,
        scarcity: Optional[Dict[str, Tuple[int, int]]] = None,
    ) -> Tuple[PlanChange, ...]:
        scarcity = scarcity or {}
        target_items = tuple(
            item
            for day in plan.day_items
            for item in day
            if item.kind is PlanItemKind.SESSION and item.session_id is not None
        )
        target_ids = {item.session_id for item in target_items if item.session_id}
        booked = {session.session_id: session for session in schedule.booked}
        favorite_ids = {session.session_id for session in schedule.favorites}
        favorites: List[PlanChange] = []
        reservations: List[PlanChange] = []
        changes: List[PlanChange] = []
        for item in sorted(target_items, key=lambda value: value.session_id or ""):
            if item.session_id is None:
                raise ValueError("session plan item is missing session_id")
            if item.session_id not in favorite_ids:
                favorites.append(
                    PlanChange(
                        kind=ChangeKind.FAVORITE,
                        session_id=item.session_id,
                        risk="Adds a local schedule favorite; no reservation is changed.",
                    )
                )
            if item.session_id in booked:
                continue
            blockers = tuple(
                session.session_id
                for session in booked.values()
                if self._overlaps(item, session)
                and session.session_id not in profile.must_attend_session_ids
            )
            must_blockers = [
                session.session_id
                for session in booked.values()
                if self._overlaps(item, session)
                and session.session_id in profile.must_attend_session_ids
            ]
            if must_blockers:
                reservations.append(
                    PlanChange(
                        kind=ChangeKind.RESERVE,
                        session_id=item.session_id,
                        risk=(
                            "Not reserved automatically because it conflicts with "
                            f"must-attend {', '.join(sorted(must_blockers))}."
                        ),
                    )
                )
            elif blockers:
                reservations.append(
                    PlanChange(
                        kind=ChangeKind.SWAP,
                        session_id=item.session_id,
                        blocking_session_ids=tuple(sorted(blockers)),
                        risk=(
                            "Releases an approved reservation before one reservation "
                            "attempt; restoration is best effort only."
                        ),
                    )
                )
            else:
                reservations.append(
                    PlanChange(
                        kind=ChangeKind.RESERVE,
                        session_id=item.session_id,
                        risk="Reserves once without cancelling an existing reservation.",
                    )
                )
        cancels: List[PlanChange] = []
        reservations.sort(key=lambda change: self._reservation_key(change, scarcity))
        for session_id in sorted(set(booked) - target_ids - set(profile.must_attend_session_ids)):
            if not any(session_id in change.blocking_session_ids for change in reservations):
                cancels.append(
                    PlanChange(
                        kind=ChangeKind.CANCEL,
                        session_id=session_id,
                        risk=("Cancels an approved reservation that is outside the selected plan."),
                    )
                )
        creates: List[PlanChange] = []
        removes: List[PlanChange] = []
        desired_transit = [
            item for day in plan.day_items for item in day if item.kind is PlanItemKind.TRANSIT
        ]
        existing_owned = [item for item in schedule.personal_time if is_concierge_transit(item)]
        for item in desired_transit:
            if not any(self._same_transit(item, existing) for existing in existing_owned):
                creates.append(
                    PlanChange(
                        kind=ChangeKind.CREATE_TRANSIT,
                        title=item.title,
                        start_time=item.start_time,
                        end_time=item.end_time,
                        location=item.venue_id,
                        description=self._transit_description(plan, item),
                        risk=(
                            "Creates a concierge-owned transit block; personal time "
                            "created by the attendee is preserved."
                        ),
                    )
                )
        for existing in existing_owned:
            if not any(self._same_transit(item, existing) for item in desired_transit):
                removes.append(
                    PlanChange(
                        kind=ChangeKind.REMOVE_TRANSIT,
                        personal_time_id=existing.id,
                        risk=(
                            "Removes only an obsolete transit block previously created "
                            "by the concierge."
                        ),
                    )
                )
        changes = [*favorites, *reservations, *cancels, *creates, *removes]
        return tuple(changes)

    @staticmethod
    def _reservation_key(
        change: PlanChange, scarcity: Dict[str, Tuple[int, int]]
    ) -> Tuple[int, int, str]:
        session_id = change.session_id or ""
        offerings, hands_on = scarcity.get(session_id, (1, 1))
        return (offerings, hands_on, session_id)

    @staticmethod
    def _overlaps(item: PlanItem, session: Session) -> bool:
        return item.start_time < session.end_time and session.start_time < item.end_time

    @staticmethod
    def _same_transit(item: PlanItem, block: PersonalTimeBlock) -> bool:
        return (
            same_route(item.title, block.title)
            and item.start_time == block.start_time
            and item.end_time == block.end_time
        )

    def _transit_description(self, plan: ItineraryPlan, item: PlanItem) -> str:
        return transit_description(plan.plan_fingerprint, item.title)

    @staticmethod
    def _executable_changes(
        changes: Sequence[PlanChange], mode: ApplicationMode
    ) -> Iterable[PlanChange]:
        if mode is ApplicationMode.PREVIEW_ONLY:
            return ()
        if mode is ApplicationMode.FAVORITES_ONLY:
            return (change for change in changes if change.kind is ChangeKind.FAVORITE)
        if mode is ApplicationMode.RESERVE_NON_CONFLICTING:
            return (
                change
                for change in changes
                if change.kind in {ChangeKind.FAVORITE, ChangeKind.RESERVE}
            )
        return changes
