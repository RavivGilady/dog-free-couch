"""
Runtime settings and secrets, kept OUT of config.yaml.

config.yaml is committed to git, so anything editable from the web UI --
above all the Telegram bot token -- lives here instead, in an instance
directory that .gitignore excludes. That keeps a token you paste into the
dashboard from ever ending up in a public repo.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

DEFAULTS = {
    "auth": {
        # Password hash only -- the plaintext is never stored.
        "password_hash": None,
        "must_change_password": True,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "send_video": True,
        "send_photo": True,
    },
    "alert": {
        "active_sound": "builtin",   # "builtin" or a filename in sounds/
        "play_sound": True,
        "repeat_sound_sec": 3,
    },
    "video": {
        "enabled": True,
        "pre_roll_sec": 4,
        "max_clip_sec": 60,
        "post_roll_sec": 3,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SettingsStore:
    def __init__(self, path: str = "instance/settings.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return _deep_merge(DEFAULTS, json.loads(self.path.read_text(encoding="utf-8")))
            except Exception:
                # A corrupt settings file must not stop the monitor from running.
                pass
        return json.loads(json.dumps(DEFAULTS))

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            # Best effort on POSIX; a no-op on Windows.
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, section: str, key: str, default=None):
        with self._lock:
            return self._data.get(section, {}).get(key, default)

    def update(self, section: str, values: dict) -> None:
        with self._lock:
            self._data.setdefault(section, {}).update(values)
            self._flush()

    def public(self) -> dict:
        """Settings safe to hand to the browser: the bot token is replaced by
        a masked hint so the dashboard can show 'configured' without ever
        shipping the secret back out."""
        data = self.all()
        data.pop("auth", None)
        token = data.get("telegram", {}).get("bot_token") or ""
        data["telegram"]["bot_token"] = ""
        data["telegram"]["bot_token_set"] = bool(token)
        data["telegram"]["bot_token_hint"] = (token[:6] + "..." + token[-4:]) if len(token) > 12 else ""
        return data


def get_or_create_secret_key(path: str = "instance/secret_key") -> bytes:
    """Flask session signing key, persisted so sessions survive a restart."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return p.read_bytes()
    key = secrets.token_bytes(32)
    p.write_bytes(key)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return key
