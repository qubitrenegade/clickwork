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
from pathlib import Path

# tomllib is stdlib in Python 3.11+. No external dependency needed.
import tomllib

from clickwork._types import Secret, normalize_prefix


class ConfigError(Exception):
    """Raised when config validation fails.

    This is a user-facing error -- the message should be actionable,
    telling them which key is missing/invalid and where to fix it.
    """


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


def _flatten_mapping(data: dict, prefix: str = "") -> dict[str, object]:
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


def _load_toml(path: Path) -> dict:
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


def _load_toml_from_bytes(data: bytes) -> dict:
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
    """Raise ConfigError unless the file behind ``fd`` is owner-only (chmod 600).

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
    it must be owner-only (mode ``0o600``). On Windows this check is skipped
    because the Unix permission model does not apply.

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
        * Backticks / command substitution (``K=`date```).
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

        from clickwork.config import load_env_file

        env = load_env_file(Path(".env"))
        # env == {"API_TOKEN": "...", "REGION": "us-east-1"}

        # Pass to a subprocess without mutating the parent environment:
        ctx.run("./deploy.sh", env={**os.environ, **env})

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
        _check_owner_only_permissions(fd, path, kind=".env file")
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
            line = line[len("export "):].lstrip()

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
            raise ConfigError(
                f"line {lineno}: malformed entry (no '=' separator)"
            )

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
            raise ConfigError(
                f"line {lineno}: empty key (missing name before '=')"
            )

        # Unwrap matching surrounding quotes. We only strip quotes when the
        # entire value is wrapped -- a value like 'foo"bar' stays literal.
        # This matches the behaviour users expect from a minimal dotenv
        # parser, without pulling in the full shell-quoting rules.
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]

        result[key] = value

    return result


def load_config(
    project_name: str,
    repo_config_path: Path | None = None,
    user_config_path: Path | None = None,
    env: str | None = None,
    schema: dict | None = None,
) -> dict:
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
    repo_data = _load_toml(repo_config_path)

    # The [default] section provides baseline values for all environments.
    repo_default = _flatten_mapping(repo_data.get("default", {}))

    # -------------------------------------------------------------------------
    # Layer 2: Env-specific section
    # -------------------------------------------------------------------------
    # [env.production], [env.staging], etc. overlay [default] -- keys present
    # in the env section override [default], but keys absent from the env
    # section still fall through to [default].
    repo_env: dict = {}
    if env and "env" in repo_data and env in repo_data["env"]:
        repo_env = _flatten_mapping(repo_data["env"][env])

    # -------------------------------------------------------------------------
    # Build the merged config dict: user < default < env-specific
    # -------------------------------------------------------------------------
    # dict.update() means the last write wins, so we apply layers in order
    # from lowest to highest priority.
    config: dict = {}
    config.update(user_config)   # Layer 4: lowest priority
    config.update(repo_default)  # Layer 3: overrides user
    config.update(repo_env)      # Layer 2: overrides default

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

            # Type check: validate the resolved value matches the declared type.
            # This catches mistakes like bucket = 42 in TOML when a string was
            # expected, or a stale env var with the wrong format.
            expected_type = key_schema.get("type")
            if expected_type and key in config:
                if not isinstance(config[key], expected_type):
                    raise ConfigError(
                        f"Config key '{key}' has type {type(config[key]).__name__}, "
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
