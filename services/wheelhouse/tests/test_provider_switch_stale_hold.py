"""A provider switch must not leave a bare-number hold standing
(wh-provider-switch-stale-hold).

``LogicController._switch_stt_provider`` replaces the engine that feeds
the word queue. Words the speech processor is already holding back were
spoken to the OLD engine, and nothing in the switch reaches them: the
bare-number hold keeps its words across the swap, and the first word the
NEW engine delivers flushes them as dictation. The user asked for a
different speech engine and got the number they said before the switch
typed into their document, with no badge click and no way to predict it.

The remote branch is what is covered here: it stops one provider process
and starts another without leaving the process. The mode-change branch
above it ends in ``restart_program``, which drops the processor with
everything in it, so it needs no clear.

wh-in-process-capture-removal deleted the third branch, the in-process one
that handed the change to STTManager, and the two tests that drove it.

The held words are DROPPED rather than typed. The reason is on
``SpeechProcessor.drop_bare_number_hold`` as a crewcut note.
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

from queue import Queue
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.wheelhouse.click_overlay_state import OverlayState
from tests.test_speech_processor_bare_number import (
    end_marker,
    make_processor,
    word,
)


def _controller(processor):
    """A stand-in ``self`` for ``_switch_stt_provider``.

    The command tests in test_ui_provider_switching.py call this method
    unbound on a ``MagicMock(spec=LogicController)``; this builds the same
    shape and additionally puts a REAL SpeechProcessor at
    ``service_manager.speech_handler.speech_processor``. A MagicMock would
    auto-create that attribute and answer every assertion about the slots
    with a mock, so the test would pass against unchanged code.

    The controller is set up so no mode change is needed: it is asked for
    another remote provider, one its launcher knows.
    """
    from main import LogicController

    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: {
        "stt.mode": "remote",
        "stt.last_provider": "google_stt",
        "stt.provider": "whisper",
    }.get(key, default)
    config_service.set = MagicMock()
    config_service.save = AsyncMock(return_value=True)

    service_manager = MagicMock()
    service_manager.speech_handler.speech_processor = processor

    remote_launcher = MagicMock()
    remote_launcher.stop_provider = AsyncMock(return_value=True)
    remote_launcher.start_provider = MagicMock(return_value=True)
    # The ordinary switch: the outgoing process takes the shutdown
    # command and is gone by the time anyone asks. Set explicitly
    # because both remote arms now poll it (the shared
    # outgoing_engine_is_gone wait, wh-provider-switch-stale-hold.2.1)
    # and an unset MagicMock answers truthy forever -- the wait would
    # then run its full _PROVIDER_EXIT_WAIT_S and every test here
    # would take ten seconds and report the old engine as surviving.
    # Tests for the cells where it does survive override this.
    remote_launcher.is_running = MagicMock(return_value=False)
    remote_launcher.get_provider_by_name.return_value = {
        "name": "parakeet_tdt",
        "display_name": "Parakeet v3 (GPU)",
    }
    service_manager.remote_stt_launcher = remote_launcher

    state_manager = MagicMock()
    state_manager.state_to_gui_queue = Queue()
    state_manager._get_current_stt_provider.return_value = "google_stt"

    controller = MagicMock(spec=LogicController)
    controller.config_service = config_service
    controller.service_manager = service_manager
    controller.state_manager = state_manager
    return controller


async def _switch(controller, provider: str) -> None:
    from main import LogicController as LC

    await LC._switch_stt_provider(controller, provider)


class TestRemoteSwitchDropsTheHold:
    """The remote branch: stop one provider process, start another."""

    @pytest.mark.asyncio
    async def test_a_remote_switch_clears_both_bare_number_slots(self):
        """Both slots are empty once the switch returns.

        The deferred word is set by hand. On a normal path it is cleared
        before the event that set it finishes, so it is never standing
        between events -- but the per-word exception handler exists
        because a raise can leave it standing, and a switch arriving in
        that window must not carry it into the new engine either.
        """
        proc, _app, _parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        assert proc._pending_bare_number_words == ["twenty"]
        proc._bare_number_deferred_word = "three"

        await _switch(_controller(proc), "parakeet_tdt")

        assert proc._pending_bare_number_words is None
        assert proc._bare_number_deferred_word is None

    @pytest.mark.asyncio
    async def test_a_word_after_a_remote_switch_types_no_stale_number(self):
        """The reported harm, driven end to end.

        A word opening a new utterance flushes a standing bare-number hold
        as dictation (test_a_new_utterance_word_never_joins_the_previous_number
        pins that flush, which is correct when both utterances came from
        the same engine). After a provider switch the held words belong to
        an engine that is no longer running, so the flush types a number
        the user is no longer saying anything about.
        """
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))

        await _switch(_controller(proc), "parakeet_tdt")

        await proc.process_word_event(word("three", start=True, uid=2))
        await proc.process_word_event(end_marker(uid=2))

        assert app.inserted_texts() == []
        assert parser.executed == ["click three"]

    @pytest.mark.asyncio
    async def test_a_switch_to_the_running_provider_keeps_the_hold(self):
        """The early return is not a switch, so it drops nothing.

        Asking for the provider that is already running returns before
        stop_provider, leaving the same engine feeding the same queue. The
        held words are still that engine's words and still the user's
        pending click.
        """
        proc, app, _parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))

        await _switch(_controller(proc), "google_stt")

        assert proc._pending_bare_number_words == ["twenty"]
        assert app.inserted_texts() == []


    @pytest.mark.asyncio
    async def test_a_failed_start_that_leaves_the_old_engine_running_keeps_the_hold(
        self, monkeypatch,
    ):
        """A switch that changed no engine must drop nothing.

        stop_provider only sends the shutdown command over the WebSocket;
        a provider whose socket is disconnected never receives it, and
        the send itself can raise. When the new provider then refuses to
        start, this branch waits for the old process to exit, finds it
        still running, and deliberately leaves it running -- it tells the
        user so in as many words. The engine feeding the word queue never
        changed, so the words it is holding are still the words the user
        is in the middle of saying, and that same engine's next word
        still extends them into the number they meant. Dropping here
        would throw away live words to fix a staleness that has not
        happened (wh-provider-switch-stale-hold.1.1).
        """
        import main

        monkeypatch.setattr(main, "_PROVIDER_EXIT_WAIT_S", 0.4)
        proc, app, _parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))

        controller = _controller(proc)
        launcher = controller.service_manager.remote_stt_launcher
        launcher.start_provider = MagicMock(return_value=False)
        launcher.is_running = MagicMock(return_value=True)

        await _switch(controller, "parakeet_tdt")

        assert proc._pending_bare_number_words == ["twenty"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_a_failed_start_whose_old_engine_exits_drops_the_hold(
        self, monkeypatch,
    ):
        """A confirmed exit leaves no engine, so the hold must go.

        The same failed start, but the old process does exit inside the
        wait, and the branch records the engine as stopped. Nothing can
        complete the held click now: the words were spoken to a process
        that is gone, so the first word of whatever engine the user
        starts next would flush them into their document. This is the
        cell that makes the drop conditional on the outcome rather than
        on reaching the start attempt.
        """
        import main

        monkeypatch.setattr(main, "_PROVIDER_EXIT_WAIT_S", 0.4)
        proc, _app, _parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        proc._bare_number_deferred_word = "three"

        controller = _controller(proc)
        launcher = controller.service_manager.remote_stt_launcher
        launcher.start_provider = MagicMock(return_value=False)
        launcher.is_running = MagicMock(side_effect=[True, False])

        await _switch(controller, "parakeet_tdt")

        controller.state_manager.set_remote_stt_stopped.assert_called_once()
        assert proc._pending_bare_number_words is None
        assert proc._bare_number_deferred_word is None


    @pytest.mark.asyncio
    async def test_a_spawned_replacement_does_not_drop_a_live_old_engine_hold(
        self, monkeypatch,
    ):
        """A spawned child is not a changed engine.

        start_provider returns True as soon as subprocess.Popen succeeds
        (stt/remote_stt_launcher.py:1426-1460); readiness is decided
        later on a background monitor thread, and the same True comes
        back when the requested provider was already running
        (:1310, :1319). So True says a child was spawned, not that the
        outgoing engine stopped. main.py's own
        _reconcile_stopped_remote_provider docstring spells the
        consequence out at main.py:1123-1135: the replacement can fail
        its startup afterwards while the engine it replaced is still
        running, and that survivor gets recorded again.

        This is the cell where the shutdown send never landed and the old
        process is still transcribing. Nothing about the user's engine
        changed, its own next word still completes the number, and the
        hold has to survive (wh-provider-switch-stale-hold.2.1).
        """
        import main

        monkeypatch.setattr(main, "_PROVIDER_EXIT_WAIT_S", 0.4)
        proc, app, _parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))

        controller = _controller(proc)
        launcher = controller.service_manager.remote_stt_launcher
        launcher.stop_provider = AsyncMock(return_value=False)
        launcher.start_provider = MagicMock(return_value=True)
        launcher.is_running = MagicMock(return_value=True)

        await _switch(controller, "parakeet_tdt")

        launcher.start_provider.assert_called_once_with("parakeet_tdt")
        assert proc._pending_bare_number_words == ["twenty"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_a_spawned_replacement_drops_the_hold_once_the_old_engine_goes(
        self, monkeypatch,
    ):
        """The ordinary successful switch still drops.

        Same successful spawn, but the outgoing process exits inside the
        wait, which is what happens on every switch that works. The
        engine the held words were spoken to is gone, so the hold goes
        with it -- the harm this bead exists for.
        """
        import main

        monkeypatch.setattr(main, "_PROVIDER_EXIT_WAIT_S", 0.4)
        proc, _app, _parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        proc._bare_number_deferred_word = "three"

        controller = _controller(proc)
        launcher = controller.service_manager.remote_stt_launcher
        launcher.is_running = MagicMock(side_effect=[True, False])

        await _switch(controller, "parakeet_tdt")

        controller.state_manager.set_running_remote_stt_provider\
            .assert_called_once_with("parakeet_tdt")
        assert proc._pending_bare_number_words is None
        assert proc._bare_number_deferred_word is None
