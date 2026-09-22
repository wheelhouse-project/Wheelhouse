"""Every process must add the owned runtime folder before any native import.

MavenCore (Windows 10, System32 msvcp140.dll 14.29.30133.0) faults inside that
system library when a native extension module built with a newer toolset calls
into it. Measured there 2026-09-19: pysilero_vad's model load exits
-1073741819 and onnxruntime's import fails, while the same code runs on
Windows 11 machines whose system copy is 14.44 or newer.

The order is the whole fix. os.add_dll_directory cannot displace a library the
process already loaded, so a module that imports a native extension before the
call keeps System32's copy and still faults. That is why the static check
below reads every entry point rather than trusting a runtime test.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

from services.runtime_dll_directory import (
    RUNTIME_DLL_DIRECTORY,
    RUNTIME_DLL_NAME,
    add_runtime_dll_directory,
)


SERVICES = Path(__file__).resolve().parents[3] / "services"

# The eleven entry points and the three spawn target modules. A spawn target's
# MODULE is listed, not its function: Windows multiprocessing re-imports the
# module in the child and runs its import block there, so the call has to sit
# at the top of the file that defines the target.
GUARDED_FILES = (
    "wheelhouse/launcher.py",
    "wheelhouse/main.py",
    "wheelhouse/input_proc.py",
    "wheelhouse/gui.py",
    "stt_providers/sherpa_offline_parakeet_stt_server/main.py",
    "stt_providers/sherpa_offline_parakeet_stt_server/launcher.py",
    "stt_providers/google_stt_server/main.py",
    "stt_providers/google_stt_server/launcher.py",
    "stt_providers/distil_medium_en/main.py",
    "stt_providers/distil_medium_en/launcher.py",
    "syscheck/syscheck.py",
    "installer/components/detector.py",
    "stt_providers/evaluation/run_benchmark.py",
    "stt_providers/evaluation/generate_corpus.py",
)

# scripts/release/manifest.toml prunes "services/installer/" from the public
# export, so a tree built by that export holds no installer service at all. A
# guarded file inside a pruned service is skipped there, and only there: the
# whole service directory has to be missing. A guarded file that is missing on
# its own still fails, in an export tree and in a development checkout alike,
# because that is a deleted entry point.
PRUNED_FROM_THE_PUBLIC_EXPORT = ("installer",)

# The bootstrap itself is pure Python and loads no extension module, so it is
# allowed to precede the call.
BOOTSTRAP_MODULES = {"runtime_dll_directory", "services"}


def _first_native_risk_import(tree):
    """Line of the first import that could load an extension module.

    Standard library imports cannot: the interpreter already holds them, or
    they are built in. Everything else can, including this project's own
    modules -- shared_audio pulls in pysilero_vad, and that is the import
    that faults on MavenCore.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import, inside the same package
                continue
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        for name in names:
            if name in ("__future__", ""):
                continue
            if name in sys.stdlib_module_names:
                continue
            if name in BOOTSTRAP_MODULES:
                continue
            return node.lineno, name
    return None, None


def _call_line(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "add_runtime_dll_directory":
                return node.lineno
    return None


def _absent_service_skip_reason(relative, services_root):
    """Why this guarded file cannot be read, or None when it must be read.

    The one accepted reason is that the public export pruned the whole service.
    A file that is missing on its own, inside a service directory that is
    present, is a deleted entry point and has to fail.
    """
    service = relative.split("/")[0]
    if service not in PRUNED_FROM_THE_PUBLIC_EXPORT:
        return None
    if (services_root / service).is_dir():
        return None
    return f"development-only {service} service is absent from this checkout"


@pytest.mark.parametrize("relative", GUARDED_FILES)
def test_entry_point_adds_the_runtime_folder_before_any_native_import(relative):
    reason = _absent_service_skip_reason(relative, SERVICES)
    if reason is not None:
        pytest.skip(reason)
    path = SERVICES / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    call_line = _call_line(tree)
    assert call_line is not None, f"{relative} never calls add_runtime_dll_directory()"
    import_line, name = _first_native_risk_import(tree)
    if import_line is None:
        return
    assert call_line < import_line, (
        f"{relative} imports {name} at line {import_line}, before "
        f"add_runtime_dll_directory() at line {call_line}. A native module "
        f"loaded first keeps System32's msvcp140.dll and still faults."
    )


def test_the_folder_is_one_constant_outside_the_replaced_application_folder():
    # install-wheelhouse.ps1 deletes %LOCALAPPDATA%\Wheelhouse\app on every
    # re-install -- Remove-Item -LiteralPath $AppDir -Recurse -Force -- and
    # re-extracts it with Expand-Archive -DestinationPath $AppDir. The
    # statements are named rather than their line numbers, which this branch
    # moved. A sibling of that folder survives the wipe, so the file does not
    # have to be re-placed each time.
    assert RUNTIME_DLL_DIRECTORY.name == "runtime"
    assert RUNTIME_DLL_DIRECTORY.parent.name == "Wheelhouse"
    assert RUNTIME_DLL_DIRECTORY.parent.name != "app"
    assert RUNTIME_DLL_NAME == "msvcp140.dll"


def test_absent_folder_changes_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("services.runtime_dll_directory.RUNTIME_DLL_DIRECTORY",
                        tmp_path / "not-there")
    monkeypatch.setattr("os.add_dll_directory", lambda p: calls.append(p),
                        raising=False)
    assert add_runtime_dll_directory() is None
    assert calls == []


def test_folder_without_the_file_changes_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("services.runtime_dll_directory.RUNTIME_DLL_DIRECTORY",
                        tmp_path)
    monkeypatch.setattr("os.add_dll_directory", lambda p: calls.append(p),
                        raising=False)
    assert add_runtime_dll_directory() is None
    assert calls == []


def test_the_folder_is_added_when_the_owned_copy_is_new_enough(monkeypatch, tmp_path):
    # A11b: the file being there is no longer the whole condition. The copy
    # written here is a few bytes of text and carries no version resource, so
    # the version a real library would report is supplied instead.
    calls = []
    owned = tmp_path / RUNTIME_DLL_NAME
    owned.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("services.runtime_dll_directory.RUNTIME_DLL_DIRECTORY",
                        tmp_path)
    monkeypatch.setattr("services.runtime_dll_directory._file_version",
                        _version_reader(owned, (14, 44, 35211, 0)))
    monkeypatch.setattr("os.add_dll_directory", lambda p: calls.append(p) or "cookie",
                        raising=False)
    assert add_runtime_dll_directory() == "cookie"
    assert calls == [str(tmp_path)]


def test_nothing_happens_away_from_windows(monkeypatch, tmp_path):
    calls = []
    (tmp_path / RUNTIME_DLL_NAME).write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("services.runtime_dll_directory.RUNTIME_DLL_DIRECTORY",
                        tmp_path)
    monkeypatch.setattr("os.add_dll_directory", lambda p: calls.append(p),
                        raising=False)
    assert add_runtime_dll_directory() is None
    assert calls == []


def test_a_failure_inside_the_call_never_stops_a_process(monkeypatch, tmp_path):
    # This runs above every entry point's imports. An exception here would
    # stop a process that would otherwise have started, which is worse than
    # the fault it prevents on the machines that do not need it.
    #
    # The version is supplied for the same reason as in the test above: since
    # A11b an unreadable owned copy returns before os.add_dll_directory is
    # ever called, and this test would then pass without reaching boom at all.
    owned = tmp_path / RUNTIME_DLL_NAME
    owned.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("services.runtime_dll_directory.RUNTIME_DLL_DIRECTORY",
                        tmp_path)
    monkeypatch.setattr("services.runtime_dll_directory._file_version",
                        _version_reader(owned, (14, 44, 35211, 0)))

    def boom(_path):
        raise OSError("the loader refused the directory")

    monkeypatch.setattr("os.add_dll_directory", boom, raising=False)
    assert add_runtime_dll_directory() is None


# --- A9: the startup notice reuses the speech-setup notice path -------------


def test_the_runtime_notice_leads_the_startup_notices(monkeypatch):
    import service_manager

    monkeypatch.setattr(service_manager, "check_provider_environments",
                        lambda providers: ["Parakeet: Speech environment is out of date."])
    monkeypatch.setattr(service_manager, "runtime_version_notice",
                        lambda: "the runtime is too old")
    # The runtime notice comes first: it explains why the environment notice
    # below it may be the smaller half of the problem.
    assert service_manager.startup_speech_notices([]) == [
        "the runtime is too old",
        "Parakeet: Speech environment is out of date.",
    ]


def test_no_runtime_notice_leaves_the_existing_notices_alone(monkeypatch):
    import service_manager

    monkeypatch.setattr(service_manager, "check_provider_environments",
                        lambda providers: ["Parakeet: Speech environment is out of date."])
    monkeypatch.setattr(service_manager, "runtime_version_notice", lambda: None)
    assert service_manager.startup_speech_notices([]) == [
        "Parakeet: Speech environment is out of date.",
    ]


def test_a_failing_runtime_check_never_loses_the_environment_notices(monkeypatch):
    import service_manager

    def boom():
        raise OSError("version query failed")

    monkeypatch.setattr(service_manager, "check_provider_environments",
                        lambda providers: ["Parakeet: Speech environment is out of date."])
    monkeypatch.setattr(service_manager, "runtime_version_notice", boom)
    assert service_manager.startup_speech_notices([]) == [
        "Parakeet: Speech environment is out of date.",
    ]


def test_the_notice_carries_no_version_numbers(monkeypatch, tmp_path):
    # Boss direction 2026-09-19, for David: a version number in a notice is
    # jargon the reader cannot act on, and 14.30 is not a measured threshold
    # anyway. The numbers belong in the log line beside it.
    from services import runtime_dll_directory as helper

    monkeypatch.setattr(sys, "platform", "win32")
    # The version branch is only reached when a system copy exists, so the
    # test supplies one. Without this the result would depend on whether the
    # computer running the test has the redistributable installed.
    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path / "empty")
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 29, 30133, 0))
    notice = helper.runtime_version_notice()
    assert notice is not None
    # "(x64)" is an architecture name, not a version, so a bare digit test
    # is the wrong measure. A version number is what has to stay out.
    assert re.search(r"\d+\.\d+", notice) is None, notice
    # A11d replaced the wording with a placeholder, so the assertion names
    # only the part any wording has to carry: what the user has to fix.
    assert "Microsoft Visual C++" in notice
    assert "http" not in notice


def test_the_log_line_beside_the_notice_names_both_versions(monkeypatch, tmp_path, caplog):
    # The winrt_capture AudioGraph message sets the pattern: the user text
    # stays plain and the developer detail goes to the log at ERROR.
    from services import runtime_dll_directory as helper

    monkeypatch.setattr(sys, "platform", "win32")
    # The version branch is only reached when a system copy exists, so the
    # test supplies one. Without this the result would depend on whether the
    # computer running the test has the redistributable installed.
    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path / "empty")
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 29, 30133, 0))
    with caplog.at_level("ERROR", logger=helper.__name__):
        helper.runtime_version_notice()
    lines = [record.getMessage() for record in caplog.records
             if record.levelname == "ERROR"]
    assert lines, "no ERROR line was logged beside the notice"
    assert any("14.29.30133.0" in line and "14.30" in line for line in lines), lines


def test_the_notice_fits_the_windows_notification_field(monkeypatch, tmp_path):
    # utils/notice_text.py shortens an over-long notice rather than losing it,
    # so a long text costs the tail, not the whole notice. Keeping the text
    # inside the field means nothing is lost and nothing is logged as cut.
    # The limit comes from that module (szInfo is WCHAR * 256), and this path
    # adds no prefix to the message: stt/provider_env_check.py line 140 passes
    # it through unchanged under the title "Wheelhouse: Speech setup".
    from services import runtime_dll_directory as helper
    from utils.notice_text import MAX_MESSAGE_WIDE_CHARACTERS

    monkeypatch.setattr(sys, "platform", "win32")
    # The version branch is only reached when a system copy exists, so the
    # test supplies one. Without this the result would depend on whether the
    # computer running the test has the redistributable installed.
    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path / "empty")
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 29, 30133, 0))
    notice = helper.runtime_version_notice()
    wide = len(notice.encode("utf-16-le")) // 2
    assert wide <= MAX_MESSAGE_WIDE_CHARACTERS, f"notice is {wide} wide characters"


def test_no_notice_when_the_owned_copy_is_new_enough(monkeypatch, tmp_path):
    # A11b: a trusted owned copy is one at or above the threshold, so the
    # version is supplied here. The file itself carries no version resource.
    from services import runtime_dll_directory as helper

    owned = tmp_path / helper.RUNTIME_DLL_NAME
    owned.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path)
    monkeypatch.setattr(helper, "_file_version",
                        _version_reader(owned, (14, 44, 35211, 0)))
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 29, 30133, 0))
    assert helper.runtime_version_notice() is None


def test_no_notice_when_the_system_runtime_is_new_enough(monkeypatch, tmp_path):
    from services import runtime_dll_directory as helper

    monkeypatch.setattr(sys, "platform", "win32")
    # The version branch is only reached when a system copy exists, so the
    # test supplies one. Without this the result would depend on whether the
    # computer running the test has the redistributable installed.
    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path / "empty")
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 44, 35211, 0))
    assert helper.runtime_version_notice() is None


def test_the_notice_fires_when_the_system_runtime_is_missing_entirely(
        monkeypatch, tmp_path, caplog):
    # A Windows computer that never installed the Microsoft Visual C++
    # Redistributable has no System32 msvcp140.dll at all. That computer is
    # worse off than MavenCore, whose copy is merely old: every native
    # extension module fails to load. Reading the version of a file that is
    # not there gives None, so the first version of this function said
    # nothing on exactly the computer that most needs the notice.
    # Found by GLM 5.3 round 1, wh-parakeet-crash-windows10.1.1.
    from services import runtime_dll_directory as helper

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path / "empty")
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", tmp_path / "absent.dll")
    with caplog.at_level("ERROR", logger=helper.__name__):
        notice = helper.runtime_version_notice()
    assert notice is not None, "a computer with no runtime at all was told nothing"
    # A11d replaced the wording with a placeholder; see the comment in
    # test_the_notice_carries_no_version_numbers.
    assert "Microsoft Visual C++" in notice
    lines = [record.getMessage() for record in caplog.records
             if record.levelname == "ERROR"]
    assert lines, "no ERROR line was logged beside the notice"
    assert any("absent.dll" in line and "empty" in line for line in lines), lines


def test_a_system_runtime_that_is_there_but_unreadable_stays_silent(
        monkeypatch, tmp_path):
    # A file that exists but carries no readable version resource is a
    # different case from a file that is not there. Nothing is known about
    # it, so nothing is said. This keeps the missing-file branch honest: it
    # fires on absence, not on an unreadable version.
    from services import runtime_dll_directory as helper

    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", tmp_path / "empty")
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "system_runtime_version", lambda: None)
    assert helper.runtime_version_notice() is None


def test_the_owned_copy_still_silences_a_missing_system_runtime(
        monkeypatch, tmp_path):
    # The owned folder is checked before anything else, so a computer that
    # already carries a copy new enough to use is quiet whatever System32
    # holds. Without this the missing-file branch would warn a user whose
    # speech engine works. A11b added the version condition, so the version
    # a real library would report is supplied here.
    from services import runtime_dll_directory as helper

    folder = tmp_path / "owned"
    folder.mkdir()
    owned = folder / helper.RUNTIME_DLL_NAME
    owned.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", folder)
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", tmp_path / "absent.dll")
    monkeypatch.setattr(helper, "_file_version",
                        _version_reader(owned, (14, 44, 35211, 0)))
    assert helper.runtime_version_notice() is None


# --- A11b: the owned copy is trusted only when it is new enough -------------


def _version_reader(owned_path, version):
    """A _file_version stand-in that answers for one path and None elsewhere.

    The owned copy in these tests is a few bytes of text, so the real
    _file_version finds no version resource in it and returns None. Reading
    the version is what is under test, so the reader is supplied instead of
    the file being a genuine library.
    """
    def read(path):
        return version if Path(path) == Path(owned_path) else None

    return read


def test_an_owned_copy_older_than_the_threshold_is_not_added(monkeypatch, tmp_path):
    # Wheelhouse's own installer wrote this copy, so a copy older than the
    # threshold means the install put the wrong file there. Adding its folder
    # would hand every process the same too-old library the fix exists to
    # avoid.
    calls = []
    owned = tmp_path / RUNTIME_DLL_NAME
    owned.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("services.runtime_dll_directory.RUNTIME_DLL_DIRECTORY",
                        tmp_path)
    monkeypatch.setattr("services.runtime_dll_directory._file_version",
                        _version_reader(owned, (14, 29, 30133, 0)))
    monkeypatch.setattr("os.add_dll_directory", lambda p: calls.append(p) or "cookie",
                        raising=False)
    assert add_runtime_dll_directory() is None
    assert calls == []


def test_an_owned_copy_older_than_the_threshold_does_not_silence_the_notice(
        monkeypatch, tmp_path):
    # The notice exists to tell a user whose speech engine cannot work. An
    # owned copy that is itself too old repairs nothing, so it must not take
    # the notice away.
    from services import runtime_dll_directory as helper

    owned_folder = tmp_path / "owned"
    owned_folder.mkdir()
    owned = owned_folder / helper.RUNTIME_DLL_NAME
    owned.write_bytes(b"not a real library")
    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", owned_folder)
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "_file_version",
                        _version_reader(owned, (14, 29, 30133, 0)))
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 29, 30133, 0))
    assert helper.runtime_version_notice() is not None


def test_an_owned_copy_whose_version_cannot_be_read_is_not_trusted(
        monkeypatch, tmp_path):
    # The deliberate trade-off, dispatcher decision 2026-09-19: the criterion
    # is "at or above the threshold", and a version nobody can read cannot be
    # shown to meet it. This is on purpose different from the system copy,
    # where an unreadable version stays silent -- that file is not ours.
    from services import runtime_dll_directory as helper

    owned_folder = tmp_path / "owned"
    owned_folder.mkdir()
    (owned_folder / helper.RUNTIME_DLL_NAME).write_bytes(b"not a real library")
    system_copy = tmp_path / "msvcp140.dll"
    system_copy.write_bytes(b"not a real library")
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "RUNTIME_DLL_DIRECTORY", owned_folder)
    monkeypatch.setattr(helper, "SYSTEM_RUNTIME_DLL", system_copy)
    monkeypatch.setattr(helper, "_file_version", lambda path: None)
    monkeypatch.setattr(helper, "system_runtime_version", lambda: (14, 29, 30133, 0))
    monkeypatch.setattr("os.add_dll_directory", lambda p: calls.append(p) or "cookie",
                        raising=False)
    assert helper.add_runtime_dll_directory() is None
    assert calls == []
    assert helper.runtime_version_notice() is not None


# --- A11d: the approved notice text, and it has to fit ----------------------


def test_the_notice_constant_fits_the_windows_notification_field():
    # utils/notice_text.py cuts an over-long message, so a notice past the
    # limit loses its tail. The limit is read from that module rather than
    # written again here: a test carrying its own copy of the number would
    # still pass if the shipped limit dropped, and the notice would be cut
    # at runtime with nothing to report it. David approved the present
    # wording on QUESTIONS-2026-09-19.md item 20, and this guards any later
    # wording the same way.
    from services import runtime_dll_directory as helper
    from utils.notice_text import MAX_MESSAGE_WIDE_CHARACTERS

    # One below szInfo's WCHAR * 256, so the text plus a terminating NUL fits.
    assert MAX_MESSAGE_WIDE_CHARACTERS == 255

    wide = len(helper.RUNTIME_NOTICE.encode("utf-16-le")) // 2
    assert wide <= MAX_MESSAGE_WIDE_CHARACTERS, (
        f"RUNTIME_NOTICE is {wide} wide characters"
    )


def test_the_notice_tells_someone_who_already_ran_the_installer_to_restart():
    # David approved three sentences on QUESTIONS-2026-09-19.md item 20,
    # which replaces items 13, 14 and 15. The third sentence goes at the end
    # of this notice, for a user who selected Yes, let Microsoft's installer
    # finish, and never restarted. Without it the notice tells that user to
    # do the one thing they have already done.
    from services import runtime_dll_directory as helper

    assert helper.RUNTIME_NOTICE.endswith(
        "If you already did that, restart the computer."
    ), helper.RUNTIME_NOTICE


# --- A13: a service the public export prunes --------------------------------


def _services_root_holding(tmp_path, *relatives):
    """A fake services root holding exactly the named files and nothing else."""
    root = tmp_path / "services"
    root.mkdir(parents=True, exist_ok=True)
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def test_a_pruned_service_is_skipped_when_the_whole_service_is_absent(tmp_path):
    root = _services_root_holding(tmp_path, "wheelhouse/launcher.py")
    assert not (root / "installer").exists()
    assert _absent_service_skip_reason("installer/components/detector.py", root) == (
        "development-only installer service is absent from this checkout"
    )


def test_a_missing_entry_file_inside_a_present_service_is_not_skipped(tmp_path):
    root = _services_root_holding(tmp_path, "installer/components/__init__.py")
    assert (root / "installer").is_dir()
    assert not (root / "installer" / "components" / "detector.py").exists()
    assert _absent_service_skip_reason("installer/components/detector.py", root) is None


def test_a_missing_entry_file_inside_a_present_service_still_fails(monkeypatch, tmp_path):
    root = _services_root_holding(tmp_path, "installer/components/__init__.py")
    monkeypatch.setattr(sys.modules[__name__], "SERVICES", root)
    with pytest.raises(FileNotFoundError):
        test_entry_point_adds_the_runtime_folder_before_any_native_import(
            "installer/components/detector.py"
        )


def test_no_guarded_file_outside_a_pruned_service_gains_a_skip(tmp_path):
    root = _services_root_holding(tmp_path)
    for relative in GUARDED_FILES:
        if relative.split("/")[0] in PRUNED_FROM_THE_PUBLIC_EXPORT:
            continue
        assert not (root / relative).exists()
        assert _absent_service_skip_reason(relative, root) is None, relative
