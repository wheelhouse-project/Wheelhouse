"""Startup must warn about stale provider environments without repairing them."""
import asyncio
import logging
import hashlib
import os
import subprocess
import sys
import threading
import time
import venv
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stt import provider_env_check as check


OUTDATED = "error: The environment is outdated; run `uv sync` to update the environment"
REMEDY = "Re-run the WheelHouse installer. Developers: run bootstrap.ps1."


@pytest.fixture
def providers(tmp_path):
    found = []
    for name in ("alpha", "beta", "gamma"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "uv.lock").write_text("version = 1\n")
        (directory / "pyproject.toml").write_text("[project]\n")
        found.append(dict(name=name, display_name=name.title(), service_dir=directory))
    return found


def slow_spawn_threading(delay):
    """A threading stand-in whose Thread.start pays a scheduling cost.

    The collector budget is set before the worker threads are spawned, so
    whatever the spawn costs is charged to every provider's budget. On a
    loaded machine that cost is real; here it is fixed so the test can say
    exactly which provider answered inside its own limit.
    """
    def Thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        start = thread.start

        def slow_start():
            time.sleep(delay)
            start()

        thread.start = slow_start
        return thread

    return SimpleNamespace(Thread=Thread)


def settle(condition, limit=5.0):
    """Wait up to limit seconds for background work to reach a state."""
    deadline = time.monotonic() + limit
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


class Runner:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        directory = Path(cmd[cmd.index("--directory") + 1])
        outcome = self.outcomes.get(directory.name, (0, "Would make no changes"))
        if isinstance(outcome, Exception):
            raise outcome
        code, stderr = outcome
        return SimpleNamespace(returncode=code, stderr=stderr, stdout="")


def test_all_providers_use_their_own_lock_and_environment(providers, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "another-env")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "another-project-env")
    runner = Runner()
    check.check_provider_environments(providers, runner=runner)
    assert len(runner.calls) == 3, "every discovered provider must be checked"
    paths = set()
    for cmd, kwargs in runner.calls:
        directory = cmd[cmd.index("--directory") + 1]
        paths.add(directory)
        assert cmd[:2] == ["uv", "sync"]
        assert kwargs["env"].get("VIRTUAL_ENV") is None
        assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(Path(directory) / ".venv")
    assert paths == {str(p["service_dir"]) for p in providers}
    assert os.environ["VIRTUAL_ENV"] == "another-env"


def test_command_is_offline_read_only_and_bounded(providers):
    runner = Runner()
    check.check_provider_environments(providers[:1], runner=runner)
    assert runner.calls, "the provider must reach the read-only check"
    cmd, kwargs = runner.calls[0]
    assert "--offline" in cmd, "the check must forbid network requests"
    assert "--check" in cmd, "the check must report, never install"
    assert "--frozen" in cmd, "the check must never update uv.lock"
    assert "--no-python-downloads" in cmd
    assert "--no-dev" in cmd, "the runtime check must not require development packages"
    assert "--inexact" in cmd, "the runtime check must tolerate installed development packages"
    assert kwargs["timeout"] == 5.0, "each subprocess must have a five-second limit"
    assert kwargs.get("shell", False) is False
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_inherited_project_override_cannot_select_another_lock(providers, monkeypatch):
    monkeypatch.setenv("UV_PROJECT", str(providers[1]["service_dir"]))
    runner = Runner()
    check.check_provider_environments(providers[:1], runner=runner)
    cmd, _ = runner.calls[0]
    assert "--project" in cmd, "an inherited UV_PROJECT must not select another lock"
    assert cmd[cmd.index("--project") + 1] == str(providers[0]["service_dir"])


def test_matching_environment_is_silent(providers, caplog):
    assert check.check_provider_environments(providers, runner=Runner()) == []
    assert not caplog.records


def test_mismatch_has_exactly_one_named_remedy(providers):
    notices = check.check_provider_environments(
        providers, runner=Runner({"alpha": (2, OUTDATED), "gamma": (2, OUTDATED)})
    )
    assert len(notices) == 2, "one notice per mismatched provider"
    assert "Alpha" in notices[0] and "Gamma" in notices[1]
    assert all(message.count(REMEDY) == 1 for message in notices)


# The complete stderr each uv version produced for the SAME outdated fixture
# environment, captured by the A1 measurement of 2026-09-19 (bd issue
# wh-provider-env-check-current-uv, comment "STAGE A1 DONE"). The exit codes
# differ, 2 against 1, and the first line differs. The OUTDATED sentence is
# byte-identical and is the last line in both, which is why the module matches
# the sentence and ignores the code. A code test dropped the repair notice for
# every user on a current uv.
CAPTURED_OUTDATED = {
    "uv-0.6.14": (2,
                  "Discovered existing environment at: .venv\n"
                  "Found up-to-date lockfile at: uv.lock\n"
                  "Would download 1 package\n"
                  "Would install 1 package\n"
                  " + idna==3.10\n" + OUTDATED + "\n"),
    "uv-0.12.17": (1,
                   "Would use project environment at: .venv\n"
                   "Would download 1 package\n"
                   "Would install 1 package\n"
                   " + idna==3.10\n" + OUTDATED + "\n"),
}


@pytest.mark.parametrize("version", list(CAPTURED_OUTDATED))
def test_measured_uv_versions_both_give_the_repair_notice(providers, caplog, version):
    """Real captured stderr from both measured uv versions, whose exit codes differ."""
    code, stderr = CAPTURED_OUTDATED[version]
    with caplog.at_level(logging.WARNING):
        notices = check.check_provider_environments(
            providers[:1], runner=Runner({"alpha": (code, stderr)})
        )
    assert len(notices) == 1, f"{version} exits {code} and must still give one notice"
    assert "Alpha" in notices[0] and REMEDY in notices[0]
    assert not caplog.records, "a recognized mismatch must not warn as well"


def test_a_line_after_the_diagnostic_is_not_a_recognized_mismatch(providers, caplog):
    """The diagnostic must be the LAST line, not merely present somewhere.

    The exit code is no part of the match, so this sentence is the whole
    discriminator. Matching it anywhere in stderr would let an ordinary uv
    error that happens to quote the sentence produce a repair notice.
    """
    _, stderr = CAPTURED_OUTDATED["uv-0.12.17"]
    with caplog.at_level(logging.WARNING):
        notices = check.check_provider_environments(
            providers[:1], runner=Runner({"alpha": (1, stderr + "note: an unmeasured trailing line\n")})
        )
    assert notices == [], "a line after the diagnostic makes the form unrecognized"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Alpha" in warnings[0].message, "an unrecognized form warns once"


def test_success_exit_gives_no_notice_even_when_stderr_holds_the_diagnostic(providers, caplog):
    """Exit 0 settles it. No stderr text may turn a healthy environment into a notice."""
    _, stderr = CAPTURED_OUTDATED["uv-0.12.17"]
    with caplog.at_level(logging.WARNING):
        notices = check.check_provider_environments(
            providers[:1], runner=Runner({"alpha": (0, stderr)})
        )
    assert notices == [], "exit 0 must stay silent whatever stderr says"
    assert not caplog.records, "exit 0 must not warn either"


@pytest.mark.parametrize("outcome", [
    pytest.param((2, "error: Failed to parse uv.lock"), id="invalid-lock"),
    pytest.param((1, "error: unavailable"), id="unknown-exit"),
    pytest.param((2, ""), id="unknown-diagnostic"),
    pytest.param(FileNotFoundError("uv missing"), id="missing-uv"),
    pytest.param(subprocess.TimeoutExpired("uv", 5), id="timeout"),
], ids=None)
def test_failure_warns_once_and_continues(providers, caplog, outcome):
    with caplog.at_level(logging.WARNING):
        notices = check.check_provider_environments(
            providers[:2], runner=Runner({"alpha": outcome, "beta": (2, OUTDATED)})
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "a failed check needs exactly one warning"
    assert "Alpha" in warnings[0].message
    assert len(notices) == 1 and "Beta" in notices[0], "other checks must continue"


def test_missing_local_lock_never_checks_an_ancestor_project(providers, caplog):
    (providers[0]["service_dir"] / "uv.lock").unlink()
    runner = Runner()
    check.check_provider_environments(providers[:1], runner=runner)
    assert len(caplog.records) == 1, "a missing local lock needs one warning"
    assert runner.calls == [], "do not let uv walk up to another project"


def test_checks_are_concurrent(providers):
    barrier = threading.Barrier(3, timeout=1)
    def run(cmd, **kwargs):
        barrier.wait()
        return Runner({n: (2, OUTDATED) for n in ("alpha", "beta", "gamma")})(cmd, **kwargs)
    notices = check.check_provider_environments(providers, runner=run)
    assert len(notices) == 3, "all three checks must run together"


def test_stuck_runner_does_not_hold_startup_or_emit_late_notice(providers, monkeypatch, caplog):
    # A stuck OS process-creation call may ignore subprocess.run's timeout.
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(check, "CHECK_TIMEOUT_SECONDS", 0.1, raising=False)
    def run(cmd, **kwargs):
        started.set()
        release.wait(2)
        finished.set()
        return SimpleNamespace(returncode=2, stderr=OUTDATED, stdout="")
    try:
        notices = check.check_provider_environments(providers[:1], runner=run)
        assert started.is_set(), "the provider check must actually start"
        assert not finished.is_set(), "startup must return before a stuck runner"
        assert notices == []
        assert len(caplog.records) == 1, "a deadline expiry needs exactly one warning"
    finally:
        release.set()
        if started.is_set():
            assert finished.wait(1)
    assert notices == [] and len(caplog.records) == 1


def test_a_finished_check_keeps_its_notice_when_a_sibling_never_answers(providers, monkeypatch, caplog):
    # One deadline shared by every provider, started before the first thread
    # spawned, threw away the answer of any provider that had not reported by
    # then -- so a slow sibling, or a machine slow to start threads, silenced
    # a provider that answered well inside its own limit
    # (wh-codex-merge-audit.5.1.1).
    monkeypatch.setattr(check, "CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(check, "threading", slow_spawn_threading(0.15))

    def run(cmd, **kwargs):
        limit = kwargs["timeout"]
        if Path(cmd[cmd.index("--directory") + 1]).name == "alpha":
            time.sleep(limit)
            raise subprocess.TimeoutExpired(cmd, limit)
        time.sleep(limit / 2)
        return SimpleNamespace(returncode=2, stderr=OUTDATED, stdout="")

    with caplog.at_level(logging.WARNING):
        notices = check.check_provider_environments(providers[:2], runner=run)
    assert notices == [f"Beta: Speech environment is out of date. {REMEDY}"], (
        "a provider that answered inside its own limit must keep its notice")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Alpha" in warnings[0].message, (
        "only the provider whose own check never answered may be warning-only")


async def test_startup_check_does_not_block_the_logic_event_loop(providers, monkeypatch):
    import service_manager as sm

    # main.py calls start_remote_stt() from the Logic event loop thread with
    # no await, so anything this method waits for stalls the websocket server
    # and every task already scheduled on that loop.
    manager = sm.ServiceManager(Mock(), Mock(), Mock(), Mock(
        get_screen_dimensions=lambda: (1920, 1080)), Mock())
    manager.config_service.get.return_value = "alpha"
    launcher = Mock()
    launcher.discover_providers.return_value = providers
    launcher.start_provider.return_value = True
    manager.remote_stt_launcher = launcher

    loop_thread = threading.get_ident()
    loop_ran = threading.Event()
    finished = threading.Event()
    observed = {}

    def slow_check(found):
        observed["thread"] = threading.get_ident()
        observed["loop_ran_during_check"] = loop_ran.wait(1.0)
        finished.set()
        return []

    monkeypatch.setattr(sm, "check_provider_environments", slow_check)
    monkeypatch.setattr(sm, "notify_provider_environment_mismatches", lambda notices: None)

    async def tick():
        loop_ran.set()

    task = asyncio.create_task(tick())
    assert manager.start_remote_stt(), "the provider must still be launched"
    await task
    assert finished.wait(2), "the environment check must actually run"
    assert observed["loop_ran_during_check"], (
        "the Logic event loop was blocked while the provider environment check ran")
    assert observed["thread"] != loop_thread, (
        "the environment check must not run on the Logic event loop thread")


def test_startup_starts_the_check_before_first_launch_and_submits_one_notice(providers, monkeypatch):
    import service_manager as sm
    from utils.notifier_worker import NotifierWorker
    from services.wheelhouse.utils import logging_setup

    # Real startup method and notifier queue; only hardware/process boundaries replaced.
    manager = sm.ServiceManager(Mock(), Mock(), Mock(), Mock(
        get_screen_dimensions=lambda: (1920, 1080)), Mock())
    manager.config_service.get.return_value = "alpha"
    launcher = Mock()
    launcher.discover_providers.return_value = providers
    manager.remote_stt_launcher = launcher
    worker = NotifierWorker()
    monkeypatch.setattr(logging_setup, "get_notifier_worker", lambda: worker)
    recorder = Runner({"beta": (2, OUTDATED)})
    began = threading.Event()
    def runner(cmd, **kwargs):
        began.set()
        return recorder(cmd, **kwargs)
    monkeypatch.setattr(check, "_run_uv", runner, raising=False)
    # This test owns the provider environment notices only. The runtime
    # notice leads the same list whenever the computer's Microsoft Visual
    # C++ runtime cannot serve, so without this line the first payload --
    # and the count below it -- would depend on the computer running the
    # test. tests/test_runtime_dll_directory.py owns that behaviour.
    monkeypatch.setattr(sm, "runtime_version_notice", lambda: None)
    events = []
    def launch(name):
        # The launch no longer waits for the answer, but the check must
        # already be under way by the time the first provider is launched.
        events.append((name, began.wait(2)))
        return True
    launcher.start_provider.side_effect = launch
    assert manager.start_remote_stt()
    assert manager.start_remote_stt()
    assert events == [("alpha", True), ("alpha", True)], "the check must start before the first launch"
    settle(lambda: len(recorder.calls) >= 3)
    time.sleep(0.3)
    assert len(recorder.calls) == 3, "every provider is checked exactly once per run"
    settle(lambda: not worker.queue.empty())
    assert not worker.queue.empty(), "the mismatch must reach the notifier queue"
    payload = worker.queue.get_nowait()
    assert "Beta" in payload.message and REMEDY in payload.message
    assert worker.queue.empty(), "one notice per mismatched provider per run"


def test_notice_reaches_worker_initialized_by_production_logging(monkeypatch):
    from services.wheelhouse.utils import logging_setup as production_logging
    from services.wheelhouse.utils import notifier_worker as qualified_notifier
    from utils import logging_setup as bare_logging
    from utils import notifier_worker as active_notifier

    # Logic calls qualified setup_logging, which currently constructs the bare
    # notifier class. Exercise initialization AND the real consumer's type gate.
    assert production_logging is not bare_logging
    assert qualified_notifier.NotifierPayload is not active_notifier.NotifierPayload
    monkeypatch.setattr(bare_logging, "_NOTIFIER_WORKER", None)
    handler = logging.NullHandler()
    handler.doRollover = lambda: None
    monkeypatch.setattr(production_logging, "ConcurrentRotatingFileHandler", lambda *a, **k: handler)
    delivered = []
    arrived = threading.Event()
    def record(self, payload):
        delivered.append(payload)
        arrived.set()
    monkeypatch.setattr(active_notifier.NotifierWorker, "_deliver", record)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        production_logging.setup_logging({})
        worker = production_logging.get_notifier_worker()
        assert isinstance(worker, active_notifier.NotifierWorker)
        assert bare_logging.get_notifier_worker() is None
        check.notify_provider_environment_mismatches(["Beta: " + REMEDY])
        assert arrived.wait(1), "the initialized production worker must accept and deliver the notice"
        assert len(delivered) == 1
        assert isinstance(delivered[0], active_notifier.NotifierPayload)
        assert delivered[0].message == "Beta: " + REMEDY
    finally:
        production_logging.shutdown_logging(timeout=2)
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.mark.parametrize("failure", ["missing", "full", "exception"])
def test_notice_queue_failure_does_not_abort_startup(providers, monkeypatch, caplog, failure):
    from services.wheelhouse.utils import logging_setup
    worker = None if failure == "missing" else Mock()
    if failure == "full":
        worker.submit.return_value = False
    elif failure == "exception":
        worker.submit.side_effect = RuntimeError("notification unavailable")
    monkeypatch.setattr(logging_setup, "get_notifier_worker", lambda: worker)
    check.notify_provider_environment_mismatches(["Beta: " + REMEDY])
    assert len(caplog.records) == 1, "unavailable notifier must log delivery failure"


@pytest.mark.parametrize("setup_kind", ["installer", "developer"])
def test_installed_uv_runtime_check_accepts_installer_and_developer_envs(tmp_path, monkeypatch, setup_kind):
    """Actual uv with local fixture wheels, using the two shipped sync forms."""
    monkeypatch.setattr(check, "CHECK_TIMEOUT_SECONDS", 30.0)
    directory = tmp_path / "fixture"
    directory.mkdir()
    for name in ("runtime_probe", "dev_probe"):
        with zipfile.ZipFile(directory / f"{name}-1.0-py3-none-any.whl", "w") as wheel:
            metadata = f"{name}-1.0.dist-info"
            wheel.writestr(f"{name}/__init__.py", "")
            wheel.writestr(f"{metadata}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
            wheel.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr(f"{metadata}/RECORD", "")
    (directory / "pyproject.toml").write_text(
        '[project]\nname="probe"\nversion="0.0.0"\nrequires-python=">=3.12"\n'
        'dependencies=["runtime-probe"]\n[dependency-groups]\ndev=["dev-probe"]\n'
        '[tool.uv.sources]\nruntime-probe={path="runtime_probe-1.0-py3-none-any.whl"}\n'
        'dev-probe={path="dev_probe-1.0-py3-none-any.whl"}\n'
    )
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(directory / ".venv")
    def setup(*args):
        subprocess.run(["uv", *args, "--directory", str(directory), "--project", str(directory),
                        "--offline", "--no-python-downloads", "--python", sys.executable], env=env, capture_output=True,
                       text=True, check=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    setup("lock")
    setup("sync", *(["--locked", "--no-dev"] if setup_kind == "installer" else []))
    site = directory / ".venv" / ("Lib/site-packages" if os.name == "nt" else
                                  f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    assert (site / "runtime_probe-1.0.dist-info").is_dir()
    assert (site / "dev_probe-1.0.dist-info").is_dir() == (setup_kind == "developer")
    def snapshot():
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob("*") if p.is_file()}
    before = snapshot()
    provider = dict(name="fixture", display_name="Fixture Speech", service_dir=directory)
    assert check.check_provider_environments([provider]) == [], f"healthy {setup_kind} environment must be silent"
    assert snapshot() == before, "runtime dependency check must not change any file"


def test_installed_uv_missing_locked_package_is_read_only(tmp_path, monkeypatch):
    """Real uv boundary; no provider, installer or microphone is started."""
    # This empirical fixture checks uv's read-only semantics, not scheduling
    # latency on a busy Windows host. Dedicated tests enforce the production
    # five-second command/deadline contract; allow this fixture process to start.
    monkeypatch.setattr(check, "CHECK_TIMEOUT_SECONDS", 30.0)
    directory = tmp_path / "fixture"
    directory.mkdir()
    venv.EnvBuilder(with_pip=False).create(directory / ".venv")
    (directory / "pyproject.toml").write_text(
        '[project]\nname="probe"\nversion="0.0.0"\nrequires-python=">=3.12"\ndependencies=[]\n'
    )
    lock = directory / "uv.lock"
    matching = ('version = 1\nrequires-python = ">=3.12"\n[[package]]\n'
                'name = "probe"\nversion = "0.0.0"\nsource = { virtual = "." }\n')
    lock.write_text(matching)
    provider = dict(name="fixture", display_name="Fixture Speech", service_dir=directory)
    # Explicit additional guard for this empirical test, including mutation runs.
    monkeypatch.setenv("UV_OFFLINE", "1")
    # uv 0.12.17 creates one zero-byte .venv/.lock of its own. Measured over
    # three consecutive checks of one fixture: the first check created it, the
    # next two created no file, and it stayed at zero bytes. Whether uv
    # rewrites that file in place was not measured, and this test does not
    # depend on the answer: it excludes the path by identity, so a rewrite
    # changes nothing here. uv 0.6.14 creates no file at all. That single
    # uv-owned path is the only difference this test tolerates. Compare it as
    # a Path: str() of a relative path gives a backslash on Windows.
    uv_own_lock = Path(".venv") / ".lock"
    def snapshot():
        files = {}
        for p in directory.rglob("*"):
            relative = p.relative_to(directory)
            if not p.is_file() or relative == uv_own_lock:
                continue
            files[str(relative)] = hashlib.sha256(p.read_bytes()).hexdigest()
        return files
    before = snapshot()
    assert check.check_provider_environments([provider]) == []
    assert snapshot() == before, "a matching check must not change any file"
    lock.write_text(matching + 'dependencies = [{ name = "idna" }]\n'
                    '[[package]]\nname = "idna"\nversion = "3.10"\n'
                    'source = { registry = "https://pypi.org/simple" }\n'
                    'wheels = [{ url = "https://files.pythonhosted.org/packages/idna-3.10-py3-none-any.whl", '
                    'hash = "sha256:' + '0' * 64 + '" }]\n')
    before = snapshot()
    notices = check.check_provider_environments([provider])
    assert len(notices) == 1 and "Fixture Speech" in notices[0]
    assert REMEDY in notices[0]
    assert snapshot() == before, "a missing-package check must not change any file"
