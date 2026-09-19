"""Run the real Input entrypoint and SHM loop without desktop side effects.

The spawn target installs boundary modules before importing input_proc; no
pytest monkeypatch or live UIActionHandler is inherited by the fresh child.
crewcut: this tests orchestration, not pynput/SendInput/clipboard behavior.
Those boundaries require separate desktop integration coverage.
"""

import multiprocessing
from multiprocessing import shared_memory
import os
from pathlib import Path
from queue import Empty
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest


def _spawn_input(shm_name, command, ready, responses, shutdown, reports,
                 fault_dir, fail_listener):
    """Only this child imports production after installing safe boundaries."""
    import builtins
    import faulthandler
    import traceback

    events = []
    attachments = []
    fault_files = []
    config = {"test_config": "spawn-owned"}

    def module(name, **attributes):
        replacement = ModuleType(name)
        replacement.__dict__.update(attributes)
        sys.modules[name] = replacement
        return replacement

    class Config:
        def __init__(self):
            events.append("config.create")

        def get_config(self):
            events.append("config.read")
            return config

    def setup_logging(received):
        assert received is config
        events.append("logging")

    class Listener:
        def __init__(self, **callbacks):
            self.kind = "mouse" if "on_click" in callbacks else "keyboard"
            assert set(callbacks) == {"on_click" if self.kind == "mouse" else "on_press"}
            assert all(callable(callback) for callback in callbacks.values())
            events.append(self.kind + ".create")

        def start(self):
            events.append(self.kind + ".start")
            if fail_listener and self.kind == "keyboard":
                raise RuntimeError("test listener startup failure")

        def stop(self):
            events.append(self.kind + ".stop")

        def join(self):
            events.append(self.kind + ".join")

    class Handler:
        def __init__(self, response_queue, received, *, shutdown_event):
            assert received is config
            assert response_queue is responses
            assert shutdown_event is shutdown
            events.append("handler")

        def start_utterance(self, text):
            events.append(("start_utterance", text))
            if text == "fail":
                raise ValueError("test action failure")

        def retract(self):
            events.append("retract")
            return {"status": "retracted", "count": 3}

        def channel_probe(self, request_id):
            events.append(("channel_probe", request_id))
            responses.put({"request_id": request_id, "status": "ok",
                           "action": "channel_probe", "path": "handler_owned"})

    class Ready:
        def set(self):
            events.append("ready")
            ready.set()

    class Attachment(shared_memory.SharedMemory):
        def __init__(self, *, name):
            super().__init__(name=name)
            attachments.append(self)
            events.append("shm.attach")

        def close(self):
            if self._buf is not None:
                events.append("shm.close")
            super().close()

    def crash_open(path, mode):
        assert Path(path).name == "wheelhouse_input_crash.log"
        assert mode == "a"
        handle = builtins.open(Path(fault_dir) / "crash.log", mode)
        fault_files.append(handle)
        return handle

    def fault_enable(*, file):
        assert file is fault_files[0]
        events.append("faulthandler")

    # Fail closed at imports: neither pynput nor the UI implementation is
    # imported. The loop and its validation/response helpers stay real.
    mouse = module("pynput.mouse", Listener=Listener)
    keyboard = module("pynput.keyboard", Listener=Listener)
    module("pynput", mouse=mouse, keyboard=keyboard)
    module("win32gui")
    module("win32process")
    module("psutil")
    module("services.wheelhouse.config_service", ConfigService=Config)
    module("utils.logging_setup", setup_logging=setup_logging)
    module("utils.process_priority",
           elevate_process_priority=lambda: events.append("priority"))
    module("ui.ui_actions", UIActionHandler=Handler)
    module("ui.clipboard", get_text_safe=lambda: (_ for _ in ()).throw(
        AssertionError("unexpected clipboard access")))
    faulthandler.enable = fault_enable

    import input_proc

    input_proc.open = crash_open
    input_proc.shared_memory = SimpleNamespace(SharedMemory=Attachment)
    error = None
    try:
        input_proc.input_process_main(
            shm_name, command, Ready(), responses, shutdown,
        )
    except BaseException:
        error = traceback.format_exc()
    finally:
        reports.put({
            "pid": os.getpid(), "events": events, "error": error,
            "shm_closed": bool(attachments) and all(
                attachment._buf is None for attachment in attachments),
            "fault_closed": bool(fault_files) and all(
                handle.closed for handle in fault_files),
        })
        # Test-owned fallback cleanup runs AFTER observing production cleanup.
        shutdown.set()
        for attachment in attachments:
            attachment.close()
        for handle in fault_files:
            handle.close()


class InputChild:
    def __init__(self, fault_dir, fail_listener=False):
        ctx = multiprocessing.get_context("spawn")
        self.shm = shared_memory.SharedMemory(create=True, size=65536)
        self.command = ctx.Event()
        self.ready = ctx.Event()
        self.shutdown = ctx.Event()
        self.responses = ctx.Queue()
        self.reports = ctx.Queue()
        self.process = ctx.Process(
            target=_spawn_input,
            args=(self.shm.name, self.command, self.ready, self.responses,
                  self.shutdown, self.reports, str(fault_dir), fail_listener),
        )

    def send(self, value):
        import pickle
        self.send_bytes(pickle.dumps(value))

    def send_bytes(self, payload):
        import struct
        deadline = time.monotonic() + 5
        while self.command.is_set():
            assert self.process.is_alive(), "Input child exited before reading frame"
            assert time.monotonic() < deadline, "Input child did not consume frame"
            time.sleep(0.005)
        assert len(payload) + 4 <= self.shm.size
        self.shm.buf[:4] = struct.pack(">I", len(payload))
        self.shm.buf[4:4 + len(payload)] = payload
        self.command.set()

    def request(self, action, params, request_id):
        self.send({"action": action, "params": params, "request_id": request_id})
        return self.responses.get(timeout=5)

    def finish(self):
        report = self.reports.get(timeout=10)
        self.process.join(timeout=5)
        assert not self.process.is_alive(), "Input child failed to exit"
        assert self.process.exitcode == 0
        assert report["pid"] != os.getpid()
        assert report["error"] is None, report["error"]
        assert report["shm_closed"], report
        assert report["fault_closed"], report
        assert report["events"][-5:] == [
            "mouse.stop", "mouse.join", "keyboard.stop", "keyboard.join", "shm.close",
        ]
        # The producer has exited and flushed its queue: this also detects a
        # duplicate generic response after a handler-owned or special reply.
        with pytest.raises(Empty):
            self.responses.get(timeout=0.1)
        return report["events"]

    def close(self):
        self.shutdown.set()
        try:
            if self.process.pid is not None:
                self.process.join(timeout=2)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=3)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=3)
                assert not self.process.is_alive(), "Could not reap Input child"
        finally:
            self.shm.close()
            self.shm.unlink()
            for queue in (self.responses, self.reports):
                queue.close()
                queue.join_thread()
            if self.process.pid is not None and not self.process.is_alive():
                self.process.close()


@pytest.fixture
def child_factory(tmp_path):
    children = []

    def start(*, fail_listener=False):
        child = InputChild(tmp_path, fail_listener)
        children.append(child)
        child.process.start()
        return child

    yield start
    for child in children:
        child.close()


@pytest.mark.timeout(60)
def test_spawn_initializes_before_ready_and_dispatches_real_frames(child_factory):
    """Catches premature readiness, wrong routes, and duplicate responses."""
    child = child_factory()
    assert child.ready.wait(timeout=10), "Input child never became ready"
    assert child.request("start_utterance", {"text": "hello"}, "generic") == {
        "request_id": "generic", "status": "ok", "path": "heuristic_done",
        "action": "start_utterance",
    }
    assert child.request("retract", {}, "special") == {
        "request_id": "special", "status": "retracted", "count": 3,
        "action": "retract",
    }
    assert child.request("channel_probe", {}, "owned") == {
        "request_id": "owned", "status": "ok", "path": "handler_owned",
        "action": "channel_probe",
    }
    child.send(None)
    events = child.finish()
    assert events[:12] == [
        "config.create", "config.read", "logging", "priority", "faulthandler",
        "shm.attach", "handler", "mouse.create", "keyboard.create",
        "mouse.start", "keyboard.start", "ready",
    ]
    assert events[12:-5] == [
        ("start_utterance", "hello"), "retract", ("channel_probe", "owned"),
    ]


@pytest.mark.timeout(60)
def test_spawn_recovers_from_bad_frames_and_handler_failure(child_factory):
    """Catches an outer-loop exit on bad IPC or a per-action exception."""
    child = child_factory()
    assert child.ready.wait(timeout=10)
    child.send_bytes(b"not a pickle")
    child.send(["not an envelope"])
    rejected = child.request("start_utterance", [], "invalid")
    assert rejected["request_id"] == "invalid"
    assert rejected["error"] is True
    assert rejected["action"] == "start_utterance"
    assert child.request("start_utterance", {"text": "fail"}, "failure") == {
        "request_id": "failure", "error": True, "message": "test action failure",
        "action": "start_utterance",
    }
    assert child.request("start_utterance", {"text": "recovered"}, "after")["status"] == "ok"
    child.shutdown.set()  # Also cover idle event shutdown without a SHM sentinel.
    events = child.finish()
    assert events[12:-5] == [
        ("start_utterance", "fail"), ("start_utterance", "recovered"),
    ]


@pytest.mark.timeout(60)
def test_spawn_startup_failure_cleans_resources_without_announcing_ready(child_factory):
    """Catches leaking SHM/listeners or announcing readiness after failed startup."""
    child = child_factory(fail_listener=True)
    events = child.finish()
    assert "keyboard.start" in events
    assert "ready" not in events
    assert not child.ready.is_set()
