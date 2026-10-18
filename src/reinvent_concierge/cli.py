"""Command-line entrypoint: ``concierge`` (or ``python -m reinvent_concierge``)."""

import asyncio
import logging
import signal
import subprocess
import sys
import webbrowser
from importlib.resources import files
from ipaddress import ip_address
from typing import Optional

import click
import truststore

from .app_context import create_app_context
from .audit import AuditLogger
from .config import Config
from .credentials import CredentialManager
from .exceptions import SignInError
from .runtime import RuntimeLeaseError


def _is_loopback_host(host: str) -> bool:
    """True for ``localhost`` or a loopback IP (SPEC-18 §18.8 DNS-rebinding guard)."""
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _configure_logging(verbose: bool) -> None:
    # Logs go to stderr: in stdio mode stdout carries the MCP protocol.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """re:Invent Autonomous Concierge."""
    _configure_logging(verbose)
    # Verify TLS against the OS trust store, not certifi's bundle, so HTTPS works behind
    # TLS-inspecting corporate proxies (e.g. Zscaler) whose root CA lives only in the OS.
    truststore.inject_into_ssl()


@cli.command("store-token")
@click.option("--with-refresh-token", is_flag=True, help="Also store a refresh token.")
def store_token(with_refresh_token: bool) -> None:
    """Store the Builder ID access token (prompted, never echoed) with mode 0600."""
    config = Config.from_env_or_file()
    token = click.prompt("Access token", hide_input=True)
    refresh_token = click.prompt("Refresh token", hide_input=True) if with_refresh_token else None
    CredentialManager(config.aws_events_credentials_path).store_token(token, refresh_token)
    click.echo(f"Token stored in {config.aws_events_credentials_path}", err=True)


@cli.command()
@click.option("--no-browser", is_flag=True, help="Print the sign-in URL instead of opening it.")
def login(no_browser: bool) -> None:
    """Sign in with AWS Builder ID (browser, PKCE) and store the tokens (mode 0600)."""
    from .auth import sign_in

    config = Config.from_env_or_file()
    credentials = CredentialManager(config.aws_events_credentials_path)

    def show(url: str) -> None:
        click.echo(f"Sign in at:\n{url}", err=True)
        if not no_browser:
            webbrowser.open(url)

    try:
        tokens = asyncio.run(sign_in(config, credentials, open_browser=show))
    except SignInError as e:
        raise click.ClickException(str(e)) from e
    lifetime = f"; access token valid {tokens.expires_in // 60} min" if tokens.expires_in else ""
    click.echo(f"Signed in{lifetime}. Tokens stored in {credentials.credentials_path}", err=True)


@cli.command()
def logout() -> None:
    """Revoke the refresh token and delete stored tokens."""
    from .auth import BUILDER_ID_PROFILE_URL, sign_out

    config = Config.from_env_or_file()
    revoked = asyncio.run(sign_out(config, CredentialManager(config.aws_events_credentials_path)))
    click.echo("Tokens deleted" + ("" if revoked else " (revocation failed)"), err=True)
    click.echo(f"To also end the Builder ID session, visit {BUILDER_ID_PROFILE_URL}", err=True)


@cli.command()
@click.option("--mcp", "include_mcp", is_flag=True, help="Also check the hosted MCP server.")
def doctor(include_mcp: bool) -> None:
    """Check connectivity, sign-in, event registration and catalog access."""
    from .doctor import run_checks

    config = Config.from_env_or_file()
    credentials = CredentialManager(config.aws_events_credentials_path)
    results = asyncio.run(run_checks(config, credentials, include_hosted_mcp=include_mcp))
    for result in results:
        mark = click.style("OK  ", fg="green") if result.ok else click.style("FAIL", fg="red")
        click.echo(f"{mark} {result.name}: {result.detail}")
    if not all(result.ok for result in results):
        raise SystemExit(1)


@cli.group("audit")
def audit_group() -> None:
    """Inspect the audit trail (stored in the planner database, SPEC-18 §18.3)."""


@audit_group.command("export")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="File to write JSON-lines to (default: print to stdout).",
)
def audit_export(output: Optional[str]) -> None:
    """Write every audit row as JSON-lines, oldest first."""
    config = Config.from_env_or_file()
    credentials = CredentialManager(config.aws_events_credentials_path)
    lines = AuditLogger(config.planner_db_path, credentials.hash_token).export_json_lines()
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(lines)
        click.echo(f"Audit trail written to {output}", err=True)
    else:
        click.echo(lines, nl=False)


@cli.command()
@click.option("--full", is_flag=True, help="Rebuild the catalog instead of a delta sync.")
def sync(full: bool) -> None:
    """Sync the session catalog into the local SQLite + FAISS store."""

    async def run() -> int:
        context = await create_app_context()
        try:
            if full:
                return await context.ingestion.initial_sync()
            return await context.ingestion.incremental_sync()
        finally:
            await context.aclose()

    click.echo(f"{asyncio.run(run())} sessions synced", err=True)


@cli.command()
@click.option(
    "--transport", type=click.Choice(["stdio", "sse"]), default="stdio", show_default=True
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address for SSE; keep it on loopback.",
)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option(
    "--allow-remote",
    is_flag=True,
    help="Allow a non-loopback --host. Every write tool becomes reachable over the network.",
)
def serve(transport: str, host: str, port: int, allow_remote: bool) -> None:
    """Run the FastMCP server (stdio for Claude Desktop, SSE for the web UI)."""
    from starlette.middleware import Middleware

    from .agent.server import build_server
    from .agent.transport_security import LoopbackHostOriginGuard

    if transport == "sse" and not _is_loopback_host(host) and not allow_remote:
        raise click.ClickException(
            f"--host {host} is not loopback. Pass --allow-remote to expose every "
            "write tool beyond localhost (SPEC-18 §18.8)."
        )
    if transport == "sse" and allow_remote:
        click.echo(
            f"WARNING: binding SSE to {host} exposes every write tool to the network; "
            "the DNS-rebinding guard is disabled for this run.",
            err=True,
        )

    async def run() -> None:
        context = await create_app_context()
        try:
            server = build_server(context.agent)
            if transport == "stdio":
                await server.run_async(transport="stdio", show_banner=False)
            elif allow_remote:
                await server.run_async(transport="sse", host=host, port=port)
            else:
                # FastMCP's own Host/Origin guard only wires into the http/streamable-http
                # transports, not this legacy SSE one, so this project supplies its own
                # DNS-rebinding guard for it (SPEC-18 §18.8).
                guard = Middleware(LoopbackHostOriginGuard)
                await server.run_async(transport="sse", host=host, port=port, middleware=[guard])
        finally:
            await context.aclose()

    asyncio.run(run())


@cli.command()
@click.option(
    "--interval",
    type=click.IntRange(min=1),
    default=None,
    help="Positive seconds between poll cycles.",
)
def snapper(interval: Optional[int]) -> None:
    """Run the seat snapper under the runtime lease until SIGTERM/SIGINT."""

    async def run_snapper() -> None:
        context = await create_app_context()
        coordinator = context.build_snapper_runtime(interval)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, coordinator.stop)
        try:
            await coordinator.start()
            await coordinator.wait()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            await coordinator.aclose()
            await context.aclose()

    try:
        asyncio.run(run_snapper())
    except RuntimeLeaseError as error:
        raise click.ClickException(str(error)) from error


@cli.command("run")
@click.option(
    "--poll-interval",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Positive seconds between seat-snapper cycles.",
)
@click.option(
    "--catalog-interval",
    type=click.FloatRange(min=0, min_open=True),
    default=300.0,
    show_default=True,
    help="Positive seconds between catalog syncs.",
)
def run(poll_interval: Optional[float], catalog_interval: float) -> None:
    """Run the single local catalog-sync and seat-snapper coordinator."""

    async def run_runtime() -> None:
        context = await create_app_context()
        coordinator = context.build_runtime_coordinator(poll_interval, catalog_interval)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, coordinator.stop)
        try:
            await coordinator.start()
            await coordinator.wait()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            await coordinator.aclose()
            await context.aclose()

    try:
        asyncio.run(run_runtime())
    except RuntimeLeaseError as error:
        raise click.ClickException(str(error)) from error


@cli.command()
@click.option("--port", default=8501, show_default=True, type=int)
def ui(port: int) -> None:
    """Launch the Streamlit UI on localhost (needs `concierge serve --transport sse`)."""
    app = files("reinvent_concierge.ui") / "app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        # Headless skips Streamlit's interactive first-run e-mail prompt.
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    click.echo(f"UI: http://127.0.0.1:{port}", err=True)
    raise SystemExit(subprocess.call(command))  # noqa: S603 - fixed argv, no shell


@cli.group("demo")
def demo_group() -> None:
    """Hosted read-only demo (SPEC-21): simulated schedule, no AWS Events credential or write."""


@demo_group.command("serve")
@click.option(
    "--host",
    default="0.0.0.0",  # noqa: S104 - this command is meant to be reachable, unlike `ui`/`serve`
    show_default=True,
    help="Bind address; the demo is meant to be reached from outside localhost.",
)
@click.option("--port", default=8080, show_default=True, type=int)
def demo_serve(host: str, port: int) -> None:
    """Launch the hosted demo: one process, Streamlit + an in-process MCP server per visitor.

    No AWS Events token is ever read, and every schedule write goes to a per-visitor
    in-memory simulator; see ``demo/simulated_schedule.py`` and ``demo/catalog_client.py``.
    """
    import shutil

    config = Config.from_env_or_file()
    if config.demo_baked_catalog_path and not config.local_db_path.exists():
        # Every visitor session's own IngestionService calls ensure_schema() against this
        # same shared catalog; that needs a writable file, so it's copied out of the
        # (possibly read-only) baked image layer once, the first time the container starts.
        config.local_db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.demo_baked_catalog_path, config.local_db_path)
        click.echo(f"Copied baked catalog to {config.local_db_path}", err=True)
    app = files("reinvent_concierge.ui") / "demo_app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.address",
        host,
        "--server.port",
        str(port),
        # Headless skips Streamlit's interactive first-run e-mail prompt.
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    click.echo(f"Demo: http://{host}:{port}", err=True)
    raise SystemExit(subprocess.call(command))  # noqa: S603 - fixed argv, no shell


if __name__ == "__main__":
    cli()
