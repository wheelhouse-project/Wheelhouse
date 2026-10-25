"""Tests for ui.paste_landing (wh-dictation-paste-loss-load).

The module asks a RichEditD2DPT control for its text length and its
selection, with SendMessageTimeout, so verified_paste can tell whether a
Ctrl+V reached the text. Every Win32 call is replaced here.
"""

import ctypes
import ctypes.wintypes as wt

import pytest

from ui import paste_landing as pl


class FakeControl:
    """A control that answers WM_GETTEXTLENGTH from a list and the selection."""

    def __init__(self, lengths, selection=(5, 5), fail=()):
        self.lengths = list(lengths)
        self.selection = selection
        self.fail = set(fail)
        self.sent = []

    def send(self, hwnd, msg):
        self.sent.append((hwnd, msg))
        if msg in self.fail:
            return None
        if msg == pl.WM_GETTEXTLENGTH:
            if len(self.lengths) > 1:
                return self.lengths.pop(0)
            return self.lengths[0]
        raise AssertionError(f"unexpected message {msg:#x}")

    def query_selection(self, hwnd):
        self.sent.append((hwnd, pl.EM_GETSEL))
        if pl.EM_GETSEL in self.fail:
            return None
        return self.selection


@pytest.fixture
def control(monkeypatch):
    fake = FakeControl([10])
    monkeypatch.setattr(pl, "_send_with_timeout", fake.send)
    monkeypatch.setattr(pl, "_query_selection", fake.query_selection)
    monkeypatch.setattr(pl, "focused_control_hwnd", lambda root: 777)
    monkeypatch.setattr(pl, "_class_name", lambda hwnd: "RichEditD2DPT")
    return fake


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------------------
# prepare_paste_check
# ---------------------------------------------------------------------------


def test_prepare_returns_the_focused_control_and_its_length(control):
    check, reason = pl.prepare_paste_check(100, "RichEditD2DPT")
    assert check == pl.PasteCheck(hwnd=777, length_before=10, root_hwnd=100)
    assert reason == "ready"


def test_prepare_skips_every_other_class(control):
    # A browser or Electron window answers WM_GETTEXTLENGTH with its caption
    # length, which a paste never changes: it must never get a check.
    check, reason = pl.prepare_paste_check(100, "Chrome_RenderWidgetHostHWND")
    assert check is None
    assert reason == "class_out_of_scope"
    assert control.sent == []


def test_prepare_skips_when_no_target_window(control):
    assert pl.prepare_paste_check(None, "RichEditD2DPT") == (None, "no_target_window")


def test_prepare_skips_when_the_focused_window_is_another_class(control, monkeypatch):
    monkeypatch.setattr(pl, "_class_name", lambda hwnd: "Edit")
    assert pl.prepare_paste_check(100, "RichEditD2DPT") == (
        None, "focused_class_mismatch",
    )


def test_prepare_skips_when_nothing_has_focus(control, monkeypatch):
    monkeypatch.setattr(pl, "focused_control_hwnd", lambda root: 0)
    assert pl.prepare_paste_check(100, "RichEditD2DPT") == (None, "no_focused_control")


def test_prepare_skips_a_non_empty_selection(control):
    control.selection = (3, 9)
    assert pl.prepare_paste_check(100, "RichEditD2DPT") == (None, "selection_not_empty")


def test_prepare_checks_a_caret_past_65535_characters(control):
    # The old packed EM_GETSEL answer was -1 here and the paste got no check.
    control.selection = (70000, 70000)
    check, reason = pl.prepare_paste_check(100, "RichEditD2DPT")
    assert reason == "ready"
    assert check == pl.PasteCheck(hwnd=777, length_before=10, root_hwnd=100)


def test_prepare_skips_a_selection_past_65535_characters(control):
    control.selection = (10, 35000)
    assert pl.prepare_paste_check(100, "RichEditD2DPT") == (None, "selection_not_empty")


def test_prepare_skips_when_the_selection_query_gets_no_answer(control):
    control.fail.add(pl.EM_GETSEL)
    assert pl.prepare_paste_check(100, "RichEditD2DPT") == (None, "selection_query_failed")


def test_prepare_skips_when_the_length_query_gets_no_answer(control):
    control.fail.add(pl.WM_GETTEXTLENGTH)
    assert pl.prepare_paste_check(100, "RichEditD2DPT") == (None, "length_query_failed")


# ---------------------------------------------------------------------------
# wait_for_length_change
# ---------------------------------------------------------------------------


def test_wait_returns_changed_with_the_new_length(control):
    control.lengths = [10, 10, 14]
    clock = FakeClock()
    result = pl.wait_for_length_change(
        777, 10, wait_s=0.1, clock=clock.clock, sleep=clock.sleep,
    )
    assert result == pl.LengthWait("changed", 14, pytest.approx(0.01))


def test_wait_returns_unchanged_at_the_deadline(control):
    control.lengths = [10]
    clock = FakeClock()
    result = pl.wait_for_length_change(
        777, 10, wait_s=0.1, clock=clock.clock, sleep=clock.sleep,
    )
    assert result.status == "unchanged"
    assert result.length == 10
    assert result.elapsed_s >= 0.1


def test_wait_reads_at_least_once_with_a_zero_wait(control):
    control.lengths = [12]
    clock = FakeClock()
    result = pl.wait_for_length_change(
        777, 10, wait_s=0.0, clock=clock.clock, sleep=clock.sleep,
    )
    assert result.status == "changed"


def test_wait_stops_on_a_failed_query(control):
    control.fail.add(pl.WM_GETTEXTLENGTH)
    clock = FakeClock()
    result = pl.wait_for_length_change(
        777, 10, wait_s=0.1, clock=clock.clock, sleep=clock.sleep,
    )
    assert result.status == "query_failed"
    assert result.length is None


def test_max_growth_of_one_paste_counts_utf16_units_and_line_breaks():
    assert pl.max_growth_of_one_paste("word") == 4
    assert pl.max_growth_of_one_paste("\U0001F600") == 2
    assert pl.max_growth_of_one_paste("\n") == 2
    assert pl.max_growth_of_one_paste("a\nb") == 4
    assert pl.max_growth_of_one_paste("") == 0


# ---------------------------------------------------------------------------
# Win32 wrappers
# ---------------------------------------------------------------------------


def test_send_with_timeout_uses_abort_if_hung(monkeypatch):
    calls = []

    def fake_smt(hwnd, msg, wparam, lparam, flags, timeout):
        calls.append((hwnd, msg, wparam, lparam, flags, timeout))
        return 1, 42

    monkeypatch.setattr(pl.win32gui, "SendMessageTimeout", fake_smt)
    assert pl._send_with_timeout(5, pl.WM_GETTEXTLENGTH) == 42
    hwnd, msg, wparam, lparam, flags, timeout = calls[0]
    assert flags & pl.SMTO_ABORTIFHUNG
    assert 0 < timeout <= 250


class FakeUser32:
    """Stands in for user32: SendMessageTimeoutW writes the selection.

    ctypes.byref(x) keeps x in ``_obj``, so the fake writes the start and
    the end into the two DWORDs the caller passed by reference.
    """

    def __init__(self, selection=(10, 35000), answer=1, error=None):
        self.calls = []

        def send_message_timeout(hwnd, msg, wparam, lparam, flags, timeout, result):
            self.calls.append((hwnd, msg, wparam, lparam, flags, timeout, result))
            if error is not None:
                raise error
            wparam._obj.value, lparam._obj.value = selection
            return answer

        self.SendMessageTimeoutW = send_message_timeout


def _user32(monkeypatch, **kwargs):
    fake = FakeUser32(**kwargs)
    monkeypatch.setattr(pl, "_user32", lambda: fake, raising=False)
    return fake


def test_query_selection_returns_the_start_and_the_end(monkeypatch):
    _user32(monkeypatch, selection=(10, 35000))
    assert pl._query_selection(5) == (10, 35000)


def test_query_selection_answers_for_a_caret_past_65535_characters(monkeypatch):
    _user32(monkeypatch, selection=(70000, 70000))
    assert pl._query_selection(5) == (70000, 70000)


def test_query_selection_sends_em_getsel_with_two_dword_pointers(monkeypatch):
    fake = _user32(monkeypatch)
    pl._query_selection(5)
    hwnd, msg, wparam, lparam, flags, timeout, _result = fake.calls[0]
    assert getattr(hwnd, "value", hwnd) == 5
    assert msg == 0x00B0
    # Two different DWORDs, passed by reference: EM_GETSEL writes the start
    # through wParam and the end through lParam.
    assert type(wparam) is type(ctypes.byref(ctypes.c_ulong()))
    assert type(lparam) is type(wparam)
    assert isinstance(wparam._obj, wt.DWORD)
    assert isinstance(lparam._obj, wt.DWORD)
    assert wparam._obj is not lparam._obj
    assert flags & pl.SMTO_ABORTIFHUNG
    assert timeout == pl.QUERY_TIMEOUT_MS


def test_query_selection_returns_none_when_the_call_times_out(monkeypatch):
    # SendMessageTimeoutW returns 0 on a timeout or a hung target; the two
    # DWORDs then hold nothing that was answered.
    _user32(monkeypatch, selection=(0, 0), answer=0)
    assert pl._query_selection(5) is None


def test_query_selection_returns_none_on_an_error(monkeypatch):
    _user32(monkeypatch, error=OSError("no user32"))
    assert pl._query_selection(5) is None


def test_send_with_timeout_returns_none_on_an_error(monkeypatch):
    def fake_smt(*args):
        raise OSError("timeout")

    monkeypatch.setattr(pl.win32gui, "SendMessageTimeout", fake_smt)
    assert pl._send_with_timeout(5, pl.WM_GETTEXTLENGTH) is None
