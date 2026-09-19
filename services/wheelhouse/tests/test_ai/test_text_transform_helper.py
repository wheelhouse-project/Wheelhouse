"""Tests for the shared capture-send-paste helper (wh-rewrite-extract-shared-body).

fix_text_ai used to hold the whole sequence -- readiness, the processing lock,
capturing the selection, sending it, checking for cancellation, pasting the
answer back. The rewriting commands need the same sequence with a different
request and different displayed wording, so the sequence moved into
_run_ai_text_transform and fix_text_ai became one short caller.

These tests drive the helper directly with a request that is NOT fix_text and
with wording that is not the correction wording. That is the property the
extraction exists for: nothing in the sequence is specific to correcting text.
The correction behaviour itself is still covered by TestFixTextAI in
test_actions.py, which was not touched.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_ai.test_silent_actions import notifications

from ai.providers.openai_compat import ChatResult, ChatStatus


def _ai(ready=True):
    ai = MagicMock()
    ai.cancel_requested = False
    ai.is_ready = MagicMock(return_value=ready)
    ai.recheck_ready = AsyncMock(return_value=ready)
    lock = asyncio.Lock()
    ai._processing_lock = lock
    ai.is_processing = MagicMock(side_effect=lock.locked)
    return ai


def _actions(ai):
    from speech.actions import ActionFunctions

    speech_handler = MagicMock()
    sm = MagicMock()
    sm.ai_service = ai
    lc = MagicMock()
    lc.service_manager = sm
    speech_handler.logic_controller = lc
    return ActionFunctions(speech_handler)


def _run(actions, send, **overrides):
    """Call the helper with pirate-flavoured wording, so nothing can pass by
    accidentally matching the correction wording."""
    kwargs = dict(
        send=send,
        working_word="Rewriting",
        no_text_message="No text to rewrite.",
        failed_message="Rewrite failed. Original text preserved.",
    )
    kwargs.update(overrides)
    return actions._run_ai_text_transform(**kwargs)


def _ok(text):
    return ChatResult(status=ChatStatus.OK, text=text)


def _capture(text, fallback=False, copy_failed=False,
             capture_failed=False):
    """A capture_selected_text result in the extended dict shape."""
    return {
        "text": text,
        "flush_failed": False,
        "select_all_fallback": fallback,
        "copy_failed": copy_failed,
        "capture_failed": capture_failed or copy_failed,
    }


# The caret request that collapses the Ctrl+A fallback selection
# (wh-review-pattern-fixes.28). Pressing Right collapses a selection to
# its end in standard Windows edit controls. Since
# wh-review-pattern-fixes.32 the press rides the acknowledged
# press_key_verified request, not the fire-and-forget command channel;
# this tuple is (action, params) as recorded by _routing_app.
_COLLAPSE_REQUEST = ("press_key_verified", {"key": "right", "repeat": 1})


def _routing_app(app, capture_result, replace_result=None,
                 collapse_result=None):
    """Wire app.send_request to answer per action and record every call.

    ``collapse_result`` may be an exception instance to simulate a
    request failure (timeout, dead channel). Returns the recorded
    (action, params) call list."""
    calls = []

    async def route(action, params=None, **kwargs):
        calls.append((action, params))
        if action == "capture_selected_text":
            return capture_result
        if action == "replace_selected_text":
            return replace_result if replace_result is not None \
                else {"success": True}
        if action == "press_key_verified":
            if isinstance(collapse_result, BaseException):
                raise collapse_result
            return collapse_result if collapse_result is not None \
                else {"success": True}
        return {}

    app.send_request = AsyncMock(side_effect=route)
    app.send_command = AsyncMock()
    return calls


class TestTheSequenceIsNotSpecificToCorrecting:

    @pytest.mark.asyncio
    async def test_the_captured_text_goes_to_the_given_request(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},
            {"success": True},
        ])
        send = AsyncMock(return_value=_ok("rewritten text"))

        await _run(actions, send)

        send.assert_awaited_once_with(ai, "original text")

    @pytest.mark.asyncio
    async def test_the_answer_is_pasted_back(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(side_effect=[
            {"text": "original text", "capture_token": "tok-1",
             "target_hwnd": 4321},
            {"success": True},
        ])

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        paste = app.send_request.call_args_list[1]
        assert paste[0] == ("replace_selected_text",)
        # wh-review-pattern-fixes.45: the replacement carries the
        # identity of the control the capture read from.
        assert paste[1] == {"params": {
            "text": "rewritten text",
            "capture_token": "tok-1",
            "target_hwnd": 4321,
        }}

    @pytest.mark.asyncio
    async def test_the_given_no_text_wording_is_displayed(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(return_value={"text": ""})
        send = AsyncMock()

        await _run(actions, send)

        assert "No text to rewrite." in notifications(actions)
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_given_working_word_is_displayed_and_shown(self):
        """Rewritten for wh-dialog-ownership-token.

        This test asserted the two bare dicts
        {"action": "show_working", "message": "Rewriting..."} and
        {"action": "hide_working"} exactly. Those dicts ARE the rule this
        bead removes -- a message that names no operation -- so the
        assertion could only pass while the defect stood. It still
        checks the working word reaches the dialog and the dialog is
        closed, and it now also requires both messages to name the same
        operation.
        """
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},
            {"success": True},
        ])
        shown = []
        actions._send_gui_action = lambda action_dict: shown.append(action_dict)

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        show = next(m for m in shown if m["action"] == "show_working")
        hide = next(m for m in shown if m["action"] == "hide_working")
        assert show["message"] == "Rewriting..."
        assert show["owner"] == hide["owner"]

    @pytest.mark.asyncio
    async def test_the_transform_names_itself_on_both_messages(self):
        """A1: this operation carries one token through show and close.

        Before the fix neither message carried an owner key, so a
        finished transform closed whatever dialog was up -- including the
        "Loading <engine>" plaque of a provider switch started after it.
        """
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(side_effect=[
            {"text": "original text"},
            {"success": True},
        ])
        shown = []
        actions._send_gui_action = lambda action_dict: shown.append(action_dict)

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        show = next(m for m in shown if m["action"] == "show_working")
        hide = next(m for m in shown if m["action"] == "hide_working")
        assert show["owner"].startswith("ai:")
        assert hide["owner"] == show["owner"]

    @pytest.mark.asyncio
    async def test_two_transforms_do_not_share_a_token(self):
        """A2: one transform cannot close a later AI operation's dialog.

        Two runs must produce two different tokens. Equal tokens would
        let the first transform's close match the second's dialog.
        """
        tokens = []
        for _ in range(2):
            ai = _ai()
            actions = _actions(ai)
            actions.speech_handler.app.send_request = AsyncMock(side_effect=[
                {"text": "original text"},
                {"success": True},
            ])
            shown = []
            actions._send_gui_action = lambda message, sink=shown: sink.append(
                message
            )

            await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

            tokens.append(
                next(m for m in shown if m["action"] == "show_working")["owner"]
            )

        assert tokens[0] != tokens[1]

    @pytest.mark.asyncio
    async def test_an_aborted_transform_still_names_itself(self):
        """A3: the close in the finally runs on paths that raised nothing.

        A failed copy aborts before the show, and the finally still
        sends a close. That close must name this operation, so the
        dialog drops it instead of taking down a provider switch's
        "Loading <engine>" plaque that no one else is watching.
        """
        ai = _ai()
        actions = _actions(ai)
        _routing_app(actions.speech_handler.app, _capture("", copy_failed=True))
        shown = []
        actions._send_gui_action = lambda action_dict: shown.append(action_dict)

        await _run(actions, AsyncMock())

        assert not [m for m in shown if m["action"] == "show_working"]
        hides = [m for m in shown if m["action"] == "hide_working"]
        assert len(hides) == 1
        assert hides[0]["owner"].startswith("ai:")

    @pytest.mark.asyncio
    async def test_the_given_failure_wording_is_displayed(self):
        """The server answered, it just did not do the job."""
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )
        send = AsyncMock(return_value=ChatResult(status=ChatStatus.HTTP_ERROR))

        await _run(actions, send)

        assert "Rewrite failed. Original text preserved." in notifications(actions)


class TestTheProtectionsSurvivedTheExtraction:

    @pytest.mark.asyncio
    async def test_an_unreachable_server_is_named_as_such(self):
        ai = _ai()
        ai.recheck_ready = AsyncMock(return_value=False)
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.TRANSPORT_ERROR)))

        assert "isn't responding" in " ".join(notifications(actions))

    @pytest.mark.asyncio
    async def test_a_cancelled_result_pastes_nothing(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        assert app.send_request.call_count == 1
        assert "Cancelled." in notifications(actions)

    @pytest.mark.asyncio
    async def test_a_cancel_that_lands_while_waiting_pastes_nothing(self):
        """The race the flag exists for: the answer arrives, then the user
        says cancel before the paste."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        async def answer_then_cancel(service, text):
            ai.cancel_requested = True
            return _ok("rewritten text")

        await _run(actions, answer_then_cancel)

        assert app.send_request.call_count == 1
        assert ai.cancel_requested is False

    @pytest.mark.asyncio
    async def test_a_second_request_while_one_is_running_is_refused(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )
        send = AsyncMock(return_value=_ok("rewritten text"))

        await ai._processing_lock.acquire()
        try:
            await _run(actions, send)
        finally:
            ai._processing_lock.release()

        send.assert_not_awaited()
        assert "already processing" in " ".join(notifications(actions)).lower()

    @pytest.mark.asyncio
    async def test_an_unready_service_does_not_capture_anything(self):
        ai = _ai(ready=False)
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock()

        await _run(actions, AsyncMock())

        app.send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_missing_model_is_named_rather_than_blamed_on_the_network(self):
        ai = _ai()
        actions = _actions(ai)
        actions.speech_handler.app.send_request = AsyncMock(
            return_value={"text": "original text"}
        )

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.MODEL_NOT_FOUND)))

        assert "model" in " ".join(notifications(actions)).lower()
        ai.recheck_ready.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unchanged_answer_is_not_pasted(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={"text": "original text"})

        await _run(actions, AsyncMock(return_value=_ok("original text")))

        assert app.send_request.call_count == 1
        assert "No changes needed." in notifications(actions)


class TestFlushFailedCaptureAborts:
    """wh-review-pattern-fixes.26: a capture that failed its
    letter-buffer flush touched neither the clipboard nor the
    selection; the transform must abort the same way."""

    @pytest.mark.asyncio
    async def test_flush_failed_capture_aborts_the_transform(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        app.send_request = AsyncMock(return_value={
            "text": "",
            "flush_failed": True,
            "select_all_fallback": False,
        })
        app.send_command = AsyncMock()
        send = AsyncMock()

        await _run(actions, send)

        send.assert_not_awaited()
        assert app.send_request.call_count == 1
        displayed = " ".join(notifications(actions)).lower()
        assert "original text preserved" in displayed
        # No Ctrl+A fired, so no collapse either.
        app.send_command.assert_not_awaited()


class TestCopyFailedCaptureAborts:
    """wh-review-pattern-fixes.35: a capture whose Ctrl+C delivery
    failed reports copy_failed=True. The selection state is UNKNOWN,
    not empty -- the transform must abort with a copy-failure notice
    and must never show the no-text wording (a false result)."""

    @pytest.mark.asyncio
    async def test_copy_failed_capture_aborts_the_transform(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        _routing_app(app, _capture("", copy_failed=True))
        send = AsyncMock()

        await _run(actions, send)

        send.assert_not_awaited()
        displayed = " ".join(
            str(c) for c in notifications(actions)
        )
        assert "No text to rewrite." not in displayed
        assert "copy" in displayed.lower()
        assert "original text preserved" in displayed.lower()

    @pytest.mark.asyncio
    async def test_copy_failed_with_armed_fallback_still_collapses(self):
        """The fallback copy can fail AFTER a verified Ctrl+A armed the
        whole-field selection: the abort must still collapse it."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(
            app, _capture("", fallback=True, copy_failed=True))
        send = AsyncMock()

        await _run(actions, send)

        send.assert_not_awaited()
        assert calls.count(_COLLAPSE_REQUEST) == 1

    @pytest.mark.asyncio
    async def test_copy_failed_without_fallback_does_not_collapse(self):
        """The first copy failed closed before any Ctrl+A: the user's
        own selection (if any) must be left alone."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("", copy_failed=True))

        await _run(actions, AsyncMock())

        assert _COLLAPSE_REQUEST not in calls


class TestCaptureFailedAborts:
    """wh-review-pattern-fixes.38: a capture that could not determine
    the selection state reports capture_failed=True. The transform must
    abort with a capture-failure notice and must never show the
    no-text wording, which claims a fact the Input process never
    established."""

    @pytest.mark.asyncio
    async def test_capture_failed_aborts_the_transform(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        _routing_app(app, _capture("", capture_failed=True))
        send = AsyncMock()

        await _run(actions, send)

        send.assert_not_awaited()
        displayed = " ".join(str(c) for c in notifications(actions))
        assert "No text to rewrite." not in displayed
        assert "original text preserved" in displayed.lower()

    @pytest.mark.asyncio
    async def test_capture_failed_with_armed_fallback_still_collapses(self):
        """The second sentinel write can fail AFTER a verified Ctrl+A
        armed the whole-field selection: the abort must collapse it."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(
            app, _capture("", fallback=True, capture_failed=True))
        send = AsyncMock()

        await _run(actions, send)

        send.assert_not_awaited()
        assert calls.count(_COLLAPSE_REQUEST) == 1

    @pytest.mark.asyncio
    async def test_capture_failed_without_fallback_does_not_collapse(self):
        """The failure came before any Ctrl+A: the user's own selection
        (if any) must be left alone."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("", capture_failed=True))

        await _run(actions, AsyncMock())

        assert _COLLAPSE_REQUEST not in calls

    @pytest.mark.asyncio
    async def test_genuine_empty_selection_still_shows_the_no_text_message(
            self):
        """The unchanged path: nothing selected and nothing failed."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        _routing_app(app, _capture(""))
        send = AsyncMock()

        await _run(actions, send)

        send.assert_not_awaited()
        assert "No text to rewrite." in notifications(actions)


class TestFallbackSelectionCollapse:
    """wh-review-pattern-fixes.28: every outcome that does not replace
    the captured text collapses a fallback-armed whole-field selection,
    and a failed paste is reported as a failure, never as Done."""

    @pytest.mark.asyncio
    async def test_cancelled_result_collapses_fallback_selection(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("text", fallback=True))

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        assert calls.count(_COLLAPSE_REQUEST) == 1

    @pytest.mark.asyncio
    async def test_failed_result_collapses_fallback_selection(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("text", fallback=True))

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.HTTP_ERROR)))

        assert calls.count(_COLLAPSE_REQUEST) == 1

    @pytest.mark.asyncio
    async def test_unchanged_result_collapses_fallback_selection(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("text", fallback=True))

        await _run(actions, AsyncMock(return_value=_ok("text")))

        assert calls.count(_COLLAPSE_REQUEST) == 1

    @pytest.mark.asyncio
    async def test_empty_capture_after_fallback_collapses_selection(self):
        """Ctrl+A fired but the copy failed: the whole field can still
        be selected, so the no-text exit must collapse too."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("", fallback=True))

        await _run(actions, AsyncMock())

        assert calls.count(_COLLAPSE_REQUEST) == 1

    @pytest.mark.asyncio
    async def test_paste_failure_collapses_and_is_not_displayed_as_done(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(
            app,
            _capture("original text", fallback=True),
            replace_result={"success": False},
        )

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        assert calls.count(_COLLAPSE_REQUEST) == 1
        assert not any(
            "done" in str(c).lower()
            for c in notifications(actions)
        )
        displayed = " ".join(notifications(actions)).lower()
        assert "could not paste" in displayed

    @pytest.mark.asyncio
    async def test_paste_failure_without_fallback_is_still_reported(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(
            app,
            _capture("original text", fallback=False),
            replace_result={"success": False},
        )

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        assert not any(
            "done" in str(c).lower()
            for c in notifications(actions)
        )
        displayed = " ".join(notifications(actions)).lower()
        assert "could not paste" in displayed
        assert _COLLAPSE_REQUEST not in calls

    @pytest.mark.asyncio
    async def test_user_selection_is_not_collapsed_on_cancel(self):
        """A selection the user made themselves (no fallback) is left
        alone when the transform is cancelled."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("text", fallback=False))

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        assert _COLLAPSE_REQUEST not in calls
        app.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_replacement_does_not_collapse(self):
        """The paste consumed the fallback selection; a collapse press
        afterward would only move the caret the user just placed."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(
            app,
            _capture("original text", fallback=True),
            replace_result={"success": True},
        )

        await _run(actions, AsyncMock(return_value=_ok("rewritten text")))

        assert _COLLAPSE_REQUEST not in calls
        app.send_command.assert_not_awaited()
        assert "Done." in notifications(actions)


class TestAcknowledgedCollapse:
    """wh-review-pattern-fixes.32 (c): the fallback-selection collapse
    must not be fire-and-forget. send_command is enqueue-only with no
    response tracking, and press_key_action discards the SendInput
    count, so a dropped Right would leave the whole-field selection
    armed while Logic believes cleanup completed. The collapse goes
    through the acknowledged press_key_verified request, and an
    unacknowledged collapse is surfaced to the user instead of
    completing silently."""

    @pytest.mark.asyncio
    async def test_collapse_uses_acknowledged_request(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        calls = _routing_app(app, _capture("text", fallback=True))

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        assert _COLLAPSE_REQUEST in calls
        app.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_acknowledged_collapse_is_silent(self):
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        _routing_app(app, _capture("text", fallback=True),
                     collapse_result={"success": True})

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        # An acknowledged collapse adds no extra warning to cancellation.
        assert notifications(actions) == ["Cancelled."]

    @pytest.mark.asyncio
    async def test_unacknowledged_collapse_shows_a_notice(self):
        """The Input process reported the Right press was not accepted:
        the whole-field selection may still be armed, and the next
        dictated insertion would overwrite the field. The user must
        hear about it."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        _routing_app(app, _capture("text", fallback=True),
                     collapse_result={"success": False})

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        displayed = " ".join(
            str(c).lower() for c in notifications(actions)
        )
        assert "selected" in displayed

    @pytest.mark.asyncio
    async def test_collapse_request_error_shows_a_notice(self):
        """A collapse request that fails outright (timeout, dead
        channel) is the same unknown-state outcome as a rejected one:
        do not complete silently."""
        ai = _ai()
        actions = _actions(ai)
        app = actions.speech_handler.app
        _routing_app(app, _capture("text", fallback=True),
                     collapse_result=asyncio.TimeoutError())

        await _run(actions, AsyncMock(return_value=ChatResult(
            status=ChatStatus.CANCELLED)))

        displayed = " ".join(
            str(c).lower() for c in notifications(actions)
        )
        assert "selected" in displayed
