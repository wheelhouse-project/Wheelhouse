"""Opt-in pytest plugin: run the cyclic garbage collector at nearly every allocation of a Qt test.

Off by default. It is not a test file, so pytest never collects it, and it
loads only when a run names it with -p. It reproduces locally the heap
corruption that public CI test (wheelhouse) part 2 hit at gui.py:3678
(wh-ci-gui-heap-hunt), which a normal local run does not show. From
services/wheelhouse, with the _collect_garbage_before_qt_tests fixture in
tests/conftest.py removed:

    PYTHONPATH=tests .venv/Scripts/python.exe -m pytest -v --tb=short -p no:cacheprovider -p gc_stress_plugin tests/test_terminal_editor_window.py tests/test_ui_provider_switching.py tests/test_visibility_toggle_save_feedback.py

dies with 0xc0000374 or an access violation in
test_visibility_round_trip_uses_real_handler_and_saved_outcome[hide-saved],
at gui.py:3678. With the fixture in place, the same command passes.

Keep -v. Whether the process dies depends on small changes to the run.
Counts on Ikon with the fixture removed: the command above crashed 3 of 3
runs; the same command without -v passed 2 of 2; the 19 part-2 files that
use qapp passed 2 of 2 (one run with -v, one without). With the fixture in
place, the command above passed 3 of 3.

gc.set_threshold(1, 1, 1) holds from the setup of each test whose fixture
closure contains qapp until its teardown ends; the default thresholds come
back after, so the rest of a run keeps its normal speed.
"""

import gc

import pytest

_DEFAULT_THRESHOLD = gc.get_threshold()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if "qapp" in getattr(item, "fixturenames", ()):
        gc.set_threshold(1, 1, 1)


def pytest_runtest_logreport(report):
    if report.when == "teardown":
        gc.set_threshold(*_DEFAULT_THRESHOLD)
