"""Generate TTS test corpus for STT model evaluation.

Synthesizes WheelHouse command vocabulary into 16kHz mono PCM WAV files
using edge-tts (Microsoft neural TTS). Produces a manifest.json mapping
every WAV file to its ground truth transcription.

Usage:
    cd services/stt_providers/shared
    uv run python ../evaluation/generate_corpus.py

Reference: docs/design/tts_test_corpus_design.md
"""

import asyncio
import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
from pydub import AudioSegment

from vocabulary import Utterance, build_vocabulary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────

VOICES = [
    ("en-US-AriaNeural", "aria"),
    ("en-US-GuyNeural", "guy"),
]

TARGET_SAMPLE_RATE = 16000  # Hz
TARGET_CHANNELS = 1  # mono
TARGET_SAMPLE_WIDTH = 2  # 16-bit

CORPUS_DIR = Path(__file__).parent / "corpus"

# Litmus test: "delete" with rate/voice variants
LITMUS_VARIANTS = [
    {"voice": "en-US-AriaNeural", "shortname": "aria", "rate": "+0%", "tag": "normal"},
    {"voice": "en-US-GuyNeural", "shortname": "guy", "rate": "+0%", "tag": "normal"},
    {"voice": "en-US-AriaNeural", "shortname": "aria", "rate": "-20%", "tag": "slow"},
    {"voice": "en-US-AriaNeural", "shortname": "aria", "rate": "+30%", "tag": "fast"},
    {"voice": "en-US-AriaNeural", "shortname": "aria", "rate": "+0%", "tag": "soft"},
]


# ── Helpers ──────────────────────────────────────────────────────


def sanitize_filename(text: str) -> str:
    """Convert utterance text to a safe filename component."""
    return text.lower().replace(" ", "_").replace("'", "")


def text_marker_path(wav_path: Path) -> Path:
    """Sidecar path that records the source text used to synthesize a WAV.

    Two utterances can sanitize to the same filename if their text only
    differs in characters that sanitize_filename() drops (case, spaces,
    apostrophes). The marker holds the exact source text so a later run
    can detect the change and regenerate the audio instead of silently
    reusing a stale WAV.
    """
    return wav_path.with_suffix(".txt")


def wav_is_current(wav_path: Path, source_text: str) -> bool:
    """Return True if the existing WAV matches the given source text.

    A WAV is current when both the audio file and its sidecar marker
    exist on disk and the marker contents equal source_text exactly. If
    the marker is missing the WAV is treated as stale so the next run
    rewrites both files together.
    """
    if not wav_path.exists():
        return False
    marker = text_marker_path(wav_path)
    if not marker.exists():
        return False
    try:
        recorded = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    return recorded == source_text


def write_text_marker(wav_path: Path, source_text: str) -> None:
    """Write the source text alongside the WAV for stale-detection."""
    text_marker_path(wav_path).write_text(source_text, encoding="utf-8")


def category_dir(category: str) -> str:
    """Map category to subdirectory name."""
    return {
        "single_word": "single_word",
        "multi_word": "multi_word",
        "parameterized": "parameterized",
        "punctuation": "punctuation",
        "dictation": "dictation",
        "discontinuous": "discontinuous",
        "itn": "itn",
        "litmus": "litmus",
    }[category]


def ensure_output_dirs() -> None:
    """Create corpus directory structure."""
    for subdir in [
        "single_word",
        "multi_word",
        "parameterized",
        "punctuation",
        "dictation",
        "discontinuous",
        "itn",
        "litmus",
    ]:
        (CORPUS_DIR / subdir).mkdir(parents=True, exist_ok=True)


async def synthesize_to_mp3(text: str, voice: str, rate: str = "+0%") -> bytes:
    """Synthesize text to MP3 bytes using edge-tts."""
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    mp3_buffer = io.BytesIO()

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_buffer.write(chunk["data"])

    return mp3_buffer.getvalue()


def convert_to_wav(mp3_bytes: bytes) -> bytes:
    """Convert MP3 bytes to 16kHz mono 16-bit PCM WAV."""
    audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
    audio = audio.set_frame_rate(TARGET_SAMPLE_RATE)
    audio = audio.set_channels(TARGET_CHANNELS)
    audio = audio.set_sample_width(TARGET_SAMPLE_WIDTH)

    wav_buffer = io.BytesIO()
    audio.export(wav_buffer, format="wav")
    return wav_buffer.getvalue()


# ── Generation ───────────────────────────────────────────────────


async def generate_standard_utterance(
    utterance: Utterance,
    voice_id: str,
    voice_short: str,
) -> dict | None:
    """Generate a single WAV file for a standard (non-litmus) utterance.

    Returns a manifest entry dict on success, None on failure. The
    synthesis step is skipped when the WAV is current: both the audio
    file and its sidecar marker exist, and the marker matches
    utterance.text exactly. If the marker is missing or its contents
    differ (the spoken text was edited but the sanitized filename did
    not change), the audio is resynthesized and the marker rewritten.
    The manifest entry is produced either way so expected_transcription
    updates always propagate.
    """
    subdir = category_dir(utterance.category)
    sanitized = sanitize_filename(utterance.text)
    filename = f"{sanitized}_{voice_short}.wav"
    rel_path = f"{subdir}/{filename}"
    abs_path = CORPUS_DIR / subdir / filename

    try:
        if not wav_is_current(abs_path, utterance.text):
            if abs_path.exists():
                log.info("  [!] regenerating %s (text changed)", rel_path)
            mp3_data = await synthesize_to_mp3(utterance.text, voice_id)
            wav_data = convert_to_wav(mp3_data)
            abs_path.write_bytes(wav_data)
            write_text_marker(abs_path, utterance.text)

        return {
            "id": f"{subdir[:2]}_{sanitized}_{voice_short}",
            "text": utterance.text,
            "expected_transcription": utterance.expected_transcription,
            "category": utterance.category,
            "voice": voice_id,
            "rate": "+0%",
            "file": rel_path,
            "is_litmus": False,
        }
    except Exception:
        log.exception("Failed to generate: %s (%s)", utterance.text, voice_short)
        return None


async def generate_litmus_variants() -> list[dict]:
    """Generate the litmus test variants for 'delete'."""
    manifest_entries = []

    for variant in LITMUS_VARIANTS:
        tag = variant["tag"]
        shortname = variant["shortname"]
        filename = f"delete_{shortname}_{tag}.wav"
        rel_path = f"litmus/{filename}"
        abs_path = CORPUS_DIR / "litmus" / filename

        # For the "soft" variant we use lower volume via SSML prosody.
        # edge-tts doesn't support volume directly in Communicate for all
        # voices, so we route through prosody volume.
        rate = variant["rate"]
        voice = variant["voice"]

        try:
            if not wav_is_current(abs_path, "delete"):
                if abs_path.exists():
                    log.info("  [!] regenerating %s (text changed)", rel_path)
                if tag == "soft":
                    communicate = edge_tts.Communicate(
                        "delete", voice, rate=rate, volume="-50%"
                    )
                    mp3_buffer = io.BytesIO()
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            mp3_buffer.write(chunk["data"])
                    mp3_data = mp3_buffer.getvalue()
                else:
                    mp3_data = await synthesize_to_mp3("delete", voice, rate)

                wav_data = convert_to_wav(mp3_data)
                abs_path.write_bytes(wav_data)
                write_text_marker(abs_path, "delete")

            manifest_entries.append(
                {
                    "id": f"lt_delete_{shortname}_{tag}",
                    "text": "delete",
                    "expected_transcription": "delete",
                    "category": "litmus",
                    "voice": voice,
                    "rate": rate,
                    "variant": tag,
                    "file": rel_path,
                    "is_litmus": True,
                }
            )
            log.info("  [+] litmus: delete (%s, %s)", shortname, tag)
        except Exception:
            log.exception("Failed litmus variant: delete (%s, %s)", shortname, tag)

    return manifest_entries


async def generate_all() -> tuple[list[dict], int]:
    """Generate all corpus WAV files.

    Returns (manifest_entries, failure_count).
    """
    vocabulary = build_vocabulary()
    ensure_output_dirs()

    manifest_entries: list[dict] = []
    failures = 0

    # Standard utterances: skip litmus entries (handled separately)
    standard = [u for u in vocabulary if u.category != "litmus"]

    total = len(standard) * len(VOICES) + len(LITMUS_VARIANTS)
    log.info("Generating %d WAV files from %d utterances...", total, len(standard) + 1)

    for utterance in standard:
        for voice_id, voice_short in VOICES:
            entry = await generate_standard_utterance(utterance, voice_id, voice_short)
            if entry:
                manifest_entries.append(entry)
                log.info(
                    "  [%d/%d] %s (%s)",
                    len(manifest_entries),
                    total,
                    utterance.text,
                    voice_short,
                )
            else:
                failures += 1

    # Litmus tests
    litmus_entries = await generate_litmus_variants()
    manifest_entries.extend(litmus_entries)

    return manifest_entries, failures


def write_manifest(entries: list[dict]) -> None:
    """Write manifest.json to corpus directory."""
    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "tts_engine": "edge-tts",
        "format": "16kHz mono PCM WAV",
        "voices": {short: full for full, short in VOICES},
        "utterance_count": len(entries),
        "utterances": entries,
    }

    manifest_path = CORPUS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Manifest written: %s (%d entries)", manifest_path, len(entries))


def print_summary(entries: list[dict], failures: int, elapsed: float) -> None:
    """Print generation summary by category."""
    categories: dict[str, int] = {}
    for entry in entries:
        cat = entry["category"]
        categories[cat] = categories.get(cat, 0) + 1

    print("\n" + "=" * 50)
    print("TTS Corpus Generation Summary")
    print("=" * 50)
    for cat, count in sorted(categories.items()):
        print(f"  {cat:20s} {count:4d} files")
    print(f"  {'TOTAL':20s} {len(entries):4d} files")
    if failures:
        print(f"  {'FAILURES':20s} {failures:4d}")
    print(f"\n  Time: {elapsed:.1f}s")
    print(f"  Output: {CORPUS_DIR}")
    print("=" * 50)


# ── Main ─────────────────────────────────────────────────────────


async def main() -> int:
    """Entry point."""
    log.info("TTS Corpus Generator")
    log.info("Output: %s", CORPUS_DIR)

    start = time.monotonic()
    entries, failures = await generate_all()
    elapsed = time.monotonic() - start

    write_manifest(entries)
    print_summary(entries, failures, elapsed)

    if failures:
        log.warning("%d utterances failed -- re-run to retry", failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
