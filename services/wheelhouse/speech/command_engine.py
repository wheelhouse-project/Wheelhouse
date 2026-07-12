"""Command parsing engine with pattern matching and action execution.

This module implements the core pattern matching and execution engine for the
WheelHouse speech recognition system. It receives patterns from PatternCatalog
(the single source of truth) and executes matched actions.

Key Classes:
  - TextParser: Pattern matching and action execution engine.
  - ConfigurationError: Exception raised for configuration loading failures (deprecated).

Key Features:
  - Consumes patterns from PatternCatalog (no independent loading)
  - Supports both command patterns (^ anchor) and replacement patterns
  - Numeric parameter validation and extraction
  - Action function resolution and execution via ActionFunctions
  - UI action step identification and routing
  - Proper fullmatch vs search based on pattern type

Pattern Matching:
  - Patterns with ^ anchor: Use fullmatch (commands - full utterance required)
  - Patterns without ^ anchor: Use search (replacements - can match mid-utterance)
  - requires_hotword: Flag indicating if pattern needs hotword prefix
  - Numeric Validation: Parameter validation for numeric inputs (e.g., "delete 5")

Action Types:
  - UI Actions: Direct interface controls (hk, press, type_text, etc.)
  - System Actions: Application control and automation (run, activate, etc.)
  - Custom Functions: Extensible action system via ActionFunctions

Typical Usage:
  from speech.command_engine import TextParser
  from speech.pattern_catalog import PatternCatalog
  
  catalog = PatternCatalog("speech/config/patterns.toml")
  parser = TextParser(speech_handler, catalog)
  
  # Parse text against patterns
  matched = await parser.parse_and_execute("delete 3")
"""
import logging
import inspect

from utils.redact import redact_transcript
import asyncio
from typing import Optional, Any, Dict, List
from .actions import ActionFunctions, words_to_int
from .pattern_matcher import PatternMatcher

logger = logging.getLogger(__name__)

_UI_STEP_NAMES = {"hk", "press", "type_text", "insert_text", "activate", "wrap_or_insert", "transform_selection"}


class ConfigurationError(Exception):
    """Raised when configuration loading fails in a way that prevents operation."""
    pass


class TextParser:
    def __init__(self, speech_handler, pattern_catalog):
        """
        Initialize TextParser with patterns from PatternCatalog.
        
        Args:
            speech_handler: SpeechHandler instance for action execution context
            pattern_catalog: PatternCatalog instance containing loaded patterns
        """
        self.speech_handler = speech_handler
        self.pattern_catalog = pattern_catalog
        self.action_functions = ActionFunctions(speech_handler)
        self.matcher = PatternMatcher(pattern_catalog)

        # Get patterns from catalog (no file loading needed)
        self.patterns = self.pattern_catalog.get_all_patterns()
        # wh-med0: communicate the matched pattern's type back to the caller
        # so SpeechProcessor can distinguish a true command (irreversible
        # side effect, blocks retract) from a replacement (pure text
        # substitution, retractable). Reset to None at the top of every
        # parse_and_execute call so a stale value cannot leak across calls.
        self.last_executed_pattern_type: Optional[str] = None
        # wh-g2-refactor.18: the focus-redirect hook used by
        # replacement-insert routing was removed with the focus-redirect
        # path. Replacement inserts now dispatch through the standard
        # intelligent_insert_text IPC and the persistent hidden
        # dictation editor on the GUI side handles terminal-at-prompt
        # routing.
        logger.info(f"TextParser initialized with {len(self.patterns)} patterns from catalog")

    async def _execute_rule(
        self,
        match,
        steps_list,
        validation_group: Optional[str],
        pattern_type: Optional[str] = None,
    ):
        """
        Executes the steps for a matched rule.

        :flow: Command and Dictation Routing
        :step: 3
        :produces_for: UI Action Execution
        :description: Executes action sequence for matched command. Validates numeric parameters, looks up action functions from actions.py, and calls them with extracted parameters. May send UI commands via IPC or execute local operations.
        :data_in: re.Match object with captured groups from regex match, steps_list (action sequence from pattern)
        :data_out: Calls action functions (may trigger UI Action Execution flow via IPC)
        :notes: Orchestrates multi-step action execution: validates numeric params via words_to_int(),\n            resolves capture group parameters (g1, g2, g3) from regex match, calls action functions\n            from ActionFunctions registry, routes UI actions to Input Process via send_command() or\n            send_request() based on awaits_done flag. Handles async/sync action functions via\n            inspection. Stores string returns in context for chaining (e.g., capture_clipboard → gs).\n            Always clears skip_clipboard_restore flag in finally block to prevent state leakage.
        """
        try:
            # Optional numeric validation
            if validation_group:
                idx = int(validation_group[1:])
                if len(match.groups()) >= idx and match.group(idx) is not None:
                    if words_to_int(match.group(idx)) is None:
                        logger.warning("Validation failed for %s; rejecting.", match.re.pattern)
                        return False

            available = self.action_functions.get_functions()
            context: Dict[str, Any] = {f"g{i}": None for i in range(1, 10)}
            groups = match.groups()
            context.update({f"g{i+1}": group for i, group in enumerate(groups) if group is not None})

            prev_was_ui = False
            for step in steps_list:
                func_name = step.get("function")
                params = step.get("params", [])
                awaits_done = bool(step.get("awaits_done", False))

                # Resolve parameters: substitute g1, g2, g3, etc. with captured groups
                # For params like "g1", use dict lookup. For params like "(g1)" or "{g1}", use string replacement
                resolved = []
                for p in params:
                    if isinstance(p, str):
                        # If entire param is a capture group marker (e.g., "g1"), do direct lookup
                        if p in context:
                            resolved.append(context[p])
                        else:
                            # Otherwise, do string replacement for embedded markers (e.g., "(g1)" → "(hello)")
                            result = p
                            for key, value in context.items():
                                if value is not None and key in result:
                                    result = result.replace(key, value)
                            resolved.append(result)
                    else:
                        resolved.append(p)
                func = available.get(func_name)
                if not func:
                    logger.error("Function '%s' not found.", func_name)
                    continue

                result = func(*resolved)
                if inspect.isawaitable(result):
                    # Local async
                    await result
                    prev_was_ui = False
                elif isinstance(result, dict) and 'action' in result:
                    """:flow: UI Action Execution
                    :step: 1
                    :description: Routes UI action payload to IPC based on execution mode
                    :data_in: Dictionary payload from action function
                    :data_out: Payload routed to send_command or send_request
                    :consumes_from: Command and Dictation Routing
                    :branches_to: Step 2a (fire-and-forget), Step 2b (request-response)
                    :execution_context: main process (logic)
                    :execution_mode: conditional
                    :condition: If awaits_done=True → send_request (2b), else → send_command (2a)
                    :notes: Checks if action function returned dict with 'action' key. Branches based on
                    awaits_done flag: True means caller needs completion confirmation (request-response
                    pattern), False means fire-and-forget. Both paths enqueue to _outbound_q but
                    send_request creates Future for response tracking.
                    """
                    # When the replacement's step is producing text via
                    # intelligent_insert_text and the focus-redirect
                    # check says we are at a terminal prompt, open the
                    # dictation editor and send the text there instead
                    # of typing it into the terminal. Without this, a
                    # first-word replacement like "period" -> "." lands
                    # in the shell directly because the speech
                    # processor's DICTATE path never runs for a matched
                    # replacement.
                    if (
                        result.get('action') == 'intelligent_insert_text'
                        and isinstance(result.get('params'), dict)
                    ):
                        insertion_text = result['params'].get('insertion_string', '')
                        processor = getattr(
                            self.speech_handler, 'speech_processor', None,
                        )
                        if (
                            isinstance(insertion_text, str)
                            and insertion_text
                            and processor is not None
                        ):
                            try:
                                routed = await processor.maybe_route_to_editor(
                                    insertion_text,
                                )
                            except Exception:
                                logger.exception(
                                    "maybe_route_to_editor raised for "
                                    "replacement text; falling through to "
                                    "intelligent_insert_text"
                                )
                                routed = False
                            if routed:
                                prev_was_ui = True
                                continue

                    # UI-bound
                    if awaits_done:
                        await self.speech_handler.app.send_request(result['action'], result.get('params') or {})
                    else:
                        await self.speech_handler.app.send_command(result)
                        # [WORKAROUND PROPOSED] tiny debounce between two UI mutations that do not await DONE
                        if prev_was_ui and func_name in _UI_STEP_NAMES:
                            await asyncio.sleep(0.12)
                    prev_was_ui = True
                else:
                    # Synchronous local return (e.g., format_date, capture_clipboard)
                    prev_was_ui = False

                    # Store string returns in context for subsequent actions
                    # This allows functions like capture_clipboard to pass values
                    # to later actions (e.g., gs) via context substitution
                    if isinstance(result, str):
                        context[func_name] = result
                        logger.debug(f"Stored return value in context['{func_name}']")

                res_key = step.get("result")
                if res_key: context[res_key] = result
            return True
        except Exception as e:
            logger.error("Rule execution error: %s", e, exc_info=True)
            return False
        # NOTE: skip_clipboard_restore flag is cleared by UtteranceClipboardManager.end_utterance()
        # when the utterance completes. We do NOT clear it here because the utterance end
        # signal arrives AFTER command execution finishes. Clearing here would cause
        # clipboard restoration to overwrite what copy/cut commands just copied.

    async def parse_and_execute(
        self,
        text: str,
        return_remainder: bool = False,
        authorized_command: bool = False,
    ):
        """
        Parse text against patterns from catalog and execute matched actions.

        Uses PatternMatcher.match_single_pattern() for fullmatch vs search logic.

        :flow: Command and Dictation Routing
        :step: 2
        :produces_for: Command and Dictation Routing
        :description: Parses command/replacement candidates against regex patterns.
            Uses PatternMatcher for fullmatch (command) vs search (replacement) decision.
        :data_in: Buffer text emitted by SpeechProcessor finalization
        :data_out: Boolean indicating successful match, or tuple with remainder

        Args:
            text: Text to parse and execute patterns against
            return_remainder: If True, return (bool, str) tuple with unmatched text remainder
            authorized_command: True only when the caller has already vetted the
                buffer through the router's hotword gate. Remainder processing
                (SpeechProcessor._process_remainder) never sets this so a
                hotword-required command sitting in a replacement remainder
                cannot fire (wh-qj70s).

        Returns:
            If return_remainder=False: bool indicating if pattern matched
            If return_remainder=True: (bool, str) tuple with (matched, remainder)
        """
        logger.debug(f"[PARSE] Trying to match '{redact_transcript(text)}' against {len(self.patterns)} patterns")
        # wh-med0: clear any prior match type so a no-match call cannot
        # leave stale state for the next caller to read.
        self.last_executed_pattern_type = None

        for pattern_data in self.patterns:
            # Use PatternMatcher for fullmatch vs search decision
            result = self.matcher.match_single_pattern(
                text, pattern_data, authorized_command=authorized_command,
            )

            if result and result.matched:
                logger.debug(f"[PARSE] [OK] Matched pattern: {result.match_object.re.pattern}")
                exec_result = await self._execute_rule(
                    result.match_object,
                    result.actions,
                    result.validation_group,
                    pattern_type=result.pattern_type,
                )

                if exec_result:
                    # Record the matched pattern's type so the caller can
                    # distinguish a true command (blocks retract) from a
                    # replacement (retractable). result.pattern_type is
                    # 'command' or 'replacement' (wh-med0).
                    self.last_executed_pattern_type = result.pattern_type

                if return_remainder:
                    return exec_result, result.remainder
                return exec_result

        logger.warning(f"[PARSE] [FAIL] No pattern matched for '{redact_transcript(text)}'")
        if return_remainder:
            return False, text
        return False