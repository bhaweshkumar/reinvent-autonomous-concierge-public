"""Streamlit UI for re:Invent Autonomous Concierge (SPEC-08).

Tool calls go to the local FastMCP server over SSE (start it with
``concierge serve --transport sse``). The UI is a thin MCP client (SPEC-18 §18.4): the
waitlist and recent outcomes are read and changed through MCP tools, never through the
waitlist store or audit log directly.

Run with: ``concierge ui`` (or ``make ui``).
"""

import html
import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from fastmcp.exceptions import ToolError

# Absolute imports: Streamlit executes this file as a script, not as a package module.
from reinvent_concierge.config import Config
from reinvent_concierge.transit import TransitEngine
from reinvent_concierge.ui.mcp_client import MCPClient
from reinvent_concierge.ui.view_models import (
    TRANSIT_LABEL,
    agenda_blocks,
    seat_label,
    to_local,
)
from reinvent_concierge.util.time import event_zone as resolve_zone
from reinvent_concierge.venues import load_registry

logger = logging.getLogger(__name__)

VENUE_COLORS = {
    **{name: load_registry().color(name) for name in load_registry().venues},
    TRANSIT_LABEL: "#9e9e9e",
}
DEFAULT_COLOR = "#8c564b"
STATUS_ICONS = {
    "WATCHING": "👀",
    "FULL": "⛔",
    "FAILED": "⚠️",
    "PAUSED": "⏸️",
    "SWAP_PROPOSED": "🔁",
}
STATUS_TEXT = {"SWAP_PROPOSED": "Seat open — overlaps your schedule"}
BOOKING_FEEDBACK = {
    "CONFIRMED": st.success,
    "FAVORITED": st.info,
    "CONFLICT_DETECTED": st.warning,
    "FAILED": st.error,
}

st.set_page_config(
    page_title="re:Invent Autonomous Concierge",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .session-card { background-color: #f0f2f6; border-radius: 10px; padding: 15px;
                    margin: 10px 0; border-left: 5px solid #1f77b4; }
    .session-title { font-size: 18px; font-weight: bold; color: #1f77b4; }
    .session-meta { font-size: 14px; color: #666; margin: 5px 0; }
    .level-badge { display: inline-block; padding: 3px 8px; border-radius: 5px;
                   font-size: 12px; font-weight: bold; margin-right: 5px; }
    .level-100 { background-color: #28a745; color: white; }
    .level-200 { background-color: #17a2b8; color: white; }
    .level-300 { background-color: #ffc107; color: black; }
    .level-400 { background-color: #dc3545; color: white; }
    .venue-tag { display: inline-block; padding: 3px 8px; border-radius: 5px; font-size: 12px;
                 background-color: #6c757d; color: white; margin-right: 5px; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_config() -> Config:
    return Config.from_env_or_file()


@st.cache_resource
def get_client() -> MCPClient:
    return MCPClient(get_config().aws_events_mcp_server_url)


@st.cache_resource
def get_transit() -> TransitEngine:
    return TransitEngine()


def event_zone() -> ZoneInfo:
    """Event timezone reported by the server (schedule output), else configuration."""
    name = st.session_state.get("event_timezone") or get_config().event_timezone
    return resolve_zone(name, get_config().event_timezone)


def call_tool(tool: str, **arguments: Any) -> Optional[Dict[str, Any]]:
    """Call an MCP tool, reporting failures in the page instead of crashing it."""
    try:
        return get_client().call(tool, **arguments)
    except ToolError as e:
        st.error(f"{tool} failed: {e}")
    except Exception as e:
        logger.warning("Calling %s failed: %s", tool, e)
        st.error(
            f"Cannot reach the concierge server at {get_config().aws_events_mcp_server_url}. "
            "Start it with `concierge serve --transport sse`."
        )
    return None


# ---------------------------------------------------------------------- shared widgets


def show_booking_result(result: Dict[str, Any]) -> None:
    """Status plus the inline transit badge for a booking action."""
    BOOKING_FEEDBACK.get(result.get("status", ""), st.info)(result.get("message", ""))
    if result.get("transit_buffer_added"):
        st.caption("🚶 Transit buffer added to your schedule")
    if result.get("canceled_sessions"):
        st.caption(f"✂️ Canceled by the approved swap: {', '.join(result['canceled_sessions'])}")


def request_booking(session_id: str, key: str, feedback: Any) -> None:
    """Book now, or store a swap proposal (and its approval token) for ``swap_approval``."""
    swap_key = f"{key}-swap-proposal"
    result = call_tool("optimize_and_book_session", session_id=session_id)
    st.session_state.pop(swap_key, None)
    if result and result.get("status") == "CONFLICT_DETECTED" and result.get("approval_token"):
        st.session_state[swap_key] = result
    elif result:
        with feedback:
            show_booking_result(result)


def swap_approval(session_id: str, key: str, label: str, feedback: Any) -> None:
    """Second click of a swap: show the stored proposal and execute it once approved."""
    swap_key = f"{key}-swap-proposal"
    swap = st.session_state.get(swap_key)
    if not swap:
        return
    with feedback:
        for change in swap.get("proposed_changes", []):
            st.write(change)
        st.warning(swap["message"])
        releases = [c for c in swap.get("proposed_changes", []) if c.startswith("Cancel ")]
        if st.button(
            f"Approve swap: {'; '.join(releases)}; then book {label}",
            key=f"{key}-approve-swap",
        ):
            st.session_state.pop(swap_key, None)
            result = call_tool(
                "optimize_and_book_session",
                session_id=session_id,
                apply_run_id=swap["apply_run_id"],
                approval_token=swap["approval_token"],
            )
            if result:
                show_booking_result(result)


def session_actions(
    session_id: str, key: str, include_cancel: bool = False, title: Optional[str] = None
) -> None:
    """Book / favorite / waitlist (and cancel) buttons. Swaps and cancels take two clicks:
    the first stores the proposal and its approval token, the second executes it."""
    label = title or session_id
    cancel_key = f"{key}-cancel-proposal"
    columns = st.columns(4 if include_cancel else 3)
    feedback = st.container()
    if columns[0].button("📅 Book", key=f"{key}-book"):
        request_booking(session_id, key, feedback)
    if columns[1].button("⭐ Favorite", key=f"{key}-fav"):
        result = call_tool("manage_favorites", action="add", session_id=session_id)
        if result:
            (feedback.success if result.get("success") else feedback.warning)(result["message"])
    if columns[2].button("⏰ Waitlist", key=f"{key}-wait"):
        result = call_tool("add_to_waitlist", session_id=session_id, priority=50)
        if result:
            feedback.success(result["message"])
    if include_cancel and columns[3].button("✖️ Cancel", key=f"{key}-cancel"):
        result = call_tool("cancel_reservation", session_id=session_id)
        st.session_state.pop(cancel_key, None)
        if result and result.get("status") == "APPROVAL_REQUIRED":
            st.session_state[cancel_key] = result
        elif result:
            feedback.warning(result["message"])

    swap_approval(session_id, key, label, feedback)

    cancel = st.session_state.get(cancel_key)
    if cancel:
        with feedback:
            st.warning(
                f"Confirm cancel {label}: your reservation is released and the seat may not "
                "be available again."
            )
            if st.button(f"Confirm cancel {label}", key=f"{key}-confirm-cancel"):
                st.session_state.pop(cancel_key, None)
                result = call_tool(
                    "cancel_reservation",
                    session_id=session_id,
                    apply_run_id=cancel["apply_run_id"],
                    approval_token=cancel["approval_token"],
                )
                if result:
                    (st.success if result.get("status") == "CANCELED" else st.warning)(
                        result["message"]
                    )


def render_session_card(session: Dict[str, Any], key: str) -> None:
    level = session.get("level")
    start = to_local(session["start_time"], event_zone())
    end = to_local(session["end_time"], event_zone())
    level_badge = (
        f'<span class="level-badge level-{int(level)}">Level {int(level)}</span>' if level else ""
    )
    st.markdown(
        f"""
    <div class="session-card">
        <div class="session-title">{html.escape(session.get("title", "Untitled Session"))}</div>
        <div class="session-meta">{level_badge}
            <span class="venue-tag">{html.escape(session.get("venue_id", "Unknown"))}</span>
        </div>
        <div class="session-meta">📅 {start:%a %b %d, %H:%M}–{end:%H:%M}</div>
        <div class="session-meta">💺 {html.escape(seat_label(session))}</div>
    </div>
    """,
        unsafe_allow_html=True,
    )
    session_actions(session["session_id"], key, title=session.get("title"))


# ---------------------------------------------------------------------- panel 1


def panel_chat_concierge() -> None:
    st.header("🤖 Interactive Chat Concierge")

    for index, message in enumerate(st.session_state.chat_history):
        with st.chat_message(message["role"]):
            st.write(message["content"])
            for session in message.get("sessions", []):
                render_session_card(session, key=f"msg{index}-{session['session_id']}")

    user_input = st.chat_input("Ask about sessions, e.g. 'advanced Bedrock agents at Venetian'")
    if user_input:
        st.session_state.chat_history.append({"role": "user", "content": user_input})
        result = call_tool("search_catalog", query=user_input, limit=5)
        if result is not None:
            sessions = result.get("sessions", [])
            reply = (
                f"Found {result.get('total_found', 0)} matching sessions:"
                if sessions
                else "No open sessions matched. Try different words or run `concierge sync`."
            )
            st.session_state.chat_history.append(
                {"role": "assistant", "content": reply, "sessions": sessions}
            )
        st.rerun()


# ---------------------------------------------------------------------- panel 2


def agenda_figure(blocks: List[Dict[str, Any]]) -> go.Figure:
    frame = pd.DataFrame(blocks)
    figure = go.Figure()
    for group, rows in frame.groupby("group", sort=False):
        is_transit = group == TRANSIT_LABEL
        figure.add_bar(
            name=group,
            x=rows["day"],
            y=rows["hours"],
            base=rows["start_hour"],
            marker_color=VENUE_COLORS.get(group, DEFAULT_COLOR),
            marker_pattern_shape="/" if is_transit else "",
            customdata=list(
                zip(
                    rows["session_id"].fillna(""),
                    rows["title"].map(html.escape),
                    rows["venue"].map(html.escape),
                    rows["level"].map(lambda v: "" if pd.isna(v) else str(int(v))),
                    rows["time"],
                    strict=True,
                )
            ),
            hovertemplate=(
                "<b>%{customdata[1]}</b><br>%{customdata[2]}<br>"
                "Level %{customdata[3]}<br>%{customdata[4]}<extra></extra>"
            ),
        )
    figure.update_layout(
        barmode="overlay",
        height=620,
        xaxis={"title": "Day", "type": "category", "categoryorder": "category ascending"},
        yaxis={"title": "Hour", "autorange": "reversed", "dtick": 1},
        legend_title_text="Venue",
    )
    return figure


def panel_agenda_matrix() -> None:
    st.header("📅 Agenda Matrix & Transit Map")
    schedule = call_tool("get_my_schedule", include_favorites=True, include_personal_time=True)
    if schedule is None:
        return
    st.session_state["event_timezone"] = schedule.get("event_timezone") or st.session_state.get(
        "event_timezone"
    )

    booked = schedule.get("booked", [])
    personal_time = schedule.get("personal_time", [])
    columns = st.columns(3)
    columns[0].metric("Reserved Sessions", len(booked))
    columns[1].metric("Favorites", len(schedule.get("favorites", [])))
    columns[2].metric(
        "Transit Buffers", sum(1 for p in personal_time if p["title"].startswith("Transit:"))
    )

    if not booked and not personal_time:
        st.info("🗓️ Your schedule is empty. Search for sessions in the chat!")
    else:
        blocks = agenda_blocks(schedule, event_zone(), get_transit())
        event = st.plotly_chart(
            agenda_figure(blocks), key="agenda", on_select="rerun", selection_mode="points"
        )
        selected = [
            point["customdata"][0]
            for point in event.selection.points
            if point.get("customdata") and point["customdata"][0]
        ]
        sessions = {s["session_id"]: s for s in booked}
        if selected and selected[0] in sessions:
            session = sessions[selected[0]]
            with st.container(border=True):
                st.subheader(session["title"])
                st.caption(
                    f"{session['venue_id']} · Level {session.get('level') or 'n/a'} · "
                    f"{seat_label(session)}"
                )
                session_actions(
                    session["session_id"],
                    key=f"drawer-{session['session_id']}",
                    include_cancel=True,
                    title=session["title"],
                )
        else:
            st.caption("Click a session block for details and actions.")

    st.subheader("🗺️ Venue transit times (minutes)")
    transit = get_transit()
    venues = transit.list_venues()
    st.dataframe(
        pd.DataFrame(
            [[transit.get_transit_minutes(a, b) for b in venues] for a in venues],
            index=venues,
            columns=venues,
        )
    )
    st.caption(
        "Every value above is an **estimate** (door-to-door, including walking and waits); "
        "none have been measured against the real event yet."
    )


# ---------------------------------------------------------------------- panel 3


@st.fragment(run_every="10s")
def waitlist_control_center() -> None:
    # Callbacks run before the rerun, so the table below already reflects each edit.
    def set_priority(session_id: str) -> None:
        call_tool(
            "update_waitlist_entry",
            session_id=session_id,
            priority=int(st.session_state[f"prio-{session_id}"]),
        )

    def set_enabled(session_id: str) -> None:
        call_tool(
            "update_waitlist_entry",
            session_id=session_id,
            enabled=st.session_state[f"snag-{session_id}"],
        )

    def remove_entry(session_id: str) -> None:
        call_tool("remove_from_waitlist", session_id=session_id)

    listing = call_tool("list_waitlist")
    entries = listing.get("entries", []) if listing else []
    if not entries:
        st.info("📭 No sessions in the waitlist. Use ⏰ Waitlist on a session card.")
    else:
        st.caption(f"📋 {len(entries)} session(s), processed in priority order")
        for entry in entries:
            session_id = entry["session_id"]
            status = entry["status"] if entry["enabled"] else "PAUSED"
            checked = (
                to_local(entry["last_checked"], event_zone()).strftime("%H:%M:%S")
                if entry["last_checked"]
                else "never"
            )
            columns = st.columns([4, 2, 2, 2, 1])
            columns[0].markdown(
                f"**{entry['title'] or session_id}**  \n"
                f"{session_id} · {entry['venue_id'] or 'venue unknown'}"
            )
            columns[1].number_input(
                "Priority (1 = highest)",
                min_value=1,
                max_value=100,
                value=entry["priority"],
                key=f"prio-{session_id}",
                on_change=set_priority,
                args=(session_id,),
            )
            status_text = STATUS_TEXT.get(status, status)
            columns[2].markdown(
                f"{STATUS_ICONS.get(status, '')} **{status_text}**  \npolled {checked}"
            )
            columns[3].toggle(
                "Auto-snag",
                value=entry["enabled"],
                key=f"snag-{session_id}",
                on_change=set_enabled,
                args=(session_id,),
            )
            columns[4].button(
                "🗑️",
                key=f"remove-{session_id}",
                help="Remove",
                on_click=remove_entry,
                args=(session_id,),
            )
            if status == "SWAP_PROPOSED":
                # Reuse the two-step book/approve flow: a fresh proposal, then approval.
                review_key = f"waitlist-{session_id}"
                feedback = st.container()
                if st.button("🔁 Review swap", key=f"{review_key}-review"):
                    request_booking(session_id, review_key, feedback)
                swap_approval(session_id, review_key, entry["title"] or session_id, feedback)

    outcomes_response = call_tool("get_recent_outcomes")
    outcomes = outcomes_response.get("outcomes", []) if outcomes_response else []
    if outcomes:
        st.subheader("Recent snapper outcomes")
        st.dataframe(pd.DataFrame(outcomes), hide_index=True)


def panel_waitlist_control() -> None:
    st.header("⏰ Waitlist Snapper Control Center")
    waitlist_control_center()


# ---------------------------------------------------------------------- panel 4


def _profile_draft(profile: Optional[Dict[str, Any]], event_id: str) -> Dict[str, Any]:
    if profile:
        return dict(profile)
    return {
        "event_id": event_id,
        "name": "My re:Invent plan",
        "interest_queries": [],
        "preferred_levels": [],
        "preferred_tracks": [],
        "preferred_services": [],
        "must_attend_session_ids": [],
        "avoid_session_ids": [],
        "fixed_personal_time": [],
        "daily_time_windows": [],
        "required_breaks": [],
        "max_sessions_per_day": 6,
        "max_venue_changes_per_day": 3,
        "transit_multiplier": "1.0",
        "minimum_gap_minutes": 0,
        "seat_policy": "RESERVABLE_ONLY",
        "strategy_weights": {
            "interest_relevance": 35,
            "topic_fit": 15,
            "seat_confidence": 15,
            "transit_efficiency": 15,
            "pacing_and_breaks": 10,
            "content_diversity": 10,
        },
        "unresolved_session_ids": [],
    }


def panel_preferences() -> None:
    st.header("🎯 Attendance Preferences")
    event_id = get_config().aws_events_event_id
    response = call_tool("get_preference_profile", event_id=event_id)
    if response is None:
        return
    profile = response.get("profile")
    draft = _profile_draft(profile, event_id)

    for warning in response.get("warnings", []):
        st.warning(warning)

    with st.expander("Propose from intent", expanded=profile is None):
        intent = st.text_area(
            "Describe what you want to learn and any constraints",
            placeholder=(
                "Build me an advanced GenAI and serverless agenda. Keep Monday afternoon free, "
                "avoid more than two venue changes per day, and preserve my keynote ABC123."
            ),
            key="preference-intent",
        )
        if st.button("Propose update", disabled=not intent, key="propose-preference"):
            proposal = call_tool("propose_preference_update", intent=intent, event_id=event_id)
            if proposal:
                st.json(proposal.get("proposed_patch", {}))
                for message in proposal.get("ambiguities", []):
                    st.warning(message)
                for message in proposal.get("unsupported_requests", []):
                    st.info(message)
                for question in proposal.get("confirmation_questions", []):
                    st.caption(question)

    interests = ", ".join(item["query"] for item in draft["interest_queries"])
    valid_policies = ["RESERVABLE_ONLY", "INCLUDE_WALK_UP", "ANY"]
    current_policy = draft["seat_policy"]
    policy_index = valid_policies.index(current_policy) if current_policy in valid_policies else 0
    with st.form("preference-profile"):
        name = st.text_input("Profile name", value=draft["name"], max_chars=80)
        interest_text = st.text_input(
            "Topics", value=interests, help="Comma-separated topics, each weighted equally."
        )
        levels = st.multiselect(
            "Preferred levels",
            options=[100, 200, 300, 400],
            default=draft["preferred_levels"],
        )
        first, second, third = st.columns(3)
        max_sessions = first.number_input(
            "Sessions per day", min_value=1, max_value=12, value=draft["max_sessions_per_day"]
        )
        max_venue_changes = second.number_input(
            "Venue changes per day",
            min_value=0,
            max_value=10,
            value=draft["max_venue_changes_per_day"],
        )
        minimum_gap = third.number_input(
            "Minimum gap (minutes)",
            min_value=0,
            max_value=60,
            value=draft["minimum_gap_minutes"],
        )
        seat_policy = st.selectbox("Seat policy", valid_policies, index=policy_index)
        saved = st.form_submit_button("Save preferences")

    if saved:
        queries = [part.strip() for part in interest_text.split(",") if part.strip()]
        if not queries:
            st.error("Add at least one topic before saving.")
        else:
            payload = dict(draft)
            payload.update(
                {
                    "event_id": event_id,
                    "name": name,
                    "interest_queries": [{"query": query, "weight": 100} for query in queries],
                    "preferred_levels": levels,
                    "max_sessions_per_day": max_sessions,
                    "max_venue_changes_per_day": max_venue_changes,
                    "minimum_gap_minutes": minimum_gap,
                    "seat_policy": seat_policy,
                }
            )
            result = call_tool(
                "save_preference_profile",
                profile=payload,
                expected_updated_at=profile.get("updated_at") if profile else None,
            )
            if result:
                (st.success if result.get("status") == "SAVED" else st.warning)(result["message"])
                if result.get("status") == "SAVED":
                    st.rerun()

    if profile and st.button("Reset local preferences", key="reset-preference"):
        result = call_tool("reset_preference_profile", event_id=event_id)
        if result:
            st.info(result["message"])
            if result.get("deleted"):
                st.rerun()


# ---------------------------------------------------------------------- panel 5


def panel_plans() -> None:
    st.header("🧭 Itinerary Plans")
    event_id = get_config().aws_events_event_id
    profile_response = call_tool("get_preference_profile", event_id=event_id)
    if profile_response is None:
        return
    profile = profile_response.get("profile")
    if profile is None:
        st.info("Save attendance preferences before generating an itinerary plan.")
        return

    if st.button("Generate plans", key="generate-itinerary-plans"):
        generated = call_tool(
            "generate_itinerary_plans",
            event_id=event_id,
            profile_id=profile["profile_id"],
        )
        if generated:
            st.session_state.itinerary_plan_set = generated

    plan_set = st.session_state.get("itinerary_plan_set")
    if not plan_set:
        st.caption("Generation is read-only. Review the alternatives before any future apply step.")
        return
    for warning in plan_set.get("warnings", []):
        st.warning(warning)
    if not plan_set.get("plans"):
        st.error("No feasible plan could be generated.")
        for reason in plan_set.get("conflict_reasons", []):
            st.write(f"{reason['reason_code']}: {reason['summary']}")
        for suggestion in plan_set.get("relaxation_suggestions", []):
            st.caption(suggestion)
        return

    plans = plan_set["plans"]
    columns = st.columns(len(plans))
    for column, plan in zip(columns, plans, strict=True):
        with column:
            st.subheader(plan["strategy"].replace("_", " ").title())
            st.metric("Score", plan["total_score"])
            components = plan["score_components"]
            st.caption(
                f"Seat confidence {components['seat_confidence']}/100 · "
                f"Travel penalty {components['transit_and_venue_penalty']}/100"
            )
            for day in plan["day_items"]:
                for item in day:
                    if item["kind"] == "SESSION":
                        start = to_local(item["start_time"], event_zone())
                        st.write(f"{start:%a %H:%M} {item['title']}")
            with st.expander("Why these choices"):
                for explanation in plan["selected_explanations"]:
                    st.write(f"{explanation['session_id']}: {explanation['summary']}")
            with st.expander("Top exclusions"):
                for reason in plan["top_excluded_reasons"]:
                    st.write(f"{reason['session_id']}: {reason['reason_code']}")
            st.divider()
            mode = st.selectbox(
                "Application mode",
                [
                    "PREVIEW_ONLY",
                    "FAVORITES_ONLY",
                    "RESERVE_NON_CONFLICTING",
                    "APPLY_APPROVED_PLAN",
                ],
                key=f"apply-mode-{plan['plan_id']}",
            )
            preview_key = f"application-preview-{plan['plan_id']}"
            if st.button("Preview schedule changes", key=f"preview-application-{plan['plan_id']}"):
                preview = call_tool(
                    "preview_itinerary_application",
                    plan_id=plan["plan_id"],
                    mode=mode,
                )
                if preview:
                    st.session_state[preview_key] = preview
            preview = st.session_state.get(preview_key)
            if preview:
                st.caption(
                    f"Preview expires {to_local(preview['expires_at'], event_zone()):%H:%M}; "
                    f"quota wait estimate: {preview['quota_wait_seconds']} seconds"
                )
                for change in preview.get("changes", []):
                    st.write(f"{change['kind']}: {change.get('session_id') or change.get('title')}")
                for risk in preview.get("risk_text", []):
                    st.warning(risk)
                destructive_changes = [
                    change
                    for change in preview.get("changes", [])
                    if mode == "APPLY_APPROVED_PLAN"
                    and change["kind"] in {"SWAP", "CANCEL", "REMOVE_TRANSIT"}
                ]
                approvals = [
                    st.checkbox(
                        f"Approve {change['kind'].lower()}: "
                        f"{change.get('session_id') or change.get('personal_time_id')}",
                        key=f"approve-application-{plan['plan_id']}-{index}",
                    )
                    for index, change in enumerate(destructive_changes)
                ]
                approved = all(approvals)
                if st.button(
                    "Apply approved preview",
                    key=f"apply-application-{plan['plan_id']}",
                    disabled=bool(destructive_changes) and not approved,
                ):
                    result = call_tool(
                        "apply_itinerary_plan",
                        plan_id=plan["plan_id"],
                        mode=mode,
                        approval_token=preview["approval_token"],
                    )
                    if result:
                        (st.success if result["state"] == "APPLIED" else st.warning)(
                            f"Application state: {result['state']}"
                        )
                        for action in result.get("manual_actions", []):
                            st.warning(action)


# ---------------------------------------------------------------------- page


def main() -> None:
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    config = get_config()
    with st.sidebar:
        st.title("🤖 re:Invent Concierge")
        if config.write_access_active:
            st.success("Reservations enabled")
        else:
            st.warning("Dry-run: bookings are saved as favorites until reservations open")
        st.caption(f"MCP server: {config.aws_events_mcp_server_url}")
        st.caption("Refresh the catalog with `concierge sync`.")
        if st.button("🧹 Clear chat"):
            st.session_state.chat_history = []
            st.rerun()

    chat, agenda, waitlist, preferences, plans = st.tabs(
        [
            "💬 Chat Concierge",
            "📅 Agenda & Transit",
            "⏰ Waitlist Control",
            "🎯 Preferences",
            "🧭 Plans",
        ]
    )
    with chat:
        panel_chat_concierge()
    with agenda:
        panel_agenda_matrix()
    with waitlist:
        panel_waitlist_control()
    with preferences:
        panel_preferences()
    with plans:
        panel_plans()


main()
