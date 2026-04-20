# Security

This document describes what clickwork actively protects against, what
it leaves to the downstream CLI author, and the assumptions behind both.
It is aimed at CLI authors building on clickwork who need to make
informed decisions about what their own code still has to handle. It is
not a marketing document.

For the broader design rationale see [ARCHITECTURE.md](../explanation/architecture.md);
for the concrete footgun list see
[LLM_REFERENCE.md](llm-reference.md#common-footguns) (entries 4, 5, 7,
8, and 10 all have security implications).

## Reporting vulnerabilities

Please do not file public issues for suspected vulnerabilities. Open a
[private security advisory](https://github.com/qubitrenegade/clickwork/security/advisories/new)
so we can coordinate a fix before public disclosure. Low-risk issues
that you believe are already public knowledge can be filed as regular
issues.

## What clickwork defends against

The guardrails below are enforced by the framework. You get them for
free if you use the relevant helper.

### Secrets leaking in argv

On many POSIX systems `argv` is visible to other local users via `ps`
and `/proc/*/cmdline`. Default Linux (`hidepid=0`) and macOS expose
every command-line argument of every running process; hardened
configurations (`hidepid=2`, SELinux policy, jails) restrict this, but
clickwork treats the worst case as the design target rather than
relying on operator-side hardening. `ctx.run_with_secrets` refuses to
put a `Secret` in argv and routes the value through env vars and
(optionally) stdin instead:

```python
ctx.run_with_secrets(["wrangler", "secret", "put", "API_TOKEN"], secrets={"CLOUDFLARE_API_TOKEN": Secret(token)}, stdin_secret="CLOUDFLARE_API_TOKEN")
```

Passing a `Secret` instance directly in the argv list raises
`ValueError` *before* the subprocess starts. See
`clickwork.process.run_with_secrets` for the full contract.

### Secrets leaking in logs

The `Secret` wrapper renders as `***` (or `Secret(***)` for `repr`) in
`str()`, f-strings, `format()`, and the dataclass `__repr__` path --
every "print this thing" surface a clickwork user is likely to hit. It
uses `__slots__`, so there's no `__dict__` to leak the value; `vars(s)`
on a `Secret` raises `TypeError` rather than exposing it. Pickling a
`Secret` is blocked outright (`__reduce__` raises `TypeError`), so
serializing secrets to disk or the wire is a hard error rather than a
"helpfully" redacted emission. The single documented escape hatch is
`.get()`, which is trivial to grep for in review.

`run_with_secrets` emits exactly one log line per subprocess with every
env var value redacted. Secret-sourced entries render as
`NAME=<redacted>`; caller-supplied `env=` entries render as
`NAME=<set>`. Names stay visible so operators can debug missing keys.

`clickwork.http` emits exactly one INFO log line per request in the
form `GET https://api.example.com/v1/foo [auth: <redacted>]`. The URL
is sanitized (`_sanitize_url_for_log`) so any userinfo is stripped
before it reaches the log. Token and password values never appear in
output.

### Secrets leaking in config

Repo config (`.<tool>.toml`) is checked into git and visible to anyone
with repo access. Keys tagged `secret: True` in the schema are
*rejected* if they appear in repo config; they must live in user
config or environment variables:

```python
CONFIG_SCHEMA = {
    "api_token": {"secret": True, "env": "MY_TOOL_API_TOKEN"},
}
```

At runtime, `secret: True` values are wrapped in `Secret` before being
handed to command code, so the log-redaction path above applies
automatically.

### URL allowlist and no-redirect HTTP

`clickwork.http.get/post/put/delete` accept an `allowed_hosts=` list.
When populated, the URL's hostname is compared case-insensitively
against each entry and a `ValueError` is raised *before* any network
activity happens on mismatch. Operators opt in per call.

The module installs a no-redirect opener: 3xx responses are not
followed. This is deliberate. urllib's default redirect handler
forwards `Authorization` headers across hosts, so a compromised or
hostile server at the original host can exfiltrate bearer tokens by
redirecting to an attacker-controlled host. Callers who need redirects
must opt in explicitly (and should set `allowed_hosts` to cover the
redirect targets).

### Scheme guard

`clickwork.http` rejects any URL whose scheme is not `http` or
`https`. `file://`, `ftp://`, `data://`, and anything else raise
`ValueError` before the request is sent. urlopen would otherwise
happily read `/etc/passwd` via `file:///etc/passwd`, which is a common
footgun when URLs come from user input.

### Discovery shadowing boundary

In `auto` discovery mode, local `.py` commands in `commands_dir`
shadow installed plugin commands on name conflicts, with an INFO log.
This is by design: the project author's commit is authoritative over
whatever happened to be `pip install`ed in the environment. The log
makes the shadow visible so a malicious package cannot silently
override a local command.

## What clickwork does NOT defend against

Clickwork is a framework, not a sandbox. The items below are explicitly
out of scope and remain the CLI author's responsibility.

### Malicious plugins

If a user `pip install`s a package that registers a malicious
`clickwork.commands` entry point, clickwork will load and run it with
the full privileges of the CLI process. There is no sandboxing, no
code signing, no per-plugin permission model. Mitigation is upstream:
pin your dependencies, use a lockfile (`uv.lock`), and review what you
install.

### Secrets in shell history

If a user types a token into a prompt or passes it as a shell
argument, it will end up in `~/.bash_history` or equivalent. Clickwork
cannot see or redact shell history. For interactive token entry,
prefer reading from stdin or env.

### Secrets in third-party libraries

Anything command code imports (requests, boto3, a custom SDK) has its
own logging and error paths. Clickwork's redaction does not extend
into those libraries. If you pass a `Secret.get()` value into a
third-party client, its logs are on you.

### Arbitrary code execution via config

User config is parsed as TOML, not executed. TOML cannot express code,
so a malicious user config cannot directly run commands. However, a
schema that accepts arbitrary strings and later hands them to
`ctx.run(["bash", "-c", value])` *is* a code-execution vector. Use
argv lists, never `bash -c`. See footgun #10.

### Cross-host credential theft on opt-out

The allowlist and the no-redirect policy are **independent controls**.
`allowed_hosts=None` (the default) disables only the allowlist --
the no-redirect opener still refuses to follow 3xx responses, so a
hostile server can't redirect an authenticated request to an
attacker-controlled host just because the caller didn't pin an
allowlist. Opting out of the no-redirect policy requires stepping
outside `clickwork.http` entirely (use `urllib.request.urlopen`
directly or install a custom opener). A caller who does both --
skips the allowlist AND bypasses the no-redirect opener -- has
taken responsibility for cross-host credential forwarding
themselves; clickwork has no way to protect that configuration.

## Threat model assumptions

- **Local filesystem is the trust boundary.** Anything on disk that
  the CLI user owns is trusted. Files owned by other users or with
  group/other permissions set are *not* trusted; the owner-only
  permission check enforces this for secrets-bearing files.
- **HTTP output past the allowlist is trusted.** Once a host is on the
  allowlist and a 2xx response comes back, clickwork trusts the body
  enough to parse it as JSON if asked. If you allowlist a host, you
  are asserting that its 2xx responses are safe to process.
- **Subprocess stdin/stdout are in-band; env is the secret channel.**
  Argv is world-readable; env is per-process and readable only by the
  process's owner (and root). Stdin is an ephemeral pipe. Secrets go
  through env and stdin, never argv.
- **The CLI author controls the command set.** Commands are plugin
  code written by the project author (or a pinned third-party
  package). Clickwork does not defend the process against its own
  command code.

## Owner-only permissions

Files that may contain secrets must be accessible only to their owner
on POSIX systems. Clickwork enforces this for:

- User config files (`~/.config/<tool>/config.toml`)
- `.env` dotenv files loaded via `clickwork.config.load_env_file`

Any group or other permission bit (read, write, *or* execute) fails
the check. `chmod 600` is the canonical fix; `0o400` and `0o700` also
pass.

```bash
chmod 600 ~/.config/my-tool/config.toml
chmod 600 .env
```

The check uses `os.fstat(fd)` on the already-opened file descriptor,
not `os.stat(path)`. This closes the TOCTOU (time-of-check /
time-of-use) window: an attacker cannot swap the file between
permission check and read because both operate on the same kernel fd.

Windows has no equivalent POSIX mode bits, so the check is skipped
there. NTFS ACLs are a separate model; clickwork does not enforce
Windows-side access controls.

Implementation: `clickwork.config._check_owner_only_permissions`.

## Verifying release artifacts

Every release from 1.0.1 onward can be verified three ways:

1. **PyPI attestation** (PEP 740) —
   `pypi-attestations verify pypi clickwork==<version>`
2. **Sigstore bundle** (GitHub Release asset) —
   `sigstore verify identity <wheel> --bundle <wheel>.sigstore --cert-identity <workflow-url> --cert-oidc-issuer https://token.actions.githubusercontent.com`
3. **Signed git tag** — `git verify-tag v<version>`

See [verifying.md](verifying.md) for worked examples + troubleshooting.

For pre-1.0.1 releases (no signing) or as a fallback if verify
tooling is unavailable, pin by hash. `pip`'s hash-checking mode
reads hashes from a requirements file rather than the command line:

```text
# requirements.txt
clickwork==1.0.0 --hash=sha256:<hash-from-pypi>
```

```bash
pip install --require-hashes -r requirements.txt
```

PyPI publishes SHA-256 hashes for each artifact on the release page.
`uv.lock` captures the same hashes when `uv add clickwork==1.0.0` is
used, and `uv sync --locked` refuses to install anything whose hash
no longer matches the lockfile.

## Cross-references

- `clickwork.http` module docstring: HTTP security invariants
  (allowlist, no-redirect, scheme guard, redaction policy).
- `clickwork.process.run_with_secrets` docstring: argv / env / stdin
  boundary, secret-in-argv rejection, logging redaction.
- `clickwork.config._check_owner_only_permissions` docstring: the
  TOCTOU-safe fstat check and its Windows carve-out.
- [LLM_REFERENCE.md#common-footguns](llm-reference.md#common-footguns)
  entries 4, 5, 7, 8, and 10 for the security-relevant footguns.
- [GUIDE.md](guide.md) for the practical end-to-end walkthrough,
  including how schemas and `Secret` are wired together.
