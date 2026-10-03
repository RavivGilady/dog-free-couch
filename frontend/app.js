/* Dashboard client: a single page talking to the server's JSON API.
 *
 * Auth is a session cookie; every state-changing request also carries the
 * CSRF token handed out by /api/auth/me. Everything shown is scoped to the
 * signed-in account server-side; the device picker only chooses which of
 * the user's cameras the Live, Sound and Settings tabs act on.
 *
 * The one non-obvious piece is audio: MediaRecorder gives webm/opus, which
 * the agent's winsound/aplay cannot play, so the recording is decoded with
 * WebAudio and re-encoded to 16-bit PCM WAV here before upload.
 */
let CSRF = "";
let devices = [];
let deviceId = null;
let eventsOffset = 0;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* Names, labels and emails are user-entered: never put them in innerHTML raw. */
function esc(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(url, opts = {}) {
  const headers = Object.assign({ "X-CSRF-Token": CSRF }, opts.headers || {});
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
    opts.method = opts.method || "POST";
  }
  const res = await fetch(url, Object.assign({}, opts, { headers }));
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (e) {
    data = { error: text };
  }
  if (res.status === 401 && !url.startsWith("/api/auth/")) {
    showAuth();
  }
  if (!res.ok)
    throw new HttpError(res.status, data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  return `${m}m ${sec % 60}s`;
}

function fmtWhen(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return sameDay ? `Today ${time}` : `${d.toLocaleDateString()} ${time}`;
}

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function localMidnight() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

const currentDevice = () => devices.find((d) => d.id === deviceId) || null;

/* ================= auth ================= */
let authMode = "login";
let allowSignup = true;

function renderAuth() {
  const signup = authMode === "signup";
  $("#auth-title").textContent = signup ? "Create an account" : "Sign in";
  $("#auth-sub").textContent = signup
    ? "Watch your couch from anywhere."
    : "Welcome back.";
  $("#auth-submit").textContent = signup ? "Create account" : "Sign in";
  $("#auth-password").autocomplete = signup
    ? "new-password"
    : "current-password";
  $("#auth-password").minLength = signup ? 8 : 1;
  $("#auth-switch").innerHTML = signup
    ? `Already have an account? <a data-mode="login">Sign in</a>`
    : allowSignup
      ? `New here? <a data-mode="signup">Create an account</a>`
      : "";
  $("#auth-error").classList.add("hidden");
}

$("#auth-switch").addEventListener("click", (e) => {
  if (e.target.dataset.mode) {
    authMode = e.target.dataset.mode;
    renderAuth();
  }
});

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = authMode === "signup" ? "/api/auth/register" : "/api/auth/login";
  try {
    const me = await api(url, {
      json: {
        email: $("#auth-email").value,
        password: $("#auth-password").value,
      },
    });
    $("#auth-password").value = "";
    enterApp(me);
  } catch (err) {
    const box = $("#auth-error");
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
});

function showAuth() {
  stopStream();
  $("#app-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
  renderAuth();
}

async function boot() {
  try {
    enterApp(await api("/api/auth/me"));
  } catch (e) {
    if (e.status === 401) {
      try {
        const r = await fetch("/api/auth/me");
        allowSignup = (await r.json()).allow_signup;
      } catch (_) {}
      showAuth();
    }
  }
}

async function enterApp(me) {
  CSRF = me.csrf_token;
  $("#who").textContent = me.user.email;
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  await loadDevices();
  if (!devices.length) switchTab("devices");
  else switchTab("live");
}

$("#btn-logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) {}
  CSRF = "";
  devices = [];
  deviceId = null;
  authMode = "login";
  showAuth();
});

/* ================= tabs ================= */
let activeTab = "live";
function switchTab(name) {
  activeTab = name;
  // Changing camera re-enters the current tab, so an unsaved polygon drawn
  // against the old camera's view is dropped here rather than re-pointed.
  stopZoneEdit();
  $$(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name),
  );
  $$(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === `tab-${name}`),
  );
  if (name === "live") {
    startStream();
    renderZoneBox();
  } else stopStream();
  if (name === "events") loadEvents(true);
  if (name === "sound") loadSounds();
  if (name === "settings") loadSettings();
  if (name === "devices") renderDevices();
}
$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => switchTab(btn.dataset.tab)),
);

/* ================= devices ================= */
async function loadDevices() {
  const r = await api("/api/devices");
  devices = r.devices;
  let saved = null;
  try {
    saved = Number(localStorage.getItem("dfc.device"));
  } catch (_) {}
  if (!devices.some((d) => d.id === deviceId)) {
    deviceId = devices.some((d) => d.id === saved)
      ? saved
      : (devices[0]?.id ?? null);
  }
  renderDevicePicker();
}

function renderDevicePicker() {
  const pick = $("#device-pick");
  const html = devices
    .map(
      (d) =>
        `<option value="${d.id}" ${d.id === deviceId ? "selected" : ""}>${esc(d.name)}${d.online ? "" : " (offline)"}</option>`,
    )
    .join("");
  // Polled every 2s: only touch the DOM on change, or an open dropdown
  // would snap shut under the user's finger.
  if (pick.dataset.html !== html) {
    pick.innerHTML = html;
    pick.dataset.html = html;
  }
  pick.classList.toggle("hidden", devices.length === 0);
}

$("#device-pick").addEventListener("change", (e) => {
  deviceId = Number(e.target.value);
  try {
    localStorage.setItem("dfc.device", String(deviceId));
  } catch (_) {}
  switchTab(activeTab);
});

function renderDevices() {
  const list = $("#devices-list");
  if (!devices.length) {
    list.innerHTML = `<p class="muted">No cameras yet. Add one below, then start the agent on the computer it's plugged into.</p>`;
    return;
  }
  list.innerHTML = devices
    .map(
      (d) => `
    <div class="device" data-id="${d.id}">
      <span class="dot ${d.online ? "live" : ""}"></span>
      <div>
        <div class="name">${esc(d.name)}</div>
        <div class="sub">${d.online ? "Online" : "Offline"} &middot; last seen ${fmtAgo(d.last_seen)}</div>
      </div>
      <div class="spacer"></div>
      <button class="small secondary btn-rename">Rename</button>
      <button class="small secondary btn-rotate">New token</button>
      <button class="small ghost btn-deldev">Delete</button>
    </div>`,
    )
    .join("");
}

$("#devices-list").addEventListener("click", async (e) => {
  const row = e.target.closest(".device");
  if (!row) return;
  const id = Number(row.dataset.id);
  const dev = devices.find((d) => d.id === id);
  try {
    if (e.target.classList.contains("btn-rename")) {
      const name = prompt("Camera name", dev.name);
      if (!name) return;
      await api(`/api/devices/${id}`, { method: "PATCH", json: { name } });
    } else if (e.target.classList.contains("btn-rotate")) {
      if (
        !confirm(
          "Issue a new token? The agent using the old one will stop working until you restart it with the new token.",
        )
      )
        return;
      const r = await api(`/api/devices/${id}/token`, { method: "POST" });
      showToken(r.token, r.agent_cmd, r.agent_cmd_local);
    } else if (e.target.classList.contains("btn-deldev")) {
      if (!confirm(`Delete "${dev.name}" and all its events and clips?`))
        return;
      await api(`/api/devices/${id}`, { method: "DELETE" });
    } else return;
    await loadDevices();
    renderDevices();
  } catch (err) {
    toast(`Failed: ${err.message}`);
  }
});

$("#btn-add-device").addEventListener("click", async () => {
  try {
    const r = await api("/api/devices", {
      json: { name: $("#new-device-name").value },
    });
    $("#new-device-name").value = "";
    deviceId = r.device.id;
    await loadDevices();
    renderDevices();
    showToken(r.token, r.agent_cmd, r.agent_cmd_local);
  } catch (err) {
    toast(`Failed: ${err.message}`);
  }
});

function showToken(token, agentCmd, agentCmdLocal) {
  // The server sends a command naming its own interpreter and an absolute
  // agent.py when it can see both, so this pastes into any terminal as-is.
  // Otherwise it can't know where the agent lives or which Python can run
  // it (that machine is often not this one), and the folder matters again.
  const cmd = agentCmd || "python agent.py";
  $("#token-hint").textContent = agentCmdLocal
    ? "On the camera computer, run:"
    : "On the camera computer, in this project's folder, with its virtualenv active, run:";
  $("#token-cmd").textContent =
    `${cmd} --server ${location.origin} --token ${token}`;
  $("#token-box").classList.remove("hidden");
}

$("#btn-copy-token").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#token-cmd").textContent);
    toast("Copied");
  } catch (_) {
    toast("Copy failed — select the text and copy it manually");
  }
});

/* ================= live ================= */
/* The feed is one long multipart/x-mixed-replace response rendered by an
 * <img>, and that is fragile in a way worth being explicit about. Hiding the
 * Live panel (the panels are display:none) or backgrounding the page lets the
 * browser drop the connection, and a dropped multipart stream arrives as a
 * *completed* load -- no "error" event -- so the <img> silently freezes on its
 * last frame with nothing left to recover from. So the connection is owned
 * here: torn down whenever the feed is not on screen, rebuilt when it is, and
 * watchdogged while it is live. */
const STREAM_STALL_MS = 6000;
let streamLive = false; // a connection is wanted and believed open
let streamFrames = 0; // frames seen on the current connection
let lastFrameAt = 0;
let lastFrameHeight = 0;

function startStream(force = false) {
  const img = $("#stream");
  const empty = $("#stream-empty");
  if (!deviceId) {
    streamLive = false;
    img.classList.add("hidden");
    img.removeAttribute("src");
    empty.textContent = "Add a camera in the Devices tab to get started.";
    empty.classList.remove("hidden");
    return;
  }
  const src = `/api/devices/${deviceId}/stream.mjpg`;
  // The cache buster matters: without it a reconnect can be served from the
  // dead connection's cache entry and never produce a new frame.
  if (force || !img.src.includes(src)) {
    streamFrames = 0;
    img.src = `${src}?t=${Date.now()}`;
  }
  lastFrameAt = Date.now();
  streamLive = true;
  img.classList.remove("hidden");
}

function stopStream() {
  // Dropping the src closes the connection, which tells the server nobody
  // is watching, so the agent stops uploading frames.
  const img = $("#stream");
  streamLive = false;
  // Hold the box open at the size the last frame gave it, so coming back to
  // the Live tab does not land on a collapsed player for a frame.
  if (lastFrameHeight) img.style.minHeight = lastFrameHeight + "px";
  img.removeAttribute("src");
}

$("#stream").addEventListener("load", () => {
  // Every part of the multipart response fires its own load event.
  streamFrames++;
  lastFrameAt = Date.now();
  if (streamFrames === 1) $("#stream").style.minHeight = "";
  lastFrameHeight = $("#stream").clientHeight || lastFrameHeight;
});

/* Reconnect the stream if it stalls (sleep, wifi drop, server restart). */
$("#stream").addEventListener("error", () => {
  if (activeTab !== "live" || !deviceId || document.hidden) return;
  setTimeout(() => {
    if (activeTab === "live" && !document.hidden) startStream(true);
  }, 2000);
});

/* A silently closed stream leaves only a frozen picture, so judge it by
 * whether frames are still arriving -- but only once this browser has proven
 * it fires a load event per frame (Safari does not, and there the tab and
 * visibility hooks plus "error" carry it), and only while the camera says it
 * has frames to send, so an offline, stopped or camera-less agent is not
 * mistaken for a dropped connection. */
setInterval(() => {
  if (!streamLive || activeTab !== "live" || document.hidden) return;
  if (streamFrames < 2 || !deviceId) return;
  const d = currentDevice();
  const s = d?.status || {};
  if (!d?.online || !s.running || !s.camera_ok) return;
  if (Date.now() - lastFrameAt > STREAM_STALL_MS) startStream(true);
}, 2000);

/* Switching away from the browser (or locking the phone) is the common way
 * to lose the feed, and nothing tells us about it afterwards. */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopStream();
  } else if (
    activeTab === "live" &&
    !$("#app-view").classList.contains("hidden")
  ) {
    startStream(true);
  }
});

let wasOnline = null;
async function pollStatus() {
  if ($("#app-view").classList.contains("hidden")) return;
  try {
    await loadDevices();
    const d = currentDevice();
    const s = d?.status || {};
    const online = !!d?.online;
    const q = deviceId ? `device_id=${deviceId}&` : "";
    const stats = await api(`/api/stats?${q}since=${localMidnight()}`);

    $("#live-dot").className =
      "dot" +
      (online && s.dog_on_couch
        ? " alarm"
        : online && s.running
          ? " live"
          : "");
    $("#s-state").textContent = !d
      ? "—"
      : !online
        ? "Offline"
        : !s.running
          ? "Stopped"
          : s.dog_on_couch
            ? "ON COUCH"
            : s.camera_ok
              ? "Watching"
              : "No camera";
    $("#s-fps").textContent = online && s.fps ? s.fps.toFixed(1) : "—";
    $("#s-dogs").textContent = online ? (s.dogs_in_frame ?? 0) : "—";
    $("#s-people").textContent = online ? (s.persons_in_frame ?? 0) : "—";
    $("#s-today").textContent = stats.today_alerts;
    $("#s-total").textContent = stats.total_alerts;
    $("#s-longest").textContent = fmtDuration(stats.longest_session_sec);
    $("#stream-badge").classList.toggle("hidden", !(online && s.dog_on_couch));

    if (d && activeTab === "live") {
      // The server ends a stream after ~20s without frames, so open a fresh
      // one when the camera comes back -- unless the page is hidden, where
      // the feed is deliberately torn down until the user returns.
      if (online && wasOnline === false && !document.hidden) startStream(true);
      const empty = $("#stream-empty");
      empty.textContent = `${d.name} is offline. Start the agent on its computer.`;
      empty.classList.toggle("hidden", online);
      $("#stream").classList.toggle("hidden", !online);
    }

    wasOnline = d ? online : null;

    const err = $("#s-error");
    if (online && s.last_error) {
      err.textContent = s.last_error;
      err.classList.remove("hidden");
    } else err.classList.add("hidden");

    if (activeTab === "live") renderZoneBox();
  } catch (_) {}
}
setInterval(pollStatus, 2000);

$("#btn-test-sound").addEventListener("click", async () => {
  if (!deviceId) return;
  try {
    const r = await api(`/api/devices/${deviceId}/commands`, {
      json: { type: "test_sound" },
    });
    toast(r.online ? "Alarm sent to the camera" : "Queued — camera is offline");
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
});

/* ================= couch zone ================= */
/* Corners are kept as fractions of the frame (0..1), never pixels: this page
 * only ever sees a CSS-scaled JPEG, and the camera's resolution can change
 * under it. The agent scales them back on its next frame.
 *
 * While editing, the orange outline burned into the video is still the zone
 * the camera is using; the dashed green one is what's being drawn. They swap
 * a couple of seconds after saving, on the agent's next heartbeat. */
const MAX_ZONE_PTS = 24;
let zoneEditing = false;
let zonePts = [];
let zoneDrag = null;

const zoneSvg = $("#zone-svg");

function savedZone() {
  const d = currentDevice();
  const z = d?.settings?.zone || {};
  // Nothing stored on the server yet: start from whatever the camera says it
  // is using, so a zone from calibrate.py can be nudged instead of redrawn.
  const points = z.points?.length ? z.points : d?.status?.zone_points || [];
  return { points, threshold: z.overlap_threshold ?? 0.35 };
}

function zoneAt(e) {
  const r = zoneSvg.getBoundingClientRect();
  const clamp = (v) => Math.min(1, Math.max(0, v));
  return [
    clamp((e.clientX - r.left) / r.width),
    clamp((e.clientY - r.top) / r.height),
  ];
}

function drawZone() {
  const r = zoneSvg.getBoundingClientRect();
  const pts = zonePts.map(([x, y]) => [x * r.width, y * r.height]);
  const coords = pts
    .map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  const shape =
    zonePts.length >= 3
      ? `<polygon class="zone-fill" points="${coords}"/>`
      : `<polyline class="zone-fill" points="${coords}" fill="none"/>`;
  zoneSvg.innerHTML =
    shape +
    pts
      .map(
        ([x, y], i) =>
          `<circle class="zone-handle" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="8" data-i="${i}"/>` +
          `<text class="zone-num" x="${(x + 11).toFixed(1)}" y="${(y - 9).toFixed(1)}">${i + 1}</text>`,
      )
      .join("");
}

function renderZoneBox() {
  const d = currentDevice();
  const btn = $("#btn-zone-edit");
  const hint = $("#zone-hint");

  if (zoneEditing) {
    hint.textContent =
      zonePts.length < 3
        ? "Click the couch's corners in the video, going around it."
        : `${zonePts.length} corners. Drag one to move it, double-click one to remove it.`;
    return;
  }

  const { points, threshold } = savedZone();
  btn.disabled = !d || !d.online;
  btn.textContent = points.length ? "Edit couch zone" : "Draw couch zone";
  hint.textContent = !d
    ? "Add a camera first."
    : !d.online
      ? "The camera is offline, so there is no video to draw on."
      : points.length
        ? `${points.length} corners, alerting at ${Math.round(threshold * 100)}% overlap.`
        : "No zone yet, so nothing counts as being on the couch. Draw one on the video.";
}

function startZoneEdit() {
  const { points, threshold } = savedZone();
  zonePts = points.map((pt) => [Number(pt[0]), Number(pt[1])]);
  zoneEditing = true;
  const pct = Math.round(threshold * 100);
  $("#zone-th").value = pct;
  $("#zone-th-val").textContent = pct;
  $("#zone-controls").classList.remove("hidden");
  $("#btn-zone-edit").classList.add("hidden");
  zoneSvg.classList.remove("hidden");
  drawZone();
  renderZoneBox();
}

function stopZoneEdit() {
  if (!zoneEditing) return;
  zoneEditing = false;
  zoneDrag = null;
  zonePts = [];
  $("#zone-controls").classList.add("hidden");
  $("#btn-zone-edit").classList.remove("hidden");
  zoneSvg.classList.add("hidden");
  zoneSvg.innerHTML = "";
  renderZoneBox();
}

zoneSvg.addEventListener("pointerdown", (e) => {
  if (!zoneEditing) return;
  const handle = e.target.closest(".zone-handle");
  if (handle) {
    zoneDrag = Number(handle.dataset.i);
    zoneSvg.setPointerCapture(e.pointerId);
    return;
  }
  if (zonePts.length >= MAX_ZONE_PTS) {
    toast(`A zone can have at most ${MAX_ZONE_PTS} corners`);
    return;
  }
  zonePts.push(zoneAt(e));
  drawZone();
  renderZoneBox();
});

zoneSvg.addEventListener("pointermove", (e) => {
  if (zoneDrag === null) return;
  zonePts[zoneDrag] = zoneAt(e);
  drawZone();
});

["pointerup", "pointercancel"].forEach((ev) =>
  zoneSvg.addEventListener(ev, () => {
    zoneDrag = null;
  }),
);

zoneSvg.addEventListener("dblclick", (e) => {
  const handle = e.target.closest(".zone-handle");
  if (!zoneEditing || !handle) return;
  zonePts.splice(Number(handle.dataset.i), 1);
  drawZone();
  renderZoneBox();
});

/* The overlay is sized in CSS pixels, so it has to be redrawn whenever the
   video box changes size (window resize, phone rotation). */
new ResizeObserver(() => {
  if (zoneEditing) drawZone();
}).observe($(".video-wrap"));

$("#btn-zone-edit").addEventListener("click", startZoneEdit);
$("#btn-zone-cancel").addEventListener("click", stopZoneEdit);

$("#btn-zone-undo").addEventListener("click", () => {
  zonePts.pop();
  drawZone();
  renderZoneBox();
});

$("#btn-zone-clear").addEventListener("click", () => {
  zonePts = [];
  drawZone();
  renderZoneBox();
});

$("#zone-th").addEventListener("input", (e) => {
  $("#zone-th-val").textContent = e.target.value;
});

$("#btn-zone-save").addEventListener("click", async () => {
  if (zonePts.length < 3) {
    toast("A zone needs at least 3 corners");
    return;
  }
  try {
    await api(`/api/devices/${deviceId}/settings`, {
      json: {
        zone: {
          points: zonePts,
          overlap_threshold: Number($("#zone-th").value) / 100,
        },
      },
    });
    await loadDevices();
    stopZoneEdit();
    toast("Zone saved — the camera picks it up within a couple of seconds");
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
});

/* ================= events ================= */
const PAGE = 30;

async function loadEvents(reset) {
  const list = $("#events-list");
  if (reset) eventsOffset = 0;
  try {
    const all = $("#all-devices").checked || !deviceId;
    const q = all ? "" : `device_id=${deviceId}&`;
    const { events } = await api(
      `/api/events?${q}limit=${PAGE}&offset=${eventsOffset}`,
    );
    const html = events.map((ev) => renderEvent(ev, all)).join("");
    if (reset) {
      list.innerHTML =
        html ||
        `<p class="muted">No events yet. When the dog gets on the couch, it shows up here.</p>`;
    } else list.insertAdjacentHTML("beforeend", html);
    eventsOffset += events.length;
    $("#btn-more").classList.toggle("hidden", events.length < PAGE);
  } catch (e) {
    list.innerHTML = `<p class="alert-error">Could not load events: ${esc(e.message)}</p>`;
  }
}

function renderEvent(ev, showDevice) {
  const thumb = ev.has_snapshot
    ? `<img class="thumb" src="/api/events/${ev.id}/snapshot" alt="Snapshot" loading="lazy">`
    : `<div class="thumb empty">no image</div>`;
  const pills = [
    showDevice ? `<span class="pill">${esc(ev.device_name)}</span>` : "",
    ev.has_video ? `<span class="pill video">video</span>` : "",
    ev.end_ts ? "" : `<span class="pill">ongoing</span>`,
    ev.confidence
      ? `<span class="pill">conf ${Number(ev.confidence).toFixed(2)}</span>`
      : "",
    ev.notified ? `<span class="pill">sent</span>` : "",
  ].join("");

  return `
  <div class="event" data-id="${ev.id}">
    ${thumb}
    <div>
      <div class="when">${fmtWhen(ev.start_ts)}</div>
      <div class="meta">${ev.duration ? "Stayed " + fmtDuration(ev.duration) : "&nbsp;"}</div>
      <div style="margin-top:6px">${pills}</div>
    </div>
    <div>
      ${ev.has_video ? `<button class="small secondary btn-play">Play</button>` : ""}
      <button class="small ghost btn-del">Delete</button>
    </div>
  </div>`;
}

$("#events-list").addEventListener("click", async (e) => {
  const row = e.target.closest(".event");
  if (!row) return;
  const id = row.dataset.id;
  const b = e.target;

  if (b.classList.contains("btn-play")) {
    const existing = row.querySelector(".event-video-row");
    if (existing) {
      existing.remove();
      b.textContent = "Play";
      return;
    }
    const div = document.createElement("div");
    div.className = "event-video-row";
    div.innerHTML = `<video controls autoplay playsinline preload="metadata" src="/api/events/${id}/video"></video>`;
    row.appendChild(div);
    b.textContent = "Hide";
  } else if (b.classList.contains("btn-del")) {
    if (!confirm("Delete this event, its snapshot and its video?")) return;
    try {
      await api(`/api/events/${id}`, { method: "DELETE" });
      row.remove();
      eventsOffset = Math.max(0, eventsOffset - 1);
      toast("Deleted");
    } catch (err) {
      toast(`Failed: ${err.message}`);
    }
  }
});

$("#all-devices").addEventListener("change", () => loadEvents(true));
$("#btn-more").addEventListener("click", () => loadEvents(false));

/* ================= sound library ================= */
async function loadSounds() {
  const list = $("#sounds-list");
  try {
    const [{ sounds: builtins }, { sounds }] = await Promise.all([
      api("/api/sounds/builtins"),
      api("/api/sounds"),
    ]);
    const active = currentDevice()?.settings.alert.active_sound ?? "builtin";
    const items = [...builtins, ...sounds];
    list.innerHTML = items
      .map((s) => {
        const isActive = String(active) === String(s.id);
        const when = s.created_at
          ? " &middot; " + new Date(s.created_at * 1000).toLocaleString()
          : "";
        const audio = s.builtin
          ? `/api/sounds/builtin/${encodeURIComponent(s.id)}/audio`
          : `/api/sounds/${s.id}/audio`;
        return `
      <div class="sound ${isActive ? "active" : ""}" data-id="${s.id}">
        <div>
          <div class="name">${esc(s.label)}${isActive ? " &middot; active" : ""}</div>
          <div class="sub">${
            s.builtin ? esc(s.blurb) : (s.duration_sec ?? "?") + "s"
          }${when}</div>
        </div>
        <div class="spacer"></div>
        <audio controls preload="none" src="${audio}" style="height:34px"></audio>
        ${isActive || !deviceId ? "" : `<button class="small secondary btn-activate">Use this</button>`}
        ${s.builtin ? "" : `<button class="small ghost btn-delsound">Delete</button>`}
      </div>`;
      })
      .join("");
  } catch (e) {
    list.innerHTML = `<p class="alert-error">${esc(e.message)}</p>`;
  }
}

async function setActiveSound(id) {
  await api(`/api/devices/${deviceId}/settings`, {
    json: { alert: { active_sound: id } },
  });
  await loadDevices();
}

$("#sounds-list").addEventListener("click", async (e) => {
  const row = e.target.closest(".sound");
  if (!row) return;
  const id = row.dataset.id;
  try {
    if (e.target.classList.contains("btn-activate")) {
      // Siren ids are names, recordings are numeric.
      await setActiveSound(/^[0-9]+$/.test(id) ? Number(id) : id);
      toast(`Alert sound updated for ${currentDevice()?.name}`);
    } else if (e.target.classList.contains("btn-delsound")) {
      if (!confirm("Delete this sound?")) return;
      await api(`/api/sounds/${id}`, { method: "DELETE" });
      await loadDevices();
    } else return;
    loadSounds();
  } catch (err) {
    toast(`Failed: ${err.message}`);
  }
});

/* ================= mic recording ================= */
let mediaRecorder = null,
  chunks = [],
  recStart = 0,
  recTimer = null,
  recordedWav = null;

function encodeWav(audioBuffer) {
  // Downmix to mono: the alarm is played through whatever speaker is on the
  // camera computer, and mono halves the file size for no audible loss here.
  const n = audioBuffer.length;
  const channels = audioBuffer.numberOfChannels;
  const data = new Float32Array(n);
  for (let c = 0; c < channels; c++) {
    const ch = audioBuffer.getChannelData(c);
    for (let i = 0; i < n; i++) data[i] += ch[i] / channels;
  }

  const rate = audioBuffer.sampleRate;
  const buffer = new ArrayBuffer(44 + n * 2);
  const view = new DataView(buffer);
  const writeStr = (off, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };

  writeStr(0, "RIFF");
  view.setUint32(4, 36 + n * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, n * 2, true);

  let peak = 0;
  for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(data[i]));
  // Normalise to just under full scale -- a phone mic recording is usually
  // far too quiet to work as an alarm otherwise.
  const gain = peak > 0.01 ? 0.95 / peak : 1;

  for (let i = 0; i < n; i++) {
    const v = Math.max(-1, Math.min(1, data[i] * gain));
    view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([view], { type: "audio/wav" });
}

$("#btn-rec").addEventListener("click", async () => {
  const btn = $("#btn-rec");

  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      clearInterval(recTimer);
      stream.getTracks().forEach((t) => t.stop());
      btn.textContent = "Record";
      btn.classList.remove("recording");
      $("#rec-hint").textContent = "Converting…";

      const blob = new Blob(chunks, { type: chunks[0]?.type || "audio/webm" });
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
      recordedWav = encodeWav(decoded);
      await ctx.close();

      $("#rec-audio").src = URL.createObjectURL(recordedWav);
      $("#rec-preview").classList.remove("hidden");
      $("#rec-hint").textContent =
        `${(recordedWav.size / 1024).toFixed(0)} KB, normalised`;
    };

    mediaRecorder.start();
    recStart = Date.now();
    btn.textContent = "Stop";
    btn.classList.add("recording");
    $("#rec-hint").textContent = "Recording…";
    $("#rec-preview").classList.add("hidden");
    recTimer = setInterval(() => {
      const s = (Date.now() - recStart) / 1000;
      $("#rec-time").textContent = s.toFixed(1) + "s";
      if (s > 30) mediaRecorder.stop(); // hard cap
    }, 100);
  } catch (e) {
    $("#rec-hint").textContent =
      "Mic blocked. Browsers only allow mic access on localhost or HTTPS.";
  }
});

$("#btn-discard").addEventListener("click", () => {
  recordedWav = null;
  $("#rec-preview").classList.add("hidden");
  $("#rec-time").textContent = "0.0s";
});

$("#btn-save-rec").addEventListener("click", async () => {
  if (!recordedWav) return;
  const fd = new FormData();
  fd.append("audio", recordedWav, "recording.wav");
  fd.append("label", $("#rec-label").value || "Recording");
  try {
    const { sound } = await api("/api/sounds", { method: "POST", body: fd });
    if (deviceId) await setActiveSound(sound.id);
    toast(deviceId ? "Saved and set as the alert sound" : "Saved");
    recordedWav = null;
    $("#rec-preview").classList.add("hidden");
    $("#rec-label").value = "";
    loadSounds();
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
});

/* ================= settings ================= */
async function loadSettings() {
  const d = currentDevice();
  const label = d ? `for ${d.name}` : "(add a camera first)";
  $("#al-device").textContent = label;
  $("#v-device").textContent = label;
  $("#btn-save-alert").disabled = $("#btn-save-video").disabled = !d;
  if (d) {
    const s = d.settings;
    $("#al-play").checked = s.alert.play_sound;
    $("#al-repeat").value = s.alert.repeat_sound_sec;
    $("#al-repeat-val").textContent = s.alert.repeat_sound_sec;
    $("#v-enabled").checked = s.video.enabled;
    $("#v-pre").value = s.video.pre_roll_sec;
    $("#v-pre-val").textContent = s.video.pre_roll_sec;
    $("#v-post").value = s.video.post_roll_sec;
    $("#v-post-val").textContent = s.video.post_roll_sec;
    $("#v-max").value = s.video.max_clip_sec;
    $("#v-max-val").textContent = s.video.max_clip_sec;
  }

  const tg = await api("/api/me/telegram");
  $("#tg-enabled").checked = tg.enabled;
  $("#tg-chat").value = tg.chat_id || "";
  $("#tg-photo").checked = tg.send_photo;
  $("#tg-video").checked = tg.send_video;
  $("#tg-token-hint").textContent = tg.bot_token_set
    ? `currently ${tg.bot_token_hint}`
    : "not set";
}

[
  ["al-repeat", "al-repeat-val"],
  ["v-pre", "v-pre-val"],
  ["v-post", "v-post-val"],
  ["v-max", "v-max-val"],
].forEach(([input, out]) => {
  $(`#${input}`).addEventListener("input", (e) => {
    $(`#${out}`).textContent = e.target.value;
  });
});

async function saveDeviceSettings(body, msg) {
  try {
    await api(`/api/devices/${deviceId}/settings`, { json: body });
    await loadDevices();
    toast(msg);
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
}

$("#btn-save-alert").addEventListener("click", () =>
  saveDeviceSettings(
    {
      alert: {
        play_sound: $("#al-play").checked,
        repeat_sound_sec: Number($("#al-repeat").value),
      },
    },
    "Alarm settings saved",
  ),
);

$("#btn-save-video").addEventListener("click", () =>
  saveDeviceSettings(
    {
      video: {
        enabled: $("#v-enabled").checked,
        pre_roll_sec: Number($("#v-pre").value),
        post_roll_sec: Number($("#v-post").value),
        max_clip_sec: Number($("#v-max").value),
      },
    },
    "Video settings saved",
  ),
);

$("#btn-save-tg").addEventListener("click", async () => {
  try {
    await api("/api/me/telegram", {
      json: {
        enabled: $("#tg-enabled").checked,
        bot_token: $("#tg-token").value,
        chat_id: $("#tg-chat").value,
        send_photo: $("#tg-photo").checked,
        send_video: $("#tg-video").checked,
      },
    });
    $("#tg-token").value = "";
    toast("Telegram settings saved");
    loadSettings();
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
});

$("#btn-test-tg").addEventListener("click", async () => {
  const box = $("#tg-result");
  box.textContent = "Testing…";
  box.className = "result";
  box.classList.remove("hidden");
  try {
    const r = await api("/api/me/telegram/test", { method: "POST" });
    box.textContent = r.message;
    box.className = "result " + (r.ok ? "ok" : "bad");
  } catch (e) {
    box.textContent = e.message;
    box.className = "result bad";
  }
});

$("#btn-save-pw").addEventListener("click", async () => {
  const box = $("#pw-result");
  box.classList.remove("hidden");
  try {
    await api("/api/me/password", {
      json: { current: $("#pw-current").value, new: $("#pw-new").value },
    });
    box.textContent = "Password changed.";
    box.className = "result ok";
    $("#pw-current").value = $("#pw-new").value = "";
  } catch (e) {
    box.textContent = e.message;
    box.className = "result bad";
  }
});

boot();
