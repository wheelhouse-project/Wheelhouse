"""Preserve the real shared cleanup dispatcher through the ownership interlock."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import mutation_gate_owned_guard as owned


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[2] / "stt_providers/shared/tests/mutation_gate_runner.py"
    spec = importlib.util.spec_from_file_location("_owned_guard_shared_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("interrupt_at", ["_restore", "_clear_pycache"])
def test_cleanup_entry_interrupt_retries_underlying_body_before_return(
    tmp_path, monkeypatch, runner, interrupt_at,
):
    target = tmp_path / "subject.py"
    original, mutant = b"value = 1\n", b"value = 2\n"
    target.write_bytes(original)
    guard = owned.MutationRun(tmp_path, [target])
    target.write_bytes(mutant)
    cleared = []

    def clear_cache(service):
        cleared.append(target.read_bytes())

    monkeypatch.setattr(runner, "_clear_pycache", clear_cache)
    cleanup_code = getattr(runner, interrupt_at).__code__
    interrupt = KeyboardInterrupt("at the underlying cleanup entry")
    entries = []

    def trace(frame, event, arg):
        if event == "call" and frame.f_code is cleanup_code:
            entries.append(frame.f_lineno)
            if len(entries) == 1:
                raise interrupt
        return trace

    previous = sys.gettrace()
    escaped = restore_deferred = cache_deferred = None
    restore_error = None
    with guard.protect(runner, ("_restore", "_clear_pycache")):
        try:
            sys.settrace(trace)
            try:
                restore_error, restore_deferred = runner._call_cleanup(
                    runner._restore, target, original, mutant, "probe")
            finally:
                _, cache_deferred = runner._call_cleanup(runner._clear_pycache, tmp_path)
        except KeyboardInterrupt as exc:
            escaped = exc
        finally:
            sys.settrace(previous)

    # A trace exception disables tracing, so the one recorded entry is the
    # strict interruption point; restored bytes and cache effects prove retry.
    assert entries == [cleanup_code.co_firstlineno]
    assert target.read_bytes() == original, "confirmed cleanup must restore after an entry interrupt"
    assert cleared == [original], "cache cleanup must run after source restoration"
    assert escaped is None, "entry interrupt must be deferred through the cleanup dispatcher"
    assert restore_error is None
    assert (restore_deferred if interrupt_at == "_restore" else cache_deferred) is interrupt


def test_started_cleanup_is_not_retried(runner, tmp_path):
    calls = []
    interrupt = KeyboardInterrupt("body already started")

    def cleanup():
        calls.append("body")
        raise interrupt

    module = SimpleNamespace(_restore=cleanup, _call_cleanup=runner._call_cleanup)
    guard = owned.MutationRun(tmp_path, [])
    with guard.protect(module, ("_restore",)):
        with pytest.raises(KeyboardInterrupt) as stopped:
            module._call_cleanup(module._restore)
    assert stopped.value is interrupt
    assert calls == ["body"], "completed or entered cleanup must not be repeated"


@pytest.mark.parametrize("through_dispatcher", [False, True])
def test_unknown_cleanup_blocks_direct_and_dispatched_work(runner, tmp_path, through_dispatcher):
    calls = []

    def cleanup():
        calls.append("body")

    module = SimpleNamespace(_restore=cleanup, _call_cleanup=runner._call_cleanup)
    guard = owned.MutationRun(tmp_path, [])
    uncertainty = owned.CleanupUnconfirmedError("owned lifetime exit not confirmed")
    guard.uncertain = uncertainty
    with guard.protect(module, ("_restore",)):
        with pytest.raises(owned.CleanupUnconfirmedError) as stopped:
            if through_dispatcher:
                module._call_cleanup(module._restore)
            else:
                module._restore()
    assert stopped.value is uncertainty
    assert not calls


def test_protect_restores_original_bindings_after_exception(runner, tmp_path):
    original_restore = runner._restore
    original_dispatcher = runner._call_cleanup
    guard = owned.MutationRun(tmp_path, [])
    with pytest.raises(RuntimeError, match="leave context"):
        with guard.protect(runner, ("_restore",)):
            raise RuntimeError("leave context")
    assert runner._restore is original_restore
    assert runner._call_cleanup is original_dispatcher


@pytest.mark.parametrize("interrupt_at", ["_restore", "_clear_pycache"])
def test_entry_interrupt_stops_real_loop_after_restoring_and_clearing(
    tmp_path, monkeypatch, runner, interrupt_at,
):
    target = tmp_path / "subject.py"
    original, mutant = b"value = 1\n", b"value = 2\n"
    target.write_bytes(original)
    guard = owned.MutationRun(tmp_path, [target])
    mutations = [dict(name=name, file=target, service=tmp_path, test_file="tests/probe.py",
                      old="value = 1", new="value = 2", expect=["test_probe"])
                 for name in ("first", "must-not-run")]
    calls, clears = [], []
    monkeypatch.setattr(runner, "_collected_names", lambda *a: {"test_probe"})

    def run_tests(*args):
        calls.append(target.read_bytes())
        return SimpleNamespace(returncode=0 if len(calls) == 1 else 1,
                               stdout="" if len(calls) == 1 else "FAILED tests/probe.py::test_probe - assert False\n",
                               stderr="")

    def clear_cache(*args):
        clears.append((len(calls), target.read_bytes()))

    monkeypatch.setattr(runner, "_run_pytest", run_tests)
    monkeypatch.setattr(runner, "_clear_pycache", clear_cache)
    cleanup_code = getattr(runner, interrupt_at).__code__
    interrupt = KeyboardInterrupt("entry after the first mutant completed")

    def trace(frame, event, arg):
        if event == "call" and frame.f_code is cleanup_code and len(calls) == 2:
            raise interrupt
        return trace

    previous = sys.gettrace()
    with guard.protect(runner, ("_restore", "_clear_pycache")):
        try:
            sys.settrace(trace)
            with pytest.raises(KeyboardInterrupt) as stopped:
                runner.run(mutations, [])
        finally:
            sys.settrace(previous)
    assert stopped.value is interrupt
    assert calls == [original, mutant], "the next mutation must not execute after interruption"
    assert target.read_bytes() == original
    assert clears[-1] == (2, original), "deferred interruption must follow restoration and cache cleanup"
