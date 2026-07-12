# syscheck.py — Windows capability snapshot
# - CPU/RAM
# - Disks with SSD/HDD detection (seek-penalty IOCTL)
# - GPU via DXGI + D3D11 (stdcall vtables, proper D3D11 feature levels; robust fallbacks; diagnostics)
# - Audio via WASAPI (IMMDevice*, IAudioClient, etc.) using ctypes only (no comtypes/WMI)
#
# Usage:
#   uv run python syscheck.py
#   uv run python syscheck.py --compact
#
# Optional deps (for CPU/RAM/disk convenience):
#   uv add psutil py-cpuinfo

import argparse
import ctypes
import ctypes.util
import json
import os
import sys
from ctypes import wintypes

# ========================= Optional deps =========================
missing = []
try:
    import psutil
except Exception:
    psutil = None
    missing.append("psutil")
try:
    import cpuinfo
except Exception:
    cpuinfo = None
    missing.append("py-cpuinfo")

# ========================= Utils =========================
def now_utc_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

def is_process_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def get_host():
    name = os.environ.get("COMPUTERNAME") or ""
    class RTL_OSVERSIONINFOW(ctypes.Structure):
        _fields_ = [
            ("dwOSVersionInfoSize", wintypes.DWORD),
            ("dwMajorVersion", wintypes.DWORD),
            ("dwMinorVersion", wintypes.DWORD),
            ("dwBuildNumber", wintypes.DWORD),
            ("szCSDVersion", wintypes.WCHAR * 128),
        ]
    info = RTL_OSVERSIONINFOW()
    info.dwOSVersionInfoSize = ctypes.sizeof(info)
    ctypes.WinDLL("ntdll").RtlGetVersion(ctypes.byref(info))
    os_release = f"{info.dwMajorVersion}.{info.dwMinorVersion}.{info.dwBuildNumber}"

    class SYSTEM_INFO(ctypes.Structure):
        _fields_ = [
            ("wProcessorArchitecture", wintypes.WORD),
            ("wReserved", wintypes.WORD),
            ("dwPageSize", wintypes.DWORD),
            ("lpMinimumApplicationAddress", wintypes.LPVOID),
            ("lpMaximumApplicationAddress", wintypes.LPVOID),
            ("dwActiveProcessorMask", ctypes.c_size_t),
            ("dwNumberOfProcessors", wintypes.DWORD),
            ("dwProcessorType", wintypes.DWORD),
            ("dwAllocationGranularity", wintypes.DWORD),
            ("wProcessorLevel", wintypes.WORD),
            ("wProcessorRevision", wintypes.WORD),
        ]
    si = SYSTEM_INFO()
    ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(si))
    arch = {9: "x64", 12: "arm64", 5: "arm", 0: "x86"}.get(si.wProcessorArchitecture, "unknown")
    return {"machine_name": name, "os_name": "Windows", "os_release": os_release, "arch": arch}

def get_cpu():
    out = {
        "vendor": "",
        "model": "",
        "physical_cores": psutil.cpu_count(False) if psutil else 0,
        "logical_cores": psutil.cpu_count(True) if psutil else 0,
        "base_mhz": 0,
        "flags": {},
    }
    if cpuinfo:
        try:
            ci = cpuinfo.get_cpu_info()
            out["vendor"] = ci.get("vendor_id_raw") or ci.get("vendor_id", "")
            out["model"] = ci.get("brand_raw") or ci.get("brand", "")
            if isinstance(ci.get("hz_advertised"), tuple):
                out["base_mhz"] = int((ci["hz_advertised"][0] or 0) / 1_000_000)
            flags = set(ci.get("flags") or [])
            def has(x): return x in flags
            out["flags"] = {
                "sse4_1": has("sse4_1"),
                "sse4_2": has("sse4_2"),
                "avx": has("avx"),
                "avx2": has("avx2"),
                "avx512": any(f.startswith("avx512") for f in flags),
                "fma": has("fma"),
                "avx_vnni": has("avxvnni") or has("avx_vnni"),
            }
        except Exception:
            pass
    return out

def get_memory():
    if not psutil:
        return {"total_bytes": 0, "available_bytes": 0}
    m = psutil.virtual_memory()
    return {"total_bytes": int(m.total), "available_bytes": int(m.available)}

# ========================= Disks (SSD/HDD seek-penalty) =========================

def _vol_label_fs(letter: str):
    vol = ctypes.create_unicode_buffer(256)
    fs  = ctypes.create_unicode_buffer(32)
    ctypes.windll.kernel32.GetVolumeInformationW(f"{letter}:\\", vol, len(vol), None, None, None, fs, len(fs))
    return vol.value, fs.value
def _drive_kind(letter: str) -> str:
    root = f"{letter}:\\" 
    dt = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    if dt == 2: return "removable"
    if dt == 4: return "network"
    if dt != 3:
        # try to detect common cloud drives via volume label
        try:
            label, _ = _vol_label_fs(letter)
            if "google drive" in label.lower():
                return "cloud"
        except Exception:
            pass
        return "unknown"
    h = ctypes.windll.kernel32.CreateFileW(f"\\\\.\\{letter}:", 0, 0x3, None, 3, 0, None)
    INVALID = ctypes.c_void_p(-1).value
    if h == INVALID: return "virtual"
    class STORAGE_PROPERTY_QUERY(ctypes.Structure):
        _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD), ("AdditionalParameters", ctypes.c_ubyte * 1)]
    class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD), ("IncursSeekPenalty", wintypes.BOOL)]
    IOCTL = 0x2D1400
    SEEK = 7
    STANDARD = 0
    q = STORAGE_PROPERTY_QUERY()
    q.PropertyId = SEEK; q.QueryType = STANDARD
    desc = DEVICE_SEEK_PENALTY_DESCRIPTOR()
    ret = wintypes.DWORD(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        h, IOCTL, ctypes.byref(q), ctypes.sizeof(q),
        ctypes.byref(desc), ctypes.sizeof(desc),
        ctypes.byref(ret), None
    )
    ctypes.windll.kernel32.CloseHandle(h)
    if ok and hasattr(desc, "IncursSeekPenalty"):
        return "hdd" if bool(desc.IncursSeekPenalty) else "ssd"
    return "virtual"

def get_disks():
    if not psutil:
        return []
    out = []
    seen = set()
    for p in psutil.disk_partitions(all=False):
        if len(p.device) >= 2 and p.device[1] == ":":
            letter = p.device[0].upper()
            if letter in seen: continue
            seen.add(letter)
            try:
                usage = psutil.disk_usage(f"{letter}:\\")
            except Exception:
                continue
            out.append({
                "drive": f"{letter}:",
                "kind": _drive_kind(letter),
                "filesystem": p.fstype or "",
                "total_bytes": int(usage.total),
                "free_bytes": int(usage.free),
                "status": "ok",
            })
    return out


# ---- Helper: driver version via registry (no WMI) ----
def get_display_driver_version_from_registry(vendor_id: int, device_id: int):
    try:
        import winreg
    except Exception:
        return None
    ven = f"VEN_{vendor_id:04X}"
    dev = f"DEV_{device_id:04X}"
    HKLM = winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(HKLM, r"SYSTEM\CurrentControlSet\Enum\PCI") as pci:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(pci, i)
                except OSError:
                    break
                i += 1
                if ven in sub and dev in sub:
                    with winreg.OpenKey(pci, sub) as inst_root:
                        j = 0
                        while True:
                            try:
                                inst = winreg.EnumKey(inst_root, j)
                            except OSError:
                                break
                            j += 1
                            try:
                                with winreg.OpenKey(inst_root, inst) as inst_key:
                                    driver_ref, _ = winreg.QueryValueEx(inst_key, "Driver")
                            except OSError:
                                continue
                            try:
                                with winreg.OpenKey(HKLM, fr"SYSTEM\CurrentControlSet\Control\Class\{driver_ref}") as cls_key:
                                    ver, _ = winreg.QueryValueEx(cls_key, "DriverVersion")
                                    if isinstance(ver, str) and ver:
                                        return ver
                            except OSError:
                                continue
    except OSError:
        return None
    return None

# ========================= GPU (DXGI + D3D11) =========================
def probe_gpus_dxgi():
    """
    Enumerate GPUs using DXGI (1.1 preferred, 1.0 fallback) and create a D3D11 device
    per adapter to determine the max feature level. Uses only valid D3D11 feature levels
    and retries without 11_1 if the runtime doesn’t support it. Also does system-wide
    HARDWARE→WARP→REFERENCE fallback. Returns (gpus, system_fl_str, any_compute, diag).
    """
    def _hx(hr: int) -> str:
        import ctypes as _ct
        return f"0x{_ct.c_uint(hr).value:08X}"

    def _hx(hr: int) -> str:
        import ctypes as _ct
        return f"0x{_ct.c_uint(hr).value:08X}"

    diag = {
        "create_factory1_hr": None,
        "create_factory0_hr": None,
        "enum_path": "",
        "enum_first_hr": None,
        "per_adapter_create_hr": [],
        "fallback_hw_hr": None,
        "fallback_warp_hr": None,
        "fallback_ref_hr": None,
    }

    try:
        dxgi = ctypes.windll.dxgi
        d3d11 = ctypes.windll.d3d11
    except Exception:
        diag["error"] = "dxgi/d3d11 load failed"
        return ([], "unknown", False, diag)

    # Helpers
    def GUID_le(s):
        import uuid
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)

    def VT(ptr, idx, restype, *args):
        # COM vtable uses stdcall on Windows
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *args)(
            ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                        ctypes.POINTER(ctypes.c_void_p))[idx]
        )

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class DXGI_ADAPTER_DESC(ctypes.Structure):  # DXGI 1.0
        _fields_ = [
            ("Description", wintypes.WCHAR * 128),
            ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
            ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
            ("DedicatedVideoMemory", ctypes.c_size_t),
            ("DedicatedSystemMemory", ctypes.c_size_t),
            ("SharedSystemMemory", ctypes.c_size_t),
        ]

    class DXGI_ADAPTER_DESC1(ctypes.Structure):  # DXGI 1.1
        _fields_ = [
            ("Description", wintypes.WCHAR * 128),
            ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
            ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
            ("DedicatedVideoMemory", ctypes.c_size_t),
            ("DedicatedSystemMemory", ctypes.c_size_t),
            ("SharedSystemMemory", ctypes.c_size_t),
            ("AdapterLuid", LUID), ("Flags", wintypes.UINT),
        ]

    S_OK = 0
    DXGI_ERROR_NOT_FOUND = 0x887A0002
    DXGI_ADAPTER_FLAG_SOFTWARE = 0x2

    MSFT_BASIC_VENDOR = 0x1414  # Microsoft Basic Render Driver

    # D3D11 — only D3D11 feature levels (12_x are invalid for D3D11CreateDevice)
    D3D_FEATURE_LEVEL_11_1 = 0xB100
    D3D_FEATURE_LEVEL_11_0 = 0xB000
    D3D_FEATURE_LEVEL_10_1 = 0xA100
    D3D_FEATURE_LEVEL_10_0 = 0xA000
    D3D_FEATURE_LEVEL_9_3  = 0x9300
    D3D_FEATURE_LEVEL_9_2  = 0x9200
    D3D_FEATURE_LEVEL_9_1  = 0x9100

    FL_PRIMARY = (ctypes.c_uint * 7)(
        D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1,
        D3D_FEATURE_LEVEL_10_0, D3D_FEATURE_LEVEL_9_3, D3D_FEATURE_LEVEL_9_2,
        D3D_FEATURE_LEVEL_9_1
    )
    FL_FALLBACK_NO_111 = (ctypes.c_uint * 6)(
        D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0,
        D3D_FEATURE_LEVEL_9_3, D3D_FEATURE_LEVEL_9_2, D3D_FEATURE_LEVEL_9_1
    )
    FL_STR = {
        D3D_FEATURE_LEVEL_11_1: "11_1",
        D3D_FEATURE_LEVEL_11_0: "11_0",
        D3D_FEATURE_LEVEL_10_1: "10_1",
        D3D_FEATURE_LEVEL_10_0: "10_0",
        D3D_FEATURE_LEVEL_9_3:  "9_3",
        D3D_FEATURE_LEVEL_9_2:  "9_2",
        D3D_FEATURE_LEVEL_9_1:  "9_1",
    }
    ORDER = ["11_1", "11_0", "10_1", "10_0", "9_3", "9_2", "9_1", "unknown"]

    D3D11CreateDevice = d3d11.D3D11CreateDevice
    D3D11CreateDevice.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint), ctypes.c_uint,
        ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_void_p),
    ]
    D3D11CreateDevice.restype = ctypes.c_long

    D3D_DRIVER_TYPE_UNKNOWN = 0
    D3D_DRIVER_TYPE_HARDWARE = 1
    D3D_DRIVER_TYPE_REFERENCE = 2
    D3D_DRIVER_TYPE_WARP = 5
    D3D11_SDK_VERSION = 7

    gpus = []
    system_best = "unknown"
    any_compute = False

    def try_create(adapter_or_None, driver_type):
        dev = ctypes.c_void_p()
        ctx = ctypes.c_void_p()
        outfl = ctypes.c_uint()
        hr = D3D11CreateDevice(
            adapter_or_None, driver_type, None, 0,
            FL_PRIMARY, 7, D3D11_SDK_VERSION,
            ctypes.byref(dev), ctypes.byref(outfl), ctypes.byref(ctx)
        )
        # On machines without 11.1 runtime, 11_1 in the array triggers E_INVALIDARG → retry without 11_1
        if hr == 0x80070057:  # E_INVALIDARG
            hr = D3D11CreateDevice(
                adapter_or_None, driver_type, None, 0,
                FL_FALLBACK_NO_111, 6, D3D11_SDK_VERSION,
                ctypes.byref(dev), ctypes.byref(outfl), ctypes.byref(ctx)
            )
        return hr, dev, outfl, ctx

    # -------- Try IDXGIFactory1 (DXGI 1.1) --------
    try:
        CreateDXGIFactory1 = dxgi.CreateDXGIFactory1
        CreateDXGIFactory1.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        CreateDXGIFactory1.restype = ctypes.c_long
        IID_IDXGIFactory1 = GUID_le("{770aae77-244d-44d7-a943-00d0100b8b6d}")
        factory1 = ctypes.c_void_p()
        hr = CreateDXGIFactory1(IID_IDXGIFactory1, ctypes.byref(factory1))
        diag["create_factory1_hr"] = _hx(hr)
    except Exception:
        factory1 = None
        diag["create_factory1_hr"] = "N/A"

    def enumerate_with_factory(factory_ptr, use_v1=True):
        nonlocal gpus, system_best, any_compute
        if use_v1:
            Enum = VT(factory_ptr, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))  # EnumAdapters1
            get_desc_idx = 10  # IDXGIAdapter1::GetDesc1
            DescX = DXGI_ADAPTER_DESC1
        else:
            Enum = VT(factory_ptr, 7, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))   # EnumAdapters
            get_desc_idx = 8   # IDXGIAdapter::GetDesc
            DescX = DXGI_ADAPTER_DESC

        first_hr = None
        idx = 0
        while True:
            adapter = ctypes.c_void_p()
            hr2 = Enum(factory_ptr, idx, ctypes.byref(adapter))
            if first_hr is None:
                first_hr = hr2
            if hr2 == DXGI_ERROR_NOT_FOUND:
                break
            if hr2 != S_OK or not adapter:
                break

            GetDescX = VT(adapter, get_desc_idx, ctypes.c_long, ctypes.POINTER(DescX))
            desc = DescX()
            hr3 = GetDescX(adapter, ctypes.byref(desc))
            Release = VT(adapter, 2, ctypes.c_ulong)
            if hr3 != S_OK:
                Release(adapter); idx += 1; continue

            # Detect software adapter flag but keep it in list
            is_software = bool(use_v1 and (getattr(desc, "Flags", 0) & DXGI_ADAPTER_FLAG_SOFTWARE))

            # Create device on this adapter
            hr_dev, dev, outfl, ctx = try_create(adapter, D3D_DRIVER_TYPE_UNKNOWN)
            diag["per_adapter_create_hr"].append(_hx(hr_dev))
            fls = "unknown"; compute = False
            if hr_dev == S_OK and dev:
                fls = FL_STR.get(outfl.value, "unknown")
                compute = outfl.value in (D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_11_1)
                VT(dev, 2, ctypes.c_ulong)(dev)
                VT(ctx, 2, ctypes.c_ulong)(ctx)

            if not is_software:
                if ORDER.index(fls) < ORDER.index(system_best):
                    system_best = fls
                any_compute |= compute

            name = desc.Description.rstrip("\x00")
            vendor = int(getattr(desc, "VendorId", 0))
            device = int(getattr(desc, "DeviceId", 0))
            dvram = int(getattr(desc, "DedicatedVideoMemory", 0))
            svram = int(getattr(desc, "SharedSystemMemory", 0))
            # Mark Microsoft Basic Render Driver as software by vendor ID as well
            is_software = bool(is_software or (vendor == MSFT_BASIC_VENDOR))
            gpus.append({
                "index": len(gpus),
                "name": name,
                "vendor_id": vendor,
                "device_id": device,
                "dedicated_vram_bytes": dvram,
                "shared_system_bytes": svram,
                "software": is_software,
                "driver_version": get_display_driver_version_from_registry(vendor, device) or "",
                "dx_feature_level_max": fls,
                "compute_support": {
                    "dx12": False,  # D3D11 path can't assert DX12; keep False here
                    "d3d11": fls not in ("unknown", "9_1", "9_2", "9_3", "10_0", "10_1")  # true if 11_0 or 11_1
                },
            })

            Release(adapter); idx += 1

        return first_hr

    if factory1:
        diag["enum_path"] = "factory1/EnumAdapters1"
        diag["enum_first_hr"] = f"0x{enumerate_with_factory(factory1, True):08X}"
        VT(factory1, 2, ctypes.c_ulong)(factory1)

    # -------- Fallback to IDXGIFactory (DXGI 1.0) --------
    if not gpus:
        CreateDXGIFactory = dxgi.CreateDXGIFactory
        CreateDXGIFactory.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_void_p)]
        CreateDXGIFactory.restype = ctypes.c_long
        IID_IDXGIFactory = GUID_le("{7B7166EC-21C7-44AE-B21A-C9AE321AE369}")
        factory0 = ctypes.c_void_p()
        hr0 = CreateDXGIFactory(IID_IDXGIFactory, ctypes.byref(factory0))
        diag["create_factory0_hr"] = f"0x{hr0:08X}"
        if hr0 == S_OK and factory0:
            diag["enum_path"] = "factory0/EnumAdapters"
            diag["enum_first_hr"] = _hx(enumerate_with_factory(factory0, False))
            VT(factory0, 2, ctypes.c_ulong)(factory0)

    # -------- System feature-level fallback (HARDWARE → WARP → REFERENCE) --------
    if system_best == "unknown":
        for drv, key in ((D3D_DRIVER_TYPE_HARDWARE, "fallback_hw_hr"),
                         (D3D_DRIVER_TYPE_WARP,     "fallback_warp_hr"),
                         (D3D_DRIVER_TYPE_REFERENCE,"fallback_ref_hr")):
            hr_def, dev, outfl, ctx = try_create(None, drv)
            diag[key] = _hx(hr_def)
            if hr_def == S_OK and dev:
                fls = FL_STR.get(outfl.value, "unknown")
                system_best = fls
                if drv == D3D_DRIVER_TYPE_HARDWARE and outfl.value in (D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_11_1):
                    any_compute = True
                VT(dev, 2, ctypes.c_ulong)(dev)
                VT(ctx, 2, ctypes.c_ulong)(ctx)
                break

    return (gpus, system_best, any_compute, diag)

# ========================= Audio (WASAPI via ctypes) =========================
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_ALL = 23
S_OK = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
    def __init__(self, s: str):
        import uuid
        u = uuid.UUID(s)
        super().__init__()
        self.Data1 = u.time_low
        self.Data2 = u.time_mid
        self.Data3 = u.time_hi_version
        d = u.bytes[8:]
        for i in range(8): self.Data4[i] = d[i]

class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]
PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)

# Opaque COM interfaces
class IMMDeviceEnumerator(ctypes.Structure): _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IMMDevice(ctypes.Structure):           _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IPropertyStore(ctypes.Structure):      _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IAudioMeterInformation(ctypes.Structure): _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IAudioSessionEnumerator(ctypes.Structure): _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IAudioSessionControl(ctypes.Structure): _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IAudioSessionManager2(ctypes.Structure): _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]
class IAudioClient(ctypes.Structure):        _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]

IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IAudioMeterInformation = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
IID_IAudioSessionManager2  = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioClient           = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")

def VT(ptr, idx, restype, *args):
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *args)(
        ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                    ctypes.POINTER(ctypes.c_void_p))[idx]
    )

def com_init():
    ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

def com_uninit():
    try: ctypes.windll.ole32.CoUninitialize()
    except Exception: pass

def get_audio():
    out = {
        "wasapi_available": True,
        "default_render_present": False,
        "default_capture_present": False,
        "render_device": {"friendly_name": "", "id": "", "endpoint_state": 0},
        "capture_device": {"friendly_name": "", "id": "", "endpoint_state": 0},
        "activity_snapshot": {
            "render_peak_0to1": 0.0, "capture_peak_0to1": 0.0,
            "render_sessions_active": 0, "capture_sessions_active": 0
        },
        "capabilities": {"loopback_supported": False, "capture_supported": False, "default_device_direction": "none"},
        "diagnostics": {"last_hr": 0, "last_error": ""}
    }
    try:
        com_init()
        pEnum = ctypes.POINTER(IMMDeviceEnumerator)()
        hr = ctypes.windll.ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDeviceEnumerator), None, 1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)
        )
        if hr != S_OK or not pEnum:
            out["wasapi_available"] = False
            out["diagnostics"]["last_error"] = f"CoCreateInstance hr=0x{hr:08X}"
            return out

        eRender, eCapture, eConsole = 0, 1, 0
        GetDefaultAudioEndpoint = VT(pEnum, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(IMMDevice)))

        def get_ep(flow):
            pp = ctypes.POINTER(IMMDevice)()
            hr = GetDefaultAudioEndpoint(pEnum, flow, eConsole, ctypes.byref(pp))
            if hr != S_OK or not pp:
                return None, "", 0, "", None
            Activate          = VT(pp, 3, ctypes.c_long, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
            OpenPropertyStore = VT(pp, 4, ctypes.c_long, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(IPropertyStore)))
            GetId             = VT(pp, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_wchar_p))
            GetState          = VT(pp, 6, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))

            pwsz = ctypes.c_wchar_p(); state = wintypes.DWORD(0)
            GetId(pp, ctypes.byref(pwsz))
            GetState(pp, ctypes.byref(state))
            dev_id = pwsz.value if pwsz else ""
            try: ctypes.windll.ole32.CoTaskMemFree(pwsz)
            except Exception: pass

            name = ""
            store = ctypes.POINTER(IPropertyStore)()
            if OpenPropertyStore(pp, 0, ctypes.byref(store)) == S_OK and store:
                GetValue = VT(store, 5, ctypes.c_long, ctypes.POINTER(PROPERTYKEY), ctypes.c_void_p)
                class _PV(ctypes.Structure):
                    _fields_ = [("vt", wintypes.USHORT),
                                ("wReserved1", wintypes.USHORT), ("wReserved2", wintypes.USHORT), ("wReserved3", wintypes.USHORT),
                                ("pszVal", ctypes.c_wchar_p)]
                pv = _PV()
                if GetValue(store, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == S_OK:
                    if pv.vt == 31 and pv.pszVal:  # VT_LPWSTR
                        name = pv.pszVal

            return pp, name, int(state.value), dev_id, Activate

        rdev, rname, rstate, rid, rActivate = get_ep(eRender)
        cdev, cname, cstate, cid, cActivate = get_ep(eCapture)

        out["default_render_present"] = bool(rdev)
        out["default_capture_present"] = bool(cdev)
        out["render_device"] = {"friendly_name": rname, "id": rid, "endpoint_state": rstate}
        out["capture_device"] = {"friendly_name": cname, "id": cid, "endpoint_state": cstate}
        out["capabilities"]["default_device_direction"] = "duplex" if (rdev and cdev) else ("render" if rdev else ("capture" if cdev else "none"))

        # Helpers for IAudio*
        def read_peak(dev_ptr, Activate):
            if not dev_ptr or not Activate: return 0.0
            p = ctypes.c_void_p()
            if Activate(dev_ptr, ctypes.byref(IID_IAudioMeterInformation), CLSCTX_ALL, None, ctypes.byref(p)) != S_OK or not p:
                return 0.0
            meter = ctypes.cast(p, ctypes.POINTER(IAudioMeterInformation))
            GetPeakValue = VT(meter, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
            val = ctypes.c_float(0.0)
            if GetPeakValue(meter, ctypes.byref(val)) == S_OK:
                return float(val.value)
            return 0.0

        def count_sessions(dev_ptr, Activate):
            if not dev_ptr or not Activate: return 0
            p = ctypes.c_void_p()
            if Activate(dev_ptr, ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, ctypes.byref(p)) != S_OK or not p:
                return 0
            mgr2 = ctypes.cast(p, ctypes.POINTER(IAudioSessionManager2))
            GetSessionEnumerator = VT(mgr2, 5, ctypes.c_long, ctypes.POINTER(ctypes.POINTER(IAudioSessionEnumerator)))
            penum = ctypes.POINTER(IAudioSessionEnumerator)()
            if GetSessionEnumerator(mgr2, ctypes.byref(penum)) != S_OK or not penum:
                return 0
            GetCount  = VT(penum, 3, ctypes.c_long, ctypes.POINTER(wintypes.INT))
            GetSession= VT(penum, 4, ctypes.c_long, wintypes.INT, ctypes.POINTER(ctypes.c_void_p))
            n = wintypes.INT(0)
            if GetCount(penum, ctypes.byref(n)) != S_OK: return 0
            active = 0
            for i in range(n.value):
                pctrl = ctypes.c_void_p()
                if GetSession(penum, i, ctypes.byref(pctrl)) == S_OK and pctrl:
                    ctrl = ctypes.cast(pctrl, ctypes.POINTER(IAudioSessionControl))
                    GetState = VT(ctrl, 3, ctypes.c_long, ctypes.POINTER(wintypes.INT))
                    st = wintypes.INT(0)
                    if GetState(ctrl, ctypes.byref(st)) == S_OK and int(st.value) == 1:
                        active += 1
            return active

        def check_capture_supported(dev_ptr, Activate):
            if not dev_ptr or not Activate: return False
            p = ctypes.c_void_p()
            if Activate(dev_ptr, ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(p)) != S_OK or not p:
                return False
            ac = ctypes.cast(p, ctypes.POINTER(IAudioClient))
            GetMixFormat = VT(ac, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
            Initialize   = VT(ac, 3, ctypes.c_long, wintypes.DWORD, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.POINTER(GUID))
            wf = ctypes.c_void_p()
            if GetMixFormat(ac, ctypes.byref(wf)) != S_OK:
                return False
            return Initialize(ac, 0, 0, 10_000_000, 0, wf, None) == S_OK

        def check_loopback_supported(dev_ptr, Activate):
            if not dev_ptr or not Activate: return False
            p = ctypes.c_void_p()
            if Activate(dev_ptr, ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(p)) != S_OK or not p:
                return False
            ac = ctypes.cast(p, ctypes.POINTER(IAudioClient))
            GetMixFormat = VT(ac, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
            Initialize   = VT(ac, 3, ctypes.c_long, wintypes.DWORD, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.POINTER(GUID))
            wf = ctypes.c_void_p()
            if GetMixFormat(ac, ctypes.byref(wf)) != S_OK:
                return False
            return Initialize(ac, 0, AUDCLNT_STREAMFLAGS_LOOPBACK, 10_000_000, 0, wf, None) == S_OK

        if rdev:
            out["activity_snapshot"]["render_peak_0to1"] = read_peak(rdev, rActivate)
            out["activity_snapshot"]["render_sessions_active"] = count_sessions(rdev, rActivate)
            out["capabilities"]["loopback_supported"] = check_loopback_supported(rdev, rActivate)
        if cdev:
            out["activity_snapshot"]["capture_peak_0to1"] = read_peak(cdev, cActivate)
            out["activity_snapshot"]["capture_sessions_active"] = count_sessions(cdev, cActivate)
            out["capabilities"]["capture_supported"] = check_capture_supported(cdev, cActivate)

    except Exception as e:
        out["wasapi_available"] = False
        out["diagnostics"]["last_error"] = f"WASAPI(ctypes) failed: {e}"
    finally:
        com_uninit()

    return out

# ========================= Privilege =========================
def get_privilege_block():
    is_admin = is_process_elevated()
    missing_caps = []
    if not is_admin:
        missing_caps.extend(["storage_smart", "lowlevel_gpu"])
    return {
        "is_admin": is_admin,
        "missing_capabilities": sorted(set(missing_caps)),
        "recommend_elevation": False,
        "note": "Run elevated only if you need disk SMART or deeper GPU info.",
    }

# ========================= Main =========================
def main():
    import os, __main__, time
    if os.name != "nt":
        print("This tool is Windows-only.", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description="Windows system capability snapshot (DXGI+D3D11 GPU, WASAPI audio, SSD/HDD)."
    )
    ap.add_argument("--compact", action="store_true", help="Emit single-line JSON.")
    args = ap.parse_args()

    host   = get_host()
    cpu    = get_cpu()
    memory = get_memory()
    disks  = get_disks()

    gpus, gpu_sys_fl, gpu_compute, gpu_diag = probe_gpus_dxgi()
    audio  = get_audio()
    priv   = get_privilege_block()

    data = {
        "schema_version": 1,
        "schema_audio_minor": 1,
        "generated_at_utc": now_utc_iso(),
        "host": host,
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "gpu": gpus,
        "gpu_feature_level_max": gpu_sys_fl,
        "gpu_compute_capable": gpu_compute,
        "audio": audio,
        "privilege": priv,
        "diagnostics": {
            "python_version": sys.version.split()[0],
            "python_executable": sys.executable,
            "missing_modules": missing,
            "dlls": {
                "d3d11": ctypes.util.find_library("d3d11"),
                "dxgi": ctypes.util.find_library("dxgi"),
                "mmdevapi": ctypes.util.find_library("MMDevAPI"),
            },
            "gpu_diagnostics": gpu_diag,
        },
    }

    # --- build metadata marker (add this) ---

    script_path = os.path.abspath(getattr(__main__, "__file__", ""))
    data["build"] = {
        "script_path": script_path,
        "script_mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(os.path.getmtime(script_path))),
        "script_sha_hint": hex(abs(hash(open(script_path, "rb").read()[:4096]))),
    }
    # --- end marker block ---

    if args.compact:
        sys.stdout.write(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    else:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
