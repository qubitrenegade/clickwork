"""Tests for plugin discovery.

Discovery finds Click commands from two sources:
1. Directory scanning: imports .py files from a commands/ dir, looks for 'cli' export
2. Entry points: reads the 'clickwork.commands' entry point group

The discovery_mode parameter controls which mechanisms are active:
- "dev": directory scanning only
- "installed": entry points only
- "auto": entry points always, plus directory scanning when commands_dir exists

Installed-mode discovery is intentionally lazy: entry points are wrapped in
lightweight Click proxies so startup and unrelated commands don't import every
installed plugin up front. The real command object loads on invocation, and on
`--help` when Click asks for command metadata.
"""

from pathlib import Path

import click
from click.testing import CliRunner


class TestDirectoryScanning:
    """Directory scanning imports .py files and looks for 'cli' attribute."""

    def test_discovers_command_from_file(self, tmp_path: Path):
        """A .py file with a 'cli' attribute should be discovered."""
        from clickwork.discovery import discover_commands_from_dir

        cmd_file = tmp_path / "greet.py"
        cmd_file.write_text(
            "import click\n\n"
            "@click.command()\n"
            "def greet():\n"
            "    '''Say hello.'''\n"
            "    click.echo('hello')\n\n"
            "cli = greet\n"
        )

        commands = discover_commands_from_dir(tmp_path)
        assert "greet" in commands
        assert isinstance(commands["greet"], click.Command)

    def test_skips_files_without_cli_export(self, tmp_path: Path, caplog):
        """Files without 'cli' attribute produce a warning, not an error."""
        from clickwork.discovery import discover_commands_from_dir

        helper = tmp_path / "utils.py"
        helper.write_text("# Just a helper module\nHELPER = True\n")

        with caplog.at_level("WARNING", logger="clickwork"):
            commands = discover_commands_from_dir(tmp_path)
        assert commands == {}
        assert "utils.py" in caplog.text

    def test_skips_subdirectories(self, tmp_path: Path):
        """Subdirectories (like lib/) should not be scanned."""
        from clickwork.discovery import discover_commands_from_dir

        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "helper.py").write_text("import click\ncli = click.command()(lambda: None)\n")

        commands = discover_commands_from_dir(tmp_path)
        assert "helper" not in commands

    def test_skips_init_files(self, tmp_path: Path):
        from clickwork.discovery import discover_commands_from_dir

        (tmp_path / "__init__.py").write_text("")

        commands = discover_commands_from_dir(tmp_path)
        assert commands == {}

    def test_discovers_click_group(self, tmp_path: Path):
        """A file exporting a click.Group should become a subcommand group."""
        from clickwork.discovery import discover_commands_from_dir

        cmd_file = tmp_path / "deploy.py"
        cmd_file.write_text(
            "import click\n\n"
            "@click.group()\n"
            "def deploy():\n"
            "    '''Deploy commands.'''\n"
            "    pass\n\n"
            "@deploy.command()\n"
            "def site():\n"
            "    click.echo('deploying site')\n\n"
            "cli = deploy\n"
        )

        commands = discover_commands_from_dir(tmp_path)
        assert "deploy" in commands
        assert isinstance(commands["deploy"], click.Group)

    def test_supports_relative_imports_between_command_files(self, tmp_path: Path):
        """Command modules should be able to import sibling helper modules."""
        from clickwork.discovery import discover_commands_from_dir

        (tmp_path / "helper.py").write_text("VALUE = 'hello from helper'\n")
        (tmp_path / "greet.py").write_text(
            "import click\n"
            "from .helper import VALUE\n\n"
            "@click.command()\n"
            "def greet():\n"
            "    click.echo(VALUE)\n\n"
            "cli = greet\n"
        )

        commands = discover_commands_from_dir(tmp_path)
        assert "greet" in commands

        runner = CliRunner()
        result = runner.invoke(commands["greet"], [])
        assert result.exit_code == 0
        assert result.output.strip() == "hello from helper"

    def test_uses_explicit_click_command_name(self, tmp_path: Path):
        """The discovered command name should match the Click-exposed name."""
        from clickwork.discovery import discover_commands_from_dir

        cmd_file = tmp_path / "deploy.py"
        cmd_file.write_text(
            "import click\n\n"
            "@click.command(name='deploy-site')\n"
            "def deploy():\n"
            "    pass\n\n"
            "cli = deploy\n"
        )

        commands = discover_commands_from_dir(tmp_path)
        assert "deploy-site" in commands
        assert "deploy" not in commands

    def test_handles_import_error_gracefully(self, tmp_path: Path, caplog):
        """A command file that fails to import should warn, not crash."""
        from clickwork.discovery import discover_commands_from_dir

        broken = tmp_path / "broken.py"
        broken.write_text("import nonexistent_module_xyz123\n")

        with caplog.at_level("WARNING", logger="clickwork"):
            commands = discover_commands_from_dir(tmp_path)
        assert commands == {}
        assert "broken.py" in caplog.text

    def test_handles_syntax_error_gracefully(self, tmp_path: Path, caplog):
        """A command file with syntax errors should warn, not crash."""
        from clickwork.discovery import discover_commands_from_dir

        broken = tmp_path / "bad_syntax.py"
        broken.write_text("def broken(\n")

        with caplog.at_level("WARNING", logger="clickwork"):
            commands = discover_commands_from_dir(tmp_path)
        assert commands == {}
        assert "bad_syntax.py" in caplog.text


class TestNamespaceIsolation:
    """Discovery namespaces must not leak between different command dirs."""

    def test_same_filename_in_two_dirs_gets_different_modules(self, tmp_path: Path):
        """Two dirs with the same helper.py should yield independent modules.

        WHY: discover_commands_from_dir() registers modules in sys.modules.
        If the namespace is flat (clickwork._discovered.helper), the second
        scan gets the cached first helper from sys.modules -- silently
        loading the wrong code.
        """
        from click.testing import CliRunner

        from clickwork.discovery import discover_commands_from_dir

        # Create two directories each with a helper.py exporting 'cli'.
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        (dir_a / "helper.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def helper():\n"
            "    click.echo('from-dir-a')\n\n"
            "cli = helper\n"
        )

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        (dir_b / "helper.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def helper():\n"
            "    click.echo('from-dir-b')\n\n"
            "cli = helper\n"
        )

        # Discover from both dirs.
        cmds_a = discover_commands_from_dir(dir_a)
        cmds_b = discover_commands_from_dir(dir_b)

        assert "helper" in cmds_a
        assert "helper" in cmds_b

        # The two commands must be distinct objects producing different output.
        runner = CliRunner()
        result_a = runner.invoke(cmds_a["helper"], [])
        result_b = runner.invoke(cmds_b["helper"], [])
        assert result_a.output.strip() == "from-dir-a"
        assert result_b.output.strip() == "from-dir-b"


class TestDiscoveryMode:
    """discover_commands() selects mechanism based on discovery_mode."""

    def test_dev_mode_uses_directory(self, tmp_path: Path):
        from clickwork.discovery import discover_commands

        cmd_file = tmp_path / "hello.py"
        cmd_file.write_text(
            "import click\n\n@click.command()\ndef hello():\n    click.echo('hi')\n\ncli = hello\n"
        )

        commands = discover_commands(
            commands_dir=tmp_path,
            discovery_mode="dev",
        )
        assert "hello" in commands

    def test_auto_mode_uses_dir_when_exists(self, tmp_path: Path):
        from clickwork.discovery import discover_commands

        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "status.py").write_text(
            "import click\n\n@click.command()\ndef status():\n    pass\n\ncli = status\n"
        )

        commands = discover_commands(
            commands_dir=commands_dir,
            discovery_mode="auto",
        )
        assert "status" in commands

    def test_auto_mode_queries_entrypoints_even_when_dir_exists(self, tmp_path: Path, monkeypatch):
        """Auto mode should use BOTH mechanisms when commands_dir exists.

        WHY: installed plugin commands should always be visible during dev.
        If auto mode only used directory scanning, plugins from other packages
        would vanish just because a commands/ directory is present.
        """
        from clickwork.discovery import discover_commands

        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "status.py").write_text(
            "import click\n\n@click.command()\ndef status():\n    pass\n\ncli = status\n"
        )

        called = {"entry_points": False}

        def _fake_entry_points(*, group=None):
            called["entry_points"] = True
            return []

        monkeypatch.setattr("importlib.metadata.entry_points", _fake_entry_points)

        commands = discover_commands(
            commands_dir=commands_dir,
            discovery_mode="auto",
        )
        assert "status" in commands
        assert called["entry_points"] is True

    def test_auto_mode_falls_back_to_entrypoints(self, tmp_path: Path):
        """When commands_dir doesn't exist, auto mode still uses entry points."""
        from clickwork.discovery import discover_commands

        # Point at a nonexistent directory -- should fall back gracefully.
        commands = discover_commands(
            commands_dir=tmp_path / "nonexistent",
            discovery_mode="auto",
        )
        # No entry points installed in test env, so should be empty.
        assert isinstance(commands, dict)

    def test_installed_mode_ignores_directory(self, tmp_path: Path):
        """In installed mode, commands_dir is ignored even if it exists."""
        from clickwork.discovery import discover_commands

        # Create a commands dir with a real command file
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "local.py").write_text(
            "import click\n\n@click.command()\ndef local():\n    pass\n\ncli = local\n"
        )

        # In installed mode, the directory should be ignored
        commands = discover_commands(
            commands_dir=commands_dir,
            discovery_mode="installed",
        )
        # "local" should NOT be discovered -- only entry points are used
        assert "local" not in commands


class TestEntrypoints:
    """Installed-mode discovery uses lazy entry-point proxies."""

    def test_discovers_entrypoint_without_loading_it(self, monkeypatch):
        from clickwork.discovery import discover_commands_from_entrypoints

        loaded = {"called": False}

        class FakeEntryPoint:
            name = "hello"

            def load(self):
                loaded["called"] = True
                return click.command(name="hello")(lambda: None)

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group=None: [FakeEntryPoint()] if group == "clickwork.commands" else [],
        )

        commands = discover_commands_from_entrypoints()
        assert "hello" in commands
        assert loaded["called"] is False

    def test_lazy_proxy_forwards_options_and_arguments(self, monkeypatch):
        from clickwork.discovery import discover_commands_from_entrypoints

        class FakeEntryPoint:
            name = "hello"

            def load(self):
                @click.command(name="hello")
                @click.option("--foo", required=True)
                @click.argument("name")
                def hello(foo: str, name: str):
                    click.echo(f"{foo}:{name}")

                return hello

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group=None: [FakeEntryPoint()] if group == "clickwork.commands" else [],
        )

        commands = discover_commands_from_entrypoints()
        runner = CliRunner()
        result = runner.invoke(commands["hello"], ["--foo", "bar", "alice"])

        assert result.exit_code == 0
        assert result.output.strip() == "bar:alice"

    def test_local_command_shadows_installed_with_info_log(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """When auto mode finds the same name in both sources, local wins.

        WHY: during dev you want to iterate on a local copy of a command
        without uninstalling the plugin. The INFO log tells you shadowing
        happened so stale local files don't silently hide installed plugins.
        """
        from clickwork.discovery import discover_commands

        installed = click.command(name="hello")(lambda: None)
        monkeypatch.setattr(
            "clickwork.discovery.discover_commands_from_entrypoints",
            lambda: {"hello": installed},
        )

        (tmp_path / "hello.py").write_text(
            "import click\n\n@click.command()\ndef hello():\n    pass\n\ncli = hello\n"
        )

        with caplog.at_level("INFO", logger="clickwork"):
            commands = discover_commands(commands_dir=tmp_path, discovery_mode="auto")
        assert "hello" in commands
        assert commands["hello"] is not installed
        assert "shadows installed plugin command" in caplog.text


class TestMixedDiscovery:
    """Mixed directory + entry-point discovery in auto mode.

    Pins the contract for Wave-3 issue #51: when auto-mode merges commands
    from both the local ``commands/`` directory and entry-point-installed
    plugins, the LOCAL command wins on name collisions. The installed
    command is silently dropped from the returned dict (though it can
    still be imported directly by whoever wants to bypass shadowing).

    WHY these tests exist as a dedicated class: the shadowing semantics
    are intentional behaviour, not an accident of how the dict happens to
    merge. Collapsing them into one-off asserts in another class risks
    someone "fixing" the merge order later and breaking dev ergonomics
    without noticing. Keeping the contract in a single named class makes
    the policy obvious to future readers.

    Pattern note: these tests stub ``importlib.metadata.entry_points``
    at module scope -- that's the same hook ``discover_commands_from_entrypoints``
    calls at runtime, so stubbing it exercises the real code path (no
    monkeypatching of the discovery helpers themselves). This mirrors
    the pattern already used by ``TestEntrypoints.test_discovers_entrypoint_without_loading_it``.
    """

    @staticmethod
    def _stub_entry_points(
        monkeypatch,
        name: str,
        command: click.Command,
    ) -> None:
        """Install a fake entry-point named ``name`` returning ``command``.

        Creates a tiny FakeEntryPoint whose ``.load()`` returns ``command``,
        then monkeypatches ``importlib.metadata.entry_points`` to return a
        list containing that one entry point when queried for the
        ``clickwork.commands`` group (and an empty list for any other group,
        so unrelated lookups elsewhere in the interpreter are not affected).
        """

        class FakeEntryPoint:
            # Matches the shape importlib.metadata.EntryPoint exposes: a
            # ``.name`` attribute and a ``.load()`` method. That's all
            # discover_commands_from_entrypoints and LazyEntryPointCommand
            # actually use from the real EntryPoint API.
            name = ""

            def load(self) -> click.Command:
                return command

        ep = FakeEntryPoint()
        ep.name = name

        def _fake_entry_points(*, group: str | None = None):
            # Only surface the stub for the clickwork group so the patch
            # can't accidentally pollute unrelated metadata queries.
            if group == "clickwork.commands":
                return [ep]
            return []

        monkeypatch.setattr("importlib.metadata.entry_points", _fake_entry_points)

    def test_local_command_shadows_installed_entry_point(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Local directory command must win over a same-named entry point.

        Stubs ``importlib.metadata.entry_points`` to return a fake EP
        named ``shared`` whose command echoes ``installed-won``, then
        plants a file at ``<tmp_path>/shared.py`` whose command echoes
        ``local-won``. After ``discover_commands(commands_dir=tmp_path,
        discovery_mode="auto")``, the returned dict's ``"shared"``
        entry must be the local one, verified by actually invoking it
        and checking the output string. If the merge order ever flipped,
        the output would change to ``installed-won`` and the test would
        fail.
        """
        from clickwork.discovery import discover_commands

        @click.command(name="shared")
        def installed_shared() -> None:
            # The installed-side command that we EXPECT to be shadowed.
            # If this message ever appears in the test output, the
            # shadowing contract has broken.
            click.echo("installed-won")

        self._stub_entry_points(monkeypatch, "shared", installed_shared)

        # Plant a local commands/shared.py that exports a Click command
        # with the SAME NAME ("shared"). The discovery code keys on the
        # Click command's ``.name`` attribute, so the module filename
        # alone isn't enough to collide -- the exported cli object's
        # name must match. Here @click.command() defaults name="shared"
        # from the function name, which is what we want.
        (tmp_path / "shared.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def shared() -> None:\n"
            "    click.echo('local-won')\n\n"
            "cli = shared\n"
        )

        commands = discover_commands(commands_dir=tmp_path, discovery_mode="auto")

        assert "shared" in commands
        # Identity check: the returned command must NOT be the installed
        # one. This catches the subtle case where both ended up in the
        # dict but the installed reference was held somewhere else.
        assert commands["shared"] is not installed_shared

        # Output check: invoke the merged command and confirm the local
        # version's stdout text, not the installed version's. This is the
        # end-to-end proof of shadowing from the caller's perspective.
        runner = CliRunner()
        result = runner.invoke(commands["shared"], [])
        assert result.exit_code == 0
        assert result.output.strip() == "local-won"

    def test_installed_command_wins_when_no_local_conflicts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Without a same-named local file, the installed entry point is kept.

        Pins the "don't over-shadow" half of the contract: shadowing only
        kicks in on name collision. When the local directory has nothing
        to shadow with, the installed command must appear in the merged
        dict AND be invocable.
        """
        from clickwork.discovery import discover_commands

        @click.command(name="solo")
        def installed_solo() -> None:
            # Name "solo" deliberately does not collide with any local file.
            click.echo("installed-solo")

        self._stub_entry_points(monkeypatch, "solo", installed_solo)

        # Create an empty commands dir -- exists so auto mode activates
        # directory scanning, but has no files so nothing collides.
        # WHY the dir must exist: discover_commands() only enables the
        # directory branch in auto mode when ``commands_dir.is_dir()``
        # returns True. An empty existing dir exercises the "both
        # mechanisms active, no collisions" path, which is the case we
        # actually want to pin here.
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()

        commands = discover_commands(commands_dir=commands_dir, discovery_mode="auto")

        assert "solo" in commands

        # Invoke via CliRunner to confirm the lazy proxy loads and runs
        # the installed command end-to-end, not just that it's in the dict.
        runner = CliRunner()
        result = runner.invoke(commands["solo"], [])
        assert result.exit_code == 0
        assert result.output.strip() == "installed-solo"

    def test_dev_mode_ignores_entry_points(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Dev mode must not include entry-point commands.

        Pins the mode-isolation contract for ``discovery_mode="dev"``:
        even when a plugin is installed and its entry point is visible
        via ``importlib.metadata``, dev mode skips the entry-point
        mechanism entirely and returns directory commands only.

        Note on naming: the task brief called this mode "directory", but
        the actual enum value accepted by discover_commands is "dev" --
        see discovery.py's docstring for the canonical names. The test
        uses the real value.
        """
        from clickwork.discovery import discover_commands

        @click.command(name="ep-only")
        def installed_cmd() -> None:
            click.echo("installed")

        self._stub_entry_points(monkeypatch, "ep-only", installed_cmd)

        # Plant a local command with a DIFFERENT name so the two sources
        # don't collide -- we want to see them side-by-side to confirm
        # that only the directory one survives, not just that one wins.
        # NB: we pass name="local-cmd" explicitly because Click auto-derives
        # a command name from the function and its normalisation rules
        # (underscores -> hyphens, trims ``_cmd`` / ``_command`` suffixes)
        # are surprising. Being explicit pins the dict key we assert on.
        (tmp_path / "local_cmd.py").write_text(
            "import click\n\n"
            "@click.command(name='local-cmd')\n"
            "def local_cmd() -> None:\n"
            "    click.echo('local')\n\n"
            "cli = local_cmd\n"
        )

        commands = discover_commands(commands_dir=tmp_path, discovery_mode="dev")

        # Local command is present.
        assert "local-cmd" in commands
        # Entry-point command must be ABSENT -- dev mode never loads it.
        assert "ep-only" not in commands

    def test_installed_mode_ignores_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Installed mode must not include directory commands.

        Symmetric counterpart to the dev-mode test: even when a valid
        ``commands/`` directory exists with working command files,
        ``discovery_mode="installed"`` returns entry-point commands only.

        Note on naming: the task brief called this mode "entrypoint", but
        the actual enum value is "installed". See discovery.py for the
        canonical names.
        """
        from clickwork.discovery import discover_commands

        @click.command(name="ep-cmd")
        def installed_cmd() -> None:
            click.echo("installed")

        self._stub_entry_points(monkeypatch, "ep-cmd", installed_cmd)

        # Plant a local file that dev/auto mode would pick up. Installed
        # mode must ignore it completely. Name is pinned explicitly to
        # avoid Click's auto-normalisation surprising the assertion.
        (tmp_path / "dir_cmd.py").write_text(
            "import click\n\n"
            "@click.command(name='dir-cmd')\n"
            "def dir_cmd() -> None:\n"
            "    click.echo('local')\n\n"
            "cli = dir_cmd\n"
        )

        commands = discover_commands(commands_dir=tmp_path, discovery_mode="installed")

        # Entry-point command is present.
        assert "ep-cmd" in commands
        # Directory command must be ABSENT -- installed mode never scans
        # the directory even when commands_dir points at a valid one.
        assert "dir-cmd" not in commands

    def test_shadowing_is_logged_at_info_level(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """The shadowing event must be announced on the clickwork logger at INFO.

        Pins the "informational, not silent" contract. Operators need to
        see WHEN a local file is shadowing an installed plugin so stale
        dev files don't invisibly hide the packaged behaviour. The log
        level is deliberately INFO (not WARNING) because shadowing is a
        normal, expected occurrence during dev -- but it must still be
        visible when INFO logging is enabled.
        """
        from clickwork.discovery import discover_commands

        @click.command(name="collide")
        def installed_cmd() -> None:
            click.echo("installed")

        self._stub_entry_points(monkeypatch, "collide", installed_cmd)

        (tmp_path / "collide.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def collide() -> None:\n"
            "    click.echo('local')\n\n"
            "cli = collide\n"
        )

        # caplog.at_level scoped to the clickwork logger -- the same logger
        # used by discovery.py (logger = logging.getLogger("clickwork")).
        # Scoping to the named logger avoids capturing unrelated INFO logs
        # from other libraries that might fire during the discovery pass.
        with caplog.at_level("INFO", logger="clickwork"):
            discover_commands(commands_dir=tmp_path, discovery_mode="auto")

        # Find the specific shadowing record. We look for INFO level AND
        # the load-bearing phrase, so an accidental WARNING-level log or
        # a different message wouldn't satisfy this assertion.
        matching = [
            r
            for r in caplog.records
            if r.name == "clickwork"
            and r.levelname == "INFO"
            and "shadows installed plugin command" in r.getMessage()
            and "collide" in r.getMessage()
        ]
        assert matching, (
            "expected an INFO-level 'shadows installed plugin command' log "
            f"from the clickwork logger mentioning the command name 'collide'; "
            f"got records: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
