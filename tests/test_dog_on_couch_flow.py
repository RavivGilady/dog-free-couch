"""End to end, with a mocked camera: show a dog picture, set the zone, and the
couch alert fires on its own.

The camera replays one synthetic "dog picture" and the detector is a stand-in
that finds the dog in it by colour (the real MobileNet-SSD weights are a
download, not part of the repo). Everything else is the real MonitorService:
capture thread, detection thread, zone scaling, debounce, snapshot, event
store. No hardware, no model, no network.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import service
from detector import Detection
from service import MonitorService

W, H = 640, 480
DOG_BGR = (40, 90, 150)       # a brown dog
DOG_BOX = (200, 220, 360, 340)  # x1, y1, x2, y2 where the dog sits


def dog_picture():
    frame = np.full((H, W, 3), 200, np.uint8)
    x1, y1, x2, y2 = DOG_BOX
    frame[y1:y2, x1:x2] = DOG_BGR
    return frame


class FakeCamera:
    def __init__(self, frame):
        self.frame = frame
        self.released = False

    def read(self):
        time.sleep(0.01)  # a camera is not infinitely fast
        return self.frame.copy()

    def release(self):
        self.released = True


class ColourDogDetector:
    """Reports a dog wherever the picture has dog-coloured pixels."""

    def __init__(self, *args, **kwargs):
        pass

    def detect(self, frame, wanted_labels=None):
        ys, xs = np.where((frame == DOG_BGR).all(axis=2))
        if not len(xs):
            return []
        box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        return [Detection("dog", 0.9, box)]


class FakeSettings:
    def all(self):
        return {"video": {}, "alert": {"play_sound": False}, "telegram": {}}

    def get(self, section, key, default=None):
        return False if (section, key) == ("video", "enabled") else default


class FakeStore:
    def __init__(self):
        self.entered = []
        self.left = []

    def add_entered(self, start, confidence, snapshot_path):
        self.entered.append((start, confidence, snapshot_path))
        return len(self.entered)

    def add_left(self, *args):
        self.left.append(args)

    def mark_notified(self, event_id):
        pass

    def attach_video(self, event_id, path):
        pass


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def start_service(monkeypatch, tmp, picture):
    monkeypatch.setattr(service, "get_camera", lambda config: FakeCamera(picture))
    monkeypatch.setattr(service, "Detector", ColourDogDetector)
    config = {
        "alert": {"video_dir": str(tmp / "videos"), "snapshot_dir": str(tmp / "snaps")},
        "debounce": {"enter_frames": 3, "exit_frames": 3, "min_alert_interval_sec": 0},
        "model": {"max_detect_fps": 0},
    }
    store = FakeStore()
    svc = MonitorService(config, store=store, settings=FakeSettings())
    svc.start()
    assert wait_for(lambda: svc.status["running"] and svc.status["fps"] > 0)
    return svc, store


def test_dog_picture_triggers_alert_once_zone_is_set(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    svc = None
    try:
        svc, store = start_service(monkeypatch, tmp, dog_picture())

        # The dog is in view, but there is no couch zone yet: seen, not alerted.
        assert wait_for(lambda: svc.status["dogs_in_frame"] == 1)
        time.sleep(0.3)
        assert svc.status["dog_on_couch"] is False
        assert store.entered == []

        # Draw the zone over the dog, as fractions of the frame, like the dashboard.
        svc.update_zone([(0.25, 0.4), (0.7, 0.4), (0.7, 0.8), (0.25, 0.8)], 0.35)

        assert wait_for(lambda: len(store.entered) == 1)
        assert svc.status["dog_on_couch"] is True
        _, confidence, snapshot = store.entered[0]
        assert confidence == 0.9
        assert snapshot and Path(snapshot).exists()  # the dog photo was saved
        assert len(svc.status["zone_points"]) == 4

        # The live view now shows the dog boxed as in-zone.
        import cv2
        jpeg, _ = svc.wait_for_jpeg(0, timeout=2.0)
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape == (H, W, 3)
        x1, y1, _, _ = DOG_BOX
        b, g, r = (int(v) for v in img[y1, x1 + 40])  # on the top edge of the box
        assert r > 150 and g < 100 and b < 100  # red = in zone
    finally:
        if svc:
            svc.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def test_zone_away_from_the_dog_never_alerts(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    svc = None
    try:
        svc, store = start_service(monkeypatch, tmp, dog_picture())
        # Couch in the top-left corner; the dog is elsewhere.
        svc.update_zone([(0.0, 0.0), (0.2, 0.0), (0.2, 0.2), (0.0, 0.2)], 0.35)

        assert wait_for(lambda: svc.status["dogs_in_frame"] == 1)
        time.sleep(0.5)
        assert svc.status["dog_on_couch"] is False
        assert store.entered == []
    finally:
        if svc:
            svc.stop()
        shutil.rmtree(tmp, ignore_errors=True)
