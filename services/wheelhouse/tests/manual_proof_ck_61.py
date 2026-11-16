"""Manual proof for CK-61: remove more than one guard at once (wh-full-refactor.3.1).

The overlay mutation gate applies one change to one file per mutation. The
CK-61 rule (an ok badge-click reply after 'hide numbers' changes nothing) has
more than one guard, so no single mutation breaks it, and the gate holds
ck-61-closed-click-complete-is-invalid as an expected survivor. This script is
the proof that the CK-61 boundary test can fail: it removes the guards named
on the command line, runs the CK-61 test, and restores each file.

Run from services/wheelhouse:

    .venv/Scripts/python.exe tests/manual_proof_ck_61.py 0,1,2

The argument is a comma-separated list of indexes into GUARDS. Expected
result (measured 2026-11-10): 0,1,2 fails both cases; 0,2 fails keep_on;
1,2 fails keep_off; 0, 1 or 2 alone passes both cases. Each failure is
"Logic sent no 'click_element' to Input within 5.0s": the late reply drove
the machine to error, and 'click three' then gave a notice, not a by-name
search. After the run, `git diff --stat -- numbered_overlay/
click_overlay_state.py` must print nothing.
"""

import os
import pathlib
import shutil
import subprocess
import sys

GUARDS = [
    # 0: _feed_click_complete_refresh feeds CLICK_COMPLETE only to a
    #    painted machine (the keep-on path).
    ("numbered_overlay/controller.py",
     "        if machine.state is not OverlayState.PAINTED:\n"
     "            logger.info(\n"
     "                \"overlay: %s at matching gen=%s but machine state=%s \"\n",
     "        if False:\n"
     "            logger.info(\n"
     "                \"overlay: %s at matching gen=%s but machine state=%s \"\n"),
    # 1: the keep-off early hide applies HIDE_NUMBERS, which a closed
    #    machine takes as a no-op; the edit feeds CLICK_COMPLETE instead.
    ("numbered_overlay/controller.py",
     "                OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),\n"
     "                source=f\"click_complete ({trigger}) gen={current_pair}\",\n",
     "                OverlayEvent(kind=OverlayEventKind.CLICK_COMPLETE),\n"
     "                source=f\"click_complete ({trigger}) gen={current_pair}\",\n"),
    # 2: the closed state takes a late CLICK_COMPLETE as a teardown no-op
    #    (the same change as the gate mutation).
    ("click_overlay_state.py",
     "            OverlayEventKind.TIMEOUT,\n"
     "            OverlayEventKind.CLICK_COMPLETE,\n"
     "        ):\n"
     "            # A build_response / paint_ack / timeout / click_complete landing in\n",
     "            OverlayEventKind.TIMEOUT,\n"
     "        ):\n"
     "            # A build_response / paint_ack / timeout / click_complete landing in\n"),
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
             "-rf", "tests/test_overlay_requirements_click.py",
             "-k", "test_ck_61"],
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
