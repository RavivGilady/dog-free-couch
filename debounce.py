"""
Debounce state machine: turns a noisy per-frame "is the dog on the couch
right now?" signal into clean "entered" / "left" events, so a single
flickery frame doesn't fire an alert and a brief drop in detection doesn't
end a session early.
"""
from __future__ import annotations

import time


class CouchSessionTracker:
    def __init__(self, enter_frames: int = 5, exit_frames: int = 8,
                 min_alert_interval_sec: float = 30.0, clock=time.time):
        self.enter_frames = enter_frames
        self.exit_frames = exit_frames
        self.min_alert_interval_sec = min_alert_interval_sec
        self._clock = clock

        self.state = "OFF"  # "OFF" | "ON"
        self._pos_streak = 0
        self._neg_streak = 0
        self._last_alert_time = float("-inf")
        self.session_start = None

    def update(self, is_on_couch: bool):
        """Feed one frame's reading in. Returns an event dict, or None.

        Event shapes:
          {"type": "entered", "start": <ts>}
          {"type": "left", "start": <ts>, "end": <ts>, "duration": <seconds>}
        """
        now = self._clock()

        if is_on_couch:
            self._pos_streak += 1
            self._neg_streak = 0
        else:
            self._neg_streak += 1
            self._pos_streak = 0

        event = None

        if self.state == "OFF" and self._pos_streak >= self.enter_frames:
            self.state = "ON"
            self.session_start = now
            if now - self._last_alert_time >= self.min_alert_interval_sec:
                self._last_alert_time = now
                event = {"type": "entered", "start": self.session_start}

        elif self.state == "ON" and self._neg_streak >= self.exit_frames:
            self.state = "OFF"
            event = {
                "type": "left",
                "start": self.session_start,
                "end": now,
                "duration": now - self.session_start,
            }
            self.session_start = None

        return event
