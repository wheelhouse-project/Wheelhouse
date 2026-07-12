"""Audio capture abstraction layer for STT services.

This module provides a unified interface for audio capture, supporting multiple
backends (WinRT, sounddevice) with automatic fallback.

GLOSSARY
--------
- **AudioProvider** - Protocol defining the common interface for audio capture
- **WinRT** - Windows Runtime API for native audio capture (Windows 10+)
- **sounddevice** - Cross-platform audio library using PortAudio
- **PCM** - Pulse Code Modulation: raw uncompressed audio format

OVERVIEW
--------
The audio layer abstracts microphone capture so STT backends don't need to
know which audio library is being used. This enables:

1. **WinRT primary** - Native Windows audio with better integration
2. **sounddevice fallback** - Proven cross-platform backup
3. **Consistent interface** - Same API regardless of backend

KEY INSIGHTS
------------
1. **Backend selection** - Use get_audio_provider() factory, don't instantiate
   directly. Factory handles availability detection and fallback.

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

# Factory handles WinRT vs sounddevice selection
provider = get_audio_provider(rate=16000, chunk_ms=30)
provider.start()

while running:
    audio_bytes = provider.read(timeout=1.0)
    if audio_bytes:
        process_audio(audio_bytes)

provider.stop()
```
"""

from .base import AudioProvider, AudioConfig, AudioStats
from .factory import get_audio_provider, get_available_providers

__all__ = [
    'AudioProvider',
    'AudioConfig',
    'AudioStats',
    'get_audio_provider',
    'get_available_providers',
]
