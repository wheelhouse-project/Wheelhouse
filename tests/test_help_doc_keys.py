"""Grep gate: the generated help doc must not document removed local-AI keys.

WheelHouse became a thin client (assignment wf-whai-b / epic wh-ay6h): the
local llama-cpp / Ollama model-hosting config was deleted and replaced by an
[ai.server] section that points at an external OpenAI-compatible server. This
test guards `services/wheelhouse/knowledge/wheelhouse_help.md` so the stale
local-model config keys can never silently re-enter the generated help doc.

Pure stdlib (pathlib + re only); no service imports, no fixtures.
"""
import re
from pathlib import Path

# tests/ -> repo root is one level up from this file's directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELP_DOC = _REPO_ROOT / "services" / "wheelhouse" / "knowledge" / "wheelhouse_help.md"

# Keys/sections that the thin-client migration removed. None of these may
# appear anywhere in the generated help doc.
BANNED_KEYS = (
    "active_model",
    "models_directory",
    "[ai.llamacpp]",
    "ai.llamacpp",
    "[ai.ollama]",
    "ai.ollama",
    "llama-cpp",
    "n_gpu_layers",
    "help_n_ctx",
    "[ai] provider",
    "ai.provider",
)


# Installation guidance retired when the public installer shipped
# (wh-help-gen-refresh). The public release installs with
# install-wheelhouse.ps1; bootstrap.ps1 and the developer-tree assumptions
# (build tools, a system Python prerequisite, the Ollama-based code indexer)
# must never re-enter the generated help doc. Checked case-insensitively.
RETIRED_INSTALL_GUIDANCE = (
    "bootstrap",
    "visual c++ build tools",
    "nomic-embed-text",
    "code indexer",
    "python 3.12",
    "installing python",
)

# Placeholders that must be resolved before the doc ships anywhere
# (wh-help-gen-refresh). The angle-bracket ORG token is the unresolved
# GitHub org from the old template; "[support channel" covers both
# bracketed support-channel placeholder forms; "to be updated" catches any
# leftover deferral marker. The ORG token is spelled as two adjacent
# string pieces so the release export's publish-day placeholder sweep (a
# raw text scan over every exported file, this one included) does not
# trip on the guard's own constant; the runtime value is unchanged.
UNRESOLVED_PLACEHOLDERS = (
    "<OR" "G>",
    "[support channel",
    "to be updated",
)

# Content the regenerated doc must contain: the real public install command
# and the real GitHub project URL (org resolved to wheelhouse-project).
REQUIRED_CURRENT_CONTENT = (
    "install-wheelhouse.ps1",
    "https://github.com/wheelhouse-project/Wheelhouse",
)

# Config claims the doc must never make (codex review, help-kit epic,
# finding wh-llm-help-kit.2.1): there is no api_key slot in [ai.server] --
# the credential comes only from the WHEELHOUSE_AI_API_KEY environment
# variable (ai/providers/openai_compat.py), and test_ai/test_config.py
# forbids the key in the tracked config. Telling users to set it sends
# them to a setting that does not exist.
BANNED_CONFIG_CLAIMS = (
    "[ai.server] api_key",
    "[ai.server] `api_key`",
    "api_key =",
)

# Feature-state notes the doc must carry (codex review finding
# wh-llm-help-kit.2.2): the in-app help chat's voice patterns are disabled
# in the shipped catalog (tests/test_help_patterns_disabled.py pins that),
# so the doc must say so instead of presenting the chat as reachable.
# Checked case-insensitively.
REQUIRED_FEATURE_STATE_NOTES = (
    "in-app help chat is currently disabled",
)


def test_help_doc_exists():
    assert _HELP_DOC.is_file(), f"help doc not found at {_HELP_DOC}"


def test_help_doc_has_no_banned_local_ai_keys():
    content = _HELP_DOC.read_text(encoding="utf-8")
    offenders = [key for key in BANNED_KEYS if key in content]
    assert not offenders, (
        "wheelhouse_help.md still documents removed local-AI config keys: "
        f"{offenders}. WheelHouse is a thin client; document the [ai.server] "
        "section instead."
    )


def test_help_doc_has_no_retired_install_guidance():
    content = _HELP_DOC.read_text(encoding="utf-8").lower()
    offenders = [key for key in RETIRED_INSTALL_GUIDANCE if key in content]
    assert not offenders, (
        "wheelhouse_help.md still contains retired install guidance: "
        f"{offenders}. The public release installs with "
        "install-wheelhouse.ps1 (see scripts/release/public/INSTALL.md); "
        "bootstrap.ps1 and developer-tree setup no longer exist for users."
    )


def test_help_doc_has_no_unresolved_placeholders():
    content = _HELP_DOC.read_text(encoding="utf-8")
    offenders = [key for key in UNRESOLVED_PLACEHOLDERS if key in content]
    assert not offenders, (
        "wheelhouse_help.md still contains unresolved placeholders: "
        f"{offenders}. The GitHub org is wheelhouse-project and the support "
        "channel is the project's GitHub issues page."
    )


def test_help_doc_documents_current_install_path():
    content = _HELP_DOC.read_text(encoding="utf-8")
    missing = [key for key in REQUIRED_CURRENT_CONTENT if key not in content]
    assert not missing, (
        "wheelhouse_help.md is missing required current install content: "
        f"{missing}. The doc must describe the install-wheelhouse.ps1 "
        "workflow and point at the real GitHub project."
    )


def test_help_doc_has_no_banned_config_claims():
    content = _HELP_DOC.read_text(encoding="utf-8")
    offenders = [key for key in BANNED_CONFIG_CLAIMS if key in content]
    assert not offenders, (
        "wheelhouse_help.md refers to a nonexistent api_key config slot: "
        f"{offenders}. The AI server credential is read only from the "
        "WHEELHOUSE_AI_API_KEY environment variable; there is no api_key "
        "line in config.toml."
    )


def test_help_doc_states_help_chat_disabled():
    content = _HELP_DOC.read_text(encoding="utf-8").lower()
    missing = [key for key in REQUIRED_FEATURE_STATE_NOTES if key not in content]
    assert not missing, (
        "wheelhouse_help.md must state that the in-app help chat is "
        f"currently disabled (missing: {missing}). The chat's voice "
        "patterns are commented out in the shipped catalog and "
        "tests/test_help_patterns_disabled.py pins that; presenting the "
        "chat as reachable sends users to a feature they cannot open."
    )


# How far (in lines) a "help chat" mention may sit from a "disabled" mention.
# One global disabled note is not enough: a regenerated doc could keep that
# note and still reintroduce active-chat claims elsewhere (codex review
# finding wh-llm-help-kit.2.6), so every mention must carry the state nearby.
_HELP_CHAT_DISABLED_WINDOW = 2

# Up to two intervening adverbs between the verb/negator and the status
# word: "isn't currently disabled", "is fully available" (codex review
# finding wh-llm-help-kit.2.11). "\w+ly" covers currently/fully/previously
# and similar; now/still/yet don't end in -ly.
_ADVERBS = r"(?:(?:\w+ly|now|still|yet)\s+){0,2}"

# A "disabled" preceded by a negator is not an affirmative disabled note:
# "the help chat is no longer disabled" must not satisfy the guard (codex
# review finding wh-llm-help-kit.2.8). Matching runs on lowercased text.
_NEGATED_DISABLED = re.compile(
    r"(?:no longer|not|never|isn't|is not|are not|aren't)\s+"
    + _ADVERBS
    + r"disabled"
)

# A direct claim that the chat is active. Catches "the help chat is
# enabled/available/..." even when an unrelated "disabled" sits nearby.
_ACTIVE_CHAT_CLAIM = re.compile(
    r"help chat\b[^.\n]*?\b(?:is|are|remains|becomes|stays)\s+"
    + _ADVERBS
    + r"(?:enabled|available|reachable|active|working|back)\b"
)


def _has_affirmative_disabled(text: str) -> bool:
    """True when the text contains a "disabled" that is not negated."""
    return "disabled" in _NEGATED_DISABLED.sub("", text)


def test_negated_disabled_forms_are_not_affirmative():
    """Adverb-carrying negations must not count as a disabled note
    (codex review finding wh-llm-help-kit.2.11)."""
    for phrase in (
        "the help chat is no longer disabled",
        "the help chat isn't currently disabled",
        "the help chat is not currently disabled",
        "the help chat was never fully disabled",
    ):
        assert not _has_affirmative_disabled(phrase), phrase


def test_active_claims_with_adverbs_are_caught():
    """Adverb-carrying active claims must match the active-claim pattern
    (codex review finding wh-llm-help-kit.2.11)."""
    for phrase in (
        "the help chat is enabled",
        "the help chat is currently enabled",
        "the help chat is fully available",
        "the help chat is now active",
        "the help chat stays reachable",
    ):
        assert _ACTIVE_CHAT_CLAIM.search(phrase), phrase


def test_real_doc_phrasings_stay_clean():
    """The wording the real doc uses must neither trip the active-claim
    pattern nor lose its affirmative disabled note."""
    for phrase in (
        "it also gates the in-app help chat, which is currently disabled "
        "in this release.",
        "this is the help surface of the current release -- the in-app "
        "help chat is currently disabled.",
    ):
        assert not _ACTIVE_CHAT_CLAIM.search(phrase), phrase
        assert _has_affirmative_disabled(phrase), phrase
    # The future-conditional mention makes no active claim; in the real doc
    # the adjacent line carries the disabled note.
    assert not _ACTIVE_CHAT_CLAIM.search(
        "if a future release re-enables the help chat, point this at a "
        "customized documentation file."
    )


def test_every_help_chat_mention_is_marked_disabled():
    lines = _HELP_DOC.read_text(encoding="utf-8").lower().splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if "help chat" not in line:
            continue
        if _ACTIVE_CHAT_CLAIM.search(line):
            offenders.append(
                f"line {i + 1} claims the chat is active: {line.strip()[:100]}"
            )
            continue
        lo = max(0, i - _HELP_CHAT_DISABLED_WINDOW)
        hi = i + _HELP_CHAT_DISABLED_WINDOW + 1
        # Only an affirmative "disabled" counts: strip negated forms first.
        if not any(
            _has_affirmative_disabled(nearby) for nearby in lines[lo:hi]
        ):
            offenders.append(f"line {i + 1}: {line.strip()[:100]}")
    assert not offenders, (
        "wheelhouse_help.md mentions the help chat without an affirmative "
        f"nearby statement that it is disabled: {offenders}. Every mention "
        "must carry the disabled state within "
        f"{_HELP_CHAT_DISABLED_WINDOW} lines -- a negated form ('no longer "
        "disabled') or a direct active claim ('the help chat is enabled') "
        "fails, so a regeneration cannot reintroduce active-chat claims "
        "while a single global disabled note keeps the other guard green."
    )
