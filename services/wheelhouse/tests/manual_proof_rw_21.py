"""Manual proof for RW-21: remove more than one guard at once (wh-full-refactor.3.1).

RW-21: a pattern_manager_tree_changed event in the closed,
post_click_settling or error state is dropped, and Logic sends nothing.
Two layers enforce the rule. decide_tree_change drops the event in those
states, and the machine's FOCUS_CHANGE arm in those states is a no-op. The
overlay mutation gate applies one change to one file per mutation, so no
single mutation breaks the rule, and the gate holds
rw-21-tree-change-state-check-skipped as an expected survivor. This script
is the proof that the RW-21 boundary test can fail: it removes the guards
named on the command line, runs the RW-21 test, and restores each file.

Run from services/wheelhouse:

    .venv/Scripts/python.exe tests/manual_proof_rw_21.py 0,1

The argument is a comma-separated list of indexes into GUARDS. Guards 1 to
3 make the FOCUS_CHANGE arm of one state act as 'show numbers', so a
tree change that reaches the machine starts a walk. Guard 4 keeps the
tracked window identity on entry to closed.

In the closed state, three layers drop the event: the state check in
decide_tree_change (0), the closed FOCUS_CHANGE arm (1), and the cleared
tracked identity (4), which makes decide_tree_change drop the event as
other_window. Measured 2026-11-11, one run each: 0,1,4 fails the closed
case; 0,2 fails the post_click_settling case; 0,1 / 0 / 1 / 1,2,3 pass all
three cases; 0,1,2,3 fails only post_click_settling. The error case
passes with guards 0 to 3; its cause is not determined. Each failure is
"assert [Frame(...)] == []": Logic sent a frame after the tree change.
After the run, `git diff --stat -- overlay_focus_hooks.py
click_overlay_state.py numbered_overlay/controller.py` must print nothing.
"""

import os
import pathlib
import shutil
import subprocess
import sys

_SHOW = "OverlayEvent(kind=OverlayEventKind.SHOW_NUMBERS)"

GUARDS = [
    # 0: decide_tree_change accepts the event in every state (the same
    #    change as the gate mutation rw-21-tree-change-state-check-skipped).
    ("overlay_focus_hooks.py",
     "    if state not in TREE_CHANGE_ACCEPTED_STATES:\n",
     "    if False:\n"),
    # 1: closed: FOCUS_CHANGE starts a walk.
    ("click_overlay_state.py",
     "            # record-only: there is no overlay to follow.\n"
     "            return ApplyResult(OverlayOutcome.NO_OP)\n",
     "            # record-only: there is no overlay to follow.\n"
     f"            return self._on_closed({_SHOW})\n"),
    # 2: post_click_settling: FOCUS_CHANGE reads at once.
    ("click_overlay_state.py",
     "            # its identity before it paints (child .2).\n"
     "            return ApplyResult(OverlayOutcome.NO_OP)\n",
     "            # its identity before it paints (child .2).\n"
     f"            return self._on_post_click_settling({_SHOW})\n"),
    # 3: error: FOCUS_CHANGE starts a fresh walk.
    ("click_overlay_state.py",
     "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
     "            return ApplyResult(OverlayOutcome.NO_OP)\n"
     "        if kind in _GENERATION_BEARING:\n",
     "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
     f"            return self._on_error({_SHOW})\n"
     "        if kind in _GENERATION_BEARING:\n"),
    # 4: _reconcile_overlay_tracked_identity keeps the tracked identity on
    #    entry to closed, so the tree-change handler does not compare
    #    against window handle 0 and drop the event as other_window.
    ("numbered_overlay/controller.py",
     "            self._overlay_tracked_identity = None\n",
     "            pass\n"),
]


def main() -> int:
    chosen = [GUARDS[int(i)] for i in sys.argv[1].split(",")]
    originals = {path: pathlib.Path(path).read_bytes() for path, _, _ in chosen}
    try:
        texts = {path: data.decode("utf-8") for path, data in originals.items()}
        for path, old, new in chosen:
            count = texts[path].count(old)
            if count != 1:
                print(f"pattern count {count} in {path}; nothing was run")
                return 2
            texts[path] = texts[path].replace(old, new, 1)
        for path, text in texts.items():
            compile(text, path, "exec")
            pathlib.Path(path).write_bytes(text.encode("utf-8"))
        for cache in pathlib.Path(".").rglob("__pycache__"):
            if ".venv" not in cache.parts:
                shutil.rmtree(cache, ignore_errors=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-p", "no:randomly", "-rf",
             "tests/test_overlay_requirements_rewalk.py",
             "-k", "test_rw_21"],
            env=env, capture_output=True, text=True, timeout=300,
        )
        for line in run.stdout.splitlines():
            if line.startswith("FAILED") or " passed" in line or " failed" in line:
                print(line[:300])
        return run.returncode
    finally:
        for path, data in originals.items():
            pathlib.Path(path).write_bytes(data)
        print("restored:", ", ".join(sorted(originals)))


if __name__ == "__main__":
    sys.exit(main())
