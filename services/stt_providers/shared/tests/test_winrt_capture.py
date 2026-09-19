"""Tests for shared_audio.capture.winrt_capture module.

Tests the WinRT AudioGraph-based audio capture implementation.
"""

import contextlib
import math
import random
import logging
import pytest
import queue
import struct
import threading
import time
from unittest.mock import Mock, patch, MagicMock, call
from types import SimpleNamespace

from shared_audio.capture.winrt_capture import WinRTAudioCapture
from shared_audio.capture.base import AudioConfig
from shared_audio.thread_priority import (
    AVRT_TASK_PRO_AUDIO,
    MmcssRegistration,
)


@pytest.fixture
def mock_winrt_available():
    """Mock WinRT availability."""
    with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', True):
        yield


@pytest.fixture
def _mock_winsdk_modules():
    """Pre-populate sys.modules with mock winsdk hierarchy.

    patch() needs the target module importable to resolve dotted paths.
    When winsdk isn't installed in the uv venv, we insert mocks first.
    """
    import sys
    mock_modules = {}
    module_paths = [
        'winsdk',
        'winsdk.windows',
        'winsdk.windows.media',
        'winsdk.windows.media.audio',
        'winsdk.windows.media.render',
        'winsdk.windows.media.capture',
        'winsdk.windows.media.mediaproperties',
        'winsdk.windows.devices',
        'winsdk.windows.devices.enumeration',
    ]
    originals = {}
    for path in module_paths:
        originals[path] = sys.modules.get(path)
        if path not in sys.modules:
            mock_modules[path] = MagicMock()
            sys.modules[path] = mock_modules[path]

    yield

    for path, mock_mod in mock_modules.items():
        if originals[path] is None:
            sys.modules.pop(path, None)
        else:
            sys.modules[path] = originals[path]


@pytest.fixture
def mock_winrt_graph(_mock_winsdk_modules):
    """Mock WinRT AudioGraph and related objects."""
    # Mock the utils module that gets imported
    import sys
    mock_utils = MagicMock()
    mock_run_sync = Mock()
    mock_utils.winrt_helpers.run_winrt_sync = mock_run_sync
    sys.modules['utils'] = mock_utils
    sys.modules['utils.winrt_helpers'] = mock_utils.winrt_helpers

    try:
        with patch('winsdk.windows.media.audio.AudioGraph') as MockGraph:
            with patch('winsdk.windows.media.audio.AudioGraphSettings') as MockSettings:
                with patch('winsdk.windows.media.mediaproperties.AudioEncodingProperties') as MockProps:
                    with patch('winsdk.windows.media.render.AudioRenderCategory'):
                        with patch('winsdk.windows.media.capture.MediaCategory'):
                                # Setup mock graph creation result
                                mock_graph = Mock()
                                mock_graph.start = Mock()
                                mock_graph.stop = Mock()
                                mock_graph.close = Mock()
                                mock_graph.create_device_input_node_async = Mock()
                                mock_graph.create_frame_output_node = Mock()

                                # Mock input node
                                mock_input_node = Mock()
                                mock_input_node.add_outgoing_connection = Mock()

                                # Mock frame output node
                                mock_frame_output = Mock()
                                mock_frame = Mock()
                                mock_audio_buffer = Mock()
                                mock_ref = Mock()

                                # Setup buffer to return float32 audio data
                                # Create a small amount of float32 data (2 samples = 8 bytes)
                                float_data = struct.pack('<ff', 0.5, -0.5)
                                mock_ref.__buffer__ = lambda self, flags: memoryview(float_data)

                                mock_audio_buffer.create_reference = Mock(return_value=mock_ref)
                                mock_audio_buffer.close = Mock()
                                mock_frame.lock_buffer = Mock(return_value=mock_audio_buffer)
                                mock_frame_output.get_frame = Mock(return_value=mock_frame)

                                # Mock async results - create once and reuse
                                mock_create_result = Mock(status=0, graph=mock_graph)
                                mock_input_result = Mock(status=0, device_input_node=mock_input_node)

                                # Setup run_winrt_sync to alternate between the two results
                                # First call creates graph, second call creates input node
                                results_cycle = [mock_create_result, mock_input_result]
                                call_count = [-1]

                                def sync_side_effect(*args, **kwargs):
                                    call_count[0] += 1
                                    return results_cycle[call_count[0] % 2]

                                mock_run_sync.side_effect = sync_side_effect

                                mock_graph.create_frame_output_node.return_value = mock_frame_output

                                yield {
                                    'graph': mock_graph,
                                    'input_node': mock_input_node,
                                    'frame_output': mock_frame_output,
                                    'create_result': mock_create_result,
                                    'input_result': mock_input_result,
                                    'run_sync': mock_run_sync,
                                }
    finally:
        # Clean up sys.modules
        sys.modules.pop('utils', None)
        sys.modules.pop('utils.winrt_helpers', None)


class TestWinRTCaptureInitialization:
    """Test initialization and configuration."""

    def test_init_with_default_config(self):
        """Should initialize with default AudioConfig."""
        capture = WinRTAudioCapture()

        assert capture.config.rate == 16000
        assert capture.config.channels == 1
        assert capture.config.chunk_ms == 30
        assert capture.overflow_callback is None
        assert capture._graph is None
        assert capture._running is False

    def test_init_with_custom_config(self):
        """Should use provided AudioConfig."""
        config = AudioConfig(rate=8000, channels=2, chunk_ms=20)
        capture = WinRTAudioCapture(config=config)

        assert capture.config.rate == 8000
        assert capture.config.channels == 2
        assert capture.config.chunk_ms == 20

    def test_init_with_overflow_callback(self):
        """Should store overflow callback and create monitor."""
        callback = Mock()
        capture = WinRTAudioCapture(overflow_callback=callback)

        assert capture.overflow_callback == callback
        assert capture.overflow_monitor is not None

    def test_init_creates_queue(self):
        """Should create queue holding ~10s of audio (333 x 30ms chunks).

        Depth is the defense against whole-machine CPU starvation stalls
        (wh-stt-audio-consumer-behind-realtime): the consumer drains at many
        times real time once it is scheduled again, so a deeper queue turns
        dropped frames into briefly delayed frames.
        """
        capture = WinRTAudioCapture()

        assert isinstance(capture._q, queue.Queue)
        assert capture._q.maxsize == 333

    def test_init_initializes_stats(self):
        """Should initialize statistics to zero."""
        capture = WinRTAudioCapture()

        assert capture._frames_captured == 0
        assert capture._drops == 0
        assert capture._max_queue_depth == 0
        assert capture._start_time is None


_MMCSS = 'shared_audio.capture.winrt_capture.register_current_thread_mmcss'
_REVERT = 'shared_audio.capture.winrt_capture.revert_current_thread_mmcss'
_ELEVATE = 'shared_audio.capture.winrt_capture.elevate_current_thread'


def _registered(handle=0x1F4):
    return MmcssRegistration(AVRT_TASK_PRO_AUDIO, handle, None)


def _refused(error='winerror 1552'):
    return MmcssRegistration(AVRT_TASK_PRO_AUDIO, None, error)


class TestWinRTCaptureThreadPriority:
    """The polling thread asks MMCSS to keep it scheduled.

    SetThreadPriority raises this thread inside the ordinary scheduler,
    which still leaves the scheduler free to skip it when the machine is
    saturated by bulk compute. MMCSS is the separate path the Windows audio
    engine itself uses, and it gives a registered thread a guaranteed share
    of every scheduling period. The PortAudio path got this first
    (wh-stt-load-metrics.3); this is the same registration on the WinRT
    path, which is the only one left since wh-portaudio-capture-removal
    deleted the other.

    The registration is made here, inside the loop, because MMCSS records
    it against the CALLING thread and only that thread may release it.
    """

    def _loop(self, capture):
        return (patch.object(capture, '_setup_graph',
                             return_value=_graph_triple()),
                patch.object(capture, '_poll_frames'),
                patch.object(capture, '_cleanup_graph'))

    def test_the_polling_thread_registers_with_mmcss(self):
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, return_value=_registered()) as mock_register, \
                patch(_REVERT):
            capture._capture_loop(capture._cycle)

        mock_register.assert_called_once_with()

    def test_a_successful_registration_does_not_also_elevate(self):
        """The mutual exclusion. Windows hands an MMCSS thread's scheduling
        to MMCSS, whose own control is AvSetMmThreadPriority, so calling
        SetThreadPriority as well is not a stronger request -- it is a
        second, different request against a thread MMCSS is already
        managing. The older call is the fallback, never an addition.
        """
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, return_value=_registered()), \
                patch(_REVERT), \
                patch(_ELEVATE, return_value=True) as mock_elevate:
            capture._capture_loop(capture._cycle)

        mock_elevate.assert_not_called()

    def test_a_null_handle_falls_back_to_thread_priority(self):
        """The failure MMCSS actually returns. A refusal comes back as a
        registration whose handle is None and whose error says why -- not
        as an exception -- so a fallback that only caught exceptions would
        leave this thread with no protection at all.
        """
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, return_value=_refused()), \
                patch(_REVERT) as mock_revert, \
                patch(_ELEVATE, return_value=True) as mock_elevate:
            capture._capture_loop(capture._cycle)

        mock_elevate.assert_called_once_with('time_critical')
        mock_revert.assert_not_called()

    def test_a_registration_that_raises_falls_back_too(self):
        """register_current_thread_mmcss promises never to raise. The loop
        does not depend on that promise: a future edit that breaks it must
        not cost this thread its scheduling protection, and must not stop
        the AudioGraph being built.
        """
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, side_effect=OSError('avrt exploded')), \
                patch(_REVERT) as mock_revert, \
                patch(_ELEVATE, return_value=True) as mock_elevate:
            capture._capture_loop(capture._cycle)

        mock_elevate.assert_called_once_with('time_critical')
        mock_revert.assert_not_called()
        assert capture._setup_error is None, (
            'a failed scheduling request was reported as a setup failure')

    def test_the_registration_is_released_when_the_loop_ends(self):
        """Only the registering thread may release an MMCSS registration,
        so the revert has to happen here rather than in stop(). A thread
        that exits still holding one leaves the service holding a handle
        nothing can release.
        """
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, return_value=_registered(0x2A)), \
                patch(_REVERT) as mock_revert:
            capture._capture_loop(capture._cycle)

        mock_revert.assert_called_once_with(0x2A)

    def test_the_registration_is_released_even_when_setup_fails(self):
        """The graph can fail to build. The revert belongs in the finally
        for the same reason the graph cleanup does.
        """
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          side_effect=RuntimeError('no microphone')), \
                patch.object(capture, '_cleanup_graph'), \
                patch(_MMCSS, return_value=_registered(0x2A)), \
                patch(_REVERT) as mock_revert:
            capture._capture_loop(capture._cycle)

        mock_revert.assert_called_once_with(0x2A)
        assert capture._setup_error == 'no microphone'


    def test_the_registration_line_prints_at_the_provider_log_level(
            self, caplog):
        """A DEBUG line never reaches the log an operator reads.

        The Parakeet provider calls logging.basicConfig(level=logging.INFO)
        at sherpa_offline_parakeet_stt_server/main.py line 34, so anything
        below INFO is discarded before it is written. Criterion 1 of
        wh-stt-load-metrics.3 asks for the MMCSS task name and handle to be
        logged once, and the PortAudio path logged its equivalent at INFO --
        microphone.py drained _pending_log_msgs with logger.info. The two
        capture paths were not allowed to disagree about the level of the
        same fact (boss ruling 2026-09-03 15:52); that path has since been
        deleted (wh-portaudio-capture-removal) and INFO is what stayed.
        """
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, return_value=_registered()), \
                patch(_REVERT):
            with caplog.at_level(
                    logging.DEBUG,
                    logger='shared_audio.capture.winrt_capture'):
                capture._capture_loop(capture._cycle)

        lines = [r for r in caplog.records
                 if r.name == 'shared_audio.capture.winrt_capture'
                 and 'MMCSS' in r.message]
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO

    def test_the_fallback_lines_print_at_the_provider_log_level(self, caplog):
        """The failure half matters more than the success half.

        A run that fell back to SetThreadPriority is the run an operator
        needs to see in the log, because it is the one where the scheduling
        protection this change exists for was not granted.
        """
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, return_value=_refused()), \
                patch(_REVERT), \
                patch(_ELEVATE, return_value=True):
            with caplog.at_level(
                    logging.DEBUG,
                    logger='shared_audio.capture.winrt_capture'):
                capture._capture_loop(capture._cycle)

        levels = [r.levelno for r in caplog.records
                  if r.name == 'shared_audio.capture.winrt_capture']
        assert levels == [logging.INFO, logging.INFO]

    def test_a_registration_that_raises_is_reported_at_that_level_too(
            self, caplog):
        """The third of the three MMCSS branches, so no branch is left at
        DEBUG for a later edit to copy from."""
        capture = WinRTAudioCapture()
        setup, poll, cleanup = self._loop(capture)
        with setup, poll, cleanup, \
                patch(_MMCSS, side_effect=OSError('avrt exploded')), \
                patch(_REVERT), \
                patch(_ELEVATE, return_value=True):
            with caplog.at_level(
                    logging.DEBUG,
                    logger='shared_audio.capture.winrt_capture'):
                capture._capture_loop(capture._cycle)

        levels = [r.levelno for r in caplog.records
                  if r.name == 'shared_audio.capture.winrt_capture']
        assert levels == [logging.INFO, logging.INFO]


class TestWinRTCaptureSetupHandshake:
    """start() must not report success before the graph exists.

    _capture_loop builds the AudioGraph on the background thread and
    catches every setup error there, so start() returned success for a
    denied microphone and the caller announced a working service that
    then read silence forever (streaming-provider review .1.22).
    wait_ready() is the handshake that closes that gap.
    """

    def test_wait_ready_true_after_successful_setup(self, mock_winrt_available):
        """A graph that comes up must report ready with no error.

        The poll is held open for the assertion because a poll that
        has returned is a graph that has shut down, and wait_ready()
        answers for the present moment (wh-stt-load-metrics.2).
        """
        release = threading.Event()
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          return_value=_graph_triple()), \
                patch.object(capture, '_poll_frames',
                             side_effect=lambda *_: release.wait(5.0)), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
                assert capture.setup_error is None
            finally:
                release.set()
                capture.stop()

    def test_wait_ready_polls_only_after_setup_succeeds(self, mock_winrt_available):
        """The handshake must sit between setup and frame polling."""
        release = threading.Event()
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          return_value=_graph_triple()), \
                patch.object(capture, '_poll_frames',
                             side_effect=lambda *_: release.wait(5.0)) as mock_poll, \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
            finally:
                release.set()
                capture.stop()
            mock_poll.assert_called_once()

    def test_wait_ready_false_and_error_after_failed_setup(self, mock_winrt_available):
        """A denied microphone must be reported, not swallowed."""
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          side_effect=RuntimeError(
                              "Microphone node failed: status=1")), \
                patch.object(capture, '_poll_frames') as mock_poll, \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                started = time.time()
                assert capture.wait_ready(timeout=5.0) is False
                # Released by the failure itself, not by the timeout: the
                # caller must not pay 5 s to learn the mic was denied.
                assert time.time() - started < 2.0
                assert capture.setup_error == "Microphone node failed: status=1"
            finally:
                capture.stop()
            mock_poll.assert_not_called()

    def test_wait_ready_times_out_when_setup_never_finishes(self, mock_winrt_available):
        """A hung AudioGraph must bound the caller's wait, not block it."""
        release = _Handoff()

        def hung_setup():
            release.wait_here(10.0)
            return _graph_triple()

        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph', side_effect=hung_setup), \
                patch.object(capture, '_poll_frames'), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                # The setup must be INSIDE the hang before the wait below is
                # measured. wait_ready() also answers False while
                # _setup_done is unset, so a capture thread the host has not
                # scheduled yet gives this test the answer it wants for the
                # wrong reason (wh-stt-load-metrics.2.1.21). The handoff's
                # own count is what says so, not an event the worker sets on
                # its way in: a worker descheduled between such an event and
                # its park is not parked at all, and the release below then
                # returns to it instantly with timed_out still False
                # (wh-stt-load-metrics.2.1.23).
                assert release.wait_for_a_parked_waiter(5.0) is True, (
                    'no thread reached the hung setup, so the bounded wait '
                    'below would have been measured against a setup that '
                    'had not started')
                started = time.time()
                assert capture.wait_ready(timeout=0.2) is False
                assert time.time() - started < 2.0
                assert capture.setup_error is None
                # Still parked, so the False above was read while the graph
                # was hung rather than while its thread was between the
                # announcement and the park.
                assert release.parked_waiters() == 1, (
                    'the hung setup was not in its park when this test read '
                    'the bounded wait above')
            finally:
                release.set()
                capture.stop()
        assert release.timed_out is False, (
            'the hung setup gave up before the test released it, so the '
            'graph finished on its own and the False above was not the '
            'bounded wait this test names')

    def test_start_after_failed_setup_resets_handshake(self, mock_winrt_available):
        """A stop/start cycle must not inherit the previous failure."""
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          side_effect=RuntimeError("no input device")), \
                patch.object(capture, '_poll_frames'), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            assert capture.wait_ready(timeout=5.0) is False
            capture.stop()

        release = threading.Event()
        with patch.object(capture, '_setup_graph',
                          return_value=_graph_triple()), \
                patch.object(capture, '_poll_frames',
                             side_effect=lambda *_: release.wait(5.0)), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
                assert capture.setup_error is None
            finally:
                release.set()
                capture.stop()


def _graph_triple(get_frame=None):
    """What _setup_graph now hands back: (graph, mic_node, frame_output).

    _setup_graph returns its resources instead of installing them on the
    provider, so every stand-in for it owes the same three
    (wh-stt-load-metrics.2.1.3). The frame output answers None by default,
    which the poll loop reads as a quiet microphone.
    """
    node = Mock()
    node.get_frame = Mock(return_value=None) if get_frame is None else get_frame
    return Mock(), Mock(), node


@contextlib.contextmanager
def _winrt_polling_a_node(capture, get_frame):
    """Run the REAL _poll_frames over a stand-in frame output node.

    Everything the loop reads comes from _setup_graph in production, so the
    stand-in is installed there and nothing else about the capture thread
    changes. Mocking _poll_frames itself cannot test the loop's own error
    handling, which is the whole subject of the tests that use this.

    The caller's get_frame is what decides whether the graph is healthy: it
    raises for a device that has gone away and returns None for a poll that
    found no frame waiting.
    """
    node = Mock()
    node.get_frame = get_frame
    with patch.object(
                capture, '_setup_graph',
                side_effect=lambda: (Mock(), Mock(), node)), \
            patch.object(capture, '_cleanup_graph'), \
            patch('shared_audio.capture.winrt_capture.'
                  'elevate_current_thread', return_value=True):
        yield node


def _wait_until(predicate, timeout):
    """Poll predicate until it holds, so a test never races a live thread."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()



class _Handoff:
    """A handoff between the test and a worker that remembers a timeout.

    wh-stt-load-metrics.2.1.19. A race guard parks its worker on
    threading.Event.wait(5.0) and drops the answer, so a worker whose
    release never came runs on exactly as though the test had released
    it. On the saturated machine this bead exists to measure, that is
    reachable: the pytest thread can lose five seconds to the scheduler,
    the overlap the test was written to create never happens, and the
    mutation the test exists to catch survives. The sibling shape fails
    correct code instead, when the worker leaves the park before the test
    has read what the park was holding open.

    This wrapper keeps the answer. `timed_out` turns True the moment a
    wait expires, and every test asserts it is False before it reads any
    conclusion, so a handoff that did not happen fails the test instead of
    deciding it. The wait stays bounded, because an unbounded one would
    turn a real deadlock into a hung suite rather than a failed test.

    It also counts the threads parked on it, which is what
    wh-stt-load-metrics.2.1.23 needed. A worker that announces its arrival
    with its own event, one line before it parks, can be descheduled
    between the two: the test then measures against a setup that has not
    reached the park, releases it, and the late worker's wait returns at
    once with timed_out still False. The count is taken inside wait_here
    with nothing of the worker's own between it and the wait, so a test
    can require a parked waiter before it reads anything AND require the
    same waiter to still be parked afterwards.

    _WatchedLock is the same repair for lock waits (2.1.11), and its
    waiting count is the same construction as this one; this class is for
    event waits.
    """

    def __init__(self):
        self._event = threading.Event()
        self._state = threading.Lock()
        self._parked = 0
        self.timed_out = False

    def set(self):
        self._event.set()

    def is_set(self):
        return self._event.is_set()

    def parked_waiters(self):
        """How many threads are inside wait_here and have not left it."""
        with self._state:
            return self._parked

    def wait_for_a_parked_waiter(self, timeout=5.0):
        """Whether a worker is parked on this handoff right now."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.parked_waiters() > 0:
                return True
            time.sleep(0.005)
        return self.parked_waiters() > 0

    def wait_here(self, timeout=5.0):
        """Park until the test releases this handoff; record a timeout."""
        with self._state:
            self._parked += 1
        try:
            if not self._event.wait(timeout):
                self.timed_out = True
                return False
            return True
        finally:
            with self._state:
                self._parked -= 1

class _WatchedLock:
    """A lifecycle lock that can say another thread is blocked on it.

    wh-stt-load-metrics.2.1.11. The lock tests used to start the protected
    work and read a wait() that timed out as proof the work was blocked. On
    a saturated machine -- the condition this whole bead exists to measure
    -- an unscheduled thread times out exactly the same way, so the test
    passed whether or not the lock was there. Every one of them could go
    green for the very defect it guards.

    This wrapper counts threads that tried the inner lock, were refused,
    and are now parked on it. While the test itself holds that lock, a
    count above zero means some other thread really is parked at the
    transition, so the state assertions that follow are about a blocked
    writer rather than about a writer that never ran.

    The count goes up only after a non-blocking attempt has been refused,
    never before (wh-stt-load-metrics.2.1.24). `published` records how
    many times the count was ever raised, which is what lets a test say
    that entering a free lock reports nothing at all.

    The wrapper delegates to whatever the constructor built, so it cannot
    fix a lock that does not exclude. That case is
    test_the_lifecycle_lock_is_a_real_mutual_exclusion_lock, which does not
    use this wrapper at all.
    """

    def __init__(self, inner):
        self._inner = inner
        self._state = threading.Lock()
        self._waiting = 0
        self._published = 0
        self._owner = threading.get_ident()

    def published(self):
        """How many times this lock ever reported a thread as blocked."""
        with self._state:
            return self._published

    def __enter__(self):
        mine = threading.get_ident() == self._owner
        if mine:
            return self._inner.__enter__()
        # Say "waiting" only after a non-blocking attempt has proved the
        # lock is held by somebody else. Counting first left a window of
        # its own: a thread that had incremented but had not yet reached
        # the lock counted as blocked, so a test could read that signal,
        # release the code under test, and let this thread take a free
        # lock it never contended for. Under the mutant that moves the
        # capture-thread install out of the lock, the stop() in
        # test_a_stop_waits_for_the_start_to_install_its_capture_thread
        # then read a blocked signal for a lock nothing was holding and
        # the mutant survived (wh-stt-load-metrics.2.1.24). A failed
        # attempt is proof of a real holder at the moment it fails, which
        # the bare count never was.
        if self._inner.acquire(blocking=False):
            return True
        with self._state:
            self._waiting += 1
            self._published += 1
        try:
            self._inner.acquire()
            return True
        finally:
            with self._state:
                self._waiting -= 1

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)

    def wait_for_a_blocked_thread(self, timeout=5.0):
        """Whether a thread other than this test's is parked at the lock."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._state:
                if self._waiting > 0:
                    return True
            time.sleep(0.005)
        with self._state:
            return self._waiting > 0


class _PausingLock:
    """A lifecycle lock that pauses one named thread just after it releases.

    wh-stt-load-metrics.2.1.16. The .15 test's only synchronisation point is
    inside stop()'s join, so it can only place a restart after the join has
    begun. A clear dedented out of the lock but left BEFORE the join runs
    earlier than that, so the restart's write lands after it and survives:
    the test passes on code with the very hole it guards. This wrapper puts
    the test's foothold at the release itself, which is the boundary the
    fix actually turns on -- inside the transition on one side, outside it
    on the other.

    Only the named thread pauses, and only on its first release, so start()
    on the main thread and the stand-in writer's own use of the lock run
    unhindered.
    """

    def __init__(self, inner, thread_name, after_release):
        self._inner = inner
        self._thread_name = thread_name
        self._after_release = after_release
        self._fired = False

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, *exc_info):
        result = self._inner.__exit__(*exc_info)
        if (not self._fired
                and threading.current_thread().name == self._thread_name):
            self._fired = True
            self._after_release()
        return result


class _CaptureThreadFactory:
    """Stands in for threading.Thread, for the capture thread only.

    wh-stt-load-metrics.2.1.13. The races below are between start() and
    stop(), and the point each one turns on is where the capture thread is
    installed. Intercepting the construction is what makes that point a
    place a test can stand; anything else the process builds while this is
    patched in gets a real thread, so the patch cannot reach past the code
    under test.
    """

    def __init__(self, on_construct=None, on_join=None, writes_for=None):
        # Captured before the patch is applied, so it is the real class.
        self._real = threading.Thread
        self._on_construct = on_construct
        self._on_join = on_join
        # The capture object whose queue the stand-in threads write into,
        # or None for stand-ins that run no code at all.
        self._writes_for = writes_for
        self.installed = []

    def __call__(self, target=None, args=(), daemon=None, name=None, **kw):
        if name != 'WinRTAudioCapture':
            return self._real(target=target, args=args, daemon=daemon,
                              name=name, **kw)
        if self._on_construct is not None:
            self._on_construct()
        if self._writes_for is not None:
            return _WritingCaptureThread(self, self._writes_for, args[0])
        return _RecordedCaptureThread(self)


class _RecordedCaptureThread:
    """A capture thread that records what was done to it, and runs nothing."""

    def __init__(self, factory):
        self._factory = factory
        self.started = False
        self.joined = False

    def start(self):
        self.started = True
        self._factory.installed.append(self)

    def join(self, timeout=None):
        self.joined = True
        if self._factory._on_join is not None:
            self._factory._on_join()

    def is_alive(self):
        return self.started and not self.joined


class _WritingCaptureThread:
    """A stand-in capture thread that writes one chunk for its own cycle.

    wh-stt-load-metrics.2.1.15. _RecordedCaptureThread runs no code, so a
    test built on it can drive a stop() and a restart past each other but
    can never see what becomes of the audio they carry. This one performs
    the same guarded write the real capture loop performs: inside the
    lifecycle lock, and only while _running holds and the cycle is still
    its own. The write runs on a real thread because start() calls start()
    on this object while holding that same lock, so an inline write would
    block against a lock its own caller owns.
    """

    def __init__(self, factory, capture, cycle):
        self._factory = factory
        self._capture = capture
        self._cycle = cycle
        self.chunk = b'cycle-%d audio' % cycle
        self.started = False
        self.joined = False
        self.wrote = False
        self._written = threading.Event()
        self._worker = None

    def start(self):
        self.started = True
        self._factory.installed.append(self)
        self._worker = self._factory._real(
            target=self._write, daemon=True, name='writing-capture')
        self._worker.start()

    def _write(self):
        with self._capture._lifecycle_lock:
            if (self._capture._running
                    and self._capture._cycle == self._cycle):
                self._capture._q.put_nowait(self.chunk)
                self.wrote = True
        self._written.set()

    def wait_for_its_write(self, timeout=5.0):
        """Whether this cycle's chunk has reached the queue."""
        self._written.wait(timeout)
        return self.wrote

    def join(self, timeout=None):
        self.joined = True
        if self._worker is not None:
            self._worker.join(timeout)
        if self._factory._on_join is not None:
            self._factory._on_join()

    def is_alive(self):
        return self.started and not self.joined


class TestTheWatchedLockOnlyReportsRealContention:
    """wh-stt-load-metrics.2.1.24, found by codex in round 16.

    _WatchedLock raised its waiting count on the way to the inner lock
    instead of after being refused it, so a thread descheduled in that gap
    counted as blocked without having contended for anything. Six lock
    tests read that signal, and under the mutant that moves the
    capture-thread install out of the lifecycle lock it was the only thing
    standing between the mutant and a pass: with a pause forced into that
    gap, test_a_stop_waits_for_the_start_to_install_its_capture_thread went
    green on the mutant. These two tests pin the repair from both sides --
    a free lock must report nothing, a held one must report exactly once.
    """

    def test_entering_a_free_lock_never_reports_a_blocked_thread(self):
        watched = _WatchedLock(threading.Lock())
        done = threading.Event()

        def _take_it():
            with watched:
                pass
            done.set()

        walker = threading.Thread(target=_take_it, name='walker')
        walker.start()
        assert done.wait(5.0) is True, 'the walker never took the free lock'
        walker.join(5.0)
        assert watched.published() == 0, (
            'a thread that was never refused the lock was still reported '
            'as blocked on it, so a test reading that signal cannot tell '
            'a real wait from a thread merely on its way to a free lock')
        assert watched.wait_for_a_blocked_thread(timeout=0.05) is False

    def test_a_thread_refused_the_lock_is_reported_once(self):
        inner = threading.Lock()
        watched = _WatchedLock(inner)
        took_it = threading.Event()

        def _take_it():
            with watched:
                took_it.set()

        inner.acquire()
        walker = threading.Thread(target=_take_it, name='walker')
        walker.start()
        try:
            assert watched.wait_for_a_blocked_thread(5.0) is True, (
                'a thread refused the held lock was not reported as '
                'blocked on it')
            assert watched.published() == 1
            assert took_it.is_set() is False, (
                'the walker entered a lock this test was still holding')
        finally:
            inner.release()
        assert took_it.wait(5.0) is True
        walker.join(5.0)
        assert watched.published() == 1


class TestAStopCannotOutrunTheStartItInterrupts:
    """wh-stt-load-metrics.2.1.13, WinRT half, found by codex in round 9.

    start() sets _running under the lifecycle lock and then released it
    before installing the capture thread. A stop() arriving in that window
    took the lock, cleared _running, looked for a thread to join, found the
    previous cycle's reference or None, and returned as though the capture
    were down. start() then launched the capture thread anyway, so a
    completed stop() left a thread behind that went on to build a WinRT
    graph. The mirror image is the same window read from the other side:
    stop() read the thread reference after releasing the lock, so a start()
    that had already begun the next cycle could have its brand-new thread
    joined and its reference nulled by the stop it had overtaken.
    """

    def test_a_stop_waits_for_the_start_to_install_its_capture_thread(
            self, mock_winrt_available):
        capture = WinRTAudioCapture()
        watched = _WatchedLock(capture._lifecycle_lock)
        capture._lifecycle_lock = watched

        events = []
        at_the_window = threading.Event()
        release = _Handoff()

        def _pause_in_the_window():
            at_the_window.set()
            release.wait_here(5.0)

        factory = _CaptureThreadFactory(on_construct=_pause_in_the_window)

        def _stop():
            capture.stop()
            events.append('stop returned')

        starter = threading.Thread(target=capture.start, name='starter')
        stopper = threading.Thread(target=_stop, name='stopper')

        with patch('shared_audio.capture.winrt_capture.threading.Thread',
                   factory):
            starter.start()
            assert at_the_window.wait(5.0) is True, (
                'start() never reached the capture-thread install')
            stopper.start()
            # A real parked writer, not a wait() that timed out: see
            # _WatchedLock. False here means stop() sailed through the
            # window instead of waiting for the lock.
            blocked = watched.wait_for_a_blocked_thread(timeout=1.0)
            release.set()
            starter.join(5.0)
            stopper.join(5.0)

        assert release.timed_out is False, (
            'the construction pause gave up before the test released it, '
            'so start() walked out of the window on its own and nothing '
            'below is about the overlap this test names')
        assert blocked is True, (
            'stop() ran straight through the window in which start() had '
            'set _running but had not yet installed its capture thread')
        assert len(factory.installed) == 1
        installed = factory.installed[0]
        assert installed.joined is True, (
            'stop() returned without joining the thread start() launched')
        assert events == ['stop returned']

    def test_a_stop_that_is_joining_does_not_take_the_next_cycles_thread(
            self, mock_winrt_available):
        capture = WinRTAudioCapture()

        in_the_join = threading.Event()
        release = _Handoff()

        def _pause_in_the_join():
            in_the_join.set()
            release.wait_here(5.0)

        factory = _CaptureThreadFactory(on_join=_pause_in_the_join)

        with patch('shared_audio.capture.winrt_capture.threading.Thread',
                   factory):
            capture.start()
            first = factory.installed[0]

            stopper = threading.Thread(target=capture.stop, name='stopper')
            stopper.start()
            assert in_the_join.wait(5.0) is True, (
                'stop() never reached the join')

            restarter = threading.Thread(target=capture.start,
                                         name='restarter')
            restarter.start()
            restarter.join(5.0)

            release.set()
            stopper.join(5.0)

        assert release.timed_out is False, (
            'the join pause gave up before the test released it, so the '
            'old stop finished before the restart and the two never '
            'overlapped -- the overlap is the whole of this test')
        assert len(factory.installed) == 2, (
            'the restart did not install a second capture thread')
        second = factory.installed[1]
        assert second is not first
        assert capture._capture_thread is second, (
            'the stop that was joining the old cycle took away the thread '
            'the new cycle had just installed, so that cycle has nothing '
            'for its own stop() to join')


class TestAStopCannotEraseTheNextCyclesAudio:
    """wh-stt-load-metrics.2.1.15, found by codex in round 10.

    stop() cleared the capture queue after releasing the lifecycle lock and
    after its bounded 2.0s join, holding only the queue's own mutex and
    testing no cycle. A start() arriving during that join took the released
    lock, advanced the cycle, and installed a capture thread whose chunks
    passed both write guards; the stop then woke and erased them. Nothing
    counted the loss, because _drops moves only on queue.Full, so the load
    reporter would read audio the provider threw away as audio the machine
    was too busy to capture -- the exact confusion this bead exists to
    remove.
    """

    def test_a_stop_that_is_joining_cannot_erase_the_next_cycles_audio(
            self, mock_winrt_available):
        capture = WinRTAudioCapture()

        in_the_join = threading.Event()
        release = _Handoff()

        def _pause_in_the_join():
            in_the_join.set()
            release.wait_here(5.0)

        factory = _CaptureThreadFactory(on_join=_pause_in_the_join,
                                        writes_for=capture)

        with patch('shared_audio.capture.winrt_capture.threading.Thread',
                   factory):
            capture.start()
            first = factory.installed[0]
            assert first.wait_for_its_write(5.0) is True, (
                'the first cycle never wrote its chunk')

            stopper = threading.Thread(target=capture.stop, name='stopper')
            stopper.start()
            assert in_the_join.wait(5.0) is True, (
                'stop() never reached the join')

            # The restart lands while that stop is still joining, and its
            # thread writes for the new cycle: both guards at the write
            # pass, because _running is True again and the cycle is its own.
            capture.start()
            second = factory.installed[1]
            assert second is not first
            assert second.wait_for_its_write(5.0) is True, (
                'the restarted cycle never wrote its chunk')

            release.set()
            stopper.join(5.0)

        assert release.timed_out is False, (
            'the join pause gave up before the test released it, so the '
            'stop finished before the restart wrote and its clear could '
            'not have reached the new cycle either way')
        assert capture.read(timeout=0.5) == second.chunk, (
            'the stop that was joining the old cycle cleared the queue '
            'after the new cycle had already written into it')
        assert capture._q.qsize() == 0, (
            "the old cycle's audio outlived the stop that ended it")

    def test_a_stop_cannot_clear_after_it_releases_the_lifecycle_lock(
            self, mock_winrt_available):
        """wh-stt-load-metrics.2.1.16, found by codex in round 11.

        The test above cannot see a clear that was dedented out of the
        lock but left before the join: such a clear runs before the join
        sets its event, so the restart it drives lands afterwards and the
        chunk survives. This one pauses the stopping thread at the release
        itself. Everything the fix promises happens before that point, so
        a clear on the far side of it erases the restart's audio and this
        test fails -- which is the whole of the property `e03f96e1`
        depends on.
        """
        capture = WinRTAudioCapture()

        released = threading.Event()
        resume = _Handoff()

        def _pause_after_the_release():
            released.set()
            resume.wait_here(5.0)

        capture._lifecycle_lock = _PausingLock(
            capture._lifecycle_lock, 'stopper', _pause_after_the_release)

        factory = _CaptureThreadFactory(writes_for=capture)

        with patch('shared_audio.capture.winrt_capture.threading.Thread',
                   factory):
            capture.start()
            first = factory.installed[0]
            assert first.wait_for_its_write(5.0) is True, (
                'the first cycle never wrote its chunk')

            stopper = threading.Thread(target=capture.stop, name='stopper')
            stopper.start()
            assert released.wait(5.0) is True, (
                'stop() never released the lifecycle lock')

            capture.start()
            second = factory.installed[1]
            assert second is not first
            assert second.wait_for_its_write(5.0) is True, (
                'the restarted cycle never wrote its chunk')

            resume.set()
            stopper.join(5.0)

        assert resume.timed_out is False, (
            'the pause at the lock release gave up before the test '
            'released it, so the stop ran to its end before the restart '
            'wrote and this test proved nothing about the far side')
        assert capture.read(timeout=0.5) == second.chunk, (
            'the stop erased the restarted cycle\'s audio, so its queue '
            'clear runs after it has given up the lifecycle lock')
        assert capture._q.qsize() == 0, (
            "the old cycle's audio outlived the stop that ended it")


class TestCaptureLiveness:
    """A capture path that opened and then died must stop answering ready.

    wh-stt-load-metrics.2. Both capture paths answered the readiness
    handshake once and never took the answer back: WinRT set _setup_ok when
    its graph came up and neither a returning frame poll nor a raising one
    cleared it, and the PortAudio adapter returned a _started flag that only
    stop() touched. A microphone that opened and later died kept answering
    ready while capturing nothing, so the load reporter marked those windows
    measured and printed drops=0 overflow=0 for a dead device. The PortAudio
    half of this class went with that path (wh-portaudio-capture-removal);
    the WinRT pairs below are what is left.

    Every test here also has to leave the other job wait_ready() does
    intact -- gating startup, where the answer must be True while capture is
    genuinely running -- so each pair below pins the live case beside the
    dead one.
    """

    def test_winrt_is_ready_while_the_graph_runs(self, mock_winrt_available):
        """The startup gate: polling in progress is the live case."""
        release = threading.Event()
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          return_value=_graph_triple()), \
                patch.object(capture, '_poll_frames',
                             side_effect=lambda *_: release.wait(5.0)), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
            finally:
                release.set()
                capture.stop()

    def test_winrt_stops_being_ready_when_the_capture_loop_ends(
            self, mock_winrt_available):
        """_poll_frames returning is the graph shutting down under us."""
        release = threading.Event()
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          return_value=_graph_triple()), \
                patch.object(capture, '_poll_frames',
                             side_effect=lambda *_: release.wait(5.0)), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
                release.set()
                capture._capture_thread.join(timeout=5.0)
                assert capture._capture_thread.is_alive() is False

                assert capture.wait_ready(timeout=0.0) is False
            finally:
                release.set()
                capture.stop()

    def test_winrt_stops_being_ready_when_every_frame_poll_raises(
            self, mock_winrt_available, _mock_winsdk_modules):
        """A graph whose every poll raises must report itself dead.

        The poll loop catches each frame's exception, logs it and keeps
        going, so the capture thread stays alive through all of it and
        nothing about the thread's lifetime can report this. That is why
        this test drives the real _poll_frames: an earlier version mocked
        _poll_frames itself with side_effect=RuntimeError, which the real
        loop cannot do, so it guarded a state production could not reach.

        setup_error must stay None. The handshake already answered True and
        a caller has moved on, so this is not a setup failure and the
        existing rule against rewriting it under that caller still holds.
        """
        capture = WinRTAudioCapture()
        with _winrt_polling_a_node(
                capture, Mock(side_effect=RuntimeError("device removed"))):
            capture.start()
            try:
                # Wait the setup handshake out before reading the answer.
                # wait_ready returns False for as long as _setup_done is
                # unset, so a poll for False that begins here is satisfied
                # by the setup that has not finished yet: it passes without
                # the liveness flag ever moving, and the mutation that
                # removes the dead write survives it
                # (wh-stt-load-metrics.2.1.18).
                assert capture._setup_done.wait(5.0) is True
                assert capture._setup_ok is True
                assert _wait_until(
                    lambda: capture.wait_ready(timeout=0.0) is False, 5.0)
                assert capture._capture_thread.is_alive() is True
                assert capture.setup_error is None
            finally:
                capture.stop()

    def test_winrt_stays_ready_through_one_failed_frame_poll(
            self, mock_winrt_available, _mock_winsdk_modules):
        """One raising poll is a hiccup, not a device that has gone away.

        The other half of the startup gate. Tearing the answer down on the
        first exception would make a provider that is capturing normally
        report unavailable windows, which is the same wrong reading as the
        defect, in the other direction.
        """
        pending = [RuntimeError("transient")]

        def get_frame():
            if pending:
                raise pending.pop()
            return None

        capture = WinRTAudioCapture()
        with _winrt_polling_a_node(capture, get_frame):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
                assert _wait_until(lambda: not pending, 5.0)

                deadline = time.time() + 0.5
                while time.time() < deadline:
                    assert capture.wait_ready(timeout=0.0) is True
                    time.sleep(0.02)
            finally:
                capture.stop()

    def test_winrt_is_ready_again_once_the_frame_polls_recover(
            self, mock_winrt_available, _mock_winsdk_modules):
        """A device that comes back must not need an external restart.

        The loop keeps running through the failures, so the recovery costs
        nothing: the reporter marks exactly the windows in which the graph
        was failing and goes back to reporting numbers afterwards.
        """
        from shared_audio.capture import winrt_capture as mod

        remaining = [mod.POLL_FAILURES_BEFORE_DEAD]
        observed_dead = _Handoff()

        def get_frame():
            if remaining[0] > 0:
                remaining[0] -= 1
                raise RuntimeError("device removed")
            # Park here so the dead window cannot close before the test
            # reads it; a fixed sleep would race the poll interval.
            observed_dead.wait_here(5.0)
            return None

        capture = WinRTAudioCapture()
        with _winrt_polling_a_node(capture, get_frame):
            capture.start()
            try:
                # The same setup barrier the dead-graph test above needs,
                # and for the same reason: without it the first poll for
                # False is answered by the unfinished setup, the dead
                # window is never observed, and the second poll is then
                # answered by the readiness that setup itself wrote -- so
                # the recovery this test is named for never happens and
                # the mutation that removes it survives
                # (wh-stt-load-metrics.2.1.18).
                assert capture._setup_done.wait(5.0) is True
                assert capture._setup_ok is True
                dead = _wait_until(
                    lambda: capture.wait_ready(timeout=0.0) is False, 5.0)
                # Read the park's own answer before the window's, so a
                # delayed test thread names its cause instead of reporting
                # a graph that will not die (wh-stt-load-metrics.2.1.19).
                assert observed_dead.timed_out is False, (
                    'the park in get_frame gave up before the test read '
                    'the dead window, so the graph came back on its own '
                    'clock and no reading below is the recovery this test '
                    'names')
                assert dead is True
                observed_dead.set()
                assert _wait_until(
                    lambda: capture.wait_ready(timeout=0.0) is True, 5.0)
            finally:
                observed_dead.set()
                capture.stop()

    def test_a_capture_thread_that_outlives_stop_does_not_answer_ready(
            self, mock_winrt_available):
        """stop() joins for 2s while setup budgets two 5s WinRT calls.

        A thread still inside _setup_graph when that join expires goes on to
        write _setup_ok and _capture_alive, so a provider that has been
        stopped starts answering ready again with no one having started it.
        The comment at stop() claimed the join covered this; the join is
        bounded and the setup is not.

        The handshake itself is a different matter and is deliberately not
        asserted here: the thread must still set _setup_done on its way out,
        or a caller blocked in wait_ready() when stop() landed would wait
        out its whole timeout for an answer the provider already has.
        """
        setup_entered = threading.Event()
        release_setup = threading.Event()
        poll_entered = threading.Event()
        release_poll = threading.Event()

        def slow_setup():
            setup_entered.set()
            release_setup.wait(10.0)
            return _graph_triple()

        def blocking_poll(*_):
            poll_entered.set()
            release_poll.wait(5.0)

        capture = WinRTAudioCapture()
        stale = None
        with patch.object(capture, '_setup_graph', side_effect=slow_setup), \
                patch.object(capture, '_poll_frames',
                             side_effect=blocking_poll), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert setup_entered.wait(5.0) is True
                stale = capture._capture_thread
                capture.stop()
                assert stale.is_alive() is True

                # Let the stale thread run all the way out, then JOIN it, so
                # the flags below are read after it had its chance to write
                # them. This used to release the setup and wait one second
                # on a poll that correct code never reaches, and discard the
                # answer: a host that had not yet scheduled the stale thread
                # gave exactly the same reading as a stale thread that ran
                # and wrote nothing (wh-stt-load-metrics.2.1.22).
                release_setup.set()
                release_poll.set()
                stale.join(timeout=5.0)
                assert stale.is_alive() is False, (
                    'the stale capture thread never finished, so the flags '
                    'below say only that it had not got there yet')
                # _poll_frames runs only for a cycle _publish_cycle
                # installed, so reaching it IS the stopped cycle publishing
                # itself.
                assert poll_entered.is_set() is False, (
                    'the stopped cycle reached _poll_frames, so it was '
                    'published after stop() had cleared it')

                assert capture._setup_ok is False
                assert capture._capture_alive is False
                assert capture.wait_ready(timeout=0.0) is False
            finally:
                release_setup.set()
                release_poll.set()
                if stale is not None:
                    stale.join(timeout=5.0)

    def test_a_stale_capture_thread_does_not_disturb_the_next_cycle(
            self, mock_winrt_available):
        """The old thread must not answer, or tear down, a restarted graph.

        It writes the same fields and calls the same _cleanup_graph, and by
        then self._graph is the NEW cycle's graph -- so the stale thread
        stops and closes a graph another thread is polling, and the fresh
        provider reports itself dead.

        This first required that the stale thread not call _cleanup_graph at
        all, which was the only safe answer while _setup_graph installed the
        graph on the provider: the thread had no reference to its own. It
        now passes what it built, so it must call cleanup, and what it must
        not do is pass the live cycle's graph (wh-stt-load-metrics.2.1.3).
        """
        first_setup_entered = threading.Event()
        release_first = threading.Event()
        release_poll = threading.Event()
        stale = {}
        lock = threading.Lock()
        setup_calls = []

        def setup():
            with lock:
                first = not setup_calls
                setup_calls.append(1)
            if first:
                first_setup_entered.set()
                release_first.wait(10.0)
            return _graph_triple()

        def poll(*_):
            if threading.current_thread() is stale.get('thread'):
                # Let the stale thread run its finally, which is where the
                # cross-cycle damage happens.
                return
            release_poll.wait(5.0)

        cleanup = Mock()
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph', side_effect=setup), \
                patch.object(capture, '_poll_frames', side_effect=poll), \
                patch.object(capture, '_cleanup_graph', cleanup), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert first_setup_entered.wait(5.0) is True
                stale['thread'] = capture._capture_thread
                capture.stop()

                capture.start()
                assert capture.wait_ready(timeout=5.0) is True

                release_first.set()
                stale['thread'].join(timeout=5.0)
                assert stale['thread'].is_alive() is False

                assert capture.wait_ready(timeout=0.0) is True
                assert cleanup.call_count == 1
                assert capture._graph is not None
                assert cleanup.call_args.args[0] is not capture._graph
            finally:
                release_first.set()
                release_poll.set()
                capture.stop()

    def test_a_stale_thread_does_not_answer_while_the_next_cycle_sets_up(
            self, mock_winrt_available):
        """The restart, caught at the moment it does the most damage.

        start() sets _running True again, so a guard that tests that flag
        alone lets the OLD thread through. Its setup finishing while the NEW
        cycle is still building its graph is the worst timing of all: it
        releases the new cycle's handshake, so a caller gates startup on a
        graph that does not exist yet, and then both threads poll one frame
        output node. Only the cycle number separates the two threads here --
        every other piece of state they share reads the same.
        """
        entered = [threading.Event(), threading.Event()]
        releases = [threading.Event(), threading.Event()]
        release_poll = threading.Event()
        stale = {}
        lock = threading.Lock()
        order = []

        def setup():
            with lock:
                n = len(order)
                order.append(n)
            entered[n].set()
            releases[n].wait(10.0)
            return _graph_triple()

        def poll(*_):
            if threading.current_thread() is stale.get('thread'):
                return
            release_poll.wait(5.0)

        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph', side_effect=setup), \
                patch.object(capture, '_poll_frames', side_effect=poll), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert entered[0].wait(5.0) is True
                stale['thread'] = capture._capture_thread
                capture.stop()

                capture.start()
                assert entered[1].wait(5.0) is True

                releases[0].set()
                stale['thread'].join(timeout=5.0)
                assert stale['thread'].is_alive() is False

                assert capture._setup_done.is_set() is False
                assert capture.wait_ready(timeout=0.0) is False

                releases[1].set()
                assert capture.wait_ready(timeout=5.0) is True
            finally:
                releases[0].set()
                releases[1].set()
                release_poll.set()
                capture.stop()

    def test_winrt_wait_does_not_block_once_the_graph_is_dead(
            self, mock_winrt_available):
        """The dead answer must be immediate.

        The load reporter asks with timeout=0.0 on every loop iteration, and
        a caller that gated startup with 15.0 must not pay 15 seconds to
        learn what the provider already knows.
        """
        capture = WinRTAudioCapture()
        with patch.object(capture, '_setup_graph',
                          return_value=_graph_triple()), \
                patch.object(capture, '_poll_frames'), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                capture._capture_thread.join(timeout=5.0)
                started = time.time()
                assert capture.wait_ready(timeout=15.0) is False
                assert time.time() - started < 2.0
            finally:
                capture.stop()


def _float32_chunk(capture):
    """One full chunk's worth of float32 input bytes.

    _poll_frames converts float32 to int16, so a chunk of target_bytes int16
    needs twice that many bytes in.
    """
    samples = capture.config.bytes_per_chunk // 2
    return struct.pack('<' + 'f' * samples, *([0.5] * samples))


def _frame_carrying(float32_data):
    """A stand-in AudioFrame the real _poll_frames can decode.

    lock_buffer's reference only has to support the buffer protocol, which
    bytes already does, so nothing here has to imitate WinRT beyond the two
    calls the loop makes.
    """
    buffer = Mock()
    buffer.create_reference = Mock(return_value=float32_data)
    buffer.close = Mock()
    frame = Mock()
    frame.lock_buffer = Mock(return_value=buffer)
    return frame


def _original_float_pcm(data):
    """Scalar conversion from af720ab6, including trailing-byte truncation."""
    count = len(data) // 4
    samples = struct.unpack('<' + 'f' * count, data[:count * 4])
    values = [max(-32768, min(32767, int(s * 32767))) for s in samples]
    return struct.pack('<' + 'h' * count, *values)


def _poll_synthetic_frames(monkeypatch, payloads):
    """Drive the real conversion, buffering, queue and error path, without a thread."""
    import shared_audio.capture.winrt_capture as module

    capture = WinRTAudioCapture()
    capture._running = True
    frames = [_frame_carrying(payload) for payload in payloads]
    remaining = iter(frames)
    alive_before_poll = []

    def get_frame():
        alive_before_poll.append(capture._capture_alive)
        frame = next(remaining, None)
        if frame is None:
            capture._running = False
        return frame

    # Bound the loop from OUTSIDE the frame supply, the way every sibling
    # that drives the real _poll_frames bounds it: those tests run the loop
    # on the capture thread and end it with capture.stop(), which clears
    # _running (test_winrt_stops_being_ready_when_every_frame_poll_raises,
    # test_winrt_stays_ready_through_one_failed_frame_poll,
    # test_winrt_is_ready_again_once_the_frame_polls_recover). This helper
    # runs the loop inline, so the fake sleep the loop already calls once
    # per iteration is where the same clear goes. Without it the only exit
    # is get_frame running out of payloads, so the loop's termination hung
    # off the very call these tests measure: a loop that polls something
    # else never reaches the end of the payloads and spins at a full core
    # forever instead of failing (measured 2026-09-15 under gate mutation
    # winrt-the-poll-loop-reads-the-shared-node, which redirects the poll
    # onto the provider's shared _frame_output; the run was aborted at this
    # test, so that mutation's own catcher never ran).
    #
    # The budget is a fixed multiple of the iterations this helper can
    # produce -- one per payload plus the final empty poll, and at most one
    # sleep in each -- so no green run can reach it. It counts iterations
    # rather than seconds because a loaded host moves seconds and cannot
    # move this.
    poll_budget = (len(payloads) + 1) * 20
    budget_left = [poll_budget]
    bound_fired = []

    def bounded_sleep(seconds):
        budget_left[0] -= 1
        if budget_left[0] <= 0:
            capture._running = False
            bound_fired.append(seconds)

    monkeypatch.setattr(module, 'time', SimpleNamespace(sleep=bounded_sleep))
    capture._poll_frames(capture._cycle, SimpleNamespace(get_frame=get_frame))
    # Read the bound's own answer before any reading of the loop's work,
    # the _Handoff.timed_out rule applied to an inline loop: a loop the
    # test had to stop must say so, not report whatever conversion it
    # happened to manage.
    assert not bound_fired, (
        f'the poll loop ran past {poll_budget} iterations for '
        f'{len(payloads)} payloads, so the test had to stop it. It never '
        'consumed the frames handed to it, so nothing below is a reading '
        'of the conversion this helper exists to drive')
    chunks = []
    while not capture._q.empty():
        chunks.append(capture._q.get_nowait())
    for frame in frames:
        frame.lock_buffer.return_value.close.assert_called_once_with()
    return capture, b''.join(chunks), alive_before_poll


@pytest.mark.usefixtures('mock_winrt_available', '_mock_winsdk_modules')
class TestFloatConversionEquivalence:
    def test_boundary_and_random_float32_frames_keep_exact_pcm(self, monkeypatch):
        randomizer = random.Random(20260908)
        values = [-0.0, 0.0, -1.0, 1.0, -2.0, 2.0, 0.5, -0.5]
        # Neighbouring float32 encodings around integer conversion boundaries.
        for integer in [1, 3, 17, 16383, 16384, 32766, 32767]:
            bits = struct.unpack('<I', struct.pack('<f', integer / 32767))[0]
            for offset in [-1, 0, 1]:
                value = struct.unpack('<f', struct.pack('<I', bits + offset))[0]
                values.extend([value, -value])
        while len(values) < 1920:
            value = struct.unpack('<f', randomizer.getrandbits(32).to_bytes(4, 'little'))[0]
            if math.isfinite(value):
                values.append(value)
        data = struct.pack('<' + 'f' * len(values), *values)
        capture, actual, _ = _poll_synthetic_frames(monkeypatch, [data])
        assert actual == _original_float_pcm(data)
        assert capture._frames_captured == 4
        assert capture._drops == 0

    def test_short_frames_trailing_bytes_and_rechunking_are_unchanged(self, monkeypatch):
        data = struct.pack('<' + 'f' * 960, *([0.5, -0.5, 1.0, -1.0] * 240))
        payloads = [b'', b'x', b'xx', b'xxx']
        for offset in range(0, len(data), 548):
            payloads.append(data[offset:offset + 548] + b'xyz')
        capture, actual, _ = _poll_synthetic_frames(monkeypatch, payloads)
        assert actual == _original_float_pcm(data)
        assert capture._frames_captured == 2
        assert capture._drops == 0

    @pytest.mark.parametrize('bad', [float('nan'), float('inf'), -float('inf')])
    def test_nonfinite_frames_are_refused_and_next_valid_frame_recovers(self, monkeypatch, caplog, bad):
        bad_data = struct.pack('<' + 'f' * 480, *([0.5] * 479 + [bad]))
        good_data = struct.pack('<' + 'f' * 480, *([-0.5] * 480))
        try:
            _original_float_pcm(bad_data)
        except (ValueError, OverflowError) as exc:
            expected = str(exc)
        capture, actual, alive = _poll_synthetic_frames(monkeypatch, [bad_data] * 3 + [good_data])
        assert actual == _original_float_pcm(good_data)
        assert capture._frames_captured == 1
        assert alive[3] is False  # Existing three-failure liveness threshold.
        assert alive[4] is True
        warnings = [record.message for record in caplog.records if 'Frame poll error:' in record.message]
        assert warnings == [f'Frame poll error: {expected}'] * 3


class _CycleNodes:
    """Hand each start() cycle its own frame output node.

    Written to satisfy the contract on both sides of
    wh-stt-load-metrics.2.1.3: it assigns the attributes, which is what
    _setup_graph did when it built the graph itself, AND returns them, which
    is what it does now that each thread owns what it created. A test using
    this therefore fails against the old code for the behaviour it asserts
    rather than for the shape of the mock.
    """

    def __init__(self, capture, nodes, gates=None):
        self._capture = capture
        self._nodes = list(nodes)
        # Keyed by cycle index rather than hung off the node, because a bare
        # Mock answers every attribute and would hand back a callable for a
        # cycle that was meant to have no gate at all.
        self._gates = dict(gates or {})
        self._lock = threading.Lock()
        self._calls = 0
        self.graphs = []

    def __call__(self):
        with self._lock:
            index = self._calls
            self._calls += 1
        node = self._nodes[min(index, len(self._nodes) - 1)]
        graph, mic_node = Mock(name=f"graph{index}"), Mock()
        with self._lock:
            self.graphs.append(graph)
        gate = self._gates.get(index)
        if gate is not None:
            gate()
        self._capture._graph = graph
        self._capture._mic_node = mic_node
        self._capture._frame_output = node
        return graph, mic_node, node


class TestCaptureCycleIsolation:
    """A thread from a previous start() must not reach the current one.

    wh-stt-load-metrics.2.1.3. The cycle number added in 9bcdc495 guarded
    the liveness flags only, and the commit message claimed more than that:
    _setup_graph installed the graph, the microphone node and the frame
    output on the provider with no guard at all, and the poll loop ran on
    _running alone, so a thread that outlived stop()'s bounded join went on
    to hand its own node to the live cycle and to write the live cycle's
    queue and counters -- the very counters this bead exists to measure.
    """

    def test_a_stale_setup_does_not_make_the_live_cycle_report_itself_dead(
            self, mock_winrt_available, _mock_winsdk_modules):
        """The live cycle must poll the node it built, not a later arrival.

        stop() joins for 2s and _setup_graph budgets two 5s WinRT calls, so
        a restart can complete while the previous thread is still building.
        That thread's graph then landed on the provider, and the live poll
        loop -- which read the attribute afresh every iteration -- started
        polling a node belonging to a graph nobody was reading.
        """
        healthy = Mock()
        healthy.get_frame = Mock(return_value=None)

        first_entered = threading.Event()
        release_first = _Handoff()
        dead = Mock()
        dead.get_frame = Mock(side_effect=RuntimeError("device gone"))

        def first_gate():
            first_entered.set()
            release_first.wait_here(10.0)

        capture = WinRTAudioCapture()
        nodes = _CycleNodes(capture, [dead, healthy], {0: first_gate})
        stale = None
        with patch.object(capture, '_setup_graph', side_effect=nodes), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert first_entered.wait(5.0) is True
                stale = capture._capture_thread
                capture.stop()

                capture.start()
                assert capture.wait_ready(timeout=5.0) is True

                release_first.set()
                stale.join(timeout=5.0)
                assert stale.is_alive() is False

                assert release_first.timed_out is False, (
                    "the stale cycle's setup gave up before the test "
                    'released it, so it finished before the restart and '
                    'never overlapped the live cycle at all')
                # The stale node raises on every poll, so three polls of it
                # would take readiness away. The live cycle is healthy and
                # must stay ready.
                assert _wait_until(
                    lambda: capture.wait_ready(timeout=0.0) is False,
                    2.0) is False
                assert dead.get_frame.call_count == 0
            finally:
                release_first.set()
                if stale is not None:
                    stale.join(timeout=5.0)
                capture.stop()

    def test_a_stale_poll_loop_ends_when_a_new_cycle_replaces_it(
            self, mock_winrt_available, _mock_winsdk_modules):
        """The loop ran on _running, which a restart sets True again.

        A thread blocked inside get_frame when stop()'s join expires resumes
        after start(), finds _running True, and keeps polling for the life
        of the process. Two threads then poll and two threads then write.
        """
        blocked = threading.Event()
        release = threading.Event()
        polls = []
        poll_lock = threading.Lock()

        def stale_frame():
            with poll_lock:
                polls.append(1)
                first = len(polls) == 1
            if first:
                blocked.set()
                release.wait(10.0)
            return None

        stale_node = Mock()
        stale_node.get_frame = stale_frame
        live_node = Mock()
        live_node.get_frame = Mock(return_value=None)

        capture = WinRTAudioCapture()
        nodes = _CycleNodes(capture, [stale_node, live_node])
        stale = None
        with patch.object(capture, '_setup_graph', side_effect=nodes), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert blocked.wait(5.0) is True
                stale = capture._capture_thread
                capture.stop()
                assert stale.is_alive() is True

                capture.start()
                assert capture.wait_ready(timeout=5.0) is True

                release.set()
                stale.join(timeout=5.0)
                assert stale.is_alive() is False
            finally:
                release.set()
                if stale is not None:
                    stale.join(timeout=5.0)
                capture.stop()

    def test_a_stale_iteration_in_flight_does_not_feed_the_new_cycle(
            self, mock_winrt_available, _mock_winsdk_modules):
        """The iteration that was already running when the restart landed.

        Exiting at the top of the loop is not enough on its own: a poll that
        was blocked in get_frame when the cycle changed still has a frame to
        deliver, and that audio belongs to a graph the provider is no longer
        reading. It must not reach the new cycle's queue or its counters,
        which is exactly what the load reporter reads.
        """
        blocked = threading.Event()
        release = _Handoff()
        polls = []
        poll_lock = threading.Lock()

        capture = WinRTAudioCapture()
        float32_data = _float32_chunk(capture)

        def stale_frame():
            with poll_lock:
                polls.append(1)
                first = len(polls) == 1
            if first:
                blocked.set()
                release.wait_here(10.0)
                return _frame_carrying(float32_data)
            return None

        stale_node = Mock()
        stale_node.get_frame = stale_frame
        live_node = Mock()
        live_node.get_frame = Mock(return_value=None)

        nodes = _CycleNodes(capture, [stale_node, live_node])
        stale = None
        with patch.object(capture, '_setup_graph', side_effect=nodes), \
                patch.object(capture, '_cleanup_graph'), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert blocked.wait(5.0) is True
                stale = capture._capture_thread
                capture.stop()

                capture.start()
                assert capture.wait_ready(timeout=5.0) is True
                captured_before = capture._frames_captured

                release.set()
                stale.join(timeout=5.0)
                assert stale.is_alive() is False

                assert release.timed_out is False, (
                    'the stale poll gave up before the test released it, '
                    'so its frame was delivered before the restart and '
                    'never had the new cycle to reach')
                assert capture._frames_captured == captured_before
                assert capture._q.qsize() == 0
            finally:
                release.set()
                if stale is not None:
                    stale.join(timeout=5.0)
                capture.stop()

    def test_each_cycle_closes_the_graph_it_created(
            self, mock_winrt_available, _mock_winsdk_modules):
        """Every thread tears down its own resources and only its own.

        Before this, _setup_graph assigned self._graph, so the graph a stale
        thread built REPLACED the live one on the provider. The live cycle
        then closed the stale graph and its own was left running with
        nothing holding a reference to it -- the opposite of what the
        crewcut comment in the teardown claimed.
        """
        first_entered = threading.Event()
        release_first = _Handoff()
        stale_node = Mock()
        stale_node.get_frame = Mock(return_value=None)
        live_node = Mock()
        live_node.get_frame = Mock(return_value=None)

        def first_gate():
            first_entered.set()
            release_first.wait_here(10.0)

        capture = WinRTAudioCapture()
        nodes = _CycleNodes(capture, [stale_node, live_node],
                            {0: first_gate})
        stale = None
        with patch.object(capture, '_setup_graph', side_effect=nodes), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert first_entered.wait(5.0) is True
                stale = capture._capture_thread
                capture.stop()

                capture.start()
                assert capture.wait_ready(timeout=5.0) is True

                release_first.set()
                stale.join(timeout=5.0)
                assert stale.is_alive() is False

                assert release_first.timed_out is False, (
                    "the stale cycle's setup gave up before the test "
                    'released it, so it built its graph before the '
                    'restart and the two never contended for the '
                    "provider's fields")
                stale_graph, live_graph = nodes.graphs[0], nodes.graphs[1]
                assert stale_graph is not live_graph
                assert stale_graph.close.call_count == 1
                assert live_graph.close.call_count == 0

                capture.stop()
                assert _wait_until(
                    lambda: live_graph.close.call_count == 1, 5.0) is True
                assert stale_graph.close.call_count == 1
            finally:
                release_first.set()
                if stale is not None:
                    stale.join(timeout=5.0)
                capture.stop()

    def test_retiring_a_stale_cycle_changes_nothing(self):
        """The teardown transition belongs to one cycle only."""
        capture = WinRTAudioCapture()
        graph = Mock()
        capture._cycle = 2
        capture._capture_alive = True
        capture._graph = graph
        capture._setup_done.clear()

        assert capture._retire_cycle(1) is False

        assert capture._capture_alive is True
        assert capture._graph is graph
        assert capture._setup_done.is_set() is False

    def test_publishing_a_stale_cycle_installs_nothing(self):
        """A stale thread's resources must never reach the provider."""
        capture = WinRTAudioCapture()
        capture._cycle = 2
        capture._running = True
        capture._setup_done.clear()

        assert capture._publish_cycle(1, Mock(), Mock(), Mock()) is False

        assert capture._graph is None
        assert capture._frame_output is None
        assert capture._setup_ok is False
        assert capture._capture_alive is False
        assert capture._setup_done.is_set() is False

    def test_the_retirement_transition_holds_the_lifecycle_lock(self):
        """Reading the cycle and writing the state must be one step.

        The guard was a plain `if self._cycle == cycle:` followed by the
        writes. A thread descheduled between the two -- the saturated
        machine this bead measures -- passes a test that was true and then
        writes into a cycle that has since been replaced.
        """
        capture = WinRTAudioCapture()
        capture._cycle = 1
        capture._capture_alive = True
        finished = threading.Event()
        watched = _WatchedLock(capture._lifecycle_lock)
        capture._lifecycle_lock = watched

        with watched:
            worker = threading.Thread(
                target=lambda: (capture._retire_cycle(1), finished.set()),
                daemon=True)
            worker.start()
            assert watched.wait_for_a_blocked_thread(5.0) is True, (
                'the worker never parked at the lifecycle lock, so nothing '
                'below this line would say whether the transition is guarded')
            assert finished.is_set() is False
            assert capture._capture_alive is True

        assert finished.wait(5.0) is True
        assert capture._capture_alive is False
        worker.join(timeout=5.0)

    def test_the_lifecycle_lock_is_a_real_mutual_exclusion_lock(self):
        """The wrapper the other lock tests install would hide a no-op lock.

        Those tests wrap whatever the constructor built, so they prove the
        transitions wait for that object. They cannot prove the object
        excludes anything. This one does, and it needs no threads: a lock
        that is held cannot be taken again.
        """
        capture = WinRTAudioCapture()
        lock = capture._lifecycle_lock

        assert hasattr(lock, 'acquire'), (
            '_lifecycle_lock is not a lock at all: %r' % (lock,))
        with lock:
            assert lock.acquire(blocking=False) is False, (
                'a second acquire succeeded while the lock was held, so the '
                'lifecycle lock excludes nothing')

    def test_the_publish_transition_holds_the_lifecycle_lock(self):
        """The same read-then-write, on the way in rather than out."""
        capture = WinRTAudioCapture()
        capture._cycle = 1
        capture._running = True
        finished = threading.Event()
        watched = _WatchedLock(capture._lifecycle_lock)
        capture._lifecycle_lock = watched

        with watched:
            worker = threading.Thread(
                target=lambda: (capture._publish_cycle(1, Mock(), Mock(),
                                                       Mock()),
                                finished.set()),
                daemon=True)
            worker.start()
            assert watched.wait_for_a_blocked_thread(5.0) is True, (
                'the worker never parked at the lifecycle lock, so nothing '
                'below this line would say whether the transition is guarded')
            assert finished.is_set() is False
            assert capture._setup_ok is False

        assert finished.wait(5.0) is True
        assert capture._setup_ok is True
        worker.join(timeout=5.0)


class TestWinRTCaptureStartStop:
    """Test start/stop lifecycle."""

    def test_start_raises_when_winrt_unavailable(self):
        """Should raise RuntimeError when WinRT not available."""
        with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', False):
            capture = WinRTAudioCapture()

            with pytest.raises(RuntimeError, match="WinRT audio APIs not available"):
                capture.start()

    def test_start_sets_running_flag(self, mock_winrt_available):
        """Should set _running flag and start_time."""
        with patch.object(threading.Thread, 'start'):
            capture = WinRTAudioCapture()
            start_time_before = time.time()

            capture.start()

            assert capture._running is True
            assert capture._start_time is not None
            assert capture._start_time >= start_time_before

    def test_start_creates_capture_thread(self, mock_winrt_available):
        """Should create and start background capture thread."""
        with patch.object(threading.Thread, 'start') as mock_thread_start:
            with patch.object(threading.Thread, '__init__', return_value=None) as mock_thread_init:
                capture = WinRTAudioCapture()
                capture.start()

                # Thread should be created
                assert capture._capture_thread is not None
                mock_thread_start.assert_called_once()

    def test_start_when_already_running_is_noop(self, mock_winrt_available):
        """Should be no-op when already running."""
        with patch.object(threading.Thread, 'start'):
            capture = WinRTAudioCapture()
            capture.start()
            first_thread = capture._capture_thread

            capture.start()  # Second start

            # Should not create new thread
            assert capture._capture_thread is first_thread

    def test_stop_clears_running_flag(self):
        """Should clear _running flag."""
        capture = WinRTAudioCapture()
        capture._running = True
        capture._capture_thread = None

        capture.stop()

        assert capture._running is False

    def test_stop_joins_capture_thread(self, mock_winrt_available):
        """Should join capture thread with timeout."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join') as mock_join:
                capture = WinRTAudioCapture()
                capture.start()

                capture.stop()

                mock_join.assert_called_once_with(timeout=2.0)
                assert capture._capture_thread is None

    def test_stop_clears_queue_when_running(self, mock_winrt_available):
        """Should clear audio queue when stopping from running state."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join'):
                capture = WinRTAudioCapture()
                capture.start()  # Set _running = True
                # Put some items in queue
                capture._q.put(b'test1')
                capture._q.put(b'test2')
                assert capture._q.qsize() == 2

                capture.stop()

                assert capture._q.qsize() == 0

    def test_stop_when_not_running_is_noop(self):
        """Should be no-op when not running."""
        capture = WinRTAudioCapture()
        # Should not raise
        capture.stop()
        assert capture._running is False


class TestWinRTCaptureRead:
    """Test audio reading."""

    def test_read_returns_audio_from_queue(self):
        """Should return audio from internal queue."""
        capture = WinRTAudioCapture()
        test_audio = b'\x01\x02\x03\x04'
        capture._q.put(test_audio)

        audio = capture.read(timeout=1.0)

        assert audio == test_audio

    def test_read_returns_none_on_timeout(self):
        """Should return None when queue is empty and timeout expires."""
        capture = WinRTAudioCapture()

        audio = capture.read(timeout=0.1)

        assert audio is None

    def test_read_respects_timeout(self):
        """Should wait for specified timeout."""
        capture = WinRTAudioCapture()
        start_time = time.time()

        audio = capture.read(timeout=0.2)

        elapsed = time.time() - start_time
        assert audio is None
        assert elapsed >= 0.2
        assert elapsed < 0.3  # Should not wait much longer


class TestWinRTCaptureStats:
    """Test statistics and monitoring."""

    def test_get_stats_returns_current_stats(self):
        """Should return current capture statistics."""
        capture = WinRTAudioCapture()
        capture._frames_captured = 100
        capture._drops = 5
        capture._max_queue_depth = 10
        capture._q.put(b'test')

        stats = capture.get_stats()

        assert stats['captured'] == 100
        assert stats['drops'] == 5
        assert stats['qsize'] == 1
        assert stats['max_q'] == 10

    def test_get_queue_size_returns_current_qsize(self):
        """Should return current queue size."""
        capture = WinRTAudioCapture()
        capture._q.put(b'a')
        capture._q.put(b'b')
        capture._q.put(b'c')

        qsize = capture.get_queue_size()

        assert qsize == 3

    def test_reset_overflow_monitor_delegates(self):
        """Should delegate to overflow_monitor.reset_for_restart()."""
        capture = WinRTAudioCapture()
        capture.overflow_monitor.reset_for_restart = Mock()

        capture.reset_overflow_monitor()

        capture.overflow_monitor.reset_for_restart.assert_called_once()

    def test_get_overflow_status_delegates(self):
        """Should delegate to overflow_monitor.get_status()."""
        capture = WinRTAudioCapture()
        mock_status = {'count': 3, 'last_time': 100}
        capture.overflow_monitor.get_status = Mock(return_value=mock_status)

        status = capture.get_overflow_status()

        assert status == mock_status
        capture.overflow_monitor.get_status.assert_called_once()


class TestWinRTCaptureGraphSetup:
    """Test WinRT graph setup."""

    def test_setup_graph_creates_audio_graph(self, mock_winrt_available, mock_winrt_graph):
        """Should create AudioGraph with correct settings."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        graph, _, _ = capture._setup_graph()

        # Should call run_winrt_sync to create graph
        assert mocks['run_sync'].call_count >= 1
        assert graph == mocks['graph']
        # Installing it is _publish_cycle's job, and only for a cycle that
        # is still current (wh-stt-load-metrics.2.1.3).
        assert capture._graph is None

    def test_setup_graph_creates_microphone_input(self, mock_winrt_available, mock_winrt_graph):
        """Should create device input node for microphone."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        _, mic_node, _ = capture._setup_graph()

        assert mic_node == mocks['input_node']
        assert capture._mic_node is None

    def test_setup_graph_creates_frame_output(self, mock_winrt_available, mock_winrt_graph):
        """Should create frame output node."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        _, _, frame_output = capture._setup_graph()

        assert frame_output == mocks['frame_output']
        assert capture._frame_output is None

    def test_setup_graph_connects_nodes(self, mock_winrt_available, mock_winrt_graph):
        """Should connect mic to frame output."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        mocks['input_node'].add_outgoing_connection.assert_called_once_with(
            mocks['frame_output']
        )

    def test_setup_graph_starts_graph(self, mock_winrt_available, mock_winrt_graph):
        """Should start the AudioGraph."""
        mocks = mock_winrt_graph
        capture = WinRTAudioCapture()

        capture._setup_graph()

        mocks['graph'].start.assert_called_once()

    def test_setup_graph_raises_on_graph_creation_failure(self, mock_winrt_available, mock_winrt_graph):
        """Should raise RuntimeError when graph creation fails."""
        mocks = mock_winrt_graph
        # 2 is FORMAT_NOT_SUPPORTED. Not 1: DEVICE_NOT_AVAILABLE has its
        # own approved wording, pinned by
        # TestAMissingAudioOutputDeviceHasItsOwnRefusal below
        # (wh-capture-winrt-required A10).
        mocks['create_result'].status = 2
        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError, match="AudioGraph creation failed"):
            capture._setup_graph()

    def test_setup_graph_raises_on_mic_node_failure(self, mock_winrt_available, mock_winrt_graph):
        """Should raise RuntimeError when mic node creation fails."""
        mocks = mock_winrt_graph
        mocks['input_result'].status = 1  # Non-zero status = failure
        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError, match="Microphone node failed"):
            capture._setup_graph()


@pytest.fixture
def winrt_graph_with_real_categories():
    """Mock the AudioGraph but keep the real winsdk category enums.

    The other WinRT fixture in this file replaces AudioRenderCategory and
    MediaCategory with mocks, which makes whatever category the code
    passes compare equal to whatever category a test names. These tests
    assert on the constants themselves, so they need the real enums.
    winsdk is a required dependency of the shared package, so it is
    installed (wh-capture-winrt-required A1).
    """
    import sys

    mock_utils = MagicMock()
    mock_run_sync = Mock()
    mock_utils.winrt_helpers.run_winrt_sync = mock_run_sync
    sys.modules['utils'] = mock_utils
    sys.modules['utils.winrt_helpers'] = mock_utils.winrt_helpers

    try:
        with patch('winsdk.windows.media.audio.AudioGraph'):
            with patch('winsdk.windows.media.audio.AudioGraphSettings') as MockSettings:
                with patch('winsdk.windows.media.mediaproperties.AudioEncodingProperties'):
                    mock_graph = Mock()
                    mock_input_node = Mock()
                    mock_run_sync.side_effect = [
                        Mock(status=0, graph=mock_graph),
                        Mock(status=0, device_input_node=mock_input_node),
                    ]

                    yield {
                        'graph': mock_graph,
                        'settings_class': MockSettings,
                    }
    finally:
        sys.modules.pop('utils', None)
        sys.modules.pop('utils.winrt_helpers', None)


class TestCaptureOpensInTheCommunicationsCategory:
    """A1/A3 of wh-screen-reader-audio-suppression-conflict.

    Windows runs its acoustic echo canceller on a capture stream that asks
    for the Communications category. That canceller is what lets
    WheelHouse keep listening while a screen reader talks through the
    speakers.
    """

    def test_the_graph_asks_for_the_communications_render_category(
        self, mock_winrt_available, winrt_graph_with_real_categories
    ):
        """AudioGraphSettings takes AudioRenderCategory.COMMUNICATIONS."""
        from winsdk.windows.media.render import AudioRenderCategory

        capture = WinRTAudioCapture()
        capture._setup_graph()

        settings_class = winrt_graph_with_real_categories['settings_class']
        assert settings_class.call_args.args[0] == AudioRenderCategory.COMMUNICATIONS

    def test_the_microphone_asks_for_the_communications_media_category(
        self, mock_winrt_available, winrt_graph_with_real_categories
    ):
        """create_device_input_node_async takes MediaCategory.COMMUNICATIONS."""
        from winsdk.windows.media.capture import MediaCategory

        capture = WinRTAudioCapture()
        capture._setup_graph()

        graph = winrt_graph_with_real_categories['graph']
        category = graph.create_device_input_node_async.call_args.args[0]
        assert category == MediaCategory.COMMUNICATIONS


# The refusal David approved on 2026-09-06 for the one AudioGraph failure
# a user can act on, written out here rather than imported, so a reworded
# constant fails this test instead of hiding behind it
# (wh-capture-winrt-required A10).
APPROVED_AUDIO_DEVICE_REFUSAL = (
    "The speech service cannot start. Windows has no audio output device "
    "available, and Windows needs one even to record. Connect or turn on "
    "your speakers, headphones, or TV, then start WheelHouse again."
)

# The developer half, which is logged rather than shown.
APPROVED_AUDIO_DEVICE_LOG_LINE = (
    "AudioGraph creation returned DEVICE_NOT_AVAILABLE (status=1)"
)

# What each provider puts in front of the refusal before it sends the
# notice. Written out here because a shared test cannot import a
# provider. The three sites are distil_medium_en/main.py:595,
# google_stt_server/main.py:238 and
# sherpa_offline_parakeet_stt_server/main.py:655, and all three use these
# exact words.
PROVIDER_NOTICE_PREFIX = "Failed to start - transcription will not work: "

# plyer builds a Windows NOTIFYICONDATAW whose szInfo field is
# WCHAR * 256, so ctypes refuses a longer notice
# (plyer/platforms/win/libs/win_api_defs.py:62).
WINDOWS_NOTICE_LIMIT = 256


class TestAMissingAudioOutputDeviceHasItsOwnRefusal:
    """A10. status=1 is AudioGraphCreationStatus.DEVICE_NOT_AVAILABLE.

    David hit it on 2026-09-06 with the TV switched off: WinRT builds the
    graph against a RENDER device even for a capture-only graph, so a
    machine with no output device available cannot record either. The
    provider carries setup_error into its startup-failed notice, so
    whatever text this raise holds is what the user reads. "AudioGraph
    creation failed: status=1" tells a user nothing they can act on.

    The other statuses keep the developer text. FORMAT_NOT_SUPPORTED and
    UNKNOWN_FAILURE name no action a user could take, and inventing one
    would send someone to check speakers that are already working.
    """

    def test_a_missing_output_device_raises_the_approved_words(
            self, mock_winrt_available, mock_winrt_graph):
        mocks = mock_winrt_graph
        mocks['create_result'].status = 1

        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError) as excinfo:
            capture._setup_graph()

        assert str(excinfo.value) == APPROVED_AUDIO_DEVICE_REFUSAL

    def test_the_missing_output_device_does_not_get_the_winsdk_words(
            self, mock_winrt_available, mock_winrt_graph):
        """The message CHOICE, which is the half an equality test cannot
        show on its own. winsdk imported here -- the graph would not have
        been attempted otherwise -- so telling this user to re-run the
        installer sends them to fix a package that is already installed.
        """
        mocks = mock_winrt_graph
        mocks['create_result'].status = 1

        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError) as excinfo:
            capture._setup_graph()

        message = str(excinfo.value)
        assert "winsdk" not in message
        assert "installer" not in message
        assert "bootstrap" not in message

    def test_another_graph_status_keeps_the_developer_message(
            self, mock_winrt_available, mock_winrt_graph):
        """status=3 is UNKNOWN_FAILURE. Nothing about it says an output
        device is missing, so it must not borrow the words for one.
        """
        mocks = mock_winrt_graph
        mocks['create_result'].status = 3

        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError) as excinfo:
            capture._setup_graph()

        assert str(excinfo.value) == "AudioGraph creation failed: status=3"

    def test_the_developer_detail_is_logged_rather_than_shown(
            self, mock_winrt_available, mock_winrt_graph, caplog):
        """The enum name a log reader needs, kept out of the notice.

        The notice has no room for it. See the length test below.
        """
        mocks = mock_winrt_graph
        mocks['create_result'].status = 1

        capture = WinRTAudioCapture()

        with caplog.at_level(
                logging.ERROR,
                logger="shared_audio.capture.winrt_capture"):
            with pytest.raises(RuntimeError):
                capture._setup_graph()

        assert APPROVED_AUDIO_DEVICE_LOG_LINE in caplog.text

    def test_the_assembled_notice_fits_the_windows_limit(
            self, mock_winrt_available, mock_winrt_graph):
        """The whole notice, prefix included, must fit in 256 characters.

        plyer builds a Windows NOTIFYICONDATAW whose szInfo field is
        WCHAR * 256. ctypes refuses a longer string, and plyer raises it
        on its own thread (plyer/platforms/win/notification.py:17), so a
        longer notice shows NOTHING at all rather than showing a cut one.
        Measured against a 272-character constant this test failed with
        319 > 256.
        """
        mocks = mock_winrt_graph
        mocks['create_result'].status = 1

        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError) as excinfo:
            capture._setup_graph()

        notice = PROVIDER_NOTICE_PREFIX + str(excinfo.value)
        assert len(notice) <= WINDOWS_NOTICE_LIMIT


class TestWinRTCaptureCleanup:
    """Test resource cleanup."""

    def test_cleanup_graph_stops_and_closes_graph(self):
        """Should stop and close the graph."""
        capture = WinRTAudioCapture()
        mock_graph = Mock()
        mock_graph.stop = Mock()
        mock_graph.close = Mock()
        capture._graph = mock_graph

        capture._cleanup_graph()

        mock_graph.stop.assert_called_once()
        mock_graph.close.assert_called_once()
        assert capture._graph is None

    def test_cleanup_graph_clears_nodes(self):
        """Should clear node references."""
        capture = WinRTAudioCapture()
        capture._graph = Mock()
        capture._mic_node = Mock()
        capture._frame_output = Mock()

        capture._cleanup_graph()

        assert capture._mic_node is None
        assert capture._frame_output is None

    def test_cleanup_graph_handles_exceptions(self):
        """Should handle cleanup errors gracefully."""
        capture = WinRTAudioCapture()
        mock_graph = Mock()
        mock_graph.stop.side_effect = Exception("Stop failed")
        capture._graph = mock_graph

        # Should not raise
        capture._cleanup_graph()

    def test_cleanup_graph_when_no_graph(self):
        """Should be no-op when no graph exists."""
        capture = WinRTAudioCapture()
        # Should not raise
        capture._cleanup_graph()


class TestWinRTCaptureListDevices:
    """Test device enumeration."""

    def test_list_audio_devices_returns_empty_when_winrt_unavailable(self):
        """Should return empty list when WinRT not available."""
        with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', False):
            capture = WinRTAudioCapture()

            devices = capture.list_audio_devices()

            assert devices == []

    def test_list_audio_devices_returns_empty_on_import_error(self, mock_winrt_available):
        """Should return empty list when winrt_helpers unavailable."""
        with patch('shared_audio.capture.winrt_capture.WINRT_AUDIO_AVAILABLE', True):
            # Mock the imports to raise ImportError
            with patch('builtins.__import__', side_effect=ImportError("No winrt_helpers")):
                capture = WinRTAudioCapture()

                devices = capture.list_audio_devices()

                assert devices == []

    def test_list_audio_devices_handles_enumeration_errors(self, mock_winrt_available, _mock_winsdk_modules):
        """Should return empty list on enumeration errors."""
        with patch('winsdk.windows.devices.enumeration.DeviceInformation') as MockDevInfo:
            MockDevInfo.find_all_async.side_effect = Exception("Enumeration failed")
            capture = WinRTAudioCapture()

            devices = capture.list_audio_devices()

            assert devices == []


class TestWinRTCaptureAdversarial:
    """Adversarial tests for edge cases."""

    def test_multiple_start_stop_cycles(self, mock_winrt_available):
        """Should handle multiple start/stop cycles."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join'):
                capture = WinRTAudioCapture()

                for i in range(3):
                    capture.start()
                    assert capture._running is True

                    capture.stop()
                    assert capture._running is False

    def test_read_after_stop_returns_none(self, mock_winrt_available):
        """Should return None when reading after stop (queue cleared)."""
        with patch.object(threading.Thread, 'start'):
            with patch.object(threading.Thread, 'join'):
                capture = WinRTAudioCapture()
                capture.start()  # Set _running = True
                capture._q.put(b'test')
                capture.stop()  # Clears queue because _running was True

                audio = capture.read(timeout=0.1)

                assert audio is None

    def test_queue_overflow_increments_drops(self):
        """Should increment drops counter when queue is full."""
        capture = WinRTAudioCapture()
        capture.overflow_monitor.report_overflow = Mock()

        # Fill the queue to maxsize
        for i in range(capture._q.maxsize):
            capture._q.put(b'x')

        # Now try to add more (this is what _poll_frames does)
        try:
            capture._q.put_nowait(b'overflow')
        except queue.Full:
            capture._drops += 1
            capture.overflow_monitor.report_overflow()

        assert capture._drops == 1
        capture.overflow_monitor.report_overflow.assert_called_once()

    def test_max_queue_depth_tracking(self):
        """Should track maximum queue depth."""
        capture = WinRTAudioCapture()

        # Simulate adding items
        for i in range(5):
            capture._q.put(b'x')
            qsize = capture._q.qsize()
            if qsize > capture._max_queue_depth:
                capture._max_queue_depth = qsize

        assert capture._max_queue_depth == 5

        # Add more
        for i in range(5):
            capture._q.put(b'x')
            qsize = capture._q.qsize()
            if qsize > capture._max_queue_depth:
                capture._max_queue_depth = qsize

        assert capture._max_queue_depth == 10

    def test_concurrent_reads_from_queue(self):
        """Should handle concurrent reads from queue."""
        capture = WinRTAudioCapture()
        capture._q.put(b'audio1')
        capture._q.put(b'audio2')
        capture._q.put(b'audio3')

        audio1 = capture.read(timeout=0.1)
        audio2 = capture.read(timeout=0.1)
        audio3 = capture.read(timeout=0.1)

        assert audio1 == b'audio1'
        assert audio2 == b'audio2'
        assert audio3 == b'audio3'

    def test_zero_timeout_read(self):
        """Should handle zero timeout (non-blocking read)."""
        capture = WinRTAudioCapture()

        # Empty queue, zero timeout should return immediately
        start = time.time()
        audio = capture.read(timeout=0.0)
        elapsed = time.time() - start

        assert audio is None
        assert elapsed < 0.1  # Should be nearly instant

    def test_large_timeout_read(self):
        """Should handle large timeout values."""
        capture = WinRTAudioCapture()
        capture._q.put(b'quick')

        # Large timeout, but should return immediately when data available
        start = time.time()
        audio = capture.read(timeout=100.0)
        elapsed = time.time() - start

        assert audio == b'quick'
        assert elapsed < 1.0  # Should return quickly, not wait for timeout


class TestCaptureResourceOwnership:
    """A thread that built nothing must not tear down what another built.

    Codex round 3 (wh-stt-load-metrics.2.1.4). Making each capture thread
    close its OWN objects introduced the mirror image of the bug it fixed:
    a thread whose _setup_graph raised holds (None, None, None), which is
    exactly the argument shape that means "close whatever the provider is
    holding" -- so a failed setup closed a LATER cycle's live graph.
    """

    def test_a_partial_setup_closes_the_graph_it_created(
            self, mock_winrt_available, mock_winrt_graph):
        """A graph created before a later setup call raises must not leak.

        _setup_graph makes the graph and then makes four more calls that can
        each fail. Every one of them leaves a started AudioGraph with nothing
        referring to it, holding the microphone open for the process's life.
        """
        mocks = mock_winrt_graph
        mocks['graph'].create_frame_output_node.side_effect = RuntimeError(
            "no output node")
        capture = WinRTAudioCapture()

        with pytest.raises(RuntimeError, match="no output node"):
            capture._setup_graph()

        assert mocks['graph'].close.call_count == 1
        assert capture._graph is None

    def test_a_setup_that_failed_does_not_close_a_later_cycles_graph(
            self, mock_winrt_available, _mock_winsdk_modules):
        """The mirror image of wh-stt-load-metrics.2.1.3, from the other side.

        Cycle 0's setup is still running when stop() and a second start()
        replace it. Its setup then raises, so it owns nothing, and the
        teardown it runs must stay a no-op instead of falling back to
        "close the provider's current graph".
        """
        blocked = threading.Event()
        release = _Handoff()

        def fail_late():
            blocked.set()
            release.wait_here(10.0)
            raise RuntimeError("microphone denied")

        live_node = Mock()
        live_node.get_frame = Mock(return_value=None)
        nodes = _CycleNodes(None, [live_node], gates={0: fail_late})
        capture = WinRTAudioCapture()
        nodes._capture = capture

        stale = None
        with patch.object(capture, '_setup_graph', side_effect=nodes), \
                patch('shared_audio.capture.winrt_capture.'
                      'elevate_current_thread', return_value=True):
            capture.start()
            try:
                assert blocked.wait(5.0) is True
                stale = capture._capture_thread
                capture.stop()
                capture.start()
                assert capture.wait_ready(timeout=5.0) is True
                live_graph = capture._graph
                assert live_graph is not None

                release.set()
                stale.join(timeout=5.0)
                assert stale.is_alive() is False

                assert release.timed_out is False, (
                    "the stale cycle's setup gave up before the test "
                    'released it, so it raised before the live cycle '
                    'existed and had no later graph to close')
                assert live_graph.close.call_count == 0
                assert live_graph.stop.call_count == 0
                assert capture._graph is live_graph
                assert capture.wait_ready(timeout=0.0) is True
            finally:
                release.set()
                if stale is not None:
                    stale.join(timeout=5.0)
                capture.stop()

    def test_a_graph_that_will_not_stop_is_still_closed(self):
        """stop() and close() are separate obligations.

        A graph whose stop() raises still holds the device until close()
        runs, and a single try block around both skips the one that
        actually releases it.
        """
        capture = WinRTAudioCapture()
        graph = Mock()
        graph.stop.side_effect = RuntimeError("graph already gone")

        capture._cleanup_graph(graph, Mock(), Mock())

        assert graph.close.call_count == 1


class TestPollLoopWriteAtomicity:
    """Every poll-loop decision and the writes it authorises are one step.

    Codex round 3 (wh-stt-load-metrics.2.1.5). The cycle token added in
    cac2df08 was read outside _lifecycle_lock at each of the poll loop's
    write sites, so a thread descheduled between the check and the write
    still wrote into a cycle that had since been stopped or replaced. The
    per-chunk guard also tested the cycle alone, which stop() never moves.
    """

    def test_a_poll_that_lands_after_stop_does_not_reach_the_queue(
            self, mock_winrt_available, _mock_winsdk_modules):
        """stop() does not change the cycle, so the cycle alone cannot guard.

        stop() clears the queue and joins for 2.0s. A poll blocked in
        get_frame past that join returns a full chunk into a provider that
        is meant to be silent, and read() then answers with audio captured
        before the stop.
        """
        blocked = threading.Event()
        release = _Handoff()
        capture = WinRTAudioCapture()
        float32_data = _float32_chunk(capture)
        calls = []
        lock = threading.Lock()

        def get_frame():
            with lock:
                first = not calls
                calls.append(1)
            if first:
                blocked.set()
                release.wait_here(10.0)
                return _frame_carrying(float32_data)
            return None

        with _winrt_polling_a_node(capture, get_frame):
            capture.start()
            try:
                assert blocked.wait(5.0) is True
                thread = capture._capture_thread
                capture.stop()
                frames_at_stop = capture._frames_captured

                release.set()
                thread.join(timeout=5.0)
                assert thread.is_alive() is False

                assert release.timed_out is False, (
                    'the blocked poll gave up before the test released '
                    'it, so its chunk arrived before the stop and never '
                    'landed on the far side of one')
                assert capture._q.qsize() == 0
                assert capture._frames_captured == frames_at_stop
            finally:
                release.set()

    def test_the_chunk_write_holds_the_lifecycle_lock(
            self, mock_winrt_available, _mock_winsdk_modules):
        """Proven by holding the lock and showing the write waits for it.

        Deleting the guard would leave the check passing and the write
        happening anyway; only the lock makes the pair one transition.

        The frame is released only after the test holds the lock, and the
        thread waits for it INSIDE get_frame, which takes no lock. An
        earlier version let the first poll return empty and took the lock
        afterwards; that parked the thread on the end-of-iteration liveness
        write instead, so the test passed even with the chunk write's own
        lock removed. The mutation gate reported it as a survivor.
        """
        entered = threading.Event()
        release_frame = threading.Event()
        capture = WinRTAudioCapture()
        watched = _WatchedLock(capture._lifecycle_lock)
        capture._lifecycle_lock = watched
        float32_data = _float32_chunk(capture)
        calls = []
        lock = threading.Lock()

        def get_frame():
            with lock:
                index = len(calls)
                calls.append(1)
            if index == 0:
                entered.set()
                release_frame.wait(10.0)
                return _frame_carrying(float32_data)
            return None

        with _winrt_polling_a_node(capture, get_frame):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True
                assert entered.wait(5.0) is True

                with watched:
                    release_frame.set()
                    assert watched.wait_for_a_blocked_thread(5.0) is True, (
                        'the poll thread never parked at the lifecycle lock '
                        'after the frame was released, so an empty queue '
                        'here would prove nothing')
                    assert capture._q.qsize() == 0

                assert _wait_until(
                    lambda: capture._q.qsize() > 0, 5.0) is True
            finally:
                release_frame.set()
                capture.stop()

    def test_the_live_liveness_write_holds_the_lifecycle_lock(
            self, mock_winrt_available, _mock_winsdk_modules):
        """A healthy poll may not resurrect a cycle that has been retired."""
        capture = WinRTAudioCapture()
        watched = _WatchedLock(capture._lifecycle_lock)
        capture._lifecycle_lock = watched

        with _winrt_polling_a_node(capture, Mock(return_value=None)):
            capture.start()
            try:
                assert capture.wait_ready(timeout=5.0) is True

                with watched:
                    capture._capture_alive = False
                    assert watched.wait_for_a_blocked_thread(5.0) is True, (
                        'the poll thread never parked at the lifecycle lock, '
                        'so an unchanged flag here would prove nothing')
                    assert capture._capture_alive is False

                assert _wait_until(
                    lambda: capture._capture_alive is True, 5.0) is True
            finally:
                capture.stop()

    def test_the_dead_liveness_write_holds_the_lifecycle_lock(
            self, mock_winrt_available, _mock_winsdk_modules):
        """The direction that matters most: False while audio is arriving.

        A stale thread declaring the provider dead blanks the diagnostic
        fields the whole load investigation reads, which is worse than the
        bug this bead started from.

        The test takes the lock only after the dead write has run once
        (wh-stt-load-metrics.5). It used to open on
        wait_ready(timeout=0.0) is False, which holds at once while setup
        is still running. On a loaded host the capture thread then parked
        at _publish_cycle, the watched lock counted it as blocked, and the
        test passed with the dead write's own lock removed; the mutation
        gate reported it as a survivor. After the publish, every poll
        raises, so the live write is never reached and the dead write is
        the only lifecycle lock left in that thread.
        """
        capture = WinRTAudioCapture()
        watched = _WatchedLock(capture._lifecycle_lock)
        capture._lifecycle_lock = watched

        with _winrt_polling_a_node(
                capture, Mock(side_effect=RuntimeError("device gone"))):
            capture.start()
            try:
                # The same opening as the live-write test: setup finished
                # and published. _setup_ok is written only by _publish_cycle,
                # so after this the thread cannot park there.
                assert capture._setup_done.wait(5.0) is True
                assert capture._setup_ok is True, (
                    'setup never published, so the thread could still park '
                    'at _publish_cycle instead of at the dead write')
                # _publish_cycle set the flag True and nothing in this test
                # has written it yet, so False now means the dead write ran.
                assert _wait_until(
                    lambda: capture._capture_alive is False, 5.0) is True, (
                    'the failing polls never declared the capture dead')
                assert capture._capture_thread.is_alive() is True

                with watched:
                    capture._capture_alive = True
                    assert watched.wait_for_a_blocked_thread(5.0) is True, (
                        'the poll thread never parked at the lifecycle lock, '
                        'so an unchanged flag here would prove nothing')
                    assert capture._capture_alive is True

                assert _wait_until(
                    lambda: capture._capture_alive is False, 5.0) is True
            finally:
                capture.stop()
