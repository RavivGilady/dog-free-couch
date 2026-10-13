"""Logging, request IDs, health/readiness and metrics."""
import io
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.observability import JsonFormatter, redact
from tests.test_server import JPEG, Browser, make_app


def test_redacts_secrets():
    assert "dfc_abcdef123456" not in redact("token dfc_abcdef123456 used")
    assert "hunter22" not in redact('{"password": "hunter22"}')
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    assert "AAH-xyz" not in redact("POST /bot123456:AAH-xyz/sendPhoto")


def test_json_log_line_has_request_and_user_ids():
    app, _ = make_app()
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(JsonFormatter())
    logging.getLogger("dfc").addHandler(h)
    logging.getLogger("dfc").setLevel(logging.INFO)
    try:
        b = Browser(app, "a@example.com")
        r = b.req("get", "/api/auth/me", headers={"X-Request-ID": "trace-12345678"})
        assert r.headers["X-Request-ID"] == "trace-12345678"
    finally:
        logging.getLogger("dfc").removeHandler(h)
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    me = [x for x in lines if x.get("path") == "/api/auth/me"][-1]
    assert me["request_id"] == "trace-12345678" and me["user_id"] == 1
    assert me["status"] == 200 and "ms" in me


def test_bad_request_id_is_replaced():
    app, _ = make_app()
    r = app.test_client().get("/healthz", headers={"X-Request-ID": "bad id!"})
    assert len(r.headers["X-Request-ID"]) == 32


def test_liveness_and_readiness():
    app, _ = make_app()
    c = app.test_client()
    assert c.get("/healthz").get_json() == {"ok": True}
    r = c.get("/readyz")
    assert r.status_code == 200 and r.get_json()["checks"] == {"database": "ok", "storage": "ok"}


def test_readiness_fails_when_storage_breaks():
    app, _ = make_app()
    def boom():
        raise OSError("disk gone")
    app.extensions["dfc"]["storage"].check = boom
    r = app.test_client().get("/readyz")
    assert r.status_code == 503 and r.get_json()["checks"]["storage"] == "failed"
    assert app.test_client().get("/healthz").status_code == 200  # liveness unaffected


def test_metrics_disabled_without_token_and_requires_it():
    app, _ = make_app()
    assert app.test_client().get("/metrics").status_code == 404
    app, _ = make_app(METRICS_TOKEN="s3cret")
    c = app.test_client()
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_content():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    b = Browser(app, "a@example.com")
    _, token = b.add_device()
    agent = app.test_client()
    auth = {"Authorization": f"Bearer {token}"}
    agent.post("/api/agent/heartbeat", json={}, headers=auth)
    agent.post("/api/agent/events", json={"client_id": "evt-12345678"}, headers=auth)

    def boom(key, f):
        raise OSError("full")
    app.extensions["dfc"]["storage"].save = boom
    r = agent.post("/api/agent/events/evt-12345678/snapshot", headers=auth,
                   data={"file": (io.BytesIO(JPEG), "s.jpg")})
    assert r.status_code == 500 and "request_id" in r.get_json()

    text = app.test_client().get("/metrics", headers={"Authorization": "Bearer s3cret"}).get_data(as_text=True)
    assert "dfc_agents_online 1" in text and "dfc_agents_total 1" in text
    assert "dfc_events_ingested_total 1" in text
    assert "dfc_upload_failures_total 1" in text
    assert 'status="5xx"' in text and "dfc_disk_used_ratio" in text
    assert "dfc_db_up 1" in text
