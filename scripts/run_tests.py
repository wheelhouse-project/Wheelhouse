"""Run WheelHouse pytest suite and print clean summary from JUnit XML.

Usage:
    python scripts/run_tests.py                    # full wheelhouse suite
    python scripts/run_tests.py -k test_shadow     # specific tests
    python scripts/run_tests.py --service installer # different service
    python scripts/run_tests.py --service release   # scripts/release/tests

The script:
1. Runs pytest in the suite directory (default: wheelhouse)
2. Parses the JUnit XML for a reliable summary
3. Prints failures with test names and error messages
4. Returns pytest's exit code
"""
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_SERVICE = "wheelhouse"

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


def main():
    # Extract --service arg if present, pass everything else to pytest
    service = DEFAULT_SERVICE
    pytest_args = []
    skip_next = False
    for i, arg in enumerate(sys.argv[1:]):
        if skip_next:
            skip_next = False
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

    xml_path = junit_report_path(pytest_args, service_dir)
    previous_report = report_signature(xml_path)

    # Run pytest
    cmd = ["uv", "run", "pytest", "--tb=short", "-q"] + pytest_args
    print(f"Running: {' '.join(cmd)}", flush=True)
    print(f"Service: {service_dir}\n", flush=True)

    env = None
    if sys.platform == "win32" and service == "release":
        # The installer tests launch Windows PowerShell via Python. Unlike a
        # direct pwsh -> powershell launch, this inherits PowerShell 7's module
        # paths unchanged; its Utility module hides WinPS's Get-FileHash.
        # Let each child shell rebuild its own defaults. Keep the reset local
        # to this test run, not the caller or other service suites (wh-tky1h).
        env = {key: value for key, value in os.environ.items()
               if key.casefold() != "psmodulepath"}
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
