"""Mutation evidence for wh-volume-router-sonos-enabled.

VolumeRouter reads plugins.sonos.enabled with the plugin registry's default
(True when the key is absent). When the plugin is off it chooses System Volume
and returns before the soco network search. Each mutation below breaks one part
of that block in handlers/volume_router.py and names the tests in
TestInitializeSonosPluginSetting that must fail.

wh-sonos-tv-recheck moved the block. It used to sit in initialize() and write
self._use_sonos directly; the decision matrix now lives in
_decide_volume_target(), which returns the answer, and initialize() stores it.
Every pattern in this file went stale at that move (nine of nine, measured
2026-10-06 with --check) and each one is rewritten here against the new
source. The guarded behaviour did not change, so no mutation was dropped.

The shared runner in services/stt_providers/shared/tests/mutation_gate_runner.py
owns pattern matching, the exactly-one-match check, the compile check, the
expected-name check, the green baseline, the per-mutation timeout, bytecode
clearing, and source restoration; this file only declares the table.

Run from services/wheelhouse with the interpreter of the checkout it sits in
(``sys.executable`` becomes the pytest launcher, see LAUNCH below):

    .venv/Scripts/python.exe tests/mutation_gate_volume_router_sonos_enabled.py
    .venv/Scripts/python.exe tests/mutation_gate_volume_router_sonos_enabled.py --check

This gate replaces ``runner.LAUNCH`` for the same reason as
tests/mutation_gate_pattern_manager_tree_changed.py: the default launch goes
through ``uv run``, which builds a .venv inside a git worktree and makes the
worktree hard to delete. The override starts ``<python> -m pytest`` in the
service directory; the ownership, timeout, marker, and parsing are unchanged.

Selection: the whole tests/test_handlers/test_volume_router.py file (86 tests
on 2026-10-11, by ``--collect-only -q``, about a second), so the other routing
tests also act as the sanity set.
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

TEST = "tests/test_handlers/test_volume_router.py"
TARGET = "handlers/volume_router.py"

# --- expected catchers, by the name pytest reports --------------------------
# The shared runner strips a parameter list from both the collected names and
# the failed names (mutation_gate_runner.py, _collected_names and
# _failed_names), so a parametrized suite is named by its bare name and one
# failing case marks it caught. Naming a single case is not possible here.
OFF = "test_sonos_plugin_off_routes_to_system_without_search"
SOCO = "test_sonos_plugin_off_never_calls_soco_discover"
KEY = "test_reads_registry_key_and_default"
ON = "test_sonos_plugin_on_routing_unchanged"

MUTATIONS = []


def add(name, old, new, *expect):
    flat = []
    for item in expect:
        flat.extend(item if isinstance(item, list) else [item])
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TEST,
                          file=SERVICE / TARGET, old=old, new=new,
                          expect=flat))


# The block as it stands in _decide_volume_target, and the code that follows
# it up to and including the Sonos network search. The move mutation below is
# built from these two pieces, so it relocates the block rather than copying
# it. Both early returns end in "None, None," and a reason, so every pattern
# that touches one carries its neighbouring lines.
BLOCK = (
    "        if not self._sonos_plugin_enabled():\n"
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n'
    "            return False, None, None, self.REASON_PLUGIN_DISABLED\n"
)
# wh-sonos-soundbar-volume: the search is now the direct query of the known TV
# speaker followed by _search_for_tv_speaker(), so the piece that follows the
# block runs through the search call.
UP_TO_SEARCH = (
    "        if self._is_internal_audio:\n"
    "            logger.debug(\n"
    "                f\"VolumeRouter: Internal audio ('{self._audio_device_name}') \"\n"
    '                f"-> System Volume"\n'
    "            )\n"
    "            return False, None, None, self.REASON_INTERNAL\n"
    "\n"
    "        # The known TV speaker is queried by its address first. When it\n"
    "        # answers, the check runs no network search.\n"
    "        if self._tv_ip is not None and await self._tv_speaker_answers():\n"
    "            self._missed_checks = 0\n"
    "            return True, self._tv_ip, self._tv_name, self._tv_reason\n"
    "\n"
    "        found, tv_speaker = await self._search_for_tv_speaker()\n"
)
READ = '        return bool(self._config.get("plugins.sonos.enabled", True))\n'

# 1. The fix reverted whole: the plugin setting is never read, so an external
# device runs the soco search and can route volume to an unloaded plugin.
add("remove-the-plugin-setting-block", BLOCK + "\n", "",
    OFF, SOCO, KEY)

# 2. Input-level: a different default. An absent key then means "off", which
# disagrees with PluginRegistry.discover_plugins, which loads the plugin.
add("default-becomes-false", READ,
    '        return bool(self._config.get("plugins.sonos.enabled", False))\n',
    KEY, ON)

# 3. Input-level: the default dropped. get() then answers None for an absent
# key, and bool(None) turns the plugin off for every install without the key.
add("default-dropped", READ,
    '        return bool(self._config.get("plugins.sonos.enabled"))\n',
    KEY, ON)

# 4. Input-level: a different key. The real setting is never read, so the
# block never runs when the user turned the plugin off.
add("key-misspelled", READ,
    '        return bool(self._config.get("plugins.sonos.enable", True))\n',
    OFF, SOCO, KEY)

# 5. The condition inverted: plugin on takes the System Volume shortcut and
# plugin off runs the search.
# start_tv_recheck reads the same setting with the same line, so the pattern
# carries the next line to stay unique.
add("condition-inverted",
    "        if not self._sonos_plugin_enabled():\n"
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n',
    "        if self._sonos_plugin_enabled():\n"
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n',
    OFF, SOCO, KEY, ON)

# 6. The block chooses Sonos instead of System Volume.
add("block-chooses-sonos",
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n'
    "            return False, None, None, self.REASON_PLUGIN_DISABLED\n",
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n'
    "            return True, None, None, self.REASON_PLUGIN_DISABLED\n",
    OFF, SOCO)

# 7. The block does not return, so control falls through to the internal-
# audio check and, for an external device, to the soco search.
add("block-falls-through-without-return",
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n'
    "            return False, None, None, self.REASON_PLUGIN_DISABLED\n",
    '            logger.debug("VolumeRouter: Sonos plugin disabled -> System Volume")\n',
    OFF, SOCO, KEY)

# 8. initialize() leaves the router marked uninitialized. The flag moved out
# of the block itself at the wh-sonos-tv-recheck move, so the mutation now
# targets the one place that writes it.
add("initialize-leaves-the-router-uninitialized",
    "        self._sonos_name = sonos_name\n        self._initialized = True\n",
    "        self._sonos_name = sonos_name\n        pass\n",
    OFF)

# 9. Placement: the check MOVED to after the soco search (deleted at its
# place, inserted after the search). The routing answer is still System
# Volume, so only the tests that require the search never to run catch it.
add("check-moved-after-the-search",
    BLOCK + "\n" + UP_TO_SEARCH,
    UP_TO_SEARCH + "\n" + BLOCK,
    OFF, SOCO, KEY)


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
