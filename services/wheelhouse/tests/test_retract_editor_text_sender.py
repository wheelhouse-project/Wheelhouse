"""Tests for LogicController.retract_editor_text's send-side contract.

wh-editor-retract-ledger-authoritative: the sender must carry the
whole_utterance flag on the wire and must NOT early-return on a zero
chars_requested in whole-utterance mode (a fully-drifted mirror reads 0
while the editor ledger still holds the utterance's words).

The tests call the unbound method against a lightweight stub self --
LogicController's full construction is heavyweight and none of it is
exercised by this method beyond the four attributes stubbed here.
"""
import asyncio
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_stub(loop):
    """A stub LogicController self for retract_editor_text."""
    sent = []
    pending = {}

    class _Queue:
        def put_nowait(self, payload):
            sent.append(payload)
            # Resolve the registered future as the GUI would: echo the
            # request and report success.
            rid = payload["request_id"]
            fut = pending[rid]
            fut.set_result({
                "chars_requested": payload["chars_requested"],
                "chars_removed": payload["chars_requested"],
                "replay_chars": 0,
                "failure_reason": "",
            })

    class _Pending:
        def register(self, request_id, generation):
            fut = loop.create_future()
            pending[request_id] = fut
            return fut

        def pop(self, request_id):
            pending.pop(request_id, None)

    stub = SimpleNamespace(
        _editor_rebuild_fanout=SimpleNamespace(observed_generation=0),
        _retract_pending=_Pending(),
        state_manager=SimpleNamespace(state_to_gui_queue=_Queue()),
        _retract_timeout_s=1.0,
    )
    return stub, sent


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(chars_requested, whole_utterance):
    from main import LogicController

    async def _drive():
        loop = asyncio.get_running_loop()
        stub, sent = _make_stub(loop)
        await LogicController.retract_editor_text(
            stub,
            chars_requested=chars_requested,
            utterance_id="66",
            replay_text="final",
            whole_utterance=whole_utterance,
        )
        return sent

    return _run(_drive())


def test_counted_mode_zero_chars_sends_nothing():
    assert _call(0, whole_utterance=False) == []


def test_counted_mode_payload_carries_flag_false():
    sent = _call(5, whole_utterance=False)
    assert len(sent) == 1
    assert sent[0]["whole_utterance"] is False
    assert sent[0]["chars_requested"] == 5


def test_whole_utterance_zero_chars_still_sends():
    """The drift case: mirror 0, ledger authoritative -- the IPC must
    still go out."""
    sent = _call(0, whole_utterance=True)
    assert len(sent) == 1
    assert sent[0]["whole_utterance"] is True
    assert sent[0]["chars_requested"] == 0
    assert sent[0]["replay_text"] == "final"


def test_whole_utterance_negative_chars_clamped_to_zero():
    sent = _call(-3, whole_utterance=True)
    assert len(sent) == 1
    assert sent[0]["chars_requested"] == 0


def _call_with_echo(chars_requested, whole_utterance, echo_whole_utterance):
    """Drive the sender against a GUI stub that echoes a chosen
    whole_utterance value."""
    from main import LogicController

    async def _drive():
        loop = asyncio.get_running_loop()
        sent = []
        futures = {}

        class _Pending:
            def register(self, request_id, generation):
                fut = loop.create_future()
                futures[request_id] = fut
                return fut

            def pop(self, request_id):
                futures.pop(request_id, None)

        class _EchoQueue:
            def put_nowait(self, payload):
                sent.append(payload)
                futures[payload["request_id"]].set_result({
                    "chars_requested": payload["chars_requested"],
                    "chars_removed": payload["chars_requested"],
                    "replay_chars": 0,
                    "failure_reason": "",
                    "whole_utterance": echo_whole_utterance,
                })

        stub = SimpleNamespace(
            _editor_rebuild_fanout=SimpleNamespace(observed_generation=0),
            _retract_pending=_Pending(),
            state_manager=SimpleNamespace(state_to_gui_queue=_EchoQueue()),
            _retract_timeout_s=1.0,
        )
        await LogicController.retract_editor_text(
            stub,
            chars_requested=chars_requested,
            utterance_id="66",
            replay_text="final",
            whole_utterance=whole_utterance,
        )
        return sent

    return _run(_drive())


def test_whole_utterance_echo_mismatch_logged(caplog):
    """Reviewer_0 finding .1.2: the sender boundary-checks the
    whole_utterance echo the same way it checks chars_requested."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="main"):
        sent = _call_with_echo(5, whole_utterance=True, echo_whole_utterance=False)
    assert len(sent) == 1
    assert any(
        "whole_utterance mismatch" in r.getMessage() for r in caplog.records
    )


def test_whole_utterance_echo_match_no_warning(caplog):
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="main"):
        _call_with_echo(5, whole_utterance=True, echo_whole_utterance=True)
    assert not any(
        "whole_utterance" in r.getMessage() for r in caplog.records
    )


def _drive_result(
    response_builder, chars_requested=5, whole_utterance=True, timeout_s=1.0,
):
    """Run retract_editor_text against a GUI stub whose response payload
    comes from response_builder(payload); return the method's return
    value. A builder returning None leaves the future unresolved, which
    exercises the timeout path."""
    from main import LogicController

    async def _drive():
        loop = asyncio.get_running_loop()
        futures = {}

        class _Pending:
            def register(self, request_id, generation):
                fut = loop.create_future()
                futures[request_id] = fut
                return fut

            def pop(self, request_id):
                futures.pop(request_id, None)

        class _Queue:
            def put_nowait(self, payload):
                response = response_builder(payload)
                if response is not None:
                    futures[payload["request_id"]].set_result(response)

        stub = SimpleNamespace(
            _editor_rebuild_fanout=SimpleNamespace(observed_generation=0),
            _retract_pending=_Pending(),
            state_manager=SimpleNamespace(state_to_gui_queue=_Queue()),
            _retract_timeout_s=timeout_s,
        )
        return await LogicController.retract_editor_text(
            stub,
            chars_requested=chars_requested,
            utterance_id="66",
            replay_text="final",
            whole_utterance=whole_utterance,
        )

    return _run(_drive())


def _echo(payload, failure_reason="", **overrides):
    """A well-formed GUI response echoing the request."""
    response = {
        "chars_requested": payload["chars_requested"],
        "chars_removed": payload["chars_requested"],
        "replay_chars": 5,
        "failure_reason": failure_reason,
        "whole_utterance": payload["whole_utterance"],
    }
    response.update(overrides)
    return response


class TestReturnContract:
    """Codex round-2 finding wh-whole-utterance-command-matching.1.1:
    the sender must tell its caller whether the GUI confirmed the
    retract-and-replay. True only on a validated success (echo checks
    passed, empty failure_reason); False on every other outcome, so the
    caller can refuse to advance state the GUI never installed."""

    def test_success_returns_true(self):
        assert _drive_result(lambda p: _echo(p)) is True

    def test_declined_failure_returns_false(self):
        assert _drive_result(
            lambda p: _echo(p, failure_reason="replay_failed"),
        ) is False

    def test_stale_generation_returns_false(self):
        assert _drive_result(
            lambda p: _echo(p, failure_reason="stale_generation"),
        ) is False

    def test_underrun_returns_false(self):
        assert _drive_result(
            lambda p: _echo(p, failure_reason="ledger_underrun"),
        ) is False

    def test_timeout_returns_false(self):
        assert _drive_result(lambda p: None, timeout_s=0.05) is False

    def test_chars_echo_mismatch_returns_false(self):
        assert _drive_result(
            lambda p: _echo(p, chars_requested=p["chars_requested"] + 1),
        ) is False

    def test_whole_utterance_echo_mismatch_returns_false(self):
        assert _drive_result(
            lambda p: _echo(p, whole_utterance=not p["whole_utterance"]),
        ) is False

    def test_counted_zero_returns_false(self):
        """The counted-mode early return sends nothing and retracts
        nothing, so it must not read as a confirmed replay."""
        assert _drive_result(
            lambda p: _echo(p), chars_requested=0, whole_utterance=False,
        ) is False


def test_response_handler_passes_whole_utterance_to_future():
    """Reviewer_0 finding .1.2 (payload half): the response handler must
    include whole_utterance in the payload it completes the future with,
    or the sender's echo check can never see it."""
    from main import LogicController

    completed = {}

    class _Pending:
        def complete(self, request_id, payload):
            completed[request_id] = payload
            return True

    stub = SimpleNamespace(_retract_pending=_Pending())
    LogicController._handle_retract_editor_text_response(stub, {
        "action": "retract_editor_text_response",
        "request_id": "r" * 8,
        "chars_requested": 0,
        "chars_removed": 7,
        "replay_chars": 5,
        "failure_reason": "",
        "whole_utterance": True,
    })
    assert completed["r" * 8]["whole_utterance"] is True
