"""Tests for winrt_helpers.py - WinRT async bridging utilities.

Tests cover:
- WinRTError exception wrapping and HRESULT extraction
- run_winrt_sync polling and status handling
- await_winrt_async async polling
- safe_winrt_call error suppression
- ensure_sta COM initialization
- check_winrt_available availability detection
"""

import asyncio
import time
from enum import IntEnum
from unittest.mock import Mock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Mock WinRT AsyncStatus for testing without winsdk installed
# ---------------------------------------------------------------------------


class MockAsyncStatus(IntEnum):
    STARTED = 0
    COMPLETED = 1
    ERROR = 2
    CANCELED = 3


# ---------------------------------------------------------------------------
# WinRTError tests
# ---------------------------------------------------------------------------


class TestWinRTError:
    """Tests for WinRTError exception wrapper."""

    def test_basic_message(self):
        from utils.winrt_helpers import WinRTError

        err = WinRTError("something failed")
        assert str(err) == "something failed"
        assert err.message == "something failed"
        assert err.original is None
        assert err.hresult is None

    def test_with_original_exception(self):
        from utils.winrt_helpers import WinRTError

        original = ValueError("bad value")
        err = WinRTError("wrapped error", original=original)
        assert err.original is original
        assert str(err) == "wrapped error"

    def test_hresult_from_attribute(self):
        from utils.winrt_helpers import WinRTError

        original = Exception("winrt error")
        original.hresult = 0x80070005
        err = WinRTError("access denied", original=original)
        assert err.hresult == 0x80070005
        assert "80070005" in str(err).upper()

    def test_hresult_from_args_int(self):
        from utils.winrt_helpers import WinRTError

        original = Exception(0x80004005)
        err = WinRTError("unspecified error", original=original)
        assert err.hresult == 0x80004005

    def test_hresult_not_extracted_from_string_args(self):
        from utils.winrt_helpers import WinRTError

        original = Exception("just a string")
        err = WinRTError("msg", original=original)
        assert err.hresult is None

    def test_str_with_hresult(self):
        from utils.winrt_helpers import WinRTError

        original = Exception("winrt error")
        original.hresult = 0x80070002
        err = WinRTError("file not found", original=original)
        result = str(err)
        assert "file not found" in result
        assert "HRESULT" in result
        assert "80070002" in result.upper()

    def test_str_without_hresult(self):
        from utils.winrt_helpers import WinRTError

        err = WinRTError("plain error")
        assert str(err) == "plain error"

    def test_is_exception(self):
        from utils.winrt_helpers import WinRTError

        err = WinRTError("test")
        assert isinstance(err, Exception)

    def test_no_original_no_hresult(self):
        from utils.winrt_helpers import WinRTError

        err = WinRTError("test", original=None)
        assert err.hresult is None
        assert err.original is None


# ---------------------------------------------------------------------------
# run_winrt_sync tests
# ---------------------------------------------------------------------------


class TestRunWinrtSync:
    """Tests for synchronous WinRT operation polling."""

    def _make_operation(self, statuses, result=None, error_code=None):
        """Create a mock WinRT operation that transitions through statuses."""
        op = Mock()
        status_iter = iter(statuses)
        op.status = next(status_iter)

        def advance_status():
            try:
                op.status = next(status_iter)
            except StopIteration:
                pass

        # Each time sleep is called, advance the status
        op._advance = advance_status
        op.get_results = Mock(return_value=result)
        op.cancel = Mock()
        if error_code is not None:
            op.error_code = error_code
        return op

    def test_immediate_completion(self):
        from utils.winrt_helpers import run_winrt_sync

        op = Mock()
        op.status = MockAsyncStatus.COMPLETED
        op.get_results = Mock(return_value="success")

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            result = run_winrt_sync(op)

        assert result == "success"
        op.get_results.assert_called_once()

    def test_polls_until_completed(self):
        from utils.winrt_helpers import run_winrt_sync

        statuses = [MockAsyncStatus.STARTED, MockAsyncStatus.STARTED, MockAsyncStatus.COMPLETED]
        call_count = 0
        op = Mock()
        op.get_results = Mock(return_value=42)

        def get_status():
            nonlocal call_count
            idx = min(call_count, len(statuses) - 1)
            return statuses[idx]

        type(op).status = property(lambda self: get_status())

        original_sleep = time.sleep

        def counting_sleep(duration):
            nonlocal call_count
            call_count += 1

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with patch("utils.winrt_helpers.time.sleep", side_effect=counting_sleep):
                result = run_winrt_sync(op, poll_interval=0.001)

        assert result == 42
        assert call_count >= 2

    def test_timeout_cancels_operation(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.STARTED
        op.cancel = Mock()

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with patch("utils.winrt_helpers.time.sleep"):
                with pytest.raises(WinRTError, match="timed out"):
                    run_winrt_sync(op, timeout=0.0, poll_interval=0.001)

        op.cancel.assert_called_once()

    def test_canceled_operation_raises(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.CANCELED

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="canceled"):
                run_winrt_sync(op)

    def test_error_status_with_error_code(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.ERROR
        op.error_code = 0x80004005

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="error code"):
                run_winrt_sync(op)

    def test_error_status_without_error_code(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.ERROR
        # Remove error_code attribute
        del op.error_code

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="operation failed"):
                run_winrt_sync(op)

    def test_unexpected_status_raises(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()
        op.status = 99  # Unknown status

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="unexpected status"):
                run_winrt_sync(op)

    def test_winsdk_not_available_raises(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()

        with patch("utils.winrt_helpers._get_async_status", return_value=None):
            with pytest.raises(WinRTError, match="winsdk not available"):
                run_winrt_sync(op)

    def test_unexpected_exception_wrapped(self):
        from utils.winrt_helpers import run_winrt_sync, WinRTError

        op = Mock()
        # Property that raises on access
        type(op).status = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="boom") as exc_info:
                run_winrt_sync(op)
            assert exc_info.value.original is not None


# ---------------------------------------------------------------------------
# await_winrt_async tests
# ---------------------------------------------------------------------------


class TestAwaitWinrtAsync:
    """Tests for async WinRT operation polling."""

    @pytest.mark.asyncio
    async def test_immediate_completion(self):
        from utils.winrt_helpers import await_winrt_async

        op = Mock()
        op.status = MockAsyncStatus.COMPLETED
        op.get_results = Mock(return_value="async_result")

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            result = await await_winrt_async(op)

        assert result == "async_result"

    @pytest.mark.asyncio
    async def test_polls_until_completed(self):
        from utils.winrt_helpers import await_winrt_async

        call_count = 0
        statuses = [MockAsyncStatus.STARTED, MockAsyncStatus.STARTED, MockAsyncStatus.COMPLETED]
        op = Mock()
        op.get_results = Mock(return_value="done")

        def get_status():
            nonlocal call_count
            idx = min(call_count, len(statuses) - 1)
            return statuses[idx]

        type(op).status = property(lambda self: get_status())

        original_sleep = asyncio.sleep

        async def counting_sleep(duration):
            nonlocal call_count
            call_count += 1

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with patch("utils.winrt_helpers.asyncio.sleep", side_effect=counting_sleep):
                result = await await_winrt_async(op, poll_interval=0.001)

        assert result == "done"
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_timeout_cancels(self):
        from utils.winrt_helpers import await_winrt_async, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.STARTED
        op.cancel = Mock()

        async def fast_sleep(duration):
            pass

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with patch("utils.winrt_helpers.asyncio.sleep", side_effect=fast_sleep):
                with pytest.raises(WinRTError, match="timed out"):
                    await await_winrt_async(op, timeout=0.0, poll_interval=0.001)

        op.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_canceled_operation(self):
        from utils.winrt_helpers import await_winrt_async, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.CANCELED

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="canceled"):
                await await_winrt_async(op)

    @pytest.mark.asyncio
    async def test_error_status(self):
        from utils.winrt_helpers import await_winrt_async, WinRTError

        op = Mock()
        op.status = MockAsyncStatus.ERROR
        op.error_code = 42

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="error code"):
                await await_winrt_async(op)

    @pytest.mark.asyncio
    async def test_winsdk_not_available(self):
        from utils.winrt_helpers import await_winrt_async, WinRTError

        with patch("utils.winrt_helpers._get_async_status", return_value=None):
            with pytest.raises(WinRTError, match="winsdk not available"):
                await await_winrt_async(Mock())

    @pytest.mark.asyncio
    async def test_unexpected_exception_wrapped(self):
        from utils.winrt_helpers import await_winrt_async, WinRTError

        op = Mock()
        type(op).status = property(lambda self: (_ for _ in ()).throw(TypeError("bad type")))

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            with pytest.raises(WinRTError, match="bad type"):
                await await_winrt_async(op)


# ---------------------------------------------------------------------------
# safe_winrt_call tests
# ---------------------------------------------------------------------------


class TestSafeWinrtCall:
    """Tests for safe_winrt_call error-suppressing wrapper."""

    @pytest.mark.asyncio
    async def test_returns_result_on_success(self):
        from utils.winrt_helpers import safe_winrt_call

        op = Mock()
        op.status = MockAsyncStatus.COMPLETED
        op.get_results = Mock(return_value="data")

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            result = await safe_winrt_call(op, "test op")

        assert result == "data"

    @pytest.mark.asyncio
    async def test_returns_none_on_winrt_error(self):
        from utils.winrt_helpers import safe_winrt_call

        op = Mock()
        op.status = MockAsyncStatus.CANCELED

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            result = await safe_winrt_call(op, "test op")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_error(self):
        from utils.winrt_helpers import safe_winrt_call

        op = Mock()
        type(op).status = property(lambda self: (_ for _ in ()).throw(RuntimeError("crash")))

        with patch("utils.winrt_helpers._get_async_status", return_value=MockAsyncStatus):
            result = await safe_winrt_call(op, "test op")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_winsdk_unavailable(self):
        from utils.winrt_helpers import safe_winrt_call

        with patch("utils.winrt_helpers._get_async_status", return_value=None):
            result = await safe_winrt_call(Mock(), "test op")

        assert result is None


# ---------------------------------------------------------------------------
# ensure_sta tests
# ---------------------------------------------------------------------------


class TestEnsureSta:
    """Tests for COM STA initialization helper."""

    def test_initializes_com_successfully(self):
        from utils.winrt_helpers import ensure_sta

        mock_pythoncom = Mock()
        mock_pythoncom.CoInitialize = Mock()
        mock_pythoncom.com_error = type("com_error", (Exception,), {})

        with patch.dict("sys.modules", {"pythoncom": mock_pythoncom}):
            result = ensure_sta()

        assert result is True
        mock_pythoncom.CoInitialize.assert_called_once()

    def test_already_initialized_returns_false(self):
        from utils.winrt_helpers import ensure_sta

        mock_pythoncom = Mock()
        com_error = type("com_error", (Exception,), {})
        mock_pythoncom.com_error = com_error
        mock_pythoncom.CoInitialize = Mock(side_effect=com_error())

        with patch.dict("sys.modules", {"pythoncom": mock_pythoncom}):
            result = ensure_sta()

        assert result is False

    def test_pythoncom_not_available(self):
        from utils.winrt_helpers import ensure_sta

        with patch("builtins.__import__", side_effect=ImportError("no pythoncom")):
            result = ensure_sta()

        assert result is False


# ---------------------------------------------------------------------------
# check_winrt_available tests
# ---------------------------------------------------------------------------


class TestCheckWinrtAvailable:
    """Tests for WinRT availability detection."""

    def test_available_when_winsdk_imports(self):
        from utils.winrt_helpers import check_winrt_available

        mock_module = Mock()
        with patch.dict("sys.modules", {"winsdk": Mock(), "winsdk.windows": Mock(), "winsdk.windows.foundation": mock_module}):
            result = check_winrt_available()
        assert result is True

    def test_unavailable_when_import_fails(self):
        from utils.winrt_helpers import check_winrt_available

        with patch("builtins.__import__", side_effect=ImportError("no winsdk")):
            result = check_winrt_available()
        assert result is False

    def test_unavailable_on_unexpected_error(self):
        from utils.winrt_helpers import check_winrt_available

        with patch("builtins.__import__", side_effect=RuntimeError("broken")):
            result = check_winrt_available()
        assert result is False


# ---------------------------------------------------------------------------
# _get_async_status tests
# ---------------------------------------------------------------------------


class TestGetAsyncStatus:
    """Tests for AsyncStatus import helper."""

    def test_returns_none_when_unavailable(self):
        from utils.winrt_helpers import _get_async_status

        with patch("builtins.__import__", side_effect=ImportError("no winsdk")):
            result = _get_async_status()
        assert result is None
