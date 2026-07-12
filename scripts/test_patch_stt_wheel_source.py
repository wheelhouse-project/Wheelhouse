# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest",
#     "tomlkit",
# ]
# ///
"""Unit tests for scripts/patch_stt_wheel_source.py (wh-e2m7).

The patcher rewrites the pywhispercpp [tool.uv.sources] path in
services/stt_providers/shared/pyproject.toml after a wheel rebuild, so the
user no longer copy-pastes the new filename by hand. Must be idempotent and
preserve TOML formatting and comments.

Run: uv run --with pytest --with tomlkit pytest scripts/test_patch_stt_wheel_source.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent / "patch_stt_wheel_source.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("patch_stt_wheel_source", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["patch_stt_wheel_source"] = module
    spec.loader.exec_module(module)
    return module


psw = _load_module()

PYPROJECT_TEMPLATE = """\
[project]
name = "shared-stt"
version = "0.1.0"
dependencies = [
    "numpy",
]

# Vulkan wheel protection: this path source keeps GPU acceleration alive.
[tool.uv.sources]
pywhispercpp = { path = "vendor/wheels/pywhispercpp-1.0-cp312-cp312-win_amd64.whl" }
"""

OLD_WHEEL = "pywhispercpp-1.0-cp312-cp312-win_amd64.whl"
NEW_WHEEL = "pywhispercpp-2.0.dev1+gabc123-cp312-cp312-win_amd64.whl"


@pytest.fixture
def service_dir(tmp_path):
    """A fake shared-service tree: pyproject.toml + vendor/wheels/<new wheel>."""
    (tmp_path / "vendor" / "wheels").mkdir(parents=True)
    (tmp_path / "vendor" / "wheels" / NEW_WHEEL).write_bytes(b"fake wheel")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_TEMPLATE, encoding="utf-8")
    return tmp_path


def test_patches_path_to_new_wheel(service_dir, capsys):
    rc = psw.patch(service_dir / "pyproject.toml", NEW_WHEEL)
    assert rc == 0
    text = (service_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert f'path = "vendor/wheels/{NEW_WHEEL}"' in text
    assert OLD_WHEEL not in text
    out = capsys.readouterr().out
    assert OLD_WHEEL in out and NEW_WHEEL in out  # prints old -> new


def test_preserves_comments_and_layout(service_dir):
    psw.patch(service_dir / "pyproject.toml", NEW_WHEEL)
    text = (service_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "# Vulkan wheel protection" in text
    assert '"numpy",' in text


def test_idempotent_second_run_reports_unchanged(service_dir, capsys):
    assert psw.patch(service_dir / "pyproject.toml", NEW_WHEEL) == 0
    first = (service_dir / "pyproject.toml").read_text(encoding="utf-8")
    capsys.readouterr()
    assert psw.patch(service_dir / "pyproject.toml", NEW_WHEEL) == 0
    second = (service_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert first == second
    assert "unchanged" in capsys.readouterr().out.lower()


def test_missing_wheel_file_fails(service_dir, capsys):
    rc = psw.patch(service_dir / "pyproject.toml", "pywhispercpp-9.9-missing.whl")
    assert rc == 1
    text = (service_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert OLD_WHEEL in text  # untouched
    assert "not found" in capsys.readouterr().err.lower()


def test_missing_sources_entry_fails(service_dir, capsys):
    stripped = PYPROJECT_TEMPLATE.replace(
        'pywhispercpp = { path = "vendor/wheels/pywhispercpp-1.0-cp312-cp312-win_amd64.whl" }\n',
        "",
    )
    (service_dir / "pyproject.toml").write_text(stripped, encoding="utf-8")
    rc = psw.patch(service_dir / "pyproject.toml", NEW_WHEEL)
    assert rc == 1
    assert "pywhispercpp" in capsys.readouterr().err


def test_missing_pyproject_fails(tmp_path, capsys):
    rc = psw.patch(tmp_path / "nope" / "pyproject.toml", NEW_WHEEL)
    assert rc == 1
    assert "pyproject" in capsys.readouterr().err.lower()
