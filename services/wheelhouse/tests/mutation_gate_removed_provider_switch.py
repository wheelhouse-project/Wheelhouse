"""Mutation gate for the move off a removed speech engine (wh-distil-whisper-removal).

Covers stt/removed_providers.py, its call in ServiceManager.start_remote_stt and
the discovery skip in stt/remote_stt_launcher.py:
the trigger condition (last_provider or the installer marker), the written
value, the marker clear, the notice-once rule, the stale-marker guard and the
save-failure paths.

Run with the isolated wheelhouse Python:

    python tests/mutation_gate_removed_provider_switch.py --check
    python tests/mutation_gate_removed_provider_switch.py > gate.log 2>&1; echo $?

--check proves every pattern matches exactly once and every Python mutant
compiles, without running a test. A full sweep uses scripts/run_tests.py and
requires a named AssertionError from the named catcher, never a collection or
runtime error. --only accepts mutation names. The gate reuses the tested
mutation build/restore machinery of the provider-environment gate.
"""
import argparse
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mutation_gate_pattern_catalog_nested_group as safety
from mutation_gate_owned_guard import MutationRun


SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
SOURCE = SERVICE / "stt/removed_providers.py"
MANAGER = SERVICE / "service_manager.py"
LAUNCHER = SERVICE / "stt/remote_stt_launcher.py"
PARAKEET_MODEL = SERVICE / "stt/parakeet_model.py"
TEST = "tests/test_removed_provider_migration.py"

MISSING = "test_missing_provider_folder_selects_parakeet_with_one_notice"
PRESENT = "test_present_provider_folder_selects_parakeet_with_one_notice"
MARKER = "test_installer_marker_alone_shows_the_notice_and_is_cleared"
SECOND = "test_a_second_start_in_the_same_run_shows_no_notice"
SAVE_FALSE = "test_a_save_failure_warns_and_parakeet_still_starts[save-returns-false]"
SAVE_RAISES = "test_a_save_failure_warns_and_parakeet_still_starts[save-raises]"
OTHER_UNRELATED = "test_other_settings_change_nothing_and_show_no_notice[unrelated-marker]"
GOOGLE = "test_a_google_choice_is_never_replaced"
STALE = "test_a_stale_marker_beside_another_engine_shows_no_false_notice"
NO_LAST = "test_a_marker_with_no_last_provider_shows_the_notice"
NON_TEXT = "test_a_hand_edited_non_text_value_does_not_stop_the_start[list]"
SET_FAILS = "test_settings_that_cannot_take_the_switch_still_start_parakeet"
LOOP_CLOSED = "test_a_loop_that_cannot_schedule_the_save_still_starts_parakeet"
SAVE_RESULT = "test_the_save_reports_whether_it_reached_the_disk[saved]"
SAVE_RESULT_REFUSED = "test_the_save_reports_whether_it_reached_the_disk[refused]"
DISCOVERY = "test_discovery_skips_a_removed_engine_folder_left_on_disk"
FALLBACK_NEVER = "test_the_fallback_never_starts_a_removed_engine_left_on_disk"
OUTCOME_A = "test_outcome_a_the_notice_comes_after_the_start_decision"
OUTCOME_B = "test_outcome_b_enabled_parakeet_with_the_model_missing"
OUTCOME_C = "test_outcome_c_names_the_engine_that_started"
OUTCOME_D = "test_outcome_d_when_no_engine_starts"
SENDER = "test_the_model_offer_sender_lives_in_the_shared_module_and_never_raises"


def mutation(name, old, new, catcher, assertion, file=SOURCE):
    return dict(name=name, file=file, old=old, new=new,
                catcher=catcher, assertion=assertion)


MUTATIONS = [
    # --- the trigger condition ------------------------------------------------
    mutation("last-provider-trigger-dropped",
             "    last_name = _removed_name(last_provider)\n",
             "    last_name = None\n",
             MISSING, "the removed engine is never launched"),
    mutation("marker-trigger-dropped",
             "    marker_name = _removed_name(marker)\n",
             "    marker_name = None\n",
             MARKER, "exactly one notice"),
    mutation("marker-guard-removed",
             "    marker_applies = marker_name is not None and last_provider in (None, DEFAULT_STT_PROVIDER)\n",
             "    marker_applies = marker_name is not None\n",
             STALE, "a stale marker must not announce"),
    mutation("unset-last-provider-not-the-default",
             "last_provider in (None, DEFAULT_STT_PROVIDER)",
             "last_provider == DEFAULT_STT_PROVIDER",
             NO_LAST, "exactly one notice"),
    mutation("non-text-guard-removed",
             "    if isinstance(value, str):\n        return REMOVED_PROVIDERS.get(value)\n",
             "    if True:\n        return REMOVED_PROVIDERS.get(value)\n",
             NON_TEXT, "a non-text value must not stop the start"),
    mutation("name-not-returned",
             "    return name\n",
             "    return None\n",
             MISSING, "exactly one notice"),
    # --- the written value and the marker clear -------------------------------
    mutation("last-provider-not-switched",
             "            config_service.set(LAST_PROVIDER_KEY, DEFAULT_STT_PROVIDER)\n",
             "            pass\n",
             MISSING, "the move must be recorded in the settings in memory"),
    mutation("last-provider-switched-to-the-wrong-engine",
             "            config_service.set(LAST_PROVIDER_KEY, DEFAULT_STT_PROVIDER)\n",
             '            config_service.set(LAST_PROVIDER_KEY, "google_stt")\n',
             MISSING, "the move must be recorded in the settings in memory"),
    mutation("switch-written-for-any-engine",
             "        if last_name is not None:\n            config_service.set(",
             "        if True:\n            config_service.set(",
             GOOGLE, "a shipped engine choice is never replaced"),
    mutation("marker-not-cleared",
             "            config_service.unset(MARKER_KEY)\n",
             "            pass\n",
             MARKER, "the marker is cleared"),
    mutation("any-marker-cleared",
             "        if marker_name is not None:\n            config_service.unset(",
             "        if True:\n            config_service.unset(",
             OTHER_UNRELATED, "settings that name no removed engine are left untouched"),
    mutation("settings-failure-uncontained",
             "    except Exception as exc:\n        # A user config can hold",
             "    except KeyError as exc:\n        # A user config can hold",
             SET_FAILS, "the start must survive a settings failure"),
    mutation("settings-failure-silent",
             '        log.warning("Could not record the move off %s in the settings: %s",\n'
             '                    last_name or marker_name, exc)\n',
             "        pass\n",
             SET_FAILS, "a failed record warns"),
    # --- the notice ----------------------------------------------------------
    mutation("notice-lacks-the-engine-name",
             '    removed = f"{display_name} was removed from Wheelhouse."\n',
             '    removed = f"The old engine was removed from Wheelhouse."\n',
             MISSING, "Distil-Whisper was removed"),
    mutation("notice-lacks-the-replacement",
             'return f"{removed} {_REPLACEMENT_NAME} is now in use as your speech engine."',
             'return f"{removed} Speech is off."',
             MISSING, "Parakeet is now in use"),
    mutation("notice-has-the-wrong-title",
             "            title=REMOVED_PROVIDER_NOTICE_TITLE, message=message,",
             '            title="Wheelhouse: Speech setup", message=message,',
             MISSING, "Speech setup"),
    mutation("notice-not-queued",
             "        notify_removed_provider(display_name, outcome, other_engine)\n",
             "        pass\n",
             MISSING, "exactly one notice", MANAGER),
    mutation("notice-queued-twice",
             "        notify_removed_provider(display_name, outcome, other_engine)\n",
             "        notify_removed_provider(display_name, outcome, other_engine)\n"
             "        notify_removed_provider(display_name, outcome, other_engine)\n",
             MISSING, "exactly one notice", MANAGER),
    mutation("notice-repeats-on-every-start",
             "        if not removed_engine:\n",
             "        if False:\n",
             SECOND, "the switch is kept in memory", MANAGER),
    # --- the save --------------------------------------------------------------
    mutation("save-not-scheduled",
             "            self.loop.create_task(save)\n",
             "            save.close()\n",
             MISSING, "the switch must reach the settings file", MANAGER),
    mutation("unschedulable-save-stops-the-start",
             "        except Exception as e:\n            save.close()",
             "        except KeyError as e:\n            save.close()",
             LOOP_CLOSED, "the start must survive a closed loop", MANAGER),
    mutation("failed-save-silent",
             "    if not saved:\n        log.warning(",
             "    if saved:\n        log.warning(",
             SAVE_FALSE, "one warning on a failed save"),
    mutation("save-exception-uncontained",
             "    except Exception as exc:\n        log.warning(\"Could not save the switch from",
             "    except KeyError as exc:\n        log.warning(\"Could not save the switch from",
             SAVE_RAISES, "one warning on a failed save"),
    mutation("save-always-reports-success",
             "    return bool(saved)\n",
             "    return True\n",
             SAVE_RESULT_REFUSED, "the result says whether the settings were saved"),
    # --- the call in start_remote_stt ------------------------------------------
    mutation("startup-migration-skipped",
             "        removed_engine = migrate_removed_provider(self.config_service)\n",
             "        removed_engine = None\n",
             MISSING, "the removed engine is never launched", MANAGER),
    mutation("startup-reads-the-unswitched-choice",
             "        last_provider = None if model_missing else DEFAULT_STT_PROVIDER\n",
             '        last_provider = None if model_missing else self.config_service.get("stt.last_provider", None)\n',
             SET_FAILS, "the start continues with Parakeet", MANAGER),
    mutation("startup-launches-the-removed-engine-when-present",
             "        last_provider = None if model_missing else DEFAULT_STT_PROVIDER\n",
             '        last_provider = None if model_missing else "distil_medium_en"\n',
             PRESENT, "an old folder left on disk is never launched", MANAGER),
    # --- the notice follows the start decision and names the engine that started
    # (review finding wh-distil-whisper-removal.1.1) -----------------------------
    mutation("notice-sent-before-the-start-decision",
             "        started = self._start_chosen_remote_stt(last_provider, exclude=DEFAULT_STT_PROVIDER)\n"
             "        other_engine = None\n"
             "        if model_missing:\n"
             "            outcome = OUTCOME_MODEL_MISSING\n"
             "            self._offer_parakeet_model()\n"
             "        elif started is None:\n"
             "            outcome = OUTCOME_NO_ENGINE_STARTED\n"
             "        elif started[0] == DEFAULT_STT_PROVIDER:\n"
             "            outcome = OUTCOME_REPLACEMENT_STARTED\n"
             "        else:\n"
             "            outcome = OUTCOME_OTHER_ENGINE_STARTED\n"
             "            other_engine = started[1]\n"
             "        self._announce_removed_provider(removed_engine, outcome, other_engine)\n",
             "        self._announce_removed_provider(removed_engine, OUTCOME_REPLACEMENT_STARTED)\n"
             "        started = self._start_chosen_remote_stt(last_provider, exclude=DEFAULT_STT_PROVIDER)\n",
             OUTCOME_A, "the notice must not be sent before the engine start is decided", MANAGER),
    mutation("model-check-skipped",
             "        model_missing = not parakeet_model.parakeet_model_complete()\n",
             "        model_missing = False\n",
             OUTCOME_B, "Parakeet without its model is not started", MANAGER),
    mutation("replacement-tried-without-its-model",
             "        last_provider = None if model_missing else DEFAULT_STT_PROVIDER\n",
             "        last_provider = DEFAULT_STT_PROVIDER\n",
             OUTCOME_B, "Parakeet without its model is not started", MANAGER),
    mutation("model-offer-not-sent",
             "            outcome = OUTCOME_MODEL_MISSING\n            self._offer_parakeet_model()\n",
             "            outcome = OUTCOME_MODEL_MISSING\n",
             OUTCOME_B, "the model offer is sent to the GUI", MANAGER),
    mutation("model-offer-sent-for-a-complete-model",
             "        if model_missing:\n"
             "            outcome = OUTCOME_MODEL_MISSING\n"
             "            self._offer_parakeet_model()\n",
             "        self._offer_parakeet_model()\n"
             "        if model_missing:\n"
             "            outcome = OUTCOME_MODEL_MISSING\n",
             OUTCOME_A, "a complete model needs no offer", MANAGER),
    mutation("wrong-engine-name-in-outcome-c",
             "            other_engine = started[1]\n",
             "            other_engine = started[0]\n",
             OUTCOME_C, "the notice text names the outcome (TEXT_C)", MANAGER),
    mutation("failed-replacement-retried-in-the-fallback",
             '        provider_names = [p["name"] for p in providers if p["name"] != exclude]\n',
             '        provider_names = [p["name"] for p in providers]\n',
             OUTCOME_C, "the other engine starts after the replacement fails", MANAGER),
    mutation("another-engine-reported-as-the-replacement",
             "        elif started[0] == DEFAULT_STT_PROVIDER:\n",
             "        elif True:\n",
             OUTCOME_C, "the notice text names the outcome (TEXT_C)", MANAGER),
    mutation("no-engine-reported-as-the-replacement",
             "            outcome = OUTCOME_NO_ENGINE_STARTED\n",
             "            outcome = OUTCOME_REPLACEMENT_STARTED\n",
             OUTCOME_D, "the notice text names the outcome (TEXT_D)", MANAGER),
    mutation("missing-model-text-says-it-is-installed",
             '                "model is not installed.")\n',
             '                "model is installed.")\n',
             OUTCOME_B, "the notice text names the outcome (TEXT_B)"),
    mutation("model-offer-sender-sends-the-wrong-kind",
             '        message = {"action": "parakeet_model_offer", "kind": "launch"}\n',
             '        message = {"action": "parakeet_model_offer", "kind": "fallback"}\n',
             SENDER, "the sender puts each offer on the queue", PARAKEET_MODEL),
    mutation("model-offer-sender-raises",
             "    except Exception as exc:  # noqa: BLE001 -- an offer must not stop its caller\n",
             "    except KeyError as exc:  # noqa: BLE001 -- an offer must not stop its caller\n",
             SENDER, "the sender must not raise", PARAKEET_MODEL),
    # --- discovery never offers a removed engine (commit 3) -------------------
    mutation("discovery-offers-a-removed-engine",
             "                if name in REMOVED_PROVIDERS:\n",
             "                if name in ():\n",
             DISCOVERY, "the provider list never offers a removed engine", LAUNCHER),
    mutation("fallback-can-start-a-removed-engine",
             "                if name in REMOVED_PROVIDERS:\n",
             "                if name in ():\n",
             FALLBACK_NEVER, "the fallback never starts a removed engine", LAUNCHER),
]


def clear_bytecode():
    # Only compiled copies of the mutated modules, not every cache in the service.
    for source in (SOURCE, MANAGER, LAUNCHER, PARAKEET_MODEL):
        for pyc in (source.parent / "__pycache__").glob(source.stem + ".*.pyc"):
            pyc.unlink(missing_ok=True)


def read_cases(report):
    root = ET.parse(report).getroot()
    return {case.attrib["name"]: case for case in root.iter("testcase")}


def is_assertion_failure(failure):
    # Pytest sometimes omits the exception name for rewritten assertions.
    # Anchor at the start so an unrelated exception mentioning it is refused.
    return failure.attrib.get("message", "").startswith(("AssertionError", "assert "))


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--only", nargs="+", choices=[m["name"] for m in MUTATIONS])
    args = parser.parse_args()
    selected = [m for m in MUTATIONS if not args.only or m["name"] in args.only]
    safety.MUTATIONS = MUTATIONS
    safety._clear_bytecode = clear_bytecode
    if args.check:
        return safety.check_only(selected)
    print(f"Full set {len(MUTATIONS)}; selected {len(selected)}; skipped {len(MUTATIONS)-len(selected)}", flush=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", UV_OFFLINE="1", UV_NO_SYNC="1", COLUMNS="1000")
    guard = MutationRun(ROOT, [item["file"] for item in selected])
    with guard.protect(safety, ("_restore", "_clear_bytecode")), tempfile.TemporaryDirectory(prefix="wh-removed-provider-gate-") as directory:
        report = Path(directory) / "results.xml"

        def run(*extra):
            report.unlink(missing_ok=True)
            return guard.run(
                [sys.executable, str(ROOT / "scripts/run_tests.py"), *extra,
                 "-p", "no:randomly", "--junitxml", str(report)],
                cwd=ROOT, env=env, timeout=120,
            )

        safety._pytest = run
        safety.TEST_FILE = TEST
        clear_bytecode()
        baseline = run(TEST)
        if baseline.returncode or not report.exists():
            print("ERROR baseline", baseline.stdout, baseline.stderr)
            return 1
        cases = read_cases(report)
        missing = [m["catcher"] for m in selected
                   if m["catcher"] not in cases or cases[m["catcher"]].find("skipped") is not None]
        if missing:
            print("ERROR missing or skipped catcher", sorted(set(missing)))
            return 1
        print(f"Baseline green; {len(cases)} collected tests; all catchers present", flush=True)
        caught = survived = errors = 0
        for item in selected:
            mutant, original, error = safety._build_mutant(item)
            if error:
                errors += 1
                print("ERROR", error, flush=True)
                continue
            safety.PYTEST_ARGS = ["-k", item["catcher"].split("[")[0]]
            result, error = safety._apply_and_run(item["file"], mutant, original)
            if item["file"].read_bytes() != original:
                print("ERROR original source not restored; stopping", flush=True)
                return 1
            if error or result is None or result.returncode not in (0, 1) or not report.exists():
                errors += 1
                print("ERROR", item["name"], error or "runner failed", flush=True)
                continue
            cases = read_cases(report)
            failures = {name: case.find("failure") for name, case in cases.items() if case.find("failure") is not None}
            if (any(case.find("error") is not None for case in cases.values()) or
                    any(not is_assertion_failure(failure) for failure in failures.values())):
                errors += 1
                print("ERROR", item["name"], "unrelated runtime/setup failure", result.stdout, flush=True)
                continue
            failure = failures.get(item["catcher"])
            if failure is None or item["assertion"] not in (failure.text or ""):
                survived += 1
                print("SURVIVED", item["name"], result.stdout, flush=True)
            else:
                caught += 1
                print("CAUGHT", item["name"], "by", item["catcher"],
                      failure.attrib.get("message", "").splitlines()[0], flush=True)
        print(f"Scope {len(selected)} of {len(MUTATIONS)}; caught {caught}, survivors {survived}, errors {errors}", flush=True)
        return int(bool(survived or errors))


if __name__ == "__main__":
    raise SystemExit(main())
