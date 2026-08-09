from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from state_machine import TransitionError

from .e2e import CaseRegistry, E2ERunner
from .conversation import ConversationController
from .chat_ui import run_chat
from . import __version__
from .cli_settings import (
    default_data_dir,
    model_config_path,
    resolve_thread,
    user_config_dir,
)
from .config_manager import ensure_model_ready, print_config_status, run_config_manager
from .environment import find_project_root, load_environment
from .releases import forwarded_version_args, package_spec
from .update_check import notify_if_update_available
from .llm_adapter import TOEDACLLMAdapter
from .service import TDService
from .states import TDState
from .storage import TDRepository, short_id


def _json_input(prompt: str) -> Any:
    return json.loads(input(prompt).strip())


def _summary(service: TDService) -> None:
    print(json.dumps({
        "user_thread_id": service.context["user_thread_id"],
        "td_id": service.context["td_id"],
        "session_id": service.context["session_id"],
        "state": service.state.value,
        "revision": service.context["revision"],
        "available_events": service.available_events,
        "current_action_id": service.context["execution"]["current_action_id"],
        "last_failure": service.context["recovery"]["last_failure"],
    }, ensure_ascii=False, indent=2))


def interact(service: TDService) -> None:
    print("TOE-DAC interactive POC. Commands: next, fail, pause, resume, cancel, show, quit")
    while True:
        _summary(service)
        command = input("toe-dac> ").strip().lower()
        try:
            if command in {"quit", "q", "exit"}:
                return
            if command == "show":
                continue
            if command == "pause":
                service.pause()
                continue
            if command == "resume":
                service.resume()
                continue
            if command == "cancel":
                service.cancel()
                continue
            if command == "fail":
                if service.state == TDState.TARGETING:
                    service.fail_targeting("execution_error", input("failure message> "))
                else:
                    print("Use a failed check for Act/Target failures in this POC.")
                continue
            if command != "next":
                print("Unknown command")
                continue
            _advance_interactively(service)
        except (ValueError, RuntimeError, TransitionError) as exc:
            print(f"ERROR: {exc}")


def _advance_interactively(service: TDService) -> None:
    state = service.state
    if state == TDState.IDLE:
        service.start()
    elif state == TDState.TARGETING:
        mode = input("target mode [submit/ask]> ").strip()
        if mode == "ask":
            service.target_needs_input(input("question> "), input("reason> "))
        else:
            service.submit_target(_json_input("target JSON> "))
    elif state == TDState.OBSERVING:
        service.submit_observation(_json_input("observation JSON> "))
    elif state == TDState.ESTIMATING:
        service.submit_estimate(_json_input("estimate JSON> "))
    elif state == TDState.DECIDING:
        service.submit_plan(_json_input("plan JSON> "))
    elif state == TDState.ACTING:
        service.submit_action_result(_json_input("action result JSON> "))
    elif state == TDState.CHECKING_ACTION:
        service.check_action(_json_input("action checks JSON array> "))
    elif state == TDState.CHECKING_TARGET:
        service.check_target(_json_input("target checks JSON array> "))
    elif state == TDState.RECOVERING:
        decision = input("decision [retry_targeting/retry_action/replan/reobserve/escalate/give_up]> ").strip()
        question = input("human question (only escalate)> ").strip() if decision == "escalate" else ""
        service.recover(decision, reason=input("reason> ").strip(), human_question=question)
    elif state == TDState.WAITING_HUMAN:
        service.human_reply(_json_input("human response JSON> "))
    elif state == TDState.PAUSED:
        service.resume()
    else:
        print(f"TD is terminal: {state.value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toe-dac")
    parser.add_argument("--version", action="store_true", help="show version and system information")
    parser.add_argument(
        "--use-version",
        metavar="VERSION",
        help="temporarily run an exact tagged release without replacing the installed version",
    )
    parser.add_argument(
        "--data",
        default=os.environ.get("TOE_DAC_DATA") or str(default_data_dir()),
        help="data directory",
    )
    subparsers = parser.add_subparsers(dest="command")
    create = subparsers.add_parser("new", help="create a new User Thread and start chatting")
    create.add_argument("--thread")
    create.add_argument("--model", help="enabled model id")
    create.add_argument("--model-config", help="local model registry JSON")
    create.add_argument("--retry-budget", type=int, default=3)
    debug_create = subparsers.add_parser("debug-new", help="open the low-level state-machine console")
    debug_create.add_argument("--thread")
    debug_create.add_argument("--retry-budget", type=int, default=3)
    open_parser = subparsers.add_parser("open")
    open_parser.add_argument("thread")
    open_parser.add_argument("td")
    show = subparsers.add_parser("show")
    show.add_argument("thread")
    show.add_argument("td")

    case = subparsers.add_parser("case", help="inspect executable E2E cases")
    case_subparsers = case.add_subparsers(dest="case_command", required=True)
    case_subparsers.add_parser("list")
    case_show = case_subparsers.add_parser("show")
    case_show.add_argument("case_id")

    run = subparsers.add_parser("run", help="run an E2E case")
    run.add_argument("case_id")
    run.add_argument("--mode", choices=["mock", "live"], default="mock")
    run.add_argument("--model")
    run.add_argument("--model-config", default="config/models.json")

    resume = subparsers.add_parser("resume", help="resume a waiting E2E run")
    resume.add_argument("run_id")
    resume.add_argument("--response", help="human response as a JSON object")

    report = subparsers.add_parser("report", help="show an E2E run report")
    report.add_argument("run_id")
    report.add_argument("--json", action="store_true", dest="as_json")

    for name, help_text in (
        ("continue", "start a new Session attached to an existing User Thread"),
        ("chat", "alias of continue"),
    ):
        continue_parser = subparsers.add_parser(name, help=help_text)
        continue_parser.add_argument("--thread", help="User Thread; defaults to the most recently used thread")
        continue_parser.add_argument("--model", help="enabled model id; defaults to the configured default model")
        continue_parser.add_argument("--model-config", help="local model registry JSON")
        continue_parser.add_argument("--retry-budget", type=int, default=3)

    thread_parser = subparsers.add_parser("thread", help="inspect User Threads")
    thread_subparsers = thread_parser.add_subparsers(dest="thread_command", required=True)
    thread_subparsers.add_parser("list")
    thread_show = thread_subparsers.add_parser("show")
    thread_show.add_argument("thread_id")

    subparsers.add_parser("threads", help="list User Threads")
    sessions = subparsers.add_parser("sessions", help="list Sessions attached to a User Thread")
    sessions.add_argument("--thread", help="User Thread; defaults to the most recently used thread")
    config_parser = subparsers.add_parser("config", help="configure model API keys and defaults")
    config_parser.add_argument("--model-config", help="local model registry JSON")
    config_parser.add_argument("--show", action="store_true", help="show status without opening the manager")
    doctor = subparsers.add_parser("doctor", help="check whether interactive chat can start")
    doctor.add_argument("--model", help="model id to validate")
    doctor.add_argument("--model-config", help="local model registry JSON")
    upgrade = subparsers.add_parser("upgrade", help="install the latest or an exact GitHub release")
    upgrade.add_argument("--version", dest="upgrade_version", help="install an exact release, for example 0.2.0")
    return parser


def main() -> None:
    project_root = find_project_root()
    if (project_root / "src" / "toe_dac").is_dir():
        load_environment(project_root)
    else:
        load_environment(user_config_dir())
    parser = build_parser()
    args = parser.parse_args()
    if args.use_version:
        try:
            selected_package = package_spec(args.use_version)
        except ValueError as exc:
            parser.error(str(exc))
        environment = os.environ.copy()
        environment["TOE_DAC_UPDATE_CHECK"] = "false"
        result = subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--from",
                selected_package,
                "toe-dac",
                *forwarded_version_args(sys.argv[1:]),
            ],
            text=True,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        return
    notify_if_update_available(__version__)
    if args.version:
        if sys.stdout.isatty():
            print(f"TD Agent {__version__}")
            print(f"Python   {sys.version.split()[0]}")
            print(f"System   {platform.system()} {platform.release()} ({platform.machine()})")
        else:
            print(f"toe-dac {__version__}")
        return
    repository = TDRepository(Path(args.data))
    command = args.command or "continue"
    if command == "debug-new":
        service = TDService.create(repository, args.thread, args.retry_budget)
        print(json.dumps({
            "user_thread_id": service.context["user_thread_id"],
            "td_id": service.context["td_id"],
        }, ensure_ascii=False))
        try:
            interact(service)
        finally:
            repository.end_session(service.context)
    elif command == "new":
        thread_id = args.thread or short_id("ut")
        if repository.thread_info(thread_id):
            parser.error(f"User Thread already exists: {thread_id}; use chat --thread {thread_id} to resume it")
        config_path = model_config_path(args.model_config)
        try:
            model_id = ensure_model_ready(config_path, args.model, interactive=sys.stdin.isatty())
            adapter = TOEDACLLMAdapter(config_path, model_id)
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        controller = ConversationController.open(repository, adapter, thread_id, args.retry_budget)
        run_chat(controller, model_id)
    elif command == "open":
        service = TDService.load(repository, args.thread, args.td)
        repository.start_new_session(service.context)
        try:
            interact(service)
        finally:
            repository.end_session(service.context)
    elif command == "show":
        _summary(TDService.load(repository, args.thread, args.td))
    elif command == "case":
        registry = CaseRegistry()
        if args.case_command == "list":
            print("CASE       LEVEL  TITLE")
            for case in registry.list():
                print(f"{case.case_id:<10} {case.level:<6} {case.title}")
        else:
            case = registry.get(args.case_id)
            print(json.dumps({
                "case_id": case.case_id,
                "title": case.title,
                "level": case.level,
                "description": case.description,
                "target": case.target,
                "budgets": case.budgets,
                "fixture": str(registry.fixture_root(case)),
            }, ensure_ascii=False, indent=2))
    elif command == "run":
        runner = E2ERunner(Path(args.data))
        try:
            record = runner.run(args.case_id, args.mode, args.model, args.model_config)
        except NotImplementedError as exc:
            parser.error(str(exc))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        if record["status"] == "waiting_human":
            print(f"\n需要人工输入：{record['human_request']}")
            print(f"继续：uv run toe-dac --data {args.data} resume {record['run_id']}")
    elif command == "resume":
        response = json.loads(args.response) if args.response else None
        record = E2ERunner(Path(args.data)).resume(args.run_id, response)
        print(json.dumps(record, ensure_ascii=False, indent=2))
    elif command == "report":
        report_data = E2ERunner(Path(args.data)).report(args.run_id)
        if args.as_json:
            print(json.dumps(report_data, ensure_ascii=False, indent=2))
        else:
            print(f"Run:            {report_data['run_id']}")
            print(f"Case:           {report_data['case_id']} {report_data['title']}")
            print(f"Status:         {report_data['status']}")
            print(f"TD state:       {report_data['td_state']}")
            print(f"Target passed:  {report_data['target_passed']}")
            print(f"Actions:        {report_data['metrics']['actions']}")
            print(f"Human inputs:   {report_data['metrics']['human_interrupts']}")
            print(f"Operations:     {report_data['operation_count']}")
            print(f"Workspace:      {report_data['artifacts']['workspace']}")
    elif command in {"chat", "continue"}:
        config_path = model_config_path(getattr(args, "model_config", None))
        try:
            model_id = ensure_model_ready(
                config_path,
                getattr(args, "model", None),
                interactive=sys.stdin.isatty(),
            )
            thread_id = resolve_thread(repository, getattr(args, "thread", None))
            adapter = TOEDACLLMAdapter(config_path, model_id)
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        controller = ConversationController.open(
            repository, adapter, thread_id, getattr(args, "retry_budget", 3),
        )
        run_chat(controller, model_id)
    elif command in {"thread", "threads"}:
        thread_command = "list" if command == "threads" else args.thread_command
        if thread_command == "list":
            print("THREAD                 ROOT_TD         SESSIONS")
            for info in repository.list_threads():
                print(
                    f"{info['user_thread_id']:<22} "
                    f"{str(info.get('root_td_id') or info.get('active_td_id')):<15} "
                    f"{len(repository.list_sessions(info['user_thread_id']))}"
                )
        else:
            info = repository.thread_info(args.thread_id)
            if info is None:
                parser.error(f"unknown User Thread: {args.thread_id}")
            info["message_count"] = len(repository.message_history(args.thread_id))
            info["session_count"] = len(repository.list_sessions(args.thread_id))
            print(json.dumps(info, ensure_ascii=False, indent=2))
    elif command == "sessions":
        try:
            thread_id = resolve_thread(repository, args.thread)
        except ValueError as exc:
            parser.error(str(exc))
        if repository.thread_info(thread_id) is None:
            parser.error(f"unknown User Thread: {thread_id}")
        print("SESSION                STATUS      STARTED_AT")
        for session in repository.list_sessions(thread_id):
            print(
                f"{session['session_id']:<22} {session['status']:<11} "
                f"{session.get('started_at', '')}"
            )
    elif command == "config":
        config_path = model_config_path(args.model_config)
        try:
            if args.show or not sys.stdin.isatty():
                print_config_status(config_path)
            else:
                run_config_manager(config_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
    elif command == "doctor":
        config_path = model_config_path(args.model_config)
        try:
            model_id = ensure_model_ready(config_path, args.model, interactive=False)
            TOEDACLLMAdapter(config_path, model_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        Path(args.data).mkdir(parents=True, exist_ok=True)
        print(f"OK  model config  {config_path.resolve()}")
        print(f"OK  model         {model_id}")
        print(f"OK  data          {Path(args.data).resolve()}")
    elif command == "upgrade":
        print(f"Current version: {__version__}")
        try:
            selected_package = package_spec(args.upgrade_version)
        except ValueError as exc:
            parser.error(str(exc))
        result = subprocess.run(
            ["uv", "tool", "install", "--force", selected_package],
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        print("Installation complete. Run `toe-dac --version` to verify the installed version.")


if __name__ == "__main__":
    main()
