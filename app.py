"""
Web dashboard for the dog couch monitor.

    python app.py                 # http://127.0.0.1:8080, local only
    python app.py --host 0.0.0.0  # reachable from your phone on the same wifi

This process owns the camera (only one process can), runs the detection
loop in a background thread, and serves:

  * a live MJPEG view of what the detector sees,
  * a dashboard of every event with its snapshot and video clip,
  * mic recording of a custom alert sound,
  * Telegram configuration, so credentials are set from the browser rather
    than by editing a file on the host.

SECURITY: this speaks plain HTTP. The password is hashed and sessions are
signed, but on a LAN anyone sniffing traffic can read the session cookie,
and over the open internet they could read the password as you type it.
Before exposing this beyond your own network, put it behind a reverse proxy
with TLS (Caddy or nginx) -- see the README.
"""
from __future__ import annotations

import argparse
import functools
import io
import mimetypes
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

import yaml
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import sounds
from alert import telegram_check
from service import MonitorService
from settings_store import SettingsStore, get_or_create_secret_key
from store import EventStore

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")

config: dict = {}
store: EventStore = None
settings: SettingsStore = None
monitor: MonitorService = None

# Login throttling, per source address. In-memory on purpose: a restart
# clearing it is fine, and it avoids another moving part.
_login_failures: dict = {}
_MAX_FAILURES = 8
_LOCKOUT_SEC = 300


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("authed"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "not authenticated"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def csrf_protect(fn):
    """Session cookies alone would let any site POST here on your behalf."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if request.method in ("POST", "PUT", "DELETE"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf_token", "")):
                return jsonify({"error": "bad or missing CSRF token"}), 403
        return fn(*a, **kw)
    return wrapper


def _locked_out(ip: str) -> bool:
    fails, until = _login_failures.get(ip, (0, 0))
    return fails >= _MAX_FAILURES and time.time() < until


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    pw_hash = settings.get("auth", "password_hash")

    if request.method == "POST":
        if _locked_out(ip):
            return render_template("login.html",
                                   error="Too many attempts. Try again in a few minutes.",
                                   first_run=not pw_hash), 429

        password = request.form.get("password", "")

        if not pw_hash:
            # First run: whatever is typed becomes the password.
            if len(password) < 8:
                return render_template("login.html", first_run=True,
                                       error="Pick a password of at least 8 characters.")
            settings.update("auth", {"password_hash": generate_password_hash(password),
                                     "must_change_password": False})
            session["authed"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("dashboard"))

        if check_password_hash(pw_hash, password):
            _login_failures.pop(ip, None)
            session["authed"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            nxt = request.args.get("next", "")
            # Only ever redirect to a local path, never to an absolute URL.
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                            else url_for("dashboard"))

        fails, _ = _login_failures.get(ip, (0, 0))
        _login_failures[ip] = (fails + 1, time.time() + _LOCKOUT_SEC)
        return render_template("login.html", error="Wrong password.", first_run=False), 401

    return render_template("login.html", first_run=not pw_hash)


@app.route("/logout", methods=["POST"])
@csrf_protect
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", csrf_token=session.get("csrf_token", ""))


# --------------------------------------------------------------------------
# live view
# --------------------------------------------------------------------------

@app.route("/stream.mjpg")
@login_required
def stream():
    def generate():
        boundary = b"--frame"
        last = None
        while True:
            jpeg = monitor.latest_jpeg()
            if jpeg is not None and jpeg is not last:
                last = jpeg
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            time.sleep(0.05)

    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.route("/api/status")
@login_required
def api_status():
    st = dict(monitor.status)
    st["stats"] = store.stats()
    st["uptime_sec"] = round(time.time() - st["started_at"], 1) if st.get("started_at") else 0
    return jsonify(st)


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

@app.route("/api/events")
@login_required
def api_events():
    limit = min(int(request.args.get("limit", 50)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    only_alerts = request.args.get("only_alerts", "1") == "1"
    rows = store.recent(limit=limit, offset=offset, only_alerts=only_alerts)
    for r in rows:
        r["has_video"] = bool(r.get("video") and Path(r["video"]).exists())
        r["has_snapshot"] = bool(r.get("snapshot") and Path(r["snapshot"]).exists())
    return jsonify({"events": rows, "stats": store.stats()})


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
@login_required
@csrf_protect
def api_delete_event(event_id):
    row = store.delete(event_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    # Remove the media too, so deleting from the dashboard actually frees space.
    for key in ("video", "snapshot"):
        p = row.get(key)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
    return jsonify({"ok": True})


def _safe_media(stored_path: str, allowed_roots) -> Path:
    """Only ever serve files that live under our own media directories.

    The path comes out of the database, but a bad row (or a future import
    feature) must not be able to hand out arbitrary files from the disk.
    """
    p = Path(stored_path).resolve()
    for root in allowed_roots:
        try:
            root = Path(root).resolve()
            p.relative_to(root)
            return p
        except (ValueError, OSError):
            continue
    abort(403)


@app.route("/media/video/<int:event_id>")
@login_required
def media_video(event_id):
    row = store.get(event_id)
    if not row or not row.get("video"):
        abort(404)
    path = _safe_media(row["video"], ["videos"])
    if not path.exists():
        abort(404)
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    # conditional=True gives byte-range support, which browsers need in
    # order to seek within a video rather than only play it straight through.
    return send_file(path, mimetype=mime, conditional=True)


@app.route("/media/snapshot/<int:event_id>")
@login_required
def media_snapshot(event_id):
    row = store.get(event_id)
    if not row or not row.get("snapshot"):
        abort(404)
    path = _safe_media(row["snapshot"], ["snapshots"])
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/jpeg", conditional=True)


# --------------------------------------------------------------------------
# sounds
# --------------------------------------------------------------------------

@app.route("/api/sounds")
@login_required
def api_sounds():
    active = settings.get("alert", "active_sound", "builtin")
    return jsonify({"sounds": sounds.list_sounds(active), "active": active})


@app.route("/api/sounds", methods=["POST"])
@login_required
@csrf_protect
def api_upload_sound():
    """Receives a WAV recorded in the browser.

    The page encodes to WAV client-side: MediaRecorder produces webm/opus,
    which winsound cannot play, and ffmpeg is not a dependency here.
    """
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "no audio uploaded"}), 400

    label = request.form.get("label", "recording")
    data = f.read()
    if len(data) > 10 * 1024 * 1024:
        return jsonify({"error": "recording too large (10MB max)"}), 413
    if not data.startswith(b"RIFF"):
        return jsonify({"error": "expected a WAV file"}), 400

    path = sounds.new_recording_path(label)
    path.write_bytes(data)

    info = sounds.wav_info(path)
    if info["duration_sec"] is None:
        path.unlink(missing_ok=True)
        return jsonify({"error": "could not decode that WAV"}), 400

    if request.form.get("activate") == "1":
        settings.update("alert", {"active_sound": path.name})
    return jsonify({"ok": True, "name": path.name, **info})


@app.route("/api/sounds/active", methods=["POST"])
@login_required
@csrf_protect
def api_set_active_sound():
    name = (request.json or {}).get("name", "builtin")
    if name != "builtin" and sounds.resolve(name) is None:
        return jsonify({"error": "unknown sound"}), 404
    settings.update("alert", {"active_sound": name})
    return jsonify({"ok": True, "active": name})


@app.route("/api/sounds/<name>", methods=["DELETE"])
@login_required
@csrf_protect
def api_delete_sound(name):
    if not sounds.is_safe_name(name):
        return jsonify({"error": "bad name"}), 400
    if settings.get("alert", "active_sound") == name:
        settings.update("alert", {"active_sound": "builtin"})
    return (jsonify({"ok": True}) if sounds.delete(name)
            else (jsonify({"error": "not found"}), 404))


@app.route("/api/sounds/preview/<name>")
@login_required
def api_preview_sound(name):
    """Play in the browser, not on the host -- so you can audition a sound
    from your phone without setting off the alarm in the living room."""
    if name == "builtin":
        from alert import get_alarm_wav
        return send_file(get_alarm_wav(), mimetype="audio/wav")
    p = sounds.resolve(name)
    if p is None:
        abort(404)
    return send_file(p, mimetype="audio/wav")


@app.route("/api/sounds/test", methods=["POST"])
@login_required
@csrf_protect
def api_test_sound():
    """Play the active sound on the HOST, exactly as an alert would."""
    from alert import play_alert_sound
    active = settings.get("alert", "active_sound", "builtin")
    play_alert_sound(custom_wav=sounds.resolve(active))
    return jsonify({"ok": True, "played": active})


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

@app.route("/api/settings")
@login_required
def api_get_settings():
    return jsonify(settings.public())


@app.route("/api/settings/telegram", methods=["POST"])
@login_required
@csrf_protect
def api_set_telegram():
    body = request.json or {}
    update = {
        "enabled": bool(body.get("enabled")),
        "chat_id": str(body.get("chat_id", "")).strip(),
        "send_video": bool(body.get("send_video", True)),
        "send_photo": bool(body.get("send_photo", True)),
    }
    # An empty token means "leave the stored one alone" -- the UI never
    # receives the real token, so it cannot echo it back to us.
    token = str(body.get("bot_token", "")).strip()
    if token:
        update["bot_token"] = token
    settings.update("telegram", update)
    return jsonify(settings.public())


@app.route("/api/settings/telegram/test", methods=["POST"])
@login_required
@csrf_protect
def api_test_telegram():
    tg = settings.all().get("telegram", {})
    ok, message = telegram_check(tg.get("bot_token", ""), tg.get("chat_id", ""))
    return jsonify({"ok": ok, "message": message})


@app.route("/api/settings/alert", methods=["POST"])
@login_required
@csrf_protect
def api_set_alert():
    body = request.json or {}
    update = {}
    if "play_sound" in body:
        update["play_sound"] = bool(body["play_sound"])
    if "repeat_sound_sec" in body:
        update["repeat_sound_sec"] = max(0, min(int(body["repeat_sound_sec"]), 600))
    settings.update("alert", update)
    return jsonify(settings.public())


@app.route("/api/settings/video", methods=["POST"])
@login_required
@csrf_protect
def api_set_video():
    body = request.json or {}
    update = {}
    if "enabled" in body:
        update["enabled"] = bool(body["enabled"])
    for key, lo, hi in (("pre_roll_sec", 0, 30), ("max_clip_sec", 5, 600),
                        ("post_roll_sec", 0, 30)):
        if key in body:
            update[key] = max(lo, min(int(body[key]), hi))
    settings.update("video", update)

    r = monitor.recorder
    if "pre_roll_sec" in update:
        from collections import deque
        r.pre_roll_sec = update["pre_roll_sec"]
        r._buffer = deque(maxlen=max(1, int(r.fps * r.pre_roll_sec)))
    if "max_clip_sec" in update:
        r.max_clip_sec = update["max_clip_sec"]
    if "post_roll_sec" in update:
        r.post_roll_sec = update["post_roll_sec"]
    return jsonify(settings.public())


@app.route("/api/settings/password", methods=["POST"])
@login_required
@csrf_protect
def api_change_password():
    body = request.json or {}
    current = body.get("current", "")
    new = body.get("new", "")
    pw_hash = settings.get("auth", "password_hash")
    if pw_hash and not check_password_hash(pw_hash, current):
        return jsonify({"error": "current password is wrong"}), 403
    if len(new) < 8:
        return jsonify({"error": "new password must be at least 8 characters"}), 400
    settings.update("auth", {"password_hash": generate_password_hash(new),
                             "must_change_password": False})
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------

def create_app(config_path: str = "config.yaml"):
    global config, store, settings, monitor

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    settings = SettingsStore()
    store = EventStore(config.get("web", {}).get("db", "data/events.db"))
    monitor = MonitorService(config, store, settings)

    app.secret_key = get_or_create_secret_key()
    app.permanent_session_lifetime = timedelta(days=7)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
    )

    if not config.get("zone", {}).get("points"):
        print("Warning: no couch zone defined. Run `python calibrate.py` first.",
              file=sys.stderr)
    return app


def main():
    ap = argparse.ArgumentParser(description="Dog couch monitor web dashboard")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 (default, local only) or 0.0.0.0 to allow LAN access")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-monitor", action="store_true",
                    help="Serve the dashboard without opening the camera.")
    args = ap.parse_args()

    create_app(args.config)

    if not args.no_monitor:
        monitor.start()

    if args.host != "127.0.0.1":
        print(f"\n  NOTE: serving on {args.host} over plain HTTP. Fine on a trusted\n"
              f"  home network; put TLS in front of it before exposing it further.\n")
    print(f"  Dashboard: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}\n")

    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
