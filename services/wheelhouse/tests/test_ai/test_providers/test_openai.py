"""Tests for OpenAIProvider (thin-client Phase A).

Tests HTTP communication with OpenAI-compatible /v1/chat/completions
endpoint plus the new list_models()/is_available() probes and the
structured ChatResult return value. All HTTP calls are mocked -- no real
API keys or endpoints needed.
"""

import os

import aiohttp
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from ai.providers.base import AIProvider
from ai.providers.openai_compat import (
    ChatResult,
    ChatStatus,
    OpenAIProvider,
)
from tests.test_ai.conftest import make_mock_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    """OpenAIProvider with default OpenAI settings."""
    return OpenAIProvider(
        api_key="sk-test-key-123",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
    )


@pytest.fixture
def gemini_provider():
    """OpenAIProvider configured for Gemini's OpenAI-compatible endpoint."""
    return OpenAIProvider(
        api_key="AIzaSy-test-key",
        model="gemini-2.0-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    )


@pytest.fixture
def groq_provider():
    """OpenAIProvider configured for Groq."""
    return OpenAIProvider(
        api_key="gsk_test-key",
        model="llama-3.1-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
    )


def _post_session(mock_resp):
    """An AsyncMock session whose .post returns mock_resp."""
    session = AsyncMock()
    session.post = MagicMock(return_value=mock_resp)
    return session


def _get_session(mock_resp):
    """An AsyncMock session whose .get returns mock_resp."""
    session = AsyncMock()
    session.get = MagicMock(return_value=mock_resp)
    return session


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:

    def test_openai_provider_satisfies_ai_provider(self):
        provider = OpenAIProvider(api_key="key", model="gpt-4o-mini")
        assert isinstance(provider, AIProvider)


# ---------------------------------------------------------------------------
# ChatResult dataclass
# ---------------------------------------------------------------------------

class TestChatResult:

    def test_fields(self):
        r = ChatResult(
            status=ChatStatus.OK,
            text="hi",
            finish_reason="stop",
            status_code=200,
        )
        assert r.ok is True
        assert r.text == "hi"
        assert r.finish_reason == "stop"
        assert r.status_code == 200

    def test_outcome_values(self):
        assert ChatResult(status=ChatStatus.OK).outcome == "ok"
        assert ChatResult(status=ChatStatus.EMPTY).outcome == "empty"
        assert ChatResult(status=ChatStatus.MODEL_NOT_FOUND).outcome == "model_not_found"
        assert ChatResult(status=ChatStatus.HTTP_ERROR).outcome == "http_error"
        assert ChatResult(status=ChatStatus.TRANSPORT_ERROR).outcome == "transport_error"

    def test_outcome_is_one_of_enum(self):
        valid = {s.value for s in ChatStatus}
        for status in ChatStatus:
            assert ChatResult(status=status).outcome in valid

    def test_truncated_property(self):
        assert ChatResult(status=ChatStatus.OK, finish_reason="length").truncated is True
        assert ChatResult(status=ChatStatus.OK, finish_reason="stop").truncated is False
        assert ChatResult(status=ChatStatus.OK).truncated is False

    def test_ok_property(self):
        assert ChatResult(status=ChatStatus.OK).ok is True
        assert ChatResult(status=ChatStatus.EMPTY).ok is False
        assert ChatResult(status=ChatStatus.HTTP_ERROR).ok is False

    def test_bool_raises_type_error(self):
        r = ChatResult(status=ChatStatus.OK, text="x")
        with pytest.raises(TypeError):
            bool(r)
        with pytest.raises(TypeError):
            if r:  # noqa: SIM103 - exercising the guard
                pass

    def test_bool_message_directs_to_ok(self):
        r = ChatResult(status=ChatStatus.EMPTY)
        with pytest.raises(TypeError, match=r"\.ok"):
            bool(r)


# ---------------------------------------------------------------------------
# Construction-time guards / warnings
# ---------------------------------------------------------------------------

class TestConstruction:

    def test_double_v1_raises(self):
        with pytest.raises(ValueError, match="v1/v1"):
            OpenAIProvider(
                api_key="k", model="m", base_url="http://localhost:11434/v1/v1"
            )

    def test_http_non_localhost_warns_once(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            OpenAIProvider(
                api_key="k", model="m", base_url="http://192.168.1.50:8000/v1"
            )
        assert "plain http" in caplog.text.lower()

    def test_http_localhost_does_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            OpenAIProvider(
                api_key="k", model="m", base_url="http://localhost:11434/v1"
            )
        assert "plain http" not in caplog.text.lower()

    def test_https_does_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            OpenAIProvider(
                api_key="k", model="m", base_url="https://api.openai.com/v1"
            )
        assert "plain http" not in caplog.text.lower()

    def test_http_substring_host_notlocalhost_warns(self, caplog):
        """http://notlocalhost.example is NOT local -- warning must fire (wh-ay6h.3.2).

        The old substring check ('localhost' in base_url) would have treated this
        as local and suppressed the warning.
        """
        import logging
        with caplog.at_level(logging.WARNING):
            OpenAIProvider(
                api_key="k", model="m",
                base_url="http://notlocalhost.example/v1",
            )
        assert "plain http" in caplog.text.lower()

    def test_http_substring_host_127_0_0_1_example_warns(self, caplog):
        """http://127.0.0.1.example is NOT local -- warning must fire (wh-ay6h.3.2).

        The old substring check ('127.0.0.1' in base_url) would have treated this
        as local and suppressed the warning.
        """
        import logging
        with caplog.at_level(logging.WARNING):
            OpenAIProvider(
                api_key="k", model="m",
                base_url="http://127.0.0.1.example/v1",
            )
        assert "plain http" in caplog.text.lower()

    def test_timeout_s_default(self):
        p = OpenAIProvider(api_key="k", model="m")
        assert p._timeout_s == 60

    def test_timeout_s_custom(self):
        p = OpenAIProvider(api_key="k", model="m", timeout_s=12)
        assert p._timeout_s == 12


# ---------------------------------------------------------------------------
# chat() tests
# ---------------------------------------------------------------------------

class TestChat:

    @pytest.mark.asyncio
    async def test_chat_returns_chat_result_ok(self, provider):
        """Successful chat returns an OK ChatResult with the message content."""
        mock_resp = make_mock_response(
            status=200,
            json_data={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello world"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat(
                [{"role": "user", "content": "Hi"}], max_tokens=100
            )

        assert isinstance(result, ChatResult)
        assert result.ok is True
        assert result.text == "Hello world"
        assert result.finish_reason == "stop"
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_chat_sends_correct_headers(self, provider):
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            await provider.chat([{"role": "user", "content": "Hi"}])

        headers = session.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test-key-123"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_chat_sends_correct_body(self, provider):
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            messages = [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ]
            await provider.chat(messages, max_tokens=200)

        body = session.post.call_args.kwargs["json"]
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == messages
        assert body["max_tokens"] == 200

    @pytest.mark.asyncio
    async def test_chat_no_reasoning_effort_cloud(self, provider):
        """reasoning_effort is absent from cloud requests (dropped entirely)."""
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            await provider.chat([{"role": "user", "content": "test"}])
        body = session.post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body

    @pytest.mark.asyncio
    async def test_chat_no_reasoning_effort_local(self):
        """reasoning_effort is absent from local requests too."""
        local = OpenAIProvider(
            api_key="not-needed", model="test-model",
            base_url="http://localhost:8781/v1",
        )
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "fixed"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(local, "_get_session", return_value=session):
            await local.chat([{"role": "user", "content": "test"}])
        body = session.post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body

    @pytest.mark.asyncio
    async def test_chat_no_reasoning_effort_lan(self):
        """A LAN (192.168.x) endpoint also gets no reasoning_effort."""
        lan = OpenAIProvider(
            api_key="k", model="m", base_url="http://192.168.1.50:8000/v1",
        )
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(lan, "_get_session", return_value=session):
            await lan.chat([{"role": "user", "content": "test"}])
        body = session.post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body

    @pytest.mark.asyncio
    async def test_chat_timeout_driven_by_timeout_s(self):
        """The chat request timeout is built from the timeout_s parameter."""
        p = OpenAIProvider(api_key="k", model="m", timeout_s=17)
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(p, "_get_session", return_value=session):
            await p.chat([{"role": "user", "content": "Hi"}])
        timeout = session.post.call_args.kwargs["timeout"]
        assert timeout.total == 17

    @pytest.mark.asyncio
    async def test_chat_empty_choices_returns_empty(self, provider):
        mock_resp = make_mock_response(status=200, json_data={"choices": []})
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.EMPTY
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_chat_empty_content_returns_empty(self, provider):
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.EMPTY

    @pytest.mark.asyncio
    async def test_chat_404_returns_model_not_found(self, provider):
        mock_resp = make_mock_response(
            status=404,
            json_data={"error": {"message": "The model 'gpt-x' does not exist"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.MODEL_NOT_FOUND
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_chat_400_model_not_found_body(self, provider):
        mock_resp = make_mock_response(
            status=400,
            json_data={"error": {"message": "model not found: foo"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.MODEL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_chat_404_non_model_body_returns_http_error(self, provider):
        """A 404 whose body does not mention a missing model (e.g. wrong route,
        nginx proxy) must return HTTP_ERROR, not MODEL_NOT_FOUND."""
        mock_resp = make_mock_response(
            status=404,
            json_data={"error": {"message": "404 Not Found"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_chat_400_missing_param_model_returns_http_error(self, provider):
        """A 400 whose body says 'model' parameter not found in request
        (malformed request, not a missing model) must return HTTP_ERROR."""
        mock_resp = make_mock_response(
            status=400,
            json_data={"error": {"message": "Required parameter 'model' not found in request"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_chat_404_ollama_native_split_phrase_returns_model_not_found(self, provider):
        """Ollama's OpenAI-compat endpoint embeds the model name between 'model'
        and 'not found' (e.g. 'model \"qwen3.5:9b\" not found, try pulling it
        first').  The contiguous substring 'model not found' is absent, so the
        old pattern missed it and returned HTTP_ERROR.  wh-ay6h.6.6."""
        mock_resp = make_mock_response(
            status=404,
            json_data={"error": {"message": 'model "qwen3.5:9b" not found, try pulling it first', "type": "api_error"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.MODEL_NOT_FOUND
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_chat_500_returns_http_error(self, provider):
        mock_resp = make_mock_response(
            status=500, json_data={"error": {"message": "Internal server error"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 500

    @pytest.mark.asyncio
    async def test_chat_non_json_500_returns_http_error_not_transport(self, provider):
        """A 500 with a non-JSON (HTML) body must return HTTP_ERROR with the real
        status code, not TRANSPORT_ERROR (wh-ay6h.3.1).

        Previously resp.json() was called before checking resp.status; a
        ContentTypeError on the HTML body fell through to the ClientError handler
        and was reported as TRANSPORT_ERROR, hiding the real HTTP 500.
        """
        mock_resp = make_mock_response(
            status=500,
            text="<html>Internal Server Error</html>",
            raise_on_json=True,
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 500

    @pytest.mark.asyncio
    async def test_chat_non_json_404_returns_http_error_not_transport(self, provider):
        """A 404 with a plain-text body must return HTTP_ERROR, not TRANSPORT_ERROR
        (wh-ay6h.3.1).  Proxy error pages and nginx 404 pages are the common case.
        """
        mock_resp = make_mock_response(
            status=404,
            text="404 Not Found",
            raise_on_json=True,
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_chat_500_model_phrase_body_returns_http_error(self, provider):
        """A 500 whose body contains a model-not-found phrase must NOT be
        classified as MODEL_NOT_FOUND -- status gating means only 400/404 can
        trigger that classification (wh-ay6h.2.3)."""
        mock_resp = make_mock_response(
            status=500, json_data={"error": {"message": "model not found: foo"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 500

    @pytest.mark.asyncio
    async def test_chat_401_returns_http_error(self, provider):
        mock_resp = make_mock_response(
            status=401, json_data={"error": {"message": "Invalid API key"}},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.HTTP_ERROR
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_chat_connection_error_returns_transport_error(self, provider):
        session = AsyncMock()
        session.post = MagicMock(
            side_effect=aiohttp.ClientConnectionError("Connection refused")
        )
        with patch.object(provider, "_get_session", return_value=session):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.TRANSPORT_ERROR

    @pytest.mark.asyncio
    async def test_chat_timeout_returns_transport_error(self, provider):
        session = AsyncMock()
        session.post = MagicMock(side_effect=TimeoutError("Timed out"))
        with patch.object(provider, "_get_session", return_value=session):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.TRANSPORT_ERROR

    @pytest.mark.asyncio
    async def test_chat_length_finish_reason_is_truncated(self, provider):
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "cut off"}, "finish_reason": "length"}]},
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.ok is True
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_chat_200_non_json_body_returns_empty_not_transport(self, provider):
        """A 200 response with a non-JSON body (proxy error page, HTML login wall)
        must return ChatStatus.EMPTY with the real status code, NOT TRANSPORT_ERROR.

        Before the fix (wh-ay6h.4.1) resp.json() on the 200 path had no try/except
        and no content_type=None; aiohttp.ContentTypeError (subclass of ClientError)
        propagated to the outer except aiohttp.ClientError handler and was reported
        as TRANSPORT_ERROR with no status_code, masking the real response code.

        Uses a real aiohttp.ContentTypeError (a ClientError subclass) to exercise
        the exact exception path from the production scenario (wh-ay6h.4.5).
        """
        mock_resp = make_mock_response(
            status=200,
            text="<html>Login required</html>",
            raise_on_json=True,
            json_exception=aiohttp.ContentTypeError(None, ()),
        )
        with patch.object(provider, "_get_session", return_value=_post_session(mock_resp)):
            result = await provider.chat([{"role": "user", "content": "Hi"}])
        assert result.status is ChatStatus.EMPTY
        assert result.status_code == 200


# ---------------------------------------------------------------------------
# list_models() tests
# ---------------------------------------------------------------------------

class TestListModels:

    @pytest.mark.asyncio
    async def test_list_models_happy_path(self, provider):
        """GET <base_url>/models parses OpenAI-style {data:[{id:...}]}."""
        mock_resp = make_mock_response(
            status=200,
            json_data={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]},
        )
        session = _get_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            models = await provider.list_models()
        assert models == ["gpt-4o-mini", "gpt-4o"]
        url = session.get.call_args.args[0]
        assert url == "https://api.openai.com/v1/models"

    @pytest.mark.asyncio
    async def test_list_models_url_no_double_v1(self):
        """A /v1-terminated base composes exactly <base>/models (no double /v1)."""
        p = OpenAIProvider(api_key="k", model="m", base_url="http://host:11434/v1")
        mock_resp = make_mock_response(status=200, json_data={"data": []})
        session = _get_session(mock_resp)
        with patch.object(p, "_get_session", return_value=session):
            await p.list_models()
        url = session.get.call_args.args[0]
        assert url == "http://host:11434/v1/models"
        assert "/v1/v1" not in url

    @pytest.mark.asyncio
    async def test_list_models_api_tags_fallback_when_v1_stripped(self):
        """When /v1 base fails at /models, fall back to <host>/api/tags."""
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434/v1")
        models_resp = make_mock_response(status=404, json_data={})
        tags_resp = make_mock_response(
            status=200,
            json_data={"models": [{"name": "qwen3.5:9b"}, {"name": "llama3:8b"}]},
        )
        session = AsyncMock()
        session.get = MagicMock(side_effect=[models_resp, tags_resp])
        with patch.object(p, "_get_session", return_value=session):
            models = await p.list_models()
        assert models == ["qwen3.5:9b", "llama3:8b"]
        # Second call is the host-root /api/tags (no /v1).
        second_url = session.get.call_args_list[1].args[0]
        assert second_url == "http://localhost:11434/api/tags"

    @pytest.mark.asyncio
    async def test_list_models_no_fallback_when_no_v1(self):
        """A non-/v1 base does NOT fall back to /api/tags (finding 1.5 guard)."""
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434")
        models_resp = make_mock_response(status=404, json_data={})
        session = AsyncMock()
        session.get = MagicMock(return_value=models_resp)
        with patch.object(p, "_get_session", return_value=session):
            models = await p.list_models()
        assert models == []
        # Exactly one GET -- no /api/tags fallback.
        assert session.get.call_count == 1
        assert session.get.call_args_list[0].args[0] == "http://localhost:11434/models"

    @pytest.mark.asyncio
    async def test_list_models_non_v1_warns_once(self, caplog):
        import logging
        p = OpenAIProvider(api_key="", model="m", base_url="http://host:9000")
        mock_resp = make_mock_response(status=404, json_data={})
        session = AsyncMock()
        session.get = MagicMock(return_value=mock_resp)
        with caplog.at_level(logging.WARNING):
            with patch.object(p, "_get_session", return_value=session):
                await p.list_models()
                await p.list_models()
        # One-time warning: fires at most once.
        assert caplog.text.lower().count("does not end in '/v1'".lower()) == 1

    @pytest.mark.asyncio
    async def test_list_models_cloud_non_v1_does_not_warn(self, caplog):
        """A configured cloud provider (is_cloud=True) legitimately uses a
        non-/v1 path: Google's Gemini OpenAI-compatible endpoint is
        /v1beta/openai/. The 'does not end in /v1' warning -- whose only purpose
        is the LOCAL Ollama /api/tags fallback -- must NOT fire for it, or every
        cloud-AI startup logs a misleading warning the user cannot act on
        (deepseek round 2, finding 1.4)."""
        import logging
        p = OpenAIProvider(
            api_key="",
            model="m",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            is_cloud=True,
        )
        mock_resp = make_mock_response(status=404, json_data={})
        session = AsyncMock()
        session.get = MagicMock(return_value=mock_resp)
        with caplog.at_level(logging.WARNING):
            with patch.object(p, "_get_session", return_value=session):
                await p.list_models()
                await p.list_models()
        assert "does not end in '/v1'".lower() not in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_list_models_failure_returns_empty(self, provider):
        session = AsyncMock()
        session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("down"))
        with patch.object(provider, "_get_session", return_value=session):
            models = await provider.list_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_list_models_double_failure_both_non_200_returns_empty(self):
        """Primary /models non-200 AND fallback /api/tags non-200 must return [].

        Exercises the double-failure path (wh-ay6h.4.3) where both probes fail:
        the primary /models 404 triggers the /api/tags fallback which also 404s.
        Both return [] and the final result is [] (not a raised exception).
        """
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434/v1")
        models_resp = make_mock_response(status=404, json_data={})
        tags_resp = make_mock_response(status=404, json_data={})
        session = AsyncMock()
        session.get = MagicMock(side_effect=[models_resp, tags_resp])
        with patch.object(p, "_get_session", return_value=session):
            models = await p.list_models()
        assert models == []
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_list_models_double_failure_both_transport_returns_empty(self):
        """Primary /models transport error AND fallback /api/tags transport error returns [].

        Both probes raise aiohttp.ClientConnectionError; neither raises to the
        caller -- list_models() swallows both and returns [] (wh-ay6h.4.3).
        """
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434/v1")
        session = AsyncMock()
        session.get = MagicMock(
            side_effect=aiohttp.ClientConnectionError("server is down")
        )
        with patch.object(p, "_get_session", return_value=session):
            models = await p.list_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_list_models_primary_non_json_200_falls_through_to_working_fallback(self):
        """Primary /models returns 200 with non-JSON body (e.g. proxy HTML login page).

        The inner try/except catches the parse failure (wh-ay6h.4.8): execution
        falls through to the /api/tags fallback which returns valid JSON, and
        list_models() returns the parsed model names.
        """
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434/v1")
        primary_resp = make_mock_response(
            status=200,
            text="<html>Captive Portal Login</html>",
            raise_on_json=True,
        )
        fallback_resp = make_mock_response(
            status=200,
            json_data={"models": [{"name": "llama3:8b"}, {"name": "qwen3.5:9b"}]},
        )
        session = AsyncMock()
        session.get = MagicMock(side_effect=[primary_resp, fallback_resp])
        with patch.object(p, "_get_session", return_value=session):
            models = await p.list_models()
        assert models == ["llama3:8b", "qwen3.5:9b"]
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_list_models_double_non_json_200_returns_empty(self):
        """Both primary /models and fallback /api/tags return 200 with non-JSON bodies.

        Both inner try/except blocks catch the parse failures (wh-ay6h.4.9).
        Neither outer except clause is entered (no transport exception propagates).
        The function reaches the final return [] at the bottom of list_models().
        """
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434/v1")
        primary_resp = make_mock_response(
            status=200,
            text="<html>Proxy Error</html>",
            raise_on_json=True,
        )
        fallback_resp = make_mock_response(
            status=200,
            text="<html>Proxy Error</html>",
            raise_on_json=True,
        )
        session = AsyncMock()
        session.get = MagicMock(side_effect=[primary_resp, fallback_resp])
        with patch.object(p, "_get_session", return_value=session):
            models = await p.list_models()
        assert models == []
        assert session.get.call_count == 2


# ---------------------------------------------------------------------------
# is_available() tests (real HTTP probe)
# ---------------------------------------------------------------------------

class TestIsAvailable:

    @pytest.mark.asyncio
    async def test_is_available_true_on_200(self, provider):
        mock_resp = make_mock_response(status=200, json_data={"data": []})
        session = _get_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            assert await provider.is_available() is True
        url = session.get.call_args.args[0]
        assert url == "https://api.openai.com/v1/models"

    @pytest.mark.asyncio
    async def test_is_available_false_on_500(self, provider):
        mock_resp = make_mock_response(status=500, json_data={})
        session = _get_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            assert await provider.is_available() is False

    @pytest.mark.asyncio
    async def test_is_available_false_on_transport_error(self, provider):
        session = AsyncMock()
        session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("down"))
        with patch.object(provider, "_get_session", return_value=session):
            assert await provider.is_available() is False

    @pytest.mark.asyncio
    async def test_is_available_fallback_path_when_v1(self):
        """/v1 base falling 404 at /models is available iff /api/tags is 200."""
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434/v1")
        models_resp = make_mock_response(status=404, json_data={})
        tags_resp = make_mock_response(status=200, json_data={"models": []})
        session = AsyncMock()
        session.get = MagicMock(side_effect=[models_resp, tags_resp])
        with patch.object(p, "_get_session", return_value=session):
            assert await p.is_available() is True
        second_url = session.get.call_args_list[1].args[0]
        assert second_url == "http://localhost:11434/api/tags"

    @pytest.mark.asyncio
    async def test_is_available_no_fallback_when_no_v1(self):
        """Non-/v1 base: a 404 at /models is unavailable, no /api/tags probe."""
        p = OpenAIProvider(api_key="", model="m", base_url="http://localhost:11434")
        models_resp = make_mock_response(status=404, json_data={})
        session = AsyncMock()
        session.get = MagicMock(return_value=models_resp)
        with patch.object(p, "_get_session", return_value=session):
            assert await p.is_available() is False
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_is_available_uses_short_probe_timeout(self, provider):
        mock_resp = make_mock_response(status=200, json_data={"data": []})
        session = _get_session(mock_resp)
        with patch.object(provider, "_get_session", return_value=session):
            await provider.is_available()
        timeout = session.get.call_args.kwargs["timeout"]
        assert timeout.total == 5


# ---------------------------------------------------------------------------
# api_key env fallback (construction)
# ---------------------------------------------------------------------------

class TestApiKey:

    def test_explicit_key_takes_priority_over_env(self):
        with patch.dict(os.environ, {"WHEELHOUSE_AI_API_KEY": "sk-from-env"}):
            provider = OpenAIProvider(api_key="sk-explicit", model="gpt-4o-mini")
        assert provider._api_key == "sk-explicit"

    def test_key_from_env_var(self):
        with patch.dict(os.environ, {"WHEELHOUSE_AI_API_KEY": "sk-from-env"}):
            provider = OpenAIProvider(api_key="", model="gpt-4o-mini")
        assert provider._api_key == "sk-from-env"


# ---------------------------------------------------------------------------
# Custom base_url tests
# ---------------------------------------------------------------------------

class TestCustomBaseUrl:

    @pytest.mark.asyncio
    async def test_custom_base_url_gemini(self, gemini_provider):
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "Gemini response"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(gemini_provider, "_get_session", return_value=session):
            result = await gemini_provider.chat([{"role": "user", "content": "Hi"}])
        url = session.post.call_args.args[0] if session.post.call_args.args else session.post.call_args.kwargs.get("url")
        assert "generativelanguage.googleapis.com" in url
        assert url.endswith("/chat/completions")
        assert result.text == "Gemini response"

    @pytest.mark.asyncio
    async def test_custom_base_url_groq(self, groq_provider):
        mock_resp = make_mock_response(
            status=200,
            json_data={"choices": [{"message": {"content": "Groq response"}, "finish_reason": "stop"}]},
        )
        session = _post_session(mock_resp)
        with patch.object(groq_provider, "_get_session", return_value=session):
            result = await groq_provider.chat([{"role": "user", "content": "Hi"}])
        url = session.post.call_args.args[0] if session.post.call_args.args else session.post.call_args.kwargs.get("url")
        assert "api.groq.com" in url
        assert url.endswith("/chat/completions")
        assert result.text == "Groq response"


# ---------------------------------------------------------------------------
# close() tests
# ---------------------------------------------------------------------------

class TestClose:

    @pytest.mark.asyncio
    async def test_close_session(self, provider):
        mock_session = AsyncMock()
        provider._session = mock_session
        await provider.close()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_no_session(self, provider):
        assert provider._session is None
        await provider.close()  # Should not raise
