"""Tests for system.py - System-level utility functions.

Tests cover:
- get_app_data_path uses APPDATA on Windows
- Fallback to home directory when APPDATA not set
- Fallback to tempdir when directory creation fails
- Directory is created if it doesn't exist
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


class TestGetAppDataPath:
    """Tests for the get_app_data_path function."""

    def test_uses_appdata_env(self, tmp_path):
        from utils.system import get_app_data_path

        appdata = str(tmp_path / "AppData")
        with patch.dict(os.environ, {"APPDATA": appdata}):
            result = get_app_data_path()

        assert "WheelHouse" in result
        assert result.startswith(appdata)
        assert os.path.isdir(result)

    def test_creates_directory(self, tmp_path):
        from utils.system import get_app_data_path

        appdata = str(tmp_path / "AppData")
        expected_dir = os.path.join(appdata, "WheelHouse")
        assert not os.path.exists(expected_dir)

        with patch.dict(os.environ, {"APPDATA": appdata}):
            result = get_app_data_path()

        assert os.path.isdir(result)

    def test_fallback_to_home_when_no_appdata(self, tmp_path):
        from utils.system import get_app_data_path

        with patch("utils.system.os.getenv", return_value=None):
            with patch("utils.system.os.path.expanduser", return_value=str(tmp_path)):
                result = get_app_data_path()

        assert ".wheelhouse" in result
        assert os.path.isdir(result)

    def test_fallback_to_tempdir_on_oserror(self):
        from utils.system import get_app_data_path

        call_count = 0
        original_makedirs = os.makedirs

        def failing_makedirs(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("permission denied")
            return original_makedirs(path, **kwargs)

        with patch.dict(os.environ, {"APPDATA": "Z:\\nonexistent"}):
            with patch("utils.system.os.makedirs", side_effect=failing_makedirs):
                result = get_app_data_path()

        assert "WheelHouse" in result

    def test_returns_string(self, tmp_path):
        from utils.system import get_app_data_path

        with patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            result = get_app_data_path()

        assert isinstance(result, str)


class TestGetUserDataDir:
    """Frozen-aware user-state directory resolution (wh-k8ef).

    Under a PyInstaller frozen build, sys.frozen is True and __file__
    resolves inside the temporary _MEIxxxxxx extraction dir that is
    wiped on exit. User state written there would not survive a
    session, so the frozen branch must resolve under the persistent
    app-data root instead.
    """

    def test_non_frozen_resolves_to_repo_data_dir(self):
        import sys as real_sys

        import utils.system as system_mod
        from utils.system import get_user_data_dir

        assert not getattr(real_sys, "frozen", False)
        expected = (
            Path(system_mod.__file__).resolve().parents[1] / "data"
        )
        assert get_user_data_dir() == expected

    def test_frozen_resolves_under_app_data(self, tmp_path, monkeypatch):
        import sys as real_sys

        from utils.system import get_user_data_dir

        appdata = tmp_path / "AppData"
        monkeypatch.setattr(real_sys, "frozen", True, raising=False)
        monkeypatch.setenv("APPDATA", str(appdata))

        result = get_user_data_dir()

        assert result == appdata / "WheelHouse" / "data"
        # The frozen branch must create the directory so first-run
        # writes do not fail on a missing parent.
        assert result.is_dir()


class TestGetBundledDataDir:
    """Frozen-aware shipped-data directory resolution (wh-k8ef).

    Read-only data shipped with the app (the soft-allow starter list)
    lives inside the frozen bundle (sys._MEIPASS), never in the
    per-user app-data root.
    """

    def test_non_frozen_matches_user_data_dir(self):
        from utils.system import get_bundled_data_dir, get_user_data_dir

        # In a source checkout, shipped data and user state share
        # services/wheelhouse/data so the developer loop is unchanged.
        assert get_bundled_data_dir() == get_user_data_dir()

    def test_frozen_resolves_under_meipass(self, tmp_path, monkeypatch):
        import sys as real_sys

        from utils.system import get_bundled_data_dir

        bundle = tmp_path / "bundle"
        monkeypatch.setattr(real_sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            real_sys, "_MEIPASS", str(bundle), raising=False
        )

        assert get_bundled_data_dir() == bundle / "data"
