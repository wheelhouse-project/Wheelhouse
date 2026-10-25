"""What the training-word mutation gates of the Parakeet provider share
(wh-first-sound-lost.23, acceptance item 31).

The gate files are mutation_gate_lead_word_recorder.py,
mutation_gate_lead_word_store.py and mutation_gate_lead_word_controller.py.
Each imports this module, which sets the launcher of the shared runner and
builds the mutation dicts. mutation_gate_lead_word.py (the join of the
engine, wh-first-sound-lost.22) keeps its own copy of the launcher, as it did
before this feature.

The launcher starts pytest directly under the provider's own interpreter,
never through ``uv run``: a uv run in a worktree can build a .venv there, and
a .venv makes the worktree undeletable (see mutation_gate_engine_audio.py).
-p no:cacheprovider keeps pytest from creating a .pytest_cache in the
provider directory.

Run a gate from services/stt_providers/sherpa_offline_parakeet_stt_server:

    .venv/Scripts/python.exe tests/mutation_gate_lead_word_<area>.py
    .venv/Scripts/python.exe tests/mutation_gate_lead_word_<area>.py name
    .venv/Scripts/python.exe tests/mutation_gate_lead_word_<area>.py --check
    .venv/Scripts/python.exe tests/mutation_gate_lead_word_<area>.py --equivalent
        # runs EQUIVALENT_MUTANTS; every one of them must be reported SURVIVED
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROVIDER = HERE.parent
PROVIDERS = PROVIDER.parent
SHARED_TESTS = PROVIDERS / "shared" / "tests"
# Running a gate as a script does not put the shared runner's directory on
# sys.path.
sys.path.insert(0, str(SHARED_TESTS))

import mutation_gate_runner as runner  # noqa: E402
from mutation_gate_runner import run  # noqa: E402

RECORDER = PROVIDER / "lead_word_recorder.py"
STORE = PROVIDER / "lead_word_store.py"
CONTROLLER = PROVIDER / "lead_word_controller.py"
ENGINE = PROVIDER / "sherpa_engine.py"
MAIN = PROVIDER / "main.py"


def _launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple."""
    python = PROVIDER / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(
            f"No interpreter for {service}: {python} does not exist. Run "
            f".claude/scripts/prepare_worktree.py to sync the providers.")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:cacheprovider",
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


def make(test_file, file):
    """A builder: m(name, old, new, expect) with the test file and file fixed.

    old and new are written with their own indentation. A pattern of ONE
    line gets a final newline added to both, so it matches the whole line and
    not the start of a longer one. A pattern of several lines is used as
    written, and may end in the middle of its last line. The runner requires
    a pattern that starts with indentation to start at a line start.
    """
    def m(name, old, new, expect, file=file, test_file=test_file):
        if "\n" not in old.rstrip("\n"):
            if not old.endswith("\n"):
                old += "\n"
            if not new.endswith("\n"):
                new = new + "\n" if new else ""
        return {
            "name": name,
            "service": PROVIDER,
            "test_file": test_file,
            "file": file,
            "old": old,
            "new": new,
            "expect": expect,
        }
    return m


def main(mutations, equivalent):
    """The command line of a gate: the set, or --equivalent for the other set."""
    if "--equivalent" in sys.argv[1:]:
        argv = [a for a in sys.argv[1:] if a != "--equivalent"]
        print(f"EQUIVALENT mutants: {len(equivalent)}; each must print SURVIVED")
        sys.exit(run(equivalent, argv))
    sys.exit(run(mutations))
