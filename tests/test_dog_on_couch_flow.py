"""End to end with a mocked camera: a real dog photo, the real detector.

The camera is the only fake -- it replays a photo of a dog on a couch (or an
empty frame). Detection is the real MobileNet-SSD model, so these tests also
prove the model finds the dog; they are skipped where the weights have not
been downloaded (python download_model.py).
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import flow_fakes as ff
from flow_fakes import wait_for

pytestmark = pytest.mark.skipif(
    not ff.MODEL_AVAILABLE, reason="model weights missing: run download_model.py")

# Short clip padding keeps the long tests quick; the logic is the same.
VIDEO = {"pre_roll_sec": 2, "post_roll_sec": 1, "max_clip_sec": 60}
FPS = 15


@pytest.fixture
def tmp():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def running(monkeypatch, tmp):
    """Start services through here so they are always stopped."""
    started = []

    def start(camera, **kw):
        svc, store = ff.start_service(monkeypatch, tmp, camera, **kw)
        started.append(svc)
        return svc, store

    yield start
    for s in started:
        s.stop()


# ---------- helpers for reading a recorded clip back ----------

def read_clip(path):
    cap = cv2.VideoCapture(path)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return frames, fps


def classify(frames):
    """'dog' / 'empty' for each frame, by which reference picture it is
    closer to (robust to video compression)."""
    refs = {"dog": ff.dog_frame().astype(np.float32),
            "empty": ff.empty_frame().astype(np.float32)}
    out = []
    for f in frames:
        d = {k: float(np.abs(f.astype(np.float32) - r).mean()) for k, r in refs.items()}
        out.append(min(d, key=d.get))
    return out


def runs(labels):
    """['empty','empty','dog'] -> [('empty', 2), ('dog', 1)]"""
    out = []
    for label in labels:
        if out and out[-1][0] == label:
            out[-1] = (label, out[-1][1] + 1)
        else:
            out.append((label, 1))
    return out


# ---------- the zone -> alert hand-off ----------

def test_real_dog_photo_triggers_alert_once_zone_is_set(running):
    cam = ff.FakeCamera(ff.dog_frame())
    svc, store = running(cam)

    # The real model sees the dog, but with no couch zone nothing counts.
    assert wait_for(lambda: svc.status["dogs_in_frame"] == 1)
    time.sleep(0.5)
    assert svc.status["dog_on_couch"] is False
    assert store.entered == []

    # Draw the zone over the couch, as the dashboard does.
    svc.update_zone(ff.COUCH_ZONE, 0.35)

    assert wait_for(lambda: len(store.entered) == 1)
    assert svc.status["dog_on_couch"] is True
    assert store.entered[0]["confidence"] > 0.5
    snap = cv2.imread(store.entered[0]["snapshot"])
    assert snap is not None and snap.shape[:2] == (ff.H, ff.W)  # the saved photo

    # The live view boxes the dog as in-zone: red pixels around the box.
    jpeg, _ = svc.wait_for_jpeg(0, timeout=2.0)
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    red = (img[..., 2] > 200) & (img[..., 1] < 60) & (img[..., 0] < 60)
    assert red.sum() > 200


def test_zone_away_from_the_dog_never_alerts(running):
    svc, store = running(ff.FakeCamera(ff.dog_frame()))
    svc.update_zone(ff.FAR_ZONE, 0.35)

    assert wait_for(lambda: svc.status["dogs_in_frame"] == 1)
    time.sleep(1.0)
    assert svc.status["dog_on_couch"] is False
    assert store.entered == []


# ---------- events and their clips ----------

def test_one_long_stay_is_one_event_with_pre_and_post_roll(running):
    """A dog that sits for a long time is a single event and a single clip:
    a few seconds of empty couch before it jumped up, the whole stay, and a
    little after it left."""
    cam = ff.FakeCamera(ff.empty_frame())
    svc, store = running(cam, record=True, video=VIDEO)
    svc.update_zone(ff.COUCH_ZONE, 0.35)
    time.sleep(3.0)                       # empty couch fills the pre-roll buffer

    stay = 10.0
    cam.show(ff.dog_frame())
    time.sleep(stay)
    cam.show(ff.empty_frame())

    assert wait_for(lambda: len(store.left) == 1 and len(store.videos) == 1, 15)
    time.sleep(1.0)                       # long enough for a bogus extra event

    assert len(store.entered) == 1 and len(store.left) == 1
    assert store.left[0]["duration"] == pytest.approx(stay, abs=2.0)

    frames, fps = read_clip(store.videos[1])
    assert fps == pytest.approx(FPS, abs=1)
    labels = classify(frames)
    shape = runs(labels)
    # One contiguous shot: empty (pre-roll) -> dog (the stay) -> empty (post-roll).
    assert [s for s, _ in shape] == ["empty", "dog", "empty"], shape
    before, dog, after = (n for _, n in shape)
    assert before >= 0.5 * VIDEO["pre_roll_sec"] * FPS          # saw it coming
    assert after >= 0.3 * VIDEO["post_roll_sec"] * FPS          # saw it leave
    assert dog >= 0.8 * stay * FPS                              # the whole stay
    assert len(frames) / fps == pytest.approx(stay + 3.0, abs=2.5)


def test_dog_back_on_the_couch_after_a_gap_is_a_second_event(running):
    """Dog leaves, the first clip closes, dog hops back up: that is a new
    event with its own clip, not a continuation of the first."""
    cam = ff.FakeCamera(ff.empty_frame())
    svc, store = running(cam, record=True, video=VIDEO,
                         debounce={"min_alert_interval_sec": 1})
    svc.update_zone(ff.COUCH_ZONE, 0.35)
    time.sleep(2.0)

    cam.show(ff.dog_frame())
    assert wait_for(lambda: len(store.entered) == 1)
    time.sleep(3.0)
    cam.show(ff.empty_frame())
    assert wait_for(lambda: len(store.left) == 1 and len(store.videos) == 1, 10)

    time.sleep(1.5)                       # a short breather, then back up
    cam.show(ff.dog_frame())
    assert wait_for(lambda: len(store.entered) == 2, 10)
    time.sleep(3.0)
    cam.show(ff.empty_frame())
    assert wait_for(lambda: len(store.left) == 2 and len(store.videos) == 2, 15)

    assert len(store.entered) == 2 and len(store.left) == 2
    assert store.entered[1]["start"] > store.left[0]["end"]
    first, second = store.videos[1], store.videos[2]
    assert first != second
    for path in (first, second):
        frames, _ = read_clip(path)
        assert "dog" in classify(frames)  # each clip really shows the dog


@pytest.mark.xfail(strict=True, reason=(
    "known gap: if the dog is back on the couch while the first clip is still "
    "in its post-roll, recorder.start() returns the open clip, so the second "
    "event never gets a video of its own"))
def test_dog_back_during_post_roll_still_gets_its_own_clip(running):
    cam = ff.FakeCamera(ff.empty_frame())
    svc, store = running(cam, record=True, video=VIDEO,
                         debounce={"min_alert_interval_sec": 1})
    svc.update_zone(ff.COUCH_ZONE, 0.35)
    time.sleep(2.0)

    cam.show(ff.dog_frame())
    time.sleep(3.0)
    cam.show(ff.empty_frame())
    time.sleep(0.6)                       # back before the 1s post-roll is over
    cam.show(ff.dog_frame())
    time.sleep(3.0)
    cam.show(ff.empty_frame())
    assert wait_for(lambda: len(store.left) == 2, 10)
    time.sleep(2.0)

    assert len(store.entered) == 2        # the alert itself does fire again
    assert len(store.videos) == 2
