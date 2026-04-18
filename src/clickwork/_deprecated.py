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

The already-warned set is keyed by ``__qualname__`` (falling back to
``__name__``). That key is stable across calls to the *same* wrapped
symbol but distinct across different wrapped symbols, so deprecating
``foo`` does not suppress a later deprecation of ``bar``.

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
import warnings
from typing import Any, Callable, TypeVar

# ``T`` carries the decorated callable's type through the decorator so
# static type-checkers see the original signature on the return value.
# This is important for IDE hover / autocomplete: the user should still
# see ``foo(a: int, b: int) -> int`` after the decorator is applied,
# not an opaque ``Callable[..., Any]``.
T = TypeVar("T", bound=Callable[..., Any])

# Module-level set of qualified names we have already warned about.
# Lives at module scope (not per-decorator-instance) so it survives the
# normal call pattern of "one @deprecated(...) site, many invocations."
# The tradeoff: this is process-wide state. If a test needs to force a
# re-warn, it can clear this set -- but we don't expose a public reset
# because production code should not depend on warning timing.
_warned: set[str] = set()


def _qualname(obj: Any) -> str:
    """Return the most specific identifier we can for a callable.

    Prefer ``__qualname__`` (which encodes class nesting, e.g.
    ``OldWidget.__init__``) so two methods named ``__init__`` on
    different classes don't collide in the warned-set. Fall back to
    ``__name__`` for oddball callables (e.g. ``functools.partial``
    objects don't have ``__qualname__``) and finally to ``repr()`` so
    we never raise from inside a decorator.
    """
    return (
        getattr(obj, "__qualname__", None)
        or getattr(obj, "__name__", None)
        or repr(obj)
    )


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
    # Keeping the ``clickwork:`` prefix lets callers filter narrowly, e.g.
    # ``filterwarnings = ["ignore::DeprecationWarning:clickwork"]`` in
    # their pytest config.
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
            # Use the class's own qualified name as the cache key so the
            # warning text reads "OldWidget is deprecated" rather than
            # "OldWidget.__init__ is deprecated."
            cache_key = _qualname(cls)
            message = _build_message(cache_key)

            @functools.wraps(original_init)
            def new_init(self: Any, *args: Any, **kwargs: Any) -> None:
                if cache_key not in _warned:
                    _warned.add(cache_key)
                    # stacklevel=2 -> blame the caller doing ``OldWidget(...)``,
                    # not this wrapper.
                    warnings.warn(message, DeprecationWarning, stacklevel=2)
                original_init(self, *args, **kwargs)

            cls.__init__ = new_init  # type: ignore[method-assign]
            return cls  # type: ignore[return-value]

        # Function / method path.
        func = target
        cache_key = _qualname(func)
        message = _build_message(cache_key)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if cache_key not in _warned:
                _warned.add(cache_key)
                warnings.warn(message, DeprecationWarning, stacklevel=2)
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
