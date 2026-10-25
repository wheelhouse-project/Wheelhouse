"""Reproducible heavy CPU load for benchmark runs under load.

Starts one busy-loop worker process per logical processor (or --workers
of them) at normal priority, and stops them all after --seconds, so a
forgotten load never keeps running. Each worker runs pure Python
arithmetic, so it holds one logical processor near 100 percent and
uses almost no memory.

Usage (from services/stt_providers/shared, with any Python 3.10+):

    python ../evaluation/cpu_load.py --seconds 1800
    python ../evaluation/cpu_load.py --seconds 1800 --workers 20

wh-transcribe-cpp-eval-adapter A5 uses it to measure STT latency while
the CPU is busy.
"""

import argparse
import multiprocessing
import os
import time


def _spin(seconds: float) -> None:
    """Busy-loop for *seconds*, counted from this worker's own start."""
    stop_at = time.monotonic() + seconds
    x = 0
    while time.monotonic() < stop_at:
        for _ in range(10_000):
            x = (x * 31 + 7) % 1_000_003


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, required=True,
                        help="Stop the load after this many seconds.")
    parser.add_argument("--workers", type=int, default=os.cpu_count(),
                        help="Worker processes (default: every logical processor).")
    args = parser.parse_args()
    if args.workers < 1 or args.seconds <= 0:
        parser.error("--workers must be 1 or more and --seconds above 0")

    workers = [
        multiprocessing.Process(target=_spin, args=(args.seconds,), daemon=True)
        for _ in range(args.workers)
    ]
    for w in workers:
        w.start()
    print(f"cpu_load: {args.workers} workers for {args.seconds:.0f} s "
          f"(pid {os.getpid()})", flush=True)
    for w in workers:
        w.join()
    print("cpu_load: done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
