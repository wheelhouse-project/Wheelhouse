"""Mutation gate for the automatic sound-pause decision (wh-audio-suppression-auto).

Every test this branch added claims to guard one defect. This gate breaks
each guarded behaviour in turn and requires the named test or tests to fail.
A mutation nothing catches is a finding against the test, not against the gate.

WHY THIS GATE HAS ITS OWN LAUNCHER
----------------------------------
The shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
launches every suite through scripts/run_tests.py, which runs ``uv run pytest``
and creates a config.toml from the template when none exists. Neither is
allowed here, and one UV_PROJECT_ENVIRONMENT cannot serve three services at
once. So this gate keeps the runner's pattern checks, byte-exact restoration
and interrupt handling, and replaces only the layer that starts pytest:
``_run_pytest``, ``_collected_names``, ``_failed_names`` and ``_clear_pycache``.

The branch's guard tests live in nine selections across three services, and the
runner runs one selection per mutation, so a behaviour guarded in two services
(a wake word during a sound pause) is two mutations with the same replacement.

THREE FALSE VERDICTS THIS LAUNCHER REFUSES TO REPORT
----------------------------------------------------
* A pytest ERROR is not a failure. A test that errors proves nothing about the
  mutant, and the runner's failed-name set would not hold it, so the mutation
  would read as a survivor. ``run_tests`` raises instead, and the runner
  reports an error.
* A verdict read outside pytest's short summary is not a verdict. A captured
  log record can begin with FAILED, so only the lines after the short-summary
  header are read.
* A mutation that breaks a selection's guard tests has proved nothing either:
  the named test failed because the menu no longer builds, not because the
  entry came back. MUST_STAY_GREEN names those guards per selection and
  ``run_tests`` raises when one of them fails.

INTERPRETERS
------------
A worktree has no virtual environments of its own, so each service's
interpreter is taken from this checkout when it has one and from the main
checkout otherwise. The shared service's environment installs shared_stt from
the main checkout, so its runs carry PYTHONPATH pointing at the shared package
of THIS checkout; without it the suite would test the code the branch did not
change.

Usage (from the worktree root, any interpreter):
    python services/wheelhouse/tests/mutation_gate_audio_suppression_auto.py --check
    python services/wheelhouse/tests/mutation_gate_audio_suppression_auto.py
    python ... mutation_gate_audio_suppression_auto.py the-template-ships-true
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3]
WHEELHOUSE = WORKTREE / "services/wheelhouse"
SHARED = WORKTREE / "services/stt_providers/shared"

spec = importlib.util.spec_from_file_location(
    "audio_suppression_shared_gate",
    WORKTREE / "services/stt_providers/shared/tests/mutation_gate_runner.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# The launch layer
# ---------------------------------------------------------------------------

def _main_checkout() -> Path:
    """The checkout that owns the virtual environments.

    A worktree's .git is a file naming <main>/.git/worktrees/<name>; a normal
    checkout's is a directory, and then this checkout is the main one.
    """
    git = WORKTREE / ".git"
    if git.is_dir():
        return WORKTREE
    gitdir = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    return gitdir.parents[2]


MAIN = _main_checkout()


def _interpreter(service: Path) -> Path:
    """The python.exe that owns a service's dependencies."""
    local = service / ".venv/Scripts/python.exe"
    if local.exists():
        return local
    return MAIN / service.relative_to(WORKTREE) / ".venv/Scripts/python.exe"


# service -> (cwd for pytest, the service whose environment runs it)
SUITES = {
    WHEELHOUSE: (WHEELHOUSE, WHEELHOUSE),
    SHARED: (SHARED, SHARED),
    # The repository-root integration suite imports the wheelhouse service.
    WORKTREE: (WORKTREE, WHEELHOUSE),
}

# Tests that must keep passing while a mutation of their selection runs. A
# failure here means the mutant broke the fixture, not that the guard caught
# it, so the run is reported as an error rather than as a catch.
MUST_STAY_GREEN = {
    "tests/test_audio_suppression_decision.py": (
        "test_the_setting_accepts_strings_in_any_case",
        "test_the_reference_entry_describes_three_values",
    ),
    ("tests/test_gui.py", "-k", "TestRemovedMenuItems"): (
        "test_the_tray_menu_still_builds_its_gated_items",
        "test_the_button_menu_still_builds_its_gated_items",
    ),
    "tests/test_retraction_policy.py": (
        "test_declared_true_reaches_the_state_manager",
    ),
    "tests/test_audio_suppression_control.py": (
        "test_a_pause_sends_no_notification",
    ),
    "tests/integration/test_wake_word_flow.py": (
        "test_idle_recovery_mode_activates_for_idle",
    ),
    "tests/test_wake_word_detector.py": (
        "test_idle_recovery_arms_on_idle",
    ),
}

RUN_TIMEOUT = 300
_SUMMARY_HEADER = "short test summary info"


def _bare_name(text: str) -> str:
    """A collected or reported test id, without its path and parameter list."""
    return re.sub(r"\[.*\]$", "", text.split("::")[-1].split()[0])


def _summary(output: str) -> str:
    return output.split(_SUMMARY_HEADER, 1)[1] if _SUMMARY_HEADER in output else ""


def failed_names(output: str) -> set[str]:
    """The tests pytest's short summary reports as FAILED, and only those."""
    return {
        _bare_name(line[len("FAILED "):])
        for line in _summary(output).splitlines()
        if line.startswith("FAILED ")
    }


def run_tests(service: Path, test_file, collect: bool = False):
    cwd, owner = SUITES[service]
    # COLUMNS: pytest truncates a short-summary line to the terminal width,
    # and at the 80-column default these test ids leave no room for the
    # message -- the one part of the line that says whether the mutant
    # reached the assertion or crashed before it.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="220")
    if owner == SHARED:
        # The shared environment installs shared_stt from the main checkout.
        env["PYTHONPATH"] = str(SHARED)
    command = [
        str(_interpreter(owner)), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:randomly",
    ]
    command += ["--collect-only", "-q"] if collect else ["-rf", "--tb=short"]
    result = subprocess.run(
        command, cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=RUN_TIMEOUT,
    )
    output = result.stdout + result.stderr
    if collect:
        if result.returncode:
            raise RuntimeError("collection failed: " + output[-2000:])
        return result
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"pytest exited {result.returncode}: " + output[-2000:])
    errored = [line for line in _summary(output).splitlines()
               if line.startswith("ERROR ")]
    if errored:
        raise RuntimeError("pytest reported an ERROR, which is no verdict: "
                           + "; ".join(errored[:3]))
    broken_guards = sorted(
        failed_names(output) & set(MUST_STAY_GREEN.get(test_file, ()))
    )
    if broken_guards:
        raise RuntimeError(
            f"guard tests failed, so this mutant proves nothing: {broken_guards}")
    # Print why each test failed. A mutant that crashes upstream of the
    # assertion reads as a catch and proves nothing, and the reason is the
    # only thing in the run that tells the two apart (mutation-gate skill).
    for line in _summary(output).splitlines():
        if line.startswith("FAILED "):
            print("  " + line[len("FAILED "):][:200], flush=True)
    return result


def collected_names(service: Path, test_file) -> set[str]:
    """Every test the selection collects, and the guard names checked once."""
    result = run_tests(service, test_file, collect=True)
    names = {_bare_name(line) for line in result.stdout.splitlines()
             if "::" in line}
    missing = sorted(set(MUST_STAY_GREEN.get(test_file, ())) - names)
    if missing:
        raise RuntimeError(f"guard tests do not exist in {test_file}: {missing}")
    return names


def clear_pycache(service: Path) -> None:
    """The runner's cache clearing, without walking a whole checkout.

    The repository-root selection makes the worktree itself the service, and
    an unpruned walk of it reads every virtual environment and .git object.
    """
    skip = {".venv", ".git", "node_modules", ".pytest_cache", "vendor"}
    for parent, directories, _ in os.walk(service):
        directories[:] = [d for d in directories if d not in skip]
        if "__pycache__" in directories:
            directories.remove("__pycache__")
            shutil.rmtree(Path(parent) / "__pycache__", ignore_errors=True)


runner._run_pytest = run_tests
runner._collected_names = collected_names
runner._failed_names = failed_names
runner._clear_pycache = clear_pycache


# ---------------------------------------------------------------------------
# The selections
# ---------------------------------------------------------------------------

DECISION_TESTS = (WHEELHOUSE, "tests/test_audio_suppression_decision.py")
STARTUP_TESTS = (WHEELHOUSE, "tests/test_audio_suppression_startup.py")
MENU_TESTS = (WHEELHOUSE, ("tests/test_gui.py", "-k", "TestRemovedMenuItems"))
REMOVED_TESTS = (WHEELHOUSE, "tests/test_audio_suppression_controls_removed.py")
RETRACTION_TESTS = (WHEELHOUSE, "tests/test_retraction_policy.py")
CONTROL_TESTS = (WHEELHOUSE, "tests/test_audio_suppression_control.py")
FLOW_TESTS = (WORKTREE, "tests/integration/test_wake_word_flow.py")
DETECTOR_TESTS = (SHARED, "tests/test_wake_word_detector.py")
HELP_TESTS = (WHEELHOUSE, "tests/test_audio_suppression_help.py")

DECISION = WHEELHOUSE / "utils/audio_suppression_decision.py"
STATE_MANAGER = WHEELHOUSE / "state_manager.py"
MAIN_PY = WHEELHOUSE / "main.py"
GUI = WHEELHOUSE / "gui.py"
WEBSOCKET = WHEELHOUSE / "integrations/websocket_manager.py"
TEMPLATE = WHEELHOUSE / "config.toml.example"
CATALOG = WHEELHOUSE / "speech/action_catalog.py"
PATTERNS = WHEELHOUSE / "speech/config/patterns.toml"
DESCRIPTIONS = WHEELHOUSE / "knowledge/helpdoc/config_descriptions.toml"
INTERACTION_MODES = (
    WHEELHOUSE / "knowledge/helpdoc/sections/090-interaction-modes.md"
)
DETECTOR = SHARED / "shared_stt/wake_word_detector.py"


def mutation(name, selection, file, old, new, expect):
    service, test_file = selection
    return dict(name=name, service=service, test_file=test_file, file=file,
                old=old, new=new, expect=list(expect))


MUTATIONS = [
    # -- The decision itself (plan items 1-13) ------------------------------
    mutation(
        "the-echo-canceller-branch-is-inverted", DECISION_TESTS, DECISION,
        """\
    if report.has_echo_canceller:
        logger.info("Audio suppression off: Windows reports %s", report.describe())
""",
        """\
    if not report.has_echo_canceller:
        logger.info("Audio suppression off: Windows reports %s", report.describe())
""",
        ["test_auto_with_an_echo_canceller_turns_suppression_off",
         "test_auto_without_an_echo_canceller_turns_suppression_on"]),
    mutation(
        "a-failed-query-turns-suppression-off", DECISION_TESTS, DECISION,
        """\
            _failure_detail(error),
        )
        return True
""",
        """\
            _failure_detail(error),
        )
        return False
""",
        ["test_a_query_failure_turns_suppression_on_with_one_warning"]),
    mutation(
        "a-failed-query-warns-about-nothing", DECISION_TESTS, DECISION,
        """\
        logger.warning(
            "Audio suppression on: could not ask Windows whether the microphone "
            "has an echo canceller (%s)",
            _failure_detail(error),
        )
        return True
""",
        """\
        return True
""",
        ["test_a_query_failure_turns_suppression_on_with_one_warning"]),
    mutation(
        "the-info-line-drops-the-device-name", DECISION_TESTS, DECISION,
        """\
            return f"{phrase} for {self.roles[0].device_name}"
""",
        """\
            return phrase
""",
        ["test_the_decision_logs_one_info_line_with_the_device_name"]),
    mutation(
        "a-forced-value-still-asks-windows", DECISION_TESTS, DECISION,
        """\
    if forced is True:
        logger.info("Audio suppression on: %s is true", AUDIO_SUPPRESSION_SETTING)
        return True
    if forced is False:
        logger.info("Audio suppression off: %s is false", AUDIO_SUPPRESSION_SETTING)
        return False
""",
        """\
    if forced is True:
        logger.info("Audio suppression on: %s is true", AUDIO_SUPPRESSION_SETTING)
    if forced is False:
        logger.info("Audio suppression off: %s is false", AUDIO_SUPPRESSION_SETTING)
""",
        ["test_true_forces_suppression_on_without_asking_windows",
         "test_false_forces_suppression_off_without_asking_windows"]),
    # Boss ruling, 2026-09-17 12:58: the two forced values swap their answers.
    mutation(
        "a-forced-true-turns-suppression-off", DECISION_TESTS, DECISION,
        """\
    if forced is True:
        logger.info("Audio suppression on: %s is true", AUDIO_SUPPRESSION_SETTING)
        return True
""",
        """\
    if forced is True:
        logger.info("Audio suppression on: %s is true", AUDIO_SUPPRESSION_SETTING)
        return False
""",
        ["test_true_forces_suppression_on_without_asking_windows"]),
    mutation(
        "a-forced-false-turns-suppression-on", DECISION_TESTS, DECISION,
        """\
    if forced is False:
        logger.info("Audio suppression off: %s is false", AUDIO_SUPPRESSION_SETTING)
        return False
""",
        """\
    if forced is False:
        logger.info("Audio suppression off: %s is false", AUDIO_SUPPRESSION_SETTING)
        return True
""",
        ["test_false_forces_suppression_off_without_asking_windows"]),
    mutation(
        "an-invalid-value-is-treated-as-true", DECISION_TESTS, DECISION,
        """\
        value,
    )
    return None
""",
        """\
        value,
    )
    return True
""",
        ["test_an_invalid_value_warns_once_and_asks_windows"]),
    mutation(
        "the-shipped-default-is-true", DECISION_TESTS, DECISION,
        'AUDIO_SUPPRESSION_DEFAULT = "auto"\n',
        'AUDIO_SUPPRESSION_DEFAULT = True\n',
        ["test_the_default_is_auto", "test_the_template_ships_auto"]),
    mutation(
        "the-query-asks-for-the-speech-category", DECISION_TESTS, DECISION,
        "            device_id, MediaCategory.COMMUNICATIONS, AudioProcessing.DEFAULT\n",
        "            device_id, MediaCategory.SPEECH, AudioProcessing.DEFAULT\n",
        ["test_the_query_asks_windows_for_communications_capture_effects"]),
    mutation(
        "the-query-asks-for-raw-processing", DECISION_TESTS, DECISION,
        "            device_id, MediaCategory.COMMUNICATIONS, AudioProcessing.DEFAULT\n",
        "            device_id, MediaCategory.COMMUNICATIONS, AudioProcessing.RAW\n",
        ["test_the_query_asks_windows_for_communications_capture_effects"]),
    mutation(
        "the-empty-device-id-is-accepted", DECISION_TESTS, DECISION,
        """\
        if not device_id:
            raise WinRTError(
                f"Windows names no default capture device for the {role_name} role"
            )
""",
        """\
        pass
""",
        ["test_an_empty_default_capture_id_is_a_failure"]),
    mutation(
        "every-effect-must-be-the-canceller", DECISION_TESTS, DECISION,
        "        has_canceller = any(\n",
        "        has_canceller = all(\n",
        ["test_the_query_asks_windows_for_communications_capture_effects"]),
    mutation(
        "the-query-runs-on-the-loop-thread", DECISION_TESTS, DECISION,
        """\
    threading.Thread(
        target=work, name=QUERY_THREAD_NAME, daemon=True
    ).start()
""",
        """\
    work()
""",
        ["test_the_query_runs_on_a_daemon_thread"]),
    mutation(
        "deliver-ignores-a-finished-future", DECISION_TESTS, DECISION,
        """\
        if future.done():
            return
        if error is not None:
""",
        """\
        if error is not None:
""",
        ["test_a_query_that_finishes_after_the_timeout_changes_nothing"]),

    # -- The three reads and the stored decision (plan items 14-16) ---------
    mutation(
        "speech-enabled-reads-the-config-key", DECISION_TESTS, STATE_MANAGER,
        "            self._audio_suppression_active and\n",
        '            self.config_service.get("ENABLE_AUDIO_SUPPRESSION", True) and\n',
        ["test_speech_enabled_follows_the_decision_not_the_setting"]),
    mutation(
        "the-ptt-refusal-reads-the-config-key", DECISION_TESTS, STATE_MANAGER,
        "                and self._audio_suppression_active):\n",
        '                and self.config_service.get("ENABLE_AUDIO_SUPPRESSION", True)):\n',
        ["test_the_ptt_refusal_reason_names_audio_only_when_the_decision_is_on"]),
    mutation(
        "the-ptt-stop-recheck-reads-the-config-key", DECISION_TESTS, STATE_MANAGER,
        """\
            and self._audio_suppression_active
        )
""",
        """\
            and self.config_service.get("ENABLE_AUDIO_SUPPRESSION", True)
        )
""",
        ["test_ptt_stop_uses_the_decision"]),
    mutation(
        "the-stored-decision-starts-off", DECISION_TESTS, STATE_MANAGER,
        "        self._audio_suppression_active = True\n",
        "        self._audio_suppression_active = False\n",
        ["test_speech_enabled_follows_the_decision_not_the_setting"]),
    mutation(
        "the-decision-is-applied-as-true-whatever-is-passed",
        DECISION_TESTS, STATE_MANAGER,
        "        self._audio_suppression_active = bool(active)\n",
        "        self._audio_suppression_active = True\n",
        ["test_speech_enabled_follows_the_decision_not_the_setting"]),

    # -- Startup (plan items 17-18) ----------------------------------------
    mutation(
        "startup-applies-the-decision-after-the-services-start",
        STARTUP_TESTS, MAIN_PY,
        """\
            self.state_manager.apply_audio_suppression_decision(
                audio_suppression_active
            )

            # Consolidate all essential background tasks
            service_tasks = self.service_manager.start_services()
""",
        """\
            # Consolidate all essential background tasks
            service_tasks = self.service_manager.start_services()
            self.state_manager.apply_audio_suppression_decision(
                audio_suppression_active
            )
""",
        ["test_startup_applies_the_decision_before_services_start"]),
    mutation(
        "startup-never-applies-the-decision", STARTUP_TESTS, MAIN_PY,
        """\
            self.state_manager.apply_audio_suppression_decision(
                audio_suppression_active
            )
""",
        """\
            pass
""",
        ["test_startup_applies_the_decision_before_services_start"]),
    mutation(
        "startup-passes-true-in-place-of-auto", STARTUP_TESTS, MAIN_PY,
        "                    AUDIO_SUPPRESSION_SETTING, AUDIO_SUPPRESSION_DEFAULT\n",
        "                    AUDIO_SUPPRESSION_SETTING, True\n",
        ["test_startup_passes_the_setting_with_the_auto_default"]),

    # -- The shipped template (plan items 19-20) ---------------------------
    mutation(
        "the-template-ships-true", DECISION_TESTS, TEMPLATE,
        'ENABLE_AUDIO_SUPPRESSION = "auto"\n',
        'ENABLE_AUDIO_SUPPRESSION = true\n',
        ["test_the_template_ships_auto"]),
    mutation(
        "the-audio-key-loses-its-comment-block", DECISION_TESTS, TEMPLATE,
        """\
# Sound this computer plays can reach the microphone and be typed as speech.
# "auto" asks Windows once, at startup, whether the default microphone has an
# echo canceller.
# With an echo canceller, sound from this computer does not pause listening.
# Without one, listening pauses while the default output device plays sound.
# That includes a screen reader.
# If Windows cannot answer, listening pauses as it does without an echo
# canceller.
# Set true to pause even with an echo canceller, or false to never pause.
# Listening resumes about 3 to 13 seconds after the sound stops.
# A push-to-talk hold that mutes the speakers resumes listening for that hold.
# Switching speech on also resumes listening, until the next time sound starts.
# A microphone plugged in or chosen after startup is checked at the next start.
ENABLE_AUDIO_SUPPRESSION = "auto"
""",
        """\
ENABLE_AUDIO_SUPPRESSION = "auto"
""",
        ["test_each_suppression_key_has_a_comment_block_directly_above_it"]),
    mutation(
        "the-sonos-key-loses-its-comment-block", DECISION_TESTS, TEMPLATE,
        """\
# Pause listening while a Sonos speaker plays music from a streaming service.
# This needs the Sonos plugin: set enabled = true under [plugins.sonos].
# Sound from a Sonos line-in or TV input does not pause listening.
# Listening resumes at the next check after the music stops.
# The check runs every polling_interval seconds, 2 in this file.
# Listening also resumes when WheelHouse cannot reach the speaker.
# Switching speech on resumes listening only until the next check finds music.
# The wake word and a push-to-talk hold do not end this pause.
# An echo canceller cannot remove this music, because it never passes through
# this computer.
ENABLE_SONOS_SUPPRESSION = true
""",
        """\
ENABLE_SONOS_SUPPRESSION = true
""",
        ["test_each_suppression_key_has_a_comment_block_directly_above_it",
         "test_the_sonos_and_idle_blocks_say_why_they_matter_with_an_echo_canceller"]),
    mutation(
        "the-idle-key-loses-its-comment-block", DECISION_TESTS, TEMPLATE,
        """\
# Pause listening when nobody uses the keyboard or mouse for a while.
# The wait is idle_timeout_minutes under [plugins.idle_monitor], 10 minutes in
# this file.
# Listening resumes within about 4 seconds of keyboard or mouse use.
# Saying the wake word, holding push-to-talk, or switching speech on also
# resumes it.
# An echo canceller removes only this computer's own sound.
# In an empty room, this pause stops a TV or other voices from being typed.
ENABLE_IDLE_SUPPRESSION = true
""",
        """\
ENABLE_IDLE_SUPPRESSION = true
""",
        ["test_each_suppression_key_has_a_comment_block_directly_above_it",
         "test_the_sonos_and_idle_blocks_say_why_they_matter_with_an_echo_canceller"]),

    # -- The removed switch comes back, one piece each (plan item 21) ------
    mutation(
        "the-tray-menu-item-comes-back", MENU_TESTS, GUI,
        """\
                pystray.MenuItem(
                    "Interim Results",
""",
        """\
                pystray.MenuItem(
                    "Audio Suppression",
                    self.toggle_interim_results,
                    checked=lambda item: interim_results_checked,
                    enabled=is_ready
                ),
                pystray.MenuItem(
                    "Interim Results",
""",
        ["test_the_tray_menu_has_no_audio_suppression"]),
    mutation(
        "the-button-menu-item-and-its-hover-text-come-back", MENU_TESTS, GUI,
        '            interim_action = QAction("Interim Results", self)\n',
        """\
            audio_action = QAction("Audio Suppression", self)
            audio_action.setCheckable(True)
            audio_action.setEnabled(is_ready)
            audio_action.setToolTip(
                "Pause listening while this computer plays sound."
            )
            menu.addAction(audio_action)

            interim_action = QAction("Interim Results", self)
""",
        ["test_the_button_menu_has_no_audio_suppression",
         "test_the_button_menu_has_no_sound_pause_hover_text"]),
    mutation(
        "the-logic-handler-map-takes-the-setter-back", REMOVED_TESTS, MAIN_PY,
        '            "toggle_button_visibility": self.state_manager.toggle_button_visibility,\n',
        """\
            "toggle_button_visibility": self.state_manager.toggle_button_visibility,
            "set_audio_suppression_enabled": self.state_manager.toggle_button_visibility,
""",
        ["test_the_logic_handler_map_has_no_audio_suppression_setter"]),
    mutation(
        "the-gui-stages-an-audio-suppression-write", REMOVED_TESTS, GUI,
        "        if command.get('action') in ('set_config_value', 'set_config_values'):\n",
        "        if command.get('action') in ('set_config_value', 'set_config_values',\n"
        "                                     'set_audio_suppression_enabled'):\n",
        ["test_the_gui_does_not_stage_an_audio_suppression_write"]),
    mutation(
        "an-audio-suppression-notice-comes-back", REMOVED_TESTS, STATE_MANAGER,
        'SPEECH_OFF_NOTICE = "Speech is off. You switched it off."\n',
        'SPEECH_OFF_NOTICE = "Speech is off. You switched it off."\n'
        'AUDIO_SUPPRESSION_OFF_NOTICE = "Sound no longer pauses listening."\n',
        ["test_the_state_manager_has_no_audio_suppression_notices"]),
    mutation(
        "the-catalog-takes-an-audio-suppression-action-back",
        REMOVED_TESTS, CATALOG,
        """\
    {
        "name": "set_speech_interaction_mode",
""",
        """\
    {
        "name": "audio_suppression_off",
        "label": "Audio suppression off",
        "summary": "Stops sound from this computer pausing listening.",
        "params": [],
        "example": 'Trigger "^audio suppression off$": saying it ends the pause.',
        "audience": "internal",
        "group": "Wheelhouse",
    },
    {
        "name": "set_speech_interaction_mode",
""",
        ["test_the_catalog_has_no_audio_suppression_actions"]),
    mutation(
        "a-shipped-pattern-calls-an-audio-suppression-action",
        REMOVED_TESTS, PATTERNS,
        """\
[[pattern]]
pattern = '''^click to talk mode$'''
""",
        """\
[[pattern]]
pattern = '''^audio suppression off$'''
doc_id = "audio-suppression-off"
whole_utterance_only = true
actions = [
    { function = "audio_suppression_off", params = [] }
]

[[pattern]]
pattern = '''^click to talk mode$'''
""",
        ["test_no_shipped_pattern_calls_an_audio_suppression_action"]),

    # -- The recovery window comes back, one piece each (plan item 22) -----
    mutation(
        "a-wake-word-ends-the-sound-pause", CONTROL_TESTS, STATE_MANAGER,
        """\
            self.send_state_update()
        else:
            logger.info(f"Wake word '{event.keyword}' detected but not idle-suppressed - ignoring")
""",
        """\
            self.send_state_update()
        elif (
            self._speech_suppressed_by_audio
            and self._audio_suppression_active
        ):
            self._speech_suppressed_by_audio = False
            self.send_state_update()
        else:
            logger.info(f"Wake word '{event.keyword}' detected but not idle-suppressed - ignoring")
""",
        ["test_a_wake_word_during_a_sound_pause_leaves_listening_off"]),
    mutation(
        "a-wake-word-ends-the-sound-pause-in-the-flow", FLOW_TESTS, STATE_MANAGER,
        """\
            self.send_state_update()
        else:
            logger.info(f"Wake word '{event.keyword}' detected but not idle-suppressed - ignoring")
""",
        """\
            self.send_state_update()
        elif (
            self._speech_suppressed_by_audio
            and self._audio_suppression_active
        ):
            self._speech_suppressed_by_audio = False
            self.send_state_update()
        else:
            logger.info(f"Wake word '{event.keyword}' detected but not idle-suppressed - ignoring")
""",
        ["test_wake_word_does_not_clear_audio_suppression"]),
    mutation(
        "the-stable-transcript-gate-comes-back", RETRACTION_TESTS, WEBSOCKET,
        "                        # Extract delta (new words since last stable)\n",
        """\
                        if getattr(
                            self.state_manager, "audio_recovery_window_open", False
                        ) is True:
                            continue

                        # Extract delta (new words since last stable)
""",
        ["test_a_stable_reaches_the_pipeline_when_the_manager_reports_a_window"]),
    mutation(
        "the-final-transcript-gate-comes-back", RETRACTION_TESTS, WEBSOCKET,
        "                        # wh-7ou.7.6.11 verdict gate: a final carrying the\n",
        """\
                        if getattr(
                            self.state_manager, "audio_recovery_window_open", False
                        ) is True:
                            continue

                        # wh-7ou.7.6.11 verdict gate: a final carrying the
""",
        ["test_a_final_reaches_the_pipeline_when_the_manager_reports_a_window"]),
    mutation(
        "idle-recovery-arms-on-audio-again", DETECTOR_TESTS, DETECTOR,
        '    "idle_recovery": ("idle",),\n',
        '    "idle_recovery": ("idle", "audio"),\n',
        ["test_idle_recovery_does_not_arm_on_audio"]),

    # -- The help prose (plan items 23-24) ---------------------------------
    mutation(
        "the-reference-entry-drops-the-screen-reader-sentence",
        HELP_TESTS, DESCRIPTIONS,
        " On a machine where Windows reports no echo canceller, sound from a "
        "screen reader also pauses listening.'\n",
        "'\n",
        ["test_the_reference_entry_says_a_screen_reader_pauses_listening_"
         "without_an_echo_canceller"]),
    mutation(
        "the-interaction-modes-page-says-a-hold-cannot-override",
        HELP_TESTS, INTERACTION_MODES,
        "A Sonos speaker playing music still pauses a hold, because muting this "
        "computer cannot silence it.\n",
        "A Sonos speaker playing music still pauses a hold, and starting a hold "
        "does not override that.\n",
        ["test_the_interaction_modes_page_no_longer_says_a_hold_cannot_"
         "override_the_sound_pause"]),
]


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
