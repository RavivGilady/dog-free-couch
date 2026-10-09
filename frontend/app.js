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
 *
 * station.js is the other half of the Devices tab: a camera that is this
 * browser rather than an agent somewhere. Everything else here treats such
 * a camera like any other, because the server does too.
 */
let CSRF = "";
let devices = [];
let deviceId = null;
let eventsOffset = 0;
/* Whether this server is a dev run (Config.DEV_MODE), which is the only
 * thing that unhides the developer-only controls -- today "Share logs" on
 * the camera station. The server decides; the page only obeys, and the
 * endpoint behind the button is gated by the same flag. */
let dev = false;
/* Where "Contact support" goes; empty hides the links. From /api/auth/me. */
let supportEmail = "";
/* The camera this tab has been running, remembered past Station.stop() so
 * the logs of a run that ended (or never started) still have a camera to
 * be filed under. */
let stationDeviceId = null;

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
      if (allowSignup && location.hash === "#signup") authMode = "signup";
      showAuth();
    }
  }
}

async function enterApp(me) {
  CSRF = me.csrf_token;
  dev = !!me.dev;
  supportEmail = me.support_email || "";
  $("#who").textContent = me.user.email;
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  renderFooter();
  await loadDevices();
  loadOnboardingTelegram();
  if (!devices.length) switchTab("devices");
  else switchTab("live");
}

$("#btn-logout").addEventListener("click", async () => {
  // Signing out with the camera still running would leave a station whose
  // events nobody in this page can see.
  if (Station.isRunning()) await Station.stop();
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) {}
  CSRF = "";
  devices = [];
  deviceId = null;
  authMode = "login";
  $("#app-foot").classList.add("hidden");
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
  if (name === "devices") {
    renderDevices();
    renderStationCard();
  }
  renderOnboarding();
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
    list.innerHTML = `<p class="muted">No cameras yet. Use this device as one below, or add a camera on the computer yours is plugged into.</p>`;
    return;
  }
  list.innerHTML = devices
    .map((d) => {
      const browser = d.kind === "browser";
      const here = browser && Station.deviceId() === d.id;
      // A browser camera has no token to copy and no agent to restart: it
      // either runs in a tab or it doesn't, so that is what the row offers.
      const actions = browser
        ? here
          ? `<button class="small secondary btn-stationstop">Stop camera</button>`
          : `<button class="small secondary btn-stationrun">Start here</button>`
        : `<button class="small secondary btn-rotate">New token</button>`;
      return `
    <div class="device" data-id="${d.id}">
      <span class="dot ${d.online ? "live" : ""}"></span>
      <div>
        <div class="name">${esc(d.name)} <span class="kind">${browser ? "browser" : "agent"}</span></div>
        <div class="sub">${here ? "Running in this tab" : d.online ? "Online" : "Offline"} &middot; last seen ${fmtAgo(d.last_seen)}</div>
      </div>
      <div class="spacer"></div>
      <button class="small secondary btn-rename">Rename</button>
      ${actions}
      <button class="small ghost btn-deldev">Delete</button>
    </div>`;
    })
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
    } else if (e.target.classList.contains("btn-stationrun")) {
      await startStation(dev);
    } else if (e.target.classList.contains("btn-stationstop")) {
      await Station.stop();
      toast("Camera stopped");
    } else if (e.target.classList.contains("btn-deldev")) {
      if (!confirm(`Delete "${dev.name}" and all its events and clips?`))
        return;
      if (Station.deviceId() === id) await Station.stop();
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
  $("#token-install").textContent =
    `curl -fsSL ${location.origin}/install.sh | bash -s -- --server ${location.origin} --token ${token}`;
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

$("#btn-copy-install").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#token-install").textContent);
    toast("Copied");
  } catch (_) {
    toast("Copy failed — select the text and copy it manually");
  }
});

/* ================= this browser as a camera ================= */
/* The station itself lives in station.js; this is only its half of the UI.
 * Once it is running it is a camera like any other, so the live view, the
 * couch zone, the settings and the events tabs need nothing new. */

async function renderStationCard() {
  const why =
    Station.supported() ||
    (Station.isRunning()
      ? "This tab is already a camera. Stop it first to start another one."
      : null);
  // Two boxes, two owners: this one says why starting isn't possible right
  // now, #station-error says why the last attempt failed. Sharing one box
  // means whichever renders last wins, and this one always renders last.
  const blocked = $("#station-blocked");
  $("#btn-station-start").disabled = !!why;
  blocked.textContent = why || "";
  blocked.classList.toggle("hidden", !why);

  // Labels only arrive once the camera has been granted once, so before
  // that this is a list of anonymous cameras -- still worth showing on a
  // phone, where the choice is front or back.
  const sel = $("#station-cam");
  const cams = await Station.listCameras();
  const html = ['<option value="">Default camera</option>']
    .concat(
      cams.map((c) => `<option value="${esc(c.id)}">${esc(c.label)}</option>`),
    )
    .join("");
  if (sel.dataset.html !== html) {
    sel.innerHTML = html;
    sel.dataset.html = html;
  }
  renderStationLogs();
}

/* The "Share logs" control: present only on a dev run, and only once this
 * tab has something to share. The count is the honest answer to "did it
 * log anything?" before the button is pressed. */
function renderStationLogs() {
  const box = $("#station-logs-box");
  const lines = dev ? Station.logCount() : 0;
  box.classList.toggle("hidden", !dev);
  $("#btn-station-logs").disabled = !lines;
  $("#station-logs-hint").textContent = lines
    ? `${lines} line${lines === 1 ? "" : "s"} from this tab`
    : "Nothing logged yet — start the camera here first.";
}

async function startStation(device) {
  const err = $("#station-error");
  err.textContent = "";
  err.classList.add("hidden");
  try {
    stationDeviceId = device.id;
    await Station.start({ device, cameraId: $("#station-cam").value || null });
    deviceId = device.id;
    try {
      localStorage.setItem("dfc.device", String(deviceId));
    } catch (_) {}
    await loadDevices();
    toast(`${device.name} is watching from this tab`);
    // A new station has no couch zone, and without one nothing counts as
    // being on the couch -- so send the user straight to where it is drawn.
    if (!device.settings?.zone?.points?.length) switchTab("live");
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove("hidden");
    toast("Could not start the camera");
    throw e;
  } finally {
    if (activeTab === "devices") renderStationCard();
  }
}

$("#btn-station-start").addEventListener("click", async () => {
  const btn = $("#btn-station-start");
  btn.disabled = true;
  btn.textContent = "Starting…";
  let created = null;
  try {
    const r = await api("/api/devices", {
      json: { name: $("#station-name").value, kind: "browser" },
    });
    created = r.device;
    await startStation(created);
    $("#station-name").value = "";
  } catch (e) {
    // Don't leave behind a camera that never managed to start: a declined
    // camera permission would otherwise litter the list.
    if (created && !Station.isRunning()) {
      try {
        await api(`/api/devices/${created.id}`, { method: "DELETE" });
      } catch (_) {}
      stationDeviceId = null;
      await loadDevices();
    }
    $("#station-error").textContent = e.message;
    $("#station-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Start camera here";
    $("#station-status").textContent = "";
    if (activeTab === "devices") {
      renderDevices();
      // Last word on the button: it stays disabled while this tab is a
      // camera, and comes back once that camera is stopped.
      renderStationCard();
    }
  }
});

$("#btn-station-stop").addEventListener("click", async () => {
  await Station.stop();
  toast("Camera stopped");
  await loadDevices();
  if (activeTab === "devices") {
    renderDevices();
    renderStationCard();
  }
});

/* Send this tab's station log to the server, where it lands in
 * data/station-logs/ as one JSON file for whoever is debugging to read.
 *
 * Which camera it is filed under: the one this tab has been running, if it
 * still exists -- a start that failed takes its device with it, and the
 * selected camera is then the least surprising place for the report, since
 * the log lines name the device themselves either way. */
function logsDeviceId() {
  const known = (id) => devices.some((d) => d.id === id);
  if (known(stationDeviceId)) return stationDeviceId;
  if (known(deviceId)) return deviceId;
  return devices[0]?.id ?? null;
}

$("#btn-station-logs").addEventListener("click", async () => {
  const btn = $("#btn-station-logs");
  const lines = Station.logLines();
  if (!lines.length) return toast("Nothing logged in this tab yet");
  const id = logsDeviceId();
  if (!id) return toast("Add a camera first — logs are filed under one");

  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    const r = await api(`/api/devices/${id}/logs`, {
      json: { lines, user_agent: navigator.userAgent },
    });
    toast(`Sent ${r.lines} lines — data/station-logs/${r.file}`);
  } catch (e) {
    // The flag lives on the server, so a page left open across a restart
    // can still be showing a button the endpoint no longer answers.
    toast(
      e.status === 404
        ? "This server isn't running in dev mode — reload the page"
        : `Could not share the logs: ${e.message}`,
    );
  } finally {
    btn.textContent = "Share logs";
    renderStationLogs();
  }
});

/* Called on every status change, a few times a second while the station
 * runs: text and classes only, nothing that reflows the page. */
Station.onChange((st) => {
  // Visible while starting too: opening the camera and downloading the
  // model take seconds, and the preview appearing is the first sign the
  // button did anything at all.
  $("#station-bar").classList.toggle("hidden", !st.running && !st.starting);
  $("#station-status").textContent = st.phase || "";
  $("#station-title").textContent = st.device?.name || "This device";
  if (st.starting) {
    $("#station-sub").textContent = st.phase;
    $("#station-sub").classList.remove("bad");
    return;
  }
  const bits = [];
  bits.push(
    !st.camera_ok ? "no camera" : st.dog_on_couch ? "DOG ON COUCH" : "watching",
  );
  if (st.fps) bits.push(`${st.fps} fps`);
  if (st.dogs_in_frame)
    bits.push(`${st.dogs_in_frame} dog${st.dogs_in_frame > 1 ? "s" : ""}`);
  if (st.recording) bits.push("recording");
  if (st.upload_queue) bits.push(`${st.upload_queue} to upload`);
  const sub = $("#station-sub");
  sub.textContent = st.last_error || bits.join(" \u00b7 ");
  sub.classList.toggle("bad", !!st.last_error);
  $("#station-badge").classList.toggle("hidden", !st.dog_on_couch);
  if (st.device?.id) stationDeviceId = st.device.id;
  // Cheap, and the count climbs while the card is open.
  if (activeTab === "devices") renderStationLogs();
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

const NO_CAMERA_HTML = `<span>No camera yet.</span> <button id="btn-empty-add" class="small">Add a camera</button>`;

function startStream(force = false) {
  const img = $("#stream");
  const empty = $("#stream-empty");
  if (!deviceId) {
    streamLive = false;
    img.classList.add("hidden");
    img.removeAttribute("src");
    empty.innerHTML = NO_CAMERA_HTML;
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
    // Two rates now: the live view runs at the camera's pace, detection at
    // its own (slower) pace in a second thread on the agent.
    $("#s-fps").textContent =
      online && s.fps
        ? s.fps.toFixed(1) +
          (s.detect_fps ? ` / ${s.detect_fps.toFixed(1)} det` : "")
        : "—";
    $("#s-dogs").textContent = online ? (s.dogs_in_frame ?? 0) : "—";
    $("#s-people").textContent = online ? (s.persons_in_frame ?? 0) : "—";
    $("#s-today").textContent = stats.today_alerts;
    $("#s-total").textContent = stats.total_alerts;
    $("#s-longest").textContent = fmtDuration(stats.longest_session_sec);
    // "Silent" is the honest answer whenever the agent has no mic: clips are
    // still recorded, so this is the only place that difference shows.
    $("#s-audio").textContent = !online ? "—" : s.audio_ok ? "On" : "Silent";
    $("#stream-badge").classList.toggle("hidden", !(online && s.dog_on_couch));

    if (d && activeTab === "live") {
      // The server ends a stream after ~20s without frames, so open a fresh
      // one when the camera comes back -- unless the page is hidden, where
      // the feed is deliberately torn down until the user returns.
      if (online && wasOnline === false && !document.hidden) startStream(true);
      const empty = $("#stream-empty");
      empty.textContent =
        d.kind === "browser"
          ? `${d.name} is offline. Open the dashboard on that device and start its camera.`
          : `${d.name} is offline. Start the agent on its computer.`;
      empty.classList.toggle("hidden", online);
      $("#stream").classList.toggle("hidden", !online);
    }

    wasOnline = d ? online : null;

    const err = $("#s-error");
    if (online && s.last_error) {
      err.textContent = s.last_error;
      err.classList.remove("hidden");
    } else err.classList.add("hidden");

    renderTrouble(d);
    if (activeTab === "live") renderZoneBox();
    renderOnboarding();
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
    if (r.online) {
      setOnb("alarm", true);
      renderOnboarding();
    }
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
        `<p class="muted">No alerts yet. When the dog gets on the couch, it shows up here. Nothing happening? Check that a couch zone is drawn on the Live tab, and see <a href="/help#troubleshooting" target="_blank">troubleshooting</a>.</p>`;
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
  // Where "the camera computer" is depends on what kind of camera it is.
  $("#al-play-label").textContent =
    d?.kind === "browser"
      ? " Play the alarm on the device running the camera tab"
      : " Play sound on the camera computer";
  $("#test-sound-hint").textContent =
    d?.kind === "browser"
      ? "Plays through the device running that camera's tab."
      : "Plays through the computer running the camera agent, not this browser.";
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
  onbTelegram = !!(tg.enabled && tg.bot_token_set && tg.chat_id);
  renderOnboarding();
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

/* ================= setup checklist ================= */
/* Every step except "alarm" and "Telegram" is read from live state, so it
 * ticks itself when the user does the thing by any route. The alarm test
 * leaves no trace on the server, so that one is remembered in this
 * browser; Telegram is fetched from the settings the server holds. */
let onbTelegram = false;

function getOnb(name) {
  try {
    return localStorage.getItem(`dfc.onb.${name}`) === "1";
  } catch (_) {
    return false;
  }
}
function setOnb(name, on) {
  try {
    if (on) localStorage.setItem(`dfc.onb.${name}`, "1");
    else localStorage.removeItem(`dfc.onb.${name}`);
  } catch (_) {}
}

async function loadOnboardingTelegram() {
  try {
    const tg = await api("/api/me/telegram");
    onbTelegram = !!(tg.enabled && tg.bot_token_set && tg.chat_id);
  } catch (_) {}
  renderOnboarding();
}

function onbSteps() {
  const d = currentDevice();
  const anyOnline = devices.some((x) => x.online);
  // Same sources as savedZone(): one drawn here, or one the camera reports
  // from its own calibration.
  const hasZone = devices.some(
    (x) =>
      (x.settings?.zone?.points?.length ? x.settings.zone.points : x.status?.zone_points || [])
        .length >= 3,
  );
  return [
    {
      id: "camera",
      title: "Add a camera",
      hint: "Use this browser, or a computer or Raspberry Pi with a webcam.",
      done: devices.length > 0,
      action: "Add camera",
      go: () => switchTab("devices"),
    },
    {
      id: "online",
      title: "Get the camera online",
      hint:
        d?.kind === "browser"
          ? "Press Start camera here on the camera device."
          : "Run the command from the Devices tab on the camera computer.",
      done: anyOnline,
      action: "Open Devices",
      go: () => switchTab("devices"),
    },
    {
      id: "zone",
      title: "Draw the couch zone",
      hint: "Click the couch's corners on the live picture.",
      done: hasZone,
      action: "Draw zone",
      go: () => {
        switchTab("live");
        if (!$("#btn-zone-edit").disabled) startZoneEdit();
      },
    },
    {
      id: "alarm",
      title: "Test the alarm",
      hint: "Play it on the camera to hear what the dog will hear.",
      done: getOnb("alarm"),
      action: "Test alarm",
      go: () => {
        switchTab("live");
        $("#btn-test-sound").click();
      },
    },
    {
      id: "telegram",
      title: "Connect Telegram",
      hint: "Optional: get a photo and the clip on your phone.",
      optional: true,
      done: onbTelegram,
      action: "Open settings",
      go: () => switchTab("settings"),
    },
  ];
}

function renderOnboarding() {
  const box = $("#onboarding");
  if (!box) return;
  const steps = onbSteps();
  const finished = steps.filter((s) => !s.optional).every((s) => s.done);
  const show =
    !getOnb("dismissed") &&
    !steps.every((s) => s.done) &&
    (activeTab === "live" || activeTab === "devices") &&
    !$("#app-view").classList.contains("hidden");
  box.classList.toggle("hidden", !show);
  if (!show) return;
  const doneCount = steps.filter((s) => s.done).length;
  $("#onb-progress").textContent = finished
    ? "Required steps done"
    : `${doneCount} of ${steps.length} done`;
  const current =
    steps.find((s) => !s.done && !s.optional) || steps.find((s) => !s.done);
  $("#onb-steps").innerHTML = steps
    .map(
      (s) => `
    <li class="onb-step ${s.done ? "done" : ""} ${s === current ? "current" : ""}">
      <span class="onb-check">${s.done ? "&#10003;" : ""}</span>
      <div class="onb-text">
        <b>${esc(s.title)}</b>${s.optional ? ' <span class="hint">(optional)</span>' : ""}
        <div class="hint">${esc(s.hint)}</div>
      </div>
      ${s.done ? "" : `<button class="small ${s === current ? "" : "secondary"}" data-onb="${s.id}">${esc(s.action)}</button>`}
    </li>`,
    )
    .join("");
}

$("#onb-steps").addEventListener("click", (e) => {
  const id = e.target.dataset?.onb;
  const step = id && onbSteps().find((s) => s.id === id);
  if (step) step.go();
});
$("#btn-onb-dismiss").addEventListener("click", () => {
  setOnb("dismissed", true);
  renderOnboarding();
  toast("Hidden. Bring it back with “Setup guide” at the bottom.");
});
$("#link-setup").addEventListener("click", (e) => {
  e.preventDefault();
  setOnb("dismissed", false);
  if (activeTab !== "live" && activeTab !== "devices") switchTab("live");
  renderOnboarding();
  $("#onboarding").scrollIntoView?.({ behavior: "smooth" });
});

/* The "Add a camera" button wherever the live view has nothing to show */
document.addEventListener("click", (e) => {
  if (e.target.id === "btn-empty-add") switchTab("devices");
});

/* ================= troubleshooting + footer ================= */
/* The first thing a stuck user sees is the Live tab, so the likely cause
 * for what the camera is reporting is spelled out there rather than only
 * in the help pages. */
function renderTrouble(d) {
  const box = $("#trouble");
  let html = "";
  let anchor = "troubleshooting";
  if (d) {
    const s = d.status || {};
    const browser = d.kind === "browser";
    if (!d.online) {
      anchor = "offline";
      html = browser
        ? `<b>${esc(d.name)} is offline.</b><ul><li>Open the dashboard on that device and press <i>Start camera here</i>.</li><li>Keep its tab open and the screen awake.</li></ul>`
        : `<b>${esc(d.name)} is offline.</b><ul><li>Is the agent running on the camera computer? On a Raspberry Pi: <code>sudo systemctl status dog-free-couch</code>.</li><li>Does that computer have internet?</li><li>Did you press <i>New token</i>? Then run the newer command.</li></ul>`;
    } else if (s.running && !s.camera_ok) {
      anchor = "no-signal";
      html = `<b>The camera is not giving a picture.</b><ul><li>Close other apps using the camera (video calls, the Camera app).</li><li>${browser ? "Pick another camera on the Devices tab." : "On the camera computer run <code>python list_cameras.py</code> and pick the one with a live picture."}</li><li>A grey or frozen picture usually means the wrong camera was chosen.</li></ul>`;
    }
  }
  box.innerHTML = html
    ? `${html}<a href="/help#${anchor}" target="_blank">More help</a>`
    : "";
  box.classList.toggle("hidden", !html);
}

function renderFooter() {
  $("#app-foot").classList.remove("hidden");
  const sup = $("#link-support");
  const fb = $("#link-feedback");
  sup.classList.toggle("hidden", !supportEmail);
  fb.classList.toggle("hidden", !supportEmail);
  if (supportEmail) {
    sup.href = `mailto:${supportEmail}?subject=${encodeURIComponent("Dog Free Couch support")}`;
    fb.href = `mailto:${supportEmail}?subject=${encodeURIComponent("Dog Free Couch feedback")}`;
  }
}

boot();
