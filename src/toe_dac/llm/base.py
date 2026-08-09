"""Base class for LLM clients."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
import logging

from .node.node import Message, LLMResponse, RetryConfig

logger = logging.getLogger(__name__)


class LLMClientBase(ABC):
    """Abstract base class for LLM clients.

    This class defines the interface that all LLM clients must implement,
    regardless of the underlying API protocol (Anthropic, OpenAI, etc.).
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        retry_config: Optional[RetryConfig] = None,
        task_id: Optional[str] = None,  # 关联的任务ID，用于日志记录
    ):
        """Initialize the LLM client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
            task_id: Optional task ID for logging
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.retry_config = retry_config or RetryConfig()
        self.task_id = task_id

        # Callback for tracking retry count
        self.retry_callback = None

    @abstractmethod
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
            LLMResponse containing the generated content, thinking, and tool calls
        """
        pass

    @abstractmethod
    def _prepare_request(
        self,
        messages: List[Message],
        tools: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Prepare the request payload for the API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        pass

    @abstractmethod
    def _convert_messages(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert internal message format to API-specific format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        pass

    @abstractmethod
    def _parse_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """Parse API response to internal format.

        Args:
            response_data: Raw response from API

        Returns:
            LLMResponse object
        """
        pass

    def _log_request(
        self,
        request_data: Dict[str, Any],
        messages: List[Message],
        tools: Optional[List[Any]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        """Log a request without credentials.

        Persistent TOE-DAC tracing is handled by the adapter/runner, so this
        transport layer deliberately has no dependency on Andybot Gateway.
        """
        safe_data = request_data.copy()
        if "api_key" in safe_data:
            safe_data["api_key"] = "***REDACTED***"
        if "Authorization" in str(safe_data):
            # Handle headers if present
            if "headers" in safe_data and "Authorization" in safe_data["headers"]:
                safe_data["headers"]["Authorization"] = "***REDACTED***"

        logger.debug("LLM request id=%s payload=%s", request_id, safe_data)

    def _estimate_total_tokens(self, messages: List[Message], tools: Optional[List[Any]]) -> int:
        """估算总 tokens 数

        这是一个简化的估算，实际应该使用 tokenizer。
        对于中文，大致按字符数估算。
        """
        total = 0

        # 消息内容
        for msg in messages:
            if msg.content:
                total += len(msg.content) * 1.5  # 中文 token 估算
            if msg.thinking:
                total += len(msg.thinking) * 1.5

        # 工具定义
        if tools:
            for tool in tools:
                if hasattr(tool, "model_dump"):
                    tool_dict = tool.model_dump()
                    total += len(str(tool_dict)) * 0.5
                elif isinstance(tool, dict):
                    total += len(str(tool)) * 0.5

        return int(total)

    def _log_response(
        self,
        response_data: Dict[str, Any],
        response: LLMResponse,
        request_id: Optional[str] = None,
    ) -> None:
        logger.debug(
            "LLM response id=%s model=%s finish=%s usage=%s",
            request_id,
            response.model_id,
            response.finish_reason,
            response.usage,
        )
