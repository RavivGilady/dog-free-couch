"""Frame publishing, now that capture and detection run in separate threads.

The live view no longer waits for a MobileNet-SSD pass: the capture thread
publishes each frame and readers block on a condition until one arrives.
These tests cover that handover and the rates around it -- no camera, no
network, no server.
"""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service import MonitorService


class FakeSettings:
    def all(self):
        return {"video": {}}


def make_service(config=None):
    tmp = Path(tempfile.mkdtemp())
    cfg = {"alert": {"video_dir": str(tmp)}}
    for section, values in (config or {}).items():
        cfg.setdefault(section, {}).update(values)
    return MonitorService(cfg, store=None, settings=FakeSettings()), tmp


def publish(svc, jpeg):
    """What the capture loop does once a frame is encoded."""
    with svc._frame_cv:
        svc._latest_jpeg = jpeg
        svc._frame_seq += 1
        svc._frame_cv.notify_all()


def test_waiting_reader_is_woken_by_the_next_frame():
    svc, tmp = make_service()
    try:
        got = []
        reader = threading.Thread(
            target=lambda: got.append(svc.wait_for_jpeg(0, timeout=5.0)))
        reader.start()
        # The reader is parked on the condition, not polling a timer, so the
        # frame reaches it as soon as it exists.
        time.sleep(0.05)
        publish(svc, b"\xff\xd8first")
        reader.join(timeout=5)

        assert got == [(b"\xff\xd8first", 1)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_reader_already_behind_does_not_wait_at_all():
    svc, tmp = make_service()
    try:
        publish(svc, b"\xff\xd8one")
        publish(svc, b"\xff\xd8two")

        t0 = time.time()
        jpeg, seq = svc.wait_for_jpeg(0, timeout=5.0)
        # Newest frame only: a reader that fell behind skips what it missed
        # rather than draining a backlog into a live view.
        assert (jpeg, seq) == (b"\xff\xd8two", 2)
        assert time.time() - t0 < 1.0

        # Caught up now, so the next call has to wait -- and time out.
        assert svc.wait_for_jpeg(seq, timeout=0.05) == (None, 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_timeout_reports_nothing_new_instead_of_blocking_forever():
    """A dead camera has to surface to the caller, not hang the thread."""
    svc, tmp = make_service()
    try:
        t0 = time.time()
        assert svc.wait_for_jpeg(0, timeout=0.1) == (None, 0)
        assert time.time() - t0 >= 0.1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clip_frame_rate_is_independent_of_the_capture_rate():
    """Clips are written -- and fed -- at alert.video_fps, so a 30fps camera
    does not produce clips that play back at double speed."""
    svc, tmp = make_service({"camera": {"fps": 30}, "alert": {"video_fps": 15}})
    try:
        assert svc.clip_fps == 15
        assert svc.recorder.fps == 15
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_detection_rate_ceiling_defaults_on_and_can_be_switched_off():
    svc, tmp = make_service()
    try:
        assert svc.max_detect_fps == 12  # a sane default for the debouncer
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    svc, tmp = make_service({"model": {"max_detect_fps": 0}})
    try:
        assert svc.max_detect_fps == 0  # "as fast as the machine manages"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stop_releases_a_detector_waiting_for_a_frame():
    """Both loops park on a condition; stop() has to signal them instead of
    leaving the caller to wait out their timeouts."""
    svc, tmp = make_service()
    try:
        svc._detect_thread = threading.Thread(target=svc._detect_loop,
                                              name="detector", daemon=True)
        svc._detect_thread.start()
        time.sleep(0.05)
        assert svc._detect_thread.is_alive()

        svc.stop()
        svc._detect_thread.join(timeout=5)
        assert not svc._detect_thread.is_alive()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
