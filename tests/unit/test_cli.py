"""Tests for the CLI factory.

create_cli() is the single function a plugin author calls to get a working CLI.
It wires together discovery, config, logging, and the CliContext. These tests
use Click's CliRunner for isolated testing without spawning subprocesses.

Test structure:
- TestCreateCli: tests the CLI group creation, flags, and discovery
- TestConvenienceMethods: tests that CliContext has all bound helpers
- TestFrameworkErrorHandling: tests that unhandled exceptions exit with code 2
- TestPassCliContextDecorator: tests the @pass_cli_context decorator
"""
from pathlib import Path
import sys

import click
from click.testing import CliRunner
import pytest


class TestCreateCli:
    """create_cli() returns a Click group with global flags and discovery."""

    def test_returns_click_group(self, tmp_path: Path):
        """create_cli() must return a Click Group, not a bare Command.

        WHY Group: the CLI has subcommands ('greet', 'deploy', etc.) and
        global flags (--dry-run, --env) that must be parsed before dispatch.
        Only a Group supports that structure.
        """
        from clickwork.cli import create_cli

        cli = create_cli(name="test-cli", commands_dir=tmp_path)
        assert isinstance(cli, click.Group)

    def test_help_shows_usage(self, tmp_path: Path):
        """--help should exit 0 and include some usage information.

        WHY this matters: if the group is misconfigured (wrong name, missing
        help text) users see confusing output and can't discover subcommands.
        """
        from clickwork.cli import create_cli

        cli = create_cli(name="test-cli", commands_dir=tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # Either the CLI name appears OR standard "Usage" header appears.
        assert "test-cli" in result.output.lower() or "Usage" in result.output

    def test_discovers_commands_from_dir(self, tmp_path: Path):
        """Commands in the commands_dir should be registered and runnable.

        WHY: the whole point of create_cli() is to wire up discovery so plugin
        authors don't have to manually register each command file.
        """
        from clickwork.cli import create_cli

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        # Write a minimal command file that follows the convention: exports 'cli'.
        (cmd_dir / "greet.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def greet():\n"
            "    '''Say hello.'''\n"
            "    click.echo('hello from greet')\n\n"
            "cli = greet\n"
        )

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        runner = CliRunner()
        result = runner.invoke(cli, ["greet"])
        assert result.exit_code == 0
        assert "hello from greet" in result.output

    def test_verbose_and_quiet_are_mutually_exclusive(self, tmp_path: Path):
        """Passing both --verbose and --quiet should exit 2 with an error message.

        WHY: these flags have opposite effects on log output. Silently picking
        one would surprise the user; a clear error is better.
        """
        from clickwork.cli import create_cli

        cli = create_cli(name="test-cli", commands_dir=tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "--quiet"])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_context_has_cli_context(self, tmp_path: Path):
        """Commands should receive a CliContext via ctx.obj.

        WHY: every command needs dry_run, yes, env, config, and logger
        without re-declaring the same Click options on every subcommand.
        The CliContext carries all of that from the group callback.
        """
        from clickwork.cli import create_cli
        from clickwork._types import CliContext

        received_ctx = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received_ctx["type"] = type(ctx).__name__
            received_ctx["dry_run"] = ctx.dry_run

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["--dry-run", "check"])
        assert result.exit_code == 0
        assert received_ctx["type"] == "CliContext"
        assert received_ctx["dry_run"] is True

    def test_env_flag_sets_context(self, tmp_path: Path):
        """--env <value> should propagate into ctx.obj.env.

        WHY: commands use ctx.env to select config sections (e.g.,
        'staging' vs 'production'). If the flag doesn't reach ctx.obj,
        commands can't branch on the selected environment.
        """
        from clickwork.cli import create_cli

        received_env = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received_env["env"] = ctx.env

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["--env", "staging", "check"])
        assert result.exit_code == 0
        assert received_env["env"] == "staging"

    def test_env_var_fallback_sets_ctx_env(self, tmp_path: Path, monkeypatch):
        """When --env is omitted, {PROJECT_NAME}_ENV env var should set ctx.env.

        WHY: CI pipelines set TEST_CLI_ENV=staging to select an environment
        without modifying every command invocation. If ctx.env stays None
        while config values come from [env.staging], commands that branch
        on ctx.env would take the wrong path.
        """
        from clickwork.cli import create_cli

        monkeypatch.setenv("TEST_CLI_ENV", "staging")

        received_env = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received_env["env"] = ctx.env

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["check"])
        assert result.exit_code == 0
        assert received_env["env"] == "staging"

    def test_dry_run_flag_sets_context(self, tmp_path: Path):
        """--dry-run should set ctx.obj.dry_run = True.

        WHY: dry_run is the core safety flag. If it doesn't reach ctx.obj,
        commands calling ctx.run() would execute destructive operations
        even when the user explicitly asked for a preview.
        """
        from clickwork.cli import create_cli

        received = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received["dry_run"] = ctx.dry_run

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["--dry-run", "check"])
        assert result.exit_code == 0
        assert received["dry_run"] is True

    def test_yes_flag_sets_context(self, tmp_path: Path):
        """--yes / -y should set ctx.obj.yes = True.

        WHY: ctx.confirm() and ctx.confirm_destructive() use yes to skip
        interactive prompts. If the flag doesn't reach ctx.obj, --yes
        would have no effect and CI pipelines would hang.
        """
        from clickwork.cli import create_cli

        received = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received["yes"] = ctx.yes

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["--yes", "check"])
        assert result.exit_code == 0
        assert received["yes"] is True

    def test_config_loaded_into_context(self, tmp_path: Path):
        """Config from a TOML file should be accessible as ctx.obj.config.

        WHY: commands use ctx.config['bucket'] etc. rather than opening
        their own config files. If create_cli() doesn't load config, every
        command would need its own loading logic -- defeating the harness.
        """
        from clickwork.cli import create_cli

        received = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received["bucket"] = ctx.config["bucket"]

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nbucket = "from-config"\n')

        cli = create_cli(
            name="test-cli",
            commands_dir=cmd_dir,
            repo_config_path=config_file,
        )
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["check"])
        assert result.exit_code == 0
        assert received["bucket"] == "from-config"


class TestConvenienceMethods:
    """Convenience methods on CliContext are bound by create_cli()."""

    def test_ctx_run_delegates_to_process_run(self, tmp_path: Path):
        """All six convenience methods must be bound (not None) after create_cli().

        WHY: commands use ctx.run(), ctx.capture() etc. without importing
        process.py. If create_cli() doesn't bind them, every ctx.run() call
        raises TypeError: 'NoneType' is not callable, which is very confusing
        for plugin authors.
        """
        from clickwork.cli import create_cli

        received = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            received["run_callable"] = ctx.run is not None
            received["capture_callable"] = ctx.capture is not None
            received["require_callable"] = ctx.require is not None
            received["confirm_callable"] = ctx.confirm is not None
            received["run_with_confirm_callable"] = ctx.run_with_confirm is not None

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["check"])
        assert result.exit_code == 0
        assert all(received.values()), f"Some methods not bound: {received}"

    def test_ctx_run_respects_dry_run(self, tmp_path: Path):
        """ctx.run() in dry-run mode should not execute the command (returns None).

        WHY: the convenience methods close over the CLI context's flags. If
        ctx.run() ignores dry_run, passing --dry-run at the CLI level has no
        effect on commands that use ctx.run() -- a critical safety regression.
        """
        from clickwork.cli import create_cli

        received = {}

        @click.command()
        @click.pass_obj
        def check(ctx):
            result = ctx.run([sys.executable, "-c", "import sys; sys.exit(1)"])
            received["result"] = result

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["--dry-run", "check"])
        assert result.exit_code == 0
        assert received["result"] is None


class TestFrameworkErrorHandling:
    """Framework errors exit with code 2 (internal error)."""

    def test_unhandled_exception_exits_with_code_2(self, tmp_path: Path):
        """An unhandled exception inside a command should exit with code 2.

        WHY: we distinguish user errors (exit 1, e.g. bad args) from framework
        bugs (exit 2, e.g. unexpected RuntimeError). This lets CI pipelines
        and shell scripts know whether a failure was the user's fault or
        a bug in the framework itself.
        """
        from clickwork.cli import create_cli

        @click.command()
        @click.pass_obj
        def broken(ctx):
            raise RuntimeError("framework bug")

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(broken)

        runner = CliRunner()
        result = runner.invoke(cli, ["broken"])
        assert result.exit_code == 2

    def test_prerequisite_error_exits_with_code_1(self, tmp_path: Path):
        """PrerequisiteError (missing tool) should exit with code 1, not 2.

        WHY: a missing binary is the user's environment problem, not a
        framework bug. Exit code 1 tells CI it's a fixable configuration
        error, not an internal failure.
        """
        from clickwork.cli import create_cli
        from clickwork._types import PrerequisiteError

        @click.command()
        @click.pass_obj
        def needs_docker(ctx):
            raise PrerequisiteError("Required tool 'docker' is not on PATH")

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(needs_docker)

        runner = CliRunner()
        result = runner.invoke(cli, ["needs-docker"])
        assert result.exit_code == 1
        assert "docker" in result.output


class TestPassCliContextDecorator:
    """@pass_cli_context injects a CliContext into the command function."""

    def test_pass_cli_context_decorator_works(self, tmp_path: Path):
        """@pass_cli_context should pass CliContext as the first argument.

        WHY: this decorator is the recommended way for command authors to
        receive a typed CliContext without importing click.pass_obj and
        remembering to call ensure_object(). It also gives a clear error
        if the command is somehow invoked outside a create_cli() harness.
        """
        from clickwork.cli import create_cli, pass_cli_context
        from clickwork._types import CliContext

        received = {}

        @click.command()
        @pass_cli_context
        def check(ctx):
            # ctx should be a CliContext, not a click.Context
            received["is_cli_context"] = isinstance(ctx, CliContext)
            received["dry_run"] = ctx.dry_run

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(check)

        runner = CliRunner()
        result = runner.invoke(cli, ["--dry-run", "check"])
        assert result.exit_code == 0, result.output
        assert received["is_cli_context"] is True
        assert received["dry_run"] is True
