"""
Diagnostic tool: cycles through camera indices (and, on Windows, backend
APIs) so you can see which one is your real, working camera -- useful when
Windows has more than one registered "camera" device (built-in webcam,
IR/Windows Hello camera, virtual camera software, etc.) and OpenCV's
default pick isn't the one you want, or gives a frozen/placeholder image.

Usage:
    python list_cameras.py

Controls:
    n            try the next index
    b            try the next backend for the current index (Windows only)
    q / esc      quit
"""
from __future__ import annotations

import sys

import cv2

MAX_INDEX = 5
WINDOWS_BACKENDS = [
    ("any", cv2.CAP_ANY),
    ("dshow", getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY)),
    ("msmf", getattr(cv2, "CAP_MSMF", cv2.CAP_ANY)),
]
OTHER_BACKENDS = [("any", cv2.CAP_ANY)]


def try_open(index: int, backend_name: str, backend_flag: int):
    cap = cv2.VideoCapture(index, backend_flag)
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def main():
    backends = WINDOWS_BACKENDS if sys.platform == "win32" else OTHER_BACKENDS

    index = 0
    backend_i = 0
    cap = None

    def open_current():
        name, flag = backends[backend_i]
        c = try_open(index, name, flag)
        print(f"index={index} backend={name}: {'OK' if c else 'failed to open'}")
        return c

    cap = open_current()
    window = "list_cameras -- 'n' next index, 'b' next backend, 'q' quit"
    cv2.namedWindow(window)

    print("If you see a live, moving picture of the room, that's the config "
          "to use. Note the index (and backend, if shown) printed above.")

    try:
        while True:
            frame = None
            if cap is not None:
                ok, frame = cap.read()
                if not ok:
                    frame = None

            if frame is None:
                import numpy as np
                frame = np.zeros((240, 640, 3), dtype="uint8")
                cv2.putText(frame, "No frame (camera failed to open or read)",
                            (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

            name, _ = backends[backend_i]
            label = f"index={index}  backend={name}"
            cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.imshow(window, frame)

            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
            elif key == ord("n"):
                if cap is not None:
                    cap.release()
                index = (index + 1) % (MAX_INDEX + 1)
                backend_i = 0
                cap = open_current()
            elif key == ord("b") and sys.platform == "win32":
                if cap is not None:
                    cap.release()
                backend_i = (backend_i + 1) % len(backends)
                cap = open_current()
    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
