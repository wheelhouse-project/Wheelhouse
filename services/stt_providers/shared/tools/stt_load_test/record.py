"""Build the recording the --playback run plays through the speakers.

Nothing here runs in a default run. A person speaks the six sentences in
that one, because ENABLE_AUDIO_SUPPRESSION stops WheelHouse listening while
sound plays through the speakers. This module is what a future headset loop
would use, where the playback reaches the microphone without reaching the
speakers.

The six sentences are synthesized with edge-tts, the same neural TTS
services/stt_providers/evaluation/generate_corpus.py already uses to build
the evaluation corpus, and joined into one 16 kHz mono PCM WAV with silence
between them. Nothing here feeds audio to a recognizer: the WAV exists so
the run can play it out of the speakers and let the real microphone capture
path hear it, which is the only way the test measures what it claims to.

The file is cached beside this module under a name derived from everything
it depends on -- the sentences, the voice, and the silence, which comes
from the provider's endpoint setting. A run whose inputs differ in any of
those asks for a different name, so it cannot be handed the previous file.
generate_corpus.py learned that lesson the same way: audio that looks
current but was built from different text is worse than no audio, because
nothing downstream can tell.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path

from .script import (LEAD_IN_S, SENTENCES, inter_sentence_gap_s, lead_out_s)

# The evaluation corpus uses this voice for half its utterances. One voice is
# enough here: this test measures the machine under load, not the model
# against a range of speakers.
DEFAULT_VOICE = 'en-US-AriaNeural'

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

# crewcut: this directory keeps one recording per distinct setting and
# nothing ever removes one, so it grows -- a changed sentence, a changed
# endpoint setting or a bumped _FORMAT_VERSION each leave the previous
# recording where it is and add another one to two megabytes beside it. To
# remove the limit, sweep the directory after a successful build and delete
# every recording except the one this run asked for; that sweep has to be
# safe against a second run that is reading a file this one would delete,
# which is why it is not here. The same directory in an existing checkout
# also holds an orphaned fixed-script.wav and fixed-script.json, left by
# the fixed-name cache this replaced. Nothing reads either file any more,
# and nothing here deletes them.
CACHE_DIR = Path(__file__).resolve().parent / 'recording'

# Bumped when the way the file is built changes in a way the manifest fields
# below cannot express. A stale cached WAV then regenerates instead of being
# reused for a shape it no longer has.
_FORMAT_VERSION = 1


def manifest_for(endpoint_silence_ms: float, voice: str) -> dict:
    """Everything the cached WAV depends on, as plain data.

    Anything that changes the audio has to appear here, or a stale file is
    reused for a run it no longer matches. ``recording_name`` hashes the
    whole of what this returns, so a field added here reaches the file name
    without anything else being touched -- and a field removed from here
    stops separating recordings that differ only in it.
    """
    return {
        'format_version': _FORMAT_VERSION,
        'voice': voice,
        'sample_rate': SAMPLE_RATE,
        'channels': CHANNELS,
        'sample_width': SAMPLE_WIDTH,
        'lead_in_s': LEAD_IN_S,
        'gap_s': inter_sentence_gap_s(endpoint_silence_ms),
        'lead_out_s': lead_out_s(endpoint_silence_ms),
        'sentences': list(SENTENCES),
    }


def recording_name(wanted: dict) -> str:
    """The file name of the recording ``wanted`` describes.

    The name carries the identity, which is what makes the cache safe to
    publish in one step. A fixed name carried none: the WAV was replaced
    first and the file describing it written second, so between those two
    writes the cache held new audio behind an old description. An
    interrupt made that state permanent and a second run could read it
    with no interrupt at all; either way a later run for the original
    inputs found a description that matched and played audio built for
    something else.

    The digest is taken over the whole of ``wanted`` in its canonical JSON
    form, never over a hand-picked list of inputs. ``manifest_for`` is
    already the complete description of what the audio depends on, so
    hashing everything it returns is complete by construction and stays
    complete when a field is added to it. A name assembled from named
    inputs would go silently wrong the first time somebody adds a field
    and does not come back here -- and that failure needs no interrupt at
    all, which makes it worse than the one this replaced.
    """
    digest = hashlib.sha256(
        json.dumps(wanted, sort_keys=True).encode('utf-8')).hexdigest()
    return f'fixed-script-{digest}.wav'


async def _synthesize(text: str, voice: str) -> bytes:
    """The MP3 bytes edge-tts produces for one sentence."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk['type'] == 'audio':
            buffer.write(chunk['data'])
    return buffer.getvalue()


def _export_atomically(track, wav_path: Path) -> None:
    """Write the finished WAV in one step, or leave the old one alone.

    ``--regenerate`` rebuilds a cache whose manifest already matches, and
    an export straight to the final path opens it in truncating mode. An
    interrupt then leaves a partial WAV beside a manifest that still calls
    the cache current, and the next ordinary --playback run reuses that
    file without checking it. Exporting to a neighbouring file and moving
    it over the target makes the replacement all-or-nothing;
    ``os.replace`` is atomic on Windows and on POSIX.
    """
    # Named for this process, so a second copy of the tool cannot move
    # its half-exported file over this one's target.
    temp = wav_path.with_name(
        f'{wav_path.name}.stt-load-test.{os.getpid()}.tmp')
    try:
        track.export(str(temp), format='wav')
        os.replace(temp, wav_path)
    finally:
        if temp.exists():
            temp.unlink()


def _build(wav_path: Path, wanted: dict) -> None:
    """Synthesize every sentence and write the joined WAV."""
    from pydub import AudioSegment

    def silence(seconds: float):
        return AudioSegment.silent(duration=int(seconds * 1000),
                                   frame_rate=SAMPLE_RATE)

    track = silence(wanted['lead_in_s'])
    for position, sentence in enumerate(wanted['sentences']):
        mp3 = asyncio.run(_synthesize(sentence, wanted['voice']))
        spoken = AudioSegment.from_mp3(io.BytesIO(mp3))
        spoken = (spoken.set_frame_rate(SAMPLE_RATE)
                  .set_channels(CHANNELS)
                  .set_sample_width(SAMPLE_WIDTH))
        track += spoken
        # The gap after the last sentence is the lead-out: the sixth
        # utterance still has to reach its endpoint, and a file that stops
        # at the last word leaves it open when the run reads the log.
        track += silence(wanted['gap_s'] if position < len(wanted['sentences']) - 1
                         else wanted['lead_out_s'])

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    _export_atomically(track, wav_path)


def ensure_recording(endpoint_silence_ms: float, voice: str = DEFAULT_VOICE,
                     cache_dir: Path = CACHE_DIR,
                     force: bool = False) -> tuple[Path, bool]:
    """The WAV to play, building it first if it is missing or stale.

    Returns the path and whether this call built it. Building needs network:
    edge-tts is Microsoft's hosted neural TTS. The cached file is reused on
    every later run, so the network is needed once per change.

    Reuse is decided by one question -- is there a file with this name --
    and by nothing else, because ``recording_name`` puts every input into
    the name. No second file has to agree with the WAV for the answer to be
    right, so there is no window in which the two can disagree, and an
    interrupt anywhere in this function leaves the cache holding only
    recordings that match their own names.
    """
    wanted = manifest_for(endpoint_silence_ms, voice)
    wav_path = cache_dir / recording_name(wanted)

    if not force and wav_path.is_file():
        return wav_path, False

    _build(wav_path, wanted)
    # For a person reading the cache directory, which is otherwise a list
    # of digests. It is named from the same digest as the WAV, so the two
    # cannot describe different recordings, and nothing reads it back --
    # losing it to an interrupt costs the reader a note and costs the run
    # nothing.
    wav_path.with_suffix('.json').write_text(
        json.dumps(wanted, indent=2), encoding='utf-8')
    return wav_path, True
