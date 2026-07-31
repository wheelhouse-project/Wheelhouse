"""GoogleDirectStreamer builds its client from the configured
service-account key file (wh-google-creds-file-picker).

When a credentials file path is given, the client comes from
SpeechClient.from_service_account_file(path). When none is given, the
client is built bare, which uses Application Default Credentials (the
GOOGLE_APPLICATION_CREDENTIALS environment variable) exactly as before.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import DebugConfig
from direct_streamer import GoogleDirectStreamer


def _make_streamer(**kwargs):
    return GoogleDirectStreamer(debug_cfg=DebugConfig(), **kwargs)


class TestCredentialsFileClientConstruction:
    def test_uses_service_account_file_when_configured(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            streamer = _make_streamer(credentials_file="C:/keys/sa.json")
            mock_cls.from_service_account_file.assert_called_once_with(
                "C:/keys/sa.json"
            )
            assert streamer._client is mock_cls.from_service_account_file.return_value

    def test_uses_default_credentials_when_not_configured(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            streamer = _make_streamer()
            mock_cls.assert_called_once_with()
            mock_cls.from_service_account_file.assert_not_called()
            assert streamer._client is mock_cls.return_value

    def test_empty_string_means_not_configured(self):
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            _make_streamer(credentials_file="")
            mock_cls.assert_called_once_with()
            mock_cls.from_service_account_file.assert_not_called()

    def test_explicit_client_wins(self):
        client = MagicMock()
        with patch("google.cloud.speech_v1.SpeechClient") as mock_cls:
            streamer = _make_streamer(
                client=client, credentials_file="C:/keys/sa.json"
            )
            assert streamer._client is client
            mock_cls.from_service_account_file.assert_not_called()
            mock_cls.assert_not_called()
