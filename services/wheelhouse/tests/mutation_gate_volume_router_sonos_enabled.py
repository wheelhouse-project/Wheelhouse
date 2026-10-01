"""Mutation evidence for wh-volume-router-sonos-enabled.

VolumeRouter.initialize() reads plugins.sonos.enabled with the plugin
registry's default (True when the key is absent). When the plugin is off it
chooses System Volume and returns before the soco network search. Each
mutation below breaks one part of that block in handlers/volume_router.py and
names the tests in TestInitializeSonosPluginSetting that must fail.

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

Selection: the whole tests/test_handlers/test_volume_router.py file (47 tests,
under a second), so the pre-existing routing tests also act as the sanity set.
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
OFF = "test_sonos_plugin_off_routes_to_system_without_search"
SOCO = "test_sonos_plugin_off_never_calls_soco_discover"
KEY = "test_reads_registry_key_and_default"
ON = "test_sonos_plugin_on_routing_unchanged"

MUTATIONS = []


def add(name, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TEST,
                          file=SERVICE / TARGET, old=old, new=new,
                          expect=list(expect)))


# The block as committed in 6eaeff52, and the code that follows it up to and
# including the Sonos network search. The move mutation below is built from
# these two pieces so it relocates the block rather than copying it.
BLOCK = (
    "        # Same key and default as PluginRegistry.discover_plugins: when the\n"
    "        # Sonos plugin is not loaded, nothing would handle Sonos-routed volume.\n"
    '        if not config.get("plugins.sonos.enabled", True):\n'
    "            self._use_sonos = False\n"
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n'
    "            self._initialized = True\n"
    "            return\n"
)
UP_TO_SEARCH = (
    "        if self._is_internal_audio:\n"
    "            self._use_sonos = False\n"
    "            logger.info(\n"
    "                f\"VolumeRouter: Internal audio detected ('{self._audio_device_name}') \"\n"
    '                f"-> using System Volume"\n'
    "            )\n"
    "            self._initialized = True\n"
    "            return\n"
    "        \n"
    "        # Step 2: External audio - check if Sonos receiving TV audio\n"
    "        logger.info(f\"VolumeRouter: External audio ('{self._audio_device_name}'), checking Sonos...\")\n"
    "        self._sonos_ip, self._sonos_name, is_receiving_tv = await self._discover_sonos_with_tv_check()\n"
)

# 1. The fix reverted whole: the plugin setting is never read, so an external
# device runs the soco search and can route volume to an unloaded plugin.
add("remove-the-plugin-setting-block", BLOCK + "\n", "",
    OFF, SOCO, KEY)

# 2. Input-level: a different default. An absent key then means "off", which
# disagrees with PluginRegistry.discover_plugins, which loads the plugin.
add("default-becomes-false",
    '        if not config.get("plugins.sonos.enabled", True):',
    '        if not config.get("plugins.sonos.enabled", False):',
    KEY, ON)

# 3. Input-level: the default dropped. get() then answers None for an absent
# key, and "not None" turns the plugin off for every install without the key.
add("default-dropped",
    '        if not config.get("plugins.sonos.enabled", True):',
    '        if not config.get("plugins.sonos.enabled"):',
    KEY, ON)

# 4. Input-level: a different key. The real setting is never read, so the
# block never runs when the user turned the plugin off.
add("key-misspelled",
    '        if not config.get("plugins.sonos.enabled", True):',
    '        if not config.get("plugins.sonos.enable", True):',
    OFF, SOCO, KEY)

# 5. The condition inverted: plugin on takes the System Volume shortcut and
# plugin off runs the search.
add("condition-inverted",
    '        if not config.get("plugins.sonos.enabled", True):',
    '        if config.get("plugins.sonos.enabled", True):',
    OFF, SOCO, KEY, ON)

# 6. The block chooses Sonos instead of System Volume.
add("block-chooses-sonos",
    "            self._use_sonos = False\n"
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n',
    "            self._use_sonos = True\n"
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n',
    OFF, SOCO)

# 7. The block does not return, so control falls through to the internal-
# audio check and, for an external device, to the soco search.
add("block-falls-through-without-return",
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n'
    "            self._initialized = True\n"
    "            return\n",
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n'
    "            self._initialized = True\n",
    OFF, SOCO, KEY)

# 8. The block leaves the router marked uninitialized.
add("block-leaves-router-uninitialized",
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n'
    "            self._initialized = True\n"
    "            return\n",
    '            logger.info("VolumeRouter: Sonos plugin disabled -> using System Volume")\n'
    "            pass\n"
    "            return\n",
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
