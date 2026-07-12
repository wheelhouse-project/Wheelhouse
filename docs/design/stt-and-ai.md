# WheelHouse Speech-to-Text and AI

> **Status:** Current
> **Date:** 2026-07-11
> **Supersedes:** `archive/local-ai-and-stt.md` (the 2026-03-28 Vulkan-wheel
> architecture: vulkan_small whisper.cpp STT and in-process llama-cpp-python
> AI, both retired)

---

## 1. Overview

WheelHouse ships three STT providers and a thin-client AI subsystem. The
default experience is fully local: the Parakeet provider runs offline on CPU,
and the AI features talk to a local Ollama server if one is present (and
quietly disable themselves if not). No custom-built GPU wheels are required
at runtime by any shipped component.

| Subsystem | What runs | Where inference happens |
|-----------|-----------|------------------------|
| STT (default) | Parakeet TDT 0.6B v3 int8 via sherpa-onnx | Local, CPU |
| STT (opt-in) | Distil-Whisper distil-medium.en via faster-whisper | Local, NVIDIA CUDA |
| STT (opt-in) | Google Cloud Speech-to-Text | Google's servers |
| AI text fixing / help chat | Any OpenAI-compatible server (Ollama by default) | Wherever that server runs |

## 2. STT provider architecture

Providers are separate processes discovered by scanning
`services/stt_providers/` for a `config.toml` with a `[provider]` section.
`RemoteSTTLauncher` (in the logic process) starts the selected provider with
`uv run --locked --no-sync` from the provider's own directory, so each
provider has its own uv-managed venv that must be synced before first run.
Transcripts stream back to the logic process over WebSocket; a provider
crash cannot take the application down.

```
Microphone
    |
    v
Provider process (audio capture -> Silero VAD -> model inference)
    |
    v
WebSocket -> Logic process (SpeechProcessor -> command routing)
```

### 2.1 The three providers

- **`sherpa_offline_parakeet_stt_server`** (default): NVIDIA NeMo Parakeet
  TDT 0.6B v3, int8 ONNX export, run through the sherpa-onnx
  `OfflineRecognizer` on CPU. Best word-error rate of every local model we
  benchmarked. Fully offline; nothing leaves the machine.
- **`distil_medium_en`** (opt-in, NVIDIA GPUs): Distil-Whisper
  distil-medium.en through faster-whisper/CTranslate2. Requires CUDA plus
  system cuBLAS/cuDNN; auto-downloads its ~756 MB model from Hugging Face on
  first run. No CPU fallback -- it is an acceleration tier, not the default.
- **`google_stt_server`** (opt-in, cloud): Google Cloud Speech-to-Text
  streaming API. Requires a Google Cloud account with Application Default
  Credentials. Audio is sent to Google while dictating.

### 2.2 Model delivery and the override file (Parakeet)

The Parakeet model (~640 MB int8 archive) is downloaded at install time, not
committed to the repository. The provider resolves `[model].model_path` at
config load in this order (see `_resolve_model_path` in the provider's
`main.py`):

1. The per-machine override file
   `%LOCALAPPDATA%\WheelHouse\stt_model_overrides.toml`, section named after
   the provider (`[parakeet_tdt]`), key `model_path`. Written by the
   installer; can be created by hand.
2. The provider's own tracked `config.toml` value (shipped empty).
3. The coded default
   `%LOCALAPPDATA%\WheelHouse\models\sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`.

A malformed or unreadable override file never crashes the provider: it logs
a warning and the next value in the chain stands.

### 2.3 Hint biasing

`services/stt_providers/shared/hints.txt` is the shared contextual-biasing
vocabulary (one phrase per line, user-editable). Google STT consumes it as
adaptation phrase hints. The Parakeet provider regenerates
`runtime/parakeet-hotwords.txt` from it at startup and can pass it to
sherpa-onnx as hotwords -- off by default because hotword biasing forces
beam-search decoding, which measured ~25% extra inference latency.

## 3. AI text processing (thin client)

The AI subsystem (`ai/providers/openai_compat.py`) is a thin HTTP client for
any OpenAI-compatible chat endpoint. There is no in-process model loading
and no GPU dependency in WheelHouse itself.

```toml
# services/wheelhouse/config.toml
[ai]
enabled = true
knowledge_base = "knowledge/wheelhouse_help.md"

[ai.server]
base_url = "http://localhost:11434/v1"   # local Ollama by default
model = "gemma3:12b"
api_key = ""                              # only needed by hosted endpoints
```

If `base_url` is empty or the server is unreachable, the AI features
(dictation fix-up, help chat) disable themselves quietly and everything else
keeps working. Privacy note: whatever text the AI features operate on is
sent to the configured server -- local Ollama means it stays on the machine;
a hosted endpoint means it leaves.

## 4. Dev-only: the evaluation harness and the Vulkan whisper wheel

`services/stt_providers/evaluation/` is the benchmark harness used to
compare providers and models. One optional adapter uses whisper.cpp through
a locally-built pywhispercpp Vulkan wheel. That wheel is **deliberately not
a declared dependency** of any service: uv validates path-source metadata on
every `uv sync --locked` regardless of dependency-group selection, so a
path-source reference would break fresh clones that lack the wheel
(wh-797.2.1). Evaluation users install it manually into the `shared` venv
from `vendor/wheels/` (dev machines) or the hosted release asset (see
wh-kft).

To rebuild the wheel from source (Windows x64, Python 3.12, VS Build Tools,
CMake, Vulkan SDK):

```bash
# from a pywhispercpp checkout; clear build/ and _skbuild/ first --
# a stale CMake cache with GGML_CUDA=ON breaks Vulkan builds
pip wheel . \
    --config-settings="cmake.define.GGML_VULKAN=ON" \
    --no-build-isolation \
    --wheel-dir=services/stt_providers/shared/vendor/wheels/
```

Supply-chain posture, hash sidecars, and the re-vendoring procedure live in
`docs/vendoring/SECURITY.md`.

## 5. Privacy summary

- **Parakeet (default):** audio and transcripts never leave the machine.
- **Distil-Whisper:** inference is local; the model download itself comes
  from Hugging Face once.
- **Google STT:** audio streams to Google while dictating.
- **AI features:** operate through the configured server; local Ollama keeps
  text on-device.
- WheelHouse itself sends no telemetry. See `PRIVACY.md` (public release)
  for the full statement, including what the app can observe and modify on
  the machine and where logs live.
