"""Core data structures for the clickwork CLI framework.

This module is the foundation every other module imports from.  Keep it
dependency-free (stdlib only) so it can be safely imported anywhere.

Four types are defined here:
  - Secret            : wraps a sensitive string and redacts it in every repr path
  - CliProcessError   : converts subprocess.CalledProcessError into a rich exception
  - PrerequisiteError : raised when a required tool is missing or not authenticated
  - CliContext        : dataclass threaded through Click's ctx.obj to every command
"""
from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Secret
# ---------------------------------------------------------------------------

class Secret:
    """An opaque wrapper that prevents accidental logging of sensitive values.

    Design goals:
      1. str() / repr() / f-strings always emit "***" so log statements that
         format a CliContext never leak credentials.
      2. .get() is the single explicit escape hatch -- its name signals intent
         at call sites and makes grep-audits straightforward.
      3. __slots__ removes __dict__ entirely so vars(s) raises TypeError and
         iterating over an object's attributes cannot surface the value.
      4. Pickling is blocked because serialising to disk/network defeats the
         whole point of the wrapper.
      5. copy / deepcopy return a fresh Secret (same value) so object-graph
         copies remain safe.

    Usage:
        token = Secret(os.environ["API_TOKEN"])
        headers = {"Authorization": f"Bearer {token.get()}"}  # explicit .get()
        logging.info("request headers: %s", headers)          # *** in logs
    """

    # __slots__ prevents __dict__ from being created, so vars(secret) raises
    # TypeError and there is no attribute bag that could leak the value.
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap a sensitive string value in an opaque, redaction-safe container.

        Args:
            value: The raw secret string (e.g., an API token or password).
        """
        self._value = value

    # --- intentional public API ---

    def get(self) -> str:
        """Return the actual secret value.

        Calling .get() is a deliberate act: reviewers know that any code
        path reaching this line is handling sensitive material.

        Returns:
            The unwrapped secret string.
        """
        return self._value

    # --- redacted repr paths ---

    def __str__(self) -> str:
        """Return a redacted placeholder instead of the secret value.

        Called by str(), format(), f-strings, and %-style formatting.

        Returns:
            The literal string ``"***"``.
        """
        return "***"

    def __repr__(self) -> str:
        """Return a safe repr that omits the secret value.

        Called by repr(), debuggers, pytest output, and dataclass __repr__.
        Includes the type name so developers know what they're looking at,
        but never includes the value itself.

        Returns:
            The literal string ``"Secret(***)"``.
        """
        return "Secret(***)"

    def __format__(self, format_spec: str) -> str:
        """Return a redacted placeholder for ``format(secret, spec)`` and ``f"{secret}"``.

        This covers calls such as ``format(secret, spec)``, ``f"{secret}"``,
        and ``f"{secret:spec}"``. The ``!r`` f-string conversion is handled
        by ``__repr__`` before formatting, so ``f"{secret!r}"`` is protected
        by ``__repr__``, not by this method.

        Args:
            format_spec: The format specification string (ignored).

        Returns:
            The literal string ``"***"``.
        """
        return "***"

    # --- truthiness without value exposure ---

    def __bool__(self) -> bool:
        """Allow truthiness checks without exposing the secret value.

        Enables guard clauses like ``if ctx.token:`` without leaking the
        value. An empty string is falsy; any non-empty string is truthy.

        Returns:
            True if the wrapped value is non-empty, False otherwise.
        """
        return bool(self._value)

    # --- serialisation block ---

    def __reduce__(self):
        """Block pickling to prevent accidental serialisation of secrets.

        pickle calls __reduce__ to determine how to serialise an object.
        Raising TypeError here stops pickle.dumps() before it writes anything
        to disk or the network.

        Raises:
            TypeError: Always -- Secret instances cannot be pickled.
        """
        raise TypeError(
            "Secret cannot be pickled -- serialising secrets is unsafe. "
            "Use Secret.get() to retrieve the value and handle it explicitly."
        )

    # --- safe copy semantics ---

    def __copy__(self) -> "Secret":
        """Return a new Secret wrapping the same value.

        copy.copy() calls __copy__ when available. We return a new Secret
        so the copy is still opaque and __slots__-protected.

        Returns:
            A new Secret instance with the same underlying value.
        """
        return Secret(self._value)

    def __deepcopy__(self, memo: dict) -> "Secret":
        """Return a new Secret wrapping the same value (deep copy is shallow here).

        copy.deepcopy() follows __deepcopy__. Strings are immutable, so a
        shallow copy of the value is always correct and avoids any attempt
        to recurse into the slot.

        Args:
            memo: The deepcopy memo dictionary (unused, but required by protocol).

        Returns:
            A new Secret instance with the same underlying value.
        """
        return Secret(self._value)


# ---------------------------------------------------------------------------
# CliProcessError
# ---------------------------------------------------------------------------

class CliProcessError(Exception):
    """A subprocess failure wrapped with enough context to act on.

    subprocess.CalledProcessError carries the raw data but its __str__ is
    terse and its fields are loosely typed.  This wrapper:
      - Exposes returncode, cmd, and stderr as first-class attributes.
      - Produces a human-readable message that is immediately actionable in
        logs and pytest output without needing to inspect the cause chain.
      - Remains a plain Exception subclass so callers can `raise` / `except` it
        using standard Python idioms.

    Usage:
        try:
            subprocess.run(["git", "push"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise CliProcessError(e) from e
    """

    def __init__(self, cause: subprocess.CalledProcessError) -> None:
        """Wrap a CalledProcessError with a human-readable message.

        Extracts the fields callers care about (returncode, cmd, stderr) and
        composes a single actionable message so that str(err) immediately
        shows what failed and why, without needing to unwrap the cause chain.

        Args:
            cause: The original subprocess.CalledProcessError to wrap.
        """
        # Store the fields we care about directly so callers don't have to
        # reach into the cause chain after catching this exception.
        self.returncode: int = cause.returncode
        self.cmd: str | list[str] = cause.cmd
        self.stderr: str = cause.stderr or ""

        # Build the user-facing message once at construction time.
        # Format: "Command failed (exit N): <cmd>\n<stderr>" (stderr optional)
        cmd_str = self.cmd if isinstance(self.cmd, str) else " ".join(self.cmd)
        message = f"Command failed (exit {self.returncode}): {cmd_str}"
        if self.stderr:
            message = f"{message}\n{self.stderr}"

        # Pass the composed message to Exception so str(err) and repr(err)
        # both show the full context without extra unwrapping.
        super().__init__(message)


# ---------------------------------------------------------------------------
# PrerequisiteError
# ---------------------------------------------------------------------------

class PrerequisiteError(Exception):
    """Raised when a required tool is missing or not authenticated.

    Commands call ctx.require("docker") at the top of their function body.
    If the binary is missing or not authenticated, require() raises this
    exception. The framework's error handler catches it and exits with
    code 1 (user error) -- the same as CliProcessError.

    Raising instead of sys.exit() lets callers catch and recover if they
    want to (e.g., fall back to an alternative tool), and keeps require()
    composable and testable.
    """


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def normalize_prefix(name: str) -> str:
    """Convert a project/CLI name to a shell-safe env-var prefix.

    Hyphens become underscores and the result is uppercased so the prefix
    conforms to POSIX env-var naming rules (e.g., ``orbit-admin`` ->
    ``ORBIT_ADMIN``).  Used by both the CLI harness (for ``{PREFIX}_ENV``
    resolution) and the config loader (for auto-prefixed env vars).

    Args:
        name: A project or CLI name, possibly containing hyphens.

    Returns:
        An uppercase, underscore-delimited prefix string.
    """
    return name.replace("-", "_").upper()


# ---------------------------------------------------------------------------
# CliContext
# ---------------------------------------------------------------------------

@dataclass
class CliContext:
    """Shared runtime state threaded through every Click command via ctx.obj.

    CliContext flows from the top-level Click group down to every sub-command.
    It carries:
      - Parsed flags (dry_run, verbose, quiet, yes) -- so sub-commands don't
        need to re-declare the same Click options.
      - A resolved config dict -- populated from layered config files by the
        CLI harness before any command handler runs.
      - A bound logger -- so commands log consistently with the same formatter
        and level set by the top-level --verbose / --quiet flags.
      - Six optional callable fields (run, capture, ...) -- injected by the
        CLI harness to abstract over dry-run / subprocess semantics.  They
        default to None so the dataclass can be constructed cheaply in tests
        without a full harness setup.

    The callable fields use repr=False, compare=False because:
      - repr=False: lambdas have unhelpful repr strings that would clutter
        debug output; the dataclass repr is already redacted via logger repr.
      - compare=False: two contexts with identical config but different
        callable bindings should compare equal for assertion purposes.

    Usage (CLI harness):
        @click.pass_context
        def cli(ctx: click.Context, dry_run: bool, verbose: int) -> None:
            ctx.ensure_object(dict)
            ctx.obj = CliContext(
                config=load_config(),
                dry_run=dry_run,
                verbose=verbose,
                ...
            )

    Usage (command):
        @click.pass_obj
        def deploy(ctx: CliContext) -> None:
            if ctx.dry_run:
                ctx.logger.info("[dry-run] would deploy")
            else:
                ctx.run(["kubectl", "apply", "-f", "manifests/"])
    """

    # --- core configuration ---

    config: dict = field(default_factory=dict)
    """Merged config from layered TOML files, keyed by string."""

    env: str | None = None
    """Active deployment environment (e.g. "staging", "prod"), or None."""

    # --- global flags ---

    dry_run: bool = False
    """When True, commands should describe actions but not execute them."""

    verbose: int = 0
    """Verbosity level: 0=normal, 1=-v, 2=-vv, etc."""

    quiet: bool = False
    """When True, suppress all output except errors."""

    yes: bool = False
    """When True, skip interactive confirmation prompts."""

    # --- logging ---

    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("clickwork"))
    """Configured logger instance."""

    # --- injectable subprocess helpers ---
    # These are None by default so CliContext can be constructed in tests
    # without wiring up a full subprocess harness.  The CLI harness injects
    # concrete implementations before dispatching to command handlers.

    run: Callable[..., subprocess.CompletedProcess | None] | None = field(
        default=None, repr=False, compare=False,
    )
    """Run a command, inheriting stdio. Raises CliProcessError on failure."""

    capture: Callable[..., str] | None = field(
        default=None, repr=False, compare=False,
    )
    """Run a command and return stripped stdout as a string."""

    require: Callable[..., None] | None = field(
        default=None, repr=False, compare=False,
    )
    """Assert that a binary exists on PATH, raising PrerequisiteError if not.

    Tests can intercept this helper by patching ``clickwork.prereqs.require``
    (the public symbol). The CLI harness binds ``ctx.require`` through a
    lazy lambda that re-reads the module attribute on every call, so
    ``unittest.mock.patch("clickwork.prereqs.require")`` transparently takes
    effect -- there is no need to reach for internal symbols like
    ``clickwork.cli._require`` (issue #8).
    """

    confirm: Callable[..., bool] | None = field(
        default=None, repr=False, compare=False,
    )
    """Prompt the user for confirmation unless --yes is set."""

    confirm_destructive: Callable[..., bool] | None = field(
        default=None, repr=False, compare=False,
    )
    """Like confirm, but adds extra warnings for irreversible operations."""

    run_with_confirm: Callable[..., subprocess.CompletedProcess | None] | None = field(
        default=None, repr=False, compare=False,
    )
    """Confirm then run: wraps confirm() + run() in a single call."""
