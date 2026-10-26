"""Mutation gate for the no-hardware software dimmer path (wh-gamma-dimmer-no-hardware).

WHAT IT GUARDS. Commit 8519aff0 added EventBus.has_subscribers (event_bus.py)
and, in coordinators/brightness_coordinator.py, a check at the top of
_route_to_hardware: with no handler subscribed to HardwareBrightnessCommand,
the step goes to _apply_step_without_hardware, which gives a dim step to the
software dimmer through _engage_and_apply_software_step (the same path as a
hardware overflow: CASCADED state, unwinding, the stop() restore) and ignores
a brighten step and a missing software dimmer. Each mutation below breaks one
changed line of
`git show 8519aff0 -- services/wheelhouse/event_bus.py
services/wheelhouse/coordinators/brightness_coordinator.py`
and names the tests that must fail. Two mutations survived the first sweep,
and a test was added with this gate for each:
test_zero_step_changes_nothing (brighten-boundary-excludes-zero) and
test_first_step_dims_by_its_own_delta (software-step-has-a-fixed-size), both
in tests/test_coordinators/test_brightness_coordinator_no_hardware.py.

TEST SELECTION. Every mutation runs the same five files (SELECTION): the new
coordinator tests, the event bus tests, the one-step-per-command tests, the
Samsung plugin tests, and the gamma dimmer tests (the eight GammaDimmer.start
mutations for finding wh-gamma-dimmer-no-hardware.1.1). A clean run takes
about 2 s.

WHAT ``caught`` MEANS. ``expect`` is the set of tests that must fail; the
shared runner reports a survivor when any of them passes. This gate prints
each run's FAILED summary lines with their reason, so the record shows WHICH
assertion fired, and an unrelated exception upstream of the guarded behaviour
cannot read as a catch.

ERROR COUNTS. The shared runner prints every error with its kind but counts
them as one number. This gate also tallies the ERROR lines by kind (stale
pattern, ambiguous pattern, indent mismatch, does not compile, timeout, suite
timeout abort, no pytest result, other) and prints one line with those counts
after the runner's own summary.

The shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
owns pattern matching, the exactly-one-match check, the compile check, the
expected-name check against --collect-only, the green baseline, the per-run
timeout, bytecode clearing with PYTHONDONTWRITEBYTECODE=1, COLUMNS=1000, the
exit-code-first verdict read from the short summary, and source restoration.
This file replaces ``runner.LAUNCH`` as
tests/mutation_gate_thumb_wheel_slot_query.py does: the default launch goes
through ``uv run``, which builds a .venv inside a git worktree.

Run from services/wheelhouse with an interpreter that has the service's
dependencies (``sys.executable`` becomes the pytest launcher):

    python tests/mutation_gate_gamma_dimmer_no_hardware.py            # all
    python tests/mutation_gate_gamma_dimmer_no_hardware.py route      # by name
    python tests/mutation_gate_gamma_dimmer_no_hardware.py --check    # patterns
"""
import builtins
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner

# A clean run of SELECTION takes about 2 s; 120 s is far above that and far
# below a hang.
runner.RUN_TIMEOUT = 120


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

_shared_run_pytest = runner._run_pytest


def _run_pytest_and_show_reasons(service, test_file):
    """The shared run, plus each FAILED summary line with its reason, so a
    failure upstream of the guarded behaviour cannot read as a catch."""
    result = _shared_run_pytest(service, test_file)
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith("FAILED "):
            print("    " + line.split("::", 1)[-1][:240])
    return result


runner._run_pytest = _run_pytest_and_show_reasons

_ERROR_KINDS = (
    ("pattern not found", "stale"),
    ("pattern ambiguous", "ambiguous"),
    ("deeper indent", "indent"),
    ("does not compile", "does-not-compile"),
    ("timed out", "timeout"),
    ("suite-timeout-abort", "suite-timeout-abort"),
    ("no-pytest-result", "no-pytest-result"),
)
_error_counts = Counter()


def _tallying_print(*args, **kwargs):
    """The runner's print, plus a count of each ERROR line by its kind."""
    text = " ".join(str(a) for a in args)
    if text.startswith("ERROR "):
        kind = next((k for marker, k in _ERROR_KINDS if marker in text), "other")
        _error_counts[kind] += 1
    builtins.print(*args, **kwargs)


# The runner's module-level name lookup finds this before the builtin.
runner.print = _tallying_print

SELECTION = (
    "tests/test_coordinators/test_brightness_coordinator_no_hardware.py",
    "tests/test_event_bus.py",
    "tests/test_coordinators/test_brightness_coordinator_one_step_per_command.py",
    "tests/test_plugins/test_samsung_plugin.py",
    "tests/test_handlers/test_gamma_dimmer.py",
)
BUS = SERVICE / "event_bus.py"
COORDINATOR = SERVICE / "coordinators/brightness_coordinator.py"
GAMMA = SERVICE / "handlers/gamma_dimmer.py"

MUTATIONS = []


def add(name, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=SELECTION,
                          file=file, old=old, new=new, expect=list(expect)))


# ---- EventBus.has_subscribers ----------------------------------------------
HAS = "        return bool(self._subscribers.get(event_type))\n"

add("has-subscribers-always-true", BUS, HAS,
    "        return True\n",
    "test_has_subscribers_is_false_for_a_type_with_no_subscriber",
    "test_has_subscribers_is_true_only_for_the_subscribed_type",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_each_step_dims_by_the_command_delta",
    "test_brighten_returns_to_no_dimming",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("has-subscribers-always-false", BUS, HAS,
    "        return False\n",
    "test_has_subscribers_is_true_only_for_the_subscribed_type",
    "test_a_plugin_that_drops_the_command_does_not_start_software_dimming",
    "test_a_panel_with_hardware_takes_the_step",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_offline_overflow_for_a_finished_command_applies_on_arrival")
add("has-subscribers-adds-an-entry", BUS, HAS,
    "        return bool(self._subscribers[event_type])\n",
    "test_has_subscribers_adds_no_entry_for_the_type_it_checks")
add("has-subscribers-returns-the-list", BUS, HAS,
    "        return self._subscribers.get(event_type)\n",
    "test_has_subscribers_is_false_for_a_type_with_no_subscriber",
    "test_has_subscribers_is_true_only_for_the_subscribed_type")

# ---- _route_to_hardware: the no-subscriber check ---------------------------
CHECK = "        if not self._event_bus.has_subscribers(HardwareBrightnessCommand):\n"

add("route-check-inverted", COORDINATOR, CHECK,
    "        if self._event_bus.has_subscribers(HardwareBrightnessCommand):\n",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_each_step_dims_by_the_command_delta",
    "test_brighten_returns_to_no_dimming",
    "test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing",
    "test_a_plugin_that_drops_the_command_does_not_start_software_dimming",
    "test_a_panel_with_hardware_takes_the_step",
    "test_mock_bus_with_a_subscriber_publishes_to_the_hardware",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("route-check-removed", COORDINATOR, CHECK,
    "        if False:\n",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_each_step_dims_by_the_command_delta",
    "test_brighten_returns_to_no_dimming",
    "test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("route-check-asks-about-the-wrong-event", COORDINATOR, CHECK,
    # The coordinator itself subscribes to BrightnessAdjustCommand, so this
    # check always finds a subscriber.
    "        if not self._event_bus.has_subscribers(BrightnessAdjustCommand):\n",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_each_step_dims_by_the_command_delta",
    "test_brighten_returns_to_no_dimming",
    "test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("route-also-publishes-after-the-software-step", COORDINATOR,
    "            await self._apply_step_without_hardware(delta)\n"
    "            return\n",
    "            await self._apply_step_without_hardware(delta)\n",
    # A real bus with no subscriber drops the publish, so only the mock bus
    # sees it.
    "test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing")

# ---- _apply_step_without_hardware ------------------------------------------
add("no-dimmer-guard-removed", COORDINATOR,
    "        if not self._software_dimmer:\n"
    "            logger.debug(f\"No hardware brightness plugin and no software dimmer;",
    # Safe replacement: the mutant runs on to _engage_and_apply_software_step,
    # which sets CASCADED, and _adjust_software_dimmer then warns and returns.
    "        if False:\n"
    "            logger.debug(f\"No hardware brightness plugin and no software dimmer;",
    "test_without_a_software_dimmer_nothing_changes")
add("brighten-boundary-excludes-zero", COORDINATOR,
    "        if delta >= 0:\n"
    "            logger.debug(f\"No hardware brightness plugin; no dimming to remove",
    "        if delta > 0:\n"
    "            logger.debug(f\"No hardware brightness plugin; no dimming to remove",
    # Survived the first sweep: no test sent a zero step.
    # test_zero_step_changes_nothing was added with this gate.
    "test_zero_step_changes_nothing")
add("brighten-and-dim-swapped", COORDINATOR,
    "        if delta >= 0:\n"
    "            logger.debug(f\"No hardware brightness plugin; no dimming to remove",
    "        if delta < 0:\n"
    "            logger.debug(f\"No hardware brightness plugin; no dimming to remove",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_each_step_dims_by_the_command_delta",
    "test_brighten_returns_to_no_dimming",
    "test_brighten_at_no_dimming_changes_nothing_and_logs_no_error",
    "test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("brighten-falls-through", COORDINATOR,
    "            logger.debug(f\"No hardware brightness plugin; no dimming to remove (delta={delta:+d})\")\n"
    "            return\n",
    "            logger.debug(f\"No hardware brightness plugin; no dimming to remove (delta={delta:+d})\")\n"
    "            pass\n",
    "test_brighten_at_no_dimming_changes_nothing_and_logs_no_error")

STEP = ("        logger.debug(f\"No hardware brightness plugin; dimming in software (delta={delta:+d})\")\n"
        "        await self._engage_and_apply_software_step(delta)\n")
STEP_HEAD = ("        logger.debug(f\"No hardware brightness plugin; dimming in software "
             "(delta={delta:+d})\")\n")

add("software-step-not-engaged", COORDINATOR, STEP,
    # No engage: the level changes, but the state stays IDLE and stop() does
    # not restore.
    STEP_HEAD + "        await self._adjust_software_dimmer(delta)\n",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_brighten_returns_to_no_dimming",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("software-step-dropped", COORDINATOR, STEP,
    STEP_HEAD + "        pass\n",
    "test_dim_step_goes_to_the_software_dimmer",
    "test_each_step_dims_by_the_command_delta",
    "test_brighten_returns_to_no_dimming",
    "test_mock_bus_with_no_subscriber_dims_in_software_and_publishes_nothing",
    "test_unwinding_returns_to_hardware_and_the_fallback_stays_off",
    "test_stop_restores_no_dimming")
add("software-step-has-a-fixed-size", COORDINATOR, STEP,
    STEP_HEAD + "        await self._engage_and_apply_software_step(-10)\n",
    # Survived the first sweep: test_each_step_dims_by_the_command_delta sends
    # -10 first, and its later steps go through the CASCADED unwinding path.
    # test_first_step_dims_by_its_own_delta was added with this gate.
    "test_first_step_dims_by_its_own_delta")

# ---- GammaDimmer.start: a ramp left dimmed by an earlier run (.1.1) ----------
DIMMED = "test_all_three_tops_below_95_percent_get_the_identity_ramp"
KEPT = "test_a_ramp_with_a_top_at_or_above_95_percent_stays_the_original"
CHECK_HEAD = "        if all(\n            self._original_ramp[channel][255] * 100 < 65535 * 95\n"
IDENTITY_WRITE = (
    "            if not self.gdi32.SetDeviceGammaRamp(self._hdc, ctypes.byref(self._original_ramp)):\n"
    "                logger.error(\"GammaDimmer: Failed to apply the identity gamma ramp\")\n")

add("dimmed-ramp-check-disabled", GAMMA, CHECK_HEAD,
    "        if False and all(\n            self._original_ramp[channel][255] * 100 < 65535 * 95\n",
    DIMMED)
add("dimmed-ramp-check-any-channel", GAMMA, CHECK_HEAD,
    "        if any(\n            self._original_ramp[channel][255] * 100 < 65535 * 95\n",
    KEPT)
add("dimmed-ramp-threshold-94", GAMMA, "< 65535 * 95\n", "< 65535 * 94\n", DIMMED)
add("dimmed-ramp-threshold-96", GAMMA, "< 65535 * 95\n", "< 65535 * 96\n", KEPT)
add("identity-ramp-not-written", GAMMA, IDENTITY_WRITE,
    "            pass\n", DIMMED)
add("identity-ramp-wrong-values", GAMMA, "= i * 257\n", "= i * 256\n", DIMMED)
add("identity-ramp-two-channels", GAMMA,
    "            for channel in range(3):\n                for i in range(256):\n"
    "                    self._original_ramp[channel][i] = i * 257\n",
    "            for channel in range(2):\n                for i in range(256):\n"
    "                    self._original_ramp[channel][i] = i * 257\n",
    DIMMED)
add("dimmed-ramp-no-warning", GAMMA,
    "            logger.warning(\n                \"GammaDimmer: the display gamma ramp",
    "            logger.info(\n                \"GammaDimmer: the display gamma ramp",
    DIMMED)


def main(argv=None):
    code = runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))
    counts = ", ".join(f"{_error_counts[k]} {k}" for _, k in _ERROR_KINDS)
    print(f"errors by kind: {counts}, {_error_counts['other']} other")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
