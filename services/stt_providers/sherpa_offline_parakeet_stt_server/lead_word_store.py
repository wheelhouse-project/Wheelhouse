"""The files and the config key of the training word (wh-first-sound-lost.23).

The training word is the recording that the lead-word join of the Parakeet
engine puts in front of the user's audio. This module owns four things:

- the folder %LOCALAPPDATA%\\WheelHouse\\lead-word\\ and its files;
- the reader and the writer of the [engine] key lead_word_path in the
  provider config.toml;
- the store: Keep, restore, turn off, turn on, discard, and status, which
  change the files and the key together;
- the one place that says when a change is complete.

No engine import is needed. Every store method that changes the word takes a
callable `set_engine_word(audio_or_None)` (the engine method set_lead_word),
so tests give it a fake and the provider gives it the engine's own method.

A store method is not thread safe on its own. The provider calls them from
the forwarder thread under the controller lock.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

# The line editor below follows write_engine_settings in
# shared_stt.engine_settings and reuses its two line patterns.
from shared_stt.engine_settings import _ENGINE_HEADER_RE, _SECTION_HEADER_RE

import lead_word_recorder
from sherpa_engine import LeadWordError, load_lead_word_audio

logger = logging.getLogger(__name__)

FILE_WAV = "hello.wav"                  # the kept recording
FILE_PREVIOUS = "hello.previous.wav"    # one older copy
FILE_CANDIDATE = "hello.candidate.wav"  # passed, not kept yet
FILE_META = "hello.json"                # measures and facts of the kept recording
# The measures file beside the candidate. The contract names four files; this
# fifth one holds the candidate's facts until Keep moves them into hello.json.
FILE_CANDIDATE_META = "hello.candidate.json"
_FILE_SWAP = "hello.swap.tmp"           # exists only while restore swaps two files

KEY_NAME = "lead_word_path"
RECORDED_FORMAT = "%Y-%m-%d %H:%M"


class LeadWordStoreError(Exception):
    """A store action failed. The message is plain text for the window."""


def lead_word_folder() -> Path:
    """The folder of the files. Creates nothing.

    Built from LOCALAPPDATA the way default_folder of
    shared_stt.engine_audio_saver builds the engine-audio folder.
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise KeyError("LOCALAPPDATA")
    return Path(base) / "WheelHouse" / "lead-word"


# ---------------------------------------------------------------------------
# The key in config.toml
# ---------------------------------------------------------------------------

def read_lead_word_key(config_path, fallback: str = "") -> str:
    """The [engine] lead_word_path value, read from config.toml now.

    Follows _hotwords_enabled_now in main.py: the file is read at call time,
    so the answer tracks the file, not a value cached at start. A file that
    cannot be read (for example mid-edit) gives `fallback`. A value that is
    not text counts as empty, as it does for the engine.
    """
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        value = config.get("engine", {}).get(KEY_NAME, "")
        return value if isinstance(value, str) else ""
    except Exception as e:
        logger.warning(
            f"Config re-read failed, using the cached {KEY_NAME}: {e}")
        return fallback


def _toml_string(value: str) -> str:
    """The value as a TOML string: literal when it can be, else basic."""
    if "'" not in value and not any(
        (ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value
    ):
        return f"'{value}'"
    out = []
    for c in value:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif ord(c) < 32 or ord(c) == 127:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _value_end(rest: str) -> int:
    """The index in `rest` just after the TOML value that starts it."""
    if rest.startswith(("'''", '"""')):
        raise ValueError(f"{KEY_NAME} holds a multi-line string; edit it by hand")
    if rest.startswith("'"):
        end = rest.find("'", 1)
        if end < 0:
            raise ValueError(f"{KEY_NAME} holds an unterminated string")
        return end + 1
    if rest.startswith('"'):
        i = 1
        while i < len(rest):
            if rest[i] == "\\":
                i += 2
                continue
            if rest[i] == '"':
                return i + 1
            i += 1
        raise ValueError(f"{KEY_NAME} holds an unterminated string")
    return re.match(r"[^\s#]*", rest).end()


_KEY_HEAD_RE = re.compile(r"^(\s*" + KEY_NAME + r"\s*=\s*)")


def write_lead_word_key(config_path, value: str) -> None:
    """Write the [engine] key lead_word_path in config.toml.

    A line-based edit, as write_engine_settings does it, for the same
    reason: the config files carry guidance in comments and a serializer
    would destroy them. Only the value of the key line changes; the text
    after the value (a same-line comment) stays. When [engine] has no such
    line, the line is added at the end of the section body; when the file has
    no [engine] section, the section is added. Every other line stays byte
    for byte, with the file's own newline convention. The edited text is
    parsed with tomllib and the value must read back exactly before anything
    reaches the disk, and the write is a temp file plus os.replace. Raises on
    any failure.
    """
    config_path = Path(config_path)
    content = config_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    formatted = _toml_string(value)

    engine_start = None
    for i, line in enumerate(lines):
        if _ENGINE_HEADER_RE.match(line.rstrip("\r\n")):
            engine_start = i
            break

    if engine_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += newline
        if lines:
            lines.append(newline)
        lines.append(f"[engine]{newline}")
        lines.append(f"{KEY_NAME} = {formatted}{newline}")
    else:
        engine_end = len(lines)
        for i in range(engine_start + 1, len(lines)):
            if _SECTION_HEADER_RE.match(lines[i]):
                engine_end = i
                break
        for i in range(engine_start + 1, engine_end):
            stripped = lines[i].rstrip("\r\n")
            eol = lines[i][len(stripped):]
            head = _KEY_HEAD_RE.match(stripped)
            if head:
                rest = stripped[head.end():]
                lines[i] = (
                    head.group(1) + formatted + rest[_value_end(rest):] + eol)
                break
        else:
            insert_at = engine_end
            while insert_at > engine_start + 1 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            if not lines[insert_at - 1].endswith("\n"):
                lines[insert_at - 1] += newline
            lines.insert(insert_at, f"{KEY_NAME} = {formatted}{newline}")

    new_content = "".join(lines)
    parsed = tomllib.loads(new_content).get("engine", {}).get(KEY_NAME)
    if parsed != value:
        raise ValueError(
            f"config edit verification failed for {KEY_NAME}: "
            f"wrote {value!r}, parsed {parsed!r}")
    _atomic_write(config_path, new_content.encode("utf-8"))


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _replace(source, target) -> None:
    """The one rename of a Keep or restore step (tests make it fail)."""
    os.replace(source, target)


def _copy_file(source, target) -> None:
    shutil.copyfile(source, target)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` through a temp file and os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _same_file(key_value: str, path: Path) -> bool:
    """True when the key value names `path` (case and slash style ignored)."""
    if not key_value:
        return False
    return os.path.normcase(os.path.abspath(key_value)) == os.path.normcase(
        os.path.abspath(path))


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreStatus:
    """What the window needs, as the store knows it now.

    `key_path` is the [engine] key value read from config.toml now.
    `key_names_other_file` is True when that value is not empty and is not the
    path of hello.wav. With `current_exists` it decides the Keep button text:
    other file, then "replaces the word file <key_path>"; else an existing
    hello.wav, then "replaces the recording from <recorded>"; else plain.
    """

    key_path: str
    key_names_other_file: bool
    current_exists: bool
    previous_exists: bool
    candidate_exists: bool
    recorded: str
    microphone: str


class LeadWordStore:
    """The folder of the training word and the config key that names it."""

    def __init__(self, folder, config_path, now=datetime.now):
        self.folder = Path(folder)
        self.config_path = Path(config_path)
        self._now = now

    # -- paths ------------------------------------------------------------

    @property
    def wav_path(self) -> Path:
        return self.folder / FILE_WAV

    @property
    def previous_path(self) -> Path:
        return self.folder / FILE_PREVIOUS

    @property
    def candidate_path(self) -> Path:
        return self.folder / FILE_CANDIDATE

    @property
    def meta_path(self) -> Path:
        return self.folder / FILE_META

    @property
    def candidate_meta_path(self) -> Path:
        return self.folder / FILE_CANDIDATE_META

    def _all_paths(self) -> list[Path]:
        return [
            self.wav_path, self.previous_path, self.candidate_path,
            self.meta_path, self.candidate_meta_path, self.folder / _FILE_SWAP,
        ]

    # -- facts --------------------------------------------------------------

    def _read_meta(self) -> dict:
        """The facts in hello.json; {} when the file is missing or unreadable."""
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self) -> StoreStatus:
        key = read_lead_word_key(self.config_path)
        current = self.wav_path.exists()
        meta = self._read_meta() if current else {}
        return StoreStatus(
            key_path=key,
            key_names_other_file=bool(key) and not _same_file(key, self.wav_path),
            current_exists=current,
            previous_exists=self.previous_path.exists(),
            candidate_exists=self.candidate_path.exists(),
            recorded=str(meta.get("recorded", "")),
            microphone=str(meta.get("microphone", "")),
        )

    # -- the candidate ------------------------------------------------------

    def save_candidate(self, cut: np.ndarray, measures: dict, microphone: str) -> Path:
        """Write a passed recording as hello.candidate.wav and its measures file.

        Neither file replaces hello.wav. Raises LeadWordStoreError when a
        write fails, and leaves no candidate behind.
        """
        facts = {
            "measures": measures,
            "recorded": self._now().strftime(RECORDED_FORMAT),
            "microphone": microphone,
            "decode": measures.get("decode", ""),
        }
        tmp = self.folder / (FILE_CANDIDATE + ".tmp")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            lead_word_recorder.write_wav(tmp, cut)
            os.replace(tmp, self.candidate_path)
            _atomic_write(
                self.candidate_meta_path,
                json.dumps(facts, indent=2, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            self.discard_candidate()
            raise LeadWordStoreError(f"Cannot save the new recording: {e}") from e
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return self.candidate_path

    def discard_candidate(self) -> bool:
        """Delete hello.candidate.wav and its measures file. True if one existed."""
        removed = False
        for path in (self.candidate_path, self.candidate_meta_path):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning(f"Cannot delete {path}: {e}")
        return removed

    def delete_refused(self) -> None:
        """A refused recording leaves no candidate. Same as discard."""
        self.discard_candidate()

    # -- snapshot and undo --------------------------------------------------

    def _snapshot(self) -> dict:
        try:
            snap = {path: _read_or_none(path) for path in self._all_paths()}
            snap["config"] = _read_or_none(self.config_path)
        except OSError as e:
            raise LeadWordStoreError(f"Cannot read the training word files: {e}") from e
        return snap

    def _undo(self, snap: dict, key_touched: bool) -> list[str]:
        """Put every file (and the config, when touched) back as in `snap`."""
        problems = []
        for path in self._all_paths():
            try:
                before = snap[path]
                if before is None:
                    path.unlink(missing_ok=True)
                elif _read_or_none(path) != before:
                    _atomic_write(path, before)
            except Exception as e:
                problems.append(f"{path.name}: {e}")
        if key_touched and snap["config"] is not None:
            try:
                if _read_or_none(self.config_path) != snap["config"]:
                    _atomic_write(self.config_path, snap["config"])
            except Exception as e:
                problems.append(f"{self.config_path.name}: {e}")
        return problems

    def _fail(self, what: str, error: Exception, snap: dict, key_touched: bool):
        problems = self._undo(snap, key_touched)
        message = f"{what}: {error}"
        if problems:
            message += " (the earlier state could not be fully put back: " + "; ".join(problems) + ")"
            logger.error(message)
        else:
            logger.warning(f"{message} (everything put back)")
        return LeadWordStoreError(message)

    def _load(self, path: Path) -> np.ndarray:
        try:
            return load_lead_word_audio(str(path), lead_word_recorder.SAMPLE_RATE)
        except LeadWordError as e:
            raise LeadWordStoreError(f"{path.name} cannot be used: {e}") from e

    def _write_json(self, path: Path, data: dict) -> None:
        _atomic_write(
            path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))

    def _set_key(self, value: str) -> None:
        write_lead_word_key(self.config_path, value)

    # -- Keep ---------------------------------------------------------------

    def keep(self, set_engine_word) -> StoreStatus:
        """Make hello.candidate.wav the training word.

        In this order: hello.wav becomes hello.previous.wav (replacing an older
        copy); hello.candidate.wav becomes hello.wav; hello.json is written;
        the key is written when it is empty, absent, or names another file;
        the engine loads the new word. At the first Keep (no hello.wav and no
        hello.previous.wav) a key that names another file has that file copied
        to hello.previous.wav first; the file itself is never changed. The
        candidate is checked with load_lead_word_audio before any change.

        When any step fails, every file and the key are put back, the engine
        is not changed, and LeadWordStoreError carries the error text.
        """
        if not self.candidate_path.exists():
            raise LeadWordStoreError("No new recording to keep")
        try:
            candidate_facts = json.loads(
                self.candidate_meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise LeadWordStoreError(
                f"The measures file of the new recording cannot be read: {e}") from e
        new_audio = self._load(self.candidate_path)
        snap = self._snapshot()
        old_key = read_lead_word_key(self.config_path)
        key_is_wav = _same_file(old_key, self.wav_path)
        old_meta = self._read_meta()
        had_current = self.wav_path.exists()
        had_previous = self.previous_path.exists()
        key_touched = False
        try:
            previous_facts = old_meta.get("previous")
            old_key_value = ""
            if had_current:
                _replace(self.wav_path, self.previous_path)
                previous_facts = {k: v for k, v in old_meta.items() if k != "previous"}
            elif not had_previous and old_key and not key_is_wav:
                if self._copy_key_file(old_key):
                    previous_facts = {
                        "measures": {}, "recorded": "", "microphone": "",
                        "decode": "", "source": old_key}
            if old_key and not key_is_wav:
                old_key_value = old_key
            _replace(self.candidate_path, self.wav_path)
            facts = dict(candidate_facts)
            if old_key_value:
                facts["old_key_value"] = old_key_value
            if previous_facts:
                facts["previous"] = previous_facts
            self._write_json(self.meta_path, facts)
            if not key_is_wav:
                key_touched = True
                self._set_key(str(self.wav_path))
            set_engine_word(new_audio)
        except Exception as e:
            raise self._fail("Keep failed", e, snap, key_touched) from e
        try:
            self.candidate_meta_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Cannot delete {self.candidate_meta_path}: {e}")
        if old_key_value:
            logger.info(
                f"Training word: the key {KEY_NAME} named '{old_key_value}'; Keep "
                f"set it to {self.wav_path}. To use the old file again, set the "
                f"key to that path by hand.")
        logger.info(f"Training word kept: {self.wav_path}")
        return self.status()

    def _copy_key_file(self, old_key: str) -> bool:
        """Copy the file that the key names to hello.previous.wav.

        False when that file does not exist (nothing to keep). The file is
        read and never changed.
        """
        tmp = self.folder / (FILE_PREVIOUS + ".tmp")
        try:
            _copy_file(old_key, tmp)
            _replace(tmp, self.previous_path)
            return True
        except FileNotFoundError:
            return False
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # -- Go back to the previous recording ---------------------------------

    def restore(self, set_engine_word) -> StoreStatus:
        """Swap hello.wav and hello.previous.wav and use the restored word.

        hello.json swaps its facts the same way, the key is set to the path of
        hello.wav, and the engine loads it. A second call goes back to the
        recording that the first call replaced. Failure puts everything back.
        """
        if not self.previous_path.exists():
            raise LeadWordStoreError("There is no previous recording")
        audio = self._load(self.previous_path)
        snap = self._snapshot()
        key_is_wav = _same_file(read_lead_word_key(self.config_path), self.wav_path)
        meta = self._read_meta()
        had_current = self.wav_path.exists()
        swap = self.folder / _FILE_SWAP
        key_touched = False
        try:
            if had_current:
                _replace(self.wav_path, swap)
                _replace(self.previous_path, self.wav_path)
                _replace(swap, self.previous_path)
            else:
                _replace(self.previous_path, self.wav_path)
            facts = dict(meta.get("previous") or {})
            if had_current:
                facts["previous"] = {k: v for k, v in meta.items() if k != "previous"}
            self._write_json(self.meta_path, facts)
            if not key_is_wav:
                key_touched = True
                self._set_key(str(self.wav_path))
            set_engine_word(audio)
        except Exception as e:
            raise self._fail("Going back failed", e, snap, key_touched) from e
        logger.info(f"Training word restored from {self.wav_path}")
        return self.status()

    # -- Turn off and turn on ----------------------------------------------

    def turn_off(self, set_engine_word) -> StoreStatus:
        """Empty the key, keep the files, and clear the engine's word."""
        snap = self._snapshot()
        key_touched = False
        try:
            if read_lead_word_key(self.config_path) != "":
                key_touched = True
                self._set_key("")
            set_engine_word(None)
        except Exception as e:
            raise self._fail("Turning off failed", e, snap, key_touched) from e
        logger.info("Training word turned off; the files are kept")
        return self.status()

    def turn_on(self, set_engine_word) -> StoreStatus:
        """Set the key to hello.wav and load that file into the engine."""
        if not self.wav_path.exists():
            raise LeadWordStoreError("There is no recording to turn on")
        audio = self._load(self.wav_path)
        snap = self._snapshot()
        key_touched = False
        try:
            if not _same_file(read_lead_word_key(self.config_path), self.wav_path):
                key_touched = True
                self._set_key(str(self.wav_path))
            set_engine_word(audio)
        except Exception as e:
            raise self._fail("Turning on failed", e, snap, key_touched) from e
        logger.info(f"Training word turned on: {self.wav_path}")
        return self.status()
