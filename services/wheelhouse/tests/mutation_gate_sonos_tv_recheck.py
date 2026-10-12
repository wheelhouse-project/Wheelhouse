"""Mutation evidence for wh-sonos-tv-recheck.

The thumb wheel must follow a Sonos speaker that starts TV audio after
Wheelhouse started. Four mechanisms carry that, and this gate breaks each one
in turn:

1. The repeated check. VolumeRouter runs the whole startup decision again
   every TV_RECHECK_INTERVAL_SECONDS seconds, and whenever a thumb-wheel
   volume command arrives while System Volume is selected.
2. The switch itself. Both volume plugins subscribe in every case and read the
   router per command, because EventBus has no unsubscribe (event_bus.py:30).
   SystemVolumePlugin sets its own device to the 50 percent slider value at
   the switch to Sonos, and only then.
3. The push-to-talk deferral. A switch during a hold waits for the release,
   because a 50 percent write during the hold would turn the speakers back up
   while the hold is still listening to them (wh-ptt-audio-override.1.4).
4. The two gaps that round 1 of the glm53 review found. A switch back during
   the same hold must end the wait (wh-sonos-tv-recheck.1.1), and the Sonos
   plugin that started in disabled mode must come back at the switch
   (wh-sonos-tv-recheck.1.2). Suite 2 above named the Sonos plugin but
   mutated only its command guard and its address read, so the revival itself
   had no mutation until suite 4.
5. The TV speaker keeps the wheel (wh-sonos-soundbar-volume). The router
   identifies the TV speaker by its x-sonos-htastream: address, then by the
   speaker already identified in this run, then by being the only visible
   soundbar across all households. It queries the known TV speaker directly
   before any network search, switches to System Volume only after two
   consecutive unanswered checks (or an internal device, no TV speaker, or
   the plugin off), treats an empty device name as not internal, and logs
   each switch with its reason. Section 5 of the table breaks each of those
   rules, and the Suite 2 and Suite 3 patterns that the rewrite made stale
   were refreshed in place.

The shared runner in services/stt_providers/shared/tests/mutation_gate_runner.py
owns pattern matching, the exactly-one-match check, the compile check, the
expected-name check, the green baseline, the per-mutation timeout, bytecode
clearing, and source restoration; this file only declares the table.

Run from services/wheelhouse with the interpreter of the checkout it sits in
(``sys.executable`` becomes the pytest launcher, see LAUNCH below):

    .venv/Scripts/python.exe tests/mutation_gate_sonos_tv_recheck.py
    .venv/Scripts/python.exe tests/mutation_gate_sonos_tv_recheck.py --check

LAUNCH is replaced for the same reason as
tests/mutation_gate_volume_router_sonos_enabled.py: the default launch goes
through ``uv run``, which builds a .venv inside a git worktree and makes the
worktree hard to delete.

Five suites, each named per mutation and each run whole, so their other
tests act as the sanity set. The counts come from
``.venv/Scripts/python.exe -m pytest <file> --collect-only -q`` on
2026-10-11:
  tests/test_handlers/test_volume_router.py          (86 tests)
  tests/test_plugins/test_system_volume_plugin.py    (69 tests)
  tests/test_plugins/test_sonos_plugin.py            (41 tests)
  tests/test_plugins/test_registry.py                (57 tests)

The shared runner strips a parameter list from both the collected and the
failed names, so every expected name below is a bare test name.
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
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

ROUTER_TEST = "tests/test_handlers/test_volume_router.py"
SYSTEM_TEST = "tests/test_plugins/test_system_volume_plugin.py"
SONOS_TEST = "tests/test_plugins/test_sonos_plugin.py"
REGISTRY_TEST = "tests/test_plugins/test_registry.py"

ROUTER = "handlers/volume_router.py"
SONOS = "plugins/sonos_plugin.py"
SYSTEM = "plugins/system_volume_plugin.py"
REGISTRY = "plugins/registry.py"

# --- expected catchers in tests/test_handlers/test_volume_router.py ---------
HALF = "test_switch_to_sonos_sets_system_volume_to_half_once"
PLUGIN_OFF = "test_sonos_plugin_off_never_switches_and_never_sets_half"
# wh-sonos-soundbar-volume: the TV audio stopping no longer causes the switch
# back (the old rule). A Windows output device that turns internal does, and
# this test keeps every switch-back mutation below caught.
BACK = "test_internal_output_device_switches_back_and_leaves_volume_alone"
WATCH = "test_speech_suppression_watch_follows_the_tv_speaker"
LOG = "test_each_switch_logs_one_info_line_with_direction_and_name"
NO_WAIT = "test_volume_command_does_not_wait_for_the_check"
TWO_CHECKS = "test_two_checks_at_once_run_one_search"
# test_one_check_runs_at_a_time guards the same property through the real
# request path and has no mutation of its own. The two early returns in
# request_tv_recheck are redundant protection: with both removed, the guard
# inside check_tv_audio_now still holds the search count at one, so no
# mutation of those two lines can change an answer a test can see.
INTERVAL = "test_the_interval_is_between_30_and_60_seconds"
LOOP_WAITS = "test_the_loop_waits_the_interval_before_each_check"
LOOP_OFF = "test_the_loop_does_not_start_when_the_sonos_plugin_is_off"
LOOP_ONCE = "test_a_second_start_does_not_add_a_second_loop"

# wh-sonos-soundbar-volume (suite 5): the TV speaker keeps the wheel.
STREAM = "test_a_stream_on_the_tv_speaker_never_switches_the_wheel"
ONE_BAR = "test_one_soundbar_and_no_tv_audio_selects_sonos"
TWO_BARS = "test_two_soundbars_and_no_tv_audio_select_system_volume"
INTERNAL_NO_SEARCH = "test_an_internal_device_name_selects_system_volume_without_a_search"
EMPTY_KEEPS = "test_an_empty_device_name_keeps_the_wheel_on_the_tv_speaker"
EMPTY_ONE_BAR = "test_an_empty_device_name_and_one_soundbar_selects_sonos"
EMPTY_STR = "test_empty_string_is_not_internal"
EMPTY_NO_SPEAKER = "test_initialize_with_empty_device_name_and_no_speaker"
ONE_MISS = "test_one_missed_check_does_not_switch_and_the_second_does"
RESET = "test_an_answer_between_two_missed_checks_resets_the_count"
OTHER_BAR = ("test_the_only_other_soundbar_does_not_replace_a_tv_speaker_that_"
             "does_not_answer")
RESET_SEARCH = "test_an_answer_found_by_the_search_resets_the_count"
HH_START = "test_the_search_finds_the_tv_speaker_in_another_household_at_startup"
HH_AFTER = ("test_the_search_finds_the_tv_speaker_in_another_household_after_a_"
            "failed_direct_query")
HH_BAR = "test_the_only_soundbar_is_found_in_another_household"
HH_TWO = "test_a_soundbar_in_each_household_is_two_soundbars"
NOT_MISS = "test_a_failed_direct_query_that_the_search_answers_is_not_a_miss"
TV_WINS = "test_a_speaker_on_tv_audio_wins_over_the_only_soundbar"
KEEP_FIRST = "test_the_first_tv_soundbar_is_kept_while_it_answers"
NO_SEARCH = "test_a_known_tv_speaker_that_answers_makes_no_network_search"
SEARCH_RUNS = "test_the_search_runs_when_the_direct_query_fails"
WHEEL_NO_SEARCH = ("test_a_thumb_wheel_check_does_not_search_while_the_tv_speaker_"
                   "answers")
STAYS_KNOWN = "test_the_tv_speaker_stays_known_while_system_volume_is_selected"
UNREADABLE = "test_a_speaker_whose_type_cannot_be_read_blocks_the_soundbar_rule"
START_LOG = "test_the_start_logs_the_target_and_the_reason"
R_TV = "test_tv_audio_found"
R_BAR = "test_only_soundbar"
R_INT = "test_output_device_internal"
R_DIS = "test_sonos_plugin_disabled"

# --- expected catchers in tests/test_plugins/test_system_volume_plugin.py ---
DEFER = "test_the_switch_during_the_hold_writes_nothing_and_logs_one_line"
RELEASE = "test_the_release_sets_50_percent_after_it_puts_the_level_back"
SECOND = "test_a_second_release_does_not_set_50_percent_again"
NO_SWITCH = "test_a_release_without_a_switch_never_sets_50_percent"
LOST = "test_a_lost_device_drops_the_waiting_switch"
AT_ONCE = "test_no_hold_sets_50_percent_at_once"
# Round 1 of the glm53 review added these two (wh-sonos-tv-recheck.1.1).
CANCEL = "test_the_switch_back_during_the_hold_cancels_the_waiting_set"
NOTHING_WAITING = "test_a_switch_back_with_nothing_waiting_writes_nothing"

# --- expected catchers in tests/test_plugins/test_sonos_plugin.py -----------
# Round 1 of the glm53 review added all three (wh-sonos-tv-recheck.1.2).
REVIVE = "test_the_switch_revives_a_plugin_that_started_in_disabled_mode"
SWITCH_BACK_DISABLED = "test_a_switch_back_leaves_a_disabled_plugin_disabled"
SECOND_SWITCH = "test_a_second_switch_adds_no_second_command_subscription"

# --- expected catchers in tests/test_plugins/test_registry.py --------------
REG_START = "test_start_all_starts_the_check_after_the_plugins_run"
REG_STOP = "test_stop_all_stops_the_check_before_the_plugins_stop"

MUTATIONS = []


def add(name, test_file, target, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=test_file,
                          file=SERVICE / target, old=old, new=new,
                          expect=list(expect)))


# ===========================================================================
# 1. The repeated check
# ===========================================================================

# The check runs the decision and then throws the answer away, which is the
# shipped behaviour before this change: one decision, at startup.
add("the-check-never-applies-its-answer", ROUTER_TEST, ROUTER,
    "            use_sonos, sonos_ip, sonos_name, reason = await self._decide_volume_target()\n"
    "            return await self._apply_volume_target(use_sonos, sonos_ip, sonos_name, reason)\n",
    "            use_sonos, sonos_ip, sonos_name, reason = await self._decide_volume_target()\n"
    "            return False\n",
    HALF, BACK, WATCH, LOG)

# A thumb-wheel turn of twenty steps would start twenty network searches.
add("every-request-starts-its-own-search", ROUTER_TEST, ROUTER,
    "        if self._check_in_progress:\n"
    "            return False\n"
    "\n"
    "        self._check_in_progress = True\n",
    "        if False:\n"
    "            return False\n"
    "\n"
    "        self._check_in_progress = True\n",
    # request_tv_recheck has its own guard on the same flag, so a test that
    # goes through it cannot see this guard. Two direct calls can.
    TWO_CHECKS)

# The second trigger of acceptance item 1 removed: a volume command no longer
# asks for a check, so a speaker that starts TV audio between two interval
# checks waits up to the whole interval.
add("the-volume-command-asks-for-no-check", ROUTER_TEST, SYSTEM,
    "        volume_router.request_tv_recheck()\n",
    "        pass\n",
    NO_WAIT)

# Acceptance item 2 broken: the command waits for the network search.
add("the-volume-command-waits-for-the-search", ROUTER_TEST, SYSTEM,
    "        volume_router.request_tv_recheck()\n\n"
    "        if not self._volume_interface:\n",
    "        await volume_router.check_tv_audio_now()\n\n"
    "        if not self._volume_interface:\n",
    NO_WAIT)

# The loop's wait is the interval, not an arbitrary value.
add("the-loop-does-not-wait-the-interval", ROUTER_TEST, ROUTER,
    "            await asyncio.sleep(self.TV_RECHECK_INTERVAL_SECONDS)\n",
    "            await asyncio.sleep(1)\n",
    LOOP_WAITS)

# Acceptance item 1 names 30 to 60 seconds.
add("the-interval-is-an-hour", ROUTER_TEST, ROUTER,
    "    TV_RECHECK_INTERVAL_SECONDS = 45\n",
    "    TV_RECHECK_INTERVAL_SECONDS = 3600\n",
    # The loop test reads the interval from the class, so it passes for any
    # value. Only the bound test can catch this one.
    INTERVAL)

# Acceptance item 4: with the plugin off, the repeated network search must not
# run either.
add("the-loop-runs-with-the-sonos-plugin-off", ROUTER_TEST, ROUTER,
    "        if not self._sonos_plugin_enabled():\n"
    '            logger.info("VolumeRouter: Sonos plugin disabled -> no TV-audio re-check")\n'
    "            return\n",
    "        if False:\n"
    '            logger.info("VolumeRouter: Sonos plugin disabled -> no TV-audio re-check")\n'
    "            return\n",
    LOOP_OFF)

# A second start_tv_recheck would leave two loops searching the network.
add("a-second-start-adds-a-second-loop", ROUTER_TEST, ROUTER,
    "        if self._recheck_task is not None and not self._recheck_task.done():\n"
    "            return\n",
    "        if False:\n"
    "            return\n",
    LOOP_ONCE)

# The registry is the only caller of the loop's start and stop.
add("the-registry-never-starts-the-loop", REGISTRY_TEST, REGISTRY,
    "            get_volume_router().start_tv_recheck()\n",
    "            pass\n",
    REG_START)

add("the-registry-never-stops-the-loop", REGISTRY_TEST, REGISTRY,
    "            await get_volume_router().stop_tv_recheck()\n",
    "            pass\n",
    REG_STOP)

# ===========================================================================
# 2. The switch
# ===========================================================================

# Acceptance item 3: the 50 percent set happens at the switch to Sonos.
HANDOVER_WRITE = (
    "            await asyncio.to_thread(\n"
    "                self._volume_interface.SetMasterVolumeLevelScalar,\n"
    "                self.SONOS_HANDOVER_VOLUME_SCALAR,\n"
    "                None,\n"
    "            )\n"
)
add("never-set-the-50-percent", SYSTEM_TEST, SYSTEM,
    HANDOVER_WRITE, "            pass\n",
    AT_ONCE, RELEASE)

add("never-set-the-50-percent-router-side", ROUTER_TEST, SYSTEM,
    HANDOVER_WRITE, "            pass\n",
    HALF)

# The slider value, not a decibel level. SetMasterVolumeLevel(0.5, None) is a
# legal call that leaves the slider near the top.
add("set-a-decibel-level-instead-of-the-slider", ROUTER_TEST, SYSTEM,
    "                self._volume_interface.SetMasterVolumeLevelScalar,\n",
    "                self._volume_interface.SetMasterVolumeLevel,\n",
    HALF)

add("the-wrong-slider-value", SYSTEM_TEST, SYSTEM,
    "    SONOS_HANDOVER_VOLUME_SCALAR = 0.5\n",
    "    SONOS_HANDOVER_VOLUME_SCALAR = 0.25\n",
    AT_ONCE, RELEASE)

# Acceptance item 5: the switch back never changes the system volume level.
# The fix for wh-sonos-tv-recheck.1.1 gave this guard a body, so the pattern
# carries the body's first comment line instead of the old bare return.
add("the-switch-back-also-sets-the-50-percent", ROUTER_TEST, SYSTEM,
    "        if not event.use_sonos:\n"
    "            # A 50 percent set can still wait for a push-to-talk release. The\n",
    "        if False:\n"
    "            # A 50 percent set can still wait for a push-to-talk release. The\n",
    BACK)

# The per-command router read in each plugin. This is the whole switch
# mechanism: EventBus has no unsubscribe, so a plugin that stops checking
# takes commands that belong to the other plugin.
add("the-sonos-plugin-takes-every-command", ROUTER_TEST, SONOS,
    "        if not get_volume_router().use_sonos:\n"
    "            return\n",
    "        if False:\n"
    "            return\n",
    BACK)

add("the-system-volume-plugin-takes-every-command", ROUTER_TEST, SYSTEM,
    "        if not volume_router.use_system_volume:\n"
    "            return\n",
    "        if False:\n"
    "            return\n",
    HALF)

# Acceptance item 6: the Sonos plugin follows the speaker that plays TV audio.
add("the-speaker-does-not-follow-the-tv-audio", ROUTER_TEST, SONOS,
    "        router_ip = get_volume_router().sonos_ip\n"
    "        if router_ip:\n"
    "            return router_ip\n"
    "        return self._configured_speaker_ip\n",
    "        return self._configured_speaker_ip or get_volume_router().sonos_ip\n",
    # Only the watch test tells the two addresses apart. It sets
    # plugins.sonos.speaker_ip to the other speaker; with no configured
    # address the mutant answers with the router's address anyway, which is
    # why the first sweep reported this mutation as a survivor.
    WATCH)

# The switch keeps the address of the speaker that plays TV audio.
add("the-switch-forgets-the-tv-speaker", ROUTER_TEST, ROUTER,
    "        if use_sonos:\n"
    "            # The TV speaker, which the Sonos plugin must use for volume and\n"
    "            # for its playback watch (acceptance item 7).\n"
    "            self._sonos_ip = sonos_ip\n"
    "            self._sonos_name = sonos_name\n",
    "        if False:\n"
    "            # The TV speaker, which the Sonos plugin must use for volume and\n"
    "            # for its playback watch (acceptance item 7).\n"
    "            self._sonos_ip = sonos_ip\n"
    "            self._sonos_name = sonos_name\n",
    HALF, WATCH)

# Acceptance item 7: one INFO line per switch, naming the direction and the
# speaker. The no-change return is what makes it one line per switch rather
# than one per check.
add("the-check-announces-a-switch-every-time", ROUTER_TEST, ROUTER,
    "        if use_sonos == self._use_sonos:\n"
    "            return False\n",
    "        if False:\n"
    "            return False\n",
    LOG)

add("the-log-line-loses-the-direction", ROUTER_TEST, ROUTER,
    '        direction = "to Sonos" if use_sonos else "to System Volume"\n',
    '        direction = "somewhere"\n',
    LOG)

add("the-log-line-loses-the-speaker-name", ROUTER_TEST, ROUTER,
    '            f"Switched thumb-wheel volume {direction}: speaker "\n'
    '            f"\'{self._sonos_name}\' (reason: {reason})"\n',
    '            f"Switched thumb-wheel volume {direction}: "\n'
    '            f"(reason: {reason})"\n',
    LOG, R_TV, R_BAR, R_INT, R_DIS, ONE_MISS)

add("the-log-line-loses-the-reason", ROUTER_TEST, ROUTER,
    '            f"Switched thumb-wheel volume {direction}: speaker "\n'
    '            f"\'{self._sonos_name}\' (reason: {reason})"\n',
    '            f"Switched thumb-wheel volume {direction}: speaker "\n'
    '            f"\'{self._sonos_name}\'"\n',
    R_TV, R_BAR, R_INT, R_DIS, ONE_MISS)

# Acceptance item 4: the plugin setting also governs the repeated check.
add("the-check-ignores-the-sonos-plugin-setting", ROUTER_TEST, ROUTER,
    "        if not self._sonos_plugin_enabled():\n"
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n',
    "        if False:\n"
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n',
    PLUGIN_OFF)

# ===========================================================================
# 3. The push-to-talk deferral
# ===========================================================================

DEFER_BRANCH = (
    "                self._pending_sonos_handover = True\n"
    "                self._pending_sonos_handover_speaker = event.speaker_name\n"
)
APPLY_AFTER_RELEASE = (
    "            if self._pending_sonos_handover:\n"
    "                self._pending_sonos_handover = False\n"
    "                speaker_name = self._pending_sonos_handover_speaker\n"
    "                self._pending_sonos_handover_speaker = None\n"
    "                await self._set_handover_volume(speaker_name)\n"
)

# The 50 percent write lands during the hold, which turns the speakers back up
# while the hold is still listening to them.
add("the-hold-does-not-defer-the-50-percent", SYSTEM_TEST, SYSTEM,
    DEFER_BRANCH,
    "                await self._set_handover_volume(event.speaker_name)\n",
    DEFER)

# The deferral never completes, so the slider never reads 50 after a switch
# that arrived during a hold.
add("the-release-never-sets-the-waiting-50-percent", SYSTEM_TEST, SYSTEM,
    APPLY_AFTER_RELEASE, "            pass\n",
    RELEASE)

# Every release sets 50 percent, whether a switch waited or not.
add("every-release-sets-the-50-percent", SYSTEM_TEST, SYSTEM,
    "            if self._pending_sonos_handover:\n"
    "                self._pending_sonos_handover = False\n",
    "            if True:\n"
    "                self._pending_sonos_handover = False\n",
    NO_SWITCH)

# The wait is not cleared, so every later release sets 50 percent again.
add("the-wait-outlives-the-release", SYSTEM_TEST, SYSTEM,
    "            if self._pending_sonos_handover:\n"
    "                self._pending_sonos_handover = False\n"
    "                speaker_name = self._pending_sonos_handover_speaker\n",
    "            if self._pending_sonos_handover:\n"
    "                speaker_name = self._pending_sonos_handover_speaker\n",
    SECOND)

# A release that finds no audio device leaves the wait set, so the next hold's
# release writes 50 percent for a switch that is long past.
add("a-lost-device-keeps-the-wait", SYSTEM_TEST, SYSTEM,
    "                self._pre_ptt_volume_db = None\n"
    "                self._pending_sonos_handover = False\n"
    "                self._pending_sonos_handover_speaker = None\n"
    "                return\n",
    "                self._pre_ptt_volume_db = None\n"
    "                return\n",
    LOST)

# Placement: the 50 percent set MOVED above the restore, so the restore's
# write of the held level overwrites it and the slider does not read 50.
RESTORE_BLOCK = (
    "            try:\n"
    "                restore_db = self._pre_ptt_volume_db\n"
    "                self._pre_ptt_volume_db = None\n"
    "                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, restore_db, None)\n"
    '                logger.info(f"[PTT] System audio restored to {restore_db:.1f}dB")\n'
    "            except Exception as e:\n"
    "                # The record on disk stays, for the same reason as above.\n"
    '                logger.error(f"[PTT] Failed to restore system audio: {e}")\n'
    "            else:\n"
    "                await self._delete_ptt_volume_record()\n"
)
COMMENT_BEFORE_APPLY = (
    "            # The switch to Sonos that arrived during the hold sets the level\n"
    "            # last, so the slider reads 50 after the release. The restore\n"
    "            # above runs first on purpose: it puts the held level back, and\n"
    "            # this set then replaces it.\n"
)
add("the-50-percent-lands-before-the-restore", SYSTEM_TEST, SYSTEM,
    RESTORE_BLOCK + "\n" + COMMENT_BEFORE_APPLY + APPLY_AFTER_RELEASE,
    APPLY_AFTER_RELEASE + "\n" + COMMENT_BEFORE_APPLY + RESTORE_BLOCK,
    RELEASE)


# ===========================================================================
# 4. The two gaps round 1 of the glm53 review found
# ===========================================================================

# wh-sonos-tv-recheck.1.1: a hold can span a switch to Sonos AND a switch
# back, because wheel commands and the repeated check both run during a hold.
# The switch back must end the wait, or the release writes 50 percent while
# System Volume is selected.
CANCEL_BLOCK = (
    "                if self._pending_sonos_handover:\n"
    "                    self._pending_sonos_handover = False\n"
    "                    self._pending_sonos_handover_speaker = None\n"
    "                    logger.info(\n"
    '                        "[PTT] The switch back to System Volume cancels the 50 "\n'
    '                        "percent set that waited for the release"\n'
    "                    )\n"
)

add("the-switch-back-keeps-the-waiting-50-percent", SYSTEM_TEST, SYSTEM,
    CANCEL_BLOCK, "                pass\n",
    CANCEL)

# The over-broad repair: cancel on every switch back. The level is untouched
# either way, so only the log line shows it. The 12-space form of the same
# condition lives in _handle_ptt_stopped, which is why the indent matters.
add("the-switch-back-cancels-when-nothing-waits", SYSTEM_TEST, SYSTEM,
    "                if self._pending_sonos_handover:\n",
    "                if True:\n",
    NOTHING_WAITING)

# wh-sonos-tv-recheck.1.2: the speaker was off when Wheelhouse started, so
# SonosPlugin.start took disabled mode. _handle_volume_target_changed is the
# only mechanism that gives the thumb wheel a speaker again.
add("the-switch-never-revives-the-sonos-plugin", SONOS_TEST, SONOS,
    "            self._sonos_available = True\n"
    "            self._speaker_ip = event.speaker_ip\n",
    "            self._speaker_ip = event.speaker_ip\n",
    REVIVE, SECOND_SWITCH)

# The same subscribe line sits in start(), so this pattern carries the two
# lines after it.
add("the-revived-plugin-never-gets-the-wheel", SONOS_TEST, SONOS,
    "            if self._event_bus:\n"
    "                self._event_bus.subscribe(VolumeAdjustCommand, self._handle_volume_adjust)\n"
    "\n"
    "        if self._state != PluginState.RUNNING:\n",
    "            pass\n"
    "\n"
    "        if self._state != PluginState.RUNNING:\n",
    REVIVE, SECOND_SWITCH)

# Acceptance item 6 wants the speech-suppression watch on the TV speaker. A
# revived plugin with no watch suppresses nothing.
add("the-revived-plugin-starts-no-watch", SONOS_TEST, SONOS,
    "            self._monitor_task = asyncio.create_task(self._monitor_playback())\n"
    '            logger.info("SonosPlugin: Started playback monitoring after the switch")\n',
    "            pass\n",
    REVIVE)

# The handler acts on every event, so the switch back takes the wheel while
# the router has System Volume selected.
add("the-switch-back-also-revives-the-sonos-plugin", SONOS_TEST, SONOS,
    "        if not event.use_sonos:\n"
    "            return\n"
    "\n"
    "        if not getattr(self, '_sonos_available', False):\n",
    "        if False:\n"
    "            return\n"
    "\n"
    "        if not getattr(self, '_sonos_available', False):\n",
    SWITCH_BACK_DISABLED)

# EventBus has no unsubscribe (event_bus.py:30), so a subscription per switch
# makes every wheel step move the speaker twice. The 12-space form of this
# condition lives in start(), which is why the indent matters.
add("the-revived-plugin-subscribes-on-every-switch", SONOS_TEST, SONOS,
    "        if not getattr(self, '_sonos_available', False):\n"
    "            self._sonos_available = True\n",
    "        if True:\n"
    "            self._sonos_available = True\n",
    SECOND_SWITCH)


# ===========================================================================
# 5. The TV speaker keeps the wheel (wh-sonos-soundbar-volume)
# ===========================================================================
# Before this change the router switched back to System Volume when the TV
# audio stopped, and it treated an empty output device name as internal. The
# rule now is in the module docstring of handlers/volume_router.py. Redundant
# protection, recorded so nobody hunts a survivor that cannot exist: item 3
# (a change of the speaker's source never switches) is held by two layers, the
# direct query (which does not read the source) and rule 1(b) (which keeps the
# speaker when the search finds it answering). Mutations 3f and 1b break one
# layer each; a mutation that broke both would need a compound edit of two
# distant places, which the shared runner's single old/new pair cannot express.

TV_DIRECT_BLOCK = (
    "        if self._tv_ip is not None and await self._tv_speaker_answers():\n"
)
TV_DIRECT_RETURN = (
    "            self._missed_checks = 0\n"
    "            return True, self._tv_ip, self._tv_name, self._tv_reason\n"
    "\n"
    "        found, tv_speaker = await self._search_for_tv_speaker()\n"
)

# --- item 1: how the search identifies the TV speaker ----------------------

# (a) removed: a speaker that plays TV audio is no longer preferred, so the
# only soundbar (Kitchen) wins over the speaker on TV audio.
add("1a-the-htastream-test-is-removed", ROUTER_TEST, ROUTER,
    "            if uri.startswith(self.HTASTREAM_URI_PREFIX):\n",
    "            if False:\n",
    TV_WINS, R_TV)

# (b) removed: the speaker identified earlier is not kept when the search finds
# it answering, so that check counts as a miss.
add("1b-the-earlier-tv-speaker-is-not-kept", ROUTER_TEST, ROUTER,
    "        if self._tv_ip is not None and self._tv_ip in track_uris:\n",
    "        if False:\n",
    NOT_MISS)

# (c) takes the first soundbar instead of the only one.
add("1c-the-first-soundbar-instead-of-the-only-one", ROUTER_TEST, ROUTER,
    "        if len(soundbars) == 1 and not unknown:\n",
    "        if len(soundbars) >= 1 and not unknown:\n",
    TWO_BARS, HH_TWO)

# (c) ignores is_soundbar: every visible speaker counts as a soundbar.
add("1c-is-soundbar-is-ignored", ROUTER_TEST, ROUTER,
    "                if await asyncio.to_thread(lambda s=speaker: s.is_soundbar):\n",
    "                if True:\n",
    ONE_BAR, R_BAR, HH_BAR)

# (c) runs while a TV speaker is known, so a short outage of that speaker lets
# the only other soundbar take its place (acceptance 10(h)).
add("1c-the-only-soundbar-replaces-the-known-tv-speaker", ROUTER_TEST, ROUTER,
    "        if self._tv_ip is not None:\n            return found, None\n",
    "        if False:\n            return found, None\n",
    OTHER_BAR)

# (c) a speaker whose type cannot be read no longer blocks the rule.
add("1c-an-unreadable-speaker-is-ignored", ROUTER_TEST, ROUTER,
    "                unknown = True\n",
    "                pass\n",
    UNREADABLE)

# The speaker stays known while System Volume is selected (rule 1(b) and the
# direct query of item 4 both need it).
add("1-the-tv-speaker-is-forgotten-at-an-internal-device", ROUTER_TEST, ROUTER,
    "            return False, None, None, self.REASON_INTERNAL\n",
    "            self._tv_ip = None\n"
    "            return False, None, None, self.REASON_INTERNAL\n",
    STAYS_KNOWN)

# --- item 2: an empty output device name is not internal --------------------

add("2-an-empty-name-counts-as-internal", ROUTER_TEST, ROUTER,
    "        if not device_name:\n"
    "            return False\n",
    "        if not device_name:\n"
    "            return True\n",
    EMPTY_STR, EMPTY_KEEPS, EMPTY_ONE_BAR, EMPTY_NO_SPEAKER)

add("2-an-internal-name-is-ignored", ROUTER_TEST, ROUTER,
    "        if self._is_internal_audio:\n",
    "        if False:\n",
    INTERNAL_NO_SEARCH, R_INT, BACK, STAYS_KNOWN)

# --- item 3: when the wheel goes to System Volume ---------------------------

add("3-one-missed-check-switches", ROUTER_TEST, ROUTER,
    "    MISSED_CHECKS_BEFORE_SWITCH = 2\n",
    "    MISSED_CHECKS_BEFORE_SWITCH = 1\n",
    ONE_MISS, RESET, RESET_SEARCH, NOT_MISS)

add("3-three-missed-checks-are-needed", ROUTER_TEST, ROUTER,
    "    MISSED_CHECKS_BEFORE_SWITCH = 2\n",
    "    MISSED_CHECKS_BEFORE_SWITCH = 3\n",
    ONE_MISS)

add("3-the-first-miss-returns-system-volume", ROUTER_TEST, ROUTER,
    "                return self._use_sonos, self._sonos_ip, self._sonos_name, None\n",
    "                return False, self._sonos_ip, self._sonos_name, self.REASON_NOT_ANSWERING\n",
    ONE_MISS, RESET, NOT_MISS)

add("3-a-miss-never-counts", ROUTER_TEST, ROUTER,
    "            self._missed_checks += 1\n",
    "            self._missed_checks += 0\n",
    ONE_MISS)

# "Consecutive": an answer ends the run of misses, whether the direct query or
# the search carried it.
add("3-a-direct-answer-does-not-reset-the-count", ROUTER_TEST, ROUTER,
    TV_DIRECT_RETURN,
    "            pass\n"
    "            return True, self._tv_ip, self._tv_name, self._tv_reason\n"
    "\n"
    "        found, tv_speaker = await self._search_for_tv_speaker()\n",
    RESET)

add("3-a-search-answer-does-not-reset-the-count", ROUTER_TEST, ROUTER,
    "            self._tv_ip, self._tv_name, self._tv_reason = tv_speaker\n"
    "            self._missed_checks = 0\n",
    "            self._tv_ip, self._tv_name, self._tv_reason = tv_speaker\n"
    "            pass\n",
    RESET_SEARCH)

# The old rule back: the answer of the direct query is "the speaker still
# plays TV audio", so a stream switches the wheel to System Volume.
add("3-a-source-change-switches", ROUTER_TEST, ROUTER,
    TV_DIRECT_RETURN,
    "            self._missed_checks = 0\n"
    "            _info = await asyncio.to_thread(\n"
    '                __import__("soco").SoCo(self._tv_ip).get_current_track_info\n'
    "            )\n"
    "            return (\n"
    '                str(_info.get("uri", "")).startswith(self.HTASTREAM_URI_PREFIX),\n'
    "                self._tv_ip, self._tv_name, self._tv_reason,\n"
    "            )\n"
    "\n"
    "        found, tv_speaker = await self._search_for_tv_speaker()\n",
    STREAM)

# --- item 4: the direct query comes first, the search only when needed ------

add("4-the-search-runs-though-the-direct-query-answers", ROUTER_TEST, ROUTER,
    TV_DIRECT_BLOCK,
    "        if self._tv_ip is not None and await self._tv_speaker_answers() and False:\n",
    NO_SEARCH, KEEP_FIRST, WHEEL_NO_SEARCH)

add("4-the-direct-query-is-never-made", ROUTER_TEST, ROUTER,
    TV_DIRECT_BLOCK,
    "        if False:\n",
    NO_SEARCH, KEEP_FIRST, WHEEL_NO_SEARCH)

add("4-the-direct-query-never-answers", ROUTER_TEST, ROUTER,
    "            return True\n"
    "        except Exception as e:\n",
    "            return False\n"
    "        except Exception as e:\n",
    NO_SEARCH, KEEP_FIRST, WHEEL_NO_SEARCH)

add("4-the-direct-query-always-answers", ROUTER_TEST, ROUTER,
    '            logger.debug(f"VolumeRouter: Direct query of {self._tv_ip} failed: {e}")\n'
    "            return False\n",
    '            logger.debug(f"VolumeRouter: Direct query of {self._tv_ip} failed: {e}")\n'
    "            return True\n",
    ONE_MISS, SEARCH_RUNS, HH_AFTER)

add("4-the-search-is-skipped-when-the-direct-query-fails", ROUTER_TEST, ROUTER,
    "        found, tv_speaker = await self._search_for_tv_speaker()\n",
    "        found, tv_speaker = (\n"
    "            ({}, None) if self._tv_ip is not None\n"
    "            else await self._search_for_tv_speaker()\n"
    "        )\n",
    SEARCH_RUNS, HH_AFTER)

# --- item 5: the search finds every household -------------------------------

add("5-the-household-scan-is-removed", ROUTER_TEST, ROUTER,
    "                *[_scan_interface(ip) for ip in local_ips], _scan_households()\n",
    "                *[_scan_interface(ip) for ip in local_ips]\n",
    HH_START, HH_AFTER, HH_BAR, NO_SEARCH, SEARCH_RUNS)

add("5-the-household-scan-is-not-multi-household", ROUTER_TEST, ROUTER,
    "                        soco.discovery.scan_network, multi_household=True\n",
    "                        soco.discovery.scan_network\n",
    # The fake answers a scan without multi_household with the first online
    # speaker's household, which here also holds the TV speaker, so only the
    # tests that read the recorded call arguments can tell.
    HH_START, HH_AFTER)

# --- item 8: the reason strings of the log line -----------------------------

add("8-the-tv-audio-reason-is-wrong", ROUTER_TEST, ROUTER,
    '    REASON_TV_AUDIO = "TV audio found"\n',
    '    REASON_TV_AUDIO = "TV audio"\n',
    R_TV, STAYS_KNOWN)

add("8-the-only-soundbar-reason-is-wrong", ROUTER_TEST, ROUTER,
    '    REASON_ONLY_SOUNDBAR = "only soundbar"\n',
    '    REASON_ONLY_SOUNDBAR = "one soundbar"\n',
    R_BAR)

add("8-the-not-answering-reason-is-wrong", ROUTER_TEST, ROUTER,
    '    REASON_NOT_ANSWERING = "speaker did not answer twice"\n',
    '    REASON_NOT_ANSWERING = "speaker did not answer"\n',
    ONE_MISS)

add("8-the-internal-reason-is-wrong", ROUTER_TEST, ROUTER,
    '    REASON_INTERNAL = "output device internal"\n',
    '    REASON_INTERNAL = "internal output"\n',
    R_INT)

add("8-the-no-tv-speaker-reason-is-wrong", ROUTER_TEST, ROUTER,
    '    REASON_NO_TV_SPEAKER = "no TV speaker"\n',
    '    REASON_NO_TV_SPEAKER = "no speaker"\n',
    START_LOG)

add("8-the-plugin-disabled-reason-is-wrong", ROUTER_TEST, ROUTER,
    '    REASON_PLUGIN_DISABLED = "Sonos plugin disabled"\n',
    '    REASON_PLUGIN_DISABLED = "plugin disabled"\n',
    R_DIS)

add("8-the-start-line-loses-the-reason", ROUTER_TEST, ROUTER,
    '            f"VolumeRouter: Thumb-wheel volume starts on {target} (reason: {reason})"\n',
    '            f"VolumeRouter: Thumb-wheel volume starts on {target}"\n',
    START_LOG)


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
