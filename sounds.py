"""
Local alert sound cache.

The built-in sirens are synthesized on the fly (see sirens.py); everything
else is a .wav recorded in the dashboard and downloaded by the agent into
sounds/ (see cloud_client.AgentLink._sync_sound). The browser converts
recordings to WAV before upload, because winsound/aplay can't play the
webm/opus that MediaRecorder produces.
"""
from __future__ import annotations

import re
from pathlib import Path

import sirens

SOUNDS_DIR = Path("sounds")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def ensure_dir() -> Path:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    return SOUNDS_DIR


def is_safe_name(name: str) -> bool:
    """Guard the filename before it ever touches the filesystem -- names
    are derived from server replies, so '../' must never resolve anywhere."""
    return bool(name) and bool(_SAFE_NAME.match(name)) and name.endswith(".wav")


def resolve(name: str) -> Path | None:
    """Map a stored sound name to a cached file, refusing anything unsafe.

    None means "no recorded sound": the caller plays a built-in siren.
    """
    if not name or sirens.is_builtin(name):
        return None
    if not is_safe_name(name):
        return None
    p = SOUNDS_DIR / name
    return p if p.exists() else None
