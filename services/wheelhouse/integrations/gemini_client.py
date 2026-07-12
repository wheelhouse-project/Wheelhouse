"""Google Gemini AI integration for text processing and correction.

This module provides a threadsafe interface to Google's Gemini AI API for
intelligent text processing, primarily used for correcting and enhancing
speech recognition results. It implements lazy loading and thread-safe
initialization patterns to ensure reliable operation in the multi-threaded
WheelHouse environment.

Key Classes:
  - GeminiClient: Main interface for Gemini AI API interactions.

Key Features:
  - Thread-safe lazy initialization of the Gemini model client
  - Configurable model selection and system prompts
  - API key validation and error handling
  - Text correction and enhancement capabilities
  - Integration with speech processing pipeline

Configuration Requirements:
  - GEMINI_API_KEY: Valid Google AI API key
  - GEMINI_MODEL_NAME: Model identifier (default: gemini-1.5-flash-latest)  
  - GEMINI_PROMPT: System prompt for text processing context

Thread Safety:
  - Uses threading.Lock for thread-safe model initialization
  - Lazy loading prevents unnecessary API calls during startup
  - Safe for use across multiple async tasks

Typical Usage:
  from integrations.gemini_client import GeminiClient
  
  gemini = GeminiClient()
  
  # Process text (automatically initializes on first use)
  corrected_text = await gemini.process_text(
      "the qwick brown fox jumps"
  )
  # Returns: "The quick brown fox jumps"
"""
import logging
import asyncio
import os
import google.generativeai as genai
from config import CONFIG
import threading

logger = logging.getLogger(__name__)

class GeminiClient:
    """
    Handles all interactions with the Google Gemini API for text correction.
    This class now uses thread-safe, lazy loading for the Gemini model client.
    """
    def __init__(self):
        self.api_key = CONFIG.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
        self.model_name = CONFIG.get("GEMINI_MODEL_NAME", "gemini-1.5-flash-latest")
        self.system_prompt = CONFIG.get("GEMINI_PROMPT")
        
        # The model is no longer initialized in the constructor.
        self.model = None
        self._lock = threading.Lock()

    def _get_model(self):
        """
        Provides a thread-safe, lazy-loaded instance of the Gemini model client.
        """
        if self.model is None:
            with self._lock:
                if self.model is None:
                    if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
                        logger.error(
                            "Cannot initialize GeminiClient: API key is not configured. "
                            "Set GEMINI_API_KEY environment variable or in config.toml"
                        )
                        return None
                    
                    if not self.system_prompt:
                        logger.error("Cannot initialize GeminiClient: System prompt is not configured.")
                        return None

                    try:
                        logger.info(f"Initializing GeminiClient (lazy, thread-safe) with model '{self.model_name}'...")
                        genai.configure(api_key=self.api_key)
                        self.model = genai.GenerativeModel(
                            model_name=self.model_name,
                            system_instruction=self.system_prompt
                        )
                        logger.info("GeminiClient initialized successfully.")
                    except Exception as e:
                        logger.error(f"Failed to initialize GeminiClient: {e}", exc_info=True)
                        # Ensure self.model remains None on failure
                        self.model = None
        return self.model

    async def fix_text(self, text: str) -> str:
        """
        Sends text to the Gemini API for correction based on the configured prompt.

        :flow: Contextual Text Fix
        :step: 2
        :description: Processes text through Gemini AI for grammar/context correction
        :data_in: Raw text string
        :data_out: Corrected text string
        :notes: Uses lazy-loaded Gemini model. Handles API errors gracefully.

        Args:
            text: The text to be corrected.

        Returns:
            The corrected text, or the original text if an error occurs.
        """
        model = self._get_model()
        if not model:
            logger.error("Cannot fix text; GeminiClient is not initialized or failed to initialize.")
            return text
        
        if not text.strip():
            logger.debug("fix_text called with empty string, returning.")
            return text

        try:
            logger.info("Sending text to Gemini for correction...")
            # Use the local 'model' variable obtained in a thread-safe way.
            response = await model.generate_content_async(text)
            corrected_text = response.text
            logger.info("Successfully received corrected text from Gemini.")
            return corrected_text
        except Exception as e:
            logger.error(f"Error during Gemini API call: {e}", exc_info=True)
            return text # Return original text on failure
