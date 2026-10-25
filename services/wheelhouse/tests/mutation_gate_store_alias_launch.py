"""Mutation gate for the App Execution Alias launch (wh-notepad-app-paths-launch).

os.startfile("notepad.exe") resolves the bare name through the App Paths
registry key. On current Windows 11 that key points into
%ProgramFiles%\\WindowsApps, ShellExecute starts the packaged Notepad.exe
outside its package, and the user sees "Microsoft.UI.Windowing.Core.dll
was not found". utils.installed_programs.store_alias_launch_target turns
such a bare name into the execution alias under
%LOCALAPPDATA%\\Microsoft\\WindowsApps, and leaves every other target as
it was. input_proc.py calls it at the two os.startfile sites that can
receive a bare name.

Run it from services/wheelhouse:

    python tests/mutation_gate_store_alias_launch.py

to check only that the patterns still match exactly once and still
compile, without running any test:

    python tests/mutation_gate_store_alias_launch.py --check

and to run only some mutations (a "ran N of M" scope line is printed):

    python tests/mutation_gate_store_alias_launch.py --only=<name> --only=<name>

Every test in the three test files was written alongside the fix rather
than against an existing defect, so this gate is what proves they can see
the defect at all. It breaks, one at a time, each condition that decides
whether a target reaches the registry, each step that reads and compares
the App Paths value, the alias path and its existence check, the registry
access itself (hive, access mask, key name, value name), and the two
call sites. The gate never writes the registry and starts no program: the
tests it runs replace the registry reader, the alias check and
os.startfile.

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern (more than one match), a mutation that does not compile, a
per-mutation timeout, a suite-timeout abort, any pytest return code other
than 0 or 1, any ERROR line in pytest's short summary, an expected test
that does not exist, and an expected test that failed for any reason other
than its own assertion. A verdict earned by an unrelated exception is no
verdict: the test never reached the assertion the mutation claims to break.

The gate detects each target file's own line endings and translates the
patterns to them before matching, so a later CRLF conversion of either
file cannot turn the multi-line patterns into a silent batch of
pattern-not-found errors. It never rewrites a file's endings, restores
each file with write_bytes, sets PYTHONDONTWRITEBYTECODE=1 and clears
__pycache__ around every run, and sets COLUMNS=1000 so pytest never cuts
a short-summary line.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
INPUT_PROC = SERVICE / "input_proc.py"
LOOKUP = SERVICE / "utils" / "installed_programs.py"

TEST_FILES = [
    "tests/test_installed_programs.py",
    "tests/test_input_proc_activate_window.py",
    "tests/test_input_proc_activate_reply.py",
]

# --tb=line carries the failure REASON. This project's pytest prints its
# short summary as a bare "FAILED <nodeid>" with no reason suffix, so the
# summary alone cannot tell an assertion failure from a crash.
PYTEST_ARGS = ["-p", "no:randomly", "-p", "no:cacheprovider", "-q", "-rfE",
               "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")

# A reason beginning with an exception class name means the test raised
# rather than failed its assertion. AssertionError is the one exception
# name that IS an assertion failure, and pytest.fail prints "Failed:".
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

_SUMMARY_HEADER = "short test summary info"

MUTATIONS = [
    # ---- installed_programs.py: which targets reach the registry ----
    {
        "name": "bare-name-check-removed",
        "file": LOOKUP,
        "old": r'''        if (
            not lowered.endswith(".exe")
            or any(c in target for c in "\\/:")
        ):
            return target
''',
        "new": """        if False:
            return target
""",
        "expect": [
            "test_anything_but_a_bare_exe_name_is_unchanged_without_a_read",
        ],
    },
    {
        "name": "backslash-no-longer-a-separator",
        "file": LOOKUP,
        "old": r'''            or any(c in target for c in "\\/:")''',
        "new": r'''            or any(c in target for c in "/:")''',
        "expect": [
            "test_anything_but_a_bare_exe_name_is_unchanged_without_a_read",
        ],
    },
    {
        "name": "slash-no-longer-a-separator",
        "file": LOOKUP,
        "old": r'''            or any(c in target for c in "\\/:")''',
        "new": r'''            or any(c in target for c in "\\:")''',
        "expect": [
            "test_anything_but_a_bare_exe_name_is_unchanged_without_a_read",
        ],
    },
    {
        "name": "colon-no-longer-a-separator",
        "file": LOOKUP,
        "old": r'''            or any(c in target for c in "\\/:")''',
        "new": r'''            or any(c in target for c in "\\/")''',
        "expect": [
            "test_anything_but_a_bare_exe_name_is_unchanged_without_a_read",
        ],
    },
    {
        "name": "exe-suffix-not-required",
        "file": LOOKUP,
        "old": """            not lowered.endswith(".exe")
""",
        "new": """            False
""",
        "expect": [
            "test_anything_but_a_bare_exe_name_is_unchanged_without_a_read",
        ],
    },
    {
        "name": "exe-suffix-test-is-case-sensitive",
        "file": LOOKUP,
        "old": "        lowered = target.lower()\n",
        "new": "        lowered = target\n",
        "expect": [
            "test_letter_case_quotes_and_an_expandable_value_still_match",
        ],
    },
    # ---- installed_programs.py: which hive decides ----
    {
        "name": "machine-hive-read-before-user-hive",
        "file": LOOKUP,
        "old": '_HIVES = ("HKCU", "HKLM")\n',
        "new": '_HIVES = ("HKLM", "HKCU")\n',
        "expect": [
            "test_the_entry_is_read_under_the_name_given",
            "test_the_per_user_entry_wins_over_the_machine_entry",
        ],
    },
    {
        # The first hive that has a value must decide alone. Here a value
        # outside WindowsApps moves on to the next hive instead of ending
        # the lookup, so a machine entry the user's own entry overrides
        # still turns the name into the alias.
        "name": "first-hive-with-a-value-does-not-decide-alone",
        "file": LOOKUP,
        "old": """                or _in_drive_root_windowsapps(resolved)
            ):
                return target
""",
        "new": """                or _in_drive_root_windowsapps(resolved)
            ):
                continue
""",
        "expect": [
            "test_the_per_user_entry_wins_over_the_machine_entry",
        ],
    },
    {
        "name": "unreadable-value-hive-ends-the-lookup",
        "file": LOOKUP,
        "old": """            except OSError:
                continue
""",
        "new": """            except OSError:
                return target
""",
        "expect": [
            "test_a_missing_per_user_key_falls_through_to_the_machine_key",
        ],
    },
    {
        # PermissionError is an OSError and is not a FileNotFoundError: a
        # per-user key the account cannot read must pass to the next hive
        # exactly as a missing one does.
        "name": "only-a-missing-key-passes-to-the-next-hive",
        "file": LOOKUP,
        "old": """            except OSError:
                continue
""",
        "new": """            except FileNotFoundError:
                continue
""",
        "expect": [
            "test_an_unreadable_per_user_key_falls_through_to_the_machine_key",
        ],
    },
    # ---- installed_programs.py: a value that is not usable ----
    {
        "name": "no-answer-is-not-skipped",
        "file": LOOKUP,
        "old": "            if not answer or not isinstance(answer[0], str) or not answer[0]:\n",
        "new": "            if False or not isinstance(answer[0], str) or not answer[0]:\n",
        "expect": [
            "test_a_value_that_is_not_usable_passes_to_the_next_hive",
        ],
    },
    {
        "name": "non-string-value-is-not-skipped",
        "file": LOOKUP,
        "old": "            if not answer or not isinstance(answer[0], str) or not answer[0]:\n",
        "new": "            if not answer or False or not answer[0]:\n",
        "expect": [
            "test_a_value_that_is_not_usable_passes_to_the_next_hive",
        ],
    },
    {
        "name": "empty-value-is-not-skipped",
        "file": LOOKUP,
        "old": "            if not answer or not isinstance(answer[0], str) or not answer[0]:\n",
        "new": "            if not answer or not isinstance(answer[0], str) or False:\n",
        "expect": [
            "test_a_value_that_is_not_usable_passes_to_the_next_hive",
        ],
    },
    {
        "name": "unusable-value-is-not-passed-over",
        "file": LOOKUP,
        "old": """            if not answer or not isinstance(answer[0], str) or not answer[0]:
                continue
""",
        "new": """            if not answer or not isinstance(answer[0], str) or not answer[0]:
                return target
""",
        "expect": [
            "test_a_value_that_is_not_usable_passes_to_the_next_hive",
        ],
    },
    # ---- installed_programs.py: reading and comparing the value ----
    {
        "name": "expandable-value-not-expanded",
        "file": LOOKUP,
        "old": "            if value_type == _REG_EXPAND_SZ:\n",
        "new": "            if False:\n",
        "expect": [
            "test_letter_case_quotes_and_an_expandable_value_still_match",
        ],
    },
    {
        "name": "plain-value-expanded-too",
        "file": LOOKUP,
        "old": "            if value_type == _REG_EXPAND_SZ:\n",
        "new": "            if True:\n",
        "expect": [
            "test_a_plain_string_value_is_not_expanded",
        ],
    },
    {
        "name": "expand-type-constant-changed",
        "file": LOOKUP,
        "old": "_REG_EXPAND_SZ = 2\n",
        "new": "_REG_EXPAND_SZ = 1\n",
        "expect": [
            "test_letter_case_quotes_and_an_expandable_value_still_match",
            "test_a_plain_string_value_is_not_expanded",
        ],
    },
    {
        "name": "quotes-not-stripped",
        "file": LOOKUP,
        "old": "value.strip().strip('\"')))\n            packaged",
        "new": "value.strip()))\n            packaged",
        "expect": [
            "test_letter_case_quotes_and_an_expandable_value_still_match",
        ],
    },
    {
        "name": "surrounding-spaces-not-stripped",
        "file": LOOKUP,
        "old": "value.strip().strip('\"')))\n            packaged",
        "new": "value.strip('\"')))\n            packaged",
        "expect": [
            "test_spaces_around_a_quoted_value_are_ignored",
        ],
    },
    {
        "name": "value-path-not-normalised",
        "file": LOOKUP,
        "old": "os.path.normcase(os.path.normpath(value.strip().strip('\"')))\n",
        "new": "os.path.normcase(value.strip().strip('\"'))\n",
        "expect": [
            "test_a_value_that_climbs_out_of_windowsapps_is_unchanged",
        ],
    },
    {
        "name": "value-case-not-folded",
        "file": LOOKUP,
        "old": "resolved = os.path.normcase(os.path.normpath(value.strip().strip('\"')))\n",
        "new": "resolved = os.path.normpath(value.strip().strip('\"'))\n",
        "expect": [
            "test_letter_case_quotes_and_an_expandable_value_still_match",
            "test_the_drive_root_windowsapps_folder_matches_in_any_letter_case",
        ],
    },
    {
        "name": "packaged-folder-case-not-folded",
        "file": LOOKUP,
        "old": """            packaged = os.path.normcase(
                os.path.normpath(
""",
        "new": """            packaged = (
                os.path.normpath(
""",
        "expect": [
            "test_a_windowsapps_entry_with_an_alias_gives_the_alias",
        ],
    },
    {
        "name": "sibling-folder-matches-the-prefix",
        "file": LOOKUP,
        "old": "                resolved.startswith(packaged + os.sep)\n",
        "new": "                resolved.startswith(packaged)\n",
        "expect": [
            "test_a_folder_that_only_starts_with_windowsapps_is_unchanged",
        ],
    },
    {
        "name": "prefix-test-becomes-contains",
        "file": LOOKUP,
        "old": "                resolved.startswith(packaged + os.sep)\n",
        "new": "                (packaged + os.sep) in resolved\n",
        "expect": [
            "test_a_path_that_only_contains_the_windowsapps_folder_is_unchanged",
        ],
    },
    {
        "name": "program-files-variable-name-changed",
        "file": LOOKUP,
        "old": '                        os.environ.get("ProgramFiles", "C:\\\\Program Files"),\n',
        "new": '                        os.environ.get("ProgramW6432", "C:\\\\Program Files"),\n',
        "expect": [
            "test_the_windowsapps_folder_is_taken_from_the_program_files_variable",
        ],
    },
    {
        "name": "program-files-default-changed",
        "file": LOOKUP,
        "old": '                        os.environ.get("ProgramFiles", "C:\\\\Program Files"),\n',
        "new": '                        os.environ.get("ProgramFiles", "D:\\\\Program Files"),\n',
        "expect": [
            "test_a_missing_program_files_variable_means_the_default_folder",
        ],
    },
    # ---- installed_programs.py: a WindowsApps folder at a drive root ----
    # (boss ruling 2026-10-19 17:31). The second alternative of the Store
    # package test is _in_drive_root_windowsapps. The conjuncts on
    # len(drive) and drive[1] are each implied by the other two (a UNC
    # drive fails the letter test and the length test), so removing one of
    # them alone changes no behaviour and has no mutation. What does change
    # behaviour: the whole letter-drive group, and the letter range.
    {
        "name": "drive-root-branch-always-false",
        "file": LOOKUP,
        "old": """    return (
        len(drive) == 2
        and drive[1] == ":"
        and "a" <= drive[0] <= "z"
        and rest.startswith(os.sep + "windowsapps" + os.sep)
    )
""",
        "new": """    return False
""",
        "expect": [
            "test_a_windowsapps_folder_at_a_drive_root_gives_the_alias",
            "test_the_drive_root_windowsapps_folder_matches_in_any_letter_case",
            "test_a_quoted_drive_root_windowsapps_value_gives_the_alias",
            "test_an_expandable_drive_root_windowsapps_value_gives_the_alias",
        ],
    },
    {
        "name": "drive-root-branch-always-true",
        "file": LOOKUP,
        "old": """    return (
        len(drive) == 2
        and drive[1] == ":"
        and "a" <= drive[0] <= "z"
        and rest.startswith(os.sep + "windowsapps" + os.sep)
    )
""",
        "new": """    return True
""",
        "expect": [
            "test_a_windowsapps_name_that_is_not_a_drive_root_folder_is_unchanged",
            "test_an_entry_outside_windowsapps_is_unchanged",
        ],
    },
    {
        "name": "drive-root-separator-boundary-removed",
        "file": LOOKUP,
        "old": """        and rest.startswith(os.sep + "windowsapps" + os.sep)
""",
        "new": """        and rest.startswith(os.sep + "windowsapps")
""",
        "expect": [
            "test_a_windowsapps_name_that_is_not_a_drive_root_folder_is_unchanged",
        ],
    },
    {
        "name": "drive-root-letter-drive-check-removed",
        "file": LOOKUP,
        "old": """        len(drive) == 2
        and drive[1] == ":"
        and "a" <= drive[0] <= "z"
        and rest.startswith""",
        "new": """        bool(drive)
        and rest.startswith""",
        "expect": [
            "test_a_windowsapps_name_that_is_not_a_drive_root_folder_is_unchanged",
        ],
    },
    {
        "name": "drive-root-letter-range-check-removed",
        "file": LOOKUP,
        "old": """        and "a" <= drive[0] <= "z"
""",
        "new": """        and True
""",
        "expect": [
            "test_a_windowsapps_name_that_is_not_a_drive_root_folder_is_unchanged",
        ],
    },
    {
        "name": "drive-root-folder-matched-anywhere-below-the-drive",
        "file": LOOKUP,
        "old": """        and rest.startswith(os.sep + "windowsapps" + os.sep)
""",
        "new": """        and (os.sep + "windowsapps" + os.sep) in rest
""",
        "expect": [
            "test_a_windowsapps_name_that_is_not_a_drive_root_folder_is_unchanged",
        ],
    },
    {
        "name": "drive-root-branch-joined-with-and-not-or",
        "file": LOOKUP,
        "old": """                resolved.startswith(packaged + os.sep)
                or _in_drive_root_windowsapps(resolved)
""",
        "new": """                resolved.startswith(packaged + os.sep)
                and _in_drive_root_windowsapps(resolved)
""",
        "expect": [
            "test_a_windowsapps_entry_with_an_alias_gives_the_alias",
            "test_a_windowsapps_folder_at_a_drive_root_gives_the_alias",
        ],
    },
    # ---- installed_programs.py: the alias ----
    {
        "name": "alias-returned-without-checking-it-exists",
        "file": LOOKUP,
        "old": "                if (alias_exists or _alias_exists)(alias):\n",
        "new": "                if True:\n",
        "expect": [
            "test_a_windowsapps_entry_without_an_alias_is_unchanged",
        ],
    },
    {
        "name": "alias-never-returned",
        "file": LOOKUP,
        "old": "                    return alias\n",
        "new": "                    return target\n",
        "expect": [
            "test_a_windowsapps_entry_with_an_alias_gives_the_alias",
        ],
    },
    {
        "name": "alias-built-from-the-wrong-base",
        "file": LOOKUP,
        "old": '                alias = os.path.join(local, "Microsoft", "WindowsApps", target)\n',
        "new": '                alias = os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "WindowsApps", target)\n',
        "expect": [
            "test_a_windowsapps_entry_with_an_alias_gives_the_alias",
        ],
    },
    {
        "name": "alias-built-without-the-microsoft-folder",
        "file": LOOKUP,
        "old": '                alias = os.path.join(local, "Microsoft", "WindowsApps", target)\n',
        "new": '                alias = os.path.join(local, "WindowsApps", target)\n',
        "expect": [
            "test_a_windowsapps_entry_with_an_alias_gives_the_alias",
        ],
    },
    {
        "name": "alias-named-in-lower-case",
        "file": LOOKUP,
        "old": '                alias = os.path.join(local, "Microsoft", "WindowsApps", target)\n',
        "new": '                alias = os.path.join(local, "Microsoft", "WindowsApps", target.lower())\n',
        "expect": [
            "test_letter_case_quotes_and_an_expandable_value_still_match",
        ],
    },
    {
        "name": "missing-local-app-data-not-checked",
        "file": LOOKUP,
        "old": "            if local:\n",
        "new": "            if True:\n",
        "expect": [
            "test_no_local_app_data_means_no_alias_is_looked_for",
        ],
    },
    {
        "name": "missing-alias-says-nothing",
        "file": LOOKUP,
        "old": """            logger.warning(
                "%s has an App Paths entry inside WindowsApps and no "
                "execution alias; starting it as given",
                target,
            )
""",
        "new": """            pass
""",
        "expect": [
            "test_a_windowsapps_entry_without_an_alias_is_unchanged",
        ],
    },
    {
        "name": "lookup-failure-escapes",
        "file": LOOKUP,
        "old": """    except Exception as exc:
        logger.warning("App Paths alias lookup failed for %s: %s", target, exc)
        return target
""",
        "new": """    except Exception as exc:
        raise
""",
        "expect": [
            "test_an_unexpected_error_in_the_lookup_is_logged_and_never_raises",
        ],
    },
    {
        "name": "lookup-failure-says-nothing",
        "file": LOOKUP,
        "old": """    except Exception as exc:
        logger.warning("App Paths alias lookup failed for %s: %s", target, exc)
        return target
""",
        "new": """    except Exception as exc:
        return target
""",
        "expect": [
            "test_an_unexpected_error_in_the_lookup_is_logged_and_never_raises",
        ],
    },
    # ---- installed_programs.py: the registry read ----
    {
        "name": "machine-key-opened-as-the-user-key",
        "file": LOOKUP,
        "old": "else winreg.HKEY_LOCAL_MACHINE\n",
        "new": "else winreg.HKEY_CURRENT_USER\n",
        "expect": [
            "test_the_registry_is_opened_for_reading_only",
        ],
    },
    {
        "name": "user-key-opened-as-the-machine-key",
        "file": LOOKUP,
        "old": 'root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else',
        "new": 'root = winreg.HKEY_CURRENT_USER if hive != "HKCU" else',
        "expect": [
            "test_the_registry_is_opened_for_reading_only",
        ],
    },
    {
        "name": "registry-opened-with-full-access",
        "file": LOOKUP,
        "old": "        root, _APP_PATHS_KEY + \"\\\\\" + name, 0, winreg.KEY_READ\n",
        "new": "        root, _APP_PATHS_KEY + \"\\\\\" + name, 0, winreg.KEY_ALL_ACCESS\n",
        "expect": [
            "test_the_registry_is_opened_for_reading_only",
        ],
    },
    {
        "name": "registry-opened-for-writing",
        "file": LOOKUP,
        "old": "        root, _APP_PATHS_KEY + \"\\\\\" + name, 0, winreg.KEY_READ\n",
        "new": "        root, _APP_PATHS_KEY + \"\\\\\" + name, 0, winreg.KEY_WRITE\n",
        "expect": [
            "test_the_registry_is_opened_for_reading_only",
        ],
    },
    {
        "name": "registry-key-name-lost",
        "file": LOOKUP,
        "old": "        root, _APP_PATHS_KEY + \"\\\\\" + name, 0, winreg.KEY_READ\n",
        "new": "        root, _APP_PATHS_KEY, 0, winreg.KEY_READ\n",
        "expect": [
            "test_the_registry_is_opened_for_reading_only",
        ],
    },
    {
        "name": "registry-value-name-changed",
        "file": LOOKUP,
        "old": '        return winreg.QueryValueEx(key, "")\n',
        "new": '        return winreg.QueryValueEx(key, "Path")\n',
        "expect": [
            "test_the_registry_is_opened_for_reading_only",
        ],
    },
    # ---- installed_programs.py: the alias existence check ----
    {
        "name": "alias-check-always-true",
        "file": LOOKUP,
        "old": "    return os.path.lexists(path)\n",
        "new": "    return True\n",
        "expect": [
            "test_the_default_alias_check_sees_a_file_that_exists",
        ],
    },
    {
        "name": "alias-check-never-true",
        "file": LOOKUP,
        "old": "    return os.path.lexists(path)\n",
        "new": "    return False\n",
        "expect": [
            "test_the_default_alias_check_sees_a_file_that_exists",
        ],
    },
    {
        "name": "alias-check-tests-for-a-folder",
        "file": LOOKUP,
        "old": "    return os.path.lexists(path)\n",
        "new": "    return os.path.isdir(path)\n",
        "expect": [
            "test_the_default_alias_check_sees_a_file_that_exists",
        ],
    },
    # ---- input_proc.py: the two call sites ----
    {
        "name": "exe-launch-starts-the-raw-name",
        "file": INPUT_PROC,
        "old": "        os.startfile(store_alias_launch_target(target))\n",
        "new": "        os.startfile(target)\n",
        "expect": [
            "test_the_exe_launch_path_starts_the_alias",
            "test_a_failed_exe_launch_names_the_spoken_program",
        ],
    },
    {
        "name": "spoken-name-launch-starts-the-raw-name",
        "file": INPUT_PROC,
        "old": "                os.startfile(store_alias_launch_target(candidate.launch_target))\n",
        "new": "                os.startfile(candidate.launch_target)\n",
        "expect": [
            "test_the_spoken_name_launch_path_starts_the_alias",
        ],
    },
    {
        # The alias replaces the name for the notices and logs too, not
        # only for the start call. The failure notice must keep the name
        # the user spoke.
        "name": "exe-launch-notices-the-alias",
        "file": INPUT_PROC,
        "old": "        os.startfile(store_alias_launch_target(target))\n",
        "new": "        target = store_alias_launch_target(target)\n        os.startfile(target)\n",
        "expect": [
            "test_a_failed_exe_launch_names_the_spoken_program",
        ],
    },
    {
        "name": "exe-launch-logs-the-alias",
        "file": INPUT_PROC,
        "old": '        logger.warning(f"Could not launch {target}: {e}")\n',
        "new": '        logger.warning(f"Could not launch {store_alias_launch_target(target)}: {e}")\n',
        "expect": [
            "test_a_failed_exe_launch_logs_the_spoken_name",
        ],
    },
    {
        "name": "exe-launch-refusal-names-the-alias",
        "file": INPUT_PROC,
        "old": '            reply.refuse(f"Could not start {target}.")\n',
        "new": '            reply.refuse(f"Could not start {store_alias_launch_target(target)}.")\n',
        "expect": [
            "test_a_failed_awaited_exe_launch_refuses_under_the_spoken_name",
        ],
    },
    {
        "name": "spoken-name-launch-logs-the-alias",
        "file": INPUT_PROC,
        "old": '                    f"Could not start {candidate.launch_target}: {e}"\n',
        "new": '                    f"Could not start {store_alias_launch_target(candidate.launch_target)}: {e}"\n',
        "expect": [
            "test_a_failed_spoken_name_launch_logs_the_candidates_own_target",
        ],
    },
]


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "1000"},
    )


def _clear_bytecode():
    """Remove cached bytecode for the two target files.

    A mutation that keeps a file the same size can restore within the
    same timestamp, and Python would then run bytecode compiled from the
    other version.
    """
    for path in (INPUT_PROC, LOOKUP):
        shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)


def _deselect(mut):
    """Drop the tests a mutation would make run until the suite timeout.

    main() refuses to start when a deselected name does not exist, or
    when it is also an expected catcher.
    """
    names = mut.get("deselect", ())
    if not names:
        return []
    return ["-k", " and ".join(f"not {name}" for name in names)]


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_FILES, "--collect-only", "-q", "-p", "no:randomly",
                  "-p", "no:cacheprovider")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def summary_lines(output):
    """The lines of pytest's short test summary, and nothing else.

    A captured log record at ERROR level also starts with "ERROR ", so a
    parser that reads every line would turn a genuine catch into an error.
    A green run prints no summary section at all.
    """
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if _SUMMARY_HEADER in line and line.startswith("="):
            tail = []
            for later in lines[index + 1:]:
                if later.startswith("="):
                    break
                tail.append(later)
            return tail
    return []


def failed_names(output):
    names = set()
    for line in summary_lines(output):
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


def error_lines(output):
    return [line for line in summary_lines(output) if line.startswith("ERROR ")]


def _sources():
    """Each target file's original bytes, text, and own line ending."""
    sources = {}
    for path in {m["file"] for m in MUTATIONS}:
        original = path.read_bytes()
        newline = "\r\n" if b"\r\n" in original else "\n"
        sources[path] = {
            "original": original,
            "text": original.decode("utf-8"),
            "newline": newline,
        }
    return sources


def _translate(sources):
    """Rewrite every pattern into its target file's line ending."""
    for mut in MUTATIONS:
        newline = sources[mut["file"]]["newline"]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)


def _prepare(mut, sources):
    """Return (mutated_text, error). Exactly one match, and it must compile."""
    text = sources[mut["file"]]["text"]
    count = text.count(mut["old"])
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(mut["old"], mut["new"], 1)
    try:
        compile(mutated, str(mut["file"]), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only(sources):
    """Report stale and non-compiling patterns without running a test.

    A --check that only counts matches can report clean while a mutation
    can never run, and every round that quotes that clean line spreads the
    false assurance.
    """
    stale, broken = [], []
    names = [m["name"] for m in MUTATIONS]
    for name in sorted({n for n in names if names.count(n) > 1}):
        print("ERROR", f"{name}: mutation name is used more than once")
        stale.append(name)
    for mut in MUTATIONS:
        mutated, error = _prepare(mut, sources)
        if error is None:
            continue
        print("ERROR", error)
        if "does not compile" in error:
            broken.append(mut["name"])
        else:
            stale.append(mut["name"])
    print()
    print(
        f"checked {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(broken)} that do not compile"
    )
    return 1 if stale or broken else 0


def _restore(path, original):
    """Put the original bytes back; report loudly if that fails."""
    for attempt in range(3):
        try:
            # write_bytes, never write_text: on Windows write_text would
            # translate every newline and silently rewrite the file's
            # endings, breaking every multi-line pattern afterwards.
            path.write_bytes(original)
            if path.read_bytes() == original:
                return True
        except OSError as exc:
            print(f"ERROR restore of {path} failed (attempt {attempt + 1}): {exc}")
    print(f"ERROR {path} STILL HOLDS A MUTANT; run git checkout on it")
    return False


def main():
    sources = _sources()
    _translate(sources)

    for path, source in sources.items():
        ending = "CRLF" if source["newline"] == "\r\n" else "LF"
        print(f"line endings in {path.name}: {ending}")

    if "--check" in sys.argv:
        return check_only(sources)

    errors, caught, survived = [], [], []

    _clear_bytecode()
    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {', '.join(TEST_FILES)}")

    # --only=<name> runs one mutation. The scope rule for a review round
    # is the mutations added or affected this round; the whole set runs
    # once before the final commit. A name that matches nothing is an
    # error, never a quiet run of zero mutations.
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
    unknown = [n for n in only if n not in {m["name"] for m in MUTATIONS}]
    if unknown:
        print("ERROR --only names no mutation:", unknown)
        return 1
    selected = [m for m in MUTATIONS if not only or m["name"] in only]

    for mut in selected:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
        for name in mut.get("deselect", ()):
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: deselected test {name!r} does not exist"
                )
            if name in mut["expect"]:
                errors.append(
                    f"{mut['name']}: {name!r} is both expected and "
                    f"deselected, so the mutation could never be caught"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*TEST_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in selected:
        path = mut["file"]
        original = sources[path]["original"]
        mutated, error = _prepare(mut, sources)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        _clear_bytecode()
        path.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*TEST_FILES, *_deselect(mut), *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            if not _restore(path, original):
                errors.append(f"{mut['name']}: restore failed")
            _clear_bytecode()
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        if result.returncode == 0:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; every test passed")
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if missing:
            survived.append(mut["name"])
            print(
                f"SURVIVED {mut['name']}; missing {missing}; "
                f"failed tests were {sorted(got)}"
            )
            continue
        crashed = [
            r for r in failure_reasons(result.stdout)
            if not is_assertion_failure(r)
        ]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    skipped = len(MUTATIONS) - len(selected)
    print(
        f"scope: ran {len(selected)} of {len(MUTATIONS)} mutations, "
        + (f"{skipped} skipped by --only" if skipped else "none skipped")
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
