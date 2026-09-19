r"""Mutation gate for the word-boundary literal body (Stage A, criterion A5)
and the prefix hold that carries a punctuation name across a pause
(Stage B, criterion C5). Bead wh-spaced-punctuation-names-unresolved.

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_punctuation_name_body.py
    .venv/Scripts/python.exe tests/mutation_gate_punctuation_name_body.py --check
    .venv/Scripts/python.exe tests/mutation_gate_punctuation_name_body.py --only <name>
    .venv/Scripts/python.exe tests/mutation_gate_punctuation_name_body.py --only=<name>

Stage A proves that tests/test_pattern_transform_boundary_body.py catches a
defect in either half of the stage A change: the extractor in
``extract_full_literal_body`` (speech/pattern_transform.py) AND the store that
puts its result in the catalog (speech/pattern_catalog.py). Both files are
here because the criterion names both, and because a gate over the extractor
alone could not tell a working extractor whose result the catalog drops from a
working one.

Stage B proves that the four punctuation-name test files catch a defect in
the hold that carries an exact name opening across an utterance boundary
(speech/speech_processor.py and speech/router.py). Its catcher tests live in
files OTHER than the Stage A one, so every mutation carries its own
``selection`` -- a list of pytest node ids that the run, the baseline and the
expected-catcher-name collection all honour. The nine Stage A entries omit the
key and get ``STAGE_A_SELECTION``.

  the-boundary-shape-is-refused
        The criterion's own mutation: the ``\b...\b`` arm of the shape test
        is disabled, which restores the shipped defect exactly. Every
        punctuation NAME in patterns.toml is written between word
        boundaries, so with this arm gone no name has a body, the catalog
        stores no ``literal_body_matchers``, and ``can_continue`` cannot
        hold a buffer part-way through a name.
  the-boundary-strip-leaves-the-boundary
        The strip takes one character off each end instead of two, so the
        body keeps the ``b`` of each ``\b``. An off-by-one that a "did it
        return something" test would miss.
  the-boundary-strip-eats-the-first-letter
        Three characters off each end. The other direction of the same
        off-by-one.
  a-half-bounded-pattern-is-accepted
        The two boundary tests are joined with ``or`` instead of ``and``,
        so a pattern bounded at ONE end is treated as a whole body and
        strips two characters off an end that carries no boundary.
  the-greedy-tail-refusal-is-dropped
        The greedy-tail guard stops working. The body extractor and
        ``extract_literal_prefix`` must stay disjoint: a greedy pattern
        belongs to the prefix extractor and its result rides the router's
        greedy timer, so a pattern that got both would be timed as greedy
        AND held as a literal.
  the-anchored-shape-is-refused
        The ``^...$`` arm is disabled instead. It is here because the two
        arms now sit on adjacent lines: an edit that merges or reorders
        them must not silently drop the shape that always worked.
  the-neither-end-refusal-is-dropped
        The refusal of a pattern anchored at NEITHER end stops working,
        so an unanchored, unbounded pattern reaches the else branch and
        ``p[2:-2]`` eats four characters off a plain phrase. It is here
        because ``NEITHER_END`` and ``NOT_BOTH`` were declared and then
        expected by no mutation, which meant nothing required either
        test to catch anything.
  the-prefix-extractor-claims-a-name-with-no-greedy-tail
        ``extract_literal_prefix`` returns the remaining text instead of
        ``""`` when it finds no greedy tail. This is the mutation that
        ``NOT_BOTH`` can see, and it is aimed at the real reason the two
        extractors stay disjoint: a bounded name has no greedy tail, so
        that early return is what declines it -- not the boundary shape.
        Disabling the guard instead would leave ``spans`` empty and then
        index ``spans[0][0]``, and the mutant would die on an
        ``IndexError`` upstream of the assertion, reading as caught while
        proving nothing.
  the-catalog-does-not-store-the-body-matchers
        speech/pattern_catalog.py stops writing ``literal_body_matchers``.
        Exactly ONE test in the file sees this, and that was measured
        rather than predicted: ``can_continue`` keeps working, because
        ``_buffer_opens_literal_prefix`` falls back to calling the
        extractor at match time when the catalog stored nothing. The
        store is a load-time cache, not the correctness path -- so
        ``test_every_one_of_them_has_body_matchers`` is the only guard
        against it, which is the reason that test earns its place.

The six Stage B mutations, against speech/speech_processor.py and
speech/router.py (criterion C5 names five subjects; the sixth pins the
ruling the hold rests on):

  the-release-deadline-is-longer-than-its-bound
        C5's "hold length". ``_arm_replacement_prefix_release`` is the
        ONE arming statement left in the file, and it arms one
        ``replacement_timeout_ms`` timer for the whole hold. Multiplying
        the bound by 20 lets a hold outlive the deadline C1 sets, so
        expiry never types the words and the two timer-spy tests see the
        wrong duration.
  a-word-that-cannot-grow-into-a-name-is-called-incomplete
        C5's "prefix exactness". ``is_incomplete_replacement_name``
        (speech/router.py) stops asking ``cannot_match`` and answers
        True for every word list that is not already a complete
        replacement. Every ordinary sentence then looks like an
        unfinished name, which is exactly the over-broad hold C2
        forbids.
  the-whole-utterance-half-of-the-hold-gate-is-dropped
        C5's "committed-text guard". The end_of_utterance arm of
        ``_should_hold_replacement_prefix`` keeps only the name test and
        loses ``_payload_is_the_whole_utterance``, so the TAIL of an
        ordinary sentence -- "I said back space" -- is held as if the
        user had begun a name.
  the-expiry-drops-the-held-words-instead-of-typing-them
        C5's "expiry path". The release deadline clears the slot instead
        of calling ``_flush_pending_replacement_prefix_as_dictation``,
        so an expired hold loses its words rather than typing them
        (C3's "on mismatch or expiry the literal words are typed
        unchanged").
  the-lifecycle-reset-no-longer-clears-the-hold
        C5's "context/provider clear". The lifecycle-reset branch's
        ``await self._consume_held_tail_at_utterance_end()`` is the
        clear: that marker is what a Mode-1 provider disagreement queues
        (integrations/websocket_manager.py:499, the only producer of
        ``is_lifecycle_reset_marker`` -- a recursive grep for that
        identifier over ``services/wheelhouse/**/*.py`` outside tests/
        returned seven lines: that one assignment, the dataclass field
        at speech/word_event.py:71, and five reads in
        speech/speech_processor.py), and the call is the one statement
        that empties the slot before the end_utterance /
        start_utterance pair. Skipping it lets a hold survive the change
        and land in phrase 2.
  a-completed-name-fires-before-its-utterance-ends
        The ruling the stage rests on: a completed name commits its
        mark only when the completing word ENDS its utterance. Dropping
        the ``if not word_event.end_of_utterance:`` hold makes a
        completed pair fire the instant the completing word arrives, so
        the release deadline can never fire with a complete name held --
        and on a provider that keeps one utterance open across the
        pause, that deadline is the only thing that ever delivers the
        mark. The catcher is therefore the deadline test, and the
        assertion that catches it is the one taken BEFORE the deadline.
  the-new-utterance-guard-is-dropped
        The shipped line was
        ``if not word_event.word or word_event.start_of_utterance:``.
        This branch splits it, and this mutation disables the
        new-utterance half entirely, so the previous utterance's held
        words fuse with the next utterance's first word whatever either
        one is.
  the-new-utterance-never-fires-a-complete-name
        The first thing the guard does is fire a held COMPLETE name,
        because a new utterance's first word is proof the previous
        utterance ended and its own end marker can be lost. Dropping
        that call sends the complete name to the flush below instead,
        so the user gets the words they said the name with rather than
        the mark.
  the-cross-utterance-join-ignores-where-the-hold-armed
        The join is allowed only for a hold armed ACROSS a confirmed
        utterance end. Dropping that half lets a hold the timeout
        sentinel armed in the MIDDLE of an utterance be completed by
        the next utterance's first word, which is the fusion
        tests/e2e/test_e2e_utterance_end_replacement.py refuses.
  the-tentative-record-is-never-set
        A join made by a word that OPENED its utterance is tentative:
        if that utterance keeps going, its later words prove it is
        ordinary dictation and the hold flushes. The record is what
        carries that, and on the shipped remote path it is the only
        thing that can, because no real word there carries
        ``end_of_utterance``. Forcing the record to False makes
        "question" then "mark my words" insert a mark.
  the-held-words-need-not-be-an-unfinished-name
        The tentative branch flushes only when the held words ALREADY
        spell a whole name. Dropping that condition -- which is what
        the branch looked like before
        wh-spaced-punctuation-names-unresolved.3.1.3 -- makes it flush
        on the very word that FINISHES a three-word name, so every
        three-word name in the catalog types its words when the pause
        falls after the first one.
  the-lifecycle-reset-keeps-the-prefix-it-armed
        A lifecycle reset drains the hold it finds on entry, but its
        own buffer finalization can arm a NEW one, and
        _consume_finalization_armed_tail leaves the replacement kind
        alone for the end-marker path's sake. Passing False for
        drain_replacement_prefix restores that defect: phrase 1's
        "open" survives the end/start pair and phrase 2's "bracket"
        completes it into "[".
  the-corrected-final-drops-the-earlier-utterances-words
        A successful retract removes the whole credited span, and a
        hold delivered after its own utterance's end_utterance is
        inside that span. Dropping the restore leaves the correction
        replaying only the new utterance's final, which is the data
        loss the finding names.
  the-boundary-record-is-never-taken
        The same loss reached one step earlier: the record must be
        taken where the Input paste counter resets, because that is
        where ownership changes and the only moment the hold still
        holds exactly the earlier utterance's words.
  the-record-outlives-the-span-it-describes
        The opposite failure. A record kept past its own
        end_utterance restores words that are already on screen and
        can never be retracted again, so a later correction types
        them a second time.
  the-tentative-record-is-cleared-as-the-hold-grows
        A hold that is already tentative stays tentative while it
        grows. Reading only ``word_event.start_of_utterance`` there
        clears the record on the word that extended the name, so a name
        finished across the boundary then absorbs the ordinary words
        after it: "open" then "single quote please" inserts the mark
        and swallows "please".

  These four replaced two entries that went stale when
  wh-spaced-punctuation-names-unresolved.3.1 rewrote the guard. The
  old pair mutated an ``and not (word_event.end_of_utterance and ...)``
  condition that no longer exists. A fifth was written and dropped as
  an equivalent mutant; the comment above that group in MUTATIONS
  records the measurement and the reachability argument.

All thirty-four must be caught. Pattern-not-found, an ambiguous pattern, a
mutation that does not compile, a per-mutation timeout, a suite-timeout
abort, and an expected catcher that SKIPPED are reported as errors, never as
a verdict. ``--check`` verifies every selected pattern matches exactly once
in the current source and that every mutant compiles, without running any
test; it is NOT a sweep and cannot see a masked survivor.

Every ``expect`` list below was MEASURED against the mutation's own
selection on 2026-09-05, not predicted, and each failure was read back to
confirm the named assertion fired rather than an exception upstream of it.

crewcut: the runner below (selection, mutant build, restore-owned verdicts,
error reporting) is a copy of the one in
tests/mutation_gate_pattern_catalog_nested_group.py, which is the shape every
gate in this service uses. Removal path: lift those functions into a
tests/mutation_gate_lib.py that both gates import, keeping the names
``_selected``, ``_build_mutant``, ``_restore`` and ``_apply_and_run`` on each
gate module as thin wrappers -- tests/test_mutation_gate_selection.py and
tests/test_mutation_gate_runner_safety.py load the nested-group gate by path
and call exactly those names, so they must keep working unchanged.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
TRANSFORM = SERVICE / "speech" / "pattern_transform.py"
CATALOG = SERVICE / "speech" / "pattern_catalog.py"
PROCESSOR = SERVICE / "speech" / "speech_processor.py"
ROUTER = SERVICE / "speech" / "router.py"
ENGINE = SERVICE / "speech" / "command_engine.py"
TEST_FILE = "tests/test_pattern_transform_boundary_body.py"
CLASSES = (
    "TestBoundaryAnchoredBodyExtraction",
    "TestQuantifierInsideTheLiteralText",
    "TestTheCatalogStoresMatchersForEveryName",
    "TestCanContinueHoldsAPartialName",
)

# ---------------------------------------------------------------------------
# Test selections. A mutation runs the node ids in its own "selection", and
# the expected-catcher-name collection reads the SAME list, so a name that
# does not exist in the selection stops the run instead of reading as a
# survivor. Stage A's nine entries omit the key and get STAGE_A_SELECTION.
# ---------------------------------------------------------------------------
STAGE_A_SELECTION = tuple(f"{TEST_FILE}::{name}" for name in CLASSES)

# Stage B, the hold itself: the release deadline, expiry, the lifecycle
# clear, and the completion rule all act on a hold that already stands, so
# the four mutations that touch them share one selection.
SEL_HOLD = (
    "tests/test_held_tail.py::TestTheHoldNeverOutlivesItsBound",
    "tests/test_held_tail.py::TestAHoldLosesNoWords",
    "tests/test_pipeline_punctuation_names.py::TestAPauseBetweenTheWords",
    "tests/test_pipeline_punctuation_names.py::TestACommandPrefixIsUnaffected",
    "tests/test_pipeline_punctuation_names.py::TestTheShippedRemoteWordShape",
    "tests/test_utterance_word_list.py::TestWordListRecording",
)
# wh-spaced-punctuation-names-unresolved.3.1. One mutation's only catcher
# is an e2e test, so that mutation needs a selection that reaches it. The
# class is not in SEL_HOLD itself because every other hold mutation runs
# in seconds without it.
SEL_HOLD_E2E = SEL_HOLD + (
    "tests/e2e/test_e2e_utterance_end_replacement.py"
    "::TestProductionMarkerShapeStillWorks",
)
# wh-spaced-punctuation-names-unresolved.3.1.3. The 1 + 2 split class is
# 34 tests and about 50 seconds on its own, so only the two mutations that
# need it carry it. Measured 2026-09-05: this selection runs in 93 seconds
# unmutated, inside the 180-second per-mutation timeout.
SEL_SPLIT = SEL_HOLD + (
    "tests/test_pipeline_punctuation_names.py::TestThePauseAfterTheFirstWord",
)
# wh-spaced-punctuation-names-unresolved.3.1.4. The lifecycle class is
# the only place a hold armed BY the reset's own finalization can be
# seen; TestAHoldLosesNoWords in SEL_HOLD covers the hold that already
# stood on entry, which is the control the fix must not disturb.
SEL_LIFECYCLE = SEL_HOLD + (
    "tests/test_held_tail.py::TestALifecycleResetDrainsWhatItArms",
)
# wh-spaced-punctuation-names-unresolved.3.1.2. The retract scope can
# only be seen end to end: at processor level the delivered text is the
# same broken and fixed, and only the real UIActionHandler.retract and
# the real paste counters separate them. SEL_HOLD rides along as the
# control that the ordinary hold behaviour is untouched.
SEL_RETRACT_SCOPE = SEL_HOLD + (
    "tests/e2e/test_e2e_held_prefix_retract_scope.py",
)
# wh-spaced-punctuation-names-unresolved.3.1.2, codex round 3. The
# restore-scope module is separate from the retract-scope one above
# because it sends the start_utterance IPC production sends and the
# older module does not.
SEL_RESTORE_SCOPE = SEL_RETRACT_SCOPE + (
    "tests/e2e/test_e2e_held_prefix_restore_scope.py",
)
# wh-spaced-punctuation-names-unresolved.3.1.9, codex round 6. The
# editor arm of the restore has no e2e coverage -- reaching it needs a
# wired logic_controller, which the e2e harness does not build -- so
# its catchers live in a module that stands a real offscreen editor
# window up behind a controller adapter. The restore-scope set rides
# along as the control that the legacy arm is untouched.
SEL_EDITOR_RESTORE = SEL_RESTORE_SCOPE + (
    "tests/test_speech_processor_editor_restore_unconditional.py",
)
# Stage B, the question "are these words an exact opening of a name": the
# router probe, the gate that consults it, and the ordinary sentence that
# must not be held.
SEL_NAME = (
    "tests/test_pattern_matcher_punctuation.py"
    "::TestTheMultiWordReplacementNameProbe",
    "tests/test_held_tail.py::TestTheHoldArmsAtAnUtteranceEnd",
    "tests/test_pipeline_punctuation_names.py::TestOrdinaryDictationIsNotHeldBack",
)
# Stage B, the whole-utterance half of the same gate. The e2e class is here
# because it is the test speech_processor.py's own docstring names for this
# guard, and it is the only one that exercises it end to end.
SEL_WHOLE = (
    "tests/test_held_tail.py::TestTheHoldArmsAtAnUtteranceEnd",
    "tests/e2e/test_e2e_utterance_end_replacement.py"
    "::TestTheSecondPassNeverExecutesACommand",
)

# -v prints one line per test, which is what names a SKIPPED catcher; -rfE
# keeps the FAILED and ERROR summary lines the readers below parse.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rfE"]
PER_MUTATION_TIMEOUT_S = 180

YIELDS_WORDS = "test_the_open_single_quote_name_yields_its_words"
FEEDS_BUILDER = "test_the_body_feeds_the_matcher_builder"
ANCHORED_SAME = "test_an_anchored_pattern_is_unchanged"
ANCHORED_LEADING_B = "test_an_anchored_pattern_with_a_leading_boundary_is_unchanged"
NEITHER_END = "test_a_pattern_anchored_at_neither_end_is_still_refused"
EMPTY_BOUNDARY = "test_the_empty_boundary_pattern_is_refused"
HALF_BOUNDED = "test_a_half_bounded_pattern_is_refused"
GREEDY_DISJOINT = "test_a_greedy_boundary_pattern_stays_with_the_prefix_extractor"
QUANTIFIER_BODY = "test_the_optional_space_survives_into_the_body"
QUANTIFIER_MATCHER = "test_the_truncation_matcher_accepts_both_spacings"
GROUP_EXPANDS = "test_a_group_inside_the_name_expands"
EVERY_NAME = "test_every_one_of_them_has_body_matchers"
NOT_BOTH = "test_no_name_gets_both_a_prefix_and_a_body"
OPEN_SINGLE = "test_open_single_can_continue"
GREATER_THAN = "test_greater_than_can_continue"

# Stage B catchers. Each name below appears in some mutation's measured
# "expect"; none is declared and then expected by nobody (the failure the
# Stage A round-2 finding recorded for NEITHER_END).
#
# PAUSED_NAMES and SHIPPED_PAUSED_OPEN_SINGLE were removed on
# 2026-09-05 for exactly that reason. They were the catchers of
# the-new-utterance-guard-is-dropped, which the
# wh-spaced-punctuation-names-unresolved.3.1.3 fix masked; the repair
# moved that mutation onto E2E_NEW_UTTERANCE_NEVER_COMPLETES and left
# these two expected by nobody. The tests themselves are untouched and
# still run in SEL_HOLD -- only the declarations are gone.
GROWING_HOLD_ONE_DEADLINE = "test_a_growing_hold_arms_one_deadline_and_no_more"
DEADLINE_IS_REPLACEMENT_TIMEOUT = "test_the_deadline_uses_the_replacement_timeout"
EXPIRY_TYPES_IT_ALL = "test_expiry_after_the_focus_answer_changed_types_it_all"
PAUSE_PAST_DEADLINE = "test_a_pause_past_the_deadline_types_the_words"
COMPLETE_FIRES_AT_DEADLINE = "test_a_completed_name_fires_when_the_deadline_passes"
SEPARATION_STAYS_WORDS = "test_question_mark_my_words_stays_words"
COMMITTED_TEXT_NEVER_ABSORBED = "test_committed_text_is_never_absorbed"
WORD_LIST_RECORDS_COMPLETION = (
    "test_word_completing_held_replacement_prefix_recorded"
)
# wh-spaced-punctuation-names-unresolved.3.1 catchers: the same cases in
# the word shape the shipped remote path sends, where no real word
# carries end_of_utterance.
SHIPPED_SEPARATION = (
    "test_question_mark_my_words_stays_words_on_the_shipped_shape"
)
COMPLETE_FIRES_ON_NEW_UTTERANCE = (
    "test_a_held_complete_name_fires_when_a_new_utterance_opens"
)
E2E_NEW_UTTERANCE_NEVER_COMPLETES = (
    "test_a_new_utterance_never_completes_the_old_replacement"
)
LONG_PAUSE_KEEPS_REST_SEPARATE = (
    "test_open_then_a_long_pause_keeps_the_rest_separate"
)
LIFECYCLE_FLUSHES_THE_HOLD = "test_a_lifecycle_reset_marker_flushes_the_hold"
# wh-spaced-punctuation-names-unresolved.3.1.3 catchers. The two
# parametrized families cover every three-word name in the shipped
# catalog on both word shapes. The ids are spelled with hyphens on
# purpose: the runner reads a collected line as
# line.split('::')[-1].split(' ')[0], so an id containing a space would
# be truncated and match nothing.
THREE_WORD_CASES = (
    "open-single-quote",
    "begin-single-quote",
    "end-single-quote",
    "close-single-quote",
    "dot-dot-dot",
    "less-than-sign",
    "greater-than-sign",
    "pound-sterling-sign",
)
SPLIT_BRIDGE = tuple(
    f"test_the_pause_after_the_first_word_still_inserts_the_mark[{case}]"
    for case in THREE_WORD_CASES
)
SPLIT_SHIPPED = tuple(
    f"test_the_pause_after_the_first_word_on_the_shipped_shape[{case}]"
    for case in THREE_WORD_CASES
)
COMPLETED_NAME_ABSORBS = (
    "test_a_completed_name_does_not_absorb_the_words_after_it"
)
# wh-spaced-punctuation-names-unresolved.3.1.4 catchers.
RESET_TYPES_WHAT_IT_HELD = (
    "test_the_reset_types_a_name_opening_its_own_finalization_held"
)
RESET_PHRASE_TWO_CANNOT_FINISH = (
    "test_the_phrase_after_the_reset_cannot_finish_the_name"
)
# wh-spaced-punctuation-names-unresolved.3.1.2 catchers. The three
# held states the finding enumerates, plus the control that the record
# does not reach past its own span.
RETRACT_KEEPS_COMPLETED_NAME = (
    "test_a_completed_name_waiting_for_its_end_keeps_its_first_word"
)
RETRACT_KEEPS_INCOMPLETE_OPENING = (
    "test_an_incomplete_opening_keeps_its_first_word"
)
RETRACT_KEEPS_MISMATCHED_FLUSH = (
    "test_a_mismatched_continuation_keeps_the_flushed_word"
)
RECORD_DOES_NOT_OUTLIVE_ITS_SPAN = (
    "test_the_record_does_not_outlive_the_span_it_describes"
)
RESTORE_EXPIRED_PREFIX_NOT_DOUBLED = (
    "test_a_prefix_that_expired_before_the_next_start_is_not_doubled"
)
RESTORE_REFUSED_PREFIX_NOT_TYPED = (
    "test_c_a_prefix_refused_by_the_screen_read_gate_is_never_typed"
)
RESTORE_BOUNDARY_WORDS_STAY_LITERAL = (
    "test_b_words_dictated_because_of_a_boundary_stay_dictated"
)
RESTORE_FIRED_NAME_KEEPS_EARLIER_WORD = (
    "test_d_a_held_name_that_fires_its_mark_keeps_the_earlier_word"
)
RESTORE_LATE_FLUSH_IS_RESTORED = (
    "test_e_a_prefix_flushed_after_the_next_start_is_restored"
)
RESTORE_MID_STEP_REFUSAL_RECORDS_NOTHING = (
    "test_f_a_gate_that_turns_refusing_mid_step_records_nothing"
)
RESTORE_LATER_START_NOT_RESTORED = (
    "test_g_a_flush_a_later_start_outran_is_not_restored"
)
RESTORE_LATER_START_MARK_NOT_RESPELLED = (
    "test_h_a_mark_a_later_start_outran_is_not_respelled"
)
RESTORE_START_DURING_THE_WAIT_STILL_RESTORES = (
    "test_i_a_start_during_the_retract_wait_still_restores"
)
EDITOR_RESTORE_KEEPS_ONE_EARLIER_WORD = (
    "test_a_later_start_leaves_the_earlier_word_in_the_replay"
)
EDITOR_RESTORE_KEEPS_TWO_EARLIER_WORDS = (
    "test_a_later_start_leaves_two_earlier_words_in_the_replay"
)
EDITOR_RESTORE_REFUSAL_INSTALLS_NOTHING = (
    "test_a_refused_retract_installs_nothing_the_restore_added"
)
NOT_A_NAME_OPENING_REFUSES = (
    "test_a_word_that_is_not_a_name_opening_still_refuses"
)
TAIL_ENDING_IN_A_NAME_WORD_REFUSES = (
    "test_a_tail_that_merely_ends_in_a_name_word_refuses"
)
PART_OF_THE_UTTERANCE_REFUSES = (
    "test_a_name_opening_that_is_only_part_of_the_utterance_refuses"
)
LEAVES_THE_OPENING = "test_a_sentence_that_leaves_the_opening_is_not"
ORDINARY_TAIL_IS_NOT_AN_OPENING = (
    "test_an_ordinary_sentence_tail_is_not_an_opening"
)
OPEN_THE_DOOR = "test_open_the_door_is_dictated"
LATE_COLLISION_PAIR_TYPES = (
    "test_a_late_command_collision_pair_types_instead_of_executing"
)

# The extractor's own answer, the matchers built from it, and the two
# can_continue questions. A mutation that empties or corrupts the body
# fails all of these, which is what tells a real break from a test that
# happens to be red.
BODY_ALL = [
    YIELDS_WORDS,
    FEEDS_BUILDER,
    QUANTIFIER_BODY,
    QUANTIFIER_MATCHER,
    GROUP_EXPANDS,
    EVERY_NAME,
    OPEN_SINGLE,
    GREATER_THAN,
]

# The shape test, with the comment block above it left out of the match so
# a later wording change cannot make the pattern stale.
_BOUNDED_LINE = (
    '    bounded = p.startswith(r"\\b") and p.endswith(r"\\b")\n'
)
_ANCHORED_LINE = '    anchored = p.startswith("^") and p.endswith("$")\n'
_GREEDY_GUARD = '    if _find_greedy_tail_spans(p):\n        return ""\n'
_ELSE_STRIP = "    else:\n        p = p[2:-2]\n"
_NEITHER_END_GUARD = (
    "    if not anchored and not bounded:\n"
    '        return ""\n'
)
# extract_literal_prefix, not the body extractor. The two stay disjoint
# because a bounded name has no greedy tail, so this early return is
# what declines it -- mutate the return, not the shape test.
_PREFIX_NO_TAIL = (
    "    spans = _find_greedy_tail_spans(p)\n"
    "    if not spans:\n"
    '        return ""\n'
)
_CATALOG_STORE = (
    "                        if literal_body and (\n"
    '                            " " in literal_body or "\\\\s" in literal_body\n'
    "                        ):\n"
)

# ---------------------------------------------------------------------------
# Stage B patterns. Each is matched against the file named in the mutation's
# "file" key; every one was counted in the current source and matches exactly
# once (see the --check summary the run prints).
# ---------------------------------------------------------------------------
# The ONE arming statement left in speech_processor.py. The flag line above
# it is part of the pattern on purpose: it pins the site to
# _arm_replacement_prefix_release rather than to any future bare
# _start_timeout call with the same argument.
_ARM_RELEASE = (
    "        self._prefix_hold_crossed_utterance_end = across_utterance_end\n"
    "        self._prefix_hold_joined_new_utterance = False\n"
    "        self._start_timeout(self.replacement_timeout_ms)\n"
)
# speech/router.py, the tail of is_incomplete_replacement_name. The
# single-word sibling above it asks the same question of ``[word]``, so the
# argument name is what makes this line unique.
_NAME_CANNOT_MATCH = (
    '        return not self.matcher.cannot_match(words, "replacement", False)\n'
)
# The end_of_utterance arm of _should_hold_replacement_prefix.
_WHOLE_UTTERANCE_GATE = (
    "            return (\n"
    '                self._payload_is_the_whole_utterance(" ".join(words))\n'
    "                and self.router.is_incomplete_replacement_name(words)\n"
    "            )\n"
)
# The release deadline's give-up arm, in the timeout-sentinel branch.
_EXPIRY_FLUSH = (
    "                await self._flush_pending_replacement_prefix_as_dictation()\n"
    "                return\n"
)
# The lifecycle-reset branch's clear. The end-marker branch's call to the
# same method passes allow_continued_hold=True over three lines, so the
# bare-parentheses form belongs to this branch alone.
# wh-spaced-punctuation-names-unresolved.3.1.4 made this call
# ambiguous: _consume_finalization_armed_tail now carries the same
# statement at the same indentation. The comment line above the
# lifecycle-reset branch's own call is what names this site, so the
# pattern carries it.
_LIFECYCLE_CLEAR = (
    "                # phrase 2 (wh-trailing-question-mark-words).\n"
    "                await self._consume_held_tail_at_utterance_end()\n"
)
# wh-spaced-punctuation-names-unresolved.3.1.2. The restore in
# _handle_retraction's retracted arm, and the record it reads.
# wh-spaced-punctuation-names-unresolved.3.1.2, codex round 3: the
# legacy arm no longer prepends into the replay string. It redelivers
# the earlier words as text, because the replay loop labels everything
# it is given as ONE utterance and the name matcher would join the
# restored words to the corrected final.
_RETRACT_RESTORE = (
    "            restored_earlier = await "
    "self._redeliver_earlier_utterance_words(\n"
    "                start_count_at_retract,\n"
    "            )\n"
)
# wh-spaced-punctuation-names-unresolved.3.1.2, codex round 3: the
# boundary now takes a CANDIDATE and clears the confirmed record.
# Only a real delivery promotes the candidate, so this site alone no
# longer decides what gets restored.
# wh-spaced-punctuation-names-unresolved.3.1.10, codex round 7: the
# record gained a third field, the delivering SURFACE, and 23598702
# clears it here beside the other two. The pattern carries that line or
# it matches nothing.
_BOUNDARY_RECORD = (
    "        self._earlier_utterance_words_in_retract_span = []\n"
    "        self._earlier_utterance_words_generation = None\n"
    "        self._earlier_utterance_words_surface = None\n"
    "        held = self._pending_replacement_prefix\n"
    "        self._words_held_across_utterance_end = "
    "list(held) if held else []\n"
)
# The start-of-utterance clear, above the top-of-loop held-tail
# advance. WebSocketManager sends start_utterance immediately before
# this word (integrations/websocket_manager.py:1292 and 1527) and that
# IPC resets the paste counter (ui/ui_action_handler.py:1126).
_START_BOUNDARY_CLEAR = (
    "        if word_event.start_of_utterance:\n"
    "            self._clear_record_delivered_before(word_event)\n"
)
# wh-spaced-punctuation-names-unresolved.3.1.6, codex round 4. The
# promotion the release deadline owes when the held words leave as a
# MARK instead of as text. The sibling call in the completing-word arm
# passes ``combined``, so the argument name makes this site unique.
_FIRED_NAME_PROMOTION = (
    "        self._record_replacement_delivery(held)\n"
)
# The comparison that separates a record delivered BEFORE the new
# utterance's start_utterance from one delivered after it. Only the
# operator differs between the two answers, so the mutation flips the
# operator rather than deleting the guard.
_SPAN_COMPARISON = (
    "        if started is None or recorded is None or recorded < started:\n"
)
# .3.1.7: the question both restores ask before they put anything back
# -- has a start_utterance gone out since the delivery that earned this
# record. The comparison and the count it reads are mutated separately:
# a guard can survive losing its comparison and still fail when the
# input it compares moves, and the two are different defects.
_LIVE_SPAN_COMPARISON = (
    "        if recorded is None or current is None or recorded == current:\n"
)
# .3.1.8: the legacy arm's read moved OUT of the guard and up to
# the moment the retract is enqueued, so the old pattern here --
# "current = self._read_start_utterance_count()" inside
# _earlier_words_still_in_live_span -- matches nothing now. This is
# the same input, re-aimed at where it is taken.
_LIVE_SPAN_READ = (
    "        start_count_at_retract = self._read_start_utterance_count()\n"
)
# .3.1.8: the moment that read is taken. Putting it back after the
# response restores the defect codex round 6 filed: the retract is
# already queued, so a start sent during the wait sits behind it
# and the Input side removes the earlier words anyway, but a count
# read afterwards sees that start and suppresses the restoration.
_RETRACT_COUNT_MOMENT = (
    "                start_count_at_retract,\n"
)
# .3.1.9: the editor arm's read of the record, with no guard between
# the two. The first line alone also matches the guard helper's own
# read, so the ``return replay_text`` below it is what pins this to
# the editor arm.
_EDITOR_ARM_RECORD_READ = (
    "        earlier = self._earlier_utterance_words_in_retract_span\n"
    "        if not earlier:\n"
    "            return replay_text\n"
)
# The stamp that says which span a promoted record was delivered into.
_RECORD_SPAN_STAMP = (
    "        self._earlier_utterance_words_generation = (\n"
    "            self._last_delivery_generation\n"
    "        )\n"
)
# The report at the LEGACY exit alone -- the fire-and-forget
# send, which is the arm a replacement's intelligent_insert_text
# step takes, because nothing sets awaits_done on an insertion
# step. The boss's 2026-09-06 ruling asks for a mutation at this
# exit specifically, not one that empties the shared helper.
_ENGINE_LEGACY_REPORT = (
    '                        # .3.1.6: same report as the awaited arm, minus\n'
    '                        # the payloads the queue refused outright.\n'
    '                        if insert_processor is not None and accepted is not False:\n'
    '                            _note_replacement_surface(\n'
    '                                insert_processor, "legacy", insert_generation,\n'
    '                            )\n'
)
# The processor's read of that report. Replacing it with a
# constant reproduces the WITHDRAWN pre-check: an answer decided
# before the rule ran, which can disagree with what the rule did.
_REPORTED_SURFACE_READ = (
    '            self._last_replacement_surface, len(spoken_words),\n'
)
# The one line that carries the insertion step's report to the
# processor. Both arms of _execute_rule call the same helper, so
# emptying it removes every report at once.
_ENGINE_REPORT_CALL = (
    "    note = getattr(processor, 'note_replacement_text_delivered', None)\n"
    "    if callable(note):\n"
    "        note(surface, generation)\n"
)
# The refusal that keeps an undelivered prefix out of the record.
_DELIVERY_REFUSAL = (
    "        if not candidate or surface is None:\n"
    "            return\n"
)
# The lifecycle branch's drain of a prefix its own finalization armed.
# The end-marker branch calls the same helper three lines differently
# (no argument), so the keyword is what makes this site unique.
_LIFECYCLE_PREFIX_DRAIN = (
    "                    await self._consume_finalization_armed_tail(\n"
    "                        drain_replacement_prefix=True,\n"
    "                    )\n"
)
# The completion rule in _resolve_pending_replacement_prefix.
_COMPLETION_NEEDS_THE_END = "        if not word_event.end_of_utterance:\n"

# The new-utterance guard, which this branch rewrote. The shipped line
# was ``if not word_event.word or word_event.start_of_utterance:``; the
# marker half is now its own guard above, and this half decides whether
# a name may finish across an utterance boundary.
#
# ``if word_event.start_of_utterance:`` appears three times in
# speech_processor.py, so the header alone is an ambiguous pattern. The
# two lines under it make this one unique, which the runner's
# exactly-one-match check would otherwise refuse.
_JOIN_HEADER = (
    "        if word_event.start_of_utterance:\n"
    "            if await self._fire_held_replacement_if_complete():\n"
    "                return False\n"
)
_JOIN_CONDITION = (
    "            if not (\n"
    "                self._prefix_hold_crossed_utterance_end\n"
    "                and self.router.is_incomplete_replacement_name(held)\n"
    "            ):\n"
)
# The tentative branch's own condition, added by
# wh-spaced-punctuation-names-unresolved.3.1.3. The elif header alone
# appears once, but the condition under it is what the mutation removes,
# so both lines are the pattern.
_TENTATIVE_HELD_GUARD = (
    "        elif self._prefix_hold_joined_new_utterance and not (\n"
    "            self.router.is_incomplete_replacement_name(held)\n"
    "        ):\n"
)
# Refreshed by .3.1.3, which made the record sticky. The previous
# single-line form no longer exists; the runner's pattern-not-found
# error is what caught the staleness.
_TENTATIVE_RECORD = (
    "        joined_new_utterance = (\n"
    "            word_event.start_of_utterance\n"
    "            or self._prefix_hold_joined_new_utterance\n"
    "        )\n"
)

MUTATIONS = [
    {
        # A5: the mutation the criterion asks for by name.
        "name": "the-boundary-shape-is-refused",
        "old": _BOUNDED_LINE,
        "new": "    bounded = False\n",
        "expect": BODY_ALL,
    },
    {
        "name": "the-boundary-strip-leaves-the-boundary",
        "old": _ELSE_STRIP,
        "new": "    else:\n        p = p[1:-1]\n",
        "expect": [
            YIELDS_WORDS,
            FEEDS_BUILDER,
            OPEN_SINGLE,
            GREATER_THAN,
            # Measured 2026-09-05: this one catches ONLY this mutation.
            # The degenerate pattern reaches the strip, and p[1:-1]
            # leaves it a two-character body instead of the empty
            # string. No other mutation changes this input's result:
            # the-boundary-shape-is-refused returns '' at the neither-
            # end guard before the strip, and the rest reach the strip
            # and still yield '' (p[3:-3] of a two- or four-character
            # string is empty too) or do not touch this function.
            EMPTY_BOUNDARY,
        ],
    },
    {
        "name": "the-boundary-strip-eats-the-first-letter",
        "old": _ELSE_STRIP,
        "new": "    else:\n        p = p[3:-3]\n",
        "expect": [YIELDS_WORDS, FEEDS_BUILDER, OPEN_SINGLE, GREATER_THAN],
    },
    {
        "name": "a-half-bounded-pattern-is-accepted",
        "old": _BOUNDED_LINE,
        "new": '    bounded = p.startswith(r"\\b") or p.endswith(r"\\b")\n',
        "expect": [HALF_BOUNDED],
    },
    {
        "name": "the-greedy-tail-refusal-is-dropped",
        "old": _GREEDY_GUARD,
        "new": '    if False:\n        return ""\n',
        "expect": [GREEDY_DISJOINT],
    },
    {
        "name": "the-anchored-shape-is-refused",
        "old": _ANCHORED_LINE,
        "new": "    anchored = False\n",
        "expect": [ANCHORED_SAME, ANCHORED_LEADING_B],
    },
    {
        # Codex round 2 (wh-spaced-punctuation-names-unresolved.1.2):
        # NEITHER_END was declared and never expected by any mutation, so
        # nothing required the neither-shape refusal to stay live. Dropping
        # the guard sends an unanchored, unbounded pattern to the else
        # branch, where p[2:-2] eats four characters off a plain phrase.
        "name": "the-neither-end-refusal-is-dropped",
        "old": _NEITHER_END_GUARD,
        "new": '    if False:\n        return ""\n',
        "expect": [NEITHER_END, HALF_BOUNDED],
    },
    {
        # The other half of the same finding. NOT_BOTH pins that no name
        # gets a prefix AND a body. The invariant does not rest on the
        # boundary shape: extract_literal_prefix declines a bounded name
        # because it finds no greedy tail. Returning the remaining text
        # instead of "" is exactly the design mistake the test guards, and
        # it is the one mutation NOT_BOTH can see.
        "name": "the-prefix-extractor-claims-a-name-with-no-greedy-tail",
        "old": _PREFIX_NO_TAIL,
        "new": (
            "    spans = _find_greedy_tail_spans(p)\n"
            "    if not spans:\n"
            "        return p\n"
        ),
        "expect": [NOT_BOTH],
    },
    {
        # The catalog half. Only ONE test sees it, and that is measured,
        # not assumed: the first sweep expected the two can_continue
        # tests to fail as well and they did not.
        # _buffer_opens_literal_prefix (pattern_matcher.py:878-900) falls
        # back to calling extract_full_literal_body on the compiled
        # pattern when the catalog stored nothing, so the store is a
        # load-time cache and not the correctness path. That makes
        # test_every_one_of_them_has_body_matchers the only test in the
        # file that can catch a dropped store, which is exactly why it is
        # worth keeping.
        "name": "the-catalog-does-not-store-the-body-matchers",
        "file": CATALOG,
        "old": _CATALOG_STORE,
        "new": (
            "                        if False and literal_body and (\n"
            '                            " " in literal_body '
            'or "\\\\s" in literal_body\n'
            "                        ):\n"
        ),
        "expect": [EVERY_NAME],
    },
    # -----------------------------------------------------------------
    # Stage B (criterion C5). Each entry carries its own selection: the
    # catchers live in four files other than the Stage A one.
    # -----------------------------------------------------------------
    {
        # C5 "hold length". x20 rather than a removal: the deadline must
        # still be ARMED, or the two timer-spy tests would see an empty
        # list and fail for the wrong reason. With 400 ms becoming
        # 8000 ms the hold outlives every wait in the selection, so the
        # expiry tests see nothing typed and the spies see 8000.
        "name": "the-release-deadline-is-longer-than-its-bound",
        "file": PROCESSOR,
        "selection": SEL_HOLD,
        "old": _ARM_RELEASE,
        "new": (
            "        self._prefix_hold_crossed_utterance_end = "
            "across_utterance_end\n"
            "        self._prefix_hold_joined_new_utterance = False\n"
            "        self._start_timeout(self.replacement_timeout_ms * 20)\n"
        ),
        # Measured 2026-09-05 against SEL_HOLD: 6 failed, 14 passed. Two
        # spy tests read 8000 where they demand replacement_timeout_ms
        # (400), and four tests that wait out the deadline find nothing
        # typed. Every failure is the test's own assertion; none is an
        # exception upstream of it.
        "expect": [
            GROWING_HOLD_ONE_DEADLINE,
            DEADLINE_IS_REPLACEMENT_TIMEOUT,
            EXPIRY_TYPES_IT_ALL,
            PAUSE_PAST_DEADLINE,
            COMPLETE_FIRES_AT_DEADLINE,
            WORD_LIST_RECORDS_COMPLETION,
        ],
    },
    {
        # C5 "prefix exactness". Returning True drops the "can these
        # words still grow into a name" half and keeps only the "are
        # they already complete" half above it, so every unfinished word
        # list is called an opening.
        "name": "a-word-that-cannot-grow-into-a-name-is-called-incomplete",
        "file": ROUTER,
        "selection": SEL_NAME,
        "old": _NAME_CANNOT_MATCH,
        "new": "        return True\n",
        # Measured 2026-09-05 against SEL_NAME: 5 failed, 6 passed. Two
        # router-probe tests fail on `assert True is False` for
        # is_incomplete_replacement_name(["open","the"]) and (["hello"]),
        # two hold-gate tests on the same shape one level up, and the
        # pipeline test finds "open the door" held instead of typed.
        "expect": [
            LEAVES_THE_OPENING,
            ORDINARY_TAIL_IS_NOT_AN_OPENING,
            NOT_A_NAME_OPENING_REFUSES,
            TAIL_ENDING_IN_A_NAME_WORD_REFUSES,
            OPEN_THE_DOOR,
        ],
    },
    {
        # C5 "committed-text guard". Only the whole-utterance half goes;
        # the name test stays, so the mutant holds a sentence TAIL that
        # happens to open a name. Dropping the name half instead would
        # be the previous mutation seen from the other side.
        "name": "the-whole-utterance-half-of-the-hold-gate-is-dropped",
        "file": PROCESSOR,
        "selection": SEL_WHOLE,
        "old": _WHOLE_UTTERANCE_GATE,
        "new": (
            "            return self.router.is_incomplete_replacement_name"
            "(words)\n"
        ),
        # Measured 2026-09-05 against SEL_WHOLE: 2 failed, 3 passed. The
        # processor-level test fails on
        # _should_hold_replacement_prefix(["space"]) returning True while
        # the utterance was "I said back space"; the e2e test fails on
        # `assert 'i said back' == 'i said back space'` -- the held word
        # never reached the typed text. The four-file union run measured
        # the same one processor-level catcher and no others, which is
        # why the e2e class is in the selection at all.
        "expect": [PART_OF_THE_UTTERANCE_REFUSES, LATE_COLLISION_PAIR_TYPES],
    },
    {
        # C5 "expiry path". The slot is cleared without the dictation
        # IPC, which is the exact shape of "expiry loses the words".
        # Deleting the flush line alone would leave the slot FULL with no
        # timer left to release it -- a stuck hold, not a lost one --
        # and deleting the `return` with it would drop the branch through
        # to the spoken-click check below. Both are different defects
        # from the one this mutation is named for, so the replacement
        # clears the slot and keeps the return.
        "name": "the-expiry-drops-the-held-words-instead-of-typing-them",
        "file": PROCESSOR,
        "selection": SEL_HOLD,
        "old": _EXPIRY_FLUSH,
        "new": (
            "                self._pending_replacement_prefix = None\n"
            "                return\n"
        ),
        # Measured 2026-09-05 against SEL_HOLD: 3 failed, 17 passed.
        # "open" vanishes from "open bracket" and from "open voice
        # access help", and the focus-answer test sees nothing reach
        # _send_to_dictation. Every test whose hold COMPLETES before the
        # deadline stays green -- the three
        # test_a_paused_name_still_inserts_its_mark cases and both timer
        # spies -- which is what tells this mutation from the deadline
        # one above.
        "expect": [
            PAUSE_PAST_DEADLINE,
            LONG_PAUSE_KEEPS_REST_SEPARATE,
            EXPIRY_TYPES_IT_ALL,
        ],
    },
    {
        # C5 "context/provider clear". The line chosen is the
        # lifecycle-reset branch's own
        # `await self._consume_held_tail_at_utterance_end()`
        # (speech_processor.py, inside `if
        # word_event.is_lifecycle_reset_marker:`). That IS the clear: a
        # Mode-1 provider disagreement queues this marker
        # (integrations/websocket_manager.py:499), and the call is the
        # one statement that empties the held slot before the branch's
        # end_utterance / start_utterance pair, so held words cannot land
        # in phrase 2. Wrapped in `if False:` rather than deleted so the
        # call keeps being parsed and merely stops being reached: a
        # deletion would also compile here (the `if self.mode !=
        # ProcessingMode.IDLE:` block follows it inside the same try),
        # but it would take the call out of the file, and a pattern that
        # deletes its own target cannot be told from one whose target
        # moved.
        "name": "the-lifecycle-reset-no-longer-clears-the-hold",
        "file": PROCESSOR,
        "selection": SEL_HOLD,
        "old": _LIFECYCLE_CLEAR,
        "new": (
            "                # phrase 2 (wh-trailing-question-mark-words).\n"
            "                if False:\n"
            "                    await "
            "self._consume_held_tail_at_utterance_end()\n"
        ),
        # Measured 2026-09-05 against SEL_HOLD: 1 failed, 19 passed, and
        # against the four-file union the same single catcher. The
        # failure is `assert [] == ['open single']` -- the held words were
        # never typed. Note which assertion in that test fires: the
        # `proc._held_tail is None` line above it runs AFTER the
        # harness's own stop(), which clears the slot, so the insertion
        # assertion is the one doing the work.
        "expect": [LIFECYCLE_FLUSHES_THE_HOLD],
    },
    {
        # The ruling the stage rests on: a completed name fires only when
        # the completing word ends its utterance. `if False:` makes the
        # completed pair fire on arrival instead of being held.
        "name": "a-completed-name-fires-before-its-utterance-ends",
        "file": PROCESSOR,
        "selection": SEL_HOLD,
        "old": _COMPLETION_NEEDS_THE_END,
        "new": "        if False:\n",
        # Measured 2026-09-05 against SEL_HOLD: 1 failed, 19 passed. The
        # catcher is the assertion taken BEFORE the deadline --
        # `assert early == []` with the message "the mark fired on
        # arrival, so the deadline never fired". The end-state-only
        # assertions in the same file cannot see this mutation, because
        # the mark still arrives; only the early one can, which is what
        # that assertion was added for.
        # test_question_mark_my_words_stays_words does NOT catch it and is
        # not listed: its "mark" opens a new utterance, so it flushes at
        # the start_of_utterance guard above and never reaches this line.
        "expect": [COMPLETE_FIRES_AT_DEADLINE],
    },
    {
        # The whole new-utterance branch is disabled, so the previous
        # utterance's held words fuse with the next utterance's first
        # word whatever either one is, and nothing fires a held
        # complete name early either.
        "name": "the-new-utterance-guard-is-dropped",
        "file": PROCESSOR,
        "selection": SEL_HOLD_E2E,
        "old": _JOIN_HEADER,
        "new": (
            "        if False:\n"
            "            if await self._fire_held_replacement_if_complete():\n"
            "                return False\n"
        ),
        # Measured 2026-09-05 against SEL_HOLD_E2E: one test fails,
        # test_a_new_utterance_never_completes_the_old_replacement, on
        #     assert 1 == 2
        # -- two utterances' words fused into one, so the second
        # utterance completed the first one's replacement. Read back and
        # confirmed as that assertion, not an exception upstream of it.
        # The e2e class is what supplies the only input this mutation
        # alone can change: a hold the timeout sentinel armed in the
        # MIDDLE of an utterance, which the header refuses to let a new
        # utterance's first word complete.
        #
        # MASKING, and how it was found (the failure mode the
        # mutation-gate skill names under "a later defense-in-depth fix
        # can MASK an older mutation's catchers"). This entry used to
        # run SEL_HOLD and expect three catchers,
        # GROWING_HOLD_ONE_DEADLINE, PAUSED_NAMES[0] and
        # SHIPPED_PAUSED_OPEN_SINGLE, each failing on
        #     assert ['open single'] == ["'"]
        # The full sweep on f525a096 reported it SURVIVED with no test
        # failing at all. Neither the mutation nor those tests had
        # changed. wh-spaced-punctuation-names-unresolved.3.1.3 added
        # the held-words condition to the tentative elif below the
        # header, and with the header dead that elif is what decides
        # these sequences. Before .3.1.3 it fired for a hold that was
        # still an unfinished opening and typed "open single"; now it
        # declines, and the name reaches its mark whether the header
        # runs or not. Isolated by measurement, not by reading:
        # re-running the mutation with ONLY the held-words condition
        # reverted brought all three catchers back, and re-running it
        # with only the sticky record reverted did not. So those three
        # tests never depended on this header; they depended on the
        # elif not firing, and the fix gave them a second way to get
        # that. The e2e input above is the one this header still owns
        # by itself.
        "expect": [E2E_NEW_UTTERANCE_NEVER_COMPLETES],
    },
    {
        # Only the complete-name fire goes. A held complete name whose
        # own end marker was lost then reaches the flush below and the
        # user gets the words instead of the mark.
        "name": "the-new-utterance-never-fires-a-complete-name",
        "file": PROCESSOR,
        "selection": SEL_HOLD,
        "old": _JOIN_HEADER,
        "new": "        if word_event.start_of_utterance:\n",
        # Measured 2026-09-05 against SEL_HOLD: exactly one test
        # fails, with
        #     assert ['question mark', 'hello'] == ['?', 'hello']
        # so the held complete name reached the flush below and the
        # user got its words instead of the mark. That is the
        # behaviour the fire exists to produce, not a crash upstream.
        "expect": [COMPLETE_FIRES_ON_NEW_UTTERANCE],
    },
    {
        # The join stops caring where the hold was armed, so a hold the
        # timeout sentinel armed mid-utterance can be completed by the
        # next utterance's first word.
        "name": "the-cross-utterance-join-ignores-where-the-hold-armed",
        "file": PROCESSOR,
        "selection": SEL_HOLD_E2E,
        "old": _JOIN_CONDITION,
        "new": (
            "            if not (\n"
            "                self.router.is_incomplete_replacement_name(held)\n"
            "            ):\n"
        ),
        # Measured 2026-09-05 against SEL_HOLD_E2E: exactly one test
        # fails, with
        #     assert 1 == 2  +  where 1 = len(['?'])
        # so a hold the timeout sentinel armed mid-utterance was
        # completed by the next utterance's first word and the two
        # deliveries the test expects became one. The e2e class is
        # this mutation's only catcher, which is why it needs the
        # wider selection. (The ERROR line about maybe_route_to_editor
        # in that run is the fixture's own logging, present in the
        # unmutated run too.)
        "expect": [E2E_NEW_UTTERANCE_NEVER_COMPLETES],
    },
    # A fifth mutation was written for the other half of the same
    # condition -- dropping ``is_incomplete_replacement_name(held)`` and
    # keeping only the crossed-utterance-end record -- and it SURVIVED
    # SEL_HOLD on 2026-09-05 (returncode 0, no test failed). It is an
    # equivalent mutant, and the reachability argument was read out of
    # speech_processor.py rather than assumed:
    #   * grep -n "_prefix_hold_crossed_utterance_end" finds one write,
    #     at line 3133 in _arm_replacement_prefix_release, reached from
    #     three call sites (1227, 1605, 3280).
    #   * every one of those sites is guarded by
    #     _should_hold_replacement_prefix with the SAME value passed as
    #     end_of_utterance, and that gate's end_of_utterance branch
    #     already requires is_incomplete_replacement_name(words).
    #   * the two growth writes keep the invariant: line 3010 requires
    #     is_incomplete_replacement_name(combined), and line 3051 writes
    #     a COMPLETE name, which the fire above this condition consumes
    #     before the condition is reached.
    # So held words reaching this condition with the crossed-end record
    # set are always an exact opening of a name, and the probe cannot
    # change the outcome. The probe is not dead code -- it is the
    # invariant stated where it is relied on -- but no test can
    # distinguish its removal here. The ARMING-side probe, which is
    # where the invariant is actually established, keeps its own
    # mutation (the-name-probe-is-dropped, SEL_NAME).
    {
        # A value mutation rather than a guard one, which is what the
        # mutation-gate skill asks for where a guard controls a loop or
        # a later branch: the tentative record is never set, so a join
        # made across an utterance boundary is never settled by the
        # later words that prove the utterance was dictation.
        "name": "the-tentative-record-is-never-set",
        "file": PROCESSOR,
        "selection": SEL_HOLD,
        "old": _TENTATIVE_RECORD,
        "new": "        joined_new_utterance = False\n",
        # Measured 2026-09-05 against SEL_HOLD: two tests fail, one per
        # word shape, both with
        #     assert '?' not in ['?', 'my words']
        # so "question" then "mark my words" inserted a mark. That is
        # the separation case the bead names, failing for its own
        # assertion. The shipped-shape case is the one that matters
        # most: on that path no real word carries end_of_utterance, so
        # this record is the ONLY thing that can refuse the join.
        "expect": [SEPARATION_STAYS_WORDS, SHIPPED_SEPARATION],
    },
    # wh-spaced-punctuation-names-unresolved.3.1.3, codex round 2. The
    # two lines the fix changed, one mutation each.
    {
        # The branch as it stood before the fix: tentative meant flush,
        # whatever the held words were.
        "name": "the-held-words-need-not-be-an-unfinished-name",
        "file": PROCESSOR,
        "selection": SEL_SPLIT,
        "old": _TENTATIVE_HELD_GUARD,
        "new": "        elif self._prefix_hold_joined_new_utterance:\n",
        # Measured 2026-09-05 against the split classes: 17 tests fail.
        # The sixteen split cases fail with
        #     assert ['open single'] == ["'"]
        # -- the name typed its first two words instead of inserting its
        # mark -- and the completed-name case fails on its own
        # assertion. Read back and confirmed: every failure is the named
        # assertion, not an exception upstream of it.
        "expect": [*SPLIT_BRIDGE, *SPLIT_SHIPPED, COMPLETED_NAME_ABSORBS],
    },
    {
        # A value mutation: the record is read from the arriving word
        # alone, so it is cleared by the very word that extended the
        # name.
        "name": "the-tentative-record-is-cleared-as-the-hold-grows",
        "file": PROCESSOR,
        "selection": SEL_SPLIT,
        "old": _TENTATIVE_RECORD,
        "new": (
            "        joined_new_utterance = word_event.start_of_utterance\n"
        ),
        # Measured 2026-09-05 against the split classes: exactly one
        # test fails, on
        #     assert "\'" not in ["\'", 'please']
        # so "open" then "single quote please" inserted the mark and
        # swallowed "please". Only one test can see this: the split
        # cases stop at the completing word and never reach a further
        # one, so the record is never re-read for them.
        "expect": [COMPLETED_NAME_ABSORBS],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.4, codex round 2.
        # A value mutation on the one argument that carries the
        # decision, rather than a guard inside the helper: the helper's
        # default is deliberately the opposite, so flipping the call
        # site is what states which caller owns the choice.
        "name": "the-lifecycle-reset-keeps-the-prefix-it-armed",
        "file": PROCESSOR,
        "selection": SEL_LIFECYCLE,
        "old": _LIFECYCLE_PREFIX_DRAIN,
        "new": (
            "                    await "
            "self._consume_finalization_armed_tail(\n"
            "                        drain_replacement_prefix=False,\n"
            "                    )\n"
        ),
        # Measured 2026-09-05 against SEL_LIFECYCLE: 2 failed, 28
        # passed. The failures are
        #     assert [] == ['open']
        # -- phrase 1 was never typed before the pair -- and
        #     assert '[' not in ['[']
        # -- phrase 2 completed phrase 1's name. Both are the named
        # assertion, not an exception upstream of it. The
        # pre-existing-hold control in TestAHoldLosesNoWords stays
        # green, which is what shows the entry drain is untouched.
        "expect": [RESET_TYPES_WHAT_IT_HELD, RESET_PHRASE_TWO_CANNOT_FINISH],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.2, codex round 2.
        # The restore itself. Assigning the name to itself keeps the
        # statement (and the else arm it lives in) rather than deleting
        # the only statement of a block, which would not compile.
        "name": "the-corrected-final-drops-the-earlier-utterances-words",
        "file": PROCESSOR,
        "selection": SEL_RETRACT_SCOPE,
        "old": _RETRACT_RESTORE,
        "new": (
            "            replay_text = replay_text\n"
        ),
        "expect": [
            RETRACT_KEEPS_COMPLETED_NAME,
            RETRACT_KEEPS_INCOMPLETE_OPENING,
            RETRACT_KEEPS_MISMATCHED_FLUSH,
        ],
    },
    {
        # The record's producer. Same loss, one step earlier: with an
        # empty record the restore above is a no-op.
        "name": "the-boundary-record-is-never-taken",
        "file": PROCESSOR,
        "selection": SEL_RETRACT_SCOPE,
        "old": _BOUNDARY_RECORD,
        "new": (
            "        self._earlier_utterance_words_in_retract_span = []\n"
            "        self._earlier_utterance_words_generation = None\n"
            "        self._earlier_utterance_words_surface = None\n"
        ),
        "expect": [
            RETRACT_KEEPS_COMPLETED_NAME,
            RETRACT_KEEPS_INCOMPLETE_OPENING,
            RETRACT_KEEPS_MISMATCHED_FLUSH,
        ],
    },
    {
        # The other direction. Keeping the record past its own
        # end_utterance restores text that is still on screen.
        "name": "the-record-outlives-the-span-it-describes",
        "file": PROCESSOR,
        "selection": SEL_RETRACT_SCOPE,
        "old": _BOUNDARY_RECORD,
        # The surface clear STAYS (.3.1.10). Only the words clear goes,
        # so the words outlive their span while the surface does not.
        # Keeping the clear is what makes the mutation visible: a stale
        # surface would fail the arms' `surface is not None` test and
        # drop the outliving record before it could be restored.
        "new": (
            "        self._earlier_utterance_words_generation = None\n"
            "        self._earlier_utterance_words_surface = None\n"
            "        held = self._pending_replacement_prefix\n"
            "        self._words_held_across_utterance_end = (\n"
            "            list(held) if held else []\n"
            "        )\n"
        ),
        "expect": [RECORD_DOES_NOT_OUTLIVE_ITS_SPAN],
    },
    # RETIRED 2026-09-06: the-start-boundary-never-clears-the-record.
    # It replaced the start-of-utterance clear's call with a block that
    # can never fire, and its one catcher was
    # test_a_prefix_that_expired_before_the_next_start_is_not_doubled.
    # The full sweep at d1b003f6 reported it SURVIVED with no failing
    # test at all ("failed tests were []"), and the run before it, at
    # 5faa77f8, records the catch by name. Nothing went stale and the
    # test is not wrong. d1b003f6 added
    # _earlier_words_still_in_live_span, a second layer over the same
    # input: case A delivers the words before the next
    # start_utterance, so the recorded count is below the count read at
    # the restore and the guard drops the record whether or not the
    # clear ran. That is the masking failure the mutation-gate skill
    # describes, and the answer it gives -- feed the catcher an input
    # only the mutated layer handles -- has no honest form here. The
    # only inputs the two sites answer differently are the unstamped
    # ones, which no shipped producer creates. The clear itself STAYS
    # (ruling, 2026-09-06): it costs nothing and frees the record at
    # the boundary. RESTORE_EXPIRED_PREFIX_NOT_DOUBLED and
    # _START_BOUNDARY_CLEAR are deliberately left defined above, so
    # this entry can be restored if the guard ever stops covering
    # case A.
    {
        # Case C. A prefix the screen-read gate refused was never
        # displayed, so nothing of it is inside the span. Recording it
        # regardless makes the correction type text the user never
        # saw. Dropping only the surface half of the guard keeps the
        # empty-candidate half, so the mutant is about delivery and
        # not about an empty list.
        "name": "an-undelivered-prefix-is-recorded-anyway",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _DELIVERY_REFUSAL,
        "new": (
            "        if not candidate:\n"
            "            return\n"
        ),
        "expect": [RESTORE_REFUSED_PREFIX_NOT_TYPED],
    },
    {
        # Case B. Putting the earlier words back through the replay
        # loop instead of delivering them as text makes them
        # neighbours of the corrected final inside one utterance, and
        # the in-utterance name matcher then joins them into a mark.
        # _restore_earlier_utterance_words still exists for the editor
        # arm, so this mutant compiles.
        #
        # wh-spaced-punctuation-names-unresolved.3.1.10, full sweep at
        # 23598702. THE EXPECT LIST WAS ONE NAME AND THAT WAS NOT
        # ENOUGH. 23598702 gave the editor arm a surface guard, and the
        # words in this scenario carry surface "legacy", so the editor
        # arm the mutant redirects them into now REFUSES them and the
        # mutant restores nothing at all. test_b's assertion was purely
        # negative -- no "?" on screen -- so deleting the restore
        # outright satisfied it and the mutation read as a survivor.
        # The repair is in two halves, both ruled 2026-09-06. test_b now
        # also asserts POSITIVELY that the earlier word is on screen, so
        # it can tell "restored literally" from "never restored"; and
        # the six word-preservation tests measured failing under the
        # mutant join the list, because a mutation that can delete the
        # restore must be named by tests that assert the words come
        # back.
        "name": "the-restored-words-go-back-through-the-replay",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _RETRACT_RESTORE,
        "new": (
            "            replay_text = "
            "self._restore_earlier_utterance_words(replay_text)\n"
            "            restored_earlier = False\n"
        ),
        "expect": [
            RESTORE_BOUNDARY_WORDS_STAY_LITERAL,
            RETRACT_KEEPS_COMPLETED_NAME,
            RETRACT_KEEPS_INCOMPLETE_OPENING,
            RETRACT_KEEPS_MISMATCHED_FLUSH,
            RESTORE_FIRED_NAME_KEEPS_EARLIER_WORD,
            RESTORE_LATE_FLUSH_IS_RESTORED,
            RESTORE_START_DURING_THE_WAIT_STILL_RESTORES,
        ],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.6, codex round 4.
        # A hold that resolves into a mark is a delivery of the
        # earlier utterance's words just as a dictated flush is.
        # Dropping the promotion leaves the confirmed record empty, so
        # the correction that removes the mark replays only its own
        # final and the earlier word is gone from the screen.
        "name": "a-fired-name-never-promotes-its-earlier-words",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _FIRED_NAME_PROMOTION,
        "new": "",
        "expect": [RESTORE_FIRED_NAME_KEEPS_EARLIER_WORD],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.5, codex round 4.
        # The two orderings the record has to tell apart differ only in
        # whether the delivery went out before or after the new
        # utterance's start_utterance, and the counts are EQUAL in the
        # second case. Widening the comparison to <= makes the equal
        # case clear as well, which is exactly the pre-fix behaviour:
        # a prefix flushed inside the new span is dropped from the
        # record, and the correction that empties that span replays
        # only its own final. Case A must stay green under this
        # mutation -- it is the strictly-smaller side, which <= still
        # clears -- so only the .3.1.5 test can catch it.
        "name": "the-clear-also-drops-a-record-from-its-own-span",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _SPAN_COMPARISON,
        "new": (
            "        if started is None or recorded is None"
            " or recorded <= started:\n"
        ),
        "expect": [RESTORE_LATE_FLUSH_IS_RESTORED],
    },
    {
        # .3.1.5 from the other end: without the stamp the record
        # carries no span at all, the comparison reads None, and the
        # missing-stamp rule clears -- the same lost word by a
        # different route. The assignment is replaced by a None store
        # rather than deleted, so the attribute still exists and the
        # mutant fails on the assertion instead of an AttributeError.
        "name": "a-promoted-record-never-records-its-span",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _RECORD_SPAN_STAMP,
        "new": "        self._earlier_utterance_words_generation = None\n",
        "expect": [RESTORE_LATE_FLUSH_IS_RESTORED],
    },
    {
        # .3.1.6, codex round 4. The promotion has to come from the
        # outcome the insertion step reports, so silencing the report
        # leaves every replacement delivery looking like one that typed
        # nothing. The held name that fires its mark then records no
        # earlier word and the correction removes the mark without
        # putting that word back. The body keeps its lookup so the
        # mutant still compiles and still runs to the assertion.
        "name": "the-insertion-step-never-reports-what-it-typed",
        "file": ENGINE,
        "selection": SEL_RESTORE_SCOPE,
        "old": _ENGINE_REPORT_CALL,
        "new": (
            "    note = getattr(processor,"
            " 'note_replacement_text_delivered', None)\n"
            "    if callable(note):\n"
            "        pass\n"
        ),
        "expect": [RESTORE_FIRED_NAME_KEEPS_EARLIER_WORD],
    },
    {
        # .3.1.6, the boss's 2026-09-06 ruling. The report has
        # to leave from the exit that actually typed, so this
        # removes it at the legacy exit alone and leaves the
        # editor arm, the refusal and the awaited arm reporting.
        # A held name that fires its mark takes this exit, so
        # its earlier word stops being recorded and the
        # correction removes the mark without putting it back.
        "name": "the-legacy-exit-never-reports-its-insertion",
        "file": ENGINE,
        "selection": SEL_RESTORE_SCOPE,
        "old": _ENGINE_LEGACY_REPORT,
        "new": "",
        "expect": [RESTORE_FIRED_NAME_KEEPS_EARLIER_WORD],
    },
    {
        # The withdrawn pre-check, reproduced. Reading a
        # constant instead of the step's own report is exactly
        # what cb9d1054 did, and it is wrong in both directions
        # because an await separates the two moments. The
        # gate-flip test measures the direction an ordinary
        # screen read produces: allowed when the step begins,
        # refusing by the time the rule asks, nothing typed, and
        # a record taken for a mark that never reached the
        # screen.
        "name": "a-refused-step-still-records-as-typed",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _REPORTED_SURFACE_READ,
        # The literal is "legacy", not an invented name (.3.1.10, full
        # sweep at 23598702). The mutation's point is that a REFUSED
        # step still takes a record; the surface it claims has to be one
        # the legacy arm will actually restore, or the arm's new surface
        # guard drops the bogus record and the mutation is masked -- the
        # measured outcome when this read "replacement", which made
        # test_f pass and moved the failure to test_d.
        "new": '            "legacy", len(spoken_words),\n',
        "expect": [RESTORE_MID_STEP_REFUSAL_RECORDS_NOTHING],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.7, codex round 5.
        # The record only describes text a correction can still remove,
        # and a later start_utterance resets the Input paste counter
        # underneath it. Keeping the record unconditionally is the
        # pre-fix behaviour: the flushed prefix is typed a second time
        # beside the copy the retract could not reach, and the fired
        # mark is spelled again beside the mark itself. The body is
        # kept so the mutant compiles and runs to the assertions.
        "name": "the-restore-never-asks-whether-the-span-is-live",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _LIVE_SPAN_COMPARISON,
        "new": "        if True:\n",
        "expect": [
            RESTORE_LATER_START_NOT_RESTORED,
            RESTORE_LATER_START_MARK_NOT_RESPELLED,
        ],
    },
    {
        # The same guard from its input side. Reading the record's own
        # number instead of the app's live count makes every comparison
        # equal, so the guard keeps every record while still looking
        # like a guard. A revert of the comparison alone would not
        # expose this; the mutation-gate skill asks for the
        # input-level change for exactly that reason.
        "name": "the-span-check-reads-the-record-instead-of-the-count",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _LIVE_SPAN_READ,
        "new": (
            "        start_count_at_retract = "
            "self._earlier_utterance_words_generation\n"
        ),
        "expect": [
            RESTORE_LATER_START_NOT_RESTORED,
            RESTORE_LATER_START_MARK_NOT_RESPELLED,
        ],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.8, codex
        # round 6. The guard asks the right question at the wrong
        # moment if the count is read after the retract's
        # response. Handing the redeliver a fresh read instead of
        # the captured one restores exactly that defect, and the
        # words the retract really removed are never put back.
        "name": "the-count-is-read-after-the-retract-instead-of-at-it",
        "file": PROCESSOR,
        "selection": SEL_RESTORE_SCOPE,
        "old": _RETRACT_COUNT_MOMENT,
        "new": (
            "                self._read_start_utterance_count(),\n"
        ),
        "expect": [RESTORE_START_DURING_THE_WAIT_STILL_RESTORES],
    },
    {
        # wh-spaced-punctuation-names-unresolved.3.1.9, codex round
        # 6. Asking the Input side's start count on the EDITOR arm is
        # the defect: that count governs the Input paste counter, and
        # the editor session is governed by the ledger's own utterance
        # id, so a later start_utterance drops words the editor
        # retract still peels. This mutation puts the question back
        # exactly where it was, which is also the whole of the fix
        # reverted at the input level -- the arm keeps its shape and
        # runs to the assertions either way.
        "name": "the-editor-restore-asks-the-input-start-count",
        "file": PROCESSOR,
        "selection": SEL_EDITOR_RESTORE,
        "old": _EDITOR_ARM_RECORD_READ,
        "new": (
            "        earlier = self._earlier_words_still_in_live_span(\n"
            "            self._read_start_utterance_count(),\n"
            "        )\n"
            "        if not earlier:\n"
            "            return replay_text\n"
        ),
        "expect": [
            EDITOR_RESTORE_KEEPS_ONE_EARLIER_WORD,
            EDITOR_RESTORE_KEEPS_TWO_EARLIER_WORDS,
            EDITOR_RESTORE_REFUSAL_INSTALLS_NOTHING,
        ],
    },
]


def _target(mut):
    return mut.get("file", TRANSFORM)


def _selection(mut):
    """The pytest node ids one mutation runs, as a hashable tuple.

    The run, the baseline and the expected-catcher-name collection all
    read this, so a mutation cannot be judged against a set of tests
    different from the one its expected names were collected from. Stage
    A's nine entries carry no ``selection`` and get the Stage A file's
    four classes.
    """
    return tuple(mut.get("selection", STAGE_A_SELECTION))


def _clear_bytecode():
    for cache in SERVICE.glob("speech/__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _test_id(line: str) -> str:
    """The ``name[param]`` part after the last ``::`` on a pytest line."""
    return line.split("::")[-1].strip().split(" ")[0]


def collect_names(selection):
    """Real test ids in ``selection``, so a renamed test cannot read as a
    survivor.

    Takes the selection rather than reading CLASSES, because a Stage B
    mutation's catchers live in files the Stage A selection does not
    name. Collecting from the SAME node ids the mutation will run is the
    point: a name collected from a wider set could still be absent from
    the run that has to fail on it.
    """
    out = _pytest(*selection, "--collect-only", "-q", "-p", "no:randomly")
    return {_test_id(line) for line in out.stdout.splitlines() if "::" in line}


def failed_names(output):
    return {
        _test_id(line)
        for line in output.splitlines()
        if line.startswith("FAILED ")
    }


def _is_error_summary(line: str) -> bool:
    """Is ``line`` pytest's ERROR summary form, and not a captured log record?

    Both summary forms name a path:
        ERROR tests/test_x.py::TestC::test_y - Exception: msg
        ERROR tests/test_x.py                     (a collection error)
    Each carries ".py", and each either uses "::" to separate node ids or
    carries no colon at all. A captured log record is
    "ERROR    <logger>:<file>:<lineno> <message>", so it has single
    colons and no "::" -- which is what tells the two apart.

    Without this test the word ERROR itself was returned as a test id,
    and one mutation's whole run yielded no verdict even though its six
    catchers had all failed on their own assertions. Found by the full
    sweep on 2026-09-05; the isolated measurement that preceded it read
    only the failed set and could not see it.
    """
    if not line.startswith("ERROR "):
        return False
    token = line[len("ERROR "):].strip().split(" ")[0]
    if ".py" not in token:
        return False
    return "::" in token or ":" not in token


def error_names(output):
    """Test ids on ERROR records: the -v per-test lines and the -rE summary.

    pytest reports an ERROR, not a FAILED, when a fixture raises or a test
    errors rather than fails, and a collection error prints only the
    summary form. Either would otherwise be read as a verdict. This gate
    needs it more than most: the module-scoped ``catalog`` fixture builds
    a PatternCatalog, and a mutation that makes catalog construction raise
    turns every test in the file into an ERROR rather than a FAILED.
    """
    return {
        _test_id(line)
        for line in output.splitlines()
        if _is_error_summary(line) or ("::" in line and " ERROR" in line)
    }


def run_defect(result):
    """A reason this run cannot yield a verdict, or None."""
    if "+++ Timeout +++" in result.stdout:
        return "suite-timeout-abort"
    if result.returncode not in (0, 1):
        return f"unexpected pytest exit status {result.returncode}"
    errored = sorted(error_names(result.stdout))
    if errored:
        return f"pytest reported ERROR records: {errored}"
    if result.stderr.strip():
        first = result.stderr.strip().splitlines()[0]
        return f"pytest wrote to stderr: {first!r}"
    return None


def skipped_names(output):
    """Test ids on -v per-test lines that read ``...::name SKIPPED (...)``."""
    return {
        _test_id(line)
        for line in output.splitlines()
        if "::" in line and " SKIPPED" in line
    }


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _build_mutant(mut):
    """Return (mutant_bytes, original_bytes, error) for one mutation.

    Reads the target itself, so a catalog mutation is built from
    pattern_catalog.py and an extractor mutation from
    pattern_transform.py. The line ending is detected per file: this
    repository mixes conventions, and an LF pattern matches nothing in a
    CRLF file.
    """
    path = _target(mut)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    newline = _newline_of(raw)
    old = mut["old"].replace("\n", newline)
    new = mut["new"].replace("\n", newline)
    count = text.count(old)
    if count != 1:
        return None, raw, (
            f"{mut['name']}: pattern matched {count} times in {path.name}, "
            "expected 1"
        )
    mutated = text.replace(old, new, 1)
    try:
        compile(mutated, str(path), "exec")
    except SyntaxError as exc:
        return None, raw, (
            f"{mut['name']}: mutated source does not compile: {exc}"
        )
    return mutated.encode("utf-8"), raw, None


def _restore(path: Path, original: bytes) -> bool:
    """Put the file back, and say whether the original bytes are back.

    Returns True only when the file now holds ``original``. A False is a
    hard condition for the caller, not a warning: a run whose target is
    still mutated cannot yield a verdict, and the mutant is sitting in a
    TRACKED file that every later run and every other session sharing the
    checkout will read as real code.

    write_bytes, never write_text: on Windows write_text rewrites every
    line ending as CRLF, which silently breaks every multi-line pattern
    in this file on the next run. A second Ctrl+C landing inside the
    restore is held, retried once, and re-raised only after the bytecode
    caches are cleared -- a same-size mutant otherwise leaves
    current-looking bytecode for the next run to import.
    """
    held = None
    restored = False
    for _ in range(2):
        try:
            path.write_bytes(original)
            restored = True
            break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR could not restore {path}: {exc}")
            break
    else:
        print(f"ERROR could not restore {path}: interrupted twice")
    _clear_bytecode()
    if held is not None:
        raise held
    return restored


def _apply_and_run(path: Path, mutant: bytes, original: bytes,
                   selection=STAGE_A_SELECTION):
    """Write the mutant, run ``selection``, restore. Return (result, error).

    The write is INSIDE the guarded block on purpose: an interrupt or an
    OSError landing during the write must still reach the restore. A
    restore that fails clears ``result`` as well as setting an error, so
    no caller can read a verdict out of a run whose source file is still
    mutated.

    ``selection`` defaults to the Stage A node ids, so the signature stays
    compatible with a three-argument call.
    """
    result = None
    error = None
    _clear_bytecode()
    try:
        path.write_bytes(mutant)
        result = _pytest(*selection, *PYTEST_ARGS)
    except subprocess.TimeoutExpired:
        error = "timed out"
    except OSError as exc:
        error = f"could not write the mutant to {path.name}: {exc}"
    finally:
        if not _restore(path, original):
            error = f"the original bytes of {path.name} were not restored"
            result = None
    return result, error


def check_only(mutations=None):
    """Verify each selected pattern without running any test.

    This is NOT a sweep. It proves every pattern matches exactly once in
    the current source and that every mutant compiles; it cannot see a
    masked survivor.
    """
    if mutations is None:
        mutations = MUTATIONS
    stale, non_compiling = [], []
    for mut in mutations:
        _mutant, _original, error = _build_mutant(mut)
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    print(
        f"checked {len(mutations)} of {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(non_compiling)} that do not compile"
    )
    return 1 if stale or non_compiling else 0


def _selected():
    """The mutations this run covers, honouring --only <name> and --only=<name>.

    Both spellings are accepted, and an empty selection is an ERROR rather
    than a green run of nothing. Every mutation here rewrites a tracked
    source file on disk, so the operator has to be able to trust the
    printed scope: a command that says one mutation and runs seven opens
    six more windows in which a concurrent test run reads a half-mutated
    file.
    """
    argv = sys.argv[1:]
    if not any(a == "--only" or a.startswith("--only=") for a in argv):
        return MUTATIONS
    wanted = set()
    for i, arg in enumerate(argv):
        if arg.startswith("--only="):
            value = arg[len("--only="):]
            if value:
                wanted.add(value)
        elif arg == "--only":
            # Everything up to the next flag. Stopping at a flag keeps a
            # later option from being read as a mutation name.
            for follower in argv[i + 1:]:
                if follower.startswith("--"):
                    break
                wanted.add(follower)
    if not wanted:
        print(
            "ERROR --only needs a mutation name: "
            "--only <name> or --only=<name>"
        )
        return None
    unknown = wanted - {m["name"] for m in MUTATIONS}
    if unknown:
        print(f"ERROR --only names no such mutation: {sorted(unknown)}")
        return None
    return [m for m in MUTATIONS if m["name"] in wanted]


def main():
    # The selection is resolved BEFORE the --check branch, so that
    # `--check --only=<name>` checks the one pattern the operator asked
    # about, and a --check with an unknown or empty --only exits non-zero.
    selected = _selected()
    if selected is None:
        return 1
    if "--check" in sys.argv[1:]:
        return check_only(selected)

    errors, caught, survived = [], [], []

    # One entry per DISTINCT selection, in first-use order. Several
    # mutations share a selection, and collecting or baselining it once
    # per mutation would multiply the slowest part of the run for
    # nothing.
    selections = []
    for mut in selected:
        sel = _selection(mut)
        if sel not in selections:
            selections.append(sel)

    real_names = {}
    for sel in selections:
        real_names[sel] = collect_names(sel)
        print(
            f"collected {len(real_names[sel])} test ids in "
            f"{len(sel)} node ids: {sel[0]}..."
        )
    for mut in selected:
        for name in mut["expect"]:
            if name not in real_names[_selection(mut)]:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist "
                    f"in its own selection"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    for sel in selections:
        _clear_bytecode()
        baseline = _pytest(*sel, *PYTEST_ARGS)
        if baseline.returncode != 0:
            print("ERROR baseline is not green; refusing to start")
            print(f"      selection: {list(sel)}")
            print(baseline.stdout[-2000:])
            return 1
        # Only the catchers of the mutations that RUN this selection: a
        # name skipped in some other selection's baseline says nothing
        # about this one.
        expected_here = {
            name
            for mut in selected
            if _selection(mut) == sel
            for name in mut["expect"]
        }
        skipped_catchers = sorted(
            skipped_names(baseline.stdout) & expected_here
        )
        if skipped_catchers:
            print(
                f"ERROR expected catchers skipped in the baseline: "
                f"{skipped_catchers}"
            )
            print(f"      selection: {list(sel)}")
            print(
                "      a skipped catcher can never fail, so its mutation "
                "cannot be proven"
            )
            return 1
    print(f"baseline green over {len(selections)} selections")

    for mut in selected:
        mutant, original, error = _build_mutant(mut)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        assert mutant is not None
        path = _target(mut)
        result, error = _apply_and_run(
            path, mutant, original, _selection(mut)
        )
        if error is not None:
            errors.append(f"{mut['name']}: {error}")
            print("ERROR", errors[-1])
            if "not restored" in error:
                # A tracked source file still holds the mutant. Every later
                # mutation would run against it, and so would any other
                # session sharing the checkout, so the sweep stops here
                # instead of piling verdicts on top of a corrupt tree.
                print("ERROR stopping the sweep; restore the file by hand")
                break
            continue
        assert result is not None
        defect = run_defect(result)
        if defect is not None:
            errors.append(f"{mut['name']}: {defect}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        skipped = [
            n for n in mut["expect"] if n in skipped_names(result.stdout)
        ]
        if skipped:
            errors.append(f"{mut['name']}: expected catcher skipped: {skipped}")
            print("ERROR", errors[-1])
            continue
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
        else:
            caught.append(mut["name"])
            print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    # Reached, not len(selected): a restore failure breaks the sweep, and
    # "none skipped" would then be a false line in the run's own summary.
    reached = len(caught) + len(survived) + len(errors)
    print(
        f"scope: {reached} of {len(selected)} selected mutations run, "
        f"{len(MUTATIONS)} in the full set"
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
