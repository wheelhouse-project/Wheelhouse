"""Actual shared-runner boundaries, with finite native child processes."""

import ctypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[4]
    modules = []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sys.__stdout__)
        for name, path in (
            ("_stt_cleanup_runner", Path(__file__).with_name("mutation_gate_runner.py")),
            ("_stt_cleanup_owned", root / "scripts/codex/owned_process.py"),
        ):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            patch.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
            modules.append(module)
    gate, helper = modules
    # Before the fix these attributes are unused; both native red cases still
    # execute the original subprocess.run path, with only argv/timeout replaced.
    monkeypatch.setattr(gate, "ROOT", tmp_path, raising=False)
    monkeypatch.setattr(gate, "EVIDENCE_ROOT", tmp_path / "evidence", raising=False)
    monkeypatch.setattr(gate, "_owned_process", helper, raising=False)
    monkeypatch.setattr(gate, "RUN_TIMEOUT", 5, raising=False)
    monkeypatch.setattr(gate, "_clear_pycache", lambda service: None)
    return gate


def _alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87
        return False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        assert result in (0, 258)
        return result == 258
    finally:
        kernel.CloseHandle(handle)


def _mutation(runner):
    service = runner.ROOT / "services/wheelhouse"
    service.mkdir(parents=True)
    source = service / "source.py"
    source.write_bytes(b"VALUE = 1\r\nNEXT = 2\r\n")
    return dict(name="first", service=service, file=source,
                test_file="tests/test_fixture.py", old="VALUE = 1", new="VALUE = 0",
                expect=["test_value"])


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows process ownership")
@pytest.mark.parametrize("boundary", ["collection", "mutation"])
def test_hung_descendant_is_stopped_within_bound_before_restore(
    runner, monkeypatch, boundary,
):
    # Both the legacy launch and owned launch use the same native interpreter.
    # A virtualenv redirector adds unrelated startup processes to this fixture.
    monkeypatch.setattr(sys, "executable", sys._base_executable)
    mutation = _mutation(runner)
    source = mutation["file"]
    original = source.read_bytes()
    scripts = runner.ROOT / "scripts"
    scripts.mkdir()
    wrapper = scripts / "run_tests.py"
    marker = runner.ROOT / "child.pid"
    wrapper.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', "
        "'import time; time.sleep(15)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
        "print('fixture child started', flush=True)\ntime.sleep(15)\n",
        encoding="utf-8",
    )
    real_run = subprocess.run

    def finite_legacy_command(command, **kwargs):
        assert command[:3] == ["uv", "run", "pytest"]
        kwargs["timeout"] = 5
        return real_run([sys._base_executable, "-I", "-S", str(wrapper)], **kwargs)

    monkeypatch.setattr(subprocess, "run", finite_legacy_command)
    restored = []
    original_restore = runner._restore

    def observe_restore(*args):
        restored.append(_alive(int(marker.read_text())) if marker.exists() else None)
        return original_restore(*args)

    monkeypatch.setattr(runner, "_restore", observe_restore)
    if boundary == "mutation":
        monkeypatch.setattr(runner, "_collected_names", lambda *args: {"test_value"})
        real_pytest = runner._run_pytest
        calls = []

        def baseline_then_hang(*args):
            calls.append(True)
            if len(calls) == 1:
                return subprocess.CompletedProcess([], 0, "", "")
            return real_pytest(*args)

        monkeypatch.setattr(runner, "_run_pytest", baseline_then_hang)
    other = subprocess.Popen(
        [sys._base_executable, "-I", "-S", "-c", "import time; time.sleep(40)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        started = time.monotonic()
        if boundary == "collection":
            with pytest.raises(subprocess.TimeoutExpired):
                runner._collected_names(mutation["service"], mutation["test_file"])
        else:
            assert runner.run([mutation], argv=[]) == 1
        elapsed = time.monotonic() - started
        assert marker.exists(), "finite fixture never started its descendant"
        assert elapsed < 8, f"{boundary} waited {elapsed:.3f}s for inherited output pipes"
        assert source.read_bytes() == original
        assert not _alive(int(marker.read_text()))
        assert other.poll() is None
        assert restored == ([] if boundary == "collection" else [False])
        print(f"{boundary}: timeout/cleanup {elapsed:.3f}s, child exited before restoration")
    finally:
        other.kill()
        other.wait(timeout=5)


@pytest.mark.parametrize("outcome", ["exception", "result"])
def test_unconfirmed_cleanup_preserves_backups_and_stops_all_writes_and_next_run(
    runner, monkeypatch, outcome,
):
    first = _mutation(runner)
    second = dict(first, name="second", old="NEXT = 2", new="NEXT = 0")
    original = first["file"].read_bytes()
    calls = []

    def invoke(command, cwd, env, timeout_seconds, evidence_dir):
        calls.append(list(command))
        if "--collect-only" in command:
            result = subprocess.CompletedProcess(command, 0, "tests/x.py::test_value\n", "")
        elif len(calls) == 2:
            result = subprocess.CompletedProcess(command, 0, "", "")
        elif outcome == "exception":
            raise runner._owned_process.CleanupUnconfirmedError("synthetic missing exit proof")
        else:
            result = subprocess.CompletedProcess(command, 1, "FAILED tests/x.py::test_value\n", "")
            result.cleanup_confirmed = False
            return result
        result.cleanup_confirmed = True
        return result

    monkeypatch.setattr(runner._owned_process, "run_owned", invoke)
    writes_after_launch = []
    restore = runner._restore

    def observed_restore(*args):
        writes_after_launch.append("restore")
        return restore(*args)

    monkeypatch.setattr(runner, "_restore", observed_restore)
    monkeypatch.setattr(runner, "_clear_pycache",
                        lambda *args: writes_after_launch.append("cache") if len(calls) >= 3 else None)
    assert runner.run([first, second], argv=[]) == 1
    assert len(calls) == 3
    assert writes_after_launch == []
    assert first["file"].read_bytes() == original.replace(b"VALUE = 1", b"VALUE = 0")
    assert original in [p.read_bytes() for p in runner.EVIDENCE_ROOT.rglob("*.original.bin")]
    assert list(runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))
    # A new process would re-import the module. Reset only the in-memory flag:
    # the durable marker must still stop a fresh normal run before collection.
    runner._cleanup_confirmed = True
    assert runner.run([first, second], argv=[]) == 1
    assert len(calls) == 3


@pytest.mark.parametrize("collect", [False, True])
@pytest.mark.parametrize("suite, relative_service", [
    ("wheelhouse", "services/wheelhouse"),
    ("stt_providers/shared", "services/stt_providers/shared"),
    ("release", "scripts/release"),
])
def test_launch_keeps_targets_and_wrapper_policy_and_confirms_before_continuation(
    runner, monkeypatch, collect, suite, relative_service,
):
    mutation = _mutation(runner)
    mutation["service"] = runner.ROOT / relative_service
    mutation["service"].mkdir(parents=True, exist_ok=True)
    targets = ("tests/a.py", "tests/b.py::test_value")
    reports = []

    def invoke(command, cwd, env, timeout_seconds, evidence_dir):
        assert command[:4] == [sys.executable, str(runner.ROOT / "scripts/run_tests.py"),
                               "--service", suite]
        assert command[4:6] == list(targets)
        assert ("--collect-only" in command) is collect
        assert cwd == runner.ROOT and timeout_seconds == 5
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert (evidence_dir / "cleanup-pending.json").exists()
        assert runner._cleanup_confirmed is False
        reports.append(next(arg for arg in command if arg.startswith("--junitxml=")))
        result = subprocess.CompletedProcess(command, 0, "tests/a.py::test_value[param]\n", "")
        result.cleanup_confirmed = True
        return result

    monkeypatch.setattr(runner._owned_process, "run_owned", invoke)
    for _ in range(2):
        result = (runner._collected_names if collect else runner._run_pytest)(mutation["service"], targets)
        assert runner._cleanup_confirmed is True
        assert not list(runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))
        if collect:
            assert result == {"test_value"}
        else:
            assert result.returncode == 0
    assert reports[0] != reports[1]


def test_collection_error_with_printed_name_never_reaches_baseline_or_mutation(runner, monkeypatch):
    mutation = _mutation(runner)
    original = mutation["file"].read_bytes()
    calls = []

    def invoke(*args):
        calls.append(True)
        result = subprocess.CompletedProcess([], 2, "tests/a.py::test_value\n", "collection error")
        result.cleanup_confirmed = True
        return result

    monkeypatch.setattr(runner._owned_process, "run_owned", invoke)
    assert runner.run([mutation], argv=[]) == 1
    assert calls == [True]
    assert mutation["file"].read_bytes() == original


@pytest.mark.parametrize("problem_type", [KeyboardInterrupt, OSError])
def test_confirmed_exception_restores_before_propagation_or_error(runner, monkeypatch, problem_type):
    mutation = _mutation(runner)
    original = mutation["file"].read_bytes()
    calls = []

    def invoke(command, *args):
        calls.append(True)
        if len(calls) == 3:
            problem = problem_type("confirmed failure")
            problem.cleanup_confirmed = True
            raise problem
        result = subprocess.CompletedProcess([], 0, "tests/a.py::test_value\n", "")
        result.cleanup_confirmed = True
        return result

    monkeypatch.setattr(runner._owned_process, "run_owned", invoke)
    if problem_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt, match="confirmed failure"):
            runner.run([mutation], argv=[])
    else:
        assert runner.run([mutation], argv=[]) == 1
    assert calls == [True] * 3
    assert mutation["file"].read_bytes() == original
    assert runner._cleanup_confirmed is True
    assert not list(runner.EVIDENCE_ROOT.rglob("cleanup-pending.json"))


def test_check_only_does_not_create_evidence_or_load_or_launch_helper(runner, monkeypatch):
    mutation = _mutation(runner)
    original = mutation["file"].read_bytes()

    def refuse():
        raise AssertionError("check-only must not load helper")

    monkeypatch.setattr(runner, "_owned", refuse)
    assert runner.run([mutation], argv=["--check"]) == 0
    assert mutation["file"].read_bytes() == original
    assert not runner.EVIDENCE_ROOT.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows project wrapper")
def test_repository_wrapper_collection_returns_real_test_names(runner, monkeypatch):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "RUN_TIMEOUT", 60)
    # Collect only: no nested tests or source mutations, and the shared
    # service's own environment, which is the environment every real gate in
    # this directory names and the one this regression's outer run uses.
    # Collecting a file from this directory under the wheelhouse environment
    # cannot work at all: this directory's conftest imports shared_audio at
    # module level, and the wheelhouse service declares no dependency on it
    # (wh-test-release-2026-09.2.3).
    names = runner._collected_names(root / "services/stt_providers/shared",
                                    "tests/test_mutation_gate_runner.py")
    assert "test_the_restore_runs_when_the_interrupt_lands_at_its_door" in names
    assert "test_the_cache_clear_runs_when_the_interrupt_lands_at_its_door" in names
