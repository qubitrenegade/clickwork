# 3. Packaging

Build `projectctl` and `projectctl-deploy` as wheels, install them
into a fresh venv, and confirm they work the same way. This is the
"can I hand this to a teammate?" gate.

## Fill in the metadata

In `projectctl/pyproject.toml`:

```toml
[project]
name = "projectctl"
version = "0.1.0"
description = "Operate the toy project."
authors = [{name = "Your Name", email = "you@example.com"}]
requires-python = ">=3.11"
dependencies = ["clickwork>=1.0"]

[project.scripts]
projectctl = "projectctl.__main__:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

The `[project.scripts]` entry means `pip install projectctl` puts a
`projectctl` executable on `$PATH` — no more `python -m projectctl`.

Do the same for `projectctl-deploy/pyproject.toml` (it doesn't need a
`project.scripts` entry because it's loaded via the plugin entry
point).

## Build

In each directory:

```bash
uv build
```

You'll get `dist/projectctl-0.1.0-py3-none-any.whl` and
`dist/projectctl-0.1.0.tar.gz` (and similar for the plugin).

## Smoke-test in a fresh venv

```bash
cd /tmp
python -m venv smoke
source smoke/bin/activate
pip install /path/to/projectctl/dist/projectctl-0.1.0-py3-none-any.whl
pip install /path/to/projectctl-deploy/dist/projectctl_deploy-0.1.0-py3-none-any.whl
projectctl --help
projectctl deploy --env production --dry-run
deactivate && rm -rf smoke
```

Both commands appear. Plugin discovery works off the installed wheel's
entry points, no extra config needed.

## Share with a teammate

- Push both projects to git.
- Cut a release on each (`uv build` → upload wheel to PyPI or an
  internal index).
- Teammate: `pip install projectctl projectctl-deploy`.

That's the full loop: local command → plugin → wheel → installed.

## Next

- **[User Guide](../../reference/guide.md)** — the full reference for
  `create_cli()` options, config handling, subprocess helpers.
- **[Plugin reference](../../reference/plugins.md)** — the entry-point
  format, naming conventions, and discovery rules in full.
- **[How-To recipes](../../how-to/index.md)** — if you have a specific
  task in mind.
