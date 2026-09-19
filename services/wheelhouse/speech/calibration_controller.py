"""Session state machine for the guided voice calibration (wh-7ou.7.2.2).

Spec: docs/superpowers/specs/2026-08-07-voice-calibration-design.md,
Sections 6.1 (state machine), 6.3 (sample validation), 8 (failure
handling). The threshold arithmetic itself lives in
``speech.calibration_math``; this module owns the session:

    idle -> intro -> word_stage(word i, sample j) -> noise_stage
         -> review -> applying -> done
    any stage -> cancelled (Cancel/close) | failed (error)

The controller is deliberately synchronous and I/O-free. Every side
effect goes through an injected callable so the state machine is fully
unit-testable with fakes:

- ``send_to_gui(message)`` puts a ``{"action": "cal_state", ...}`` dict
  on the Logic-to-GUI state queue (message contract part B).
- ``provider`` carries the two provider-bound commands as methods:
  ``set_calibration_mode(enabled)`` and
  ``apply_engine_settings(settings)`` (message contract part A).
- ``schedule(delay_s, callback)`` arms a one-shot timer and returns a
  handle with ``.cancel()`` (loop.call_later in production).

Inbound events arrive as method calls made by the owning process:
``handle_gui_action`` for the five cal_* actions from the GUI queue,
``handle_final`` for final transcripts (with the contract's confidence
block) handed over by the WebSocket manager's typing gate,
``on_engine_settings_result`` for the provider's save answer, and
``on_provider_connected`` for the provider's (re)connect.

The typing gate (spec Section 6.2, integrations/websocket_manager.py)
holds for the WHOLE session: while ``session_active`` is True the
manager drops provisional text (stables) outright and routes every
final through ``handle_final``, which consumes it -- as a measurement
in the word/noise stages, swallowed elsewhere -- unless it is a
voice-click utterance ("click <target>"), the one allowlisted channel
that flows normally on every screen so all of the window's buttons
stay hands-free (spec Section 3.7; review finding wh-7ou.7.6.3).

Calibration mode is switched off on EVERY exit path: at review entry
(captures are over), and unconditionally again at session end --
cancel, inactivity, failure, and the applied path alike. The provider's
own disconnect/timeout reset (spec Section 5.2) remains the crash
backstop; this controller never relies on it.
"""

import asyncio
import logging
from typing import Any, Callable, Optional, Tuple

from speech.calibration_math import (
    CalibrationProposal,
    WordSample,
    compute_thresholds,
)

logger = logging.getLogger(__name__)

# Spec Section 3.3: the shipped v1 word list, five samples each.
CAL_WORDS = ("comma", "period", "delete", "select")
SAMPLES_PER_WORD = 5
# Spec Section 3.4: three separate coughs.
NOISE_SAMPLE_TOTAL = 3
# Spec Section 6.3: three consecutive non-matching captures move on.
WORD_RETRY_CAP = 3
# Spec Section 6.3: twenty minutes with no captures cancels cleanly.
INACTIVITY_TIMEOUT_S = 20 * 60.0
# The provider's calibration-mode bypass auto-disables 600 seconds after
# the last enable (whisper_engine._CALIBRATION_MODE_TIMEOUT_S) as a
# crash backstop. The session allows 20 minutes of inactivity and
# promises no time limit, so the controller re-sends the enable on a
# half-timeout cadence while a capture stage is live -- the provider
# window can then never lapse mid-session, and the backstop still fires
# within 10 minutes if this controller dies (review finding
# wh-7ou.7.6.4).
CAL_MODE_REFRESH_S = 300.0
# Spec Section 3.6: show the taking-longer text instead of waiting
# forever for the provider's post-apply restart.
RESTART_SLOW_TIMEOUT_S = 30.0

# Shipped defaults for the two [engine] settings; must match
# services/stt_providers/distil_medium_en/config.toml and the
# whisper_engine constructor defaults.
_DEFAULT_MIN_PROBABILITY = 0.6
_DEFAULT_MAX_NO_SPEECH_PROB = 0.03

# Internal states. The GUI screen names overlap but are not identical:
# _AWAITING_RESTART still shows the "applying" screen.
_IDLE = "idle"
_WRONG_PROVIDER = "wrong_provider"
_INTRO = "intro"
_WORD = "word"
_NOISE = "noise"
_REVIEW = "review"
_APPLYING = "applying"
_AWAITING_RESTART = "awaiting_restart"
_SAVE_FAILED = "save_failed"


def _normalize_word(text: Any) -> str:
    """Lowercase and strip punctuation, per spec Section 6.3."""
    if not isinstance(text, str):
        return ""
    kept = "".join(
        ch for ch in text.lower() if ch.isalnum() or ch.isspace()
    )
    return " ".join(kept.split())


def _is_valid_probability(value: Any) -> bool:
    """A usable measurement: a real number in [0, 1] (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0.0 <= value <= 1.0


def read_current_thresholds(config_path) -> Tuple[float, float]:
    """Read the two rescue thresholds from a provider's config.toml.

    Wiring helper for main.py: the review stage needs the CURRENT
    values (spec Section 7 inputs). Falls back per-value to the shipped
    defaults on a missing file, unparseable TOML, or an out-of-range
    value -- a broken config must degrade the details view, never break
    the session.
    """
    values = {}
    if config_path is not None:
        try:
            import tomllib
            from pathlib import Path

            data = tomllib.loads(
                Path(config_path).read_text(encoding="utf-8")
            )
            engine = data.get("engine", {})
            if isinstance(engine, dict):
                values = engine
        except Exception as exc:  # noqa: BLE001 -- degrade to defaults
            logger.warning(
                "Could not read provider thresholds from %s: %s",
                config_path, exc,
            )
    min_prob = values.get("single_word_min_probability")
    max_ns = values.get("single_word_max_no_speech_prob")
    return (
        float(min_prob) if _is_valid_probability(min_prob)
        else _DEFAULT_MIN_PROBABILITY,
        float(max_ns) if _is_valid_probability(max_ns)
        else _DEFAULT_MAX_NO_SPEECH_PROB,
    )


def _asyncio_schedule(delay_s: float, callback: Callable[[], None]):
    """Default scheduler: loop.call_later on the running loop.

    Returns None when no loop is running (bare unit-test contexts); the
    controller treats a None handle as an unarmed timer.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.call_later(delay_s, callback)


class CalibrationController:
    """Owns one voice-calibration session end to end."""

    def __init__(
        self,
        send_to_gui: Callable[[dict], None],
        provider: Any,
        is_distil_provider: Callable[[], bool],
        get_push_to_talk: Callable[[], bool],
        get_current_thresholds: Callable[[], Tuple[float, float]],
        schedule: Optional[Callable[..., Any]] = None,
    ):
        self._send_to_gui = send_to_gui
        self._provider = provider
        self._is_distil_provider = is_distil_provider
        self._get_push_to_talk = get_push_to_talk
        self._get_current_thresholds = get_current_thresholds
        self._schedule = schedule if schedule is not None else _asyncio_schedule

        self._state = _IDLE
        self._last_state: Optional[dict] = None
        self._inactivity_handle: Any = None
        self._restart_handle: Any = None
        self._refresh_handle: Any = None
        self._reset_session_data()

    # ------------------------------------------------------------------ #
    #  Public surface
    # ------------------------------------------------------------------ #

    @property
    def session_active(self) -> bool:
        """True from intro through the apply wait; False when idle or on
        the wrong-provider notice (which is inert, not a session)."""
        return self._state not in (_IDLE, _WRONG_PROVIDER)

    @property
    def capturing(self) -> bool:
        """True while spoken words are measurements (word/noise stages).

        The typing gate itself keys on ``session_active`` (the whole
        session, wh-7ou.7.6.3); this narrower flag drives the
        calibration-mode refresh cadence and stays available for
        diagnostics.
        """
        return self._state in (_WORD, _NOISE)

    def handle_gui_action(self, action: str) -> None:
        """Dispatch one of the five cal_* actions from the GUI queue."""
        try:
            if action == "cal_session_open":
                self._on_session_open()
            elif action == "cal_cancel":
                self._on_cancel()
            elif action == "cal_start":
                if self._state == _INTRO:
                    self._enter_word_stage()
            elif action == "cal_skip_noise":
                if self._state == _NOISE:
                    # An explicit user choice, and the only path to the
                    # skip formula: a completed noise stage with unusable
                    # data keeps the current ceiling instead (review
                    # finding wh-7ou.7.6.5).
                    self._noise_skipped = True
                    self._enter_review()
            elif action == "cal_apply":
                if self._state in (_REVIEW, _SAVE_FAILED):
                    self._start_apply()
            else:
                logger.warning("Unknown calibration action: %s", action)
                return
            if self.session_active:
                self._arm_inactivity()
        except Exception:
            logger.exception("Calibration action %r failed", action)
            self._fail()

    def handle_final(self, text: str, confidence: Optional[dict]) -> bool:
        """Decide what one final transcript does during the session.

        Returns True when the final was consumed -- as a measurement in
        the word/noise stages, or swallowed on any other active screen
        so nothing types and no command fires (spec Sections 3.7, 6.2;
        review finding wh-7ou.7.6.3). Returns False so the caller lets
        the transcript flow normally in two cases: no session is active,
        or the utterance is a voice-click command ("click <target>") --
        the one allowlisted channel, kept open on EVERY screen so all of
        the window's buttons stay hands-free, including the
        capture-stage Cancel and Skip buttons.
        """
        try:
            if not self.session_active:
                return False
            if self._is_click_utterance(text):
                return False
            if self._state == _WORD:
                self._arm_inactivity()
                self._on_word_final(text, confidence)
                return True
            if self._state == _NOISE:
                self._arm_inactivity()
                self._on_noise_final(confidence)
                return True
            # Intro, review, applying, awaiting-restart, save-failed:
            # swallow silently. The session promises measurement-only
            # speech from start to finish.
            return True
        except Exception:
            logger.exception("Calibration transcript handling failed")
            self._fail()
            return True

    @staticmethod
    def _is_click_utterance(text: Any) -> bool:
        """A voice-click command: "click <target>" (spec Section 3.7).

        The bare word "click" is not a complete command and letting it
        flow would type it into a focused editor, so the allowlist is
        the two-word-or-more prefix form only. The calibration words
        (comma, period, delete, select) can never match, and a noise
        capture decoding to exactly this shape is vanishingly unlikely.
        """
        return _normalize_word(text).startswith("click ")

    def on_engine_settings_result(
        self, ok: bool, error: Optional[str],
    ) -> None:
        """The provider's answer to apply_engine_settings."""
        try:
            if self._state != _APPLYING:
                return
            if ok:
                if self._provider_connected_while_applying:
                    # The post-apply restart reconnected BEFORE this
                    # result frame was handled (the result rides the old
                    # socket's queue; wh-7ou.7.6.6 round 4). No further
                    # connect is coming, so waiting in the restart state
                    # would strand the session until the inactivity
                    # timeout with the typing gate held. Complete now.
                    self._send_state(screen="applied")
                    self._end_session()
                    return
                # Settings written; the provider restarts itself next.
                # The restart-slow timer armed at apply time keeps
                # running until the reconnect.
                self._state = _AWAITING_RESTART
                return
            self._cancel_restart_timer()
            self._state = _SAVE_FAILED
            self._send_state(
                screen="save_failed",
                review_kind=self._review_kind,
                details=self._details,
                error_detail=error,
            )
        except Exception:
            logger.exception("Calibration settings result handling failed")
            self._fail()

    def on_provider_connected(self) -> None:
        """The provider process (re)connected to the WebSocket server."""
        try:
            if not self.session_active:
                return
            if not self._is_distil_provider():
                # A mid-session provider switch: whatever just connected
                # is not the whisper provider this session is measuring,
                # so it must neither complete the post-apply wait nor
                # receive a calibration-mode enable (wh-7ou.7.6.6). End
                # the session now, the same way the inactivity timeout
                # does -- silently ignoring the connect left the session
                # measuring the replacement provider's finals and held
                # the typing gate until the inactivity timeout (round-3
                # rebuttal). The mode-off _end_session sends is harmless
                # to a non-whisper provider.
                logger.info(
                    "Calibration session ended: provider switched away "
                    "from Distil-Whisper mid-session"
                )
                self._send_state(screen="cancelled")
                self._end_session()
                return
            if self._state == _APPLYING:
                # The provider connected while the apply result is
                # still in flight: either the restart raced ahead of
                # the result frame, or an orphan was promoted over the
                # applying stream (wh-7ou.7.6.6 round 4). Remember it;
                # an ok result now completes the session immediately
                # instead of waiting in the restart state for a connect
                # that already happened.
                self._provider_connected_while_applying = True
                return
            if self._state in (_WORD, _NOISE):
                # A mid-session provider restart reset calibration mode
                # on the provider side (spec Section 5.2); re-enable it
                # so the remaining captures still arrive unsuppressed.
                # The re-enable restarts the provider's 600s window, so
                # the refresh cadence restarts with it.
                self._provider.set_calibration_mode(True)
                self._arm_mode_refresh()
                return
            if self._state == _AWAITING_RESTART:
                # The post-apply restart completed: the new values are
                # loaded. Show the all-set screen and end the session.
                self._send_state(screen="applied")
                self._end_session()
        except Exception:
            logger.exception("Calibration reconnect handling failed")
            self._fail()

    # ------------------------------------------------------------------ #
    #  GUI actions
    # ------------------------------------------------------------------ #

    def _on_session_open(self) -> None:
        if self._state in (_IDLE, _WRONG_PROVIDER, _INTRO):
            # Fresh (or not-yet-started) session: re-evaluate the
            # provider so a window reopened after a provider switch
            # shows the right first screen.
            if not self._is_distil_provider():
                self._state = _WRONG_PROVIDER
                self._send_state(screen="wrong_provider")
                return
            self._state = _INTRO
            self._send_state(
                screen="intro",
                push_to_talk=bool(self._get_push_to_talk()),
            )
            return
        # Mid-session reopen: answer with the current screen.
        if self._last_state is not None:
            self._send_state(**self._last_state)

    def _on_cancel(self) -> None:
        if self._state == _IDLE:
            return
        if self._state == _WRONG_PROVIDER:
            # The notice is inert: no session, nothing to undo, and no
            # calibration command was ever sent to whatever non-Distil
            # provider is connected.
            self._state = _IDLE
            self._last_state = None
            return
        self._end_session()

    def _enter_word_stage(self) -> None:
        self._reset_session_data()
        self._provider.set_calibration_mode(True)
        self._arm_mode_refresh()
        self._state = _WORD
        self._send_word_state(feedback=None)

    def _start_apply(self) -> None:
        proposal = self._proposal
        if proposal is None:
            return
        settings = {}
        if not proposal.min_probability.keep:
            settings["single_word_min_probability"] = (
                proposal.min_probability.proposed
            )
        if not proposal.max_no_speech_prob.keep:
            settings["single_word_max_no_speech_prob"] = (
                proposal.max_no_speech_prob.proposed
            )
        if not settings:
            # Nothing learned: the finish screen's only button is OK,
            # which the GUI sends as cal_cancel. A stray cal_apply must
            # not fabricate an empty write.
            return
        self._state = _APPLYING
        # Each apply starts its own restart wait: a connect remembered
        # during an earlier (failed) apply must not complete this one.
        self._provider_connected_while_applying = False
        self._send_state(screen="applying")
        self._provider.apply_engine_settings(settings)
        self._arm_restart_timer()

    # ------------------------------------------------------------------ #
    #  Captures
    # ------------------------------------------------------------------ #

    def _on_word_final(self, text: str, confidence: Optional[dict]) -> None:
        if self._is_word_match(text, confidence):
            self._word_samples.append(WordSample(
                min_word_probability=confidence.get("min_word_probability"),
                max_no_speech_prob=confidence.get("max_no_speech_prob"),
            ))
            self._samples_for_word += 1
            self._consecutive_misses = 0
            if self._samples_for_word >= SAMPLES_PER_WORD:
                self._advance_word(feedback="got_it")
            else:
                self._send_word_state(feedback="got_it")
            return
        self._consecutive_misses += 1
        if self._consecutive_misses >= WORD_RETRY_CAP:
            # Spec Section 6.3: move on rather than looping forever. The
            # samples already accepted for this word stay in the pool.
            self._advance_word(feedback=None)
            return
        self._send_word_state(feedback="try_again")

    def _is_word_match(self, text: str, confidence: Optional[dict]) -> bool:
        if not isinstance(confidence, dict):
            return False
        word_count = confidence.get("word_count")
        if isinstance(word_count, bool) or word_count != 1:
            return False
        return _normalize_word(text) == CAL_WORDS[self._word_index]

    def _advance_word(self, feedback: Optional[str]) -> None:
        self._word_index += 1
        self._samples_for_word = 0
        self._consecutive_misses = 0
        if self._word_index >= len(CAL_WORDS):
            self._state = _NOISE
            # The last word's feedback stays on the word stage: the
            # noise screen's "Got it!" appears per cough (spec Section
            # 3.4), so the stage opens with none showing.
            self._send_noise_state(feedback=None)
        else:
            self._send_word_state(feedback=feedback)

    def _on_noise_final(self, confidence: Optional[dict]) -> None:
        # Any final heard during the noise stage counts (spec Section
        # 6.3); its highest no-speech probability is the datum. A
        # missing block still advances the dot -- the cough was heard --
        # and the unusable datum is dropped by the arithmetic later.
        datum = (
            confidence.get("max_no_speech_prob")
            if isinstance(confidence, dict) else None
        )
        self._noise_samples.append(datum)
        if len(self._noise_samples) >= NOISE_SAMPLE_TOTAL:
            self._enter_review()
        else:
            self._send_noise_state(feedback="got_it")

    # ------------------------------------------------------------------ #
    #  Review / apply plumbing
    # ------------------------------------------------------------------ #

    def _enter_review(self) -> None:
        # Captures are over: the filter must come back on now, not at
        # session end -- the user may sit on the review screen. The
        # refresh stops with it so no later re-enable can undo the off.
        self._cancel_mode_refresh()
        self._provider.set_calibration_mode(False)
        current_min, current_ceiling = self._get_current_thresholds()
        proposal = compute_thresholds(
            self._word_samples,
            self._noise_samples,
            current_min,
            current_ceiling,
            noise_skipped=self._noise_skipped,
        )
        self._proposal = proposal
        self._review_kind = self._kind_for(proposal)
        self._details = self._details_for(
            proposal, current_min, current_ceiling,
        )
        self._state = _REVIEW
        self._send_state(
            screen="review",
            review_kind=self._review_kind,
            details=self._details,
        )

    @staticmethod
    def _kind_for(proposal: CalibrationProposal) -> str:
        written = (
            (not proposal.min_probability.keep)
            + (not proposal.max_no_speech_prob.keep)
        )
        return {2: "full", 1: "partial", 0: "nothing"}[written]

    @staticmethod
    def _details_for(
        proposal: CalibrationProposal,
        current_min: float,
        current_ceiling: float,
    ) -> dict:
        proposed = {}
        if not proposal.min_probability.keep:
            proposed["single_word_min_probability"] = (
                proposal.min_probability.proposed
            )
        if not proposal.max_no_speech_prob.keep:
            proposed["single_word_max_no_speech_prob"] = (
                proposal.max_no_speech_prob.proposed
            )
        measured = {
            name: value
            for name, value in vars(proposal.measured).items()
            if value is not None
        }
        return {
            "current": {
                "single_word_min_probability": current_min,
                "single_word_max_no_speech_prob": current_ceiling,
            },
            "proposed": proposed,
            "measured": measured,
        }

    # ------------------------------------------------------------------ #
    #  State messages
    # ------------------------------------------------------------------ #

    def _send_state(self, **state) -> None:
        self._last_state = state
        self._send_to_gui({"action": "cal_state", "state": state})

    def _send_word_state(self, feedback: Optional[str]) -> None:
        self._send_state(
            screen="word",
            word=CAL_WORDS[self._word_index],
            word_index=self._word_index + 1,
            word_total=len(CAL_WORDS),
            sample_count=self._samples_for_word,
            sample_total=SAMPLES_PER_WORD,
            feedback=feedback,
        )

    def _send_noise_state(self, feedback: Optional[str]) -> None:
        self._send_state(
            screen="noise",
            noise_count=len(self._noise_samples),
            noise_total=NOISE_SAMPLE_TOTAL,
            feedback=feedback,
        )

    # ------------------------------------------------------------------ #
    #  Timers
    # ------------------------------------------------------------------ #

    def _arm_inactivity(self) -> None:
        self._cancel_inactivity_timer()
        self._inactivity_handle = self._schedule(
            INACTIVITY_TIMEOUT_S, self._on_inactivity_timeout,
        )

    def _on_inactivity_timeout(self) -> None:
        # Twenty minutes with nothing heard and nothing clicked: the
        # user walked away. Cancel cleanly -- nothing further written,
        # calibration mode off -- and tell the window, because ending
        # the session releases the typing gate: a screen frozen on a
        # capture stage would invite words that now type into whatever
        # has focus (review finding wh-7ou.7.6.1).
        self._inactivity_handle = None
        if not self.session_active:
            return
        logger.info("Calibration session cancelled after inactivity")
        self._send_state(screen="cancelled")
        self._end_session()

    def _arm_mode_refresh(self) -> None:
        self._cancel_mode_refresh()
        self._refresh_handle = self._schedule(
            CAL_MODE_REFRESH_S, self._on_mode_refresh,
        )

    def _on_mode_refresh(self) -> None:
        self._refresh_handle = None
        if not self.capturing:
            return
        try:
            self._provider.set_calibration_mode(True)
            self._arm_mode_refresh()
        except Exception:
            logger.exception("Calibration mode refresh failed")
            self._fail()

    def _cancel_mode_refresh(self) -> None:
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()
            self._refresh_handle = None

    def _arm_restart_timer(self) -> None:
        self._cancel_restart_timer()
        self._restart_handle = self._schedule(
            RESTART_SLOW_TIMEOUT_S, self._on_restart_slow,
        )

    def _on_restart_slow(self) -> None:
        self._restart_handle = None
        if self._state not in (_APPLYING, _AWAITING_RESTART):
            return
        # Keep waiting -- the launcher's crash recovery will respawn the
        # provider -- but stop implying it is imminent (spec Section 3.6).
        self._send_state(screen="restart_slow")

    def _cancel_inactivity_timer(self) -> None:
        if self._inactivity_handle is not None:
            self._inactivity_handle.cancel()
            self._inactivity_handle = None

    def _cancel_restart_timer(self) -> None:
        if self._restart_handle is not None:
            self._restart_handle.cancel()
            self._restart_handle = None

    # ------------------------------------------------------------------ #
    #  Session end
    # ------------------------------------------------------------------ #

    def _reset_session_data(self) -> None:
        self._word_index = 0
        self._samples_for_word = 0
        self._consecutive_misses = 0
        self._word_samples: list = []
        self._noise_samples: list = []
        self._noise_skipped = False
        self._proposal: Optional[CalibrationProposal] = None
        self._review_kind: Optional[str] = None
        self._details: Optional[dict] = None
        self._provider_connected_while_applying = False

    def _end_session(self) -> None:
        """Every exit path funnels here: cancel, inactivity, failure,
        and the applied path alike. Calibration mode goes off
        unconditionally -- redundantly with the review-entry off and
        the provider's own disconnect reset, on purpose."""
        self._cancel_inactivity_timer()
        self._cancel_restart_timer()
        self._cancel_mode_refresh()
        try:
            self._provider.set_calibration_mode(False)
        except Exception:  # noqa: BLE001 -- ending must never fail
            logger.exception(
                "Could not send calibration-mode off at session end"
            )
        self._state = _IDLE
        self._last_state = None
        self._reset_session_data()

    def _fail(self) -> None:
        try:
            if self._state == _WRONG_PROVIDER:
                self._state = _IDLE
                self._last_state = None
            elif self._state != _IDLE:
                # Same reason as the inactivity timeout: ending the
                # session releases the typing gate, so the window must
                # not stay frozen on a capture screen.
                self._send_state(screen="cancelled")
                self._end_session()
        except Exception:  # noqa: BLE001 -- last-resort recovery
            logger.exception("Calibration session failure cleanup failed")
            self._state = _IDLE
