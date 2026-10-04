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

If you are running the server (step 5), you can skip this and draw the zone
from your own computer instead -- on the live view in the dashboard, which is
the only option when the camera is a headless Pi in another room. See
[Drawing the couch zone from the dashboard](#drawing-the-couch-zone-from-the-dashboard).

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
- a siren plays (pick one with `alert.sound` in `config.yaml`; hear them
  all with `python -m sirens`)
- if you've set up Telegram (below), a push notification is sent to your phone

### Tuning

Everything lives in `config.yaml`:

- `zone.overlap_threshold` -- how much of the dog's box must be inside the
  couch zone to count (0.35 = 35%). Lower it if a dog draped half off the
  couch isn't triggering; raise it if walking past the couch is triggering.
- `debounce.enter_frames` / `exit_frames` -- how many consecutive frames of
  "yes"/"no" are needed before it's treated as a real event. Higher values
  are steadier but slower to react. These count _detection_ frames, not live
  view frames, so at a detection rate of ~10fps, 5 frames is about half a
  second.
- `debounce.min_alert_interval_sec` -- won't send more than one alert this
  often, so it doesn't spam you while the dog just... stays there.
- `model.confidence_threshold` -- how sure the detector must be that
  something is a dog before it's considered at all.

### Tuning the live view's frame rate

The dashboard's FPS readout shows two numbers: the live view's frame rate and,
after the slash, how often detection runs. They are deliberately different --
capture and detection run in separate threads, so the picture stays smooth
even though a MobileNet-SSD pass costs 100-250ms on CPU.

If the live view is still slow:

- `camera.fourcc: "mjpg"` -- the biggest single win on USB webcams. Many only
  reach 30fps in MJPEG and drop to 5-10fps on raw YUY2 at the same resolution.
- `camera.fps` -- what to request from the camera; it caps the live view.
- `camera.width` / `height` -- fewer pixels means cheaper JPEG encoding, and
  detection resizes to 300x300 anyway, so dropping to 480x360 costs little
  accuracy.
- `display.jpeg_quality` -- lower it (50-60) when watching over wifi; the
  encode and the transfer both get cheaper.
- `model.max_detect_fps` -- how often detection may run at most. Lowering it
  leaves more CPU for capture; raising it (or 0 for unlimited) reacts sooner
  at the cost of a less smooth picture. Note that `debounce.enter_frames`
  counts _detection_ frames, so this is the rate that decides how quickly an
  event fires.
- `alert.video_fps` -- the frame rate event clips are written at. Clips are
  fed at exactly this rate regardless of how fast the camera runs, so they
  play back at real speed.

When watching through the hosted dashboard, the agent still uploads at most a
few frames a second (`LIVE_MAX_FPS` in `cloud_client.py`) -- that cap is about
your upload bandwidth, not the camera.

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

## 4. Web dashboard: server + camera agent (recommended)

The dashboard is a website you can host online, with an account per person
and any number of cameras per account. It has two parts:

```
 home (laptop / Pi)                         internet (VPS / PaaS)
+----------------------+   HTTPS          +-------------------------+
| agent.py             | ---------------> | server/  (Flask API)    |  <-- browser
|  camera + detection  |  events, clips,  |  accounts, devices,     |      (frontend/,
|  alarm sound         |  live frames     |  events DB, clip store, |       served by
|  clip recording      | <--------------- |  Telegram notifications |       the server)
+----------------------+  settings, cmds  +-------------------------+
```

- **The agent** (`agent.py`) runs next to the camera. Detection, the alarm
  and clip recording all happen there, so the alarm still works if the
  internet is down; events and clips queue up and upload when it is back.
  It only sends live video while someone has the Live tab open.
- **The server** (`server/`) stores events and clips, serves them to their
  owner only, relays the live view, pushes settings and "test alarm" to the
  agent, and sends Telegram notifications.
- **The frontend** (`frontend/`) is a static single-page app the server
  serves at `/`.
- **A camera station** is the same thing as the agent, except that it is a
  dashboard tab: the browser opens its own camera, detects the dog in the
  page and sounds the alarm out of that device's speakers. Nothing to
  install, and the server cannot tell the two apart. See
  [Using a browser as the camera](#using-a-browser-as-the-camera).

### Try it locally

```bash
pip install -r server/requirements.txt
python -m server                      # http://127.0.0.1:8000
```

Open it, create an account, go to **Devices**, add a camera. The page shows
a command with a one-time token; run it in a second terminal on the camera
machine:

```bash
python agent.py --server http://127.0.0.1:8000 --token dfc_...
```

The token is saved to `instance/device_token`, so afterwards
`python agent.py --server ...` is enough (or put the URL in `config.yaml`
under `cloud.server`).

The dashboard gives you:

- **Live** - the camera feed with the couch zone and detection boxes, live
  stats, a "test alarm" button that plays on the camera computer, and the
  couch zone editor.
- **Events** - every alert with its snapshot and a playable clip, per
  camera or across all cameras.
- **Sound** - choose one of the built-in sirens (wail, yelp, hi-lo horn,
  whoop, or the original two-tone beeps), or record a custom alert through
  your browser mic and send it to the selected camera.
- **Settings** - alarm repeat and clip lengths (per camera), Telegram and
  password (per account).
- **Devices** - add, rename and remove cameras; issue a new token; or turn
  the device you are reading the dashboard on into a camera itself.

### Using a browser as the camera

If the computer or phone you are reading the dashboard on is the one facing
the couch, you don't need the agent at all. On **Devices**, under _Use this
device as a camera_, name it and press **Start camera here**. The page asks
for the camera, downloads the detection model once, and starts watching;
a bar in the corner shows the preview and what it is seeing, with a
**Stop camera** button.

From then on it is a camera like any other on the account: it shows up in
the camera picker, the Live tab, the events list and Telegram, and you draw
its couch zone the same way (a new station has no zone yet, so nothing
counts as being on the couch until you draw one).

What is different from the agent, and why:

- **Detection runs in the page**, with COCO-SSD under TensorFlow.js instead
  of MobileNet-SSD under OpenCV. Same classes and the same confidence
  threshold, but the model is fetched from a CDN, so a station needs
  internet the first time it starts -- where an agent only needs it to
  report.
- **The alarm plays from that device's speakers.** Browsers only allow that
  after a click, which is one more reason the station starts from a button.
- **The tab has to stay open and the screen awake.** A background tab gets
  its timers throttled and is handed fewer frames, which is exactly the
  wrong thing for a camera; the station asks for a screen wake lock where
  the browser offers one, and keeps its preview on screen. Leave the device
  plugged in: detection and an open camera are hungry.
- **Clip pre-roll is approximate.** `MediaRecorder` has no rewind, so a clip
  is one whole recording segment, recycled while nothing is happening --
  the lead-in before the alert comes out as up to `pre_roll + 2s` rather
  than exactly the pre-roll you set. Its clips are webm rather than mp4,
  and carry no total length, so the player's scrubber only fills in as it
  buffers. The video itself is fine.
- **One camera, one tab.** Starting the same camera somewhere else issues a
  new device token and the older tab stops itself on its next heartbeat,
  rather than two tabs fighting over one camera's events and frames. The
  token is never written to disk: it is requested when the station starts
  and forgotten when it stops.
- **It needs https** (or `localhost`): no browser gives a page a camera
  otherwise. A deployment following _Deploy online_ below already has it.

#### Station logs, and sharing them

A station logs what it is doing to the browser console with a `[station]`
prefix, and keeps the last 1000 lines in the tab: each start with the camera
it opened and at what resolution, the model download, every couch event and
every upload (with the HTTP status when one fails), settings arriving from
the dashboard, the server going away and coming back, clips queued with
their size, and a one-line summary of frame rate and what is in frame once a
minute, so a quiet night still shows the station was awake. The buffer
survives **Stop camera** and restarts -- a start that failed is exactly the
run worth reading.

That matters because a station usually runs on the phone or laptop by the
couch, whose console nobody is looking at. So on a **dev run** -- a server
started with `python -m server`, or anything with `DEV_MODE=1` -- the
Devices tab grows a **Share logs** button next to the station card. It posts
the buffer to the server, which writes it as one JSON file under
`data/station-logs/` (device, settings, last reported status, user agent and
the lines), prints the path to the server's terminal, and keeps the 50 most
recent reports.

On a normal deployment the flag is off, the button is hidden, and the
endpoint behind it does not exist -- so nothing a station logs leaves the
tab unless you ask for it on a dev server.

### Drawing the couch zone from the dashboard

On the **Live** tab, click **Draw couch zone** and click the couch's corners
in the video, going around it. Drag a corner to move it, double-click one to
remove it, and use the slider to set how much of the dog has to be inside to
count. **Save zone** sends it to the camera, which picks it up on its next
heartbeat -- a second or two, no restart.

The corners are stored as fractions of the frame rather than pixels, because
the browser only ever sees a scaled copy of the video. A side effect worth
having: changing the camera's resolution does not invalidate the zone.

Two things to know while editing:

- The orange outline burned into the video is the zone the camera is using
  now; the dashed green one is what you are drawing. They swap over a second
  or two after you save.
- A camera already calibrated with `calibrate.py` reports that zone to the
  dashboard, so the editor opens on it and you can adjust it rather than
  start over. Once you save from the dashboard, that zone wins and
  `config.yaml`'s `zone.points` is no longer consulted (it stays as a
  fallback for a camera with no server zone, and for `monitor.py`).

### Deploy online

**Any VPS with Docker** (simplest, includes automatic HTTPS):

1. Point a DNS record such as `couch.example.com` at the server.
2. Create `.env` next to `docker-compose.yml`:
   ```
   DOMAIN=couch.example.com
   SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
   ```
3. `docker compose up -d`

Caddy gets a TLS certificate automatically. Data (SQLite db and clips)
lives in the `couch-data` volume.

**A PaaS** (Render, Railway, Fly.io...): deploy the `Dockerfile` and set
the environment variables below. These hosts usually wipe the container
disk on redeploy, so either attach a persistent volume at `/data` or use
Postgres + S3.

| Variable                                    | Default                     | Meaning                                                                        |
| ------------------------------------------- | --------------------------- | ------------------------------------------------------------------------------ |
| `SECRET_KEY`                                | generated (dev only)        | Signs session cookies. **Required** in production.                             |
| `DATABASE_URL`                              | `sqlite:////data/server.db` | e.g. `postgresql+psycopg://user:pw@host/db`                                    |
| `STORAGE`                                   | `local`                     | `s3` to keep media in a bucket (AWS, Cloudflare R2, Backblaze B2, MinIO)       |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION` |                             | Bucket settings; credentials via `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` |
| `MEDIA_DIR`                                 | `/data/media`               | Where media goes with `STORAGE=local`                                          |
| `ALLOW_SIGNUP`                              | `1`                         | `0` closes registration once your household has accounts                       |
| `RETENTION_DAYS`                            | `30`                        | Events and clips older than this are deleted; `0` keeps forever                |
| `TRUST_PROXY`                               | `0`                         | `1` behind a reverse proxy / PaaS router (real client IPs, https)              |
| `SECURE_COOKIES`                            | `0`                         | `1` whenever the site is served over HTTPS                                     |
| `MAX_UPLOAD_MB`                             | `200`                       | Largest clip upload accepted                                                   |
| `DEV_MODE`                                  | `0`                         | Developer-only extras (the station's **Share logs** button). On automatically under `python -m server` |

**Run one server process.** The live view keeps the latest frame of each
camera in memory, so the image runs a single gunicorn worker with many
threads. That comfortably serves a household or a few dozen users; going
beyond that would mean moving the live relay to Redis.

### Important: one process owns the camera

A camera can only be opened by one process at a time, so **run either
`agent.py` or `monitor.py`, not both**. `monitor.py` still exists for a
standalone box with no server at all.

### Video clips

Clips include a few seconds of **pre-roll** from before the alert fired, so
you see the dog actually getting on rather than already sitting there. The
codec is probed at startup: H.264 where available (all browsers play it),
falling back to WebM/VP8. After a successful upload the agent deletes its
local copy (pass `--keep-local` to keep it).

#### Sound in the clips

Clips are recorded with sound from a microphone on the camera machine --
the whine before the jump, and your own voice on the alarm. It needs two
things the detection itself does not:

```bash
pip install sounddevice      # on Linux also: sudo apt install -y libportaudio2
```

plus **ffmpeg** on `PATH` (`winget install ffmpeg`, `brew install ffmpeg`,
`sudo apt install -y ffmpeg`), because OpenCV can only write video -- the
audio is muxed in once the clip is closed. Set `DFC_FFMPEG` instead if it
lives somewhere off `PATH`.

Check both halves, and list the input devices, with:

```bash
python -m audio
```

Then in `config.yaml`:

```yaml
audio:
  enabled: true
  device:          # empty = system default, or an index/name from the list
  sample_rate: 44100
  channels: 1
```

The buffer holds a little more than one full-length clip, so raising
`max_clip_sec` costs memory on the camera machine: ~88KB per second at
44.1kHz mono, i.e. ~5MB for the default 60s clip and ~54MB at the 600s
maximum. Drop `sample_rate` to 16000 if that matters on a small Pi -- a dog
and a doorbell are perfectly recognisable at 16kHz.

If the mic or ffmpeg is missing, clips are still recorded -- just silent,
and the dashboard's **Clip sound** reads `Silent` rather than `On`. Nothing
is ever recorded between events: the microphone feeds the same rolling
buffer the video pre-roll uses, and only the stretch belonging to a clip is
ever written to disk.

One side effect worth knowing: the muxed clip plays back at the frame rate
the camera really achieved rather than the configured one, so clips with
sound are no longer slightly fast or slow. That is the only way the audio
can stay in sync, and it makes the duration honest.

### Security

- Passwords are hashed; sessions are signed, HttpOnly cookies; every
  state-changing request needs a CSRF token; logins are rate-limited.
- Every query is scoped to the signed-in account: one user can't see,
  stream or delete another's cameras, events, clips or sounds (covered by
  `tests/test_server.py`).
- Each agent authenticates with its own random device token. The server
  keeps only a hash of it, and "New token" revokes the old one instantly.
- Telegram bot tokens live in the server database; they are never sent back
  to the browser or stored on the camera.
- **Always serve it over HTTPS** online (the compose file does). The agent
  warns if pointed at a remote `http://` URL. Browsers also only allow
  microphone recording on HTTPS or localhost.

## 5. Optional: push notifications to your phone (Telegram)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the
   prompts. You'll get a bot token (looks like `123456:ABC-DEF...`).
2. Message your new bot anything (so it's allowed to message you back).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   and find `"chat":{"id": ...}` in the response -- that's your chat id.
4. Enter both in the dashboard under **Settings > Telegram** and press
   "Test connection". The server then sends a photo when the dog gets on the
   couch and the clip once it is uploaded, for every camera on the account.

(Standalone `monitor.py` still reads `alert.telegram` from `config.yaml`.)

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
3. Redraw the couch zone for the Pi's camera -- its field of view will
   differ from your laptop's. Easiest from the dashboard's Live tab, since
   a headless Pi cannot show `calibrate.py`'s window. Then
   `python agent.py --server https://your-site` (or `python monitor.py --headless`)
   if the Pi has no monitor attached (this skips the preview window but
   keeps logging/snapshots/sound/Telegram working).

A Pi 4 or better runs MobileNet-SSD comfortably in real time on CPU; no
GPU or Coral accelerator needed for this use case.

## Project layout

```
agent.py        # camera agent: runs detection and reports to the server
cloud_client.py # agent <-> server: heartbeat, upload queue, live frames
service.py      # capture + detection loops (camera -> detector -> zone -> debounce)
recorder.py     # ring-buffered clip recorder with pre-roll
audio.py        # mic capture + ffmpeg mux, so clips have sound (python -m audio)
camera.py       # camera backends: OpenCV webcam today, picamera2 on a Pi later
detector.py     # MobileNet-SSD wrapper -- finds "dog" boxes in a frame
zone.py         # geometry: how much of a box overlaps the couch polygon
debounce.py     # turns noisy per-frame readings into clean enter/leave events
alert.py        # snapshots, local alarm playback, Telegram, CSV log
sirens.py       # the built-in sirens, synthesized (python -m sirens to hear)
sounds.py       # local cache of alert sounds downloaded from the server
calibrate.py    # one-time tool to draw the couch zone
monitor.py      # standalone loop with no server (local window, Telegram)
server/         # web backend (Flask + SQLAlchemy): API, storage, live relay
frontend/       # dashboard (static HTML/JS/CSS, served by the server)
frontend/station.js  # a camera station: the dashboard tab *as* the agent
Dockerfile, docker-compose.yml, Caddyfile   # deployment
tests/
```

Run the tests any time with:

```bash
pip install pytest -r requirements.txt -r server/requirements.txt
python -m pytest tests
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
- A camera station is only watching while its tab is open, and only
  reliably while that tab is visible -- it is the right answer for a spare
  laptop left pointing at the couch, and the wrong one for a camera that
  has to come back up by itself after a power cut. That is what the agent
  (and a service unit) is for.
- One camera per agent. For a second room, add another device in the
  dashboard and run a second agent with its own config and token
  (`python agent.py --config room2.yaml` with `DFC_TOKEN` set, since the
  saved `instance/device_token` belongs to the first one).
