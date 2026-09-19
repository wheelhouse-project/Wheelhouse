"""Customize and Duplicate stop being the same button.

wh-pattern-override-doc-id A2. Both handlers called ``_open_editor(entry=pat)``
with nothing to tell them apart, so whatever one did the other did too. Now
that a saved rule can carry the built-in's durable name, that sameness is a
defect with two bad halves and no good one: give both the name and a duplicate
claims the built-in its original already claims, with the file order deciding
which of the two wins; give neither the name and Customize writes a rule
identified by its expression alone, which a release rewriting that expression
orphans -- the bug this bead exists to fix.

The seam is one explicit flag on ``_open_editor``. The two buttons are
pinned to opposite sides of it where the rest of that constructor's
arguments are already pinned -- TestOpenEditorWiring in
test_pattern_manager_dialog.py, which patches CreatePatternDialog and
reads what actually reaches it. This file follows the id the rest of the
way: out of the editor into its save payload and its try-it draft, and
through the Logic handler into the user file.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
pytestmark = pytest.mark.usefixtures("qapp")


BUILTIN = {
    "id": "sysid",
    "trigger_display": "maximize",
    "requires_hotword": False,
    "is_user_created": False,
    "overrides_builtin": False,
    "raw_pattern": "^maximize$",
    "raw_actions": [{"function": "hk", "params": ["win", "up"]}],
    "doc_id": "window-maximize",
}


class TestTheEditorCarriesTheIdOnlyWhenAsked:
    def _dialog(self, keep_identity):
        from create_pattern_dialog import CreatePatternDialog
        return CreatePatternDialog(
            "x-ray", parent=None, entry=dict(BUILTIN),
            keep_identity=keep_identity,
        )

    def test_a_customize_save_sends_the_doc_id(self):
        dialog = self._dialog(True)
        assert dialog.get_pattern_data()["doc_id"] == "window-maximize"

    def test_a_duplicate_save_sends_no_doc_id(self):
        dialog = self._dialog(False)
        assert "doc_id" not in dialog.get_pattern_data()

    def test_the_try_it_draft_carries_it_too(self):
        """The preview must simulate the merge the save will perform.

        Without the id in the draft the preview keys on the expression, so
        a Customize of a rewritten built-in would be shown losing and then
        win on save. The draft is get_pattern_data()'s own output, so this
        is one assertion on the wire rather than on the builder again.
        """
        dialog = self._dialog(True)
        sent = []
        dialog.pattern_action.connect(sent.append)
        dialog._try_input.setText("maximize")
        dialog._send_test_draft()
        assert sent and sent[0]["action"] == "pm_test_draft"
        assert sent[0]["data"]["draft"]["doc_id"] == "window-maximize"

    def test_a_malformed_stored_id_is_not_carried(self):
        """A hand-edited id the merge ignores must not reach a save."""
        from create_pattern_dialog import CreatePatternDialog
        dialog = CreatePatternDialog(
            "x-ray", parent=None,
            entry=dict(BUILTIN, doc_id="Window Maximize"),
            keep_identity=True,
        )
        assert "doc_id" not in dialog.get_pattern_data()

    def test_an_advanced_save_carries_it_as_well(self):
        """Advanced mode builds its payload separately and must not drop it."""
        dialog = self._dialog(True)
        dialog._mode = "advanced"
        assert dialog.get_pattern_data()["doc_id"] == "window-maximize"


def _runtime_entry(regex, is_user=False, doc_id=None):
    """One entry shaped like the live merged list (TextParser.patterns)."""
    import re

    from speech.pattern_transform import transform_pattern

    transformed, meta = transform_pattern(regex)
    entry = {
        "compiled_pattern": re.compile(transformed, re.IGNORECASE),
        "pattern_type": "command",
        "actions": [{"function": "hk", "params": ["win", "up"]}],
        "requires_hotword": False,
        "validation_group": meta.get("validation_group"),
        "is_greedy": meta.get("is_greedy", False),
        "raw_pattern": regex,
        "is_user": is_user,
    }
    if doc_id is not None:
        entry["doc_id"] = doc_id
    return entry


_OWN = "^(?:maximize)$"
_OTHER = "^(?:escape|dismiss)$"


class TestAnEditPreviewAgreesWithTheSaveItPreviews:
    """wh-pattern-override-doc-id.2.2.

    An edit in place keeps the rule's doc_id, but it takes it from the block
    on disk: ``update_pattern`` reads it there and writes it back, so an edit
    cannot move a rule onto a different built-in (A2). The editor still
    dropped the id from its try-it draft, so the preview placed the draft by
    its expression while the save places it by the id. The two then answer
    different questions, which is the one thing the try-it line exists not to
    do.

    The reachable sequence, in the shipped file as it stands. Customize a
    built-in, then Edit the copy and give it the phrases "escape" and
    "dismiss". ``generate_expression`` joins a phrase list into
    ``^(?:escape|dismiss)$``, which is byte for byte the expression the
    shipped ``escape`` entry carries, so the draft's text key is that
    built-in's text key. Keyed on text -- through the legacy branch, which
    answers with the built-in's OWN doc identity -- the draft takes the
    ``escape`` slot and that built-in vanishes from the simulation, so the
    preview reports the edited rule responding. The save keeps the id of the
    built-in the rule was customized from, so the shipped ``escape`` entry
    stays where it is and the edited rule goes back to its own slot. Both
    then answer "escape", the earlier one wins, and the rule the preview
    promised never runs.

    The fixture below is that shape with a hotkey action, so the editor
    opens in the simple pane the phrases are typed into.

    These tests stop at ``_simulate_merge`` and never call
    ``run_test_draft``. Answering a draft matches through
    ``speech.safe_regex.match_bounded``, which starts a multiprocessing
    pool, and a pool started in a process that already holds a
    QApplication corrupts the heap in the next Qt dialog this suite builds
    (measured here: PatternManagerDialog dies with 0xC0000374 on two runs
    of two, and the same selection passes with this class deselected).
    The answer-level half of this finding lives in
    test_pattern_tester_doc_id.py, which holds no QApplication.
    """

    def _edit_dialog(self, phrases=("escape", "dismiss")):
        from create_pattern_dialog import CreatePatternDialog
        from speech.pattern_manager import PatternManager

        dialog = CreatePatternDialog(
            "x-ray", parent=None,
            entry=dict(
                BUILTIN, raw_pattern=_OWN, phrases=["maximize"],
                is_user_created=True, overrides_builtin=True,
            ),
            pattern_id=PatternManager.pattern_id(_OWN),
        )
        dialog._phrase_editor.set_phrases(list(phrases))
        return dialog

    def _draft(self, dialog, text="escape"):
        sent = []
        dialog.pattern_action.connect(sent.append)
        dialog._try_input.setText(text)
        dialog._send_test_draft()
        assert sent and sent[0]["action"] == "pm_test_draft"
        return sent[0]["data"]["draft"]

    def _runtime(self):
        """The other built-in first, then the rule being edited, sitting in
        the slot of the built-in it was customized from."""
        return [
            _runtime_entry(_OTHER, doc_id="escape"),
            _runtime_entry(_OWN, is_user=True, doc_id="window-maximize"),
        ]

    def test_the_edit_draft_lands_where_the_save_will_put_it(self):
        from speech.pattern_manager import PatternManager
        from speech.pattern_tester import _build_draft_entry, _simulate_merge

        draft_entry, error = _build_draft_entry(
            self._draft(self._edit_dialog()),
        )
        assert error is None
        simulated = _simulate_merge(
            self._runtime(), draft_entry, PatternManager.pattern_id(_OWN),
        )
        assert [e["raw_pattern"] for e in simulated] == [_OTHER, _OTHER], (
            "the shipped entry keeps its slot and the draft takes the slot "
            "its own doc_id names"
        )
        assert simulated[1] is draft_entry

    def test_the_edit_payload_carries_the_rules_own_doc_id(self):
        assert (
            self._edit_dialog().get_pattern_data()["doc_id"]
            == "window-maximize"
        )

    def test_an_advanced_edit_carries_it_as_well(self):
        """Advanced mode builds its payload separately and must not drop it."""
        dialog = self._edit_dialog()
        dialog._mode = "advanced"
        assert dialog.get_pattern_data()["doc_id"] == "window-maximize"

    def test_an_edit_of_a_rule_that_has_no_id_still_carries_none(self):
        """A pre-doc_id override edited in place keeps identifying by text.

        Passes before AND after: the bound on the other side of the fix.
        """
        from create_pattern_dialog import CreatePatternDialog
        from speech.pattern_manager import PatternManager

        entry = dict(BUILTIN, is_user_created=True)
        del entry["doc_id"]
        dialog = CreatePatternDialog(
            "x-ray", parent=None, entry=entry,
            pattern_id=PatternManager.pattern_id("^maximize$"),
        )
        assert "doc_id" not in dialog.get_pattern_data()

    def test_a_malformed_stored_id_is_still_not_carried_by_an_edit(self):
        """Passes before AND after: a hand-edited id the merge ignores must
        not reach the preview either, or the preview would key on an
        identity nothing merges on."""
        from create_pattern_dialog import CreatePatternDialog
        from speech.pattern_manager import PatternManager

        dialog = CreatePatternDialog(
            "x-ray", parent=None,
            entry=dict(BUILTIN, is_user_created=True, doc_id="Window Maximize"),
            pattern_id=PatternManager.pattern_id("^maximize$"),
        )
        assert "doc_id" not in dialog.get_pattern_data()

    def test_a_duplicate_still_carries_no_id(self):
        """Passes before AND after: the fix must widen the carry to edit,
        not to every dialog opened from an entry. Duplicate creates a NEW
        rule and must not claim the built-in the original already claims."""
        from create_pattern_dialog import CreatePatternDialog

        dialog = CreatePatternDialog(
            "x-ray", parent=None, entry=dict(BUILTIN),
        )
        assert "doc_id" not in dialog.get_pattern_data()


class TestTheLogicHandlerPassesItThrough:
    """pm_create_pattern is the wire between the editor and the file.

    A forwarding gap here is invisible in every other test, because the
    editor and the pattern manager each work correctly in isolation. The
    round trip below is the only place the whole path is exercised.
    """

    async def test_a_customize_message_writes_the_doc_id_to_the_user_file(
        self, tmp_path,
    ):
        controller, handler, user_file = _controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "alt", "m"]},
                "phrases": ["maximize"],
                "doc_id": "window-maximize",
            }},
        )
        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert results and results[0]["data"]["success"] is True
        assert _blocks(user_file)[0]["doc_id"] == "window-maximize"

    async def test_a_duplicate_message_writes_no_doc_id(self, tmp_path):
        controller, handler, user_file = _controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "alt", "m"]},
                "phrases": ["maximize"],
            }},
        )
        assert "doc_id" not in _blocks(user_file)[0]

    async def test_a_malformed_doc_id_is_refused_with_a_readable_error(
        self, tmp_path,
    ):
        """The refusal reaches the editor rather than corrupting the file."""
        controller, handler, user_file = _controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_create_pattern",
            {"data": {
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "alt", "m"]},
                "phrases": ["maximize"],
                "doc_id": "Window Maximize",
            }},
        )
        results = [
            m for m in controller.state_manager.state_to_gui_queue.items
            if m.get("action") == "pm_create_result"
        ]
        assert results and results[0]["data"]["success"] is False
        assert "doc_id" in results[0]["data"]["error"]


# ---------------------------------------------------------------------------
# Logic-side harness (style of test_pattern_manager_update.py)
# ---------------------------------------------------------------------------

_SYSTEM = (
    'COMMAND_HOTWORD = "x-ray"\n\n'
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    'pattern = "^maximize$"\n'
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)


class _FakeTextParser:
    def __init__(self):
        self.patterns = []


class _FakeSpeechHandler:
    def __init__(self, patterns_file, user_patterns_file):
        from speech.pattern_catalog import PatternCatalog

        self.patterns_file = patterns_file
        self.user_patterns_file = user_patterns_file
        self.pattern_catalog = PatternCatalog(patterns_file, user_patterns_file)
        self.text_parser = _FakeTextParser()
        self.applied_hotwords = []

    def apply_hotword(self, hotword):
        self.applied_hotwords.append(hotword)


class _CapturingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _controller(tmp_path):
    from main import LogicController

    system_file = tmp_path / "patterns.toml"
    system_file.write_text(_SYSTEM, encoding="utf-8")
    user_file = tmp_path / "user_patterns.toml"

    controller = MagicMock(spec=LogicController)
    controller._handle_pattern_manager_action = (
        LogicController._handle_pattern_manager_action.__get__(controller)
    )
    handler = _FakeSpeechHandler(str(system_file), str(user_file))
    controller.service_manager = MagicMock()
    controller.service_manager.speech_handler = handler
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _CapturingQueue()
    return controller, handler, user_file


def _blocks(user_file):
    import tomllib

    with open(user_file, "rb") as fh:
        return tomllib.load(fh).get("pattern", [])
