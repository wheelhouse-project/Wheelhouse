"""The nested-group mutation gate must scope its run to what was asked.

wh-grid-click-nested-group.1.2. The gate rewrites speech/pattern_catalog.py
and speech/config/patterns.toml on disk for every mutation it runs, and its
own docstring tells the operator not to run any test suite while a sweep is
going. That warning is only useful if the operator can tell how many
mutations a command will run.

Two ways the selection could mislead, both of them silent:

* ``--only=<name>`` looked like a request for one mutation and ran all five.
  The equals form is the one the review-loop runbook hands to reviewers, so
  the operator who most needs the scoping is the one who lost it.
* A ``--only`` with no name after it selected nothing, ran no mutation, and
  exited 0. A green exit code with no work done is the worst outcome of the
  three, because nothing in the output looks wrong.

These tests read the gate's ``_selected`` directly with a prepared
``sys.argv``. They start no subprocess and rewrite no source file.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parent / (
    "mutation_gate_pattern_catalog_nested_group.py"
)


def _load_gate():
    """Import the gate script by path.

    The file deliberately has no ``test_`` prefix, so pytest never collects
    it and a plain import statement cannot reach it either.
    """
    spec = importlib.util.spec_from_file_location(
        "_nested_group_gate_under_test", _GATE_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"could not build an import spec for {_GATE_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


@pytest.fixture
def selection(gate, monkeypatch):
    """Call the gate's _selected() with a prepared argv."""

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["gate.py", *args])
        return gate._selected()

    return run


class TestOnlySelection:
    def test_no_flag_runs_every_mutation(self, gate, selection):
        assert selection() == gate.MUTATIONS

    def test_the_space_form_selects_one_mutation(self, gate, selection):
        name = gate.MUTATIONS[0]["name"]
        chosen = selection("--only", name)
        assert [m["name"] for m in chosen] == [name]

    def test_the_equals_form_selects_one_mutation(self, gate, selection):
        name = gate.MUTATIONS[0]["name"]
        chosen = selection(f"--only={name}")
        assert [m["name"] for m in chosen] == [name], (
            "--only=<name> ran the whole sweep instead of the one mutation "
            "asked for; that is five file-rewriting windows where the "
            "operator was told to expect one"
        )

    def test_the_equals_form_rejects_an_unknown_name(self, selection):
        assert selection("--only=no-such-mutation") is None

    def test_a_bare_only_with_no_name_is_an_error(self, selection):
        assert selection("--only") is None, (
            "a --only with nothing after it selected no mutation and would "
            "have exited 0, which reads exactly like a clean sweep"
        )

    def test_an_empty_equals_value_is_an_error(self, selection):
        assert selection("--only=") is None

    def test_both_forms_can_be_combined(self, gate, selection):
        first, second = gate.MUTATIONS[0]["name"], gate.MUTATIONS[1]["name"]
        chosen = selection("--only", first, f"--only={second}")
        assert {m["name"] for m in chosen} == {first, second}

    def test_a_later_flag_is_not_read_as_a_mutation_name(self, gate, selection):
        name = gate.MUTATIONS[0]["name"]
        chosen = selection("--only", name, "--verbose")
        assert [m["name"] for m in chosen] == [name], (
            "a flag after the space form was treated as a mutation name, so "
            "the run failed with 'no such mutation' instead of running"
        )


class TestCheckModeHonoursTheSelection:
    """wh-grid-click-nested-group.2.4.

    ``main`` returned ``check_only()`` the moment it saw ``--check``, before
    ``_selected`` was ever called, and ``check_only`` walked the whole
    ``MUTATIONS`` list. So ``--check --only=<name>`` reported five patterns
    checked while the operator had asked about one, and ``--check`` with an
    unknown or empty ``--only`` exited 0. These call ``main`` itself rather
    than ``_selected``, because the defect was in the dispatch and a test of
    ``_selected`` alone cannot see it. Check mode starts no subprocess and
    writes no file, so calling ``main`` here is safe.
    """

    @pytest.fixture
    def check(self, gate, monkeypatch, capsys):
        def run(*args):
            monkeypatch.setattr(sys, "argv", ["gate.py", "--check", *args])
            code = gate.main()
            return code, capsys.readouterr().out

        return run

    def test_the_equals_form_checks_only_that_mutation(self, gate, check):
        code, out = check(f"--only={gate.MUTATIONS[0]['name']}")
        assert code == 0, out
        assert f"checked 1 of {len(gate.MUTATIONS)} patterns" in out, (
            "check mode ignored --only and validated the whole set, so an "
            f"operator's scoped check silently read something else: {out!r}"
        )

    def test_the_space_form_checks_only_that_mutation(self, gate, check):
        code, out = check("--only", gate.MUTATIONS[0]["name"])
        assert code == 0, out
        assert f"checked 1 of {len(gate.MUTATIONS)} patterns" in out, out

    def test_no_only_checks_every_mutation(self, gate, check):
        total = len(gate.MUTATIONS)
        code, out = check()
        assert code == 0, out
        assert f"checked {total} of {total} patterns" in out, out

    def test_an_unknown_name_is_an_error(self, check):
        code, out = check("--only=no-such-mutation")
        assert code == 1, (
            f"check mode accepted a mutation name that does not exist: {out!r}"
        )

    def test_a_bare_only_is_an_error(self, check):
        code, out = check("--only")
        assert code == 1, (
            f"check mode accepted a --only with no name after it: {out!r}"
        )
