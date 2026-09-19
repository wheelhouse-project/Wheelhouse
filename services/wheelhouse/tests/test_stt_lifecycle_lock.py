"""One lock serializes the STT lifecycle commands (review finding
wh-google-creds-file-picker.1.3).

The GUI listener starts switch_stt_provider and restart_stt_service as
independent asyncio tasks. The switch awaits for up to ten seconds
between stopping the outgoing provider and starting the replacement
process, so without serialization a second switch, or a restart command,
can interleave with that window and leave two providers running or none.
Every handler below must acquire the shared ``_stt_lifecycle_lock``
before touching provider state, so overlapping commands run one at a
time in arrival order.

The credentials handler that motivated this lock is gone:
wh-remove-restart-credentials-items deleted
_set_google_credentials_file and hard_restart_stt_service on David's
order (QUESTIONS-2026-09-04.md item 20), and their tests with them. The
lock still guards the two commands that remain.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _controller(current_provider: str = "google_stt"):
    from main import LogicController

    controller = MagicMock(spec=LogicController)

    class _Settings:
        def __init__(self, values):
            self.values = dict(values)
            self.save_count = 0

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

        def unset(self, key):
            self.values.pop(key, None)

        async def save(self) -> bool:
            self.save_count += 1
            return True

    controller.config_service = _Settings(
        {"stt.mode": "remote", "stt.last_provider": current_provider}
    )
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    # The tray's read path: runtime record first, config as fallback.
    # _switch_stt_provider shares it (provider-removal review .1.9).
    controller.state_manager._get_current_stt_provider.return_value = (
        current_provider
    )
    controller.service_manager = MagicMock()
    launcher = MagicMock()
    launcher.stop_provider = AsyncMock(return_value=True)
    launcher.start_provider = MagicMock(return_value=True)
    launcher.is_running = MagicMock(return_value=False)
    launcher.get_provider_by_name = MagicMock(return_value={"name": "any"})
    controller.service_manager.remote_stt_launcher = launcher
    controller.app = MagicMock()
    controller.app.websocket_manager = MagicMock()
    controller.app.websocket_manager.send_command_to_stt = AsyncMock()
    return controller


async def _held_lock(controller):
    """Give the controller a shared lifecycle lock and acquire it."""
    lock = asyncio.Lock()
    controller._stt_lifecycle_lock = lock
    await lock.acquire()
    return lock


@pytest.mark.asyncio
async def test_switch_provider_waits_for_lifecycle_lock():
    from main import LogicController

    controller = _controller(current_provider="google_stt")
    lock = await _held_lock(controller)

    task = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    await asyncio.sleep(0.05)
    launcher = controller.service_manager.remote_stt_launcher
    launcher.stop_provider.assert_not_awaited()
    launcher.start_provider.assert_not_called()

    lock.release()
    await asyncio.wait_for(task, timeout=5)
    launcher.stop_provider.assert_awaited_once_with("google_stt")
    launcher.start_provider.assert_called_once_with("parakeet_tdt")


@pytest.mark.asyncio
async def test_restart_service_waits_for_lifecycle_lock():
    from main import LogicController

    controller = _controller()
    lock = await _held_lock(controller)

    task = asyncio.ensure_future(
        LogicController.restart_stt_service(controller)
    )
    await asyncio.sleep(0.05)
    send = controller.app.websocket_manager.send_command_to_stt
    send.assert_not_awaited()

    lock.release()
    await asyncio.wait_for(task, timeout=5)
    send.assert_awaited_once_with("restart_service")


@pytest.mark.asyncio
async def test_lock_is_created_when_absent():
    """A controller without the lock attribute (a spec'd mock in tests,
    or a controller built before __init__ finished) gets one lazily, so
    the serialization never silently disappears."""
    from main import LogicController

    controller = _controller()

    await LogicController.restart_stt_service(controller)

    assert isinstance(controller._stt_lifecycle_lock, asyncio.Lock)


def _recording_controller(current_provider: str = "google_stt"):
    """A controller whose runtime provider record answers with whatever
    the last completed switch wrote.

    The plain ``_controller`` above pins ``_get_current_stt_provider`` to
    one value, which is enough for a test that runs a single command. A
    queued second command has to read what the first one recorded, so
    this fake keeps the record in a dict that
    ``set_running_remote_stt_provider`` rewrites -- the same pair of
    calls ``_switch_stt_provider`` makes when it reads the record on
    entry and writes it after a successful start.
    """
    controller = _controller(current_provider=current_provider)
    record = {"provider": current_provider}
    controller.state_manager._get_current_stt_provider.side_effect = (
        lambda: record["provider"]
    )
    controller.state_manager.set_running_remote_stt_provider.side_effect = (
        lambda name: record.__setitem__("provider", name)
    )
    return controller, record


def _order_recorder(controller):
    """Record every stop and start in arrival order.

    The first stop parks on an event the test controls. That is the
    window a second command would run inside if the lock were gone:
    ``_switch_stt_provider`` has sent the stop by then but has not
    started the replacement, so the runtime record still names the
    outgoing provider.
    """
    order: list[str] = []
    first_stop_reached = asyncio.Event()
    release_first_stop = asyncio.Event()
    launcher = controller.service_manager.remote_stt_launcher

    async def recording_stop(name):
        order.append(f"stop:{name}")
        if not first_stop_reached.is_set():
            first_stop_reached.set()
            await release_first_stop.wait()
        return True

    launcher.stop_provider = AsyncMock(side_effect=recording_stop)
    launcher.start_provider = MagicMock(
        side_effect=lambda name: order.append(f"start:{name}") or True
    )
    return order, first_stop_reached, release_first_stop


@pytest.mark.asyncio
async def test_a_queued_switch_stops_the_provider_the_first_switch_started():
    """Two switches, the second sent while the first holds the lock, run
    one at a time, and the second stops the provider the first STARTED.

    This is the lock's whole contract, and it is the half no other test
    covers: the two waits-for-the-lock tests above hold the lock from
    the test itself, so no earlier command has written anything for the
    later one to read. Without serialization the second switch reads the
    record while the first is suspended between its stop and its start,
    stops google_stt a second time, starts its own provider, and the
    first switch then starts parakeet_tdt on top of it -- two engines
    running, which is the harm this module's docstring names.
    """
    from main import LogicController

    controller, record = _recording_controller(current_provider="google_stt")
    order, first_stop_reached, release_first_stop = _order_recorder(controller)

    first = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    await asyncio.wait_for(first_stop_reached.wait(), timeout=5)

    second = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "distil_medium_en")
    )
    # Long enough for an unserialized second switch to run its whole
    # remote branch: nothing in that branch awaits except the stop,
    # which returns at once now that the first stop has been reached.
    await asyncio.sleep(0.05)

    release_first_stop.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert order == [
        "stop:google_stt",
        "start:parakeet_tdt",
        "stop:parakeet_tdt",
        "start:distil_medium_en",
    ]
    assert record["provider"] == "distil_medium_en"


@pytest.mark.asyncio
async def test_a_queued_restart_sends_only_after_the_switch_finishes():
    """A restart command sent while a switch holds the lock sends its
    websocket command only after the switch has started the replacement.

    The restart command is the other member of the serialized pair, and
    the same window applies to it: sent while the switch sits between
    its stop and its start, an unserialized restart would tell a
    provider to restart while no provider process is running.
    """
    from main import LogicController

    controller, _record = _recording_controller(current_provider="google_stt")
    order, first_stop_reached, release_first_stop = _order_recorder(controller)
    send = controller.app.websocket_manager.send_command_to_stt
    send.side_effect = lambda command: order.append(f"send:{command}")

    switch = asyncio.ensure_future(
        LogicController._switch_stt_provider(controller, "parakeet_tdt")
    )
    await asyncio.wait_for(first_stop_reached.wait(), timeout=5)

    restart = asyncio.ensure_future(
        LogicController.restart_stt_service(controller)
    )
    await asyncio.sleep(0.05)
    assert order == ["stop:google_stt"], (
        "the restart command ran inside the switch's stop-to-start window"
    )

    release_first_stop.set()
    await asyncio.wait_for(asyncio.gather(switch, restart), timeout=5)

    assert order == [
        "stop:google_stt",
        "start:parakeet_tdt",
        "send:restart_service",
    ]
