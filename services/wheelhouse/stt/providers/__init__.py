"""
STT Provider implementations.

:flow: Each provider implements STTProvider ABC from base.py.

Note: Local providers (Vosk, Whisper) have been removed.
All STT is now handled by remote providers.
"""

from stt.providers.google_provider import GoogleSTTProvider
from stt.providers.azure_provider import AzureSTTProvider

__all__ = [
    "GoogleSTTProvider",
    "AzureSTTProvider",
]
