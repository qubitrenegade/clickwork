"""End-to-end CLI tests using Click's CliRunner.

These tests create real CLI instances with real command files on disk,
invoke them, and verify the full output. No mocking -- this catches
integration issues between modules.
"""

from pathlib import Path

from click.testing import CliRunner

from clickwork.cli import create_cli


class TestCliEndToEnd:
    """Full lifecycle: create CLI, discover commands, invoke, verify output."""

    def test_help_lists_discovered_commands(self, tmp_path: Path):
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        (cmd_dir / "deploy.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def deploy():\n"
            "    '''Deploy the application.'''\n"
            "    click.echo('deployed')\n\n"
            "cli = deploy\n"
        )
        (cmd_dir / "status.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def status():\n"
            "    '''Show current status.'''\n"
            "    click.echo('ok')\n\n"
            "cli = status\n"
        )

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "deploy" in result.output
        assert "status" in result.output

    def test_dry_run_flag_propagates(self, tmp_path: Path):
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        (cmd_dir / "check.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "@click.pass_obj\n"
            "def check(ctx):\n"
            "    '''Check dry-run flag.'''\n"
            "    click.echo(f'dry_run={ctx.dry_run}')\n\n"
            "cli = check\n"
        )

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        runner = CliRunner()
        result = runner.invoke(cli, ["--dry-run", "check"])
        assert result.exit_code == 0
        assert "dry_run=True" in result.output

    def test_unknown_command_shows_help(self, tmp_path: Path):
        cli = create_cli(name="test-cli", commands_dir=tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["nonexistent"])
        assert result.exit_code != 0
