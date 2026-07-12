"""AIService -- coordinator for text correction and help Q&A.

Manages the AI provider lifecycle, prompt construction, response sanitization,
speech output, and concurrency control. This is the central service that
action functions call into.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from ai.help_chat import HelpChatSession
from ai.prompts import HELP_CHAT_SYSTEM, HELP_SYSTEM_TEMPLATE, TEXT_CORRECTION_SYSTEM
from ai.providers.openai_compat import ChatResult, ChatStatus, OpenAIProvider
from ai.speech_output import SpeechOutput

log = logging.getLogger(__name__)

# Default interval (seconds) for the periodic readiness probe and model-list
# refresh background loops added in Phase A (design 5.2).
_REFRESH_INTERVAL_S = 60


def _legacy_to_result(raw) -> ChatResult:
    """Pass a ChatResult from provider chat() through unchanged (design 5.1a).

    The bare-str branches that once handled LlamaCppProvider / OllamaProvider
    returns were removed in wh-ay6h.13.5: those providers were deleted in
    Phase C (commit 43533716) and OpenAIProvider always returns ChatResult.
    """
    if isinstance(raw, ChatResult):
        return raw
    raise TypeError(f"provider.chat() returned {type(raw).__name__!r}, expected ChatResult")

# Words-to-tokens ratio (rough approximation)
_WORDS_TO_TOKENS = 1.4

# Preamble patterns that LLMs commonly prepend
_PREAMBLE_PATTERNS = [
    re.compile(r"^(?:Sure!?\s*)?Here(?:'s| is) the (?:corrected|fixed) (?:text|version):\s*\n?", re.IGNORECASE),
    re.compile(r"^Corrected version:\s*\n?", re.IGNORECASE),
    re.compile(r"^Sure!?\s*Here you go:\s*\n?", re.IGNORECASE),
]

# Trailing commentary pattern: blank line followed by non-content text
_TRAILING_COMMENTARY = re.compile(
    r"\n\n(?:I hope this helps|Let me know|Note:|Please note|Is there anything)"
    r".*$",
    re.IGNORECASE | re.DOTALL,
)

# Thinking tags
_THINKING_TAG = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Code fences
_CODE_FENCE = re.compile(r"^```\w*\n?(.*?)\n?```$", re.DOTALL)


class AIService:
    """Manages AI capabilities: text correction, help Q&A."""

    def __init__(self, config_service):
        self._config = config_service
        self._provider = None
        self._knowledge_base: Optional[str] = None
        self._speech = SpeechOutput()
        self._processing_lock = asyncio.Lock()
        self.cancel_requested: bool = False
        self._help_session: Optional[HelpChatSession] = None

        # -- Phase A thin-client coordinator state (design 5.2) --
        # The currently-selected model id for the thin-client path. Mutated by
        # set_model under the processing lock.
        self._model_name: str = self._config.get("ai.server.model", "")
        # Cached live model list from the most recent refresh_models() call.
        self._models: list[str] = []
        self._models_refreshed_at: Optional[float] = None
        self._last_refresh_ok: bool = False
        # Cached readiness: updated by recheck_ready() and (local) a successful
        # refresh_models(). is_ready() reads this; it is never frozen after the
        # one startup probe.
        self._ready: bool = False
        # Background tasks (first probe + periodic loops). Held so they can be
        # cancelled on stop and are not garbage-collected while running.
        self._bg_tasks: list[asyncio.Task] = []
        # One-time latch: warn at most once when [ai.server] is missing while
        # ai.enabled is true (upgrade-from-old-config case, design 5.4).
        self._warned_missing_server = False
        # Cached ChatResult from the most recent help_ask()/chat_help() call.
        # help_ask() returns this so _handle_help_ask can read .ok/.truncated
        # without the str-returning chat_help -> HelpChatSession.ask path
        # discarding the finish_reason signal (design 5.1a / s7).
        self._last_help_result: ChatResult = ChatResult(status=ChatStatus.EMPTY)

    # -- Lifecycle --

    async def start(self) -> None:
        """Build the thin-client coordinator from [ai.server] and probe in the
        background (design 5.2).

        Constructs a single OpenAIProvider from the [ai.server] block and never
        eager-loads or makes a synchronous reachability call -- a slow or
        black-holed server must not delay startup. When AI is off (disabled or
        no server address) start() loads the knowledge base + help session and
        returns without building a provider or launching any network probe.
        Otherwise it launches the first reachability probe (not awaited) plus
        the periodic recheck_ready loop (both kinds) and the local-only
        refresh_models loop via _launch_background_probes().
        """
        self._knowledge_base = self._load_knowledge_base()
        self._help_session = HelpChatSession(self)

        # AI off: no provider, no probe, no network. is_ready() stays False.
        if self._ai_off():
            self._provider = None
            # Upgrade-from-old-config warning (design 5.4): when ai.enabled is
            # true but [ai.server].base_url is blank the user needs to know why
            # AI is off. Emit at most once per service instance.
            if (
                self._config.get("ai.enabled", True)
                and not self._config.get("ai.server.base_url", "")
                and not self._warned_missing_server
            ):
                self._warned_missing_server = True
                log.warning(
                    "ai.enabled is true but [ai.server].base_url is empty. "
                    "AI features are off until a server address is configured "
                    "(upgrade-from-old-config: set [ai.server] in config.toml)."
                )
            log.info("AIService started with AI off (disabled or no [ai.server] address)")
            return

        self._provider = self._build_server_provider()

        # Launch the reachability probe + periodic loops in the background
        # without awaiting, so a slow/hung server never delays startup. The
        # first probe flips is_ready() true on success (design 5.2).
        self._launch_background_probes()

        log.info("AIService started (provider=%s, kind=%s)",
                 type(self._provider).__name__, self._server_kind())

    def _build_server_provider(self) -> "OpenAIProvider":
        """Build the single thin-client OpenAIProvider from [ai.server].

        Reads base_url / model / api_key / timeout_s from the [ai.server]
        block. Only called from start() after _ai_off() has already returned
        False, so base_url is guaranteed non-empty here. The upgrade-from-
        old-config warning for a blank base_url is emitted in start() before
        the _ai_off() early return (design 5.4, finding wh-ay6h.6.3).
        """
        base_url = self._config.get("ai.server.base_url", "")
        return OpenAIProvider(
            api_key=self._config.get("ai.server.api_key", ""),
            model=self._config.get("ai.server.model", ""),
            base_url=base_url or "http://localhost:8781/v1",
            timeout_s=self._config.get("ai.server.timeout_s", 60),
        )

    async def stop(self) -> None:
        """Unload model (if applicable), close provider, stop TTS."""
        # Cancel the Phase A background probe / refresh loops if any are live,
        # then await them so they have fully unwound off the aiohttp session
        # before we call close() below. task.cancel() only *requests*
        # cancellation; without the gather the provider.close() call can race
        # with a loop still suspended inside await session.get(...).
        bg_tasks = list(self._bg_tasks)
        for task in bg_tasks:
            task.cancel()
        pending_exc: BaseException | None = None
        if bg_tasks:
            results = await asyncio.gather(*bg_tasks, return_exceptions=True)
            # Record (do not yet raise) the first unexpected BaseException so
            # test-suite HTTP-guard violations (BaseException subclasses) are
            # not silently discarded here. CancelledError is the normal outcome
            # for a freshly-cancelled task and is suppressed (finding wh-ay6h.21.2).
            # Re-raising HERE would skip the cleanup below and leave the service
            # half-stopped -- bg tasks still listed, provider session still open
            # (finding wh-ay6h.21.4). Finish the cleanup first, then re-raise at
            # the end.
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    pending_exc = result
                    # Only the first non-CancelledError BaseException is kept.
                    # For the HTTP-guard use case (one probe task raises
                    # _HttpBlockedError) this is sufficient.
                    #
                    # NOTE: Exception-level failures are handled *within* the
                    # loop handlers (_probe_loop, _refresh_loop) which log at
                    # WARNING and continue; they never reach this gather.
                    # BaseException failures terminate the task and arrive here;
                    # only the first is kept -- additional ones are silently
                    # discarded (no logging).
                    break
        self._bg_tasks = []

        try:
            if self._provider is not None:
                provider = self._provider
                self._provider = None  # clear before close so it's never left set
                try:
                    if hasattr(provider, "unload_model"):
                        await provider.unload_model()
                except Exception:
                    log.warning("provider.unload_model() raised during stop", exc_info=True)
                finally:
                    # close() runs even if unload_model() raised -- including a
                    # BaseException such as KeyboardInterrupt. _provider is
                    # already cleared to None above, so a partially torn-down
                    # provider would otherwise leak its aiohttp session with no
                    # reference left to close it (wh-ay6h.22.7, wh-ay6h.22.11).
                    # The finally guarantees close() WITHOUT swallowing a
                    # BaseException from unload_model(): that exception still
                    # propagates out after close() has run.
                    try:
                        await provider.close()
                    except Exception:
                        log.warning("provider.close() raised during stop", exc_info=True)
        finally:
            # speech.shutdown() always runs regardless of provider teardown
            # failures -- skipping it leaks the TTS ThreadPoolExecutor
            # (wh-ay6h.22.6). Its own failure is logged at WARNING to match the
            # provider-cleanup handling (wh-ay6h.22.12). Cleanup exceptions are
            # swallowed and logged, never chained onto the re-raised bg-task
            # exception, so pending_exc.__context__ is None by design
            # (wh-ay6h.22.10).
            try:
                await self._speech.shutdown()
            except Exception:
                log.warning("speech.shutdown() raised during stop", exc_info=True)
            finally:
                # "AIService stopped" is always logged (even if speech.shutdown
                # raised) as the signal that stop() ran to completion
                # (wh-ay6h.22.12). The recorded bg-task exception (the root
                # cause) is re-raised last, inside this finally, so a secondary
                # teardown failure cannot silently discard it (wh-ay6h.22.1).
                log.info("AIService stopped")
                if pending_exc is not None:
                    raise pending_exc

    # -- Thin-client coordinator API (design 5.2) --
    #
    # The old eager-load local-provider lifecycle (_create_provider, switch_model,
    # switch_provider, etc.) was deleted in Phase C (commit 43533716). Only the
    # thin-client coordinator path that reads the [ai.server] config block remains.

    def _ai_off(self) -> bool:
        """Single off predicate: AI disabled, server disabled, or no server address (design s4).

        Checks ai.server.enabled so the inference path matches the GUI state
        managed by state_manager._get_available_ai_providers (case b). When
        ai.server.enabled=false the service returns is_ready()=False and
        skips all background probes, consistent with the menu semantics
        (wh-ay6h.6.1).
        """
        if not self._config.get("ai.enabled", True):
            return True
        if not self._config.get("ai.server.enabled", True):
            return True
        return not self._config.get("ai.server.base_url", "")

    def _server_kind(self) -> str:
        """The configured server kind: 'local' (live model list) or 'cloud'."""
        return self._config.get("ai.server.kind", "local")

    def is_ready(self) -> bool:
        """True when AI is on and the last reachability check passed.

        The cached value is updated by recheck_ready() and, for a local
        server, by a successful refresh_models() -- it is not frozen after the
        one startup probe (design 5.2, reviewer_2 finding 3.3).
        """
        if self._ai_off():
            return False
        return self._ready

    def is_processing(self) -> bool:
        """True while the processing lock is held (a fix/help call is in flight)."""
        return self._processing_lock.locked()

    def cached_models(self) -> list[str]:
        """The model list from the most recent refresh_models() call (no network)."""
        return list(self._models)

    async def set_model(self, model_name: str) -> None:
        """Switch the selected model, serialized against an in-flight call.

        Acquires _processing_lock around the mutation so a model swap cannot
        tear state with a chat() that is mid-flight, and so the help-session
        reset cannot clear history under a chat_help coroutine that is
        mid-await (design 5.2, reviewer_0 finding 1.8). The critical section
        is fast (no model load), so it cannot deadlock the long fix/help path.
        """
        async with self._processing_lock:
            self._model_name = model_name
            if self._provider is not None and hasattr(self._provider, "_model"):
                self._provider._model = model_name
            if self._help_session:
                self._help_session.reset()
            log.info("AI model set to: %s", model_name)

    async def refresh_models(self) -> None:
        """Refresh the cached model list from the server (local kind only).

        Calls list_models() and updates the cache, the ok flag, and -- on
        success -- the cached readiness (a successful GET /models is itself a
        reachability signal). Local-only: a cloud address has no useful live
        list. This is a plain coroutine with its own per-iteration try/except
        in the loop; it is NOT wrapped in any shutdown-on-exception helper, so
        a transient refresh error never takes down voice control (design 5.2).
        """
        if self._server_kind() == "cloud":
            return  # Cloud has no live model list; local-only contract (design 5.2, wh-ay6h.7.1)
        if self._provider is None:
            return
        if not hasattr(self._provider, "list_models"):
            return
        models = await self._provider.list_models()
        if models:
            self._models = list(models)
            self._models_refreshed_at = asyncio.get_event_loop().time()
            self._last_refresh_ok = True
            # A non-empty model-list fetch corroborates readiness (design 5.2).
            self._ready = True
            return
        # An empty list is ambiguous: list_models() returns [] both for a
        # reachable server with no models and for ANY failure (transport
        # error, timeout, non-200), so it cannot corroborate readiness on its
        # own (reviewer_0 finding wh-ay6h.2.6: asserting ready here marked a
        # DOWN server ready on every refresh tick, defeating the is_ready()
        # gate). Disambiguate with the real probe; on an unreachable server
        # drop readiness and keep the last good cache.
        reachable = False
        if hasattr(self._provider, "is_available"):
            try:
                reachable = bool(await self._provider.is_available())
            except Exception:
                reachable = False
        self._last_refresh_ok = reachable
        self._ready = reachable
        if reachable:
            self._models = []
            self._models_refreshed_at = asyncio.get_event_loop().time()

    async def recheck_ready(self) -> bool:
        """Re-probe reachability, update the cached readiness, and return it.

        Runs a single is_available() check with the short probe timeout and
        updates the cached state is_ready() reads, then returns the fresh
        boolean so a transport-failure triage can re-probe through the
        coordinator instead of reaching into self._provider (design 5.2,
        reviewer_2 finding 3.2). When AI is off or no provider is present it
        is False with no network call.
        """
        if self._ai_off() or self._provider is None:
            self._ready = False
            return False
        if not hasattr(self._provider, "is_available"):
            self._ready = False
            return False
        # No outer wait_for here: is_available() already bounds each probe
        # individually with _PROBE_TIMEOUT_S (5 s per GET). It can issue two
        # sequential GETs (primary + /api/tags fallback), making the worst-case
        # runtime ~2 * _PROBE_TIMEOUT_S = 10 s. Wrapping with an equal 5 s
        # deadline would silently cut off the fallback probe on any slow-loris
        # primary endpoint (finding wh-ay6h.6.2). The 10 s worst case is well
        # within the goal of not stalling for the full chat timeout (60 s).
        try:
            fresh = await self._provider.is_available()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("recheck_ready probe failed: %s", e)
            fresh = False
        self._ready = bool(fresh)
        return self._ready

    def _launch_background_probes(self) -> None:
        """Launch the first reachability probe and the periodic loops.

        The first probe runs as a background task -- it is NOT awaited here, so
        a slow or hung server never delays app startup; is_ready() flips true
        when that probe succeeds. The periodic readiness loop runs for BOTH
        local and cloud kinds; the periodic model-list refresh runs only for a
        local kind (design 5.2, reviewer_2 findings 3.2/3.3). No-op when AI is
        off.
        """
        if self._ai_off():
            return
        loop = asyncio.get_event_loop()
        self._bg_tasks.append(loop.create_task(self.recheck_ready()))
        self._bg_tasks.append(loop.create_task(self._readiness_loop()))
        if self._server_kind() == "local":
            self._bg_tasks.append(loop.create_task(self._refresh_loop()))

    async def _readiness_loop(self, interval: float = _REFRESH_INTERVAL_S) -> None:
        """Periodic readiness re-probe for both local and cloud kinds.

        Per-iteration try/except logs and continues so a transient probe
        error never silently stops the loop (design 5.2). Not wrapped in any
        shutdown-on-exception helper.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await self.recheck_ready()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Periodic readiness probe failed: %s", e)

    async def _refresh_loop(self, interval: float = _REFRESH_INTERVAL_S) -> None:
        """Periodic model-list refresh loop (local kind only).

        Per-iteration try/except so a transient refresh error never takes down
        voice control and never silently stops the loop (design 5.2). Not
        wrapped in any shutdown-on-exception helper.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh_models()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Periodic model-list refresh failed: %s", e)

    # -- Public API --

    async def fix_text(self, text: str) -> ChatResult:
        """Correct formatting of STT output.

        Returns a ChatResult (design 5.1a Phase B): OK with the sanitized
        corrected text on success; CANCELLED when the caller set
        cancel_requested (either before or after the network call); EMPTY on
        empty input or no provider; otherwise the provider's non-OK status
        (TRANSPORT_ERROR, HTTP_ERROR, MODEL_NOT_FOUND, EMPTY) so the caller
        can show the section-7 notices. Callers read result.ok / result.text /
        result.status.

        Concurrency is managed by the caller (fix_text_ai action) via
        ``_processing_lock``.  This method must NOT acquire that lock
        itself to avoid non-reentrant deadlock.
        """
        if not text or not text.strip():
            return ChatResult(status=ChatStatus.EMPTY)

        if self._provider is None:
            log.warning("fix_text called with no provider loaded")
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)

        # Check cancellation before starting
        if self.cancel_requested:
            self.cancel_requested = False
            return ChatResult(status=ChatStatus.CANCELLED)

        messages = [
            {"role": "system", "content": TEXT_CORRECTION_SYSTEM},
            {"role": "user", "content": text},
        ]

        # Text correction output ~= input length.  Use generous multiplier
        # to account for thinking-model reasoning tokens that consume budget.
        word_count = len(text.split())
        max_tokens = max(1024, int(word_count * _WORDS_TO_TOKENS * 4))
        raw = await self._provider.chat(messages, max_tokens=max_tokens)
        result = _legacy_to_result(raw)

        # Check cancellation after AI response BEFORE surfacing any non-OK
        # result: a cancel that races a transport/HTTP error must consume the
        # flag and return CANCELLED; otherwise the flag leaks and silently
        # cancels the next fix_text() call.  chat_help() uses the same order.
        if self.cancel_requested:
            self.cancel_requested = False
            return ChatResult(status=ChatStatus.CANCELLED)

        if not result.ok:
            return result

        return ChatResult(
            status=ChatStatus.OK,
            text=self._sanitize_response(result.text),
            finish_reason=result.finish_reason,
            status_code=result.status_code,
        )

    async def ask_help(self, question: str) -> ChatResult:
        """Answer a WheelHouse help question (single-turn for now).

        Returns a ChatResult (design 5.1a Phase B). Missing knowledge base or
        no provider map to OK with an explanatory message / TRANSPORT_ERROR so
        the caller branches on result.ok / result.status.
        """
        if self._knowledge_base is None:
            return ChatResult(
                status=ChatStatus.OK,
                text="Knowledge base is not loaded. Help is unavailable.",
            )
        if self._provider is None:
            log.warning("ask_help called with no provider loaded")
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)

        system_prompt = HELP_SYSTEM_TEMPLATE.format(
            knowledge_base=self._knowledge_base
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        max_tokens = self._config.get("ai.help.max_response_tokens", 800)
        raw = await self._provider.chat(messages, max_tokens=max_tokens)
        return _legacy_to_result(raw)

    async def chat_help(self, history: list[dict]) -> Optional[str]:
        """Answer a help question with multi-turn conversation history.

        Acquires _processing_lock to serialize against a concurrent model
        selection (set_model also acquires this lock) so a swap cannot tear
        state mid-inference. We just send the prompt to the loaded provider.

        Args:
            history: All turns so far. Format:
                     [{"role": "user", "content": ...},
                      {"role": "assistant", "content": ...}, ...]
                     The last entry is the new question.

        Returns the model's response text, or None on failure/cancellation.

        The full ChatResult (including the finish_reason == "length"
        truncation signal) is stashed on self._last_help_result so help_ask()
        can surface .ok/.truncated to _handle_help_ask without this str-return
        path discarding it. The str return is preserved so HelpChatSession.ask
        -- which must not acquire the processing lock and must keep history in
        valid user/assistant pairs (deadlock decision 26) -- is unchanged
        (design 5.1a / s7).
        """
        if self._knowledge_base is None:
            self._last_help_result = ChatResult(status=ChatStatus.EMPTY)
            return None
        if self._provider is None:
            log.warning("chat_help called with no provider loaded")
            self._last_help_result = ChatResult(status=ChatStatus.TRANSPORT_ERROR)
            return None

        if self.cancel_requested:
            self.cancel_requested = False
            self._last_help_result = ChatResult(status=ChatStatus.EMPTY)
            return None

        async with self._processing_lock:
            system_prompt = HELP_CHAT_SYSTEM.format(
                knowledge_base=self._knowledge_base
            )
            messages = [
                {"role": "system", "content": system_prompt},
                *history,
            ]

            max_tokens = self._config.get("ai.help.max_response_tokens", 800)
            raw = await self._provider.chat(messages, max_tokens=max_tokens)
            result = _legacy_to_result(raw)
            self._last_help_result = result

            if self.cancel_requested:
                self.cancel_requested = False
                self._last_help_result = ChatResult(status=ChatStatus.EMPTY)
                return None

            if not result.ok:
                return None

            return result.text

    async def help_ask(self, question: str) -> ChatResult:
        """Public entry point for help chat questions. Delegates to
        HelpChatSession and returns a ChatResult (design 5.1a Phase B).

        HelpChatSession.ask returns the response text (preserving its
        no-lock / append-then-snapshot deadlock contract, decision 26); the
        structured ChatResult -- carrying the finish_reason == "length"
        truncation signal _handle_help_ask shows the 'model may be too small'
        hint on -- is read back from self._last_help_result, which chat_help
        stashed on the same call.

        Single-flight assumption (finding wh-ay6h.10.6): _last_help_result is
        only valid here because help questions are serialised through a single
        GUI window and the tray menu does not expose a concurrent-call path.
        If that assumption ever changes (e.g. a programmatic concurrent caller
        is added), _last_help_result must be replaced with a per-call return
        value from chat_help (change chat_help to return a tuple
        (Optional[str], ChatResult) and update HelpChatSession.ask to unpack
        it; decision 26 still holds because HelpChatSession.ask would only
        expose the str half to history management).
        """
        if not self._help_session:
            return ChatResult(status=ChatStatus.TRANSPORT_ERROR)
        text = await self._help_session.ask(question)
        result = self._last_help_result
        if text is None:
            # ask popped the question on a non-OK chat_help; surface the
            # stashed non-OK status (TRANSPORT_ERROR / HTTP_ERROR / EMPTY...).
            return result
        # OK path: prefer the text ask returned, carry the truncation signal.
        return ChatResult(
            status=ChatStatus.OK,
            text=text,
            finish_reason=result.finish_reason,
            status_code=result.status_code,
        )

    def help_reset(self) -> None:
        """Public entry point to reset help conversation history."""
        if self._help_session:
            self._help_session.reset()

    async def speak(self, text: str) -> None:
        """Speak text via TTS. Falls back to toast notification."""
        await self._speech.speak(text)

    async def speak_brief(self, text: str) -> None:
        """Speak a short status message (fire-and-forget)."""
        await self._speech.speak_brief(text)

    # -- Internal --

    def _load_knowledge_base(self) -> Optional[str]:
        """Read knowledge base file from disk."""
        kb_path = self._config.get("ai.knowledge_base", "")
        if not kb_path:
            return None
        try:
            path = Path(kb_path)
            if not path.is_absolute():
                path = Path(__file__).parent.parent / kb_path
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning("Knowledge base not found: %s", kb_path)
            return None
        except Exception as e:
            log.warning("Failed to load knowledge base: %s", e)
            return None

    def _sanitize_response(self, text: str) -> str:
        """Strip common LLM artifacts from response text."""
        # 1. Strip thinking tags
        text = _THINKING_TAG.sub("", text)

        # 2. Strip preambles (before fences -- preamble may precede fences)
        for pattern in _PREAMBLE_PATTERNS:
            text = pattern.sub("", text)

        # 3. Strip code fences
        fence_match = _CODE_FENCE.match(text.strip())
        if fence_match:
            text = fence_match.group(1)

        # 4. Strip trailing commentary
        text = _TRAILING_COMMENTARY.sub("", text)

        return text.strip()
