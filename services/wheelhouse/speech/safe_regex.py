r"""Bounded regex matching in a disposable worker process (wh-pattern-editor-r0.4).

Python's ``re`` has no timeout: a nested-quantifier expression like
``^(\w+\s*)+$`` compiles fine and then takes exponential time to fail a
match. Run inside the Logic asyncio loop, one such pattern kills the speech
pipeline -- and a hands-free user cannot recover. The only way to stop a
runaway ``re`` match in CPython is to kill the process running it, so every
untrusted match here runs in a single-worker multiprocessing pool that is
terminated on timeout and recreated on the next call.

Spawn-safety rules (Windows uses the ``spawn`` start method): the pool is
created lazily on first use, never at import time -- the spawned child
re-imports this module, and an import-time pool would fork-bomb. The worker
function sits at module top level with picklable scalar arguments so the
child can locate and call it.
"""
import atexit
import logging
import multiprocessing
import multiprocessing.pool
import re
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Generous bound for pool warm-up: on Windows the spawned child takes a few
# hundred ms to start and import. The warm-up ping pays that cost at pool
# creation so it is never charged against a caller's short match timeout
# (which would misreport a healthy pattern as pathological on first use).
_WARMUP_TIMEOUT = 30.0

_pool: Optional[multiprocessing.pool.Pool] = None
_atexit_registered = False

# Guards every _pool transition (create, discard, shutdown). Today's only
# caller is the Logic asyncio loop (single-threaded), but the module is
# importable from anywhere and a check-then-act race on first use would
# leak a worker process (wh-pattern-editor-r1.2). Reentrant because
# match_bounded holds it across a call that also enters _get_pool.
_lock = threading.RLock()


class RegexTimeout(Exception):
    """A bounded regex match exceeded its time budget."""


def _run_match(
    pattern: str, text: str, flags: int, mode: str,
) -> Optional[dict[str, Any]]:
    """Compile and run one match in the worker process.

    Top-level by design: the spawn start method pickles the function by
    reference and the child imports this module to find it.
    """
    compiled = re.compile(pattern, flags)
    if mode == "fullmatch":
        match = compiled.fullmatch(text)
    else:
        match = compiled.search(text)
    if match is None:
        return None
    return {"groups": match.groups(), "groupdict": match.groupdict()}


def _get_pool() -> multiprocessing.pool.Pool:
    """Return the single-worker pool, creating and warming it on first use."""
    global _pool, _atexit_registered
    with _lock:
        if _pool is None:
            pool = multiprocessing.get_context("spawn").Pool(1)
            try:
                pool.apply_async(_run_match, ("x", "x", 0, "search")).get(
                    _WARMUP_TIMEOUT,
                )
            except Exception:
                # A failed warm-up must not leak the just-created worker
                # process; _pool stays unset so a later call can try
                # again (wh-pattern-editor-r1.1).
                pool.terminate()
                pool.join()
                raise
            _pool = pool
            if not _atexit_registered:
                # Terminate the pool before interpreter teardown; a live
                # Pool's __del__ during shutdown raises ignored
                # AttributeErrors once the pickle machinery is gone.
                atexit.register(shutdown)
                _atexit_registered = True
        return _pool


def match_bounded(
    pattern: str,
    text: str,
    flags: int = 0,
    timeout: float = 0.25,
    mode: str = "search",
) -> Optional[dict[str, Any]]:
    """Match ``pattern`` against ``text`` in the worker, bounded by ``timeout``.

    Args:
        pattern: Regex source string (compiled in the worker).
        text: Text to match against.
        flags: ``re`` flags for the worker's compile (e.g. ``re.IGNORECASE``).
        timeout: Seconds to wait before declaring the match runaway.
        mode: ``"search"`` or ``"fullmatch"`` -- mirrors the runtime's
            anchor-driven strategy split (commands fullmatch, replacements
            search).

    Returns:
        None on no-match, else a picklable
        ``{"groups": <tuple of positional groups>,
        "groupdict": <dict of named groups>}``.

    Raises:
        RegexTimeout: The match exceeded ``timeout``. The worker process is
            terminated -- the only way to stop a runaway ``re`` match in
            CPython -- and the pool is discarded so the next call recreates
            it.
    """
    global _pool
    with _lock:
        pool = _get_pool()
        result = pool.apply_async(_run_match, (pattern, text, flags, mode))
        try:
            return result.get(timeout)
        except multiprocessing.TimeoutError:
            logger.warning(
                "Bounded regex match exceeded %.2fs; terminating worker "
                "(pattern %r)", timeout, pattern,
            )
            pool.terminate()
            pool.join()
            _pool = None
            raise RegexTimeout(
                f"Regex match exceeded {timeout}s and was terminated"
            ) from None
        except Exception:
            # Anything else -- a worker killed by the OS, broken pool
            # machinery, or an exception raised inside the worker function
            # itself -- discards the pool too. Keeping it could pin a
            # broken pool forever (every later call would fail until the
            # Logic process restarts); discarding a healthy one merely
            # costs a respawn on the next call (wh-pattern-editor-r1.3).
            logger.exception(
                "Bounded regex worker failed (pattern %r); discarding pool",
                pattern,
            )
            pool.terminate()
            pool.join()
            _pool = None
            raise


def shutdown() -> None:
    """Terminate the pool if it exists (test hygiene / process shutdown)."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.terminate()
        pool.join()
