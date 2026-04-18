"""Tests for clickwork.add_global_option.

add_global_option() installs a single Click option at every level (root group,
nested groups, and leaf subcommands) so users can pass it at any position on
the command line. The resolved value is merged into the Click root context's
``meta`` dict under the option's Python-identifier name.

Resolution rules (exercised below):
    - Flags (``is_flag=True``) OR across levels: truthy at ANY level wins.
    - Value options (string, int, etc.) use innermost-wins semantics.
    - Not passed anywhere => False for flags, None (or Click default) for values.

These tests build minimal inline Click CLIs to keep each test focused on the
parsing/merge behaviour of add_global_option itself. A single end-of-file
integration test confirms it also works with ``clickwork.create_cli()``.
"""
from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from clickwork import add_global_option
from clickwork.cli import create_cli


def _build_root_with_sub() -> tuple[click.Group, dict[str, object]]:
    """Build a root group with a single leaf subcommand for flag/value tests.

    The subcommand's callback writes ``ctx.meta`` into a shared dict so tests
    can assert on the resolved values after invocation. We use a dict-capture
    pattern because CliRunner can't directly return the Click context.

    Returns:
        A (root_group, captured) tuple. ``captured`` starts empty and is
        populated with a copy of ``ctx.find_root().meta`` when the subcommand
        runs.
    """
    captured: dict[str, object] = {}

    @click.group()
    def root() -> None:
        """Root group for testing global options."""

    @root.command("sub-cmd")
    @click.pass_context
    def sub_cmd(ctx: click.Context) -> None:
        """Leaf subcommand that snapshots ctx.meta so tests can inspect it."""
        # Copy from the ROOT meta because that's where add_global_option
        # stores its resolved values (the invariant the feature promises).
        captured.update(ctx.find_root().meta)

    return root, captured


class TestAddGlobalOptionFlag:
    """Flag behaviour: --json is_flag=True, OR across levels, default False."""

    def test_add_global_option_root_level_parses(self) -> None:
        """``myapp --json sub-cmd`` sets ``ctx.meta['json']`` to True.

        WHY this is the simplest case: the flag sits on the root group,
        where Click would normally bind it. We just need to make sure our
        callback writes through to the root context's meta.
        """
        root, captured = _build_root_with_sub()
        add_global_option(root, "--json", is_flag=True, help="Emit JSON.")

        runner = CliRunner()
        result = runner.invoke(root, ["--json", "sub-cmd"])

        assert result.exit_code == 0, result.output
        assert captured.get("json") is True

    def test_add_global_option_subcommand_level_parses(self) -> None:
        """``myapp sub-cmd --json`` sets ``ctx.meta['json']`` to True.

        WHY this matters: users intuitively expect ``--json`` to work on the
        subcommand they're calling, not only on the top-level binary. Our
        callback on the subcommand's option has to walk up to root.meta.
        """
        root, captured = _build_root_with_sub()
        add_global_option(root, "--json", is_flag=True, help="Emit JSON.")

        runner = CliRunner()
        result = runner.invoke(root, ["sub-cmd", "--json"])

        assert result.exit_code == 0, result.output
        assert captured.get("json") is True

    def test_add_global_option_group_level_parses(self) -> None:
        """With a nested group, ``myapp group --json sub-cmd`` sets meta.

        The nested group is a common pattern (``orbit-admin users list``).
        add_global_option must recurse into nested groups so users can also
        pass the flag at the middle level.
        """
        captured: dict[str, object] = {}

        @click.group()
        def root() -> None:
            """Root group."""

        @root.group("group")
        def inner_group() -> None:
            """Nested group under root."""

        @inner_group.command("sub-cmd")
        @click.pass_context
        def sub_cmd(ctx: click.Context) -> None:
            """Leaf under the nested group."""
            captured.update(ctx.find_root().meta)

        add_global_option(root, "--json", is_flag=True, help="Emit JSON.")

        runner = CliRunner()
        result = runner.invoke(root, ["group", "--json", "sub-cmd"])

        assert result.exit_code == 0, result.output
        assert captured.get("json") is True

    def test_add_global_option_flag_or_semantics_across_levels(self) -> None:
        """OR semantics: flag at root OR subcommand OR both => True.

        The OR rule means any single occurrence at any level flips the meta
        value to True. Passing at two levels is still True (not a conflict).
        """
        root, captured = _build_root_with_sub()
        add_global_option(root, "--json", is_flag=True, help="Emit JSON.")

        runner = CliRunner()
        result = runner.invoke(root, ["--json", "sub-cmd", "--json"])

        assert result.exit_code == 0, result.output
        assert captured.get("json") is True

    def test_add_global_option_not_passed_is_falsy_or_none(self) -> None:
        """No --json anywhere => meta['json'] is False (flag default)."""
        root, captured = _build_root_with_sub()
        add_global_option(root, "--json", is_flag=True, help="Emit JSON.")

        runner = CliRunner()
        result = runner.invoke(root, ["sub-cmd"])

        assert result.exit_code == 0, result.output
        # False, not missing: even a default-only parse writes to meta so
        # consumers can unconditionally read ctx.meta['json'] without
        # .get()-with-default.
        assert captured.get("json") is False


class TestAddGlobalOptionValue:
    """Value behaviour: --env is a string option, innermost-wins semantics."""

    def test_value_innermost_wins_when_both_levels_set(self) -> None:
        """``--env=prod sub-cmd --env=staging`` => meta['env'] == 'staging'.

        Innermost wins because the subcommand level is closer to the action
        being taken. This matches how most CLIs handle overrides (e.g.,
        config-file env beats global env).
        """
        root, captured = _build_root_with_sub()
        add_global_option(root, "--env", default=None, help="Environment.")

        runner = CliRunner()
        result = runner.invoke(root, ["--env=prod", "sub-cmd", "--env=staging"])

        assert result.exit_code == 0, result.output
        assert captured.get("env") == "staging"

    def test_value_root_only_propagates_to_meta(self) -> None:
        """``--env=prod sub-cmd`` => meta['env'] == 'prod' (root wins alone)."""
        root, captured = _build_root_with_sub()
        add_global_option(root, "--env", default=None, help="Environment.")

        runner = CliRunner()
        result = runner.invoke(root, ["--env=prod", "sub-cmd"])

        assert result.exit_code == 0, result.output
        assert captured.get("env") == "prod"

    def test_value_not_passed_is_none(self) -> None:
        """No --env anywhere => meta['env'] is None (the Click default)."""
        root, captured = _build_root_with_sub()
        add_global_option(root, "--env", default=None, help="Environment.")

        runner = CliRunner()
        result = runner.invoke(root, ["sub-cmd"])

        assert result.exit_code == 0, result.output
        assert captured.get("env") is None


class TestAddGlobalOptionSnapshotSemantics:
    """add_global_option is a call-time snapshot, NOT retroactive."""

    def test_added_subcommands_do_not_inherit_option_retroactively(self) -> None:
        """Subcommands attached AFTER add_global_option() don't get the option.

        WHY snapshot: retroactive registration would require monkey-patching
        Group.add_command and introduces lifecycle surprises (e.g., options
        appearing on commands imported from third-party plugins). The
        snapshot rule keeps the behaviour predictable and testable.
        """
        root, _ = _build_root_with_sub()
        add_global_option(root, "--json", is_flag=True, help="Emit JSON.")

        # Attach a fresh command AFTER the snapshot. It must NOT know about
        # --json, so invoking ``fresh --json`` should error as an unknown
        # option (Click's UsageError, exit code 2).
        @root.command("fresh")
        def fresh() -> None:
            """A command added after add_global_option() ran."""
            click.echo("fresh ran")

        runner = CliRunner()
        result = runner.invoke(root, ["fresh", "--json"])

        assert result.exit_code != 0
        # Click's canonical phrasing for unknown options is "no such option".
        assert "no such option" in result.output.lower()


class TestAddGlobalOptionIntegration:
    """End-to-end check that add_global_option composes with create_cli()."""

    def test_works_with_create_cli_harness(self, tmp_path: Path) -> None:
        """A CLI built via create_cli() accepts add_global_option flags.

        This confirms we don't depend on any clickwork-specific wiring
        besides Click's own ctx.meta: the feature works whether the root
        group was built by hand or by the clickwork harness.
        """
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        # A command file that snapshots ctx.find_root().meta into a file so
        # we can assert on it after CliRunner invokes the CLI. We can't
        # capture via a shared dict here because discover_commands() imports
        # the module fresh -- references from the test module aren't visible.
        snapshot_path = tmp_path / "snapshot.txt"
        (cmd_dir / "echo_json.py").write_text(
            "import click\n"
            f"SNAPSHOT = {str(snapshot_path)!r}\n"
            "\n"
            "@click.command('echo-json')\n"
            "@click.pass_context\n"
            "def cli(ctx):\n"
            "    root_meta = ctx.find_root().meta\n"
            "    # Write 'json=True' (or False) so the test can read it back.\n"
            "    with open(SNAPSHOT, 'w') as fh:\n"
            "        fh.write(f\"json={root_meta.get('json')!r}\")\n"
        )

        cli = create_cli(name="test-cli", commands_dir=cmd_dir)
        add_global_option(cli, "--json", is_flag=True, help="Emit JSON.")

        runner = CliRunner()
        result = runner.invoke(cli, ["echo-json", "--json"])

        assert result.exit_code == 0, result.output
        assert snapshot_path.read_text() == "json=True"
