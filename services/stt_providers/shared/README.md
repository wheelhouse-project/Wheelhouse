# WheelHouse Shared Components

Shared library for WheelHouse STT providers containing:

- `stt/ws_forwarder.py` - WebSocket client for sending transcripts to WheelHouse
- `stt/launcher.py` - Process supervisor with crash recovery
- `audio/agc.py` - Smart Automatic Gain Control
- `audio/silero_vad.py` - Silero VAD wrapper
- `audio/lead_in_buffer.py` - Audio lead-in buffer for capturing pre-speech audio
