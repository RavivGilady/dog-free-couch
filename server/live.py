"""
Live view relay.

The camera is at home and the server is on the internet, so the server can't
pull frames -- the agent pushes them. To avoid uploading video around the
clock, the agent only pushes while someone is actually watching: each viewer
stream marks the device as watched, and the agent learns that from its
heartbeat reply.

Frames are held in memory, which is why the server must run as a single
process (one gunicorn worker with many threads). That is plenty for a
household-scale deployment; scaling out would mean moving this into Redis.
"""
from __future__ import annotations

import threading
import time

VIEWER_TTL_SEC = 6.0


class LiveHub:
    def __init__(self):
        self._lock = threading.Condition()
        self._frames: dict[int, tuple[float, bytes]] = {}
        self._watched: dict[int, float] = {}

    def push(self, device_id: int, jpeg: bytes) -> None:
        with self._lock:
            self._frames[device_id] = (time.time(), jpeg)
            self._lock.notify_all()

    def mark_watched(self, device_id: int) -> None:
        with self._lock:
            self._watched[device_id] = time.time()

    def is_watched(self, device_id: int) -> bool:
        with self._lock:
            return time.time() - self._watched.get(device_id, 0) < VIEWER_TTL_SEC

    def latest(self, device_id: int):
        with self._lock:
            return self._frames.get(device_id)

    def wait_newer(self, device_id: int, after: float, timeout: float = 2.0):
        """Block until a frame newer than `after` arrives, or time out."""
        deadline = time.time() + timeout
        with self._lock:
            while True:
                cur = self._frames.get(device_id)
                if cur and cur[0] > after:
                    return cur
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)
