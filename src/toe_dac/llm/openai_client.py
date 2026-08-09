"""OpenAI-compatible LLM client."""

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
import logging

from .base import LLMClientBase
from .node.node import Message, MessageRole, LLMResponse, ToolCall, ToolType

logger = logging.getLogger(__name__)


class OpenAIClient(LLMClientBase):
    """LLM client for OpenAI-compatible APIs."""

    async def generate(
        self,
        messages: List[Message],
        tools: Optional[List[Any]] = None,
    ) -> LLMResponse:
        """Generate response from OpenAI-compatible API.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content
        """
        # 生成请求ID用于关联请求和响应
        request_id = str(uuid.uuid4())[:8]

        # Prepare request payload
        request_data = self._prepare_request(messages, tools)
        self._log_request(request_data, messages, tools, request_id)

        # Determine the request URL
        # If api_base already ends with /chat/completions, use as-is;
        # otherwise append /chat/completions (for base URLs like https://api.deepseek.com/v1)
        request_url = self.api_base
        if not request_url.endswith("/chat/completions"):
            request_url = f"{request_url.rstrip('/')}/chat/completions"

        # Make API request
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    request_url,
                    headers=headers,
                    json=request_data,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_body = await response.text()
                        logger.error(f"HTTP {response.status} from {request_url}: {error_body[:500]}")
                        response.raise_for_status()
                    response_data = await response.json()

                    # Parse response
                    parsed_response = self._parse_response(response_data)

                    # Log response with correlation
                    self._log_response(response_data, parsed_response, request_id)

                    return parsed_response

        except aiohttp.ClientError as e:
            logger.error(f"HTTP error in OpenAI client: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in OpenAI client: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in OpenAI client: {e}")
            raise

    def _prepare_request(
        self,
        messages: List[Message],
        tools: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Prepare OpenAI-compatible request payload.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        system_message, api_messages = self._convert_messages(messages)

        # Start with basic request data
        request_data = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": 4096,  # Default value, can be overridden
        }

        # Add system message if present
        if system_message:
            # Insert system message at the beginning
            request_data["messages"].insert(0, {"role": "system", "content": system_message})

        # Add tools if present
        if tools:
            request_data["tools"] = self._convert_tools(tools)
            request_data["tool_choice"] = "auto"

        return request_data

    def _convert_messages(self, messages: List[Message]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert internal messages to OpenAI format.

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
                continue

            api_msg = {
                "role": msg.role.value,
            }

            # Handle tool calls - assistant with tool_calls
            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                api_msg["content"] = msg.content if msg.content else None
                # DeepSeek V4 thinking 模式：必须传回 reasoning_content
                if msg.thinking:
                    api_msg["reasoning_content"] = msg.thinking
                api_msg["tool_calls"] = [
                    {
                        "id": tool_call.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tool_call.get("function", {}).get("name", ""),
                            "arguments": tool_call.get("function", {}).get("arguments", "{}"),
                        }
                    }
                    for tool_call in msg.tool_calls
                ]
            # Handle tool responses
            elif msg.role == MessageRole.TOOL and msg.tool_call_id:
                api_msg["content"] = msg.content
                api_msg["tool_call_id"] = msg.tool_call_id
            else:
                # 普通 assistant 消息也需要传回 reasoning_content
                api_msg["content"] = msg.content if msg.content else None
                if msg.role == MessageRole.ASSISTANT and msg.thinking:
                    api_msg["reasoning_content"] = msg.thinking

            api_messages.append(api_msg)

        return system_message, api_messages

    def _convert_tools(self, tools: List[Any]) -> List[Dict[str, Any]]:
        """Convert tools to OpenAI format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in OpenAI format
        """
        openai_tools = []

        for tool in tools:
            if hasattr(tool, "model_dump"):
                tool_dict = tool.model_dump()
            elif isinstance(tool, dict):
                tool_dict = tool
            else:
                continue

            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool_dict.get("function", {}).get("name", ""),
                    "description": tool_dict.get("function", {}).get("description", ""),
                    "parameters": tool_dict.get("function", {}).get("parameters", {}),
                }
            }
            openai_tools.append(openai_tool)

        return openai_tools

    def _parse_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """Parse OpenAI API response.

        Args:
            response_data: Raw response from API

        Returns:
            LLMResponse object
        """
        # Extract choice data
        choices = response_data.get("choices", [])
        if not choices:
            raise ValueError("No choices in response")

        choice = choices[0]
        message = choice.get("message", {})

        # Extract content
        content = message.get("content") or ""

        # Extract thinking (from reasoning or reasoning_content if available)
        thinking = None
        if "reasoning" in message:
            thinking = message.get("reasoning", "")
        elif "reasoning_content" in message:
            thinking = message.get("reasoning_content", "")

        # Extract tool calls
        tool_calls = []
        msg_tool_calls = message.get("tool_calls")
        if msg_tool_calls and isinstance(msg_tool_calls, list):
            for tool_call_data in msg_tool_calls:
                function_data = tool_call_data.get("function", {})
                tool_call = ToolCall(
                    id=tool_call_data.get("id", ""),
                    type=ToolType.FUNCTION,
                    function={
                        "name": function_data.get("name", ""),
                        "arguments": function_data.get("arguments", "{}"),
                    }
                )
                tool_calls.append(tool_call)

        # Extract finish reason
        finish_reason = choice.get("finish_reason", "")

        # Extract usage
        usage = None
        if "usage" in response_data:
            usage = {
                "input_tokens": response_data["usage"].get("prompt_tokens", 0),
                "output_tokens": response_data["usage"].get("completion_tokens", 0),
                "total_tokens": response_data["usage"].get("total_tokens", 0),
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
