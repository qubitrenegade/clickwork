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

    def test_help_does_not_leak_internal_docstring(self, tmp_path: Path):
        """--help must NOT expose the internal cli_group callback's docstring.

        WHY this matters: the inner cli_group() function has a developer-facing
        docstring documenting its callback args (ctx, verbose, quiet, etc.).
        Click's @click.group() decorator falls back to the callback's __doc__
        when no explicit ``help=`` is provided, so a plain create_cli() leaks
        that internal docstring into user-visible --help output.

        End users should never see phrases like "CLI entry point" or "Runs
        before every subcommand" or a raw "Args:" block -- that's an
        implementation detail of clickwork's factory, not the CLI they're
        using. This test pins the regression fix for issue #4.
        """
        from clickwork.cli import create_cli

        cli = create_cli(name="test-cli", commands_dir=tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # These phrases come verbatim from cli_group's docstring. If any of
        # them appear in --help, Click is still falling back to __doc__.
        assert "CLI entry point" not in result.output
        assert "Runs before every subcommand" not in result.output
        assert "configure logging, load config" not in result.output
        # The docstring also contains a Google-style "Args:" section with
        # parameter descriptions; none of that should reach the user.
        assert "Args:" not in result.output

    def test_help_shows_description_when_provided(self, tmp_path: Path):
        """When description= is passed, --help should display it.

        WHY: plugin authors want the ability to provide a short summary of
        what their CLI does (e.g., "Admin CLI for orbit"). Accepting a
        description parameter gives them that lever without forcing them
        to subclass or monkey-patch the Click group.
        """
        from clickwork.cli import create_cli

        cli = create_cli(
            name="test-cli",
            description="My awesome CLI for testing",
            commands_dir=tmp_path,
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "My awesome CLI for testing" in result.output
        # Regression guard: providing a description must still suppress
        # the internal docstring, not append to it.
        assert "CLI entry point" not in result.output

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


class TestAddParentToPath:
    """create_cli(add_parent_to_path=True) inserts commands_dir.parent.parent into sys.path.

    WHY this feature exists: plugin authors want their command files to be able
    to ``from tools.lib.X import Y`` without having to add sys.path boilerplate
    in their CLI entry script. When ``add_parent_to_path=True`` (opt-in), the
    factory prepends the resolved GRANDPARENT of ``commands_dir`` (i.e., the
    project root that contains ``tools/`` as a package) to ``sys.path`` so
    command modules can import the parent package (``tools``) and its siblings.

    Why grandparent and not parent: making ``tools/`` importable as a package
    (so ``import tools`` or ``from tools.lib.X import Y`` works) requires the
    directory that *contains* ``tools/`` to be on sys.path -- that's
    ``commands_dir.parent.parent``. Inserting just ``commands_dir.parent``
    would enable ``import lib`` style sibling imports, which is a less useful
    feature than what issue #15 called for.

    sys.path isolation: each test snapshots and restores ``sys.path`` via
    ``monkeypatch.setattr`` to avoid leaking mutations across the suite.
    monkeypatch auto-restores when the test finishes, which is the cleanest
    reader pattern available in pytest.
    """

    def test_add_parent_to_path_false_by_default_does_not_modify_sys_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Default behaviour must leave sys.path untouched.

        WHY: existing consumers of create_cli() rely on sys.path staying
        exactly as they configured it. The new kwarg is opt-in (defaults to
        False) so we don't change import resolution for anyone unless they
        explicitly ask for the auto-insertion behaviour.
        """
        from clickwork.cli import create_cli

        # Snapshot sys.path via monkeypatch so any mutation is auto-restored.
        # list(sys.path) copies the contents so our snapshot isn't the live
        # reference Click/whatever else might mutate during the call.
        monkeypatch.setattr("sys.path", list(sys.path))
        before = list(sys.path)

        create_cli(name="t", commands_dir=tmp_path)

        assert sys.path == before, (
            f"sys.path changed even though add_parent_to_path defaulted False: "
            f"before={before!r}, after={sys.path!r}"
        )

    def test_add_parent_to_path_true_inserts_commands_dir_grandparent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """With add_parent_to_path=True, sys.path[0] must be the resolved grandparent.

        WHY grandparent: to make ``commands_dir.parent`` importable as a
        package (e.g. ``import tools``), ``commands_dir.parent.parent`` has
        to be on sys.path. This test pins that relationship so a future
        refactor can't silently regress to the easier-but-wrong "insert
        parent" behavior -- see the module comment for the full rationale.

        WHY the resolved path: the implementation calls .resolve() so
        different unresolved spellings of the same directory (relative
        paths from different CWDs, symlinks, etc.) don't cause duplicate
        entries. We assert against the resolved absolute path to match
        what the implementation inserts.
        """
        from clickwork.cli import create_cli

        # Snapshot sys.path via monkeypatch for auto-restoration.
        monkeypatch.setattr("sys.path", list(sys.path))

        # Build a realistic layout: tmp_path / "project" / "tools" / "commands".
        # The commands_dir here is .../project/tools/commands, so the
        # grandparent is .../project. We assert sys.path[0] matches that.
        project_root = tmp_path / "project"
        tools_dir = project_root / "tools"
        commands_dir = tools_dir / "commands"
        commands_dir.mkdir(parents=True)

        create_cli(name="t", commands_dir=commands_dir, add_parent_to_path=True)

        expected = str(commands_dir.parent.parent.resolve())
        # Sanity check on the test's own assumptions: the resolved
        # grandparent of tools/commands must equal the resolved project
        # root we just built. If this assertion fails, the test is
        # comparing the wrong reference value.
        assert expected == str(project_root.resolve())
        assert sys.path[0] == expected, (
            f"Expected resolved grandparent at sys.path[0]: "
            f"expected={expected!r}, got sys.path[:3]={sys.path[:3]!r}"
        )

    def test_add_parent_to_path_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Calling create_cli() twice must not insert the path twice.

        WHY: plugin authors may instantiate create_cli() more than once in
        tests or REPL sessions. Each call blindly prepending would bloat
        sys.path unboundedly and shadow earlier entries with stale copies.
        The implementation must dedupe on the resolved absolute path.
        """
        from clickwork.cli import create_cli

        # Snapshot sys.path via monkeypatch for auto-restoration.
        monkeypatch.setattr("sys.path", list(sys.path))

        project_root = tmp_path / "project"
        commands_dir = project_root / "tools" / "commands"
        commands_dir.mkdir(parents=True)

        create_cli(name="t", commands_dir=commands_dir, add_parent_to_path=True)
        create_cli(name="t", commands_dir=commands_dir, add_parent_to_path=True)

        expected = str(commands_dir.parent.parent.resolve())
        # count() on the list tells us how many times the resolved path appears.
        # Exactly one is the correct answer -- the first insert wins, the
        # second call is a no-op because the path is already present.
        assert sys.path.count(expected) == 1, (
            f"Expected resolved grandparent to appear exactly once in sys.path; "
            f"found {sys.path.count(expected)} occurrences in {sys.path!r}"
        )


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


class TestClickExceptionHandling:
    """Click's own exceptions (UsageError, FileError, etc.) are user errors.

    WHY this class exists: ClickException and its subclasses represent user
    mistakes -- bad flags, missing files, invalid parameter values. Before
    the fix for issue #5, they fell through to the generic ``except Exception``
    branch in wrapped_invoke(), which stamped them with exit code 2 (framework
    bug) and an "Internal error:" prefix -- swallowing Click's own formatting
    (including the usage hint from UsageError). These tests pin the correct
    behaviour: we re-raise ClickException so Click's native handling surfaces
    the message with the right exit code and no "Internal error:" prefix.
    """

    def test_usage_error_is_not_treated_as_framework_bug(self, tmp_path: Path):
        """A click.UsageError must not be prefixed with "Internal error:".

        WHY: UsageError is the user passing a bad flag ("no such option"),
        which Click formats with a helpful "Usage: ... --help" hint. The old
        behaviour swallowed all of that and printed "Internal error: no such
        option: --foo", which wrongly implied a framework bug.

        We assert the absence of the "Internal error:" prefix rather than a
        specific exit code because Click's own convention assigns UsageError
        exit code 2 (collision with our framework-error code 2, but that's
        Click's behaviour and distinct from our "we crashed" path). What we
        really care about is that Click handled it, not us.
        """
        from clickwork.cli import create_cli

        @click.command()
        @click.pass_obj
        def bad_usage(ctx):
            # Simulates Click deep in a command raising UsageError from code
            # the command called (e.g., a helper that validates inputs).
            raise click.UsageError("no such option: --foo")

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(bad_usage)

        runner = CliRunner()
        result = runner.invoke(cli, ["bad-usage"], standalone_mode=True)
        # Click-native UsageError handling uses exit code 2 BUT does NOT
        # print "Internal error:" -- that's the key signal that we delegated
        # to Click instead of wrapping the exception as a framework bug.
        assert "Internal error:" not in result.output
        # Click's UsageError output includes "Error:" (Click's own formatter).
        assert "Error:" in result.output
        # UsageError.exit_code is 2 in Click -- happens to match our framework
        # error code, but semantically these are distinct paths.
        assert result.exit_code == 2

    def test_file_error_uses_click_native_handling(self, tmp_path: Path):
        """A click.FileError should exit 1 with Click's own "Error:" prefix.

        WHY: FileError is for "can't open this file" messages. Click's default
        exit_code for FileError is 1 (a user error, same as ours), and its
        formatter prints "Error: Could not open file 'X': <reason>". Before
        the fix, the user got "Internal error: <reason>" and exit 2 -- hiding
        the filename entirely.
        """
        from clickwork.cli import create_cli

        @click.command()
        @click.pass_obj
        def bad_file(ctx):
            # FileError is what click.File() raises for unreadable paths;
            # commands can also raise it directly for custom file handling.
            raise click.FileError("myfile.txt", "file not found")

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(bad_file)

        runner = CliRunner()
        result = runner.invoke(cli, ["bad-file"], standalone_mode=True)
        # User error -- not a framework bug.
        assert result.exit_code == 1
        assert "Internal error:" not in result.output
        # Click's native formatter mentions the filename and reason.
        assert "myfile.txt" in result.output
        assert "file not found" in result.output

    def test_generic_click_exception_exits_1_without_internal_prefix(
        self, tmp_path: Path
    ):
        """A plain click.ClickException should exit 1 with Click's own format.

        WHY: Plugin authors sometimes raise ClickException directly to signal
        a clean user error with a custom message. ClickException.exit_code
        defaults to 1, and Click formats the message as "Error: <msg>". Before
        the fix, we clobbered both the exit code (to 2) and the formatting
        (with "Internal error:").
        """
        from clickwork.cli import create_cli

        @click.command()
        @click.pass_obj
        def raises_click_exc(ctx):
            raise click.ClickException("generic user error")

        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        cli.add_command(raises_click_exc)

        runner = CliRunner()
        result = runner.invoke(cli, ["raises-click-exc"], standalone_mode=True)
        assert result.exit_code == 1
        assert "Internal error:" not in result.output
        assert "generic user error" in result.output


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
