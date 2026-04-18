"""Tests for the layered config system.

The config module is the most complex piece of the framework. It loads TOML
files from multiple locations and merges them with cascading precedence:

    env vars > env-specific section > [default] section > user-level config

Key behaviors tested:
- TOML parsing from repo and user config files
- Environment cascading (env-specific overrides [default], not replaces)
- Env var resolution (explicit mappings win over auto-prefixed)
- Schema validation (required keys, types, defaults)
- Secret safety (refuse secrets in repo config)
"""
import sys
from pathlib import Path

import pytest


class TestLoadTomlConfig:
    """load_config() reads and merges TOML files."""

    def test_loads_default_section(self, tmp_path: Path):
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nbucket = "releases-staging"\n')

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["bucket"] == "releases-staging"

    def test_env_overrides_default(self, tmp_path: Path):
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text(
            '[default]\nbucket = "staging"\n\n'
            '[env.production]\nbucket = "prod"\n'
        )

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            env="production",
        )
        assert config["bucket"] == "prod"

    def test_env_falls_through_to_default(self, tmp_path: Path):
        """Keys not in the env section should come from [default]."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text(
            '[default]\nbucket = "staging"\nregion = "us-east-1"\n\n'
            '[env.production]\nbucket = "prod"\n'
        )

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            env="production",
        )
        assert config["bucket"] == "prod"
        assert config["region"] == "us-east-1"

    def test_missing_config_file_returns_empty(self, tmp_path: Path):
        from clickwork.config import load_config

        config = load_config(
            project_name="test-cli",
            repo_config_path=tmp_path / "nonexistent.toml",
        )
        assert config == {}

    def test_user_config_merged_below_repo(self, tmp_path: Path):
        """User config has lowest priority -- repo config overrides it."""
        import os
        from clickwork.config import load_config

        repo_config = tmp_path / "repo" / ".test-cli.toml"
        repo_config.parent.mkdir()
        repo_config.write_text('[default]\nbucket = "from-repo"\n')

        user_config = tmp_path / "user" / "config.toml"
        user_config.parent.mkdir()
        user_config.write_text('bucket = "from-user"\nregion = "user-region"\n')
        # User config may contain secrets, so it must be owner-only (matching
        # what a real ~/.config file should be).
        os.chmod(user_config, 0o600)

        config = load_config(
            project_name="test-cli",
            repo_config_path=repo_config,
            user_config_path=user_config,
        )
        assert config["bucket"] == "from-repo"
        assert config["region"] == "user-region"

    def test_spec_style_dotted_toml_keys_are_flattened(self, tmp_path: Path):
        """TOML dotted keys become flat config keys used by command authors."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text(
            '[default]\n'
            'cloudflare.account_id = "abc123"\n'
            'r2.bucket = "releases-staging"\n'
        )

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["cloudflare.account_id"] == "abc123"
        assert config["r2.bucket"] == "releases-staging"


class TestEnvVarResolution:
    """Environment variables have highest priority in config resolution."""

    def test_explicit_env_var_mapping(self, tmp_path: Path, monkeypatch):
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\naccount_id = "from-file"\n')

        monkeypatch.setenv("CF_ACCOUNT_ID", "from-env")

        schema = {
            "account_id": {"env": "CF_ACCOUNT_ID"},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["account_id"] == "from-env"

    def test_auto_prefix_env_var(self, tmp_path: Path, monkeypatch):
        """Auto-prefixed: TEST_CLI_BUCKET reads from env if no explicit mapping."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nbucket = "from-file"\n')

        monkeypatch.setenv("TEST_CLI_BUCKET", "from-auto-env")

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["bucket"] == "from-auto-env"

    def test_explicit_mapping_wins_over_auto_prefix(self, tmp_path: Path, monkeypatch):
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\naccount_id = "from-file"\n')

        monkeypatch.setenv("CF_ACCOUNT_ID", "explicit-wins")
        monkeypatch.setenv("TEST_CLI_ACCOUNT_ID", "auto-loses")

        schema = {
            "account_id": {"env": "CF_ACCOUNT_ID"},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["account_id"] == "explicit-wins"

    def test_project_env_var_fallback(self, tmp_path: Path, monkeypatch):
        """When --env is omitted, {PROJECT_NAME}_ENV selects the environment."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text(
            '[default]\nbucket = "default-bucket"\n\n'
            '[env.staging]\nbucket = "staging-bucket"\n'
        )

        monkeypatch.setenv("TEST_CLI_ENV", "staging")

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            env=None,  # --env not passed
        )
        assert config["bucket"] == "staging-bucket"


class TestSchemaValidation:
    """Config schema validates required keys, types, and defaults."""

    def test_required_key_missing_raises(self, tmp_path: Path):
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        schema = {
            "account_id": {"required": True},
        }

        with pytest.raises(ConfigError, match="account_id"):
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )

    def test_default_fills_missing_key(self, tmp_path: Path):
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        schema = {
            "bucket": {"default": "fallback-bucket"},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["bucket"] == "fallback-bucket"

    def test_secret_in_repo_config_raises(self, tmp_path: Path):
        """Keys tagged secret=True must not appear in repo config."""
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\napi_token = "should-not-be-here"\n')

        schema = {
            "api_token": {"secret": True},
        }

        with pytest.raises(ConfigError, match="secret"):
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )

    def test_type_mismatch_raises(self, tmp_path: Path):
        """Values that don't match the declared type should raise ConfigError."""
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nport = "not-a-number"\n')

        schema = {
            "port": {"type": int},
        }

        with pytest.raises(ConfigError, match="port"):
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )

    def test_type_match_passes(self, tmp_path: Path):
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\nport = 8080\n")

        schema = {
            "port": {"type": int},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["port"] == 8080

    def test_description_field_is_ignored(self, tmp_path: Path):
        """Schema 'description' field is for documentation only -- must not cause errors."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\naccount_id = "abc"\n')

        schema = {
            "account_id": {
                "required": True,
                "description": "The account ID for deployments",
            },
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["account_id"] == "abc"


class TestSecretWrapping:
    """Secret-tagged config values are wrapped in Secret() instances."""

    def test_secret_from_env_var_is_wrapped(self, tmp_path: Path, monkeypatch):
        """A secret-tagged value loaded from an env var should be a Secret.

        WHY: plain strings leak in logs via f-strings, repr, and %-formatting.
        Wrapping in Secret() ensures str(value) returns '***' so accidental
        logging of ctx.config['api_token'] never exposes the real credential.
        """
        from clickwork.config import load_config
        from clickwork._types import Secret

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_API_TOKEN", "super-secret-value")

        schema = {
            "api_token": {"secret": True},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert isinstance(config["api_token"], Secret)
        assert config["api_token"].get() == "super-secret-value"
        # str() must redact the value.
        assert str(config["api_token"]) == "***"

    def test_secret_from_user_config_is_wrapped(self, tmp_path: Path):
        """A secret-tagged value from user config should also be a Secret."""
        import os
        from clickwork.config import load_config
        from clickwork._types import Secret

        repo_config = tmp_path / ".test-cli.toml"
        repo_config.write_text("[default]\n")

        user_config = tmp_path / "user" / "config.toml"
        user_config.parent.mkdir()
        user_config.write_text('api_token = "from-user-config"\n')
        os.chmod(user_config, 0o600)

        schema = {
            "api_token": {"secret": True},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=repo_config,
            user_config_path=user_config,
            schema=schema,
        )
        assert isinstance(config["api_token"], Secret)
        assert config["api_token"].get() == "from-user-config"

    def test_non_secret_value_stays_plain_string(self, tmp_path: Path):
        """Values without secret: True should remain plain strings."""
        from clickwork.config import load_config
        from clickwork._types import Secret

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nbucket = "my-bucket"\n')

        schema = {
            "bucket": {"type": str},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert not isinstance(config["bucket"], Secret)
        assert config["bucket"] == "my-bucket"


class TestEnvVarTypes:
    """Env vars always arrive as strings; schema ``type`` drives coercion.

    WHY this section exists: environment variables at the OS level are
    *always* strings -- ``os.environ`` is ``dict[str, str]``, the C-level
    ``environ`` array is a list of ``NAME=value`` byte strings. clickwork
    preserves that: a value sourced from an env var enters the config
    dict as a ``str``, full stop. The schema's ``type`` field is what
    tells the loader the intended type, and the loader coerces
    str -> int / float / bool at the schema layer before the validation
    check runs. If coercion fails (e.g. ``"not-a-number"`` for
    ``type: int``), ``ConfigError`` is raised with a specific, actionable
    message naming the key and the offending value.

    Pinning this behavior means plugin authors can declare typed config
    keys and feed them from env vars or CI secrets without every
    consumer re-implementing ``int(os.environ["PORT"])`` locally.

    These tests lock the contract for 1.0. Do not relax them without
    updating ``docs/GUIDE.md`` + ``docs/API_POLICY.md`` in the same PR.
    """

    def test_env_var_without_schema_stays_string(self, tmp_path: Path, monkeypatch):
        """An env var override with no schema entry enters config dict AS a string.

        Even when the env var value LOOKS like an int ("42"), without
        a schema type declaration the loader cannot know the intended
        Python type and must leave the env-sourced value as a string.
        Plugin authors who want typed values declare ``type:`` in the
        schema -- that's the explicit opt-in.

        The TOML seed key makes the auto-prefix lookup trip: without
        a schema, the loader only checks env vars for keys already
        present in config files, so we need ``port`` in ``[default]``
        for the env override to apply.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        # Seed the key so the auto-prefix env lookup has something to
        # override. The TOML value is itself a string ("0"); the env
        # var wins and replaces it with "42" -- both remain strings
        # because no schema declared a type.
        config_file.write_text('[default]\nport = "0"\n')

        monkeypatch.setenv("TEST_CLI_PORT", "42")

        # No schema -- loader has no type hint, so the env string passes
        # through untouched.
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["port"] == "42"
        assert isinstance(config["port"], str)

    def test_env_var_coerced_to_int_by_schema(self, tmp_path: Path, monkeypatch):
        """schema type=int + env var "8080" -> config["port"] == 8080 (int).

        This is the "strings in, schema coerces" contract: env delivers
        a string, schema declares int, loader produces int.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_PORT", "8080")

        schema = {
            "port": {"type": int},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["port"] == 8080
        assert isinstance(config["port"], int)

    def test_env_var_coerced_to_float_by_schema(self, tmp_path: Path, monkeypatch):
        """schema type=float + env var "3.14" -> config["ratio"] == 3.14."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_RATIO", "3.14")

        schema = {
            "ratio": {"type": float},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["ratio"] == 3.14
        assert isinstance(config["ratio"], float)

    def test_env_var_type_str_stays_string(self, tmp_path: Path, monkeypatch):
        """schema type=str + env var "42" -> config["tag"] == "42" (unchanged).

        Pins that declaring ``type: str`` is a no-op for env-sourced
        values -- no "helpful" number-detection. Strings stay strings.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_TAG", "42")

        schema = {
            "tag": {"type": str},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["tag"] == "42"
        assert isinstance(config["tag"], str)

    def test_env_var_int_coercion_failure_raises(self, tmp_path: Path, monkeypatch):
        """Un-coercible env value raises ConfigError naming key + value.

        "not-a-number" cannot be parsed as int. The loader must fail
        loudly rather than silently dropping the value or defaulting to
        zero. The message names the key so the operator can fix the
        offending env var.
        """
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_PORT", "not-a-number")

        schema = {
            "port": {"type": int},
        }
        with pytest.raises(ConfigError, match="port"):
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )

    def test_env_var_float_coercion_failure_raises(self, tmp_path: Path, monkeypatch):
        """Un-coercible float value raises ConfigError."""
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_RATIO", "definitely-not-a-float")

        schema = {
            "ratio": {"type": float},
        }
        with pytest.raises(ConfigError, match="ratio"):
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )

    @pytest.mark.parametrize("truthy", ["true", "True", "TRUE", "1", "yes", "YES", "on", "On"])
    def test_env_var_bool_truthy_strings_coerce_to_true(self, tmp_path: Path, monkeypatch, truthy: str):
        """Explicit truthy-string set: true/1/yes/on (case-insensitive).

        WHY pin the exact set: Python's ``bool("false")`` is ``True``
        (non-empty string), which is the classic foot-cannon. We pick a
        short, explicit, case-insensitive allowlist that matches shell
        conventions and reject everything else. No surprises.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_ENABLED", truthy)

        schema = {
            "enabled": {"type": bool},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["enabled"] is True

    @pytest.mark.parametrize("falsy", ["false", "False", "FALSE", "0", "no", "NO", "off", "Off"])
    def test_env_var_bool_falsy_strings_coerce_to_false(self, tmp_path: Path, monkeypatch, falsy: str):
        """Explicit falsy-string set: false/0/no/off (case-insensitive)."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_ENABLED", falsy)

        schema = {
            "enabled": {"type": bool},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["enabled"] is False

    def test_env_var_bool_ambiguous_string_raises(self, tmp_path: Path, monkeypatch):
        """Unknown bool-ish string raises ConfigError.

        "maybe" isn't in the truthy or falsy set. Rather than guessing,
        raise so the operator fixes the env var to use one of the
        accepted tokens. This prevents the classic ``bool("false") ==
        True`` foot-cannon.
        """
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_ENABLED", "maybe")

        schema = {
            "enabled": {"type": bool},
        }
        with pytest.raises(ConfigError, match="enabled"):
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )

    def test_toml_int_value_unchanged_by_schema(self, tmp_path: Path):
        """TOML already-typed int values are not re-coerced.

        TOML carries types natively (``port = 8080`` parses as int).
        The schema type check still runs (catches mismatches), but no
        coercion pass touches values that already match their declared
        type. Pinned so a future "always coerce" refactor can't silently
        round-trip TOML ints through ``str`` -> ``int``.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\nport = 8080\n")

        schema = {
            "port": {"type": int},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["port"] == 8080
        assert isinstance(config["port"], int)

    def test_toml_string_value_coerced_by_schema(self, tmp_path: Path):
        """A TOML string literal (``port = "8080"``) is coerced to ``int``.

        The coercion rule is uniform across string sources: env vars,
        TOML string literals, and user-supplied overrides all funnel
        through the same ``_coerce_value`` call. Pinning this case
        prevents a future refactor from narrowing coercion to env-only
        (the original intent of the change in #41) without catching
        the broader behavior the implementation actually ships.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        # Note the QUOTES: TOML parses this as a string, not an int.
        config_file.write_text('[default]\nport = "8080"\n')

        schema = {
            "port": {"type": int},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["port"] == 8080
        assert isinstance(config["port"], int)

    def test_toml_string_value_coerced_to_bool(self, tmp_path: Path):
        """Sibling of the int case: a quoted TOML string coerces to bool.

        Pins that the bool allowlist (_TRUTHY_STRINGS / _FALSY_STRINGS)
        applies to TOML-sourced strings the same way it applies to env
        vars. A user who wrote ``debug = "true"`` in TOML (instead of
        the native ``debug = true``) should still get ``True`` under
        ``type: bool`` rather than a type-mismatch ConfigError.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\ndebug = "true"\n')

        schema = {
            "debug": {"type": bool},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["debug"] is True

    def test_secret_coercion_error_redacts_value(self, tmp_path: Path, monkeypatch):
        """A failing coercion on a ``secret: True`` key redacts the value.

        Without this redaction, a misconfigured secret env var (e.g.
        ``CLI_TOKEN=not-a-number`` against ``type: int``) would
        surface the raw token in the ConfigError message and leak
        into logs / stderr / CI output. Pinned so a future edit to
        the error path can't reintroduce the leak.
        """
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        # Use an obviously-unique sentinel so the assertion below is
        # decisive -- if this token ever shows up in the exception
        # message, the redaction is broken.
        secret_token = "not-a-number-SENTINEL-d41d8cd9"
        monkeypatch.setenv("TEST_CLI_TOKEN", secret_token)

        schema = {
            "token": {"type": int, "secret": True},
        }
        with pytest.raises(ConfigError) as excinfo:
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )
        msg = str(excinfo.value)
        # The redaction marker MUST appear (positive confirmation
        # the branch fired), and the raw token MUST NOT appear
        # (negative confirmation nothing leaked).
        assert "<redacted>" in msg
        assert secret_token not in msg
        # The key name should still appear so the operator knows
        # which env var to fix.
        assert "token" in msg

    def test_non_secret_coercion_error_still_echoes_value(self, tmp_path: Path, monkeypatch):
        """Redaction is scoped to secret keys -- non-secret errors keep the value.

        The operator debugging a non-secret misconfiguration needs to
        see the bad value to fix it. Pinning this path prevents an
        over-eager redaction from swallowing useful diagnostics for
        regular keys.
        """
        from clickwork.config import load_config, ConfigError

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_PORT", "not-a-port")

        schema = {
            "port": {"type": int},
        }
        with pytest.raises(ConfigError) as excinfo:
            load_config(
                project_name="test-cli",
                repo_config_path=config_file,
                schema=schema,
            )
        msg = str(excinfo.value)
        assert "not-a-port" in msg
        assert "<redacted>" not in msg

    def test_explicit_env_mapping_coerced_same_as_auto_prefix(self, tmp_path: Path, monkeypatch):
        """Coercion applies regardless of which env-var mechanism sourced the value.

        Both the explicit ``env: "CF_PORT"`` mapping and the auto-prefix
        ``TEST_CLI_PORT`` path deliver strings from ``os.environ``. The
        schema ``type`` pass runs after the merge, so it coerces either
        source uniformly. Pinning both paths prevents a future refactor
        from coercing one but not the other.
        """
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("CF_PORT", "9090")

        schema = {
            "port": {"type": int, "env": "CF_PORT"},
        }
        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["port"] == 9090
        assert isinstance(config["port"], int)


class TestEnvVarDottedKeys:
    """Auto-prefix env var resolution handles dotted keys correctly."""

    def test_dotted_key_converts_dots_to_underscores(self, tmp_path: Path, monkeypatch):
        """'cloudflare.account_id' -> TEST_CLI_CLOUDFLARE_ACCOUNT_ID."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\ncloudflare.account_id = "from-file"\n')

        monkeypatch.setenv("TEST_CLI_CLOUDFLARE_ACCOUNT_ID", "from-env")

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["cloudflare.account_id"] == "from-env"

    def test_schema_only_key_resolved_from_env(self, tmp_path: Path, monkeypatch):
        """Keys in schema but not in any config file should still resolve from env."""
        from clickwork.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text("[default]\n")

        monkeypatch.setenv("TEST_CLI_NEW_KEY", "from-env")

        schema = {
            "new_key": {},
        }

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
            schema=schema,
        )
        assert config["new_key"] == "from-env"


@pytest.mark.skipif(sys.platform == "win32", reason="Unix permission model does not apply on Windows")
class TestUserConfigPermissions:
    """User config file permissions are checked for safety."""

    def test_world_readable_config_is_refused(self, tmp_path: Path):
        """User config readable by others should raise ConfigError."""
        import os
        from clickwork.config import load_config, ConfigError

        user_config = tmp_path / "config.toml"
        user_config.write_text('token = "secret"\n')
        os.chmod(user_config, 0o644)  # world-readable

        repo_config = tmp_path / ".test-cli.toml"
        repo_config.write_text("[default]\n")

        with pytest.raises(ConfigError, match="permission"):
            load_config(
                project_name="test-cli",
                repo_config_path=repo_config,
                user_config_path=user_config,
            )

    def test_owner_only_config_passes(self, tmp_path: Path):
        """User config with 0o600 permissions should load fine."""
        import os
        from clickwork.config import load_config

        user_config = tmp_path / "config.toml"
        user_config.write_text('region = "us-east-1"\n')
        os.chmod(user_config, 0o600)

        repo_config = tmp_path / ".test-cli.toml"
        repo_config.write_text("[default]\n")

        config = load_config(
            project_name="test-cli",
            repo_config_path=repo_config,
            user_config_path=user_config,
        )
        assert config["region"] == "us-east-1"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file-mode semantics don't apply on Windows",
    )
    def test_group_writable_config_is_refused(self, tmp_path: Path):
        """User config with ANY group/other bit (e.g. 0o620) must fail.

        WHY this regression test exists: the permission check was
        extracted into _check_owner_only_permissions and tightened from
        "group/other READ only" to "ANY group/other bit" so group-
        writable secrets files (a tampering risk even when not readable)
        are also rejected. The old test covered 0o644; this one pins
        the group-WRITE-only case so a future reader can't relax the
        check back to read-only without a loud test failure.
        """
        import os
        from clickwork.config import load_config, ConfigError

        user_config = tmp_path / "config.toml"
        user_config.write_text('token = "secret"\n')
        # 0o620 = owner rw, group w, other ---. Not group-readable,
        # so a pre-tightening check (S_IRGRP | S_IROTH) would let this
        # through. The current S_IRWXG | S_IRWXO check catches it.
        os.chmod(user_config, 0o620)

        repo_config = tmp_path / ".test-cli.toml"
        repo_config.write_text("[default]\n")

        with pytest.raises(ConfigError, match="permission"):
            load_config(
                project_name="test-cli",
                repo_config_path=repo_config,
                user_config_path=user_config,
            )


class TestLoadEnvFile:
    """load_env_file() reads dotenv-style files into a plain dict.

    This helper is intentionally *not* integrated into load_config() -- it
    is a sibling utility so callers (deploy/release/admin commands) can
    source credentials from a .env file and pass them to ctx.run(env=...)
    or inject into os.environ themselves. Keeping it standalone means
    the TOML pipeline stays focused on structured data, and the dotenv
    grammar stays deliberately tiny (no variable substitution, no
    command substitution, no heredocs -- see the load_env_file()
    docstring itself for the explicit out-of-scope list; the module
    docstring on config.py describes TOML layered config, not dotenv).
    """

    def test_load_env_file_parses_simple_key_value(self, tmp_path: Path):
        """The simplest case: one KEY=VALUE line produces one dict entry."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("K=v\n")
        # Owner-only permissions: this file may hold secrets, same treatment
        # as user config.
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "v"}

    def test_load_env_file_strips_export_prefix(self, tmp_path: Path):
        """A leading 'export ' (shell-style) is stripped so the same file
        works with both ``source .env`` and load_env_file()."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("export K=v\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "v"}

    def test_load_env_file_strips_leading_whitespace_around_equals(self, tmp_path: Path):
        """``KEY = value`` and ``KEY= value`` produce a value without the
        leading space.

        WHY this regression test exists: the partition-on-'=' parse
        leaves leading whitespace on the value side for forms like
        ``KEY = value`` (spaces around '=', a common shape in hand-edited
        .env files). Without lstrip, the value would be ``" value"`` with
        a real leading space -- easy to miss and trivial to mangle a
        secret token. Pins the strip so a regression produces a loud
        test failure rather than a silent content bug.
        """
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("KEY = value\nKEY2= value\nKEY3 =value\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {
            "KEY": "value",
            "KEY2": "value",
            "KEY3": "value",
        }

    def test_load_env_file_strips_whitespace_before_quote_unwrap(self, tmp_path: Path):
        """``KEY = "value"`` must still unwrap the quotes.

        If the value-side whitespace isn't stripped BEFORE the quote-
        unwrap check, the check sees a value starting with a space and
        doesn't recognise the quotes as wrapping. The parser would then
        leave the quote characters literally in the stored value
        (``' "value"'`` instead of ``"value"``) -- subtle enough that
        callers wouldn't notice until the subprocess tries to use the
        literal quoted form of the token.
        """
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text('KEY = "wrapped"\nK2=   \'singly\'\n')
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"KEY": "wrapped", "K2": "singly"}

    def test_load_env_file_preserves_whitespace_inside_quotes(self, tmp_path: Path):
        """Leading whitespace INSIDE quotes is intentional and preserved.

        The lstrip on the pre-quote-unwrap value removes only whitespace
        AROUND the '=' operator; whitespace kept inside a quoted value is
        a deliberate user choice (e.g. a token formatted with a leading
        space, or a human-readable prefix) and must survive.
        """
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text('KEY = "  leading spaces preserved"\n')
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"KEY": "  leading spaces preserved"}

    def test_load_env_file_handles_double_quotes(self, tmp_path: Path):
        """Values wrapped in double quotes have the quotes stripped so
        spaces and other whitespace-adjacent characters survive parsing."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text('K="v with spaces"\n')
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "v with spaces"}

    def test_load_env_file_handles_single_quotes(self, tmp_path: Path):
        """Single quotes are treated identically to double quotes for the
        purposes of this minimal parser -- the grammar deliberately does
        not distinguish shell's 'literal vs interpolated' semantics."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("K='v'\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "v"}

    def test_load_env_file_skips_comments(self, tmp_path: Path):
        """Full-line comments (# ...) are skipped; inline trailing comments
        are NOT supported -- the `#` in ``K=val # comment`` is treated as
        part of the value. See the load_env_file() docstring's "Not
        supported" section for the full anti-feature list (variable
        substitution, inline comments, heredocs, etc.)."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("# this is a comment\nK=v\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "v"}

    def test_load_env_file_skips_blank_lines(self, tmp_path: Path):
        """Blank lines (whitespace-only or empty) are harmless and ignored."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("\n\nK=v\n\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "v"}

    def test_load_env_file_handles_multiple_keys(self, tmp_path: Path):
        """Multiple KEY=VALUE lines produce multiple dict entries."""
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("A=1\nB=2\nC=3\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"A": "1", "B": "2", "C": "3"}

    def test_load_env_file_raises_on_missing_file(self, tmp_path: Path):
        """Missing env file is an error -- unlike user config, callers
        ask for this file by name, so absence is a bug, not a fallback."""
        from clickwork.config import load_env_file

        # Don't create the file -- load_env_file should raise.
        # FileNotFoundError bubbles up from os.open; this keeps the
        # semantics obvious to the caller.
        with pytest.raises(FileNotFoundError):
            load_env_file(tmp_path / "missing.env")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file-mode semantics don't apply on Windows",
    )
    def test_load_env_file_rejects_world_readable_file(self, tmp_path: Path):
        """A .env file commonly holds secrets; any group/other bit rejects it.

        The implementation forbids any group or other permission bit
        (read, write, or execute) -- not just "looser than 0o600". Modes
        like 0o400 or 0o700 pass the check; 0o644 here fails because
        group/other have the read bit set.
        """
        import os
        from clickwork.config import load_env_file, ConfigError

        env_file = tmp_path / ".env"
        env_file.write_text("K=v\n")
        os.chmod(env_file, 0o644)  # world-readable

        with pytest.raises(ConfigError, match="permission"):
            load_env_file(env_file)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file-mode semantics don't apply on Windows",
    )
    def test_load_env_file_rejects_group_writable_file(self, tmp_path: Path):
        """Group-WRITE alone (0o620) must also fail -- tampering risk.

        WHY: a group-writable secrets file lets another user replace or
        modify our secrets even when they can't read them directly. The
        permission guard rejects ANY group/other bit, not just readability.
        This regression test pins that -- without it, someone could
        accidentally relax the check back to S_IRGRP|S_IROTH and lose
        tamper-resistance without any test failing.
        """
        import os
        from clickwork.config import load_env_file, ConfigError

        env_file = tmp_path / ".env"
        env_file.write_text("K=v\n")
        # 0o620 = owner rw, group w only, other ---. Not group-readable,
        # so a pre-tightening check (S_IRGRP | S_IROTH) would let this
        # through. The current S_IRWXG | S_IRWXO check catches it.
        os.chmod(env_file, 0o620)

        with pytest.raises(ConfigError, match="permission"):
            load_env_file(env_file)

    def test_load_env_file_raises_on_malformed_line(self, tmp_path: Path):
        """A line without '=' is ambiguous -- the parser refuses it rather
        than silently dropping or misinterpreting. The error message must
        name the 1-based line number so the caller can locate the problem."""
        import os
        from clickwork.config import load_env_file, ConfigError

        env_file = tmp_path / ".env"
        # Line 1: valid. Line 2: malformed (no '='). Line 3: valid.
        env_file.write_text("A=1\nBROKEN\nC=3\n")
        os.chmod(env_file, 0o600)

        with pytest.raises(ConfigError, match="line 2"):
            load_env_file(env_file)

    def test_load_env_file_raises_on_empty_key(self, tmp_path: Path):
        """'=value' (or 'export =value') has no variable name.

        Without a guard, the parser would return {"": "value"}, which is
        never a valid environment variable name and blows up later when
        passed to subprocess or os.environ. Fail fast with a line number
        so the caller can fix the bad line immediately.
        """
        import os
        from clickwork.config import load_env_file, ConfigError

        env_file = tmp_path / ".env"
        # Line 1: valid. Line 2: empty key. Line 3: valid.
        env_file.write_text("A=1\n=orphaned\nC=3\n")
        os.chmod(env_file, 0o600)

        with pytest.raises(ConfigError, match="line 2"):
            load_env_file(env_file)

    def test_load_env_file_raises_on_empty_key_after_export(self, tmp_path: Path):
        """'export =value' is the same bug, just with the export prefix.

        Ensures the empty-key guard runs AFTER the export strip so we
        don't miss the bad case.
        """
        import os
        from clickwork.config import load_env_file, ConfigError

        env_file = tmp_path / ".env"
        env_file.write_text("export =value\n")
        os.chmod(env_file, 0o600)

        with pytest.raises(ConfigError, match="line 1"):
            load_env_file(env_file)

    def test_load_env_file_does_not_expand_variables(self, tmp_path: Path):
        """Variable substitution (K=$OTHER) is deliberately unsupported.

        This is an anti-test: the parser stores the literal string '$OTHER'
        rather than trying to resolve it from os.environ or other entries
        in the file. If someone "helpfully" adds substitution later, this
        test will flag it as a breaking change. Callers who want shell
        semantics should use 'sh -c "set -a; source .env; env"' instead.
        """
        import os
        from clickwork.config import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("K=$OTHER\n")
        os.chmod(env_file, 0o600)

        assert load_env_file(env_file) == {"K": "$OTHER"}
