"""Grep gate: the generated help doc must not document removed local-AI keys.

WheelHouse became a thin client (assignment wf-whai-b / epic wh-ay6h): the
local llama-cpp / Ollama model-hosting config was deleted and replaced by an
[ai.server] section that points at an external OpenAI-compatible server. This
test guards `services/wheelhouse/knowledge/wheelhouse_help.md` so the stale
local-model config keys can never silently re-enter the generated help doc.

Pure stdlib (pathlib only); no service imports, no fixtures.
"""
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
