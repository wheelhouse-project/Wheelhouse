"""The activate_window answer to an awaited request (wh-safety-word-free-commands, stage S1).

A request that carries a request id is an awaited activate: activate, switch
to, go to and show (new in this change), and close, minimize and maximize (their
hotkey step follows the answer). Input must send exactly one reply, within the
Logic process's wait, and the reply is what decides what the Logic process does:

* no window matched and nothing started, or Windows refused a found window:
  an error reply marked ``refusal`` carrying the message the Input notice would
  have shown, and NO Input notice (the Logic process shows it, or types the
  words, depending on the safety word);
* a program was started but its window is not in front by the verification
  limit: an error reply with ``outcome`` "stopped", so the rule sends no key;
* the window is in front: an ok reply, as before.

A plain activate (no request id) keeps today's notices and sends nothing; the
tests in test_input_proc_activate_window.py pin that.
"""
import os
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest

import input_proc


@pytest.fixture(autouse=True)
def _no_launches_left_over():
    with input_proc._launch_threads_lock:
        input_proc._launch_threads.clear()
    yield
    with input_proc._launch_threads_lock:
        started = list(input_proc._launch_threads)
        input_proc._launch_threads.clear()
    for _, thread in started:
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _no_real_wait_for_a_started_store_window(monkeypatch):
    monkeypatch.setattr(input_proc, "_STORE_WINDOW_WAIT_S", 0.0, raising=False)
    monkeypatch.setattr(
        input_proc, "_foreground_window", lambda: 0x5150, raising=False
    )


def _program(name, target=None):
    p = MagicMock()
    p.name = name
    p.launch_target = target or f"C:\\Start\\{name}.lnk"
    p.app_id = None
    p.fallbacks = ()
    return p


def _store_program(name, app_id):
    p = _program(name)
    p.app_id = app_id
    return p


class _Run:
    """What one awaited activate answered, and what it showed."""

    def __init__(self, monkeypatch, target, *, found_hwnd=None, programs=(),
                 brought_forward=True, foreground=False, request_id="r1",
                 startfile_fails=False, running_store_hwnd=None,
                 find_programs=None):
        self.replies = queue.Queue()
        self.notices = []
        self.startfile_calls = []
        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: found_hwnd
        )
        monkeypatch.setattr(
            input_proc, "_activate_window_impl",
            lambda hwnd, logger: brought_forward,
        )
        monkeypatch.setattr(
            input_proc, "_target_is_foreground",
            lambda t, logger: foreground, raising=False,
        )
        monkeypatch.setattr(
            input_proc, "_find_window_by_app_id",
            lambda app_id, logger: running_store_hwnd,
        )

        def startfile(path):
            self.startfile_calls.append(path)
            if startfile_fails:
                raise OSError("no such program")

        monkeypatch.setattr(os, "startfile", startfile, raising=False)
        self.buffer = MagicMock()
        self.thread = input_proc._handle_activate_window(
            {"target": target}, request_id, self.replies, "activate_window",
            threading.Event(), 50, 10, {}, MagicMock(),
            buffer_manager=self.buffer,
            notify=lambda title, message: self.notices.append(message),
            find_programs=find_programs or (lambda name: list(programs)),
        )
        if self.thread is not None:
            self.thread.join(timeout=10)

    def first(self, timeout=5.0):
        try:
            return self.replies.get(timeout=timeout)
        except queue.Empty:
            return None

    def exactly_one(self):
        reply = self.first()
        assert reply is not None, "no reply was sent"
        time.sleep(0.2)
        extra = []
        while not self.replies.empty():
            extra.append(self.replies.get_nowait())
        assert extra == [], f"more than one reply for one request: {extra}"
        return reply


def _assert_refusal(reply, message):
    assert reply["request_id"] == "r1"
    assert reply["error"] is True
    assert reply["refusal"] is True
    assert reply["message"] == message
    assert reply["action"] == "activate_window"
    assert "outcome" not in reply


class TestNothingMatchedAndNothingStarts:
    def test_no_program_answers_with_the_notice_text_and_shows_none(
        self, monkeypatch
    ):
        run = _Run(monkeypatch, "notepad")
        _assert_refusal(run.exactly_one(), "No program matched notepad.")
        assert run.notices == []
        assert run.startfile_calls == []

    def test_several_programs_answer_with_the_list_and_show_none(
        self, monkeypatch
    ):
        run = _Run(
            monkeypatch, "note",
            programs=[_program("Notepad"), _program("Notes")],
        )
        reply = run.exactly_one()
        assert reply["refusal"] is True
        assert reply["message"].startswith("More than one program matches")
        assert "Notepad" in reply["message"]
        assert run.notices == []
        assert run.startfile_calls == []

    def test_an_exe_that_cannot_start_answers_and_shows_none(self, monkeypatch):
        run = _Run(monkeypatch, "notepad.exe", startfile_fails=True)
        _assert_refusal(run.exactly_one(), "Could not start notepad.exe.")
        assert run.notices == []

    def test_a_spoken_program_that_cannot_start_answers_and_shows_none(
        self, monkeypatch
    ):
        run = _Run(
            monkeypatch, "notepad", programs=[_program("Notepad")],
            startfile_fails=True,
        )
        _assert_refusal(run.exactly_one(), "Could not start Notepad.")
        assert run.notices == []

    def test_a_running_store_program_that_is_refused_answers(self, monkeypatch):
        run = _Run(
            monkeypatch, "terminal",
            programs=[_store_program("Windows Terminal", "WT.App")],
            running_store_hwnd=77, brought_forward=False,
        )
        _assert_refusal(
            run.exactly_one(),
            "Windows refused to bring Windows Terminal to the front",
        )
        assert run.notices == []
        assert run.startfile_calls == []

    def test_a_lookup_that_raises_still_answers_once(self, monkeypatch):
        def lookup(name):
            raise ValueError("registry unreadable")

        run = _Run(monkeypatch, "notepad", find_programs=lookup)
        reply = run.exactly_one()
        assert reply["error"] is True
        assert reply["request_id"] == "r1"

    @pytest.mark.parametrize(
        "target", ["notepad.exe", "notepad"], ids=["exe", "spoken"]
    )
    def test_the_launch_limit_answers_and_shows_no_notice(
        self, monkeypatch, target
    ):
        release = threading.Event()
        started = []

        def blocking_startfile(path):
            started.append(path)
            release.wait(timeout=10)

        monkeypatch.setattr(os, "startfile", blocking_startfile, raising=False)
        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: None
        )
        try:
            for _ in range(input_proc._MAX_LAUNCHES_IN_FLIGHT):
                input_proc._handle_activate_window(
                    {"target": "notepad.exe"}, None, queue.Queue(),
                    "activate_window", threading.Event(), 50, 10, {},
                    MagicMock(), find_programs=lambda name: [],
                )
            deadline = time.monotonic() + 5
            while (len(started) < input_proc._MAX_LAUNCHES_IN_FLIGHT
                   and time.monotonic() < deadline):
                time.sleep(0.005)
            assert len(started) == input_proc._MAX_LAUNCHES_IN_FLIGHT

            run = _Run.__new__(_Run)
            run.replies = queue.Queue()
            run.notices = []
            input_proc._handle_activate_window(
                {"target": target}, "r1", run.replies,
                "activate_window", threading.Event(), 50, 10, {},
                MagicMock(),
                notify=lambda title, message: run.notices.append(message),
                find_programs=lambda name: [_program("Notepad")],
            )
            _assert_refusal(run.exactly_one(), input_proc._LAUNCH_BUSY_NOTICE)
            assert run.notices == []
        finally:
            release.set()

    def test_a_plain_activate_still_shows_its_notice_and_sends_nothing(
        self, monkeypatch
    ):
        run = _Run(monkeypatch, "notepad", request_id=None)
        assert run.notices == ["No program matched notepad."]
        assert run.first(timeout=0.3) is None


class TestAFoundWindowThatWindowsRefuses:
    def test_the_refusal_is_marked_and_shows_no_notice(self, monkeypatch):
        run = _Run(monkeypatch, "notepad.exe", found_hwnd=4242,
                   brought_forward=False)
        _assert_refusal(
            run.exactly_one(),
            "Windows refused to bring notepad.exe to the front",
        )
        assert run.notices == []


class TestAStartedProgramMustComeForward:
    def test_an_exe_whose_window_is_not_in_front_stops_the_rule(
        self, monkeypatch
    ):
        run = _Run(monkeypatch, "notepad.exe", foreground=False)
        reply = run.exactly_one()
        assert reply["request_id"] == "r1"
        assert reply["error"] is True
        assert reply["refusal"] is True
        assert reply["outcome"] == "stopped"
        assert run.startfile_calls == ["notepad.exe"]

    def test_a_spoken_program_whose_window_is_not_in_front_stops_the_rule(
        self, monkeypatch
    ):
        run = _Run(monkeypatch, "notepad", programs=[_program("Notepad")],
                   foreground=False)
        reply = run.exactly_one()
        assert reply["outcome"] == "stopped"
        assert reply["error"] is True

    def test_an_exe_whose_window_comes_to_the_front_answers_ok(
        self, monkeypatch
    ):
        run = _Run(monkeypatch, "notepad.exe", foreground=True)
        reply = run.exactly_one()
        assert reply["request_id"] == "r1"
        assert reply["status"] == "ok"
        assert "error" not in reply

    def test_a_spoken_program_whose_window_comes_to_the_front_answers_ok(
        self, monkeypatch
    ):
        run = _Run(monkeypatch, "notepad", programs=[_program("Notepad")],
                   foreground=True)
        reply = run.exactly_one()
        assert reply["status"] == "ok"

    def test_the_reply_does_not_wait_for_the_store_window_wait(
        self, monkeypatch
    ):
        """The 10 s wait for a started Store window may go on; the answer may not."""
        release = threading.Event()
        entered = threading.Event()

        def long_wait(name, app_id, foreground_at_start, logger):
            entered.set()
            release.wait(timeout=10)

        monkeypatch.setattr(
            input_proc, "_bring_started_store_window_forward", long_wait
        )
        try:
            run = _Run.__new__(_Run)
            run.replies = queue.Queue()
            run.notices = []
            monkeypatch.setattr(
                input_proc, "_find_window_by_target", lambda t, logger: None
            )
            monkeypatch.setattr(
                input_proc, "_find_window_by_app_id", lambda a, logger: None
            )
            monkeypatch.setattr(
                input_proc, "_target_is_foreground",
                lambda t, logger: False, raising=False,
            )
            monkeypatch.setattr(
                os, "startfile", lambda path: None, raising=False
            )
            input_proc._handle_activate_window(
                {"target": "terminal"}, "r1", run.replies, "activate_window",
                threading.Event(), 50, 10, {}, MagicMock(),
                notify=lambda title, message: run.notices.append(message),
                find_programs=lambda name: [
                    _store_program("Windows Terminal", "WT.App")
                ],
            )
            assert entered.wait(timeout=5)
            reply = run.first(timeout=3.0)
            assert reply is not None, "the reply waited for the Store wait"
            assert reply["outcome"] == "stopped"
        finally:
            release.set()

    def test_a_running_store_program_that_is_brought_forward_answers_ok(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            input_proc, "_verify_window_activation",
            lambda target, request_id, response_queue, action, *rest: (
                response_queue.put({
                    "request_id": request_id, "status": "ok",
                    "path": "heuristic_done", "action": action,
                })
            ),
        )
        run = _Run(
            monkeypatch, "terminal",
            programs=[_store_program("Windows Terminal", "WT.App")],
            running_store_hwnd=77, brought_forward=True,
        )
        reply = run.exactly_one()
        assert reply["status"] == "ok"
        assert run.startfile_calls == []


class TestOneAnswerPerRequest:
    """Whichever thread answers first wins; every later answer is dropped."""

    def _reply(self, request_queue):
        return input_proc._ActivateReply(
            "r1", request_queue, "activate_window", "notepad", 50, 10, 0,
            MagicMock(),
        )

    def test_a_second_answer_is_dropped(self):
        replies = queue.Queue()
        reply = self._reply(replies)
        assert reply.refuse("first") is True
        assert reply.put({"request_id": "r1", "status": "ok"}) is False
        assert reply.stop("third") is False
        sent = []
        while not replies.empty():
            sent.append(replies.get_nowait())
        assert [r["message"] for r in sent] == ["first"]

    def test_an_error_after_the_answer_is_dropped(self, monkeypatch):
        # The verification answers ok, then something on the same thread
        # raises: the handler's error reply must not be a second answer.
        def answer_then_raise(target, request_id, response_queue, action, *rest):
            response_queue.put({
                "request_id": request_id, "status": "ok",
                "path": "foreground_done", "action": action,
            })
            raise RuntimeError("raised after the answer")

        monkeypatch.setattr(
            input_proc, "_verify_window_activation", answer_then_raise
        )
        run = _Run(monkeypatch, "notepad.exe", found_hwnd=4242,
                   brought_forward=True)
        reply = run.exactly_one()
        assert reply["status"] == "ok"

    def test_a_verification_that_raises_stops_the_rule(self, monkeypatch):
        # A crash while the started window is being checked must still
        # answer, or the Logic process waits out its whole timeout; and the
        # answer is the stop, because nothing proved the window is in front.
        def crash(target, logger):
            raise RuntimeError("window lookup crashed")

        monkeypatch.setattr(input_proc, "_target_is_foreground", crash)
        replies = queue.Queue()
        reply = self._reply(replies)
        reply.started()
        try:
            answer = replies.get(timeout=5)
        except queue.Empty:
            pytest.fail("the verification crashed and nothing was answered")
        assert answer["error"] is True
        assert answer["refusal"] is True
        assert answer["outcome"] == "stopped"
        assert answer["request_id"] == "r1"
