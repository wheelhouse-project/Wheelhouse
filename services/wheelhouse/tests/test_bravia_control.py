"""BraviaControl transport contracts; all TV traffic is mocked (wh-ybdyq.1).

Sony reports application errors inside an HTTP 200 body
({"error": [code, message], "id": N}), so HTTP success alone must not be
reported as brightness success.
"""
import logging
from unittest.mock import Mock

import pytest
import requests

import services.wheelhouse.integrations.bravia_control as mod

ERROR_ENVELOPE = {"error": [7, "Illegal State"], "id": 12}


def _response(body=None, json_error=None, http_error=None):
    response = Mock()
    if http_error is not None:
        response.raise_for_status.side_effect = http_error
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = body
    return response


@pytest.fixture
def bravia():
    return mod.BraviaControl(ip_address="192.0.2.20", psk="test-psk")


@pytest.fixture
def post(monkeypatch):
    post = Mock()
    monkeypatch.setattr(mod.requests, "post", post)
    return post


class TestSetBrightnessResponseEnvelope:
    async def test_error_envelope_in_http_200_returns_false(self, bravia, post):
        post.return_value = _response(ERROR_ENVELOPE)
        assert await bravia.set_brightness(50) is False

    async def test_error_envelope_logs_code_and_message_at_error(self, bravia, post, caplog):
        post.return_value = _response(ERROR_ENVELOPE)
        with caplog.at_level(logging.ERROR, logger=mod.logger.name):
            await bravia.set_brightness(50)
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        # The wording proves the envelope branch logged it; the no-result
        # branch would also log the body, code and message included.
        assert any("Error code 7: Illegal State" in m for m in errors), errors

    async def test_result_envelope_returns_true(self, bravia, post):
        post.return_value = _response({"result": [], "id": 12})
        assert await bravia.set_brightness(50) is True

    async def test_non_json_body_returns_false(self, bravia, post):
        post.return_value = _response(json_error=requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0))
        assert await bravia.set_brightness(50) is False

    async def test_non_json_body_plain_value_error_returns_false(self, bravia, post):
        post.return_value = _response(json_error=ValueError("not json"))
        assert await bravia.set_brightness(50) is False

    async def test_body_with_neither_key_returns_false(self, bravia, post):
        post.return_value = _response({"id": 12})
        assert await bravia.set_brightness(50) is False

    async def test_non_dict_json_body_returns_false(self, bravia, post):
        post.return_value = _response([])
        assert await bravia.set_brightness(50) is False

    @pytest.mark.parametrize("error", [requests.exceptions.ConnectionError("down"), requests.exceptions.Timeout("slow")])
    async def test_connection_error_and_timeout_return_none(self, bravia, post, error):
        post.side_effect = error
        assert await bravia.set_brightness(50) is None

    async def test_http_500_returns_false(self, bravia, post):
        post.return_value = _response(http_error=requests.exceptions.HTTPError("500 Server Error"))
        assert await bravia.set_brightness(50) is False


class TestGetBrightnessResponseEnvelope:
    async def test_error_envelope_reports_no_brightness(self, bravia, post):
        post.return_value = _response(ERROR_ENVELOPE)
        assert await bravia.get_brightness() is None

    async def test_result_envelope_reports_normalized_brightness(self, bravia, post):
        post.return_value = _response({"result": [[{"target": "brightness", "currentValue": "25"}]], "id": 13})
        assert await bravia.get_brightness() == 50
