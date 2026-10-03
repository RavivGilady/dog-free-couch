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

## 2. Choose your camera

A laptop usually registers more than one camera -- the real webcam, the
infrared Windows Hello sensor, a meeting app's virtual camera -- and OpenCV
addresses them by bare index, so the one it picks by default is often not
the one pointing at the couch.

```bash
python list_cameras.py
```

It finds every camera on the computer, names them (asking the OS, so you
see "Integrated Camera" rather than "index 0"), and says which ones give a
live picture. Then a preview window opens: press `n` / `p` to step through
them, and `s` on the one showing your couch. That writes the choice into
`config.yaml` -- comments and all other settings left alone -- and
everything else (`calibrate.py`, `monitor.py`, `agent.py`) picks it up.

Other ways to run it:

```bash
python list_cameras.py --list           # just list what was found
python list_cameras.py --no-preview     # choose from the list, no window
python list_cameras.py --index 1 --save # save a known choice outright
```

`--no-preview` is the one to use over SSH on a headless box.

On Windows, `pip install -r requirements.txt` also installs `pygrabber`,
which lets the tool read the DirectShow device order so the names line up
exactly with the indices. Without it the names still show, but they come
from the OS device list and may be shuffled -- trust the picture.

## 3. Calibrate the couch zone

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

## 4. Run the monitor

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

This is the camera picker from step 2: it names every camera, flags the
ones whose picture is frozen rather than live, and saves the one you pick
into `config.yaml` as both an index and a backend:

```yaml
camera:
  index: 1 # whatever index showed a live picture
  backend_api: dshow # whatever backend showed a live picture
```

`dshow` (DirectShow) fixes this for most people -- `monitor.py` and
`calibrate.py` already try it first automatically on Windows, so if you are
still seeing a frozen frame, it usually means index 0 is not your real
camera at all, which is exactly what the picker sorts out.

## 5. Web dashboard: server + camera agent (recommended)

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
- **Devices** - add, rename and remove cameras; issue a new token.

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

## 6. Optional: push notifications to your phone (Telegram)

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the
   prompts. You'll get a bot token (looks like `123456:ABC-DEF...`).
2. Message your new bot anything (so it's allowed to message you back).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   and find `"chat":{"id": ...}` in the response -- that's your chat id.
4. Enter both in the dashboard under **Settings > Telegram** and press
   "Test connection". The server then sends a photo when the dog gets on the
   couch and the clip once it is uploaded, for every camera on the account.

(Standalone `monitor.py` still reads `alert.telegram` from `config.yaml`.)

## 7. Moving to a Raspberry Pi later

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
service.py      # the detection loop (camera -> detector -> zone -> debounce)
recorder.py     # ring-buffered clip recorder with pre-roll
camera.py       # camera backends: OpenCV webcam today, picamera2 on a Pi later
detector.py     # MobileNet-SSD wrapper -- finds "dog" boxes in a frame
zone.py         # geometry: how much of a box overlaps the couch polygon
debounce.py     # turns noisy per-frame readings into clean enter/leave events
alert.py        # snapshots, local alarm playback, Telegram, CSV log
sirens.py       # the built-in sirens, synthesized (python -m sirens to hear)
sounds.py       # local cache of alert sounds downloaded from the server
cameras.py      # finds the computer's cameras and names them (per-OS lookup)
list_cameras.py # pick which camera to use; saves it into config.yaml
calibrate.py    # one-time tool to draw the couch zone
monitor.py      # standalone loop with no server (local window, Telegram)
server/         # web backend (Flask + SQLAlchemy): API, storage, live relay
frontend/       # dashboard (static HTML/JS/CSS, served by the server)
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
- One camera per agent. For a second room, add another device in the
  dashboard and run a second agent with its own config and token
  (`python agent.py --config room2.yaml` with `DFC_TOKEN` set, since the
  saved `instance/device_token` belongs to the first one).
