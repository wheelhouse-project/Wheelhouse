"""Unit tests for the pure parts of scripts/benchmarks/paste_loss_repro.py.

No window, no clipboard, no key sending, no subprocess: the harness itself
is never run here. The Input child entry is tested with ``input_process_main``
replaced by a recorder.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "benchmarks" / "paste_loss_repro.py"


def _load():
    spec = importlib.util.spec_from_file_location("paste_loss_repro", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["paste_loss_repro"] = module
    spec.loader.exec_module(module)
    return module


repro = _load()


CHECK_TEXT = """# Title

Some text with a code fence that must be ignored:

```
not part of the text
```

## The text to dictate (the same in both runs)

```
notepad

The weather was cool period

I walked to the store period
new paragraph

Second line here period
```
"""


# ---------------------------------------------------------------------------
# Sentence loading
# ---------------------------------------------------------------------------


def test_load_sentences_skips_notepad_new_paragraph_and_blank(tmp_path):
    path = tmp_path / "check.md"
    path.write_text(CHECK_TEXT, encoding="utf-8")
    assert repro.load_sentences(path) == [
        ["The", "weather", "was", "cool"],
        ["I", "walked", "to", "the", "store"],
        ["Second", "line", "here"],
    ]


def test_load_sentences_rejects_line_without_period(tmp_path):
    path = tmp_path / "check.md"
    path.write_text(
        "## The text to dictate\n\n```\nno ending here\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        repro.load_sentences(path)


def test_load_sentences_rejects_file_without_text(tmp_path):
    path = tmp_path / "check.md"
    path.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError):
        repro.load_sentences(path)


@pytest.mark.skipif(
    not repro.DEFAULT_CHECK_PATH.exists(),
    reason="the check file lives only in the main checkout",
)
def test_real_check_file_has_forty_sentences():
    sentences = repro.load_sentences(repro.DEFAULT_CHECK_PATH)
    assert len(sentences) == 40
    assert all(
        "period" not in [w.lower() for w in words] for words in sentences
    )


# ---------------------------------------------------------------------------
# Word splitting and normalisation
# ---------------------------------------------------------------------------


def test_normalize_words_lowercases_and_drops_punctuation():
    assert repro.normalize_words("The Weather, was cool.\nNext!") == [
        "the", "weather", "was", "cool", "next",
    ]


def test_count_periods_finds_a_lost_period():
    # diag-B lost the "." after sentence 20: the word counter cannot see it.
    assert repro.count_periods("a b. c d. e.", "a b c d. e.") == (1, 0)


def test_count_periods_finds_an_extra_period():
    assert repro.count_periods("a b. c.", "a. b. c.") == (0, 1)


def test_count_periods_is_zero_when_every_period_landed():
    assert repro.count_periods("a b. c.", "A b. C.") == (0, 0)


def test_count_periods_counts_a_lost_and_a_repeated_period_separately():
    # The totals are equal (2 and 2), so a count of totals reads (0, 0). The
    # period after "a" is lost and a second period follows "b".
    assert repro.count_periods("a. b.", "a b..") == (1, 1)


def test_count_periods_keeps_a_lost_and_a_shifted_repeated_period_apart():
    # wh-dictation-paste-loss-load.1.4: the period after "one" is lost, the
    # period after "tell" is typed twice, and the word "two" is lost while
    # its period lands after "ran". One alignment of words and periods
    # together paired the wrong periods and read (0, 0).
    assert repro.count_periods(
        "one. tell. ran ran two. sat. time two.",
        "one tell.. ran ran. sat. time two.",
    ) == (1, 1)


def test_count_periods_finds_no_period_error_when_only_words_were_lost():
    # wh-dictation-paste-loss-load.1.4: "b" and the second "a" are lost, and
    # both periods land after the first "a". No period paste was lost or
    # typed twice, but one alignment of words and periods together read
    # (1, 1), which would fail a clean run on its period count.
    assert repro.count_periods("a b. a.", "a..") == (0, 0)


def test_count_periods_prefers_the_history_with_fewer_period_errors():
    # The first "c" is typed twice and the second "c" is lost: no period
    # error. An alignment that keeps both words matched instead reads a
    # lost period after the first "c" and a repeated one after the second.
    assert repro.count_periods("c. c.", "c c..") == (0, 0)


def test_count_periods_reads_no_history_that_needs_a_stray_word():
    # wh-dictation-paste-loss-load.1.5: "ran", the period after "ran",
    # "cat", and the period after "a" are lost, and the period after "cat"
    # is typed twice. Only that history explains the text. An alignment
    # that matched a third period read (1, 0), because it took the later
    # "a" as an extra word, and a lost or repeated paste never puts an
    # extra word there.
    assert repro.count_periods("ran. one. cat. a.", "one... a") == (2, 1)


def test_expected_text_ends_each_sentence_with_a_period():
    assert repro.expected_text([["a", "b"], ["c"]]) == "a b. c."


# ---------------------------------------------------------------------------
# Timing plan
# ---------------------------------------------------------------------------


def test_build_plan_orders_start_words_period_end():
    plan = repro.build_plan([["a", "b"]], 0.1, 1.0, id_prefix="p")
    assert [(s.kind, s.text) for s in plan] == [
        ("start", ""), ("word", "a"), ("word", "b"), ("word", "."),
        ("end", ""),
    ]
    assert [s.utterance_id for s in plan] == ["p-s01"] * 5


def test_build_plan_spaces_words_by_the_word_gap():
    plan = repro.build_plan([["a", "b"]], 0.1, 1.0)
    offsets = [s.offset_s for s in plan]
    assert offsets == pytest.approx([0.0, 0.0, 0.1, 0.2, 0.2])


def test_build_plan_starts_next_sentence_a_sentence_gap_after_the_last_send():
    plan = repro.build_plan([["a"], ["b"]], 0.1, 1.0)
    first_end = [s for s in plan if s.sentence == 1 and s.kind == "end"][0]
    second_start = [s for s in plan if s.sentence == 2 and s.kind == "start"][0]
    assert first_end.offset_s == pytest.approx(0.1)
    assert second_start.offset_s == pytest.approx(1.1)
    assert second_start.utterance_id != first_end.utterance_id


def test_build_plan_sends_one_word_payload_per_word_plus_period():
    sentences = [["a", "b", "c"], ["d"]]
    plan = repro.build_plan(sentences, 0.0, 0.0)
    assert sum(1 for s in plan if s.kind == "word") == 4 + 2


def test_unicode_texts_apply_spacing_and_sentence_capitals():
    pieces = repro.unicode_texts([["the", "cat"], ["it", "ran"]])
    assert pieces == ["The", " cat", ".", " It", " ran", "."]


# ---------------------------------------------------------------------------
# Missing, duplicated, out-of-order counter
# ---------------------------------------------------------------------------


def _count(expected: str, got: str):
    return repro.compare_words(
        repro.normalize_words(expected), repro.normalize_words(got)
    )


def test_identical_text_has_no_differences():
    result = _count("a b c d.", "A b, c d")
    assert (result.missing, result.duplicated, result.out_of_order,
            result.cut, result.extra) == (0, 0, 0, 0, 0)
    assert result.expected == result.got == 4
    assert result.differences == []


def test_missing_words_are_counted():
    result = _count("one two three four five", "one four five")
    assert result.missing == 2
    assert (result.duplicated, result.out_of_order, result.cut) == (0, 0, 0)


def test_duplicated_word_is_counted():
    result = _count("the cat sat", "the the cat sat")
    assert result.duplicated == 1
    assert (result.missing, result.out_of_order) == (0, 0)


def test_duplicated_block_is_counted():
    result = _count("a b c d", "a b c d b c")
    assert result.duplicated == 2
    assert result.missing == 0


def test_moved_block_counts_as_out_of_order_not_missing_or_duplicated():
    result = _count("a b c d e f g h", "a b g h c d e f")
    assert result.out_of_order > 0
    assert (result.missing, result.duplicated) == (0, 0)
    assert result.got == result.expected


def test_two_swapped_words_count_as_out_of_order():
    result = _count("one two three", "one three two")
    assert result.out_of_order == 1
    assert (result.missing, result.duplicated) == (0, 0)


def test_cut_word_is_counted_as_cut_and_not_missing():
    result = _count("walk in the morning", "walk in the mor")
    assert result.cut == 1
    assert result.missing == 0


def test_unrelated_extra_word_is_extra_not_duplicated():
    result = _count("a b c", "a zzz b c")
    assert result.extra == 1
    assert result.duplicated == 0


def test_missing_and_duplicated_in_one_text():
    result = _count("a b c d e", "a b b d e e")
    assert result.missing == 1  # c
    assert result.duplicated == 2  # b, e


def test_empty_document_misses_every_word():
    result = _count("a b c", "")
    assert result.missing == 3
    assert result.got == 0


# ---------------------------------------------------------------------------
# RESULT line
# ---------------------------------------------------------------------------


def _record(**overrides):
    record = {
        "method": "paste", "listeners": "real", "expected": 100, "got": 98,
        "missing": 3, "duplicated": 1, "out_of_order": 2, "cut": 0,
        "missing_periods": 1, "extra_periods": 0,
        "elapsed_s": 12.34, "child_exited": True,
        "pre_paste_settle_ms": 0, "paste_check": "on",
    }
    record.update(overrides)
    return record


def test_result_line_carries_every_count_and_the_mode():
    line = repro.format_result_line("load.1", _record())
    assert line == (
        "RESULT run=load.1 method=paste listeners=real "
        "pre_paste_settle_ms=0 paste_check=on expected=100 got=98 "
        "missing=3 duplicated=1 out_of_order=2 cut=0 "
        "missing_periods=1 extra_periods=0 elapsed_s=12.3 "
        "child_exited=true"
    )


def test_result_line_shows_the_pre_paste_settle():
    line = repro.format_result_line("x", _record(pre_paste_settle_ms=50.0))
    assert "pre_paste_settle_ms=50 " in line


def test_result_line_shows_inert_listeners_and_failed_exit():
    line = repro.format_result_line(
        "x", _record(listeners="inert", child_exited=False)
    )
    assert "listeners=inert" in line
    assert line.endswith("child_exited=false")


def test_result_line_without_a_child_says_na():
    line = repro.format_result_line(
        "x", _record(method="unicode", listeners="none", child_exited=None)
    )
    assert line.endswith("child_exited=na")


# ---------------------------------------------------------------------------
# Child exit check
# ---------------------------------------------------------------------------


def _process(alive_after_join: bool, exitcode=0, pid=4321):
    process = MagicMock()
    process.pid = pid
    process.exitcode = exitcode
    process.is_alive.return_value = alive_after_join
    return process


def test_child_that_exited_and_whose_pid_is_gone_is_exited():
    process = _process(False, exitcode=0)
    info = repro.verify_child_exit(
        process, 1.0, pid_exists_fn=lambda pid: False
    )
    assert info == {
        "pid": 4321, "exitcode": 0, "terminated": False,
        "child_exited": True,
    }
    process.terminate.assert_not_called()


def test_child_still_alive_after_join_is_terminated_and_not_exited():
    process = _process(True, exitcode=1)
    info = repro.verify_child_exit(
        process, 1.0, pid_exists_fn=lambda pid: False
    )
    process.terminate.assert_called_once_with()
    assert info["terminated"] is True
    assert info["child_exited"] is False


def test_child_whose_pid_still_exists_is_not_exited():
    process = _process(False, exitcode=0)
    seen = []

    def exists(pid):
        seen.append(pid)
        return True

    info = repro.verify_child_exit(process, 1.0, pid_exists_fn=exists)
    assert seen == [4321]
    assert info["child_exited"] is False
    process.terminate.assert_not_called()


def test_join_timeout_is_passed_to_the_process():
    process = _process(False)
    repro.verify_child_exit(process, 7.5, pid_exists_fn=lambda pid: False)
    process.join.assert_called_with(7.5)


def test_ctypes_pid_check_sees_this_process_and_a_missing_pid():
    assert repro._pid_exists_ctypes(os.getpid()) is True
    assert repro._pid_exists_ctypes(0xFFFFFFF0) is False


def test_pid_exists_sees_this_process():
    assert repro.pid_exists(os.getpid()) is True


# ---------------------------------------------------------------------------
# Input child entry: the listener stand-in
# ---------------------------------------------------------------------------


@pytest.fixture
def pynput_restored(monkeypatch):
    """Snapshot the pynput listener classes so a patch is undone."""
    from pynput import keyboard, mouse

    monkeypatch.setattr(mouse, "Listener", mouse.Listener)
    monkeypatch.setattr(keyboard, "Listener", keyboard.Listener)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return mouse, keyboard


def _fake_input_proc(monkeypatch, seen: dict):
    from pynput import keyboard, mouse

    def input_process_main(*args):
        seen["args"] = args
        seen["mouse"] = mouse.Listener
        seen["keyboard"] = keyboard.Listener

    module = types.ModuleType("input_proc")
    module.input_process_main = input_process_main
    monkeypatch.setitem(sys.modules, "input_proc", module)


def test_inert_flag_puts_the_stand_in_in_place_before_the_call(
    monkeypatch, pynput_restored,
):
    seen: dict = {}
    _fake_input_proc(monkeypatch, seen)
    repro.input_child_main("shm", "cmd", "ready", "queue", "stop", True)
    assert seen["mouse"] is repro._InertListener
    assert seen["keyboard"] is repro._InertListener
    assert seen["args"] == ("shm", "cmd", "ready", "queue", "stop")


def test_default_mode_does_not_patch_pynput(monkeypatch, pynput_restored):
    mouse, keyboard = pynput_restored
    real_mouse, real_keyboard = mouse.Listener, keyboard.Listener
    seen: dict = {}
    _fake_input_proc(monkeypatch, seen)
    repro.input_child_main("shm", "cmd", "ready", "queue", "stop", False)
    assert seen["mouse"] is real_mouse
    assert seen["keyboard"] is real_keyboard
    assert seen["mouse"] is not repro._InertListener
    assert seen["args"] == ("shm", "cmd", "ready", "queue", "stop")


def test_inert_listener_methods_do_nothing():
    listener = repro._InertListener(on_click=lambda *a: None)
    assert listener.start() is None
    assert listener.stop() is None
    assert listener.join() is None
    assert listener.running is False


def test_inert_flag_is_off_by_default(monkeypatch, tmp_path):
    captured = []

    def fake_run_once(args, sentences, label, index):
        captured.append(args)
        return {"status": "failed", "reason": "stop here"}

    monkeypatch.setattr(repro, "run_once", fake_run_once)
    check = tmp_path / "check.md"
    check.write_text(CHECK_TEXT, encoding="utf-8")
    repro.main(["--check-path", str(check)])
    assert captured[0].inert_listeners is False
    assert captured[0].method == "paste"
    assert captured[0].word_gap_ms == 112.0
    assert captured[0].sentence_gap_ms == 1000.0


# ---------------------------------------------------------------------------
# Pre-paste settle (wrapper around verified_press_keys)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clipboard_module(monkeypatch):
    """A stand-in ui.clipboard_operations with a recording press function."""
    calls: list = []

    def original(*keys, **kwargs):
        calls.append(("press", keys))
        return (True, len(keys), len(keys))

    module = types.ModuleType("ui.clipboard_operations")
    module.verified_press_keys = original
    monkeypatch.setitem(sys.modules, "ui.clipboard_operations", module)
    return module, original, calls


def test_zero_settle_patches_nothing(
    monkeypatch, pynput_restored, fake_clipboard_module,
):
    module, original, _calls = fake_clipboard_module
    seen: dict = {}
    _fake_input_proc(monkeypatch, seen)
    repro.input_child_main("shm", "cmd", "ready", "queue", "stop", False, 0)
    assert module.verified_press_keys is original
    repro.input_child_main("shm", "cmd", "ready", "queue", "stop", False)
    assert module.verified_press_keys is original


def test_positive_settle_is_installed_before_input_process_main(
    monkeypatch, pynput_restored, fake_clipboard_module,
):
    module, original, _calls = fake_clipboard_module
    seen: dict = {}
    from pynput import keyboard

    def input_process_main(*args):
        seen["patched"] = module.verified_press_keys
        seen["args"] = args

    fake = types.ModuleType("input_proc")
    fake.input_process_main = input_process_main
    monkeypatch.setitem(sys.modules, "input_proc", fake)
    repro.input_child_main("shm", "cmd", "ready", "queue", "stop", False, 40)
    assert seen["patched"] is not original
    assert seen["args"] == ("shm", "cmd", "ready", "queue", "stop")
    assert keyboard.Listener is not repro._InertListener


def test_ctrl_v_sleeps_before_the_original_call(
    monkeypatch, fake_clipboard_module,
):
    module, _original, calls = fake_clipboard_module
    monkeypatch.setattr(
        repro.time, "sleep", lambda seconds: calls.append(("sleep", seconds))
    )
    repro.install_pre_paste_settle(50)
    result = module.verified_press_keys("ctrl", "v")
    assert calls == [("sleep", 0.05), ("press", ("ctrl", "v"))]
    assert result == (True, 2, 2)


@pytest.mark.parametrize(
    "keys",
    [("ctrl", "c"), ("delete",), ("v",), ("ctrl", "shift", "v"),
     ("v", "ctrl")],
)
def test_other_keys_pass_through_without_a_sleep(
    monkeypatch, fake_clipboard_module, keys,
):
    module, _original, calls = fake_clipboard_module
    monkeypatch.setattr(
        repro.time, "sleep", lambda seconds: calls.append(("sleep", seconds))
    )
    repro.install_pre_paste_settle(50)
    result = module.verified_press_keys(*keys)
    assert calls == [("press", keys)]
    assert result == (True, len(keys), len(keys))


def test_no_paste_check_makes_every_paste_skip_the_check(monkeypatch):
    # The origin/dev baseline in the load session: the Input child pastes
    # as before the fix (wh-dictation-paste-loss-load).
    import types
    module = types.SimpleNamespace(
        prepare_paste_check=lambda hwnd, cls: ("check", "ready"),
    )
    monkeypatch.setattr(
        repro.importlib, "import_module", lambda name: module, raising=False,
    )
    repro.install_no_paste_check()
    assert module.prepare_paste_check(1, "RichEditD2DPT") == (
        None, "class_out_of_scope",
    )


def test_result_line_shows_the_paste_check_mode():
    assert " paste_check=off " in repro.format_result_line(
        "x", _record(paste_check="off"),
    )


def test_paste_check_is_on_by_default(monkeypatch, tmp_path):
    captured = []

    def fake_run_once(args, sentences, label, index):
        captured.append(args)
        return {"status": "failed", "reason": "stop here"}

    monkeypatch.setattr(repro, "run_once", fake_run_once)
    check = tmp_path / "check.md"
    check.write_text(CHECK_TEXT, encoding="utf-8")
    repro.main(["--check-path", str(check)])
    assert captured[0].no_paste_check is False
    repro.main(["--check-path", str(check), "--no-paste-check"])
    assert captured[1].no_paste_check is True


def test_pre_paste_settle_is_zero_by_default(monkeypatch, tmp_path):
    captured = []

    def fake_run_once(args, sentences, label, index):
        captured.append(args)
        return {"status": "failed", "reason": "stop here"}

    monkeypatch.setattr(repro, "run_once", fake_run_once)
    check = tmp_path / "check.md"
    check.write_text(CHECK_TEXT, encoding="utf-8")
    repro.main(["--check-path", str(check)])
    assert captured[0].pre_paste_settle_ms == 0
    repro.main(["--check-path", str(check), "--pre-paste-settle-ms", "75"])
    assert captured[1].pre_paste_settle_ms == 75


# ---------------------------------------------------------------------------
# Own-window foreground (boss ruling 00:35, 2026-10-20)
# ---------------------------------------------------------------------------


def _focus(window_pid, *, before_pids=frozenset({16488}), started=200.0):
    """Run focus_own_window for window 777 with a launch at time 100.0."""
    stolen = []

    def create_time(pid):
        if isinstance(started, Exception):
            raise started
        return started

    refusal = repro.focus_own_window(
        777, launched_pid=500, launch_time=100.0, before_pids=before_pids,
        pid_of=lambda handle: window_pid, create_time=create_time,
        steal=lambda handle: stolen.append(handle) or True,
    )
    return refusal, stolen


def test_focus_own_window_brings_forward_the_window_of_the_launched_process():
    # The launched process needs no start-time read: a failed read must
    # not refuse it.
    assert _focus(500, started=OSError("not read")) == (None, [777])


def test_focus_own_window_accepts_a_notepad_process_started_after_the_launch():
    # The Windows 11 notepad.exe in System32 can hand the window to the
    # packaged Notepad, a second process that the launch started.
    assert _focus(900, started=100.5) == (None, [777])


def test_focus_own_window_refuses_a_window_of_a_notepad_open_before_the_run():
    refusal, stolen = _focus(16488, started=200.0)
    assert refusal is not None and "16488" in refusal
    assert stolen == []


def test_focus_own_window_refuses_a_process_that_started_before_the_launch():
    refusal, stolen = _focus(900, started=99.0)
    assert refusal is not None and "900" in refusal
    assert stolen == []


def test_focus_own_window_refuses_when_the_start_time_cannot_be_read():
    refusal, stolen = _focus(900, started=OSError("gone"))
    assert refusal is not None and "gone" in refusal
    assert stolen == []


# wh-dictation-paste-loss-load.1.6: the window is proven to derive from this
# launch, once more immediately before the first keystroke.
def _proof(window_pid, *, current, before=frozenset({16488}), launched=500):
    def list_notepad_pids():
        if isinstance(current, Exception):
            raise current
        return current

    return repro.own_window_proof(
        window_pid, launched_pid=launched, before_notepad_pids=set(before),
        list_notepad_pids=list_notepad_pids,
    )


def test_proof_accepts_the_window_of_the_launched_process_on_its_pid():
    # Two other new processes would defeat the single-process proof, so
    # only the launched pid can be what accepts this window.
    assert _proof(500, current={16488, 500, 901, 902}) == (
        "launched pid", None,
    )


def test_proof_accepts_one_new_process_besides_the_launched_one():
    # The System32 notepad.exe (500) handed its window to the packaged
    # Notepad (901); the launched process may or may not still be listed.
    assert _proof(901, current={16488, 500, 901}) == (
        "single new process", None,
    )
    assert _proof(901, current={16488, 901}) == ("single new process", None)


def test_proof_refuses_two_new_processes_besides_the_launched_one():
    # The finding's sequence: David opens a Notepad (902) while the launch
    # hands its window to the packaged Notepad (901).
    proof, refusal = _proof(902, current={16488, 500, 901, 902})
    assert proof is None and refusal is not None and "2" in refusal


def test_proof_refuses_a_window_that_is_not_the_single_new_process():
    # 901 is the one new notepad.exe, but the window belongs to 902: a
    # process that psutil does not name notepad.exe, or a reused ID. The
    # single new process proves only its own window.
    proof, refusal = _proof(902, current={16488, 500, 901})
    assert proof is None and refusal is not None and "902" in refusal


def test_proof_refuses_when_no_new_process_exists():
    proof, refusal = _proof(901, current={16488, 500})
    assert proof is None and refusal is not None and "0" in refusal


def test_proof_does_not_count_a_notepad_open_before_the_run():
    # 16488 was open before the run, so only 901 is new.
    assert _proof(901, current={16488, 901}) == ("single new process", None)


def test_proof_refuses_when_the_process_list_cannot_be_read():
    proof, refusal = _proof(901, current=OSError("denied"))
    assert proof is None and refusal is not None and "denied" in refusal


def test_proof_by_launched_pid_needs_no_process_list():
    assert _proof(500, current=OSError("denied")) == ("launched pid", None)


# wh-dictation-paste-loss-load.1.8: the cleanup closes the window only
# while its handle still names the window that the run accepted.
_OWN = ("Notepad", 4242, 100.5)


def _cleanup(identity, now, *, alive=True):
    def read_identity(handle):
        if isinstance(now, Exception):
            raise now
        return now

    return repro.cleanup_refusal(
        777, identity, is_window=lambda handle: alive,
        read_identity=read_identity,
    )


def test_cleanup_closes_the_window_that_the_run_accepted():
    assert _cleanup(_OWN, _OWN) is None


def test_cleanup_leaves_a_handle_that_now_names_another_process():
    refusal = _cleanup(_OWN, ("Notepad", 5151, 100.5))
    assert refusal is not None and "777" in refusal


def test_cleanup_leaves_a_handle_whose_process_started_again():
    # The same process id with a later start time is a new process.
    assert _cleanup(_OWN, ("Notepad", 4242, 900.0)) is not None


def test_cleanup_leaves_a_handle_of_another_window_class():
    assert _cleanup(_OWN, ("OpusApp", 4242, 100.5)) is not None


def test_cleanup_leaves_a_window_that_the_run_never_accepted():
    # focus_own_window refused it, so it may be David's window. The
    # identity comparison would also refuse; the reason must say why.
    refusal = _cleanup(None, _OWN)
    assert refusal is not None and "never accepted" in refusal


def test_cleanup_leaves_a_window_whose_identity_cannot_be_read():
    assert _cleanup(_OWN, OSError("access denied")) is not None


def test_cleanup_has_nothing_to_close_when_the_window_is_gone():
    assert _cleanup(_OWN, _OWN, alive=False) is not None


# wh-dictation-paste-loss-load.1.11 (boss ruling 13:12, 2026-10-20): the
# focused control must be inside the run's own window, and the run stops
# before the next sentence when the foreground leaves that window. David's
# own Notepad window (handle 926246) must never get typed text.
_OWN_HANDLE = 777
_DAVIDS_WINDOW = 926246


class _Control:
    def __init__(self, handle, class_name="RichEditD2DPT", text=""):
        self._handle = handle
        self.ClassName = class_name
        self.pattern_requests = 0
        self._text = text

    @property
    def NativeWindowHandle(self):
        if isinstance(self._handle, Exception):
            raise self._handle
        return self._handle

    def GetPattern(self, pattern_id):
        self.pattern_requests += 1
        text = self._text
        return types.SimpleNamespace(
            DocumentRange=types.SimpleNamespace(GetText=lambda count: text)
        )


def _auto(control):
    return types.SimpleNamespace(
        GetFocusedControl=lambda: control,
        PatternId=types.SimpleNamespace(TextPattern="TextPattern"),
    )


def _roots(mapping):
    def root_of(handle):
        result = mapping[handle]
        if isinstance(result, Exception):
            raise result
        return result

    return root_of


def test_measure_document_accepts_a_control_inside_the_own_window():
    control = _Control(888)
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({888: _OWN_HANDLE}),
    )
    assert refusal is None and pattern is not None


def test_measure_document_refuses_a_control_inside_another_window():
    # The class check alone passes: David's Notepad has the same class.
    control = _Control(999)
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({999: _DAVIDS_WINDOW}),
    )
    assert pattern is None
    assert refusal is not None and str(_DAVIDS_WINDOW) in refusal
    # The refusal comes before the text read: David's text is never read.
    assert control.pattern_requests == 0


def test_measure_document_refuses_a_control_whose_handle_cannot_be_read():
    control = _Control(OSError("no handle"))
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({}),
    )
    assert pattern is None and refusal is not None and "no handle" in refusal
    assert control.pattern_requests == 0


def test_measure_document_refuses_a_control_with_handle_zero():
    control = _Control(0)
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({0: _OWN_HANDLE}),
    )
    assert pattern is None and refusal is not None
    assert control.pattern_requests == 0


def test_measure_document_refuses_when_the_top_level_window_cannot_be_read():
    control = _Control(888)
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({888: OSError("denied")}),
    )
    assert pattern is None and refusal is not None and "denied" in refusal
    assert control.pattern_requests == 0


def test_measure_document_refuses_a_top_level_window_of_zero():
    # GetAncestor answers 0 when it fails.
    control = _Control(888)
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({888: 0}),
    )
    assert pattern is None and refusal is not None


def test_measure_document_still_refuses_a_non_empty_document():
    control = _Control(888, text="David's words")
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({888: _OWN_HANDLE}),
    )
    assert pattern is None and refusal is not None and "not empty" in refusal


def test_measure_document_still_refuses_another_control_class():
    control = _Control(888, class_name="Edit")
    pattern, refusal = repro._measure_document(
        _auto(control), _OWN_HANDLE, _roots({888: _OWN_HANDLE}),
    )
    assert pattern is None and refusal is not None and "Edit" in refusal


def test_measure_document_refuses_when_nothing_has_focus():
    pattern, refusal = repro._measure_document(
        _auto(None), _OWN_HANDLE, _roots({}),
    )
    assert pattern is None and refusal == "no focused control"


def _foreground(value):
    def read():
        if isinstance(value, Exception):
            raise value
        return value

    return read


def test_foreground_stop_reason_is_none_while_the_own_window_is_in_front():
    assert repro.foreground_stop_reason(
        _foreground(_OWN_HANDLE), _OWN_HANDLE,
    ) is None


def test_foreground_stop_reason_names_a_window_that_took_the_foreground():
    reason = repro.foreground_stop_reason(
        _foreground(_DAVIDS_WINDOW), _OWN_HANDLE,
    )
    assert reason is not None and str(_DAVIDS_WINDOW) in reason


def test_foreground_stop_reason_stops_when_no_window_has_the_foreground():
    # GetForegroundWindow answers 0 between two windows.
    assert repro.foreground_stop_reason(_foreground(0), _OWN_HANDLE) is not None


def test_foreground_stop_reason_stops_when_the_foreground_cannot_be_read():
    reason = repro.foreground_stop_reason(
        _foreground(OSError("no desktop")), _OWN_HANDLE,
    )
    assert reason is not None and "no desktop" in reason


def _drive(stop_check, sentences=None):
    """Run _run_sends over two short sentences; return (sent, outcome)."""
    import asyncio

    sentences = sentences or [["a", "b"], ["c", "d"]]
    plan = repro.build_plan(sentences, 0.0, 0.0)
    sent: list = []

    async def send_word(step):
        sent.append(step.text)

    async def send_command(step):
        sent.append(step.kind)

    outcome = asyncio.run(
        repro._run_sends(plan, send_word, send_command, [], stop_check)
    )
    return sent, outcome


def test_run_sends_without_a_stop_check_sends_everything():
    sent, (failures, stop_reason) = _drive(None)
    assert stop_reason is None and failures == []
    assert sent == [
        "start", "a", "b", ".", "end", "start", "c", "d", ".", "end",
    ]


def test_run_sends_stops_before_the_next_sentence_and_sends_nothing_more():
    def stop_check(step):
        if step.sentence == 2:
            return "the foreground left the window"
        return None

    sent, (failures, stop_reason) = _drive(stop_check)
    assert sent == ["start", "a", "b", ".", "end"]
    assert stop_reason is not None
    assert "sentence 2" in stop_reason
    assert "the foreground left the window" in stop_reason


def test_run_sends_checks_before_the_first_send():
    sent, (failures, stop_reason) = _drive(lambda step: "away")
    assert sent == [] and stop_reason is not None
    assert "sentence 1" in stop_reason


def test_run_sends_stops_inside_a_sentence_before_the_next_word():
    def stop_check(step):
        return "away" if step.text == "b" else None

    sent, (failures, stop_reason) = _drive(stop_check)
    assert sent == ["start", "a"]
    assert stop_reason is not None and "sentence 1" in stop_reason


def test_mark_stopped_gives_the_record_the_stopped_status_and_the_reason():
    record = repro.mark_stopped({"run": "r.1", "status": "failed"}, "away")
    assert record["status"] == "stopped"
    assert record["reason"] == "away"
    assert record["status"] != "measured"
    assert "missing" not in record and "duplicated" not in record


def test_a_stopped_run_is_not_a_result_and_ends_the_loop(
    monkeypatch, tmp_path, capsys,
):
    calls = []

    def fake_run_once(args, sentences, label, index):
        calls.append(index)
        return repro.mark_stopped({"run": f"r.{index}"}, "the foreground left")

    monkeypatch.setattr(repro, "run_once", fake_run_once)
    check = tmp_path / "check.md"
    check.write_text(CHECK_TEXT, encoding="utf-8")
    out = tmp_path / "out.json"
    code = repro.main([
        "--check-path", str(check), "--runs", "3", "--out", str(out),
    ])
    assert code == 2
    assert calls == [1]
    printed = capsys.readouterr().out
    assert "stopped" in printed and "the foreground left" in printed
    import json

    records = json.loads(out.read_text(encoding="utf-8"))["runs"]
    assert [r["status"] for r in records] == ["stopped"]
