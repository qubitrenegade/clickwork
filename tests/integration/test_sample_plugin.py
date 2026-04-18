"""Integration test for the sample plugin.

Installs the sample plugin into the test environment and verifies it's
discoverable via entry points. This test is slower (subprocess pip install)
but catches real packaging issues.

Marked ``network`` because pip install needs to download build dependencies
(hatchling) from PyPI. Skip with ``pytest -m "not network"`` in sandboxed
or offline environments.
"""

import subprocess
import sys

import pytest


class TestSamplePlugin:
    """Verify sample-plugin commands work when installed via entry points."""

    @pytest.mark.network
    def test_hello_greet_via_installed_entrypoint(self, tmp_path):
        """Install the fixture into a temp venv and invoke it via installed mode."""
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        sample_plugin = project_root / "tests" / "fixtures" / "sample-plugin"
        venv_dir = tmp_path / "venv"

        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        if sys.platform == "win32":
            python = venv_dir / "Scripts" / "python.exe"
        else:
            python = venv_dir / "bin" / "python"

        # Install the framework from the checkout plus the sample plugin fixture.
        subprocess.run([str(python), "-m", "pip", "install", "-e", str(project_root)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", str(sample_plugin)], check=True)

        script = """
from click.testing import CliRunner
from clickwork.cli import create_cli

cli = create_cli(name="test-cli", discovery_mode="installed")
result = CliRunner().invoke(cli, ["hello", "greet", "World"])
print(result.output, end="")
raise SystemExit(result.exit_code)
"""
        result = subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Hello, World!" in result.stdout

    @pytest.mark.network
    def test_installed_help_lists_sample_plugin_command(self, tmp_path):
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        sample_plugin = project_root / "tests" / "fixtures" / "sample-plugin"
        venv_dir = tmp_path / "venv"

        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        if sys.platform == "win32":
            python = venv_dir / "Scripts" / "python.exe"
        else:
            python = venv_dir / "bin" / "python"

        subprocess.run([str(python), "-m", "pip", "install", "-e", str(project_root)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", str(sample_plugin)], check=True)

        script = """
from click.testing import CliRunner
from clickwork.cli import create_cli

cli = create_cli(name="test-cli", discovery_mode="installed")
result = CliRunner().invoke(cli, ["--help"])
print(result.output, end="")
raise SystemExit(result.exit_code)
"""
        result = subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "hello" in result.stdout
