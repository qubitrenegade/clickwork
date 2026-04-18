"""Internal deprecation decorator for clickwork's own public surface.

Why the underscore
------------------
This module lives at ``clickwork._deprecated`` and is deliberately NOT
re-exported from ``clickwork/__init__.py``. The leading underscore is a
Python-convention marker meaning "internal; no compatibility promise."
clickwork uses this decorator on *its own* deprecated public symbols so
every deprecation emits a consistent, filterable warning. Plugin authors
who want to deprecate their own commands should write their own
``warnings.warn(..., DeprecationWarning)`` call (or a similar helper);
importing from ``clickwork._deprecated`` is not supported. If demand
grows for a re-exported version, we add a public re-export in a future
minor release -- going private-first is the low-risk default.

See ``docs/API_POLICY.md`` for the full deprecation runway policy (a
symbol deprecated in 1.1 must still work in 1.2, etc.).

First-call-only discipline
--------------------------
``DeprecationWarning`` fires **once per decorated symbol**, on the first
call. This matters for two reasons:

1. **No import-time warnings.** ``docs/API_POLICY.md`` pins this as
   non-negotiable. Downstream test suites often run with
   ``filterwarnings = ["error"]`` (clickwork's own pytest config does
   exactly this). If merely importing a module that *contains* a
   deprecated symbol raised a warning, every downstream test would fail
   the moment the consumer upgraded clickwork -- even consumers who
   never touched the deprecated surface. Emitting on first *call*
   instead of at decoration time means the warning only reaches callers
   who are actually using the doomed API.
2. **No spam.** A deprecated helper inside a hot loop would otherwise
   print thousands of identical messages. One warning per symbol per
   process is enough to signal intent; the changelog and migration
   guide carry the details.

The already-warned set is keyed by ``f"{__module__}.{__qualname__}"``
(with safe fallbacks through ``__name__`` and ``repr()``). The module
prefix matters: without it, two different modules each defining
``def foo()`` would share a cache entry, so after module A's ``foo``
fires its warning, module B's ``foo`` would be silenced for the rest
of the process. The module-qualified key keeps them distinct while
still being stable across repeated calls to the same symbol.

Stacklevel rationale
--------------------
We pass ``stacklevel=2`` to ``warnings.warn``. That tells Python to
attribute the warning to the frame **above** the wrapper, i.e. the
caller's source line. Without it, ``python -W error`` tracebacks and
any ``showwarning`` override would all blame this file, which is useless
to the user: they want to see "this call on line 47 of my_script.py is
deprecated," not "line 100 of clickwork/_deprecated.py."
"""

from __future__ import annotations

import functools
import inspect
import threading
import warnings
from collections.abc import Callable
from typing import Any, TypeVar, cast

# ``T`` carries the decorated callable's type through the decorator so
# static type-checkers see the original signature on the return value.
# This is important for IDE hover / autocomplete: the user should still
# see ``foo(a: int, b: int) -> int`` after the decorator is applied,
# not an opaque ``Callable[..., Any]``.
#
# The TypeVar is **unbounded** so classes type-check as well as
# functions. A bound of ``Callable[..., Any]`` would reject
# ``@deprecated(...)`` on a class, because ``type`` is not a
# ``Callable[..., Any]`` in typing semantics even though classes are
# callable at runtime (calling a class invokes ``__init__``). Dropping
# the bound costs a little precision on the parameter type but keeps
# both the function- and class-decorator paths typeable from a single
# overload, which is what users expect at call sites.
T = TypeVar("T")

# Module-level set of qualified names we have already warned about.
# Lives at module scope (not per-decorator-instance) so it survives the
# normal call pattern of "one @deprecated(...) site, many invocations."
# The tradeoff: this is process-wide state. If a test needs to force a
# re-warn, it can clear this set -- but we don't expose a public reset
# because production code should not depend on warning timing.
_warned: set[str] = set()

# Guards the check-and-add on ``_warned``. Without it, two threads
# racing on the first call of the same deprecated symbol can both pass
# ``cache_key not in _warned`` before either adds, so
# ``warnings.warn(...)`` fires twice and breaks the "warn once per
# symbol per process" contract. A plain ``threading.Lock`` is enough
# since the critical section is a single set mutation. Cost at steady
# state (key already in the set) is one attribute-access + one
# lock-acquire per call, which is negligible next to the warning-emit
# path we're avoiding.
_warned_lock = threading.Lock()


def _qualname(obj: Any) -> str:
    """Return the most specific display identifier we can for a callable.

    Prefer ``__qualname__`` (which encodes class nesting, e.g.
    ``OldWidget.__init__``) so two methods named ``__init__`` on
    different classes read distinctly in the warning text. Fall back to
    ``__name__`` for oddball callables (e.g. ``functools.partial``
    objects don't have ``__qualname__``) and finally to ``repr()`` so
    we never raise from inside a decorator.

    This is the **display** name that appears in the warning message.
    It is deliberately not used as the dedup cache key -- see
    ``_cache_key`` for that, which prefixes the module so two
    identically-named functions in different modules don't collide.
    """
    return getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or repr(obj)


def _cache_key(obj: Any) -> str:
    """Return the module-qualified key used to dedup warnings.

    Keying on ``__qualname__`` alone is wrong: two modules each
    defining ``def foo(): ...`` would share a cache entry, so once
    module A's ``foo`` fires its warning, module B's ``foo`` would be
    silenced for the rest of the process even though it's a different
    symbol. Prefix the module (``fn.__module__``) to keep the entries
    distinct. We still fall back through the same chain as
    ``_qualname`` so an object missing ``__module__`` never raises.
    """
    module = getattr(obj, "__module__", None) or "<unknown-module>"
    return f"{module}.{_qualname(obj)}"


def deprecated(
    since: str,
    removed_in: str,
    reason: str = "",
) -> Callable[[T], T]:
    """Mark a function, method, or class as deprecated.

    Args:
        since: The clickwork version in which the symbol was deprecated
            (e.g. ``"1.1"``). Included verbatim in the warning message.
        removed_in: The earliest clickwork version that may remove the
            symbol (e.g. ``"1.2"``). Per ``docs/API_POLICY.md`` the
            symbol must still work for at least one full minor release,
            so this is typically ``since`` incremented by one minor.
        reason: Human-readable guidance for the caller, usually a
            pointer to the replacement API (e.g. ``"use bar() instead"``).
            Optional but strongly recommended -- a bare "deprecated"
            message is rarely actionable.

    Returns:
        A decorator that wraps the target and emits a
        ``DeprecationWarning`` on its first call.

    Examples:
        Function:

            @deprecated(since="1.1", removed_in="1.2", reason="use new_func()")
            def old_func(): ...

        Class (warning fires at instantiation, not at class reference):

            @deprecated(since="1.1", removed_in="1.2", reason="use NewWidget")
            class OldWidget: ...
    """

    # The warning text is templated once and reused every time we fire.
    # Keeping the ``clickwork:`` prefix lets callers filter narrowly by
    # matching the **message field** (the second field of pytest's
    # ``filterwarnings`` spec, which is a regex against the warning text).
    # Example ``pyproject.toml`` pytest config -- note the DOUBLE QUOTES.
    # TOML single-quoted strings don't process backslash escapes, so
    # ``'ignore:clickwork\\::DeprecationWarning'`` ends up as the literal
    # six-char sequence ``clickwork\\:`` and pytest never matches the
    # warning. Use double quotes so TOML resolves ``\\:`` to ``\:``
    # (the regex-escape for the ``:`` field separator)::
    #
    #     [tool.pytest.ini_options]
    #     filterwarnings = [
    #         "ignore:clickwork\\::DeprecationWarning",
    #     ]
    #
    # Note: the obvious-looking ``"ignore::DeprecationWarning:clickwork"``
    # (module field, 4th field) does NOT work, because ``stacklevel=2``
    # attributes the warning to the CALLER's module, not to ``clickwork``.
    # Filter by the ``clickwork:`` message prefix instead.
    def _build_message(qualname: str) -> str:
        tail = f" {reason}" if reason else ""
        return (
            f"clickwork: {qualname} is deprecated since {since}; "
            f"will be removed in {removed_in}.{tail}"
        )

    def decorator(target: T) -> T:
        # Class path: wrap __init__ so the warning fires at instantiation.
        # Wrapping the class object itself (e.g. via a factory) would
        # break isinstance checks and subclassing, which downstream
        # callers may rely on.
        if inspect.isclass(target):
            cls = target
            original_init = cls.__init__
            # Display name drives the human-readable warning text so the
            # message reads "OldWidget is deprecated" rather than
            # "OldWidget.__init__ is deprecated."
            display = _qualname(cls)
            # Dedup key is module-qualified so two classes named
            # ``OldWidget`` in different modules both get to warn on
            # first instantiation instead of the second one being
            # silently suppressed by the first's cache entry.
            cache_key = _cache_key(cls)
            message = _build_message(display)

            @functools.wraps(original_init)
            def new_init(self: Any, *args: Any, **kwargs: Any) -> Any:
                # Fast-path: membership in a set is O(1), atomic for a
                # single-expression read, and doesn't acquire the lock.
                # Once a symbol has warned, every subsequent call skips
                # the lock entirely -- a deprecated function in a hot
                # loop pays nothing extra. The lock only guards the
                # check-and-add on the first-warn window so two threads
                # racing on the first call don't both emit a warning.
                if cache_key not in _warned:
                    with _warned_lock:
                        should_warn = cache_key not in _warned
                        if should_warn:
                            _warned.add(cache_key)
                    if should_warn:
                        # stacklevel=2 -> blame the caller doing
                        # ``OldWidget(...)`` not this wrapper.
                        warnings.warn(message, DeprecationWarning, stacklevel=2)
                # Return ``original_init``'s result so a buggy
                # ``__init__`` that returns non-None still trips
                # Python's ``type.__call__`` TypeError (which my
                # wrapper would otherwise silently mask by discarding
                # the return).
                return original_init(self, *args, **kwargs)

            cls.__init__ = new_init
            return cls

        # Function / method path.
        # ``T`` is unbounded (so classes type-check too), but mypy can't
        # prove the function-path ``target`` is callable. Cast here so
        # ``functools.wraps`` and the inner ``func(...)`` both
        # type-check; at runtime ``target`` was already confirmed
        # non-class above, so the cast is sound.
        func = cast("Callable[..., Any]", target)
        display = _qualname(func)
        cache_key = _cache_key(func)
        message = _build_message(display)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Same fast-path + lock-on-first-warn discipline as the
            # class path above. Once a symbol has warned, subsequent
            # calls skip the lock entirely via the membership check.
            if cache_key not in _warned:
                with _warned_lock:
                    should_warn = cache_key not in _warned
                    if should_warn:
                        _warned.add(cache_key)
                if should_warn:
                    warnings.warn(message, DeprecationWarning, stacklevel=2)
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
