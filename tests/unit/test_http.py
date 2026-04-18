"""Tests for the clickwork.http module: stdlib HTTP client with allowlist.

The key design decisions pinned by these tests:

1. stdlib only -- urllib.request/urllib.parse/json/base64. No requests/httpx.
2. Allowlist preflight -- if populated, URL host MUST match (case-insensitive)
   or ValueError is raised BEFORE urlopen is invoked. Not HttpError, because
   no HTTP status exists for a request that never left the process.
3. Auth precedence -- explicit ``headers["Authorization"]`` wins over
   ``bearer_token`` or ``basic_auth``. bearer_token/basic_auth accept a
   ``Secret`` and unwrap only at header-build time.
4. JSON auto-parse -- response Content-Type must have the
   ``application/json`` media type (case-insensitive); parameters such
   as ``; charset=utf-8`` are allowed after it. Non-matching media
   types like ``application/jsonx`` are explicitly NOT treated as
   JSON. ``parse_json=False`` forces raw bytes even on a JSON
   content-type.
5. Error model -- non-2xx responses arrive via ``urllib.error.HTTPError`` and
   are re-raised as ``HttpError`` with all four attrs populated (status_code,
   headers, url, response_body). Transport errors (timeout, DNS, ECONNREFUSED)
   propagate unmodified.
6. Redaction -- log lines include ``[auth: <redacted>]`` when auth was used,
   never the token/password value itself.
7. Body encoding -- ``body=None`` means "no request body is sent at all"
   (matches ``urllib.request.Request(data=None)``; this is what GET and
   DELETE default to). Any other JSON-type body (dict, list, str, int,
   float, bool) is ``json.dumps``-encoded and the Content-Type header
   is set to ``application/json`` unless the caller already set one.
   Raw ``bytes`` are sent as-is with no Content-Type override.
8. Scheme guard -- only http/https URLs are accepted. file://, ftp://,
   etc. raise ValueError up front so ``clickwork.http`` can't be turned
   into a generic URL fetcher by untrusted input.
9. Empty-list allowlist fails closed -- ``allowed_hosts=[]`` raises
   ValueError ("pass None to disable") rather than silently disabling
   the check. An empty list at runtime is almost always a config bug.

Every test mocks ``clickwork.http._dispatch_request`` -- no real
network traffic. ``_dispatch_request`` is the module-level wrapper
around the custom no-redirect opener; patching it gives tests a
single, stable seam (see ``clickwork.http._dispatch_request`` for
why the custom opener exists).
"""
from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(
    *,
    status: int = 200,
    body: bytes = b"",
    content_type: str = "application/json",
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock object that quacks like ``urllib.request.urlopen``'s return.

    The real return value is an ``http.client.HTTPResponse`` used as a context
    manager. We model that with a MagicMock that supports ``__enter__`` /
    ``__exit__`` and exposes ``.read()`` plus ``.headers`` (dict-like).
    """
    headers: dict[str, str] = {"Content-Type": content_type}
    if extra_headers:
        headers.update(extra_headers)

    resp = MagicMock()
    # urlopen's return is a context manager (``with urlopen(req) as r:``),
    # so __enter__ must return the response object itself.
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = body
    resp.status = status
    # ``resp.headers`` supports both item access and .items() in real HTTPResponse.
    # MagicMock lets us fake both by wrapping a plain dict subclass.
    resp.headers = _HeaderDict(headers)
    return resp


class _HeaderDict(dict):
    """Case-insensitive-ish header dict that also exposes .items() / .get().

    ``http.client.HTTPMessage`` supports case-insensitive access; for our
    mock we only need enough of that surface for the http module's lookups.
    """

    def get(self, key, default=None):  # type: ignore[override]
        # Case-insensitive single-key lookup (real HTTPMessage.get behaves
        # this way). Simple linear scan is fine for the small fixture dicts
        # we build in tests.
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _capture_request() -> tuple[MagicMock, list]:
    """Return (urlopen_mock, captured) where captured holds the Request objects.

    The urllib.request API passes a Request object as the first positional arg
    to urlopen. Our mock records every Request so tests can assert on headers,
    method, data, and timeout.
    """
    captured: list = []
    mock = MagicMock()

    def _side_effect(req, *args, **kwargs):
        # Record (request, kwargs) so tests can inspect both the Request
        # payload and the timeout that urlopen received.
        captured.append((req, kwargs))
        return _make_response(body=b'{}', content_type="application/json")

    mock.side_effect = _side_effect
    return mock, captured


# ---------------------------------------------------------------------------
# GET: response parsing
# ---------------------------------------------------------------------------

class TestGetResponseParsing:
    """Response body decoding rules: JSON auto-parse vs raw bytes."""

    def test_get_parses_json_when_content_type_is_application_json(self):
        from clickwork import http

        resp = _make_response(body=b'{"ok": true}', content_type="application/json")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == {"ok": True}

    def test_get_parses_json_with_charset_suffix(self):
        """``application/json; charset=utf-8`` must also auto-parse.

        Many servers append the charset parameter. The content-type prefix
        match (not exact match) pins that we handle this idiomatic variant.
        """
        from clickwork import http

        resp = _make_response(
            body=b'{"ok": true}', content_type="application/json; charset=utf-8",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == {"ok": True}

    def test_get_returns_bytes_for_non_json(self):
        """Non-JSON content-type should return the raw bytes unchanged."""
        from clickwork import http

        resp = _make_response(body=b"<html></html>", content_type="text/html")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/")

        assert result == b"<html></html>"

    def test_get_parse_json_false_forces_raw(self):
        """``parse_json=False`` returns bytes even when Content-Type is JSON."""
        from clickwork import http

        resp = _make_response(body=b'{"ok": true}', content_type="application/json")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api", parse_json=False)

        assert result == b'{"ok": true}'

    def test_get_does_not_parse_application_jsonx(self):
        """``application/jsonx`` is NOT treated as JSON -- strict media-type match.

        WHY this test exists: an early draft used
        ``startswith("application/json")`` which would have matched
        ``application/jsonx`` (and any other ``application/json`` prefix)
        as JSON, shipping a silent parse for a type the caller never
        asked for. The current check splits on ``;`` and compares the
        media type exactly; this regression test pins that contract so
        a refactor back to a prefix match would fail loudly.
        """
        from clickwork import http

        resp = _make_response(
            body=b'not really json', content_type="application/jsonx",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        # Raw bytes -- no json.loads attempt.
        assert result == b"not really json"


# ---------------------------------------------------------------------------
# Auth handling
# ---------------------------------------------------------------------------

class TestAuthHeaders:
    """bearer_token / basic_auth / explicit headers precedence."""

    def test_get_bearer_token_sets_authorization_header(self):
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.get("https://example.com/api", bearer_token="abc123")

        req, _ = captured[0]
        # urllib.request.Request lowercases header names via add_header /
        # get_header. We normalize by re-scanning the Request's header dict.
        assert req.get_header("Authorization") == "Bearer abc123"

    def test_get_basic_auth_sets_authorization_header(self):
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.get("https://example.com/api", basic_auth=("user", "pw"))

        req, _ = captured[0]
        expected = "Basic " + base64.b64encode(b"user:pw").decode("ascii")
        assert req.get_header("Authorization") == expected

    def test_explicit_headers_authorization_overrides_bearer_token(self):
        """Caller's headers["Authorization"] must win over bearer_token.

        The dedicated kwargs are shortcuts for the 90% case; passing an
        explicit header must always be the escape hatch that wins.
        """
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.get(
                "https://example.com/api",
                bearer_token="ignored",
                headers={"Authorization": "Custom winning"},
            )

        req, _ = captured[0]
        assert req.get_header("Authorization") == "Custom winning"

    def test_bearer_token_accepts_Secret_instance(self, caplog):
        """Secret is unwrapped at header-build time AND never leaks in logs."""
        from clickwork import http
        from clickwork._types import Secret

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock), caplog.at_level(
            logging.DEBUG, logger="clickwork.http",
        ):
            http.get("https://example.com/api", bearer_token=Secret("supertok"))

        req, _ = captured[0]
        assert req.get_header("Authorization") == "Bearer supertok"
        # Every log record emitted during the call must be free of the
        # unwrapped secret value.
        for record in caplog.records:
            assert "supertok" not in record.getMessage()

    def test_get_basic_auth_accepts_Secret_password(self, caplog):
        """``basic_auth=(user, Secret("pw"))`` must match plain-string result.

        Two pins here: (1) Secret unwrapping produces the same base64 header
        as the plain-string form, proving parity; (2) the secret value never
        appears unredacted in log output, proving the Secret-safety contract.
        """
        from clickwork import http
        from clickwork._types import Secret

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock), caplog.at_level(
            logging.DEBUG, logger="clickwork.http",
        ):
            http.get(
                "https://example.com/api",
                basic_auth=("user", Secret("hunter2")),
            )

        req, _ = captured[0]
        expected = "Basic " + base64.b64encode(b"user:hunter2").decode("ascii")
        assert req.get_header("Authorization") == expected

        # The Secret value must never appear unredacted in log output.
        for record in caplog.records:
            assert "hunter2" not in record.getMessage()


# ---------------------------------------------------------------------------
# Logging / redaction
# ---------------------------------------------------------------------------

class TestLoggingRedaction:
    """Log lines must redact auth material; never leak tokens."""

    def test_http_logs_redact_bearer_token(self, caplog):
        from clickwork import http

        mock = MagicMock(return_value=_make_response(body=b"{}"))
        with patch("clickwork.http._dispatch_request", mock), caplog.at_level(
            logging.DEBUG, logger="clickwork.http",
        ):
            http.get("https://example.com/api", bearer_token="topsecret")

        # At least one record should describe the request with redacted auth.
        auth_lines = [r.getMessage() for r in caplog.records if "auth" in r.getMessage()]
        assert any("<redacted>" in msg for msg in auth_lines)
        for record in caplog.records:
            assert "topsecret" not in record.getMessage()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestHttpError:
    """Non-2xx responses become HttpError; transport errors propagate."""

    def test_get_raises_http_error_on_non_2xx(self):
        """404 with JSON body -> HttpError with parsed body + all attrs."""
        from clickwork import http
        from urllib.error import HTTPError

        # urllib hands non-2xx responses to us via HTTPError, which has
        # a read()-able file-like body and a headers object. We build one
        # directly instead of going through a real urlopen.
        err = HTTPError(
            url="https://example.com/api",
            code=404,
            msg="Not Found",
            hdrs=_HeaderDict({"Content-Type": "application/json"}),  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": "not found"}'),
        )

        with patch("clickwork.http._dispatch_request", side_effect=err):
            with pytest.raises(http.HttpError) as exc_info:
                http.get("https://example.com/api")

        assert exc_info.value.status_code == 404
        assert exc_info.value.response_body == {"error": "not found"}
        assert exc_info.value.url == "https://example.com/api"
        # Headers should reflect what the server returned.
        assert "Content-Type" in exc_info.value.headers or "content-type" in {
            k.lower() for k in exc_info.value.headers
        }

    def test_get_http_error_body_kept_as_bytes_when_not_json(self):
        from clickwork import http
        from urllib.error import HTTPError

        err = HTTPError(
            url="https://example.com/api",
            code=500,
            msg="Server Error",
            hdrs=_HeaderDict({"Content-Type": "text/html"}),  # type: ignore[arg-type]
            fp=BytesIO(b"<html>oops</html>"),
        )

        with patch("clickwork.http._dispatch_request", side_effect=err):
            with pytest.raises(http.HttpError) as exc_info:
                http.get("https://example.com/api")

        assert exc_info.value.status_code == 500
        assert exc_info.value.response_body == b"<html>oops</html>"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

class TestAllowedHosts:
    """Per-call URL allowlist preflight."""

    def test_allowed_hosts_accepts_matching_host(self):
        from clickwork import http

        resp = _make_response(body=b"{}")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get(
                "https://api.cloudflare.com/v4/zones",
                allowed_hosts=["api.cloudflare.com"],
            )

        assert result == {}

    def test_allowed_hosts_rejects_mismatched_host(self):
        """Mismatched host -> ValueError BEFORE urlopen is invoked.

        The mock is wired to raise if it's called. If the allowlist check
        doesn't fire first, this test fails loudly at the mock site, not
        at a pytest.raises miss -- so a regression cannot hide.
        """
        from clickwork import http

        def _fail_if_called(*args, **kwargs):
            pytest.fail(
                "urlopen was invoked despite allowlist mismatch -- "
                "the preflight host check regressed."
            )

        with patch("clickwork.http._dispatch_request", side_effect=_fail_if_called):
            with pytest.raises(ValueError):
                http.get(
                    "https://evil.example/steal",
                    allowed_hosts=["api.cloudflare.com"],
                )

    def test_allowed_hosts_none_skips_check(self):
        """``allowed_hosts=None`` disables the preflight entirely."""
        from clickwork import http

        resp = _make_response(body=b"{}")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            # Any URL should be fine when the allowlist is disabled.
            result = http.get("https://wherever.example/", allowed_hosts=None)

        assert result == {}

    def test_allowed_hosts_empty_list_fails_closed(self):
        """``allowed_hosts=[]`` raises ValueError instead of silently skipping.

        WHY: an empty list at runtime is almost always a config bug (an
        env var that expanded to ``""``, a TOML entry that parsed empty,
        etc.). Earlier drafts treated ``[]`` the same as ``None`` (skip)
        which would turn a misconfiguration into a silent security
        regression -- every host suddenly allowed. Fail closed with a
        clear "pass None to disable" message so the caller can tell
        which interpretation they wanted.
        """
        from clickwork import http

        # If the allowlist DID skip (the pre-fix behaviour), urlopen
        # would be called -- wiring the mock to raise if it is called
        # pins that the check fires BEFORE any network activity.
        def _urlopen_must_not_run(*args, **kwargs):
            raise AssertionError(
                "urlopen was called despite empty allowed_hosts; the "
                "allowlist should have failed closed first."
            )

        with patch("clickwork.http._dispatch_request", side_effect=_urlopen_must_not_run):
            with pytest.raises(ValueError, match="empty list"):
                http.get("https://api.cloudflare.com/", allowed_hosts=[])

    def test_rejects_non_http_scheme_file(self):
        """file:// URLs are rejected before any urlopen call.

        WHY: urllib.request.urlopen happily follows file:// (and ftp://
        etc.) and reads the local filesystem. A clickwork.http caller
        accepting URLs from user input or config would otherwise turn
        this into a local-file-read primitive. The scheme guard makes
        the "HTTP client" contract enforceable.
        """
        from clickwork import http

        def _urlopen_must_not_run(*args, **kwargs):
            raise AssertionError(
                "urlopen was called despite non-http scheme; the scheme "
                "check should have fired first."
            )

        with patch("clickwork.http._dispatch_request", side_effect=_urlopen_must_not_run):
            with pytest.raises(ValueError, match="scheme"):
                http.get("file:///etc/passwd")

    def test_rejects_non_http_scheme_ftp(self):
        """ftp:// URLs are also rejected by the scheme guard.

        Same reason as file://; documented separately so nobody later
        decides "ftp is fine actually" without noticing the test.
        """
        from clickwork import http

        def _urlopen_must_not_run(*args, **kwargs):
            raise AssertionError("urlopen should not have been called")

        with patch("clickwork.http._dispatch_request", side_effect=_urlopen_must_not_run):
            with pytest.raises(ValueError, match="scheme"):
                http.get("ftp://example.com/payload")


# ---------------------------------------------------------------------------
# Empty / malformed body handling
# ---------------------------------------------------------------------------

class TestEmptyBodyHandling:
    """204/empty-body responses with JSON Content-Type don't crash."""

    def test_empty_body_with_json_content_type_does_not_crash(self):
        """A 204 No Content response with Content-Type: application/json
        and an empty body must NOT raise JSONDecodeError.

        WHY: ``json.loads(b"")`` raises JSONDecodeError, and some APIs
        legitimately return empty bodies on successful no-content
        responses (PUT / DELETE / 204). An earlier draft would have
        crashed inside the parser, burying the URL/status context. We
        now hand back the empty bytes so the caller can do whatever
        makes sense (treat as success, skip parsing, etc.).
        """
        from clickwork import http

        resp = _make_response(
            status=204, body=b"", content_type="application/json",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        # Empty body passes through as bytes (not a parsed None).
        assert result == b""

    def test_whitespace_only_body_does_not_crash(self):
        """Whitespace-only JSON body also handled gracefully."""
        from clickwork import http

        resp = _make_response(
            body=b"   \n\t  ", content_type="application/json",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == b"   \n\t  "

    def test_non_utf8_body_with_json_content_type_returns_bytes(self):
        """A non-UTF-8 payload under JSON Content-Type doesn't crash.

        WHY: ``json.loads(bytes)`` on bytes that aren't valid UTF-8
        raises UnicodeDecodeError (subclass of UnicodeError), NOT
        JSONDecodeError. An earlier draft only caught JSONDecodeError
        so a server sending latin-1 garbage under ``application/json``
        would bypass the "return bytes" recovery path and propagate a
        confusing decode error. Catching UnicodeError too closes the
        gap.
        """
        from clickwork import http

        # \\xff is not a valid UTF-8 start byte. json.loads(b"\\xff") raises
        # UnicodeDecodeError on Python 3.11+, which is what we need.
        resp = _make_response(
            body=b"\xff\xfe\xff",
            content_type="application/json",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == b"\xff\xfe\xff"

    def test_content_type_with_whitespace_before_semicolon_parses_json(self):
        """``application/json ; charset=utf-8`` (note the space) auto-parses.

        WHY: RFC 2045 allows optional whitespace around MIME parameter
        delimiters. An earlier ``startswith("application/json;")``
        check would miss this form and silently return bytes. The
        fixed parser splits on ``;`` and strips whitespace.
        """
        from clickwork import http

        resp = _make_response(
            body=b'{"ok": true}',
            content_type="application/json ; charset=utf-8",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == {"ok": True}

    def test_content_type_uppercase_parses_json(self):
        """Case-insensitive match: ``Application/JSON`` also parses."""
        from clickwork import http

        resp = _make_response(body=b'{"ok": true}', content_type="Application/JSON")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == {"ok": True}

    def test_malformed_json_with_json_content_type_returns_bytes(self):
        """A server sending garbage under Content-Type: application/json
        gets the raw bytes back instead of a JSONDecodeError.

        WHY: clickwork.http can't be responsible for a server's bad
        behaviour. Crashing the parser would mask the real request
        context; returning bytes lets the caller log the response and
        decide. Especially matters on the HttpError path -- a 500 with
        an HTML error page under a JSON content-type shouldn't crash
        our error construction.
        """
        from clickwork import http

        resp = _make_response(
            body=b"<html>not json at all</html>",
            content_type="application/json",
        )
        with patch("clickwork.http._dispatch_request", return_value=resp):
            result = http.get("https://example.com/api")

        assert result == b"<html>not json at all</html>"


class TestLogSanitization:
    """Logged URLs are sanitized so embedded credentials never leak."""

    def test_userinfo_stripped_from_log(self, caplog):
        """``https://user:token@host/path`` must log as ``https://host/path``.

        WHY: RFC 3986 lets credentials appear in the URL itself
        (``user:pass@host``). Some APIs accept this form; many
        libraries extract those credentials into the Authorization
        header automatically. Either way, the raw URL string contains
        the secret and must not reach the log.
        """
        from clickwork import http

        resp = _make_response(body=b"{}")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            with caplog.at_level(logging.INFO, logger="clickwork.http"):
                http.get("https://alice:super-secret-token@example.com/api")

        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "super-secret-token" not in all_log_text
        assert "alice" not in all_log_text
        # The sanitized form must still contain enough info to debug.
        assert "example.com" in all_log_text
        assert "/api" in all_log_text

    def test_query_string_stripped_from_log(self, caplog):
        """``?api_key=xxx`` must not end up in the log.

        Some APIs still accept credentials as query params. Even if
        the caller is using bearer_token correctly, a URL like
        ``?api_key=...`` would slip past the header redaction. The
        sanitizer drops the query entirely.
        """
        from clickwork import http

        resp = _make_response(body=b"{}")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            with caplog.at_level(logging.INFO, logger="clickwork.http"):
                http.get("https://example.com/api?api_key=leaked-creds")

        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "leaked-creds" not in all_log_text
        assert "api_key" not in all_log_text
        assert "example.com" in all_log_text
        assert "/api" in all_log_text

    def test_port_preserved_in_log(self, caplog):
        """Non-default ports are operationally useful; they stay visible."""
        from clickwork import http

        resp = _make_response(body=b"{}")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            with caplog.at_level(logging.INFO, logger="clickwork.http"):
                http.get("https://internal.example:8443/health")

        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "internal.example:8443" in all_log_text

    def test_ipv6_netloc_kept_bracketed_in_log(self, caplog):
        """IPv6 hosts round-trip through the sanitizer with correct brackets.

        WHY: urlparse().hostname strips the brackets from IPv6 addresses
        (``[::1]`` -> ``::1``), so naively rebuilding the netloc as
        ``::1:8443`` would produce an ambiguous form (is that
        ``::1:8443`` as an IPv6 address, or IPv6 ``::1`` on port 8443?).
        The sanitizer must re-bracket any hostname containing a colon
        before appending a port.
        """
        from clickwork import http

        resp = _make_response(body=b"{}")
        with patch("clickwork.http._dispatch_request", return_value=resp):
            with caplog.at_level(logging.INFO, logger="clickwork.http"):
                http.get("https://[::1]:8443/metrics")

        all_log_text = "\n".join(rec.getMessage() for rec in caplog.records)
        # Brackets must be present; naive netloc reconstruction would
        # produce ``::1:8443`` without them.
        assert "[::1]:8443" in all_log_text


class TestRedirectsDisabled:
    """3xx responses are not auto-followed; they surface as HttpError.

    WHY this matters: urllib's default opener follows redirects AND
    forwards request headers (including Authorization) along. That's
    a cross-host credential leak waiting to happen, AND it bypasses
    the allowlist because we only validated the initial URL. clickwork.http
    installs a _NoRedirectHandler on its module opener so a 3xx surfaces
    as an HttpError with the status code, leaving the caller free to
    inspect the Location header and decide whether to follow.
    """

    def test_3xx_redirect_raises_http_error_instead_of_following(self):
        """A 302 with Location to another host must raise HttpError, not follow."""
        import urllib.error
        from clickwork import http

        # Build a urllib HTTPError carrying the 302 status + Location header,
        # which is what urllib raises under our _NoRedirectHandler (since
        # redirect_request returns None, the 3xx becomes a terminal error).
        def _raise_302(request, *args, **kwargs):
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=302,
                msg="Found",
                hdrs={  # type: ignore[arg-type]
                    "Location": "https://evil.example/stolen",
                    "Content-Type": "text/html",
                },
                fp=BytesIO(b"<html>redirect</html>"),
            )

        with patch("clickwork.http._dispatch_request", side_effect=_raise_302):
            try:
                http.get(
                    "https://api.cloudflare.com/thing",
                    bearer_token="super-secret-token",
                )
            except http.HttpError as e:
                # The 302 surfaces as HttpError with the real status so
                # the caller can branch on it. No silent follow.
                assert e.status_code == 302
                # The Location header is accessible so callers who WANT
                # to follow can extract it manually after validating the
                # target host themselves.
                assert "Location" in e.headers
                assert "evil.example" in e.headers["Location"]
                # Critically: the token must not appear anywhere in the
                # str(e) or e.url -- the sanitizer already covered URL
                # creds, but this confirms no redirect-path plumbing
                # accidentally echoed the Authorization header value.
                assert "super-secret-token" not in str(e)
                assert "super-secret-token" not in e.url
            else:
                raise AssertionError("Expected HttpError for 3xx redirect")


class TestHttpErrorMessageSanitization:
    """HttpError's .url attribute and str() must not leak URL credentials.

    WHY: a caller who embeds credentials in the URL (``user:pass@host``
    or ``?api_key=xxx``) would otherwise see those secrets leak through
    ``str(HttpError)`` or ``HttpError.url`` when a traceback is logged
    on an unhandled exception. Log-line sanitization alone isn't enough
    because the exception object itself carries the URL.
    """

    def test_http_error_url_attribute_is_sanitized(self):
        """HttpError.url strips userinfo and query string."""
        import urllib.error
        from clickwork import http

        def _raise_404(req, *args, **kwargs):
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=404,
                msg="Not Found",
                hdrs={"Content-Type": "application/json"},  # type: ignore[arg-type]
                fp=BytesIO(b'{"error": "nope"}'),
            )

        with patch("clickwork.http._dispatch_request", side_effect=_raise_404):
            try:
                http.get("https://alice:token@api.example.com/thing?api_key=xxx")
            except http.HttpError as e:
                # Credentials must not survive on .url
                assert "alice" not in e.url
                assert "token" not in e.url
                assert "api_key" not in e.url
                assert "xxx" not in e.url
                # Legitimate parts must be there
                assert "api.example.com" in e.url
                assert "/thing" in e.url
                # Same sanitization flows into the string form
                assert "alice" not in str(e)
                assert "token" not in str(e)
                assert "api_key" not in str(e)
            else:
                raise AssertionError("Expected HttpError to be raised")


class TestTimeout:

    def test_timeout_forwarded_to_urlopen(self):
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.get("https://example.com/api", timeout=7.5)

        _, kwargs = captured[0]
        assert kwargs.get("timeout") == 7.5


# ---------------------------------------------------------------------------
# post / put / delete sanity
# ---------------------------------------------------------------------------

class TestBodyMethods:
    """post/put/delete round-trip JSON bodies correctly."""

    def test_post_sends_dict_body_as_json(self):
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.post("https://example.com/api", body={"key": "val"})

        req, _ = captured[0]
        assert req.get_method() == "POST"
        assert req.data == b'{"key": "val"}'
        # Content-Type must be auto-set to application/json because the
        # body is a JSON-type (dict) and the caller didn't override it.
        assert req.get_header("Content-type") == "application/json"

    def test_post_sends_list_body_as_json(self):
        """``body`` contract is JSONValue (not just dict).

        A list at the top level is perfectly valid JSON. Pinning this
        prevents a future narrowing of the accepted types to dict-only.
        """
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.post("https://example.com/api", body=[1, 2, 3])

        req, _ = captured[0]
        assert req.data == b"[1, 2, 3]"
        assert req.get_header("Content-type") == "application/json"

    def test_put_sends_dict_body_as_json(self):
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.put("https://example.com/api", body={"key": "val"})

        req, _ = captured[0]
        assert req.get_method() == "PUT"
        assert req.data == b'{"key": "val"}'
        assert req.get_header("Content-type") == "application/json"

    def test_delete_sends_dict_body_as_json(self):
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.delete("https://example.com/api", body={"key": "val"})

        req, _ = captured[0]
        assert req.get_method() == "DELETE"
        assert req.data == b'{"key": "val"}'
        assert req.get_header("Content-type") == "application/json"

    def test_post_preserves_caller_supplied_content_type_for_json_body(self):
        """Explicit Content-Type survives JSON body encoding.

        WHY this regression test exists: the contract says auto-setting
        Content-Type only happens when the caller didn't supply one,
        so APIs that require a specific content type (e.g.
        ``application/vnd.api+json``) can opt out of our default.
        Without this test, a refactor that unconditionally overwrites
        to ``application/json`` would silently break those callers.
        """
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.post(
                "https://example.com/api",
                body={"key": "val"},
                headers={"Content-Type": "application/vnd.api+json"},
            )

        req, _ = captured[0]
        assert req.data == b'{"key": "val"}'
        # Caller's header wins; we did NOT overwrite to application/json.
        assert req.get_header("Content-type") == "application/vnd.api+json"

    def test_post_bytes_body_does_not_set_content_type(self):
        """Raw bytes bodies never get an auto-Content-Type.

        WHY: bytes could be anything (gzip, protobuf, form-encoded,
        image data). Auto-setting ``Content-Type: application/json``
        on a bytes body would be actively wrong for every non-JSON
        use case. The contract says "caller owns the framing" for
        bytes. This test pins that.
        """
        from clickwork import http

        mock, captured = _capture_request()
        with patch("clickwork.http._dispatch_request", mock):
            http.post("https://example.com/api", body=b"raw-binary-payload")

        req, _ = captured[0]
        assert req.data == b"raw-binary-payload"
        # No Content-Type auto-set; the request goes out with whatever
        # default (or none) urllib would apply to raw bytes.
        assert req.get_header("Content-type") is None
