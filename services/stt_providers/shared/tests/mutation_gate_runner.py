"""The shared runner half of a mutation gate.

A gate module supplies a list of mutations and calls `run(MUTATIONS)`. Each
mutation is a dict:

    name       a short identifier; also what a command-line filter matches
    service    Path of the service directory selected through scripts/run_tests.py
    test_file  the test path to run, or a tuple of paths for one behaviour
               guarded in two suites at once
    file       Path of the source file to mutate
    old        the exact snippet to replace, written with "\\n" line endings
    new        what to put in its place
    expect     the test names that must FAIL for the mutation to count as
               caught

This module exists because tests/mutation_gate_load_metrics.py carries a
crewcut comment naming exactly this extraction ("lift the runner half of
either file into a tests/mutation_gate_runner.py ... and have both import
it"). The first half is done here and
tests/mutation_gate_capture_frame_loss.py uses it.

crewcut: the two older gates (mutation_gate_load_metrics.py and
mutation_gate_process_priority.py) still carry their own copies of this
code. Migrating them is the remaining half of that crewcut, and it is a
separate change: both have their own guard tests, and rewriting a working
gate to prove a point about duplication risks the gate itself. To finish
it: delete each file's helpers and main(), import run from here, and keep
each file's MUTATIONS list where it is.

Discipline implemented here, per the mutation-gate skill:

  - byte IO throughout, so a restore cannot rewrite the file's line endings
  - patterns translated to the target file's own line endings, so a CRLF
    file does not report every multi-line pattern as missing
  - exactly one match required; zero and two are both ERRORS, never verdicts
  - the mutant is parsed before it runs, so a syntax error cannot read as a
    catch -- Python by compiling it, PowerShell through its own parser
    (a target of any other kind skips this, since a document has no syntax)
  - every expected test name validated against real collection before the
    first mutation, so a renamed test cannot read as a survivor
  - a green baseline required, so an already-red suite cannot report every
    mutation as caught
  - a per-run timeout with Windows Job ownership and confirmed descendant
    exit before restoration; the suite's abort banner is also an error
  - a run that prints no pytest result line is an error, never a verdict,
    so a launch that dies under host load cannot read as a survivor
  - exact original/mutant backups before source writes and a pre-launch marker
    that blocks future sweeps if exit cannot be confirmed; uncertain cleanup
    leaves source unchanged until recovery, never restores under a live child
  - __pycache__ cleared with PYTHONDONTWRITEBYTECODE set, so a same-size
    mutant cannot be run from stale bytecode
  - restore in a finally that refuses to overwrite a concurrent edit, and
    that survives a Ctrl+C landing inside the restore itself, or at the
    restore call's own door
  - every write to a tracked file made whole-or-not-at-all, through a
    temporary file this process alone owns, so neither an interrupt nor a
    second gate run in the same checkout can leave a partial or foreign
    file behind
  - line-buffered output, so a killed run's log names the mutation that
    actually hung
  - a --check mode that verifies every pattern and compiles every mutant
    without running a test, reporting stale and non-compiling apart

A gate is run as a script from its service directory:

    python tests/mutation_gate_<name>.py            # the whole set
    python tests/mutation_gate_<name>.py some-name  # one, by name filter
    python tests/mutation_gate_<name>.py --check    # patterns only

Execution requires the Windows owned-process helper. --check does not load it.
Recovery artifacts are under the repository's .pytest_cache/mutation-gate-runner.
Do not remove cleanup-pending.json until owned-process exit has been established
and the recorded source/original/mutant hashes have been reconciled. The runner
does not scan for or kill processes from an earlier invocation.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE_ROOT = ROOT / ".pytest_cache/mutation-gate-runner"
RUN_TIMEOUT = 300
_owned_process = None
_cleanup_confirmed = True


def _owned():
    """Load the local stdlib helper only when execution is requested."""
    global _owned_process
    if _owned_process is None:
        path = ROOT / "scripts/codex/owned_process.py"
        name = "_gate_owned_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _owned_process = module
    return _owned_process


def _admit_execution():
    if not _cleanup_confirmed or any(EVIDENCE_ROOT.rglob("cleanup-pending.json")):
        raise OSError(
            f"Process cleanup unconfirmed; refusing launch or mutation. "
            f"Preserved recovery evidence: {EVIDENCE_ROOT}")


def _default_launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple.

    Every gate in the main checkout goes through scripts/run_tests.py, which
    starts pytest under ``uv run``. A gate module that must not let ``uv``
    build a .venv where it runs -- a git worktree, where a .venv makes the
    directory undeletable -- replaces this function by assigning
    ``runner.LAUNCH``. Nothing else about the run changes: the same owned
    Job, the same timeout, the same marker, the same output parsing.
    """
    resolved_service = service.resolve()
    suite = ("release" if resolved_service == (ROOT / "scripts/release").resolve()
             else resolved_service.relative_to(ROOT / "services").as_posix())
    command = [sys.executable, str(ROOT / "scripts/run_tests.py"), "--service", suite,
               *_targets(test_file), f"--junitxml={report}",
               *(["--collect-only"] if collect else ["-rf", "-p", "no:randomly"])]
    return command, ROOT


LAUNCH = _default_launch


def _invoke(service, test_file, collect=False):
    """No source restore or next launch until every owned process has exited.

    The pending marker exists BEFORE launch. Only explicit exit confirmation
    removes it, so a killed coordinator or failed error-report write cannot
    silently admit a later sweep. Raw logs and byte backups remain for recovery.
    """
    global _cleanup_confirmed
    _admit_execution()
    helper = _owned()
    evidence = EVIDENCE_ROOT / uuid.uuid4().hex
    evidence.mkdir(parents=True)
    report = evidence / "junit.xml"
    command, cwd = LAUNCH(service, test_file, report, collect)
    command = list(command)
    marker = evidence / "cleanup-pending.json"
    marker.write_text(json.dumps({"command": command, "service": str(service)}), encoding="utf-8")
    _cleanup_confirmed = False
    confirmed = False
    try:
        # COLUMNS=1000 keeps the FAILURE REASON on each short-summary line.
        # pytest builds that line as "FAILED <nodeid>" and then appends
        # " - <reason>" only while it fits the terminal width
        # (_pytest/terminal.py, _get_line_with_reprcrash_message: the node id
        # is never trimmed, the reason is dropped whole when the remaining
        # width is too small). A captured run has no terminal, so the width is
        # 80, and this project's node ids routinely pass 100 characters -- so
        # at the default every reason is dropped. Measured 2026-09-22 against
        # a real mutant: at COLUMNS=80 both summary lines ended at the node
        # id; at COLUMNS=1000 both carried "- AssertionError: assert ...".
        #
        # The name _failed_names reads is NOT at risk: it sits before the cut.
        # What the reason buys is the check the mutation-gate skill requires
        # before trusting a verdict -- that the expected assertion fired,
        # rather than an unrelated exception upstream of it, which reads as a
        # catch and proves nothing.
        result = helper.run_owned(command, cwd,
                                  dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                       COLUMNS="1000"),
                                  RUN_TIMEOUT, evidence)
        if result.cleanup_confirmed is not True:
            raise helper.CleanupUnconfirmedError("Test launch did not confirm process exit")
        confirmed = True
        return result
    except helper.CleanupUnconfirmedError:
        # Catch this before broad OSError; it is a typed refusal to restore.
        raise
    except BaseException as exc:
        confirmed = getattr(exc, "cleanup_confirmed", False) is True
        if not confirmed:
            raise helper.CleanupUnconfirmedError("Test launch did not confirm process exit") from exc
        raise
    finally:
        _cleanup_confirmed = confirmed
        if confirmed:
            marker.unlink()


def _save_recovery(path, original, mutated, name):
    """Complete both backups before a tracked source write becomes possible."""
    _admit_execution()
    directory = EVIDENCE_ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True)
    for suffix, data in (("original", original), ("mutant", mutated)):
        with (directory / f"source.{suffix}.bin").open("xb") as stream:
            stream.write(data)
    (directory / "source.json").write_text(json.dumps({
        "source": str(path.resolve()), "mutation": name,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "mutant_sha256": hashlib.sha256(mutated).hexdigest(),
    }), encoding="utf-8")


def _translate(pattern: str, data: bytes) -> bytes:
    raw = pattern.encode("utf-8")
    if b"\r\n" in data:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


def _syntax_problem(path: Path, mutated: bytes):
    """Whether a mutant is syntactically valid, as a message or None.

    A mutation that cannot parse reads as `caught`: the interpreter
    refuses it before a single test runs, every test in the selection
    fails, and the failure is indistinguishable from a genuine catch. The
    Python arm has always compiled the mutant for that reason. The
    PowerShell arm exists because a .ps1 target had no equivalent, and the
    installer's own test harness starts by parsing the whole script -- so
    one bad mutant would fail all of that file's tests at once and prove
    nothing.

    A target of any other kind has no syntax to check, and returns None: a
    document mutation must not be reported as broken.
    """
    if path.suffix == ".py":
        try:
            compile(mutated.decode("utf-8"), str(path), "exec")
        except SyntaxError as e:
            return f"mutant does not compile: {e}"
        return None
    if path.suffix == ".ps1":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if exe is None:
            # Not a silent skip: without the parser the check cannot be
            # made, and reporting it as fine is what this whole function
            # exists to prevent.
            return "mutant cannot be parse-checked: no powershell on PATH"
        probe = path.with_name(path.name + f".mutant-probe-{os.getpid()}")
        try:
            probe.write_bytes(mutated)
            code = (
                "$e = $null; "
                "[System.Management.Automation.Language.Parser]::ParseFile("
                f"'{probe}', [ref]$null, [ref]$e) | Out-Null; "
                "if ($e.Count -gt 0) { Write-Output $e[0].ToString(); exit 3 }"
            )
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-Command", code],
                capture_output=True, text=True, timeout=120,
            )
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        if proc.returncode != 0:
            # The parser writes the location first and the reason a few
            # lines below it, under a caret display. Keeping only the first
            # line would report where and never what, so the whole message
            # is folded onto one line and capped.
            detail = " / ".join(
                line.strip()
                for line in (proc.stdout + proc.stderr).splitlines()
                if line.strip()
            )
            return f"mutant does not parse: {detail[:400] or proc.returncode}"
        return None
    return None


def _clear_pycache(service: Path) -> None:
    for d in service.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _targets(test_file):
    """A mutation's test_file, as the list of paths to hand pytest.

    A str for the usual one-suite mutation, a tuple when one behaviour is
    guarded in two suites at once. Tuples rather than lists because run()
    uses (service, test_file) as a set key.
    """
    return [test_file] if isinstance(test_file, str) else list(test_file)


def _run_pytest(service: Path, test_file):
    return _invoke(service, test_file)


def _collected_names(service: Path, test_file):
    run = _invoke(service, test_file, collect=True)
    if run.returncode:
        raise RuntimeError("Test collection failed: " + run.stdout + run.stderr)
    names = set()
    for line in run.stdout.splitlines():
        if "::" in line:
            names.add(re.sub(r"\[.*\]$", "", line.rsplit("::", 1)[-1]))
    return names


def _failed_names(output: str):
    """The test names pytest's SHORT TEST SUMMARY section reports as failed.

    The body of a run is not a source of verdicts. A test that captures a log
    record written at ERROR level prints it into the body, and a record whose
    message begins with FAILED would be read as a test result by a scan over
    the whole output. Only the lines after the "short test summary info"
    banner are read, and only when that banner is present; without it (a gate
    that does not pass -rf) the whole output is read, as before.
    """
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "short test summary info" in line:
            lines = lines[index + 1:]
            break
    return {
        re.sub(r"\[.*\]$", "", line.split("::")[-1].split()[0])
        for line in lines
        if line.startswith("FAILED")
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Give path these exact bytes, or leave the bytes it already has.

    Path.write_bytes truncates the target and then writes it, so an
    interrupt landing between those two steps leaves bytes that are
    neither the pre-run snapshot nor the whole mutant. A source file in
    that state does not compile, and _restore reads it as somebody else's
    save and refuses to repair the damage this runner did
    (wh-stt-load-metrics.3.2.2). Writing the complete bytes to a temporary
    file beside the target and renaming over it means the target only ever
    holds one whole version, which is what keeps the three states _restore
    knows about exhaustive.

    The temporary file goes in the target's own directory because
    os.replace is atomic only within a single volume, and it carries this
    process's id because two gate runs in one checkout can select
    mutations over the same source file. Sharing one name lets each move
    the other's bytes over the target under its own rename, and lets each
    delete the file the other is about to rename
    (wh-stt-load-metrics.3.2.4). The same name is used by the fuller gate
    in tests/mutation_gate_stt_load_test.py, for the same reason.
    """
    tmp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        # Windows refuses a rename over a file another process still holds
        # open (PermissionError, WinError 5). A language server or an
        # antivirus scan reading the source is transient, so the rename is
        # retried a bounded number of times before the error propagates --
        # unbounded would hang the sweep, and no retry at all would leave the
        # mutant in place with the rename reported as failed.
        for attempt in range(1, 6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.2)
    finally:
        # A successful replace already consumed the temporary file. An
        # interrupt before it leaves the target untouched, which is the
        # point, and the temporary file behind, which is litter this
        # runner must not leave in a tracked tree. Only this process's own
        # file is removed, because the name carries this process's id.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _call_cleanup(call, *args):
    """Call a cleanup helper again if the interrupt beat it to its door.

    A guard written inside a helper cannot cover that helper's own call
    boundary: the interpreter checks for a pending signal at the callee's
    FIRST instruction, so a Ctrl+C there skips the whole call. From the
    finally in run() that meant the restore never happened at all, leaving
    the whole mutant this run had just published in a tracked source file
    for every later run and every other session in this checkout to read
    as the real code (wh-stt-load-metrics.3.2.5). The atomic write of
    wh-stt-load-metrics.3.2.2 turned the old half-written file into a
    whole mutant; it did not recover this.

    The retry happens only when the body never ran, read off the
    traceback: a call the interrupt reached at its door leaves a frame of
    the callee's still sitting on the callee's own ``def`` line, or no
    frame of the callee's at all, while a body that ran left its frame on
    one of its statements. The line is compared rather than the
    instruction offset because a closure's frame begins past offset 0.
    The distinction matters because _restore ENDS by re-raising an
    interrupt it held, so an escaping KeyboardInterrupt is the ordinary
    outcome of a call that did all of its work; calling that one again
    would repeat work already done.

    Returns ``(result, deferred)``: what ``call`` returned, and the
    KeyboardInterrupt held back here, or None. The caller re-raises the
    held one only after the bytecode caches are cleared, because a
    same-size mutant leaves current-looking bytecode behind a restored
    source file.

    Only KeyboardInterrupt is deferred; anything else propagates on the
    first attempt. The bound is two attempts, the bound _restore already
    uses, so an operator holding Ctrl+C down is not put in a loop.

    This is the third copy of this function. The other two are
    _call_cleanup in tools/stt_load_test/run.py and in
    tests/mutation_gate_stt_load_test.py, whose docstring records why
    those two are copies rather than one import. This one is a copy for
    the same reason the gate's is: the runner is standard-library-only by
    design, and it exists to rewrite source files, so a runner whose
    mutant recovery ran imported code could not be trusted to recover
    anything. The three are kept identical in behaviour, and the
    parametrised guarding fixture in tests/test_stt_load_test.py runs one
    set of tests against all three.

    crewcut: this does not close the window. The interrupt can land at
    this function's own call bytecode, or between the callee returning and
    its value being stored here. It makes the operator land two precisely
    timed interrupts where one used to do, rather than making the cleanup
    unskippable. Removing the remainder needs the calls to run with the
    signal blocked, which Python does not offer for SIGINT on Windows.
    """
    deferred = None
    for _entry in (1, 2):
        try:
            result = call(*args)
        except KeyboardInterrupt as stop:
            code = getattr(call, "__code__", None)
            started = code is None
            step = stop.__traceback__
            while step is not None:
                if (step.tb_frame.f_code is code
                        and step.tb_lineno != code.co_firstlineno):
                    started = True
                step = step.tb_next
            if started:
                raise
            if deferred is None:
                deferred = stop
            continue
        return result, deferred
    raise deferred


def _restore(path: Path, original: bytes, mutated: bytes, name: str):
    """Put the pre-run bytes back; return an error string, or None.

    Never overwrite bytes this run did not write: a save landing from an
    editor while pytest ran would otherwise be replaced by the stale
    snapshot and lost.

    A KeyboardInterrupt landing inside this function is held, the restore
    retried once, and the interrupt re-raised after the restore attempt.
    Callers clear bytecode caches in their own finally -- a same-size
    mutant otherwise leaves current-looking bytecode after source is back.
    """
    deferred = None
    error = None
    for attempt in (1, 2):
        try:
            current = path.read_bytes()
            if current == original:
                break
            if current != mutated:
                error = (f"{name}: {path} changed while pytest ran; refusing "
                         "to overwrite the concurrent edit with the stale "
                         "pre-run snapshot -- reconcile the file by hand")
                break
            _atomic_write_bytes(path, original)
            break
        except OSError as e:
            if attempt == 2:
                error = (f"{name}: restore failed; the MUTANT MAY REMAIN in "
                         f"{path}: {e}")
        except KeyboardInterrupt as stop:
            if deferred is None:
                deferred = stop
            if attempt == 2:
                raise
    if deferred is not None:
        if error:
            deferred.add_note(error)
        raise deferred
    return error


def _line_start_problem(data, old):
    """The problem, if the one match does not begin at a line start.

    An indented pattern can match four characters into a DEEPER indent,
    because the deeper line contains the shallower one as a substring.
    The count is then exactly one and the check says ok, so this failure
    hides where "pattern ambiguous" would have shown. It survives only
    while old and new carry the same leading spaces and the file's indent
    grew by exactly the difference; the next indent change moves the edit
    somewhere nobody chose.

    Observed 2026-09-04 on this gate: wrapping two announcement bodies in
    "with self._notification_lock:" added four spaces, and six patterns
    across parakeet and distil went on matching, every one of them four
    characters into the new indent, every one reported ok.

    Only a match preceded by nothing but whitespace on its own line is
    reported. A pattern that deliberately quotes the middle of a line has
    real text before it, and that case stays legal.
    """
    idx = data.index(old)
    line_start = data.rfind(b"\n", 0, idx) + 1
    before = data[line_start:idx]
    if before and not before.strip():
        return (f"pattern matches {len(before)} characters into a deeper "
                f"indent, not at a line start")
    return None


def _check(selected, total, skipped) -> int:
    """Answer, without running a test, whether each mutation still applies.

    A gate's patterns quote the source, so an ordinary fix elsewhere makes
    them stale -- and a stale pattern reads as a survivor hours into a
    sweep. This answers in seconds instead.

    Compiling each mutant is part of the answer, not an extra. A pattern
    can match exactly once and still produce text that does not parse; a
    check that asks only "does it match" then reports clean while the
    mutation can never run, and every round quoting that clean line
    spreads the false assurance (mutation-gate skill, observed on the
    load-test gate over eight rounds).

    Stale and non-compiling are counted apart because they are different
    repairs: a stale pattern is re-quoted against the current source, a
    non-compiling mutant is rewritten. Non-Python targets skip the
    compile -- a document has no syntax, and compiling one would report
    every document mutation as broken.
    """
    stale, broken = [], []
    for m in selected:
        path = m["file"]
        data = path.read_bytes()
        old = _translate(m["old"], data)
        new = _translate(m["new"], data)
        count = data.count(old)
        if count != 1:
            problem = ("pattern not found" if count == 0
                       else f"pattern ambiguous ({count} matches)")
            stale.append(m["name"])
            print(f"ERROR {m['name']}: {problem}")
            continue
        problem = _line_start_problem(data, old)
        if problem:
            stale.append(m["name"])
            print(f"ERROR {m['name']}: {problem}")
            continue
        problem = _syntax_problem(path, data.replace(old, new, 1))
        if problem:
            broken.append(m["name"])
            print(f"ERROR {m['name']}: {problem}")
            continue
        print(f"ok {m['name']}")
    print(f"checked {len(selected)} patterns of {total}, "
          f"skipped {skipped} by name filter, {len(stale)} stale, "
          f"{len(broken)} that do not compile")
    return 1 if (stale or broken) else 0


def run(mutations, argv=None) -> int:
    """Run the selected mutations; return a process exit code.

    With --check among the arguments, verify the patterns and return
    without running anything. --check sits beside the name filters rather
    than replacing them, so one mutation of a large gate can be checked
    on its own.
    """
    only = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in only
    only = [a for a in only if a != "--check"]
    selected = [m for m in mutations
                if not only or any(a in m["name"] for a in only)]
    if only and not selected:
        print(f"ERROR no mutation name matches {only}")
        return 1
    skipped = len(mutations) - len(selected)

    if check_only:
        return _check(selected, len(mutations), skipped)

    try:
        _admit_execution()
    except OSError as exc:
        print(f"ERROR {exc}")
        return 1

    errors, survivors = [], []
    suites = {(m["service"], m["test_file"]) for m in selected}

    # Validate every expected name against real collection first: a renamed
    # test can never appear in the failed set, so its mutation would report
    # as a survivor while the test that catches it sits there passing.
    for service, test_file in sorted(suites, key=lambda s: str(s)):
        try:
            real = _collected_names(service, test_file)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            print(f"ERROR collection failed for {service.name}/{test_file}: {exc}")
            return 1
        if not real:
            errors.append(f"{test_file}: collected no tests in {service.name}")
            continue
        for m in selected:
            if (m["service"], m["test_file"]) != (service, test_file):
                continue
            for name in m["expect"]:
                if name not in real:
                    errors.append(
                        f"{m['name']}: expected test {name} does not exist "
                        f"in {test_file}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation.
    for service, test_file in sorted(suites, key=lambda s: str(s)):
        _clear_pycache(service)
        try:
            base = _run_pytest(service, test_file)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            print(f"ERROR baseline failed for {service.name}/{test_file}: {exc}")
            return 1
        if base.returncode != 0:
            print(f"ERROR baseline not green for {service.name}/{test_file}; "
                  "refusing to mutate")
            print(base.stdout[-2000:])
            return 1
        print(f"baseline green: {service.name}/{test_file}")

    for m in selected:
        path, service, test_file = m["file"], m["service"], m["test_file"]
        data = path.read_bytes()
        old = _translate(m["old"], data)
        new = _translate(m["new"], data)
        count = data.count(old)
        if count != 1:
            problem = ("pattern not found" if count == 0
                       else f"pattern ambiguous ({count} matches)")
            errors.append(f"{m['name']}: {problem}")
            print(f"ERROR {m['name']}: {problem}")
            continue
        problem = _line_start_problem(data, old)
        if problem:
            errors.append(f"{m['name']}: {problem}")
            print(f"ERROR {m['name']}: {problem}")
            continue
        mutated = data.replace(old, new, 1)
        # Only a mutant with a syntax to check is checked. A markdown mutant
        # has none, and rejecting it would report every document mutation as
        # broken -- while a Python or PowerShell mutant that cannot parse
        # reads as "caught" and proves nothing.
        problem = _syntax_problem(path, mutated)
        if problem:
            errors.append(f"{m['name']}: {problem}")
            print(f"ERROR {m['name']}: {problem}")
            continue

        _save_recovery(path, data, mutated, m["name"])
        _clear_pycache(service)
        proc = None
        restore_error = None
        execution_error = None
        # The write that applies the mutant sits INSIDE this try, not above
        # it: an interrupt landing on that one write would otherwise leave
        # the mutant in a tracked file with no restore attempted at all.
        # _restore returns early when the file still holds the pre-run
        # bytes, so covering the write costs nothing when the interrupt
        # arrives before it.
        try:
            _atomic_write_bytes(path, mutated)
            proc = _run_pytest(service, test_file)
        except subprocess.TimeoutExpired:
            pass
        except (OSError, RuntimeError, ValueError) as exc:
            execution_error = str(exc)
        finally:
            # Both cleanup calls go through _call_cleanup. A guard written
            # inside a helper cannot cover that helper's own call
            # boundary, and a Ctrl+C landing there skipped the whole call
            # -- leaving the mutant the write above had already published
            # in a tracked source file, or leaving a mutant .pyc beside a
            # source file already put back (wh-stt-load-metrics.3.2.5).
            deferred = cache_deferred = None
            try:
                if _cleanup_confirmed:
                    restore_error, deferred = _call_cleanup(
                        _restore, path, data, mutated, m["name"])
                else:
                    restore_error = (f"{m['name']}: process cleanup unconfirmed; source not restored; "
                                     f"recovery evidence: {EVIDENCE_ROOT}")
            finally:
                # The cache clear is in its own finally so it runs even
                # when _restore re-raises a held interrupt. A same-size
                # mutant otherwise leaves bytecode compiled from the
                # mutant while the source on disk reads correct, and the
                # next run executes the mutant from that cache.
                if _cleanup_confirmed:
                    _, cache_deferred = _call_cleanup(_clear_pycache, service)
            if restore_error:
                print(f"ERROR {restore_error}")
            # An interrupt either helper held is re-raised only here,
            # after the cache clearing above, and the restore's goes
            # first: it is the older event and the one that says a tracked
            # file may still hold the mutant.
            if deferred is None:
                deferred = cache_deferred
            if deferred is not None:
                raise deferred
        if restore_error:
            errors.append(restore_error)
            print("aborting remaining mutations: source restoration was not confirmed")
            break
        if proc is None:
            detail = execution_error or "pytest timed out"
            errors.append(f"{m['name']}: {detail}")
            print(f"ERROR {m['name']}: {detail}")
            continue
        out = proc.stdout + proc.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print(f"ERROR {m['name']}: suite-timeout-abort")
            continue
        if not re.search(r"\b\d+ (?:passed|failed)\b[^\n]*\bin \d+(?:\.\d+)?s\b", out):
            # No pytest result line: the launch died before pytest reported,
            # or pytest collected nothing. Neither proves anything about
            # the mutant, and an empty failed set would read as a survivor
            # (observed 2026-09-09: two runs of 31 printed no result under
            # host load and were reported SURVIVED; both mutations were
            # caught when run again, wh-audit13-preengine-load-review.1).
            detail = f"no-pytest-result (exit {proc.returncode})"
            errors.append(f"{m['name']}: {detail}")
            print(f"ERROR {m['name']}: {detail}")
            continue
        # The exit code decides first. Exit 0 means pytest failed no test, so
        # the mutation survived whatever the text says; only a nonzero exit
        # makes the short summary worth reading.
        failed = set() if proc.returncode == 0 else _failed_names(out)
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(f"{m['name']}: expected {missing}")
            print(f"SURVIVED {m['name']}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
        else:
            print(f"caught {m['name']} by {sorted(failed & set(m['expect']))}")

    print(f"scope: ran {len(selected)} of {len(mutations)} mutations, "
          f"skipped {skipped} by name filter, "
          f"{len(survivors)} survivors, {len(errors)} errors")
    return 1 if (survivors or errors) else 0
