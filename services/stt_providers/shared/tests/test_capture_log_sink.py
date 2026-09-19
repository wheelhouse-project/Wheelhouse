"""The deferred-logging sink writes nothing on the calling thread.

wh-audio-callback-log built two log sinks so a caller on a real-time thread
could publish a line without taking a logging handler lock: OverflowMonitor
takes `log_sink` and elevate_current_thread takes `log_sink`. Each one hands
its caller a (logger, level, message) entry instead of creating the record,
and the caller emits it later from a thread that may block.

The one caller that passed a sink was MicrophoneStream, on the PortAudio
callback thread. That class went with the PortAudio capture path
(wh-portaudio-capture-removal), so both parameters now have no caller in the
shipped code; the crewcut comments beside them say so. The behaviour is still
guarded here, for two reasons. The mutation gate
tests/mutation_gate_capture_frame_loss.py proves four defects in
overflow_monitor.py and thread_priority.py through these tests
(callback-log-monitor-ignores-its-sink, callback-log-sink-given-the-wrong-
logger, elevation-warning-ignores-the-sink,
elevation-warning-dropped-instead-of-deferred), and a mutation with no
catcher reads as a survivor. And a future real-time caller -- the WinRT
capture path already runs a poll loop that could become one -- needs the
mechanism to still work when it arrives.

These tests replace the parts of the deleted tests/test_capture_callback_
logging.py that were about the sink rather than about the PortAudio callback
thread. What is gone with that file is the drain side: MicrophoneStream's
bounded _pending_log_msgs deque, its level check on the way in, and read()
and stop() emitting what the callback left behind. No surviving class has an
equivalent, so those tests had no subject left.
"""
import logging

from shared_audio import CAPTURE_LOGGER_NAME
from shared_audio.overflow_monitor import OverflowConfig, OverflowMonitor
from shared_audio.thread_priority import elevate_current_thread


SUMMARY_MIDDLE = "audio input overflows in the last"
OVERFLOW_LOGGER_NAME = "shared_audio.overflow_monitor"


class _RecordingSink:
    """Collects what a sink is handed, without creating a log record."""

    def __init__(self):
        self.entries = []

    def __call__(self, logger, level, message):
        self.entries.append((logger, level, message))

    def messages(self):
        return [message for _, _, message in self.entries]

    def containing(self, fragment):
        return [entry for entry in self.entries if fragment in entry[2]]


class _RecordingHandler(logging.Handler):
    """Records every log record created under the capture tree."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.seen = []

    def emit(self, record):
        self.seen.append(f"{record.name} {record.levelname}: "
                         f"{record.getMessage()}")


class _CaptureTree:
    """Attach a recording handler to the root of the capture logger tree.

    Written as a context manager rather than a fixture so each test states
    the window it is asserting over; the absence assertions below are only
    as good as that window.
    """

    def __enter__(self):
        self.logger = logging.getLogger(CAPTURE_LOGGER_NAME)
        self.handler = _RecordingHandler()
        self._previous_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._previous_level)
        return False


class _Kernel32Stub:
    """Just enough kernel32 for elevate_current_thread's two failure arms.

    A stub rather than a Mock: SetThreadPriority's return value is the
    whole point of one case, and a Mock would answer every other attribute
    too, which would hide a call this test did not expect.
    """

    def __init__(self, refuse=False, raises=None):
        self._refuse = refuse
        self._raises = raises
        self.calls = 0

    def GetCurrentThread(self):
        return 0x1234

    def SetThreadPriority(self, handle, value):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return 0 if self._refuse else 1

    def GetThreadPriority(self, handle):
        return 2


class TestAMonitorWithASinkCreatesNoLogRecord:
    """OverflowMonitor._emit is the one place this class logs."""

    def _monitor(self, sink):
        return OverflowMonitor(OverflowConfig(), log_sink=sink)

    def test_a_burst_past_the_threshold_creates_no_log_record(self):
        """The burst is what makes this test worth having.

        A single overflow reaches only the rate-limited summary. Crossing
        the threshold also reaches _should_restart's own lines, so a change
        that moved the summary alone would pass a one-overflow test and
        fail here.
        """
        sink = _RecordingSink()
        monitor = self._monitor(sink)
        with _CaptureTree() as capture_tree:
            for _ in range(OverflowConfig.overflow_threshold):
                monitor.report_overflow()
        assert capture_tree.seen == [], (
            "these records were created by the caller's own thread, which "
            "for a real-time caller means taking a logging handler lock: "
            f"{capture_tree.seen}")

    def test_the_lines_are_handed_to_the_sink_instead_of_dropped(self):
        """Not logging must not mean not reporting.

        Written as a separate test from the one above so a change that
        satisfies it by deleting the lines fails here rather than passing
        both.
        """
        sink = _RecordingSink()
        monitor = self._monitor(sink)
        with _CaptureTree():
            for _ in range(OverflowConfig.overflow_threshold):
                monitor.report_overflow()
        assert sink.containing(SUMMARY_MIDDLE), (
            "the overflow summary is neither logged nor waiting in the "
            f"sink for its caller to emit: {sink.messages()}")

    def test_the_sink_keeps_the_level_of_each_line(self):
        """The summary is INFO and the per-event detail is DEBUG.

        Flattening them would read as working -- every line still appears,
        with the right text -- while an operator filtering by level stops
        seeing what they filtered for.
        """
        sink = _RecordingSink()
        monitor = self._monitor(sink)
        with _CaptureTree():
            monitor.report_overflow()
        summaries = sink.containing(SUMMARY_MIDDLE)
        assert len(summaries) == 1, sink.messages()
        assert summaries[0][1] == logging.INFO
        assert [level for _, level, message in sink.entries
                if "Detected overflow event" in message] == [logging.DEBUG]

    def test_the_sink_is_given_the_overflow_monitors_own_logger(self):
        """The logger name decides whether google_stt_server forwards it.

        google_stt_server attaches its handler to shared_audio.overflow_
        monitor alone, so a summary handed over under the draining module's
        logger would vanish from wheelhouse.log on that provider while
        still reaching Parakeet's tree-root handler -- a break visible on
        one provider only.
        """
        sink = _RecordingSink()
        monitor = self._monitor(sink)
        with _CaptureTree():
            monitor.report_overflow()
        summaries = sink.containing(SUMMARY_MIDDLE)
        assert len(summaries) == 1, sink.messages()
        assert summaries[0][0].name == OVERFLOW_LOGGER_NAME

    def test_without_a_sink_the_monitor_still_logs_here_and_now(self):
        """The other half of the same guard.

        Every caller on an ordinary thread passes no sink and needs the
        line where it happened. A sink-only _emit would lose it for the
        WinRT poll loop, which is the only caller left.
        """
        monitor = OverflowMonitor(OverflowConfig())
        with _CaptureTree() as capture_tree:
            monitor.report_overflow()
        assert [line for line in capture_tree.seen
                if SUMMARY_MIDDLE in line], capture_tree.seen


class TestTheElevationFallbackHonoursItsSink:
    """elevate_current_thread writes a WARNING in each of its two failure
    arms, and takes a sink for the same reason the monitor does.

    Both arms are driven through the real helper with a stubbed kernel32,
    because a patched-out helper proves nothing about the code that logs.
    """

    def _elevate_with(self, monkeypatch, stub, sink):
        monkeypatch.setattr("shared_audio.thread_priority._kernel32", stub)
        return elevate_current_thread('time_critical', log_sink=sink)

    def test_a_refused_elevation_with_a_sink_creates_no_log_record(
            self, monkeypatch):
        stub = _Kernel32Stub(refuse=True)
        sink = _RecordingSink()
        with _CaptureTree() as capture_tree:
            self._elevate_with(monkeypatch, stub, sink)
        assert stub.calls == 1, "the real helper never ran"
        assert capture_tree.seen == [], capture_tree.seen

    def test_a_raising_elevation_with_a_sink_creates_no_log_record(
            self, monkeypatch):
        stub = _Kernel32Stub(raises=OSError("the OS refused"))
        sink = _RecordingSink()
        with _CaptureTree() as capture_tree:
            self._elevate_with(monkeypatch, stub, sink)
        assert stub.calls == 1, "the real helper never ran"
        assert capture_tree.seen == [], capture_tree.seen

    def test_the_refusal_reaches_the_sink(self, monkeypatch):
        """Held, not dropped. A guard that only counted log records would
        also pass if the line were deleted."""
        stub = _Kernel32Stub(refuse=True)
        sink = _RecordingSink()
        with _CaptureTree():
            self._elevate_with(monkeypatch, stub, sink)
        refused = sink.containing("SetThreadPriority")
        assert len(refused) == 1, sink.messages()
        assert refused[0][1] == logging.WARNING

    def test_the_raised_error_reaches_the_sink(self, monkeypatch):
        stub = _Kernel32Stub(raises=OSError("the OS refused"))
        sink = _RecordingSink()
        with _CaptureTree():
            self._elevate_with(monkeypatch, stub, sink)
        unavailable = sink.containing("elevation unavailable")
        assert len(unavailable) == 1, sink.messages()
        assert unavailable[0][1] == logging.WARNING
        assert "the OS refused" in unavailable[0][2]
