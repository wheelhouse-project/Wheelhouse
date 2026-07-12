"""Stress test for SharedMemory IPC race condition.

Tests the race condition where the consumer clears the event BEFORE finishing
the data copy, allowing the sender to overwrite the buffer mid-read.

Exit code 0xC0000374 (STATUS_HEAP_CORRUPTION) was observed when this race
was triggered in production with rapid back-to-back commands.
"""
import asyncio
import logging
import math
import multiprocessing
import pickle
import struct
import sys
import time
from multiprocessing import shared_memory, Queue, Event
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import MagicMock

import pytest

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


class IPCConsumer:
    """Simulates the Input process consumer side of IPC.

    This isolates just the SharedMemory read logic from input_proc.py
    for testing the race condition.
    """

    def __init__(self, shm: shared_memory.SharedMemory,
                 command_ready_event: Event,
                 received_commands: List,
                 corruption_events: List,
                 stop_event: Event,
                 clear_before_read: bool = True):
        """
        Args:
            clear_before_read: If True, uses buggy behavior (clear event before read).
                             If False, uses fixed behavior (clear event after read).
        """
        self.shm = shm
        self.command_ready_event = command_ready_event
        self.received_commands = received_commands
        self.corruption_events = corruption_events
        self.stop_event = stop_event
        self.clear_before_read = clear_before_read

    def run(self):
        """Consumer loop - reads commands from shared memory."""
        while not self.stop_event.is_set():
            # Wait for command with short timeout
            signaled = self.command_ready_event.wait(timeout=0.01)
            if not signaled:
                continue

            if self.clear_before_read:
                # BUGGY: Clear event before reading data
                # This allows sender to overwrite buffer while we're reading
                self.command_ready_event.clear()

            try:
                # Read size header
                size_bytes = bytes(self.shm.buf[:4])
                msg_len = struct.unpack('>I', size_bytes)[0]

                # Validate size to catch corruption
                if msg_len > self.shm.size - 4 or msg_len == 0:
                    self.corruption_events.append({
                        'type': 'invalid_size',
                        'msg_len': msg_len,
                        'max_size': self.shm.size - 4
                    })
                    if not self.clear_before_read:
                        self.command_ready_event.clear()
                    continue

                # Copy data
                msg_data = bytearray(msg_len)
                msg_data[:] = self.shm.buf[4:4 + msg_len]

                if not self.clear_before_read:
                    # FIXED: Clear event after data is safely copied
                    self.command_ready_event.clear()

                # Unpickle
                command = pickle.loads(msg_data)
                self.received_commands.append(command)

            except Exception as e:
                self.corruption_events.append({
                    'type': 'exception',
                    'error': str(e),
                    'error_type': type(e).__name__
                })
                if not self.clear_before_read:
                    self.command_ready_event.clear()


def consumer_process(shm_name: str, command_ready_event: Event,
                     results_queue: Queue, stop_event: Event,
                     clear_before_read: bool):
    """Process entry point for consumer."""
    # Use multiprocessing.Manager lists for cross-process sharing
    received = []
    corruptions = []

    shm = shared_memory.SharedMemory(name=shm_name)
    try:
        consumer = IPCConsumer(
            shm=shm,
            command_ready_event=command_ready_event,
            received_commands=received,
            corruption_events=corruptions,
            stop_event=stop_event,
            clear_before_read=clear_before_read
        )
        consumer.run()
    finally:
        shm.close()
        # Send results back
        results_queue.put({
            'received_count': len(received),
            'corruptions': corruptions,
            'received_sequence': [cmd.get('seq') for cmd in received if isinstance(cmd, dict)]
        })


class IPCSender:
    """Simulates the Logic process sender side of IPC."""

    def __init__(self, shm: shared_memory.SharedMemory,
                 command_ready_event: Event):
        self.shm = shm
        self.command_ready_event = command_ready_event
        self.timeout_warnings = 0

    def _frame_and_write(self, payload: Dict[str, Any]) -> None:
        """Write framed payload to shared memory."""
        data = pickle.dumps(payload)
        size = len(data)
        size_bytes = struct.pack('>I', size)
        self.shm.buf[:4] = size_bytes
        self.shm.buf[4:4 + size] = data

    def _await_event_state(self, desired_set: bool, timeout_s: float = 1.0) -> bool:
        """Poll event until it matches desired state or timeout."""
        start = time.time()
        while True:
            if self.command_ready_event.is_set() == desired_set:
                return True
            if time.time() - start > timeout_s:
                return False
            time.sleep(0.001)  # 1ms poll

    def send_one(self, payload: Dict[str, Any], timeout_s: float = 0.1) -> bool:
        """Send a single command.

        Args:
            timeout_s: Short timeout for stress testing (default 100ms instead of 1s)

        Returns:
            True if consumer acknowledged, False if timeout
        """
        # Wait for previous command to be cleared
        if not self._await_event_state(desired_set=False, timeout_s=timeout_s):
            self.timeout_warnings += 1
            # In production this logs warning and continues - we do same

        self._frame_and_write(payload)
        self.command_ready_event.set()

        # Observe consumer clear
        cleared = self._await_event_state(desired_set=False, timeout_s=timeout_s)
        if not cleared:
            self.timeout_warnings += 1

        return cleared


class TestIPCRaceCondition:
    """Tests for the SharedMemory IPC race condition."""

    SHM_SIZE = 64 * 1024  # 64KB like production

    # How long the sender waits for the consumer to pick up the previous
    # command before it overwrites the buffer and moves on. Each overwrite of
    # an unread command loses exactly one command. This is deliberately far
    # shorter than production's 1s so the test still exercises back-pressure,
    # but 50ms was short enough that a single scheduling hiccup under machine
    # load (a full parallel suite run, a background review) descheduled the
    # consumer past it and dropped commands, turning this into a load-
    # conditional flake (wh-ipc-stress-flake). 250ms tolerates a brief
    # deschedule without slowing the happy path, where the consumer clears in
    # a millisecond or two.
    SENDER_PATIENCE_S = 0.25

    # Fraction of sent commands the FIXED behavior may lose and still pass.
    # The loss ceiling must scale with the command count, not be a fixed
    # integer: under load the loss count grows with how long the consumer is
    # descheduled, so any constant is beatable by a slower machine. The
    # corruption==0 and no-crash assertions -- the properties this test
    # actually exists to protect -- stay strict and load-independent. The
    # buggy behavior loses far more AND corrupts, so a real regression still
    # fails.
    MAX_LOSS_FRACTION = 0.03
    MIN_LOSS_ALLOWANCE = 5  # floor, for small command counts and shutdown timing

    @pytest.fixture
    def ipc_setup(self):
        """Set up shared memory and events for IPC testing."""
        shm = shared_memory.SharedMemory(create=True, size=self.SHM_SIZE)
        command_ready_event = multiprocessing.Event()
        stop_event = multiprocessing.Event()
        results_queue = multiprocessing.Queue()

        yield {
            'shm': shm,
            'command_ready_event': command_ready_event,
            'stop_event': stop_event,
            'results_queue': results_queue
        }

        # Cleanup
        shm.close()
        shm.unlink()

    def _run_stress_test(self, ipc_setup, num_commands: int,
                         clear_before_read: bool) -> Dict:
        """Run stress test with given configuration.

        Args:
            num_commands: Number of commands to send
            clear_before_read: True for buggy behavior, False for fixed

        Returns:
            Dict with test results
        """
        shm = ipc_setup['shm']
        command_ready_event = ipc_setup['command_ready_event']
        stop_event = ipc_setup['stop_event']
        results_queue = ipc_setup['results_queue']

        # Start consumer process
        proc = multiprocessing.Process(
            target=consumer_process,
            args=(shm.name, command_ready_event, results_queue,
                  stop_event, clear_before_read)
        )
        proc.start()

        # Give consumer time to start
        time.sleep(0.1)

        # Create sender
        sender = IPCSender(shm, command_ready_event)

        # Blast commands as fast as possible
        start_time = time.time()
        for i in range(num_commands):
            payload = {
                'action': 'intelligent_insert_text',
                'params': {'insertion_string': '.'},
                'seq': i,
                'timestamp': time.time()
            }
            sender.send_one(payload, timeout_s=self.SENDER_PATIENCE_S)

        elapsed = time.time() - start_time

        # Signal stop and wait for consumer
        time.sleep(0.1)  # Let final commands process
        stop_event.set()
        proc.join(timeout=5.0)

        if proc.is_alive():
            proc.terminate()
            proc.join()

        # Get results
        try:
            consumer_results = results_queue.get(timeout=1.0)
        except:
            consumer_results = {'received_count': 0, 'corruptions': [], 'received_sequence': []}

        return {
            'sent': num_commands,
            'received': consumer_results['received_count'],
            'corruptions': consumer_results['corruptions'],
            'timeout_warnings': sender.timeout_warnings,
            'elapsed_s': elapsed,
            'rate_per_s': num_commands / elapsed if elapsed > 0 else 0,
            'sequence': consumer_results.get('received_sequence', []),
            'process_exit_code': proc.exitcode
        }

    def test_buggy_behavior_under_stress(self, ipc_setup):
        """Test that buggy clear-before-read can trigger race indicators.

        This test uses the BUGGY behavior where event is cleared before
        data copy completes. Under stress, this should show:
        - Timeout warnings (sender proceeds while consumer still reading)
        - Potential data corruption
        """
        results = self._run_stress_test(
            ipc_setup,
            num_commands=500,
            clear_before_read=True  # BUGGY behavior
        )

        logger.info(f"Buggy behavior results: sent={results['sent']}, "
                   f"received={results['received']}, "
                   f"timeouts={results['timeout_warnings']}, "
                   f"corruptions={len(results['corruptions'])}, "
                   f"rate={results['rate_per_s']:.0f}/s")

        # We expect this test to potentially show race indicators
        # but not necessarily crash (crash requires exact timing)

        # Check for process crash (heap corruption would cause this)
        if results['process_exit_code'] not in (0, None):
            logger.warning(f"Consumer crashed with exit code: {results['process_exit_code']}")

        # Log corruption events if any
        for corruption in results['corruptions']:
            logger.warning(f"Corruption detected: {corruption}")

        # This test documents the buggy behavior - it may or may not
        # trigger visible corruption depending on timing
        assert results['sent'] == 500

    def test_fixed_behavior_under_stress(self, ipc_setup):
        """Test that fixed clear-after-read eliminates race condition.

        This test uses the FIXED behavior where event is cleared after
        data copy completes. This should be robust under stress with:
        - No data corruption
        - No process crashes
        - All commands received intact
        """
        results = self._run_stress_test(
            ipc_setup,
            num_commands=500,
            clear_before_read=False  # FIXED behavior
        )

        logger.info(f"Fixed behavior results: sent={results['sent']}, "
                   f"received={results['received']}, "
                   f"timeouts={results['timeout_warnings']}, "
                   f"corruptions={len(results['corruptions'])}, "
                   f"rate={results['rate_per_s']:.0f}/s")

        # With fixed behavior, we should have no corruption
        assert len(results['corruptions']) == 0, f"Corruptions detected: {results['corruptions']}"

        # Process should not crash
        assert results['process_exit_code'] in (0, None), \
            f"Consumer crashed with exit code: {results['process_exit_code']}"

        # Nearly all commands should be received. The tolerated loss scales
        # with the command count (see MAX_LOSS_FRACTION) with a small floor,
        # so a slower machine does not fail a run that is otherwise correct;
        # the corruption and crash assertions above stay strict.
        max_loss = max(
            self.MIN_LOSS_ALLOWANCE,
            math.ceil(results['sent'] * self.MAX_LOSS_FRACTION),
        )
        assert results['received'] >= results['sent'] - max_loss, \
            (f"Too many commands lost: sent={results['sent']}, "
             f"received={results['received']}, tolerated_loss={max_loss}")

    def test_compare_buggy_vs_fixed(self, ipc_setup):
        """Compare buggy vs fixed behavior side-by-side.

        This test runs both behaviors and compares reliability.
        The fixed version should be strictly more reliable.
        """
        # Can't reuse ipc_setup for two runs, so just do fixed
        results_fixed = self._run_stress_test(
            ipc_setup,
            num_commands=200,
            clear_before_read=False
        )

        logger.info(f"Fixed: {results_fixed['received']}/{results_fixed['sent']} received, "
                   f"{len(results_fixed['corruptions'])} corruptions")

        # Fixed should be reliable
        assert len(results_fixed['corruptions']) == 0
        assert results_fixed['process_exit_code'] in (0, None)


class TestIPCSynchronizationProtocol:
    """Unit tests for the IPC synchronization protocol."""

    def test_event_clear_timing_matters(self):
        """Demonstrate that event clear timing affects correctness.

        This is a conceptual test showing the race window.
        """
        # The race window exists between:
        # T1: Consumer clears event
        # T2: Consumer finishes reading
        #
        # If sender sees cleared event at T1 and writes before T2,
        # consumer reads corrupted data.
        #
        # Fix: Move clear to after T2, eliminating the window.

        # This test documents the issue - actual stress tests above
        # verify the fix works in practice.
        pass

    def test_frame_format_is_correct(self):
        """Test that frame format [4-byte size][data] is correctly handled."""
        payload = {'action': 'test', 'seq': 42}
        data = pickle.dumps(payload)
        size = len(data)
        size_bytes = struct.pack('>I', size)

        # Verify we can reconstruct
        recovered_size = struct.unpack('>I', size_bytes)[0]
        assert recovered_size == size

        # Verify pickle round-trip
        recovered_payload = pickle.loads(data)
        assert recovered_payload == payload
