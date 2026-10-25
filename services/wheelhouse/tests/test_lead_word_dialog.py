"""'Your training word' window (wh-first-sound-lost.23, acceptance items
20, 21, 13, 17, 18, 22, 23, 25 and 26).

The window holds no session logic; each test feeds it the lw_state payload
that the Logic process would send and checks what a user sees and which
action a press sends. Playback tests patch winsound, so no sound plays.
"""
import wave
from unittest.mock import MagicMock

import pytest
from PySide6.QtGui import QCloseEvent

import lead_word_dialog as lwd
from lead_word_dialog import LeadWordDialog

pytestmark = pytest.mark.usefixtures("qapp")

HELLO = "hello"


def _state(**over):
    state = {
        "supported": True, "screen": "idle", "busy": False, "playing": False,
        "loaded": False, "recorded": "", "microphone": "",
        "previous_exists": False, "current_exists": False, "key_path": "",
        "candidate": False, "measures": {}, "reason": "", "error": "",
        "notice": "", "noise_advice": False,
    }
    state.update(over)
    return state


def _with_recording(**over):
    fields = {
        "loaded": True, "recorded": "2026-10-22 15:37",
        "microphone": "Desk mic", "current_exists": True,
    }
    fields.update(over)
    return _state(**fields)


def _passed(**over):
    return _with_recording(
        screen="passed", candidate=True,
        measures={"voiced_ms": 640, "level_dbfs": -12.4}, **over,
    )


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "WheelHouse" / "lead-word"
    path.mkdir(parents=True)
    return path


def _write_wav(path, ms=500):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * (16 * ms))


@pytest.fixture
def dialog():
    d = LeadWordDialog()
    d.set_environment(
        speech_enabled=True, push_to_talk=False,
        provider="parakeet_tdt", microphone="Desk mic",
    )
    yield d
    d.close()
    d.deleteLater()


@pytest.fixture
def actions(dialog):
    seen = []
    dialog.lead_word_action.connect(seen.append)
    return seen


def _visible(dialog):
    """Texts of the buttons a user can see."""
    return [b.text() for b in dialog._buttons if not b.isHidden()]


def _enabled(dialog):
    return [
        b.text() for b in dialog._buttons if not b.isHidden() and b.isEnabled()
    ]


def _texts(dialog):
    return " ".join(
        label.text() for label in (
            dialog._message_label, dialog._detail_label, dialog._recording_label,
            dialog._join_label, dialog._mic_label, dialog._error_label,
            dialog._hint_label, dialog._advice_label,
        ) if not label.isHidden()
    )


def _note(dialog):
    """The line above the buttons, as a user reads it (no break marks)."""
    assert not dialog._keep_note_label.isHidden()
    return dialog._keep_note_label.text().replace("\u200b", "")


# --------------------------------------------------------------------------
# Item 20: the idle window
# --------------------------------------------------------------------------

def test_title_is_your_training_word(dialog):
    assert dialog.windowTitle() == "Your training word"


def test_no_recording_shows_no_recording_and_join_off(dialog):
    dialog.handle_state(_state())
    assert "No recording" in _texts(dialog)
    assert "The training word is off." in _texts(dialog)


def test_a_recording_shows_its_date_microphone_and_join_on(dialog):
    dialog.handle_state(_with_recording())
    text = _texts(dialog)
    assert "Recording from 2026-10-22 15:37, microphone Desk mic" in text
    assert "The training word is on." in text


def test_idle_buttons_with_no_recording(dialog):
    dialog.handle_state(_state())
    assert _visible(dialog) == [
        "Record", "Play current recording", "Turn off", "Close",
    ]
    # Play current and Turn off are available only when a recording exists.
    assert _enabled(dialog) == ["Record", "Close"]


def test_idle_buttons_with_a_recording(dialog):
    dialog.handle_state(_with_recording())
    assert _enabled(dialog) == [
        "Record", "Play current recording", "Turn off", "Close",
    ]


def test_non_parakeet_engine_gets_the_explanation_and_no_record(dialog):
    dialog.set_environment(provider="google_stt")
    dialog.handle_state(_state())
    assert "Only the Parakeet speech engine uses the training word" in _texts(
        dialog
    )
    assert _visible(dialog) == ["Close"]
    assert _enabled(dialog) == ["Close"]


def test_a_parakeet_engine_that_is_not_ready_waits(dialog):
    dialog.handle_state(_state(supported=False, screen="loading"))
    assert "Waiting for the Parakeet engine" in _texts(dialog)
    assert _visible(dialog) == ["Close"]


def test_loading_screen_has_only_close(dialog):
    dialog.handle_state(_state(screen="loading"))
    assert _visible(dialog) == ["Close"]
    assert _enabled(dialog) == ["Close"]


def test_a_malformed_state_is_ignored(dialog):
    dialog.handle_state(_state())
    dialog.handle_state("not a dict")
    dialog.handle_state(None)
    assert "No recording" in _texts(dialog)


# --------------------------------------------------------------------------
# Item 21: Record
# --------------------------------------------------------------------------

def test_record_is_off_while_speech_is_off(dialog):
    dialog.set_environment(speech_enabled=False, push_to_talk=False)
    dialog.handle_state(_state())
    assert "Record" not in _enabled(dialog)
    assert "Turn speech on first" in _texts(dialog)


def test_record_works_in_push_to_talk_mode_with_speech_off(dialog):
    # Departure from item 21: in push-to-talk mode speech is off between
    # holds, so the mode itself counts as speech on.
    dialog.set_environment(speech_enabled=False, push_to_talk=True)
    dialog.handle_state(_state())
    assert "Record" in _enabled(dialog)


def test_record_press_sends_lw_record(dialog, actions):
    dialog.handle_state(_state())
    dialog._record_btn.click()
    assert actions == [{"action": "lw_record"}]


def test_listening_shows_the_instruction(dialog):
    dialog.handle_state(_state(screen="listening", busy=False))
    assert (
        "Say hello once, at your normal speaking volume. Then stay quiet."
        in _texts(dialog)
    )
    assert "push-to-talk" not in _texts(dialog)


def test_listening_in_push_to_talk_mode_says_to_hold_the_button(dialog):
    dialog.set_environment(push_to_talk=True)
    dialog.handle_state(_state(screen="listening"))
    text = _texts(dialog)
    assert "Say hello once, at your normal speaking volume." in text
    assert "Hold the floating button while you say hello." in text


@pytest.mark.parametrize("screen", ["listening", "checking"])
def test_record_and_both_play_buttons_are_off_while_recording(dialog, screen):
    dialog.handle_state(_with_recording(screen=screen))
    enabled = _enabled(dialog)
    for name in ("Record", "Play current recording", "Play new recording"):
        assert name not in enabled


def test_record_and_both_play_buttons_are_off_during_playback(dialog):
    dialog.handle_state(_with_recording(playing=True))
    enabled = _enabled(dialog)
    assert "Record" not in enabled
    assert "Play current recording" not in enabled
    dialog.handle_state(_passed(playing=True))
    enabled = _enabled(dialog)
    assert "Play new recording" not in enabled
    assert "Play current recording" not in enabled


def test_the_production_parakeet_id_shows_record(dialog):
    # The GUI process receives the provider ID from the state update,
    # which is "parakeet_tdt" (stt/parakeet_model.py PROVIDER_NAME).
    dialog.set_environment(provider="parakeet_tdt")
    dialog.handle_state(_state())
    assert "Record" in _visible(dialog)


def test_another_engine_hides_record(dialog):
    dialog.set_environment(provider="google_stt")
    dialog.handle_state(_state())
    assert "Record" not in _visible(dialog)


def test_discard_is_off_during_playback(dialog):
    dialog.handle_state(_passed(playing=True))
    assert "Discard" not in _enabled(dialog)
    dialog.handle_state(_passed(playing=False))
    assert "Discard" in _enabled(dialog)


def test_checking_shows_a_working_message(dialog):
    dialog.handle_state(_state(screen="checking"))
    assert "Checking the recording." in _texts(dialog)


def test_refused_shows_the_reason_and_record_again(dialog):
    dialog.handle_state(_state(screen="refused", reason="No speech heard"))
    assert "No speech heard" in _texts(dialog)
    assert "Record again" in _enabled(dialog)
    assert "Record" not in _visible(dialog)


def test_passed_shows_length_level_and_the_one_word(dialog):
    dialog.handle_state(_passed())
    text = _texts(dialog)
    assert "Length: 640 ms." in text
    assert "Level: -12 dBFS." in text
    assert "One word: hello." in text


def test_passed_buttons_with_a_current_recording(dialog):
    dialog.handle_state(_passed())
    assert _visible(dialog) == [
        "Play current recording",
        "Play new recording",
        "Keep new recording",
        "Discard",
    ]


def test_passed_buttons_with_no_current_recording(dialog):
    dialog.handle_state(_state(
        screen="passed", candidate=True, measures={"voiced_ms": 600},
    ))
    assert _visible(dialog) == [
        "Play current recording", "Play new recording", "Keep new recording",
        "Discard",
    ]
    assert "Play current recording" not in _enabled(dialog)


def test_a_provider_error_is_shown(dialog):
    dialog.handle_state(_state(error="WheelHouse could not reach the speech engine."))
    assert "could not reach the speech engine" in _texts(dialog)


# --------------------------------------------------------------------------
# Item 13: the Keep texts
# --------------------------------------------------------------------------

def test_keep_note_names_the_other_file_the_key_points_at(dialog, folder):
    other = r"C:\Users\x\comms_hello.wav"
    dialog.handle_state(_passed(key_path=other))
    assert dialog._keep_btn.text() == "Keep new recording"
    assert _note(dialog) == f"Keep new recording replaces the word file {other}."


def test_keep_note_names_the_date_when_hello_wav_exists(dialog, folder):
    dialog.handle_state(_passed(key_path=str(folder / "hello.wav")))
    assert dialog._keep_btn.text() == "Keep new recording"
    assert _note(dialog) == (
        "Keep new recording replaces the recording from 2026-10-22 15:37."
    )


def test_keep_note_is_hidden_when_no_recording_and_the_key_is_empty(dialog, folder):
    dialog.handle_state(_state(screen="passed", candidate=True))
    assert dialog._keep_btn.text() == "Keep new recording"
    assert dialog._keep_note_label.isHidden()
    assert dialog._keep_note_label.text() == ""


def test_other_file_wins_over_the_date(dialog, folder):
    dialog.handle_state(_passed(key_path=r"C:\other\word.wav"))
    assert "replaces the word file" in _note(dialog)


def test_a_key_that_differs_only_in_case_and_slashes_is_hello_wav(dialog, folder):
    odd = str(folder / "hello.wav").upper().replace("\\", "/")
    dialog.handle_state(_passed(key_path=odd))
    assert "replaces the recording from" in _note(dialog)


def test_keep_press_sends_lw_keep(dialog, actions):
    dialog.handle_state(_passed())
    dialog._keep_btn.click()
    assert actions == [{"action": "lw_keep"}]


def test_discard_press_sends_lw_discard(dialog, actions):
    dialog.handle_state(_passed())
    dialog._discard_btn.click()
    assert actions == [{"action": "lw_discard"}]


# --------------------------------------------------------------------------
# Items 17, 18: Turn off, Turn on, Go back
# --------------------------------------------------------------------------

def test_turn_off_press_sends_lw_off(dialog, actions):
    dialog.handle_state(_with_recording())
    dialog._switch_btn.click()
    assert actions == [{"action": "lw_off"}]


def test_with_the_join_off_the_button_reads_turn_on_and_sends_lw_on(dialog, actions):
    dialog.handle_state(_with_recording(loaded=False))
    assert dialog._switch_btn.text() == "Turn on"
    dialog._switch_btn.click()
    assert actions == [{"action": "lw_on"}]


def test_go_back_is_shown_only_while_a_previous_recording_exists(dialog, actions):
    dialog.handle_state(_with_recording(previous_exists=False))
    assert "Go back to previous recording" not in _visible(dialog)
    dialog.handle_state(_with_recording(previous_exists=True))
    assert "Go back to previous recording" in _enabled(dialog)
    dialog._go_back_btn.click()
    assert actions == [{"action": "lw_restore"}]


def test_go_back_is_hidden_while_a_new_recording_waits_for_keep(dialog):
    # Departure from item 18: with a candidate on disk, a restore would
    # leave the candidate behind, so the button waits for Keep or Discard.
    dialog.handle_state(_passed(previous_exists=True))
    assert "Go back to previous recording" not in _visible(dialog)


@pytest.mark.parametrize(
    "notice,words",
    [
        ("kept", "Your new recording is saved"),
        ("restored", "Went back to the previous recording"),
        ("off", "The training word is off. Your recordings are kept."),
        ("on", "The training word is on."),
    ],
    # Explicit ids: the mutation gate reads a parametrized test name up to
    # the first space, so an id built from the text would not match.
    ids=["kept", "restored", "off", "on"],
)
def test_each_notice_has_its_text(dialog, notice, words):
    dialog.handle_state(_with_recording(notice=notice))
    assert words in _texts(dialog)


# --------------------------------------------------------------------------
# Item 23: microphone change
# --------------------------------------------------------------------------

def test_a_different_microphone_suggests_a_new_recording(dialog):
    dialog.set_environment(microphone="Headset")
    dialog.handle_state(_with_recording())
    text = dialog._mic_label.text()
    assert not dialog._mic_label.isHidden()
    assert "Desk mic" in text and "Headset" in text
    assert "new recording" in text
    # The join stays on.
    assert "The training word is on." in _texts(dialog)


def test_the_same_microphone_suggests_nothing(dialog):
    dialog.set_environment(microphone="Desk mic")
    dialog.handle_state(_with_recording())
    assert dialog._mic_label.isHidden()
    assert dialog._mic_label.text() == ""


def test_an_unknown_current_microphone_suggests_nothing(dialog):
    dialog.set_environment(microphone="")
    dialog.handle_state(_with_recording())
    assert dialog._mic_label.isHidden()


# --------------------------------------------------------------------------
# Item 25: the accessible name equals the text in every state
# --------------------------------------------------------------------------

ALL_STATES = {
    "loading": _state(screen="loading"),
    "unsupported": _state(supported=False, screen="loading"),
    "idle no recording": _state(),
    "idle recording": _with_recording(),
    "idle recording join off": _with_recording(loaded=False),
    "idle previous": _with_recording(previous_exists=True),
    "listening": _state(screen="listening"),
    "checking": _state(screen="checking"),
    "refused": _state(screen="refused", reason="No speech heard"),
    "passed plain": _state(screen="passed", candidate=True),
    "passed date": _passed(),
    "passed other file": _passed(key_path=r"C:\Users\x\comms_hello.wav"),
    "busy": _with_recording(busy=True),
    "playing": _with_recording(playing=True),
}


@pytest.mark.parametrize("name", list(ALL_STATES))
def test_every_button_accessible_name_equals_its_text(dialog, name):
    dialog.handle_state(ALL_STATES[name])
    for btn in dialog._buttons:
        assert btn.accessibleName() == btn.text(), (name, btn.text())


def test_accessible_names_follow_the_text_through_a_sequence(dialog):
    for name in ALL_STATES:
        dialog.handle_state(ALL_STATES[name])
        for btn in dialog._buttons:
            assert btn.accessibleName() == btn.text(), (name, btn.text())
    # Back to the first state: nothing kept an old name.
    dialog.handle_state(ALL_STATES["idle no recording"])
    assert dialog._switch_btn.accessibleName() == "Turn off"
    assert dialog._record_btn.accessibleName() == "Record"


def test_the_turn_on_name_changes_with_the_text(dialog):
    dialog.handle_state(_with_recording(loaded=False))
    assert dialog._switch_btn.accessibleName() == "Turn on"
    dialog.handle_state(_with_recording(loaded=True))
    assert dialog._switch_btn.accessibleName() == "Turn off"


def test_every_keep_text_is_the_spoken_name(dialog):
    # 'click keep new recording' must find Keep in each of its three cases.
    for state in (
        _passed(key_path=r"C:\a\b.wav"), _passed(),
        _state(screen="passed", candidate=True),
    ):
        dialog.handle_state(state)
        assert dialog._keep_btn.text() == "Keep new recording"
        assert dialog._keep_btn.accessibleName() == "Keep new recording"


# --------------------------------------------------------------------------
# Item 22: playback
# --------------------------------------------------------------------------

@pytest.fixture
def sound(monkeypatch):
    fake = MagicMock()
    fake.SND_FILENAME = 0x20000
    fake.SND_ASYNC = 0x1
    fake.SND_PURGE = 0x40
    monkeypatch.setattr(lwd, "winsound", fake)
    return fake


def test_play_current_asks_logic_with_the_duration_and_plays_nothing_yet(
    dialog, actions, folder, sound
):
    _write_wav(folder / "hello.wav", ms=500)
    dialog.handle_state(_with_recording())
    dialog._play_current_btn.click()
    assert actions == [
        {"action": "lw_play_start", "which": "current", "duration_ms": 500}
    ]
    sound.PlaySound.assert_not_called()


def test_play_new_asks_for_the_candidate_file(dialog, actions, folder, sound):
    _write_wav(folder / "hello.candidate.wav", ms=640)
    dialog.handle_state(_passed())
    dialog._play_new_btn.click()
    assert actions == [
        {"action": "lw_play_start", "which": "candidate", "duration_ms": 640}
    ]


def test_play_go_plays_the_file_with_the_async_flags(dialog, folder, sound):
    _write_wav(folder / "hello.wav")
    dialog.handle_state(_with_recording(playing=True))
    assert dialog.play_now("current") is True
    sound.PlaySound.assert_called_once_with(
        str(folder / "hello.wav"), sound.SND_FILENAME | sound.SND_ASYNC
    )


def test_play_go_plays_the_candidate_file(dialog, folder, sound):
    _write_wav(folder / "hello.candidate.wav")
    dialog.handle_state(_passed(playing=True))
    dialog.play_now("candidate")
    assert sound.PlaySound.call_args.args[0] == str(
        folder / "hello.candidate.wav"
    )


def test_play_go_with_no_playback_running_plays_nothing(dialog, folder, sound):
    _write_wav(folder / "hello.wav")
    dialog.handle_state(_with_recording(playing=False))
    assert dialog.play_now("current") is False
    sound.PlaySound.assert_not_called()


def test_play_go_after_close_plays_nothing(dialog, folder, sound):
    # A late lw_play_go can reach the queue after the user closed the
    # window; Logic has already released the hold then.
    _write_wav(folder / "hello.wav")
    dialog.handle_state(_with_recording(playing=True))
    dialog.reject()
    sound.PlaySound.reset_mock()
    assert dialog.play_now("current") is False
    sound.PlaySound.assert_not_called()


def test_a_missing_file_shows_an_error_and_sends_nothing(
    dialog, actions, folder, sound
):
    dialog.handle_state(_with_recording())
    dialog._play_current_btn.click()
    assert actions == []
    assert "could not play" in _texts(dialog)


def test_a_playback_failure_shows_an_error(dialog, folder, sound):
    _write_wav(folder / "hello.wav")
    sound.PlaySound.side_effect = RuntimeError("no device")
    dialog.handle_state(_with_recording(playing=True))
    assert dialog.play_now("current") is False
    assert "could not play" in _texts(dialog)


def test_a_new_state_clears_a_local_error(dialog, folder, sound):
    dialog.handle_state(_with_recording())
    dialog._play_current_btn.click()
    assert "could not play" in _texts(dialog)
    dialog.handle_state(_with_recording())
    assert "could not play" not in _texts(dialog)


def test_a_file_with_no_samples_is_not_played(dialog, actions, folder, sound):
    (folder / "hello.wav").write_bytes(b"not a wav")
    dialog.handle_state(_with_recording())
    dialog._play_current_btn.click()
    assert actions == []


def test_wav_duration_rounds_to_whole_milliseconds(folder):
    _write_wav(folder / "a.wav", ms=1234)
    assert lwd.wav_duration_ms(str(folder / "a.wav")) == 1234
    assert lwd.wav_duration_ms(str(folder / "missing.wav")) is None


def test_the_folder_comes_from_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert lwd.lead_word_folder() == str(tmp_path / "WheelHouse" / "lead-word")
    monkeypatch.delenv("LOCALAPPDATA")
    assert lwd.lead_word_folder() is None


# --------------------------------------------------------------------------
# Item 26: closing
# --------------------------------------------------------------------------

# The window is never shown in these tests: the first real show of a window
# makes Windows raise a COM callback fault message in the test process.
# A spy on hide() proves the window goes away.

def test_close_button_sends_one_lw_close_and_hides(dialog, actions, sound):
    dialog.hide = MagicMock()
    dialog.handle_state(_state())
    dialog._close_btn.click()
    assert actions == [{"action": "lw_close"}]
    dialog.hide.assert_called_once_with()


def test_the_title_bar_close_and_escape_send_the_same_one_close(
    dialog, actions, sound
):
    dialog.close()
    dialog.reject()
    assert actions == [{"action": "lw_close"}]


def test_the_title_bar_close_hides_instead_of_destroying(dialog, actions, sound):
    dialog.hide = MagicMock()
    dialog.close()
    dialog.hide.assert_called_once_with()


def test_a_new_opening_arms_lw_close_again(dialog, actions, sound):
    dialog.close()
    dialog.begin_session()
    dialog.close()
    assert actions == [{"action": "lw_close"}, {"action": "lw_close"}]


def test_closing_stops_a_running_sound(dialog, sound):
    dialog.close()
    sound.PlaySound.assert_called_once_with(None, sound.SND_PURGE)


def test_begin_session_blanks_the_view(dialog):
    dialog.handle_state(_with_recording())
    dialog.begin_session()
    assert "Recording from" not in _texts(dialog)
    assert _visible(dialog) == ["Close"]


# --------------------------------------------------------------------------
# Strength tests added with the mutation gate (wh-first-sound-lost.23, stage
# G2). Each one is red under a named mutation in
# tests/mutation_gate_lead_word_dialog.py.
# --------------------------------------------------------------------------

@pytest.fixture
def bare_dialog():
    """A window that has been told nothing about the engine."""
    d = LeadWordDialog()
    yield d
    d.close()
    d.deleteLater()


def test_the_loading_screen_says_it_asks_the_engine(dialog):
    dialog.handle_state(_state(screen="loading"))
    assert "Asking the speech engine about your recording." in _texts(dialog)


@pytest.mark.parametrize(
    "notice,text",
    [
        ("kept", "Your new recording is saved. The training word is on."),
        ("restored",
         "Went back to the previous recording. The training word is on."),
        ("off", "The training word is off. Your recordings are kept."),
        ("on", "The training word is on."),
    ],
    ids=["kept", "restored", "off", "on"],
)
def test_each_notice_is_the_whole_message(dialog, notice, text):
    # The join line says 'The training word is on.' for a loaded word, so
    # only the message line can prove the notice text.
    dialog.handle_state(_with_recording(notice=notice))
    assert dialog._message_label.text() == text
    assert not dialog._message_label.isHidden()


def test_an_unknown_notice_shows_no_message(dialog):
    dialog.handle_state(_with_recording(notice="something else"))
    assert dialog._message_label.text() == ""
    assert dialog._message_label.isHidden()


def test_a_refusal_with_a_reason_shows_it_after_the_lead_in(dialog):
    dialog.handle_state(_state(screen="refused", reason="No speech heard"))
    assert dialog._message_label.text() == (
        "This recording was not used. No speech heard"
    )


@pytest.mark.parametrize("reason", ["", None, 5])
def test_a_refusal_with_no_reason_uses_the_fallback_text(dialog, reason):
    dialog.handle_state(_state(screen="refused", reason=reason))
    assert dialog._message_label.text() == (
        "The recording did not pass the check."
    )


def test_the_passed_message_and_detail_are_exact(dialog):
    dialog.handle_state(_passed())
    assert dialog._message_label.text() == "The recording passed the check."
    assert dialog._detail_label.text() == (
        "Length: 640 ms. Level: -12 dBFS. One word: hello."
    )


@pytest.mark.parametrize(
    "measures",
    [
        {"voiced_ms": True, "level_dbfs": False},
        {"voiced_ms": "640", "level_dbfs": "-12"},
        {},
        "not a dict",
    ],
    ids=["booleans", "numbers-as-text", "empty", "not-a-dict"],
)
def test_a_measure_that_is_not_a_number_is_not_shown(dialog, measures):
    dialog.handle_state(_state(screen="passed", candidate=True, measures=measures))
    assert dialog._detail_label.text() == "One word: hello."


def test_no_button_is_the_default_button(dialog):
    # A stray Enter must not press Keep, Discard or Turn off.
    dialog.handle_state(_passed())
    for btn in dialog._buttons:
        assert btn.autoDefault() is False, btn.text()
        assert btn.isDefault() is False, btn.text()


def test_every_button_has_its_accessible_name_before_any_state_arrives(dialog):
    for btn in dialog._buttons:
        assert btn.accessibleName() == btn.text(), btn.text()


def test_the_intro_shows_for_parakeet_and_hides_for_another_engine(dialog):
    dialog.handle_state(_state())
    assert not dialog._intro_label.isHidden()
    dialog.set_environment(provider="google_stt")
    assert dialog._intro_label.isHidden()


def test_an_engine_not_yet_known_is_not_called_wrong(bare_dialog):
    bare_dialog.set_environment(speech_enabled=True)
    bare_dialog.handle_state(_state())
    assert "Only the Parakeet" not in _texts(bare_dialog)
    assert "Record" in _visible(bare_dialog)


@pytest.mark.parametrize("which", ["refused-everything", "passed-with-error"])
def test_switching_to_another_engine_clears_every_line(dialog, which):
    dialog.set_environment(speech_enabled=False, microphone="Headset")
    if which == "refused-everything":
        dialog.handle_state(_with_recording(
            screen="refused", reason="No speech heard",
            error="WheelHouse could not reach the speech engine.",
        ))
        assert not dialog._hint_label.isHidden()
        assert not dialog._mic_label.isHidden()
    else:
        dialog.handle_state(_passed(error="Disk full"))
        assert not dialog._detail_label.isHidden()
    assert not dialog._error_label.isHidden()
    dialog.set_environment(provider="google_stt")
    assert _texts(dialog) == (
        "Only the Parakeet speech engine uses the training word. You are "
        "using a different engine, which does not need it. If you switch to "
        "Parakeet later, come back here."
    )
    assert _visible(dialog) == ["Close"]


def test_an_unsupported_provider_waits_whatever_the_screen_says(dialog):
    dialog.handle_state(_with_recording(supported=False))
    text = _texts(dialog)
    assert "Waiting for the Parakeet engine to start." in text
    assert "Recording from" not in text
    assert _visible(dialog) == ["Close"]
    # The lines of a working window are hidden, not just empty.
    for label in (
        dialog._recording_label, dialog._join_label, dialog._mic_label,
        dialog._detail_label, dialog._hint_label,
    ):
        assert label.isHidden()


@pytest.mark.parametrize("screen", [None, "closed"])
def test_a_missing_or_closed_screen_waits_for_the_engine(dialog, screen):
    dialog.handle_state(_with_recording(screen=screen))
    text = _texts(dialog)
    assert "Asking the speech engine about your recording." in text
    assert "Recording from" not in text
    assert _visible(dialog) == ["Close"]


def test_the_waiting_screen_after_a_working_one_has_only_close(dialog):
    dialog.handle_state(_with_recording(previous_exists=True))
    assert len(_visible(dialog)) > 1
    dialog.handle_state(_state(screen="loading"))
    assert _visible(dialog) == ["Close"]


def test_the_waiting_screen_shows_an_error(dialog):
    dialog.handle_state(_state(
        screen="loading", error="WheelHouse could not reach the speech engine.",
    ))
    assert "could not reach the speech engine" in _texts(dialog)


def test_no_microphone_suggestion_without_a_recording(dialog):
    dialog.set_environment(microphone="Headset")
    dialog.handle_state(_state(microphone="Desk mic"))
    assert dialog._mic_label.isHidden()


def test_the_microphone_suggestion_says_the_word_stays_on(dialog):
    dialog.set_environment(microphone="Headset")
    dialog.handle_state(_with_recording())
    assert dialog._mic_label.text() == (
        "You made this recording with a different microphone (Desk mic). "
        "Your microphone now is Headset. A new recording may work better. "
        "The training word stays on until you turn it off."
    )


def test_a_microphone_that_is_not_text_counts_as_unknown(dialog):
    dialog.handle_state(_with_recording())
    dialog.set_environment(microphone=5)
    assert dialog._mic_label.isHidden()


@pytest.mark.parametrize(
    "state,shown",
    [
        (dict(screen="idle"), True),
        (dict(screen="refused", reason="No speech heard"), True),
        (dict(screen="passed", candidate=True), False),
        (dict(screen="listening"), False),
        (dict(screen="checking"), False),
        (dict(screen="idle", busy=True), False),
        (dict(screen="idle", playing=True), False),
    ],
    ids=["idle", "refused", "passed", "listening", "checking", "busy", "playing"],
)
def test_the_speech_off_hint_shows_only_when_record_could_be_pressed(
    dialog, state, shown
):
    dialog.set_environment(speech_enabled=False, push_to_talk=False)
    dialog.handle_state(_state(**state))
    assert (not dialog._hint_label.isHidden()) is shown
    if shown:
        assert dialog._hint_label.text() == (
            "Record works while speech is on. Turn speech on first."
        )


def test_the_speech_off_hint_is_hidden_while_speech_is_on(dialog):
    dialog.handle_state(_state())
    assert dialog._hint_label.isHidden()
    assert dialog._hint_label.text() == ""


@pytest.mark.parametrize("flag", ["busy", "playing"])
def test_candidate_buttons_are_off_while_a_request_runs_or_a_sound_plays(
    dialog, flag
):
    dialog.handle_state(_passed(**{flag: True}))
    enabled = _enabled(dialog)
    assert "Play new recording" not in enabled
    assert "Discard" not in enabled
    assert "Keep new recording" not in enabled
    assert dialog._close_btn.isHidden()


@pytest.mark.parametrize(
    "over",
    [
        dict(busy=True), dict(playing=True), dict(screen="listening"),
        dict(screen="checking"),
    ],
    ids=["busy", "playing", "listening", "checking"],
)
def test_record_go_back_turn_off_and_play_are_off_while_locked(dialog, over):
    dialog.handle_state(_with_recording(previous_exists=True, **over))
    enabled = _enabled(dialog)
    for name in (
        "Record", "Go back to previous recording", "Turn off",
        "Play current recording",
    ):
        assert name not in enabled, name
    assert "Close" in enabled


def test_the_buttons_come_back_when_the_lock_ends(dialog):
    dialog.handle_state(_with_recording(previous_exists=True, busy=True))
    dialog.handle_state(_with_recording(previous_exists=True))
    assert _enabled(dialog) == [
        "Record", "Play current recording", "Go back to previous recording",
        "Turn off", "Close",
    ]


def test_keep_note_names_the_word_file_when_the_folder_is_unknown(
    dialog, monkeypatch
):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    dialog.handle_state(_passed(key_path=r"C:\a\b.wav"))
    assert dialog._keep_btn.text() == "Keep new recording"
    assert _note(dialog) == (
        r"Keep new recording replaces the word file C:\a\b.wav."
    )


def test_a_key_path_that_is_not_text_is_ignored(dialog, folder):
    dialog.handle_state(_passed(key_path=123))
    assert _note(dialog) == (
        "Keep new recording replaces the recording from 2026-10-22 15:37."
    )


def test_a_recorded_date_that_is_not_text_is_ignored(dialog, folder):
    dialog.handle_state(_state(screen="passed", candidate=True, recorded=7))
    assert dialog._keep_btn.text() == "Keep new recording"
    assert dialog._keep_note_label.isHidden()


def test_begin_session_clears_a_local_error(dialog, folder):
    dialog.handle_state(_with_recording())
    dialog._play_current_btn.click()
    assert "could not play" in _texts(dialog)
    dialog.begin_session()
    assert "could not play" not in _texts(dialog)


def test_set_environment_keeps_values_left_at_none(dialog):
    dialog.set_environment(
        speech_enabled=True, push_to_talk=True, provider="parakeet_tdt",
        microphone="Desk mic",
    )
    dialog.set_environment()
    assert dialog._env == {
        "speech_enabled": True, "push_to_talk": True,
        "provider": "parakeet_tdt", "microphone": "Desk mic",
    }


def test_set_environment_can_turn_the_flags_off(dialog):
    dialog.set_environment(speech_enabled=True, push_to_talk=True)
    dialog.set_environment(speech_enabled=False, push_to_talk=False)
    assert dialog._env["speech_enabled"] is False
    assert dialog._env["push_to_talk"] is False


@pytest.mark.parametrize("which", ["current", "candidate", "nonsense"])
def test_play_go_with_a_missing_file_shows_an_error_and_plays_nothing(
    dialog, folder, sound, which
):
    dialog.handle_state(_with_recording(playing=True))
    assert dialog.play_now(which) is False
    sound.PlaySound.assert_not_called()
    assert "could not play" in _texts(dialog)


def test_play_go_without_a_known_folder_shows_an_error(dialog, sound, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    dialog.handle_state(_with_recording(playing=True))
    assert dialog.play_now("current") is False
    sound.PlaySound.assert_not_called()
    assert "could not play" in _texts(dialog)


def _write_frames(path, frames, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)


def test_wav_duration_has_a_floor_of_one_millisecond(folder):
    _write_frames(folder / "zero.wav", 0)
    assert lwd.wav_duration_ms(str(folder / "zero.wav")) == 1


def test_wav_duration_rounds_half_a_millisecond_up(folder):
    # 8009 frames at 16 kHz are 500.5625 ms.
    _write_frames(folder / "half.wav", 8009)
    assert lwd.wav_duration_ms(str(folder / "half.wav")) == 501


def test_wav_duration_of_a_file_with_a_zero_rate_is_unknown(folder):
    path = folder / "zero_rate.wav"
    _write_frames(path, 160)
    raw = bytearray(path.read_bytes())
    raw[24:28] = (0).to_bytes(4, "little")  # the sample-rate field
    path.write_bytes(bytes(raw))
    assert lwd.wav_duration_ms(str(path)) is None


@pytest.mark.parametrize(
    "content", [b"", b"not a wav", b"RIFF\x00\x00\x00\x00WAVE"],
    ids=["empty", "garbage", "header-only"],
)
def test_wav_duration_of_an_unreadable_file_is_unknown(folder, content):
    path = folder / "bad.wav"
    path.write_bytes(content)
    assert lwd.wav_duration_ms(str(path)) is None


def test_the_close_event_is_ignored_so_the_window_can_open_again(dialog, sound):
    from PySide6.QtGui import QCloseEvent

    dialog.hide = MagicMock()
    event = QCloseEvent()
    event.accept()
    dialog.closeEvent(event)
    assert event.isAccepted() is False


def test_escape_alone_sends_one_lw_close(dialog, actions, sound):
    dialog.hide = MagicMock()
    dialog.reject()
    assert actions == [{"action": "lw_close"}]
    dialog.hide.assert_called_once_with()


def test_a_failing_sound_stop_does_not_block_the_close(dialog, actions, sound):
    sound.PlaySound.side_effect = RuntimeError("no device")
    dialog.hide = MagicMock()
    dialog._close_btn.click()
    assert actions == [{"action": "lw_close"}]
    dialog.hide.assert_called_once_with()


def test_closing_the_window_does_not_quit_the_application(dialog):
    from PySide6.QtCore import Qt

    assert dialog.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose) is False


# --------------------------------------------------------------------------
# More strength tests, each red under a named mutation in
# tests/mutation_gate_lead_word_dialog.py.
# --------------------------------------------------------------------------

def test_a_window_told_nothing_does_not_allow_record(bare_dialog):
    # Before the GUI gives the window its environment, speech counts as off,
    # push-to-talk as off, and the microphone as unknown.
    bare_dialog.handle_state(_with_recording())
    assert "Record" not in _enabled(bare_dialog)
    assert "Turn speech on first" in _texts(bare_dialog)
    assert bare_dialog._mic_label.isHidden()
    assert bare_dialog._error_label.isHidden()


def test_a_window_that_has_heard_nothing_waits_for_the_engine(bare_dialog):
    assert _texts(bare_dialog) == "Waiting for the Parakeet engine to start."
    assert _visible(bare_dialog) == ["Close"]
    bare_dialog.begin_session()
    assert _texts(bare_dialog) == "Waiting for the Parakeet engine to start."


def _rows(dialog):
    layout = dialog.layout()
    rows = []
    for index in range(layout.count()):
        row = layout.itemAt(index).layout()
        if row is None:
            continue
        rows.append([
            row.itemAt(i).widget().text() for i in range(row.count())
            if row.itemAt(i).widget() is not None
        ])
    return rows


def test_the_buttons_are_laid_out_in_three_rows(dialog):
    assert _rows(dialog) == [
        ["Record", "Play current recording", "Play new recording"],
        ["Keep new recording", "Discard"],
        ["Go back to previous recording", "Turn off", "Close"],
    ]


def test_every_text_line_wraps_and_shows_plain_text(dialog):
    from PySide6.QtCore import Qt

    for label in (
        dialog._intro_label, dialog._recording_label, dialog._join_label,
        dialog._mic_label, dialog._message_label, dialog._detail_label,
        dialog._error_label, dialog._hint_label, dialog._keep_note_label,
        dialog._advice_label,
    ):
        assert label.wordWrap() is True
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_set_environment_stores_plain_booleans(dialog):
    dialog.set_environment(speech_enabled=1, push_to_talk=0)
    assert dialog._env["speech_enabled"] is True
    assert dialog._env["push_to_talk"] is False


def test_a_recording_with_no_microphone_name_shows_only_the_date(dialog):
    dialog.set_environment(microphone="Headset")
    dialog.handle_state(_with_recording(microphone=""))
    assert dialog._recording_label.text() == "Recording from 2026-10-22 15:37"
    # An unknown recording microphone is not a different microphone.
    assert dialog._mic_label.isHidden()


def test_labels_with_nothing_to_say_are_hidden(dialog):
    dialog.handle_state(_state())
    assert dialog._message_label.isHidden()
    assert dialog._detail_label.isHidden()
    assert dialog._error_label.isHidden()
    assert dialog._hint_label.isHidden()
    assert dialog._mic_label.isHidden()


@pytest.mark.parametrize(
    "notice", [5, None, ["kept"], {"a": 1}],
    ids=["number", "none", "list", "dict"],
)
def test_a_notice_that_is_not_text_shows_no_message(dialog, notice):
    dialog.handle_state(_with_recording(notice=notice))
    assert dialog._message_label.text() == ""
    assert dialog._message_label.isHidden()


@pytest.mark.parametrize("error", [5, None, ["x"]], ids=["number", "none", "list"])
def test_an_error_that_is_not_text_is_ignored(dialog, error):
    dialog.handle_state(_state(error=error))
    assert dialog._error_label.text() == ""
    assert dialog._error_label.isHidden()


def test_play_without_a_known_folder_shows_an_error_and_sends_nothing(
    dialog, actions, monkeypatch
):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    dialog.handle_state(_with_recording())
    dialog._play_current_btn.click()
    assert actions == []
    assert "could not play" in _texts(dialog)


# --------------------------------------------------------------------------
# wh-first-sound-lost.23.1.1: hello.wav exists with no recorded date (an
# earlier file brought back with "Go back to previous recording"). The window
# takes current_exists from the state, not the date.
# --------------------------------------------------------------------------

def _earlier_file(**over):
    return _state(**{
        "loaded": True, "recorded": "", "microphone": "",
        "current_exists": True, "previous_exists": True, **over,
    })


def test_an_earlier_file_counts_as_a_recording(dialog):
    dialog.handle_state(_earlier_file())
    text = _texts(dialog)
    assert "No recording" not in text
    assert "Recording from your earlier file" in text
    assert "The training word is on." in text
    assert _enabled(dialog) == [
        "Record", "Play current recording", "Go back to previous recording",
        "Turn off", "Close",
    ]


def test_an_earlier_file_with_the_word_off_can_be_turned_on(dialog):
    dialog.handle_state(_earlier_file(loaded=False))
    assert "Turn on" in _enabled(dialog)


def test_playback_still_locks_the_buttons_of_an_earlier_file(dialog):
    dialog.handle_state(_earlier_file(playing=True))
    assert "Play current recording" not in _enabled(dialog)
    assert "Turn off" not in _enabled(dialog)


def test_a_date_without_current_exists_is_not_a_recording(dialog):
    """The Logic process fills current_exists for every provider, so the
    window reads that field alone."""
    dialog.handle_state(_state(recorded="2026-10-22 15:37", current_exists=False))
    assert "No recording" in _texts(dialog)


def test_keep_note_names_the_earlier_file(dialog, folder):
    dialog.handle_state(_earlier_file(
        screen="passed", candidate=True, measures={"voiced_ms": 600},
        key_path=str(folder / "hello.wav"),
    ))
    assert dialog._keep_btn.text() == "Keep new recording"
    assert _note(dialog) == (
        "Keep new recording replaces the recording from your earlier file."
    )


def test_keep_note_still_names_the_other_file_before_the_earlier_file(
    dialog, folder
):
    dialog.handle_state(_earlier_file(
        screen="passed", candidate=True, key_path=r"C:\other\word.wav",
    ))
    assert "replaces the word file" in _note(dialog)


def test_the_earlier_file_text_is_a_module_constant():
    assert lwd.EARLIER_FILE_TEXT == "Recording from your earlier file"


# --------------------------------------------------------------------------
# wh-first-sound-lost.25: the Keep button, the line above it, and Close
# --------------------------------------------------------------------------

LONG_KEY = (
    r"C:\Users\example\AppData\Local\WheelHouse\podiatrist-2026-10-21"
    r"\work\c13\lead\comms_hello.wav"
)


def test_a_long_key_path_goes_in_a_wrapping_line_not_on_the_keep_button(
    dialog, folder
):
    dialog.handle_state(_passed(key_path=LONG_KEY))
    sentence = f"Keep new recording replaces the word file {LONG_KEY}."
    note = dialog._keep_note_label
    assert dialog._keep_btn.text() == "Keep new recording"
    assert dialog._keep_btn.accessibleName() == "Keep new recording"
    assert not note.isHidden()
    assert note.text().replace("\u200b", "") == sentence
    assert note.accessibleName() == "Note: " + sentence
    assert note.wordWrap() is True
    assert dialog._close_btn.isHidden()
    assert not dialog._close_btn.isVisibleTo(dialog)


def _uia_matches(dialog):
    """The dialog's visible buttons and text lines as the UIA walker lists them.

    The walker reads each control's accessible name and screen bounds; this
    builds ElementMatch rows from the same two Qt values. Buttons take the
    Button role and a Text control takes control type 50020, as the walker
    reports them for Qt widgets. Both support Invoke and are enabled, which
    is what the verifier measured on the live dialog (bead
    wh-first-sound-lost.25.1.1).
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QLabel, QPushButton

    from ui.element_types import ElementMatch

    rows = []
    for number, widget in enumerate(
        dialog.findChildren(QPushButton) + dialog.findChildren(QLabel), 1
    ):
        name = widget.accessibleName()
        if not name or not widget.isVisibleTo(dialog):
            continue
        origin = widget.mapToGlobal(QPoint(0, 0))
        is_button = isinstance(widget, QPushButton)
        rows.append(ElementMatch(
            item_id=f"m{number}", display_number=number, name=name,
            role="button" if is_button else "text",
            bounds=(origin.x(), origin.y(), widget.width(), widget.height()),
            monitor_id=0, score=0.0, is_eligible=False, source="uia",
            invoke_supported=True, is_enabled=widget.isEnabled(),
            control_ref=None, control_type_id=50000 if is_button else 50020,
        ))
    return rows


def _decide_voice_click(dialog, name, cursor):
    """Run a role-less 'click <name>' through the production scorer and rule.

    confidence_scorer and clear_winner_rule.decide are the production
    modules. The uia_walker.walk_window step is not here: it needs a shown
    window with a live UI Automation tree, and the offscreen Qt platform,
    which some test runs use, gives none. _uia_matches supplies the rows the walker would return for
    this dialog, built from its real accessible names and bounds. The live
    walk is recorded on bead wh-first-sound-lost.25.1.1.
    """
    from dataclasses import replace

    from ui.clear_winner_rule import decide
    from ui.confidence_scorer import is_eligible, score
    from ui.element_types import ElementQuery

    query = ElementQuery(
        name=name, role=None, ordinal=None, spatial=None,
        raw_utterance="click " + name,
    )
    scored = [
        replace(m, score=score(query, m), is_eligible=True)
        for m in _uia_matches(dialog) if is_eligible(query, m)
    ]
    return decide(scored, cursor, 0, lambda _monitor: 96.0)


def _centre(dialog, name):
    row = next(m for m in _uia_matches(dialog) if m.name == name)
    x, y, w, h = row.bounds
    return (x + w // 2, y + h // 2)


def test_a_voice_click_on_keep_new_recording_picks_the_keep_button(
    dialog, folder
):
    """The note line starts its name with Note, so it is not a name match.

    Before the change the note's name began with the Keep button's name, so
    'click keep new recording' made the note an eligible starts-with match.
    With the cursor at the Play new recording button the note won and the
    press did nothing; with the cursor far away the outcome was ambiguous.
    """
    dialog.handle_state(_passed(key_path=LONG_KEY))
    dialog.show()
    names = [m.name for m in _uia_matches(dialog)]
    assert "Keep new recording" in names
    assert any("replaces the word file" in n for n in names)

    near_play = _centre(dialog, "Play new recording")
    keep = _centre(dialog, "Keep new recording")
    far = (keep[0] + 6000, keep[1] + 3000)
    for cursor in (near_play, keep, far):
        outcome = _decide_voice_click(dialog, "keep new recording", cursor)
        assert outcome.outcome == "ok", (cursor, outcome.outcome)
        assert outcome.winner.name == "Keep new recording", cursor


@pytest.mark.parametrize(
    "state",
    [
        _state(screen="idle"),
        _state(screen="refused", reason="Too quiet."),
        _with_recording(screen="listening"),
        _with_recording(screen="checking"),
        _with_recording(screen="idle", notice="kept"),
        _with_recording(),
    ],
    ids=["idle", "refused", "listening", "checking", "notice", "recording"],
)
def test_every_other_screen_keeps_close_and_hides_the_note(dialog, state):
    dialog.handle_state(state)
    assert dialog._close_btn.isVisibleTo(dialog)
    assert dialog._close_btn.isEnabled()
    assert dialog._close_btn.text() == "Close"
    assert dialog._keep_note_label.isHidden()


def test_the_waiting_and_wrong_engine_views_keep_close(dialog, bare_dialog):
    bare_dialog.handle_state(_passed(key_path=LONG_KEY))
    # No engine told yet counts as Parakeet, so this is the passed view.
    assert bare_dialog._close_btn.isHidden()
    dialog.handle_state(_state(screen="loading"))
    assert dialog._close_btn.isVisibleTo(dialog)
    assert dialog._keep_note_label.isHidden()
    dialog.handle_state(_state(supported=False))
    assert dialog._close_btn.isVisibleTo(dialog)
    dialog.handle_state(_passed(key_path=LONG_KEY))
    assert dialog._close_btn.isHidden()
    dialog.set_environment(provider="whisper_cpp")
    assert dialog._close_btn.isVisibleTo(dialog)
    assert dialog._close_btn.isEnabled()
    assert dialog._keep_note_label.isHidden()


def test_close_comes_back_when_the_screen_leaves_passed(dialog, folder):
    dialog.handle_state(_passed(key_path=LONG_KEY))
    assert dialog._close_btn.isHidden()
    dialog.handle_state(_with_recording())
    assert dialog._close_btn.isVisibleTo(dialog)
    assert dialog._keep_note_label.isHidden()
    assert dialog._keep_note_label.text() == ""


def test_title_bar_close_and_escape_each_send_one_lw_close_on_passed(
    dialog, actions, sound
):
    dialog.hide = MagicMock()
    dialog.handle_state(_passed(key_path=LONG_KEY))
    assert dialog._close_btn.isHidden()
    event = QCloseEvent()
    dialog.closeEvent(event)
    assert actions == [{"action": "lw_close"}]
    dialog.begin_session()
    actions.clear()
    dialog.handle_state(_passed(key_path=LONG_KEY))
    dialog.reject()
    assert actions == [{"action": "lw_close"}]
    dialog.reject()
    dialog.closeEvent(QCloseEvent())
    assert actions == [{"action": "lw_close"}]


def test_the_default_size_shows_the_whole_keep_text_and_the_whole_path(
    dialog, folder, sound
):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QApplication

    dialog.handle_state(_passed(key_path=LONG_KEY))
    dialog.show()
    try:
        QApplication.processEvents()
        # A path with no break mark makes the label ask for more width, and
        # the window grows past its default size instead of wrapping.
        assert dialog.width() == dialog.minimumWidth()
        keep = dialog._keep_btn
        assert keep.width() >= keep.sizeHint().width()
        note = dialog._keep_note_label
        assert not note.isHidden()
        width = note.contentsRect().width()
        needed = QFontMetrics(note.font()).boundingRect(
            QRect(0, 0, width, 100000), int(Qt.TextFlag.TextWordWrap),
            note.text(),
        )
        assert needed.width() <= width
        assert note.height() >= note.heightForWidth(note.width())
    finally:
        dialog.hide()


# --------------------------------------------------------------------------
# The noise advice (wh-training-word-noisy-reject, acceptance item 3). The
# provider decides it; the window shows the text on the passed screen only.
# --------------------------------------------------------------------------

# At "passed" the take is only a candidate until Keep, so the advice does
# not say that the recording was saved (the boss ruling of 2026-10-24 15:24).
ADVICE = (
    "The background noise is high. Wheelhouse works better in a quieter "
    "place or with a better microphone."
)


def test_the_advice_text_is_the_one_of_the_acceptance_field():
    assert lwd.NOISE_ADVICE_TEXT == ADVICE


def test_a_passed_recording_with_the_advice_shows_it(dialog):
    dialog.handle_state(_passed(noise_advice=True))
    assert not dialog._advice_label.isHidden()
    assert dialog._advice_label.text() == ADVICE
    assert dialog._message_label.text() == "The recording passed the check."
    assert "Keep new recording" in _visible(dialog)


def test_a_passed_recording_without_the_advice_hides_it(dialog):
    dialog.handle_state(_passed(noise_advice=False))
    assert dialog._advice_label.isHidden()
    assert ADVICE not in _texts(dialog)


def test_a_state_without_the_advice_field_hides_it(dialog):
    state = _passed()
    del state["noise_advice"]
    dialog.handle_state(state)
    assert dialog._message_label.text() == "The recording passed the check."
    assert dialog._advice_label.isHidden()


@pytest.mark.parametrize("value", [1, "true", None])
def test_an_advice_value_that_is_not_true_hides_it(dialog, value):
    dialog.handle_state(_passed(noise_advice=value))
    assert dialog._advice_label.isHidden()


@pytest.mark.parametrize("screen", ["idle", "listening", "checking", "refused"])
def test_the_advice_shows_on_the_passed_screen_only(dialog, screen):
    dialog.handle_state(_with_recording(screen=screen, noise_advice=True))
    assert dialog._advice_label.isHidden()


def test_the_advice_goes_when_the_next_state_has_none(dialog):
    dialog.handle_state(_passed(noise_advice=True))
    assert not dialog._advice_label.isHidden()
    dialog.handle_state(_with_recording(screen="listening"))
    assert dialog._advice_label.isHidden()


def test_the_waiting_view_hides_the_advice(dialog):
    dialog.handle_state(_passed(noise_advice=True))
    assert not dialog._advice_label.isHidden()
    dialog.handle_state(_passed(noise_advice=True, supported=False))
    assert dialog._advice_label.isHidden()
    assert ADVICE not in _texts(dialog)


def test_another_engine_hides_the_advice(dialog):
    dialog.handle_state(_passed(noise_advice=True))
    assert not dialog._advice_label.isHidden()
    dialog.set_environment(provider="google_stt")
    assert dialog._advice_label.isHidden()
    assert ADVICE not in _texts(dialog)


def test_the_advice_line_sits_in_the_window_below_the_detail(dialog):
    # A label outside the layout has no parent, and showing it opens a
    # separate window instead of a line in this one.
    layout = dialog.layout()
    detail = layout.indexOf(dialog._detail_label)
    assert detail >= 0
    assert layout.indexOf(dialog._advice_label) == detail + 1
    assert dialog._advice_label.parent() is dialog
