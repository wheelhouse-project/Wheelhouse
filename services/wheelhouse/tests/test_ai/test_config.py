"""Tests for AI config section in config.toml.

Phase D1: the legacy multi-provider [ai] shape (provider/active_model/
models_directory and the [ai.llamacpp]/[ai.ollama]/[ai.openai]/[ai.local]
blocks, plus several no-reader help knobs) has been removed. These tests
assert those keys are ABSENT and the thin-client [ai.server] shape is present.
"""

import tomllib
from pathlib import Path

import pytest


class TestAIConfigParsing:
    """Verify the real config.toml parses with the new-only [ai] shape."""

    @pytest.fixture
    def config_data(self):
        config_path = Path(__file__).parent.parent.parent / "config.toml"
        with open(config_path, "rb") as f:
            return tomllib.load(f)

    def test_ai_section_exists(self, config_data):
        assert "ai" in config_data

    def test_ai_enabled(self, config_data):
        assert config_data["ai"]["enabled"] is True

    def test_ai_knowledge_base_path(self, config_data):
        assert config_data["ai"]["knowledge_base"] == "knowledge/wheelhouse_help.md"

    # --- absence assertions: dead legacy [ai] header keys ---

    def test_ai_provider_removed(self, config_data):
        assert "provider" not in config_data["ai"]

    def test_ai_legacy_model_active_key_removed(self, config_data):
        assert "active_model" not in config_data["ai"]

    def test_ai_models_directory_removed(self, config_data):
        assert "models_directory" not in config_data["ai"]

    # --- absence assertions: dead legacy provider subsections ---

    def test_ai_llamacpp_section_removed(self, config_data):
        assert "llamacpp" not in config_data["ai"]

    def test_ai_ollama_section_removed(self, config_data):
        assert "ollama" not in config_data["ai"]

    def test_ai_openai_section_removed(self, config_data):
        assert "openai" not in config_data["ai"]

    def test_ai_local_section_removed(self, config_data):
        assert "local" not in config_data["ai"]

    # --- absence assertions: removed text_correction / help knobs ---

    def test_ai_text_correction_enabled_removed(self, config_data):
        # the whole [ai.text_correction] section was deleted with its only key
        text_correction = config_data["ai"].get("text_correction", {})
        assert "enabled" not in text_correction

    def test_ai_help_enabled_removed(self, config_data):
        assert "enabled" not in config_data["ai"]["help"]

    def test_ai_help_speak_response_removed(self, config_data):
        assert "speak_response" not in config_data["ai"]["help"]

    def test_ai_help_conversation_timeout_minutes_removed(self, config_data):
        assert "conversation_timeout_minutes" not in config_data["ai"]["help"]

    def test_ai_help_max_conversation_turns_removed(self, config_data):
        assert "max_conversation_turns" not in config_data["ai"]["help"]

    # --- new-shape presence assertions: [ai.server] thin client ---

    def test_ai_server_section(self, config_data):
        server = config_data["ai"]["server"]
        assert "base_url" in server
        assert "model" in server
        assert "api_key" in server
        assert "timeout_s" in server
        assert "kind" in server

    # --- retained help keys ---

    def test_ai_help_max_response_tokens(self, config_data):
        assert config_data["ai"]["help"]["max_response_tokens"] == 800

    def test_ai_help_gem_url(self, config_data):
        assert config_data["ai"]["help"]["gem_url"] == ""


class TestAIConfigViaMockService:
    """Verify dotted-key access works (mirrors how AIService reads config)."""

    def test_get_server_base_url(self, mock_config):
        mock_config._config["ai"] = {"server": {"base_url": "http://localhost:11434/v1"}}
        assert mock_config.get("ai.server.base_url") == "http://localhost:11434/v1"

    def test_get_server_kind(self, mock_config):
        mock_config._config["ai"] = {"server": {"kind": "local"}}
        assert mock_config.get("ai.server.kind") == "local"

    def test_get_default_on_missing(self, mock_config):
        mock_config._config["ai"] = {}
        assert mock_config.get("ai.server.base_url", "default") == "default"
