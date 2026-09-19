"""Tests for speech/action_catalog.py (wh-pattern-editor-catalog).

The catalog is the data source the Pattern Manager editor uses for its
function picker, generated parameter fields, hover help, and the Help
reference page (spec: docs/plans/2026-07-09-pattern-manager-editor-design-v1.md
section 5). Three guarantees are enforced here:

1. Drift, both directions: every function registered in the real
   ``ActionFunctions`` registry has a catalog entry, and every catalog
   entry names a registered function. The registry is walked for real
   (``ActionFunctions(Mock())``), not scraped from source, so a rename
   or new registration fails this file immediately.
2. Dependency-freeness: the module imports in a bare subprocess where
   every non-stdlib import is blocked (same style as
   test_pywin32_import_isolation.py), proving both the Logic and GUI
   processes can import it with no WheelHouse or third-party deps.
3. Structural validity: required fields, allowed audience values,
   allowed param kinds, non-empty summaries, and the spec-fixed
   ``internal`` set (exactly four names; spec section 5 says the
   internal list is fixed while basic/advanced may move).
"""
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

from speech import action_catalog
from speech.actions import ActionFunctions

_TESTS_DIR = Path(__file__).parent
_SERVICE_DIR = _TESTS_DIR.parent

# Independent copies of the spec's allowed sets. Deliberately NOT imported
# from action_catalog: if the module's own constants drifted from the spec,
# tests reusing them would still pass.
ALLOWED_AUDIENCES = {"basic", "advanced", "internal"}
ALLOWED_PARAM_KINDS = {
    "text",
    "key",
    "keys",
    "path",
    "exe_or_title",
    "number",
    "group_ref",
    "choice",
}
# Fixed by spec section 5: never shown in the picker.
SPEC_INTERNAL_SET = {
    "skip_clipboard_restore",
    "capture_clipboard",
    "add_hint_to_stt",
    "set_speech_interaction_mode",
}

ENTRY_FIELDS = {
    "name", "label", "summary", "params", "example", "audience", "group",
}
PARAM_FIELDS = {"name", "summary", "kind"}

# The picker's sub-heading names (wh-action-picker-ordering). Kept as an
# independent copy for the same reason as the sets above: a typo that
# invented a near-duplicate group ("Mouse Grid") would still pass a test
# that reused the module's own tuple.
ALLOWED_GROUPS = {
    "AI",
    "Clicking",
    "Clipboard",
    "Date and pauses",
    "Keyboard",
    "Mouse grid",
    "Programs and web",
    "Scrolling",
    "Text",
    "Wheelhouse",
}


def _registered_names():
    """Walk the real registry: the names patterns.toml can call."""
    return set(ActionFunctions(Mock()).get_functions())


def _catalog_names():
    return [entry["name"] for entry in action_catalog.ACTION_CATALOG]


# ---------------------------------------------------------------------------
# 1. Drift against the real registry
# ---------------------------------------------------------------------------


class TestRegistryDrift:
    def test_every_registered_function_has_a_catalog_entry(self):
        missing = _registered_names() - set(_catalog_names())
        assert not missing, (
            "Registered in speech/actions.py but missing from the catalog "
            f"(add entries to speech/action_catalog.py): {sorted(missing)}"
        )

    def test_every_catalog_entry_names_a_registered_function(self):
        stale = set(_catalog_names()) - _registered_names()
        assert not stale, (
            "In the catalog but not registered in speech/actions.py "
            f"(remove or rename these entries): {sorted(stale)}"
        )

    def test_catalog_names_are_unique(self):
        names = _catalog_names()
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"Duplicate catalog entries: {sorted(dupes)}"

    def test_by_name_index_matches_the_sequence(self):
        assert set(action_catalog.CATALOG_BY_NAME) == set(_catalog_names())
        for entry in action_catalog.ACTION_CATALOG:
            assert action_catalog.CATALOG_BY_NAME[entry["name"]] is entry


# ---------------------------------------------------------------------------
# 2. Dependency-freeness (bare subprocess, stdlib only)
# ---------------------------------------------------------------------------


class TestDependencyFreeness:
    def test_imports_with_every_non_stdlib_module_blocked(self):
        """Import the module in a subprocess whose meta-path raises on any
        import that is neither stdlib nor the module itself. If
        action_catalog ever grows a WheelHouse or third-party import (or a
        side effect that triggers one), this fails with the blocked name."""
        script = f"""
import sys
import importlib.abc

ALLOWED_LOCAL = {{"speech", "speech.action_catalog"}}

class _BlockNonStdlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ALLOWED_LOCAL:
            return None
        root = fullname.partition(".")[0]
        if root in sys.stdlib_module_names:
            return None
        raise ModuleNotFoundError(
            "blocked for dependency-freeness test: " + fullname
        )

sys.meta_path.insert(0, _BlockNonStdlib())
sys.path.insert(0, {str(_SERVICE_DIR)!r})

import speech.action_catalog as ac

assert len(ac.ACTION_CATALOG) > 0
assert "hk" in ac.CATALOG_BY_NAME
print("IMPORT_OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_SERVICE_DIR),
        )
        assert result.returncode == 0, result.stderr[-3000:]
        assert "IMPORT_OK" in result.stdout


# ---------------------------------------------------------------------------
# 3. Structural validity
# ---------------------------------------------------------------------------


class TestEntryStructure:
    def test_every_entry_has_exactly_the_required_fields(self):
        for entry in action_catalog.ACTION_CATALOG:
            assert set(entry) == ENTRY_FIELDS, (
                f"entry {entry.get('name')!r} has fields {sorted(entry)}, "
                f"expected exactly {sorted(ENTRY_FIELDS)}"
            )

    def test_string_fields_are_nonempty_strings(self):
        for entry in action_catalog.ACTION_CATALOG:
            for field in ("name", "label", "summary", "example"):
                value = entry[field]
                assert isinstance(value, str) and value.strip(), (
                    f"entry {entry.get('name')!r}: field {field!r} must be "
                    f"a non-empty string, got {value!r}"
                )

    def test_audience_in_allowed_set(self):
        for entry in action_catalog.ACTION_CATALOG:
            assert entry["audience"] in ALLOWED_AUDIENCES, (
                f"entry {entry['name']!r}: audience {entry['audience']!r} "
                f"not in {sorted(ALLOWED_AUDIENCES)}"
            )

    def test_params_are_well_formed(self):
        for entry in action_catalog.ACTION_CATALOG:
            params = entry["params"]
            assert isinstance(params, (list, tuple)), (
                f"entry {entry['name']!r}: params must be a sequence"
            )
            for param in params:
                name = entry["name"]
                # choices is present exactly when kind == "choice".
                if param.get("kind") == "choice":
                    expected = PARAM_FIELDS | {"choices"}
                else:
                    expected = PARAM_FIELDS
                assert set(param) == expected, (
                    f"entry {name!r}: param {param.get('name')!r} has "
                    f"fields {sorted(param)}, expected {sorted(expected)}"
                )
                assert param["kind"] in ALLOWED_PARAM_KINDS, (
                    f"entry {name!r}: param {param['name']!r} kind "
                    f"{param['kind']!r} not in {sorted(ALLOWED_PARAM_KINDS)}"
                )
                for field in ("name", "summary"):
                    value = param[field]
                    assert isinstance(value, str) and value.strip(), (
                        f"entry {name!r}: param field {field!r} must be a "
                        f"non-empty string, got {value!r}"
                    )
                if "choices" in param:
                    choices = param["choices"]
                    assert isinstance(choices, (list, tuple)) and choices, (
                        f"entry {name!r}: param {param['name']!r} choices "
                        "must be a non-empty sequence"
                    )
                    for choice in choices:
                        assert isinstance(choice, str) and choice.strip(), (
                            f"entry {name!r}: param {param['name']!r} has "
                            f"a bad choice {choice!r}"
                        )

    def test_internal_entries_are_exactly_the_spec_four(self):
        internal = {
            entry["name"]
            for entry in action_catalog.ACTION_CATALOG
            if entry["audience"] == "internal"
        }
        assert internal == SPEC_INTERNAL_SET, (
            "The internal set is fixed by spec section 5. "
            f"Expected {sorted(SPEC_INTERNAL_SET)}, got {sorted(internal)}"
        )

    def test_basic_entries_exist(self):
        # Spec: basic = the four simple-mode action types. Membership may
        # move between basic and advanced later (a one-line change), so we
        # only require that a non-empty basic tier exists for the picker.
        basic = [
            entry
            for entry in action_catalog.ACTION_CATALOG
            if entry["audience"] == "basic"
        ]
        assert basic, "The catalog must expose at least one basic entry"

    def test_result_producing_actions_are_catalog_entries(self):
        # The editor offers a result-name field for exactly these actions
        # (wh-editor-step-result-name); a typo here would hide the field
        # from an action that does produce a value.
        assert action_catalog.RESULT_PRODUCING_ACTIONS
        for name in action_catalog.RESULT_PRODUCING_ACTIONS:
            assert name in action_catalog.CATALOG_BY_NAME, name

    def test_every_entry_group_is_in_the_allowed_set(self):
        for entry in action_catalog.ACTION_CATALOG:
            assert entry["group"] in ALLOWED_GROUPS, (
                f"entry {entry['name']!r}: group {entry.get('group')!r} not "
                f"in {sorted(ALLOWED_GROUPS)}"
            )

    def test_transform_selection_choices_are_real_transforms(self):
        """The catalog hardcodes the transform names (importing the UI
        module would break dependency-freeness), so guard the copy: every
        advertised choice must be accepted by the real transformer.
        SelectionTransformer.apply_transformation returns None for unknown
        types, so a stale or misspelled choice fails here."""
        from ui.selection_transformer import SelectionTransformer

        transformer = SelectionTransformer()
        entry = action_catalog.CATALOG_BY_NAME["transform_selection"]
        (param,) = entry["params"]
        for choice in param["choices"]:
            result = transformer.apply_transformation("hello world", choice)
            assert result is not None, (
                f"catalog advertises transform {choice!r} but "
                "SelectionTransformer does not accept it"
            )


# ---------------------------------------------------------------------------
# 4. Picker order (wh-action-picker-ordering)
# ---------------------------------------------------------------------------


class TestPickerSections:
    """``picker_sections`` holds the one ordering rule the editor renders.

    The rule a user can read off the list: entries are sorted A-Z by their
    display label, and the long advanced list is split into named groups
    that are themselves sorted A-Z. Nothing here may move an entry between
    the basic and advanced headings -- the audience field decides that.
    """

    def _names(self, sections):
        return [
            entry["name"]
            for _group, entries in sections
            for entry in entries
        ]

    def test_basic_is_one_unnamed_alphabetical_section(self):
        sections = action_catalog.picker_sections("basic")
        assert len(sections) == 1
        group, entries = sections[0]
        assert group is None
        labels = [entry["label"] for entry in entries]
        assert labels == sorted(labels, key=str.casefold)

    def test_advanced_groups_are_alphabetical_and_named(self):
        sections = action_catalog.picker_sections("advanced")
        groups = [group for group, _entries in sections]
        assert len(groups) > 1
        assert all(isinstance(group, str) and group for group in groups)
        assert groups == sorted(groups, key=str.casefold)

    def test_advanced_entries_are_alphabetical_inside_each_group(self):
        for group, entries in action_catalog.picker_sections("advanced"):
            labels = [entry["label"] for entry in entries]
            assert labels == sorted(labels, key=str.casefold), group

    def test_every_advanced_entry_carries_its_own_group(self):
        for group, entries in action_catalog.picker_sections("advanced"):
            for entry in entries:
                assert entry["group"] == group

    def test_sections_never_move_an_entry_between_audiences(self):
        for audience in ("basic", "advanced"):
            expected = {
                entry["name"]
                for entry in action_catalog.ACTION_CATALOG
                if entry["audience"] == audience
            }
            listed = self._names(action_catalog.picker_sections(audience))
            assert sorted(listed) == sorted(expected)
            assert len(listed) == len(set(listed))

    def test_internal_entries_get_no_section(self):
        assert action_catalog.picker_sections("internal") == ()


# ---------------------------------------------------------------------------
# The Pattern Manager Help reference renders every non-internal example
# verbatim (pattern_help_dialog.build_function_reference_html), so an example
# is a spoken instruction to the user, not a comment. Commit 9fd44173 renamed
# the trigger words of the numbered overlay and the mouse grid and left these
# four examples on the retired words. Codex found it in the closing-gate
# review (wh-voice-access-parity.1.6.2.3).

# Catalog entry name -> the pattern that actually ships for it.
RENAMED_COMMAND_ACTIONS = {
    "show_overlay_command": "^(?:show|apply) numbers$",
    "hide_overlay_command": "^(?:hide|dismiss) numbers$",
    "grid_show_command": "^(?:show|apply) grid$",
    "grid_dismiss_command": "^(?:hide|dismiss) grid$",
}

_TRIGGER_RE = re.compile(r'Trigger "([^"]+)"')
_SAYING_RE = re.compile(r'saying "([^"]+)"')


def _shipped_patterns():
    """Every raw pattern string in the production pattern file."""
    raw = (_SERVICE_DIR / "speech" / "config" / "patterns.toml").read_bytes()
    parsed = tomllib.loads(raw.decode("utf-8"))
    return {entry["pattern"] for entry in parsed["pattern"]}


class TestRenamedCommandExamples:
    """A renamed command must not keep its old words in visible help.

    The scope is deliberately the four entries this branch renamed. A
    blanket rule over every catalog entry would be wrong: several examples
    quote a pattern the user writes for themselves, such as
    ``^jira (.+)$``, which the shipped file has no reason to carry.
    """

    def test_documented_trigger_is_the_shipped_pattern(self):
        shipped = _shipped_patterns()
        for name, expected in RENAMED_COMMAND_ACTIONS.items():
            entry = action_catalog.CATALOG_BY_NAME[name]
            match = _TRIGGER_RE.search(entry["example"])
            assert match, f"{name} example quotes no trigger"
            assert match.group(1) == expected, (
                f"{name} documents the trigger {match.group(1)!r}, but the "
                f"shipped pattern is {expected!r}"
            )
            assert expected in shipped, (
                f"{expected!r} is not in the production pattern file"
            )

    def test_the_spoken_example_runs_the_command_it_describes(self):
        for name, expected in RENAMED_COMMAND_ACTIONS.items():
            entry = action_catalog.CATALOG_BY_NAME[name]
            match = _SAYING_RE.search(entry["example"])
            assert match, f"{name} example quotes no spoken phrase"
            spoken = match.group(1)
            assert re.match(expected, spoken), (
                f"{name} tells the user to say {spoken!r}, which no longer "
                f"matches the shipped pattern {expected!r}"
            )
# ---------------------------------------------------------------------------
# wh-voice-access-parity.1.6.2.2: a command that needs the hotword must not be
# published with a bare invocation
# ---------------------------------------------------------------------------
#
# Commit 9fd44173 gave click-element requires_hotword = true. The catalog kept
# telling the reader to say a bare "click submit button", and quoted the
# retired trigger ^click\s+(.+)$ beside it, so both halves of the example were
# false. pattern_help_dialog.build_function_reference_html renders every
# non-internal example verbatim, which makes an example an instruction rather
# than a comment -- the same reasoning TestRenamedCommandExamples above rests
# on.
#
# The rule below is deliberately narrow in ONE way and broad in another. It is
# narrow in that it judges only examples whose quoted trigger is a pattern the
# shipped file actually carries: many examples quote a pattern the user writes
# for themselves, such as ^jira (.+)$, and the shipped file has no reason to
# hold those. It is broad in that within that set it covers every entry, not a
# hand list, so a FUTURE requires_hotword change on any command is caught the
# moment it lands rather than when someone remembers to extend a list.
#
# Measured 2026-08-28 over the shipped file: 22 catalog examples quote a
# trigger the pattern file carries, 11 quote one it does not, and three of the
# 22 require the hotword -- fix_text_ai, cancel_fix and click_element.


def _shipped_pattern_entries():
    """Every pattern table in the production file, parsed."""
    raw = (_SERVICE_DIR / "speech" / "config" / "patterns.toml").read_bytes()
    return tomllib.loads(raw.decode("utf-8"))["pattern"]


def _command_hotword():
    """The hotword the production pattern file declares, not a constant here."""
    raw = (_SERVICE_DIR / "speech" / "config" / "patterns.toml").read_bytes()
    return tomllib.loads(raw.decode("utf-8"))["COMMAND_HOTWORD"]


def _examples_over_shipped_triggers():
    """(name, entry, shipped pattern table) for each example quoting a shipped
    trigger. Internal entries carry no example the reader ever sees."""
    by_pattern = {p["pattern"]: p for p in _shipped_pattern_entries()}
    found = []
    for entry in action_catalog.ACTION_CATALOG:
        if entry.get("audience") == "internal":
            continue
        match = _TRIGGER_RE.search(entry.get("example") or "")
        if not match:
            continue
        shipped = by_pattern.get(match.group(1))
        if shipped is not None:
            found.append((entry["name"], entry, shipped))
    return found


class TestPublishedExamplesHonourTheHotword:
    def test_the_click_by_name_example_quotes_the_shipped_trigger(self):
        """The named half of the finding: the trigger itself was retired.

        Kept as its own test beside the sweep below, because a sweep that
        judges only examples whose trigger IS shipped cannot see an example
        whose trigger is shipped nowhere -- which is precisely the state this
        entry was in.
        """
        entry = action_catalog.CATALOG_BY_NAME["click_element"]
        match = _TRIGGER_RE.search(entry["example"])
        assert match, "click_element example quotes no trigger"
        assert match.group(1) in _shipped_patterns(), (
            f"click_element documents the trigger {match.group(1)!r}, which "
            "the production pattern file does not carry"
        )

    def test_a_hotword_command_is_never_shown_without_its_hotword(self):
        hotword = _command_hotword()
        offenders = []
        for name, entry, shipped in _examples_over_shipped_triggers():
            if not shipped.get("requires_hotword"):
                continue
            said = _SAYING_RE.search(entry["example"])
            if not said:
                offenders.append(f"{name}: example quotes no spoken phrase")
                continue
            spoken = said.group(1)
            if not spoken.lower().startswith(f"{hotword.lower()} "):
                offenders.append(
                    f"{name}: tells the reader to say {spoken!r}, but "
                    f"{shipped['pattern']!r} requires the {hotword!r} hotword"
                )
        assert offenders == [], (
            "these published examples show a hotword command without its "
            f"hotword: {offenders}"
        )

    def test_the_hotword_example_still_fires_once_the_hotword_is_removed(self):
        """The other half: carrying the hotword is not enough if the rest is
        wrong. The router consumes the hotword before matching, so what is
        left must match the shipped pattern."""
        hotword = _command_hotword()
        offenders = []
        for name, entry, shipped in _examples_over_shipped_triggers():
            if not shipped.get("requires_hotword"):
                continue
            said = _SAYING_RE.search(entry["example"])
            if not said:
                continue
            spoken = said.group(1)
            body = spoken[len(hotword) + 1:] if spoken.lower().startswith(
                f"{hotword.lower()} "
            ) else spoken
            if not re.match(shipped["pattern"], body):
                offenders.append(
                    f"{name}: {body!r} does not match {shipped['pattern']!r}"
                )
        assert offenders == [], (
            f"these published examples name a phrase that never fires: {offenders}"
        )

    def test_the_rule_covers_something(self):
        """A guard that judges nothing passes forever and nobody notices.

        If every hotword command loses its catalog example, the two sweeps
        above go quiet rather than failing, so the count is asserted here
        instead of being left implicit.
        """
        hotword_entries = [
            name
            for name, _entry, shipped in _examples_over_shipped_triggers()
            if shipped.get("requires_hotword")
        ]
        assert "click_element" in hotword_entries, (
            "click_element is the entry this finding is about; if its trigger "
            "stopped matching a shipped pattern the sweeps would go blind to "
            f"it. In scope right now: {sorted(hotword_entries)}"
        )
        assert len(hotword_entries) >= 3, (
            "fewer hotword commands are documented than the three measured "
            f"when this guard was written: {sorted(hotword_entries)}"
        )
