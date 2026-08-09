"""Lightweight LLM data models with no runtime validation dependency."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DataModel:
    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class Message(DataModel):
    role: MessageRole
    content: str
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    thinking: str | None = None


class ToolType(str, Enum):
    FUNCTION = "function"


@dataclass
class ToolFunction(DataModel):
    name: str
    parameters: dict[str, Any]
    description: str | None = None


@dataclass
class Tool(DataModel):
    type: ToolType
    function: ToolFunction


@dataclass
class ToolCall(DataModel):
    id: str
    type: ToolType
    function: dict[str, Any]


@dataclass
class LLMResponse(DataModel):
    content: str | None = None
    thinking: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    model_id: str | None = None


@dataclass
class ModelConfig(DataModel):
    id: str
    name: str
    vendor: str
    apiKey: str
    url: str
    maxInputTokens: int
    maxOutputTokens: int
    supportsToolCall: bool
    supportsImages: bool
    enabled: bool

    @property
    def normalized_model_id(self) -> str:
        normalized = "".join(
            character
            for character in self.name.lower().replace(" ", "_")
            if character.isalnum() or character == "_"
        )
        if normalized and normalized[0].isdigit():
            normalized = "model_" + normalized
        return normalized


@dataclass
class many_ModelConfig:
    root: list[ModelConfig]

    def get_data(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.root]


@dataclass
class RetryConfig(DataModel):
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    retry_on_status: list[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])


@dataclass
class many_RetryConfig:
    root: list[RetryConfig]

    def get_data(self) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.root]
