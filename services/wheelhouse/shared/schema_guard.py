"""Shared raw-exception guard for IPC schema ``from_dict`` parsers.

wh-schema-hostile-mapping-guard (from codex finding wh-n29v.81.1). The
wh-uf54 graceful-degrade boundary promises every schema's ``from_dict``
lets ONLY its typed SchemaError escape. The parsers access their payload
via ``key in payload`` / ``payload[key]`` after an ``isinstance(payload,
Mapping)`` gate; a Mapping subclass whose ``__contains__`` or
``__getitem__`` raises passes the gate and would otherwise bubble the RAW
``KeyError`` / ``TypeError`` / ``AttributeError`` out of the parse body.

``reraise_as_schema_error`` converts those three to the schema's typed
error. The typed errors are ``ValueError`` subclasses -- deliberately NOT
in the caught tuple -- so an already-typed error passes through unchanged.

Reachability is low (the real Input -> Logic transport is pickle over the
response queue, which reconstructs a plain dict), so this is consistency
hardening of the promise, not a live-bug fix.
``ShowNumberedOverlayResponse.from_dict`` carries the same guard inline
(the original wh-n29v.81.1 fix) rather than via this decorator; the
behavior is identical.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


def reraise_as_schema_error(
    error_cls: type[Exception],
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Decorator: convert raw KeyError/TypeError/AttributeError to
    ``error_cls``.

    Apply UNDER ``@classmethod`` (classmethod outermost)::

        @classmethod
        @reraise_as_schema_error(FooSchemaError)
        def from_dict(cls, payload): ...
    """

    def decorate(func: Callable[..., _T]) -> Callable[..., _T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> _T:
            try:
                return func(*args, **kwargs)
            except (KeyError, TypeError, AttributeError) as exc:
                raise error_cls(f"malformed payload: {exc!r}") from exc

        return wrapper

    return decorate
