# tests/test_safe_regex.py
r"""Tests for bounded regex matching in a worker process (wh-pattern-editor-r0.4).

Python's ``re`` has no timeout: a nested-quantifier expression like
``^(\w+\s*)+$`` takes exponential time, and matched inside the Logic asyncio
loop it would freeze the speech pipeline with no hands-free recovery.
``speech.safe_regex`` runs every untrusted match in a disposable
single-worker pool: on timeout the worker process is terminated (the only
way to stop a runaway ``re`` match in CPython) and the pool is recreated on
the next call.

Process spawn on Windows costs a few hundred ms on first use; the pool is
warmed at creation so that cost is never charged against a caller's match
timeout. The pathological tests are deliberately few (each pays a timeout
plus a pool respawn) to keep the suite fast.
"""
import re
import time

import pytest

from speech import safe_regex
from speech.safe_regex import RegexTimeout, match_bounded


PATHOLOGICAL = r"^(\w+\s*)+$"
PATHOLOGICAL_TEXT = "a" * 30 + "!"

# Generous budget for matches that must SUCCEED. These tests verify pool
# lifecycle and result contents, not the production latency budget: on a
# loaded shared CI runner a healthy worker round-trip can exceed the
# 0.25 s production default (public CI run 29591822010 failed exactly
# this way), and a spurious RegexTimeout turns a lifecycle test into a
# flake. Timeout behavior itself is covered by TestTimeout, whose
# pathological matches keep the tight default on purpose.
HEALTHY_TIMEOUT = 5.0


@pytest.fixture(scope="module", autouse=True)
def _shutdown_pool_after_module():
    yield
    safe_regex.shutdown()


class TestMatchBounded:
    def test_match_round_trips_groups_and_groupdict(self):
        result = match_bounded(
            r"^(\w+) (?P<rest>.+)$", "hello there world",
            timeout=HEALTHY_TIMEOUT,
        )
        assert result is not None
        assert result["groups"] == ("hello", "there world")
        assert result["groupdict"] == {"rest": "there world"}

    def test_no_match_returns_none(self):
        assert match_bounded(r"^save$", "deploy", timeout=HEALTHY_TIMEOUT) is None

    def test_flags_are_applied(self):
        assert match_bounded(r"^save$", "SAVE", timeout=HEALTHY_TIMEOUT) is None
        assert match_bounded(
            r"^save$", "SAVE", flags=re.IGNORECASE,
            timeout=HEALTHY_TIMEOUT,
        ) is not None

    def test_fullmatch_mode(self):
        # search finds the embedded word; fullmatch requires the whole text.
        assert match_bounded(
            r"save", "please save it", timeout=HEALTHY_TIMEOUT,
        ) is not None
        assert match_bounded(
            r"save", "please save it", mode="fullmatch",
            timeout=HEALTHY_TIMEOUT,
        ) is None
        assert match_bounded(
            r"save", "save", mode="fullmatch", timeout=HEALTHY_TIMEOUT,
        ) is not None

    def test_unfilled_group_round_trips_as_none(self):
        result = match_bounded(
            r"^undo\s*(\d+)?$", "undo", mode="fullmatch",
            timeout=HEALTHY_TIMEOUT,
        )
        assert result is not None
        assert result["groups"] == (None,)


class TestTimeout:
    def test_pathological_pattern_raises_regex_timeout_quickly(self):
        match_bounded(  # pool spawn paid outside the timing
            r"^warm$", "warm", timeout=HEALTHY_TIMEOUT,
        )
        start = time.monotonic()
        with pytest.raises(RegexTimeout):
            match_bounded(PATHOLOGICAL, PATHOLOGICAL_TEXT, mode="fullmatch")
        assert time.monotonic() - start < 1.5

    def test_pool_recovers_after_timeout(self):
        with pytest.raises(RegexTimeout):
            match_bounded(PATHOLOGICAL, PATHOLOGICAL_TEXT, mode="fullmatch")
        # The terminated pool was discarded; the next call recreates it.
        result = match_bounded(
            r"^(\w+)$", "recovered", mode="fullmatch",
            timeout=HEALTHY_TIMEOUT,
        )
        assert result is not None
        assert result["groups"] == ("recovered",)


class TestFailureRecovery:
    """Pool lifecycle under failures (wh-pattern-editor-r1.1/.2/.3)."""

    def test_warmup_failure_terminates_the_new_pool(self, monkeypatch):
        # r1.1: a warm-up that fails must not leak the just-created
        # worker process, and _pool must stay unset so a later call can
        # try again.
        safe_regex.shutdown()

        class FakeResult:
            def get(self, timeout):
                raise multiprocessing_TimeoutError()

        class FakePool:
            def __init__(self):
                self.terminated = False
                self.joined = False

            def apply_async(self, fn, args):
                return FakeResult()

            def terminate(self):
                self.terminated = True

            def join(self):
                self.joined = True

        class FakeContext:
            def __init__(self):
                self.pools = []

            def Pool(self, n):
                pool = FakePool()
                self.pools.append(pool)
                return pool

        import multiprocessing
        multiprocessing_TimeoutError = multiprocessing.TimeoutError
        ctx = FakeContext()
        monkeypatch.setattr(
            safe_regex.multiprocessing, "get_context", lambda method: ctx,
        )
        with pytest.raises(multiprocessing.TimeoutError):
            match_bounded(r"^x$", "x")
        assert len(ctx.pools) == 1
        assert ctx.pools[0].terminated
        assert ctx.pools[0].joined
        assert safe_regex._pool is None

    def test_worker_exception_discards_pool_and_next_call_works(self):
        # r1.3: any non-timeout failure from the worker (here: a pattern
        # that does not compile, raising re.error through the result)
        # must discard the pool instead of leaving _pool referencing a
        # possibly broken one; the next call recreates it and works.
        safe_regex.shutdown()
        with pytest.raises(re.error):
            match_bounded(r"(", "text")
        assert safe_regex._pool is None
        result = match_bounded(
            r"^(\w+)$", "healed", mode="fullmatch",
            timeout=HEALTHY_TIMEOUT,
        )
        assert result is not None
        assert result["groups"] == ("healed",)

    def test_concurrent_first_use_creates_one_pool(self):
        # r1.2: two threads racing on first use must not each create a
        # pool (one would leak). The lock serializes creation.
        from concurrent.futures import ThreadPoolExecutor

        safe_regex.shutdown()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda text: match_bounded(
                    r"^(\w+)$", text, mode="fullmatch",
                    timeout=HEALTHY_TIMEOUT,
                ),
                ["alpha", "beta"],
            ))
        assert all(r is not None for r in results)
        assert {r["groups"][0] for r in results} == {"alpha", "beta"}
