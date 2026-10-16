"""Shared pieces for the mocked-camera tests and the live demo.

The camera is fake -- it replays a real photo of a dog on a couch, or an
empty frame -- but everything downstream is the real thing: the real
MobileNet-SSD detector, the real MonitorService threads, zone scaling,
debounce, snapshots and clip recording.
"""
import threading
import time
from pathlib import Path

import cv2
import numpy as np

import service

ROOT = Path(__file__).resolve().parent.parent
MODEL = {"prototxt": str(ROOT / "models" / "MobileNetSSD_deploy.prototxt"),
         "weights": str(ROOT / "models" / "MobileNetSSD_deploy.caffemodel")}
MODEL_AVAILABLE = all(Path(p).exists() for p in MODEL.values())

W, H = 640, 422  # even: video codecs drop an odd row
# Public-domain photo (DPLA / Wikimedia Commons): a dog sitting on a couch.
DOG_PHOTO = Path(__file__).resolve().parent / "assets" / "dog_on_couch.jpg"
# A zone covering the couch and the dog on it, as fractions of the frame.
COUCH_ZONE = [(0.15, 0.2), (0.95, 0.2), (0.95, 0.95), (0.15, 0.95)]
# A zone in the top-left corner, nowhere near the dog.
FAR_ZONE = [(0.0, 0.0), (0.1, 0.0), (0.1, 0.1), (0.0, 0.1)]


def dog_frame():
    return cv2.resize(cv2.imread(str(DOG_PHOTO)), (W, H))


def empty_frame():
    """The room with nobody on the couch: a flat, featureless frame."""
    return np.full((H, W, 3), 90, np.uint8)


class FakeCamera:
    """Replays whatever is set with show(), at roughly `fps`."""

    def __init__(self, frame, fps=30):
        self.frame = frame
        self.delay = 1.0 / fps
        self.released = False

    def show(self, frame):
        self.frame = frame

    def read(self):
        time.sleep(self.delay)
        return self.frame.copy()

    def release(self):
        self.released = True


class FakeSettings:
    def __init__(self, video=None, record=False):
        self.video = video or {}
        self.record = record

    def all(self):
        return {"video": self.video, "alert": {"play_sound": False}, "telegram": {}}

    def get(self, section, key, default=None):
        if (section, key) == ("video", "enabled"):
            return self.record
        return default


class FakeStore:
    """Stands in for the cloud store; records what the service reports."""

    def __init__(self):
        self.entered, self.left, self.videos = [], [], {}
        self._lock = threading.Lock()

    def add_entered(self, start, confidence, snapshot_path):
        with self._lock:
            self.entered.append({"start": start, "confidence": confidence,
                                 "snapshot": snapshot_path})
            return len(self.entered)

    def add_left(self, start, end, duration):
        with self._lock:
            self.left.append({"start": start, "end": end, "duration": duration})

    def mark_notified(self, event_id):
        pass

    def attach_video(self, event_id, path):
        with self._lock:
            self.videos[event_id] = path


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def start_service(monkeypatch, tmp, camera, *, record=False, video=None,
                  debounce=None, store=None):
    """A running MonitorService fed by `camera`, using the real detector."""
    monkeypatch.setattr(service, "get_camera", lambda config: camera)
    monkeypatch.setattr(service, "play_alert_sound", lambda *a, **k: None)
    config = {
        "alert": {"video_dir": str(tmp / "videos"), "snapshot_dir": str(tmp / "snaps"),
                  "video_fps": 15},
        "debounce": {"enter_frames": 3, "exit_frames": 3,
                     "min_alert_interval_sec": 0, **(debounce or {})},
        "model": {**MODEL, "max_detect_fps": 10},
    }
    store = store or FakeStore()
    svc = service.MonitorService(config, store=store,
                                 settings=FakeSettings(video, record))
    svc.start()
    assert wait_for(lambda: svc.status["running"] and svc.status["fps"] > 0), \
        svc.status["last_error"]
    return svc, store
