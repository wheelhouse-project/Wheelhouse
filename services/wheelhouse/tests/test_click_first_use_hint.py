"""Tests for the screen-reader-flag first-use discovery hint (wh-r3xy1).

Covers the hint module (``click_first_use_hint``):
  * the eligibility check reuses DEFAULT_BROWSER_PROCESS_NAMES;
  * the loader's missing-file / unparseable-file tolerance;
  * the atomic writer round-trips and is atomic (no temp residue);
  * the deleter resets the record and tolerates an absent file;
  * the suppression state machine: one-shot display, then persistence after
    three subsequent eligible clicks into ANY Chromium-family process, or an
    explicit dismiss; never fires when the flag is on or for a non-Chromium
    process (wh-9f3t.60.1).

And the Logic-side wiring (wh-9f3t.60.3): the LogicController hook
``_maybe_show_first_use_hint`` / ``_first_use_hint_tracker`` /
``_forward_first_use_hint`` puts a ``click_first_use_hint`` action on the GUI
state queue exactly once for an eligible click, not at all when the flag is on
or the process is non-browser, and the tracker is built with the ClickConfig
browser list (not the narrow default fallback).
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import click_first_use_hint as hint
from click_first_use_hint import (
    HINT_TEXT,
    SUBSEQUENT_CLICK_THRESHOLD,
    FirstUseHintTracker,
    HintDecision,
    delete_hint_record,
    is_chromium_family,
    load_hint_shown,
    mark_hint_shown,
)
from shared.rejection_category import DEFAULT_BROWSER_PROCESS_NAMES


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "click_first_use_hint_shown.toml"


# ---------------------------------------------------------------------------
# Exact wording (the v5 contract)
# ---------------------------------------------------------------------------


class TestHintWording:
    def test_exact_v5_wording_is_used_verbatim(self):
        assert HINT_TEXT == (
            "Wheelhouse can speed up clicks in this app by setting the Windows "
            "screen-reader flag. Tradeoff: PSReadLine will warn in every "
            "PowerShell session. See config.toml `[click] "
            "enable_screen_reader_flag` to opt in. Tap to dismiss."
        )


# ---------------------------------------------------------------------------
# Eligibility check -- reuses DEFAULT_BROWSER_PROCESS_NAMES
# ---------------------------------------------------------------------------


class TestIsChromiumFamily:
    def test_known_browser_is_eligible(self):
        # Take a real member of the imported list so the test stays in sync.
        member = next(iter(DEFAULT_BROWSER_PROCESS_NAMES))
        assert is_chromium_family(member) is True

    def test_match_is_case_insensitive(self):
        assert is_chromium_family("CHROME.EXE") is True

    def test_non_browser_is_not_eligible(self):
        assert is_chromium_family("notepad.exe") is False

    def test_empty_process_is_not_eligible(self):
        assert is_chromium_family("") is False

    def test_uses_imported_list_not_a_redefinition(self):
        # Every built-in name must resolve as eligible -- guards against a
        # divergent hard-coded copy inside the module.
        for name in DEFAULT_BROWSER_PROCESS_NAMES:
            assert is_chromium_family(name) is True

    def test_explicit_override_list_is_honored(self):
        assert is_chromium_family(
            "myapp.exe", browser_process_names={"myapp.exe"}
        ) is True
        assert is_chromium_family(
            "chrome.exe", browser_process_names={"myapp.exe"}
        ) is False


# ---------------------------------------------------------------------------
# Loader tolerance
# ---------------------------------------------------------------------------


class TestLoadHintShown:
    def test_missing_file_means_not_shown(self, record_path: Path):
        assert record_path.exists() is False
        assert load_hint_shown(record_path) is False

    def test_unparseable_file_treated_as_shown(self, record_path: Path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("this is = = not valid toml [[[", encoding="utf-8")
        assert load_hint_shown(record_path) is True

    def test_well_formed_shown_true_is_shown(self, record_path: Path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("shown = true\n", encoding="utf-8")
        assert load_hint_shown(record_path) is True

    def test_well_formed_without_shown_key_is_not_shown(self, record_path: Path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text('recorded_at = "2026-01-01T00:00:00+00:00"\n', encoding="utf-8")
        assert load_hint_shown(record_path) is False


# ---------------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------------


class TestMarkHintShown:
    def test_round_trips_shown_true(self, record_path: Path):
        assert mark_hint_shown(record_path) is True
        assert record_path.exists() is True
        with open(record_path, "rb") as handle:
            data = tomllib.load(handle)
        assert data["shown"] is True
        assert load_hint_shown(record_path) is True

    def test_creates_parent_directory(self, record_path: Path):
        assert record_path.parent.exists() is False
        assert mark_hint_shown(record_path) is True
        assert record_path.parent.exists() is True

    def test_no_temp_residue_after_write(self, record_path: Path):
        assert mark_hint_shown(record_path) is True
        # Atomic write must leave no ``*.tmp`` sibling behind.
        residue = list(record_path.parent.glob("*.tmp"))
        assert residue == []

    def test_overwrites_existing_record(self, record_path: Path):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("garbage", encoding="utf-8")
        assert mark_hint_shown(record_path) is True
        with open(record_path, "rb") as handle:
            data = tomllib.load(handle)
        assert data["shown"] is True


# ---------------------------------------------------------------------------
# Deleter (CLI reset support)
# ---------------------------------------------------------------------------


class TestDeleteHintRecord:
    def test_deletes_existing_record(self, record_path: Path):
        assert mark_hint_shown(record_path) is True
        assert record_path.exists() is True
        assert delete_hint_record(record_path) is True
        assert record_path.exists() is False

    def test_absent_file_is_success(self, record_path: Path):
        assert record_path.exists() is False
        assert delete_hint_record(record_path) is True

    def test_delete_failure_returns_false(self, record_path: Path, monkeypatch):
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("shown = true\n", encoding="utf-8")

        def _boom(self):
            raise PermissionError("locked")

        monkeypatch.setattr(Path, "unlink", _boom)
        assert delete_hint_record(record_path) is False


# ---------------------------------------------------------------------------
# Suppression state machine
# ---------------------------------------------------------------------------


class TestFirstUseHintTracker:
    def test_fires_on_first_chromium_click_flag_off(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=False) is True

    def test_does_not_fire_when_flag_on(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=True) is False

    def test_does_not_fire_for_non_chromium_process(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("notepad.exe", flag_enabled=False) is False

    def test_does_not_fire_when_already_recorded_on_disk(self, record_path: Path):
        # A pre-existing record means the hint is permanently suppressed.
        assert mark_hint_shown(record_path) is True
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=False) is False

    def test_dismiss_records_shown_and_suppresses(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=False) is True
        assert tracker.note_dismissed() is True
        # Recorded on disk so a fresh tracker (a restart) stays suppressed.
        assert load_hint_shown(record_path) is True
        assert tracker.note_click("chrome.exe", flag_enabled=False) is False
        fresh = FirstUseHintTracker(record_path)
        assert fresh.note_click("chrome.exe", flag_enabled=False) is False

    def test_displays_exactly_once_then_silent(self, record_path: Path):
        # wh-9f3t.60.1: one-shot display. The notice shows on the FIRST
        # eligible click and NEVER re-displays on later eligible clicks.
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=False) is True
        # Every subsequent eligible click returns False (no re-display).
        for _ in range(5):
            assert tracker.note_click("chrome.exe", flag_enabled=False) is False

    def test_persists_after_three_subsequent_clicks_same_browser(self, record_path: Path):
        # wh-9f3t.60.1: after the single display, three subsequent eligible
        # clicks (here same browser) persist "shown" to disk.
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=False) is True
        # Not yet recorded until the threshold of subsequent clicks is met.
        for _ in range(SUBSEQUENT_CLICK_THRESHOLD - 1):
            assert tracker.note_click("chrome.exe", flag_enabled=False) is False
            assert load_hint_shown(record_path) is False
        # The threshold'th subsequent click records as shown on disk.
        assert tracker.note_click("chrome.exe", flag_enabled=False) is False
        assert load_hint_shown(record_path) is True
        # A fresh tracker (a restart) stays suppressed.
        fresh = FirstUseHintTracker(record_path)
        assert fresh.note_click("chrome.exe", flag_enabled=False) is False

    def test_persists_after_three_subsequent_clicks_any_browser(self, record_path: Path):
        # wh-9f3t.60.1: the recording counter spans ANY Chromium-family
        # process, not just the first-display process. A user alternating
        # browsers reaches the persistence threshold (the old same-process
        # rule was unreachable and would fire the hint forever).
        members = sorted(DEFAULT_BROWSER_PROCESS_NAMES)
        # Need at least three distinct browser names to alternate across.
        assert len(members) >= 3
        tracker = FirstUseHintTracker(record_path)
        # First display on members[0].
        assert tracker.note_click(members[0], flag_enabled=False) is True
        # Three subsequent clicks into DIFFERENT browsers -- none re-display,
        # and the third persists "shown".
        assert tracker.note_click(members[1], flag_enabled=False) is False
        assert load_hint_shown(record_path) is False
        assert tracker.note_click(members[2], flag_enabled=False) is False
        assert load_hint_shown(record_path) is False
        assert tracker.note_click(members[1], flag_enabled=False) is False
        assert load_hint_shown(record_path) is True

    def test_non_chromium_clicks_between_do_not_advance(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.note_click("chrome.exe", flag_enabled=False) is True
        # An ineligible click in the middle returns False and does not count.
        assert tracker.note_click("notepad.exe", flag_enabled=False) is False
        # Two eligible subsequent clicks: under the threshold of 3 subsequent,
        # so not yet recorded.
        assert tracker.note_click("chrome.exe", flag_enabled=False) is False
        assert tracker.note_click("chrome.exe", flag_enabled=False) is False
        assert load_hint_shown(record_path) is False


# ---------------------------------------------------------------------------
# Decision/mutation split (wh-9f3t.61.2): evaluate is pure, commit gates display
# ---------------------------------------------------------------------------


class TestEvaluateSplit:
    def test_evaluate_is_pure_no_mutation(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        # Repeated evaluate without commit keeps returning SHOW (no mutation).
        for _ in range(3):
            assert tracker.evaluate("chrome.exe", flag_enabled=False) is HintDecision.SHOW
        assert load_hint_shown(record_path) is False

    def test_evaluate_ignore_for_flag_on_and_non_browser(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.evaluate("chrome.exe", flag_enabled=True) is HintDecision.IGNORE
        assert tracker.evaluate("notepad.exe", flag_enabled=False) is HintDecision.IGNORE

    def test_commit_then_evaluate_returns_count(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        assert tracker.evaluate("chrome.exe", flag_enabled=False) is HintDecision.SHOW
        tracker.commit_displayed()
        assert tracker.evaluate("chrome.exe", flag_enabled=False) is HintDecision.COUNT

    def test_note_counted_persists_at_threshold(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        tracker.commit_displayed()
        for _ in range(SUBSEQUENT_CLICK_THRESHOLD - 1):
            assert tracker.note_counted() is False
            assert load_hint_shown(record_path) is False
        # Success path: threshold reached AND the durable record was written.
        assert tracker.note_counted() is True
        assert load_hint_shown(record_path) is True

    def test_note_counted_returns_false_on_write_failure(self, record_path: Path, monkeypatch):
        # wh-9f3t.62.1: the return value is honest. When the durable write
        # fails at the threshold, note_counted returns False (the record is
        # NOT written) -- but the in-memory recorded_shown is still True so
        # the hint is suppressed for the session (degrade-safe).
        tracker = FirstUseHintTracker(record_path)
        tracker.commit_displayed()
        for _ in range(SUBSEQUENT_CLICK_THRESHOLD - 1):
            assert tracker.note_counted() is False

        # Make the durable writer fail on the threshold call.
        monkeypatch.setattr(hint, "mark_hint_shown", lambda _path: False)
        assert tracker.note_counted() is False
        # The durable record was never written.
        assert load_hint_shown(record_path) is False
        # But the session-local flag is set so the hint will not re-show.
        assert tracker.recorded_shown is True

    def test_commit_is_idempotent(self, record_path: Path):
        tracker = FirstUseHintTracker(record_path)
        tracker.commit_displayed()
        tracker.note_counted()  # counter at 1
        tracker.commit_displayed()  # must NOT reset the counter back to 0
        # Two more counted clicks reach the threshold of 3.
        assert tracker.note_counted() is False
        assert tracker.note_counted() is True
        assert load_hint_shown(record_path) is True


# ---------------------------------------------------------------------------
# Logic-side wiring (wh-9f3t.60.3): the LogicController hook
# ---------------------------------------------------------------------------


def _make_controller(record_path: Path, *, flag_enabled: bool,
                     browser_processes=(), browser_processes_extend=()) -> Any:
    """Build a LogicController with only the attributes the hint hook reads.

    Bypasses the heavy __init__ via object.__new__ so the test drives the REAL
    hint methods (the bound _maybe_show_first_use_hint / _first_use_hint_tracker
    / _forward_first_use_hint), not a re-implementation. The GUI state queue is
    a MagicMock so we can assert what the hook enqueues. Returns Any so the
    dynamic test-only attributes (set via setattr) do not trip the typed
    LogicController surface in pyright.
    """
    from main import LogicController

    controller: Any = object.__new__(LogicController)
    setattr(controller, "_first_use_hint", None)
    # _first_use_hint_path is the documented test-override hook read via getattr
    # in _resolve_first_use_hint_path; it is not a declared instance attribute.
    setattr(controller, "_first_use_hint_path", record_path)
    click_config = MagicMock()
    click_config.enable_screen_reader_flag = flag_enabled
    click_config.browser_processes = tuple(browser_processes)
    click_config.browser_processes_extend = tuple(browser_processes_extend)
    setattr(controller, "click_config", click_config)
    state_manager = MagicMock()
    state_manager.state_to_gui_queue = MagicMock()
    setattr(controller, "state_manager", state_manager)
    return controller


def _enqueued_actions(controller: Any) -> list:
    """Return the list of action strings the hook put on the GUI queue."""
    queue: Any = controller.state_manager.state_to_gui_queue
    return [
        call.args[0]["action"]
        for call in queue.put_nowait.call_args_list
    ]


class TestLogicHook:
    def test_eligible_click_enqueues_hint_action_once(self, record_path: Path):
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=("chrome.exe",),
        )
        controller._resolve_foreground_process_name = lambda: "chrome.exe"

        async def drive():
            # Five eligible clicks; the one-shot hint enqueues exactly once.
            for _ in range(5):
                await controller._maybe_show_first_use_hint("trace-1")

        asyncio.run(drive())

        actions = _enqueued_actions(controller)
        assert actions.count("click_first_use_hint") == 1
        # The enqueued payload carries the verbatim wording.
        first_call = controller.state_manager.state_to_gui_queue.put_nowait.call_args_list[0]
        assert first_call.args[0]["message"] == HINT_TEXT

    def test_no_hint_when_flag_enabled(self, record_path: Path):
        controller = _make_controller(
            record_path, flag_enabled=True,
            browser_processes=("chrome.exe",),
        )
        controller._resolve_foreground_process_name = lambda: "chrome.exe"

        asyncio.run(controller._maybe_show_first_use_hint("trace-2"))

        assert _enqueued_actions(controller) == []

    def test_no_hint_for_non_browser_process(self, record_path: Path):
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=("chrome.exe",),
        )
        controller._resolve_foreground_process_name = lambda: "notepad.exe"

        asyncio.run(controller._maybe_show_first_use_hint("trace-3"))

        assert _enqueued_actions(controller) == []

    def test_no_hint_when_foreground_unresolvable(self, record_path: Path):
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=("chrome.exe",),
        )
        controller._resolve_foreground_process_name = lambda: ""

        asyncio.run(controller._maybe_show_first_use_hint("trace-4"))

        assert _enqueued_actions(controller) == []

    def test_tracker_built_with_clickconfig_browser_list(self, record_path: Path):
        # Guards the wh-9f3t.60.3 regression: the tracker must be constructed
        # with the ClickConfig browser list (browser_processes +
        # browser_processes_extend), NOT the narrow DEFAULT_BROWSER_PROCESS_NAMES
        # fallback. We configure a custom browser name that is NOT in the
        # default set and assert the hint fires for it.
        custom = "mycustombrowser.exe"
        assert custom not in DEFAULT_BROWSER_PROCESS_NAMES
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=(),
            browser_processes_extend=(custom,),
        )
        controller._resolve_foreground_process_name = lambda: custom

        asyncio.run(controller._maybe_show_first_use_hint("trace-5"))

        assert _enqueued_actions(controller).count("click_first_use_hint") == 1

    def test_recorded_short_circuits_before_foreground_read(self, record_path: Path):
        # Once the record exists on disk, the hook must NOT even call the
        # (blocking) foreground resolver -- the recorded_shown short-circuit.
        assert mark_hint_shown(record_path) is True
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=("chrome.exe",),
        )
        resolver_calls = {"n": 0}

        def _resolver():
            resolver_calls["n"] += 1
            return "chrome.exe"

        controller._resolve_foreground_process_name = _resolver

        asyncio.run(controller._maybe_show_first_use_hint("trace-6"))

        assert resolver_calls["n"] == 0
        assert _enqueued_actions(controller) == []

    def test_failed_enqueue_does_not_commit_display_and_retries(self, record_path: Path):
        # wh-9f3t.61.2: if the GUI enqueue fails on the first eligible click,
        # the tracker must NOT mark displayed -- the next eligible click retries
        # the show -- and the record must NOT be written for a hint never seen.
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=("chrome.exe",),
        )
        controller._resolve_foreground_process_name = lambda: "chrome.exe"
        queue = controller.state_manager.state_to_gui_queue

        # First click: enqueue raises -> forward returns False -> no commit.
        queue.put_nowait.side_effect = RuntimeError("queue full")
        asyncio.run(controller._maybe_show_first_use_hint("trace-a"))
        # The hint was never delivered, so the durable record is absent.
        assert record_path.exists() is False
        assert load_hint_shown(record_path) is False

        # Second click: queue recovers -> the show RETRIES and is delivered.
        queue.put_nowait.side_effect = None
        queue.put_nowait.reset_mock()
        asyncio.run(controller._maybe_show_first_use_hint("trace-b"))
        assert _enqueued_actions(controller).count("click_first_use_hint") == 1

    def test_committed_display_then_counts_persist(self, record_path: Path):
        # After a successful first delivery, subsequent eligible clicks are
        # COUNT verdicts that advance the persistence counter; the third one
        # records "shown" to disk (any-browser counting, wh-9f3t.61.2).
        controller = _make_controller(
            record_path, flag_enabled=False,
            browser_processes=("chrome.exe",),
        )
        controller._resolve_foreground_process_name = lambda: "chrome.exe"

        async def drive(n):
            for _ in range(n):
                await controller._maybe_show_first_use_hint("trace-c")

        # 1 display + SUBSEQUENT_CLICK_THRESHOLD count clicks.
        asyncio.run(drive(1 + SUBSEQUENT_CLICK_THRESHOLD))
        # Exactly one notice was enqueued (one-shot).
        assert _enqueued_actions(controller).count("click_first_use_hint") == 1
        # And the record is now persisted so it never reappears.
        assert load_hint_shown(record_path) is True
