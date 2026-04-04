"""Core data structures for the qbrd-tools CLI framework.

This module is the foundation every other module imports from.  Keep it
dependency-free (stdlib only) so it can be safely imported anywhere.

Three types are defined here:
  - Secret      : wraps a sensitive string and redacts it in every repr path
  - CliProcessError : converts subprocess.CalledProcessError into a rich exception
  - CliContext  : dataclass threaded through Click's ctx.obj to every command
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


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

    # __slots__ prevents a __dict__ from being created for instances.
    # Without __dict__ there is no attribute-bag that could leak _value.
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap a sensitive string value in an opaque, redaction-safe container.

        Args:
            value: The raw secret string (e.g., an API token or password).
        """
        # Store the real value under a name-mangled-ish private slot.
        # The leading underscore is a convention; the slots guard is the
        # real protection.
        object.__setattr__(self, "_value", value)

    # --- intentional public API ---

    def get(self) -> str:
        """Return the actual secret value.

        Calling .get() is a deliberate act: reviewers know that any code
        path reaching this line is handling sensitive material.

        Returns:
            The unwrapped secret string.
        """
        return object.__getattribute__(self, "_value")

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
            The literal string ``"Secret(***)""``.
        """
        return "Secret(***)"

    def __format__(self, format_spec: str) -> str:
        """Return a redacted placeholder for all format-spec paths.

        f"{secret}" calls __format__("") first, falling back to __str__ only
        if __format__ is absent. We override it explicitly so that format
        specs like f"{secret!r}" also return a safe string.

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
        return bool(object.__getattribute__(self, "_value"))

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
        return Secret(object.__getattribute__(self, "_value"))

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
        return Secret(object.__getattribute__(self, "_value"))


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
    """Merged config from layered TOML/YAML files, keyed by string."""

    env: Optional[str] = None
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

    logger: Any = field(default_factory=lambda: logging.getLogger("qbrd_tools"))
    """Configured logger instance.  Accepts Any to avoid coupling to Logger."""

    # --- injectable subprocess helpers ---
    # These are None by default so CliContext can be constructed in tests
    # without wiring up a full subprocess harness.  The CLI harness injects
    # concrete implementations before dispatching to command handlers.

    run: Optional[Callable] = field(default=None, repr=False, compare=False)
    """Run a command, inheriting stdio.  Raises CliProcessError on failure."""

    capture: Optional[Callable] = field(default=None, repr=False, compare=False)
    """Run a command and return stripped stdout as a string."""

    require: Optional[Callable] = field(default=None, repr=False, compare=False)
    """Assert that a binary exists on PATH, raising a clear error if not."""

    confirm: Optional[Callable] = field(default=None, repr=False, compare=False)
    """Prompt the user for confirmation unless --yes is set."""

    confirm_destructive: Optional[Callable] = field(default=None, repr=False, compare=False)
    """Like confirm, but adds extra warnings for irreversible operations."""

    run_with_confirm: Optional[Callable] = field(default=None, repr=False, compare=False)
    """Confirm then run: wraps confirm() + run() in a single call."""
