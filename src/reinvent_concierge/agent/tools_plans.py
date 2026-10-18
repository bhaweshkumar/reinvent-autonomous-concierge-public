"""Itinerary plan generation, comparison and application tools (SPEC-18 §18.1 split)."""

from typing import List, Optional

from ..planner import (
    ApplicationMode,
    ApplyResult,
    ItineraryPlan,
    PlanPreview,
    PlanSet,
    PlanStrategy,
    ScheduleSnapshot,
)
from .contracts import (
    ApplyItineraryPlanInput,
    CompareItineraryPlansInput,
    CompareItineraryPlansOutput,
    ExplainPlanChoiceInput,
    ExplainPlanChoiceOutput,
    GenerateItineraryPlansInput,
    GetApplyStatusInput,
    GetItineraryPlanInput,
    PreviewItineraryApplicationInput,
    ResumeApplyRecoveryInput,
)
from .runner import _ToolRunner


class PlansToolsMixin(_ToolRunner):
    # ------------------------------------------------------------------ tool 12

    async def generate_itinerary_plans(
        self,
        event_id: str,
        profile_id: str,
        strategies: Optional[List[PlanStrategy]] = None,
    ) -> PlanSet:
        """Generate immutable itinerary alternatives without changing the AWS schedule."""
        return await self._run_tool(
            "generate_itinerary_plans",
            GenerateItineraryPlansInput,
            {"event_id": event_id, "profile_id": profile_id, "strategies": strategies},
            self._generate_itinerary_plans,
        )

    async def _generate_itinerary_plans(self, params: GenerateItineraryPlansInput) -> PlanSet:
        profile = await self._preference_store().get(params.event_id)
        if profile is None:
            raise ValueError(f"no preference profile exists for event {params.event_id}")
        if str(profile.profile_id) != params.profile_id:
            raise ValueError("profile_id does not match the active event preference profile")
        builder, planner, plan_store = self._itinerary_services()
        # This is a source-of-truth read only. CandidateBuilder itself remains local-only.
        snapshot = ScheduleSnapshot.from_schedule(params.event_id, await self._schedule())
        candidates = await builder.build(profile, snapshot)
        plan_set = await planner.plan(candidates, profile, snapshot, params.strategies)
        await plan_store.save_plan_set(plan_set)
        return plan_set

    # ------------------------------------------------------------------ tool 13

    async def get_itinerary_plan(self, plan_id: str) -> ItineraryPlan:
        """Fetch one immutable locally persisted itinerary plan."""
        return await self._run_tool(
            "get_itinerary_plan",
            GetItineraryPlanInput,
            {"plan_id": plan_id},
            self._get_itinerary_plan,
        )

    async def _get_itinerary_plan(self, params: GetItineraryPlanInput) -> ItineraryPlan:
        _builder, _planner, plan_store = self._itinerary_services()
        plan = await plan_store.get(params.plan_id)
        if plan is None:
            raise ValueError(f"itinerary plan {params.plan_id} was not found")
        return plan

    # ------------------------------------------------------------------ tool 14

    async def compare_itinerary_plans(self, plan_ids: List[str]) -> CompareItineraryPlansOutput:
        """Compare persisted plan alternatives without regenerating or mutating a schedule."""
        return await self._run_tool(
            "compare_itinerary_plans",
            CompareItineraryPlansInput,
            {"plan_ids": plan_ids},
            self._compare_itinerary_plans,
        )

    async def _compare_itinerary_plans(
        self, params: CompareItineraryPlansInput
    ) -> CompareItineraryPlansOutput:
        _builder, _planner, plan_store = self._itinerary_services()
        plans = []
        for plan_id in params.plan_ids:
            plan = await plan_store.get(plan_id)
            if plan is None:
                raise ValueError(f"itinerary plan {plan_id} was not found")
            plans.append(plan)
        if len({plan.plan_set_id for plan in plans}) != 1:
            raise ValueError("only plans from the same generated plan set can be compared")
        return CompareItineraryPlansOutput(
            plans=plans,
            comparison={
                str(plan.plan_id): (
                    f"Score {plan.total_score}; transit penalty "
                    f"{plan.score_components.transit_and_venue_penalty}/100; seat confidence "
                    f"{plan.score_components.seat_confidence}/100"
                )
                for plan in plans
            },
        )

    # ------------------------------------------------------------------ tool 15

    async def explain_plan_choice(
        self, plan_id: str, session_id: Optional[str] = None
    ) -> ExplainPlanChoiceOutput:
        """Return deterministic score and reason-code evidence for a plan or session."""
        return await self._run_tool(
            "explain_plan_choice",
            ExplainPlanChoiceInput,
            {"plan_id": plan_id, "session_id": session_id},
            self._explain_plan_choice,
        )

    async def _explain_plan_choice(self, params: ExplainPlanChoiceInput) -> ExplainPlanChoiceOutput:
        plan = await self._get_itinerary_plan(GetItineraryPlanInput(plan_id=params.plan_id))
        selected = list(plan.selected_explanations)
        excluded = list(plan.top_excluded_reasons)
        if params.session_id:
            selected = [item for item in selected if item.session_id == params.session_id]
            excluded = [item for item in excluded if item.session_id == params.session_id]
            if not selected and not excluded:
                raise ValueError(
                    f"session {params.session_id} is not part of this plan's explanation"
                )
        return ExplainPlanChoiceOutput(
            plan=plan,
            selected_explanations=[item.model_dump(mode="json") for item in selected],
            excluded_reasons=[item.model_dump(mode="json") for item in excluded],
        )

    # ------------------------------------------------------------------ tool 16

    async def preview_itinerary_application(
        self, plan_id: str, mode: ApplicationMode
    ) -> PlanPreview:
        return await self._run_tool(
            "preview_itinerary_application",
            PreviewItineraryApplicationInput,
            {"plan_id": plan_id, "mode": mode},
            lambda params: self._application_service().preview(params.plan_id, params.mode),
        )

    async def apply_itinerary_plan(
        self, plan_id: str, mode: ApplicationMode, approval_token: str
    ) -> ApplyResult:
        return await self._run_tool(
            "apply_itinerary_plan",
            ApplyItineraryPlanInput,
            {"plan_id": plan_id, "mode": mode, "approval_token": approval_token},
            lambda params: self._application_service().apply(
                params.plan_id, params.mode, params.approval_token
            ),
        )

    async def get_apply_status(self, apply_run_id: str) -> ApplyResult:
        return await self._run_tool(
            "get_apply_status",
            GetApplyStatusInput,
            {"apply_run_id": apply_run_id},
            lambda params: self._application_service().status(params.apply_run_id),
        )

    async def resume_apply_recovery(self, apply_run_id: str, approval_token: str) -> ApplyResult:
        return await self._run_tool(
            "resume_apply_recovery",
            ResumeApplyRecoveryInput,
            {"apply_run_id": apply_run_id, "approval_token": approval_token},
            lambda params: self._application_service().resume_recovery(
                params.apply_run_id, params.approval_token
            ),
        )
