"""Validate and write calibrated single-word rescue settings (wh-7ou.7.1.3).

Provider-side core of the apply_engine_settings command (voice calibration,
spec Section 5.3): the Logic process sends the two calibrated thresholds,
and the provider writes them into its own config.toml [engine] section
before hard-restarting so the new values load.

Two deliberate properties:

- The allowed-key list is fixed at exactly the two single-word rescue
  settings. The command must never grow into a generic remote config
  writer; in particular hallucination_logprob_threshold is not calibrated
  by design (spec decision 3).
- The write is a line-based edit, not a parse-and-serialize round trip:
  the provider config files carry calibration history and tuning guidance
  in comments, and a serializer would destroy them. tomllib (stdlib) has
  no writer, and a comment-preserving writer dependency is not worth
  adding for a two-key edit.
"""
import math
import os
import re
import tomllib
from pathlib import Path

# Contract A.3: exactly these two keys are legal, nothing else ever.
ALLOWED_ENGINE_SETTINGS = (
    "single_word_min_probability",
    "single_word_max_no_speech_prob",
)

_SECTION_HEADER_RE = re.compile(r"^\s*\[")
_ENGINE_HEADER_RE = re.compile(r"^\s*\[engine\]\s*(?:#.*)?$")


def validate_engine_settings(settings) -> tuple[dict[str, float], str | None]:
    """Return (normalized settings, None) or ({}, error text).

    The error text travels verbatim in engine_settings_result and lands
    under the calibration window's "Show details", so it names the exact
    key and value that failed. Booleans are rejected before the numeric
    check because bool is an int subclass and TOML true/false must never
    pass as 1.0/0.0 (same rationale as whisper_engine._coerce_threshold,
    wh-7ou.6.1.7); NaN and infinities are rejected because neither is a
    usable probability. The range is inclusive: 0.0 and 1.0 are the
    documented off-switch values. An empty dict is an error rather than a
    no-op so a buggy caller cannot trigger a pointless provider restart.
    """
    if not isinstance(settings, dict):
        return {}, f"settings must be an object, got {type(settings).__name__}"
    unknown = [key for key in settings if key not in ALLOWED_ENGINE_SETTINGS]
    if unknown:
        return {}, "unknown setting(s): " + ", ".join(sorted(str(k) for k in unknown))
    if not settings:
        return {}, "no settings provided"
    clean: dict[str, float] = {}
    for key in ALLOWED_ENGINE_SETTINGS:
        if key not in settings:
            continue  # write-or-keep: an absent key keeps its current value
        value = settings[key]
        error = f"{key} must be a number between 0 and 1, got {value!r}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}, error
        try:
            coerced = float(value)
        except OverflowError:
            return {}, error
        if math.isnan(coerced) or math.isinf(coerced):
            return {}, error
        if not (0.0 <= coerced <= 1.0):
            return {}, error
        clean[key] = coerced
    return clean, None


def _format_value(value: float) -> str:
    # repr of a float is always a valid TOML float ("0.15", "1.0", "2e-05").
    return repr(float(value))


def write_engine_settings(config_path: Path, settings: dict[str, float]) -> None:
    """Write validated settings into config_path's [engine] section.

    Preserves every other line byte for byte: comments, other keys, other
    sections, same-line comments after a rewritten value, and the file's
    newline convention (a CRLF config stays pure CRLF). A key missing from
    the section is inserted at the end of the section body; a missing
    [engine] section is appended. The edited content is re-parsed with
    tomllib before anything touches disk, and the write itself is a
    temp-file-plus-os.replace so a failure can never leave a truncated
    config behind. Raises on any failure; callers report the error text
    and must not restart (spec Section 8).
    """
    config_path = Path(config_path)
    content = config_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)

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
        for key in ALLOWED_ENGINE_SETTINGS:
            if key in settings:
                lines.append(f"{key} = {_format_value(settings[key])}{newline}")
    else:
        engine_end = len(lines)
        for i in range(engine_start + 1, len(lines)):
            if _SECTION_HEADER_RE.match(lines[i]):
                engine_end = i
                break

        for key in ALLOWED_ENGINE_SETTINGS:
            if key not in settings:
                continue
            formatted = _format_value(settings[key])
            # Value part is a bare TOML float (no '#', no whitespace), so a
            # trailing same-line comment is cleanly captured and kept.
            key_re = re.compile(
                r"^(\s*" + re.escape(key) + r"\s*=\s*)(.*?)(\s*(?:#.*)?)$"
            )
            for i in range(engine_start + 1, engine_end):
                stripped = lines[i].rstrip("\r\n")
                eol = lines[i][len(stripped):]
                match = key_re.match(stripped)
                if match:
                    lines[i] = match.group(1) + formatted + match.group(3) + eol
                    break
            else:
                insert_at = engine_end
                while (
                    insert_at > engine_start + 1
                    and lines[insert_at - 1].strip() == ""
                ):
                    insert_at -= 1
                if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
                    lines[insert_at - 1] += newline
                lines.insert(insert_at, f"{key} = {formatted}{newline}")
                engine_end += 1

    new_content = "".join(lines)

    # Prove the edit before it reaches disk: the rewritten file must parse,
    # and every requested value must read back exactly (repr round-trips a
    # float bit for bit through tomllib).
    parsed_engine = tomllib.loads(new_content).get("engine", {})
    for key, value in settings.items():
        if parsed_engine.get(key) != float(value):
            raise ValueError(
                f"config edit verification failed for {key}: "
                f"wrote {float(value)!r}, parsed {parsed_engine.get(key)!r}"
            )

    tmp_path = config_path.with_name(config_path.name + ".tmp")
    try:
        tmp_path.write_bytes(new_content.encode("utf-8"))
        os.replace(tmp_path, config_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
