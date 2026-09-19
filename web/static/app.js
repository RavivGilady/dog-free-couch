/* Dashboard client.
 *
 * The one genuinely non-obvious piece here is the audio path: MediaRecorder
 * gives us webm/opus, which Python's winsound cannot play, and ffmpeg is not
 * a dependency of this project. So the recording is decoded with WebAudio and
 * re-encoded to 16-bit PCM WAV in the browser before upload -- the server
 * then only ever deals with plain WAV files.
 */
const CSRF = document.body.dataset.csrf;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

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
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
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

/* ---------------- tabs ---------------- */
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "events") loadEvents();
    if (btn.dataset.tab === "sound") loadSounds();
    if (btn.dataset.tab === "settings") loadSettings();
  });
});

/* ---------------- live status ---------------- */
async function pollStatus() {
  try {
    const s = await api("/api/status");
    const dot = $("#live-dot");
    dot.className =
      "dot" + (s.dog_on_couch ? " alarm" : s.running ? " live" : "");
    $("#s-state").textContent = !s.running
      ? "Stopped"
      : s.dog_on_couch
        ? "ON COUCH"
        : s.camera_ok
          ? "Watching"
          : "No camera";
    $("#s-fps").textContent = s.fps ? s.fps.toFixed(1) : "—";
    $("#s-dogs").textContent = s.dogs_in_frame;
    $("#s-people").textContent = s.persons_in_frame;
    $("#s-today").textContent = s.stats.today_alerts;
    $("#s-total").textContent = s.stats.total_alerts;
    $("#s-longest").textContent = fmtDuration(s.stats.longest_session_sec);
    $("#stream-badge").classList.toggle("hidden", !s.dog_on_couch);

    const err = $("#s-error");
    if (s.last_error) {
      err.textContent = s.last_error;
      err.classList.remove("hidden");
    } else err.classList.add("hidden");
  } catch (e) {
    if (String(e).includes("401")) location.href = "/login";
  }
}
setInterval(pollStatus, 1000);
pollStatus();

$("#btn-test-sound").addEventListener("click", async () => {
  try {
    const r = await api("/api/sounds/test", { method: "POST" });
    toast(`Played "${r.played}" on the host`);
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
});

/* ---------------- events ---------------- */
async function loadEvents() {
  const list = $("#events-list");
  try {
    const onlyAlerts = $("#only-alerts").checked ? "1" : "0";
    const { events } = await api(
      `/api/events?only_alerts=${onlyAlerts}&limit=100`,
    );
    if (!events.length) {
      list.innerHTML = `<p class="muted">No events yet. When the dog gets on the couch, it shows up here.</p>`;
      return;
    }
    list.innerHTML = events.map(renderEvent).join("");
    wireEventButtons();
  } catch (e) {
    list.innerHTML = `<p class="alert-error">Could not load events: ${e.message}</p>`;
  }
}

function renderEvent(ev) {
  const isAlert = ev.type === "entered";
  const thumb = ev.has_snapshot
    ? `<img class="thumb" src="/media/snapshot/${ev.id}" alt="Snapshot" data-zoom="${ev.id}">`
    : `<div class="thumb empty">no image</div>`;
  const pills = [
    isAlert
      ? `<span class="pill">alert</span>`
      : `<span class="pill">left</span>`,
    ev.has_video ? `<span class="pill video">video</span>` : "",
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
      ${ev.has_video ? `<button class="small secondary btn-play" data-id="${ev.id}">Play</button>` : ""}
      <button class="small ghost btn-del" data-id="${ev.id}">Delete</button>
    </div>
  </div>`;
}

function wireEventButtons() {
  $$(".btn-play").forEach((b) =>
    b.addEventListener("click", () => {
      const row = b.closest(".event");
      if (row.querySelector("video")) {
        row.querySelector(".event-video-row").remove();
        b.textContent = "Play";
        return;
      }
      const div = document.createElement("div");
      div.className = "event-video-row";
      div.innerHTML = `<video controls autoplay preload="metadata" src="/media/video/${b.dataset.id}"></video>`;
      row.appendChild(div);
      b.textContent = "Hide";
    }),
  );

  $$(".btn-del").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Delete this event, its snapshot and its video?")) return;
      try {
        await api(`/api/events/${b.dataset.id}`, { method: "DELETE" });
        b.closest(".event").remove();
        toast("Deleted");
      } catch (e) {
        toast(`Failed: ${e.message}`);
      }
    }),
  );
}

$("#only-alerts").addEventListener("change", loadEvents);

/* ---------------- sound library ---------------- */
async function loadSounds() {
  const list = $("#sounds-list");
  try {
    const { sounds } = await api("/api/sounds");
    list.innerHTML = sounds
      .map(
        (s) => `
      <div class="sound ${s.active ? "active" : ""}">
        <div>
          <div class="name">${s.label}${s.active ? " &middot; active" : ""}</div>
          <div class="sub">${s.builtin ? "Built in" : (s.duration_sec ?? "?") + "s"}${s.created ? " &middot; " + s.created.replace("T", " ") : ""}</div>
        </div>
        <div class="spacer"></div>
        <audio controls preload="none" src="/api/sounds/preview/${s.name}" style="height:34px"></audio>
        ${s.active ? "" : `<button class="small secondary btn-activate" data-name="${s.name}">Use this</button>`}
        ${s.builtin ? "" : `<button class="small ghost btn-delsound" data-name="${s.name}">Delete</button>`}
      </div>`,
      )
      .join("");

    $$(".btn-activate").forEach((b) =>
      b.addEventListener("click", async () => {
        await api("/api/sounds/active", { json: { name: b.dataset.name } });
        toast("Alert sound updated");
        loadSounds();
      }),
    );
    $$(".btn-delsound").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Delete this sound?")) return;
        await api(`/api/sounds/${b.dataset.name}`, { method: "DELETE" });
        loadSounds();
      }),
    );
  } catch (e) {
    list.innerHTML = `<p class="alert-error">${e.message}</p>`;
  }
}

/* ---------------- mic recording ---------------- */
let mediaRecorder = null,
  chunks = [],
  recStart = 0,
  recTimer = null,
  recordedWav = null;

function encodeWav(audioBuffer) {
  // Downmix to mono: the alarm is played through whatever speaker is on the
  // host, and mono halves the file size for no audible loss here.
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
  fd.append("label", $("#rec-label").value || "recording");
  fd.append("activate", "1");
  try {
    await api("/api/sounds", { method: "POST", body: fd });
    toast("Saved and set as the alert sound");
    recordedWav = null;
    $("#rec-preview").classList.add("hidden");
    $("#rec-label").value = "";
    loadSounds();
  } catch (e) {
    toast(`Failed: ${e.message}`);
  }
});

/* ---------------- settings ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  $("#tg-enabled").checked = s.telegram.enabled;
  $("#tg-chat").value = s.telegram.chat_id || "";
  $("#tg-photo").checked = s.telegram.send_photo;
  $("#tg-video").checked = s.telegram.send_video;
  $("#tg-token-hint").textContent = s.telegram.bot_token_set
    ? `currently ${s.telegram.bot_token_hint}`
    : "not set";

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

$("#btn-save-tg").addEventListener("click", async () => {
  try {
    await api("/api/settings/telegram", {
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
    const r = await api("/api/settings/telegram/test", { method: "POST" });
    box.textContent = r.message;
    box.className = "result " + (r.ok ? "ok" : "bad");
  } catch (e) {
    box.textContent = e.message;
    box.className = "result bad";
  }
});

$("#btn-save-alert").addEventListener("click", async () => {
  await api("/api/settings/alert", {
    json: {
      play_sound: $("#al-play").checked,
      repeat_sound_sec: Number($("#al-repeat").value),
    },
  });
  toast("Alarm settings saved");
});

$("#btn-save-video").addEventListener("click", async () => {
  await api("/api/settings/video", {
    json: {
      enabled: $("#v-enabled").checked,
      pre_roll_sec: Number($("#v-pre").value),
      post_roll_sec: Number($("#v-post").value),
      max_clip_sec: Number($("#v-max").value),
    },
  });
  toast("Video settings saved");
});

$("#btn-save-pw").addEventListener("click", async () => {
  const box = $("#pw-result");
  box.classList.remove("hidden");
  try {
    await api("/api/settings/password", {
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

/* Reconnect the stream if it stalls (sleep, wifi drop). */
$("#stream").addEventListener("error", () => {
  setTimeout(() => {
    $("#stream").src = "/stream.mjpg?t=" + Date.now();
  }, 2000);
});
