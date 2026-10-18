"""SPEC-18 §18.8: ``concierge serve`` refuses a non-loopback --host without --allow-remote."""

from click.testing import CliRunner

from reinvent_concierge import cli as cli_module


def test_non_loopback_host_is_refused_without_allow_remote():
    result = CliRunner().invoke(
        cli_module.cli,
        ["serve", "--transport", "sse", "--host", "0.0.0.0"],  # noqa: S104
    )
    assert result.exit_code != 0
    assert "--allow-remote" in result.output
