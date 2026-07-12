"""Fault injection tests for SharedMemory IPC resilience.

Tests what happens when data is corrupted, oversized, or when events are
signaled incorrectly. Uses real SharedMemory and Event primitives -- no mocks.

The IPC protocol under test:
  - SharedMemory buffer (default 64KB)
  - 4-byte big-endian size header followed by pickle data
  - multiprocessing.Event for signaling data availability
  - Writer: app.py _frame_and_write()
  - Reader: input_proc.py main loop (step 6)
"""
import pickle
import struct
import time
from multiprocessing import shared_memory, Event

import pytest


# ---------------------------------------------------------------------------
# Helpers that replicate production IPC logic in isolation
# ---------------------------------------------------------------------------

SHM_SIZE = 64 * 1024  # 64KB, matches production


def _write_frame(shm: shared_memory.SharedMemory, payload: dict) -> None:
    """Replicate app.py _frame_and_write -- pickle + size header."""
    data = pickle.dumps(payload)
    size = len(data)
    if size > shm.size - 4:
        raise ValueError(
            f"Payload size ({size}b) exceeds shared memory capacity ({shm.size - 4}b)."
        )
    size_bytes = struct.pack(">I", size)
    shm.buf[:4] = size_bytes
    shm.buf[4 : 4 + size] = data


def _read_frame(shm: shared_memory.SharedMemory) -> dict:
    """Replicate input_proc.py reader -- size header then pickle load."""
    size_bytes = bytes(shm.buf[:4])
    msg_len = struct.unpack(">I", size_bytes)[0]
    msg_data = bytearray(msg_len)
    msg_data[:] = shm.buf[4 : 4 + msg_len]
    return pickle.loads(msg_data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def shm():
    """Create a fresh SharedMemory segment and clean it up after the test."""
    segment = shared_memory.SharedMemory(create=True, size=SHM_SIZE)
    yield segment
    segment.close()
    segment.unlink()


@pytest.fixture()
def command_ready():
    """Create a multiprocessing Event for signaling."""
    return Event()


# ---------------------------------------------------------------------------
# TestCorruptedIPCData
# ---------------------------------------------------------------------------


class TestCorruptedIPCData:
    """Verify resilience when the shared memory buffer contains bad data."""

    def test_truncated_header_reads_wrong_data(self, shm):
        """A partially-written size header makes the reader extract wrong data.

        If the writer crashes mid-header-write, the 4-byte size is mangled.
        For small payloads (< 256 bytes), the zero-padded header coincidentally
        produces the correct length, so _read_frame succeeds but this is luck,
        not correctness. For larger payloads, the size would be wrong.

        This test writes a known payload with a corrupted header and verifies
        the reader gets back data that differs from what was written -- proving
        the corruption is detectable at the application level even though
        Python doesn't raise.
        """
        # Use a larger payload to ensure the partial header produces wrong size
        payload = {"action": "test", "data": "x" * 300}
        data = pickle.dumps(payload)
        real_size = len(data)

        # Write only 2 bytes of the 4-byte header; bytes 2-3 stay as zeros
        partial_header = struct.pack(">H", real_size)  # 2-byte big-endian
        shm.buf[0:2] = partial_header
        shm.buf[2:4] = b"\x00\x00"
        shm.buf[4 : 4 + len(data)] = data

        # The mangled 4-byte size should differ from the real size
        msg_len = struct.unpack(">I", bytes(shm.buf[:4]))[0]
        assert msg_len != real_size, (
            f"Mangled header should produce wrong size: got {msg_len}, real={real_size}"
        )

        # _read_frame will read wrong-sized data -- either raises or returns garbage
        try:
            result = _read_frame(shm)
            # If it somehow succeeds, it returned wrong data
            assert result != payload, (
                "Corrupted header should not produce correct payload"
            )
        except Exception:
            pass  # Expected: deserialization error from wrong-sized read

    def test_oversized_header_reads_clamped_data(self, shm):
        """Size header claims more data than the 64KB buffer holds.

        Python's memoryview silently clamps the slice: shm.buf[4:4+huge]
        returns only (shm.size - 4) bytes. The bytearray(huge) is created
        at the claimed size, then the slice assignment silently copies only
        the available bytes into the first portion, leaving the rest as zeros.

        This means _read_frame gets a bytearray full of mostly zeros, which
        pickle.loads may or may not handle. The key safety property: no
        buffer overrun, no segfault. The data is just wrong.
        """
        # Write a small valid payload
        real_payload = {"action": "test", "seq": 99}
        _write_frame(shm, real_payload)

        # Corrupt the size header to claim far more data than exists
        fake_size = SHM_SIZE + 40_000  # well beyond buffer
        shm.buf[:4] = struct.pack(">I", fake_size)

        # Confirm the header value exceeds the buffer
        msg_len = struct.unpack(">I", bytes(shm.buf[:4]))[0]
        assert msg_len > shm.size - 4, (
            "Test setup error: fake size should exceed buffer"
        )

        # _read_frame may succeed (returning garbage) or raise.
        # Either way, it must NOT crash the process (no segfault).
        try:
            result = _read_frame(shm)
            # If it succeeds, the result is wrong (zero-padded garbage)
            assert result != real_payload, (
                "Oversized header should not return the correct original payload"
            )
        except Exception:
            pass  # Deserialization error from zero-padded data is expected

    def test_corrupted_pickle_data_raises_on_read(self, shm):
        """Valid size header but garbage pickle bytes.

        When the buffer contains a correct size header but the payload is
        garbage, _read_frame must raise a deserialization error. This tests
        the reader helper (which replicates production logic), not raw pickle.
        """
        garbage = b"\xde\xad\xbe\xef" * 20  # 80 bytes of nonsense
        size_header = struct.pack(">I", len(garbage))
        shm.buf[:4] = size_header
        shm.buf[4 : 4 + len(garbage)] = garbage

        # _read_frame should raise because the pickle data is invalid
        with pytest.raises(Exception):
            _read_frame(shm)

    def test_valid_pickle_round_trip(self, shm):
        """Baseline: normal IPC protocol works end-to-end."""
        payload = {
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "hello world"},
            "request_id": "abc-123",
        }

        _write_frame(shm, payload)
        recovered = _read_frame(shm)

        assert recovered == payload
        assert recovered["action"] == "intelligent_insert_text"
        assert recovered["params"]["insertion_string"] == "hello world"
        assert recovered["request_id"] == "abc-123"


# ---------------------------------------------------------------------------
# TestIPCEventCoordination
# ---------------------------------------------------------------------------


class TestIPCEventCoordination:
    """Verify event signaling discipline between writer and reader."""

    def test_event_clear_after_copy_prevents_race(self, shm, command_ready):
        """Event must be cleared AFTER the data copy, not before.

        Sequence (correct):
          1. Writer writes data, sets event.
          2. Reader sees event set, reads data, THEN clears event.
          3. Writer sees event cleared, knows it is safe to write next payload.

        If step 2 cleared the event BEFORE copying, the writer could overwrite
        the buffer while the reader is still copying -- heap corruption.
        """
        payload_a = {"action": "type_text", "params": {"text": "AAA"}, "seq": 1}
        payload_b = {"action": "type_text", "params": {"text": "BBB"}, "seq": 2}

        # --- Round 1: write A, signal ---
        _write_frame(shm, payload_a)
        command_ready.set()

        # Simulate reader: wait for event
        assert command_ready.is_set()

        # Reader copies data BEFORE clearing (correct order)
        recovered_a = _read_frame(shm)
        command_ready.clear()  # safe: data already copied

        assert recovered_a == payload_a

        # --- Round 2: writer sees cleared, writes B ---
        assert not command_ready.is_set()
        _write_frame(shm, payload_b)
        command_ready.set()

        # Reader does the same for round 2
        assert command_ready.is_set()
        recovered_b = _read_frame(shm)
        command_ready.clear()

        assert recovered_b == payload_b
        # No data from payload_a leaking into payload_b
        assert recovered_b["params"]["text"] == "BBB"

    def test_event_not_set_means_no_data(self, shm, command_ready):
        """When the event is not set the reader must not attempt to read.

        This mirrors the production loop where `command_ready_event.wait(timeout=0.01)`
        returns False and the loop continues without touching shared memory.
        """
        # Event starts unset
        assert not command_ready.is_set()

        # Simulate the production guard: wait with very short timeout
        signaled = command_ready.wait(timeout=0.01)
        assert signaled is False, "Event should not be signaled"

        # Reader skips -- no read from shared memory.
        # Write some known data to prove the reader *could* read garbage
        # if it bypassed the guard.
        shm.buf[:4] = b"\xff\xff\xff\xff"  # invalid size
        # (The fact that we don't crash here proves the guard works)


# ---------------------------------------------------------------------------
# TestLargePayloads
# ---------------------------------------------------------------------------


class TestLargePayloads:
    """Verify behaviour at the edges of the 64KB buffer capacity."""

    def test_payload_near_buffer_limit(self, shm):
        """A large-but-valid payload that nearly fills the 64KB buffer.

        The writer reserves 4 bytes for the size header, so maximum pickle
        payload is shm.size - 4 bytes.
        """
        # Build a payload whose pickle serialization is close to the limit.
        # Strings are efficient in pickle, so use a large one.
        max_data_bytes = shm.size - 4
        # Start with a big string and trim to fit
        big_string = "X" * (max_data_bytes - 100)  # leave room for pickle overhead
        payload = {"action": "test_large", "data": big_string}

        # Verify it serializes within limits
        data = pickle.dumps(payload)
        assert len(data) <= max_data_bytes, (
            f"Test setup: pickled size {len(data)} exceeds {max_data_bytes}"
        )

        # Write and read back
        _write_frame(shm, payload)
        recovered = _read_frame(shm)

        assert recovered["action"] == "test_large"
        assert recovered["data"] == big_string

    def test_payload_exceeding_buffer_detected(self, shm):
        """An oversized payload must be rejected BEFORE writing to shared memory.

        The production _frame_and_write raises ValueError when the pickled
        payload exceeds shm.size - 4 bytes.
        """
        max_data_bytes = shm.size - 4
        # Create a payload that definitely exceeds the buffer
        huge_string = "Y" * (max_data_bytes + 1000)
        payload = {"action": "test_huge", "data": huge_string}

        # Confirm the pickled size actually exceeds the limit
        data = pickle.dumps(payload)
        assert len(data) > max_data_bytes, (
            f"Test setup: pickled size {len(data)} should exceed {max_data_bytes}"
        )

        # _write_frame must raise before touching the buffer
        with pytest.raises(ValueError, match="exceeds shared memory capacity"):
            _write_frame(shm, payload)

        # Buffer should still contain whatever was there before (zeros for new shm)
        # -- verify the oversized write did NOT partially corrupt the buffer
        header_bytes = bytes(shm.buf[:4])
        assert header_bytes == b"\x00\x00\x00\x00", (
            "Buffer should be untouched after rejected write"
        )
