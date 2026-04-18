"""Subprocess helpers for clickwork commands.

Three main functions:
- run(cmd): Execute a mutating command (deploy, build, push). Streams output
  in real-time, raises CliProcessError on failure, respects --dry-run.
- capture(cmd): Execute a read-only command and return stripped stdout. Always
  runs, even in dry-run mode, because commands need the data to proceed.
- run_with_confirm(cmd, message): Prompt before executing a destructive command.
  Combines confirmation + execution in one call.

All accept argv-style lists only (never strings) to prevent shell injection.
Secrets should be passed via the env parameter, not as argv arguments, because
argv is visible in `ps` output.

Signal handling: when the user presses Ctrl-C, the framework forwards SIGINT to
the child process, waits for it to exit, then re-raises KeyboardInterrupt so the
caller sees the interruption only after the child has had a chance to clean up.
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess

from clickwork._types import CliProcessError, Secret
from clickwork.prompts import confirm as _prompt_confirm

logger = logging.getLogger("clickwork")

# How long to wait for a child process to exit after forwarding SIGINT
# before escalating to SIGKILL. Long enough for graceful shutdown of most
# deploy/build commands, short enough to not leave the user staring at a
# frozen terminal.
SIGINT_TIMEOUT_SECONDS = 10


def _validate_cmd(cmd: list[str] | str) -> None:
    """Reject string commands to prevent shell injection.

    Accepting a raw string like "echo hello" would require shell=True, which
    opens the door to injection (e.g., "echo hello; rm -rf /"). Enforcing a
    list forces callers to be explicit about each argument boundary.

    Args:
        cmd: The command to validate. Must be a ``list[str]``; raises if it
            is a string, tuple, or any other type.

    Raises:
        TypeError: If cmd is not a list.
    """
    if not isinstance(cmd, list):
        raise TypeError(
            f"cmd must be a list, not {type(cmd).__name__}. Got: {cmd!r}. "
            "Use ['echo', 'hello'] instead of 'echo hello' to prevent shell injection."
        )


def _build_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Merge extra env vars with os.environ, or return None.

    Returning None (not an empty dict) when no extra vars are provided lets
    subprocess inherit the full parent environment via the default env=None
    path, which is what most processes expect (PATH, HOME, etc.).

    Args:
        env: Additional environment variables to layer on top of os.environ,
            or None to use the inherited environment unchanged.

    Returns:
        A merged dict of os.environ plus any caller-supplied vars, or None
        if no extra vars were provided.
    """
    if env is not None:
        # Spread os.environ first so caller-supplied vars win on conflict.
        # This is the safest default: commands see all the usual env vars
        # plus whatever secrets the caller injected.
        return {**os.environ, **env}
    return None


def _format_cmd(cmd: list[str]) -> str:
    """Format a command list as a shell-ready string for display.

    On POSIX platforms, uses ``shlex.quote`` so each argument is rendered
    in a form suitable for pasting into a POSIX shell.  On Windows, uses
    ``subprocess.list2cmdline`` which follows cmd.exe quoting conventions.

    Args:
        cmd: The command as an argv list.

    Returns:
        A single string suitable for logging or dry-run output.
    """
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(arg) for arg in cmd)


def _wait_with_signal_forwarding(proc: subprocess.Popen) -> int:
    """Wait for a child process, forwarding SIGINT before re-raising.

    This preserves Ctrl-C semantics for long-running deploy/build commands:
    the child gets a chance to handle SIGINT and clean up before the parent
    aborts. Without this, Python would raise KeyboardInterrupt immediately and
    leave the child running in the background as an orphan.

    If the child does not exit within SIGINT_TIMEOUT_SECONDS after receiving
    SIGINT, the framework escalates to SIGKILL to prevent indefinite hangs
    (e.g., a child that catches and ignores SIGINT).

    Args:
        proc: The running subprocess to wait on.

    Returns:
        The process exit code (0 for success, non-zero for failure).

    Raises:
        KeyboardInterrupt: After forwarding SIGINT to the child and waiting
            for it to exit, so the caller sees the interruption only once
            the child has cleaned up.
    """
    try:
        # Block until the child exits normally.
        return proc.wait()
    except KeyboardInterrupt:
        # User pressed Ctrl-C. Tell the child to stop gracefully via SIGINT
        # (the same signal the terminal sent us), then wait for it to exit
        # before propagating the interruption upward.
        try:
            proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            # The child may already have exited by the time we forward SIGINT.
            pass
        try:
            proc.wait(timeout=SIGINT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # The child ignored SIGINT for too long. Escalate to SIGKILL so
            # we don't hang forever waiting for a process that won't exit.
            proc.kill()
            proc.wait()
        except (ProcessLookupError, OSError):
            # If the process is already gone, the important part is that we
            # still re-raise KeyboardInterrupt for the caller.
            pass
        raise



def _validate_stdin_params(
    stdin_text: str | None, stdin_bytes: bytes | None
) -> None:
    """Enforce mutual exclusivity between stdin_text and stdin_bytes.

    WHY two separate kwargs instead of one polymorphic stdin=str|bytes:
    self-documenting call sites. ``run(cmd, stdin_text=token)`` is
    unambiguous; ``run(cmd, stdin=token)`` forces the reader to check
    the type to know whether the child sees text or bytes. Splitting the
    parameter makes the intent explicit at every call site. The cost is
    this one-time validation that the caller didn't pass both.

    Args:
        stdin_text: Text payload for the child's stdin, or None.
        stdin_bytes: Binary payload for the child's stdin, or None.

    Raises:
        ValueError: If both parameters are set. Passing neither is fine
            (it means "don't attach anything to stdin").
    """
    if stdin_text is not None and stdin_bytes is not None:
        raise ValueError(
            "Pass stdin_text OR stdin_bytes, not both. "
            "Use stdin_text for UTF-8 strings; use stdin_bytes for raw bytes."
        )


def run(
    cmd: list[str],
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    *,
    stdin_text: str | None = None,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess | None:
    """Execute a command, streaming output in real-time.

    Args:
        cmd: Command as an argv list. Strings are rejected (TypeError).
        dry_run: If True, print the command but don't execute it.
        env: Extra environment variables merged with os.environ. Use this
            for secrets instead of putting them in cmd (argv is visible in ps).
        stdin_text: If set, encode this string as UTF-8 and pipe the bytes
            to the child's stdin. Mutually exclusive with stdin_bytes.
            (Implementation note: the child's stdin is always opened in
            binary mode and we encode here -- this avoids platform-locale
            surprises and newline translation, so secret/token values
            arrive byte-exact regardless of OS.)
        stdin_bytes: If set, pipe these raw bytes to the child's stdin.
            Mutually exclusive with stdin_text.

    Returns:
        subprocess.CompletedProcess on success, or None if dry_run=True.

    Raises:
        CliProcessError: If the command exits with non-zero status.
        TypeError: If cmd is a string instead of a list.
        ValueError: If both stdin_text and stdin_bytes are set.

    Passing data on stdin (secrets-via-stdin):
        Many tools accept a secret on stdin so it never appears in argv
        (which is visible in ``ps`` output). Common examples:

        - ``wrangler secret put API_KEY`` reads the secret from stdin
        - ``gh auth login --with-token`` reads the token from stdin
        - ``docker login --password-stdin`` reads the password from stdin

        Use ``stdin_text`` for UTF-8 text (the common case), or
        ``stdin_bytes`` for raw binary payloads. Never pass secrets via
        ``cmd`` (argv is world-readable in ``ps``); prefer ``env`` for
        env-var-based secrets and ``stdin_text`` for stdin-based ones.

        Example (calling this module-level function directly)::

            run(["wrangler", "secret", "put", "API_KEY"], stdin_text=token)

        Via CliContext::

            ctx.run(["wrangler", "secret", "put", "API_KEY"], stdin_text=token)
    """
    _validate_cmd(cmd)
    # Validate stdin mutual exclusivity BEFORE the dry_run short-circuit so
    # callers catch the programming mistake in both live and dry-run modes.
    _validate_stdin_params(stdin_text, stdin_bytes)

    if dry_run:
        # Log what would have run so dry-run mode is still informative.
        logger.info("[dry-run] Would execute: %s", _format_cmd(cmd))
        return None

    full_env = _build_env(env)

    # Decide whether we need to attach a stdin pipe, and if so, normalize
    # the payload to raw bytes. Only one of stdin_text or stdin_bytes can
    # be set (enforced above).
    #
    # WHY always bytes on the wire (even for stdin_text): Popen(text=True)
    # would wrap proc.stdin in a TextIOWrapper that uses the *platform
    # locale encoding* -- not necessarily UTF-8 -- and can apply newline
    # translation ("\n" -> "\r\n" on Windows). For secrets and tokens
    # that must be transmitted byte-exactly (a flipped newline silently
    # corrupts a token's hash, a locale mismatch breaks the first
    # non-ASCII character), that's a real bug. Encoding stdin_text to
    # UTF-8 ourselves and writing bytes directly bypasses both hazards
    # and unifies the two code paths below.
    stdin_payload: bytes | None
    if stdin_text is not None:
        stdin_payload = stdin_text.encode("utf-8")
    elif stdin_bytes is not None:
        stdin_payload = stdin_bytes
    else:
        stdin_payload = None

    # Only open a pipe when stdin_text or stdin_bytes was provided (even
    # if the resulting payload is an empty string/bytes -- that's a valid
    # "send immediate EOF" case, e.g. for a command that wants to see
    # closed stdin as a signal). When neither was provided, we inherit
    # the parent's stdin (the existing behavior) so interactive tools
    # that read from the TTY still work.
    popen_kwargs: dict = {"env": full_env, "shell": False}
    if stdin_payload is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
        # Deliberately NOT setting text=True: we normalized to bytes above
        # so the child's stdin stream is binary. No locale dependency, no
        # newline translation, byte-exact transmission.

    # Use Popen instead of subprocess.run so we can explicitly forward SIGINT
    # to the child and wait for it before propagating KeyboardInterrupt.
    # subprocess.run() has no hook for signal interception.
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        # The binary doesn't exist. This is a user/environment error (like
        # PrerequisiteError), not a framework bug. Surface it as exit code 1
        # via CliProcessError with an actionable message.
        raise CliProcessError(
            subprocess.CalledProcessError(
                returncode=127, cmd=cmd, stderr=f"Command not found: {cmd[0]}"
            )
        )

    # If we opened a stdin pipe, write the payload and close it so the child
    # sees EOF and can proceed. We do this BEFORE _wait_with_signal_forwarding
    # so the child isn't blocked waiting for stdin input we haven't sent yet.
    #
    # WHY manual write-and-close instead of proc.communicate(input=...):
    # communicate() internally calls proc.wait(), which bypasses our
    # _wait_with_signal_forwarding helper and therefore breaks Ctrl-C
    # SIGINT forwarding to the child. A manual write-then-close keeps the
    # existing wait path intact -- the child sees the stdin payload and
    # EOF, and the parent still forwards SIGINT on KeyboardInterrupt.
    #
    # Risk: if the child produces a huge stdout/stderr burst while we're
    # writing to stdin, the OS pipe buffer could fill and deadlock. In
    # practice, we don't capture stdout/stderr (they inherit the parent's
    # file descriptors), so the child's output streams freely to the
    # terminal and never fills a buffer we control. And stdin payloads
    # for this use case (secrets, tokens) are small enough to fit in a
    # single pipe buffer write, so the write itself won't block either.
    if stdin_payload is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_payload)
        except BrokenPipeError:
            # The child exited (or closed its stdin) before we finished
            # writing. This is a legitimate flow for tools that validate
            # arguments or environment before consuming stdin, then exit
            # with a non-zero status. Swallow the write error here and
            # let the wait path below report the child's real exit code
            # via CliProcessError -- that message is more actionable than
            # a BrokenPipeError traceback from the parent.
            pass
        except KeyboardInterrupt:
            # User pressed Ctrl-C while we were still writing stdin. Do
            # the same SIGINT-forward + wait-with-escalation dance that
            # ``_wait_with_signal_forwarding`` does when it catches KI
            # mid-wait, so the child gets a chance to clean up regardless
            # of exactly when during the call the signal arrived.
            # Without this, the KI would propagate up past ``proc``
            # entirely and leave the child running until OS cleanup.
            #
            # Every child-process interaction below is guarded against
            # ProcessLookupError / OSError because the Ctrl-C could have
            # arrived right *after* the child already exited on its own
            # (signals and fast-exiting children race). When that happens
            # we still want the KeyboardInterrupt to propagate cleanly to
            # the caller -- not be masked by an ignored "no such process"
            # error during cleanup.
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                # Child already exited; SIGINT delivery is moot.
                pass
            try:
                proc.wait(timeout=SIGINT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # Child ignored SIGINT; escalate to SIGKILL so we don't
                # hang the terminal waiting on a wedged child.
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait()
                except OSError:
                    pass
            except OSError:
                # wait() itself can raise on some platforms if the child
                # is already gone -- also fine, we're done either way.
                pass
            raise
        finally:
            # Close in a finally so a successful-but-partial write still
            # sends EOF. Wrap the close in its own try because closing a
            # pipe whose peer already closed can also raise BrokenPipeError
            # on some platforms (it's the flush-on-close that fails).
            # (The KeyboardInterrupt branch above already closes before
            # raising; this finally is a no-op there because stdin is
            # already closed.)
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                # BrokenPipeError / OSError: child already closed its side
                # of the pipe (race, fast exit, etc.).
                # ValueError: stdin already closed by the KI branch above.
                # In every case, the semantic goal (send EOF; don't mask
                # the real exit code) is already satisfied -- swallow.
                pass

    returncode = _wait_with_signal_forwarding(proc)
    if returncode != 0:
        raise CliProcessError(
            subprocess.CalledProcessError(returncode=returncode, cmd=cmd)
        )
    return subprocess.CompletedProcess(cmd, returncode)


def capture(
    cmd: list[str],
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    """Execute a command and return its stdout as a stripped string.

    Unlike run(), capture() always executes even in dry-run mode because
    commands typically need the captured data to make decisions (e.g.,
    listing resources before deciding what to deploy).

    Args:
        cmd: Command as an argv list.
        dry_run: Ignored -- capture always executes. Parameter exists for
            API consistency so callers can pass ctx.dry_run uniformly.
        env: Extra environment variables merged with os.environ.

    Returns:
        The command's stdout, stripped of leading/trailing whitespace.

    Raises:
        CliProcessError: If the command exits with non-zero status.
        TypeError: If cmd is a string instead of a list.
    """
    _validate_cmd(cmd)

    _ = dry_run  # accepted for API consistency; capture always executes

    full_env = _build_env(env)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, env=full_env,
            shell=False,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        # Same treatment as run(): missing binary is a user/environment
        # error (exit 1), not a framework bug (exit 2).
        raise CliProcessError(
            subprocess.CalledProcessError(
                returncode=127, cmd=cmd, stderr=f"Command not found: {cmd[0]}"
            )
        )
    except subprocess.CalledProcessError as e:
        raise CliProcessError(e) from e


def run_with_confirm(
    cmd: list[str],
    message: str,
    yes: bool = False,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    *,
    stdin_text: str | None = None,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess | None:
    """Prompt for confirmation, then execute a destructive command.

    Combines confirmation + execution so command authors don't forget either
    step. Uses prompts.confirm() directly for TTY-aware interactive prompts.
    When yes=True the prompt is bypassed entirely (--yes flag behaviour).

    Args:
        cmd: Command as an argv list.
        message: Confirmation prompt (e.g., "Deploy to production?").
        yes: If True, skip the prompt (--yes flag).
        dry_run: If True, print the command but don't execute it.
        env: Extra environment variables merged with os.environ.
        stdin_text: If set, encode this string as UTF-8 and pipe the bytes
            to the child's stdin. Mutually exclusive with stdin_bytes.
            (Implementation note: this function delegates to ``run()``,
            which opens stdin in binary mode and itself encodes
            ``stdin_text`` to UTF-8 before writing -- so the encoding
            happens in ``run()``, not here. No locale dependency, no
            newline translation.)
        stdin_bytes: If set, pipe these raw bytes to the child's stdin.
            Mutually exclusive with stdin_text.

    Returns:
        subprocess.CompletedProcess on success, or None if denied/dry-run.

    Raises:
        ValueError: If both stdin_text and stdin_bytes are set.

    See Also:
        ``run()`` -- full documentation of the secrets-via-stdin pattern
        (``wrangler secret put``, ``gh auth login --with-token``,
        ``docker login --password-stdin``).
    """
    _validate_cmd(cmd)
    # Validate stdin arguments here too so callers get the same early
    # ValueError they'd get from run(), rather than a confusing error that
    # only surfaces after the confirmation prompt has been answered.
    _validate_stdin_params(stdin_text, stdin_bytes)

    # Delegate to the framework's TTY-aware confirm() from prompts.py.
    # When yes=True, confirm() returns True immediately and skips the prompt.
    # When stdin is not a TTY (piped/CI), confirm() returns False (safe deny).
    if not _prompt_confirm(message, yes=yes):
        logger.info("Cancelled: %s", _format_cmd(cmd))
        return None

    # Delegate to run() so dry-run, env passing, stdin piping, and signal
    # forwarding are all handled consistently in one place.
    return run(
        cmd,
        dry_run=dry_run,
        env=env,
        stdin_text=stdin_text,
        stdin_bytes=stdin_bytes,
    )


def _validate_no_secret_in_argv(cmd: list[str | Secret]) -> None:
    """Reject any ``Secret`` instance that appears as an argv element.

    WHY an explicit-instance check and NOT a deep value scan:
    argv is world-readable via ``ps`` / ``/proc/*/cmdline`` on most
    POSIX systems. The goal of this guard is to catch the common
    footgun -- a caller writing ``run_with_secrets(["curl", "-H",
    f"Authorization: Bearer {tok}"], ...)`` where ``tok`` is a
    ``Secret`` -- and surface it loudly before the subprocess starts.

    We deliberately do NOT scan string elements for values that
    happen to match some ``Secret.get()``. That would either require
    the caller to declare every secret in advance (defeating the
    ``secrets=`` signature) or scan every string against every known
    Secret globally (brittle, performance-hostile, and prone to
    false positives on short secrets). The explicit-Secret check
    catches the realistic mistake without the deep-scan pitfalls.

    The error message names the offending arg's POSITION (its index
    in ``cmd``), never its value. Leaking ``.get()`` in our own
    rejection path would undermine the whole point of the helper.

    Args:
        cmd: Argv list whose elements may be ``str`` or ``Secret``.

    Raises:
        ValueError: If any element is a ``Secret`` instance. The
            message names the first offending position.
    """
    for idx, arg in enumerate(cmd):
        if isinstance(arg, Secret):
            # NOTE: no str(arg) here -- Secret.__str__ returns "***"
            # which is safe, but we also don't want the error message
            # to hint at the arg beyond its position. Position alone
            # is enough for the caller to fix the call site.
            raise ValueError(
                f"cmd[{idx}] is a Secret instance. Do not place secrets "
                "in argv (visible in `ps` output). Pass them via "
                "`secrets={...}` (env) or `stdin_secret=\"NAME\"` (stdin) instead."
            )


def _format_env_redacted(
    env: dict[str, str] | None,
    secret_keys: set[str],
) -> str:
    """Render an env dict for logging, redacting secret-sourced values.

    Keys that came from ``secrets={}`` are rendered as ``NAME=<redacted>``;
    all other keys keep their value. Env-var NAMES stay visible so
    operators debugging a subprocess launch can confirm what the child
    sees (missing keys, typos). Only values are hidden.

    Args:
        env: The full env dict that will be passed to the subprocess,
            or ``None`` if no extra env was built.
        secret_keys: The set of keys whose values came from ``secrets``
            and therefore must be redacted.

    Returns:
        A single-line string representation suitable for a log message.
    """
    if env is None:
        return "{}"
    parts: list[str] = []
    for name, value in env.items():
        if name in secret_keys:
            parts.append(f"{name}=<redacted>")
        else:
            parts.append(f"{name}={value}")
    return "{" + ", ".join(parts) + "}"


def run_with_secrets(
    cmd: list[str | Secret],
    *,
    secrets: dict[str, Secret],
    stdin_secret: str | None = None,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    """Execute a command with secrets delivered via env and/or stdin, never argv.

    This is a safety-focused wrapper over :func:`run` for subprocesses that
    need sensitive data. It centralises two guardrails that are easy to
    forget at individual call sites:

    1. **Explicit-Secret rejection in argv.** Any ``Secret`` instance that
       appears directly in ``cmd`` raises :class:`ValueError` before the
       subprocess starts. Argv is world-readable via ``ps`` and
       ``/proc/*/cmdline`` on most POSIX systems, so putting a secret
       there leaks it to any local user. The check is intentionally
       shallow: we reject *explicit Secret instances only*, not string
       arguments whose value happens to match some ``Secret.get()``. The
       rationale -- and why a deep scan is NOT the right choice here --
       is spelled out on :func:`_validate_no_secret_in_argv`.
    2. **Redacted logging.** The helper emits its own log line BEFORE
       delegating to :func:`run`, with every secret-sourced env-var
       rendered as ``NAME=<redacted>``. Env-var names stay visible so
       operators can see what environment the subprocess sees; values
       are hidden. ``run()`` itself has no knowledge of Secret semantics
       and emits no env-echoing logs during normal execution, so no
       redaction is needed there.

    Delivery:

    - **Env (always).** Every key in ``secrets`` is placed in the
      subprocess's environment with its unwrapped value. Caller-supplied
      ``env`` is merged underneath; on key conflict, ``secrets`` wins.
    - **Stdin (optional).** If ``stdin_secret="NAME"`` is set, the value
      of ``secrets["NAME"]`` is ALSO piped to the child's stdin (via
      Wave 1's ``stdin_text=`` helper on :func:`run`). The same value
      is in env -- some tools prefer one channel, some the other.

    Args:
        cmd: Argv list. Elements are typed as ``str | Secret`` so the
            Secret-in-argv check is meaningful at the type level, but
            after validation argv is guaranteed to be plain strings.
        secrets: Keyword-only. Mapping of env-var names to ``Secret``
            instances. Passing an empty dict is legal (no secrets
            delivered) but in that case you probably want :func:`run`
            directly -- using this helper when there are no secrets
            just adds ceremony with no safety benefit.
        stdin_secret: Keyword-only. If set, the name of the key in
            ``secrets`` whose value should ALSO be piped through stdin.
            Must be a key in ``secrets`` -- otherwise raises
            :class:`ValueError` without leaking any secret value.
        dry_run: Keyword-only. If True, log what would happen and return
            ``None`` without spawning a subprocess or reading any
            secret value into a child's env / stdin. Matches the
            :func:`run` dry-run semantics.
        env: Keyword-only. Additional (non-secret) env vars to pass
            through to the subprocess. Merged UNDER ``secrets`` so the
            secret value always wins on key conflict.

    Returns:
        :class:`subprocess.CompletedProcess` on success, or ``None`` in
        dry-run mode.

    Raises:
        ValueError: If any ``Secret`` appears in ``cmd``, or if
            ``stdin_secret`` names a key that isn't in ``secrets``.
        CliProcessError: Propagated from :func:`run` if the child
            exits non-zero.

    Example -- ``wrangler secret put`` reads the secret from stdin so
    it never appears in argv::

        ctx.run_with_secrets(
            ["wrangler", "secret", "put", "API_TOKEN"],
            secrets={"CLOUDFLARE_API_TOKEN": Secret(token)},
            stdin_secret="CLOUDFLARE_API_TOKEN",
        )

    Example -- ``docker login --password-stdin`` uses the same pattern::

        ctx.run_with_secrets(
            ["docker", "login", "-u", username, "--password-stdin",
             "registry.example.com"],
            secrets={"DOCKER_REG_PASSWORD": Secret(password)},
            stdin_secret="DOCKER_REG_PASSWORD",
        )

    Follow-up (deliberately out of scope here):
        A global ``--log-insecure-secrets`` flag / env var would let
        operators opt in to unredacted logging during local debugging.
        That's tracked as a separate issue; this helper always redacts.
    """
    # 1. Same list-not-string guardrail as run() / capture() /
    # run_with_confirm. Callers who pass a tuple or a raw string would
    # otherwise slip past the argv iteration below and bypass the
    # shell-injection contract the other helpers enforce. _validate_cmd
    # only checks isinstance(cmd, list), so our declared
    # list[str | Secret] element typing still goes through it cleanly.
    _validate_cmd(cmd)

    # 2. Argv guardrail. Run this BEFORE any logging / env building so
    # the rejection happens as early as possible and can't accidentally
    # surface the secret through a pre-check log line.
    _validate_no_secret_in_argv(cmd)

    # 3. After the Secret check, every element must be a plain str. A
    # PathLike, bytes, or int sneaking through would otherwise get
    # silently dropped by the old filter-comprehension, changing the
    # command the child sees. Fail loudly with the offending index so
    # the caller knows exactly what to fix.
    for idx, arg in enumerate(cmd):
        if not isinstance(arg, str):
            raise TypeError(
                f"cmd[{idx}] must be a str; got {type(arg).__name__}. "
                "run_with_secrets only accepts str (and Secret, which is "
                "rejected separately as a guardrail). Convert paths with "
                "str(path), ints with str(n), etc."
            )

    # 4. stdin_secret must resolve to a key in ``secrets``. Done BEFORE
    # we touch Secret.get() for any reason, so a typo surfaces as a
    # clear ValueError with no secret material in flight.
    if stdin_secret is not None and stdin_secret not in secrets:
        # Include the requested key name (safe -- the caller typed it)
        # but NEVER iterate the secrets dict values into the message.
        raise ValueError(
            f"stdin_secret={stdin_secret!r} is not a key in secrets={{...}}. "
            "The name must match one of the keys you pass in `secrets`."
        )

    # After validation, every cmd element is a plain str -- the element
    # typing is list[str | Secret] at the API boundary, but the runtime
    # is guaranteed narrower. Cast via list() so downstream helpers
    # (run, _format_cmd) get the concrete list[str] they expect.
    plain_cmd: list[str] = list(cmd)  # type: ignore[arg-type]

    stdin_display = (
        f"<redacted:{stdin_secret}>" if stdin_secret is not None else "<none>"
    )

    # 5. Dry-run short-circuit. Docstring promises dry_run does not
    # pull secret values into memory. Honouring that means bailing out
    # BEFORE Secret.get() on any entry of ``secrets`` -- only the
    # caller-supplied ``env`` (which is already plain strings) and the
    # secret KEYS (not values) appear in the dry-run log.
    if dry_run:
        base_env_for_log: dict[str, str] = dict(env) if env is not None else {}
        # Show the secret keys as NAME=<redacted> in the dry-run log so
        # an operator inspecting a dry-run can see the full env shape
        # without any values having been read off the Secret objects.
        redacted_secret_env = {name: "<redacted>" for name in secrets}
        display_env = {**base_env_for_log, **redacted_secret_env}
        logger.info(
            "run_with_secrets [dry-run]: cmd=%s env=%s stdin=%s",
            _format_cmd(plain_cmd),
            # secret_keys = names we'd redact; in dry-run every secret
            # is redacted regardless because we haven't unwrapped them.
            _format_env_redacted(display_env, set(secrets)),
            stdin_display,
        )
        return None

    # 6. Build the full env for the subprocess. Caller's env goes first
    # so secrets win on key conflict -- the helper's job is to deliver
    # the secret, and a stale override from ``env`` would silently break
    # that contract. Each Secret.get() is called EXACTLY ONCE and the
    # result reused below for stdin delivery if needed.
    base_env: dict[str, str] = dict(env) if env is not None else {}
    secret_env = {name: s.get() for name, s in secrets.items()}
    full_env: dict[str, str] = {**base_env, **secret_env}
    # Track which keys came from secrets so the log line redacts only
    # those values -- non-secret env vars stay plainly visible.
    secret_keys = set(secret_env.keys())

    # 7. Resolve the stdin payload from the ALREADY-unwrapped secret_env
    # dict rather than calling Secret.get() a second time -- one unwrap
    # per secret keeps the "minimal touch" contract explicit.
    stdin_payload: str | None = None
    if stdin_secret is not None:
        stdin_payload = secret_env[stdin_secret]

    # 8. Emit the helper's own log line. This is the SINGLE place where
    # the "secrets-in-play" subprocess launch is recorded. We log
    # BEFORE delegating to run() so:
    #   - the argv (already validated Secret-free and all-str) appears
    #     once, here, with full context (env + stdin redacted);
    #   - run()'s own logging stays unchanged -- it never sees Secret
    #     objects, only plain strings, and during normal (non-dry-run)
    #     execution it doesn't log env at all (see process.run()).
    logger.info(
        "run_with_secrets: cmd=%s env=%s stdin=%s",
        _format_cmd(plain_cmd),
        _format_env_redacted(full_env, secret_keys),
        stdin_display,
    )

    # 9. Delegate. run() handles signal forwarding and stdin piping --
    # we reuse all of it instead of reinventing the wheel (and instead
    # of teaching run() about Secret). dry_run was already handled
    # above so we pass False here to make that explicit.
    return run(
        plain_cmd,
        dry_run=False,
        env=full_env,
        stdin_text=stdin_payload,
    )
