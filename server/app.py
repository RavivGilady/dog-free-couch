"""
Dog Free Couch server: accounts, devices, event storage, and the dashboard.

    python -m server                       # dev: http://127.0.0.1:8000
    gunicorn -w 1 -k gthread --threads 32 "server.app:create_app()"   # prod

Run exactly ONE worker process: the live-view relay keeps frames in memory
(see live.py). Threads, not processes, provide the concurrency.

The camera never connects here directly. An agent (agent.py) runs next to
the camera, does detection locally, and talks to this server over HTTPS:
heartbeats for settings/commands, uploads for events and clips, and frame
pushes only while someone is watching the live view.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from datetime import timedelta
from html import escape as html_escape
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from .config import Config
from .ext import current_user, db, error, login_required, login_user
from .live import LiveHub
from .models import Event, User, make_session_factory
from .storage import make_storage

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Login throttling per source address. In-memory on purpose: one process,
# and a restart clearing it is harmless.
_login_failures: dict = {}
_MAX_FAILURES = 8
_LOCKOUT_SEC = 300


def create_app(config: Config | None = None, start_background: bool = True) -> Flask:
    cfg = config or Config()
    app = Flask(__name__, static_folder=None)
    app.secret_key = cfg.SECRET_KEY
    app.permanent_session_lifetime = timedelta(days=30)
    app.config.update(
        DFC=cfg,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.SECURE_COOKIES,
        MAX_CONTENT_LENGTH=cfg.MAX_UPLOAD_MB * 1024 * 1024,
    )

    if cfg.TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    engine, Session = make_session_factory(cfg.DATABASE_URL)
    app.extensions["dfc"] = {
        "engine": engine,
        "db": Session,
        "storage": make_storage(cfg),
        "live": LiveHub(),
    }

    @app.teardown_appcontext
    def _remove_session(_exc):
        Session.remove()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    from .agent_api import bp as agent_bp
    from .api import bp as api_bp
    app.register_blueprint(api_bp)
    app.register_blueprint(agent_bp)
    _register_auth(app)
    _register_frontend(app)

    if start_background and cfg.RETENTION_DAYS > 0:
        threading.Thread(target=_retention_loop, args=(app,), daemon=True,
                         name="retention").start()
    return app


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def _register_auth(app: Flask) -> None:
    def _locked_out(ip: str) -> bool:
        fails, until = _login_failures.get(ip, (0, 0))
        return fails >= _MAX_FAILURES and time.time() < until

    def _fail(ip: str) -> None:
        fails, _ = _login_failures.get(ip, (0, 0))
        _login_failures[ip] = (fails + 1, time.time() + _LOCKOUT_SEC)

    def _me_payload(user: User) -> dict:
        # "dev" is about this server process, not this account: it says the
        # page may show the developer-only extras (see Config.DEV_MODE).
        return {"user": {"id": user.id, "email": user.email},
                "csrf_token": session.get("csrf"),
                "dev": app.config["DFC"].DEV_MODE,
                "support_email": app.config["DFC"].SUPPORT_EMAIL}

    @app.get("/api/auth/me")
    def auth_me():
        user = current_user()
        if user is None:
            return jsonify({"user": None,
                            "allow_signup": app.config["DFC"].ALLOW_SIGNUP}), 401
        return jsonify(_me_payload(user))

    @app.post("/api/auth/register")
    def auth_register():
        if not app.config["DFC"].ALLOW_SIGNUP:
            return error("Sign-ups are closed on this server.", 403)
        ip = request.remote_addr or "?"
        if _locked_out(ip):
            return error("Too many attempts. Try again in a few minutes.", 429)
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        if not _EMAIL.match(email) or len(email) > 255:
            return error("Enter a valid email address.")
        if len(password) < 8:
            return error("Pick a password of at least 8 characters.")
        s = db()
        if s.query(User).filter_by(email=email).first():
            # Counted as a failure so this can't be used to cheaply
            # enumerate which addresses have accounts.
            _fail(ip)
            return error("An account with that email already exists.", 409)
        user = User(email=email, password_hash=generate_password_hash(password))
        s.add(user)
        s.commit()
        login_user(user)
        return jsonify(_me_payload(user)), 201

    @app.post("/api/auth/login")
    def auth_login():
        ip = request.remote_addr or "?"
        if _locked_out(ip):
            return error("Too many attempts. Try again in a few minutes.", 429)
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip().lower()
        user = db().query(User).filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, str(body.get("password", ""))):
            _fail(ip)
            return error("Wrong email or password.", 401)
        _login_failures.pop(ip, None)
        login_user(user)
        return jsonify(_me_payload(user))

    @app.post("/api/auth/logout")
    @login_required
    def auth_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.post("/api/me/password")
    @login_required
    def change_password():
        body = request.get_json(silent=True) or {}
        user = current_user()
        if not check_password_hash(user.password_hash, str(body.get("current", ""))):
            return error("Current password is wrong.", 403)
        new = str(body.get("new", ""))
        if len(new) < 8:
            return error("New password must be at least 8 characters.")
        user.password_hash = generate_password_hash(new)
        db().commit()
        return jsonify({"ok": True})


# --------------------------------------------------------------------------
# frontend
# --------------------------------------------------------------------------

def _register_frontend(app: Flask) -> None:
    """The dashboard is a static single-page app; serving it from the same
    origin as the API keeps cookies simple and avoids CORS entirely."""

    def _page(name: str):
        resp = send_from_directory(FRONTEND_DIR, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    def _page_with_support(name: str):
        """Static page with the operator's support address filled in. The
        placeholder sits in an href, so the whole link is dropped when no
        address is configured."""
        html = (FRONTEND_DIR / name).read_text(encoding="utf-8")
        email = html_escape(app.config["DFC"].SUPPORT_EMAIL, quote=True)
        if email:
            html = html.replace("{{SUPPORT_EMAIL}}", email)
        else:
            html = re.sub(r"<!--support-->.*?<!--/support-->", "", html, flags=re.S)
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        # Visitors get the pitch; people who are already signed in go
        # straight to their dashboard, as before.
        if current_user() is not None:
            return _page("index.html")
        return _page_with_support("landing.html")

    @app.get("/app")
    def dashboard():
        return _page("index.html")

    @app.get("/help")
    def help_page():
        return _page_with_support("help.html")

    @app.get("/install.sh")
    def install_script():
        resp = send_from_directory(FRONTEND_DIR, "install-agent.sh",
                                   mimetype="text/x-shellscript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/assets/<path:name>")
    def assets(name):
        resp = send_from_directory(FRONTEND_DIR, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------

def _retention_loop(app: Flask) -> None:
    while True:
        try:
            with app.app_context():
                prune_old_events(app)
        except Exception as e:
            print(f"[retention] sweep failed: {e}", file=sys.stderr)
        finally:
            app.extensions["dfc"]["db"].remove()
        time.sleep(3600)


def prune_old_events(app: Flask) -> int:
    days = app.config["DFC"].RETENTION_DAYS
    if days <= 0:
        return 0
    s = app.extensions["dfc"]["db"]
    store = app.extensions["dfc"]["storage"]
    cutoff = time.time() - days * 86400
    old = s.query(Event).filter(Event.start_ts < cutoff).limit(500).all()
    for ev in old:
        for key in (ev.snapshot_key, ev.video_key):
            if key:
                store.delete(key)
        s.delete(ev)
    s.commit()
    return len(old)
