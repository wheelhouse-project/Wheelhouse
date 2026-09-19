"""Full route mutation sweep for wh-ptt-button-color-inconsistent.

Run from the repository root with the WheelHouse venv Python. Reuses the
shared gate's byte-exact restoration, interrupt handling and pattern checks.
The adapter runs the mandatory test wrapper and accepts only assertion failures
from fresh JUnit data, retaining every parameterized route's identity.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TEST = "tests/test_ptt_mode_consistency.py"
spec = importlib.util.spec_from_file_location(
    "ptt_shared_gate", ROOT / "services/stt_providers/shared/tests/mutation_gate_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def _pytest_result_line(cases, failed):
    """The pytest summary line this gate's own JUnit report implies.

    The shared runner reports a run whose output holds no pytest result line
    as an error rather than a verdict (mutation_gate_runner.py, added by
    0618a95b). This gate replaces pytest's stdout with its own FAILED lines,
    so it must carry the counts itself, or every caught mutation reads as an
    error (wh-codex-merge-audit.13.1.3). Every number below comes from the
    report this run just wrote: the case count, the failure count, and the
    summed per-case times. A run that produced no case at all yields no count
    word, so the shared runner still refuses it.
    """
    parts = []
    if failed:
        parts.append(f"{len(failed)} failed")
    passed = len(cases) - len(failed)
    if passed:
        parts.append(f"{passed} passed")
    seconds = sum(float(case.get("time") or 0) for case in cases)
    return ", ".join(parts) + f" in {seconds:.2f}s"
def run_tests(service, test_file, collect=False):
    report = service / "test-results.xml"
    before = report.stat().st_mtime_ns if report.exists() else None
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_tests.py"), test_file,
         *( ["--collect-only"] if collect else [] )],
        cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, timeout=300,
    )
    if collect:
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return {line.split("::")[-1].strip() for line in result.stdout.splitlines()
                if line.startswith("tests/") and "::" in line}
    if result.returncode not in (0, 1) or not report.exists() or report.stat().st_mtime_ns == before:
        raise RuntimeError("Test runner failed or JUnit is stale: " + result.stdout + result.stderr)
    tree = ET.parse(report)
    if tree.findall(".//error"):
        raise RuntimeError("Test setup/runtime error: " + result.stdout + result.stderr)
    cases = tree.findall(".//testcase")
    failed = []
    for case in cases:
        failure = case.find("failure")
        if failure is not None:
            message = failure.get("message", "")
            if not (message.startswith("assert ") or message.startswith("AssertionError")):
                raise RuntimeError("Non-assertion failure: " + message)
            failed.append(case.get("name"))
            print(f"  {case.get('name')}: {message.splitlines()[0]}", flush=True)
    lines = ["FAILED " + name for name in failed]
    lines.append(_pytest_result_line(cases, failed))
    result.stdout = "\n".join(lines)
    result.stderr = ""
    return result


runner._run_pytest = run_tests
runner._collected_names = lambda service, test: run_tests(service, test, collect=True)
runner._failed_names = lambda output: {line[7:] for line in output.splitlines()
                                      if line.startswith("FAILED ")}


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TEST, file=SERVICE / file,
                old=old, new=new, expect=expect)


ENABLE = [f"test_continuous_activation_leaves_ptt_mode[{route}]"
          for route in ("qt", "pystray", "floating_single", "tray_single", "ipc")]
ENTRY = [f"test_ptt_entry_routes_disable_continuous_and_show_blue[{route}]"
         for route in ("qt", "pystray", "floating_double", "tray_double", "voice")]
MUTATIONS = [
    mutation("startup-coexistence", "state_manager.py",
             '            self._speech_enabled = False  # PTT starts idle, regardless of speech-on-start.\n',
             '            pass  # Restore the conflicting startup settings.\n',
             ["test_ptt_startup_overrides_continuous_startup_setting"]),
    mutation("continuous-coexistence-all-routes", "state_manager.py",
             '                self.set_speech_interaction_mode("toggle")\n',
             '                pass\n', ENABLE + ["test_explicit_enable_during_suppressed_hold_survives_release"]),
    mutation("continuous-wrong-mode-all-routes", "state_manager.py",
             '                self.set_speech_interaction_mode("toggle")\n',
             '                self.set_speech_interaction_mode("push_to_talk")\n', ENABLE),
    mutation("ptt-entry-keeps-continuous-all-routes", "state_manager.py",
             '        if self._speech_enabled:\n            self._set_speech_enabled_explicitly(False)\n',
             '        if False:\n            self._set_speech_enabled_explicitly(False)\n', ENTRY),
    mutation("voice-entry-bypasses-state", "speech/actions.py",
             '                sm.set_speech_interaction_mode(mode)\n',
             '                pass\n', [ENTRY[-1]]),
    mutation("gui-entry-wrong-request", "gui.py",
             '        self.send_command({"action": "set_speech_interaction_mode", "mode": new_mode})\n',
             '        self.send_command({"action": "set_speech_interaction_mode", "mode": "toggle"})\n', ENTRY[:-1]),
    mutation("dispatcher-entry-wrong-mode", "main.py",
             '            "set_speech_interaction_mode": lambda: self.state_manager.set_speech_interaction_mode(command.get("mode", "toggle")),\n',
             '            "set_speech_interaction_mode": lambda: self.state_manager.set_speech_interaction_mode("toggle"),\n', ENTRY[:-1]),
    mutation("startup-leaves-engine-on-its-default", "state_manager.py",
             '        self._sync_engine_transcription_status()\n',
             '        pass  # Leave the engine on its stored enabled default.\n',
             ["test_startup_tells_engine_the_state_the_button_shows"]),
    mutation("startup-reports-a-constant-off-state", "state_manager.py",
             '        enabled = self.speech_enabled\n',
             '        enabled = False  # Report off whatever the button shows.\n',
             ["test_toggle_startup_tells_engine_speech_is_on"]),
    mutation("release-restores-stale-continuous", "state_manager.py",
             "        self._speech_enabled = getattr(self, '_speech_before_ptt', False)\n",
             "        self._speech_enabled = True\n",
             [f"test_hold_temporarily_listens_in_ptt_and_returns_idle[{ending}]"
              for ending in ("released", "drag_cancel", "gesture_cancel", "timeout")]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
