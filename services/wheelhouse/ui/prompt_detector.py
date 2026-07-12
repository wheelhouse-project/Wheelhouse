"""Detect whether a terminal shell is at a prompt (idle) or running a command.

After the console-probe-helper split (wh-jvrs.1), this module performs NO
Win32 console work in the Logic process. The console-input-mode probe that used
to run inline here -- and could leak the Logic process's logging output into
the user's focused terminal during the brief window the process was bound to
the foreign console -- now lives in an isolated helper subprocess
(``ui/console_probe_helper.py``), reached over a request/reply pipe by
``ui/console_probe_client.py``.

``PromptDetector`` is kept as the named seam the production wiring already
injects (``speech_handler._default_prompt_detector_call`` constructs it and
calls ``is_at_prompt``). It is now a thin delegator: it forwards
``is_at_prompt(process_name, pid)`` to a ``ConsoleProbeClient`` and handles a
raised exception by its type. A ``ConsoleProbeError`` -- the client's
out-of-band transport-failure signal (read timeout, EOF, broken pipe,
malformed response, pid mismatch, dead helper) -- is PROPAGATED, not
swallowed, so the policy routes it to ``prompt_detector_error`` instead of
caching a fake busy verdict (wh-jvrs.3.1). Any OTHER unexpected exception
fails closed (returns False) so a genuine bug never crashes the speech path.
All caching, transport, timeout, and crash-recovery behaviour lives in the
client; the helper owns the console attach and the at-prompt shell-walk logic.

The production caller constructs a fresh ``PromptDetector()`` on every detector
call (``speech_handler._default_prompt_detector_call``), and that caller is out
of scope for this slice. To keep the persistent-helper design real anyway, the
default client is a MODULE-LEVEL SINGLETON: every ``PromptDetector()`` built
without an explicit ``client`` shares one long-lived ``ConsoleProbeClient`` --
so one helper subprocess is spawned, the 0.2s per-pid cache is shared across
calls, and the respawn-on-EOF recovery actually has a surviving client to act
on. Without the singleton, a fresh client (and a cold subprocess spawn) would
land on the voice hot path on every probe, the cache would never hit, and the
respawn branch would be unreachable (wh-jvrs.1.1.1). Tests inject an explicit
``client`` and never touch the singleton.
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Process-wide shared client. Built once on first production use and reused by
# every default-constructed PromptDetector so the helper subprocess, its pipe,
# and the per-pid cache persist across calls. Guarded by a lock because the
# first construction can race the speech pipeline's prewarm and should_redirect
# paths (the policy runs the detector on a single-worker executor, but prewarm
# scheduling and the executor thread are different threads).
_shared_client = None
_shared_client_lock = threading.Lock()
# Set once, the first time the singleton is built, so the atexit teardown is
# registered exactly once per process (wh-jvrs.2.2).
_atexit_registered = False


def _close_shared_client() -> None:
    """Terminate the shared helper subprocess and drop the singleton.

    Idempotent: a no-op when no singleton was ever built. Registered with
    ``atexit`` on first construction so the Logic process explicitly reaps the
    helper on a clean exit instead of relying solely on the OS closing the
    helper's stdin pipe (which makes its ``for raw in stdin`` loop hit EOF). The
    OS-pipe path still saves us if the process is killed before atexit runs;
    this is belt-and-suspenders against an orphaned, console-attaching helper --
    the exact failure class this split exists to prevent (wh-jvrs.2.2). Also the
    idempotent disposal hook a future in-process restart path can call.
    """
    global _shared_client
    with _shared_client_lock:
        client = _shared_client
        _shared_client = None
    if client is None:
        return
    try:
        client.close()
    except Exception:
        logger.debug("prompt_detector: shared client close() failed", exc_info=True)


def is_console_probe_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` is a ConsoleProbeError (or a subclass thereof).

    Robust to BOTH the dual import-path hazard AND subclassing. This is the
    single probe-error classifier shared by ``PromptDetector.is_at_prompt`` and
    ``speech_handler._default_prompt_detector_call`` so the two propagation
    guards cannot drift (wh-jvrs.3.6).

    Two hazards are handled together:

      * **Dual import path.** ``ConsoleProbeError`` can resolve to TWO distinct
        class objects under the test sys.path -- one via
        ``ui.console_probe_client`` (service-dir on the path) and one via
        ``services.wheelhouse.ui.console_probe_client`` (project-root on the
        path). A plain ``except`` bound to one would miss an instance of the
        other. We ``isinstance`` against BOTH copies.
      * **Subclasses.** A name-ONLY check (``type(exc).__name__ ==
        "ConsoleProbeError"``) recognises the base class but SILENTLY DROPS any
        subclass (``DerivedProbeError``), recreating the wh-jvrs.3.1 failure
        mode for subclassed transport errors: the policy would cache a False as
        terminal_busy instead of routing prompt_detector_error. ``isinstance``
        already covers subclasses of whichever copy we imported; for the corner
        case of a subclass whose base is the OTHER (unimported-here) copy, we
        also walk the MRO and match ``ConsoleProbeError`` by name ANYWHERE in
        the chain -- not just on the leaf type (wh-jvrs.3.6).
    """
    for _module_path in (
        "ui.console_probe_client",
        "services.wheelhouse.ui.console_probe_client",
    ):
        try:
            module = __import__(_module_path, fromlist=["ConsoleProbeError"])
            console_probe_error = module.ConsoleProbeError
        except Exception:
            continue
        if isinstance(exc, console_probe_error):
            return True
    # Fallback: a subclass of a ConsoleProbeError copy that neither import above
    # resolved (e.g. a third sys.path copy, or both imports failing) is still a
    # transport error if any ancestor is named ConsoleProbeError. Matching by
    # name across the WHOLE MRO -- not only the leaf type -- is what makes this
    # recognise subclasses, closing the wh-jvrs.3.6 gap left by the old
    # leaf-name-only check.
    return any(
        base.__name__ == "ConsoleProbeError" for base in type(exc).__mro__
    )


# Backwards-compatible private alias retained for any in-tree caller that still
# imports the underscored name; the public classifier above is canonical.
_is_console_probe_error = is_console_probe_error


def _get_shared_client():
    """Return the process-wide ConsoleProbeClient, building it once."""
    global _shared_client, _atexit_registered
    if _shared_client is None:
        with _shared_client_lock:
            if _shared_client is None:
                from services.wheelhouse.ui.console_probe_client import (
                    ConsoleProbeClient,
                )
                _shared_client = ConsoleProbeClient()
                if not _atexit_registered:
                    atexit.register(_close_shared_client)
                    _atexit_registered = True
    return _shared_client


class PromptDetector:
    """Determine if a terminal's shell is waiting for input.

    Delegates the actual probe to a :class:`ConsoleProbeClient`, which owns the
    helper subprocess that performs all console attachment. The Logic process
    therefore never binds to a foreign terminal's console.
    """

    def __init__(self, *, client: Optional[object] = None) -> None:
        """Construct the detector.

        Args:
          client: an object exposing ``is_at_prompt(process_name, pid) -> bool``
            (a :class:`ConsoleProbeClient` in production, a fake in tests). When
            None, the process-wide shared client is used (lazily built on first
            use), so a fresh ``PromptDetector()`` per call still reuses one
            persistent helper subprocess and one cache.
        """
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        return _get_shared_client()

    def is_at_prompt(self, terminal_process_name: str, terminal_pid: int) -> bool:
        """Return True iff the terminal's shell is at a prompt.

        Forwards to the console-probe client. A returned bool is a genuine
        answer (``True`` at a prompt, ``False`` busy).

        A ``ConsoleProbeError`` -- the client's signal for a transport failure
        (read timeout, EOF, broken pipe, malformed response, dead helper) -- is
        deliberately PROPAGATED, not swallowed (wh-jvrs.3.1). The
        ``FocusRedirectPolicy`` runs this call inside ``asyncio.wait_for`` and
        already maps any raised exception to its ``prompt_detector_error``
        failure path, so a transient helper stall surfaces as a distinct
        timeout/error verdict instead of being cached as ``terminal_busy`` and
        suppressing the terminal-editor redirect for the whole utterance.

        Any OTHER unexpected exception still fails closed to False so a genuine
        bug never crashes the speech path and never wrongly redirects dictation
        into a busy or unknown shell.
        """
        try:
            client = self._get_client()
        except Exception:
            logger.debug(
                "PromptDetector.is_at_prompt could not get a client for "
                "pid=%s; failing closed",
                terminal_pid,
                exc_info=True,
            )
            return False
        try:
            return bool(client.is_at_prompt(terminal_process_name, terminal_pid))
        except Exception as exc:
            if is_console_probe_error(exc):
                # Transport failure (including any ConsoleProbeError SUBCLASS):
                # let the policy see it as prompt_detector_error rather than a
                # False busy verdict (wh-jvrs.3.1 / wh-jvrs.3.6).
                raise
            logger.debug(
                "PromptDetector.is_at_prompt failed for pid=%s; failing closed",
                terminal_pid,
                exc_info=True,
            )
            return False
