"""The fixed script the run dictates, and the silence between its sentences.

One source of truth for three readers: run.py prompts a person through these
sentences one at a time, record.py synthesizes the same ones into the WAV the
optional --playback run uses, and judge.py compares what the recognizer wrote
against them. A copy in any of the three could drift, and the whole point of
a fixed script is that two runs said the same words.

The silences are the same in both cases, and they are load-bearing rather
than decorative: the provider closes an utterance after endpoint_silence_ms
of quiet, so two sentences with too little between them arrive as one
utterance under a single utt=N, and the per-sentence join in judge.py has
nothing to tell them apart with.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared_stt.transcript_rules import apply_itn, normalize_transcript

PROCEDURE_DOC = (Path(__file__).resolve().parents[5]
                 / 'docs' / 'testing' / 'stt-cpu-load-test-procedure.md')

# The fixed script from the procedure, verbatim. A test reads the list back
# out of the document and fails if the two ever differ.
#
# No sentence ends with a period. Wheelhouse is a dictation system, so a
# reader who speaks the punctuation puts the word "period" in the transcript,
# and the recognizer mishears it as often as not -- the live run of
# 2026-08-30 produced "the quick brown fox jumps over the lazy dog kid" and
# "check the microphone little period". Every one of those marked a sentence
# wrong for a reason that had nothing to do with load, which is the only
# thing this test measures. Removing the punctuation removes the cause and
# lets the comparison stay exact.
SENTENCES: tuple[str, ...] = (
    "The quick brown fox jumps over the lazy dog",
    "Please open the settings window and check the microphone level",
    "November December January February March April",
    "Seventeen thousand four hundred and thirty two",
    "She sells sea shells by the sea shore on Saturday",
    "Close the window and stop listening now",
)

# The provider's coded default when config.toml names no value; see
# sherpa_offline_parakeet_stt_server/main.py, which passes
# engine_config.get("endpoint_silence_ms", 800) to both the engine and the
# forced-endpoint path.
DEFAULT_ENDPOINT_SILENCE_MS = 800

# How much longer than the endpoint threshold the silence between sentences
# runs. The threshold is what the provider needs when it is keeping up; this
# test deliberately makes it not keep up, and the endpoint decision is one of
# the things that gets slow. Three times the threshold is the margin.
_GAP_MULTIPLE = 3.0

# Silence before the first sentence and after the last. The lead-in gives the
# VAD a noise floor to measure before any speech arrives, and the lead-out
# lets the sixth sentence reach its endpoint before the file stops playing --
# without it the last utterance can still be open when the run ends.
LEAD_IN_S = 2.0


def inter_sentence_gap_s(endpoint_silence_ms: float) -> float:
    """Seconds of silence to leave between two sentences.

    Derived from the provider's own setting rather than fixed, because the
    per-sentence join in judge.py depends on each sentence becoming its own
    utterance: the provider ends one after endpoint_silence_ms of trailing
    silence, and two sentences inside one utterance share a single utt=N.
    """
    return _GAP_MULTIPLE * float(endpoint_silence_ms) / 1000.0


def lead_out_s(endpoint_silence_ms: float) -> float:
    """Seconds of silence after the last sentence.

    The same margin as between sentences: the sixth utterance has to end
    before the run stops reading the log, or its [load-diag] utt= line is
    never written and the sentence reads as missing.
    """
    return inter_sentence_gap_s(endpoint_silence_ms)


def expected_transcript(sentence: str) -> str:
    """The text the provider would write for ``sentence``, spoken cleanly.

    The log does not carry what was said; it carries what the recognizer
    produced after the provider's own text rules. sherpa_engine.py applies
    normalize_transcript then apply_itn to every hypothesis, in that order,
    so "Seventeen thousand four hundred and thirty two" reaches the log as a
    numeral. Comparing against the spoken words would report that correct
    transcript as wrong.
    """
    return apply_itn(normalize_transcript(sentence))


@dataclass(frozen=True)
class Prompt:
    """One sentence, and the silence that has to precede it.

    The silence is the point. In the recorded run it was silence in the WAV
    file; with a person speaking it is time the prompt is withheld, which is
    the only way the script can make the speaker leave a gap. A prompt shown
    early is read aloud early, and two sentences inside one utterance share a
    single utt=N -- which is what the per-sentence join in judge.py needs to
    be able to tell them apart.
    """
    index: int
    sentence: str
    silence_before_s: float


def prompt_plan(endpoint_silence_ms: float) -> tuple[Prompt, ...]:
    """The six sentences with the pacing a person has to be held to.

    The first waits out LEAD_IN_S so the VAD has a noise floor before any
    speech arrives; the rest wait out the same margin over the provider's
    endpoint threshold that the recording's silences carried.
    """
    gap = inter_sentence_gap_s(endpoint_silence_ms)
    return tuple(
        Prompt(index=position + 1, sentence=sentence,
               silence_before_s=LEAD_IN_S if position == 0 else gap)
        for position, sentence in enumerate(SENTENCES))
