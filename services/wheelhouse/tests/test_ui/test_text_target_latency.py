"""Latency baseline harness for the text-target predicate (wh-9weum, wh-ka5b).

Captures per-call wall-clock latency for ``TextTargetPredicate.evaluate``
against representative mock controls. The baseline is written to
``baselines/text_target_latency.json`` next to this file.

Phase 5 (wh-mm39e) re-runs this harness on the implementation and
compares the per-scenario averages against the baseline. The Phase 5
task adds the comparison + threshold logic; this Phase 1 harness only
captures the numbers.

How the harness runs:

  - The default pytest invocation runs ``test_baseline_runs_without_error``
    which exercises every scenario once with a tiny iteration count
    just to confirm the harness works. It does NOT measure or compare.
  - The full latency capture is gated on the env var
    ``WHEELHOUSE_LATENCY_BASELINE=1``. With the var set, the test runs
    each scenario for ``_ITERATIONS_PER_SCENARIO`` iterations, computes
    the average, and rewrites the baseline JSON. This keeps wall-clock
    measurements out of the routine ``uv run pytest`` flow (where
    a timing flake would noise the suite) while leaving the harness
    callable when a baseline is intentionally being captured.

The harness measures ``evaluate`` calls against fixture controls only;
no real UIA initialization, no real COM, no GUI. ``GetPattern`` is
mocked to return a constant. The baseline is therefore a measurement of
the predicate's branching cost, not of UIA round-trip cost. That is the
right thing to capture, because Phase 1's risk is that the new
EditControl / browser-trap / soft-reject branches slow the existing
accept and reject paths.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import uiautomation as auto

from ui.text_target import TextTargetPredicate


_BASELINE_DIR = Path(__file__).parent / "baselines"
_BASELINE_FILE = _BASELINE_DIR / "text_target_latency.json"

# Iteration counts. The env-gated capture path is what writes the
# baseline; the default smoke path runs each scenario once.
_ITERATIONS_PER_SCENARIO = 200
_SMOKE_ITERATIONS = 1

# Warmup iterations run before the real measurement on the capture
# path. The first iteration of each scenario pays a one-time Python
# cost (attribute caches, mock object setup) that, without a warmup
# pass, shows up as an 8-millisecond max on text_pattern_accept and
# pushes the per-scenario mean far above the steady-state value. A
# 50-iteration warmup brings the captured means within 3% across
# repeated runs, which is what the wh-mm39e (performance check on
# the predicate accept path) comparison needs to be meaningful.
_WARMUP_ITERATIONS = 50


def _ctrl(*, control_type=auto.ControlType.EditControl,
          control_type_name="EditControl",
          class_name="Edit",
          has_text_pattern=True,
          has_value_pattern=True,
          is_focusable=True,
          is_enabled=True):
    """Build a mock UIA control for predicate timing.

    Mirrors the helper in test_text_target.py so timings reflect the
    same fixture shape the unit tests already cover.
    """
    ctrl = MagicMock()
    ctrl.ControlType = int(control_type)
    ctrl.ControlTypeName = control_type_name
    ctrl.ClassName = class_name
    ctrl.IsKeyboardFocusable = is_focusable
    ctrl.IsEnabled = is_enabled

    def get_pattern(pid):
        if pid == auto.PatternId.TextPattern and has_text_pattern:
            return MagicMock(name="TextPattern")
        if pid == auto.PatternId.ValuePattern and has_value_pattern:
            return MagicMock(name="ValuePattern")
        return None

    ctrl.GetPattern.side_effect = get_pattern
    return ctrl


def _scenarios():
    """Each entry: (name, control factory, evaluate kwargs).

    Names map to the routing branches Phase 1 cares about: the existing
    text_pattern_available accept, the new edit_control accept, the new
    soft default_reject_paste_capable_class, the new browser-empty-trap
    hard reject, the existing default_reject, and a denylist hit.
    """
    return [
        ("text_pattern_accept", lambda: _ctrl(
            control_type=auto.ControlType.EditControl,
            class_name="Edit", has_text_pattern=True,
        ), {"class_name": "Edit", "process_name": "notepad.exe"}),

        ("edit_control_accept", lambda: _ctrl(
            control_type=auto.ControlType.EditControl,
            class_name="CustomEdit", has_text_pattern=False,
            has_value_pattern=False,
        ), {"class_name": "CustomEdit", "process_name": "myapp.exe"}),

        ("soft_reject_paste_capable_class", lambda: _ctrl(
            control_type=auto.ControlType.PaneControl,
            control_type_name="PaneControl",
            class_name="ZedTextField", has_text_pattern=False,
            has_value_pattern=False,
        ), {"class_name": "ZedTextField", "process_name": "zed.exe"}),

        ("browser_empty_class_trap", lambda: _ctrl(
            control_type=auto.ControlType.DocumentControl,
            control_type_name="DocumentControl",
            class_name="", has_text_pattern=False,
            has_value_pattern=False,
        ), {"class_name": "", "process_name": "brave.exe"}),

        # The EditControl exemption from the browser-empty-ClassName trap.
        # Lands on rule 7 (text_pattern_available accept) because TextPattern
        # is present. Captures the cost of the exemption clause itself plus
        # the TextPattern probe that follows. If the wh-aria-textbox-spoof-
        # mitigation follow-up later adds a TextEditPattern.IsTextEditable
        # probe to this path, the regression will show against this baseline.
        ("browser_edit_control_exemption_accept", lambda: _ctrl(
            control_type=auto.ControlType.EditControl,
            control_type_name="EditControl",
            class_name="", has_text_pattern=True,
        ), {"class_name": "", "process_name": "brave.exe"}),

        ("default_reject_empty_class_non_browser", lambda: _ctrl(
            control_type=auto.ControlType.PaneControl,
            control_type_name="PaneControl",
            class_name="", has_text_pattern=False,
            has_value_pattern=False,
        ), {"class_name": "", "process_name": "myapp.exe"}),

        ("denylist_control_type", lambda: _ctrl(
            control_type=auto.ControlType.MenuItemControl,
            control_type_name="MenuItemControl",
            class_name="MenuItem", has_text_pattern=False,
        ), {"class_name": "MenuItem", "process_name": "notepad.exe"}),
    ]


def _measure(predicate, iterations):
    """Run every scenario for ``iterations`` and return per-scenario stats.

    Returns a dict of {scenario_name: {iterations, mean_us, stdev_us,
    min_us, max_us}}. Microsecond resolution is enough -- predicate calls
    are typically tens of microseconds without UIA round-trips.
    """
    results: dict = {}
    for name, factory, kwargs in _scenarios():
        timings = []
        for _ in range(iterations):
            ctrl = factory()
            t0 = time.perf_counter()
            predicate.evaluate(ctrl, **kwargs)
            t1 = time.perf_counter()
            timings.append((t1 - t0) * 1_000_000)
        results[name] = {
            "iterations": iterations,
            "mean_us": statistics.fmean(timings),
            "stdev_us": (
                statistics.pstdev(timings) if len(timings) > 1 else 0.0
            ),
            "min_us": min(timings),
            "max_us": max(timings),
        }
    return results


def test_baseline_runs_without_error():
    """Smoke test: every scenario must evaluate cleanly.

    Catches harness drift (e.g. a fixture that no longer matches the
    predicate's expected shape) without depending on wall-clock timing.
    Always runs in the regular pytest flow.
    """
    predicate = TextTargetPredicate()
    results = _measure(predicate, _SMOKE_ITERATIONS)
    expected = {name for name, _, _ in _scenarios()}
    assert set(results.keys()) == expected
    for name, stats in results.items():
        assert stats["iterations"] == _SMOKE_ITERATIONS
        assert stats["mean_us"] >= 0


@pytest.mark.skipif(
    os.environ.get("WHEELHOUSE_LATENCY_BASELINE") != "1",
    reason=(
        "Run with WHEELHOUSE_LATENCY_BASELINE=1 to (re)capture the "
        "predicate latency baseline. Skipped in routine pytest runs to "
        "avoid wall-clock flakiness."
    ),
)
def test_capture_baseline_writes_json():
    predicate = TextTargetPredicate()
    # Warmup pass: results discarded. Pays the first-iteration cost on
    # every scenario before the real measurement starts so the per-call
    # max no longer carries the cold-call outlier.
    _measure(predicate, _WARMUP_ITERATIONS)
    results = _measure(predicate, _ITERATIONS_PER_SCENARIO)
    _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "iterations_per_scenario": _ITERATIONS_PER_SCENARIO,
        "warmup_iterations_per_scenario": _WARMUP_ITERATIONS,
        "captured_phase": "wh-9weum Phase 1",
        "scenarios": results,
    }
    _BASELINE_FILE.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    # Sanity check: every scenario produced a finite mean.
    for scenario_name, stats in results.items():
        assert stats["mean_us"] > 0, scenario_name
