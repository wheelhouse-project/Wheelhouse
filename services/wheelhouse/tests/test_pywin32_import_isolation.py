"""Tests for the conditional pywin32 stub (wh-logic-test-import-isolation).

Importing ``main`` pulls the full Logic import graph, which reaches
pythoncom/win32* (pywin32). In environments where pywin32 is absent
(reviewer sandboxes, non-Windows CI), every test file doing
``from main import LogicController`` used to die at import before
exercising any logic. The conftest now installs MagicMock stubs for
exactly the pywin32-family modules that fail to resolve -- and never
for ones that resolve, so the WheelHouse dev environment (pywin32
installed) keeps the real modules and nothing is masked.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

_TESTS_DIR = Path(__file__).parent
_SERVICE_DIR = _TESTS_DIR.parent
_PROJECT_ROOT = _SERVICE_DIR.parent.parent
_STUB_PATH = _TESTS_DIR / "_pywin32_stub.py"


def _load_stub_module():
    spec = importlib.util.spec_from_file_location(
        "_pywin32_stub_under_test", _STUB_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_missing_module_gets_a_stub():
    mod = _load_stub_module()
    fake = "wh_definitely_absent_module_xyz"
    try:
        mod.ensure_win32_importable([fake])
        assert fake in sys.modules
        assert isinstance(sys.modules[fake], MagicMock)
    finally:
        sys.modules.pop(fake, None)


def test_resolvable_module_is_not_stubbed():
    mod = _load_stub_module()
    mod.ensure_win32_importable(["secrets"])
    import secrets

    assert not isinstance(secrets, MagicMock)
    assert callable(secrets.token_hex)


def test_already_imported_module_untouched():
    mod = _load_stub_module()
    before = sys.modules["sys"]
    mod.ensure_win32_importable(["sys"])
    assert sys.modules["sys"] is before


def test_default_list_covers_the_observed_import_graph():
    """Every pywin32-family module the production import graph reaches
    (grep over services/wheelhouse minus tests, 2026-07-05) must be in
    the default list, plus pywintypes (the family's shared bootstrap
    module, reachable via except-clauses)."""
    mod = _load_stub_module()
    assert set(mod.WIN32_FAMILY_MODULES) >= {
        "pythoncom",
        "pywintypes",
        "win32api",
        "win32clipboard",
        "win32con",
        "win32gui",
        "win32process",
        # reached by ui/elevation_check.py (wh-elevated-target-notice)
        "win32security",
        # stdlib Windows-only, reached by speech/actions.py
        # _default_browser_exe and patched by TestDefaultBrowserExe
        # (reviewer_0 finding wh-user-patterns-split.12.3)
        "winreg",
    }


def test_main_imports_without_pywin32():
    """The acceptance case: with pywin32 blocked (simulating an
    environment where it is not installed), the conftest stub makes
    ``from main import LogicController`` succeed."""
    script = f"""
import sys
import importlib.abc

class _BlockPywin32(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if root in ("pythoncom", "pywintypes") or root.startswith("win32"):
            raise ModuleNotFoundError("blocked for portability test: " + fullname)
        return None

sys.meta_path.insert(0, _BlockPywin32())
sys.path.insert(0, {str(_PROJECT_ROOT)!r})
sys.path.insert(0, {str(_SERVICE_DIR)!r})

import importlib.util as _u
spec = _u.spec_from_file_location("_pywin32_stub", {str(_STUB_PATH)!r})
mod = _u.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.ensure_win32_importable()

from main import LogicController
assert hasattr(LogicController, "_build_gui_handler_map")
print("IMPORT_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(_SERVICE_DIR),
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "IMPORT_OK" in result.stdout
