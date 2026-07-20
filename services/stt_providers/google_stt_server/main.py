"""Google Speech-to-Text Server with Overlay Mode Processing

ARCHITECTURE:
  Audio → VAD → Google STT → Stability Filter → Overlay Messages → WheelHouse

CORE COMPONENTS:
  • StabilityProcessor: Sends full stable text and final transcripts
  • UtteranceManager: Handles speech boundaries and finalization triggers  
  • Main Loop: Orchestrates audio flow and response processing

PROCESSING FLOW:
  1. VAD detects speech → start new utterance
  2. Stream audio to Google STT API
  3. Filter responses by stability threshold (0.9)
  4. Send "stable" messages with full stable text via WebSocket
  5. Send "final" message when utterance completes
  6. Reset state for next utterance

KEY BEHAVIORS:
  • Sends full stable text (not incremental deltas)
  • WheelHouse extracts word deltas from stable messages
  • Hybrid finalization: Google final, EOS fallback, or silence timeout
  • Treats Google finals as maximally stable

CONFIGURATION:
  • stability_commit_threshold: 0.9 (confidence required)

RESULT: Sub-400ms latency with high accuracy, simplified message protocol.

Typical Usage:
  python main.py --config config.toml --device-index 1
"""
import logging
import os
import signal
import sys
import time
import threading
import queue
from collections import deque
from enum import Enum, auto

# Allow running this file directly by ensuring the repository root is on sys.path
try:
    from pathlib import Path
    _this_file = Path(__file__).resolve()
    for parent in [_this_file.parent, *_this_file.parents]:
        if (parent / 'pyproject.toml').exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            break
except Exception:
    pass

from google.cloud.speech_v1.types import StreamingRecognizeResponse

from config_loader import load_config
from shared_stt.redact import redact_transcript

# Add stt_providers/ to sys.path for cross-provider imports
sys.path.append(str(Path(__file__).parent.parent))
# Add services/ to sys.path for version_info
sys.path.append(str(Path(__file__).parent.parent.parent))
from version_info import get_startup_banner
import collections

# Import from shared libraries
from shared_audio.diagnostics import run_mic_check, LoopStallTracker
from shared_audio.capture import get_audio_provider, get_available_providers, AudioConfig
from shared_audio.silero_vad import SileroVAD
from shared_audio.agc import SmartAGC, AGCConfig
from shared_audio.thread_priority import elevate_current_thread
from shared_stt.ws_forwarder import WSForwarder, WebSocketLogHandler

from usage_metrics import UsageMetrics

# Match the faster_whisper_cpu reference pattern: module-level basicConfig for
# stdout + a named logger the WebSocketLogHandler attaches to post-connect.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("GoogleSTT")


class UtteranceState(Enum):
    """Defines the states of the utterance lifecycle."""
    IDLE = auto()      # Waiting for the first sign of a new utterance.
    ACTIVE = auto()    # Actively receiving partials for the current utterance.
    FINALIZED = auto() # A final transcript has been sent; waiting for a new utterance to start.


class StabilityProcessor:
    """Handles stable segment extraction and overlay mode transmission."""

    def __init__(self, config, forwarder=None):
        self.stability_threshold = config.latency.stability_commit_threshold
        self.forwarder = forwarder
        self.config = config
        self.last_google_response_time = None  # For silence timeout calculation

        # Track last sent stable text to avoid duplicate sends
        self._last_stable_sent = ""

        # Whether to send interim (stable) results or only final results
        self.send_interim_results = True
        
    def reset_for_new_utterance(self):
        """Reset state for a new utterance."""
        self._last_stable_sent = ""
        self.last_google_response_time = None
        
    def process_response(self, response, utterance_id, is_final=False):
        """Process Google STT response using overlay mode."""
        # Update last Google response timestamp for silence timeout
        self.last_google_response_time = time.time()
        
        if is_final:
            # Send final transcript - this IS the utterance end
            final_text = self._extract_full_text(response)
            if final_text.strip():
                # final_reason="GOOGLE_FINAL" is hard-coded here because reaching
                # the is_final=True branch is the Google-final signal itself.
                # _finalize_utterance("GOOGLE_FINAL") runs in UtteranceManager
                # AFTER this send, so the reason cannot be passed through that
                # path. WheelHouse's three-mode retraction policy (wh-x4fwo)
                # uses this reason to detect ambiguous cases.
                self.forwarder.send_final(
                    final_text.strip(),
                    utterance_id,
                    trace_id=self.forwarder._current_trace_id,
                    final_reason="GOOGLE_FINAL",
                )
            return final_text

        # Extract stable portion
        stable_text = self._extract_stable_segments(response)

        # Send if we have stable text, it's different from last send, and interim results are enabled
        if stable_text.strip() and stable_text != self._last_stable_sent:
            self._last_stable_sent = stable_text
            if self.send_interim_results:
                self.forwarder.send_stable(stable_text.strip(), utterance_id, trace_id=self.forwarder._current_trace_id)

        return stable_text
        
    def _extract_stable_segments(self, response):
        """Extract text from segments above stability threshold."""
        stable_text = ""
        for result in response.results:
            if result.alternatives and hasattr(result, 'stability'):
                transcript = result.alternatives[0].transcript
                stability = result.stability
                
                if stability >= self.stability_threshold:
                    stable_text += transcript
                else:
                    break
        return stable_text
        
    def _extract_full_text(self, response):
        """Extract complete text from response (for finals)."""
        full_parts = []
        for result in response.results:
            if result.alternatives:
                full_parts.append(result.alternatives[0].transcript)
        return "".join(full_parts)


class UtteranceManager:
    """Manages utterance state transitions and finalization triggers."""
    
    def __init__(self, config, stability_processor, forwarder=None, usage_metrics=None):
        self.state = UtteranceState.IDLE
        self.current_utterance_id = 0
        self.last_speech_ts = None
        self.silence_threshold = config.silence_finalize_ms / 1000
        self.stability_processor = stability_processor
        self.forwarder = forwarder
        self.config = config
        self.closed_utterances = set()  # Track finalized utterance IDs to ignore late Google responses
        
        # Usage metrics tracking
        self.usage_metrics = usage_metrics
        self.last_billed_seconds = 0  # From Google's total_billed_time
        self.last_final_text = ""  # Track text for metrics
        self.utterance_start_time = None  # Track when utterance started for duration fallback
        self._last_result_type = None  # For AGC feedback
        self._last_word_count = 0  # For AGC feedback
        
        # Final-driven utterance management:
        # Google sends multiple responses per utterance: interim results + final result(s)
        # Problem: Google sometimes sends duplicate final results for same utterance
        # Solution: Process only the FIRST final result per utterance, ignore all subsequent finals
        # This flag tracks whether we've already processed a final for current utterance
        self.utterance_has_final = False
        
        # EOS fallback timer tracking
        self.eos_fallback_timer = None
        
        self.stream_should_close = False  # Flag to signal stream should be closed after utterance finalization
        
        # Track if we got any useful transcription (used by check_no_text_timeout)
        self.has_received_stable_text = False
        
        # Hard timeout - abort if no text received within N seconds of utterance start
        self.max_no_text_threshold = config.max_no_text_seconds
        
    def clear_tracking_sets(self):
        """Clear tracking sets when new stream starts (prevents memory leaks)."""
        self.closed_utterances.clear()
        if self.config.debug.log_lifecycle:
            logger.info("[fsm] Cleared tracking sets for new stream")
    
    def update_speech_timestamp(self, timestamp):
        """Update the last speech timestamp."""
        self.last_speech_ts = timestamp
        
    def start_new_utterance(self):
        """Called when VAD commits to new speech."""
        self.current_utterance_id += 1
        self.state = UtteranceState.ACTIVE
        self.stability_processor.reset_for_new_utterance()

        # Reset final-driven management flag for new utterance
        self.utterance_has_final = False
        self.utterance_start_time = time.time()  # Track start for billing fallback

        # Cancel any pending EOS fallback timer
        if self.eos_fallback_timer:
            self.eos_fallback_timer.cancel()
            self.eos_fallback_timer = None

        # Clear tracking sets for new stream to prevent memory leaks
        self.clear_tracking_sets()

        # Generate trace_id at utterance birth
        from shared_stt.ws_forwarder import generate_trace_id
        self._current_trace_id = generate_trace_id()

        if self.config.debug.log_lifecycle:
            logger.info(f"[fsm] VAD commit. New UTT-{self.current_utterance_id}. State -> ACTIVE.")

        # Reset text tracking for new utterance
        self.has_received_stable_text = False

        # Notify WheelHouse that speech started (for instant visual feedback)
        # This triggers the GUI pulse ~150ms after speech start
        if self.forwarder:
            self.forwarder.send_vad_start(self.current_utterance_id, trace_id=self._current_trace_id)
        
    def process_google_response(self, response):
        """
        Handle response from Google STT using final-driven utterance management.
        
        ARCHITECTURE DECISION - Hybrid Final/EOS Approach:
        ========================================================
        Problem: Google STT sends EOS (End-of-Single-Utterance) events that sometimes arrive 
        WITHOUT a corresponding final transcription result. The original final-driven approach
        ignored all EOS events and waited only for final results, but this caused freezes when
        Google sent EOS without final results.
        
        Root Cause: Google's behavior is inconsistent - sometimes EOS + final, sometimes just EOS,
        sometimes just final. Relying exclusively on either signal causes issues.
        
        Solution: HYBRID approach that prefers final results but uses EOS as a fallback:
        1. Process interim results normally (for real-time feedback)
        2. On EOS event: start 500ms fallback timer, but continue waiting for final result
        3. On final result: process it immediately + finalize (cancels any pending EOS timer)
        4. If EOS timer expires without final result: finalize using EOS fallback
        
        This prevents both premature finalization (EOS before final) and freezing (EOS without final).
        """
        if self.state != UtteranceState.ACTIVE:
            return None

        # wh-2w8y: classify and log every Google response so we can see
        # which response types kept the silence_finalize timer alive during
        # apparent silence on the wire. Runs before the EOS / final / interim
        # branches below, so this fires for every response we get.
        self._log_response_kind(response)

        # HYBRID EOS HANDLING - Use EOS as fallback finalization trigger
        if response.speech_event_type == StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE:
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] EOS event received for UTT-{self.current_utterance_id} - starting EOS fallback timer")

            # Forward the EOS lifecycle event to WheelHouse BEFORE the fallback
            # timer is armed. WheelHouse's three-mode retraction policy
            # (wh-x4fwo) needs the eos signal to arrive ahead of the eventual
            # final on the wire so it can apply Mode 2 (trust stable, drop
            # disagreeing final).
            if self.forwarder:
                tid = getattr(self, '_current_trace_id', '')
                self.forwarder.send_eos(self.current_utterance_id, trace_id=tid)

            # Cancel any existing EOS timer
            if self.eos_fallback_timer:
                self.eos_fallback_timer.cancel()

            # Start a fallback timer to finalize if no final result arrives within 500ms
            def eos_fallback_finalization():
                time.sleep(0.5)  # Wait 500ms for final result
                if (self.state == UtteranceState.ACTIVE and
                    self.current_utterance_id and
                    not self.utterance_has_final):
                    if self.config.debug.log_lifecycle:
                        logger.info(f"[fsm] EOS fallback triggered for UTT-{self.current_utterance_id} - no final result received")
                    self._finalize_utterance("EOS_FALLBACK")

            # Start fallback timer in background and store reference
            self.eos_fallback_timer = threading.Timer(0.5, eos_fallback_finalization)
            self.eos_fallback_timer.start()
            return None
            
        if not response.results:
            return None
            
        # Check if this is a final result
        is_final = any(res.is_final for res in response.results)
        
        if is_final:
            # FIRST FINAL WINS - Process only the first final result per utterance
            if self.utterance_has_final:
                if self.config.debug.log_lifecycle:
                    logger.info(f"[fsm] Ignoring duplicate final result for UTT-{self.current_utterance_id} (already processed)")
                return None
                
            # Mark that we've processed a final for this utterance
            self.utterance_has_final = True
            
            # Cancel any pending EOS fallback timer since we got the final result
            if self.eos_fallback_timer:
                self.eos_fallback_timer.cancel()
                self.eos_fallback_timer = None
            
            # Extract billing info from Google's response
            if hasattr(response, 'total_billed_time') and response.total_billed_time:
                self.last_billed_seconds = response.total_billed_time.seconds
                
            # Process the final result through stability filter
            delta = self.stability_processor.process_response(response, self.current_utterance_id, is_final=True)
            self.last_final_text = delta.strip() if delta else ""
            
            # Finalize the utterance
            self._finalize_utterance("GOOGLE_FINAL")
            
            # Return any text delta from the final result
            return delta
        else:
            # Process interim (non-final) results normally for real-time feedback
            result = self.stability_processor.process_response(response, self.current_utterance_id, is_final=False)
            # Track if we've received any useful transcription (prevents VAD silence abort)
            if result and result.strip():
                self.has_received_stable_text = True
            return result

    def _log_response_kind(self, response):
        """wh-2w8y: classify and log every Google response.

        Reports kind, time since the previous response that updated the
        silence-finalize timer, and the top transcript fragment. Lets us
        verify whether low-stability or empty-results responses kept the
        silence_finalize timer alive during apparent gaps in the stable
        stream, which is the suspected cause of two-sentence merges.
        """
        now = time.time()
        last_t = self.stability_processor.last_google_response_time
        elapsed_ms = (now - last_t) * 1000 if last_t else 0.0

        if response.speech_event_type == StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE:
            logger.info(
                "[google-trace] UTT-%d: EOS (elapsed_since_last=%.0fms)",
                self.current_utterance_id, elapsed_ms,
            )
            return

        if not response.results:
            logger.info(
                "[google-trace] UTT-%d: empty_results (elapsed_since_last=%.0fms)",
                self.current_utterance_id, elapsed_ms,
            )
            return

        is_final = any(r.is_final for r in response.results)
        top = response.results[0]
        transcript = top.alternatives[0].transcript if top.alternatives else ""
        stability = getattr(top, "stability", 0.0)
        threshold = self.stability_processor.stability_threshold

        if is_final:
            logger.info(
                "[google-trace] UTT-%d: final (elapsed_since_last=%.0fms text=%r)",
                self.current_utterance_id, elapsed_ms, redact_transcript(transcript),
            )
        elif stability >= threshold:
            changed = transcript.strip() != self.stability_processor._last_stable_sent.strip()
            kind = "stable_changed" if changed else "stable_unchanged"
            logger.info(
                "[google-trace] UTT-%d: %s (elapsed_since_last=%.0fms stability=%.3f text=%r)",
                self.current_utterance_id, kind, elapsed_ms, stability,
                redact_transcript(transcript),
            )
        else:
            logger.info(
                "[google-trace] UTT-%d: interim_below_threshold (elapsed_since_last=%.0fms stability=%.3f text=%r)",
                self.current_utterance_id, elapsed_ms, stability,
                redact_transcript(transcript),
            )

    def check_silence_finalization(self, current_time):
        """Check if silence threshold exceeded timeout from last Google response."""

        if (self.state == UtteranceState.ACTIVE and 
            self.stability_processor.last_google_response_time and
            (current_time - self.stability_processor.last_google_response_time) > (self.silence_threshold)):
            
            # Finalize to clean up state (overlay mode sends final via websocket)
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] Silence timeout - finalizing UTT-{self.current_utterance_id}")
            return self._finalize_utterance("GOOGLE_SILENCE_2S")
        return None
    

    def check_no_text_timeout(self, current_time: float) -> bool:
        """Check if hard timeout exceeded - no stable text within max_no_text_seconds.
        
        This catches persistent noise where VAD keeps detecting intermittent "speech"
        but Google never returns useful transcription. Unlike VAD silence abort,
        this fires regardless of VAD state.
        
        Returns True if stream should be aborted due to timeout.
        """
        if self.state != UtteranceState.ACTIVE:
            return False
        
        # Disable if threshold is 0 or if we already got useful text
        if self.max_no_text_threshold <= 0 or self.has_received_stable_text:
            return False
        
        # Check if utterance has been active too long without any text
        if self.utterance_start_time is None:
            return False
            
        elapsed = current_time - self.utterance_start_time
        if elapsed >= self.max_no_text_threshold:
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] No-text timeout - {elapsed:.1f}s with no transcription for UTT-{self.current_utterance_id}")
            return True
        
        return False
        
    def _finalize_utterance(self, reason):
        """End current utterance and reset state."""
        if self.state != UtteranceState.ACTIVE:
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] Stale finalization trigger ignored for utt_id={self.current_utterance_id} (state={self.state.name})")
            return None
            
        # Calculate latency if we have speech timing
        latency_ms = (time.time() - self.last_speech_ts) * 1000 if self.last_speech_ts else -1
        
        if self.config.debug.log_lifecycle:
            logger.info(f"[FINAL:{reason}] UTT-{self.current_utterance_id} (eos_latency={latency_ms:.0f}ms)")
        
        # CRITICAL FIX: For non-Google-final triggers (GOOGLE_SILENCE_2S, EOS_FALLBACK,
        # NO_TEXT_TIMEOUT), we must send a final WebSocket message so WheelHouse queues
        # end_utterance. Without this, the clipboard manager times out after 60s.
        # The reason string is forwarded as final_reason so WheelHouse's three-mode
        # retraction policy (wh-x4fwo) can identify Mode 1 (treat-as-fresh) cases.
        if reason != "GOOGLE_FINAL" and self.forwarder:
            # Use the last stable text we sent as the final text
            last_text = self.stability_processor._last_stable_sent.strip()
            tid = getattr(self, '_current_trace_id', '')
            if last_text:
                self.forwarder.send_final(
                    last_text, self.current_utterance_id, trace_id=tid, final_reason=reason
                )
                logger.info(f"[ws] UTT-{self.current_utterance_id}: sent fallback final with {len(last_text)} chars ({reason})")
            else:
                # No stable text was ever sent - send empty final to trigger end_utterance anyway
                # This prevents 60s clipboard timeout even for utterances with no recognized speech
                self.forwarder.send_final(
                    "", self.current_utterance_id, trace_id=tid, final_reason=reason
                )
                logger.info(f"[ws] UTT-{self.current_utterance_id}: sent empty fallback final ({reason})")
        
        # Transition state and mark utterance as closed
        self.state = UtteranceState.FINALIZED
        self.closed_utterances.add(self.current_utterance_id)
        self.stream_should_close = True  # Signal that stream should be closed to prevent contamination
        if self.config.debug.log_lifecycle:
            logger.info(f"[fsm] UTT-{self.current_utterance_id}: State transition to FINALIZED on trigger: {reason}")
        
        # Log usage metrics
        if self.usage_metrics:
            # Get the final text (prefer last_final_text, fallback to last stable sent)
            final_text = self.last_final_text or self.stability_processor._last_stable_sent.strip()
            # Count words in the final text
            word_count = len(final_text.split()) if final_text else 0
            # Use Google's billed time if available, otherwise estimate from stream duration
            billed = self.last_billed_seconds
            if billed == 0 and self.utterance_start_time:
                # Round up to nearest second (Google bills in 1s increments)
                billed = int(time.time() - self.utterance_start_time) + 1
            self.usage_metrics.log_utterance(
                utterance_id=self.current_utterance_id,
                result_type=reason,
                billed_seconds=billed,
                word_count=word_count,
                text=final_text,
            )
            
            # Return the result type and word count for AGC feedback
            self._last_result_type = reason
            self._last_word_count = word_count
            
            # Reset for next utterance
            self.last_billed_seconds = 0
            self.last_final_text = ""
            self.utterance_start_time = None
            
        return None


def handle_set_log_level(level: str):
    """Handle set_log_level command from WheelHouse via WebSocket.

    Adjusts both the logger's own level and every attached handler's level so
    DEBUG records actually propagate through the WebSocketLogHandler when
    WheelHouse asks to see them. Handlers default to INFO at install time
    (see forwarder-setup block), so bumping only the logger would leave DEBUG
    records stranded.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper())
    if numeric_level is not None:
        logger.setLevel(numeric_level)
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        logger.info(f"[ws] Log forwarding level set to {level}")


def main(argv=None):
    """
    The main entry point for the Google STT server with hybrid final/EOS processing.
    
    :flow: STT Transcription
    :step: 2
    :description: Orchestrates the complete speech capture pipeline. Uses *Voice Activity Detection*
    to trigger utterance start, manages *GoogleDirectStreamer* lifecycle, and forwards Google
    responses to the *StabilityProcessor* for overlay mode transmission. Implements **hybrid
    final/EOS utterance management** that prefers Google final results but arms a 500 ms EOS
    fallback to guarantee clean shutdown when finals never arrive. Integrates silence finalization
    and automatic restart handling for PortAudio overflow events, emitting WebSocket notifications
    for the WheelHouse GUI when restarts occur.
    :data_in: Raw audio frames from `MicrophoneStream`, Google streaming responses (partials, finals,
    EOS events, stability scores), overflow telemetry.
    :data_out: Stable/final messages via `WSForwarder`, and optional restart/health notifications.
    """
    args, cfg = load_config()
    
    # Restart control - use threading.Event for thread-safe signaling across threads
    restart_requested_event = threading.Event()
    restart_count = 0
    max_restarts = 3
    
    # Control variables (declared early so handlers can use nonlocal)
    stop = False
    streaming = None
    
    # WebSocket forwarder (initialized later)
    forwarder = None
    
    def on_overflow_detected():
        """Log overflow events without triggering restart."""
        logger.info("[overflow] WARNING: PortAudio buffer overflow detected (audio frames may be dropped)")

    def handle_add_hint(hint: str):
        """Handle add_hint command from WheelHouse via WebSocket.

        This callback is invoked when the user speaks "x-ray boost" with text selected.
        It adds the hint to the shared hints.txt file, sends a notification, and
        triggers a service restart so the new hint takes effect immediately.

        Args:
            hint: The phrase to add to the STT hints list
        """
        try:
            import sys
            # Add shared module to path for import
            shared_path = Path(__file__).parent.parent / "shared"
            if str(shared_path) not in sys.path:
                sys.path.insert(0, str(shared_path))
            from hints_updater import add_hint

            logger.info(f"[config] Processing add_hint request: '{redact_transcript(hint)}'")
            success = add_hint(hint)
            
            if success:
                # Trigger restart to load new hint immediately (thread-safe)
                restart_requested_event.set()
                logger.info(f"[config] Hint added successfully, triggering service restart")
                
                if forwarder:
                    forwarder.send_notification(
                        "STT Hint Added",
                        f"Added '{hint}' - restarting to apply changes"
                    )
            elif not success and forwarder:
                logger.info(f"[config] Hint already exists, sending notification")
                forwarder.send_notification(
                    "STT Hint",
                    f"Hint '{hint}' already exists"
                )
            elif not forwarder:
                logger.info("[config] Warning: forwarder is None, cannot send notification")
        except Exception as e:
            logger.info(f"[config] Error adding hint: {e}")
            if forwarder:
                forwarder.send_notification(
                    "STT Error",
                    f"Failed to add hint: {e}"
                )

    def handle_restart_service():
        """Handle restart_service command from WheelHouse via WebSocket.
        
        This callback is invoked when the user clicks "Restart Transcription Service"
        in the WheelHouse GUI menu. It triggers a graceful restart of the STT process.
        """
        restart_requested_event.set()
        logger.info("[restart] Restart requested via WebSocket command")
        if forwarder:
            forwarder.send_notification(
                "STT Service",
                "Restarting transcription service..."
            )

    def handle_hard_restart_service():
        """Handle hard_restart_service command from WheelHouse via WebSocket.
        
        This triggers a FULL process restart by creating a restart flag file
        and exiting cleanly. The launcher.py supervisor detects the flag and
        restarts the process, allowing all config changes (including device) to apply.
        """
        nonlocal stop
        logger.info("[restart] Hard restart requested - creating flag file and exiting")
        if forwarder:
            forwarder.send_notification(
                "STT Service",
                "Full restart in progress..."
            )
        
        # Create restart flag file for launcher to detect
        from shared_stt.launcher import get_restart_flag_path
        flag_path = get_restart_flag_path("google_stt")
        
        try:
            with open(flag_path, "w") as f:
                f.write("restart")
            logger.info(f"[restart] Created restart flag: {flag_path}")
        except IOError as e:
            logger.info(f"[restart] Failed to create restart flag: {e}")
        
        # Signal main loop to exit
        stop = True

    def handle_shutdown():
        """Handle shutdown command from WheelHouse via WebSocket.

        This triggers a clean shutdown (exit code 0) that the launcher will NOT
        restart. Used when WheelHouse wants to stop the STT provider permanently
        (e.g., when switching to a different provider or shutting down).
        """
        nonlocal stop
        logger.info("[shutdown] Shutdown command received - exiting cleanly")
        # Signal main loop to exit (will exit with code 0)
        stop = True

    def handle_set_interim_results(enabled: bool):
        """Handle set_interim_results command from WheelHouse via WebSocket.

        Toggles whether to send interim (stable) results or only final results.

        Args:
            enabled: If True, send stable (partial) results during speech.
                    If False, only send final results at end of utterance.
        """
        nonlocal stability_processor
        if stability_processor:
            stability_processor.send_interim_results = enabled
            logger.info(f"[config] Interim results {'enabled' if enabled else 'disabled'}")

    # Wake word detection
    wake_word_detector = None
    wake_word_listening = False
    wake_word_mode = getattr(cfg, 'wake_word_mode', "idle_recovery")

    if getattr(cfg, 'wake_word_enabled', False):
        from shared_stt.wake_word_detector import WakeWordDetector
        wake_word_detector = WakeWordDetector(
            keyword=getattr(cfg, 'wake_word_keyword', 'computer'),
            model_dir=getattr(cfg, 'wake_word_model_dir', 'data/wake_words'),
            sensitivity=getattr(cfg, 'wake_word_sensitivity', 0.5),
        )
        logger.info(f"[wake_word] Detector initialized: keyword='{cfg.wake_word_keyword}', "
             f"mode='{wake_word_mode}', loaded={wake_word_detector.is_loaded}")

    def handle_wake_word_activate(reason):
        """Activate/deactivate wake word listening based on transcription status changes.

        Called by WSForwarder when WheelHouse sends set_transcription_status:
        - reason=None: transcription re-enabled -> stop listening for wake word
        - reason="idle": transcription disabled due to idle timeout -> start listening
        - reason="audio"/"sonos": transcription disabled for other reasons

        The wake_word_mode determines which reasons trigger listening:
        - "idle_recovery": only activates on reason="idle"
        - "push_to_talk": activates on reason in ("idle", "audio", "sonos")
        """
        nonlocal wake_word_listening
        if reason is None:
            wake_word_listening = False
            return
        if not wake_word_detector or not wake_word_detector.is_loaded:
            return
        should_activate = False
        if wake_word_mode == "idle_recovery":
            should_activate = (reason == "idle")
        elif wake_word_mode == "push_to_talk":
            should_activate = (reason in ("idle", "audio", "sonos"))
        if should_activate:
            wake_word_detector.reset()
            wake_word_listening = True
            logger.info(f"[wake_word] Listening activated (reason={reason})")
        else:
            wake_word_listening = False

    # Initialize audio capture using factory (auto-selects WinRT or sounddevice)
    audio_config = AudioConfig(
        rate=cfg.rate,
        channels=1,
        chunk_ms=cfg.chunk_ms,
        device_index=cfg.device_index
    )
    
    available_backends = get_available_providers()
    logger.info(f"[audio] Available backends: {available_backends}")
    
    # Overflow detection: log warnings but do NOT restart the mic.
    # Restarting the mic causes a 2s+ blackout that often makes things worse.
    mic = get_audio_provider(config=audio_config, overflow_callback=on_overflow_detected)
    logger.info("[overflow] Overflow logging enabled (auto-restart disabled)")
    
    if args.list_devices:
        for d in mic.list_audio_devices():
            print(f"  [{d['index']}] {d['name']} - rate={d['rate']} channels={d['channels']}")
        return 0

    if cfg.mic_check_seconds > 0:
        return run_mic_check(
            mic,
            duration_seconds=cfg.mic_check_seconds,
            rate=cfg.rate,
            chunk_ms=cfg.chunk_ms,
            write_wav_path=cfg.mic_check_write if cfg.mic_check_write else None,
            device_index=cfg.device_index
        )

    try:
        from direct_streamer import GoogleDirectStreamer
    except ImportError:
        logger.info("[ERROR] Failed to import Google STT components.")
        return 1

    # Initialize core components
    # Initialize Silero VAD (only supported backend)
    logger.info(f"[vad] Using Silero VAD (threshold={cfg.silero_threshold})")
    vad = SileroVAD(threshold=cfg.silero_threshold, sample_rate=cfg.rate)
    
    transcription_enabled_event = threading.Event()
    transcription_enabled_event.set()

    # Disconnect timeout: exit cleanly after 5s without WheelHouse connection
    DISCONNECT_TIMEOUT_S = 5.0
    disconnect_timer: threading.Timer | None = None

    def handle_disconnect_timeout():
        """Called when disconnect timeout expires - exit cleanly."""
        nonlocal stop
        logger.info(f"[ws] Wheelhouse disconnected for {DISCONNECT_TIMEOUT_S}s - exiting cleanly")
        stop = True

    def handle_wheelhouse_disconnect():
        """Handle WheelHouse disconnect - pause transcription and start exit timer."""
        nonlocal disconnect_timer
        logger.info("[ws] Wheelhouse disconnected - pausing transcription, will exit in 5s if no reconnect")
        transcription_enabled_event.clear()

        # Start disconnect timeout timer
        if disconnect_timer:
            disconnect_timer.cancel()
        disconnect_timer = threading.Timer(DISCONNECT_TIMEOUT_S, handle_disconnect_timeout)
        disconnect_timer.daemon = True
        disconnect_timer.start()

    def handle_wheelhouse_reconnect():
        """Handle WheelHouse reconnect - cancel exit timer and resume transcription."""
        nonlocal disconnect_timer
        logger.info("[ws] Wheelhouse reconnected - resuming transcription")

        # Cancel disconnect timeout timer
        if disconnect_timer:
            disconnect_timer.cancel()
            disconnect_timer = None

        # Re-enable transcription (the server will send set_transcription_status if needed)
        transcription_enabled_event.set()

    # Setup WebSocket forwarder
    if cfg.forward_ws:
        try:
            forwarder = WSForwarder(
                host=cfg.ws_host,
                port=cfg.ws_port,
                transcription_enabled_event=transcription_enabled_event,
                add_hint_callback=handle_add_hint,
                restart_callback=handle_restart_service,
                hard_restart_callback=handle_hard_restart_service,
                on_disconnect_callback=handle_wheelhouse_disconnect,
                on_reconnect_callback=handle_wheelhouse_reconnect,
                shutdown_callback=handle_shutdown,
                set_interim_results_callback=handle_set_interim_results,
                set_log_level_callback=handle_set_log_level,
                wake_word_activate_callback=handle_wake_word_activate,
                debug=cfg.debug.log_lifecycle,
                provider_name="google_stt",
                emits_eos=True,
            )
            forwarder.start()
            # Enable log forwarding to WheelHouse via the standard Python logging
            # pipeline (wh-6wp). The handler queues records through WSForwarder,
            # so logs emitted before the WebSocket connects are buffered, not lost.
            ws_log_handler = WebSocketLogHandler(forwarder, source="Google STT")
            ws_log_handler.setLevel(logging.INFO)
            logger.addHandler(ws_log_handler)
            logger.propagate = False
            # Forward the audio overflow monitor's rate-limited INFO summary to
            # WheelHouse so an ongoing audio overflow is visible in
            # wheelhouse.log, not only on the provider console. Use a SEPARATE
            # handler instance fixed at INFO, not the GoogleSTT handler above:
            # handle_set_log_level() raises/lowers the level of every handler on
            # the GoogleSTT logger, so sharing one object would let a
            # set_log_level("WARNING") command silently stop forwarding the
            # overflow summary. Attach to the specific overflow logger, not a
            # broad parent, to avoid catching shared_stt.ws_forwarder's own
            # "sent log" records and creating a feedback loop (see
            # distil_medium_en for the same pattern).
            overflow_log_handler = WebSocketLogHandler(forwarder, source="Google STT")
            overflow_log_handler.setLevel(logging.INFO)
            logging.getLogger("shared_audio.overflow_monitor").addHandler(overflow_log_handler)
            logger.info(f"[ws] forwarding to ws://{cfg.ws_host}:{cfg.ws_port}")
        except Exception as e:
            logger.warning(f"[ws] Could not start WebSocket forwarder: {e}")

    # Initialize stability-based processing and usage metrics
    usage_metrics = UsageMetrics()
    stability_processor = StabilityProcessor(cfg, forwarder)
    utterance_mgr = UtteranceManager(cfg, stability_processor, forwarder, usage_metrics)

    # Initialize Smart AGC (uses config from config_loader)
    agc_config = AGCConfig(
        enabled=cfg.agc.enabled,
        target_speech_rms=cfg.agc.target_speech_rms,
        vad_threshold_rms=cfg.agc.vad_threshold_rms,
        noise_floor_alpha=cfg.agc.noise_floor_alpha,
        min_gain=cfg.agc.min_gain,
        max_gain=cfg.agc.max_gain,
        initial_noise_floor=cfg.agc.initial_noise_floor,
    )
    agc = SmartAGC(agc_config)
    agc_log_interval = 30.0  # Log AGC diagnostics every 30 seconds
    last_agc_log = time.time()

    # Overflow diagnostics
    overflow_diag_interval = 30.0
    last_overflow_diag_log = time.time()
    vad_times_ms = []
    agc_times_ms = []

    # Always-on consumer stall detection: when this loop goes unscheduled for
    # seconds (whole-machine CPU saturation) the capture queue fills and
    # frames drop with no other direct log signature
    # (wh-stt-audio-consumer-behind-realtime).
    stall_tracker = LoopStallTracker()

    if cfg.agc.enabled:
        logger.info(f"[agc] Smart AGC enabled: target_rms={cfg.agc.target_speech_rms}, max_gain={cfg.agc.max_gain}")
    else:
        logger.info("[agc] Smart AGC disabled")

    def _stop(*_):
        nonlocal stop
        stop = True

    # Setup signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: 
            signal.signal(sig, _stop)
        except Exception: 
            pass
    
    # Helper function to close stream cleanly
    def close_stream(reason: str):
        nonlocal streaming, vad_gate_open
        if not streaming: 
            return
        logger.info(f"[metrics] Total stream active time: {streaming.elapsed:.2f}s ({reason})")
        streaming.finish()
        streaming = None
        # Reset VAD gate and buffer state
        vad_gate_open = False
        lead_in_buffer.clear()
        vad.reset()
        utterance_mgr.state = UtteranceState.IDLE
        if cfg.debug.log_lifecycle:
            logger.info("[fsm] Stream closed. Resetting VAD and FSM state to IDLE.")
        return None

    # Initialize audio processing
    mic.start()

    # Keep the per-frame consumer loop scheduled under machine-wide CPU load;
    # it needs only a few percent of one core but must get it on time
    # (wh-stt-audio-consumer-behind-realtime).
    consumer_elevated = elevate_current_thread('highest')
    logger.info(f"[priority] Consumer thread priority elevated: {consumer_elevated}")


    # --- Lead-in Buffer ---
    # Keeps rolling audio so first syllables aren't cut off when speech starts
    lead_in_frames = int(cfg.vad_lead_in_ms / cfg.chunk_ms)
    lead_in_buffer = collections.deque(maxlen=lead_in_frames)
    vad_gate_open = False  # True when streaming audio to Google
    last_is_speech = False  # Track last VAD result for silence timeouts
    mic_none_count = 0  # Track consecutive mic.read() -> None for stall detection
    # --------------------------

    # Display startup banner with version info
    logger.info(f"[startup] {get_startup_banner('Google STT Server')} - Ready")

    # Send "ready" notification to WheelHouse after WebSocket connects
    # Note: WheelHouse sends "Loading..." notification when starting provider,
    # so we only need to send the "ready" notification here
    if forwarder:
        def send_ready_notification():
            time.sleep(3.0)  # Wait for WebSocket to connect and service to be ready

            logger.info("[startup] Sending 'ready' notification to Wheelhouse...")
            forwarder.send_notification(
                "Google STT",
                "Transcription service ready"
            )
            logger.info("[startup] Startup notification sent")
        threading.Thread(target=send_ready_notification, daemon=True).start()

    if cfg.debug.log_lifecycle:
        logger.info(f"[vad-debug] lead_in_ms={cfg.vad_lead_in_ms}")

    # Main processing loop
    try:
        while not stop:
            stall_msg = stall_tracker.record(mic.get_queue_size())
            if stall_msg:
                logger.info(stall_msg)

            # Check if restart was requested (thread-safe check)
            if restart_requested_event.is_set():
                restart_count += 1
                if restart_count > max_restarts:
                    logger.info(f"[restart] Maximum restart attempts ({max_restarts}) exceeded - stopping service")
                    # Send notification about failure
                    if forwarder and cfg.forward_ws:
                        forwarder.send_notification(
                            "Wheelhouse: STT Service Error",
                            f"Audio overflow persists after {max_restarts} restart attempts. Service stopped."
                        )
                    stop = True
                    break
                    
                logger.info(f"[restart] Performing restart #{restart_count}")
                
                # Send notification about restart
                if forwarder and cfg.forward_ws:
                    forwarder.send_notification(
                        "Wheelhouse: STT Restarting",
                        f"Restarting service (attempt {restart_count}/{max_restarts})..."
                    )
                
                # Close current stream cleanly
                if streaming:
                    logger.info("[restart] Closing current stream")
                    streaming.finish()
                    streaming = None
                
                # Stop and restart microphone
                logger.info("[restart] Restarting microphone stream")
                mic.stop()
                time.sleep(2.0)  # Wait for resources to clear
                mic.start()
                mic.reset_overflow_monitor()
                
                # Reload config from disk (soft restart hot-reload)
                _, new_cfg = load_config()
                old_hints_count = len(cfg.phrase_hints)
                cfg = new_cfg  # Update the config reference
                
                # Update StabilityProcessor with new config
                stability_processor.stability_threshold = cfg.latency.stability_commit_threshold
                stability_processor.config = cfg
                
                # Update UtteranceManager thresholds
                utterance_mgr.silence_threshold = cfg.silence_finalize_ms / 1000
                utterance_mgr.max_no_text_threshold = cfg.max_no_text_seconds
                utterance_mgr.config = cfg
                
                # Reset lead-in buffer with new settings
                lead_in_frames = int(cfg.vad_lead_in_ms / cfg.chunk_ms)
                lead_in_buffer.clear()
                # Recreate buffer with new maxlen if lead_in_ms changed
                lead_in_buffer = collections.deque(maxlen=lead_in_frames)
                vad_gate_open = False
                
                logger.info(f"[restart] Config reloaded: hints={len(cfg.phrase_hints)} (was {old_hints_count})")
                
                # Reset state
                utterance_mgr.state = UtteranceState.IDLE
                restart_requested_event.clear()
                
                logger.info("[restart] Restart completed - resuming normal operation")

                # Reset restart counter after successful restart
                # This allows future hint additions to work without hitting the max limit
                restart_count = 0

                # Send completion notification
                if forwarder and cfg.forward_ws:
                    forwarder.send_notification(
                        "STT Service",
                        "Service restart completed. Ready."
                    )

                # The restart block pauses this loop for seconds on purpose;
                # don't count that pause as a scheduling stall.
                stall_tracker.reset()
                continue
            
            # Skip all audio processing if transcription is disabled (audio suppression active)
            # This prevents VAD triggering, utterance starts, and API billing during suppression
            if not transcription_enabled_event.is_set():
                audio_frame = mic.read(timeout=0.05)
                if audio_frame and wake_word_detector and wake_word_listening:
                    result = wake_word_detector.process(audio_frame)
                    if result:
                        forwarder.send_wake_word_detected(result)
                        wake_word_listening = False
                        transcription_enabled_event.set()
                        vad.reset()
                        logger.info(f"[wake_word] '{result}' detected - resuming transcription")
                else:
                    time.sleep(0.02)  # Small delay to prevent busy-wait
                continue
            
            current_time = time.time()
            
            # 1. Process Google STT responses
            if streaming:
                while True:
                    try:
                        response = streaming.get_response_non_blocking()
                        if response is None:
                            logger.info("[fsm] Streamer signaled an error or session end.")
                            time.sleep(0.2)
                            break
                        
                        # Process response through utterance manager
                        result = utterance_mgr.process_google_response(response)
                        
                        # Feed AGC outcome for successful GOOGLE_FINAL
                        if hasattr(utterance_mgr, '_last_result_type') and utterance_mgr._last_result_type == "GOOGLE_FINAL":
                            agc.on_stt_outcome("GOOGLE_FINAL", utterance_mgr._last_word_count)
                            utterance_mgr._last_result_type = None  # Clear to avoid duplicate calls
                        
                        if result == "STOP":
                            stop = True
                            break

                            
                    except queue.Empty:
                        break

            # 2. Read audio and process via AGC + Deflector
            audio_frame = mic.read(timeout=0.05)
            if audio_frame is None:
                mic_none_count += 1
                if mic_none_count == 40:
                    logger.info("[mic] WARNING: no audio frames for ~2s -- microphone may be stalled")
                elif mic_none_count > 0 and mic_none_count % 600 == 0:
                    logger.info(f"[mic] WARNING: still no audio frames ({mic_none_count * 0.05:.0f}s stalled)")
                continue
            if mic_none_count >= 40:
                logger.info(f"[mic] Audio resumed after {mic_none_count * 0.05:.1f}s stall ({mic_none_count} None reads)")
            mic_none_count = 0
            
            # Get raw VAD result BEFORE AGC (for AGC's is_speech parameter)
            if cfg.debug.log_overflow_diagnostics:
                t0 = time.perf_counter()
            raw_is_speech = vad.is_speech(audio_frame)
            if cfg.debug.log_overflow_diagnostics:
                t1 = time.perf_counter()

            # wh-2w8y: log every VAD transition while the gate is open. This
            # captures whether Silero saw silence inside a single utterance
            # whose stable transcript Google later revised end-to-end.
            # INFO level so it forwards to wheelhouse.log over the existing
            # WebSocketLogHandler.
            if vad_gate_open and raw_is_speech != last_is_speech:
                logger.info(
                    "[vad-trace] UTT-%d: %s (silero_conf=%.3f)",
                    utterance_mgr.current_utterance_id,
                    "speech_start" if raw_is_speech else "silence_start",
                    vad.get_confidence(),
                )

            last_is_speech = raw_is_speech  # Track for silence timeouts

            # Apply Smart AGC - normalizes audio and adapts to noise floor
            agc_audio = agc.process(audio_frame, raw_is_speech)
            if cfg.debug.log_overflow_diagnostics:
                t2 = time.perf_counter()
                vad_times_ms.append((t1 - t0) * 1000)
                agc_times_ms.append((t2 - t1) * 1000)
            
            # --- Inline Lead-in Buffer Logic (replaces VadDeflector) ---
            valid_chunks = []
            if vad_gate_open:
                # Gate open: pass audio directly through
                valid_chunks.append(agc_audio)
            else:
                # Gate closed: check if speech started
                if raw_is_speech:
                    # Speech detected! Open gate and send lead-in + current frame
                    vad_gate_open = True
                    valid_chunks.extend(lead_in_buffer)  # Send buffered audio first
                    valid_chunks.append(agc_audio)
                    lead_in_buffer.clear()
                else:
                    # Silence: add to rolling lead-in buffer
                    lead_in_buffer.append(audio_frame)
            # --------------------------
            
            # Periodic AGC diagnostics logging
            if cfg.agc.enabled and (current_time - last_agc_log) > agc_log_interval:
                diag = agc.diagnostics
                logger.debug(f"[agc] gain={diag['effective_gain']:.2f}x, noise_floor={diag['noise_floor']:.4f}, failures={diag['consecutive_failures']}")
                last_agc_log = current_time

                # VAD stall check (piggyback on AGC interval)
                vad_warning = vad.check_stall()
                if vad_warning:
                    logger.info(vad_warning)

            # Periodic overflow diagnostics logging
            if cfg.debug.log_overflow_diagnostics and (current_time - last_overflow_diag_log) > overflow_diag_interval:
                mic_q = mic.get_queue_size()
                streamer_diag = streaming.diagnostics if streaming else {}

                vad_avg = sum(vad_times_ms) / len(vad_times_ms) if vad_times_ms else 0
                vad_max = max(vad_times_ms) if vad_times_ms else 0
                agc_avg = sum(agc_times_ms) / len(agc_times_ms) if agc_times_ms else 0
                agc_max = max(agc_times_ms) if agc_times_ms else 0

                stall_snap = stall_tracker.snapshot_and_reset_window()

                logger.info(f"[overflow-diag] queues: mic={mic_q}, google_audio={streamer_diag.get('audio_q_size', 0)}, google_resp={streamer_diag.get('response_q_size', 0)}")
                logger.info(f"[overflow-diag] timing: vad={vad_avg:.1f}ms(max={vad_max:.1f}), agc={agc_avg:.2f}ms(max={agc_max:.2f})")
                logger.info(f"[overflow-diag] loop stalls: count={stall_snap['stalls']}, max_gap={stall_snap['max_gap_ms']:.0f}ms")

                vad_times_ms.clear()
                agc_times_ms.clear()
                last_overflow_diag_log = current_time

            # Update timestamps based on VAD (for silence timeouts)
            if last_is_speech:
                utterance_mgr.update_speech_timestamp(current_time)

            # 3. Handle Valid Speech (Start/Continue Stream)
            if valid_chunks:
                # If valid_chunks is not empty, it means we have Confirmed Speech
                
                # Start new stream if needed
                if streaming is None:
                    if utterance_mgr.state in (UtteranceState.IDLE, UtteranceState.FINALIZED):
                        utterance_mgr.start_new_utterance()
                        
                        try:
                            # (Your existing Streamer init code here...)
                            streaming = GoogleDirectStreamer(
                                language=cfg.language, model=cfg.model, sample_rate=cfg.rate,
                                enable_auto_punct=cfg.auto_punct, debug_cfg=cfg.debug,
                                single_utterance=cfg.single_utterance, phrase_hints=cfg.phrase_hints,
                                phrase_hints_boost=cfg.hints_boost, class_tokens=cfg.class_tokens,
                                transcription_enabled_event=transcription_enabled_event
                            )
                            streaming.start()
                        except Exception as e:
                            logger.info(f"Failed to start streamer: {type(e).__name__}: {e!r}")
                            streaming = None

                # Send audio to Google
                if streaming and utterance_mgr.state == UtteranceState.ACTIVE:
                    for chunk in valid_chunks:
                        streaming.send_audio(chunk)


            # 4.5. Check for hard no-text timeout (catches persistent intermittent noise)
            if streaming and utterance_mgr.check_no_text_timeout(current_time):
                utterance_mgr._finalize_utterance("NO_TEXT_TIMEOUT")
                # Feed outcome to AGC (false positive - sent to Google but no text)
                agc.on_stt_outcome("NO_TEXT_TIMEOUT", utterance_mgr._last_word_count)

            # 5. Check silence-based finalization
            result = utterance_mgr.check_silence_finalization(current_time)
            if result == "STOP":
                stop = True
                break


            # 5.5. Check if stream should be closed after utterance finalization
            if utterance_mgr.stream_should_close and streaming:
                close_stream("prevent_contamination")
                utterance_mgr.stream_should_close = False
                continue

            # 6. Stream lifecycle management
            if streaming and streaming.elapsed >= cfg.max_stream_seconds:
                result = close_stream("hit max duration")
                if result == "STOP":
                    stop = True
                    break

    finally:
        if streaming:
            logger.info(f"[metrics] Total stream active time: {streaming.elapsed:.2f}s (program exit)")
            streaming.finish()
        mic.stop()
        if forwarder:
            forwarder.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())