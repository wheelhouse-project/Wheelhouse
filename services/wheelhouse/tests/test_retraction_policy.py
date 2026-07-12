"""Phase 2 regression tests for the three-mode Google STT retraction policy.

Implements wh-zu7w3 (test fixtures) per the synthesized design from
wh-76yv (closed adversarial review epic).

Test 1 from the original plan -- the SpeechProcessor + fake-app integration
test for Mode 1 IPC ordering -- is intentionally NOT in this file. It needs
larger fixture infrastructure than the WebSocketManager-only tests below
and lives in test_retraction_policy_mode1_integration.py (wh-sqa8). The
Mode 1 queueing behavior at the WebSocketManager boundary IS covered by
`test_mode1_queues_lifecycle_reset_marker`.

Test coverage in this file:
- test_mode1_queues_lifecycle_reset_marker: Trace 1 shape -- no eos, fallback
  final_reason. Asserts the WordEvent stream contains a lifecycle reset
  marker between phrase 1 and phrase 2 with no retraction marker.
- test_mode2_post_eos_final_disagrees: Trace 2 shape -- eos, then disagreeing
  GOOGLE_FINAL. Asserts no retraction marker, end_marker only, warning logged.
- test_mode3_pre_eos_stable_disagreement: Trace 3 shape -- pre-eos stable
  disagreement, then disagreeing final. Asserts retraction marker queued.
- test_older_server_no_eos_no_final_reason: backward compat -- no eos, no
  final_reason, no capabilities. Asserts conservative Mode 3 with the
  EOS_NOT_RECEIVED warning SUPPRESSED (wh-nvyh: an older server never
  declares emits_eos, so the diagnostic stays at the silent default).
- test_ambiguous_google_final_no_eos: GOOGLE_FINAL with no eos and no stable
  disagreement. Asserts conservative Mode 3 + AMBIGUOUS_NO_EOS warning.
- test_race_eos_before_first_stable: eos arrives before any stable for an
  utterance. Asserts EOS state survives the new-utterance reset.
- test_stream_diagnostic_resets_between_clients: EOS_NOT_RECEIVED warning
  re-fires on a fresh stream after a client disconnect.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from speech.word_event import WordEvent


class FakeWebsocket:
    """Minimal websocket stand-in for WebSocketManager.handle_connection.

    Yields each pre-loaded message string when iterated, records every send,
    and exposes a remote_address tuple so the manager's logger does not raise.
    """

    def __init__(self, messages: List[str]) -> None:
        self._messages = list(messages)
        self.sent: List[str] = []
        self.remote_address = ("127.0.0.1", 0)

    def __aiter__(self) -> "FakeWebsocket":
        self._iter = iter(self._messages)
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def make_message(msg_type: str, **fields: Any) -> str:
    """Build a JSON message string for the fake websocket.

    Mirrors the payload shape WSForwarder serializes on the wire.
    """
    payload = {"type": msg_type}
    payload.update(fields)
    return json.dumps(payload)


def stable_message(text: str, utterance_id: int, trace_id: str = "T-1") -> str:
    return make_message(
        "stable", text=text, utterance_id=utterance_id,
        is_partial=True, trace_id=trace_id,
    )


def final_message(
    text: str,
    utterance_id: int,
    final_reason: Optional[str] = None,
    trace_id: str = "T-1",
) -> str:
    fields: dict = {
        "text": text,
        "utterance_id": utterance_id,
        "is_partial": False,
        "trace_id": trace_id,
    }
    if final_reason is not None:
        fields["final_reason"] = final_reason
    return make_message("final", **fields)


def eos_message(utterance_id: int, trace_id: str = "T-1") -> str:
    return make_message("eos", utterance_id=utterance_id, trace_id=trace_id)


def make_manager(event_loop: asyncio.AbstractEventLoop):
    """Build a WebSocketManager wired with mocked dependencies."""
    from integrations.websocket_manager import WebSocketManager

    manager = WebSocketManager(loop=event_loop)
    manager.state_manager = MagicMock()
    manager.state_manager.config_service = MagicMock()
    manager.state_manager.config_service.get = MagicMock(return_value=False)
    manager.state_manager._get_current_stt_provider = MagicMock(return_value="google_stt")
    manager._app = AsyncMock()
    return manager


async def drain_word_queue(manager) -> List[WordEvent]:
    """Pull every WordEvent currently on the manager's word_queue."""
    events: List[WordEvent] = []
    while not manager.word_queue.empty():
        events.append(await manager.word_queue.get())
    return events


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def manager(event_loop):
    return make_manager(event_loop)


# ===========================================================================
# Mode 1: server-side fallback finalization treats new content as fresh
# ===========================================================================

class TestMode1FallbackFinalAsFreshContent:
    """Trace 1 (UTT-67) shape: no eos, fallback final_reason."""

    @pytest.mark.asyncio
    async def test_mode1_queues_lifecycle_reset_marker(self, manager):
        """Stable then disagreeing fallback final should queue a lifecycle reset.

        WordEvent stream should be:
          phrase 1 stable words (start_of_utterance=True on first word)
          lifecycle_reset_marker
          phrase 2 final words (start_of_utterance=True on first word)
          end_marker
        And NOT a retraction marker.
        """
        utt = 67
        ws = FakeWebsocket([
            stable_message("this should not be P1", utt),
            final_message(
                "and it should be one of the children of",
                utt,
                final_reason="GOOGLE_SILENCE_2S",
            ),
        ])

        await manager.handle_connection(ws)

        events = await drain_word_queue(manager)

        # Phrase 1 stable: 5 words.
        phrase1 = ["this", "should", "not", "be", "P1"]
        assert [e.word for e in events[:5]] == phrase1
        assert events[0].start_of_utterance is True
        for e in events[1:5]:
            assert e.start_of_utterance is False

        # Lifecycle reset marker between phrases.
        reset = events[5]
        assert reset.is_lifecycle_reset_marker is True
        assert reset.utterance_id == utt
        assert reset.is_retraction_marker is False

        # Phrase 2 final: 9 words. First should have start_of_utterance=True.
        phrase2 = ["and", "it", "should", "be", "one", "of", "the", "children", "of"]
        assert [e.word for e in events[6:15]] == phrase2
        assert events[6].start_of_utterance is True

        # Final end_marker.
        assert events[-1].is_utterance_end_marker is True
        assert events[-1].utterance_id == utt

        # No retraction marker anywhere.
        assert not any(e.is_retraction_marker for e in events)


# ===========================================================================
# Mode 2: post-eos disagreement trusts the stable transcript
# ===========================================================================

class TestMode2PostEosFinalDisagrees:
    """Trace 2 (UTT-533) shape: eos arrives, then disagreeing GOOGLE_FINAL."""

    @pytest.mark.asyncio
    async def test_mode2_no_retraction_only_end_marker(self, manager, caplog):
        utt = 533
        ws = FakeWebsocket([
            stable_message("hello world", utt),
            eos_message(utt),
            final_message(
                "completely different text",
                utt,
                final_reason="GOOGLE_FINAL",
            ),
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)

        events = await drain_word_queue(manager)

        # Phrase 1 stable: 2 words.
        assert [e.word for e in events[:2]] == ["hello", "world"]
        # No retraction marker -- Mode 2 keeps the stable transcript.
        assert not any(e.is_retraction_marker for e in events)
        # Last event is the end_marker.
        assert events[-1].is_utterance_end_marker is True
        assert events[-1].utterance_id == utt
        # No lifecycle reset marker -- Mode 2 is not a fresh-content append.
        assert not any(e.is_lifecycle_reset_marker for e in events)

        # POST_EOS_FINAL_DROPPED log entry should be present.
        assert any("POST_EOS_FINAL_DROPPED" in rec.message for rec in caplog.records)


# ===========================================================================
# Mode 3: pre-eos stable disagreement keeps the current retract+replay
# ===========================================================================

class TestMode3PreEosStableDisagreement:
    """Trace 3 (UTT-609) shape: stable disagrees pre-eos, final disagrees post-eos."""

    @pytest.mark.asyncio
    async def test_mode3_retraction_marker_queued(self, manager):
        utt = 609
        ws = FakeWebsocket([
            stable_message("at the moment", utt),
            stable_message("only for Mac OS", utt),  # Stable disagreement.
            eos_message(utt),
            final_message(
                "only for Mac OS at the moment",
                utt,
                final_reason="GOOGLE_FINAL",
            ),
        ])

        await manager.handle_connection(ws)

        events = await drain_word_queue(manager)

        # Phrase 1 stable: 3 words from "at the moment".
        assert [e.word for e in events[:3]] == ["at", "the", "moment"]

        # Retraction marker for the disagreeing final.
        retraction = next(e for e in events if e.is_retraction_marker)
        assert retraction.retraction_full_text == "only for Mac OS at the moment"
        assert retraction.utterance_id == utt

        # End marker after retraction.
        assert events[-1].is_utterance_end_marker is True


# ===========================================================================
# Backward compatibility: older server with no eos and no final_reason
# ===========================================================================

class TestOlderServerBackwardCompat:
    """Conservative-default behavior when the STT server lacks the new protocol."""

    @pytest.mark.asyncio
    async def test_older_server_no_eos_no_final_reason(self, manager, caplog):
        """No eos messages and no final_reason field -> conservative Mode 3.

        wh-nvyh: an older server also sends no capabilities message, so the
        EOS_NOT_RECEIVED diagnostic stays silent (the safe default) -- the
        bead explicitly accepts losing the warning for pre-handshake Google
        servers because it is a diagnostic, not a correctness signal. The
        conservative Mode 3 retraction behavior is unchanged.
        """
        ws = FakeWebsocket([
            stable_message("first attempt", 1),
            final_message("totally different", 1),  # No final_reason kwarg.
            stable_message("hello world", 2),
            final_message("goodbye now", 2),  # No final_reason kwarg.
            stable_message("more text", 3),
            final_message("revised text", 3),  # Disagrees, no final_reason.
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)

        events = await drain_word_queue(manager)

        # Disagreeing finals without final_reason should hit the conservative
        # Mode 3 default and queue retraction markers.
        retractions = [e for e in events if e.is_retraction_marker]
        assert len(retractions) >= 1, "Conservative default should retract"

        # No capabilities declaration -> the diagnostic stays silent.
        assert not any(
            "EOS_NOT_RECEIVED" in rec.message for rec in caplog.records
        ), "Warning must stay silent for a server that never declared emits_eos"


# ===========================================================================
# Ambiguous: GOOGLE_FINAL with no EOS and no stable disagreement
# ===========================================================================

class TestAmbiguousGoogleFinalNoEos:
    """Disagreeing GOOGLE_FINAL with no eos and no prior stable disagreement."""

    @pytest.mark.asyncio
    async def test_ambiguous_google_final_no_eos_defaults_to_mode3(self, manager, caplog):
        utt = 1
        ws = FakeWebsocket([
            stable_message("hello world", utt),
            # GOOGLE_FINAL with no preceding eos; final disagrees.
            final_message(
                "different text",
                utt,
                final_reason="GOOGLE_FINAL",
            ),
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)

        events = await drain_word_queue(manager)

        retractions = [e for e in events if e.is_retraction_marker]
        assert len(retractions) == 1, "Ambiguous case should default to retract+replay"

        assert any(
            "AMBIGUOUS_NO_EOS" in rec.message for rec in caplog.records
        ), "Ambiguous classification should log a warning"


# ===========================================================================
# Race: eos arrives before any stable for an utterance
# ===========================================================================

class TestRaceEosBeforeFirstStable:
    """Race condition from wh-76yv.2: eos can arrive before first stable."""

    @pytest.mark.asyncio
    async def test_race_eos_before_first_stable_survives_reset(self, manager):
        """EOS state must survive _extract_delta's new-utterance reset.

        Sequence: previous utterance ends, eos for new utterance arrives,
        first stable for new utterance arrives (which triggers the
        new-utterance reset block in _extract_delta), then disagreeing final.
        The disagreeing final must classify as Mode 2 (no retraction) --
        proving _eos_received_for_utterance_id was not erased.
        """
        # Establish prior utterance state by feeding utterance 1 first so
        # _last_stable_utterance_id is set.
        prior_ws = FakeWebsocket([
            stable_message("prior utterance", 1),
            final_message("prior utterance", 1, final_reason="GOOGLE_FINAL"),
        ])
        await manager.handle_connection(prior_ws)
        # Drain so the assertion below counts only the new utterance's events.
        await drain_word_queue(manager)

        utt = 2
        ws = FakeWebsocket([
            eos_message(utt),                # EOS for utterance 2 BEFORE stable.
            stable_message("hello world", utt),  # Triggers _extract_delta reset.
            final_message(
                "totally different",
                utt,
                final_reason="GOOGLE_FINAL",  # Disagrees.
            ),
        ])

        await manager.handle_connection(ws)

        events = await drain_word_queue(manager)

        # Mode 2 path: no retraction marker for utterance 2's final.
        assert not any(e.is_retraction_marker for e in events), (
            "EOS state was erased by _extract_delta reset; final fell through "
            "to Mode 3 instead of Mode 2"
        )


# ===========================================================================
# Stream lifecycle: EOS_NOT_RECEIVED warning re-fires on next stream
# ===========================================================================

class TestStreamDiagnosticResetsBetweenClients:
    """EOS_NOT_RECEIVED warning resets when the client disconnects."""

    @pytest.mark.asyncio
    async def test_warning_refires_after_client_disconnect(self, manager, caplog):
        """First stream emits the warning, second stream emits it again.

        wh-nvyh: each stream now opens with a capabilities message declaring
        emits_eos=true, the way an updated Google provider's forwarder does
        on every (re)connect.
        """
        first_stream = FakeWebsocket([
            capabilities_message("google_stt", True),
            stable_message("a", 1),
            final_message("a", 1),
            stable_message("b", 2),
            final_message("b", 2),
            stable_message("c", 3),
            final_message("c", 3),  # Triggers the warning on the third final.
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(first_stream)

        first_warnings = [
            rec for rec in caplog.records
            if "EOS_NOT_RECEIVED" in rec.message
        ]
        assert len(first_warnings) >= 1, "First stream should emit the warning"

        # Drain and clear log records between streams.
        await drain_word_queue(manager)
        caplog.clear()

        second_stream = FakeWebsocket([
            capabilities_message("google_stt", True),
            stable_message("d", 4),
            final_message("d", 4),
            stable_message("e", 5),
            final_message("e", 5),
            stable_message("f", 6),
            final_message("f", 6),  # Should re-trigger the warning.
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(second_stream)

        second_warnings = [
            rec for rec in caplog.records
            if "EOS_NOT_RECEIVED" in rec.message
        ]
        assert len(second_warnings) >= 1, (
            "Second stream should emit the warning again after the previous "
            "stream's diagnostic state was reset"
        )


# ===========================================================================
# Stream-boundary state hygiene (wh-eknz.1, wh-eknz.2)
# ===========================================================================

class TestPerUtteranceStateAcrossStreams:
    """Per-utterance flags must NOT survive across stream boundaries.

    Implementation review finding wh-eknz.1: utterance IDs reset to 1 in each
    STT provider process, so a stale _eos_received_for_utterance_id from
    stream A would otherwise match stream B's first utterance and steer the
    decision tree into Mode 2 instead of the conservative default.
    """

    @pytest.mark.asyncio
    async def test_eos_flag_does_not_carry_across_streams(self, manager):
        utt = 1
        # Stream A: send eos for utt 1, then disconnect (no final).
        stream_a = FakeWebsocket([eos_message(utt)])
        await manager.handle_connection(stream_a)

        # After A's disconnect (the last client), the EOS flag must be cleared.
        assert manager._eos_received_for_utterance_id is None, (
            "EOS flag survived stream A's disconnect"
        )

        # Stream B reuses utterance_id=1 from a fresh STT provider process.
        stream_b = FakeWebsocket([
            stable_message("hello world", utt),
            final_message(
                "different text",
                utt,
                final_reason="GOOGLE_FINAL",
            ),
        ])
        await manager.handle_connection(stream_b)

        events = await drain_word_queue(manager)

        # Expectation: the disagreeing GOOGLE_FINAL with no eos in stream B
        # must take the conservative Mode 3 path (retraction marker queued).
        # Without the cross-stream reset, this test would fail because the
        # decision tree would see the stale eos_received flag and pick Mode 2.
        retractions = [e for e in events if e.is_retraction_marker]
        assert len(retractions) == 1, (
            "Stream B's disagreeing GOOGLE_FINAL fell through to Mode 2 "
            "because stream A's EOS flag survived the disconnect"
        )

    @pytest.mark.asyncio
    async def test_stable_disagreement_flag_does_not_carry_across_streams(self, manager):
        utt = 1
        # Stream A: trigger a stable disagreement on utt 1.
        stream_a = FakeWebsocket([
            stable_message("first text", utt),
            stable_message("totally different", utt),  # Stable disagreement.
        ])
        await manager.handle_connection(stream_a)

        assert manager._stable_disagreement_for_utterance_id is None, (
            "Stable-disagreement flag survived stream A's disconnect"
        )


class TestStreamLifecycleBoundsState:
    """Stream-level diagnostic state must follow the active-stream lifecycle.

    Implementation review finding wh-eknz.2: WebSocketManager keeps disabled
    clients in self._clients, so a stale client's later disconnect must NOT
    wipe the active stream's diagnostic state. Conversely, when a new active
    client takes over, the diagnostic counter must reset so a downgraded
    provider gets the warning even if the prior provider had observed eos.
    """

    @pytest.mark.asyncio
    async def test_add_client_resets_state_when_new_active_takes_over(self, manager):
        # Simulate prior active stream that observed eos and counted finals.
        manager._eos_observed_in_stream = True
        manager._utterances_with_final_in_stream = 5
        manager._eos_warning_emitted = True
        manager._eos_received_for_utterance_id = 7
        manager._stable_disagreement_for_utterance_id = 9

        # Drop a placeholder fake client into _clients so add_client hits the
        # disable-existing branch (we just need add_client to run its reset).
        manager._clients.add(FakeWebsocket([]))

        new_client = FakeWebsocket([])
        await manager.add_client(new_client)

        assert manager._eos_observed_in_stream is False
        assert manager._utterances_with_final_in_stream == 0
        assert manager._eos_warning_emitted is False
        assert manager._eos_received_for_utterance_id is None
        assert manager._stable_disagreement_for_utterance_id is None

    def test_remove_client_preserves_state_when_other_clients_remain(self, manager):
        # Active client present. State reflects an active stream that has
        # already observed eos.
        active = FakeWebsocket([])
        stale_disabled = FakeWebsocket([])
        manager._clients.add(active)
        manager._clients.add(stale_disabled)

        manager._eos_observed_in_stream = True
        manager._utterances_with_final_in_stream = 2

        # Old disabled client disconnects mid-active-stream.
        manager.remove_client(stale_disabled)

        # Active stream's diagnostic state must survive the stale disconnect.
        assert manager._eos_observed_in_stream is True, (
            "Stale-client disconnect wiped the active stream's eos evidence"
        )
        assert manager._utterances_with_final_in_stream == 2
        assert active in manager._clients

    def test_remove_client_resets_state_when_last_client_disconnects(self, manager):
        active = FakeWebsocket([])
        manager._clients.add(active)

        manager._eos_observed_in_stream = True
        manager._utterances_with_final_in_stream = 5
        manager._eos_warning_emitted = True
        manager._eos_received_for_utterance_id = 1
        manager._stable_disagreement_for_utterance_id = 1

        manager.remove_client(active)

        assert manager._eos_observed_in_stream is False
        assert manager._utterances_with_final_in_stream == 0
        assert manager._eos_warning_emitted is False
        assert manager._eos_received_for_utterance_id is None
        assert manager._stable_disagreement_for_utterance_id is None


# ===========================================================================
# Provider gating for the EOS_NOT_RECEIVED diagnostic
# ===========================================================================

class TestEosWarningGatedByProvider:
    """The EOS_NOT_RECEIVED warning fires only for providers that emit eos.

    Local providers (distil_medium_en, sherpa_offline_parakeet_stt_server)
    finalize via their own VAD and never emit eos. The warning would
    otherwise fire on every stream after the third final and tell the user
    to "verify STT provider is updated", which is misleading. Since wh-nvyh
    the gate reads the provider's own capabilities declaration (emits_eos
    false for the local providers) instead of a hardcoded name set.
    """

    @pytest.mark.asyncio
    async def test_warning_skipped_for_distil_medium_en(self, manager, caplog):
        ws = FakeWebsocket([
            capabilities_message("distil_medium_en", False),
            stable_message("a", 1),
            final_message("a", 1),
            stable_message("b", 2),
            final_message("b", 2),
            stable_message("c", 3),
            final_message("c", 3),
            stable_message("d", 4),
            final_message("d", 4),
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)

        warnings = [
            rec for rec in caplog.records
            if "EOS_NOT_RECEIVED" in rec.message
        ]
        assert warnings == [], (
            "EOS_NOT_RECEIVED warning fired for a local provider that is not "
            "expected to emit eos"
        )

    @pytest.mark.asyncio
    async def test_warning_skipped_for_parakeet(self, manager, caplog):
        ws = FakeWebsocket([
            capabilities_message("sherpa_offline_parakeet", False),
            stable_message("a", 1),
            final_message("a", 1),
            stable_message("b", 2),
            final_message("b", 2),
            stable_message("c", 3),
            final_message("c", 3),
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)

        warnings = [
            rec for rec in caplog.records
            if "EOS_NOT_RECEIVED" in rec.message
        ]
        assert warnings == [], "EOS_NOT_RECEIVED warning fired for Parakeet"

    @pytest.mark.asyncio
    async def test_warning_skipped_when_state_manager_unavailable(self, event_loop, caplog):
        """A bare manager with no state_manager and no capabilities message
        keeps the warning suppressed (wh-nvyh: the gate no longer consults
        state_manager at all; the silent default covers this case)."""
        from integrations.websocket_manager import WebSocketManager

        bare_manager = WebSocketManager(loop=event_loop)
        bare_manager._app = AsyncMock()
        # Intentionally leave state_manager as None.
        ws = FakeWebsocket([
            stable_message("a", 1),
            final_message("a", 1),
            stable_message("b", 2),
            final_message("b", 2),
            stable_message("c", 3),
            final_message("c", 3),
        ])

        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await bare_manager.handle_connection(ws)

        warnings = [
            rec for rec in caplog.records
            if "EOS_NOT_RECEIVED" in rec.message
        ]
        assert warnings == [], (
            "EOS_NOT_RECEIVED warning fired with no state_manager available"
        )


# ===========================================================================
# Capability handshake (wh-nvyh): providers declare emits_eos on connect
# ===========================================================================

def capabilities_message(provider: str, emits_eos: bool) -> str:
    return make_message("capabilities", provider=provider, emits_eos=emits_eos)


def _three_finals_no_eos(start_utt: int = 1) -> list:
    msgs = []
    for i in range(3):
        utt = start_utt + i
        msgs.append(stable_message(f"text {utt}", utt))
        msgs.append(final_message(f"text {utt}", utt))
    return msgs


class TestCapabilitiesHandshake:
    """wh-nvyh: the EOS_NOT_RECEIVED gate follows the provider's declared
    capability, not a hardcoded provider-name set. The manager mock still
    reports provider name 'google_stt' (make_manager), which under the old
    frozenset design would arm the warning -- these tests prove the name
    no longer matters.
    """

    @pytest.mark.asyncio
    async def test_declared_emits_eos_true_arms_the_warning(self, manager, caplog):
        ws = FakeWebsocket(
            [capabilities_message("google_stt", True)] + _three_finals_no_eos()
        )
        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)
        assert any(
            "EOS_NOT_RECEIVED" in rec.message for rec in caplog.records
        ), "Provider declared emits_eos=True; warning should fire after 3 finals"

    @pytest.mark.asyncio
    async def test_declared_emits_eos_false_keeps_the_warning_silent(
        self, manager, caplog
    ):
        ws = FakeWebsocket(
            [capabilities_message("distil_medium_en", False)]
            + _three_finals_no_eos()
        )
        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)
        assert not any(
            "EOS_NOT_RECEIVED" in rec.message for rec in caplog.records
        ), "Provider declared emits_eos=False; warning must stay silent"

    @pytest.mark.asyncio
    async def test_no_capabilities_message_defaults_to_silent(self, manager, caplog):
        """A provider that never sends capabilities (older server) gets the
        safe default: no EOS_NOT_RECEIVED warning -- even though the mocked
        provider name is 'google_stt'."""
        ws = FakeWebsocket(_three_finals_no_eos())
        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(ws)
        assert not any(
            "EOS_NOT_RECEIVED" in rec.message for rec in caplog.records
        ), "No capabilities message must default to the silent gate"

    @pytest.mark.asyncio
    async def test_declaration_does_not_survive_stream_boundary(
        self, manager, caplog
    ):
        """Stream 1 declares emits_eos=True (warning fires). Stream 2 sends
        no capabilities; the flag must have been reset with the rest of the
        per-stream state."""
        first = FakeWebsocket(
            [capabilities_message("google_stt", True)] + _three_finals_no_eos(1)
        )
        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(first)
        assert any(
            "EOS_NOT_RECEIVED" in rec.message for rec in caplog.records
        )
        await drain_word_queue(manager)
        caplog.clear()

        second = FakeWebsocket(_three_finals_no_eos(4))
        with caplog.at_level(logging.WARNING, logger="integrations.websocket_manager"):
            await manager.handle_connection(second)
        assert not any(
            "EOS_NOT_RECEIVED" in rec.message for rec in caplog.records
        ), "emits_eos declaration leaked across the stream boundary"

    def test_provider_name_frozenset_removed(self):
        from integrations.websocket_manager import WebSocketManager

        assert not hasattr(WebSocketManager, "_STT_PROVIDERS_THAT_EMIT_EOS"), (
            "wh-nvyh: the hardcoded provider-name set must be gone; the "
            "capability handshake replaces it"
        )


class TestCapabilitiesActiveClientGate:
    """reviewer_0 finding wh-nvyh.1.1: only the ACTIVE client's declaration
    may set the per-stream flag. An orphaned provider reconnecting during
    its backoff window must not overwrite the new active stream's value."""

    @pytest.mark.asyncio
    async def test_stale_client_declaration_is_ignored(self, manager):
        client_a = FakeWebsocket([])
        client_b = FakeWebsocket([])
        await manager.add_client(client_a)
        await manager.add_client(client_b)  # B is now the active stream.

        manager._apply_capabilities(
            client_a, {"provider": "google_stt", "emits_eos": True}
        )
        assert manager._provider_emits_eos is False, (
            "A non-active client's declaration must not set the flag"
        )

        manager._apply_capabilities(
            client_b, {"provider": "google_stt", "emits_eos": True}
        )
        assert manager._provider_emits_eos is True

    @pytest.mark.asyncio
    async def test_no_active_client_ignores_declaration(self, manager):
        client_a = FakeWebsocket([])
        await manager.add_client(client_a)
        manager.remove_client(client_a)

        manager._apply_capabilities(
            client_a, {"provider": "google_stt", "emits_eos": True}
        )
        assert manager._provider_emits_eos is False

    @pytest.mark.asyncio
    async def test_active_disconnect_resets_capability_despite_lingering_client(
        self, manager
    ):
        """codex finding wh-nvyh.3.1: when the ACTIVE client disconnects
        while an older disabled client is still connected, the departed
        stream's declared capability (and its diagnostic counters) must
        not linger. Before the fix, the reset only ran when _clients
        became empty, so a disabled client's late finals could hit the
        EOS_NOT_RECEIVED gate armed by a provider that already left."""
        client_a = FakeWebsocket([])
        client_b = FakeWebsocket([])
        await manager.add_client(client_a)
        await manager.add_client(client_b)  # B active; A disabled, connected.

        manager._apply_capabilities(
            client_b, {"provider": "google_stt", "emits_eos": True}
        )
        assert manager._provider_emits_eos is True
        # Simulate the departed stream having already counted finals, so a
        # stale counter would be near the warning threshold.
        manager._utterances_with_final_in_stream = 2

        manager.remove_client(client_b)  # Active leaves; A remains in _clients.

        assert manager._provider_emits_eos is False, (
            "The departed active stream's capability must not survive its "
            "disconnect just because a disabled client is still connected"
        )
        assert manager._utterances_with_final_in_stream == 0
        assert manager._eos_warning_emitted is False
        assert manager._eos_observed_in_stream is False
