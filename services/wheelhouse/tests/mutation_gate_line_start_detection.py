"""Mutation gate for the empty tiptap prompt line-start fix (wh-pzt).

WHAT THE FIX DOES. ShadowBufferManager.synchronize (ui/shadow_buffer.py)
asks _is_editor_empty whether the element that encloses the caret carries
the class token "is-editor-empty". When it does, the buffer is read as empty
with the caret at 0 and synchronize returns early, so the placeholder text
"Type / for commands" never reaches TextPerfector as preceding characters.
"is-empty" alone marks an empty paragraph inside a non-empty editor and
keeps the normal read. Any failure of the enclosing-element read keeps the
normal read too.

WHAT IT COVERS:

  editor-empty-check-always-false          the detection never fires
  token-match-loosened-to-is-empty-substring  an empty paragraph in a
                                           non-empty editor reads as empty
  early-return-removed                     the placeholder is read and stored
  enclosing-read-error-escapes             a COMError from the enclosing read
                                           escapes the helper

RUNNER. The shared runner (services/stt_providers/shared/tests/
mutation_gate_runner.py) owns the launches, the exactly-one-match and
line-start checks, CRLF translation, the compile check (also in --check),
the per-run timeout with Windows Job ownership, PYTHONDONTWRITEBYTECODE and
__pycache__ clearing, expected-name validation against --collect-only, the
green baseline, byte-exact atomic restore with a bounded retry, the
KeyboardInterrupt-safe restore, line-buffered output, the suite-timeout
banner check, and the scope line.

VERDICTS. Modelled on tests/mutation_gate_greedy_prefix_class_separator.py:
the verdict comes from the JUnit report the shared runner already asks
pytest to write, never from lines in pytest's console output, so a captured
log record or printed text cannot become a verdict. Every failure in the
report must be an assertion failure; a failure of any other kind (an
AttributeError or TypeError upstream of the guarded behaviour) and any
<error> or <skipped> element make the run an ERROR, never a catch. Each
failure's assertion text is printed so the record shows which assertion
fired. The shared runner counts a mutation as caught only when EVERY name
in its expect list fails, which is stricter than "at least one".

pytest runs through scripts/run_tests.py, which calls `uv run pytest`.
UV_LOCKED=1 is set below so that call behaves as `uv run --locked`.

Run from services/wheelhouse:

    uv run --locked python tests/mutation_gate_line_start_detection.py
    uv run --locked python tests/mutation_gate_line_start_detection.py --check
    uv run --locked python tests/mutation_gate_line_start_detection.py early-return
"""
import importlib.util
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TARGET = SERVICE / "ui/shadow_buffer.py"
TESTS = (
    "tests/test_ui/test_shadow_buffer.py::TestEditorEmptyPlaceholder",
    "tests/test_ui/test_verified_unicode_strategy.py::TestVerifiedUnicodeStrategyEmptyTiptapPrompt",
    # Sanity: the normal synchronize path must stay green under every mutant.
    "tests/test_ui/test_shadow_buffer.py::TestSynchronize",
)

os.environ["UV_LOCKED"] = "1"

spec = importlib.util.spec_from_file_location(
    "line_start_shared_gate",
    ROOT / "services/stt_providers/shared/tests/mutation_gate_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.stdout.reconfigure(line_buffering=True, errors="backslashreplace")

_shared_run_pytest = runner._run_pytest


def _pytest_result_line(cases, failed):
    """The pytest summary line this gate's own JUnit report implies.

    Copied from tests/mutation_gate_greedy_prefix_class_separator.py. The
    shared runner reports a run whose output holds no pytest result line as
    an error, and this gate replaces pytest's stdout with its own FAILED
    lines, so it must carry the counts itself. A run that produced no case
    at all yields no count word, so the shared runner still refuses it.
    """
    parts = []
    if failed:
        parts.append(f"{len(failed)} failed")
    passed = len(cases) - len(failed)
    if passed:
        parts.append(f"{passed} passed")
    seconds = sum(float(case.get("time") or 0) for case in cases)
    return ", ".join(parts) + f" in {seconds:.2f}s"


def _run_pytest_from_junit(service, test_file):
    """One shared owned run, with its verdict read from the JUnit report."""
    result = _shared_run_pytest(service, test_file)
    out = result.stdout + result.stderr
    if "+++ Timeout +++" in out:
        # Hand the raw output back so the shared runner reports
        # suite-timeout-abort instead of any verdict.
        return result
    paths = [arg.split("=", 1)[1] for arg in result.args
             if isinstance(arg, str) and arg.startswith("--junitxml=")]
    if len(paths) != 1:
        raise RuntimeError("Shared runner did not name exactly one JUnit report")
    report = Path(paths[0])
    if result.returncode not in (0, 1) or not report.exists():
        raise RuntimeError(f"No completed test verdict (exit {result.returncode}): "
                           + out[-2000:])
    tree = ET.parse(report)
    cases = tree.findall(".//testcase")
    if not cases or tree.findall(".//error") or tree.findall(".//skipped"):
        raise RuntimeError("Empty, erroring or skipped test scope: " + out[-2000:])
    failed = []
    for case in cases:
        failure = case.find("failure")
        if failure is None:
            continue
        message = failure.get("message", "")
        name = re.sub(r"\[.*\]$", "", case.get("name", ""))
        if not (message.startswith("assert ") or message.startswith("AssertionError")):
            raise RuntimeError(f"Non-assertion test failure in {name}: {message[:400]}")
        failed.append(name)
        detail = " / ".join(line.strip() for line in message.splitlines()[:3]
                            if line.strip())
        print(f"    {name}: {detail[:240]}")
    if bool(failed) != bool(result.returncode):
        raise RuntimeError("JUnit failures disagree with the process result")
    lines = ["FAILED " + name for name in failed]
    lines.append(_pytest_result_line(cases, failed))
    return subprocess.CompletedProcess(result.args, result.returncode,
                                       "\n".join(lines), "")


runner._run_pytest = _run_pytest_from_junit
runner._failed_names = lambda output: {line[7:] for line in output.splitlines()
                                       if line.startswith("FAILED ")}


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=TARGET,
                old=old, new=new, expect=expect)


CLASS_TOKEN_RETURN = (
    '        return isinstance(class_name, str) and "is-editor-empty" in class_name.split()\n'
)

MUTATIONS = [
    mutation("editor-empty-check-always-false",
             CLASS_TOKEN_RETURN,
             "        return False\n",
             ["test_editor_empty_placeholder_reads_as_line_start",
              "test_first_word_into_empty_prompt_has_no_leading_space"]),
    mutation("token-match-loosened-to-is-empty-substring",
             CLASS_TOKEN_RETURN,
             '        return isinstance(class_name, str) and "is-empty" in class_name\n',
             ["test_empty_paragraph_in_non_empty_editor_keeps_text"]),
    mutation("early-return-removed",
             "                if self._is_editor_empty(sel_range):\n"
             "                    with self._lock:\n"
             '                        self._buffer = ""\n'
             "                        self._cursor_pos = 0\n"
             "                        self._selection_len = 0\n"
             "                    return True\n",
             "                if self._is_editor_empty(sel_range):\n"
             "                    with self._lock:\n"
             '                        self._buffer = ""\n'
             "                        self._cursor_pos = 0\n"
             "                        self._selection_len = 0\n"
             "                    pass\n",
             ["test_editor_empty_placeholder_reads_as_line_start",
              "test_first_word_into_empty_prompt_has_no_leading_space"]),
    mutation("enclosing-read-error-escapes",
             "        except (_ctypes.COMError, AttributeError, ValueError, TypeError):\n"
             "            return False\n",
             "        except (_ctypes.COMError, AttributeError, ValueError, TypeError):\n"
             "            raise\n",
             ["test_enclosing_control_error_keeps_today_path"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
