"""Repo-wide guard: no GPL-adjacent GUI automation imports in the source tree.

wh-drop-pyautogui: pyautogui's win32 install dependency MouseInfo is GPLv3 --
unshippable in an Apache-2.0 release. These guards pin pyautogui's absence
from the whole services/wheelhouse source tree and from pyproject.toml.

These guards live in their own file because they cover the whole source
tree rather than one component.
"""
from pathlib import Path

# services/wheelhouse (this file is services/wheelhouse/tests/<file>)
_ROOT = Path(__file__).resolve().parents[1]


class TestNoGplImports:
    def test_no_pyautogui_or_mouseinfo_imports_in_source_tree(self):
        """pyautogui pulls in MouseInfo (GPLv3) on win32. Neither may be
        imported anywhere in the shipped source tree."""
        root = _ROOT
        assert (root / "pyproject.toml").is_file(), f"wrong scan root: {root}"
        banned = ("import " + "pyautogui", "import " + "mouseinfo")
        offenders = []
        for py in root.rglob("*.py"):
            if ".venv" in py.parts or "site-packages" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            if any(b in text for b in banned):
                offenders.append(str(py))
        assert offenders == [], f"GPL-adjacent imports found: {offenders}"

    def test_pyautogui_not_declared_in_pyproject(self):
        root = _ROOT
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert "pyautogui" not in text
