# Wave 3 plan — runtime features built on Wave 1

**Date:** 2026-04-17
**Roadmap:** [docs/superpowers/specs/2026-04-17-clickwork-0.2.0-roadmap.md](../superpowers/specs/2026-04-17-clickwork-0.2.0-roadmap.md)
**Scope:** Issues #11 (secret-passing helper) and #13 (HTTP client) — two parallel PRs.
**Depends on:** Wave 1 `stdin_text`/`stdin_bytes` (already merged — `clickwork.process.run` supports it). Wave 2 PRs merged before Wave 3 agent dispatch.

## API shape decisions

### #11 — `ctx.run_with_secrets`

| Decision | Choice |
|----------|--------|
| Surface | Standalone helper `ctx.run_with_secrets(cmd, secrets={...}, stdin_secret="KEY")` — does NOT modify existing `ctx.run`. Makes the "secrets-in-play" contract explicit at every call site. |
| Argv rejection | Only reject **explicit `Secret` instances** appearing in `cmd`. No deep scan for string values that match `Secret.get()`. Simpler, fewer false positives; the explicit-Secret rejection catches the common footgun ("I put my Secret in argv by mistake"). |
| Secret delivery | For each entry in `secrets={name: Secret(value)}`: pass via `env=` to the underlying subprocess. Additionally, if `stdin_secret="NAME"` is provided, route that same secret's `.get()` value through `stdin_text=` (using Wave 1's #10 helper). Keep the dual-channel explicit — this matches the real-world patterns (`wrangler secret put --env-stdin`, `docker login --password-stdin`). |
| Log redaction | After the Secret-in-argv check, argv is guaranteed to be plain strings (any Secret got rejected, not substituted). So log the argv as-is and focus redaction on the **env-var values**: log lines show `NAME=<redacted>` for secret-sourced keys. Env-var *names* stay visible so operators can see what environment the subprocess sees; only the values are hidden. |
| Follow-up (out of scope) | A `--log-insecure-secrets` global flag / env var for opt-in unredacted logging during local debugging. File as a separate issue; not blocking 0.2.0. |

### #13 — `clickwork.http` client

| Decision | Choice |
|----------|--------|
| Module location | New module `clickwork.http` — stateless helpers. Import as `from clickwork import http` then `http.get(...)`. Keeps it usable from non-CLI contexts too. |
| Allowlist enforcement | Per-call keyword-only `allowed_hosts: list[str] \| None` on each method. `None` = disabled (explicit opt-out for ops who know what they're doing). Populated list = URL host must match one of the entries or **`ValueError`** is raised before any network request. *(We raise `ValueError`, not `HttpError`, for pre-flight rejections because there is no HTTP `status_code` at that point -- the request never left the process. `HttpError` is reserved for actual HTTP non-2xx responses.)* |
| Auth | Both dedicated kwargs AND generic `headers=` escape hatch: `bearer_token: str \| Secret \| None` and `basic_auth: tuple[str, str \| Secret] \| None` for the 90% case; `headers: dict[str, str] \| None` for everything else. The password half of `basic_auth` accepts `Secret` for parity with `bearer_token` — passwords are secret-bearing, and forcing callers to unwrap before passing would defeat the point of the `Secret` type. Tests must cover `basic_auth=(user, Secret("pw"))` end-to-end: the header is base64-encoded correctly, the secret value never appears in log output, and `Secret.get()` is called once internally. If the caller sets `headers["Authorization"]` explicitly, that wins over `bearer_token` / `basic_auth` (so "escape hatch" really escapes). |
| JSON parsing | Auto-parse only when the response `Content-Type` is `application/json` (or starts with it, to handle `application/json; charset=utf-8`). Non-JSON responses return raw bytes. `parse_json: bool = True` kwarg lets the caller force raw even for `application/json` (e.g. to avoid double-parsing if they use a custom decoder). Follow-up: investigate auto-parsing other `application/*` types (ndjson, x-yaml, etc.) — out of scope for 0.2.0. |
| Error model | Custom `HttpError(Exception)` raised on non-2xx. Attributes: `status_code: int`, `response_body: JSONValue \| bytes`, `headers: dict[str, str]`, `url: str`. Message includes status + first line of body for quick triage. `JSONValue` is a recursive type alias covering every value `json.loads()` can produce (`dict[str, JSONValue] \| list[JSONValue] \| str \| int \| float \| bool \| None`); `bytes` is the fallback for non-JSON response bodies. Matches the existing `CliProcessError` / `PrerequisiteError` pattern — structured exception attrs so callers can `except HttpError as e: if e.status_code == 404: ...`. |
| Return type | `get/post/put/delete` return `JSONValue \| bytes` — `JSONValue` when Content-Type matches `application/json` and `parse_json=True`, `bytes` otherwise. Narrow to the concrete type at the call site with an `isinstance` or a `cast`. |
| HTTP methods | `get`, `post`, `put`, `delete` — all four ship in this PR. `paginate()` deferred to a follow-up PR (roadmap-level scope cut). |
| Implementation | stdlib `urllib.request` only. No `requests` dependency. |

## Branch + worktree layout

| Issue | Branch | Worktree path |
|-------|--------|---------------|
| #11 | `feat/secret-subprocess-11` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-secrets-11` |
| #13 | `feat/http-client-13` | `/home/qbrd/qbrd-orbit-widener/worktrees/clickwork-http-13` |

*(Already prepped during Wave 2 Copilot waits; will rebase onto post-Wave-2 main before agent dispatch.)*

## Per-issue tasks

### #11 — `ctx.run_with_secrets(cmd, secrets={...}, stdin_secret=...)`

**Signature:** `cmd: list[str | Secret]`. Keep the `list`-not-`Sequence` constraint because `clickwork.process._validate_cmd` already enforces "must be a list" at runtime (see `process.py`'s docstring -- the list-only rule is a deliberate shell-injection guardrail). Using `str | Secret` in the element type lets the Secret-in-argv check accept a `Secret` at the signature level without callers needing `# type: ignore`. After validation, argv is guaranteed to be plain strings.

**Files:** `src/clickwork/process.py` (add `run_with_secrets` alongside existing `run` / `run_with_confirm`), `src/clickwork/cli.py` (bind a forwarding method onto `CliContext`), `tests/unit/test_process.py` and `tests/unit/test_cli.py` (ctx-level forwarding tests).

**TDD:**

1. **Red.** Add tests in `tests/unit/test_process.py`:
   - `test_run_with_secrets_rejects_Secret_in_argv` — `run_with_secrets(["cmd", Secret("foo")], secrets={})` raises `ValueError` (or `TypeError` — match the existing `_validate_cmd` error type style). The error message must name the offending arg's **position**, NOT its `.get()` value (don't leak the secret in the error).
   - `test_run_with_secrets_routes_via_env` — child process echoes `os.environ["TOKEN"]`, caller invokes with `secrets={"TOKEN": Secret("supersecret")}`. Assert child saw `"supersecret"`.
   - `test_run_with_secrets_routes_via_stdin_when_stdin_secret_set` — child process echoes `sys.stdin.read()`, caller invokes with `secrets={"PW": Secret("hunter2")}, stdin_secret="PW"`. Assert child saw `"hunter2"` on stdin (the same secret is ALSO in env, that's fine — some tools prefer one or the other).
   - `test_run_with_secrets_logs_redacted` — patch the process logger, invoke with `secrets={"K": Secret("v")}`, assert the log message contains `<redacted>` and does NOT contain `"v"`. Env-var NAMES (`K`) should still be visible.
   - `test_run_with_secrets_stdin_secret_must_be_in_secrets_dict` — invoke with `stdin_secret="MISSING"` and `secrets={}`. Assert `ValueError` (the name doesn't resolve to any known secret).
   - Ctx-level forwarding test in `tests/unit/test_cli.py`: a command calls `ctx.run_with_secrets(...)` via `CliRunner`; assert it round-trips a secret through stdin identically to `test_run_with_secrets_routes_via_stdin_when_stdin_secret_set`. Pins the cli.py lambda forwarding.

2. **Green.** Implement in `src/clickwork/process.py`:
   ```python
   def run_with_secrets(
       cmd: list[str | Secret],
       *,
       secrets: dict[str, Secret],
       stdin_secret: str | None = None,
       dry_run: bool = False,
       env: dict[str, str] | None = None,
   ) -> subprocess.CompletedProcess | None:
       # 1. Validate: no Secret instance appears in cmd (explicit-rejection only).
       # 2. Validate: stdin_secret is either None or a key in secrets.
       # 3. Build full env: (caller's env or empty) + {k: s.get() for k, s in secrets.items()}
       # 4. If stdin_secret: payload = secrets[stdin_secret].get(); delegate to run(cmd, env=..., stdin_text=payload, dry_run=dry_run)
       #    Else: delegate to run(cmd, env=..., dry_run=dry_run)
       # 5. Log the command BEFORE delegation, with argv untouched (already checked no Secrets in it) and env vars displayed as "NAME=<redacted>" for secret-sourced keys.
   ```
   Bind it on `cli_ctx` the same way the existing helpers do in `src/clickwork/cli.py` — a forwarding lambda that captures `cli_ctx.dry_run` and takes `env=None` as a passthrough kwarg:

   ```python
   cli_ctx.run_with_secrets = lambda cmd, *, secrets, stdin_secret=None, env=None: _run_with_secrets(
       cmd,
       secrets=secrets,
       stdin_secret=stdin_secret,
       dry_run=cli_ctx.dry_run,
       env=env,
   )
   ```

   This mirrors the existing `cli_ctx.run` / `cli_ctx.run_with_confirm` bindings. `CliContext` does not currently hold an `env` dict itself; env is always passed per-call.

3. **Refactor.** Docstring on `run_with_secrets` covering:
   - The explicit-Secret-rejection contract (and why we don't deep-scan).
   - The dual-channel delivery (env + optional stdin).
   - The redaction policy (values redacted, names visible).
   - Example for `wrangler secret put` and `docker login --password-stdin`.
   - Pointer to future `--log-insecure-secrets` follow-up (documented as TBD, not shipped).

**Constraints:**
- **Must close issue #11.** `Fixes #11`.
- Depends on existing `run(..., stdin_text=...)` from Wave 1's #10 — use it directly, don't reinvent stdin piping.
- Log redaction must happen in ONE place (the log line inside `run_with_secrets`). Do NOT modify `run()`'s existing logging — `run()` has no knowledge of Secret semantics.
- Strong typing.
- Zero warnings policy.
- Teaching-style comments.
- Do NOT commit or push.

### #13 — `clickwork.http` client

**Files:** new `src/clickwork/http.py`, new `tests/unit/test_http.py`. Re-export `HttpError` and the four methods via `clickwork.__init__` if there's a public re-export pattern.

**Public API:**
```python
def get(url: str, *,
        allowed_hosts: list[str] | None = None,
        bearer_token: str | Secret | None = None,
        basic_auth: tuple[str, str | Secret] | None = None,
        headers: dict[str, str] | None = None,
        parse_json: bool = True,
        timeout: float = 30.0) -> JSONValue | bytes: ...

def post(url: str, *, body: JSONValue | bytes | None = None, ...) -> JSONValue | bytes: ...
def put(url: str, *, body: JSONValue | bytes | None = None, ...) -> JSONValue | bytes: ...
def delete(url: str, *, ...) -> JSONValue | bytes: ...

# JSONValue is a recursive alias for every top-level type json.loads may
# return. Narrow to the concrete type at the call site with isinstance /
# cast, same as any other union-typed helper.
JSONValue = (
    dict[str, "JSONValue"]
    | list["JSONValue"]
    | str
    | int
    | float
    | bool
    | None
)

class HttpError(Exception):
    def __init__(self, status_code: int, response_body: JSONValue | bytes,
                 headers: dict[str, str], url: str, message: str): ...
    # All five exposed as instance attributes.
```

**TDD:**

Use `pytest-mock` (already in dev deps) or monkeypatch `urllib.request.urlopen` to stub HTTP responses. DO NOT hit the network in unit tests.

1. **Red.** Add tests in `tests/unit/test_http.py`:
   - `test_get_parses_json_when_content_type_is_application_json` — mock response with `Content-Type: application/json` returns `{"ok": true}`; assert `http.get(url)` returns `{"ok": True}` (dict).
   - `test_get_parses_json_with_charset_suffix` — `Content-Type: application/json; charset=utf-8` also parses.
   - `test_get_returns_bytes_for_non_json` — `Content-Type: text/html`; assert bytes returned.
   - `test_get_parse_json_false_forces_raw` — even with `application/json` header, `parse_json=False` returns bytes.
   - `test_get_bearer_token_sets_authorization_header` — mock captures headers; assert `Authorization: Bearer <token>`.
   - `test_get_basic_auth_sets_authorization_header` — `basic_auth=("user", "pw")`, assert `Authorization: Basic <base64("user:pw")>`.
   - `test_get_basic_auth_accepts_Secret_password` — `basic_auth=("user", Secret("hunter2"))`, assert the header is the same base64 you'd get from the plain-string form AND assert the `Secret` value does not appear unredacted anywhere in captured log output. Pins the Secret-safety contract end-to-end so nobody accidentally ships a basic-auth path that can't safely carry a secret password.
   - `test_explicit_headers_authorization_overrides_bearer_token` — both set; caller's headers win.
   - `test_bearer_token_accepts_Secret_instance` — pass `bearer_token=Secret("tok")`; assert header value is the unwrapped string (but also assert that log output redacts it — see next test).
   - `test_http_logs_redact_bearer_token` — patch the http logger; assert log contains `<redacted>` and not the token value.
   - `test_get_raises_http_error_on_non_2xx` — mock 404 response with JSON body `{"error": "not found"}`. Assert `HttpError` with `.status_code == 404`, `.response_body == {"error": "not found"}` (parsed since content-type matched), `.url == url`, `.headers` contains whatever the mock returned.
   - `test_get_http_error_body_kept_as_bytes_when_not_json` — 500 with `text/html` body; `.response_body` is bytes.
   - `test_allowed_hosts_accepts_matching_host` — `allowed_hosts=["api.cloudflare.com"]`, `url="https://api.cloudflare.com/..."`, succeeds.
   - `test_allowed_hosts_rejects_mismatched_host` — same allowlist, `url="https://evil.example/..."`. Assert **`ValueError`** raised BEFORE any network request (mock urlopen to raise if called; the test fails if the mock was invoked). `ValueError` rather than `HttpError` because no HTTP status exists for a request that never left the process.
   - `test_allowed_hosts_none_skips_check` — `allowed_hosts=None`, any URL passes the allowlist stage.
   - `test_timeout_forwarded_to_urlopen` — assert the timeout kwarg reaches urlopen.
   - Small sanity tests for `post`/`put`/`delete` each round-trip a body dict as JSON (via `Content-Type: application/json` request header + body encode).

2. **Green.** Implement in `src/clickwork/http.py`. Key internal structure:
   - `_send(method, url, *, body, headers, bearer_token, basic_auth, allowed_hosts, parse_json, timeout)` — shared core.
   - Allowlist check up front: parse `url` via `urllib.parse.urlparse`, compare `.hostname` to the list (case-insensitive).
   - Header merge: start with user-supplied `headers` (defensive copy); if user didn't set `Authorization` AND caller passed `bearer_token` or `basic_auth`, add it.
   - Body encoding: any `JSONValue` (dict, list, str, int, float, bool, None) → `json.dumps(body).encode("utf-8")` with `Content-Type: application/json` set (if not already set); bytes → send as-is. Tests should cover at least dict and list body values so nobody accidentally narrows the accepted types later.
   - Execute via `urllib.request.Request` + `urllib.request.urlopen`. Catch `urllib.error.HTTPError` to populate `HttpError` (non-2xx responses arrive there). For other errors (timeout, DNS failure), let them propagate — they're not "the server said no", they're framework-level.
   - Response: read body. If Content-Type matches `application/json` and `parse_json=True`, return the parsed JSON value via `json.loads(body)` (may be dict, list, str, number, bool, or None). Else return bytes.
   - Each public method (`get`/`post`/`put`/`delete`) is a thin call to `_send`.

3. **Refactor.** Module docstring explains:
   - No requests dep, stdlib only
   - Allowlist philosophy (opt-in, None to disable)
   - Auth precedence (explicit headers > dedicated kwargs)
   - JSON auto-parse rules
   - Redaction policy (log bearer/basic, never in full)
   - The `HttpError` structure + how to catch it

**Constraints:**
- **Must close issue #13.** `Fixes #13`.
- stdlib only (no `requests` / `httpx` / other third-party).
- `paginate()` deliberately not in this PR — file a follow-up issue with pattern notes from orbit-admin.
- `bearer_token` accepts `str` or `Secret`; internally call `.get()` when it's a Secret. Same for any other secret-bearing values.
- Log line for each request: `"{METHOD} {url} [auth: <redacted>]"` or `"{METHOD} {url}"` if no auth. Never include token / password values.
- Strong typing (use `TypedDict` or dataclasses for `HttpError` attrs if it helps).
- Zero warnings policy.
- Teaching-style comments — match existing module style.
- Do NOT commit or push.

## Per-wave execution checklist

- [ ] Wave 2 PRs merged; Wave 3 worktrees rebased onto post-Wave-2 main
- [ ] Baseline `pytest -q` passes in each worktree
- [ ] Two parallel subagents dispatched (one per issue)
- [ ] Diffs reviewed in main session
- [ ] Commit + push + PRs with `Fixes #N`
- [ ] Copilot review loop per PR (expect detailed review on #13 — it's the largest diff)
- [ ] Merges (independent — no inter-dependencies within Wave 3)
- [ ] Worktrees + local branches cleaned up

## Out of scope for Wave 3 (documented follow-ups)

- **`--log-insecure-secrets`** global flag / env var for opt-in unredacted logging during local debugging (from #11)
- **`clickwork.http.paginate(url, cursor_param="cursor")`** cursor-based pagination helper (from #13)
- **Auto-parse additional `application/*` types** (ndjson, x-yaml, etc.) in `clickwork.http` (from #13)
- **`capture(stdin_text=...)`** — extending the stdout-capturing helper to accept stdin data; flag this if any #11/#13 use case needs it
- **Retry / backoff** on transient network errors in `clickwork.http` — left out deliberately; let callers decide whether to retry
