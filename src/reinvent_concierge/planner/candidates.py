"""Bounded candidate-set construction from the local catalog (SPEC-18 §18.1 split).

Split out of ``itinerary.py``; ``itinerary.py`` re-exports these for backward
compatibility.
"""

from collections.abc import Sequence
from typing import Dict, List, Optional, Protocol, Tuple

from ..models import SESSION_CODE_PATTERN, Session, SessionSummary
from .models import ScheduleSnapshot
from .plan_models import (
    Candidate,
    CandidateExclusion,
    CandidateSet,
    CandidateSource,
    ReasonCode,
)
from .preferences import AttendeePreferenceProfile, SeatPolicy


class CandidateRepository(Protocol):
    async def search(self, query: str, limit: int) -> Sequence[SessionSummary]: ...

    async def get_local(self, session_id: str) -> Optional[Session]: ...

    async def list_local(self, limit: int) -> Sequence[Session]: ...


def _session_occurrence_key(session: Session) -> Optional[str]:
    if session.repeat_group:
        return session.repeat_group
    match = SESSION_CODE_PATTERN.search(f"{session.title} {session.session_id}".upper())
    return match.group(1) if match else None


def _seat_confidence(session: Session) -> int:
    band = (session.seat_availability or "").casefold()
    if session.status == "CANCELLED":
        return 0
    if session.is_reservable is False:
        return 15 if band == "walkup" else 0
    if session.capacity is not None and session.reserved_count is not None:
        if session.capacity == 0:
            return 50
        return max(
            0,
            min(
                100,
                ((session.capacity - session.reserved_count) * 100) // session.capacity,
            ),
        )
    return {"available": 100, "limited": 70, "verylimited": 35, "unavailable": 0, "walkup": 50}.get(
        band, 50
    )


class CandidateBuilder:
    """Builds a bounded candidate set from local catalog search and lookup only."""

    def __init__(
        self,
        repository: CandidateRepository,
        max_candidates: int = 250,
        candidate_scan_limit: int = 500,
    ) -> None:
        self._repository = repository
        self._max_candidates = max_candidates
        self._candidate_scan_limit = candidate_scan_limit

    async def build(
        self, profile: AttendeePreferenceProfile, snapshot: ScheduleSnapshot
    ) -> CandidateSet:
        by_id: Dict[str, Tuple[Session, set[CandidateSource], int]] = {}
        exclusions: List[CandidateExclusion] = []

        def include(session: Session, source: CandidateSource, relevance: int = 0) -> None:
            current = by_id.get(session.session_id)
            if current is None:
                by_id[session.session_id] = (session, {source}, relevance)
            else:
                current[1].add(source)
                by_id[session.session_id] = (current[0], current[1], max(current[2], relevance))

        for query in profile.interest_queries:
            results = await self._repository.search(query.query, limit=20)
            for rank, result in enumerate(results, start=1):
                session = await self._repository.get_local(result.session_id)
                if session is not None:
                    include(session, CandidateSource.INTEREST_SEARCH, max(1, 100 - (rank - 1) * 5))

        source_ids = (
            (profile.must_attend_session_ids, CandidateSource.MUST_ATTEND),
            (snapshot.booked_session_ids, CandidateSource.CURRENT_RESERVATION),
            (snapshot.favorite_session_ids, CandidateSource.FAVORITE),
        )
        for session_ids, source in source_ids:
            for session_id in sorted(session_ids):
                session = await self._repository.get_local(session_id)
                if session is None:
                    exclusions.append(
                        CandidateExclusion(
                            session_id=session_id,
                            reason_code=ReasonCode.MISSING_MUST_ATTEND
                            if source is CandidateSource.MUST_ATTEND
                            else ReasonCode.CANDIDATE_LIMIT,
                            detail="The local catalog does not contain this committed session."
                            if source is CandidateSource.MUST_ATTEND
                            else "The local catalog does not contain this scheduled session.",
                        )
                    )
                else:
                    include(session, source, 100 if source is CandidateSource.MUST_ATTEND else 0)

        for session in await self._repository.list_local(limit=self._candidate_scan_limit):
            text = " ".join(
                filter(None, (session.track, session.title, session.description))
            ).casefold()
            track_match = bool(
                profile.preferred_tracks
                & {part.strip() for part in (session.track or "").split(",")}
            )
            structured = {service.casefold() for service in session.services}
            service_match = any(
                service.casefold() in structured or service.casefold() in text
                for service in profile.preferred_services
            )
            if track_match or service_match:
                include(session, CandidateSource.PREFERRED_METADATA, 0)

        admitted: List[Candidate] = []
        for session_id, (session, sources, relevance) in sorted(by_id.items()):
            if session_id in profile.avoid_session_ids:
                exclusions.append(
                    CandidateExclusion(
                        session_id=session_id,
                        reason_code=ReasonCode.AVOIDED,
                        detail="Explicitly avoided by the profile.",
                    )
                )
                continue
            if session.status == "CANCELLED":
                exclusions.append(
                    CandidateExclusion(
                        session_id=session_id,
                        reason_code=ReasonCode.CANCELLED,
                        detail="The catalog marks this session as cancelled.",
                    )
                )
                continue
            if not self._seat_allowed(session, profile.seat_policy):
                exclusions.append(
                    CandidateExclusion(
                        session_id=session_id,
                        reason_code=ReasonCode.SEAT_POLICY,
                        detail="Excluded by the configured seat policy.",
                    )
                )
                continue
            preferred = self._preferred_fit(session, profile)
            admitted.append(
                Candidate(
                    session=session,
                    sources=tuple(sorted(sources, key=lambda item: item.value)),
                    relevance_score=relevance,
                    preferred_fit_score=preferred,
                    seat_confidence_score=_seat_confidence(session),
                    occurrence_key=_session_occurrence_key(session),
                )
            )

        commitments = [
            candidate for candidate in admitted if CandidateSource.MUST_ATTEND in candidate.sources
        ]
        others = [candidate for candidate in admitted if candidate not in commitments]
        others.sort(key=lambda item: (-self._candidate_priority(item), item.session_id))
        kept = commitments + others[: max(0, self._max_candidates - len(commitments))]
        kept_ids = {candidate.session_id for candidate in kept}
        for candidate in admitted:
            if candidate.session_id not in kept_ids:
                exclusions.append(
                    CandidateExclusion(
                        session_id=candidate.session_id,
                        reason_code=ReasonCode.CANDIDATE_LIMIT,
                        detail="Not retained in the bounded candidate set.",
                    )
                )
        warnings = (
            ()
            if len(commitments) <= self._max_candidates
            else (
                "Must-attend commitments exceed the configured candidate bound and were preserved.",
            )
        )
        return CandidateSet(
            candidates=tuple(sorted(kept, key=lambda item: item.session_id)),
            exclusions=tuple(
                sorted(
                    exclusions,
                    key=lambda item: (item.reason_code.value, item.session_id),
                )
            ),
            warnings=warnings,
        )

    @staticmethod
    def _seat_allowed(session: Session, policy: SeatPolicy) -> bool:
        if policy is SeatPolicy.ANY:
            return True
        if policy is SeatPolicy.INCLUDE_WALK_UP:
            return session.has_open_seat or (session.seat_availability or "").casefold() == "walkup"
        return session.has_open_seat

    @staticmethod
    def _preferred_fit(session: Session, profile: AttendeePreferenceProfile) -> int:
        score = 0
        if profile.preferred_levels and session.level in profile.preferred_levels:
            score += 35
        tracks = {part.strip() for part in (session.track or "").split(",") if part.strip()}
        if profile.preferred_tracks & tracks:
            score += 35
        text = " ".join(
            filter(None, (session.title, session.description, session.track))
        ).casefold()
        structured = {service.casefold() for service in session.services}
        if any(
            service.casefold() in structured or service.casefold() in text
            for service in profile.preferred_services
        ):
            score += 30
        return min(100, score)

    @staticmethod
    def _candidate_priority(candidate: Candidate) -> int:
        return (
            candidate.relevance_score * 5
            + candidate.preferred_fit_score * 3
            + candidate.seat_confidence_score
        )


class IngestionCatalog(Protocol):
    async def search(
        self,
        query: str,
        level: Optional[int] = None,
        venue: Optional[str] = None,
        hero_only: bool = False,
        limit: int = 5,
    ) -> Sequence[SessionSummary]: ...

    async def get_session_local(self, session_id: str) -> Optional[Session]: ...

    async def list_local_sessions(self, limit: int) -> Sequence[Session]: ...


class IngestionCandidateRepository:
    """Adapter that constrains planner reads to the local ingestion catalog."""

    def __init__(self, ingestion: IngestionCatalog) -> None:
        self._ingestion = ingestion

    async def search(self, query: str, limit: int) -> Sequence[SessionSummary]:
        return await self._ingestion.search(query=query, limit=limit)

    async def get_local(self, session_id: str) -> Optional[Session]:
        return await self._ingestion.get_session_local(session_id)

    async def list_local(self, limit: int) -> Sequence[Session]:
        return await self._ingestion.list_local_sessions(limit=limit)
