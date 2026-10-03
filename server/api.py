"""
Dashboard API: everything the signed-in browser calls.

Every lookup goes through _own_device / _own_event / _own_sound, which
filter by the current user. A 404 (not 403) for someone else's id is
deliberate: it doesn't reveal that the id exists.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import time
import wave
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, jsonify, request,
                   send_file)
from sqlalchemy import func

from . import telegram
from .ext import (current_user, db, error, live, login_required, storage)
from .models import (AGENT_KIND, BROWSER_KIND, DEVICE_DEFAULTS, DEVICE_KINDS,
                     Device, Event, Sound, hash_token, new_device_token)
from .storage import new_key

bp = Blueprint("api", __name__, url_prefix="/api")

STREAM_IDLE_SEC = 20
MAX_ZONE_POINTS = 24


def _quote(part: str) -> str:
    return f'"{part}"' if any(c.isspace() for c in part) else part


def _agent_command() -> tuple[str, bool]:
    """The "run this on the camera computer" command the dashboard shows
    with a new token, and whether it came out machine-specific.

    Absolute, and with this interpreter spelled out, when agent.py sits
    next to this package and we can actually import what it needs: then the
    command can be pasted into any terminal without cd-ing into the project
    or activating its virtualenv first. Bare `python agent.py` is wrong in
    that case -- it picks up whatever is on PATH, typically a system Python
    with no yaml or opencv.

    Both conditions have to hold. The server image deliberately does not
    ship the agent (it runs by the camera, often another machine entirely),
    and a server-only virtualenv has no opencv even when the file is there,
    so in either case naming this interpreter would just send someone to a
    Python that cannot run the agent. We fall back to the relative command
    and let the README's install steps speak instead.
    """
    script = Path(__file__).resolve().parent.parent / "agent.py"
    if script.is_file() and _has_agent_deps():
        return f"{_quote(sys.executable)} {_quote(str(script))}", True
    return "python agent.py", False


def _has_agent_deps() -> bool:
    """Whether this interpreter could import the agent's top-level deps.
    find_spec only resolves them, so nothing heavy gets loaded here."""
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("yaml", "cv2", "numpy"))
    except (ImportError, ValueError):
        return False


def _own_device(device_id: int) -> Device:
    d = db().query(Device).filter_by(id=device_id, user_id=current_user().id).first()
    if d is None:
        abort(404)
    return d


def _own_event(event_id: int) -> Event:
    ev = (db().query(Event).join(Device)
          .filter(Event.id == event_id, Device.user_id == current_user().id).first())
    if ev is None:
        abort(404)
    return ev


def _own_sound(sound_id: int) -> Sound:
    s = db().query(Sound).filter_by(id=sound_id, user_id=current_user().id).first()
    if s is None:
        abort(404)
    return s


def _clamp_int(value, lo, hi) -> int:
    return max(lo, min(int(value), hi))


def _clamp_float(value, lo, hi) -> float:
    return max(lo, min(float(value), hi))


def _zone_points(raw) -> list[list[float]]:
    """Validate a couch polygon drawn in the dashboard.

    Points are fractions of the frame, not pixels -- see DEVICE_DEFAULTS.
    An empty list is allowed and means "no zone set here"; anything else
    needs at least 3 corners to enclose any area at all. Coordinates are
    clamped rather than rejected, so a corner dragged a hair past the edge
    of the video still saves.
    """
    if not isinstance(raw, list) or len(raw) > MAX_ZONE_POINTS:
        raise ValueError("bad polygon")
    points = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("bad point")
        points.append([round(_clamp_float(c, 0.0, 1.0), 5) for c in p])
    if points and len(points) < 3:
        raise ValueError("need at least 3 points")
    return points


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------

@bp.get("/devices")
@login_required
def list_devices():
    devices = db().query(Device).filter_by(user_id=current_user().id).order_by(Device.id).all()
    return jsonify({"devices": [d.to_dict() for d in devices]})


@bp.post("/devices")
@login_required
def create_device():
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind", AGENT_KIND))
    if kind not in DEVICE_KINDS:
        return error("unknown camera kind")
    name = str(body.get("name", "")).strip()[:100] or (
        "This browser" if kind == BROWSER_KIND else "Living room")
    token = new_device_token()
    d = Device(user_id=current_user().id, name=name, kind=kind,
               token_hash=hash_token(token))
    db().add(d)
    db().commit()
    # The only time the token is ever shown: we keep just its hash.
    payload = {"device": d.to_dict(), "token": token}
    if kind == AGENT_KIND:
        # A browser station has no command to paste: the page that asked for
        # the camera is the one that will run it.
        cmd, local = _agent_command()
        payload.update(agent_cmd=cmd, agent_cmd_local=local)
    return jsonify(payload), 201


@bp.post("/devices/<int:device_id>/browser-token")
@login_required
def browser_token(device_id):
    """Hand a device token to the camera station running in this page.

    Nothing is copied by hand here: the station is already signed in as the
    owner, so it just asks for a token when it starts and keeps it in memory
    for as long as it runs. The call rotates, which is exactly what we want
    -- one camera is driven by one tab, and a tab that lost the race finds
    out on its next heartbeat (401) and stops itself, instead of two tabs
    pushing frames and events for the same camera.
    """
    d = _own_device(device_id)
    if d.kind != BROWSER_KIND:
        return error("not a browser camera")
    token = new_device_token()
    d.token_hash = hash_token(token)
    db().commit()
    return jsonify({"token": token})


@bp.patch("/devices/<int:device_id>")
@login_required
def rename_device(device_id):
    d = _own_device(device_id)
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:100]
    if not name:
        return error("Name can't be empty.")
    d.name = name
    db().commit()
    return jsonify({"device": d.to_dict()})


@bp.delete("/devices/<int:device_id>")
@login_required
def delete_device(device_id):
    d = _own_device(device_id)
    for ev in d.events:
        for key in (ev.snapshot_key, ev.video_key):
            if key:
                storage().delete(key)
    db().delete(d)
    db().commit()
    return jsonify({"ok": True})


@bp.post("/devices/<int:device_id>/token")
@login_required
def rotate_token(device_id):
    """Issue a new token; the old one stops working immediately."""
    d = _own_device(device_id)
    token = new_device_token()
    d.token_hash = hash_token(token)
    db().commit()
    cmd, local = _agent_command()
    return jsonify({"token": token, "agent_cmd": cmd,
                    "agent_cmd_local": local})


@bp.post("/devices/<int:device_id>/settings")
@login_required
def update_device_settings(device_id):
    d = _own_device(device_id)
    body = request.get_json(silent=True) or {}
    s = d.settings

    a = body.get("alert") or {}
    if "play_sound" in a:
        s["alert"]["play_sound"] = bool(a["play_sound"])
    if "repeat_sound_sec" in a:
        s["alert"]["repeat_sound_sec"] = _clamp_int(a["repeat_sound_sec"], 0, 600)
    if "active_sound" in a:
        import sirens

        val = a["active_sound"]
        if not sirens.is_builtin(val):
            try:
                val = _own_sound(int(val)).id
            except (TypeError, ValueError):
                return error("unknown sound")
        s["alert"]["active_sound"] = val

    v = body.get("video") or {}
    if "enabled" in v:
        s["video"]["enabled"] = bool(v["enabled"])
    for key, lo, hi in (("pre_roll_sec", 0, 30), ("max_clip_sec", 5, 600),
                        ("post_roll_sec", 0, 30)):
        if key in v:
            s["video"][key] = _clamp_int(v[key], lo, hi)

    z = body.get("zone") or {}
    if "points" in z:
        try:
            s["zone"]["points"] = _zone_points(z["points"])
        except (TypeError, ValueError) as e:
            return error(f"bad couch zone: {e}")
    if "overlap_threshold" in z:
        try:
            s["zone"]["overlap_threshold"] = round(
                _clamp_float(z["overlap_threshold"], 0.05, 0.95), 3)
        except (TypeError, ValueError):
            return error("bad overlap threshold")

    # Only known keys survive, so a client can't stash arbitrary data here.
    d.settings = {sec: {k: s[sec][k] for k in DEVICE_DEFAULTS[sec]} for sec in DEVICE_DEFAULTS}
    db().commit()
    return jsonify({"device": d.to_dict()})


@bp.post("/devices/<int:device_id>/commands")
@login_required
def queue_command(device_id):
    d = _own_device(device_id)
    cmd = str((request.get_json(silent=True) or {}).get("type", ""))
    if cmd not in ("test_sound",):
        return error("unknown command")
    queued = json.loads(d.commands_json or "[]")[-9:]
    queued.append({"type": cmd, "at": time.time()})
    d.commands_json = json.dumps(queued)
    db().commit()
    return jsonify({"ok": True, "online": d.online()})


# --------------------------------------------------------------------------
# station logs (dev only)
# --------------------------------------------------------------------------

MAX_LOG_LINES = 2000
MAX_LOG_MESSAGE = 500
# How many reports to keep on disk. These are a developer's scratch
# material, not user data, so the oldest are simply dropped.
KEEP_LOG_REPORTS = 50
_LOG_LEVELS = ("info", "warn", "error")


def _log_lines(raw) -> list[dict]:
    """Validate a camera station's log buffer (see logLines() in station.js).

    The page is the only thing that writes these, but it is still a
    browser: nothing here is trusted. Lines are kept in order, capped in
    count and length, and reduced to three known fields, so a report can
    never become a way to park arbitrary data on the server.
    """
    if not isinstance(raw, list):
        raise ValueError("expected a list of log lines")
    lines = []
    for item in raw[-MAX_LOG_LINES:]:
        if not isinstance(item, dict):
            raise ValueError("bad log line")
        level = str(item.get("level", "info"))
        try:
            ts = round(float(item.get("t") or 0), 3)
        except (TypeError, ValueError):
            ts = 0.0
        lines.append({
            "t": ts,
            "level": level if level in _LOG_LEVELS else "info",
            "msg": str(item.get("msg", ""))[:MAX_LOG_MESSAGE],
        })
    return lines


@bp.post("/devices/<int:device_id>/logs")
@login_required
def share_station_logs(device_id):
    """Take a camera station's log and write it next to the database.

    Only on a dev run (Config.DEV_MODE): on a real deployment the endpoint
    does not exist at all, which is why it 404s rather than 403s -- the
    dashboard hides the button from the same flag, and a page left open
    across a restart then gets the same answer as any other stranger.

    One JSON file per report, named after the device and the moment it
    arrived, so "it did nothing when the dog jumped up at 20:14" can be
    read back later without a database round trip.
    """
    if not current_app.config["DFC"].DEV_MODE:
        abort(404)
    d = _own_device(device_id)
    body = request.get_json(silent=True) or {}
    try:
        lines = _log_lines(body.get("lines"))
    except (TypeError, ValueError) as e:
        return error(f"bad logs: {e}")
    if not lines:
        return error("no log lines to share")

    report = {
        "device": {"id": d.id, "name": d.name, "kind": d.kind},
        "user": current_user().email,
        "received_at": time.time(),
        "note": str(body.get("note", ""))[:500],
        "user_agent": str(body.get("user_agent", ""))[:300],
        "status": d.status,
        "settings": d.settings,
        "lines": lines,
    }

    directory = Path(current_app.config["DFC"].DATA_DIR) / "station-logs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(report["received_at"]))
    path = directory / f"{stamp}-device{d.id}.json"
    # The button can be pressed twice in a second; don't let the second
    # press quietly overwrite the first report.
    nth = 2
    while path.exists():
        path = directory / f"{stamp}-device{d.id}-{nth}.json"
        nth += 1
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _prune_log_reports(directory)

    # The person who will read this is watching the terminal the dev server
    # runs in, so point them straight at the file.
    print(f"[logs] {len(lines)} line(s) from \"{d.name}\" -> {path}", file=sys.stderr)
    return jsonify({"ok": True, "lines": len(lines), "file": path.name})


def _prune_log_reports(directory: Path) -> None:
    reports = sorted(directory.glob("*.json"))
    for old in reports[:-KEEP_LOG_REPORTS]:
        try:
            old.unlink()
        except OSError:
            pass


@bp.get("/devices/<int:device_id>/stream.mjpg")
@login_required
def stream(device_id):
    d = _own_device(device_id)
    hub = live()
    hub.mark_watched(d.id)
    dev_id = d.id

    def part(jpeg: bytes) -> bytes:
        return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

    def generate():
        last_ts, jpeg = 0.0, None
        idle_since = time.time()
        while True:
            # Keep telling the agent someone is here, even while frames are
            # still on their way (the agent only starts after a heartbeat).
            hub.mark_watched(dev_id)
            frame = hub.wait_newer(dev_id, last_ts, timeout=2.0)
            if frame is not None:
                last_ts, jpeg = frame
                idle_since = time.time()
                yield part(jpeg)
            elif jpeg is not None:
                # Re-send the last frame: a server only notices a viewer has
                # left when a write fails, so a stream must never go silent.
                yield part(jpeg)
            if time.time() - idle_since > STREAM_IDLE_SEC:
                # Camera offline: end the response rather than hold a thread
                # forever. The page reconnects once the camera is back.
                return

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def _event_query(device_id):
    q = db().query(Event).join(Device).filter(Device.user_id == current_user().id)
    if device_id:
        q = q.filter(Event.device_id == device_id)
    return q


@bp.get("/events")
@login_required
def list_events():
    device_id = request.args.get("device_id", type=int)
    limit = _clamp_int(request.args.get("limit", 50), 1, 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    rows = (_event_query(device_id).order_by(Event.start_ts.desc(), Event.id.desc())
            .limit(limit).offset(offset).all())
    return jsonify({"events": [e.to_dict() for e in rows]})


@bp.get("/stats")
@login_required
def stats():
    """`since` is the browser's local midnight, so "today" means the user's
    day rather than the server's UTC one."""
    device_id = request.args.get("device_id", type=int)
    since = request.args.get("since", type=float) or (time.time() - 86400)
    base = _event_query(device_id)
    total = base.count()
    today = base.filter(Event.start_ts >= since).count()
    agg = base.with_entities(func.coalesce(func.sum(Event.duration), 0),
                             func.coalesce(func.max(Event.duration), 0)).one()
    return jsonify({
        "total_alerts": total,
        "today_alerts": today,
        "total_seconds_on_couch": round(float(agg[0] or 0), 1),
        "longest_session_sec": round(float(agg[1] or 0), 1),
    })


@bp.delete("/events/<int:event_id>")
@login_required
def delete_event(event_id):
    ev = _own_event(event_id)
    for key in (ev.snapshot_key, ev.video_key):
        if key:
            storage().delete(key)
    db().delete(ev)
    db().commit()
    return jsonify({"ok": True})


@bp.get("/events/<int:event_id>/snapshot")
@login_required
def event_snapshot(event_id):
    ev = _own_event(event_id)
    if not ev.snapshot_key:
        abort(404)
    return storage().serve(ev.snapshot_key, "image/jpeg")


@bp.get("/events/<int:event_id>/video")
@login_required
def event_video(event_id):
    ev = _own_event(event_id)
    if not ev.video_key:
        abort(404)
    return storage().serve(ev.video_key)


# --------------------------------------------------------------------------
# sounds
# --------------------------------------------------------------------------

@bp.get("/sounds")
@login_required
def list_sounds():
    rows = (db().query(Sound).filter_by(user_id=current_user().id)
            .order_by(Sound.created_at.desc()).all())
    return jsonify({"sounds": [s.to_dict() for s in rows]})


@bp.post("/sounds")
@login_required
def upload_sound():
    """WAV only: the browser converts its recording before upload, because
    the agent plays sounds with winsound/aplay, which can't decode webm."""
    f = request.files.get("audio")
    if not f:
        return error("no audio uploaded")
    data = f.read()
    if len(data) > 10 * 1024 * 1024:
        return error("recording too large (10MB max)", 413)
    if not data.startswith(b"RIFF"):
        return error("expected a WAV file")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            duration = round(w.getnframes() / float(w.getframerate() or 1), 2)
    except Exception:
        return error("could not decode that WAV")

    label = re.sub(r"\s+", " ", str(request.form.get("label", "")).strip())[:80] or "Recording"
    user = current_user()
    key = new_key(user.id, "sounds", ".wav")
    size = storage().save(key, io.BytesIO(data))
    sound = Sound(user_id=user.id, label=label, key=key, size=size, duration_sec=duration)
    db().add(sound)
    db().commit()
    return jsonify({"sound": sound.to_dict()}), 201


@bp.delete("/sounds/<int:sound_id>")
@login_required
def delete_sound(sound_id):
    sound = _own_sound(sound_id)
    # Any camera using it falls back to the built-in alarm.
    for d in current_user().devices:
        s = d.settings
        if s["alert"]["active_sound"] == sound.id:
            s["alert"]["active_sound"] = "builtin"
            d.settings = s
    storage().delete(sound.key)
    db().delete(sound)
    db().commit()
    return jsonify({"ok": True})


@bp.get("/sounds/builtins")
@login_required
def list_builtin_sounds():
    """The synthesized siren pack, which every account has."""
    import sirens

    return jsonify({"sounds": sirens.catalog()})


@bp.get("/sounds/builtin/audio")
@login_required
def builtin_sound():
    """Kept as the default siren's URL; bookmarks and old clients use it."""
    from alert import get_alarm_wav
    return send_file(get_alarm_wav(), mimetype="audio/wav")


@bp.get("/sounds/builtin/<name>/audio")
@login_required
def builtin_siren(name):
    import sirens

    if not sirens.is_builtin(name):
        abort(404)
    # Served from memory: rendering is cheap and this is a preview click,
    # not something the agent fetches (it synthesizes its own copy).
    return Response(sirens.wav_bytes(name), mimetype="audio/wav")


@bp.get("/sounds/<int:sound_id>/audio")
@login_required
def sound_audio(sound_id):
    return storage().serve(_own_sound(sound_id).key, "audio/wav")


# --------------------------------------------------------------------------
# telegram (per user)
# --------------------------------------------------------------------------

@bp.get("/me/telegram")
@login_required
def get_telegram():
    return jsonify(current_user().telegram_public())


@bp.post("/me/telegram")
@login_required
def set_telegram():
    user = current_user()
    body = request.get_json(silent=True) or {}
    tg = user.telegram
    tg.update({
        "enabled": bool(body.get("enabled")),
        "chat_id": str(body.get("chat_id", "")).strip()[:64],
        "send_photo": bool(body.get("send_photo", True)),
        "send_video": bool(body.get("send_video", True)),
    })
    # Blank means "keep the stored token": the page never receives the real
    # token, so it can't echo it back.
    token = str(body.get("bot_token", "")).strip()
    if token:
        tg["bot_token"] = token[:200]
    user.telegram = tg
    db().commit()
    return jsonify(user.telegram_public())


@bp.post("/me/telegram/test")
@login_required
def test_telegram():
    tg = current_user().telegram
    ok, message = telegram.check(tg.get("bot_token", ""), tg.get("chat_id", ""))
    return jsonify({"ok": ok, "message": message})
