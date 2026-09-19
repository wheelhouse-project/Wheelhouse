"""Mutation gate for the wh-provider-ready-handshake guard tests.

Proves each guard test fails for the right reason when the behaviour it
protects is broken. Run from services/stt_providers/shared with plain
python (the gate needs only the standard library; it invokes `uv run
pytest` as a subprocess, in each provider's own service directory):

    python tests/mutation_gate_provider_ready_handshake.py
    python tests/mutation_gate_provider_ready_handshake.py google
    python tests/mutation_gate_provider_ready_handshake.py --check

The defect this branch fixed was the same on all three shipped
providers: the ready notice went out on a timer that knew nothing about
the microphone. A denied or missing device produced "Transcription
service ready" while every spoken word was discarded, and on parakeet
and distil an unconditional "Audio capture started" log line said the
same thing to whoever read wheelhouse.log afterwards.

So the mutations mostly restore parts of that defect: the handshake not
consulted, the failure notice sent with the ready kind, the reason
dropped from the message or the log, the capture-started line moved back
ahead of the handshake, and the announcement started before capture.
Each one is a state the code was actually in before this branch.

Google differs in three ways and its mutations follow: it has no
"Audio capture started" line to move, its call site already ran after
mic.start(), and it has a credentials preflight whose failure must keep
precedence over a capture failure. The last of those is behaviour the
other two providers do not have at all.

The last group mutates the runner's own --check mode, which this branch
added. A check that reports clean while a pattern is stale, or while a
mutant cannot compile, is worse than no check: every later round quotes
the clean line.

Runner discipline lives in tests/mutation_gate_runner.py.
"""
import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path, but this keeps it working when the gate
# is invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent

PARAKEET = PROVIDERS / "sherpa_offline_parakeet_stt_server"
DISTIL = PROVIDERS / "distil_medium_en"
GOOGLE = PROVIDERS / "google_stt_server"

READINESS_TESTS = "tests/test_startup_readiness.py"
RUNNER_TESTS = "tests/test_mutation_gate_runner.py"
RUNNER = SHARED / "tests" / "mutation_gate_runner.py"


def _provider_mutations(label, service, indent="        "):
    """The mutations parakeet and distil share.

    Both hold the announcement as a method on their server class, so the
    snippets differ only in the label. Google is written out separately
    below: its function is module-level, takes the capture provider as an
    argument, and has no capture-started line.
    """
    main = service / "main.py"
    i = indent
    # The announcement body sits one level deeper than the method since
    # wh-provider-ready-handshake.1.2 wrapped it in
    # "with self._notification_lock:". Patterns inside that block quote b;
    # the wait, cleanup's own lines and the docstring end still quote i.
    b = indent + "    "
    return [
        {
            # The whole defect in one line: announce without asking.
            "name": f"{label}-handshake-not-consulted",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": f"{i}ready = self.audio_capture.wait_ready()",
            "new": f"{i}ready = True",
            "expect": ["test_the_handshake_is_actually_called",
                       "test_a_failed_capture_never_gets_the_ready_notice",
                       "test_a_failed_capture_sends_startup_failed"],
        },
        {
            # A number here can disagree with the provider's own default,
            # and the disagreement is invisible until a slow device.
            "name": f"{label}-wait-gets-a-local-timeout",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": f"{i}ready = self.audio_capture.wait_ready()",
            "new": f"{i}ready = self.audio_capture.wait_ready(timeout=1.0)",
            "expect": ["test_the_wait_uses_the_handshakes_own_timeout"],
        },
        {
            # The shutdown check removed, which is the state the code
            # was in before wh-provider-ready-handshake.1.1: an
            # intentional stop landing inside the wait released it as
            # False, and the announcement then sent a startup failure
            # for a stop the user had asked for. "if False:" rather
            # than deleting the block, because deleting the only
            # statements of an if-body leaves a construct that does not
            # compile, and a mutant that does not compile reads as
            # caught while proving nothing (mutation-gate skill).
            #
            # The next line is part of the pattern because the match is
            # a plain substring count: process_audio_loop's own
            # "if not self.running:" is indented deeper, and the deeper
            # line CONTAINS this one, so the bare line matches twice.
            "name": f"{label}-shutdown-check-removed",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": (f"{b}if not self.running:\n"
                    f"{b}    logger.info(\n"
                    f'{b}        "Server is stopping - no startup '
                    f'notification "\n'
                    f'{b}        "will be sent")'),
            "new": (f"{b}if False:\n"
                    f"{b}    logger.info(\n"
                    f'{b}        "Server is stopping - no startup '
                    f'notification "\n'
                    f'{b}        "will be sent")'),
            "expect": ["test_a_stop_during_the_wait_sends_no_failure_notice",
                       "test_a_stop_during_the_wait_sends_no_ready_notice"],
        },
        {
            # stop() sets the flag too, so a gate that only drove stop()
            # would report this caught by accident. The test calls
            # cleanup() directly, which is the path run()'s try/finally
            # takes when process_audio_loop raises.
            "name": f"{label}-cleanup-leaves-running-true",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            # The "with" line is part of the pattern because the write
            # moved under it. Quoting the write alone would still match,
            # four characters into the deeper indent, and the runner
            # rejects that (mutation_gate_runner._line_start_problem).
            "old": (f"{i}with self._notification_lock:\n"
                    f"{b}self.running = False\n"
                    f'{i}logger.info("Cleaning up...")'),
            "new": f'{i}logger.info("Cleaning up...")',
            "expect": ["test_cleanup_marks_the_server_stopped"],
        },
        {
            # The serialization itself, removed from the writing side.
            # cleanup() still records the stop, so
            # test_cleanup_marks_the_server_stopped stays green and this
            # is not a second copy of the mutation above: what goes is
            # only the lock, which is what lets cleanup() run between the
            # announcement's read of the flag and its send.
            #
            # The catching tests pause the announcement AFTER the flag is
            # read and BEFORE the send. A test that paused earlier would
            # be caught by the shutdown check alone and would report this
            # mutation caught while proving nothing about the lock.
            "name": f"{label}-notification-lock-removed",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": (f"{i}with self._notification_lock:\n"
                    f"{b}self.running = False\n"
                    f'{i}logger.info("Cleaning up...")'),
            "new": (f"{i}self.running = False\n"
                    f'{i}logger.info("Cleaning up...")'),
            "expect": [
                "test_the_ready_notice_is_never_sent_into_a_stopping"
                "_server",
                "test_the_failure_notice_is_never_sent_into_a_stopping"
                "_server"],
        },
        {
            # kind is what WheelHouse routes on: "error" is a running
            # service's problem, "startup_failed" is a start that failed.
            "name": f"{label}-failure-kind-dropped",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": (f'{b}    f"Failed to start - transcription will not '
                    f'work: {{detail}}",\n'
                    f'{b}    kind="startup_failed",'),
            "new": (f'{b}    f"Failed to start - transcription will not '
                    f'work: {{detail}}",\n'
                    f'{b}    kind="error",'),
            "expect": ["test_a_failed_capture_sends_startup_failed"],
        },
        {
            # "Failed to start" with no reason tells the user nothing
            # they can act on.
            "name": f"{label}-failure-reason-dropped",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": (f'{b}    f"Failed to start - transcription will not '
                    f'work: {{detail}}",'),
            "new": (f'{b}    "Failed to start - transcription will not '
                    f'work",'),
            "expect": ["test_the_failure_notice_names_the_cause"],
        },
        {
            # The log line is what an operator reads afterwards; without
            # the reason it says only that something went wrong.
            "name": f"{label}-failure-log-drops-the-reason",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": f'{b}logger.error(f"Audio capture is not ready: '
                   f'{{detail}}")',
            "new": f'{b}logger.error("Audio capture is not ready")',
            "expect": ["test_a_failed_handshake_logs_the_failure_instead"],
        },
        {
            # A MOVE, not a copy: the line leaves the success path and
            # lands ahead of the handshake, which is where it used to be.
            # An added copy would leave the original in place and the
            # placement test would pass while the gate reported a
            # survivor (mutation-gate skill).
            "name": f"{label}-capture-started-line-moves-back",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            # The whole region is the pattern because the move's two
            # ends are no longer adjacent: the shutdown check sits
            # between them, and one replacement cannot edit two
            # disjoint places.
            "old": (f"{i}ready = self.audio_capture.wait_ready()\n"
                    f"{i}with self._notification_lock:\n"
                    f"{b}if not self.running:\n"
                    f"{b}    logger.info(\n"
                    f'{b}        "Server is stopping - no startup '
                    f'notification "\n'
                    f'{b}        "will be sent")\n'
                    f"{b}    return\n"
                    f"\n"
                    f"{b}if ready:\n"
                    f'{b}    logger.info("Audio capture started")\n'
                    f'{b}    logger.info("Sending \'ready\' notification '
                    f'to Wheelhouse...")\n'),
            "new": (f'{i}logger.info("Audio capture started")\n'
                    f"{i}ready = self.audio_capture.wait_ready()\n"
                    f"{i}with self._notification_lock:\n"
                    f"{b}if not self.running:\n"
                    f"{b}    logger.info(\n"
                    f'{b}        "Server is stopping - no startup '
                    f'notification "\n'
                    f'{b}        "will be sent")\n'
                    f"{b}    return\n"
                    f"\n"
                    f"{b}if ready:\n"
                    f'{b}    logger.info("Sending \'ready\' notification '
                    f'to Wheelhouse...")\n'),
            "expect": ["test_the_line_is_not_written_for_a_failed_handshake"],
        },
        {
            # The 3.0 second timer, back where it was. The import is
            # aliased and function-local on purpose: a bare "import time"
            # inside the function would bind the name time as a local of
            # that function and every earlier use would raise
            # UnboundLocalError before the sleep ran, which is a false
            # catch (mutation-gate skill). distil no longer imports time
            # at module level at all, so the local import is also what
            # makes this mutation apply to both providers unchanged.
            "name": f"{label}-announcement-sleeps-again",
            "service": service,
            "test_file": READINESS_TESTS,
            "file": main,
            "old": (f'{i}"""\n'
                    f"{i}ready = self.audio_capture.wait_ready()"),
            "new": (f'{i}"""\n'
                    f"{i}from time import sleep as _sleep\n"
                    f"{i}_sleep(3.0)\n"
                    f"{i}ready = self.audio_capture.wait_ready()"),
            "expect": ["test_the_announcement_does_not_sleep"],
        },
    ]


MUTATIONS = _provider_mutations("parakeet", PARAKEET) + [
    {
        # The original order. wait_ready() asked before start() is a
        # question about a provider nobody has opened yet.
        "name": "parakeet-announcement-before-capture-start",
        "service": PARAKEET,
        "test_file": READINESS_TESTS,
        "file": PARAKEET / "main.py",
        "old": ("        self.audio_capture.start()\n"
                "\n"
                "        # After start(), never before: wait_ready() is an "
                "answer about\n"
                "        # a capture provider that has been asked to open. "
                "The\n"
                '        # "Audio capture started" log line moved inside '
                "the\n"
                "        # announcement, so it is written only once the "
                "handshake proves\n"
                "        # the microphone works (wh-provider-ready-handshake "
                "criterion 3).\n"
                "        self._send_startup_notification()"),
        "new": ("        self._send_startup_notification()\n"
                "\n"
                "        self.audio_capture.start()"),
        "expect": ["test_capture_starts_before_the_announcement"],
    },
] + _provider_mutations("distil", DISTIL) + [
    {
        "name": "distil-announcement-before-capture-start",
        "service": DISTIL,
        "test_file": READINESS_TESTS,
        "file": DISTIL / "main.py",
        "old": ("        self.audio_capture.start()\n"
                "\n"
                "        # After start(), never before: wait_ready() is an "
                "answer about\n"
                "        # a capture provider that has been asked to open. "
                "The\n"
                '        # "Audio capture started" log line moved inside '
                "the\n"
                "        # announcement, so it is written only once the "
                "handshake proves\n"
                "        # the microphone works (wh-provider-ready-handshake "
                "criterion 3).\n"
                "        self._send_startup_notification()"),
        "new": ("        self._send_startup_notification()\n"
                "\n"
                "        self.audio_capture.start()"),
        "expect": ["test_capture_starts_before_the_announcement"],
    },

    # -- google -----------------------------------------------------------
    {
        "name": "google-handshake-not-consulted",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": "    ready = mic.wait_ready()",
        "new": "    ready = True",
        "expect": ["test_the_handshake_is_actually_called",
                   "test_a_failed_capture_never_gets_the_ready_notice",
                   "test_a_failed_capture_sends_startup_failed"],
    },
    {
        "name": "google-wait-gets-a-local-timeout",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": "    ready = mic.wait_ready()",
        "new": "    ready = mic.wait_ready(timeout=1.0)",
        "expect": ["test_the_wait_uses_the_handshakes_own_timeout"],
    },
    {
        # google's half of wh-provider-ready-handshake.1.1. It has no
        # self.running -- main() keeps a local -- so the announcement
        # thread is handed a threading.Event that main()'s finally sets
        # before mic.stop(). Removing the read restores the state where
        # an intentional stop landing inside the wait produced a startup
        # failure notice for a stop the user had asked for.
        "name": "google-shutdown-check-removed",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ("    if shutting_down.is_set():\n"
                "        return None"),
        "new": ("    if False:\n"
                "        return None"),
        "expect": ["test_a_stop_during_the_wait_sends_no_failure_notice",
                   "test_a_stop_during_the_wait_sends_no_ready_notice"],
    },
    {
        "name": "google-failure-reason-dropped",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ('        "Failed to start - transcription will not work: " '
                "+ detail,"),
        "new": '        "Failed to start - transcription will not work",',
        "expect": ["test_the_failure_notice_names_the_cause"],
    },
    {
        # The neighbouring line is part of the pattern because
        # '        "startup_failed",' on its own also matches
        # startup_notification's credentials failure above.
        "name": "google-failure-kind-dropped",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ('        "Failed to start - transcription will not work: " '
                "+ detail,\n"
                '        "startup_failed",'),
        "new": ('        "Failed to start - transcription will not work: " '
                "+ detail,\n"
                '        "ready",'),
        "expect": ["test_a_failed_capture_sends_startup_failed"],
    },
    {
        # WheelHouse routes on the title, so a capture failure under a
        # different one is a notice the user may never see beside the
        # provider's others.
        "name": "google-failure-title-changed",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ("    return (\n"
                '        "Google STT",\n'
                '        "Failed to start - transcription will not work: " '
                "+ detail,"),
        "new": ("    return (\n"
                '        "Google Speech",\n'
                '        "Failed to start - transcription will not work: " '
                "+ detail,"),
        "expect": ["test_the_notice_keeps_the_providers_own_title"],
    },
    {
        "name": "google-failure-log-drops-the-reason",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": '    logger.error(f"[startup] Audio capture is not ready: '
               '{detail}")',
        "new": '    logger.error("[startup] Audio capture is not ready")',
        "expect": ["test_the_failure_is_logged"],
    },
    {
        # A provider with no client will not transcribe whatever the
        # microphone does, so the credentials message is the one to act
        # on.
        #
        # The "does not wait on capture" test is NOT in this expect list,
        # and was removed from it when the sweep reported this mutation
        # as a survivor. The two halves of the precedence live in two
        # functions since wh-provider-ready-handshake.1.2: this check
        # decides WHICH message goes out, and capture_handshake's own
        # check decides whether the wait happens at all. Disabling this
        # one no longer changes whether the wait runs, so that test
        # cannot fail under it, and naming it here would have reported a
        # real catch as a survivor for ever. The companion entry below
        # pins the other half.
        "name": "google-credentials-precedence-lost",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ("    if startup_error is not None:\n"
                "        return startup_notification(startup_error)"),
        "new": ("    if False:\n"
                "        return startup_notification(startup_error)"),
        "expect": [
            "test_a_credentials_error_is_reported_not_the_capture_error",
            "test_a_credentials_error_with_a_working_mic_still_fails"],
    },
    {
        # The other half: the wait itself is skipped when there is no
        # client. Without this the provider spends the full 15 second
        # handshake before it can report a failure it already knew
        # about.
        #
        # The next line is part of the pattern because
        # startup_notification_after_capture opens with the same line at
        # the same indent; "return None" is what makes this one
        # capture_handshake's.
        "name": "google-credentials-error-still-waits",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ("    if startup_error is not None:\n"
                "        return None"),
        "new": ("    if False:\n"
                "        return None"),
        "expect": ["test_a_credentials_error_does_not_wait_on_capture"],
    },
    {
        # google keeps its module-level time import, so this one needs no
        # local import.
        "name": "google-sender-sleeps-again",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        # The sleep goes ahead of the handshake, which is the only
        # place it can go now: the decision moved under the notification
        # lock, and a sleep there would hold the lock for three seconds
        # and stall any shutdown that asked for it.
        "old": "    ready = capture_handshake(startup_error, mic)",
        "new": ("    time.sleep(3.0)\n"
                "    ready = capture_handshake(startup_error, mic)"),
        "expect": ["test_it_does_not_sleep"],
    },
    {
        # The serialization itself, removed from the writing side.
        # begin_shutdown still records the stop, so nothing that reads
        # the event afterwards changes; what goes is only the lock that
        # stops the record from landing between the sender's read of it
        # and the hand-off of the notice to the forwarder's loop.
        "name": "google-notification-lock-removed",
        "service": GOOGLE,
        "test_file": READINESS_TESTS,
        "file": GOOGLE / "main.py",
        "old": ("    with notification_lock:\n"
                "        shutting_down.set()"),
        "new": "    shutting_down.set()",
        "expect": [
            "test_the_ready_notice_is_never_sent_into_a_stopping_server",
            "test_the_failure_notice_is_never_sent_into_a_stopping"
            "_server"],
    },

    # -- the runner's own --check mode ------------------------------------
    {
        # The failure that costs the most: --check falls through into the
        # full run, so the answer meant to take seconds takes the hours
        # the check exists to save.
        "name": "check-falls-through-into-the-run",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ("    if check_only:\n"
                "        return _check(selected, len(mutations), skipped)"),
        "new": ("    if False:\n"
                "        return _check(selected, len(mutations), skipped)"),
        "expect": ["test_the_check_runs_no_tests"],
    },
    {
        # Re-quoted 2026-09-05 (wh-parakeet-hotword-vocab.3): the suffix
        # test moved out of _check and into _syntax_problem, which now
        # answers for Python and PowerShell both. The behaviour these two
        # protect is unchanged, so the mutations move with the code rather
        # than being deleted.
        "name": "check-skips-the-compile",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('    if path.suffix == ".py":\n'
                "        try:\n"),
        "new": ("    if False:\n"
                "        try:\n"),
        "expect": ["test_a_mutant_that_does_not_compile_fails_the_check",
                   "test_stale_and_non_compiling_are_counted_apart"],
    },
    {
        "name": "check-compiles-a-document",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('    if path.suffix == ".py":\n'
                "        try:\n"),
        "new": ("    if True:\n"
                "        try:\n"),
        "expect": ["test_a_non_python_target_is_not_compiled"],
    },
    {
        # The PowerShell arm, added with it. Without this check a .ps1
        # mutant that does not parse fails every test in its selection --
        # including the ones the mutation names -- and reads as caught.
        "name": "check-skips-the-powershell-parse",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('    if path.suffix == ".ps1":\n'
                "        exe = shutil.which(\"powershell\") or shutil.which(\"pwsh\")\n"),
        "new": ("    if False:\n"
                "        exe = shutil.which(\"powershell\") or shutil.which(\"pwsh\")\n"),
        "expect": ["test_a_powershell_mutant_that_does_not_parse_fails_the_check"],
    },
    {
        # The rule that catches a pattern matching four characters into a
        # deeper indent. "problem = None" rather than deleting the block,
        # because deleting the only statements of a block leaves a
        # construct that does not compile, and a mutant that does not
        # compile reads as caught while proving nothing.
        "name": "check-ignores-a-mid-indent-match",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ("        problem = _line_start_problem(data, old)\n"
                "        if problem:\n"
                '            stale.append(m["name"])\n'
                "            print(f\"ERROR {m['name']}: {problem}\")\n"
                "            continue"),
        "new": "        problem = None",
        "expect": ["test_a_match_inside_a_deeper_indent_fails_the_check"],
    },
    {
        # A second match is the hardest gate failure to see: replace()
        # edits the first, so the mutation lands where its name does not
        # claim and the round reports a survivor for untouched code.
        #
        # The stale.append line is part of the pattern because the count
        # test and its problem message are byte-identical in _check and
        # in the run loop. The gate's own --check reported that as
        # "pattern ambiguous (2 matches)" on this entry's first run,
        # which is the failure mode this entry is about.
        "name": "check-never-counts-a-second-match",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ("        if count != 1:\n"
                '            problem = ("pattern not found" if count == 0\n'
                "                       else f\"pattern ambiguous ({count} "
                "matches)\")\n"
                '            stale.append(m["name"])'),
        "new": ("        if count == 0:\n"
                '            problem = ("pattern not found" if count == 0\n'
                "                       else f\"pattern ambiguous ({count} "
                "matches)\")\n"
                '            stale.append(m["name"])'),
        "expect": ["test_an_ambiguous_pattern_fails_the_check"],
    },
    {
        # The first two lines are part of the pattern because the two
        # _translate calls on their own also match the run loop below.
        "name": "check-ignores-line-endings",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": ('        path = m["file"]\n'
                "        data = path.read_bytes()\n"
                '        old = _translate(m["old"], data)\n'
                '        new = _translate(m["new"], data)'),
        "new": ('        path = m["file"]\n'
                "        data = path.read_bytes()\n"
                '        old = m["old"].encode("utf-8")\n'
                '        new = m["new"].encode("utf-8")'),
        "expect": ["test_the_check_translates_line_endings"],
    },
    {
        # One list under two names. Stale and non-compiling are different
        # repairs, and a summary that adds them together tells the reader
        # neither.
        "name": "check-counts-stale-and-broken-together",
        "service": SHARED,
        "test_file": RUNNER_TESTS,
        "file": RUNNER,
        "old": "    stale, broken = [], []",
        "new": "    stale = broken = []",
        "expect": ["test_stale_and_non_compiling_are_counted_apart"],
    },
]


if __name__ == "__main__":
    raise SystemExit(run(MUTATIONS))
