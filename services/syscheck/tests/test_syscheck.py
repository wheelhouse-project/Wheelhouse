# test_syscheck.py - Tests for Windows system capability detection
#
# Strategy: Mock all Windows-specific APIs (ctypes.windll, psutil, cpuinfo)
# to test pure logic paths. Focus on functions that have testable logic
# rather than those that are pure COM/ctypes plumbing.

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


# -----------------------------------------------------------------------
# now_utc_iso
# -----------------------------------------------------------------------

class TestNowUtcIso:

    def test_returns_iso_format_string(self):
        from syscheck import now_utc_iso
        result = now_utc_iso()
        # Should parse as valid ISO datetime
        dt = datetime.datetime.fromisoformat(result)
        assert dt.tzinfo is not None  # timezone-aware

    def test_has_no_microseconds(self):
        from syscheck import now_utc_iso
        result = now_utc_iso()
        dt = datetime.datetime.fromisoformat(result)
        assert dt.microsecond == 0


# -----------------------------------------------------------------------
# is_process_elevated
# -----------------------------------------------------------------------

class TestIsProcessElevated:

    @patch("syscheck.ctypes")
    def test_returns_true_when_admin(self, mock_ctypes):
        mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = 1
        from syscheck import is_process_elevated
        assert is_process_elevated() is True

    def test_returns_false_on_exception(self):
        """If ctypes call fails, should return False (not raise)."""
        from syscheck import is_process_elevated
        with patch("syscheck.ctypes") as mock_ctypes:
            mock_ctypes.windll.shell32.IsUserAnAdmin.side_effect = OSError("access denied")
            result = is_process_elevated()
            assert result is False


# -----------------------------------------------------------------------
# get_cpu
# -----------------------------------------------------------------------

class TestGetCpu:

    def test_returns_dict_with_expected_keys(self):
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.side_effect = lambda logical: 8 if logical else 4
            mock_cpuinfo.get_cpu_info.return_value = {
                "vendor_id_raw": "GenuineIntel",
                "brand_raw": "Intel Core i7-12700K",
                "flags": ["sse4_1", "sse4_2", "avx", "avx2", "fma"],
            }
            result = get_cpu()

        assert result["vendor"] == "GenuineIntel"
        assert result["model"] == "Intel Core i7-12700K"
        assert result["physical_cores"] == 4
        assert result["logical_cores"] == 8
        assert result["flags"]["sse4_1"] is True
        assert result["flags"]["avx2"] is True
        assert result["flags"]["avx512"] is False

    def test_handles_missing_psutil(self):
        from syscheck import get_cpu
        with patch("syscheck.psutil", None), \
             patch("syscheck.cpuinfo", None):
            result = get_cpu()

        assert result["physical_cores"] == 0
        assert result["logical_cores"] == 0
        assert result["vendor"] == ""
        assert result["model"] == ""

    def test_handles_cpuinfo_exception(self):
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.return_value = 4
            mock_cpuinfo.get_cpu_info.side_effect = RuntimeError("cpuinfo failed")
            result = get_cpu()

        # Should degrade gracefully
        assert result["vendor"] == ""
        assert result["model"] == ""

    def test_avx512_detected_from_prefix(self):
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.return_value = 4
            mock_cpuinfo.get_cpu_info.return_value = {
                "flags": ["avx512f", "avx512bw", "avx2"],
            }
            result = get_cpu()

        assert result["flags"]["avx512"] is True

    def test_avx_vnni_alternative_flag(self):
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.return_value = 4
            mock_cpuinfo.get_cpu_info.return_value = {
                "flags": ["avx_vnni"],
            }
            result = get_cpu()

        assert result["flags"]["avx_vnni"] is True

    def test_hz_advertised_tuple_conversion(self):
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.return_value = 4
            mock_cpuinfo.get_cpu_info.return_value = {
                "hz_advertised": (3_700_000_000, 0),
                "flags": [],
            }
            result = get_cpu()

        assert result["base_mhz"] == 3700

    def test_fallback_vendor_and_brand_keys(self):
        """When vendor_id_raw is missing, falls back to vendor_id."""
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.return_value = 4
            mock_cpuinfo.get_cpu_info.return_value = {
                "vendor_id": "AuthenticAMD",
                "brand": "AMD Ryzen 5 5600X",
                "flags": [],
            }
            result = get_cpu()

        assert result["vendor"] == "AuthenticAMD"
        assert result["model"] == "AMD Ryzen 5 5600X"


# -----------------------------------------------------------------------
# get_memory
# -----------------------------------------------------------------------

class TestGetMemory:

    def test_returns_memory_info(self):
        from syscheck import get_memory
        mock_mem = MagicMock()
        mock_mem.total = 34359738368  # 32 GB
        mock_mem.available = 17179869184  # 16 GB
        with patch("syscheck.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            result = get_memory()

        assert result["total_bytes"] == 34359738368
        assert result["available_bytes"] == 17179869184

    def test_returns_zeros_without_psutil(self):
        from syscheck import get_memory
        with patch("syscheck.psutil", None):
            result = get_memory()

        assert result["total_bytes"] == 0
        assert result["available_bytes"] == 0


# -----------------------------------------------------------------------
# get_disks
# -----------------------------------------------------------------------

class TestGetDisks:

    def test_returns_empty_without_psutil(self):
        from syscheck import get_disks
        with patch("syscheck.psutil", None):
            result = get_disks()
        assert result == []

    def test_skips_duplicate_drive_letters(self):
        from syscheck import get_disks
        part1 = MagicMock()
        part1.device = "C:\\"
        part1.fstype = "NTFS"
        part2 = MagicMock()
        part2.device = "C:\\"
        part2.fstype = "NTFS"

        usage = MagicMock()
        usage.total = 500 * 1024**3
        usage.free = 100 * 1024**3

        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck._drive_kind", return_value="ssd"):
            mock_psutil.disk_partitions.return_value = [part1, part2]
            mock_psutil.disk_usage.return_value = usage
            result = get_disks()

        # Only one entry for C:
        assert len(result) == 1
        assert result[0]["drive"] == "C:"

    def test_skips_partitions_with_usage_error(self):
        from syscheck import get_disks
        part = MagicMock()
        part.device = "D:\\"
        part.fstype = "NTFS"

        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck._drive_kind", return_value="ssd"):
            mock_psutil.disk_partitions.return_value = [part]
            mock_psutil.disk_usage.side_effect = PermissionError("access denied")
            result = get_disks()

        assert result == []

    def test_returns_disk_info(self):
        from syscheck import get_disks
        part = MagicMock()
        part.device = "C:\\"
        part.fstype = "NTFS"

        usage = MagicMock()
        usage.total = 500 * 1024**3
        usage.free = 100 * 1024**3

        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck._drive_kind", return_value="ssd"):
            mock_psutil.disk_partitions.return_value = [part]
            mock_psutil.disk_usage.return_value = usage
            result = get_disks()

        assert len(result) == 1
        assert result[0]["drive"] == "C:"
        assert result[0]["kind"] == "ssd"
        assert result[0]["filesystem"] == "NTFS"
        assert result[0]["total_bytes"] == 500 * 1024**3
        assert result[0]["free_bytes"] == 100 * 1024**3
        assert result[0]["status"] == "ok"


# -----------------------------------------------------------------------
# get_privilege_block
# -----------------------------------------------------------------------

class TestGetPrivilegeBlock:

    def test_elevated_returns_no_missing_caps(self):
        from syscheck import get_privilege_block
        with patch("syscheck.is_process_elevated", return_value=True):
            result = get_privilege_block()

        assert result["is_admin"] is True
        assert result["missing_capabilities"] == []
        assert result["recommend_elevation"] is False

    def test_not_elevated_reports_missing_caps(self):
        from syscheck import get_privilege_block
        with patch("syscheck.is_process_elevated", return_value=False):
            result = get_privilege_block()

        assert result["is_admin"] is False
        assert "storage_smart" in result["missing_capabilities"]
        assert "lowlevel_gpu" in result["missing_capabilities"]

    def test_missing_capabilities_are_sorted(self):
        from syscheck import get_privilege_block
        with patch("syscheck.is_process_elevated", return_value=False):
            result = get_privilege_block()

        caps = result["missing_capabilities"]
        assert caps == sorted(caps)


# -----------------------------------------------------------------------
# get_display_driver_version_from_registry
# -----------------------------------------------------------------------

class TestGetDisplayDriverVersionFromRegistry:

    def test_returns_none_when_winreg_unavailable(self):
        from syscheck import get_display_driver_version_from_registry
        with patch.dict("sys.modules", {"winreg": None}):
            result = get_display_driver_version_from_registry(0x10DE, 0x2504)
        # When import fails, should return None
        assert result is None

    def test_returns_none_on_os_error(self):
        from syscheck import get_display_driver_version_from_registry
        mock_winreg = MagicMock()
        mock_winreg.HKEY_LOCAL_MACHINE = 0x80000002
        mock_winreg.OpenKey.side_effect = OSError("registry access denied")
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            result = get_display_driver_version_from_registry(0x10DE, 0x2504)
        assert result is None

    def test_returns_none_when_no_matching_device(self):
        from syscheck import get_display_driver_version_from_registry
        mock_winreg = MagicMock()
        mock_winreg.HKEY_LOCAL_MACHINE = 0x80000002

        # EnumKey returns a subkey that doesn't match, then raises OSError
        pci_ctx = MagicMock()
        mock_winreg.OpenKey.return_value.__enter__ = MagicMock(return_value=pci_ctx)
        mock_winreg.OpenKey.return_value.__exit__ = MagicMock(return_value=False)
        mock_winreg.EnumKey.side_effect = [
            "VEN_8086&DEV_1234&SUBSYS_00000000",  # Does not match 10DE/2504
            OSError("no more subkeys"),
        ]

        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            result = get_display_driver_version_from_registry(0x10DE, 0x2504)
        assert result is None


# -----------------------------------------------------------------------
# probe_gpus_dxgi - smoke test (heavily mocked)
# -----------------------------------------------------------------------

class TestProbeGpusDxgi:

    def test_returns_empty_when_dxgi_unavailable(self):
        """When dxgi/d3d11 DLLs can't load, returns empty list with diag."""
        from syscheck import probe_gpus_dxgi
        with patch("syscheck.ctypes") as mock_ctypes:
            # Make windll.dxgi raise
            type(mock_ctypes.windll).dxgi = PropertyMock(side_effect=OSError("no dxgi"))
            gpus, sys_fl, compute, diag = probe_gpus_dxgi()

        assert gpus == []
        assert sys_fl == "unknown"
        assert compute is False
        assert "error" in diag


# -----------------------------------------------------------------------
# get_audio - smoke test (heavily mocked)
# -----------------------------------------------------------------------

class TestGetAudio:

    def test_returns_structure_when_wasapi_fails(self):
        """When COM initialization or WASAPI fails, returns valid dict."""
        from syscheck import get_audio
        with patch("syscheck.com_init", side_effect=OSError("COM init failed")), \
             patch("syscheck.com_uninit"):
            result = get_audio()

        assert result["wasapi_available"] is False
        assert "diagnostics" in result
        assert "last_error" in result["diagnostics"]

    def test_audio_output_has_expected_keys(self):
        """Verify the output structure even on failure."""
        from syscheck import get_audio
        with patch("syscheck.com_init", side_effect=OSError("no COM")), \
             patch("syscheck.com_uninit"):
            result = get_audio()

        assert "default_render_present" in result
        assert "default_capture_present" in result
        assert "render_device" in result
        assert "capture_device" in result
        assert "activity_snapshot" in result
        assert "capabilities" in result


# -----------------------------------------------------------------------
# main - integration smoke test
# -----------------------------------------------------------------------

class TestMain:

    def test_non_windows_exits(self):
        """On non-Windows platforms, main() should exit with code 1.

        main() does `import os` locally, so we patch the os module itself
        rather than syscheck.os."""
        import os as real_os
        import syscheck
        with patch("os.name", "posix"), \
             patch("sys.argv", ["syscheck.py"]):
            with pytest.raises(SystemExit) as exc_info:
                syscheck.main()
            assert exc_info.value.code == 1

    def test_main_produces_json_output(self):
        """When all subsystems are mocked, main() writes valid JSON to stdout."""
        import syscheck
        from io import StringIO

        mock_stdout = StringIO()
        with patch.object(syscheck, "os") as mock_os, \
             patch("sys.argv", ["syscheck.py"]), \
             patch("sys.stdout", mock_stdout), \
             patch.object(syscheck, "get_host", return_value={
                 "machine_name": "TESTPC", "os_name": "Windows",
                 "os_release": "10.0.19045", "arch": "x64"
             }), \
             patch.object(syscheck, "get_cpu", return_value={
                 "vendor": "TestVendor", "model": "TestCPU",
                 "physical_cores": 4, "logical_cores": 8,
                 "base_mhz": 3600, "flags": {}
             }), \
             patch.object(syscheck, "get_memory", return_value={
                 "total_bytes": 16 * 1024**3, "available_bytes": 8 * 1024**3
             }), \
             patch.object(syscheck, "get_disks", return_value=[]), \
             patch.object(syscheck, "probe_gpus_dxgi", return_value=([], "unknown", False, {})), \
             patch.object(syscheck, "get_audio", return_value={
                 "wasapi_available": False, "diagnostics": {"last_error": "mocked"}
             }), \
             patch.object(syscheck, "get_privilege_block", return_value={
                 "is_admin": False, "missing_capabilities": [], "recommend_elevation": False, "note": ""
             }), \
             patch.object(syscheck, "missing", []):
            mock_os.name = "nt"
            mock_os.path = __import__("os").path
            mock_os.environ = {"COMPUTERNAME": "TESTPC"}
            # Mock __main__.__file__ for the build block
            mock_main = MagicMock()
            mock_main.__file__ = __file__
            with patch.dict("sys.modules", {"__main__": mock_main}):
                syscheck.main()

        output = mock_stdout.getvalue()
        data = json.loads(output)
        assert data["schema_version"] == 1
        assert data["host"]["machine_name"] == "TESTPC"
        assert data["cpu"]["vendor"] == "TestVendor"

    def test_compact_flag_produces_single_line(self):
        """--compact should produce JSON without newlines."""
        import syscheck
        from io import StringIO

        mock_stdout = StringIO()
        with patch.object(syscheck, "os") as mock_os, \
             patch("sys.argv", ["syscheck.py", "--compact"]), \
             patch("sys.stdout", mock_stdout), \
             patch.object(syscheck, "get_host", return_value={
                 "machine_name": "TESTPC", "os_name": "Windows",
                 "os_release": "10.0.19045", "arch": "x64"
             }), \
             patch.object(syscheck, "get_cpu", return_value={
                 "vendor": "V", "model": "M", "physical_cores": 1,
                 "logical_cores": 1, "base_mhz": 1000, "flags": {}
             }), \
             patch.object(syscheck, "get_memory", return_value={
                 "total_bytes": 0, "available_bytes": 0
             }), \
             patch.object(syscheck, "get_disks", return_value=[]), \
             patch.object(syscheck, "probe_gpus_dxgi", return_value=([], "unknown", False, {})), \
             patch.object(syscheck, "get_audio", return_value={
                 "wasapi_available": False, "diagnostics": {"last_error": "mocked"}
             }), \
             patch.object(syscheck, "get_privilege_block", return_value={
                 "is_admin": False, "missing_capabilities": [], "recommend_elevation": False, "note": ""
             }), \
             patch.object(syscheck, "missing", []):
            mock_os.name = "nt"
            mock_os.path = __import__("os").path
            mock_os.environ = {"COMPUTERNAME": "TESTPC"}
            mock_main = MagicMock()
            mock_main.__file__ = __file__
            with patch.dict("sys.modules", {"__main__": mock_main}):
                syscheck.main()

        output = mock_stdout.getvalue()
        # Compact JSON should be a single line (no newlines)
        assert "\n" not in output
        # Still valid JSON
        data = json.loads(output)
        assert "schema_version" in data


# -----------------------------------------------------------------------
# Adversarial: unexpected system responses
# -----------------------------------------------------------------------

class TestAdversarial:

    def test_get_cpu_with_empty_flags(self):
        """cpuinfo returns empty flags list."""
        from syscheck import get_cpu
        with patch("syscheck.psutil") as mock_psutil, \
             patch("syscheck.cpuinfo") as mock_cpuinfo:
            mock_psutil.cpu_count.return_value = 2
            mock_cpuinfo.get_cpu_info.return_value = {
                "flags": [],
            }
            result = get_cpu()

        assert all(v is False for v in result["flags"].values())

    def test_get_disks_with_short_device_name(self):
        """Partition with device name shorter than 2 chars is skipped."""
        from syscheck import get_disks
        part = MagicMock()
        part.device = "X"  # len < 2

        with patch("syscheck.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = [part]
            result = get_disks()

        assert result == []

    def test_get_disks_with_non_colon_device(self):
        """Partition device where second char is not ':' is skipped."""
        from syscheck import get_disks
        part = MagicMock()
        part.device = "/dev/sda1"  # No colon at position 1

        with patch("syscheck.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = [part]
            result = get_disks()

        assert result == []

    def test_get_memory_returns_ints(self):
        """Verify memory values are converted to int even if psutil returns float-like."""
        from syscheck import get_memory
        mock_mem = MagicMock()
        mock_mem.total = 17179869184.0  # float
        mock_mem.available = 8589934592.0

        with patch("syscheck.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_mem
            result = get_memory()

        assert isinstance(result["total_bytes"], int)
        assert isinstance(result["available_bytes"], int)
