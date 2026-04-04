"""Tests for plugin discovery.

Discovery finds Click commands from two sources:
1. Directory scanning: imports .py files from a commands/ dir, looks for 'cli' export
2. Entry points: reads the 'qbrd_tools.commands' entry point group

The discovery_mode parameter controls which mechanisms are active:
- "dev": directory scanning only
- "installed": entry points only
- "auto": directory scanning if commands_dir exists, else entry points

Installed-mode discovery is intentionally lazy: entry points are wrapped in
lightweight Click proxies so startup and unrelated commands don't import every
installed plugin up front. The real command object loads on invocation, and on
`--help` when Click asks for command metadata.
"""
from pathlib import Path

import click
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
        assert isinstance(commands["greet"], click.BaseCommand)

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
        assert isinstance(commands["deploy"], click.MultiCommand)

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
        (commands_dir / "test_cmd.py").write_text(
            "import click\n\n"
            "@click.command()\n"
            "def test_cmd():\n"
            "    pass\n\n"
            "cli = test_cmd\n"
        )

        commands = discover_commands(
            commands_dir=commands_dir,
            discovery_mode="auto",
        )
        assert "test_cmd" in commands

    def test_auto_mode_falls_back_to_entrypoints(self, tmp_path: Path):
        """When commands_dir doesn't exist, auto mode uses entry points."""
        from qbrd_tools.discovery import discover_commands

        # Point at a nonexistent directory -- should fall back gracefully.
        commands = discover_commands(
            commands_dir=tmp_path / "nonexistent",
            discovery_mode="auto",
        )
        # No entry points installed in test env, so should be empty.
        assert isinstance(commands, dict)


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

        with caplog.at_level("INFO"):
            commands = discover_commands(commands_dir=tmp_path, discovery_mode="auto")
        assert "hello" in commands
        assert "shadows installed plugin command" in caplog.text
