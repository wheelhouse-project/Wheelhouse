"""A removed speech engine in the settings selects Parakeet, with one notice.

wh-distil-whisper-removal, commit 1. Two things name the removed engine:

* stt.last_provider still reads "distil_medium_en" (an app-only update, or a
  settings file the installer did not rewrite), whether or not the provider
  folder exists;
* the installer marker stt.removed_provider reads "distil_medium_en". The
  installer rewrites last_provider to the engine it installed before the app
  starts, so without the marker the app could not tell that the user's engine
  was removed.

Real ServiceManager.start_remote_stt, real ConfigService on a temporary
settings file, real NotifierWorker queue. Only the launcher (processes) and
the uv environment check are replaced.
"""
import asyncio
import copy
import logging
import queue
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import service_manager as sm
from config_service import ConfigService
from stt import parakeet_model, removed_providers
from utils.notifier_worker import NotifierWorker


DISPLAY_NAMES = {"google_stt": "Google Cloud", "parakeet_tdt": "Parakeet"}
FALLBACK_PLAN = parakeet_model.InstallerFallback(
    command=parakeet_model.ONE_LINE_INSTALL_COMMAND, reason="no_setup_copy")


def provider(name):
    return dict(name=name, display_name=DISPLAY_NAMES.get(name, name),
                service_dir=Path("unused"))


class Rig:
    """One app start: a ServiceManager over a settings file."""

    def __init__(self, tmp_path, monkeypatch, settings_text, providers, working=None,
                 model_complete=True, installer=FALLBACK_PLAN):
        from services.wheelhouse.utils import logging_setup

        self.path = tmp_path / "config.toml"
        if settings_text is not None:
            self.path.write_text(settings_text, encoding="utf-8")
        self.loop = asyncio.new_event_loop()
        self.config = ConfigService(str(self.path))
        self.worker = NotifierWorker()
        monkeypatch.setattr(logging_setup, "get_notifier_worker", lambda: self.worker)
        monkeypatch.setattr(sm, "startup_speech_notices", lambda found: [])
        monkeypatch.setattr(sm, "runtime_version_notice", lambda: None)
        # The model check reads the real model folder on this computer unless
        # a test decides the answer; the installer locator reads the Setup
        # copy in %LOCALAPPDATA%.
        self.model_checks = 0

        def model_complete_now():
            self.model_checks += 1
            return model_complete

        monkeypatch.setattr(parakeet_model, "parakeet_model_complete", model_complete_now)
        monkeypatch.setattr(parakeet_model, "locate_installer", lambda: installer)
        self.state_manager = Mock()
        self.state_manager.state_to_gui_queue = queue.Queue()
        self.started = []
        self.notices_when_started = []
        self.launcher = Mock()
        self.launcher.discover_providers.return_value = [provider(n) for n in providers]

        def start(name):
            self.started.append(name)
            self.notices_when_started.append(self.worker.queue.qsize())
            return working is None or name in working

        self.launcher.start_provider.side_effect = start
        self.manager = sm.ServiceManager(
            self.config, Mock(), self.loop,
            Mock(get_screen_dimensions=lambda: (1920, 1080)), self.state_manager,
        )
        self.manager.remote_stt_launcher = self.launcher

    def start(self):
        result = self.manager.start_remote_stt()
        # Let a scheduled save run, as the Logic loop would. Wait for the
        # tasks themselves: the save writes the file in a worker thread, and a
        # fixed sleep let a slow write finish after the test read the file.
        pending = asyncio.all_tasks(self.loop)
        if pending:
            self.loop.run_until_complete(asyncio.wait(pending))
        return result

    def notices(self):
        found = []
        while not self.worker.queue.empty():
            found.append(self.worker.queue.get_nowait())
        return found

    def offers(self):
        found = []
        while not self.state_manager.state_to_gui_queue.empty():
            found.append(self.state_manager.state_to_gui_queue.get_nowait())
        return [m for m in found if m.get("action") == "parakeet_model_offer"]

    def saved(self):
        return tomllib.loads(self.path.read_text(encoding="utf-8"))

    def close(self):
        self.loop.close()


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
    rigs = []

    def make(settings_text, providers=("parakeet_tdt", "google_stt"), working=None, subdir="a",
             **options):
        directory = tmp_path / subdir
        directory.mkdir(exist_ok=True)
        rig = Rig(directory, monkeypatch, settings_text, providers, working, **options)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()


DISTIL = '[stt]\nlast_provider = "distil_medium_en"\nmode = "remote"\n'


def assert_one_removal_notice(notices):
    assert len(notices) == 1, f"exactly one notice, got {[n.message for n in notices]}"
    notice = notices[0]
    assert notice.title == removed_providers.REMOVED_PROVIDER_NOTICE_TITLE
    assert "Distil-Whisper was removed" in notice.message
    assert "Parakeet is now in use" in notice.message


def test_missing_provider_folder_selects_parakeet_with_one_notice(make_rig):
    rig = make_rig(DISTIL, providers=("parakeet_tdt", "google_stt"))
    assert rig.start()
    assert rig.started == ["parakeet_tdt"], "the removed engine is never launched"
    assert_one_removal_notice(rig.notices())
    assert rig.config.get("stt.last_provider") == "parakeet_tdt", (
        "the move must be recorded in the settings in memory")
    assert rig.saved()["stt"]["last_provider"] == "parakeet_tdt", (
        "the switch must reach the settings file")
    assert rig.saved()["stt"]["mode"] == "remote", "other settings are kept"


def test_present_provider_folder_selects_parakeet_with_one_notice(make_rig):
    rig = make_rig(DISTIL, providers=("parakeet_tdt", "google_stt", "distil_medium_en"))
    assert rig.start()
    assert rig.started == ["parakeet_tdt"], "an old folder left on disk is never launched"
    assert_one_removal_notice(rig.notices())
    assert rig.saved()["stt"]["last_provider"] == "parakeet_tdt"


def test_installer_marker_alone_shows_the_notice_and_is_cleared(make_rig):
    rig = make_rig(
        '[stt]\nlast_provider = "parakeet_tdt"\nremoved_provider = "distil_medium_en"\n'
    )
    assert rig.start()
    assert rig.started == ["parakeet_tdt"]
    assert_one_removal_notice(rig.notices())
    assert "removed_provider" not in rig.saved()["stt"], "the marker is cleared on disk"
    assert rig.config.get("stt.removed_provider") is None, "the marker is cleared in memory"
    assert rig.saved()["stt"]["last_provider"] == "parakeet_tdt"


def test_marker_and_removed_last_provider_together_show_one_notice(make_rig):
    rig = make_rig(
        '[stt]\nlast_provider = "distil_medium_en"\nremoved_provider = "distil_medium_en"\n'
    )
    assert rig.start()
    assert_one_removal_notice(rig.notices())
    assert "removed_provider" not in rig.saved()["stt"]


def test_a_second_start_in_the_same_run_shows_no_notice(make_rig):
    rig = make_rig(DISTIL)
    assert rig.start()
    assert len(rig.notices()) == 1
    assert rig.start()
    assert rig.notices() == [], "the switch is kept in memory, so a second start is silent"
    assert rig.started == ["parakeet_tdt", "parakeet_tdt"]


def test_a_start_after_a_saved_switch_shows_no_notice(make_rig):
    first = make_rig(DISTIL, subdir="first")
    assert first.start()
    assert len(first.notices()) == 1
    # A new run reads the file the first run saved.
    second = make_rig(None, subdir="first")
    assert second.start()
    assert second.notices() == [], "the saved switch must not announce itself again"
    assert second.started == ["parakeet_tdt"]


@pytest.mark.parametrize("failure", [
    pytest.param(AsyncMock(return_value=False), id="save-returns-false"),
    pytest.param(AsyncMock(side_effect=OSError("disk full")), id="save-raises"),
])
def test_a_save_failure_warns_and_parakeet_still_starts(make_rig, caplog, failure):
    rig = make_rig(DISTIL)
    rig.config.save = failure
    with caplog.at_level(logging.WARNING):
        assert rig.start()
    assert rig.started == ["parakeet_tdt"], "the start continues with Parakeet"
    assert_one_removal_notice(rig.notices())
    failure.assert_awaited_once()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "Could not save" in r.getMessage()]
    assert len(warnings) == 1, (
        f"one warning on a failed save, got {[r.getMessage() for r in caplog.records]}")
    assert "Distil-Whisper" in warnings[0].getMessage()


@pytest.mark.parametrize("settings", [
    pytest.param('[stt]\nlast_provider = "parakeet_tdt"\n', id="parakeet"),
    pytest.param('[stt]\nlast_provider = "google_stt"\n', id="google"),
    pytest.param('[stt]\nlast_provider = "parakeet_tdt"\nremoved_provider = "kroko"\n',
                 id="unrelated-marker"),
])
def test_other_settings_change_nothing_and_show_no_notice(make_rig, settings):
    rig = make_rig(settings)
    before = rig.path.read_bytes()
    in_memory = copy.deepcopy(rig.config.get_config())
    rig.config.save = AsyncMock(return_value=True)
    assert rig.start()
    assert rig.notices() == [], "no removed engine, no notice"
    assert rig.path.read_bytes() == before
    assert rig.config.get_config() == in_memory, (
        "settings that name no removed engine are left untouched")
    rig.config.save.assert_not_awaited()
    assert rig.model_checks == 0, "only a removed engine adds the model check"
    assert rig.offers() == [], "only a removed engine can send the model offer"


def test_a_google_choice_is_never_replaced(make_rig):
    rig = make_rig('[stt]\nlast_provider = "google_stt"\n')
    assert rig.start()
    assert rig.started == ["google_stt"], "a shipped engine choice is never replaced"


def test_a_stale_marker_beside_another_engine_shows_no_false_notice(make_rig):
    """The notice says Parakeet is in use. With Google selected that is false,
    so the marker is cleared in memory (the next settings save drops it) and
    nothing is announced."""
    rig = make_rig(
        '[stt]\nlast_provider = "google_stt"\nremoved_provider = "distil_medium_en"\n'
    )
    assert rig.start()
    assert rig.notices() == [], "a stale marker must not announce a switch to Parakeet"
    assert rig.started == ["google_stt"]
    assert rig.config.get("stt.removed_provider") is None
    assert rig.config.get("stt.last_provider") == "google_stt"


def test_a_marker_with_no_last_provider_shows_the_notice(make_rig):
    """The default engine is Parakeet, so an unset choice is not another engine."""
    rig = make_rig('[stt]\nremoved_provider = "distil_medium_en"\n')
    assert rig.start()
    assert_one_removal_notice(rig.notices())
    assert "removed_provider" not in rig.saved()["stt"]


@pytest.mark.parametrize("value", [
    pytest.param(["distil_medium_en"], id="list"),
    pytest.param(7, id="number"),
])
def test_a_hand_edited_non_text_value_does_not_stop_the_start(make_rig, value):
    rig = make_rig("[stt]\n")
    rig.config.set("stt.last_provider", value)
    rig.config.set("stt.removed_provider", value)
    try:
        assert rig.manager.start_remote_stt()
    except Exception as exc:
        raise AssertionError(f"a non-text value must not stop the start: {exc!r}") from exc
    assert rig.notices() == [], "a non-text value names no removed engine"


def test_settings_that_cannot_take_the_switch_still_start_parakeet(make_rig, caplog):
    """A plain value where the [stt] table belongs makes set() raise TypeError.
    The engine must still start as Parakeet, with the notice."""
    rig = make_rig(DISTIL)
    rig.config.set = Mock(side_effect=TypeError("'str' object does not support item assignment"))
    with caplog.at_level(logging.WARNING):
        try:
            assert rig.start()
        except Exception as exc:
            raise AssertionError(f"the start must survive a settings failure: {exc!r}") from exc
    assert rig.started == ["parakeet_tdt"], "the start continues with Parakeet"
    assert_one_removal_notice(rig.notices())
    assert any("Could not record the move" in r.getMessage() for r in caplog.records), (
        "a failed record warns")


def test_a_loop_that_cannot_schedule_the_save_still_starts_parakeet(make_rig):
    rig = make_rig(DISTIL)
    rig.manager.loop = Mock(create_task=Mock(side_effect=RuntimeError("Event loop is closed")))
    try:
        assert rig.manager.start_remote_stt()
    except Exception as exc:
        raise AssertionError(f"the start must survive a closed loop: {exc!r}") from exc
    assert rig.started == ["parakeet_tdt"]
    assert_one_removal_notice(rig.notices())


# --- the notice names the engine that actually started (review finding .1.1) ----
#
# The notice used to be sent before the start decision and always said Parakeet
# was in use. It is now sent after the decision, and its text depends on which
# engine started. Four outcomes, all starting "Distil-Whisper was removed from
# Wheelhouse.":
#   a. Parakeet started and its speech model is complete.
#   b. Parakeet's speech model is not complete: Parakeet is not started, the
#      model offer goes to the GUI, and the ordinary fallback runs.
#   c. Parakeet's model is complete but Parakeet did not start, and another
#      engine did.
#   d. No engine started.

REMOVED = "Distil-Whisper was removed from Wheelhouse."
TEXT_A = f"{REMOVED} Parakeet is now in use as your speech engine."
TEXT_B = f"{REMOVED} Parakeet replaces it, but its speech model is not installed."
TEXT_C = f"{REMOVED} Parakeet could not start, so Google Cloud is in use as your speech engine."
TEXT_D = f"{REMOVED} No speech engine could start."
FALLBACK_OFFER = {
    "action": "parakeet_model_offer",
    "kind": "fallback",
    "command": parakeet_model.ONE_LINE_INSTALL_COMMAND,
}
LAUNCH_OFFER = {"action": "parakeet_model_offer", "kind": "launch"}


def only_notice(rig):
    notices = rig.notices()
    assert len(notices) == 1, f"exactly one notice, got {[n.message for n in notices]}"
    assert notices[0].title == removed_providers.REMOVED_PROVIDER_NOTICE_TITLE
    return notices[0].message


def test_outcome_a_the_notice_comes_after_the_start_decision(make_rig):
    rig = make_rig(DISTIL)
    assert rig.start()
    assert rig.notices_when_started == [0], (
        "the notice must not be sent before the engine start is decided")
    assert only_notice(rig) == TEXT_A, "the notice text names the outcome (TEXT_A)"
    assert rig.offers() == [], "a complete model needs no offer"
    assert rig.model_checks == 1, "the model is checked once per start"


def test_outcome_b_enabled_parakeet_with_the_model_missing(make_rig):
    rig = make_rig(DISTIL, providers=("parakeet_tdt", "google_stt"), model_complete=False)
    assert rig.start()
    assert "parakeet_tdt" not in rig.started, "Parakeet without its model is not started"
    assert rig.started == ["google_stt"], "the ordinary fallback starts the other engine"
    assert only_notice(rig) == TEXT_B, "the notice text names the outcome (TEXT_B)"
    assert rig.offers() == [FALLBACK_OFFER], "the model offer is sent to the GUI"
    assert rig.saved()["stt"]["last_provider"] != "distil_medium_en", (
        "the settings save of the switch still runs")


def test_outcome_b_disabled_parakeet_with_google_available(make_rig):
    # Discovery leaves a disabled provider out, so only Google is found.
    rig = make_rig(DISTIL, providers=("google_stt",), model_complete=False)
    assert rig.start()
    assert "parakeet_tdt" not in rig.started, "Parakeet without its model is not started"
    assert rig.started == ["google_stt"]
    assert only_notice(rig) == TEXT_B, "the notice text names the outcome (TEXT_B)"
    assert rig.offers() == [FALLBACK_OFFER], "the model offer is sent to the GUI"


def test_outcome_b_sends_the_launch_offer_when_the_installer_is_found(make_rig):
    plan = parakeet_model.InstallerLaunchPlan(script_path=Path("install-wheelhouse.ps1"))
    rig = make_rig(DISTIL, model_complete=False, installer=plan)
    assert rig.start()
    assert rig.offers() == [LAUNCH_OFFER], "the same offer LogicController sends"
    assert only_notice(rig) == TEXT_B, "the notice text names the outcome (TEXT_B)"


def test_outcome_b_with_no_other_engine_still_offers_the_model(make_rig):
    rig = make_rig(DISTIL, providers=("parakeet_tdt",), model_complete=False)
    assert rig.start() is False
    assert rig.started == [], "Parakeet without its model is not started"
    assert only_notice(rig) == TEXT_B, "the notice text names the outcome (TEXT_B)"
    assert rig.offers() == [FALLBACK_OFFER], "the model offer is sent to the GUI"


def test_outcome_c_names_the_engine_that_started(make_rig):
    rig = make_rig(DISTIL, working={"google_stt"})
    assert rig.start(), "the other engine starts after the replacement fails"
    assert rig.started == ["parakeet_tdt", "google_stt"]
    assert only_notice(rig) == TEXT_C, "the notice text names the outcome (TEXT_C)"
    assert rig.offers() == [], "the model is complete, so no offer"


def test_outcome_d_when_no_engine_starts(make_rig):
    rig = make_rig(DISTIL, working=set())
    assert rig.start() is False
    assert only_notice(rig) == TEXT_D, "the notice text names the outcome (TEXT_D)"
    assert rig.offers() == []
    assert rig.saved()["stt"]["last_provider"] == "parakeet_tdt", (
        "the settings save of the switch still runs")


def test_outcome_d_when_no_engine_is_found(make_rig):
    rig = make_rig(DISTIL, providers=())
    assert rig.start() is False
    assert only_notice(rig) == TEXT_D, "the notice text names the outcome (TEXT_D)"
    assert rig.offers() == []
    assert rig.saved()["stt"]["last_provider"] == "parakeet_tdt"


def test_the_model_offer_sender_lives_in_the_shared_module_and_never_raises():
    """LogicController._switch_stt_provider and the startup path call this one function."""
    state_manager = Mock()
    state_manager.state_to_gui_queue = queue.Queue()
    plan = parakeet_model.InstallerLaunchPlan(script_path=Path("install-wheelhouse.ps1"))
    parakeet_model.send_parakeet_model_offer(state_manager, plan)
    parakeet_model.send_parakeet_model_offer(state_manager, FALLBACK_PLAN)
    sent = [state_manager.state_to_gui_queue.get_nowait() for _ in range(2)]
    assert sent == [LAUNCH_OFFER, FALLBACK_OFFER], "the sender puts each offer on the queue"

    full = Mock()
    full.state_to_gui_queue = Mock(put_nowait=Mock(side_effect=queue.Full()))
    try:
        parakeet_model.send_parakeet_model_offer(full, FALLBACK_PLAN)
    except Exception as exc:
        raise AssertionError(f"the sender must not raise: {exc!r}") from exc


def test_a_second_start_shows_the_notice_only_once_after_outcome_b(make_rig):
    rig = make_rig(DISTIL, model_complete=False)
    assert rig.start()
    assert only_notice(rig) == TEXT_B, "the notice text names the outcome (TEXT_B)"
    assert len(rig.offers()) == 1
    assert rig.start()
    assert rig.notices() == [], "the switch was recorded, so the notice does not repeat"


@pytest.mark.parametrize("save, expected", [
    pytest.param(AsyncMock(return_value=True), True, id="saved"),
    pytest.param(AsyncMock(return_value=False), False, id="refused"),
    pytest.param(AsyncMock(side_effect=OSError("denied")), False, id="raised"),
])
def test_the_save_reports_whether_it_reached_the_disk(save, expected):
    config = Mock(save=save)
    saved = asyncio.run(removed_providers.save_removed_provider_switch(config, "Distil-Whisper"))
    assert saved is expected, "the result says whether the settings were saved"


# --- provider discovery never offers a removed engine (commit 3) ---------------
#
# An old machine can still hold the distil_medium_en folder on disk after the
# update. start_remote_stt moves the settings off it, but the fallback and the
# tray menu take provider names from filesystem discovery, so discovery itself
# must leave every removed engine out.

def _write_provider_folder(services_dir, folder, name):
    directory = services_dir / folder
    directory.mkdir()
    (directory / "config.toml").write_text(
        f'[provider]\nname = "{name}"\ndisplay_name = "{name}"\nlauncher = "launcher.py"\n',
        encoding="utf-8",
    )
    (directory / "launcher.py").write_text("# launcher stub", encoding="utf-8")


def test_discovery_skips_a_removed_engine_folder_left_on_disk(tmp_path):
    services_dir = tmp_path / "stt_providers"
    services_dir.mkdir()
    _write_provider_folder(services_dir, "distil_medium_en", "distil_medium_en")
    _write_provider_folder(services_dir, "google_stt_server", "google_stt")

    launcher = sm.RemoteSTTLauncher(services_dir=services_dir, app_data_dir=tmp_path / "data")
    names = [p["name"] for p in launcher.discover_providers()]

    assert names == ["google_stt"], "the provider list never offers a removed engine"
    assert [p["name"] for p in launcher.get_providers()] == ["google_stt"], (
        "the cached list the tray menu reads never offers a removed engine")
    assert launcher.get_provider_by_name("distil_medium_en") is None


def test_the_fallback_never_starts_a_removed_engine_left_on_disk(tmp_path, monkeypatch):
    services_dir = tmp_path / "stt_providers"
    services_dir.mkdir()
    _write_provider_folder(services_dir, "distil_medium_en", "distil_medium_en")

    from services.wheelhouse.utils import logging_setup
    worker = NotifierWorker()
    monkeypatch.setattr(logging_setup, "get_notifier_worker", lambda: worker)
    monkeypatch.setattr(sm, "startup_speech_notices", lambda found: [])
    monkeypatch.setattr(sm, "runtime_version_notice", lambda: None)
    settings = tmp_path / "config.toml"
    settings.write_text('[stt]\nlast_provider = "parakeet_tdt"\n', encoding="utf-8")
    loop = asyncio.new_event_loop()
    try:
        manager = sm.ServiceManager(
            ConfigService(str(settings)), Mock(), loop,
            Mock(get_screen_dimensions=lambda: (1920, 1080)), Mock(),
        )
        launcher = sm.RemoteSTTLauncher(services_dir=services_dir, app_data_dir=tmp_path / "data")
        started = []

        # Like the real start_provider, refuse a name that discovery did not
        # find; a stub that accepts every name ends the call at the
        # last-provider start and never reaches the fallback.
        def start_provider(provider_name, **_kwargs):
            started.append(provider_name)
            return provider_name in {p["name"] for p in launcher.get_providers()}

        launcher.start_provider = start_provider
        manager.remote_stt_launcher = launcher

        result = manager.start_remote_stt()
        assert "distil_medium_en" not in started, "the fallback never starts a removed engine"
        assert result is False, "no shipped engine is on disk"
    finally:
        loop.close()
