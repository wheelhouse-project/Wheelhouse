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
import subprocess
import sys
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

ENTRY_FIELDS = {"name", "label", "summary", "params", "example", "audience"}
PARAM_FIELDS = {"name", "summary", "kind"}


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
