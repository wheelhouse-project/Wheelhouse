"""Tests for AppAdapter dispatch logic."""
import pytest
from unittest.mock import MagicMock, patch
from services.wheelhouse.tests.e2e.app_adapter import AppAdapter
from services.wheelhouse.tests.e2e.os_mocks import Recording, make_mock_context


class NameBombMeta(type):
    """Metaclass whose class-name access raises.

    wh-overlay-slow-uia-stale-badges.14.61: the adapter mirrors
    production's malformed-envelope boundary and repeated its unsafe
    type-name renderers, so the two had to be fixed together or an
    end-to-end workflow would report a different failure than the real
    Input process.
    """

    @property
    def __name__(cls):
        raise RuntimeError("type name access exploded")


class NameBombValue(metaclass=NameBombMeta):
    """A command value whose class name refuses to render."""


class TestAppAdapterDispatch:
    """Verify AppAdapter routes action dicts to UIActionHandler methods."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_press_key_dispatches(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "press_key_action",
            "params": {"key": "backspace", "repeat": 1}
        })
        assert len(recording.keystrokes) >= 1
        assert recording.get_keystroke_keys()[0] == ("backspace",)

    @pytest.mark.asyncio
    async def test_hotkey_dispatches(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "hotkey_action",
            "params": {"keys": ["ctrl", "z"], "repeat": 1}
        })
        assert len(recording.keystrokes) >= 1
        assert recording.get_keystroke_keys()[0] == ("ctrl", "z")

    @pytest.mark.asyncio
    async def test_send_request_returns_response(self, setup):
        adapter, recording = setup
        result = await adapter.send_request("press_key_action", {"key": "enter", "repeat": 1})
        assert result is True

    @pytest.mark.asyncio
    async def test_unknown_action_does_not_crash(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "nonexistent_action",
            "params": {}
        })
        # No exception raised


class TestAppAdapterParamsBoundary:
    """wh-overlay-slow-uia-stale-badges.14.25: the adapter mirrors the
    corrected production boundary -- only an ABSENT params key (or an
    omitted None argument) defaults to {}; a supplied non-mapping is
    rejected without dispatch, like input_proc's _extract_params."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_absent_params_key_defaults_to_empty_dict(self, setup):
        adapter, recording = setup
        await adapter.send_command({"action": "retract"})
        # Dispatched with no kwargs; no exception raised.

    @pytest.mark.asyncio
    @pytest.mark.parametrize("supplied", [[], "", 0, False, None])
    async def test_supplied_non_mapping_params_not_dispatched(self, setup, supplied):
        adapter, recording = setup
        await adapter.send_command({
            "action": "press_key_action",
            "params": supplied,
        })
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_request_activate_window_resolves(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.29: input_proc answers
        activate_window before its hasattr branch, so an awaited send of
        it resolves in production and must not time out in E2E."""
        adapter, recording = setup
        assert await adapter.send_request(
            "activate_window", {"window_title": "x"},
        ) is True

    @pytest.mark.asyncio
    async def test_send_request_te_event_ack_resolves(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.29: _te_event_ack is an
        acknowledged input_proc special route."""
        adapter, recording = setup
        assert await adapter.send_request(
            "_te_event_ack",
            {"request_id": "", "op": "show", "editor_hwnd": 0},
        ) is True

    @pytest.mark.asyncio
    async def test_send_request_add_soft_allow_tuple_resolves(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.29: add_soft_allow_tuple
        is an acknowledged input_proc special route."""
        adapter, recording = setup
        assert await adapter.send_request(
            "add_soft_allow_tuple",
            {
                "process_name": "e2e.exe",
                "class_name": "Edit",
                "control_type": "Edit",
            },
        ) is True

    @pytest.mark.asyncio
    async def test_send_request_add_soft_allow_tuple_error_raises(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.29: an error response from
        the special route becomes RuntimeError, as the demux raises it."""
        adapter, recording = setup
        with pytest.raises(RuntimeError):
            await adapter.send_request("add_soft_allow_tuple", {})

    @pytest.mark.asyncio
    async def test_send_request_terminal_editor_cancelled_resolves(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.60: production dispatches
        terminal_editor_cancelled through its own handler, which reads
        only params["request_id"]. The adapter had no route for it, so
        it fell through to the UI handler with **params."""
        adapter, _recording = setup
        assert await adapter.send_request(
            "terminal_editor_cancelled", {"request_id": "editor-session"},
        ) is True

    @pytest.mark.asyncio
    async def test_send_request_terminal_editor_cancelled_ignores_extra_params(
        self, setup,
    ):
        """The production handler consumes one key and ignores the rest,
        so an extra key must not raise TypeError in E2E."""
        adapter, _recording = setup
        assert await adapter.send_request(
            "terminal_editor_cancelled",
            {"request_id": "editor-session", "ignored_by_production": True},
        ) is True

    @pytest.mark.asyncio
    async def test_send_request_terminal_editor_cancelled_error_raises(self, setup):
        """A handler failure becomes the standard error response, which
        the demux raises as RuntimeError."""
        adapter, _recording = setup
        adapter.handler.terminal_editor_cancelled = MagicMock(
            side_effect=RuntimeError("editor cleanup failed")
        )
        with pytest.raises(RuntimeError):
            await adapter.send_request(
                "terminal_editor_cancelled", {"request_id": "editor-session"},
            )

    @pytest.mark.asyncio
    async def test_send_command_drops_a_hostile_type_name_action(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.61: the adapter's
        non-string-action reject renders the type name, so a value whose
        metaclass raises escaped the warning-and-drop path production
        promises."""
        adapter, recording = setup
        await adapter.send_command({"action": NameBombValue()})
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_command_drops_hostile_type_name_params(self, setup):
        """The non-mapping params reject carried the same lookup."""
        adapter, recording = setup
        await adapter.send_command({
            "action": "press_key_action",
            "params": NameBombValue(),
        })
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_request_hostile_type_name_action_raises_runtime_error(self, setup):
        """The awaited form answers RuntimeError, as the demux does.

        The hostile value raises RuntimeError itself, so the assertion
        reads the message: only the adapter's own reject text proves the
        type-name lookup did not escape.
        """
        adapter, _recording = setup
        with pytest.raises(RuntimeError, match="Invalid action"):
            await adapter.send_request(NameBombValue())

    @pytest.mark.asyncio
    async def test_send_request_hostile_type_name_params_raises_runtime_error(self, setup):
        """The awaited params reject carried the same lookup."""
        adapter, _recording = setup
        with pytest.raises(RuntimeError, match="Invalid params"):
            await adapter.send_request("press_key_action", NameBombValue())

    @pytest.mark.asyncio
    async def test_send_request_set_log_level_error_raises(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.55: set_log_level became
        conditionally acknowledged in .14.40 -- a valid level keeps the
        route's historical silence, but a malformed one gets the
        standard error response, which the real demux raises as
        RuntimeError. The adapter had no route for it, so a definitive
        Input-side rejection was reported as an unanswered request."""
        adapter, _recording = setup
        with pytest.raises(RuntimeError):
            await adapter.send_request("set_log_level", {"level": []})

    @pytest.mark.asyncio
    async def test_send_request_set_log_level_valid_still_times_out(self, setup):
        """The valid path answers nothing in production, so an awaited
        send must still raise TimeoutError -- the .14.55 fix must not
        turn the success branch into a resolution."""
        import asyncio
        import logging as _logging
        adapter, _recording = setup
        root = _logging.getLogger()
        saved = root.level
        try:
            with pytest.raises(asyncio.TimeoutError):
                await adapter.send_request("set_log_level", {"level": "DEBUG"})
        finally:
            root.setLevel(saved)

    @pytest.mark.asyncio
    async def test_special_routes_never_use_multiprocessing_queue(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.31: the special-route
        acknowledgement queue must be a synchronous thread queue.
        multiprocessing.Queue hands put() to a feeder thread, so the
        immediate get_nowait() raced (about 6 misses in 100 on the
        reviewer's host). This patch makes any construction of the
        adapter module's multiprocessing Queue name fail loudly, so the
        test fails deterministically if the branch regresses.
        """
        import services.wheelhouse.tests.e2e.app_adapter as adapter_module

        class ForbiddenQueue:
            def __init__(self, *args, **kwargs):
                raise AssertionError(
                    "special routes must not construct multiprocessing.Queue"
                )

        adapter, recording = setup
        with patch.object(adapter_module, "Queue", ForbiddenQueue):
            assert await adapter.send_request(
                "activate_window", {"window_title": "x"},
            ) is True
            assert await adapter.send_request(
                "_te_event_ack",
                {"request_id": "", "op": "show", "editor_hwnd": 0},
            ) is True
            assert await adapter.send_request(
                "add_soft_allow_tuple",
                {
                    "process_name": "e2e.exe",
                    "class_name": "Edit",
                    "control_type": "Edit",
                },
            ) is True
            with pytest.raises(RuntimeError):
                await adapter.send_request("add_soft_allow_tuple", {})

    @pytest.mark.asyncio
    async def test_send_request_unknown_action_raises_timeout(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.27: production never
        answers an unknown action (input_proc warns and continues), so
        the caller gets asyncio.TimeoutError. The adapter must fail the
        same way, not resolve True for a misspelled awaited action."""
        import asyncio
        adapter, recording = setup
        with pytest.raises(asyncio.TimeoutError):
            await adapter.send_request("not_a_real_action", {})
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_request_response_less_route_raises_timeout(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.27: set_log_level is an
        input_proc special case that answers nothing, so an awaited
        send of it times out in production."""
        import asyncio
        adapter, recording = setup
        with pytest.raises(asyncio.TimeoutError):
            await adapter.send_request("set_log_level", {"level": "INFO"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("supplied", [[], "", 0, False])
    async def test_send_request_supplied_non_mapping_raises(self, setup, supplied):
        """wh-overlay-slow-uia-stale-badges.14.26: an awaited request with
        rejected params must FAIL like production (the demux turns the
        input-side error response into a RuntimeError), not resolve True."""
        adapter, recording = setup
        with pytest.raises(RuntimeError):
            await adapter.send_request("press_key_action", supplied)
        assert recording.keystrokes == []


class UnhashableStrAction(str):
    """str subclass with a disabled hash (wh-overlay-slow-uia-stale-badges.14.38)."""

    __hash__ = None  # type: ignore[assignment]


class HashRaisesStrAction(str):
    """str subclass whose hash raises its own exception type (.14.38)."""

    def __hash__(self):
        raise RuntimeError("hash is not supported by this value")


class EqRaisesStrAction(str):
    """str subclass whose equality raises (.14.38)."""

    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("eq is not supported by this value")


class BenignSubDict(dict):
    """Plain dict subclass (.14.38): production rejects non-exact-dict
    params, so the adapter must too, even when the subclass would have
    dispatched cleanly."""


class TestAppAdapterActionBoundary:
    """wh-overlay-slow-uia-stale-badges.14.37: the adapter mirrors the
    production malformed-action contract (.14.33) -- input_proc's
    _validate_action warning-drops a non-string fire-and-forget action
    and answers a non-string request action with the standard error
    response, which the demux raises as RuntimeError. The adapter's
    hasattr raised raw TypeError for both instead.

    wh-overlay-slow-uia-stale-badges.14.38: only an EXACT str action
    and EXACT dict params pass, mirroring the hardened reader boundary
    -- a str subclass with a broken hash raises from the adapter's own
    hasattr, and a raising __eq__ raises from its special-route
    comparisons."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_action", [None, [], {}])
    async def test_send_command_drops_non_string_action(
        self, setup, bad_action,
    ):
        adapter, recording = setup
        await adapter.send_command({"action": bad_action, "params": {}})
        # Warning-dropped without dispatch; no exception raised.
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_action", [None, [], {}])
    async def test_send_request_non_string_action_raises_runtime_error(
        self, setup, bad_action,
    ):
        adapter, recording = setup
        with pytest.raises(RuntimeError):
            await adapter.send_request(bad_action, {})
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_action", [
        UnhashableStrAction("press_key_action"),
        HashRaisesStrAction("press_key_action"),
        # No eq-raises case here: hasattr treats the failed comparison
        # as a miss, so send_command already warning-drops it.
    ], ids=["hash-disabled", "hash-raises"])
    async def test_send_command_drops_str_subclass_action(
        self, setup, bad_action,
    ):
        adapter, recording = setup
        await adapter.send_command({
            "action": bad_action,
            "params": {"key": "enter", "repeat": 1},
        })
        # Warning-dropped without dispatch; no exception raised.
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_action", [
        UnhashableStrAction("press_key_action"),
        HashRaisesStrAction("press_key_action"),
        EqRaisesStrAction("press_key_action"),
    ], ids=["hash-disabled", "hash-raises", "eq-raises"])
    async def test_send_request_str_subclass_action_raises_runtime_error(
        self, setup, bad_action,
    ):
        adapter, recording = setup
        with pytest.raises(RuntimeError, match="Invalid action"):
            await adapter.send_request(
                bad_action, {"key": "enter", "repeat": 1},
            )
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_command_drops_dict_subclass_params(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "press_key_action",
            "params": BenignSubDict(key="enter", repeat=1),
        })
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_request_dict_subclass_params_raises_runtime_error(
        self, setup,
    ):
        adapter, recording = setup
        with pytest.raises(RuntimeError, match="Invalid params"):
            await adapter.send_request(
                "press_key_action", BenignSubDict(key="enter", repeat=1),
            )
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_command_drops_dict_subclass_envelope(self, setup):
        """wh-overlay-slow-uia-stale-badges.14.41: production runs the
        whole PAYLOAD through _read_envelope, whose exact-dict gate
        drops a dict-subclass envelope before any field lookup. The
        adapter must apply the same chokepoint, even when the subclass
        would have dispatched cleanly."""
        adapter, recording = setup
        await adapter.send_command(BenignSubDict(
            action="press_key_action",
            params={"key": "enter", "repeat": 1},
        ))
        assert recording.keystrokes == []

    @pytest.mark.asyncio
    async def test_send_command_poisoned_action_key_neither_raises_nor_dispatches(
        self, setup,
    ):
        """wh-overlay-slow-uia-stale-badges.14.41: an exact dict whose
        stored key raises from __eq__ makes payload.get("action") raise
        inside the lookup's rich comparison. Production's _read_envelope
        contains that under its protective except and drops the
        envelope; the adapter's bare payload.get raised it raw."""
        adapter, recording = setup
        await adapter.send_command({
            EqRaisesStrAction("action"): "press_key_action",
            "params": {"key": "enter", "repeat": 1},
        })
        assert recording.keystrokes == []


class TestSubprocessInterception:
    """Verify subprocess.Popen is intercepted and recorded."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_run_program_records_command(self, setup):
        """run_program() should record the command instead of executing it."""
        from services.wheelhouse.speech.actions import subprocess
        # subprocess.Popen is patched -- calling it records to run_programs
        subprocess.Popen("explorer.exe ms-settings:", shell=True)
        _, recording = setup
        assert "explorer.exe ms-settings:" in recording.run_programs

    @pytest.mark.asyncio
    async def test_run_programs_starts_empty(self, setup):
        _, recording = setup
        assert recording.run_programs == []

    @pytest.mark.asyncio
    async def test_clear_resets_run_programs(self, setup):
        _, recording = setup
        recording.run_programs.append("test.exe")
        recording.clear()
        assert recording.run_programs == []


class TestUnicodeRouting:
    """wh-wxkp: short text routes to VerifiedUnicodeStrategy end-to-end.

    The harness previously forced ui_actions.verified_unicode.max_chars=0
    so every insertion fell through to the clipboard path. With the UIA
    shadow-buffer surface and the Unicode Win32 boundary mocked, the
    production default (50 chars) is active and short text must deliver
    via type_string_verified, not clipboard paste.
    """

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_short_text_routes_to_unicode(self, setup):
        adapter, recording = setup
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "hello"},
        })
        # Perfected against the empty mock shadow buffer: capitalized,
        # no leading space, delivered via SendInput.
        assert recording.unicode_sends == ["Hello"]
        # Assertion-compatibility mirror: the send also lands in
        # clipboard_pastes so pre-Unicode e2e assertions keep passing.
        assert recording.clipboard_pastes == ["Hello"]
        # No paste keystroke fired -- delivery was Unicode SendInput.
        assert ("ctrl", "v") not in recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_unicode_output_matches_clipboard_path_composition(self, setup):
        """Streamed words compose identically to the old clipboard path."""
        adapter, recording = setup
        for word in ["hello", "world"]:
            await adapter.send_command({
                "action": "intelligent_insert_text",
                "params": {"insertion_string": word},
            })
        # Same observable text output the clipboard path produced:
        # first word capitalized, second word space-prefixed lowercase.
        assert recording.clipboard_pastes == ["Hello", " world"]
        assert recording.unicode_sends == ["Hello", " world"]

    @pytest.mark.asyncio
    async def test_long_text_still_routes_to_clipboard(self, setup):
        adapter, recording = setup
        long_text = "this is a deliberately long dictated phrase that exceeds fifty characters"
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": long_text},
        })
        assert recording.unicode_sends == []
        assert len(recording.clipboard_pastes) == 1
        assert ("ctrl", "v") in recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_clear_resets_unicode_sends(self, setup):
        _, recording = setup
        recording.unicode_sends.append("test")
        recording.clear()
        assert recording.unicode_sends == []


class TestVerifiedKeyBoundaryIsolation:
    """wh-review-pattern-fixes.40: no e2e dispatch may reach real SendInput.

    ``ui_action_handler`` binds ``verified_press_keys``,
    ``_send_modifier_keyups`` and ``send_backspaces`` directly from
    ``utils.win_input_sender`` at import time. AppAdapter patched only
    ``press_keys`` and ``type_string``, so ``transform_selection``,
    ``wrap_or_insert``, ``capture_selected_text``, ``replace_selected_text``,
    ``press_key_verified`` and ``retract`` sent real chords to whichever
    window held focus on the developer machine.

    These tests never let a real chord fire, red or green: each one installs
    a stand-in for the real function BEFORE AppAdapter is constructed. The
    stand-in records the escape instead of calling Win32. A test is red when
    the escape list is not empty.
    """

    #: Every ``utils.win_input_sender`` symbol ui_action_handler binds at
    #: module import time and that reaches Win32 SendInput.
    BOUNDARY_SYMBOLS = (
        "verified_press_keys",
        "_send_modifier_keyups",
        "send_backspaces",
    )

    @staticmethod
    def _install_tripwires(escaped):
        """Replace the real SendInput bindings with recording stand-ins.

        Returns (module, tripwires). The caller must restore the saved
        originals. AppAdapter's own ``mock.patch`` calls layer on top and
        restore the tripwire on ``stop_patches()``.
        """
        import services.wheelhouse.ui.ui_action_handler as uah

        def verified(*keys):
            escaped.append(("verified_press_keys", keys))
            return True, len(keys) * 2, len(keys) * 2

        def keyups(keys):
            escaped.append(("_send_modifier_keyups", tuple(keys)))

        def backspaces(count):
            escaped.append(("send_backspaces", count))
            return True

        tripwires = {
            "verified_press_keys": verified,
            "_send_modifier_keyups": keyups,
            "send_backspaces": backspaces,
        }
        saved = {name: getattr(uah, name) for name in tripwires}
        for name, stub in tripwires.items():
            setattr(uah, name, stub)
        return uah, tripwires, saved

    @pytest.fixture
    def tripwired(self):
        """AppAdapter built while stand-ins occupy the real bindings."""
        escaped = []
        uah, tripwires, saved = self._install_tripwires(escaped)
        recording = Recording()
        adapter = AppAdapter(recording)
        try:
            yield adapter, recording, escaped, uah, tripwires
        finally:
            adapter.stop_patches()
            for name, original in saved.items():
                setattr(uah, name, original)

    @pytest.mark.asyncio
    async def test_transform_selection_copy_is_recorded_not_sent(self, tripwired):
        """transform_selection's Ctrl+C must land in Recording, not in Win32."""
        adapter, recording, escaped, _uah, _tripwires = tripwired
        await adapter.send_command({
            "action": "transform_selection",
            "params": {"transformation_type": "snake_case"},
        })
        assert escaped == [], (
            "transform_selection reached the real SendInput boundary: "
            f"{escaped}"
        )
        assert ("ctrl", "c") in recording.get_keystroke_keys()

    @pytest.mark.asyncio
    async def test_adapter_rebinds_every_sendinput_symbol(self, tripwired):
        """Each SendInput binding is replaced while the patches are live."""
        _adapter, _recording, _escaped, uah, tripwires = tripwired
        still_real = [
            name for name in self.BOUNDARY_SYMBOLS
            if getattr(uah, name) is tripwires[name]
        ]
        assert still_real == [], (
            "AppAdapter left these ui_action_handler bindings pointing at "
            f"the real win_input_sender function: {still_real}"
        )

    @pytest.mark.asyncio
    async def test_recorded_chord_reports_full_delivery(self, tripwired):
        """The recorder returns the triple production code unpacks.

        ``verified_press_keys`` returns ``(ok, accepted, expected)`` and every
        caller unpacks it, then fails closed when ``ok`` is false. A recorder
        with the wrong return shape would make every verified path report a
        short delivery and abandon the action.
        """
        _adapter, recording, _escaped, uah, _tripwires = tripwired
        before = len(recording.keystrokes)
        ok, accepted, expected = uah.verified_press_keys("ctrl", "shift", "x")
        assert len(recording.keystrokes) == before + 1
        assert recording.get_keystroke_keys()[-1] == ("ctrl", "shift", "x")
        assert ok is True
        assert accepted == expected

    @pytest.mark.asyncio
    async def test_retract_backspaces_are_recorded_not_sent(self, tripwired):
        """retract() sends N backspaces through the same SendInput path."""
        _adapter, recording, escaped, uah, _tripwires = tripwired
        delivered = uah.send_backspaces(3)
        assert escaped == [], f"send_backspaces reached Win32: {escaped}"
        assert delivered is True
        assert recording.backspace_sends == [3]


class TestMouseAndNotificationBoundaryIsolation:
    """wh-review-pattern-fixes.42: no e2e dispatch may move the real mouse.

    Six ``ui_action_handler`` wrappers import their provider from
    ``utils.win_input_sender`` INSIDE the function body, so a patch on the
    ``ui_action_handler`` module cannot intercept them:
    ``_win32_coordinate_click`` and ``_win32_gesture_click`` import
    ``click_at``, ``_win32_click_point`` imports ``click_point``,
    ``_win32_move_pointer`` imports ``move_pointer_to``,
    ``_win32_drag_pointer`` imports ``drag_pointer``, and
    ``_win32_root_window_at_point`` imports ``root_window_at_point``.
    ``UIActionHandler.show_notification`` has the same shape with
    ``plyer.notification``.

    The handler stores the three pointer wrappers in ``_mouse_click_seam``,
    ``_mouse_move_seam`` and ``_mouse_drag_seam``, and the ClickExecutor
    holds the coordinate, gesture and hit-test wrappers. AppAdapter's empty
    config reaches ``ClickConfig``'s ``enabled=True`` default, so none of
    these dispatches short-circuits.

    These tests never let a real click, move, drag or toast happen, red or
    green: each one installs a stand-in for the real provider BEFORE
    AppAdapter is constructed. The stand-in records the escape instead of
    calling Win32. A test is red when the escape list is not empty, which
    is what happens when a patch entry is missing or aimed at the wrong
    module.
    """

    #: Every ``utils.win_input_sender`` symbol the mouse and hit-test
    #: wrappers import at call time.
    PROVIDER_SYMBOLS = (
        "click_at",
        "click_point",
        "move_pointer_to",
        "drag_pointer",
        "root_window_at_point",
    )

    #: The window handle the hit-test stand-in reports. It differs from
    #: AppAdapter.FOREGROUND_HWND so a test can tell the tripwire's answer
    #: apart from the recorder's answer.
    TRIPWIRE_HWND = 9901

    @staticmethod
    def _install_tripwires(escaped):
        """Replace the real mouse providers with recording stand-ins.

        Returns (module, tripwires, saved). The caller must restore the
        saved originals. AppAdapter's own ``mock.patch`` calls layer on top
        and restore the tripwire on ``stop_patches()``.
        """
        import utils.win_input_sender as wis

        cls = TestMouseAndNotificationBoundaryIsolation

        def click_at(x, y, button="left", click_count=1):
            escaped.append(("click_at", (x, y, button, click_count)))
            return True, 2 * int(click_count), None

        def click_point(x, y, button="left", click_count=1):
            escaped.append(("click_point", (x, y, button, click_count)))
            return True, None

        def move_pointer_to(x, y):
            escaped.append(("move_pointer_to", (x, y)))
            return True, None

        def drag_pointer(start_x, start_y, end_x, end_y, duration_ms=250):
            escaped.append((
                "drag_pointer", (start_x, start_y, end_x, end_y, duration_ms),
            ))
            return True, None

        def root_window_at_point(x, y):
            escaped.append(("root_window_at_point", (x, y)))
            return cls.TRIPWIRE_HWND

        tripwires = {
            "click_at": click_at,
            "click_point": click_point,
            "move_pointer_to": move_pointer_to,
            "drag_pointer": drag_pointer,
            "root_window_at_point": root_window_at_point,
        }
        saved = {name: getattr(wis, name) for name in tripwires}
        for name, stub in tripwires.items():
            setattr(wis, name, stub)
        return wis, tripwires, saved

    @staticmethod
    def _install_notify_tripwire(escaped):
        """Replace plyer's notify with a recording stand-in.

        ``plyer.notification`` is a lazy proxy: reading any attribute
        imports the Windows implementation and every get and set is
        forwarded to that instance. Returns (proxy, stub, saved).
        """
        from plyer import notification

        def notify(**kwargs):
            escaped.append(("notify", dict(kwargs)))

        saved = notification.notify
        notification.notify = notify
        return notification, notify, saved

    @pytest.fixture
    def tripwired(self):
        """AppAdapter built while stand-ins occupy the real providers."""
        escaped = []
        wis, tripwires, saved = self._install_tripwires(escaped)
        proxy, notify_stub, saved_notify = self._install_notify_tripwire(escaped)
        recording = Recording()
        adapter = AppAdapter(recording)
        try:
            yield adapter, recording, escaped, wis, tripwires, notify_stub
        finally:
            adapter.stop_patches()
            for name, original in saved.items():
                setattr(wis, name, original)
            proxy.notify = saved_notify

    @pytest.mark.asyncio
    async def test_click_point_is_recorded_not_clicked(self, tripwired):
        """click_point must land in Recording, not on the real mouse."""
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        await adapter.send_command({
            "action": "click_point",
            "params": {"x": 120, "y": 340, "button": "right", "click_count": 2},
        })
        assert escaped == [], (
            f"click_point reached the real mouse provider: {escaped}"
        )
        assert recording.mouse_clicks == [(120, 340, "right", 2)]

    @pytest.mark.asyncio
    async def test_move_pointer_is_recorded_not_moved(self, tripwired):
        """move_pointer must land in Recording, not on the real cursor."""
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        await adapter.send_command({
            "action": "move_pointer", "params": {"x": 7, "y": 9},
        })
        assert escaped == [], (
            f"move_pointer reached the real mouse provider: {escaped}"
        )
        assert recording.pointer_moves == [(7, 9)]

    @pytest.mark.asyncio
    async def test_perform_drag_is_recorded_not_dragged(self, tripwired):
        """perform_drag must land in Recording, not on the real mouse."""
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        await adapter.send_command({
            "action": "perform_drag",
            "params": {
                "start_x": 1, "start_y": 2, "end_x": 30, "end_y": 40,
                "duration_ms": 120,
            },
        })
        assert escaped == [], (
            f"perform_drag reached the real mouse provider: {escaped}"
        )
        assert recording.pointer_drags == [(1, 2, 30, 40, 120)]

    @pytest.mark.asyncio
    async def test_coordinate_click_seam_is_recorded_not_clicked(self, tripwired):
        """The ClickExecutor's default-gesture seam must not reach Win32.

        The seam is the wrapper the executor holds, so calling it exercises
        the same late import the by-name click path takes.
        """
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        executor = adapter.handler._get_click_executor()
        succeeded, events_sent, reason = executor._coordinate_click_fn(50, 60)
        assert escaped == [], (
            f"the coordinate click seam reached the real provider: {escaped}"
        )
        assert recording.coordinate_clicks == [(50, 60, "left", 1)]
        assert succeeded is True
        assert events_sent == 2
        assert reason is None

    @pytest.mark.asyncio
    async def test_gesture_click_seam_is_recorded_not_clicked(self, tripwired):
        """The ClickExecutor's gesture seam must not reach Win32."""
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        executor = adapter.handler._get_click_executor()
        succeeded, events_sent, reason = executor._gesture_click_fn(
            11, 22, "right", 2,
        )
        assert escaped == [], (
            f"the gesture click seam reached the real provider: {escaped}"
        )
        assert recording.coordinate_clicks == [(11, 22, "right", 2)]
        assert succeeded is True
        assert events_sent == 4
        assert reason is None

    @pytest.mark.asyncio
    async def test_hit_test_seam_reads_the_fake_desktop(self, tripwired):
        """The hit-test seam must answer from Recording, not the desktop.

        This seam reads the host rather than sending input, but the
        executor refuses a coordinate click whose point resolves to a
        window other than the target. The recorder answers with the one
        window the end-to-end tests target, so the refusal cannot depend on
        what sat under the point on the developer's screen.
        """
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        executor = adapter.handler._get_click_executor()
        hwnd = executor._window_at_point_fn(3, 4)
        assert escaped == [], (
            f"the hit-test seam reached the real provider: {escaped}"
        )
        assert recording.hit_tests == [(3, 4)]
        assert hwnd == AppAdapter.FOREGROUND_HWND

    @pytest.mark.asyncio
    async def test_show_notification_is_recorded_not_raised(self, tripwired):
        """show_notification must not put a toast on the real screen."""
        adapter, recording, escaped, _wis, _tw, _notify = tripwired
        await adapter.send_command({
            "action": "show_notification",
            "params": {"title": "Wheelhouse", "message": "hello"},
        })
        assert escaped == [], f"show_notification reached plyer: {escaped}"
        assert len(recording.notifications) == 1
        assert recording.notifications[0]["title"] == "Wheelhouse"
        assert recording.notifications[0]["message"] == "hello"

    @pytest.mark.asyncio
    async def test_adapter_rebinds_every_mouse_provider_symbol(self, tripwired):
        """Each provider binding is replaced while the patches are live.

        This is the test that fails when a patch entry names the wrong
        module. A patch aimed at ``ui_action_handler`` leaves the
        ``utils.win_input_sender`` attribute pointing at the tripwire.
        """
        _adapter, _rec, _escaped, wis, tripwires, _notify = tripwired
        unpatched = [
            name for name in self.PROVIDER_SYMBOLS
            if getattr(wis, name) is tripwires[name]
        ]
        assert unpatched == [], (
            "AppAdapter left these utils.win_input_sender functions reachable "
            f"from the late imports in ui_action_handler: {unpatched}"
        )

    @pytest.mark.asyncio
    async def test_adapter_rebinds_plyer_notify(self, tripwired):
        """The plyer notify binding is replaced while the patches are live."""
        from plyer import notification
        _adapter, _rec, _escaped, _wis, _tw, notify_stub = tripwired
        assert notification.notify is not notify_stub, (
            "AppAdapter left plyer.notification.notify reachable from "
            "show_notification's late import"
        )


class TestTextDeliveryLedger:
    """wh-review-pattern-fixes.46: a clipboard write is not a delivery.

    ``AppAdapter._mock_pyperclip_copy`` appends every non-sentinel
    clipboard write to ``Recording.clipboard_pastes``. That append happens
    at the WRITE, before ``verified_paste`` reaches its focus proof and
    long before any Ctrl+V. A run in which every focus proof refuses still
    fills that list, so a test that reads it cannot tell a delivered word
    from a word that reached no window.

    ``Recording.text_deliveries`` is the separate delivery record. The
    harness appends to it from ``ClipboardOperations.credit_paste_chars``,
    which production calls at exactly two points: the end of
    ``verified_paste`` after the post-paste foreground check passed, and
    the end of ``VerifiedUnicodeStrategy.insert`` after the post-send
    foreground check passed. Both points sit after the delivery succeeded.
    """

    #: A foreground window handle that is NOT the one the mocked focused
    #: control resolves to (``AppAdapter.FOREGROUND_HWND``). With this
    #: value every focus proof in ``verified_paste``,
    #: ``VerifiedUnicodeStrategy`` and ``WindowFocusManager`` refuses.
    WRONG_HWND = 2

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.fixture
    def wrong_foreground(self):
        """An adapter whose focus stand-in names the wrong window."""
        recording = Recording()
        adapter = AppAdapter(recording, foreground_hwnd=self.WRONG_HWND)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_unicode_send_records_a_delivery(self, setup):
        """A successful Unicode send lands in the delivery record."""
        adapter, recording = setup
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "hello"},
        })
        assert recording.unicode_sends == ["Hello"]
        assert recording.text_deliveries == ["Hello"]

    @pytest.mark.asyncio
    async def test_accepted_paste_records_a_delivery(self, setup):
        """A fully accepted clipboard paste lands in the delivery record."""
        adapter, recording = setup
        long_text = (
            "this is a deliberately long dictated phrase that exceeds fifty "
            "characters"
        )
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": long_text},
        })
        assert recording.unicode_sends == []
        assert ("ctrl", "v") in recording.get_keystroke_keys()
        assert len(recording.text_deliveries) == 1
        # TextPerfector capitalizes the first word of the document.
        assert recording.text_deliveries[0].lower() == long_text

    @pytest.mark.asyncio
    async def test_wrong_foreground_writes_clipboard_but_delivers_nothing(
        self, wrong_foreground,
    ):
        """The defect itself: clipboard writes without a delivery.

        With the focus stand-in pointed at a window that is not the
        target, every send path refuses. The clipboard-write list still
        fills, which is why it cannot serve as the delivery oracle.
        """
        adapter, recording = wrong_foreground
        await adapter.send_command({
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "hello"},
        })
        assert recording.clipboard_pastes != [], (
            "the clipboard write is the diagnostic this test depends on"
        )
        assert recording.unicode_sends == []
        assert ("ctrl", "v") not in recording.get_keystroke_keys()
        assert recording.text_deliveries == [], (
            "nothing reached the target window, so the delivery record "
            f"must stay empty: {recording.text_deliveries}"
        )

    @pytest.mark.asyncio
    async def test_clear_resets_text_deliveries(self, setup):
        _, recording = setup
        recording.text_deliveries.append("test")
        recording.clear()
        assert recording.text_deliveries == []


class TestVoiceClickBoundaryIsolation:
    """wh-review-pattern-fixes.47: no e2e click may touch the real desktop.

    ``UIActionHandler._get_click_executor`` builds the ClickExecutor from
    production seams: ``_win32_foreground_probe`` and ``_win32_on_screen``
    from its own module, and ``_default_is_window_visible``,
    ``_default_owner_of`` and ``_default_class_name_of`` from
    ``ui.uia_walker`` -- the import path is ``from ui import uia_walker``
    inside that method, NOT
    ``services.wheelhouse.ui.uia_walker``. The point-hits-winner seam is a
    bound method that resolves the memoised IUIAutomation root at call
    time and hands it to the module-level ``_uia_point_hits_winner``.
    ``_get_click_element_finder`` calls ``ui.uia_walker.create_automation``
    to build that root, and both click handlers call
    ``_capture_click_foreground`` before they walk.

    Left alone, a full ``click_element`` or ``click_snapshot_item``
    dispatch therefore builds a real IUIAutomation tree, walks the
    developer's focused window, samples the developer's foreground and
    monitor state, and presses a real control.

    These tests never let any of that happen, red or green: each one
    installs a stand-in for the real boundary function BEFORE AppAdapter
    is constructed. The stand-in records the escape instead of calling the
    host. A test is red when the escape list is not empty, which is what
    happens when an injection is missing or a patch names the wrong module
    path.
    """

    #: Every ``ui_action_handler`` module-level symbol the click paths read
    #: at dispatch time and that reaches the host.
    HANDLER_BOUNDARY_SYMBOLS = (
        "_capture_click_foreground",
        "_win32_foreground_probe",
        "_win32_on_screen",
        "_uia_point_hits_winner",
    )

    #: Every ``ui.uia_walker`` symbol the click paths read at dispatch
    #: time and that reaches the host. The import path matters: the
    #: handler reaches this module as ``ui.uia_walker``.
    WALKER_BOUNDARY_SYMBOLS = (
        "create_automation",
        "point_hits_winner",
        "_default_is_window_visible",
        "_default_owner_of",
        "_default_class_name_of",
    )

    @staticmethod
    def _install_tripwires(escaped):
        """Replace the real click boundaries with recording stand-ins.

        Returns (uah, uia_walker, tripwires, saved). The caller must
        restore the saved originals. AppAdapter's own patches and
        injections layer on top and are undone by ``stop_patches()``.
        """
        import services.wheelhouse.ui.ui_action_handler as uah
        from ui import uia_walker
        from services.wheelhouse.tests.e2e.os_mocks import FakeAutomationRoot

        def capture_click_foreground():
            from ui.element_finder import ForegroundContext

            escaped.append(("_capture_click_foreground", ()))
            return ForegroundContext(
                foreground_window=1, foreground_pid=1234,
                foreground_process_name="notepad.exe",
                foreground_window_creation_time=1,
                cursor_at_walk=(0, 0), cursor_monitor_id=0,
            )

        def win32_foreground_probe():
            from ui.click_executor import ForegroundProbe

            escaped.append(("_win32_foreground_probe", ()))
            return ForegroundProbe(
                window=1, pid=1234, process_name="notepad.exe",
                window_creation_time=1,
            )

        def win32_on_screen(x, y):
            escaped.append(("_win32_on_screen", (x, y)))
            return True

        def uia_point_hits_winner(automation, winner, x, y):
            escaped.append(("_uia_point_hits_winner", (x, y)))
            return True

        def create_automation():
            escaped.append(("create_automation", ()))
            return FakeAutomationRoot()

        def point_hits_winner(automation, control_ref, x, y):
            escaped.append(("point_hits_winner", (x, y)))
            return True

        def is_window_visible(hwnd):
            escaped.append(("_default_is_window_visible", (hwnd,)))
            return True

        def owner_of(hwnd):
            escaped.append(("_default_owner_of", (hwnd,)))
            return 1

        def class_name_of(hwnd):
            escaped.append(("_default_class_name_of", (hwnd,)))
            return "Shell_TrayWnd"

        handler_tripwires = {
            "_capture_click_foreground": capture_click_foreground,
            "_win32_foreground_probe": win32_foreground_probe,
            "_win32_on_screen": win32_on_screen,
            "_uia_point_hits_winner": uia_point_hits_winner,
        }
        walker_tripwires = {
            "create_automation": create_automation,
            "point_hits_winner": point_hits_winner,
            "_default_is_window_visible": is_window_visible,
            "_default_owner_of": owner_of,
            "_default_class_name_of": class_name_of,
        }
        saved = {
            (uah, name): getattr(uah, name) for name in handler_tripwires
        }
        saved.update({
            (uia_walker, name): getattr(uia_walker, name)
            for name in walker_tripwires
        })
        for name, stub in handler_tripwires.items():
            setattr(uah, name, stub)
        for name, stub in walker_tripwires.items():
            setattr(uia_walker, name, stub)
        return uah, uia_walker, handler_tripwires, walker_tripwires, saved

    @pytest.fixture
    def tripwired(self):
        """AppAdapter built while stand-ins occupy the real boundaries."""
        escaped = []
        (
            uah, walker, handler_tripwires, walker_tripwires, saved,
        ) = self._install_tripwires(escaped)
        recording = Recording()
        adapter = AppAdapter(recording)
        try:
            yield (
                adapter, recording, escaped, uah, walker,
                handler_tripwires, walker_tripwires,
            )
        finally:
            adapter.stop_patches()
            for (module, name), original in saved.items():
                setattr(module, name, original)

    @staticmethod
    def _take_response(adapter):
        """Read the one Schema A response the click handler emitted."""
        return adapter._response_queue.get(timeout=5)

    @staticmethod
    def _query(name):
        from ui.element_types import ElementQuery

        return ElementQuery(
            name=name, role=None, ordinal=None, spatial=None,
            raw_utterance=f"click {name}",
        )

    @pytest.mark.asyncio
    async def test_click_element_dispatch_stays_off_every_boundary(
        self, tripwired,
    ):
        """A full by-name click runs end to end without a host call."""
        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        await adapter.send_command({
            "action": "click_element",
            "params": {"query": self._query("Save"), "trace_id": "t1",
                       "request_id": "r1"},
        })
        assert escaped == [], (
            f"the by-name click dispatch reached the host: {escaped}"
        )
        response = self._take_response(adapter)
        assert response["outcome"] == "ok", (
            f"the by-name click did not complete: {response}"
        )
        assert response["matched_name"] == "Save"
        # The press went through the recording provider, not a real UIA
        # Invoke on a control on the developer's screen.
        assert recording.element_invokes == ["Save"]
        # The walk ran against the injected finder.
        assert recording.element_walks == [("Save", None)]
        # The verification block read the fake foreground twice: once at
        # walk time, once at click time.
        assert recording.click_foreground_captures == [
            recording.click_foreground_window,
        ]
        assert recording.click_foreground_probes == [
            recording.click_foreground_window,
        ]
        assert recording.click_on_screen_tests == [(120, 220)]

    @pytest.mark.asyncio
    async def test_click_snapshot_item_dispatch_stays_off_every_boundary(
        self, tripwired,
    ):
        """A full numbered-badge click runs end to end without a host call.

        The badge this test picks was walked from an owned popup, so the
        dispatch also runs the popup-visibility and popup-owner seams.
        """
        from services.wheelhouse.tests.e2e.os_mocks import (
            FAKE_POPUP_HWND, FAKE_POPUP_ITEM_ID, FAKE_SNAPSHOT_ID,
        )

        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        await adapter.send_command({
            "action": "click_snapshot_item",
            "params": {"snapshot_id": FAKE_SNAPSHOT_ID,
                       "item_id": FAKE_POPUP_ITEM_ID,
                       "trace_id": "t2", "request_id": "r2"},
        })
        assert escaped == [], (
            f"the numbered-badge click dispatch reached the host: {escaped}"
        )
        response = self._take_response(adapter)
        assert response["outcome"] == "ok", (
            f"the numbered-badge click did not complete: {response}"
        )
        assert response["matched_name"] == "Open Recent"
        assert recording.element_invokes == ["Open Recent"]
        # The popup-owned winner drove both popup seams.
        assert recording.click_popup_visible_tests == [FAKE_POPUP_HWND]
        assert recording.click_popup_owner_tests == [FAKE_POPUP_HWND]

    @pytest.mark.asyncio
    async def test_foreground_probe_seam_reads_the_fake_desktop(
        self, tripwired,
    ):
        """The executor's pre-click probe must not read the real foreground."""
        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        executor = adapter.handler._get_click_executor()
        probe = executor._foreground_probe()
        assert escaped == [], (
            f"the foreground probe seam reached the host: {escaped}"
        )
        assert probe.window == recording.click_foreground_window
        assert probe.pid == recording.click_foreground_pid
        assert probe.process_name == recording.click_foreground_process
        assert recording.click_foreground_probes == [probe.window]

    @pytest.mark.asyncio
    async def test_on_screen_seam_answers_from_the_recording(self, tripwired):
        """The executor's on-screen check must not read the real monitors."""
        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        executor = adapter.handler._get_click_executor()
        assert executor._on_screen_fn(11, 22) is True
        assert escaped == [], (
            f"the on-screen seam reached the host: {escaped}"
        )
        assert recording.click_on_screen_tests == [(11, 22)]

    @pytest.mark.asyncio
    async def test_popup_seams_answer_from_the_recording(self, tripwired):
        """The popup liveness seams must not probe real windows."""
        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        executor = adapter.handler._get_click_executor()
        assert executor._popup_visible_fn(4242) is True
        assert executor._popup_owner_fn(4242) == (
            recording.click_foreground_window
        )
        assert escaped == [], (
            f"a popup seam reached the host: {escaped}"
        )
        assert recording.click_popup_visible_tests == [4242]
        assert recording.click_popup_owner_tests == [4242]

    @pytest.mark.asyncio
    async def test_shell_class_seam_answers_from_the_recording(
        self, tripwired,
    ):
        """The shell-class re-read must not read a real window class."""
        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        executor = adapter.handler._get_click_executor()
        assert executor._shell_class_fn(4242) == recording.shell_class_name
        assert escaped == [], (
            f"the shell-class seam reached the host: {escaped}"
        )
        assert recording.click_shell_class_tests == [4242]

    @pytest.mark.asyncio
    async def test_point_hits_winner_seam_answers_from_the_recording(
        self, tripwired,
    ):
        """The UIA obstruction check must not walk a real tree.

        The seam is the handler's bound method, so calling it also proves
        the memoised automation root is the injected one: the real method
        raises when no usable root is stored.
        """
        from services.wheelhouse.tests.e2e.os_mocks import (
            FakeAutomationRoot, make_fake_click_snapshot,
        )

        adapter, recording, escaped, _uah, _w, _ht, _wt = tripwired
        executor = adapter.handler._get_click_executor()
        # Built locally, not read off the adapter, so this test is red on
        # the escape assertion rather than on a missing injection.
        winner = make_fake_click_snapshot(recording).matches[0]
        assert executor._point_hits_winner_fn(winner, 33, 44) is True
        assert escaped == [], (
            f"the point-hits-winner seam reached the host: {escaped}"
        )
        assert recording.click_point_winner_tests == [(33, 44)]
        assert isinstance(
            adapter.handler._click_automation_root, FakeAutomationRoot,
        )

    @pytest.mark.asyncio
    async def test_invoke_providers_are_recording_no_ops(self, tripwired):
        """Both press providers must be recorders, not the real UIA presses.

        ``ClickExecutor`` binds them as constructor DEFAULT arguments, so
        the binding is made once when the class body runs and no
        module-level patch can replace it. The adapter must therefore
        replace the attributes on the built executor.
        """
        from ui import uia_walker

        adapter, recording, _escaped, _uah, _w, _ht, _wt = tripwired
        executor = adapter.handler._get_click_executor()
        assert executor._invoke_fn is not uia_walker.invoke_via_invoke_pattern, (
            "the executor still presses controls through the real UIA "
            "Invoke pattern"
        )
        assert executor._do_default_action_fn is not (
            uia_walker.do_default_action_via_legacy_pattern
        ), (
            "the executor still presses controls through the real MSAA "
            "default action"
        )
        from services.wheelhouse.tests.e2e.os_mocks import FakeControlRef

        control_ref = FakeControlRef("Save")
        executor._invoke_fn(control_ref)
        executor._do_default_action_fn(control_ref)
        assert recording.element_invokes == ["Save"]
        assert recording.element_default_actions == ["Save"]

    @pytest.mark.asyncio
    async def test_adapter_rebinds_every_click_boundary_symbol(
        self, tripwired,
    ):
        """Each boundary binding is replaced while the patches are live.

        This is the test that fails when a patch names a wrong-but-
        plausible module path -- ``services.wheelhouse.ui.uia_walker``
        instead of the ``ui.uia_walker`` the handler imports, for example.
        The wrongly-named patch leaves the real module attribute pointing
        at the tripwire.
        """
        (
            _adapter, _rec, _escaped, uah, walker, handler_tripwires,
            walker_tripwires,
        ) = tripwired
        unpatched = [
            f"ui_action_handler.{name}"
            for name in self.HANDLER_BOUNDARY_SYMBOLS
            if getattr(uah, name) is handler_tripwires[name]
        ]
        unpatched += [
            f"uia_walker.{name}"
            for name in self.WALKER_BOUNDARY_SYMBOLS
            if getattr(walker, name) is walker_tripwires[name]
        ]
        assert unpatched == [], (
            "AppAdapter left these click boundaries reachable from a click "
            f"dispatch: {unpatched}"
        )

    @pytest.mark.asyncio
    async def test_click_dispatch_never_builds_a_real_automation_root(
        self, tripwired,
    ):
        """The COM root comes from the injection, never from create_automation."""
        from services.wheelhouse.tests.e2e.os_mocks import FakeAutomationRoot

        adapter, _rec, escaped, _uah, _w, _ht, _wt = tripwired
        await adapter.send_command({
            "action": "click_element",
            "params": {"query": self._query("Save"), "trace_id": "t3",
                       "request_id": "r3"},
        })
        created = [entry for entry in escaped if entry[0] == "create_automation"]
        assert created == [], (
            "the click dispatch built a real IUIAutomation root"
        )
        # The adapter also patches create_automation, so the tripwire
        # cannot see a call that the patch absorbed. The recorder can.
        assert _rec.automation_roots_created == [], (
            "the click dispatch called create_automation instead of using "
            "the injected root"
        )
        assert isinstance(
            adapter.handler._click_automation_root, FakeAutomationRoot,
        )


class TestTypeTextRecording:
    """Verify type_text actions dispatch through handler and record via type_string mock."""

    @pytest.fixture
    def setup(self):
        recording = Recording()
        adapter = AppAdapter(recording)
        yield adapter, recording
        adapter.stop_patches()

    @pytest.mark.asyncio
    async def test_type_text_recorded(self, setup):
        adapter, recording = setup
        await adapter.send_command({"action": "type_text", "params": {"text": "hello world"}})
        assert recording.typed_texts == ["hello world"]

    @pytest.mark.asyncio
    async def test_type_text_uses_type_string_not_clipboard(self, setup):
        """type_text should use raw keystrokes (type_string), not clipboard paste."""
        adapter, recording = setup
        await adapter.send_command({"action": "type_text", "params": {"text": "test"}})
        assert recording.typed_texts == ["test"]
        assert len(recording.keystrokes) == 0
        assert len(recording.clipboard_pastes) == 0

    @pytest.mark.asyncio
    async def test_typed_texts_starts_empty(self, setup):
        _, recording = setup
        assert recording.typed_texts == []

    @pytest.mark.asyncio
    async def test_clear_resets_typed_texts(self, setup):
        _, recording = setup
        recording.typed_texts.append("test")
        recording.clear()
        assert recording.typed_texts == []
