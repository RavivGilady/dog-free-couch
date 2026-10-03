"""
Database models.

Ownership is a straight line: a User owns Devices (one per camera/agent) and
Sounds; a Device owns Events. Every query from the browser is scoped through
the signed-in user, so one account can never see another's clips.

Settings the agent obeys (alarm, clip lengths, active sound, the couch
zone) live on the Device, because two cameras in two rooms may well want
different behaviour.
Telegram lives on the User: it is "where do my notifications go", not a
property of a camera.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from sqlalchemy import (Boolean, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, scoped_session, sessionmaker)

DEVICE_DEFAULTS = {
    "alert": {
        "play_sound": True,
        "repeat_sound_sec": 3,
        "active_sound": "builtin",  # a sirens.SIRENS id or a Sound id
    },
    "video": {
        "enabled": True,
        "pre_roll_sec": 4,
        "max_clip_sec": 60,
        "post_roll_sec": 3,
    },
    # The couch polygon, drawn in the dashboard on top of the live view.
    # Points are fractions of the frame (0..1), not pixels: the browser only
    # ever sees a scaled JPEG, and the camera's resolution can change under
    # us. Empty means "not set here" -- the agent then keeps using the zone
    # in its own config.yaml, from calibrate.py.
    "zone": {
        "points": [],
        "overlap_threshold": 0.35,
    },
}

TELEGRAM_DEFAULTS = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",
    "send_photo": True,
    "send_video": True,
}


def _merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def hash_token(token: str) -> str:
    # Device tokens are 256 bits of randomness, so a fast hash is enough --
    # there is nothing to brute-force, unlike a human password.
    return hashlib.sha256(token.encode()).hexdigest()


def new_device_token() -> str:
    return "dfc_" + secrets.token_urlsafe(32)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    telegram_json: Mapped[str] = mapped_column(Text, default="{}")

    devices: Mapped[list["Device"]] = relationship(back_populates="user",
                                                   cascade="all, delete-orphan")
    sounds: Mapped[list["Sound"]] = relationship(back_populates="user",
                                                 cascade="all, delete-orphan")

    @property
    def telegram(self) -> dict:
        return _merge(TELEGRAM_DEFAULTS, json.loads(self.telegram_json or "{}"))

    @telegram.setter
    def telegram(self, value: dict) -> None:
        self.telegram_json = json.dumps(value)

    def telegram_public(self) -> dict:
        """The bot token never goes back to the browser -- only whether one
        is set and a masked hint so the user can tell which one it is."""
        tg = self.telegram
        token = tg.pop("bot_token", "") or ""
        tg["bot_token_set"] = bool(token)
        tg["bot_token_hint"] = (token[:6] + "..." + token[-4:]) if len(token) > 12 else ""
        return tg


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_json: Mapped[str] = mapped_column(Text, default="{}")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    # Commands queued from the dashboard ("test_sound"), drained by the
    # agent's next heartbeat.
    commands_json: Mapped[str] = mapped_column(Text, default="[]")

    user: Mapped[User] = relationship(back_populates="devices")
    events: Mapped[list["Event"]] = relationship(back_populates="device",
                                                 cascade="all, delete-orphan")

    @property
    def settings(self) -> dict:
        return _merge(DEVICE_DEFAULTS, json.loads(self.settings_json or "{}"))

    @settings.setter
    def settings(self, value: dict) -> None:
        self.settings_json = json.dumps(value)

    @property
    def status(self) -> dict:
        return json.loads(self.status_json or "{}")

    def online(self, now: float | None = None) -> bool:
        return bool(self.last_seen) and (now or time.time()) - self.last_seen < 20

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "online": self.online(),
            "status": self.status,
            "settings": self.settings,
        }


class Event(Base):
    """One couch session: from the dog getting on until it gets off."""
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("device_id", "client_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    # Generated by the agent, so retried uploads are idempotent and the
    # agent never has to wait on the server to learn an id.
    client_id: Mapped[str] = mapped_column(String(64))
    start_ts: Mapped[float] = mapped_column(Float, index=True)
    end_ts: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    video_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    video_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    device: Mapped[Device] = relationship(back_populates="events")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "device_name": self.device.name if self.device else None,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration": self.duration,
            "confidence": self.confidence,
            "has_snapshot": bool(self.snapshot_key),
            "has_video": bool(self.video_key),
            "video_size": self.video_size,
            "notified": self.notified,
        }


class Sound(Base):
    __tablename__ = "sounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    user: Mapped[User] = relationship(back_populates="sounds")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "size": self.size,
            "duration_sec": self.duration_sec,
            "created_at": self.created_at,
        }


def make_session_factory(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        from pathlib import Path
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Request threads, the stream generator and the retention sweeper
        # all share the engine.
        kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(url, pool_pre_ping=True, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _):
            cur = conn.cursor()
            # Without this, ON DELETE CASCADE is silently ignored by SQLite.
            cur.execute("PRAGMA foreign_keys=ON")
            # WAL lets the dashboard read while an agent is writing.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    Base.metadata.create_all(engine)
    return engine, scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
