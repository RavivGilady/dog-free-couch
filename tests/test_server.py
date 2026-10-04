"""Server API tests: accounts, devices, the agent upload path, and -- most
importantly -- that one user can never reach another user's data.

Runs against a throwaway SQLite db and media dir; no camera, no network.
"""
import io
import json
import shutil
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import create_app, prune_old_events
from server.config import Config

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000)
    return buf.getvalue()


def make_app(**overrides):
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(DATA_DIR=tmp, DATABASE_URL=f"sqlite:///{tmp / 'test.db'}",
                 MEDIA_DIR=tmp / "media", SECRET_KEY="test", STORAGE="local",
                 RETENTION_DAYS=30, ALLOW_SIGNUP=True, **overrides)
    return create_app(cfg, start_background=False), tmp


class Browser:
    """A signed-in user: test client + CSRF header on every request."""

    def __init__(self, app, email):
        self.c = app.test_client()
        r = self.c.post("/api/auth/register", json={"email": email, "password": "password123"})
        assert r.status_code == 201, r.get_json()
        self.csrf = r.get_json()["csrf_token"]

    def req(self, method, url, **kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
        return getattr(self.c, method)(url, **kw)

    def add_device(self, name="Living room"):
        r = self.req("post", "/api/devices", json={"name": name})
        assert r.status_code == 201
        body = r.get_json()
        return body["device"]["id"], body["token"]


class Agent:
    def __init__(self, app, token):
        self.c = app.test_client()
        self.h = {"Authorization": f"Bearer {token}"}

    def post(self, url, headers=None, **kw):
        return self.c.post("/api/agent" + url, headers={**self.h, **(headers or {})}, **kw)

    def get(self, url, **kw):
        return self.c.get("/api/agent" + url, headers=self.h, **kw)

    def full_event(self, client_id, start=None):
        start = start or time.time()
        assert self.post("/events", json={"client_id": client_id, "start_ts": start,
                                          "confidence": 0.9}).status_code == 200
        assert self.post(f"/events/{client_id}/snapshot",
                         data={"file": (io.BytesIO(JPEG), "s.jpg")}).status_code == 200
        assert self.post(f"/events/{client_id}/video",
                         data={"file": (io.BytesIO(MP4), "clip.mp4")}).status_code == 200
        assert self.post(f"/events/{client_id}/end",
                         json={"end_ts": start + 12, "duration": 12}).status_code == 200


def test_auth_flow_and_csrf():
    app, tmp = make_app()
    try:
        c = app.test_client()
        assert c.get("/api/auth/me").status_code == 401
        assert c.post("/api/auth/register", json={"email": "bad", "password": "password123"}).status_code == 400
        assert c.post("/api/auth/register", json={"email": "a@x.io", "password": "short"}).status_code == 400

        b = Browser(app, "a@x.io")
        assert b.c.get("/api/auth/me").get_json()["user"]["email"] == "a@x.io"
        # Mutations without the CSRF header are refused even with a session.
        assert b.c.post("/api/devices", json={"name": "x"}).status_code == 403
        # Duplicate signup refused.
        assert app.test_client().post("/api/auth/register",
                                      json={"email": "A@x.io", "password": "password123"}).status_code == 409

        c2 = app.test_client()
        assert c2.post("/api/auth/login", json={"email": "a@x.io", "password": "nope"}).status_code == 401
        assert c2.post("/api/auth/login", json={"email": "a@x.io", "password": "password123"}).status_code == 200
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_signup_can_be_closed():
    app, tmp = make_app()
    try:
        app.config["DFC"].ALLOW_SIGNUP = False
        r = app.test_client().post("/api/auth/register",
                                   json={"email": "a@x.io", "password": "password123"})
        assert r.status_code == 403
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_upload_path_and_serving():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, token = b.add_device()
        agent = Agent(app, token)

        assert Agent(app, "dfc_wrong").post("/heartbeat", json={}).status_code == 401

        hb = agent.post("/heartbeat", json={"status": {"running": True, "fps": 12.5}}).get_json()
        assert hb["device"]["id"] == dev_id and hb["live_wanted"] is False
        assert b.c.get("/api/devices").get_json()["devices"][0]["online"] is True

        agent.full_event("evt_000000001")
        # Retrying a create is idempotent.
        agent.post("/events", json={"client_id": "evt_000000001", "start_ts": 1})

        events = b.c.get(f"/api/events?device_id={dev_id}").get_json()["events"]
        assert len(events) == 1
        ev = events[0]
        assert ev["has_snapshot"] and ev["has_video"] and ev["duration"] == 12

        with b.c.get(f"/api/events/{ev['id']}/snapshot") as r:
            assert r.data == JPEG
        with b.c.get(f"/api/events/{ev['id']}/video", headers={"Range": "bytes=0-9"}) as r:
            assert r.status_code == 206 and len(r.data) == 10

        stats = b.c.get("/api/stats?since=0").get_json()
        assert stats["total_alerts"] == 1 and stats["longest_session_sec"] == 12

        # Deleting the event removes its media from disk too.
        media = list((tmp / "media").rglob("*.*"))
        assert len(media) == 2
        assert b.req("delete", f"/api/events/{ev['id']}").status_code == 200
        assert not any(p.exists() for p in media)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_users_are_isolated():
    app, tmp = make_app()
    try:
        alice, bob = Browser(app, "alice@x.io"), Browser(app, "bob@x.io")
        a_dev, a_token = alice.add_device()
        b_dev, b_token = bob.add_device()
        Agent(app, a_token).full_event("alice_evt_01")
        a_event = alice.c.get("/api/events").get_json()["events"][0]["id"]

        assert bob.c.get("/api/events").get_json()["events"] == []
        assert bob.c.get(f"/api/events?device_id={a_dev}").get_json()["events"] == []
        assert bob.c.get(f"/api/events/{a_event}/video").status_code == 404
        assert bob.c.get(f"/api/events/{a_event}/snapshot").status_code == 404
        assert bob.req("delete", f"/api/events/{a_event}").status_code == 404
        assert bob.req("delete", f"/api/devices/{a_dev}").status_code == 404
        assert bob.req("post", f"/api/devices/{a_dev}/token").status_code == 404
        assert bob.req("post", f"/api/devices/{a_dev}/settings", json={}).status_code == 404
        assert bob.c.get(f"/api/devices/{a_dev}/stream.mjpg").status_code == 404
        assert bob.c.get(f"/api/stats?device_id={a_dev}&since=0").get_json()["total_alerts"] == 0

        # Bob's agent can't attach media to (or end) Alice's event either:
        # client ids are scoped per device.
        bob_agent = Agent(app, b_token)
        assert bob_agent.post("/events/alice_evt_01/end", json={"duration": 1}).status_code == 404

        # Nor can a sound of Alice's be used on Bob's camera.
        r = alice.req("post", "/api/sounds", data={"audio": (io.BytesIO(_wav()), "a.wav"), "label": "hey"})
        sound_id = r.get_json()["sound"]["id"]
        assert bob.req("post", f"/api/devices/{b_dev}/settings",
                       json={"alert": {"active_sound": sound_id}}).status_code == 404
        assert bob_agent.get(f"/sounds/{sound_id}").status_code == 404
        assert bob.c.get(f"/api/sounds/{sound_id}/audio").status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_settings_commands_and_sounds_reach_agent():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, token = b.add_device()
        agent = Agent(app, token)

        wav = _wav()
        r = b.req("post", "/api/sounds", data={"audio": (io.BytesIO(wav), "r.wav"), "label": "No couch!"})
        assert r.status_code == 201
        sound_id = r.get_json()["sound"]["id"]
        assert b.req("post", "/api/sounds", data={"audio": (io.BytesIO(b"nope"), "r.wav")}).status_code == 400

        r = b.req("post", f"/api/devices/{dev_id}/settings", json={
            "alert": {"repeat_sound_sec": 9999, "active_sound": sound_id},
            "video": {"pre_roll_sec": 7, "bogus": 1},
        })
        assert r.status_code == 200
        b.req("post", f"/api/devices/{dev_id}/commands", json={"type": "test_sound"})

        hb = agent.post("/heartbeat", json={}).get_json()
        assert hb["settings"]["alert"]["repeat_sound_sec"] == 600  # clamped
        assert hb["settings"]["video"]["pre_roll_sec"] == 7
        assert "bogus" not in hb["settings"]["video"]
        assert hb["active_sound"] == {"id": sound_id, "size": len(wav)}
        assert [c["type"] for c in hb["commands"]] == ["test_sound"]
        assert agent.post("/heartbeat", json={}).get_json()["commands"] == []  # drained
        assert agent.get(f"/sounds/{sound_id}").data == wav

        # Deleting the active sound falls the camera back to the built-in alarm.
        b.req("delete", f"/api/sounds/{sound_id}")
        hb = agent.post("/heartbeat", json={}).get_json()
        assert hb["settings"]["alert"]["active_sound"] == "builtin" and hb["active_sound"] is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_builtin_sirens_are_listed_and_selectable():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, token = b.add_device()
        agent = Agent(app, token)

        names = [s["id"] for s in b.c.get("/api/sounds/builtins").get_json()["sounds"]]
        assert "builtin" in names and len(names) > 1
        assert b.c.get("/api/sounds/builtin/wail/audio").data.startswith(b"RIFF")
        assert b.c.get("/api/sounds/builtin/audio").data.startswith(b"RIFF")
        # Not a route the filesystem can be walked through.
        assert b.c.get("/api/sounds/builtin/nope/audio").status_code == 404
        assert app.test_client().get("/api/sounds/builtins").status_code == 401

        # A siren is stored by name, and the agent gets no recording to
        # download -- it synthesizes the siren itself.
        assert b.req("post", f"/api/devices/{dev_id}/settings",
                     json={"alert": {"active_sound": "yelp"}}).status_code == 200
        hb = agent.post("/heartbeat", json={}).get_json()
        assert hb["settings"]["alert"]["active_sound"] == "yelp"
        assert hb["active_sound"] is None

        # An unknown name is not a siren and not a sound id of this user's.
        assert b.req("post", f"/api/devices/{dev_id}/settings",
                     json={"alert": {"active_sound": "../etc/passwd"}}).status_code == 400
        assert agent.post("/heartbeat", json={}).get_json()[
            "settings"]["alert"]["active_sound"] == "yelp"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_zone_drawn_in_the_dashboard_reaches_the_agent():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, token = b.add_device()
        agent = Agent(app, token)

        # No zone set here yet: the agent keeps using its own config.yaml.
        hb = agent.post("/heartbeat", json={}).get_json()
        assert hb["settings"]["zone"]["points"] == []

        r = b.req("post", f"/api/devices/{dev_id}/settings", json={
            "zone": {"points": [[0.1, 0.2], [0.9, 0.2], [1.4, 0.95], [0.1, -0.3]],
                     "overlap_threshold": 0.9},
        })
        assert r.status_code == 200
        hb = agent.post("/heartbeat", json={}).get_json()
        # Out-of-frame corners are clamped, not rejected: a corner dragged a
        # hair past the edge of the video is still what the user meant.
        assert hb["settings"]["zone"]["points"] == [[0.1, 0.2], [0.9, 0.2],
                                                    [1.0, 0.95], [0.1, 0.0]]
        assert hb["settings"]["zone"]["overlap_threshold"] == 0.9

        # A polygon that encloses nothing, or is absurdly large, is refused --
        # as is a threshold outside its range (clamped).
        for bad in ([[0.1, 0.1], [0.5, 0.5]], [[0.1]], "nope", [[0.1, 0.1]] * 25):
            assert b.req("post", f"/api/devices/{dev_id}/settings",
                         json={"zone": {"points": bad}}).status_code == 400
        b.req("post", f"/api/devices/{dev_id}/settings",
              json={"zone": {"overlap_threshold": 0}})
        hb = agent.post("/heartbeat", json={}).get_json()
        assert hb["settings"]["zone"]["overlap_threshold"] == 0.05
        assert len(hb["settings"]["zone"]["points"]) == 4  # unchanged by the above

        # The zone the camera reports using comes back to the dashboard, so an
        # existing calibrate.py zone can be edited instead of redrawn.
        agent.post("/heartbeat", json={"status": {"running": True,
                                                  "zone_points": [[0.0, 0.5]]}})
        dev = b.req("get", "/api/devices").get_json()["devices"][0]
        assert dev["status"]["zone_points"] == [[0.0, 0.5]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_token_rotation_revokes_old_token():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, old = b.add_device()
        new = b.req("post", f"/api/devices/{dev_id}/token").get_json()["token"]
        assert Agent(app, old).post("/heartbeat", json={}).status_code == 401
        assert Agent(app, new).post("/heartbeat", json={}).status_code == 200
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_browser_camera_runs_on_the_same_agent_api():
    """A camera station is a dashboard tab, so it gets its token over the
    session it is already signed in with -- and then looks like any agent."""
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        r = b.req("post", "/api/devices", json={"name": "Laptop", "kind": "browser"})
        assert r.status_code == 201
        body = r.get_json()
        dev_id = body["device"]["id"]
        assert body["device"]["kind"] == "browser"
        # Nothing to paste into a terminal: the page that asked runs it.
        assert "agent_cmd" not in body

        first = b.req("post", f"/api/devices/{dev_id}/browser-token").get_json()["token"]
        agent = Agent(app, first)
        agent.full_event("browser-clip-1")
        assert b.req("get", "/api/events").get_json()["events"][0]["has_video"]

        # Starting the same camera in a second tab retires the first tab's
        # token, so only one station ever drives one camera.
        second = b.req("post", f"/api/devices/{dev_id}/browser-token").get_json()["token"]
        assert second != first
        assert agent.post("/heartbeat", json={}).status_code == 401
        assert Agent(app, second).post("/heartbeat", json={}).status_code == 200

        # An agent camera has no browser token to hand out.
        agent_id, _ = b.add_device("Living room")
        assert b.req("get", "/api/devices").get_json()["devices"][1]["kind"] == "agent"
        assert b.req("post", f"/api/devices/{agent_id}/browser-token").status_code == 400
        assert b.req("post", "/api/devices", json={"kind": "toaster"}).status_code == 400
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_station_logs_are_shared_only_on_a_dev_run():
    """The "Share logs" button is developer tooling: on a normal deployment
    the endpoint behind it isn't there at all."""
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, _ = b.add_device("Laptop")
        assert b.req("get", "/api/auth/me").get_json()["dev"] is False
        r = b.req("post", f"/api/devices/{dev_id}/logs",
                  json={"lines": [{"t": 1.0, "level": "info", "msg": "hi"}]})
        assert r.status_code == 404
        assert not (tmp / "station-logs").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_station_logs_are_written_and_sanitized_in_dev_mode():
    app, tmp = make_app(DEV_MODE=True)
    try:
        b = Browser(app, "a@x.io")
        dev_id, _ = b.add_device("Laptop")
        assert b.req("get", "/api/auth/me").get_json()["dev"] is True

        lines = [{"t": 1.5, "level": "info", "msg": "camera open"},
                 {"t": 2.0, "level": "error", "msg": "x" * 900},
                 # Unknown level and a missing timestamp both degrade rather
                 # than failing the whole report.
                 {"level": "shout", "msg": "odd"}]
        r = b.req("post", f"/api/devices/{dev_id}/logs",
                  json={"lines": lines, "user_agent": "pytest",
                        "extra": "ignored"})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["lines"] == 3

        written = list((tmp / "station-logs").glob("*.json"))
        assert [p.name for p in written] == [body["file"]]
        report = json.loads(written[0].read_text(encoding="utf-8"))
        assert report["device"] == {"id": dev_id, "name": "Laptop",
                                   "kind": "agent"}
        assert report["user"] == "a@x.io"
        assert report["user_agent"] == "pytest"
        assert "extra" not in report
        assert report["lines"][0] == {"t": 1.5, "level": "info",
                                      "msg": "camera open"}
        assert len(report["lines"][1]["msg"]) == 500
        assert report["lines"][2] == {"t": 0.0, "level": "info", "msg": "odd"}

        # Nothing to say, and nonsense, are both refused.
        assert b.req("post", f"/api/devices/{dev_id}/logs",
                     json={"lines": []}).status_code == 400
        assert b.req("post", f"/api/devices/{dev_id}/logs",
                     json={"lines": "nope"}).status_code == 400
        assert b.req("post", f"/api/devices/{dev_id}/logs",
                     json={"lines": ["nope"]}).status_code == 400

        # And one account cannot file logs against another's camera.
        other = Browser(app, "b@x.io")
        assert other.req("post", f"/api/devices/{dev_id}/logs",
                         json={"lines": lines}).status_code == 404
        assert len(list((tmp / "station-logs").glob("*.json"))) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_columns_added_after_a_database_exists():
    """A column from a newer version has to reach a data/server.db that was
    created before it existed, or every device read blows up."""
    from sqlalchemy import text

    from server.models import make_session_factory

    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        b.add_device()
        engine = app.extensions["dfc"]["engine"]
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE devices DROP COLUMN kind"))

        engine2, Session = make_session_factory(f"sqlite:///{tmp / 'test.db'}")
        with engine2.begin() as conn:
            kinds = list(conn.execute(text("SELECT kind FROM devices")))
        assert kinds == [("agent",)]
        Session.remove()
        engine2.dispose()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_live_frames_only_relayed_while_watched():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        dev_id, token = b.add_device()
        agent = Agent(app, token)
        hub = app.extensions["dfc"]["live"]

        r = agent.post("/frame", data=JPEG, headers={"Content-Type": "image/jpeg"})
        assert r.get_json()["live_wanted"] is False and hub.latest(dev_id) is None

        hub.mark_watched(dev_id)
        assert agent.post("/heartbeat", json={}).get_json()["live_wanted"] is True
        agent.post("/frame", data=JPEG, headers={"Content-Type": "image/jpeg"})
        assert hub.latest(dev_id)[1] == JPEG
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stream_ends_when_camera_is_silent():
    """An offline camera must not pin a server thread forever."""
    import server.api as api
    app, tmp = make_app()
    old = api.STREAM_IDLE_SEC
    api.STREAM_IDLE_SEC = 0.1
    try:
        b = Browser(app, "a@x.io")
        dev_id, _ = b.add_device()
        with b.c.get(f"/api/devices/{dev_id}/stream.mjpg") as r:
            assert r.status_code == 200 and r.data == b""
    finally:
        api.STREAM_IDLE_SEC = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_retention_prunes_old_events_and_media():
    app, tmp = make_app()
    try:
        b = Browser(app, "a@x.io")
        _, token = b.add_device()
        agent = Agent(app, token)
        agent.full_event("old_event_1", start=time.time() - 40 * 86400)
        agent.full_event("new_event_1")
        with app.app_context():
            assert prune_old_events(app) == 1
        assert len(b.c.get("/api/events").get_json()["events"]) == 1
        assert len(list((tmp / "media").rglob("*.*"))) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_frontend_is_served():
    app, tmp = make_app()
    try:
        c = app.test_client()
        assert b"Dog Free Couch" in c.get("/").data
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/assets/../server/config.py").status_code == 404
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
