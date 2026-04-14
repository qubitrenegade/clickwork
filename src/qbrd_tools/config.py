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
import stat
import sys
from pathlib import Path

# tomllib is stdlib in Python 3.11+. No external dependency needed.
import tomllib

from qbrd_tools._types import Secret


class ConfigError(Exception):
    """Raised when config validation fails.

    This is a user-facing error -- the message should be actionable,
    telling them which key is missing/invalid and where to fix it.
    """


def _normalize_prefix(project_name: str) -> str:
    """Convert a project name to a shell-safe environment variable prefix.

    Hyphens become underscores and the result is uppercased so the prefix
    conforms to POSIX env var naming rules (e.g., ``orbit-admin`` ->
    ``ORBIT_ADMIN``).

    Args:
        project_name: The CLI project name, possibly hyphenated.

    Returns:
        An uppercase, underscore-delimited prefix string.
    """
    return project_name.replace("-", "_").upper()


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
    schemas in qbrd-tools use flat dotted keys, so we normalize TOML data
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


def _check_user_config_permissions(path: Path) -> bytes | None:
    """Refuse to load user config if it is readable by group or others.

    User config may contain secrets (API tokens, personal credentials), so
    it must be owner-only (mode ``0o600``). On Windows this check is skipped
    because the Unix permission model does not apply.

    We use ``fstat()`` on an already-open file descriptor instead of
    ``os.stat()`` on the path to avoid a TOCTOU (time-of-check/time-of-use)
    race: between stat() and open() an attacker could swap the file. Opening
    first and then fstat()-ing the fd ensures we inspect the exact file we
    will read.

    After confirming the permissions are safe, the file contents are read from
    the same open fd and returned so the caller can parse them without
    re-opening the file by path (which would reintroduce the TOCTOU window).

    Args:
        path: Path to the user config file to check. If the file does not
            exist the function returns None (missing config is fine).

    Returns:
        The raw bytes of the file if it exists and has safe permissions,
        or None if the file does not exist.

    Raises:
        ConfigError: If the file exists and is readable by group or other
            users (i.e., mode bits include S_IRGRP or S_IROTH).
    """
    if not path.is_file():
        # Nothing to check -- missing user config is fine (it's optional).
        return None

    # Open the file first, then stat the open fd (TOCTOU-safe).
    fd = os.open(str(path), os.O_RDONLY)
    try:
        # Skip permission check on Windows where Unix permission bits
        # are not meaningful.
        if sys.platform != "win32":
            st = os.fstat(fd)
            mode = stat.S_IMODE(st.st_mode)
            # S_IRGRP = group-read bit, S_IROTH = other-read bit.
            # Either being set means the file is too permissive for secrets.
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                raise ConfigError(
                    f"User config {path} has unsafe permission {oct(mode)} "
                    f"(readable by group/others). Secrets may be exposed.\n"
                    f"Fix with: chmod 600 {path}"
                )

        # Read from the already-open fd so we don't reopen the file by path.
        # This is the TOCTOU-safe read: we hold the fd across the permission
        # check and the read, so no substitution can happen between them.
        return os.read(fd, os.fstat(fd).st_size)
    finally:
        # Always close the fd, even if an exception is raised above.
        os.close(fd)


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
    prefix = _normalize_prefix(project_name)

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
    # _check_user_config_permissions opens the file, checks permissions via
    # fstat(), and returns the raw bytes read from the same fd.  We then parse
    # those bytes directly, avoiding a second open() that would reintroduce a
    # TOCTOU window between the permission check and the read.
    user_config_bytes = _check_user_config_permissions(user_config_path)
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
