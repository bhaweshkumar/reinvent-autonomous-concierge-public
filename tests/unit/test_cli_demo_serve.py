"""SPEC-21: ``concierge demo serve`` is registered with the expected options."""

from click.testing import CliRunner

from reinvent_concierge import cli as cli_module


def test_demo_group_lists_serve_subcommand():
    result = CliRunner().invoke(cli_module.cli, ["demo", "--help"])

    assert result.exit_code == 0
    assert "serve" in result.output


def test_demo_serve_help_shows_host_and_port_options():
    result = CliRunner().invoke(cli_module.cli, ["demo", "serve", "--help"])

    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output


def test_demo_serve_defaults_to_a_non_loopback_host():
    # Unlike `ui`/`serve`, the demo is meant to be reachable from outside localhost.
    result = CliRunner().invoke(cli_module.cli, ["demo", "serve", "--help"])

    assert "0.0.0.0" in result.output  # noqa: S104 - asserting the CLI's own default, not binding
