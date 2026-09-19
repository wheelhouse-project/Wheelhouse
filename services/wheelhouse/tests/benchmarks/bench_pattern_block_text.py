"""Reproduce five-run block-location medians without timing assertions.

Run with the wheelhouse venv Python. The first fifty shipped entries supply
the user-file workload; the JSON includes their IDs and input hash so catalog
changes cannot silently change the measurement. Parsing/serialization and
correctness checks happen outside the timed region.

Historical observations in pattern_block_text.py used a 12,469-byte, 599-line
user file: ~1.3 ms last block and ~33-34 ms all fifty. That original input was
not checked in; this catalog-derived workload is a reproducible replacement,
not a claim to recover its exact bytes or establish a performance bound.
"""

from hashlib import sha256
import json
from pathlib import Path
import platform
from statistics import median
import sys
from time import perf_counter
import tomllib

SERVICE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(SERVICE / "tests"))

from doc_id_catalog_fixtures import CATALOG_PATH
from speech.pattern_block_text import locate_pattern_block
import tomli_w


def workload():
    entries = tomllib.loads(CATALOG_PATH.read_text(encoding="utf-8"))["pattern"][:50]
    if len(entries) != 50:
        raise ValueError("The benchmark needs fifty shipped entries")
    text = tomli_w.dumps({"COMMAND_HOTWORD": "x-ray", "pattern": entries})
    return text, entries


def main():
    text, entries = workload()
    lines = text.splitlines(keepends=True)
    for i, expected in enumerate(entries):
        start, end = locate_pattern_block(lines, i)
        assert start is not None and end is not None
        assert tomllib.loads("".join(lines[start:end]))["pattern"] == [expected]
    last, all_blocks = [], []
    for _ in range(5):
        start = perf_counter()
        locate_pattern_block(lines, 49)
        last.append((perf_counter() - start) * 1000)
        start = perf_counter()
        for i in range(50):
            locate_pattern_block(lines, i)
        all_blocks.append((perf_counter() - start) * 1000)
    print(json.dumps({
        "python": platform.python_version(), "platform": platform.platform(),
        "source": str(CATALOG_PATH), "bytes": len(text.encode("utf-8")),
        "lines": len(lines), "blocks": len(entries),
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "doc_ids": [entry.get("doc_id") for entry in entries],
        "last_block_ms": last, "all_fifty_ms": all_blocks,
        "median_last_block_ms": median(last),
        "median_all_fifty_ms": median(all_blocks),
    }, indent=2))


if __name__ == "__main__":
    main()
