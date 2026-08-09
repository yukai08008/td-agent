from __future__ import annotations

import asyncio
import sys
from typing import Any

import questionary
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

from state_machine import TransitionError

from .conversation import ConversationController
from .events import ConversationEvent


PHASE_LABELS = {
    "target": "Target",
    "observe": "Observe",
    "estimate": "Estimate",
    "decide": "Decide",
    "act": "Act",
    "recover": "Recover",
}


class ChatUI:
    """Rich terminal view over the UI-neutral conversation controller."""

    def __init__(self, controller: ConversationController, model_id: str, console: Console | None = None):
        self.controller = controller
        self.model_id = model_id
        self.console = console or Console(theme=Theme({"phase": "cyan", "success": "green"}))
        self._status: Any = None

    def run(self) -> None:
        self.show_header()
        self.console.print("[dim]输入需求开始；/help 查看会话命令。[/dim]\n")
        try:
            while True:
                try:
                    content = self._prompt()
                except (EOFError, KeyboardInterrupt):
                    self._stop_status()
                    self.console.print()
                    return
                if content is None:
                    return
                content = content.strip()
                if not content:
                    continue
                try:
                    if self._handle_command(content):
                        if content in {"/quit", "/exit"}:
                            return
                        continue
                    asyncio.run(self.controller.handle_user_events(content, self.render_event))
                    self._stop_status()
                except (ValueError, RuntimeError, TransitionError) as exc:
                    self._stop_status()
                    self.console.print(Panel(str(exc), title="执行异常", border_style="red"))
        finally:
            self._stop_status()
            self.controller.close_session()

    def _prompt(self) -> str | None:
        if sys.stdin.isatty():
            return questionary.text("You ›", qmark="").ask()
        return input("You > ")

    def _handle_command(self, content: str) -> bool:
        if not content.startswith("/"):
            return False
        if content in {"/quit", "/exit"}:
            return True
        if content == "/help":
            self.console.print(
                "/status  当前状态    /history  最近消息\n"
                "/pause   暂停        /resume   恢复        /cancel  取消    /quit  退出"
            )
        elif content == "/status":
            self.show_header(verbose=True)
        elif content == "/history":
            self.show_history()
        elif content == "/pause":
            self.controller.service.pause()
            self.show_header()
        elif content == "/resume":
            self.controller.service.resume()
            self.show_header()
        elif content == "/cancel":
            self.controller.service.cancel()
            self.show_header()
        else:
            self.console.print(f"[yellow]未知命令：{content}。使用 /help 查看可用命令。[/yellow]")
        return True

    def show_header(self, verbose: bool = False) -> None:
        context = self.controller.service.context
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("Thread", context["user_thread_id"])
        table.add_row("Session", context["session_id"])
        table.add_row("阶段", self.controller.service.state.value)
        table.add_row("模型", self.model_id)
        if verbose:
            table.add_row("TD", context["td_id"])
            table.add_row("Revision", str(context["revision"]))
        self.console.print(Panel(table, title="TOE-DAC", border_style="cyan", expand=False))

    def show_history(self) -> None:
        context = self.controller.service.context
        history = self.controller.repository.message_history(context["user_thread_id"], limit=20)
        if not history:
            self.console.print("[dim]当前还没有消息。[/dim]")
            return
        for item in history:
            label = "You" if item["role"] == "user" else "Agent"
            style = "blue" if item["role"] == "user" else "cyan"
            self.console.print(Panel(Markdown(item["content"]), title=label, border_style=style, expand=False))

    def render_event(self, event: ConversationEvent) -> None:
        if event.type == "phase_started":
            self._stop_status()
            phase = PHASE_LABELS.get(event.phase or "", event.phase or "处理")
            self._status = self.console.status(f"[phase]{phase}[/phase] 正在处理…")
            self._status.start()
            return
        self._stop_status()
        if event.type == "phase_completed":
            self.console.print(f"[success]✓[/success] {event.message}")
        elif event.type == "human_question":
            reason = event.data.get("reason")
            body = event.message if not reason else f"{event.message}\n\n[dim]原因：{reason}[/dim]"
            self.console.print(Panel(body, title="需要你的确认", border_style="yellow", expand=False))
        elif event.type in {"executor_boundary", "recovery_required"}:
            self.console.print(Panel(event.message, title="执行边界", border_style="yellow", expand=False))
        elif event.visible:
            self.console.print(Panel(Markdown(event.message), title="Agent", border_style="cyan", expand=False))

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None


def run_chat(controller: ConversationController, model_id: str) -> None:
    ChatUI(controller, model_id).run()
