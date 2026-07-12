"""Shared test fixtures for wheelhouse service tests.

Provides mock fixtures for core infrastructure components:
- EventBus (real instance or mock)
- ConfigService (mock with in-memory config)
- StateManager (mock with controllable properties)

Also sets up sys.path for both the wheelhouse service directory and the
project root, so that both `from state_manager import ...` and
`from services.wheelhouse.event_bus import ...` style imports resolve.
"""

import sys
from pathlib import Path

# Project root (WheelHouse/)
_project_root = Path(__file__).parent.parent.parent.parent
# Service directory (services/wheelhouse/)
_service_dir = Path(__file__).parent.parent

for _p in (_project_root, _service_dir):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

# Stub any pywin32-family module that fails to resolve, BEFORE any test
# imports main (whose import graph reaches pythoncom/win32*). On the dev
# environment pywin32 is installed and this is a no-op; on environments
# without it (reviewer sandboxes, non-Windows CI) it keeps
# 'from main import LogicController' importable so wiring tests run
# (wh-logic-test-import-isolation). Loaded by file path because tests/
# is not a package.
import importlib.util as _ilu

_stub_spec = _ilu.spec_from_file_location(
    "_pywin32_stub", Path(__file__).parent / "_pywin32_stub.py"
)
assert _stub_spec is not None and _stub_spec.loader is not None
_stub_mod = _ilu.module_from_spec(_stub_spec)
_stub_spec.loader.exec_module(_stub_mod)
_stub_mod.ensure_win32_importable()

import asyncio
from multiprocessing import Queue
from unittest.mock import Mock, AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Hermeticity guard: never load the developer's personal user patterns
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _no_default_user_patterns_file():
    """Force PatternCatalog's default user-file resolution to "" (no file).

    Many tests construct ``PatternCatalog(<system file>)`` with no explicit
    user path. Outside pytest that default resolves to the real per-machine
    ``data/user_patterns.toml``; if it also did so in tests, every such test
    would silently merge in the developer's personal patterns and become
    machine-dependent. Tests that exercise the merge pass an explicit
    ``user_patterns_file`` path, which this guard does not touch.

    Session-scoped on purpose (wh-user-patterns-split.12.1): pytest
    instantiates higher-scoped fixtures first, so a function-scoped guard
    ran AFTER the session-scoped e2e ``pattern_catalog`` fixture and the
    module-scoped property-test ``matcher`` fixture had already built their
    catalogs against the real personal file. A session-scoped autouse
    fixture is instantiated before every non-autouse fixture of any scope,
    which closes that gap; those two fixtures also pass an explicit ""
    themselves as a second line of defense.
    """
    mp = pytest.MonkeyPatch()
    from speech.pattern_catalog import PatternCatalog
    mp.setattr(
        PatternCatalog,
        "_default_user_patterns_file",
        staticmethod(lambda: ""),
    )
    yield
    mp.undo()


# ---------------------------------------------------------------------------
# Qt application fixture (required for tests that create real widgets)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication for GUI widget tests."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def mock_editor_window():
    """Replace TerminalDictationEditorWindow with a Mock while a test runs.

    GuiManager.__init__ builds this real QDialog even when a test patches
    every other Qt piece, so every GuiManager-constructing unit test used to
    leave a real native dialog behind. Incidental native widgets are
    access-violation surface in full-suite runs (wh-pytest-flaky-segfault):
    the observed crashes died inside a QDialog constructor late in the
    suite, after the COM-heavy test_ui package had run. GuiManager test
    files opt in via
    ``pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")``.

    Tests that exercise the real editor window construct it directly and
    must NOT use this fixture."""
    with patch("terminal_editor_window.TerminalDictationEditorWindow") as cls:
        yield cls


# ---------------------------------------------------------------------------
# EventBus fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def event_bus():
    """Real EventBus instance for integration-style tests."""
    from event_bus import EventBus
    return EventBus()


@pytest.fixture
def mock_event_bus():
    """Mock EventBus for unit tests that don't need real pub/sub."""
    bus = Mock()
    bus.publish = AsyncMock()
    bus.subscribe = Mock()
    bus._subscribers = {}
    return bus


# ---------------------------------------------------------------------------
# ConfigService fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config(tmp_path):
    """Mock ConfigService backed by an in-memory dict.

    Usage in tests:
        def test_something(mock_config):
            mock_config._config["SPEECH_ENABLED_ON_STARTUP"] = True
            svc = SomeService(mock_config)
    """
    config_data = {}
    svc = Mock()

    def _get(key, default=None):
        if "." in key:
            keys = key.split(".")
            value = config_data
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                    if value is None:
                        return default
                else:
                    return default
            return value
        return config_data.get(key, default)

    def _set(key, value):
        if "." in key:
            keys = key.split(".")
            target = config_data
            for k in keys[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]
            target[keys[-1]] = value
        else:
            config_data[key] = value

    svc.get = _get
    svc.set = _set
    svc.get_config = lambda: config_data
    svc.save = AsyncMock()
    svc._config = config_data
    svc.config_path = str(tmp_path / "config.toml")
    return svc


# ---------------------------------------------------------------------------
# StateManager fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_state_manager():
    """Mock StateManager with controllable speech state properties.

    Provides the most commonly checked attributes without needing
    real EventBus wiring or multiprocessing queues.
    """
    manager = Mock()
    manager.speech_enabled = True
    manager._speech_enabled = True
    manager._speech_suppressed_by_audio = False
    manager._speech_suppressed_by_sonos = False
    manager._speech_suppressed_by_idle = False
    manager.interim_results_enabled = True
    manager.stt_websocket_connection = None
    manager._stt_manager = None
    manager._remote_stt_launcher = None
    manager.send_state_update = Mock()
    manager.toggle_speech_enabled_state = Mock()
    manager.set_speech_suppressed_by_audio = Mock()
    return manager


# ---------------------------------------------------------------------------
# IPC / Queue fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gui_queue():
    """Mock multiprocessing Queue for GUI state sync."""
    q = Mock(spec=Queue)
    q.put_nowait = Mock()
    return q


@pytest.fixture
def mock_websocket_manager():
    """Mock WebSocketManager for STT connection management."""
    ws = Mock()
    ws.broadcast = AsyncMock()
    ws.send_to = AsyncMock()
    ws.is_connected = Mock(return_value=False)
    return ws
