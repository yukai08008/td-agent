"""Anthropic-compatible LLM client."""

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple
import logging

from .base import LLMClientBase
from .http_transport import HTTPResponseError, post_json
from .node.node import Message, MessageRole, LLMResponse, ToolCall, ToolType

logger = logging.getLogger(__name__)


class AnthropicClient(LLMClientBase):
    """LLM client for Anthropic-compatible APIs."""

    async def generate(
        self,
        messages: List[Message],
        tools: Optional[List[Any]] = None,
    ) -> LLMResponse:
        """Generate response from Anthropic-compatible API.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content
        """
        # Prepare request payload
        request_data = self._prepare_request(messages, tools)
        self._log_request(request_data, messages, tools)

        # Determine the request URL
        # If api_base already ends with /messages, use as-is;
        # otherwise append /messages (for base URLs like https://api.anthropic.com/v1)
        request_url = self.api_base
        if not request_url.endswith("/messages"):
            request_url = f"{request_url.rstrip('/')}/messages"

        # Make API request
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            response_data = await post_json(request_url, headers, request_data)
            parsed_response = self._parse_response(response_data)
            self._log_response(response_data, parsed_response)
            return parsed_response
        except HTTPResponseError as e:
            logger.error("HTTP error in Anthropic client: %s", e)
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in Anthropic client: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in Anthropic client: {e}")
            raise

    def _prepare_request(
        self,
        messages: List[Message],
        tools: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Prepare Anthropic-compatible request payload.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        system_message, api_messages = self._convert_messages(messages)

        request_data = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": 4096,  # Default value, can be overridden
        }

        if system_message:
            request_data["system"] = system_message

        if tools:
            request_data["tools"] = self._convert_tools(tools)

        return request_data

    def _convert_messages(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert internal messages to Anthropic format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        system_message = None
        api_messages = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                system_message = msg.content
            else:
                api_msg = {
                    "role": msg.role.value,
                    "content": msg.content,
                }

                # Add tool calls if present
                if msg.tool_calls:
                    api_msg["content"] = [
                        {
                            "type": "tool_use",
                            "id": tool_call.get("id", ""),
                            "name": tool_call.get("function", {}).get("name", ""),
                            "input": json.loads(tool_call.get("function", {}).get("arguments", "{}")),
                        }
                        for tool_call in msg.tool_calls
                    ]
                elif msg.role == MessageRole.TOOL:
                    api_msg["content"] = [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id or "",
                            "content": msg.content,
                        }
                    ]

                api_messages.append(api_msg)

        return system_message, api_messages

    def _convert_tools(self, tools: List[Any]) -> List[Dict[str, Any]]:
        """Convert tools to Anthropic format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in Anthropic format
        """
        anthropic_tools = []

        for tool in tools:
            if hasattr(tool, "model_dump"):
                tool_dict = tool.model_dump()
            elif isinstance(tool, dict):
                tool_dict = tool
            else:
                continue

            anthropic_tool = {
                "name": tool_dict.get("function", {}).get("name", ""),
                "description": tool_dict.get("function", {}).get("description", ""),
                "input_schema": tool_dict.get("function", {}).get("parameters", {}),
            }
            anthropic_tools.append(anthropic_tool)

        return anthropic_tools

    def _parse_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """Parse Anthropic API response.

        Args:
            response_data: Raw response from API

        Returns:
            LLMResponse object
        """
        content = ""
        thinking = None
        tool_calls = []

        # Extract content from response
        if "content" in response_data:
            for item in response_data["content"]:
                if item.get("type") == "text":
                    content = item.get("text", "")
                elif item.get("type") == "thinking":
                    thinking = item.get("thinking", "")
                elif item.get("type") == "tool_use":
                    tool_call = ToolCall(
                        id=item.get("id", ""),
                        type=ToolType.FUNCTION,
                        function={
                            "name": item.get("name", ""),
                            "arguments": json.dumps(item.get("input", {})),
                        }
                    )
                    tool_calls.append(tool_call)

        # Extract finish reason
        finish_reason = response_data.get("stop_reason", "")

        # Extract usage
        usage = None
        if "usage" in response_data:
            usage = {
                "input_tokens": response_data["usage"].get("input_tokens", 0),
                "output_tokens": response_data["usage"].get("output_tokens", 0),
                "total_tokens": response_data["usage"].get("input_tokens", 0) + response_data["usage"].get("output_tokens", 0),
            }

        return LLMResponse(
            content=content,
            thinking=thinking,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
            usage=usage,
            model_id=self._normalize_model_id(self.model),  # 使用标准化的模型ID
        )

    def _normalize_model_id(self, model_name: str) -> str:
        """标准化模型ID

        将模型名称中的空格替换为下划线，便于在数据模型中使用
        """
        # 将空格替换为下划线，移除其他特殊字符
        normalized = model_name.lower().replace(' ', '_')
        # 移除括号和特殊字符
        normalized = ''.join(c for c in normalized if c.isalnum() or c == '_')
        # 确保不以数字开头
        if normalized and normalized[0].isdigit():
            normalized = 'model_' + normalized
        return normalized
