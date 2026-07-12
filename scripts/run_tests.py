"""Run WheelHouse pytest suite and print clean summary from JUnit XML.

Usage:
    python scripts/run_tests.py                    # full wheelhouse suite
    python scripts/run_tests.py -k test_shadow     # specific tests
    python scripts/run_tests.py --service installer # different service

The script:
1. Runs pytest in the service directory (default: wheelhouse)
2. Parses the JUnit XML for a reliable summary
3. Prints failures with test names and error messages
4. Returns pytest's exit code
"""
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_SERVICE = "wheelhouse"


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

    service_dir = REPO_ROOT / "services" / service
    if not (service_dir / "pyproject.toml").exists():
        print(f"[!] Service '{service}' not found at {service_dir}")
        sys.exit(1)

    xml_path = service_dir / "test-results.xml"

    # Run pytest
    cmd = ["uv", "run", "pytest", "--tb=short", "-q"] + pytest_args
    print(f"Running: {' '.join(cmd)}", flush=True)
    print(f"Service: {service_dir}\n", flush=True)

    result = subprocess.run(cmd, cwd=str(service_dir))

    # Parse and display results from XML (immune to stdout truncation)
    sys.stdout.flush()
    parse_results(xml_path)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
