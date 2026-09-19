"""Mutation gate for the software dimmer default and its fallback.

Guarded code:

* coordinators/brightness_coordinator.py, BrightnessCoordinator.__init__:
  the default software dimmer type is gamma_dimmer; the accepted values
  software_dimmer, overlay and gamma_dimmer are kept without a warning; any
  other value logs exactly one WARNING that names the value seen and the
  accepted values, and then the coordinator uses gamma_dimmer.
* service_manager.py, ServiceManager.initialize_services: software_dimmer
  and overlay build SoftwareDimmer; every other value, including a missing
  key, builds GammaDimmer, and service_manager logs no WARNING of its own.

Catchers: TestDimmerTypeSelection in
tests/test_coordinators/test_brightness_coordinator_dimmer_type.py and
TestInitializeServices in tests/test_service_manager_lifecycle.py. Each
mutation names its catcher tests (a parametrized case by its full name with
the explicit id) and a text that must appear on the failing source line (">")
or an error line ("E") of that test's failure report, so a catcher that fails
somewhere else is an error, not a catch.

Equivalent mutant left out on purpose: changing only the service_manager
default (for example removing it, so a missing key reads as None) cannot
change behaviour, because every value other than software_dimmer and overlay
builds GammaDimmer. The mutation sm-default-and-else-arm-removed changes the
default together with the fallback arm instead, which is the real
regression.

Usage (run from the worktree's services/wheelhouse with the main checkout's
wheelhouse interpreter; never through uv):
    python tests/mutation_gate_dimmer_default.py --check
    python tests/mutation_gate_dimmer_default.py [--only NAME ...] [--log PATH]

--check verifies that every pattern matches exactly once and that every mutant
compiles, then exits. A sweep also validates the catcher names with
--collect-only and requires a green baseline before the first mutation.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run concurrently with a test suite or edits in this worktree: the gate
rewrites the two target files while each mutation runs.
"""
# crewcut: the runner is copied from
# services/stt_providers/shared/tests/mutation_gate_wake_word_download.py
# rather than shared, because each gate lives beside its own service's tests.
# A runner module importable from every service would remove the duplication.
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
COORDINATOR = SERVICE / "coordinators" / "brightness_coordinator.py"
SERVICE_MANAGER = SERVICE / "service_manager.py"
TEST_FILES = (
    "tests/test_coordinators/test_brightness_coordinator_dimmer_type.py",
    "tests/test_service_manager_lifecycle.py",
)
BYTECODE_DIRS = (
    SERVICE / "__pycache__",
    SERVICE / "coordinators" / "__pycache__",
    SERVICE / "tests" / "__pycache__",
    SERVICE / "tests" / "test_coordinators" / "__pycache__",
)
RUN_TIMEOUT = 180  # A clean run of both test files takes about 6 seconds.

C_DEFAULT = "test_no_software_dimmer_key_uses_gamma_dimmer"
C_UNRECOGNISED = "test_unrecognised_value_is_refused_with_one_warning_and_uses_gamma_dimmer"
C_KEEP = "test_accepted_type_is_kept_without_warning"
C_KEEP_SOFTWARE = C_KEEP + "[software-dimmer]"
C_KEEP_OVERLAY = C_KEEP + "[overlay]"
C_KEEP_GAMMA = C_KEEP + "[gamma-dimmer]"
S_UNRECOGNISED = "test_dimmer_type_unrecognised_builds_gamma_dimmer_without_warning"
S_DEFAULT = "test_dimmer_type_default_builds_gamma_dimmer"
S_OVERLAY = "test_dimmer_type_overlay[overlay]"
S_SOFTWARE = "test_dimmer_type_overlay[software-dimmer]"

TYPE_IS_GAMMA = 'assert coordinator._software_dimmer_type == "gamma_dimmer"'
TYPE_KEPT = "assert coordinator._software_dimmer_type == value"
ONE_WARNING = "assert len(warnings) == 1"
ACCEPTED_NAME = "assert accepted in accepted_part"
VALUE_SEEN = "assert \"'nonsense'\" in seen_part"
NO_WARNING = "assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []"
GAMMA_BUILT = "mock_gamma_cls.assert_called_once_with(service_manager.loop)"
SOFTWARE_BUILT = "mock_dimmer_cls.assert_called_once_with(service_manager.loop)"
NO_SM_WARNING = "assert sm_warnings == []"

COORD_CHECK = (
    'coordinator_config.get("software_dimmer", "gamma_dimmer")\n'
    '        if self._software_dimmer_type not in ("software_dimmer", "overlay", "gamma_dimmer"):\n'
)
ACCEPTED_TEXT = '"accepted values are software_dimmer, overlay, gamma_dimmer. "'

MUTATIONS = [
    {
        # A default that is not accepted: the fallback turns it into
        # gamma_dimmer, so the only visible change is a WARNING for a config
        # that never set the key.
        "name": "coord-default-unrecognised",
        "target": COORDINATOR,
        "old": 'coordinator_config.get("software_dimmer", "gamma_dimmer")',
        "new": 'coordinator_config.get("software_dimmer", "nonsense")',
        "catchers": {C_DEFAULT: NO_WARNING},
    },
    {
        # An accepted default other than gamma_dimmer: no warning, wrong type.
        "name": "coord-default-overlay",
        "target": COORDINATOR,
        "old": 'coordinator_config.get("software_dimmer", "gamma_dimmer")',
        "new": 'coordinator_config.get("software_dimmer", "overlay")',
        "catchers": {C_DEFAULT: TYPE_IS_GAMMA},
    },
    {
        # No check at all: no warning, and the unrecognised value is kept.
        "name": "coord-check-removed",
        "target": COORDINATOR,
        "old": COORD_CHECK,
        "new": COORD_CHECK.replace(
            'if self._software_dimmer_type not in ("software_dimmer", "overlay", "gamma_dimmer"):',
            "if False:",
        ),
        "catchers": {C_UNRECOGNISED: TYPE_IS_GAMMA},
    },
    {
        # The warning is logged but the unrecognised value is kept.
        "name": "coord-unrecognised-value-kept",
        "target": COORDINATOR,
        "old": '            self._software_dimmer_type = "gamma_dimmer"\n',
        "new": "            pass\n",
        "catchers": {C_UNRECOGNISED: TYPE_IS_GAMMA},
    },
    {
        # The value is replaced but no warning is logged.
        "name": "coord-warning-removed",
        "target": COORDINATOR,
        "old": "            logger.warning(\n"
               '                f"brightness_coordinator.software_dimmer = ',
        "new": "            (lambda *_: None)(\n"
               '                f"brightness_coordinator.software_dimmer = ',
        "catchers": {C_UNRECOGNISED: ONE_WARNING},
    },
    {
        # The refusal is logged below WARNING, so no warning is seen.
        "name": "coord-warning-downgraded-to-info",
        "target": COORDINATOR,
        "old": "            logger.warning(\n"
               '                f"brightness_coordinator.software_dimmer = ',
        "new": "            logger.info(\n"
               '                f"brightness_coordinator.software_dimmer = ',
        "catchers": {C_UNRECOGNISED: ONE_WARNING},
    },
    {
        # A second WARNING after the first one.
        "name": "coord-second-warning",
        "target": COORDINATOR,
        "old": (
            '                "Using gamma_dimmer."\n'
            "            )\n"
        ),
        "new": (
            '                "Using gamma_dimmer."\n'
            "            )\n"
            '            logger.warning("Using gamma_dimmer.")\n'
        ),
        "catchers": {C_UNRECOGNISED: ONE_WARNING},
    },
    {
        "name": "coord-warning-drops-overlay",
        "target": COORDINATOR,
        "old": ACCEPTED_TEXT,
        "new": '"accepted values are software_dimmer, gamma_dimmer. "',
        "catchers": {C_UNRECOGNISED: ACCEPTED_NAME},
    },
    {
        # 'software_dimmer' still appears in the config key name at the start
        # of the message, so the catcher must look inside the accepted list.
        "name": "coord-warning-drops-software-dimmer",
        "target": COORDINATOR,
        "old": ACCEPTED_TEXT,
        "new": '"accepted values are overlay, gamma_dimmer. "',
        "catchers": {C_UNRECOGNISED: ACCEPTED_NAME},
    },
    {
        # 'gamma_dimmer' still appears in "Using gamma_dimmer.", so the
        # catcher must look inside the accepted list.
        "name": "coord-warning-drops-gamma-dimmer",
        "target": COORDINATOR,
        "old": ACCEPTED_TEXT,
        "new": '"accepted values are software_dimmer, overlay. "',
        "catchers": {C_UNRECOGNISED: ACCEPTED_NAME},
    },
    {
        "name": "coord-warning-drops-value-seen",
        "target": COORDINATOR,
        "old": 'f"brightness_coordinator.software_dimmer = {self._software_dimmer_type!r} "',
        "new": '"brightness_coordinator.software_dimmer "',
        "catchers": {C_UNRECOGNISED: VALUE_SEEN},
    },
    {
        # The value is named without its quotes, so an empty or blank value
        # would be invisible in the log.
        "name": "coord-warning-value-not-repr",
        "target": COORDINATOR,
        "old": 'f"brightness_coordinator.software_dimmer = {self._software_dimmer_type!r} "',
        "new": 'f"brightness_coordinator.software_dimmer = {self._software_dimmer_type} "',
        "catchers": {C_UNRECOGNISED: VALUE_SEEN},
    },
    {
        # The check fires for every value except gamma_dimmer, so the other
        # two accepted values are warned about and replaced.
        "name": "coord-check-too-broad",
        "target": COORDINATOR,
        "old": COORD_CHECK,
        "new": COORD_CHECK.replace(
            'not in ("software_dimmer", "overlay", "gamma_dimmer"):',
            '!= "gamma_dimmer":',
        ),
        "catchers": {C_KEEP_SOFTWARE: TYPE_KEPT, C_KEEP_OVERLAY: TYPE_KEPT},
    },
    {
        "name": "coord-check-refuses-overlay",
        "target": COORDINATOR,
        "old": COORD_CHECK,
        "new": COORD_CHECK.replace(
            '("software_dimmer", "overlay", "gamma_dimmer"):',
            '("software_dimmer", "gamma_dimmer"):',
        ),
        "catchers": {C_KEEP_OVERLAY: TYPE_KEPT},
    },
    {
        "name": "coord-check-refuses-software-dimmer",
        "target": COORDINATOR,
        "old": COORD_CHECK,
        "new": COORD_CHECK.replace(
            '("software_dimmer", "overlay", "gamma_dimmer"):',
            '("overlay", "gamma_dimmer"):',
        ),
        "catchers": {C_KEEP_SOFTWARE: TYPE_KEPT},
    },
    {
        # gamma_dimmer is refused and then replaced by itself, so the type is
        # right and only the warning shows the defect.
        "name": "coord-check-refuses-gamma-dimmer",
        "target": COORDINATOR,
        "old": COORD_CHECK,
        "new": COORD_CHECK.replace(
            '("software_dimmer", "overlay", "gamma_dimmer"):',
            '("software_dimmer", "overlay"):',
        ),
        "catchers": {C_KEEP_GAMMA: NO_WARNING, C_DEFAULT: NO_WARNING},
    },
    {
        # An unrecognised value builds no dimmer object.
        "name": "sm-unrecognised-builds-no-dimmer",
        "target": SERVICE_MANAGER,
        "old": (
            "        else:\n"
            "            from services.wheelhouse.handlers.gamma_dimmer import GammaDimmer\n"
        ),
        "new": (
            '        elif dimmer_type == "gamma_dimmer":\n'
            "            from services.wheelhouse.handlers.gamma_dimmer import GammaDimmer\n"
        ),
        "catchers": {S_UNRECOGNISED: GAMMA_BUILT},
    },
    {
        # The old shape: no default, and only 'gamma_dimmer' builds
        # GammaDimmer, so a missing key and an unrecognised value build none.
        "name": "sm-default-and-else-arm-removed",
        "target": SERVICE_MANAGER,
        "old": (
            'get("brightness_coordinator.software_dimmer", "gamma_dimmer")\n'
            '        if dimmer_type in ("software_dimmer", "overlay"):\n'
            "            self.software_dimmer = SoftwareDimmer(self.loop)\n"
            '            log.info("Using SoftwareDimmer (overlay window)")\n'
            "        else:\n"
        ),
        "new": (
            'get("brightness_coordinator.software_dimmer")\n'
            '        if dimmer_type in ("software_dimmer", "overlay"):\n'
            "            self.software_dimmer = SoftwareDimmer(self.loop)\n"
            '            log.info("Using SoftwareDimmer (overlay window)")\n'
            '        elif dimmer_type == "gamma_dimmer":\n'
        ),
        "catchers": {S_DEFAULT: GAMMA_BUILT, S_UNRECOGNISED: GAMMA_BUILT},
    },
    {
        # Every value other than gamma_dimmer builds SoftwareDimmer.
        "name": "sm-unrecognised-builds-software-dimmer",
        "target": SERVICE_MANAGER,
        "old": 'if dimmer_type in ("software_dimmer", "overlay"):',
        "new": 'if dimmer_type != "gamma_dimmer":',
        "catchers": {S_UNRECOGNISED: GAMMA_BUILT},
    },
    {
        # A missing key builds SoftwareDimmer.
        "name": "sm-default-software-dimmer",
        "target": SERVICE_MANAGER,
        "old": 'get("brightness_coordinator.software_dimmer", "gamma_dimmer")',
        "new": 'get("brightness_coordinator.software_dimmer", "overlay")',
        "catchers": {S_DEFAULT: GAMMA_BUILT},
    },
    {
        "name": "sm-overlay-not-mapped",
        "target": SERVICE_MANAGER,
        "old": 'if dimmer_type in ("software_dimmer", "overlay"):',
        "new": 'if dimmer_type in ("software_dimmer",):',
        "catchers": {S_OVERLAY: SOFTWARE_BUILT},
    },
    {
        "name": "sm-software-dimmer-not-mapped",
        "target": SERVICE_MANAGER,
        "old": 'if dimmer_type in ("software_dimmer", "overlay"):',
        "new": 'if dimmer_type in ("overlay",):',
        "catchers": {S_SOFTWARE: SOFTWARE_BUILT},
    },
    {
        # service_manager logs its own WARNING for the fallback, so a start
        # with an unrecognised value logs two.
        "name": "sm-adds-own-warning",
        "target": SERVICE_MANAGER,
        "old": '            log.info("Using GammaDimmer (native gamma ramp)")\n',
        "new": (
            '            log.info("Using GammaDimmer (native gamma ramp)")\n'
            '            log.warning(f"software_dimmer {dimmer_type!r}: using GammaDimmer")\n'
        ),
        "catchers": {S_UNRECOGNISED: NO_SM_WARNING},
    },
]

TARGETS = (COORDINATOR, SERVICE_MANAGER)

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")
FAILURE_HEADER = re.compile(r"^_{3,} (\S+) _{3,}$")


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(originals, names):
    """Return (prepared mutants, stale, ambiguous, broken error lines)."""
    prepared, stale, ambiguous, broken = [], [], [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        target = mutation["target"]
        original = originals[target]
        source = original.decode("utf-8")
        ending = _line_ending(original)
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
            compile(mutant, str(target), "exec")
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
    command = [sys.executable, "-m", "pytest", *TEST_FILES, "-p", "no:randomly",
               "-p", "no:cacheprovider", *extra]
    return subprocess.run(command, cwd=SERVICE, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _collected_names():
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            # Keep the parametrized name (explicit ids carry no space) and
            # the base name, so a catcher may name either.
            full = line.split("::")[-1].strip()
            names.add(full)
            names.add(full.split("[")[0])
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


def _run_files():
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


def _restore(target: Path, original: bytes, stat):
    """Restore bytes and timestamps; hold a Ctrl+C until after the cache clear."""
    held = None
    for _attempt in range(2):
        try:
            _write_with_retry(target, original)
            os.utime(target, ns=stat)
            if target.read_bytes() == original:
                break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as exc:
            print(f"RESTORE-ATTEMPT-FAILED {target}: {exc}", flush=True)
    restored = False
    try:
        restored = target.read_bytes() == original
    except (OSError, KeyboardInterrupt):
        pass
    if not restored:
        print(f"RESTORE FAILED: {target} may still hold a mutant; check git diff", flush=True)
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {target}")


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

        originals = {target: target.read_bytes() for target in TARGETS}
        stats = {target: (target.stat().st_atime_ns, target.stat().st_mtime_ns)
                 for target in TARGETS}
        prepared, stale, ambiguous, broken = _prepare(originals, set(args.only))
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
            baseline, _ = _run_files()
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
            target = mutation["target"]
            original, stat = originals[target], stats[target]
            try:
                _write_with_retry(target, mutant)
                os.utime(target, ns=(stat[0], stat[1] + index * 1_000_000_000))
                _clear_bytecode()
                records, failures = _run_files()
                verdict, detail = _judge(mutation, records, failures)
            except subprocess.TimeoutExpired:
                verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
            except RuntimeError as exc:
                verdict, detail = "ERROR", str(exc)
            finally:
                _restore(target, original, stat)
            if any(t.read_bytes() != originals[t] for t in TARGETS):
                say(f"ERROR {name}: a target differs from the original after restore")
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
