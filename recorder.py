"""
Event video clips.

Two things make this more than "open a VideoWriter on alert":

1. Pre-roll. By the time the debouncer confirms a dog is on the couch,
   the interesting part (the jump) already happened. A ring buffer keeps
   the last few seconds of frames around so the clip starts *before* the
   event.

2. Codec. Browsers only play H.264/VP8/VP9 in a <video> tag, and OpenCV's
   H.264 support depends on an openh264 library that isn't always present
   on Windows. So the codec is probed once at startup and falls back to
   WebM/VP8 rather than silently writing a file the dashboard can't play.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# Ordered by preference: all are playable in a browser <video> element.
_CODEC_CANDIDATES = [("avc1", ".mp4"), ("VP80", ".webm"), ("mp4v", ".mp4")]

_probed: tuple | None = None


def probe_codec(verbose: bool = True) -> tuple:
    """Find a codec this machine can actually encode AND decode back.

    isOpened() alone lies -- on a box without openh264 it can return True
    and still produce an unusable file -- so each candidate is written and
    read back before it's trusted.
    """
    global _probed
    if _probed is not None:
        return _probed

    import cv2
    import numpy as np
    import tempfile

    frames = [np.full((120, 160, 3), 128, dtype=np.uint8) for _ in range(5)]
    tmpdir = Path(tempfile.mkdtemp())

    for fourcc, ext in _CODEC_CANDIDATES:
        path = tmpdir / f"probe{ext}"
        try:
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), 10.0, (160, 120))
            if not w.isOpened():
                w.release()
                continue
            for f in frames:
                w.write(f)
            w.release()

            cap = cv2.VideoCapture(str(path))
            ok, _ = cap.read()
            cap.release()
            if ok and path.stat().st_size > 0:
                _probed = (fourcc, ext)
                if verbose:
                    print(f"[recorder] using codec {fourcc} ({ext})")
                return _probed
        except Exception:
            continue
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    if verbose:
        print("[recorder] WARNING: no browser-playable codec found; "
              "clips will be written as MJPEG .avi and may not play in the "
              "dashboard.", file=sys.stderr)
    _probed = ("MJPG", ".avi")
    return _probed


class ClipRecorder:
    """Ring-buffered clip recorder. Feed it every frame; it decides what to keep.

    Call feed() once per frame, start() when an event fires, and stop() when
    it ends. Writing happens on a worker thread so the capture loop is never
    blocked by disk I/O or encoding.
    """

    def __init__(self, out_dir: str = "videos", fps: float = 15.0,
                 pre_roll_sec: float = 4.0, max_clip_sec: float = 60.0,
                 post_roll_sec: float = 3.0):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.fps = max(1.0, float(fps))
        self.pre_roll_sec = pre_roll_sec
        self.max_clip_sec = max_clip_sec
        self.post_roll_sec = post_roll_sec

        self._buffer = deque(maxlen=max(1, int(self.fps * pre_roll_sec)))
        self._lock = threading.Lock()
        self._writer = None
        self._path = None
        self._started_at = None
        self._stopping_at = None
        self._frames_written = 0
        self._on_done = None

    @property
    def recording(self) -> bool:
        return self._writer is not None

    def feed(self, frame) -> None:
        """Every frame goes here: buffered when idle, written when recording."""
        with self._lock:
            if self._writer is None:
                self._buffer.append(frame.copy())
                return

            self._writer.write(frame)
            self._frames_written += 1
            now = time.time()

            # Hard cap so a dog that naps all afternoon can't fill the disk.
            if now - self._started_at >= self.max_clip_sec:
                self._finish_locked()
                return

            # Post-roll: keep rolling briefly after it leaves, so the clip
            # shows the dog actually getting off.
            if self._stopping_at is not None and now >= self._stopping_at:
                self._finish_locked()

    def start(self, frame_shape, on_done=None) -> str | None:
        """Begin a clip, seeded with the buffered pre-roll frames."""
        import cv2

        with self._lock:
            if self._writer is not None:
                return self._path

            fourcc, ext = probe_codec()
            name = f"dog_on_couch_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
            path = self.out_dir / name
            h, w = frame_shape[:2]

            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc),
                                     self.fps, (w, h))
            if not writer.isOpened():
                print(f"[recorder] could not open VideoWriter for {path}", file=sys.stderr)
                return None

            self._writer = writer
            self._path = str(path)
            self._started_at = time.time()
            self._stopping_at = None
            self._frames_written = 0
            self._on_done = on_done

            for buffered in self._buffer:
                writer.write(buffered)
                self._frames_written += 1
            self._buffer.clear()

            return self._path

    def stop(self) -> None:
        """Ask the clip to end after post_roll_sec more frames."""
        with self._lock:
            if self._writer is None:
                return
            if self._stopping_at is None:
                self._stopping_at = time.time() + self.post_roll_sec

    def finish_now(self) -> None:
        with self._lock:
            self._finish_locked()

    def _finish_locked(self) -> None:
        if self._writer is None:
            return
        self._writer.release()
        path, cb, n = self._path, self._on_done, self._frames_written
        self._writer = None
        self._path = None
        self._stopping_at = None
        self._on_done = None

        if cb:
            # Callback off the lock's critical path, on its own thread, so a
            # slow Telegram upload can't stall the capture loop.
            threading.Thread(target=cb, args=(path, n), daemon=True).start()
