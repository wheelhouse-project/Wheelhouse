"""Mutation gate for the screen-read dictation gate
(wh-overlay-slow-uia-stale-badges.7).

Run it from services/wheelhouse:

    python tests/mutation_gate_read_dictation_gate.py

The change adds LogicController.screen_read_in_flight_since (a monotonic
stamp spanning the Input round trips that run a UIA walk), a dictation
refusal in SpeechProcessor._send_to_dictation gated by that marker and a
10 s cap, the processor's sentence_closed_event, and
LogicController._wait_for_open_sentence_end (a bounded, abort-wakeable
hold on mid-sentence reads). Twelve of the tests were written red-first;
this gate supplies the red proof for the green-before guards (the cap,
the Mock-typing guard, the editor-path exemption, the wait bound) and
proves every catcher can see its defect.

Mutations, by target file:

speech/speech_processor.py:
  gate-condition-dropped   The refusal never fires; words type during a
                           read.
  gate-never-expires       The cap is removed; a stale marker refuses
                           forever. This is the red proof for
                           acceptance 4.
  gate-mock-guard-dropped  The isinstance guard becomes a None check; a
                           legacy MagicMock controller's auto-attribute
                           reaches float arithmetic and raises
                           TypeError. The expected catch IS that raise:
                           the guard exists precisely to prevent it.
  gate-before-editor-routing
                           A gate check is inserted BEFORE editor
                           routing; editor-bound words (which never
                           cross the Input command loop) get refused.
  sentence-open-dropped    A typed word no longer opens the sentence.
  end-utterance-close-dropped
                           The IDLE end-marker send no longer closes it.
  lifecycle-close-dropped  The lifecycle pair's end send no longer
                           closes it.
  pending-end-close-dropped
                           The deferred end send no longer closes it.
  start-close-dropped      A new utterance's start no longer closes a
                           stale sentence.

  pending-end-stale-guard-dropped
                           The deferred end closes the sentence even
                           for a STALE slot (finding .7.1.2): a read
                           could jump between two words of one
                           sentence.

main.py:
  sentence-wait-dropped    The build dispatch no longer waits for the
                           open sentence; the read jumps the queue.
  wait-bound-removed       The wait never gives up; the catcher's own
                           asyncio.wait_for trips TimeoutError. The
                           expected catch IS that raise: the bound
                           exists precisely to prevent the hang.
  wait-stale-check-dropped The wait no longer notices a machine that
                           left the build behind (finding .7.1.4); a
                           build queued past a hide sleeps out the
                           bound. The catcher's wait_for TimeoutError
                           IS the catch.
  stale-check-closed-dropped
                           Only the CLOSED half of the staleness
                           predicate is disabled; the queued-past-hide
                           catcher times out.
  stale-check-pair-dropped Only the pair-mismatch half is disabled;
                           the queued-past-supersede catcher times
                           out.
  marker-never-set         The overlay build send no longer marks the
                           round trip.
  marker-never-cleared     The overlay build finally no longer clears
                           the marker.
  click-marker-never-set   The by-name click send no longer marks the
                           round trip.
  click-marker-never-cleared
                           The by-name click finally no longer clears
                           the marker.
  count-not-incremented    _mark_screen_read_started no longer counts
                           (finding .7.1.3); the first finisher hides
                           an overlapping walk from the gate.
  finished-always-clears   _mark_screen_read_finished clears the stamp
                           while walks remain in flight.
  build-site-raw-set       The build site writes the stamp directly
                           instead of counting the walk in.
  click-site-raw-set       The click site writes the stamp directly
                           instead of counting the walk in.
  click-site-raw-clear     The click finally clears the stamp directly,
                           hiding a concurrent build walk.
  auto-open-ungated        Every build is treated as a walk again
                           (finding .7.1.9); the AUTO_OPEN snapshot
                           repaint waits on the open sentence and sets
                           the dictation-refusing marker. The skip-wait
                           catcher's own 0.8 s wait_for TimeoutError IS
                           its catch.

speech/command_engine.py:
  engine-gate-dropped      Text-insertion replacement payloads bypass
                           the gate again (finding .7.1.1).
  engine-clear-dropped     An engine text send no longer opens the
                           dictation sentence.
  engine-clear-before-gate The sentence event is cleared even for a
                           refused step.
  engine-gate-before-editor
                           A gate check is inserted BEFORE the editor
                           consult; editor-bound replacements (which
                           never cross the Input command loop) get
                           refused.
  rule-scan-dropped        The whole-rule pre-scan is disabled (finding
                           .7.1.6); a scope transform's awaited
                           selection hk queues behind the read before
                           the transform's per-payload gate can refuse.
  wrap-transform-not-classified
                           wrap_or_insert / transform_selection leave
                           the payload classification; an unclassified
                           wrap no longer opens the sentence.
  ai-transform-not-classified
                           fix_text_ai / rewrite_text_ai leave the
                           whole-rule classification (finding .7.1.8);
                           an AI transform's capture queues behind the
                           in-flight read.
  ff-restore-dropped       A fire-and-forget send the app refused
                           (False) no longer restores the sentence it
                           opened (finding .7.1.7).
  ff-restore-unconditional The restore fires even for a sentence an
                           earlier accepted word opened; the failed
                           step closes a sentence it did not open.
  awaited-restore-dropped  The awaited send's pre-enqueue failure no
                           longer restores the sentence.
  ff-serialization-restore-dropped
                           The fire-and-forget wrap re-raises the
                           serialization rejection without restoring
                           the sentence (finding .7.1.10 site 3).

speech/actions.py:
  go-fallback-raw-send     The cursor_navigate dictation fallback
                           reverts to the raw send_command (finding
                           .7.1.5), bypassing the gate, the editor
                           consult, and sentence tracking.
  go-fallback-flag-dropped The fallback no longer marks the parse as
                           dictation; a later STT revision could not
                           retract the typed words.
  replace-recheck-dropped  The AI transform's pre-paste re-check is
                           disabled (finding .7.1.8); a read that began
                           during the model await no longer suppresses
                           the replace_selected_text send.
  replace-recheck-inverted The re-check refuses every paste, read or
                           no read -- the red proof for the no-read
                           green-path guard test.

speech/speech_processor.py (finding .7.1.7 restore):
  dictation-restore-dropped
                           The dictation helper's pre-enqueue failure
                           no longer restores the sentence.
  dictation-restore-catches-timeout
                           The restore broadens to every exception; a
                           timed-out durable request that can still
                           deliver late would wrongly close the
                           sentence.

app.py (finding .7.1.10):
  serialization-not-delivery
                           IpcSerializationError loses its
                           IpcDeliveryError base; the pre-enqueue size
                           rejection stops being a proven never-sent
                           failure and all three restore sites let the
                           closed sentence stay lost.

module identity (finding .7.1.11 -- the launcher loads main top-level
while main.py builds WheelHouseApp from services.wheelhouse.app, so the
same app.py backs two module objects with distinct exception classes;
the restore sites must catch the package classes the production app
raises, and the restore tests raise the package classes to match):
  dictation-restore-top-level-identity
                           The dictation helper reverts to the
                           top-level `from app import`; production
                           package-class rejections stop matching.
  awaited-restore-top-level-identity
                           The awaited engine restore reverts to the
                           top-level import.
  ff-restore-top-level-identity
                           The fire-and-forget restore reverts to the
                           top-level import.

All must be caught. "Caught" means the expected test failed on its own
assertion -- except the mutations marked ``allow_raise``, where the
expected failure is the named exception itself (documented above).
Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutation that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any
ERROR line in the summary, and an expected test that failed for any
reason other than its own assertion (or its allowed raise).

The gate detects each target file's own line endings and translates the
patterns to them before matching. It never rewrites a file's endings.
"""

# --check validates patterns and Python syntax without collecting tests,
# recovering pending mutations, or writing target files.
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SP = SERVICE / "speech" / "speech_processor.py"
MAIN = SERVICE / "main.py"
CE = SERVICE / "speech" / "command_engine.py"
ACT = SERVICE / "speech" / "actions.py"
APP = SERVICE / "app.py"
TEST_FILES = ["tests/test_screen_read_dictation_gate.py"]

PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATIONS = [
    {
        "name": "gate-condition-dropped",
        "src": SP,
        "old": "        if self._screen_read_refuses_dictation():\n",
        "new": "        if False:\n",
        "expect": [
            "test_dictation_word_refused_while_a_read_is_in_flight",
            "test_refused_word_never_types_after_the_read_ends",
        ],
    },
    {
        # Red proof for acceptance 4: a gate without its cap refuses a
        # stale marker forever. The pattern names the cap_s local, not
        # _READ_GATE_MAX_REFUSAL_S: commit ac85fa50 gave the screen read
        # its own limit, so the constant is now the local's floor.
        "name": "gate-never-expires",
        "src": SP,
        # ac85fa50 gave the screen read its own configurable limit, so
        # the comparison now reads a local cap_s instead of the module
        # constant and the old anchor stopped matching. This mutation
        # reported pattern-not-found in every run since 2026-09-01. The
        # mutant is unchanged: the gate never expires.
        "old": "        return (time.monotonic() - since) < cap_s\n",
        "new": "        return True\n",
        "expect": ["test_gate_expires_after_its_maximum_wait"],
    },
    {
        # The guarded behavior is "a Mock auto-attribute must not reach
        # float arithmetic"; the TypeError is the defect itself, so a
        # raise IS the catch here.
        "name": "gate-mock-guard-dropped",
        "src": SP,
        "old": """        if not isinstance(since, (int, float)) or isinstance(since, bool):
            return False
""",
        "new": """        if since is None:
            return False
""",
        "expect": ["test_non_numeric_marker_value_dictates_unchanged"],
        "allow_raise": "TypeError",
    },
    {
        # Placement claim: the gate must sit AFTER editor routing. An
        # extra gate check before it refuses editor-bound words that
        # never cross the Input command loop.
        "name": "gate-before-editor-routing",
        "src": SP,
        # wh-spaced-punctuation-names-unresolved.3.1.2 (6a4ee76b) gave
        # _send_to_dictation a return value naming the surface that
        # received the text, so the editor arm returns "editor" instead
        # of returning bare and the old anchor stopped matching. The
        # mutant is unchanged: an extra gate check before editor
        # routing.
        "old": """        if await self.maybe_route_to_editor(text):
            return "editor"
""",
        "new": """        if self._screen_read_refuses_dictation():
            return None
        if await self.maybe_route_to_editor(text):
            return "editor"
""",
        "expect": ["test_editor_routed_words_pass_during_a_read"],
    },
    {
        "name": "sentence-open-dropped",
        "src": SP,
        "old": "        self.sentence_closed_event.clear()\n",
        "new": "        pass\n",
        "expect": [
            "test_sentence_opens_on_first_typed_word_and_closes_at_the_end_marker",
        ],
    },
    {
        "name": "end-utterance-close-dropped",
        "src": SP,
        # wh-spaced-punctuation-names-unresolved.3.1.2 (de2bb449) put
        # every end_utterance send behind _send_end_utterance, which
        # also records the words still held at that boundary. The
        # signal this mutation removes still goes out first.
        "old": """                self.sentence_closed_event.set()
                await self._send_end_utterance(word_event.utterance_id)
""",
        "new": """                await self._send_end_utterance(word_event.utterance_id)
""",
        "expect": [
            "test_sentence_opens_on_first_typed_word_and_closes_at_the_end_marker",
        ],
    },
    {
        "name": "lifecycle-close-dropped",
        "src": SP,
        # Same refactor as the entry above. The twelve-space indent is
        # what keeps this anchor apart from the sixteen-space one in
        # the end-marker branch.
        "old": """            self.sentence_closed_event.set()
            await self._send_end_utterance(word_event.utterance_id)
""",
        "new": """            await self._send_end_utterance(word_event.utterance_id)
""",
        "expect": ["test_the_lifecycle_pair_closes_the_sentence"],
    },
    {
        "name": "pending-end-close-dropped",
        "src": SP,
        "old": """            if self._pending_utterance_end == self._current_utterance_id:
                self.sentence_closed_event.set()
""",
        "new": """            if self._pending_utterance_end == self._current_utterance_id:
                pass
""",
        "expect": ["test_a_deferred_end_send_closes_the_sentence"],
    },
    {
        # Finding .7.1.2: without the id comparison, a stale slot's
        # deferred send closes the LIVE sentence mid-utterance.
        "name": "pending-end-stale-guard-dropped",
        "src": SP,
        "old": """            if self._pending_utterance_end == self._current_utterance_id:
                self.sentence_closed_event.set()
""",
        "new": """            self.sentence_closed_event.set()
""",
        "expect": [
            "test_a_stale_deferred_end_does_not_close_the_live_sentence",
        ],
    },
    {
        "name": "start-close-dropped",
        "src": SP,
        "old": """            # will clear the event again in _send_to_dictation.
            self.sentence_closed_event.set()
""",
        "new": """            # will clear the event again in _send_to_dictation.
            pass
""",
        "expect": ["test_a_new_utterance_start_closes_the_stale_sentence"],
    },
    {
        "name": "sentence-wait-dropped",
        "src": MAIN,
        "old": """            await self._wait_for_open_sentence_end(
                sid=sid, gen=gen, action=action, trace_id=trace_id,
            )
""",
        "new": """            pass
""",
        "expect": [
            "test_read_requested_mid_sentence_waits_for_the_sentence_end",
            "test_a_hide_during_the_sentence_wait_wakes_the_build",
        ],
    },
    {
        # The catcher's own asyncio.wait_for(timeout=2.0) trips; the
        # hang the bound prevents IS the defect, so the TimeoutError
        # raise is the catch.
        "name": "wait-bound-removed",
        "src": MAIN,
        "old": "                timeout=bound,\n",
        "new": "                timeout=None,\n",
        "expect": ["test_the_sentence_wait_gives_up_at_its_bound"],
        "allow_raise": "TimeoutError",
    },
    {
        "name": "marker-never-set",
        "src": MAIN,
        "old": """        if walks_input:
            self._mark_screen_read_started()
        try:
            await asyncio.wait(
""",
        "new": """        try:
            await asyncio.wait(
""",
        "expect": ["test_marker_set_during_the_round_trip_and_cleared_after"],
    },
    {
        "name": "marker-never-cleared",
        "src": MAIN,
        "old": """        finally:
            if walks_input:
                self._mark_screen_read_finished()
            self._overlay_build_abort = None
""",
        "new": """        finally:
            self._overlay_build_abort = None
""",
        "expect": [
            "test_marker_set_during_the_round_trip_and_cleared_after",
            "test_marker_cleared_when_the_send_times_out",
        ],
    },
    {
        "name": "click-marker-never-set",
        "src": MAIN,
        "old": """        self._mark_screen_read_started()
        try:
            raw = await self.app.send_request(
""",
        "new": """        try:
            raw = await self.app.send_request(
""",
        "expect": ["test_click_element_marker_spans_the_send"],
    },
    {
        "name": "click-marker-never-cleared",
        "src": MAIN,
        "old": """        finally:
            self._mark_screen_read_finished()

        from shared.click_element import (
""",
        "new": """        finally:
            pass

        from shared.click_element import (
""",
        "expect": [
            "test_click_element_marker_spans_the_send",
            "test_click_element_marker_cleared_on_timeout",
        ],
    },
    {
        # Finding .7.1.3: a start that does not count leaves the first
        # finisher free to clear the marker under an overlapping walk.
        "name": "count-not-incremented",
        "src": MAIN,
        "old": "        self._screen_reads_in_flight = count + 1\n",
        "new": "        self._screen_reads_in_flight = count\n",
        "expect": [
            "test_the_first_finisher_keeps_the_marker_until_the_last",
            "test_the_build_walk_counts_its_round_trip",
        ],
    },
    {
        "name": "finished-always-clears",
        "src": MAIN,
        "old": """        self._screen_reads_in_flight = max(count - 1, 0)
        if self._screen_reads_in_flight == 0:
            self.screen_read_in_flight_since = None
""",
        "new": """        self._screen_reads_in_flight = max(count - 1, 0)
        self.screen_read_in_flight_since = None
""",
        "expect": [
            "test_the_first_finisher_keeps_the_marker_until_the_last",
            "test_a_click_walk_finishing_keeps_a_build_walks_marker",
        ],
    },
    {
        # The SITE must count the walk in, not write the stamp raw.
        "name": "build-site-raw-set",
        "src": MAIN,
        "old": """        abort_wait = self.loop.create_task(abort.wait())
        if walks_input:
            self._mark_screen_read_started()
""",
        "new": """        abort_wait = self.loop.create_task(abort.wait())
        if walks_input:
            self.screen_read_in_flight_since = time.monotonic()
""",
        "expect": ["test_the_build_walk_counts_its_round_trip"],
    },
    {
        "name": "click-site-raw-set",
        "src": MAIN,
        "old": """        self._mark_screen_read_started()
        try:
            raw = await self.app.send_request(
""",
        "new": """        self.screen_read_in_flight_since = time.monotonic()
        try:
            raw = await self.app.send_request(
""",
        "expect": ["test_a_click_walk_finishing_keeps_a_build_walks_marker"],
    },
    {
        "name": "click-site-raw-clear",
        "src": MAIN,
        "old": """        finally:
            self._mark_screen_read_finished()

        from shared.click_element import (
""",
        "new": """        finally:
            self.screen_read_in_flight_since = None

        from shared.click_element import (
""",
        "expect": ["test_a_click_walk_finishing_keeps_a_build_walks_marker"],
    },
    {
        # Finding .7.1.4: without the pre-park staleness check, a build
        # queued past a hide sleeps out the full wait bound; the
        # catcher's own asyncio.wait_for(timeout=0.8) trips, and that
        # TimeoutError IS the defect the check prevents.
        "name": "wait-stale-check-dropped",
        "src": MAIN,
        "old": """        machine = getattr(self, "click_overlay_state", None)
        if machine is not None and (
            machine.state is OverlayState.CLOSED
            or (machine.overlay_session_id, machine.paint_generation)
            != (sid, gen)
        ):
""",
        "new": """        machine = getattr(self, "click_overlay_state", None)
        if False:
""",
        "expect": [
            "test_a_build_queued_past_a_hide_skips_the_sentence_wait",
            "test_a_build_queued_past_a_supersede_skips_the_sentence_wait",
        ],
        "allow_raise": "TimeoutError",
    },
    {
        "name": "stale-check-closed-dropped",
        "src": MAIN,
        "old": """        if machine is not None and (
            machine.state is OverlayState.CLOSED
            or (machine.overlay_session_id, machine.paint_generation)
""",
        "new": """        if machine is not None and (
            False
            or (machine.overlay_session_id, machine.paint_generation)
""",
        "expect": ["test_a_build_queued_past_a_hide_skips_the_sentence_wait"],
        "allow_raise": "TimeoutError",
    },
    {
        "name": "stale-check-pair-dropped",
        "src": MAIN,
        "old": """            machine.state is OverlayState.CLOSED
            or (machine.overlay_session_id, machine.paint_generation)
            != (sid, gen)
        ):
""",
        "new": """            machine.state is OverlayState.CLOSED
        ):
""",
        "expect": [
            "test_a_build_queued_past_a_supersede_skips_the_sentence_wait",
        ],
        "allow_raise": "TimeoutError",
    },
    {
        # Finding .7.1.1: without the engine gate, text-insertion
        # replacement payloads reach Input during a read.
        "name": "engine-gate-dropped",
        "src": CE,
        "old": "                        if callable(refuses) and refuses() is True:\n",
        "new": "                        if False:\n",
        # The .7.1.6 pre-scan now ALSO refuses type_text / insert_raw
        # rules (their function names are in
        # _FOREGROUND_TEXT_RULE_FUNCTIONS), so
        # test_type_text_and_raw_insert_are_refused_too no longer sees
        # this mutant -- the documented defense-in-depth masking
        # pattern. The layer-unique catchers use the "text" function
        # (an editor-routable intelligent-insert producer), which only
        # the per-payload gate handles.
        "expect": [
            "test_a_replacement_is_refused_while_a_read_is_in_flight",
            "test_a_refused_replacement_does_not_open_a_sentence",
        ],
    },
    {
        "name": "engine-clear-dropped",
        "src": CE,
        "old": """                        if isinstance(closed, asyncio.Event):
                            closed_event = closed
                            sentence_was_closed = closed.is_set()
                            closed.clear()
""",
        "new": """                        if isinstance(closed, asyncio.Event):
                            closed_event = closed
                            sentence_was_closed = closed.is_set()
""",
        "expect": ["test_a_replacement_types_when_no_read_is_in_flight"],
    },
    {
        # The clear must sit AFTER the refusal: a refused step opens
        # no sentence.
        "name": "engine-clear-before-gate",
        "src": CE,
        "old": "                        if callable(refuses) and refuses() is True:\n",
        "new": """                        closed = getattr(
                            processor, 'sentence_closed_event', None,
                        )
                        if isinstance(closed, asyncio.Event):
                            closed.clear()
                        if callable(refuses) and refuses() is True:
""",
        "expect": ["test_a_refused_replacement_does_not_open_a_sentence"],
    },
    {
        # Placement claim: the engine gate must sit AFTER the editor
        # consult. A gate check inserted before it refuses
        # editor-bound replacements, which never cross the Input loop.
        "name": "engine-gate-before-editor",
        "src": CE,
        "old": """                            try:
                                routed = await processor.maybe_route_to_editor(
""",
        "new": """                            refuses = getattr(
                                processor,
                                '_screen_read_refuses_dictation', None,
                            )
                            if callable(refuses) and refuses() is True:
                                return True
                            try:
                                routed = await processor.maybe_route_to_editor(
""",
        "expect": [
            "test_an_editor_routed_replacement_passes_during_a_read",
        ],
    },
    {
        # Finding .7.1.6: without the whole-rule pre-scan, a scope
        # transform's awaited selection hk queues behind the read
        # before the transform's own per-payload gate can refuse.
        "name": "rule-scan-dropped",
        "src": CE,
        "old": """            if any(
                step.get("function") in _FOREGROUND_TEXT_RULE_FUNCTIONS
                for step in steps_list
            ):
""",
        "new": """            if False:
""",
        "expect": [
            "test_a_scope_transform_is_refused_before_its_selection_hotkey",
        ],
    },
    {
        # Finding .7.1.6: wrap_or_insert / transform_selection dropped
        # from the payload classification. The pre-scan still refuses
        # them during a read, so the catcher is the green-path wrap
        # test: an unclassified wrap no longer opens the sentence.
        "name": "wrap-transform-not-classified",
        "src": CE,
        "old": """_TEXT_INSERTION_ACTIONS = frozenset(
    {
        "intelligent_insert_text", "type_text", "raw_insert_text",
        "wrap_or_insert", "transform_selection",
    }
)
""",
        "new": """_TEXT_INSERTION_ACTIONS = frozenset(
    {
        "intelligent_insert_text", "type_text", "raw_insert_text",
    }
)
""",
        "expect": ["test_a_wrap_types_when_no_read_is_in_flight"],
    },
    {
        # Finding .7.1.5: the cursor_navigate fallback reverts to the
        # raw send_command, bypassing the gate, the editor consult,
        # and sentence tracking.
        "name": "go-fallback-raw-send",
        "src": ACT,
        "old": """            try:
                await send(utterance)
""",
        "new": """            try:
                await self.speech_handler.app.send_command(
                    self.insert_text(utterance)
                )
""",
        "expect": [
            "test_an_unparseable_go_phrase_is_refused_while_a_read_is_in_flight",
            "test_an_unparseable_go_phrase_dictates_through_the_helper",
        ],
    },
    {
        # Finding .7.1.5: the fallback no longer marks the parse as
        # dictation; a later STT revision could not retract the words.
        "name": "go-fallback-flag-dropped",
        "src": ACT,
        "old": """            parser = getattr(processor, "text_parser", None)
            if parser is not None:
                parser.dictation_fallback_this_parse = True
""",
        "new": """            parser = getattr(processor, "text_parser", None)
            if parser is not None:
                pass
""",
        "expect": [
            "test_an_unparseable_go_phrase_dictates_through_the_helper",
        ],
    },
    {
        # Finding .7.1.7: a fire-and-forget send the app refused
        # (False) no longer restores the sentence it opened.
        "name": "ff-restore-dropped",
        "src": CE,
        "old": """                        if (
                            accepted is False
                            and sentence_was_closed
                            and closed_event is not None
                        ):
                            closed_event.set()
""",
        "new": """                        if (
                            False
                            and sentence_was_closed
                            and closed_event is not None
                        ):
                            closed_event.set()
""",
        "expect": [
            "test_a_failed_fire_and_forget_send_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.7: the restore fires even when the sentence was
        # ALREADY open from an earlier accepted word -- the failed step
        # then closes a sentence it did not open.
        "name": "ff-restore-unconditional",
        "src": CE,
        "old": """                        if (
                            accepted is False
                            and sentence_was_closed
                            and closed_event is not None
                        ):
                            closed_event.set()
""",
        "new": """                        if (
                            accepted is False
                            and closed_event is not None
                        ):
                            closed_event.set()
""",
        "expect": [
            "test_a_failed_send_keeps_a_sentence_an_earlier_word_opened",
        ],
    },
    {
        # Finding .7.1.7: the dictation helper's pre-enqueue failure
        # no longer restores the sentence.
        "name": "dictation-restore-dropped",
        "src": SP,
        "old": """        except IpcDeliveryError:
            if sentence_was_closed:
                self.sentence_closed_event.set()
            raise
""",
        "new": """        except IpcDeliveryError:
            raise
""",
        "expect": [
            "test_a_queue_full_dictation_send_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.7: the restore must stay limited to the proven
        # never-enqueued failure. Broadened to every exception, a
        # TIMEOUT restores too -- but a timed-out durable request can
        # still deliver late and type, so the sentence must stay open.
        "name": "dictation-restore-catches-timeout",
        "src": SP,
        "old": """        except IpcDeliveryError:
            if sentence_was_closed:
""",
        "new": """        except Exception:
            if sentence_was_closed:
""",
        "expect": [
            "test_a_timeout_keeps_the_sentence_conservatively_open",
        ],
    },
    {
        # Finding .7.1.7: the awaited engine send's pre-enqueue
        # failure no longer restores the sentence.
        # Anchored on the awaited branch's own comment tail because the
        # new fire-and-forget wrap (finding .7.1.10) carries an
        # otherwise byte-identical restore block.
        "name": "awaited-restore-dropped",
        "src": CE,
        "old": """                            # isinstance would not match.
                            from services.wheelhouse.app import (
                                IpcDeliveryError,
                            )
                            if (
                                sentence_was_closed
                                and closed_event is not None
                                and isinstance(exc, IpcDeliveryError)
                            ):
                                closed_event.set()
""",
        "new": """                            # isinstance would not match.
                            from services.wheelhouse.app import (
                                IpcDeliveryError,
                            )
                            if (
                                sentence_was_closed
                                and closed_event is not None
                                and False
                            ):
                                closed_event.set()
""",
        "expect": [
            "test_an_awaited_engine_send_queue_full_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.8: fix_text_ai / rewrite_text_ai leave the
        # whole-rule classification; an AI transform's capture queues
        # behind the in-flight read again.
        "name": "ai-transform-not-classified",
        "src": CE,
        "old": """_FOREGROUND_TEXT_RULE_FUNCTIONS = frozenset(
    {
        "literal", "type_text", "insert_raw",
        "wrap_or_insert", "transform_selection",
        "fix_text_ai", "rewrite_text_ai",
    }
)
""",
        "new": """_FOREGROUND_TEXT_RULE_FUNCTIONS = frozenset(
    {
        "literal", "type_text", "insert_raw",
        "wrap_or_insert", "transform_selection",
    }
)
""",
        "expect": [
            "test_an_ai_transform_rule_is_refused_before_its_capture",
        ],
    },
    {
        # Finding .7.1.8: the pre-paste re-check is disabled; a read
        # that began during the model await no longer suppresses the
        # replace_selected_text send.
        "name": "replace-recheck-dropped",
        "src": ACT,
        "old": """                    if callable(paste_refuses) and paste_refuses() is True:
""",
        "new": """                    if False:
""",
        "expect": [
            "test_a_read_during_the_model_await_suppresses_the_paste",
        ],
    },
    {
        # Finding .7.1.8: the re-check must refuse ONLY during a live
        # read. Inverted, it refuses every paste -- the red proof for
        # the green-before no-read guard test.
        "name": "replace-recheck-inverted",
        "src": ACT,
        "old": """                    if callable(paste_refuses) and paste_refuses() is True:
""",
        "new": """                    if callable(paste_refuses) and paste_refuses() is not True:
""",
        "expect": [
            "test_the_paste_still_lands_when_no_read_is_in_flight",
        ],
    },
    {
        # Finding .7.1.9: every build treated as a walk again. The
        # skip-wait catcher parks on the open sentence and trips its own
        # 0.8 s wait_for, so its expected failure is the TimeoutError.
        "name": "auto-open-ungated",
        "src": MAIN,
        "old": """        walks_input = action == "start_overlay_walk"
""",
        "new": """        walks_input = True
""",
        "expect": [
            "test_an_auto_open_repaint_does_not_set_the_marker",
            "test_an_auto_open_repaint_skips_the_sentence_wait",
        ],
        "allow_raise": "TimeoutError",
    },
    {
        # Finding .7.1.10: the rejection stops being a delivery error, so
        # all three restore sites let the closed sentence stay lost. The
        # fire-and-forget catcher fails too, for the same reason.
        "name": "serialization-not-delivery",
        "src": APP,
        "old": """class IpcSerializationError(IpcDeliveryError, ValueError):
""",
        "new": """class IpcSerializationError(ValueError):
""",
        "expect": [
            "test_the_oversize_rejection_is_a_never_enqueued_failure",
            "test_an_oversize_dictation_send_restores_the_closed_sentence",
            "test_an_oversize_awaited_engine_send_restores_the_closed_sentence",
            "test_an_oversize_fire_and_forget_send_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.10 site 3: the fire-and-forget wrap re-raises
        # without restoring. Anchored on the .7.1.10 comment's last line
        # because the awaited branch's except body is otherwise
        # byte-identical.
        "name": "ff-serialization-restore-dropped",
        "src": CE,
        "old": """                            # awaited branch above.
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
""",
        "new": """                            # awaited branch above.
                            raise
""",
        "expect": [
            "test_an_oversize_fire_and_forget_send_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.11: the dictation helper reverts to the
        # top-level `from app import` -- a second module object whose
        # classes the production (package-class) raises do not match.
        "name": "dictation-restore-top-level-identity",
        "src": SP,
        "old": """        from services.wheelhouse.app import IpcDeliveryError
""",
        "new": """        from app import IpcDeliveryError
""",
        "expect": [
            "test_a_queue_full_dictation_send_restores_the_closed_sentence",
            "test_an_oversize_dictation_send_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.11: the awaited engine restore reverts to the
        # top-level import.
        "name": "awaited-restore-top-level-identity",
        "src": CE,
        "old": """                            # isinstance would not match.
                            from services.wheelhouse.app import (
                                IpcDeliveryError,
                            )
""",
        "new": """                            # isinstance would not match.
                            from app import IpcDeliveryError
""",
        "expect": [
            "test_an_awaited_engine_send_queue_full_restores_the_closed_sentence",
            "test_an_oversize_awaited_engine_send_restores_the_closed_sentence",
        ],
    },
    {
        # Finding .7.1.11: the fire-and-forget restore reverts to the
        # top-level import.
        "name": "ff-restore-top-level-identity",
        "src": CE,
        "old": """                            # awaited branch above.
                            from services.wheelhouse.app import (
                                IpcDeliveryError,
                            )
""",
        "new": """                            # awaited branch above.
                            from app import IpcDeliveryError
""",
        "expect": [
            "test_an_oversize_fire_and_forget_send_restores_the_closed_sentence",
        ],
    },
]


def _pytest(*extra):
    return subprocess.run(
        ["uv", "run", "python", "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def collect_names():
    """Real test names in the file, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_acceptable_failure(reason, allow_raise):
    """True for an assertion failure, or the mutation's allowed raise."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    if allow_raise and reason.startswith(allow_raise):
        return True
    # asyncio.TimeoutError prints as "TimeoutError" on 3.11+, but be
    # tolerant of the qualified form.
    if allow_raise and reason.startswith(f"asyncio.{allow_raise}"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    """Every test name on a FAILED summary line, parameters stripped."""
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    """Every failure reason --tb=line printed, one per failing test."""
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [
        line for line in output.splitlines() if _ERROR_SUMMARY.match(line)
    ]


def check_only() -> int:
    """Validate every pattern and Python mutant without running or writing."""
    stale = ambiguous = broken = 0
    for mutation in MUTATIONS:
        path = mutation["src"]
        data = path.read_bytes()
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        old = mutation["old"].encode("utf-8").replace(b"\n", newline)
        new = mutation["new"].encode("utf-8").replace(b"\n", newline)
        count = data.count(old)
        if count == 0:
            stale += 1
            print(f"STALE {mutation['name']}: pattern not found in {path}")
            continue
        if count != 1:
            ambiguous += 1
            print(f"AMBIGUOUS {mutation['name']}: {count} matches in {path}")
            continue
        if path.suffix == ".py":
            try:
                compile(data.replace(old, new, 1), str(path), "exec")
            except SyntaxError as exc:
                broken += 1
                print(f"BROKEN {mutation['name']}: mutant does not compile: {exc}")
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{ambiguous} ambiguous, {broken} that do not compile"
    )
    return 1 if stale or ambiguous or broken else 0


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    originals = {path: path.read_bytes() for path in (SP, MAIN, CE, ACT, APP)}
    texts = {}
    for path, raw in originals.items():
        newline = "\r\n" if b"\r\n" in raw else "\n"
        texts[path] = (raw.decode("utf-8"), newline)
        print(
            f"line endings in {path.name}: "
            f"{'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
        )
    for mut in MUTATIONS:
        newline = texts[mut["src"]][1]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {', '.join(TEST_FILES)}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*TEST_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        src = mut["src"]
        text = texts[src][0]
        count = text.count(mut["old"])
        if count != 1:
            errors.append(
                f"{mut['name']}: pattern matched {count} times, expected 1"
            )
            print("ERROR", errors[-1])
            continue
        mutated = text.replace(mut["old"], mut["new"], 1)
        try:
            compile(mutated, str(src), "exec")
        except SyntaxError as exc:
            errors.append(
                f"{mut['name']}: mutated source does not compile: {exc}"
            )
            print("ERROR", errors[-1])
            continue
        src.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*TEST_FILES, *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            src.write_bytes(originals[src])
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            src.write_bytes(originals[src])
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; "
                f"no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
            continue
        allow = mut.get("allow_raise")
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_acceptable_failure(r, allow)]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
