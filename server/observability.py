"""
Logging, error tracking and metrics.

* Logs are one JSON object per line on stdout (the container runtime ships
  and rotates them), each carrying the request ID and the user/device that
  made the request. Anything that looks like a secret is redacted.
* Sentry is optional: set SENTRY_DSN. Events are tagged with RELEASE.
* /metrics speaks the Prometheus text format with no extra dependency. It
  is off unless METRICS_TOKEN is set, and then needs that bearer token.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from werkzeug.exceptions import HTTPException

from flask import Flask, current_app, g, has_request_context, jsonify, request

from scrub import redact, scrub_event

log = logging.getLogger("dfc")
access_log = logging.getLogger("dfc.http")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# Successful calls on these paths happen every few seconds per camera/viewer.
_NOISY_PATHS = ("/healthz", "/readyz", "/api/agent/heartbeat", "/api/agent/frame")
_READY_TTL = 5.0


def component(logger_name: str) -> str:
    """Which part of the app logged this: "dfc.telegram" -> "telegram".
    Render has no log groups and only labels logs by type/level/instance,
    so this field is what you filter on there (text: "component": "http").
    """
    return logger_name.removeprefix("dfc.") if logger_name.startswith("dfc.") else "app"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "component": component(record.name),
            "msg": redact(record.getMessage()),
        }
        if has_request_context():
            out["request_id"] = getattr(g, "request_id", None)
            # Cached on g at authentication time: touching ORM objects or the
            # session from inside a log call can trigger queries and cookies.
            out["user_id"] = g.get("log_user_id")
            if g.get("log_device_id") is not None:
                out["device_id"] = g.log_device_id
        for k, v in getattr(record, "fields", {}).items():
            out[k] = redact(v) if isinstance(v, str) else v
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper() if level.upper() in logging._nameToLevel else "INFO")
    # Gunicorn's access log would duplicate ours.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, str], int] = {}
        self.latency: dict[str, list] = {}  # route -> [bucket counts..., sum, count]
        self.counters: dict[str, int] = {"events_ingested_total": 0,
                                         "upload_failures_total": 0}

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        # Clients can send arbitrary method tokens; keep label cardinality bounded.
        key = (method if method in _METHODS else "OTHER", route, f"{status // 100}xx")
        with self._lock:
            self.requests[key] = self.requests.get(key, 0) + 1
            h = self.latency.setdefault(route, [0] * (len(_LATENCY_BUCKETS) + 1) + [0.0, 0])
            for i, b in enumerate(_LATENCY_BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[len(_LATENCY_BUCKETS)] += 1  # +Inf
            h[-2] += seconds
            h[-1] += 1

    def render(self, gauges: dict[str, float]) -> str:
        lines = []
        with self._lock:
            lines += ["# TYPE dfc_http_requests_total counter"]
            for (m, r, s), n in sorted(self.requests.items()):
                lines.append(f'dfc_http_requests_total{{method="{m}",route="{r}",status="{s}"}} {n}')
            lines += ["# TYPE dfc_http_request_duration_seconds histogram"]
            for route, h in sorted(self.latency.items()):
                for i, b in enumerate(_LATENCY_BUCKETS):
                    lines.append(f'dfc_http_request_duration_seconds_bucket{{route="{route}",le="{b}"}} {h[i]}')
                lines.append(f'dfc_http_request_duration_seconds_bucket{{route="{route}",le="+Inf"}} {h[len(_LATENCY_BUCKETS)]}')
                lines.append(f'dfc_http_request_duration_seconds_sum{{route="{route}"}} {h[-2]:.6f}')
                lines.append(f'dfc_http_request_duration_seconds_count{{route="{route}"}} {h[-1]}')
            for name, v in sorted(self.counters.items()):
                lines += [f"# TYPE dfc_{name} counter", f"dfc_{name} {v}"]
        for name, v in gauges.items():
            lines += [f"# TYPE dfc_{name} gauge", f"dfc_{name} {v}"]
        return "\n".join(lines) + "\n"


def metrics() -> Metrics:
    return current_app.extensions["dfc"]["metrics"]


def _gauges(app: Flask) -> dict[str, float]:
    from sqlalchemy import func

    from .models import ONLINE_WINDOW, Device
    ext = app.extensions["dfc"]
    cfg = app.config["DFC"]
    out: dict[str, float] = {}
    try:
        now = time.time()
        q = ext["db"].query(func.count(Device.id))
        out["agents_total"] = q.scalar()
        out["agents_online"] = q.filter(Device.last_seen > now - ONLINE_WINDOW).scalar()
        out["db_up"] = 1
    except Exception:
        log.exception("metrics: database query failed")
        out["db_up"] = 0
    finally:
        ext["db"].remove()
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        du = shutil.disk_usage(cfg.DATA_DIR)
        out["disk_used_ratio"] = round(du.used / du.total, 4)
        out["disk_free_bytes"] = du.free
    except Exception:
        log.exception("metrics: disk usage failed")
    # A backup job touches this file on success. 0 means "never", so the
    # BackupStale alert fires instead of staying silent on an absent series.
    marker = Path(cfg.DATA_DIR) / "backup_last_success"
    out["backup_last_success_timestamp"] = int(marker.stat().st_mtime) if marker.exists() else 0
    return out


# --------------------------------------------------------------------------
# sentry
# --------------------------------------------------------------------------

def init_sentry(dsn: str, release: str, component: str) -> bool:
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return False

    sentry_sdk.init(dsn=dsn, release=release or None, send_default_pii=False,
                    before_send=scrub_event, traces_sample_rate=0)
    sentry_sdk.set_tag("component", component)
    return True


# --------------------------------------------------------------------------
# flask wiring
# --------------------------------------------------------------------------

def init_app(app: Flask) -> None:
    cfg = app.config["DFC"]
    app.extensions["dfc"]["metrics"] = Metrics()
    setup_logging(cfg.LOG_LEVEL, cfg.LOG_JSON)
    init_sentry(cfg.SENTRY_DSN, cfg.RELEASE, "server")

    @app.before_request
    def _start():
        sent = request.headers.get("X-Request-ID", "")
        g.request_id = sent if _REQUEST_ID.match(sent) else uuid.uuid4().hex
        g.t0 = time.perf_counter()

    @app.after_request
    def _finish(resp):
        resp.headers["X-Request-ID"] = getattr(g, "request_id", "")
        t0 = getattr(g, "t0", None)
        if t0 is not None and request.endpoint != "static":
            secs = time.perf_counter() - t0
            route = request.url_rule.rule if request.url_rule else "unmatched"
            app.extensions["dfc"]["metrics"].observe(request.method, route, resp.status_code, secs)
            level = logging.ERROR if resp.status_code >= 500 else logging.INFO
            if resp.status_code < 400 and request.path in _NOISY_PATHS:
                level = logging.DEBUG
            access_log.log(level, "request", extra={"fields": {
                "method": request.method, "path": request.path,
                "status": resp.status_code, "ms": round(secs * 1000, 1)}})
        return resp

    @app.errorhandler(Exception)
    def _unhandled(exc):
        if isinstance(exc, HTTPException):
            return exc
        log.exception("unhandled exception")
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except ImportError:
            pass
        resp = jsonify({"error": "internal error", "request_id": g.get("request_id")})
        resp.status_code = 500
        return resp

    @app.get("/healthz")
    def healthz():
        """Liveness: the process is up and serving. Deliberately cheap and
        dependency-free, so a flaky database cannot get the app restarted."""
        return jsonify({"ok": True})

    ready_cache = {"at": 0.0, "checks": None}

    @app.get("/readyz")
    def readyz():
        """Readiness: can we actually do our job (database + media store)?
        Unauthenticated, so the result is cached for a few seconds."""
        from sqlalchemy import text
        now = time.monotonic()
        if ready_cache["checks"] is None or now - ready_cache["at"] > _READY_TTL:
            ext = app.extensions["dfc"]
            checks = {}
            try:
                ext["db"].execute(text("SELECT 1"))
                checks["database"] = "ok"
            except Exception as e:
                # Class only: the message can carry the DSN or host.
                log.error("readiness: database check failed: %s", type(e).__name__)
                checks["database"] = "failed"
            finally:
                ext["db"].remove()
            try:
                ext["storage"].check()
                checks["storage"] = "ok"
            except Exception as e:
                log.error("readiness: storage check failed: %s", type(e).__name__)
                checks["storage"] = "failed"
            ready_cache.update(at=now, checks=checks)
        checks = ready_cache["checks"]
        ok = all(v == "ok" for v in checks.values())
        return jsonify({"ok": ok, "checks": checks}), (200 if ok else 503)

    @app.get("/metrics")
    def metrics_endpoint():
        token = cfg.METRICS_TOKEN
        if not token:
            return jsonify({"error": "not found"}), 404
        sent = request.headers.get("Authorization", "")
        # Bytes: compare_digest raises TypeError on a non-ASCII str.
        if not secrets.compare_digest(sent.encode(), f"Bearer {token}".encode()):
            return jsonify({"error": "unauthorized"}), 401
        body = app.extensions["dfc"]["metrics"].render(_gauges(app))
        return body, 200, {"Content-Type": "text/plain; version=0.0.4"}
