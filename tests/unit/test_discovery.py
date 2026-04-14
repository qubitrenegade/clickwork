"""Tests for plugin discovery.

Discovery finds Click commands from two sources:
1. Directory scanning: imports .py files from a commands/ dir, looks for 'cli' export
2. Entry points: reads the 'qbrd_tools.commands' entry point group

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
import pytest


class TestDirectoryScanning:
    """Directory scanning imports .py files and looks for 'cli' attribute."""

    def test_discovers_command_from_file(self, tmp_path: Path):
        """A .py file with a 'cli' attribute should be discovered."""
        from qbrd_tools.discovery import discover_commands_from_dir

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

    def test_skips_files_without_cli_export(self, tmp_path: Path, capsys):
        """Files without 'cli' attribute produce a warning, not an error."""
        from qbrd_tools.discovery import discover_commands_from_dir

        helper = tmp_path / "utils.py"
        helper.write_text("# Just a helper module\nHELPER = True\n")

        commands = discover_commands_from_dir(tmp_path)
        assert commands == {}
        captured = capsys.readouterr()
        assert "utils.py" in captured.err

    def test_skips_subdirectories(self, tmp_path: Path):
        """Subdirectories (like lib/) should not be scanned."""
        from qbrd_tools.discovery import discover_commands_from_dir

        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "helper.py").write_text("import click\ncli = click.command()(lambda: None)\n")

        commands = discover_commands_from_dir(tmp_path)
        assert "helper" not in commands

    def test_skips_init_files(self, tmp_path: Path):
        from qbrd_tools.discovery import discover_commands_from_dir

        (tmp_path / "__init__.py").write_text("")

        commands = discover_commands_from_dir(tmp_path)
        assert commands == {}

    def test_discovers_click_group(self, tmp_path: Path):
        """A file exporting a click.Group should become a subcommand group."""
        from qbrd_tools.discovery import discover_commands_from_dir

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
        from qbrd_tools.discovery import discover_commands_from_dir

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
        from qbrd_tools.discovery import discover_commands_from_dir

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

    def test_handles_import_error_gracefully(self, tmp_path: Path, capsys):
        """A command file that fails to import should warn, not crash."""
        from qbrd_tools.discovery import discover_commands_from_dir

        broken = tmp_path / "broken.py"
        broken.write_text("import nonexistent_module_xyz123\n")

        commands = discover_commands_from_dir(tmp_path)
        assert commands == {}
        captured = capsys.readouterr()
        assert "broken.py" in captured.err

    def test_handles_syntax_error_gracefully(self, tmp_path: Path, capsys):
        """A command file with syntax errors should warn, not crash."""
        from qbrd_tools.discovery import discover_commands_from_dir

        broken = tmp_path / "bad_syntax.py"
        broken.write_text("def broken(\n")

        commands = discover_commands_from_dir(tmp_path)
        assert commands == {}
        captured = capsys.readouterr()
        assert "bad_syntax.py" in captured.err


class TestNamespaceIsolation:
    """Discovery namespaces must not leak between different command dirs."""

    def test_same_filename_in_two_dirs_gets_different_modules(self, tmp_path: Path):
        """Two dirs with the same helper.py should yield independent modules.

        WHY: discover_commands_from_dir() registers modules in sys.modules.
        If the namespace is flat (qbrd_tools._discovered.helper), the second
        scan gets the cached first helper from sys.modules -- silently
        loading the wrong code.
        """
        from qbrd_tools.discovery import discover_commands_from_dir
        from click.testing import CliRunner

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
        from qbrd_tools.discovery import discover_commands

        cmd_file = tmp_path / "hello.py"
        cmd_file.write_text(
            "import click\n\n"
            "@click.command()\n"
            "def hello():\n"
            "    click.echo('hi')\n\n"
            "cli = hello\n"
        )

        commands = discover_commands(
            commands_dir=tmp_path,
            discovery_mode="dev",
        )
        assert "hello" in commands

    def test_auto_mode_uses_dir_when_exists(self, tmp_path: Path):
        from qbrd_tools.discovery import discover_commands

        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "status.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def status():\n"
            "    pass\n\n"
            "cli = status\n"
        )

        commands = discover_commands(
            commands_dir=commands_dir,
            discovery_mode="auto",
        )
        assert "status" in commands

    def test_auto_mode_falls_back_to_entrypoints(self, tmp_path: Path):
        """When commands_dir doesn't exist, auto mode still uses entry points."""
        from qbrd_tools.discovery import discover_commands

        # Point at a nonexistent directory -- should fall back gracefully.
        commands = discover_commands(
            commands_dir=tmp_path / "nonexistent",
            discovery_mode="auto",
        )
        # No entry points installed in test env, so should be empty.
        assert isinstance(commands, dict)

    def test_installed_mode_ignores_directory(self, tmp_path: Path):
        """In installed mode, commands_dir is ignored even if it exists."""
        from qbrd_tools.discovery import discover_commands

        # Create a commands dir with a real command file
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "local.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def local():\n"
            "    pass\n\n"
            "cli = local\n"
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
        from qbrd_tools.discovery import discover_commands_from_entrypoints

        loaded = {"called": False}

        class FakeEntryPoint:
            name = "hello"

            def load(self):
                loaded["called"] = True
                return click.command(name="hello")(lambda: None)

        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda group=None: [FakeEntryPoint()] if group == "qbrd_tools.commands" else [],
        )

        commands = discover_commands_from_entrypoints()
        assert "hello" in commands
        assert loaded["called"] is False

    def test_lazy_proxy_forwards_options_and_arguments(self, monkeypatch):
        from qbrd_tools.discovery import discover_commands_from_entrypoints

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
            lambda group=None: [FakeEntryPoint()] if group == "qbrd_tools.commands" else [],
        )

        commands = discover_commands_from_entrypoints()
        runner = CliRunner()
        result = runner.invoke(commands["hello"], ["--foo", "bar", "alice"])

        assert result.exit_code == 0
        assert result.output.strip() == "bar:alice"

    def test_local_command_shadow_logs_at_info(self, tmp_path: Path, monkeypatch, caplog):
        from qbrd_tools.discovery import discover_commands

        installed = click.command(name="hello")(lambda: None)
        monkeypatch.setattr(
            "qbrd_tools.discovery.discover_commands_from_entrypoints",
            lambda: {"hello": installed},
        )

        (tmp_path / "hello.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def hello():\n"
            "    pass\n\n"
            "cli = hello\n"
        )

        with caplog.at_level("INFO", logger="qbrd_tools"):
            commands = discover_commands(commands_dir=tmp_path, discovery_mode="auto")
        assert "hello" in commands
        assert "shadows installed plugin command" in caplog.text
