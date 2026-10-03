"""
Agent API: what a camera agent calls, authenticated by its device token.

Events are addressed by the agent's own client_id rather than a server id,
so every call is idempotent: an agent that lost its connection mid-upload
can simply retry, and it never has to wait on the server before carrying on
with detection.
"""
from __future__ import annotations

import io
import json
import re
import time

from flask import Blueprint, g, jsonify, request

import sirens

from . import telegram
from .ext import db, device_required, error, in_background, live, storage
from .models import Device, Event, Sound
from .storage import new_key

bp = Blueprint("agent_api", __name__, url_prefix="/api/agent")

_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_VIDEO_EXT = {".mp4", ".webm", ".avi"}
# "zone_points" is the polygon the agent is actually using, as fractions of
# the frame: for a camera set up with calibrate.py it is the only way the
# dashboard can show that zone and let it be edited rather than redrawn.
_STATUS_KEYS = ("running", "camera_ok", "audio_ok", "dog_on_couch", "dogs_in_frame",
                "persons_in_frame", "fps", "recording", "last_error", "started_at",
                "upload_queue", "zone_points")


def _event(client_id: str) -> Event | None:
    if not _CLIENT_ID.match(client_id):
        return None
    return db().query(Event).filter_by(device_id=g.device.id, client_id=client_id).first()


@bp.post("/heartbeat")
@device_required
def heartbeat():
    """Every couple of seconds: report status, collect settings + commands."""
    d: Device = g.device
    body = request.get_json(silent=True) or {}
    status = body.get("status") or {}
    d.status_json = json.dumps({k: status.get(k) for k in _STATUS_KEYS})
    d.last_seen = time.time()

    commands = json.loads(d.commands_json or "[]")
    d.commands_json = "[]"
    db().commit()

    settings = d.settings
    active = settings["alert"]["active_sound"]
    sound = None
    if not sirens.is_builtin(active):
        s = db().query(Sound).filter_by(id=active, user_id=d.user_id).first()
        sound = {"id": s.id, "size": s.size} if s else None

    return jsonify({
        "device": {"id": d.id, "name": d.name},
        "settings": settings,
        "active_sound": sound,
        "commands": commands,
        "live_wanted": live().is_watched(d.id),
    })


@bp.post("/events")
@device_required
def create_event():
    body = request.get_json(silent=True) or {}
    client_id = str(body.get("client_id", ""))
    if not _CLIENT_ID.match(client_id):
        return error("bad client_id")
    ev = _event(client_id)
    if ev is None:
        ev = Event(device_id=g.device.id, client_id=client_id,
                   start_ts=float(body.get("start_ts") or time.time()),
                   confidence=body.get("confidence"))
        db().add(ev)
        db().commit()
    return jsonify({"id": ev.id})


@bp.post("/events/<client_id>/end")
@device_required
def end_event(client_id):
    ev = _event(client_id)
    if ev is None:
        return error("unknown event", 404)
    body = request.get_json(silent=True) or {}
    ev.end_ts = body.get("end_ts")
    ev.duration = body.get("duration")
    db().commit()
    return jsonify({"ok": True})


@bp.post("/events/<client_id>/snapshot")
@device_required
def upload_snapshot(client_id):
    ev = _event(client_id)
    if ev is None:
        return error("unknown event", 404)
    f = request.files.get("file")
    if not f:
        return error("no file")
    data = f.read()
    if not data.startswith(b"\xff\xd8"):
        return error("expected a JPEG")
    if ev.snapshot_key:
        return jsonify({"ok": True, "duplicate": True})

    key = new_key(g.device.user_id, "snapshots", ".jpg")
    storage().save(key, io.BytesIO(data))
    ev.snapshot_key = key
    db().commit()

    tg = g.device.user.telegram
    if tg["enabled"] and tg["bot_token"] and tg["chat_id"]:
        in_background(_notify_photo, ev.id, tg, g.device.name, data if tg["send_photo"] else None)
    return jsonify({"ok": True})


@bp.post("/events/<client_id>/video")
@device_required
def upload_video(client_id):
    ev = _event(client_id)
    if ev is None:
        return error("unknown event", 404)
    f = request.files.get("file")
    if not f or not f.filename:
        return error("no file")
    ext = "." + f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in _VIDEO_EXT:
        return error("unsupported video type")
    if ev.video_key:
        return jsonify({"ok": True, "duplicate": True})

    key = new_key(g.device.user_id, "videos", ext)
    ev.video_size = storage().save(key, f.stream)
    ev.video_key = key
    db().commit()

    tg = g.device.user.telegram
    if tg["enabled"] and tg["send_video"] and tg["bot_token"] and tg["chat_id"]:
        in_background(_notify_video, key, tg, g.device.name)
    return jsonify({"ok": True})


@bp.post("/frame")
@device_required
def push_frame():
    """One JPEG of the live view, raw in the body. Dropped unless someone
    is watching, so a stale agent can't keep the server busy."""
    hub = live()
    watched = hub.is_watched(g.device.id)
    data = request.get_data(cache=False)
    if watched and data.startswith(b"\xff\xd8") and len(data) < 4 * 1024 * 1024:
        hub.push(g.device.id, data)
    return jsonify({"live_wanted": watched})


@bp.get("/sounds/<int:sound_id>")
@device_required
def download_sound(sound_id):
    s = db().query(Sound).filter_by(id=sound_id, user_id=g.device.user_id).first()
    if s is None:
        return error("not found", 404)
    return storage().serve(s.key, "audio/wav")


# --------------------------------------------------------------------------
# telegram side effects (background threads)
# --------------------------------------------------------------------------

def _notify_photo(event_id: int, tg: dict, device_name: str, jpeg: bytes | None) -> None:
    ok = telegram.send_photo(tg["bot_token"], tg["chat_id"],
                             f"Dog on the couch! ({device_name})", jpeg)
    if ok:
        ev = db().get(Event, event_id)
        if ev:
            ev.notified = True
            db().commit()


def _notify_video(key: str, tg: dict, device_name: str) -> None:
    data = storage().read_bytes(key)
    telegram.send_video(tg["bot_token"], tg["chat_id"], f"Dog on the couch ({device_name})",
                        data, key.rsplit("/", 1)[-1])
