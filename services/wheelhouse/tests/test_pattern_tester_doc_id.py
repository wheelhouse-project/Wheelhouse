# tests/test_pattern_tester_doc_id.py
"""The try-it preview simulates the merge the save will actually perform.

wh-pattern-override-doc-id A6. ``_simulate_merge`` held its own copy of the
normalized-text key, so once the runtime merge moved to ``doc_id`` the preview
answered a different question from the save it previews. That is worse than a
wrong preview in general: the whole point of the try-it line is that a user can
trust it before committing an edit. A Customize draft whose built-in has been
rewritten would be shown losing to the built-in and then, on save, win.

The rule now comes from ``speech.pattern_identity``, the same one the catalog
merge and the manager listing call.
"""

import re

import pytest

from speech import pattern_tester
from speech.pattern_manager import PatternManager
from speech.pattern_matcher import PatternMatcher
from speech.pattern_transform import transform_pattern
from doc_id_catalog_fixtures import runtime_entry, MAX_PATTERN, RESTORE_PATTERN


@pytest.fixture(autouse=True, scope="module")
def _close_the_bounded_regex_pool():
    """Leave no live match worker behind for the next test file.

    ``run_test_draft`` matches through ``speech.safe_regex.match_bounded``,
    which starts a spawn-context multiprocessing pool and keeps it alive
    for the rest of the process. A Qt test file that builds a dialog after
    that pool exists reads a corrupted screen object: measured in this
    selection, ``PatternManagerDialog.__init__`` got a plain ``QObject``
    back from ``self.screen()`` on one run and died with 0xC0000374 on
    another, while the same files without this one passed three of three.
    ``shutdown`` is the module's own hygiene entry point.
    """
    yield
    from speech import safe_regex

    safe_regex.shutdown()


_HK_SAVE = [{"function": "hk", "params": ["ctrl", "s"]}]


def _entry(regex, actions=None, is_user=False, doc_id=None):
    """Synthetic identity edge cases; shipped inputs use runtime_entry()."""
    transformed, meta = transform_pattern(regex)
    entry = {
        "compiled_pattern": re.compile(transformed, re.IGNORECASE),
        "pattern_type": "command" if regex.startswith("^") else "replacement",
        "actions": actions if actions is not None else list(_HK_SAVE),
        "requires_hotword": False,
        "validation_group": meta.get("validation_group"),
        "is_greedy": meta.get("is_greedy", False),
        "raw_pattern": regex,
        "is_user": is_user,
    }
    if doc_id is not None:
        entry["doc_id"] = doc_id
    return entry


def _matcher():
    return PatternMatcher(None)


def _draft(trigger, doc_id=None, keys=("ctrl", "alt", "m")):
    draft = {
        "pattern_type": "command",
        "action_type": "hotkey",
        "action_params": {"keys": list(keys)},
        "requires_hotword": False,
        "trigger": trigger,
    }
    if doc_id is not None:
        draft["doc_id"] = doc_id
    return draft


def _raw_order(simulated):
    return [entry.get("raw_pattern") for entry in simulated]


class TestTheDraftTakesTheBuiltinSlot:
    """A Customize draft replaces its built-in, wherever the built-in sits."""

    def test_a_draft_carrying_a_builtin_doc_id_replaces_that_builtin(self, tmp_path):
        """The bead's defect on the preview side.

        The built-in's expression was rewritten by a release; the draft the
        user is editing still carries the built-in's doc_id. Keyed on text
        the two no longer matched, so the preview appended the draft after
        everything and showed the built-in winning -- while the save would
        have replaced it.
        """
        patterns = [
            runtime_entry("maximize-window", tmp_path, rewrite=True),
            runtime_entry("restore-window", tmp_path),
        ]
        draft_entry, error = pattern_tester._build_draft_entry(
            _draft("maximize", doc_id="maximize-window"),
        )
        assert error is None
        assert draft_entry is not None
        simulated = pattern_tester._simulate_merge(patterns, draft_entry, None)
        assert _raw_order(simulated) == ["^maximize$", RESTORE_PATTERN], (
            "the draft must take the built-in's own slot, not the end of "
            f"the list; the simulated order is {_raw_order(simulated)}"
        )

    def test_the_draft_wins_the_try_it_line_after_the_rewrite(self, tmp_path):
        """The user-visible half of the same defect."""
        patterns = [runtime_entry("maximize-window", tmp_path, rewrite=True)]
        result = pattern_tester.run_test_draft(
            _draft("maximize", doc_id="maximize-window"),
            "maximize", patterns, _matcher(),
        catalog=None,
        )
        assert result["draft_error"] is None
        assert result["winner"] == "draft"


class TestWhatMustNotChange:
    def test_a_draft_without_a_doc_id_still_replaces_by_text(self):
        """Every edit of a pre-doc_id user pattern behaves as it always did."""
        patterns = [_entry("^save$", is_user=True), _entry("^quit$")]
        draft_entry, error = pattern_tester._build_draft_entry(_draft("save"))
        assert error is None
        assert draft_entry is not None
        simulated = pattern_tester._simulate_merge(patterns, draft_entry, None)
        assert _raw_order(simulated) == ["^save$", "^quit$"]

    def test_a_draft_with_a_new_doc_id_appends(self, tmp_path):
        patterns = [runtime_entry("maximize-window", tmp_path)]
        draft_entry, error = pattern_tester._build_draft_entry(
            _draft("launch", doc_id="my-own-command"),
        )
        assert error is None
        assert draft_entry is not None
        simulated = pattern_tester._simulate_merge(patterns, draft_entry, None)
        assert _raw_order(simulated) == [MAX_PATTERN, "^launch$"]

    def test_an_excluded_entry_still_yields_its_slot_to_the_draft(self):
        """An unchanged-trigger edit keeps the block's position.

        The excluded stale entry is removed and the draft is placed where it
        stood, so a rewritten user block does not jump to the end of the
        merged order.
        """
        patterns = [
            _entry("^first$"),
            _entry("^old$", is_user=True, doc_id="my-own-command"),
            _entry("^last$"),
        ]
        stale_id = PatternManager.pattern_id("^old$")
        draft_entry, error = pattern_tester._build_draft_entry(
            _draft("new", doc_id="my-own-command"),
        )
        assert error is None
        assert draft_entry is not None
        simulated = pattern_tester._simulate_merge(
            patterns, draft_entry, stale_id,
        )
        assert _raw_order(simulated) == ["^first$", "^new$", "^last$"]

    def test_a_doc_id_does_not_capture_a_pattern_of_the_same_text(self):
        """The two kinds of identity stay separate namespaces here too."""
        patterns = [_entry("maximize-window", is_user=True)]
        draft_entry, error = pattern_tester._build_draft_entry(
            _draft("launch", doc_id="maximize-window"),
        )
        assert error is None
        assert draft_entry is not None
        simulated = pattern_tester._simulate_merge(patterns, draft_entry, None)
        assert _raw_order(simulated) == ["maximize-window", "^launch$"]

    def test_a_malformed_draft_doc_id_is_not_carried_into_the_entry(self):
        """A malformed id merges on text, so it must not reach the entry.

        Carrying it would make the simulated entry claim an identity the
        merge does not honor, and the preview would place it somewhere the
        save will not.
        """
        draft_entry, error = pattern_tester._build_draft_entry(
            _draft("launch", doc_id="Window Maximize"),
        )
        assert error is None
        assert draft_entry is not None
        assert "doc_id" not in draft_entry

    def test_an_edit_draft_that_names_its_rule_reports_the_real_winner(self, tmp_path):
        """wh-pattern-override-doc-id.2.2, the answer the user reads.

        The editor's own half of this is in
        test_pattern_customize_vs_duplicate.py; this is what the try-it
        line says once the draft carries the id. The rule being edited was
        customized from ``maximize-window`` and its new phrases generate
        ``^(?:escape|dismiss)$``, the expression the shipped ``escape``
        entry carries. Without the id the draft takes the ``escape`` slot
        by the legacy text rule -- which answers with that built-in's OWN
        doc identity -- and the preview says the draft wins. The save keeps
        the ``maximize-window`` id, so the shipped entry stays first and
        answers, and the edited rule never runs.

        This passes before AND after the editor fix -- the simulation was
        already right, and the editor was the side not sending the id. It
        is here to hold the two answers apart, so a later change cannot
        make the id-less draft mean the same thing as the named one.
        """
        patterns = [
            runtime_entry("escape", tmp_path),
            _entry("^(?:maximize)$", is_user=True, doc_id="maximize-window"),
        ]
        draft = {
            "pattern_type": "command",
            "action_type": "hotkey",
            "action_params": {"keys": ["ctrl", "alt", "m"]},
            "requires_hotword": False,
            "phrases": ["escape", "dismiss"],
            "exclude_pattern_id": PatternManager.pattern_id("^(?:maximize)$"),
            "doc_id": "maximize-window",
        }
        result = pattern_tester.run_test_draft(
            draft, "escape", patterns, _matcher(),
        catalog=None,
        )
        assert result["draft_error"] is None
        assert result["winner"] == "existing", (
            "the shipped entry keeps its slot and answers first, so the "
            "preview must not promise the draft"
        )

        without_id = dict(draft)
        del without_id["doc_id"]
        assert pattern_tester.run_test_draft(
            without_id, "escape", patterns, _matcher(),
        catalog=None,
        )["winner"] == "draft", (
            "the same draft without the id is the state the editor used to "
            "send, and it is the wrong answer -- kept here so the two "
            "cannot silently converge on it"
        )

    def test_a_duplicate_user_text_is_still_reported_as_a_collision(self):
        """The save rejects two user blocks resolving to the same expression.

        That check is about the EXPRESSION, not about which built-in a rule
        overrides, so it stays on the text key even though the merge no
        longer does. The preview must keep reporting it in the save's words.
        """
        patterns = [_entry("^save$", is_user=True)]
        result = pattern_tester.run_test_draft(
            _draft("save", doc_id="my-own-command"),
            "save", patterns, _matcher(),
        catalog=None,
        )
        assert result["draft_error"] is not None
        assert "Duplicate" in result["draft_error"]
