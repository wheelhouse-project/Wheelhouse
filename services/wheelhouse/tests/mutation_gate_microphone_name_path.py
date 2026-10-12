"""Mutation gate for the microphone name path (wh-show-microphone-name.4).

WHY THIS GATE EXISTS. The microphone's name travels from the speech
provider process to the graphical user interface process through five
links. Each link has tests, but a test can pass for the wrong reason, so
each link is broken here at the level of the decision the code makes, one
place at a time, and the gate confirms a named test goes red at its own
assertion. A name that never arrives shows "Listening on: no microphone"
to a user whose microphone works, so every link matters.

THE FIVE LINKS AND WHAT THE GATE BREAKS IN EACH:

  1. The capture's own report (shared_audio/capture/winrt_capture.py).
     The first graph reports ('opened', device); stop() reports
     ('closed', None). Broken: the opened report loses the device, the
     opened report is never made, the closed report is never made, the
     fallback to the Windows default capture device's name is removed.
  2. The provider relay (shared_stt/mic_notice.py). The notifier turns
     the four capture events into send_microphone calls. Broken: opened
     sends "", closed and lost keep the old name, and the recovered name
     is held back by the loss flag.
  3. The sender (shared_stt/ws_forwarder.py). The queued frame is rebuilt
     key by key in the sender loop, and the last name is repeated on every
     (re)connect. Broken: the sender-loop payload key, the sender-loop
     branch itself, the connect-time repeat's key, the remembered name,
     and the empty-name test (an empty name is a name).
  4. The receive side (integrations/websocket_manager.py). Broken: the
     read of the frame's device key, the active-client gate, the dispatch
     of the frame, and the clearing when the active client leaves.
  5. The store and the interface. state_manager.py: the state-update key
     and the update itself. gui.py: the GuiManager read of the key, both
     strings of the "Listening on" entry (the no-microphone wording and
     the named form) and the doubled ampersand of the window menu.

TWO SERVICES, TWO INTERPRETERS. The Logic process (services/wheelhouse,
which also holds the graphical user interface code) and the shared STT
package (services/stt_providers/shared) have their own virtual
environments. Each suite runs under its own interpreter, from its own
directory in THIS tree, which is what puts this tree's code first on
sys.path. The default interpreter is the suite's own .venv; an
environment variable overrides it:

    MICNAME_GATE_PYTHON_LOGIC, MICNAME_GATE_PYTHON_SHARED

(BOOST_GATE_PYTHON_LOGIC and BOOST_GATE_PYTHON_SHARED are accepted too, so
one set of overrides serves both gates.) A git worktree has no .venv for
either service; point the variables at the main checkout's interpreters.

Before the baseline, the gate resolves every target module under its
suite's interpreter and refuses to start unless the module file is the
target in THIS tree. A worktree borrowing the main checkout's interpreter
would otherwise test the main checkout's code and report every mutation as
a survivor.

THE TEST SET is the test files of the five links that the gate can run on
this host. The shared test_ws_forwarder.py is kept out: its port binds
fail on host-reserved port ranges (WinError 10013). It holds
test_send_microphone_reaches_the_wire, which pins the same payload key
over a real socket; the queued-name tests added to
test_ws_forwarder_capabilities.py pin it over a fake one, so every
mutation names at least one expected test from a file that runs here.

READING A MUTANT RUN. A mutant runs the whole selection WITHOUT -x, so
the summary lists every test it broke. The mutation counts as caught only
when the exit code is 1, there is no ERROR record (a fixture or setup
error), and at least one test NAMED in the mutation's expected list
failed with an assertion. That is the proof that the named test itself
goes red. Other failing tests are counted in the message and never make a
catch. A collection or import error (exit code 2 or more), the
pytest-timeout abort banner, an expected test that fails on something
that is not an assertion (for example an IndexError from indexing a frame
the mutant no longer sends), and a run in which no expected test failed
are all errors, never catches. The message names the expected tests that
did not fail, so a reader can see which test did the work.

DISCIPLINE, per the mutation-gate skill: byte IO and atomic writes;
patterns translated to each target's own line ending; exactly one match
at a line start; each mutant compiled before it runs (and in --check);
every expected test name validated against real collection; a green
baseline per suite; __pycache__ cleared and PYTHONDONTWRITEBYTECODE set;
a per-run timeout; the pytest-timeout abort banner reported as an error;
COLUMNS=1000 so the short-summary reason is kept; verdicts read only
from the short-summary section, keyed on the exit code first; each catch
confirmed as an assertion; restore in a finally that holds a Ctrl+C and
refuses to overwrite a concurrent edit; a final byte check of every
target. A mutation that removes a block's only statement puts `pass` in
its place so the mutant still compiles.

Run it from services/wheelhouse. NEVER while a test suite is running in
the same tree -- this file rewrites the sources that suite imports. Send
the output to a file and read the exit code of the gate itself, never of
a pipe:

    <interpreter> tests/mutation_gate_microphone_name_path.py > gate.log 2>&1
    <interpreter> tests/mutation_gate_microphone_name_path.py --check
    <interpreter> tests/mutation_gate_microphone_name_path.py <name> ...
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

WH_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = WH_DIR.parents[1]
PROVIDERS = REPO_ROOT / "services" / "stt_providers"


def _venv_python(service_dir):
    return service_dir / ".venv" / "Scripts" / "python.exe"


# name -> directory, interpreter variables, test files, sys.path entries
# for the provenance probe (the ones the suite's conftest or tests insert).
SUITES = {
    "logic": {
        "dir": WH_DIR,
        "env": ("MICNAME_GATE_PYTHON_LOGIC", "BOOST_GATE_PYTHON_LOGIC"),
        "tests": ("tests/test_state_manager.py",
                  "tests/test_websocket_manager.py",
                  "tests/test_gui.py"),
        "path": (REPO_ROOT, WH_DIR),
    },
    "shared": {
        "dir": PROVIDERS / "shared",
        "env": ("MICNAME_GATE_PYTHON_SHARED", "BOOST_GATE_PYTHON_SHARED"),
        "tests": ("tests/test_ws_forwarder_capabilities.py",
                  "tests/test_mic_notice.py",
                  "tests/test_winrt_capture.py"),
        "path": (PROVIDERS / "shared",),
    },
}


def _python(suite):
    for variable in SUITES[suite]["env"]:
        override = os.environ.get(variable)
        if override:
            return Path(override)
    return _venv_python(SUITES[suite]["dir"])


CAPTURE = PROVIDERS / "shared" / "shared_audio" / "capture" / "winrt_capture.py"
NOTICE = PROVIDERS / "shared" / "shared_stt" / "mic_notice.py"
FORWARDER = PROVIDERS / "shared" / "shared_stt" / "ws_forwarder.py"
MANAGER = WH_DIR / "integrations" / "websocket_manager.py"
STATE = WH_DIR / "state_manager.py"
GUI = WH_DIR / "gui.py"

# Target file -> (suite, importable module name for the provenance probe).
TARGETS = {
    CAPTURE: ("shared", "shared_audio.capture.winrt_capture"),
    NOTICE: ("shared", "shared_stt.mic_notice"),
    FORWARDER: ("shared", "shared_stt.ws_forwarder"),
    MANAGER: ("logic", "integrations.websocket_manager"),
    STATE: ("logic", "state_manager"),
    GUI: ("logic", "gui"),
}


def _run_suites(mutation):
    """The suite that holds the expected tests."""
    return [mutation["suite"]]


def _probes():
    """(suite, target, module) for every import the provenance check
    makes."""
    return sorted(((suite, target, module)
                   for target, (suite, module) in TARGETS.items()),
                  key=lambda p: (p[0], str(p[1])))


# Both selections run in about 30 seconds clean (584 and 167+2 tests
# measured 2026-10-10). This limit only turns a hang into a reported
# error rather than a lost run.
RUN_TIMEOUT_S = 600
TIMEOUT_BANNER = "+++ Timeout +++"
SUMMARY_HEADER = "short test summary info"
ASSERTION_REASONS = ("assert", "AssertionError", "Failed", "DID NOT RAISE")


def _t(cls, name=None):
    return f"{cls}::{name}" if name else cls


# Shared test classes and bare functions.
_NOTICE = "TestTheMicrophoneReport"
_CAPTURE = "TestMicrophoneNameReport"
# Logic test classes.
_FRAME = "TestMicrophoneFrame"
_STATE = "TestMicrophoneName"
_QUEUES = "TestCheckQueuesAndEvents"
_MENUS = "TestListeningOnEntryOnBothMenus"


def _m(name, suite, target, old, new, expect):
    return {"name": name, "suite": suite, "target": target,
            "old": old, "new": new, "expect": expect}


MUTATIONS = [
    # ------------------------------------------------------------------
    # 1. The capture's own report of the opened device name
    # ------------------------------------------------------------------
    _m("capture-opened-report-loses-the-device", "shared", CAPTURE,
       "                self._report_outage('opened', _device_name(owned[1]))\n",
       "                self._report_outage('opened', None)\n",
       [_t(_CAPTURE, "test_the_first_graph_reports_its_device_as_opened")]),
    _m("capture-never-reports-opened", "shared", CAPTURE,
       "                self._report_outage('opened', _device_name(owned[1]))\n",
       "                pass\n",
       [_t(_CAPTURE, "test_the_first_graph_reports_its_device_as_opened")]),
    # A node that carries no name falls back to the Windows default capture
    # device's name (acceptance 7). Removing the fallback must break the
    # opened report and the recovery report.
    _m("capture-name-fallback-removed", "shared", CAPTURE,
       "    return _default_capture_device_name()\n",
       "    return None\n",
       [_t(_CAPTURE, "test_a_node_with_no_device_is_opened_with_the_default_name"),
        _t(_CAPTURE,
           "test_a_node_with_an_empty_name_is_opened_with_the_default_name"),
        _t(_CAPTURE,
           "test_the_recovery_notice_and_log_line_use_the_same_name")]),
    _m("capture-stop-never-reports-closed", "shared", CAPTURE,
       "        self._report_outage('closed', None)\n",
       "        pass\n",
       [_t(_CAPTURE, "test_stopping_reports_closed_once"),
        _t(_CAPTURE,
           "test_the_stop_and_restart_clears_the_name_then_sets_the_new_one")]),
    # ------------------------------------------------------------------
    # 2. The provider relay: capture event -> send_microphone
    # ------------------------------------------------------------------
    _m("notifier-opened-sends-no-name", "shared", NOTICE,
       "        if event == OUTAGE_OPENED:\n"
       "            self._report_microphone(device or \"\")\n",
       "        if event == OUTAGE_OPENED:\n"
       "            self._report_microphone(\"\")\n",
       [_t(_NOTICE, "test_opened_sends_the_device_name")]),
    _m("notifier-closed-keeps-the-name", "shared", NOTICE,
       "        elif event == OUTAGE_CLOSED:\n"
       "            self._report_microphone(\"\")\n",
       "        elif event == OUTAGE_CLOSED:\n"
       "            pass\n",
       [_t(_NOTICE, "test_closed_sends_an_empty_name")]),
    _m("notifier-lost-keeps-the-name", "shared", NOTICE,
       "        elif event == OUTAGE_LOST:\n"
       "            self._report_microphone(\"\")\n",
       "        elif event == OUTAGE_LOST:\n"
       "            pass\n",
       [_t(_NOTICE,
           "test_lost_sends_an_empty_name_and_still_sends_its_notice")]),
    _m("notifier-recovered-name-waits-for-the-loss-flag", "shared", NOTICE,
       "        elif event == OUTAGE_RECOVERED:\n"
       "            self._report_microphone(device or \"\")\n"
       "            if not self._loss_announced:\n"
       "                return\n",
       "        elif event == OUTAGE_RECOVERED:\n"
       "            if not self._loss_announced:\n"
       "                return\n"
       "            self._report_microphone(device or \"\")\n",
       [_t(_NOTICE, "test_the_name_goes_out_when_the_loss_was_never_announced")]),
    # ------------------------------------------------------------------
    # 3. The sender: queued frame and connect-time repeat
    # ------------------------------------------------------------------
    _m("sender-loop-payload-drops-the-device-key", "shared", FORWARDER,
       "                                    \"device\": msg_data.get(\"device\", \"\"),\n",
       "",
       [_t("test_a_queued_microphone_name_reaches_the_wire"),
        _t("test_a_queued_empty_microphone_name_reaches_the_wire")]),
    _m("sender-loop-has-no-microphone-branch", "shared", FORWARDER,
       "                            elif msg_type == \"microphone\":\n",
       "                            elif msg_type == \"microphone_\":\n",
       [_t("test_a_queued_microphone_name_reaches_the_wire"),
        _t("test_a_queued_empty_microphone_name_reaches_the_wire")]),
    _m("connect-repeat-drops-the-device-key", "shared", FORWARDER,
       "                                \"device\": microphone_device,\n",
       "",
       [_t("test_microphone_name_is_resent_on_every_connect"),
        _t("test_empty_microphone_name_is_resent_on_connect")]),
    _m("send-microphone-forgets-the-name", "shared", FORWARDER,
       "        self._microphone_device = device\n",
       "        pass\n",
       # The older connect tests index the frame that is no longer sent
       # and die on an IndexError, which is not an assertion, so they are
       # not named; the queued test asserts the frame types first.
       [_t("test_a_queued_microphone_name_reaches_the_wire")]),
    _m("connect-repeat-skips-an-empty-name", "shared", FORWARDER,
       "                    if capabilities_sent and microphone_device is not None:\n",
       "                    if capabilities_sent and microphone_device:\n",
       # Same reason: the older empty-name test indexes the missing frame.
       [_t("test_a_queued_empty_microphone_name_reaches_the_wire")]),
    # ------------------------------------------------------------------
    # 4. The receive side in WheelHouse
    # ------------------------------------------------------------------
    _m("receive-reads-the-wrong-key", "logic", MANAGER,
       "        device = data.get(\"device\")\n",
       "        device = data.get(\"name\")\n",
       [_t(_FRAME, "test_an_active_clients_frame_stores_the_name")]),
    _m("receive-accepts-a-non-active-client", "logic", MANAGER,
       "        if websocket is not self._active_stt_client:\n"
       "            logger.debug(\n"
       "                \"[MICROPHONE] ignored",
       "        if False:\n"
       "            logger.debug(\n"
       "                \"[MICROPHONE] ignored",
       [_t(_FRAME, "test_a_frame_from_a_non_active_client_is_ignored")]),
    _m("receive-never-dispatches-the-frame", "logic", MANAGER,
       "                    if msg_type == \"microphone\":\n"
       "                        self._apply_microphone(websocket, data)\n",
       "                    if msg_type == \"microphone\":\n"
       "                        pass\n",
       [_t(_FRAME, "test_an_active_clients_frame_stores_the_name")]),
    _m("active-client-leaving-keeps-the-name", "logic", MANAGER,
       "                    self.state_manager.set_microphone_name(\"\")\n",
       "                    pass\n",
       [_t(_FRAME, "test_removing_the_active_client_clears_the_name"),
        _t(_FRAME,
           "test_removing_the_active_client_clears_it_with_another_left")]),
    # ------------------------------------------------------------------
    # 5. The store and the interface
    # ------------------------------------------------------------------
    _m("state-update-key-renamed", "logic", STATE,
       "                'microphone_name': self._microphone_name,\n",
       "                'microphone': self._microphone_name,\n",
       # test_includes_expected_keys pins the key set by assertion. The
       # TestMicrophoneName tests read state["microphone_name"] and die on
       # a KeyError, which is not an assertion, so they are not named.
       [_t("TestSendStateUpdate", "test_includes_expected_keys")]),
    _m("setting-the-name-sends-no-state-update", "logic", STATE,
       "        self._microphone_name = name if isinstance(name, str) else \"\"\n"
       "        self.send_state_update()\n",
       "        self._microphone_name = name if isinstance(name, str) else \"\"\n"
       "        pass\n",
       [_t(_STATE, "test_setting_the_name_sends_a_state_update_carrying_it"),
        _t(_STATE,
           "test_clearing_the_name_sends_a_state_update_without_it")]),
    _m("gui-reads-the-wrong-key", "logic", GUI,
       "                    self.microphone_name = message.get('microphone_name', '')\n",
       "                    self.microphone_name = message.get('microphone', '')\n",
       [_t(_QUEUES, "test_a_state_update_stores_the_microphone_name")]),
    _m("no-microphone-wording-changed", "logic", GUI,
       "            else \"Listening on: no microphone\"\n",
       "            else \"Listening on: none\"\n",
       [_t(_MENUS, "test_tray_entry_says_no_microphone_when_no_name_is_stored"),
        _t(_MENUS,
           "test_window_entry_says_no_microphone_when_no_name_is_stored")]),
    _m("named-label-drops-the-name", "logic", GUI,
       "            f\"Listening on: {self.microphone_name}\"\n",
       "            \"Listening on: \"\n",
       [_t(_MENUS, "test_tray_entry_names_the_microphone"),
        _t(_MENUS, "test_window_entry_names_the_microphone")]),
    # One doubling serves both menus, because Win32 and Qt both read "&"
    # as the keyboard-shortcut marker (BOSS RULING 11:56, 2026-10-10). So
    # one mutation names both tests: removing the single call must break
    # the tray entry and the window entry together.
    _m("both-menus-lose-the-doubled-ampersand", "logic", GUI,
       "        ).replace(\"&\", \"&&\")\n",
       "        )\n",
       [_t(_MENUS,
           "test_the_window_entry_doubles_an_ampersand_in_a_device_name"),
        _t(_MENUS,
           "test_the_tray_entry_doubles_an_ampersand_in_a_device_name")]),
]


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------


def _endings(originals):
    """Each target's own line ending. The patterns are translated to it;
    the file is never normalised."""
    return {t: ("\r\n" if b"\r\n" in raw else "\n")
            for t, raw in originals.items()}


def _translate(text, ending):
    return text if ending == "\n" else text.replace("\n", ending)


def _apply(mutation, sources, endings):
    """Return (mutated source text, error or None)."""
    target = mutation["target"]
    source = sources[target]
    old = _translate(mutation["old"], endings[target])
    new = _translate(mutation["new"], endings[target])
    count = source.count(old)
    if count == 0:
        return None, f"{mutation['name']}: pattern not found in {target.name}"
    if count > 1:
        return None, (f"{mutation['name']}: pattern ambiguous in "
                      f"{target.name}, matched {count} times, need 1")
    idx = source.index(old)
    line_start = source.rfind("\n", 0, idx) + 1
    before = source[line_start:idx]
    if before and not before.strip():
        return None, (f"{mutation['name']}: pattern matches {len(before)} "
                      "characters into a deeper indent, not at a line start")
    mutated = source.replace(old, new, 1)
    if mutated == source:
        return None, f"{mutation['name']}: mutation changes nothing"
    try:
        compile(mutated, str(target), "exec")
    except SyntaxError as exc:
        return None, f"{mutation['name']}: mutant does not compile: {exc}"
    return mutated, None


def _check(mutations, sources, endings):
    stale, broken = [], []
    for mutation in mutations:
        _, error = _apply(mutation, sources, endings)
        if error is None:
            print(f"ok {mutation['name']}")
        elif "does not compile" in error:
            broken.append(error)
        else:
            stale.append(error)
    for line in stale + broken:
        print(f"ERROR {line}")
    print(f"checked {len(mutations)} patterns of {len(MUTATIONS)}, "
          f"{len(stale)} stale, {len(broken)} that do not compile")
    return 1 if stale or broken else 0


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "1000"
    return env


def _pytest(suite, args, stop_first=False):
    spec = SUITES[suite]
    first = ["-x"] if stop_first else []
    return subprocess.run(
        [str(_python(suite)), "-m", "pytest", *first, *args,
         "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=spec["dir"], env=_env(), capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _provenance(suites):
    """Import each target under its suite's interpreter; every module file
    must be the target in this tree. Return a list of problems."""
    problems = []
    for suite, target, module in _probes():
        if suite not in suites:
            continue
        paths = [str(p) for p in SUITES[suite]["path"]]
        code = ("import sys, importlib; sys.path[:0] = " + repr(paths)
                + "; print(importlib.import_module(" + repr(module)
                + ").__file__)")
        run = subprocess.run(
            [str(_python(suite)), "-c", code], cwd=SUITES[suite]["dir"],
            env=_env(), capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
        found = run.stdout.strip().splitlines()[-1:] or [""]
        if run.returncode != 0 or Path(found[0]).resolve() != target.resolve():
            problems.append(
                f"{suite}: {module} imports from {found[0]!r}, not {target} "
                f"(exit {run.returncode}) {run.stderr[-400:]}")
        else:
            print(f"provenance {suite}: {module} -> {found[0]}")
    return problems


def _node_key(node_id):
    """``Class::method`` or ``function``, parameters cut."""
    return node_id.split("::", 1)[1].split("[")[0].strip()


def _collect(suite):
    run = _pytest(suite, [*SUITES[suite]["tests"], "--collect-only", "-q"])
    if run.returncode != 0:
        return set(), (f"cannot collect {suite}: {run.stdout[-800:]}"
                       f"{run.stderr[-800:]}")
    return {_node_key(line.strip())
            for line in run.stdout.splitlines() if "::" in line}, None


def _summary_section(output):
    lines = output.splitlines()
    start = None
    for index, line in enumerate(lines):
        if SUMMARY_HEADER in line and line.startswith("="):
            start = index + 1
    return None if start is None else lines[start:]


def _clear_pycache():
    """Remove every __pycache__ under the suites, .venv excluded; return
    (error or None, held interrupt)."""
    interrupt, leftovers = None, []
    for _pass in (1, 2):
        leftovers = []
        try:
            for spec in SUITES.values():
                for d in spec["dir"].rglob("__pycache__"):
                    if ".venv" in d.parts:
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    if d.exists():
                        leftovers.append(d)
        except KeyboardInterrupt as stop:
            interrupt = interrupt or stop
            continue
        if not leftovers:
            return None, interrupt
    return (f"could not remove __pycache__ (a MUTANT .pyc MAY REMAIN): "
            f"{', '.join(str(d) for d in leftovers)}"), interrupt


def _atomic_write(target, raw):
    """Write the whole bytes through a pid-named neighbour and os.replace,
    retrying a PermissionError a few times (a language server or the last
    pytest can hold the file for a moment on Windows)."""
    tmp = target.with_name(f"{target.name}.mutation-gate.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(raw)
        for attempt in range(10):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.5)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            print(f"ERROR could not remove {tmp}: {exc}")


def _restore(target, original, mutated):
    """Put the pre-run bytes back; return (error or None, held interrupt).
    Never overwrite bytes this run did not write."""
    interrupt = None
    for _attempt in (1, 2):
        try:
            current = target.read_bytes()
            if current == original:
                return None, interrupt
            if current != mutated:
                return (f"{target} changed while pytest ran; refusing to "
                        "overwrite the concurrent edit -- reconcile by hand"
                        ), interrupt
            _atomic_write(target, original)
            return None, interrupt
        except KeyboardInterrupt as stop:
            interrupt = interrupt or stop
            continue
        except OSError as exc:
            return (f"restore failed; the MUTANT REMAINS in {target}: {exc}"
                    ), interrupt
    return (f"interrupted during every restore attempt; the MUTANT MAY "
            f"REMAIN in {target}"), interrupt


def _final_mismatches(originals):
    problems = []
    for target, raw in sorted(originals.items()):
        try:
            if target.read_bytes() != raw:
                problems.append(f"{target} does not hold its pre-run bytes "
                                "at the end of the sweep; check it by hand")
        except OSError as exc:
            problems.append(f"cannot read {target} at the end: {exc}")
        leftover = target.with_name(
            f"{target.name}.mutation-gate.{os.getpid()}.tmp")
        if leftover.exists():
            problems.append(f"temporary file left behind: {leftover}")
    return problems


def _failure_records(summary):
    """Every FAILED or ERROR record of a run, as (kind, node, reason)."""
    records = []
    for line in summary:
        for kind in ("FAILED ", "ERROR "):
            if not line.startswith(kind):
                continue
            rest = line[len(kind):]
            node = rest.split(" ", 1)[0]
            reason = rest.split(" - ", 1)[1].strip() if " - " in rest else ""
            records.append((kind.strip(), node, reason))
            break
    return records


def _verdict(mutation, suite, result):
    """Read one run that did not pass. Return ("caught" | "error",
    message).

    The run is NOT stopped at the first failure, so the summary lists every
    test the mutant broke. A catch needs exit code 1, no ERROR record
    (a fixture or setup error), and at least one test NAMED in the
    mutation's expected list failing with an assertion. That is the
    acceptance bar: the named test itself goes red at its own assertion.
    Other failing tests are reported by count; they never make a catch.
    Anything else is an error, never a catch: a collection or import
    error (exit code 2 or more), the timeout banner, an expected test that
    fails on something that is not an assertion, and a run in which no
    expected test failed (the mutation is real but its expected list is
    wrong, or the tests do not guard it)."""
    name = mutation["name"]
    combined = result.stdout + result.stderr
    if TIMEOUT_BANNER in combined:
        return "error", f"{name}: suite-timeout-abort in {suite}, no verdict"
    if result.returncode != 1:
        return "error", (f"{name}: pytest exited {result.returncode} in "
                         f"{suite}, neither a pass nor a test failure: "
                         f"{combined[-600:]!r}")
    summary = _summary_section(combined)
    if summary is None:
        return "error", f"{name}: failure in {suite} but no short summary"
    records = _failure_records(summary)
    errors = [r for r in records if r[0] != "FAILED"]
    if errors:
        return "error", (f"{name}: {len(errors)} ERROR record(s) in {suite}, "
                         f"first {errors[0][1]}: {errors[0][2]!r}")
    expected = set(mutation["expect"])
    hits, soft, others = [], [], []
    for _kind, node, reason in records:
        key = _node_key(node) if "::" in node else node
        is_assertion = bool(reason) and reason.startswith(ASSERTION_REASONS)
        if key in expected:
            (hits if is_assertion else soft).append((key, node, reason))
        else:
            others.append(node)
    if soft:
        return "error", (f"{name}: expected test {soft[0][1]} failed on "
                         f"{soft[0][2]!r}, which is not an assertion")
    if not hits:
        return "error", (f"{name}: {len(records)} test(s) failed in {suite} "
                         f"but none of the expected {sorted(expected)}; "
                         f"first {records[0][1]}")
    missing = sorted(expected - {k for k, _n, _r in hits})
    first = hits[0]
    return "caught", (
        f"{name}: expected {len(hits)} of {len(expected)} failed "
        f"(not failed: {missing or 'none'}), {len(others)} other failed; "
        f"first {first[1]} - {first[2][:160]}")


def main(argv):
    args = list(argv[1:])
    check_only = "--check" in args
    selected = [a for a in args if a != "--check"]

    names = [m["name"] for m in MUTATIONS]
    duplicate = sorted({n for n in names if names.count(n) > 1})
    if duplicate:
        print(f"ERROR duplicate mutation names: {duplicate}")
        return 1
    if selected:
        unknown = [n for n in selected if n not in names]
        if unknown:
            print(f"ERROR no such mutation: {unknown}")
            return 1
        mutations = [m for m in MUTATIONS if m["name"] in selected]
    else:
        mutations = list(MUTATIONS)

    originals = {t: t.read_bytes() for t in TARGETS}
    endings = _endings(originals)
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    if check_only:
        return _check(mutations, sources, endings)

    suites = sorted({s for m in mutations for s in _run_suites(m)})
    for suite in suites:
        print(f"suite {suite}: {_python(suite)} in {SUITES[suite]['dir']}")
    problems = _provenance(suites)
    if problems:
        for p in problems:
            print(f"ERROR {p}")
        return 1

    errors = []
    for suite in suites:
        found, problem = _collect(suite)
        if problem:
            print(f"ERROR {problem}")
            return 1
        for m in mutations:
            if m["suite"] != suite:
                continue
            for test in m["expect"]:
                if test not in found:
                    errors.append(f"{m['name']}: no such test {test} "
                                  f"in suite {suite}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1
    print("name validation: every expected test exists")

    error, stop = _clear_pycache()
    if error:
        print(f"ERROR {error}")
    if stop is not None:
        raise stop
    if error:
        return 1
    for suite in suites:
        started = time.monotonic()
        base = _pytest(suite, list(SUITES[suite]["tests"]))
        print(f"baseline {suite}: exit {base.returncode} in "
              f"{time.monotonic() - started:.0f}s; "
              f"{(base.stdout.strip().splitlines() or [''])[-1]}")
        if base.returncode != 0:
            print(f"ERROR baseline not green for {suite}; refusing to start")
            print(base.stdout[-2000:])
            return 1
        print(f"baseline green: {suite}")
    print(f"running {len(mutations)} mutations")

    caught, survivors = [], []
    held = None
    try:
        for mutation in mutations:
            name, target = mutation["name"], mutation["target"]
            mutated, error = _apply(mutation, sources, endings)
            if mutated is None:
                errors.append(error)
                print(f"ERROR    {error}")
                continue
            raw = mutated.encode("utf-8")
            result = None
            ran_in = None
            wrote = False
            started = time.monotonic()
            try:
                wrote = True
                _atomic_write(target, raw)
                for suite in _run_suites(mutation):
                    ran_in = suite
                    result = _pytest(suite, list(SUITES[suite]["tests"]))
                    if result.returncode != 0:
                        break
            except subprocess.TimeoutExpired:
                result = None
                errors.append(f"{name}: never terminated within "
                              f"{RUN_TIMEOUT_S}s")
                print(f"ERROR    {name}: never terminated")
            except OSError as exc:
                errors.append(f"{name}: {exc}")
                print(f"ERROR    {name}: {exc}")
            finally:
                if wrote:
                    problem, stop = _restore(target, originals[target], raw)
                    if problem:
                        errors.append(f"{name}: {problem}")
                        print(f"ERROR    {name}: {problem}")
                    held = held or stop
                cache_error, stop = _clear_pycache()
                if cache_error:
                    errors.append(f"{name}: {cache_error}")
                    print(f"ERROR    {name}: {cache_error}")
                held = held or stop
            if held is not None:
                raise held
            if result is None:
                continue
            took = f" [{time.monotonic() - started:.0f}s]"
            if result.returncode == 0:
                kind = "survived"
                message = (f"{name}: every test passed in "
                           f"{_run_suites(mutation)}; expected "
                           f"{mutation['expect']}")
            else:
                kind, message = _verdict(mutation, ran_in, result)
            message += took
            if kind == "caught":
                caught.append(name)
                print(f"caught   {message}")
            elif kind == "survived":
                survivors.append(message)
                print(f"SURVIVED {message}")
            else:
                errors.append(message)
                print(f"ERROR    {message}")
    finally:
        for problem in _final_mismatches(originals):
            errors.append(problem)
            print(f"ERROR {problem}")

    if len(mutations) == len(MUTATIONS):
        scope = f"all {len(MUTATIONS)} mutations, none skipped"
    else:
        ran = {m["name"] for m in mutations}
        scope = (f"{len(mutations)} of {len(MUTATIONS)} mutations, named on "
                 f"the command line. NOT RUN: "
                 f"{[n for n in names if n not in ran]}")
    print(f"\nscope: {scope}.\ncaught {len(caught)}, survivors "
          f"{len(survivors)}, errors {len(errors)}")
    for line in survivors + errors:
        print(f"  {line}")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
