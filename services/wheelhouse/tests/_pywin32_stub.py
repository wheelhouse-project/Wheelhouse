"""Conditional pywin32 stubs for import-safety (wh-logic-test-import-isolation).

Importing ``main`` pulls the full Logic import graph
(ServiceManager -> MouseHandler -> handlers.audio_monitor -> pythoncom,
plus win32api/win32gui/... elsewhere). pywin32 needs a post-install
step, so reviewer sandboxes and non-Windows CI often lack it; there,
every test doing ``from main import LogicController`` died at import
before exercising any logic.

``ensure_win32_importable`` installs a MagicMock in ``sys.modules`` for
each pywin32-family module that does NOT resolve, and leaves resolvable
or already-imported modules strictly alone. On the WheelHouse dev
environment (pywin32 installed) it is therefore a no-op: the real
modules load and nothing is masked. The check uses
``importlib.util.find_spec``, which locates a module without executing
it, so the probe itself costs no COM/DLL initialization.

This module must stay dependency-free (stdlib only): the conftest loads
it before anything else, and a portability test executes it in a bare
subprocess.
"""
import importlib.util
import sys
from unittest.mock import MagicMock

# Every pywin32-family module the production import graph reaches
# (grep over services/wheelhouse minus tests, 2026-07-05), plus
# pywintypes -- the family's shared bootstrap module, reachable through
# except-clauses even though nothing imports it directly today, plus
# winreg -- stdlib but Windows-only, reached by speech/actions.py
# _default_browser_exe and patched by its tests
# (wh-user-patterns-split.12.3).
WIN32_FAMILY_MODULES = (
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32clipboard",
    "win32con",
    "win32gui",
    "win32process",
    "winreg",
)


def ensure_win32_importable(names=WIN32_FAMILY_MODULES):
    """Stub exactly the ``names`` that fail to resolve; touch nothing else."""
    for name in names:
        if name in sys.modules:
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            # ImportError covers a blocked/missing parent package;
            # ValueError covers a module with __spec__ set to None.
            spec = None
        if spec is None:
            sys.modules[name] = MagicMock(name=f"pywin32-stub:{name}")
