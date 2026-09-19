"""Mutation gate for the Bravia error-envelope handling in set_brightness (wh-ybdyq.1).

Sony reports application errors inside an HTTP 200 body
({"error": [code, message], "id": N}). The fix in
integrations/bravia_control.py (set_brightness._send_request) reports such a
body as False and logs the code and message at ERROR. Each mutation below
breaks that handling; the named catcher tests in tests/test_bravia_control.py
must fail at the named assertion for the mutation to count as caught.

Usage (run from services/wheelhouse with the wheelhouse interpreter):
    python tests/mutation_gate_bravia_error_envelope.py --check
    python tests/mutation_gate_bravia_error_envelope.py [--only NAME ...] [--log PATH]

--check verifies that every pattern matches exactly once and that every mutant
compiles, then exits. A sweep also validates the expected catcher names with
--collect-only and requires a green baseline before the first mutation.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run concurrently with a test suite or edits in this worktree: the gate
rewrites integrations/bravia_control.py while each mutation runs.
"""
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
TARGET = SERVICE / "integrations" / "bravia_control.py"
TEST_FILE = "tests/test_bravia_control.py"
SELECTION = [TEST_FILE]
RUN_TIMEOUT = 180  # A clean run of the selection takes a few seconds.

MUTATIONS = [
    {
        # The envelope branch never runs, so the error body is no longer
        # recognised as Sony's error envelope. The no-result branch still
        # returns False, so only the log wording test can catch this.
        "name": "error-key-check-removed",
        "old": 'if isinstance(body, dict) and "error" in body:',
        "new": "if False:",
        "catchers": {
            "test_error_envelope_logs_code_and_message_at_error": "AssertionError",
        },
    },
    {
        # The envelope is recognised but reported as None (TV offline)
        # instead of False (command rejected).
        "name": "error-envelope-returns-none",
        "old": (
            '                        f"Error code {code}: {message}"\n'
            "                    )\n"
            "                    return False\n"
        ),
        "new": (
            '                        f"Error code {code}: {message}"\n'
            "                    )\n"
            "                    return None\n"
        ),
        "catchers": {
            "test_error_envelope_in_http_200_returns_false": "assert None is False",
        },
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(original: bytes, names):
    """Return (prepared mutants, error lines). Never touches the file."""
    source = original.decode("utf-8")
    ending = _line_ending(original)
    prepared, errors = [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        old = mutation["old"].replace("\n", ending)
        new = mutation["new"].replace("\n", ending)
        count = source.count(old)
        if count == 0:
            errors.append(f"ERROR {mutation['name']}: pattern-not-found")
            continue
        if count > 1:
            errors.append(f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches)")
            continue
        mutant = source.replace(old, new, 1)
        try:
            compile(mutant, str(TARGET), "exec")
        except SyntaxError as exc:
            errors.append(f"ERROR {mutation['name']}: does-not-compile ({exc})")
            continue
        prepared.append((mutation, mutant.encode("utf-8")))
    return prepared, errors


def _clear_bytecode():
    for cache in SERVICE.joinpath("integrations").glob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "400"  # keep short-summary messages untruncated
    return env


def _pytest(extra, timeout):
    command = [sys.executable, "-m", "pytest", *SELECTION, "-p", "no:randomly",
               "-p", "no:cacheprovider", "--junitxml=NUL" if os.name == "nt" else "--junitxml=/dev/null",
               *extra]
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
                test_id = match.group(2)
                name = test_id.split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection():
    result = _pytest(["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    if result.returncode == 1 and not records:
        raise RuntimeError(f"pytest exit 1 with no short-summary records: {output[-1500:]}")
    return records


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


def _judge(mutation, records):
    unexpected = [f"{name}: {message}" for name, (kind, message) in records.items()
                  if kind == "ERROR" or not (message.startswith("assert") or message.startswith("AssertionError"))]
    if unexpected:
        return "ERROR", "non-assertion failure: " + "; ".join(unexpected)
    fired, green, wrong = [], [], []
    for name, marker in mutation["catchers"].items():
        if name not in records:
            green.append(name)
        elif not records[name][1].startswith(marker):
            wrong.append(f"{name}: {records[name][1]} (expected {marker!r})")
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME", help="run only these mutations")
    parser.add_argument("--log", type=Path, help="also append output to this file")
    args = parser.parse_args(argv)

    log = args.log.open("a", encoding="utf-8", buffering=1) if args.log else None

    def say(text):
        print(text, flush=True)
        if log:
            log.write(text + "\n")

    known = {m["name"] for m in MUTATIONS}
    unknown = [n for n in args.only if n not in known]
    if unknown:
        say(f"ERROR unknown mutation names: {', '.join(unknown)}")
        return 1

    original = TARGET.read_bytes()
    stat = (TARGET.stat().st_atime_ns, TARGET.stat().st_mtime_ns)
    prepared, errors = _prepare(original, set(args.only))
    for line in errors:
        say(line)
    stale = sum("pattern-" in e for e in errors)
    broken = sum("does-not-compile" in e for e in errors)
    selected = len(prepared) + len(errors)
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, {broken} that do not compile")
    if args.check:
        return 1 if errors else 0

    rc, collected = _collected_names()
    missing = sorted({name for mutation, _ in prepared for name in mutation["catchers"]} - collected)
    if rc != 0 or not collected or missing:
        say(f"ERROR expected catcher names not collected (collect rc={rc}): {', '.join(missing) or 'none collected'}")
        return 1

    _clear_bytecode()
    try:
        baseline = _run_selection()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        say(f"ERROR baseline: {exc}")
        return 1
    if baseline:
        say(f"ERROR baseline not green: {sorted(baseline)}")
        return 1
    say(f"Baseline green; running {len(prepared)} of {len(MUTATIONS)} mutations"
        + (f" (--only {' '.join(args.only)})" if args.only else ""))

    survivors = 0
    error_count = len(errors)
    for index, (mutation, mutant) in enumerate(prepared, 1):
        name = mutation["name"]
        try:
            _write_with_retry(TARGET, mutant)
            os.utime(TARGET, ns=(stat[0], stat[1] + index * 1_000_000_000))
            _clear_bytecode()
            records = _run_selection()
            verdict, detail = _judge(mutation, records)
        except subprocess.TimeoutExpired:
            verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
        except RuntimeError as exc:
            verdict, detail = "ERROR", str(exc)
        finally:
            _restore(original, stat)
        if verdict == "SURVIVED":
            survivors += 1
        elif verdict == "ERROR":
            error_count += 1
        say(f"{verdict} {name}: {detail}")

    say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
        f"{len(prepared) - survivors - (error_count - len(errors))} caught, "
        f"{survivors} survived, {error_count} errors")
    if log:
        log.close()
    return 1 if survivors or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
