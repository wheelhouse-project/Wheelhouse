"""Shared fixtures for AI service tests.

Provides mock HTTP responses, provider factories, and MockProvider
for testing AI services without real network calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from ai.providers.base import AIProvider
from ai.providers.openai_compat import ChatResult, ChatStatus


@pytest.fixture
def mock_aiohttp_session():
    """Mock aiohttp.ClientSession with configurable responses."""
    session = AsyncMock()

    # Make session usable as async context manager (for `async with` creation)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    return session


class _HttpBlockedError(BaseException):
    """Sentinel raised by _block_real_http when un-mocked HTTP is attempted.

    Inherits from BaseException, not Exception, so provider try/except Exception
    blocks cannot swallow it silently.  Any un-mocked aiohttp.ClientSession or
    aiohttp.TCPConnector construction will propagate out of the provider method
    and fail the test loudly rather than being absorbed as a TRANSPORT_ERROR or
    empty list (finding wh-ay6h.21.1).
    """


@pytest.fixture(autouse=True)
def _block_real_http():
    """Fail fast on any un-mocked real network call in the AI test suite.

    Patches aiohttp.ClientSession and aiohttp.TCPConnector at module scope so
    that constructing a real HTTP session raises immediately. Tests that need
    HTTP behaviour mock it explicitly -- either via the mock_aiohttp_session
    fixture or by patching the provider's _get_session inside the test body.
    Those narrower per-test patches override this autouse patch for the
    duration of the test, so the existing tests keep working; only a path that
    forgot to mock the network trips this guard.

    Raises _HttpBlockedError (a BaseException subclass) rather than
    RuntimeError so that broad ``except Exception`` handlers in the provider
    cannot absorb the guard and convert it into a benign transport-error result
    (finding wh-ay6h.21.1).

    Yield so the patches are torn down after each test (function scope).
    """
    msg = ("Real HTTP is blocked in AI tests -- use mock_aiohttp_session or "
           "per-test patches")

    def _blocked(*_args, **_kwargs):
        raise _HttpBlockedError(msg)

    with patch.object(aiohttp, "ClientSession", side_effect=_blocked), \
         patch.object(aiohttp, "TCPConnector", side_effect=_blocked):
        yield


def make_mock_response(status=200, json_data=None, text="", raise_on_json=False,
                       json_exception=None):
    """Create a mock aiohttp response.

    Args:
        status: HTTP status code
        json_data: Data to return from .json()
        text: Data to return from .text()
        raise_on_json: If True, .json() raises an error (generic Exception by
            default; pass json_exception to specify the exact exception type).
        json_exception: Exception instance to raise from .json() when
            raise_on_json is True.  Defaults to Exception("Invalid JSON").
    """
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(return_value=text)

    if raise_on_json:
        exc = json_exception if json_exception is not None else Exception("Invalid JSON")
        response.json = AsyncMock(side_effect=exc)
    else:
        response.json = AsyncMock(return_value=json_data or {})

    # Support async context manager (for `async with session.post(...) as resp`)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    return response


class MockProvider:
    """Mock AI provider implementing the AIProvider protocol.

    Configurable chat response for testing AIService without real HTTP calls.
    """

    def __init__(self, chat_response=None, available: bool = True):
        if chat_response is None:
            chat_response = ChatResult(status=ChatStatus.OK, text="mock response")
        self._chat_response = chat_response
        self._available = available
        self.chat = AsyncMock(return_value=chat_response)
        self.is_available = AsyncMock(return_value=available)
        self.unload_model = AsyncMock()
        self.close = AsyncMock()

    # Verify protocol conformance
    assert isinstance(type("_Check", (), {
        "chat": AsyncMock(), "is_available": AsyncMock()
    })(), AIProvider)


@pytest.fixture
def mock_provider():
    """Create a MockProvider with default happy-path responses."""
    return MockProvider()


@pytest.fixture
def ai_config():
    """Mock ConfigService with AI config values for AIService tests.

    Carries the thin-client [ai.server] block that the repointed start()
    reads (design 5.2): base_url / model / api_key / timeout_s / kind +
    ai.server.enabled. The legacy ai.provider / ai.ollama.* / ai.llamacpp.* /
    ai.openai.* keys are vestigial -- the multi-provider factory and the
    per-model swap entry points were all removed in Phase C (commit 43533716)
    and no production code reads these keys any longer (finding wh-ay6h.10.7).
    They are kept here to avoid breaking any fixture consumers that assemble
    config dicts by merging with this baseline.
    """
    config = MagicMock()
    config.get = MagicMock(side_effect=lambda key, default=None: {
        "ai.enabled": True,
        # -- thin-client [ai.server] block (new schema start() reads) --
        "ai.server.base_url": "http://localhost:8781/v1",
        "ai.server.model": "qwen3.5:9b",
        "ai.server.api_key": "",
        "ai.server.timeout_s": 60,
        "ai.server.kind": "local",
        "ai.server.enabled": True,
        # -- legacy provider keys (vestigial; deleted in Phase C, commit 43533716) --
        "ai.provider": "ollama",
        "ai.knowledge_base": "knowledge/wheelhouse_help.md",
        "ai.ollama.host": "localhost:11434",
        "ai.ollama.model": "qwen3.5:9b",
        "ai.ollama.keep_alive": "30m",
        "ai.openai.api_key": "",
        "ai.openai.model": "gpt-4o-mini",
        "ai.openai.base_url": "https://api.openai.com/v1",
        "ai.llamacpp.model_path": "D:/Models/test-model.gguf",
        "ai.llamacpp.n_gpu_layers": -1,
        "ai.llamacpp.n_ctx": 16384,
        "ai.llamacpp.n_threads": 4,
        "ai.llamacpp.tier": 3,
        "ai.llamacpp.think": False,
        "ai.text_correction.enabled": True,
        "ai.help.enabled": True,
        "ai.help.speak_response": True,
        "ai.help.max_response_tokens": 800,
        "ai.help.conversation_timeout_minutes": 5,
        "ai.help.max_conversation_turns": 10,
        "ai.active_model": "",
        "ai.models_directory": "D:/Models",
    }.get(key, default))
    return config
