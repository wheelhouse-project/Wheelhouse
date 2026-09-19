"""Source guard for the shipped providers' WinRT-only capture rule.

Read source rather than importing provider entry points: this shared guard
needs neither their model assets nor Google credentials nor a microphone.
Runtime construction and startup refusal are covered by test_audio_capture_factory.
Pin the current explicit import convention; this is not Python dataflow analysis
or a proof against arbitrary alternative constructors or dynamic imports.
"""

import ast
from pathlib import Path

import pytest


PROVIDERS = Path(__file__).resolve().parents[2]
CAPTURE = PROVIDERS / "shared" / "shared_audio" / "capture"
SHIPPED_PROVIDERS = (
    "google_stt_server",
    "sherpa_offline_parakeet_stt_server",
    "distil_medium_en",
)


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _sounddevice_import_lines(tree):
    lines = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or "", *(alias.name for alias in node.names)]
        if any("sounddevice" in name.split(".") for name in names):
            lines.append(node.lineno)
    return lines


def _has_only_shared_factory_binding(tree):
    bindings = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                # A star import could replace the factory without naming it.
                if name in {"get_audio_provider", "*"}:
                    bindings.append(node)
        elif isinstance(node, ast.Name) and node.id == "get_audio_provider":
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bindings.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "get_audio_provider":
                bindings.append(node)
        elif isinstance(node, ast.arg) and node.arg == "get_audio_provider":
            bindings.append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name == "get_audio_provider":
            bindings.append(node)
    if len(bindings) != 1:
        return False
    binding = bindings[0]
    return (
        binding in tree.body
        and isinstance(binding, ast.ImportFrom)
        and binding.level == 0
        and binding.module == "shared_audio.capture"
        and any(alias.name == "get_audio_provider" for alias in binding.names)
    )


def test_every_shipped_provider_uses_winrt_only():
    provider_violations = []
    for provider in SHIPPED_PROVIDERS:
        tree = _tree(PROVIDERS / provider / "main.py")
        assert not _sounddevice_import_lines(tree), (
            f"{provider}/main.py must not import sounddevice"
        )
        assert _has_only_shared_factory_binding(tree), (
            f"{provider}/main.py must import get_audio_provider directly from "
            "shared_audio.capture without rebinding or shadowing it"
        )
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_audio_provider"
        ]
        # Explicit keywords prevent a positional backend or **options from
        # concealing selection. Check every call, including diagnostics.
        if not calls or any(
            call.args or any(
                keyword.arg not in {"config", "overflow_callback"}
                for keyword in call.keywords
            )
            for call in calls
        ):
            provider_violations.append(provider)
    assert not provider_violations, (
        "Every shipped provider must call get_audio_provider without backend "
        f"selection: {provider_violations}"
    )

    factory = _tree(CAPTURE / "factory.py")
    functions = [
        node for node in factory.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_audio_provider"
    ]
    returns = [
        node.value for function in functions for node in ast.walk(function)
        if isinstance(node, ast.Return)
    ]
    assert len(functions) == 1 and returns and all(
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "WinRTAudioCapture"
        for value in returns
    ), "The capture factory must construct and return WinRTAudioCapture only"

    sounddevice_imports = []
    sources = sorted(CAPTURE.rglob("*.py"))
    assert sources, "The capture source directory must not be empty"
    for source in sources:
        sounddevice_imports.extend(
            f"{source.name}:{line}" for line in _sounddevice_import_lines(_tree(source))
        )
    assert not sounddevice_imports, (
        "shared_audio/capture must not import sounddevice: "
        f"{sounddevice_imports}"
    )


@pytest.mark.parametrize("provider", SHIPPED_PROVIDERS)
@pytest.mark.parametrize(
    ("mutation", "assertion"),
    [
        ("import-sounddevice", "must not import sounddevice"),
        ("from-sounddevice", "must not import sounddevice"),
        ("wrapper-import", "must import get_audio_provider directly"),
        ("local-function", "must import get_audio_provider directly"),
        ("assignment", "must import get_audio_provider directly"),
        ("aliased-import", "must import get_audio_provider directly"),
        ("parameter", "must import get_audio_provider directly"),
        ("wildcard-import", "must import get_audio_provider directly"),
    ],
)
def test_provider_guard_rejects_alternative_capture_bindings(
    monkeypatch, provider, mutation, assertion
):
    """Exercise the real guard with changed source ASTs, never live providers."""
    path = PROVIDERS / provider / "main.py"
    source = path.read_text(encoding="utf-8")
    if mutation == "wrapper-import":
        original_import = "from shared_audio.capture import ("
        assert source.count(original_import) == 1
        source = source.replace(original_import, "from capture_wrapper import (")
    else:
        source += "\n" + {
            "import-sounddevice": "import sounddevice as sd\n",
            "from-sounddevice": "from sounddevice import InputStream\n",
            "local-function": "def get_audio_provider(**kwargs):\n    return None\n",
            "assignment": "get_audio_provider = lambda **kwargs: None\n",
            "aliased-import": "from capture_wrapper import open_capture as get_audio_provider\n",
            "parameter": (
                "def open_debug_capture(get_audio_provider):\n"
                "    return get_audio_provider(config=None)\n"
            ),
            "wildcard-import": "from capture_wrapper import *\n",
        }[mutation]
    mutated = ast.parse(source, filename=str(path))
    read_tree = _tree

    def read_mutated_tree(candidate):
        return mutated if candidate == path else read_tree(candidate)

    monkeypatch.setitem(globals(), "_tree", read_mutated_tree)
    with pytest.raises(AssertionError, match=assertion):
        test_every_shipped_provider_uses_winrt_only()
