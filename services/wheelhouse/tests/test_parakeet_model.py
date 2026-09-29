"""The Parakeet model check and the installer locator behind the download offer
(wh-parakeet-model-download-offer).

Every test points LOCALAPPDATA at a pytest temporary folder, so nothing here
reads or writes the real per-machine override file, model folder, or Setup
copy. The parity of the completeness rule with the installer's own
PowerShell is held by scripts/release/tests/test_installer.py, which runs
both on the same trees.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from stt import parakeet_model as pm


INT8_SET = ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")
FULL_SET = ("tokens.txt", "encoder.onnx", "decoder.onnx", "joiner.onnx", "encoder.weights")


def _tree(root: Path, names, *, empty=(), dirs=()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name in dirs:
            (root / name).mkdir()
        elif name in empty:
            (root / name).write_bytes(b"")
        else:
            (root / name).write_bytes(b"x")
    return root


@pytest.fixture
def local_app_data(tmp_path, monkeypatch):
    folder = tmp_path / "LocalAppData"
    folder.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder


@pytest.fixture
def provider_dir(tmp_path):
    folder = tmp_path / "provider"
    folder.mkdir()
    (folder / "config.toml").write_text(
        '[provider]\nname = "parakeet_tdt"\n\n[model]\nmodel_path = ""\n',
        encoding="utf-8",
    )
    return folder


def _write_overrides(local_app_data: Path, text: str, *, bom: bool = False) -> Path:
    path = local_app_data / "WheelHouse" / "stt_model_overrides.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


def _toml_path(path: Path) -> str:
    # A TOML literal string, so backslashes in a Windows path stay as they are.
    return "'" + str(path) + "'"


# --- Resolver order ----------------------------------------------------------


def test_override_file_wins_over_config_and_default(local_app_data, provider_dir, tmp_path):
    (provider_dir / "config.toml").write_text(
        f"[provider]\nname = \"parakeet_tdt\"\n\n[model]\nmodel_path = {_toml_path(tmp_path / 'configured')}\n",
        encoding="utf-8",
    )
    _write_overrides(local_app_data, f"[parakeet_tdt]\nmodel_path = {_toml_path(tmp_path / 'override')}\n")
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "override"


def test_override_file_with_a_utf8_bom_is_read(local_app_data, provider_dir, tmp_path):
    _write_overrides(
        local_app_data,
        f"[parakeet_tdt]\nmodel_path = {_toml_path(tmp_path / 'override')}\n",
        bom=True,
    )
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "override"


def test_config_value_is_used_when_no_override_file(local_app_data, provider_dir, tmp_path):
    (provider_dir / "config.toml").write_text(
        f"[provider]\nname = \"parakeet_tdt\"\n\n[model]\nmodel_path = {_toml_path(tmp_path / 'configured')}\n",
        encoding="utf-8",
    )
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "configured"


def test_default_under_local_app_data_when_nothing_is_configured(local_app_data, provider_dir):
    assert pm.resolve_model_dir(provider_dir) == (
        local_app_data / "WheelHouse" / "models" / pm.DEFAULT_MODEL_DIRNAME
    )


@pytest.mark.parametrize(
    "override_text",
    [
        "this is = = not toml\n",
        "[other_provider]\nmodel_path = 'C:/elsewhere'\n",
        "parakeet_tdt = 'a string, not a table'\n",
        "[parakeet_tdt]\nmodel_path = 42\n",
        "[parakeet_tdt]\nmodel_path = '   '\n",
        "[parakeet_tdt]\n",
    ],
    ids=["malformed", "other-section", "not-a-table", "non-string", "blank", "no-key"],
)
def test_an_unusable_override_falls_through_to_the_config_value(
    local_app_data, provider_dir, tmp_path, override_text
):
    """The provider never raises on a bad override file: the tracked value
    stands (provider main.py _resolve_model_path)."""
    (provider_dir / "config.toml").write_text(
        f"[provider]\nname = \"parakeet_tdt\"\n\n[model]\nmodel_path = {_toml_path(tmp_path / 'configured')}\n",
        encoding="utf-8",
    )
    _write_overrides(local_app_data, override_text)
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "configured"


def test_a_non_utf8_override_file_falls_through(local_app_data, provider_dir):
    path = local_app_data / "WheelHouse" / "stt_model_overrides.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"[parakeet_tdt]\nmodel_path = '\xff\xfe'\n")
    assert pm.resolve_model_dir(provider_dir) == (
        local_app_data / "WheelHouse" / "models" / pm.DEFAULT_MODEL_DIRNAME
    )


def test_a_non_string_config_value_is_ignored(local_app_data, provider_dir):
    (provider_dir / "config.toml").write_text(
        "[provider]\nname = \"parakeet_tdt\"\n\n[model]\nmodel_path = 7\n", encoding="utf-8"
    )
    assert pm.resolve_model_dir(provider_dir) == (
        local_app_data / "WheelHouse" / "models" / pm.DEFAULT_MODEL_DIRNAME
    )


def test_a_malformed_provider_config_counts_as_no_configured_value(local_app_data, provider_dir):
    (provider_dir / "config.toml").write_text("[model\nmodel_path = 'x'\n", encoding="utf-8")
    assert pm.resolve_model_dir(provider_dir) == (
        local_app_data / "WheelHouse" / "models" / pm.DEFAULT_MODEL_DIRNAME
    )


def test_the_override_section_is_the_provider_name_from_its_config(
    local_app_data, provider_dir, tmp_path
):
    (provider_dir / "config.toml").write_text(
        '[provider]\nname = "renamed"\n\n[model]\nmodel_path = ""\n', encoding="utf-8"
    )
    _write_overrides(
        local_app_data,
        f"[parakeet_tdt]\nmodel_path = {_toml_path(tmp_path / 'wrong')}\n"
        f"[renamed]\nmodel_path = {_toml_path(tmp_path / 'right')}\n",
    )
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "right"


def test_a_relative_path_resolves_against_the_provider_folder(local_app_data, provider_dir):
    """The provider runs with its own folder as the working directory
    (remote_stt_launcher.py Popen cwd=service_dir), so that is where a
    relative model_path points."""
    _write_overrides(local_app_data, "[parakeet_tdt]\nmodel_path = 'models/p'\n")
    assert pm.resolve_model_dir(provider_dir) == provider_dir / "models" / "p"


def test_the_override_file_is_read_again_at_every_call(local_app_data, provider_dir, tmp_path):
    first = pm.resolve_model_dir(provider_dir)
    assert first == local_app_data / "WheelHouse" / "models" / pm.DEFAULT_MODEL_DIRNAME
    _write_overrides(local_app_data, f"[parakeet_tdt]\nmodel_path = {_toml_path(tmp_path / 'a')}\n")
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "a"
    _write_overrides(local_app_data, f"[parakeet_tdt]\nmodel_path = {_toml_path(tmp_path / 'b')}\n")
    assert pm.resolve_model_dir(provider_dir) == tmp_path / "b"


def test_the_default_provider_folder_is_the_parakeet_provider():
    folder = pm.default_provider_dir()
    assert folder.name == "sherpa_offline_parakeet_stt_server"
    assert folder.parent.name == "stt_providers"


# --- Completeness rule --------------------------------------------------------


def test_complete_int8_model(tmp_path):
    assert pm.model_state(_tree(tmp_path / "m", INT8_SET)) == "complete"
    assert pm.is_model_complete(tmp_path / "m") is True


def test_complete_full_precision_model(tmp_path):
    assert pm.model_state(_tree(tmp_path / "m", FULL_SET)) == "complete"


@pytest.mark.parametrize("missing", INT8_SET)
def test_int8_model_missing_one_file_is_incomplete(tmp_path, missing):
    names = [n for n in INT8_SET if n != missing]
    assert pm.model_state(_tree(tmp_path / "m", names)) == "incomplete"
    assert pm.is_model_complete(tmp_path / "m") is False


@pytest.mark.parametrize("missing", FULL_SET)
def test_full_precision_model_missing_one_file_is_incomplete(tmp_path, missing):
    names = [n for n in FULL_SET if n != missing]
    assert pm.model_state(_tree(tmp_path / "m", names)) == "incomplete"


@pytest.mark.parametrize("empty", FULL_SET)
def test_a_zero_byte_entry_is_incomplete(tmp_path, empty):
    assert pm.model_state(_tree(tmp_path / "m", FULL_SET, empty=(empty,))) == "incomplete"


@pytest.mark.parametrize("folder", INT8_SET)
def test_a_directory_in_place_of_a_file_is_incomplete(tmp_path, folder):
    assert pm.model_state(_tree(tmp_path / "m", INT8_SET, dirs=(folder,))) == "incomplete"


def test_a_non_regular_entry_with_a_size_is_bad(tmp_path, monkeypatch):
    """The regular-file rule on its own, apart from the non-empty rule.

    On Windows os.lstat gives a directory st_size 0, so the directory test
    above is also caught by the non-empty rule and cannot tell the two
    apart. A POSIX directory reports a size (4096 here), and the installer
    rejects any entry that is not a file whatever its size.
    """
    import stat as stat_module
    from types import SimpleNamespace

    root = _tree(tmp_path / "m", INT8_SET)
    real_lstat = os.lstat

    def directory_lstat(path, *args, **kwargs):
        if Path(path).name == "encoder.int8.onnx":
            # Only the three fields model_file_state reads.
            return SimpleNamespace(
                st_mode=stat_module.S_IFDIR | 0o755, st_size=4096, st_file_attributes=0,
            )
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(pm.os, "lstat", directory_lstat)
    assert pm.model_file_state(root / "encoder.int8.onnx") == "bad"
    assert pm.model_state(root) == "incomplete"


def test_a_missing_model_directory_is_incomplete(tmp_path):
    assert pm.model_state(tmp_path / "absent") == "incomplete"
    assert pm.is_model_complete(tmp_path / "absent") is False


def test_a_file_in_place_of_the_model_directory_is_incomplete(tmp_path):
    (tmp_path / "m").write_bytes(b"x")
    assert pm.model_state(tmp_path / "m") == "incomplete"


def test_the_int8_set_does_not_need_the_external_weights(tmp_path):
    assert pm.model_state(_tree(tmp_path / "m", INT8_SET)) == "complete"


def test_a_broken_int8_set_falls_back_to_a_complete_full_precision_set(tmp_path):
    names = ["tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", *FULL_SET[1:]]
    assert pm.model_state(_tree(tmp_path / "m", names)) == "complete"


def test_the_bpe_vocab_is_not_required(tmp_path):
    root = _tree(tmp_path / "m", FULL_SET)
    assert not (root / "bpe.vocab").exists()
    assert pm.model_state(root) == "complete"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_a_hidden_entry_is_unreadable_as_the_installer_sees_it(tmp_path):
    """Get-Item without -Force raises an IOException, not ItemNotFound, on a
    hidden file, so Get-ModelFileState answers 'unreadable' (measured on
    PowerShell 5.1.26100 and 7.6.6)."""
    import ctypes

    root = _tree(tmp_path / "m", INT8_SET)
    assert ctypes.windll.kernel32.SetFileAttributesW(str(root / "tokens.txt"), 0x2)
    assert pm.model_file_state(root / "tokens.txt") == "unreadable"
    assert pm.model_state(root) == "unreadable"
    assert pm.is_model_complete(root) is False


def test_a_symbolic_link_entry_is_bad(tmp_path):
    """PowerShell's Get-Item reports a file symlink with Length 0 (measured on
    PowerShell 5.1.26100 and 7.6.6), so the installer rejects it."""
    root = _tree(tmp_path / "m", INT8_SET)
    target = tmp_path / "real-encoder"
    target.write_bytes(b"xyz")
    (root / "encoder.int8.onnx").unlink()
    try:
        os.symlink(target, root / "encoder.int8.onnx")
    except OSError as exc:
        pytest.skip(f"symbolic links need a privilege this account lacks: {exc}")
    assert pm.model_file_state(root / "encoder.int8.onnx") == "bad"
    assert pm.model_state(root) == "incomplete"


def test_an_unreadable_entry_makes_the_model_incomplete_for_the_offer(tmp_path, monkeypatch):
    root = _tree(tmp_path / "m", INT8_SET)
    real_lstat = os.lstat

    def refusing_lstat(path, *args, **kwargs):
        if Path(path).name == "joiner.int8.onnx":
            raise PermissionError(13, "Access is denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(pm.os, "lstat", refusing_lstat)
    assert pm.model_file_state(root / "joiner.int8.onnx") == "unreadable"
    assert pm.model_state(root) == "unreadable"
    assert pm.is_model_complete(root) is False


def test_parakeet_model_complete_follows_the_resolved_path(local_app_data, provider_dir, tmp_path):
    complete = _tree(tmp_path / "complete", FULL_SET)
    _write_overrides(local_app_data, f"[parakeet_tdt]\nmodel_path = {_toml_path(complete)}\n")
    assert pm.parakeet_model_complete(provider_dir) is True
    _write_overrides(local_app_data, f"[parakeet_tdt]\nmodel_path = {_toml_path(tmp_path / 'empty')}\n")
    assert pm.parakeet_model_complete(provider_dir) is False


def test_a_check_that_raises_counts_as_incomplete(monkeypatch, provider_dir):
    """An error inside the check means the offer is shown, never the switch."""

    def failing_resolve(provider_dir=None):
        raise OSError("disk")

    monkeypatch.setattr(pm, "resolve_model_dir", failing_resolve)
    assert pm.parakeet_model_complete(provider_dir) is False


# --- Installer locator --------------------------------------------------------


STAMPED = (
    '$AppVersion = "{version}"\n'
    '$DefaultArchiveUrl = "https://example.invalid/wheelhouse-$AppVersion.zip"\n'
    '$DefaultArchiveSha256 = "{sha}"\n'
)


def _setup_copy(local_app_data: Path, version: str, sha: str = "a" * 64, *, bom=False) -> Path:
    path = local_app_data / "WheelhouseSetup" / "install-wheelhouse.ps1"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = STAMPED.format(version=version, sha=sha).encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


def test_locator_falls_back_without_a_setup_copy(local_app_data):
    result = pm.locate_installer("1.2.0")
    assert isinstance(result, pm.InstallerFallback)
    assert result.command == pm.ONE_LINE_INSTALL_COMMAND
    assert result.reason == "no_setup_copy"


def test_locator_falls_back_on_a_version_mismatch(local_app_data):
    _setup_copy(local_app_data, "1.1.0")
    result = pm.locate_installer("1.2.0")
    assert isinstance(result, pm.InstallerFallback)
    assert result.reason == "version_mismatch"
    assert result.command == pm.ONE_LINE_INSTALL_COMMAND


def test_locator_falls_back_on_the_unstamped_placeholder(local_app_data):
    _setup_copy(local_app_data, "1.2.0", sha="<ARCHIVE-SHA256>")
    result = pm.locate_installer("1.2.0")
    assert isinstance(result, pm.InstallerFallback)
    assert result.reason == "unstamped"


def test_locator_falls_back_when_the_app_version_is_unknown(local_app_data):
    _setup_copy(local_app_data, "1.2.0")
    result = pm.locate_installer("unknown")
    assert isinstance(result, pm.InstallerFallback)
    assert result.reason == "unknown_app_version"


def test_locator_falls_back_when_the_setup_copy_names_no_version(local_app_data):
    path = local_app_data / "WheelhouseSetup" / "install-wheelhouse.ps1"
    path.parent.mkdir(parents=True)
    path.write_text("# no version here\n", encoding="utf-8")
    result = pm.locate_installer("1.2.0")
    assert isinstance(result, pm.InstallerFallback)
    assert result.reason == "version_mismatch"


def test_locator_uses_the_setup_copy_on_a_version_match(local_app_data):
    path = _setup_copy(local_app_data, "1.2.0", bom=True)
    result = pm.locate_installer("1.2.0")
    assert isinstance(result, pm.InstallerLaunchPlan)
    assert result.script_path == path


def test_locator_reads_the_app_version_by_default(local_app_data, monkeypatch):
    from utils import app_version

    _setup_copy(local_app_data, "9.9.9")
    monkeypatch.setattr(app_version, "get_app_version", lambda: "9.9.9")
    assert isinstance(pm.locate_installer(), pm.InstallerLaunchPlan)
    monkeypatch.setattr(app_version, "get_app_version", lambda: "9.9.8")
    assert isinstance(pm.locate_installer(), pm.InstallerFallback)


def test_locator_falls_back_without_local_app_data(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "")
    result = pm.locate_installer("1.2.0")
    assert isinstance(result, pm.InstallerFallback)
    assert result.reason == "no_setup_copy"


# --- Startup shortcut ---------------------------------------------------------


def test_startup_shortcut_path_is_the_installer_name_in_the_startup_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_startup_folder", lambda: tmp_path)
    assert pm.startup_shortcut_path() == tmp_path / "Wheelhouse.lnk"
    assert pm.startup_shortcut_exists() is False
    (tmp_path / "Wheelhouse.lnk").write_bytes(b"x")
    assert pm.startup_shortcut_exists() is True


def test_no_startup_folder_means_no_shortcut(monkeypatch):
    monkeypatch.setattr(pm, "_startup_folder", lambda: None)
    assert pm.startup_shortcut_path() is None
    assert pm.startup_shortcut_exists() is False


# --- Launch command -----------------------------------------------------------


SCRIPT = Path(r"C:\Users\O'Brien\AppData\Local\WheelhouseSetup\install-wheelhouse.ps1")


def test_the_command_is_windows_powershell_with_bypass():
    cmd = pm.build_launch_command(SCRIPT, [111, 222], auto_start=True)
    assert cmd[0].lower().endswith("powershell.exe")
    assert cmd[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    assert len(cmd) == 6


def test_the_command_runs_the_installer_with_parakeet_and_start_now():
    wrapper = pm.build_launch_command(SCRIPT, [111, 222], auto_start=True)[5]
    assert "-SttProvider parakeet_tdt" in wrapper
    assert "-StartNow yes" in wrapper
    assert "-AutoStart yes" in wrapper
    assert "-AutoStart no" not in wrapper


def test_the_command_passes_auto_start_no():
    wrapper = pm.build_launch_command(SCRIPT, [111], auto_start=False)[5]
    assert "-AutoStart no" in wrapper
    assert "-AutoStart yes" not in wrapper


def test_the_command_waits_for_the_given_process_ids():
    wrapper = pm.build_launch_command(SCRIPT, [111, 222], auto_start=True)[5]
    assert "@(111,222)" in wrapper
    assert "Wait-Process" in wrapper
    assert f"-Timeout {pm.WAIT_FOR_EXIT_SECONDS}" in wrapper or "-Timeout $limit" in wrapper
    assert f"$limit = {pm.WAIT_FOR_EXIT_SECONDS}" in wrapper


def test_the_script_path_is_single_quoted_with_inner_quotes_doubled():
    wrapper = pm.build_launch_command(SCRIPT, [111], auto_start=True)[5]
    assert "& 'C:\\Users\\O''Brien\\AppData\\Local\\WheelhouseSetup\\install-wheelhouse.ps1'" in wrapper


def test_typographic_single_quotes_are_doubled_too():
    """PowerShell ends a single-quoted string on U+2018 to U+201B as well."""
    path = Path("C:\\Users\\O\u2019Brien\\install-wheelhouse.ps1")
    wrapper = pm.build_launch_command(path, [1], auto_start=True)[5]
    assert "O\u2019\u2019Brien" in wrapper


def test_the_wrapper_has_no_double_quotes():
    """The wrapper travels as one command-line argument; a double quote in it
    would need escaping that powershell.exe and the C runtime read
    differently."""
    wrapper = pm.build_launch_command(SCRIPT, [111], auto_start=True)[5]
    assert '"' not in wrapper


def test_the_no_start_branch_says_so_and_exits_before_the_installer():
    wrapper = pm.build_launch_command(SCRIPT, [111], auto_start=True)[5]
    message = "Wheelhouse did not close"
    assert message in wrapper
    assert "the Wheelhouse installer was not started" in wrapper
    assert "ReadKey" in wrapper
    no_start = wrapper.index(message)
    exit_at = wrapper.index("exit 1", no_start)
    assert exit_at < wrapper.index("& '")


def test_the_wrapper_waits_on_the_installers_own_running_test():
    wrapper = pm.build_launch_command(SCRIPT, [111], auto_start=True)[5]
    assert "Win32_Process" in wrapper
    for name in ("python.exe", "pythonw.exe", "uv.exe"):
        assert f"''{name}''" in wrapper
    assert "Wheelhouse\\app" in wrapper


@pytest.mark.parametrize("bad", [[0], [-5], ["12; Remove-Item x"]])
def test_process_ids_must_be_positive_integers(bad):
    with pytest.raises((ValueError, TypeError)):
        pm.build_launch_command(SCRIPT, bad, auto_start=True)


def test_an_empty_process_list_is_allowed():
    wrapper = pm.build_launch_command(SCRIPT, [], auto_start=True)[5]
    assert "@()" in wrapper


# --- Launcher -----------------------------------------------------------------


class _Recorder:
    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise self.raises
        return object()


def test_launcher_opens_a_new_console_in_the_setup_folder(tmp_path, monkeypatch):
    runner = _Recorder()
    monkeypatch.setattr(pm, "_popen", runner)
    script = tmp_path / "WheelhouseSetup" / "install-wheelhouse.ps1"
    assert pm.launch_installer(script, [42], auto_start=False) is True
    (args, kwargs), = runner.calls
    assert args == pm.build_launch_command(script, [42], auto_start=False)
    assert kwargs["cwd"] == str(script.parent)
    assert kwargs["creationflags"] == pm.CREATE_NEW_CONSOLE
    assert not kwargs["creationflags"] & 0x01000000, "no CREATE_BREAKAWAY_FROM_JOB"


def test_launcher_reads_the_startup_shortcut_when_auto_start_is_not_given(tmp_path, monkeypatch):
    runner = _Recorder()
    monkeypatch.setattr(pm, "_popen", runner)
    monkeypatch.setattr(pm, "_startup_folder", lambda: tmp_path)
    script = tmp_path / "install-wheelhouse.ps1"
    pm.launch_installer(script, [42])
    assert "-AutoStart no" in runner.calls[-1][0][5]
    (tmp_path / "Wheelhouse.lnk").write_bytes(b"x")
    pm.launch_installer(script, [42])
    assert "-AutoStart yes" in runner.calls[-1][0][5]


def test_launcher_reports_a_failed_start(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_popen", _Recorder(raises=OSError("no powershell")))
    assert pm.launch_installer(tmp_path / "x.ps1", [42], auto_start=True) is False


# --- The wrapper runs (Windows PowerShell, a stand-in script, never the installer)


def _run_wrapper(cmd, local_app_data):
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_app_data)
    return subprocess.run(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _stand_in_script(folder: Path) -> tuple[Path, Path]:
    marker = folder / "ran.txt"
    script = folder / "install-wheelhouse.ps1"
    script.write_text(
        "param([string]$SttProvider, [string]$StartNow, [string]$AutoStart)\n"
        f"Set-Content -LiteralPath '{marker}' -Value \"$SttProvider|$StartNow|$AutoStart\"\n",
        encoding="utf-8",
    )
    return script, marker


@pytest.mark.skipif(sys.platform != "win32", reason="drives powershell.exe")
def test_the_wrapper_starts_the_script_once_wheelhouse_has_closed(tmp_path, local_app_data):
    setup = tmp_path / "Setup Folder"
    setup.mkdir()
    script, marker = _stand_in_script(setup)
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    cmd = pm.build_launch_command(script, [finished.pid], auto_start=False, timeout_seconds=10)
    result = _run_wrapper(cmd, local_app_data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8").strip() == "parakeet_tdt|yes|no"


@pytest.mark.skipif(sys.platform != "win32", reason="drives powershell.exe")
def test_the_wrapper_does_not_start_the_script_while_a_process_id_lives(tmp_path, local_app_data):
    script, marker = _stand_in_script(tmp_path)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        cmd = pm.build_launch_command(script, [sleeper.pid], auto_start=True, timeout_seconds=2)
        result = _run_wrapper(cmd, local_app_data)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert not marker.exists(), "the installer must not start while Wheelhouse runs"
    assert "Wheelhouse did not close" in result.stdout
    assert result.returncode == 1


@pytest.mark.skipif(sys.platform != "win32", reason="drives powershell.exe")
def test_the_wrapper_waits_for_any_python_in_the_app_folder(tmp_path, local_app_data):
    """The installer refuses while any python.exe, pythonw.exe or uv.exe has
    the app folder on its command line (Test-RunningWheelHouse), so the
    wrapper waits for those too, not only for the ids it was given."""
    script, marker = _stand_in_script(tmp_path)
    app_dir = local_app_data / "Wheelhouse" / "app"
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", str(app_dir / "services")]
    )
    try:
        cmd = pm.build_launch_command(script, [], auto_start=True, timeout_seconds=2)
        result = _run_wrapper(cmd, local_app_data)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert not marker.exists()
    assert "Wheelhouse did not close" in result.stdout
