"""
MonitorService: the detection loop, wrapped so it can run under a web server.

Only one process can hold a camera open, so when the dashboard is running it
owns the camera and everything else -- the live stream, the recorder, the
alerts -- reads from this one loop. monitor.py still works standalone for a
headless box; it just does not get the web UI.

Everything the dashboard can change at runtime (active sound, Telegram
credentials, whether video is recorded) is read from SettingsStore on each
use rather than captured at startup, so a settings change takes effect on
the next event without a restart.
"""
from __future__ import annotations

import sys
import threading
import time

from alert import (log_event, play_alert_sound, save_snapshot,
                   send_telegram_alert, send_telegram_video)
from camera import get_camera
from debounce import CouchSessionTracker
from detector import Detector
from recorder import ClipRecorder
from zone import is_dog_on_couch
import sounds


class MonitorService:
    def __init__(self, config: dict, store, settings):
        self.config = config
        self.store = store
        self.settings = settings

        self._thread = None
        self._stop = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_jpeg = None
        self._latest_raw = None

        self.status = {
            "running": False,
            "camera_ok": False,
            "dog_on_couch": False,
            "dogs_in_frame": 0,
            "persons_in_frame": 0,
            "fps": 0.0,
            "recording": False,
            "last_error": None,
            "started_at": None,
        }

        zone_cfg = config.get("zone", {})
        self.zone_points = [tuple(p) for p in zone_cfg.get("points", [])]
        self.overlap_threshold = zone_cfg.get("overlap_threshold", 0.35)

        d = config.get("debounce", {})
        self.tracker = CouchSessionTracker(
            enter_frames=d.get("enter_frames", 5),
            exit_frames=d.get("exit_frames", 8),
            min_alert_interval_sec=d.get("min_alert_interval_sec", 30),
        )

        v = settings.all().get("video", {})
        self.recorder = ClipRecorder(
            out_dir=config.get("alert", {}).get("video_dir", "videos"),
            fps=config.get("camera", {}).get("fps", 15),
            pre_roll_sec=v.get("pre_roll_sec", 4),
            max_clip_sec=v.get("max_clip_sec", 60),
            post_roll_sec=v.get("post_roll_sec", 3),
        )

        self._detector = None
        self._camera = None
        self._current_event_id = None
        self._last_sound_time = float("-inf")

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.status["running"] = False

    # ---------- frame access for the web UI ----------

    def latest_jpeg(self):
        with self._frame_lock:
            return self._latest_jpeg

    def snapshot_now(self):
        with self._frame_lock:
            return None if self._latest_raw is None else self._latest_raw.copy()

    def update_zone(self, points, overlap_threshold=None) -> None:
        self.zone_points = [tuple(p) for p in points]
        if overlap_threshold is not None:
            self.overlap_threshold = overlap_threshold

    # ---------- the loop ----------

    def _run(self) -> None:
        import cv2

        model_cfg = self.config.get("model", {})
        try:
            self._detector = Detector(
                prototxt_path=model_cfg.get("prototxt", "models/MobileNetSSD_deploy.prototxt"),
                weights_path=model_cfg.get("weights", "models/MobileNetSSD_deploy.caffemodel"),
                confidence_threshold=model_cfg.get("confidence_threshold", 0.5),
            )
            self._camera = get_camera(self.config)
        except Exception as e:
            self.status["last_error"] = str(e)
            self.status["running"] = False
            print(f"[service] startup failed: {e}", file=sys.stderr)
            return

        self.status.update({"running": True, "camera_ok": True,
                            "started_at": time.time(), "last_error": None})
        log_persons = self.config.get("debug", {}).get("log_persons", False)
        frame_times = []

        try:
            while not self._stop.is_set():
                t0 = time.time()
                frame = self._camera.read()
                if frame is None:
                    self.status["camera_ok"] = False
                    self.status["last_error"] = "Lost camera feed"
                    time.sleep(0.5)
                    continue
                self.status["camera_ok"] = True

                wanted = {"dog", "person"} if log_persons else {"dog"}
                detections = self._detector.detect(frame, wanted_labels=wanted)

                any_on_couch = False
                best_conf = 0.0
                boxes = []
                dogs = persons = 0
                for det in detections:
                    in_zone = is_dog_on_couch(det.box, self.zone_points,
                                              frame.shape, self.overlap_threshold)
                    boxes.append((det.label, det.box, in_zone, det.confidence))
                    if det.label == "person":
                        persons += 1
                        continue
                    dogs += 1
                    if in_zone:
                        any_on_couch = True
                        best_conf = max(best_conf, det.confidence)

                self.status.update({"dogs_in_frame": dogs, "persons_in_frame": persons,
                                    "dog_on_couch": any_on_couch})

                self.recorder.feed(frame)
                event = self.tracker.update(any_on_couch)
                if event:
                    self._handle_event(event, frame, best_conf)

                self._maybe_play_sound()

                annotated = self._annotate(frame.copy(), boxes)
                ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    with self._frame_lock:
                        self._latest_jpeg = buf.tobytes()
                        self._latest_raw = frame

                self.status["recording"] = self.recorder.recording

                frame_times.append(time.time() - t0)
                if len(frame_times) > 30:
                    frame_times.pop(0)
                avg = sum(frame_times) / len(frame_times)
                self.status["fps"] = round(1.0 / avg, 1) if avg > 0 else 0.0

        except Exception as e:
            self.status["last_error"] = str(e)
            print(f"[service] loop crashed: {e}", file=sys.stderr)
        finally:
            self.recorder.finish_now()
            if self._camera:
                self._camera.release()
            self.status["running"] = False

    # ---------- events ----------

    def _handle_event(self, event, frame, confidence) -> None:
        alert_cfg = self.config.get("alert", {})

        if event["type"] == "entered":
            snapshot_path = None
            if alert_cfg.get("save_snapshot", True):
                snapshot_path = save_snapshot(frame, alert_cfg.get("snapshot_dir", "snapshots"))

            event_id = self.store.add_entered(event["start"], confidence, snapshot_path)
            self._current_event_id = event_id
            print(f"[ALERT] Dog on the couch (event #{event_id})")

            if alert_cfg.get("log_csv"):
                log_event(alert_cfg["log_csv"], event, confidence, snapshot_path)

            if self.settings.get("video", "enabled", True):
                self.recorder.start(
                    frame.shape,
                    on_done=lambda path, n, eid=event_id: self._on_clip_done(eid, path, n),
                )

            threading.Thread(target=self._notify_entered,
                             args=(event_id, snapshot_path), daemon=True).start()

        elif event["type"] == "left":
            self.store.add_left(event["start"], event["end"], event["duration"])
            print(f"[INFO] Dog left the couch after {event['duration']:.0f}s")
            if alert_cfg.get("log_csv"):
                log_event(alert_cfg["log_csv"], event)
            self.recorder.stop()

    def _notify_entered(self, event_id, snapshot_path) -> None:
        """Photo goes out immediately; the clip follows once it is finished."""
        tg = self.settings.all().get("telegram", {})
        if not (tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id")):
            return
        if tg.get("send_photo", True):
            send_telegram_alert(tg["bot_token"], tg["chat_id"],
                                "Your dog just got on the couch!",
                                photo_path=snapshot_path)
        self.store.mark_notified(event_id)

    def _on_clip_done(self, event_id, path, frames_written) -> None:
        if not path:
            return
        self.store.attach_video(event_id, path)
        print(f"[INFO] Clip saved for event #{event_id}: {path} ({frames_written} frames)")

        tg = self.settings.all().get("telegram", {})
        if tg.get("enabled") and tg.get("send_video", True) and tg.get("bot_token"):
            send_telegram_video(tg["bot_token"], tg["chat_id"], path,
                                caption="Dog on the couch")

    # ---------- sound ----------

    def _maybe_play_sound(self) -> None:
        a = self.settings.all().get("alert", {})
        if not a.get("play_sound", True):
            return

        if self.tracker.state != "ON":
            self._last_sound_time = float("-inf")
            return

        repeat = a.get("repeat_sound_sec", 3) or 0
        now = time.time()
        if now - self._last_sound_time >= repeat:
            play_alert_sound(custom_wav=sounds.resolve(a.get("active_sound", "builtin")))
            self._last_sound_time = now if repeat else float("inf")

    # ---------- drawing ----------

    def _annotate(self, frame, boxes):
        import cv2
        import numpy as np

        if self.zone_points and len(self.zone_points) >= 3:
            pts = np.array(self.zone_points, dtype="int32")
            overlay = frame.copy()
            cv2.fillPoly(overlay, [pts], (0, 200, 255))
            cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, dst=frame)
            cv2.polylines(frame, [pts], True, (0, 200, 255), 2)

        for label, box, in_zone, conf in boxes:
            x1, y1, x2, y2 = box
            color = (255, 160, 0) if label == "person" else ((0, 0, 255) if in_zone else (0, 255, 0))
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"{label} {conf:.2f}" + (" IN ZONE" if in_zone else ""),
                        (x1, max(0, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        if self.recorder.recording:
            cv2.circle(frame, (frame.shape[1] - 24, 22), 8, (0, 0, 255), -1)
            cv2.putText(frame, "REC", (frame.shape[1] - 70, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        return frame
