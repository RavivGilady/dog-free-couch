# Dog-on-couch monitor

Watches a camera feed, and tells you when your dog gets on the couch.

How it works: a lightweight object detector (MobileNet-SSD, runs fine on
CPU) finds dogs in each frame; a "couch zone" you draw once tells the
program which part of the frame _is_ the couch; when a detected dog's box
overlaps that zone enough, for enough consecutive frames, it logs the
event, saves a snapshot, plays a sound, and (optionally) pings your phone
via Telegram.

It's built so the same code runs on your laptop's webcam today and on a
Raspberry Pi's camera module later -- only one config line changes.

## 1. Install (on your laptop)

Requires Python 3.9+.

```bash
cd dog_couch_monitor
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python download_model.py        # fetches the ~23MB detection model, one time
```

## 2. Calibrate the couch zone

Point your webcam at the couch, then run:

```bash
python calibrate.py
```

A window opens showing the live feed. Click the couch's corners in order
(4 clicks is usually enough), then press `s` to save. Press `u` to undo a
point, `c` to clear, `q` to quit without saving.

This writes the zone's coordinates into `config.yaml` under `zone.points`.
Re-run this any time you move the camera or rearrange furniture.

## 3. Run the monitor

```bash
python monitor.py
```

A window shows the live feed with the couch zone highlighted and any
detected dog boxed in green (or red once it's confirmed "on the couch").
Press `q` in that window (or Ctrl+C in the terminal) to stop.

What happens on a confirmed "on the couch" event:

- a snapshot is saved to `snapshots/`
- a line is appended to `logs/events.csv` (timestamp, duration, confidence, snapshot path)
- a short sound plays
- if you've set up Telegram (below), a push notification is sent to your phone

### Tuning

Everything lives in `config.yaml`:

- `zone.overlap_threshold` -- how much of the dog's box must be inside the
  couch zone to count (0.35 = 35%). Lower it if a dog draped half off the
  couch isn't triggering; raise it if walking past the couch is triggering.
- `debounce.enter_frames` / `exit_frames` -- how many consecutive frames of
  "yes"/"no" are needed before it's treated as a real event. Higher values
  are steadier but slower to react; at ~15-20fps, 5 frames is roughly a
  third of a second.
- `debounce.min_alert_interval_sec` -- won't send more than one alert this
  often, so it doesn't spam you while the dog just... stays there.
- `model.confidence_threshold` -- how sure the detector must be that
  something is a dog before it's considered at all.

### Troubleshooting: gray/frozen "no signal" window instead of the camera

If `calibrate.py` or `monitor.py` opens a window but it's a static gray
image (often with a little camera icon) instead of a live picture -- even
though the camera works fine in the Windows Camera app -- this is a known
OpenCV-on-Windows quirk: its default backend (Media Foundation) sometimes
grabs a placeholder instead of the real device, especially when Windows
has more than one "camera" registered (built-in webcam, an IR camera for
Windows Hello, virtual camera software, etc).

Fix:

```bash
python list_cameras.py
```

This opens a window and prints which index/backend combo it's trying.
Press `n` to cycle to the next camera index, `b` to cycle backends (`any`
/ `dshow` / `msmf`) for the current index, until you see a real, moving
picture of the room. Note the index and backend it shows, then set them
in `config.yaml`:

```yaml
camera:
  index: 1 # whatever index showed a live picture
  backend_api: dshow # whatever backend showed a live picture
```

`dshow` (DirectShow) fixes this for most people -- `monitor.py` and
`calibrate.py` already try it first automatically on Windows, so if you're
still seeing a frozen frame, it usually means index 0 isn't your real
camera at all; `list_cameras.py` will find the right index.

## 4. Web dashboard (recommended)

    python app.py

Then open <http://127.0.0.1:8080>. On first visit you set a dashboard
password; it is hashed with werkzeug and stored in `instance/settings.json`,
which is gitignored.

The dashboard gives you:

* **Live** - the camera feed with the couch zone and detection boxes drawn
  on it, plus live stats and a "test alarm" button.
* **Events** - every alert with its snapshot and a playable video clip.
* **Sound** - record a custom alert through your browser mic (say whatever
  actually works on your dog) and set it as the alarm.
* **Settings** - Telegram credentials, alarm repeat interval, clip lengths,
  and password change.

To reach it from your phone on the same wifi:

    python app.py --host 0.0.0.0

### Important: one process owns the camera

A camera can only be opened by one process at a time. `app.py` runs the
detection loop itself, so **run either `app.py` or `monitor.py`, not both**.
`monitor.py` still exists for a headless box with no web UI.

### Video clips

Clips include a few seconds of **pre-roll** from before the alert fired, so
you see the dog actually getting on rather than already sitting there. The
codec is probed at startup: H.264 where available (all browsers play it),
falling back to WebM/VP8. Adjust pre-roll, post-roll and the length cap in
Settings.

### Security

The dashboard is HTTP only. Passwords are hashed, sessions are signed and
HttpOnly, POSTs are CSRF-protected, and logins are rate-limited after 8
failures. That is appropriate for your own machine or a trusted home
network. **Before exposing it to the internet, put it behind a reverse proxy
with TLS** (Caddy gets you an automatic certificate in about three lines).
Without TLS, your password crosses the network in the clear.

Note that browsers only allow microphone access on `localhost` or over
HTTPS, so recording a custom sound from your phone over plain LAN HTTP will
be blocked by the browser - record it on the host, or set up TLS.

## 5. Optional: push notifications to your phone (Telegram)

This works from a laptop or later from a headless Raspberry Pi, and takes
about 2 minutes:

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the
   prompts. You'll get a bot token (looks like `123456:ABC-DEF...`).
2. Message your new bot anything (so it's allowed to message you back).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   and find `"chat":{"id": ...}` in the response -- that's your chat id.
4. In `config.yaml`, set:
   ```yaml
   alert:
     telegram:
       enabled: true
       bot_token: "123456:ABC-DEF..."
       chat_id: "123456789"
   ```

## 6. Moving to a Raspberry Pi later

The detection, zone, and alert logic (`detector.py`, `zone.py`,
`debounce.py`, `alert.py`, `monitor.py`) don't know or care what camera
they're reading from -- only `camera.py` does. To switch:

1. On the Pi: `sudo apt install -y python3-picamera2`, then
   `pip install -r requirements.txt` in the same virtualenv (or
   `--system-site-packages` so it can see picamera2, which apt installs
   system-wide, not via pip).
2. In `config.yaml`, change:
   ```yaml
   camera:
     backend: picamera2
   ```
3. Re-run `python calibrate.py` from the Pi (the camera's field of view
   will differ from your laptop's), then `python monitor.py --headless`
   if the Pi has no monitor attached (this skips the preview window but
   keeps logging/snapshots/sound/Telegram working).

A Pi 4 or better runs MobileNet-SSD comfortably in real time on CPU; no
GPU or Coral accelerator needed for this use case.

Telegram is now configured from the dashboard's Settings tab rather than
config.yaml, so the bot token never lands in a committed file.

## Project layout

```
camera.py       # camera backends: OpenCV webcam today, picamera2 on a Pi later
detector.py     # MobileNet-SSD wrapper -- finds "dog" boxes in a frame
zone.py         # geometry: how much of a box overlaps the couch polygon
debounce.py     # turns noisy per-frame readings into clean enter/leave events
alert.py        # logging, snapshots, sound, Telegram
calibrate.py    # one-time tool to draw the couch zone
monitor.py      # main loop, ties everything together
download_model.py
config.yaml
tests/          # unit tests for zone.py and debounce.py (no camera needed)
```

Run the tests any time with:

```bash
python tests/test_zone.py
python tests/test_debounce.py
```

## Known limitations

- MobileNet-SSD is trained on the Pascal VOC dataset (20 everyday object
  classes) -- it recognizes "dog" well across breeds and sizes, but it's
  a 2017-era lightweight model, not state-of-the-art. If you get
  consistent misses with a particular lighting setup or a very small/dark
  dog, lowering `model.confidence_threshold` slightly (e.g. to 0.4) often
  helps.
- This decides "on the couch" purely from 2D box overlap in the camera's
  view, not true depth/3D reasoning -- an unusual camera angle where the
  couch and floor overlap in the frame could cause false positives. Placing
  the camera to look across the couch (not straight down its length) gives
  the cleanest zone.
- One camera only. Multiple couches/rooms would mean running a second
  instance with its own config and camera index.
