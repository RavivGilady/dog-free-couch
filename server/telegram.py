"""
Telegram notifications, sent from the server.

Sending from here rather than from the agent means each user's bot token
lives in one place (the server's database) instead of on every camera, and a
clip only crosses the home uplink once. All functions take bytes because the
media may live in S3 rather than on this disk. None of them raise: a flaky
Telegram must never fail an agent's upload.
"""
from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger("dfc.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Telegram's bot upload limit


def _post(token: str, method: str, timeout: float, **kw):
    return requests.post(_API.format(token=token, method=method), timeout=timeout, **kw)


def send_photo(token: str, chat_id: str, caption: str, jpeg: bytes | None) -> bool:
    try:
        if jpeg:
            r = _post(token, "sendPhoto", 20, data={"chat_id": chat_id, "caption": caption},
                      files={"photo": ("snapshot.jpg", jpeg, "image/jpeg")})
        else:
            r = _post(token, "sendMessage", 10, data={"chat_id": chat_id, "text": caption})
        return r.ok
    except Exception as e:
        log.warning("photo failed: %s", e.__class__.__name__)
        return False


def send_video(token: str, chat_id: str, caption: str, data: bytes, filename: str) -> bool:
    try:
        if len(data) > _MAX_VIDEO_BYTES:
            _post(token, "sendMessage", 10, data={
                "chat_id": chat_id,
                "text": f"{caption} (clip too large for Telegram: {len(data) / 1e6:.0f}MB)"})
            return False
        r = _post(token, "sendVideo", 120, data={"chat_id": chat_id, "caption": caption},
                  files={"video": (filename, data)})
        if not r.ok:
            log.warning("sendVideo failed: HTTP %s %s", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        log.warning("video failed: %s", e.__class__.__name__)
        return False


def check(token: str, chat_id: str) -> tuple:
    """Returns (ok, message) so the UI can say exactly what is wrong."""
    try:
        if not token:
            return False, "No bot token set."
        r = requests.get(_API.format(token=token, method="getMe"), timeout=10)
        if not r.ok:
            return False, f"Token rejected by Telegram (HTTP {r.status_code})."
        name = r.json().get("result", {}).get("username", "?")
        if not chat_id:
            return False, f"Bot @{name} is valid, but no chat id is set."
        r2 = _post(token, "sendMessage", 10,
                   data={"chat_id": chat_id, "text": "Dog Free Couch: test message."})
        if not r2.ok:
            return False, (f"Bot @{name} is valid but sending to chat {chat_id} failed "
                           f"(HTTP {r2.status_code}). Have you messaged the bot first?")
        return True, f"Sent a test message via @{name}."
    except Exception as e:
        return False, f"Could not reach Telegram: {e}"


# --------------------------------------------------------------------------
# the shared bot: one token owned by the server, many users linked to it
# --------------------------------------------------------------------------

def send_message(token: str, chat_id, text: str, button: tuple | None = None) -> bool:
    """`button` is (label, https url): a tap-through under the message."""
    data = {"chat_id": chat_id, "text": text}
    if button:
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]})
    try:
        return _post(token, "sendMessage", 10, data=data).ok
    except Exception as e:
        log.warning("message failed: %s", e.__class__.__name__)
        return False


def bot_username(token: str) -> str:
    """The bot's @name, or "" if Telegram rejects the token or is unreachable."""
    try:
        r = requests.get(_API.format(token=token, method="getMe"), timeout=10)
        return r.json().get("result", {}).get("username", "") if r.ok else ""
    except Exception:
        return ""


def set_webhook(token: str, url: str, secret: str) -> bool:
    try:
        r = _post(token, "setWebhook", 10, data={
            "url": url, "secret_token": secret, "allowed_updates": '["message"]'})
        return r.ok
    except Exception as e:
        log.warning("setWebhook failed: %s", e.__class__.__name__)
        return False


def delete_webhook(token: str) -> None:
    try:
        _post(token, "deleteWebhook", 10)
    except Exception:
        pass


def get_updates(token: str, offset: int, timeout: int = 25) -> list:
    """Long poll. Raises on failure so the caller can back off."""
    r = _post(token, "getUpdates", timeout + 10,
              data={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'})
    r.raise_for_status()
    return r.json().get("result", [])
