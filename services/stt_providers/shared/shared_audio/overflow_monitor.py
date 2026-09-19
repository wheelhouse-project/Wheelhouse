"""Audio overflow detection and restart REQUESTS

This module watches audio input overflow events and asks for a service
restart when it sees a persistent overflow pattern. Asking is all it does.
Whether a restart follows is the calling provider's decision, and neither
shipped provider restarts on an overflow: google_stt_server passes a
callback that only writes a log line, and the Parakeet provider passes no
callback at all (wh-audio-callback-log.2.4).

One capture backend reports here. Two did until the PortAudio path was
deleted (wh-portaudio-capture-removal), and they lost frames in different
places, so each named its own (see CAPTURE_QUEUE_SOURCE and
PORTAUDIO_SOURCE) and every line this module writes still says which one it
means.

Key Classes:
  - OverflowMonitor: Tracks overflow frequency and requests a restart when
    the threshold is crossed

Key Features:
  - Sliding window overflow tracking
  - Configurable thresholds to distinguish temporary vs persistent issues
  - Cooldown protection to prevent repeated requests
  - A request the caller is free to act on or ignore

Typical Usage:
  overflow_monitor = OverflowMonitor(config)

  # In audio callback when status indicates overflow:
  if overflow_monitor.report_overflow():
      # The monitor asked for a restart; this caller chooses to act
      restart_needed = True
"""
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)

#: Where the frames were lost, in words, for the line an operator reads.
#: The WinRT backend has no PortAudio in its capture path at all: its
#: capture thread drops a chunk when the queue it hands to the forwarder
#: is full. The sounddevice backend did have PortAudio, and PortAudio was
#: what reported the overflow there. A single monitor served both, so the
#: backend that builds it says which phrase is true of itself.
CAPTURE_QUEUE_SOURCE = "the queue between capture and the forwarder"
#: crewcut: no backend answers this any more. The one that did,
#: SounddeviceAudioCapture, went with the PortAudio capture path
#: (wh-portaudio-capture-removal); grep for "PORTAUDIO_SOURCE" under
#: services/stt_providers finds this definition, the module docstring
#: above, and tests/mutation_gate_overflow_wording.py, and no provider.
#: It is kept because it is the wrong answer the overflow-wording
#: mutations swap in to prove the WinRT line names the queue. Delete it,
#: and this comment, when a second backend either arrives or is ruled
#: out for good.
PORTAUDIO_SOURCE = "PortAudio"


@dataclass
class OverflowConfig:
    """Configuration for overflow detection and restart-request behavior."""
    # How many overflows in the window make the monitor request a restart
    overflow_threshold: int = 5
    # Time window in seconds to track overflows
    window_seconds: float = 30.0
    # Minimum time between restart requests (prevents request loops)
    restart_cooldown_seconds: float = 60.0
    # Maximum restart requests before the monitor stops asking
    max_restart_attempts: int = 3
    # Reset attempt counter after this many seconds of stable operation
    stable_reset_seconds: float = 300.0  # 5 minutes
    # Minimum seconds between INFO summary lines. Individual overflow events are
    # logged at DEBUG only; at INFO they flood the log (this is called several
    # times per second during a sustained overflow).
    log_summary_interval_seconds: float = 10.0
    # What overflowed, in words, for the summary line below. The default
    # names no mechanism because a monitor built without a backend cannot
    # know one; both shipped backends pass their own phrase.
    overflow_source: str = "the capture path"


class OverflowMonitor:
    """Monitors audio overflow events and requests a restart when needed.

    Requesting is the whole of what this class does. The request reaches
    the caller as the return value of report_overflow() and as the
    optional restart_callback; acting on it is the caller's decision
    (wh-audio-callback-log.2.4).
    """

    def __init__(self, config: OverflowConfig,
                 restart_callback: Optional[Callable] = None,
                 log_sink: Optional[Callable] = None):
        self.config = config
        self.restart_callback = restart_callback
        # crewcut: no caller passes a sink any more. The one that did,
        # MicrophoneStream on the PortAudio callback thread, was deleted
        # with the rest of that path (wh-portaudio-capture-removal). The
        # parameter is kept because the WinRT frame-poll loop is the kind
        # of caller it exists for, and tests/test_capture_log_sink.py
        # keeps four mutation-gate catchers alive on it. To remove it,
        # delete this parameter, the sink arm of _emit, that test file,
        # and the two entries named callback-log-monitor-ignores-its-sink
        # and callback-log-sink-given-the-wrong-logger in
        # tests/mutation_gate_capture_frame_loss.py.
        #
        # Where this monitor's log lines go. None writes them here and now,
        # which is right for a caller on an ordinary thread -- the WinRT
        # frame-poll loop, which already logs directly beside its own call.
        # A caller on a real-time thread passes a sink taking
        # (logger, level, message) and emits the line later from a thread
        # that may block (wh-audio-callback-log). See _emit.
        self._log_sink = log_sink

        # Overflow tracking
        self.overflow_times = deque()  # Store timestamps of overflow events

        # Restart management
        self.last_restart_time = 0.0
        self.restart_attempts = 0
        self.service_start_time = time.time()

        # State tracking
        self.restart_requested = False

        # Whether the one-time restart-cap WARNING has been written. Not
        # reset by reset_for_restart: that clears the overflow window but
        # leaves restart_attempts alone, so the cap stays reached
        # (wh-stt-load-metrics.3, criterion 5).
        self._cap_warning_logged = False

        # Monotonic timestamp of the last INFO summary line, for rate-limiting
        # (see log_summary_interval_seconds). None until the first summary, so
        # the first overflow always logs regardless of the monotonic clock's
        # arbitrary reference point (it can read < the interval just after boot).
        self._last_summary_time = None

    def _emit(self, level: int, message: str) -> None:
        """Write one line, or hand it to the sink that will write it later.

        Every logging call in this class goes through here, including the
        ones in _should_restart: report_overflow is called from a real-time
        audio callback, and _should_restart runs on that same thread
        (wh-audio-callback-log).

        The sink is given THIS module's logger, not the caller's. That is
        load-bearing rather than tidy: google_stt_server attaches its
        forwarding handler to "shared_audio.overflow_monitor" and nothing
        broader, on purpose, so a line re-emitted through the draining
        module's own logger would stop being forwarded on that provider
        while still reaching Parakeet's handler on the capture tree's root.
        """
        if self._log_sink is None:
            logger.log(level, message)
        else:
            self._log_sink(logger, level, message)

    def report_overflow(self, context: dict = None) -> bool:
        """
        Report an overflow event and decide whether to ask for a restart.

        Args:
            context: Optional dict with state at overflow time for diagnostics

        Returns:
            True if a restart was requested, False otherwise. A request
            is not a restart. What follows belongs to the provider that
            supplied restart_callback, and neither shipped provider
            restarts on an overflow: google_stt_server's callback writes
            one line and returns (main.py:1086-1099), and the Parakeet
            provider supplies no callback at all (main.py:178).
        """
        current_time = time.time()

        # Add this overflow to our tracking
        self.overflow_times.append(current_time)

        # Clean old overflow events outside our window
        window_start = current_time - self.config.window_seconds
        while self.overflow_times and self.overflow_times[0] < window_start:
            self.overflow_times.popleft()

        overflow_count = len(self.overflow_times)

        # Per-event detail at DEBUG only. At INFO this floods the log: a
        # sustained overflow calls this several times per second.
        self._emit(logging.DEBUG, f"[overflow] Detected overflow event ({overflow_count}/{self.config.overflow_threshold} in {self.config.window_seconds}s window)")
        if context:
            self._emit(logging.DEBUG, f"[overflow] Context: {context}")

        # Rate-limited INFO summary so an ongoing overflow stays visible (and can
        # be forwarded to wheelhouse.log) without one line per dropped frame.
        # Measure the interval on a monotonic clock: time.time() can jump
        # backward (an NTP correction, a manual clock change), which would
        # suppress the summary for an unbounded period while frames keep
        # dropping -- the exact signal this line exists to preserve.
        #
        # The line reports the count, the window and where the count came
        # from, and stops there (wh-stt-load-metrics.3, criterion 4). It used
        # to end "(audio consumer behind real time)", which named a different
        # measurement: the consumer falling behind fills the capture queue and
        # shows up as `drops`. On 2026-09-03 all 122 utterances had drops=0
        # while 13 carried overflows, so this line asserted the opposite of
        # what the numbers beside it said, and the investigation read it as
        # fact for two hours.
        #
        # "Where the count came from" said PortAudio whichever backend was
        # running. The WinRT backend has no PortAudio, so on the shipped
        # default the line named a component that was not in the process.
        # The backend supplies the phrase now.
        summary_now = time.monotonic()
        if (self._last_summary_time is None
                or summary_now - self._last_summary_time >= self.config.log_summary_interval_seconds):
            self._emit(
                logging.INFO,
                f"[overflow] {overflow_count} audio input overflows in the "
                f"last {self.config.window_seconds:.0f}s, measured at "
                f"{self.config.overflow_source}"
            )
            self._last_summary_time = summary_now

        # Check if we've exceeded threshold
        if overflow_count >= self.config.overflow_threshold:
            return self._should_restart(current_time)

        return False

    def _should_restart(self, current_time: float) -> bool:
        """Decide whether this overflow should ask for a restart.

        What this returns is a request, not an act -- see
        report_overflow for who receives it and what they do with it.
        """

        # Check cooldown period
        time_since_last_restart = current_time - self.last_restart_time
        if time_since_last_restart < self.config.restart_cooldown_seconds:
            # DEBUG: repeats on every overflow while inside the cooldown
            # window. It says what happened -- no request -- rather than
            # "Restart needed but ...", which stated a need this function
            # never establishes: it is deciding whether to ask, and what
            # follows an ask is the provider's business
            # (wh-stt-overflow-config-and-wording, criterion 4).
            self._emit(
                logging.DEBUG,
                f"[overflow] No restart requested: still inside the "
                f"{self.config.restart_cooldown_seconds}s cooldown "
                f"({time_since_last_restart:.1f}s since the last request)"
            )
            return False

        # Check if we've exceeded max attempts
        if self.restart_attempts >= self.config.max_restart_attempts:
            # One WARNING the first time the cap turns a request away
            # (wh-stt-load-metrics.3, criterion 5). Before this, the only
            # record of the cap was a DEBUG line, so a reader of the log
            # could not tell "the overflows stopped" from "the monitor gave
            # up and capture carried on losing frames". Verified in the
            # 09:17-09:20 log on 2026-09-03: after attempt 3/3 utterances
            # kept finalizing with 3-22 overflows per 30 s, because reaching
            # the cap stops requests and closes nothing.
            #
            # It fires once for the life of the monitor. Reaching the cap is
            # terminal here: this check sits above the stable_reset_seconds
            # branch below, so once restart_attempts reaches the maximum the
            # function always returns here and the counter is never reset.
            if not self._cap_warning_logged:
                self._cap_warning_logged = True
                self._emit(
                    logging.WARNING,
                    f"[overflow] Restart attempt limit reached "
                    f"({self.restart_attempts}/"
                    f"{self.config.max_restart_attempts}); no further "
                    f"restarts will be requested and the capture stream is "
                    f"left open, so capture continues degraded"
                )
            # DEBUG: repeats on every overflow once the attempt cap is
            # reached, under the one-time WARNING above. Same wording rule
            # as the cooldown branch.
            self._emit(
                logging.DEBUG,
                f"[overflow] No restart requested: the attempt limit is "
                f"reached ({self.restart_attempts}/"
                f"{self.config.max_restart_attempts})"
            )
            return False

        # Check if enough time has passed since service start to reset attempt counter
        time_since_start = current_time - self.service_start_time
        if time_since_start > self.config.stable_reset_seconds:
            self._emit(logging.INFO, f"[overflow] Resetting restart attempt counter after {time_since_start:.1f}s of stable operation")
            self.restart_attempts = 0
            self.service_start_time = current_time

        # All checks passed - request a restart
        self.last_restart_time = current_time
        self.restart_attempts += 1
        self.restart_requested = True

        # States the measurement and the request, never a restart as
        # something that happened (boss ruling on wh-stt-load-metrics.3,
        # 2026-09-03). Whether a restart follows depends on the provider,
        # and today neither shipped provider restarts on an overflow:
        # google_stt_server passes on_overflow_detected, which writes one
        # log line and returns (main.py:1086-1099, under the banner
        # "auto-restart disabled" at :1276), and the Parakeet provider
        # passes no callback at all. Its restart_requested_event is set
        # only by the add-hint and restart-service WebSocket handlers, so
        # every restart that happens was asked for by a user. An earlier
        # version of this comment said google reopens its stream here;
        # it does not (wh-audio-callback-log.2.4).
        self._emit(
            logging.INFO,
            f"[overflow] Overflow threshold crossed; restart requested "
            f"(attempt {self.restart_attempts}/"
            f"{self.config.max_restart_attempts})")

        # Call restart callback if provided
        #
        # crewcut: this runs on whatever thread called report_overflow,
        # and a provider is free to log inside it -- google_stt_server's
        # on_overflow_detected (main.py) is one logger.info call. The
        # thread that made this a real-time hazard was MicrophoneStream's
        # PortAudio callback, deleted by wh-portaudio-capture-removal;
        # WinRTAudioCapture reports from its own capture thread, which no
        # audio driver is waiting on. The limit is therefore smaller than
        # it was, not gone. Deferring the call itself would change
        # when a restart is requested, which is a different question from
        # where a line is written, so it was left alone here
        # (wh-audio-callback-log). Removing the limit means making the
        # provider's callback publish state instead of logging, in the
        # provider.
        if self.restart_callback:
            self.restart_callback()

        return True

    def reset_for_restart(self):
        """Reset state after restart has been completed."""
        self.overflow_times.clear()
        self.restart_requested = False
        # Reset the summary rate-limit gate too. A restart starts the overflow
        # tracking fresh, so the "first overflow always logs" property (see the
        # None init in __init__) must hold again: without this, a restart that
        # happens within one summary interval of the last summary would suppress
        # the first post-restart summary for the rest of the interval -- exactly
        # when the operator needs to know whether the restart resolved the drops.
        self._last_summary_time = None
        # Written here and now, not through _emit. This is a control-path
        # method: its callers own the stream's lifecycle, and no audio
        # callback reaches it, so there is no real-time thread to protect
        # from the write (wh-audio-callback-log). The caller this named,
        # MicrophoneStream.reset_overflow_monitor, went with the PortAudio
        # capture path (wh-portaudio-capture-removal).
        logger.info("[overflow] Monitor state reset after restart")

    def get_status(self) -> dict:
        """Get current overflow monitoring status for debugging."""
        current_time = time.time()
        return {
            'overflow_count_current_window': len(self.overflow_times),
            'threshold': self.config.overflow_threshold,
            'window_seconds': self.config.window_seconds,
            'restart_attempts': self.restart_attempts,
            'max_attempts': self.config.max_restart_attempts,
            'time_since_last_restart': current_time - self.last_restart_time,
            'cooldown_seconds': self.config.restart_cooldown_seconds,
            'restart_requested': self.restart_requested
        }
