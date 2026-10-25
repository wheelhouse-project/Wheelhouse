"""LeadWordController: the Logic-process side of 'Your training word'.

Acceptance items of wh-first-sound-lost.23 covered here: 10 (request IDs, the
window ignores a result for another request), 13 (key_path reaches the
window), 16 to 18 (off, on, go back), 20 (status when the window opens),
21 (Record, refusal, candidate), 22 (playback hold), 26 (close during a
recording).
"""
import asyncio
import logging
import re

import pytest

import speech.lead_word_controller as lwc
from speech.lead_word_controller import (
    PLAYBACK_MAX_MS,
    PLAYBACK_TAIL_S,
    RESPONSE_TIMEOUT_S,
    SCREEN_CHECKING,
    SCREEN_IDLE,
    SCREEN_LISTENING,
    SCREEN_LOADING,
    SCREEN_PASSED,
    SCREEN_REFUSED,
    LeadWordController,
)


class FakeProvider:
    def __init__(self):
        self.sent = []  # (command, request_id)
        self.send_result = True
        self.events = []  # ordered log of hold / release / gui messages
        self.pending_hold = None
        self.hold_immediately = True

    def send_command(self, command, request_id):
        self.sent.append((command, request_id))
        return self.send_result

    def hold_transcription(self, done):
        self.events.append("hold")
        if self.hold_immediately:
            done()
        else:
            self.pending_hold = done

    def release_transcription(self):
        self.events.append("release")


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert not self.cancelled
        self.callback()


class Harness:
    def __init__(self, supported=True):
        self.gui = []
        self.provider = FakeProvider()
        self.timers = []
        self._n = 0

        def schedule(delay, callback):
            timer = FakeTimer(delay, callback)
            self.timers.append(timer)
            return timer

        def new_id():
            self._n += 1
            return f"r{self._n}"

        def to_gui(message):
            self.gui.append(message)
            self.provider.events.append(message["action"])

        self.controller = LeadWordController(
            send_to_gui=to_gui,
            provider=self.provider,
            schedule=schedule,
            new_request_id=new_id,
        )
        self.controller.set_supported(supported)

    @property
    def state(self):
        states = [m for m in self.gui if m["action"] == "lw_state"]
        return states[-1]["state"]

    @property
    def last_id(self):
        return self.provider.sent[-1][1]

    def live_timers(self):
        return [t for t in self.timers if not t.cancelled]

    def act(self, action, **command):
        self.controller.handle_gui_action(action, command)

    def result(self, stage, request_id=None, **fields):
        data = {"type": "lead_word_result", "stage": stage,
                "request_id": request_id or self.last_id,
                "reason": "", "measures": {}}
        data.update(fields)
        self.controller.on_result(data)

    def open_with_status(self, **info):
        self.act("lw_open")
        base = {"loaded": False, "recorded": "", "microphone": "",
                "previous_exists": False, "key_path": ""}
        base.update(info)
        self.result("status", **base)


STATUS_WITH_RECORDING = dict(
    loaded=True, recorded="2026-10-22 15:37", microphone="Desk mic",
    previous_exists=False, key_path=r"C:\L\hello.wav",
)
PASSED_MEASURES = {"voiced_ms": 620, "level_dbfs": -13.9, "decode": "hello"}


def test_opening_the_window_asks_the_provider_for_its_status():
    h = Harness()
    h.act("lw_open")
    assert h.provider.sent == [("lead_word_status", "r1")]
    assert h.state["screen"] == SCREEN_LOADING
    assert h.state["supported"] is True


def test_opening_with_a_provider_that_lacks_the_feature_sends_nothing():
    h = Harness(supported=False)
    h.act("lw_open")
    assert h.provider.sent == []
    assert h.state["supported"] is False


def test_status_result_fills_the_window_state():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    state = h.state
    assert state["screen"] == SCREEN_IDLE
    assert state["loaded"] is True
    assert state["recorded"] == "2026-10-22 15:37"
    assert state["microphone"] == "Desk mic"
    assert state["previous_exists"] is False
    assert state["key_path"] == r"C:\L\hello.wav"
    assert state["busy"] is False


def test_a_result_with_another_request_id_is_ignored():
    h = Harness()
    h.act("lw_open")
    before = len(h.gui)
    h.result("status", request_id="not-the-current-one", loaded=True,
             recorded="x", microphone="", previous_exists=False, key_path="")
    assert len(h.gui) == before
    assert h.state["screen"] == SCREEN_LOADING
    h.result("status", loaded=True, recorded="x", microphone="",
             previous_exists=False, key_path="")
    assert h.state["screen"] == SCREEN_IDLE


def test_an_earlier_request_is_not_current_after_a_newer_command():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    record_id = h.last_id
    h.act("lw_discard")
    h.result("passed", request_id=record_id, measures=PASSED_MEASURES,
             key_path="")
    assert h.state["screen"] != SCREEN_PASSED
    assert h.state["candidate"] is False


def test_each_command_gets_a_new_request_id():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    ids = [rid for _cmd, rid in h.provider.sent]
    assert len(set(ids)) == len(ids) == 2


def test_record_goes_listening_then_checking_then_passed():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    assert h.provider.sent[-1][0] == "lead_word_record"
    assert h.state["screen"] == SCREEN_LISTENING
    h.result("listening")
    assert h.state["screen"] == SCREEN_LISTENING
    h.result("checking")
    assert h.state["screen"] == SCREEN_CHECKING
    h.result("passed", measures=PASSED_MEASURES, key_path=r"C:\L\other.wav")
    state = h.state
    assert state["screen"] == SCREEN_PASSED
    assert state["candidate"] is True
    assert state["measures"] == PASSED_MEASURES
    assert state["key_path"] == r"C:\L\other.wav"
    # The status fields from before the recording stay.
    assert state["recorded"] == "2026-10-22 15:37"
    assert state["loaded"] is True


def test_a_second_record_press_during_a_recording_sends_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.act("lw_record")
    assert [c for c, _ in h.provider.sent].count("lead_word_record") == 1
    h.result("listening")
    h.act("lw_record")
    assert [c for c, _ in h.provider.sent].count("lead_word_record") == 1


def test_refused_result_carries_the_reason_and_no_candidate():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("refused", reason="No speech heard", measures={"capture_ms": 10000})
    state = h.state
    assert state["screen"] == SCREEN_REFUSED
    assert state["reason"] == "No speech heard"
    assert state["candidate"] is False
    assert state["measures"] == {"capture_ms": 10000}


def test_keep_sends_keep_and_the_kept_result_updates_the_state():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_keep")
    assert h.provider.sent[-1][0] == "lead_word_keep"
    assert h.state["busy"] is True
    h.result("kept", loaded=True, recorded="2026-10-22 16:00",
             microphone="Desk mic", previous_exists=False)
    state = h.state
    assert state["screen"] == SCREEN_IDLE
    assert state["candidate"] is False
    assert state["recorded"] == "2026-10-22 16:00"
    assert state["loaded"] is True
    assert state["notice"] == "kept"
    assert state["busy"] is False


def test_keep_without_a_candidate_sends_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_keep")
    assert [c for c, _ in h.provider.sent] == ["lead_word_status"]


def test_failed_keep_shows_the_error_and_keeps_the_candidate():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_keep")
    h.result("failed", reason="Disk full")
    state = h.state
    assert state["screen"] == SCREEN_PASSED
    assert state["candidate"] is True
    assert state["error"] == "Disk full"
    assert state["busy"] is False


def test_discard_sends_discard_and_the_status_reply_clears_the_candidate():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_discard")
    assert h.provider.sent[-1][0] == "lead_word_discard"
    assert h.state["candidate"] is False
    assert h.state["screen"] == SCREEN_IDLE
    h.result("status", loaded=False, recorded="", microphone="",
             previous_exists=False, key_path="")
    assert h.state["busy"] is False


@pytest.mark.parametrize(
    "action,command,stage",
    [
        ("lw_restore", "lead_word_restore", "restored"),
        ("lw_off", "lead_word_off", "off"),
        ("lw_on", "lead_word_on", "on"),
    ],
)
def test_restore_off_and_on_send_their_command_and_take_the_new_state(
    action, command, stage
):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act(action)
    assert h.provider.sent[-1][0] == command
    h.result(stage, loaded=(stage != "off"), recorded="2026-10-21 09:00",
             microphone="Old mic", previous_exists=(stage != "restored"))
    state = h.state
    assert state["loaded"] is (stage != "off")
    assert state["recorded"] == "2026-10-21 09:00"
    assert state["previous_exists"] is (stage != "restored")
    assert state["notice"] == stage


def test_a_command_is_ignored_while_another_request_is_running():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.act("lw_restore")
    assert [c for c, _ in h.provider.sent] == ["lead_word_status", "lead_word_off"]


# --- item 26: closing the window ------------------------------------------

@pytest.mark.parametrize("stage", ["listening", "checking"])
def test_close_in_the_listening_or_checking_stage_sends_discard(stage):
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result(stage)
    h.act("lw_close")
    assert h.provider.sent[-1][0] == "lead_word_discard"
    assert h.controller.window_open is False


def test_close_while_waiting_for_the_listening_message_sends_discard():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.act("lw_close")
    assert h.provider.sent[-1][0] == "lead_word_discard"


def test_close_with_a_candidate_sends_discard():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_close")
    assert h.provider.sent[-1][0] == "lead_word_discard"


def test_close_with_nothing_pending_sends_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_close")
    assert [c for c, _ in h.provider.sent] == ["lead_word_status"]


def test_a_result_after_close_is_ignored_and_pushes_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.act("lw_close")
    before = len(h.gui)
    h.result("passed", request_id="r2", measures=PASSED_MEASURES, key_path="")
    assert len(h.gui) == before


def test_reopening_starts_with_a_new_status_request():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_close")
    h.act("lw_open")
    assert h.provider.sent[-1][0] == "lead_word_status"
    assert h.state["screen"] == SCREEN_LOADING
    assert h.state["recorded"] == ""


# --- send failure and timeout ----------------------------------------------

def test_a_command_that_cannot_be_sent_shows_an_error_and_waits_for_nothing():
    h = Harness()
    h.open_with_status()
    h.provider.send_result = False
    h.act("lw_record")
    state = h.state
    assert state["screen"] == SCREEN_IDLE
    assert "could not reach" in state["error"].lower()
    assert state["busy"] is False
    assert h.live_timers() == []


def test_a_send_that_fails_later_shows_the_error_for_the_current_request():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.controller.on_send_failed(h.last_id)
    state = h.state
    assert state["screen"] == SCREEN_IDLE
    assert "could not reach" in state["error"].lower()
    assert h.live_timers() == []


def test_a_late_send_failure_of_an_older_request_is_ignored():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    record_id = h.last_id
    h.act("lw_discard")
    before = len(h.gui)
    h.controller.on_send_failed(record_id)
    assert len(h.gui) == before
    assert h.controller.current_request_id is not None


def test_a_request_without_an_answer_ends_with_an_error_after_the_timeout():
    h = Harness()
    h.act("lw_open")
    timer = h.live_timers()[-1]
    assert timer.delay == RESPONSE_TIMEOUT_S
    timer.fire()
    state = h.state
    assert state["screen"] == SCREEN_IDLE
    assert state["busy"] is False
    assert "did not answer" in state["error"]


@pytest.mark.parametrize("last_stage", [None, "listening", "checking"])
def test_a_recording_without_an_answer_sends_discard_at_the_timeout(last_stage):
    # Without the discard, the provider can stay in recording mode and
    # take the next utterance as a hidden take.
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    if last_stage:
        h.result(last_stage)
    h.live_timers()[-1].fire()
    assert h.provider.sent[-1][0] == "lead_word_discard"
    assert h.state["screen"] == SCREEN_IDLE
    assert "did not answer" in h.state["error"]


def test_a_status_without_an_answer_sends_no_discard_at_the_timeout():
    h = Harness()
    h.act("lw_open")
    h.live_timers()[-1].fire()
    assert [c for c, _ in h.provider.sent] == ["lead_word_status"]


def test_a_progress_result_arms_the_timer_again():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    first = h.live_timers()[-1]
    h.result("listening")
    assert first.cancelled
    assert len(h.live_timers()) == 1
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    assert h.live_timers() == []


# --- capabilities ------------------------------------------------------------

def test_a_provider_that_reconnects_while_the_window_is_open_gets_a_new_status():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.controller.set_supported(True)
    assert h.provider.sent[-1][0] == "lead_word_status"
    assert h.state["screen"] == SCREEN_LOADING


def test_a_provider_that_loses_support_while_open_shows_unsupported():
    h = Harness()
    h.open_with_status()
    h.controller.set_supported(False)
    assert h.state["supported"] is False
    h.act("lw_record")
    assert [c for c, _ in h.provider.sent] == ["lead_word_status"]


# --- item 22: playback hold --------------------------------------------------

def test_playback_holds_transcription_before_the_gui_is_told_to_play():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.provider.events.clear()
    h.act("lw_play_start", which="current", duration_ms=620)
    events = h.provider.events
    assert events.index("hold") < events.index("lw_play_go")
    go = [m for m in h.gui if m["action"] == "lw_play_go"]
    assert go == [{"action": "lw_play_go", "which": "current"}]
    assert h.state["playing"] is True


def test_the_gui_is_not_told_to_play_until_the_engine_hold_is_done():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.provider.hold_immediately = False
    h.act("lw_play_start", which="current", duration_ms=620)
    assert not [m for m in h.gui if m["action"] == "lw_play_go"]
    h.provider.pending_hold()
    assert [m for m in h.gui if m["action"] == "lw_play_go"]


def test_transcription_returns_after_the_playback_length_plus_300_ms():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="candidate", duration_ms=620)
    timer = h.live_timers()[-1]
    assert timer.delay == pytest.approx(0.620 + PLAYBACK_TAIL_S)
    assert PLAYBACK_TAIL_S == pytest.approx(0.3)
    assert "release" not in h.provider.events
    timer.fire()
    assert h.provider.events.count("release") == 1
    assert h.state["playing"] is False


def test_closing_the_window_during_playback_releases_transcription():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    h.act("lw_close")
    assert h.provider.events.count("release") == 1
    assert h.live_timers() == []


def test_a_second_play_during_playback_is_ignored():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    h.act("lw_play_start", which="current", duration_ms=620)
    assert h.provider.events.count("hold") == 1


@pytest.mark.parametrize("duration", [0, -5, 60_001, "620", None, True, 1.5])
def test_a_malformed_playback_length_starts_no_hold(duration):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=duration)
    assert "hold" not in h.provider.events


def test_playback_is_refused_while_listening():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    h.act("lw_play_start", which="current", duration_ms=620)
    assert "hold" not in h.provider.events


def test_record_is_ignored_during_playback():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    h.act("lw_record")
    assert "lead_word_record" not in [c for c, _ in h.provider.sent]


def test_the_hold_timer_ends_a_hold_whose_done_never_comes():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.provider.hold_immediately = False
    h.act("lw_play_start", which="current", duration_ms=620)
    timer = h.live_timers()[-1]
    assert timer.delay == RESPONSE_TIMEOUT_S
    timer.fire()
    assert h.provider.events.count("release") == 1
    # A late done must not start a sound.
    h.provider.pending_hold()
    assert not [m for m in h.gui if m["action"] == "lw_play_go"]


# --------------------------------------------------------------------------
# Strength tests added with the mutation gate (wh-first-sound-lost.23, stage
# G2). Each one is red under a named mutation in
# tests/mutation_gate_lead_word_app_controller.py.
# --------------------------------------------------------------------------

STATE_FIELDS = {
    "supported", "screen", "busy", "playing", "loaded", "recorded",
    "microphone", "previous_exists", "current_exists", "key_path",
    "candidate", "measures", "reason", "error", "notice", "noise_advice",
}


def _gui_count(h):
    return len(h.gui)


def test_the_constants_have_their_documented_values():
    assert PLAYBACK_TAIL_S == 0.3
    assert PLAYBACK_MAX_MS == 60_000
    assert RESPONSE_TIMEOUT_S == 45.0


def test_the_state_message_carries_exactly_these_fields():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    assert set(h.state) == STATE_FIELDS
    assert h.state["supported"] is True
    assert h.state["playing"] is False


def test_the_state_message_measures_are_a_copy():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.state["measures"]["extra"] = 1
    h.act("lw_play_end")
    assert "extra" not in h.state["measures"]


def test_the_stored_measures_are_a_copy_of_the_result():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    measures = {"voiced_ms": 600}
    h.result("passed", measures=measures, key_path="")
    measures["extra"] = 1
    h.act("lw_play_end")
    assert h.state["measures"] == {"voiced_ms": 600}


# --- the default scheduler and request ID --------------------------------

def test_the_default_scheduler_does_nothing_outside_an_event_loop():
    assert lwc._asyncio_schedule(1.0, lambda: None) is None


def test_the_default_scheduler_arms_a_timer_on_the_running_loop():
    async def main():
        loop = asyncio.get_running_loop()
        handle = lwc._asyncio_schedule(5.0, lambda: None)
        try:
            assert isinstance(handle, asyncio.TimerHandle)
            assert handle.when() - loop.time() == pytest.approx(5.0, abs=1.0)
        finally:
            handle.cancel()

    asyncio.run(main())


def test_a_controller_without_a_scheduler_or_id_maker_still_opens():
    provider = FakeProvider()
    pushed = []
    c = LeadWordController(send_to_gui=pushed.append, provider=provider)
    c.set_supported(True)
    pushed.clear()
    c.handle_gui_action("lw_open", {})
    # The request timer is armed with the default scheduler before the first
    # state is pushed; a missing default stops the action before the push
    # (the failure is logged, not raised).
    assert [m["state"]["screen"] for m in pushed if m["action"] == "lw_state"] \
        == [SCREEN_LOADING]
    c.handle_gui_action("lw_close", {})
    c.handle_gui_action("lw_open", {})
    first, second = provider.sent
    assert first[0] == second[0] == "lead_word_status"
    assert re.fullmatch(r"[0-9a-f]{32}", first[1])
    assert re.fullmatch(r"[0-9a-f]{32}", second[1])
    assert first[1] != second[1]


def test_a_timer_that_cannot_be_cancelled_does_not_stop_the_controller():
    class BadTimer:
        def cancel(self):
            raise RuntimeError("cannot cancel")

    gui = []
    provider = FakeProvider()
    ids = iter(f"r{n}" for n in range(1, 50))
    c = LeadWordController(
        send_to_gui=gui.append, provider=provider,
        schedule=lambda delay, cb: BadTimer(),
        new_request_id=lambda: next(ids),
    )
    c.set_supported(True)
    c.handle_gui_action("lw_open", {})
    c.on_result({"stage": "status", "request_id": "r1", "loaded": False,
                 "recorded": "", "microphone": "", "previous_exists": False,
                 "key_path": ""})
    assert gui[-1]["state"]["screen"] == SCREEN_IDLE


# --- set_supported -----------------------------------------------------------

def test_a_provider_declaring_support_while_the_window_is_closed_sends_nothing():
    h = Harness(supported=False)
    h.controller.set_supported(True)
    assert h.provider.sent == []
    assert h.gui == []


def test_losing_support_while_the_window_is_closed_pushes_nothing():
    h = Harness()
    h.controller.set_supported(False)
    assert h.gui == []
    h.act("lw_open")
    assert h.state["supported"] is False


def test_support_is_stored_as_a_true_or_false_value():
    h = Harness(supported=False)
    h.controller.set_supported(1)
    h.act("lw_open")
    assert h.state["supported"] is True


def test_losing_support_while_open_ends_the_request_and_shows_loading():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    assert h.state["busy"] is True
    assert h.live_timers()
    h.controller.set_supported(False)
    assert h.live_timers() == []
    assert h.controller.current_request_id is None
    assert h.state["screen"] == SCREEN_LOADING
    assert h.state["busy"] is False
    assert h.state["supported"] is False


def test_losing_the_provider_during_playback_releases_the_hold():
    # T2: set_supported(False) during playback releases the playback hold.
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    assert h.state["playing"] is True
    h.controller.set_supported(False)
    assert h.provider.events.count("release") == 1
    assert h.state["playing"] is False
    assert h.live_timers() == []


def test_reopening_the_window_during_playback_releases_the_hold():
    # T3: _release_playback in _on_open.
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    h.act("lw_open")
    assert h.provider.events.count("release") == 1
    assert h.state["playing"] is False
    assert h.state["screen"] == SCREEN_LOADING


def test_opening_with_an_unsupported_provider_shows_loading_and_not_busy():
    h = Harness(supported=False)
    h.act("lw_open")
    assert h.controller.window_open is True
    assert h.state["screen"] == SCREEN_LOADING
    assert h.state["busy"] is False


# --- handle_gui_action -------------------------------------------------------

def test_an_unknown_action_is_ignored_with_a_warning(caplog):
    h = Harness()
    h.open_with_status()
    before = (_gui_count(h), len(h.provider.sent))
    with caplog.at_level(logging.WARNING):
        h.act("lw_nonsense")
    assert (_gui_count(h), len(h.provider.sent)) == before
    assert "Unknown lead-word action" in caplog.text


def test_a_command_that_is_not_a_dict_counts_as_empty(caplog):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    with caplog.at_level(logging.DEBUG):
        h.controller.handle_gui_action("lw_play_start", "not a dict")
        h.controller.handle_gui_action("lw_play_start", None)
        h.controller.handle_gui_action("lw_close", None)
    assert "hold" not in h.provider.events
    assert h.controller.window_open is False
    # An empty command is a bad duration. It is not an exception.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_an_exception_inside_an_action_is_logged_and_not_raised(caplog):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)

    def boom(done):
        raise RuntimeError("engine gone")

    h.provider.hold_transcription = boom
    with caplog.at_level(logging.ERROR):
        h.act("lw_play_start", which="current", duration_ms=620)
    assert "Lead-word action 'lw_play_start' failed" in caplog.text


def test_a_result_that_breaks_the_handler_is_logged_and_not_raised(caplog):
    class Broken(dict):
        def get(self, *args):
            raise RuntimeError("broken result")

    h = Harness()
    h.open_with_status()
    with caplog.at_level(logging.ERROR):
        h.controller.on_result(Broken(request_id=h.last_id))
    assert "Lead-word result handling failed" in caplog.text


@pytest.mark.parametrize("data", ["text", None, 5, ["status"]])
def test_a_result_that_is_not_a_dict_is_ignored_without_an_error(data, caplog):
    h = Harness()
    h.open_with_status()
    before = _gui_count(h)
    with caplog.at_level(logging.ERROR):
        h.controller.on_result(data)
    assert _gui_count(h) == before
    assert caplog.records == []


# --- close -------------------------------------------------------------------

def test_close_ends_the_request_timer_and_clears_the_view():
    h = Harness()
    h.act("lw_open")
    assert h.live_timers()
    h.act("lw_close")
    assert h.live_timers() == []
    assert h.controller.current_request_id is None
    assert h.controller.screen == "closed"


def test_close_after_losing_support_sends_no_discard_for_a_candidate():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.controller.set_supported(False)
    h.act("lw_close")
    assert [c for c, _ in h.provider.sent] == ["lead_word_status", "lead_word_record"]


def test_a_close_that_comes_before_any_open_does_nothing():
    h = Harness()
    h.act("lw_close")
    assert h.provider.sent == []
    assert h.gui == []


def test_a_late_send_failure_after_the_window_closed_changes_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_close")
    before = _gui_count(h)
    h.controller.on_send_failed(None)
    assert h.controller.screen == "closed"
    assert _gui_count(h) == before


# --- commands ------------------------------------------------------------------

def test_a_command_before_the_window_opens_is_ignored():
    h = Harness()
    h.act("lw_record")
    h.act("lw_off")
    assert h.provider.sent == []


@pytest.mark.parametrize("stage", ["listening", "checking"])
@pytest.mark.parametrize("action", ["lw_keep", "lw_restore", "lw_off", "lw_on"])
def test_the_other_commands_wait_while_a_recording_runs(action, stage):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    h.result(stage)
    sent = len(h.provider.sent)
    h.act(action)
    assert len(h.provider.sent) == sent


@pytest.mark.parametrize("stage", ["listening", "checking"])
def test_discard_works_while_a_recording_runs(stage):
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result(stage)
    h.act("lw_discard")
    assert h.provider.sent[-1][0] == "lead_word_discard"
    assert h.state["screen"] == SCREEN_IDLE
    assert h.state["busy"] is True


def test_record_arms_the_request_timer_and_is_not_busy():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    assert h.live_timers()[-1].delay == 45.0
    assert h.state["busy"] is False


def test_a_command_is_busy_and_arms_the_request_timer():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    assert h.state["busy"] is True
    assert h.live_timers()[-1].delay == 45.0


@pytest.mark.parametrize("action", ["lw_off", "lw_restore", "lw_discard"])
def test_a_command_is_ignored_during_playback(action):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    sent = len(h.provider.sent)
    h.act(action)
    assert len(h.provider.sent) == sent


def test_record_clears_the_candidate_and_its_measures():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_record")
    assert h.state["candidate"] is False
    assert h.state["measures"] == {}
    assert h.state["screen"] == SCREEN_LISTENING


def test_record_clears_a_refusal_reason():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("refused", reason="No speech heard", measures={"capture_ms": 1})
    h.act("lw_record")
    assert h.state["reason"] == ""
    assert h.state["measures"] == {}


def test_record_clears_an_error():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("failed", reason="Disk full")
    assert h.state["error"] == "Disk full"
    h.act("lw_record")
    assert h.state["error"] == ""


def test_record_clears_a_notice():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.result("off", loaded=False, recorded="x", microphone="", previous_exists=False)
    assert h.state["notice"] == "off"
    h.act("lw_record")
    assert h.state["notice"] == ""


def test_a_new_command_clears_an_error():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_keep")
    h.result("failed", reason="Disk full")
    assert h.state["error"] == "Disk full"
    h.act("lw_keep")
    assert h.state["error"] == ""
    assert h.state["busy"] is True


def test_a_new_command_clears_a_notice():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.result("off", loaded=False, recorded="x", microphone="", previous_exists=False)
    assert h.state["notice"] == "off"
    h.act("lw_on")
    assert h.state["notice"] == ""


def test_discard_clears_a_refusal_and_its_measures():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("refused", reason="No speech heard", measures={"capture_ms": 1})
    h.act("lw_discard")
    state = h.state
    assert state["reason"] == ""
    assert state["measures"] == {}
    assert state["screen"] == SCREEN_IDLE


def test_discard_clears_an_error_and_a_notice():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("failed", reason="Disk full")
    h.act("lw_discard")
    assert h.state["error"] == ""
    h.result("kept", loaded=True, recorded="x", microphone="", previous_exists=False)
    assert h.state["notice"] == "kept"
    h.act("lw_discard")
    assert h.state["notice"] == ""


# --- playback --------------------------------------------------------------------

@pytest.mark.parametrize("duration", [1, 60_000])
def test_the_playback_length_limits_are_inclusive(duration):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=duration)
    assert h.provider.events.count("hold") == 1
    assert h.state["playing"] is True
    assert h.live_timers()[-1].delay == pytest.approx(duration / 1000.0 + 0.3)


@pytest.mark.parametrize("which", ["", "other", None, "Current", 5])
def test_playback_of_an_unknown_file_starts_no_hold(which):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which=which, duration_ms=620)
    assert "hold" not in h.provider.events
    assert h.state["playing"] is False


def test_playback_before_the_window_opens_is_ignored():
    h = Harness()
    h.act("lw_play_start", which="current", duration_ms=620)
    assert "hold" not in h.provider.events
    assert h.gui == []


def test_playback_is_refused_while_a_request_runs():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.act("lw_play_start", which="current", duration_ms=620)
    assert "hold" not in h.provider.events


def test_playback_is_refused_while_checking():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    h.result("checking")
    h.act("lw_play_start", which="current", duration_ms=620)
    assert "hold" not in h.provider.events


def test_playback_is_refused_on_the_waiting_screen():
    h = Harness(supported=False)
    h.act("lw_open")
    h.act("lw_play_start", which="current", duration_ms=620)
    assert "hold" not in h.provider.events


def test_the_hold_timer_is_replaced_when_the_sound_starts():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    assert len(h.live_timers()) == 1
    assert h.live_timers()[0].delay == pytest.approx(0.92)
    assert h.timers[-2].cancelled


def test_closing_the_window_while_the_hold_is_pending_starts_no_sound():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.provider.hold_immediately = False
    h.act("lw_play_start", which="current", duration_ms=620)
    h.act("lw_close")
    h.provider.pending_hold()
    assert not [m for m in h.gui if m["action"] == "lw_play_go"]
    assert h.provider.events.count("release") == 1


def test_a_failing_release_does_not_break_the_end_of_playback(caplog):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)

    def boom():
        raise RuntimeError("engine gone")

    h.provider.release_transcription = boom
    with caplog.at_level(logging.ERROR):
        h.live_timers()[-1].fire()
    assert h.state["playing"] is False
    assert "transcription release failed" in caplog.text


def test_the_gui_saying_playback_ended_releases_the_hold_and_pushes():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="current", duration_ms=620)
    h.act("lw_play_end")
    assert h.provider.events.count("release") == 1
    assert h.state["playing"] is False
    assert h.live_timers() == []


def test_playback_end_after_close_pushes_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_close")
    before = _gui_count(h)
    h.act("lw_play_end")
    assert _gui_count(h) == before


# --- results -----------------------------------------------------------------------

def test_a_passed_result_keeps_the_key_path_when_it_sends_no_text():
    h = Harness()
    h.open_with_status(key_path=r"C:\L\hello.wav")
    h.act("lw_record")
    h.result("passed", measures="not a dict", key_path=5)
    assert h.state["key_path"] == r"C:\L\hello.wav"
    assert h.state["measures"] == {}


@pytest.mark.parametrize("earlier", ["refused", "failed", "kept"])
def test_a_passed_result_clears_what_an_earlier_result_left(earlier):
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    if earlier == "refused":
        h.result("refused", reason="No speech heard")
    elif earlier == "failed":
        h.result("failed", reason="Disk full")
    else:
        h.result("kept", loaded=True, recorded="x", microphone="",
                 previous_exists=False)
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    state = h.state
    assert state["screen"] == SCREEN_PASSED
    assert (state["reason"], state["error"], state["notice"]) == ("", "", "")


@pytest.mark.parametrize("earlier", ["passed", "failed", "kept"])
def test_a_refused_result_clears_what_an_earlier_result_left(earlier):
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    if earlier == "passed":
        h.result("passed", measures=PASSED_MEASURES, key_path="")
    elif earlier == "failed":
        h.result("failed", reason="Disk full")
    else:
        h.result("kept", loaded=True, recorded="x", microphone="",
                 previous_exists=False)
    h.result("refused", reason="No speech heard", measures={"capture_ms": 1})
    state = h.state
    assert state["screen"] == SCREEN_REFUSED
    assert state["candidate"] is False
    assert (state["error"], state["notice"]) == ("", "")
    assert state["reason"] == "No speech heard"


def test_a_refused_result_without_text_or_measures_is_empty():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    # A list of pairs is what dict() would turn into a dict, so a helper that
    # passed any value through would show it here.
    h.result("refused", reason=7, measures=[("voiced_ms", 5)])
    # The result must have been applied; the state before it also reads
    # reason "" and measures {}, which would make the two checks below hold
    # for a result that was dropped.
    assert h.state["screen"] == SCREEN_REFUSED
    assert h.state["reason"] == ""
    assert h.state["measures"] == {}


def test_a_failed_result_without_text_uses_the_fallback():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("failed", reason=None)
    assert h.state["error"] == "The request failed."
    assert h.state["screen"] == SCREEN_IDLE
    assert h.state["busy"] is False


def test_a_failed_result_clears_a_notice():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("kept", loaded=True, recorded="x", microphone="", previous_exists=False)
    assert h.state["notice"] == "kept"
    h.result("failed", reason="Disk full")
    assert h.state["notice"] == ""


@pytest.mark.parametrize("stage", ["status", "kept", "restored", "off", "on"])
@pytest.mark.parametrize("earlier", ["passed", "refused", "failed"])
def test_a_state_result_clears_what_an_earlier_result_left(stage, earlier):
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    if earlier == "passed":
        h.result("passed", measures=PASSED_MEASURES, key_path="")
    elif earlier == "refused":
        h.result("refused", reason="No speech heard", measures={"capture_ms": 1})
    else:
        h.result("failed", reason="Disk full")
    h.result(stage, loaded=True, recorded="r", microphone="m",
             previous_exists=True)
    state = h.state
    assert state["screen"] == SCREEN_IDLE
    assert state["candidate"] is False
    assert state["measures"] == {}
    assert (state["reason"], state["error"]) == ("", "")
    assert state["notice"] == ("" if stage == "status" else stage)
    assert state["busy"] is False


def test_a_status_result_reports_a_previous_recording():
    h = Harness()
    h.open_with_status(loaded=True, previous_exists=True)
    assert h.state["previous_exists"] is True


def test_state_fields_of_the_wrong_type_become_empty_and_absent_ones_stay():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.result("off", loaded="yes", recorded=5, microphone=None, previous_exists=1)
    state = h.state
    assert state["loaded"] is False
    assert state["recorded"] == ""
    assert state["microphone"] == ""
    assert state["previous_exists"] is False
    h.act("lw_on")
    h.result("on", loaded=True, recorded="r", microphone="m", previous_exists=True)
    assert h.state["previous_exists"] is True
    h.act("lw_off")
    h.result("off", loaded=False)
    state = h.state
    assert state["loaded"] is False
    assert state["recorded"] == "r"
    assert state["microphone"] == "m"
    assert state["previous_exists"] is True


def test_only_a_status_result_changes_the_key_path():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.result("off", loaded=False, recorded="", microphone="",
             previous_exists=False, key_path=r"C:\L\other.wav")
    assert h.state["key_path"] == r"C:\L\hello.wav"
    h.act("lw_on")
    h.result("status", loaded=True, key_path=r"C:\L\other.wav")
    assert h.state["key_path"] == r"C:\L\other.wav"
    h.act("lw_on")
    h.result("status", loaded=True, key_path=9)
    assert h.state["key_path"] == r"C:\L\other.wav"


def test_a_result_with_an_unknown_stage_pushes_nothing(caplog):
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    before = _gui_count(h)
    with caplog.at_level(logging.WARNING):
        h.result("weird")
    assert _gui_count(h) == before
    assert "unknown stage" in caplog.text


# --- send failure and timeout, exact texts -----------------------------------------

def test_a_failed_send_with_a_candidate_goes_back_to_the_candidate_screen():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.provider.send_result = False
    h.act("lw_keep")
    state = h.state
    assert state["screen"] == SCREEN_PASSED
    assert state["candidate"] is True
    assert state["error"] == "WheelHouse could not reach the speech engine."
    assert state["busy"] is False
    assert h.controller.current_request_id is None


def test_a_provider_that_raises_while_sending_counts_as_a_failed_send(caplog):
    h = Harness()
    h.open_with_status()

    def boom(command, request_id):
        raise RuntimeError("socket closed")

    h.provider.send_command = boom
    with caplog.at_level(logging.ERROR):
        h.act("lw_record")
    assert h.state["screen"] == SCREEN_IDLE
    assert h.state["error"] == "WheelHouse could not reach the speech engine."
    assert h.live_timers() == []
    assert "could not be sent" in caplog.text


def test_a_late_send_failure_clears_a_notice():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    request_id = h.last_id
    h.result("off", loaded=False, recorded="", microphone="", previous_exists=False)
    assert h.state["notice"] == "off"
    h.controller.on_send_failed(request_id)
    assert h.state["notice"] == ""
    assert h.state["error"] == "WheelHouse could not reach the speech engine."


def test_a_timeout_after_a_candidate_goes_back_to_the_candidate_screen():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    h.act("lw_keep")
    h.live_timers()[-1].fire()
    state = h.state
    assert state["screen"] == SCREEN_PASSED
    assert state["candidate"] is True
    assert state["error"] == "The speech engine did not answer."
    assert state["busy"] is False
    assert h.controller.current_request_id is None


def test_a_timeout_clears_a_notice():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("kept", loaded=True, recorded="x", microphone="", previous_exists=False)
    assert h.state["notice"] == "kept"
    h.result("listening")
    h.live_timers()[-1].fire()
    assert h.state["notice"] == ""
    assert h.state["error"] == "The speech engine did not answer."


def test_a_timer_of_an_older_request_does_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    old_timer = h.live_timers()[-1]
    h.act("lw_discard")
    before = _gui_count(h)
    old_timer.callback()
    assert _gui_count(h) == before
    assert h.state["error"] == ""
    assert h.controller.current_request_id is not None


# --------------------------------------------------------------------------
# More strength tests, each red under a named mutation in
# tests/mutation_gate_lead_word_app_controller.py.
# --------------------------------------------------------------------------

def test_the_screen_names_are_the_wire_values():
    # The window compares these strings; the tests above import the names, so
    # only literal values prove the wire format.
    assert lwc.SCREEN_CLOSED == "closed"
    assert lwc.SCREEN_LOADING == "loading"
    assert lwc.SCREEN_IDLE == "idle"
    assert lwc.SCREEN_LISTENING == "listening"
    assert lwc.SCREEN_CHECKING == "checking"
    assert lwc.SCREEN_PASSED == "passed"
    assert lwc.SCREEN_REFUSED == "refused"


def test_the_default_scheduler_runs_the_callback():
    async def main():
        fired = []
        lwc._asyncio_schedule(0.0, lambda: fired.append("ran"))
        await asyncio.sleep(0.05)
        return fired

    assert asyncio.run(main()) == ["ran"]


def test_cancelling_no_timer_logs_nothing(caplog):
    h = Harness()
    with caplog.at_level(logging.DEBUG):
        h.act("lw_open")
        h.act("lw_close")
    assert "cancel failed" not in caplog.text


def test_a_new_controller_does_not_assume_support():
    gui = []
    c = LeadWordController(
        send_to_gui=gui.append, provider=FakeProvider(),
        schedule=lambda delay, cb: None, new_request_id=lambda: "r1",
    )
    c.handle_gui_action("lw_open", {})
    assert gui[-1]["state"]["supported"] is False
    assert gui[-1]["state"]["screen"] == SCREEN_LOADING


BLANK_STATE = {
    "supported": False, "screen": "loading", "busy": False, "playing": False,
    "loaded": False, "recorded": "", "microphone": "",
    "previous_exists": False, "current_exists": False, "key_path": "",
    "candidate": False, "measures": {}, "reason": "", "error": "",
    "notice": "", "noise_advice": False,
}


def test_the_window_state_before_any_answer_is_blank():
    h = Harness(supported=False)
    h.act("lw_open")
    assert h.state == BLANK_STATE


def test_a_session_leaves_nothing_for_the_next_one():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    h.result("refused", reason="No speech heard", measures={"capture_ms": 1})
    h.act("lw_close")
    h.controller.set_supported(False)
    h.act("lw_open")
    assert h.state == BLANK_STATE
    assert h.controller.screen == "loading"


def test_a_window_that_never_played_never_releases_transcription():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_record")
    h.act("lw_close")
    h.act("lw_open")
    h.controller.set_supported(False)
    assert "release" not in h.provider.events
    assert "hold" not in h.provider.events


def test_the_gui_is_told_which_file_to_play():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_play_start", which="candidate", duration_ms=640)
    go = [m for m in h.gui if m["action"] == "lw_play_go"]
    assert go == [{"action": "lw_play_go", "which": "candidate"}]


def test_a_reconnect_asks_again_and_waits_on_the_answer():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.controller.set_supported(True)
    assert h.state["busy"] is True
    assert h.state["screen"] == SCREEN_LOADING
    assert h.live_timers()[-1].delay == RESPONSE_TIMEOUT_S


def test_a_reconnect_clears_an_error():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("failed", reason="Disk full")
    assert h.state["error"] == "Disk full"
    h.controller.set_supported(True)
    assert h.state["error"] == ""


def test_a_reconnect_clears_a_notice():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    h.act("lw_off")
    h.result("off", loaded=False, recorded="x", microphone="", previous_exists=False)
    assert h.state["notice"] == "off"
    h.controller.set_supported(True)
    assert h.state["notice"] == ""


def test_a_reconnect_drops_the_candidate():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    assert h.state["candidate"] is True
    h.controller.set_supported(True)
    assert h.state["candidate"] is False


def test_a_result_without_a_request_id_after_close_changes_nothing():
    h = Harness()
    h.open_with_status()
    h.act("lw_close")
    before = _gui_count(h)
    h.controller.on_result({"stage": "status", "request_id": None, "loaded": True})
    assert h.controller.screen == "closed"
    assert _gui_count(h) == before


def test_the_screen_property_follows_the_view():
    h = Harness()
    assert h.controller.screen == "closed"
    h.act("lw_open")
    assert h.controller.screen == SCREEN_LOADING
    h.result("status", loaded=False, recorded="", microphone="",
             previous_exists=False, key_path="")
    assert h.controller.screen == SCREEN_IDLE


def test_the_current_request_id_is_the_last_one_sent():
    h = Harness()
    h.act("lw_open")
    assert h.controller.current_request_id == "r1"
    h.result("status", loaded=False, recorded="", microphone="",
             previous_exists=False, key_path="")
    h.act("lw_record")
    assert h.controller.current_request_id == "r2"


# --------------------------------------------------------------------------
# current_exists (wh-first-sound-lost.23.1.1): hello.wav exists although its
# recorded date can be empty (a restored earlier file). A provider that does
# not send the field counts as current_exists = (recorded != "").
# --------------------------------------------------------------------------

def test_current_exists_from_the_provider_reaches_the_window():
    h = Harness()
    h.open_with_status(loaded=True, recorded="", current_exists=True)
    assert h.state["current_exists"] is True
    assert h.state["recorded"] == ""
    h.act("lw_restore")
    h.result("restored", loaded=True, recorded="", microphone="",
             previous_exists=True, current_exists=True)
    assert h.state["current_exists"] is True


def test_current_exists_false_from_the_provider_wins_over_a_recorded_date():
    h = Harness()
    h.open_with_status(loaded=False, recorded="2026-10-22 15:37",
                       current_exists=False)
    assert h.state["current_exists"] is False


def test_a_provider_without_current_exists_counts_a_recorded_date():
    h = Harness()
    h.open_with_status(**STATUS_WITH_RECORDING)
    assert h.state["current_exists"] is True
    h.act("lw_off")
    h.result("off", loaded=False, recorded="", microphone="",
             previous_exists=False)
    assert h.state["current_exists"] is False


def test_a_current_exists_that_is_not_true_or_false_counts_as_false():
    h = Harness()
    h.open_with_status(loaded=True, recorded="2026-10-22 15:37",
                       current_exists=1)
    assert h.state["current_exists"] is False


# --------------------------------------------------------------------------
# The noise advice (wh-training-word-noisy-reject). The provider decides it
# and sends noise_advice in the passed result; the window shows the advice
# text while that candidate is shown.
# --------------------------------------------------------------------------

def _passed_with_advice(h):
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="",
             noise_advice=True)
    assert h.state["noise_advice"] is True


def test_a_passed_result_with_the_noise_advice_reaches_the_window():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    assert h.state["screen"] == SCREEN_PASSED


def test_an_older_passed_result_without_the_field_gives_no_advice():
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    assert h.state["screen"] == SCREEN_PASSED
    assert h.state["noise_advice"] is False


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_a_noise_advice_that_is_not_true_gives_no_advice(value):
    h = Harness()
    h.open_with_status()
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="",
             noise_advice=value)
    assert h.state["screen"] == SCREEN_PASSED
    assert h.state["noise_advice"] is False


def test_the_window_opens_with_no_advice():
    # The state pushed at the opening, before the provider answers: a status
    # answer clears the advice by itself, so it cannot show this.
    h = Harness()
    h.act("lw_open")
    assert h.state["noise_advice"] is False


def test_a_passed_result_without_advice_clears_an_earlier_advice():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.act("lw_record")
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    assert h.state["noise_advice"] is False


def test_each_passed_result_sets_the_advice_from_its_own_field():
    # A second passed result for the same request (no record between them)
    # replaces the advice of the first one.
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.result("passed", measures=PASSED_MEASURES, key_path="")
    assert h.state["noise_advice"] is False


def test_record_clears_the_advice():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.act("lw_record")
    assert h.state["screen"] == SCREEN_LISTENING
    assert h.state["noise_advice"] is False


def test_discard_clears_the_advice():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.act("lw_discard")
    assert h.state["noise_advice"] is False


def test_a_refused_result_clears_the_advice():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    # No record press between the two results: a press clears it by itself.
    h.result("refused", reason="No speech heard", measures={})
    assert h.state["screen"] == SCREEN_REFUSED
    assert h.state["noise_advice"] is False


@pytest.mark.parametrize("stage", ["status", "kept", "restored", "off", "on"])
def test_a_state_result_clears_the_advice(stage):
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.result(stage, loaded=True, recorded="r", microphone="m",
             previous_exists=True)
    assert h.state["noise_advice"] is False


def test_a_failed_keep_keeps_the_advice_with_the_candidate():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.act("lw_keep")
    h.result("failed", reason="Disk full")
    assert h.state["screen"] == SCREEN_PASSED
    assert h.state["noise_advice"] is True


def test_closing_the_window_clears_the_advice():
    h = Harness()
    h.open_with_status()
    _passed_with_advice(h)
    h.act("lw_close")
    # The state pushed at the next opening, before the status answer.
    h.act("lw_open")
    assert h.state["noise_advice"] is False
