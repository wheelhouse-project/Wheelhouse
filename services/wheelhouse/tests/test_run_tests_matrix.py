"""The test runner has to be able to reach a suite that is not under services/.

wh-ci-release-tests: scripts/run_tests.py resolved every --service name as
REPO_ROOT/services/<name>, so the 1,786 tests under scripts/release/tests --
the helpdoc CI, the size budget, the command generator -- could not be run
through the runner at all, and published-doc defects shipped because of it.

The runner lives at the repository root, outside every service, so it has no
suite of its own. It is tested from here, loaded through importlib, the same
way services/wheelhouse/tests/test_codex_startup_wrapper.py already tests
scripts/codex/start_wheelhouse_codex.py.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_tests.py"


def _load_runner():
    """Import scripts/run_tests.py as a module without running it."""
    spec = importlib.util.spec_from_file_location("wheelhouse_run_tests", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class TestTheRunnerResolvesASuiteName:
    """Every name the runner accepts has to land on the directory that owns it."""

    def test_release_resolves_to_the_scripts_directory_that_holds_it(self) -> None:
        assert runner.resolve_suite_dir("release") == REPO_ROOT / "scripts" / "release"

    def test_the_resolved_release_directory_really_holds_the_tests(self) -> None:
        # The point of the entry is reachability, so the path it produces has
        # to be the one with the suite in it, not merely a well-formed path.
        resolved = runner.resolve_suite_dir("release")
        if not resolved.is_dir():
            pytest.skip("development-only scripts/release suite is absent from this checkout")
        assert (resolved / "pyproject.toml").exists()
        assert (resolved / "tests").is_dir()

    def test_a_service_name_still_resolves_under_services(self) -> None:
        # The fallback is what keeps every existing invocation working.
        assert runner.resolve_suite_dir("wheelhouse") == REPO_ROOT / "services" / "wheelhouse"
        assert runner.resolve_suite_dir("installer") == REPO_ROOT / "services" / "installer"

    def test_a_nested_service_name_still_resolves_under_services(self) -> None:
        assert runner.resolve_suite_dir("stt_providers/shared") == (
            REPO_ROOT / "services" / "stt_providers" / "shared"
        )


class TestTheRunnerNamesWhatItCanRun:
    """An unknown name has to tell the reader what the known names are."""

    def test_an_unknown_name_exits_non_zero_and_lists_release(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["run_tests.py", "--service", "no-such-suite"])

        with pytest.raises(SystemExit) as exit_info:
            runner.main()

        assert exit_info.value.code == 1
        printed = capsys.readouterr().out
        assert "no-such-suite" in printed
        assert "release" in printed
        assert "wheelhouse" in printed


class TestAFailingSuiteFailsTheRun:
    """pytest's exit code has to reach the caller, or a red suite reads green."""

    @pytest.fixture(autouse=True)
    def isolated_release_suite(self, tmp_path, monkeypatch):
        # Runner dispatch and exit propagation need a suite directory, not
        # the private release tooling. Exercise them in every checkout.
        (tmp_path / "scripts" / "release").mkdir(parents=True)
        (tmp_path / "scripts" / "release" / "pyproject.toml").write_text(
            '[project]\nname="test-suite"\nversion="0.0.0"\n', encoding="utf-8"
        )
        monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)

    def test_a_failing_release_run_exits_with_pytests_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _record(cmd, cwd=None, **kwargs):
            # Bind the arguments instead of answering the same way whatever is
            # passed: a stub that ignores cwd would pass even if the runner
            # dispatched to the wrong directory.
            seen["cmd"] = list(cmd)
            seen["cwd"] = cwd
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        monkeypatch.setattr(runner.subprocess, "run", _record)
        monkeypatch.setattr(runner, "parse_results", lambda _path: None)
        monkeypatch.setattr(sys, "argv", ["run_tests.py", "--service", "release"])

        with pytest.raises(SystemExit) as exit_info:
            runner.main()

        assert exit_info.value.code == 1
        assert seen["cwd"] == str(runner.REPO_ROOT / "scripts" / "release")
        assert seen["cmd"][:3] == ["uv", "run", "pytest"]

    def test_a_passing_release_run_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _pass(cmd, cwd=None, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(runner.subprocess, "run", _pass)
        monkeypatch.setattr(runner, "parse_results", lambda _path: None)
        monkeypatch.setattr(sys, "argv", ["run_tests.py", "--service", "release"])

        with pytest.raises(SystemExit) as exit_info:
            runner.main()

        assert exit_info.value.code == 0
