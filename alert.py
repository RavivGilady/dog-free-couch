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


# Cached path to the generated alarm WAV, built once on first use.
_ALARM_WAV = None


def _build_alarm_wav(beeps=3, freq=1400.0, beep_ms=160, gap_ms=90, volume=0.85):
    """Synthesize a short, loud two-tone alarm and cache it as a .wav.

    Deliberately NOT a system sound: Windows' MessageBeep() plays whatever
    the user's sound scheme maps to, which is often something soft (or
    nothing at all), and a subtle chirp is useless as a dog deterrent.
    A generated square wave is loud, unmistakable, and identical on every
    machine regardless of the user's sound settings.
    """
    import math
    import struct
    import tempfile
    import wave

    rate = 44100
    amp = int(32767 * max(0.0, min(1.0, volume)))
    frames = bytearray()

    def tone(f, ms):
        n = int(rate * ms / 1000)
        for i in range(n):
            # Square wave -- far more piercing than a sine at equal amplitude.
            v = amp if math.sin(2 * math.pi * f * i / rate) >= 0 else -amp
            # Brief fade in/out so the edges don't click.
            fade = min(1.0, i / 200.0, (n - i) / 200.0)
            frames.extend(struct.pack("<h", int(v * fade)))

    def silence(ms):
        frames.extend(bytes(2 * int(rate * ms / 1000)))

    for i in range(beeps):
        # Alternate two pitches -- a warble carries better than one flat note.
        tone(freq if i % 2 == 0 else freq * 0.75, beep_ms)
        if i < beeps - 1:
            silence(gap_ms)

    dest = Path(tempfile.gettempdir()) / "dog_couch_alarm.wav"
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return str(dest)


def get_alarm_wav():
    global _ALARM_WAV
    if _ALARM_WAV is None or not Path(_ALARM_WAV).exists():
        _ALARM_WAV = _build_alarm_wav()
    return _ALARM_WAV


def play_alert_sound(custom_wav=None):
    """Best-effort local alert sound. Never raises and never blocks -- a
    missing audio backend shouldn't take down (or stall) the monitor."""
    try:
        # A sound recorded in the dashboard wins; otherwise fall back to
        # the synthesized alarm so there is always *something* audible.
        wav = str(custom_wav) if custom_wav and Path(custom_wav).exists() else get_alarm_wav()

        if sys.platform == "win32":
            import winsound  # type: ignore
            # SND_ASYNC so the capture loop keeps running while it plays.
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return

        import shutil
        import subprocess
        for player in ("paplay", "aplay", "afplay"):
            if shutil.which(player):
                subprocess.Popen([player, wav],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
        print("", end="", flush=True)
    except Exception as e:
        print(f"[alert] sound failed ({e}); falling back to terminal bell",
              file=sys.stderr)
        print("", end="", flush=True)


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


def send_telegram_video(bot_token: str, chat_id: str, video_path: str,
                        caption: str = "") -> bool:
    """Send a recorded clip to Telegram. Never raises; returns success.

    Telegram caps bot uploads at 50MB -- a long clip can exceed that, so the
    size is checked up front rather than failing halfway through an upload.
    """
    try:
        import requests

        if not (bot_token and chat_id):
            return False
        path = Path(video_path)
        if not path.exists():
            print(f"[alert] clip missing, not sending: {video_path}", file=sys.stderr)
            return False

        size_mb = path.stat().st_size / 1e6
        if size_mb > 50:
            print(f"[alert] clip is {size_mb:.0f}MB, over Telegram's 50MB bot "
                  "limit -- sending a message instead", file=sys.stderr)
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data={"chat_id": chat_id,
                      "text": f"{caption} (clip too large to send: {size_mb:.0f}MB)"},
                timeout=10)
            return False

        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendVideo",
                data={"chat_id": chat_id, "caption": caption},
                files={"video": f},
                timeout=120,
            )
        if not r.ok:
            print(f"[alert] Telegram sendVideo failed: {r.status_code} {r.text[:200]}",
                  file=sys.stderr)
        return r.ok
    except Exception as e:
        print(f"[alert] Telegram video send failed: {e}", file=sys.stderr)
        return False


def telegram_check(bot_token: str, chat_id: str) -> tuple:
    """Validate credentials for the dashboard's 'Test connection' button.

    Returns (ok, message) so the UI can show precisely what went wrong
    instead of a generic failure.
    """
    try:
        import requests

        if not bot_token:
            return False, "No bot token set."
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=10)
        if not r.ok:
            return False, f"Token rejected by Telegram (HTTP {r.status_code})."
        name = r.json().get("result", {}).get("username", "?")
        if not chat_id:
            return False, f"Bot @{name} is valid, but no chat id is set."

        r2 = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": "Dog couch monitor: test message."},
            timeout=10)
        if not r2.ok:
            return False, (f"Bot @{name} is valid but sending to chat {chat_id} failed "
                           f"(HTTP {r2.status_code}). Have you messaged the bot first?")
        return True, f"Sent a test message via @{name}."
    except Exception as e:
        return False, f"Could not reach Telegram: {e}"
