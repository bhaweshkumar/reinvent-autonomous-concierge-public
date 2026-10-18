"""Hosted read-only demo UI (SPEC-21 §21.2).

Search -> preference profile -> itinerary plans -> preview -> approve -> apply -> journal,
against a per-visitor ``SimulatedScheduleClient``. No AWS Events token exists anywhere in
this process and no AWS Events write is possible (``app_context.create_app_context``'s
``profile="DEMO"``; see ``demo/catalog_client.py`` and ``demo/simulated_schedule.py``).

Streamlit connects to an in-process FastMCP server over fastmcp's in-memory transport (no
network MCP endpoint is exposed): ``MCPClient`` already accepts a ``FastMCP`` instance in
place of a URL (see ``ui/mcp_client.py`` and its tests).

Run with: ``concierge demo serve`` (not ``streamlit run`` directly — the CLI command applies
the same loopback/host guard ``concierge serve`` does).
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

import streamlit as st
from fastmcp.exceptions import ToolError

# Absolute imports: Streamlit executes this file as a script, not as a package module.
from reinvent_concierge.agent.server import build_server
from reinvent_concierge.app_context import AppContext, create_app_context
from reinvent_concierge.config import Config
from reinvent_concierge.ui.mcp_client import MCPClient

st.set_page_config(page_title="re:Invent Concierge — Demo", page_icon="🤖", layout="wide")

_DEMO_EVENT_ID = "reinvent2026demo"


@dataclass
class DemoSession:
    context: AppContext
    client: MCPClient
    last_active: float


# One process serves every visitor; each visitor's own session lives in this dict, keyed by
# a uuid stored in that browser's ``st.session_state`` (never shared across visitors).
_SESSIONS: Dict[str, DemoSession] = {}


def _base_config() -> Config:
    return Config.from_env_or_file()


def _expire_idle_sessions(config: Config) -> None:
    cutoff = time.monotonic() - config.demo_session_idle_minutes * 60
    expired = [session_id for session_id, s in _SESSIONS.items() if s.last_active < cutoff]
    for session_id in expired:
        session = _SESSIONS.pop(session_id)
        asyncio.run(session.context.aclose())
        (config.demo_session_db_dir / f"{session_id}.db").unlink(missing_ok=True)


def _bootstrap_session() -> DemoSession:
    """Return this browser's demo session, creating one (under the concurrency cap) if needed."""
    config = _base_config()
    _expire_idle_sessions(config)

    session_id: Optional[str] = st.session_state.get("demo_session_id")
    if session_id is not None and session_id in _SESSIONS:
        session = _SESSIONS[session_id]
        session.last_active = time.monotonic()
        return session

    if len(_SESSIONS) >= config.demo_max_concurrent_sessions:
        st.error("The demo is at capacity. Please try again in a few minutes.")
        st.stop()

    session_id = uuid.uuid4().hex
    db_dir = config.demo_session_db_dir
    db_dir.mkdir(parents=True, exist_ok=True)
    session_config = config.model_copy(
        update={
            "planner_db_path": db_dir / f"{session_id}.db",
            "aws_events_event_id": _DEMO_EVENT_ID,
            "write_access_active": True,  # the simulator, never the real API, executes writes
        }
    )
    context = asyncio.run(create_app_context(session_config, profile="DEMO"))
    client = MCPClient(build_server(context.agent))
    session = DemoSession(context, client, time.monotonic())
    _SESSIONS[session_id] = session
    st.session_state["demo_session_id"] = session_id
    return session


def _call(client: MCPClient, tool: str, **arguments) -> dict:
    try:
        return client.call(tool, **arguments)
    except ToolError as exc:
        st.error(str(exc))
        st.stop()
        raise  # unreachable; st.stop() always raises, but this satisfies type checkers


def _demo_banner() -> None:
    st.warning(
        "**DEMO** — this page talks only to a simulated schedule. No AWS Events sign-in "
        "exists, and nothing here can write to the real re:Invent catalog.",
        icon="🧪",
    )


def _search_panel(client: MCPClient) -> None:
    st.subheader("Search the catalog")
    query = st.text_input("What are you interested in?", value="generative AI")
    if st.button("Search", key="search-button") and query:
        result = _call(client, "search_catalog", query=query, limit=10)
        for session in result.get("sessions", []):
            st.markdown(f"**{session['title']}** — {session.get('venue_id', 'Unknown')}")
            st.caption(session.get("session_id", ""))


def _preferences_panel(client: MCPClient) -> Optional[str]:
    st.subheader("Build a preference profile")
    interest = st.text_input("One thing you want to learn about", value="generative AI")
    name = st.text_input("Profile name", value="Demo visitor")
    if st.button("Save profile", key="save-profile-button") and interest:
        result = _call(
            client,
            "save_preference_profile",
            profile={
                "event_id": _DEMO_EVENT_ID,
                "name": name,
                "interest_queries": [{"query": interest, "weight": 100}],
            },
        )
        if result.get("status") == "SAVED":
            st.session_state["demo_profile_id"] = result.get("profile_id")
            st.success("Profile saved.")
        else:
            st.warning(f"Profile not saved: {result.get('status')}")
    return st.session_state.get("demo_profile_id")


def _plans_panel(client: MCPClient, profile_id: Optional[str]) -> None:
    st.subheader("Generate itinerary plans")
    if profile_id is None:
        st.caption("Save a preference profile first.")
        return
    if st.button("Generate plans", key="generate-plans-button"):
        plan_set = _call(
            client, "generate_itinerary_plans", event_id=_DEMO_EVENT_ID, profile_id=profile_id
        )
        st.session_state["demo_plans"] = plan_set.get("plans", [])
    for plan in st.session_state.get("demo_plans", []):
        plan_id = plan["plan_id"]
        with st.expander(f"Plan {plan_id} — score {plan.get('total_score')}"):
            st.json(plan)
            if st.button("Preview application", key=f"preview-{plan_id}"):
                st.session_state[f"demo_preview_{plan_id}"] = _call(
                    client,
                    "preview_itinerary_application",
                    plan_id=plan_id,
                    mode="RESERVE_NON_CONFLICTING",
                )
            preview: Optional[dict] = st.session_state.get(f"demo_preview_{plan_id}")
            if preview:
                st.json(preview)
                if st.button("Approve and apply", key=f"apply-{plan_id}"):
                    result = _call(
                        client,
                        "apply_itinerary_plan",
                        plan_id=plan_id,
                        mode="RESERVE_NON_CONFLICTING",
                        approval_token=preview["approval_token"],
                    )
                    st.success(f"Apply run {result.get('apply_run_id')}: {result.get('state')}")


def _journal_panel(client: MCPClient) -> None:
    st.subheader("Recent outcomes (journal)")
    result = _call(client, "get_recent_outcomes", limit=20)
    for outcome in result.get("outcomes", []):
        st.write(outcome)


def main() -> None:
    _demo_banner()
    session = _bootstrap_session()

    search_tab, preferences_tab, plans_tab, journal_tab = st.tabs(
        ["Search", "Preferences", "Plans", "Journal"]
    )
    with search_tab:
        _search_panel(session.client)
    with preferences_tab:
        _preferences_panel(session.client)
    with plans_tab:
        _plans_panel(session.client, st.session_state.get("demo_profile_id"))
    with journal_tab:
        _journal_panel(session.client)


main()
