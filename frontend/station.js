/* Camera station: this browser tab *is* the agent.
 *
 * Same contract as agent.py -- detection happens where the camera is, and
 * only events, clips and (while someone is watching) frames go to the
 * server -- except that "where the camera is" is this page. It talks to the
 * very same /api/agent endpoints with a device token, so the server, the
 * dashboard, Telegram and the event history can't tell the difference.
 *
 * What is ported from the agent, and where from:
 *   zone.py       -> overlapFraction(), by clipping instead of rasterizing
 *   debounce.py   -> makeTracker(), frame counts unchanged
 *   service.py    -> the detect loop, annotate(), maybePlaySound()
 *   cloud_client.py -> makeUploader(), heartbeatLoop(), liveLoop()
 *
 * Two things genuinely differ:
 *   - detection is COCO-SSD under TensorFlow.js, loaded from a CDN, rather
 *     than MobileNet-SSD under OpenCV. Same classes ("dog", "person"), same
 *     confidence threshold; a station therefore needs internet on first
 *     start, where an agent only needs it to report.
 *   - the alarm plays out of this tab's speakers, and the built-in sirens
 *     are fetched from the server as WAVs instead of being synthesized
 *     locally (the server already renders them for the preview buttons).
 *
 * The token is never written to disk. It is requested when the station
 * starts and lives in this closure until it stops, so closing the tab
 * leaves nothing behind, and starting the same camera in a second tab
 * rotates the token and retires the first one (see browser_token in
 * server/api.py).
 */
const Station = (() => {
  const TFJS_SRC =
    "https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@4.22.0/dist/tf.min.js";
  const COCO_SRC =
    "https://cdn.jsdelivr.net/npm/@tensorflow-models/coco-ssd@2.2.3/dist/coco-ssd.min.js";

  /* Detection is the expensive part, and a dog settling onto a couch is not
   * a fast event: a few frames a second is plenty and leaves the machine
   * usable. The frame counts below are the agent's, unchanged, so "5 frames
   * on the couch" is about a second here rather than a third of one. */
  const DETECT_FPS = 6;
  const CONFIDENCE = 0.5;
  const ENTER_FRAMES = 5;
  const EXIT_FRAMES = 8;
  const MIN_ALERT_INTERVAL_SEC = 30;

  const HEARTBEAT_MS = 2000;
  /* One "still here, this is what I see" line a minute. Without it a quiet
   * night leaves no trace at all, and "was it even running?" is the first
   * question any shared log has to answer. */
  const LOG_SUMMARY_SEC = 60;
  // 0 = no cap: the live view goes as fast as the camera makes new frames
  // and the previous upload finishes (see liveLoop).
  const LIVE_MAX_FPS = 0;
  // The live view is a preview, not the recording: a small frame uploads
  // several times faster than a full-size one, and upload speed is what
  // limits it. Frames go up a few at a time so the round trip to the server
  // doesn't set the pace either.
  const LIVE_MAX_WIDTH = 640;
  const LIVE_JPEG_QUALITY = 0.5;
  const LIVE_IN_FLIGHT = 3;
  const MAX_BOXES = 20;
  /* A clip is one MediaRecorder segment, so its pre-roll is however long the
   * segment has been running. Segments are recycled while nothing is
   * happening to keep that from growing without bound -- see makeRecorder. */
  const SEGMENT_SLACK_SEC = 2;

  const DEFAULTS = {
    alert: { play_sound: true, repeat_sound_sec: 3, active_sound: "builtin" },
    video: {
      enabled: true,
      pre_roll_sec: 4,
      max_clip_sec: 60,
      post_roll_sec: 3,
    },
    zone: { points: [], overlap_threshold: 0.35 },
  };

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const nowSec = () => Date.now() / 1000;

  let dev = null; // {id, name} of the device this station drives
  let token = "";
  let stream = null;
  const det = {
    worker: null,
    ready: false,
    progress: -1,
    pending: new Map(),
    seq: 0,
  };
  let video = null;
  let raw = null; // the frame as the camera gave it: snapshots, detection
  let shown = null; // the newest frame with zone and boxes drawn on: live view
  let overlay = null; // just the zone and boxes, on a transparent layer
  let settings = clone(DEFAULTS);
  let tracker = null;
  let recorder = null;
  let uploader = null;
  let alarm = null;
  let wakeLock = null;
  // What start() is doing right now, for the UI: opening the camera and
  // downloading the model take seconds, and a station that says nothing
  // while it waits is indistinguishable from a button that did nothing.
  let phase = null;
  let running = false;
  // Bumped on every start: the loops below run until *their* generation is
  // retired, so a restart can never leave the previous camera's loops alive.
  let generation = 0;
  let liveWanted = false;
  let lastSoundAt = -Infinity;
  let openEvent = null; // {id, start} of the couch session being uploaded
  let frameGaps = [];
  let onChange = () => {};

  let status = blankStatus();
  let paused = false;
  let videoWanted = null; // what start() asked the browser for, to reopen with
  let switching = false; // a pause or resume is in flight

  function blankStatus() {
    return {
      running: false,
      camera_ok: false,
      audio_ok: false,
      dog_on_couch: false,
      dogs_in_frame: 0,
      persons_in_frame: 0,
      paused: false,
      fps: 0,
      recording: false,
      last_error: null,
      started_at: null,
      upload_queue: 0,
      zone_points: [],
    };
  }

  function publish(patch) {
    if (patch) Object.assign(status, patch);
    onChange(snapshot());
  }

  function snapshot() {
    return {
      ...status,
      device: dev,
      running,
      phase,
      starting: !!phase,
      // 0..1 while the model downloads, -1 when its size isn't known yet,
      // null when there is nothing loading.
      model_progress: det.worker && !det.ready ? det.progress : null,
    };
  }

  /* ================= logs ================= */

  /* The agent prints its progress; so does this, with the same prefix, so
   * "nothing happened" can always be chased down in the console.
   *
   * Every line is also kept here, because a station usually runs on the
   * phone or laptop by the couch -- not on the machine whose console
   * anyone is looking at -- and the interesting ones happen while nobody
   * is watching: a camera lost at 3am, a clip that never uploaded, the
   * model reloading. The buffer is a ring, so a station left running for
   * days costs a bounded amount of memory, and it survives stop() and
   * restarts: a start that failed is exactly the run worth reading.
   *
   * It stays in the tab: nothing uploads it and nothing writes it to disk,
   * so reading it means being on the device with the camera, either in
   * that console or by typing Station.logLines() into it.
   */
  const LOG_LIMIT = 1000;
  const logBuffer = [];

  function record(level, message) {
    const msg = String(message);
    logBuffer.push({ t: nowSec(), level, msg });
    if (logBuffer.length > LOG_LIMIT) logBuffer.shift();
    const line = `[station] ${msg}`;
    if (level === "error") console.error(line);
    else if (level === "warn") console.warn(line);
    else console.log(line);
  }

  const log = (message) => record("info", message);
  const warn = (message) => record("warn", message);
  const logError = (message) => record("error", message);

  function setPhase(message) {
    phase = message;
    if (message) log(message);
    publish();
  }

  function fail(message) {
    // The detect loop can hit the same failure every frame; the log wants
    // it once, when it starts, not six times a second.
    if (message && message !== status.last_error) logError(message);
    status.last_error = message;
    publish();
  }

  /* ================= geometry (zone.py) ================= */

  /* Fraction of the dog's box that falls inside the couch polygon.
   *
   * zone.py rasterizes the polygon and counts the pixels under the box.
   * Clipping the polygon to the box and comparing areas gives the same
   * number exactly, with no pixels to walk -- and clipping *the polygon*
   * against *the box* (rather than the other way round) keeps it correct for
   * a polygon that isn't convex, because the clip region, a rectangle,
   * always is.
   *
   * Everything here is in fractions of the frame, 0..1, which is how the
   * dashboard stores a drawn zone and how boxes are normalized below, so no
   * resolution ever enters into it. */
  function overlapFraction(box, poly) {
    const [x1, y1, x2, y2] = box;
    const boxArea = (x2 - x1) * (y2 - y1);
    if (!poly || poly.length < 3 || boxArea <= 0) return 0;

    let clipped = poly.map(([x, y]) => [x, y]);
    const edges = [
      (p) => p[0] >= x1,
      (p) => p[0] <= x2,
      (p) => p[1] >= y1,
      (p) => p[1] <= y2,
    ];
    const cuts = [
      (a, b) => [x1, lerpY(a, b, x1)],
      (a, b) => [x2, lerpY(a, b, x2)],
      (a, b) => [lerpX(a, b, y1), y1],
      (a, b) => [lerpX(a, b, y2), y2],
    ];
    for (let e = 0; e < edges.length && clipped.length; e++) {
      const inside = edges[e];
      const cut = cuts[e];
      const out = [];
      for (let i = 0; i < clipped.length; i++) {
        const cur = clipped[i];
        const prev = clipped[(i + clipped.length - 1) % clipped.length];
        const curIn = inside(cur);
        if (curIn !== inside(prev)) out.push(cut(prev, cur));
        if (curIn) out.push(cur);
      }
      clipped = out;
    }
    return Math.min(1, area(clipped) / boxArea);
  }

  const lerpY = (a, b, x) =>
    b[0] === a[0] ? a[1] : a[1] + ((b[1] - a[1]) * (x - a[0])) / (b[0] - a[0]);
  const lerpX = (a, b, y) =>
    b[1] === a[1] ? a[0] : a[0] + ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]);

  function area(poly) {
    let sum = 0;
    for (let i = 0; i < poly.length; i++) {
      const [x1, y1] = poly[i];
      const [x2, y2] = poly[(i + 1) % poly.length];
      sum += x1 * y2 - x2 * y1;
    }
    return Math.abs(sum) / 2;
  }

  /* ================= debounce (debounce.py) ================= */

  function makeTracker() {
    let state = "OFF";
    let pos = 0;
    let neg = 0;
    let lastAlert = -Infinity;
    let sessionStart = null;

    return {
      get state() {
        return state;
      },
      update(onCouch) {
        const now = nowSec();
        if (onCouch) {
          pos++;
          neg = 0;
        } else {
          neg++;
          pos = 0;
        }
        if (state === "OFF" && pos >= ENTER_FRAMES) {
          state = "ON";
          sessionStart = now;
          if (now - lastAlert >= MIN_ALERT_INTERVAL_SEC) {
            lastAlert = now;
            return { type: "entered", start: sessionStart };
          }
        } else if (state === "ON" && neg >= EXIT_FRAMES) {
          state = "OFF";
          const ev = {
            type: "left",
            start: sessionStart,
            end: now,
            duration: now - sessionStart,
          };
          sessionStart = null;
          return ev;
        }
        return null;
      },
    };
  }

  /* ================= uploads (cloud_client.Uploader) ================= */

  /* One FIFO worker, so an event always exists before its snapshot and clip
   * are attached, and the detect loop never waits on the network. The
   * endpoints are keyed by our own client_id and idempotent, so a retry
   * after a half-finished attempt is safe. */
  function makeUploader(authToken) {
    const q = [];
    let pumping = false;
    let alive = true;

    async function send(job) {
      const headers = { Authorization: `Bearer ${authToken}` };
      let res;
      if (job.kind === "create" || job.kind === "end") {
        const path =
          job.kind === "create" ? "/events" : `/events/${job.client_id}/end`;
        res = await fetch(`/api/agent${path}`, {
          method: "POST",
          headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify(job.body),
        });
      } else {
        const form = new FormData();
        form.append("file", job.blob, job.filename);
        res = await fetch(`/api/agent/events/${job.client_id}/${job.kind}`, {
          method: "POST",
          headers,
          body: form,
        });
      }
      if (res.ok) {
        log(`uploaded ${job.kind} for event ${job.client_id}`);
        return "ok";
      }
      // Other 4xx won't succeed on retry (bad input); anything else -- server
      // down, rate limited -- is worth waiting out. 401 is terminal here,
      // unlike on an agent: this token was handed out for one run, and once
      // another tab has rotated it no amount of waiting brings it back.
      const hopeless =
        res.status >= 400 &&
        res.status < 500 &&
        ![408, 429].includes(res.status);
      warn(
        `${job.kind} for event ${job.client_id} got HTTP ${res.status}; ` +
          (hopeless ? "giving up on it" : "will retry"),
      );
      return hopeless ? "drop" : "retry";
    }

    async function pump() {
      if (pumping) return;
      pumping = true;
      while (alive && q.length) {
        let delay = 2000;
        for (;;) {
          let outcome = "retry";
          try {
            outcome = await send(q[0]);
          } catch (e) {
            outcome = "retry"; // offline, or the page is going away
            warn(
              `could not send ${q[0].kind} for event ${q[0].client_id}: ` +
                `${e.message || e}; retrying in ${Math.round(delay / 1000)}s`,
            );
          }
          if (outcome !== "retry" || !alive) break;
          await sleep(delay);
          delay = Math.min(delay * 2, 300000);
        }
        q.shift();
        publish({ upload_queue: q.length });
      }
      pumping = false;
    }

    return {
      submit(job) {
        q.push(job);
        publish({ upload_queue: q.length });
        pump();
      },
      pending: () => q.length,
      close() {
        alive = false;
      },
    };
  }

  /* ================= alarm ================= */

  /* Built-in sirens come from the server as WAVs rather than being
   * synthesized the way sirens.py does it on an agent: the server already
   * renders them for the dashboard's preview buttons, and one fetch per
   * sound, cached here, beats a second implementation of the oscillators.
   *
   * The AudioContext is created inside the click that starts the station,
   * which is what keeps a browser from muting the alarm later. */
  function makeAlarm() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = Ctx ? new Ctx() : null;
    const buffers = new Map();
    let current = null;

    async function buffer(url) {
      if (!buffers.has(url)) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        buffers.set(url, await ctx.decodeAudioData(await res.arrayBuffer()));
      }
      return buffers.get(url);
    }

    function url(active) {
      // A number is a recording made in the dashboard; anything else names
      // one of the built-in sirens.
      return typeof active === "number"
        ? `/api/sounds/${active}/audio`
        : `/api/sounds/builtin/${encodeURIComponent(active || "builtin")}/audio`;
    }

    return {
      unlock() {
        if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
      },
      async play(active) {
        if (!ctx) return;
        this.unlock();
        try {
          const src = ctx.createBufferSource();
          src.buffer = await buffer(url(active));
          src.connect(ctx.destination);
          src.start();
          current = src;
        } catch (e) {
          fail(`Could not play the alert sound: ${e.message}`);
        }
      },
      silence() {
        try {
          current?.stop();
        } catch (_) {}
        current = null;
      },
      close() {
        this.silence();
        ctx?.close().catch(() => {});
      },
    };
  }

  function maybePlaySound() {
    const a = settings.alert;
    if (!a.play_sound) return;
    if (tracker.state !== "ON") {
      lastSoundAt = -Infinity;
      return;
    }
    const repeat = a.repeat_sound_sec || 0;
    const now = nowSec();
    if (now - lastSoundAt >= repeat) {
      // Only the first one per session: a 3s repeat would otherwise push
      // everything else out of a 1000-line buffer in under an hour.
      if (lastSoundAt === -Infinity)
        log(`sounding the alarm (${a.active_sound})`);
      alarm.play(a.active_sound);
      // 0 means "once when it gets on, then silence", which is what an
      // infinite last-played time says.
      lastSoundAt = repeat ? now : Infinity;
    }
  }

  /* ================= clips ================= */

  /* Pre-roll ("the seconds before the alert, so you see the jump") needs
   * video that was recorded before we knew we wanted it. The agent keeps a
   * ring of decoded frames; MediaRecorder has no such rewind, and its
   * chunks after the first are useless on their own -- only the first one
   * carries the webm header.
   *
   * So a clip is simply one whole recording segment: start to finish of a
   * MediaRecorder run. While nothing is happening the segment is thrown
   * away and restarted once it is older than the pre-roll, which bounds how
   * much lead-in a clip can open with; when an event starts, the segment in
   * progress is kept and left running until the dog leaves plus post-roll.
   * Lead-in is therefore up to pre_roll + 2s rather than exactly pre_roll.
   */
  function makeRecorder(mediaStream) {
    // With a mic in the stream the codec string has to name an audio codec
    // too: asking for "codecs=vp9" alone gets a video-only recording out of
    // a stream that has sound in it, which is exactly the silent clip this
    // is here to avoid. The video-only strings stay as the fallback for a
    // station whose mic was refused, or a browser that won't do opus.
    const hasAudio = mediaStream.getAudioTracks().length > 0;
    // MP4 comes first because Telegram only plays H.264 MP4 inline; a webm
    // arrives as a file attachment. Only Chrome 126+, Safari and recent Edge
    // can record it, so webm stays as the fallback (Firefox, older browsers).
    const mime = [
      ...(hasAudio ? ['video/mp4;codecs="avc1,mp4a.40.2"'] : []),
      "video/mp4;codecs=avc1",
      ...(hasAudio
        ? [
            'video/webm;codecs="vp9,opus"',
            'video/webm;codecs="vp8,opus"',
            "video/webm",
          ]
        : []),
      "video/webm;codecs=vp9",
      "video/webm;codecs=vp8",
      "video/webm",
    ].find((m) => window.MediaRecorder?.isTypeSupported?.(m));
    if (!mime) {
      warn("this browser records no video format we can upload; clips off");
      return null;
    }
    log(`recording clips as ${mime}`);

    let rec = null;
    let chunks = [];
    let segmentStart = 0;
    let event = null; // {client_id, start} while a clip is being kept
    let endAt = null; // when the kept clip should be finalized
    let keep = false; // hand the next assembled blob to the uploader

    function begin() {
      chunks = [];
      segmentStart = nowSec();
      rec = new MediaRecorder(mediaStream, {
        mimeType: mime,
        videoBitsPerSecond: 1500000,
        audioBitsPerSecond: 96000,
      });
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size) chunks.push(e.data);
      };
      rec.onstop = () => {
        const done = keep ? event : null;
        const parts = chunks;
        keep = false;
        event = null;
        endAt = null;
        rec = null;
        if (done && parts.length) {
          const blob = new Blob(parts, { type: mime });
          log(
            `clip for event ${done.client_id}: ` +
              `${(blob.size / 1048576).toFixed(1)} MB over ` +
              `${Math.round(nowSec() - segmentStart)}s, queued for upload`,
          );
          uploader.submit({
            kind: "video",
            client_id: done.client_id,
            blob,
            filename: `clip.${mime.includes("webm") ? "webm" : "mp4"}`,
          });
        }
        publish({ recording: false });
        if (running && settings.video.enabled) begin();
      };
      // One blob per second: the segment is only ever used whole, but
      // slicing keeps each piece small enough to hold in memory.
      rec.start(1000);
    }

    return {
      get recording() {
        return !!event;
      },
      sync() {
        // Recording nothing costs nothing: with clips off there is no
        // recorder at all, which matters on a laptop's battery.
        if (settings.video.enabled && !rec) begin();
        else if (!settings.video.enabled && rec) {
          keep = false;
          event = null;
          rec.stop();
        }
      },
      start(clientId) {
        if (!rec) {
          warn(`no recorder, so event ${clientId} gets no clip`);
          return;
        }
        log(
          `keeping this segment for event ${clientId} ` +
            `(${Math.round(nowSec() - segmentStart)}s of lead-in)`,
        );
        event = { client_id: clientId, start: nowSec() };
        keep = true;
        endAt = null;
        publish({ recording: true });
      },
      stop() {
        if (event) endAt = nowSec() + settings.video.post_roll_sec;
      },
      tick() {
        if (!rec) return;
        const age = nowSec() - segmentStart;
        if (!event) {
          if (age > settings.video.pre_roll_sec + SEGMENT_SLACK_SEC) {
            keep = false;
            rec.stop(); // onstop starts the next segment
          }
          return;
        }
        // Cap the clip even if the dog never leaves, like ClipRecorder does.
        if (age >= settings.video.max_clip_sec || (endAt && nowSec() >= endAt))
          rec.stop();
      },
      finish() {
        if (rec) {
          keep = !!event;
          rec.stop();
        }
      },
    };
  }

  /* ================= drawing (service._annotate) ================= */

  /* The boxes are drawn on a transparent layer and laid over the newest
   * video frame whenever one is sent, so the live view runs at the camera's
   * pace and not at the detector's. */
  function compose() {
    if (!shown.width || video.readyState < 2) return;
    const ctx = shown.getContext("2d");
    ctx.drawImage(video, 0, 0, shown.width, shown.height);
    ctx.drawImage(overlay, 0, 0, shown.width, shown.height);
  }

  function annotate(boxes, zone) {
    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    const w = overlay.width;
    const h = overlay.height;

    if (zone.length >= 3) {
      ctx.beginPath();
      zone.forEach(([x, y], i) =>
        i ? ctx.lineTo(x * w, y * h) : ctx.moveTo(x * w, y * h),
      );
      ctx.closePath();
      ctx.fillStyle = "rgba(255, 200, 0, 0.15)";
      ctx.fill();
      ctx.strokeStyle = "rgb(255, 200, 0)";
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    ctx.font = "16px system-ui, sans-serif";
    ctx.lineWidth = 2;
    for (const b of boxes) {
      const color =
        b.label === "person"
          ? "rgb(0, 160, 255)"
          : b.inZone
            ? "rgb(255, 60, 60)"
            : "rgb(60, 220, 60)";
      const [x1, y1, x2, y2] = b.box;
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.strokeRect(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h);
      ctx.fillText(
        `${b.label} ${b.score.toFixed(2)}${b.inZone ? " IN ZONE" : ""}`,
        x1 * w,
        Math.max(14, y1 * h - 6),
      );
    }

    if (recorder?.recording) {
      ctx.fillStyle = "rgb(255, 60, 60)";
      ctx.beginPath();
      ctx.arc(w - 24, 22, 8, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillText("REC", w - 72, 28);
    }
    compose();
  }

  /* ================= the loop (service._run) ================= */

  async function detectLoop(gen) {
    const interval = 1000 / DETECT_FPS;
    let last = performance.now();
    let lastSummary = nowSec();

    while (running && gen === generation) {
      const started = performance.now();
      try {
        await detectOnce();
      } catch (e) {
        fail(e.message || String(e));
      }
      frameGaps.push(started - last);
      if (frameGaps.length > 30) frameGaps.shift();
      const avg = frameGaps.reduce((a, b) => a + b, 0) / frameGaps.length;
      status.detect_fps = avg > 0 ? Math.round((1000 / avg) * 10) / 10 : 0;
      // Nobody watching means no live frames to measure: fall back to the
      // detection rate, which is all the station is doing then.
      if (!liveWanted) status.fps = status.detect_fps;
      last = started;
      recorder?.tick();
      if (nowSec() - lastSummary >= LOG_SUMMARY_SEC) {
        lastSummary = nowSec();
        log(
          `${status.fps} fps, ${status.dogs_in_frame} dog(s), ` +
            `${status.persons_in_frame} person(s), ` +
            `${tracker.state === "ON" ? "on the couch" : "couch clear"}` +
            `${status.recording ? ", recording" : ""}` +
            `${uploader.pending() ? `, ${uploader.pending()} to upload` : ""}` +
            `${status.last_error ? `, error: ${status.last_error}` : ""}`,
        );
      }
      await sleep(Math.max(0, interval - (performance.now() - started)));
    }
  }

  async function detectOnce() {
    // Paused: the camera is released, so there is nothing to look at (and
    // nothing to mistake for a lost feed).
    if (paused || !stream) return;
    if (!video.videoWidth || video.readyState < 2) {
      // A paused or still-starting video is not a lost camera; a stream that
      // ended (webcam unplugged, another app took it) is.
      const live = stream?.getVideoTracks()?.[0]?.readyState === "live";
      publish({
        camera_ok: live,
        last_error: live ? status.last_error : "Lost the camera feed",
      });
      return;
    }
    if (raw.width !== video.videoWidth || raw.height !== video.videoHeight) {
      raw.width = overlay.width = video.videoWidth;
      raw.height = overlay.height = video.videoHeight;
      shown.width = Math.min(LIVE_MAX_WIDTH, video.videoWidth);
      shown.height = Math.round(
        (video.videoHeight * shown.width) / video.videoWidth,
      );
    }
    const w = raw.width;
    const h = raw.height;
    raw.getContext("2d").drawImage(video, 0, 0, w, h);

    const zone = settings.zone.points;
    if (!det.ready) {
      // No model yet (or it failed): keep the camera and live view going.
      publish({ camera_ok: true, zone_points: zone });
      annotate([], zone);
      return;
    }
    const preds = await detectRemote(raw);
    const threshold = settings.zone.overlap_threshold;

    const boxes = [];
    let dogs = 0;
    let persons = 0;
    let onCouch = false;
    let best = 0;
    for (const p of preds) {
      if (p.class !== "dog" && p.class !== "person") continue;
      const box = [
        p.bbox[0] / w,
        p.bbox[1] / h,
        (p.bbox[0] + p.bbox[2]) / w,
        (p.bbox[1] + p.bbox[3]) / h,
      ];
      const inZone =
        zone.length >= 3 && overlapFraction(box, zone) >= threshold;
      boxes.push({ label: p.class, box, inZone, score: p.score });
      if (p.class === "person") {
        persons++;
        continue;
      }
      dogs++;
      if (inZone) {
        onCouch = true;
        best = Math.max(best, p.score);
      }
    }

    publish({
      camera_ok: true,
      dogs_in_frame: dogs,
      persons_in_frame: persons,
      dog_on_couch: onCouch,
      zone_points: zone,
    });

    const event = tracker.update(onCouch);
    if (event) await handleEvent(event, best);
    maybePlaySound();
    annotate(boxes, zone);
  }

  async function handleEvent(event, confidence) {
    if (event.type === "entered") {
      const clientId = clientKey();
      log(
        `dog on the couch (confidence ${confidence ? confidence.toFixed(2) : "?"}), ` +
          `event ${clientId}`,
      );
      uploader.submit({
        kind: "create",
        client_id: clientId,
        body: {
          client_id: clientId,
          start_ts: event.start,
          confidence: confidence ? Math.round(confidence * 1000) / 1000 : null,
        },
      });
      const jpeg = await blobOf(raw, 0.9);
      if (jpeg)
        uploader.submit({
          kind: "snapshot",
          client_id: clientId,
          blob: jpeg,
          filename: "snapshot.jpg",
        });
      recorder?.start(clientId);
      openEvent = { id: clientId, start: event.start };
    } else if (event.type === "left") {
      log(
        `dog left after ${Math.round(event.duration)}s` +
          (openEvent ? ` (event ${openEvent.id})` : " (no open event)"),
      );
      if (openEvent)
        uploader.submit({
          kind: "end",
          client_id: openEvent.id,
          body: {
            end_ts: event.end,
            duration: Math.round(event.duration * 10) / 10,
          },
        });
      openEvent = null;
      recorder?.stop();
      alarm.silence();
    }
  }

  /* Events are addressed by an id we make up, which is what makes every
   * upload idempotent and retryable. The server only insists on 8-64
   * url-safe characters (see _CLIENT_ID in server/agent_api.py). */
  function clientKey() {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    return btoa(String.fromCharCode(...bytes))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  const blobOf = (canvas, quality) =>
    new Promise((resolve) =>
      canvas.toBlob((b) => resolve(b), "image/jpeg", quality),
    );

  /* ================= server link (cloud_client.AgentLink) ================= */

  async function heartbeatLoop(gen) {
    while (running && gen === generation) {
      try {
        const res = await fetch("/api/agent/heartbeat", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            status: { ...status, upload_queue: uploader.pending() },
          }),
        });
        if (res.status === 401) {
          // The only way this happens is another tab (or a rotated token)
          // taking the camera over. Two stations on one camera would fight
          // over events and frames, so this one steps aside.
          warn("the server rejected our token: this camera was taken over");
          await stop(
            "This camera was started somewhere else, so this tab stopped.",
          );
          return;
        }
        if (res.ok) {
          apply(await res.json());
          if (status.last_error === OFFLINE) {
            log("the server is reachable again");
            publish({ last_error: null });
          }
        }
      } catch (e) {
        if (status.last_error !== OFFLINE)
          warn(`lost the server: ${e.message || e}; queueing here`);
        publish({ last_error: OFFLINE });
      }
      await sleep(HEARTBEAT_MS);
    }
  }

  const OFFLINE = "Can't reach the server; events are queued here.";

  function apply(reply) {
    const before = JSON.stringify(settings);
    for (const section of Object.keys(DEFAULTS)) {
      settings[section] = {
        ...DEFAULTS[section],
        ...(reply.settings?.[section] || {}),
      };
    }
    // Settings arrive on every heartbeat, so only a change is worth a line
    // -- and it is worth one: "it stopped alerting" is usually a zone or a
    // threshold that was edited in the dashboard, not the detector.
    const after = JSON.stringify(settings);
    if (after !== before) log(`settings from the server: ${after}`);
    if (!!reply.live_wanted !== liveWanted)
      log(reply.live_wanted ? "someone is watching" : "nobody is watching");
    liveWanted = !!reply.live_wanted;
    if (!!reply.paused !== paused) {
      log(reply.paused ? "paused from the dashboard" : "resumed from the dashboard");
      paused = !!reply.paused;
      publish({ paused });
    }
    // Also retried on every heartbeat, so a camera that failed to reopen
    // gets another go instead of staying dark.
    if (running && !switching) {
      if (paused && stream) pauseCamera();
      else if (!paused && !stream) resumeCamera();
    }
    recorder?.sync();
    for (const cmd of reply.commands || []) {
      log(`command from the dashboard: ${cmd.type}`);
      if (cmd.type === "test_sound") alarm.play(settings.alert.active_sound);
    }
  }

  /* Paused means the camera is off, not just ignored: the tracks are
   * stopped (the camera light goes out) and reopened on resume. The tab and
   * its heartbeat stay up, which is how a resume reaches us. */
  async function pauseCamera() {
    switching = true;
    try {
      liveWanted = false;
      recorder?.finish();
      recorder = null;
      alarm?.silence();
      if (openEvent) {
        const end = nowSec();
        uploader.submit({
          kind: "end",
          client_id: openEvent.id,
          body: {
            end_ts: end,
            duration: Math.round((end - openEvent.start) * 10) / 10,
          },
        });
        openEvent = null;
      }
      tracker = makeTracker();
      stream?.getTracks().forEach((t) => t.stop());
      stream = null;
      if (video) video.srcObject = null;
      publish({
        camera_ok: false,
        dog_on_couch: false,
        dogs_in_frame: 0,
        persons_in_frame: 0,
        recording: false,
        fps: 0,
        detect_fps: 0,
        last_error: null,
      });
      log("camera released");
    } finally {
      switching = false;
    }
  }

  // The microphone goes into the clips, so it is asked for together with the
  // camera -- the browser prompts for both at once. Refusing the mic refuses
  // the whole call, though, and a station with no sound beats no station at
  // all, so that drops back to video alone.
  async function openStream() {
    try {
      return await navigator.mediaDevices.getUserMedia({
        video: videoWanted,
        // The alarm is going off in the same room: processing meant for
        // a voice call would gate it away as noise, and the alarm is
        // half of what makes the clip worth hearing.
        audio: { echoCancellation: false, noiseSuppression: false },
      });
    } catch (e) {
      log(`no microphone (${e.name || e.message}); clips will be silent`);
      return navigator.mediaDevices.getUserMedia({
        video: videoWanted,
        audio: false,
      });
    }
  }

  async function resumeCamera() {
    switching = true;
    try {
      stream = await openStream();
      video.srcObject = stream;
      await video.play().catch(() => {});
      recorder = makeRecorder(stream);
      recorder?.sync();
      publish({ camera_ok: true, last_error: null });
      log("camera reopened");
    } catch (e) {
      stream?.getTracks().forEach((t) => t.stop());
      stream = null;
      publish({ last_error: `Couldn't reopen the camera: ${e.message}` });
      log(`could not reopen the camera: ${e.message}`);
    } finally {
      switching = false;
    }
  }

  async function liveLoop(gen) {
    const interval = LIVE_MAX_FPS ? 1000 / LIVE_MAX_FPS : 0;
    const sentAt = [];
    // Wait for the camera's next frame so an uncapped loop doesn't send the
    // same picture twice; the timeout covers a hidden tab, where the
    // callback may never fire.
    const nextVideoFrame = () =>
      video.requestVideoFrameCallback
        ? new Promise((resolve) => {
            const t = setTimeout(resolve, 200);
            video.requestVideoFrameCallback(() => {
              clearTimeout(t);
              resolve();
            });
          })
        : sleep(33);
    let inFlight = 0;
    let backoffUntil = 0;
    const send = async (jpeg) => {
      inFlight++;
      try {
        const res = await fetch("/api/agent/frame", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "image/jpeg",
            // Uploads overlap, so they can land out of order; the server
            // keeps only the newest.
            "X-Frame-Seq": String(Math.floor(performance.timeOrigin + performance.now())),
          },
          body: jpeg,
        });
        if (res.ok && !(await res.json()).live_wanted) liveWanted = false;
        // The rate the dashboard actually sees, over the last 30 frames.
        sentAt.push(performance.now());
        if (sentAt.length > 30) sentAt.shift();
        if (sentAt.length > 1) {
          const span = sentAt[sentAt.length - 1] - sentAt[0];
          status.fps =
            Math.round(((sentAt.length - 1) / (span / 1000)) * 10) / 10;
        }
      } catch (_) {
        backoffUntil = performance.now() + 1000;
      } finally {
        inFlight--;
      }
    };
    while (running && gen === generation) {
      const started = performance.now();
      if (liveWanted && shown.width) {
        if (inFlight >= LIVE_IN_FLIGHT || started < backoffUntil) {
          await sleep(5);
          continue;
        }
        try {
          if (!LIVE_MAX_FPS) await nextVideoFrame();
          compose();
          const jpeg = await blobOf(shown, LIVE_JPEG_QUALITY);
          if (jpeg) send(jpeg);
        } catch (_) {
          await sleep(1000);
        }
      }
      if (!liveWanted) sentAt.length = 0;
      await sleep(
        Math.max(
          interval - (performance.now() - started),
          liveWanted ? 0 : 500,
        ),
      );
    }
  }

  /* ================= start / stop ================= */

  /* A blocked CDN is the likeliest way for the model to fail, and a blackholed
   * request neither loads nor errors, so the load gets a deadline. */
  const LOAD_TIMEOUT_MS = 30000;

  /* The model lives in a worker (detector-worker.js), started in the
   * background: the camera, the live view and the heartbeat come up without
   * waiting for it, and a model that fails to download or load is reported
   * and retried instead of taking the camera down with it. The worker
   * outlives a stop, so pressing Start again is instant. */
  const DETECTOR_RETRY_MS = 15000;
  const NO_DETECTOR = "Detection isn't running yet";

  function startDetector(gen) {
    if (det.worker) return; // loading, or already up
    const began = nowSec();
    log("loading the detection model (in a worker)");
    const worker = new Worker("/assets/detector-worker.js");
    det.worker = worker;
    det.ready = false;
    det.progress = -1;
    publish();

    const giveUp = (why) => {
      if (det.worker !== worker) return;
      worker.terminate();
      det.worker = null;
      det.ready = false;
      for (const p of det.pending.values()) p.reject(new Error(why));
      det.pending.clear();
      logError(`detection model: ${why}`);
      if (running && gen === generation) {
        publish({ last_error: `${NO_DETECTOR}: ${why}` });
        setTimeout(() => {
          if (running && gen === generation) startDetector(gen);
        }, DETECTOR_RETRY_MS);
      }
    };
    const timer = setTimeout(
      () =>
        giveUp(
          "the model did not load within " +
            `${Math.round(LOAD_TIMEOUT_MS / 1000)}s -- check this device's ` +
            "internet, or any blocker on cdn.jsdelivr.net and " +
            "storage.googleapis.com",
        ),
      LOAD_TIMEOUT_MS * 2,
    );

    worker.onmessage = (e) => {
      const m = e.data;
      if (m.type === "ready") {
        clearTimeout(timer);
        det.ready = true;
        publish();
        log(
          `detection model ready in ${(nowSec() - began).toFixed(1)}s ` +
            `(${m.backend})`,
        );
        if ((status.last_error || "").startsWith(NO_DETECTOR))
          publish({ last_error: null });
      } else if (m.type === "progress") {
        // Never claim 100% before the model is actually ready: the weights
        // still have to be built into a graph after the last byte arrives.
        det.progress = m.total ? Math.min(m.loaded / m.total, 0.99) : -1;
        publish();
      } else if (m.type === "error") {
        clearTimeout(timer);
        giveUp(m.message);
      } else if (m.type === "result" || m.type === "failed") {
        const p = det.pending.get(m.id);
        det.pending.delete(m.id);
        if (!p) return;
        if (m.type === "result") p.resolve(m.preds);
        else p.reject(new Error(m.message));
      }
    };
    worker.onerror = (e) => {
      clearTimeout(timer);
      giveUp(e.message || "the detection worker crashed");
    };
    worker.postMessage({ type: "load", tfjs: TFJS_SRC, coco: COCO_SRC });
  }

  function detectRemote(canvas) {
    return new Promise((resolve, reject) => {
      const id = ++det.seq;
      det.pending.set(id, { resolve, reject });
      createImageBitmap(canvas)
        .then((bitmap) =>
          det.worker.postMessage(
            {
              type: "detect",
              id,
              bitmap,
              maxBoxes: MAX_BOXES,
              minScore: CONFIDENCE,
            },
            [bitmap],
          ),
        )
        .catch((e) => {
          det.pending.delete(id);
          reject(e);
        });
    });
  }

  async function listCameras() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    const all = await navigator.mediaDevices.enumerateDevices();
    return all
      .filter((d) => d.kind === "videoinput")
      .map((d, i) => ({ id: d.deviceId, label: d.label || `Camera ${i + 1}` }));
  }

  /* getUserMedia reports refusals as DOMExceptions whose names mean a lot
   * more than their messages do. */
  function explainStartFailure(e) {
    const name = e?.name || "";
    if (name === "NotAllowedError" || name === "SecurityError")
      return (
        "The camera was blocked for this page. Allow it in the browser's " +
        "address-bar camera icon, then press Start again."
      );
    if (name === "NotFoundError" || name === "OverconstrainedError")
      return "No camera to use on this device (or the one picked is gone).";
    if (name === "NotReadableError" || name === "AbortError")
      return (
        "The camera is already in use. Close whatever has it -- including " +
        "an agent.py or monitor.py running on this machine -- and try again."
      );
    return e?.message || String(e);
  }

  function supported() {
    if (!window.isSecureContext)
      return "A browser only gives a page the camera over https (or on localhost).";
    if (!navigator.mediaDevices?.getUserMedia)
      return "This browser won't give a page access to the camera.";
    return null;
  }

  /* Open the camera, fetch a token, load the model, then run. Anything that
   * fails here is reported and leaves nothing running. */
  async function start({ device, cameraId }) {
    if (running) await stop();
    // The buffer spans runs, so each one announces itself: a shared log is
    // usually "it worked this morning and not now", and the boundary
    // between those two attempts is the first thing to look for. Before
    // any refusal, so an attempt that got nowhere still leaves a trace.
    log(
      `=== starting as "${device.name}" (device ${device.id}), ` +
        `camera ${cameraId || "default"} ===`,
    );
    const why = supported();
    if (why) {
      logError(why);
      throw new Error(why);
    }

    dev = { id: device.id, name: device.name };
    generation++;
    status = blankStatus();
    video = document.querySelector("#station-video");
    alarm = makeAlarm();
    alarm.unlock(); // we are inside the click that started us

    try {
      setPhase("Waiting for permission to use the camera\u2026");
      videoWanted = cameraId
        ? { deviceId: { exact: cameraId } }
        : { width: { ideal: 1280 }, height: { ideal: 720 } };
      stream = await openStream();
      video.srcObject = stream;
      await video.play().catch(() => {});
      const track = stream.getVideoTracks()[0];
      const got = track?.getSettings?.() || {};
      log(
        `camera open: ${track?.label || "unnamed"} ` +
          `${got.width || "?"}x${got.height || "?"}` +
          `${got.frameRate ? ` @ ${Math.round(got.frameRate)} fps` : ""}`,
      );
      const mic = stream.getAudioTracks()[0];
      log(
        mic
          ? `microphone open: ${mic.label || "unnamed"}`
          : "no microphone: clips will be silent",
      );

      setPhase("Registering this device as a camera\u2026");
      let r;
      try {
        r = await api(`/api/devices/${device.id}/browser-token`, {
          method: "POST",
        });
      } catch (e) {
        // This page comes from the same place as the API, so a missing
        // endpoint means the files on disk are newer than the process
        // serving them.
        if (e.status === 404)
          throw new Error(
            "This server doesn't know about camera stations yet. Restart it " +
              "so it picks up the new code, then press Start again.",
          );
        throw e;
      }
      token = r.token;
      log("got a device token for this run");
    } catch (e) {
      log(`could not start: ${e.message}`);
      await stop();
      // getUserMedia's own messages say nothing a user can act on, so the
      // common refusals get named here instead.
      throw new Error(explainStartFailure(e));
    }

    raw = document.createElement("canvas");
    shown = document.createElement("canvas");
    overlay = document.createElement("canvas");
    tracker = makeTracker();
    uploader = makeUploader(token);
    recorder = makeRecorder(stream);
    frameGaps = [];
    lastSoundAt = -Infinity;
    openEvent = null;
    running = true;
    phase = null;
    log(`watching as "${dev.name}"`);
    publish({
      running: true,
      started_at: nowSec(),
      camera_ok: true,
      audio_ok: stream.getAudioTracks().length > 0,
      last_error: recorder
        ? null
        : "This browser can't record clips, so only snapshots are saved.",
    });
    recorder?.sync();

    // Best effort: a sleeping screen is a station that sees nothing, and
    // the browser throttles this tab's timers when it is in the background.
    try {
      wakeLock = await navigator.wakeLock?.request("screen");
    } catch (_) {}

    const gen = generation;
    startDetector(gen);
    detectLoop(gen);
    heartbeatLoop(gen);
    liveLoop(gen);
    return snapshot();
  }

  async function stop(reason = null) {
    const wasRunning = running;
    running = false;
    phase = null;
    if (wasRunning) {
      const left = uploader?.pending() || 0;
      log(
        (reason ? `stopped: ${reason}` : "stopped") +
          (left ? ` (${left} upload(s) still draining)` : ""),
      );
    }
    liveWanted = false;
    recorder?.finish();
    alarm?.silence();
    // Queued uploads are left to drain: the page is still open, and the
    // alarm already happened -- the clip catching up a few seconds later is
    // the same deal the agent gives on a flaky connection.
    // Stopping with the dog still on the couch closes the session now, so
    // the event doesn't sit open forever with no duration against it.
    if (wasRunning && openEvent) {
      const end = nowSec();
      uploader.submit({
        kind: "end",
        client_id: openEvent.id,
        body: {
          end_ts: end,
          duration: Math.round((end - openEvent.start) * 10) / 10,
        },
      });
      openEvent = null;
    }
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
    if (video) video.srcObject = null;
    try {
      await wakeLock?.release();
    } catch (_) {}
    wakeLock = null;
    alarm?.close();
    alarm = null;
    recorder = null;
    token = "";
    status = { ...blankStatus(), last_error: reason };
    publish();
    return snapshot();
  }

  /* A station that is closed mid-session leaves the event open; the next
   * heartbeat never comes, so the dashboard shows the camera offline within
   * 20 seconds either way. Stopping cleanly is still worth a try. */
  window.addEventListener("pagehide", () => {
    if (running) {
      running = false;
      stream?.getTracks().forEach((t) => t.stop());
    }
  });

  return {
    start,
    stop,
    listCameras,
    supported,
    isRunning: () => running,
    deviceId: () => (running ? (dev?.id ?? null) : null),
    snapshot,
    /* For reading the buffer from the station's own devtools console. A
     * copy, so nothing outside here can edit it. */
    logLines: () => logBuffer.map((l) => ({ ...l })),
    logCount: () => logBuffer.length,
    onChange(fn) {
      onChange = fn || (() => {});
    },
  };
})();
