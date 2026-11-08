"""Mutation gate for the launch start folder (wh-installer-update-folder-locked).

Programs and browsers that Wheelhouse starts for the user must start outside
the app folder. Each mutation below puts one call site, or one step of the
helper in utils/launch_cwd.py, back to the old behavior; the named tests must
fail, and each must fail on its own assertion (the gate prints the failure
line of every catcher so it can be read).

Run from services/wheelhouse with that service's Python:

    .venv/Scripts/python tests/mutation_gate_launch_cwd.py [--check] [--only=name]

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutant that does not compile, a per-mutation timeout, an expected
test that does not exist, and a baseline that is not green. The gate matches
patterns in the file's own line endings, restores each file with write_bytes,
sets PYTHONDONTWRITEBYTECODE=1 and COLUMNS=1000, and clears __pycache__.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
INPUT_PROC = SERVICE / "input_proc.py"
ACTIONS = SERVICE / "speech" / "actions.py"
MAIN = SERVICE / "main.py"
HELPER = SERVICE / "utils" / "launch_cwd.py"
ABOUT = SERVICE / "about_window.py"
HELP = SERVICE / "pattern_help_dialog.py"
CREATE = SERVICE / "create_pattern_dialog.py"
CREATE_TESTS = "tests/test_create_pattern_dialog.py"

CWD_TESTS = "tests/test_launch_cwd.py"
WINDOW_TESTS = "tests/test_input_proc_activate_window.py"
HELP_TESTS = "tests/test_logic_open_help_online.py"

EXE_SITE = (
    "        os.startfile(\n"
    "            store_alias_launch_target(target), cwd=launch_start_folder()\n"
    "        )\n"
)
CAND_SITE = (
    "                os.startfile(\n"
    "                    store_alias_launch_target(candidate.launch_target),\n"
    "                    cwd=launch_start_folder(),\n"
    "                )\n"
)

MUTATIONS = [
    {
        "name": "exe-site-drops-the-start-folder",
        "file": INPUT_PROC, "old": EXE_SITE,
        "new": "        os.startfile(store_alias_launch_target(target))\n",
        "tests": [WINDOW_TESTS],
        "expect": ["test_a_bare_exe_name_starts_in_the_profile_folder"],
    },
    {
        "name": "program-site-drops-the-start-folder",
        "file": INPUT_PROC, "old": CAND_SITE,
        "new": "                os.startfile(store_alias_launch_target(candidate.launch_target))\n",
        "tests": [WINDOW_TESTS],
        "expect": ["test_an_installed_program_starts_in_the_profile_folder"],
    },
    {
        "name": "run-program-drops-the-start-folder",
        "file": ACTIONS, "old": "                cwd=launch_start_folder(),\n            )\n        except Exception as e: logger.error(\"run_program",
        "new": "            )\n        except Exception as e: logger.error(\"run_program",
        "tests": [CWD_TESTS],
        "expect": ["test_run_program"],
    },
    {
        "name": "actions-module-stops-registering-the-browser",
        "file": ACTIONS,
        "old": "logger = logging.getLogger(__name__)\n\n# wh-installer-update-folder-locked: webbrowser.open starts the browser\n# outside the app folder from here on.\nensure_browser_start_folder()\n",
        "new": "logger = logging.getLogger(__name__)\n",
        "tests": [CWD_TESTS],
        "expect": ["test_importing_the_actions_module_registers_the_browser"],
    },
    {
        "name": "help-command-does-not-register-the-browser",
        "file": MAIN,
        "old": "            ensure_browser_start_folder()\n            opened = await",
        "new": "            opened = await",
        "tests": [HELP_TESTS],
        "expect": ["test_the_browser_gets_a_start_folder_outside_the_app_folder"],
    },
    {
        "name": "about-link-bypasses-the-helper",
        "file": ABOUT, "old": "open_in_browser(WEBSITE_URL)",
        "new": "__import__('webbrowser').open(WEBSITE_URL)",
        "tests": [CWD_TESTS],
        "expect": ["test_about_window_website_link"],
    },
    {
        "name": "pattern-help-link-bypasses-the-helper",
        "file": HELP, "old": "open_in_browser(url.toString())",
        "new": "__import__('webbrowser').open(url.toString())",
        "tests": [CWD_TESTS],
        "expect": ["test_pattern_help_external_link"],
    },
    {
        "name": "open-in-browser-does-not-register",
        "file": HELPER,
        "old": "    ensure_browser_start_folder()\n    return webbrowser.open(url)",
        "new": "    return webbrowser.open(url)",
        "tests": [CWD_TESTS],
        "expect": ["test_about_window_website_link", "test_pattern_help_external_link"],
    },
    {
        "name": "browser-drops-the-start-folder",
        "file": HELPER, "old": "os.startfile(url, cwd=launch_start_folder())",
        "new": "os.startfile(url)",
        "tests": [CWD_TESTS],
        "expect": ["test_opening_a_url_starts_it_outside_the_app_folder"],
    },
    {
        "name": "browser-reports-success-on-refusal",
        "file": HELPER, "old": "        except OSError:\n            return False\n        return True",
        "new": "        except OSError:\n            return True\n        return True",
        "tests": [CWD_TESTS],
        "expect": ["test_a_shell_refusal_returns_false_like_the_default_browser"],
    },
    {
        "name": "browser-is-registered-last",
        "file": HELPER, "old": "OutsideAppBrowser(), preferred=True)",
        "new": "OutsideAppBrowser(), preferred=False)",
        "tests": [CWD_TESTS],
        "expect": ["test_opening_a_url_starts_it_outside_the_app_folder"],
    },
    {
        "name": "browser-is-registered-twice",
        "file": HELPER, "old": "        if _BROWSER_NAME in webbrowser._browsers:\n            return\n",
        "new": "        if False:\n            return\n",
        "tests": [CWD_TESTS],
        "expect": ["test_registering_twice_adds_one_browser"],
    },
    {
        "name": "start-folder-is-the-process-folder",
        "file": HELPER, "old": "    for candidate in (os.path.expanduser(\"~\"), tempfile.gettempdir()):",
        "new": "    for candidate in (os.getcwd(), tempfile.gettempdir()):",
        "tests": [CWD_TESTS],
        "expect": ["test_it_is_the_user_profile_folder"],
    },
    {
        "name": "start-folder-skips-the-existence-check",
        "file": HELPER, "old": "            if candidate and os.path.isdir(candidate):",
        "new": "            if candidate:",
        "tests": [CWD_TESTS],
        "expect": ["test_a_missing_profile_folder_falls_back_to_an_existing_one"],
    },
    {
        "name": "regex-link-opens-through-qt",
        "file": CREATE,
        "old": "        self._regex_link_label.setOpenExternalLinks(False)\n        self._regex_link_label.linkActivated.connect(self._open_regex_link)\n",
        "new": "        self._regex_link_label.setOpenExternalLinks(True)\n",
        "tests": [CREATE_TESTS],
        "expect": ["test_regex101_link_uses_pinned_url", "test_regex101_link_opens_through_the_start_folder_helper"],
    },
]


def run_pytest(tests, expr=None, collect=False):
    cmd = [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:randomly",
           "-p", "no:cacheprovider", "--tb=line"]
    if collect:
        cmd.append("--collect-only")
    if expr:
        cmd += ["-k", expr]
    return subprocess.run(
        cmd, cwd=SERVICE, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "1000"},
    )


def clear_bytecode():
    for p in (INPUT_PROC, ACTIONS, MAIN, HELPER, ABOUT, HELP, CREATE):
        shutil.rmtree(p.parent / "__pycache__", ignore_errors=True)


def translate(text, raw):
    return text.replace("\n", "\r\n") if b"\r\n" in raw else text


def apply(mut):
    raw = mut["file"].read_bytes()
    src = raw.decode("utf-8")
    old, new = translate(mut["old"], raw), translate(mut["new"], raw)
    count = src.count(old)
    if count != 1:
        return raw, None, f"pattern matched {count} times"
    mutated = src.replace(old, new, 1)
    try:
        compile(mutated, str(mut["file"]), "exec")
    except SyntaxError as e:
        return raw, None, f"does not compile: {e}"
    return raw, mutated.encode("utf-8"), None


def failed_lines(out, names):
    return [ln for ln in out.splitlines()
            if any(n in ln for n in names) and ("FAILED" in ln or "Error" in ln)]


def main():
    check = "--check" in sys.argv
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
    todo = [m for m in MUTATIONS if not only or m["name"] in only]
    print(f"scope: running {len(todo)} of {len(MUTATIONS)} mutations")
    errors = 0
    for m in todo:
        _, mutated, err = apply(m)
        if err:
            print(f"ERROR {m['name']}: {err}")
            errors += 1
    if errors or check:
        print(f"checked {len(todo)} patterns, {errors} errors")
        return 1 if errors else 0
    all_tests = sorted({t for m in todo for t in m["tests"]})
    names = set()
    for line in run_pytest(all_tests, collect=True).stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split("[")[0].strip())
    for m in todo:
        for n in m["expect"]:
            if n not in names:
                print(f"ERROR {m['name']}: test {n} does not exist")
                return 1
    base = run_pytest(all_tests)
    if base.returncode != 0:
        print("ERROR: baseline is not green")
        print(base.stdout[-2000:])
        return 1
    survivors = 0
    for m in todo:
        raw, mutated, _ = apply(m)
        status = ""
        try:
            m["file"].write_bytes(mutated)
            clear_bytecode()
            try:
                res = run_pytest(m["tests"])
            except subprocess.TimeoutExpired:
                status = "ERROR timeout"
                errors += 1
                res = None
        finally:
            m["file"].write_bytes(raw)
            clear_bytecode()
        if res is not None:
            lines = failed_lines(res.stdout, m["expect"])
            caught = [n for n in m["expect"] if any(n in ln for ln in lines)]
            if res.returncode == 0:
                status = "SURVIVED"
                survivors += 1
            elif len(caught) != len(m["expect"]):
                status = f"UNEXPECTED (caught {caught})"
                survivors += 1
            else:
                status = "caught"
            for ln in lines:
                print("    " + ln[-220:])
        print(f"{status}: {m['name']}")
    print(f"done: {len(todo)} run, {survivors} survivors, {errors} errors")
    return 1 if survivors or errors else 0


if __name__ == "__main__":
    sys.exit(main())
