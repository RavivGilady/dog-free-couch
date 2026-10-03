"""
Shared request plumbing: access to the db/storage/live hub, the auth
decorators for browsers (session cookie + CSRF) and agents (bearer token),
and a helper for work that must not hold up a response.
"""
from __future__ import annotations

import functools
import secrets
import threading

from flask import current_app, g, jsonify, request, session

from .models import Device, User, hash_token


def db():
    return current_app.extensions["dfc"]["db"]


def storage():
    return current_app.extensions["dfc"]["storage"]


def live():
    return current_app.extensions["dfc"]["live"]


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def current_user() -> User | None:
    if "user" not in g:
        uid = session.get("uid")
        g.user = db().get(User, uid) if uid else None
    return g.user


def login_user(user: User) -> None:
    session.clear()
    session["uid"] = user.id
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def login_required(fn):
    """Session auth, plus CSRF on anything that changes state: a cookie
    alone would let any other site make requests on the user's behalf."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if current_user() is None:
            return error("not authenticated", 401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = request.headers.get("X-CSRF-Token", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                return error("bad or missing CSRF token", 403)
        return fn(*a, **kw)
    return wrapper


def device_required(fn):
    """Agent auth: `Authorization: Bearer dfc_...`, looked up by hash."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return error("missing device token", 401)
        device = db().query(Device).filter_by(token_hash=hash_token(auth[7:].strip())).first()
        if device is None:
            return error("unknown device token", 401)
        g.device = device
        return fn(*a, **kw)
    return wrapper


def in_background(fn, *args) -> None:
    """Run fn on a thread with its own db session and app context, for slow
    side effects (Telegram uploads) that must not delay the response."""
    app = current_app._get_current_object()

    def run():
        with app.app_context():
            try:
                fn(*args)
            finally:
                app.extensions["dfc"]["db"].remove()

    threading.Thread(target=run, daemon=True).start()
