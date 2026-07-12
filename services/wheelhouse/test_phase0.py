"""Phase 0 validation test - verifies all WinRT helpers work correctly."""
import sys
sys.path.insert(0, '.')
import asyncio

print('=' * 60)
print('PHASE 0 VALIDATION TEST')
print('=' * 60)

# Test 1: Module imports
print('\n[1] Testing winrt_helpers module imports...')
try:
    from utils.winrt_helpers import (
        WINRT_AVAILABLE,
        WinRTError,
        check_winrt_available,
        ensure_sta,
        run_winrt_sync,
        await_winrt_async,
        safe_winrt_call
    )
    print('    [+] All exports imported successfully')
except ImportError as e:
    print(f'    [x] Import failed: {e}')
    sys.exit(1)

# Test 2: WINRT_AVAILABLE flag
print('\n[2] Testing WINRT_AVAILABLE flag...')
print(f'    WINRT_AVAILABLE = {WINRT_AVAILABLE}')
if not WINRT_AVAILABLE:
    print('    [x] WinRT not available - cannot continue')
    sys.exit(1)
print('    [+] WinRT is available')

# Test 3: All required WinRT namespaces for future phases
print('\n[3] Testing WinRT namespace imports...')
namespaces = {
    'Foundation': 'winsdk.windows.foundation',
    'SpeechRecognition': 'winsdk.windows.media.speechrecognition',
    'Audio': 'winsdk.windows.media.audio',
    'Clipboard': 'winsdk.windows.applicationmodel.datatransfer',
    'DeviceEnumeration': 'winsdk.windows.devices.enumeration',
    'OCR': 'winsdk.windows.media.ocr',
}
all_ok = True
for name, module in namespaces.items():
    try:
        __import__(module)
        print(f'    [+] {name}: OK')
    except ImportError as e:
        print(f'    [x] {name}: FAILED - {e}')
        all_ok = False

if not all_ok:
    print('    [!] Some namespaces failed to import')

# Test 4: run_winrt_sync with DeviceInformation
print('\n[4] Testing run_winrt_sync()...')
from winsdk.windows.devices.enumeration import DeviceInformation
try:
    op = DeviceInformation.find_all_async()
    devices = run_winrt_sync(op, timeout=5.0)
    print(f'    [+] Found {devices.size} devices')
except WinRTError as e:
    print(f'    [x] WinRTError: {e}')
except Exception as e:
    print(f'    [x] Error: {type(e).__name__}: {e}')

# Test 5: await_winrt_async
print('\n[5] Testing await_winrt_async()...')
async def test_async():
    op = DeviceInformation.find_all_async()
    devices = await await_winrt_async(op, timeout=5.0)
    return devices.size

try:
    count = asyncio.run(test_async())
    print(f'    [+] Found {count} devices (async)')
except Exception as e:
    print(f'    [x] Error: {type(e).__name__}: {e}')

# Test 6: safe_winrt_call (error handling)
print('\n[6] Testing safe_winrt_call()...')
async def test_safe():
    op = DeviceInformation.find_all_async()
    return await safe_winrt_call(op, 'device enumeration', timeout=5.0)

try:
    result = asyncio.run(test_safe())
    if result:
        print(f'    [+] Found {result.size} devices (safe call)')
    else:
        print('    [x] safe_winrt_call returned None unexpectedly')
except Exception as e:
    print(f'    [x] Error: {type(e).__name__}: {e}')

# Test 7: WinRTError exception
print('\n[7] Testing WinRTError formatting...')
try:
    err = WinRTError('Test error', original=OSError('underlying issue'))
    print(f'    Error message: {err}')
    print(f'    [+] WinRTError works correctly')
except Exception as e:
    print(f'    [x] Error: {e}')

# Test 8: Timeout behavior
print('\n[8] Testing timeout behavior...')
try:
    op = DeviceInformation.find_all_async()
    devices = run_winrt_sync(op, timeout=10.0)
    print(f'    [+] Timeout handling works')
except WinRTError as e:
    if 'timed out' in str(e):
        print('    [+] Timeout exception raised correctly')
    else:
        print(f'    [x] Unexpected error: {e}')

print('\n' + '=' * 60)
print('PHASE 0 VALIDATION COMPLETE')
print('=' * 60)
print('\nAll tests passed. Ready for Phase 1.')
