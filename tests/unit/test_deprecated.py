"""Unit tests for clickwork._deprecated.

These tests pin the contract documented in `docs/API_POLICY.md`:

- `DeprecationWarning` fires on the first call to the deprecated surface,
  NOT at import time. That discipline exists because downstream test
  suites often run with `filterwarnings = ["error"]` (clickwork's own
  pytest config does exactly this). An import-time warning would break
  every downstream test, even for callers who never touch the deprecated
  surface.
- Subsequent calls do NOT re-warn. Deprecation warnings are informative,
  not a spam channel.
- `stacklevel=2` means the warning points at the caller's source line,
  not at the decorator's internals. Without that, tracebacks and
  ``-W error`` output would blame `_deprecated.py` instead of the real
  culprit.
- Decorated functions preserve their signature and docstring so tooling
  (IDE autocomplete, Sphinx, inspect.signature) is unaffected.

The test suite is wired through pytest's ``filterwarnings = ["error"]``
(see pyproject.toml). That means **any unexpected `DeprecationWarning`
raised during collection or a test body crashes the run**. Tests that
intentionally exercise the warning path use
``warnings.catch_warnings()`` or ``pytest.warns()`` to observe without
propagating.
"""
from __future__ import annotations

import inspect
import warnings

import pytest


# ---------------------------------------------------------------------------
# Function-decorator path
# ---------------------------------------------------------------------------


def test_function_emits_warning_on_first_call():
    """First call to a @deprecated function emits DeprecationWarning.

    Also pins the exact message format, including the ``clickwork:``
    prefix that downstream consumers filter on.
    """
    from clickwork._deprecated import deprecated

    @deprecated(since="1.1", removed_in="1.2", reason="use bar() instead")
    def foo() -> int:
        return 42

    with pytest.warns(DeprecationWarning) as record:
        result = foo()

    assert result == 42
    assert len(record) == 1
    message = str(record[0].message)
    # The ``clickwork:`` prefix lets callers narrow warning filters to
    # just clickwork deprecations. Because ``stacklevel=2`` attributes
    # the warning to the CALLER's module (not to ``clickwork``), the
    # correct way to filter is against the **message field** (regex),
    # not the module field. For example, in ``pyproject.toml`` (note
    # DOUBLE QUOTES -- TOML single-quoted strings don't escape the
    # backslash, which would cause the filter to miss the warning)::
    #
    #     [tool.pytest.ini_options]
    #     filterwarnings = [
    #         "ignore:clickwork\\::DeprecationWarning",
    #     ]
    #
    # The second field of that spec is a regex matched against the
    # warning message; our ``clickwork:`` prefix is what makes it work.
    assert message.startswith("clickwork:")
    # The qualified name of the wrapped function appears in the message
    # so the user can grep their own code for the usage site.
    assert "foo" in message
    assert "1.1" in message
    assert "1.2" in message
    assert "use bar() instead" in message


def test_cache_key_is_module_qualified():
    """Two same-named functions in different modules both warn on first call.

    The dedup cache key must include the defining module. If the key
    were just ``__qualname__``, then declaring ``def foo()`` in module
    A and also in module B would make them collide: A's first call
    fires the warning, adds ``"foo"`` to the warned-set, and B's first
    call is then silently suppressed even though it's a different
    symbol. That's a correctness bug in any project that happens to
    reuse short names across modules (very common for ``main``,
    ``run``, ``helper``, etc.).

    We simulate two separate modules by setting ``__module__`` on two
    functions **before** decorating them (_cache_key is computed at
    decoration time, so pre-decoration mutation is what the test needs
    to stage the collision correctly). Both resulting wrappers share
    the same ``__qualname__`` and differ only in ``__module__``.
    """
    from clickwork._deprecated import deprecated

    # _cache_key is computed at DECORATION TIME from the function's
    # attributes as they are THEN. Mutating ``__module__`` on the
    # resulting wrapper after decoration wouldn't change what cache key
    # the decorator stored. To stage the collision scenario correctly,
    # we mutate ``__module__`` BEFORE passing the function through the
    # decorator factory. Two freshly-defined functions in this test
    # share the Python ``__qualname__`` (both are
    # ``test_cache_key_is_module_qualified.<locals>.collide``), so if
    # the cache key were qualname-only they'd collide.
    def collide() -> int:
        return 1
    collide.__module__ = "pkg_a.sub"
    decorated_a = deprecated(since="1.1", removed_in="1.2", reason="gone")(collide)

    # Redefining ``collide`` in the same scope reuses the ``__qualname__``;
    # the point of the test is to prove that two same-qualname functions
    # in DIFFERENT modules don't share a dedup entry.
    def collide() -> int:  # noqa: F811 -- intentional name reuse
        return 2
    collide.__module__ = "pkg_b.sub"
    decorated_b = deprecated(since="1.1", removed_in="1.2", reason="gone")(collide)

    # First call of module A's ``collide`` warns.
    with pytest.warns(DeprecationWarning) as record_a:
        assert decorated_a() == 1
    assert len(record_a) == 1

    # First call of module B's ``collide`` must ALSO warn -- a single
    # DeprecationWarning is expected here, not zero. If the cache key
    # weren't module-qualified, module A's earlier warn would have
    # already filled the slot and this block would raise
    # ``Failed: DID NOT WARN``.
    with pytest.warns(DeprecationWarning) as record_b:
        assert decorated_b() == 2
    assert len(record_b) == 1


def test_only_warns_once():
    """Second call to the same deprecated surface is silent.

    If we warned on every call, a deprecated helper inside a tight loop
    would drown the user's console. Once per symbol is enough to signal
    intent; the migration guide carries the rest of the message.
    """
    from clickwork._deprecated import deprecated

    @deprecated(since="1.1", removed_in="1.2", reason="gone soon")
    def once_only() -> str:
        return "ok"

    # First call warns.
    with pytest.warns(DeprecationWarning):
        once_only()

    # Second call must not warn at all. We escalate warnings to errors
    # inside this block so an accidental second warning would surface as
    # a test failure rather than a silent regression.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert once_only() == "ok"


def test_import_does_not_warn():
    """Importing / decorating must not warn -- only calling does.

    This is the critical import-time guard from ``docs/API_POLICY.md``.
    Downstream suites with ``filterwarnings = ["error"]`` would otherwise
    crash the moment they imported clickwork, even if they never touched
    the deprecated surface.
    """
    from clickwork._deprecated import deprecated

    # ``simplefilter("error")`` inside the with-block means any warning
    # raised during decoration would become an exception and fail the
    # test. The decorator body must therefore be completely silent.
    with warnings.catch_warnings():
        warnings.simplefilter("error")

        @deprecated(since="1.1", removed_in="1.2", reason="nope")
        def untouched() -> None:
            return None

        # Merely referencing the name must also stay silent. Only
        # *calling* the function is the trigger.
        _ = untouched
        _ = untouched.__name__


# ---------------------------------------------------------------------------
# Class-decorator path
# ---------------------------------------------------------------------------


def test_class_decorator_fires_on_instantiation():
    """@deprecated on a class warns at ``Foo()``, not at ``Foo`` reference.

    The decorator installs a wrapper around ``__init__`` so the warning
    only fires when the user actually builds an instance. Holding a
    reference to the class (e.g. for isinstance checks, or because it's
    a default argument) must remain silent.
    """
    from clickwork._deprecated import deprecated

    @deprecated(since="1.1", removed_in="1.2", reason="use Bar")
    class OldWidget:
        def __init__(self, value: int) -> None:
            self.value = value

    # Referencing the class must not warn.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ref = OldWidget  # noqa: F841

    # Instantiating does warn, exactly once.
    with pytest.warns(DeprecationWarning) as record:
        w = OldWidget(7)
    assert w.value == 7
    assert len(record) == 1

    # Second instantiation is silent (same once-only discipline as for
    # functions; the cache key is the class's qualified name).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        w2 = OldWidget(9)
    assert w2.value == 9


# ---------------------------------------------------------------------------
# Metadata preservation
# ---------------------------------------------------------------------------


def test_preserves_signature_and_docstring():
    """Decoration leaves inspect.signature and __doc__ intact.

    ``functools.wraps`` handles this, but we pin it so a future refactor
    that drops wraps (or rolls its own wrapper) fails loudly here.
    """
    from clickwork._deprecated import deprecated

    @deprecated(since="1.1", removed_in="1.2", reason="x")
    def add(a: int, b: int = 1) -> int:
        """Add two numbers."""
        return a + b

    sig = inspect.signature(add)
    assert list(sig.parameters) == ["a", "b"]
    assert sig.parameters["b"].default == 1
    # Return annotation must survive, since type-checkers and IDEs rely
    # on it. (Because this test file uses ``from __future__ import
    # annotations``, the annotation is stored as the string ``"int"``
    # rather than the builtin -- that is PEP 563 behavior, not a
    # decorator artifact. We assert on the string form, which is what
    # the decorator actually has to preserve.)
    assert sig.return_annotation == "int"
    assert add.__doc__ == "Add two numbers."
    assert add.__name__ == "add"
    # ``__wrapped__`` is set by ``functools.wraps`` and is what lets
    # IDEs and debuggers unwrap decorated callables.
    assert hasattr(add, "__wrapped__")


# ---------------------------------------------------------------------------
# Stacklevel
# ---------------------------------------------------------------------------


def test_stacklevel_points_to_caller():
    """Warning is attributed to THIS file, not to _deprecated.py.

    ``stacklevel=2`` inside ``warnings.warn`` tells Python to blame the
    frame above the wrapper. Without that, ``python -W error`` traces
    and ``showwarning`` output would all point at clickwork's internals,
    which is useless for the user trying to find their own call site.
    """
    from clickwork._deprecated import deprecated

    @deprecated(since="1.1", removed_in="1.2", reason="pick a caller")
    def target() -> None:
        return None

    # ``catch_warnings(record=True)`` captures warning objects including
    # their ``filename`` attribute, which is what ``stacklevel`` sets.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        target()

    assert len(caught) == 1
    w = caught[0]
    assert issubclass(w.category, DeprecationWarning)
    # The warning's filename should be THIS test module, not _deprecated.py.
    # We compare on basenames to avoid absolute-path brittleness across
    # worktrees and CI runners.
    assert w.filename.endswith("test_deprecated.py"), (
        f"expected stacklevel=2 to attribute warning to the caller; "
        f"got filename={w.filename!r}"
    )
