"""Mutation gate for wh-remote-stt-robustness.

Every test this gate names was written before the code it protects, but a
red-first run only proves the tests failed once. This proves each one
still fails when the specific behaviour it claims to pin is broken, and
keeps proving it after later changes.

Run from services/wheelhouse:

    uv run --no-sync python tests/mutation_gate_remote_stt_robustness.py
    uv run --no-sync python tests/mutation_gate_remote_stt_robustness.py --check

`--check` answers, without running anything, whether every pattern
still matches exactly once and still produces text that parses. Run it
whenever a full sweep is too expensive to repeat.

Each mutation edits one source file, runs the tests that must fail, and
restores the file byte for byte. A mutation counts as caught only when
every test named in `catchers` appears in that run's failures. Anything
else -- a pattern that matches no times or more than once, a mutation
that does not compile, a run that times out or that pytest's own timeout
aborts, a test name that no longer exists, a red baseline -- is reported
as an ERROR, never as a verdict.

``--check`` answers the two offline questions -- does every pattern match
exactly once in the current source, and does every mutant still parse --
without running a test or writing a file. Use it while a suite or a
reviewer round is live; the full run rewrites source files in place and
must never share a worktree with either.

wh-launch-generation extended this gate rather than starting a second
one: it changes the same three write sites and the same startup monitor,
and a separate gate would have had to copy this runner whole. Its
mutations begin at the wh-launch-generation heading in MUTATIONS.

wh-provider-switch-stale-hold extended it for the same reason: its
subject is _switch_stt_provider, the function three of
this gate's earlier mutations already edit, and it needs the two things
this runner already has and the two gates the bead named do not -- a
per-mutation source file (its call sites are in main.py, not the speech
processor) and a per-mutation test selection. Three mutations: one per
switch branch removing the call that drops the held bare number, and one
in speech/speech_processor.py removing the clear inside the method they
call, because both call sites can stand while the method drops nothing.

wh-launch-addressed-notices is the last section and brings this gate its
eighth source file, gui.py. It extends wh-launch-generation rather than
starting a gate of its own: the launch number it carries is the one that
section stamps, and the chain it protects runs from the launcher through
main.py's queue callbacks and the websocket manager's two dismiss sites
into the GUI, which is where the decision to drop a superseded dismiss is
finally made. Two of the mutations it was meant to carry are recorded as
comments beside the entries instead, because no test in this repository
can fail on either; each comment names what was measured.

This gate now covers every stt write in _switch_stt_provider. It used not
to. The in-process branch wrote stt.provider, and no test could drive
that write: the mode that branch needed was read through the same broken
section, so ConfigService.get answered with the "remote" default and the
branch was never taken. It was excluded deliberately rather than left as
a standing survivor. wh-in-process-capture-removal then deleted the
branch and the write together, so there is nothing left to exclude.
grep -rn "stt[.]provider" --include=*.py over services/wheelhouse, .venv
excluded, now matches only two ConfigService docstrings that use the key
as a dot-notation example and three test files; no production write
remains.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parent

MAIN = SERVICE / "main.py"
STATE = SERVICE / "state_manager.py"
SERVICE_MGR = SERVICE / "service_manager.py"
LAUNCHER = SERVICE / "stt" / "remote_stt_launcher.py"
GUI = SERVICE / "gui.py"

PROC = SERVICE / "speech" / "speech_processor.py"

MAL = "tests/test_malformed_stt_settings.py"
STOP = "tests/test_remote_stt_stopped_state.py"
HOLD = "tests/test_provider_switch_stale_hold.py"
SM = "tests/test_state_manager.py::TestSTTHelpers"
WS = SERVICE / "integrations" / "websocket_manager.py"
ACTIONS = SERVICE / "speech" / "actions.py"
OWNER = SERVICE / "shared" / "dialog_owner.py"
LG = "tests/test_launch_generation.py"
# Class-scoped like SM: mutating gui.py reaches most of test_gui.py, and
# a mutation needs only the tests that name it as their catcher plus the
# ones around them (wh-launch-addressed-notices).
DLG = "tests/test_gui.py::TestWorkingDialog"
GQ = "tests/test_gui.py::TestCheckQueuesAndEvents"
WSN = "tests/test_websocket_manager.py::TestNotificationHandling"
# Class-scoped for the same reason as WSN: wh-capture-winrt-required's
# two classes are the only tests that name these mutations as their
# catchers, and running the whole file for each one buys nothing.
CBL = "tests/test_websocket_manager.py::TestCaptureBackendLogLine"
WRR = "tests/test_websocket_manager.py::TestWinrtRefusalReachesTheUser"
TOK = "tests/test_shared_dialog_owner.py"
AI = "tests/test_ai/test_ask_ai_action.py::TestAskAIAction"
GWD = "tests/test_gui.py::TestGuiManagerWorkingDialog"
XFM = (
    "tests/test_ai/test_text_transform_helper.py::"
    "TestTheSequenceIsNotSpecificToCorrecting"
)

# A provider service, not this one. The launch stamp can only be
# corrected when the name a provider announces is the name the launcher
# started it under, so the declaration is part of the behaviour this
# gate protects (wh-ready-connection-stamp.1.1).
PARAKEET_MAIN = (
    SERVICE.parent
    / "stt_providers"
    / "sherpa_offline_parakeet_stt_server"
    / "main.py"
)

# The other provider whose ready notice had to start carrying a kind when
# the substring fallback went away (wh-ready-connection-stamp.2.2.1).
DISTIL_MAIN = (
    SERVICE.parent
    / "stt_providers"
    / "distil_medium_en"
    / "main.py"
)

RUN_TIMEOUT_S = 300

# Every mutated file's original bytes, filled in before the first write.
SOURCES: dict[Path, bytes] = {}

MUTATIONS = [
    # ---- Gap B: the settings-write guard --------------------------------
    {
        "name": "guard-lets-the-error-through",
        "file": MAIN,
        "old": """    except TypeError as e:
        logger.error(
            f'Could not store {key}: the settings file holds a value where '
""",
        "new": """    except ValueError as e:
        logger.error(
            f'Could not store {key}: the settings file holds a value where '
""",
        "selection": [MAL],
        # wh-in-process-capture-removal deleted TestModeChange with the
        # mode-change block it drove. The catcher below was measured
        # under this mutation afterwards (Boss e7 ruling (4)). It carries
        # no assert: the whole test is that the call returns, and under
        # the mutation the TypeError escapes _store_setting and comes out
        # of the awaited _switch_stt_provider -- which is the very thing
        # the test exists to prevent, not an unrelated crash.
        "catchers": [
            "TestProviderSelection::test_a_scalar_stt_setting_does_not_close_the_program",
        ],
    },
    {
        "name": "guard-reports-success",
        "file": MAIN,
        "old": """            f'a section belongs ({e})'
        )
        return False
""",
        "new": """            f'a section belongs ({e})'
        )
        return True
""",
        "selection": [MAL],
        # The same deletion; the same measurement. Both catchers below
        # were run under this mutation and both fail on their own
        # assertions -- "the refused write told the user nothing" and
        # save.assert_not_called (Boss e7 ruling (4)).
        "catchers": [
            "TestProviderSelection::test_a_scalar_stt_setting_is_reported_to_the_user",
            "TestProviderSelection::test_a_refused_write_is_never_saved",
        ],
    },
    {
        "name": "provider-record-write-unguarded",
        "file": MAIN,
        "old": """                stored = _store_setting(self, "stt.last_provider", provider)
""",
        "new": """                stored = self.config_service.set("stt.last_provider", provider) is None
""",
        "selection": [MAL],
        "catchers": [
            "TestProviderSelection::test_a_scalar_stt_setting_does_not_close_the_program",
            "TestProviderSelection::test_a_scalar_stt_setting_is_reported_to_the_user",
        ],
    },
    # ---- Gap A: the stopped state ---------------------------------------
    {
        "name": "reader-ignores-the-stopped-state",
        "file": STATE,
        # wh-in-process-capture-removal dedented this line by four spaces
        # when it deleted the mode local and the `if mode == "remote":`
        # wrapper around the body of _get_current_stt_provider.
        "old": """        if self._remote_stt_confirmed_stopped:
""",
        "new": """        if False and self._remote_stt_confirmed_stopped:
""",
        "selection": [SM],
        "catchers": [
            "test_a_stopped_engine_selects_nothing",
            "test_a_stopped_engine_selects_nothing_without_a_record",
            "test_a_stop_naming_the_running_provider_is_honoured",
        ],
    },
    {
        "name": "a-later-start-leaves-the-stopped-state-set",
        "file": STATE,
        "old": """            self._running_remote_stt_provider = provider
            self._running_remote_stt_generation = generation
            self._remote_stt_confirmed_stopped = False
""",
        "new": """            self._running_remote_stt_provider = provider
            self._running_remote_stt_generation = generation
            pass
""",
        "selection": [SM],
        "catchers": ["test_a_later_start_clears_the_stopped_state"],
    },
    {
        "name": "a-foreign-stop-signal-is-honoured",
        "file": STATE,
        "old": """                    or not self._same_launch(generation)
                )
            ):
                return
""",
        "new": """                    or not self._same_launch(generation)
                )
            ):
                pass
""",
        "selection": [SM],
        "catchers": ["test_a_stop_naming_another_provider_is_ignored"],
    },
    {
        "name": "reported-startup-failure-not-passed-on",
        "file": LAUNCHER,
        "old": """                self._provider_stopped(
                    provider_name, may_wait=True, generation=generation
                )
                return
""",
        "new": """                pass
                return
""",
        "selection": [STOP],
        "catchers": ["test_a_reported_startup_failure_marks_the_provider_stopped"],
    },
    {
        # The anchor is the notify block BEFORE the call, not the method
        # after it. The original anchored on "def discover_providers"
        # and broke the moment _watch_for_late_death was inserted
        # between the two, which the first full sweep of this branch
        # caught as pattern matched 0 times. The watch's own report
        # block is word-for-word identical at eight spaces, so the
        # sixteen-space indentation here is what keeps this unique
        # (wh-provider-late-death-watch).
        # Anchored on the dead-child branch's own comment line, since the
        # _end_starting_state call now sits between the notify and the
        # report (wh-ready-connection-stamp.2.1.2).
        "name": "dead-subprocess-not-passed-on",
        "file": LAUNCHER,
        "old": """            # (wh-ready-connection-stamp.2.1.2).
            self._end_starting_state(generation)
            self._provider_stopped(
                provider_name, may_wait=True, generation=generation
            )
""",
        "new": """            # (wh-ready-connection-stamp.2.1.2).
            self._end_starting_state(generation)
            pass
""",
        "selection": [STOP],
        "catchers": ["test_a_dead_subprocess_marks_the_provider_stopped"],
    },
    {
        "name": "a-live-subprocess-is-reported-stopped",
        "file": LAUNCHER,
        "old": """        if subprocess_alive and not undeclared_failure:
""",
        "new": """        if False and subprocess_alive and not undeclared_failure:
""",
        "selection": [STOP],
        "catchers": ["test_a_slow_but_live_subprocess_is_not_marked_stopped"],
    },
    {
        "name": "a-failing-callback-escapes-the-monitor-thread",
        "file": LAUNCHER,
        "old": """            except Exception as e:
                logger.warning(f"Failed to report a stopped provider: {e}")
""",
        "new": """            except ValueError as e:
                logger.warning(f"Failed to report a stopped provider: {e}")
""",
        "selection": [STOP],
        "catchers": ["test_a_failing_callback_does_not_escape_the_monitor_thread"],
    },
    {
        "name": "autostart-failure-does-not-mark-stopped",
        "file": SERVICE_MGR,
        "old": """        self.state_manager.set_remote_stt_stopped()
        self.state_manager.send_state_update()
        return False
""",
        "new": """        pass
        self.state_manager.send_state_update()
        return False
""",
        "selection": [STOP],
        "catchers": ["test_no_provider_starting_marks_the_engine_stopped"],
    },
    {
        "name": "failed-switch-does-not-mark-stopped",
        "file": MAIN,
        # Anchored on the drop above it since
        # wh-provider-switch-stale-hold.1.1 put that call, and its own
        # comment block, between the (wh-remote-stt-robustness) comment
        # this used to quote and the marking call. The drop line is
        # unique at this indent.
        "old": """                    drop_stale_bare_number_hold()
                    self.state_manager.set_remote_stt_stopped()
""",
        "new": """                    drop_stale_bare_number_hold()
                    pass
""",
        "selection": [STOP],
        "catchers": [
            "test_a_replacement_that_fails_to_start_marks_the_engine_stopped"
        ],
    },
    {
        "name": "failed-switch-does-not-push-the-correction",
        "file": MAIN,
        "old": """                    self.state_manager.set_remote_stt_stopped()
                self.state_manager.send_state_update()
""",
        "new": """                    self.state_manager.set_remote_stt_stopped()
                pass
""",
        "selection": [STOP],
        "catchers": ["test_a_failed_replacement_pushes_the_correction_to_the_gui"],
    },
    # ---- Round 1 finding .1.2: the two record setters are serialised ----
    {
        "name": "stop-guard-runs-outside-the-lock",
        "file": STATE,
        "old": """        with self._remote_stt_record_lock:
            if (
                provider is not None
                and self._running_remote_stt_provider is not None
                and (
                    self._running_remote_stt_provider != provider
                    or not self._same_launch(generation)
                )
            ):
                return
            self._running_remote_stt_provider = None
""",
        "new": """        if (
            provider is not None
            and self._running_remote_stt_provider is not None
            and (
                self._running_remote_stt_provider != provider
                or not self._same_launch(generation)
            )
        ):
            return
        with self._remote_stt_record_lock:
            self._running_remote_stt_provider = None
""",
        "selection": [SM],
        "catchers": [
            "test_a_late_stop_cannot_blank_a_replacement_recorded_meanwhile"
        ],
    },
    {
        "name": "stop-takes-no-lock-at-all",
        "file": STATE,
        "old": """        with self._remote_stt_record_lock:
            if (
                provider is not None
""",
        "new": """        if True:
            if (
                provider is not None
""",
        "selection": [SM],
        "catchers": [
            "test_a_late_stop_cannot_blank_a_replacement_recorded_meanwhile"
        ],
    },
    {
        "name": "start-takes-no-lock-at-all",
        "file": STATE,
        "old": """        with self._remote_stt_record_lock:
            self._running_remote_stt_provider = provider
""",
        "new": """        if True:
            self._running_remote_stt_provider = provider
""",
        "selection": [SM],
        "catchers": [
            "test_a_late_stop_cannot_blank_a_replacement_recorded_meanwhile"
        ],
    },
    # ---- Round 1 codex findings .2.1 and .2.2 ---------------------------
    {
        "name": "empty-discovery-does-not-mark-stopped",
        "file": SERVICE_MGR,
        "old": """            self.state_manager.set_remote_stt_stopped()
            self.state_manager.send_state_update()
            return False
""",
        "new": """            pass
            return False
""",
        "selection": [STOP],
        "catchers": ["test_no_providers_discovered_marks_the_engine_stopped"],
    },
    {
        "name": "failed-switch-blanks-a-surviving-engine",
        "file": MAIN,
        "old": """                if still_running:
                    # Nothing in this branch cleared the record, so
""",
        "new": """                if False:
                    # Nothing in this branch cleared the record, so
""",
        "selection": [STOP],
        "catchers": [
            "test_an_engine_that_outlives_a_failed_switch_is_still_shown"
        ],
    },
    {
        # Re-anchored inside outgoing_engine_is_gone, where the wait
        # moved when the success arm came to need the same answer
        # (wh-provider-switch-stale-hold.2.1). Deciding on the first
        # answer reports the outgoing engine as surviving on every
        # ordinary switch, because it is normally still alive for a
        # moment after the shutdown command -- which is why the success
        # arm catches this now as well as the failure arm.
        "name": "failed-switch-does-not-wait-for-the-exit",
        "file": MAIN,
        "old": """            while still_running and waited < _PROVIDER_EXIT_WAIT_S:
""",
        "new": """            while False:
""",
        "selection": [STOP, HOLD],
        "catchers": [
            "test_a_failed_switch_waits_for_the_old_engine_to_exit",
            "TestRemoteSwitchDropsTheHold::"
            "test_a_spawned_replacement_drops_the_hold_once_the_old_engine_"
            "goes",
        ],
    },
    # ---- Round 2 codex finding .2.5 -------------------------------------
    {
        "name": "delayed-failure-blanks-a-surviving-engine",
        "file": MAIN,
        "old": """    if survivor is not None:
        logger.warning(
""",
        "new": """    if False:
        logger.warning(
""",
        "selection": [STOP],
        "catchers": [
            "test_an_engine_that_outlived_a_failed_replacement_is_still_shown"
        ],
    },
    {
        "name": "delayed-failure-does-not-wait-for-the-exit",
        "file": MAIN,
        "old": """            if survivor is None or waited >= _PROVIDER_EXIT_WAIT_S:
                break
""",
        "new": """            if True:
                break
""",
        "selection": [STOP],
        "catchers": [
            "test_the_reconciliation_waits_for_a_dying_engine_to_exit"
        ],
    },
    {
        "name": "the-failed-provider-nominates-itself",
        "file": MAIN,
        "old": """                if not name or name == provider_name:
""",
        "new": """                if not name:
""",
        "selection": [STOP],
        "catchers": ["test_the_failed_provider_itself_is_not_a_survivor"],
    },
    {
        "name": "an-unreadable-provider-list-loses-the-stopped-state",
        "file": MAIN,
        "old": """    except Exception as e:
        # This thread is the only place that learns a start failed. An
        # unreadable provider list must not cost the stopped state too.
        logger.warning(f"Could not check for a surviving speech engine: {e}")
        survivor = None
""",
        "new": """    except Exception as e:
        logger.warning(f"Could not check for a surviving speech engine: {e}")
        return
""",
        "selection": [STOP],
        "catchers": [
            "test_a_launcher_that_cannot_answer_still_marks_the_engine_stopped"
        ],
    },
    # ---- Round 3 codex findings .2.6 and .2.7 ---------------------------
    {
        "name": "a-report-that-may-not-wait-scans-anyway",
        "file": MAIN,
        "old": """    if not may_wait:
        state_manager.set_remote_stt_stopped(provider_name, generation)
        return
""",
        "new": """    if False:
        state_manager.set_remote_stt_stopped(provider_name, generation)
        return
""",
        "selection": [STOP],
        "catchers": ["test_a_report_that_may_not_wait_does_not_scan_providers"],
    },
    {
        "name": "the-synchronous-failure-path-asks-to-wait",
        "file": LAUNCHER,
        "old": """            self._provider_stopped(provider_name, generation=generation)
            return False
""",
        "new": """            self._provider_stopped(
                provider_name, may_wait=True, generation=generation
            )
            return False
""",
        "selection": [STOP],
        "catchers": [
            "test_a_synchronous_launch_failure_does_not_ask_for_a_wait"
        ],
    },
    {
        "name": "the-survivor-write-ignores-a-newer-record",
        "file": STATE,
        "old": """            if self._running_remote_stt_provider != stopped:
                return False
""",
        "new": """            if False:
                return False
""",
        "selection": [STOP],
        "catchers": [
            "test_a_newer_engine_recorded_meanwhile_is_not_overwritten"
        ],
    },
    # ---- wh-launch-generation: one launch told from the next ------------
    {
        "name": "the-record-does-not-store-the-launch",
        "file": STATE,
        "old": """        generation = self._launch_generation(provider)
        with self._remote_stt_record_lock:
            self._running_remote_stt_provider = provider
""",
        "new": """        generation = None
        with self._remote_stt_record_lock:
            self._running_remote_stt_provider = provider
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_restart_between_the_refusal_and_the_clear_is_not_blanked",
            "test_a_stale_survivor_does_not_overwrite_a_relaunch_of_the_same_name",
        ],
    },
    {
        "name": "the-survivor-write-ignores-the-launch-generation",
        "file": STATE,
        "old": """            if not self._same_launch(generation):
                return False
""",
        "new": """            if False:
                return False
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_stale_survivor_does_not_overwrite_a_relaunch_of_the_same_name"
        ],
    },
    {
        "name": "the-clear-ignores-the-launch-generation",
        "file": STATE,
        "old": """                    self._running_remote_stt_provider != provider
                    or not self._same_launch(generation)
""",
        "new": """                    self._running_remote_stt_provider != provider
                    or False
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_restart_between_the_refusal_and_the_clear_is_not_blanked"
        ],
    },
    {
        "name": "the-reconciliation-drops-the-launch-on-the-survivor-write",
        "file": MAIN,
        "old": """        if state_manager.replace_stopped_remote_stt_provider(
            provider_name, survivor, generation
        ):
""",
        "new": """        if state_manager.replace_stopped_remote_stt_provider(
            provider_name, survivor, None
        ):
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_stale_survivor_does_not_overwrite_a_relaunch_of_the_same_name"
        ],
    },
    {
        "name": "the-reconciliation-drops-the-launch-on-the-clear",
        "file": MAIN,
        "old": """    # (wh-remote-stt-robustness.2.7, wh-launch-generation).
    state_manager.set_remote_stt_stopped(provider_name, generation)
""",
        "new": """    # (wh-remote-stt-robustness.2.7, wh-launch-generation).
    state_manager.set_remote_stt_stopped(provider_name, None)
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_restart_between_the_refusal_and_the_clear_is_not_blanked"
        ],
    },
    {
        "name": "every-launch-reuses-one-generation",
        "file": LAUNCHER,
        "old": """                self._launch_generation_counter += 1
                generation = self._launch_generation_counter
""",
        "new": """                generation = self._launch_generation_counter
""",
        "selection": [LG],
        "catchers": [
            "test_each_start_of_the_same_provider_gets_a_new_generation"
        ],
    },
    {
        # Replaces the-monitor-keeps-a-foreign-startup-failure, whose
        # target (_discard_foreign_startup_failure) was deleted by the
        # fix for wh-launch-generation.1.1. The behaviour it protected
        # is still here, reached a different way.
        "name": "the-monitor-waits-on-the-shared-event-not-its-own-slot",
        "file": LAUNCHER,
        "old": """                ready = signal.event.is_set()
                failed = signal.failed
""",
        "new": """                ready = self._provider_ready_event.is_set()
                failed = self._provider_startup_failed
""",
        "selection": [LG, STOP],
        # Only the stale-monitor test exercises this: the older-launch
        # test never sets a current launch, so the shared event stays
        # clear there and the mutated monitor times out exactly as the
        # real one does. Verified by a gate run, 2026-08-30.
        "catchers": [
            "test_a_stale_monitor_does_not_swallow_the_current_launchs_failure",
        ],
    },
    {
        "name": "the-failure-marks-every-launch-not-its-own",
        "file": LAUNCHER,
        "old": """            targets = [generation]
""",
        "new": """            targets = sorted(set(self._launch_generations.values()))
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_failure_from_an_older_launch_does_not_report_the_new_one_stopped",
        ],
    },
    {
        "name": "an-older-launchs-failure-ends-the-current-starting-state",
        "file": LAUNCHER,
        # The 8-space form is a substring of the 12-space guard in the
        # undeclared branch, which a-consumed-record-ends-any-launchs-startup
        # owns. The comment line below is what makes this anchor name one
        # site (mutation-gate: exactly one match).
        "old": """            self._provider_startup_failed = True
            self._end_starting_state(generation)
""",
        "new": """            self._provider_startup_failed = True
            self._provider_ready_event.set()
""",
        "selection": [LG],
        "catchers": [
            "test_the_replacement_is_still_starting_after_the_foreign_failure",
        ],
    },
    {
        "name": "the-connection-is-never-rebound-to-its-own-launch",
        "file": WS,
        "old": """        self._rebind_launch_stamp(websocket, data.get("provider"))
        if websocket is not self._active_stt_client:
""",
        "new": """        if websocket is not self._active_stt_client:
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_connection_is_bound_to_the_launch_that_spawned_it",
            "test_a_startup_failure_carries_the_senders_launch_not_the_current_one",
        ],
    },
    {
        "name": "an-unattributed-failure-is-guessed-instead-of-dropped",
        "file": WS,
        "old": """                                if websocket in self._stt_client_provisional:
""",
        "new": """                                if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_from_an_undeclared_connection_is_dropped",
        ],
    },
    {
        "name": "the-rebind-keeps-the-provisional-mark",
        "file": WS,
        "old": """        self._stt_client_provisional.discard(websocket)
        if previous != generation:
""",
        "new": """        if previous != generation:
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_connection_is_bound_to_the_launch_that_spawned_it",
        ],
    },
    {
        "name": "the-connection-is-stamped-with-nothing",
        "file": WS,
        "old": """        self._stt_client_generations[websocket] = self._current_launch_generation()
""",
        "new": """        self._stt_client_generations[websocket] = None
""",
        "selection": [LG],
        # The capabilities rebind added for wh-launch-generation.1.2
        # now covers the input the second catcher fed the connect-time
        # stamp, so nulling that stamp no longer changes what it sees.
        # Verified by a gate run, 2026-08-30.
        "catchers": [
            "test_a_connection_is_stamped_with_the_launch_that_spawned_it",
        ],
    },
    {
        "name": "the-failure-signal-names-the-current-launch-not-the-sender",
        "file": WS,
        "old": """                                    self.remote_stt_launcher.signal_provider_startup_failed(
                                        self._stt_client_generations.get(websocket)
                                    )
""",
        "new": """                                    self.remote_stt_launcher.signal_provider_startup_failed(
                                        self._current_launch_generation()
                                    )
""",
        "selection": [LG],
        "catchers": [
            "test_a_startup_failure_carries_the_senders_launch_not_the_current_one"
        ],
    },
    {
        "name": "the-stamp-outlives-its-connection",
        "file": WS,
        "old": """            self._stt_client_generations.pop(websocket, None)
""",
        "new": """            pass
""",
        "selection": [LG],
        "catchers": ["test_the_stamp_leaves_with_the_connection"],
    },
    {
        "name": "the-monitor-trusts-the-waits-answer-not-its-slot",
        "file": LAUNCHER,
        "old": """                ready = signal.event.is_set()
""",
        "new": """                ready = False
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_failure_landing_at_the_deadline_still_reports_stopped",
        ],
    },
    {
        "name": "the-monitor-removes-its-slot-instead-of-closing-it",
        "file": LAUNCHER,
        "old": """                signal.closed = True
""",
        "new": """                self._launch_signals.pop(generation, None)
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_arriving_after_the_monitor_finished_still_reports",
        ],
    },
    {
        "name": "an-orphaned-failure-is-never-reported",
        "file": LAUNCHER,
        "old": """                if signal.closed and not signal.reported:
""",
        "new": """                if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_arriving_after_the_monitor_finished_still_reports",
        ],
    },
    {
        "name": "the-monitor-ignores-a-recorded-undeclared-failure",
        "file": LAUNCHER,
        # The first line alone also guards the watchdog report in
        # start_provider, so the line under it is what makes this the
        # monitor's record read (wh-gate-remote-stt-patterns-repair).
        "old": """                if generation is not None:
                    undeclared_failure = (
""",
        "new": """                if False:
                    undeclared_failure = (
""",
        "selection": [LG, STOP],
        "catchers": [
            "test_a_recorded_undeclared_failure_ends_the_slow_start_excuse",
        ],
    },
    {
        "name": "any-launchs-record-ends-the-slow-start",
        "file": LAUNCHER,
        "old": """                    undeclared_failure = (
                        generation in self._undeclared_startup_failures
                    )
""",
        "new": """                    undeclared_failure = bool(
                        self._undeclared_startup_failures
                    )
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_recorded_against_another_launch_is_not_borrowed",
        ],
    },
    {
        "name": "the-drop-does-not-record-the-failure",
        "file": WS,
        "old": """                                    self.remote_stt_launcher.record_undeclared_startup_failure(
                                        self._stt_client_generations.get(websocket)
                                    )
""",
        "new": """                                    pass
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_from_an_undeclared_connection_is_dropped",
        ],
    },
    {
        "name": "the-record-never-reports-an-orphaned-launch",
        "file": LAUNCHER,
        # Refreshed twice. wh-launch-signal-eviction split this test in
        # two: an evicted launch takes the None arm above, and a launch
        # whose slot is still in the ring -- which is what this
        # mutation's catcher builds -- takes this one. Then
        # wh-launch-signal-eviction.1.2 widened the condition to
        # `elif signal.closed:`, because the discard below now runs
        # whether or not the report is made.
        "old": """            elif signal.closed:
""",
        "new": """            elif False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_record_made_after_the_monitor_gave_up_still_reports_stopped",
        ],
    },
    {
        "name": "a-closed-slot-strands-the-record-it-cannot-report",
        "file": LAUNCHER,
        # The discard is hoisted out of the report on purpose: the record
        # has one reader, and a closed slot means that reader has
        # finished, so the record goes whether or not this arm reports
        # anything. Tying it to the report stranded the record for the
        # life of the launcher in both cases where no report is made
        # (wh-launch-signal-eviction.1.2).
        "old": """                self._undeclared_startup_failures.discard(
                    provisional_generation
                )
                if not signal.reported and name is not None:
""",
        "new": """                if not signal.reported and name is not None:
""",
        "selection": [LG],
        "catchers": [
            "test_a_record_is_dropped_when_its_launch_was_already_reported",
            "test_a_record_is_dropped_when_its_launch_can_no_longer_be_named",
        ],
    },
    {
        "name": "the-monitor-claims-no-report-under-the-lock",
        "file": LAUNCHER,
        "old": """                if will_report:
""",
        "new": """                if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_between_closing_the_slot_and_reporting_reports_once",
        ],
    },
    {
        "name": "an-orphaned-report-does-not-wait",
        "file": LAUNCHER,
        "old": """            kwargs={"may_wait": True, "generation": generation},
""",
        "new": """            kwargs={"may_wait": False, "generation": generation},
""",
        "selection": [LG],
        "catchers": [
            "test_an_orphaned_failure_reconciles_survivors",
            "test_a_failure_arriving_after_the_monitor_finished_still_reports",
        ],
    },
    {
        "name": "a-consumed-record-ends-any-launchs-startup",
        "file": LAUNCHER,
        "old": """            # step (wh-launch-generation.2.14).
            self._end_starting_state(generation)
""",
        "new": """            # step (wh-launch-generation.2.14).
            self._provider_ready_event.set()
""",
        "selection": [LG],
        "catchers": [
            "test_an_earlier_launchs_record_leaves_the_new_launch_starting",
        ],
    },
    {
        "name": "an-orphaned-record-ends-any-launchs-startup",
        "file": LAUNCHER,
        "old": """        self._end_starting_state(provisional_generation)
""",
        "new": """        self._provider_ready_event.set()
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_record_for_an_earlier_launch_leaves_the_new_one_starting",
        ],
    },
    {
        "name": "the-monitor-reads-the-name-keyed-handle",
        "file": LAUNCHER,
        "old": """        proc = process if process is not None else self._subprocesses.get(
            provider_name
        )
""",
        "new": """        proc = self._subprocesses.get(provider_name)
""",
        "selection": [LG],
        "catchers": [
            "test_a_restart_of_the_same_provider_does_not_lend_its_process",
        ],
    },
    {
        "name": "a-replaced-launch-still-owns-the-shared-display",
        "file": LAUNCHER,
        "old": """        return generation is None or generation == self._current_launch_generation
""",
        "new": """        return True
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launch_does_not_announce_a_failure_for_the_new_one",
        ],
    },
    {
        "name": "start-provider-does-not-name-the-child",
        "file": LAUNCHER,
        "old": """                    generation,
                    proc,
""",
        "new": """                    generation,
                    None,
""",
        "selection": [LG],
        "catchers": [
            "test_start_provider_passes_the_spawned_child_to_the_monitor",
        ],
    },
    {
        "name": "any-launchs-failure-hides-the-dialog",
        "file": WS,
        "old": """                            if (
                                self._sending_launch_is_current(websocket)
                                and self.state_manager
""",
        "new": """                            if (
                                self.state_manager
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launchs_failure_does_not_hide_the_new_dialog",
        ],
    },
    {
        "name": "any-launchs-failure-still-toasts",
        "file": WS,
        # REFRESHED, not replaced: the .2.12 fix widened the condition
        # from one kind to the exempt-kind tuple, so the old anchor no
        # longer matched. The behaviour this breaks is unchanged.
        "old": """                        if kind in _STARTUP_SUPPRESSION_EXEMPT_KINDS and not (
                            self._sending_launch_is_current(websocket)
                        ):
""",
        "new": """                        if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launchs_failure_does_not_toast",
        ],
    },
    {
        "name": "no-launch-owns-the-websocket-display",
        "file": WS,
        "old": """            return bool(
                launcher.launch_is_current(
                    self._stt_client_generations.get(websocket)
                )
            )
""",
        "new": """            return False
""",
        "selection": [LG],
        "catchers": [
            "test_the_current_launchs_failure_still_hides_and_toasts",
        ],
    },
    {
        "name": "only-the-startup-kind-is-checked-for-currency",
        "file": WS,
        "old": """                        if kind in _STARTUP_SUPPRESSION_EXEMPT_KINDS and not (
""",
        "new": """                        if kind == "startup_failed" and not (
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launchs_runtime_error_does_not_toast",
        ],
    },
    {
        "name": "the-runtime-error-loses-its-suppression-exemption",
        "file": WS,
        "old": """                            kind not in _STARTUP_SUPPRESSION_EXEMPT_KINDS
""",
        "new": """                            kind not in ("startup_failed",)
""",
        "selection": [LG],
        "catchers": [
            "test_the_current_launchs_runtime_error_still_toasts",
        ],
    },
    {
        # Refreshed for the one-line condition
        # (wh-ready-connection-stamp.2.2.1). The mutation is unchanged in
        # substance: it drops the kind test and matches on the message
        # text, which is the defect that commit removed.
        "name": "any-message-saying-ready-completes-the-startup",
        "file": WS,
        "old": """                        elif self.remote_stt_launcher and kind == "ready":
""",
        "new": """                        elif self.remote_stt_launcher and "ready" in notification_message.lower():
""",
        "selection": [LG],
        "catchers": [
            "test_a_structured_error_does_not_complete_startup",
            "test_a_kind_less_ready_message_no_longer_completes_startup",
        ],
    },
    {
        # Refreshed for the one-line condition
        # (wh-ready-connection-stamp.2.2.1). The mutation is unchanged in
        # substance: it puts the kind-less substring arm back in place of
        # the kind test.
        "name": "only-an-unclassified-frame-completes-the-startup",
        "file": WS,
        "old": """                        elif self.remote_stt_launcher and kind == "ready":
""",
        "new": """                        elif self.remote_stt_launcher and (
                            not kind and "ready" in notification_message.lower()
                        ):
""",
        "selection": [LG],
        "catchers": [
            "test_a_structured_ready_frame_still_completes_startup",
            "test_a_kind_less_ready_message_no_longer_completes_startup",
            # Its frame carries kind="ready" since
            # wh-ready-connection-stamp.2.2.1, so this mutation's
            # kind-less arm never matches it and the launch it belongs
            # to is never completed. Listing it costs nothing: this
            # entry already selects the whole file.
            "test_a_late_parakeet_ready_reaches_its_own_launch",
        ],
    },
    {
        "name": "the-guard-and-the-write-are-two-steps",
        "file": LAUNCHER,
        "old": """        with self._launch_signals_lock:
            if self.launch_is_current(generation):
                self._provider_ready_event.set()
""",
        "new": """        if self.launch_is_current(generation):
            self._provider_ready_event.set()
""",
        "selection": [LG],
        # NOT the failure-signal test: that site takes the lock at its
        # own call site, around the startup-failed flag as well, so it
        # keeps holding it when the helper stops. It survived here on
        # the first run for exactly that reason, which is the behaviour
        # and not a gap in the test.
        "catchers": [
            "test_a_switch_during_the_monitors_guard_leaves_the_new_launch_starting",
            "test_a_switch_during_the_late_records_guard_leaves_it_starting",
        ],
    },
    {
        "name": "the-stamp-is-not-held-against-the-guards",
        "file": LAUNCHER,
        # Refreshed for wh-launch-signal-eviction: the stamp block
        # gained the slot creation and the monitor's hold, both of
        # which belong inside the same lock this mutation removes.
        # Refreshed again for wh-gate-remote-stt-patterns-repair: the
        # lock block now opens with the watchdog retry's own check, so
        # the lock line is replaced in place rather than the stamp
        # moved out below it. A stamp moved out still waits on the
        # `with` line's acquire, and that mutant survived every catcher
        # while the stamp stayed ordered against the guards.
        "old": """            with self._launch_signals_lock:
                if _watchdog_generation is not None:
""",
        "new": """            if True:
                if _watchdog_generation is not None:
""",
        "selection": [LG],
        "catchers": [
            "test_a_switch_during_the_monitors_guard_leaves_the_new_launch_starting",
            "test_a_switch_during_the_failure_signals_guard_leaves_it_starting",
            "test_a_switch_during_the_late_records_guard_leaves_it_starting",
        ],
    },
    # ---- wh-ready-connection-stamp: the ready signal carries its own
    # connection's launch, the same way the failure signal already did.
    {
        "name": "the-ready-signal-names-the-current-launch-not-the-sender",
        "file": LAUNCHER,
        # The two lines alone appear in signal_provider_startup_failed
        # as well, so the comment below them is what makes this the
        # ready path and not that one.
        "old": """        if generation is None:
            generation = self._current_launch_generation
        # Both writes under ONE acquisition. Read apart, they are a
""",
        "new": """        generation = self._current_launch_generation
        # Both writes under ONE acquisition. Read apart, they are a
""",
        "selection": [LG],
        "catchers": [
            "test_an_earlier_launchs_ready_leaves_the_new_launch_starting",
            "test_an_earlier_launchs_ready_does_not_wake_the_new_monitor",
            "test_an_earlier_launchs_ready_does_not_end_the_new_monitors_wait",
        ],
    },
    {
        "name": "the-ready-wakes-the-current-launchs-slot-not-the-senders",
        "file": LAUNCHER,
        # Refreshed for wh-gate-remote-stt-patterns-repair: the ready
        # path now records on the slot whether the launch failed.
        "old": """        with self._launch_signals_lock:
            signal = self._launch_signal(generation)
            if signal is not None:
                signal.ready = not signal.failed
                signal.event.set()
""",
        "new": """        with self._launch_signals_lock:
            signal = self._launch_signal(self._current_launch_generation)
            if signal is not None:
                signal.ready = not signal.failed
                signal.event.set()
""",
        "selection": [LG],
        # Not the starting-state test: this mutation leaves
        # _end_starting_state reading the sender's own generation, so
        # is_starting is still guarded. Only the slot moves.
        "catchers": [
            "test_an_earlier_launchs_ready_does_not_wake_the_new_monitor",
            "test_an_earlier_launchs_ready_does_not_end_the_new_monitors_wait",
        ],
    },
    {
        "name": "an-older-launchs-ready-ends-the-current-starting-state",
        "file": LAUNCHER,
        "old": """            # provider it replaced (wh-ready-connection-stamp).
            self._end_starting_state(generation)
""",
        "new": """            # provider it replaced (wh-ready-connection-stamp).
            self._provider_ready_event.set()
""",
        "selection": [LG],
        # Only the shared flag moves here, so the slot assertions and
        # the monitor test both stay green -- which is the point of
        # keeping this mutation separate from the two above.
        "catchers": [
            "test_an_earlier_launchs_ready_leaves_the_new_launch_starting",
        ],
    },
    {
        "name": "the-ready-frame-does-not-carry-its-connection",
        "file": WS,
        "old": """                            self.remote_stt_launcher.signal_provider_ready(
                                self._stt_client_generations.get(websocket)
                            )
""",
        "new": """                            self.remote_stt_launcher.signal_provider_ready()
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launchs_ready_signals_its_own_launch",
            "test_the_current_launchs_ready_still_carries_its_stamp",
        ],
    },
    {
        # The input-level mutation the mutation-gate skill asks for
        # beside the reverts above: the call still carries a generation,
        # but reads it from the launcher's current launch instead of
        # from the connection that sent the frame. A revert-only gate
        # would let this pass.
        "name": "the-ready-frame-reads-the-current-launch-not-its-own-stamp",
        "file": WS,
        "old": """                            self.remote_stt_launcher.signal_provider_ready(
                                self._stt_client_generations.get(websocket)
                            )
""",
        "new": """                            self.remote_stt_launcher.signal_provider_ready(
                                self._current_launch_generation()
                            )
""",
        "selection": [LG],
        # The current-launch test stamps sender and current alike, so
        # the two sources agree there and only the replaced-launch test
        # can tell them apart.
        "catchers": [
            "test_a_replaced_launchs_ready_signals_its_own_launch",
        ],
    },
    # ---- The declared provider name -------------------------------------
    # A stamp is only corrected when the launcher recognises the name the
    # connection announces, so the declaration itself carries the
    # behaviour. These two mutate a DIFFERENT service's source than every
    # mutation above (wh-ready-connection-stamp.1.1).
    #
    # Both quote the "debug=True," line above the declaration. Without
    # it the pattern matched three times once wh-capture-winrt-required
    # A3 added two refusal notices that name the same provider, and a
    # runner that edits the first match would have mutated a refusal
    # while reporting a survivor for the declaration nothing touched
    # (mutation-gate skill, the exactly-one-match rule). The neighbour
    # line belongs to the WSForwarder construction alone.
    {
        "name": "the-provider-announces-a-name-the-launcher-never-started",
        "file": PARAKEET_MAIN,
        "old": """            debug=True,
            provider_name="parakeet_tdt",
""",
        "new": """            debug=True,
            provider_name="sherpa_offline_parakeet",
""",
        "selection": [LG],
        "catchers": [
            "test_the_declared_name_matches_the_manifest_name",
            "test_a_late_parakeet_ready_reaches_its_own_launch",
        ],
    },
    {
        # The input-level mutation the mutation-gate skill asks for
        # beside the revert above. Reverting to the one historical name
        # would let a test pass by refusing that single string; this
        # names a value that never existed, so only a test comparing the
        # declaration against the manifest can still catch it.
        "name": "the-provider-announces-a-name-that-never-existed",
        "file": PARAKEET_MAIN,
        "old": """            debug=True,
            provider_name="parakeet_tdt",
""",
        "new": """            debug=True,
            provider_name="parakeet",
""",
        "selection": [LG],
        "catchers": [
            "test_the_declared_name_matches_the_manifest_name",
            "test_a_late_parakeet_ready_reaches_its_own_launch",
        ],
    },
    # wh-provider-late-death-watch. The startup monitor's slow-start
    # branch hands a still-live child to a watch that outlives the
    # monitor, so a pre-ready exit after the bounded wait is reported
    # the way an in-window death is. The mutations below break each
    # part of that in turn.
    #
    # No count is given on purpose. This comment said ten, and the
    # commit that added the eleventh entry did not update it. A count
    # in a section heading goes stale on the next entry, and the same
    # mistake was made twice on this branch -- once here and once in
    # the test helper's docstring, which now names its property instead
    # of a number (wh-provider-late-death-watch.2.6,
    # wh-provider-late-death-watch.2.7).
    {
        "name": "the-late-death-watch-never-starts",
        "file": LAUNCHER,
        "old": """            if signal is not None and proc is not None:
                watch = threading.Thread(
""",
        "new": """            if False:
                watch = threading.Thread(
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_death_reports_stopped_like_an_in_window_death",
            "test_a_superseded_launchs_late_death_shows_no_notice",
            "test_the_watch_holds_no_lock_across_its_report",
        ],
    },
    {
        # The guard is disabled rather than the wait deleted, so the
        # mutant still terminates: a deleted wait would spin the loop on
        # poll() until the limit and prove nothing about the branch.
        #
        # The catcher asserts on polling, not on the absence of a
        # report. It used to be the exits-after-going-ready test, which
        # stopped failing under this mutant the moment the .2.3 in-lock
        # recheck landed: that recheck suppresses the report for the
        # same input, so disabling this branch changed nothing the test
        # could see. A set slot ending the watch before it polls is the
        # part only this branch owns
        # (wh-provider-late-death-watch.2.3).
        "name": "the-late-death-watch-ignores-the-ready-signal",
        "file": LAUNCHER,
        "old": """            if signal.event.wait(timeout=poll_interval):
""",
        "new": """            if signal.event.wait(timeout=poll_interval) and False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_ready_slot_ends_the_watch_before_it_polls",
        ],
    },
    {
        "name": "the-late-death-watch-does-not-claim-the-report",
        "file": LAUNCHER,
        "old": """            if signal.reported:
                # A late startup_failed frame reached this launch first
                # and has already reported it.
                return
            signal.reported = True
""",
        "new": """            pass
""",
        "selection": [LG],
        "catchers": [
            "test_a_failure_frame_after_a_late_death_does_not_report_twice",
        ],
    },
    {
        "name": "the-late-death-notice-ignores-the-launch-generation",
        "file": LAUNCHER,
        "old": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=generation
        )
""",
        "new": """        if True:
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=generation
        )
""",
        "selection": [LG],
        "catchers": [
            "test_a_superseded_launchs_late_death_shows_no_notice",
        ],
    },
    {
        "name": "the-late-death-report-drops-its-generation",
        "file": LAUNCHER,
        "old": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=generation
        )
""",
        "new": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=None
        )
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_death_reports_stopped_like_an_in_window_death",
            "test_a_superseded_launchs_late_death_shows_no_notice",
        ],
    },
    {
        "name": "the-late-death-report-does-not-wait",
        "file": LAUNCHER,
        "old": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=generation
        )
""",
        "new": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=False, generation=generation
        )
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_death_reports_stopped_like_an_in_window_death",
            "test_a_superseded_launchs_late_death_shows_no_notice",
        ],
    },
    {
        # The watch's own report block, at eight-space indentation. The
        # monitor's dead-child branch carries the same three lines at
        # twelve, so the indentation is what keeps this pattern unique.
        # Deleting the hide leaves the notify behind, so the block still
        # compiles and the mutant cannot hang.
        #
        # This mutation exists because deleting that line broke nothing:
        # every test in the late-death class passed with the watch's
        # hide gone, since the two assertions there that mention the
        # dialog are both negative and the positive pin that existed
        # covered the monitor's copy (wh-provider-late-death-watch.1.4).
        "name": "the-late-death-report-leaves-the-dialog-up",
        "file": LAUNCHER,
        "old": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
""",
        "new": """        if self.launch_is_current(generation):
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
""",
        "selection": [LG],
        "catchers": [
            "test_the_watch_hides_the_working_dialog_for_its_own_launch",
            "test_a_late_death_reports_stopped_like_an_in_window_death",
        ],
    },
    {
        # Inverting the liveness test, not deleting it: the watch still
        # reaches its limit and returns, so the mutant cannot hang --
        # but only for a test that SHRINKS `_late_death_watch_limit`.
        # The default limit is 300s and the default poll is 1.0s, so a
        # test that leaves them alone passes on the first poll and hangs
        # under this mutant for five minutes. pytest's own timeout then
        # aborts the whole run, and this gate reports that as an error
        # rather than a verdict, so the catcher below never runs. A test
        # added to this selection must bound its watch
        # (wh-launch-addressed-notices).
        # The catcher is the live-child test, and only that one. The
        # late-death tests also fail under this mutant, but they fail
        # because no report was made at all, which proves the mutant
        # changed something and NOT the property this mutation names
        # (wh-provider-late-death-watch.2.2).
        "name": "the-late-death-watch-reports-a-live-child",
        "file": LAUNCHER,
        "old": """            if proc.poll() is not None:
                break
""",
        "new": """            if proc.poll() is None:
                break
""",
        "selection": [LG],
        "catchers": [
            "test_a_child_that_is_still_alive_is_never_reported",
        ],
    },
    {
        # The report is made outside the lock. Moving it inside is the
        # mutation because deleting the lock cannot fail: the probe
        # would find it free either way.
        "name": "the-late-death-report-is-made-under-the-lock",
        "file": LAUNCHER,
        "old": """        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=generation
        )
""",
        "new": """        with self._launch_signals_lock:
            if self.launch_is_current(generation):
                self._hide_working(generation)
                self._notify(
                    display_name,
                    "Failed to start - try restarting Wheelhouse",
                    generation,
                )
            self._provider_stopped(
                provider_name, may_wait=True, generation=generation
            )
""",
        "selection": [LG],
        "catchers": [
            "test_the_watch_holds_no_lock_across_its_report",
        ],
    },
    {
        # The filter is removed, not the append: a mutation that
        # dropped the append would break every other late-death test
        # for an unrelated reason and prove nothing about pruning.
        "name": "the-late-death-watch-list-never-prunes",
        "file": LAUNCHER,
        "old": """                    self._late_death_watch_threads = [
                        existing
                        for existing in self._late_death_watch_threads
                        if existing.is_alive()
                    ]
                    self._late_death_watch_threads.append(watch)
""",
        "new": """                    self._late_death_watch_threads.append(watch)
""",
        "selection": [LG],
        "catchers": [
            "test_the_watch_list_drops_threads_that_have_finished",
        ],
    },
    {
        # The recheck is disabled, not deleted: the branch has to stay
        # so the mutant compiles and reaches the claim below it.
        "name": "the-late-death-claim-ignores-a-late-ready",
        "file": LAUNCHER,
        "old": """            if signal.event.is_set():
""",
        "new": """            if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_ready_that_lands_while_the_watch_polls_is_no_failure",
        ],
    },
    # ---- wh-launch-signal-eviction: which slot the cap may drop ---------
    {
        "name": "eviction-drops-a-slot-its-owner-still-holds",
        "file": LAUNCHER,
        "old": """                if signal.owners == 0:
                    oldest = generation
                    break
""",
        "new": """                if True:
                    oldest = generation
                    break
""",
        "selection": [LG],
        "catchers": [
            "test_an_owner_still_learns_its_outcome_after_nine_later_launches",
            "test_the_monitor_closes_the_slot_the_ring_still_holds",
        ],
    },
    {
        "name": "eviction-drops-the-slot-it-has-just-created",
        "file": LAUNCHER,
        "old": """                if generation == protect:
                    continue
""",
        "new": """                if False:
                    continue
""",
        "selection": [LG],
        "catchers": [
            "test_a_new_launch_keeps_the_slot_it_was_just_given",
        ],
    },
    {
        "name": "an-evicted-launch-gets-a-fresh-slot-nobody-reads",
        "file": LAUNCHER,
        "old": """            if generation <= self._launch_generation_counter:
""",
        "new": """            if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_ready_for_an_evicted_launch_builds_no_unowned_slot",
        ],
    },
    {
        "name": "an-evicted-launch-leaves-no-report-owing",
        "file": LAUNCHER,
        "old": """            if evicted.closed and not evicted.reported:
""",
        "new": """            if False:
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_failure_for_an_evicted_launch_still_reports_the_orphan",
            "test_a_late_undeclared_failure_for_an_evicted_launch_still_reports",
        ],
    },
    {
        "name": "the-owed-report-is-not-spent-when-it-is-claimed",
        "file": LAUNCHER,
        "old": """        del self._evicted_unreported[generation]
        return True
""",
        "new": """        return True
""",
        "selection": [LG],
        "catchers": [
            "test_an_evicted_launch_is_reported_once_however_many_failures_come",
        ],
    },
    {
        "name": "the-launch-is-not-held-when-it-is-stamped",
        "file": LAUNCHER,
        "old": """                # below on the one path that returns without a monitor.
                self._claim_launch_signal(generation)
""",
        "new": """                # below on the one path that returns without a monitor.
                pass
""",
        "selection": [LG],
        "catchers": [
            "test_start_provider_holds_the_slot_before_the_monitor_begins",
        ],
    },
    {
        "name": "the-monitor-never-gives-its-slot-back",
        "file": LAUNCHER,
        "old": """        finally:
            self._release_launch_signal(generation)

    def _monitor_startup_body(
""",
        "new": """        finally:
            pass

    def _monitor_startup_body(
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_ready_for_an_evicted_launch_builds_no_unowned_slot",
            "test_a_late_failure_for_an_evicted_launch_still_reports_the_orphan",
            "test_a_late_undeclared_failure_for_an_evicted_launch_still_reports",
        ],
    },
    {
        "name": "the-late-death-watch-does-not-hold-its-slot",
        "file": LAUNCHER,
        # Refreshed for wh-launch-signal-eviction.1.1, which put the
        # start inside a `try` so a refused thread can give the hold
        # back. The claim itself is unchanged; only the line after it is.
        "old": """                    # (wh-launch-signal-eviction).
                    self._claim_launch_signal(generation)
                try:
""",
        "new": """                    # (wh-launch-signal-eviction).
                    pass
                try:
""",
        "selection": [LG],
        "catchers": [
            "test_the_monitor_closes_the_slot_the_ring_still_holds",
        ],
    },
    {
        "name": "a-watch-that-cannot-start-keeps-its-slot",
        "file": LAUNCHER,
        # The watch's hold is released by the watch's own `finally`,
        # which a thread that never ran never reaches, so the start needs
        # its own release. A leaked hold is permanent -- every eviction
        # pass skips a held slot (wh-launch-signal-eviction.1.1).
        "old": """                    self._release_launch_signal(generation)
                    raise
""",
        "new": """                    pass
                    raise
""",
        "selection": [LG],
        "catchers": [
            "test_a_watch_that_cannot_start_gives_its_slot_back",
        ],
    },
    {
        "name": "a-refused-reporter-start-escapes-its-caller",
        "file": LAUNCHER,
        # The caller has already spent the launch's one report and has
        # more of its own work after the call, so the exception must not
        # escape (wh-launch-signal-eviction.2.1). This mutation's
        # catchers fail on the escaping refusal rather than on a named
        # assertion. That is not the false catch the skill warns about:
        # the escape IS the defect here, which is what the third
        # catcher's name says. The pattern carries both exception names
        # because the guard widened in wh-launch-signal-eviction.2.2.
        "old": """        except (RuntimeError, MemoryError) as e:
""",
        "new": """        except ValueError as e:
""",
        "selection": [LG],
        "catchers": [
            "test_a_failed_start_leaves_an_in_ring_report_still_owed[runtimeerror]",
            "test_a_failed_start_leaves_an_in_ring_report_still_owed[memoryerror]",
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed[runtimeerror]",
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed[memoryerror]",
            "test_a_failed_start_does_not_abort_the_failure_signal[runtimeerror]",
            "test_a_failed_start_does_not_abort_the_failure_signal[memoryerror]",
        ],
    },
    {
        "name": "a-reporter-start-guard-that-names-one-refusal",
        "file": LAUNCHER,
        # `Thread.start` raises MemoryError as well as RuntimeError
        # before the new thread exists, so a guard that names only
        # RuntimeError leaves the launch's one report spent on a report
        # nobody made -- the defect the widened guard removed. Only the
        # memoryerror cases catch this; the runtimeerror cases stay
        # green, which is what makes the parametrized ids worth naming
        # separately (wh-launch-signal-eviction.2.2).
        "old": """        except (RuntimeError, MemoryError) as e:
""",
        "new": """        except RuntimeError as e:
""",
        "selection": [LG],
        "catchers": [
            "test_a_failed_start_leaves_an_in_ring_report_still_owed"
            "[memoryerror]",
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed"
            "[memoryerror]",
            "test_a_failed_start_does_not_abort_the_failure_signal"
            "[memoryerror]",
        ],
    },
    {
        "name": "a-reporter-handoff-guarded-only-from-the-start-call",
        "file": LAUNCHER,
        # The full revert. Construction and the prune both run between
        # the caller's irrevocable spend and the running thread, and
        # both allocate, so leaving them outside the try loses the
        # report exactly as an unguarded start did
        # (wh-launch-signal-eviction.2.3).
        "old": """        thread = None
        tracked = False
        try:
            thread = threading.Thread(
                target=self._provider_stopped,
                args=(provider_name,),
                kwargs={"may_wait": True, "generation": generation},
                daemon=True,
            )
            with self._launch_signals_lock:
                # Finished threads are dropped here rather than joined:
                # nothing waits on them outside the tests, and a launcher
                # that ran for hours must not accumulate handles.
                self._orphan_report_threads = [
                    existing
                    for existing in self._orphan_report_threads
                    if existing.is_alive()
                ]
                self._orphan_report_threads.append(thread)
                tracked = True
            thread.start()
""",
        "new": """        thread = None
        tracked = False
        thread = threading.Thread(
            target=self._provider_stopped,
            args=(provider_name,),
            kwargs={"may_wait": True, "generation": generation},
            daemon=True,
        )
        with self._launch_signals_lock:
            # Finished threads are dropped here rather than joined:
            # nothing waits on them outside the tests, and a launcher
            # that ran for hours must not accumulate handles.
            self._orphan_report_threads = [
                existing
                for existing in self._orphan_report_threads
                if existing.is_alive()
            ]
            self._orphan_report_threads.append(thread)
            tracked = True
        try:
            thread.start()
""",
        "selection": [LG],
        "catchers": [
            "test_a_refused_reporter_construction_leaves_an_in_ring"
            "_report_owed[runtimeerror]",
            "test_a_refused_reporter_construction_leaves_an_in_ring"
            "_report_owed[memoryerror]",
            "test_a_refused_reporter_construction_leaves_an_evicted"
            "_debt_unclaimed[runtimeerror]",
            "test_a_refused_reporter_construction_leaves_an_evicted"
            "_debt_unclaimed[memoryerror]",
            "test_a_refused_reporter_construction_does_not_abort"
            "_the_signal[runtimeerror]",
            "test_a_refused_reporter_construction_does_not_abort"
            "_the_signal[memoryerror]",
            "test_a_refusal_while_pruning_finished_reporters"
            "_leaves_report_owed[runtimeerror]",
            "test_a_refusal_while_pruning_finished_reporters"
            "_leaves_report_owed[memoryerror]",
        ],
    },
    {
        "name": "a-reporter-built-before-the-guard-opens",
        "file": LAUNCHER,
        # The construction alone, left outside. `Thread.__init__` builds
        # an Event and registers the object, so it raises MemoryError
        # under exhaustion and RuntimeError when daemon threads are
        # disabled. The prune stays guarded, so only the six
        # construction cases catch this and the two pruning cases stay
        # green -- which is what separates the two regions
        # (wh-launch-signal-eviction.2.3).
        "old": """        thread = None
        tracked = False
        try:
            thread = threading.Thread(
                target=self._provider_stopped,
                args=(provider_name,),
                kwargs={"may_wait": True, "generation": generation},
                daemon=True,
            )
            with self._launch_signals_lock:
""",
        "new": """        thread = None
        tracked = False
        thread = threading.Thread(
            target=self._provider_stopped,
            args=(provider_name,),
            kwargs={"may_wait": True, "generation": generation},
            daemon=True,
        )
        try:
            with self._launch_signals_lock:
""",
        "selection": [LG],
        "catchers": [
            "test_a_refused_reporter_construction_leaves_an_in_ring"
            "_report_owed[runtimeerror]",
            "test_a_refused_reporter_construction_leaves_an_in_ring"
            "_report_owed[memoryerror]",
            "test_a_refused_reporter_construction_leaves_an_evicted"
            "_debt_unclaimed[runtimeerror]",
            "test_a_refused_reporter_construction_leaves_an_evicted"
            "_debt_unclaimed[memoryerror]",
            "test_a_refused_reporter_construction_does_not_abort"
            "_the_signal[runtimeerror]",
            "test_a_refused_reporter_construction_does_not_abort"
            "_the_signal[memoryerror]",
        ],
    },
    {
        "name": "a-handle-taken-back-out-on-the-wrong-question",
        "file": LAUNCHER,
        # `tracked` says a handle was actually appended; `thread is not
        # None` says only that one was built. They differ exactly when
        # the prune refuses between the two, and there `remove` raises
        # ValueError, which nothing catches -- so the escape the fix
        # closed reopens under a different exception. Only the two
        # pruning cases catch it (wh-launch-signal-eviction.2.3).
        "old": """            if tracked:
""",
        "new": """            if thread is not None:
""",
        "selection": [LG],
        "catchers": [
            "test_a_refusal_while_pruning_finished_reporters"
            "_leaves_report_owed[runtimeerror]",
            "test_a_refusal_while_pruning_finished_reporters"
            "_leaves_report_owed[memoryerror]",
        ],
    },
    {
        "name": "a-refused-reporter-start-reports-success",
        "file": LAUNCHER,
        # The answer is what tells the caller to put back what it spent.
        # A start that claims success spends the report on nothing
        # (wh-launch-signal-eviction.2.1).
        "old": """            return False
        return True

    def _restore_unspent_report(""",
        "new": """            return True
        return True

    def _restore_unspent_report(""",
        "selection": [LG],
        "catchers": [
            "test_a_failed_start_leaves_an_in_ring_report_still_owed[runtimeerror]",
            "test_a_failed_start_leaves_an_in_ring_report_still_owed[memoryerror]",
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed[runtimeerror]",
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed[memoryerror]",
        ],
    },
    {
        "name": "an-unspent-report-is-not-restored-in-the-ring",
        "file": LAUNCHER,
        # The slot is still in the ring, so `reported` is the one-shot
        # that was spent (wh-launch-signal-eviction.2.1).
        "old": """                signal.reported = False
""",
        "new": """                pass
""",
        "selection": [LG],
        "catchers": [
            "test_a_failed_start_leaves_an_in_ring_report_still_owed[runtimeerror]",
            "test_a_failed_start_leaves_an_in_ring_report_still_owed[memoryerror]",
        ],
    },
    {
        "name": "an-unspent-report-is-not-restored-after-eviction",
        "file": LAUNCHER,
        # The slot has gone, so the eviction debt is the one-shot that
        # was spent (wh-launch-signal-eviction.2.1).
        "old": """            self._evicted_unreported[generation] = None
""",
        "new": """            pass
""",
        "selection": [LG],
        "catchers": [
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed[runtimeerror]",
            "test_a_failed_start_leaves_an_evicted_launchs_debt_unclaimed[memoryerror]",
        ],
    },
    {
        "name": "an-undeclared-failure-strands-its-record-after-eviction",
        "file": LAUNCHER,
        "old": """                self._undeclared_startup_failures.discard(
                    provisional_generation
                )
                if name is not None and self._claim_evicted_report(
""",
        "new": """                if name is not None and self._claim_evicted_report(
""",
        "selection": [LG],
        "catchers": [
            "test_a_late_undeclared_failure_for_an_evicted_launch_still_reports",
        ],
    },
    # ---- wh-provider-switch-stale-hold: the switch drops the held -------
    # ---- bare number on both same-mode branches ------------------------
    {
        # `pass`, not a deletion: the call is the only statement in its
        # block once the comment above it goes with the pattern, and an
        # empty block is the does-not-compile false pass. The bead-id
        # comment line is part of the pattern because the call text is
        # byte-identical at all three sites; the three differ only in
        # indentation, which the pattern carries.
        "name": "the-remote-switch-keeps-the-stale-hold",
        "file": MAIN,
        "old": """                if await outgoing_engine_is_gone(
                    remote_launcher, current_provider,
                ):
                    drop_stale_bare_number_hold()
""",
        "new": """                if await outgoing_engine_is_gone(
                    remote_launcher, current_provider,
                ):
                    pass
""",
        "selection": [HOLD],
        "catchers": [
            "TestRemoteSwitchDropsTheHold::"
            "test_a_remote_switch_clears_both_bare_number_slots",
            "TestRemoteSwitchDropsTheHold::"
            "test_a_word_after_a_remote_switch_types_no_stale_number",
        ],
    },
    {
        # The confirmed-exit arm of the failed start. Same call text as
        # the success arm above, four spaces deeper.
        "name": "the-confirmed-exit-keeps-the-stale-hold",
        "file": MAIN,
        "old": """                    # (wh-provider-switch-stale-hold.1.1).
                    drop_stale_bare_number_hold()
""",
        "new": """                    # (wh-provider-switch-stale-hold.1.1).
                    pass
""",
        "selection": [HOLD],
        "catchers": [
            "TestRemoteSwitchDropsTheHold::"
            "test_a_failed_start_whose_old_engine_exits_drops_the_hold",
        ],
    },
    {
        # Renamed from the-remote-switch-drops-on-the-stop-send, whose
        # defect is no longer one edit away: the drop sits behind the
        # wait now, so reinstating the stop-send placement would take a
        # multi-block rewrite. This puts the SUCCESSOR defect in its
        # place, the one codex round 1 found -- treat a successful spawn
        # as proof the engine changed. Both discard a live hold while the
        # outgoing engine is still the user's engine, so the harm the
        # gate protects is the same
        # (wh-provider-switch-stale-hold.2.1).
        "name": "the-remote-switch-drops-on-the-spawn",
        "file": MAIN,
        "old": """                if await outgoing_engine_is_gone(
                    remote_launcher, current_provider,
                ):
                    drop_stale_bare_number_hold()
            else:
""",
        "new": """                if True:
                    drop_stale_bare_number_hold()
            else:
""",
        "selection": [HOLD],
        "catchers": [
            "TestRemoteSwitchDropsTheHold::"
            "test_a_spawned_replacement_does_not_drop_a_live_old_engine_hold",
        ],
    },
    {
        # The other half: the call sites can both stand and still drop
        # nothing if the method stops clearing. The comment line above
        # the two assignments is part of the pattern because the same
        # two-line pair appears at the same indent in the flush and the
        # consume.
        "name": "the-drop-clears-neither-slot",
        "file": PROC,
        "old": """        # named here mean. A tail held for another kind is left alone.
        self._pending_bare_number_words = None
        self._bare_number_deferred_word = None
""",
        "new": """        # named here mean. A tail held for another kind is left alone.
        pass
""",
        "selection": [HOLD],
        # wh-in-process-capture-removal deleted the two
        # TestInProcessSwitchDropsTheHold catchers this entry also named.
        # The two below were measured under this mutation after that
        # deletion and both still fail on their own assertions, so the
        # entry keeps a catcher of its own (Boss e7 ruling (4)).
        "catchers": [
            "TestRemoteSwitchDropsTheHold::"
            "test_a_remote_switch_clears_both_bare_number_slots",
            "TestRemoteSwitchDropsTheHold::"
            "test_a_word_after_a_remote_switch_types_no_stale_number",
        ],
    },
    # ---- wh-launch-addressed-notices: the dismiss names its launch ------
    # ---- and the GUI drops one addressed to a replaced launch -----------
    #
    # One working dialog is shared by every provider, so a monitor for a
    # launch the user has already replaced could dismiss the
    # replacement's loading display. The chain runs launcher -> main's
    # queue callbacks -> the GUI's queue reader -> WorkingDialog, and a
    # break anywhere along it leaves the decision inert with every other
    # test still green. gui.py joins this gate as its eighth source for
    # that reason: the decision itself lives there.
    {
        "name": "the-dialog-forgets-which-launch-raised-it",
        "file": GUI,
        "old": """        self._owner = owner
        self._base_message = message
""",
        "new": """        self._owner = None
        self._base_message = message
""",
        "selection": [DLG],
        # The catchers changed with the rule, and had to
        # (wh-dialog-ownership-token.1.1). The pair that stood here --
        # a-replaced-launch-is-dropped and the-animation-still-runs --
        # asserted that a dialog stays UP under a stale dismiss. A
        # dialog the mutation leaves unowned drops that dismiss too
        # ("stt:1" matches neither None nor STARTUP_OWNER), so both
        # produced the same visible outcome mutated and unmutated and
        # the entry survived its own sweep. These two assert the
        # opposite outcomes -- one that an owner's own dismiss CLOSES
        # the dialog, one that an unnamed dismiss LEAVES it up -- and a
        # forgotten owner inverts each of them, on its own assertion
        # rather than on an exception upstream of it.
        "catchers": [
            "TestWorkingDialog::"
            "test_a_dismiss_from_the_launch_that_owns_the_dialog_applies",
            "TestWorkingDialog::"
            "test_a_dismiss_naming_no_operation_leaves_an_owned_dialog_up",
        ],
    },
    {
        # The whole three-clause guard, neutralised rather than deleted:
        # `return` is the only statement in the block, and an empty
        # block is the does-not-compile false pass.
        "name": "a-superseded-dismiss-closes-the-dialog-anyway",
        "file": GUI,
        # Refreshed for wh-dialog-ownership-token, which rewrote the
        # guard. The behaviour is unchanged and the entry is not: a
        # dismiss addressed to a replaced launch is still dropped.
        "old": """        if owner != self._owner and self._owner != STARTUP_OWNER:
            return
""",
        "new": """        if False:
            return
""",
        "selection": [DLG],
        "catchers": [
            "TestWorkingDialog::"
            "test_a_dismiss_addressed_to_a_replaced_launch_is_dropped",
            "TestWorkingDialog::"
            "test_a_dropped_dismiss_leaves_the_animation_running",
        ],
    },
    # Two entries stood here and are retired, not repaired, by
    # wh-dialog-ownership-token. They were an-unstamped-dismiss-is-
    # dropped and a-dismiss-against-an-unowned-dialog-is-dropped, and
    # each protected one clause of the old three-clause guard: that a
    # dismiss naming nothing closed whatever was up, and that a dismiss
    # naming a launch closed a dialog nobody owned. That pair IS the
    # defect this bead removes -- an AI request finishing closed the
    # "Loading <engine>" dialog of a provider switch started after it.
    # Their catchers, test_an_unstamped_dismiss_applies_to_a_stamped_
    # dialog and test_a_stamped_dismiss_applies_to_an_unstamped_dialog,
    # are the two tests A4 required replacing, so no repair was
    # available: there is no wording of those mutations that still
    # protects a behaviour the code has. The replacement rule is
    # covered by a-dismiss-naming-no-operation-closes-an-owned-dialog
    # and the-startup-plaque-is-owned-like-an-operation below. Recorded
    # here rather than deleted quietly, because a gate that loses
    # entries without a reason is how coverage disappears unnoticed.
    # No entry for the owner clear at the end of `hide_working`
    # (`self._owner = None`). Replacing it with `pass`
    # survives: `show_working` assigns the owner unconditionally on
    # every raise, so nothing can read a left-behind owner, and two
    # dismisses in a row differ in nothing observable -- the dialog is
    # already hidden and the dot timer already stopped. Measured, not
    # assumed: the mutation was applied and all 128 tests in
    # tests/test_gui.py passed, `test_the_owner_does_not_outlive_the_
    # dialog_it_owned` among them. That test is a boundary check that is
    # green before and after, not a red-first guard, and the clear is
    # defence in depth against a future `show_working` that stops
    # assigning. A standing survivor here would fail every sweep, so it
    # is recorded as this comment instead (wh-launch-addressed-notices).
    #
    # Re-measured for wh-dialog-ownership-token, which rewrote the rule
    # this sits inside, because a claim about current behaviour stops
    # being evidence the moment the behaviour around it changes. The
    # mutation was applied again and all 152 tests in tests/test_gui.py
    # and tests/test_shared_dialog_owner.py passed, so it is still a
    # standing survivor and still belongs in a comment rather than in
    # the sweep. It survives for the same reason as before: the clear
    # only matters if `show_working` ever stops assigning on every
    # raise, and it still assigns.
    {
        "name": "the-show-dispatch-drops-the-launch",
        "file": GUI,
        "old": """                    self.working_dialog.show_working(
                        message.get("message", "Working"),
                        message.get("owner"),
                    )
""",
        "new": """                    self.working_dialog.show_working(
                        message.get("message", "Working"),
                    )
""",
        "selection": [GQ],
        "catchers": [
            "TestCheckQueuesAndEvents::"
            "test_a_show_working_message_carries_its_launch_to_the_dialog",
        ],
    },
    {
        "name": "the-hide-dispatch-drops-the-launch",
        "file": GUI,
        "old": """                    self.working_dialog.hide_working(message.get("owner"))
""",
        "new": """                    self.working_dialog.hide_working()
""",
        "selection": [GQ],
        "catchers": [
            "TestCheckQueuesAndEvents::"
            "test_a_hide_working_message_carries_its_launch_to_the_dialog",
        ],
    },
    {
        "name": "the-queued-show-message-drops-the-launch",
        "file": MAIN,
        "old": """                "action": "show_working",
                "message": message,
                "owner": launch_owner_token(generation),
""",
        "new": """                "action": "show_working",
                "message": message,
""",
        "selection": [LG],
        "catchers": [
            "TestTheQueueCallbacksForwardTheLaunch::"
            "test_the_show_message_carries_the_launch",
        ],
    },
    {
        "name": "the-queued-dismiss-message-drops-the-launch",
        "file": MAIN,
        "old": """                "action": "hide_working",
                "owner": launch_owner_token(generation),
""",
        "new": """                "action": "hide_working",
""",
        "selection": [LG],
        "catchers": [
            "TestTheQueueCallbacksForwardTheLaunch::"
            "test_the_dismiss_message_carries_the_launch",
        ],
    },
    # ---- wh-dialog-ownership-token: a dismiss must NAME the owner -------
    #
    # The rule above stopped a REPLACED LAUNCH dismissing a replacement.
    # It did not stop an operation that named nothing at all, because it
    # dropped a dismiss only when the two sides contradicted each other.
    # An AI request finishing therefore closed the "Loading <engine>"
    # dialog of a provider switch started after it, and the engine went
    # on starting with nothing on screen to say so. A1 to A4 of
    # wh-dialog-ownership-token are the four mutations below plus the
    # token-source pair after them.
    {
        # A2 and A3. The whole rule back to what it was: a dismiss
        # naming nothing closes whatever is up.
        "name": "a-dismiss-naming-no-operation-closes-an-owned-dialog",
        "file": GUI,
        "old": """        if owner != self._owner and self._owner != STARTUP_OWNER:
            return
""",
        "new": """        if (
            owner is not None
            and self._owner is not None
            and owner != self._owner
        ):
            return
""",
        "selection": [DLG],
        "catchers": [
            "TestWorkingDialog::"
            "test_a_dismiss_naming_no_operation_leaves_an_owned_dialog_up",
            "TestWorkingDialog::"
            "test_a_dropped_dismiss_from_another_operation_leaves_the_animation",
        ],
    },
    {
        # A3, the other direction: without the placeholder clause the
        # startup plaque is owned like any operation, and the dismiss
        # that ends startup -- which carries no launch whenever no
        # launch stamped the connection -- is dropped against it. The
        # plaque then stays on screen for the rest of the session.
        "name": "the-startup-plaque-is-owned-like-an-operation",
        "file": GUI,
        "old": """        if owner != self._owner and self._owner != STARTUP_OWNER:
""",
        "new": """        if owner != self._owner:
""",
        "selection": [DLG],
        "catchers": [
            "TestWorkingDialog::"
            "test_a_dismiss_naming_no_operation_closes_the_startup_plaque",
            "TestWorkingDialog::"
            "test_a_launchs_dismiss_closes_the_startup_plaque",
        ],
    },
    {
        # A1. The startup raise names no operation, so the plaque is
        # owned by nothing and the first stamped dismiss is dropped
        # against it.
        "name": "the-gui-startup-raise-names-no-operation",
        "file": GUI,
        "old": """        self.working_dialog.show_working("Starting", STARTUP_OWNER)
""",
        "new": """        self.working_dialog.show_working("Starting")
""",
        "selection": [GWD],
        "catchers": [
            "TestGuiManagerWorkingDialog::"
            "test_the_startup_raise_names_the_startup_placeholder",
        ],
    },
    {
        # A1 and A4. The AI request raises a dialog it does not name, so
        # its own close cannot match it.
        "name": "the-ai-request-raise-names-no-operation",
        "file": ACTIONS,
        "old": """                {"action": "show_working", "message": "Asking...", "owner": owner}
""",
        "new": """                {"action": "show_working", "message": "Asking..."}
""",
        "selection": [AI],
        "catchers": [
            "TestAskAIAction::"
            "test_returns_trimmed_reply_and_shows_working_indication",
        ],
    },
    {
        # A3 and A4, and the reported defect itself: the close names
        # nothing, so it dismisses whatever is up -- including a
        # provider switch's dialog raised after this request started.
        # Every exit runs it, which is why the timeout, the not-ok and
        # the over-cap tests all catch it beside the success one.
        # The pattern carries the comment line ABOVE the clear, not the
        # close alone. Commit 3 gave the text transform the same close,
        # and wh-cancel-fix-running-rewrite.1.1 (2061ad52) gave it the
        # same cancel_requested clear too, so the two code lines match
        # two sites; a two-match pattern mutates whichever comes first
        # while claiming to test this one. The ask_ai comment line is
        # what tells the sites apart.
        "name": "the-ai-request-close-names-no-operation",
        "file": ACTIONS,
        "old": """                # prevent stale cancellation from pre-cancelling the next fix.
                ai.cancel_requested = False
                self._send_gui_action({"action": "hide_working", "owner": owner})
""",
        "new": """                # prevent stale cancellation from pre-cancelling the next fix.
                ai.cancel_requested = False
                self._send_gui_action({"action": "hide_working"})
""",
        "selection": [AI],
        "catchers": [
            "TestAskAIAction::"
            "test_returns_trimmed_reply_and_shows_working_indication",
            "TestAskAIAction::"
            "test_not_ok_server_response_raises_and_logs_its_reason",
            "TestAskAIAction::"
            "test_request_timeout_raises_and_logs_its_reason",
            "TestAskAIAction::"
            "test_reply_over_output_cap_raises_and_logs_its_reason",
        ],
    },
    {
        # A1 and A3 for the other AI path. The text transform raises a
        # dialog it does not name, so its own close cannot match it and
        # the dialog stays up for the rest of the session.
        "name": "the-text-transform-raise-names-no-operation",
        "file": ACTIONS,
        "old": """                        "message": f"{working_word}...",
                        "owner": owner,
""",
        "new": """                        "message": f"{working_word}...",
""",
        "selection": [XFM],
        "catchers": [
            "TestTheSequenceIsNotSpecificToCorrecting::"
            "test_the_given_working_word_is_displayed_and_shown",
            "TestTheSequenceIsNotSpecificToCorrecting::"
            "test_the_transform_names_itself_on_both_messages",
            "TestTheSequenceIsNotSpecificToCorrecting::"
            "test_two_transforms_do_not_share_a_token",
        ],
    },
    {
        # A3, and the half of the reported defect that the ask_ai fix
        # alone did not reach. The transform's close names nothing, so
        # it dismisses whatever is up. The finally runs on paths that
        # never raised a dialog -- a failed copy, an empty selection --
        # which is why the aborted-transform test catches it too.
        "name": "the-text-transform-close-names-no-operation",
        "file": ACTIONS,
        "old": """                self._send_gui_action({"action": "hide_working", "owner": owner})
                # wh-review-pattern-fixes.28: collapse the fallback-armed
""",
        "new": """                self._send_gui_action({"action": "hide_working"})
                # wh-review-pattern-fixes.28: collapse the fallback-armed
""",
        "selection": [XFM],
        "catchers": [
            "TestTheSequenceIsNotSpecificToCorrecting::"
            "test_the_given_working_word_is_displayed_and_shown",
            "TestTheSequenceIsNotSpecificToCorrecting::"
            "test_the_transform_names_itself_on_both_messages",
            "TestTheSequenceIsNotSpecificToCorrecting::"
            "test_an_aborted_transform_still_names_itself",
        ],
    },
    {
        # A1, within one source. Every AI request sharing one token
        # means a request that finishes late dismisses a later one's
        # dialog -- the same defect, one source in.
        "name": "every-ai-request-shares-one-token",
        "file": OWNER,
        "old": """    return f"ai:{uuid.uuid4()}"
""",
        "new": """    return "ai:0"
""",
        "selection": [TOK],
        "catchers": [
            "TestTokensCannotCollideAcrossSources::"
            "test_two_ai_requests_never_share_a_token",
            "TestTokensCannotCollideAcrossSources::"
            "test_many_ai_requests_never_share_a_token",
        ],
    },
    {
        # A1, across sources. Without the prefix the launch token is the
        # bare generation again, which is what could collide with a
        # second counter's value.
        "name": "the-launch-token-loses-its-source",
        "file": OWNER,
        "old": """    return f"stt:{generation}"
""",
        "new": """    return f"{generation}"
""",
        "selection": [TOK, LG],
        "catchers": [
            "TestTokensCannotCollideAcrossSources::"
            "test_a_launch_token_names_its_source",
            "TestTheQueueCallbacksForwardTheLaunch::"
            "test_the_show_message_carries_the_launch",
        ],
    },
    {
        # A1. Naming no launch must keep meaning "this message names no
        # operation". A token here would make an unstamped provider
        # message claim a dialog, and the AI text transform's own
        # dismiss -- which also names nothing -- would stop matching it.
        "name": "a-caller-with-no-launch-gets-a-token-anyway",
        "file": OWNER,
        "old": """    if generation is None:
        return None
""",
        "new": """    if generation is None:
        return "stt:none"
""",
        "selection": [TOK, LG],
        "catchers": [
            "TestTokensCannotCollideAcrossSources::"
            "test_a_caller_with_no_launch_names_no_operation",
            "TestTheQueueCallbacksForwardTheLaunch::"
            "test_a_caller_that_names_no_launch_sends_none",
        ],
    },
    # The forwarding inside `RemoteSTTLauncher._hide_working` and
    # `_show_working` had no catcher when the dialog half was written:
    # every test in TestTheDialogNamesTheLaunchItBelongsTo replaced
    # those two methods with a MagicMock and read the argument at the
    # CALL SITE, so the body never ran, and the seven tests that did
    # register a hide callback (all in tests/test_remote_stt_launcher.py,
    # found by grepping tests/ for set_working_callback,
    # _hide_working_callback and _show_working_callback) discarded the
    # number. Commit 698f4a08 added two tests that register a real
    # callback and read what it receives, so the forwarding does have
    # entries now: the-launcher-dismiss-forwarding-drops-the-launch and
    # the-launcher-show-forwarding-drops-the-launch, the last two in
    # this list (wh-launch-addressed-notices).
    {
        # A MOVE, not a copy: the call is deleted from after the stamp
        # and inserted before the lock block that makes the launch, so
        # the mutant still holds exactly one of it. The pattern has to
        # span both positions because the runner does one replacement
        # per mutation. The old position this reinstates -- immediately
        # before `proc = None` -- cannot be reached by one replacement
        # without carrying the whole command-building section as
        # pattern, and `generation` is not even bound that early, so the
        # mutant would die on UnboundLocalError instead of the
        # assertion. Just before the lock is the same defect with the
        # same argument: the dialog names a launch that does not exist
        # yet, and `generation` is None there
        # (wh-launch-addressed-notices). Refreshed for
        # wh-gate-remote-stt-patterns-repair: the lock block now opens
        # with the watchdog retry's own check, so the pattern carries
        # that check to keep the insertion point before the lock.
        "name": "the-dialog-is-raised-before-its-launch-exists",
        "file": LAUNCHER,
        "old": """            with self._launch_signals_lock:
                if _watchdog_generation is not None:
                    previous = self._launch_signal(_watchdog_generation)
                    if (
                        self._launch_generations.get(provider_name) != _watchdog_generation
                        or self._watchdog_shutdown or previous is None
                        or previous.stop_requested
                    ):
                        return False
                    # This callback only compares/writes StateManager's record
                    # under its record lock. Publish BEFORE a fast failed spawn
                    # can report the new generation. No notification, queue put,
                    # process operation or await is allowed in this callback.
                    if _on_restarting is None or not _on_restarting(self._launch_generation_counter + 1):
                        return False
                self._launch_generation_counter += 1
                generation = self._launch_generation_counter
                self._launch_generations[provider_name] = generation
                self._current_launch_generation = generation
                self._provider_ready_event.clear()
                self._provider_startup_failed = False
                new_signal = self._open_launch_signal(generation)
                new_signal.watchdog_retry_used = _watchdog_generation is not None
                # The startup monitor holds this slot for its whole
                # bounded wait, but it does not begin until after the
                # Popen and the port-file write below. Taking the hold
                # here, under the lock that stamps the launch, is what
                # stops the cap dropping the slot across that span
                # (wh-launch-signal-eviction). Released by
                # `_monitor_startup`'s `finally`, or by the handler
                # below on the one path that returns without a monitor.
                self._claim_launch_signal(generation)

            # The dialog is raised here rather than before the stamp,
            # because it now names the launch it belongs to and that
            # launch does not exist until the block above. Nothing
            # between the old position and this one blocks -- the
            # command list and, when wake words are on, one path
            # resolution -- so the dialog still appears before the
            # spawn. A failure earlier than this leaves the dialog
            # unraised and the dismiss below unstamped, which is what
            # the same failure did before: the dialog it had just
            # raised was the replacement's, and hiding it left the same
            # empty screen (wh-launch-addressed-notices).
            self._show_working(f"Loading {display_name}", generation)
""",
        "new": """            self._show_working(f"Loading {display_name}", generation)

            with self._launch_signals_lock:
                if _watchdog_generation is not None:
                    previous = self._launch_signal(_watchdog_generation)
                    if (
                        self._launch_generations.get(provider_name) != _watchdog_generation
                        or self._watchdog_shutdown or previous is None
                        or previous.stop_requested
                    ):
                        return False
                    if _on_restarting is None or not _on_restarting(self._launch_generation_counter + 1):
                        return False
                self._launch_generation_counter += 1
                generation = self._launch_generation_counter
                self._launch_generations[provider_name] = generation
                self._current_launch_generation = generation
                self._provider_ready_event.clear()
                self._provider_startup_failed = False
                new_signal = self._open_launch_signal(generation)
                new_signal.watchdog_retry_used = _watchdog_generation is not None
                self._claim_launch_signal(generation)
""",
        "selection": [LG],
        "catchers": [
            "TestTheDialogNamesTheLaunchItBelongsTo::"
            "test_the_loading_dialog_names_the_launch_it_was_raised_for",
        ],
    },
    {
        # The two dismiss puts in this file are byte-identical, so the
        # pattern carries the `_sending_launch_is_current` gate above
        # this one to tell them apart.
        "name": "the-startup-failed-dismiss-drops-the-launch",
        "file": WS,
        "old": """                            if (
                                self._sending_launch_is_current(websocket)
                                and self.state_manager
                                and hasattr(self.state_manager, 'state_to_gui_queue')
                            ):
                                try:
                                    self.state_manager.state_to_gui_queue.put_nowait({
                                        "action": "hide_working",
                                        "owner": launch_owner_token(
                                            self._stt_client_generations.get(websocket)
                                        ),
                                    })
""",
        "new": """                            if (
                                self._sending_launch_is_current(websocket)
                                and self.state_manager
                                and hasattr(self.state_manager, 'state_to_gui_queue')
                            ):
                                try:
                                    self.state_manager.state_to_gui_queue.put_nowait({
                                        "action": "hide_working",
                                    })
""",
        "selection": [WSN],
        "catchers": [
            "TestNotificationHandling::"
            "test_startup_failed_kind_signals_failure_and_still_toasts",
        ],
    },
    {
        # The ready branch's dismiss, told from the startup_failed one
        # by the `continue` that follows it.
        "name": "the-ready-dismiss-drops-the-launch",
        "file": WS,
        "old": """                                        "action": "hide_working",
                                        "owner": launch_owner_token(
                                            self._stt_client_generations.get(websocket)
                                        ),
                                    })
                                except Exception:
                                    pass
                            continue  # Working dialog dismissal is sufficient; skip toast
""",
        "new": """                                        "action": "hide_working",
                                    })
                                except Exception:
                                    pass
                            continue  # Working dialog dismissal is sufficient; skip toast
""",
        "selection": [WSN],
        "catchers": [
            "TestNotificationHandling::"
            "test_ready_kind_dismisses_the_dialog_naming_its_own_launch",
        ],
    },
    # ---- The notice half: the launch is read at delivery -----------------
    # The dialog half moves its decision to the GUI. The notice has no
    # such ordering point, so `_notify` asks again immediately before it
    # calls the callback (wh-launch-addressed-notices, Option A).
    {
        "name": "the-notice-skips-its-delivery-time-check",
        "file": LAUNCHER,
        # Refreshed for wh-gate-remote-stt-patterns-repair: the check
        # now reads a `selected` answer that also covers a per-provider
        # notice, so disabling the drop disables it for both kinds.
        "old": """            if not selected:
""",
        "new": """            if False:
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_notice_for_a_replaced_launch_is_dropped",
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_switch_between_the_gate_and_the_toast_drops_the_notice",
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_dropped_notice_names_both_launches",
        ],
    },
    {
        # The same line read the other way round, so a notice that names
        # no launch is refused instead of delivered.
        "name": "the-notice-check-drops-an-unstamped-notice",
        "file": LAUNCHER,
        # Refreshed for wh-gate-remote-stt-patterns-repair: the
        # launch-wide answer is now the `else` arm of `selected`.
        "old": """                        else self.launch_is_current(generation))
""",
        "new": """                        else generation == self._current_launch_generation)
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_notice_that_names_no_launch_is_delivered",
        ],
    },
    {
        # The check runs and the log prints, but the notice goes out.
        "name": "the-dropped-notice-is-delivered-anyway",
        "file": LAUNCHER,
        "old": """                return
            try:
                self._notify_callback(title, message)
""",
        "new": """                pass
            try:
                self._notify_callback(title, message)
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_notice_for_a_replaced_launch_is_dropped",
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_switch_between_the_gate_and_the_toast_drops_the_notice",
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_dropped_notice_names_both_launches",
        ],
    },
    {
        "name": "the-dropped-notice-is-silent",
        "file": LAUNCHER,
        "old": """                logger.info(
                    f"Dropped the notice for launch generation "
                    f"{generation} ({title}: {message}): launch "
                    f"{self._current_launch_generation} is current now"
                )
""",
        "new": """                pass
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_a_dropped_notice_names_both_launches",
        ],
    },
    {
        "name": "the-dead-child-notice-drops-its-launch",
        "file": LAUNCHER,
        "old": """                self._notify(
                    display_name,
                    "Failed to start - try restarting Wheelhouse",
                    generation,
                )
""",
        "new": """                self._notify(
                    display_name,
                    "Failed to start - try restarting Wheelhouse",
                    None,
                )
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_the_dead_child_branch_names_its_launch",
        ],
    },
    {
        # This call and the one in start_provider are byte-identical, so
        # each pattern carries the `_provider_stopped` line under it.
        "name": "the-late-death-notice-drops-its-launch",
        "file": LAUNCHER,
        "old": """            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
""",
        "new": """            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                None,
            )
        self._provider_stopped(
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_the_late_death_watch_names_its_launch",
        ],
    },
    {
        "name": "the-start-failure-notice-drops-its-launch",
        "file": LAUNCHER,
        # Refreshed for wh-gate-remote-stt-patterns-repair: the line
        # under the call is now the watchdog retry's branch.
        "old": """            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
            if _watchdog_generation is not None:
""",
        "new": """            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                None,
            )
            if _watchdog_generation is not None:
""",
        "selection": [LG],
        "catchers": [
            "TestTheNoticeNamesTheLaunchItBelongsTo::"
            "test_the_start_failure_names_its_launch",
        ],
    },
    {
        # The forwarding itself. Every branch test replaces these two
        # methods with a MagicMock and reads the call site, so these two
        # entries had no catcher until the tests below were written.
        "name": "the-launcher-dismiss-forwarding-drops-the-launch",
        "file": LAUNCHER,
        "old": """                self._hide_working_callback(generation)
""",
        "new": """                self._hide_working_callback(None)
""",
        "selection": [LG],
        "catchers": [
            "TestTheDialogNamesTheLaunchItBelongsTo::"
            "test_the_launcher_hands_the_dismiss_callback_its_launch",
        ],
    },
    {
        "name": "the-launcher-show-forwarding-drops-the-launch",
        "file": LAUNCHER,
        "old": """                self._show_working_callback(message, generation)
""",
        "new": """                self._show_working_callback(message, None)
""",
        "selection": [LG],
        "catchers": [
            "TestTheDialogNamesTheLaunchItBelongsTo::"
            "test_the_launcher_hands_the_show_callback_its_launch",
        ],
    },
    {
        # The guess is passed again: with the provisional test always
        # false, the else arm runs for every connection, which is the
        # pre-fix behaviour (wh-ready-connection-stamp.2).
        "name": "a-provisional-connections-ready-is-passed-again",
        "file": WS,
        "old": """                            # deadline (wh-ready-connection-stamp.2.1.1).
                            if websocket in self._stt_client_provisional:
""",
        "new": """                            # deadline (wh-ready-connection-stamp.2.1.1).
                            if False:
""",
        "selection": [WSN],
        "catchers": [
            "test_a_ready_from_an_undeclared_connection_is_dropped",
        ],
    },
    {
        # The comment line above the call is carried in the pattern
        # because `self._end_starting_state(generation)` appears five
        # times in this file; the comment makes the match unique, which
        # is the exactly-one-match rule the runner enforces. `pass`
        # rather than a deletion so the block cannot be left empty
        # (wh-ready-connection-stamp.2).
        "name": "the-slow-start-branch-never-ends-the-starting-state",
        "file": LAUNCHER,
        "old": """            # (wh-launch-generation.2.6).
            self._end_starting_state(generation)
""",
        "new": """            # (wh-launch-generation.2.6).
            pass
""",
        "selection": [LG],
        "catchers": [
            "test_the_slow_start_branch_ends_its_own_launchs_starting_state",
        ],
    },
    {
        # The over-reach mutant for criterion 3's test (b): the provisional
        # check becomes unconditional, so EVERY ready is dropped, including
        # the one from a connection that declared its provider. A drop that
        # wide would leave every launch to its deadline. The same comment
        # line as the mutation above keeps the match unique
        # (wh-ready-connection-stamp.2, boss ruling 2 of 2026-09-05).
        "name": "every-ready-is-dropped",
        "file": WS,
        "old": """                            # deadline (wh-ready-connection-stamp.2.1.1).
                            if websocket in self._stt_client_provisional:
""",
        "new": """                            # deadline (wh-ready-connection-stamp.2.1.1).
                            if True:
""",
        "selection": [WSN],
        "catchers": [
            "test_a_ready_from_a_declared_connection_still_signals",
            "test_ready_notification_signals_launcher",
        ],
    },
    # wh-ready-connection-stamp.2.2.1. The ready branch tests kind and
    # nothing else. The six entries below pin that from both sides: the
    # branch itself, and the kind the two providers that lacked one now
    # send. Without the provider pair a mutation could strip kind="ready"
    # from a provider and nothing here would notice, because this gate
    # runs in the wheelhouse virtual environment and cannot run a
    # provider's own suite.
    {
        # The revert. The kind-less substring fallback returns, and with
        # it every notice it misread: "already" contains "ready".
        "name": "the-substring-fallback-comes-back",
        "file": WS,
        "old": """                        elif self.remote_stt_launcher and kind == "ready":
""",
        "new": """                        elif self.remote_stt_launcher and (
                            kind == "ready"
                            or (not kind and "ready" in notification_message.lower())
                        ):
""",
        "selection": [WSN],
        "catchers": [
            "test_a_duplicate_hint_notice_is_not_a_ready[google]",
            "test_a_duplicate_hint_notice_is_not_a_ready[distil]",
            "test_a_duplicate_hint_notice_is_not_a_ready[parakeet]",
            "test_a_failed_hint_save_notice_is_not_a_ready[distil]",
            "test_a_failed_hint_save_notice_is_not_a_ready[parakeet]",
            "test_a_kind_less_notice_saying_ready_no_longer_signals",
        ],
    },
    {
        # The input-level change beside the revert. The branch keeps its
        # shape and tests a kind no provider sends, so no launch ever
        # completes. A test that only refused the old fallback would stay
        # green here, which is the point of running both.
        "name": "the-ready-kind-is-never-matched",
        "file": WS,
        "old": """                        elif self.remote_stt_launcher and kind == "ready":
""",
        "new": """                        elif self.remote_stt_launcher and kind == "readied":
""",
        "selection": [WSN],
        "catchers": [
            "test_each_shipped_providers_ready_completes_its_launch[google]",
            "test_each_shipped_providers_ready_completes_its_launch[distil]",
            "test_each_shipped_providers_ready_completes_its_launch[parakeet]",
            "test_ready_notification_signals_launcher",
        ],
    },
    {
        "name": "distil-sends-no-ready-kind",
        "file": DISTIL_MAIN,
        "old": """                    self.DISPLAY_NAME, "Transcription service ready",
                    kind="ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "new": """                    self.DISPLAY_NAME, "Transcription service ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "selection": [LG],
        "catchers": [
            "test_the_ready_notice_declares_kind_ready[distil_medium_en]",
        ],
    },
    {
        # The input-level sibling: the kind is present but wrong, which
        # is what drift actually looks like. A test asserting only that
        # some kind exists would pass here.
        "name": "distil-sends-the-wrong-ready-kind",
        "file": DISTIL_MAIN,
        "old": """                    kind="ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "new": """                    kind="readied",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "selection": [LG],
        "catchers": [
            "test_the_ready_notice_declares_kind_ready[distil_medium_en]",
        ],
    },
    {
        "name": "parakeet-sends-no-ready-kind",
        "file": PARAKEET_MAIN,
        "old": """                    self.display_name,
                    "Transcription service ready" + self._hotwords_notice(),
                    kind="ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "new": """                    self.display_name,
                    "Transcription service ready" + self._hotwords_notice(),
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "selection": [LG],
        "catchers": [
            "test_the_ready_notice_declares_kind_ready"
            "[sherpa_offline_parakeet_stt_server]",
        ],
    },
    {
        "name": "parakeet-sends-the-wrong-ready-kind",
        "file": PARAKEET_MAIN,
        "old": """                    kind="ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "new": """                    kind="readied",
                    capture_backend=CAPTURE_BACKEND_NAME)
""",
        "selection": [LG],
        "catchers": [
            "test_the_ready_notice_declares_kind_ready"
            "[sherpa_offline_parakeet_stt_server]",
        ],
    },
    {
        # The dismiss comes back for the provisional case: the dropped
        # ready queues hide_working with the connect-time guess before
        # its continue, which is what closed the CURRENT launch's dialog
        # on a ready the branch had just declared un-attributable. The
        # continue-then-signal pair is the only place the provisional arm
        # hands over to the rebound arm, so the match is unique
        # (wh-ready-connection-stamp.2.1.1).
        "name": "a-dropped-ready-still-dismisses-the-dialog",
        "file": WS,
        # The pattern used to run from the "continue" straight into the
        # signal call. wh-capture-winrt-required A4 put the capture-path
        # log line between them, so it stopped matching. Re-anchored on
        # the "continue" and the comment that opens the log line, which
        # is the smallest unique span that survives either edit.
        "old": """                                )
                                continue
                            # The one line in wheelhouse.log that answers
""",
        "new": """                                )
                                self.state_manager.state_to_gui_queue.put_nowait({
                                    "action": "hide_working",
                                    "owner": launch_owner_token(
                                        self._stt_client_generations.get(websocket)
                                    ),
                                })
                                continue
                            # The one line in wheelhouse.log that answers
""",
        "selection": [WSN],
        "catchers": [
            "test_a_dropped_ready_does_not_dismiss_the_dialog",
        ],
    },
    {
        # The wrong-launch mutant for criterion 3's test (d): the slow-start
        # branch ends the starting state directly instead of through
        # _end_starting_state, so it no longer asks whether its launch is
        # still current, and a replaced launch's slow start ends the
        # replacement's startup. Test (c) still passes under this mutant,
        # which is what makes (d) the test that carries it
        # (wh-ready-connection-stamp.2, boss ruling 2 of 2026-09-05).
        "name": "a-replaced-slow-start-ends-the-current-startup",
        "file": LAUNCHER,
        "old": """            # (wh-launch-generation.2.6).
            self._end_starting_state(generation)
""",
        "new": """            # (wh-launch-generation.2.6).
            self._provider_ready_event.set()
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launchs_slow_start_leaves_the_new_startup_alone",
        ],
    },
    {
        # The dead-child branch's call reverted, the same shape as the
        # slow-start revert above: the comment line makes the match
        # unique among the five calls, and `pass` keeps the block valid
        # (wh-ready-connection-stamp.2.1.2).
        "name": "the-dead-child-branch-never-ends-the-starting-state",
        "file": LAUNCHER,
        "old": """            # (wh-ready-connection-stamp.2.1.2).
            self._end_starting_state(generation)
""",
        "new": """            # (wh-ready-connection-stamp.2.1.2).
            pass
""",
        "selection": [LG],
        "catchers": [
            "test_the_dead_child_branch_ends_its_own_launchs_starting_state",
        ],
    },
    {
        # The wrong-launch mutant for the dead-child branch, the sibling
        # of a-replaced-slow-start-ends-the-current-startup: the branch
        # sets the event directly, so a replaced launch's dead child ends
        # the replacement's startup (wh-ready-connection-stamp.2.1.2).
        "name": "a-replaced-dead-child-ends-the-current-startup",
        "file": LAUNCHER,
        "old": """            # (wh-ready-connection-stamp.2.1.2).
            self._end_starting_state(generation)
""",
        "new": """            # (wh-ready-connection-stamp.2.1.2).
            self._provider_ready_event.set()
""",
        "selection": [LG],
        "catchers": [
            "test_a_replaced_launchs_dead_child_leaves_the_new_startup_alone",
        ],
    },

    # ---------------------------------------------------------------
    # wh-capture-winrt-required, the WheelHouse half of criterion A4
    #
    # This bead extends this gate rather than starting a WheelHouse one
    # of its own, for the reason the sections above give: its subject is
    # integrations/websocket_manager.py's notification block, which this
    # gate already mutates in five places, and it needs the two things
    # this runner has -- a per-mutation source file and a per-mutation
    # test selection. The provider half of A4, and all of A2 and A3,
    # live in the shared package's own gate,
    # services/stt_providers/shared/tests/
    # mutation_gate_winrt_capture_required.py; a mutation there could not
    # be checked from here, because the tests that catch it need a
    # provider's virtual environment.
    #
    # What A4 is for: on 2026-09-05 a provider captured through PortAudio
    # for hours while the shipped default is WinRT, and nothing on either
    # side of the socket said so. A working capture and a wrong capture
    # look identical from WheelHouse unless the provider is asked to name
    # the path it took.
    {
        # The line deleted outright, which is the state before A4 and the
        # state the 2026-09-05 investigation was in: a log with nothing
        # in it about which capture path the run used.
        "name": "the-ready-writes-no-capture-line",
        "file": WS,
        "old": """                            logger.info(
                                "STT provider ready: %s -- capture backend "
                                "%s (launch %s)",
                                title,
                                capture_backend or "not reported",
                                self._stt_client_generations.get(websocket),
                            )
""",
        "new": "",
        "selection": [CBL],
        "catchers": [
            "test_the_ready_writes_the_capture_backend_to_the_log",
            "test_the_line_reports_the_backend_the_provider_named",
            "test_a_ready_without_the_field_says_so_rather_than_guessing",
        ],
    },
    {
        # Worse than no line: a constant. A line that says "winrt"
        # whatever the provider reported would have stayed green all the
        # way through the PortAudio run, and would have told the person
        # reading the log the opposite of the truth. This is the mutant
        # that separates "a line exists" from "the line is a
        # measurement", which is why the tests drive a second backend
        # name rather than only the expected one.
        "name": "the-capture-line-always-says-winrt",
        "file": WS,
        "old": """                                capture_backend or "not reported",
""",
        "new": """                                "winrt",
""",
        "selection": [CBL],
        "catchers": [
            "test_the_line_reports_the_backend_the_provider_named",
            "test_a_ready_without_the_field_says_so_rather_than_guessing",
        ],
    },
    {
        # The frame field never read, so every ready reports "not
        # reported" however clearly the provider named its path. The
        # sibling of the constant above and the same class of defect: a
        # line that cannot vary is not evidence. "" rather than deleting
        # the assignment, because the name is read a few lines below and
        # a NameError there would crash upstream of the behaviour, which
        # reads as a catch while proving nothing (mutation-gate skill).
        "name": "the-capture-backend-field-is-never-read",
        "file": WS,
        "old": """                        capture_backend = data.get("capture_backend", "")
""",
        "new": """                        capture_backend = ""
""",
        "selection": [CBL],
        "catchers": [
            "test_the_ready_writes_the_capture_backend_to_the_log",
            "test_the_line_reports_the_backend_the_provider_named",
        ],
    },
    {
        # The boss's added requirement for A4, and the one mutation here
        # whose behaviour predates this bead: the fall-through to the
        # toast after the startup_failed branch
        # (wh-google-creds-file-picker.1.5). A `continue` there stops the
        # provider's refusal reaching the user, so the approved wording
        # -- "the audio package winsdk is not installed. Re-run the
        # WheelHouse installer." -- is written, sent over the socket,
        # received, and then dropped. The user is left with a dead engine
        # and no statement of why, which is exactly the state A3 was
        # written to end.
        #
        # Its guard test could not go red first for that reason. This
        # mutation is what earns it.
        "name": "a-startup-failure-never-reaches-the-toast",
        "file": WS,
        "old": """                                except Exception:
                                    pass
                        # Check if this is a "ready" notification from STT provider
""",
        "new": """                                except Exception:
                                    pass
                            continue
                        # Check if this is a "ready" notification from STT provider
""",
        "selection": [WRR],
        "catchers": [
            "test_the_refusal_text_reaches_the_user_notice_unchanged",
        ],
    },
    {
        # The quieter failure of the same claim: the notice arrives but
        # not whole. A refusal cut short keeps the sentence that names
        # the problem and loses the two that say what to do about it,
        # which is most of the value of the approved wording. This is why
        # the guard test compares the whole string instead of asserting a
        # substring.
        "name": "the-toast-truncates-the-providers-message",
        "file": WS,
        "old": (
            "_send_notification(title, notification_message)\n"
        ),
        "new": (
            "_send_notification(title, notification_message[:40])\n"
        ),
        "selection": [WRR],
        "catchers": [
            "test_the_refusal_text_reaches_the_user_notice_unchanged",
        ],
    },
]


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _clear_pycache():
    for cache in SERVICE.rglob("__pycache__"):
        for item in cache.glob("*.pyc"):
            try:
                item.unlink()
            except OSError:
                pass


def _run(selection):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q", "-rf", "-p", "no:randomly"],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=RUN_TIMEOUT_S,
    )


def _failed_names(output):
    names = set()
    for line in output.splitlines():
        if not line.startswith("FAILED "):
            continue
        # The `[case]` suffix is KEPT: different cases of one
        # parametrized test catch different mutations, and a runner that
        # cuts the suffix cannot express that. This is why every
        # parametrized case in the selection carries an explicit id with
        # no space in it -- the split on " " just above would otherwise
        # read only the first half of the id
        # (wh-launch-signal-eviction.2.2).
        names.add(line[len("FAILED "):].split(" ")[0])
    return names


def _matches(name, failed):
    return any(item.endswith(name) or name in item for item in failed)


def _collect_names(selection):
    # `-o addopts=` drops the project's junit-xml option, and `-p
    # no:cacheprovider` stops the `.pytest_cache` write. Collection then
    # produces no file, which is what lets `--check` call this and stay
    # read-only, and what stops two collections in one worktree racing
    # over the same report file (wh-launch-signal-eviction.1.3).
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *selection,
            "-q",
            "--collect-only",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=RUN_TIMEOUT_S,
    )
    names = set()
    for line in result.stdout.splitlines():
        if "::" not in line:
            continue
        # Kept whole for the same reason as in `_failed_names`.
        names.add(line.strip())
    return names


def _translate(pattern, newline):
    return pattern.replace("\n", newline) if newline != "\n" else pattern


def _check(mutations) -> int:
    """Answer whether every pattern still matches once and still parses.

    Two separate failures, reported separately. A pattern a later fix
    rewrote matches nothing (or, worse, matches twice and mutates
    somewhere the name does not claim) -- that is STALE. A pattern that
    matches once can still produce text Python cannot parse, and such a
    mutation is rejected before a single test runs, so the sweep prints
    `caught` and proves nothing -- that is NON-COMPILING, and it is the
    one this check exists for. Neither costs a test run, so this is the
    whole-gate check to reach for when a full sweep is too expensive.
    """
    stale = []
    broken = []
    missing = []
    for mutation in mutations:
        name = mutation["name"]
        path = mutation["file"]
        text = path.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        old = _translate(mutation["old"], newline)
        new = _translate(mutation["new"], newline)
        count = text.count(old)
        if count != 1:
            stale.append(f"{name}: pattern matched {count} times, expected 1")
            print(f"STALE {name}: pattern matched {count} times")
            continue
        if path.suffix != ".py":
            continue
        try:
            compile(text.replace(old, new, 1), str(path), "exec")
        except SyntaxError as e:
            broken.append(f"{name}: {e}")
            print(f"NON-COMPILING {name}: {e}")
    # The sweep refuses to start on a catcher name that no longer
    # collects, so a check that answered only for the patterns could
    # print clean over a gate that cannot run at all. Collection writes
    # no file (see `_collect_names`), so this keeps the check read-only.
    collected = _collect_names(
        sorted({item for m in mutations for item in m["selection"]})
    )
    for mutation in mutations:
        for catcher in mutation["catchers"]:
            if not _matches(catcher, collected):
                missing.append(f"{mutation['name']}: {catcher}")
                print(f"UNCOLLECTED {mutation['name']}: {catcher}")
    print(
        f"\nchecked {len(mutations)} patterns, {len(stale)} stale, "
        f"{len(broken)} that do not compile, {len(missing)} catcher "
        f"names that no longer collect"
    )
    return 0 if not stale and not broken and not missing else 1


def main() -> int:
    # A run may cover a subset: a mutation already seen to fail for the
    # right reason, whose code and catchers have not changed since, proves
    # nothing on a re-run. Name the ones to run with --only <substring>,
    # repeatable. The scope is printed either way, so a partial run cannot
    # read as a full sweep.
    wanted = [
        argv[i + 1]
        for argv in [sys.argv]
        for i, item in enumerate(argv)
        if item == "--only" and i + 1 < len(argv)
    ]
    if wanted:
        mutations = [
            m for m in MUTATIONS if any(w in m["name"] for w in wanted)
        ]
        if not mutations:
            print(f"ERROR --only matched no mutation: {', '.join(wanted)}")
            return 1
    else:
        mutations = list(MUTATIONS)

    if "--check" in sys.argv:
        # Returns above `_clear_pycache()` on purpose: the check deletes
        # no file and writes none, so it is safe to run beside anything,
        # including a test run that the sweep itself must never share a
        # worktree with. It does start one pytest process, to collect
        # test names (wh-launch-signal-eviction.1.3).
        return _check(mutations)

    _clear_pycache()

    # Read once, before anything is written, so the final check compares
    # against the state the gate started from rather than against
    # whatever the last restore happened to write.
    SOURCES.update(
        {path: path.read_bytes() for path in {m["file"] for m in mutations}}
    )

    every_selection = sorted({item for m in mutations for item in m["selection"]})
    if len(mutations) == len(MUTATIONS):
        print(f"gate: FULL SWEEP, {len(MUTATIONS)} mutations")
    else:
        print(
            f"gate: PARTIAL RUN, {len(mutations)} of {len(MUTATIONS)} "
            f"mutations; {len(MUTATIONS) - len(mutations)} skipped as "
            "already proven and unchanged"
        )
        for m in mutations:
            print(f"      running: {m['name']}")
    print(f"gate: {len(every_selection)} selections")

    print("baseline: running the unmutated suite")
    baseline = _run(every_selection)
    if baseline.returncode != 0:
        print("ERROR baseline is red; refusing to start")
        print(baseline.stdout[-4000:])
        return 1
    print("baseline: green")

    collected = _collect_names(every_selection)
    missing = [
        (m["name"], c)
        for m in mutations
        for c in m["catchers"]
        if not _matches(c, collected)
    ]
    if missing:
        for name, catcher in missing:
            print(f"ERROR {name}: expected catcher not collected: {catcher}")
        return 1
    print(f"catchers: all {sum(len(m['catchers']) for m in mutations)} names collected")

    caught = []
    survived = []
    errors = []

    for mutation in mutations:
        name = mutation["name"]
        path = mutation["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        old = _translate(mutation["old"], newline)
        new = _translate(mutation["new"], newline)

        count = text.count(old)
        if count != 1:
            errors.append(f"{name}: pattern matched {count} times, expected 1")
            print(f"ERROR {name}: pattern matched {count} times")
            continue

        mutated = text.replace(old, new, 1)
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as e:
            errors.append(f"{name}: mutated source does not compile ({e})")
            print(f"ERROR {name}: mutated source does not compile")
            continue

        path.write_bytes(mutated.encode("utf-8"))
        _clear_pycache()
        try:
            result = _run(mutation["selection"])
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            path.write_bytes(original)
            _clear_pycache()
            errors.append(f"{name}: the mutated run did not finish")
            print(f"ERROR {name}: the mutated run did not finish")
            continue
        finally:
            path.write_bytes(original)
            _clear_pycache()

        if "+++ Timeout +++" in output:
            errors.append(f"{name}: pytest's own timeout aborted the run")
            print(f"ERROR {name}: pytest's own timeout aborted the run")
            continue

        failed = _failed_names(output)
        uncaught = [c for c in mutation["catchers"] if not _matches(c, failed)]
        if uncaught:
            survived.append((name, uncaught))
            print(f"SURVIVED {name}: still passing -> {', '.join(uncaught)}")
        else:
            caught.append(name)
            print(f"caught   {name}")

    unrestored = [
        str(path.relative_to(SERVICE))
        for path, content in SOURCES.items()
        if path.read_bytes() != content
    ]
    for path in unrestored:
        print(f"ERROR {path} was not restored byte for byte")
    print(
        f"\n{len(mutations)} mutations run: {len(caught)} caught, "
        f"{len(survived)} survived, {len(errors)} errors; "
        f"{len(SOURCES) - len(unrestored)} of {len(SOURCES)} sources restored"
    )
    return 0 if (not survived and not errors and not unrestored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
