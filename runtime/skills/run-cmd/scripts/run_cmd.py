#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo


JOB_ID = re.compile(r"^cmd-[a-f0-9]{8}$")
MAX_CHUNK = 32_000


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def emit(value: dict, exit_code: int = 0) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    raise SystemExit(exit_code)


def state_root() -> Path:
    configured = os.environ.get("TOE_DAC_SKILL_STATE_DIR", "").strip()
    if not configured:
        emit({"ok": False, "error": "TOE_DAC_SKILL_STATE_DIR is not configured"}, 2)
    root = Path(configured).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def job_directory(job_id: str) -> Path:
    if not JOB_ID.fullmatch(job_id):
        emit({"ok": False, "error": "invalid job_id"}, 2)
    return state_root() / job_id


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_chunk(path: Path, offset: int) -> tuple[str, int, bool]:
    if not path.is_file():
        return "", 0, False
    size = path.stat().st_size
    start = min(max(offset, 0), size)
    with path.open("rb") as handle:
        handle.seek(start)
        content = handle.read(MAX_CHUNK)
    next_offset = start + len(content)
    return content.decode("utf-8", errors="replace"), next_offset, next_offset < size


def start(args: argparse.Namespace) -> None:
    root = state_root()
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    if not cwd.is_dir():
        emit({"ok": False, "error": f"working directory does not exist: {cwd}"}, 2)
    job_id = f"cmd-{uuid.uuid4().hex[:8]}"
    directory = root / job_id
    directory.mkdir(mode=0o700)
    metadata = {
        "job_id": job_id,
        "status": "starting",
        "command": args.command,
        "cwd": str(cwd),
        "created_at": now(),
    }
    write_json(directory / "meta.json", metadata)
    worker = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            "--job-dir",
            str(directory),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
    metadata.update({"status": "running", "supervisor_pid": worker.pid, "started_at": now()})
    write_json(directory / "meta.json", metadata)
    emit({
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "supervisor_pid": worker.pid,
        "cwd": str(cwd),
    })


def worker(args: argparse.Namespace) -> None:
    directory = Path(args.job_dir).resolve()
    root = state_root()
    try:
        directory.relative_to(root)
    except ValueError:
        raise SystemExit(2)
    metadata_path = directory / "meta.json"
    metadata = read_json(metadata_path)
    if not metadata:
        raise SystemExit(2)
    with (directory / "stdout.log").open("ab", buffering=0) as stdout_handle, (
        directory / "stderr.log"
    ).open("ab", buffering=0) as stderr_handle:
        process = subprocess.Popen(
            ["bash", "-lc", str(metadata["command"])],
            cwd=str(metadata["cwd"]),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            close_fds=True,
        )
        metadata.update({
            "supervisor_pid": os.getpid(),
            "command_pid": process.pid,
            "status": "running",
        })
        write_json(metadata_path, metadata)
        exit_code = process.wait()
    result = {
        "job_id": metadata["job_id"],
        "status": "completed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "finished_at": now(),
    }
    write_json(directory / "result.json", result)


def status(args: argparse.Namespace) -> None:
    directory = job_directory(args.job_id)
    metadata = read_json(directory / "meta.json")
    if not metadata:
        emit({"ok": False, "error": f"job not found: {args.job_id}"}, 2)
    result = read_json(directory / "result.json")
    killed = read_json(directory / "killed.json")
    if result:
        current_status = result["status"]
        exit_code = result.get("exit_code")
    elif killed:
        current_status = "killed"
        exit_code = None
    else:
        pid = int(metadata.get("supervisor_pid", 0))
        if pid <= 1:
            emit({"ok": False, "job_id": args.job_id, "status": "lost", "error": "invalid supervisor pid"}, 1)
        try:
            os.kill(pid, 0)
            current_status = "running"
        except (OSError, ValueError):
            current_status = "lost"
        exit_code = None
    stdout, stdout_offset, stdout_more = read_chunk(directory / "stdout.log", args.stdout_offset)
    stderr, stderr_offset, stderr_more = read_chunk(directory / "stderr.log", args.stderr_offset)
    emit({
        "ok": current_status not in {"failed", "lost"},
        "job_id": args.job_id,
        "status": current_status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_offset": stdout_offset,
        "stderr_offset": stderr_offset,
        "stdout_more": stdout_more,
        "stderr_more": stderr_more,
    }, 0 if current_status not in {"failed", "lost"} else 1)


def kill(args: argparse.Namespace) -> None:
    directory = job_directory(args.job_id)
    metadata = read_json(directory / "meta.json")
    if not metadata:
        emit({"ok": False, "error": f"job not found: {args.job_id}"}, 2)
    if (directory / "result.json").is_file():
        emit({"ok": True, "job_id": args.job_id, "status": "already_finished"})
    pid = int(metadata.get("supervisor_pid", 0))
    if pid <= 1:
        emit({"ok": False, "job_id": args.job_id, "error": "invalid supervisor pid"}, 1)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (OSError, ValueError) as exc:
        emit({"ok": False, "job_id": args.job_id, "error": str(exc)}, 1)
    write_json(directory / "killed.json", {
        "job_id": args.job_id,
        "status": "killed",
        "killed_at": now(),
    })
    emit({"ok": True, "job_id": args.job_id, "status": "killed"})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Persistent asynchronous Bash job runner")
    commands = root.add_subparsers(dest="action", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--command", required=True)
    start_parser.add_argument("--cwd")
    start_parser.set_defaults(handler=start)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--job-id", required=True)
    status_parser.add_argument("--stdout-offset", type=int, default=0)
    status_parser.add_argument("--stderr-offset", type=int, default=0)
    status_parser.set_defaults(handler=status)
    kill_parser = commands.add_parser("kill")
    kill_parser.add_argument("--job-id", required=True)
    kill_parser.set_defaults(handler=kill)
    worker_parser = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--job-dir", required=True)
    worker_parser.set_defaults(handler=worker)
    return root


def main() -> None:
    arguments = parser().parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
