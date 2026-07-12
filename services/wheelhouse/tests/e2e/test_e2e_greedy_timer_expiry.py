"""E2E coverage for the greedy-pattern buffer-timer-expiry path (wh-greedy-e2e-timer-coverage).

The greedy-helper slice replaced wait_for_timeout(1100) with an explicit
utterance end marker in the greedy e2e tests, because the 5000 ms greedy
buffer timer made real waits impractical. That left the timer-expiry
path -- timer fires -> timeout-finalize sentinel -> decide_timeout ->
_resolve_finalization -- without end-to-end coverage.

These tests restore it without a 5000 ms sleep: the harness accepts a
small greedy_timeout_ms, so the REAL timer arms, expires, posts the
sentinel through the word queue, and drives finalization. `press shift
tab` is the probe because its finalization output is observable at the
OS-mock boundary (a recorded shift+tab keystroke).
"""
import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness


@pytest.fixture
async def fast_greedy_harness(pattern_catalog):
    """Harness whose greedy buffer timer expires in 150 ms instead of 5 s."""
    h = E2EPipelineHarness(catalog=pattern_catalog, greedy_timeout_ms=150)
    await h.start()
    yield h
    await h.stop()


class TestGreedyBufferTimerExpiry:
    @pytest.mark.asyncio
    async def test_timer_expiry_finalizes_greedy_command(self, fast_greedy_harness):
        """No end marker: the greedy buffer timer itself must fire, post
        the timeout-finalize sentinel, and finalize the buffered
        `press shift tab` into a real keystroke."""
        h = fast_greedy_harness
        await h.send_word("press", start_of_utterance=True)
        await h.send_word("shift", delay_before_ms=20)
        await h.send_word("tab", delay_before_ms=20)

        # 150 ms timer + queue hop + action execution; 800 ms is generous
        # without approaching the 30 s per-test cap.
        await h.wait_for_timeout(800)

        keys = h.recording.get_keystroke_keys()
        assert ("shift", "tab") in keys, (
            f"Greedy buffer timer did not finalize the command; keystrokes: {keys}"
        )

    @pytest.mark.asyncio
    async def test_stale_timer_sentinel_does_not_double_finalize(
        self, fast_greedy_harness
    ):
        """End marker first, then the armed timer expires anyway: the
        late sentinel must be dropped (stale token / IDLE mode), not
        re-finalize the buffer into a second keystroke."""
        h = fast_greedy_harness
        await h.send_word("press", start_of_utterance=True)
        await h.send_word("shift", delay_before_ms=20)
        await h.send_word("tab", delay_before_ms=20)
        await h.send_utterance_end_marker(utterance_id=1)

        # Wait long enough for the (already-armed) 150 ms timer to have
        # expired well after the end-marker finalization.
        await h.wait_for_timeout(800)

        keys = [k for k in h.recording.get_keystroke_keys() if k == ("shift", "tab")]
        assert len(keys) == 1, (
            f"Expected exactly one shift+tab (no double finalization), got {keys}"
        )
