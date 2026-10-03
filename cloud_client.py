"""
The agent's side of the server connection.

MonitorService was written against two local objects -- an EventStore and a
SettingsStore. RemoteStore and RemoteSettings implement the same methods, so
the detection loop runs unchanged and simply reports to the server instead.
The couch zone comes down the same way: the dashboard stores it as fractions
of the frame and AgentLink hands it to the monitor on the heartbeat that
changes it.

Uploads go through a single FIFO worker so ordering is preserved (an event
is always created before its snapshot/clip are attached) and the capture
loop never waits on the network. A failed upload is retried with backoff;
the server's endpoints are idempotent, so a retry after a half-finished
attempt is safe.
"""
from __future__ import annotations

import queue
import secrets
import sys
import threading
import time
from pathlib import Path

import requests

import sirens
import sounds

HEARTBEAT_SEC = 2.0
LIVE_MAX_FPS = 6.0


class CloudClient:
    def __init__(self, server: str, token: str, timeout: float = 15.0):
        self.server = server.rstrip("/")
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def url(self, path: str) -> str:
        return f"{self.server}/api/agent{path}"

    def post(self, path: str, timeout: float | None = None, **kw):
        return self.http.post(self.url(path), timeout=timeout or self.timeout, **kw)

    def get(self, path: str, **kw):
        return self.http.get(self.url(path), timeout=self.timeout, **kw)


class Uploader:
    """Ordered, retrying upload queue."""

    def __init__(self, client: CloudClient, delete_after_upload: bool = True):
        self.client = client
        self.delete_after_upload = delete_after_upload
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="uploader", daemon=True).start()

    def submit(self, kind: str, **job) -> None:
        self._q.put((kind, job))

    def pending(self) -> int:
        return self._q.qsize()

    def _run(self) -> None:
        while True:
            kind, job = self._q.get()
            delay = 2.0
            while True:
                try:
                    r = self._send(kind, job)
                    if r is None or r.ok:
                        break
                    # Other 4xx won't succeed on retry (bad input); anything
                    # else -- server down, rate limited, token being fixed --
                    # is worth waiting out.
                    if 400 <= r.status_code < 500 and r.status_code not in (401, 408, 429):
                        print(f"[cloud] dropping {kind}: HTTP {r.status_code} {r.text[:200]}",
                              file=sys.stderr)
                        break
                    print(f"[cloud] {kind} failed (HTTP {r.status_code}); retrying in {delay:.0f}s",
                          file=sys.stderr)
                except requests.RequestException as e:
                    print(f"[cloud] {kind} failed ({e.__class__.__name__}); retrying in {delay:.0f}s",
                          file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def _send(self, kind: str, job: dict):
        c = self.client
        if kind == "create":
            return c.post("/events", json=job)
        if kind == "end":
            return c.post(f"/events/{job['client_id']}/end", json=job)
        if kind in ("snapshot", "video"):
            path = Path(job["path"])
            if not path.exists():
                print(f"[cloud] {kind} file vanished: {path}", file=sys.stderr)
                return None
            with open(path, "rb") as f:
                r = c.post(f"/events/{job['client_id']}/{kind}", timeout=600,
                           files={"file": (path.name, f)})
            if r.ok:
                print(f"[cloud] uploaded {kind} {path.name}")
                if self.delete_after_upload:
                    path.unlink(missing_ok=True)
            return r
        raise ValueError(kind)


class RemoteStore:
    """EventStore stand-in: same methods MonitorService calls, but the
    events go to the server."""

    def __init__(self, uploader: Uploader):
        self.up = uploader
        self._open: dict[float, str] = {}

    def add_entered(self, start_ts, confidence=None, snapshot=None) -> str:
        client_id = secrets.token_urlsafe(12)
        self._open[start_ts] = client_id
        self.up.submit("create", client_id=client_id, start_ts=start_ts,
                       confidence=round(confidence, 3) if confidence else None)
        if snapshot:
            self.up.submit("snapshot", client_id=client_id, path=snapshot)
        return client_id

    def add_left(self, start_ts, end_ts, duration) -> None:
        client_id = self._open.pop(start_ts, None)
        if client_id:
            self.up.submit("end", client_id=client_id, end_ts=end_ts, duration=round(duration, 1))

    def attach_video(self, event_id, video_path) -> None:
        self.up.submit("video", client_id=event_id, path=video_path)

    def mark_notified(self, event_id) -> None:
        pass  # the server sends notifications now


class RemoteSettings:
    """SettingsStore stand-in, refreshed from each heartbeat.

    Telegram is always reported as disabled: the server notifies, using the
    token stored in the user's account, so the agent never holds it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "alert": {"play_sound": True, "repeat_sound_sec": 3, "active_sound": "builtin"},
            "video": {"enabled": True, "pre_roll_sec": 4, "max_clip_sec": 60, "post_roll_sec": 3},
            "telegram": {"enabled": False},
            "zone": {"points": [], "overlap_threshold": 0.35},
        }

    def replace(self, settings: dict, local_sound: str) -> dict:
        """Swap in server settings; returns the previous values, so the caller
        can tell what changed -- the recorder and the couch zone both have to
        be pushed into the running monitor, not just stored here.

        A server too old to know about zones sends no zone section at all;
        the current one is then left alone rather than cleared.
        """
        with self._lock:
            old = {k: dict(v) for k, v in self._data.items()}
            self._data["alert"] = dict(settings.get("alert", {}), active_sound=local_sound)
            self._data["video"] = dict(settings.get("video", {}))
            if settings.get("zone"):
                self._data["zone"] = dict(settings["zone"])
            return old

    def all(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def get(self, section, key, default=None):
        with self._lock:
            return self._data.get(section, {}).get(key, default)


class AgentLink:
    """Heartbeat + live-view loops tying a MonitorService to the server."""

    def __init__(self, client: CloudClient, monitor, settings: RemoteSettings,
                 uploader: Uploader):
        self.client = client
        self.monitor = monitor
        self.settings = settings
        self.uploader = uploader
        self.live_wanted = threading.Event()
        self.connected = None  # unknown until the first heartbeat
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._live_loop, name="live", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---------- heartbeat ----------

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                status = dict(self.monitor.status, upload_queue=self.uploader.pending())
                r = self.client.post("/heartbeat", json={"status": status}, timeout=10)
                if r.status_code == 401:
                    self._set_connected(False, "server rejected the device token")
                elif r.ok:
                    self._set_connected(True)
                    self._apply(r.json())
                else:
                    self._set_connected(False, f"HTTP {r.status_code}")
            except requests.RequestException as e:
                self._set_connected(False, e.__class__.__name__)
            self._stop.wait(HEARTBEAT_SEC)

    def _set_connected(self, ok: bool, why: str = "") -> None:
        # Log transitions only, not every 2-second heartbeat.
        if ok and self.connected is not True:
            print(f"[cloud] connected to {self.client.server}")
        elif not ok and self.connected is not False:
            print(f"[cloud] cannot reach server: {why}", file=sys.stderr)
        self.connected = ok

    def _apply(self, reply: dict) -> None:
        settings = reply.get("settings", {})
        local_sound = self._sync_sound(
            reply.get("active_sound"),
            (settings.get("alert") or {}).get("active_sound"))
        old = self.settings.replace(settings, local_sound)
        new = self.settings.all()
        if new["video"] != old["video"]:
            self.monitor.apply_video_settings(new["video"])
        # An empty polygon means the dashboard has no zone of its own, which
        # leaves the one from config.yaml in place rather than blinding the
        # camera -- clearing a zone would stop it ever alerting.
        if new["zone"] != old["zone"] and new["zone"].get("points"):
            z = new["zone"]
            self.monitor.update_zone(z["points"], z.get("overlap_threshold"))
            print(f"[cloud] couch zone updated from the dashboard "
                  f"({len(z['points'])} corners)")

        if reply.get("live_wanted"):
            self.live_wanted.set()
        else:
            self.live_wanted.clear()

        for cmd in reply.get("commands", []):
            if cmd.get("type") == "test_sound":
                from alert import play_alert_sound
                print("[cloud] test alarm requested from dashboard")
                play_alert_sound(custom_wav=sounds.resolve(local_sound),
                                 builtin=local_sound)

    def _sync_sound(self, active, chosen=None) -> str:
        """Fetch the dashboard-chosen recording once and cache it in sounds/.

        No recording means a built-in siren is selected, and its id passes
        straight through -- the agent synthesizes those locally.
        """
        if not active:
            return chosen if sirens.is_builtin(chosen) else "builtin"
        name = f"cloud_{int(active['id'])}.wav"
        path = sounds.ensure_dir() / name
        if path.exists() and path.stat().st_size == active.get("size"):
            return name
        try:
            r = self.client.get(f"/sounds/{int(active['id'])}")
            if r.ok and r.content.startswith(b"RIFF"):
                path.write_bytes(r.content)
                print(f"[cloud] downloaded alert sound {name}")
                return name
        except requests.RequestException:
            pass
        return name if path.exists() else "builtin"

    # ---------- live view ----------

    def _live_loop(self) -> None:
        last = None
        while not self._stop.is_set():
            if not self.live_wanted.wait(timeout=1.0):
                continue
            t0 = time.time()
            jpeg = self.monitor.latest_jpeg()
            if jpeg is not None and jpeg is not last:
                last = jpeg
                try:
                    r = self.client.post("/frame", data=jpeg, timeout=5,
                                         headers={"Content-Type": "image/jpeg"})
                    if r.ok and not r.json().get("live_wanted"):
                        self.live_wanted.clear()
                except requests.RequestException:
                    time.sleep(1.0)
            time.sleep(max(0.0, 1.0 / LIVE_MAX_FPS - (time.time() - t0)))
