"""Read-only startup checks for discovered speech provider environments.

The command asks uv for a read-only answer: --check, --frozen, --offline and
--no-python-downloads.

Measured with uv 0.6.14 and uv 0.12.17, on a fixture lock, in both the
matching and the missing-package case: neither version installed a package,
rewrote uv.lock, or changed any provider file. uv 0.12.17 created one
zero-byte .venv/.lock of its own: over three consecutive checks of one
fixture the first check created it and the next two created no file, and it
stayed at zero bytes. Whether uv rewrites that file in place was not
measured. uv 0.6.14 created no file at all.

The same outdated environment exited 2 under uv 0.6.14 and 1 under uv 0.12.17,
while both ended stderr with the same diagnostic sentence. So the complete
final sentence decides a mismatch and the exit code does not.
Unknown diagnostics degrade to a warning, never a speculative repair notice.
"""
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path


log = logging.getLogger(__name__)
CHECK_TIMEOUT_SECONDS = 5.0
# Spawning a thread and reaching the uv call costs time the subprocess
# limit above does not cover, so the collector allows each check a
# little more than the check itself is allowed.
COLLECT_GRACE_SECONDS = 0.5
_OUTDATED = "error: The environment is outdated; run `uv sync` to update the environment"
_REMEDY = "Re-run the WheelHouse installer. Developers: run bootstrap.ps1."
_run_uv = subprocess.run


def _check_one(provider, runner):
    directory = Path(provider["service_dir"]).resolve()
    # uv searches parents when local project files are absent. Never audit
    # some ancestor's environment instead of this discovered provider's lock.
    if not all((directory / name).is_file() for name in ("pyproject.toml", "uv.lock")):
        raise ValueError("provider pyproject.toml or uv.lock is missing")
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(directory / ".venv")
    # The installer omits dev dependencies; bootstrap includes them. Audit the
    # runtime set while allowing extra installed packages in either environment.
    result = runner(
        ["uv", "sync", "--directory", str(directory), "--project", str(directory), "--check", "--frozen",
         "--no-dev", "--inexact", "--offline", "--no-python-downloads", "--color", "never"],
        env=env, capture_output=True, text=True, errors="replace",
        timeout=CHECK_TIMEOUT_SECONDS,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode == 0:
        return False
    # crewcut: this reads uv's terminal diagnostic sentence instead of a
    # structured result. Measured: uv 0.6.14 has no structured result at all
    # ("uv sync --help" lists no output-format option, and --output-format is
    # rejected with "unexpected argument" and exit 2), while uv 0.12.17 does
    # (--output-format json prints sync.action "check" with a sync.changes
    # array, empty when the environment matches). One command cannot use it on
    # both versions, and uv 0.12.17 names that schema "preview" itself. Switch
    # to the structured result once the oldest supported uv has a stable one.
    # A changed diagnostic yields one warning and continues.
    # The exit code is deliberately no part of the match. The same outdated
    # environment exited 2 under uv 0.6.14 and 1 under uv 0.12.17, so a test
    # against a code silently replaced the repair notice with a warning. The
    # line above has already established that this exit code is nonzero.
    if result.stderr.rstrip().endswith(_OUTDATED):
        return True
    raise RuntimeError(f"uv check exited {result.returncode} without a recognized mismatch")


def check_provider_environments(providers, *, runner=None):
    """Return mismatch notices in discovery order, one budget per provider.

    Only the caller logs or builds notices. Late workers cannot publish stale
    results. Daemon threads also bound the wait if OS process creation stalls
    before subprocess.run can enforce its own timeout; no executor shutdown
    implicitly joins such a thread. Every provider is attempted concurrently.
    """
    runner = runner or _run_uv
    results = queue.Queue()
    outcomes = {}
    deadlines = {}

    def worker(index, provider):
        try:
            result = _check_one(provider, runner)
        except Exception as exc:
            result = exc
        results.put((index, result))

    for index, provider in enumerate(providers):
        try:
            threading.Thread(target=worker, args=(index, provider),
                             name="ProviderEnvironmentCheck", daemon=True).start()
        except Exception as exc:
            outcomes[index] = exc
        else:
            # One deadline shared by every provider, and started before the
            # first thread spawned, discarded the answer of any provider that
            # had not reported by then: a slow sibling silenced a check that
            # finished, and a slow machine produced no notice at all
            # (wh-codex-merge-audit.5.1.1). Each check owns its budget now.
            deadlines[index] = time.monotonic() + CHECK_TIMEOUT_SECONDS + COLLECT_GRACE_SECONDS
    while len(outcomes) < len(providers):
        deadline = max((deadlines[i] for i in deadlines if i not in outcomes), default=0.0)
        try:
            index, result = results.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        outcomes[index] = result

    notices = []
    for index, provider in enumerate(providers):
        name = provider.get("display_name") or provider.get("name", "Unknown provider")
        result = outcomes.get(index, TimeoutError("five-second startup check deadline exceeded"))
        if isinstance(result, Exception):
            log.warning("Could not check %s environment: %s", name, result)
        elif result:
            notices.append(f"{name}: Speech environment is out of date. {_REMEDY}")
    return notices


def notify_provider_environment_mismatches(notices):
    """Submit each notice once, without waiting for Windows toast delivery."""
    from services.wheelhouse.utils.logging_setup import get_notifier_worker
    # Production's qualified logging_setup constructs this bare notifier class;
    # its consumer rejects the distinct qualified NotifierPayload identity.
    from utils.notifier_worker import NotifierPayload

    for message in notices:
        try:
            worker = get_notifier_worker()
            if worker is None or not worker.submit(NotifierPayload(
                title="Wheelhouse: Speech setup", message=message,
                levelname="INFO", trace_id="",
            )):
                log.warning("Could not queue provider startup notice: %s", message)
        except Exception as exc:
            log.warning("Could not queue provider startup notice: %s (%s)", message, exc)
