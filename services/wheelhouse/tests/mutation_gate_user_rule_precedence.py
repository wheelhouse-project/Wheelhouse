"""Mutation gate for the person's own rules running before the built-ins.

wh-user-rule-precedence, acceptance item 8 (David's answer to
QUESTIONS-2026-09-28.md item 12, option three; boss rulings R1 and R2).

WHAT THE CHANGE DOES.

  Order    PatternCatalog._merge_entries settles every slot first, then
           moves every user entry that ends in no system slot (the rows
           past the shipped count) in front of every shipped entry,
           sorted by position in the user file, not by the order the loop
           appended them. Entries holding a built-in's slot stay there.
  Save     PatternManager._stands_in_front_for_save asks, with the merge's
           own identity rule (_claimed_builtin), whether the block a save
           writes will hold no slot: a valid doc_id naming a shipped entry
           holds one; an own rule (origin "user", no doc_id) never does;
           a block with neither key holds one only when exactly one
           shipped entry carries its text; a doc_id naming nothing holds
           none. create_pattern and update_pattern build the block's
           identity with _saved_identity; the Try-it preview
           (speech/pattern_tester.py::_simulate_save) asks about its
           simulated block.
  Flag     PatternManager._whole_utterance_for_save copies
           whole_utterance_only from the built-ins whose words the rule
           takes (_sample_phrases, _taken_builtin_flags, with the
           runtime's punctuation retry) only for a save that stands in
           front; a slot-keeping save keeps the stored value.
           requires_hotword is never copied. The sample phrases of an
           advanced expression include the phrases enumerated from its
           parsed form (_enumerated_phrases, wh-user-rule-precedence.2.1),
           so '^show(?: desktop)?$' also samples 'show desktop'.

WHAT IT COVERS. Every mutation names the tests that must fail. The shared
selection is the three files that pin this behaviour; the ^-guard mutation
runs the two older tests that pin the flag being dropped on a replacement.

Mutations considered and left out, with the reason:

  _sample_phrases mode fullmatch -> search
                    the wordings are the expression's own display text,
                    so a ^-anchored expression that answers a wording by
                    search and not by fullmatch needs text the display
                    never produces; no reachable input was found
  first-match break removed in _taken_builtin_flags
                    the any() over the list makes it visible only for a
                    phrase two shipped rules with different flags both
                    answer
  _normalize_first_word_in_text dropped in _taken_builtin_flags
                    the retry's interior-word pass strips the first word
                    too, so no sample phrase was found whose answer
                    changes; the call stays to mirror the runtime
  _claimed_builtin and legacy_candidates themselves
                    shared with the merge and the listing badge; the
                    doc_id identity gate
                    (tests/mutation_gate_pattern_doc_id_identity.py)
                    mutates them. This gate mutates the save decision's
                    own inputs to them instead.
  the 'count == low' re-raise in _enumerate_item removed (so a repeat
  whose minimum count already passes the length guard gives no phrases
  instead of raising)
                    visible only where that repeat sits in an alternation
                    or an enclosing optional group or repeat:
                    '^(?:select all|(?:bbbbbbbbbb){30})$' then gives
                    ['select all'] and '^go(?: (?:bbbbbbbbbb){30})?$' gives
                    ['go'], where the shipped code gives []. The mutant's
                    extra phrases are ones the expression answers, so a
                    test that failed on them would pin the documented
                    "gives nothing" limit, not a user-visible behaviour
  the phrase-cap break in the repeat loop removed
                    _enumerate_union cuts the result to the same 64
                    phrases, so the break changes only how much work the
                    loop does, never the phrases it gives

Run from services/wheelhouse with the interpreter the whole worktree uses.
In a worktree that is the main checkout's interpreter, so <main> below is
the main checkout's root:

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_user_rule_precedence.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_user_rule_precedence.py

pytest starts as ``<this interpreter> -m pytest`` in the service directory,
never through scripts/run_tests.py, whose ``uv run`` builds a .venv inside a
git worktree. The shared runner
(services/stt_providers/shared/tests/mutation_gate_runner.py) owns
everything else: exactly-one pattern matches, compiled mutants, the
expected-name check against real collection, the green baseline, the
per-run timeout, byte-exact restoration, and the short-summary verdicts.

This gate adds one thing on top: after each run it prints the short-summary
FAILED lines, reason included (the runner sets COLUMNS=1000), so a reader
can confirm the expected assertion fired rather than an unrelated error.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
MANAGER = SERVICE / "speech/pattern_manager.py"
CATALOG = SERVICE / "speech/pattern_catalog.py"
TESTER = SERVICE / "speech/pattern_tester.py"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner  # noqa: E402


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_plain_run_pytest = runner._run_pytest


def _run_pytest_showing_reasons(service, test_file):
    """The runner's own run, then the short-summary FAILED lines printed.

    Verdicts still come from the runner. This only shows the reason each
    test failed, so an AttributeError or an ImportError upstream of the
    guarded assertion cannot pass as a catch unseen.
    """
    result = _plain_run_pytest(service, test_file)
    if result is not None and result.returncode:
        lines = (result.stdout + result.stderr).splitlines()
        for index, line in enumerate(lines):
            if "short test summary info" in line:
                for failed in lines[index + 1:]:
                    if failed.startswith("FAILED"):
                        print("    " + failed.split("::", 1)[-1][:300])
                break
    return result


runner._run_pytest = _run_pytest_showing_reasons

TESTS = (
    "tests/test_pattern_user_rule_precedence.py",
    "tests/test_pattern_tester_save_agreement.py",
    "tests/test_pattern_override_trigger_move.py",
)
ANCHOR_TESTS = (
    "tests/test_pattern_manager.py::TestCreatePattern"
    "::test_create_drops_flag_on_replacement_expression",
    "tests/test_pattern_manager_update.py::TestUpdatePattern"
    "::test_flag_dropped_when_update_makes_replacement",
)

# --- expected catchers ------------------------------------------------------
# test_pattern_user_rule_precedence.py: placement
MOVED_WINS = "test_an_edited_builtin_moved_onto_another_builtins_words_answers"
OWN_MOVED_EDIT = "test_a_moved_edit"
OWN_ADDED = "test_an_added_rule"
OWN_DUPLICATE = "test_a_duplicate"
OWN_BEFORE_CHANGE = "test_a_rule_saved_before_this_change"
OWN_REPLACEMENT = "test_a_user_replacement_rule"
OWN_FIRST_IN_FILE = (
    "test_the_first_own_rule_in_the_file_answers_a_shared_phrase")
SLOT_KEPT_WORDS = "test_an_edit_that_kept_its_words"
SLOT_DISABLED = "test_a_disabled_override"
SLOT_LEGACY = "test_an_unambiguous_customization_saved_before_ids"
ORDER_SLOTS_STAY = "test_slot_less_rules_lead_in_file_order_and_slots_stay"
ORDER_DISPLACED = "test_a_displaced_claimant_sits_at_its_own_file_position"
ORDER_NO_SINGLE = "test_entries_that_name_no_single_builtin_lead"
ORDER_OWN_TEXT = (
    "test_an_own_rule_with_a_builtins_text_does_not_take_its_slot")
PREVIEW_CREATE = "test_a_create"
PREVIEW_UPDATE = "test_an_update"
# test_pattern_user_rule_precedence.py: the whole-utterance copy
EDIT_GETS_FLAG = "test_an_edit_moved_onto_a_whole_utterance_builtin_gets_the_flag"
CUSTOMIZE_GETS_FLAG = (
    "test_a_customize_moved_onto_a_whole_utterance_builtin_gets_the_flag")
EDIT_DROPS_FLAG = "test_an_edit_moved_onto_a_builtin_without_the_flag_drops_it"
CUSTOMIZE_DROPS_FLAG = (
    "test_a_customize_moved_onto_a_builtin_without_the_flag_drops_it")
ANY_TAKEN = "test_any_taken_builtin_with_the_flag_sets_it"
UNTAKEN_KEEPS = "test_words_no_builtin_takes_keep_the_carried_value"
ADD_COPIES = "test_an_add_onto_a_whole_utterance_builtins_words"
DUPLICATE_COPIES = "test_a_duplicate_moved_onto_a_builtin_without_the_flag"
DUPLICATE_ON_SAVE = "test_a_duplicate_that_keeps_its_builtins_words"
ADDED_EDIT_COPIES = (
    "test_an_edit_of_an_added_rule_moved_onto_a_whole_utterance_builtin")
OWN_ON_BUILTIN_TEXT = (
    "test_an_edit_of_an_own_rule_onto_a_builtins_own_expression")
ADD_ON_BUILTIN_TEXT = (
    "test_an_add_with_a_builtins_own_expression_copies_the_flag")
OLD_RULE_COPIES = "test_a_rule_saved_before_this_change_copies_on_its_next_save"
SLOT_CLEARED_BY_HAND = (
    "test_an_unchanged_trigger_edit_with_the_flag_cleared_by_hand")
SLOT_CUSTOMIZE_KEEPS = "test_a_customize_that_keeps_its_words_keeps_the_argument"
SLOT_BEFORE_IDS = "test_a_customization_saved_before_ids"
UNKNOWN_ID_COPIES = "test_a_doc_id_naming_no_builtin_copies_the_flag"
HOTWORD_CHECKED = "test_a_checked_box_stays_on_words_whose_builtin_has_none"
HOTWORD_CLEAR = "test_a_clear_box_stays_on_words_whose_builtin_needs_the_hotword"
HOTWORD_ADD = "test_an_add_keeps_a_clear_box"
PREVIEW_FLAG = "test_the_preview_row_carries_the_saved_flag"
PREVIEW_PLAIN_FLAG = "test_a_plain_advanced_expression_previews_the_saved_flag"
PREVIEW_OWN_FLAG = "test_an_own_rule_previews_the_saved_flag"
RUNTIME_DESKTOP = "test_the_rule_on_desktop_words_is_whole_utterance_only"
RUNTIME_CLOSE = "test_the_rule_on_close_window_words_is_not"
RUNTIME_ADDED = "test_an_added_rule_on_desktop_words_answers_first_with_the_flag"
# test_pattern_user_rule_precedence.py: the expression enumeration
OPTIONAL_ADD = "test_an_add_of_an_optional_group"
OPTIONAL_EDIT = "test_a_moved_edit_to_an_optional_group"
OPTIONAL_PREVIEW = "test_the_preview_of_an_optional_group"
FINITE_ADD = "test_an_add_of_a_finite_range"
FINITE_PREVIEW = "test_the_preview_of_a_finite_range"
ENUMERATED = "test_the_enumerated_phrases"
ENUMERATION_CAP = "test_the_enumeration_cap"
ENUMERATION_UNSUPPORTED = "test_an_unsupported_construct_gives_nothing"
LONG_ADD = "test_an_add_of_a_long_range"
LONG_PREVIEW = "test_the_preview_of_a_long_range"
PRODUCT_RAISES = "test_a_product_with_no_phrase_within_the_length_guard_raises"
PRODUCT_KEEPS = "test_a_product_keeps_the_phrases_within_the_length_guard"
DISPLAY_STILL_SAMPLED = (
    "test_the_display_still_samples_what_the_enumeration_cannot_read")
# test_pattern_tester_save_agreement.py
CLAIMANT_FIRST = "test_a_claimant_moved_onto_a_builtins_expression_answers_first"
# the two older replacement-flag tests
CREATE_REPLACEMENT = "test_create_drops_flag_on_replacement_expression"
UPDATE_REPLACEMENT = "test_flag_dropped_when_update_makes_replacement"


def mutation(name, target, old, new, expect, tests=TESTS):
    return dict(name=name, service=SERVICE, test_file=tests, file=target,
                old=old, new=new, expect=expect)


# The three statements at the end of _merge_entries, quoted whole by the
# mutations that rewrite more than one of them.
_SORT = (
    "        leading = sorted(\n"
    "            merged[system_count:], key=lambda entry: file_position[id(entry)],\n"
    "        )\n"
)
_RETURN = "        return leading + merged[:system_count]\n"

# The last count a repeat is expanded at, in _enumerate_item; quoted by the
# repeat mutations.
_LAST_COUNT = "            last = low + 1 if high == parser.MAXREPEAT else high\n"

MUTATIONS = [
    # --- the merge order (_merge_entries) -----------------------------------
    # The rows past the shipped count stay where the loop appended them,
    # after every built-in: the order before this bead.
    mutation("c-front-move-removed", CATALOG,
             _RETURN,
             "        return merged\n",
             [MOVED_WINS, OWN_MOVED_EDIT, OWN_ADDED, OWN_DUPLICATE,
              OWN_BEFORE_CHANGE, OWN_REPLACEMENT, OWN_FIRST_IN_FILE,
              SLOT_KEPT_WORDS, ORDER_SLOTS_STAY, ORDER_DISPLACED,
              ORDER_NO_SINGLE, ORDER_OWN_TEXT, PREVIEW_CREATE,
              PREVIEW_UPDATE, CLAIMANT_FIRST]),
    # Sorted into file order, but placed after the built-ins.
    mutation("c-front-move-placed-at-the-end", CATALOG,
             _RETURN,
             "        return merged[:system_count] + leading\n",
             [MOVED_WINS, OWN_MOVED_EDIT, OWN_ADDED, OWN_DUPLICATE,
              OWN_BEFORE_CHANGE, OWN_REPLACEMENT, OWN_FIRST_IN_FILE,
              SLOT_KEPT_WORDS, ORDER_SLOTS_STAY, ORDER_DISPLACED,
              ORDER_NO_SINGLE, ORDER_OWN_TEXT, PREVIEW_CREATE,
              PREVIEW_UPDATE, CLAIMANT_FIRST]),
    mutation("c-front-order-reversed", CATALOG,
             "            merged[system_count:], key=lambda entry: file_position[id(entry)],\n",
             "            merged[system_count:], key=lambda entry: file_position[id(entry)],\n"
             "            reverse=True,\n",
             [OWN_FIRST_IN_FILE, ORDER_SLOTS_STAY, ORDER_DISPLACED,
              ORDER_NO_SINGLE]),
    # The order the loop appended them in: a displaced claimant lands
    # after every entry the loop reached before its displacement.
    mutation("c-front-order-is-append-order", CATALOG,
             _SORT,
             "        leading = merged[system_count:]\n",
             [ORDER_DISPLACED]),
    # The first slot-less row stays behind the built-ins.
    mutation("c-front-slice-starts-one-late", CATALOG,
             "        system_count = len(system_entries)\n",
             "        system_count = len(system_entries) + 1\n",
             [MOVED_WINS, OWN_ADDED, OWN_DUPLICATE, ORDER_SLOTS_STAY,
              ORDER_OWN_TEXT]),
    # The last shipped row moves in front too. A shipped row has no file
    # position, so the mutant's key gives it the last one; indexing it
    # directly would raise KeyError before any order assertion ran, which
    # is a false catch.
    mutation("c-front-slice-starts-one-early", CATALOG,
             "        system_count = len(system_entries)\n"
             "        file_position = {\n"
             "            id(entry): position for position, entry in enumerate(user_entries)\n"
             "        }\n" + _SORT,
             "        system_count = len(system_entries) - 1\n"
             "        file_position = {\n"
             "            id(entry): position for position, entry in enumerate(user_entries)\n"
             "        }\n"
             "        leading = sorted(\n"
             "            merged[system_count:],\n"
             "            key=lambda entry: file_position.get(id(entry), len(user_entries)),\n"
             "        )\n",
             [ORDER_SLOTS_STAY, SLOT_KEPT_WORDS, SLOT_DISABLED,
              ORDER_NO_SINGLE]),
    # Every user entry moves in front, slot holders included.
    mutation("c-slot-holders-move-too", CATALOG,
             _SORT + _RETURN,
             "        leading = sorted(\n"
             "            [entry for entry in merged if id(entry) in file_position],\n"
             "            key=lambda entry: file_position[id(entry)],\n"
             "        )\n"
             "        return leading + [\n"
             "            entry for entry in merged[:system_count]\n"
             "            if id(entry) not in file_position\n"
             "        ]\n",
             [ORDER_SLOTS_STAY, SLOT_KEPT_WORDS, SLOT_LEGACY]),

    # --- the save decision (_stands_in_front_for_save) ----------------------
    # Every save stands in front, so every save copies the flag.
    mutation("s-every-save-stands-in-front", MANAGER,
             "        return claimed is None\n",
             "        return True\n",
             [SLOT_CLEARED_BY_HAND, SLOT_CUSTOMIZE_KEEPS, SLOT_BEFORE_IDS]),
    # No save stands in front, so no save copies the flag.
    mutation("s-no-save-stands-in-front", MANAGER,
             "        return claimed is None\n",
             "        return False\n",
             [EDIT_GETS_FLAG, CUSTOMIZE_GETS_FLAG, EDIT_DROPS_FLAG,
              CUSTOMIZE_DROPS_FLAG, ANY_TAKEN, ADD_COPIES, DUPLICATE_COPIES,
              DUPLICATE_ON_SAVE, ADDED_EDIT_COPIES, OWN_ON_BUILTIN_TEXT,
              OLD_RULE_COPIES, UNKNOWN_ID_COPIES, PREVIEW_FLAG,
              PREVIEW_PLAIN_FLAG, PREVIEW_OWN_FLAG, RUNTIME_DESKTOP,
              RUNTIME_CLOSE, RUNTIME_ADDED]),
    # Branch 1: a valid doc_id naming a shipped entry no longer finds it.
    mutation("s-shipped-doc-id-holds-no-slot", MANAGER,
             "                if entry_identity(entry) == user_key\n",
             "                if False\n",
             [SLOT_CLEARED_BY_HAND, SLOT_CUSTOMIZE_KEEPS]),
    # Branch 2: the own-rule mark is not consulted, so an own rule with a
    # built-in's exact text reads as a customization saved before ids.
    # The catchers write save's exact expression '^save$'; a simple-mode
    # draft of "save" builds a different expression and never reaches the
    # legacy text match.
    mutation("s-own-rule-mark-ignored", MANAGER,
             "            block, system_keys, entry_text_candidates(system_entries),\n",
             "            {key: value for key, value in block.items()\n"
             "             if key != ORIGIN_KEY},\n"
             "            system_keys, entry_text_candidates(system_entries),\n",
             [EDIT_GETS_FLAG, ADD_ON_BUILTIN_TEXT, OWN_ON_BUILTIN_TEXT]),
    # Branch 3: a block with neither key never finds its one legacy
    # candidate.
    mutation("s-legacy-candidate-ignored", MANAGER,
             "            block, system_keys, entry_text_candidates(system_entries),\n",
             "            block, system_keys, {},\n",
             [SLOT_BEFORE_IDS]),
    # Branch 4: a doc_id that names nothing shipped is read as a slot.
    mutation("s-unknown-doc-id-holds-a-slot", MANAGER,
             "        return claimed is None\n",
             "        return claimed is None and not is_valid_doc_id(\n"
             "            block.get(DOC_ID_KEY))\n",
             [UNKNOWN_ID_COPIES]),
    # _saved_identity leaves out the saved doc_id, so a slot-keeping save
    # reads as slot-less.
    mutation("s-saved-identity-drops-the-doc-id", MANAGER,
             "        if doc_id is not None:\n"
             "            block[DOC_ID_KEY] = doc_id\n",
             "        if False:\n"
             "            block[DOC_ID_KEY] = doc_id\n",
             [SLOT_CLEARED_BY_HAND, SLOT_CUSTOMIZE_KEEPS]),
    # _saved_identity leaves out the origin line the save writes.
    mutation("s-saved-identity-drops-the-origin", MANAGER,
             "        if own_rule:\n"
             "            block[ORIGIN_KEY] = ORIGIN_OWN\n",
             "        if False:\n"
             "            block[ORIGIN_KEY] = ORIGIN_OWN\n",
             [EDIT_GETS_FLAG, ADD_ON_BUILTIN_TEXT, OWN_ON_BUILTIN_TEXT]),
    # create_pattern asks about a block without the doc_id it writes.
    mutation("s-create-asks-without-the-saved-doc-id", MANAGER,
             "                    self._doc_id_for_save(doc_id, regex, system_entries),\n"
             "                    True,\n",
             "                    None,\n"
             "                    True,\n",
             [SLOT_CUSTOMIZE_KEEPS]),
    # create_pattern asks about a block without the origin it writes.
    mutation("s-create-asks-without-the-origin", MANAGER,
             "                    self._doc_id_for_save(doc_id, regex, system_entries),\n"
             "                    True,\n",
             "                    self._doc_id_for_save(doc_id, regex, system_entries),\n"
             "                    False,\n",
             [ADD_ON_BUILTIN_TEXT]),
    # update_pattern asks about a block without the origin it keeps.
    mutation("s-update-asks-without-the-origin", MANAGER,
             "                    original_origin == ORIGIN_OWN,\n",
             "                    False,\n",
             [OWN_ON_BUILTIN_TEXT]),

    # --- the whole-utterance copy (item 4) ----------------------------------
    # A slot-keeping save copies too.
    mutation("w-slot-keeping-save-copies", MANAGER,
             "        if in_front:\n",
             "        if True:\n",
             [SLOT_CLEARED_BY_HAND, SLOT_CUSTOMIZE_KEEPS, SLOT_BEFORE_IDS]),
    mutation("w-create-skips-the-copy", MANAGER,
             "                    in_front, regex, system_entries,\n"
             "                    whole_utterance_only,\n",
             "                    False, regex, system_entries,\n"
             "                    whole_utterance_only,\n",
             [CUSTOMIZE_GETS_FLAG, CUSTOMIZE_DROPS_FLAG, ADD_COPIES,
              DUPLICATE_COPIES, DUPLICATE_ON_SAVE, RUNTIME_ADDED]),
    mutation("w-update-skips-the-copy", MANAGER,
             "                    in_front, regex, system_entries,\n"
             "                    original_whole_utterance,\n",
             "                    False, regex, system_entries,\n"
             "                    original_whole_utterance,\n",
             [EDIT_GETS_FLAG, EDIT_DROPS_FLAG, ANY_TAKEN, ADDED_EDIT_COPIES,
              OWN_ON_BUILTIN_TEXT, OLD_RULE_COPIES, UNKNOWN_ID_COPIES,
              RUNTIME_DESKTOP, RUNTIME_CLOSE]),
    mutation("w-create-searches-the-user-file", MANAGER,
             "                    in_front, regex, system_entries,\n"
             "                    whole_utterance_only,\n",
             "                    in_front, regex,\n"
             "                    self._load_pattern_dicts(self.user_patterns_file),\n"
             "                    whole_utterance_only,\n",
             [CUSTOMIZE_GETS_FLAG, CUSTOMIZE_DROPS_FLAG]),
    mutation("w-update-searches-the-user-file", MANAGER,
             "                    in_front, regex, system_entries,\n"
             "                    original_whole_utterance,\n",
             "                    in_front, regex, toml_patterns,\n"
             "                    original_whole_utterance,\n",
             [EDIT_GETS_FLAG, EDIT_DROPS_FLAG, ANY_TAKEN]),
    mutation("w-any-becomes-all", MANAGER,
             "                value = any(taken)\n",
             "                value = all(taken)\n",
             [ANY_TAKEN]),
    mutation("w-taken-flags-ignored", MANAGER,
             "            if taken:\n"
             "                value = any(taken)\n",
             "            if False:\n"
             "                value = any(taken)\n",
             [EDIT_GETS_FLAG, CUSTOMIZE_GETS_FLAG, EDIT_DROPS_FLAG,
              CUSTOMIZE_DROPS_FLAG, PREVIEW_FLAG, RUNTIME_DESKTOP,
              RUNTIME_CLOSE]),
    mutation("w-sample-match-filter-dropped", MANAGER,
             "            return [\n"
             "                wording for wording in wordings\n"
             "                if match_bounded(\n",
             "            return list(wordings)\n"
             "            return [\n"
             "                wording for wording in wordings\n"
             "                if match_bounded(\n",
             [UNTAKEN_KEEPS]),
    # '^desktop$' is also enumerated since wh-user-rule-precedence.2.1, so
    # the catcher is an expression the enumeration cannot read.
    mutation("w-strip-display-fallback-removed", MANAGER,
             "            stripped = cls._strip_display(pattern)\n"
             "            wordings = [stripped] if stripped else []\n",
             "            wordings = []\n",
             [DISPLAY_STILL_SAMPLED]),
    # The ^ guard predates this bead (wh-int8-punctuation-mishears.1.5); the
    # branch moved it into _whole_utterance_for_save, so the two tests that
    # pin it are the catchers.
    mutation("w-anchor-guard-dropped", MANAGER,
             '        if not pattern.startswith("^"):\n'
             "            return False\n"
             "        value = carried is True\n",
             "        value = carried is True\n",
             [CREATE_REPLACEMENT, UPDATE_REPLACEMENT], tests=ANCHOR_TESTS),
    mutation("w-builtin-fullmatch-becomes-search", MANAGER,
             "                    found = PatternMatcher._match_command_with_punct_retry(\n"
             "                        regex, _normalize_first_word_in_text(phrase),\n"
             "                    )[0]\n",
             "                    found = regex.search(phrase)\n",
             [UNTAKEN_KEEPS]),
    mutation("w-builtin-drops-punctuation-retry", MANAGER,
             "                    found = PatternMatcher._match_command_with_punct_retry(\n"
             "                        regex, _normalize_first_word_in_text(phrase),\n"
             "                    )[0]\n",
             "                    found = regex.fullmatch(phrase)\n",
             [EDIT_GETS_FLAG]),

    # --- the expression enumeration (wh-user-rule-precedence.2.1) -----------
    # _sample_phrases no longer asks for the enumerated phrases: the
    # defect as the review found it.
    mutation("e-enumeration-not-sampled", MANAGER,
             "            [*wordings, *cls._enumerated_phrases(pattern)],\n",
             "            [*wordings],\n",
             [OPTIONAL_ADD, OPTIONAL_EDIT, OPTIONAL_PREVIEW]),
    # An optional group (and every repeat) gives only its minimum count.
    mutation("e-optional-group-not-expanded", MANAGER,
             _LAST_COUNT,
             "            last = low\n",
             [OPTIONAL_ADD, OPTIONAL_EDIT, OPTIONAL_PREVIEW, FINITE_ADD,
              FINITE_PREVIEW, ENUMERATED]),
    # A fixed count is also expanded at count + 1.
    mutation("e-fixed-count-expanded-too", MANAGER,
             _LAST_COUNT,
             "            last = low + 1 if high in (parser.MAXREPEAT, low) else high\n",
             [ENUMERATED]),
    # A finite range is expanded only at its minimum and the minimum plus
    # one again: the defect wh-user-rule-precedence.2.2 found.
    # '^select al{0,2}$' then never samples 'select all'.
    mutation("e-finite-range-stops-at-low-plus-one", MANAGER,
             _LAST_COUNT,
             "            last = low + 1 if high == parser.MAXREPEAT else min(high, low + 1)\n",
             [FINITE_ADD, FINITE_PREVIEW, ENUMERATED]),
    # An unbounded repeat is expanded toward its maximum (MAXREPEAT) and
    # stops only at the phrase cap: '^item (\d+) now$' gives 64 phrases.
    mutation("e-unbounded-range-expanded-to-its-maximum", MANAGER,
             _LAST_COUNT,
             "            last = high\n",
             [ENUMERATED]),
    # A later count that passes the length guard raises like the minimum
    # does, so the whole enumeration gives nothing instead of the shorter
    # counts: '^(?:b...){1,5}$' (100 b's) gives [] instead of two phrases.
    mutation("e-length-guard-drops-the-earlier-counts", MANAGER,
             "                    if count == low:\n"
             "                        raise\n"
             "                    break\n",
             "                    raise\n",
             [ENUMERATED]),
    # A joined phrase past the length guard stops the whole enumeration
    # again, instead of being dropped: the defect wh-user-rule-precedence.2.3
    # found. '^show(?: desktop){0,32}$' then samples nothing.
    mutation("e-too-long-join-stops-the-enumeration", MANAGER,
             "                    dropped = True\n"
             "                    continue\n",
             "                    raise cls._NotEnumerable(text[:40])\n",
             [LONG_ADD, LONG_PREVIEW, ENUMERATED, PRODUCT_KEEPS]),
    # A product with no phrase within the guard returns nothing instead of
    # raising, so a range such as '^b{200,100000}$' walks every count.
    mutation("e-no-fitting-phrase-returns-empty", MANAGER,
             "        if dropped and not joined:\n",
             "        if False:\n",
             [PRODUCT_RAISES]),
    # The representative member of a digit class changes.
    mutation("e-digit-member-changed", MANAGER,
             '        _regex_parser.CATEGORY_DIGIT: "2",\n',
             '        _regex_parser.CATEGORY_DIGIT: "3",\n',
             [ENUMERATED]),
    # A range gives its end instead of its start.
    mutation("e-range-member-is-the-end", MANAGER,
             "                return [chr(member[0])]\n",
             "                return [chr(member[1])]\n",
             [ENUMERATED]),
    # The cap doubles, so the 128 combinations are not cut to 64.
    mutation("e-cap-raised", MANAGER,
             "    _MAX_ENUMERATED_PHRASES = 64\n",
             "    _MAX_ENUMERATED_PHRASES = 128\n",
             [ENUMERATION_CAP]),
    # An unsupported opcode gives an empty piece instead of stopping the
    # enumeration.
    mutation("e-unsupported-opcode-skipped", MANAGER,
             '            return ["a"]\n'
             "        raise cls._NotEnumerable(opcode)\n",
             '            return ["a"]\n'
             '        return [""]\n',
             [ENUMERATION_UNSUPPORTED]),
    # A negated class gives an empty piece instead of stopping it.
    mutation("e-negated-class-skipped", MANAGER,
             "            raise cls._NotEnumerable(member_opcode)\n",
             '            return [""]\n',
             [ENUMERATION_UNSUPPORTED]),
    # A category with no member gives 'a' instead of stopping it.
    mutation("e-unknown-category-given-a-member", MANAGER,
             "            raise cls._NotEnumerable(category)\n",
             '            return ["a"]\n',
             [ENUMERATION_UNSUPPORTED]),
    # The fallback catches only a parse error, so an unsupported
    # construct raises out of the enumeration.
    mutation("e-unsupported-construct-raises", MANAGER,
             "            pieces = cls._enumerate_sequence(_regex_parser.parse(raw_pattern))\n"
             "        except Exception:\n",
             "            pieces = cls._enumerate_sequence(_regex_parser.parse(raw_pattern))\n"
             "        except re.error:\n",
             [ENUMERATION_UNSUPPORTED]),

    # --- requires_hotword is never copied -----------------------------------
    # Each save path copies requires_hotword from the taken built-ins the
    # way it copies whole_utterance_only: the same decision, run over the
    # shipped entries with their hotword value in the flag's place.
    mutation("h-create-copies-the-hotword", MANAGER,
             "                regex, action_steps, requires_hotword, stored_phrases,\n",
             "                regex, action_steps, self._whole_utterance_for_save(\n"
             "                    in_front, regex,\n"
             "                    [dict(e, whole_utterance_only=e.get('requires_hotword'))\n"
             "                     for e in system_entries],\n"
             "                    requires_hotword,\n"
             "                ), stored_phrases,\n",
             [HOTWORD_ADD]),
    mutation("h-update-copies-the-hotword", MANAGER,
             '                regex, action_steps, data.get("requires_hotword", False),\n',
             "                regex, action_steps, self._whole_utterance_for_save(\n"
             "                    in_front, regex,\n"
             "                    [dict(e, whole_utterance_only=e.get('requires_hotword'))\n"
             "                     for e in system_entries],\n"
             '                    data.get("requires_hotword", False),\n'
             "                ),\n",
             [HOTWORD_CHECKED, HOTWORD_CLEAR]),

    # --- the Try-it draft (pattern_tester._simulate_save) -------------------
    mutation("t-edited-draft-skips-the-copy", TESTER,
             "                    PatternManager._stands_in_front_for_save(\n"
             "                        block, system_entries,\n"
             "                    ),\n"
             '                    block["pattern"], system_entries,\n'
             "                    entry.get(WHOLE_UTTERANCE_KEY),\n",
             "                    False,\n"
             '                    block["pattern"], system_entries,\n'
             "                    entry.get(WHOLE_UTTERANCE_KEY),\n",
             [PREVIEW_FLAG, PREVIEW_PLAIN_FLAG, PREVIEW_OWN_FLAG]),
    mutation("t-created-draft-skips-the-copy", TESTER,
             "                PatternManager._stands_in_front_for_save(\n"
             "                    block, system_entries,\n"
             "                ),\n"
             '                block["pattern"], system_entries,\n'
             "                whole_utterance_only,\n",
             "                False,\n"
             '                block["pattern"], system_entries,\n'
             "                whole_utterance_only,\n",
             [PREVIEW_FLAG, PREVIEW_PLAIN_FLAG, PREVIEW_OWN_FLAG]),
    mutation("t-edited-draft-ignores-the-stored-flag", TESTER,
             "                    entry.get(WHOLE_UTTERANCE_KEY),\n",
             "                    None,\n",
             [PREVIEW_FLAG]),
    mutation("t-created-draft-ignores-its-flag", TESTER,
             '                block["pattern"], system_entries,\n'
             "                whole_utterance_only,\n",
             '                block["pattern"], system_entries,\n'
             "                None,\n",
             [PREVIEW_FLAG]),
    mutation("t-run-test-draft-drops-the-flag", TESTER,
             "            whole_utterance_only=draft.get(WHOLE_UTTERANCE_KEY),\n",
             "",
             [PREVIEW_FLAG]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
