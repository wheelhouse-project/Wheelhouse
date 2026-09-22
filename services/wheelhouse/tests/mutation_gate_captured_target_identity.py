"""Mutation evidence for original-context target identity; fixture-only tests."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner

TEST = "tests/test_ui/test_captured_target_identity.py"
MUTATIONS = []


def add(name, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=TEST,
                          file=SERVICE / file, old=old, new=new, expect=list(expect)))


add("drop-original-capture", "ui/context.py",
    "    target_identity = capture_target_identity(focused_control)",
    "    target_identity = None",
    "test_fallback_uses_identity_captured_before_the_control_turns_stale")
add("drop-context-propagation", "ui/context.py",
    "        target_identity=target_identity,", "        target_identity=None,",
    "test_capture_failure_is_explicit_and_never_falls_back_to_current_foreground")
add("resolve-stale-control-again", "ui/strategies/specific.py",
    "        return identity.root or None",
    "        return _hwnd_from_control(context.focused_control)",
    "test_fallback_uses_identity_captured_before_the_control_turns_stale")
add("drop-paste-identity-propagation", "ui/strategies/specific.py",
    '    return {"target_identity": identity} if identity is not None else {}',
    "    return {}",
    "test_window_identity_change_during_clipboard_verification_refuses_input")
add("drop-pid-guard", "ui/target_identity.py",
    "            if _process_id_for_hwnd(self.hwnd) != self.process_id:",
    "            if False:",
    "test_window_identity_change_during_clipboard_verification_refuses_input")
add("drop-parent-root-guard", "ui/target_identity.py",
    "            if normalize_hwnd_for_foreground_compare(self.hwnd) != self.root:",
    "            if False:",
    "test_window_identity_change_during_clipboard_verification_refuses_input")

# Removing either of the duplicate marker probes alone is masked by the
# other. Remove both probes of one identity component for these mutations.
identity_source = (SERVICE / "ui/target_identity.py").read_text(encoding="utf-8")
start = identity_source.index("    def is_current(")
method = identity_source[start:identity_source.index("\n\ndef capture_target_identity", start)]
for label, owner, marker, catcher in (
    ("raw", "self.hwnd", "self.tag", "test_window_identity_change_during_clipboard_verification_refuses_input"),
    ("root", "self.root", "self.root_tag", "test_recycled_root_with_same_raw_handle_and_pid_is_refused"),
    ("foreground", "self.foreground_root", "self.foreground_tag", "test_browser_helper_keeps_only_the_original_foreground_object"),
):
    negative = f"read_hwnd_provenance({owner}) != {marker}"
    positive = f"read_hwnd_provenance({owner}) == {marker}"
    assert method.count(negative) == method.count(positive) == 1
    add(f"drop-{label}-object-provenance", "ui/target_identity.py", method,
        method.replace(negative, "False").replace(positive, "True"), catcher)

add("drop-unicode-foreground-guard", "ui/target_identity.py",
    "            if require_foreground and normalize_hwnd_for_foreground_compare(",
    "            if False and normalize_hwnd_for_foreground_compare(",
    "test_unicode_rechecks_focus_after_modifier_snapshot_before_send")
# Re-anchored under wh-lost-word-neighbour-paths. That branch added
# delivered_nothing=True to this refusal and reflowed the return across
# four lines, so the old pattern -- which carried the return statement --
# stopped matching and the gate reported an error instead of applying the
# mutation. The condition line ALONE is unique in specific.py (verified:
# one match, at any indent), and it carries no comment text, so rewording
# the reason comment underneath it cannot make this pattern stale again.
# The mutation is unchanged in intent: disable the last identity recheck
# before the send, and the body below it still parses under "if False:".
add("drop-last-unicode-check", "ui/strategies/specific.py",
    "        if identity is not None and not identity.is_current():\n",
    "        if False:\n",
    "test_unicode_rechecks_focus_after_modifier_snapshot_before_send")
add("drop-last-paste-check", "ui/clipboard_operations.py",
    '        if not self._captured_identity_is_current(target_identity):\n            logger.error("verified_paste: captured target changed during preparation; refusing to send")\n            return False',
    "        pass",
    "test_window_identity_change_during_clipboard_verification_refuses_input")
add("drop-clear-selection-check", "ui/clipboard_operations.py",
    "            if not self._captured_identity_is_current(target_identity):\n                self.last_cleared_selection = None\n                return False",
    "            pass",
    "test_slow_preparation_never_sends_keys_after_a_foreground_switch")
add("drop-context-probe-check", "ui/clipboard_operations.py",
    "            if not self._captured_identity_is_current(target_identity):\n                return False\n            ok, accepted, expected = verified_press_keys(*keys)",
    "            ok, accepted, expected = verified_press_keys(*keys)",
    "test_slow_preparation_never_sends_keys_after_a_foreground_switch")
add("drop-flutter-target-foreground-binding", "ui/clipboard_operations.py",
    '            matches = self._foreground_matches_target(\n                identity.root, win32gui.GetForegroundWindow(),\n                phase="captured-identity",\n            )',
    "            matches = True",
    "test_flutter_capture_must_belong_to_the_foreground_before_sendkeys")




add("drop-rejected-cache-identity", "ui/strategies/specific.py",
    "            return identity.hwnd, identity.process_id, identity.root, identity.tag",
    "            return 0, identity.process_id, 0, 0",
    "test_rejected_text_keeps_the_original_identity_when_uia_turns_stale")
add("lose-refusal-error-feedback", "ui/clipboard_operations.py",
    '            logger.error("verified_paste: captured target identity or focus changed; refusing to send")',
    '            logger.warning("verified_paste: captured target identity or focus changed; refusing to send")',
    "test_unresolved_target_refusal_keeps_error_feedback")

add("drop-final-browser-native-proof", "ui/clipboard_operations.py",
    "            return matches and identity.is_current()",
    "            return matches",
    "test_browser_probe_cannot_leave_a_stale_foreground_proof_before_paste")

# The shared runner owns launches, persisted admission, and source restoration.
# This adapter retains the stricter JUnit verdict and aggregate evidence.
import hashlib
import json
import os
from unittest.mock import patch
import xml.etree.ElementTree as ET


class OwnedGate:
    def __init__(self, evidence_dir):
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.reports = []
        self.original = {}

    @property
    def cleanup_confirmed(self):
        try:
            runner._admit_execution()
        except OSError:
            return False
        return True

    def snapshot(self, mutations):
        self.original = {m["file"]: m["file"].read_bytes() for m in mutations}
        for index, (path, data) in enumerate(self.original.items()):
            (self.evidence_dir / f"original-{index}.bin").write_bytes(data)

    def call(self, service, selection, collect=False):
        if service.resolve() != SERVICE.resolve():
            raise ValueError("This gate only owns the WheelHouse service")
        label = f"run-{len(self.reports):02d}"
        report = dict(label=label, collect=collect)
        self.reports.append(report)
        try:
            with patch.dict(os.environ, QT_QPA_PLATFORM="offscreen"):
                result = runner._invoke(service, selection, collect=collect)
        except BaseException as exc:
            report["exception"] = type(exc).__name__
            report["cleanup_confirmed"] = self.cleanup_confirmed
            raise
        report.update(exit=result.returncode, cleanup_confirmed=True)
        (self.evidence_dir / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if not collect:
            paths = [arg.split("=", 1)[1] for arg in result.args if arg.startswith("--junitxml=")]
            if len(paths) != 1:
                raise RuntimeError("Owned runner did not identify one JUnit report")
            xml = Path(paths[0])
            report["junit"] = str(xml)
            parsed = ET.parse(xml).getroot()
            report["failed"] = [tc.attrib["name"] for tc in parsed.iter("testcase") if tc.find("failure") is not None]
            report["errors"] = len(list(parsed.iter("error")))
            report["skipped"] = len(list(parsed.iter("skipped")))
            if report["errors"] or report["skipped"]:
                raise RuntimeError(f"Invalid mutation verdict: {report}")
        return result

    def save_report(self):
        restored = {str(path.relative_to(ROOT)): path.read_bytes() == data
                    for path, data in self.original.items()}
        hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.original}
        for index, path in enumerate(self.original):
            (self.evidence_dir / f"current-{index}.bin").write_bytes(path.read_bytes())
        (self.evidence_dir / "mutations.json").write_text(json.dumps(dict(
            reports=self.reports, cleanup_confirmed=self.cleanup_confirmed,
            restored=restored, hashes=hashes,
        ), indent=2), encoding="utf-8")
        return restored

    def run(self, mutations, argv=None):
        self.snapshot(mutations)
        try:
            with patch.dict(os.environ, QT_QPA_PLATFORM="offscreen"), \
                 patch.object(runner, "_run_pytest", self.call):
                status = runner.run(mutations, argv)
        finally:
            restored = self.save_report()
        if not self.cleanup_confirmed or not all(restored.values()):
            return 1
        return status


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check" in args:
        return runner.run(MUTATIONS, args)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return OwnedGate(ROOT / ".tmp/captured-target-mutations" / stamp).run(MUTATIONS, args)


if __name__ == "__main__":
    raise SystemExit(main())
