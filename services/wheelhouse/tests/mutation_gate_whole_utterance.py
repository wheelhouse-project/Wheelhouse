"""Mutation gate for Stage 3 of wh-whole-utterance-command-matching.

Run it from services/wheelhouse:

    python tests/mutation_gate_whole_utterance.py

Stage 3 defers impossible buffers to the utterance end, judges
whole_utterance_only patterns against the utterance's word list, and
(item-20 R1) splits a trailing command word off dictation-bound
payloads at marker-driven finalization. The mutations disable each of
those pieces one at a time; the catchers are the Stage-3 suites
(tests/test_whole_utterance_finalization.py,
tests/e2e/test_e2e_trailing_at_utterance_end.py,
tests/test_router_gaps.py, tests/test_retraction_replay_guard.py) plus
the absorbed bare-number suite.

Run a subset with --only name1,name2 (the skill's run-scope rule:
re-run only mutations added or whose target code / catching test
changed this round; the full sweep runs once before the final commit).

Thirty-one mutations across speech/router.py and
speech/speech_processor.py:

  deferral-restored-finalize   Step 3 finalizes impossible buffers at
                               word speed again instead of deferring.
  deferral-uses-greedy-timer   The deferral arms the greedy timer
                               instead of the mode's fixed timeout.
  whole-utterance-check-dropped  whole_utterance_only fires without
                               the buffer-spans-utterance check.
  span-hotword-head-dropped    The active wake word stops counting as
                               an allowed head.
  snapshot-to-live-list        The auto-finalize reads the live word
                               list instead of the previous
                               utterance's snapshot.
  lifecycle-finalize-drop      The lifecycle-reset branch stops
                               finalizing a deferred buffer before the
                               end_utterance/start_utterance pair.
  r1-split-hotword-gate-dropped  The R1 split stops exempting
                               hotword-authorized dictation.
  r1-split-never               The R1 split never recognizes a
                               trailing last word.
  finalization-consume-skips-trailing  The post-finalization consume
                               stops firing a finalization-armed
                               trailing word.
  marker-consume-skips-bare-number  The end-marker consume stops
                               clicking a held bare number (the
                               19(b) guard's hand-proven mutation,
                               scripted).
  flag-context-not-threaded    Router step 1 (the flagged-last-word
                               finalization) stops forwarding
                               utterance_words, so the in-process
                               bridge loses the span check
                               (review finding .3.1.1).
  span-fused-head-exact-again  The span head check reverts to exact
                               string equality, rejecting a fused
                               "xray" (review finding .3.1.2).
  flag-driven-split-dropped    _utterance_end_is_confirmed stops
                               recognizing the flagged last word, so
                               the R1 split misses the in-process
                               bridge's ruled split (review finding
                               .3.1.1; retargeted to the extracted
                               helper by .1.5).
  marker-active-never-set      The end-marker branch stops declaring
                               its finalization context, so the
                               marker-driven split never applies.
  stale-gate-restored          The split reads the stale
                               _pending_utterance_end slot again
                               instead of calling
                               _utterance_end_is_confirmed, so a
                               timeout finalization splits without a
                               confirmed utterance end (review finding
                               .3.1.3 instance 4; retargeted by .1.5).
  lifecycle-pair-skip-on-raise The lifecycle-reset branch stops
                               catching a finalization raise, so the
                               end/start pair is skipped (review
                               finding .3.1.3 instance 2).
  lifecycle-advance-exclusion-restored  The top-of-loop advance runs
                               the held-tail flush on the lifecycle
                               marker again, outside the branch's
                               recovery guard -- and outside the
                               consume dispatch, so a pre-held tail
                               dictates instead of acting (review
                               findings .3.1.4, .3.1.8).
  lifecycle-try-shrunk-to-finalize  The lifecycle held-tail dispatch
                               runs before the recovery guard again,
                               as the old flush, so a raise skips the
                               pair and a pre-held tail dictates
                               instead of acting (review findings
                               .3.1.4, .3.1.8).
  split-site-sends-end-early   The DICTATE split site sends the
                               deferred end_utterance before the
                               armed action fires (review finding
                               .3.1.5).
  remainder-site-sends-end-early  The EXECUTE remainder site sends
                               the deferred end_utterance before the
                               armed action fires (review finding
                               .3.1.5).
  marker-branch-post-consume-send-dropped  The end-marker branch
                               stops sending the deferred
                               end_utterance a split site left
                               pending (review finding .3.1.5).
  lifecycle-marker-active-never-set  The lifecycle-reset branch stops
                               declaring its finalization context, so
                               the R1 split never applies to the
                               Mode-1 close (review finding .3.1.6,
                               boss ruling (a)).
  lifecycle-consume-dropped    The lifecycle-reset branch stops
                               consuming the finalization-armed tail
                               before the pair (review finding
                               .3.1.6, boss ruling (a)).
  punctuated-rematch-dropped   The trailing-action re-match reads the
                               raw token again, so STT terminal
                               punctuation turns the fired command
                               into dictation (review finding
                               .3.1.7).
  lifecycle-preheld-flush-restored  The lifecycle-reset branch flushes
                               the pre-held tail as dictation again
                               instead of consuming it, so a pre-held
                               trailing command types its word and a
                               pre-held bare number dictates (review
                               finding .3.1.8, boss ruling (a)).
  stale-utterance-end-survives-a-raise
                               The per-word exception handler stops
                               clearing _pending_utterance_end, so the
                               slot survives a raise and the next
                               utterance sends end_utterance carrying
                               the previous utterance's id
                               (wh-pending-utterance-end-stale-slot).
  count-growth-check-dropped   A filled count stops waiting for a
                               word that could extend it, so
                               "backspace twenty three" presses
                               twenty times and dictates "three"
                               again (wh-whole-utterance-command-
                               matching.4).
  count-growth-reads-matched-text  The growth check reads the whole
                               matched text instead of the captured
                               count group, so no phrase ever looks
                               extendable and the wait never happens.
  count-growth-drops-the-alias-options  The growth check stops
                               passing the aliases/zero pair that
                               speech/actions.py:words_to_int uses,
                               so a homophone count ("to hundred")
                               fires at the first word.
  count-growth-end-check-dropped  The growth check stops asking
                               whether the count ENDS the matched
                               text, so a user pattern that puts a
                               required literal word after the count
                               ("tab N times") waits for the end
                               marker although "times" already
                               bounded the
                               count (wh-whole-utterance-command-
                               matching.4.1.1).
  count-growth-end-reads-whole-match  That same check reads the end
                               of the WHOLE match instead of the end
                               of the count group, so a literal after
                               the count looks like part of it and
                               the pointless wait returns.

All thirty-one must be caught, and "caught" means the expected test failed on
its own assertion. Reported as errors, never as a verdict:
pattern-not-found, an ambiguous pattern, a mutation that does not
compile, a per-mutation timeout, a suite-timeout abort, any pytest
return code other than 0 or 1, any ERROR line in the summary, and an
expected test that failed for any reason other than its own assertion.

The gate detects each target file's own line endings and translates
each pattern to them before matching. It never rewrites a file's
endings.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
ROUTER = SERVICE / "speech" / "router.py"
PROC = SERVICE / "speech" / "speech_processor.py"

WHOLE = "tests/test_whole_utterance_finalization.py"
E2E = "tests/e2e/test_e2e_trailing_at_utterance_end.py"
GAPS = "tests/test_router_gaps.py"
GUARD = "tests/test_retraction_replay_guard.py"
BARE = "tests/test_speech_processor_bare_number.py"
ALL_FILES = [WHOLE, E2E, GAPS, GUARD, BARE]

PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATIONS = [
    {
        "name": "deferral-restored-finalize",
        "src": ROUTER,
        "files": [WHOLE, GAPS],
        "old": """                if mode is not ProcessingMode.MID_REPLACEMENT_BUFFERING:
                    timeout = (
""",
        "new": """                if False:
                    timeout = (
""",
        # The last two are the count wait's stake in this same branch
        # (wh-whole-utterance-command-matching.4.1.3). A filled count
        # that a later word disproves reaches the deferral exactly as an
        # unfilled one does, so removing the deferral presses at that
        # word and splits "question mark".
        "expect": [
            "test_impossible_command_buffer_defers_to_the_end",
            "test_hotword_only_buffer_defers_as_impossible_when_inactive",
            "test_a_disproving_word_does_not_press_where_it_arrives",
            "test_a_command_still_ends_an_utterance_that_holds_punctuation",
        ],
    },
    {
        "name": "deferral-uses-greedy-timer",
        "src": ROUTER,
        "files": [WHOLE, GAPS],
        "old": """                    timeout = (
                        command_timeout_ms
                        if mode is ProcessingMode.COMMAND_BUFFERING
                        else replacement_timeout_ms
                    )
""",
        "new": """                    timeout = greedy_timeout_ms
""",
        "expect": ["test_mark_plus_off_word_defers_on_the_fixed_timer"],
    },
    {
        "name": "whole-utterance-check-dropped",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """            if (
                utterance_words is not None
                and result.pattern_data.get("whole_utterance_only")
""",
        "new": """            if (
                False
                and result.pattern_data.get("whole_utterance_only")
""",
        "expect": ["test_alias_suppressed_when_earlier_words_streamed"],
    },
    {
        "name": "span-hotword-head-dropped",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """        if hotword_active and len(head) == 1:
""",
        "new": """        if False:
""",
        "expect": ["test_hotword_head_still_counts_as_spanning"],
    },
    {
        "name": "snapshot-to-live-list",
        "src": PROC,
        "files": [WHOLE],
        "old": """                utterance_words=list(self._previous_utterance_words),
""",
        "new": """                utterance_words=list(self._words_this_utterance),
""",
        "expect": ["test_cut_short_save_still_fires_on_auto_finalize"],
    },
    {
        "name": "lifecycle-finalize-drop",
        "src": PROC,
        "files": [WHOLE],
        "old": """                if self.mode != ProcessingMode.IDLE:
                    finalize_decision = self.router.decide_timeout(
""",
        "new": """                if False:
                    finalize_decision = self.router.decide_timeout(
""",
        "expect": ["test_lifecycle_reset_closes_the_deferred_buffer"],
    },
    {
        "name": "r1-split-hotword-gate-dropped",
        "src": PROC,
        "files": [E2E],
        "old": """        if hotword_authorized:
            return text, None
""",
        "new": """        if False:
            return text, None
""",
        "expect": ["test_hotword_dictation_keeps_typing_the_trailing_word"],
    },
    {
        "name": "r1-split-never",
        "src": PROC,
        "files": [E2E],
        "old": """        if self.catalog.get_trailing_command(words[-1]) is None:
            return text, None
""",
        "new": """        if True:
            return text, None
""",
        "expect": [
            "test_trailing_word_after_an_impossible_buffer_still_fires",
            "test_backspace_submit_fires_enter_under_r1",
        ],
    },
    {
        "name": "finalization-consume-skips-trailing",
        "src": PROC,
        "files": [E2E],
        # wh-spaced-punctuation-names-unresolved.3.1.4 (9becbc36) gave
        # _consume_finalization_armed_tail a drain_replacement_prefix
        # parameter, so its prefix arm is no longer a bare early
        # return and the old anchor stopped matching. The trailing arm
        # this mutation removes is unchanged.
        "old": """        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._consume_pending_trailing_word_at_utterance_end()
        else:
            await self._consume_pending_bare_number()
""",
        "new": """        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            pass
        else:
            await self._consume_pending_bare_number()
""",
        "expect": [
            "test_trailing_word_after_an_impossible_buffer_still_fires",
            "test_backspace_submit_fires_enter_under_r1",
        ],
    },
    {
        "name": "marker-consume-skips-bare-number",
        "src": PROC,
        "files": [GUARD, BARE],
        # wh-spaced-punctuation-names-unresolved.3 (cac374c3) put the
        # complete-name fire and the continued-hold check above the
        # flush, so the prefix arm is no longer one line. The elif /
        # else dispatch this mutation targets is unchanged, and the
        # elif is what keeps this anchor apart from the sibling block
        # in _consume_finalization_armed_tail.
        "old": """            await self._flush_pending_replacement_prefix_as_dictation()
        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._consume_pending_trailing_word_at_utterance_end()
        else:
            await self._consume_pending_bare_number()
""",
        "new": """            await self._flush_pending_replacement_prefix_as_dictation()
        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._consume_pending_trailing_word_at_utterance_end()
        else:
            pass
""",
        "expect": [
            "test_replay_armed_number_clicks_and_the_next_utterance_stays_clean",
            "test_bare_digit_final_executes_click",
        ],
    },
    {
        "name": "flag-context-not-threaded",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """            return self._resolve_finalization(
                new_buffer,
                hotword_active,
                allow_commands=mode is not ProcessingMode.MID_REPLACEMENT_BUFFERING,
                utterance_words=utterance_words,
            )
""",
        "new": """            return self._resolve_finalization(
                new_buffer,
                hotword_active,
                allow_commands=mode is not ProcessingMode.MID_REPLACEMENT_BUFFERING,
                utterance_words=None,
            )
""",
        "expect": [
            "test_flagged_finalization_suppresses_a_non_spanning_alias",
        ],
    },
    {
        "name": "span-fused-head-exact-again",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """            if _word_matches_hotword(head[0], self._active_hotword):
""",
        "new": """            if head[0].lower() == self._active_hotword:
""",
        "expect": ["test_fused_hotword_head_still_counts_as_spanning"],
    },
    {
        "name": "flag-driven-split-dropped",
        "src": PROC,
        "files": [E2E],
        # Retargeted for wh-overlay-count-homophones.1.5. The literal
        # this named -- the split's own inline gate on
        # (self._marker_finalization_active or flagged_last_word) --
        # was replaced in 09bdf680 by a call to the extracted
        # _utterance_end_is_confirmed, so flagged_last_word no longer
        # appears in speech_processor.py at all and the entry reported
        # "pattern matched 0 times". The behaviour is unchanged and
        # still worth pinning, so the mutation now removes the same
        # real-word arm from the helper's return. The helper serves the
        # multi-word badge check too, but that path's tests live in
        # tests/test_grid_number_badge_click.py, outside ALL_FILES, so
        # nothing but the split can produce this catch.
        "old": """        return self._marker_finalization_active or (
            word_event is not None
            and word_event.end_of_utterance
            and bool(word_event.word)
            and not word_event.is_utterance_end_marker
            and not word_event.is_timeout_finalize_marker
            and not word_event.is_retraction_marker
            and not word_event.is_lifecycle_reset_marker
        )
""",
        "new": """        return self._marker_finalization_active
""",
        "expect": [
            "test_flagged_backspace_submit_fires_enter",
            "test_flagged_backspace_hello_submit_fires_enter",
        ],
    },
    {
        "name": "marker-active-never-set",
        "src": PROC,
        "files": [E2E],
        "old": """                )
                self._marker_finalization_active = True
""",
        "new": """                )
                self._marker_finalization_active = False
""",
        "expect": [
            "test_trailing_word_after_an_impossible_buffer_still_fires",
            "test_backspace_submit_fires_enter_under_r1",
        ],
    },
    {
        "name": "stale-gate-restored",
        "src": PROC,
        "files": [E2E],
        # Retargeted for wh-overlay-count-homophones.1.5, same cause as
        # flag-driven-split-dropped above. This one mutates the split's
        # CALL of the helper rather than the helper itself, because the
        # behaviour it pins is the whole gate being replaced by a read
        # of the stale _pending_utterance_end slot -- which is what the
        # pre-09bdf680 mutation did, and it keeps the mutation scoped to
        # the split exactly as before.
        "old": """        if not self._utterance_end_is_confirmed(word_event):
""",
        "new": """        if self._pending_utterance_end is None:
""",
        "expect": [
            "test_timeout_finalization_ignores_the_stale_slot",
        ],
    },
    {
        "name": "lifecycle-pair-skip-on-raise",
        "src": PROC,
        "files": [WHOLE],
        "old": """            except Exception:
                logger.exception(
                    "Lifecycle-reset close-out failed; held/deferred "
""",
        "new": """            except ():
                logger.exception(
                    "Lifecycle-reset close-out failed; held/deferred "
""",
        "expect": [
            "test_lifecycle_reset_sends_the_pair_even_when_finalization_raises",
            "test_lifecycle_reset_sends_the_pair_even_when_a_held_flush_raises",
        ],
    },
    {
        "name": "lifecycle-advance-exclusion-restored",
        "src": PROC,
        "files": [WHOLE],
        "old": """            and not word_event.is_timeout_finalize_marker
            and not word_event.is_lifecycle_reset_marker
        ):
""",
        "new": """            and not word_event.is_timeout_finalize_marker
        ):
""",
        "expect": [
            "test_lifecycle_preheld_trailing_word_fires_enter_before_the_pair",
            "test_lifecycle_preheld_bare_number_clicks_before_the_pair",
        ],
    },
    {
        "name": "lifecycle-try-shrunk-to-finalize",
        "src": PROC,
        "files": [WHOLE],
        "old": """            try:
                # The ONE dispatch for holds standing when this marker
""",
        "new": """            await self._flush_held_tail_as_dictation()
            try:
                # The ONE dispatch for holds standing when this marker
""",
        "expect": [
            "test_lifecycle_preheld_trailing_word_fires_enter_before_the_pair",
            "test_lifecycle_preheld_bare_number_clicks_before_the_pair",
        ],
    },
    {
        "name": "lifecycle-preheld-flush-restored",
        "src": PROC,
        "files": [WHOLE],
        "old": """                await self._consume_held_tail_at_utterance_end()
                # wh-whole-utterance-command-matching.3: the deferral
""",
        "new": """                await self._flush_held_tail_as_dictation()
                # wh-whole-utterance-command-matching.3: the deferral
""",
        "expect": [
            "test_lifecycle_preheld_trailing_word_fires_enter_before_the_pair",
            "test_lifecycle_preheld_bare_number_clicks_before_the_pair",
        ],
    },
    {
        "name": "split-site-sends-end-early",
        "src": PROC,
        "files": [WHOLE],
        "old": """                    # the flagged-last-word path never set the slot.
                    return
""",
        "new": """                    # the flagged-last-word path never set the slot.
                    await self._send_pending_utterance_end()
                    return
""",
        "expect": [
            "test_dictate_split_fires_enter_before_end_utterance",
        ],
    },
    {
        "name": "remainder-site-sends-end-early",
        "src": PROC,
        "files": [WHOLE],
        "old": """            if armed_trailing is None:
                await self._send_pending_utterance_end()
""",
        "new": """            if True:
                await self._send_pending_utterance_end()
""",
        "expect": [
            "test_remainder_split_fires_enter_before_end_utterance",
        ],
    },
    {
        "name": "marker-branch-post-consume-send-dropped",
        "src": PROC,
        "files": [WHOLE],
        "old": """                # sent and cleared it.
                await self._send_pending_utterance_end()
""",
        "new": """                # sent and cleared it.
                pass
""",
        "expect": [
            "test_dictate_split_fires_enter_before_end_utterance",
            "test_remainder_split_fires_enter_before_end_utterance",
        ],
    },
    {
        "name": "lifecycle-marker-active-never-set",
        "src": PROC,
        "files": [WHOLE],
        "old": """                    self._marker_finalization_active = True
                    try:
""",
        "new": """                    self._marker_finalization_active = False
                    try:
""",
        "expect": [
            "test_lifecycle_remainder_split_fires_enter_before_the_pair",
            "test_lifecycle_dictate_split_fires_enter_before_the_pair",
        ],
    },
    {
        "name": "lifecycle-consume-dropped",
        "src": PROC,
        "files": [WHOLE],
        # wh-spaced-punctuation-names-unresolved.3.1.4 (9becbc36) made
        # this call pass drain_replacement_prefix=True, so the reset
        # delivers a prefix its own finalization armed. The mutation is
        # unchanged: it still removes the whole consume.
        "old": """                    await self._consume_finalization_armed_tail(
                        drain_replacement_prefix=True,
                    )
            except Exception:
""",
        "new": """                    pass
            except Exception:
""",
        "expect": [
            "test_lifecycle_remainder_split_fires_enter_before_the_pair",
            "test_lifecycle_dictate_split_fires_enter_before_the_pair",
        ],
    },
    {
        "name": "punctuated-rematch-dropped",
        "src": PROC,
        "files": [WHOLE],
        "old": """        match = compiled.match(_normalize_lookup_word(word))
""",
        "new": """        match = compiled.match(word)
""",
        "expect": [
            "test_punctuated_ordinary_trailing_word_fires_enter",
            "test_punctuated_remainder_split_fires_enter",
            "test_punctuated_dictate_split_fires_enter",
        ],
    },
    {
        "name": "stale-utterance-end-survives-a-raise",
        "src": PROC,
        "files": [WHOLE],
        "old": """                    # a decision failure apart from a transport failure --
                    # the same condition the bare-number crewcut above
                    # names.
                    self._pending_utterance_end = None
""",
        "new": """                    # a decision failure apart from a transport failure --
                    # the same condition the bare-number crewcut above
                    # names.
                    pass
""",
        "expect": [
            "test_a_raise_in_decide_timeout_leaves_no_stale_end",
            "test_a_raise_in_execute_decision_leaves_no_stale_end",
            "test_no_stale_end_lands_between_the_words_of_a_replay",
        ],
    },
    {
        "name": "count-growth-check-dropped",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """                growable = not unfilled and self._count_can_still_grow(result)
""",
        "new": """                growable = False
""",
        "expect": [
            "test_the_whole_count_reaches_one_press",
            "test_an_incomplete_count_fires_nothing_yet",
            "test_an_extendable_count_waits_for_the_end_of_the_utterance",
            "test_a_homophone_count_waits_the_same_way",
        ],
    },
    {
        "name": "count-growth-reads-matched-text",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """            group_num = int(result.validation_group[1:])  # "g1" -> 1
            captured = result.match_object.group(group_num)
""",
        "new": """            group_num = int(result.validation_group[1:])  # "g1" -> 1
            captured = result.matched_text
""",
        "expect": [
            "test_the_whole_count_reaches_one_press",
            "test_an_incomplete_count_fires_nothing_yet",
            "test_an_extendable_count_waits_for_the_end_of_the_utterance",
            "test_a_homophone_count_waits_the_same_way",
        ],
    },
    {
        "name": "count-growth-drops-the-alias-options",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """        return number_phrase_can_extend(captured, aliases=True, zero=True)
""",
        "new": """        return number_phrase_can_extend(captured)
""",
        "expect": ["test_a_homophone_count_waits_the_same_way"],
    },
    {
        "name": "count-growth-end-check-dropped",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """        if group_end != len(result.match_object.string):
            return False
""",
        "new": """        if False:
            return False
""",
        "expect": ["test_a_count_followed_by_literal_text_executes_at_once"],
    },
    {
        "name": "count-growth-end-reads-whole-match",
        "src": ROUTER,
        "files": [WHOLE],
        "old": """            group_end = result.match_object.end(group_num)
""",
        "new": """            group_end = result.match_object.end()
""",
        "expect": ["test_a_count_followed_by_literal_text_executes_at_once"],
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
    """Real test names in the files, so a renamed test cannot read as a survivor."""
    out = _pytest(*ALL_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert statement."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
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
    """Every failure reason --tb=line printed, one per failing parameter."""
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


def main():
    mutations = MUTATIONS
    skipped = 0
    if len(sys.argv) > 2 and sys.argv[1] == "--only":
        wanted = set(sys.argv[2].split(","))
        known = {m["name"] for m in MUTATIONS}
        unknown = wanted - known
        if unknown:
            print(f"ERROR --only names not in the set: {sorted(unknown)}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in wanted]
        skipped = len(MUTATIONS) - len(mutations)
    elif len(sys.argv) > 1:
        print(f"ERROR unrecognized arguments: {sys.argv[1:]}")
        return 1

    originals = {}
    texts = {}
    for src in (ROUTER, PROC):
        raw = src.read_bytes()
        originals[src] = raw
        newline = "\r\n" if b"\r\n" in raw else "\n"
        texts[src] = (raw.decode("utf-8"), newline)
        print(
            f"line endings in {src.name}: "
            f"{'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
        )
    for mut in MUTATIONS:
        newline = texts[mut["src"]][1]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {', '.join(ALL_FILES)}")
    for mut in mutations:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*ALL_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in mutations:
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
            result = _pytest(*mut["files"], *PYTEST_ARGS)
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
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
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
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_assertion_failure(r)]
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
    if skipped:
        print(
            f"scope: PARTIAL RUN via --only -- {len(mutations)} of "
            f"{len(MUTATIONS)} mutations ran, {skipped} skipped as "
            f"already proven (run-scope rule in the mutation-gate skill)"
        )
    else:
        print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
