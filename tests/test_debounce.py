import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debounce import CouchSessionTracker


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def advance(self, dt=1.0):
        self.t += dt
        return self.t

    def __call__(self):
        return self.t


def test_single_flicker_frame_does_not_trigger():
    clock = FakeClock()
    tracker = CouchSessionTracker(enter_frames=5, exit_frames=5, min_alert_interval_sec=30, clock=clock)

    events = []
    for is_on in [True, False, True, False, True]:  # never 5 in a row
        clock.advance()
        events.append(tracker.update(is_on))

    assert all(e is None for e in events)
    assert tracker.state == "OFF"


def test_sustained_presence_triggers_entered_event():
    clock = FakeClock()
    tracker = CouchSessionTracker(enter_frames=3, exit_frames=3, min_alert_interval_sec=30, clock=clock)

    events = []
    for _ in range(3):
        clock.advance()
        events.append(tracker.update(True))

    fired = [e for e in events if e is not None]
    assert len(fired) == 1
    assert fired[0]["type"] == "entered"
    assert tracker.state == "ON"


def test_leaving_after_enough_negative_frames_fires_left_event():
    clock = FakeClock()
    tracker = CouchSessionTracker(enter_frames=2, exit_frames=2, min_alert_interval_sec=0, clock=clock)

    clock.advance(); tracker.update(True)
    clock.advance(); tracker.update(True)  # now ON

    clock.advance(); left1 = tracker.update(False)
    clock.advance(); left2 = tracker.update(False)  # now OFF

    assert left1 is None
    assert left2["type"] == "left"
    assert left2["duration"] > 0
    assert tracker.state == "OFF"


def test_min_alert_interval_suppresses_rapid_re_alerts():
    clock = FakeClock()
    tracker = CouchSessionTracker(enter_frames=1, exit_frames=1, min_alert_interval_sec=100, clock=clock)

    clock.advance()
    first = tracker.update(True)  # enters -> should alert
    clock.advance()
    tracker.update(False)  # leaves
    clock.advance(2)
    second = tracker.update(True)  # re-enters quickly -> should NOT re-alert (state still updates though)

    assert first is not None and first["type"] == "entered"
    assert second is None  # suppressed by min_alert_interval_sec
    assert tracker.state == "ON"  # state machine still tracks it, just doesn't re-alert


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
