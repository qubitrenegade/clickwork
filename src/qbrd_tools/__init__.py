"""qbrd-tools: Reusable CLI framework for project automation.

This package provides the building blocks for project-specific CLI tools.
It handles plugin discovery, layered config, subprocess management, and
common utilities so command authors can focus on business logic.

Public API:
    create_cli        - Build a CLI with global flags and plugin discovery
    CliContext         - Typed context object passed to every command
    pass_cli_context   - Decorator for commands (handles nested group footgun)
    Secret            - Redacted wrapper for sensitive config values
    CliProcessError   - Exception raised when subprocess fails
    PrerequisiteError - Exception raised when a required tool is missing
    ConfigError       - Exception raised when config validation fails
"""

__version__ = "0.1.0"

from qbrd_tools._types import CliContext, CliProcessError, PrerequisiteError, Secret
from qbrd_tools.cli import create_cli, pass_cli_context
from qbrd_tools.config import ConfigError

__all__ = [
    "create_cli",
    "CliContext",
    "pass_cli_context",
    "Secret",
    "CliProcessError",
    "ConfigError",
    "PrerequisiteError",
]
