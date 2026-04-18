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

**JSON auto-parse.** Responses whose ``Content-Type`` starts with
``application/json`` (so ``application/json; charset=utf-8`` also parses)
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
        response_body: Parsed JSON if the response's Content-Type was
            ``application/json`` (or a charset-suffixed variant), else the
            raw bytes of the body.
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
            response_body: Parsed JSON body (when the response Content-Type
                matched) or raw bytes.
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

def _check_allowed_hosts(url: str, allowed_hosts: list[str] | None) -> None:
    """Enforce the per-call URL allowlist BEFORE any network activity.

    ``None`` disables the check (explicit opt-out for operators who know
    what they're doing). A populated list is treated as "the URL host
    MUST be one of these". Matching is case-insensitive because DNS is
    case-insensitive.

    Args:
        url: The target URL to validate.
        allowed_hosts: None to skip, or a list of acceptable hostnames.

    Raises:
        ValueError: If ``allowed_hosts`` is populated and the URL's host
            doesn't match any entry. ``ValueError`` rather than
            :class:`HttpError` because no HTTP response exists yet --
            the request never left the process.
    """
    if not allowed_hosts:
        # ``None`` or empty list both mean "skip the check". Empty list is
        # treated as a no-op rather than "deny everything" because an empty
        # list arriving at runtime is almost always a config bug, not an
        # explicit policy; failing open with a clear "you probably meant
        # None" story is safer than silently blocking every call.
        return

    hostname = urllib.parse.urlparse(url).hostname
    if hostname is None:
        # urlparse returns None for URLs without a host component (e.g.
        # ``file:///foo``). Treat this as a denied-by-default case rather
        # than letting it sneak through -- the allowlist exists precisely
        # to gate on host identity.
        raise ValueError(
            f"URL {url!r} has no hostname component; cannot be allowlisted."
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

    # JSON-type path. ``json.dumps`` handles dict/list/str/int/float/bool/None
    # uniformly -- the JSONValue alias captures the whole surface.
    encoded = json.dumps(body).encode("utf-8")
    # Only set Content-Type if the caller didn't specify their own; this
    # lets people override with ``application/vnd.api+json`` etc.
    has_ct = any(k.lower() == "content-type" for k in headers)
    if not has_ct:
        headers["Content-Type"] = "application/json"
    return encoded


def _is_json_content_type(content_type: str | None) -> bool:
    """Decide whether a response Content-Type triggers JSON auto-parse.

    We use a prefix match against ``application/json`` so that the common
    ``application/json; charset=utf-8`` variant parses the same way as a
    bare ``application/json``. Case-insensitive because Content-Type is
    an HTTP header and HTTP header values for media-types are defined to
    be case-insensitive on the type/subtype portion.

    Args:
        content_type: The raw Content-Type header value (may be ``None``
            if the server didn't send one).

    Returns:
        True when the response should be ``json.loads``-decoded.
    """
    if not content_type:
        return False
    # Strip whitespace and lowercase for robust matching; a server sending
    # ``Application/JSON ; charset=utf-8`` should still auto-parse.
    normalized = content_type.strip().lower()
    return normalized == "application/json" or normalized.startswith(
        "application/json;"
    )


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

    Args:
        body: The raw response bytes.
        content_type: The response's Content-Type header value.
        parse_json: Whether the caller opted in to JSON auto-parse.

    Returns:
        The parsed JSON value (any :data:`JSONValue`) or the raw bytes.
    """
    if parse_json and _is_json_content_type(content_type):
        return json.loads(body)
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
    if auth_attached:
        logger.info("%s %s [auth: <redacted>]", method, url)
    else:
        logger.info("%s %s", method, url)

    # --- 5. Build the Request and dispatch. ---
    #
    # WHY ``method=`` explicitly: urllib.request.Request infers the method
    # from whether ``data`` is present (GET if not, POST if yes). That's
    # wrong for PUT and DELETE, and for a GET-with-body edge case. Passing
    # ``method`` explicitly removes the ambiguity.
    request = urllib.request.Request(
        url, data=body_bytes, method=method, headers=merged_headers,
    )

    try:
        # ``timeout`` must be forwarded to urlopen, NOT stashed on the Request,
        # because Request has no timeout attribute -- urlopen owns the socket
        # deadline.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            response_content_type = response.headers.get("Content-Type")
    except urllib.error.HTTPError as err:
        # Non-2xx responses arrive here. We read the body, parse per
        # Content-Type, and re-raise as HttpError with the full context
        # so callers can branch on status_code / response_body.
        err_body_raw = err.read() if err.fp is not None else b""
        err_content_type = (
            err.headers.get("Content-Type") if err.headers is not None else None
        )
        err_response_body = _parse_response_body(
            err_body_raw, err_content_type, parse_json,
        )
        err_headers = _headers_to_dict(err.headers)

        # First line of body (or its repr if it's bytes) gives a quick
        # triage message without dumping the full payload into the log.
        if isinstance(err_response_body, bytes):
            snippet = err_response_body[:200].decode("utf-8", errors="replace")
        else:
            snippet = json.dumps(err_response_body)[:200]
        message = f"HTTP {err.code} for {url}: {snippet}"
        raise HttpError(
            status_code=err.code,
            response_body=err_response_body,
            headers=err_headers,
            url=url,
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
        body: The request body. Any :data:`JSONValue` (dict, list, str,
            int, float, bool, None) is ``json.dumps``-encoded and the
            Content-Type header is set to ``application/json`` unless the
            caller already supplied one. Raw ``bytes`` are sent as-is
            (the caller owns the framing). ``None`` sends no body.
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
