"""Mutation evidence for the stale provider cleanup
(wh-stale-provider-pid-reuse, acceptance A4).

RemoteSTTLauncher._terminate_stale_provider stops the process named in a
provider PID file only when _pid_file_names_live_provider(pid, pid_file)
returns True; otherwise it logs one INFO line and stops nothing. The PID
and port files are removed in every case. This gate removes, inverts, and
forces the check, lowers the log line, skips the cleanup, and restores the
start_provider() warning that claimed a stop, and requires the named tests
to fail.

The helper's own rules (the 2 s slack, the AccessDenied name check) are
gated by the ``pidfile-`` mutations in mutation_gate_parakeet_model_offer.py.

Run from services/wheelhouse with the worktree's own interpreter
(``sys.executable`` becomes the pytest launcher, see _launch below):

    .venv/Scripts/python.exe tests/mutation_gate_stale_provider_pid.py --check
    .venv/Scripts/python.exe tests/mutation_gate_stale_provider_pid.py

WHY THIS GATE REPLACES ``runner.LAUNCH``: the default launch runs
``uv run pytest``, and ``uv`` builds a .venv wherever it runs, which makes a
git worktree undeletable. See mutation_gate_parakeet_model_offer.py.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))

import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (see the module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly", "-p", "no:cacheprovider"]),
    ]
    return command, service


runner.LAUNCH = _launch

TESTS = (
    "tests/test_stale_provider_termination.py",
    "tests/test_remote_stt_launcher.py::TestStaleProviderOnRestart",
)
LAUNCHER_SRC = "stt/remote_stt_launcher.py"

# tests/test_stale_provider_termination.py
NEWER = "test_a_process_newer_than_the_pid_file_is_not_stopped"
OLDER = "test_a_process_older_than_the_pid_file_is_stopped"
DENIED_OTHER = "test_access_denied_and_another_program_name_is_not_stopped"
DENIED_PROVIDER = "test_access_denied_and_a_provider_name_is_stopped"
# tests/test_remote_stt_launcher.py::TestStaleProviderOnRestart
RESTART_REUSED = "test_port_mismatch_spares_a_program_that_reused_the_pid"
RESTART_MISMATCH = "test_kills_old_provider_on_port_mismatch"
RESTART_NO_PORT = "test_starts_fresh_when_no_port_file"

MUTATIONS = []


def add(name, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TESTS,
                          file=SERVICE / LAUNCHER_SRC, old=old, new=new,
                          expect=list(expect)))


CHECK = "            if self._pid_file_names_live_provider(pid, pid_file):\n"

# The old code: every live process named in the file is stopped.
add("stale-check-removed", CHECK, "            if True:\n",
    NEWER, DENIED_OTHER, RESTART_REUSED)
add("stale-check-inverted", CHECK,
    "            if not self._pid_file_names_live_provider(pid, pid_file):\n",
    NEWER, OLDER, DENIED_OTHER, DENIED_PROVIDER, RESTART_REUSED,
    RESTART_MISMATCH, RESTART_NO_PORT)
add("stale-check-never-stops", CHECK, "            if False:\n",
    OLDER, DENIED_PROVIDER, RESTART_MISMATCH, RESTART_NO_PORT)
add("stale-skip-logged-at-debug",
    "                logger.info(\n"
    "                    f\"Did not stop stale provider {provider_name} (PID {pid}): \"\n",
    "                logger.debug(\n"
    "                    f\"Did not stop stale provider {provider_name} (PID {pid}): \"\n",
    NEWER, DENIED_OTHER)
# A2: the log line still fires, so only the file check can fail.
add("stale-skip-keeps-the-files",
    "                    \"no live provider process has that id\"\n"
    "                )\n",
    "                    \"no live provider process has that id\"\n"
    "                )\n"
    "                return\n",
    NEWER, DENIED_OTHER)
# Boss ruling 2026-09-26 09:57: the start_provider() warning must not claim
# a stop that does not happen. Restore the old text.
add("restart-warning-claims-a-stop",
    "                    f\"but current port is {self.ws_port} - stopping the old provider \"\n"
    "                    \"if its PID file still names a live provider\"\n",
    "                    f\"but current port is {self.ws_port} - terminating stale process\"\n",
    RESTART_REUSED)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
