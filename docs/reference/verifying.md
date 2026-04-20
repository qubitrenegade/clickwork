# Verifying a clickwork release

Every release from 1.0.1 onward is provenance-protected three ways:
the PyPI package carries PEP 740 attestations, the GitHub Release
carries Sigstore `.sigstore` bundles, and the git tag is GPG-signed
(workflow key by default; maintainer key in fallback). Pick whichever
verify path matches how you installed.

(Examples below use `1.0.1` as the target version — substitute the
version you installed.)

## Verifying the PyPI package

!!! note "Manual verify today"
    `pip`'s built-in auto-verify of PEP 740 attestations is not yet
    GA. Today, verification is a manual step via the
    [`pypi-attestations`](https://pypi.org/project/pypi-attestations/)
    CLI. When installers ship auto-verify, this section will update
    to reference the flag.

Install the `pypi-attestations` CLI in a scratch venv:

    pip install pypi-attestations

Verify the attestations for clickwork 1.0.1 on PyPI:

    pypi-attestations verify pypi clickwork==1.0.1

Expected output: "OK" per artifact, with the workflow identity
(`https://github.com/qubitrenegade/clickwork/.github/workflows/publish.yml@refs/tags/v1.0.1`)
named.

## Verifying a GitHub Release asset

Download the wheel (or sdist) + its `.sigstore` bundle from the
Release page. Install the `sigstore-python` CLI from PyPI
(`sigstore`):

    pip install sigstore

Verify (adjust the `--cert-identity` tag ref to match the version
you downloaded — for a prerelease use the hyphenated form, e.g.
`@refs/tags/v1.0.1-rc0`):

    sigstore verify identity \
      ./clickwork-1.0.1-py3-none-any.whl \
      --bundle ./clickwork-1.0.1-py3-none-any.whl.sigstore \
      --cert-identity https://github.com/qubitrenegade/clickwork/.github/workflows/publish.yml@refs/tags/v1.0.1 \
      --cert-oidc-issuer https://token.actions.githubusercontent.com

(Repeat with the sdist if you pulled the sdist.)

Expected output: `OK: ./clickwork-1.0.1-py3-none-any.whl`.

## Verifying the git tag

    git verify-tag v1.0.1

Expected output: "Good signature from" followed by either the
release-signing key UID (workflow path, default for 1.0.1+) or the
maintainer's personal key UID (local-GPG fallback path, documented in
`CONTRIBUTING.md` for emergency release cuts).

The public half of whichever key signed the tag is published on the
maintainer's GitHub account (Settings → SSH and GPG keys), which is
what gives signed tags a green "Verified" badge on the tag detail
page.

## Troubleshooting

### No `.sigstore` bundle on the Release page

Releases before 1.0.1 were not signed. If the Release page has no
`.sigstore` files, the bundle verify path is unavailable and you
should either upgrade to 1.0.1+ or fall back to the hash-pinning
verify path documented in [security.md](security.md).

### `pypi-attestations` reports no attestations

Check you're on 1.0.1 or later: `pip show clickwork`. Attestations
start with 1.0.1.

### `git verify-tag` says "Can't check signature: No public key"

The tag is signed (by the workflow key for the recommended release
path, or the maintainer's personal key for the local-GPG fallback
documented in `CONTRIBUTING.md`), but your local GPG keyring doesn't
have the signer public key yet. Both keys are published as GPG keys
on the maintainer's GitHub account, which GitHub exposes via
`https://github.com/<user>.gpg`:

    curl -fsSL https://github.com/qubitrenegade.gpg | gpg --import

This returns every public GPG key associated with the account
(workflow key + maintainer key); GPG will pick whichever one matches
the tag's signature.

If `git verify-tag` instead reports "no signature", the tag is
unsigned and this fallback flow does not apply.

### `sigstore verify identity` rejects the certificate identity

The `--cert-identity` is the workflow URL that signed the artifact.
For a final release `vX.Y.Z`, the ref form is `refs/tags/vX.Y.Z`;
for a hyphenated prerelease `vX.Y.Z-rc0`, it's `refs/tags/vX.Y.Z-rc0`
exactly as the tag reads. If your `--cert-identity` string doesn't
match the tag you installed from, verification will (correctly)
reject — adjust and retry.

## See also

- [security.md](security.md) — threat model + hash-pinning fallback
  verify path for pre-1.0.1 releases.
- [CONTRIBUTING.md — Cutting a release (recommended: workflow-driven)](https://github.com/qubitrenegade/clickwork/blob/main/CONTRIBUTING.md#cutting-a-release-recommended-workflow-driven)
  — how the release-signing machinery works from the maintainer
  side. (Absolute GitHub URL because `CONTRIBUTING.md` isn't under
  `docs/` — a relative link would break the docs build.)
- Parent issue: [#61](https://github.com/qubitrenegade/clickwork/issues/61).
