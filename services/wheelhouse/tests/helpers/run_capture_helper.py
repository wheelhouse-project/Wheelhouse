"""Tiny real-subprocess helper for ``run_capture`` action tests.

Each mode has intentionally simple, deterministic behavior so the tests can
exercise asyncio subprocess handling without substituting a mock process.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    mode = sys.argv[1]
    if mode == "emit":
        sys.stdout.write(sys.argv[2])
        return 0
    if mode == "emit_bytes":
        sys.stdout.buffer.write(b"\xff\xfeoutput\n")
        return 0
    if mode == "unicode":
        sys.stdout.write(chr(0x4E00) * int(sys.argv[2]))
        return 0
    if mode == "stderr_success":
        sys.stderr.write(sys.argv[2])
        return 0
    if mode == "stderr_failure":
        sys.stderr.write(sys.argv[2])
        return int(sys.argv[3])
    if mode == "flood_stdout_until_complete":
        chunk = b"x" * int(sys.argv[2])
        for _ in range(int(sys.argv[3])):
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(sys.stdout.fileno(), remaining)
                remaining = remaining[written:]
        Path(sys.argv[4]).write_text("complete", encoding="ascii")
        return 0
    if mode == "flood_stderr":
        chunk = b"e" * int(sys.argv[2])
        for _ in range(int(sys.argv[3])):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
        return int(sys.argv[4])
    if mode == "exit":
        return int(sys.argv[2])
    if mode == "sleep":
        time.sleep(float(sys.argv[2]))
        return 0
    raise ValueError(f"unknown helper mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
