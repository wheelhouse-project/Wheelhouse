"""Windows Runtime (WinRT) integration utilities for WheelHouse.

This module provides helper utilities for integrating WinRT APIs with WheelHouse's
asyncio-based architecture. WinRT is used throughout WheelHouse for native Windows
functionality including speech recognition, audio capture, clipboard access, and
device enumeration.

GLOSSARY
--------
- **WinRT** - Windows Runtime: Microsoft's native API for Windows 10+ applications.
  Provides access to system capabilities not available via standard Python libraries.
- **winsdk** - Python package providing WinRT bindings (v1.0.0b10).
- **IAsyncOperation** - WinRT's async pattern. Returns immediately, must poll status
  or use completed callback to get results.
- **AsyncStatus** - Enum indicating operation state: STARTED, COMPLETED, ERROR, CANCELED.
- **STA** - Single-Threaded Apartment: COM threading model required by some WinRT APIs.
- **COM** - Component Object Model: Windows inter-process communication mechanism
  underlying WinRT.

OVERVIEW
--------
WheelHouse uses WinRT for several capabilities:

1. **Speech Recognition** (Phase 3) - Local STT via Windows.Media.SpeechRecognition
2. **Audio Capture** (Phase 1) - Microphone input via Windows.Media.Audio
3. **Clipboard** (Phase 4) - History access via Windows.ApplicationModel.DataTransfer
4. **Device Enumeration** (Phase 2) - HID/Bluetooth via Windows.Devices.Enumeration
5. **Screen Capture/OCR** (Phase 6) - Via Windows.Graphics.Capture and Windows.Media.Ocr

KEY INSIGHTS
------------
1. **winsdk async is NOT Python async** - IAsyncOperation objects cannot be directly
   awaited. Use `await_winrt_async()` to bridge to asyncio, or `run_winrt_sync()` 
   for synchronous contexts.

2. **Status polling pattern** - WinRT async operations return immediately. You must
   either poll `op.status` until COMPLETED, or register a `completed` callback.

3. **Process isolation** - WinRT COM objects cannot be pickled or shared between
   processes. Each WheelHouse process (STT, Logic, Input, GUI) must create its
   own WinRT instances. This is not a limitation - it's the expected pattern.

4. **COM initialization** - Most WinRT APIs work without explicit COM initialization.
   The `ensure_sta()` helper is provided for edge cases where explicit 
   initialization is needed (e.g., certain UI-related APIs).

5. **Error translation** - WinRT exceptions are translated to Python exceptions
   by winsdk, but error messages can be cryptic. The `WinRTError` wrapper provides
   cleaner error reporting.

Example Usage
-------------
```python
from services.wheelhouse.utils.winrt_helpers import run_winrt_sync, await_winrt_async, WinRTError

# Synchronous context
try:
    result = run_winrt_sync(some_winrt_async_operation(), timeout=5.0)
except WinRTError as e:
    logger.error(f"WinRT operation failed: {e}")

# Async context
async def my_async_function():
    result = await await_winrt_async(some_winrt_async_operation())
```
"""

import asyncio
import logging
import time
from typing import TypeVar, Any, Optional, Union

logger = logging.getLogger(__name__)

T = TypeVar('T')


class WinRTError(Exception):
    """Exception wrapper for WinRT errors with improved error messages.
    
    WinRT exceptions from winsdk can have cryptic error codes. This wrapper
    provides a consistent exception type for WinRT-related errors with
    human-readable messages.
    
    Attributes:
        message: Human-readable error description.
        original: The original exception from winsdk, if any.
        hresult: The Windows HRESULT error code, if available.
    """
    
    def __init__(self, message: str, original: Optional[Exception] = None):
        """Initialize WinRTError.
        
        Args:
            message: Human-readable error description.
            original: The original exception that triggered this error.
        """
        super().__init__(message)
        self.message = message
        self.original = original
        self.hresult: Optional[int] = None
        
        # Extract HRESULT if available from original exception
        if original is not None:
            # winsdk exceptions often have hresult attribute
            if hasattr(original, 'hresult'):
                self.hresult = getattr(original, 'hresult')
            # Or it might be in the args
            elif original.args and isinstance(original.args[0], int):
                self.hresult = original.args[0]
    
    def __str__(self) -> str:
        """Return formatted error message with HRESULT if available."""
        if self.hresult is not None:
            return f"{self.message} (HRESULT: 0x{self.hresult:08X})"
        return self.message


def ensure_sta() -> bool:
    """Ensure COM is initialized in Single-Threaded Apartment mode.
    
    Some WinRT APIs (particularly UI-related ones) require STA initialization.
    This function initializes COM in STA mode if not already initialized.
    
    Note: Most WinRT APIs used via asyncio do not require explicit COM
    initialization - winsdk handles this automatically. Use this function
    only when you encounter COM-related errors with specific APIs.
    
    Returns:
        True if COM was initialized by this call, False if already initialized.
    
    Example:
        ```python
        if ensure_sta():
            logger.debug("COM initialized in STA mode")
        ```
    """
    try:
        import pythoncom
        try:
            pythoncom.CoInitialize()
            logger.debug("COM initialized in STA mode")
            return True
        except pythoncom.com_error:
            # Already initialized (not an error)
            return False
    except ImportError:
        # pythoncom not available, likely not needed
        # winsdk handles COM for most cases
        logger.debug("pythoncom not available, skipping explicit COM init")
        return False


def _get_async_status():
    """Import AsyncStatus enum from winsdk.
    
    Returns:
        AsyncStatus enum, or None if winsdk not available.
    """
    try:
        from winsdk.windows.foundation import AsyncStatus
        return AsyncStatus
    except ImportError:
        return None


def run_winrt_sync(
    operation: Any,
    timeout: Optional[float] = None,
    poll_interval: float = 0.01
) -> T:
    """Run a WinRT async operation synchronously.
    
    This function polls the IAsyncOperation status until completion or timeout.
    Use this when calling WinRT from synchronous code.
    
    Args:
        operation: The WinRT IAsyncOperation object (from calling *_async() method).
        timeout: Optional timeout in seconds. None means wait indefinitely.
        poll_interval: How often to check status, in seconds. Default 10ms.
    
    Returns:
        The result of the async operation.
    
    Raises:
        WinRTError: If the operation fails, is canceled, or times out.
    
    Example:
        ```python
        from winsdk.windows.devices.enumeration import DeviceInformation
        
        # Get the async operation
        op = DeviceInformation.find_all_async()
        
        # Run synchronously with timeout
        devices = run_winrt_sync(op, timeout=5.0)
        print(f"Found {devices.size} devices")
        ```
    """
    AsyncStatus = _get_async_status()
    if AsyncStatus is None:
        raise WinRTError("winsdk not available")
    
    start_time = time.monotonic()
    
    try:
        # Poll until complete
        while operation.status == AsyncStatus.STARTED:
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    operation.cancel()
                    raise WinRTError(f"WinRT operation timed out after {timeout} seconds")
            time.sleep(poll_interval)
        
        # Check final status
        if operation.status == AsyncStatus.COMPLETED:
            return operation.get_results()
        elif operation.status == AsyncStatus.CANCELED:
            raise WinRTError("WinRT operation was canceled")
        elif operation.status == AsyncStatus.ERROR:
            # Try to get error info
            try:
                error_code = operation.error_code
                raise WinRTError(f"WinRT operation failed with error code: {error_code}")
            except AttributeError:
                raise WinRTError("WinRT operation failed")
        else:
            raise WinRTError(f"WinRT operation ended with unexpected status: {operation.status}")
            
    except WinRTError:
        raise
    except Exception as e:
        raise WinRTError(f"WinRT operation failed: {e}", original=e) from e


async def await_winrt_async(
    operation: Any,
    timeout: Optional[float] = None,
    poll_interval: float = 0.01
) -> T:
    """Await a WinRT async operation in an asyncio context.
    
    This function asynchronously polls the IAsyncOperation status, yielding
    to the event loop between polls. Use this in async code to avoid blocking.
    
    Args:
        operation: The WinRT IAsyncOperation object (from calling *_async() method).
        timeout: Optional timeout in seconds. None means wait indefinitely.
        poll_interval: How often to check status, in seconds. Default 10ms.
    
    Returns:
        The result of the async operation.
    
    Raises:
        WinRTError: If the operation fails, is canceled, or times out.
    
    Example:
        ```python
        from winsdk.windows.devices.enumeration import DeviceInformation
        
        async def list_devices():
            op = DeviceInformation.find_all_async()
            devices = await await_winrt_async(op, timeout=5.0)
            return devices
        ```
    """
    AsyncStatus = _get_async_status()
    if AsyncStatus is None:
        raise WinRTError("winsdk not available")
    
    start_time = time.monotonic()
    
    try:
        # Async poll until complete
        while operation.status == AsyncStatus.STARTED:
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    operation.cancel()
                    raise WinRTError(f"WinRT operation timed out after {timeout} seconds")
            await asyncio.sleep(poll_interval)
        
        # Check final status
        if operation.status == AsyncStatus.COMPLETED:
            return operation.get_results()
        elif operation.status == AsyncStatus.CANCELED:
            raise WinRTError("WinRT operation was canceled")
        elif operation.status == AsyncStatus.ERROR:
            try:
                error_code = operation.error_code
                raise WinRTError(f"WinRT operation failed with error code: {error_code}")
            except AttributeError:
                raise WinRTError("WinRT operation failed")
        else:
            raise WinRTError(f"WinRT operation ended with unexpected status: {operation.status}")
            
    except WinRTError:
        raise
    except Exception as e:
        raise WinRTError(f"WinRT operation failed: {e}", original=e) from e


async def safe_winrt_call(
    operation: Any,
    operation_name: str = "WinRT operation",
    timeout: Optional[float] = None
) -> Optional[T]:
    """Execute a WinRT async operation with error handling and logging.
    
    This is a convenience wrapper that catches WinRT exceptions and logs them,
    returning None on failure instead of raising. Use this for non-critical
    operations where failure should be logged but not propagate.
    
    Args:
        operation: The WinRT IAsyncOperation to execute.
        operation_name: Description of the operation for logging.
        timeout: Optional timeout in seconds.
    
    Returns:
        The result of the operation, or None if it failed.
    
    Example:
        ```python
        # Non-critical operation - log failure but continue
        from winsdk.windows.applicationmodel.datatransfer import Clipboard
        
        history = await safe_winrt_call(
            Clipboard.get_history_items_async(),
            "clipboard history retrieval",
            timeout=2.0
        )
        if history is None:
            # Fall back to non-history clipboard access
            pass
        ```
    """
    try:
        return await await_winrt_async(operation, timeout=timeout)
    except WinRTError as e:
        logger.warning(f"{operation_name} failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"{operation_name} failed unexpectedly: {e}")
        return None


def check_winrt_available() -> bool:
    """Check if WinRT/winsdk is available and functional.
    
    Use this to gracefully handle systems where WinRT is not available
    (e.g., older Windows versions, non-Windows platforms during development).
    
    Returns:
        True if winsdk is installed and basic imports work.
    
    Example:
        ```python
        if not check_winrt_available():
            logger.warning("WinRT not available, using fallback")
            return FallbackImplementation()
        return WinRTImplementation()
        ```
    """
    try:
        # Try importing core winsdk components
        from winsdk.windows.foundation import AsyncStatus  # noqa: F401
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.warning(f"WinRT availability check failed: {e}")
        return False


# Module-level availability flag (cached at import time)
WINRT_AVAILABLE = check_winrt_available()
