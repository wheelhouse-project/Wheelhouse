"""Mutation gate for the provider watchdog step (merge 3226556f).

Committed beside the tests it checks, so it can be re-run on demand
(wh-codex-merge-audit.8.1.1). Run it from the service directory with the
service venv; it refuses to start unless the worktree is clean.

Follows the user-level `mutation-gate` skill:
  - every pattern must match the target file EXACTLY once (ambiguous and
    missing are separate errors, never "survived");
  - every mutant must compile (a SyntaxError is an error, never "caught");
  - bytecode caches are cleared and PYTHONDONTWRITEBYTECODE=1 is set;
  - every expected catcher name is validated against --collect-only first;
  - the unmutated baseline must be green before the first mutation;
  - each mutation runs under its own subprocess timeout;
  - the source is restored with write_bytes, in a finally, and a second
    KeyboardInterrupt cannot escape with the mutant still on disk;
  - patterns are translated to the target file's own line ending;
  - stdout is line-buffered so a killed run still shows its position.

Usage:
    python mutation_gate_provider_watchdog.py [--check] [--only NAME[,NAME]]
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

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]
PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_provider_watchdog.py"
PER_MUTATION_TIMEOUT = 600  # baseline is ~60 s wall


# --------------------------------------------------------------------------
# Mutations.  `old` must appear exactly once in the target file.
# --------------------------------------------------------------------------
MUTATIONS = [
    # ---- 1. the cadence hookup itself -------------------------------------
    dict(
        name="cadence-call-removed",
        file="state_manager.py",
        proves="send_state_update runs the watchdog on the 3 s state-update loop",
        old=(
            "        try:\n"
            "            self._check_remote_stt_health()\n"
            "        except Exception as e:\n"
            '            logger.warning(f"Could not check speech provider health: {e}")\n'
        ),
        new="        pass\n",
        expect=[
            "test_post_ready_death_restarts_once_and_publishes_new_generation",
        ],
    ),
    # ---- 2. the containment around it -------------------------------------
    dict(
        name="health-check-uncontained",
        file="state_manager.py",
        proves="a watchdog failure must not stop the GUI state update",
        old=(
            "        try:\n"
            "            self._check_remote_stt_health()\n"
            "        except Exception as e:\n"
            '            logger.warning(f"Could not check speech provider health: {e}")\n'
        ),
        new="        self._check_remote_stt_health()\n",
        expect=[
            "test_unknown_process_health_preserves_state_delivery_without_retry",
        ],
    ),
    # ---- 3. the state transition the diff added ---------------------------
    dict(
        name="state-record-generation-not-updated",
        file="state_manager.py",
        proves="record_restart publishes the retry's new generation into the record",
        old=(
            "                    return False\n"
            "                self._running_remote_stt_generation = new_generation\n"
            "                return True\n"
        ),
        new=(
            "                    return False\n"
            "                return True\n"
        ),
        expect=[
            "test_post_ready_death_restarts_once_and_publishes_new_generation",
        ],
    ),
    # ---- 4. arming ---------------------------------------------------------
    dict(
        name="ready-flag-never-armed",
        file="stt/remote_stt_launcher.py",
        proves="a successful ready is what arms the post-ready watchdog",
        old="                signal.ready = not signal.failed\n",
        new="                signal.ready = False\n",
        expect=[
            "test_post_ready_death_restarts_once_and_publishes_new_generation",
        ],
    ),
    # ---- 5/6. the one-retry budget ----------------------------------------
    dict(
        name="retry-never",
        file="stt/remote_stt_launcher.py",
        proves="a first post-ready death gets one automatic restart",
        old=(
            "            retry_used = signal.watchdog_retry_used\n"
            "        if retry_used:\n"
        ),
        new=(
            "            retry_used = signal.watchdog_retry_used\n"
            "        if True:\n"
        ),
        expect=[
            "test_post_ready_death_restarts_once_and_publishes_new_generation",
        ],
    ),
    dict(
        name="retry-always",
        file="stt/remote_stt_launcher.py",
        proves="the retry budget is exactly one, not unlimited",
        old=(
            "            retry_used = signal.watchdog_retry_used\n"
            "        if retry_used:\n"
        ),
        new=(
            "            retry_used = signal.watchdog_retry_used\n"
            "        if False:\n"
        ),
        expect=[
            "test_successful_retry_death_does_not_restart_a_second_time",
        ],
    ),
    dict(
        name="retry-budget-not-stamped",
        file="stt/remote_stt_launcher.py",
        proves="the restart's own signal remembers the budget is spent",
        old="                new_signal.watchdog_retry_used = _watchdog_generation is not None\n",
        new="                new_signal.watchdog_retry_used = False\n",
        expect=[
            "test_successful_retry_death_does_not_restart_a_second_time",
        ],
    ),
    # ---- 7/8. what counts as death ----------------------------------------
    # A "live-process-treated-as-dead" mutation (dropping `proc.poll() is
    # None` from the liveness test) is EQUIVALENT and is deliberately not
    # here: `is_running` repeats that same poll on the same handle, so the
    # mutant cannot change behaviour. It survived the 2026-09-08 sweep for
    # that reason and would make every later sweep exit non-zero for no
    # signal.
    dict(
        name="pid-survivor-ignored",
        file="stt/remote_stt_launcher.py",
        proves="a live PID-file process blocks the restart even after the supervisor exits",
        old="        if proc is None or proc.poll() is None or self.is_running(provider_name):\n",
        new="        if proc is None or proc.poll() is None:\n",
        expect=[
            "test_live_pid_after_supervisor_exit_is_not_confirmed_provider_death",
        ],
    ),
    # ---- 9/10. explicit intent disables recovery ---------------------------
    dict(
        name="shutdown-flag-not-set",
        file="stt/remote_stt_launcher.py",
        proves="application shutdown disables every recovery before the first send",
        old=(
            "        with self._launch_signals_lock:\n"
            "            self._watchdog_shutdown = True\n"
        ),
        new=(
            "        with self._launch_signals_lock:\n"
            "            self._watchdog_shutdown = False\n"
        ),
        expect=[
            "test_application_shutdown_does_not_revive_already_dead_provider",
            "test_shutdown_disables_all_recovery_before_first_provider_send",
        ],
    ),
    dict(
        name="stop-request-not-recorded",
        file="stt/remote_stt_launcher.py",
        proves="an intentional stop disables recovery before its first await",
        old=(
            "        with self._launch_signals_lock:\n"
            "            signal = self._launch_signal(self._launch_generations.get(provider_name))\n"
            "            if signal is not None:\n"
            "                signal.stop_requested = True\n"
        ),
        new=(
            "        with self._launch_signals_lock:\n"
            "            signal = self._launch_signal(self._launch_generations.get(provider_name))\n"
            "            if signal is not None:\n"
            "                signal.stop_requested = False\n"
        ),
        expect=[
            "test_intentional_stop_disables_recovery_before_shutdown_send",
            "test_a_requested_stop_still_blocks_recovery_when_the_engine_dies",
        ],
    ),
    # ---- 11/12. the two new notices ---------------------------------------
    dict(
        name="notify-after-retry-death-removed",
        file="stt/remote_stt_launcher.py",
        proves="the user is told when the provider dies after its automatic restart",
        old=(
            "            self._report_watchdog_stopped(provider_name, generation)\n"
            "            self._notify(\n"
            "                provider_name,\n"
            '                "Speech provider stopped after its automatic restart - try restarting Wheelhouse",\n'
            "                generation, owner=provider_name,\n"
            "            )\n"
            "            return\n"
        ),
        new=(
            "            self._report_watchdog_stopped(provider_name, generation)\n"
            "            return\n"
        ),
        expect=[
            "test_successful_retry_death_does_not_restart_a_second_time",
        ],
    ),
    dict(
        name="notify-after-refusal-removed",
        file="stt/remote_stt_launcher.py",
        proves="the user is told when the automatic restart could not even begin",
        old=(
            "            self._report_watchdog_stopped(provider_name, generation)\n"
            "            self._notify(\n"
            "                provider_name,\n"
            '                "Speech provider could not restart - try restarting Wheelhouse",\n'
            "                generation, owner=provider_name,\n"
            "            )\n"
        ),
        new=(
            "            self._report_watchdog_stopped(provider_name, generation)\n"
        ),
        expect=[
            "test_failed_retry_spawn_clears_tray_and_notifies",
            "test_retry_refused_before_spawn_still_clears_tray_and_notifies",
        ],
    ),
    # ---- 13. off-loop survivor reconciliation ------------------------------
    dict(
        name="may-wait-forced-false",
        file="stt/remote_stt_launcher.py",
        proves="the watchdog's terminal report reaches the survivor reconciliation off-loop",
        old='                kwargs={"may_wait": True, "generation": generation},\n',
        new='                kwargs={"may_wait": False, "generation": generation},\n',
        expect=[
            "test_watchdog_terminal_failure_reconciles_surviving_switch_off_loop",
        ],
    ),
    # ---- 14-20. wh-codex-merge-audit.8.1.1 (and .8.1.3): the watchdog
    # follows the launch the record names, not the newest launch of any
    # provider, and a stop that never landed does not blind it forever.
    dict(
        name="entry-gate-launcher-wide-again",
        file="stt/remote_stt_launcher.py",
        proves="an engine adopted at an older stamp is still watched",
        old=(
            "            if (\n"
            "                generation is None\n"
            "                or self._watchdog_shutdown or signal is None\n"
        ),
        new=(
            "            if (\n"
            "                generation is None or not self.launch_is_current(generation)\n"
            "                or self._watchdog_shutdown or signal is None\n"
        ),
        expect=[
            "test_reconciled_survivor_death_restarts_once_and_records_the_new_launch",
            "test_switch_back_to_a_surviving_engine_watches_it_again",
        ],
    ),
    dict(
        name="restart-gate-launcher-wide-again",
        file="stt/remote_stt_launcher.py",
        proves="the pre-restart gate asks the same per-provider question",
        old=(
            "                self._launch_generations.get(provider_name) != generation\n"
            "                or self._watchdog_shutdown or signal.stop_requested\n"
            "                or signal.reported or signal.failed\n"
        ),
        new=(
            "                not self.launch_is_current(generation)\n"
            "                or self._watchdog_shutdown or signal.stop_requested\n"
            "                or signal.reported or signal.failed\n"
        ),
        expect=[
            "test_reconciled_survivor_death_restarts_once_and_records_the_new_launch",
            "test_switch_back_to_a_surviving_engine_watches_it_again",
        ],
    ),
    dict(
        name="report-gate-launcher-wide-again",
        file="stt/remote_stt_launcher.py",
        proves="the terminal report reaches an engine adopted at an older stamp",
        old=(
            "                signal is None or self._watchdog_shutdown or signal.stop_requested\n"
            "                or self._launch_generations.get(provider_name) != generation\n"
        ),
        new=(
            "                signal is None or self._watchdog_shutdown or signal.stop_requested\n"
            "                or not self.launch_is_current(generation)\n"
        ),
        expect=[
            "test_reconciled_survivor_refused_restart_reports_and_notifies",
        ],
    ),
    dict(
        name="refusal-report-gate-launcher-wide-again",
        file="stt/remote_stt_launcher.py",
        proves="a refused restart still reports and notifies for such an engine",
        old="            and self._launch_generations.get(provider_name) == generation\n",
        new="            and self.launch_is_current(generation)\n",
        expect=[
            "test_reconciled_survivor_refused_restart_reports_and_notifies",
        ],
    ),
    dict(
        name="start-guard-launcher-wide-again",
        file="stt/remote_stt_launcher.py",
        proves="start_provider accepts the watchdog's restart of such an engine",
        old=(
            "                    self._launch_generations.get(provider_name) != _watchdog_generation\n"
            "                    or self._watchdog_shutdown or previous is None\n"
        ),
        new=(
            "                    not self.launch_is_current(_watchdog_generation)\n"
            "                    or self._watchdog_shutdown or previous is None\n"
        ),
        expect=[
            "test_reconciled_survivor_death_restarts_once_and_records_the_new_launch",
            "test_switch_back_to_a_surviving_engine_watches_it_again",
        ],
    ),
    dict(
        name="stamp-guard-launcher-wide-again",
        file="stt/remote_stt_launcher.py",
        proves="the stamp guard accepts it too, so the restart is really stamped",
        old=(
            "                        self._launch_generations.get(provider_name) != _watchdog_generation\n"
            "                        or self._watchdog_shutdown or previous is None\n"
        ),
        new=(
            "                        not self.launch_is_current(_watchdog_generation)\n"
            "                        or self._watchdog_shutdown or previous is None\n"
        ),
        expect=[
            "test_reconciled_survivor_death_restarts_once_and_records_the_new_launch",
            "test_switch_back_to_a_surviving_engine_watches_it_again",
        ],
    ),
    dict(
        name="stale-stop-request-not-cleared",
        file="stt/remote_stt_launcher.py",
        proves="re-adopting an engine clears the intent of a stop that never landed",
        old=(
            "        with self._launch_signals_lock:\n"
            "            signal = self._launch_signal(self._launch_generations.get(provider_name))\n"
            "            if signal is not None:\n"
            "                signal.stop_requested = False\n"
        ),
        new=(
            "        with self._launch_signals_lock:\n"
            "            signal = self._launch_signal(self._launch_generations.get(provider_name))\n"
            "            if signal is not None:\n"
            "                pass\n"
        ),
        expect=[
            "test_switch_back_to_a_surviving_engine_watches_it_again",
            "test_reconciled_survivor_death_restarts_once_and_records_the_new_launch",
        ],
    ),
    dict(
        name="notify-drops-a-stale-owned-launch",
        file="stt/remote_stt_launcher.py",
        proves="a notice about one provider survives a newer launch of another",
        old=(
            "            selected = (self._launch_generations.get(owner) == generation if owner\n"
            "                        else self.launch_is_current(generation))\n"
        ),
        new="            selected = self.launch_is_current(generation)\n",
        expect=[
            "test_reconciled_survivor_refused_restart_reports_and_notifies",
        ],
    ),
    dict(
        name="failed-switch-does-not-rearm",
        file="main.py",
        proves="the engine a failed switch left running is re-armed",
        old=(
            "                    # Still the recorded engine, so keep watching it.\n"
            "                    remote_launcher.rearm_watchdog(current_provider)\n"
        ),
        new="",
        expect=[
            "test_failed_switch_keeps_watching_the_engine_it_left_running",
        ],
    ),
    dict(
        name="reconciled-survivor-does-not-rearm",
        file="main.py",
        proves="the survivor reconciliation re-arms the engine it adopts",
        old=(
            "            # The survivor is the recorded engine now: watch it.\n"
            "            remote_launcher.rearm_watchdog(survivor)\n"
        ),
        new="",
        expect=[
            "test_reconciled_survivor_death_restarts_once_and_records_the_new_launch",
        ],
    ),
    dict(
        name="already-running-branch-does-not-rearm",
        file="stt/remote_stt_launcher.py",
        proves="a switch back to a live engine re-arms that engine",
        # Both adoption exits of the already-running branch now carry the
        # same two lines, so each pattern is anchored on the log line above
        # it -- an unanchored snippet would match twice and mutate the wrong
        # exit (wh-codex-merge-audit.8.2.1).
        old=(
            '                    "is still alive - not starting a duplicate"\n'
            "                )\n"
            "                # Adopting this live launch re-arms its watchdog.\n"
            "                self.rearm_watchdog(provider_name)\n"
            "                return True\n"
        ),
        new=(
            '                    "is still alive - not starting a duplicate"\n'
            "                )\n"
            "                return True\n"
        ),
        expect=[
            "test_switch_back_to_a_surviving_engine_watches_it_again",
        ],
    ),
    dict(
        name="port-match-adoption-does-not-rearm",
        file="stt/remote_stt_launcher.py",
        proves="the port-match adoption exit re-arms the engine it adopts",
        old=(
            '                logger.info(f"Provider {provider_name} is already'
            ' running on port {self.ws_port}")\n'
            "                # Adopting this live launch re-arms its watchdog.\n"
            "                self.rearm_watchdog(provider_name)\n"
            "                return True\n"
        ),
        new=(
            '                logger.info(f"Provider {provider_name} is already'
            ' running on port {self.ws_port}")\n'
            "                return True\n"
        ),
        expect=[
            "test_switch_back_to_a_pid_file_survivor_watches_it_again",
        ],
    ),
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def newline_of(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def to_file_newline(text: str, nl: str) -> str:
    return text.replace("\n", nl) if nl != "\n" else text


def clear_pycache(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        if ".venv" in cache.parts:
            continue
        shutil.rmtree(cache, ignore_errors=True)


def restore(path: Path, original: bytes) -> None:
    """Put the file back. A second Ctrl+C must not escape with the mutant."""
    held = None
    for attempt in range(6):
        try:
            path.write_bytes(original)
            if held is not None:
                clear_pycache(SERVICE)
                raise held
            return
        except KeyboardInterrupt as exc:      # second interrupt, inside restore
            held = exc
            time.sleep(0.2)
        except PermissionError:               # WinError 5: another holder
            time.sleep(0.5)
        except OSError:
            time.sleep(0.5)
    print(f"  [ERROR] COULD NOT RESTORE {path} -- the mutant is still on disk")
    if held is not None:
        raise held


def collected_test_names() -> set[str]:
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", TESTS, "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(SERVICE), capture_output=True, text=True, timeout=600,
        env=child_env(),
    )
    names = set()
    for line in proc.stdout.splitlines():
        if "::" not in line:
            continue
        tail = line.split("::")[-1].split(" ")[0].strip()
        names.add(tail)
        names.add(tail.split("[")[0])
    return names


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_DEBUG_TEMPROOT", None)
    return env


FAIL_LINE = re.compile(r"^(FAILED|ERROR) (tests/\S+::\S+)(?: - (.*))?$")
COUNTS = re.compile(r"^(?:=+ )?(?:\x1b\[[0-9;]*m)?(\d+ (?:passed|failed).*?)(?: =+)?$")


def run_selection() -> tuple[int, str, dict[str, str]]:
    """Run the guard tests. Returns (rc, combined output, {test: reason})."""
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", TESTS, "-q", "-rf",
         "-p", "no:cacheprovider"],
        cwd=str(SERVICE), capture_output=True, text=True,
        timeout=PER_MUTATION_TIMEOUT, env=child_env(),
    )
    out = proc.stdout + proc.stderr
    failed: dict[str, str] = {}
    for line in out.splitlines():
        m = FAIL_LINE.match(line.strip())
        if m:
            nodeid, reason = m.group(2), (m.group(3) or "").strip()
            name = nodeid.split("::")[-1]
            failed[name] = reason
            failed[name.split("[")[0]] = reason
    return proc.returncode, out, failed


def summary_line(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line):
            return line.strip()
    return "(no summary line)"


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify patterns and compilation only; run no tests")
    ap.add_argument("--only", default="", help="comma-separated mutation names")
    args = ap.parse_args()

    wanted = [n for n in args.only.split(",") if n]
    selected = [m for m in MUTATIONS if not wanted or m["name"] in wanted]
    unknown = set(wanted) - {m["name"] for m in MUTATIONS}
    if unknown:
        print(f"[ERROR] unknown mutation name(s): {sorted(unknown)}")
        return 2
    print(f"scope: running {len(selected)} of {len(MUTATIONS)} mutations"
          f"{' (--only)' if wanted else ' (full sweep)'}")

    errors: list[str] = []

    # ---- the worktree must start clean --------------------------------
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(SERVICE),
                        capture_output=True, text=True)
    if st.stdout.strip():
        print("[ERROR] worktree is not clean before the gate:")
        print(st.stdout)
        return 2

    # ---- pattern + compile pre-flight ---------------------------------
    sources: dict[str, bytes] = {}
    for m in selected:
        path = SERVICE / m["file"]
        if m["file"] not in sources:
            sources[m["file"]] = path.read_bytes()
        raw = sources[m["file"]]
        nl = newline_of(raw)
        text = raw.decode("utf-8")
        old = to_file_newline(m["old"], nl)
        count = text.count(old)
        if count == 0:
            errors.append(f"{m['name']}: PATTERN-NOT-FOUND in {m['file']}")
            m["_bad"] = True
            continue
        if count > 1:
            errors.append(f"{m['name']}: PATTERN-AMBIGUOUS ({count} matches) in {m['file']}")
            m["_bad"] = True
            continue
        mutated = text.replace(old, to_file_newline(m["new"], nl), 1)
        if mutated == text:
            errors.append(f"{m['name']}: MUTATION IS A NO-OP")
            m["_bad"] = True
            continue
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as e:
            errors.append(f"{m['name']}: DOES-NOT-COMPILE ({e})")
            m["_bad"] = True
            continue
        m["_mutated"] = mutated.encode("utf-8")
    print(f"checked {len(selected)} patterns, "
          f"{sum(1 for m in selected if m.get('_bad'))} bad")

    # ---- expected catcher names must exist ----------------------------
    print("collecting test names ...")
    names = collected_test_names()
    if not names:
        print("[ERROR] collection produced no test names")
        return 2
    for m in selected:
        for want in m["expect"]:
            if want not in names:
                errors.append(f"{m['name']}: EXPECTED-TEST-MISSING {want}")
                m["_bad"] = True

    if errors:
        print("\n=== PRE-FLIGHT ERRORS ===")
        for e in errors:
            print("  " + e)
        return 2

    if args.check:
        print("\n--check: patterns match once, mutants compile, catcher names exist.")
        return 0

    # ---- baseline must be green ---------------------------------------
    print("baseline (unmutated) run ...")
    clear_pycache(SERVICE)
    rc, out, failed = run_selection()
    print(f"  baseline: rc={rc}  {summary_line(out)}")
    if rc != 0 or failed:
        print("[ERROR] baseline is not green; refusing to start")
        return 2

    # ---- the sweep -----------------------------------------------------
    results = []
    for i, m in enumerate(selected, 1):
        path = SERVICE / m["file"]
        original = sources[m["file"]]
        print(f"\n[{i}/{len(selected)}] {m['name']}  ({m['file']})")
        print(f"      proves: {m['proves']}")
        verdict, detail = "?", ""
        try:
            clear_pycache(SERVICE)
            path.write_bytes(m["_mutated"])
            clear_pycache(SERVICE)
            try:
                rc, out, failed = run_selection()
            except subprocess.TimeoutExpired:
                verdict, detail = "ERROR", "HANG (per-mutation timeout)"
            else:
                if "+++ Timeout +++" in out:
                    verdict, detail = "ERROR", "suite-timeout-abort"
                else:
                    hits = {n: failed[n] for n in m["expect"] if n in failed}
                    others = sorted(
                        n for n in failed
                        if "[" not in n and n not in m["expect"]
                    )
                    if hits:
                        verdict = "caught"
                        detail = "; ".join(f"{n}: {r[:110]}" for n, r in hits.items())
                        if others:
                            detail += f" | also failed: {', '.join(others)}"
                    elif failed:
                        verdict = "SURVIVED-BY-EXPECTED"
                        detail = (f"none of {m['expect']} failed; other failures: "
                                  f"{', '.join(others)}")
                    else:
                        verdict = "SURVIVED"
                        detail = f"rc={rc} {summary_line(out)}"
        finally:
            restore(path, original)
            clear_pycache(SERVICE)
        print(f"      -> {verdict}: {detail}")
        results.append((m["name"], verdict, detail, m["proves"]))

    # ---- report ---------------------------------------------------------
    print("\n" + "=" * 72)
    print(f"SUMMARY  ({len(selected)} of {len(MUTATIONS)} mutations run)")
    print("=" * 72)
    caught = [r for r in results if r[1] == "caught"]
    bad = [r for r in results if r[1] != "caught"]
    for name, verdict, detail, proves in results:
        print(f"  {verdict:22s} {name}")
        if verdict != "caught":
            print(f"      untested behaviour: {proves}")
            print(f"      {detail}")
    print(f"\ncaught={len(caught)}  survived/error={len(bad)}")

    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(SERVICE),
                        capture_output=True, text=True)
    print(f"worktree after the gate: "
          f"{'CLEAN' if not st.stdout.strip() else 'DIRTY -- ' + st.stdout}")
    leftovers = list((SERVICE).rglob("*.mutation-gate.*.tmp"))
    if leftovers:
        print(f"[ERROR] leftover temp files: {leftovers}")
    return 0 if not bad and not leftovers and not st.stdout.strip() else 1


if __name__ == "__main__":
    sys.exit(main())
