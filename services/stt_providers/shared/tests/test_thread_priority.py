"""Tests for shared_audio.thread_priority -- Windows thread priority elevation.

The elevation exists so the audio capture and consumer threads keep getting
scheduled when the whole machine is saturated by bulk compute (the root cause
of the wh-stt-audio-consumer-behind-realtime overflow bursts). Tests run the
real Windows API round-trip in a scratch thread so the pytest main thread's
priority is never changed.
"""
import logging
import sys
import threading

import pytest

import shared_audio.thread_priority as thread_priority
from shared_audio.thread_priority import (
    elevate_current_thread,
    get_current_thread_priority,
)

_windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows thread priorities only"
)


def _run_in_thread(fn):
    """Run fn in a dedicated thread and return its result (or raise)."""
    result = {}

    def wrapper():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in caller
            result["error"] = e

    t = threading.Thread(target=wrapper)
    t.start()
    t.join(timeout=5.0)
    if "error" in result:
        raise result["error"]
    return result["value"]


@_windows_only
class TestElevateCurrentThread:
    def test_invalid_level_raises(self):
        with pytest.raises(ValueError):
            elevate_current_thread("supersonic")

    def test_elevate_highest_round_trip(self):
        def body():
            ok = elevate_current_thread("highest")
            return ok, get_current_thread_priority()

        ok, priority = _run_in_thread(body)
        assert ok is True
        assert priority == 2  # THREAD_PRIORITY_HIGHEST

    def test_elevate_time_critical_round_trip(self):
        def body():
            ok = elevate_current_thread("time_critical")
            return ok, get_current_thread_priority()

        ok, priority = _run_in_thread(body)
        assert ok is True
        assert priority == 15  # THREAD_PRIORITY_TIME_CRITICAL

    def test_default_thread_priority_is_normal(self):
        priority = _run_in_thread(get_current_thread_priority)
        assert priority == 0  # THREAD_PRIORITY_NORMAL


class _FakeKernel32:
    """Stand-in for the kernel32 handle so failure branches run anywhere."""

    def __init__(self, set_result=1, set_raises=None, get_result=0):
        self._set_result = set_result
        self._set_raises = set_raises
        self._get_result = get_result

    def GetCurrentThread(self):
        return 0x1234

    def SetThreadPriority(self, handle, level):
        if self._set_raises is not None:
            raise self._set_raises
        return self._set_result

    def GetThreadPriority(self, handle):
        return self._get_result


class TestFailurePaths:
    """The never-crash guarantee: OS failures return False/None, never raise.

    These fake the kernel32 handle, so they run on any platform and do not
    touch the real thread priority.
    """

    def test_set_priority_os_refusal_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_kernel32", _FakeKernel32(set_result=0)
        )
        assert elevate_current_thread("highest") is False

    def test_set_priority_exception_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority,
            "_kernel32",
            _FakeKernel32(set_raises=OSError("access violation")),
        )
        assert elevate_current_thread("time_critical") is False

    def test_get_priority_error_sentinel_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority, "_kernel32", _FakeKernel32(get_result=0x7FFFFFFF)
        )
        assert get_current_thread_priority() is None

    def test_no_kernel32_elevate_returns_false(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        assert elevate_current_thread("highest") is False

    def test_no_kernel32_get_priority_returns_none(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        assert get_current_thread_priority() is None

    def test_invalid_level_still_raises_with_no_kernel32(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        with pytest.raises(ValueError):
            elevate_current_thread("supersonic")


class _FakeProcessKernel32(_FakeKernel32):
    """Extends the fake with the process-class API surface."""

    def __init__(self, set_class_result=1, set_class_raises=None,
                 get_class_result=0x00000080, get_class_raises=None, **kw):
        super().__init__(**kw)
        self._set_class_result = set_class_result
        self._set_class_raises = set_class_raises
        self._get_class_result = get_class_result
        self._get_class_raises = get_class_raises

    def GetCurrentProcess(self):
        return 0x5678

    def GetPriorityClass(self, handle):
        if self._get_class_raises is not None:
            raise self._get_class_raises
        return self._get_class_result

    def SetPriorityClass(self, handle, priority_class):
        if self._set_class_raises is not None:
            raise self._set_class_raises
        return self._set_class_result


@_windows_only
class TestElevateCurrentProcess:
    def test_round_trip_sets_high_priority_class(self):
        """Real Windows round-trip: elevate, read back High (0x80), restore."""
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetPriorityClass.argtypes = (ctypes.c_void_p,)
        kernel32.GetPriorityClass.restype = ctypes.c_uint32
        kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.SetPriorityClass.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        original = kernel32.GetPriorityClass(handle)
        assert original != 0
        try:
            ok = thread_priority.elevate_current_process()
            assert ok is True
            assert kernel32.GetPriorityClass(handle) == 0x00000080
        finally:
            kernel32.SetPriorityClass(handle, original)


class TestElevateCurrentProcessFailurePaths:
    """OS failures return False, never raise -- same guarantee as threads."""

    def test_os_refusal_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority,
            "_kernel32",
            _FakeProcessKernel32(set_class_result=0),
        )
        assert thread_priority.elevate_current_process() is False

    def test_exception_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            thread_priority,
            "_kernel32",
            _FakeProcessKernel32(set_class_raises=OSError("denied")),
        )
        assert thread_priority.elevate_current_process() is False

    def test_no_kernel32_returns_false(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        assert thread_priority.elevate_current_process() is False


class TestProviderMainsElevate:
    """Every STT provider worker elevates its own process class at startup.

    Source-level guard tests: running a provider main would load a model.
    The Task Scheduler launch chain hands every provider a Below Normal
    process class (wh-process-priority-durable); deleting the elevation
    call from a provider makes the matching test fail.
    """

    PROVIDERS = [
        "distil_medium_en",
        "google_stt_server",
        "sherpa_offline_parakeet_stt_server",
    ]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_provider_main_elevates_process(self, provider):
        """The call must be a direct statement of the __main__ block.

        The __main__ block is the only code path a spawned provider worker
        executes, so a call relocated to module level, a helper, or a dead
        branch no longer runs at startup and must not count
        (wh-process-priority-durable.1.2).
        """
        import ast
        from pathlib import Path

        main_py = (
            Path(__file__).resolve().parents[2] / provider / "main.py"
        )
        source = main_py.read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = False
        for node in tree.body:
            if not (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Eq)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value == "__main__"
            ):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Expr) and isinstance(
                    stmt.value, ast.Call
                ):
                    fn = stmt.value.func
                    name = getattr(fn, "id", None) or getattr(
                        fn, "attr", None
                    )
                    if name == "elevate_current_process":
                        found = True
        assert found, (
            f"{provider}/main.py does not call elevate_current_process as a "
            f"direct statement of its __main__ block"
        )


class TestMutationGateRecovery:
    """The gate journal restores a mutant left by an abrupt termination.

    A reboot or kill while a mutant is on disk bypasses the finally-restore
    and the original bytes die with the process; the journal persists them
    so the next invocation can undo the mutant before its baseline runs
    (wh-process-priority-durable.1.8). Temp files only -- never live source.
    """

    def _load_gate(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "mutation_gate_thread_priority_module",
            str(Path(__file__).resolve().parent / "mutation_gate_process_priority.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_publish_durable_replaces_target_on_disk(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        tmp = tmp_path / "target.py.gate-tmp"
        target.write_bytes(b"old")
        tmp.write_bytes(b"new")
        gate._publish_durable(tmp, target)
        assert target.read_bytes() == b"new"
        assert not tmp.exists()

    def test_write_atomic_publishes_through_durable_rename(
        self, tmp_path, monkeypatch
    ):
        # os.replace alone is not power-loss durable on Windows (MoveFileExW
        # without MOVEFILE_WRITE_THROUGH), so every gate publish must go
        # through _publish_durable (wh-process-priority-durable.1.9).
        gate = self._load_gate()
        target = tmp_path / "target.py"
        calls = []

        def recorder(tmp, path):
            calls.append((tmp, path))
            import os

            os.replace(tmp, path)

        monkeypatch.setattr(gate, "_publish_durable", recorder)
        gate._write_atomic(target, b"payload")
        assert target.read_bytes() == b"payload"
        assert len(calls) == 1 and calls[0][1] == target

    def test_interrupted_run_is_recovered(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        original, mutant = b"x = 1\n", b"x = 2\n"
        target.write_bytes(original)
        gate._write_journal(target, original, mutant)
        gate._write_atomic(target, mutant)
        # Abrupt termination: no restore ran, the journal is still there.
        errors = gate._recover_pending([target])
        assert errors == []
        assert target.read_bytes() == original
        assert not gate._journal_path(target).exists()

    def test_stale_journal_with_restored_target_is_removed(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        original, mutant = b"x = 1\n", b"x = 2\n"
        target.write_bytes(original)
        gate._write_journal(target, original, mutant)
        # Crash landed between the journal write and the mutant write.
        errors = gate._recover_pending([target])
        assert errors == []
        assert target.read_bytes() == original
        assert not gate._journal_path(target).exists()

    def test_conflicting_target_is_reported_not_overwritten(self, tmp_path):
        gate = self._load_gate()
        target = tmp_path / "target.py"
        original, mutant = b"x = 1\n", b"x = 2\n"
        edit = b"x = 3  # post-crash edit\n"
        target.write_bytes(original)
        gate._write_journal(target, original, mutant)
        gate._write_atomic(target, mutant)
        target.write_bytes(edit)
        errors = gate._recover_pending([target])
        assert len(errors) == 1 and "reconcile" in errors[0]
        assert target.read_bytes() == edit
        assert gate._journal_path(target).exists()


class TestProcessPriorityClassReadback:
    """The class the MMCSS line reports beside the thread priority.

    The thread number alone does not say how a thread is scheduled: Windows
    folds the process class into the base priority, so priority 15 inside a
    Below Normal process sits under a Normal-class thread at 8. A Task
    Scheduler launch hands the whole tree Below Normal
    (wh-process-priority-durable), which is exactly the case a log line
    showing only the thread number cannot distinguish from a healthy one.
    """

    @_windows_only
    def test_it_reads_the_class_the_win32_api_reports(self):
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetPriorityClass.argtypes = (ctypes.c_void_p,)
        kernel32.GetPriorityClass.restype = ctypes.c_uint32
        expected = kernel32.GetPriorityClass(kernel32.GetCurrentProcess())
        assert expected != 0
        assert (thread_priority.get_current_process_priority_class()
                == expected)

    def test_no_kernel32_returns_none(self, monkeypatch):
        monkeypatch.setattr(thread_priority, "_kernel32", None)
        assert thread_priority.get_current_process_priority_class() is None

    def test_a_zero_return_is_reported_as_unavailable(self, monkeypatch):
        """GetPriorityClass answers 0 on failure, and 0 is not a class."""
        monkeypatch.setattr(
            thread_priority, "_kernel32",
            _FakeProcessKernel32(get_class_result=0))
        assert thread_priority.get_current_process_priority_class() is None

    def test_a_raise_is_reported_as_unavailable(self, monkeypatch):
        """Same guarantee as every other reader here: never raise into the
        audio path, report unavailable instead."""
        monkeypatch.setattr(
            thread_priority, "_kernel32",
            _FakeProcessKernel32(get_class_raises=OSError("boom")))
        assert thread_priority.get_current_process_priority_class() is None


class TestDescribePriorityClass:
    """The number alone is unreadable in a log; the name carries the point."""

    def test_a_known_class_is_named_and_numbered(self):
        assert thread_priority.describe_priority_class(
            thread_priority.HIGH_PRIORITY_CLASS) == "HIGH (0x80)"

    def test_below_normal_is_named_too(self):
        assert thread_priority.describe_priority_class(
            0x00004000) == "BELOW_NORMAL (0x4000)"

    def test_an_unreadable_class_says_so(self):
        assert thread_priority.describe_priority_class(None) == "unavailable"

    def test_an_unknown_value_keeps_its_number(self):
        """A class Windows adds later must still print something usable."""
        assert thread_priority.describe_priority_class(0x1234) == (
            "unknown (0x1234)")


class TestWhereTheFailureWarningsGo:
    """elevate_current_thread's two failure arms write a WARNING. Which
    thread creates that record is a caller's choice, because one caller is
    the PortAudio callback and a logging handler there can block on a
    handler lock (wh-audio-callback-log.2.5).

    These pin both halves. The default -- no sink -- must still write here
    and now, which is what every caller on an ordinary thread relies on.
    A sink must receive the finished line INSTEAD, not as well.
    """

    def test_the_default_writes_the_refusal_here_and_now(
            self, monkeypatch, caplog):
        monkeypatch.setattr(
            thread_priority, "_kernel32", _FakeKernel32(set_result=0))
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.thread_priority"):
            assert elevate_current_thread("highest") is False
        warnings = [r for r in caplog.records
                    if "SetThreadPriority" in r.message]
        assert len(warnings) == 1, [r.message for r in caplog.records]

    def test_the_default_writes_the_raised_error_here_and_now(
            self, monkeypatch, caplog):
        monkeypatch.setattr(
            thread_priority, "_kernel32",
            _FakeKernel32(set_raises=OSError("access violation")))
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.thread_priority"):
            assert elevate_current_thread("time_critical") is False
        warnings = [r for r in caplog.records
                    if "elevation unavailable" in r.message]
        assert len(warnings) == 1, [r.message for r in caplog.records]
        assert "access violation" in warnings[0].message

    def test_a_sink_receives_the_refusal_and_the_logger_does_not(
            self, monkeypatch, caplog):
        monkeypatch.setattr(
            thread_priority, "_kernel32", _FakeKernel32(set_result=0))
        sunk = []
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.thread_priority"):
            assert elevate_current_thread(
                "highest",
                log_sink=lambda *a: sunk.append(a)) is False
        assert caplog.records == []
        assert len(sunk) == 1
        logger, level, message = sunk[0]
        assert logger is thread_priority.logger
        assert level == logging.WARNING
        assert "SetThreadPriority" in message

    def test_a_sink_receives_the_raised_error_and_the_logger_does_not(
            self, monkeypatch, caplog):
        monkeypatch.setattr(
            thread_priority, "_kernel32",
            _FakeKernel32(set_raises=OSError("access violation")))
        sunk = []
        with caplog.at_level(logging.WARNING,
                             logger="shared_audio.thread_priority"):
            assert elevate_current_thread(
                "time_critical",
                log_sink=lambda *a: sunk.append(a)) is False
        assert caplog.records == []
        assert len(sunk) == 1
        assert "access violation" in sunk[0][2]
