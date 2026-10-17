"""Watch the mocked-camera scenario live in a browser.

    python tests/demo_dog_flow.py        # then open http://localhost:8765

Same fake camera and real detector as test_dog_on_couch_flow.py, replaying a
scripted day: empty couch, dog jumps up for 10s, leaves, comes back for 5s.
The couch zone appears 2s after start, then you can watch the alerts and clip
saves happen. Not a test -- nothing is asserted. Ctrl+C to stop.
"""
import json
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flow_fakes as ff
import service

PORT = 8765
events = []  # shown in the page; also printed

# What the camera shows, as (seconds, picture): empty couch, the dog jumps up
# and stays, leaves, comes back shortly after, leaves again. Then it repeats.
SCRIPT = [(4, "empty"), (10, "dog"), (4, "empty"), (5, "dog"), (5, "empty")]


class ScriptedCamera(ff.FakeCamera):
    def __init__(self):
        super().__init__(ff.empty_frame())
        self.pictures = {"empty": ff.empty_frame(), "dog": ff.dog_frame()}
        self.t0 = time.time()

    def read(self):
        t = (time.time() - self.t0) % sum(d for d, _ in SCRIPT)
        for dur, name in SCRIPT:
            if t < dur:
                self.frame = self.pictures[name]
                break
            t -= dur
        return super().read()


def log(msg):
    events.append(f"{time.strftime('%H:%M:%S')}  {msg}")
    print(events[-1], flush=True)


class LoggingStore(ff.FakeStore):
    def add_entered(self, *a):
        log(f"ALERT: dog on the couch")
        return super().add_entered(*a)

    def add_left(self, *a):
        log(f"dog left the couch")
        return super().add_left(*a)

    def attach_video(self, event_id, path):
        log(f"clip saved for event #{event_id}")
        return super().attach_video(event_id, path)


PAGE = """<!doctype html><meta charset=utf-8><title>Mock camera demo</title>
<style>body{font:16px system-ui;background:#111;color:#eee;max-width:700px;margin:1.5rem auto;padding:0 1rem}
img{width:100%;border-radius:8px}#s{padding:.6rem 1rem;border-radius:8px;margin:.8rem 0;font-weight:600}
.off{background:#2a6}.on{background:#c22}pre{background:#1c1c1c;padding:.8rem;border-radius:8px;min-height:4rem}
button{padding:.5rem 1rem;margin-right:.5rem}</style>
<h2>Mock camera: live view</h2><img src="/stream">
<div id=s>...</div>
<button onclick="fetch('/zone')">Set zone</button><button onclick="fetch('/clear')">Clear zone</button>
<pre id=log></pre>
<script>setInterval(async()=>{const r=await (await fetch('/status')).json();
const s=document.getElementById('s');s.className=r.on?'on':'off';
s.textContent=(r.on?'DOG ON COUCH - alarm':'Couch clear')+' | zone '+(r.zone?'set':'not set')+' | dogs seen: '+r.dogs;
document.getElementById('log').textContent=r.events.join('\n')||'(no events yet)'},300)</script>"""


def main():
    if not ff.MODEL_AVAILABLE:
        sys.exit("Model weights missing: run python download_model.py first.")
    tmp = Path(tempfile.mkdtemp())
    service.get_camera = lambda config: ScriptedCamera()
    service.play_alert_sound = lambda *a, **k: None
    config = {
        "alert": {"video_dir": str(tmp / "v"), "snapshot_dir": str(tmp / "s"), "video_fps": 15},
        "debounce": {"enter_frames": 3, "exit_frames": 3, "min_alert_interval_sec": 1},
        "model": {**ff.MODEL, "max_detect_fps": 10},
    }
    svc = service.MonitorService(config, store=LoggingStore(),
                                 settings=ff.FakeSettings({"pre_roll_sec": 2, "post_roll_sec": 1}, record=True))
    svc.start()
    threading.Timer(2.0, lambda: svc.update_zone(ff.COUCH_ZONE, 0.35)).start()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
                self.end_headers()
                seq = 0
                try:
                    while True:
                        jpeg, seq = svc.wait_for_jpeg(seq, 2.0)
                        if jpeg:
                            self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                except OSError:
                    return
            body, ctype = PAGE.encode(), "text/html"
            if self.path == "/status":
                body = json.dumps({"on": svc.status["dog_on_couch"], "dogs": svc.status["dogs_in_frame"],
                                   "zone": bool(svc.zone_points), "events": events[-8:]}).encode()
                ctype = "application/json"
            elif self.path == "/zone":
                svc.update_zone(ff.COUCH_ZONE, 0.35)
            elif self.path == "/clear":
                svc.update_zone([], 0.35)
                svc.zone_points = []
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"Open http://localhost:{PORT}  (Ctrl+C to stop)")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()


if __name__ == "__main__":
    main()
