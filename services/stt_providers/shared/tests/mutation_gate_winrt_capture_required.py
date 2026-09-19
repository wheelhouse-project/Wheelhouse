"""Mutation gate for wh-capture-winrt-required.

The bead's criterion A6. Five behaviours are pinned here, and each one is
the exact shape of a defect that has already happened or that the change
was written to make impossible:

  the factory refuses instead of falling back    (A2)
  the refusal carries the wording David approved (A2)
  each provider sends that refusal and quits     (A3)
  each ready notice names the capture path built (A4)
  a capture that fails later ends the run        (A9)
  a missing output device says so, not "winsdk" (A10)

Why a gate at all. The defect this bead answers ran on David's machine
for hours on 2026-09-05: the Parakeet venv had no winsdk, the factory's
auto path chose sounddevice without a word, and every diagnostic in the
process reported PortAudio while the shipped default is WinRT. Nothing
failed. A test suite written after that fix passes whether or not the
fix is load-bearing, so the tests owe a demonstration that they can see
the defect return. That is what each mutation below is: the old
behaviour put back, one piece at a time.

Run it from this directory:

    uv run --no-sync python tests/mutation_gate_winrt_capture_required.py

``--check`` answers the two offline questions -- does every pattern match
exactly once, and does every mutant still parse -- without running a test
or writing a file. It is NOT a sweep: it cannot see a survivor, and it
cannot see a mutation masked by a later layer. Use it while a suite or a
reviewer round is live, and run the gate in full before the final commit.

The gate spans four service directories. The runner takes a per-mutation
``service``, so each mutation's tests run in the virtual environment that
owns the code it breaks; the shared package and the three providers each
have their own. That is also why a provider mutation cannot be checked by
a shared test: nothing in this service can import a provider.

The WheelHouse half of A4 -- the wheelhouse.log line written from the
accepted ready, and the fall-through that carries the refusal to the
notice the user sees -- is NOT here. It lives in
services/wheelhouse/tests/mutation_gate_remote_stt_robustness.py, which
already mutates integrations/websocket_manager.py and already has the
per-mutation source file and test selection those need. Splitting a
gate by service rather than by bead keeps each one runnable from the
service directory whose venv its tests need.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path; this keeps it working when the gate is
# invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent

PARAKEET = PROVIDERS / "sherpa_offline_parakeet_stt_server"
DISTIL = PROVIDERS / "distil_medium_en"
GOOGLE = PROVIDERS / "google_stt_server"

FACTORY = SHARED / "shared_audio" / "capture" / "factory.py"
WINRT_CAPTURE = (
    SHARED / "shared_audio" / "capture" / "winrt_capture.py"
)

FACTORY_TESTS = "tests/test_audio_capture_factory.py"
WINRT_CAPTURE_TESTS = "tests/test_winrt_capture.py"
SHARED_BACKEND_TESTS = "tests/test_ready_backend_name.py"
REFUSAL_TESTS = "tests/test_winrt_capture_required.py"
BACKEND_TESTS = "tests/test_ready_backend_name.py"


# --------------------------------------------------------------------------
# A2: the factory refuses, and the refusal says the approved thing
# --------------------------------------------------------------------------

FACTORY_MUTATIONS = [
    {
        # Half one of the old factory-falls-back-to-sounddevice mutation:
        # the source text. The import is aliased and function-local. An
        # unaliased `import shared_audio.capture.sounddevice_capture`
        # would bind the name `shared_audio` as a local of this function,
        # and every earlier use of it would raise UnboundLocalError
        # before the mutated line ran -- a crash upstream of the
        # behaviour, which reads as a catch while proving nothing
        # (mutation-gate skill).
        #
        # ONE expected test, not four. wh-portaudio-capture-removal
        # deleted sounddevice_capture.py, so this mutant now raises
        # ModuleNotFoundError the moment the branch runs. The three
        # refusal tests do fail under it, but on that crash rather than
        # on their own assertions, which is exactly the false catch the
        # mutation-gate skill names. Only the source-text test earns its
        # verdict here -- it reads factory.py's import lines and never
        # executes the branch -- and it failed on its own assertion when
        # this was measured:
        #     E   AssertionError: assert not
        #         ['from .sounddevice_capture import (']
        # The behaviour the other three guard is proven by the mutation
        # below instead.
        "name": "factory-imports-the-deleted-fallback",
        "service": SHARED,
        "test_file": FACTORY_TESTS,
        "file": FACTORY,
        "old": (
            "    if not WINRT_AUDIO_AVAILABLE:\n"
            "        # No capture is built. Raising after construction would"
            " leave a\n"
            "        # microphone open in a process that is about to exit.\n"
            "        logger.error(\n"
            "            \"WinRT audio capture is unavailable: winsdk did not"
            " import\"\n"
            "        )\n"
            "        raise RuntimeError(WINRT_REQUIRED_MESSAGE)\n"
        ),
        "new": (
            "    if not WINRT_AUDIO_AVAILABLE:\n"
            "        from .sounddevice_capture import (\n"
            "            SounddeviceAudioCapture as _Fallback,\n"
            "        )\n"
            "        logger.error(\n"
            "            \"WinRT audio capture is unavailable: winsdk did not"
            " import\"\n"
            "        )\n"
            "        return _Fallback(config or AudioConfig(),"
            " overflow_callback)\n"
        ),
        "expect": [
            "test_the_source_imports_no_sounddevice_capture",
        ],
    },
    {
        # Half two: the behaviour. This is what factory.py did before A2
        # -- an unavailable WinRT returned a capture object and the
        # process ran on, which is the 2026-09-05 defect. The returned
        # class is WinRTAudioCapture rather than the deleted PortAudio
        # adapter, so the mutant builds something that really exists and
        # every catcher reaches its own assertion instead of an import
        # error. WinRTAudioCapture.__init__ opens no device and starts no
        # thread (winrt_capture.py:191-257), so constructing it in a test
        # that does not patch it is safe.
        #
        # test_builds_no_capture_at_all_when_winrt_does_not_load fails on
        # its pytest.raises rather than on MockWinRT.assert_not_called().
        # Both are the same guarantee: the refusal builds nothing.
        "name": "factory-falls-back-instead-of-refusing",
        "service": SHARED,
        "test_file": FACTORY_TESTS,
        "file": FACTORY,
        "old": (
            "    if not WINRT_AUDIO_AVAILABLE:\n"
            "        # No capture is built. Raising after construction would"
            " leave a\n"
            "        # microphone open in a process that is about to exit.\n"
            "        logger.error(\n"
            "            \"WinRT audio capture is unavailable: winsdk did not"
            " import\"\n"
            "        )\n"
            "        raise RuntimeError(WINRT_REQUIRED_MESSAGE)\n"
        ),
        "new": (
            "    if not WINRT_AUDIO_AVAILABLE:\n"
            "        logger.error(\n"
            "            \"WinRT audio capture is unavailable: winsdk did not"
            " import\"\n"
            "        )\n"
            "        return WinRTAudioCapture(config or AudioConfig(),"
            " overflow_callback)\n"
        ),
        "expect": [
            "test_raises_runtime_error_when_winrt_does_not_load",
            "test_the_refusal_text_is_exactly_the_approved_wording",
            "test_builds_no_capture_at_all_when_winrt_does_not_load",
            "test_a_missing_winsdk_makes_the_factory_refuse",
        ],
    },
    {
        # The wording is the whole of what a user gets. A message that
        # named the package but not the fix would leave someone with a
        # dead engine and nothing to do about it, which is the state the
        # 2026-09-05 run left David in. David approved this exact text,
        # so a silent reword is a user-visible change made without him.
        "name": "factory-refusal-loses-the-instructions",
        "service": SHARED,
        "test_file": FACTORY_TESTS,
        "file": FACTORY,
        "old": (
            "WINRT_REQUIRED_MESSAGE = (\n"
            "    \"The speech service cannot start: the audio package winsdk"
            " is not \"\n"
            "    \"installed. Re-run the WheelHouse installer. Developers:"
            " run \"\n"
            "    \"bootstrap.ps1.\"\n"
            ")\n"
        ),
        "new": (
            "WINRT_REQUIRED_MESSAGE = (\n"
            "    \"The speech service cannot start: winsdk is not"
            " installed.\"\n"
            ")\n"
        ),
        "expect": [
            "test_the_refusal_text_is_exactly_the_approved_wording",
        ],
    },
    {
        # A4's constant. The name is what makes a WRONG capture path
        # visible, so a name that does not match the path the factory
        # builds is worse than none: it reads as a confirmation. This
        # mutation writes the very name the 2026-09-05 run was really
        # using, which is the one a reader would most readily believe.
        "name": "factory-backend-name-lies",
        "service": SHARED,
        "test_file": SHARED_BACKEND_TESTS,
        "file": FACTORY,
        "old": "CAPTURE_BACKEND_NAME = \"winrt\"\n",
        "new": "CAPTURE_BACKEND_NAME = \"portaudio\"\n",
        "expect": [
            "test_the_backend_name_is_winrt",
        ],
    },
]


# --------------------------------------------------------------------------
# A3: each provider sends the refusal and exits nonzero
# --------------------------------------------------------------------------

def _refusal_mutations(label, service, call, indent):
    """The two refusal mutations every provider shares.

    Each provider catches the factory's RuntimeError, sends the notice,
    and quits. The two ways that can go wrong are opposite and both were
    live possibilities while A3 was written: quitting without telling
    anyone, and telling someone but carrying on anyway.

    ``call`` is the provider's own send, quoted whole, because the
    argument list differs per provider (display name, ws host and port,
    provider_name, and google's emits_eos).
    """
    return [
        {
            # Quiet death. The provider exits, WheelHouse's launch
            # monitor eventually times out, and the user is told the
            # engine stopped without being told the one thing that would
            # let them fix it.
            "name": f"{label}-refusal-not-sent",
            "service": service,
            "test_file": REFUSAL_TESTS,
            "file": service / "main.py",
            "old": call,
            "new": f"{indent}pass\n",
            "expect": [
                "test_construction_sends_the_refusal_and_exits_nonzero",
                "test_the_refusal_connection_declares_the_" + label
                + "_provider",
            ],
        },
    ]


PARAKEET_REFUSAL_CALL = (
    "            send_startup_failed_notice(\n"
    "                self.display_name,\n"
    "                str(exc),\n"
    "                ws_host,\n"
    "                ws_port,\n"
    "                provider_name=\"parakeet_tdt\",\n"
    "            )\n"
)

DISTIL_REFUSAL_CALL = (
    "            send_startup_failed_notice(\n"
    "                self.DISPLAY_NAME,\n"
    "                str(exc),\n"
    "                ws_host,\n"
    "                ws_port,\n"
    "                provider_name=\"distil_medium_en\",\n"
    "            )\n"
)

GOOGLE_REFUSAL_CALL = (
    "        send_startup_failed_notice(\n"
    "            \"Google STT\",\n"
    "            str(exc),\n"
    "            cfg.ws_host,\n"
    "            cfg.ws_port,\n"
    "            provider_name=\"google_stt\",\n"
    "            emits_eos=True,\n"
    "        )\n"
)

REFUSAL_MUTATIONS = (
    _refusal_mutations("parakeet", PARAKEET, PARAKEET_REFUSAL_CALL,
                       " " * 12)
    + _refusal_mutations("distil", DISTIL, DISTIL_REFUSAL_CALL, " " * 12)
    + [
        {
            # Google's own, written out: its refusal sits in main()
            # rather than in a server constructor, so it returns a code
            # instead of calling sys.exit, and its test name differs.
            "name": "google-refusal-not-sent",
            "service": GOOGLE,
            "test_file": REFUSAL_TESTS,
            "file": GOOGLE / "main.py",
            "old": GOOGLE_REFUSAL_CALL,
            "new": "        pass\n",
            "expect": [
                "test_main_sends_the_refusal_and_returns_nonzero",
                "test_the_refusal_connection_declares_the_google_provider",
            ],
        },
        {
            # The other half of A3, and the half a "did it send?" test
            # cannot see: a provider that says why it cannot start and
            # then starts anyway is the 2026-09-05 defect with a notice
            # attached. sys.exit is replaced with `pass` rather than
            # deleted, because deleting the only statement of a block
            # leaves code that does not compile, and a mutant that does
            # not compile reads as caught (mutation-gate skill).
            #
            # The comment line above the exit is part of the pattern
            # because A9 put a SECOND "            sys.exit(1)" in this
            # file, in start(). The bare line now matches twice and the
            # runner reports an ambiguous pattern, which is a gate
            # failure rather than a survivor.
            "name": "parakeet-refusal-does-not-quit",
            "service": PARAKEET,
            "test_file": REFUSAL_TESTS,
            "file": PARAKEET / "main.py",
            # Anchor refreshed for wh-capture-winrt-required.1.5, which
            # put a comment block and REFUSAL_EXIT_CODE where the bare
            # sys.exit(1) used to sit. The behaviour the mutation breaks
            # is unchanged: the constructor refusal still has to end the
            # process, whatever number it ends it with.
            "old": ("            # (wh-capture-winrt-required.1.5).\n"
                    "            sys.exit(REFUSAL_EXIT_CODE)\n"),
            # `return`, not `pass`. With `pass` this __init__ runs on to
            # `capture_stats=self.audio_capture.get_stats` at main.py:275,
            # where the refused constructor has no audio_capture, and both
            # expected tests died there on AttributeError without reaching
            # a single assertion of their own -- a catch earned by an
            # unrelated crash, which the mutation-gate skill calls a false
            # catch and which this runner cannot see, because it prints
            # WHICH tests failed and never why. `return` ends __init__
            # without ending the process, which is the behaviour this
            # entry means to break, and it stops short of line 275, so
            # both tests now fail with DID NOT RAISE SystemExit. Distil's
            # twin entry never needed this: its __init__ makes no second
            # use of audio_capture. Found by codex in round 3 as a static
            # observation and proved by running the mutation
            # (wh-capture-winrt-required.1).
            "new": ("            # (wh-capture-winrt-required.1.5).\n"
                    "            return\n"),
            "expect": [
                "test_construction_sends_the_refusal_and_exits_nonzero",
                "test_no_engine_is_loaded_once_the_capture_has_refused",
            ],
        },
        {
            # Same disambiguation as parakeet above, for the same
            # reason: A9 added a second exit to this file too.
            "name": "distil-refusal-does-not-quit",
            "service": DISTIL,
            "test_file": REFUSAL_TESTS,
            "file": DISTIL / "main.py",
            # Anchor refreshed for the same reason as parakeet above.
            "old": ("            # (wh-capture-winrt-required.1.5).\n"
                    "            sys.exit(REFUSAL_EXIT_CODE)\n"),
            "new": ("            # (wh-capture-winrt-required.1.5).\n"
                    "            pass\n"),
            "expect": [
                "test_construction_sends_the_refusal_and_exits_nonzero",
                "test_no_engine_is_loaded_once_the_capture_has_refused",
            ],
        },
    ]
)


# --------------------------------------------------------------------------
# A4: each ready notice names the capture path the factory built
# --------------------------------------------------------------------------

BACKEND_MUTATIONS = [
    {
        "name": "parakeet-ready-drops-the-backend",
        "service": PARAKEET,
        "test_file": BACKEND_TESTS,
        "file": PARAKEET / "main.py",
        "old": (
            "                    kind=\"ready\",\n"
            "                    capture_backend=CAPTURE_BACKEND_NAME)\n"
        ),
        "new": "                    kind=\"ready\")\n",
        "expect": [
            "test_the_ready_notice_names_the_winrt_capture",
            "test_the_name_comes_from_the_factory_that_built_the_capture",
        ],
    },
    {
        "name": "distil-ready-drops-the-backend",
        "service": DISTIL,
        "test_file": BACKEND_TESTS,
        "file": DISTIL / "main.py",
        "old": (
            "                    kind=\"ready\",\n"
            "                    capture_backend=CAPTURE_BACKEND_NAME)\n"
        ),
        "new": "                    kind=\"ready\")\n",
        "expect": [
            "test_the_ready_notice_names_the_winrt_capture",
            "test_the_name_comes_from_the_factory_that_built_the_capture",
        ],
    },
    {
        "name": "google-ready-drops-the-backend",
        "service": GOOGLE,
        "test_file": BACKEND_TESTS,
        "file": GOOGLE / "main.py",
        "old": (
            "        forwarder.send_notification(\n"
            "            title, message, kind=kind,\n"
            "            capture_backend=CAPTURE_BACKEND_NAME if kind =="
            " \"ready\" else \"\")\n"
        ),
        "new": (
            "        forwarder.send_notification(\n"
            "            title, message, kind=kind)\n"
        ),
        "expect": [
            "test_the_ready_notice_names_the_winrt_capture",
            "test_the_name_comes_from_the_factory_that_built_the_capture",
        ],
    },
    {
        # The condition, not the argument. Google's send serves three
        # outcomes and only one of them has a true answer about capture;
        # dropping the condition makes a credentials failure and a
        # capture failure both claim a working WinRT path. That is a
        # false statement in the log rather than a missing one, which is
        # the harder of the two to notice.
        "name": "google-every-notice-claims-a-backend",
        "service": GOOGLE,
        "test_file": BACKEND_TESTS,
        "file": GOOGLE / "main.py",
        "old": (
            "            capture_backend=CAPTURE_BACKEND_NAME if kind =="
            " \"ready\" else \"\")\n"
        ),
        "new": "            capture_backend=CAPTURE_BACKEND_NAME)\n",
        "expect": [
            "test_a_capture_that_never_opened_claims_no_backend",
            "test_a_credentials_failure_claims_no_backend",
        ],
    },
    {
        # Google's second ready-capable send: a credentials reload that
        # succeeds. Its guard is a source check, which is the weaker
        # kind, and this mutation is where that test earns its place --
        # driving the send needs the whole restart handler.
        "name": "google-restart-ready-drops-the-backend",
        "service": GOOGLE,
        "test_file": BACKEND_TESTS,
        "file": GOOGLE / "main.py",
        "old": (
            "                    forwarder.send_notification(\n"
            "                        title, message, kind=kind,\n"
            "                        capture_backend=(\n"
            "                            CAPTURE_BACKEND_NAME if kind =="
            " \"ready\" else \"\"))\n"
        ),
        "new": (
            "                    forwarder.send_notification(\n"
            "                        title, message, kind=kind)\n"
        ),
        "expect": [
            "test_the_restart_completion_send_site_passes_the_backend",
        ],
    },
    {
        # The trap this field walked into on the way in, kept as a
        # mutation because it is invisible from any test that stubs the
        # forwarder. ws_forwarder's sender loop rebuilds the outgoing
        # notification payload key by key, so a field accepted by
        # send_notification and absent from that payload never leaves the
        # process. The file already records the same trap for "kind"
        # (wh-google-creds-file-picker.1.15). Only the end-to-end socket
        # tests can see it.
        "name": "forwarder-payload-drops-the-backend",
        "service": SHARED,
        "test_file": SHARED_BACKEND_TESTS,
        "file": SHARED / "shared_stt" / "ws_forwarder.py",
        "old": (
            "                                    \"capture_backend\":"
            " msg_data.get(\n"
            "                                        \"capture_backend\","
            " \"\"),\n"
        ),
        "new": "",
        "expect": [
            "test_a_ready_notification_carries_the_capture_backend",
            "test_a_notice_that_names_no_backend_still_carries_the_field",
        ],
    },
]


# --------------------------------------------------------------------------
# A5 preamble: a --list-devices run refuses on stderr and notifies nobody
# --------------------------------------------------------------------------
#
# Boss ruling, 2026-09-06. A3's notice exists so a provider WheelHouse
# LAUNCHED can say why it quit. WheelHouse never launched a
# --list-devices run, so a notice from one is addressed to nobody -- and
# it cannot be ignored either, because the notice names the provider, so
# WheelHouse reads it against whatever launch of that provider happens to
# be live and ends a session the person never touched. The two mutations
# per provider are the two ways to lose that: say nothing at all, or say
# it to WheelHouse again.

_LIST_DEVICES_EXPECT = [
    "test_list_devices_refuses_on_stderr_and_builds_no_forwarder",
]

LIST_DEVICES_MUTATIONS = [
    {
        "name": "parakeet-list-devices-refuses-in-silence",
        "service": PARAKEET,
        "test_file": REFUSAL_TESTS,
        "file": PARAKEET / "main.py",
        "old": "        print(str(exc), file=sys.stderr)\n",
        "new": "        pass\n",
        "expect": _LIST_DEVICES_EXPECT,
    },
    {
        # The notice put back, which is what the code did before the
        # ruling. The test's forwarder_class.assert_not_called() is the
        # only assertion that can see this one: the exit code and the
        # stderr text are both unchanged by it.
        "name": "parakeet-list-devices-notifies-wheelhouse",
        "service": PARAKEET,
        "test_file": REFUSAL_TESTS,
        "file": PARAKEET / "main.py",
        "old": "        print(str(exc), file=sys.stderr)\n",
        "new": (
            "        print(str(exc), file=sys.stderr)\n"
            "        send_startup_failed_notice(\n"
            "            \"Parakeet\", str(exc), \"localhost\", 8765,\n"
            "            provider_name=\"parakeet_tdt\")\n"
        ),
        "expect": _LIST_DEVICES_EXPECT,
    },
    {
        "name": "distil-list-devices-refuses-in-silence",
        "service": DISTIL,
        "test_file": REFUSAL_TESTS,
        "file": DISTIL / "main.py",
        "old": "        print(str(exc), file=sys.stderr)\n",
        "new": "        pass\n",
        "expect": _LIST_DEVICES_EXPECT,
    },
    {
        "name": "distil-list-devices-notifies-wheelhouse",
        "service": DISTIL,
        "test_file": REFUSAL_TESTS,
        "file": DISTIL / "main.py",
        "old": "        print(str(exc), file=sys.stderr)\n",
        "new": (
            "        print(str(exc), file=sys.stderr)\n"
            "        send_startup_failed_notice(\n"
            "            \"Distil\", str(exc), \"localhost\", 8765,\n"
            "            provider_name=\"distil_medium_en\")\n"
        ),
        "expect": _LIST_DEVICES_EXPECT,
    },
    {
        "name": "google-list-devices-refuses-in-silence",
        "service": GOOGLE,
        "test_file": REFUSAL_TESTS,
        "file": GOOGLE / "main.py",
        "old": "            print(str(exc), file=sys.stderr)\n",
        "new": "            pass\n",
        "expect": _LIST_DEVICES_EXPECT,
    },
    {
        # Google's second way in differs from the other two providers.
        # It has no run_list_devices function: the flag is handled inside
        # main(), below the capture both paths need, so the branch that
        # keeps the notice away from a device listing is an `if` at the
        # shared refusal site. Disabling that `if` sends the notice again
        # AND loses the stderr line, so both assertions fire.
        #
        # Pattern refreshed at cd21d6b9. The fix for
        # wh-capture-winrt-required.1.1 added a second reason to take the
        # console path, so the `if` this mutation disables now reads
        # `if args.list_devices or not cfg.forward_ws:`. The run at
        # 04:47 on 2026-09-06 reported this entry as "pattern not found",
        # which is a gate failure, not a survivor and not a pass. The
        # mutation is kept rather than deleted: the behaviour it protects
        # is still there, under new code.
        "name": "google-list-devices-takes-the-launch-path",
        "service": GOOGLE,
        "test_file": REFUSAL_TESTS,
        "file": GOOGLE / "main.py",
        "old": "        if args.list_devices or not cfg.forward_ws:\n"
               "            print(str(exc), file=sys.stderr)\n",
        "new": "        if False:\n"
               "            print(str(exc), file=sys.stderr)\n",
        "expect": _LIST_DEVICES_EXPECT,
    },
    {
        # The other half of that same `if`, added with the fix it guards.
        # Dropping `or not cfg.forward_ws` restores exactly the defect
        # wh-capture-winrt-required.1.1 reported: a developer run with
        # forwarding turned off still opens a forwarder of its own inside
        # send_startup_failed_notice and reaches WheelHouse, which can end
        # a live session.
        #
        # --list-devices is unaffected by this mutation, so
        # _LIST_DEVICES_EXPECT cannot see it. Only the forwarding-off test
        # can, and its assert_not_called() is the assertion that fires.
        "name": "google-refusal-ignores-forward-ws",
        "service": GOOGLE,
        "test_file": REFUSAL_TESTS,
        "file": GOOGLE / "main.py",
        "old": "        if args.list_devices or not cfg.forward_ws:\n"
               "            print(str(exc), file=sys.stderr)\n",
        "new": "        if args.list_devices:\n"
               "            print(str(exc), file=sys.stderr)\n",
        "expect": ["test_forwarding_turned_off_means_no_refusal_connection"],
    },
]


# --------------------------------------------------------------------------
# A9: a capture that fails AFTER the model loads ends the run, nonzero
# --------------------------------------------------------------------------
#
# A3 covers the capture that is never BUILT. A9 covers the one that
# builds and then fails to start -- an AudioGraph that will not open, a
# device another process holds, a permission denied once the model is
# already in memory. That path sent the startup_failed notice and
# returned, leaving the audio loop turning on a capture that will never
# produce a chunk: a live process, a service that looks healthy to
# WheelHouse, and every spoken word discarded, for as long as the
# machine stayed up.
#
# One mutation per provider, and each is the shipped defect restored
# whole rather than a piece of it: the notice still goes out and the run
# still keeps going. A mutation that removed only the exit would leave
# the loop stopped, which is a state neither the old code nor the new
# one produces, and a gate entry for a state that cannot happen proves
# nothing about the state that can.

A9_TESTS = "tests/test_capture_failure_ends_the_run.py"

_A9_SERVER_EXPECT = [
    "test_a_failed_handshake_ends_the_audio_loop",
    "test_the_failure_is_recorded_as_a_stop",
    "test_start_exits_nonzero_after_a_failed_capture",
    "test_cleanup_runs_before_the_exit",
]

# parakeet and distil hold the run state on the server object, so the
# same three lines say it for both: the flag start() reads for its exit
# code, and the flag process_audio_loop reads to keep turning.
_A9_SERVER_STOP = (
    "            logger.error(\n"
    "                \"Audio capture never became ready - stopping the"
    " service\")\n"
    "            self._capture_failed = True\n"
    "            self.running = False\n"
)

CAPTURE_FAILURE_MUTATIONS = [
    {
        "name": "parakeet-capture-failure-keeps-running",
        "service": PARAKEET,
        "test_file": A9_TESTS,
        "file": PARAKEET / "main.py",
        "old": _A9_SERVER_STOP,
        "new": "",
        "expect": _A9_SERVER_EXPECT,
    },
    {
        "name": "distil-capture-failure-keeps-running",
        "service": DISTIL,
        "test_file": A9_TESTS,
        "file": DISTIL / "main.py",
        "old": _A9_SERVER_STOP,
        "new": "",
        "expect": _A9_SERVER_EXPECT,
    },
    {
        # Google's is written out separately because its mechanism is
        # different, not merely its indentation. It has no server object:
        # main() keeps the run state in a LOCAL that the announcement's
        # daemon thread cannot assign to, so the answer travels back as a
        # threading.Event that the loop condition and the return
        # statement both read. Deleting the set is therefore the whole
        # defect here, the way deleting the two flag writes is for the
        # other two.
        #
        # The startup_error guard goes with it. It is what keeps this to
        # a CAPTURE failure: ready is None on the credentials branch, and
        # a mutation that left the guard behind would not compile with
        # its body gone.
        "name": "google-capture-failure-keeps-running",
        "service": GOOGLE,
        "test_file": A9_TESTS,
        "file": GOOGLE / "main.py",
        # Anchor narrowed for wh-capture-winrt-required.1.4, which put a
        # comment block and the bounded connection wait between the log
        # line and the set. Only the set is mutated now, and to `pass`
        # rather than to nothing, so the guard above keeps a body and the
        # mutant still parses. The behaviour under test is unchanged and
        # the mutation is if anything tighter: the run learns nothing
        # about the failed capture, while everything around it -- the
        # log line, the queued notice, the wait -- still happens, so a
        # catch cannot come from a crash upstream of the assertion.
        "old": "            capture_failed.set()\n",
        "new": "            pass\n",
        "expect": [
            "test_a_failed_capture_marks_the_run_failed",
            "test_main_returns_nonzero_after_a_capture_failure",
            "test_the_teardown_still_runs",
        ],
    },
]


# --------------------------------------------------------------------------
# A10: the one AudioGraph failure a user can act on says what to do
# --------------------------------------------------------------------------

AUDIO_DEVICE_MUTATIONS = [
    {
        # The message CHOICE, which is the half an equality test cannot
        # prove on its own: a test that only compares the text raised for
        # DEVICE_NOT_AVAILABLE against the approved words stays green while
        # the raise sends a DIFFERENT approved text. So the mutation sends
        # the other one -- the winsdk refusal, which is the wording most
        # likely to be reached for, and the one that is wrong here for a
        # reason worth stating: getting this far proves winsdk imported, so
        # telling the user to re-run the installer sends them to fix a
        # package they already have.
        #
        # The text is written out rather than imported. winrt_capture.py
        # cannot import factory.py -- factory imports IT -- so a mutation
        # that reached for the constant would raise before the behaviour
        # ran, and a failure upstream of the behaviour reads as a catch
        # while proving nothing (mutation-gate skill).
        "name": "device-missing-refusal-sends-the-winsdk-words",
        "service": SHARED,
        "test_file": WINRT_CAPTURE_TESTS,
        "file": WINRT_CAPTURE,
        "old": (
            "                logger.error(AUDIO_DEVICE_MISSING_LOG_LINE)\n"
            "                raise RuntimeError(AUDIO_DEVICE_MISSING_MESSAGE)"
            "\n"
        ),
        "new": (
            "                logger.error(AUDIO_DEVICE_MISSING_LOG_LINE)\n"
            "                raise RuntimeError(\n"
            "                    \"The speech service cannot start: the"
            " audio package \"\n"
            "                    \"winsdk is not installed. Re-run the"
            " WheelHouse \"\n"
            "                    \"installer. Developers: run"
            " bootstrap.ps1.\"\n"
            "                )\n"
        ),
        "expect": [
            "test_a_missing_output_device_raises_the_approved_words",
            "test_the_missing_output_device_does_not_get_the_winsdk_words",
        ],
    },
]


# --------------------------------------------------------------------------
# .1.5: a refusal is not a crash, so the supervisor must not start it again
# --------------------------------------------------------------------------
#
# Codex round 2 found the whole refusal path ending in exit 1, which
# run_launcher() reads as a crash whenever it lands inside
# crash_threshold_s (15 seconds) -- the ordinary case on a machine whose
# model is already in the file cache. The provider was then started three
# times over, each attempt refusing again against a launch WheelHouse had
# already recorded as stopped. The fix is one shared exit code and one
# branch in should_restart, so the gate owes both halves: the branch, and
# the code actually reaching the branch from each provider.

REFUSAL_EXIT_MUTATIONS = [
    {
        # The branch itself, put back the way it was: with it gone, the
        # uptime test below decides, and a refusal reached in under a
        # second is restarted. Deleting it leaves `elif exit_code == 0:`
        # as the next arm, so the mutant still parses -- the failure is
        # a wrong answer, not a SyntaxError.
        "name": "refusal-is-restarted-like-a-crash",
        "service": SHARED,
        "test_file": "tests/test_refusal_is_not_a_crash.py",
        "file": SHARED / "shared_stt" / "launcher.py",
        "old": (
            "    elif exit_code == REFUSAL_EXIT_CODE:\n"
            "        # The launcher log is where a person looks when a"
            " provider does\n"
            "        # not appear. Without this line the log shows a child"
            " that\n"
            "        # exited and a supervisor that quit, and nothing saying"
            " the\n"
            "        # provider itself declined to run.\n"
            "        logger.error(\n"
            "            f\"The provider refused to start (exit code"
            " {REFUSAL_EXIT_CODE}) \"\n"
            "            \"and will not be restarted\")\n"
            "        return False\n"
        ),
        "new": "",
        "expect": [
            "test_a_refusal_is_not_restarted_even_on_a_fast_machine",
            "test_the_refusal_is_reported_as_a_refusal",
            "test_a_refusal_runs_one_child_and_stops",
            "test_a_refusal_is_not_counted_toward_the_crash_limit",
        ],
    },
    {
        # One refusal site per provider, the constructor refusal (A3),
        # sending 1 again. The branch above can be correct while a
        # provider never reaches it, and that is the half a test of
        # should_restart alone cannot see.
        #
        # The preceding comment line is part of the pattern because
        # `sys.exit(REFUSAL_EXIT_CODE)` appears twice in this file at the
        # same indentation -- here and at the A9 exit -- and a pattern
        # matching both would edit the first and report a survivor for
        # code nothing touched.
        "name": "parakeet-constructor-refusal-exits-one",
        "service": PARAKEET,
        "test_file": REFUSAL_TESTS,
        "file": PARAKEET / "main.py",
        "old": (
            "            # (wh-capture-winrt-required.1.5).\n"
            "            sys.exit(REFUSAL_EXIT_CODE)\n"
        ),
        "new": (
            "            # (wh-capture-winrt-required.1.5).\n"
            "            sys.exit(1)\n"
        ),
        "expect": ["test_the_constructor_refusal_exits_with_the_refusal_code"],
    },
    {
        "name": "distil-constructor-refusal-exits-one",
        "service": DISTIL,
        "test_file": REFUSAL_TESTS,
        "file": DISTIL / "main.py",
        "old": (
            "            # (wh-capture-winrt-required.1.5).\n"
            "            sys.exit(REFUSAL_EXIT_CODE)\n"
        ),
        "new": (
            "            # (wh-capture-winrt-required.1.5).\n"
            "            sys.exit(1)\n"
        ),
        "expect": ["test_the_constructor_refusal_exits_with_the_refusal_code"],
    },
    {
        # Google returns rather than exits -- `sys.exit(main())` is the
        # last line of the module -- and its two returns of the constant
        # are identical, so the anchor is the last line of the comment
        # that only the first one carries.
        "name": "google-capture-refusal-returns-one",
        "service": GOOGLE,
        "test_file": REFUSAL_TESTS,
        "file": GOOGLE / "main.py",
        "old": (
            "        # expects from a command that printed an error.\n"
            "        return REFUSAL_EXIT_CODE\n"
        ),
        "new": (
            "        # expects from a command that printed an error.\n"
            "        return 1\n"
        ),
        "expect": ["test_the_capture_refusal_exits_with_the_refusal_code"],
    },
]


# --------------------------------------------------------------------------
# .1.4: the queued refusal gets a connection to leave on
# --------------------------------------------------------------------------
#
# send_notification only schedules a queue put on the sender loop, and
# WSForwarder.stop() drains that queue ONLY when a connection is already
# live. A capture failure that landed before the provider's first
# handshake finished therefore set the stop event with the notice still
# in the queue, and the sender loop -- `while capabilities_sent and not
# self._stop_evt.is_set():` -- never read it again. The user saw nothing.
#
# The fix is one call to the bounded wait the constructor refusal already
# used. That call is a single line, which is exactly the kind of line a
# later edit removes without anyone noticing, so each provider owes a
# mutation that takes it out again. `pass` rather than nothing: the call
# is the only statement between two comments, and deleting it outright
# would leave a block whose body is gone.

NOTICE_WAIT_MUTATIONS = [
    {
        "name": "parakeet-refusal-does-not-wait-for-a-connection",
        "service": PARAKEET,
        "test_file": A9_TESTS,
        "file": PARAKEET / "main.py",
        "old": (
            "            wait_for_notice_connection("
            "self.forwarder, self.forwarder.uri)\n"
        ),
        "new": "            pass\n",
        "expect": [
            "test_a_live_connection_is_confirmed_before_the_refusal_ends"
            "_the_run",
            "test_a_connection_that_opens_during_the_wait_gets_the_notice",
            "test_a_notice_that_never_connects_is_reported_lost_with_its"
            "_address",
        ],
    },
    {
        "name": "distil-refusal-does-not-wait-for-a-connection",
        "service": DISTIL,
        "test_file": A9_TESTS,
        "file": DISTIL / "main.py",
        "old": (
            "            wait_for_notice_connection("
            "self.forwarder, self.forwarder.uri)\n"
        ),
        "new": "            pass\n",
        "expect": [
            "test_a_live_connection_is_confirmed_before_the_refusal_ends"
            "_the_run",
            "test_a_connection_that_opens_during_the_wait_gets_the_notice",
            "test_a_notice_that_never_connects_is_reported_lost_with_its"
            "_address",
        ],
    },
    {
        # Google keeps the forwarder in a local rather than on a server
        # object, so the call reads differently; the line it replaces is
        # the same one.
        "name": "google-refusal-does-not-wait-for-a-connection",
        "service": GOOGLE,
        "test_file": A9_TESTS,
        "file": GOOGLE / "main.py",
        "old": (
            "            wait_for_notice_connection(forwarder,"
            " forwarder.uri)\n"
        ),
        "new": "            pass\n",
        "expect": [
            "test_a_live_connection_is_confirmed_before_the_refusal_ends"
            "_the_run",
            "test_a_connection_that_opens_during_the_wait_gets_the_notice",
            "test_a_notice_that_never_connects_is_reported_lost_with_its"
            "_address",
        ],
    },
]


MUTATIONS = (
    FACTORY_MUTATIONS
    + REFUSAL_MUTATIONS
    + BACKEND_MUTATIONS
    + LIST_DEVICES_MUTATIONS
    + CAPTURE_FAILURE_MUTATIONS
    + AUDIO_DEVICE_MUTATIONS
    + REFUSAL_EXIT_MUTATIONS
    + NOTICE_WAIT_MUTATIONS
)


if __name__ == "__main__":
    sys.exit(run(MUTATIONS))
