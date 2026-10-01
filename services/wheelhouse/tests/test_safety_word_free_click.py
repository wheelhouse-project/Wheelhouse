"""click, clicks and tap run without the safety word (wh-safety-word-free-commands, stage S2).

"click <name or number>" (also "clicks" and "tap") no longer needs the safety
word. The safety word now decides what a click that did nothing does:

* no safety word: the whole utterance is typed as dictation and no notice
  appears, because the user may simply have been speaking;
* safety word spoken: the click notice explains the failure and nothing is
  typed.

Three outcomes mean "nothing was clicked" and follow that rule: no control
has the name (not_found), clicking is turned off (disabled_by_config) and the
control walk ran out of time (walk_deadline_exceeded), plus a click target
that is only punctuation. An ambiguous match and a timeout keep their own
notices and never type: the click may still run in the Input process.

The processor tests drive the REAL patterns, the REAL TextParser and the REAL
LogicController.forward_click_element (bound to a spec'd mock controller, the
rig of tests/test_click_flow.py); only the Input process reply is scripted.
"""
from __future__ import annotations

import asyncio
import queue
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_WHEELHOUSE_DIR = _TESTS_DIR.parent
_REPO_ROOT = _WHEELHOUSE_DIR.parents[1]
_PATTERNS_PATH = _WHEELHOUSE_DIR / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_WHEELHOUSE_DIR))

from click_notice_toast_wording import compose_click_notice_wording
from shared.click_element import ClickElementResponse
from shared.click_notice import ClickNoticeEvent
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from ui.click_config import ClickConfig
from ui.element_types import ElementQuery

from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from tests.test_safety_word_free_window_commands import ScriptedApp
from tests.test_speech_pipeline import SpeechPipelineHarness
from tests.test_voice_access_punctuation_router import (
    _inserted_text,
    _send_phrase,
)


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------

def _response(outcome, reason=None, matched_names=(), matched_name=None):
    return ClickElementResponse(
        status="ok" if outcome != "execution_failed" else "error",
        outcome=outcome,
        reason=reason,
        matched_names=tuple(matched_names),
        snapshot_id="walk-1",
        snapshot_summary=None,
        matched_name=matched_name,
        trace_id="t-1",
    ).to_dict()


NOT_FOUND = _response("not_found")
OK = _response("ok", matched_name="Reply")
WALK_DEADLINE = _response("execution_failed", reason="walk_deadline_exceeded")
AMBIGUOUS = _response("ambiguous", matched_names=("Reply", "Reply all"))
OTHER_FAILURE = _response(
    "execution_failed", reason="disabled", matched_name="Reply",
    matched_names=("Reply",),
)


def make_controller(*, enabled=True, send_result=None, send_exc=None):
    """A MagicMock(spec=LogicController) with the click methods bound.

    ``state_manager.state_to_gui_queue`` is a real queue, so both the click
    toast (``show_click_notice``) and a generic notification land in it.
    """
    from main import LogicController

    c = MagicMock(spec=LogicController)
    c.forward_click_element = LogicController.forward_click_element.__get__(c)
    c._forward_click_notice = LogicController._forward_click_notice.__get__(c)
    cfg = ClickConfig.from_raw({"enabled": enabled, "response_timeout_ms": 3000})
    c.click_config = cfg
    c.click_snapshot_summary_cache = ClickSnapshotSummaryCache(
        ttl_seconds=float(cfg.snapshot_ttl_seconds),
    )
    c._click_disabled_notice_shown = False
    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue = queue.Queue()

    captured: Dict[str, Any] = {}

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        captured["action"] = action
        captured["params"] = params
        if send_exc is not None:
            raise send_exc
        return send_result

    c.app = MagicMock()
    c.app.send_request = _send_request
    c._captured = captured
    return c


def gui_items(controller) -> List[Dict[str, Any]]:
    q = controller.state_manager.state_to_gui_queue
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def click_notice_texts(items) -> List[str]:
    """The wording the GUI would render for every click notice in ``items``."""
    texts = []
    for item in items:
        if item.get("action") == "show_click_notice":
            payload = {k: v for k, v in item.items() if k != "action"}
            texts.append(compose_click_notice_wording(
                ClickNoticeEvent.from_dict(payload)
            ))
    return texts


def generic_notices(items) -> List[str]:
    return [
        i["message"] for i in items if i.get("action") == "show_notification"
    ]


def make_processor(controller, app):
    handler = MagicMock()
    handler.app = app
    handler.logic_controller = controller
    parser = TextParser(handler, PatternCatalog(str(_PATTERNS_PATH)))
    proc = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=parser.pattern_catalog,
        text_parser=parser,
        app=app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
    )
    handler.speech_processor = proc
    return proc


@pytest.fixture
def app():
    return ScriptedApp()


async def run(controller, app, text, *, hotword):
    processor = make_processor(controller, app)
    await processor._execute_command(text, hotword_authorized=hotword)


# ---------------------------------------------------------------------------
# Item 2 and item 6: click, clicks and tap run without the safety word
# ---------------------------------------------------------------------------

class TestRunsWithoutTheSafetyWord:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["click", "clicks", "tap"])
    async def test_a_click_runs_with_no_safety_word(self, app, verb):
        c = make_controller(send_result=OK)

        await run(c, app, f"{verb} reply", hotword=False)

        assert c._captured["action"] == "click_element"
        assert c._captured["params"]["query"].name == "reply"
        assert app.dictated() == []
        assert gui_items(c) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["click", "clicks", "tap"])
    async def test_the_safety_word_before_a_click_still_runs_it(
        self, app, verb
    ):
        c = make_controller(send_result=OK)

        await run(c, app, f"{verb} reply", hotword=True)

        assert c._captured["params"]["query"].name == "reply"
        assert app.dictated() == []
        assert gui_items(c) == []


# ---------------------------------------------------------------------------
# Item 3 and item 4: the three nothing-was-clicked outcomes
# ---------------------------------------------------------------------------

class TestNotFound:
    @pytest.mark.asyncio
    async def test_without_the_safety_word_the_words_are_typed_with_no_notice(
        self, app
    ):
        c = make_controller(send_result=NOT_FOUND)

        await run(c, app, "click reply", hotword=False)

        assert c._captured["action"] == "click_element"
        assert app.dictated() == ["click reply"]
        assert gui_items(c) == []

    @pytest.mark.asyncio
    async def test_with_the_safety_word_a_notice_is_shown_and_nothing_typed(
        self, app
    ):
        c = make_controller(send_result=NOT_FOUND)

        await run(c, app, "click reply", hotword=True)

        assert click_notice_texts(gui_items(c)) == ["No match for 'reply'."]
        assert app.dictated() == []


class TestNotFoundTypesLikeDictation:
    @pytest.mark.asyncio
    async def test_a_replacement_word_is_converted_when_the_words_are_typed(
        self, app
    ):
        c = make_controller(send_result=NOT_FOUND)

        await run(c, app, "click reply comma please", hotword=False)

        typed = " ".join(
            (a["params"].get("insertion_string") or a["params"].get("text") or "")
            for a in app.actions if a.get("params")
        )
        assert "," in typed
        assert "comma" not in typed
        assert gui_items(c) == []


class TestWalkDeadlineExceeded:
    @pytest.mark.asyncio
    async def test_without_the_safety_word_the_words_are_typed_with_no_notice(
        self, app
    ):
        c = make_controller(send_result=WALK_DEADLINE)

        await run(c, app, "tap reply", hotword=False)

        assert c._captured["action"] == "click_element"
        assert app.dictated() == ["tap reply"]
        assert gui_items(c) == []

    @pytest.mark.asyncio
    async def test_with_the_safety_word_a_notice_is_shown_and_nothing_typed(
        self, app
    ):
        c = make_controller(send_result=WALK_DEADLINE)

        await run(c, app, "tap reply", hotword=True)

        items = gui_items(c)
        [item] = [i for i in items if i["action"] == "show_click_notice"]
        assert item["outcome"] == "execution_failed"
        assert item["reason"] == "walk_deadline_exceeded"
        assert len(click_notice_texts(items)) == 1
        assert app.dictated() == []


class TestDisabledByConfig:
    @pytest.mark.asyncio
    async def test_without_the_safety_word_the_words_are_typed_with_no_notice(
        self, app
    ):
        c = make_controller(enabled=False)

        await run(c, app, "click reply", hotword=False)

        assert "action" not in c._captured  # the gate is before any IPC
        assert app.dictated() == ["click reply"]
        assert gui_items(c) == []
        # No notice was shown, so the once-per-session flag stays unset.
        assert c._click_disabled_notice_shown is False

    @pytest.mark.asyncio
    async def test_with_the_safety_word_a_notice_is_shown_and_nothing_typed(
        self, app
    ):
        c = make_controller(enabled=False)

        await run(c, app, "click reply", hotword=True)

        items = gui_items(c)
        [item] = [i for i in items if i["action"] == "show_click_notice"]
        assert item["reason"] == "disabled_by_config"
        assert app.dictated() == []
        assert c._click_disabled_notice_shown is True

    @pytest.mark.asyncio
    async def test_a_typed_attempt_does_not_use_up_the_one_notice(self, app):
        c = make_controller(enabled=False)

        await run(c, app, "click reply", hotword=False)
        await run(c, app, "click reply", hotword=True)

        assert len(click_notice_texts(gui_items(c))) == 1


class TestUnparseableTarget:
    @pytest.mark.asyncio
    async def test_without_the_safety_word_the_words_are_typed(self, app):
        c = make_controller(send_result=OK)

        await run(c, app, "click .", hotword=False)

        assert "action" not in c._captured
        assert app.dictated() == ["click ."]
        assert gui_items(c) == []

    @pytest.mark.asyncio
    async def test_with_the_safety_word_a_notice_is_shown_and_nothing_typed(
        self, app
    ):
        c = make_controller(send_result=OK)

        await run(c, app, "click .", hotword=True)

        assert "action" not in c._captured
        assert generic_notices(gui_items(c)) == [
            "Say the name or number of what to click."
        ]
        assert app.dictated() == []


# ---------------------------------------------------------------------------
# Outcomes that never type: the click may have happened or may still happen
# ---------------------------------------------------------------------------

class TestOutcomesThatNeverType:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("hotword", [False, True])
    async def test_ambiguous_shows_its_notice_and_types_nothing(
        self, app, hotword
    ):
        c = make_controller(send_result=AMBIGUOUS)

        await run(c, app, "click reply", hotword=hotword)

        assert click_notice_texts(gui_items(c)) == [
            "Found 'Reply' and 'Reply all' -- be more specific."
        ]
        assert app.dictated() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hotword", [False, True])
    async def test_a_timeout_shows_its_notice_and_types_nothing(
        self, app, hotword
    ):
        """The click can still run in Input, so the words must not be typed."""
        c = make_controller(send_exc=asyncio.TimeoutError())

        await run(c, app, "click reply", hotword=hotword)

        items = gui_items(c)
        [item] = [i for i in items if i["action"] == "show_click_notice"]
        assert item["reason"] == "timeout"
        assert app.dictated() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hotword", [False, True])
    async def test_an_execution_failure_with_a_match_shows_its_notice_only(
        self, app, hotword
    ):
        c = make_controller(send_result=OTHER_FAILURE)

        await run(c, app, "click reply", hotword=hotword)

        items = gui_items(c)
        [item] = [i for i in items if i["action"] == "show_click_notice"]
        assert item["reason"] == "disabled"
        assert app.dictated() == []


# ---------------------------------------------------------------------------
# The awaiter itself: defer_failure_notice
# ---------------------------------------------------------------------------

def _query(name="reply"):
    return ElementQuery(name, None, None, None, name)


class TestForwardClickElementDefersThreeFailures:
    @pytest.mark.asyncio
    async def test_not_found_returns_the_notice_arguments_and_shows_nothing(self):
        c = make_controller(send_result=NOT_FOUND)

        result = await c.forward_click_element(
            _query(), "tr", defer_failure_notice=True,
        )

        assert result["outcome"] == "not_found"
        assert result["spoken_name"] == "reply"
        assert result["trace_id"] == "t-1"
        assert result["snapshot_id"] == "walk-1"
        assert gui_items(c) == []

    @pytest.mark.asyncio
    async def test_walk_deadline_exceeded_returns_the_arguments(self):
        c = make_controller(send_result=WALK_DEADLINE)

        result = await c.forward_click_element(
            _query(), "tr", defer_failure_notice=True,
        )

        assert result["outcome"] == "execution_failed"
        assert result["reason"] == "walk_deadline_exceeded"
        assert gui_items(c) == []

    @pytest.mark.asyncio
    async def test_disabled_by_config_returns_the_arguments_without_the_flag(
        self,
    ):
        c = make_controller(enabled=False)

        result = await c.forward_click_element(
            _query(), "tr", defer_failure_notice=True,
        )

        assert result["reason"] == "disabled_by_config"
        assert result["spoken_name"] == "reply"
        assert "action" not in c._captured
        assert gui_items(c) == []
        assert c._click_disabled_notice_shown is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(send_result=OK),
            dict(send_result=AMBIGUOUS),
            dict(send_result=OTHER_FAILURE),
            dict(send_result={"not": "a response"}),
            dict(send_exc=asyncio.TimeoutError()),
            dict(send_exc=RuntimeError("queue full")),
        ],
        ids=["ok", "ambiguous", "other-reason", "malformed", "timeout",
             "send-failed"],
    )
    async def test_every_other_path_returns_none_and_shows_as_before(
        self, kwargs
    ):
        deferred = make_controller(**kwargs)
        plain = make_controller(**kwargs)

        result = await deferred.forward_click_element(
            _query(), "tr", defer_failure_notice=True,
        )
        expected = await plain.forward_click_element(_query(), "tr")

        assert result is None and expected is None
        assert gui_items(deferred) == gui_items(plain)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs,reason_or_outcome",
        [
            (dict(send_result=NOT_FOUND), "not_found"),
            (dict(send_result=WALK_DEADLINE), "walk_deadline_exceeded"),
            (dict(enabled=False), "disabled_by_config"),
        ],
        ids=["not-found", "walk-deadline", "disabled"],
    )
    async def test_the_default_still_shows_the_notice_at_once(
        self, kwargs, reason_or_outcome
    ):
        c = make_controller(**kwargs)

        result = await c.forward_click_element(_query(), "tr")

        assert result is None
        [item] = gui_items(c)
        assert reason_or_outcome in (item["outcome"], item["reason"])
        if reason_or_outcome == "disabled_by_config":
            assert c._click_disabled_notice_shown is True


# ---------------------------------------------------------------------------
# Router to processor, word by word
# ---------------------------------------------------------------------------

@pytest.fixture
async def harness():
    h = SpeechPipelineHarness()
    await h.start()
    yield h
    await h.stop()


def _wire(harness, controller):
    harness.mock_speech_handler.logic_controller = controller


class TestEndToEnd:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words", [("click", "reply"), ("clicks", "reply"), ("tap", "reply")],
    )
    async def test_a_click_runs_with_no_safety_word(self, harness, words):
        c = make_controller(send_result=OK)
        _wire(harness, c)

        await _send_phrase(harness, words)

        assert c._captured["params"]["query"].name == "reply"
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    async def test_the_safety_word_before_a_click_still_runs_it(self, harness):
        """Item 6: x-ray click reply still clicks."""
        c = make_controller(send_result=OK)
        _wire(harness, c)

        await _send_phrase(harness, ("click", "reply"), prefix=("x-ray",))

        assert c._captured["params"]["query"].name == "reply"
        assert _inserted_text(harness) == []

    @pytest.mark.asyncio
    async def test_a_click_with_no_match_is_typed_whole(self, harness):
        c = make_controller(send_result=NOT_FOUND)
        _wire(harness, c)

        await _send_phrase(harness, ("click", "reply"))

        assert c._captured["action"] == "click_element"
        assert " ".join(_inserted_text(harness)) == "click reply"
        assert gui_items(c) == []

    @pytest.mark.asyncio
    async def test_a_click_with_no_match_after_the_safety_word_shows_a_notice(
        self, harness
    ):
        c = make_controller(send_result=NOT_FOUND)
        _wire(harness, c)

        await _send_phrase(harness, ("click", "reply"), prefix=("x-ray",))

        assert _inserted_text(harness) == []
        assert click_notice_texts(gui_items(c)) == ["No match for 'reply'."]
