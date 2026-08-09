"""LLM client wrapper that supports multiple providers.

This module provides a unified interface for different LLM providers
(Anthropic and OpenAI) through a single LLMClient class.
"""

import logging
from typing import Any, List, Optional

from .node.node import LLMProvider, Message, LLMResponse, RetryConfig
from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .openai_client import OpenAIClient

logger = logging.getLogger(__name__)


class LLMClient:
    """LLM Client wrapper supporting multiple providers.

    This class provides a unified interface for different LLM providers.
    It automatically instantiates the correct underlying client based on
    the provider parameter.

    For MiniMax API (api.minimax.io or api.minimaxi.com), it appends the
    appropriate endpoint suffix based on provider:
    - anthropic: /anthropic
    - openai: /v1

    For third-party APIs, it uses the api_base as-is.
    """

    # MiniMax API domains that need automatic suffix handling
    MINIMAX_DOMAINS = ("api.minimax.io", "api.minimaxi.com")

    def __init__(
        self,
        api_key: str,
        provider: LLMProvider = LLMProvider.ANTHROPIC,
        api_base: str = "https://api.minimaxi.com",
        model: str = "MiniMax-M2.5",
        retry_config: Optional[RetryConfig] = None,
        model_config: Optional[Any] = None,  # 新增：可选的 ModelConfig 对象
        task_id: Optional[str] = None,  # 新增：任务ID，用于日志记录
    ):
        """Initialize LLM client with specified provider.

        Args:
            api_key: API key for authentication
            provider: LLM provider (anthropic or openai)
            api_base: Full API endpoint URL.
            model: Model name to use
            retry_config: Optional retry configuration
            model_config: Optional ModelConfig object for normalized model_id
            task_id: Optional task ID for logging
        """
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.retry_config = retry_config or RetryConfig()
        self.model_config = model_config  # 新增

        # Normalize api_base (remove trailing slash)
        api_base = api_base.rstrip("/")

        # Check if this is a MiniMax bare domain (without path suffix)
        is_minimax_bare = (
            any(domain in api_base for domain in self.MINIMAX_DOMAINS)
            and not any(suffix in api_base for suffix in ["/v1", "/anthropic", "/chat/completions"])
        )

        if is_minimax_bare:
            # For MiniMax bare domain, append the appropriate suffix
            if provider == LLMProvider.ANTHROPIC:
                full_api_base = f"{api_base}/anthropic/v1/messages"
            elif provider == LLMProvider.OPENAI:
                full_api_base = f"{api_base}/v1/chat/completions"
            else:
                raise ValueError(f"Unsupported provider: {provider}")
        else:
            # For all other APIs (including models.json configs with full URLs),
            # use api_base as-is
            full_api_base = api_base

        self.api_base = full_api_base

        # Store task_id for logging
        self.task_id = task_id

        # Instantiate the appropriate client
        self._client: LLMClientBase
        if provider == LLMProvider.ANTHROPIC:
            self._client = AnthropicClient(
                api_key=api_key,
                api_base=full_api_base,
                model=model,
                retry_config=retry_config,
                task_id=task_id,
            )
        elif provider == LLMProvider.OPENAI:
            self._client = OpenAIClient(
                api_key=api_key,
                api_base=full_api_base,
                model=model,
                retry_config=retry_config,
                task_id=task_id,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        logger.debug("Initialized LLM client with provider: %s, api_base: %s", provider, full_api_base)

    @property
    def retry_callback(self):
        """Get retry callback."""
        return self._client.retry_callback

    @retry_callback.setter
    def retry_callback(self, value):
        """Set retry callback."""
        self._client.retry_callback = value

    async def generate(
        self,
        messages: List[Message],
        tools: Optional[List[Any]] = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content
        """
        response = await self._client.generate(messages, tools)

        # 如果有 model_config，使用其 normalized_model_id 覆盖
        if self.model_config and hasattr(self.model_config, 'normalized_model_id'):
            response.model_id = self.model_config.normalized_model_id

        return response
