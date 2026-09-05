"""Structured logging: JSON lines, request/case ids from context, PII masked before it is written."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.config import settings

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)
case_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("case_id", default=None)

_PHONE_RE = re.compile(r"(?<!\d)(\+?91[\s-]?)?([6-9]\d{9})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])([A-Za-z0-9._%+-]*)(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_VPA_RE = re.compile(r"\b([a-z0-9._-]{2,})(@(?:okhdfcbank|okicici|oksbi|okaxis|paytm|ybl|upi|ibl|axl)\b)", re.I)


def mask_pii(text: str) -> str:
    """Mask Indian phone numbers, emails and UPI ids in free text."""
    if not text:
        return text
    text = _PHONE_RE.sub(lambda m: (m.group(1) or "") + m.group(2)[:2] + "******" + m.group(2)[-2:], text)
    text = _EMAIL_RE.sub(lambda m: m.group(1) + "***" + m.group(3), text)
    text = _VPA_RE.sub(lambda m: m.group(1)[0] + "***" + m.group(2), text)
    return text


class PIIMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = mask_pii(msg)
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": settings.APP_NAME,
            "env": settings.ENVIRONMENT,
        }
        rid, cid = request_id_var.get(), case_id_var.get()
        if rid:
            payload["request_id"] = rid
        if cid:
            payload["case_id"] = cid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        for key in ("event", "duration_ms", "status_code", "path", "method"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, default=str)


def configure_logging(json_logs: Optional[bool] = None, level: Optional[str] = None) -> None:
    """Idempotent root logger setup. JSON in production, human-readable in development."""
    root = logging.getLogger()
    if getattr(root, "_revenueshield_configured", False):
        return
    use_json = settings.LOG_JSON if json_logs is None else json_logs
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if use_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(PIIMaskingFilter())
    root.handlers = [handler]
    root.setLevel(getattr(logging, (level or settings.LOG_LEVEL).upper(), logging.INFO))
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._revenueshield_configured = True  # type: ignore[attr-defined]


class RequestContextMiddleware:
    """ASGI middleware: request id in/out, latency, one structured access log line, metrics."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.monotonic()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                raw.append((b"x-request-id", rid.encode()))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            path = scope.get("path", "")
            method = scope.get("method", "")
            try:
                from app.observability.metrics import metrics
                metrics.http_request(method, _route_template(scope) or path, status_holder["status"], duration_ms / 1000.0)
            except Exception:
                pass
            if not path.startswith("/metrics"):
                logging.getLogger("http.access").info(
                    f"{method} {path} -> {status_holder['status']} in {duration_ms}ms",
                    extra={"event": "http_request", "duration_ms": duration_ms, "status_code": status_holder["status"], "path": path, "method": method},
                )
            request_id_var.reset(token)


def _route_template(scope) -> Optional[str]:
    route = scope.get("route")
    return getattr(route, "path", None)
