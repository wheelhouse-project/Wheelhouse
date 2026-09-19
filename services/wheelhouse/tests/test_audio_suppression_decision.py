"""The startup decision that turns the sound pause on or off.

wh-audio-suppression-auto. The sound pause used to be a switch the user
toggled. It is now a decision WheelHouse makes once, at startup, from what
Windows reports about the default microphone: a microphone with an echo
canceller already removes this computer's own sound, so pausing listening
buys nothing and costs the user every command spoken over sound.

ENABLE_AUDIO_SUPPRESSION carries three values. "auto" (the shipped default)
lets the query decide, true always pauses, and false never pauses. A value
the reader does not recognise gives one WARNING and is treated as "auto".

Two device roles are asked, AudioDeviceRole.DEFAULT and
AudioDeviceRole.COMMUNICATIONS (boss ruling Q5, 2026-09-17 12:58). The pause
is off only when both report an echo canceller, because a wrong answer in the
off direction is the costly one: the user speaks over sound and nothing is
recognised. The INFO line names both devices when the two roles differ.

Every test here patches the query. Nothing in this file calls a Windows audio
API; tests 12 to 14 put fake winsdk modules in sys.modules instead.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import tomllib
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

# The StateManager fixture the last three tests need already exists.
from tests.test_state_manager import sm  # noqa: F401

from services.wheelhouse.utils.audio_suppression_decision import (
    AUDIO_SUPPRESSION_DEFAULT,
    AUDIO_SUPPRESSION_SETTING,
    QUERY_TIMEOUT_SECONDS,
    ROLE_COMMUNICATIONS,
    ROLE_DEFAULT,
    EchoCancellerReport,
    RoleReport,
    decide_audio_suppression,
    parse_audio_suppression_setting,
    query_echo_canceller,
)
from services.wheelhouse.utils import audio_suppression_decision as decision_module
from services.wheelhouse.utils.winrt_helpers import WinRTError

LOGGER_NAME = decision_module.__name__

# The name in acceptance criterion 2's example line.
SCARLETT = "Microphone (2- Scarlett Solo USB)"
HEADSET = "Headset Microphone (Jabra Evolve 65)"

_WHEELHOUSE = Path(__file__).parent.parent
_EXAMPLE_CONFIG = _WHEELHOUSE / "config.toml.example"
_CONFIG_DESCRIPTIONS = _WHEELHOUSE / "knowledge" / "helpdoc" / "config_descriptions.toml"

SUPPRESSION_KEYS = (
    "ENABLE_AUDIO_SUPPRESSION",
    "ENABLE_SONOS_SUPPRESSION",
    "ENABLE_IDLE_SUPPRESSION",
)


def _one_device(has_canceller: bool, name: str = SCARLETT) -> EchoCancellerReport:
    """Both roles name the same microphone, as they do on most machines."""
    return EchoCancellerReport(
        (
            RoleReport(ROLE_DEFAULT, name, has_canceller),
            RoleReport(ROLE_COMMUNICATIONS, name, has_canceller),
        )
    )


def _two_devices(default_has: bool, communications_has: bool) -> EchoCancellerReport:
    """The two roles name different microphones."""
    return EchoCancellerReport(
        (
            RoleReport(ROLE_DEFAULT, SCARLETT, default_has),
            RoleReport(ROLE_COMMUNICATIONS, HEADSET, communications_has),
        )
    )


def _records(caplog, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == level
    ]


def _decision_lines(caplog) -> list[str]:
    """The INFO lines that state the decision, without the duration line."""
    return [line for line in _records(caplog, logging.INFO)
            if line.startswith("Audio suppression o")]


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


class TestTheAutomaticDecision:
    """"auto" asks Windows, and the answer decides."""

    async def test_auto_with_an_echo_canceller_turns_suppression_off(self):
        query = Mock(return_value=_one_device(True))

        assert await decide_audio_suppression("auto", query=query) is False
        query.assert_called_once_with()

    async def test_auto_without_an_echo_canceller_turns_suppression_on(self):
        query = Mock(return_value=_one_device(False))

        assert await decide_audio_suppression("auto", query=query) is True

    async def test_the_decision_logs_one_info_line_with_the_device_name(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        await decide_audio_suppression("auto", query=Mock(return_value=_one_device(True)))

        assert _decision_lines(caplog) == [
            "Audio suppression off: Windows reports an echo canceller for "
            "Microphone (2- Scarlett Solo USB)"
        ]

        caplog.clear()
        await decide_audio_suppression("auto", query=Mock(return_value=_one_device(False)))

        assert _decision_lines(caplog) == [
            "Audio suppression on: Windows reports no echo canceller for "
            "Microphone (2- Scarlett Solo USB)"
        ]

    async def test_both_device_roles_are_named_when_they_differ(self, caplog):
        """Boss ruling Q5: the INFO line names both devices."""
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert await decide_audio_suppression(
            "auto", query=Mock(return_value=_two_devices(True, True))
        ) is False

        assert _decision_lines(caplog) == [
            "Audio suppression off: Windows reports an echo canceller for "
            f"{SCARLETT} (default) and {HEADSET} (communications)"
        ]

    async def test_one_role_without_a_canceller_keeps_suppression_on(self, caplog):
        """Boss ruling Q5: off only when both roles report a canceller."""
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert await decide_audio_suppression(
            "auto", query=Mock(return_value=_two_devices(True, False))
        ) is True

        assert _decision_lines(caplog) == [
            "Audio suppression on: Windows reports an echo canceller for "
            f"{SCARLETT} (default) and no echo canceller for "
            f"{HEADSET} (communications)"
        ]

    async def test_the_query_duration_is_logged_at_info(self, caplog):
        """Boss ruling Q7: the manual-test checklist quotes the duration."""
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        await decide_audio_suppression("auto", query=Mock(return_value=_one_device(True)))

        duration_lines = [
            line for line in _records(caplog, logging.INFO)
            if line.startswith("Audio suppression query took ")
        ]
        assert len(duration_lines) == 1
        assert duration_lines[0].endswith(" s")


class TestAFailedQuery:
    """Windows cannot answer, so listening pauses, as it does with no canceller."""

    @pytest.mark.parametrize(
        "failure",
        ["import", "os", "empty-id", "hang"],
    )
    async def test_a_query_failure_turns_suppression_on_with_one_warning(
        self, caplog, failure
    ):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        release = threading.Event()
        timeout = QUERY_TIMEOUT_SECONDS

        if failure == "hang":
            timeout = 0.05

            def query():
                release.wait(10)
                return _one_device(True)
        else:
            error = {
                "import": ImportError("No module named 'winsdk'"),
                "os": OSError("the audio service is not running"),
                "empty-id": WinRTError("Windows names no default capture device"),
            }[failure]

            def query():
                raise error

        try:
            assert await decide_audio_suppression(
                "auto", query=query, timeout=timeout
            ) is True
        finally:
            release.set()

        assert len(_records(caplog, logging.WARNING)) == 1
        assert _records(caplog, logging.WARNING)[0].startswith(
            "Audio suppression on: could not ask Windows whether the microphone "
            "has an echo canceller ("
        )
        assert _records(caplog, logging.INFO) == []

    async def test_a_query_that_finishes_after_the_timeout_changes_nothing(
        self, caplog
    ):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        release = threading.Event()
        loop_errors: list[dict] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: loop_errors.append(context)
        )

        def query():
            release.wait(10)
            return _one_device(True)

        try:
            assert await decide_audio_suppression(
                "auto", query=query, timeout=0.05
            ) is True
            after_timeout = list(caplog.records)
        finally:
            release.set()

        await asyncio.sleep(0.2)

        assert list(caplog.records) == after_timeout
        assert loop_errors == []


class TestAForcedValue:
    """true and false decide without a Windows call (boss ruling Q8)."""

    async def test_true_forces_suppression_on_without_asking_windows(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        query = Mock()

        assert await decide_audio_suppression(True, query=query) is True

        query.assert_not_called()
        assert _records(caplog, logging.INFO) == [
            f"Audio suppression on: {AUDIO_SUPPRESSION_SETTING} is true"
        ]

    async def test_false_forces_suppression_off_without_asking_windows(self, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        query = Mock()

        assert await decide_audio_suppression(False, query=query) is False

        query.assert_not_called()
        assert _records(caplog, logging.INFO) == [
            f"Audio suppression off: {AUDIO_SUPPRESSION_SETTING} is false"
        ]


class TestTheSettingValue:
    """Boss ruling Q4: TOML booleans and the three strings, in any case."""

    @pytest.mark.parametrize(
        "value, parsed",
        [
            pytest.param("auto", None, id="auto"),
            pytest.param("AUTO", None, id="auto-upper"),
            pytest.param(" true ", True, id="true-padded"),
            pytest.param("False", False, id="false-capitalised"),
            pytest.param(True, True, id="toml-true"),
            pytest.param(False, False, id="toml-false"),
        ],
    )
    def test_the_setting_accepts_strings_in_any_case(self, value, parsed):
        assert parse_audio_suppression_setting(value) is parsed

    @pytest.mark.parametrize(
        "value", [pytest.param(3, id="integer"), pytest.param("sometimes", id="word")]
    )
    async def test_an_invalid_value_warns_once_and_asks_windows(self, caplog, value):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        query = Mock(return_value=_one_device(True))

        assert await decide_audio_suppression(value, query=query) is False

        query.assert_called_once_with()
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert repr(value) in warnings[0]
        # The WARNING names the accepted values and the fallback (ruling Q4).
        assert '"auto", true or false' in warnings[0]
        assert '"auto"' in warnings[0].split(";")[-1]

    def test_the_default_is_auto(self):
        assert AUDIO_SUPPRESSION_DEFAULT == "auto"
        assert parse_audio_suppression_setting(AUDIO_SUPPRESSION_DEFAULT) is None


class TestTheQueryThread:
    """Boss ruling Q7: a daemon thread, so a hung COM call cannot block exit."""

    async def test_the_query_runs_on_a_daemon_thread(self):
        seen: list[threading.Thread] = []

        def query():
            seen.append(threading.current_thread())
            return _one_device(True)

        await decide_audio_suppression("auto", query=query)

        assert len(seen) == 1
        assert seen[0] is not threading.current_thread()
        assert seen[0].daemon is True


# ---------------------------------------------------------------------------
# The Windows query itself, against fake winsdk modules
# ---------------------------------------------------------------------------


class _FakeEffect:
    def __init__(self, effect_type):
        self.audio_effect_type = effect_type


class _FakeEffectsManager:
    def __init__(self, effects):
        self._effects = effects

    def get_audio_capture_effects(self):
        return list(self._effects)


def _install_fake_winsdk(monkeypatch, *, ids, effects_by_id, names=None):
    """Put fake winsdk modules in sys.modules and record every call.

    ``ids`` maps a role value to the device id Windows would return.
    ``effects_by_id`` maps a device id to the effect types of its manager.
    """
    calls = {"roles": [], "managers": []}

    class AudioDeviceRole:
        DEFAULT = 0
        COMMUNICATIONS = 1

    class MediaCategory:
        OTHER = 0
        COMMUNICATIONS = 1
        # Never asked for by this module, and that is the point: a mutation
        # that asks for the wrong category must reach the assertion below
        # rather than raise AttributeError before it
        # (mutation_gate_audio_suppression_auto.py, the-query-asks-for-the-
        # speech-category).
        SPEECH = 4

    class AudioProcessing:
        DEFAULT = 0
        RAW = 1

    class AudioEffectType:
        ACOUSTIC_ECHO_CANCELLATION = 1
        NOISE_SUPPRESSION = 2

    class MediaDevice:
        @staticmethod
        def get_default_audio_capture_id(role):
            calls["roles"].append(role)
            return ids[role]

    class AudioEffectsManager:
        @staticmethod
        def create_audio_capture_effects_manager(device_id, category, mode):
            calls["managers"].append((device_id, category, mode))
            return _FakeEffectsManager(
                [_FakeEffect(kind) for kind in effects_by_id[device_id]]
            )

    class DeviceInformation:
        @staticmethod
        def create_from_id_async(device_id):
            return device_id

    modules = {
        "winsdk.windows.media": {"AudioProcessing": AudioProcessing},
        "winsdk.windows.media.capture": {"MediaCategory": MediaCategory},
        "winsdk.windows.media.devices": {
            "AudioDeviceRole": AudioDeviceRole,
            "MediaDevice": MediaDevice,
        },
        "winsdk.windows.media.effects": {
            "AudioEffectsManager": AudioEffectsManager,
            "AudioEffectType": AudioEffectType,
        },
        "winsdk.windows.devices.enumeration": {"DeviceInformation": DeviceInformation},
    }
    for name, members in modules.items():
        module = types.ModuleType(name)
        for attr, value in members.items():
            setattr(module, attr, value)
        monkeypatch.setitem(sys.modules, name, module)

    lookups = names if names is not None else {}

    def fake_run_winrt_sync(operation, timeout=None, poll_interval=0.01):
        if operation in lookups:
            return types.SimpleNamespace(name=lookups[operation])
        raise WinRTError(f"device name lookup failed for {operation}")

    monkeypatch.setattr(decision_module, "run_winrt_sync", fake_run_winrt_sync)
    return calls


class TestTheWindowsQuery:
    """query_echo_canceller asks for the communications capture effects."""

    def test_the_query_asks_windows_for_communications_capture_effects(
        self, monkeypatch
    ):
        calls = _install_fake_winsdk(
            monkeypatch,
            ids={0: "id-default", 1: "id-default"},
            effects_by_id={"id-default": [2, 1]},
            names={"id-default": SCARLETT},
        )

        report = query_echo_canceller()

        assert calls["roles"] == [0, 1]
        assert calls["managers"] == [("id-default", 1, 0), ("id-default", 1, 0)]
        assert report.has_echo_canceller is True
        assert [role.device_name for role in report.roles] == [SCARLETT, SCARLETT]
        assert [role.role for role in report.roles] == [
            ROLE_DEFAULT,
            ROLE_COMMUNICATIONS,
        ]

    def test_no_canceller_among_the_effects_reports_none(self, monkeypatch):
        _install_fake_winsdk(
            monkeypatch,
            ids={0: "id-default", 1: "id-default"},
            effects_by_id={"id-default": [2]},
            names={"id-default": SCARLETT},
        )

        assert query_echo_canceller().has_echo_canceller is False

    def test_each_role_is_asked_about_its_own_device(self, monkeypatch):
        _install_fake_winsdk(
            monkeypatch,
            ids={0: "id-default", 1: "id-comms"},
            effects_by_id={"id-default": [1], "id-comms": []},
            names={"id-default": SCARLETT, "id-comms": HEADSET},
        )

        report = query_echo_canceller()

        assert [(r.device_name, r.has_echo_canceller) for r in report.roles] == [
            (SCARLETT, True),
            (HEADSET, False),
        ]
        assert report.has_echo_canceller is False

    def test_an_empty_default_capture_id_is_a_failure(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        _install_fake_winsdk(
            monkeypatch,
            ids={0: "", 1: "id-comms"},
            # The empty id has effects it must never be asked for: without
            # them, code that dropped the check below would raise KeyError
            # instead of reaching this test's assertion, and the gate would
            # report a catch that proved nothing
            # (mutation_gate_audio_suppression_auto.py,
            # the-empty-device-id-is-accepted).
            effects_by_id={"": [1], "id-comms": [1]},
            names={"id-comms": HEADSET},
        )

        with pytest.raises(WinRTError):
            query_echo_canceller()

        assert await_decision_on(query_echo_canceller) is True
        assert len(_records(caplog, logging.WARNING)) == 1

    def test_the_device_name_falls_back_to_the_device_id(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        _install_fake_winsdk(
            monkeypatch,
            ids={0: "id-default", 1: "id-default"},
            effects_by_id={"id-default": [1]},
            names={},  # every name lookup fails
        )

        report = query_echo_canceller()

        assert [role.device_name for role in report.roles] == [
            "id-default",
            "id-default",
        ]
        assert await_decision_on(lambda: report) is False
        assert _decision_lines(caplog) == [
            "Audio suppression off: Windows reports an echo canceller for id-default"
        ]


def await_decision_on(query):
    """Run decide_audio_suppression on its own loop, for a synchronous test."""
    return asyncio.run(decide_audio_suppression("auto", query=query))


# ---------------------------------------------------------------------------
# The shipped template and the published reference
# ---------------------------------------------------------------------------


def _template_lines() -> list[str]:
    return _EXAMPLE_CONFIG.read_text(encoding="utf-8").splitlines()


def _comment_block(key: str) -> list[str]:
    """The contiguous comment lines directly above ``key``."""
    lines = _template_lines()
    index = next(
        i for i, line in enumerate(lines) if line.startswith(f"{key} =")
    )
    block: list[str] = []
    cursor = index - 1
    while cursor >= 0 and lines[cursor].startswith("#"):
        block.insert(0, lines[cursor])
        cursor -= 1
    return block


def _sentences(block: list[str]) -> list[str]:
    joined = " ".join(line.lstrip("#").strip() for line in block)
    return [part.strip() for part in joined.split(". ") if part.strip()]


class TestTheShippedTemplate:
    """config.toml.example ships "auto" and explains all three pauses."""

    def test_the_template_ships_auto(self):
        with open(_EXAMPLE_CONFIG, "rb") as handle:
            shipped = tomllib.load(handle)["ENABLE_AUDIO_SUPPRESSION"]

        assert shipped == AUDIO_SUPPRESSION_DEFAULT

    @pytest.mark.parametrize("key", SUPPRESSION_KEYS)
    def test_each_suppression_key_has_a_comment_block_directly_above_it(self, key):
        block = _comment_block(key)

        assert len(block) >= 3
        for sentence in _sentences(block):
            assert len(sentence.split()) <= 20, sentence
        joined = " ".join(_sentences(block)).lower()
        # David's wording rule (11:09): the present tense, never a promise.
        assert "will " not in f"{joined} "
        assert "coming change" not in joined

    def test_the_sonos_and_idle_blocks_say_why_they_matter_with_an_echo_canceller(self):
        sonos = " ".join(_sentences(_comment_block("ENABLE_SONOS_SUPPRESSION")))
        idle = " ".join(_sentences(_comment_block("ENABLE_IDLE_SUPPRESSION")))

        assert "never passes through this computer" in sonos
        assert "empty room" in idle

    def test_the_reference_entry_describes_three_values(self):
        # knowledge/helpdoc/ is a helpdoc pipeline source, and the release
        # manifest prunes it, so this sidecar file is absent from an
        # exported tree while every other test in this file still runs
        # there (wh-test-release-2026-09.2.3).
        if not _CONFIG_DESCRIPTIONS.is_file():
            pytest.skip(
                "development-only helpdoc sources are absent from this checkout"
            )
        with open(_CONFIG_DESCRIPTIONS, "rb") as handle:
            entries = tomllib.load(handle)["config"]
        entry = next(
            item for item in entries if item["key"] == AUDIO_SUPPRESSION_SETTING
        )

        assert entry["valid_values"] == '"auto", true, false'
        assert (
            "services/wheelhouse/utils/audio_suppression_decision.py"
            in entry["consumer_sources"]
        )


# ---------------------------------------------------------------------------
# What StateManager does with the decision
# ---------------------------------------------------------------------------


class TestTheStateManagerFollowsTheDecision:
    """The three reads that used to consult the config key now read the decision."""

    def test_speech_enabled_follows_the_decision_not_the_setting(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm.config_service._config["ENABLE_AUDIO_SUPPRESSION"] = False

        assert sm.speech_enabled is False

        sm.apply_audio_suppression_decision(False)
        assert sm.speech_enabled is True

        sm.apply_audio_suppression_decision(True)
        assert sm.speech_enabled is False

    def test_the_ptt_refusal_reason_names_audio_only_when_the_decision_is_on(self, sm):
        sm._ptt_active = True
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm._speech_suppressed_by_idle = True
        sm._ptt_audio_override = False
        sm.config_service._config["ENABLE_AUDIO_SUPPRESSION"] = True

        sm.apply_audio_suppression_decision(False)
        assert sm._ptt_refusal_reason() == (
            "Listening is paused because the computer is idle."
        )

        sm.apply_audio_suppression_decision(True)
        assert sm._ptt_refusal_reason() == (
            "System audio is suppressing listening. Pause it before using push to talk."
        )

    def test_ptt_stop_uses_the_decision(self, sm):
        sm._ptt_active = True
        sm._audio_playing_at_ptt_start = True
        sm._ptt_mute_confirmed = True
        sm._ptt_audio_recheck_handle = None
        sm.config_service._config["ENABLE_AUDIO_SUPPRESSION"] = True
        sm.apply_audio_suppression_decision(False)

        sm.ptt_stop()

        assert sm._ptt_audio_recheck_handle is None
