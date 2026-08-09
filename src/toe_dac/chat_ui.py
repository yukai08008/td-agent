from __future__ import annotations

import asyncio
import difflib
import os
import platform
import subprocess
import sys
from typing import Any

import questionary
from prompt_toolkit.history import FileHistory
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
    "action_check": "Action Check",
    "target_check": "Target Check",
    "recover": "Recover",
}


class ChatUI:
    """Rich terminal view over the UI-neutral conversation controller."""

    def __init__(self, controller: ConversationController, model_id: str, console: Console | None = None):
        self.controller = controller
        self.model_id = model_id
        self.console = console or Console(theme=Theme({"phase": "cyan", "success": "green"}))
        self._status: Any = None
        history_path = self.controller.repository.session_evidence_dir(
            self.controller.service.context
        ) / ".input-history"
        self._input_history = FileHistory(str(history_path))

    def run(self) -> None:
        self.show_header()
        resume_hint = self.controller.resume_hint()
        if resume_hint:
            title = "需求概要" if self.controller.read_only else "继续已有需求"
            self.console.print(Panel(resume_hint, title=title, border_style="blue", expand=False))
        if self.controller.read_only:
            self.console.print(Panel(
                "需求已经结束，当前为只读浏览模式。使用 /history、/show、/evidence 或 /artifacts 查看。",
                title="只读 Session", border_style="yellow", expand=False,
            ))
        else:
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
                if self.controller.read_only and not content.startswith("/"):
                    self.console.print(Panel(
                        "完成态 Session 不再接受新的任务输入。使用只读命令查看，或用 `toe-dac new` 开始新需求。",
                        title="只读模式", border_style="yellow", expand=False,
                    ))
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
            self.controller.detach_connection()

    def _prompt(self) -> str | None:
        if sys.stdin.isatty():
            return questionary.text("You ›", qmark="", history=self._input_history).ask()
        return input("You > ")

    def _handle_command(self, content: str) -> bool:
        if not content.startswith("/"):
            return False
        if content in {"/quit", "/exit"}:
            return True
        known_exact = {
            "/help", "/status", "/history", "/why", "/show", "/timing", "/evidence",
            "/artifacts", "/continue", "/reobserve", "/replan", "/pause", "/resume",
            "/cancel", "/quit", "/exit",
        }
        known_prefixes = ("/show ", "/reobserve ", "/replan ")
        if content not in known_exact and not content.startswith(known_prefixes):
            command = content.split(maxsplit=1)[0]
            suggestion = difflib.get_close_matches(command, sorted(known_exact), n=1, cutoff=0.6)
            hint = f"；你是否想输入 `{suggestion[0]}`？" if suggestion else ""
            self.console.print(f"[yellow]未知命令：{command}{hint} 使用 /help 查看可用命令。[/yellow]")
            return True
        read_only_commands = {
            "/help", "/status", "/history", "/why", "/show", "/timing", "/evidence", "/artifacts",
        }
        if (
            self.controller.read_only
            and content not in read_only_commands
            and not content.startswith("/show ")
        ):
            self.console.print(
                "[yellow]完成态 Session 是只读的；该命令会修改状态，不能执行。[/yellow]"
            )
            return True
        if content == "/help":
            self.console.print(
                "/status  当前状态    /why  停止原因    /history  最近消息\n"
                "/show target|observe|estimate|plan|action|artifacts|errors|timing\n"
                "/evidence 只读打开当前 Session 的 trace 证据目录    /artifacts 打开产物目录\n"
                "/continue 继续       /reobserve [原因]       /replan [调整要求]\n"
                "/pause   暂停        /resume   恢复        /cancel  取消    /quit  退出"
            )
        elif content == "/status":
            self.show_header(verbose=True)
        elif content == "/history":
            self.show_history()
        elif content == "/why":
            self.console.print(Panel(Markdown(self.controller.why()), title="为什么", border_style="yellow"))
        elif content == "/show":
            self.console.print(Panel(
                Markdown(self.controller.inspect("summary")), title="summary", border_style="cyan",
            ))
        elif content.startswith("/show "):
            section = content.removeprefix("/show ").strip()
            self.console.print(Panel(Markdown(self.controller.inspect(section)), title=section, border_style="cyan"))
        elif content == "/timing":
            self.console.print(Panel(Markdown(self.controller.inspect("timing")), title="timing", border_style="cyan"))
        elif content == "/evidence":
            directory = self.controller.repository.session_evidence_dir(
                self.controller.service.context
            )
            self._open_directory(directory)
            self.console.print(f"[green]已打开 Session 证据目录：[/green]{directory}")
        elif content == "/artifacts":
            directory = self.controller.repository.artifact_dir(self.controller.service.context)
            if not directory.exists() or not any(directory.iterdir()):
                self.console.print("[yellow]当前 TD 没有可浏览的 Artifact。[/yellow]")
            else:
                self._open_directory(directory)
                self.console.print(f"[green]已打开 Artifact 目录：[/green]{directory}")
        elif content == "/continue":
            asyncio.run(self.controller.handle_user_events("继续", self.render_event))
        elif content == "/reobserve" or content.startswith("/reobserve "):
            reason = content.removeprefix("/reobserve").strip()
            self.controller.service.user_reobserve(reason)
            self.console.print("[yellow]已回到 Observe。使用 /continue 继续。[/yellow]")
            self.show_header()
        elif content == "/replan" or content.startswith("/replan "):
            reason = content.removeprefix("/replan").strip()
            self.controller.service.user_replan(reason)
            self.console.print("[yellow]已回到 Decide。使用 /continue 继续。[/yellow]")
            self.show_header()
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

    @staticmethod
    def _open_directory(directory: Any) -> None:
        path = str(directory)
        try:
            if platform.system() == "Darwin":
                subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif platform.system() == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RuntimeError(f"无法打开证据目录 {path}: {exc}") from exc

    def show_header(self, verbose: bool = False) -> None:
        context = self.controller.service.context
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("Thread", context["user_thread_id"])
        table.add_row("Session", context["session_id"])
        table.add_row("阶段", self.controller.service.state.value)
        if self.controller.read_only:
            table.add_row("模式", "只读")
        table.add_row("模型", self.model_id)
        target = context.get("target", {}).get("positive", [])
        if target:
            summary = str(target[0])
            table.add_row("需求", summary if len(summary) <= 60 else f"{summary[:57]}...")
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
        if event.type == "progress":
            self._stop_status()
            self.console.print(f"[dim]  · {event.message}[/dim]")
            phase = PHASE_LABELS.get(event.phase or "", event.phase or "处理")
            self._status = self.console.status(f"[phase]{phase}[/phase] 正在处理…")
            self._status.start()
            return
        self._stop_status()
        if event.type == "phase_completed":
            duration = event.data.get("duration_ms")
            elapsed = f" [dim]({float(duration) / 1000:.1f}s)[/dim]" if duration is not None else ""
            self.console.print(f"[success]✓[/success] {event.message}{elapsed}")
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
