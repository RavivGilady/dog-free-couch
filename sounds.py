"""
Alert sound library.

"builtin" is the synthesized square-wave alarm from alert.py; everything
else is a .wav the user recorded in the dashboard. Recordings arrive from
the browser already encoded as WAV (MediaRecorder gives webm/opus, which
winsound can't play and ffmpeg isn't available to convert, so the page
decodes and re-encodes to WAV client-side before uploading).
"""
from __future__ import annotations

import re
import wave
from datetime import datetime
from pathlib import Path

SOUNDS_DIR = Path("sounds")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def ensure_dir() -> Path:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    return SOUNDS_DIR


def is_safe_name(name: str) -> bool:
    """Guard the filename before it ever touches the filesystem -- this
    value comes from the browser, so '../' must never resolve anywhere."""
    return bool(name) and bool(_SAFE_NAME.match(name)) and name.endswith(".wav")


def wav_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
            return {
                "duration_sec": round(frames / float(rate or 1), 2),
                "sample_rate": rate,
                "channels": w.getnchannels(),
            }
    except Exception:
        return {"duration_sec": None, "sample_rate": None, "channels": None}


def list_sounds(active: str = "builtin") -> list:
    ensure_dir()
    out = [{
        "name": "builtin",
        "label": "Built-in alarm (square wave)",
        "builtin": True,
        "active": active == "builtin",
        "size": None,
        "duration_sec": 0.66,
        "created": None,
    }]
    for p in sorted(SOUNDS_DIR.glob("*.wav"), key=lambda x: x.stat().st_mtime, reverse=True):
        info = wav_info(p)
        out.append({
            "name": p.name,
            "label": p.stem.replace("_", " "),
            "builtin": False,
            "active": active == p.name,
            "size": p.stat().st_size,
            "duration_sec": info["duration_sec"],
            "created": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return out


def new_recording_path(label: str = "") -> Path:
    ensure_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (label or "recording")).strip("_").lower()[:40] or "recording"
    return SOUNDS_DIR / f"{slug}_{stamp}.wav"


def resolve(name: str) -> Path | None:
    """Map a stored sound name to a path, refusing anything unsafe."""
    if not name or name == "builtin":
        return None
    if not is_safe_name(name):
        return None
    p = SOUNDS_DIR / name
    return p if p.exists() else None


def delete(name: str) -> bool:
    p = resolve(name)
    if p is None:
        return False
    p.unlink()
    return True
