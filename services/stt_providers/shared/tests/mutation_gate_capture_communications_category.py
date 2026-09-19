"""Mutation gate for wh-screen-reader-audio-suppression-conflict step 1.

The bead's criterion A3. Two behaviours are pinned here, and both are the
same defect seen from its two halves:

  the graph asks for the Communications render category   (A1)
  the microphone asks for the Communications capture one  (A1)

Why a gate at all. The whole of step 1 is two constants. A test that reads
a constant passes whether or not anyone ever changed it, and the change is
exactly the kind a later edit reverts by accident -- "SPEECH" is the
obvious name for a speech recogniser's capture stream, and it is what the
code said from the day the WinRT path was written until this bead. What
the Communications category buys is Windows' acoustic echo canceller,
which is the only thing that can keep a screen reader's speech out of the
recogniser while the microphone stays open. Nothing else in the suite
fails if the category goes back, so these two mutations are the only
proof that the two tests are load-bearing.

Both mutations put the old constant back, one site at a time. A single
mutation covering both sites would hide a half-revert, and the pair is
what the canceller needs: the graph carries the render category and the
input node carries the capture category.

Run it from services/stt_providers/shared:

    uv run --no-sync python tests/mutation_gate_capture_communications_category.py

``--check`` answers the two offline questions -- does every pattern match
exactly once, and does every mutant still parse -- without running a test
or writing a file. It is NOT a sweep: it cannot see a survivor. Use it
while a suite or a reviewer round is live, and run the gate in full
before the final commit.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path; this keeps it working when the gate is
# invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]

WINRT_CAPTURE = SHARED / "shared_audio" / "capture" / "winrt_capture.py"

WINRT_CAPTURE_TESTS = "tests/test_winrt_capture.py"


# --------------------------------------------------------------------------
# A1/A3: both category sites name Communications
# --------------------------------------------------------------------------

CATEGORY_MUTATIONS = [
    {
        # The render half. AudioGraphSettings carries the category the
        # graph itself runs under. The pattern keeps the assignment's
        # left-hand side, which is what makes it unique: the enum name
        # alone appears again in the import line above.
        "name": "the-graph-opens-in-the-speech-render-category",
        "service": SHARED,
        "test_file": WINRT_CAPTURE_TESTS,
        "file": WINRT_CAPTURE,
        "old": (
            "        settings = AudioGraphSettings("
            "AudioRenderCategory.COMMUNICATIONS)\n"
        ),
        "new": (
            "        settings = AudioGraphSettings("
            "AudioRenderCategory.SPEECH)\n"
        ),
        "expect": [
            "test_the_graph_asks_for_the_communications_render_category",
        ],
    },
    {
        # The capture half. create_device_input_node_async carries the
        # category the microphone node runs under, and the effects
        # Windows applies to a capture stream follow this one.
        "name": "the-microphone-opens-in-the-speech-capture-category",
        "service": SHARED,
        "test_file": WINRT_CAPTURE_TESTS,
        "file": WINRT_CAPTURE,
        "old": (
            "                graph.create_device_input_node_async("
            "MediaCategory.COMMUNICATIONS),\n"
        ),
        "new": (
            "                graph.create_device_input_node_async("
            "MediaCategory.SPEECH),\n"
        ),
        "expect": [
            "test_the_microphone_asks_for_the_communications_media_category",
        ],
    },
]


MUTATIONS = CATEGORY_MUTATIONS


if __name__ == "__main__":
    sys.exit(run(MUTATIONS))
