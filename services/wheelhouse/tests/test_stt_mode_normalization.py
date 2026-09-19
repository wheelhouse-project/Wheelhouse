"""One settings value cannot split startup, and an unusable one is reported.

wh-stt-mode-normalize. `stt.mode` used to be read raw at seven places, and
the three readings disagreed about every value that is neither "remote" nor
"in_process": ServiceManager treated anything except "in_process" as remote
and built the launcher, LogicController started the WebSocket only for the
exact string "remote" and then started neither STT path, and StateManager
treated only the exact string "remote" as remote and otherwise reported the
in-process provider. A user whose file said `mode = "remotee"` got remote
machinery, no engine, and a provider menu with nothing in it -- no working
hands-free recovery (wh-remote-stt-robustness.2.4). The fix routed all seven
reads through one helper.

wh-in-process-capture-removal deleted the in-process engine. With one mode
left there is nothing for a helper to decide, so the seven reads are gone
and `warn_if_stt_mode_unsupported` is what remains of it: startup calls it
once, and it writes one WARNING per process for a value that is present and
unusable and nothing at all for a key that is simply absent. It returns no
answer, so no test here asserts one. No configuration error and no startup
refusal, because refusing to start takes away the voice control the user
needs to fix the file.

The `stt.mode` key itself stays: it is what gives the WARNING something to
report, and without it a settings file from the in-process build would start
remote without a word about the setting it lost.

These tests drive the real ConfigService from a real settings file rather
than a fake: the four unusable shapes below (`"remotee"`, `true`, `7`,
`"in_process"`) are all valid TOML, and it is TOML that produces them.
"""

from __future__ import annotations

import asyncio
import logging

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from services.wheelhouse.config_service import (
    ConfigService,
    forget_stt_mode_warnings,
    warn_if_stt_mode_unsupported,
)


# The value the accepted-value half of the WARNING has to name.
ACCEPTED_VALUE = "remote"


def _settings(mode_line: str) -> str:
    """A settings file whose [stt] section carries `mode_line` verbatim."""
    return (
        "[stt]\n"
        f"{mode_line}"
        'last_provider = "parakeet_tdt"\n'
        'provider = "google"\n'
    )


def _config_service(tmp_path, mode_line: str) -> ConfigService:
    path = tmp_path / "config.toml"
    path.write_text(_settings(mode_line), encoding="utf-8")
    return ConfigService(str(path))


# Every mode a settings file can carry that the program cannot use, with the
# text the WARNING must quote. `None` marks the case that logs nothing.
# Each id is spelled out and free of spaces so a mutation gate can name the
# individual case (mutation-gate skill, the parametrized-id rule).
UNUSABLE_MODES = [
    pytest.param('mode = "remotee"\n', "'remotee'", id="unrecognized-string"),
    pytest.param("mode = true\n", "True", id="boolean-true"),
    pytest.param("mode = 7\n", "7", id="integer-seven"),
    # wh-in-process-capture-removal. A file left over from the in-process
    # build is unusable in exactly the same way a typo is, so it belongs in
    # this list rather than in a case of its own.
    pytest.param('mode = "in_process"\n', "'in_process'", id="removed-mode"),
    pytest.param("", None, id="missing-key"),
]


@pytest.fixture(autouse=True)
def _forget_warned_modes():
    """The warn-once record is process state; no test may inherit it."""
    forget_stt_mode_warnings()
    yield
    forget_stt_mode_warnings()


class TestTheHelperWarnsOncePerProcess:
    """The user is told once, and only about a value that is really there.

    This is the whole of what the helper does since
    wh-in-process-capture-removal, so it is the whole of what can be
    asserted about a direct call to it.
    """

    @pytest.mark.parametrize(
        "mode_line, quoted",
        [case for case in UNUSABLE_MODES if case.values[1] is not None],
    )
    def test_an_unusable_value_is_named_in_one_warning(
        self, tmp_path, caplog, mode_line, quoted
    ):
        config = _config_service(tmp_path, mode_line)

        with caplog.at_level(
            logging.WARNING, logger="services.wheelhouse.config_service"
        ):
            warn_if_stt_mode_unsupported(config)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert quoted in message
        assert ACCEPTED_VALUE in message

    def test_a_missing_key_is_silent(self, tmp_path, caplog):
        config = _config_service(tmp_path, "")

        with caplog.at_level(
            logging.WARNING, logger="services.wheelhouse.config_service"
        ):
            warn_if_stt_mode_unsupported(config)

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_the_recognized_mode_is_silent(self, tmp_path, caplog):
        config = _config_service(tmp_path, 'mode = "remote"\n')

        with caplog.at_level(
            logging.WARNING, logger="services.wheelhouse.config_service"
        ):
            warn_if_stt_mode_unsupported(config)

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_repeated_reads_warn_only_once(self, tmp_path, caplog):
        """Three reads of the same bad value produce one WARNING.

        The record was added when state_manager._get_current_stt_mode ran
        on every state update, three seconds apart, and a per-call warning
        would have filled the log. wh-in-process-capture-removal deleted
        that reader and left startup as the only caller, but once-per-
        process stays a property of the function rather than of who calls
        it.
        """
        config = _config_service(tmp_path, 'mode = "remotee"\n')

        with caplog.at_level(
            logging.WARNING, logger="services.wheelhouse.config_service"
        ):
            warn_if_stt_mode_unsupported(config)
            warn_if_stt_mode_unsupported(config)
            warn_if_stt_mode_unsupported(config)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


class TestInProcessIsNoLongerAMode:
    """wh-in-process-capture-removal. The WARNING stops offering the mode.

    That a settings file still saying `mode = "in_process"` warns once is
    covered by the `removed-mode` case in UNUSABLE_MODES above. What only
    this test pins is the wording: the message quotes the value the user
    wrote, and it must not go on naming that same value as something the
    program still accepts. The mode key stays in the settings file
    precisely so this warning has something to report; a file whose mode
    is never read would start remote in silence.
    """

    def test_the_warning_names_remote_as_the_only_mode(self, tmp_path, caplog):
        config = _config_service(tmp_path, 'mode = "in_process"\n')

        with caplog.at_level(
            logging.WARNING, logger="services.wheelhouse.config_service"
        ):
            warn_if_stt_mode_unsupported(config)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "'in_process'" in message
        assert '"remote"' in message
        assert "only" in message
        # The quoted-back value is the one permitted occurrence.
        assert "in_process" not in message.replace("'in_process'", "")


def _service_manager(config):
    from service_manager import ServiceManager

    loop = asyncio.new_event_loop()
    app = MagicMock()
    app.get_screen_dimensions.return_value = (1920, 1080)
    manager = ServiceManager(config, MagicMock(), loop, app, MagicMock())
    return manager, loop


class TestServiceManagerBuildsTheRemoteLauncher:
    """initialize_services must reach the same decision as startup."""

    @patch("service_manager.BraviaControl")
    @patch("service_manager.AudioMonitor")
    @patch("service_manager.MouseHandler")
    @patch("service_manager.SpeechHandler")
    @patch("service_manager.PluginRegistry")
    @patch("service_manager.RemoteSTTLauncher")
    @pytest.mark.parametrize("mode_line, _quoted", UNUSABLE_MODES)
    def test_an_unusable_mode_builds_the_remote_launcher(
        self,
        mock_launcher_cls,
        _plugin,
        _speech,
        _mouse,
        _audio,
        _bravia,
        tmp_path,
        mode_line,
        _quoted,
    ):
        launcher = MagicMock()
        launcher.discover_providers.return_value = [{"name": "parakeet_tdt"}]
        mock_launcher_cls.return_value = launcher
        manager, loop = _service_manager(_config_service(tmp_path, mode_line))

        try:
            manager.initialize_services()
        finally:
            loop.close()

        assert manager.remote_stt_launcher is launcher
        manager.state_manager.set_remote_stt_launcher.assert_called_once_with(
            launcher
        )


class _StopStartup(Exception):
    """Ends LogicController.main after the STT branch has run.

    main() catches every exception, logs it, and shuts down, so raising from
    start_services stops the startup path without leaving a task or a socket
    behind. Since wh-audio-suppression-auto, start_services is no longer the
    first statement after the branch under test: the sound-pause decision runs
    first, and _run_startup patches it away.
    """


def _startup_controller(config):
    """A LogicController stand-in for driving main() through the STT branch."""
    controller = MagicMock()
    controller.config_service = config
    controller.gui_shm_name = None

    controller.app.start = AsyncMock()
    controller.shutdown = AsyncMock()

    services = controller.service_manager
    services.speech_handler.speech_processor.start = AsyncMock()
    services.start_services.side_effect = _StopStartup()
    return controller


async def _run_startup(controller, decide=None):
    """Drive LogicController.main; `decide` stands in for the sound-pause query.

    decide_audio_suppression runs before start_services and would ask Windows
    about the real microphone (wh-audio-suppression-auto), so it is always
    replaced. A caller that wants to assert on that call passes its own mock.
    """
    from main import LogicController

    # signal.signal installs a real process-wide handler; the startup path is
    # not what these tests are about, and the suite must leave no side effect.
    with patch("main.signal"), patch(
        "main.decide_audio_suppression",
        decide if decide is not None else AsyncMock(return_value=True),
    ):
        await LogicController.main(controller, MagicMock())


class TestStartupStartsTheRemotePath:
    """LogicController.main must not start remote machinery with no engine."""

    @pytest.mark.parametrize("mode_line, _quoted", UNUSABLE_MODES)
    def test_an_unusable_mode_starts_the_websocket_and_the_remote_path(
        self, tmp_path, mode_line, _quoted
    ):
        controller = _startup_controller(_config_service(tmp_path, mode_line))

        asyncio.run(_run_startup(controller))

        # wh-in-process-capture-removal deleted the start_websocket flag that
        # the in-process mode passed as False, so "the WebSocket started" is
        # now the bare fact that app.start ran.
        controller.app.start.assert_awaited_once()
        controller.service_manager.start_remote_stt.assert_called_once_with()

    @pytest.mark.parametrize(
        "mode_line, quoted",
        [case for case in UNUSABLE_MODES if case.values[1] is not None],
    )
    def test_the_startup_names_the_unusable_mode_in_one_warning(
        self, tmp_path, caplog, mode_line, quoted
    ):
        """Startup reads stt.mode through the helper, and warns once.

        wh-in-process-capture-removal: the helper answers nothing now, so
        the WARNING is the only thing a caller can be seen to get from it.
        That makes this test the one catcher for a mutation that reads the
        key at startup without going through the helper.
        """
        controller = _startup_controller(_config_service(tmp_path, mode_line))

        with caplog.at_level(
            logging.WARNING, logger="services.wheelhouse.config_service"
        ):
            asyncio.run(_run_startup(controller))

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "services.wheelhouse.config_service"
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert quoted in message
        assert ACCEPTED_VALUE in message
