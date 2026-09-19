"""Mutation gate for the wh-stt-overflow-config-and-wording guard tests.

Proves each guard test fails for the right reason when the behaviour it
protects is broken. Run from services/stt_providers/shared with plain
python (the gate needs only the standard library; it invokes `uv run
pytest` as a subprocess, in whichever service each mutation names):

    python tests/mutation_gate_overflow_wording.py
    python tests/mutation_gate_overflow_wording.py loader   # name filter
    python tests/mutation_gate_overflow_wording.py --check  # patterns only

The gate lives here, beside the runner, although four of its mutations
edit the google provider: the change it guards spans both services, and
splitting it would leave two half-gates for one behaviour. Each mutation
carries the service its tests run in.

Two kinds of behaviour are protected and they break differently.

The loader mutations attack a REFUSAL. `validate_config_or_exit` exits 2
on any section it does not know, so deleting [overflow_detection] from the
loader without also listing it as optional turns every settings file
written before David's ruling into a startup refusal over a section
nothing reads. Nothing about that is visible in a diff of the deletion:
the section simply stops being mentioned, and the refusal arrives on a
user's machine.

The wording mutations attack LINES A PERSON READS. A wrong line still
prints and still looks like an answer. The line these replace named
PortAudio whichever backend was running, and WinRT -- the shipped default
-- has no PortAudio in its capture path at all. So most of these mutations
restore the exact wording that was wrong, rather than deleting anything.

Runner discipline lives in tests/mutation_gate_runner.py.
"""
import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path; this keeps it working when the gate is
# invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
GOOGLE = SHARED.parent / "google_stt_server"

WORDING_TESTS = "tests/test_capture_log_wording.py"
LOADER_TESTS = "tests/test_config_loader.py"
MAIN_TESTS = "tests/test_main.py"

LOADER = GOOGLE / "config_loader.py"
GOOGLE_MAIN = GOOGLE / "main.py"
OVERFLOW = SHARED / "shared_audio" / "overflow_monitor.py"
WINRT = SHARED / "shared_audio" / "capture" / "winrt_capture.py"

MUTATIONS = [
    # -- config_loader.py: the section that was deleted -------------------
    {
        # The deletion's real hazard. Unknown sections are refused, so a
        # loader that simply forgets the name turns a saved settings file
        # into an exit(2) about a section nothing reads.
        "name": "loader-leftover-section-refused",
        "service": GOOGLE,
        "test_file": LOADER_TESTS,
        "file": LOADER,
        "old": '    optional_sections = {"agc", "provider", "forwarding", '
               '"wake_word", "overflow_detection"}',
        "new": '    optional_sections = {"agc", "provider", "forwarding", '
               '"wake_word"}',
        "expect": ["test_a_leftover_section_is_accepted",
                   "test_a_leftover_section_says_nothing"],
    },
    {
        # The opposite mistake: leaving the name in the required set means
        # a settings file written after the ruling cannot start.
        "name": "loader-section-required-again",
        "service": GOOGLE,
        "test_file": LOADER_TESTS,
        "file": LOADER,
        "old": '    required_sections = {"server", "adaptation", "client", '
               '"diagnostics", "debug", "latency"}',
        "new": '    required_sections = {"server", "adaptation", "client", '
               '"diagnostics", "debug", "latency", "overflow_detection"}',
        "expect": ["test_a_config_without_the_section_is_accepted"],
    },
    {
        # The dataclass coming back is how the dead config returns: a
        # later reader sees a type for the section and assumes it is live.
        "name": "loader-dataclass-back",
        "service": GOOGLE,
        "test_file": LOADER_TESTS,
        "file": LOADER,
        "old": 'class AppConfig:\n'
               '    """Strongly-typed application configuration."""',
        "new": 'class OverflowDetectionConfig:\n'
               '    """Put back by the mutation gate. Nothing reads it."""\n'
               '    enabled: bool = False\n'
               '\n'
               '\n'
               '@dataclass\n'
               'class AppConfig:\n'
               '    """Strongly-typed application configuration."""',
        "expect": ["test_the_loader_no_longer_defines_the_dataclass"],
    },
    {
        # And the field coming back is how a value nothing sets becomes
        # readable again further down the flow.
        "name": "loader-appconfig-field-back",
        "service": GOOGLE,
        "test_file": LOADER_TESTS,
        "file": LOADER,
        "old": '    latency: "LatencyConfig"\n'
               '    debug: DebugConfig\n'
               '    agc: "AGCConfig"',
        "new": '    latency: "LatencyConfig"\n'
               '    debug: DebugConfig\n'
               '    agc: "AGCConfig"\n'
               '    overflow_detection: dict = field(default_factory=dict)',
        "expect": ["test_the_loaded_config_carries_no_overflow_field"],
    },

    # -- the phrase each backend publishes --------------------------------
    {
        # The original defect, in one line: the WinRT path saying PortAudio.
        # A literal rather than the constant, because winrt_capture.py does
        # not import PORTAUDIO_SOURCE and a NameError in the class body
        # would fail every test in the file for an unrelated reason.
        "name": "winrt-source-says-portaudio",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": WINRT,
        "old": "    OVERFLOW_SOURCE = CAPTURE_QUEUE_SOURCE",
        "new": '    OVERFLOW_SOURCE = "PortAudio"',
        "expect": ["test_the_winrt_backend_hands_the_monitor_the_queue",
                   "test_the_backend_publishes_the_phrase_a_provider_"
                   "can_read"],
    },
    {
        # Publishing the phrase is not enough; the monitor has to be given
        # it, or the summary falls back to the mechanism-free default.
        "name": "winrt-monitor-loses-the-source",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": WINRT,
        "old": "            stable_reset_seconds=300.0,\n"
               "            overflow_source=self.OVERFLOW_SOURCE\n"
               "        )",
        "new": "            stable_reset_seconds=300.0\n"
               "        )",
        "expect": ["test_the_winrt_backend_hands_the_monitor_the_queue"],
    },

    # -- overflow_monitor.py: the summary line ----------------------------
    {
        # The parameter exists so this line can differ per backend. A
        # hardcoded phrase here undoes the whole change while every
        # backend still publishes the right words.
        "name": "summary-hardcodes-portaudio",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": '                f"{self.config.overflow_source}"',
        "new": '                f"PortAudio"',
        "expect": ["test_the_winrt_backend_names_the_queue",
                   "test_summary_names_the_count_the_window_and_the_source"],
    },

    # -- overflow_monitor.py: the two refusal lines -----------------------
    {
        # _should_restart is deciding whether to ASK. "Restart needed"
        # states a need it never establishes, and the Parakeet provider
        # passes no overflow_callback at all, so nothing there acts on one.
        "name": "cooldown-line-says-a-restart-is-needed",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": '            self._emit(\n'
               '                logging.DEBUG,\n'
               '                f"[overflow] No restart requested: still '
               'inside the "\n'
               '                f"{self.config.restart_cooldown_seconds}s '
               'cooldown "\n'
               '                f"({time_since_last_restart:.1f}s since '
               'the last request)"\n'
               '            )',
        "new": '            self._emit(\n'
               '                logging.DEBUG,\n'
               '                f"[overflow] Restart needed but still in '
               'cooldown "\n'
               '                f"({time_since_last_restart:.1f}s < "\n'
               '                f"{self.config.restart_cooldown_seconds}s)"\n'
               '            )',
        "expect": ["test_the_cooldown_line_says_no_request_was_made",
                   "test_neither_refusal_claims_a_restart_is_needed"],
    },
    {
        "name": "attempt-limit-line-says-a-restart-is-needed",
        "service": SHARED,
        "test_file": WORDING_TESTS,
        "file": OVERFLOW,
        "old": '            self._emit(\n'
               '                logging.DEBUG,\n'
               '                f"[overflow] No restart requested: the '
               'attempt limit is "\n'
               '                f"reached ({self.restart_attempts}/"\n'
               '                f"{self.config.max_restart_attempts})"\n'
               '            )',
        "new": '            self._emit(\n'
               '                logging.DEBUG,\n'
               '                f"[overflow] Restart needed but max '
               'attempts reached "\n'
               '                f"({self.restart_attempts}/"\n'
               '                f"{self.config.max_restart_attempts})"\n'
               '            )',
        "expect": ["test_the_attempt_limit_line_says_no_request_was_made",
                   "test_neither_refusal_claims_a_restart_is_needed"],
    },

    # -- google main.py: the provider's own overflow line -----------------
    {
        # The line the bead was filed about, restored exactly.
        "name": "google-callback-hardcodes-portaudio",
        "service": GOOGLE,
        "test_file": MAIN_TESTS,
        "file": GOOGLE_MAIN,
        "old": '            f"[overflow] audio input overflow at '
               '{mic.OVERFLOW_SOURCE}; "',
        "new": '            f"[overflow] audio input overflow at '
               'PortAudio; "',
        "expect": ["test_the_line_names_the_queue_on_the_winrt_backend",
                   "test_the_line_does_not_name_a_restart"],
    },
    {
        # No restart follows this callback. Saying one does is the third
        # of the three misleading messages the bead names.
        "name": "google-callback-announces-a-restart",
        "service": GOOGLE,
        "test_file": MAIN_TESTS,
        "file": GOOGLE_MAIN,
        "old": '            f"frames may be dropped"',
        "new": '            f"frames may be dropped; restarting the '
               'microphone"',
        "expect": ["test_the_line_does_not_name_a_restart",
                   "test_the_line_names_the_queue_on_the_winrt_backend",
                   "test_the_line_names_whatever_source_the_provider_"
                   "publishes"],
    },
]


if __name__ == "__main__":
    raise SystemExit(run(MUTATIONS))
