"""
One-time setup: click the corners of the couch in your camera view so the
monitor knows what "on the couch" means for your room.

Usage:
    python calibrate.py [--config config.yaml]

Controls:
    left click   add a point (do this 4x, going around the couch)
    u            undo last point
    c            clear all points
    s / enter    save the zone to config.yaml and exit
    q / esc      quit without saving
"""
from __future__ import annotations

import argparse
import sys

import cv2
import numpy as np
import yaml

from camera import get_camera


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def save_config(path: str, config: dict) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    points: list = []

    def on_mouse(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))

    window = "Calibrate couch zone -- click corners, 's' to save, 'q' to quit"
    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)

    cam = get_camera(config)
    print("Click the couch's corners (in order, going around it). Press 's' when done.")

    try:
        while True:
            frame = cam.read()
            if frame is None:
                print("Failed to read a frame from the camera.", file=sys.stderr)
                break

            display = frame.copy()
            for i, p in enumerate(points):
                cv2.circle(display, p, 5, (0, 255, 0), -1)
                cv2.putText(display, str(i + 1), (p[0] + 8, p[1] - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if len(points) >= 2:
                pts_arr = np.array(points, dtype="int32")
                cv2.polylines(display, [pts_arr], isClosed=len(points) >= 3,
                              color=(0, 200, 255), thickness=2)

            cv2.imshow(window, display)
            key = cv2.waitKey(20) & 0xFF

            if key in (ord("q"), 27):  # q or Esc
                print("Cancelled -- zone not saved.")
                break
            elif key == ord("u") and points:
                points.pop()
            elif key == ord("c"):
                points.clear()
            elif key in (ord("s"), 13):  # s or Enter
                if len(points) < 3:
                    print("Need at least 3 points to define a zone.")
                    continue
                config.setdefault("zone", {})["points"] = [list(p) for p in points]
                save_config(args.config, config)
                print(f"Saved {len(points)}-point couch zone to {args.config}")
                break
    finally:
        cam.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
