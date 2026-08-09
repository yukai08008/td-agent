from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class AccessLogger:
    """Seven-day operational access log without message or credential content."""

    def __init__(self, log_directory: str | Path):
        path = Path(log_directory) / "access.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.logger = logging.getLogger(f"toe_dac.access.{path.resolve()}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = TimedRotatingFileHandler(
                path,
                when="midnight",
                interval=1,
                backupCount=7,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s request_id=%(request_id)s "
                "user_thread=%(user_thread_id)s session=%(session_id)s "
                "type=%(access_type)s duration_ms=%(duration_ms).1f status=%(status)s"
            ))
            self.logger.addHandler(handler)

    def record(
        self,
        *,
        request_id: str,
        user_thread_id: str,
        session_id: str,
        access_type: str,
        duration_ms: float,
        status: str = "ok",
    ) -> None:
        self.logger.info("access", extra={
            "request_id": request_id,
            "user_thread_id": user_thread_id,
            "session_id": session_id,
            "access_type": access_type,
            "duration_ms": duration_ms,
            "status": status,
        })
        for handler in self.logger.handlers:
            handler.flush()
