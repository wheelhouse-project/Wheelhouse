"""Audio capture abstraction layer for STT services.

This module provides a unified interface for audio capture. There is one
backend, WinRT, and no fallback: a provider that cannot load winsdk
refuses to start and says why (wh-capture-winrt-required). The sentence
here used to promise "multiple backends (WinRT, sounddevice) with
automatic fallback", which the same branch made false.

GLOSSARY
--------
- **AudioProvider** - Protocol defining the common interface for audio capture
- **WinRT** - Windows Runtime API for native audio capture (Windows 10+)
- **sounddevice** - Cross-platform audio library using PortAudio. It was
  the second capture path until wh-portaudio-capture-removal deleted
  sounddevice_capture.py and microphone.py. The package itself stays in
  shared/pyproject.toml, because tools/stt_load_test/run.py plays audio
  through it; no provider captures through it any more.
- **PCM** - Pulse Code Modulation: raw uncompressed audio format

OVERVIEW
--------
The audio layer abstracts microphone capture so STT backends don't need to
know which audio library is being used. This enables:

1. **WinRT only** - Native Windows audio, and the one capture path every
   provider takes (wh-capture-winrt-required). A provider whose venv
   cannot load winsdk refuses to start rather than capturing another way.
2. **Consistent interface** - Every provider reads the same AudioProvider.

KEY INSIGHTS
------------
1. **One construction point** - Use the get_audio_provider() factory,
   don't instantiate directly. The factory raises with the approved
   refusal text when WinRT cannot load, and the caller turns that into a
   startup-failed notice.

2. **Format standardization** - All providers output 16kHz mono int16 PCM,
   matching STT service requirements.

3. **Queue-based streaming** - All providers use internal queues. The read()
   method blocks until audio is available or timeout.

4. **Statistics tracking** - All providers expose get_stats() for monitoring
   capture health (frames captured, drops, queue depth).

Example Usage
-------------
```python
from shared_audio.capture import get_audio_provider

# The factory returns the WinRT capture, or raises RuntimeError
provider = get_audio_provider()
provider.start()

while running:
    audio_bytes = provider.read(timeout=1.0)
    if audio_bytes:
        process_audio(audio_bytes)

provider.stop()
```
"""

from .base import AudioProvider, AudioConfig, AudioStats
from .winrt_capture import (
    AUDIO_DEVICE_MISSING_MESSAGE,
    AUDIO_DEVICE_MISSING_LOG_LINE,
)
from .factory import (
    get_audio_provider,
    WINRT_REQUIRED_MESSAGE,
    CAPTURE_BACKEND_NAME,
)

__all__ = [
    'AudioProvider',
    'AudioConfig',
    'AudioStats',
    'get_audio_provider',
    'WINRT_REQUIRED_MESSAGE',
    'AUDIO_DEVICE_MISSING_MESSAGE',
    'AUDIO_DEVICE_MISSING_LOG_LINE',
    'CAPTURE_BACKEND_NAME',
]
