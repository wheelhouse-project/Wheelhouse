"""Mutation gate for wh-mic-loss-notice.

The capture now tells its provider about a lost and a recovered microphone
through ``outage_callback``; each provider hands it a
``shared_stt.mic_notice.MicOutageNotifier``, which turns the two calls into
the ruled on-screen notices; and WheelHouse lets the two new kinds past its
startup suppression while still dropping them from a replaced launch.
Each mutation below breaks one piece of that, or one boss ruling
(D1 the exact words, D3 one call per outage and none from a reminder,
D5 the kinds and the exemption, K1 a notice fault never stops recovery,
K2 no recovery notice without a sent loss notice).

One gate covers four services, because one behaviour crosses them. Each
mutation names its own service directory, and LAUNCH below starts pytest
with THAT service's interpreter:

    shared        services/stt_providers/google_stt_server/.venv (its
                  editable wheelhouse-shared install points at this tree;
                  the shared package has no venv of its own in a worktree)
    parakeet, distil, google
                  the provider's own .venv
    wheelhouse    <main checkout>/services/wheelhouse/.venv, run with the
                  worktree's services/wheelhouse as the working directory
                  (a worktree has no wheelhouse .venv, and pyproject's
                  ``pythonpath = ["."]`` makes the worktree's code the code
                  under test)

Run it from services/stt_providers/shared with any of those interpreters:

    ../google_stt_server/.venv/Scripts/python.exe \\
        tests/mutation_gate_mic_loss_notice.py --check
    ../google_stt_server/.venv/Scripts/python.exe \\
        tests/mutation_gate_mic_loss_notice.py

``--check`` answers the two offline questions -- does every pattern match
exactly once, and does every mutant still parse -- without running a test
or writing a file. It is NOT a sweep. Run the gate in full before the
final commit.

WHY THIS GATE REPLACES ``runner.LAUNCH``: see
tests/mutation_gate_mic_loss_recovery.py. ``uv run`` in a worktree builds
a .venv inside the tree, and that .venv makes the worktree undeletable.

WHY THIS GATE PRINTS THE FAILURE REASONS: a catch counts only when the
expected assertion fired, not an unrelated exception upstream of it
(mutation-gate skill). The runner prints the failed names only, so
``_run_pytest`` is wrapped to echo each short-summary line, reason
included, under the verdict.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent
PARAKEET = PROVIDERS / "sherpa_offline_parakeet_stt_server"
DISTIL = PROVIDERS / "distil_medium_en"
GOOGLE = PROVIDERS / "google_stt_server"
ROOT = PROVIDERS.parents[1]
WHEELHOUSE = ROOT / "services" / "wheelhouse"

WINRT_CAPTURE = SHARED / "shared_audio" / "capture" / "winrt_capture.py"
FACTORY = SHARED / "shared_audio" / "capture" / "factory.py"
MIC_NOTICE = SHARED / "shared_stt" / "mic_notice.py"
WEBSOCKET_MANAGER = WHEELHOUSE / "integrations" / "websocket_manager.py"


def _main_checkout(root: Path) -> Path:
    """The main checkout's root, read from a worktree's .git link file.

    In the main checkout itself .git is a directory and root is the answer.
    In a worktree .git is a file reading ``gitdir: <main>/.git/worktrees/<n>``.
    """
    link = root / ".git"
    if link.is_file():
        text = link.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            return Path(text[len("gitdir:"):].strip()).resolve().parents[2]
    return root


_VENV_PYTHON = Path(".venv") / "Scripts" / "python.exe"
INTERPRETERS = {
    SHARED: GOOGLE / _VENV_PYTHON,
    PARAKEET: PARAKEET / _VENV_PYTHON,
    DISTIL: DISTIL / _VENV_PYTHON,
    GOOGLE: GOOGLE / _VENV_PYTHON,
    WHEELHOUSE: _main_checkout(ROOT) / "services" / "wheelhouse" / _VENV_PYTHON,
}


def _launch(service, test_file, report, collect):
    """Start pytest with the service's own interpreter, never ``uv run``."""
    python = INTERPRETERS[service]
    if not python.is_file():
        # Raised as OSError so the runner reports an error, never a verdict.
        raise OSError(f"no interpreter for {service.name}: {python}")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_plain_run_pytest = runner._run_pytest


def _run_pytest_showing_reasons(service, test_file):
    """The runner's _run_pytest, echoing each failure's reason line."""
    result = _plain_run_pytest(service, test_file)
    out = result.stdout + result.stderr
    summary = out.split("short test summary info", 1)
    if len(summary) == 2:
        for line in summary[1].splitlines():
            if line.startswith(("FAILED", "ERROR")):
                print(f"    reason: {line[:400]}")
    return result


runner._run_pytest = _run_pytest_showing_reasons


# --------------------------------------------------------------------------
# Test selections and catchers
# --------------------------------------------------------------------------

CAPTURE_TESTS = (
    "tests/test_winrt_capture.py::TestMicrophoneOutageCallback",
    "tests/test_winrt_capture.py::TestMicrophoneLossRecovery",
)
FACTORY_TESTS = "tests/test_audio_capture_factory.py"
NOTICE_TESTS = "tests/test_mic_notice.py"
PROVIDER_TESTS = "tests/test_mic_outage_notice.py"
WHEELHOUSE_TESTS = (
    "tests/test_websocket_manager.py::TestNotificationHandling",
    "tests/test_launch_generation.py::"
    "TestAReplacedLaunchsFailureLeavesTheDisplayAlone",
)

# tests/test_winrt_capture.py, class TestMicrophoneOutageCallback
LOSS_AND_RECOVERY = "test_a_loss_and_its_recovery_each_call_the_callback_once"
LOSS_BEFORE_RETURN = "test_the_loss_call_comes_before_the_microphone_returns"
NO_REMINDER_CALLS = "test_reminders_never_call_the_callback"
RAISING_CALLBACK = "test_a_raising_callback_is_logged_and_recovery_completes"

# tests/test_audio_capture_factory.py
FACTORY_PASSES = "test_passes_the_outage_callback_through"

# tests/test_mic_notice.py
LOSS_TEXT = "test_the_loss_text_is_the_ruled_text"
RECOVERY_DEVICE_TEXT = "test_the_recovery_text_names_the_device"
RECOVERY_PLAIN_TEXT = "test_the_recovery_text_without_a_device"
KINDS = "test_the_kinds"
PAIR_SENT = "test_a_loss_and_its_recovery_each_send_one_notice"
PLAIN_SENT = "test_a_recovery_with_no_device_sends_the_plain_text"
NO_FORWARDER = "test_no_forwarder_sends_nothing_and_does_not_raise"
READ_AT_CALL = "test_the_forwarder_is_read_at_call_time"
K2_SILENT_LOSS = "test_k2_a_loss_with_no_forwarder_sends_no_recovery"
K2_NEXT_OUTAGE = "test_k2_the_next_outage_after_a_silent_one_is_announced"
ONE_RECOVERY = "test_one_recovery_per_announced_loss"
FAILED_LOSS_SEND = "test_a_failed_loss_send_is_not_counted_as_handed_over"

# each provider's tests/test_mic_outage_notice.py
GIVEN_CALLBACK = "test_the_capture_is_given_an_outage_callback"
SENDS_WITH_TITLE = "test_the_callback_sends_both_notices_with_the_display_title"
GOOGLE_SENDS = "test_the_callback_sends_both_notices_through_the_forwarder"
GOOGLE_LIST_DEVICES = (
    "test_a_list_devices_run_has_no_forwarder_and_sends_nothing")

# services/wheelhouse tests
TOASTS_DURING_STARTUP = "test_microphone_notices_toast_even_during_startup"
CURRENT_LAUNCH_TOASTS = (
    "test_the_current_launchs_microphone_notice_still_toasts")
REPLACED_LAUNCH_DROPPED = (
    "test_a_replaced_launchs_microphone_notice_does_not_toast")


def _mutation(name, service, test_file, file, old, new, *expect):
    return {
        "name": name,
        "service": service,
        "test_file": test_file,
        "file": file,
        "old": old,
        "new": new,
        "expect": list(expect),
    }


def _capture(name, old, new, *expect):
    return _mutation(name, SHARED, CAPTURE_TESTS, WINRT_CAPTURE,
                     old, new, *expect)


def _notice(name, old, new, *expect):
    return _mutation(name, SHARED, NOTICE_TESTS, MIC_NOTICE,
                     old, new, *expect)


# --------------------------------------------------------------------------
# The capture: one "lost" call per outage, one "recovered" call with the
# device, none from a reminder, and a raising callback contained (K1)
# --------------------------------------------------------------------------

CAPTURE_MUTATIONS = [
    _capture(
        "the-loss-is-never-reported",
        "                outage = _Outage(reason)\n"
        "                self._report_outage('lost', None)\n",
        "                outage = _Outage(reason)\n"
        "                pass\n",
        LOSS_AND_RECOVERY, LOSS_BEFORE_RETURN, NO_REMINDER_CALLS,
        RAISING_CALLBACK,
    ),
    _capture(
        "the-recovery-is-never-reported",
        "                        self._report_outage('recovered', "
        "recovering.device)\n",
        "                        pass\n",
        LOSS_AND_RECOVERY, RAISING_CALLBACK,
    ),
    # Ruling C4 carried to the notice: the device is known and not passed.
    _capture(
        "the-recovery-report-drops-the-device",
        "                        self._report_outage('recovered', "
        "recovering.device)\n",
        "                        self._report_outage('recovered', None)\n",
        LOSS_AND_RECOVERY,
    ),
    # Ruling D3: a reminder is a log line only. Two reminder sites exist:
    # a graph that built but never delivered (the poll loop), and a rebuild
    # that raised (the retry loop). Each gets its own mutation.
    _capture(
        "a-reminder-for-a-silent-graph-reports-a-loss",
        "                outage.last_error = reason\n"
        "                outage.remind()\n",
        "                outage.last_error = reason\n"
        "                outage.remind()\n"
        "                self._report_outage('lost', None)\n",
        NO_REMINDER_CALLS,
    ),
    _capture(
        "a-reminder-for-a-failed-rebuild-reports-a-loss",
        "                outage.remind()\n"
        "                if stop_event.wait(REBUILD_RETRY_SECONDS):\n",
        "                outage.remind()\n"
        "                self._report_outage('lost', None)\n"
        "                if stop_event.wait(REBUILD_RETRY_SECONDS):\n",
        LOSS_BEFORE_RETURN,
    ),
    # Ruling K1: the try/except removed. The except body stays behind an
    # `if False:` so the mutant still parses; a raising callback then
    # propagates out of _report_outage into the capture thread.
    _capture(
        "a-raising-callback-is-not-contained",
        "        try:\n"
        "            callback(event, device)\n"
        "        except Exception as e:\n",
        "        callback(event, device)\n"
        "        if False:\n",
        RAISING_CALLBACK,
    ),
]


# --------------------------------------------------------------------------
# The factory passes the callback on
# --------------------------------------------------------------------------

FACTORY_MUTATIONS = [
    _mutation(
        "the-factory-drops-the-outage-callback",
        SHARED, FACTORY_TESTS, FACTORY,
        "    return WinRTAudioCapture(config, overflow_callback, "
        "outage_callback)\n",
        "    return WinRTAudioCapture(config, overflow_callback, None)\n",
        FACTORY_PASSES,
    ),
]


# --------------------------------------------------------------------------
# shared_stt/mic_notice.py: the ruled words (D1), the kinds (D5), and the
# rules the notifier keeps (K2, no forwarder, one recovery per loss)
# --------------------------------------------------------------------------

NOTICE_MUTATIONS = [
    _notice(
        "the-loss-text-changes",
        '    "Microphone lost. Speech recognition is waiting for it to '
        'come back.")\n',
        '    "Microphone lost. Speech recognition is waiting for it.")\n',
        LOSS_TEXT,
    ),
    _notice(
        "the-plain-recovery-text-changes",
        'MIC_RECOVERED_MESSAGE = "Microphone is back. Speech recognition '
        'works again."\n',
        'MIC_RECOVERED_MESSAGE = "Microphone is back. Speech recognition '
        'works."\n',
        RECOVERY_PLAIN_TEXT, PLAIN_SENT,
    ),
    _notice(
        "the-device-recovery-text-changes",
        '    "Microphone is back ({device}). Speech recognition works '
        'again.")\n',
        '    "Microphone is back on {device}. Speech recognition works '
        'again.")\n',
        RECOVERY_DEVICE_TEXT, PAIR_SENT,
    ),
    # The device text used with no device, and the plain text with one.
    _notice(
        "the-recovery-text-ignores-the-device",
        "    if device:\n"
        "        return MIC_RECOVERED_ON_DEVICE_MESSAGE.format(device=device)\n",
        "    if False:\n"
        "        return MIC_RECOVERED_ON_DEVICE_MESSAGE.format(device=device)\n",
        RECOVERY_DEVICE_TEXT, PAIR_SENT,
    ),
    _notice(
        "the-lost-kind-changes",
        'MIC_LOST_KIND = "mic_lost"\n',
        'MIC_LOST_KIND = "mic_loss"\n',
        KINDS, PAIR_SENT,
    ),
    _notice(
        "the-recovered-kind-changes",
        'MIC_RECOVERED_KIND = "mic_recovered"\n',
        'MIC_RECOVERED_KIND = "mic_restored"\n',
        KINDS, PAIR_SENT,
    ),
    # Ruling K2 removed: a recovery goes out with no sent loss before it.
    _notice(
        "k2-a-recovery-is-sent-without-a-sent-loss",
        "        elif event == OUTAGE_RECOVERED:\n"
        "            if not self._loss_announced:\n",
        "        elif event == OUTAGE_RECOVERED:\n"
        "            if False:\n",
        K2_SILENT_LOSS, K2_NEXT_OUTAGE, ONE_RECOVERY, FAILED_LOSS_SEND,
    ),
    # The loss is recorded as sent before the send, so a send that raises
    # still unlocks the recovery notice.
    _notice(
        "a-loss-counts-as-sent-before-the-send",
        "            forwarder = self._get_forwarder()\n"
        "            if forwarder is None:\n"
        "                return\n"
        "            # crewcut: no rate limit.",
        "            self._loss_announced = True\n"
        "            forwarder = self._get_forwarder()\n"
        "            if forwarder is None:\n"
        "                return\n"
        "            # crewcut: no rate limit.",
        K2_SILENT_LOSS, K2_NEXT_OUTAGE, FAILED_LOSS_SEND,
    ),
    # The flag is not cleared when a new outage begins, so an outage whose
    # loss notice was NOT sent inherits the previous outage's "sent".
    _notice(
        "the-sent-flag-is-not-reset-by-a-new-loss",
        "        if event == OUTAGE_LOST:\n"
        "            self._loss_announced = False\n",
        "        if event == OUTAGE_LOST:\n"
        "            pass\n",
        "test_a_new_outage_whose_loss_was_not_sent_sends_no_recovery",
    ),
    # The flag is not cleared by the recovery it allowed.
    _notice(
        "the-sent-flag-is-not-reset-by-its-recovery",
        "                return\n"
        "            self._loss_announced = False\n"
        "            forwarder = self._get_forwarder()\n",
        "                return\n"
        "            pass\n"
        "            forwarder = self._get_forwarder()\n",
        ONE_RECOVERY,
    ),
    # The "no forwarder -> no send" guards. Without them the notifier
    # calls send_notification on None and raises; the tests that say the
    # call "does not raise" are the ones that must see it.
    _notice(
        "a-loss-with-no-forwarder-is-sent-to-none",
        "            forwarder = self._get_forwarder()\n"
        "            if forwarder is None:\n"
        "                return\n"
        "            # crewcut: no rate limit.",
        "            forwarder = self._get_forwarder()\n"
        "            if False:\n"
        "                return\n"
        "            # crewcut: no rate limit.",
        NO_FORWARDER,
    ),
    _notice(
        "a-recovery-with-no-forwarder-is-sent-to-none",
        "            if forwarder is None:\n"
        "                return\n"
        "            forwarder.send_notification(\n"
        "                self._title, recovery_message(device),\n",
        "            if False:\n"
        "                return\n"
        "            forwarder.send_notification(\n"
        "                self._title, recovery_message(device),\n",
        "test_a_forwarder_gone_by_the_recovery_sends_nothing",
    ),
    # The forwarder read once, at construction, rather than at each call.
    _notice(
        "the-forwarder-is-read-once-at-construction",
        "        self._get_forwarder = get_forwarder\n",
        "        _first = get_forwarder()\n"
        "        self._get_forwarder = lambda: _first\n",
        READ_AT_CALL, K2_NEXT_OUTAGE,
    ),
]


# --------------------------------------------------------------------------
# Each provider hands its capture a notifier that reads its forwarder
# --------------------------------------------------------------------------

PROVIDER_MUTATIONS = [
    _mutation(
        "parakeet-passes-no-outage-callback",
        PARAKEET, PROVIDER_TESTS, PARAKEET / "main.py",
        "                config=audio_config, outage_callback=outage_notifier)\n",
        "                config=audio_config)\n",
        GIVEN_CALLBACK, SENDS_WITH_TITLE,
    ),
    _mutation(
        "parakeet-notifier-never-finds-the-forwarder",
        PARAKEET, PROVIDER_TESTS, PARAKEET / "main.py",
        "            self.display_name, lambda: getattr(self, 'forwarder', "
        "None))\n",
        "            self.display_name, lambda: None)\n",
        SENDS_WITH_TITLE,
    ),
    _mutation(
        "distil-passes-no-outage-callback",
        DISTIL, PROVIDER_TESTS, DISTIL / "main.py",
        "                config=audio_config, outage_callback=outage_notifier)\n",
        "                config=audio_config)\n",
        GIVEN_CALLBACK, SENDS_WITH_TITLE,
    ),
    _mutation(
        "distil-notifier-never-finds-the-forwarder",
        DISTIL, PROVIDER_TESTS, DISTIL / "main.py",
        "            self.DISPLAY_NAME, lambda: getattr(self, 'forwarder', "
        "None))\n",
        "            self.DISPLAY_NAME, lambda: None)\n",
        SENDS_WITH_TITLE,
    ),
    _mutation(
        "google-passes-no-outage-callback",
        GOOGLE, PROVIDER_TESTS, GOOGLE / "main.py",
        "                                 overflow_callback="
        "on_overflow_detected,\n"
        "                                 outage_callback=outage_notifier)\n",
        "                                 overflow_callback="
        "on_overflow_detected)\n",
        GOOGLE_SENDS, GOOGLE_LIST_DEVICES,
    ),
    _mutation(
        "google-notifier-never-finds-the-forwarder",
        GOOGLE, PROVIDER_TESTS, GOOGLE / "main.py",
        '    outage_notifier = MicOutageNotifier("Google STT", '
        'lambda: forwarder)\n',
        '    outage_notifier = MicOutageNotifier("Google STT", '
        'lambda: None)\n',
        GOOGLE_SENDS,
    ),
]


# --------------------------------------------------------------------------
# WheelHouse: both kinds exempt from the startup suppression (D5), and a
# replaced launch's notices still dropped
# --------------------------------------------------------------------------

def _wheelhouse(name, old, new, *expect):
    return _mutation(name, WHEELHOUSE, WHEELHOUSE_TESTS, WEBSOCKET_MANAGER,
                     old, new, *expect)


WHEELHOUSE_MUTATIONS = [
    _wheelhouse(
        "mic-lost-is-not-exempt",
        '    "startup_failed", "error", "mic_lost", "mic_recovered")\n',
        '    "startup_failed", "error", "mic_recovered")\n',
        TOASTS_DURING_STARTUP, CURRENT_LAUNCH_TOASTS,
    ),
    _wheelhouse(
        "mic-recovered-is-not-exempt",
        '    "startup_failed", "error", "mic_lost", "mic_recovered")\n',
        '    "startup_failed", "error", "mic_lost")\n',
        TOASTS_DURING_STARTUP, CURRENT_LAUNCH_TOASTS,
    ),
    # The replaced-launch drop skips the two microphone kinds only, so a
    # replaced launch's microphone notice rides the exemption onto the
    # screen. startup_failed and error keep the check, which isolates the
    # proof to the kinds this branch added.
    _wheelhouse(
        "a-replaced-launchs-microphone-notice-is-not-dropped",
        "                        if kind in _STARTUP_SUPPRESSION_EXEMPT_KINDS "
        "and not (\n",
        "                        if kind in _STARTUP_SUPPRESSION_EXEMPT_KINDS "
        "and kind not in (\"mic_lost\", \"mic_recovered\") and not (\n",
        REPLACED_LAUNCH_DROPPED,
    ),
]


MUTATIONS = (
    CAPTURE_MUTATIONS
    + FACTORY_MUTATIONS
    + NOTICE_MUTATIONS
    + PROVIDER_MUTATIONS
    + WHEELHOUSE_MUTATIONS
)


if __name__ == "__main__":
    sys.exit(runner.run(MUTATIONS))
