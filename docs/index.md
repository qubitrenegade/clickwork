# clickwork

**Reusable CLI framework for project automation.**

clickwork gives you a one-file `cli.py` that auto-discovers commands
from a local directory AND from installed plugins, with type-safe
config, structured logging, and subprocess helpers that handle signals
correctly. It's the framework half of "should I just make a CLI for
this?" so you can focus on the commands themselves.

---

## Install

```bash
pip install clickwork
```

Requires Python 3.11+.

## Three ways in

=== "New here"

    A guided Quickstart and a multi-page practical Walkthrough are
    landing in the next docs PR. Until then, start with the
    [User Guide](reference/guide.md) for a single-page tour.

=== "I know what I want to do"

    Task-oriented How-To recipes are landing in the next docs PR. For
    now, the [User Guide](reference/guide.md) has examples inline and
    the [Plugin reference](reference/plugins.md) covers plugin
    authoring.

=== "I need to look something up"

    The **[Reference](reference/guide.md)** section has the full User
    Guide, plugin spec, security model, migration notes, an
    auto-generated [API reference](reference/api.md), and an
    [LLM-oriented reference](reference/llm-reference.md) for use with
    coding assistants.

## Why clickwork

- **Data-driven discovery.** Drop a `cli.py`-shaped file into your
  `commands/` directory; it shows up. Install a plugin that ships
  commands; they show up too. Local wins on collision.
- **Typed config, not string juggling.** `ConfigError` when a key is
  missing or mistyped, with the file path and key name in the message.
- **Signals done right.** Subprocess helpers forward `SIGINT` and
  escalate to `SIGKILL` on timeout. You don't lose half a pipeline to
  a Ctrl-C that got eaten.
- **Typed all the way down.** `py.typed` marker ships in the wheel;
  `mypy --strict` passes on clickwork's own tree.

## Project links

- [Source on GitHub](https://github.com/qubitrenegade/clickwork)
- [Issues + roadmap](https://github.com/qubitrenegade/clickwork/issues)
- [Changelog](https://github.com/qubitrenegade/clickwork/blob/main/CHANGELOG.md)
- [Public API policy](explanation/api-policy.md) — what the semver
  promise covers, and what it doesn't.
