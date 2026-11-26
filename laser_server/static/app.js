"use strict";

const $ = (sel) => document.querySelector(sel);
let status = null;
let logSince = 0;

function toast(msg, kind = "error") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 4000);
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : `${res.status} ${res.statusText}`;
    toast(msg);
    throw new Error(msg);
  }
  return data;
}
const post = (url, body) => api("POST", url, body).catch(() => {});

const fmt = (n) => (typeof n === "number" ? n.toFixed(2) : "–");
function fmtTime(s) {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
}

// ------------------------------------------------------------------ status
async function pollStatus() {
  try {
    status = await (await fetch("/api/status")).json();
    render(status);
  } catch {
    $("#conn").textContent = "Server unreachable";
    $("#conn").className = "pill bad";
  }
  setTimeout(pollStatus, 300);
}

function render(s) {
  const conn = $("#conn");
  conn.textContent = s.connected ? `Connected ${s.port || ""}` : "Laser not connected";
  conn.className = `pill ${s.connected ? "ok" : "bad"}`;

  const st = $("#state");
  st.textContent = s.state;
  const base = s.state.split(":")[0];
  st.className = "pill " + ({ Idle: "ok", Run: "warn", Jog: "warn", Home: "warn", Hold: "warn", Alarm: "bad", Door: "bad" }[base] || "");

  ["x", "y", "z"].forEach((a, i) => {
    $(`#w${a}`).textContent = fmt(s.wpos[i]);
    $(`#m${a}`).textContent = fmt(s.mpos[i]);
  });
  $("#feed").textContent = `${Math.round(s.feed / 6) / 10} mm/s`;
  $("#power").textContent = Math.round(s.spindle);
  $("#version").textContent = s.version || "";

  const alarm = $("#alarm");
  if (s.alarm || base === "Alarm") {
    alarm.textContent = `${s.alarm || "Alarm"} — home the machine or press Unlock.`;
    alarm.classList.remove("hidden");
  } else alarm.classList.add("hidden");

  const j = s.job;
  const running = j.running;
  $("#frame-power").textContent = s.frame_power;
  const rem = $("#job-remaining");
  if (running && j.preparing) {
    rem.innerHTML = `<span class="spinner"></span>Preparing job…`;
  } else if (running && j.remaining != null) {
    const done = new Date(Date.now() + j.remaining * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    rem.innerHTML = `~${fmtTime(j.remaining)} left <span class="dim">· done ≈ ${done}${j.paused ? " (paused)" : ""} · est. total ${fmtTime(j.est_total)}</span>`;
  } else rem.textContent = "";
  $("#cam-hud").textContent = running
    ? `${s.state} · ${j.percent}%${j.remaining != null ? ` · ~${fmtTime(j.remaining)} left` : ""}`
    : `${s.state} · X ${fmt(s.wpos[0])} Y ${fmt(s.wpos[1])}`;
  $("#job-name").textContent = j.name ? `${j.name}${running ? (j.paused ? " (paused)" : " (running)") : ""}` : "No job running";
  $("#job-name").classList.toggle("dim", !running);
  $("#job-bar").style.width = `${j.percent}%`;
  $("#job-lines").textContent = j.total_lines ? `${j.lines_done} / ${j.total_lines} lines (${j.percent}%)` : "";
  $("#job-time").textContent = j.elapsed != null ? `Elapsed ${fmtTime(j.elapsed)}` : "";
  $("#job-result").textContent = !running && j.result ? `Last job: ${j.result}` : "";

  $("#btn-pause").disabled = !running || j.paused;
  $("#btn-resume").disabled = !running || !j.paused;
  $("#btn-stop").disabled = !running;
  const manualOff = running || !s.connected;
  document.querySelectorAll(".jog button, #files .run, #files .frame, #cmd-form button")
    .forEach((b) => (b.disabled = manualOff));

  const camMsg = $("#cam-msg");
  if (!s.camera.available) {
    camMsg.textContent = s.camera.error || "Waiting for camera…";
    camMsg.classList.remove("hidden");
  } else if (camMsg.textContent && !camMsg.classList.contains("hidden")) {
    camMsg.classList.add("hidden");
    startCamera();
  }
}

// ------------------------------------------------------------------ camera
function startCamera() {
  $("#cam").src = `/camera/stream.mjpg?t=${Date.now()}`;
}
$("#cam").addEventListener("error", () => setTimeout(startCamera, 3000));

// --------------------------------------------------------------------- jog
document.querySelectorAll("[data-jog]").forEach((b) => {
  b.addEventListener("click", () => {
    const [dx, dy] = b.dataset.jog.split(",").map(Number);
    const step = parseFloat($("#step").value);
    post("/api/jog", { x: dx * step, y: dy * step, feed: (parseFloat($("#jogfeed").value) || 20) * 60 });
  });
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const map = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
  if (map[e.key]) {
    e.preventDefault();
    const step = parseFloat($("#step").value);
    post("/api/jog", { x: map[e.key][0] * step, y: map[e.key][1] * step, feed: (parseFloat($("#jogfeed").value) || 20) * 60 });
  } else if (e.key === "Escape") post("/api/jog/cancel");
});

$("#btn-home").onclick = () => post("/api/home");
$("#btn-unlock").onclick = () => post("/api/unlock");
$("#btn-set-origin").onclick = () => post("/api/origin/set");
$("#btn-goto-origin").onclick = () => post("/api/origin/goto");
$("#btn-reset").onclick = () => post("/api/reset");
$("#btn-pause").onclick = () => post("/api/job/pause");
$("#btn-resume").onclick = () => post("/api/job/resume");
$("#btn-stop").onclick = () => post("/api/job/stop");

// ------------------------------------------------------------------- files
async function loadFiles() {
  let files = [];
  try { files = await api("GET", "/api/jobs"); } catch { return; }
  const tbody = $("#files tbody");
  tbody.innerHTML = "";
  if (!files.length) {
    tbody.innerHTML = `<tr><td class="dim">No files yet — upload G-code exported from LightBurn, LaserGRBL, etc.</td></tr>`;
    return;
  }
  for (const f of files) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.className = "name";
    name.innerHTML = `<div></div><div class="small dim">${(f.size / 1024).toFixed(1)} KB · ${new Date(f.modified * 1000).toLocaleString()}</div>`;
    name.firstChild.textContent = f.name;
    const actions = document.createElement("td");
    actions.className = "actions";
    const enc = encodeURIComponent(f.name);
    const mk = (label, cls, fn) => {
      const b = document.createElement("button");
      b.textContent = label; b.className = cls; b.onclick = fn;
      actions.appendChild(b); actions.append(" ");
    };
    mk("Frame", "frame", async () => {
      const laser = $("#frame-laser").checked;
      try {
        const b = await api("POST", `/api/jobs/${enc}/frame?laser=${laser}`);
        toast(`Framing ${b.xmin.toFixed(1)},${b.ymin.toFixed(1)} → ${b.xmax.toFixed(1)},${b.ymax.toFixed(1)} ` +
              (laser ? `(laser on at ${status?.frame_power ?? 1}%)` : "(laser off)"), "info");
      } catch {}
    });
    mk("Run", "run", () => {
      if (confirm(`Start "${f.name}"?\n\nMake sure the material is in place, the lid/shield is closed and ventilation is on.`))
        post(`/api/jobs/${enc}/run`);
    });
    mk("Delete", "", async () => {
      if (confirm(`Delete "${f.name}"?`)) { await api("DELETE", `/api/jobs/${enc}`).catch(() => {}); loadFiles(); }
    });
    tr.append(name, actions);
    tbody.appendChild(tr);
  }
  if (status) render(status);
}

$("#upload").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  $("#upload-status").textContent = `Uploading ${file.name}…`;
  try {
    await api("POST", "/api/jobs", fd);
    $("#upload-status").textContent = `Uploaded ${file.name}`;
  } catch { $("#upload-status").textContent = ""; }
  e.target.value = "";
  loadFiles();
});

// ----------------------------------------------------------------- console
async function pollLog() {
  try {
    const entries = await (await fetch(`/api/console?since=${logSince}`)).json();
    if (entries.length) {
      const log = $("#log");
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
      for (const e of entries) {
        const div = document.createElement("div");
        div.className = e.kind;
        div.textContent = (e.kind === "tx" ? "> " : "") + e.text;
        log.appendChild(div);
        logSince = Math.max(logSince, e.t);
      }
      while (log.childElementCount > 500) log.firstChild.remove();
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
  } catch {}
  setTimeout(pollLog, 700);
}

$("#cmd-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const cmd = $("#cmd").value.trim();
  if (!cmd) return;
  post("/api/command", { command: cmd });
  $("#cmd").value = "";
});

// -------------------------------------------------------------- fullscreen
function toggleFullscreen() {
  const wrap = $("#cam-wrap");
  if (document.fullscreenElement) document.exitFullscreen();
  else if (wrap.requestFullscreen) wrap.requestFullscreen().catch(() => toast("Fullscreen isn't available in this browser"));
  else if (wrap.webkitRequestFullscreen) wrap.webkitRequestFullscreen();  // iOS Safari
}
$("#btn-fullscreen").onclick = toggleFullscreen;
$("#btn-exit-fullscreen").onclick = toggleFullscreen;
$("#btn-fs-stop").onclick = () => post("/api/reset");
$("#cam").addEventListener("dblclick", toggleFullscreen);
document.addEventListener("keydown", (e) => {
  if (!e.target.matches("input, select, textarea") && e.key.toLowerCase() === "f") toggleFullscreen();
});

try { $("#frame-laser").checked = localStorage.getItem("laser-server.frameLaser") === "1"; } catch {}
$("#frame-laser").addEventListener("change", () => {
  try { localStorage.setItem("laser-server.frameLaser", $("#frame-laser").checked ? "1" : "0"); } catch {}
});

startCamera();
pollStatus();
pollLog();
loadFiles();
