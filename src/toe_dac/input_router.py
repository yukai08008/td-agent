from __future__ import annotations

import re
from typing import Literal


ConversationIntent = Literal[
    "greeting", "clarify", "status", "inspect_target", "inspect_observation",
    "inspect_estimate", "inspect_plan", "inspect_action", "inspect_errors",
    "inspect_timing", "task_input",
]


def route_input(content: str) -> ConversationIntent:
    """Route local conversation-control input without invoking the model."""
    normalized = re.sub(r"[\s!！。,.，？?～~]+", "", content).casefold()
    if normalized in {
        "你好", "您好", "嗨", "哈喽", "早上好", "上午好", "下午好", "晚上好",
        "hi", "hello", "hey", "goodmorning", "goodafternoon", "goodevening",
    }:
        return "greeting"
    if normalized in {
        "啥意思", "什么意思", "这是什么意思", "没明白", "不明白", "我没明白",
        "解释一下", "请解释", "为什么", "为什么这样", "为什么停了", "为什么停下",
        "为什么卡住了", "为什么不能继续", "我该说什么", "我应该说什么",
        "现在需要我做什么", "需要我做什么", "我能做什么", "whatdoyoumean",
        "idontunderstand", "explain", "why", "whatshouldido",
    }:
        return "clarify"
    if normalized in {
        "状态", "当前状态", "现在什么状态", "进展", "当前进展", "进展怎么样",
        "现在到哪了", "做到哪了", "status", "progress",
    }:
        return "status"
    if normalized in {"目标是什么", "当前目标", "看目标", "target", "showtarget"}:
        return "inspect_target"
    if normalized in {
        "你observe到了什么", "observe到了什么", "观察到了什么", "当前观察",
        "看观察", "showobserve", "observation", "showobservation",
    }:
        return "inspect_observation"
    if normalized in {"怎么评估的", "当前评估", "看评估", "estimate", "showestimate"}:
        return "inspect_estimate"
    if normalized in {"计划是什么", "当前计划", "看计划", "plan", "showplan"}:
        return "inspect_plan"
    if normalized in {"当前动作", "正在执行什么", "看动作", "action", "showaction"}:
        return "inspect_action"
    if normalized in {"出了什么错", "最近错误", "看错误", "errors", "showerrors"}:
        return "inspect_errors"
    if normalized in {"用了多久", "耗时", "时间统计", "timing", "showtiming"}:
        return "inspect_timing"
    return "task_input"
