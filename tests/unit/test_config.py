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
from pathlib import Path

import pytest


class TestLoadTomlConfig:
    """load_config() reads and merges TOML files."""

    def test_loads_default_section(self, tmp_path: Path):
        from qbrd_tools.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nbucket = "releases-staging"\n')

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["bucket"] == "releases-staging"

    def test_env_overrides_default(self, tmp_path: Path):
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

        config = load_config(
            project_name="test-cli",
            repo_config_path=tmp_path / "nonexistent.toml",
        )
        assert config == {}

    def test_user_config_merged_below_repo(self, tmp_path: Path):
        """User config has lowest priority -- repo config overrides it."""
        import os
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

        config_file = tmp_path / ".test-cli.toml"
        config_file.write_text('[default]\nbucket = "from-file"\n')

        monkeypatch.setenv("TEST_CLI_BUCKET", "from-auto-env")

        config = load_config(
            project_name="test-cli",
            repo_config_path=config_file,
        )
        assert config["bucket"] == "from-auto-env"

    def test_explicit_mapping_wins_over_auto_prefix(self, tmp_path: Path, monkeypatch):
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config, ConfigError

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
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config, ConfigError

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
        from qbrd_tools.config import load_config, ConfigError

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
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

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


class TestEnvVarDottedKeys:
    """Auto-prefix env var resolution handles dotted keys correctly."""

    def test_dotted_key_converts_dots_to_underscores(self, tmp_path: Path, monkeypatch):
        """'cloudflare.account_id' -> TEST_CLI_CLOUDFLARE_ACCOUNT_ID."""
        from qbrd_tools.config import load_config

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
        from qbrd_tools.config import load_config

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


class TestUserConfigPermissions:
    """User config file permissions are checked for safety."""

    def test_world_readable_config_is_refused(self, tmp_path: Path):
        """User config readable by others should raise ConfigError."""
        import os
        from qbrd_tools.config import load_config, ConfigError

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
        from qbrd_tools.config import load_config

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
