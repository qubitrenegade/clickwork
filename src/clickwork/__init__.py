"""clickwork: Reusable CLI framework for project automation.

This package provides the building blocks for project-specific CLI tools.
It handles plugin discovery, layered config, subprocess management, and
common utilities so command authors can focus on business logic.

Public API:
    create_cli        - Build a CLI with global flags and plugin discovery
    add_global_option - Install a Click option at root + every group + every subcommand
    load_config       - Load layered TOML config (for custom config scenarios)
    CliContext         - Typed context object passed to every command
    pass_cli_context   - Decorator for commands (handles nested group footgun)
    Secret            - Redacted wrapper for sensitive config values
    CliProcessError   - Exception raised when subprocess fails
    PrerequisiteError - Exception raised when a required tool is missing
    ConfigError       - Exception raised when config validation fails
    HttpError         - Exception raised when an HTTP call returns non-2xx
    ClickworkDiscoveryError - Exception raised when strict discovery finds
                              a broken import, missing cli attribute, or
                              similar command-discovery failure (opt-in via
                              create_cli(..., strict=True))
    platform_dispatch - Decorator that routes a command to a per-OS impl
    platform          - Submodule exposing dispatch(), is_linux/macos/windows
    http              - Submodule exposing get/post/put/delete + HttpError
    testing           - Submodule exposing run_cli() + make_test_cli() helpers
"""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _metadata_version

# WHY ``testing`` is imported here alongside ``http`` / ``platform``: all
# three are advertised as importable both as ``clickwork.<name>`` and as
# ``from clickwork import <name>``. The top-level import makes the
# attribute-on-package form work without relying on implicit submodule
# resolution (which doesn't fire until the user imports the submodule
# explicitly somewhere else first).
from clickwork import http, platform, testing
from clickwork._types import (
    CliContext,
    CliProcessError,
    PrerequisiteError,
    Secret,
    normalize_prefix,
)
from clickwork.cli import create_cli, pass_cli_context
from clickwork.config import ConfigError, load_config
from clickwork.discovery import ClickworkDiscoveryError
from clickwork.global_options import add_global_option
from clickwork.http import HttpError, delete, get, post, put
from clickwork.platform import platform_dispatch

# Single source of truth: derive the runtime version from the installed
# package metadata so pyproject.toml is authoritative and we can't drift
# the two again (this bit us during the 1.0 cut -- pyproject said 1.0.0,
# __init__ still said 0.2.0, and --version reported the stale value).
# PackageNotFoundError covers the "running from an uninstalled source
# checkout" edge case -- shouldn't happen for ordinary consumers, but
# falling back to "0+unknown" keeps `clickwork.__version__` available
# instead of raising on import.
try:
    __version__ = _metadata_version("clickwork")
except _PackageNotFoundError:  # pragma: no cover - editable/uninstalled checkout
    __version__ = "0+unknown"

del _metadata_version, _PackageNotFoundError

__all__ = [
    "create_cli",
    "add_global_option",
    "load_config",
    "CliContext",
    "pass_cli_context",
    "Secret",
    "CliProcessError",
    "ConfigError",
    "PrerequisiteError",
    "HttpError",
    "ClickworkDiscoveryError",
    "normalize_prefix",
    "platform",
    "platform_dispatch",
    "http",
    "get",
    "post",
    "put",
    "delete",
    "testing",
]
