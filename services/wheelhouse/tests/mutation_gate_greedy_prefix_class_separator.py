"""Bounded class-separator mutation checks; run from the repository root.

--check only checks exact source patterns and mutant syntax. Execution uses
the project wrapper and canonical owned-process helper; uncertain cleanup
blocks source restoration and subsequent runs, preserving evidence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TESTS = ("tests/test_pattern_transform.py", "tests/test_router_greedy_helper.py",
         "tests/test_router_multiword_phrase.py",
         "tests/e2e/test_e2e_multiword_phrase_replacement.py")
SELECTION = ("class_separator or class_without_a_single_space or class_boundaries_keep"
             " or multiword_phrase")
EVIDENCE = ROOT / ".pytest_cache/greedy-prefix-class-mutations"
UNSAFE_MARKER = EVIDENCE / "cleanup-unconfirmed.json"
_cleanup_confirmed = True


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load("greedy_class_shared_gate", ROOT / "services/stt_providers/shared/tests/mutation_gate_runner.py")
_shared_restore = runner._restore
_shared_call_cleanup = runner._call_cleanup


def _mark_uncertain(evidence, error):
    global _cleanup_confirmed
    _cleanup_confirmed = False
    UNSAFE_MARKER.write_text(json.dumps({
        "evidence": str(evidence), "error": str(error),
        "action": "Stop. Prove owned-child exit before restoring source or removing this marker."
    }, indent=2), encoding="utf-8")


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
    owned = _load("greedy_class_owned_process", ROOT / "scripts/codex/owned_process.py")
    evidence = EVIDENCE / uuid.uuid4().hex
    evidence.mkdir(parents=True, exist_ok=False)
    report = evidence / "results.xml"
    command = [sys.executable, str(ROOT / "scripts/run_tests.py"), *tests,
               "-k", SELECTION, "--junitxml", str(report)]
    if collect:
        command.append("--collect-only")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPYCACHEPREFIX=str(evidence / "bytecode"))
    # Persist uncertainty before handing control to a child-owning operation.
    _mark_uncertain(evidence, "Owned invocation in progress")
    try:
        result = owned.run_owned(command, ROOT, env, 180, evidence)
    except owned.CleanupUnconfirmedError as exc:
        _mark_uncertain(evidence, exc)
        raise
    except BaseException as exc:
        if getattr(exc, "cleanup_confirmed", False):
            _cleanup_confirmed = True
            UNSAFE_MARKER.unlink()
        else:
            _mark_uncertain(evidence, exc)
        raise
    if not getattr(result, "cleanup_confirmed", False):
        _mark_uncertain(evidence, "Owned runner returned without confirming cleanup")
        raise RuntimeError("Owned runner returned without confirming cleanup")
    _cleanup_confirmed = True
    UNSAFE_MARKER.unlink()
    stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
    if collect:
        if result.returncode:
            raise RuntimeError("Collection failed: " + stdout + stderr)
        names = {re.sub(r"\[.*\]$", "", line.split("::")[-1].strip())
                 for line in stdout.splitlines() if line.startswith("tests/") and "::" in line}
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
            failed.append(re.sub(r"\[.*\]$", "", case.get("name", "")))
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


def _call_cleanup(call, *args):
    # The dispatcher must inspect the underlying restore entry, not the
    # already-entered ownership wrapper (the Codex98 caller contract).
    if call is _restore and _cleanup_confirmed:
        call = _shared_restore
    return _shared_call_cleanup(call, *args)


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS,
                file=SERVICE / "speech/pattern_transform.py", old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("class-not-a-boundary", '    if text[start] != "[":\n', '    if True:\n',
             ["test_class_separator_keeps_the_grouped_first_word", "test_shipped_class_separator_command_uses_greedy_timer"]),
    mutation("truncate-after-separator", '                offsets.append(start)\n', '                offsets.append(end)\n',
             ["test_class_separator_keeps_the_grouped_first_word", "test_shipped_class_separator_command_uses_greedy_timer"]),
    mutation("ignore-single-space-eligibility", '            if start and re.fullmatch(text[start:end], " ") is not None:\n',
             '            if start:\n', ["test_adjacent_class_separators_cannot_invent_a_one_space_join"]),
    mutation("remove-matcher-bound", '    while i < len(text) and len(offsets) < MAX_PREFIX_MATCHERS - 1:\n',
             '    while i < len(text):\n', ["test_class_separator_matcher_count_keeps_the_existing_bound"]),
    # wh-multiword-phrase-replacement: the escaped space (backslash, space)
    # that re.escape writes between the words of a simple-mode phrase.
    mutation("escaped-space-not-a-separator", '    if text.startswith("\\\\ ", start):\n', '    if False:\n',
             ["test_multiword_phrase_matchers_hold_each_opening",
              "test_multiword_phrase_every_alternative_holds_its_openings",
              "test_multiword_phrase_quantified_escaped_space_holds_each_word",
              "test_multiword_phrase_command_collision_keeps_buffering_at_word_two",
              "test_multiword_phrase_catalog_stores_matchers_for_each_opening",
              "test_multiword_phrase_two_word_opening_is_an_incomplete_name",
              "test_multiword_phrase_in_one_breath_types_the_replacement",
              "test_multiword_phrase_paused_after_two_words_types_the_replacement"]),
    mutation("escaped-space-ignores-quantifier",
             '        quant = _SPACE_QUANTIFIER_RE.match(text, start + 2)\n'
             '        assert quant is not None\n'
             '        return quant.end()\n',
             '        return start + 2\n',
             ["test_multiword_phrase_escaped_space_without_one_space_holds_nothing"]),
    mutation("escape-pair-split", '        elif text[i] == "\\\\":\n            i += 2\n',
             '        elif text[i] == "\\\\":\n            i += 1\n',
             ["test_multiword_phrase_escaped_backslash_then_space_stays_a_literal_backslash"]),
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
            backup = backup_dir / f"source-{index:02d}.original"
            backup.write_bytes(original)
            backups.append({"source": str(path), "backup": str(backup),
                            "sha256": hashlib.sha256(original).hexdigest()})
            for m in (m for m in MUTATIONS if m["file"] == path):
                old = runner._translate(m["old"], original)
                if original.count(old) == 1:
                    changed = original.replace(old, runner._translate(m["new"], original), 1)
                    mutant = backup_dir / (m["name"] + ".mutant")
                    mutant.write_bytes(changed)
                    backups.append({"source": str(path), "mutation": m["name"], "backup": str(mutant),
                                    "sha256": hashlib.sha256(changed).hexdigest()})
        (backup_dir / "manifest.json").write_text(json.dumps(backups, indent=2), encoding="utf-8")
        print(f"Original and mutant source backups: {backup_dir}", flush=True)
    # Each subprocess has a fresh bytecode root; do not delete shared caches.
    runner._clear_pycache = lambda service: None
    runner._run_pytest = _invoke
    runner._collected_names = lambda service, tests: _invoke(service, tests, collect=True)
    runner._failed_names = lambda output: {line[7:] for line in output.splitlines() if line.startswith("FAILED ")}
    runner._restore = _restore
    runner._call_cleanup = _call_cleanup
    return runner.run(MUTATIONS, args)


if __name__ == "__main__":
    raise SystemExit(main())
