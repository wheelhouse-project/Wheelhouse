"""Native public-helper contract and mutation-caller restoration scenarios."""

import ctypes
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    """Wire the unchanged timer gate to the public helper as a test-only caller.

    This adapter is not shipped. It demonstrates the exact typed uncertainty
    catch callers need, while real inherited restore/stop logic runs on tmp_path.
    """
    directory = Path(__file__).resolve().parent
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sys.__stdout__)
        modules = []
        for name, path in (
            ("_owned_process_test_base_gate", directory / "mutation_gate_overlay_timer_state_check.py"),
            ("_owned_process_test_helper", directory.parents[2] / "scripts/codex/owned_process.py"),
        ):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            patch.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
            modules.append(module)
    gate, helper = modules
    helper.gate = gate
    helper.ROOT = directory.parents[2]
    helper.EVIDENCE = helper.ROOT / ".pytest_cache/owned-process-tests"
    helper._safe_to_restore = True
    original_restore = gate._restore

    def invoke(*extra):
        if not helper._safe_to_restore:
            raise helper.CleanupUnconfirmedError("Previous cleanup unconfirmed; refusing launch")
        try:
            result = helper.run_owned(
                # These tiny fixture scripts use only the standard library.
                # Avoid venv redirection and optional site initialization so
                # the native deadline exercises the payload and its children.
                [sys._base_executable, "-I", "-S",
                 str(helper.ROOT / "scripts/run_tests.py"), *extra],
                helper.ROOT, {**os.environ, "UV_NO_SYNC": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                gate.PER_MUTATION_TIMEOUT_S, helper.EVIDENCE,
            )
        except helper.CleanupUnconfirmedError as exc:
            assert exc.cleanup_confirmed is False
            helper._safe_to_restore = False
            raise
        except BaseException as exc:
            assert exc.cleanup_confirmed is True
            raise
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.cleanup_confirmed is True
        return result

    def restore(path, original):
        if not helper._safe_to_restore:
            print("process cleanup unconfirmed; source not restored")
            return False
        return original_restore(path, original)

    def main():
        helper.EVIDENCE.mkdir(parents=True, exist_ok=True)
        original = gate.SRC.read_bytes()
        (helper.EVIDENCE / "original-test-caller.py").write_bytes(original)
        with monkeypatch.context() as patch:
            patch.setattr(gate, "_pytest", helper._run_tests)
            patch.setattr(gate, "_restore", restore)
            patch.setattr(gate, "_clear_bytecode", lambda: None)
            return gate.main()

    helper._run_tests = invoke
    helper._restore_after_cleanup = restore
    helper.main = main
    return helper


def _alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87  # The process no longer exists.
        return False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        assert result in (0, 258)
        return result == 258
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_timeout_stops_descendant_before_restoring_source(
    runner, tmp_path, monkeypatch, parent_exits,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    marker = tmp_path / "child.pid"
    # Finite lifetimes keep the pre-fix regression bounded too. Both parent
    # shapes inherit output handles exactly like run_tests.py -> uv -> pytest.
    wrapper = scripts / "run_tests.py"
    wrapper.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', "
        "'import time; time.sleep(15)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
        "print('fixture descendant started', flush=True)\n"
        + ("" if parent_exits else "time.sleep(15)\n"),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 5)
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    monkeypatch.setattr(runner.gate, "_clear_bytecode", lambda: None)
    source = tmp_path / "source.py"
    source.write_bytes(b"original\n")
    original_restore = runner.gate._restore
    restored = []

    def observe_restore(path, original):
        restored.append(_alive(int(marker.read_text())) if marker.exists() else None)
        return original_restore(path, original)

    monkeypatch.setattr(runner.gate, "_restore", observe_restore)
    other = subprocess.Popen(
        [sys._base_executable, "-I", "-S", "-c", "import time; time.sleep(40)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Match the existing native exporter fixture: retry only if a busy
        # machine never starts the payload. Once the marker exists, a failed
        # timeout bound, output capture, or ownership assertion is never retried.
        for attempt in range(3):
            restored.clear()
            started = time.monotonic()
            result, error = runner.gate._apply_and_run(
                source, b"mutant\n", b"original\n", ["fixture"],
            )
            elapsed = time.monotonic() - started
            assert elapsed < 8, f"timeout cleanup waited {elapsed:.2f}s for inherited pipes"
            assert source.read_bytes() == b"original\n"
            assert other.poll() is None, "the gate stopped an unrelated process"
            assert result is None and error == "timed out"
            if not marker.exists():
                continue
            assert restored == [False], "restoration raced a still-running descendant"
            assert not _alive(int(marker.read_text()))
            logs = [p.read_text(encoding="utf-8") for p in runner.EVIDENCE.glob("*.log")]
            assert any("fixture descendant started" in output for output in logs)
            print(f"confirmed attempt {attempt + 1}: parent_exits={parent_exits}, "
                  f"timeout and cleanup {elapsed:.3f}s, child stopped before restoration")
            break
        else:
            pytest.fail("fixture never reached descendant creation in three bounded attempts")
    finally:
        other.kill()
        other.wait(timeout=5)


def _fixture_root(runner, tmp_path, monkeypatch, body):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_tests.py").write_text(body, encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(runner.gate, "_clear_bytecode", lambda: None)
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 5)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
def test_fixture_startup_ignores_optional_service_site_hooks(runner, tmp_path, monkeypatch):
    """Synthetic payload readiness must not depend on service site startup."""
    marker = tmp_path / "site-hook-started"
    startup = tmp_path / "optional-site"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text(
        "import pathlib, time\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(startup))
    _fixture_root(runner, tmp_path, monkeypatch, "print('fixture ready', flush=True)\n")
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    source = tmp_path / "source.py"
    result, error = runner.gate._apply_and_run(source, b"mutant\n", b"original\n", [])
    assert not marker.exists(), "synthetic fixture loaded optional service site hooks"
    assert error is None
    assert result.returncode == 0 and result.stdout.strip() == "fixture ready"
    assert source.read_bytes() == b"original\n"
    assert runner._safe_to_restore


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
@pytest.mark.parametrize("operation", ["create", "assign"])
def test_ownership_refusal_never_starts_payload_and_restores(
    runner, tmp_path, monkeypatch, operation,
):
    marker = tmp_path / "payload-started"
    _fixture_root(runner, tmp_path, monkeypatch,
                  f"from pathlib import Path; Path({str(marker)!r}).touch()\n")

    def refuse(*args):
        raise OSError(f"synthetic {operation} refusal")

    monkeypatch.setattr(runner._OwnedJob, "__init__" if operation == "create" else "assign", refuse)
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    source = tmp_path / "source.py"
    result, error = runner.gate._apply_and_run(source, b"mutant\n", b"original\n", [])
    assert result is None and f"synthetic {operation} refusal" in error
    assert not marker.exists()
    assert runner._safe_to_restore
    assert source.read_bytes() == b"original\n"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
@pytest.mark.parametrize("uncertainty", ["accounting", "new_member", "missing_member", "unsignaled_handle", "cleanup_interrupt"])
def test_unconfirmed_cleanup_is_bounded_and_stops_before_restore_or_next_mutation(
    runner, tmp_path, monkeypatch, capsys, uncertainty,
):
    # The handle-failure case needs a live owned process when cleanup starts.
    # Keep it finite as a backstop; the private Job ends it at the deadline.
    body = ("import time; time.sleep(30)\n" if uncertainty == "unsignaled_handle"
            else "print('fixture finished', flush=True)\n")
    _fixture_root(runner, tmp_path, monkeypatch, body)
    source = tmp_path / "source.py"
    original = b"alpha = 1\nomega = 2\n"
    source.write_bytes(original)
    monkeypatch.setattr(runner.gate, "SRC", source)
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 1.0 if uncertainty == "unsignaled_handle" else 0.2)
    monkeypatch.setattr(runner.gate, "collect_names", lambda: {"test_one"})
    monkeypatch.setattr(runner.gate, "_selected", lambda argv: [
        dict(name="first", old="alpha = 1", new="alpha = 0", expect=["test_one"], selection=[]),
        dict(name="second", old="omega = 2", new="omega = 0", expect=["test_one"], selection=[]),
    ])
    # A real private job owns/kills the finite fixture. Faults simulate each
    # missing proof of exit without leaving an unbounded synthetic process.
    handle_snapshots = []
    if uncertainty == "accounting":
        monkeypatch.setattr(runner._OwnedJob, "active", lambda self: 1)
    elif uncertainty == "missing_member":
        total = runner._job_total
        # A completed descendant no longer appears in the active PID list.
        # Stable lifetime accounting is insufficient without its exit proof.
        monkeypatch.setattr(runner, "_job_total", lambda job: total(job) + 1)
    elif uncertainty == "new_member":
        total = runner._job_total
        calls = []

        def changed_total(job):
            calls.append(True)
            return total(job) + (len(calls) > 1)

        monkeypatch.setattr(runner, "_job_total", changed_total)
    elif uncertainty == "unsignaled_handle":
        owned_handles = runner._owned_process_handles

        def unsignaled_handles(job, deadline):
            handles = owned_handles(job, deadline)
            handle_snapshots.append(len(handles))
            assert handles, "the gated launcher must be present in the snapshot"
            monkeypatch.setattr(job.dll, "WaitForSingleObject", lambda *args: 258)
            return handles

        monkeypatch.setattr(runner, "_owned_process_handles", unsignaled_handles)
    else:
        stop_and_wait = runner._stop_job_and_wait

        def interrupted_cleanup(job, deadline):
            stop_and_wait(job, deadline)
            raise KeyboardInterrupt("synthetic interrupt before cleanup confirmation")

        monkeypatch.setattr(runner, "_stop_job_and_wait", interrupted_cleanup)
    real_run = runner._run_tests
    runs = []

    def baseline_then_mutation(*args):
        runs.append(args)
        if len(runs) == 1:
            return subprocess.CompletedProcess([], 0, "", "")
        return real_run(*args)

    monkeypatch.setattr(runner, "_run_tests", baseline_then_mutation)
    cleanup_budgets = []
    cleanup_step = runner._stop_job_and_wait

    def observe_cleanup_budget(job, deadline):
        cleanup_budgets.append(deadline - time.monotonic())
        return cleanup_step(job, deadline)

    monkeypatch.setattr(runner, "_stop_job_and_wait", observe_cleanup_budget)
    assert runner.main() == 1
    if uncertainty == "unsignaled_handle":
        # Cleanup wraps assertion failures too; prove that this test reached
        # its intended nonempty handle wait, not an empty-snapshot assertion.
        assert handle_snapshots and all(handle_snapshots), (
            "cleanup uncertainty came from an empty snapshot, not the intended handle wait"
        )
    # Process creation and host scheduling are outside the cleanup budget.
    # Native timeout journeys check elapsed execution; the deterministic
    # tests below check that cleanup cannot renew or exceed its own deadline.
    assert len(cleanup_budgets) == 1
    assert cleanup_budgets[0] <= runner.CLEANUP_SECONDS
    assert len(runs) == 2, "a second mutation started after unconfirmed cleanup"
    assert source.read_bytes() == b"alpha = 0\nomega = 2\n", "restoration raced unconfirmed cleanup"
    assert not runner._safe_to_restore
    assert "source not restored" in capsys.readouterr().out
    assert [path.read_bytes() for path in runner.EVIDENCE.glob("original-*.py")] == [original]
    with pytest.raises(OSError, match="refusing launch"):
        real_run()


def test_cleanup_accounting_poll_stops_at_its_deadline(runner, monkeypatch):
    now = [0.0]
    sleeps = []
    terminated = []

    def advance(seconds):
        assert 0 < seconds <= 0.02
        sleeps.append(seconds)
        now[0] += seconds

    clock = SimpleNamespace(monotonic=lambda: now[0], sleep=advance)
    monkeypatch.setitem(runner._OwnedJob.stop.__globals__, "time", clock)
    job = SimpleNamespace(
        handle=7,
        dll=SimpleNamespace(TerminateJobObject=lambda *args: terminated.append(args) or 1),
        active=lambda: 1,
    )
    with pytest.raises(OSError, match="cleanup grace"):
        runner._OwnedJob.stop(job, 2.0)
    assert terminated == [(7, 1)]
    assert sleeps and now[0] == pytest.approx(2.0)


def test_cleanup_waits_for_retained_member_omitted_from_final_list(runner, monkeypatch):
    waits = []
    closed = []

    def empty_pid_list(job, kind, info, size, returned):
        assert kind == 3
        counts = ctypes.cast(info, ctypes.POINTER(ctypes.c_ulong))
        counts[0] = counts[1] = 0
        return 1

    def wait(handle, milliseconds):
        waits.append(handle)
        return 258

    kernel = SimpleNamespace(
        OpenProcess=lambda *args: pytest.fail("the exited PID is no longer enumerated"),
        IsProcessInJob=lambda *args: pytest.fail("membership was verified when retained"),
        QueryInformationJobObject=empty_pid_list,
        WaitForSingleObject=wait, CloseHandle=closed.append,
    )
    job = SimpleNamespace(handle=7, dll=kernel, process_handles={123: 22}, stop=lambda deadline: None)
    monkeypatch.setattr(runner, "_job_total", lambda job: 1)
    with pytest.raises(OSError, match="did not signal"):
        runner._stop_job_and_wait(job, time.monotonic() + 2)
    assert waits == [22], "final-list omission discarded the retained exit proof"
    assert closed == [22]


def test_cleanup_handle_waits_share_the_remaining_deadline(runner, monkeypatch):
    now = [0.0]
    waits = []
    closed = []
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(runner, "_job_total", lambda job: 2)

    def handles(job, deadline):
        assert deadline == 2.0
        now[0] = 0.25
        return [11, 22]

    def stop(deadline):
        assert deadline == 2.0
        now[0] = 0.5

    def wait(handle, milliseconds):
        waits.append((handle, milliseconds))
        if handle == 11:
            now[0] = 1.5
            return 0
        now[0] = 2.0
        return 258

    monkeypatch.setattr(runner, "_owned_process_handles", handles)
    job = SimpleNamespace(
        stop=stop,
        dll=SimpleNamespace(WaitForSingleObject=wait, CloseHandle=closed.append),
    )
    with pytest.raises(OSError, match="did not signal"):
        runner._stop_job_and_wait(job, 2.0)
    assert waits == [(11, 1500), (22, 500)]
    assert closed == [11, 22]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
def test_completed_wrapper_preserves_status_output_and_exact_restore(runner, tmp_path, monkeypatch):
    _fixture_root(runner, tmp_path, monkeypatch,
                  "import os, sys\nprint(os.environ['UV_NO_SYNC'])\n"
                  "print('stderr evidence', file=sys.stderr)\nsys.exit(7)\n")
    # This verifies successful completion, not the five-second timeout bound.
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 15)
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    source = tmp_path / "source.py"
    result, error = runner.gate._apply_and_run(source, b"mutant\n", b"original\r\n", [])
    assert error is None
    assert result.returncode == 7
    assert result.stdout.strip() == "1"
    assert result.stderr.strip() == "stderr evidence"
    assert source.read_bytes() == b"original\r\n"
    assert runner._safe_to_restore


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
def test_oversized_output_is_an_error_with_raw_evidence_and_exact_restore(runner, tmp_path, monkeypatch):
    _fixture_root(runner, tmp_path, monkeypatch, "print('x' * 32)\n")
    # Like the successful completion case, this exercises output validation.
    # The timeout journeys retain their separate five-second execution bound.
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 15)
    monkeypatch.setattr(runner, "OUTPUT_LIMIT", 16)
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    source = tmp_path / "source.py"
    result, error = runner.gate._apply_and_run(source, b"mutant\n", b"original\n", [])
    assert result is None and "full raw log retained, no verdict" in error
    assert source.read_bytes() == b"original\n"
    assert runner._safe_to_restore
    assert next(runner.EVIDENCE.glob("*.stdout.log")).read_bytes().strip() == b"x" * 32


def test_unsupported_ownership_platform_refuses_execution(runner, tmp_path, monkeypatch):
    _fixture_root(runner, tmp_path, monkeypatch, "raise AssertionError('must not launch')\n")
    monkeypatch.setattr(sys, "platform", "unsupported-fixture-platform")
    with pytest.raises(OSError, match="requires Windows Job Object ownership"):
        runner._run_tests()
    assert runner._safe_to_restore


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
def test_interruption_stops_descendant_before_source_restoration(runner, tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    _fixture_root(runner, tmp_path, monkeypatch,
                  "import pathlib, subprocess, sys, time\n"
                  "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', "
                  "'import time; time.sleep(15)'])\n"
                  f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
                  "time.sleep(15)\n")
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 10)
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    source = tmp_path / "source.py"
    sleep = time.sleep
    interrupted = False
    stop = runner._OwnedJob.stop

    def observe_stop(job, deadline):
        print(f"before Job stop: active={job.active()}")
        result = stop(job, deadline)
        print(f"after Job stop: active={job.active()}")
        return result

    monkeypatch.setattr(runner._OwnedJob, "stop", observe_stop)

    def interrupt_after_child_started(seconds):
        nonlocal interrupted
        if marker.exists() and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        sleep(seconds)

    def observe_restore(path, original):
        assert marker.exists(), "the fixture never reached descendant creation"
        alive = _alive(int(marker.read_text()))
        print(f"interruption restore: safe={runner._safe_to_restore}, child_alive={alive}")
        assert not alive
        assert runner._safe_to_restore
        return runner._restore_after_cleanup(path, original)

    monkeypatch.setattr(time, "sleep", interrupt_after_child_started)
    monkeypatch.setattr(runner.gate, "_restore", observe_restore)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        runner.gate._apply_and_run(source, b"mutant\n", b"original\n", [])
    assert time.monotonic() - started < 12
    assert source.read_bytes() == b"original\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows helper validates arguments after platform admission")
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_refuses_launch_with_confirmed_cleanup(runner, tmp_path, timeout):
    with pytest.raises(ValueError, match="positive and finite") as caught:
        runner.run_owned([sys.executable, "-c", "raise AssertionError('must not launch')"],
                         tmp_path, os.environ, timeout, tmp_path / "evidence")
    assert caught.value.cleanup_confirmed is True
    assert not (tmp_path / "evidence").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows descendant ownership")
def test_job_close_error_after_confirmed_exit_allows_exact_restore(runner, tmp_path, monkeypatch):
    _fixture_root(runner, tmp_path, monkeypatch, "print('finished')\n")
    close = runner._OwnedJob.close

    def close_then_fail(job):
        close(job)
        raise OSError("synthetic close-report error")

    monkeypatch.setattr(runner._OwnedJob, "close", close_then_fail)
    monkeypatch.setattr(runner.gate, "_pytest", runner._run_tests)
    source = tmp_path / "source.py"
    result, error = runner.gate._apply_and_run(source, b"mutant\n", b"original\n", [])
    assert result is None and "synthetic close-report error" in error
    assert runner._safe_to_restore
    assert source.read_bytes() == b"original\n"


@pytest.mark.parametrize("failure, execution", [
    ("missing_member", "finished"), ("missing_member", "timeout"),
    ("missing_member", "interrupt"), ("new_member", "finished"),
    ("unsignaled_handle", "finished"),
])
def test_cleanup_diagnostics_survive_close_and_caller_refuses_restoration(
    runner, tmp_path, monkeypatch, capsys, failure, execution,
):
    """Fake the kernel/launcher, retaining the real public and caller contracts."""
    now = [0.0]
    deadlines, waits, closed, caught, runs = [], [], [], [], []
    process = SimpleNamespace(stdin=io.BytesIO(), returncode=0,
                              poll=lambda: 0, wait=lambda timeout: 0,
                              kill=lambda: pytest.fail("must not kill a fake exited launcher"))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runner, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: pytest.fail("no polling needed"),
    ))
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner.subprocess, "CREATE_NO_WINDOW", 0, raising=False)

    def assign(process):
        if execution == "timeout":
            now[0] = 1.0

    def stop(deadline):
        deadlines.append(deadline)

    def wait(handle, milliseconds):
        waits.append((handle, milliseconds))
        return 258 if failure == "unsignaled_handle" else 0

    job = SimpleNamespace(
        process_handles={123: 22}, assign=assign, stop=stop, active=lambda: 0,
        dll=SimpleNamespace(WaitForSingleObject=wait, CloseHandle=closed.append),
    )
    job.close = lambda: job.process_handles.clear()
    monkeypatch.setattr(runner, "_OwnedJob", lambda: job)
    enumerations = []

    def observed_handles(job, deadline):
        enumerations.append(deadline)
        if execution == "interrupt" and len(enumerations) == 1:
            raise KeyboardInterrupt("synthetic execution interruption")
        return list(job.process_handles.values())

    monkeypatch.setattr(runner, "_owned_process_handles", observed_handles)
    totals = iter([2, 2] if failure == "missing_member" else [1, 2])
    monkeypatch.setattr(runner, "_job_total", lambda job: next(totals))
    _fixture_root(runner, tmp_path, monkeypatch, "raise AssertionError('fake launcher only')\n")
    monkeypatch.setattr(runner.gate, "PER_MUTATION_TIMEOUT_S", 1)
    source = tmp_path / "source.py"
    original = b"alpha = 1\nomega = 2\n"
    source.write_bytes(original)
    monkeypatch.setattr(runner.gate, "SRC", source)
    monkeypatch.setattr(runner.gate, "collect_names", lambda: {"test_one"})
    monkeypatch.setattr(runner.gate, "_selected", lambda argv: [
        dict(name="first", old="alpha = 1", new="alpha = 0", expect=["test_one"], selection=[]),
        dict(name="second", old="omega = 2", new="omega = 0", expect=["test_one"], selection=[]),
    ])
    real_run = runner._run_tests

    def baseline_then_mutation(*args):
        runs.append(args)
        if len(runs) == 1:
            return subprocess.CompletedProcess([], 0, "", "")
        try:
            return real_run(*args)
        except runner.CleanupUnconfirmedError as exc:
            caught.append(exc)
            raise

    monkeypatch.setattr(runner, "_run_tests", baseline_then_mutation)
    assert runner.main() == 1
    assert len(runs) == 2, "second mutation started after unconfirmed cleanup"
    assert source.read_bytes() == b"alpha = 0\nomega = 2\n"
    assert [p.read_bytes() for p in runner.EVIDENCE.glob("original-*.py")] == [original]
    assert not runner._safe_to_restore
    with pytest.raises(runner.CleanupUnconfirmedError, match="refusing launch"):
        real_run()
    assert deadlines == [now[0] + runner.CLEANUP_SECONDS]
    assert waits == [(22, 2000)]
    assert closed == [22] and job.process_handles == {}
    assert len(caught) == 1 and caught[0].cleanup_confirmed is False
    evidence = caught[0].cleanup_diagnostics
    assert evidence["timed_out"] is (execution == "timeout")
    assert evidence["pending_exception_type"] == ("KeyboardInterrupt" if execution == "interrupt" else None)
    job_evidence = evidence["cleanup_failures"][0]["job"]
    assert job_evidence["initial_lifetime_total"] == (2 if failure == "missing_member" else 1)
    assert job_evidence["final_lifetime_total"] == (None if failure == "unsignaled_handle" else 2)
    assert job_evidence["observed_handle_count"] == 1
    assert job_evidence["observed_process_ids"] == [123]
    assert job_evidence["wait_results"] == ([258] if failure == "unsignaled_handle" else [0])
    assert job_evidence["omitted_process_ids"] == job_evidence["omitted_wait_results"] == 0
    reason = {"missing_member": "handle was not observed", "new_member": "membership grew",
              "unsignaled_handle": "did not signal"}[failure]
    assert reason in str(caught[0])
    output = capsys.readouterr().out
    assert "source not restored" in output
    assert "initial_lifetime_total" in str(caught[0]), "string-only evidence lost diagnostics"


def test_cleanup_diagnostics_bound_reported_members_without_shortening_waits(runner, monkeypatch):
    closed, waited = [], []
    handles = {pid: pid + 1000 for pid in range(1, 71)}
    job = SimpleNamespace(
        process_handles=handles, stop=lambda deadline: None,
        dll=SimpleNamespace(WaitForSingleObject=lambda handle, ms: waited.append(handle) or 0,
                            CloseHandle=closed.append),
    )
    monkeypatch.setattr(runner, "_job_total", lambda job: 71)
    monkeypatch.setattr(runner, "_owned_process_handles", lambda job, deadline: list(handles.values()))
    with pytest.raises(OSError, match="handle was not observed") as caught:
        runner._stop_job_and_wait(job, time.monotonic() + 2)
    assert waited == closed == list(range(1001, 1071)), "diagnostic limit changed exit proof"
    assert job.process_handles == {}
    evidence = caught.value.cleanup_diagnostics
    assert evidence["observed_handle_count"] == 70
    assert evidence["observed_process_ids"] == list(range(1, 65))
    assert evidence["wait_results"] == [0] * 64
    assert evidence["omitted_process_ids"] == evidence["omitted_wait_results"] == 6


def test_cleanup_complete_observation_still_waits_and_returns(runner, monkeypatch):
    waited, closed = [], []
    job = SimpleNamespace(
        process_handles={123: 11, 456: 22}, stop=lambda deadline: None,
        dll=SimpleNamespace(WaitForSingleObject=lambda handle, ms: waited.append(handle) or 0,
                            CloseHandle=closed.append),
    )
    monkeypatch.setattr(runner, "_job_total", lambda job: 2)
    monkeypatch.setattr(runner, "_owned_process_handles", lambda job, deadline: list(job.process_handles.values()))
    assert runner._stop_job_and_wait(job, time.monotonic() + 2) is None
    assert waited == closed == [11, 22]
    assert job.process_handles == {}
