# test_snapshot_local_ai.py - The local AI decision in the snapshot
# (wh-ai-installer-local-mode)
#
# The installer already runs `syscheck.py --compact` once and parses the JSON
# to decide whether to offer the CUDA speech engine. It needs a second answer
# from the same hardware: where -- or whether -- the local AI model runs.
#
# Carrying that answer in the snapshot rather than behind a second flag is what
# lets the installer keep making one call. Each call is a `uv run`, which pays
# for interpreter startup and an environment check; a second one would be a few
# seconds of an install for an answer already computed.
#
# It also means the answer travels with the snapshot. When a user reports that
# the AI features were never installed, the file they send says why, in a
# sentence, beside the hardware that decided it.

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _snapshot(*, gpus=(), total_ram_bytes=16 * 1024**3):
    """Run main() over a described machine and return the parsed snapshot."""
    import syscheck

    out = StringIO()
    with patch.object(syscheck, "os") as mock_os, \
         patch("sys.argv", ["syscheck.py", "--compact"]), \
         patch("sys.stdout", out), \
         patch.object(syscheck, "get_host", return_value={
             "machine_name": "TESTPC", "os_name": "Windows",
             "os_release": "10.0.19045", "arch": "x64"}), \
         patch.object(syscheck, "get_cpu", return_value={
             "vendor": "V", "model": "M", "physical_cores": 4,
             "logical_cores": 8, "base_mhz": 3000, "flags": {}}), \
         patch.object(syscheck, "get_memory", return_value={
             "total_bytes": total_ram_bytes, "available_bytes": total_ram_bytes // 2}), \
         patch.object(syscheck, "get_disks", return_value=[]), \
         patch.object(syscheck, "probe_gpus_dxgi",
                      return_value=(list(gpus), "12_1", True, {})), \
         patch.object(syscheck, "get_audio", return_value={
             "wasapi_available": False, "diagnostics": {"last_error": "mocked"}}), \
         patch.object(syscheck, "get_privilege_block", return_value={
             "is_admin": False, "missing_capabilities": [],
             "recommend_elevation": False, "note": ""}), \
         patch.object(syscheck, "missing", []):
        mock_os.name = "nt"
        mock_os.path = __import__("os").path
        mock_os.environ = {"COMPUTERNAME": "TESTPC"}
        main_mod = MagicMock()
        main_mod.__file__ = __file__
        with patch.dict("sys.modules", {"__main__": main_mod}):
            syscheck.main()
    return json.loads(out.getvalue())


def _card(vram_gb, *, software=False, vendor_id=0x10DE):
    return {
        "index": 0, "name": "Described GPU", "vendor_id": vendor_id,
        "dedicated_vram_bytes": int(vram_gb * 1024**3),
        "software": software, "compute_support": {"d3d11": True},
    }


class TestTheAnswerIsInTheSnapshot:

    def test_a_machine_with_a_capable_card_is_told_to_use_it(self):
        data = _snapshot(gpus=[_card(8)], total_ram_bytes=16 * 1024**3)
        assert data["local_ai"]["install"] is True
        assert data["local_ai"]["gpu_layers"] == 99

    def test_a_machine_with_memory_and_no_card_is_told_to_use_the_processor(self):
        data = _snapshot(gpus=[], total_ram_bytes=16 * 1024**3)
        assert data["local_ai"]["install"] is True
        assert data["local_ai"]["gpu_layers"] == 0

    def test_a_machine_with_neither_is_told_no(self):
        data = _snapshot(gpus=[], total_ram_bytes=8 * 1024**3)
        assert data["local_ai"]["install"] is False
        assert data["local_ai"]["model"] is None

    def test_the_reason_comes_with_it(self):
        """The installer prints this sentence. Recomputing a wording on the
        PowerShell side would be a second place for it to go stale."""
        data = _snapshot(gpus=[], total_ram_bytes=8 * 1024**3)
        assert data["local_ai"]["reason"]

    @pytest.mark.parametrize("key", ["install", "model", "gpu_layers", "reason"])
    def test_every_field_the_installer_reads_is_present(self, key):
        data = _snapshot(gpus=[_card(8)])
        assert key in data["local_ai"]


class TestItIsTheSameAnswerTheLadderGives:
    """The whole point of putting it here is that there is one ladder. A
    reimplementation inside main() would drift from recommendations.py without
    any test noticing, because both would still be internally consistent."""

    @pytest.mark.parametrize("gpus,ram_gb", [
        ([_card(8)], 16),
        ([_card(2)], 32),
        ([], 16),
        ([], 8),
        ([_card(8, software=True)], 32),
    ])
    def test_it_matches_get_local_ai_decision_run_on_the_same_snapshot(self, gpus, ram_gb):
        from recommendations import get_local_ai_decision

        data = _snapshot(gpus=gpus, total_ram_bytes=int(ram_gb * 1024**3))
        assert data["local_ai"] == get_local_ai_decision(data)


class TestItDoesNotDisturbTheRestOfTheSnapshot:

    def test_the_existing_keys_are_still_there(self):
        """The installer's CUDA check reads .gpu from this same document."""
        data = _snapshot(gpus=[_card(8)])
        for key in ("schema_version", "host", "cpu", "memory", "disks", "gpu",
                    "audio", "privilege", "diagnostics", "build"):
            assert key in data, key

    def test_compact_is_still_one_line(self):
        """PowerShell joins the stdout lines before parsing; a snapshot that
        grew a newline would still parse, but the property is what the
        installer's join relies on."""
        import syscheck

        out = StringIO()
        with patch.object(syscheck, "os") as mock_os, \
             patch("sys.argv", ["syscheck.py", "--compact"]), \
             patch("sys.stdout", out), \
             patch.object(syscheck, "get_host", return_value={
                 "machine_name": "T", "os_name": "W", "os_release": "1", "arch": "x64"}), \
             patch.object(syscheck, "get_cpu", return_value={
                 "vendor": "V", "model": "M", "physical_cores": 1,
                 "logical_cores": 1, "base_mhz": 1, "flags": {}}), \
             patch.object(syscheck, "get_memory", return_value={
                 "total_bytes": 0, "available_bytes": 0}), \
             patch.object(syscheck, "get_disks", return_value=[]), \
             patch.object(syscheck, "probe_gpus_dxgi", return_value=([], "unknown", False, {})), \
             patch.object(syscheck, "get_audio", return_value={
                 "wasapi_available": False, "diagnostics": {"last_error": "m"}}), \
             patch.object(syscheck, "get_privilege_block", return_value={
                 "is_admin": False, "missing_capabilities": [],
                 "recommend_elevation": False, "note": ""}), \
             patch.object(syscheck, "missing", []):
            mock_os.name = "nt"
            mock_os.path = __import__("os").path
            mock_os.environ = {"COMPUTERNAME": "T"}
            main_mod = MagicMock()
            main_mod.__file__ = __file__
            with patch.dict("sys.modules", {"__main__": main_mod}):
                syscheck.main()
        assert "\n" not in out.getvalue()


class TestABrokenLadderDoesNotBreakTheSnapshot:
    """syscheck is also a support tool. Whatever the AI decision does, the
    hardware report has to come out."""

    def test_the_snapshot_survives_a_ladder_that_raises(self):
        import syscheck

        with patch.object(syscheck, "get_local_ai_decision",
                          side_effect=RuntimeError("ladder exploded")):
            data = _snapshot(gpus=[_card(8)])
        assert data["cpu"]["vendor"] == "V", "the hardware report still arrives"
        assert data["local_ai"]["install"] is False
        assert data["local_ai"]["reason"], "and says why there is no answer"

    def test_the_fallback_answer_has_the_same_keys_as_a_real_one(self):
        """The installer reads gpu_layers and model off whatever it finds under
        local_ai. A fallback missing a key would make it read $null where the
        ladder promised a value, so the two shapes have to agree."""
        import syscheck

        from recommendations import get_local_ai_decision

        real = get_local_ai_decision({"gpu": [_card(8)], "memory": {}})
        with patch.object(syscheck, "get_local_ai_decision",
                          side_effect=RuntimeError("ladder exploded")):
            data = _snapshot(gpus=[_card(8)])
        assert set(data["local_ai"]) == set(real)
        assert data["local_ai"]["model"] is None
        assert data["local_ai"]["gpu_layers"] is None
