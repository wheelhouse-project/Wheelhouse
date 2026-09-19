"""Saving a moved customization restores its origin (wh-override-trigger-reenable-origin)."""

import tomllib

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager
from speech.pattern_matcher import PatternMatcher
from speech.pattern_tester import run_test_draft


SYSTEM = '''COMMAND_HOTWORD = "x-ray"
[[pattern]]
doc_id = "first-command"
pattern = '^first$'
actions = [{function = "text", params = ["FIRST"]}]
[[pattern]]
doc_id = "second-command"
pattern = '^second$'
actions = [{function = "text", params = ["SECOND"]}]
'''


@pytest.fixture
def bench(tmp_path):
    system = tmp_path / "patterns.toml"
    user = tmp_path / "user_patterns.toml"
    system.write_text(SYSTEM, encoding="utf-8")
    user.write_text('', encoding="utf-8")
    yield PatternManager(str(system), str(user)), system, user
    from speech import safe_regex
    safe_regex.shutdown()


def data(expression):
    return dict(expression=expression, pattern_type="command",
                action_type="text", action_params={"output": "CUSTOM"})


def outputs(catalog, phrase):
    return [entry[2]["actions"][0]["params"][0]
            for entry in catalog.get_matching_patterns(phrase)]


@pytest.mark.parametrize("mode", ["create", "update"])
@pytest.mark.parametrize("destination", ["second", "new"])
def test_moved_customization_restores_origin_and_previews_save(bench, mode, destination):
    """Keeping the old doc_id hides FIRST and puts CUSTOM in the wrong slot."""
    manager, system, user = bench
    draft = data(f"^{destination}$") | {"doc_id": "first-command"}
    if mode == "update":
        created = manager.create_pattern(**data("^first$"), doc_id="first-command")
        assert created["success"], created
        draft["exclude_pattern_id"] = created["pattern_id"]
    catalog = PatternCatalog(str(system), str(user))
    before = user.read_bytes()
    preview = run_test_draft(draft, destination, catalog.get_all_patterns(),
                             PatternMatcher(catalog), catalog=catalog)
    assert preview["success"] and preview["draft_error"] is None, preview
    assert user.read_bytes() == before
    if mode == "update":
        result = manager.update_pattern(created["pattern_id"], data(f"^{destination}$"))
    else:
        result = manager.create_pattern(**draft)
    assert result["success"], result
    assert catalog.reload()
    assert outputs(catalog, "first") == ["FIRST"]
    assert outputs(catalog, destination) == (["SECOND", "CUSTOM"] if destination == "second" else ["CUSTOM"])
    assert preview["winner"] == ("existing" if destination == "second" else "draft")
    stored = tomllib.loads(user.read_text(encoding="utf-8"))["pattern"][0]
    assert "doc_id" not in stored
    assert stored["origin"] == "user"
    restarted = PatternCatalog(str(system), str(user))
    assert outputs(restarted, "first") == ["FIRST"]
    listing = manager.get_all_patterns_structured()
    rows = [row for category in listing["categories"].values()
            for row in category["patterns"]]
    custom = next(row for row in rows if row["is_user_created"])
    assert not custom["overrides_builtin"]


def test_unchanged_trigger_keeps_override_after_release_rewrite(bench):
    """Comparing only against today's shipped regex silently drops old overrides."""
    manager, system, user = bench
    created = manager.create_pattern(**data("^first$"), doc_id="first-command")
    assert created["success"]
    system.write_text(SYSTEM.replace("^first$", "^first(?: now)?$"), encoding="utf-8")
    result = manager.update_pattern(created["pattern_id"], data("^first$"))
    assert result["success"], result
    assert outputs(PatternCatalog(str(system), str(user)), "first") == ["CUSTOM"]
    assert tomllib.loads(user.read_text(encoding="utf-8"))["pattern"][0]["doc_id"] == "first-command"


def test_legacy_moved_override_uses_load_resolution_for_destination(bench):
    """Dropping origin metadata must not bypass the existing legacy resolution."""
    manager, system, user = bench
    user.write_text('''[[pattern]]
doc_id = "first-command"
pattern = '^first$'
actions = [{function = "text", params = ["OLD"]}]
''', encoding="utf-8")
    result = manager.update_pattern(manager.pattern_id("^first$"), data("^second$"))
    assert result["success"], result
    catalog = PatternCatalog(str(system), str(user))
    assert outputs(catalog, "first") == ["FIRST"]
    assert outputs(catalog, "second") == ["CUSTOM"]


@pytest.mark.parametrize("mode", ["create", "update"])
def test_simple_mode_serialization_keeps_unchanged_override(bench, mode):
    """A redundant phrase group must not restore the built-in on action-only save."""
    manager, system, user = bench
    if mode == "update":
        created = manager.create_pattern(**data("^first$"), doc_id="first-command")
        assert created["success"]
        # A release changes the shipped regex, but this edit keeps the words
        # already stored in the user's rule, only switching serialization.
        system.write_text(SYSTEM.replace("^first$", "^first(?: now)?$"), encoding="utf-8")
    draft = dict(phrases=["first"], pattern_type="command", action_type="text",
                 action_params={"output": "CUSTOM"}, doc_id="first-command")
    if mode == "update":
        draft["exclude_pattern_id"] = created["pattern_id"]
    catalog = PatternCatalog(str(system), str(user))
    preview = run_test_draft(draft, "first", catalog.get_all_patterns(),
                             PatternMatcher(catalog), catalog=catalog)
    if mode == "update":
        result = manager.update_pattern(created["pattern_id"], draft)
    else:
        result = manager.create_pattern(**draft)
    assert result["success"], result
    assert catalog.reload()
    assert outputs(catalog, "first") == ["CUSTOM"]
    assert preview["winner"] == "draft", preview


def test_moving_one_claimant_preserves_explicit_disable_and_failed_save(bench, monkeypatch):
    """Never remove sibling overrides or mutate the live file before replacement succeeds."""
    manager, system, user = bench
    created = manager.create_pattern(**data("^first$"), doc_id="first-command")
    assert created["success"]
    with user.open("a", encoding="utf-8") as fh:
        fh.write('''\n[[pattern]]
doc_id = "first-command"
pattern = '^disabled$'
actions = []
''')
    before = user.read_bytes()
    def failed_replace(*args):
        raise OSError("injected replacement failure")
    with monkeypatch.context() as patch:
        patch.setattr("speech.pattern_manager.os.replace", failed_replace)
        result = manager.update_pattern(created["pattern_id"], data("^new$"))
    assert not result["success"]
    assert "injected replacement failure" in result["error"]
    assert user.read_bytes() == before
    result = manager.update_pattern(created["pattern_id"], data("^new$"))
    assert result["success"], result
    catalog = PatternCatalog(str(system), str(user))
    assert outputs(catalog, "first") == []
    assert outputs(catalog, "new") == ["CUSTOM"]
    blocks = tomllib.loads(user.read_text(encoding="utf-8"))["pattern"]
    assert "doc_id" not in blocks[0]
    assert blocks[1]["doc_id"] == "first-command" and blocks[1]["actions"] == []
