"""Speech service composition and initialization.

This module creates and wires together the speech processing pipeline components.
It acts as a service container that initializes the pattern catalog,
command parser, and speech processor with proper dependency injection.

Key Classes:
  - SpeechHandler: Service container for speech processing components

Components Created:
  - PatternCatalog: Loads and indexes unified command/replacement patterns
  - TextParser: Parses and executes command patterns
  - FocusRedirectPolicy: Decides per-DICTATE-word whether dictation
    routes to the persistent hidden editor (terminal-at-prompt) or
    falls through to the standard ``intelligent_insert_text`` path
    (wh-g2-refactor.18 slice 18.32.1).
  - SpeechProcessor: Main word processing state machine; consults the
    focus-redirect policy and routes dictation either through
    ``logic_controller.insert_editor_word`` or through the standard
    ``intelligent_insert_text`` IPC.

wh-g2-refactor.18: the heavy focus-redirect path (held-back queues,
drain coordination, asymmetric lifecycle wiring) is gone. The
persistent hidden dictation editor on the GUI side owns the actual
editor lifecycle; this composition layer only wires the small
policy-only surface that decides where each DICTATE word goes.

Typical Usage:
  from speech.speech_handler import SpeechHandler
  from services.wheelhouse.config_service import ConfigService

  config_service = ConfigService()
  handler = SpeechHandler(app, logic_controller, config_service)

  # Initialize speech processor with word queue
  handler.initialize_speech_processor(word_queue)
  await handler.speech_processor.start()
"""
# speech/speech_handler.py
import logging
import asyncio
from typing import Optional
from .command_engine import TextParser
from .pattern_catalog import PatternCatalog
from .speech_processor import SpeechProcessor

logger = logging.getLogger(__name__)

class SpeechHandler:
    """Service container for speech processing components.
    
    Creates and wires together the speech processing pipeline:
    - PatternCatalog: Unified pattern storage and lookup
    - TextParser: Command parsing
    - SpeechProcessor: Main processing state machine
    """
    def __init__(self, app, logic_controller, config_service):
        """Initialize speech service components.
        
        Args:
            app: WheelHouse application instance
            logic_controller: Main logic controller
            config_service: Configuration service
        """
        self.app = app
        self.logic_controller = logic_controller
        self.config_service = config_service
        self.config = self.config_service.get_config()
        self.patterns_file = self.config.get("STT_PATTERNS_FILE", "speech/config/patterns.toml")
        self.user_patterns_file = self._resolve_user_patterns_file()

        logger.info(
            "Initializing SpeechHandler with system patterns file: %s, "
            "user patterns file: %s",
            self.patterns_file, self.user_patterns_file,
        )

        # Create PatternCatalog first (single source of truth for patterns).
        # It loads the system file plus the writable user file and merges them.
        self.pattern_catalog = PatternCatalog(self.patterns_file, self.user_patterns_file)
        
        # Create TextParser with patterns from catalog (no independent loading)
        self.text_parser = TextParser(self, self.pattern_catalog)
        
        # Speech processor will be initialized after word_queue is available
        self.speech_processor: Optional[SpeechProcessor] = None

    def _resolve_user_patterns_file(self) -> str:
        """Resolve the writable user patterns file path.

        Honors an optional ``STT_USER_PATTERNS_FILE`` config override (a test
        seam, mirroring the approved-controls path override); otherwise
        resolves ``get_user_data_dir()/user_patterns.toml`` so the file
        survives a shipped-patterns update (wh-user-patterns-split).
        """
        override = self.config.get("STT_USER_PATTERNS_FILE")
        if isinstance(override, str) and override:
            return override
        try:
            from utils.system import get_user_data_dir
            return str(get_user_data_dir() / "user_patterns.toml")
        except Exception:
            # get_user_data_dir() does mkdir() under a frozen build, which can
            # raise on a permission or disk error. Degrade to no user file
            # (the catalog and manager both treat "" as absent) rather than
            # crashing speech init (wh-user-patterns-split-bulletproof.3.2).
            logger.warning(
                "Could not resolve the user patterns directory; loading "
                "system patterns only",
                exc_info=True,
            )
            return ""

    def initialize_speech_processor(self, word_queue: asyncio.Queue):
        """Initialize speech processor with word queue.

        Called after websocket_manager is created to wire up the word queue.

        Args:
            word_queue: Queue from websocket_manager containing WordEvents
        """
        # Fail-fast: require explicit timeout configuration (no defaults)
        try:
            replacement_timeout_ms = self.config["REPLACEMENT_TIMEOUT_MS"]
            command_timeout_ms = self.config["COMMAND_TIMEOUT_MS"]
        except KeyError as e:
            raise ValueError(
                f"Missing required configuration: {e}. "
                "Please set REPLACEMENT_TIMEOUT_MS and COMMAND_TIMEOUT_MS in config.toml"
            ) from e
        # Greedy-pattern timer (wh-greedy-buffer-race). Optional with a sane
        # default so an older config.toml without the key still works.
        greedy_timeout_ms = self.config.get("GREEDY_TIMEOUT_MS", 5000)

        # Get hotword from pattern catalog (loaded from patterns.toml)
        hotword = self.pattern_catalog.command_hotword or "x-ray"

        # wh-g2-refactor.18 (slice 18.32.1): re-wire the focus-redirect
        # policy onto the SpeechProcessor so DICTATE words can route
        # into the persistent hidden editor via the editor IPC. The
        # policy itself is unchanged; only its consumer changed (the
        # processor now calls should_redirect directly instead of the
        # deleted FocusRedirectPath wrapper).
        focus_redirect_policy = _build_focus_redirect_policy()
        focused_hwnd_provider = _build_default_focused_hwnd_provider()

        # Wire the policy's prewarm hook on the websocket manager so
        # each Silero vad_start fires the prompt detector for the
        # current foreground HWND. By the time the first dictated
        # word arrives ~1.5 seconds later, the detector result is
        # cached and the policy's 500 ms timeout becomes a cold-start
        # backstop instead of a hot-path wait
        # (wh-prewarm-detector-vad-start).
        prewarm_status = "skipped (no websocket_manager)"
        ws_manager = getattr(self.app, "websocket_manager", None)
        if ws_manager is not None and hasattr(
            ws_manager, "set_vad_start_callback",
        ):
            def _vad_start_prewarm(
                policy=focus_redirect_policy,
                provider=focused_hwnd_provider,
            ) -> None:
                try:
                    hwnd = int(provider() or 0)
                except Exception:
                    return
                if not hwnd:
                    return
                try:
                    policy.prewarm(hwnd)
                except Exception:
                    logger.exception(
                        "focus_redirect_policy.prewarm raised; "
                        "detector will be re-attempted on the first "
                        "dictated word"
                    )

            ws_manager.set_vad_start_callback(_vad_start_prewarm)
            prewarm_status = "wired"

        logger.info(
            "Focus-redirect policy wired (wh-g2-refactor.18 slice "
            "18.32.1); vad_start prewarm %s",
            prewarm_status,
        )
        logger.info("Initializing SpeechProcessor")
        self.speech_processor = SpeechProcessor(
            word_queue=word_queue,
            catalog=self.pattern_catalog,
            text_parser=self.text_parser,
            app=self.app,
            replacement_timeout_ms=replacement_timeout_ms,
            command_timeout_ms=command_timeout_ms,
            greedy_timeout_ms=greedy_timeout_ms,
            hotword=hotword,
            logic_controller=self.logic_controller,
            focus_redirect_policy=focus_redirect_policy,
            focused_hwnd_provider=focused_hwnd_provider,
        )

    def apply_hotword(self, hotword: str) -> None:
        """Push a changed command hotword onto the running speech processor.

        Called after a PatternCatalog reload so a new hotword -- from the
        shipped file or a user override -- takes effect immediately instead of
        only on restart (wh-user-patterns-split.4). A no-op before the speech
        processor has been created.
        """
        if self.speech_processor is not None:
            self.speech_processor.apply_hotword(hotword)


def _build_focus_redirect_policy():
    """Construct the production FocusRedirectPolicy with its dependencies.

    Wires:
      * a fresh LogicMirror stub (the persistent editor lives in CLOSED
        from the policy's point of view; the mirror is retained as a
        named seam so a future re-introduction of editor-aware
        short-circuits has a single hook to extend);
      * the production PromptDetector via a lazy-imported callable so
        headless test contexts that import speech_handler without
        Win32 do not crash here.
    """
    from services.wheelhouse.shared.editor_lifecycle import LogicMirror
    from services.wheelhouse.speech.focus_redirect_policy import (
        FocusRedirectPolicy,
    )

    mirror = LogicMirror()
    return FocusRedirectPolicy(
        mirror=mirror,
        prompt_detector_call=_default_prompt_detector_call,
    )


def _default_prompt_detector_call(process_name: str, pid: int) -> bool:
    """Default prompt-detector callable for FocusRedirectPolicy.

    Lazy-imports PromptDetector so this module can be imported in
    headless test contexts that have no Win32 available. Returns False
    on any import or unexpected call failure; the policy's fail-closed
    posture is enforced regardless.

    A ``ConsoleProbeError`` -- the console-probe client's signal for a
    TRANSPORT failure (read timeout, EOF, broken pipe, malformed
    response, dead helper) -- is deliberately propagated, NOT swallowed
    into a False. The FocusRedirectPolicy runs this callable inside
    ``asyncio.wait_for`` and maps a raised exception to its
    ``prompt_detector_error`` failure path. Swallowing it here would
    re-create the wh-jvrs.3.1 bug: a transient helper stall would surface
    as a False busy verdict that the policy caches as ``terminal_busy``,
    suppressing the terminal-editor redirect for the whole utterance even
    when the terminal is actually at a prompt.
    """
    try:
        from services.wheelhouse.ui.prompt_detector import PromptDetector
    except Exception:
        return False
    try:
        detector = PromptDetector()
        return bool(detector.is_at_prompt(process_name, pid))
    except Exception as exc:
        # A ConsoleProbeError is a transport failure: propagate it so the
        # policy routes it to prompt_detector_error (wh-jvrs.3.1). The shared
        # classifier recognises it across BOTH import-path class copies
        # (wh-jvrs.3.2) AND any subclass anywhere in the MRO (wh-jvrs.3.6) --
        # a bare name-only ``type(exc).__name__ == "ConsoleProbeError"`` check
        # silently dropped subclasses and re-created the 3.1 bug here. Every
        # other failure still fails closed to False.
        if _classify_console_probe_error(exc):
            raise
        return False


def _classify_console_probe_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` is a ConsoleProbeError or subclass.

    Delegates to the single classifier in ``ui.prompt_detector`` so this seam
    and ``PromptDetector.is_at_prompt`` share one definition and cannot drift
    (wh-jvrs.3.6). Lazy-imports it to keep ``speech_handler`` importable in
    headless contexts; if even that import fails, falls back to an MRO name
    walk so subclasses are still recognised rather than dropped.
    """
    try:
        from services.wheelhouse.ui.prompt_detector import (
            is_console_probe_error,
        )
        return is_console_probe_error(exc)
    except Exception:
        return any(
            base.__name__ == "ConsoleProbeError" for base in type(exc).__mro__
        )


def _build_default_focused_hwnd_provider():
    """Return a zero-arg callable that reports the current foreground HWND.

    Lazy-imports ``win32gui`` so headless test contexts do not crash.
    Falls back to a callable that always returns 0 (which the policy
    treats as ``cannot_resolve_focused_process`` and silently declines).
    """
    try:
        import win32gui
    except ImportError:
        return lambda: 0
    return win32gui.GetForegroundWindow