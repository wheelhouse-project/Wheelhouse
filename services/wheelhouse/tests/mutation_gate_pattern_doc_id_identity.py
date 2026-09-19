"""Mutation gate for the doc_id identity rule (wh-pattern-override-doc-id).

Run it from services/wheelhouse with the service's own interpreter:

    uv run python tests/mutation_gate_pattern_doc_id_identity.py
    uv run python tests/mutation_gate_pattern_doc_id_identity.py --check
    uv run python tests/mutation_gate_pattern_doc_id_identity.py --only <name>
    uv run python tests/mutation_gate_pattern_doc_id_identity.py --only=<name>

The bead's defect: a saved override was associated with the built-in it
replaced by the built-in's own regex, so a release that rewrote that regex
orphaned the override -- the merge appended it after every built-in, the
built-in won again, and the Pattern Manager stopped showing the badge that
would have told the user. The fix keys the association on the pattern's
durable ``doc_id`` instead, through ONE rule in
``speech/pattern_identity.py`` that the runtime merge, the manager listing
and the try-it preview all call.

This gate mutates only Python source: ``speech/pattern_identity.py``,
``speech/pattern_buildable.py``, ``speech/pattern_catalog.py``,
``speech/pattern_manager.py``, ``speech/pattern_tester.py``,
``speech/speech_handler.py`` and ``pattern_manager_dialog.py``. It never
touches ``speech/config/patterns.toml``, ``hints.txt`` or any
``config.toml`` (wh-pattern-override-doc-id.1 G5), so no shipped pattern
text and no shipped doc_id is edited by a run.

  the-identity-prefers-the-text-key-over-the-doc-id   (G1 a)
        ``identity_from`` asks the pattern text first, so a named entry
        identifies by its regex again. This is the bead's defect restored
        at its source.
  the-two-identity-kinds-share-one-tag                (G1 b)
        TEXT_KIND becomes "doc", collapsing the two namespaces into one,
        so a user entry whose regex happens to read "window-maximize"
        takes over the built-in carrying that doc_id.
  the-built-entry-does-not-carry-the-doc-id           (G1 c)
        ``PatternCatalog._build_structures`` stops copying the id into the
        runtime entry, so anything working on the merged list -- the
        try-it preview above all -- falls back to the text key.
  the-matched-builtin-is-appended-not-replaced        (G1 d)
        ``_merge_entries`` appends the matching user entry instead of
        replacing the built-in in place, which loses both the built-in's
        order position and the replacement itself.
  the-block-writer-omits-the-doc-id-line              (G1 e)
        ``_build_block_lines`` stops writing the ``doc_id`` line, so a
        customised copy is saved identified by its expression alone.
  create-accepts-a-malformed-doc-id                   (G1 f)
        ``create_pattern`` silently drops a malformed id and writes the
        block anyway instead of refusing before the file is touched.
  an-update-drops-the-doc-id                          (G1 g)
        ``update_pattern`` rebuilds the block without the id, so the next
        release orphans the override that an edit just rewrote.
  an-update-takes-the-doc-id-from-the-caller          (G1 h)
        ``update_pattern`` reads the id out of the editor's ``data``
        instead of the block on disk, so an edit could move a rule onto a
        different built-in.
  duplicate-keeps-the-builtin-identity                (G1 i)
        ``_on_duplicate_clicked`` passes keep_identity=True, so two rules
        claim one built-in and the file order decides which wins.
  customize-drops-the-builtin-identity                (G1 i)
        ``_on_customize_clicked`` passes keep_identity=False, which is the
        other half of the same seam: the two buttons opened the editor
        with the same call until this flag separated them.
  the-manager-listing-keeps-its-own-text-key          (G1 j)
        ``PatternManager._override_key`` computes the old normalized-text
        key locally instead of delegating, so the badge answers a
        different question from the merge.
  the-preview-keeps-its-own-text-key                  (G1 k)
        ``pattern_tester._simulate_merge`` computes the draft's key from
        the text locally, so the try-it line disagrees with the save it
        previews.
  the-legacy-migration-never-fires                    (G1 l)
        The one-candidate branch is disabled, so an override saved before
        doc_ids existed stops replacing its built-in.
  the-ambiguous-legacy-override-migrates-anyway       (G1 m)
        The same branch fires on any number of candidates, so an override
        that two built-ins could claim is silently handed to the first.
  legacy-candidates-answers-a-named-entry-by-its-text (G1 n)
        ``legacy_candidates`` drops the kind half of its guard, so an entry
        that names itself is answered by the text map as well. The doc_id
        then acts as a text key, and a rule whose expression equals that id
        is taken over.
  a-named-override-is-migrated-by-its-text            (G1 n)
        The same rule broken at the merge's call site instead: a text key
        is built for every user entry, so an entry that carries its own
        doc_id is migrated onto the built-in that shares its expression.
  the-manager-does-not-mark-the-ambiguous-entry       (G1 o)
        The listing stops setting ``unresolved_override``, so the window
        shows an ordinary user pattern where the merge could not place one.
  the-list-does-not-mark-an-unresolved-override       (G1 p)
        The tree row loses the ``[unresolved override]`` mark and falls
        back to ``[user]``, which tells the reader the opposite of the
        truth.
  the-detail-badge-does-not-say-unresolved            (G1 p)
        The detail pane's badge loses the same state.

The list above names the entries filed against the bead's own acceptance
points. The entries added for a review finding -- .2.1, .2.2, .2.3, .3.1,
.3.2, .3.3, .3.4 and .3.6 -- each carry their explanation as a comment on
the entry itself.

Every entry in ``MUTATIONS`` must be caught. Pattern-not-found, an
ambiguous pattern, a mutation that does not compile, a per-mutation
timeout, a suite-timeout abort, an expected catcher that SKIPPED, an
expected catcher whose name does not exist and a failed restore are
reported as errors, never as a verdict; a failed restore stops the sweep.
``--check`` verifies every pattern matches
exactly once in the current source and that every mutant compiles, without
running any test.

A NOTE ON THE THREE CATCHERS THAT FAIL WITH A KeyError, recorded so a later
reader does not mistake them for the false catch the mutation-gate skill
warns about. ``the-block-writer-omits-the-doc-id-line`` and both update
mutations are caught by ``blocks[0]["doc_id"] == ...``, and
``the-manager-does-not-mark-the-ambiguous-entry`` by
``entry["unresolved_override"] is True``. The KeyError is raised BY that
subscript, on the exact key the mutation stops writing -- it is the guarded
behaviour itself, not an unrelated crash upstream of the assertion. The
block-writer mutation additionally fails
``test_a_customised_builtin_still_wins_after_its_regex_is_rewritten`` on a
plain ``assert 2 == 1``, so it is proven twice over.

A NOTE ON THE TWO MUTATIONS FOR (n), because the rule has two halves and
only one of them lives inside ``legacy_candidates``. The guard mutation is
narrow on purpose: for a DOC identity the function looks ``identity[1]`` up
in a map keyed by normalized pattern TEXT, so it changes nothing unless some
entry's expression happens to equal some doc_id. Exactly one fixture in the
guard set builds that collision --
``test_a_doc_id_does_not_capture_a_pattern_of_the_same_text``, whose user
entry's regex reads ``window-maximize`` while the draft carries the doc_id
``window-maximize`` -- and it is the sole catcher. The second mutation
breaks the same rule at the merge's call site, where the harm is the
ordinary one: an entry with its own doc_id is migrated onto the built-in
that shares its expression.

A NOTE ON ONE LINE OF THE .3.2 FIX THAT NO MUTATION COVERS, recorded so a
later reader does not think it was forgotten.
``_merge_entries`` marks the appended displaced row in ``claimed``, and
removing that line alone changes no answer, so a mutation for it would
survive. With the built-in's identity anchored to the built-in's own
position, the only key that can still reach an appended row is the key the
moved entry introduced -- a normalized-text key -- so every entry that
reaches it carries that row's own trigger and is superseded whether or not
the row is marked. The line is there to keep ``claimed`` meaning what it
says, and ``the-displaced-row-keeps-its-pre-fix-bookkeeping`` removes it
together with the anchor, which is the state that lost a rule.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
IDENTITY = SERVICE / "speech" / "pattern_identity.py"
BUILDABLE = SERVICE / "speech" / "pattern_buildable.py"
CATALOG = SERVICE / "speech" / "pattern_catalog.py"
BLOCK_TEXT = SERVICE / "speech" / "pattern_block_text.py"
MANAGER = SERVICE / "speech" / "pattern_manager.py"
TESTER = SERVICE / "speech" / "pattern_tester.py"
DIALOG = SERVICE / "pattern_manager_dialog.py"
EDITOR = SERVICE / "create_pattern_dialog.py"
HANDLER = SERVICE / "speech" / "speech_handler.py"

# The guard set, run for every mutation. The whole set takes about twelve
# seconds, so narrowing it per mutation would save little and would hide a
# catcher in another file -- the identity rule is deliberately shared by the
# merge, the listing and the preview, and a mutation in one of them is
# supposed to be visible in the others' tests.
SELECTION = [
    "tests/test_pattern_identity.py",
    "tests/test_pattern_catalog_doc_id_merge.py",
    "tests/test_pattern_manager_doc_id_listing.py",
    # .3.6. The claimant count lives in the same listing call as the
    # override badge, so it belongs beside that file. It calls only
    # get_all_patterns_structured, never create_pattern or
    # update_pattern, so it reaches no _probe_backtracking and starts
    # no safe_regex pool; it needs no teardown and is safe ahead of
    # the Qt targets below.
    "tests/test_pattern_remove_customization_promise.py",
    "tests/test_pattern_tester_doc_id.py",
    # Kept beside the other tester file and ahead of every Qt target: it
    # builds a PatternManager, which starts the safe_regex multiprocessing
    # pool, and a PatternManagerDialog built while that pool exists reads a
    # corrupted screen object. Its own module fixture shuts the pool down,
    # which is what lets the dialog targets below still pass.
    "tests/test_pattern_tester_save_agreement.py",
    # .3.3, the duplicate that must stay independent. It calls
    # create_pattern and update_pattern, so it starts the safe_regex
    # pool; its own module fixture shuts the pool down, and it sits
    # here ahead of every Qt target for that reason.
    "tests/test_pattern_duplicate_stays_independent.py",
    "tests/test_pattern_customize_keeps_identity.py",
    "tests/test_pattern_customize_vs_duplicate.py",
    "tests/test_pattern_disabled_override_stays.py",
    "tests/test_pattern_legacy_override_migration.py",
    # .3.1, the recovered name written into the user file. It builds
    # PatternCatalog objects and one SpeechHandler whose PatternCatalog
    # is a recorder, so it calls no create_pattern or update_pattern and
    # starts no safe_regex pool. Safe anywhere ahead of the Qt targets.
    "tests/test_pattern_legacy_id_written_at_load.py",
    "tests/test_pattern_unbuildable_override.py",
    "tests/test_pattern_unresolved_override_badge.py",
    # .3.6, the words the window shows. A Qt target, so it sits here
    # with the others, after every file that starts the safe_regex
    # pool and shuts it down again.
    "tests/test_pattern_remove_customization_words.py",
    # Only this class: the rest of the dialog file is about other panes.
    "tests/test_pattern_manager_dialog.py::TestOpenEditorWiring",
]

# The one target a single mutation drops. Named here rather than sliced off
# the end of SELECTION so a later reorder cannot silently drop a different
# file, and asserted so a rename shows up as a refusal instead of a run that
# quietly drops nothing. Only the-list-does-not-mark-an-unresolved-override
# uses it; the comment on that entry says why.
_EDITOR_WIRING_TARGET = (
    "tests/test_pattern_manager_dialog.py::TestOpenEditorWiring"
)
SELECTION_WITHOUT_EDITOR_WIRING = [
    target for target in SELECTION if target != _EDITOR_WIRING_TARGET
]
assert len(SELECTION_WITHOUT_EDITOR_WIRING) == len(SELECTION) - 1, (
    f"{_EDITOR_WIRING_TARGET} is no longer in SELECTION"
)

# -v prints one line per test, which is what names a SKIPPED catcher; -rfE
# keeps the FAILED and ERROR summary lines the readers below parse.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rfE"]
PER_MUTATION_TIMEOUT_S = 300

MUTATIONS = [
    {
        "name": "a-trigger-move-keeps-the-origin",
        "file": MANAGER,
        "old": (
            "            if isinstance(original, str) and key != cls._save_trigger_key(original):\n"
            "                return None\n"
        ),
        "new": (
            "            if isinstance(original, str) and key != cls._save_trigger_key(original):\n"
            "                return doc_id\n"
        ),
        "selection": ["tests/test_pattern_override_trigger_move.py"],
        "expect": [
            "test_moved_customization_restores_origin_and_previews_save[second-create]",
            "test_moved_customization_restores_origin_and_previews_save[second-update]",
            "test_legacy_moved_override_uses_load_resolution_for_destination",
        ],
    },
    {
        "name": "an-unchanged-trigger-loses-its-rewritten-origin",
        "file": MANAGER,
        "old": (
            "        if previous_pattern is not None and key == cls._save_trigger_key(previous_pattern):\n"
            "            return doc_id\n"
        ),
        "new": "",
        "selection": ["tests/test_pattern_override_trigger_move.py"],
        "expect": ["test_unchanged_trigger_keeps_override_after_release_rewrite"],
    },
    {
        "name": "simple-mode-serialization-detaches-the-origin",
        "file": MANAGER,
        "old": "        if phrases is not None and len(phrases) == 1:\n",
        "new": "        if False:\n",
        "selection": ["tests/test_pattern_override_trigger_move.py"],
        "expect": [
            "test_simple_mode_serialization_keeps_unchanged_override[create]",
            "test_simple_mode_serialization_keeps_unchanged_override[update]",
        ],
    },
    {
        # .3.1. The whole write, switched off. Every other .3.1 mutation
        # below asks whether the write does the right thing; this one asks
        # whether it happens at all.
        "name": "the-load-never-writes-the-recovered-name",
        "file": CATALOG,
        "old": (
            "        if recovered:\n"
            "            targets = {\n"
        ),
        "new": (
            "        if False:\n"
            "            targets = {\n"
        ),
        "expect": [
            "test_the_doc_id_reaches_the_file",
            "test_the_association_survives_a_rewrite_of_the_builtin",
            # The suppression half of the bead: an override with no actions
            # switches a built-in off, and the rewrite must not switch it
            # back on. Both names measured, not guessed.
            "test_the_silenced_block_gets_its_name",
            "test_the_command_stays_off_after_a_rewrite",
        ],
    },
    {
        # .3.1, the boss's required entry. The two lines still reach the
        # file and the block still parses, so nothing complains -- but the
        # block's own body, including a comment the person wrote inside it,
        # is replaced by them. It is the mutation that separates "inserts
        # two lines" from "rewrites the block".
        "name": "the-migration-replaces-the-block-instead-of-inserting",
        "file": CATALOG,
        "old": "                lines[start + 1:start + 1] = [\n",
        "new": "                lines[start + 1:end] = [\n",
        # CATCHER LIST REPAIRED at .3.7, and the three names it used to
        # hold are now green under this mutation. They were
        # test_the_hand_written_comment_stays_with_its_block,
        # test_the_expression_is_not_damaged and
        # test_a_fresh_catalog_over_the_migrated_file_answers_the_same,
        # and every one of them asserts that something was NOT damaged.
        # _only_the_named_blocks_took_the_keys is the upstream layer that
        # now refuses this class of write: the replaced block loses its
        # other keys, the candidate file differs by more than the two
        # names, the check refuses, and nothing is written at all. A
        # refused write damages nothing, so those three pass. Their
        # passing is the evidence the file was protected; it is not
        # evidence the mutation is harmless. The names below are what
        # measurably goes red, and they fail on their own assertions --
        # verified by hand, not inferred: "assert None ==
        # 'window-maximize'" for the two doc_id reads, and "assert [] ==
        # ['doc_id = ...', 'origin = ...']" for the line count, whose
        # printed file still holds the person's own comment.
        #
        # Known and accepted: with the check refusing, this mutation and
        # the-load-never-writes-the-recovered-name leave the same file,
        # so no file-outcome test separates them. Measured -- all four of
        # test_only_two_lines_are_added, test_a_backup_of_the_original_
        # is_kept, test_a_permission_error_is_survived and
        # test_the_failure_is_reported_as_a_warning are caught by that
        # entry too. What this entry still proves is that a block-
        # replacing insert never reaches the file.
        "expect": [
            "test_only_two_lines_are_added",
            "test_the_doc_id_reaches_the_file",
            "test_the_block_gets_its_name",
        ],
    },
    {
        # .3.1. One line lower is inside a triple-quoted expression, and
        # the file still parses afterwards, so only a test that reads the
        # block back can see it.
        "name": "the-insert-lands-one-line-below-the-header",
        "file": CATALOG,
        "old": "                lines[start + 1:start + 1] = [\n",
        "new": "                lines[start + 2:start + 2] = [\n",
        # CATCHER LIST REPAIRED at .3.7. test_the_expression_is_not_
        # damaged was dropped because it is now green under this
        # mutation. It asserts the expression still reads
        # "^maximize$\n", and _only_the_named_blocks_took_the_keys
        # refuses the write outright when the two lines land inside that
        # triple-quoted string, so the file is left alone and the
        # expression is undamaged. Its passing is the evidence the file
        # was protected, not evidence the mutation is harmless.
        #
        # The two names below are the whole measured red set, and this
        # entry is still diagnostic because they fail for DIFFERENT
        # reasons -- verified by hand, not inferred:
        #   test_the_block_gets_its_name uses a multi-line expression.
        #     The insert lands inside the string, the parsed value
        #     changes, the check refuses, and nothing is written:
        #     "assert None == 'window-maximize'".
        #   test_the_file_is_left_exactly_as_it_was uses a one-line
        #     expression. The insert lands after that complete key, so
        #     the parsed values are unchanged, the check PASSES, and the
        #     file really is written with the two lines in the wrong
        #     place. Its byte comparison is what sees it, printing the
        #     two lines after "pattern = '''^maximize$'''" instead of
        #     before it.
        # So one half proves the check refuses a damaging insert, and
        # the other proves a byte comparison catches a misplaced insert
        # the check cannot see.
        "expect": [
            "test_the_block_gets_its_name",
            "test_the_file_is_left_exactly_as_it_was",
        ],
    },
    {
        # .3.1. Only the name is written, so the migrated block is not the
        # shape create_pattern writes for a Customize.
        "name": "the-migration-omits-the-editors-own-key",
        "file": CATALOG,
        "old": (
            "                    f'{DOC_ID_KEY} = \"{doc_id}\"{newline}',\n"
            "                    f'{ORIGIN_KEY} = \"{ORIGIN_OWN}\"{newline}',\n"
        ),
        "new": (
            "                    f'{DOC_ID_KEY} = \"{doc_id}\"{newline}',\n"
        ),
        "expect": [
            "test_the_block_also_carries_the_editors_own_key",
            "test_only_two_lines_are_added",
        ],
    },
    {
        # .3.1. The name -- the one thing the migration exists to write --
        # is left out.
        "name": "the-migration-omits-the-recovered-name",
        "file": CATALOG,
        "old": (
            "                    f'{DOC_ID_KEY} = \"{doc_id}\"{newline}',\n"
            "                    f'{ORIGIN_KEY} = \"{ORIGIN_OWN}\"{newline}',\n"
        ),
        "new": (
            "                    f'{ORIGIN_KEY} = \"{ORIGIN_OWN}\"{newline}',\n"
        ),
        "expect": [
            "test_the_doc_id_reaches_the_file",
            "test_the_association_survives_a_rewrite_of_the_builtin",
        ],
    },
    {
        # .3.1. The migration writes to the name the Pattern Manager's own
        # save uses, destroying the one copy an edit could be undone from.
        "name": "the-migration-takes-the-editors-backup-name",
        "file": CATALOG,
        "old": '    BACKUP_SUFFIX = ".pre-doc-id-migration.bak"\n',
        "new": '    BACKUP_SUFFIX = ".bak"\n',
        "expect": [
            "test_the_backup_is_not_the_editors_own",
            "test_a_backup_of_the_original_is_kept",
        ],
    },
    {
        # .3.1. Every migration keeps a copy, so a later one replaces the
        # original with an already-migrated file and the migration can no
        # longer be undone.
        "name": "every-migration-overwrites-the-first-backup",
        "file": CATALOG,
        "old": "            if not os.path.exists(backup_path):\n",
        "new": "            if True:\n",
        "expect": [
            "test_the_first_backup_is_not_overwritten",
        ],
    },
    {
        # .3.1, constraint 4. A refused write stops the load instead of
        # being logged, so a read-only file takes the person's commands
        # away rather than postponing their durability.
        "name": "a-refused-write-stops-the-load",
        "file": CATALOG,
        "old": "        except Exception as exc:\n",
        "new": "        except ZeroDivisionError as exc:\n",
        "expect": [
            "test_the_load_still_gives_the_person_their_rule",
            "test_the_failure_is_reported_as_a_warning",
            # The two failures the acceptance field names by hand: a
            # PermissionError from the replace, and a file that is really
            # read-only on disk with nothing patched.
            "test_a_permission_error_is_survived",
            "test_a_read_only_file_is_survived",
        ],
    },
    {
        # .3.1, constraint 5. The parameter exists and nothing passes it,
        # so the person who never opens the Pattern Manager window -- the
        # whole population the bead is about -- is never migrated.
        "name": "the-app-load-does-not-ask-for-the-migration",
        "file": HANDLER,
        "old": "            migrate_legacy_ids=True,\n",
        "new": "            migrate_legacy_ids=False,\n",
        "expect": [
            "test_the_app_load_asks_for_the_migration",
        ],
    },
    {
        "name": "the-identity-prefers-the-text-key-over-the-doc-id",
        "file": IDENTITY,
        "old": (
            "    if not isinstance(pattern, str):\n"
            "        return None\n"
            "    if is_valid_doc_id(doc_id):\n"
            "        return (DOC_KIND, doc_id)\n"
            "    return (TEXT_KIND, normalized_text_key(pattern))\n"
        ),
        "new": (
            "    if isinstance(pattern, str):\n"
            "        return (TEXT_KIND, normalized_text_key(pattern))\n"
            "    if is_valid_doc_id(doc_id):\n"
            "        return (DOC_KIND, doc_id)\n"
            "    return None\n"
        ),
        "expect": [
            "test_an_entry_with_a_doc_id_identifies_by_it",
            "test_the_doc_id_survives_a_rewrite_of_the_pattern_text",
            "test_the_badge_is_still_shown_after_the_rewrite",
        ],
    },
    {
        # wh-pattern-override-doc-id.2.3. The doc branch used to answer
        # before anything looked at the pattern, so a hand-edited
        # `pattern = 5` under a valid doc_id took the built-in's slot in
        # the merge and was only then dropped by the build. The built-in
        # stopped answering and the window still listed it. This mutation
        # restores that order and nothing else.
        #
        # The two catalog-merge catchers this once named --
        # test_a_non_string_pattern_leaves_the_builtin_answering and
        # test_a_missing_pattern_leaves_the_builtin_answering -- were
        # removed on 2026-09-06 because they can no longer fail here. The
        # .3.4 fix (02290c19) put speech/pattern_buildable.slot_identity
        # in front of every merge lookup, and its can_build_expression
        # answers False for a non-string or missing `pattern` before
        # entry_identity is called at all, so the merge never reaches the
        # order this mutation changes. Measured on the unmutated source:
        # can_build_expression is False and slot_identity is None for both
        # fixtures. Those two tests now pin an outcome two layers protect,
        # and no single-file mutation can turn them red; the layer they
        # still guard alone carries its own mutations under BUILDABLE.
        "name": "an-unbuildable-entry-still-claims-its-doc-id",
        "file": IDENTITY,
        "old": (
            "    if not isinstance(pattern, str):\n"
            "        return None\n"
            "    if is_valid_doc_id(doc_id):\n"
            "        return (DOC_KIND, doc_id)\n"
        ),
        "new": (
            "    if is_valid_doc_id(doc_id):\n"
            "        return (DOC_KIND, doc_id)\n"
            "    if not isinstance(pattern, str):\n"
            "        return None\n"
        ),
        "expect": [
            "test_a_valid_doc_id_cannot_rescue_a_pattern_that_is_not_a_string",
            "test_a_valid_doc_id_cannot_rescue_a_missing_pattern",
        ],
    },
    {
        # The honest shape of "the kind tag stops separating the two
        # namespaces" is to give both kinds the same tag. It compiles, and
        # the catcher is a real assertion that the two identities differ.
        "name": "the-two-identity-kinds-share-one-tag",
        "file": IDENTITY,
        "old": 'TEXT_KIND = "text"\n',
        "new": 'TEXT_KIND = "doc"\n',
        "expect": [
            "test_a_doc_id_never_collides_with_a_pattern_of_the_same_text",
            "test_a_user_regex_equal_to_a_doc_id_does_not_capture_that_builtin",
        ],
    },
    {
        "name": "the-built-entry-does-not-carry-the-doc-id",
        "file": CATALOG,
        "old": (
            "                    entry_doc_id = rule.get(DOC_ID_KEY)\n"
            "                    if is_valid_doc_id(entry_doc_id):\n"
            "                        built_entry[DOC_ID_KEY] = entry_doc_id\n"
        ),
        "new": (
            "                    entry_doc_id = rule.get(DOC_ID_KEY)\n"
            "                    if False:\n"
            "                        built_entry[DOC_ID_KEY] = entry_doc_id\n"
        ),
        # Without the id on the built entries the two twins collapse to one
        # text identity, the ambiguity disappears, and the draft replaces a
        # built-in the merge would have left alone.
        "expect": ["test_an_ambiguous_legacy_draft_appends"],
    },
    {
        "name": "the-matched-builtin-is-appended-not-replaced",
        "file": CATALOG,
        "old": (
            "                merged[index] = user_entry\n"
            "                claimed.add(index)\n"
        ),
        "new": (
            "                merged.append(user_entry)\n"
            "                claimed.add(len(merged) - 1)\n"
        ),
        "expect": [
            "test_the_override_replaces_rather_than_joins_the_builtin",
            "test_the_override_keeps_the_builtin_position_after_the_rewrite",
        ],
    },
    {
        "name": "the-block-writer-omits-the-doc-id-line",
        "file": MANAGER,
        "old": (
            "        if doc_id is not None:\n"
            "            lines.append("
            'f"doc_id = {cls._format_toml_value(doc_id)}")\n'
        ),
        "new": (
            "        if False:\n"
            "            lines.append("
            'f"doc_id = {cls._format_toml_value(doc_id)}")\n'
        ),
        "expect": [
            "test_a_created_block_carries_the_doc_id_it_was_given",
            "test_a_customised_builtin_still_wins_after_its_regex_is_rewritten",
        ],
    },
    {
        # Not `if False:` on this guard. The block writer's own last-line-of-
        # defence raises for a malformed id, create_pattern's except turns
        # that into {"success": False, "error": "...doc_id..."}, and both
        # refusal tests would still pass -- a masked mutation. Silently
        # dropping the bad id is the mutation that actually reaches the
        # file: the save then succeeds and writes a block.
        "name": "create-accepts-a-malformed-doc-id",
        "file": MANAGER,
        "old": (
            "        if doc_id is not None and not is_valid_doc_id(doc_id):\n"
            "            return {\n"
            '                "success": False,\n'
            '                "error": (\n'
            "                    f\"Invalid doc_id {doc_id!r}: a pattern's"
            ' durable name "\n'
            '                    f"is lowercase letters and digits in'
            ' hyphen-separated "\n'
            '                    f"groups"\n'
            "                ),\n"
            "            }\n"
        ),
        "new": (
            "        if doc_id is not None and not is_valid_doc_id(doc_id):\n"
            "            doc_id = None\n"
        ),
        "expect": [
            "test_a_refused_doc_id_writes_nothing_at_all",
            "test_a_malformed_doc_id_is_refused_rather_than_written[spaces]",
            "test_a_malformed_doc_id_is_refused_with_a_readable_error",
        ],
    },
    {
        "name": "an-update-drops-the-doc-id",
        "file": MANAGER,
        "old": (
            "                doc_id=self._doc_id_for_save(\n"
            "                    original_doc_id, regex,\n"
            "                    self._load_pattern_dicts(self.patterns_file),\n"
            "                    toml_patterns[target_index][\"pattern\"],\n"
            "                ),\n"
        ),
        "new": "                doc_id=None,\n",
        "expect": ["test_updating_a_block_carries_its_doc_id_forward"],
    },
    {
        "name": "an-update-takes-the-doc-id-from-the-caller",
        "file": MANAGER,
        "old": (
            "            original_doc_id = toml_patterns[target_index]"
            ".get(DOC_ID_KEY)\n"
        ),
        "new": "            original_doc_id = data.get(DOC_ID_KEY)\n",
        "expect": ["test_updating_a_block_carries_its_doc_id_forward"],
    },
    {
        "name": "duplicate-keeps-the-builtin-identity",
        "file": DIALOG,
        "old": "        self._open_editor(entry=pat)\n",
        "new": "        self._open_editor(entry=pat, keep_identity=True)\n",
        "expect": ["test_duplicate_opens_editor_without_pattern_id"],
    },
    {
        "name": "customize-drops-the-builtin-identity",
        "file": DIALOG,
        "old": "        self._open_editor(entry=pat, keep_identity=True)\n",
        "new": "        self._open_editor(entry=pat, keep_identity=False)\n",
        "expect": ["test_customize_opens_editor_without_pattern_id"],
    },
    {
        # The listing's own copy of the pre-doc_id key, restored. Guarded
        # against a non-string `pattern = 5` so the mutant reaches the
        # assertion instead of dying in normalized_text_key.
        "name": "the-manager-listing-keeps-its-own-text-key",
        "file": MANAGER,
        # The delegate's name changed with .3.4 (entry_identity ->
        # slot_identity); the behaviour this pins did not.
        "old": "        return slot_identity(pat_data)\n",
        "new": (
            "        raw = pat_data.get(\"pattern\")\n"
            "        if not isinstance(raw, str):\n"
            "            return None\n"
            "        return (\"text\", normalized_text_key(raw))\n"
        ),
        "expect": ["test_the_badge_is_still_shown_after_the_rewrite"],
    },
    {
        # The preview's own copy of the pre-doc_id key, restored.
        "name": "the-preview-keeps-its-own-text-key",
        "file": TESTER,
        "old": "    draft_key = runtime_entry_identity(draft_entry)\n",
        "new": (
            "    draft_key = "
            "(\"text\", _raw_pattern(draft_entry).strip().casefold())\n"
        ),
        "expect": [
            "test_a_draft_carrying_a_builtin_doc_id_replaces_that_builtin",
            "test_the_draft_wins_the_try_it_line_after_the_rewrite",
        ],
    },
    {
        "name": "the-legacy-migration-never-fires",
        "file": CATALOG,
        "old": "            if len(matches) == 1:\n",
        "new": "            if False:\n",
        "expect": [
            "test_a_legacy_override_still_wins_over_its_builtin",
            "test_it_replaces_the_builtin_rather_than_joining_it",
            "test_it_takes_the_builtin_position_rather_than_the_end",
        ],
    },
    {
        "name": "the-ambiguous-legacy-override-migrates-anyway",
        "file": CATALOG,
        "old": "            if len(matches) == 1:\n",
        "new": "            if len(matches) >= 1:\n",
        "expect": [
            "test_neither_builtin_is_taken_over",
            "test_the_ambiguity_is_reported_with_both_candidates",
        ],
    },
    {
        # The kind half of the guard that answers a named entry by its name
        # alone. See "A NOTE ON THE TWO MUTATIONS FOR (n)" in the module
        # docstring for why exactly one fixture can see this.
        "name": "legacy-candidates-answers-a-named-entry-by-its-text",
        "file": IDENTITY,
        "old": "    if identity is None or identity[0] != TEXT_KIND:\n",
        "new": "    if identity is None:\n",
        "expect": ["test_a_doc_id_does_not_capture_a_pattern_of_the_same_text"],
    },
    {
        # The same rule broken at the merge's call site, where the harm is
        # the ordinary one.
        # Refreshed for .3.3, which gave legacy_candidates the entry as its
        # first parameter. The mutation is unchanged in meaning: it forces
        # the text key for every user entry.
        "name": "a-named-override-is-migrated-by-its-text",
        "file": CATALOG,
        "old": (
            "                matches = legacy_candidates("
            "user_entry, key, candidates)\n"
        ),
        "new": (
            "                forced = user_entry.get(\"pattern\")\n"
            "                matches = legacy_candidates(\n"
            "                    user_entry,\n"
            "                    (\"text\", forced.strip().casefold())\n"
            "                    if isinstance(forced, str) else None,\n"
            "                    candidates,\n"
            "                )\n"
        ),
        "expect": [
            "test_a_user_rule_with_its_own_doc_id_is_not_migrated_by_text",
        ],
    },
    {
        # Refreshed 2026-09-06. The anchor quoted the inline code that
        # became PatternManager._claimed_builtin, so this entry had been
        # stale since that extraction and the staleness went unseen because
        # every --check run since was narrowed with --only. The mutation is
        # unchanged in meaning: the listing stops reporting the A4 state.
        "name": "the-manager-does-not-mark-the-ambiguous-entry",
        "file": MANAGER,
        "old": (
            "        matches = legacy_candidates("
            "pat_data, user_key, system_candidates)\n"
            "        if len(matches) == 1:\n"
            "            return matches[0], False\n"
            "        return None, len(matches) > 1\n"
        ),
        "new": (
            "        matches = legacy_candidates("
            "pat_data, user_key, system_candidates)\n"
            "        if len(matches) == 1:\n"
            "            return matches[0], False\n"
            "        return None, False\n"
        ),
        "expect": [
            "test_an_ambiguous_legacy_override_is_listed_as_unresolved",
            "test_an_ambiguous_legacy_row_neither_counts_nor_is_counted",
        ],
    },
    {
        # This one entry drops tests/test_pattern_manager_dialog.py::
        # TestOpenEditorWiring from its run. That class holds none of this
        # mutation's catchers and the mutation cannot reach it, but with it
        # in the selection the run dies at exit status 3221226356
        # (0xC0000374, STATUS_HEAP_CORRUPTION) inside
        # pattern_manager_dialog.py _apply_screen_bounded_size, during a
        # PatternManagerDialog construction -- before populate, so the
        # mutated label branch plays no part in it. The gate then reports an
        # ERROR for a mutation whose three catchers had already failed.
        #
        # Measured 2026-09-06, three times each. With the class: exit
        # 3221226356, every time, in the full sweep and in two isolated
        # runs. Without it and nothing else changed: exit 1, "3 failed, 236
        # passed in 14.35s", exactly the three expected catchers red. The
        # other 64 mutations run the same selection with that class and do
        # not crash, so the difference is this mutation's three failures,
        # not the class on its own.
        #
        # The reading, recorded on the epic as a test-process artifact and
        # not as a defect in the shipped app: no dialog test destroys its
        # dialog, and pytest holds a failed test's frame for its report, so
        # three FAILED tests keep three PatternManagerDialog objects alive
        # that three PASSED tests would have released. Giving those test
        # files real teardown is the wider repair and is deliberately not
        # done here.
        "name": "the-list-does-not-mark-an-unresolved-override",
        "file": DIALOG,
        "selection": SELECTION_WITHOUT_EDITOR_WIRING,
        "old": '                elif pat.get("unresolved_override"):\n',
        "new": "                elif False:\n",
        "expect": [
            "test_the_list_marks_an_unresolved_override",
            "test_the_list_says_what_the_mark_means",
            "test_an_unresolved_entry_is_not_also_marked_user",
        ],
    },
    {
        "name": "the-detail-badge-does-not-say-unresolved",
        "file": DIALOG,
        "old": (
            "        unresolved = is_user and"
            ' bool(pat.get("unresolved_override"))\n'
        ),
        "new": "        unresolved = False\n",
        "expect": ["test_the_detail_badge_says_unresolved"],
    },
    {
        # wh-pattern-override-doc-id.2.1. The merge wrote the second
        # claimant over the first, so a rule the user wrote stopped
        # running and nothing said so.
        "name": "the-displaced-user-entry-is-dropped",
        "file": CATALOG,
        "old": (
            "                displaced = merged[index] if index in claimed else None\n"
        ),
        "new": "                displaced = None\n",
        "expect": [
            "test_both_claims_on_one_builtin_survive",
            "test_the_last_claim_holds_the_builtin_slot",
            "test_a_legacy_claim_does_not_evict_the_named_one",
            "test_the_displaced_entry_is_reported",
        ],
    },
    {
        # Inverting the check breaks both directions at once: a rule
        # with its own trigger is dropped, and a superseded copy is
        # appended where it can never match.
        "name": "the-supersede-check-is-inverted",
        "file": CATALOG,
        "old": (
            "                if displaced is not None and self._same_trigger(\n"
        ),
        "new": (
            "                if displaced is not None and not self._same_trigger(\n"
        ),
        "expect": [
            "test_both_claims_on_one_builtin_survive",
            "test_the_last_claim_holds_the_builtin_slot",
            "test_a_second_copy_of_one_trigger_supersedes_the_first",
            "test_the_last_of_two_legacy_copies_holds_the_slot",
        ],
    },
    {
        # A rule that moved out of its built-in's slot must not move
        # silently: the warning has to name the rule that moved.
        "name": "the-displaced-rule-is-not-named",
        "file": CATALOG,
        "old": '                        displaced.get("pattern"),\n',
        "new": '                        "",\n',
        "expect": ["test_the_displaced_entry_is_reported"],
    },
    {
        # The editor stops carrying the id for an edit in place, which is
        # the state that made the try-it preview place a draft somewhere
        # the save would not (wh-pattern-override-doc-id.2.2).
        "name": "an-edit-draft-drops-the-rules-identity",
        "file": EDITOR,
        "old": (
            "            if (keep_identity or self._edit_mode)\n"
            "            and is_valid_doc_id(stored_doc_id) else None\n"
        ),
        "new": (
            "            if keep_identity\n"
            "            and is_valid_doc_id(stored_doc_id) else None\n"
        ),
        "expect": [
            "test_the_edit_draft_lands_where_the_save_will_put_it",
            "test_the_edit_payload_carries_the_rules_own_doc_id",
            "test_an_advanced_edit_carries_it_as_well",
        ],
    },
    {
        # The other side of the same condition: carrying the id for every
        # dialog opened from an entry would hand a Duplicate the built-in
        # its original already claims.
        "name": "a-duplicate-draft-claims-the-builtin-too",
        "file": EDITOR,
        "old": (
            "            if (keep_identity or self._edit_mode)\n"
            "            and is_valid_doc_id(stored_doc_id) else None\n"
        ),
        "new": (
            "            if (keep_identity or entry is not None)\n"
            "            and is_valid_doc_id(stored_doc_id) else None\n"
        ),
        "expect": [
            "test_a_duplicate_save_sends_no_doc_id",
            "test_a_duplicate_still_carries_no_id",
        ],
    },
    {
        # wh-pattern-override-doc-id.3.2. The built-in's identity was
        # repointed at the row a displaced claimant had been moved to, so
        # the next claimant of that built-in landed on the moved rule
        # instead of the built-in's own position -- and stayed at the end
        # of the command order itself.
        "name": "the-builtin-identity-follows-the-displaced-row",
        "file": CATALOG,
        "old": (
            "                        and displaced_key not in builtin_slots\n"
        ),
        "new": "",
        "expect": [
            "test_the_last_claimant_still_takes_the_builtin_slot",
            "test_a_disabled_claimant_is_kept_through_the_same_sequence",
        ],
    },
    {
        # The same branch as it stood before .3.2: no owner recorded for
        # the appended row AND no anchor on the built-in's identity. This
        # is the state that lost a saved rule outright.
        "name": "the-displaced-row-keeps-its-pre-fix-bookkeeping",
        "file": CATALOG,
        "old": (
            "                    claimed.add(len(merged) - 1)\n"
            "                    displaced_key = self._merge_key(displaced)\n"
            "                    if (\n"
            "                        displaced_key is not None\n"
            "                        and displaced_key not in builtin_slots\n"
        ),
        "new": (
            "                    displaced_key = self._merge_key(displaced)\n"
            "                    if (\n"
            "                        displaced_key is not None\n"
        ),
        "expect": [
            "test_all_three_claimants_still_run",
            "test_the_last_claimant_still_takes_the_builtin_slot",
            "test_a_repeated_legacy_expression_still_supersedes_its_own_copy",
            "test_a_disabled_claimant_is_kept_through_the_same_sequence",
        ],
    },
    {
        # The other half of the anchor: a key the user's own entry
        # introduced still has to follow that entry to its new row, or a
        # second copy of one expression joins the copy that moved instead
        # of superseding it, and two rules end up on one phrase.
        "name": "the-displaced-rows-own-key-does-not-follow-it",
        "file": CATALOG,
        "old": (
            "                    if (\n"
            "                        displaced_key is not None\n"
            "                        and displaced_key not in builtin_slots\n"
            "                        and key_to_index.get(displaced_key) == index\n"
            "                    ):\n"
        ),
        "new": (
            "                    if False:\n"
        ),
        "expect": [
            "test_a_repeated_legacy_expression_still_supersedes_its_own_copy",
        ],
    },
    {
        # wh-pattern-override-doc-id.3.4. The merge places a user entry
        # before the catalog tries to build anything, so a rule whose
        # expression the build rejects used to take a built-in's slot and
        # leave the command with no responder at all. This mutation drops
        # the whole refusal.
        "name": "an-unbuildable-rule-still-claims-a-slot",
        "file": BUILDABLE,
        "old": (
            "    if not can_build_expression(entry):\n"
            "        return None\n"
            "    return entry_identity(entry)\n"
        ),
        "new": "    return entry_identity(entry)\n",
        "expect": [
            "test_the_builtin_still_responds[empty-expression]",
            "test_the_builtin_still_responds[invalid-regex]",
            "test_the_builtin_still_responds[trailing-more-than-one-word]",
            "test_the_builtin_still_responds[trailing-requires-hotword]",
            "test_the_manager_does_not_call_it_an_override[empty-expression]",
            "test_the_manager_does_not_call_it_an_override[invalid-regex]",
            "test_the_manager_does_not_call_it_an_override"
            "[trailing-more-than-one-word]",
            "test_the_manager_does_not_call_it_an_override"
            "[trailing-requires-hotword]",
        ],
    },
    {
        # An empty expression is skipped by the build loop's opening
        # guard, so an entry carrying one produces no rule.
        "name": "an-empty-expression-counts-as-buildable",
        "file": BUILDABLE,
        "old": (
            "    if not isinstance(expression, str) or not expression:\n"
        ),
        "new": "    if not isinstance(expression, str):\n",
        "expect": [
            "test_the_predicate_agrees_with_the_catalog[empty-expression]",
            "test_the_builtin_still_responds[empty-expression]",
            "test_the_manager_does_not_call_it_an_override[empty-expression]",
        ],
    },
    {
        # An expression re.compile rejects is dropped at build time, so
        # the entry has to be refused the slot before then.
        "name": "an-expression-that-cannot-compile-counts-as-buildable",
        "file": BUILDABLE,
        "old": (
            "    try:\n"
            "        transformed, _metadata = transform_pattern(expression)\n"
            "        re.compile(transformed, re.IGNORECASE)\n"
            "    except re.error:\n"
            "        return False\n"
            "    return True\n"
        ),
        "new": "    return True\n",
        "expect": [
            "test_the_predicate_agrees_with_the_catalog[invalid-regex]",
            "test_the_builtin_still_responds[invalid-regex]",
            "test_the_manager_does_not_call_it_an_override[invalid-regex]",
        ],
    },
    {
        # The v1 trailing contract is one literal word; anything else is
        # rejected by _build_trailing_entry.
        "name": "a-trailing-expression-of-any-shape-counts-as-buildable",
        "file": BUILDABLE,
        "old": (
            "        return _TRAILING_WORD_RE.fullmatch(candidate) is not None\n"
        ),
        "new": "        return True\n",
        "expect": [
            "test_the_predicate_agrees_with_the_catalog"
            "[trailing-more-than-one-word]",
            "test_the_builtin_still_responds[trailing-more-than-one-word]",
            "test_the_manager_does_not_call_it_an_override"
            "[trailing-more-than-one-word]",
        ],
    },
    {
        # A hotword must precede its command and a trailing command must
        # be the last word, so the build refuses the pair.
        "name": "a-trailing-entry-may-require-the-hotword",
        "file": BUILDABLE,
        "old": (
            '        if entry.get("requires_hotword", False):\n'
            "            return False\n"
        ),
        "new": "",
        "expect": [
            "test_the_predicate_agrees_with_the_catalog"
            "[trailing-requires-hotword]",
            "test_the_builtin_still_responds[trailing-requires-hotword]",
            "test_the_manager_does_not_call_it_an_override"
            "[trailing-requires-hotword]",
        ],
    },
    {
        # .3.5. Back to re-deriving the placement from the built list,
        # which has lost the user file's order, the rows the build
        # dropped, and a legacy resolution's attached identity.
        "name": "the-preview-re-derives-the-placement",
        "file": TESTER,
        "old": (
            "    draft_block = _draft_block(draft) "
            "if catalog is not None else None\n"
        ),
        "new": "    draft_block = None\n",
        "expect": [
            "test_the_preview_and_the_save_agree_when_two_rules_claim_one_builtin",
            "test_the_preview_and_the_save_agree_when_the_claimant_has_no_row",
            "test_the_preview_and_the_save_agree_for_a_legacy_resolved_override",
            "test_the_stored_id_beats_the_id_the_draft_carries",
        ],
    },
    {
        # .3.5. ``update_pattern`` keeps the block's stored id and ignores
        # the draft's; a simulation that took the draft's would move the
        # rule to a different built-in's slot.
        # Refreshed for .3.3, which added the origin handling below the id
        # handling. The mutation still removes only the id half, so the
        # origin lines are carried through unchanged.
        "name": "the-edited-block-takes-the-drafts-own-id",
        "file": TESTER,
        "old": (
            "    block = dict(draft_block)\n"
            "    block.pop(DOC_ID_KEY, None)\n"
            "    stored_doc_id = existing.get(DOC_ID_KEY)\n"
            "    if is_valid_doc_id(stored_doc_id):\n"
            "        block[DOC_ID_KEY] = stored_doc_id\n"
            "    block.pop(ORIGIN_KEY, None)\n"
        ),
        "new": (
            "    block = dict(draft_block)\n"
            "    block.pop(ORIGIN_KEY, None)\n"
        ),
        "expect": [
            "test_the_stored_id_beats_the_id_the_draft_carries",
        ],
    },
    {
        # .3.5. An edit modelled as a create: the old block stays and the
        # draft is appended, so the rule loses the slot it holds.
        "name": "the-edited-block-is-always-appended",
        "file": TESTER,
        "old": "            not replaced\n",
        "new": "            False\n",
        # test_the_preview_leaves_the_catalog_alone does NOT belong here,
        # measured: that test only asks whether the catalog changed, and an
        # append changes nothing about the catalog.
        "expect": [
            "test_the_preview_and_the_save_agree_when_two_rules_claim_one_builtin",
            "test_the_stored_id_beats_the_id_the_draft_carries",
        ],
    },
    {
        # .3.5. The draft's row is a USER row. Found by expression alone,
        # a built-in carrying the same expression is read as the draft.
        "name": "the-draft-row-is-found-by-expression-alone",
        "file": TESTER,
        "old": (
            '        if entry.get("is_user") '
            'and entry.get("raw_pattern") == expression:\n'
        ),
        "new": '        if entry.get("raw_pattern") == expression:\n',
        "expect": [
            "test_a_builtin_sharing_the_expression_is_not_the_draft",
        ],
    },
    {
        # .3.5. Without the loader's own tag, a block the caller added
        # carries no _source_file, the build reads it as a shipped entry,
        # is_user comes out false, and the draft's row cannot be found.
        "name": "the-source-file-tag-is-dropped",
        "file": CATALOG,
        "old": (
            "        tagged = self._tag_source(user_entries, "
            "self._user_patterns_file)\n"
            "        merged = self._merge_entries("
            "self._raw_system_entries, tagged)\n"
        ),
        "new": (
            "        merged = self._merge_entries("
            "self._raw_system_entries, user_entries)\n"
        ),
        # The measured catcher set. Two of the three agreement tests do NOT
        # belong here: with the tag gone the draft's row is read as a
        # shipped row, the preview finds no draft row and reports a loss,
        # and in those two cases the save makes the draft lose as well, so
        # the two answers still agree.
        "expect": [
            "test_the_preview_and_the_save_agree_when_the_claimant_has_no_row",
            "test_the_stored_id_beats_the_id_the_draft_carries",
            "test_one_claimant_edited_in_place",
            "test_an_edit_that_reaches_another_builtins_phrase",
            "test_the_edited_blocks_own_trigger_is_not_a_collision",
        ],
    },
    {
        # .3.5. Asking is not saving. The Logic process goes on matching
        # speech against this catalog while the dialog is open.
        "name": "build-from-user-entries-stores-its-result",
        "file": CATALOG,
        "old": (
            "        _first_words, all_patterns, _count, _trailing = (\n"
            "            self._build_structures(merged, self._patterns_file)\n"
            "        )\n"
            "        return all_patterns\n"
        ),
        "new": (
            "        _first_words, all_patterns, _count, _trailing = (\n"
            "            self._build_structures(merged, self._patterns_file)\n"
            "        )\n"
            "        self.all_patterns = all_patterns\n"
            "        return all_patterns\n"
        ),
        "expect": [
            "test_the_preview_leaves_the_catalog_alone",
        ],
    },
    {
        # .3.5. The duplicate-trigger check back on the built list, which
        # has dropped every user block the build rejected. The save reads
        # the file, so it still sees them and still refuses.
        "name": "the-duplicate-check-reads-the-built-list",
        "file": TESTER,
        "old": "    if catalog is not None:\n",
        "new": "    if False:\n",
        "expect": [
            "test_the_preview_reports_the_refusal_in_the_saves_words",
        ],
    },
    {
        # .3.6. The count reports the claimants, not the OTHERS. The only
        # claimant of a built-in then reports one other, so the window
        # refuses to promise a restoration it would really perform.
        "name": "the-claimant-count-includes-the-row-itself",
        "file": MANAGER,
        "old": (
            "                        claim_counts[claimed] - 1"
            " if claimed is not None else 0\n"
        ),
        "new": (
            "                        claim_counts[claimed]"
            " if claimed is not None else 0\n"
        ),
        # The measured set: every listed test. A row that claims a
        # built-in alone reports one other, so both the counting
        # tests and the must-not-count tests fail.
        "expect": [
            "test_a_named_claimant_and_a_legacy_one_count_each_other",
            "test_a_remaining_no_action_claimant_is_counted",
            "test_an_ambiguous_legacy_row_neither_counts_nor_is_counted",
            "test_an_independent_user_rule_claims_nothing",
            "test_an_unbuildable_row_claims_nothing",
            "test_claimants_of_different_builtins_do_not_count_each_other",
            "test_the_only_claimant_reports_no_others",
            "test_three_claimants_each_report_two_others",
            "test_two_claimants_of_one_builtin_each_report_one_other",
        ],
    },
    {
        # .3.6. The absent-not-zero contract broken: every claiming row
        # carries the key, so a reader testing presence reads "others
        # remain" for the last claimant.
        "name": "the-count-key-is-written-when-it-is-zero",
        "file": MANAGER,
        "old": "        if other_claimants > 0:\n",
        "new": "        if True:\n",
        # The measured set. The counting tests do NOT belong here:
        # they read a count that is already non-zero, and writing a
        # zero as well changes nothing about it.
        "expect": [
            "test_an_ambiguous_legacy_row_neither_counts_nor_is_counted",
            "test_an_independent_user_rule_claims_nothing",
            "test_an_unbuildable_row_claims_nothing",
            "test_claimants_of_different_builtins_do_not_count_each_other",
            "test_the_only_claimant_reports_no_others",
        ],
    },
    {
        # .3.6. The legacy claim resolved to the ROW's own text key instead
        # of the built-in the merge migrates it to, so a pre-doc_id override
        # and a named override of one built-in stop seeing each other.
        "name": "the-legacy-claim-keeps-the-rows-own-key",
        "file": MANAGER,
        "old": "            return matches[0], False\n",
        "new": "            return user_key, False\n",
        # The measured set, one test. Only the mixed case pairs a
        # legacy row with a named one; everywhere else the resolved
        # built-in and the row's own key give the same count.
        "expect": [
            "test_a_named_claimant_and_a_legacy_one_count_each_other",
        ],
    },
    {
        # .3.6. A row the merge refuses to place still claims a built-in, so
        # it is counted as one of the claimants keeping that built-in
        # suppressed when it replaces nothing at all.
        "name": "an-unplaceable-row-still-claims-a-builtin",
        "file": MANAGER,
        "old": "        return None, len(matches) > 1\n",
        "new": "        return user_key, len(matches) > 1\n",
        # The measured set. It reaches beyond this bead's own file
        # because the badge reads the same helper: a row that
        # claims nothing must not be badged as an override either.
        "expect": [
            "test_a_user_pattern_with_its_own_doc_id_overrides_nothing",
            "test_a_user_regex_equal_to_a_doc_id_does_not_claim_that_builtin",
            "test_an_ambiguous_legacy_override_is_listed_as_unresolved",
            "test_an_ambiguous_legacy_row_neither_counts_nor_is_counted",
            "test_an_independent_user_rule_claims_nothing",
            "test_an_independent_user_rule_is_neither",
        ],
    },
    {
        # .3.6. A doc_id no shipped pattern carries is treated as a claim on
        # a built-in, so an ordinary user rule is counted against one.
        "name": "a-direct-claim-is-not-checked-against-the-system-file",
        "file": MANAGER,
        "old": (
            "        if user_key is not None and user_key in system_keys:\n"
        ),
        "new": "        if user_key is not None:\n",
        # The measured set, the badge tests included for the same
        # reason as the mutation above.
        "expect": [
            "test_a_named_claimant_and_a_legacy_one_count_each_other",
            "test_a_user_pattern_with_its_own_doc_id_overrides_nothing",
            "test_a_user_regex_equal_to_a_doc_id_does_not_claim_that_builtin",
            "test_an_ambiguous_legacy_override_is_listed_as_unresolved",
            "test_an_ambiguous_legacy_row_neither_counts_nor_is_counted",
            "test_an_independent_user_rule_claims_nothing",
            "test_an_independent_user_rule_is_neither",
        ],
    },
    {
        # .3.6. The hover help promises restoration to every copy again,
        # so a person deletes a rule they wanted and the built-in stays
        # switched off by the copies that remain.
        "name": "the-tooltip-promises-restoration-to-every-copy",
        "file": DIALOG,
        "old": '            others = pat.get("other_claimants", 0)\n',
        "new": "            others = 0\n",
        # The measured set. The confirmation tests are absent
        # because this mutation reaches only the hover help.
        "expect": [
            "test_the_badge_tooltip_counts_them",
            "test_the_badge_tooltip_says_removing_this_copy_is_not_enough",
            "test_the_plural_form_carries_no_parenthesised_s",
        ],
    },
    {
        # .3.6. The same promise back in the confirmation box, which is
        # the last thing the person reads before agreeing.
        "name": "the-confirmation-promises-restoration-to-every-copy",
        "file": DIALOG,
        # The comment line above the target comes with it: the
        # eight-space form is a substring of the twelve-space tooltip
        # line, so on its own it matches twice.
        "old": (
            "        # the older sentence true: the catalog keeps "
            "those rows on purpose.\n"
            '        others = pat.get("other_claimants", 0)\n'
        ),
        "new": (
            "        # the older sentence true: the catalog keeps "
            "those rows on purpose.\n"
            "        others = 0\n"
        ),
        # The measured set, the other half of the pair above.
        "expect": [
            "test_the_confirmation_counts_them",
            "test_the_confirmation_says_the_builtin_will_not_take_over",
        ],
    },
    {
        # .3.6. One other copy is described in the plural: "1 other
        # customizations also replace it".
        "name": "one-other-copy-takes-the-plural-words",
        "file": DIALOG,
        "old": "    if count == 1:\n",
        "new": "    if False:\n",
        # The measured set: the two one-other tests. The counting
        # tests use two and three, which already take the plural.
        "expect": [
            "test_the_badge_tooltip_says_removing_this_copy_is_not_enough",
            "test_the_confirmation_says_the_builtin_will_not_take_over",
        ],
    },
    {
        # .3.6. Every count is described in the singular, so three
        # remaining copies read as one and the person removes one and
        # expects the built-in back.
        "name": "every-count-takes-the-singular-words",
        "file": DIALOG,
        "old": '    return f"{count} other customizations", "replace"\n',
        "new": '    return "One other customization", "replaces"\n',
        # The measured set: the counting tests. The one-other
        # tests keep passing, because singular is right for them.
        "expect": [
            "test_the_badge_tooltip_counts_them",
            "test_the_confirmation_counts_them",
            "test_the_plural_form_carries_no_parenthesised_s",
        ],
    },
    {
        # wh-pattern-override-doc-id.3.3. The guard that keeps a rule
        # the person created out of the legacy text migration is gone,
        # so a Duplicate takes over the built-in whose words it copied.
        # This is the bead's defect restored at its source.
        "name": "the-legacy-migration-ignores-the-own-rule-mark",
        "file": IDENTITY,
        "old": (
            "    if is_own_rule(entry):\n"
            "        return []\n"
            "    if identity is None or identity[0] != TEXT_KIND:\n"
        ),
        "new": (
            "    if False:\n"
            "        return []\n"
            "    if identity is None or identity[0] != TEXT_KIND:\n"
        ),
        "expect": [
            "test_a_new_rule_copying_a_builtin_does_not_take_it_over",
            "test_a_switched_off_duplicate_switches_nothing_off",
            "test_both_rules_are_present",
            "test_saving_a_duplicate_leaves_the_builtin_running",
            "test_the_builtin_still_runs_its_own_action",
            "test_the_listing_does_not_call_it_an_override",
        ],
    },
    {
        # Presence of the key decides instead of its value, so a
        # hand-written origin detaches a customization from the
        # built-in it replaces.
        "name": "the-own-rule-mark-accepts-any-origin-value",
        "file": IDENTITY,
        "old": (
            "        entry.get(ORIGIN_KEY) == ORIGIN_OWN\n"
            "        and DOC_ID_KEY not in entry\n"
            "    )\n"
        ),
        "new": (
            "        ORIGIN_KEY in entry\n"
            "        and DOC_ID_KEY not in entry\n"
            "    )\n"
        ),
        "expect": [
            "test_a_hand_written_origin_value_is_ignored",
        ],
    },
    {
        # The second half of is_own_rule is gone, so a block that names
        # a built-in badly is read as an independent rule instead of
        # falling back to the text key the way identity_from documents.
        "name": "the-own-rule-mark-ignores-a-doc-id-the-block-carries",
        "file": IDENTITY,
        "old": (
            "    return (\n"
            "        entry.get(ORIGIN_KEY) == ORIGIN_OWN\n"
            "        and DOC_ID_KEY not in entry\n"
            "    )\n"
        ),
        "new": (
            "    return (\n"
            "        entry.get(ORIGIN_KEY) == ORIGIN_OWN\n"
            "        and True\n"
            "    )\n"
        ),
        "expect": [
            "test_a_block_naming_a_builtin_badly_still_falls_back_to_text",
        ],
    },
    {
        # create_pattern stops saying the block is the person's own, so
        # a Duplicate is saved in the shape the merge reads as a rule
        # written before ids existed.
        "name": "create-does-not-mark-the-block-it-writes",
        "file": MANAGER,
        "old": (
            "                own_rule=True,\n"
            "            )\n"
        ),
        "new": (
            "                own_rule=False,\n"
            "            )\n"
        ),
        "expect": [
            "test_a_customize_carries_both_keys",
            "test_a_duplicate_carries_the_key",
            "test_a_new_rule_carries_the_key",
            "test_a_new_rule_copying_a_builtin_does_not_take_it_over",
            "test_saving_a_duplicate_leaves_the_builtin_running",
        ],
    },
    {
        # update_pattern adds the key instead of preserving it, so an
        # ordinary edit of a customization saved before ids existed
        # turns that rule into an independent one and the built-in
        # starts answering again.
        "name": "an-update-marks-every-block-it-touches",
        "file": MANAGER,
        "old": (
            "                own_rule=original_origin == ORIGIN_OWN,\n"
        ),
        "new": (
            "                own_rule=True,\n"
        ),
        "expect": [
            "test_an_edit_does_not_add_the_key_to_a_pre_doc_id_block",
        ],
    },
    {
        # _build_block_lines never writes the line, so no block on disk
        # ever carries the mark whatever the caller asked for.
        "name": "the-block-writer-omits-the-origin-line",
        "file": MANAGER,
        "old": (
            "        if own_rule:\n"
            "            lines.append(\n"
            "                f\"{ORIGIN_KEY} = {cls._format_toml_value(ORIGIN_OWN)}\"\n"
            "            )\n"
        ),
        "new": (
            "        if False:\n"
            "            lines.append(\n"
            "                f\"{ORIGIN_KEY} = {cls._format_toml_value(ORIGIN_OWN)}\"\n"
            "            )\n"
        ),
        "expect": [
            "test_a_customize_carries_both_keys",
            "test_a_duplicate_carries_the_key",
            "test_a_new_rule_carries_the_key",
            "test_a_new_rule_copying_a_builtin_does_not_take_it_over",
            "test_an_edit_keeps_the_key_the_block_already_had",
            "test_saving_a_duplicate_leaves_the_builtin_running",
        ],
    },
    {
        # _draft_block stops marking the block a create would write, so
        # the try-it preview shows a new rule taking over the built-in
        # whose words it copied while the save leaves that built-in
        # answering. The two lines above the target are carried in the
        # pattern because the four-space assignment is a substring of
        # the eight-space copy in _edited_block below it.
        "name": "the-previewed-new-block-is-not-marked",
        "file": TESTER,
        "old": (
            "    if is_valid_doc_id(draft_doc_id):\n"
            "        block[DOC_ID_KEY] = draft_doc_id\n"
            "    block[ORIGIN_KEY] = ORIGIN_OWN\n"
            "    return block\n"
        ),
        "new": (
            "    if is_valid_doc_id(draft_doc_id):\n"
            "        block[DOC_ID_KEY] = draft_doc_id\n"
            "    return block\n"
        ),
        "expect": [
            "test_a_new_rule_copying_a_builtin_does_not_take_it_over",
        ],
    },
    {
        # _edited_block stops removing the mark _draft_block put on the
        # draft, so an edit of a customization saved before ids existed
        # previews as an independent rule while the save keeps it
        # attached to its built-in.
        "name": "the-previewed-edit-keeps-the-drafts-own-mark",
        "file": TESTER,
        "old": (
            "    block.pop(ORIGIN_KEY, None)\n"
            "    if existing.get(ORIGIN_KEY) == ORIGIN_OWN:\n"
            "        block[ORIGIN_KEY] = ORIGIN_OWN\n"
            "    return block\n"
        ),
        "new": (
            "    if existing.get(ORIGIN_KEY) == ORIGIN_OWN:\n"
            "        block[ORIGIN_KEY] = ORIGIN_OWN\n"
            "    return block\n"
        ),
        "expect": [
            "test_an_edit_that_keeps_a_pre_doc_id_overrides_words",
        ],
    },
    {
        # .3.7. The walk counts header-looking lines without asking the
        # parser which of them it also sees as a header, so a line inside
        # a person's multi-line string is counted with the real ones and
        # every block after it is one place out.
        "name": "the-header-count-does-not-ask-the-parser",
        "file": BLOCK_TEXT,
        "old": (
            "        if is_pattern_header(lines[i]) and _stands_at_top_level(\n"
            "            lines, anchor, i,\n"
            "        ):\n"
        ),
        "new": "        if is_pattern_header(lines[i]):\n",
        # Three names, not the five the decoy classes hold. Measured
        # by running this mutation: with the count wrong the two
        # lines land in the decoy string, and the whole-file check
        # then sees a table nobody named change, refuses, and writes
        # nothing at all. So the two tests that ask whether the typed
        # text was edited -- test_the_typed_example_is_not_edited and
        # test_the_decoys_own_expression_is_untouched -- stay green
        # under the mutation, and only the tests that ask whether the
        # right block was NAMED go red. The check masks them on
        # purpose; naming them here would report a false survivor.
        "expect": [
            "test_the_real_override_is_the_one_named",
            "test_the_command_still_answers_after_a_rewrite",
            "test_the_file_is_left_exactly_as_it_was",
        ],
    },
    {
        # .3.7. The same question at the other end of the block: a block
        # holding a header-looking line inside its own action is cut in
        # the middle of that string, and what was located no longer
        # parses, so the block is passed over and never named.
        "name": "the-block-end-does-not-ask-the-parser",
        "file": BLOCK_TEXT,
        "old": (
            "                    if ends_the_block and _stands_at_top_level(\n"
            "                        lines, start, j,\n"
            "                    ):\n"
        ),
        "new": "                    if ends_the_block:\n",
        "expect": [
            "test_the_block_is_still_named",
        ],
    },
    {
        # .3.1, replacing the-located-block-is-not-checked-before-writing.
        # That mutation disabled the CALL to _block_takes_the_id, and the
        # .3.7 walk fix masked it: with the count correct, the located
        # block is the right one, so skipping the check changes nothing
        # observable and no test can go red. The check is kept as the
        # per-block half of the two defences, and this mutation is what
        # proves its contract is tested: a block that already names a
        # built-in is not one this migration may claim.
        "name": "a-block-already-naming-a-builtin-is-taken-anyway",
        "file": CATALOG,
        "old": (
            "        return (\n"
            "            table.get(\"pattern\") == expression\n"
            "            and DOC_ID_KEY not in table\n"
            "        )\n"
        ),
        "new": "        return table.get(\"pattern\") == expression\n",
        "expect": [
            "test_a_block_already_naming_a_builtin_is_not_taken",
        ],
    },
    {
        # .3.1. One located range must hold exactly one table. Accepting
        # more would let a write land in a range covering two blocks.
        "name": "more-than-one-table-is-still-one-block",
        "file": CATALOG,
        "old": (
            "        if len(tables) != 1 or not isinstance(tables[0], dict):\n"
        ),
        "new": (
            "        if not tables or not isinstance(tables[0], dict):\n"
        ),
        "expect": [
            "test_two_tables_are_not_one_block",
        ],
    },
    {
        # .3.7. The whole-file check agrees with anything, so a file that
        # differs from the original by more than the two names on the
        # named blocks is installed without a word.
        "name": "the-whole-file-check-always-agrees",
        "file": CATALOG,
        "old": (
            "        before = before_doc.pop(\"pattern\", [])\n"
        ),
        "new": (
            "        return True\n"
            "        before = before_doc.pop(\"pattern\", [])\n"
        ),
        "expect": [
            "test_a_block_that_was_not_named_may_not_change",
            "test_a_named_block_may_change_nothing_else",
            "test_a_block_may_not_appear",
            "test_the_hotword_may_not_change",
            "test_a_block_nobody_named_may_not_take_the_keys",
            "test_the_name_written_must_be_a_doc_id",
            "test_the_other_key_must_say_the_person_owns_it",
        ],
    },
    {
        # .3.8. The write happens and the entries this load retains never
        # take the names it wrote, so every later reader -- the try-it
        # preview above all -- works from a snapshot the file disagrees
        # with until the next load.
        "name": "the-retained-entries-do-not-take-what-was-written",
        "file": CATALOG,
        "old": (
            "            for file_position in "
            "self._write_recovered_ids(targets):\n"
        ),
        "new": (
            "            self._write_recovered_ids(targets)\n"
            "            for file_position in ():\n"
        ),
        "expect": [
            "test_a_written_name_reaches_the_retained_entries",
        ],
    },
    {
        # .3.8. The other direction: the entries take a name for every
        # block the migration MEANT to write, so a refused or failed
        # write leaves the snapshot claiming an identity the file does
        # not carry -- the same disagreement, pointing the other way.
        "name": "the-retained-entries-take-a-name-nobody-wrote",
        "file": CATALOG,
        "old": (
            "            for file_position in "
            "self._write_recovered_ids(targets):\n"
        ),
        "new": (
            "            self._write_recovered_ids(targets)\n"
            "            for file_position in targets:\n"
        ),
        "expect": [
            "test_a_refused_write_leaves_the_entries_alone",
        ],
    },
    {
        # .3.7. The whole-file check still refuses and the caller writes
        # anyway, so a file the check judged wrong is installed with only
        # a warning to show for it. the-whole-file-check-always-agrees
        # mutates the check itself; this one leaves the check alone and
        # makes the caller ignore what it said.
        #
        # The three-line old text is what makes the match unique:
        # "return set()" appears three times in _write_recovered_ids, and
        # "path, sorted(written)," appears only in this warning. Removing
        # the return leaves the logger.warning call as the if body, so
        # the mutant still compiles.
        "name": "the-refused-correspondence-check-writes-anyway",
        "file": CATALOG,
        "old": (
            "                    path, sorted(written),\n"
            "                )\n"
            "                return set()\n"
        ),
        "new": (
            "                    path, sorted(written),\n"
            "                )\n"
        ),
        # One catcher on purpose. The check never refuses on unmutated
        # input, so no other test's behaviour changes under this mutation.
        "expect": [
            "test_a_refused_correspondence_check_leaves_the_file_byte_identical",
        ],
    },
]

# Every file a mutation can rewrite. Used to clear exactly the bytecode
# caches that could hold a stale compile of a mutated module.
TARGET_FILES = sorted({mut["file"] for mut in MUTATIONS}, key=str)


def _target(mut):
    return mut["file"]


def _selection(mut):
    return list(mut.get("selection", SELECTION))


def _clear_bytecode():
    """Drop the caches beside the mutated modules only.

    Python decides whether cached bytecode is current from the source's
    (mtime, size). Several mutations here keep the file the same length, so
    a restore fast enough to leave both unchanged would hand the next run
    the other version's bytecode. Only the mutated modules' own cache
    directories are touched -- a glob over the service would walk .venv.
    """
    for path in {p.parent / "__pycache__" for p in TARGET_FILES}:
        shutil.rmtree(path, ignore_errors=True)


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
    """Real test ids in the selection, so a rename cannot read as a survivor.

    A name that no longer exists can never appear in the failed set, so a
    genuine catch would be reported as a survivor and the reader would go
    hunting for a test that is sitting there passing.
    """
    out = _pytest(*selection, "--collect-only", "-q", "-p", "no:randomly")
    return {_test_id(line) for line in out.stdout.splitlines() if "::" in line}


def failed_names(output):
    return {
        _test_id(line)
        for line in output.splitlines()
        if line.startswith("FAILED ")
    }


def error_names(output):
    """Test ids on ERROR records: the -v per-test lines and the -rE summary.

    pytest reports an ERROR, not a FAILED, when a fixture raises or a test
    errors rather than fails, and a collection error prints only the summary
    form. Either would otherwise be read as a verdict.

    The summary form is read ONLY from the short-summary section. A captured
    log record at ERROR level opens with the same word and names a logger
    instead of a test, so reading it anywhere turned a genuine catch into an
    error: the catcher for an-unbuildable-entry-still-claims-its-doc-id makes
    the catalog log "ERROR speech.pattern_catalog:...", which carries no "::",
    so the id came out as the bare word "ERROR"
    (wh-pattern-override-doc-id.2.3). Captured output is always printed before
    the short-summary banner, so the split separates the two for good.
    """
    lines = output.splitlines()
    summary_at = next(
        (
            i
            for i, line in enumerate(lines)
            if "short test summary info" in line
        ),
        len(lines),
    )
    named = {
        _test_id(line)
        for line in lines
        if "::" in line and " ERROR" in line
    }
    named |= {
        _test_id(line)
        for line in lines[summary_at:]
        if line.startswith("ERROR ")
    }
    return named


def skipped_names(output):
    """Test ids on -v per-test lines that read ``...::name SKIPPED (...)``."""
    return {
        _test_id(line)
        for line in output.splitlines()
        if "::" in line and " SKIPPED" in line
    }


def run_defect(result):
    """A reason this run cannot yield a verdict, or None.

    The suite-timeout abort is the subtle one: pytest-timeout kills the
    process before the failure summary prints, so a runner that parses
    FAILED lines finds none and reports a survivor for a mutation earlier
    tests in the same run may have genuinely caught.
    """
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


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _build_mutant(mut):
    """Return (mutant_bytes, original_bytes, error) for one mutation.

    The line ending is detected per file: this repository mixes
    conventions, and an LF pattern matches nothing in a CRLF file. Every
    target here is Python, so every mutant is compiled before it is
    written -- a mutant that does not parse reads as `caught` while proving
    nothing, because the interpreter rejects it before a test runs.
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

    A False is a hard condition for the caller, not a warning: a run whose
    target is still mutated cannot yield a verdict, and the mutant sits in a
    TRACKED file that every later run and every other session sharing the
    checkout reads as real code.

    write_bytes, never write_text: on Windows write_text rewrites every line
    ending as CRLF, which silently breaks every multi-line pattern in this
    file on the next run. A second Ctrl+C landing inside the restore is
    held, retried once, and re-raised only AFTER the bytecode caches are
    cleared -- a same-size mutant otherwise leaves current-looking bytecode
    for the next run to import.
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


def _apply_and_run(path: Path, mutant: bytes, original: bytes, selection):
    """Write the mutant, run the suite, restore. Return (result, error).

    The write is INSIDE the guarded block on purpose: an interrupt or an
    OSError landing during the write would otherwise leave the tracked file
    mutated with nothing to put it back. A restore that fails clears
    ``result`` as well as setting an error, so no caller can read a verdict
    out of a run whose source file is still mutated.
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


def check_only(mutations):
    """Verify each selected pattern without running any test.

    This is NOT a sweep. It proves every pattern matches exactly once in the
    current source and that every mutant compiles; it cannot see a masked
    survivor. The compile half matters: a pattern can match exactly once and
    still produce text that does not parse, and a --check that answered only
    "do the patterns match" would report clean while the mutation could
    never run.
    """
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
    than a green run of nothing: every mutation here rewrites a tracked
    source file, so the operator has to be able to trust the printed scope.
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
            # Everything up to the next flag, so a later option is not read
            # as a mutation name.
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
    # The selection is resolved BEFORE the --check branch, so
    # `--check --only=<name>` checks the one pattern the operator asked
    # about and a --check with an unknown or empty --only exits non-zero.
    selected = _selected()
    if selected is None:
        return 1
    if "--check" in sys.argv[1:]:
        return check_only(selected)

    errors, caught, survived = [], [], []

    # The baseline runs the targets in the order they are DECLARED, which
    # is the order every per-mutation run uses (``_selection`` returns the
    # list unchanged). Sorting them here made the baseline run an order no
    # mutation run ever uses, and that order is not safe: it put the Qt
    # dialog tests immediately after the file that starts the safe_regex
    # multiprocessing pool. That pairing is a known hazard in this suite --
    # see the fixture docstring in tests/test_pattern_tester_doc_id.py and
    # the class docstring in tests/test_pattern_customize_vs_duplicate.py,
    # both of which record PatternManagerDialog reading a corrupted screen
    # object once the pool exists. Measured on the eleven targets: the
    # declared order passes 152 tests, the sorted order fails 9 in
    # tests/test_pattern_unresolved_override_badge.py with
    # ``'PySide6.QtCore.QObject' object has no attribute
    # 'availableGeometry'``.
    every_selection = []
    for m in selected:
        for target in _selection(m):
            if target not in every_selection:
                every_selection.append(target)
    real_names = collect_names(every_selection)
    print(f"collected {len(real_names)} test ids in {len(every_selection)} targets")
    for mut in selected:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    _clear_bytecode()
    baseline = _pytest(*every_selection, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    expected_all = {name for mut in selected for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(
            f"ERROR expected catchers skipped in the baseline: "
            f"{skipped_catchers}"
        )
        print(
            "      a skipped catcher can never fail, so its mutation cannot "
            "be proven"
        )
        return 1
    print("baseline green")

    for mut in selected:
        mutant, original, error = _build_mutant(mut)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        assert mutant is not None
        path = _target(mut)
        result, error = _apply_and_run(
            path, mutant, original, _selection(mut),
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
