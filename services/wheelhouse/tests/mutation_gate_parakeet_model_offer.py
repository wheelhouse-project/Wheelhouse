"""Mutation evidence for the Parakeet model download offer
(wh-parakeet-model-download-offer, acceptance A5).

Three pieces of the offer decide whether it appears, and this gate breaks
each of them and requires the named tests to fail:

  - the completeness check in stt/parakeet_model.py (a copy of the
    installer's Get-ModelState and Get-ModelFileState): each required file,
    the non-empty rule, the regular-file rule, the int8 and full-precision
    branches, the encoder.weights requirement, and an unreadable entry or a
    failed check counting as incomplete;
  - the Logic check in main.py _switch_stt_provider: only a Parakeet pick
    is checked, and an incomplete model returns before stop_provider;
  - the tray-menu query RemoteSTTLauncher.get_not_installed_providers: a
    Parakeet folder with enabled = false AND an incomplete model, never a
    template and never another engine's folder.

It also breaks the PID-file check the installer launch depends on
(acceptance A7): RemoteSTTLauncher._pid_file_names_live_provider counts a
live process named in a provider PID file only when it is older than the
file (plus a 2 s slack), or, when its start time is not readable, only
under a provider process name. The ``pidfile-`` mutations below.

The parity of the completeness rule with the installer's own PowerShell is
held separately, by scripts/release/tests/test_installer.py.

Run from services/wheelhouse with the worktree's own interpreter
(``sys.executable`` becomes the pytest launcher, see LAUNCH below):

    .venv/Scripts/python.exe tests/mutation_gate_parakeet_model_offer.py --check
    .venv/Scripts/python.exe tests/mutation_gate_parakeet_model_offer.py

WHY THIS GATE REPLACES ``runner.LAUNCH``. The default launch goes through
``scripts/run_tests.py``, which runs ``uv run pytest``, and ``uv`` builds a
.venv wherever it runs; in a git worktree that is what makes the directory
undeletable. This gate launches ``<python> -m pytest`` in the service
directory instead, as mutation_gate_pattern_manager_tree_changed.py does.

MUTATIONS THAT ARE NOT HERE, and why:

  - Removing the discovery short-circuit at the top of
    get_not_installed_providers (``if any(... for p in self.get_providers())``)
    on its own is EQUIVALENT. The folder scan below it skips every folder
    whose Parakeet is enabled, and discovery lists only enabled folders, so
    with one Parakeet folder the answer is the same. Only a second, disabled
    Parakeet folder beside an enabled one could tell them apart. The scan's
    own enabled test is mutated below (``menu-lists-an-enabled-parakeet``).
  - Moving the Logic check below stop_provider is not written as a move:
    ``logic-offer-falls-through-to-the-switch`` already requires that no
    engine is stopped when the offer is sent, and the same assertion is
    what a moved check would break.
  - Dropping NotADirectoryError from model_file_state's first except clause
    is not here. On Windows, os.lstat under a path whose parent is a file
    raises FileNotFoundError, so the clause is not reached on the machine
    this gate runs on.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (see the module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly", "-p", "no:cacheprovider"]),
    ]
    return command, service


runner.LAUNCH = _launch

MODEL_TESTS = "tests/test_parakeet_model.py"
OFFER_TESTS = "tests/test_parakeet_download_offer.py"

MODEL_SRC = "stt/parakeet_model.py"
MAIN_SRC = "main.py"
LAUNCHER_SRC = "stt/remote_stt_launcher.py"

# --- expected catchers, by the name pytest reports ---------------------------

# tests/test_parakeet_model.py
INT8_COMPLETE = "test_complete_int8_model"
FULL_COMPLETE = "test_complete_full_precision_model"
INT8_MISSING = "test_int8_model_missing_one_file_is_incomplete"
FULL_MISSING = "test_full_precision_model_missing_one_file_is_incomplete"
ZERO_BYTE = "test_a_zero_byte_entry_is_incomplete"
NON_REGULAR = "test_a_non_regular_entry_with_a_size_is_bad"
BROKEN_INT8_FALLS_BACK = "test_a_broken_int8_set_falls_back_to_a_complete_full_precision_set"
HIDDEN = "test_a_hidden_entry_is_unreadable_as_the_installer_sees_it"
UNREADABLE = "test_an_unreadable_entry_makes_the_model_incomplete_for_the_offer"
CHECK_RAISES = "test_a_check_that_raises_counts_as_incomplete"

# tests/test_parakeet_download_offer.py -- the Logic check
L_INCOMPLETE = "test_an_incomplete_model_sends_the_launch_offer_and_changes_nothing"
L_FALLBACK = "test_no_setup_copy_sends_the_fallback_offer"
L_COMPLETE = "test_a_complete_model_switches_as_today"
L_EVERY_PICK = "test_the_model_is_checked_at_every_pick"
L_OTHER_ENGINE = "test_another_engine_never_checks_the_parakeet_model"

# tests/test_parakeet_download_offer.py -- the tray-menu query
Q_BOSS = "test_disabled_parakeet_is_left_out_of_discovery_and_listed_in_the_menu"
Q_NAMES = "test_the_query_names_the_disabled_parakeet"
Q_COMPLETE = "test_nothing_is_listed_when_the_model_is_complete"
Q_RECHECK = "test_the_model_is_checked_again_at_every_query"
Q_ENABLED_DROPPED = "test_an_enabled_parakeet_that_discovery_leaves_out_is_not_listed"
Q_OTHER_ENGINE = "test_a_disabled_provider_is_not_taken_for_parakeet"
Q_TEMPLATE = "test_a_disabled_parakeet_template_is_not_listed"

MUTATIONS = []


def add(name, file, tests, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=tests,
                          file=SERVICE / file, old=old, new=new,
                          expect=list(expect)))


# --- the completeness check (stt/parakeet_model.py) --------------------------

INT8_SET = '_INT8_SET = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")\n'
FULL_SET = (
    '_FULL_PRECISION_SET = ("encoder.onnx", "decoder.onnx", "joiner.onnx", '
    '"encoder.weights")\n'
)

add("tokens-not-required", MODEL_SRC, MODEL_TESTS,
    '    if tokens == "ok":\n',
    "    if True:\n",
    INT8_MISSING, FULL_MISSING)
add("int8-set-without-joiner", MODEL_SRC, MODEL_TESTS,
    INT8_SET,
    '_INT8_SET = ("encoder.int8.onnx", "decoder.int8.onnx")\n',
    INT8_MISSING)
add("int8-set-without-encoder", MODEL_SRC, MODEL_TESTS,
    INT8_SET,
    '_INT8_SET = ("decoder.int8.onnx", "joiner.int8.onnx")\n',
    INT8_MISSING)
add("full-set-without-encoder-weights", MODEL_SRC, MODEL_TESTS,
    FULL_SET,
    '_FULL_PRECISION_SET = ("encoder.onnx", "decoder.onnx", "joiner.onnx")\n',
    FULL_MISSING)
add("full-set-without-decoder", MODEL_SRC, MODEL_TESTS,
    FULL_SET,
    '_FULL_PRECISION_SET = ("encoder.onnx", "joiner.onnx", "encoder.weights")\n',
    FULL_MISSING)
add("only-the-int8-branch", MODEL_SRC, MODEL_TESTS,
    "        for required in (_INT8_SET, _FULL_PRECISION_SET):\n",
    "        for required in (_INT8_SET,):\n",
    FULL_COMPLETE, BROKEN_INT8_FALLS_BACK)
add("only-the-full-precision-branch", MODEL_SRC, MODEL_TESTS,
    "        for required in (_INT8_SET, _FULL_PRECISION_SET):\n",
    "        for required in (_FULL_PRECISION_SET,):\n",
    INT8_COMPLETE)
# One missing entry stops the set's scan but no longer marks the set short.
add("a-bad-entry-leaves-the-set-present", MODEL_SRC, MODEL_TESTS,
    "                if state != \"ok\":\n"
    "                    all_present = False\n"
    "                    break\n",
    "                if state != \"ok\":\n"
    "                    break\n",
    INT8_MISSING, FULL_MISSING)
add("a-missing-entry-reads-ok", MODEL_SRC, MODEL_TESTS,
    "    except (FileNotFoundError, NotADirectoryError):\n"
    "        return \"bad\"\n",
    "    except (FileNotFoundError, NotADirectoryError):\n"
    "        return \"ok\"\n",
    INT8_MISSING, FULL_MISSING)
add("an-empty-entry-is-accepted", MODEL_SRC, MODEL_TESTS,
    "    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:\n",
    "    if not stat.S_ISREG(info.st_mode):\n",
    ZERO_BYTE)
add("a-non-regular-entry-is-accepted", MODEL_SRC, MODEL_TESTS,
    "    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:\n",
    "    if info.st_size == 0:\n",
    NON_REGULAR)
add("an-lstat-error-reads-ok", MODEL_SRC, MODEL_TESTS,
    "    except OSError:\n"
    "        return \"unreadable\"\n",
    "    except OSError:\n"
    "        return \"ok\"\n",
    UNREADABLE)
add("a-hidden-entry-reads-ok", MODEL_SRC, MODEL_TESTS,
    '    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_HIDDEN:\n',
    "    if False:\n",
    HIDDEN)
add("the-unreadable-state-is-dropped", MODEL_SRC, MODEL_TESTS,
    "    if saw_unreadable:\n"
    "        return \"unreadable\"\n",
    "",
    UNREADABLE, HIDDEN)
add("an-unreadable-model-counts-as-complete", MODEL_SRC, MODEL_TESTS,
    '    return model_state(model_dir) == "complete"\n',
    '    return model_state(model_dir) != "incomplete"\n',
    UNREADABLE, HIDDEN)
add("a-failed-check-counts-as-complete", MODEL_SRC, MODEL_TESTS,
    '        logger.warning("Could not check the Parakeet model: %s", exc)\n'
    "        return False\n",
    '        logger.warning("Could not check the Parakeet model: %s", exc)\n'
    "        return True\n",
    CHECK_RAISES)

# --- the Logic check (main.py _switch_stt_provider) --------------------------

CHECK_CONDITION = (
    "        if (\n"
    "            provider == parakeet_model.PROVIDER_NAME\n"
    "            and not parakeet_model.parakeet_model_complete()\n"
    "        ):\n"
)

add("logic-checks-every-engine", MAIN_SRC, OFFER_TESTS,
    CHECK_CONDITION,
    "        if (\n"
    "            not parakeet_model.parakeet_model_complete()\n"
    "        ):\n",
    L_OTHER_ENGINE)
add("logic-checks-every-engine-but-parakeet", MAIN_SRC, OFFER_TESTS,
    CHECK_CONDITION,
    "        if (\n"
    "            provider != parakeet_model.PROVIDER_NAME\n"
    "            and not parakeet_model.parakeet_model_complete()\n"
    "        ):\n",
    L_INCOMPLETE, L_FALLBACK, L_EVERY_PICK, L_OTHER_ENGINE)
add("logic-offers-on-a-complete-model", MAIN_SRC, OFFER_TESTS,
    CHECK_CONDITION,
    "        if (\n"
    "            provider == parakeet_model.PROVIDER_NAME\n"
    "            and parakeet_model.parakeet_model_complete()\n"
    "        ):\n",
    L_INCOMPLETE, L_FALLBACK, L_COMPLETE, L_EVERY_PICK)
# The early return removed: the offer is sent, then the engine is stopped
# and the new one started anyway.
add("logic-offer-falls-through-to-the-switch", MAIN_SRC, OFFER_TESTS,
    "            _send_parakeet_model_offer(self.state_manager, plan)\n"
    "            return\n"
    "\n"
    "        if remote_launcher:\n",
    "            _send_parakeet_model_offer(self.state_manager, plan)\n"
    "\n"
    "        if remote_launcher:\n",
    L_INCOMPLETE, L_FALLBACK)

# --- the tray-menu query (stt/remote_stt_launcher.py) ------------------------

SCAN_SKIP = (
    '                if section.get("enabled", True) or section.get("template", False):\n'
)

add("menu-lists-a-complete-model", LAUNCHER_SRC, OFFER_TESTS,
    "            if parakeet_model.parakeet_model_complete(service_dir):\n"
    "                return []\n",
    "",
    Q_COMPLETE, Q_RECHECK)
add("menu-lists-an-enabled-parakeet", LAUNCHER_SRC, OFFER_TESTS,
    SCAN_SKIP,
    '                if section.get("template", False):\n',
    Q_ENABLED_DROPPED)
add("menu-lists-a-template", LAUNCHER_SRC, OFFER_TESTS,
    SCAN_SKIP,
    '                if section.get("enabled", True):\n',
    Q_TEMPLATE)
add("menu-takes-any-disabled-folder-for-parakeet", LAUNCHER_SRC, OFFER_TESTS,
    "                if section.get(\"name\") != parakeet_model.PROVIDER_NAME:\n"
    "                    continue\n",
    "",
    Q_OTHER_ENGINE)
add("menu-never-lists-the-entry", LAUNCHER_SRC, OFFER_TESTS,
    "            service_dir = self._find_disabled_parakeet_dir()\n"
    "            if service_dir is None:\n"
    "                return []\n",
    "            service_dir = self._find_disabled_parakeet_dir()\n"
    "            if service_dir is None or service_dir is not None:\n"
    "                return []\n",
    Q_BOSS, Q_NAMES, Q_RECHECK)

# --- provider PID files for the launch console (A7) --------------------------
# RemoteSTTLauncher._pid_file_names_live_provider: a live process named in a
# PID file counts only when it started no later than the file time plus the
# slack; on AccessDenied, only under a provider process name.

P_NEWER = "test_a_process_newer_than_the_pid_file_is_not_counted"
P_OLDER = "test_a_process_older_than_the_pid_file_is_counted"
P_SLACK = "test_a_process_started_just_after_the_file_time_is_counted"
# The runner reads a parametrized name without its id; under each mutation
# below that uses it, only the case named in the comment can fail.
P_NAME = "test_access_denied_counts_a_provider_process_name"
P_OTHER_PROGRAM = "test_access_denied_does_not_count_another_program"
P_NAME_UNREADABLE = "test_access_denied_and_an_unreadable_name_is_not_counted"
P_EXITED = "test_a_process_that_exits_before_the_check_is_skipped"
P_GONE = "test_a_process_gone_at_lookup_is_skipped"
P_OTHER_FAILURE = "test_any_other_failure_is_skipped_and_never_raised"

add("pidfile-slack-wide-enough-for-a-newer-process", LAUNCHER_SRC, OFFER_TESTS,
    "_PID_FILE_START_SLACK = 2.0\n",
    "_PID_FILE_START_SLACK = 5.0\n",
    P_NEWER)
add("pidfile-comparison-flipped", LAUNCHER_SRC, OFFER_TESTS,
    "            return started <= written + _PID_FILE_START_SLACK\n",
    "            return started >= written + _PID_FILE_START_SLACK\n",
    P_NEWER, P_OLDER)
add("pidfile-slack-removed", LAUNCHER_SRC, OFFER_TESTS,
    "            return started <= written + _PID_FILE_START_SLACK\n",
    "            return started <= written\n",
    P_SLACK)
add("pidfile-access-denied-counts-every-name", LAUNCHER_SRC, OFFER_TESTS,
    "                return process.name().lower() in _PROVIDER_PROCESS_NAMES\n",
    "                return True\n",
    P_OTHER_PROGRAM, P_NAME_UNREADABLE)
add("pidfile-access-denied-name-case-sensitive", LAUNCHER_SRC, OFFER_TESTS,
    "                return process.name().lower() in _PROVIDER_PROCESS_NAMES\n",
    "                return process.name() in _PROVIDER_PROCESS_NAMES\n",
    P_NAME)  # the pythonw-mixed-case case
add("pidfile-uv-name-dropped", LAUNCHER_SRC, OFFER_TESTS,
    '_PROVIDER_PROCESS_NAMES = frozenset({"python.exe", "pythonw.exe", "uv.exe"})\n',
    '_PROVIDER_PROCESS_NAMES = frozenset({"python.exe", "pythonw.exe"})\n',
    P_NAME)  # the uv case
add("pidfile-no-such-process-counted", LAUNCHER_SRC, OFFER_TESTS,
    "        except psutil.NoSuchProcess:\n"
    "            return False\n",
    "        except psutil.NoSuchProcess:\n"
    "            return True\n",
    P_EXITED, P_GONE)
add("pidfile-other-failure-counted", LAUNCHER_SRC, OFFER_TESTS,
    "        except psutil.NoSuchProcess:\n"
    "            return False\n"
    "        except Exception:  # noqa: BLE001\n"
    "            return False\n",
    "        except psutil.NoSuchProcess:\n"
    "            return False\n"
    "        except Exception:  # noqa: BLE001\n"
    "            return True\n",
    P_OTHER_FAILURE)
# The old check: any live process named in the file counts.
add("pidfile-helper-bypassed", LAUNCHER_SRC, OFFER_TESTS,
    "            if pid not in pids and self._pid_file_names_live_provider(pid, pid_file):\n",
    "            if pid not in pids and pid > 0 and psutil.pid_exists(pid):\n",
    P_NEWER, P_OTHER_PROGRAM, P_NAME_UNREADABLE, P_EXITED, P_GONE,
    P_OTHER_FAILURE)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
