"""Logging, request IDs, health/readiness and metrics."""
import io
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import yaml

from scrub import redact, scrub_event
from server.observability import JsonFormatter, setup_logging
from server.storage import LocalStorage
from tests.test_server import JPEG, MP4, Browser, make_app


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    dfc = logging.getLogger("dfc")
    saved = (root.handlers[:], root.level, dfc.level, dfc.handlers[:])
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    dfc.setLevel(saved[2])
    dfc.handlers[:] = saved[3]


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


def _scrape(app, token="s3cret"):
    return app.test_client().get("/metrics", headers={"Authorization": f"Bearer {token}"}).get_data(as_text=True)


def test_metrics_rejects_non_ascii_authorization_without_500():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    r = app.test_client().get("/metrics", headers={"Authorization": "Bearer é"})
    assert r.status_code == 401


def test_log_fields_and_exceptions_are_redacted():
    rec = logging.LogRecord("dfc", logging.INFO, __file__, 1, "hi", None, None)
    rec.fields = {"path": "/x?token=abc123&a=1", "key": "dfc_abcdef123456", "n": 5}
    out = json.loads(JsonFormatter().format(rec))
    assert "abc123" not in out["path"] and "dfc_abcdef" not in out["key"] and out["n"] == 5
    try:
        raise RuntimeError("POST https://api.telegram.org/bot123456:AAH-xyz/sendPhoto failed")
    except RuntimeError:
        rec = logging.LogRecord("dfc", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    assert "AAH-xyz" not in json.loads(JsonFormatter().format(rec))["exc"]


@pytest.mark.parametrize("text", ["token=abc1234", "api_key: abc1234", "secret='abc1234'", "api-key=abc1234"])
def test_redact_key_value_branches(text):
    assert "abc1234" not in redact(text)


def test_redact_leaves_ordinary_text():
    for t in ("camera started", "tokenizer ready", "events: 3"):
        assert redact(t) == t


def test_scrub_event_redacts_everything_sensitive():
    secret = "https://api.telegram.org/bot123456:AAH-xyz/sendPhoto"
    event = {
        "message": f"failed {secret}",
        "exception": {"values": [{"value": f"ConnectionError: {secret}"}]},
        "breadcrumbs": {"values": [{"message": secret, "data": {"url": secret}}]},
        "request": {"url": "https://x/y?token=abc1234", "query_string": "api_key=abc1234",
                    "cookies": {"s": "1"}, "data": {"password": "p"},
                    "headers": {"Authorization": "Bearer abc", "Accept": "*/*"}},
    }
    out = scrub_event(event)
    blob = json.dumps(out)
    assert "AAH-xyz" not in blob and "abc1234" not in blob
    assert "cookies" not in out["request"] and "data" not in out["request"]
    assert out["request"]["headers"]["Authorization"] == "[redacted]"
    assert out["request"]["headers"]["Accept"] == "*/*"


def test_unusual_http_methods_share_one_label():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    c = app.test_client()
    for m in ("FOO", "BAR"):
        c.open("/healthz", method=m)
    text = _scrape(app)
    assert 'method="OTHER"' in text and 'method="FOO"' not in text


def test_metric_labels_use_route_rule_and_unmatched():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    c = app.test_client()
    c.get("/no/such/path/123")
    c.get("/healthz")
    text = _scrape(app)
    assert 'route="unmatched"' in text and "/no/such/path" not in text
    assert 'route="/healthz"' in text


def test_histogram_is_cumulative_and_inf_equals_count():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    for _ in range(3):
        app.test_client().get("/healthz")
    text = _scrape(app)
    rows = [l for l in text.splitlines()
            if l.startswith("dfc_http_request_duration_seconds") and 'route="/healthz"' in l]
    buckets = [int(l.rsplit(" ", 1)[1]) for l in rows if "_bucket" in l]
    assert buckets == sorted(buckets)
    inf = next(int(l.rsplit(" ", 1)[1]) for l in rows if 'le="+Inf"' in l)
    count = next(int(l.rsplit(" ", 1)[1]) for l in rows if "_count" in l)
    assert inf == count == 3
    assert 'dfc_http_requests_total{method="GET",route="/healthz",status="2xx"} 3' in text


def test_db_down_and_stale_agent_gauges():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    b = Browser(app, "a@example.com")
    _, token = b.add_device()
    agent = app.test_client()
    agent.post("/api/agent/heartbeat", json={}, headers={"Authorization": f"Bearer {token}"})
    assert "dfc_agents_online 1" in _scrape(app)
    from server.models import Device
    db = app.extensions["dfc"]["db"]
    db.query(Device).update({"last_seen": time.time() - 60})
    db.commit()
    db.remove()
    text = _scrape(app)
    assert "dfc_agents_total 1" in text and "dfc_agents_online 0" in text

    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("db gone")

        def remove(self):
            pass
    real = app.extensions["dfc"]["db"]
    app.extensions["dfc"]["db"] = Broken()
    try:
        assert "dfc_db_up 0" in _scrape(app)
    finally:
        app.extensions["dfc"]["db"] = real


def test_backup_gauge_is_zero_until_a_backup_runs():
    app, tmp = make_app(METRICS_TOKEN="s3cret")
    assert "dfc_backup_last_success_timestamp 0\n" in _scrape(app)
    (tmp / "backup_last_success").write_text("")
    text = _scrape(app)
    assert "dfc_backup_last_success_timestamp 0\n" not in text
    assert "dfc_backup_last_success_timestamp 1" in text


def test_duplicate_client_id_counts_once_and_log_has_device_id():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    b = Browser(app, "a@example.com")
    dev_id, token = b.add_device()
    agent = app.test_client()
    auth = {"Authorization": f"Bearer {token}"}
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(JsonFormatter())
    logging.getLogger("dfc").addHandler(h)
    logging.getLogger("dfc").setLevel(logging.INFO)
    for _ in range(2):
        agent.post("/api/agent/events", json={"client_id": "evt-12345678"}, headers=auth)
    assert "dfc_events_ingested_total 1" in _scrape(app)
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    ev = [x for x in lines if x.get("path") == "/api/agent/events"][-1]
    assert ev["device_id"] == dev_id and ev["user_id"] == 1


def test_video_upload_failure_is_counted():
    app, _ = make_app(METRICS_TOKEN="s3cret")
    b = Browser(app, "a@example.com")
    _, token = b.add_device()
    agent = app.test_client()
    auth = {"Authorization": f"Bearer {token}"}
    agent.post("/api/agent/events", json={"client_id": "evt-12345678"}, headers=auth)

    def boom(key, f):
        raise OSError("full")
    app.extensions["dfc"]["storage"].save = boom
    r = agent.post("/api/agent/events/evt-12345678/video", headers=auth,
                   data={"file": (io.BytesIO(MP4), "v.mp4")})
    assert r.status_code == 500
    assert "dfc_upload_failures_total 1" in _scrape(app)


def test_readiness_fails_when_database_breaks_and_hides_details():
    app, _ = make_app()

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("postgresql://user:pw@host/db unreachable")

        def remove(self):
            pass
    app.extensions["dfc"]["db"] = Broken()
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(JsonFormatter())
    logging.getLogger("dfc").addHandler(h)
    r = app.test_client().get("/readyz")
    assert r.status_code == 503 and r.get_json()["checks"]["database"] == "failed"
    assert "pw@host" not in stream.getvalue() and "RuntimeError" in stream.getvalue()


def test_readiness_result_is_cached_briefly():
    app, _ = make_app()
    calls = []
    real = app.extensions["dfc"]["storage"].check
    app.extensions["dfc"]["storage"].check = lambda: (calls.append(1), real())
    c = app.test_client()
    c.get("/readyz")
    c.get("/readyz")
    assert len(calls) == 1


def test_local_storage_check_leaves_no_file(tmp_path):
    LocalStorage(tmp_path).check()
    assert list(tmp_path.iterdir()) == []


def test_s3_check_calls_head_bucket():
    from server.storage import S3Storage
    s = S3Storage.__new__(S3Storage)
    s.bucket = "b"
    calls = []
    s.s3 = type("C", (), {"head_bucket": lambda self, **kw: calls.append(kw)})()
    s.check()
    assert calls == [{"Bucket": "b"}]


def test_unknown_route_stays_404_not_500():
    app, _ = make_app()
    assert app.test_client().get("/definitely/not/here").status_code == 404


def test_setup_logging_bad_level_and_plain_mode():
    setup_logging("nonsense", json_logs=False)
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    setup_logging("debug")
    assert root.level == logging.DEBUG and isinstance(root.handlers[0].formatter, JsonFormatter)


def test_config_parses_observability_env(monkeypatch):
    from server.config import Config
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_JSON", "0")
    monkeypatch.setenv("METRICS_TOKEN", "t")
    monkeypatch.setenv("SECRET_KEY", "k")
    c = Config()
    assert (c.LOG_LEVEL, c.LOG_JSON, c.METRICS_TOKEN) == ("DEBUG", False, "t")


def test_logged_exception_with_bot_url_does_not_leak_token():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(JsonFormatter())
    log = logging.getLogger("dfc")
    log.addHandler(h)
    log.setLevel(logging.INFO)
    try:
        raise ConnectionError("POST https://api.telegram.org/bot123456:AAH-xyz/sendMessage failed")
    except ConnectionError:
        log.exception("telegram send failed")
    assert "AAH-xyz" not in stream.getvalue()


def test_alert_rules_only_reference_exported_metrics():
    import re
    rules = yaml.safe_load((Path(__file__).resolve().parent.parent / "deploy/monitoring/alerts.yml").read_text())
    app, _ = make_app(METRICS_TOKEN="s3cret")
    app.test_client().get("/healthz")
    text = _scrape(app)
    exported = set(re.findall(r"^# TYPE (\w+)", text, re.M))
    for group in rules["groups"]:
        for rule in group["rules"]:
            for name in re.findall(r"\b(dfc_\w+)", rule["expr"]):
                base = re.sub(r"_(bucket|sum|count)$", "", name)
                assert base in exported, f"{rule['alert']} uses unknown metric {name}"


def test_component_names_the_logger():
    from server.observability import component
    assert component("dfc.telegram") == "telegram"
    assert component("dfc.http") == "http"
    assert component("dfc") == "app"
    assert component("sqlalchemy.engine") == "app"
