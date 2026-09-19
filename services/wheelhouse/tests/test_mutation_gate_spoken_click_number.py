"""The spoken-click mutation gate must refuse a run it cannot judge.

wh-number-badge-problems.1.5 (codex round 5): the gate decided caught or
survived from FAILED records and the exit status alone. pytest reports an
ERROR record, not a FAILED record, when collection fails, when a fixture
raises, or when a test errors rather than fails. Such a run carries no
verdict about the mutation, yet the old code gave it one: a collection
error printed no FAILED line and read as SURVIVED, and an unrelated ERROR
beside a real catcher failure read as caught.

These tests drive ``run_defect`` with the shapes pytest actually prints.
"""

import importlib.util
from pathlib import Path

GATE_PATH = Path(__file__).resolve().parent / "mutation_gate_spoken_click_number.py"

_spec = importlib.util.spec_from_file_location(
    "mutation_gate_spoken_click_number", GATE_PATH
)
assert _spec is not None and _spec.loader is not None, GATE_PATH
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


class _Run:
    """The three fields of a CompletedProcess the gate reads."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


CATCHER = "tests/test_speech_processor_bare_number.py::TestX::test_a[digits-period]"

FAILED_ONLY = (
    f"{CATCHER} FAILED                                                  [ 50%]\n"
    "\n"
    "=========================== short test summary info ===========================\n"
    f"FAILED {CATCHER} - AssertionError: assert [] == ['click 74.']\n"
    "1 failed, 34 passed in 12.10s\n"
)

CLEAN_PASS = (
    f"{CATCHER} PASSED                                                  [ 50%]\n"
    "35 passed in 11.80s\n"
)


def test_an_ordinary_failure_run_still_yields_a_verdict():
    """The guard must not refuse the run the gate exists to read."""
    assert gate.run_defect(_Run(1, FAILED_ONLY)) is None


def test_a_clean_pass_still_yields_a_verdict():
    """A survivor is a real answer, so a passing run keeps its verdict."""
    assert gate.run_defect(_Run(0, CLEAN_PASS)) is None


def test_a_collection_error_is_an_error_not_a_survivor():
    """pytest exits 2 and prints no FAILED line, which read as SURVIVED."""
    stdout = (
        "=================================== ERRORS ====================================\n"
        "_______________ ERROR collecting tests/test_speech_processor_bare_number.py ____\n"
        "ImportError: cannot import name 'ClickCommandParser'\n"
        "=========================== short test summary info ===========================\n"
        "ERROR tests/test_speech_processor_bare_number.py\n"
        "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
    )
    assert gate.run_defect(_Run(2, stdout)) is not None


def test_an_unrelated_error_beside_the_catcher_is_an_error_not_a_catch():
    """The catcher failed, but something else errored, so the run is unusable."""
    stdout = FAILED_ONLY + (
        "tests/test_speech_processor_bare_number.py::TestY::test_z ERROR      [ 60%]\n"
        "ERROR tests/test_speech_processor_bare_number.py::TestY::test_z - OSError\n"
    )
    assert gate.run_defect(_Run(1, stdout)) is not None


def test_collecting_no_tests_is_an_error():
    """Exit 5 means nothing ran, which cannot prove a mutation."""
    assert gate.run_defect(_Run(5, "no tests ran in 0.30s\n")) is not None


def test_output_on_stderr_is_an_error():
    """A crash outside the test run leaves the FAILED records untrustworthy."""
    run = _Run(1, FAILED_ONLY, stderr="Fatal Python error: Segmentation fault\n")
    assert gate.run_defect(run) is not None


def test_a_suite_timeout_abort_is_an_error():
    """pytest-timeout kills the run before the summary, so no FAILED line prints."""
    stdout = "+++ Timeout +++\nstack dump\n"
    assert gate.run_defect(_Run(1, stdout)) is not None


def test_the_error_reason_names_the_errored_test():
    """The reason must say which record made the run unusable."""
    stdout = FAILED_ONLY + (
        "ERROR tests/test_speech_processor_bare_number.py::TestY::test_z - OSError\n"
    )
    assert "test_z" in gate.run_defect(_Run(1, stdout))
