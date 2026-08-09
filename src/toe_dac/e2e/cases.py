from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CaseDefinition:
    case_id: str
    title: str
    level: str
    description: str
    fixture_name: str
    target: dict[str, Any]
    budgets: dict[str, int]
    user_request: str = ""
    oracle: dict[str, Any] = field(default_factory=dict)


class CaseRegistry:
    def __init__(self):
        common_budget = {
            "max_llm_calls": 14,
            "max_actions": 8,
            "max_recoveries": 3,
            "max_wall_seconds": 300,
        }
        self._cases = {
            "REG-001": CaseDefinition(
                case_id="REG-001",
                title="Example.com 网页取证与中文报告",
                level="L1",
                description=(
                    "标准只读 Web 回归：验证一次输入可完成浏览、事实提取、截图取证、"
                    "中文报告、双层检查和终态持久化。"
                ),
                fixture_name="web_report",
                user_request=(
                    "访问 https://example.com，确认页面标题和主要内容，"
                    "生成一份简短中文报告，并保留网页截图作为证据。"
                ),
                target={
                    "positive": [
                        "访问 https://example.com 并确认页面标题",
                        "确认页面主要内容并生成简短中文报告",
                        "保存真实网页截图作为证据",
                    ],
                    "negative": [
                        "不得猜测网页内容", "不得伪造截图", "不得修改网页或执行其他外部写操作",
                    ],
                    "acceptance_criteria": [
                        {"criterion_id": "tc_title", "description": "报告确认标题为 Example Domain", "required": True},
                        {"criterion_id": "tc_content", "description": "报告概括页面的示例域名用途", "required": True},
                        {"criterion_id": "tc_report", "description": "产出简短中文报告", "required": True},
                        {"criterion_id": "tc_screenshot", "description": "存在有效 PNG 网页截图证据", "required": True},
                    ],
                },
                budgets={
                    **common_budget,
                    "max_llm_calls": 12,
                    "max_actions": 2,
                    "max_wall_seconds": 180,
                },
                oracle={
                    "expected_url": "https://example.com",
                    "expected_title": "Example Domain",
                    "content_markers": ["documentation examples", "illustrative examples", "示例", "文档"],
                    "report_language": "zh-CN",
                    "screenshot_format": "png",
                    "human_interrupts": 0,
                    "terminal_state": "succeeded",
                },
            ),
            "LIVE-001": CaseDefinition(
                case_id="LIVE-001",
                title="模糊需求请求人工补充",
                level="L0",
                description="验证 Targeting 不臆造目标，并可跨 Session 接收人工答案。",
                fixture_name="readme_project",
                target={
                    "positive": ["README 包含安装、运行和测试说明"],
                    "negative": ["不得修改代码和测试"],
                    "acceptance_criteria": [
                        {"criterion_id": "tc_readme", "description": "README 包含安装、运行和测试三个章节", "required": True}
                    ],
                },
                budgets=dict(common_budget),
            ),
            "LIVE-002": CaseDefinition(
                case_id="LIVE-002",
                title="修复 Python 计算器边界错误",
                level="L2",
                description="验证完整 TOE-DAC、Action Check 与 Target Check。",
                fixture_name="calculator_bug",
                target={
                    "positive": ["使计算器项目全部测试通过"],
                    "negative": ["不得修改测试", "不得改变 divide 函数签名"],
                    "acceptance_criteria": [
                        {"criterion_id": "tc_tests", "description": "pytest -q 退出码为 0", "required": True},
                        {"criterion_id": "tc_tests_unchanged", "description": "测试文件未改变", "required": True},
                    ],
                },
                budgets=dict(common_budget),
            ),
            "LIVE-006": CaseDefinition(
                case_id="LIVE-006",
                title="非法 Plan 输出修复",
                level="L1",
                description="模拟模型首次遗漏 actions，验证拒绝操作不污染状态并可修复。",
                fixture_name="calculator_bug",
                target={
                    "positive": ["产出一个结构合法的修复计划"],
                    "negative": ["非法 Plan 不得进入 Act"],
                    "acceptance_criteria": [
                        {"criterion_id": "tc_plan", "description": "Plan 至少包含一个带断言的 Action", "required": True}
                    ],
                },
                budgets=dict(common_budget),
            ),
        }

    def list(self) -> list[CaseDefinition]:
        return list(self._cases.values())

    def get(self, case_id: str) -> CaseDefinition:
        try:
            return self._cases[case_id.upper()]
        except KeyError as exc:
            raise KeyError(f"unknown case: {case_id}") from exc

    @staticmethod
    def fixture_root(case: CaseDefinition) -> Path:
        return Path(__file__).parent / "fixtures" / case.fixture_name
