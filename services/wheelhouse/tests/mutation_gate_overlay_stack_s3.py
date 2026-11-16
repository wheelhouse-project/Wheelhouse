"""Mutation evidence for the S3 requirement tests (wh-full-refactor.4.1).

Area S3: the bare-number and spoken-click holds of SpeechProcessor
(speech/speech_processor.py). Each mutation names its row BN-n from
docs/design/overlay-stack-requirements.md and the boundary tests of
tests/test_overlay_stack_requirements_s3.py that must catch it.

Same form and the same shared runner as
tests/mutation_gate_overlay_requirements.py (read its docstring for the
disciplines the runner owns). A mutation marked as an expected survivor gives
its reason as survives=; the sweep fails when any test of its selection fails
under it, because the reason is then no longer true.

Run from services/wheelhouse:
  .venv/Scripts/python.exe tests/mutation_gate_overlay_stack_s3.py --check
  .venv/Scripts/python.exe tests/mutation_gate_overlay_stack_s3.py
  .venv/Scripts/python.exe tests/mutation_gate_overlay_stack_s3.py bn-1
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Run pytest with this interpreter, so a worktree builds no new venv."""
    command = [
        sys.executable, "-m", "pytest", *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_runner_failed_names = runner._failed_names


def _failed_names(output):
    """The runner's failed-name set, cut at the first "[" of a parameter id."""
    return {name.split("[", 1)[0] for name in _runner_failed_names(output)}


runner._failed_names = _failed_names

MUTATIONS = []

_T = "test_overlay_stack_requirements_s3.py"
_SP = "speech/speech_processor.py"
_BNH = "speech/bare_number_holds.py"


def sel(*names):
    """The pytest node ids of the named tests."""
    return tuple(f"tests/{_T}::{name}" for name in names)


def add(name, file, old, new, *expect, suite, survives=None):
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


BN_1 = "test_bn_1_number_that_opens_the_utterance_clicks_its_badge"
BN_2 = "test_bn_2_number_filler_before_the_number_clicks_its_badge"
BN_3 = "test_bn_3_number_is_typed_when_no_badges_are_showing"
BN_4 = "test_bn_4_number_outside_1_to_999_is_typed"
BN_5 = "test_bn_5_number_in_the_middle_of_an_utterance_is_typed"
BN_6 = "test_bn_6_word_that_does_not_extend_the_number_types_both"
BN_7 = "test_bn_7_number_with_other_words_before_it_is_not_a_click"
BN_8 = "test_bn_8_held_number_is_typed_when_the_badges_vanish"
BN_9 = "test_bn_9_new_utterance_types_the_held_number_first"
BN_10 = "test_bn_10_dropped_hold_types_nothing_and_clicks_nothing"
BN_11 = "test_bn_11_spoken_click_with_a_number_clicks_the_badge"
BN_12 = "test_bn_12_grid_number_word_clicks_a_badge_when_numbers_show"
BN_13 = "test_bn_13_try_badge_click_answers_for_the_grid_fallback"
BN_3B = "test_bn_3b_number_is_a_click_while_a_refresh_walk_is_in_flight"
BN_14 = "test_bn_14_number_alone_is_typed_when_the_buffer_timer_ends"
BN_16 = "test_bn_16_number_payload_finalized_by_the_timer_is_typed"
BN_18 = "test_bn_18_a_number_click_blocks_a_later_retraction"
BN_17A = "test_bn_17a_retraction_replay_of_a_number_clicks_its_badge"
BN_17B = "test_bn_17b_retraction_marker_types_a_held_number"
BN_17C = "test_bn_17c_lifecycle_reset_marker_clicks_a_held_number"
BN_17D = "test_bn_17d_stop_drops_a_held_number_silently"
BN_9B = "test_bn_9b_buffer_left_open_is_typed_before_the_new_utterance"
BN_13B = "test_bn_13b_badge_click_answers_false_when_the_click_pattern_misses"
BN_14B = "test_bn_14b_numbers_alone_is_typed_at_the_end_marker"
BN_15 = "test_bn_15_spoken_click_hold_clicks_at_the_utterance_end"
BN_15B = "test_bn_15b_held_spoken_click_is_typed_when_no_end_marker_comes"
BN_15C = "test_bn_15c_spoken_click_is_typed_when_no_badges_show"
BN_15E = "test_bn_15e_held_spoken_click_is_typed_before_the_next_utterance"
BN_15F = "test_bn_15f_held_spoken_click_is_typed_before_a_later_word"

# BN-1 / BN-5: only a number that opens the utterance opens a hold.
add("bn-1-opening-word-test-inverted", _BNH,
    "        if not self._host.current_word_starts_utterance:\n"
    "            return False\n"
    "        # wh-number-badge-problems.1.4",
    "        if self._host.current_word_starts_utterance:\n"
    "            return False\n"
    "        # wh-number-badge-problems.1.4",
    BN_1, BN_5, suite=sel(BN_1, BN_5))

# BN-3: no badges showing, no hold; the gate answers the state.
add("bn-3-overlay-gate-always-open", _BNH,
    "        return self._host.overlay_state_value() in (\"painted\", \"refresh_in_flight\")\n",
    "        return True\n",
    BN_3, BN_13, BN_15C, suite=sel(BN_3, BN_13, BN_15C, BN_1))
add("bn-3-overlay-gate-always-closed", _BNH,
    "        return self._host.overlay_state_value() in (\"painted\", \"refresh_in_flight\")\n",
    "        return False\n",
    BN_1, BN_12, suite=sel(BN_1, BN_12, BN_3))
# The refresh_in_flight half of the gate: BN-3b says "show numbers" and leaves
# the walk unanswered, so the overlay stays in that state while a number is spoken.
add("bn-3-overlay-gate-drops-refresh-state", _BNH,
    "        return self._host.overlay_state_value() in (\"painted\", \"refresh_in_flight\")\n",
    "        return self._host.overlay_state_value() in (\"painted\",)\n",
    BN_3B, suite=sel(BN_1, BN_3, BN_3B))

# BN-17: markers from the STT path and shutdown.
add("bn-17a-retraction-replay-never-replays", _SP,
    "            if reason == 'nothing_to_retract' and had_buffered_words:\n",
    "            if False and reason == 'nothing_to_retract' and had_buffered_words:\n",
    BN_17A, suite=sel(BN_17A, BN_1))
add("bn-17b-held-tail-step-skips-retraction-marker", _SP,
    "            and not word_event.is_timeout_finalize_marker\n"
    "            and not word_event.is_lifecycle_reset_marker\n"
    "        ):\n"
    "            if await self._advance_held_tail(word_event):\n",
    "            and not word_event.is_timeout_finalize_marker\n"
    "            and not word_event.is_lifecycle_reset_marker\n"
    "            and not word_event.is_retraction_marker\n"
    "        ):\n"
    "            if await self._advance_held_tail(word_event):\n",
    BN_17B, suite=sel(BN_17B, BN_1))
add("bn-17c-lifecycle-reset-skips-the-consume", _SP,
    "                await self._consume_held_tail_at_utterance_end()\n"
    "                # wh-whole-utterance-command-matching.3: the deferral\n",
    "                pass\n"
    "                # wh-whole-utterance-command-matching.3: the deferral\n",
    BN_17C, suite=sel(BN_17C, BN_1))
add("bn-17d-stop-keeps-the-held-tail", _SP,
    "        self._held_tail = None\n"
    "        self._bare_number_deferred_word = None\n"
    "        # wh-g2-refactor.18: the wh-n8bu holdback",
    "        self._bare_number_deferred_word = None\n"
    "        # wh-g2-refactor.18: the wh-n8bu holdback",
    BN_17D, suite=sel(BN_17D, BN_1))

# BN-18: a bare-number click blocks a later retraction in the same utterance.
add("bn-18-click-does-not-set-the-command-flag", _BNH,
    "            self._host.command_executed_in_utterance = True\n"
    "        return True\n"
    "\n"
    "    async def try_bare_number_badge_click",
    "            self._host.command_executed_in_utterance = False\n"
    "        return True\n"
    "\n"
    "    async def try_bare_number_badge_click",
    BN_18, suite=sel(BN_18, BN_1))

# BN-14: "number" alone is finalized as dictation by the router, at the end
# marker or when the command buffer timer ends; the replacement-prefix release
# deadline then types it. Both cases are in BN-14.
add("bn-14-timer-fallback-drops-the-buffer", "speech/router.py",
    '        return Decision(Action.DICTATE, payload=final_text, reason="Finalized as dictation")\n',
    '        return Decision(Action.IGNORE, reason="Finalized as dictation")\n',
    BN_14, suite=sel(BN_14))

# BN-1: terminal punctuation on the last token is ignored for the parse.
add("bn-1-trailing-punctuation-kept", _BNH,
    "        stripped[-1] = stripped[-1].rstrip(_TRAILING_PUNCT)\n",
    "        stripped[-1] = stripped[-1]\n",
    BN_1, suite=sel(BN_1, BN_5))

# BN-1: a held number grows with each next word that keeps it a number.
add("bn-1-hold-never-extends", _BNH,
    "        # wh-overlay-count-homophones: aliases, so \"twenty\" + \"for\" stays a\n"
    "        # growing number (24) instead of flushing the hold to dictation.\n"
    "        return parse_number_word(\n"
    "            self._spoken_number_text(held + [text]), aliases=True,\n"
    "        ) is not None\n",
    "        return False\n",
    BN_1, suite=sel(BN_1, BN_6))
add("bn-1-extension-ignores-aliases", _BNH,
    "            self._spoken_number_text(held + [text]), aliases=True,\n",
    "            self._spoken_number_text(held + [text]), aliases=False,\n",
    BN_1, suite=sel(BN_1, BN_6))
add("bn-1-extension-appends-nothing", _BNH,
    "        held.append(text)\n"
    "        pipeline_logger.info(\n"
    "            \"BARE-NUMBER candidate extended",
    "        pipeline_logger.info(\n"
    "            \"BARE-NUMBER candidate extended",
    BN_1, suite=sel(BN_1, BN_6))

# BN-6: a word that does not extend the number types the held words first.
# The consume-time parse check types the same words, one per held word, so a
# hold that grows without the guard types the same text.
add("bn-6-extension-guard-always-true", _BNH,
    "        # wh-overlay-count-homophones: aliases, so \"twenty\" + \"for\" stays a\n"
    "        # growing number (24) instead of flushing the hold to dictation.\n"
    "        return parse_number_word(\n"
    "            self._spoken_number_text(held + [text]), aliases=True,\n"
    "        ) is not None\n",
    "        return True\n",
    BN_6, suite=sel(BN_6, BN_1),
    survives="a hold that grows over a non-number word is typed word by word "
             "at consume time, because the held words then parse to no number; "
             "the typed words are the same as the flush gives")
add("bn-6-flush-types-nothing", _BNH,
    "        for held_word in pending:\n"
    "            await self._host.send_to_dictation(held_word)\n"
    "\n"
    "    async def consume_pending_bare_number(self) -> None:\n",
    "        for held_word in []:\n"
    "            await self._host.send_to_dictation(held_word)\n"
    "\n"
    "    async def consume_pending_bare_number(self) -> None:\n",
    BN_6, BN_9, suite=sel(BN_6, BN_9, BN_1))

# BN-7 / BN-2: a finalized multi-word payload clicks only when it is the whole
# utterance and the number word list parses. In BN-7 "hello number 74",
# "hello" is typed, "number" opens a replacement buffer, and the end-of-utterance
# flag on the last word "74" finalizes "number 74" as dictation (router step 1,
# before the end marker): the payload is the tail of the utterance.
add("bn-7-whole-utterance-test-dropped", _SP,
    "                and self.bare_number_holds.payload_is_the_whole_utterance(decision.payload)\n"
    "            ):\n"
    "                if await self.bare_number_holds.try_bare_number_badge_click(decision.payload):\n",
    "                and True\n"
    "            ):\n"
    "                if await self.bare_number_holds.try_bare_number_badge_click(decision.payload):\n",
    BN_7, suite=sel(BN_7, BN_2))
add("bn-2-finalized-payload-never-clicks", _SP,
    "                if await self.bare_number_holds.try_bare_number_badge_click(decision.payload):\n"
    "                    await self._send_pending_utterance_end()\n"
    "                    return\n",
    "                if False and await self.bare_number_holds.try_bare_number_badge_click(decision.payload):\n"
    "                    await self._send_pending_utterance_end()\n"
    "                    return\n",
    BN_2, BN_1, suite=sel(BN_2, BN_1, BN_7))
# BN-16: the timer finalization of a multi-word payload must not click.
add("bn-2-click-without-confirmed-end", _SP,
    "                and self.bare_number_holds.utterance_end_is_confirmed(word_event)\n"
    "                and self.bare_number_holds.payload_is_the_whole_utterance(decision.payload)\n",
    "                and True\n"
    "                and self.bare_number_holds.payload_is_the_whole_utterance(decision.payload)\n",
    BN_16, suite=sel(BN_16, BN_2, BN_7))

# BN-8: the overlay is checked again when the held number is consumed.
add("bn-8-consume-skips-the-overlay-check", _BNH,
    "        if not self._overlay_accepts_bare_number():\n"
    "            pipeline_logger.info(\n"
    "                \"BARE-NUMBER overlay no longer showing badges; dictating \"\n",
    "        if False:\n"
    "            pipeline_logger.info(\n"
    "                \"BARE-NUMBER overlay no longer showing badges; dictating \"\n",
    BN_8, suite=sel(BN_8, BN_1))

# BN-9: a new utterance start types the held number first. The first site
# runs when a new utterance starts while a buffer of the previous utterance is
# still open, because its end marker did not arrive (BN-9b).
add("bn-9-new-utterance-keeps-the-hold", _SP,
    "            await self.bare_number_holds.flush_pending_bare_number_as_dictation()\n"
    "\n"
    "        # Delegate decision to Router\n",
    "            pass\n"
    "\n"
    "        # Delegate decision to Router\n",
    BN_9B, suite=sel(BN_9, BN_9B, BN_1))
add("bn-9-held-tail-step-keeps-the-hold", _SP,
    "        else:\n"
    "            await self.bare_number_holds.flush_pending_bare_number_as_dictation()\n"
    "        return False\n",
    "        else:\n"
    "            pass\n"
    "        return False\n",
    BN_9, BN_6, suite=sel(BN_9, BN_6, BN_1))

# BN-10: a dropped hold is neither typed nor clicked.
add("bn-10-drop-keeps-the-hold", _BNH,
    "        self._host.pending_bare_number_words = None\n"
    "        self._host.bare_number_deferred_word = None\n"
    "        if pending or deferred:\n",
    "        self._host.bare_number_deferred_word = None\n"
    "        if pending or deferred:\n",
    BN_10, suite=sel(BN_10, BN_1))

# BN-4: numbers outside 1..999 are typed. The range lives in the parser.
add("bn-4-number-range-too-wide", "speech/number_word_parser.py",
    "_MAX = 999\n", "_MAX = 9999\n",
    BN_4, suite=sel(BN_4, BN_1))
# Two layers guard BN-4 in the processor: the parse at hold time and the parse
# at consume time. Removing one alone leaves the other, which types the words.
add("bn-4-hold-time-parse-dropped", _BNH,
    "            parse_number_word(bare, aliases=True) is None\n",
    "            False\n",
    BN_4, suite=sel(BN_4, BN_1),
    survives="the consume-time parse of _held_bare_number_command still "
             "returns no command for such words, so they are typed anyway")

# BN-11 / BN-15: with the shipped click pattern the router executes a spoken
# "click N" itself (Router, _execute_decision EXECUTE branch, TextParser,
# ActionFunctions.click_element, the overlay controller). That path runs
# outside the 16 S3 methods, so no S3 mutation names test_bn_11 as its catcher.
# When a user pattern override makes the click pattern need the safety word,
# the router finalizes "click 74" as dictation: at the last word when it
# carries the end-of-utterance flag (as in say()), or at the end marker after
# "Impossible buffer deferred to utterance end" when it does not. The
# spoken-click hold then makes the badge click (BN-15).
add("bn-11-spoken-click-hold-never-opens", _BNH,
    "        tokens = text.split()\n"
    "        if len(tokens) < 2:\n"
    "            return False\n"
    "        if tokens[0].casefold() not in self._SPOKEN_CLICK_VERBS:\n",
    "        tokens = text.split()\n"
    "        return False\n"
    "        if tokens[0].casefold() not in self._SPOKEN_CLICK_VERBS:\n",
    BN_15B, suite=sel(BN_15, BN_15B, BN_11, BN_1))
# Measured: with the hold gone, BN-15 still passes, because the finalized-payload
# check of the DICTATE branch clicks the whole-utterance payload "click 74"
# through try_bare_number_badge_click (the last word carries the end flag, so
# the end counts as confirmed). BN-15b catches it: without the hold, nothing
# waits for the release deadline.
# BN-15: the held spoken click names its badge. With no command text, no badge
# is clicked (measured: assert [] == ['snap-1-item-74'] in all 4 cases); the
# finalized-payload check uses the same helper, so it cannot click instead.
add("bn-15-spoken-click-names-no-command", _BNH,
    "            return None if number is None else f\"click {number}\"\n",
    "            return None\n",
    BN_15, suite=sel(BN_15, BN_11, BN_1))
# BN-15b: the release deadline types a held spoken click when no end marker comes.
add("bn-15-release-deadline-never-armed", _BNH,
    "            return\n"
    "        self._host.start_timeout(self._host.command_timeout_ms)\n"
    "\n"
    "    def _held_bare_number_command",
    "            return\n"
    "        pass\n"
    "\n"
    "    def _held_bare_number_command",
    BN_15B, suite=sel(BN_15B, BN_15))
add("bn-15-deadline-keeps-the-hold", _SP,
    "                    \"held; dictating it\",\n"
    "                )\n"
    "                await self.bare_number_holds.flush_pending_bare_number_as_dictation()\n"
    "                return\n",
    "                    \"held; dictating it\",\n"
    "                )\n"
    "                pass\n"
    "                return\n",
    BN_15B, suite=sel(BN_15B, BN_15))

# BN-12 / BN-13: the grid fallback calls the public badge click.
add("bn-12-grid-fallback-never-clicks", _BNH,
    "        if not self._overlay_accepts_bare_number():\n"
    "            return False\n"
    "        return await self._execute_bare_number_click(spoken.split())\n",
    "        return False\n",
    BN_12, BN_13, BN_1, suite=sel(BN_12, BN_13, BN_1))
add("bn-13-badge-click-answers-true-without-a-click", _BNH,
    "        return await self._execute_bare_number_click(spoken.split())\n",
    "        return True\n",
    BN_13, BN_12, suite=sel(BN_13, BN_12))
add("bn-13-badge-click-skips-the-overlay-gate", _BNH,
    "        if not self._overlay_accepts_bare_number():\n"
    "            return False\n"
    "        return await self._execute_bare_number_click(spoken.split())\n",
    "        return await self._execute_bare_number_click(spoken.split())\n",
    BN_13, suite=sel(BN_13, BN_12))
# BN-13b: a click command that runs nothing answers False.
add("bn-13-unexecuted-click-answers-true", _BNH,
    "        if not executed:\n"
    "            return False\n"
    "        if self._host.text_parser.last_executed_pattern_type == \"command\":\n",
    "        if False:\n"
    "            return False\n"
    "        if self._host.text_parser.last_executed_pattern_type == \"command\":\n",
    BN_13B, suite=sel(BN_13B, BN_13))

# BN-14b: a held filler that names no number is typed, never clicked by name.
add("bn-14b-filler-hold-clicks-by-name", _BNH,
    "        ) is None:\n"
    "            return None\n"
    "        return f\"click {' '.join(held)}\"\n",
    "        ) is None:\n"
    "            pass\n"
    "        return f\"click {' '.join(held)}\"\n",
    BN_14B, suite=sel(BN_14B, BN_1))

# wh-full-refactor.4.2: a hold that the end marker's finalization opens types
# its words before end_utterance. Each mutation puts the old order back.
BN_14B_ORDER = "test_bn_14b_typed_word_leaves_before_end_utterance"
BN_15_ORDER = "test_bn_15_held_click_typed_as_spoken_leaves_before_end_utterance"
add("bn-14b-hold-arm-sends-the-end-before-the-consume", _SP,
    "                self.bare_number_holds.arm_spoken_click_release()\n"
    "                return\n",
    "                self.bare_number_holds.arm_spoken_click_release()\n"
    "                await self._send_pending_utterance_end()\n"
    "                return\n",
    BN_14B_ORDER, BN_15_ORDER,
    suite=sel(BN_14B_ORDER, BN_15_ORDER, BN_14B, BN_15, BN_1))
add("bn-14b-end-marker-sends-the-end-before-the-tail", _SP,
    "                await self._consume_finalization_armed_tail()\n"
    "                # wh-full-refactor.4.2: the bare-number and spoken-click\n",
    "                await self._send_pending_utterance_end()\n"
    "                await self._consume_finalization_armed_tail()\n"
    "                # wh-full-refactor.4.2: the bare-number and spoken-click\n",
    BN_14B_ORDER, BN_15_ORDER,
    suite=sel(BN_14B_ORDER, BN_15_ORDER, BN_14B, BN_15, BN_1))
# The extension arm never runs inside the end marker's finalization: the
# branch's entry consume clears the deferred word first (bd comment EXTENSION
# HOLD on wh-full-refactor.4.2). The send there finds no pending end.
add("bn-1-extension-arm-sends-the-end", _SP,
    "                if self.bare_number_holds.extend_bare_number_hold(decision.payload):\n"
    "                    return\n",
    "                if self.bare_number_holds.extend_bare_number_hold(decision.payload):\n"
    "                    await self._send_pending_utterance_end()\n"
    "                    return\n",
    suite=sel(BN_14B_ORDER, BN_15_ORDER, BN_1, BN_6),
    survives="the extension arm runs only on a word-driven decision, where "
             "no end_utterance is pending, so the added send does nothing")


# BN-15e / BN-15f (stage C, wh-full-refactor.4.4): a spoken-click hold keeps
# the spoken payload as ONE held word, so every dictation fallback types it
# in one text (wh-number-badge-problems.1.3).
add("bn-15e-spoken-click-held-as-separate-words", _BNH,
    "        self._host.pending_bare_number_words = [text]\n"
    "        pipeline_logger.info(\n"
    "            \"BARE-NUMBER candidate held from spoken click text=%r \"\n",
    "        self._host.pending_bare_number_words = text.split()\n"
    "        pipeline_logger.info(\n"
    "            \"BARE-NUMBER candidate held from spoken click text=%r \"\n",
    BN_15E, BN_15F, suite=sel(BN_15E, BN_15F, BN_15))


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
