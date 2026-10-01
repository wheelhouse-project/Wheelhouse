"""Window commands that no longer need the safety word (wh-safety-word-free-commands, stage S1).

activate <app>, switch to <app>, show <app>, minimize <app> and maximize <app>
run without the safety word. "go to <app>" and close / exit / quit <app> keep
it. The safety word now also decides what a FAILED window command does:

* no safety word: the whole utterance is typed as dictation and no notice
  appears, because the user may simply have been speaking;
* safety word spoken: a notice explains the failure and nothing is typed.

The Input process can also STOP a rule on purpose (a program was started but
its window did not come forward, or the answer never arrived). The Logic
process then sends no further step, types nothing and shows no notice, so the
Alt+F4, Win+Down or Win+Up of close, minimize and maximize never reaches the
window that happens to be in front.

Levels covered: the shipped pattern rows, the rule engine (TextParser), the
speech processor (_execute_command), and the router-to-processor path.
"""
import asyncio
import logging
import queue
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_WHEELHOUSE_DIR = _TESTS_DIR.parent
_REPO_ROOT = _WHEELHOUSE_DIR.parents[1]
_PATTERNS_PATH = _WHEELHOUSE_DIR / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_WHEELHOUSE_DIR))

from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor

from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _hotkey_key_lists,
    _inserted_text,
    _send_phrase,
)

# (utterance, the hotkey its second step sends or None, the words after the verb)
FREED = [
    ("activate notepad", None),
    ("switch to notepad", None),
    ("show notepad", None),
    ("minimize notepad", ["win", "down"]),
    ("maximize notepad", ["win", "up"]),
]
FREED_IDS = [utterance for utterance, _ in FREED]
WITH_HOTKEY = [row for row in FREED if row[1]] + [("close notepad", ["alt", "f4"])]
WITH_HOTKEY_IDS = [utterance for utterance, _ in WITH_HOTKEY]
# close, exit and quit share one row (close-app) and keep the safety word.
CLOSING = [
    ("close notepad", ["alt", "f4"]),
    ("exit notepad", ["alt", "f4"]),
    ("quit notepad", ["alt", "f4"]),
]
FAILING = FREED + CLOSING
FAILING_IDS = [utterance for utterance, _ in FAILING]

NO_PROGRAM = "No program matched notepad."
REFUSED = "Windows refused to bring notepad to the front"


def _refusal(message, outcome=None):
    """The exception the Logic process raises for an Input refusal reply."""
    from app import InputRefused

    reply = {
        "request_id": "r1", "error": True, "refusal": True,
        "message": message, "action": "activate_window",
    }
    if outcome:
        reply["outcome"] = outcome
    return InputRefused(message, reply)


class ScriptedApp:
    """Records every payload; activate_window answers as the test scripts."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self.request_kwargs: List[tuple] = []
        self.activate_error = None

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: dict, *args, **kwargs):
        self.actions.append({"action": action, "params": params})
        self.request_kwargs.append((action, kwargs))
        if action == "activate_window" and self.activate_error is not None:
            raise self.activate_error
        return {"status": "ok"}

    def hotkeys(self):
        return [
            a["params"]["keys"] for a in self.actions
            if a.get("action") == "hotkey_action"
        ]

    def activations(self):
        return [
            a["params"]["target"] for a in self.actions
            if a.get("action") == "activate_window"
        ]

    def dictated(self):
        return [
            a["params"]["insertion_string"] for a in self.actions
            if a.get("action") == "intelligent_insert_text"
        ]


@pytest.fixture
def app():
    return ScriptedApp()


@pytest.fixture
def gui_queue():
    return queue.Queue()


@pytest.fixture
def parser(app, gui_queue):
    handler = MagicMock()
    handler.app = app
    handler.logic_controller.state_manager.state_to_gui_queue = gui_queue
    return TextParser(handler, PatternCatalog(str(_PATTERNS_PATH)))


@pytest.fixture
def processor(parser, app):
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=parser.pattern_catalog,
        text_parser=parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
    )
    parser.speech_handler.speech_processor = proc
    return proc


def _notices(gui_queue) -> List[str]:
    found = []
    while not gui_queue.empty():
        item = gui_queue.get_nowait()
        if item.get("action") == "show_notification":
            found.append(item["message"])
    return found


def _typed_text(app) -> List[str]:
    """Every string the app was asked to type, from any typing action."""
    typed = []
    for a in app.actions:
        params = a.get("params") or {}
        for key in ("insertion_string", "text"):
            if isinstance(params.get(key), str):
                typed.append(params[key])
    return typed


@pytest.fixture(scope="module")
def rows():
    return tomllib.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))["pattern"]


# ---------------------------------------------------------------------------
# Item 2 and item 6: which commands run without the safety word
# ---------------------------------------------------------------------------

class TestShippedRows:
    def test_the_freed_rows_do_not_require_the_safety_word(self, rows):
        by_id = {row["doc_id"]: row for row in rows}
        for doc_id in (
            "activate-app", "switch-to-app", "show-app",
            "minimize-app", "maximize-app",
        ):
            assert not by_id[doc_id].get("requires_hotword"), doc_id

    def test_go_to_app_is_its_own_row_and_keeps_the_safety_word(self, rows):
        by_id = {row["doc_id"]: row for row in rows}
        assert by_id["go-to-app"]["requires_hotword"] is True
        assert by_id["go-to-app"]["pattern"] == r"^go to\s+(.+)$"
        assert by_id["switch-to-app"]["pattern"] == r"^switch to\s+(.+)$"

    def test_close_app_keeps_the_safety_word(self, rows):
        by_id = {row["doc_id"]: row for row in rows}
        assert by_id["close-app"]["requires_hotword"] is True

    def test_go_to_app_precedes_cursor_navigate(self, rows):
        ids = [row["doc_id"] for row in rows]
        assert ids.index("go-to-app") < ids.index("cursor-navigate")
        assert ids.index("switch-to-app") < ids.index("cursor-navigate")

    def test_the_awaited_activate_steps(self, rows):
        by_id = {row["doc_id"]: row for row in rows}
        for doc_id in ("activate-app", "switch-to-app", "go-to-app", "show-app"):
            step = by_id[doc_id]["actions"][0]
            assert step["function"] == "activate"
            assert step.get("awaits_done") is True, doc_id


class TestWhichCommandsRunWithoutTheSafetyWord:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FREED, ids=FREED_IDS)
    async def test_a_freed_command_runs_without_the_safety_word(
        self, parser, app, utterance, hotkey
    ):
        assert await parser.parse_and_execute(
            utterance, authorized_command=False
        ) is True

        assert app.activations() == ["notepad"]
        assert app.hotkeys() == ([hotkey] if hotkey else [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FREED, ids=FREED_IDS)
    async def test_the_safety_word_before_a_freed_command_still_runs_it(
        self, parser, app, utterance, hotkey
    ):
        assert await parser.parse_and_execute(
            utterance, authorized_command=True
        ) is True

        assert app.activations() == ["notepad"]
        assert app.hotkeys() == ([hotkey] if hotkey else [])

    @pytest.mark.asyncio
    async def test_go_to_app_does_not_activate_without_the_safety_word(
        self, parser, app
    ):
        await parser.parse_and_execute(
            "go to notepad", authorized_command=False
        )

        assert app.activations() == []

    @pytest.mark.asyncio
    async def test_go_to_app_activates_with_the_safety_word(self, parser, app):
        assert await parser.parse_and_execute(
            "go to notepad", authorized_command=True
        ) is True

        assert app.activations() == ["notepad"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "utterance,keys",
        [("go to end of line", ["end"]), ("go to top", ["ctrl", "home"])],
    )
    async def test_go_to_cursor_movement_runs_without_the_safety_word(
        self, parser, app, utterance, keys
    ):
        assert await parser.parse_and_execute(
            utterance, authorized_command=False
        ) is True

        assert app.activations() == []
        assert keys in app.hotkeys()

    @pytest.mark.asyncio
    async def test_close_app_does_not_run_without_the_safety_word(
        self, parser, app
    ):
        await parser.parse_and_execute(
            "close notepad", authorized_command=False
        )

        assert app.activations() == []
        assert ["alt", "f4"] not in app.hotkeys()


# ---------------------------------------------------------------------------
# The rule engine: what a refused or stopped activate does
# ---------------------------------------------------------------------------

class TestRuleEngineFailureRecord:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FAILING, ids=FAILING_IDS)
    async def test_a_refused_activate_fails_the_rule_with_a_notice_text(
        self, parser, app, utterance, hotkey
    ):
        from speech.actions import StepFailed

        app.activate_error = _refusal(NO_PROGRAM)

        assert await parser.parse_and_execute(
            utterance, authorized_command=True
        ) is False

        assert isinstance(parser.last_step_failure, StepFailed)
        assert parser.last_step_failure.notice == NO_PROGRAM
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    async def test_the_failure_record_is_reset_by_the_next_call(
        self, parser, app
    ):
        app.activate_error = _refusal(NO_PROGRAM)
        await parser.parse_and_execute("show notepad", authorized_command=True)
        assert parser.last_step_failure is not None

        app.activate_error = None
        await parser.parse_and_execute("show notepad", authorized_command=True)

        assert parser.last_step_failure is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FAILING, ids=FAILING_IDS)
    async def test_a_stopped_rule_is_handled_and_sends_no_key(
        self, parser, app, utterance, hotkey
    ):
        """Item 5, Logic side: no Alt+F4, Win+Down or Win+Up after a stop."""
        app.activate_error = _refusal("did not come forward", "stopped")

        assert await parser.parse_and_execute(
            utterance, authorized_command=True
        ) is True

        assert parser.last_step_failure is None
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FAILING, ids=FAILING_IDS)
    async def test_an_activate_that_never_answers_stops_the_rule(
        self, parser, app, utterance, hotkey
    ):
        app.activate_error = asyncio.TimeoutError()

        assert await parser.parse_and_execute(
            utterance, authorized_command=True
        ) is True

        assert parser.last_step_failure is None
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    async def test_another_error_keeps_todays_behaviour(self, parser, app):
        """A plain RuntimeError is still a rule error with no failure record."""
        app.activate_error = RuntimeError("Input process crashed")

        assert await parser.parse_and_execute(
            "show notepad", authorized_command=True
        ) is False

        assert parser.last_step_failure is None


# ---------------------------------------------------------------------------
# The speech processor: the safety word decides dictation or notice
# ---------------------------------------------------------------------------

class TestProcessorWithoutTheSafetyWord:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FREED, ids=FREED_IDS)
    @pytest.mark.parametrize(
        "message", [NO_PROGRAM, REFUSED], ids=["no-window", "refused"]
    )
    async def test_a_failed_freed_command_is_typed_with_no_notice(
        self, processor, app, gui_queue, utterance, hotkey, message
    ):
        app.activate_error = _refusal(message)

        await processor._execute_command(utterance, hotword_authorized=False)

        assert app.dictated() == [utterance]
        assert _notices(gui_queue) == []
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FREED, ids=FREED_IDS)
    async def test_a_stopped_freed_command_types_nothing_and_says_nothing(
        self, processor, app, gui_queue, utterance, hotkey
    ):
        app.activate_error = _refusal("did not come forward", "stopped")

        await processor._execute_command(utterance, hotword_authorized=False)

        assert app.dictated() == []
        assert _notices(gui_queue) == []
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    async def test_a_freed_command_that_never_got_an_answer_types_nothing(
        self, processor, app, gui_queue
    ):
        app.activate_error = asyncio.TimeoutError()

        await processor._execute_command(
            "minimize notepad", hotword_authorized=False
        )

        assert app.dictated() == []
        assert _notices(gui_queue) == []
        assert app.hotkeys() == []


class TestProcessorWithTheSafetyWord:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("utterance,hotkey", FAILING, ids=FAILING_IDS)
    @pytest.mark.parametrize(
        "message", [NO_PROGRAM, REFUSED], ids=["no-window", "refused"]
    )
    async def test_a_failed_command_shows_a_notice_and_types_nothing(
        self, processor, app, gui_queue, utterance, hotkey, message
    ):
        app.activate_error = _refusal(message)

        await processor._execute_command(utterance, hotword_authorized=True)

        assert _notices(gui_queue) == [message]
        assert app.dictated() == []
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    async def test_go_to_app_failure_shows_a_notice(
        self, processor, app, gui_queue
    ):
        app.activate_error = _refusal(NO_PROGRAM)

        await processor._execute_command(
            "go to notepad", hotword_authorized=True
        )

        assert _notices(gui_queue) == [NO_PROGRAM]
        assert app.dictated() == []

    @pytest.mark.asyncio
    async def test_a_stopped_command_shows_no_notice_and_types_nothing(
        self, processor, app, gui_queue
    ):
        app.activate_error = _refusal("did not come forward", "stopped")

        await processor._execute_command(
            "close notepad", hotword_authorized=True
        )

        assert _notices(gui_queue) == []
        assert app.dictated() == []
        assert app.hotkeys() == []

    @pytest.mark.asyncio
    async def test_a_success_types_nothing_and_shows_nothing(
        self, processor, app, gui_queue
    ):
        await processor._execute_command(
            "minimize notepad", hotword_authorized=True
        )

        assert app.activations() == ["notepad"]
        assert app.hotkeys() == [["win", "down"]]
        assert app.dictated() == []
        assert _notices(gui_queue) == []

    @pytest.mark.asyncio
    async def test_a_failure_without_a_step_record_is_still_dictated(
        self, processor, app, gui_queue
    ):
        """Only a recorded step failure takes the notice branch."""
        await processor._execute_command(
            "press xyzzy", hotword_authorized=True
        )

        assert app.dictated() == ["press xyzzy"]
        assert _notices(gui_queue) == []


class TestFailedCommandTypesLikeDictation:
    """Item 3: "typed as dictation" means spoken replacement words convert."""

    @pytest.mark.asyncio
    async def test_a_replacement_word_is_converted_when_the_words_are_typed(
        self, processor, app, gui_queue
    ):
        app.activate_error = _refusal(NO_PROGRAM)

        await processor._execute_command(
            "activate my files comma please", hotword_authorized=False
        )

        typed = " ".join(_typed_text(app))
        assert "," in typed
        assert "comma" not in typed
        assert "activate my files" in typed and "please" in typed
        assert _notices(gui_queue) == []

    @pytest.mark.asyncio
    async def test_a_command_word_in_the_typed_words_is_not_run(
        self, processor, app
    ):
        """Only replacements run on this path; a command stays typed words."""
        app.activate_error = _refusal(NO_PROGRAM)

        await processor._execute_command(
            "activate my files press enter", hotword_authorized=False
        )

        assert app.hotkeys() == []
        assert app.dictated() == ["activate my files press enter"]


class TestTimeoutBoundsAndWiring:
    @pytest.mark.asyncio
    async def test_the_activate_step_waits_longer_than_the_default_timeout(
        self, parser, app
    ):
        from speech import command_engine

        await parser.parse_and_execute("minimize notepad")

        by_action = dict(app.request_kwargs)
        assert by_action["activate_window"]["timeout_s"] == (
            command_engine.ACTIVATE_TIMEOUT_S
        )
        assert command_engine.ACTIVATE_TIMEOUT_S > 5.0
        assert by_action["activate_window"]["quiet_timeout"] is True
        # Only the activate step is widened; the hotkey step keeps the default.
        assert by_action["hotkey_action"] == {}


@pytest.fixture
def real_ipc(monkeypatch):
    """A real WheelHouseApp behind a TextParser and a SpeechProcessor.

    Nothing drains the outbound queue, so every payload the Logic process
    sends stays there for the test to read and answer.
    """
    from speech import command_engine

    shm = MagicMock()
    shm.buf = bytearray(1024 * 64)
    shm.size = 1024 * 64
    shm.name = "test_shm"
    command_ready = MagicMock()
    command_ready.is_set.return_value = False
    ui_ready = MagicMock()
    ui_ready.is_set.return_value = True
    with patch("app.shared_memory.SharedMemory", return_value=shm):
        from app import WheelHouseApp
        real_app = WheelHouseApp(
            shm_name="test_shm",
            command_ready_event=command_ready,
            ui_ready_event=ui_ready,
            response_queue=queue.Queue(),
            shm_bytes=1024 * 64,
            response_timeout_s=0.05,
        )
    monkeypatch.setattr(command_engine, "ACTIVATE_TIMEOUT_S", 0.05)
    gui = queue.Queue()
    handler = MagicMock()
    handler.app = real_app
    handler.logic_controller.state_manager.state_to_gui_queue = gui
    parser = TextParser(handler, PatternCatalog(str(_PATTERNS_PATH)))
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=parser.pattern_catalog,
        text_parser=parser,
        app=real_app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
    )
    handler.speech_processor = proc
    return real_app, proc, gui


def _sent_actions(real_app) -> List[str]:
    sent = []
    while True:
        try:
            sent.append(real_app._outbound_q.get_nowait()["action"])
        except asyncio.QueueEmpty:
            return sent


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestRealIpcPath:
    """The real send_request, rule engine and speech processor together."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hotword", [False, True], ids=["no-word", "word"])
    async def test_a_slow_activate_stops_quietly_with_no_error_record(
        self, real_ipc, caplog, hotword
    ):
        """No answer in time: no ERROR record (it would raise a popup), no
        key, nothing typed, no notice."""
        real_app, proc, gui = real_ipc

        with caplog.at_level(logging.DEBUG):
            await proc._execute_command(
                "minimize notepad", hotword_authorized=hotword
            )

        assert _errors(caplog) == []
        assert _sent_actions(real_app) == ["activate_window"]
        assert _notices(gui) == []

    @pytest.mark.asyncio
    async def test_a_refused_bring_forward_logs_no_error_and_shows_a_notice(
        self, real_ipc, caplog, monkeypatch
    ):
        """Item 4 on the real path: app.send_request, the rule engine and
        the speech processor log nothing at ERROR for a refusal."""
        real_app, proc, gui = real_ipc
        real_app.response_timeout_s = 2.0
        # The activate step waits ACTIVATE_TIMEOUT_S, not
        # response_timeout_s. The fixture's 0.05 s could expire before the
        # demux loop delivers the refusal on a loaded host.
        from speech import command_engine
        monkeypatch.setattr(command_engine, "ACTIVATE_TIMEOUT_S", 2.0)
        demux = asyncio.create_task(real_app._demux_loop())
        try:
            with caplog.at_level(logging.DEBUG):
                run = asyncio.create_task(
                    proc._execute_command(
                        "minimize notepad", hotword_authorized=True
                    )
                )
                item = await asyncio.wait_for(real_app._outbound_q.get(), 2)
                real_app.response_queue.put({
                    "request_id": item["request_id"], "error": True,
                    "refusal": True, "message": REFUSED,
                    "action": "activate_window",
                })
                await asyncio.wait_for(run, 3)
        finally:
            demux.cancel()
            await asyncio.gather(demux, return_exceptions=True)

        assert _errors(caplog) == []
        assert _notices(gui) == [REFUSED]
        assert _sent_actions(real_app) == []


# ---------------------------------------------------------------------------
# Router to processor, word by word
# ---------------------------------------------------------------------------

@pytest.fixture
async def harness():
    h = SpeechPipelineHarness()
    h.gui_queue = queue.Queue()
    h.mock_speech_handler.logic_controller.state_manager.state_to_gui_queue = (
        h.gui_queue
    )
    await h.start()
    yield h
    await h.stop()


def _script_activate_failure(harness, exc):
    original = harness.mock_app.send_request

    async def send_request(action, params, *args, **kwargs):
        await original(action, params)
        if action == "activate_window":
            raise exc
        return True

    harness.mock_app.send_request = send_request


class TestEndToEnd:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [("activate", "notepad"), ("switch", "to", "notepad"),
         ("show", "notepad"), ("minimize", "notepad"),
         ("maximize", "notepad")],
    )
    async def test_a_freed_command_runs_with_no_safety_word(
        self, harness, words
    ):
        await _send_phrase(harness, words)

        assert "activate_window" in harness.mock_app.get_all_actions()
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    async def test_the_safety_word_before_a_freed_command_still_runs_it(
        self, harness
    ):
        await _send_phrase(harness, ("activate", "notepad"), prefix=("x-ray",))

        assert "activate_window" in harness.mock_app.get_all_actions()
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    async def test_a_failed_freed_command_is_typed_whole(self, harness):
        _script_activate_failure(harness, _refusal(NO_PROGRAM))

        await _send_phrase(harness, ("activate", "notepad"))

        assert " ".join(_inserted_text(harness)) == "activate notepad"
        assert _notices(harness.gui_queue) == []

    @pytest.mark.asyncio
    async def test_a_failed_freed_command_with_the_safety_word_shows_a_notice(
        self, harness
    ):
        _script_activate_failure(harness, _refusal(NO_PROGRAM))

        await _send_phrase(harness, ("activate", "notepad"), prefix=("x-ray",))

        assert _inserted_text(harness) == []
        assert _notices(harness.gui_queue) == [NO_PROGRAM]

    @pytest.mark.asyncio
    async def test_a_failed_close_shows_a_notice_and_sends_no_key(
        self, harness
    ):
        _script_activate_failure(harness, _refusal(NO_PROGRAM))

        await _send_phrase(harness, ("close", "notepad"), prefix=("x-ray",))

        assert _inserted_text(harness) == []
        assert _hotkey_key_lists(harness) == []
        assert _notices(harness.gui_queue) == [NO_PROGRAM]

    @pytest.mark.asyncio
    async def test_go_to_app_without_the_safety_word_activates_nothing(
        self, harness
    ):
        await _send_phrase(harness, ("go", "to", "notepad"))

        assert "activate_window" not in harness.mock_app.get_all_actions()
