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

## WebSocket Protocol

Connects on `ws://localhost:8002`. Messages match google_stt_server format:

```json
{"type": "vad_start", "utterance_id": 1}
{"type": "final", "text": "hello world", "utterance_id": 1}
```

Note: This server uses offline (batch) recognition, so it only sends `final` results
after VAD detects end of speech. For streaming partial results, use a streaming 
Zipformer server instead.
