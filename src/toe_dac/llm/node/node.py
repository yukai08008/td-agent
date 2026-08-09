"""LLM 相关数据模型 - 遵循 st-0001 规范

该模块包含 LLM 客户端所需的数据模型，包括：
- 消息模型
- 工具调用模型
- 响应模型
- 配置模型
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, RootModel


class LLMProvider(str, Enum):
    """LLM 提供商枚举

    规范编号: st-0001
    说明: 支持的 LLM 提供商类型
    """
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class MessageRole(str, Enum):
    """消息角色枚举

    规范编号: st-0001
    说明: 消息的角色类型
    """
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(BaseModel):
    """消息模型

    规范编号: st-0001
    说明: 表示 LLM 对话中的一条消息
    """
    role: MessageRole = Field(description="消息角色")
    content: str = Field(description="消息内容")
    name: Optional[str] = Field(default=None, description="发送者名称")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(default=None, description="工具调用列表")
    tool_call_id: Optional[str] = Field(default=None, description="工具调用ID")
    thinking: Optional[str] = Field(default=None, description="思考过程（DeepSeek V4 等模型的 reasoning_content）")


class ToolType(str, Enum):
    """工具类型枚举

    规范编号: st-0001
    说明: 工具的类型
    """
    FUNCTION = "function"


class ToolFunction(BaseModel):
    """工具函数模型

    规范编号: st-0001
    说明: 工具函数的描述
    """
    name: str = Field(description="函数名称")
    description: Optional[str] = Field(default=None, description="函数描述")
    parameters: Dict[str, Any] = Field(description="函数参数")


class Tool(BaseModel):
    """工具模型

    规范编号: st-0001
    说明: 可用的工具定义
    """
    type: ToolType = Field(description="工具类型")
    function: ToolFunction = Field(description="工具函数")


class ToolCall(BaseModel):
    """工具调用模型

    规范编号: st-0001
    说明: LLM 对工具的调用
    """
    id: str = Field(description="工具调用ID")
    type: ToolType = Field(description="工具类型")
    function: Dict[str, Any] = Field(description="函数调用详情")


class LLMResponse(BaseModel):
    """LLM 响应模型

    规范编号: st-0001
    说明: LLM 生成的响应
    """
    content: Optional[str] = Field(default=None, description="响应内容")
    thinking: Optional[str] = Field(default=None, description="思考过程")
    tool_calls: Optional[List[ToolCall]] = Field(default=None, description="工具调用列表")
    finish_reason: Optional[str] = Field(default=None, description="完成原因")
    usage: Optional[Dict[str, int]] = Field(default=None, description="使用统计")
    model_id: Optional[str] = Field(default=None, description="模型ID")


class ModelConfig(BaseModel):
    """模型配置模型

    规范编号: st-0001
    说明: 单个 LLM 模型的配置信息
    """
    id: str = Field(description="模型ID")
    name: str = Field(description="模型显示名称")
    vendor: str = Field(description="供应商")
    apiKey: str = Field(description="API密钥")
    url: str = Field(description="API地址")
    maxInputTokens: int = Field(description="最大输入token数")
    maxOutputTokens: int = Field(description="最大输出token数")
    supportsToolCall: bool = Field(description="是否支持工具调用")
    supportsImages: bool = Field(description="是否支持图片")
    enabled: bool = Field(description="是否启用")

    @property
    def normalized_model_id(self) -> str:
        """获取标准化的模型ID

        使用 name 字段，将空格替换为下划线，便于在数据模型中使用
        相同的模型可能来自不同的 vendor，所以使用 name 而不是 id
        """
        # 将空格替换为下划线，移除其他特殊字符
        normalized = self.name.lower().replace(' ', '_')
        # 移除括号和特殊字符
        normalized = ''.join(c for c in normalized if c.isalnum() or c == '_')
        # 确保不以数字开头
        if normalized[0].isdigit():
            normalized = 'model_' + normalized
        return normalized


class many_ModelConfig(RootModel[List[ModelConfig]]):
    """多个模型配置的包装模型

    规范编号: st-0001
    说明: 多个 ModelConfig 对象的集合
    """

    def get_data(self) -> List[Dict[str, Any]]:
        """获取模型数据列表

        返回:
            模型配置字典列表
        """
        return [x.model_dump() for x in self.root]


class RetryConfig(BaseModel):
    """重试配置模型

    规范编号: st-0001
    说明: 请求重试的配置
    """
    max_retries: int = Field(default=3, description="最大重试次数")
    base_delay: float = Field(default=1.0, description="基础延迟（秒）")
    max_delay: float = Field(default=30.0, description="最大延迟（秒）")
    backoff_factor: float = Field(default=2.0, description="退避因子")
    retry_on_status: List[int] = Field(default=[429, 500, 502, 503, 504], description="重试状态码")


class many_RetryConfig(RootModel[List[RetryConfig]]):
    """多个重试配置的包装模型

    规范编号: st-0001
    说明: 多个 RetryConfig 对象的集合
    """

    def get_data(self) -> List[Dict[str, Any]]:
        """获取重试配置数据列表

        返回:
            重试配置字典列表
        """
        return [x.model_dump() for x in self.root]
