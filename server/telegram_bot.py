"""
The shared Telegram bot: one token owned by the server, many users.

A user links their chat from the dashboard, which hands out a one-time code
and opens t.me/<bot>?start=<code>. The /start message that follows tells us
which Telegram chat belongs to which account. After that, notifications go
to that chat using the server's token (see agent_api).

Updates arrive by webhook when PUBLIC_URL is set (production), otherwise a
long-polling thread fetches them (local development has no public address).
"""
from __future__ import annotations

import secrets
import sys
import threading
import time

from . import telegram
from .models import User

LINK_TTL_SEC = 15 * 60

_HELP = ("Hi! To get Dog Free Couch alerts here, open the dashboard, go to "
         "Settings and press \"Connect Telegram\".")


def new_link_code() -> str:
    return secrets.token_urlsafe(16)  # fits Telegram's 64-char /start limit


def handle_update(app, update: dict) -> None:
    """Never raises: one malformed update must not stop the bot."""
    try:
        msg = update.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text.startswith("/"):
            return
        cfg = app.config["DFC"]
        token = cfg.TELEGRAM_BOT_TOKEN
        button = None
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        with app.app_context():
            s = app.extensions["dfc"]["db"]
            try:
                if cmd == "/start":
                    reply, linked = _link(s, chat_id, arg.strip())
                    # Telegram only accepts https links in buttons, so this
                    # appears once PUBLIC_URL is set, not on localhost.
                    if linked and cfg.PUBLIC_URL.startswith("https://"):
                        button = ("Back to Dog Free Couch", cfg.PUBLIC_URL)
                elif cmd == "/stop":
                    reply = _unlink(s, chat_id)
                else:
                    reply = _HELP
            finally:
                s.remove()
        telegram.send_message(token, chat_id, reply, button)
    except Exception as e:
        print(f"[telegram-bot] update failed: {e}", file=sys.stderr)


def _link(s, chat_id, code: str) -> tuple:
    """Returns (reply text, whether the chat is now linked)."""
    if not code:
        return _HELP, False
    user = s.query(User).filter_by(tg_link_code=code).first()
    if user is None or (user.tg_link_expires or 0) < time.time():
        return "That link has expired. Press \"Connect Telegram\" in the dashboard again.", False
    chat = str(chat_id)
    # A chat belongs to one account, so alerts never reach two households.
    for other in s.query(User).filter(User.telegram_json.like(f'%"{chat}"%')):
        if other.id != user.id and other.telegram.get("chat_id") == chat:
            tg = other.telegram
            tg.update(chat_id="", enabled=False)
            other.telegram = tg
    tg = user.telegram
    tg.update(chat_id=chat, enabled=True)
    user.telegram = tg
    user.tg_link_code = None
    user.tg_link_expires = None
    s.commit()
    return f"Connected as {user.email}. You'll get a message here when the dog is on the couch. Send /stop to unsubscribe.", True


def _unlink(s, chat_id) -> str:
    chat = str(chat_id)
    found = False
    for user in s.query(User).filter(User.telegram_json.like(f'%"{chat}"%')):
        tg = user.telegram
        if tg.get("chat_id") == chat:
            tg.update(chat_id="", enabled=False)
            user.telegram = tg
            found = True
    s.commit()
    return "Unsubscribed. Alerts to this chat are off." if found else "This chat isn't connected to an account."


def start(app) -> None:
    """Pick webhook or polling. Best effort: Telegram being down at boot
    must not stop the server."""
    cfg = app.config["DFC"]
    if not cfg.TELEGRAM_BOT_TOKEN:
        return
    if cfg.PUBLIC_URL:
        url = cfg.PUBLIC_URL.rstrip("/") + "/api/telegram/webhook"
        threading.Thread(
            target=lambda: telegram.set_webhook(cfg.TELEGRAM_BOT_TOKEN, url,
                                                cfg.TELEGRAM_WEBHOOK_SECRET),
            daemon=True, name="tg-webhook").start()
    else:
        threading.Thread(target=_poll, args=(app,), daemon=True, name="tg-poll").start()


def _poll(app) -> None:
    token = app.config["DFC"].TELEGRAM_BOT_TOKEN
    telegram.delete_webhook(token)  # getUpdates is refused while a webhook is set
    offset = 0
    while True:
        try:
            for update in telegram.get_updates(token, offset):
                offset = update["update_id"] + 1
                handle_update(app, update)
        except Exception as e:
            print(f"[telegram-bot] polling failed: {e}", file=sys.stderr)
            time.sleep(10)
