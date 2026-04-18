"""add_global_option: install a Click option at root + every group + every subcommand.

This module implements the ``add_global_option`` primitive documented in
issue #14. The goal is to let plugin authors declare an option ONCE and have
it accepted at every level of their CLI hierarchy, with sensible merging
semantics into ``ctx.meta``.

Design choices worth reading before changing this file:

1. **Option callback, not invoke wrapper.** Click fires option ``callback=``
   functions during parsing, exactly when it has the parsed value in hand and
   the Click context for the *current* command level. Hooking at this point
   means we don't have to introspect CliRunner results, monkey-patch
   ``invoke``, or intercept group callbacks -- we just observe the parsed
   value where Click naturally exposes it.

2. **Root-context meta as the single source of truth.** Each level's callback
   walks to ``ctx.find_root()`` and merges into that meta dict. Subcommand
   callbacks run AFTER group callbacks because Click parses the group's
   options, invokes its callback, then recurses into the subcommand. This
   parse order guarantees that a merge callback using
   "innermost wins when explicitly set" on each level arrives at the correct
   final value for value-options.

3. **Call-time snapshot, not retroactive.** The caller hands us the current
   ``cli`` tree and we walk it immediately. Commands attached LATER don't get
   the option. This is intentional: a retroactive scheme would need to
   monkey-patch ``Group.add_command`` and would introduce surprising
   lifecycle interactions with lazy plugin loading. If you need to cover
   commands added later, call ``add_global_option`` again after adding them.

4. **Detecting "explicitly set".** For value options we must distinguish "the
   user passed ``--env=prod``" from "Click filled in the default of ``None``".
   Click exposes ``ctx.get_parameter_source(name)`` for this, returning
   ``ParameterSource.COMMANDLINE`` for user-supplied values and
   ``ParameterSource.DEFAULT`` for defaults. We only overwrite meta when the
   source is not DEFAULT, so a root-level ``--env=prod`` followed by a bare
   subcommand still leaves ``meta['env'] == 'prod'``.

   Flags get simpler treatment: any truthy occurrence wins via OR, because
   the flag's presence is itself the signal regardless of the default.
"""
from __future__ import annotations

from typing import Any

import click
from click.core import ParameterSource


def add_global_option(
    cli: click.Group,
    *param_decls: str,
    **option_kwargs: Any,
) -> None:
    """Install a Click option at root + every group + every subcommand of ``cli``.

    The option is accepted at ANY level of the CLI hierarchy. The resolved
    value is merged into the root Click context's ``meta`` dict under the
    option's Python-identifier name (e.g., ``--foo-bar`` -> ``meta['foo_bar']``).
    Read it from command callbacks via::

        # For add_global_option(cli, "--json", is_flag=True) the meta key
        # is "json" (Click's standard param-decl-to-name derivation). Use
        # whatever key your flag derives to -- ``--my-flag`` -> ``"my_flag"``,
        # ``--api-url`` -> ``"api_url"``, etc.
        root_meta = click.get_current_context().find_root().meta
        is_json = root_meta["json"]

    Merge semantics:
        * Flags (``is_flag=True``) use **OR across levels**: a truthy value
          at ANY level wins, so ``meta[name]`` is ``True`` if the user passed
          the flag at root OR group OR subcommand (or any combination).
        * Value options (strings, ints, enums, ...) use **innermost-wins**:
          the deepest level that *explicitly* supplied the option provides
          the final value. "Explicit" here means any Click
          ``ParameterSource`` other than ``DEFAULT`` -- command line is
          the common case, but environment variables and
          ``default_map``-sourced values also count as explicit and can
          override outer levels. Levels that parsed only the Click
          default do NOT overwrite an already-set value.
        * Not passed anywhere: ``meta[name]`` is ``False`` for flags and the
          Click-resolved default (usually ``None``) for value options.

    Snapshot behaviour:
        Registration is a **call-time snapshot** of ``cli``'s command tree.
        Commands attached to ``cli`` AFTER ``add_global_option`` returns do
        NOT retroactively receive the option. This is deliberate: retroactive
        registration would require monkey-patching ``Group.add_command`` and
        introduces lifecycle surprises. Call ``add_global_option`` again if
        you need to cover later additions.

    Args:
        cli: The root Click group to install the option on. Walked recursively
            to discover nested groups and leaf commands.
        *param_decls: Click parameter declarations -- the same strings you'd
            pass to ``@click.option(...)``, e.g., ``"--json"`` or
            ``"--env", "-e"``.
        **option_kwargs: Keyword arguments forwarded to ``click.Option``. Use
            ``is_flag=True`` for boolean flags, ``default=...`` for value
            options, etc. The ``callback=`` kwarg is reserved by this
            function; passing it raises ``TypeError``.

    Raises:
        TypeError: If ``option_kwargs`` contains a ``callback`` key -- we own
            the callback slot to implement the merge semantics. Wrap the
            click.Option yourself and register it manually if you need a
            custom callback.
        TypeError: If ``option_kwargs`` contains ``expose_value=True`` -- we
            force ``expose_value=False`` so the installed option doesn't
            appear as a kwarg on every command's callback signature. If
            you need the value injected into a specific command's function,
            use ``click.option()`` directly on that command instead.
        ValueError: If Click cannot derive a Python name from
            ``param_decls`` (typically because no long-form flag like
            ``--foo`` was provided).
        ValueError: If any command or group in the tree already has an
            option with the same Python name or flag string -- catches
            both "called ``add_global_option()`` twice with the same flag"
            and "command has the option hand-declared already". The error
            message names the specific command and conflict so the caller
            can locate the issue immediately.

    Examples:
        Flag with OR semantics, read anywhere in your code::

            cli = clickwork.create_cli(name="myapp", commands_dir=...)
            clickwork.add_global_option(cli, "--json", is_flag=True,
                                        help="Output as JSON.")

            # All three invocations leave ctx.find_root().meta['json'] == True:
            #   myapp --json sub-cmd
            #   myapp sub-cmd --json
            #   myapp group --json sub-cmd

        Value option with innermost-wins. Note this example uses
        ``--region`` rather than ``--env``: ``create_cli`` already
        installs ``--env`` at the root (alongside ``--verbose``,
        ``--quiet``, ``--dry-run``, ``--yes``), so calling
        ``add_global_option(cli, "--env", ...)`` against a
        ``create_cli`` root would raise ``ValueError`` for a
        flag-string collision. Pick a name that is not one of the
        clickwork-reserved built-ins::

            clickwork.add_global_option(cli, "--region", default=None,
                                        help="Target region.")

            # myapp --region=us-east sub-cmd --region=eu-west
            #   => ctx.find_root().meta['region'] == 'eu-west'   (inner wins)
            # myapp --region=us-east sub-cmd
            #   => ctx.find_root().meta['region'] == 'us-east'   (outer alone)
            # myapp sub-cmd
            #   => ctx.find_root().meta['region'] is None        (Click default)
    """
    # Guard: we install our own callback to implement the merge. If the
    # caller supplies one, silently dropping it would lead to confusing bugs
    # where their side effect never runs. Raise loudly instead.
    if "callback" in option_kwargs:
        raise TypeError(
            "add_global_option() owns the 'callback=' kwarg; pass a "
            "plain option config without callback, or register the option "
            "manually with click.option() if you need custom callback logic."
        )

    # Derive the option name (the key under which Click stores the parsed
    # value, and the key we use in ctx.meta) by constructing a throwaway
    # Option. Click's own name-derivation logic handles edge cases like
    # flag/slash syntax (``--shout/--no-shout``) and aliases so we don't
    # have to reimplement it.
    name = _derive_option_name(param_decls, option_kwargs)

    # Flags use OR, value options use innermost-wins. But slash-flags
    # (``--foo/--no-foo``) have TWO user-facing forms: the "on" value and
    # an explicit "off" value. OR'ing them across levels would prevent an
    # inner ``--no-foo`` from overriding an outer ``--foo`` (False never
    # wins an OR). Detect slash-flags via the probe's ``secondary_opts``
    # and treat them as value-innermost instead, so users get the
    # intuitive "inner level wins" semantics for both forms.
    #
    # Plain flags (single ``--foo`` declaration) keep OR semantics: there
    # is no way for the user to say "off" at an inner level, so truthy-
    # anywhere is the only sensible rule.
    probe_kwargs = {k: v for k, v in option_kwargs.items() if k != "callback"}
    probe = click.Option(list(param_decls), **probe_kwargs)
    is_slash_flag = bool(getattr(probe, "secondary_opts", None))
    is_flag = bool(option_kwargs.get("is_flag", False)) and not is_slash_flag

    def _merge_callback(
        ctx: click.Context,
        _param: click.Parameter,
        value: Any,
    ) -> Any:
        """Merge this level's parsed value into the root context's meta.

        Runs once per level where the option is installed (root group,
        intermediate groups, and the leaf subcommand). Click invokes it with
        the already-parsed value for this level.

        Args:
            ctx: The Click context for the CURRENT level (root, intermediate
                group, or leaf command). We walk to ``ctx.find_root()`` to
                write into the top-level meta dict.
            _param: The click.Parameter instance for this option at this
                level. Unused -- we use the outer ``name`` variable instead.
            value: The value Click parsed for this level -- either from the
                command line, from the default, or (rarely) from an
                environment variable.

        Returns:
            ``value`` unchanged. Click requires callbacks to return the
            value that will be passed to the command's callback, and we
            don't want to alter local-level behaviour.
        """
        root_meta = ctx.find_root().meta

        if is_flag:
            # OR across levels: any truthy occurrence wins. We read the
            # current meta (defaulting to False so first-call initialization
            # and "not-yet-set" look the same) and OR in the new value. This
            # naturally handles:
            #   - no flag at any level     -> False OR False = False
            #   - flag at outer only       -> False OR True  = True (later
            #                                  inner False OR True = True)
            #   - flag at inner only       -> False OR False = False, then
            #                                  False OR True = True
            #   - flag at both levels      -> True OR True   = True
            root_meta[name] = bool(root_meta.get(name, False)) or bool(value)
        else:
            # Innermost-wins for value options. We overwrite only when this
            # level's value was EXPLICITLY supplied (command line, env var,
            # or default_map) -- never when it came from the plain default,
            # which would stomp on an explicit outer value with None.
            #
            # get_parameter_source() returns None if the parameter wasn't
            # processed for this context (shouldn't happen here since the
            # callback only fires when it WAS processed), and one of the
            # ParameterSource values otherwise.
            source = ctx.get_parameter_source(name)
            if source is not None and source != ParameterSource.DEFAULT:
                root_meta[name] = value
            elif name not in root_meta:
                # First time we see the option at any level and it's just
                # the default. Record the default so downstream code can
                # read ctx.meta[name] without a .get()-with-default dance.
                root_meta[name] = value
            # Else: an outer level already wrote an explicit value; a later
            # default-only level should NOT overwrite it. Do nothing.

        return value

    # Guard: expose_value is owned by add_global_option just like callback.
    # A caller-supplied ``expose_value=True`` would surface the option as a
    # kwarg on every existing command callback, breaking those signatures
    # at runtime. Reject explicitly rather than letting that silently break.
    if option_kwargs.get("expose_value", False):
        raise TypeError(
            "add_global_option() forces expose_value=False so installed "
            "options don't show up as kwargs on every command's callback. "
            "If you need the value injected into a specific command, use "
            "click.option() directly on that command instead."
        )

    # Merge our callback into the kwargs we'll forward to click.Option below.
    # We've already rejected caller-supplied callbacks above, so this assign
    # is safe.
    #
    # WHY expose_value=False: the caller's existing command callbacks weren't
    # written expecting this option as a keyword argument -- they shouldn't
    # have to change just because we installed a global option on their
    # command. ``expose_value=False`` tells Click to parse the option (firing
    # our callback with the value) but NOT pass it to the command callback
    # as a kwarg. The merged value is still available via ctx.meta.
    option_kwargs_with_cb = dict(option_kwargs)
    option_kwargs_with_cb["callback"] = _merge_callback
    option_kwargs_with_cb["expose_value"] = False

    # Compute the full set of flag strings this option will claim, using
    # Click's OWN parsing of param_decls (via the probe constructed above)
    # rather than string matching on the raw decls.
    #
    # WHY reuse the probe: a slash-flag like "--shout/--no-shout" is a
    # single element in param_decls, but Click splits it into
    # probe.opts=["--shout"] + probe.secondary_opts=["--no-shout"]. A
    # naive string-match would leave "--shout/--no-shout" intact and
    # miss a collision with an existing command that already declares
    # just "--shout" or "--no-shout" separately. Collecting from the
    # probe gives us the actual set of strings Click will register on
    # each command, matching how ``existing.opts`` / ``secondary_opts``
    # is populated on already-installed options.
    new_flag_strings = set(getattr(probe, "opts", ())) | set(
        getattr(probe, "secondary_opts", ())
    )

    # Walk the command tree and install a FRESH click.Option on every level.
    # WHY a fresh instance per level: click.Option objects are stateful
    # (they're bound to a specific command's parameter list), so sharing a
    # single instance across multiple commands causes subtle double-registration
    # issues. Constructing per-level is cheap and keeps each command
    # self-contained.
    _install_on_group(cli, param_decls, option_kwargs_with_cb, name, new_flag_strings)


def _derive_option_name(
    param_decls: tuple[str, ...],
    option_kwargs: dict[str, Any],
) -> str:
    """Derive the Python-identifier name Click would assign to this option.

    Click's own ``Option.__init__`` runs this logic -- we construct a
    throwaway instance and read its ``.name`` attribute. This is robust to
    edge cases (short/long aliases, flag/slash syntax, explicit ``name=``
    kwarg) that a hand-rolled implementation would likely miss.

    Args:
        param_decls: The param declaration tuple as passed to
            ``add_global_option``.
        option_kwargs: The option kwargs; specifically we strip ``callback=``
            (unsupported by caller, but defensive) so construction doesn't
            reject the probe.

    Returns:
        The Python identifier Click would use for this option (e.g.,
        ``'foo_bar'`` for ``--foo-bar``).
    """
    # Strip any caller-unfriendly kwargs before the probe construction so
    # this function is robust to misuse upstream (we raise for `callback`
    # earlier, but `_derive_option_name` is a pure helper that shouldn't
    # also validate).
    probe_kwargs = {k: v for k, v in option_kwargs.items() if k != "callback"}
    probe = click.Option(list(param_decls), **probe_kwargs)
    # Option.name is set during __init__ by Click's own derivation rules.
    # The attribute is typed as Optional[str] in Click's stubs, so we
    # validate explicitly. (Using ``assert`` here would be stripped under
    # ``python -O`` and let ``None`` silently propagate into ctx.meta
    # writes, producing confusing errors far from the real cause.)
    if probe.name is None:
        raise ValueError(
            f"Click failed to derive a name for option {param_decls!r}; "
            "pass an explicit 'param_decls' that includes a long-form flag."
        )
    return probe.name


def _install_on_group(
    group: click.Group,
    param_decls: tuple[str, ...],
    option_kwargs: dict[str, Any],
    name: str,
    new_flag_strings: set[str],
) -> None:
    """Recursively install the option on a group, all its subgroups, and all leaves.

    Walks ``group.commands`` and dispatches:
      * Nested ``click.Group`` -> recurse into it AND attach the option to
        the group itself (users can pass the flag between ``outer`` and
        ``inner`` group names).
      * Plain ``click.Command`` -> attach the option to that leaf command.

    The root group itself also gets the option -- this enables the
    ``myapp --json sub-cmd`` case where the flag precedes any subcommand.

    Args:
        group: The current group being processed. On the first call, this is
            the CLI root; on recursive calls it's a nested group.
        param_decls: Param declarations, forwarded unchanged to each new
            ``click.Option`` instance.
        option_kwargs: Option kwargs (already including our merge callback).
        name: Click-derived Python name for the option (used to detect
            conflicts with options already on a command).

    Raises:
        ValueError: If any command or group in the tree already defines
            an option with the same Python name. Calling add_global_option()
            twice with the same flag (or against a tree whose commands
            already declare the flag manually) would otherwise produce a
            confusing Click "option already registered" error at parse time.
    """
    _install_on_command(group, param_decls, option_kwargs, name, new_flag_strings)

    # Then visit every registered subcommand. group.commands is a dict of
    # {name: Command}; we ignore names and just dispatch on type.
    for sub in group.commands.values():
        if isinstance(sub, click.Group):
            # Recurse: the subgroup (and everything under it) gets the option
            # too. This handles arbitrarily-deep nesting.
            _install_on_group(sub, param_decls, option_kwargs, name, new_flag_strings)
        else:
            # Leaf command: just attach the option via the conflict-checked
            # helper so callers get a clear error instead of a runtime
            # Click surprise when something already claimed the name.
            _install_on_command(sub, param_decls, option_kwargs, name, new_flag_strings)


def _install_on_command(
    command: click.Command,
    param_decls: tuple[str, ...],
    option_kwargs: dict[str, Any],
    name: str,
    new_flag_strings: set[str],
) -> None:
    """Attach a fresh click.Option to one command, rejecting name conflicts.

    Checks ``command.params`` for an existing parameter whose Python name
    matches -- this catches both (a) calling ``add_global_option()`` twice
    with the same flag and (b) installing a flag onto a command tree where
    some command already declared it manually. Either case would later
    surface as a confusing Click error at parse/help time; raising here
    points the caller directly at the conflict.

    Args:
        command: The Click command (or group) to install on. Click stores
            params on the command's ``.params`` list; appending is the
            documented way to add options programmatically.
        param_decls: Option declarations (``"--json"`` etc.)
        option_kwargs: Fully prepared kwargs (callback + expose_value set).
        name: The Python name Click would derive; used for the conflict check.

    Raises:
        ValueError: If a parameter with the same name is already on the
            command. Message identifies the command so the caller can
            locate the conflict.
    """
    # We check for conflicts two ways because Click decouples a parameter's
    # Python identifier from its flag strings. ``@click.option("output_json",
    # "--json")`` has ``name == "output_json"`` but opts ``["--json"]``, so a
    # name-only check would miss the flag-string collision and the caller
    # would hit Click's own "option already registered" error later.
    #
    # ``new_flag_strings`` was computed once in add_global_option() from a
    # probe click.Option (so slash-flags like "--shout/--no-shout" are
    # already split into {"--shout", "--no-shout"} instead of being a
    # single unsplit string).
    for existing in command.params:
        existing_opts = set(getattr(existing, "opts", ()))
        # Collect secondary_opts too (the "--no-foo" side of a slash-flag).
        existing_opts.update(getattr(existing, "secondary_opts", ()))
        flag_conflict = new_flag_strings & existing_opts
        name_conflict = getattr(existing, "name", None) == name
        if flag_conflict or name_conflict:
            # Help the caller find the conflict: command.name is Click's
            # own identifier for the command (set via @click.command(name=)
            # or derived from the function name).
            cmd_label = getattr(command, "name", None) or type(command).__name__
            # Name the specific reason so the caller can locate the issue
            # even when names and flag strings disagree.
            if flag_conflict:
                reason = (
                    f"already uses flag string(s) {sorted(flag_conflict)!r} "
                    f"on parameter {getattr(existing, 'name', '?')!r}"
                )
            else:
                reason = f"already has a parameter named {name!r}"
            raise ValueError(
                f"Cannot install global option {param_decls!r}: command "
                f"{cmd_label!r} {reason}. Either rename the conflicting "
                "option, remove the manual declaration, or don't call "
                "add_global_option() twice for the same flag."
            )
    command.params.append(click.Option(list(param_decls), **option_kwargs))
