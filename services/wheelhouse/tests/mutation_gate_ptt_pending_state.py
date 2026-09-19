"""Pending/refusal mutation gate for wh-ptt-optimistic-red.

Uses the project test wrapper, fresh JUnit and bytecode locations, shared
pattern/restore logic, and bounded owned-process cleanup. --check never starts
a child or changes source. Run from the repository root.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TESTS = ("tests/test_gui.py", "tests/test_ptt_integration.py", "tests/test_state_manager.py")
EVIDENCE = ROOT / ".pytest_cache/ptt-pending-mutations"
UNSAFE_MARKER = EVIDENCE / "cleanup-unconfirmed.json"
_cleanup_confirmed = True


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load("ptt_pending_shared_gate", ROOT / "services/stt_providers/shared/tests/mutation_gate_runner.py")
_shared_restore = runner._restore


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
def _invoke(service, tests, *, collect=False):
    global _cleanup_confirmed
    owned = _load("ptt_pending_owned_process", ROOT / "scripts/codex/owned_process.py")
    evidence = EVIDENCE / uuid.uuid4().hex
    evidence.mkdir(parents=True, exist_ok=False)
    report = evidence / "results.xml"
    command = [sys.executable, str(ROOT / "scripts/run_tests.py"), *tests,
               "-k", "ptt_pending", "--junitxml", str(report)]
    if collect:
        command.append("--collect-only")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPYCACHEPREFIX=str(evidence / "bytecode"))
    try:
        result = owned.run_owned(command, ROOT, env, 180, evidence)
    except owned.CleanupUnconfirmedError as exc:
        _cleanup_confirmed = False
        UNSAFE_MARKER.write_text(json.dumps({
            "evidence": str(evidence), "error": str(exc),
            "action": "Stop. Prove owned-child exit before restoring source or clearing this marker."
        }, indent=2), encoding="utf-8")
        raise
    if not result.cleanup_confirmed:
        _cleanup_confirmed = False
        UNSAFE_MARKER.write_text(json.dumps({
            "evidence": str(evidence), "error": "Owned runner returned without confirming cleanup",
            "action": "Stop. Prove owned-child exit before restoring source or clearing this marker."
        }, indent=2), encoding="utf-8")
        raise RuntimeError("Owned runner returned without confirming cleanup")
    stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
    if collect:
        if result.returncode:
            raise RuntimeError("Collection failed: " + stdout + stderr)
        names = {line.split("::")[-1].strip() for line in stdout.splitlines()
                 if line.startswith("tests/") and "::" in line}
        if not names:
            raise RuntimeError("Collection produced no test names")
        return names
    if result.returncode not in (0, 1) or not report.exists():
        raise RuntimeError("No completed test verdict: " + stdout + stderr)
    tree = ET.parse(report)
    cases = tree.findall(".//testcase")
    if not cases or tree.findall(".//error") or tree.findall(".//skipped"):
        raise RuntimeError("Empty, erroring or skipped test scope: " + stdout + stderr)
    failed = []
    for case in cases:
        failure = case.find("failure")
        if failure is not None:
            message = failure.get("message", "")
            if not (message.startswith("assert ") or message.startswith("AssertionError")):
                raise RuntimeError("Non-assertion test failure: " + message)
            failed.append(case.get("name"))
    if bool(failed) != bool(result.returncode):
        raise RuntimeError("JUnit failures disagree with the process result")
    lines = ["FAILED " + name for name in failed]
    lines.append(_pytest_result_line(cases, failed))
    return subprocess.CompletedProcess(command, result.returncode,
                                       "\n".join(lines), "")


def _restore(path, original, mutated, name):
    if not _cleanup_confirmed:
        return f"{name}: owned children have not exited; source restoration is unsafe; see {UNSAFE_MARKER}"
    return _shared_restore(path, original, mutated, name)


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=SERVICE / file,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("ordinary-accessibility-refresh-dropped", "gui.py",
             '            if not self._ptt_feedback or not self._ptt_held or message.get("speech_enabled", False):\n',
             '            if self._ptt_feedback and (not self._ptt_held or message.get("speech_enabled", False)):\n',
             ["test_ptt_pending_release_refreshes_continuous_accessibility"]),
    mutation("ordinary-off-state-clears-refusal", "gui.py",
             '            if not self._ptt_feedback or not self._ptt_held or message.get("speech_enabled", False):\n',
             '            if True:\n',
             ["test_ptt_pending_queue_refusal_survives_ordinary_off_state"]),
    mutation("optimistic-recording", "gui.py",
             '        self._set_ptt_feedback("pending", "Push to talk requested. Waiting for listening confirmation.")\n',
             '        self._set_ptt_feedback("", "Push to talk requested. Waiting for listening confirmation.")\n        self.speech_enabled = True\n',
             ["test_ptt_pending_then_confirm", "test_ptt_pending_then_refuse", "test_ptt_pending_sonos_refusal_reaches_real_gui"]),
    mutation("pending-state-gate", "gui.py",
             '        self.button.set_state(self.speech_enabled and self._ptt_feedback not in ("pending", "refused"))\n',
             '        self.button.set_state(self.speech_enabled)\n',
             ["test_ptt_pending_ignores_an_older_hold_reply"]),
    mutation("old-reply-confirms-new-hold", "gui.py",
             '        if message.get("ptt_request_id") != self._ptt_request_id:\n',
             '        if False:\n', ["test_ptt_pending_ignores_an_older_hold_reply"]),
    mutation("released-hold-confirmed", "gui.py",
             '        if not self._ptt_held and message.get("ptt_active", False):\n',
             '        if False:\n', ["test_ptt_pending_release_before_reply_never_confirms_the_hold"]),
    mutation("release-instruction-outlives-the-hold", "gui.py",
             '        if message.get("ptt_active", False) and message.get("speech_enabled", False):\n',
             '        if message.get("speech_enabled", False):\n',
             ["test_ptt_pending_toggle_release_stops_asking_for_a_release"]),
    mutation("ended-hold-reported-as-released", "gui.py",
             '        elif self._ptt_held:\n',
             '        elif False:\n',
             ["test_ptt_pending_hold_ended_underneath_the_user_asks_for_a_new_hold[False]",
              "test_ptt_pending_hold_ended_underneath_the_user_asks_for_a_new_hold[True]"]),
    # wh-codex-merge-audit.7.1.1: the cutoff cell is chosen on speech, not on
    # the hold flag. Constant False re-routes the still-held speech-on cell
    # into "refused", which is the exact regression the finding reported.
    mutation("still-held-listening-painted-refused", "gui.py",
             '            listening = message.get("speech_enabled", False)\n',
             '            listening = False\n',
             ["test_ptt_pending_hold_ended_underneath_the_user_asks_for_a_new_hold[True]"]),
    mutation("toggle-release-reported-as-a-release", "gui.py",
             '        elif message.get("speech_enabled", False):\n            self._set_ptt_feedback("", "Listening.")\n',
             '        elif False:\n            self._set_ptt_feedback("", "Listening.")\n',
             ["test_ptt_pending_toggle_release_stops_asking_for_a_release"]),
    mutation("release-text-dropped", "gui.py",
             '            self._set_ptt_feedback("", "Push to talk released.")\n',
             '            self._set_ptt_feedback("", "Listening.")\n',
             ["test_ptt_pending_release_before_reply_never_confirms_the_hold"]),
    mutation("refusal-reason-dropped-in-gui", "gui.py",
             '            self._set_ptt_feedback("refused", message.get("ptt_refusal_reason") or "Push to talk is not listening.")\n',
             '            self._set_ptt_feedback("refused", "Push to talk is not listening.")\n',
             ["test_ptt_pending_then_refuse", "test_ptt_pending_sonos_refusal_reaches_real_gui"]),
    mutation("refused-notice-repeated", "gui.py",
             '        if state == "refused" and changed:\n',
             '        if state == "refused":\n',
             ["test_ptt_pending_repeated_refusal_shows_one_notice"]),
    mutation("notice-failure-uncontained", "gui.py",
             '            try:\n                send_notice("Push to talk", description, timeout=8)\n'
             '            except Exception:\n'
             '                logger.warning("PTT notice unavailable; button retains the reason", exc_info=True)\n',
             '            send_notice("Push to talk", description, timeout=8)\n',
             ["test_ptt_pending_notice_failure_does_not_stop_the_state_update"]),
    mutation("refusal-reason-dropped-in-state", "state_manager.py",
             "                'ptt_refusal_reason': self._ptt_refusal_reason(),\n",
             "                'ptt_refusal_reason': '',\n",
             ["test_ptt_pending_sonos_refusal_reaches_real_gui", "test_ptt_pending_state_carries_actual_reason[sonos-Sonos]"]),
    mutation("request-token-not-forwarded", "main.py",
             '            "ptt_start": lambda: self.state_manager.ptt_start(source=command.get("source", "unknown"), request_id=command.get("request_id")),\n',
             '            "ptt_start": lambda: self.state_manager.ptt_start(source=command.get("source", "unknown")),\n',
             ["test_ptt_pending_sonos_refusal_reaches_real_gui"]),
    mutation("pending-painted-red", "gui.py",
             '            color = QColor(230, 165, 20, 230)  # Amber: requested, not confirmed\n',
             '            color = QColor(200, 0, 0, 220)  # Incorrect recording red\n',
             ["test_ptt_pending_feedback_is_distinct_and_accessible"]),
    mutation("pending-glyph-back-to-white", "gui.py",
             '            painter.setPen(QColor(40, 40, 40) if self._ptt_feedback == "pending" else QColor(255, 255, 255))\n',
             '            painter.setPen(QColor(255, 255, 255))\n',
             ["test_ptt_pending_glyph_meets_the_non_text_contrast_minimum[pending]"]),
    mutation("accessible-description-dropped", "gui.py",
             '        self.setAccessibleDescription(description)\n',
             '        self.setAccessibleDescription("")\n',
             ["test_ptt_pending_feedback_is_distinct_and_accessible", "test_ptt_pending_sonos_refusal_reaches_real_gui"]),
    mutation("missing-reply-no-feedback", "gui.py",
             '        if self._ptt_feedback == "pending" and not self.shutdown_event.is_set():\n',
             '        if False:\n', ["test_ptt_pending_missing_reply_stays_non_recording"]),
    mutation("restarted-logic-state-masked", "gui.py",
             '            if (self._ptt_feedback != "pending"\n',
             '            if (False\n',
             ["test_ptt_pending_logic_restart_can_report_actual_listening[False]",
              "test_ptt_pending_logic_restart_can_report_actual_listening[True]"]),
    mutation("timeout-forgets-hold-correlation", "gui.py",
             '            self._set_ptt_feedback("refused", "Listening has not been confirmed. Release and try push to talk again.")\n',
             '            self._ptt_request_id = None\n            self._set_ptt_feedback("refused", "Listening has not been confirmed. Release and try push to talk again.")\n',
             ["test_ptt_pending_timeout_still_ignores_older_hold_reply[False]",
              "test_ptt_pending_timeout_still_ignores_older_hold_reply[True]"]),
    mutation("active-hold-not-acknowledged", "state_manager.py",
             '            # A restarted GUI needs an acknowledgement for its new request.\n',
             '            return  # Incorrectly drop the new GUI request.\n            # A restarted GUI needs an acknowledgement for its new request.\n',
             ["test_ptt_pending_new_gui_request_acknowledges_existing_hold[False]",
              "test_ptt_pending_new_gui_request_acknowledges_existing_hold[True]"]),
    mutation("timeout-state-refresh-dropped", "gui.py",
             '            self.send_command({"action": "request_initial_state"})\n',
             '            pass  # Incorrectly omit the current-state read.\n',
             ["test_ptt_pending_timeout_refreshes_an_early_restart_state"]),
]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check" not in args and UNSAFE_MARKER.exists():
        print(f"ERROR unresolved owned-process cleanup: {UNSAFE_MARKER}")
        return 1
    if "--check" not in args:
        backup_dir = EVIDENCE / "source-backups" / uuid.uuid4().hex
        backup_dir.mkdir(parents=True, exist_ok=False)
        backups = []
        for index, path in enumerate(sorted({m["file"] for m in MUTATIONS})):
            original = path.read_bytes()
            backup = backup_dir / f"source-{index:02d}.bin"
            backup.write_bytes(original)
            backups.append({"source": str(path), "backup": str(backup),
                            "sha256": hashlib.sha256(original).hexdigest()})
        (backup_dir / "manifest.json").write_text(json.dumps(backups, indent=2), encoding="utf-8")
        print(f"Original source backups: {backup_dir}", flush=True)
    # Each subprocess gets a new bytecode root; no shared cache deletion.
    runner._clear_pycache = lambda service: None
    runner._run_pytest = _invoke
    runner._collected_names = lambda service, tests: _invoke(service, tests, collect=True)
    runner._failed_names = lambda output: {line[7:] for line in output.splitlines() if line.startswith("FAILED ")}
    runner._restore = _restore
    return runner.run(MUTATIONS, args)


if __name__ == "__main__":
    raise SystemExit(main())
