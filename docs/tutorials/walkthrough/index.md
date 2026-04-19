# Practical Walkthrough

A 30-to-60-minute guided build of a realistic clickwork CLI. You'll
end up with a project that has a local command, an installed plugin
that contributes a command, and a publishable wheel.

## What you'll build

A CLI named `projectctl` that helps you operate a toy project:

- `projectctl tail-logs` — a local command that tails a log file
- `projectctl deploy` — a command contributed by an installed plugin
  `projectctl-deploy`

## Pages

1. **[Your first command](01-your-first-command.md)** — project layout,
   local command, `create_cli()` + `commands_dir`, running it.
2. **[Adding a plugin](02-adding-a-plugin.md)** — a separate plugin
   package that contributes commands via entry points, installed
   alongside the main CLI.
3. **[Packaging](03-packaging.md)** — `pyproject.toml` metadata,
   `uv build`, installing the wheel in a fresh venv, sharing with a
   teammate.

## Prerequisites

- Completed the [Quickstart](../quickstart.md) OR comfortable with
  Python packaging basics.
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

Ready? Start with [Your first command](01-your-first-command.md).
