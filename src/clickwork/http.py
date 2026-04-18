"""Stateless HTTP client built on the Python standard library.

Design tenets
-------------

**stdlib only.** This module uses ``urllib.request`` + ``urllib.parse`` +
``json`` + ``base64`` -- no ``requests``, no ``httpx``, no third-party HTTP
client. Clickwork is intended to be trivially embeddable in any Python 3.11+
project without pulling in an SSL-pinned dependency tree. If a caller needs
``requests``-style conveniences (sessions, connection pooling, complex
retry policies), they can add that dependency themselves at the project
level; we refuse to make the choice for them.

**Allowlist opt-in.** ``allowed_hosts`` defaults to ``None`` (disabled). When
populated, the URL's ``.hostname`` is compared case-insensitively against
each entry and a ``ValueError`` is raised **before** any network activity
happens on a mismatch. We raise ``ValueError`` (not ``HttpError``) for these
pre-flight rejections because no HTTP status exists for a request that
never left the process -- ``HttpError`` is reserved for actual server
non-2xx responses. Operators who want the safety net opt in per-call by
passing a populated list; the rest of the world gets `urllib`'s default
behaviour.

**Auth precedence.** If the caller's ``headers`` dict already contains
``Authorization``, that wins over everything. Otherwise, ``bearer_token``
produces ``Authorization: Bearer <token>`` and ``basic_auth`` produces
``Authorization: Basic <base64(user:pw)>``. Both accept either ``str`` or
:class:`~clickwork._types.Secret`; unwrapping happens **only** at the moment
the header value is built, and the unwrapped value is never logged.

**JSON auto-parse.** Responses whose ``Content-Type`` media type is
``application/json`` (case-insensitive; parameters like
``; charset=utf-8`` are allowed after the media type)
are ``json.loads``-decoded when ``parse_json=True`` (the default). Any other
Content-Type -- or ``parse_json=False`` -- yields raw ``bytes``. The return
type is therefore the union ``JSONValue | bytes``; narrow at the call site
with an ``isinstance`` check or a ``typing.cast``.

**Redaction policy.** Each request emits exactly one log line at INFO level::

    GET https://api.example.com/v1/foo [auth: <redacted>]

If no auth was attached, the ``[auth: ...]`` suffix is omitted entirely.
Token and password values NEVER appear in log output. ``Secret.get()``
is called at most once per request and only inside header construction.

**Error model.** Non-2xx responses arrive via ``urllib.error.HTTPError``;
we catch them and re-raise as :class:`HttpError` with all four attributes
populated: ``status_code``, ``headers``, ``url``, and ``response_body``
(parsed as JSON when the error response's Content-Type matched, else
bytes). Transport-level errors (timeouts, DNS failures, connection
refused) are NOT caught -- they propagate as the underlying
``urllib.error.URLError`` subclass so callers can distinguish "the server
said no" (HttpError) from "we never reached a server" (URLError). Catch
pattern::

    try:
        data = http.get(url, allowed_hosts=[...])
    except HttpError as e:
        if e.status_code == 404:
            ...  # handle 404 specifically
        raise
    except URLError:
        ...  # network/transport issue -- retry, fail over, etc.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from clickwork._types import Secret


# ---------------------------------------------------------------------------
# JSONValue recursive alias
# ---------------------------------------------------------------------------
#
# This union covers every Python value that ``json.loads`` can produce at
# any nesting depth. Using it as the body argument + return type makes the
# JSON contract visible in signatures rather than hiding it behind ``Any``.
# Callers that need a concrete type (e.g., a dict) should narrow at the
# call site with ``isinstance`` or ``typing.cast``.
JSONValue = (
    dict[str, "JSONValue"]
    | list["JSONValue"]
    | str
    | int
    | float
    | bool
    | None
)


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
#
# Namespaced under ``clickwork.http`` so callers / tests can raise or lower
# the level independently of the rest of the framework. Every request
# produces exactly one INFO-level log line here; see the module docstring
# for the exact format and the redaction policy.
logger = logging.getLogger("clickwork.http")


# ---------------------------------------------------------------------------
# HttpError
# ---------------------------------------------------------------------------

class HttpError(Exception):
    """Raised when the server returns a non-2xx response.

    Mirrors the :class:`~clickwork._types.CliProcessError` pattern: raw
    fields are exposed as structured attributes so callers can branch on
    ``status_code`` without parsing a string message.

    Attributes:
        status_code: HTTP status code reported by the server (e.g. 404, 500).
        response_body: Parsed JSON if the original request was issued with
            ``parse_json=True`` (the default) AND the error response's
            Content-Type was ``application/json`` (or a charset-suffixed
            variant); raw bytes otherwise. The ``parse_json`` flag gates
            parsing on BOTH the success and error paths uniformly so a
            caller opting out of auto-parse gets the same raw-bytes
            treatment regardless of status code.
        headers: Response headers as a plain ``dict[str, str]``. Multi-valued
            headers are collapsed (the last value wins) since this is the
            simplest shape callers reason about; if multi-value support
            becomes necessary, we can introduce a richer container later.
        url: The URL that produced this error -- useful for logs when a
            single caller issues multiple requests in a loop.

    Usage::

        try:
            http.get(url, allowed_hosts=[...])
        except HttpError as e:
            if e.status_code == 404:
                return None  # "not found" is expected here
            raise           # anything else is a real error
    """

    def __init__(
        self,
        status_code: int,
        response_body: JSONValue | bytes,
        headers: dict[str, str],
        url: str,
        message: str,
    ) -> None:
        """Build an HttpError with the full context needed for triage.

        Args:
            status_code: HTTP status from the server.
            response_body: Parsed JSON body (when the caller opted into
                ``parse_json=True`` -- the default -- AND the response
                Content-Type matched ``application/json``) or raw bytes.
                Gating by the request's ``parse_json`` flag is uniform
                across success and error paths; see the class-level
                docstring for the full rule.
            headers: Response headers as a plain string-keyed dict.
            url: The URL that produced this error.
            message: A pre-composed human-readable message for ``str(err)``.
        """
        self.status_code: int = status_code
        self.response_body: JSONValue | bytes = response_body
        self.headers: dict[str, str] = headers
        self.url: str = url
        # Pass the composed message to Exception so ``str(err)`` / ``repr(err)``
        # both show the full context without unwrapping the attribute bag.
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that refuses to follow 3xx responses.

    Installed on the module-level opener below so ``_dispatch_request``
    treats a 3xx response as terminal instead of transparently issuing a
    second request to the Location. See ``_send()`` for the full rationale
    (allowlist-bypass + cross-host credential forwarding).

    The override returns ``None`` from ``redirect_request`` which is
    urllib's documented "do not redirect" contract. That causes urllib
    to raise the 3xx as an ``urllib.error.HTTPError``, which our except
    branch then surfaces to callers as ``HttpError`` with the 3xx
    status code (e.g. 302) so the caller can inspect the ``Location``
    header from ``HttpError.headers`` and decide whether to follow.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None signals "do not redirect"; urllib converts the
        # 3xx into an HTTPError that propagates out of opener.open().
        return None


# Module-level opener built once. We install our no-redirect handler
# here instead of mutating the urllib global via ``install_opener``
# (which would change redirect behaviour for any OTHER code in the
# same process that happens to use urllib). Keeping the opener scoped
# to ``clickwork.http`` lets the rest of the process keep its default
# urlopen semantics.
_opener = urllib.request.build_opener(_NoRedirectHandler())


def _dispatch_request(request: urllib.request.Request, *, timeout: float):
    """Send ``request`` through the no-redirect opener; return the response.

    WHY a thin module-level wrapper exists: tests need a single obvious
    seam to mock. Patching ``urllib.request.urlopen`` doesn't help here
    because ``_send()`` uses ``_opener.open`` (which bypasses the
    module-global urlopen). A dedicated wrapper gives tests one place
    to point ``unittest.mock.patch("clickwork.http._dispatch_request")``
    at without fighting the opener chain.

    Args:
        request: The prepared ``urllib.request.Request`` (method, URL,
            headers, body already attached).
        timeout: Socket deadline forwarded to ``opener.open``.

    Returns:
        The opened response (a context-manager file-like).
    """
    return _opener.open(request, timeout=timeout)


def _sanitize_url_for_log(url: str) -> str:
    """Strip credentials and query string from a URL for safe logging.

    Two leak paths this handles:
      1. ``https://user:token@host/path`` -- RFC 3986 userinfo. Credentials
         embedded in the URL itself. Logging the full URL leaks them.
      2. ``https://host/path?api_key=xxx`` -- credentials as query params.
         Some APIs still accept this form.

    The fragment is also dropped for consistency (fragments shouldn't
    carry secrets, but they add noise and aren't part of the request
    wire anyway).

    Robustness invariants:
      - MUST NOT raise on any input. The sanitizer runs on every error
        path in ``_send()`` (scheme guard, allowlist, HttpError.url) --
        if it raises while building an error message, the caller sees
        a confusing inner exception ("Port out of range") that buries
        the real cause. This is why we operate on ``parts.netloc``
        directly instead of touching ``parts.port`` (which validates
        the port lazily and raises ``ValueError`` for out-of-range or
        non-numeric ports like ``:99999`` or ``:abc``).
      - Hostless-but-parseable URLs (``file:///etc/passwd``,
        ``http:///path``) return a sanitized scheme + path instead
        of an opaque ``<unparseable URL>`` placeholder. Operators
        debugging scheme-guard rejections need to see the scheme.
        The placeholder is reserved for genuinely unparseable input
        (where ``urlparse`` itself raises).

    Edge cases:
      - IPv6 addresses keep their brackets natively because we do not
        round-trip through ``parts.hostname`` (which strips them);
        ``parts.netloc`` already carries ``[::1]:8443`` verbatim.
      - Malformed ports are preserved in the sanitized output. This
        is deliberate: an operator looking at the log/error should
        see the actual port that was attempted, not a silently
        corrected form. The alternative ("drop the port") would
        mislead triage.

    Args:
        url: The URL that was passed to the public ``get``/``post``/etc.

    Returns:
        A URL safe to emit in a log line: scheme + host + port + path,
        no userinfo, no query, no fragment. Empty-hostname inputs
        still return scheme + path (so the caller can see what got
        rejected).
    """
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return "<unparseable URL>"

    # Strip userinfo from netloc WITHOUT touching ``parts.hostname`` or
    # ``parts.port``. Both of those accessors run additional validation
    # (port range, IDNA host check) that can raise ``ValueError`` on
    # malformed inputs, which would turn the sanitizer into a source of
    # its own exceptions on the exact error paths that depend on it to
    # build a clean message. ``parts.netloc`` is a raw string attribute
    # -- no validation runs -- so operating on it is safe even when the
    # original URL has an out-of-range port or a non-integer port.
    #
    # ``rpartition("@")`` is the right split because userinfo can
    # itself contain colons (``user:pass``); splitting from the right
    # cleanly separates "everything before the last @" (userinfo) from
    # "host[:port]" (what we want to keep). When there's no userinfo,
    # rpartition returns ``("", "", original)`` so ``host_and_port``
    # is just the original netloc unchanged.
    _, _, host_and_port = parts.netloc.rpartition("@")

    # Drop QUERY and FRAGMENT entirely (those are the credential-leak
    # vectors). Preserve ``params`` -- the rarely-used semicolon-
    # separated path-params component, e.g. ``/path;session=abc``.
    # params aren't credential-carrying the way query strings are, and
    # the path-level semantics still go out on the wire, so the log
    # should show the same shape.
    return urllib.parse.urlunparse(
        (parts.scheme, host_and_port, parts.path, parts.params, "", "")
    )


def _check_allowed_hosts(url: str, allowed_hosts: list[str] | None) -> None:
    """Enforce the per-call URL allowlist BEFORE any network activity.

    ``None`` disables the check (explicit opt-out for operators who know
    what they're doing). A populated list is treated as "the URL host
    MUST be one of these". Matching is case-insensitive because DNS is
    case-insensitive.

    Args:
        url: The target URL to validate.
        allowed_hosts: None to skip, or a non-empty list of acceptable
            hostnames. An empty list is rejected as a config bug -- see
            Raises.

    Raises:
        ValueError: If ``allowed_hosts`` is populated and the URL's host
            doesn't match any entry, OR if ``allowed_hosts`` is an empty
            list, OR if the URL's scheme is not http/https. ``ValueError``
            rather than :class:`HttpError` because no HTTP response
            exists yet -- the request never left the process.
    """
    # Scheme guard. urlopen() will happily follow file:// and ftp://, which
    # is a nasty surprise for callers accepting URLs from user input or
    # config -- "HTTP client" should refuse to be turned into a generic
    # URL fetcher. Reject anything that isn't http or https up front so
    # the rest of the pipeline can assume we're actually making an HTTP
    # request.
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        # Sanitize the URL in the error message so a caller's embedded
        # credentials (userinfo or query params) can't leak through the
        # exception / traceback. Same discipline as the per-request log
        # line and HttpError.url.
        raise ValueError(
            f"URL {_sanitize_url_for_log(url)!r} uses scheme {scheme!r}; "
            "clickwork.http only supports http and https. Accepting "
            "arbitrary URL schemes from user input is a footgun "
            "(file://, ftp://, etc.). If you genuinely need non-HTTP "
            "fetch, use urllib directly."
        )

    if allowed_hosts is None:
        # Explicit opt-out: the caller said "no allowlist". Skip.
        return

    if len(allowed_hosts) == 0:
        # Fail CLOSED on an empty list. An empty allowed_hosts arriving at
        # runtime is almost always a config bug (e.g. an environment
        # variable that expanded to ""), and silently allowing every host
        # in that case would turn a misconfiguration into a security
        # regression. Callers who genuinely want to disable the check
        # must pass ``None`` explicitly.
        raise ValueError(
            "allowed_hosts is an empty list; pass None to explicitly "
            "disable the allowlist, or populate the list with the hosts "
            "this call is permitted to reach. An empty list is rejected "
            "because it's almost always a config bug and failing open "
            "would be a silent security regression."
        )

    hostname = urllib.parse.urlparse(url).hostname
    if hostname is None:
        # urlparse returns None for URLs without a host component (e.g.
        # ``file:///foo``). Treat this as a denied-by-default case rather
        # than letting it sneak through -- the allowlist exists precisely
        # to gate on host identity. Sanitize the URL in the error for
        # the same reason as the scheme error above.
        raise ValueError(
            f"URL {_sanitize_url_for_log(url)!r} has no hostname "
            "component; cannot be allowlisted."
        )

    hostname_lower = hostname.lower()
    allowed_lower = {h.lower() for h in allowed_hosts}
    if hostname_lower not in allowed_lower:
        raise ValueError(
            f"Host {hostname!r} is not in allowed_hosts={allowed_hosts!r}."
        )


def _unwrap_secret(value: str | Secret) -> str:
    """Retrieve the plain string from a ``str`` or :class:`Secret` input.

    Centralising this in one tiny helper makes the policy auditable: every
    ``Secret.get()`` call in this module lives exactly here. Grep for
    ``.get()`` in ``clickwork/http.py`` and you'll find only this call.

    Args:
        value: Either a plain string or a :class:`Secret` wrapping one.

    Returns:
        The unwrapped plain string.
    """
    if isinstance(value, Secret):
        return value.get()
    return value


def _build_headers(
    headers: dict[str, str] | None,
    bearer_token: str | Secret | None,
    basic_auth: tuple[str, str | Secret] | None,
) -> tuple[dict[str, str], bool]:
    """Assemble the final request headers with auth precedence applied.

    Precedence rules (highest first):
      1. Caller's ``headers["Authorization"]`` -- explicit wins, full stop.
         This is the documented escape hatch for anything the dedicated
         kwargs can't express.
      2. ``bearer_token`` -- convenience for the 90% case.
      3. ``basic_auth`` -- convenience for basic-auth APIs; password half
         accepts a :class:`Secret` for parity with ``bearer_token``.

    Args:
        headers: The caller's explicit headers (may be ``None``).
        bearer_token: Optional bearer token (str or Secret).
        basic_auth: Optional ``(user, password)`` tuple where password may
            be a :class:`Secret`.

    Returns:
        A tuple of ``(merged_headers, auth_was_attached)``. The boolean is
        used by the caller to decide whether the log line should include
        the ``[auth: <redacted>]`` suffix.
    """
    # Defensive copy so callers can reuse the same dict across calls
    # without observing mutation as the method appends Content-Type, etc.
    merged: dict[str, str] = dict(headers) if headers else {}

    # Case-insensitive check: has the caller already set Authorization?
    # We scan keys ourselves because ``dict`` itself is case-sensitive,
    # but HTTP header names are not.
    caller_set_auth = any(k.lower() == "authorization" for k in merged)

    auth_attached = caller_set_auth
    if not caller_set_auth:
        if bearer_token is not None:
            # Unwrap at exactly one site; the plain value lives only in
            # this header string from here on.
            merged["Authorization"] = f"Bearer {_unwrap_secret(bearer_token)}"
            auth_attached = True
        elif basic_auth is not None:
            user, pw = basic_auth
            credentials = f"{user}:{_unwrap_secret(pw)}".encode("utf-8")
            encoded = base64.b64encode(credentials).decode("ascii")
            merged["Authorization"] = f"Basic {encoded}"
            auth_attached = True

    return merged, auth_attached


def _encode_body(
    body: JSONValue | bytes | None,
    headers: dict[str, str],
) -> bytes | None:
    """Convert ``body`` to bytes and set ``Content-Type`` if appropriate.

    Rules:
      - ``None`` -> ``None`` (no request body at all; GET/DELETE default).
      - ``bytes`` -> passed through unchanged; do NOT override any
        Content-Type the caller explicitly set (raw bytes could be any
        binary payload -- gzip, protobuf, image data -- the caller owns
        the framing).
      - Anything else (dict / list / str / int / float / bool) is
        serialised with ``json.dumps`` and encoded as UTF-8, and
        ``Content-Type: application/json`` is added unless the caller
        already specified a Content-Type (letting them pick, for example,
        ``application/vnd.api+json`` if their API is fussy about the
        exact media type).

    Args:
        body: The user-supplied body, which may be any ``JSONValue``,
            raw bytes, or ``None``.
        headers: The merged header dict; mutated in place to add
            ``Content-Type`` when we encode a JSON-type body and the
            caller didn't set their own.

    Returns:
        The encoded request payload as ``bytes``, or ``None`` if no body
        should be sent.
    """
    if body is None:
        # No body = no Content-Type management needed; let the request
        # go out with whatever (if anything) the caller set. Callers
        # might still want headers like ``Accept`` on a GET.
        return None

    if isinstance(body, bytes):
        # Raw bytes: caller owns the framing. We don't second-guess them
        # by setting Content-Type; if they wanted one, they included it
        # in ``headers``.
        return body

    # JSON-type path. ``json.dumps`` handles dict/list/str/int/float/bool
    # uniformly. (A literal JSON ``null`` is NOT reachable here: Python's
    # ``None`` was short-circuited above as "no request body at all" per
    # the module contract. If a caller genuinely wants to send the bytes
    # ``null`` as a JSON-encoded null body, they can pass ``b"null"``
    # with their own Content-Type.)
    encoded = json.dumps(body).encode("utf-8")
    # Only set Content-Type if the caller didn't specify their own; this
    # lets people override with ``application/vnd.api+json`` etc.
    has_ct = any(k.lower() == "content-type" for k in headers)
    if not has_ct:
        headers["Content-Type"] = "application/json"
    return encoded


def _is_json_content_type(content_type: str | None) -> bool:
    """Decide whether a response Content-Type triggers JSON auto-parse.

    We split off any MIME parameters (for example ``; charset=utf-8``)
    and require an EXACT media-type match against ``application/json``
    (case-insensitive). This means:

      - ``application/json``                -> True
      - ``application/json; charset=utf-8`` -> True (parameters are dropped)
      - ``application/json ; charset=utf-8``-> True (whitespace tolerated)
      - ``Application/JSON``                -> True (case-insensitive)
      - ``application/jsonx``               -> **False** (NOT a prefix match)
      - ``application/vnd.api+json``        -> False (different media type)

    This is deliberately stricter than a ``startswith`` check: the
    earlier draft used one, which would have matched ``application/jsonx``
    and silently parsed anything whose type string happened to begin
    with "application/json". The split-and-compare approach matches
    the RFC 2045 media-type grammar and avoids that footgun.

    Args:
        content_type: The raw Content-Type header value (may be ``None``
            if the server didn't send one).

    Returns:
        True when the response should be ``json.loads``-decoded.
    """
    if not content_type:
        return False
    # Parse by splitting on the MIME parameter delimiter (``;``) and
    # comparing only the type/subtype portion. This handles:
    #   "application/json"                   -> ["application/json"]
    #   "application/json; charset=utf-8"    -> ["application/json", " charset=utf-8"]
    #   "application/json ; charset=utf-8"   -> ["application/json ", " charset=utf-8"]
    #   "Application/JSON"                   -> ["Application/JSON"]
    # The earlier "startswith" approach missed the third case because
    # it left a trailing space on the type portion. Splitting + strip
    # + lowercase is the RFC-aligned way to compare MIME types: the
    # type/subtype is insensitive to case and to surrounding whitespace
    # around the parameter delimiter.
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def _headers_to_dict(raw_headers) -> dict[str, str]:
    """Flatten an HTTPMessage-like headers object into a plain dict.

    ``http.client.HTTPResponse.headers`` is an ``HTTPMessage`` which supports
    multi-valued headers (e.g. multiple ``Set-Cookie`` entries). For the
    :class:`HttpError` payload we flatten to a plain ``dict[str, str]``
    because the common case is key-lookup, and if a caller needs
    multi-value semantics they can add it later via a richer container.

    Args:
        raw_headers: An HTTPMessage-like object exposing ``.items()``.

    Returns:
        A plain ``dict[str, str]`` of headers (last wins on duplicates).
    """
    if raw_headers is None:
        return {}
    if hasattr(raw_headers, "items"):
        # Last value wins on duplicates -- acceptable for the common case.
        return {k: v for k, v in raw_headers.items()}
    # Fall back to treating the object itself as a dict-of-some-kind.
    return dict(raw_headers)


def _parse_response_body(body: bytes, content_type: str | None, parse_json: bool) -> JSONValue | bytes:
    """Decode a raw response body according to the parse_json + Content-Type rules.

    See the module docstring for the full policy. Used in two places: the
    normal success path and the :class:`HttpError` construction path
    (which decides whether the error body is a parsed JSON value or bytes).

    Edge cases handled:
      - Empty or whitespace-only body: returned as-is (bytes). A 204 No
        Content with ``Content-Type: application/json`` and an empty
        body is a real thing on the wire, and ``json.loads(b"")`` raises
        ``JSONDecodeError`` -- we treat empty body as "nothing to parse"
        and hand back the original bytes so the caller's error handling
        doesn't lose URL/status context to a crash inside this helper.
      - Malformed JSON with a JSON Content-Type: returned as raw bytes
        (same rationale -- don't turn a server misbehaviour into a
        confusing internal crash; give the caller the bytes so they
        can decide how to react, including on the ``HttpError`` path
        where ``response_body`` becoming parseable-dict-or-bytes is
        documented contract).

    Args:
        body: The raw response bytes.
        content_type: The response's Content-Type header value.
        parse_json: Whether the caller opted in to JSON auto-parse.

    Returns:
        The parsed JSON value (any :data:`JSONValue`) or the raw bytes.
    """
    if parse_json and _is_json_content_type(content_type):
        # Skip json.loads on empty/whitespace bodies: json.loads(b"")
        # raises JSONDecodeError which would turn a legitimate 204 /
        # empty response into an internal crash that masks the real
        # request context.
        if not body.strip():
            return body
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeError):
            # Server sent JSON Content-Type with a payload that either
            # isn't valid JSON (JSONDecodeError) or isn't valid UTF-8
            # (UnicodeError / UnicodeDecodeError). Either way, hand back
            # the raw bytes so the caller can log / inspect / error out
            # with full context. Particularly important on the HttpError
            # path -- a misbehaving server replying to a 500 with HTML
            # under a JSON Content-Type shouldn't obscure the status code
            # by crashing inside our error construction.
            return body
    return body


# ---------------------------------------------------------------------------
# Shared core
# ---------------------------------------------------------------------------

def _send(
    method: str,
    url: str,
    *,
    body: JSONValue | bytes | None = None,
    headers: dict[str, str] | None = None,
    bearer_token: str | Secret | None = None,
    basic_auth: tuple[str, str | Secret] | None = None,
    allowed_hosts: list[str] | None = None,
    parse_json: bool = True,
    timeout: float = 30.0,
) -> JSONValue | bytes:
    """Shared implementation for every HTTP verb in this module.

    Flow:

    1. Allowlist preflight (may raise :class:`ValueError`).
    2. Build merged headers with auth applied (explicit headers win).
    3. Encode the request body and set ``Content-Type`` when appropriate.
    4. Emit exactly one INFO-level log line (with ``[auth: <redacted>]``
       suffix when auth was attached, otherwise just ``METHOD url``).
    5. Dispatch via ``urllib.request.urlopen``.
    6. On 2xx, decode the response body per the parse_json + Content-Type
       rules and return it.
    7. On non-2xx, catch ``urllib.error.HTTPError`` and re-raise as
       :class:`HttpError` with all attributes populated.

    Transport errors (timeout, DNS, connection refused) are NOT caught
    here -- let them propagate as the underlying ``URLError`` subclass.
    """
    # --- 1. Allowlist preflight (happens BEFORE any network activity). ---
    _check_allowed_hosts(url, allowed_hosts)

    # --- 2. Headers + auth. ---
    merged_headers, auth_attached = _build_headers(headers, bearer_token, basic_auth)

    # --- 3. Body encoding (may mutate merged_headers to add Content-Type). ---
    body_bytes = _encode_body(body, merged_headers)

    # --- 4. Log line (exactly one per request). ---
    #
    # Sanitize the URL before logging -- some APIs still accept
    # credentials either as RFC 3986 userinfo (https://user:pass@host)
    # or as query parameters (?api_key=xxx). The auth-header redaction
    # above wouldn't catch those forms because they live on the URL
    # itself. ``_sanitize_url_for_log`` strips userinfo + query + fragment
    # so the log keeps scheme + host + port + path (operationally useful)
    # without ever surfacing credential material.
    safe_url = _sanitize_url_for_log(url)
    if auth_attached:
        logger.info("%s %s [auth: <redacted>]", method, safe_url)
    else:
        logger.info("%s %s", method, safe_url)

    # --- 5. Build the Request and dispatch. ---
    #
    # WHY ``method=`` explicitly: urllib.request.Request infers the method
    # from whether ``data`` is present (GET if not, POST if yes). That's
    # wrong for PUT and DELETE, and for a GET-with-body edge case. Passing
    # ``method`` explicitly removes the ambiguity.
    request = urllib.request.Request(
        url, data=body_bytes, method=method, headers=merged_headers,
    )

    # WHY we install an opener that REFUSES HTTP redirects (3xx). urllib's
    # default urlopen follows redirects automatically AND forwards the
    # Request's headers -- including any ``Authorization`` we just built
    # from ``bearer_token`` / ``basic_auth`` -- along to the redirect
    # target. That can:
    #   (a) BYPASS the allowlist. We only validated the URL the caller
    #       passed; a 301 pointing at ``evil.example`` would send the
    #       request anyway.
    #   (b) LEAK credentials cross-host. Even if redirect semantics
    #       were fine, auth-header forwarding across hosts is a
    #       well-known credential-leak class.
    # Disabling redirects closes both. A caller who genuinely needs
    # redirect-following can see the 3xx surface as HttpError,
    # inspect the Location header themselves, and issue a new call
    # with the right allowlist. That's the tradeoff we accept in
    # exchange for the security invariant.
    #
    # WHY a custom handler instead of ``urllib.request.Request.headers`` /
    # ``OpenerDirector``: HTTPRedirectHandler exposes ``redirect_request``
    # which we override to return None, making urllib treat 3xx as a
    # terminal response (it becomes an HTTPError with the actual 3xx
    # status, surfacing to our except branch below).
    try:
        # ``timeout`` must be forwarded to opener, NOT stashed on the
        # Request, because Request has no timeout attribute -- the
        # opener's open() method owns the socket deadline. We route
        # through ``_dispatch_request`` (defined at module scope above)
        # so tests have a single, obvious patch target.
        with _dispatch_request(request, timeout=timeout) as response:
            response_body = response.read()
            response_content_type = response.headers.get("Content-Type")
    except urllib.error.HTTPError as err:
        # Non-2xx responses arrive here. We read the body, parse per
        # Content-Type, and re-raise as HttpError with the full context
        # so callers can branch on status_code / response_body.
        #
        # WHY a try/finally around the read: ``urllib.error.HTTPError``
        # carries a still-open response fp (the success path uses a
        # context manager that closes it; the error path has to do it
        # explicitly). Without the close, repeated non-2xx responses
        # would leak file descriptors / socket handles. ``err.close()``
        # is the documented way to release both the underlying socket
        # and any buffering on err.fp.
        try:
            err_body_raw = err.read() if err.fp is not None else b""
            err_content_type = (
                err.headers.get("Content-Type") if err.headers is not None else None
            )
            err_response_body = _parse_response_body(
                err_body_raw, err_content_type, parse_json,
            )
            err_headers = _headers_to_dict(err.headers)
        finally:
            err.close()

        # Include a short preview in the message without dumping the full
        # payload: bytes are truncated to 200 bytes and decoded as UTF-8
        # with replacement (so binary error pages don't crash the error
        # construction); structured (already-parsed-to-JSON) bodies are
        # json-serialised and then truncated the same way. Keeps triage
        # messages readable without letting a giant HTML error page
        # flood the logs.
        if isinstance(err_response_body, bytes):
            snippet = err_response_body[:200].decode("utf-8", errors="replace")
        else:
            snippet = json.dumps(err_response_body)[:200]
        # Sanitize the URL for both the human-readable message AND the
        # .url attribute on HttpError. A caller who embeds credentials
        # in the URL (userinfo or query params) would otherwise see
        # those secrets leak via ``str(HttpError)`` or
        # ``HttpError.url`` -- e.g. when a traceback is logged by an
        # operator. The sanitized form drops userinfo/query/fragment
        # and keeps scheme + host + port + path, matching what the
        # per-request log line shows.
        #
        # WHY err.url over the original ``url`` arg: ``urllib.error.HTTPError``
        # populates ``err.url`` with the URL that actually produced the
        # error. With redirects disabled this equals ``url`` in the
        # common case, but urllib can also normalize the URL (e.g.
        # percent-encoding fixups) before sending, and err.url reflects
        # what went on the wire. Using err.url keeps the reported URL
        # honest. Fall back to ``url`` if for any reason err.url is
        # missing (defensive; shouldn't happen in practice).
        url_for_error = getattr(err, "url", None) or url
        safe_url_for_error = _sanitize_url_for_log(url_for_error)
        message = f"HTTP {err.code} for {safe_url_for_error}: {snippet}"
        raise HttpError(
            status_code=err.code,
            response_body=err_response_body,
            headers=err_headers,
            url=safe_url_for_error,
            message=message,
        ) from err

    # --- 6. Success-path response decoding. ---
    return _parse_response_body(response_body, response_content_type, parse_json)


# ---------------------------------------------------------------------------
# Public methods (thin wrappers around _send)
# ---------------------------------------------------------------------------

def get(
    url: str,
    *,
    allowed_hosts: list[str] | None = None,
    bearer_token: str | Secret | None = None,
    basic_auth: tuple[str, str | Secret] | None = None,
    headers: dict[str, str] | None = None,
    parse_json: bool = True,
    timeout: float = 30.0,
) -> JSONValue | bytes:
    """Issue an HTTP GET and return the parsed JSON or raw bytes.

    Args:
        url: Absolute URL (``https://...``).
        allowed_hosts: Optional per-call URL allowlist. ``None`` (default)
            disables the check. Populated list -> the URL host must match
            one entry (case-insensitive) or :class:`ValueError` is raised
            before any network activity.
        bearer_token: Optional bearer token (``str`` or :class:`Secret`);
            produces ``Authorization: Bearer <token>``.
        basic_auth: Optional ``(user, password)`` tuple; password may be a
            :class:`Secret`. Produces ``Authorization: Basic <base64(u:p)>``.
        headers: Optional extra request headers. An explicit
            ``Authorization`` entry wins over ``bearer_token`` /
            ``basic_auth``.
        parse_json: When True (default), auto-parse responses whose
            Content-Type matches ``application/json``. Set to False to
            force raw bytes.
        timeout: Socket-level timeout in seconds.

    Returns:
        The parsed JSON value (``JSONValue``) when Content-Type matched
        and ``parse_json=True``; raw ``bytes`` otherwise.

    Raises:
        ValueError: On allowlist mismatch (before any network traffic).
        HttpError: On any non-2xx HTTP response.
        urllib.error.URLError: On transport failures (timeout, DNS, etc.).
    """
    return _send(
        "GET", url,
        allowed_hosts=allowed_hosts,
        bearer_token=bearer_token,
        basic_auth=basic_auth,
        headers=headers,
        parse_json=parse_json,
        timeout=timeout,
    )


def post(
    url: str,
    *,
    body: JSONValue | bytes | None = None,
    allowed_hosts: list[str] | None = None,
    bearer_token: str | Secret | None = None,
    basic_auth: tuple[str, str | Secret] | None = None,
    headers: dict[str, str] | None = None,
    parse_json: bool = True,
    timeout: float = 30.0,
) -> JSONValue | bytes:
    """Issue an HTTP POST. See :func:`get` for shared kwargs.

    Args:
        body: The request body.

            * ``None`` (the default) sends NO request body at all --
              same wire behaviour as ``urllib.request.Request(data=None)``.
              This is NOT the same as sending a JSON-encoded ``null``
              payload; if you genuinely want that, pass ``b"null"`` and
              set Content-Type yourself.
            * Any other :data:`JSONValue` (dict, list, str, int, float,
              bool) is ``json.dumps``-encoded and the Content-Type
              header is set to ``application/json`` unless the caller
              already supplied one.
            * Raw ``bytes`` are sent as-is (the caller owns the framing
              and must supply their own Content-Type if appropriate).
    """
    return _send(
        "POST", url,
        body=body,
        allowed_hosts=allowed_hosts,
        bearer_token=bearer_token,
        basic_auth=basic_auth,
        headers=headers,
        parse_json=parse_json,
        timeout=timeout,
    )


def put(
    url: str,
    *,
    body: JSONValue | bytes | None = None,
    allowed_hosts: list[str] | None = None,
    bearer_token: str | Secret | None = None,
    basic_auth: tuple[str, str | Secret] | None = None,
    headers: dict[str, str] | None = None,
    parse_json: bool = True,
    timeout: float = 30.0,
) -> JSONValue | bytes:
    """Issue an HTTP PUT. See :func:`get` / :func:`post` for shared kwargs."""
    return _send(
        "PUT", url,
        body=body,
        allowed_hosts=allowed_hosts,
        bearer_token=bearer_token,
        basic_auth=basic_auth,
        headers=headers,
        parse_json=parse_json,
        timeout=timeout,
    )


def delete(
    url: str,
    *,
    body: JSONValue | bytes | None = None,
    allowed_hosts: list[str] | None = None,
    bearer_token: str | Secret | None = None,
    basic_auth: tuple[str, str | Secret] | None = None,
    headers: dict[str, str] | None = None,
    parse_json: bool = True,
    timeout: float = 30.0,
) -> JSONValue | bytes:
    """Issue an HTTP DELETE. See :func:`get` / :func:`post` for shared kwargs.

    DELETE with a body is unusual but legal (some APIs use it for bulk
    delete payloads); we accept it symmetrically with PUT/POST rather
    than forcing a different signature.
    """
    return _send(
        "DELETE", url,
        body=body,
        allowed_hosts=allowed_hosts,
        bearer_token=bearer_token,
        basic_auth=basic_auth,
        headers=headers,
        parse_json=parse_json,
        timeout=timeout,
    )
