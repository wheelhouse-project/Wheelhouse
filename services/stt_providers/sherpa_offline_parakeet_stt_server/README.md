# Sherpa Offline Parakeet STT Server

Local speech-to-text server using Sherpa-ONNX with NVIDIA Parakeet TDT model.

Uses `OfflineRecognizer` with VAD-triggered batch recognition.

## Setup

1. Extract the Parakeet model to `%LOCALAPPDATA%\WheelHouse\models\`
   (the WheelHouse installer does this for you), or point the provider at
   any directory via `%LOCALAPPDATA%\WheelHouse\stt_model_overrides.toml`
   (`[parakeet_tdt]` section, key `model_path`) or this service's
   `config.toml` `[model].model_path`.
2. Install dependencies (run from this service directory):
   ```bash
   uv sync
   ```

3. Run:
   ```bash
   uv run python main.py
   ```

## Configuration

Edit `config.toml`:
- `use_gpu = true` for CUDA acceleration
- `vad.threshold` to tune speech detection sensitivity

## Hint boosting

Sherpa can bias recognition toward a list of phrases. Plain-text phrases
need `modeling_unit='bpe'` and a sentencepiece vocabulary, which must sit
beside the model's ONNX files as `bpe.vocab`.

That vocabulary is NOT `tokens.txt`. Both files have two columns, so
`tokens.txt` passes a shape check, but its second column holds token ids
rather than log probabilities, and sherpa then tokenizes every phrase
wrongly while reporting nothing. The engine used to pass `tokens.txt`
here. The real file now ships at `model_assets/bpe.vocab` with a `.sha256`
sidecar beside it, and the WheelHouse installer copies it into the model
directory.

`bpe_vocab_rejection_reason` in `sherpa_engine.py` checks the file before
use. An unusable one drops every hint argument and records the reason in
`hotwords_status`, so boosting turns off instead of biasing on wrong
tokens, and the caller can tell the user the truth. The degrade has to be
all or nothing: `modeling_unit='bpe'` with no readable `bpe_vocab`
access-violates sherpa natively.

Where the file came from and how to regenerate it: `docs/vendoring/SECURITY.md`,
under "Model artifacts".

## WebSocket Protocol

Connects on `ws://localhost:8002`. Messages match google_stt_server format:

```json
{"type": "vad_start", "utterance_id": 1}
{"type": "final", "text": "hello world", "utterance_id": 1}
```

Note: This server uses offline (batch) recognition, so it only sends `final` results
after VAD detects end of speech.
