"""The try-it preview answers the same question the save answers.

wh-pattern-override-doc-id.3.5. ``_simulate_merge`` was handed the already
merged, BUILT, filtered pattern list, and that list has lost three facts the
merge decides with: the user file's own order, the rows the build dropped (a
no-action override is absent from it entirely), and the built-in identity a
legacy text resolution attached to a row that carries no id of its own. So
the simulation guessed -- it replaced the FIRST row carrying the draft's
doc_id -- and a doc_id names a built-in association, not one saved block.

Every test here asks the preview and then performs the real save, reloads
the catalog, and asks what actually responds. The two answers must agree.
A test that only checked the simulated ORDER could be made to pass by
teaching the simulation a second wrong rule; comparing against the save
cannot.
"""

import pytest

from speech.pattern_manager import PatternManager
from speech.pattern_matcher import PatternMatcher
from speech.pattern_catalog import PatternCatalog
from speech.pattern_tester import run_test_draft, run_test_phrase


@pytest.fixture(autouse=True, scope="module")
def _close_the_bounded_regex_pool():
    """Leave no live match worker behind for the next test file.

    ``run_test_draft`` matches through ``speech.safe_regex.match_bounded``,
    which starts a spawn-context multiprocessing pool and keeps it alive for
    the rest of the process. A Qt test file that builds a dialog after that
    pool exists reads a corrupted screen object -- the hazard recorded in
    the same fixture in tests/test_pattern_tester_doc_id.py and in the class
    docstring of tests/test_pattern_customize_vs_duplicate.py. ``shutdown``
    is the module's own hygiene entry point.
    """
    yield
    from speech import safe_regex

    safe_regex.shutdown()


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'

# Three built-ins in a known order. The middle one exists so a rule placed
# at the FIRST built-in's slot and a rule placed at the LAST one's can be
# told apart: they answer a phrase they both match in different orders.
SYSTEM = (
    "[[pattern]]\n"
    'doc_id = "escape-key"\n'
    "pattern = '''^escape$'''\n"
    'actions = [{ function = "hk", params = ["escape"] }]\n'
    "\n"
    "[[pattern]]\n"
    'doc_id = "middle-thing"\n'
    "pattern = '''^middle$'''\n"
    'actions = [{ function = "hk", params = ["f5"] }]\n'
    "\n"
    "[[pattern]]\n"
    'doc_id = "undo-last"\n'
    "pattern = '''^undo$'''\n"
    'actions = [{ function = "hk", params = ["ctrl", "z"] }]\n'
)


def _user_block(expression, *, doc_id=None, output="typed", actions=None):
    lines = ["[[pattern]]\n"]
    if doc_id is not None:
        lines.append(f'doc_id = "{doc_id}"\n')
    lines.append(f"pattern = '''{expression}'''\n")
    if actions is None:
        lines.append(
            f'actions = [{{ function = "text", params = ["{output}"] }}]\n'
        )
    else:
        lines.append(actions)
    lines.append("\n")
    return "".join(lines)


class _Bench:
    """One system file, one user file, and the objects that read them."""

    def __init__(self, tmp_path, user_text):
        self.system_file = str(tmp_path / "patterns.toml")
        self.user_file = str(tmp_path / "user_patterns.toml")
        with open(self.system_file, "w", encoding="utf-8") as fh:
            fh.write(HOTWORD + SYSTEM)
        with open(self.user_file, "w", encoding="utf-8") as fh:
            fh.write(HOTWORD + user_text)
        self.catalog = PatternCatalog(self.system_file, self.user_file)
        self.manager = PatternManager(self.system_file, self.user_file)
        self.matcher = PatternMatcher(self.catalog)

    def preview(self, draft, text):
        return run_test_draft(
            draft, text, self.catalog.get_all_patterns(), self.matcher,
            catalog=self.catalog,
        )

    def save(self, pattern_id_hex, data):
        result = self.manager.update_pattern(pattern_id_hex, data)
        assert result["success"], result
        assert self.catalog.reload()
        return result

    def actual(self, text):
        return run_test_phrase(
            text, self.catalog.get_all_patterns(), self.matcher,
        )

    def responder(self, text):
        """The action text the winning rule types, or None."""
        answer = self.actual(text)
        assert answer["success"], answer
        match = answer["match"]
        if match is None:
            return None
        return match["resolved_steps"]


def _draft(expression, output, exclude_pattern_id, doc_id=None):
    """The editor's draft for an edit of an existing rule.

    ``doc_id`` is the id the EDITED BLOCK already carries. The editor sends
    it because ``update_pattern`` keeps it: the id belongs to the rule, not
    to the edit, so a draft that dropped it would preview a save that never
    happens.
    """
    draft = {
        "pattern_type": "command",
        "expression": expression,
        "action_type": "text",
        "action_params": {"output": output},
        "requires_hotword": False,
        "exclude_pattern_id": exclude_pattern_id,
    }
    if doc_id is not None:
        draft["doc_id"] = doc_id
    return draft


def _save_data(expression, output):
    return {
        "pattern_type": "command",
        "expression": expression,
        "action_type": "text",
        "action_params": {"output": output},
    }


def _typed(resolved_steps):
    """The text a resolved step list types."""
    return [
        step["params"][0]
        for step in resolved_steps
        if step.get("function") == "text"
    ]


class TestTwoRulesClaimOneBuiltin:
    """Case A. Editing the claimant the merge displaced, not the slot holder.

    Both user rules name ``escape-key``. The LAST one in file order holds
    the built-in's slot and the first is appended after every built-in. The
    simulation replaced the first row it found carrying that doc_id, which
    is the slot holder -- the wrong row -- and dropped it, so the draft
    appeared to win a phrase the slot holder actually keeps.
    """

    USER = (
        _user_block("^grow$", doc_id="escape-key", output="first")
        + _user_block("^expand$", doc_id="escape-key", output="second")
    )

    def _bench(self, tmp_path):
        return _Bench(tmp_path, self.USER)

    def test_the_preview_and_the_save_agree_when_two_rules_claim_one_builtin(
        self, tmp_path,
    ):
        bench = self._bench(tmp_path)
        edited = PatternManager.pattern_id("^grow$")
        draft = _draft(
            "^expand(?: slowly)?$", "draft", edited, doc_id="escape-key",
        )

        preview = bench.preview(draft, "expand")
        assert preview["success"], preview
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^expand(?: slowly)?$", "draft"))
        after = bench.actual("expand")
        assert after["success"], after

        won = preview["winner"] == "draft"
        actually_won = _typed(after["match"]["resolved_steps"]) == ["draft"]
        assert won == actually_won, (
            "the preview said the draft "
            f"{'wins' if won else 'loses'} but the save made it "
            f"{'win' if actually_won else 'lose'}"
        )

    def test_the_rule_holding_the_slot_still_answers_after_the_edit(
        self, tmp_path,
    ):
        """The save's own answer, stated on its own.

        The second rule holds the built-in's slot before and after the
        edit, because it is still the last claimant in file order. It is
        what "expand" reaches.
        """
        bench = self._bench(tmp_path)
        edited = PatternManager.pattern_id("^grow$")
        bench.save(edited, _save_data("^expand(?: slowly)?$", "draft"))
        assert _typed(bench.responder("expand")) == ["second"]

    def test_the_edited_rule_still_runs_under_its_own_phrase(self, tmp_path):
        """It is not lost, only later in the order."""
        bench = self._bench(tmp_path)
        edited = PatternManager.pattern_id("^grow$")
        bench.save(edited, _save_data("^expand(?: slowly)?$", "draft"))
        assert _typed(bench.responder("expand slowly")) == ["draft"]


class TestTheClaimantHasNoBuiltRow:
    """Case B. A no-action override holds a slot the built list cannot show.

    ``actions = []`` is how a user switches a built-in off. The rule still
    claims the built-in's identity in the merge, but the build produces no
    row for it, so the merged pattern list contains neither the override
    nor the built-in it suppressed. The simulation therefore saw no claim
    at all and appended the draft, while the save puts it at the
    suppressed built-in's own position.
    """

    USER = _user_block(
        "^grow$", doc_id="escape-key", actions="actions = []\n",
    )

    def test_the_preview_and_the_save_agree_when_the_claimant_has_no_row(
        self, tmp_path,
    ):
        bench = _Bench(tmp_path, self.USER)
        edited = PatternManager.pattern_id("^grow$")
        draft = _draft(
            "^undo(?: last)?$", "draft", edited, doc_id="escape-key",
        )

        preview = bench.preview(draft, "undo")
        assert preview["success"], preview
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^undo(?: last)?$", "draft"))
        after = bench.actual("undo")
        assert after["success"], after

        won = preview["winner"] == "draft"
        actually_won = (
            after["match"] is not None
            and _typed(after["match"]["resolved_steps"]) == ["draft"]
        )
        assert won == actually_won, (
            "the preview said the draft "
            f"{'wins' if won else 'loses'} but the save made it "
            f"{'win' if actually_won else 'lose'}"
        )

    def test_moving_the_suppression_restores_its_origin(
        self, tmp_path,
    ):
        """A trigger move drops the old suppression (wh-override-trigger-reenable-origin)."""
        bench = _Bench(tmp_path, self.USER)
        edited = PatternManager.pattern_id("^grow$")
        bench.save(edited, _save_data("^undo(?: last)?$", "draft"))
        assert bench.responder("escape") == [{"function": "hk", "params": ["escape"]}]
        assert bench.responder("undo") == [{"function": "hk", "params": ["ctrl", "z"]}]
        assert _typed(bench.responder("undo last")) == ["draft"]


class TestALegacyOverrideKeepsItsResolvedIdentity:
    """The third state the finding names: identity lost on the way out.

    A user rule saved before doc_ids existed carries none. The merge
    resolves it onto the built-in carrying the same expression and it takes
    that built-in's slot. The built row records no id, so a later draft
    naming that built-in could not find the slot in the built list.
    """

    USER = _user_block("^escape$", output="legacy")

    def test_the_preview_and_the_save_agree_for_a_legacy_resolved_override(
        self, tmp_path,
    ):
        bench = _Bench(tmp_path, self.USER)
        edited = PatternManager.pattern_id("^escape$")
        draft = _draft("^escape(?: now)?$", "draft", edited)

        preview = bench.preview(draft, "escape")
        assert preview["success"], preview
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^escape(?: now)?$", "draft"))
        after = bench.actual("escape")
        assert after["success"], after

        won = preview["winner"] == "draft"
        actually_won = (
            after["match"] is not None
            and _typed(after["match"]["resolved_steps"]) == ["draft"]
        )
        assert won == actually_won, (
            "the preview said the draft "
            f"{'wins' if won else 'loses'} but the save made it "
            f"{'win' if actually_won else 'lose'}"
        )


class TestWhatMustNotChange:
    """The states the simulation already answered correctly."""

    def test_one_claimant_edited_in_place(self, tmp_path):
        """The ordinary Customize edit: preview and save already agreed."""
        bench = _Bench(
            tmp_path,
            _user_block("^escape(?: key)?$", doc_id="escape-key", output="only"),
        )
        edited = PatternManager.pattern_id("^escape(?: key)?$")
        draft = _draft(
            "^escape(?: key)?$", "draft", edited, doc_id="escape-key",
        )

        preview = bench.preview(draft, "escape")
        assert preview["winner"] == "draft", preview

        bench.save(edited, _save_data("^escape(?: key)?$", "draft"))
        assert _typed(bench.responder("escape")) == ["draft"]

    def test_an_edit_that_reaches_another_builtins_phrase(self, tmp_path):
        """Dropping the origin lets this legacy rule resolve onto undo.

        The preview must model that same save-time detachment and load-time
        resolution (wh-override-trigger-reenable-origin).
        """
        bench = _Bench(
            tmp_path,
            _user_block("^grow$", doc_id="escape-key", output="only"),
        )
        edited = PatternManager.pattern_id("^grow$")
        draft = _draft("^undo$", "draft", edited, doc_id="escape-key")

        preview = bench.preview(draft, "undo")
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^undo$", "draft"))
        after = bench.actual("undo")
        won = preview["winner"] == "draft"
        actually_won = (
            after["match"] is not None
            and _typed(after["match"]["resolved_steps"]) == ["draft"]
        )
        assert won == actually_won, (
            "the preview said the draft "
            f"{'wins' if won else 'loses'} but the save made it "
            f"{'win' if actually_won else 'lose'}"
        )


class TestTheRulesTheSimulationMustCopy:
    """Three save behaviours the simulation is easy to get wrong.

    Each puts the draft in a different slot depending on which rule the
    simulation follows, and the phrase under test is one that two rules
    answer, so the slot decides the winner.
    """

    def test_the_stored_id_beats_the_id_the_draft_carries(self, tmp_path):
        """``update_pattern`` keeps the block's own id and ignores the
        draft's, so the edited rule stays attached to the built-in it was
        already attached to.

        The edited block claims ``escape-key`` (the FIRST built-in) while
        the draft carries ``undo-last`` (the LAST). A simulation that
        preferred the draft's id would put the rule after the
        ``middle-thing`` claimant, which answers the same phrase.
        """
        bench = _Bench(
            tmp_path,
            _user_block("^zoom(?: in)?$", doc_id="escape-key", output="only")
            + _user_block("^zoom$", doc_id="middle-thing", output="middle"),
        )
        edited = PatternManager.pattern_id("^zoom(?: in)?$")
        draft = _draft("^zoom(?: in)?$", "draft", edited, doc_id="undo-last")

        preview = bench.preview(draft, "zoom")
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^zoom(?: in)?$", "draft"))
        assert _typed(bench.responder("zoom")) == ["draft"], (
            "the saved rule keeps the escape-key slot, which is ahead of "
            "the middle-thing claimant"
        )
        assert preview["winner"] == "draft", preview

    def test_a_builtin_sharing_the_expression_is_not_the_draft(
        self, tmp_path,
    ):
        """The draft's row is a USER row.

        The edited rule claims ``undo-last``, the LAST built-in, and saves
        under the FIRST built-in's own expression. A simulation that found
        its row by expression alone would pick the built-in at slot 0 and
        report the draft answering a phrase the built-in answers.
        """
        bench = _Bench(
            tmp_path,
            _user_block("^grow$", doc_id="undo-last", output="only")
            + 'origin = "user"\n',
        )
        edited = PatternManager.pattern_id("^grow$")
        draft = _draft("^escape$", "draft", edited, doc_id="undo-last")

        preview = bench.preview(draft, "escape")
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^escape$", "draft"))
        assert _typed(bench.responder("escape")) == [], (
            "the shipped escape built-in still answers first; it is a "
            "hotkey, so it types nothing"
        )
        assert preview["winner"] == "existing", preview

    def test_the_preview_leaves_the_catalog_alone(self, tmp_path):
        """Asking is not saving.

        The simulation merges and builds a whole pattern list. None of it
        may reach the catalog: the user has not pressed save, and the
        Logic process goes on matching speech against this catalog while
        the dialog is open.
        """
        bench = _Bench(
            tmp_path,
            _user_block("^grow$", doc_id="escape-key", output="only"),
        )
        before = bench.catalog.get_all_patterns()
        before_order = [entry["raw_pattern"] for entry in before]
        edited = PatternManager.pattern_id("^grow$")

        bench.preview(_draft("^zoom$", "draft", edited), "zoom")

        after = bench.catalog.get_all_patterns()
        assert after is before, "the catalog swapped in a new list"
        assert [entry["raw_pattern"] for entry in after] == before_order
        assert _typed(bench.responder("grow")) == ["only"]
class TestTheDuplicateTriggerCheckReadsTheFile:
    """The refusal check must see the blocks the BUILD threw away.

    ``update_pattern`` refuses a save whose trigger key collides with a
    different user block, and it finds that block by reading the user file
    (``PatternManager._find_user_trigger_collision``, which walks
    ``_load_pattern_dicts(self.user_patterns_file)`` or the entries the
    writer is about to save). ``_load_pattern_dicts`` keeps every table
    whose ``pattern`` value is a string, actions or no actions.

    The BUILD is stricter. ``PatternCatalog._build_structures`` keeps an
    entry only ``if pattern_str and actions_list``, so a user block with an
    empty action list is absent from the built list entirely.

    A preview that ran its collision check over the built list therefore
    could not see such a block, reported no refusal, and went on to
    simulate a save the save itself rejects
    (wh-pattern-override-doc-id.3.5, boss ruling 2026-09-06).
    """

    ACTIONLESS = 'actions = []\n'

    def test_a_block_the_build_dropped_still_refuses_the_save(
        self, tmp_path,
    ):
        """The save refuses. This pins the behaviour the preview must copy."""
        bench = _Bench(
            tmp_path,
            _user_block("^grow$", actions=self.ACTIONLESS)
            + _user_block("^zoom$", output="edit me"),
        )
        assert all(
            entry["raw_pattern"] != "^grow$"
            for entry in bench.catalog.get_all_patterns()
        ), "the build must drop the actionless block for this test to mean anything"

        edited = PatternManager.pattern_id("^zoom$")
        result = bench.manager.update_pattern(
            edited, _save_data("^grow$", "draft"),
        )

        assert result["success"] is False, result
        assert result["error"] == (
            PatternManager._duplicate_trigger_message("^grow$")
        )

    def test_the_preview_reports_the_refusal_in_the_saves_words(
        self, tmp_path,
    ):
        """The preview must answer what the save answers."""
        bench = _Bench(
            tmp_path,
            _user_block("^grow$", actions=self.ACTIONLESS)
            + _user_block("^zoom$", output="edit me"),
        )
        edited = PatternManager.pattern_id("^zoom$")

        preview = bench.preview(_draft("^grow$", "draft", edited), "grow")

        assert preview["draft_error"] == (
            PatternManager._duplicate_trigger_message("^grow$")
        ), preview
        assert preview["winner"] == "none", preview

    def test_the_edited_blocks_own_trigger_is_not_a_collision(
        self, tmp_path,
    ):
        """Keeping your own trigger is an edit, not a duplicate.

        ``exclude_pattern_id`` is what tells the two apart, and the check
        has to apply it to the raw list the same way the save does.
        """
        bench = _Bench(
            tmp_path,
            _user_block("^zoom$", doc_id="escape-key", output="edit me"),
        )
        edited = PatternManager.pattern_id("^zoom$")

        preview = bench.preview(
            _draft("^zoom$", "draft", edited, doc_id="escape-key"), "zoom",
        )

        assert preview["draft_error"] is None, preview
        assert preview["winner"] == "draft", preview

    def test_a_builtin_sharing_the_trigger_is_not_a_collision(
        self, tmp_path,
    ):
        """Overriding a built-in is the Customize flow working as designed.

        The raw USER list is the right list to read for a second reason:
        the shipped blocks must stay out of the check. ``^escape$`` is a
        built-in here and the draft takes it deliberately.
        """
        bench = _Bench(
            tmp_path,
            _user_block("^zoom$", output="edit me"),
        )
        edited = PatternManager.pattern_id("^zoom$")

        preview = bench.preview(_draft("^escape$", "draft", edited), "escape")

        assert preview["draft_error"] is None, preview


class TestTheOwnRuleMarkReachesThePreview:
    """Case D. The preview models the ``origin`` key the save writes.

    ``create_pattern`` marks every block it writes as the person's own
    rule, and ``update_pattern`` preserves whatever the block on disk
    carried. A preview that modelled either differently would show a draft
    taking over a built-in the save leaves alone, or the reverse
    (wh-pattern-override-doc-id.3.3).

    ``TestALegacyOverrideKeepsItsResolvedIdentity`` above does NOT cover
    the third state. Its edit rewrites the expression, so the edited block
    no longer carries the built-in's text and the legacy migration passes
    it over whether the key is there or not. The third test below keeps
    the expression, which is what makes the key decide.
    """

    def _agree(self, bench, preview, typed, phrase):
        """Compare the preview's answer with the save's, for one phrase."""
        after = bench.actual(phrase)
        assert after["success"], after
        won = preview["winner"] == "draft"
        match = after["match"]
        actually_won = (
            match is not None and _typed(match["resolved_steps"]) == [typed]
        )
        assert won == actually_won, (
            "the preview said the draft "
            f"{'wins' if won else 'loses'} but the save made it "
            f"{'win' if actually_won else 'lose'}"
        )
        return actually_won

    def test_a_new_rule_copying_a_builtin_does_not_take_it_over(
        self, tmp_path,
    ):
        """A create. The save marks the block, so the preview must too.

        Without the mark in the previewed block the simulation reads the
        draft as a rule saved before ids existed, resolves it onto the
        built-in whose expression it copied, and shows the person their
        new rule winning a phrase the built-in actually keeps.
        """
        bench = _Bench(tmp_path, "")
        draft = {
            "pattern_type": "command",
            "expression": "^escape$",
            "action_type": "text",
            "action_params": {"output": "mine"},
            "requires_hotword": False,
        }

        preview = bench.preview(draft, "escape")
        assert preview["success"], preview
        assert preview["draft_error"] is None, preview["draft_error"]

        created = bench.manager.create_pattern(
            pattern_type="command",
            expression="^escape$",
            action_type="text",
            action_params={"output": "mine"},
        )
        assert created["success"], created
        assert bench.catalog.reload()

        assert self._agree(bench, preview, "mine", "escape") is False
        assert bench.responder("escape") == [
            {"function": "hk", "params": ["escape"]}
        ]

    def test_an_edit_of_the_persons_own_rule_keeps_it_independent(
        self, tmp_path,
    ):
        """An edit of a marked block. The mark survives, so both agree.

        ``update_pattern`` carries the stored key forward, so this rule is
        still the person's own after the edit and the built-in still
        answers ``escape``. A preview that dropped the key would show the
        draft taking the built-in over.
        """
        user = (
            "[[pattern]]\n"
            'origin = "user"\n'
            "pattern = '''^escape$'''\n"
            'actions = [{ function = "text", params = ["mine"] }]\n'
            "\n"
        )
        bench = _Bench(tmp_path, user)
        edited = PatternManager.pattern_id("^escape$")
        draft = _draft("^escape(?: now)?$", "draft", edited)

        preview = bench.preview(draft, "escape")
        assert preview["success"], preview
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^escape(?: now)?$", "draft"))

        assert self._agree(bench, preview, "draft", "escape") is False
        assert bench.responder("escape") == [
            {"function": "hk", "params": ["escape"]}
        ]

    def test_an_edit_that_keeps_a_pre_doc_id_overrides_words(self, tmp_path):
        """The third state: the key must NOT appear on this edit.

        This rule was saved before ids existed and replaces its built-in by
        expression text alone. The edit changes only the action, so the
        text association still holds and the rule keeps the built-in's
        place. A preview that carried the draft's own mark into the edited
        block would read the rule as independent and show the built-in
        winning a phrase the person's rule actually keeps.
        """
        bench = _Bench(tmp_path, _user_block("^escape$", output="legacy"))
        edited = PatternManager.pattern_id("^escape$")
        draft = _draft("^escape$", "draft", edited)

        preview = bench.preview(draft, "escape")
        assert preview["success"], preview
        assert preview["draft_error"] is None, preview["draft_error"]

        bench.save(edited, _save_data("^escape$", "draft"))

        assert self._agree(bench, preview, "draft", "escape") is True
