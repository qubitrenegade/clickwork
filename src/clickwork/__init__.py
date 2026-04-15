"""clickwork: Reusable CLI framework for project automation.

This package provides the building blocks for project-specific CLI tools.
It handles plugin discovery, layered config, subprocess management, and
common utilities so command authors can focus on business logic.

Public API:
    create_cli        - Build a CLI with global flags and plugin discovery
    load_config       - Load layered TOML config (for custom config scenarios)
    CliContext         - Typed context object passed to every command
    pass_cli_context   - Decorator for commands (handles nested group footgun)
    Secret            - Redacted wrapper for sensitive config values
    CliProcessError   - Exception raised when subprocess fails
    PrerequisiteError - Exception raised when a required tool is missing
    ConfigError       - Exception raised when config validation fails
"""

__version__ = "0.1.0"

from clickwork._types import CliContext, CliProcessError, PrerequisiteError, Secret, normalize_prefix
from clickwork.cli import create_cli, pass_cli_context
from clickwork.config import ConfigError, load_config

__all__ = [
    "create_cli",
    "load_config",
    "CliContext",
    "pass_cli_context",
    "Secret",
    "CliProcessError",
    "ConfigError",
    "PrerequisiteError",
    "normalize_prefix",
]
