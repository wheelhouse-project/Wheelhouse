"""Mutation gate for one software step per brightness command (wh-brightness-double-dim-two-plugins).

Every BrightnessAdjustCommand carries an id. BrightnessCoordinator copies it into
the HardwareBrightnessCommand, each brightness plugin copies it into every state
and overflow event it publishes for that command, and the coordinator keeps a
record for each open command, so that one wheel step adds at most one software
dimming step however many plugins answer it. Each mutation below breaks one part
of that in one target file; every named catcher test must fail at the named
assertion for the mutation to count as caught.

Usage (run from services/wheelhouse with the wheelhouse interpreter):
    .venv/Scripts/python.exe tests/mutation_gate_brightness_one_step_per_command.py --check
    .venv/Scripts/python.exe tests/mutation_gate_brightness_one_step_per_command.py [--only NAME ...] [--log PATH]

--check verifies that every pattern matches exactly once in its target file and
that every mutant compiles, then exits. A sweep also validates the expected
catcher names, parameter ids included, with --collect-only and requires a green
baseline before the first mutation.

Exit status: 0 only when every selected mutation is caught; 1 on any survivor or
error (pattern not found, pattern ambiguous, does not compile, timeout,
suite-timeout abort, non-assertion failure, exception inside an event handler,
restore failure).

Do not run concurrently with a test suite or edits in this worktree: the gate
rewrites events.py, coordinators/brightness_coordinator.py and the three
brightness plugins while each mutation runs.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
ONE_STEP_TESTS = "tests/test_coordinators/test_brightness_coordinator_one_step_per_command.py"
PROPAGATION_TESTS = "tests/test_coordinators/test_brightness_command_id_propagation.py"
SELECTION = [ONE_STEP_TESTS, PROPAGATION_TESTS]
RUN_TIMEOUT = 180  # A clean run of the selection takes about two seconds.

COORDINATOR = "coordinators/brightness_coordinator.py"
EVENTS = "events.py"
PANEL = "plugins/internal_panel_plugin.py"
BRAVIA = "plugins/bravia_plugin.py"
SAMSUNG = "plugins/samsung_plugin.py"

# Catcher tests in ONE_STEP_TESTS (real EventBus, coordinator and plugins)
PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG = "test_panel_at_minimum_and_offline_samsung_add_one_software_step"
TWO_OFFLINE_TVS = "test_two_offline_tvs_add_one_software_step"
PANEL_IN_HARDWARE_AND_OFFLINE_TV = "test_panel_dimmed_in_hardware_and_offline_tv_add_no_software_step"
OFFLINE_ANSWER_BEFORE_PANEL_STATE = "test_offline_tv_answer_before_panel_state_still_adds_no_software_step"
BOTH_AT_MINIMUM = "test_panel_and_samsung_both_at_minimum_add_one_software_step"
OVERFLOW_AND_STATE_NOT_APPLIED = "test_tv_that_sent_an_overflow_and_a_state_did_not_apply_the_command_in_hardware"
LATE_OFFLINE_OVERFLOW = "test_offline_overflow_for_a_finished_command_applies_on_arrival"

# Catcher tests in PROPAGATION_TESTS; a parametrized case is named with its id
UNIQUE_IDS = "test_each_brightness_adjust_command_gets_its_own_integer_id"
COPIES_IDLE = "test_coordinator_copies_the_command_id_into_the_hardware_command[idle_route]"
COPIES_UNWINDING = "test_coordinator_copies_the_command_id_into_the_hardware_command[unwinding_remainder]"
PANEL_TAGS = "test_internal_panel_tags_every_answer_with_the_command_id"
PANEL_AT_MINIMUM = PANEL_TAGS + "[at_minimum]"
PANEL_AT_MAXIMUM = PANEL_TAGS + "[at_maximum]"
PANEL_SUCCESS = PANEL_TAGS + "[mid_range_success]"
PANEL_READ_NONE = PANEL_TAGS + "[startup_read_none]"
BRAVIA_TAGS = "test_bravia_tags_every_answer_with_the_command_id"
BRAVIA_OFFLINE_ON_READ = BRAVIA_TAGS + "[offline_on_read]"
BRAVIA_OFFLINE_ON_ADJUST = BRAVIA_TAGS + "[offline_on_adjust]"
BRAVIA_AT_LIMIT = BRAVIA_TAGS + "[at_limit]"
BRAVIA_SUCCESS = BRAVIA_TAGS + "[success]"
SAMSUNG_TAGS = "test_samsung_tags_every_answer_with_the_command_id"
SAMSUNG_OFFLINE = SAMSUNG_TAGS + "[offline]"
SAMSUNG_TIMEOUT = SAMSUNG_TAGS + "[timeout]"
SAMSUNG_SUCCESS = SAMSUNG_TAGS + "[success]"
SAMSUNG_AT_LIMIT = SAMSUNG_TAGS + "[at_hardware_limit]"

# The start of the short-summary message that names the assertion each catcher must
# fail at. Without -v pytest shortens each side of a comparison to 30 characters, and a
# message whose text holds a single quote keeps its "AssertionError: " prefix. The
# answer-list markers therefore name the assertion (the sorted list of answers, which
# starts with the named plugin), not the element that lost its id; the id of a real
# command changes with the test order, so it cannot be part of a marker.
TWO_STEPS = "assert [call(90), call(80)] == [call(90)]"
ONE_STEP_INSTEAD_OF_NONE = "assert [call(90)] == []"
NO_STEP_INSTEAD_OF_ONE = "assert [] == [call(90)]"
NO_STEP_INSTEAD_OF_SAMSUNG_STEP = "assert [] == [call(99)]"
PANEL_ANSWERS = "AssertionError: assert [('internal_p"
BRAVIA_ANSWERS = "AssertionError: assert [('bravia', '"
ARRIVALS = "AssertionError: assert [('overflow',"
UNTAGGED_ONE = "assert [None] == [4242]"
UNTAGGED_FIRST_OF_TWO = "assert [None, 4242] == [4242, 4242]"
UNTAGGED_SECOND_OF_TWO = "assert [4242, None] == [4242, 4242]"

MUTATIONS = [
    # ---- coordinators/brightness_coordinator.py ----
    {
        # The hardware command goes out without the id, so no plugin answer is
        # tagged: every overflow applies on arrival and the published command
        # carries None.
        "name": "route-drops-id",
        "target": COORDINATOR,
        "old": "        event = HardwareBrightnessCommand(delta=delta, command_id=command_id)\n",
        "new": "        event = HardwareBrightnessCommand(delta=delta)\n",
        "catchers": {
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: PANEL_ANSWERS,
            TWO_OFFLINE_TVS: BRAVIA_ANSWERS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: BRAVIA_ANSWERS,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ARRIVALS,
            BOTH_AT_MINIMUM: PANEL_ANSWERS,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
            COPIES_IDLE: "assert [(-10, None)] == ",
            COPIES_UNWINDING: "assert [(5, None)] == ",
        },
    },
    {
        # An IDLE command routes untagged: no record opens and no answer is tagged.
        "name": "idle-route-drops-id",
        "target": COORDINATOR,
        "old": "            await self._route_to_hardware(delta, command_id=event.command_id)\n",
        "new": "            await self._route_to_hardware(delta)\n",
        "catchers": {
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: PANEL_ANSWERS,
            TWO_OFFLINE_TVS: BRAVIA_ANSWERS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: BRAVIA_ANSWERS,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ARRIVALS,
            BOTH_AT_MINIMUM: PANEL_ANSWERS,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
            COPIES_IDLE: "assert [(-10, None)] == ",
        },
    },
    {
        # A CASCADED command loses its id before unwinding, so the remainder
        # routed to hardware is untagged.
        "name": "unwinding-drops-id",
        "target": COORDINATOR,
        "old": "            await self._handle_unwinding(delta, command_id=event.command_id)\n",
        "new": "            await self._handle_unwinding(delta)\n",
        "catchers": {
            COPIES_UNWINDING: "assert [(5, None)] == ",
        },
    },
    {
        # Unwinding routes the remainder of the step to hardware without the id.
        "name": "unwinding-remainder-drops-id",
        "target": COORDINATOR,
        "old": "                    await self._route_to_hardware(remaining, command_id=command_id)\n",
        "new": "                    await self._route_to_hardware(remaining)\n",
        "catchers": {
            COPIES_UNWINDING: "assert [(5, None)] == ",
        },
    },
    {
        # No record is ever found for a tagged answer, so every overflow applies
        # on arrival and nothing is held.
        "name": "record-never-opened",
        "target": COORDINATOR,
        "old": "        self._open_commands[command_id] = record\n",
        "new": "        pass\n",
        "catchers": {
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: TWO_STEPS,
            TWO_OFFLINE_TVS: TWO_STEPS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ONE_STEP_INSTEAD_OF_NONE,
            BOTH_AT_MINIMUM: TWO_STEPS,
        },
    },
    {
        # The record stays open after the publish returns, so a tagged overflow
        # that arrives later is held in a record that nothing settles. Every
        # plugin answer that exists today arrives during the publish, so only a
        # late overflow published by the test shows it.
        "name": "record-never-closed",
        "target": COORDINATOR,
        "old": "            self._open_commands.pop(command_id, None)\n",
        "new": "            pass\n",
        "catchers": {
            LATE_OFFLINE_OVERFLOW: NO_STEP_INSTEAD_OF_ONE,
        },
    },
    {
        # A held offline step applies although an at_hardware_limit step already
        # ran for the same command.
        "name": "settle-ignores-software-step",
        "target": COORDINATOR,
        "old": (
            "        if record.software_step_applied:\n"
            "            logger.debug(\n"
            '                f"Dropping device_offline overflow for command {command_id}: "\n'
        ),
        "new": (
            "        if False:\n"
            "            logger.debug(\n"
            '                f"Dropping device_offline overflow for command {command_id}: "\n'
        ),
        "catchers": {
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: TWO_STEPS,
        },
    },
    {
        # A held offline step applies although a plugin applied the command in hardware.
        "name": "settle-ignores-hardware-apply",
        "target": COORDINATOR,
        "old": "        if applied_in_hardware:\n",
        "new": "        if False:\n",
        "catchers": {
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ONE_STEP_INSTEAD_OF_NONE,
        },
    },
    {
        # A plugin that sent an overflow as well as a state counts as having
        # applied the command in hardware. Only a plugin whose overflow added no
        # step shows it: Bravia's at-limit overflow rejected by the at_min check.
        "name": "applied-ignores-overflow-plugins",
        "target": COORDINATOR,
        "old": "        applied_in_hardware = record.state_plugins - record.overflow_plugins\n",
        "new": "        applied_in_hardware = record.state_plugins\n",
        "catchers": {
            OVERFLOW_AND_STATE_NOT_APPLIED: NO_STEP_INSTEAD_OF_SAMSUNG_STEP,
        },
    },
    {
        # A tagged state never marks its plugin as having answered the command.
        "name": "state-not-recorded",
        "target": COORDINATOR,
        "old": "            record.state_plugins.add(plugin_name)\n",
        "new": "            pass\n",
        "catchers": {
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ONE_STEP_INSTEAD_OF_NONE,
        },
    },
    {
        # A tagged overflow never marks its plugin as having overflowed, with the
        # same effect as applied-ignores-overflow-plugins.
        "name": "overflow-not-recorded",
        "target": COORDINATOR,
        "old": "            record.overflow_plugins.add(plugin_name)\n",
        "new": "            pass\n",
        "catchers": {
            OVERFLOW_AND_STATE_NOT_APPLIED: NO_STEP_INSTEAD_OF_SAMSUNG_STEP,
        },
    },
    {
        # A tagged device_offline overflow applies on arrival instead of waiting
        # for every plugin to answer.
        "name": "offline-not-held",
        "target": COORDINATOR,
        "old": (
            '        if reason == "device_offline":\n'
            "            if record is not None:\n"
        ),
        "new": (
            '        if reason == "device_offline":\n'
            "            if False:\n"
        ),
        "catchers": {
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: TWO_STEPS,
            TWO_OFFLINE_TVS: TWO_STEPS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ONE_STEP_INSTEAD_OF_NONE,
        },
    },
    {
        # Every at_hardware_limit overflow of one command applies.
        "name": "limit-not-once-per-command",
        "target": COORDINATOR,
        "old": (
            "            if record.software_step_applied:\n"
            "                logger.debug(\n"
            "                    f\"Dropping at_hardware_limit overflow from '{plugin_name}' for command \"\n"
        ),
        "new": (
            "            if False:\n"
            "                logger.debug(\n"
            "                    f\"Dropping at_hardware_limit overflow from '{plugin_name}' for command \"\n"
        ),
        "catchers": {
            BOTH_AT_MINIMUM: TWO_STEPS,
        },
    },
    {
        # An at_hardware_limit step runs without marking the command, so a held
        # offline step for the same command also applies.
        "name": "limit-step-not-marked",
        "target": COORDINATOR,
        "old": "            record.software_step_applied = True\n",
        "new": "            pass\n",
        "catchers": {
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: TWO_STEPS,
            BOTH_AT_MINIMUM: TWO_STEPS,
        },
    },
    # ---- events.py ----
    {
        # Every command is untagged, so every overflow applies on arrival.
        "name": "adjust-id-default-none",
        "target": EVENTS,
        "old": "    command_id: Optional[int] = field(default_factory=_brightness_command_ids.__next__)\n",
        "new": "    command_id: Optional[int] = None\n",
        "catchers": {
            UNIQUE_IDS: "AssertionError: (None, None)",
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: TWO_STEPS,
            TWO_OFFLINE_TVS: TWO_STEPS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ONE_STEP_INSTEAD_OF_NONE,
            BOTH_AT_MINIMUM: TWO_STEPS,
        },
    },
    {
        # Every command gets the same id.
        "name": "adjust-ids-not-unique",
        "target": EVENTS,
        "old": "_brightness_command_ids = itertools.count(1)\n",
        "new": "_brightness_command_ids = itertools.repeat(1)\n",
        "catchers": {
            UNIQUE_IDS: "assert 1 != 1",
        },
    },
    # ---- plugins/internal_panel_plugin.py ----
    {
        "name": "panel-offline-drops-id",
        "target": PANEL,
        "old": '                        event.delta, "device_offline", command_id=event.command_id)\n',
        "new": '                        event.delta, "device_offline")\n',
        "catchers": {
            PANEL_READ_NONE: UNTAGGED_ONE,
        },
    },
    {
        "name": "panel-minimum-state-drops-id",
        "target": PANEL,
        "old": (
            "                self._current_brightness = 0\n"
            "                await self._publish_brightness_state(command_id=event.command_id)\n"
        ),
        "new": (
            "                self._current_brightness = 0\n"
            "                await self._publish_brightness_state()\n"
        ),
        "catchers": {
            PANEL_AT_MINIMUM: UNTAGGED_FIRST_OF_TWO,
        },
    },
    {
        "name": "panel-minimum-overflow-drops-id",
        "target": PANEL,
        "old": (
            '                    overflow_delta, "at_hardware_limit", command_id=event.command_id)\n'
            '                logger.debug(f"Hardware at minimum (0%), overflow cascade: {overflow_delta}")\n'
        ),
        "new": (
            '                    overflow_delta, "at_hardware_limit")\n'
            '                logger.debug(f"Hardware at minimum (0%), overflow cascade: {overflow_delta}")\n'
        ),
        "catchers": {
            PANEL_AT_MINIMUM: UNTAGGED_SECOND_OF_TWO,
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: PANEL_ANSWERS,
            BOTH_AT_MINIMUM: PANEL_ANSWERS,
        },
    },
    {
        "name": "panel-maximum-state-drops-id",
        "target": PANEL,
        "old": (
            "                self._current_brightness = 100\n"
            "                await self._publish_brightness_state(command_id=event.command_id)\n"
        ),
        "new": (
            "                self._current_brightness = 100\n"
            "                await self._publish_brightness_state()\n"
        ),
        "catchers": {
            PANEL_AT_MAXIMUM: UNTAGGED_FIRST_OF_TWO,
        },
    },
    {
        "name": "panel-maximum-overflow-drops-id",
        "target": PANEL,
        "old": (
            '                    overflow_delta, "at_hardware_limit", command_id=event.command_id)\n'
            '                logger.debug(f"Hardware at maximum (100%), overflow cascade: {overflow_delta}")\n'
        ),
        "new": (
            '                    overflow_delta, "at_hardware_limit")\n'
            '                logger.debug(f"Hardware at maximum (100%), overflow cascade: {overflow_delta}")\n'
        ),
        "catchers": {
            PANEL_AT_MAXIMUM: UNTAGGED_SECOND_OF_TWO,
        },
    },
    {
        "name": "panel-success-state-drops-id",
        "target": PANEL,
        "old": (
            "                self._current_brightness = target\n"
            "                await self._publish_brightness_state(command_id=event.command_id)\n"
        ),
        "new": (
            "                self._current_brightness = target\n"
            "                await self._publish_brightness_state()\n"
        ),
        "catchers": {
            PANEL_SUCCESS: UNTAGGED_ONE,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ARRIVALS,
        },
    },
    {
        "name": "panel-state-helper-drops-id",
        "target": PANEL,
        "old": (
            "                command_id=command_id\n"
            "            ))\n"
            '            logger.debug(f"Published brightness state: {self._current_brightness}%")\n'
        ),
        "new": (
            "                command_id=None\n"
            "            ))\n"
            '            logger.debug(f"Published brightness state: {self._current_brightness}%")\n'
        ),
        "catchers": {
            PANEL_AT_MINIMUM: UNTAGGED_FIRST_OF_TWO,
            PANEL_AT_MAXIMUM: UNTAGGED_FIRST_OF_TWO,
            PANEL_SUCCESS: UNTAGGED_ONE,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: ONE_STEP_INSTEAD_OF_NONE,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ARRIVALS,
        },
    },
    {
        "name": "panel-overflow-helper-drops-id",
        "target": PANEL,
        "old": (
            "                command_id=command_id\n"
            "            ))\n"
            '            logger.debug(f"Published overflow event: delta={delta}, reason={reason}")\n'
        ),
        "new": (
            "                command_id=None\n"
            "            ))\n"
            '            logger.debug(f"Published overflow event: delta={delta}, reason={reason}")\n'
        ),
        "catchers": {
            PANEL_AT_MINIMUM: UNTAGGED_SECOND_OF_TWO,
            PANEL_AT_MAXIMUM: UNTAGGED_SECOND_OF_TWO,
            PANEL_READ_NONE: UNTAGGED_ONE,
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: PANEL_ANSWERS,
            BOTH_AT_MINIMUM: PANEL_ANSWERS,
        },
    },
    # ---- plugins/bravia_plugin.py ----
    {
        "name": "bravia-offline-on-read-drops-id",
        "target": BRAVIA,
        "old": (
            '                logger.warning(f"{self.name}: TV offline, publishing overflow event")\n'
            '                await self._publish_overflow(delta, "device_offline", command_id=event.command_id)\n'
        ),
        "new": (
            '                logger.warning(f"{self.name}: TV offline, publishing overflow event")\n'
            '                await self._publish_overflow(delta, "device_offline")\n'
        ),
        "catchers": {
            BRAVIA_OFFLINE_ON_READ: UNTAGGED_ONE,
            TWO_OFFLINE_TVS: BRAVIA_ANSWERS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: BRAVIA_ANSWERS,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ARRIVALS,
        },
    },
    {
        "name": "bravia-at-limit-overflow-drops-id",
        "target": BRAVIA,
        "old": '                await self._publish_overflow(delta, "at_hardware_limit", command_id=event.command_id)\n',
        "new": '                await self._publish_overflow(delta, "at_hardware_limit")\n',
        "catchers": {
            BRAVIA_AT_LIMIT: UNTAGGED_FIRST_OF_TWO,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
        },
    },
    {
        "name": "bravia-at-limit-state-drops-id",
        "target": BRAVIA,
        "old": (
            "                await self._publish_state_change(current_brightness, command_id=event.command_id)"
            "  # Still publish state for awareness\n"
        ),
        "new": (
            "                await self._publish_state_change(current_brightness)"
            "  # Still publish state for awareness\n"
        ),
        "catchers": {
            BRAVIA_AT_LIMIT: UNTAGGED_SECOND_OF_TWO,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
        },
    },
    {
        "name": "bravia-offline-on-adjust-drops-id",
        "target": BRAVIA,
        "old": (
            '                logger.warning(f"{self.name}: TV went offline during adjustment")\n'
            '                await self._publish_overflow(delta, "device_offline", command_id=event.command_id)\n'
        ),
        "new": (
            '                logger.warning(f"{self.name}: TV went offline during adjustment")\n'
            '                await self._publish_overflow(delta, "device_offline")\n'
        ),
        "catchers": {
            BRAVIA_OFFLINE_ON_ADJUST: UNTAGGED_ONE,
        },
    },
    {
        "name": "bravia-success-state-drops-id",
        "target": BRAVIA,
        "old": "                await self._publish_state_change(new_brightness, command_id=event.command_id)\n",
        "new": "                await self._publish_state_change(new_brightness)\n",
        "catchers": {
            BRAVIA_SUCCESS: UNTAGGED_ONE,
        },
    },
    {
        "name": "bravia-state-helper-drops-id",
        "target": BRAVIA,
        "old": (
            "            command_id=command_id\n"
            "        )\n"
            "        await self._event_bus.publish(event)\n"
            "        self._last_known_brightness = brightness\n"
        ),
        "new": (
            "            command_id=None\n"
            "        )\n"
            "        await self._event_bus.publish(event)\n"
            "        self._last_known_brightness = brightness\n"
        ),
        "catchers": {
            BRAVIA_AT_LIMIT: UNTAGGED_SECOND_OF_TWO,
            BRAVIA_SUCCESS: UNTAGGED_ONE,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
        },
    },
    {
        "name": "bravia-overflow-helper-drops-id",
        "target": BRAVIA,
        "old": (
            "            reason=reason,\n"
            "            timestamp=time.time(),\n"
            "            command_id=command_id\n"
        ),
        "new": (
            "            reason=reason,\n"
            "            timestamp=time.time(),\n"
            "            command_id=None\n"
        ),
        "catchers": {
            BRAVIA_OFFLINE_ON_READ: UNTAGGED_ONE,
            BRAVIA_OFFLINE_ON_ADJUST: UNTAGGED_ONE,
            BRAVIA_AT_LIMIT: UNTAGGED_FIRST_OF_TWO,
            TWO_OFFLINE_TVS: BRAVIA_ANSWERS,
            PANEL_IN_HARDWARE_AND_OFFLINE_TV: BRAVIA_ANSWERS,
            OFFLINE_ANSWER_BEFORE_PANEL_STATE: ARRIVALS,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
        },
    },
    # ---- plugins/samsung_plugin.py ----
    {
        "name": "samsung-offline-drops-id",
        "target": SAMSUNG,
        "old": (
            "                        delta=event.delta, source_plugin=self.name, reason='device_offline',\n"
            "                        command_id=event.command_id))\n"
        ),
        "new": "                        delta=event.delta, source_plugin=self.name, reason='device_offline'))\n",
        "catchers": {
            SAMSUNG_OFFLINE: UNTAGGED_ONE,
            SAMSUNG_TIMEOUT: UNTAGGED_ONE,
            PANEL_AT_MINIMUM_AND_OFFLINE_SAMSUNG: PANEL_ANSWERS,
            TWO_OFFLINE_TVS: BRAVIA_ANSWERS,
            OVERFLOW_AND_STATE_NOT_APPLIED: BRAVIA_ANSWERS,
        },
    },
    {
        "name": "samsung-state-drops-id",
        "target": SAMSUNG,
        "old": "            await self._publish_state(result.level, command_id=event.command_id)\n",
        "new": "            await self._publish_state(result.level)\n",
        "catchers": {
            SAMSUNG_SUCCESS: UNTAGGED_ONE,
            SAMSUNG_AT_LIMIT: UNTAGGED_FIRST_OF_TWO,
        },
    },
    {
        "name": "samsung-at-limit-drops-id",
        "target": SAMSUNG,
        "old": (
            "                    delta=result.overflow, source_plugin=self.name, reason='at_hardware_limit',\n"
            "                    command_id=event.command_id))\n"
        ),
        "new": "                    delta=result.overflow, source_plugin=self.name, reason='at_hardware_limit'))\n",
        "catchers": {
            SAMSUNG_AT_LIMIT: UNTAGGED_SECOND_OF_TWO,
            BOTH_AT_MINIMUM: PANEL_ANSWERS,
        },
    },
    {
        "name": "samsung-state-helper-drops-id",
        "target": SAMSUNG,
        "old": (
            "            level=value, at_min=value == 0, at_max=value == 100, source_plugin=self.name,\n"
            "            command_id=command_id))\n"
        ),
        "new": (
            "            level=value, at_min=value == 0, at_max=value == 100, source_plugin=self.name,\n"
            "            command_id=None))\n"
        ),
        "catchers": {
            SAMSUNG_SUCCESS: UNTAGGED_ONE,
            SAMSUNG_AT_LIMIT: UNTAGGED_FIRST_OF_TWO,
        },
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")

# EventBus.publish logs and swallows an exception raised by a subscriber, and the
# plugins catch their own, so a mutant that crashes inside a handler fails a test
# at an ordinary assertion. The captured log of that failing test shows one of
# these texts; the run is then an error, not a verdict.
HANDLER_EXCEPTION_MARKERS = (
    "EventBus: handler raised",
    "EventBus: failed to invoke handler",
    "Error handling brightness command",
    "Failed to publish brightness state",
    "Failed to publish overflow event",
)


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(names):
    """Return (prepared mutants, error lines). Never touches a file.

    Each prepared entry is (mutation, target path, original bytes, mutant bytes).
    """
    originals = {}
    prepared, errors = [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        target = SERVICE / mutation["target"]
        if target not in originals:
            originals[target] = target.read_bytes()
        original = originals[target]
        source = original.decode("utf-8")
        ending = _line_ending(original)
        old = mutation["old"].replace("\n", ending)
        new = mutation["new"].replace("\n", ending)
        count = source.count(old)
        if count == 0:
            errors.append(f"ERROR {mutation['name']}: pattern-not-found in {mutation['target']}")
            continue
        if count > 1:
            errors.append(f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches in {mutation['target']})")
            continue
        mutant = source.replace(old, new, 1)
        try:
            compile(mutant, str(target), "exec")
        except SyntaxError as exc:
            errors.append(f"ERROR {mutation['name']}: does-not-compile ({exc})")
            continue
        prepared.append((mutation, target, original, mutant.encode("utf-8")))
    return prepared, errors


def _clear_bytecode():
    for directory in sorted({(SERVICE / m["target"]).parent for m in MUTATIONS}):
        shutil.rmtree(directory / "__pycache__", ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "400"  # keep short-summary messages untruncated
    return env


def _pytest(extra, timeout):
    command = [sys.executable, "-m", "pytest", *SELECTION, "-p", "no:randomly",
               "-p", "no:cacheprovider", "--junitxml=NUL" if os.name == "nt" else "--junitxml=/dev/null",
               *extra]
    return subprocess.run(command, cwd=SERVICE, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _collected_names():
    """Collected test names with their parameter ids (the part after the last '::')."""
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split(" ")[0].strip())
    return result.returncode, names


def _summary(output: str):
    """Map test name (with parameter id) -> (kind, message) from pytest's short summary only."""
    records, inside = {}, False
    for line in output.splitlines():
        if SUMMARY_HEADER.match(line):
            inside = True
            continue
        if inside and SECTION_RULE.match(line):
            break
        if inside:
            match = SUMMARY_LINE.match(line)
            if match:
                test_id = match.group(2)
                name = test_id.split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection():
    result = _pytest(["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    handler_errors = [marker for marker in HANDLER_EXCEPTION_MARKERS if marker in output]
    if handler_errors:
        raise RuntimeError("exception inside an event handler (captured log shows: "
                           + "; ".join(handler_errors) + ")")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    if result.returncode == 1 and not records:
        raise RuntimeError(f"pytest exit 1 with no short-summary records: {output[-1500:]}")
    return records


def _write_with_retry(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    last = None
    try:
        for _ in range(10):
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.5)
        raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _restore(target: Path, original: bytes, stat):
    """Restore bytes and timestamps; hold a Ctrl+C until after the cache clear."""
    held = None
    for _attempt in range(2):
        try:
            _write_with_retry(target, original)
            os.utime(target, ns=stat)
            if target.read_bytes() == original:
                break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as exc:
            print(f"RESTORE-ATTEMPT-FAILED {target}: {exc}", flush=True)
    restored = False
    try:
        restored = target.read_bytes() == original
    except (OSError, KeyboardInterrupt):
        pass
    if not restored:
        print(f"RESTORE FAILED: {target} may still hold a mutant; check git diff", flush=True)
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {target}")


def _judge(mutation, records):
    unexpected = [f"{name}: {message}" for name, (kind, message) in records.items()
                  if kind == "ERROR" or not (message.startswith("assert") or message.startswith("AssertionError"))]
    if unexpected:
        return "ERROR", "non-assertion failure: " + "; ".join(unexpected)
    fired, green, wrong = [], [], []
    for name, marker in mutation["catchers"].items():
        if name not in records:
            green.append(name)
        elif not records[name][1].startswith(marker):
            wrong.append(f"{name}: {records[name][1]} (expected {marker!r})")
        else:
            fired.append(f"{name}: {records[name][1]}")
    if wrong:
        return "ERROR", "catcher failed at an unexpected assertion: " + "; ".join(wrong)
    others = sorted(set(records) - set(mutation["catchers"]))
    detail = []
    if fired:
        detail.append("fired: " + "; ".join(fired))
    if green:
        detail.append("expected catchers still green: " + ", ".join(green))
    if others:
        detail.append("other failures: " + "; ".join(f"{n}: {records[n][1]}" for n in others))
    if fired and not green:
        return "CAUGHT", " | ".join(detail)
    return "SURVIVED", " | ".join(detail) or "no test failed"


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME", help="run only these mutations")
    parser.add_argument("--log", type=Path, help="also append output to this file")
    args = parser.parse_args(argv)

    log = args.log.open("a", encoding="utf-8", buffering=1) if args.log else None

    def say(text):
        print(text, flush=True)
        if log:
            log.write(text + "\n")

    known = {m["name"] for m in MUTATIONS}
    unknown = [n for n in args.only if n not in known]
    if unknown:
        say(f"ERROR unknown mutation names: {', '.join(unknown)}")
        return 1
    if len(known) != len(MUTATIONS):
        say("ERROR two mutations share a name")
        return 1

    prepared, errors = _prepare(set(args.only))
    for line in errors:
        say(line)
    stale = sum("pattern-" in e for e in errors)
    broken = sum("does-not-compile" in e for e in errors)
    selected = len(prepared) + len(errors)
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, {broken} that do not compile")
    if args.check:
        return 1 if errors else 0

    rc, collected = _collected_names()
    missing = sorted({name for mutation, *_ in prepared for name in mutation["catchers"]} - collected)
    if rc != 0 or not collected or missing:
        say(f"ERROR expected catcher names not collected (collect rc={rc}): {', '.join(missing) or 'none collected'}")
        return 1

    stats = {}
    for _mutation, target, _original, _mutant in prepared:
        if target not in stats:
            info = target.stat()
            stats[target] = (info.st_atime_ns, info.st_mtime_ns)

    _clear_bytecode()
    try:
        baseline = _run_selection()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        say(f"ERROR baseline: {exc}")
        return 1
    if baseline:
        say(f"ERROR baseline not green: {sorted(baseline)}")
        return 1
    say(f"Baseline green; running {len(prepared)} of {len(MUTATIONS)} mutations"
        + (f" (--only {' '.join(args.only)})" if args.only else ""))

    survivors = 0
    error_count = len(errors)
    for index, (mutation, target, original, mutant) in enumerate(prepared, 1):
        name = mutation["name"]
        stat = stats[target]
        try:
            _write_with_retry(target, mutant)
            os.utime(target, ns=(stat[0], stat[1] + index * 1_000_000_000))
            _clear_bytecode()
            records = _run_selection()
            verdict, detail = _judge(mutation, records)
        except subprocess.TimeoutExpired:
            verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
        except RuntimeError as exc:
            verdict, detail = "ERROR", str(exc)
        finally:
            _restore(target, original, stat)
        if verdict == "SURVIVED":
            survivors += 1
        elif verdict == "ERROR":
            error_count += 1
        say(f"{verdict} {name} ({mutation['target']}): {detail}")

    say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
        f"{len(prepared) - survivors - (error_count - len(errors))} caught, "
        f"{survivors} survived, {error_count} errors")
    if log:
        log.close()
    return 1 if survivors or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
