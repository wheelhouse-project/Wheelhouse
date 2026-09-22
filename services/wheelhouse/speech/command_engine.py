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
import re

from utils.redact import redact_transcript
import asyncio
from typing import Optional, Any, Dict, List
from urllib.parse import quote_plus
from .actions import ActionFailed, ActionFunctions, words_to_int
from .pattern_matcher import PatternMatcher

logger = logging.getLogger(__name__)

_UI_STEP_NAMES = {"hk", "press", "type_text", "insert_text", "activate", "wrap_or_insert", "transform_selection"}

# wh-overlay-slow-uia-stale-badges.7.1.1: step payloads that type text
# into the foreground window. They cross Input's ONE command loop
# exactly like a dictated word, so the screen-read dictation gate
# covers them (see _execute_rule). Actions that do not type -- click,
# press, scroll -- are commands and stay ungated on purpose.
# wh-overlay-slow-uia-stale-badges.7.1.6 adds wrap_or_insert and
# transform_selection: both mutate foreground text in Input (a wrap
# pastes or inserts; a transform copies the selection and pastes the
# converted text back), so they are dictation-shaped for the gate even
# though their payload is not a plain insertion.
_TEXT_INSERTION_ACTIONS = frozenset(
    {
        "intelligent_insert_text", "type_text", "raw_insert_text",
        "wrap_or_insert", "transform_selection",
    }
)

# wh-overlay-slow-uia-stale-badges.7.1.6: step FUNCTION names whose
# payload mutates foreground text and can never route to the editor
# (the editor consult in _execute_rule applies only to
# intelligent_insert_text payloads). A rule containing one of these is
# classified BEFORE its first step runs: the scope transforms
# ("uppercase next two words") run an awaited hk selection step first,
# and refusing only at the transform's own dispatch would already have
# queued that hk behind the in-flight read -- by the time the transform
# step is evaluated the read marker may have cleared, and the transform
# would then mutate the post-read foreground. Editor-routable
# intelligent-insert producers (text, insert_text, insert_newlines,
# number_point) are deliberately NOT here: their gate runs per payload,
# after the editor consult, so the editor exemption survives.
# crewcut: a hand-edited multi-step pattern that puts an hk before an
# intelligent-insert step would run its hk before the per-payload gate
# refuses the insert. No shipped pattern has that shape (the only
# multi-step text rules, find and search-web-windows, use type_text,
# which is listed here). Removing the limit means classifying payloads
# without calling the step functions, which the engine cannot do today.
# wh-overlay-slow-uia-stale-badges.7.1.8 adds fix_text_ai and
# rewrite_text_ai: both capture the selection through Input (a Ctrl+C
# round trip) and paste the model's answer back, so the whole rule must
# be refused before the capture can queue behind an in-flight read. The
# paste has its own late re-check in _run_ai_text_transform, because a
# read can also begin during the model await.
_FOREGROUND_TEXT_RULE_FUNCTIONS = frozenset(
    {
        "literal", "type_text", "insert_raw",
        "wrap_or_insert", "transform_selection",
        "fix_text_ai", "rewrite_text_ai",
    }
)

# Actions whose substituted parameter values are URL-encoded (quote_plus,
# the same encoding gs applies to its query). Spoken text and stored
# results then arrive in the URL as data, never as URL structure, so the
# scheme and host must be written in the pattern's template (pattern-actions
# spec 5.1). The bare-marker lookup encodes too: a raw spoken URL passed as
# the whole parameter would otherwise slip past open_url's scheme check.
_URL_ENCODED_SUBSTITUTION_ACTIONS = frozenset({"open_url"})

# The scheme-and-authority region of a URL template: everything from the
# start of the string through the host (up to the first /, ?, #, or \
# after ://; browsers treat a backslash like a slash in http(s) URLs). A
# substitution marker inside this region would let spoken text or a
# stored result choose where the browser goes -- quote_plus leaves
# letters, digits, dots, and hyphens unchanged, so an encoded value is
# still a valid host (wh-open-url-action.1.1). Group 2 is the authority
# alone: an EMPTY authority means a separator run follows the scheme
# (https:///...), which browsers normalize before finding the host, so
# the template's written host is not where the browser goes
# (wh-open-url-action.1.8).
_URL_SCHEME_AUTHORITY_RE = re.compile(r"^([^:/?#]*://)([^/?#\\]*)")

# The capture-group keys _execute_rule seeds into every step context,
# whether or not the matched pattern defines that many groups.
_CAPTURE_KEYS = frozenset(f"g{i}" for i in range(1, 10))


def _note_replacement_surface(processor, surface, generation):
    """Tell the processor what a replacement's insertion step did.

    wh-spaced-punctuation-names-unresolved.3.1.6. A held punctuation
    name can be spelled half by an earlier utterance, and the processor
    has to know whether the mark this rule produced really reached the
    screen before it records those earlier words as retractable. Only
    the step itself knows: the editor arm is exempt from the screen-read
    gate, so a check made anywhere else can disagree with what ran.

    The call is optional in both directions. A processor that does not
    carry the method (every mock in the older suites) is left alone.
    """
    note = getattr(processor, 'note_replacement_text_delivered', None)
    if callable(note):
        note(surface, generation)


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
        # Whether the running speech engine applies a saved hint: True,
        # False, or None for unknown. Set by SpeechProcessor.apply_hint_engine
        # (wh-boost-engine-qualification); read by parse_and_execute.
        self.hint_engine: Optional[bool] = None

        # Get patterns from catalog (no file loading needed)
        self.patterns = self.pattern_catalog.get_all_patterns()
        # wh-med0: communicate the matched pattern's type back to the caller
        # so SpeechProcessor can distinguish a true command (irreversible
        # side effect, blocks retract) from a replacement (pure text
        # substitution, retractable). Reset to None at the top of every
        # parse_and_execute call so a stale value cannot leak across calls.
        self.last_executed_pattern_type: Optional[str] = None
        # wh-mouse-grid.1.27: set by the grid actions' dictation fallback
        # DURING rule execution (grid closed -> the utterance typed as
        # plain dictation via SpeechProcessor._send_to_dictation). Read
        # after execution: a parse that ended in the fallback records
        # 'dictation_fallback' instead of the pattern's own type, so
        # SpeechProcessor._execute_command does not treat the typed text
        # as an irreversible command -- which would block retraction when
        # STT later revises the words. Reset at the top of every
        # parse_and_execute call, like last_executed_pattern_type.
        self.dictation_fallback_this_parse: bool = False
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

            # wh-overlay-slow-uia-stale-badges.7.1.6: classify the WHOLE
            # rule before its first step runs. A rule that will mutate
            # foreground text through a non-editor-routable action is
            # refused up front while a screen read is in flight, so an
            # earlier step (the scope transforms' awaited selection hk,
            # find's ctrl+f) cannot queue behind the read first. The
            # per-payload gate below still covers the editor-routable
            # producers after their editor consult.
            if any(
                step.get("function") in _FOREGROUND_TEXT_RULE_FUNCTIONS
                for step in steps_list
            ):
                scan_processor = getattr(
                    self.speech_handler, 'speech_processor', None,
                )
                rule_refuses = getattr(
                    scan_processor, '_screen_read_refuses_dictation',
                    None,
                )
                if callable(rule_refuses) and rule_refuses() is True:
                    logger.info(
                        "RULE refused (screen read in flight): a step "
                        "would mutate foreground text; whole rule "
                        "dropped before its first step"
                    )
                    return True

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
                url_encode = func_name in _URL_ENCODED_SUBSTITUTION_ACTIONS
                resolved = []
                for p in params:
                    if isinstance(p, str):
                        # If entire param is a capture group marker (e.g., "g1"), do direct lookup
                        if p in context:
                            value = context[p]
                            if url_encode and value is not None:
                                value = quote_plus(str(value))
                            resolved.append(value)
                        else:
                            if url_encode:
                                # wh-open-url-action.1.1 / .1.3: the scheme
                                # and host must be fixed by the template,
                                # and every marker the template references
                                # must have a value. Checked against the
                                # original template, before replacement, so
                                # substituted text containing marker-like
                                # characters cannot re-trigger it.
                                # wh-open-url-action.1.4: g1..g9 exist in
                                # the context for every pattern; only the
                                # ones this pattern actually defines are
                                # references. Literal URL text that merely
                                # looks like a marker (a fixed /g1/ path
                                # segment) stays literal when the pattern
                                # has no such group -- nothing could ever
                                # substitute into it.
                                # wh-open-url-action.1.6: the exemption
                                # holds only while the slot still carries
                                # its seeded None. A step's result key can
                                # reuse a capture name and write real data
                                # into an undefined slot; the replacement
                                # loop would substitute it, so it must be
                                # checked like any other reference.
                                # wh-open-url-action.1.8: browsers strip
                                # tab/CR/LF anywhere in a URL and collapse
                                # separator runs after the scheme before
                                # finding the host, so a template using
                                # either form parses to a different host
                                # than the one written. Both are rejected
                                # outright. Template text is redacted in
                                # every message here because authored URLs
                                # can carry secrets and the engine logs
                                # these errors (wh-open-url-action.1.9).
                                if (
                                    "\t" in p or "\r" in p or "\n" in p
                                ):
                                    raise ValueError(
                                        "open_url: URL template contains a "
                                        "control character (tab, carriage "
                                        "return, or line feed); template "
                                        f"{redact_transcript(p)}"
                                    )
                                authority = _URL_SCHEME_AUTHORITY_RE.match(p)
                                if authority and authority.group(2) == "":
                                    raise ValueError(
                                        "open_url: separator run after the "
                                        "scheme (empty host); the address "
                                        "must be http:// or https:// "
                                        "followed directly by the site "
                                        f"name; template {redact_transcript(p)}"
                                    )
                                defined_captures = {
                                    f"g{i}"
                                    for i in range(1, min(len(groups), 9) + 1)
                                }
                                for key, value in context.items():
                                    if (
                                        key in _CAPTURE_KEYS
                                        and key not in defined_captures
                                        and value is None
                                    ):
                                        continue
                                    if authority and key in authority.group(0):
                                        raise ValueError(
                                            f"open_url: substitution marker {key!r} "
                                            "in the scheme or host of template "
                                            f"{redact_transcript(p)}; the scheme and "
                                            "host must be written in the template"
                                        )
                                    if value is None and key in p:
                                        raise ValueError(
                                            f"open_url: unresolved marker {key!r} in "
                                            f"URL template {redact_transcript(p)}; "
                                            "refusing to open a URL with missing data"
                                        )
                            # Otherwise, do string replacement for embedded markers (e.g., "(g1)" → "(hello)")
                            result = p
                            for key, value in context.items():
                                if value is not None and key in result:
                                    replacement = (
                                        quote_plus(str(value)) if url_encode else value
                                    )
                                    result = result.replace(key, replacement)
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
                    result = await result
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
                            # wh-spaced-punctuation-names-unresolved
                            # .3.1.5: read the app's start_utterance
                            # count immediately before the enqueue this
                            # route performs, so a promotion below
                            # names the span the text really lands in.
                            editor_generation = getattr(
                                processor, '_read_start_utterance_count', None,
                            )
                            editor_generation = (
                                editor_generation()
                                if callable(editor_generation) else None
                            )
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
                                # .3.1.6: the editor really took the
                                # text, and this arm never consults the
                                # screen-read gate below, so only the
                                # step itself can say so.
                                _note_replacement_surface(
                                    processor, "editor", editor_generation,
                                )
                                prev_was_ui = True
                                continue

                    # wh-overlay-slow-uia-stale-badges.7.1.1: a step
                    # that produces foreground text is dictation in
                    # command clothing -- the payload crosses Input's
                    # ONE command loop exactly like a dictated word
                    # and would queue behind an in-flight screen
                    # read, typing seconds late into whatever has
                    # focus by then. Consult the same gate
                    # _send_to_dictation uses, AFTER the editor
                    # consult above so the editor path keeps its
                    # exemption (an editor insert never crosses the
                    # Input loop). A refusal consumes the WHOLE rule
                    # (return True): running the remaining steps
                    # without their text would execute half a
                    # command, and returning False would hand the
                    # words back to dictation, retyping what the
                    # gate just refused. The identity check
                    # ("is True") keeps legacy MagicMock fixtures
                    # inert, matching the gate's own isinstance
                    # guard.
                    closed_event = None
                    sentence_was_closed = False
                    insert_processor = None
                    insert_generation = None
                    if result.get('action') in _TEXT_INSERTION_ACTIONS:
                        processor = getattr(
                            self.speech_handler, 'speech_processor', None,
                        )
                        refuses = getattr(
                            processor, '_screen_read_refuses_dictation',
                            None,
                        )
                        if callable(refuses) and refuses() is True:
                            logger.info(
                                "REPLACEMENT refused (screen read in "
                                "flight): action=%s dropped, rule "
                                "consumed",
                                result.get('action'),
                            )
                            # .3.1.6: nothing was typed, so a held name
                            # that produced this text records nothing.
                            _note_replacement_surface(processor, None, None)
                            return True
                        # The step is about to type: it opens a
                        # dictation sentence exactly as
                        # _send_to_dictation does, so a read
                        # requested during a replacement-only
                        # utterance waits for the utterance end
                        # (wh-overlay-slow-uia-stale-badges.7).
                        # wh-overlay-slow-uia-stale-badges.7.1.7:
                        # remember whether the sentence was closed so
                        # a send the app provably never accepted can
                        # restore it below -- a never-enqueued payload
                        # leaves no Input work that could ever produce
                        # the end_utterance that closes the sentence.
                        closed = getattr(
                            processor, 'sentence_closed_event', None,
                        )
                        if isinstance(closed, asyncio.Event):
                            closed_event = closed
                            sentence_was_closed = closed.is_set()
                            closed.clear()
                        # .3.1.5: the count read immediately before the
                        # send below, with nothing awaited in between.
                        insert_processor = processor
                        read_count = getattr(
                            processor, '_read_start_utterance_count', None,
                        )
                        insert_generation = (
                            read_count() if callable(read_count) else None
                        )

                    # UI-bound
                    if awaits_done:
                        # Only an ABSENT params key defaults to {}
                        # (wh-overlay-slow-uia-stale-badges.14.25): a
                        # supplied falsy non-mapping travels unchanged
                        # so the input-side gate is the one validator.
                        try:
                            await self.speech_handler.app.send_request(
                                result['action'],
                                result['params'] if 'params' in result else {},
                            )
                        except Exception as exc:
                            # wh-overlay-slow-uia-stale-badges.7.1.7:
                            # only the pre-enqueue delivery failure
                            # (outbound queue full) proves nothing
                            # reached Input. A timeout does NOT: the
                            # durable request may still deliver late
                            # and type, so the sentence stays
                            # conservatively open. The re-raise keeps
                            # the existing rule-abandon behavior.
                            # wh-overlay-slow-uia-stale-badges.7.1.11:
                            # the package path -- the production app is
                            # built from services.wheelhouse.app, and a
                            # top-level `from app import` binds a second
                            # module object whose classes this
                            # isinstance would not match.
                            from services.wheelhouse.app import (
                                IpcDeliveryError,
                            )
                            if (
                                sentence_was_closed
                                and closed_event is not None
                                and isinstance(exc, IpcDeliveryError)
                            ):
                                closed_event.set()
                            raise
                        # .3.1.6: the send returned without raising, so
                        # the text is on its way to the same span a
                        # correction of this utterance retracts.
                        if insert_processor is not None:
                            _note_replacement_surface(
                                insert_processor, "legacy", insert_generation,
                            )
                    else:
                        try:
                            accepted = (
                                await self.speech_handler.app.send_command(
                                    result
                                )
                            )
                        except Exception as exc:
                            # wh-overlay-slow-uia-stale-badges.7.1.10:
                            # the pre-enqueue serialization rejection
                            # RAISES out of send_command instead of
                            # returning False, so the restore below
                            # never saw it. It is a proven never-sent
                            # failure (the size check runs before any
                            # enqueue), so the sentence it closed is
                            # restored; the re-raise keeps the existing
                            # rule-abandon behavior.
                            # wh-overlay-slow-uia-stale-badges.7.1.11:
                            # the package path, same reason as the
                            # awaited branch above.
                            from services.wheelhouse.app import (
                                IpcDeliveryError,
                            )
                            if (
                                sentence_was_closed
                                and closed_event is not None
                                and isinstance(exc, IpcDeliveryError)
                            ):
                                closed_event.set()
                            raise
                        # wh-overlay-slow-uia-stale-badges.7.1.7: False
                        # is the documented not-accepted result
                        # (outbound queue full; the payload was never
                        # enqueued). The identity check keeps mock apps
                        # whose send_command returns a MagicMock inert.
                        if (
                            accepted is False
                            and sentence_was_closed
                            and closed_event is not None
                        ):
                            closed_event.set()
                        # .3.1.6: same report as the awaited arm, minus
                        # the payloads the queue refused outright.
                        if insert_processor is not None and accepted is not False:
                            _note_replacement_surface(
                                insert_processor, "legacy", insert_generation,
                            )
                        # [WORKAROUND PROPOSED] tiny debounce between two UI mutations that do not await DONE
                        if prev_was_ui and func_name in _UI_STEP_NAMES:
                            await asyncio.sleep(0.12)
                    prev_was_ui = True
                else:
                    # Synchronous local return (e.g., format_date, capture_clipboard)
                    prev_was_ui = False

                # Store string returns in context for subsequent actions.
                # Async result-producing actions (run_capture) use this same
                # path as synchronous capture_clipboard and format_date.
                if isinstance(result, str):
                    context[func_name] = result
                    logger.debug(f"Stored return value in context['{func_name}']")

                res_key = step.get("result")
                if res_key: context[res_key] = result
            return True
        except ActionFailed as e:
            # wh-arrow-key-names-missing: an action reported that it could
            # not run. This is an ordinary outcome of a misheard key name,
            # not a defect, so it is logged without a traceback. Returning
            # False makes parse_and_execute report no match, and
            # SpeechProcessor._execute_command then sends the spoken words
            # to dictation instead of consuming them.
            # crewcut: a rule whose FAILING step is not its first one keeps
            # whatever the earlier steps already did, and the speech
            # processor then dictates the whole utterance on top of that.
            # Every shipped pattern that calls press_keys has exactly one
            # step, so no shipped command can reach that state; a
            # hand-edited multi-step pattern could. Removing the limit
            # means recording each step's side effects and undoing them
            # here, which needs an undo path the action functions do not
            # have yet.
            logger.info("Rule abandoned: %s", e)
            return False
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

        A pattern whose actions save a hint is refused while
        ``self.hint_engine`` is False; SpeechProcessor.apply_hint_engine
        sets it to the value the router uses, so both layers refuse the
        same pattern (wh-boost-engine-qualification).

        Returns:
            If return_remainder=False: bool indicating if pattern matched
            If return_remainder=True: (bool, str) tuple with (matched, remainder)
        """
        logger.debug(f"[PARSE] Trying to match '{redact_transcript(text)}' against {len(self.patterns)} patterns")
        # wh-med0: clear any prior match type so a no-match call cannot
        # leave stale state for the next caller to read. The fallback
        # flag resets with it: a stale True would mark the NEXT real
        # command as retractable dictation (wh-mouse-grid.1.27).
        self.last_executed_pattern_type = None
        self.dictation_fallback_this_parse = False

        for pattern_data in self.patterns:
            # Use PatternMatcher for fullmatch vs search decision
            result = self.matcher.match_single_pattern(
                text, pattern_data, authorized_command=authorized_command,
                hint_engine=self.hint_engine,
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
                    # 'command' or 'replacement' (wh-med0). A grid word
                    # that fell back to dictation during execution (grid
                    # closed) records 'dictation_fallback' instead: the
                    # typed words are ordinary dictation and must stay
                    # retractable (wh-mouse-grid.1.27).
                    if self.dictation_fallback_this_parse:
                        self.last_executed_pattern_type = (
                            "dictation_fallback"
                        )
                    else:
                        self.last_executed_pattern_type = (
                            result.pattern_type
                        )

                if return_remainder:
                    return exec_result, result.remainder
                return exec_result

        logger.warning(f"[PARSE] [FAIL] No pattern matched for '{redact_transcript(text)}'")
        if return_remainder:
            return False, text
        return False
