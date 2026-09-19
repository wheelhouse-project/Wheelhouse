"""Mutation gate for the wake word model download hardening (wh-wake-word-download-hardening).

Guarded code: shared_stt/wake_word_detector.py, the download path
(_download_model) and the except arm of _load_model:

* the download has a socket timeout;
* a body that does not start with the ONNX header byte is refused;
* the whole body is written, not only the first chunk;
* a refused or failed download leaves no file;
* a model that fails to load is removed when this start downloaded it or
  when it has no ONNX header, and kept otherwise.

Catchers: TestWakeWordModelDownload in tests/test_wake_word_detector.py.
Each mutation names its catcher tests and a text that must appear on the
failing source line (">") or an error line ("E") of that test's failure
report, so a catcher that fails somewhere else is an error, not a catch.

Usage (run from services/stt_providers/shared with the shared interpreter;
never through uv):
    python tests/mutation_gate_wake_word_download.py --check
    python tests/mutation_gate_wake_word_download.py [--only NAME ...] [--log PATH]

--check verifies that every pattern matches exactly once and that every mutant
compiles, then exits. A sweep also validates the catcher names with
--collect-only and requires a green baseline before the first mutation.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run concurrently with a test suite or edits in this worktree: the gate
rewrites shared_stt/wake_word_detector.py while each mutation runs.
"""
# crewcut: the runner is modelled on
# services/wheelhouse/tests/mutation_gate_audio_pause_notice.py and copied
# rather than shared, because the two services have separate interpreters. A
# runner module importable from both services would remove the duplication.
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
TARGET = SERVICE / "shared_stt" / "wake_word_detector.py"
TEST_FILE = "tests/test_wake_word_detector.py"
BYTECODE_DIRS = (SERVICE / "shared_stt" / "__pycache__", SERVICE / "tests" / "__pycache__")
RUN_TIMEOUT = 120  # A clean run of the test file takes a few seconds.

SILENT = "test_a_server_that_sends_nothing_times_out_and_leaves_no_file"
HTML = "test_an_html_page_is_refused_and_leaves_no_file"
EMPTY = "test_an_empty_body_is_refused_and_leaves_no_file"
WHOLE = "test_an_onnx_body_is_saved_whole_and_loaded"
DOWNLOADED_BAD = "test_a_downloaded_file_that_fails_to_load_is_removed"
SAVED_HTML = "test_a_saved_file_without_the_onnx_header_is_removed_when_it_fails_to_load"
SAVED_ONNX = "test_a_saved_onnx_file_is_kept_when_the_resources_step_fails"
STOPPED = "test_a_download_stopped_part_way_leaves_no_model_file"
SHORT = "test_a_body_shorter_than_its_content_length_is_refused"

MUTATIONS = [
    {
        # A body cut short by an early close is renamed onto the model name.
        "name": "short-body-accepted",
        "old": 'if getattr(response, "length", None):',
        "new": "if False:",
        "catchers": {SHORT: "assert list(tmp_path.iterdir()) == []"},
    },
    {
        # The body is written straight to the model name, so a download stopped
        # part way leaves a partial model that the next start would keep.
        "name": "partial-file-not-used",
        "old": "open(partial, \"wb\") as out:",
        "new": "open(target, \"wb\") as out:",
        "catchers": {STOPPED: "assert not (tmp_path / \"test_word_v2.onnx\").exists()"},
    },
    {
        # No timeout: the silent server holds the connection for 4 s.
        "name": "timeout-removed",
        "old": "urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S)",
        "new": "urlopen(url)",
        "catchers": {SILENT: "< 2.0"},
    },
    {
        # Any body is accepted, so the HTML page and the empty body are saved.
        "name": "header-check-removed",
        "old": "if not first.startswith(_ONNX_HEADER):",
        "new": "if False:",
        "catchers": {
            HTML: "assert list(tmp_path.iterdir()) == []",
            EMPTY: "assert list(tmp_path.iterdir()) == []",
        },
    },
    {
        # The wrong header byte refuses a real ONNX body and marks a saved
        # ONNX file as not ONNX.
        "name": "header-byte-wrong",
        "old": '_ONNX_HEADER = b"\\x08"',
        "new": '_ONNX_HEADER = b"<"',
        "catchers": {
            WHOLE: "assert target.exists()",
            SAVED_ONNX: "assert saved.exists()",
        },
    },
    {
        # Only the first chunk is written. The Content-Length check then
        # refuses the short file, so the model is never saved at all.
        "name": "rest-of-body-not-copied",
        "old": "                    shutil.copyfileobj(response, out, _DOWNLOAD_CHUNK_BYTES)\n",
        "new": "                    pass\n",
        "catchers": {WHOLE: "assert target.exists()"},
    },
    {
        # A refused download leaves its partial file behind.
        "name": "download-except-keeps-file",
        "old": (
            "                partial.unlink(missing_ok=True)\n"
            "                continue\n"
        ),
        "new": (
            "                pass\n"
            "                continue\n"
        ),
        "catchers": {
            HTML: "assert list(tmp_path.iterdir()) == []",
            EMPTY: "assert list(tmp_path.iterdir()) == []",
        },
    },
    {
        # A model that fails to load is never removed.
        "name": "load-failure-keeps-file",
        "old": "model_path.unlink(missing_ok=True)",
        "new": "pass",
        "catchers": {
            DOWNLOADED_BAD: "assert list(tmp_path.iterdir()) == []",
            SAVED_HTML: "assert not saved.exists()",
        },
    },
    {
        # A downloaded ONNX file that fails to load is kept.
        "name": "downloaded-rule-dropped",
        "old": "if model_path == self._downloaded_path or not is_onnx:",
        "new": "if not is_onnx:",
        "catchers": {DOWNLOADED_BAD: "assert list(tmp_path.iterdir()) == []"},
    },
    {
        # A saved file without the ONNX header is kept.
        "name": "header-rule-dropped",
        "old": "if model_path == self._downloaded_path or not is_onnx:",
        "new": "if model_path == self._downloaded_path:",
        "catchers": {SAVED_HTML: "assert not saved.exists()"},
    },
    {
        # Every load failure removes the file, the shipped model included.
        "name": "load-failure-always-unlinks",
        "old": "if model_path == self._downloaded_path or not is_onnx:",
        "new": "if True:",
        "catchers": {SAVED_ONNX: "assert saved.exists()"},
    },
    {
        # The download is not recorded, so a downloaded ONNX file that fails
        # to load looks like a shipped file and is kept.
        "name": "downloaded-path-not-recorded",
        "old": "self._downloaded_path = target",
        "new": "pass",
        "catchers": {DOWNLOADED_BAD: "assert list(tmp_path.iterdir()) == []"},
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")
FAILURE_HEADER = re.compile(r"^_{3,} (\S+) _{3,}$")


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(original: bytes, names):
    """Return (prepared mutants, stale, ambiguous, broken error lines)."""
    prepared, stale, ambiguous, broken = [], [], [], []
    source = original.decode("utf-8")
    ending = _line_ending(original)
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        old = mutation["old"].replace("\n", ending)
        new = mutation["new"].replace("\n", ending)
        count = source.count(old)
        if count == 0:
            stale.append(f"ERROR {mutation['name']}: pattern-not-found")
            continue
        if count > 1:
            ambiguous.append(f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches)")
            continue
        mutant = source.replace(old, new, 1)
        try:
            compile(mutant, str(TARGET), "exec")
        except SyntaxError as exc:
            broken.append(f"ERROR {mutation['name']}: does-not-compile ({exc})")
            continue
        prepared.append((mutation, mutant.encode("utf-8")))
    return prepared, stale, ambiguous, broken


def _clear_bytecode():
    for directory in BYTECODE_DIRS:
        shutil.rmtree(directory, ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "400"  # keep short-summary messages untruncated
    return env


def _pytest(extra, timeout):
    command = [sys.executable, "-m", "pytest", TEST_FILE, "-p", "no:randomly",
               "-p", "no:cacheprovider", *extra]
    return subprocess.run(command, cwd=SERVICE, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _collected_names():
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split("[")[0].strip())
    return result.returncode, names


def _summary(output: str):
    """Map test name -> (kind, message) from pytest's short summary only."""
    records, inside = {}, False
    for line in output.splitlines():
        if SUMMARY_HEADER.match(line):
            inside = True
            continue
        if inside and SECTION_RULE.match(line):
            break
        if inside:
            match = SUMMARY_LINE.match(line)
            if match:
                name = match.group(2).split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _failure_lines(output: str):
    """Map test name -> its '>' and 'E' lines from the FAILURES section."""
    sections, current = {}, None
    for line in output.splitlines():
        header = FAILURE_HEADER.match(line)
        if header:
            current = header.group(1).split(".")[-1]
            sections[current] = []
            continue
        if SECTION_RULE.match(line):
            current = None
            continue
        if current is not None and (line.startswith(">") or line.startswith("E ")):
            sections[current].append(line)
    return sections


def _run_file():
    result = _pytest(["-q", "-rf"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    if result.returncode == 1 and not records:
        raise RuntimeError(f"pytest exit 1 with no short-summary records: {output[-1500:]}")
    return records, _failure_lines(output)


def _write_with_retry(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    last = None
    try:
        for _ in range(10):
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.5)
        raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _restore(original: bytes, stat):
    """Restore bytes and timestamps; hold a Ctrl+C until after the cache clear."""
    held = None
    for _attempt in range(2):
        try:
            _write_with_retry(TARGET, original)
            os.utime(TARGET, ns=stat)
            if TARGET.read_bytes() == original:
                break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as exc:
            print(f"RESTORE-ATTEMPT-FAILED {TARGET}: {exc}", flush=True)
    restored = False
    try:
        restored = TARGET.read_bytes() == original
    except (OSError, KeyboardInterrupt):
        pass
    if not restored:
        print(f"RESTORE FAILED: {TARGET} may still hold a mutant; check git diff", flush=True)
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {TARGET}")


def _is_assertion(message: str) -> bool:
    return message.startswith("assert") or message.startswith("AssertionError")


def _judge(mutation, records, failures):
    non_assertion = [f"{name}: {message}" for name, (kind, message) in records.items()
                     if kind == "ERROR" or not _is_assertion(message)]
    if non_assertion:
        return "ERROR", "non-assertion failure: " + "; ".join(non_assertion)
    fired, green, wrong = [], [], []
    for name, text in mutation["catchers"].items():
        if name not in records:
            green.append(name)
        elif not any(text in line for line in failures.get(name, [])):
            wrong.append(f"{name}: {records[name][1]} (expected {text!r})")
        else:
            fired.append(f"{name}: {records[name][1]}")
    if wrong:
        return "ERROR", "catcher failed at an unexpected assertion: " + "; ".join(wrong)
    others = sorted(set(records) - set(mutation["catchers"]))
    detail = []
    if fired:
        detail.append("fired: " + "; ".join(fired))
    if green:
        detail.append("expected catchers still green: " + ", ".join(green))
    if others:
        detail.append("other failures: " + "; ".join(f"{n}: {records[n][1]}" for n in others))
    if not green:
        return "CAUGHT", " | ".join(detail)
    return "SURVIVED", " | ".join(detail) or "no test failed"


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME", help="run only these mutations")
    parser.add_argument("--log", type=Path, help="also append output to this file")
    args = parser.parse_args(argv)

    log = args.log.open("a", encoding="utf-8", buffering=1) if args.log else None

    def say(text):
        print(text, flush=True)
        if log:
            log.write(text + "\n")

    try:
        known = {m["name"] for m in MUTATIONS}
        unknown = [n for n in args.only if n not in known]
        if unknown:
            say(f"ERROR unknown mutation names: {', '.join(unknown)}")
            return 1

        original = TARGET.read_bytes()
        stat = (TARGET.stat().st_atime_ns, TARGET.stat().st_mtime_ns)
        prepared, stale, ambiguous, broken = _prepare(original, set(args.only))
        errors = stale + ambiguous + broken
        for line in errors:
            say(line)
        selected = len(prepared) + len(errors)
        say(f"checked {selected} patterns, {len(stale)} stale, {len(ambiguous)} ambiguous, "
            f"{len(broken)} that do not compile")
        if args.check:
            return 1 if errors else 0

        rc, collected = _collected_names()
        missing = sorted({name for mutation, _ in prepared for name in mutation["catchers"]}
                         - collected)
        if rc != 0 or not collected or missing:
            say(f"ERROR expected catcher names not collected (collect rc={rc}): "
                f"{', '.join(missing) or 'none collected'}")
            return 1

        _clear_bytecode()
        try:
            baseline, _ = _run_file()
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            say(f"ERROR baseline: {exc}")
            return 1
        if baseline:
            say(f"ERROR baseline not green: {sorted(baseline)}")
            return 1
        say(f"Baseline green; running {len(prepared)} of {len(MUTATIONS)} mutations"
            + (f" (--only {' '.join(args.only)})" if args.only else ""))

        caught = survivors = 0
        error_count = len(errors)
        for index, (mutation, mutant) in enumerate(prepared, 1):
            name = mutation["name"]
            try:
                _write_with_retry(TARGET, mutant)
                os.utime(TARGET, ns=(stat[0], stat[1] + index * 1_000_000_000))
                _clear_bytecode()
                records, failures = _run_file()
                verdict, detail = _judge(mutation, records, failures)
            except subprocess.TimeoutExpired:
                verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
            except RuntimeError as exc:
                verdict, detail = "ERROR", str(exc)
            finally:
                _restore(original, stat)
            if TARGET.read_bytes() != original:
                say(f"ERROR {name}: target differs from the original after restore")
                return 1
            if verdict == "CAUGHT":
                caught += 1
                say(f"caught {name}: {detail}")
            elif verdict == "SURVIVED":
                survivors += 1
                say(f"SURVIVED {name}: {detail}")
            else:
                error_count += 1
                say(f"ERROR {name}: {detail}")

        say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
            f"{caught} caught, {survivors} survived, {error_count} errors")
        return 0 if caught == len(prepared) and not error_count else 1
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
