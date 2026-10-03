"""
MonitorService: the detection loop, wrapped so it can run alongside the
agent's network threads.

Only one process can hold a camera open, so the agent owns the camera and
everything else -- the live view, the recorder, the alerts -- reads from this
one loop. monitor.py still works standalone for a box with no server.

`store` and `settings` are the cloud_client.RemoteStore / RemoteSettings
adapters. Settings (active sound, whether video is recorded, the couch zone)
are read on each use rather than captured at startup, so a change made in
the dashboard takes effect on the next event -- or, for the zone, the next
frame -- without a restart.

Two threads, on purpose. Capture (grab -> annotate -> JPEG) runs in one and
detection in the other, because a MobileNet-SSD forward pass costs tens to
hundreds of milliseconds on CPU: with both in a single loop the live view
could only ever be as fast as inference, which made it crawl. The detector
now always works on the newest frame and silently skips the ones it could
not keep up with, while the live view runs at the camera's own frame rate
and reuses the most recent boxes until fresh ones arrive.
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
        self._detect_thread = None
        self._stop = threading.Event()

        # Published frames. The condition lets a live-view reader block until
        # a new frame exists instead of polling on a timer, which kept a
        # frame-rate ceiling (and avoidable latency) in the live view.
        self._frame_cv = threading.Condition()
        self._latest_jpeg = None
        self._latest_raw = None
        self._frame_seq = 0

        # Hand-off to the detector: always just the newest frame.
        self._detect_cv = threading.Condition()
        self._detect_frame = None
        # Last known boxes, reused by the capture loop between detections.
        # Only ever replaced wholesale, never mutated, so readers need no lock.
        self._boxes = []

        self.status = {
            "running": False,
            "camera_ok": False,
            "dog_on_couch": False,
            "dogs_in_frame": 0,
            "persons_in_frame": 0,
            "fps": 0.0,         # live view / capture rate
            "detect_fps": 0.0,  # how often detection actually runs
            "recording": False,
            "last_error": None,
            "started_at": None,
            "zone_points": [],
        }

        zone_cfg = config.get("zone", {})
        # Pixel polygon, the one detection and the overlay actually use.
        self.zone_points = [tuple(p) for p in zone_cfg.get("points", [])]
        self.overlap_threshold = zone_cfg.get("overlap_threshold", 0.35)
        # A zone drawn in the dashboard arrives as fractions of the frame and
        # is scaled to pixels once a frame has told us the real size.
        # None means no dashboard zone: the config.yaml one stands.
        self._zone_norm = None
        self._zone_frame = None     # frame size the pixel zone was built for
        self._zone_published = None

        d = config.get("debounce", {})
        self.tracker = CouchSessionTracker(
            enter_frames=d.get("enter_frames", 5),
            exit_frames=d.get("exit_frames", 8),
            min_alert_interval_sec=d.get("min_alert_interval_sec", 30),
        )

        # Clips are written at their own frame rate, independent of how fast
        # the camera is read, and fed at exactly that rate below -- otherwise
        # a 30fps capture written as a 15fps file plays back at double speed.
        self.clip_fps = float(config.get("alert", {}).get("video_fps", 15))
        # Detection gets a ceiling so it cannot eat every core and starve the
        # capture thread. 0 means "as fast as the machine manages".
        self.max_detect_fps = float(config.get("model", {}).get("max_detect_fps", 12) or 0)

        v = settings.all().get("video", {})
        self.recorder = ClipRecorder(
            out_dir=config.get("alert", {}).get("video_dir", "videos"),
            fps=self.clip_fps,
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
        # Both threads park on a condition, so signal them rather than waiting
        # out their timeouts.
        with self._detect_cv:
            self._detect_cv.notify_all()
        with self._frame_cv:
            self._frame_cv.notify_all()
        for t in (self._thread, self._detect_thread):
            if t:
                t.join(timeout=5)
        self.status["running"] = False

    # ---------- frame access for the web UI ----------

    def wait_for_jpeg(self, last_seq: int, timeout: float = 5.0):
        """Block until a frame newer than `last_seq` is published.

        Returns (jpeg, seq), or (None, last_seq) if nothing new arrived in
        `timeout` -- which lets the caller notice a dead camera (or a gone
        client) instead of blocking forever.
        """
        with self._frame_cv:
            if self._frame_seq == last_seq:
                self._frame_cv.wait(timeout)
                if self._frame_seq == last_seq:
                    return None, last_seq
            return self._latest_jpeg, self._frame_seq

    def snapshot_now(self):
        with self._frame_cv:
            return None if self._latest_raw is None else self._latest_raw.copy()

    def apply_video_settings(self, v: dict) -> None:
        from collections import deque
        r = self.recorder
        with r._lock:
            if "pre_roll_sec" in v and v["pre_roll_sec"] != r.pre_roll_sec:
                r.pre_roll_sec = v["pre_roll_sec"]
                r._buffer = deque(r._buffer, maxlen=max(1, int(r.fps * r.pre_roll_sec)))
            r.max_clip_sec = v.get("max_clip_sec", r.max_clip_sec)
            r.post_roll_sec = v.get("post_roll_sec", r.post_roll_sec)

    def update_zone(self, points, overlap_threshold=None) -> None:
        """Replace the couch polygon with one drawn in the dashboard.

        `points` are (x, y) fractions of the frame, 0..1, because the browser
        draws on a scaled JPEG and never knows the camera's pixels. They are
        scaled on the next frame, so a resolution change needs no redraw.
        """
        self._zone_norm = [(float(x), float(y)) for x, y in points]
        self._zone_frame = None
        if overlap_threshold is not None:
            self.overlap_threshold = float(overlap_threshold)

    def _resolve_zone(self, shape) -> None:
        """Scale a dashboard zone to this frame, and publish whichever zone
        is in effect (dashboard or config.yaml) as fractions, for the editor
        in the dashboard to start from."""
        h, w = shape[:2]
        if not (w and h):
            return
        if self._zone_norm is not None and self._zone_frame != (w, h):
            self.zone_points = [(min(w - 1, max(0, round(x * w))),
                                 min(h - 1, max(0, round(y * h))))
                                for x, y in self._zone_norm]
            self._zone_frame = (w, h)

        key = (w, h, tuple(self.zone_points))
        if key != self._zone_published:
            self._zone_published = key
            self.status["zone_points"] = [[round(x / w, 5), round(y / h, 5)]
                                          for x, y in self.zone_points]

    # ---------- the loops ----------

    def _run(self) -> None:
        """Capture loop: read, annotate with the latest boxes, publish."""
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

        self._detect_thread = threading.Thread(target=self._detect_loop,
                                               name="detector", daemon=True)
        self._detect_thread.start()

        jpeg_quality = int(self.config.get("display", {}).get("jpeg_quality", 70))
        frame_times = []
        next_clip_frame = 0.0

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
                self._resolve_zone(frame.shape)

                # Hand the detector the newest frame. If it is still busy, the
                # previous one is simply dropped -- stale detections are worse
                # than missed ones, and this is what keeps capture independent.
                with self._detect_cv:
                    self._detect_frame = frame
                    self._detect_cv.notify()

                # Clips get frames at clip_fps, not at the capture rate, so
                # they play back at real speed.
                if t0 >= next_clip_frame:
                    self.recorder.feed(frame)
                    next_clip_frame = max(t0, next_clip_frame) + 1.0 / self.clip_fps
                self.status["recording"] = self.recorder.recording

                annotated = self._annotate(frame.copy(), self._boxes)
                ok, buf = cv2.imencode(".jpg", annotated,
                                       [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                if ok:
                    with self._frame_cv:
                        self._latest_jpeg = buf.tobytes()
                        self._latest_raw = frame
                        self._frame_seq += 1
                        self._frame_cv.notify_all()

                frame_times.append(time.time() - t0)
                if len(frame_times) > 30:
                    frame_times.pop(0)
                avg = sum(frame_times) / len(frame_times)
                self.status["fps"] = round(1.0 / avg, 1) if avg > 0 else 0.0

        except Exception as e:
            self.status["last_error"] = str(e)
            print(f"[service] capture loop crashed: {e}", file=sys.stderr)
        finally:
            # The detector has nothing left to read once the camera is gone.
            self._stop.set()
            with self._detect_cv:
                self._detect_cv.notify_all()
            self.recorder.finish_now()
            if self._camera:
                self._camera.release()
            self.status["running"] = False

    def _detect_loop(self) -> None:
        """Detection loop: newest frame in, boxes and events out."""
        log_persons = self.config.get("debug", {}).get("log_persons", False)
        wanted = {"dog", "person"} if log_persons else {"dog"}
        min_interval = 1.0 / self.max_detect_fps if self.max_detect_fps else 0.0
        detect_times = []
        last_start = None

        try:
            while not self._stop.is_set():
                with self._detect_cv:
                    while self._detect_frame is None and not self._stop.is_set():
                        self._detect_cv.wait(0.25)
                    frame, self._detect_frame = self._detect_frame, None
                if frame is None:
                    continue

                t0 = time.time()
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

                self._boxes = boxes
                self.status.update({"dogs_in_frame": dogs, "persons_in_frame": persons,
                                    "dog_on_couch": any_on_couch})

                event = self.tracker.update(any_on_couch)
                if event:
                    self._handle_event(event, frame, best_conf)

                self._maybe_play_sound()

                # Measured start-to-start, so the reported rate includes any
                # idle time and matches what the debouncer actually sees.
                elapsed = time.time() - t0
                if last_start is not None:
                    detect_times.append(t0 - last_start)
                    if len(detect_times) > 30:
                        detect_times.pop(0)
                    avg = sum(detect_times) / len(detect_times)
                    self.status["detect_fps"] = round(1.0 / avg, 1) if avg > 0 else 0.0
                last_start = t0

                if min_interval:
                    self._stop.wait(max(0.0, min_interval - elapsed))

        except Exception as e:
            self.status["last_error"] = str(e)
            print(f"[service] detection loop crashed: {e}", file=sys.stderr)

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
            chosen = a.get("active_sound", "builtin")
            play_alert_sound(custom_wav=sounds.resolve(chosen), builtin=chosen)
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
