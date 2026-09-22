"""Google declares that it applies a saved hint (wh-boost-engine-qualification).

Google STT applies hints always (config_loader has no [hotwords] gate), so
the capabilities frame says True and the Logic process lets "boost" run.
The value must be on the forwarder BEFORE start(), because start() spawns
the thread that sends the frame.
"""
from __future__ import annotations

from tests import test_capture_failure_ends_the_run as capture_harness
from tests.test_main import make_forwarder


def test_main_declares_hints_before_the_forwarder_starts():
    forwarder = make_forwarder()
    seen_at_start = []
    forwarder.start.side_effect = (
        lambda: seen_at_start.append(forwarder.applies_hints))

    capture_harness.TestMainEndsTheRun._run_main_with_a_failed_capture(
        forwarder)

    assert seen_at_start == [True]
