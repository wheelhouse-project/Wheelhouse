"""Validator for the ``[ai.runtime]`` block (wh-ai-runtime-config-section).

The AI subsystem is a thin client for an OpenAI-compatible server. Usually
somebody else runs that server. When ``[ai.runtime] enabled`` is true,
Wheelhouse starts one itself from a bundled llama.cpp build and a model file
the installer downloaded. This module turns the raw, unchecked table that
``ConfigService`` returns into a typed :class:`RuntimeConfig`.

The port is not a key here
==========================
It is read out of ``[ai.server] base_url``, which the client already uses.
Storing the port twice would let the launcher start a server on one port while
the client talks to another, and no code would notice: the client would report
an unreachable server while a healthy one sat on the other port. Deriving it
means the two cannot disagree. A ``port`` key written inside ``[ai.runtime]``
is ignored on purpose rather than honoured.

Because we start the process ourselves, we know exactly what the address has
to be: plain HTTP, the IPv4 loopback address, an explicit port, and the
OpenAI v1 root. Every part is checked, not only the host and the port. The
client builds each request address by appending to base_url, so a wrong
scheme, a wrong path, a query, or a fragment produces a runtime that starts a
server and then fails every spoken command -- the exact disagreement this
module exists to prevent. Any such address disables the local runtime rather
than being obeyed.

Never raises
============
``from_raw`` follows the same fail-soft contract as ``ClickConfig.from_raw``
(``ui/click_config.py``): a present key with a bad type or range logs an error,
disables the local runtime, and records ``invalid_key``. A missing optional key
takes its default. The AI features themselves stay governed by ``[ai] enabled``
and ``[ai.server] base_url``, so a disabled local runtime means "do not start a
server", not "no AI" -- an external server at that address still works.

Two disabled shapes, matching ClickConfig:

* ``enabled=False`` with ``invalid_key`` set -- something was malformed.
* ``enabled=False`` with ``invalid_key=None`` -- the section is absent, or the
  operator set ``enabled=false``. Nothing failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_SIZE = 8192
DEFAULT_GPU_LAYERS = 99
DEFAULT_STARTUP_TIMEOUT_S = 90

# Hosts we accept in base_url. We start the process on this machine, so the
# address has to point at this machine.
# Only the IPv4 loopback address. The launcher starts the server with
# --host 127.0.0.1 and asks http://127.0.0.1:<port>/health whether it is
# ready, so an IPv6 address here would enable a runtime whose server binds one
# socket while the client connects to another. On Windows the two loopback
# stacks are separate, so the request fails against a server that is running
# correctly. A user who wants IPv6 runs their own server and leaves this
# section switched off.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

# The launcher serves plain HTTP. An https address would have the client
# attempt an encrypted connection to a server speaking plaintext, and every
# request would fail against a server that is running correctly.
_SERVED_SCHEME = "http"

# The client builds its request addresses by appending to base_url: it posts
# to "<base_url>/chat/completions" and asks "<base_url>/models". So base_url
# has to be the OpenAI v1 root exactly, or those two addresses land somewhere
# the server does not answer. A single trailing slash is the one accepted
# variation, because the client removes it before appending.
_SERVED_PATHS = frozenset({"/v1", "/v1/"})

_BASE_URL_KEY = "ai.server.base_url"


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated settings for the Wheelhouse-managed model server.

    ``port`` is derived from ``[ai.server] base_url``; see the module
    docstring. ``invalid_key`` is ``None`` when nothing failed validation --
    including when the operator validly turned the runtime off -- and otherwise
    names the first key that failed.
    """

    enabled: bool
    model_path: str
    binary_dir: str
    port: int
    context_size: int
    gpu_layers: int
    startup_timeout_seconds: int
    invalid_key: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Any, base_url: str) -> "RuntimeConfig":
        """Validate a raw ``[ai.runtime]`` table; never raises.

        ``raw`` is typed ``Any`` deliberately: ``ConfigService`` returns
        ``Any``, and a malformed ``[ai.runtime]`` value can be a scalar or a
        list rather than a table. The ``isinstance`` guard below is a real
        runtime branch.
        """
        if not isinstance(raw, dict):
            if raw is not None:
                logger.error(
                    "[ai.runtime] must be a table, got %s; Wheelhouse will not "
                    "start a local model server.",
                    type(raw).__name__,
                )
            return _disabled("<not-a-table>" if raw is not None else None)
        try:
            return cls._validate(raw, base_url)
        except Exception:  # noqa: BLE001 -- the contract is to NEVER raise
            logger.exception(
                "[ai.runtime] validation failed unexpectedly; Wheelhouse will "
                "not start a local model server."
            )
            return _disabled("<unexpected>")

    @classmethod
    def _validate(cls, raw: dict, base_url: str) -> "RuntimeConfig":
        # An absent section is not a fault: the overwhelmingly common case is
        # an external server, which is what the AI subsystem shipped with.
        if "enabled" not in raw:
            return _disabled(None)

        enabled = raw["enabled"]
        # bool is a subclass of int in Python, so an int 1 would pass a bare
        # isinstance(x, int) check. Require a real bool.
        if not isinstance(enabled, bool):
            return _fault("enabled", enabled)
        if not enabled:
            # Valid opt-out. Not a fault, so invalid_key stays None.
            return _disabled(None)

        model_path = raw.get("model_path", "")
        if not isinstance(model_path, str) or not model_path.strip():
            return _fault("model_path", model_path)

        binary_dir = raw.get("binary_dir", "")
        if not isinstance(binary_dir, str) or not binary_dir.strip():
            return _fault("binary_dir", binary_dir)

        context_size = _positive_int(raw, "context_size", DEFAULT_CONTEXT_SIZE)
        if context_size is None:
            return _fault("context_size", raw.get("context_size"))

        # 0 is a real value here: it means place no layers on the graphics card
        # and run entirely on the processor, which is the 16 GB-system-memory
        # rung of the hardware ladder.
        gpu_layers = _non_negative_int(raw, "gpu_layers", DEFAULT_GPU_LAYERS)
        if gpu_layers is None:
            return _fault("gpu_layers", raw.get("gpu_layers"))

        startup_timeout = _positive_int(
            raw, "startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT_S
        )
        if startup_timeout is None:
            return _fault(
                "startup_timeout_seconds", raw.get("startup_timeout_seconds")
            )

        port = _port_from_base_url(base_url)
        if port is None:
            return _fault_named(_BASE_URL_KEY, base_url)

        return cls(
            enabled=True,
            model_path=model_path,
            binary_dir=binary_dir,
            port=port,
            context_size=context_size,
            gpu_layers=gpu_layers,
            startup_timeout_seconds=startup_timeout,
            invalid_key=None,
        )


def _port_from_base_url(base_url: str) -> Optional[int]:
    """The port we must start the server on, or None if base_url is unusable.

    Checks the whole address, not only the host and the port. The launcher
    always serves plain HTTP on the IPv4 loopback address at the OpenAI v1
    root, and the client builds every request address by appending to
    base_url. Any part of the address that disagrees with that produces a
    runtime which starts a server and then fails every spoken command, so
    each part is checked here rather than only the two that name the socket.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    try:
        parts = urlsplit(base_url.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        # urlsplit raises on a malformed or out-of-range port, such as
        # "http://x:notaport/" or "http://127.0.0.1:99999/v1".
        return None

    if parts.scheme.lower() != _SERVED_SCHEME:
        return None
    if host not in _LOOPBACK_HOSTS:
        return None
    # Port 0 asks the operating system to choose a free port. The server would
    # then listen somewhere we cannot predict, while the health check and the
    # client both keep connecting to port 0.
    if port is None or not 1 <= port <= 65535:
        return None
    if parts.path not in _SERVED_PATHS:
        return None
    # A query or fragment would end up in the middle of the built address,
    # before "/chat/completions" rather than after it.
    if parts.query or parts.fragment:
        return None
    # We start this server ourselves and give it no credentials, so an address
    # carrying them describes a server that is not the one we would start.
    if parts.username or parts.password:
        return None
    return port


def _positive_int(raw: dict, key: str, default: int) -> Optional[int]:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _non_negative_int(raw: dict, key: str, default: int) -> Optional[int]:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _disabled(invalid_key: Optional[str]) -> RuntimeConfig:
    return RuntimeConfig(
        enabled=False,
        model_path="",
        binary_dir="",
        port=0,
        context_size=DEFAULT_CONTEXT_SIZE,
        gpu_layers=DEFAULT_GPU_LAYERS,
        startup_timeout_seconds=DEFAULT_STARTUP_TIMEOUT_S,
        invalid_key=invalid_key,
    )


def _fault(key: str, value: Any) -> RuntimeConfig:
    return _fault_named(key, value)


def _fault_named(key: str, value: Any) -> RuntimeConfig:
    logger.error(
        "[ai.runtime] %s is invalid (%r); Wheelhouse will not start a local "
        "model server. An external server at [ai.server] base_url still works.",
        key,
        value,
    )
    return _disabled(key)
