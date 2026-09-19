"""Only utils/notice_text.py may reach plyer.

wh-notice-length-guard, acceptance criterion A5. The guard in
utils/notice_text.py measures a notice against the fixed-size fields
plyer writes it into. A module that calls plyer itself walks around the
guard, and the failure it walks into is silent: plyer fills the
NOTIFYICONDATAW structure on a thread it starts itself, so the ValueError
ctypes raises never reaches WheelHouse and the user simply sees nothing.

A guard nothing is obliged to use decays the moment somebody adds a
twelfth notice. These tests are that obligation. They read the source
text rather than the imported modules, because the point is to catch a
new call the moment it is written, including one on a branch no test
exercises and one inside a function that never runs on this machine.

SCOPE. Every .py file under services/wheelhouse, minus five directory
names: .venv (installed packages, including plyer itself), tests (this
file quotes the forbidden text, and so do the mutation gates),
__pycache__, build and dist. SKIPPED_DIRECTORY_NAMES below holds those
same five names; this sentence and that set must agree.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# services/wheelhouse, three parents up from tests/test_utils/<this file>.
SERVICE_ROOT = Path(__file__).resolve().parents[2]

# The one module allowed to reach plyer, relative to SERVICE_ROOT.
THE_GUARD = Path("utils") / "notice_text.py"

SKIPPED_DIRECTORY_NAMES = {".venv", "tests", "__pycache__", "build", "dist"}

# The call itself. Written in two pieces so this line does not read as
# the very text it forbids when somebody greps for the call.
A_PLYER_NOTIFY_CALL = "notification" + ".notify("

# An import of plyer, at the start of a line so that prose in a
# docstring naming the library cannot match. Both forms, because
# `import plyer` followed by `plyer.notification.notify(...)` reaches the
# same place as `from plyer import notification`.
A_PLYER_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+plyer\b|import[ \t]+plyer\b)", re.MULTILINE)


def _source_files() -> list[Path]:
    """Every WheelHouse .py file the scan covers, as absolute paths."""
    found = []
    for path in SERVICE_ROOT.rglob("*.py"):
        relative = path.relative_to(SERVICE_ROOT)
        if SKIPPED_DIRECTORY_NAMES.intersection(relative.parts):
            continue
        found.append(path)
    return sorted(found)


def _offenders(count_in) -> list[str]:
    """Name each offending file and how many times it offends.

    ``count_in`` takes the file's text and returns how many times the
    forbidden thing appears in it. The count goes in the reported name
    so a failure states the size of the job, not only its shape: seven
    files held the eleven direct calls this guard was written for.
    """
    named = []
    for path in _source_files():
        relative = path.relative_to(SERVICE_ROOT)
        if relative == THE_GUARD:
            continue
        found = count_in(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            named.append(f"{relative.as_posix()} ({found})")
    return sorted(named)


class TestTheScanCanSeeTheCodeItJudges:
    """The scan is worthless if it reads nothing; prove it reads."""

    def test_the_scan_covers_a_large_part_of_the_service(self):
        assert len(_source_files()) > 100

    def test_the_guard_itself_is_inside_the_scanned_tree(self):
        assert (SERVICE_ROOT / THE_GUARD).is_file()

    def test_the_guard_is_excluded_from_the_offender_list_by_name(self):
        # Every other test here would also pass if the scan silently
        # covered nothing, so pin the one exclusion directly.
        assert THE_GUARD in {
            path.relative_to(SERVICE_ROOT) for path in _source_files()
        }


class TestOnlyTheGuardCallsPlyer:
    def test_no_other_module_calls_plyer_notify(self):
        offenders = _offenders(lambda text: text.count(A_PLYER_NOTIFY_CALL))
        assert offenders == [], (
            f"These modules call plyer directly and so walk around the "
            f"length guard: {offenders}. Send the notice through "
            f"utils.notice_text.send_notice instead."
        )

    def test_the_guard_itself_holds_the_call(self):
        # Without this the test above passes when the call has been
        # deleted everywhere and no notice is ever sent at all.
        text = (SERVICE_ROOT / THE_GUARD).read_text(encoding="utf-8")
        assert A_PLYER_NOTIFY_CALL in text

    def test_no_other_module_imports_plyer(self):
        # The stronger form: an alias import
        # (`from plyer import notification as n`, then `n.notify(...)`)
        # never produces the text the test above looks for.
        offenders = _offenders(lambda text: len(A_PLYER_IMPORT.findall(text)))
        assert offenders == [], (
            f"These modules import plyer. Only utils/notice_text.py may: "
            f"{offenders}."
        )

    def test_the_guard_itself_imports_plyer(self):
        text = (SERVICE_ROOT / THE_GUARD).read_text(encoding="utf-8")
        assert A_PLYER_IMPORT.search(text) is not None


class TestTheScanWouldReportARealOffender:
    """Feed the matcher a planted offender and watch it be named."""

    @pytest.fixture
    def a_planted_caller(self, tmp_path, monkeypatch):
        """A throwaway service tree holding one offending module."""
        (tmp_path / "utils").mkdir()
        (tmp_path / THE_GUARD).write_text(
            "from plyer import notification\n"
            "notification.notify(title='t', message='m')\n",
            encoding="utf-8",
        )
        (tmp_path / "sneaky.py").write_text(
            "from plyer import notification\n"
            "notification.notify(title='t', message='m')\n",
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "quotes_it.py").write_text(
            "from plyer import notification\n"
            "notification.notify(title='t', message='m')\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys.modules[__name__], "SERVICE_ROOT", tmp_path,
        )
        return tmp_path

    def test_a_planted_direct_call_is_named(self, a_planted_caller):
        assert _offenders(lambda text: text.count(A_PLYER_NOTIFY_CALL)) == [
            "sneaky.py (1)"
        ]

    def test_a_planted_import_is_named(self, a_planted_caller):
        assert _offenders(
            lambda text: len(A_PLYER_IMPORT.findall(text))
        ) == ["sneaky.py (1)"]

    def test_a_test_file_that_quotes_the_call_is_not_named(
        self, a_planted_caller
    ):
        # The tests directory has to be skipped: this very file, and the
        # mutation gate beside it, both contain the forbidden text.
        named = _offenders(lambda text: text.count(A_PLYER_NOTIFY_CALL))
        assert "tests/quotes_it.py (1)" not in named
