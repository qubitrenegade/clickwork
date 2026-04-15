"""Sample 'hello' command group demonstrating qbrd-tools framework features.

This command group shows:
- Basic command registration (just export cli = ...)
- Subcommand groups (@click.group)
- Accessing CliContext (config, dry-run, logging)
- Using require() for prerequisite checks
- Using run()/capture() for subprocess execution
"""
import click


@click.group()
def hello():
    """Sample commands for testing and demonstration."""
    pass


@hello.command()
@click.argument("name", default="world")
@click.pass_obj
def greet(ctx, name: str):
    """Say hello to someone. Demonstrates basic command + context access."""
    if ctx and ctx.verbose:
        ctx.logger.info("Verbose mode: greeting %s", name)
    click.echo(f"Hello, {name}!")


@hello.command()
@click.pass_obj
def info(ctx):
    """Show current config and flags. Demonstrates config access."""
    click.echo(f"env: {ctx.env}")
    click.echo(f"dry_run: {ctx.dry_run}")
    click.echo(f"verbose: {ctx.verbose}")


# The framework discovers commands via this export.
# For a group, this makes 'hello' a top-level command with subcommands.
cli = hello
