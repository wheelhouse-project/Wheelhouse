"""Run WheelHouse pytest suite and print clean summary from JUnit XML.

Usage:
    python scripts/run_tests.py                    # full wheelhouse suite
    python scripts/run_tests.py -k test_shadow     # specific tests
    python scripts/run_tests.py --service installer # different service
    python scripts/run_tests.py --service release   # scripts/release/tests
    python scripts/run_tests.py --split             # full suite, in parts

The script:
1. Runs pytest in the suite directory (default: wheelhouse)
2. Parses the JUnit XML for a reliable summary
3. Prints failures with test names and error messages
4. Returns pytest's exit code

With --split, a suite named in SPLIT_PARTS runs as several pytest processes,
one per fixed part of its test files; every part runs, each part's JUnit
report is summarized, and the exit code is 0 only when every part passed.
"""
import fnmatch
import os
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_SERVICE = "wheelhouse"

# crewcut: the wheelhouse suite dies with 0xC0000374 (heap corruption) at
# about test 13,700 of one pytest process, in 8 of 12 full runs on
# 2026-09-21/22. The damage builds up over the whole run: every half of the
# files before the crash site ran clean, and the cause is still unknown
# (wh-gui-manager-fixture-heap-crash). Running the suite in two processes (7,937
# and 7,515 tests on 2026-09-22) avoids the crash without finding it. Remove the
# entry once that bead's cause is fixed; add a part if one process again
# approaches about 13,000 tests.
SPLIT_PARTS = {"wheelhouse": 2}

# pytest's default norecursedirs. Its directory collector also always skips
# __pycache__.
DEFAULT_NORECURSEDIRS = ("*.egg", ".*", "_darcs", "build", "CVS", "dist",
                         "node_modules", "venv", "{arch}")

# Suites that are not services and therefore do not live under services/.
# wh-ci-release-tests: this runner resolved every name as services/<name>, so
# the 1,786 tests under scripts/release/tests -- helpdoc CI, size budget,
# command generator -- could not be reached through it at all, and published
# doc defects shipped because nothing ran them automatically.
EXTRA_SUITES = {
    "release": Path("scripts") / "release",
}


def resolve_suite_dir(name):
    """The directory that owns the suite called *name*.

    A name in EXTRA_SUITES resolves to its own path; every other name keeps
    the services/<name> convention, so existing invocations are unaffected.
    """
    relative = EXTRA_SUITES.get(name)
    if relative is not None:
        return REPO_ROOT / relative
    return REPO_ROOT / "services" / name


def known_suite_names():
    """Every name this runner can resolve, for the not-found message.

    Services are found by their pyproject.toml at one or two levels under
    services/ -- two because the STT providers are nested. Directories whose
    name starts with a dot are skipped so a virtual environment cannot be
    mistaken for a service.
    """
    names = set(EXTRA_SUITES)
    services_root = REPO_ROOT / "services"
    for pattern in ("*/pyproject.toml", "*/*/pyproject.toml"):
        for found in services_root.glob(pattern):
            relative = found.parent.relative_to(services_root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            names.add(relative.as_posix())
    return sorted(names)


def junit_report_path(pytest_args, service_dir):
    """Follow explicit JUnit options; relative paths belong to pytest's cwd.

    Config/environment-only overrides are not reimplemented here: if they
    redirect output, the unchanged-report guard below declines the summary.
    """
    report = "test-results.xml"
    args = iter(pytest_args)
    for arg in args:
        if arg == "--":
            break
        option, equals, value = arg.partition("=")
        if option in ("--junitxml", "--junit-xml"):
            report = value if equals else next(args, "")
    # Match pytest's expansion before resolving against the child directory.
    path = Path(os.path.expanduser(os.path.expandvars(report)))
    return service_dir / path


def report_signature(path):
    """Observe freshness without deleting a previous run's report."""
    try:
        if not path.is_file():
            return None
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except OSError:
        return None


def parse_results(xml_path):
    """Parse JUnit XML and print summary."""
    try:
        root = ET.parse(xml_path).getroot()
        ts = root.find("testsuite")
        if ts is None:
            print(f"[!] No testsuite element in {xml_path}")
            return

        attrib = ts.attrib
        total = int(attrib.get("tests", 0))
        fail = int(attrib.get("failures", 0))
        err = int(attrib.get("errors", 0))
        skip = int(attrib.get("skipped", 0))
        t = float(attrib.get("time", 0))
        passed = total - fail - err - skip
        minutes = int(t // 60)
        seconds = t % 60

        if minutes:
            time_str = f"{minutes}m {seconds:.0f}s"
        else:
            time_str = f"{t:.1f}s"

        if fail == 0 and err == 0:
            print(f"\n[+] {passed} passed, {skip} skipped ({time_str})")
        else:
            print(f"\n[!] {passed} passed, {fail} failed, {err} errors, {skip} skipped ({time_str})")
            print()
            for tc in root.iter("testcase"):
                for f in tc.iter("failure"):
                    name = f"{tc.attrib.get('classname', '')}::{tc.attrib.get('name', '')}"
                    msg = f.attrib.get("message", "")[:300]
                    print(f"  FAILED: {name}")
                    print(f"    {msg}")
                    print()
                for e in tc.iter("error"):
                    name = f"{tc.attrib.get('classname', '')}::{tc.attrib.get('name', '')}"
                    msg = e.attrib.get("message", "")[:300]
                    print(f"  ERROR: {name}")
                    print(f"    {msg}")
                    print()

    except FileNotFoundError:
        print(f"[!] No test results file at {xml_path}")
        print("    pytest may have been killed before writing results")
    except ET.ParseError as e:
        print(f"[!] Failed to parse {xml_path}: {e}")


def report_counts(xml_path):
    """(passed, failed, errors, skipped) from a JUnit report, or None."""
    try:
        ts = ET.parse(xml_path).getroot().find("testsuite")
    except (OSError, ET.ParseError):
        return None
    if ts is None:
        return None
    total, fail, err, skip = (int(ts.attrib.get(key, 0))
                              for key in ("tests", "failures", "errors", "skipped"))
    return total - fail - err - skip, fail, err, skip


def _as_list(value, default):
    if not value:
        return list(default)
    return value.split() if isinstance(value, str) else list(value)


def partition_test_files(suite_dir, parts):
    """The suite's test files in pytest's collection order, cut into *parts*.

    Files come from the suite's own testpaths, python_files and
    norecursedirs settings, walked the way pytest walks them: entries of each
    directory in name order, files and subdirectories interleaved. The parts
    are contiguous and differ in size by at most one file.
    """
    try:
        data = tomllib.loads((suite_dir / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    patterns = _as_list(ini.get("python_files"), ("test_*.py", "*_test.py"))
    skipped = _as_list(ini.get("norecursedirs"), DEFAULT_NORECURSEDIRS)

    def walk(directory):
        found = []
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            return found
        for entry in entries:
            if entry.is_dir():
                if entry.name == "__pycache__" or any(
                        fnmatch.fnmatch(entry.name, p) for p in skipped):
                    continue
                found.extend(walk(entry))
            elif any(fnmatch.fnmatch(entry.name, p) for p in patterns):
                found.append(entry.relative_to(suite_dir).as_posix())
        return found

    files = []
    for top in _as_list(ini.get("testpaths"), (".",)):
        files.extend(walk(suite_dir / top))
    base, extra = divmod(len(files), parts)
    result, start = [], 0
    for index in range(parts):
        end = start + base + (1 if index < extra else 0)
        result.append(files[start:end])
        start = end
    return result


def names_a_test_path(arg, suite_dir):
    """True when a pytest argument selects tests by path or node id."""
    if arg.startswith("-"):
        return False
    return "::" in arg or (suite_dir / arg).exists()


# crewcut: a hand list of the pytest options whose value is the next argument,
# so "-k tests" is not read as the tests directory. An option missing here
# only makes --split refuse a legal value; add it to the list when that happens.
VALUE_OPTIONS = {"-k", "-m", "-p", "-c", "-o", "-W", "--junitxml", "--junit-xml",
                 "--junit-prefix", "--timeout", "--maxfail", "--deselect",
                 "--ignore", "--ignore-glob", "--rootdir", "--confcutdir",
                 "--log-level", "--durations", "--tb"}

# pytest's exit code when it selected no test.
NO_TESTS_COLLECTED = 5

# crewcut: a hand list of the pytest options that select a subset of tests,
# so one part of a split run may legitimately select nothing. Without one of
# them an empty part fails the run. Add an option here if it wrongly fails.
SELECTION_OPTIONS = ("-k", "-m", "--deselect", "--lf", "--last-failed")


def selects_a_subset(args):
    """True when the pytest arguments carry an option that selects a subset of tests."""
    return any(arg == option or arg.startswith(option + "=")
               or (len(option) == 2 and arg.startswith(option))
               for arg in args for option in SELECTION_OPTIONS)


def named_test_paths(args, suite_dir):
    """The arguments that select tests by path or node id, skipping option values."""
    named, is_value = [], False
    for arg in args:
        if is_value:
            is_value = False
        elif arg in VALUE_OPTIONS:
            is_value = True
        elif names_a_test_path(arg, suite_dir):
            named.append(arg)
    return named


def run_split(service_dir, pytest_args, parts, env):
    """Run the suite as *parts* pytest processes; return the run's exit code.

    Every part runs even after an earlier part fails, so one run reports
    every failure. When the arguments select a subset (-k, -m, ...), a part
    whose selection matched nothing (exit 5 with a fresh report) counts as
    passed, unless every part matched nothing. Any
    other nonzero part exit code fails the run; the first one is returned.
    """
    base = junit_report_path(pytest_args, service_dir)
    if base == service_dir:
        base = service_dir / "test-results.xml"
    exit_code = 0
    empty_parts = 0
    may_be_empty = selects_a_subset(pytest_args)
    totals = [0, 0, 0, 0]
    incomplete = []
    for number, files in enumerate(partition_test_files(service_dir, parts), 1):
        report = base.with_name(f"{base.stem}-part{number}{base.suffix}")
        previous = report_signature(report)
        options = ["--tb=short", "-q", *pytest_args]
        cmd = ["uv", "run", "pytest", *options, *files, f"--junitxml={report}"]
        print(f"\n=== part {number} of {parts}: {len(files)} test files, "
              f"{files[0] if files else '-'} .. {files[-1] if files else '-'} ===",
              flush=True)
        print(f"Running: uv run pytest {' '.join(options)} <{len(files)} files> "
              f"--junitxml={report}", flush=True)
        result = subprocess.run(cmd, cwd=str(service_dir), env=env)
        sys.stdout.flush()
        current = report_signature(report)
        counts = None if current is None or current == previous else report_counts(report)
        if counts is None:
            print(f"[!] part {number}: JUnit report missing, unreadable, or unchanged "
                  f"at {report} (exit code {result.returncode}).")
            incomplete.append(number)
        else:
            parse_results(report)
            totals = [a + b for a, b in zip(totals, counts)]
        if (may_be_empty and result.returncode == NO_TESTS_COLLECTED
                and counts is not None):
            empty_parts += 1  # -k or -m selected nothing in this part's files
        elif result.returncode != 0 and exit_code == 0:
            exit_code = result.returncode
    if empty_parts == parts:
        exit_code = NO_TESTS_COLLECTED
    passed, fail, err, skip = totals
    scope = f"across {parts} parts"
    if incomplete:
        scope += f"; no fresh report from part {', '.join(map(str, incomplete))}"
    if fail == 0 and err == 0 and not incomplete:
        print(f"\n[+] {passed} passed, {skip} skipped {scope}")
    else:
        print(f"\n[!] {passed} passed, {fail} failed, {err} errors, {skip} skipped {scope}")
    return exit_code


def main():
    # Extract --service arg if present, pass everything else to pytest
    service = DEFAULT_SERVICE
    split = False
    pytest_args = []
    skip_next = False
    for i, arg in enumerate(sys.argv[1:]):
        if skip_next:
            skip_next = False
            continue
        if arg == "--split":
            split = True
            continue
        if arg == "--service":
            if i + 1 < len(sys.argv) - 1:
                service = sys.argv[i + 2]
                skip_next = True
            continue
        pytest_args.append(arg)

    service_dir = resolve_suite_dir(service)
    if not (service_dir / "pyproject.toml").exists():
        print(f"[!] Suite '{service}' not found at {service_dir}")
        print(f"    Known suites: {', '.join(known_suite_names())}")
        sys.exit(1)

    # Fresh checkout: config.toml is per-machine and untracked (the installer
    # and CONTRIBUTING.md both create it from the example). Some test modules
    # import application modules that read it at import time, so provision it
    # here the same way before pytest starts.
    example = service_dir / "config.toml.example"
    config = service_dir / "config.toml"
    if example.exists() and not config.exists():
        shutil.copyfile(example, config)
        print(f"[+] Created {config} from config.toml.example (fresh checkout)",
              flush=True)

    env = None
    if sys.platform == "win32" and service == "release":
        # The installer tests launch Windows PowerShell via Python. Unlike a
        # direct pwsh -> powershell launch, this inherits PowerShell 7's module
        # paths unchanged; its Utility module hides WinPS's Get-FileHash.
        # Let each child shell rebuild its own defaults. Keep the reset local
        # to this test run, not the caller or other service suites (wh-tky1h).
        env = {key: value for key, value in os.environ.items()
               if key.casefold() != "psmodulepath"}

    parts = SPLIT_PARTS.get(service, 1) if split else 1
    if parts > 1:
        named = named_test_paths(pytest_args, service_dir)
        if named:
            print(f"[!] --split runs the whole suite in parts, so it cannot take a "
                  f"test path: {', '.join(named)}")
            sys.exit(2)
        print(f"Service: {service_dir}", flush=True)
        sys.exit(run_split(service_dir, pytest_args, parts, env))

    xml_path = junit_report_path(pytest_args, service_dir)
    previous_report = report_signature(xml_path)

    # Run pytest
    cmd = ["uv", "run", "pytest", "--tb=short", "-q"] + pytest_args
    print(f"Running: {' '.join(cmd)}", flush=True)
    print(f"Service: {service_dir}\n", flush=True)

    result = subprocess.run(cmd, cwd=str(service_dir), env=env)

    # Parse and display results from XML (immune to stdout truncation)
    sys.stdout.flush()
    current_report = report_signature(xml_path)
    if current_report is None or current_report == previous_report:
        print(f"[!] JUnit report missing, unreadable, or unchanged at {xml_path}; "
              "skipping summary (no fresh results).")
    else:
        parse_results(xml_path)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
