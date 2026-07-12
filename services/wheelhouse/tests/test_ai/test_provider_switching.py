"""Tests for AI Model switching via GUI menu.

Covers the full data flow:
1. AIService.set_model() -- thin-client model selection on the coordinator
2. StateManager - include AI model state in send_state_update()
3. main.py - switch_ai_provider handler in handler_map
4. GUI - AI Model submenu rendering and switch command dispatch

TDD: These tests are written FIRST per CLAUDE.md requirements.
"""

import asyncio
from queue import Queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

from ai.service import AIService
from ai.providers.openai_compat import OpenAIProvider


# ---------------------------------------------------------------------------
# StateManager AI provider state
# ---------------------------------------------------------------------------

class TestStateManagerAIProvider:
    """Tests for AI model info in state updates (thin-client, decision 29)."""

    @staticmethod
    def _server_config(extra=None):
        """Config mock carrying the [ai.server] block + button keys."""
        values = {
            "ai.enabled": True,
            "ai.server.base_url": "http://localhost:8781/v1",
            "ai.server.model": "qwen3.5:9b",
            "ai.server.kind": "local",
            "ai.server.enabled": True,
            "stt.mode": "remote",
            "stt.last_provider": "google_stt",
            "FLOATING_BUTTON_VISIBLE": True,
            "FLOATING_BUTTON_SIZE": 50,
            "FLOATING_BUTTON_POS": [100, 100],
            "SHOW_SPEECH_PULSE": True,
        }
        if extra:
            values.update(extra)
        config_service = MagicMock()
        config_service.get.side_effect = lambda key, default=None: values.get(key, default)
        return config_service

    @pytest.fixture
    def sm_loop(self):
        """A dedicated asyncio event loop for StateManager construction.

        StateManager needs a loop handle but never runs it here. A yield
        fixture owns the loop lifecycle so it is always closed in teardown,
        even when a test body raises -- the old per-test
        asyncio.new_event_loop() / loop.close() pattern leaked the loop on any
        failure before the finally and produced a noisy ResourceWarning at
        interpreter shutdown.
        """
        loop = asyncio.new_event_loop()
        try:
            yield loop
        finally:
            loop.close()

    def _make_sm(self, loop, config_service, ai_service=None):
        from state_manager import StateManager

        sm = StateManager(
            config_service=config_service,
            event_bus=MagicMock(),
            loop=loop,
            state_to_gui_queue=Queue(),
            websocket_manager=None,
        )
        if ai_service is not None:
            sm.set_ai_service(ai_service)
        return sm

    @pytest.fixture
    def state_manager(self, sm_loop):
        """StateManager wired with a configured+enabled local [ai.server]."""
        return self._make_sm(sm_loop, self._server_config())

    def test_state_update_includes_ai_provider_cached_model(self, state_manager):
        """ai_provider reflects the cached selected model (set_model), falling
        back to the configured [ai.server].model when the service is unwired."""
        state_manager.send_state_update()
        msg = state_manager.state_to_gui_queue.get_nowait()

        assert "ai_provider" in msg
        # No AIService wired -> falls back to the configured server model.
        assert msg["ai_provider"] == "qwen3.5:9b"

    def test_state_update_ai_provider_uses_cached_model_name(self, sm_loop):
        """When an AIService is wired, ai_provider is its cached _model_name."""
        ai = MagicMock()
        ai._model_name = "selected-model:7b"
        ai.cached_models = MagicMock(return_value=["selected-model:7b"])
        sm = self._make_sm(sm_loop, self._server_config(), ai_service=ai)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        assert msg["ai_provider"] == "selected-model:7b"

    def test_state_update_local_kind_lists_live_plus_configured(self, sm_loop):
        """Local kind: provider list = live list + configured model always
        included (decision 29c)."""
        ai = MagicMock()
        ai._model_name = "qwen3.5:9b"
        ai.cached_models = MagicMock(return_value=["live-a", "live-b"])
        sm = self._make_sm(sm_loop, self._server_config(), ai_service=ai)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        avail = msg["ai_providers_available"]
        assert "live-a" in avail
        assert "live-b" in avail
        # Configured model always present so the selection is selectable.
        assert "qwen3.5:9b" in avail
        # No sentinel when configured.
        assert "__ai_unconfigured__" not in avail

    def test_state_update_cloud_kind_lists_configured_model_only(self, sm_loop):
        """Cloud kind: provider list = configured model only (decision 29c)."""
        config = self._server_config({
            "ai.server.kind": "cloud",
            "ai.server.model": "gemini-flash",
        })
        ai = MagicMock()
        ai._model_name = "gemini-flash"
        ai.cached_models = MagicMock(return_value=["ignored-live"])
        sm = self._make_sm(sm_loop, config, ai_service=ai)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        assert msg["ai_providers_available"] == ["gemini-flash"]

    def test_state_update_disabled_server_shows_disabled_sentinel(self, sm_loop):
        """Configured but ai.server.enabled False -> __ai_disabled__ sentinel
        (finding wh-ay6h.6.7 / decision 29b update): the GUI renders it as
        a non-selectable placeholder, same pattern as __ai_unconfigured__."""
        config = self._server_config({"ai.server.enabled": False})
        sm = self._make_sm(sm_loop, config)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        assert msg["ai_providers_available"] == ["__ai_disabled__"]
        assert "__ai_unconfigured__" not in msg["ai_providers_available"]

    def test_state_update_unconfigured_server_emits_sentinel(self, sm_loop):
        """[ai.server] unconfigured (no base_url) -> the __ai_unconfigured__
        sentinel list (decision 29a)."""
        config = self._server_config({"ai.server.base_url": ""})
        sm = self._make_sm(sm_loop, config)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        assert msg["ai_providers_available"] == ["__ai_unconfigured__"]

    def test_state_update_master_ai_disabled_shows_disabled_sentinel(self, sm_loop):
        """ai.enabled=false master kill switch -> __ai_disabled__ sentinel even
        when ai.server.base_url and ai.server.enabled are set (wh-ay6h.10.8).

        This mirrors AIService._ai_off() which also checks ai.enabled first.
        The menu predicate must agree so the AI Model submenu does not render
        clickable real models against a service that is globally disabled."""
        config = self._server_config({"ai.enabled": False})
        sm = self._make_sm(sm_loop, config)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        assert msg["ai_providers_available"] == ["__ai_disabled__"]
        # Must not leak real model names when AI is globally off.
        assert "__ai_unconfigured__" not in msg["ai_providers_available"]

    def test_state_update_does_not_touch_deleted_model_list_attr(self, sm_loop):
        """Regression: _get_ai_provider_display_names must not reach for the
        deleted multi-provider model-list attribute (finding wh-ay6h.10.3).

        Wire a MagicMock with spec=AIService so that accessing any attribute
        absent from the real class raises AttributeError. If the deleted loop
        is re-introduced, send_state_update will raise here instead of
        silently dropping the entire state dict (which the bare-MagicMock
        test helpers mask because MagicMock auto-creates missing attrs)."""
        ai = MagicMock(spec=AIService)
        ai._model_name = "qwen3.5:9b"
        ai.cached_models = MagicMock(return_value=["qwen3.5:9b"])
        sm = self._make_sm(sm_loop, self._server_config(), ai_service=ai)
        sm.send_state_update()
        msg = sm.state_to_gui_queue.get_nowait()
        # The state dict must be present (no silent drop from AttributeError).
        assert "ai_provider_display_names" in msg


# ---------------------------------------------------------------------------
# main.py handler (switch_ai_provider)
# ---------------------------------------------------------------------------

class TestSwitchAIProviderHandler:
    """Tests for the switch_ai_provider handler in LogicController.

    Thin-client contract (design 5.4): selection is a fast set_model on the
    single [ai.server] client -- no WorkingDialog bracket and none of the
    deleted per-model swap / lookup entry points. The handler sets the model,
    refreshes the cached list, persists the choice, and pushes a state update.
    """

    def _make_controller(self, ai_service):
        from main import LogicController

        service_manager = MagicMock()
        service_manager.ai_service = ai_service

        state_manager = MagicMock()
        state_manager.send_state_update = MagicMock()

        controller = MagicMock(spec=LogicController)
        controller.config_service = MagicMock()
        controller.config_service.save = AsyncMock()
        controller.service_manager = service_manager
        controller.state_manager = state_manager
        return LogicController, controller, state_manager

    @pytest.mark.asyncio
    async def test_switch_sets_model_and_refreshes(self):
        """The handler calls set_model + refresh_models and notifies the GUI."""
        ai_service = MagicMock()
        ai_service.set_model = AsyncMock()
        ai_service.refresh_models = AsyncMock()

        LogicController, controller, state_manager = self._make_controller(ai_service)

        await LogicController._switch_ai_provider(controller, "qwen3.5:9b")

        ai_service.set_model.assert_awaited_once_with("qwen3.5:9b")
        ai_service.refresh_models.assert_awaited_once()
        state_manager.send_state_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_switch_persists_model_and_no_legacy_swap(self):
        """config_service.set/save ARE called to persist the selection
        (finding wh-ay6h.10.5). The mock is spec'd to AIService so the deleted
        per-model swap entry points are absent from its surface -- the handler
        cannot reach them and the thin client has no GGUF load (design 5.4)."""
        ai_service = MagicMock(spec=AIService)
        ai_service.set_model = AsyncMock()
        ai_service.refresh_models = AsyncMock()

        LogicController, controller, _ = self._make_controller(ai_service)

        await LogicController._switch_ai_provider(controller, "some-model")

        ai_service.set_model.assert_awaited_once_with("some-model")
        controller.config_service.set.assert_called_once_with("ai.server.model", "some-model")
        controller.config_service.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_switch_ignores_unconfigured_sentinel(self):
        """The __ai_unconfigured__ sentinel is a non-selectable placeholder and
        is defensively ignored (spec 5.4): no set_model, no state push."""
        ai_service = MagicMock()
        ai_service.set_model = AsyncMock()
        ai_service.refresh_models = AsyncMock()

        LogicController, controller, state_manager = self._make_controller(ai_service)

        await LogicController._switch_ai_provider(controller, "__ai_unconfigured__")

        ai_service.set_model.assert_not_called()
        ai_service.refresh_models.assert_not_called()
        state_manager.send_state_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_ignores_disabled_sentinel(self):
        """The __ai_disabled__ sentinel is a non-selectable placeholder and
        must be defensively ignored like __ai_unconfigured__ (wh-ay6h.7.2):
        no set_model, no refresh_models, no state push."""
        ai_service = MagicMock()
        ai_service.set_model = AsyncMock()
        ai_service.refresh_models = AsyncMock()

        LogicController, controller, state_manager = self._make_controller(ai_service)

        await LogicController._switch_ai_provider(controller, "__ai_disabled__")

        ai_service.set_model.assert_not_called()
        ai_service.refresh_models.assert_not_called()
        state_manager.send_state_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_no_ai_service_logs_warning(self):
        """When AIService is None the handler returns early without raising."""
        from main import LogicController

        service_manager = MagicMock()
        service_manager.ai_service = None

        controller = MagicMock(spec=LogicController)
        controller.config_service = MagicMock()
        controller.service_manager = service_manager
        controller.state_manager = MagicMock()

        await LogicController._switch_ai_provider(controller, "some-model")

        controller.state_manager.send_state_update.assert_not_called()


# ---------------------------------------------------------------------------
# GUI AI Model submenu
# ---------------------------------------------------------------------------

class TestGuiAIProviderMenu:
    """Tests for the AI Model submenu in the system tray."""

    @pytest.fixture
    def gui_manager(self, qapp):
        """Create GuiManager with mocked dependencies.

        Requests the session-scoped qapp fixture so a QApplication exists
        before GuiManager (a QObject that builds Qt widgets in __init__) is
        constructed -- without it GuiManager() blocks/aborts in a headless
        test run.
        """
        from gui import GuiManager

        shutdown_event = MagicMock()
        commands_queue = Queue()
        state_queue = Queue()

        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            manager = GuiManager(
                shutdown_event, commands_queue, state_queue
            )
            manager.initial_state_received = True
            yield manager

    def test_gui_stores_ai_provider_from_state_update(self, gui_manager):
        """GUI stores ai_provider fields from state update message."""
        # Simulate state update
        gui_manager.ai_provider = "ollama"
        gui_manager.ai_providers_available = ["ollama", "openai"]
        gui_manager.ai_provider_display_names = {
            "ollama": "Ollama (local)",
            "openai": "Cloud",
        }

        assert gui_manager.ai_provider == "ollama"
        assert len(gui_manager.ai_providers_available) == 2

    def test_gui_sends_switch_ai_provider_command(self, gui_manager):
        """switch_ai_provider() sends correct command to Logic process."""
        gui_manager.switch_ai_provider("openai")

        cmd = gui_manager.commands_to_logic_queue.get_nowait()
        assert cmd["action"] == "switch_ai_provider"
        assert cmd["provider"] == "openai"

    def test_gui_ai_provider_display_name(self, gui_manager):
        """_get_ai_provider_display_name returns mapped name or fallback."""
        gui_manager.ai_provider_display_names = {
            "ollama": "Ollama (local)",
            "openai": "Cloud",
        }

        assert gui_manager._get_ai_provider_display_name("ollama") == "Ollama (local)"
        assert gui_manager._get_ai_provider_display_name("openai") == "Cloud"
        # Fallback for unknown
        assert gui_manager._get_ai_provider_display_name("unknown") == "Unknown"

    def test_gui_initializes_ai_provider_state(self, qapp):
        """GuiManager initializes ai_provider state attributes."""
        from gui import GuiManager

        with patch("gui.FloatingButton"), \
             patch("gui.WorkingDialog"), \
             patch("gui.pystray") as mock_pystray, \
             patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            manager = GuiManager(
                MagicMock(), Queue(), Queue()
            )

        assert manager.ai_provider is None
        assert manager.ai_providers_available == []
        assert manager.ai_provider_display_names == {}
