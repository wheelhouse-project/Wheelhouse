# tests/test_calibration_controller.py
"""Unit tests for the voice-calibration session state machine (wh-7ou.7.2.2).

Spec: docs/superpowers/specs/2026-08-07-voice-calibration-design.md
Sections 6.1 (state machine), 6.3 (sample validation), 8 (failure
handling). The controller is driven with fake transcript events and a
fake provider channel; no asyncio loop, no I/O.

Message contract (fixed for this build):

- GUI -> Logic actions: cal_session_open, cal_start, cal_skip_noise,
  cal_apply, cal_cancel.
- Logic -> GUI: {"action": "cal_state", "state": {...}} with the
  screen-keyed state payload.
- Logic -> provider: set_calibration_mode / apply_engine_settings.
- Provider -> Logic: engine_settings_result plus the reconnect signal.
"""
import asyncio
import queue
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from speech.calibration_controller import (
    CAL_MODE_REFRESH_S,
    CAL_WORDS,
    INACTIVITY_TIMEOUT_S,
    NOISE_SAMPLE_TOTAL,
    RESTART_SLOW_TIMEOUT_S,
    SAMPLES_PER_WORD,
    WORD_RETRY_CAP,
    CalibrationController,
    read_current_thresholds,
)


# --------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------- #

class FakeProvider:
    """Records the two provider-bound calibration commands."""

    def __init__(self):
        self.mode_calls = []
        self.apply_calls = []
        self.mode_on_error: Exception | None = None

    def set_calibration_mode(self, enabled):
        if enabled and self.mode_on_error is not None:
            raise self.mode_on_error
        self.mode_calls.append(enabled)

    def apply_engine_settings(self, settings):
        self.apply_calls.append(dict(settings))


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeScheduler:
    """Injectable stand-in for loop.call_later; tests fire timers by hand."""

    def __init__(self):
        self.timers = []

    def __call__(self, delay, callback):
        timer = FakeTimer(delay, callback)
        self.timers.append(timer)
        return timer

    def live(self, delay=None):
        return [
            t for t in self.timers
            if not t.cancelled and (delay is None or t.delay == delay)
        ]

    def fire(self, timer):
        # A real TimerHandle fires once; mark it spent before calling.
        timer.cancelled = True
        timer.callback()


def make_controller(is_distil=True, push_to_talk=False, current=(0.6, 0.03)):
    sent = []
    provider = FakeProvider()
    sched = FakeScheduler()
    flags = {"is_distil": is_distil, "push_to_talk": push_to_talk}
    controller = CalibrationController(
        send_to_gui=sent.append,
        provider=provider,
        is_distil_provider=lambda: flags["is_distil"],
        get_push_to_talk=lambda: flags["push_to_talk"],
        get_current_thresholds=lambda: current,
        schedule=sched,
    )
    return controller, sent, provider, sched, flags


def conf(prob=0.4, no_speech=0.01, word_count=1, **overrides):
    """A contract-shaped confidence block for a final."""
    block = {
        "min_word_probability": prob,
        "max_no_speech_prob": no_speech,
        "peak_avg_logprob": -0.3,
        "word_count": word_count,
        "suppressed": False,
        "rescued": False,
    }
    block.update(overrides)
    return block


def states(sent):
    return [m["state"] for m in sent if m["action"] == "cal_state"]


def last_state(sent):
    return states(sent)[-1]


# Stage drivers ------------------------------------------------------- #

def to_intro(c):
    c.handle_gui_action("cal_session_open")


def to_word(c):
    to_intro(c)
    c.handle_gui_action("cal_start")


def complete_word_stage(c, low_prob=0.198, high_no_speech=0.017,
                        prob=0.45, no_speech=0.010):
    """Feed five matching finals per word. One sample carries the lowest
    word probability and one the highest no-speech value so the spec
    Section 7 fixture numbers are reproduced."""
    for wi, word in enumerate(CAL_WORDS):
        for si in range(SAMPLES_PER_WORD):
            p = low_prob if (wi == 0 and si == 0) else prob
            ns = high_no_speech if (wi == 1 and si == 2) else no_speech
            assert c.handle_final(word, conf(prob=p, no_speech=ns))


def to_noise(c):
    to_word(c)
    complete_word_stage(c)


def complete_noise_stage(c, values=(0.044, 0.06, 0.089)):
    for v in values:
        assert c.handle_final("cough noise", conf(
            prob=0.1, no_speech=v, word_count=2,
        ))


def to_review(c):
    to_noise(c)
    complete_noise_stage(c)


def to_applying(c):
    to_review(c)
    c.handle_gui_action("cal_apply")


def to_awaiting_restart(c):
    to_applying(c)
    c.on_engine_settings_result(True, None)


def to_save_failed(c):
    to_applying(c)
    c.on_engine_settings_result(False, "disk full")


# --------------------------------------------------------------------- #
#  Session open
# --------------------------------------------------------------------- #

class TestSessionOpen:
    def test_wrong_provider_screen_when_not_distil(self):
        c, sent, provider, sched, _ = make_controller(is_distil=False)
        c.handle_gui_action("cal_session_open")
        assert last_state(sent) == {"screen": "wrong_provider"}
        # No session: nothing sent to the provider.
        assert provider.mode_calls == []
        assert not c.session_active

    def test_intro_screen_with_push_to_talk_false(self):
        c, sent, _, _, _ = make_controller()
        c.handle_gui_action("cal_session_open")
        assert last_state(sent) == {"screen": "intro", "push_to_talk": False}

    def test_intro_screen_with_push_to_talk_true(self):
        c, sent, _, _, _ = make_controller(push_to_talk=True)
        c.handle_gui_action("cal_session_open")
        assert last_state(sent) == {"screen": "intro", "push_to_talk": True}

    def test_reopen_before_start_reevaluates_provider(self):
        c, sent, _, _, flags = make_controller(is_distil=False)
        c.handle_gui_action("cal_session_open")
        assert last_state(sent)["screen"] == "wrong_provider"
        # The user switches to Distil-Whisper, then opens the window again.
        flags["is_distil"] = True
        c.handle_gui_action("cal_session_open")
        assert last_state(sent)["screen"] == "intro"

    def test_reopen_mid_session_resends_current_state(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        before = last_state(sent)
        c.handle_gui_action("cal_session_open")
        assert last_state(sent) == before

    def test_wrong_provider_cancel_returns_to_idle_silently(self):
        c, sent, provider, _, _ = make_controller(is_distil=False)
        c.handle_gui_action("cal_session_open")
        c.handle_gui_action("cal_cancel")
        assert provider.mode_calls == []
        assert not c.session_active


# --------------------------------------------------------------------- #
#  Word stage
# --------------------------------------------------------------------- #

class TestWordStage:
    def test_start_enables_calibration_mode_and_shows_first_word(self):
        c, sent, provider, _, _ = make_controller()
        to_word(c)
        assert provider.mode_calls == [True]
        assert last_state(sent) == {
            "screen": "word",
            "word": "comma",
            "word_index": 1,
            "word_total": len(CAL_WORDS),
            "sample_count": 0,
            "sample_total": SAMPLES_PER_WORD,
            "feedback": None,
        }

    def test_matching_final_advances_dot_with_got_it(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        assert c.handle_final("comma", conf())
        state = last_state(sent)
        assert state["screen"] == "word"
        assert state["sample_count"] == 1
        assert state["feedback"] == "got_it"

    def test_normalization_accepts_case_and_punctuation(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        assert c.handle_final(" Comma. ", conf())
        assert last_state(sent)["feedback"] == "got_it"

    def test_non_matching_text_gives_try_again_without_advance(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        assert c.handle_final("cough", conf())
        state = last_state(sent)
        assert state["screen"] == "word"
        assert state["word"] == "comma"
        assert state["sample_count"] == 0
        assert state["feedback"] == "try_again"

    def test_multi_word_count_gives_try_again(self):
        # Normalized text can only equal a one-word prompt when the
        # capture is one word, but the confidence word_count is the
        # contract's own check and must gate independently.
        c, sent, _, _, _ = make_controller()
        to_word(c)
        assert c.handle_final("comma", conf(word_count=2))
        assert last_state(sent)["feedback"] == "try_again"

    def test_missing_confidence_gives_try_again(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        assert c.handle_final("comma", None)
        assert last_state(sent)["feedback"] == "try_again"

    def test_five_samples_move_to_next_word(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        for _ in range(SAMPLES_PER_WORD):
            c.handle_final("comma", conf())
        state = last_state(sent)
        assert state["word"] == "period"
        assert state["word_index"] == 2
        assert state["sample_count"] == 0
        assert state["feedback"] == "got_it"

    def test_third_consecutive_miss_moves_to_next_word(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        for _ in range(WORD_RETRY_CAP):
            c.handle_final("wrong", conf())
        state = last_state(sent)
        assert state["word"] == "period"
        assert state["word_index"] == 2
        assert state["feedback"] is None

    def test_matching_capture_resets_the_miss_counter(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        c.handle_final("wrong", conf())
        c.handle_final("wrong", conf())
        c.handle_final("comma", conf())     # resets consecutive misses
        c.handle_final("wrong", conf())
        c.handle_final("wrong", conf())
        # Only two consecutive misses since the match: still on comma.
        state = last_state(sent)
        assert state["word"] == "comma"
        assert state["sample_count"] == 1

    def test_retry_cap_on_last_word_moves_to_noise(self):
        c, sent, _, _, _ = make_controller()
        to_word(c)
        complete_word_stage(c)
        # complete_word_stage already reaches noise; rebuild a session
        # that caps out on the last word instead.
        c2, sent2, _, _, _ = make_controller()
        to_word(c2)
        for word in CAL_WORDS[:-1]:
            for _ in range(SAMPLES_PER_WORD):
                c2.handle_final(word, conf())
        for _ in range(WORD_RETRY_CAP):
            c2.handle_final("wrong", conf())
        assert last_state(sent2)["screen"] == "noise"

    def test_capturing_only_during_word_and_noise(self):
        c, _, _, _, _ = make_controller()
        assert not c.capturing
        to_intro(c)
        assert not c.capturing
        c.handle_gui_action("cal_start")
        assert c.capturing
        complete_word_stage(c)
        assert c.capturing          # noise stage
        complete_noise_stage(c)
        assert not c.capturing      # review

    def test_reconnect_during_word_stage_reenables_calibration_mode(self):
        # A provider crash mid-session resets its calibration mode on
        # disconnect (spec Section 5.2); the controller re-enables it
        # when the provider comes back so the session can continue.
        c, _, provider, _, _ = make_controller()
        to_word(c)
        c.on_provider_connected()
        assert provider.mode_calls == [True, True]


class TestCalibrationModeRefresh:
    """The provider's calibration-mode bypass auto-disables 600 seconds
    after the last enable (whisper_engine._CALIBRATION_MODE_TIMEOUT_S),
    while the session allows 20 minutes of inactivity and promises no
    time limit. The controller therefore re-sends the enable on a
    5-minute cadence for as long as a capture stage is live, so the
    provider window can never lapse mid-session (review finding
    wh-7ou.7.6.4).
    """

    def test_refresh_cadence_is_half_the_provider_timeout(self):
        # whisper_engine cannot be imported from this venv; pin the
        # number so a drift on either side fails a test somewhere.
        assert CAL_MODE_REFRESH_S == 300.0

    def test_refresh_timer_armed_on_word_stage_entry(self):
        c, _, _, sched, _ = make_controller()
        to_word(c)
        assert len(sched.live(CAL_MODE_REFRESH_S)) == 1

    def test_refresh_resends_enable_and_rearms(self):
        c, _, provider, sched, _ = make_controller()
        to_word(c)
        (timer,) = sched.live(CAL_MODE_REFRESH_S)
        sched.fire(timer)
        assert provider.mode_calls == [True, True]
        assert len(sched.live(CAL_MODE_REFRESH_S)) == 1

    def test_refresh_survives_into_the_noise_stage(self):
        c, _, provider, sched, _ = make_controller()
        to_noise(c)
        (timer,) = sched.live(CAL_MODE_REFRESH_S)
        sched.fire(timer)
        assert provider.mode_calls[-1] is True
        assert len(sched.live(CAL_MODE_REFRESH_S)) == 1

    def test_refresh_stops_at_review_entry(self):
        c, _, provider, sched, _ = make_controller()
        to_review(c)
        assert sched.live(CAL_MODE_REFRESH_S) == []
        # And review turned the mode off; no later refresh may undo it.
        assert provider.mode_calls[-1] is False

    def test_refresh_stops_on_cancel(self):
        c, _, _, sched, _ = make_controller()
        to_word(c)
        c.handle_gui_action("cal_cancel")
        assert sched.live(CAL_MODE_REFRESH_S) == []

    def test_reconnect_rearms_the_refresh_cadence(self):
        # The reconnect re-enable restarts the provider's 600s window;
        # the refresh timer restarts with it so the two stay in step.
        c, _, provider, sched, _ = make_controller()
        to_word(c)
        (before,) = sched.live(CAL_MODE_REFRESH_S)
        c.on_provider_connected()
        assert provider.mode_calls == [True, True]
        assert before.cancelled
        assert len(sched.live(CAL_MODE_REFRESH_S)) == 1

    def test_refresh_failure_runs_failure_recovery(self):
        c, sent, provider, sched, _ = make_controller()
        to_word(c)
        provider.mode_on_error = RuntimeError("channel broke")
        (timer,) = sched.live(CAL_MODE_REFRESH_S)
        sched.fire(timer)
        assert not c.session_active
        assert last_state(sent) == {"screen": "cancelled"}


# --------------------------------------------------------------------- #
#  Noise stage
# --------------------------------------------------------------------- #

class TestNoiseStage:
    def test_completing_words_enters_noise(self):
        c, sent, _, _, _ = make_controller()
        to_noise(c)
        state = last_state(sent)
        assert state["screen"] == "noise"
        assert state["noise_count"] == 0
        assert state["noise_total"] == NOISE_SAMPLE_TOTAL
        # The last word's "got_it" belongs to the word stage; the noise
        # screen's "Got it!" appears per cough (spec Section 3.4), so
        # the stage must open with no feedback showing.
        assert state["feedback"] is None

    def test_any_final_counts_as_a_noise_sample(self):
        c, sent, _, _, _ = make_controller()
        to_noise(c)
        assert c.handle_final("ahem", None)     # even without confidence
        state = last_state(sent)
        assert state["noise_count"] == 1
        assert state["feedback"] == "got_it"

    def test_three_noise_samples_enter_review(self):
        c, sent, _, _, _ = make_controller()
        to_review(c)
        assert last_state(sent)["screen"] == "review"

    def test_skip_enters_review_without_noise_data(self):
        c, sent, _, _, _ = make_controller()
        to_noise(c)
        c.handle_gui_action("cal_skip_noise")
        state = last_state(sent)
        assert state["screen"] == "review"
        # Skip formula: twice the highest word no-speech value (0.017).
        assert state["details"]["proposed"][
            "single_word_max_no_speech_prob"] == 0.034

    def test_unusable_completed_noise_stage_is_not_a_skip(self):
        # Three real coughs whose measurement blocks carry no usable
        # no-speech value: the noise stage COMPLETED, so the skip
        # formula (twice the highest word value) must not run -- the
        # ceiling keeps its current value and only the floor is written
        # (review finding wh-7ou.7.6.5).
        c, sent, _, _, _ = make_controller()
        to_noise(c)
        for _ in range(NOISE_SAMPLE_TOTAL):
            assert c.handle_final("ahem", conf(
                prob=0.1, no_speech=None, word_count=2,
            ))
        state = last_state(sent)
        assert state["screen"] == "review"
        assert state["review_kind"] == "partial"
        assert "single_word_max_no_speech_prob" not in state[
            "details"]["proposed"]
        assert state["details"]["proposed"][
            "single_word_min_probability"] == 0.15

    def test_calibration_mode_turned_off_at_review_entry(self):
        c, _, provider, _, _ = make_controller()
        to_review(c)
        assert provider.mode_calls == [True, False]


# --------------------------------------------------------------------- #
#  Review and apply
# --------------------------------------------------------------------- #

class TestReviewAndApply:
    def test_full_review_reproduces_the_hand_calibration(self):
        c, sent, _, _, _ = make_controller()
        to_review(c)
        state = last_state(sent)
        assert state["review_kind"] == "full"
        details = state["details"]
        assert details["current"] == {
            "single_word_min_probability": 0.6,
            "single_word_max_no_speech_prob": 0.03,
        }
        assert details["proposed"] == {
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.03,
        }
        assert details["measured"]["word_probability_min"] == 0.198
        assert details["measured"]["noise_no_speech_min"] == 0.044

    def test_partial_when_words_and_noise_overlap(self):
        c, sent, provider, _, _ = make_controller()
        to_word(c)
        complete_word_stage(c, high_no_speech=0.04)
        complete_noise_stage(c, values=(0.05, 0.05, 0.05))  # ratio < 2
        state = last_state(sent)
        assert state["review_kind"] == "partial"
        assert "single_word_max_no_speech_prob" not in state["details"]["proposed"]
        c.handle_gui_action("cal_apply")
        # Write-or-keep: only the floor is written.
        assert provider.apply_calls == [
            {"single_word_min_probability": 0.15},
        ]

    def test_nothing_when_measurements_are_unusable(self):
        c, sent, provider, _, _ = make_controller()
        to_word(c)
        for word in CAL_WORDS:
            for _ in range(SAMPLES_PER_WORD):
                # Accepted for progression (word_count 1, text match) but
                # carrying no usable numbers.
                c.handle_final(word, conf(prob=None, no_speech=None))
        for _ in range(NOISE_SAMPLE_TOTAL):
            c.handle_final("ahem", conf(prob=None, no_speech=None,
                                        word_count=1))
        state = last_state(sent)
        assert state["screen"] == "review"
        assert state["review_kind"] == "nothing"
        # Apply is not a legal choice when there is nothing to write.
        n_states = len(states(sent))
        c.handle_gui_action("cal_apply")
        assert provider.apply_calls == []
        assert len(states(sent)) == n_states

    def test_apply_sends_settings_and_applying_screen(self):
        c, sent, provider, _, _ = make_controller()
        to_applying(c)
        assert last_state(sent) == {"screen": "applying"}
        assert provider.apply_calls == [{
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.03,
        }]

    def test_result_ok_then_reconnect_shows_applied_and_ends(self):
        c, sent, provider, _, _ = make_controller()
        to_awaiting_restart(c)
        c.on_provider_connected()
        assert last_state(sent) == {"screen": "applied"}
        assert provider.mode_calls[-1] is False
        assert not c.session_active
        # The finished session no longer consumes transcripts.
        assert c.handle_final("comma", conf()) is False

    def test_reconnect_before_result_does_not_finish(self):
        c, sent, _, _, _ = make_controller()
        to_applying(c)
        c.on_provider_connected()
        assert last_state(sent) == {"screen": "applying"}
        assert c.session_active

    def test_save_failure_returns_with_error_detail(self):
        c, sent, provider, _, _ = make_controller()
        to_save_failed(c)
        state = last_state(sent)
        assert state["screen"] == "save_failed"
        assert state["error_detail"] == "disk full"
        assert state["review_kind"] == "full"
        assert state["details"]["proposed"] == {
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.03,
        }

    def test_apply_can_be_retried_after_save_failure(self):
        c, sent, provider, _, _ = make_controller()
        to_save_failed(c)
        c.handle_gui_action("cal_apply")
        assert last_state(sent) == {"screen": "applying"}
        assert len(provider.apply_calls) == 2
        assert provider.apply_calls[0] == provider.apply_calls[1]

    def test_slow_restart_shows_taking_longer_then_applied(self):
        c, sent, provider, sched, _ = make_controller()
        to_awaiting_restart(c)
        (timer,) = sched.live(RESTART_SLOW_TIMEOUT_S)
        sched.fire(timer)
        assert last_state(sent) == {"screen": "restart_slow"}
        c.on_provider_connected()
        assert last_state(sent) == {"screen": "applied"}
        assert not c.session_active

    def test_reconnect_then_ok_result_completes_without_second_connect(self):
        # wh-7ou.7.6.6 round-4 rebuttal: the post-apply restart can
        # reconnect BEFORE the ok result frame is handled (the result
        # rides the old socket's queue). Waiting for another connect
        # after the result would strand the session -- no further
        # connect is coming -- so the result must complete it.
        c, sent, provider, _, _ = make_controller()
        to_applying(c)
        c.on_provider_connected()
        c.on_engine_settings_result(True, None)
        assert last_state(sent) == {"screen": "applied"}
        assert provider.mode_calls[-1] is False
        assert not c.session_active
        assert c.handle_final("comma", conf()) is False

    def test_reconnect_then_failed_result_still_shows_save_failed(self):
        # The connect memory must not upgrade a FAILED save into the
        # all-set screen: ok=false keeps the save_failed path.
        c, sent, _, _, _ = make_controller()
        to_applying(c)
        c.on_provider_connected()
        c.on_engine_settings_result(False, "disk full")
        state = last_state(sent)
        assert state["screen"] == "save_failed"
        assert c.session_active

    def test_connect_before_apply_does_not_short_circuit_the_wait(self):
        # A connect BEFORE the apply was even sent (a crash-recovery
        # respawn during review) is not the post-apply restart; the ok
        # result must still wait for the real reconnect.
        c, sent, _, _, _ = make_controller()
        to_review(c)
        c.on_provider_connected()
        c.handle_gui_action("cal_apply")
        c.on_engine_settings_result(True, None)
        assert last_state(sent) == {"screen": "applying"}
        assert c.session_active
        c.on_provider_connected()
        assert last_state(sent) == {"screen": "applied"}
        assert not c.session_active

    def test_retry_apply_clears_the_reconnect_memory(self):
        # A connect during a FAILED first apply must not complete the
        # retried apply early: the retry starts its own wait.
        c, sent, _, _, _ = make_controller()
        to_applying(c)
        c.on_provider_connected()
        c.on_engine_settings_result(False, "disk full")
        c.handle_gui_action("cal_apply")
        c.on_engine_settings_result(True, None)
        assert last_state(sent) == {"screen": "applying"}
        assert c.session_active


# --------------------------------------------------------------------- #
#  Cancel and timeouts
# --------------------------------------------------------------------- #

STAGE_DRIVERS = [
    ("intro", to_intro),
    ("word", to_word),
    ("noise", to_noise),
    ("review", to_review),
    ("applying", to_applying),
    ("awaiting_restart", to_awaiting_restart),
    ("save_failed", to_save_failed),
]

NON_CAPTURE_DRIVERS = [
    (stage, driver) for stage, driver in STAGE_DRIVERS
    if stage not in ("word", "noise")
]


class TestTypingGateScope:
    """The typing gate holds for the WHOLE session (spec Sections 3.7,
    6.2): on every active screen, spoken words neither type nor fire
    commands. The one allowlisted channel is a voice-click utterance
    ("click <target>"), which flows normally on every screen so all of
    the window's buttons stay hands-free -- including the capture-stage
    Cancel and Skip buttons (review finding wh-7ou.7.6.3).
    """

    @pytest.mark.parametrize(
        "stage,driver", NON_CAPTURE_DRIVERS,
        ids=[s for s, _ in NON_CAPTURE_DRIVERS],
    )
    def test_dictation_swallowed_on_every_non_capture_screen(
        self, stage, driver,
    ):
        c, sent, provider, _, _ = make_controller()
        driver(c)
        n_states = len(states(sent))
        assert c.handle_final("hello world", conf(word_count=2)) is True
        # Swallowed without a reaction: no screen change, no sample.
        assert len(states(sent)) == n_states
        assert c.session_active

    @pytest.mark.parametrize(
        "stage,driver", STAGE_DRIVERS, ids=[s for s, _ in STAGE_DRIVERS],
    )
    def test_click_utterance_flows_on_every_screen(self, stage, driver):
        c, sent, _, _, _ = make_controller()
        driver(c)
        n_states = len(states(sent))
        assert c.handle_final(
            "click cancel", conf(word_count=2),
        ) is False
        # Not consumed: no miss counted, no noise dot, no state sent.
        assert len(states(sent)) == n_states

    def test_click_utterance_normalization(self):
        # Case and punctuation must not defeat the allowlist.
        c, sent, _, _, _ = make_controller()
        to_word(c)
        assert c.handle_final(" Click Cancel. ", conf(word_count=2)) is False

    def test_bare_click_without_target_is_swallowed(self):
        # "click" alone is not a complete click command; letting it flow
        # would type the word into a focused editor.
        c, _, _, _, _ = make_controller()
        to_intro(c)
        assert c.handle_final("click", conf()) is True

    def test_clicklike_word_is_not_allowlisted(self):
        # The allowlist is the "click " prefix, not any word starting
        # with those letters.
        c, _, _, _, _ = make_controller()
        to_intro(c)
        assert c.handle_final("clicking away", conf(word_count=2)) is True

    def test_finals_flow_when_idle(self):
        c, _, _, _, _ = make_controller()
        assert c.handle_final("hello world", conf(word_count=2)) is False

    def test_finals_flow_on_wrong_provider_notice(self):
        # The notice is inert, not a session: speech must flow normally.
        c, _, _, _, _ = make_controller(is_distil=False)
        c.handle_gui_action("cal_session_open")
        assert c.handle_final("hello world", conf(word_count=2)) is False


class TestProviderStreamBinding:
    """A provider (re)connect only concerns the session when the
    configured provider is still Distil-Whisper (review finding
    wh-7ou.7.6.6): after a mid-session provider switch, the replacement
    provider's connect ENDS the session with the session-ended screen.
    Silently ignoring it left the session measuring the replacement
    provider's finals -- junk samples, and a typing gate held until the
    inactivity timeout (round-3 rebuttal).
    """

    def test_provider_switch_mid_apply_ends_the_session(self):
        c, sent, _, _, flags = make_controller()
        to_awaiting_restart(c)
        flags["is_distil"] = False
        c.on_provider_connected()
        assert not c.session_active
        assert last_state(sent) == {"screen": "cancelled"}

    def test_provider_switch_mid_word_ends_the_session_without_enable(self):
        c, sent, provider, _, flags = make_controller()
        to_word(c)
        flags["is_distil"] = False
        c.on_provider_connected()
        assert not c.session_active
        assert last_state(sent) == {"screen": "cancelled"}
        # No re-enable was sent to whatever provider just connected; the
        # session-end mode-off is the only call after the stage enable.
        assert True not in provider.mode_calls[1:]

    def test_provider_switch_releases_the_typing_gate(self):
        c, _, _, _, flags = make_controller()
        to_word(c)
        flags["is_distil"] = False
        c.on_provider_connected()
        # The replacement provider's finals flow normally: no session.
        assert c.handle_final("hello", None) is False


class TestCancelAndTimeouts:
    @pytest.mark.parametrize(
        "stage,driver", STAGE_DRIVERS, ids=[s for s, _ in STAGE_DRIVERS],
    )
    def test_cancel_turns_calibration_mode_off_at_every_stage(
        self, stage, driver,
    ):
        c, sent, provider, _, _ = make_controller()
        driver(c)
        applies_before = len(provider.apply_calls)
        c.handle_gui_action("cal_cancel")
        assert provider.mode_calls[-1] is False
        assert not c.session_active
        # Cancel never writes settings.
        assert len(provider.apply_calls) == applies_before
        # A dead session neither consumes finals nor reacts to actions.
        assert c.handle_final("comma", conf()) is False
        n_states = len(states(sent))
        c.handle_gui_action("cal_apply")
        assert len(states(sent)) == n_states

    def test_inactivity_cancels_cleanly(self):
        c, sent, provider, sched, _ = make_controller()
        to_word(c)
        (timer,) = sched.live(INACTIVITY_TIMEOUT_S)
        sched.fire(timer)
        assert provider.mode_calls[-1] is False
        assert provider.apply_calls == []
        assert not c.session_active
        # The window must not stay frozen on the word screen while the
        # typing gate silently releases: the timeout tells the GUI the
        # session ended (review finding wh-7ou.7.6.1).
        assert last_state(sent) == {"screen": "cancelled"}

    def test_failure_recovery_shows_session_ended_screen(self):
        c, sent, provider, _, _ = make_controller()
        to_word(c)
        # A reconnect during the word stage re-enables calibration mode;
        # make that raise so the controller's failure recovery runs.
        provider.mode_on_error = RuntimeError("provider channel broke")
        c.on_provider_connected()
        assert not c.session_active
        assert last_state(sent) == {"screen": "cancelled"}

    def test_a_capture_rearms_the_inactivity_timer(self):
        c, _, _, sched, _ = make_controller()
        to_word(c)
        (before,) = sched.live(INACTIVITY_TIMEOUT_S)
        c.handle_final("comma", conf())
        assert before.cancelled
        assert len(sched.live(INACTIVITY_TIMEOUT_S)) == 1

    def test_session_end_cancels_all_timers(self):
        c, _, _, sched, _ = make_controller()
        to_awaiting_restart(c)
        c.handle_gui_action("cal_cancel")
        assert sched.live() == []

    def test_actions_in_idle_are_ignored(self):
        c, sent, provider, _, _ = make_controller()
        for action in ("cal_start", "cal_skip_noise", "cal_apply",
                       "cal_cancel"):
            c.handle_gui_action(action)
        assert sent == []
        assert provider.mode_calls == []
        assert provider.apply_calls == []


# --------------------------------------------------------------------- #
#  read_current_thresholds (wiring helper)
# --------------------------------------------------------------------- #

class TestReadCurrentThresholds:
    def test_reads_engine_values_from_provider_config(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[engine]\n"
            "single_word_min_probability = 0.15\n"
            "single_word_max_no_speech_prob = 0.02\n",
            encoding="utf-8",
        )
        assert read_current_thresholds(path) == (0.15, 0.02)

    def test_missing_file_falls_back_to_shipped_defaults(self, tmp_path):
        assert read_current_thresholds(tmp_path / "absent.toml") == (0.6, 0.03)

    def test_none_path_falls_back_to_shipped_defaults(self):
        assert read_current_thresholds(None) == (0.6, 0.03)

    def test_out_of_range_value_falls_back_per_value(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[engine]\n"
            "single_word_min_probability = 7.5\n"
            "single_word_max_no_speech_prob = 0.02\n",
            encoding="utf-8",
        )
        assert read_current_thresholds(path) == (0.6, 0.02)


# --------------------------------------------------------------------- #
#  main.py wiring
# --------------------------------------------------------------------- #

class TestMainWiring:
    def _fake_logic(self):
        from main import LogicController

        fake = MagicMock(spec=LogicController)
        fake._calibration_controller = None
        fake.config_service = SimpleNamespace(
            get=lambda key, default=None: {
                "stt.mode": "remote",
                "stt.last_provider": "distil_medium_en",
                "speech.interaction_mode": "toggle",
            }.get(key, default),
        )
        fake.service_manager = SimpleNamespace(remote_stt_launcher=None)
        fake.state_manager = MagicMock()
        # The tray's read path: runtime record first, config as fallback.
        # _is_distil_provider shares it (provider-removal review .1.9).
        fake.state_manager._get_current_stt_provider.return_value = (
            "distil_medium_en"
        )
        fake.app = MagicMock()
        return fake

    def test_wrong_provider_check_prefers_the_running_record(self):
        """A repaired-at-runtime fallback to Distil must pass the check.

        After a fallback with an unrepairable config, the stored value
        still names the default while the record knows Distil runs; the
        tray already shows Distil, so the calibration check must agree
        (provider-removal review .1.9).
        """
        from main import LogicController

        fake = self._fake_logic()
        fake.config_service = SimpleNamespace(
            get=lambda key, default=None: {
                "stt.mode": "remote",
                "stt.last_provider": "parakeet_tdt",
                "speech.interaction_mode": "toggle",
            }.get(key, default),
        )
        fake.state_manager._get_current_stt_provider.return_value = (
            "distil_medium_en"
        )
        controller = LogicController._get_calibration_controller(fake)
        controller.handle_gui_action("cal_session_open")
        (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
        assert msg["state"]["screen"] == "intro"

    def test_wrong_provider_check_ignores_stale_config_value(self):
        """A config that names Distil while another engine runs must fail."""
        from main import LogicController

        fake = self._fake_logic()
        fake.state_manager._get_current_stt_provider.return_value = (
            "parakeet_tdt"
        )
        controller = LogicController._get_calibration_controller(fake)
        controller.handle_gui_action("cal_session_open")
        (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
        assert msg["state"]["screen"] == "wrong_provider"

    def test_get_calibration_controller_constructs_and_caches(self):
        from main import LogicController

        fake = self._fake_logic()
        first = LogicController._get_calibration_controller(fake)
        second = LogicController._get_calibration_controller(fake)
        assert isinstance(first, CalibrationController)
        assert first is second

    def test_session_open_reaches_the_gui_state_queue(self):
        from main import LogicController

        fake = self._fake_logic()
        controller = LogicController._get_calibration_controller(fake)
        controller.handle_gui_action("cal_session_open")
        (msg,), _ = fake.state_manager.state_to_gui_queue.put_nowait.call_args
        assert msg["action"] == "cal_state"
        assert msg["state"]["screen"] == "intro"

    def test_cal_start_sends_set_calibration_mode_to_the_provider(self):
        from main import LogicController

        fake = self._fake_logic()
        controller = LogicController._get_calibration_controller(fake)
        controller.handle_gui_action("cal_session_open")
        controller.handle_gui_action("cal_start")
        # Targeted at the active stream, never broadcast: a superseded
        # provider kept connected in DISABLED state must not receive
        # calibration commands (wh-7ou.7.6.6).
        ws = fake.app.websocket_manager
        ws.send_command_to_active_stt.assert_called_with(
            "set_calibration_mode", enabled=True,
        )
        ws.send_command_to_stt.assert_not_called()
        assert fake.create_task_with_error_handling.called

    def test_apply_send_failure_reports_save_failed(self):
        """The apply command is the one send whose silent loss strands
        the window on the restarting screen with the typing gate held
        (review finding wh-7ou.7.6.7): a send that never left this
        process must come back as an ok=false settings result."""
        from main import LogicController

        fake = self._fake_logic()
        controller = LogicController._get_calibration_controller(fake)
        controller.on_engine_settings_result = MagicMock()
        ws = fake.app.websocket_manager
        ws.send_command_to_active_stt = AsyncMock(return_value=False)

        controller._provider.apply_engine_settings(
            {"single_word_min_probability": 0.15},
        )
        (coro, _name), _ = fake.create_task_with_error_handling.call_args
        asyncio.run(coro)

        controller.on_engine_settings_result.assert_called_once()
        (ok, error), _ = controller.on_engine_settings_result.call_args
        assert ok is False
        assert "engine" in error

    def test_apply_send_success_reports_nothing(self):
        """The provider's own engine_settings_result is the only reply
        on the success path; the channel must not fabricate a second."""
        from main import LogicController

        fake = self._fake_logic()
        controller = LogicController._get_calibration_controller(fake)
        controller.on_engine_settings_result = MagicMock()
        ws = fake.app.websocket_manager
        ws.send_command_to_active_stt = AsyncMock(return_value=True)

        controller._provider.apply_engine_settings(
            {"single_word_min_probability": 0.15},
        )
        (coro, _name), _ = fake.create_task_with_error_handling.call_args
        asyncio.run(coro)

        controller.on_engine_settings_result.assert_not_called()

    def test_apply_with_no_ws_manager_reports_save_failed(self):
        """No WebSocket manager at all (early startup, teardown): the
        apply must fail fast to the save-failed screen, not strand the
        restarting screen."""
        from main import LogicController

        fake = self._fake_logic()
        controller = LogicController._get_calibration_controller(fake)
        controller.on_engine_settings_result = MagicMock()
        fake.app.websocket_manager = None

        controller._provider.apply_engine_settings(
            {"single_word_min_probability": 0.15},
        )

        controller.on_engine_settings_result.assert_called_once()
        (ok, _error), _ = controller.on_engine_settings_result.call_args
        assert ok is False

    def test_handle_calibration_action_routes_to_the_controller(self):
        from main import LogicController

        fake = self._fake_logic()
        controller = MagicMock()
        fake._get_calibration_controller = MagicMock(return_value=controller)
        LogicController._handle_calibration_action(fake, "cal_apply")
        controller.handle_gui_action.assert_called_once_with("cal_apply")

    async def test_gui_listener_routes_cal_actions(self):
        from main import LogicController

        fake = MagicMock(spec=LogicController)
        shutdown = threading.Event()
        fake.shutdown_event = shutdown
        fake._handle_calibration_action = MagicMock(
            side_effect=lambda action: shutdown.set(),
        )
        # If the cal_ branch is missing, the dispatcher path runs instead;
        # make it stop the loop too so a red run fails fast instead of
        # hanging until the pytest timeout.
        fake._build_gui_handler_map = MagicMock(
            side_effect=lambda command: (shutdown.set(), {})[1],
        )
        q = queue.Queue()
        q.put({"action": "cal_session_open"})
        await LogicController._listen_for_gui_commands(fake, q)
        fake._handle_calibration_action.assert_called_once_with(
            "cal_session_open",
        )
