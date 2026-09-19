"""
Camera source abstraction.

Today this runs against a laptop webcam via OpenCV's VideoCapture. Later,
on a Raspberry Pi with the official camera module, you can switch the
`camera.backend` config value to "picamera2" without touching any other
code (detector, zone, monitor logic are all camera-agnostic).
"""
from __future__ import annotations

import abc
import sys
import time


class CameraSource(abc.ABC):
    """Common interface every camera backend implements."""

    @abc.abstractmethod
    def read(self):
        """Return a BGR numpy frame, or None if a frame couldn't be grabbed."""
        raise NotImplementedError

    @abc.abstractmethod
    def release(self):
        raise NotImplementedError


class OpenCVCamera(CameraSource):
    """USB / built-in webcam via OpenCV. Works on a laptop and on a Pi with
    a USB webcam (or the Pi camera exposed as /dev/video0 via bcm2835-v4l2)."""

    # On Windows, OpenCV's default backend (Media Foundation) sometimes hands
    # back a static "no signal" placeholder frame instead of a real error --
    # isOpened() is True and read() succeeds, but every frame is identical
    # and near-uniform. DirectShow (CAP_DSHOW) is the classic fix, so it's
    # tried first there; other platforms use OpenCV's normal auto-detection.
    _BACKEND_CANDIDATES = {
        "win32": ["dshow", "any"],
        "default": ["any"],
    }
    _BACKEND_FLAGS = {}  # filled in lazily once cv2 is imported

    def __init__(self, index: int = 0, width: int = 640, height: int = 480,
                 warmup_frames: int = 5, backend: str | None = None):
        import cv2  # local import so this module can be inspected without cv2 installed

        self._cv2 = cv2
        if not self._BACKEND_FLAGS:
            self._BACKEND_FLAGS.update({
                "any": cv2.CAP_ANY,
                "dshow": getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY),
                "msmf": getattr(cv2, "CAP_MSMF", cv2.CAP_ANY),
            })

        backends_to_try = [backend] if backend else self._BACKEND_CANDIDATES.get(
            sys.platform, self._BACKEND_CANDIDATES["default"]
        )

        self.cap = None
        tried = []
        for name in backends_to_try:
            flag = self._BACKEND_FLAGS.get(name, cv2.CAP_ANY)
            cap = cv2.VideoCapture(index, flag)
            tried.append(name)
            if cap.isOpened():
                self.cap = cap
                break
            cap.release()

        if self.cap is None:
            raise RuntimeError(
                f"Could not open camera at index {index} (tried backend(s): {tried}). "
                "Is it plugged in / not in use by another app? On macOS you may also "
                "need to grant camera permission to your terminal."
            )

        if width:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        # Let auto-exposure/white-balance settle, and grab a real frame to
        # sanity-check the feed isn't a dead/placeholder image.
        last_frame = None
        for _ in range(warmup_frames):
            ok, last_frame = self.cap.read()
            time.sleep(0.05)

        if last_frame is not None and _looks_like_dead_frame(last_frame):
            print(
                f"Warning: camera index {index} opened but frames look like a "
                "static placeholder (very low variation), not a live feed. "
                "Run list_cameras.py to check other indices, or set "
                "camera.backend_api in config.yaml explicitly.",
                file=sys.stderr,
            )

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        return frame

    def release(self):
        self.cap.release()


def _looks_like_dead_frame(frame, std_threshold: float = 3.0) -> bool:
    """Heuristic: a real camera frame has meaningful pixel variation; a
    static "no signal" placeholder is almost perfectly flat/uniform."""
    return float(frame.std()) < std_threshold


class PiCamera2Source(CameraSource):
    """Native Raspberry Pi camera module via picamera2 (libcamera).

    Only import/instantiate this on a Pi with `picamera2` installed
    (typically via `sudo apt install -y python3-picamera2`, not pip).
    """

    def __init__(self, width: int = 640, height: int = 480):
        from picamera2 import Picamera2  # type: ignore

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": (width, height), "format": "RGB888"}
        )
        self.picam2.configure(config)
        self.picam2.start()
        time.sleep(1.0)  # sensor warmup

    def read(self):
        # picamera2 gives RGB; downstream (OpenCV DNN) expects BGR.
        import cv2

        frame = self.picam2.capture_array()
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def release(self):
        self.picam2.stop()


def get_camera(config: dict) -> CameraSource:
    """Factory: build the configured camera backend."""
    cam_cfg = config.get("camera", {})
    backend = cam_cfg.get("backend", "opencv")
    width = cam_cfg.get("width", 640)
    height = cam_cfg.get("height", 480)

    if backend == "opencv":
        return OpenCVCamera(
            index=cam_cfg.get("index", 0),
            width=width,
            height=height,
            backend=cam_cfg.get("backend_api"),  # e.g. "dshow" / "msmf" / "any"; auto-picked if unset
        )
    elif backend == "picamera2":
        return PiCamera2Source(width=width, height=height)
    else:
        raise ValueError(f"Unknown camera backend: {backend!r}")
