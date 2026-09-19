"""
Main loop: watch the camera, detect dogs, check overlap with the couch
zone, debounce, and fire alerts.

Usage:
    python monitor.py [--config config.yaml] [--headless]

Run calibrate.py first if config.yaml has no zone defined yet.
"""
from __future__ import annotations

import argparse
import sys
import time

import cv2
import yaml

from alert import log_event, play_alert_sound, save_snapshot, send_telegram_alert
from camera import get_camera
from debounce import CouchSessionTracker
from detector import Detector
from zone import is_dog_on_couch


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}
    if not config.get("zone", {}).get("points"):
        print(
            f"Warning: no couch zone defined in {path}. "
            "Run `python calibrate.py` first, or the monitor will never "
            "trigger.",
            file=sys.stderr,
        )
    return config


def draw_overlay(frame, zone_points, boxes_with_state):
    """boxes_with_state: iterable of (label, box, in_zone, confidence)."""
    import numpy as np

    if zone_points and len(zone_points) >= 3:
        pts = np.array(zone_points, dtype="int32")
        overlay = frame.copy()
        cv2.fillPoly(overlay, [pts], (0, 200, 255))
        cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, dst=frame)
        cv2.polylines(frame, [pts], True, (0, 200, 255), 2)

    for label, box, in_zone, confidence in boxes_with_state:
        x1, y1, x2, y2 = box
        if label == "person":
            # Debug-only class: blue, so it never reads as a real alert.
            color = (255, 160, 0)
        else:
            color = (0, 0, 255) if in_zone else (0, 255, 0)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        caption = f"{label} {confidence:.2f}" + (" IN ZONE" if in_zone else "")
        cv2.putText(frame, caption, (x1, max(0, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--headless", action="store_true",
                         help="Don't open a preview window (for a Pi with no display attached).")
    parser.add_argument("--log-persons", action="store_true",
                         help="Testing aid: also detect people and log when one enters the "
                              "couch zone. Never triggers alerts.")
    args = parser.parse_args()

    config = load_config(args.config)
    model_cfg = config.get("model", {})
    zone_cfg = config.get("zone", {})
    debounce_cfg = config.get("debounce", {})
    alert_cfg = config.get("alert", {})
    display_cfg = config.get("display", {})
    debug_cfg = config.get("debug", {})

    show_window = display_cfg.get("show_window", True) and not args.headless
    log_persons = debug_cfg.get("log_persons", False) or args.log_persons

    detector = Detector(
        prototxt_path=model_cfg.get("prototxt", "models/MobileNetSSD_deploy.prototxt"),
        weights_path=model_cfg.get("weights", "models/MobileNetSSD_deploy.caffemodel"),
        confidence_threshold=model_cfg.get("confidence_threshold", 0.5),
    )
    tracker = CouchSessionTracker(
        enter_frames=debounce_cfg.get("enter_frames", 5),
        exit_frames=debounce_cfg.get("exit_frames", 8),
        min_alert_interval_sec=debounce_cfg.get("min_alert_interval_sec", 30),
    )

    zone_points = [tuple(p) for p in zone_cfg.get("points", [])]
    overlap_threshold = zone_cfg.get("overlap_threshold", 0.35)

    cam = get_camera(config)
    print("Monitoring started. Press 'q' in the preview window (or Ctrl+C) to stop.")
    if log_persons:
        print("[DEBUG] Person logging is ON (testing aid -- people never trigger alerts).")

    # Only logged when it changes, so a 30fps loop doesn't flood the console.
    last_person_report = None

    # Sound is driven by the tracker's STATE, not by the "entered" event, so
    # it keeps going for as long as the dog is up there -- including when the
    # event itself was suppressed by min_alert_interval_sec.
    play_sound = alert_cfg.get("play_sound", True)
    repeat_sound_sec = alert_cfg.get("repeat_sound_sec", 0) or 0
    last_sound_time = float("-inf")

    try:
        while True:
            frame = cam.read()
            if frame is None:
                print("Lost camera feed, stopping.", file=sys.stderr)
                break

            wanted = {"dog", "person"} if log_persons else {"dog"}
            detections = detector.detect(frame, wanted_labels=wanted)

            # If multiple dogs are ever in frame, treat the couch as
            # occupied if ANY of them overlaps it enough.
            best_confidence = 0.0
            any_on_couch = False
            per_box_state = []
            persons_in_zone = 0
            persons_seen = 0
            for det in detections:
                in_zone = is_dog_on_couch(det.box, zone_points, frame.shape, overlap_threshold)
                per_box_state.append((det.label, det.box, in_zone, det.confidence))

                if det.label == "person":
                    # Debug only: counted and drawn, but deliberately kept out
                    # of the tracker so it can never fire an alert.
                    persons_seen += 1
                    persons_in_zone += int(in_zone)
                    continue

                if in_zone:
                    any_on_couch = True
                    best_confidence = max(best_confidence, det.confidence)

            if log_persons:
                report = (persons_seen, persons_in_zone)
                if report != last_person_report:
                    last_person_report = report
                    print(f"[DEBUG] person: {persons_seen} in frame, "
                          f"{persons_in_zone} in couch zone")

            event = tracker.update(any_on_couch)

            if event and event["type"] == "entered":
                print(f"[ALERT] Dog got on the couch at {event['start']}")
                snapshot_path = None
                if alert_cfg.get("save_snapshot", True):
                    snapshot_path = save_snapshot(frame, alert_cfg.get("snapshot_dir", "snapshots"))
                if alert_cfg.get("log_csv"):
                    log_event(alert_cfg["log_csv"], event, best_confidence, snapshot_path)

                telegram_cfg = alert_cfg.get("telegram", {})
                if telegram_cfg.get("enabled"):
                    send_telegram_alert(
                        telegram_cfg.get("bot_token", ""),
                        telegram_cfg.get("chat_id", ""),
                        "🐕 Your dog just got on the couch!",
                        photo_path=snapshot_path,
                    )

            elif event and event["type"] == "left":
                print(f"[INFO] Dog left the couch after {event['duration']:.0f}s")
                if alert_cfg.get("log_csv"):
                    log_event(alert_cfg["log_csv"], event)

            # Nag while the dog is on the couch; goes quiet the moment the
            # tracker says it left.
            if play_sound and tracker.state == "ON":
                now = time.time()
                if now - last_sound_time >= repeat_sound_sec:
                    play_alert_sound()
                    # A repeat interval of 0 means "beep once on entry only":
                    # push the next allowed beep out of reach until it leaves.
                    last_sound_time = now if repeat_sound_sec else float("inf")
            elif tracker.state == "OFF":
                last_sound_time = float("-inf")

            if show_window:
                draw_overlay(frame, zone_points, per_box_state)
                cv2.imshow("Dog couch monitor", frame)
                if (cv2.waitKey(1) & 0xFF) == ord("q"):
                    break

    except KeyboardInterrupt:
        pass
    finally:
        cam.release()
        if show_window:
            cv2.destroyAllWindows()
        print("Monitoring stopped.")


if __name__ == "__main__":
    main()
