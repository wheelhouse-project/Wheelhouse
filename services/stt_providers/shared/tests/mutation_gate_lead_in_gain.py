"""Mutation gate for wh-first-sound-lost.9 (the gain of the 300 ms lead-in).

Acceptance item 3 says each new test fails when the fix is removed. The fix
has two parts: SmartAGC.apply_last_gain in shared_audio/agc.py, and the call
to it where AudioProcessor.process_chunk flushes the lead-in buffer.

Run it from services/stt_providers/shared:

    python tests/mutation_gate_lead_in_gain.py            # the whole set
    python tests/mutation_gate_lead_in_gain.py name       # one, by name filter
    python tests/mutation_gate_lead_in_gain.py --check    # patterns only

The AGC-off test of acceptance item 2
(TestLeadInGain::test_with_agc_off_the_engine_gets_the_raw_audio) passes
before and after the fix, so no mutation of the fix can make it fail: with
AGC off, process() returns before it stores a gain, and the stored gain of
1.0 leaves the audio unchanged. The unit test
test_it_returns_the_input_when_agc_is_switched_off guards the off check in
apply_last_gain instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The runner sits beside this file. Running the gate as a script already
# puts that directory on sys.path; this keeps it working when the gate is
# invoked by an absolute path from another directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402
from mutation_gate_runner import run  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent
GOOGLE = PROVIDERS / "google_stt_server"

AGC = SHARED / "shared_audio" / "agc.py"
PROCESSOR = SHARED / "shared_stt" / "audio_processor.py"

TESTS = ("tests/test_agc.py", "tests/test_audio_processor.py")


# The launcher: start pytest directly, never through ``uv run``, under the
# Google provider's interpreter. mutation_gate_engine_audio.py gives the
# reasons: no uv sync may run in services/stt_providers/shared, and the
# shared test directory needs pytest_asyncio, which the Parakeet venv does
# not install.
def _launch(service, test_file, report, collect):
    """The argv and working directory for one pytest run, as a 2-tuple."""
    python = GOOGLE / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(
            f"No interpreter for {service}: {python} does not exist. Run "
            f".claude/scripts/prepare_worktree.py to sync the providers.")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch


_LEAD_IN = "test_the_lead_in_gets_the_gain_of_the_first_speech_chunk"
_CAPPED = "test_it_uses_the_effective_gain_after_the_caps"
_CLIPPED = "test_it_uses_the_same_clipping_rule"
_OFF = "test_it_returns_the_input_when_agc_is_switched_off"
_FLUSH = "test_flushes_lead_in_on_speech"

_CALL = "                    self._process_speech_audio(self.agc.apply_last_gain(lead_in))\n"
_STORE = "        self._last_effective_gain = effective_gain\n"
_OFF_CHECK = (
    "        if not self.config.enabled:\n"
    "            return pcm_bytes\n"
    "        return self._apply_gain(pcm_bytes, self._last_effective_gain)\n"
)

MUTATIONS = [
    {
        # The fix removed: the lead-in reaches the engine raw, at gain 1.0.
        "name": "lead-in-stays-raw",
        "service": SHARED,
        "test_file": TESTS,
        "file": PROCESSOR,
        "old": _CALL,
        "new": "                    self._process_speech_audio(lead_in)\n",
        "expect": [_LEAD_IN, _FLUSH],
    },
    {
        # process() never records its gain, so apply_last_gain keeps 1.0.
        "name": "last-gain-not-stored",
        "service": SHARED,
        "test_file": TESTS,
        "file": AGC,
        "old": _STORE,
        "new": "        pass\n",
        "expect": [_LEAD_IN, _CAPPED, _CLIPPED],
    },
    {
        # The stored gain is speech_gain before the caps, not the gain that
        # the chunk got. Equal when no cap is active, so only the capped
        # unit tests can see it.
        "name": "last-gain-before-the-caps",
        "service": SHARED,
        "test_file": TESTS,
        "file": AGC,
        "old": _STORE,
        "new": "        self._last_effective_gain = self.speech_gain\n",
        "expect": [_CAPPED],
    },
    {
        # The off check removed: a gain stored while AGC was on reaches
        # the lead-in after AGC is switched off.
        "name": "off-check-removed",
        "service": SHARED,
        "test_file": TESTS,
        "file": AGC,
        "old": _OFF_CHECK,
        "new": "        return self._apply_gain(pcm_bytes, self._last_effective_gain)\n",
        "expect": [_OFF],
    },
    {
        # The off check inverted: AGC on returns the raw lead-in, and AGC
        # off applies the stored gain.
        "name": "off-check-inverted",
        "service": SHARED,
        "test_file": TESTS,
        "file": AGC,
        "old": _OFF_CHECK,
        "new": _OFF_CHECK.replace("if not self.config", "if self.config"),
        "expect": [_LEAD_IN, _CAPPED, _CLIPPED, _OFF],
    },
    {
        # The clipping rule bypassed: the raw product wraps around in int16.
        "name": "no-clipping",
        "service": SHARED,
        "test_file": TESTS,
        "file": AGC,
        "old": _OFF_CHECK,
        "new": _OFF_CHECK.replace(
            "        return self._apply_gain(pcm_bytes, self._last_effective_gain)\n",
            "        import numpy as _np\n"
            "        return (_np.frombuffer(pcm_bytes, dtype='<i2')"
            " * self._last_effective_gain).astype('<i2').tobytes()\n"),
        "expect": [_CLIPPED],
    },
]


if __name__ == "__main__":
    sys.exit(run(MUTATIONS))
