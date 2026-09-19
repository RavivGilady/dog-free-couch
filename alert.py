"""
What happens when the dog is confirmed on the couch: a log line, a saved
snapshot, a local sound, and (optionally) a Telegram push notification so
it reaches your phone even if you're not near the laptop/Pi.
"""
from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path


def log_event(csv_path: str, event: dict, confidence: float | None = None,
              snapshot_path: str | None = None) -> None:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["event", "start", "end", "duration_sec", "confidence", "snapshot"])
        writer.writerow([
            event.get("type"),
            _fmt(event.get("start")),
            _fmt(event.get("end")),
            round(event["duration"], 1) if "duration" in event else "",
            round(confidence, 2) if confidence is not None else "",
            snapshot_path or "",
        ])


def _fmt(ts):
    if ts is None:
        return ""
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def save_snapshot(frame, snapshot_dir: str) -> str:
    import cv2

    path = Path(snapshot_dir)
    path.mkdir(parents=True, exist_ok=True)
    filename = f"dog_on_couch_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    full_path = path / filename
    cv2.imwrite(str(full_path), frame)
    return str(full_path)


def play_alert_sound() -> None:
    """Best-effort local alert sound. Never raises -- a missing audio
    backend shouldn't take down the monitor."""
    try:
        if sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        elif sys.platform.startswith("linux"):
            import subprocess
            # 'paplay' with a common system sound; falls back to a terminal bell.
            candidates = [
                "/usr/share/sounds/freedesktop/stereo/complete.oga",
                "/usr/share/sounds/alsa/Front_Center.wav",
            ]
            played = False
            for sound in candidates:
                if Path(sound).exists():
                    subprocess.Popen(["paplay", sound])
                    played = True
                    break
            if not played:
                print("\a", end="", flush=True)
        elif sys.platform == "win32":
            import winsound  # type: ignore
            winsound.MessageBeep()
        else:
            print("\a", end="", flush=True)
    except Exception:
        print("\a", end="", flush=True)


def send_telegram_alert(bot_token: str, chat_id: str, message: str,
                         photo_path: str | None = None) -> None:
    """Send a Telegram message (and optional photo). Requires a bot token
    and chat id -- see README for the 2-minute setup. Never raises."""
    try:
        import requests

        base = f"https://api.telegram.org/bot{bot_token}"
        requests.post(f"{base}/sendMessage", data={"chat_id": chat_id, "text": message}, timeout=10)

        if photo_path and Path(photo_path).exists():
            with open(photo_path, "rb") as f:
                requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id},
                    files={"photo": f},
                    timeout=15,
                )
    except Exception as e:
        print(f"[alert] Telegram notification failed: {e}", file=sys.stderr)
