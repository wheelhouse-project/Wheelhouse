"""One launch of a provider is told apart from a later launch of the same one.

Identity was the provider NAME at every place that records the running
remote engine, and a name cannot tell a launch that failed from a launch
of the same provider that started afterwards. Three symptoms follow, and
a fix has to close all three (wh-launch-generation, from the ruling on
wh-remote-stt-robustness.2.8):

Ordering 1 -- the refused compare-and-set. The reconciliation for a
failed launch finds a survivor, the compare-and-set is refused because a
switch recorded a different engine meanwhile, and the fall-through at the
end then clears a record that a restart of the SAME provider put back.

Ordering 2 -- the successful compare-and-set. The switch away and back
lands during the bounded wait rather than after the compare, so the
record names the failed provider again at the moment of the compare, the
compare SUCCEEDS, and the stale survivor is written over a launch that is
transcribing. This ordering reaches the write through the success path,
which is why a fifth name comparison could not close it.

Item 12 -- the blank display. `_provider_ready_event` and
`_provider_startup_failed` are one pair shared by every provider, and the
websocket manager drops a notification only from a NON-active client. The
provider being replaced keeps an active connection until the replacement
connects, so its `startup_failed` reaches the launcher, wakes the
replacement's own startup monitor, and is read there as the
replacement's own failure.

The identity is a launch generation number: the launcher stamps one at
every start, the startup monitor and the failure signal carry it, and
both engine-record write sites compare it instead of the name.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

_PROVIDERS_DIR = Path(__file__).resolve().parents[2] / "stt_providers"


def _manifest_provider_name(service_dir: Path) -> str:
    """The name WheelHouse launches a provider under.

    `discover_providers` reads it from the `[provider]` section of the
    service's config.toml, and `start_provider` keys the launch
    generation by it.
    """
    with (service_dir / "config.toml").open("rb") as handle:
        return tomllib.load(handle)["provider"]["name"]


def _declared_provider_name(service_dir: Path) -> str:
    """The name a provider announces in its own capabilities frame.

    Read from the source rather than hardcoded, so a test that pins the
    two names together fails when either side moves. The provider passes
    it to WSForwarder as a literal keyword argument.

    The read is anchored on the `debug=` argument directly above it,
    because `provider_name=` is no longer unique in a provider's main.py:
    wh-capture-winrt-required added `send_startup_failed_notice` calls
    that name the provider as well, and they come FIRST in the file. A
    plain `re.search` therefore returned a refusal's name and left the
    declaration itself unguarded -- the mutations
    `the-provider-announces-a-name-the-launcher-never-started` and
    `the-provider-announces-a-name-that-never-existed` in
    tests/mutation_gate_remote_stt_robustness.py rewrite only the
    WSForwarder site, and both survived until this was anchored.

    Exactly one match is required, so a second construction site fails
    the read instead of being taken silently.
    """
    source = (service_dir / "main.py").read_text(encoding="utf-8")
    matches = re.findall(
        r'\n\s*debug=[^\n]*,\n\s*provider_name="([^"]+)"', source)
    assert len(matches) == 1, (
        f"{service_dir.name}/main.py has {len(matches)} provider_name "
        "arguments below a debug= argument; expected exactly the one "
        "WSForwarder construction"
    )
    return matches[0]


def _launcher(tmp_path):
    from stt.remote_stt_launcher import RemoteSTTLauncher

    return RemoteSTTLauncher(
        services_dir=tmp_path / "providers",
        app_data_dir=tmp_path / "appdata",
    )


def _slot(launcher, generation):
    """The slot of a launch that must still have one.

    `_launch_signal` answers None for a launch whose slot the cap has
    dropped, so every caller has to consider that answer. A test that
    stamped the launch a few lines earlier has evicted nothing, and
    saying so once here keeps the check out of every assertion below
    (wh-launch-signal-eviction).
    """
    signal = launcher._launch_signal(generation)
    assert signal is not None, f"launch {generation} has no slot"
    return signal


def _live_subprocess():
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


def _state_with_generations(generations):
    """A real StateManager wired to a launcher that answers
    `launch_generation`, so a test asserts the answer the tray reads.

    `generations` is mutated by the tests to move a provider on to its
    next launch, exactly as a restart would.
    """
    from state_manager import StateManager

    values = {"stt.mode": "remote", "stt.last_provider": "parakeet_tdt"}
    config_service = MagicMock()
    config_service.get.side_effect = lambda key, default=None: values.get(key, default)
    state = StateManager(
        config_service=config_service,
        event_bus=MagicMock(),
        loop=MagicMock(),
        state_to_gui_queue=MagicMock(),
        websocket_manager=MagicMock(),
    )
    state.send_state_update = MagicMock()
    launcher = MagicMock()
    launcher.launch_generation.side_effect = lambda name: generations.get(name)
    state.set_remote_stt_launcher(launcher)
    return state, launcher


class TestOrderingOneTheRefusedCompare:
    """A restart of the failed provider survives the fall-through clear."""

    def test_a_restart_between_the_refusal_and_the_clear_is_not_blanked(
        self, monkeypatch
    ):
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.0)
        generations = {"parakeet_tdt": 1, "google_stt": 1, "whisper_cpp": 2}
        state, launcher = _state_with_generations(generations)
        state.set_running_remote_stt_provider("parakeet_tdt")

        launcher.get_providers.return_value = [
            {"name": "google_stt"},
            {"name": "parakeet_tdt"},
        ]

        def _running(name):
            # The user switches away while the scan runs, so the record
            # names another engine by the time of the compare and the
            # compare-and-set is refused.
            state.set_running_remote_stt_provider("whisper_cpp")
            return name == "google_stt"

        launcher.is_running.side_effect = _running

        real_replace = state.replace_stopped_remote_stt_provider

        def _replace_then_restart(*args, **kwargs):
            refused = real_replace(*args, **kwargs)
            assert refused is False
            # The serialized switch back to parakeet_tdt lands here: a
            # NEW launch of the same provider, now transcribing.
            generations["parakeet_tdt"] = 3
            state.set_running_remote_stt_provider("parakeet_tdt")
            return refused

        state.replace_stopped_remote_stt_provider = _replace_then_restart

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True, generation=1
        )

        assert state._get_current_stt_provider() == "parakeet_tdt"


class TestOrderingTwoTheSuccessfulCompare:
    """The stale survivor is not written over a newer launch of the same
    provider, on the path where the compare-and-set SUCCEEDS."""

    def test_a_stale_survivor_does_not_overwrite_a_relaunch_of_the_same_name(
        self, monkeypatch
    ):
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.0)
        generations = {"parakeet_tdt": 1, "google_stt": 1}
        state, launcher = _state_with_generations(generations)
        state.set_running_remote_stt_provider("parakeet_tdt")

        launcher.get_providers.return_value = [
            {"name": "google_stt"},
            {"name": "parakeet_tdt"},
        ]

        def _running(name):
            # The switch away and back completes during the scan, so the
            # record names parakeet_tdt again -- a DIFFERENT launch of it
            # -- before the compare-and-set is reached. The name matches,
            # so only a generation can refuse this write.
            generations["parakeet_tdt"] = 4
            state.set_running_remote_stt_provider("parakeet_tdt")
            return name == "google_stt"

        launcher.is_running.side_effect = _running

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True, generation=1
        )

        assert state._get_current_stt_provider() == "parakeet_tdt"

    def test_a_survivor_is_still_recorded_when_the_launch_did_not_move(
        self, monkeypatch
    ):
        # The behaviour wh-remote-stt-robustness.2.5 added, kept: the
        # generation compare must not refuse the case the survivor write
        # exists for.
        import main as main_module
        from main import _reconcile_stopped_remote_provider

        monkeypatch.setattr(main_module, "_PROVIDER_EXIT_WAIT_S", 0.0)
        generations = {"parakeet_tdt": 1, "google_stt": 2}
        state, launcher = _state_with_generations(generations)
        state.set_running_remote_stt_provider("parakeet_tdt")
        launcher.get_providers.return_value = [
            {"name": "google_stt"},
            {"name": "parakeet_tdt"},
        ]
        launcher.is_running.side_effect = lambda name: name == "google_stt"

        _reconcile_stopped_remote_provider(
            state, launcher, "parakeet_tdt", may_wait=True, generation=1
        )

        assert state._get_current_stt_provider() == "google_stt"


class TestItemTwelveTheBlankDisplay:
    """A startup failure reported by the launch being replaced must not
    end the replacement's own startup monitor."""

    def test_a_failure_from_an_older_launch_does_not_report_the_new_one_stopped(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        # Launch 2 of parakeet_tdt is in flight and healthy.
        launcher._launch_generations["parakeet_tdt"] = 2
        launcher._provider_ready_event.clear()
        launcher._provider_startup_failed = False
        launcher._subprocesses["parakeet_tdt"] = _live_subprocess()

        # The provider being replaced still holds the active connection,
        # so its startup_failed reaches the launcher stamped with the
        # generation of the launch that sent it.
        launcher.signal_provider_startup_failed(generation=1)

        launcher._monitor_startup(
            "parakeet_tdt", "Parakeet", timeout=0.05, generation=2
        )

        stopped.assert_not_called()

    def test_the_replacement_is_still_starting_after_the_foreign_failure(
        self, tmp_path
    ):
        # An older launch's failure must not end the starting state of
        # the launch that replaced it: the startup suppression would
        # then let the replacement's own notices through early.
        launcher = _launcher(tmp_path)
        launcher._launch_generations["parakeet_tdt"] = 2
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()
        launcher._provider_startup_failed = False

        launcher.signal_provider_startup_failed(generation=1)

        assert launcher.is_starting is True
        # The failure went to the launch that sent it, and left the
        # replacement's own slot untouched.
        assert launcher._launch_signals[1].failed is True
        assert _slot(launcher, 2).failed is False
        assert _slot(launcher, 2).event.is_set() is False

    def test_a_failure_from_the_monitors_own_launch_still_reports_stopped(
        self, tmp_path
    ):
        # The behaviour wh-google-creds-file-picker.1.5 added, kept.
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        launcher._launch_generations["parakeet_tdt"] = 2
        launcher._provider_ready_event.clear()
        launcher._provider_startup_failed = False

        launcher.signal_provider_startup_failed(generation=2)

        launcher._monitor_startup(
            "parakeet_tdt", "Parakeet", timeout=0.05, generation=2
        )

        stopped.assert_called_once_with(
            "parakeet_tdt", may_wait=True, generation=2
        )

    def test_a_failure_with_no_generation_still_reports_stopped(self, tmp_path):
        # Only google_stt sends kind="startup_failed" today, and a
        # connection registered before any launch was stamped has no
        # generation to carry. An unstamped failure keeps the old
        # behaviour rather than being discarded.
        launcher = _launcher(tmp_path)
        stopped = MagicMock()
        launcher.set_provider_stopped_callback(stopped)
        launcher._launch_generations["google_stt"] = 5
        launcher._provider_ready_event.clear()
        launcher._provider_startup_failed = False

        launcher.signal_provider_startup_failed()

        launcher._monitor_startup("google_stt", "Google", timeout=0.05, generation=5)

        stopped.assert_called_once_with("google_stt", may_wait=True, generation=5)


class TestTheLauncherStampsEveryLaunch:
    """The stamp itself: every start gets a number, and it goes up."""

    def test_each_start_of_the_same_provider_gets_a_new_generation(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher.ws_port = 5555
        launcher.get_provider_by_name = MagicMock(
            return_value={
                "name": "parakeet_tdt",
                "display_name": "Parakeet",
                "service_dir": tmp_path / "nowhere",
                "launcher": "run.py",
            }
        )

        assert launcher.start_provider("parakeet_tdt") is False
        first = launcher.launch_generation("parakeet_tdt")
        assert launcher.start_provider("parakeet_tdt") is False
        second = launcher.launch_generation("parakeet_tdt")

        assert first is not None
        assert second is not None
        assert second > first

    def test_a_provider_that_never_started_has_no_generation(self, tmp_path):
        launcher = _launcher(tmp_path)

        assert launcher.launch_generation("parakeet_tdt") is None


class TestTheConnectionCarriesItsOwnLaunch:
    """The failure signal is stamped by the connection that sent it.

    The launcher cannot see the connection, and the shared failure flag
    says nothing about which provider set it, so the identity has to be
    attached where the frame arrives (wh-launch-generation).
    """

    @staticmethod
    def _manager():
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=asyncio.get_running_loop())
        # The toast path needs a state manager; these tests are about
        # the stamp, so leave it out and let that path be skipped.
        manager.state_manager = None
        return manager

    @staticmethod
    def _client():
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.send = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_a_connection_is_stamped_with_the_launch_that_spawned_it(self):
        manager = self._manager()
        launcher = MagicMock()
        launcher.current_launch_generation.return_value = 7
        manager.remote_stt_launcher = launcher
        ws = self._client()

        await manager.add_client(ws)

        assert manager._stt_client_generations[ws] == 7

    @pytest.mark.asyncio
    async def test_the_stamp_leaves_with_the_connection(self):
        manager = self._manager()
        launcher = MagicMock()
        launcher.current_launch_generation.return_value = 7
        manager.remote_stt_launcher = launcher
        ws = self._client()
        await manager.add_client(ws)

        manager.remove_client(ws)

        assert ws not in manager._stt_client_generations

    @pytest.mark.asyncio
    async def test_a_startup_failure_carries_the_senders_launch_not_the_current_one(
        self,
    ):
        manager = self._manager()
        # The replacement has been spawned but has not connected yet, so
        # the provider being replaced is still the active client and its
        # frame passes the non-active-client gate.
        current = {"generation": 1}
        launcher = MagicMock()
        launcher.current_launch_generation.side_effect = lambda: current["generation"]
        launcher.launch_generation.side_effect = lambda name: (
            1 if name == "parakeet_tdt" else None
        )
        manager.remote_stt_launcher = launcher

        async def _frames():
            current["generation"] = 2
            # Every shipped provider declares itself immediately after
            # connecting, which is what binds this connection to the
            # launch that spawned it (wh-launch-generation.1.2).
            yield json.dumps(
                {
                    "type": "capabilities",
                    "provider": "parakeet_tdt",
                    "emits_eos": False,
                }
            )
            yield json.dumps(
                {
                    "type": "notification",
                    "title": "Parakeet",
                    "message": "model failed to load",
                    "kind": "startup_failed",
                }
            )

        ws = self._client()
        ws.__aiter__ = Mock(return_value=_frames())

        await manager.handle_connection(ws)

        launcher.signal_provider_startup_failed.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_a_late_connection_is_bound_to_the_launch_that_spawned_it(self):
        """A provider connects after a LATER launch was already stamped.

        Regression test named by reviewer_0 finding
        wh-launch-generation.1.2: the connect-time stamp reads whichever
        launch is newest, but a provider connects seconds after its own
        spawn, so a slow provider from launch 5 that finally connects
        while launch 6 is starting is stamped 6 and its own startup
        failure is attributed to launch 6.
        """
        manager = self._manager()
        launcher = MagicMock()
        # Launch 6 is the newest, so the connect-time stamp is 6 -- but
        # this connection belongs to launch 5, which spawned parakeet.
        launcher.current_launch_generation.return_value = 6
        launcher.launch_generation.side_effect = lambda name: {
            "parakeet_tdt": 5,
            "google": 6,
        }.get(name)
        manager.remote_stt_launcher = launcher

        async def _frames():
            # Every shipped provider sends this immediately after the
            # connection opens (shared_stt/ws_forwarder.py), so it
            # arrives before any failure the provider reports.
            yield json.dumps(
                {
                    "type": "capabilities",
                    "provider": "parakeet_tdt",
                    "emits_eos": False,
                }
            )
            yield json.dumps(
                {
                    "type": "notification",
                    "title": "Parakeet",
                    "message": "model failed to load",
                    "kind": "startup_failed",
                }
            )

        ws = self._client()
        ws.__aiter__ = Mock(return_value=_frames())

        await manager.handle_connection(ws)

        launcher.signal_provider_startup_failed.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_a_failure_from_an_undeclared_connection_is_dropped(self):
        """A connection that never said which provider it is has only a
        provisional stamp, so its failure cannot be attributed to a
        launch. Attributing it to the connect-time guess is the defect
        this change removes, and guessing by name would put name
        identity back, so the signal is dropped (wh-launch-generation.1.2)
        and recorded against the provisional stamp instead. The launch
        that was starting at the time reports the stop when its own
        monitor gives up; the monitor's live-subprocess branch cannot do
        that without the record, which is what
        wh-launch-generation.2.3 found."""
        manager = self._manager()
        launcher = MagicMock()
        launcher.current_launch_generation.return_value = 6
        manager.remote_stt_launcher = launcher

        async def _frames():
            # No capabilities message: this provider never declares
            # itself, so the stamp stays provisional.
            yield json.dumps(
                {
                    "type": "notification",
                    "title": "Parakeet",
                    "message": "model failed to load",
                    "kind": "startup_failed",
                }
            )

        ws = self._client()
        ws.__aiter__ = Mock(return_value=_frames())

        await manager.handle_connection(ws)

        launcher.signal_provider_startup_failed.assert_not_called()
        # Dropping it is only half the behaviour: without the record,
        # the launch that was starting reads its provider's silence as
        # a slow cold start and never reports it
        # (wh-launch-generation.2.3).
        launcher.record_undeclared_startup_failure.assert_called_once_with(6)


class TestOneLaunchCannotDestroyAnothersSignal:
    """Two startup monitors are alive at once and must not collide.

    Regression test named by reviewer_0 finding
    wh-launch-generation.1.1. stop_provider only sends a shutdown
    command over the websocket, and the monitor thread is a local of
    start_provider that is never stored, joined, or cancelled, so an
    earlier launch's monitor is still waiting when the user switches
    away. The failure flag, the failure generation, and the ready event
    are one slot shared by every launch, so the earlier monitor can
    reach that slot first, find a stamp that is not its own, clear the
    whole slot, and leave the owning monitor with nothing to read.
    """

    def test_a_stale_monitor_does_not_swallow_the_current_launchs_failure(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        # Launch 1 started parakeet; the user then switched to google,
        # which is launch 2. Both subprocesses are alive, so neither
        # monitor's timeout branch reports a failure of its own.
        launcher._launch_generations = {"parakeet_tdt": 1, "google": 2}
        launcher._current_launch_generation = 2
        launcher._subprocesses = {
            "parakeet_tdt": _live_subprocess(),
            "google": _live_subprocess(),
        }
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        # Launch 2 reports its own startup failure.
        launcher.signal_provider_startup_failed(2)

        # Launch 1's monitor is still alive and reaches the shared slot
        # first. It must not consume a signal that is not its own.
        launcher._monitor_startup(
            "parakeet_tdt", "Parakeet", timeout=0.05, generation=1
        )
        # Launch 2's own monitor now runs and must still see its failure.
        launcher._monitor_startup("google", "Google", timeout=0.05, generation=2)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=2
        )


class TestAFailureAtTheDeadlineIsStillReconciled:
    """A startup failure that lands while the monitor's wait is timing
    out must still be reported, not dropped.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.2. The monitor gates everything on the wait's
    own return value: `ready = signal.event.wait(timeout)` and then
    `if ready:`. A failure that sets the slot at the deadline leaves
    `ready` False, so the monitor takes the timeout branch instead of
    the failure branch -- and when the provider's subprocess is still
    alive that branch only hides the working dialog and never calls
    _provider_stopped. The engine record then keeps naming a launch
    whose startup failed.
    """

    def test_a_failure_landing_at_the_deadline_still_reports_stopped(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        # A live subprocess is what makes the timeout branch silent: it
        # hides the dialog and returns without reporting (wh-v0q).
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        signal = _slot(launcher, 1)
        real_wait = signal.event.wait

        def _fail_at_the_deadline(timeout=None):
            """The provider reports its failure as the wait gives up.

            This is the interleaving the finding names: the slot is set
            after the wait has decided to return False, so the wait's
            own answer says 'not ready' while the slot says 'failed'.
            """
            answer = real_wait(timeout)
            launcher.signal_provider_startup_failed(1)
            return answer

        signal.event.wait = _fail_at_the_deadline

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )

    def test_a_failure_arriving_after_the_monitor_finished_still_reports(
        self, tmp_path
    ):
        """The other half of the same finding: the failure lands after
        the monitor has already given up.

        The monitor removed its own slot, so the failure signal built a
        fresh slot that no monitor will ever read, and the launch stayed
        recorded as running. Nothing else reports it: the live-
        subprocess timeout branch is silent by design (wh-v0q), and the
        monitor has returned. The signal itself is the only party left
        that can tell the rest of the program, and it makes that report
        on a thread of its own so the bounded survivor wait cannot block
        the event loop (wh-launch-generation.2.5).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        # The monitor runs to its deadline and gives up quietly, because
        # the subprocess is still alive.
        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)
        launcher._provider_stopped.assert_not_called()

        # The provider now reports that its startup failed.
        launcher.signal_provider_startup_failed(1)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )


class TestADroppedFailureStillReachesItsOwnLaunch:
    """A failure that cannot be attributed is dropped as a signal, but
    the launch that was starting at the time must still learn of it.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.3. The websocket manager drops a
    kind="startup_failed" from a connection that never declared its
    provider, and the crewcut beside that drop claimed the owning
    launch's own monitor would report it on timeout. It does not: the
    monitor's timeout branch treats a live subprocess as a slow cold
    start (wh-v0q) and returns without reporting. Nothing else sets
    _provider_ready_event on that path either, so is_starting read true
    for the rest of the session and the startup suppression swallowed
    every later notice from the provider -- the exact defect
    wh-google-creds-file-picker.1.5 exists for.
    """

    def test_a_recorded_undeclared_failure_ends_the_slow_start_excuse(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        # Alive, which is what makes the timeout branch silent (wh-v0q).
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        # A connection that never said which provider it is reported a
        # failed startup while launch 1 was the one starting.
        launcher.record_undeclared_startup_failure(1)

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )
        assert launcher.is_starting is False

    def test_an_unreported_launch_still_gets_the_slow_start_benefit(
        self, tmp_path
    ):
        """The opposite case, so the fix cannot become 'always report'.

        With no recorded failure, a live subprocess still means the
        provider is warming up, and the monitor must stay silent
        (wh-v0q).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)

        launcher._provider_stopped.assert_not_called()

    def test_a_failure_recorded_against_another_launch_is_not_borrowed(
        self, tmp_path
    ):
        """A record belongs to the launch it was stamped against.

        Launch 1's dropped failure must not make launch 2's monitor
        report, or the record would recreate the cross-launch
        attribution this whole change removes.
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"parakeet_tdt": 1, "google": 2}
        launcher._current_launch_generation = 2
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher.record_undeclared_startup_failure(1)

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=2)

        launcher._provider_stopped.assert_not_called()


def _dead_subprocess():
    proc = MagicMock()
    proc.poll.return_value = 0
    return proc


def _join_orphan_reports(launcher):
    """Wait for the reports the launcher handed to its own threads.

    An orphaned failure is reported off the event loop thread so the
    bounded survivor wait cannot block it, so a test that asserts on the
    callback has to wait for that thread (wh-launch-generation.2.5).
    """
    for thread in list(getattr(launcher, "_orphan_report_threads", ())):
        thread.join(timeout=5)
        assert not thread.is_alive(), "an orphan report thread did not finish"


class TestALateUndeclaredFailureStillReachesItsLaunch:
    """A dropped failure recorded AFTER its monitor gave up must still
    be reported.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.4. The monitor reads the undeclared-failure
    record at its deadline and then returns; a record written after that
    read has no reader left, because `record_undeclared_startup_failure`
    only adds to a set. It never signals readiness and never reports the
    provider stopped, so the launch stays recorded as running and
    is_starting stays true -- the same stranded state
    wh-launch-generation.2.3 was opened for, one ordering later.
    """

    def test_a_record_made_after_the_monitor_gave_up_still_reports_stopped(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        # Alive, which is what makes the timeout branch silent (wh-v0q).
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        # The monitor reaches its deadline with nothing recorded and
        # gives the provider the slow-cold-start benefit.
        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)
        launcher._provider_stopped.assert_not_called()

        # Only now does the connection's startup_failed reach the event
        # loop, and the websocket manager drops it as unattributable.
        launcher.record_undeclared_startup_failure(1)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )
        assert launcher.is_starting is False


class TestOneLaunchIsReportedOnce:
    """The monitor and the failure signal must not both report the same
    launch, and the one report that happens must reconcile survivors.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.5. The monitor marks its slot closed when it
    samples it, but claims the report much later -- after hiding the
    working dialog and sending the timeout notification. A failure
    arriving in that window sees a closed, unclaimed slot, reports the
    launch itself without waiting, and the engine record is cleared; the
    monitor's own report then follows and its survivor write is refused,
    because the record it compares against is already gone. The tray is
    left showing nothing running while another engine transcribes, which
    is exactly wh-remote-stt-robustness.2.5.
    """

    def test_a_failure_between_closing_the_slot_and_reporting_reports_once(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        # Dead, so the monitor takes the branch that notifies and then
        # reports -- the branch with the widest window.
        launcher._subprocesses = {"google": _dead_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()

        def _fail_inside_the_window(*args, **kwargs):
            """The provider's failure lands while the monitor notifies.

            This is the interleaving the finding names: after the slot
            was closed, before the monitor claimed its report.
            """
            launcher.signal_provider_startup_failed(1)

        launcher._notify = MagicMock(side_effect=_fail_inside_the_window)

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)
        _join_orphan_reports(launcher)

        # Exactly one report, and it is the monitor's -- the only one
        # that reconciles a surviving engine.
        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )

    def test_an_orphaned_failure_reconciles_survivors(self, tmp_path):
        """The other half of the same finding: when the failure signal
        IS the only reporter, its report must still wait and scan.

        Reporting without the wait skips main.py's survivor
        reconciliation entirely, so the tray shows nothing running while
        the engine the switch replaced is still transcribing
        (wh-remote-stt-robustness.2.5).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)
        launcher._provider_stopped.assert_not_called()

        launcher.signal_provider_startup_failed(1)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )


class TestARecordEndsOnlyItsOwnLaunchsStartup:
    """Consuming a record for an earlier launch must not end the
    starting state of the launch that replaced it.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.6. The undeclared-failure branch sets the
    shared `_provider_ready_event` with no generation comparison, while
    the failure signal guards the same write. A record stamped against
    launch N and consumed by N's monitor after the user has switched to
    N+1 therefore reports N+1 as no longer starting, and the websocket
    manager's startup suppression stops holding N+1's notices back.
    """

    def test_an_earlier_launchs_record_leaves_the_new_launch_starting(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1, "parakeet_tdt": 2}
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        # Launch 1's connection reported a failure it could not name.
        launcher._current_launch_generation = 1
        launcher.record_undeclared_startup_failure(1)

        # The user switches to another provider before launch 1's
        # monitor reaches its deadline, so launch 2 is now the one
        # starting.
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()

        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)
        _join_orphan_reports(launcher)

        # Launch 1 is still reported stopped -- both engine-record write
        # sites compare the generation, so the report is harmless.
        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )
        # But launch 2 is still starting.
        assert launcher.is_starting is True

    def test_a_late_record_for_an_earlier_launch_leaves_the_new_one_starting(
        self, tmp_path
    ):
        """The same guard on the other consumer of the record.

        When the record arrives after its monitor has gone, the record
        itself makes the report, and it must apply the same generation
        comparison -- otherwise the path added for
        wh-launch-generation.2.4 reintroduces .2.6 through a second
        door.
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1, "parakeet_tdt": 2}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        # Launch 1's monitor gives up quietly on a live subprocess.
        launcher._monitor_startup("google", "Google", timeout=0.01, generation=1)
        launcher._provider_stopped.assert_not_called()

        # The user switches to another provider, and only then does
        # launch 1's dropped failure reach the event loop.
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()
        launcher.record_undeclared_startup_failure(1)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )
        assert launcher.is_starting is True


class TestAMonitorActsOnlyForItsOwnLaunch:
    """A monitor whose launch has been replaced must not read the new
    launch's process, and must not touch the display it owns.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.7. `_subprocesses` is keyed by provider NAME
    and `start_provider` overwrites that key on every restart, so the
    liveness check answers for whichever launch of the provider is the
    most recent one -- the fourth place in this file where a name stood
    in for a launch. The working dialog and the failure notice are
    shared by every provider in the same way, and nothing compared the
    generation before touching them, so a monitor for a launch the user
    had already replaced could dismiss the new launch's loading display
    and announce "Failed to start" for a provider that was starting
    normally.
    """

    def test_a_restart_of_the_same_provider_does_not_lend_its_process(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        # The name-keyed handle holds the RESTART's live process, which
        # says nothing about the launch this monitor supervises.
        launcher._launch_generations = {"google": 2}
        launcher._current_launch_generation = 2
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup(
            "google",
            "Google",
            timeout=0.01,
            generation=1,
            process=_dead_subprocess(),
        )

        # Launch 1's own process is gone, so launch 1 is reported
        # stopped. Both engine-record write sites compare the
        # generation, so the report cannot reach launch 2's record.
        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )

    def test_a_replaced_launch_does_not_announce_a_failure_for_the_new_one(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 2}
        launcher._current_launch_generation = 2
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup(
            "google",
            "Google",
            timeout=0.01,
            generation=1,
            process=_dead_subprocess(),
        )

        # The display belongs to launch 2, which is starting normally.
        launcher._notify.assert_not_called()
        launcher._hide_working.assert_not_called()

    def test_the_launch_that_is_still_current_still_gets_its_notice(
        self, tmp_path
    ):
        """The opposite case, so the guard cannot become 'never tell
        the user'.

        A launch that is still the current one failed, and the notice
        and the dialog are the only signals the user gets.
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _dead_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup(
            "google",
            "Google",
            timeout=0.01,
            generation=1,
            process=_dead_subprocess(),
        )

        launcher._hide_working.assert_called_once_with(1)
        launcher._notify.assert_called_once_with(
            "Google", "Failed to start - try restarting Wheelhouse", 1
        )


class TestTheMonitorIsGivenItsOwnChild:
    """start_provider hands the monitor the child it just spawned.

    The plumbing half of wh-launch-generation.2.7. A monitor can only
    watch its own launch's process if the launch that spawned it names
    that process; reading it back out of `_subprocesses` by provider
    name is the defect, because a restart overwrites that key.
    """

    def test_start_provider_passes_the_spawned_child_to_the_monitor(
        self, tmp_path
    ):
        import time
        from unittest.mock import patch

        from stt.remote_stt_launcher import RemoteSTTLauncher

        service = tmp_path / "providers" / "google_stt_server"
        service.mkdir(parents=True)
        (service / "config.toml").write_text(
            '[provider]\nname = "google_stt"\n'
            'display_name = "Google Cloud STT"\nlauncher = "launcher.py"\n'
        )
        (service / "launcher.py").write_text("# launcher stub")

        launcher = RemoteSTTLauncher(
            services_dir=tmp_path / "providers",
            app_data_dir=tmp_path / "appdata",
            ws_port=5500,
        )
        launcher._monitor_startup = MagicMock()

        with patch("subprocess.Popen") as popen:
            child = MagicMock()
            child.pid = 4242
            popen.return_value = child

            assert launcher.start_provider("google_stt") is True

        # The monitor runs on its own thread, so wait for the call
        # rather than reading it straight after start_provider returns.
        deadline = time.monotonic() + 5
        while (
            not launcher._monitor_startup.called
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        assert launcher._monitor_startup.called
        assert launcher._monitor_startup.call_args.args[-1] is child


class TestAReplacedLaunchsFailureLeavesTheDisplayAlone:
    """The websocket half of the shared-display rule (wh-launch-generation.2.8).

    `_monitor_startup` learned to compare the launch before touching the
    working dialog or the failure notice (wh-launch-generation.2.7), but
    the websocket manager has its own path to both, and it took neither
    comparison. A provider being replaced keeps the active connection
    until the replacement connects, so its `startup_failed` frame passes
    the non-active-client gate and reaches a `hide_working` that blanks
    the REPLACEMENT'S loading dialog, and a toast that is exempt from the
    startup suppression by design (wh-google-creds-file-picker.1.5).
    """

    @staticmethod
    def _state_manager():
        notifier = MagicMock()
        notifier._send_notification = MagicMock()
        state_manager = MagicMock()
        state_manager.speech_notifier = notifier
        return state_manager, notifier

    @staticmethod
    def _client(frames):
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.send = AsyncMock()
        ws.__aiter__ = Mock(return_value=frames)
        return ws

    @staticmethod
    def _launcher(current):
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.side_effect = lambda: current[
            "generation"
        ]
        launcher.launch_generation.side_effect = lambda name: (
            1 if name == "parakeet_tdt" else None
        )
        launcher.launch_is_current.side_effect = lambda generation: (
            generation is None or generation == current["generation"]
        )
        return launcher

    @staticmethod
    def _frames(
        current,
        replacement_generation,
        kind="startup_failed",
        message="model failed to load",
    ):
        async def _gen():
            # The replacement is stamped while the provider it replaces
            # is still the only connected one.
            current["generation"] = replacement_generation
            yield json.dumps(
                {
                    "type": "capabilities",
                    "provider": "parakeet_tdt",
                    "emits_eos": False,
                }
            )
            yield json.dumps(
                {
                    "type": "notification",
                    "title": "Parakeet",
                    "message": message,
                    "kind": kind,
                }
            )

        return _gen()

    @staticmethod
    def _manager(state_manager, launcher):
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=asyncio.get_running_loop())
        manager.state_manager = state_manager
        manager.remote_stt_launcher = launcher
        return manager

    @pytest.mark.asyncio
    async def test_a_replaced_launchs_failure_does_not_hide_the_new_dialog(
        self,
    ):
        current = {"generation": 1}
        launcher = self._launcher(current)
        state_manager, _ = self._state_manager()
        manager = self._manager(state_manager, launcher)

        await manager.handle_connection(
            self._client(self._frames(current, 2))
        )

        # The signal itself is still delivered against the sending
        # launch -- only the shared display is left alone.
        launcher.signal_provider_startup_failed.assert_called_once_with(1)
        # Matched on the action alone, never on the whole message. The
        # dismiss carries a generation since 698f4a08, so an assertion
        # that looks for the bare {"action": "hide_working"} dict is
        # true whatever the code does, and the full sweep caught this
        # one passing under a mutation that removed the guard
        # (wh-launch-addressed-notices).
        assert [
            call.args[0]
            for call in state_manager.state_to_gui_queue.put_nowait.call_args_list
            if call.args
            and isinstance(call.args[0], dict)
            and call.args[0].get("action") == "hide_working"
        ] == []

    @pytest.mark.asyncio
    async def test_a_replaced_launchs_failure_does_not_toast(self):
        current = {"generation": 1}
        launcher = self._launcher(current)
        state_manager, notifier = self._state_manager()
        manager = self._manager(state_manager, launcher)

        await manager.handle_connection(
            self._client(self._frames(current, 2))
        )

        notifier._send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_current_launchs_failure_still_hides_and_toasts(self):
        """The opposite case, so the guard cannot become "never tell the user".

        Nothing replaced this launch, so its failure is the only signal
        the engine is not coming up and both halves must still fire
        (wh-google-creds-file-picker.1.5).
        """
        current = {"generation": 1}
        launcher = self._launcher(current)
        state_manager, notifier = self._state_manager()
        manager = self._manager(state_manager, launcher)

        await manager.handle_connection(
            self._client(self._frames(current, 1))
        )

        launcher.signal_provider_startup_failed.assert_called_once_with(1)
        state_manager.state_to_gui_queue.put_nowait.assert_any_call(
            {"action": "hide_working", "owner": "stt:1"}
        )
        notifier._send_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_replaced_launchs_runtime_error_does_not_toast(self):
        """The other kind the startup suppression exempts.

        The suppression exempts "startup_failed" AND "error", because a
        real failure is the only signal the engine is not coming up
        (wh-google-creds-file-picker.1.5). Both are equally wrong to
        show for a launch the user has already replaced, and the check
        added for wh-launch-generation.2.8 covered only the first, so a
        kind="error" frame skipped the check for not being
        startup_failed and skipped the suppression for being exempt
        (wh-launch-generation.2.12). google_stt_server sends this kind
        from report_streamer_start_failure.
        """
        current = {"generation": 1}
        launcher = self._launcher(current)
        state_manager, notifier = self._state_manager()
        manager = self._manager(state_manager, launcher)

        await manager.handle_connection(
            self._client(
                self._frames(
                    current,
                    2,
                    kind="error",
                    message="Speech engine error - transcription is not working",
                )
            )
        )

        notifier._send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_current_launchs_runtime_error_still_toasts(self):
        """The opposite case for kind="error".

        Nothing replaced this launch, so its error is the only signal
        the engine has stopped working and it must still reach the user
        even while `is_starting` is true.
        """
        current = {"generation": 1}
        launcher = self._launcher(current)
        state_manager, notifier = self._state_manager()
        manager = self._manager(state_manager, launcher)

        await manager.handle_connection(
            self._client(
                self._frames(
                    current,
                    1,
                    kind="error",
                    message="Speech engine error - transcription is not working",
                )
            )
        )

        notifier._send_notification.assert_called_once()


class TestOnlyAReadyFrameCompletesStartup:
    """An error is not a ready signal, whatever words it contains.

    The legacy fallback accepts any notification whose message contains
    "ready", and the comment above it already listed "not ready" among
    the strings it wrongly accepts. A provider's structured
    kind="error" carries arbitrary exception text -- google_stt_server's
    report_streamer_start_failure appends `type(exc).__name__: exc` --
    so an exception saying "stream is not ready" completed the startup
    it was reporting the failure of: the launcher was told ready, the
    loading display closed, and the error never reached the user
    (wh-launch-generation.2.13).

    The two opposite cases below are what keep the fix from becoming
    "nothing completes startup". The kind="ready" case matters most:
    that frame passes TODAY only by matching "Ready." in its own text,
    so a fix that merely restricted the fallback to unclassified frames
    would silently break the one provider that sends the structured
    kind.
    """

    @staticmethod
    def _parts():
        notifier = MagicMock()
        notifier._send_notification = MagicMock()
        state_manager = MagicMock()
        state_manager.speech_notifier = notifier
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.side_effect = lambda: 1
        launcher.launch_generation.side_effect = lambda name: (
            1 if name == "parakeet_tdt" else None
        )
        launcher.launch_is_current.side_effect = lambda generation: (
            generation is None or generation == 1
        )
        return state_manager, notifier, launcher

    @staticmethod
    async def _deliver(state_manager, launcher, kind, message):
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=asyncio.get_running_loop())
        manager.state_manager = state_manager
        manager.remote_stt_launcher = launcher

        async def _gen():
            yield json.dumps(
                {
                    "type": "capabilities",
                    "provider": "parakeet_tdt",
                    "emits_eos": False,
                }
            )
            frame = {
                "type": "notification",
                "title": "Parakeet",
                "message": message,
            }
            if kind is not None:
                frame["kind"] = kind
            yield json.dumps(frame)

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.send = AsyncMock()
        ws.__aiter__ = Mock(return_value=_gen())
        await manager.handle_connection(ws)

    @pytest.mark.asyncio
    async def test_a_structured_error_does_not_complete_startup(self):
        state_manager, notifier, launcher = self._parts()

        await self._deliver(
            state_manager,
            launcher,
            "error",
            "Speech engine error - transcription is not working: "
            "RuntimeError: stream is not ready",
        )

        launcher.signal_provider_ready.assert_not_called()
        # The error is the only signal the engine stopped working, so it
        # must still reach the user (wh-google-creds-file-picker.1.5).
        notifier._send_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_structured_ready_frame_still_completes_startup(self):
        """The frame google_stt_server sends after a credentials reload."""
        state_manager, _notifier, launcher = self._parts()

        await self._deliver(
            state_manager, launcher, "ready", "Service restart completed. Ready."
        )

        launcher.signal_provider_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_kind_less_ready_message_no_longer_completes_startup(self):
        """Readiness is carried by kind alone, never by the message text.

        This test asserted the opposite until
        wh-ready-connection-stamp.2.2.1, and its docstring said every
        provider except google_stt_server sends no kind. Both stopped
        being true in the same commit: distil_medium_en and the sherpa
        parakeet provider now send kind="ready", and the substring
        fallback that read a kind-less notice is gone. The fallback also
        matched "already", so every provider's "Hint '<word>' already
        exists" notice completed the launch and never reached the user.
        """
        state_manager, _notifier, launcher = self._parts()

        await self._deliver(
            state_manager, launcher, None, "Parakeet model loaded and ready"
        )

        launcher.signal_provider_ready.assert_not_called()


class TestASwitchCannotLandInsideTheGenerationGuard:
    """Comparing the generation and ending the starting state must be
    one step, not two.

    Regression test named by reviewer_1 finding
    wh-launch-generation.2.14, widened to all three sites of the same
    shape. Each site asks whether its launch is still the current one
    and then sets the shared `_provider_ready_event`, while
    `start_provider` stamps the next generation and clears that same
    event -- and neither side held a lock. A switch landing between the
    comparison and the set therefore passed the .2.6 guard and then
    ended the REPLACEMENT's starting state, so the websocket manager's
    startup suppression stopped holding the replacement's notices back
    while it was still loading.

    Each test drives the real `start_provider` for the replacement
    rather than a copy of its stamp, so the test contends with the
    production code and cannot pass by imitating it.
    """

    @staticmethod
    def _launcher_with_two_providers(tmp_path):
        from stt.remote_stt_launcher import RemoteSTTLauncher

        for directory, name, display in (
            ("google_stt_server", "google_stt", "Google Cloud STT"),
            ("parakeet_server", "parakeet_tdt", "Parakeet TDT"),
        ):
            service = tmp_path / "providers" / directory
            service.mkdir(parents=True)
            (service / "config.toml").write_text(
                f'[provider]\nname = "{name}"\n'
                f'display_name = "{display}"\nlauncher = "launcher.py"\n'
            )
            (service / "launcher.py").write_text("# launcher stub")

        launcher = RemoteSTTLauncher(
            services_dir=tmp_path / "providers",
            app_data_dir=tmp_path / "appdata",
            ws_port=5500,
        )
        launcher._monitor_startup = MagicMock()
        launcher._show_working = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._provider_stopped = MagicMock()
        return launcher

    @staticmethod
    def _switch_at_the_guarded_write(launcher, replacement):
        """Start a real replacement launch at the guarded write itself.

        The wrapper fires on the first `_provider_ready_event.set()`,
        which is the write every one of the three sites makes after its
        generation comparison. Without the fix the replacement runs to
        completion inside that gap -- it stamps the next generation and
        clears the event -- and the original `set()` then lands on the
        replacement's starting state. With the fix the replacement
        blocks on the lock the guarded block holds, this wait expires,
        and the replacement clears the event afterwards.

        The wait is what makes the ordering deterministic in both
        directions, so no test here depends on losing a race by timing.
        """
        import threading

        original_set = launcher._provider_ready_event.set
        threads: "list[threading.Thread]" = []

        def set_and_race():
            if not threads:
                thread = threading.Thread(
                    target=lambda: launcher.start_provider(replacement),
                    daemon=True,
                )
                threads.append(thread)
                thread.start()
                thread.join(timeout=2.0)
            original_set()

        launcher._provider_ready_event.set = set_and_race
        return threads

    @staticmethod
    def _finish(launcher, threads):
        del launcher._provider_ready_event.set
        assert threads, "the replacement launch never started"
        threads[0].join(timeout=10)
        assert not threads[0].is_alive(), "the replacement launch never finished"
        _join_orphan_reports(launcher)

    def test_a_switch_during_the_monitors_guard_leaves_the_new_launch_starting(
        self, tmp_path
    ):
        from unittest.mock import patch

        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = self._launcher_with_two_providers(tmp_path)
        with patch("subprocess.Popen") as popen:
            popen.return_value = _live_subprocess()
            assert launcher.start_provider("google_stt") is True
            assert launcher._current_launch_generation == 1

            # Launch 1's connection reported a failure it could not
            # name, which launch 1's own monitor consumes at its
            # deadline.
            launcher.record_undeclared_startup_failure(1)
            threads = self._switch_at_the_guarded_write(launcher, "parakeet_tdt")
            RemoteSTTLauncher._monitor_startup(
                launcher, "google_stt", "Google", timeout=0.01, generation=1
            )
            self._finish(launcher, threads)

        assert launcher._current_launch_generation == 2
        assert launcher.is_starting is True

    def test_a_switch_during_the_failure_signals_guard_leaves_it_starting(
        self, tmp_path
    ):
        from unittest.mock import patch

        launcher = self._launcher_with_two_providers(tmp_path)
        with patch("subprocess.Popen") as popen:
            popen.return_value = _live_subprocess()
            assert launcher.start_provider("google_stt") is True

            threads = self._switch_at_the_guarded_write(launcher, "parakeet_tdt")
            launcher.signal_provider_startup_failed(generation=1)
            self._finish(launcher, threads)

        assert launcher._current_launch_generation == 2
        assert launcher.is_starting is True

    def test_a_switch_during_the_late_records_guard_leaves_it_starting(
        self, tmp_path
    ):
        from unittest.mock import patch

        from stt.remote_stt_launcher import RemoteSTTLauncher

        launcher = self._launcher_with_two_providers(tmp_path)
        with patch("subprocess.Popen") as popen:
            popen.return_value = _live_subprocess()
            assert launcher.start_provider("google_stt") is True

            # Launch 1's monitor gives up quietly on a live subprocess
            # and closes its slot, so the record arriving afterwards is
            # the party that reports the stop -- and the party that
            # makes the guarded write.
            RemoteSTTLauncher._monitor_startup(
                launcher, "google_stt", "Google", timeout=0.01, generation=1
            )
            launcher._provider_stopped.assert_not_called()

            threads = self._switch_at_the_guarded_write(launcher, "parakeet_tdt")
            launcher.record_undeclared_startup_failure(1)
            self._finish(launcher, threads)

        assert launcher._current_launch_generation == 2
        assert launcher.is_starting is True


class TestAReadyReachesOnlyItsOwnLaunch:
    """A ready from a superseded launch must not complete the launch
    that replaced it (wh-ready-connection-stamp).

    The launch-generation branch merged at caed53e6 bound the FAILURE
    signal to the connection that carried it and left the READY signal
    unbound: `signal_provider_ready` took no generation and set the slot
    of whichever launch was current, plus the shared
    `_provider_ready_event` with no comparison at all. Its own docstring
    said so, naming the open question (QUESTIONS-2026-08-29 item 24),
    which David answered (a) on 2026-08-30.

    The provider being replaced keeps the active connection until the
    replacement connects, so its queued ready frame passes the
    non-active-client gate. It then ended the REPLACEMENT'S starting
    state and returned the replacement's monitor as though the
    replacement had reported ready. A replacement that then died before
    connecting had no monitor left to report it, so the user saw the
    replacement selected and running while dictation still came from the
    provider it replaced.
    """

    def test_an_earlier_launchs_ready_leaves_the_new_launch_starting(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1, "parakeet_tdt": 2}

        # Launch 2 replaced launch 1 and is the one starting now.
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()

        # Launch 1's provider delivers its queued ready over the
        # connection it still holds.
        launcher.signal_provider_ready(1)

        assert launcher.is_starting is True

    def test_an_earlier_launchs_ready_does_not_wake_the_new_monitor(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1, "parakeet_tdt": 2}
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()

        launcher.signal_provider_ready(1)

        # Launch 2's own slot is untouched, so its monitor is still
        # waiting for its own provider.
        assert _slot(launcher, 2).event.is_set() is False
        # Launch 1's slot is the one that was set.
        assert _slot(launcher, 1).event.is_set() is True

    def test_the_current_launchs_ready_still_completes_it(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"parakeet_tdt": 2}
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()

        launcher.signal_provider_ready(2)

        assert launcher.is_starting is False
        assert _slot(launcher, 2).event.is_set() is True

    def test_a_ready_with_no_generation_still_completes_the_current_launch(
        self, tmp_path
    ):
        """A connection the manager could not stamp keeps the behaviour
        the ready path had before it carried a generation at all."""
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"parakeet_tdt": 2}
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()

        launcher.signal_provider_ready()

        assert launcher.is_starting is False
        assert _slot(launcher, 2).event.is_set() is True

    def test_an_earlier_launchs_ready_does_not_end_the_new_monitors_wait(
        self, tmp_path
    ):
        """The whole path, through the monitor that reads the slot.

        The slot assertions above name the mechanism; this names the
        outcome the user gets. Launch 2's monitor must not return as
        though its own provider had reported ready.
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1, "parakeet_tdt": 2}
        launcher._current_launch_generation = 2
        launcher._provider_ready_event.clear()
        launcher._subprocesses = {"parakeet_tdt": _dead_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher.signal_provider_ready(1)
        launcher._monitor_startup(
            "parakeet_tdt", "Parakeet", timeout=0.01, generation=2
        )

        # Launch 2's child is dead and never reported ready, so its
        # monitor must reach the dead-child branch and report it
        # stopped. Borrowing launch 1's ready sent it to the silent
        # "startup confirmed" return instead.
        launcher._provider_stopped.assert_called_once_with(
            "parakeet_tdt", may_wait=True, generation=2
        )


class TestAReplacedLaunchsReadyCarriesItsOwnConnection:
    """The websocket half of the same rule (wh-ready-connection-stamp).

    `signal_provider_startup_failed` is already called with the sending
    connection's stamp; the ready branch called `signal_provider_ready`
    with nothing, so the launcher could only guess at the current
    launch. The connection's stamp is the same one the failure branch
    reads, corrected from the provider's own capabilities message.
    """

    @pytest.mark.asyncio
    async def test_a_replaced_launchs_ready_signals_its_own_launch(self):
        shared = TestAReplacedLaunchsFailureLeavesTheDisplayAlone
        current = {"generation": 1}
        launcher = shared._launcher(current)
        state_manager, _notifier = shared._state_manager()
        manager = shared._manager(state_manager, launcher)

        await manager.handle_connection(
            shared._client(
                shared._frames(
                    current, 2, kind="ready", message="Parakeet ready"
                )
            )
        )

        launcher.signal_provider_ready.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_the_current_launchs_ready_still_carries_its_stamp(self):
        """The stamp is passed whatever it is, so the launcher makes the
        comparison in one place rather than two."""
        shared = TestAReplacedLaunchsFailureLeavesTheDisplayAlone
        current = {"generation": 1}
        launcher = shared._launcher(current)
        state_manager, _notifier = shared._state_manager()
        manager = shared._manager(state_manager, launcher)

        await manager.handle_connection(
            shared._client(
                shared._frames(
                    current, 1, kind="ready", message="Parakeet ready"
                )
            )
        )

        launcher.signal_provider_ready.assert_called_once_with(1)


class TestEveryProviderDeclaresTheNameItIsLaunchedUnder:
    """The stamp can only be corrected when the two names are the same.

    `_rebind_launch_stamp` moves a connection off its connect-time guess
    by asking the launcher which launch of the DECLARED provider is the
    latest, and `launch_generation` answers None for a name the launcher
    never started. The launcher keys those generations by the name in
    the service's config.toml, so a provider that announces any other
    name is never rebound: its connection keeps the guess for its whole
    life, and a ready arriving on it completes whichever launch was
    current when it connected (wh-ready-connection-stamp.1.1).

    The parakeet provider announced `sherpa_offline_parakeet` while
    WheelHouse launched it as `parakeet_tdt`, which is the default
    provider, so this held for every parakeet connection. Approved by
    David 2026-08-30 (QUESTIONS-2026-08-30 item 1).
    """

    @pytest.mark.parametrize(
        "service",
        [
            "google_stt_server",
            "distil_medium_en",
            "sherpa_offline_parakeet_stt_server",
        ],
    )
    def test_the_declared_name_matches_the_manifest_name(self, service):
        service_dir = _PROVIDERS_DIR / service
        assert _declared_provider_name(service_dir) == (
            _manifest_provider_name(service_dir)
        )


def _ready_notice_kind(service_dir: Path) -> str:
    """The kind a provider puts on its "Transcription service ready".

    Read from the source for the same reason _declared_provider_name is:
    the provider services have their own virtual environments, so nothing
    in this suite can import them or run their tests, and a value written
    here instead would keep passing after the provider changed.
    """
    source = (service_dir / "main.py").read_text(encoding="utf-8")
    # The message may carry a suffix on the same line, as parakeet's
    # "Transcription service ready" + self._hotwords_notice() does; the
    # kind keyword must still be the next argument.
    #
    # It need not be the LAST argument. It was until
    # wh-capture-winrt-required A4 added capture_backend after it, and
    # this reader's closing `\s*\)` then matched nothing, so both
    # providers read as sending no kind at all while both plainly sent
    # one. Anything after the kind is allowed now; what is pinned is
    # that a call carrying the ready message names its kind.
    match = re.search(
        r'send_notification\(\s*[^)]*?"Transcription service ready"[^\n]*,\s*'
        r'kind="([^"]+)"[^)]*\)',
        source,
    )
    assert match is not None, (
        f"{service_dir.name}/main.py sends no ready notice carrying a kind"
    )
    return match.group(1)


class TestEveryProviderClassifiesItsReadyNotice:
    """WheelHouse routes readiness on kind, and only on kind.

    The websocket manager used to accept any kind-less notice whose text
    contained "ready". "already" contains "ready", so every provider's
    "Hint '<word>' already exists" notice completed the launch and never
    reached the user, and so did a failed hint save whose word contained
    "ready". Removing that fallback is safe only while every provider
    classifies its own ready, which is what this pins
    (wh-ready-connection-stamp.2.2.1).

    google_stt_server is absent on purpose: it builds its notice as a
    (title, message, kind) triple and passes kind=kind, a different shape
    this reader would not match. Its kind is pinned instead by
    test_each_shipped_providers_ready_completes_its_launch in
    test_websocket_manager.py and by its own suite.
    """

    @pytest.mark.parametrize(
        "service",
        [
            "distil_medium_en",
            "sherpa_offline_parakeet_stt_server",
        ],
    )
    def test_the_ready_notice_declares_kind_ready(self, service):
        assert _ready_notice_kind(_PROVIDERS_DIR / service) == "ready"


class TestASlowProvidersReadyDoesNotCompleteTheLaunchThatReplacedIt:
    """The reachable sequence behind wh-ready-connection-stamp.1.1.

    One session, the default provider, and a plain switch:

    1. Launch 1 starts parakeet, which is slow -- the GPU cold start the
       slow-start branch of `_monitor_startup` exists for.
    2. The user switches to another provider, launch 2. Parakeet has not
       connected yet.
    3. Parakeet connects DURING launch 2 and is stamped 2.
    4. Parakeet announces itself. Only a name the launcher recognises
       moves the stamp back to launch 1.
    5. Parakeet sends its ready. With the stamp still at 2 it completes
       launch 2, whose own provider never reported ready.

    The name is read from the provider's source rather than written
    here, so this fails if the provider stops announcing the name the
    launcher knows it by.
    """

    @pytest.mark.asyncio
    async def test_a_late_parakeet_ready_reaches_its_own_launch(self):
        shared = TestAReplacedLaunchsFailureLeavesTheDisplayAlone
        declared = _declared_provider_name(
            _PROVIDERS_DIR / "sherpa_offline_parakeet_stt_server"
        )
        # The replacement is already the current launch when the slow
        # provider finally connects, so the connect-time guess is 2.
        current = {"generation": 2}
        launcher = shared._launcher(current)
        state_manager, _notifier = shared._state_manager()
        manager = shared._manager(state_manager, launcher)

        async def _frames():
            yield json.dumps(
                {
                    "type": "capabilities",
                    "provider": declared,
                    "emits_eos": False,
                }
            )
            # The provider's own ready text and the kind it carries,
            # both copied from
            # services/stt_providers/sherpa_offline_parakeet_stt_server/
            # main.py:596. It sent no kind until
            # wh-ready-connection-stamp.2.2.1 and reached the launcher
            # through the substring fallback, which that commit removed.
            # test_the_ready_notice_declares_kind_ready in this file
            # fails if the provider stops sending this kind.
            yield json.dumps(
                {
                    "type": "notification",
                    "title": "Parakeet v3",
                    "message": "Transcription service ready",
                    "kind": "ready",
                }
            )

        await manager.handle_connection(shared._client(_frames()))

        launcher.signal_provider_ready.assert_called_once_with(1)


def _dying_subprocess():
    """A child alive when the monitor polls it and dead every poll after.

    The monitor polls once, at its deadline, and that one answer is what
    sends it down the slow-start branch. Every later answer is the late
    exit this bead is about, so the two are pinned in one object and no
    test has to time a flip against a running thread
    (wh-provider-late-death-watch).
    """
    proc = MagicMock()
    answers = [None]

    def poll():
        return answers.pop(0) if answers else 1

    proc.poll.side_effect = poll
    return proc


def _join_late_death_watches(launcher):
    """Wait for the liveness watches the launcher started.

    The slow-start branch hands its child to a watch thread of its own
    so `_monitor_startup` still returns at its deadline. Every test
    above that reaches that branch -- a live child, no ready signal,
    no recorded failure -- depends on that contract, because an inline
    watch would hold the monitor for `_late_death_watch_limit`
    instead.

    No count is given here on purpose. This docstring said four; the
    measured number was nine, and any number goes stale as soon as a
    test is added. To measure it, count the tests above this helper
    that make `_monitor_startup` create a `_watch_for_late_death`
    thread (wh-provider-late-death-watch.2.6).

    A test asserting on a late death therefore has to wait for that
    thread, exactly as `_join_orphan_reports` waits for an orphaned
    report (wh-provider-late-death-watch).
    """
    # The timeout is far above any watch bound a test sets. When the two
    # are equal, a watch that runs to its bound races the join, and the
    # failure a mutation earns can be this assertion rather than the
    # missing report the test names (wh-provider-late-death-watch.2.2).
    for thread in list(getattr(launcher, "_late_death_watch_threads", ())):
        thread.join(timeout=15)
        assert not thread.is_alive(), "a late-death watch thread did not finish"


class TestARecordWhoseReaderHasGoneIsDropped:
    """The undeclared-failure record is dropped once its slot closes.

    The record has exactly one reader: `_monitor_startup_body` at its
    deadline, immediately before it closes the slot. A record written
    after that read can never be read again, so the arm that finds an
    evicted slot discards it and says so. The arm for a slot still in
    the ring tied its discard to making the report instead, which left
    the record behind for the life of the launcher in the two cases
    where no report is made (wh-launch-signal-eviction.1.2).
    """

    def test_a_record_is_dropped_when_its_launch_was_already_reported(
        self, tmp_path
    ):
        """The report was claimed at the deadline; the record still goes.

        `reported` True is the monitor saying it made the report itself,
        so this arm has nothing to report -- but the record it was
        handed still has no reader left.
        """
        launcher = _launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        signal = _slot(launcher, generation)
        signal.closed = True
        signal.reported = True

        launcher.record_undeclared_startup_failure(generation)

        assert generation not in launcher._undeclared_startup_failures, (
            "a record for an already-reported launch was left behind"
        )

    def test_a_record_is_dropped_when_its_launch_can_no_longer_be_named(
        self, tmp_path
    ):
        """No name means no report, and the record still has no reader.

        A later launch of the same provider takes the name, so
        `_provider_of_generation` cannot recover it
        (wh-launch-generation.2.2).
        """
        launcher = _launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        signal = _slot(launcher, generation)
        signal.closed = True
        _stamp_launch(launcher, "google")

        launcher.record_undeclared_startup_failure(generation)

        assert generation not in launcher._undeclared_startup_failures, (
            "a record for an unnameable launch was left behind"
        )


class TestAChildThatDiesAfterItsMonitorGaveUp:
    """A pre-ready child that exits after the bounded wait is reported.

    The startup monitor decides its whole outcome once, at its deadline.
    The slow-start branch is the one that decides nothing: a child still
    alive there is assumed to be warming up (wh-v0q), so the monitor
    hides the working dialog and returns without claiming a report. Until
    this bead nothing watched that child afterwards, so a provider that
    died a second later left the user with a dead engine selected, no
    failure notice, and a dialog that had already closed
    (wh-launch-generation.2.10, wh-provider-late-death-watch).
    """

    def _watching_launcher(self, tmp_path, generations, current):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = dict(generations)
        launcher._current_launch_generation = current
        # Short enough that the watch answers within a test, long enough
        # that a loaded machine does not hit the limit before the poll.
        launcher._late_death_poll_interval = 0.01
        launcher._late_death_watch_limit = 5.0
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        return launcher

    def test_a_watch_that_cannot_start_gives_its_slot_back(self, tmp_path):
        """A watch thread that never starts must not pin its slot.

        The claim for the late-death watch is taken under the signal
        lock, and its only release is the watch's own `finally`, which a
        thread that never runs never reaches. A leaked hold is
        permanent: `_evict_unowned_slots` skips every held slot, so one
        failed thread start raises the ring's floor for the life of the
        launcher (wh-launch-signal-eviction.1.1).
        """
        import threading
        from unittest.mock import patch

        launcher = self._watching_launcher(tmp_path, {}, None)
        # Stamped the way `start_provider` stamps, so the monitor holds
        # the slot it is about to be given. Without that hold the
        # wrapper's own release lands on the watch's claim instead and
        # hides the leak this test exists for.
        generation = _stamp_launch(launcher, "google")
        # Alive at the deadline, which is what sends the monitor down
        # the slow-start branch that starts the watch (wh-v0q).
        proc = _live_subprocess()
        launcher._subprocesses = {"google": proc}

        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("no threads")
        ):
            with pytest.raises(RuntimeError):
                launcher._monitor_startup(
                    "google",
                    "Google",
                    timeout=0.01,
                    generation=generation,
                    process=proc,
                )

        assert _slot(launcher, generation).owners == 0, (
            "the watch's hold outlived a thread that never started"
        )

    def test_a_late_death_reports_stopped_like_an_in_window_death(
        self, tmp_path
    ):
        """The acceptance criterion: same handling as a death in window.

        The dead-child branch hides the dialog, names the provider in a
        generic failure notice, and reports it stopped carrying its own
        generation. A death one moment later must produce all three.
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        proc = _dying_subprocess()
        launcher._subprocesses = {"google": proc}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )
        launcher._notify.assert_called_once_with(
            "Google", "Failed to start - try restarting Wheelhouse", 1
        )
        # Two callers, one call each: the monitor's slow-start branch
        # hid the dialog at its deadline, and the watch hides it again
        # when the child dies. The count is what makes this docstring's
        # "all three" true, because the whole path runs here and a
        # single-call assertion could not tell the two apart. The
        # watch's own call is pinned by itself in
        # test_the_watch_hides_the_working_dialog_for_its_own_launch
        # (wh-provider-late-death-watch.1.4).
        assert launcher._hide_working.call_count == 2

    def test_the_watch_hides_the_working_dialog_for_its_own_launch(
        self, tmp_path
    ):
        """The third leg of the in-window match, pinned on its own.

        The acceptance-criterion test above runs the whole path through
        `_monitor_startup`, whose slow-start branch hides the dialog
        too, so no assertion there can name which caller made a given
        call. Calling the watch directly leaves one caller, so
        `assert_called_once_with` names the watch's own hide.

        Without this the hide could be deleted from the watch's report
        block and every test in this class still passed: the two
        assertions that mention the dialog here are both negative, and
        the positive pin that exists for the in-window branch covers
        the monitor's copy (wh-provider-late-death-watch.1.4).
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        signal = _slot(launcher, 1)

        launcher._watch_for_late_death(
            "google", "Google", signal, _dead_subprocess(), 1
        )

        launcher._hide_working.assert_called_once_with(1)

    def test_a_superseded_launchs_late_death_shows_no_notice(self, tmp_path):
        """A launch the user has already replaced owns none of the display.

        The notice and the working dialog are shared by every provider,
        so a late death belonging to a replaced launch must stay silent
        or it announces a failure for the healthy provider that is
        starting now (wh-launch-generation.2.7). The stopped report is
        still made, carrying the dead launch's own generation, because
        the engine record compares that stamp and refuses a report from
        a launch it has moved past.
        """
        launcher = self._watching_launcher(
            tmp_path, {"google": 1, "parakeet_tdt": 2}, 2
        )
        proc = _dying_subprocess()
        launcher._subprocesses = {"google": proc}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=1
        )
        launcher._notify.assert_not_called()
        launcher._hide_working.assert_not_called()

    def test_a_failure_frame_after_a_late_death_does_not_report_twice(
        self, tmp_path
    ):
        """One launch is reported once, whichever party gets there first.

        The monitor left the slot closed and unclaimed, which is how a
        late `startup_failed` learns that it must report the launch
        itself. The watch reaches the same slot, so without a claim the
        two would both report the same launch -- the double report whose
        second survivor write is refused, leaving the tray stopped while
        another engine transcribes (wh-launch-generation.2.5).
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        proc = _dying_subprocess()
        launcher._subprocesses = {"google": proc}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        launcher.signal_provider_startup_failed(generation=1)
        _join_orphan_reports(launcher)

        assert launcher._provider_stopped.call_count == 1

    def test_a_child_that_goes_ready_late_is_never_reported(self, tmp_path):
        """The slow-start benefit survives the watch.

        A GPU cold start can outlast the ready timeout while the
        provider is perfectly healthy, and the whole point of the
        slow-start branch is to stay quiet for it (wh-v0q). A watch that
        reported such a provider would turn that benefit into the false
        alarm it was written to remove.
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        proc = _live_subprocess()
        launcher._subprocesses = {"google": proc}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        launcher.signal_provider_ready(generation=1)
        _join_late_death_watches(launcher)

        launcher._provider_stopped.assert_not_called()
        launcher._notify.assert_not_called()

    def test_a_child_that_exits_after_going_ready_is_no_startup_failure(
        self, tmp_path
    ):
        """A provider that started and later stopped did not fail to start.

        A clean shutdown inside the watch window looks exactly like a
        late death from the outside: a pre-ready child and one that had
        already announced itself differ only in whether the launch's own
        slot was set. Reading that slot is what keeps the watch from
        telling the user a healthy provider failed to start.

        The watch is called directly, with the slot already set, because
        setting it from the test thread after the monitor returns would
        race the watch's first poll -- and a test that sometimes drives
        the branch it names proves nothing on the runs where it does not.
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        signal = _slot(launcher, 1)
        signal.event.set()

        launcher._watch_for_late_death(
            "google", "Google", signal, _dead_subprocess(), 1
        )

        launcher._provider_stopped.assert_not_called()
        launcher._notify.assert_not_called()

    def test_a_ready_slot_ends_the_watch_before_it_polls(self, tmp_path):
        """The set slot is the watch's exit for a launch that succeeded.

        A healthy provider goes ready and then runs for hours, so the
        wait at the top of the loop is what ends its watch at the moment
        it announces itself. Without that wait the watch would keep
        polling a live child until its own limit -- one lingering thread
        per slow start -- and the in-lock recheck cannot end it either,
        because that recheck is reached only after the child has exited.

        Polling is therefore the observable, and the assertion is on
        `poll`, not on the absence of a report: a slot that is already
        set must end the watch before it looks at the child at all. The
        older test that watched for the absent report can no longer show
        this, because the .2.3 recheck also suppresses that report
        (wh-provider-late-death-watch.2.3).
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        launcher._late_death_watch_limit = 0.2
        signal = _slot(launcher, 1)
        signal.event.set()
        proc = _live_subprocess()

        launcher._watch_for_late_death("google", "Google", signal, proc, 1)

        proc.poll.assert_not_called()
        launcher._provider_stopped.assert_not_called()

    def test_a_child_that_is_still_alive_is_never_reported(self, tmp_path):
        """The safety half of the liveness check.

        The watch exists to notice an exit, so a child that has NOT
        exited must reach the end of the watch with no notice and no
        stopped report. Otherwise every genuine slow start -- the case
        the branch was written for -- would announce a failure the user
        does not have.

        The watch is called directly, with a slot that is never set and
        a child that answers None to every poll, so the bound is the
        only way out. The bound is set far below the join timeout: a
        test whose watch bound equals the join timeout can fail on the
        join instead of on the report, which is what made the live-child
        mutation prove nothing (wh-provider-late-death-watch.2.2).
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        launcher._late_death_watch_limit = 0.2
        signal = _slot(launcher, 1)

        launcher._watch_for_late_death(
            "google", "Google", signal, _live_subprocess(), 1
        )

        launcher._provider_stopped.assert_not_called()
        launcher._notify.assert_not_called()
        launcher._hide_working.assert_not_called()

    def test_a_ready_that_lands_while_the_watch_polls_is_no_failure(
        self, tmp_path
    ):
        """A launch that announced itself is never a startup failure.

        The watch reads its slot at the top of the loop and then polls
        the child. A ready landing between those two steps is invisible
        to that read, and `signal_provider_ready` sets the slot without
        claiming the report, so the claim's `reported` check does not
        see it either. The watch would then call a launch that had
        already succeeded a startup failure, and change the selected
        engine to stopped after the user was told it was ready. The
        window is a thread-scheduling window, not a few instructions
        (wh-provider-late-death-watch.2.3).

        The ready is driven from inside `poll` so the order is fixed
        rather than timed: a test that only sometimes reaches the race
        proves nothing on the runs where it does not.
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        signal = _slot(launcher, 1)
        proc = MagicMock()

        def poll():
            launcher.signal_provider_ready(1)
            return 0

        proc.poll.side_effect = poll

        launcher._watch_for_late_death("google", "Google", signal, proc, 1)

        launcher._provider_stopped.assert_not_called()
        launcher._notify.assert_not_called()

    def test_the_watch_holds_no_lock_across_its_report(self, tmp_path):
        """The acceptance criterion: no blocking work under the lock.

        `_notify` ends in a Win32 notification of unbounded duration and
        `_provider_stopped` ends in a bounded survivor wait. Holding
        this class's lock across either would stall `start_provider` and
        the event loop thread, which is the reason `launch_is_current`
        is deliberately read without one (wh-launch-generation.2.9). The
        probe runs on a thread of its own because the lock is an RLock:
        the watch thread could re-acquire it and prove nothing.
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        free = []

        def probe(*_args, **_kwargs):
            result = {}

            def acquire():
                got = launcher._launch_signals_lock.acquire(blocking=False)
                result["free"] = got
                if got:
                    launcher._launch_signals_lock.release()

            thread = threading.Thread(target=acquire)
            thread.start()
            thread.join(timeout=5)
            free.append(result.get("free"))

        launcher._notify = MagicMock(side_effect=probe)
        launcher._provider_stopped = MagicMock(side_effect=probe)
        proc = _dying_subprocess()
        launcher._subprocesses = {"google": proc}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        assert free == [True, True]

    def test_the_watch_list_drops_threads_that_have_finished(self, tmp_path):
        """A launcher that runs for hours must not accumulate handles.

        Every slow start appends one watch thread, and nothing in
        production joins them. `_report_stopped_off_loop` already drops
        finished threads at each append, with that reason written beside
        it; this list holds the same kind of handle. A slow start is the
        GPU cold-start case this bead exists for (wh-v0q), so a tray
        application that cold-starts a provider once an hour would keep
        every dead thread object for the rest of the session
        (wh-provider-late-death-watch.1.5).
        """
        launcher = self._watching_launcher(tmp_path, {"google": 1}, 1)
        first = _dying_subprocess()
        launcher._subprocesses = {"google": first}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=first
        )
        _join_late_death_watches(launcher)

        finished = list(launcher._late_death_watch_threads)
        assert len(finished) == 1
        assert not finished[0].is_alive()

        launcher._launch_generations = {"google": 2}
        launcher._current_launch_generation = 2
        second = _dying_subprocess()
        launcher._subprocesses = {"google": second}

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=2, process=second
        )
        # Read before joining the second watch: the assertion is about
        # what the append dropped, not about what a later join leaves.
        remaining = list(launcher._late_death_watch_threads)
        _join_late_death_watches(launcher)

        assert finished[0] not in remaining


def _stamp_launch(launcher, provider_name="other", hold=True):
    """One launch, stamped exactly as `start_provider` stamps one.

    `start_provider` advances the counter, records the generation
    against the provider name, makes it current, and creates that
    launch's signal slot -- all inside ONE acquisition of the signal
    lock (remote_stt_launcher.py:1417-1424). The order matters to the
    tests below: a generation is never issued without a slot, which is
    what lets `_launch_signal` tell a launch it has evicted from one it
    has never seen. Spawning a real child would add nothing these tests
    read, so the stamp block is reproduced and the Popen is not.

    The hold is taken here too, because `start_provider` takes it in
    that same locked step and the whole subject of these tests is what
    the cap may drop. `_monitor_startup` gives it back, so a launch
    stamped here whose monitor never runs keeps its hold -- which is
    what a launch still starting looks like, and what the eviction
    assertions below rely on. `hold=False` stamps a launch nobody is
    waiting on, for the tests that need the cap to have a slot it may
    drop.
    """
    with launcher._launch_signals_lock:
        launcher._launch_generation_counter += 1
        generation = launcher._launch_generation_counter
        launcher._launch_generations[provider_name] = generation
        launcher._current_launch_generation = generation
        launcher._open_launch_signal(generation)
        if hold:
            launcher._claim_launch_signal(generation)
    return generation


class TestAnEvictedSlotDoesNotStrandItsOwner:
    """A launch still learns its own outcome after eight later launches.

    The signal ring holds at most `_MAX_TRACKED_LAUNCH_SIGNALS` slots
    and a ninth launch drops the oldest. Every party that reads a slot
    takes the object ONCE and holds it: `_monitor_startup` for its whole
    bounded wait, and `_watch_for_late_death` for `_late_death_watch_limit`
    after that. `_launch_signal` could not tell a generation it had
    evicted from one it had never seen, so it built a FRESH slot for
    either, and four things followed. The held object was never set, so
    the owner never woke. The fresh slot was unowned, so nobody read it.
    The fresh slot carried `closed=False`, so the orphan report that a
    closed slot exists to trigger was skipped. And nothing anywhere
    detected the state (wh-launch-signal-eviction).

    Reachability is a person, not a loop: eight `start_provider` calls
    inside one wait is somebody switching providers repeatedly, or
    retrying a provider that will not start.
    """

    def _quiet_watch(self, launcher):
        """Bound the late-death watch so it gives up without claiming.

        The slow-start branch hands a live child to a watch thread. A
        watch whose child never exits and never signals runs to its
        limit and returns WITHOUT claiming the report, which is the
        state these tests need: the slot is closed, unclaimed, and no
        longer owned. A dying child would take the late-death branch
        instead and claim the report, which is a different test.
        """
        launcher._late_death_poll_interval = 0.01
        launcher._late_death_watch_limit = 0.02

    def _monitored_launcher(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        self._quiet_watch(launcher)
        return launcher

    def test_an_owner_still_learns_its_outcome_after_nine_later_launches(
        self, tmp_path
    ):
        """Criterion 1. The ready reaches the slot the monitor is holding.

        Nine launches inside one wait is one more than the ring holds,
        so the owner's generation is the one eviction would take. The
        monitor is still waiting on that object, so a ready that reaches
        a different object is a ready the owner never sees.
        """
        launcher = self._monitored_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        signal = _slot(launcher, generation)
        real_wait = signal.event.wait

        def _nine_launches_then_ready(timeout=None):
            for _ in range(9):
                _stamp_launch(launcher)
            launcher.signal_provider_ready(generation)
            return real_wait(timeout)

        signal.event.wait = _nine_launches_then_ready

        launcher._monitor_startup(
            "google", "Google", timeout=0.5, generation=generation
        )
        _join_late_death_watches(launcher)

        assert signal.event.is_set() is True, (
            "the ready went to a slot the waiting monitor does not hold, so "
            "the owner never learned the outcome of its own launch"
        )

    def test_the_monitor_closes_the_slot_the_ring_still_holds(self, tmp_path):
        """Criterion 4, the monitor half.

        `_monitor_startup` writes `closed = True` on the object it took
        at the start of its wait. Writing that on an object the ring no
        longer holds tells nobody anything: the next reader builds a
        fresh slot whose `closed` is False.
        """
        launcher = self._monitored_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        signal = _slot(launcher, generation)
        real_wait = signal.event.wait
        # Read DURING the wait, not after it. The monitor gives its hold
        # back as it returns, and a slot nobody holds is exactly the
        # kind the cap is meant to drop -- so a read taken afterwards
        # asks a different question than this test's name.
        held_during_the_wait = []
        launched = []

        def _nine_launches(timeout=None):
            # The monitor waits more than once, and the late-death watch
            # waits again after it. The launches belong to the FIRST
            # wait only -- what the later waits show is that the slot is
            # still held, not that nine more launches evict it -- and a
            # fixed count keeps this loop from ever running long under a
            # mutation.
            if not launched:
                launched.append(True)
                for _ in range(9):
                    _stamp_launch(launcher)
            held_during_the_wait.append(
                launcher._launch_signals.get(generation)
            )
            return real_wait(timeout)

        signal.event.wait = _nine_launches

        launcher._monitor_startup(
            "google", "Google", timeout=0.05, generation=generation
        )
        _join_late_death_watches(launcher)

        assert held_during_the_wait, "the monitor never reached its wait"
        assert all(held is signal for held in held_during_the_wait), (
            "the ring dropped the slot its monitor was holding, so the "
            "closed flag the monitor wrote is invisible to every later "
            f"reader; the reads taken during the wait were "
            f"{held_during_the_wait}"
        )

    def test_a_late_ready_for_an_evicted_launch_builds_no_unowned_slot(
        self, tmp_path
    ):
        """Criterion 2. Nobody is left to read a slot built now.

        This launch has no owner: its monitor finished and its watch
        gave up. Eviction is therefore correct here, and the defect is
        what happens NEXT -- a ready that arrives afterwards used to
        build a fresh slot, which evicts a real one to make room and is
        itself read by nobody.
        """
        launcher = self._monitored_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        for _ in range(9):
            _stamp_launch(launcher)
        assert generation not in launcher._launch_signals, (
            "the setup did not evict the launch these assertions are about"
        )
        before = list(launcher._launch_signals)

        launcher.signal_provider_ready(generation)

        assert list(launcher._launch_signals) == before, (
            "a ready for an evicted launch built a slot nobody will read, "
            "and dropped a live launch's slot to make room for it"
        )

    def test_a_late_failure_for_an_evicted_launch_still_reports_the_orphan(
        self, tmp_path
    ):
        """Criterion 3. The closed-slot handoff survives eviction.

        A monitor that gives up on a live child leaves its slot closed
        and unclaimed. That pair is the whole handoff: it tells a late
        failure that nobody is left to report this launch, so the
        failure reports it itself (wh-launch-generation.2.2). A fresh
        slot carries `closed=False`, so eviction silently cancelled the
        handoff and the launch stayed recorded as running.
        """
        launcher = self._monitored_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        assert launcher._launch_signals[generation].closed is True
        assert launcher._launch_signals[generation].reported is False
        for _ in range(9):
            _stamp_launch(launcher)
        assert generation not in launcher._launch_signals

        launcher.signal_provider_startup_failed(generation)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=generation
        )

    def test_a_late_undeclared_failure_for_an_evicted_launch_still_reports(
        self, tmp_path
    ):
        """Criterion 4, the `record_undeclared_startup_failure` half.

        The same handoff, reached by the other path: a connection that
        reported a failed startup without saying which provider it is.
        It reads the slot for the launch that was starting when the
        connection arrived, and a fresh slot hides the closed flag from
        it exactly as it does from the failure signal.
        """
        launcher = self._monitored_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        for _ in range(9):
            _stamp_launch(launcher)
        assert generation not in launcher._launch_signals

        launcher.record_undeclared_startup_failure(generation)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=generation
        )
        # The record is read at exactly one place, the monitor's
        # deadline, and that deadline has passed. Leaving it behind
        # strands it for the life of the launcher.
        assert generation not in launcher._undeclared_startup_failures, (
            "the undeclared-failure record outlived the only reader it "
            "has, so nothing will ever take it out again"
        )


class TestTheHoldThatKeepsASlotReachable:
    """The three guards that decide which slot the cap may drop.

    Each one is a separate way for a launch to lose the slot its own
    outcome will be written to, and each fails silently: the launch waits
    out its timeout on an object nothing will ever set
    (wh-launch-signal-eviction).
    """

    def _launcher_with_service(self, tmp_path):
        """A launcher whose google_stt service is real enough to start.

        `start_provider` reads the provider's own config.toml and its
        launcher script, so the tests that drive the real method rather
        than the stamp helper need both on disk.
        """
        service = tmp_path / "providers" / "google_stt_server"
        service.mkdir(parents=True)
        (service / "config.toml").write_text(
            '[provider]\nname = "google_stt"\n'
            'display_name = "Google Cloud STT"\nlauncher = "launcher.py"\n'
        )
        (service / "launcher.py").write_text("# launcher stub")
        from stt.remote_stt_launcher import RemoteSTTLauncher

        # A websocket port, which `_launcher` leaves unset: without one
        # `start_provider` refuses before it reaches the stamp, and the
        # hold this test is about is taken in that same step.
        return RemoteSTTLauncher(
            services_dir=tmp_path / "providers",
            app_data_dir=tmp_path / "appdata",
            ws_port=5500,
        )

    def test_a_new_launch_keeps_the_slot_it_was_just_given(self, tmp_path):
        """A launch must not evict ITSELF to satisfy the cap.

        The owner takes its hold in the step after the slot is created,
        so for that moment the new slot is the only unowned one in the
        ring. When every older slot is held, a scan for "the oldest slot
        nobody holds" finds the new one and drops it -- the launch ends
        up with no slot at all, and its outcome is lost exactly as it is
        when an older slot is dropped from under its owner.
        """
        launcher = _launcher(tmp_path)
        held = [_stamp_launch(launcher) for _ in range(8)]
        assert len(launcher._launch_signals) == len(held)

        generation = _stamp_launch(launcher, "google")

        assert generation in launcher._launch_signals, (
            "the launch dropped its own slot to make room for itself, so "
            "nothing will ever tell it how it went"
        )
        assert launcher._launch_signal(generation) is not None

    def test_start_provider_holds_the_slot_before_the_monitor_begins(
        self, tmp_path
    ):
        """The hold belongs to the step that stamps the launch.

        The Popen, the port-file write and the monitor thread's own
        start all sit between the stamp and the monitor's first line. A
        hold taken inside the monitor would leave the slot droppable
        across that whole span, so `start_provider` takes it under the
        same lock as the stamp. The monitor is stubbed here precisely
        because a real one would take over the hold and hide its absence.
        """
        from unittest.mock import patch

        launcher = self._launcher_with_service(tmp_path)
        launcher._monitor_startup = MagicMock()

        with patch("subprocess.Popen") as popen:
            child = MagicMock()
            child.pid = 4242
            popen.return_value = child
            assert launcher.start_provider("google_stt") is True

        generation = launcher._launch_generations["google_stt"]
        # Eight launches nobody waits on. They give the cap something it
        # may drop, so the scan reaches a decision about this launch
        # instead of finding no candidate and stopping.
        for _ in range(8):
            _stamp_launch(launcher, hold=False)

        assert generation in launcher._launch_signals, (
            "the cap dropped the slot before the monitor even started, so "
            "the launch that is starting right now can no longer be told "
            "how it went"
        )

    def test_an_evicted_launch_is_reported_once_however_many_failures_come(
        self, tmp_path
    ):
        """The debt is spent as it is claimed.

        While a slot is alive, `reported` is what stops two failures for
        one launch producing two orphan reports
        (wh-launch-generation.2.5). The record that carries the handoff
        across an eviction has to give the same guarantee, so it is
        removed by the claim rather than read and left behind.
        """
        launcher = _launcher(tmp_path)
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._late_death_poll_interval = 0.01
        launcher._late_death_watch_limit = 0.02
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        for _ in range(9):
            _stamp_launch(launcher)
        assert generation not in launcher._launch_signals

        launcher.signal_provider_startup_failed(generation)
        launcher.signal_provider_startup_failed(generation)
        _join_orphan_reports(launcher)

        launcher._provider_stopped.assert_called_once_with(
            "google", may_wait=True, generation=generation
        )

_REFUSED_STARTS = [
    pytest.param(RuntimeError, id="runtimeerror"),
    pytest.param(MemoryError, id="memoryerror"),
]
"""Every exception `Thread.start` can raise before the thread runs.

Read from the interpreter that runs this suite, CPython 3.12.10, in
its standard library file Lib/threading.py:
`_limbo[self] = self` and `_start_new_thread(...)` are the only
statements before the new thread exists, and the allocations either one
performs raise MemoryError, while `_start_new_thread` raises RuntimeError
when the process cannot create another native thread. Ids carry no space,
so the mutation gate can name each case as a catcher
(wh-launch-signal-eviction.2.2).
"""

class TestAReportThatCannotStartIsNotSpent:
    """A one-shot report survives a reporter thread that cannot start.

    Both callers of `_report_stopped_off_loop` spend something
    irrevocable before it runs: `signal.reported` for a slot still in the
    ring, and the eviction debt for a slot the cap has dropped. The
    reporter's `Thread.start()` was unguarded, so a start that raises
    lost the only report that launch would ever get and left the failed
    provider recorded as running (wh-launch-signal-eviction.2.1).

    Each test runs for every refusal in `_REFUSED_STARTS`. The first
    guard named RuntimeError alone, which left the MemoryError a thread
    start raises under memory exhaustion escaping exactly as before
    (wh-launch-signal-eviction.2.2).
    """

    def _reporting_launcher(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._late_death_poll_interval = 0.01
        launcher._late_death_watch_limit = 0.02
        return launcher

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_failed_start_leaves_an_in_ring_report_still_owed(
        self, tmp_path, refusal
    ):
        """The slot is still in the ring, so `reported` is what was spent.

        A second failure for the same launch must still report it, which
        it can only do while `reported` is False.
        """
        from unittest.mock import patch

        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        assert _slot(launcher, generation).closed is True
        assert _slot(launcher, generation).reported is False

        with patch.object(
            threading.Thread,
            "start",
            side_effect=refusal("the thread could not start"),
        ):
            launcher.signal_provider_startup_failed(generation)

        assert _slot(launcher, generation).reported is False, (
            "the report was spent by a reporter thread that never started"
        )

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed(
        self, tmp_path, refusal
    ):
        """The slot has gone, so the eviction debt is what was spent.

        The debt is the whole handoff for an evicted launch. Spending it
        on a report that was never made loses the launch entirely.
        """
        from unittest.mock import patch

        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        for _ in range(9):
            _stamp_launch(launcher)
        assert generation not in launcher._launch_signals
        assert generation in launcher._evicted_unreported

        with patch.object(
            threading.Thread,
            "start",
            side_effect=refusal("the thread could not start"),
        ):
            launcher.signal_provider_startup_failed(generation)

        assert generation in launcher._evicted_unreported, (
            "the eviction debt was spent by a reporter thread that never "
            "started"
        )

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_failed_start_does_not_abort_the_failure_signal(
        self, tmp_path, refusal
    ):
        """The caller finishes its own work after a refused start.

        `signal_provider_startup_failed` updates the shared startup state
        after it reports. An exception escaping the reporter skipped that
        update, so the launch stayed recorded as starting as well as
        unreported.
        """
        from unittest.mock import patch

        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)

        with patch.object(
            threading.Thread,
            "start",
            side_effect=refusal("the thread could not start"),
        ):
            launcher.signal_provider_startup_failed(generation)

        assert _slot(launcher, generation).failed is True, (
            "the failure signal did not finish its own work"
        )

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_refused_reporter_construction_leaves_an_in_ring_report_owed(
        self, tmp_path, refusal
    ):
        """Building the reporter allocates, so it can refuse before start.

        `Thread.__init__` builds an `Event` of its own (CPython 3.12.10,
        Lib/threading.py:935), calls `_make_invoke_excepthook()`, and
        registers the new object in `_dangling`, so exhaustion raises
        here rather than at `start()`. It also raises RuntimeError
        outright when daemon threads are disabled, and this reporter asks
        for `daemon=True`. The construction sat outside the guard, so
        either refusal escaped and the caller's already-spent report was
        lost (wh-launch-signal-eviction.2.3).
        """
        from unittest.mock import patch

        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        assert _slot(launcher, generation).reported is False

        with patch.object(
            threading.Thread,
            "__init__",
            side_effect=refusal("the thread could not be built"),
        ):
            launcher.signal_provider_startup_failed(generation)

        assert _slot(launcher, generation).reported is False, (
            "the report was spent by a reporter that was never built"
        )

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_refused_reporter_construction_leaves_an_evicted_debt_unclaimed(
        self, tmp_path, refusal
    ):
        """The evicted-launch half of the same escape.

        For a slot the cap has dropped, the debt in `_evicted_unreported`
        is the whole handoff, so a construction that refuses after the
        claim loses the launch entirely
        (wh-launch-signal-eviction.2.3).
        """
        from unittest.mock import patch

        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        for _ in range(9):
            _stamp_launch(launcher)
        assert generation not in launcher._launch_signals
        assert generation in launcher._evicted_unreported

        with patch.object(
            threading.Thread,
            "__init__",
            side_effect=refusal("the thread could not be built"),
        ):
            launcher.signal_provider_startup_failed(generation)

        assert generation in launcher._evicted_unreported, (
            "the eviction debt was spent by a reporter that was never built"
        )

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_refused_reporter_construction_does_not_abort_the_signal(
        self, tmp_path, refusal
    ):
        """The caller's own remaining work still runs.

        `signal_provider_startup_failed` sets `_provider_startup_failed`
        and ends the starting state AFTER its reporting loop, so an
        escape from the loop skipped both and left the launch recorded as
        still starting. This asserts on `_provider_startup_failed`
        rather than on `signal.failed`, which is already True before the
        loop begins and so cannot tell the two outcomes apart
        (wh-launch-signal-eviction.2.3).
        """
        from unittest.mock import patch

        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)
        assert launcher._provider_startup_failed is False

        with patch.object(
            threading.Thread,
            "__init__",
            side_effect=refusal("the thread could not be built"),
        ):
            launcher.signal_provider_startup_failed(generation)

        assert launcher._provider_startup_failed is True, (
            "the failure signal did not finish its own work"
        )

    @pytest.mark.parametrize("refusal", _REFUSED_STARTS)
    def test_a_refusal_while_pruning_finished_reporters_leaves_report_owed(
        self, tmp_path, refusal
    ):
        """The bookkeeping between construction and start allocates too.

        Before starting, the helper rebuilds `_orphan_report_threads`
        without its finished entries. That builds a new list and calls
        `is_alive()` on every handle, so it can refuse under the same
        exhaustion that refuses the start, and it sat outside the guard
        as well. Codex reported the construction alone; this pins the
        third region (wh-launch-signal-eviction.2.3).

        A handle whose `is_alive` refuses is the injection point, which
        needs no patch of the interpreter's own threading module.
        """
        launcher = self._reporting_launcher(tmp_path)
        generation = _stamp_launch(launcher, "google")
        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=generation
        )
        _join_late_death_watches(launcher)

        class _RefusingHandle:
            def is_alive(self):
                raise refusal("the prune could not allocate")

        launcher._orphan_report_threads.append(_RefusingHandle())
        assert _slot(launcher, generation).reported is False

        launcher.signal_provider_startup_failed(generation)

        assert _slot(launcher, generation).reported is False, (
            "the report was spent by a prune that refused before the start"
        )


class TestTheDialogNamesTheLaunchItBelongsTo:
    """Every message about the shared working dialog carries its launch.

    The launcher cannot decide by itself whether a dismiss is still
    wanted: it reads `launch_is_current` and acts afterwards, and a
    switch can land in between (wh-launch-generation.2.9). Naming the
    launch on the message moves the decision to the GUI, where the queue
    has already ordered the replacement's show against this hide
    (wh-launch-addressed-notices).
    """

    def _provider_tree(self, tmp_path):
        """A discoverable provider on disk, and a launcher that can start it.

        `start_provider` refuses before it reaches any of the code under
        test when no websocket port has been assigned, so the port is
        part of the setup rather than an incidental detail.
        """
        service = tmp_path / "providers" / "google_stt_server"
        service.mkdir(parents=True)
        (service / "config.toml").write_text(
            '[provider]\nname = "google_stt"\n'
            'display_name = "Google Cloud STT"\nlauncher = "launcher.py"\n'
        )
        (service / "launcher.py").write_text("# launcher stub")
        launcher = _launcher(tmp_path)
        launcher.ws_port = 5500
        return launcher

    def test_the_loading_dialog_names_the_launch_it_was_raised_for(
        self, tmp_path
    ):
        from unittest.mock import patch

        launcher = self._provider_tree(tmp_path)
        launcher._monitor_startup = MagicMock()
        launcher._show_working = MagicMock()

        with patch("subprocess.Popen") as popen:
            popen.return_value = _live_subprocess()
            assert launcher.start_provider("google_stt") is True

        launcher._show_working.assert_called_once_with(
            "Loading Google Cloud STT", launcher._current_launch_generation
        )
        assert launcher._current_launch_generation is not None, (
            "the dialog cannot name a launch that does not exist yet"
        )

    def test_a_failure_before_the_stamp_dismisses_with_no_launch(
        self, tmp_path
    ):
        """No launch exists yet, so the dismiss can only be unstamped.

        The dialog was never raised on this path either, so an unstamped
        dismiss is what today's behaviour already was.
        """
        launcher = self._provider_tree(tmp_path)
        launcher.wake_word_config = {"enabled": True, "model_dir": "models"}
        launcher._resolve_wake_word_model_dir = MagicMock(
            side_effect=OSError("no model directory")
        )
        launcher._hide_working = MagicMock()
        launcher._provider_stopped = MagicMock()

        assert launcher.start_provider("google_stt") is False

        launcher._hide_working.assert_called_once_with(None)

    def test_a_failure_after_the_stamp_dismisses_its_own_launch(
        self, tmp_path
    ):
        from unittest.mock import patch

        launcher = self._provider_tree(tmp_path)
        launcher._hide_working = MagicMock()
        launcher._provider_stopped = MagicMock()

        with patch("subprocess.Popen", side_effect=OSError("fail")):
            assert launcher.start_provider("google_stt") is False

        launcher._hide_working.assert_called_once_with(
            launcher._current_launch_generation
        )
        assert launcher._current_launch_generation == 1

    def test_the_dead_child_branch_dismisses_its_own_launch(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _dead_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1,
            process=_dead_subprocess(),
        )

        launcher._hide_working.assert_called_once_with(1)

    def test_the_slow_start_branch_dismisses_its_own_launch(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        proc = _live_subprocess()
        launcher._subprocesses = {"google": proc}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._late_death_watch_limit = 0.0

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        launcher._hide_working.assert_called_once_with(1)

    def test_the_slow_start_branch_ends_its_own_launchs_starting_state(
        self, tmp_path
    ):
        """A slow start must end the starting state, or it never ends.

        The branch hides the working dialog and returns, so the user
        already believes startup is over. Before this change it left
        `is_starting` true for the rest of the session, and the
        websocket manager's startup suppression then swallowed every
        later non-exempt notice from that provider. Nothing else ends
        the state once the monitor has returned: the provider's own
        ready is the only other path, and a provider whose ready was
        dropped or never sent never takes it
        (wh-ready-connection-stamp.2).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        proc = _live_subprocess()
        launcher._subprocesses = {"google": proc}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._late_death_watch_limit = 0.0
        launcher._provider_ready_event.clear()
        assert launcher.is_starting is True

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        assert launcher.is_starting is False

    def test_a_replaced_launchs_slow_start_leaves_the_new_startup_alone(
        self, tmp_path
    ):
        """A monitor whose launch was replaced must not end the
        replacement's starting state.

        `is_starting` is one flag shared by every provider. If launch 1's
        slow-start branch ended it while launch 2 was still loading, the
        websocket manager would stop holding launch 2's notices back
        mid-startup -- which is wh-launch-generation.2.6 reopened through
        the new call. `_end_starting_state` compares the generation under
        the lock, and this pins that the slow-start branch relies on that
        comparison rather than setting the event itself
        (wh-ready-connection-stamp.2).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 2
        proc = _live_subprocess()
        launcher._subprocesses = {"google": proc}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._late_death_watch_limit = 0.0
        launcher._provider_ready_event.clear()

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1, process=proc
        )
        _join_late_death_watches(launcher)

        assert launcher.is_starting is True
        launcher._hide_working.assert_not_called()

    def test_the_dead_child_branch_ends_its_own_launchs_starting_state(
        self, tmp_path
    ):
        """A child that dies inside the startup window must end the
        starting state, or it never ends.

        The branch hides the dialog, tells the user the engine failed to
        start, and reports the stop, so the visual startup is over. It
        left `is_starting` true, exactly as the slow-start branch did
        before wh-ready-connection-stamp.2: the failure branch above it
        ends the state for the reason recorded in
        wh-google-creds-file-picker.1.5, and that reason applies to this
        exit path equally. Until the next start_provider, the websocket
        manager's startup suppression swallowed every non-exempt notice
        from whatever provider was connected -- a surviving previous
        engine's hint notices, for instance
        (wh-ready-connection-stamp.2.1.2).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._subprocesses = {"google": _dead_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._provider_ready_event.clear()
        assert launcher.is_starting is True

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1,
            process=_dead_subprocess(),
        )

        assert launcher.is_starting is False

    def test_a_replaced_launchs_dead_child_leaves_the_new_startup_alone(
        self, tmp_path
    ):
        """The dead-child branch relies on the generation comparison too.

        Launch 1's child died while launch 2 was already loading. Ending
        the shared state directly would stop the websocket manager
        holding launch 2's notices back mid-startup
        (wh-launch-generation.2.6); the branch must go through
        `_end_starting_state`, which compares under the lock and refuses
        (wh-ready-connection-stamp.2.1.2).
        """
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 2
        launcher._subprocesses = {"google": _dead_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._provider_ready_event.clear()

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1,
            process=_dead_subprocess(),
        )

        assert launcher.is_starting is True
        launcher._hide_working.assert_not_called()

    def test_the_undeclared_failure_branch_dismisses_its_own_launch(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._undeclared_startup_failures = {1}
        launcher._subprocesses = {"google": _live_subprocess()}
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup(
            "google", "Google", timeout=0.01, generation=1,
            process=_live_subprocess(),
        )

        launcher._hide_working.assert_called_once_with(1)

    def test_a_declared_startup_failure_dismisses_its_own_launch(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._open_launch_signal(1)
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher.signal_provider_startup_failed(1)

        launcher._monitor_startup(
            "google", "Google", timeout=5, generation=1,
            process=_live_subprocess(),
        )

        launcher._hide_working.assert_called_once_with(1)

    def test_the_late_death_watch_dismisses_its_own_launch(self, tmp_path):
        launcher = _launcher(tmp_path)
        # Bounded like every other late-death test in this file. The
        # defaults are a 1.0s poll and a 300s limit, and this test left
        # them alone: passing, it broke out on the first poll and cost a
        # second, and under the gate's late-death liveness mutation the
        # dead child never breaks, so the loop waited out all 300s.
        # pytest's own timeout then aborted the whole run, which the
        # gate reports as an error rather than a verdict, and the
        # mutation's real catcher never got to run
        # (wh-launch-addressed-notices).
        launcher._late_death_poll_interval = 0.01
        launcher._late_death_watch_limit = 0.02
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._open_launch_signal(1)
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._watch_for_late_death(
            "google", "Google", _slot(launcher, 1), _dead_subprocess(), 1
        )

        launcher._hide_working.assert_called_once_with(1)

    def test_the_launcher_hands_the_dismiss_callback_its_launch(
        self, tmp_path
    ):
        """The forwarding itself, which no test above can see.

        Every test in this class replaces `_hide_working` with a
        MagicMock and reads the call site, so a body that dropped the
        number on the way to the callback would leave all of them
        green. The mutation entry for that line had no catcher, which
        is how the gap was found.
        """
        launcher = _launcher(tmp_path)
        hidden = []
        launcher.set_working_callback(
            show=lambda message, generation: None,
            hide=lambda generation: hidden.append(generation),
        )

        launcher._hide_working(7)

        assert hidden == [7]

    def test_the_launcher_hands_the_show_callback_its_launch(self, tmp_path):
        launcher = _launcher(tmp_path)
        shown = []
        launcher.set_working_callback(
            show=lambda message, generation: shown.append(
                (message, generation)
            ),
            hide=lambda generation: None,
        )

        launcher._show_working("Loading Parakeet", 7)

        assert shown == [("Loading Parakeet", 7)]


class TestTheQueueCallbacksForwardTheLaunch:
    """The Logic-side link between the launcher and the GUI.

    The launcher names the launch and the GUI decides what to do with a
    dismiss, but neither is reached if the pair in between drops the
    number on the way through (wh-launch-addressed-notices).
    """

    def _callbacks(self):
        from main import _working_dialog_callbacks

        queue = MagicMock()
        show, hide = _working_dialog_callbacks(queue)
        return queue, show, hide

    def test_the_show_message_carries_the_launch(self):
        queue, show, _hide = self._callbacks()

        show("Loading Parakeet", 7)

        queue.put_nowait.assert_called_once_with({
            "action": "show_working",
            "message": "Loading Parakeet",
            "owner": "stt:7",
        })

    def test_the_dismiss_message_carries_the_launch(self):
        queue, _show, hide = self._callbacks()

        hide(7)

        queue.put_nowait.assert_called_once_with({
            "action": "hide_working",
            "owner": "stt:7",
        })

    def test_a_caller_that_names_no_launch_sends_none(self):
        queue, show, hide = self._callbacks()

        show("Asking")
        hide()

        assert queue.put_nowait.call_args_list[0].args[0]["owner"] is None
        assert queue.put_nowait.call_args_list[1].args[0]["owner"] is None

    def test_a_queue_that_refuses_does_not_end_the_startup(self):
        # Both run on the launcher's monitor threads, where an exception
        # would abandon the rest of the startup path.
        queue, show, hide = self._callbacks()
        queue.put_nowait.side_effect = ValueError("queue closed")

        show("Loading Parakeet", 7)
        hide(7)


class TestTheNoticeNamesTheLaunchItBelongsTo:
    """The failure notice is decided at the moment it is delivered.

    The dialog half moved its decision to the GUI, where the queue has
    already ordered the replacement's show against the older launch's
    hide. The notice has no such ordering point: it ends in a plyer
    call in the Logic process, so the only place left to decide is
    immediately before that call. What remains is the few instructions
    between the read and the call, rather than a window spanning a
    dismiss and a queue put (wh-launch-addressed-notices, the ruling on
    wh-launch-generation.2.9).
    """

    def _launcher_with_notify(self, tmp_path, current):
        launcher = _launcher(tmp_path)
        launcher._current_launch_generation = current
        sent = MagicMock()
        launcher.set_notify_callback(sent)
        return launcher, sent

    def test_a_notice_for_the_current_launch_is_delivered(self, tmp_path):
        launcher, sent = self._launcher_with_notify(tmp_path, 4)

        launcher._notify("Parakeet", "Failed to start", 4)

        sent.assert_called_once_with("Parakeet", "Failed to start")

    def test_a_notice_for_a_replaced_launch_is_dropped(self, tmp_path):
        launcher, sent = self._launcher_with_notify(tmp_path, 5)

        launcher._notify("Parakeet", "Failed to start", 4)

        sent.assert_not_called()

    def test_a_notice_that_names_no_launch_is_delivered(self, tmp_path):
        """Today's behaviour for every caller that names no launch."""
        launcher, sent = self._launcher_with_notify(tmp_path, 5)

        launcher._notify("Wheelhouse", "Something happened")

        sent.assert_called_once_with("Wheelhouse", "Something happened")

    def test_a_dropped_notice_names_both_launches(self, tmp_path, caplog):
        import logging

        launcher, sent = self._launcher_with_notify(tmp_path, 5)

        with caplog.at_level(logging.INFO, logger="stt.remote_stt_launcher"):
            launcher._notify("Parakeet", "Failed to start", 4)

        assert "4" in caplog.text, caplog.text
        assert "5" in caplog.text, caplog.text
        sent.assert_not_called()

    def test_a_switch_between_the_gate_and_the_toast_drops_the_notice(
        self, tmp_path
    ):
        """The residual race the ruling closed.

        The dead-child branch reads `launch_is_current` once and then
        does two things with the answer. The dismiss goes first, so a
        replacement stamped while that dismiss is in flight leaves the
        notice about to announce a failure for a launch that is
        starting normally (wh-launch-generation.2.9). The replacement is
        stamped from the dismiss here because the dismiss is the one
        step the real code takes in between.
        """
        launcher, sent = self._launcher_with_notify(tmp_path, 1)
        launcher._launch_generations = {"google": 1}
        launcher._provider_stopped = MagicMock()

        def _replace(generation):
            launcher._current_launch_generation = 2

        launcher._hide_working = MagicMock(side_effect=_replace)

        launcher._monitor_startup(
            "google",
            "Google",
            timeout=0.01,
            generation=1,
            process=_dead_subprocess(),
        )

        launcher._hide_working.assert_called_once_with(1)
        sent.assert_not_called()

    def test_the_dead_child_branch_names_its_launch(self, tmp_path):
        launcher = _launcher(tmp_path)
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._monitor_startup(
            "google",
            "Google",
            timeout=0.01,
            generation=1,
            process=_dead_subprocess(),
        )

        launcher._notify.assert_called_once_with(
            "Google", "Failed to start - try restarting Wheelhouse", 1
        )

    def test_the_late_death_watch_names_its_launch(self, tmp_path):
        launcher = _launcher(tmp_path)
        # Bounded for the reason its sibling in
        # TestTheDialogNamesTheLaunchItBelongsTo records: a dead child
        # under the gate's late-death liveness mutation never breaks out
        # of the watch, so the default 300s limit outlasts pytest's own
        # 30s timeout and aborts the whole gate run
        # (wh-launch-addressed-notices).
        launcher._late_death_poll_interval = 0.01
        launcher._late_death_watch_limit = 0.02
        launcher._launch_generations = {"google": 1}
        launcher._current_launch_generation = 1
        launcher._open_launch_signal(1)
        launcher._provider_stopped = MagicMock()
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()

        launcher._watch_for_late_death(
            "google", "Google", _slot(launcher, 1), _dead_subprocess(), 1
        )

        launcher._notify.assert_called_once_with(
            "Google", "Failed to start - try restarting Wheelhouse", 1
        )

    def test_the_start_failure_names_its_launch(self, tmp_path):
        from unittest.mock import patch

        service = tmp_path / "providers" / "google_stt_server"
        service.mkdir(parents=True)
        (service / "config.toml").write_text(
            '[provider]\nname = "google_stt"\n'
            'display_name = "Google Cloud STT"\nlauncher = "launcher.py"\n'
        )
        (service / "launcher.py").write_text("# launcher stub")
        launcher = _launcher(tmp_path)
        launcher.ws_port = 5500
        launcher._hide_working = MagicMock()
        launcher._notify = MagicMock()
        launcher._provider_stopped = MagicMock()

        with patch("subprocess.Popen", side_effect=OSError("fail")):
            assert launcher.start_provider("google_stt") is False

        launcher._notify.assert_called_once_with(
            "Google Cloud STT",
            "Failed to start - try restarting Wheelhouse",
            launcher._current_launch_generation,
        )
        assert launcher._current_launch_generation == 1
