# Plugin authoring and distribution

A 15-minute walkthrough for shipping a clickwork plugin on PyPI. If you
already have a Python project and want its commands to appear inside a
host CLI (or inside any clickwork CLI that opts into installed-plugin
discovery), start here.

This guide is entry-point focused. For deeper material on commands,
config, subprocess helpers, and testing, follow the links into
[GUIDE.md](GUIDE.md), [API_POLICY.md](API_POLICY.md), and
[LLM_REFERENCE.md](LLM_REFERENCE.md) rather than re-reading the same
content twice.

## What is a clickwork plugin

A clickwork plugin is a Click command (or group) that a clickwork-built
CLI discovers at runtime. There are two delivery paths, and picking
between them is really a question of who owns the code:

- **Local commands** live in a `commands/` directory next to the CLI
  entry point. The host project owns the files. The framework imports
  each `.py`, looks for a `cli` attribute, and registers it. This is
  the right path when the commands are specific to one project, get
  edited in the same repo, and ship alongside that repo's other code.
- **Installed plugins** ship as their own pip-installable package. The
  package declares a `clickwork.commands` entry point and the framework
  finds it via `importlib.metadata`. Pick this when the commands are
  reusable across projects, have their own release cadence, or come
  from a different team or external contributor than the host CLI.

Both mechanisms can coexist in one CLI. When a local command and an
installed plugin register the same name, the local one wins and the
framework logs a note. See the "Plugin Discovery" section in the
[README](../README.md#plugin-discovery) for the precedence rules.

The rest of this guide covers the installed-plugin path. If you only
need local commands, [GUIDE.md](GUIDE.md#your-first-cli) is the better
starting point.

## Anatomy of a plugin package

A minimal plugin is one pyproject file, one `src/` tree, and one
command module. Structure:

```
my-deploy-tools/
  pyproject.toml
  src/
    my_deploy_tools/
      __init__.py
      deploy.py
```

`pyproject.toml` carries the entry-point declaration. The examples in
this guide target the **1.0 release and later**. On the 0.2.x series
the canonical dependency was `clickwork>=0.2,<1` and several APIs
referenced below (`strict=`, `package_name=`, etc.) were not yet
public. If you are upgrading from 0.2.x, [MIGRATING.md](MIGRATING.md)
walks through the breaking changes.

The important parts of the example are the `clickwork>=1.0` dependency
(so pip refuses to install your plugin against an unsupported framework
release) and the `[project.entry-points."clickwork.commands"]` table:

```toml
[project]
name = "my-deploy-tools"
version = "0.1.0"
description = "Deployment commands for the acme CLI"
requires-python = ">=3.11"
dependencies = [
    "clickwork>=1.0",
]

[project.entry-points."clickwork.commands"]
# The key is the command name shown in --help. The value is the
# import path to the click Command or Group. Use "cli" as the
# attribute name to match clickwork's protocol-level contract
# (see API_POLICY.md's "Protocol-level surfaces"). The loader
# technically accepts any `module:attribute` path, but publishing
# under a non-standard name isn't covered by the stability promise
# and makes your plugin harder for reviewers to read.
deploy = "my_deploy_tools.deploy:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/my_deploy_tools"]
```

One entry-point line per command. A single plugin package can register
multiple commands by adding more rows to that table.

The entry-point group name (`clickwork.commands`) and the shape of a
registered entry (a `click.Command` or `click.Group`) are both part of
clickwork's protocol-level public surface, so your plugin keeps working
across minor releases. See
[API_POLICY.md](API_POLICY.md#protocol-level-surfaces) for the exact
promise.

## Writing a command

The command module itself is pure Click plus a few clickwork imports.
The command receives a typed `CliContext` that exposes config, flags,
subprocess helpers, and prerequisite checks -- everything a command
usually needs, in one object. Prefer `@pass_cli_context` over
`@click.pass_obj`: both receive the same `CliContext` because
clickwork forwards `obj=ctx.obj` through the entry-point proxy,
but `pass_cli_context` carries clickwork-specific type hints and
a clearer error if the CLI wasn't built via `create_cli()`. Use
`@click.pass_obj` when you specifically want Click's native
decorator (e.g. for cross-framework compatibility); otherwise the
clickwork one is slightly easier to debug.

```python
# src/my_deploy_tools/deploy.py
"""Deploy a component to the active environment."""
from __future__ import annotations

import click

from clickwork import CliContext, pass_cli_context


@click.command()
@click.argument("target")
@click.option(
    "--force/--no-force",
    default=False,
    help="Skip the staging-environment guard.",
)
@pass_cli_context
def cli(ctx: CliContext, target: str, force: bool) -> None:
    """Deploy TARGET using the host CLI's configured credentials."""
    # Fail fast if the CLI the operator needs isn't installed.
    ctx.require("wrangler")

    # Read config resolved from env vars, repo TOML, and user TOML.
    account_id = ctx.config.get("cloudflare.account_id")
    if account_id is None:
        raise click.ClickException("cloudflare.account_id is not configured")

    # Refuse production unless the operator passes --force.
    if ctx.env == "production" and not force:
        raise click.ClickException("production deploys require --force")

    # ctx.run respects --dry-run: in dry-run mode it logs the command
    # at INFO (visible at -v or higher) and returns without spawning a
    # subprocess.
    ctx.run(
        ["wrangler", "deploy", target, "--account-id", account_id],
    )
```

The module exposes its command as `cli`, which is the attribute the
entry-point value `my_deploy_tools.deploy:cli` points to. That name is
convention, not requirement; any attribute name works as long as the
entry-point value matches.

If your command needs to pass secrets to a subprocess, reach for
`clickwork.process.run_with_secrets` rather than threading them through
`argv` -- `ps` output is world-readable on every mainstream OS. See
[GUIDE.md on secret-safe subprocesses](GUIDE.md#passing-secrets-to-subprocesses)
for the full pattern.

## Testing the plugin

The bare minimum is one test that invokes the command end-to-end
through Click's testing harness. `clickwork.testing.run_cli` and
`make_test_cli` collapse the usual boilerplate (constructing a runner,
pinning `catch_exceptions=False` so real tracebacks surface in pytest
output) into two calls:

```python
# tests/test_deploy.py
from clickwork.testing import make_test_cli, run_cli

from my_deploy_tools.deploy import cli as deploy_cli


def test_deploy_requires_force_in_production():
    # make_test_cli builds a clickwork CLI with no commands, which is
    # fine for testing a command we have imported directly. For a full
    # discovery round trip, pass commands_dir= or install the plugin
    # into the test venv.
    host = make_test_cli()
    host.add_command(deploy_cli, name="deploy")

    result = run_cli(host, ["--env", "production", "deploy", "site"])

    assert result.exit_code != 0
    assert "production deploys require --force" in result.stderr
```

For full coverage patterns -- stream-specific assertions, dry-run
expectations, schema validation, subprocess mocking -- see
[GUIDE.md's "Testing commands with `clickwork.testing`" section](GUIDE.md#testing-commands-with-clickworktesting).
That section is the canonical reference; this guide stays focused on
the packaging path.

## Publishing to PyPI

Once the plugin is tested, the release flow is the standard uv or
twine one. Build the artifacts and upload them:

```bash
uv build                 # produces dist/*.whl and dist/*.tar.gz
uv publish               # uploads to PyPI; wants PYPI_TOKEN in env
# or, if you prefer twine:
# python -m build && twine upload dist/*
```

The key thing to notice: the consumer does not have to do anything
special in their CLI to pick up your commands. As soon as
`pip install my-deploy-tools` (or the uv/pipx equivalent) runs into
the same environment as the host CLI, `importlib.metadata` sees your
entry point and the next invocation discovers your commands. A host
CLI built with `create_cli(name="acme", package_name="acme")` gets
your `deploy` subcommand automatically -- no code change on their
side. The `package_name` kwarg is the post-#48 way to opt into
`--version`; pass it so `acme --version` prints the host's installed
version string. See
[GUIDE.md on the version flag](GUIDE.md#version-flag) for the full
story.

For production host CLIs, the host author should also pass
`strict=True` to `create_cli()` so discovery-time failures raise at
startup instead of silently dropping the command. What strict catches
depends on the discovery mechanism:

- **Directory scan**: missing `cli` attribute, import error, invalid
  `cli` type, duplicate command name -- all caught at startup, because
  directory-scanned modules are imported eagerly.
- **Entry-point scan**: entry-point enumeration failures (e.g. a
  malformed installed distribution's metadata) and duplicate
  entry-point names, because those are the two categories visible
  without loading a plugin. Categories that require importing the
  plugin -- missing `cli` attribute, import error, invalid `cli`
  type, per-plugin flag collisions -- are deferred to invocation
  time, since `LazyEntryPointCommand` does not load its target until
  the command actually runs. Startup stays fast; some defects move
  to first-use time. If you want those caught at release-validation
  time rather than by operators, run your plugin's own test suite
  in CI (the plugin author's responsibility).

Strict discovery is opt-in for compatibility with existing CLIs; new
deployments should turn it on.

## Testing against multiple clickwork versions

Real plugins end up living longer than a single clickwork minor. If
you want a full matrix (every supported Python against every
clickwork minor you claim to support), reach for tox or nox -- the
ergonomics are worth it once the matrix has more than two rows.

For a one-off check without adding a test runner, use uv's lock-file
workflow:

```bash
uv lock --upgrade-package clickwork==1.0.0
uv run pytest
uv lock --upgrade-package clickwork==1.1.0
uv run pytest
```

Do this whenever you upgrade your own `clickwork>=X` floor: pin the
old ceiling, run the suite, pin the new floor, run again. The lock
file captures whichever version succeeded last, so commit the one
you want CI to honour.

## Upgrade path when clickwork changes

Each clickwork minor ships with a migration note in `CHANGELOG.md`,
and breaking changes carry a `BREAKING:` marker in the PR that
introduced them. For the 0.x to 1.0 jump specifically, see
`MIGRATING.md` once 1.0 ships -- it will enumerate every breaking
change from the 0.2.x series, every deprecation shim, and every new
public API your plugin can start relying on.

Deprecated public symbols stay available for at least one full minor
release before removal (e.g. a symbol deprecated in 1.1 is removed no
earlier than 1.2), and emit `DeprecationWarning` on first use. Run
your test suite with `-W error::DeprecationWarning` to catch them
before your plugin's users do.

## Common pitfalls

A short list of things that bite plugin authors specifically. For the
longer catalogue that covers command-authoring footguns too, see
[LLM_REFERENCE.md's common footguns](LLM_REFERENCE.md#common-footguns).

- **Forgetting to re-install after editing `pyproject.toml`.** Entry
  points are baked into the installed distribution's metadata at
  install time. Editing the entry-point table and expecting the host
  CLI to pick it up on the next run will not work -- you need
  `uv pip install -e .` (or the pip equivalent) for the metadata to
  refresh.
- **Shadowing a host CLI's local command.** If the host keeps a
  `commands/deploy.py` and your plugin also registers `deploy`, the
  host's local file wins. Clickwork logs a note about the shadowing
  at INFO level, so it's not strictly silent, but a host that runs
  at the default WARNING verbosity will still see your command
  disappear. Pick a more specific name (`acme-deploy`) or coordinate
  with the host.
- **Importing private clickwork modules.** Anything under
  `clickwork._types`, `clickwork._logging`, or similar is private
  and can change without a major bump. Import only from the surface
  documented in [API_POLICY.md](API_POLICY.md#public-api-surface).
- **Hardcoding the clickwork version in your command code.** Ask
  `importlib.metadata.version("clickwork")` at runtime if you need
  to branch on framework version -- never hardcode `"1.0.0"` as a
  string constant, and never pin your dependency to `clickwork==X`
  rather than `clickwork>=X`.
- **Forgetting the `clickwork` dependency entirely.** A plugin that
  imports from `clickwork` but does not list it in
  `[project].dependencies` will install and import fine in your dev
  venv (where clickwork is present transitively) and fail hard for
  end users who install only your plugin. Pin
  `clickwork>=1.0` explicitly.
