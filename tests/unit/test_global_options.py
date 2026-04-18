"""Tests for clickwork.add_global_option.

add_global_option() installs a single Click option at every level (root group,
nested groups, and leaf subcommands) so users can pass it at any position on
the command line. The resolved value is merged into the Click root context's
``meta`` dict under the option's Python-identifier name.

Resolution rules (exercised below):
    - Plain flags (single ``--foo`` with ``is_flag=True``) OR across levels:
      truthy at ANY level wins.
    - Slash-flags (``--foo/--no-foo`` with ``is_flag=True``) are an
      exception -- they use innermost-wins so an inner ``--no-foo`` can
      override an outer ``--foo`` (OR would never let False win, rendering
      the off-form useless at inner levels).
    - Value options (string, int, etc.) use innermost-wins semantics.
    - Not passed anywhere => Click-resolved default (typically False for
      flags and None for value options, but the caller can override either
      via ``default=...`` in the option_kwargs).

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

        # Click uses exit code 2 specifically for UsageError (its standard
        # classification for "bad CLI invocation"). Tighten from `!= 0` to
        # `== 2` so we catch a regression where some unrelated exit code
        # (e.g. 1 from a thrown exception in a callback) would otherwise
        # let this test silently pass.
        assert result.exit_code == 2
        # Click's canonical phrasing for unknown options is "no such option".
        assert "no such option" in result.output.lower()


class TestAddGlobalOptionGuards:
    """add_global_option refuses caller configurations that would silently break.

    These guards exist so misuse surfaces at add_global_option() call time
    rather than later during parse or dispatch where the error message
    would be from Click, buried, and hard to connect to the real cause.
    """

    def test_rejects_caller_supplied_expose_value_true(self) -> None:
        """expose_value=True would inject the flag as a kwarg on every command.

        That breaks existing command signatures (they weren't written to
        receive the global option). We own expose_value=False as an API
        invariant; raise if the caller tries to override.
        """
        import pytest

        @click.group()
        def root() -> None: ...

        @root.command("sub-cmd")
        def sub() -> None: ...

        with pytest.raises(TypeError, match="expose_value"):
            add_global_option(root, "--json", is_flag=True, expose_value=True)

    def test_rejects_duplicate_install(self) -> None:
        """Calling add_global_option twice with the same flag raises ValueError.

        Click would otherwise fail at parse/help time with a confusing
        "option already registered" error. Surfacing the conflict at the
        second add_global_option call points the caller at the real cause.
        """
        import pytest

        @click.group()
        def root() -> None: ...

        @root.command("sub-cmd")
        def sub() -> None: ...

        add_global_option(root, "--json", is_flag=True)

        with pytest.raises(ValueError, match="Cannot install global option"):
            add_global_option(root, "--json", is_flag=True)

    def test_rejects_conflict_with_existing_manual_option(self) -> None:
        """If a command already has a matching option, installing raises.

        Same cause as the duplicate-install case but surfaces when the
        caller is mixing add_global_option with hand-declared options.
        """
        import pytest

        @click.group()
        def root() -> None: ...

        @root.command("sub-cmd")
        @click.option("--json", is_flag=True)
        def sub(json: bool) -> None: ...

        with pytest.raises(ValueError, match="Cannot install global option"):
            add_global_option(root, "--json", is_flag=True)

    def test_slash_flag_uses_innermost_wins(self) -> None:
        """--foo/--no-foo is a slash-flag and resolves innermost-wins.

        For plain flags ("--foo" only) we OR across levels -- there's no
        way to explicitly say "off" at an inner level. But slash-flags
        give the user an explicit off-form ("--no-foo"), and the
        intuitive semantic is "the level the user typed it at wins". An
        inner --no-foo must be able to override an outer --foo, which
        OR-merge can't produce (False never wins an OR).
        """
        captured: dict[str, object] = {}

        @click.group()
        def root() -> None: ...

        @root.command("sub-cmd")
        @click.pass_context
        def sub(ctx: click.Context) -> None:
            captured.update(ctx.find_root().meta)

        add_global_option(root, "--shout/--no-shout", is_flag=True, default=False)

        runner = CliRunner()

        # Inner --no-shout should override outer --shout. With OR merge
        # (the plain-flag rule) this would return True; with innermost-
        # wins it correctly returns False.
        captured.clear()
        result = runner.invoke(root, ["--shout", "sub-cmd", "--no-shout"])
        assert result.exit_code == 0, result.output
        assert captured.get("shout") is False, (
            "Inner --no-shout should override outer --shout (innermost-wins "
            f"for slash-flags); got ctx.meta['shout']={captured.get('shout')!r}"
        )

        # Outer --shout alone still works.
        captured.clear()
        result = runner.invoke(root, ["--shout", "sub-cmd"])
        assert result.exit_code == 0, result.output
        assert captured.get("shout") is True


class TestAddGlobalOptionConflictDetection:
    """The conflict check catches flag-string collisions too, not just name collisions."""

    def test_rejects_conflict_by_flag_string_even_when_names_differ(self) -> None:
        """A pre-existing @click.option('output_json', '--json') collides on '--json'.

        The Python name 'output_json' does NOT match add_global_option's
        derived 'json', so a name-only conflict check would miss this. The
        flag-string check catches it.
        """
        import pytest

        @click.group()
        def root() -> None: ...

        @root.command("sub-cmd")
        @click.option("output_json", "--json", is_flag=True)
        def sub(output_json: bool) -> None: ...

        with pytest.raises(ValueError, match="already uses flag string"):
            add_global_option(root, "--json", is_flag=True)

    def test_rejects_slash_flag_conflict(self) -> None:
        """A slash-flag param_decl must match against existing --foo/--no-foo.

        '--shout/--no-shout' is a *single* string in param_decls but Click
        splits it into two flag strings (opts=['--shout'] +
        secondary_opts=['--no-shout']). Early drafts of the conflict
        check filtered param_decls with .startswith('-'), which for slash-
        flags left the unsplit '--shout/--no-shout' string -- intersection
        with {'--shout','--no-shout'} is empty and the collision slips
        through. This test pins the probe-based derivation that splits
        the slash-flag correctly.
        """
        import pytest

        @click.group()
        def root() -> None: ...

        @root.command("sub-cmd")
        @click.option("--shout/--no-shout", is_flag=True, default=False)
        def sub(shout: bool) -> None: ...

        with pytest.raises(ValueError, match="already uses flag string"):
            add_global_option(root, "--shout/--no-shout", is_flag=True, default=False)


class TestAddGlobalOptionEntryPointPropagation:
    """ctx.meta values propagate into entry-point plugin commands.

    LazyEntryPointCommand.invoke creates a fresh Click context for the
    loaded plugin command; without parent= wiring, the plugin's
    ctx.find_root() would return a detached root and miss values that
    add_global_option wrote to the true root's meta. This test pins the
    parent=ctx.parent forwarding we added in discovery.py (it has to be
    ctx.parent, not ctx itself, to avoid Click double-counting the
    plugin-cmd segment in the loaded command's command_path -- the proxy
    ctx already represents that level in the chain).
    """

    def test_global_option_value_reaches_entry_point_plugin(self, monkeypatch) -> None:
        """A plugin command loaded via entry point sees ctx.find_root().meta['json']."""
        import click
        from click.testing import CliRunner
        from clickwork.discovery import discover_commands_from_entrypoints

        captured: dict = {}

        class FakeEntryPoint:
            """Imitates importlib.metadata.EntryPoint's name + load() interface."""

            name = "plugin-cmd"

            def load(self):
                @click.command(name="plugin-cmd")
                @click.pass_context
                def plugin_cmd(ctx: click.Context) -> None:
                    # The key assertion: the plugin's find_root() returns
                    # the parent CLI's context, so meta values from
                    # add_global_option are visible here.
                    captured["json"] = ctx.find_root().meta.get("json")

                return plugin_cmd

        # Replace the real importlib.metadata.entry_points with our stub so
        # discover_commands_from_entrypoints returns the fake above.
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group=None: [FakeEntryPoint()] if group == "clickwork.commands" else [],
        )

        @click.group()
        def root() -> None: ...

        # Attach the lazy entry-point proxy to the root group. Normally
        # create_cli() does this, but we build a minimal root here so the
        # test stays focused on the meta-propagation invariant.
        commands = discover_commands_from_entrypoints()
        for cmd_name, cmd in commands.items():
            root.add_command(cmd, cmd_name)

        add_global_option(root, "--json", is_flag=True)

        runner = CliRunner()
        result = runner.invoke(root, ["--json", "plugin-cmd"])

        assert result.exit_code == 0, result.output
        assert captured.get("json") is True, (
            "Global option value did not propagate into the entry-point "
            f"plugin command; got captured={captured!r}. Check that "
            "LazyEntryPointCommand.invoke forwards parent=ctx.parent to loaded.main()."
        )

    def test_global_option_flag_collision_with_plugin_raises(self, monkeypatch) -> None:
        """If a plugin declares the same flag add_global_option installed, error.

        WHY this test exists: add_global_option's conflict detection can't
        see inside a LazyEntryPointCommand until the plugin is actually
        loaded. So a plugin's private ``--json`` stays invisible to the
        install-time guard. Without a runtime check in
        LazyEntryPointCommand.invoke, Click would parse the flag at the
        proxy level, consume the token, and the plugin would silently
        never see its own option -- a nasty "it's ignored and I have no
        idea why" debugging experience for plugin authors.

        The runtime check in discovery.py compares the proxy's and the
        loaded command's flag strings at invoke time and raises
        click.UsageError on overlap. This test builds that exact collision
        (plugin declares --json internally, CLI has add_global_option's
        --json installed) and asserts the error fires with both sides
        named so the caller can find the conflict quickly.
        """
        import click
        from click.testing import CliRunner
        from clickwork.discovery import discover_commands_from_entrypoints

        class FakeEntryPoint:
            name = "plugin-cmd"

            def load(self):
                # The plugin independently declares --json on its own
                # command. This is the scenario add_global_option's
                # install-time check can't detect because at install
                # time the plugin hasn't been loaded yet.
                @click.command(name="plugin-cmd")
                @click.option("--json", is_flag=True)
                def plugin_cmd(json: bool) -> None: ...

                return plugin_cmd

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group=None: [FakeEntryPoint()] if group == "clickwork.commands" else [],
        )

        @click.group()
        def root() -> None: ...

        commands = discover_commands_from_entrypoints()
        for cmd_name, cmd in commands.items():
            root.add_command(cmd, cmd_name)

        # This succeeds because the proxy's params (at this point) are
        # empty, so add_global_option sees no collision. The collision
        # only materialises at invoke time when the plugin is loaded.
        add_global_option(root, "--json", is_flag=True)

        runner = CliRunner()
        result = runner.invoke(root, ["plugin-cmd"])

        # UsageError exit code is 2; message must name both sides so the
        # caller can figure out where to fix it.
        assert result.exit_code == 2, (
            f"Expected UsageError (exit 2) on plugin flag collision; "
            f"got exit={result.exit_code}, output={result.output!r}"
        )
        assert "plugin-cmd" in result.output
        assert "--json" in result.output

    def test_global_option_flag_collision_on_nested_subcommand_raises(
        self, monkeypatch
    ) -> None:
        """Collision on a nested subcommand of a loaded group also raises.

        WHY this test exists: the runtime check walks the LOADED command's
        tree, not just its own ``.params``. If a plugin's entry-point
        target is a ``click.Group`` and the collision lives on one of the
        group's subcommands, an earlier "only check loaded.params" version
        would MISS it -- Click's parser would still consume the flag at
        the proxy level, so the subcommand would silently never see its
        own option. This test builds that exact shape (a loaded group
        ``plugin`` with a subcommand ``sub`` that declares ``--json``)
        and asserts the runtime check fires with both ``sub`` and
        ``--json`` named in the error.
        """
        import click
        from click.testing import CliRunner
        from clickwork.discovery import discover_commands_from_entrypoints

        class FakeEntryPoint:
            name = "plugin"

            def load(self):
                @click.group(name="plugin")
                def plugin_grp() -> None: ...

                # Collision lives on the SUBCOMMAND, not the group itself.
                # A "check loaded.params only" walk would miss this.
                @plugin_grp.command("sub")
                @click.option("--json", is_flag=True)
                def sub(json: bool) -> None: ...

                return plugin_grp

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group=None: [FakeEntryPoint()] if group == "clickwork.commands" else [],
        )

        @click.group()
        def root() -> None: ...

        commands = discover_commands_from_entrypoints()
        for cmd_name, cmd in commands.items():
            root.add_command(cmd, cmd_name)

        add_global_option(root, "--json", is_flag=True)

        runner = CliRunner()
        # Repro from the user review on PR #26: invoke the nested
        # subcommand with the colliding flag. Without the recursive walk,
        # this returns exit 0 with root meta['json']=True but the
        # subcommand sees json=False -- a silent wrong-behaviour. With
        # the walk, we get UsageError (exit 2) up front.
        result = runner.invoke(root, ["plugin", "sub", "--json"])

        assert result.exit_code == 2, (
            f"Expected UsageError (exit 2) on nested-subcommand flag collision; "
            f"got exit={result.exit_code}, output={result.output!r}"
        )
        assert "sub" in result.output, (
            f"Error message must name the colliding subcommand path; got "
            f"output={result.output!r}"
        )
        assert "--json" in result.output


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
