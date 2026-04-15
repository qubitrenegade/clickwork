"""Unit tests for clickwork._types.

Tests are written before the implementation (TDD / Red phase).
Each test class covers one exported type: Secret, CliProcessError, CliContext.

Run with:
    uv run pytest tests/unit/test_types.py -v
"""
import copy
import pickle
import subprocess

import pytest


# ---------------------------------------------------------------------------
# Secret
# ---------------------------------------------------------------------------

class TestSecret:
    """Verify that Secret wraps a string and keeps it out of every repr path."""

    def test_str_is_redacted(self):
        # str() must never reveal the actual value -- callers log context objects
        # and accidentally leaking a token via str(ctx) is a hard-to-audit bug.
        from clickwork._types import Secret

        s = Secret("super-secret")
        assert str(s) == "***"

    def test_repr_is_redacted(self):
        # repr() is used by debuggers, pytest output, and dataclass __repr__.
        # The type name is allowed (helps debugging), but the value must not appear.
        from clickwork._types import Secret

        s = Secret("super-secret")
        r = repr(s)
        assert "super-secret" not in r
        assert "Secret" in r

    def test_get_returns_actual_value(self):
        # .get() is the intentional, explicit escape hatch to retrieve the value.
        # Using it at a call site signals to reviewers that secrets are in play.
        from clickwork._types import Secret

        s = Secret("my-token")
        assert s.get() == "my-token"

    def test_fstring_is_redacted(self):
        # f-strings call __format__ which delegates to __str__ -- verify the chain.
        from clickwork._types import Secret

        s = Secret("tok-abc123")
        formatted = f"token={s}"
        assert "tok-abc123" not in formatted
        assert formatted == "token=***"

    def test_bool_is_true_when_nonempty(self):
        # Allow `if ctx.token:` guards without exposing the value.
        from clickwork._types import Secret

        assert bool(Secret("x")) is True
        assert bool(Secret("")) is False

    def test_vars_does_not_leak_value(self):
        # vars() / __dict__ is the most common accidental leak.  __slots__ removes
        # __dict__ entirely; accessing vars() on a slotted object raises TypeError.
        from clickwork._types import Secret

        s = Secret("secret-value")
        with pytest.raises(TypeError):
            vars(s)

    def test_pickle_raises(self):
        # Pickling would write the value to disk/network.  We block it explicitly.
        from clickwork._types import Secret

        s = Secret("cannot-pickle-me")
        with pytest.raises(TypeError):
            pickle.dumps(s)

    def test_copy_returns_redacted(self):
        # copy.copy() should produce a new Secret wrapping the same value.
        # The internal attribute must not be reachable via normal dict access.
        from clickwork._types import Secret

        s = Secret("clone-me")
        s2 = copy.copy(s)
        # The copy still returns the real value through the sanctioned API.
        assert s2.get() == "clone-me"
        # But vars() is still blocked (slots still applies).
        with pytest.raises(TypeError):
            vars(s2)


# ---------------------------------------------------------------------------
# CliProcessError
# ---------------------------------------------------------------------------

class TestCliProcessError:
    """Verify CliProcessError wraps CalledProcessError cleanly."""

    def _make_cpe(self, returncode: int = 1, cmd: str = "git status", stderr: str = "fatal: not a git repo"):
        # Helper to construct a CalledProcessError -- CalledProcessError takes
        # (returncode, cmd, output, stderr) positional args.
        return subprocess.CalledProcessError(
            returncode=returncode,
            cmd=cmd,
            output=None,
            stderr=stderr,
        )

    def test_wraps_called_process_error(self):
        # CliProcessError must surface the three most useful fields from the
        # underlying CalledProcessError without callers having to unwrap it.
        from clickwork._types import CliProcessError

        cpe = self._make_cpe(returncode=128, cmd="git push", stderr="permission denied")
        err = CliProcessError(cpe)

        assert err.returncode == 128
        assert err.cmd == "git push"
        assert err.stderr == "permission denied"

    def test_is_exception(self):
        # Must be raise-able as a normal Python exception.
        from clickwork._types import CliProcessError

        cpe = self._make_cpe()
        err = CliProcessError(cpe)
        assert isinstance(err, Exception)

    def test_message_includes_command_and_exit_code(self):
        # str(err) surfaces in logs and pytest output -- include the command and
        # exit code so the failure is immediately actionable without a traceback.
        from clickwork._types import CliProcessError

        cpe = self._make_cpe(returncode=2, cmd="npm test", stderr="")
        err = CliProcessError(cpe)
        msg = str(err)

        assert "npm test" in msg
        assert "2" in msg


# ---------------------------------------------------------------------------
# CliContext
# ---------------------------------------------------------------------------

class TestCliContext:
    """Verify CliContext construction, config access, and callable fields."""

    def test_construction_with_defaults(self, make_cli_context):
        # make_cli_context is provided by tests/conftest.py; it creates a
        # CliContext with sensible defaults so we don't repeat 7 kwargs everywhere.
        ctx = make_cli_context()

        assert ctx.config == {}
        assert ctx.env is None
        assert ctx.dry_run is False
        assert ctx.verbose == 0
        assert ctx.quiet is False
        assert ctx.yes is False
        assert ctx.logger is not None

    def test_config_get_returns_value(self, make_cli_context):
        # config is a plain dict -- verify basic dict semantics are preserved.
        ctx = make_cli_context(config={"deploy_branch": "main"})
        assert ctx.config.get("deploy_branch") == "main"

    def test_config_get_returns_none_for_missing(self, make_cli_context):
        # Missing keys should return None (default dict.get behaviour), not raise.
        ctx = make_cli_context(config={})
        assert ctx.config.get("nonexistent") is None

    def test_callable_fields_default_to_none(self, make_cli_context):
        # All six callable fields must be None by default; they're injected by the
        # CLI harness at runtime, so the dataclass must accept absent values.
        ctx = make_cli_context()

        assert ctx.run is None
        assert ctx.capture is None
        assert ctx.require is None
        assert ctx.confirm is None
        assert ctx.confirm_destructive is None
        assert ctx.run_with_confirm is None

    def test_callable_fields_are_callable_when_bound(self, make_cli_context):
        # Verify the fields accept callables (lambdas or bound methods) without
        # type errors -- the dataclass must not enforce non-None on these fields.
        sentinel = object()
        ctx = make_cli_context(run=lambda *a, **kw: sentinel)

        assert ctx.run is not None
        assert callable(ctx.run)
        assert ctx.run() is sentinel
