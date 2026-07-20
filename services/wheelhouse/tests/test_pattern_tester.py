# tests/test_pattern_tester.py
"""Tests for the Pattern Manager try-it evaluator (wh-pattern-editor-test-messages).

speech/pattern_tester.py answers the two editor try-it questions from the
SAME in-memory objects the runtime matches with (TextParser.patterns +
PatternMatcher), so the answer cannot drift from runtime behavior:

* run_test_phrase(text, patterns, matcher): which pattern responds to this
  text right now -- first match wins in merged-catalog order, wake word
  assumed spoken (the requires_hotword flag is reported instead of a
  silent no-match).
* run_test_draft(draft, text, patterns, matcher): build a create-shaped
  draft via the same paths create_pattern uses (a bad draft is a
  draft_error string, NOT a handler failure), simulate the catalog merge
  (same trigger key replaces in place, new trigger appends after
  everything), honor exclude_pattern_id so an edited pattern does not
  shadow itself, and answer which pattern responds first.

The module tests run against small in-memory pattern lists shaped like
PatternCatalog.get_all_patterns() entries; the handler tests round-trip the
pm_test_phrase / pm_test_draft messages through
main.LogicController._handle_pattern_manager_action in the style of
test_pattern_manager_update.py.

Spec: docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 7.
"""
import re
from unittest.mock import MagicMock

from speech import pattern_tester
from speech.pattern_manager import PatternManager
from speech.pattern_matcher import PatternMatcher
from speech.pattern_transform import transform_pattern


# ---------------------------------------------------------------------------
# In-memory catalog helpers
# ---------------------------------------------------------------------------

_HK_SAVE = [{"function": "hk", "params": ["ctrl", "s"]}]


def _entry(regex, actions=None, requires_hotword=False, is_user=False,
           with_identity=True):
    """Build one pattern entry shaped like PatternCatalog.get_all_patterns().

    Mirrors _build_structures: the compiled pattern is the TRANSFORMED
    string (numeric (\\d+) groups become (\\w+) with validation metadata),
    compiled with IGNORECASE, and the type is auto-detected from the ^
    anchor. ``raw_pattern``/``is_user`` are the identity keys the tester
    prefers; ``with_identity=False`` builds a today-shaped catalog entry
    without them to pin the fallback behavior.
    """
    transformed, meta = transform_pattern(regex)
    entry = {
        "compiled_pattern": re.compile(transformed, re.IGNORECASE),
        "pattern_type": "command" if regex.startswith("^") else "replacement",
        "actions": actions if actions is not None else list(_HK_SAVE),
        "requires_hotword": requires_hotword,
        "validation_group": meta.get("validation_group"),
        "is_greedy": meta.get("is_greedy", False),
    }
    if with_identity:
        entry["raw_pattern"] = regex
        entry["is_user"] = is_user
    return entry


def _matcher():
    # match_single_pattern and validate_numeric never touch the catalog
    # reference, so the module-level tests need no PatternCatalog at all.
    return PatternMatcher(None)


def _draft(trigger=None, phrases=None, action_type="hotkey",
           action_params=None, pattern_type="command",
           requires_hotword=False, exclude_pattern_id=None):
    """Build the create-shaped draft dict pm_test_draft consumes."""
    draft = {
        "pattern_type": pattern_type,
        "action_type": action_type,
        "action_params": (
            action_params if action_params is not None
            else {"keys": ["ctrl", "d"]}
        ),
        "requires_hotword": requires_hotword,
    }
    if trigger is not None:
        draft["trigger"] = trigger
    if phrases is not None:
        draft["phrases"] = phrases
    if exclude_pattern_id is not None:
        draft["exclude_pattern_id"] = exclude_pattern_id
    return draft


# ---------------------------------------------------------------------------
# run_test_phrase
# ---------------------------------------------------------------------------

class TestRunTestPhrase:

    def test_match_reports_identity_flags_and_steps(self):
        patterns = [
            _entry("^save$", requires_hotword=True),
            _entry("^quit$"),
        ]
        result = pattern_tester.run_test_phrase("save", patterns, _matcher())
        assert result["success"] is True
        match = result["match"]
        assert match is not None
        assert match["pattern_id"] == PatternManager.pattern_id("^save$")
        assert match["trigger_display"] == "save"
        assert match["requires_hotword"] is True
        assert match["is_user_created"] is False
        assert match["groups"] == []
        assert match["resolved_steps"] == [
            {"function": "hk", "params": ["ctrl", "s"]},
        ]

    def test_no_match_returns_null_match(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_phrase(
            "flibbertigibbet", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["match"] is None

    def test_first_match_wins_in_catalog_order(self):
        patterns = [
            _entry("^save$"),
            _entry("^(?:save|store)$"),
        ]
        result = pattern_tester.run_test_phrase("save", patterns, _matcher())
        assert result["match"]["pattern_id"] == (
            PatternManager.pattern_id("^save$")
        )

    def test_user_created_flag_reported(self):
        patterns = [_entry("^deploy$", is_user=True)]
        result = pattern_tester.run_test_phrase("deploy", patterns, _matcher())
        assert result["match"]["is_user_created"] is True

    def test_group_capture_and_substitution_in_resolved_steps(self):
        patterns = [
            _entry(
                "^open (.+)$",
                actions=[
                    {"function": "activate", "params": ["g1"]},
                    {"function": "text", "params": ["opening (g1) now"]},
                ],
            ),
        ]
        result = pattern_tester.run_test_phrase(
            "open the pod bay doors", patterns, _matcher(),
        )
        match = result["match"]
        assert match["groups"] == ["the pod bay doors"]
        assert match["resolved_steps"] == [
            {"function": "activate", "params": ["the pod bay doors"]},
            {"function": "text", "params": ["opening (the pod bay doors) now"]},
        ]

    def test_unfilled_optional_group_resolves_to_none(self):
        # Mirrors _execute_rule: g1..g9 are pre-seeded to None, so a whole
        # "gN" param whose group did not capture resolves to None -- exactly
        # what the runtime would pass to the action function.
        patterns = [
            _entry(
                r"^undo\s*(\d+)?$",
                actions=[{"function": "hk", "params": ["ctrl", "z", "g1"]}],
            ),
        ]
        result = pattern_tester.run_test_phrase("undo", patterns, _matcher())
        match = result["match"]
        assert match["groups"] == [None]
        assert match["resolved_steps"] == [
            {"function": "hk", "params": ["ctrl", "z", None]},
        ]

    def test_numeric_validation_failure_skips_pattern(self):
        # The catalog compiles (\d+) as (\w+) plus validation metadata; at
        # the router, a match whose numeric group fails words_to_int is
        # skipped. "delete xyz" therefore reports no match, "delete 3"
        # matches with the captured count.
        patterns = [_entry(r"^delete\s*(\d+)?$")]
        no = pattern_tester.run_test_phrase("delete xyz", patterns, _matcher())
        assert no["match"] is None
        yes = pattern_tester.run_test_phrase("delete 3", patterns, _matcher())
        assert yes["match"] is not None
        assert yes["match"]["groups"] == ["3"]
        assert yes["match"]["pattern_id"] == (
            PatternManager.pattern_id(r"^delete\s*(\d+)?$")
        )

    def test_replacement_pattern_matches_anywhere(self):
        patterns = [
            _entry(
                r"\bperiod\b",
                actions=[{"function": "text", "params": ["."]}],
            ),
        ]
        result = pattern_tester.run_test_phrase(
            "hello period", patterns, _matcher(),
        )
        assert result["match"] is not None

    def test_identity_fallback_without_raw_pattern_keys(self):
        # Today's PatternCatalog entries carry neither raw_pattern nor
        # is_user; the tester falls back to hashing the compiled pattern
        # string (identical to the raw string for any pattern without a
        # numeric transform) and to is_user_created=False.
        patterns = [_entry("^save$", with_identity=False)]
        result = pattern_tester.run_test_phrase("save", patterns, _matcher())
        match = result["match"]
        assert match["pattern_id"] == PatternManager.pattern_id("^save$")
        assert match["is_user_created"] is False


# ---------------------------------------------------------------------------
# run_test_draft
# ---------------------------------------------------------------------------

class TestRunTestDraft:

    def test_draft_wins_on_new_trigger(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy"), "deploy", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] is None
        assert result["draft_matches"] is True
        assert result["winner"] == "draft"
        assert result["shadowed_by"] is None
        assert result["groups"] == []
        assert result["resolved_steps"] == [
            {"function": "hk", "params": ["ctrl", "d"]},
        ]

    def test_existing_earlier_pattern_wins(self):
        # The draft's phrase expression ^(?:save)$ is a NEW trigger key
        # (keys compare raw strings), so it appends after everything and
        # the shipped ^save$ responds first.
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(phrases=["save"]), "save", patterns, _matcher(),
        )
        assert result["winner"] == "existing"
        assert result["shadowed_by"] == {
            "pattern_id": PatternManager.pattern_id("^save$"),
            "trigger_display": "save",
            "is_user_created": False,
        }
        # The draft WOULD match the text -- it just loses on order.
        assert result["draft_matches"] is True
        # Groups/steps are only reported when the draft wins.
        assert result["groups"] == []
        assert result["resolved_steps"] == []

    def test_same_trigger_draft_replaces_builtin_in_place_and_wins(self):
        # generate_regex("save") == ^save$ == the built-in's key, so the
        # draft REPLACES the built-in at its position. If the simulation
        # appended instead, the second pattern would respond first and the
        # winner would wrongly be "existing".
        patterns = [
            _entry("^save$"),
            _entry("^(?:save|store)$"),
        ]
        result = pattern_tester.run_test_draft(
            _draft(trigger="save"), "save", patterns, _matcher(),
        )
        assert result["winner"] == "draft"
        assert result["shadowed_by"] is None

    def test_draft_loses_to_earlier_user_pattern(self):
        patterns = [
            _entry("^save$"),
            _entry("^(?:ship|deploy)$", is_user=True),
        ]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy"), "deploy", patterns, _matcher(),
        )
        assert result["winner"] == "existing"
        # trigger_display comes from the same PatternManager helper the
        # manager tree uses; an alternation-only expression falls back to
        # quoting the raw pattern, so the two views always agree.
        assert result["shadowed_by"] == {
            "pattern_id": PatternManager.pattern_id("^(?:ship|deploy)$"),
            "trigger_display": PatternManager._trigger_display(
                "^(?:ship|deploy)$",
            ),
            "is_user_created": True,
        }
        assert result["draft_matches"] is True

    def test_empty_trigger_is_draft_error_not_failure(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(trigger="   "), "save", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] == "Trigger phrase cannot be empty"
        assert result["winner"] == "none"
        assert result["draft_matches"] is False

    def test_unknown_action_type_is_draft_error(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(trigger="warp", action_type="teleport", action_params={}),
            "warp", patterns, _matcher(),
        )
        assert result["success"] is True
        assert "Unknown action type" in result["draft_error"]
        assert result["winner"] == "none"

    def test_invalid_phrase_list_is_draft_error(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(phrases=["editor", "Editor"]), "editor",
            patterns, _matcher(),
        )
        assert result["success"] is True
        assert "Duplicate phrase" in result["draft_error"]

    def test_phrases_based_draft_matches_any_alternative(self):
        patterns = [_entry("^save$")]
        draft = _draft(
            phrases=["editor", "code editor"],
            action_type="activate",
            action_params={"target": "notepad"},
        )
        result = pattern_tester.run_test_draft(
            draft, "code editor", patterns, _matcher(),
        )
        assert result["winner"] == "draft"
        assert result["resolved_steps"] == [
            {"function": "activate", "params": ["notepad"]},
        ]
        # Command phrases are anchored: extra words mean no response.
        partial = pattern_tester.run_test_draft(
            draft, "code editor please", patterns, _matcher(),
        )
        assert partial["winner"] == "none"
        assert partial["draft_matches"] is False

    def test_hotword_flag_passthrough(self):
        # The tester assumes the wake word was spoken, so a
        # requires_hotword draft still responds (the dialog renders the
        # flag separately).
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy", requires_hotword=True),
            "deploy", patterns, _matcher(),
        )
        assert result["winner"] == "draft"

    def test_exclude_pattern_id_removes_stale_self(self):
        # Editing ^deploy$ into "ship it": the old block is being
        # rewritten, so it must not shadow the draft (or answer at all)
        # when the user tests the OLD trigger text.
        deploy_id = PatternManager.pattern_id("^deploy$")
        patterns = [
            _entry("^save$"),
            _entry("^deploy$", is_user=True),
        ]
        excluded = pattern_tester.run_test_draft(
            _draft(trigger="ship it", exclude_pattern_id=deploy_id),
            "deploy", patterns, _matcher(),
        )
        assert excluded["winner"] == "none"
        assert excluded["shadowed_by"] is None
        # Contrast: without the exclusion the stale self would respond.
        stale = pattern_tester.run_test_draft(
            _draft(trigger="ship it"), "deploy", patterns, _matcher(),
        )
        assert stale["winner"] == "existing"
        assert stale["shadowed_by"]["pattern_id"] == deploy_id

    def test_exclude_with_unchanged_trigger_replaces_in_place(self):
        # Editing ^deploy$ without changing the trigger: the draft takes
        # the old block's slot and wins -- the stale self must not be
        # reported as a conflicting existing pattern.
        deploy_id = PatternManager.pattern_id("^deploy$")
        patterns = [
            _entry("^save$"),
            _entry("^deploy$", is_user=True),
        ]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy", exclude_pattern_id=deploy_id),
            "deploy", patterns, _matcher(),
        )
        assert result["winner"] == "draft"
        assert result["shadowed_by"] is None

    def test_nothing_matches_reports_none(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy"), "hello world", patterns, _matcher(),
        )
        assert result["winner"] == "none"
        assert result["draft_matches"] is False
        assert result["shadowed_by"] is None


# ---------------------------------------------------------------------------
# run_test_draft: advanced-mode raw drafts (wh-pattern-editor-advanced)
# ---------------------------------------------------------------------------

def _raw_draft(expression, actions=None, pattern_type="command", **extra):
    """Advanced-mode draft: raw 'expression' + raw 'actions' steps."""
    draft = {
        "pattern_type": pattern_type,
        "expression": expression,
        "actions": actions if actions is not None else [
            {"function": "hk", "params": ["ctrl", "d"]},
        ],
        "requires_hotword": False,
    }
    draft.update(extra)
    return draft


class TestDraftDuplicateTrigger:
    """A draft whose trigger key collides with a DIFFERENT existing USER
    pattern would be rejected by the save (wh-pattern-editor-r0.1), so the
    try-it preview must show that rejection as a draft_error instead of
    simulating a merge the save will never perform
    (wh-pattern-editor-r2.1). The edit-mode carve-out and the built-in
    Customize preview keep working."""

    def test_collision_with_other_user_pattern_is_draft_error(self):
        patterns = [_entry("^deploy$", is_user=True)]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy"), "deploy", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] is not None
        assert "Duplicate" in result["draft_error"]
        assert result["draft_matches"] is False
        assert result["winner"] == "none"

    def test_edit_mode_keeps_own_trigger_without_duplicate_error(self):
        patterns = [_entry("^deploy$", is_user=True)]
        result = pattern_tester.run_test_draft(
            _draft(
                trigger="deploy",
                exclude_pattern_id=PatternManager.pattern_id("^deploy$"),
            ),
            "deploy", patterns, _matcher(),
        )
        assert result["draft_error"] is None
        assert result["winner"] == "draft"

    def test_builtin_same_trigger_previews_override_not_duplicate(self):
        # Overriding a built-in is the Customize flow working as designed;
        # only USER-vs-USER collisions are duplicates.
        patterns = [_entry("^deploy$", is_user=False)]
        result = pattern_tester.run_test_draft(
            _draft(trigger="deploy"), "deploy", patterns, _matcher(),
        )
        assert result["draft_error"] is None
        assert result["winner"] == "draft"


class TestRunTestDraftRaw:
    """Raw drafts go through the same resolution paths raw saves use, so
    the try-it answer and the save validation can never disagree."""

    def test_out_of_range_group_ref_is_draft_error(self):
        # Save-path parity for wh-pattern-editor-r2.2: the group-ref range
        # check lives at the shared _resolve_block_content seam, so the
        # draft preview reports it the same way the save rejects it.
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _raw_draft(
                r"^open (.+)$",
                actions=[{"function": "activate", "params": ["g2"]}],
            ),
            "open notepad", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] is not None
        assert "g2" in result["draft_error"]
        assert result["winner"] == "none"

    def test_expression_draft_wins_and_resolves_groups(self):
        patterns = [_entry("^save$")]
        draft = _raw_draft(
            r"^find\s+(.+)$",
            actions=[
                {"function": "hk", "params": ["ctrl", "f"]},
                {"function": "type_text", "params": ["g1"]},
            ],
        )
        result = pattern_tester.run_test_draft(
            draft, "find hello world", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] is None
        assert result["winner"] == "draft"
        assert result["groups"] == ["hello world"]
        assert result["resolved_steps"] == [
            {"function": "hk", "params": ["ctrl", "f"]},
            {"function": "type_text", "params": ["hello world"]},
        ]

    def test_expression_compile_error_is_draft_error(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _raw_draft("^(unclosed$"), "save", patterns, _matcher(),
        )
        assert result["success"] is True
        assert "does not compile" in result["draft_error"]
        assert result["winner"] == "none"

    def test_type_contradiction_is_draft_error(self):
        # Same honesty rule as the save path: the runtime decides the kind
        # from the ^ anchor, so a contradictory declared type is an error,
        # not a silent reinterpretation.
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _raw_draft(r"\bdeploy\b", pattern_type="command"),
            "deploy", patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] is not None
        assert "^" in result["draft_error"]

    def test_empty_raw_actions_is_draft_error(self):
        patterns = [_entry("^save$")]
        result = pattern_tester.run_test_draft(
            _raw_draft("^deploy$", actions=[]), "deploy",
            patterns, _matcher(),
        )
        assert result["success"] is True
        assert "action step" in result["draft_error"]

    def test_expression_same_key_replaces_existing_in_place(self):
        # A raw expression equal to an existing pattern's raw string shares
        # its trigger key, so the draft replaces it in place and wins.
        patterns = [
            _entry("^save$"),
            _entry("^(?:save|store)$"),
        ]
        result = pattern_tester.run_test_draft(
            _raw_draft("^save$"), "save", patterns, _matcher(),
        )
        assert result["winner"] == "draft"
        assert result["shadowed_by"] is None

    def test_expression_wins_over_trigger_and_phrases(self):
        patterns = [_entry("^save$")]
        draft = _raw_draft("^ship$", trigger="deploy", phrases=["deploy"])
        hit = pattern_tester.run_test_draft(
            draft, "ship", patterns, _matcher(),
        )
        assert hit["winner"] == "draft"
        miss = pattern_tester.run_test_draft(
            draft, "deploy", patterns, _matcher(),
        )
        assert miss["winner"] == "none"

    def test_numeric_transform_applies_to_raw_expression(self):
        # A raw (\d+) group compiles the way the catalog would compile it:
        # transformed to (\w+) plus the numeric validation gate.
        patterns = [_entry("^save$")]
        draft = _raw_draft(
            r"^repeat (\d+)$",
            actions=[{"function": "hk", "params": ["ctrl", "y", "g1"]}],
        )
        yes = pattern_tester.run_test_draft(
            draft, "repeat 3", patterns, _matcher(),
        )
        assert yes["winner"] == "draft"
        assert yes["groups"] == ["3"]
        no = pattern_tester.run_test_draft(
            draft, "repeat xyz", patterns, _matcher(),
        )
        assert no["winner"] == "none"


# ---------------------------------------------------------------------------
# Bounded matching (wh-pattern-editor-r0.4): pathological patterns raise
# draft_error / abort the test instead of hanging the Logic asyncio loop.
# ---------------------------------------------------------------------------

PATHOLOGICAL_EXPR = r"^(\w+\s*)+$"
PATHOLOGICAL_TEXT = "a" * 30 + "!"


class TestBoundedMatching:

    def test_pathological_draft_returns_draft_error(self):
        patterns = [_entry("^save$")]
        draft = _raw_draft(PATHOLOGICAL_EXPR)
        result = pattern_tester.run_test_draft(
            draft, PATHOLOGICAL_TEXT, patterns, _matcher(),
        )
        assert result["success"] is True
        assert result["draft_error"] == (
            "This pattern takes too long to match. It could freeze "
            "Wheelhouse. Simplify the expression (avoid nested repeats "
            "like (\\w+\\s*)+)."
        )
        assert result["winner"] == "none"
        assert result["draft_matches"] is False

    def test_pathological_saved_pattern_aborts_phrase_test(self):
        # First timeout aborts the whole test; the failure names the
        # offending pattern's trigger or expression (an alternation-only
        # expression falls back to quoting the raw pattern).
        patterns = [_entry(PATHOLOGICAL_EXPR)]
        result = pattern_tester.run_test_phrase(
            PATHOLOGICAL_TEXT, patterns, _matcher(),
        )
        assert result["success"] is False
        assert "too long" in result["error"]
        assert PATHOLOGICAL_EXPR in result["error"]


# ---------------------------------------------------------------------------
# Logic-side handlers (style of test_pattern_manager_update.py)
# ---------------------------------------------------------------------------

SYSTEM_CONTENT = (
    'COMMAND_HOTWORD = "x-ray"\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^save$'''\n"
    'requires_hotword = true\n'
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "s"] }\n'
    ']\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^open (.+)$'''\n"
    'actions = [\n'
    '    { function = "activate", params = ["g1"] }\n'
    ']\n'
)


class _FakeTextParser:
    """Same wiring TextParser.__init__ does, minus action execution."""

    def __init__(self, catalog):
        self.patterns = catalog.get_all_patterns()
        self.matcher = PatternMatcher(catalog)


class _FakeSpeechHandler:
    """Minimal stand-in exposing what _handle_pattern_manager_action touches."""

    def __init__(self, patterns_file, user_patterns_file):
        from speech.pattern_catalog import PatternCatalog

        self.patterns_file = patterns_file
        self.user_patterns_file = user_patterns_file
        self.pattern_catalog = PatternCatalog(patterns_file, user_patterns_file)
        self.text_parser = _FakeTextParser(self.pattern_catalog)

    def apply_hotword(self, hotword):
        pass


class _CapturingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _make_controller(tmp_path):
    from main import LogicController

    system_file = tmp_path / "patterns.toml"
    system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
    user_file = tmp_path / "user_patterns.toml"  # never created

    controller = MagicMock(spec=LogicController)
    controller._handle_pattern_manager_action = (
        LogicController._handle_pattern_manager_action.__get__(controller)
    )
    handler = _FakeSpeechHandler(str(system_file), str(user_file))
    controller.service_manager = MagicMock()
    controller.service_manager.speech_handler = handler
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _CapturingQueue()
    return controller


def _results(controller, action):
    return [
        m for m in controller.state_manager.state_to_gui_queue.items
        if m.get("action") == action
    ]


class TestPatternTesterHandlers:
    """pm_test_phrase / pm_test_draft: same envelope and queue as other pm_*."""

    async def test_phrase_round_trip_match(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_phrase", {"data": {"text": "open notepad"}},
        )
        results = _results(controller, "pm_test_phrase_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        match = data["match"]
        assert match["pattern_id"] == PatternManager.pattern_id("^open (.+)$")
        assert match["requires_hotword"] is False
        assert match["groups"] == ["notepad"]
        assert match["resolved_steps"] == [
            {"function": "activate", "params": ["notepad"]},
        ]

    async def test_phrase_round_trip_hotword_flag(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_phrase", {"data": {"text": "save"}},
        )
        data = _results(controller, "pm_test_phrase_result")[0]["data"]
        assert data["match"]["pattern_id"] == PatternManager.pattern_id("^save$")
        assert data["match"]["requires_hotword"] is True

    async def test_phrase_round_trip_no_match(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_phrase", {"data": {"text": "gibberish words"}},
        )
        data = _results(controller, "pm_test_phrase_result")[0]["data"]
        assert data["success"] is True
        assert data["match"] is None

    async def test_draft_round_trip_draft_wins(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_draft",
            {"data": {"draft": _draft(trigger="deploy"), "text": "deploy"}},
        )
        results = _results(controller, "pm_test_draft_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        assert data["winner"] == "draft"
        assert data["resolved_steps"] == [
            {"function": "hk", "params": ["ctrl", "d"]},
        ]

    async def test_draft_round_trip_shadowed_by_builtin(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_draft",
            {"data": {"draft": _draft(phrases=["save"]), "text": "save"}},
        )
        data = _results(controller, "pm_test_draft_result")[0]["data"]
        assert data["winner"] == "existing"
        assert data["shadowed_by"]["pattern_id"] == (
            PatternManager.pattern_id("^save$")
        )
        assert data["draft_matches"] is True

    async def test_draft_round_trip_draft_error(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_draft",
            {"data": {"draft": _draft(trigger=""), "text": "save"}},
        )
        data = _results(controller, "pm_test_draft_result")[0]["data"]
        assert data["success"] is True
        assert data["draft_error"] == "Trigger phrase cannot be empty"
        assert data["winner"] == "none"

    async def test_missing_text_field_reports_error_envelope(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_phrase", {"data": {}},
        )
        data = _results(controller, "pm_test_phrase_result")[0]["data"]
        assert data["success"] is False
        assert "error" in data

    async def test_draft_round_trip_raw_expression(self, tmp_path):
        # Advanced-mode draft through the real handler: raw expression +
        # raw steps, groups captured and resolved (wh-pattern-editor-advanced).
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_draft",
            {"data": {
                "draft": _raw_draft(
                    r"^find\s+(.+)$",
                    actions=[
                        {"function": "hk", "params": ["ctrl", "f"]},
                        {"function": "type_text", "params": ["g1"]},
                    ],
                ),
                "text": "find hello",
            }},
        )
        data = _results(controller, "pm_test_draft_result")[0]["data"]
        assert data["success"] is True
        assert data["winner"] == "draft"
        assert data["groups"] == ["hello"]
        assert data["resolved_steps"] == [
            {"function": "hk", "params": ["ctrl", "f"]},
            {"function": "type_text", "params": ["hello"]},
        ]

    async def test_phrase_result_echoes_request_id(self, tmp_path):
        # The dialogs correlate answers with the request that produced
        # them via an echoed request_id, so a slow answer cannot render
        # against newer input (wh-pattern-editor-r6.1).
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_phrase", {"data": {"text": "save", "request_id": 7}},
        )
        data = _results(controller, "pm_test_phrase_result")[0]["data"]
        assert data["request_id"] == 7

    async def test_phrase_result_without_request_id_omits_key(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_phrase", {"data": {"text": "save"}},
        )
        data = _results(controller, "pm_test_phrase_result")[0]["data"]
        assert "request_id" not in data

    async def test_draft_result_echoes_request_id(self, tmp_path):
        controller = _make_controller(tmp_path)
        await controller._handle_pattern_manager_action(
            "pm_test_draft",
            {"data": {"draft": _draft(trigger="deploy"), "text": "deploy",
                      "request_id": 3}},
        )
        data = _results(controller, "pm_test_draft_result")[0]["data"]
        assert data["request_id"] == 3
