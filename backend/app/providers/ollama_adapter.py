"""Ollama provider adapter — local models via OpenAI-compatible API."""

from __future__ import annotations

import logging
import os

from . import register
from .openai_adapter import OpenAIAdapter

logger = logging.getLogger(__name__)


class OllamaAdapter(OpenAIAdapter):
    """Ollama uses an OpenAI-compatible endpoint at localhost:11434."""

    def __init__(self) -> None:
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        super().__init__(
            base_url=f"{host}/v1",
            api_key="ollama",  # Ollama doesn't need a real key
            default_model="qwen2.5-coder",
        )


register("ollama", OllamaAdapter)
