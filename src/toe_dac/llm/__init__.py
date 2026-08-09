"""Standalone LLM client module migrated from Andybot.

This module provides a unified interface for interacting with various LLM providers
(Anthropic and OpenAI compatible APIs).
"""

from .llm_wrapper import LLMClient
from .anthropic_client import AnthropicClient
from .openai_client import OpenAIClient
from .base import LLMClientBase

# Re-export node models for convenience
from .node.node import (
    LLMProvider,
    Message,
    MessageRole,
    Tool,
    ToolFunction,
    ToolType,
    ToolCall,
    LLMResponse,
    ModelConfig,
    many_ModelConfig,
    RetryConfig,
    many_RetryConfig,
)

__all__ = [
    # Main client
    "LLMClient",

    # Client implementations
    "AnthropicClient",
    "OpenAIClient",
    "LLMClientBase",

    # Data models
    "LLMProvider",
    "Message",
    "MessageRole",
    "Tool",
    "ToolFunction",
    "ToolType",
    "ToolCall",
    "LLMResponse",
    "ModelConfig",
    "many_ModelConfig",
    "RetryConfig",
    "many_RetryConfig",
]

__version__ = "0.1.0"
