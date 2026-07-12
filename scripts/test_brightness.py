"""Standalone laptop brightness control tester.

Tests multiple approaches to control laptop display brightness:
1. WMI (Windows Management Instrumentation)
2. PowerShell (WMIC)
3. ctypes (Windows Display API)
4. screen-brightness-control package

Run this script directly to test which methods work on your laptop.
"""

import subprocess
import sys


def test_wmi_approach():
    """Test WMI-based brightness control."""
    print("\n" + "="*60)
    print("TEST 1: WMI Approach")
    print("="*60)
    
    try:
        import wmi
        
        # Connect to brightness namespace
        connection = wmi.WMI(namespace="root\\wmi")
        
        # Query current brightness
        brightness_instances = connection.query("SELECT * FROM WmiMonitorBrightness")
        if not brightness_instances:
            print("[!] No WmiMonitorBrightness instances found")
            return False
        
        current = brightness_instances[0].CurrentBrightness
        print(f"[+] Current brightness: {current}%")
        
        # Query brightness methods
        methods = connection.query("SELECT * FROM WmiMonitorBrightnessMethods")
        if not methods:
            print("[!] No WmiMonitorBrightnessMethods found")
            return False
        
        # Try to set brightness
        new_level = 50 if current != 50 else 60
        print(f"[*] Attempting to set brightness to {new_level}%...")
        
        result = methods[0].WmiSetBrightness(1, new_level)
        print(f"[*] WmiSetBrightness returned: {result}")
        
        # Verify change
        brightness_instances = connection.query("SELECT * FROM WmiMonitorBrightness")
        new_current = brightness_instances[0].CurrentBrightness
        print(f"[+] New brightness: {new_current}%")
        
        if new_current == new_level:
            print("[+] SUCCESS: WMI approach works!")
            # Restore original
            methods[0].WmiSetBrightness(1, current)
            print(f"[*] Restored to {current}%")
            return True
        else:
            print("[!] Brightness did not change as expected")
            return False
            
    except ImportError:
        print("[!] WMI module not installed. Install with: pip install WMI")
        return False
    except Exception as e:
        print(f"[!] WMI error: {e}")
        return False


def test_powershell_wmic():
    """Test PowerShell WMIC approach."""
    print("\n" + "="*60)
    print("TEST 2: PowerShell WMIC Approach")
    print("="*60)
    
    try:
        # Get current brightness
        get_cmd = "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness"
        result = subprocess.run(
            ["powershell", "-Command", get_cmd],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode != 0:
            print(f"[!] PowerShell get failed: {result.stderr}")
            return False
        
        current = int(result.stdout.strip())
        print(f"[+] Current brightness: {current}%")
        
        # Set brightness
        new_level = 50 if current != 50 else 60
        set_cmd = f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {new_level})"
        print(f"[*] Attempting to set brightness to {new_level}%...")
        
        result = subprocess.run(
            ["powershell", "-Command", set_cmd],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode != 0:
            print(f"[!] PowerShell set failed: {result.stderr}")
            return False
        
        # Verify
        result = subprocess.run(
            ["powershell", "-Command", get_cmd],
            capture_output=True, text=True, timeout=10
        )
        new_current = int(result.stdout.strip())
        print(f"[+] New brightness: {new_current}%")
        
        if new_current == new_level:
            print("[+] SUCCESS: PowerShell WMIC approach works!")
            # Restore
            restore_cmd = f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {current})"
            subprocess.run(["powershell", "-Command", restore_cmd], capture_output=True, timeout=10)
            print(f"[*] Restored to {current}%")
            return True
        else:
            print("[!] Brightness did not change as expected")
            return False
            
    except subprocess.TimeoutExpired:
        print("[!] PowerShell command timed out")
        return False
    except Exception as e:
        print(f"[!] PowerShell error: {e}")
        return False


def test_powershell_cim():
    """Test PowerShell CIM approach (newer than WMI)."""
    print("\n" + "="*60)
    print("TEST 3: PowerShell CIM Approach (Modern)")
    print("="*60)
    
    try:
        # Get current brightness using CIM
        get_cmd = "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
        result = subprocess.run(
            ["powershell", "-Command", get_cmd],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode != 0 or not result.stdout.strip():
            print(f"[!] CIM get failed: {result.stderr or 'No output'}")
            return False
        
        current = int(result.stdout.strip())
        print(f"[+] Current brightness: {current}%")
        
        # Set brightness using CIM
        new_level = 50 if current != 50 else 60
        # CIM uses Invoke-CimMethod for methods
        set_cmd = f'''
$instance = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods
Invoke-CimMethod -InputObject $instance -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={new_level}}}
'''
        print(f"[*] Attempting to set brightness to {new_level}%...")
        
        result = subprocess.run(
            ["powershell", "-Command", set_cmd],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode != 0:
            print(f"[!] CIM set failed: {result.stderr}")
            # Try alternative syntax
            print("[*] Trying alternative CIM syntax...")
            set_cmd2 = f'Invoke-CimMethod -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={new_level}}}'
            result = subprocess.run(
                ["powershell", "-Command", set_cmd2],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                print(f"[!] Alternative CIM also failed: {result.stderr}")
                return False
        
        # Verify
        result = subprocess.run(
            ["powershell", "-Command", get_cmd],
            capture_output=True, text=True, timeout=10
        )
        new_current = int(result.stdout.strip())
        print(f"[+] New brightness: {new_current}%")
        
        if new_current == new_level:
            print("[+] SUCCESS: CIM approach works!")
            # Restore
            restore_cmd = f'Invoke-CimMethod -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={current}}}'
            subprocess.run(["powershell", "-Command", restore_cmd], capture_output=True, timeout=10)
            print(f"[*] Restored to {current}%")
            return True
        else:
            print("[!] Brightness did not change as expected")
            return False
            
    except Exception as e:
        print(f"[!] CIM error: {e}")
        return False


def test_screen_brightness_control():
    """Test screen-brightness-control package."""
    print("\n" + "="*60)
    print("TEST 4: screen-brightness-control Package")
    print("="*60)
    
    try:
        import screen_brightness_control as sbc
        
        # Get current brightness
        current = sbc.get_brightness()
        print(f"[+] Current brightness: {current}")
        
        if isinstance(current, list):
            current = current[0] if current else None
        
        if current is None:
            print("[!] Could not get brightness")
            return False
        
        # Set brightness
        new_level = 50 if current != 50 else 60
        print(f"[*] Attempting to set brightness to {new_level}%...")
        
        sbc.set_brightness(new_level)
        
        # Verify
        new_current = sbc.get_brightness()
        if isinstance(new_current, list):
            new_current = new_current[0] if new_current else None
        
        print(f"[+] New brightness: {new_current}%")
        
        if new_current == new_level:
            print("[+] SUCCESS: screen-brightness-control works!")
            sbc.set_brightness(current)
            print(f"[*] Restored to {current}%")
            return True
        else:
            print("[!] Brightness did not change as expected")
            return False
            
    except ImportError:
        print("[!] screen-brightness-control not installed.")
        print("    Install with: pip install screen-brightness-control")
        return False
    except Exception as e:
        print(f"[!] screen-brightness-control error: {e}")
        return False


def test_windows_display_api():
    """Test Windows Display Configuration API via ctypes."""
    print("\n" + "="*60)
    print("TEST 5: Windows Display API (SetMonitorBrightness)")
    print("="*60)
    
    try:
        import ctypes
        from ctypes import wintypes
        
        # Load required DLLs
        user32 = ctypes.windll.user32
        dxva2 = ctypes.windll.dxva2
        
        # Get physical monitors
        class PHYSICAL_MONITOR(ctypes.Structure):
            _fields_ = [
                ('hPhysicalMonitor', wintypes.HANDLE),
                ('szPhysicalMonitorDescription', wintypes.WCHAR * 128)
            ]
        
        # Get the monitor handle
        monitor = user32.MonitorFromWindow(0, 1)  # MONITOR_DEFAULTTOPRIMARY
        print(f"[*] Monitor handle: {monitor}")
        
        # Get physical monitor count
        num_monitors = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(monitor, ctypes.byref(num_monitors)):
            print("[!] GetNumberOfPhysicalMonitorsFromHMONITOR failed")
            return False
        
        print(f"[*] Number of physical monitors: {num_monitors.value}")
        
        if num_monitors.value == 0:
            print("[!] No physical monitors found")
            return False
        
        # Get physical monitor handles
        physical_monitors = (PHYSICAL_MONITOR * num_monitors.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(monitor, num_monitors.value, physical_monitors):
            print("[!] GetPhysicalMonitorsFromHMONITOR failed")
            return False
        
        hPhysicalMonitor = physical_monitors[0].hPhysicalMonitor
        description = physical_monitors[0].szPhysicalMonitorDescription
        print(f"[*] Physical monitor: {description}")
        
        # Get current brightness
        min_brightness = wintypes.DWORD()
        current_brightness = wintypes.DWORD()
        max_brightness = wintypes.DWORD()
        
        if not dxva2.GetMonitorBrightness(
            hPhysicalMonitor,
            ctypes.byref(min_brightness),
            ctypes.byref(current_brightness),
            ctypes.byref(max_brightness)
        ):
            error = ctypes.get_last_error()
            print(f"[!] GetMonitorBrightness failed (error {error})")
            print("    This API typically only works with external DDC/CI monitors")
            dxva2.DestroyPhysicalMonitors(num_monitors.value, physical_monitors)
            return False
        
        print(f"[+] Brightness range: {min_brightness.value} - {max_brightness.value}")
        print(f"[+] Current brightness: {current_brightness.value}")
        
        # Try to set brightness
        new_level = 50 if current_brightness.value != 50 else 60
        print(f"[*] Attempting to set brightness to {new_level}...")
        
        if not dxva2.SetMonitorBrightness(hPhysicalMonitor, new_level):
            print("[!] SetMonitorBrightness failed")
            dxva2.DestroyPhysicalMonitors(num_monitors.value, physical_monitors)
            return False
        
        # Verify
        dxva2.GetMonitorBrightness(
            hPhysicalMonitor,
            ctypes.byref(min_brightness),
            ctypes.byref(current_brightness),
            ctypes.byref(max_brightness)
        )
        print(f"[+] New brightness: {current_brightness.value}")
        
        # Cleanup
        dxva2.DestroyPhysicalMonitors(num_monitors.value, physical_monitors)
        
        if current_brightness.value == new_level:
            print("[+] SUCCESS: Windows Display API works!")
            return True
        else:
            print("[!] Brightness did not change as expected")
            return False
            
    except Exception as e:
        print(f"[!] Windows Display API error: {e}")
        return False


def main():
    """Run all brightness control tests."""
    print("="*60)
    print("LAPTOP BRIGHTNESS CONTROL TESTER")
    print("="*60)
    print("Testing multiple approaches to control laptop brightness...")
    print("Each test will attempt to change brightness and restore it.")
    
    results = {}
    
    # Run all tests
    results['WMI'] = test_wmi_approach()
    results['PowerShell WMIC'] = test_powershell_wmic()
    results['PowerShell CIM'] = test_powershell_cim()
    results['screen-brightness-control'] = test_screen_brightness_control()
    results['Windows Display API'] = test_windows_display_api()
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    working = []
    for name, success in results.items():
        status = "[+] WORKS" if success else "[x] FAILED"
        print(f"  {name}: {status}")
        if success:
            working.append(name)
    
    print()
    if working:
        print(f"Working methods: {', '.join(working)}")
        print("\nRecommendation: Use the first working method for WheelHouse integration.")
    else:
        print("No working methods found!")
        print("\nPossible causes:")
        print("  - Running as non-admin (some methods require elevation)")
        print("  - Laptop doesn't support WMI brightness control")
        print("  - Display driver doesn't expose brightness APIs")
        print("\nTry running as Administrator.")


if __name__ == "__main__":
    main()
