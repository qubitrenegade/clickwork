"""Layered TOML configuration with environment support.

Config is loaded from multiple sources with cascading precedence
(highest wins):

    1. Environment variables (explicit mapping or auto-prefixed)
    2. Env-specific section ([env.staging]) in repo config
    3. [default] section in repo config (.{project-name}.toml)
    4. User-level config (~/.config/{project-name}/config.toml)

The env-specific section *overrides* [default] but doesn't *replace* it --
keys not specified in the env section fall through to [default].

Schema validation (optional) ensures required keys exist, types match,
and secrets don't leak into repo config. User config with loose permissions
is refused (not just warned) to prevent secret leakage.
"""

from __future__ import annotations

import os
import shlex
import stat
import sys

# tomllib is stdlib in Python 3.11+. No external dependency needed.
import tomllib
from pathlib import Path
from typing import Any

from clickwork._types import Secret, normalize_prefix


class ConfigError(Exception):
    """Raised when config validation fails.

    This is a user-facing error -- the message should be actionable,
    telling them which key is missing/invalid and where to fix it.
    """


# Explicit boolean token sets. Defined at module scope so the docstring
# for ``_coerce_value`` can reference them and downstream code (plus
# docs/tests) always agree on the exact accepted tokens.
#
# WHY not use ``bool(value)``: Python's built-in truthiness considers
# any non-empty string truthy, so ``bool("false")`` is ``True`` -- the
# classic foot-cannon for env-var parsing. Instead we use an explicit
# case-insensitive allowlist that matches shell conventions. Anything
# outside both sets raises ConfigError rather than silently guessing.
_TRUTHY_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSY_STRINGS = frozenset({"false", "0", "no", "off"})


def _coerce_value(
    value: object,
    expected_type: type,
    key: str,
    key_schema: dict[str, Any] | None = None,
) -> object:
    """Coerce a string value to ``expected_type``.

    Environment variables at the OS level are ALWAYS strings
    (``os.environ`` is ``dict[str, str]``), and TOML string literals
    (``port = "8080"``) are strings even when the schema declares a
    numeric type. Rather than forcing every plugin author to re-
    implement ``int(os.environ["PORT"])`` with their own error
    handling, the loader performs the coercion centrally at the
    schema layer using this helper.

    The rule is uniform across sources: any string value whose
    schema declares a non-``str`` type is coerced. Values that
    already match ``expected_type`` (e.g. TOML's native ``port =
    8080`` parsing to ``int``) pass through unchanged.

    Supported coercion table:

        str -> int    : ``int(value)`` (base 10)
        str -> float  : ``float(value)``
        str -> bool   : explicit allowlist -- see _TRUTHY_STRINGS /
                        _FALSY_STRINGS, case-insensitive.
        str -> str    : no-op (returned unchanged)

    Any other combination (e.g. ``list`` -> ``int``, or an unsupported
    ``expected_type``) triggers the same ConfigError a straight
    ``isinstance`` mismatch would -- the caller's validation branch
    still fires.

    Args:
        value: The resolved config value for ``key``. Often a str
            (from an env var or TOML string literal); may also be an
            already-typed value that happens to match ``expected_type``.
        expected_type: The type the schema declared for ``key``
            (``int``, ``float``, ``bool``, or ``str``).
        key: The dotted config key -- used to build an actionable
            error message so the operator knows which env var to fix.
        key_schema: The schema entry for ``key``, used to detect
            ``secret: True`` so the error message redacts the value
            instead of echoing a misconfigured secret token verbatim.
            Optional for backward compatibility with callers that
            don't have the schema entry handy.

    Returns:
        The coerced value, or ``value`` unchanged if it already
        matches ``expected_type`` (including the no-op ``str`` case).

    Raises:
        ConfigError: If ``value`` is a string but does not parse as
            ``expected_type`` (e.g. ``"not-a-number"`` for int, or a
            bool token outside the explicit allowlist). The message
            names ``key`` and the offending value verbatim -- except
            when the schema marks the key as ``secret: True``, in
            which case the value is shown as ``<redacted>`` to
            prevent misconfigured secret env vars from leaking into
            logs via the exception.
    """
    # Fast-path: if the value already has the expected type, nothing
    # to do. ``bool`` is a subclass of ``int`` in Python, so we check
    # bool first to avoid accidentally treating ``True`` as "already
    # an int" when the schema wanted a bool.
    if expected_type is bool and isinstance(value, bool):
        return value
    # ``not isinstance(value, bool)`` on the int branch keeps ``True``
    # from matching ``type: int`` via the bool-is-subclass-of-int
    # quirk -- a schema that says ``int`` should reject a bool value.
    if expected_type is int and isinstance(value, int) and not isinstance(value, bool):
        return value
    if expected_type is float and isinstance(value, float):
        return value
    if expected_type is str and isinstance(value, str):
        return value

    # From here on, coercion only applies to string values. Both string
    # sources (env vars and TOML string literals like ``port = "8080"``)
    # follow the same rule: if the schema declares a non-``str`` type
    # and the merged value is a string, the loader coerces it. Non-
    # string mismatches (e.g. a TOML ``port = [8080]`` list against
    # ``type: int``) fall through unchanged so the caller's
    # isinstance check raises the familiar "type X, expected Y"
    # ConfigError. Example of the pass-through path: TOML native
    # ``port = 8080`` arrives as int and skips coercion entirely.
    if not isinstance(value, str):
        return value

    # Build a display form of the value for error messages. When the
    # schema marks this key as a secret, echoing the raw value into a
    # ConfigError could leak a misconfigured secret env var into logs
    # / stderr / CI output. Use ``<redacted>`` in that case so the
    # operator still sees WHICH key failed without exposing the token.
    # The raw ``value`` is still used for coercion attempts above --
    # we only redact the user-facing message.
    is_secret = bool(key_schema and key_schema.get("secret"))
    display_value = "<redacted>" if is_secret else repr(value)

    # String -> bool: explicit allowlist, case-insensitive. We lowercase
    # once and compare against two frozensets so the token list stays
    # in one place (the module-level constants) and the implementation
    # is O(1) per lookup.
    if expected_type is bool:
        token = value.strip().lower()
        if token in _TRUTHY_STRINGS:
            return True
        if token in _FALSY_STRINGS:
            return False
        # Build the error message from the actual token sets so the
        # operator sees the same list the code accepts, and future
        # additions can't drift out of sync with the message.
        raise ConfigError(
            f"Config key '{key}' has value {display_value}, which is not a "
            f"valid boolean. Accepted tokens (case-insensitive): "
            f"{sorted(_TRUTHY_STRINGS)} for true, "
            f"{sorted(_FALSY_STRINGS)} for false."
        )

    # String -> int: base 10. ``int("3.14")`` raises ValueError, which
    # we catch and re-raise as ConfigError so callers only ever have
    # to handle one exception type from load_config().
    if expected_type is int:
        try:
            return int(value)
        except ValueError as exc:
            # For secret keys, also suppress the underlying ValueError
            # text (``exc``) AND suppress the exception chain -- Python's
            # int() error message embeds the raw token, so both the
            # chained ``__cause__`` and any naive ``str(exc)`` would
            # defeat the redaction when a traceback is surfaced. Using
            # ``from None`` breaks the chain so the raw token doesn't
            # survive on the exception's ``__cause__`` attribute.
            detail = "invalid literal" if is_secret else str(exc)
            message = (
                f"Config key '{key}' has value {display_value}, which cannot "
                f"be parsed as int: {detail}."
            )
            if is_secret:
                raise ConfigError(message) from None
            raise ConfigError(message) from exc

    # String -> float: accepts scientific notation, signed, decimals.
    if expected_type is float:
        try:
            return float(value)
        except ValueError as exc:
            # Same redaction rationale as the int branch: float()'s
            # ValueError message contains the raw token, and both the
            # message AND the exception chain must be scrubbed for
            # secret keys.
            detail = "invalid literal" if is_secret else str(exc)
            message = (
                f"Config key '{key}' has value {display_value}, which cannot "
                f"be parsed as float: {detail}."
            )
            if is_secret:
                raise ConfigError(message) from None
            raise ConfigError(message) from exc

    # Unsupported target type (e.g. ``type: list``, ``type: dict``).
    # Rather than guess, return the value unchanged and let the
    # isinstance() check in the caller flag the mismatch. Pinning the
    # "stdlib scalar types only" rule here means adding new coercion
    # targets is an explicit code edit, not a silent behaviour change.
    return value


def _key_to_env_suffix(key: str) -> str:
    """Convert a dotted config key to an env var suffix.

    Dots and hyphens become underscores and the result is uppercased so the
    suffix can be appended to a prefix to form a valid env var name (e.g.,
    ``cloudflare.account_id`` -> ``CLOUDFLARE_ACCOUNT_ID``).

    Args:
        key: A dotted config key, possibly containing hyphens.

    Returns:
        An uppercase, underscore-delimited env var suffix.
    """
    return key.replace(".", "_").replace("-", "_").upper()


def _flatten_mapping(data: dict[str, Any], prefix: str = "") -> dict[str, object]:
    """Flatten nested TOML dicts into a single-level dotted-key mapping.

    TOML dotted keys such as ``cloudflare.account_id = "abc"`` parse as
    nested dicts (``{"cloudflare": {"account_id": "abc"}}``). Commands and
    schemas in clickwork use flat dotted keys, so we normalize TOML data
    into that shape before merging config layers.

    Args:
        data: A (possibly nested) dict as parsed from a TOML file.
        prefix: Dotted key prefix accumulated during recursion. Should be
            left as the default empty string by external callers.

    Returns:
        A flat dict mapping dotted-key strings to their leaf values.
    """
    flat: dict[str, object] = {}
    for key, value in data.items():
        # Build the full dotted key: prepend parent prefix if one exists.
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            # Recurse into nested dicts, accumulating the dotted prefix so
            # {'cloudflare': {'account_id': 'x'}} becomes 'cloudflare.account_id'.
            flat.update(_flatten_mapping(value, prefix=full_key))
        else:
            flat[full_key] = value
    return flat


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning an empty dict if the file does not exist.

    Returning an empty dict instead of raising means callers can safely
    invoke this for optional config paths without wrapping every call in
    try/except, keeping the layered config logic in load_config() clean.

    Args:
        path: Filesystem path to the TOML file to load.

    Returns:
        Parsed TOML contents as a (possibly nested) dict, or an empty dict
        if the file does not exist.
    """
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _load_toml_from_bytes(data: bytes) -> dict[str, Any]:
    """Parse TOML from an in-memory bytes buffer.

    Used when the caller has already read the file bytes (e.g., from an
    fd held open for TOCTOU-safe permission checking) and needs to parse
    them without re-opening the file by path.

    Args:
        data: Raw TOML file contents as bytes.

    Returns:
        Parsed TOML contents as a (possibly nested) dict.
    """
    # tomllib.loads() requires a str, but tomllib.load() requires a binary
    # IO object.  We use an in-memory BytesIO so no second open() is needed.
    import io

    return tomllib.load(io.BytesIO(data))


def _check_owner_only_permissions(fd: int, path: Path, kind: str) -> None:
    """Raise ConfigError if the file behind ``fd`` has any group/other perms.

    "Owner-only" in the informal sense: nothing outside the file's owner
    can read, write, or execute it. The owner's own bits are NOT
    constrained (so ``0o600``, ``0o700``, ``0o400``, etc. all pass) --
    the helper is about keeping *other* users out, not about the owner's
    own mode choices.

    Factored out of the user-config reader so the same permission guard can
    be reused by any secrets-bearing file (user config TOML, .env dotenv
    files, etc.) without duplicating the TOCTOU-safe fstat() logic or the
    Windows carve-out.

    The check uses ``os.fstat(fd)`` (not ``os.stat(path)``) so it operates
    on the already-opened file descriptor. This closes the TOCTOU
    (time-of-check/time-of-use) window: an attacker cannot swap the file
    between permission check and read, because both operate on the same
    kernel fd.

    On Windows the Unix permission model does not apply (NTFS ACLs use a
    completely different mechanism), so the check is a no-op there. Callers
    should still gate file creation on platform-appropriate ACLs; this
    helper only enforces the POSIX mode-bit portion.

    Args:
        fd: An open file descriptor to check. The caller retains ownership
            -- this function does not close it.
        path: The path used to open ``fd``; only used to build a helpful
            error message pointing the user at the right file to chmod.
        kind: Human-readable label for the file type (e.g., "User config",
            ".env file"). Interpolated into the error message so the
            caller sees which file failed the check.

    Raises:
        ConfigError: If ANY group or other permission bits are set on the
            file (read, write, OR execute). A group-writable file is a
            tampering risk even when not group-readable, so the check
            covers all three bit classes for group/other rather than
            just read bits. Owner bits are NOT constrained -- the helper
            only cares that no one *else* can access the file; owner
            execute (or setuid, etc.) is the caller's problem. This
            matches the standard "chmod 600" remediation, which clears
            every bit except owner read/write.
    """
    # Skip the check entirely on Windows -- POSIX mode bits are meaningless
    # there, and fstat() on Windows typically returns 0o666 for any file.
    if sys.platform == "win32":
        return

    st = os.fstat(fd)
    mode = stat.S_IMODE(st.st_mode)
    # Reject ANY group/other permission bits (not just read). A file with
    # mode 0o620 (owner rw, group w, other ---) has group-write, which
    # means another user could *tamper* with our secrets file -- still a
    # compromise even though they can't read it directly. S_IRWXG and
    # S_IRWXO cover all of read/write/execute for group/other, matching
    # the "chmod 600" remediation we recommend.
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        # Shell-quote the path in the remediation hint so paths with
        # spaces or leading '-' characters remain safe to copy/paste into
        # a terminal. shlex.quote() wraps the string in single quotes
        # when needed and leaves safe paths unchanged.
        raise ConfigError(
            f"{kind} {path} has unsafe permission {oct(mode)} "
            f"(accessible by group/others). Secrets may be exposed or "
            f"tampered with.\n"
            f"Fix with: chmod 600 {shlex.quote(str(path))}"
        )


def _read_checked_user_config(path: Path) -> bytes | None:
    """Open, permission-check, and read a user config file in one operation.

    User config may contain secrets (API tokens, personal credentials), so
    no one but the file's owner may access it. Any group/other permission
    bit -- read, write, or execute -- raises ConfigError. ``chmod 600`` is
    the canonical remediation, but ``0o400`` or ``0o700`` also pass the
    check; only group/other bits are forbidden. On Windows this check is
    skipped because the Unix permission model does not apply.

    The entire open-check-read sequence uses a single file descriptor to
    avoid TOCTOU (time-of-check/time-of-use) races: we open first, then
    ``fstat()`` the fd (not the path), then read from the same fd. No
    second open is needed, so no substitution can happen between the
    permission check and the read.

    Args:
        path: Path to the user config file to read. If the file does not
            exist the function returns None (missing config is fine).

    Returns:
        The raw bytes of the file if it exists and has safe permissions,
        or None if the file does not exist.

    Raises:
        ConfigError: If the file exists and has ANY group/other permission
            bit set (read, write, or execute). See
            ``_check_owner_only_permissions`` for the precise rule --
            this helper delegates the check there, so when the rule
            tightens the behaviour here tightens too.
    """
    # Open the file first, then stat the open fd (TOCTOU-safe).
    # FileNotFoundError is caught instead of a pre-check with path.is_file()
    # to avoid a TOCTOU race between the existence check and the open.
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        # Missing user config is fine -- it's optional.
        return None
    # Wrap the raw fd in a Python file object immediately so we get
    # automatic cleanup (no manual os.close needed) and .read() handles
    # short reads internally -- os.read() can return fewer bytes than
    # requested. closefd=True means the file object owns the fd.
    with os.fdopen(fd, "rb") as f:
        # Delegate the permission check to the shared helper so the
        # dotenv loader and any future secrets-bearing reader can reuse
        # exactly the same logic (including the Windows carve-out).
        _check_owner_only_permissions(fd, path, kind="User config")
        return f.read()


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file into a plain dict of string key/value pairs.

    This is a *standalone* helper -- it is **not** integrated into
    ``load_config()``. The TOML pipeline handles structured config; this
    helper covers the separate case where a command needs to source
    credentials or environment variables from a ``.env`` file and pass
    them to a subprocess (``ctx.run(env=...)``) or inject them into
    ``os.environ``.

    Supported syntax (deliberately tiny):

        KEY=value              # simple assignment
        export KEY=value       # optional shell-style 'export' prefix, stripped
        KEY="value with ws"    # double-quoted value (quotes stripped)
        KEY='value with ws'    # single-quoted value (quotes stripped)
        # full-line comment    # skipped
        <blank line>           # skipped

    Explicitly **NOT supported** (by design -- do not add these):

        * Variable substitution. ``K=$OTHER`` stores the literal string
          ``"$OTHER"``; nothing is resolved from ``os.environ`` or from
          earlier entries in the file. If you need shell semantics, use
          ``sh -c 'set -a; source .env; env'`` and capture stdout.
        * Backticks / command substitution (e.g. ``K=$(date)`` or the
          backtick form).
        * Heredocs and multi-line values.
        * Inline trailing comments (``K=val # comment`` is parsed as
          ``K`` -> ``"val # comment"`` because the literal '#' is part of
          the value, not a comment marker).

    These omissions are intentional. A tiny grammar is a feature: it means
    you can look at a ``.env`` file and know exactly what each line does
    without reading the parser.

    Security: the file must be owner-only (``chmod 600``) on POSIX
    platforms. Because ``.env`` files typically hold secrets, any
    group or other permission bit (read, write, OR execute) raises
    ConfigError -- not just group/other readability. A group-writable
    file is a tampering risk even when not group-readable, so the
    rejection matches what ``chmod 600`` actually enforces. On Windows
    the check is skipped -- POSIX mode bits do not apply there, and
    callers are expected to protect the file via NTFS ACLs instead.

    Example usage::

        from pathlib import Path
        from clickwork.config import load_env_file

        env = load_env_file(Path(".env"))
        # env == {"API_TOKEN": "...", "REGION": "us-east-1"}

        # Pass to a subprocess without mutating the parent environment.
        # Note the list form: ctx.run (and the underlying subprocess
        # helpers) reject string commands as a shell-injection guardrail,
        # so every cmd must be an argv list.
        ctx.run(["./deploy.sh"], env={**os.environ, **env})

        # Or inject into the parent process:
        os.environ.update(env)

    Args:
        path: Path to the dotenv file to parse. Must exist (unlike user
            config, a missing .env is an error -- the caller asked for
            this file by name).

    Returns:
        Dict mapping keys to their (possibly unquoted) string values.
        Insertion order matches the order each key *first* appears in
        the file. If the file contains the same key twice, the later
        value overwrites the earlier one but the dict slot stays at
        the first occurrence's position -- if caller cares about order,
        they should ensure no duplicate keys in the file.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ConfigError: If the file has unsafe permissions (POSIX only), or
            if any line is malformed -- missing ``=`` separator, or
            producing an empty key (e.g. ``=value`` or ``export =v``).
            Malformed-line errors include the 1-based line number so
            the caller can locate the problem.
    """
    # Open then fstat -- same TOCTOU-safe pattern as _read_checked_user_config.
    # We deliberately do not catch FileNotFoundError: a missing .env is a
    # real error for this function (the caller explicitly asked for it),
    # unlike user config which is optional.
    fd = os.open(str(path), os.O_RDONLY)
    # os.fdopen(..., "r") gives us text mode with universal newlines so
    # Windows CRLF files parse correctly alongside Unix LF files.
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        # Reuse the exact permission check (including the Windows carve-out)
        # used by user config. See _check_owner_only_permissions for details.
        #
        # The shared helper already formats errors as
        # "{kind} {path} ..." so we only need to pass a generic
        # category label here; the path itself is interpolated by
        # the helper. Keeping kind short avoids double-labelling like
        # "dotenv file '.env' /tmp/.../.env ...".
        _check_owner_only_permissions(fd, path, kind="dotenv file")
        text = f.read()

    result: dict[str, str] = {}
    # enumerate(..., start=1) gives 1-based line numbers for human-friendly
    # error messages -- line 1 in the error should match line 1 in the file.
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        # Strip surrounding whitespace once; we'll work with the trimmed
        # form from here on. (Python text mode with default newline=None
        # already normalizes "\r\n" and bare "\r" to "\n" via universal
        # newlines, so we don't need to worry about stray carriage
        # returns; .strip() is purely for leading/trailing spaces and
        # tabs from hand-edited files.)
        line = raw_line.strip()

        # Skip blank lines and full-line comments. These two branches are
        # the only lines that do not produce a key/value pair.
        if not line or line.startswith("#"):
            continue

        # Strip the optional shell-style 'export ' prefix so the same file
        # can be consumed by both load_env_file() and 'source .env'.
        # We match 'export ' with a trailing space to avoid eating the
        # 'export' portion of a legitimate key like 'exportKEY=v'.
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        # Split on the *first* '=' only. Values may legitimately contain
        # '=' (e.g., base64-encoded tokens), so str.partition is safer than
        # str.split('=', 1) because it returns a 3-tuple even when '=' is
        # absent, which we check for below.
        key, sep, value = line.partition("=")
        if not sep:
            # No '=' in the line -- we can't tell what the caller meant.
            # Refuse rather than silently dropping or misinterpreting.
            #
            # WHY we don't echo raw_line in the error: .env files are the
            # canonical home for secrets (that's the whole point of this
            # parser). If a malformed line contained a partial secret
            # assignment, echoing raw_line would leak it into logs/CI
            # output. Just the line number is enough for the caller to
            # open the file and find the bad line.
            raise ConfigError(f"{path}: line {lineno}: malformed entry (no '=' separator)")

        # Keys are trimmed; values are *not* trimmed beyond outer whitespace.
        # We already stripped the whole line above, so key.strip() on top of
        # that is cheap and defensive against 'KEY =value' style spacing.
        key = key.strip()

        # Empty-key guard: '=value' or 'export =value' would otherwise
        # produce {"": "value"}, which is never a valid environment
        # variable name and will blow up later when passed to
        # subprocess/os.environ. Fail early with a clear line number.
        # (Same no-raw-line policy as the separator error above -- don't
        # echo the bad line because it may contain secrets.)
        if not key:
            raise ConfigError(f"{path}: line {lineno}: empty key (missing name before '=')")

        # Strip leading whitespace from the value before looking for
        # quotes. Common dotenv forms like ``KEY = value`` or
        # ``KEY= "value"`` would otherwise produce values like ``" value"``
        # (with a real leading space) or leave the surrounding quotes
        # intact (because value[0] is a space, not a quote). ``lstrip()``
        # removes ALL leading whitespace characters (spaces, tabs, etc.)
        # not just one -- but that's correct here: there is no legitimate
        # dotenv form where "two spaces before the value" means anything
        # different from "one space before the value", and quoted values
        # still preserve their own internal whitespace. Matches what
        # python-dotenv / direnv / shell-source do in
        # practice, while still letting QUOTED values preserve their own
        # leading whitespace:
        #   KEY = "  value"   -> value becomes "  value" (inside quotes)
        #   KEY =   value     -> value becomes "value"   (bare, space stripped)
        # Trailing whitespace of the BARE (unquoted) value is already
        # gone because raw_line.strip() at the top of the loop stripped
        # both ends of the whole line. That's fine: .env files don't
        # conventionally carry trailing whitespace in values; anyone
        # who needs a trailing space can preserve it inside quotes
        # (the quote-unwrap below runs AFTER the quote characters are
        # still intact, so "KEY = 'value '" yields "value " with the
        # intended trailing space).
        value = value.lstrip()

        # Unwrap matching surrounding quotes. We only strip quotes when the
        # entire value is wrapped -- a value like 'foo"bar' stays literal.
        # This matches the behaviour users expect from a minimal dotenv
        # parser, without pulling in the full shell-quoting rules.
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]

        result[key] = value

    return result


def load_config(
    project_name: str,
    repo_config_path: Path | None = None,
    user_config_path: Path | None = None,
    env: str | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load and merge config from all sources.

    Args:
        project_name: CLI project name (e.g., "orbit-admin"). Used for
            env var prefix and default config file paths.
        repo_config_path: Path to repo-level config (.orbit-admin.toml).
            If None, looks for .{project_name}.toml in cwd.
        user_config_path: Path to user-level config. If None, uses
            ~/.config/{project_name}/config.toml.
        env: Selected environment (e.g., "staging"). Falls back to the
            {PROJECT_NAME}_ENV env var when --env is omitted (i.e., env is None).
        schema: Optional config schema dict for validation.

    Returns:
        Merged config dict with all keys resolved.

    Raises:
        ConfigError: If schema validation fails (missing required key,
            secret in repo config, type mismatch, unsafe permissions).
    """
    # Derive the env var prefix once; used throughout this function.
    # 'test-cli' -> 'TEST_CLI'
    prefix = normalize_prefix(project_name)

    # {PROJECT_NAME}_ENV is a fallback when --env is omitted (env is None).
    # CI pipelines can set this env var to select an environment without
    # modifying every command invocation.
    if env is None:
        env = os.environ.get(f"{prefix}_ENV")

    # -------------------------------------------------------------------------
    # Layer 4 (lowest priority): User-level config
    # -------------------------------------------------------------------------
    # User config lives outside the repo and may contain secrets (API tokens,
    # personal credentials). It is deliberately lowest priority so repo config
    # can override it for shared settings like bucket names.
    if user_config_path is None:
        user_config_path = Path.home() / ".config" / project_name / "config.toml"

    # Refuse to load user config if it's too permissive -- secrets must not
    # be readable by group or other users on the same machine.
    # _read_checked_user_config opens the file, checks permissions via
    # fstat(), and returns the raw bytes read from the same fd.  We then parse
    # those bytes directly, avoiding a second open() that would reintroduce a
    # TOCTOU window between the permission check and the read.
    user_config_bytes = _read_checked_user_config(user_config_path)
    if user_config_bytes is not None:
        user_config = _flatten_mapping(_load_toml_from_bytes(user_config_bytes))
    else:
        user_config = {}

    # -------------------------------------------------------------------------
    # Layer 3: Repo [default] section
    # -------------------------------------------------------------------------
    # The repo config lives at .{project_name}.toml in the project root.
    # It's checked into git and holds non-secret defaults shared by the team.
    if repo_config_path is None:
        repo_config_path = Path.cwd() / f".{project_name}.toml"

    # Load the full TOML file once; we'll extract sections from it below.
    # ``repo_config_exists`` tells us whether the file is actually on disk --
    # the fail-fast unknown-env check below uses it to stay silent when
    # there is no repo config at all (e.g. a CLI invoked outside a project
    # dir with --env=staging from muscle memory). Only once a project has
    # a config file does "unknown env" become an actionable misconfig.
    repo_config_exists = repo_config_path.is_file()
    repo_data = _load_toml(repo_config_path)

    # The [default] section provides baseline values for all environments.
    repo_default = _flatten_mapping(repo_data.get("default", {}))

    # -------------------------------------------------------------------------
    # Layer 2: Env-specific section
    # -------------------------------------------------------------------------
    # [env.production], [env.staging], etc. overlay [default] -- keys present
    # in the env section override [default], but keys absent from the env
    # section still fall through to [default].
    #
    # Fail-fast on unknown env names: when the caller explicitly selects an
    # env (via ``--env production`` or the ``{PROJECT_NAME}_ENV`` fallback)
    # but the TOML file has no matching ``[env.production]`` section, silently
    # loading ``[default]`` is a footgun -- the operator thinks they're on
    # "production" settings, but they're actually on dev defaults. Matching
    # the "fail loud" discipline applied elsewhere (required keys, unsafe
    # file perms, secret-in-repo), we raise ConfigError with a message that
    # names the missing section AND the envs that ARE defined so the
    # operator can pick the right one or add the section.
    repo_env: dict[str, Any] = {}
    # Treat empty string the same as ``None`` (no env selected). An
    # env var like ``{PREFIX}_ENV=`` resolves to ``""``, and callers
    # relied on pre-#52 behavior that silently fell back to
    # ``[default]``. Raising ``ConfigError`` on empty-string env would
    # be a breaking change beyond the typo-detection scope of #52, so
    # the guard uses ``if env`` (truthy) rather than ``if env is not
    # None``. This keeps the fail-fast discipline for *typos* (which
    # arrive as non-empty strings) without regressing the
    # unset-env-var path.
    if env and repo_config_exists:
        # ``env_sections`` is the set of environment names the loader found
        # in the file. It might be empty (file declares no [env.*] tables at
        # all) or missing the selected name. Either case is a misconfig.
        #
        # Guard against a malformed TOML where ``env`` isn't a table --
        # e.g. ``env = "staging"`` at the top level instead of
        # ``[env.staging]``. Without this check, ``env in env_sections``
        # would iterate over the string's characters and ``_flatten_mapping``
        # would blow up on a non-dict input. Treating any non-dict as
        # "no env sections defined" routes it through the same error path
        # as the missing-section case, which gives the operator an
        # actionable message.
        env_sections_raw = repo_data.get("env", {})
        env_sections: dict[str, Any] = (
            env_sections_raw if isinstance(env_sections_raw, dict) else {}
        )
        if env in env_sections:
            # Also guard the selected section itself: a TOML dotted-key
            # form like ``env.production = "x"`` (vs the intended
            # nested table ``[env.production]``) lands as a non-dict
            # value in env_sections[env] and would blow up
            # ``_flatten_mapping``. Route that through the same
            # "malformed section" error path so the operator sees a
            # clean message instead of an AttributeError.
            env_section_raw = env_sections[env]
            if not isinstance(env_section_raw, dict):
                raise ConfigError(
                    f"Config env '{env}' is not a TOML table in "
                    f"{repo_config_path}. Check the TOML syntax: the "
                    f"section must be declared as ``[env.{env}]`` with "
                    "nested keys, not as a bare dotted-key assignment."
                )
            repo_env = _flatten_mapping(env_section_raw)
        else:
            # Sort the defined-sections list so error messages are stable
            # across dict-iteration orderings (Python 3.7+ preserves insertion
            # order, but a user editing the TOML file shouldn't have test
            # failures depend on the order keys were typed).
            defined = sorted(env_sections.keys())
            if defined:
                defined_clause = f"Defined sections: {defined}."
            else:
                defined_clause = "No [env.*] sections are defined in this file."
            raise ConfigError(
                f"Config env '{env}' is not defined in {repo_config_path}. "
                f"{defined_clause} "
                f"Add an [env.{env}] section or select a defined env."
            )

    # -------------------------------------------------------------------------
    # Build the merged config dict: user < default < env-specific
    # -------------------------------------------------------------------------
    # dict.update() means the last write wins, so we apply layers in order
    # from lowest to highest priority.
    config: dict[str, Any] = {}
    config.update(user_config)  # Layer 4: lowest priority
    config.update(repo_default)  # Layer 3: overrides user
    config.update(repo_env)  # Layer 2: overrides default

    # Track which keys came from repo config so the secret check can
    # identify values that should never live in a git-tracked file.
    repo_keys = set(repo_default.keys()) | set(repo_env.keys())

    # -------------------------------------------------------------------------
    # Layer 1 (highest priority): Environment variables
    # -------------------------------------------------------------------------
    # Two mechanisms for env var resolution:
    #   a) Explicit mapping: schema["key"]["env"] = "CF_ACCOUNT_ID"
    #      Use this for third-party env var names that don't follow our prefix.
    #   b) Auto-prefix: PROJECT_NAME_KEY (dots -> underscores, uppercased)
    #      Use this for keys that follow our naming convention.
    #
    # Explicit wins over auto-prefix when both are set.

    # Pass (a): Apply explicit env var mappings from schema.
    if schema:
        for key, key_schema in schema.items():
            explicit_env = key_schema.get("env")
            if explicit_env and explicit_env in os.environ:
                # Explicit mapping wins -- set it now so the auto-prefix
                # pass below skips this key.
                config[key] = os.environ[explicit_env]

    # Collect all keys to check for auto-prefixed env vars. We include schema
    # keys even if they don't appear in any config file -- an env var can
    # inject a value for a schema-declared key that has no file fallback.
    all_keys = set(config.keys())
    if schema:
        all_keys |= set(schema.keys())

    # Pass (b): Apply auto-prefixed env vars for all known keys.
    for key in all_keys:
        # 'test-cli' + 'cloudflare.account_id' -> 'TEST_CLI_CLOUDFLARE_ACCOUNT_ID'
        auto_var = f"{prefix}_{_key_to_env_suffix(key)}"
        if auto_var in os.environ:
            # Only apply auto-prefix if no explicit mapping already set this key.
            # An explicit mapping in the schema indicates the author prefers a
            # specific env var name; we must not silently override their choice.
            explicit_env = (schema or {}).get(key, {}).get("env")
            if not (explicit_env and explicit_env in os.environ):
                config[key] = os.environ[auto_var]

    # -------------------------------------------------------------------------
    # Schema validation
    # -------------------------------------------------------------------------
    # Validation runs after all layers are merged so it sees the final values.
    # Order matters: secret check first (refuse dangerous configs early),
    # then defaults (fill missing values), then type check, then required check.
    if schema:
        for key, key_schema in schema.items():
            # Secret check: keys tagged secret=True must not appear in repo
            # config. Repo config is checked into git and visible to anyone
            # with repo access. Secrets must live in user config or env vars.
            if key_schema.get("secret") and key in repo_keys:
                raise ConfigError(
                    f"Config key '{key}' is tagged as secret but appears in "
                    f"repo config ({repo_config_path}). Move it to user config "
                    f"({user_config_path}) or use an environment variable."
                )

            # Default fill: if the key is still absent after all layers,
            # apply the schema default. This runs after env vars so that
            # a live env var always beats the schema default.
            if key not in config and "default" in key_schema:
                config[key] = key_schema["default"]

            # Type check + coercion: env vars always arrive as strings
            # (``os.environ`` is ``dict[str, str]``), but TOML string
            # literals and TOML string literals can also be strings
            # even when the schema declares ``int``/``bool``/``float``.
            # The rule is uniform: every string value in the merged
            # config dict gets coerced to the schema-declared type,
            # regardless of which source produced the string. See
            # _coerce_value for the supported coercion table and the
            # exact bool-token allowlist. Values that already match
            # ``expected_type`` pass through unchanged (TOML natively
            # carries int/float/bool, so ``port = 8080`` arrives as
            # int and skips coercion entirely).
            expected_type = key_schema.get("type")
            if expected_type and key in config:
                # Secrets are wrapped AFTER this validation pass, so a
                # ``secret: True`` value is still a plain str/int/etc.
                # here and coerces normally. Pass the schema entry into
                # _coerce_value so the error path can redact the value
                # for secret keys instead of echoing the raw token.
                config[key] = _coerce_value(config[key], expected_type, key, key_schema)
                # Post-coercion isinstance check is still the
                # authoritative gate. If _coerce_value returned the
                # value unchanged (unsupported target type, or a
                # non-string source that didn't match), this catches
                # the mismatch with the original "type X, expected Y"
                # message callers depend on.
                #
                # Special case: ``bool`` is a subclass of ``int`` in
                # Python, so ``isinstance(True, int)`` is True and a
                # TOML value like ``port = true`` would silently pass
                # ``type: int`` validation. Explicitly reject bool
                # values when the schema wants an int (and vice versa
                # -- an int shouldn't satisfy ``type: bool``) to match
                # the intent of the type declaration.
                current = config[key]
                wrong_bool_for_int = expected_type is int and isinstance(current, bool)
                wrong_int_for_bool = (
                    expected_type is bool
                    and not isinstance(current, bool)
                    and isinstance(current, int)
                )
                if (
                    not isinstance(current, expected_type)
                    or wrong_bool_for_int
                    or wrong_int_for_bool
                ):
                    raise ConfigError(
                        f"Config key '{key}' has type {type(current).__name__}, "
                        f"expected {expected_type.__name__}. "
                        f"Check the value in {repo_config_path} or {user_config_path}."
                    )

            # Required check: if the key is still absent after defaults and
            # env vars, raise so the user gets a clear error rather than a
            # confusing KeyError later in command code.
            if key_schema.get("required") and key not in config:
                raise ConfigError(
                    f"Required config key '{key}' is missing. "
                    f"Set it in {repo_config_path}, {user_config_path}, "
                    f"or via environment variable."
                )

        # -------------------------------------------------------------------------
        # Secret wrapping
        # -------------------------------------------------------------------------
        # After all merging and validation, wrap any value whose schema entry
        # has ``secret: True`` in a Secret() instance.  This prevents accidental
        # leakage via logging, repr, or f-strings -- callers must use
        # ``config["key"].get()`` to access the real value.
        for key, key_schema in schema.items():
            if key_schema.get("secret") and key in config:
                value = config[key]
                # Don't double-wrap if it's already a Secret (defensive).
                # Secret wraps strings only -- coerce to str to satisfy the
                # type contract. TOML values are always str/int/float/bool,
                # and secrets should always be strings in practice.
                if not isinstance(value, Secret):
                    config[key] = Secret(str(value))

    return config
