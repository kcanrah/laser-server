"use strict";
// Design editor. World coordinates are millimetres, origin bottom-left, Y up,
// matching the machine's work coordinates. Geometry must match design.py.

const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
const STORAGE_KEY = "laser-server.design";
const PALETTE = ["#e5484d", "#3b82f6", "#22c55e", "#eab308", "#a855f7", "#ec4899", "#14b8a6", "#f97316"];

let design = null;
let bed = { w: 400, h: 400 };
let machine = null;
let fonts = { fonts: [], default: null };
let selectedId = null;
let currentLayer = null;
let expandedLayer = null;
let tool = "select";
const view = { zoom: 1, panX: 0, panY: 0 };
let undoStack = [];
let redoStack = [];
let dirty = false;
let previewOn = false;
let previewTimer = null;
let drag = null;
let spaceDown = false;
const snap = { grid: true, size: 1, objects: true };
try { Object.assign(snap, JSON.parse(localStorage.getItem("laser-server.snap") || "{}")); } catch {}
let cursorWorld = null;

// ----------------------------------------------------------------- helpers
const uid = () => Math.random().toString(36).slice(2, 10);
const r1 = (v) => Math.round(v * 10) / 10;
const r3 = (v) => Math.round(v * 1000) / 1000;
const deg = (r) => (r * 180) / Math.PI;
const rad = (d) => (d * Math.PI) / 180;
// Speeds are stored as mm/min (what G-code uses) and shown as mm/s.
const toMmS = (mmMin) => Math.round((mmMin / 60) * 100) / 100;
const fromMmS = (mmS) => mmS * 60;
const isImageLayer = (l) => l?.mode === "image";
const fitsLayer = (o, l) => (o.type === "image") === isImageLayer(l);

function toast(msg, kind = "error", html = false) {
  const t = $("#toast");
  if (html) t.innerHTML = msg; else t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), html ? 8000 : 4000);
}

async function api(method, url, body, { quiet = false } = {}) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail
      : Array.isArray(data.detail) ? data.detail.map((d) => `${d.loc?.at(-1) ?? ""}: ${d.msg}`).join("; ")
      : `${res.status} ${res.statusText}`;
    if (!quiet) toast(msg);
    throw new Error(msg);
  }
  return data;
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function fmtTime(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

// --------------------------------------------------------------- the model
function newLayer(i, overrides = {}) {
  return { id: uid(), name: `Layer ${i + 1}`, color: PALETTE[i % PALETTE.length], mode: "line",
    power: 50, speed: 1000, passes: 1, hatch: 0.1, angle: 0, enabled: true, ...overrides };
}

function defaultDesign() {
  return {
    name: "untitled",
    layers: [
      newLayer(0, { name: "Cut", mode: "line", power: 100, speed: 300, passes: 1 }),
      newLayer(1, { name: "Engrave", mode: "fill", power: 30, speed: 3000, hatch: 0.1 }),
      newLayer(2, { name: "Score", mode: "line", power: 20, speed: 1500 }),
    ],
    objects: [],
  };
}

const layerById = (id) => design.layers.find((l) => l.id === id);
const objById = (id) => design.objects.find((o) => o.id === id);
const selected = () => (selectedId ? objById(selectedId) : null);

// ---------------------------------------------------------------- geometry
function localPaths(o) {
  if (o.type === "rect" || o.type === "image") return [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]];
  if (o.type === "ellipse") {
    const per = (Math.PI * (Math.abs(o.w) + Math.abs(o.h))) / 2;
    const n = Math.floor(Math.min(720, Math.max(24, per / 0.5)));
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const a = (2 * Math.PI * i) / n;
      pts.push([0.5 + 0.5 * Math.cos(a), 0.5 + 0.5 * Math.sin(a)]);
    }
    return [pts];
  }
  return (o.paths || []).filter((p) => p.length >= 2);
}

function pathsBBox(paths) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const pl of paths) for (const [x, y] of pl) {
    if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y;
  }
  return x0 === Infinity ? null : [x0, y0, x1, y1];
}

function rotateAbout(px, py, cx, cy, a) {
  const c = Math.cos(a), s = Math.sin(a), dx = px - cx, dy = py - cy;
  return [cx + dx * c - dy * s, cy + dx * s + dy * c];
}

function objectPolylines(o) {
  const local = localPaths(o);
  const lb = o.type === "path" ? pathsBBox(local) : [0, 0, 1, 1];
  if (!lb) return [];
  const [lx0, ly0, lx1, ly1] = lb;
  const bw = lx1 - lx0, bh = ly1 - ly0;
  const sx = bw > 1e-9 ? o.w / bw : 1, sy = bh > 1e-9 ? o.h / bh : 1;
  const cx = o.x + o.w / 2, cy = o.y + o.h / 2, a = rad(o.rot || 0);
  return local.map((pl) => pl.map(([px, py]) => {
    const ax = (px - lx0) * sx + o.x, ay = (py - ly0) * sy + o.y;
    return a ? rotateAbout(ax, ay, cx, cy, a) : [ax, ay];
  }));
}

function boxCorners(o) {
  const cx = o.x + o.w / 2, cy = o.y + o.h / 2, a = rad(o.rot || 0);
  return [[0, 0], [1, 0], [1, 1], [0, 1]].map(([u, v]) => rotateAbout(o.x + u * o.w, o.y + v * o.h, cx, cy, a));
}

function polyD(pls) {
  return pls.map((pl) => "M" + pl.map(([x, y]) => `${r3(x)} ${r3(y)}`).join("L")).join("");
}

// ----------------------------------------------------------------- history
function checkpoint() {
  undoStack.push(JSON.stringify(design));
  if (undoStack.length > 100) undoStack.shift();
  redoStack = [];
}

function changed({ layers = false } = {}) {
  dirty = true;
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(design)); } catch {}
  if (layers) renderLayers();
  renderObjects();
  renderProps();
  schedulePreview();
}

function undo() {
  if (!undoStack.length) return;
  redoStack.push(JSON.stringify(design));
  design = JSON.parse(undoStack.pop());
  afterLoad();
  changed({ layers: true });
}

function redo() {
  if (!redoStack.length) return;
  undoStack.push(JSON.stringify(design));
  design = JSON.parse(redoStack.pop());
  afterLoad();
  changed({ layers: true });
}

function afterLoad() {
  if (!design.layers?.length) design.layers = defaultDesign().layers;
  design.objects ||= [];
  for (const o of design.objects) o.id ||= uid();
  if (!layerById(currentLayer)) currentLayer = design.layers[0].id;
  if (selectedId && !objById(selectedId)) selectedId = null;
  $("#design-name").value = design.name || "untitled";
}

// ------------------------------------------------------------------- view
const svg = $("#stage");
const world = $("#world");

function applyView() {
  world.setAttribute("transform", `translate(${view.panX} ${view.panY}) scale(${view.zoom} ${-view.zoom})`);
  renderOverlay();
}

function fitView() {
  const r = svg.getBoundingClientRect();
  const pad = 30;
  view.zoom = Math.max(0.05, Math.min((r.width - 2 * pad) / bed.w, (r.height - 2 * pad) / bed.h));
  view.panX = (r.width - bed.w * view.zoom) / 2;
  view.panY = (r.height + bed.h * view.zoom) / 2;
  applyView();
}

function toWorld(e) {
  const r = svg.getBoundingClientRect();
  return [(e.clientX - r.left - view.panX) / view.zoom, (view.panY - (e.clientY - r.top)) / view.zoom];
}

function renderBed() {
  const b = $("#bed");
  b.setAttribute("width", bed.w);
  b.setAttribute("height", bed.h);
  const g = $("#grid");
  g.innerHTML = "";
  for (let x = 10; x < bed.w; x += 10) el("line", { x1: x, y1: 0, x2: x, y2: bed.h, class: x % 50 ? "" : "major" }, g);
  for (let y = 10; y < bed.h; y += 10) el("line", { x1: 0, y1: y, x2: bed.w, y2: y, class: y % 50 ? "" : "major" }, g);
}

// ---------------------------------------------------------------- render
function renderObjects() {
  const g = $("#objects");
  g.innerHTML = "";
  for (const o of design.objects) {
    const layer = layerById(o.layer) || design.layers[0];
    if (o.type === "image") {
      const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
      const grp = el("g", { "data-id": o.id, transform: `rotate(${o.rot || 0} ${cx} ${cy})`,
        class: layer.enabled ? "" : "hidden-layer", opacity: layer.enabled ? 1 : 0.3 }, g);
      const flip = el("g", { transform: `translate(${o.x} ${o.y + o.h}) scale(1 -1)` }, grp);
      const img = el("image", { width: o.w, height: o.h, preserveAspectRatio: "none" }, flip);
      img.setAttribute("href", `/api/images/${o.image}`);
      if (o.invert || o.brightness || o.contrast) {
        const b = 1 + (o.brightness || 0) / 100, c = 1 + (o.contrast || 0) / 100;
        img.style.filter = `${o.invert ? "invert(1) " : ""}brightness(${b}) contrast(${c})`;
      }
      el("rect", { x: o.x, y: o.y, width: o.w, height: o.h, class: "img-frame", stroke: layer.color, "pointer-events": "all" }, grp);
      continue;
    }
    const d = polyD(objectPolylines(o));
    const grp = el("g", { "data-id": o.id }, g);
    el("path", { d, stroke: "transparent", "stroke-width": 10, fill: "transparent",
      "vector-effect": "non-scaling-stroke", "pointer-events": "all" }, grp);
    const fill = layer.mode.includes("fill") ? layer.color + "40" : "none";
    el("path", { d, stroke: layer.color, fill, class: layer.enabled ? "" : "hidden-layer", "pointer-events": "none" }, grp);
  }
  renderOverlay();
}

function renderOverlay() {
  const g = $("#overlay");
  g.innerHTML = "";
  const o = selected();
  if (o) {
    const c = boxCorners(o);
    el("polygon", { points: c.map((p) => p.join(",")).join(" "), class: "sel-box" }, g);
    const hr = 5 / view.zoom;
    const handles = [[0, 0], [1, 0], [1, 1], [0, 1]];
    handles.forEach(([u, v], i) => el("circle", { cx: c[i][0], cy: c[i][1], r: hr, class: "handle", "data-handle": `${u},${v}`, style: "cursor: nwse-resize" }, g));
    // rotation handle above the top edge
    const a = rad(o.rot || 0);
    const topMid = [(c[2][0] + c[3][0]) / 2, (c[2][1] + c[3][1]) / 2];
    const off = 24 / view.zoom;
    const rh = [topMid[0] - Math.sin(a) * off, topMid[1] + Math.cos(a) * off];
    el("line", { x1: topMid[0], y1: topMid[1], x2: rh[0], y2: rh[1], class: "sel-box" }, g);
    el("circle", { cx: rh[0], cy: rh[1], r: hr, class: "handle rot", "data-handle": "rot" }, g);
  }
  for (const gd of drag?.guides || []) {
    if (gd.x !== undefined) el("line", { x1: gd.x, y1: -1e4, x2: gd.x, y2: 1e4, class: "guide" }, g);
    else el("line", { x1: -1e4, y1: gd.y, x2: 1e4, y2: gd.y, class: "guide" }, g);
  }
  if (drag?.mode === "draw" && drag.cur) {
    const [x0, y0] = drag.start, [x1, y1] = drag.cur;
    if (drag.tool === "line") el("line", { x1: x0, y1: y0, x2: x1, y2: y1, class: "rubber" }, g);
    else if (drag.tool === "ellipse")
      el("ellipse", { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, rx: Math.abs(x1 - x0) / 2, ry: Math.abs(y1 - y0) / 2, class: "rubber" }, g);
    else el("rect", { x: Math.min(x0, x1), y: Math.min(y0, y1), width: Math.abs(x1 - x0), height: Math.abs(y1 - y0), class: "rubber" }, g);
  }
  drawRulers();
}

const MODE_NAMES = { line: "Line", fill: "Fill", "fill+line": "Fill+Line", image: "Image" };
function layerSummary(l) {
  const p = linkedPreset(l);
  const star = p ? (presetMatches(l, p) ? "★ " : "☆ ") : "";
  return `${star}${MODE_NAMES[l.mode]} · ${l.power}% · ${toMmS(l.speed)} mm/s · ${l.passes}×`;
}

// ---------------------------------------------------------------- presets
let presets = [];
const PRESET_KEYS = ["mode", "power", "min_power", "speed", "passes", "hatch", "angle", "dither", "overscan", "bidirectional"];
const presetLabel = (p) => (p.material ? `${p.material} – ${p.name}` : p.name);
const linkedPreset = (l) => (l.preset ? presets.find((p) => p.id === l.preset) : null);

function presetMatches(l, p) {
  return Object.entries(p.settings).every(([k, v]) =>
    typeof v === "number" ? Math.abs((l[k] ?? NaN) - v) < 1e-6 : l[k] === v);
}

function presetSummary(st) {
  let out = `${MODE_NAMES[st.mode]} · ${st.power}% · ${toMmS(st.speed)} mm/s · ${st.passes}×`;
  if (st.mode !== "line" && st.hatch) out += ` · ${st.hatch} mm`;
  if (st.mode === "image" && st.dither) out += ` · ${{ floyd: "dithered", threshold: "threshold", grayscale: "grayscale" }[st.dither]}`;
  return out;
}

const layerSettings = (l) => Object.fromEntries(PRESET_KEYS.filter((k) => l[k] !== undefined).map((k) => [k, l[k]]));

async function loadPresets() {
  try { presets = await api("GET", "/api/presets", undefined, { quiet: true }); } catch { presets = []; }
}

function presetOptionsHTML(filterImage) {
  const groups = {};
  for (const p of presets) {
    if (filterImage !== null && (p.settings.mode === "image") !== filterImage) continue;
    (groups[p.material || "Other"] ||= []).push(p);
  }
  const esc = (t) => t.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  return Object.entries(groups).map(([m, ps]) =>
    `<optgroup label="${esc(m)}">${ps.map((p) => `<option value="${p.id}">${esc(p.name)} — ${esc(presetSummary(p.settings))}</option>`).join("")}</optgroup>`).join("");
}

function applyPreset(l, p) {
  const hasObjects = design.objects.some((o) => o.layer === l.id);
  if (hasObjects && (p.settings.mode === "image") !== isImageLayer(l)) {
    toast(p.settings.mode === "image" ? "That's an image preset; this layer has shapes." : "That's a shape preset; this layer has images.");
    return;
  }
  checkpoint();
  Object.assign(l, p.settings);
  l.preset = p.id;
  if (/^Layer \d+$/.test(l.name) || !l.name) l.name = presetLabel(p);
  changed({ layers: true });
  toast(`Applied preset "${presetLabel(p)}"`, "info");
}

// Preset editor. opts: {settings, preset (to edit), layer (to link after saving), material}
let presetCtx = null;
function openPresetDialog(opts) {
  presetCtx = opts;
  const f = $("#preset-form");
  const st = { ...(opts.preset?.settings || {}), ...(opts.settings || {}) };
  f.elements.material.value = opts.preset?.material ?? opts.material ?? "";
  f.elements.name.value = opts.preset?.name ?? opts.name ?? "";
  f.elements.notes.value = opts.preset?.notes ?? "";
  f.elements.mode.value = st.mode || "line";
  f.elements.power.value = st.power ?? 50;
  f.elements.speed.value = toMmS(st.speed ?? 1200);
  f.elements.passes.value = st.passes ?? 1;
  f.elements.hatch.value = st.hatch ?? 0.1;
  presetCtx.base = st;
  const mats = [...new Set(presets.map((p) => p.material).filter(Boolean))];
  $("#material-list").innerHTML = mats.map((m) => `<option value="${m.replace(/"/g, "&quot;")}">`).join("");
  const target = opts.preset || (opts.layer && linkedPreset(opts.layer));
  $("#preset-title").textContent = opts.preset ? "Edit preset" : "Save as preset";
  const upd = $("#preset-update");
  upd.classList.toggle("hidden", !target);
  if (target) { upd.textContent = `Update "${presetLabel(target)}"`; presetCtx.target = target; }
  $("#preset-save").textContent = opts.preset ? "Save as a copy" : "Save as new preset";
  syncPresetForm();
  $("#preset-dialog").showModal();
  (f.elements.material.value ? f.elements.name : f.elements.material).focus();
}

function syncPresetForm() {
  const mode = $("#preset-form").elements.mode.value;
  $(".pf-interval").classList.toggle("hidden", mode === "line");
}
$("#preset-form").elements.mode.addEventListener("change", syncPresetForm);

function presetFromForm() {
  const f = $("#preset-form").elements;
  const num = (n) => parseFloat(f[n].value);
  const settings = { ...presetCtx.base, mode: f.mode.value, power: num("power"), speed: fromMmS(num("speed")),
    passes: Math.round(num("passes")) };
  if (f.mode.value !== "line") settings.hatch = num("hatch") || 0.1;
  if (f.mode.value === "image") { settings.dither ||= "floyd"; settings.overscan ??= 2; settings.min_power ??= 0; settings.bidirectional ??= true; }
  return { material: f.material.value.trim(), name: f.name.value.trim(), notes: f.notes.value.trim(), settings };
}

async function submitPreset(update) {
  const data = presetFromForm();
  if (!data.name) { toast("Give the preset a name"); return; }
  if (!update && presets.some((p) => p.material.toLowerCase() === data.material.toLowerCase() && p.name.toLowerCase() === data.name.toLowerCase())
      && !confirm(`There's already a "${presetLabel(data)}" preset. Save another one with the same name?`)) return;
  try {
    const saved = update
      ? await api("PUT", `/api/presets/${presetCtx.target.id}`, data)
      : await api("POST", "/api/presets", data);
    await loadPresets();
    const l = presetCtx.layer;
    if (l) {
      // Link the layer to the preset; if the user edited values in the dialog, apply them.
      if (!presetMatches(l, saved)) { checkpoint(); Object.assign(l, saved.settings); }
      l.preset = saved.id;
      changed({ layers: true });
    } else renderLayers();
    $("#preset-dialog").close();
    if ($("#presets-dialog").open) renderPresetList();
    toast(`${update ? "Updated" : "Saved"} preset "${presetLabel(saved)}"`, "info");
  } catch {}
}
$("#preset-save").onclick = () => submitPreset(false);
$("#preset-update").onclick = () => submitPreset(true);
$("#preset-cancel").onclick = () => $("#preset-dialog").close();

function renderPresetList() {
  const box = $("#preset-list");
  box.innerHTML = presets.length ? "" : `<p class="dim small">No presets yet. Open a layer's ▾ settings and click "Save as preset…", or save one from a test pattern.</p>`;
  const groups = {};
  for (const p of presets) (groups[p.material || "Other"] ||= []).push(p);
  for (const [m, ps] of Object.entries(groups)) {
    const g = document.createElement("div");
    g.className = "preset-group";
    g.innerHTML = "<h3></h3>";
    g.querySelector("h3").textContent = m;
    for (const p of ps) {
      const it = document.createElement("div");
      it.className = "preset-item";
      it.innerHTML = `<div class="info"><div class="nm"></div><div class="sum"></div><div class="notes"></div></div>
        <button class="use" title="Apply to the current layer">Apply</button><button class="ed">Edit</button><button class="danger del">Delete</button>`;
      it.querySelector(".nm").textContent = p.name;
      it.querySelector(".sum").textContent = presetSummary(p.settings);
      it.querySelector(".notes").textContent = p.notes || "";
      it.querySelector(".use").onclick = () => {
        const l = layerById(currentLayer);
        if (l) { applyPreset(l, p); expandedLayer = l.id; renderLayers(); }
      };
      it.querySelector(".ed").onclick = () => openPresetDialog({ preset: p });
      it.querySelector(".del").onclick = async () => {
        if (!confirm(`Delete preset "${presetLabel(p)}"?`)) return;
        try { await api("DELETE", `/api/presets/${p.id}`); await loadPresets(); renderPresetList(); renderLayers(); } catch {}
      };
      g.appendChild(it);
    }
    box.appendChild(g);
  }
}

$("#btn-presets").onclick = async () => {
  await loadPresets();
  renderPresetList();
  $("#presets-dialog").showModal();
};
$("#presets-close").onclick = () => $("#presets-dialog").close();
$("#preset-import").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("POST", "/api/presets/import", fd);
    await loadPresets();
    renderPresetList();
    renderLayers();
    toast(`Imported ${r.imported} preset(s)`, "info");
  } catch {}
});

function renderLayers() {
  const box = $("#layers");
  box.innerHTML = "";
  for (const l of design.layers) {
    const count = design.objects.filter((o) => o.layer === l.id).length;
    const div = document.createElement("div");
    div.className = "layer" + (l.id === currentLayer ? " current" : "");
    div.innerHTML = `
      <div class="layer-row" title="Click to make this the layer new objects go on">
        <input type="color" value="${l.color}" title="Layer color">
        <div class="lname"><div class="nm"></div><div class="lsum">${layerSummary(l)} · ${count} obj</div></div>
        <input type="checkbox" ${l.enabled ? "checked" : ""} title="Include this layer when burning">
        <button class="small-btn tog" title="Settings">${expandedLayer === l.id ? "▴" : "▾"}</button>
      </div>`;
    div.querySelector(".nm").textContent = l.name;
    const row = div.querySelector(".layer-row");
    row.addEventListener("click", (e) => {
      if (e.target.closest("input, button")) return;
      currentLayer = l.id;
      renderLayers();
    });
    const [color, enabled] = row.querySelectorAll("input");
    color.addEventListener("change", () => { checkpoint(); l.color = color.value; changed({ layers: true }); });
    enabled.addEventListener("change", () => { checkpoint(); l.enabled = enabled.checked; changed({ layers: true }); });
    row.querySelector(".tog").addEventListener("click", () => {
      expandedLayer = expandedLayer === l.id ? null : l.id;
      renderLayers();
    });

    if (expandedLayer === l.id) {
      const body = document.createElement("div");
      body.className = "layer-body";
      const fill = l.mode.includes("fill");
      const img = isImageLayer(l);
      l.dither ||= "floyd";
      l.min_power ??= 0;
      l.overscan ??= 2;
      l.bidirectional ??= true;
      body.innerHTML = `
        <label class="full">Name <input data-k="name" type="text"></label>
        <div class="grid2">
          <label>Mode <select data-k="mode">
            <option value="line">Line (cut/score)</option><option value="fill">Fill (engrave)</option>
            <option value="fill+line">Fill + Line</option><option value="image">Image (raster)</option></select></label>
          <label>${img ? "Max power %" : "Power %"} <input data-k="power" type="number" min="0" max="100" step="1"></label>
          <label>Speed mm/s <input data-k="speed" type="number" min="0.1" step="1"></label>
          <label>Passes <input data-k="passes" type="number" min="1" max="100" step="1"></label>
          ${fill ? `<label>Line interval mm <input data-k="hatch" type="number" min="0.01" step="0.01"></label>
          <label>Fill angle° <input data-k="angle" type="number" step="15"></label>` : ""}
          ${img ? `<label>Dither <select data-k="dither">
              <option value="floyd">Dithered (photos)</option><option value="threshold">Threshold (B&amp;W)</option>
              <option value="grayscale">Grayscale (power)</option></select></label>
            <label>Min power % <input data-k="min_power" type="number" min="0" max="100" step="1" ${l.dither === "grayscale" ? "" : "disabled title='Grayscale only'"}></label>
            <label>Line interval mm <input data-k="hatch" type="number" min="0.02" step="0.01"></label>
            <label>Resolution <input type="text" disabled value="${Math.round(25.4 / (l.hatch || 0.1))} DPI"></label>
            <label>Overscan mm <input data-k="overscan" type="number" min="0" step="0.5"></label>
            <label class="check"><input data-k="bidirectional" type="checkbox"> Both directions</label>` : ""}
        </div>
        <div class="row"><button class="small-btn danger del">Delete layer</button></div>`;
      body.querySelectorAll("[data-k]").forEach((inp) => {
        const k0 = inp.dataset.k;
        if (inp.type === "checkbox") inp.checked = !!l[k0];
        else inp.value = k0 === "speed" ? toMmS(l.speed) : l[k0];
        inp.addEventListener("change", () => {
          const k = inp.dataset.k;
          let v = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? parseFloat(inp.value) : inp.value;
          if (inp.type === "number" && !Number.isFinite(v)) { inp.value = k === "speed" ? toMmS(l.speed) : l[k]; return; }
          if (k === "power" || k === "min_power") v = Math.min(100, Math.max(0, v));
          if (k === "passes") v = Math.min(100, Math.max(1, Math.round(v)));
          if (k === "speed") v = fromMmS(Math.max(0.1, v));
          if (k === "hatch") v = Math.max(0.01, v);
          if (k === "overscan") v = Math.max(0, v);
          if (k === "mode" && (v === "image") !== isImageLayer(l) && design.objects.some((o) => o.layer === l.id)) {
            toast(v === "image" ? "Image layers can only hold images. Move this layer's shapes first."
                                : "This layer has images. Move them to another image layer first.");
            inp.value = l.mode;
            return;
          }
          checkpoint();
          l[k] = v;
          if (k === "mode" && v === "image" && l.speed < 1200) l.speed = 6000;
          changed({ layers: true });
        });
      });
      body.querySelector(".del").addEventListener("click", () => {
        if (design.layers.length === 1) return toast("A design needs at least one layer");
        const n = design.objects.filter((o) => o.layer === l.id).length;
        if (n && !confirm(`Delete layer "${l.name}" and its ${n} object(s)?`)) return;
        checkpoint();
        design.objects = design.objects.filter((o) => o.layer !== l.id);
        design.layers = design.layers.filter((x) => x !== l);
        afterLoad();
        changed({ layers: true });
      });
      // Presets: apply one, see which one the layer came from, save the layer's settings.
      const linked = linkedPreset(l);
      const hasObjs = design.objects.some((o) => o.layer === l.id);
      const pr = document.createElement("div");
      pr.innerHTML = `
        <div class="preset-row">
          <select class="preset-sel" title="Apply saved settings for a material"><option value="">★ Apply a preset…</option>
            ${presetOptionsHTML(hasObjs ? isImageLayer(l) : null)}</select>
          <button class="small-btn save-preset" title="Save this layer's settings as a material preset">Save as preset…</button>
        </div>
        ${linked ? `<div class="preset-link ${presetMatches(l, linked) ? "" : "modified"}">
            <span></span>${presetMatches(l, linked) ? "" : `<button class="small-btn upd-preset">Update preset</button>`}</div>` : ""}`;
      if (linked) pr.querySelector(".preset-link span").textContent =
        presetMatches(l, linked) ? `★ ${presetLabel(linked)}` : `☆ Changed from ${presetLabel(linked)}`;
      pr.querySelector(".preset-sel").addEventListener("change", (e) => {
        const p = presets.find((x) => x.id === e.target.value);
        if (p) applyPreset(l, p);
      });
      pr.querySelector(".save-preset").addEventListener("click", () =>
        openPresetDialog({ layer: l, settings: layerSettings(l), material: linked?.material,
          name: linked ? linked.name : /^Layer \d+$/.test(l.name) ? "" : l.name }));
      pr.querySelector(".upd-preset")?.addEventListener("click", () =>
        openPresetDialog({ layer: l, settings: layerSettings(l), material: linked.material, name: linked.name }));
      body.querySelector("label.full").after(...pr.children);
      div.appendChild(body);
    }
    box.appendChild(div);
  }
}

function renderProps() {
  const o = selected();
  $("#props").classList.toggle("hidden", !o);
  $("#no-selection").classList.toggle("hidden", !!o);
  if (!o) return;
  const ls = $("#p-layer");
  ls.innerHTML = "";
  for (const l of design.layers) {
    if (!fitsLayer(o, l)) continue;
    const opt = document.createElement("option");
    opt.value = l.id; opt.textContent = l.name;
    ls.appendChild(opt);
  }
  ls.value = o.layer;
  const active = document.activeElement;
  for (const [id, v] of [["#p-x", o.x], ["#p-y", o.y], ["#p-w", o.w], ["#p-h", o.h], ["#p-rot", o.rot || 0]]) {
    if ($(id) !== active) $(id).value = r1(v);
  }
  const isImg = o.type === "image";
  $("#image-props").classList.toggle("hidden", !isImg);
  if (isImg) {
    $("#p-invert").checked = !!o.invert;
    for (const k of ["brightness", "contrast"]) {
      $(`#p-${k}`).value = o[k] || 0;
      $(`#v-${k}`).textContent = o[k] || 0;
    }
  }
  const isText = !!o.text;
  $("#text-props").classList.toggle("hidden", !isText);
  if (isText) {
    if ($("#p-text") !== active) $("#p-text").value = o.text.text;
    $("#p-font").value = o.text.font;
    if ($("#p-size") !== active) $("#p-size").value = o.text.size;
    $("#p-align").value = o.text.align || "left";
  }
}

// ----------------------------------------------------------------- objects
function addObject(o) {
  checkpoint();
  o.id = uid();
  o.layer ||= currentLayer;
  if (!fitsLayer(o, layerById(o.layer))) {
    const l = design.layers.find((x) => fitsLayer(o, x));
    if (l) o.layer = l.id;
    else {
      const nl = newLayer(design.layers.length, { name: "Shapes", mode: "line" });
      design.layers.push(nl);
      o.layer = nl.id;
    }
  }
  o.rot ||= 0;
  design.objects.push(o);
  selectedId = o.id;
  changed({ layers: true });
  return o;
}

async function refreshText(o, keepSize = false) {
  const { paths } = await api("POST", "/api/text", { text: o.text.text || " ", font: o.text.font, size: o.text.size, align: o.text.align });
  const bb = pathsBBox(paths);
  o.paths = paths;
  if (bb && !keepSize) { o.w = r3(bb[2] - bb[0]); o.h = r3(bb[3] - bb[1]); }
}

async function addText(x, y) {
  const o = { type: "path", x, y, w: 0, h: 0, paths: [],
    text: { text: "Text", font: fonts.default, size: 10, align: "left" } };
  try { await refreshText(o); } catch { return; }
  addObject(o);
  setTool("select");
  $("#p-text").focus();
  $("#p-text").select();
}

function deleteSelected() {
  if (!selected()) return;
  checkpoint();
  design.objects = design.objects.filter((o) => o.id !== selectedId);
  selectedId = null;
  changed({ layers: true });
}

function duplicateSelected() {
  const o = selected();
  if (!o) return;
  const copy = JSON.parse(JSON.stringify(o));
  copy.x += 5; copy.y += 5;
  addObject(copy);
}

// --------------------------------------------------------------- snapping
function objectBBox(o) {
  if (o.type === "image" || o.type === "rect") {
    const c = boxCorners(o);
    return [Math.min(...c.map((p) => p[0])), Math.min(...c.map((p) => p[1])),
            Math.max(...c.map((p) => p[0])), Math.max(...c.map((p) => p[1]))];
  }
  return pathsBBox(objectPolylines(o));
}

function snapTargets(excludeId) {
  const xs = [0, bed.w / 2, bed.w], ys = [0, bed.h / 2, bed.h];
  if (snap.objects) {
    for (const o of design.objects) {
      if (o.id === excludeId) continue;
      const b = objectBBox(o);
      if (!b) continue;
      xs.push(b[0], (b[0] + b[2]) / 2, b[2]);
      ys.push(b[1], (b[1] + b[3]) / 2, b[3]);
    }
  }
  return { xs, ys };
}

// Best shift (within tolerance) that lines one of `vals` up with a target.
function nearest(vals, targets, tol) {
  let best = null;
  for (const v of vals) for (const t of targets) {
    const d = t - v;
    if (Math.abs(d) <= tol && (!best || Math.abs(d) < Math.abs(best.d))) best = { d, t };
  }
  return best;
}

const snapOff = (e) => e.altKey;
const gridRound = (v) => Math.round(v / snap.size) * snap.size;

function snapPoint(p, e, excludeId) {
  const guides = [];
  if (snapOff(e)) return { p, guides };
  let [x, y] = p;
  const tol = 8 / view.zoom;
  const t = snap.objects ? snapTargets(excludeId) : null;
  const nx = t && nearest([x], t.xs, tol), ny = t && nearest([y], t.ys, tol);
  if (nx) { x = nx.t; guides.push({ x: nx.t }); } else if (snap.grid) x = gridRound(x);
  if (ny) { y = ny.t; guides.push({ y: ny.t }); } else if (snap.grid) y = gridRound(y);
  return { p: [x, y], guides };
}

// Snap a whole-object move: line up edges/centres, else put the bbox corner on the grid.
function snapMove(dx, dy, e) {
  const guides = [];
  if (snapOff(e) || !drag.bbox) return { dx, dy, guides };
  const [bx0, by0, bx1, by1] = drag.bbox;
  const tol = 8 / view.zoom;
  const xs = [bx0 + dx, (bx0 + bx1) / 2 + dx, bx1 + dx];
  const ys = [by0 + dy, (by0 + by1) / 2 + dy, by1 + dy];
  const nx = snap.objects && nearest(xs, drag.targets.xs, tol);
  const ny = snap.objects && nearest(ys, drag.targets.ys, tol);
  if (nx) { dx += nx.d; guides.push({ x: nx.t }); } else if (snap.grid) dx = gridRound(bx0 + dx) - bx0;
  if (ny) { dy += ny.d; guides.push({ y: ny.t }); } else if (snap.grid) dy = gridRound(by0 + dy) - by0;
  return { dx, dy, guides };
}

function bindSnapUI() {
  $("#snap-grid").checked = snap.grid;
  $("#snap-objects").checked = snap.objects;
  $("#snap-size").value = String(snap.size);
  if (!$("#snap-size").value) $("#snap-size").value = "1";
  const save = () => {
    snap.grid = $("#snap-grid").checked;
    snap.objects = $("#snap-objects").checked;
    snap.size = parseFloat($("#snap-size").value);
    try { localStorage.setItem("laser-server.snap", JSON.stringify(snap)); } catch {}
  };
  ["#snap-grid", "#snap-objects", "#snap-size"].forEach((id) => $(id).addEventListener("change", save));
  save();
}

// ----------------------------------------------------------------- rulers
function rulerStep() {
  const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
  const major = steps.find((s) => s * view.zoom >= 55) || 1000;
  const lead = major / Math.pow(10, Math.floor(Math.log10(major) + 1e-9));
  let minor = major / (lead === 2 ? 4 : 5);
  if (minor * view.zoom < 4) minor = major / 2;
  return { major, minor };
}

function prepCanvas(c) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
  }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function drawRulers() {
  const top = $("#ruler-top"), left = $("#ruler-left");
  if (!top.clientWidth) return;
  const { major, minor } = rulerStep();
  const dec = major < 1 ? 1 : 0;
  const sel = selected() ? objectBBox(selected()) : null;
  const css = getComputedStyle(document.documentElement);
  const dim = css.getPropertyValue("--dim").trim() || "#8c96a3";
  const accent = css.getPropertyValue("--accent").trim() || "#ff7a1a";

  // top (X)
  let { ctx, w, h } = prepCanvas(top);
  if (sel) {
    ctx.fillStyle = "rgba(255,122,26,.18)";
    ctx.fillRect(view.panX + sel[0] * view.zoom, 0, (sel[2] - sel[0]) * view.zoom, h);
  }
  ctx.strokeStyle = dim; ctx.fillStyle = dim; ctx.font = "10px system-ui, sans-serif"; ctx.textBaseline = "top";
  ctx.beginPath();
  const x0 = Math.floor(-view.panX / view.zoom / minor) * minor;
  for (let x = x0; view.panX + x * view.zoom <= w; x += minor) {
    const sx = Math.round(view.panX + x * view.zoom) + 0.5;
    const isMajor = Math.abs(x / major - Math.round(x / major)) < 1e-6;
    ctx.moveTo(sx, h); ctx.lineTo(sx, h - (isMajor ? 12 : 5));
    if (isMajor) ctx.fillText((Math.round(x * 10) / 10).toFixed(dec), sx + 3, 2);
  }
  ctx.stroke();
  if (cursorWorld) {
    ctx.strokeStyle = accent; ctx.beginPath();
    const cx = Math.round(view.panX + cursorWorld[0] * view.zoom) + 0.5;
    ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke();
  }

  // left (Y, up)
  ({ ctx, w, h } = prepCanvas(left));
  if (sel) {
    ctx.fillStyle = "rgba(255,122,26,.18)";
    ctx.fillRect(0, view.panY - sel[3] * view.zoom, w, (sel[3] - sel[1]) * view.zoom);
  }
  ctx.strokeStyle = dim; ctx.fillStyle = dim; ctx.font = "10px system-ui, sans-serif"; ctx.textBaseline = "top";
  ctx.beginPath();
  const yTop = view.panY / view.zoom;
  const y0 = Math.floor((view.panY - h) / view.zoom / minor) * minor;
  for (let y = y0; y <= yTop + minor; y += minor) {
    const sy = Math.round(view.panY - y * view.zoom) + 0.5;
    const isMajor = Math.abs(y / major - Math.round(y / major)) < 1e-6;
    ctx.moveTo(w, sy); ctx.lineTo(w - (isMajor ? 12 : 5), sy);
    if (isMajor) {
      ctx.save(); ctx.translate(2, sy - 3); ctx.rotate(-Math.PI / 2);
      ctx.fillText((Math.round(y * 10) / 10).toFixed(dec), 0, 0); ctx.restore();
    }
  }
  ctx.stroke();
  if (cursorWorld) {
    ctx.strokeStyle = accent; ctx.beginPath();
    const cy = Math.round(view.panY - cursorWorld[1] * view.zoom) + 0.5;
    ctx.moveTo(0, cy); ctx.lineTo(w, cy); ctx.stroke();
  }
}

// ------------------------------------------------------------------ tools
function setTool(t) {
  tool = t;
  document.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === t));
  svg.style.cursor = t === "select" ? "default" : "crosshair";
}

svg.addEventListener("pointerdown", (e) => {
  const p = toWorld(e);
  svg.setPointerCapture(e.pointerId);
  if (e.button === 1 || spaceDown) {
    drag = { mode: "pan", sx: e.clientX, sy: e.clientY, px: view.panX, py: view.panY };
    return;
  }
  if (e.button !== 0) return;

  if (tool === "text") { const sp = snapPoint(p, e).p; addText(r1(sp[0]), r1(sp[1])); return; }
  if (tool !== "select") { drag = { mode: "draw", tool, start: snapPoint(p, e).p, cur: null }; return; }

  const handle = e.target.closest("[data-handle]");
  const o = selected();
  if (handle && o) {
    checkpoint();
    const orig = { ...o };
    if (handle.dataset.handle === "rot") {
      drag = { mode: "rotate", orig, moved: false };
    } else {
      const [hu, hv] = handle.dataset.handle.split(",").map(Number);
      const au = 1 - hu, av = 1 - hv;
      const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
      const anchor = rotateAbout(o.x + au * o.w, o.y + av * o.h, cx, cy, rad(o.rot || 0));
      drag = { mode: "resize", orig, hu, hv, au, av, anchor, moved: false, guides: [] };
    }
    return;
  }
  const hit = e.target.closest("[data-id]");
  if (hit) {
    selectedId = hit.dataset.id;
    const so = selected();
    currentLayer = so.layer;
    checkpoint();
    drag = { mode: "move", start: p, ox: so.x, oy: so.y, moved: false,
      bbox: objectBBox(so), targets: snapTargets(so.id), guides: [] };
    renderLayers();
    renderProps();
    renderOverlay();
    return;
  }
  selectedId = null;
  renderProps();
  renderOverlay();
  drag = { mode: "pan", sx: e.clientX, sy: e.clientY, px: view.panX, py: view.panY };
});

svg.addEventListener("pointermove", (e) => {
  let p = toWorld(e);
  cursorWorld = p;
  $("#coords").textContent = `X ${p[0].toFixed(1)}  Y ${p[1].toFixed(1)} mm`;
  if (!drag) { drawRulers(); return; }
  if (drag.mode === "draw" || drag.mode === "resize") {
    const sp = snapPoint(p, e, selectedId);
    p = sp.p;
    drag.guides = sp.guides;
  }
  const o = selected();
  if (drag.mode === "pan") {
    view.panX = drag.px + (e.clientX - drag.sx);
    view.panY = drag.py + (e.clientY - drag.sy);
    applyView();
  } else if (drag.mode === "draw") {
    drag.cur = p;
    renderOverlay();
  } else if (drag.mode === "move" && o) {
    let dx = p[0] - drag.start[0], dy = p[1] - drag.start[1];
    if (e.shiftKey) { if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0; }
    const sm = snapMove(dx, dy, e);
    if (e.shiftKey) { if (dx === 0) sm.dx = 0; if (dy === 0) sm.dy = 0; }
    drag.guides = sm.guides;
    o.x = r3(drag.ox + sm.dx);
    o.y = r3(drag.oy + sm.dy);
    drag.moved = true;
    renderObjects();
    renderProps();
  } else if (drag.mode === "rotate" && o) {
    const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
    let a = deg(Math.atan2(p[1] - cy, p[0] - cx)) - 90;
    a = e.shiftKey ? Math.round(a / 15) * 15 : Math.round(a);
    if (snap.grid && !snapOff(e) && !e.shiftKey) a = Math.round(a);
    o.rot = ((a % 360) + 540) % 360 - 180;
    drag.moved = true;
    renderObjects();
    renderProps();
  } else if (drag.mode === "resize" && o) {
    const a = rad(drag.orig.rot || 0);
    const u = [Math.cos(a), Math.sin(a)], v = [-Math.sin(a), Math.cos(a)];
    const d = [p[0] - drag.anchor[0], p[1] - drag.anchor[1]];
    let w = Math.max(0.1, (drag.hu - drag.au) * (d[0] * u[0] + d[1] * u[1]));
    let h = Math.max(0.1, (drag.hv - drag.av) * (d[0] * v[0] + d[1] * v[1]));
    const lock = $("#p-lock").checked !== e.shiftKey;
    if (lock && drag.orig.w > 0.01 && drag.orig.h > 0.01) {
      const k = Math.max(w / drag.orig.w, h / drag.orig.h);
      w = drag.orig.w * k; h = drag.orig.h * k;
    }
    if (drag.orig.h < 0.01) h = drag.orig.h;
    if (drag.orig.w < 0.01) w = drag.orig.w;
    const c = [drag.anchor[0] + u[0] * (0.5 - drag.au) * w + v[0] * (0.5 - drag.av) * h,
               drag.anchor[1] + u[1] * (0.5 - drag.au) * w + v[1] * (0.5 - drag.av) * h];
    o.w = r3(w); o.h = r3(h);
    o.x = r3(c[0] - w / 2); o.y = r3(c[1] - h / 2);
    drag.moved = true;
    renderObjects();
    renderProps();
  }
});

function endDrag() {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (d.mode === "draw") {
    const [x0, y0] = d.start;
    const [x1, y1] = d.cur || d.start;
    const small = Math.hypot(x1 - x0, y1 - y0) < 1;
    if (d.tool === "line") {
      if (small) { renderOverlay(); return; }
      addObject({ type: "path", x: r1(Math.min(x0, x1)), y: r1(Math.min(y0, y1)),
        w: r1(Math.abs(x1 - x0)), h: r1(Math.abs(y1 - y0)), paths: [[[x0, y0], [x1, y1]]] });
    } else {
      const w = small ? 20 : Math.abs(x1 - x0), h = small ? 20 : Math.abs(y1 - y0);
      addObject({ type: d.tool, x: r1(small ? x0 : Math.min(x0, x1)), y: r1(small ? y0 : Math.min(y0, y1)), w: r1(w), h: r1(h) });
    }
    setTool("select");
    return;
  }
  if (["move", "rotate", "resize"].includes(d.mode)) {
    if (d.moved) changed(); else undoStack.pop();
  }
  renderOverlay();
}
svg.addEventListener("pointerup", endDrag);
svg.addEventListener("pointerleave", () => { cursorWorld = null; drawRulers(); });
svg.addEventListener("pointercancel", endDrag);

svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = svg.getBoundingClientRect();
  const sx = e.clientX - r.left, sy = e.clientY - r.top;
  const [wx, wy] = toWorld(e);
  view.zoom = Math.min(200, Math.max(0.05, view.zoom * Math.exp(-e.deltaY * 0.0015)));
  view.panX = sx - wx * view.zoom;
  view.panY = sy + wy * view.zoom;
  applyView();
}, { passive: false });

document.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));

// --------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  const typing = e.target.matches("input, textarea, select");
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); saveDesign(); return; }
  if (typing) return;
  if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSelected(); return; }
  if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelected(); return; }
  if (e.key === "Escape") { selectedId = null; setTool("select"); renderProps(); renderOverlay(); return; }
  if (e.key === " ") { spaceDown = true; svg.style.cursor = "grab"; e.preventDefault(); return; }
  const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
  const o = selected();
  if (arrows[e.key] && o) {
    e.preventDefault();
    const step = e.shiftKey ? 10 : 1;
    checkpoint();
    o.x = r1(o.x + arrows[e.key][0] * step);
    o.y = r1(o.y + arrows[e.key][1] * step);
    changed();
    return;
  }
  if (!mod && e.key.toLowerCase() === "i") { $("#import-image").click(); return; }
  const keys = { v: "select", r: "rect", e: "ellipse", l: "line", t: "text" };
  if (!mod && keys[e.key.toLowerCase()]) setTool(keys[e.key.toLowerCase()]);
});
document.addEventListener("keyup", (e) => {
  if (e.key === " ") { spaceDown = false; setTool(tool); }
});

// ------------------------------------------------------------ properties
function bindNumber(id, apply) {
  $(id).addEventListener("change", () => {
    const o = selected();
    const v = parseFloat($(id).value);
    if (!o || !Number.isFinite(v)) return renderProps();
    checkpoint();
    apply(o, v);
    changed();
  });
}
bindNumber("#p-x", (o, v) => (o.x = v));
bindNumber("#p-y", (o, v) => (o.y = v));
bindNumber("#p-rot", (o, v) => (o.rot = ((v % 360) + 540) % 360 - 180));
bindNumber("#p-w", (o, v) => {
  v = Math.max(0.1, v);
  if ($("#p-lock").checked && o.w > 0.01) o.h = r3(o.h * (v / o.w));
  o.w = v;
});
bindNumber("#p-h", (o, v) => {
  v = Math.max(0.1, v);
  if ($("#p-lock").checked && o.h > 0.01) o.w = r3(o.w * (v / o.h));
  o.h = v;
});
$("#p-layer").addEventListener("change", () => {
  const o = selected();
  if (!o) return;
  checkpoint();
  o.layer = currentLayer = $("#p-layer").value;
  changed({ layers: true });
});

async function textChanged(mutate) {
  const o = selected();
  if (!o?.text) return;
  const before = JSON.stringify(design);
  mutate(o.text);
  try {
    await refreshText(o);
    undoStack.push(before);
    redoStack = [];
    changed();
  } catch {
    design = JSON.parse(before);
    afterLoad();
    changed();
  }
}
let textTimer = null;
$("#p-text").addEventListener("input", () => {
  clearTimeout(textTimer);
  textTimer = setTimeout(() => textChanged((t) => (t.text = $("#p-text").value)), 350);
});
$("#p-font").addEventListener("change", () => textChanged((t) => (t.font = $("#p-font").value)));
$("#p-size").addEventListener("change", () => {
  const v = parseFloat($("#p-size").value);
  if (v > 0) textChanged((t) => (t.size = v));
});
$("#p-align").addEventListener("change", () => textChanged((t) => (t.align = $("#p-align").value)));

$("#p-invert").addEventListener("change", () => {
  const o = selected();
  if (!o) return;
  checkpoint();
  o.invert = $("#p-invert").checked;
  changed();
});
for (const k of ["brightness", "contrast"]) {
  $(`#p-${k}`).addEventListener("input", () => { $(`#v-${k}`).textContent = $(`#p-${k}`).value; });
  $(`#p-${k}`).addEventListener("change", () => {
    const o = selected();
    if (!o) return;
    checkpoint();
    o[k] = parseInt($(`#p-${k}`).value, 10);
    changed();
  });
}

$("#btn-del").onclick = deleteSelected;
$("#btn-dup").onclick = duplicateSelected;
$("#btn-center").onclick = () => {
  const o = selected();
  if (!o) return;
  checkpoint();
  o.x = r1((bed.w - o.w) / 2);
  o.y = r1((bed.h - o.h) / 2);
  changed();
};
$("#btn-undo").onclick = undo;
$("#btn-redo").onclick = redo;
$("#btn-fit").onclick = fitView;
$("#btn-add-layer").onclick = () => {
  checkpoint();
  const l = newLayer(design.layers.length);
  design.layers.push(l);
  currentLayer = expandedLayer = l.id;
  changed({ layers: true });
};
$("#design-name").addEventListener("change", () => {
  design.name = $("#design-name").value.trim() || "untitled";
  changed();
});

// ---------------------------------------------------------------- preview
function schedulePreview() {
  if (!previewOn) return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 400);
}

let previewSeq = 0;
async function runPreview() {
  const g = $("#toolpaths");
  const seq = ++previewSeq;
  const stats = $("#stats");
  stats.classList.remove("hidden");
  const spin = setTimeout(() => { if (seq === previewSeq) stats.innerHTML = `<span class="spinner"></span>Updating preview…`; }, 300);
  let res;
  try { res = await api("POST", "/api/design/preview", design); } catch { clearTimeout(spin); stats.textContent = "Preview failed"; return; }
  clearTimeout(spin);
  if (!previewOn || seq !== previewSeq) return;
  g.innerHTML = "";
  for (const tp of res.toolpaths) {
    if (tp.paths.length) el("path", { d: polyD(tp.paths), stroke: tp.color }, g);
    for (const im of tp.images || []) {
      const flip = el("g", { transform: `translate(${im.x} ${im.y + im.h}) scale(1 -1)` }, g);
      const node = el("image", { width: im.w, height: im.h, preserveAspectRatio: "none" }, flip);
      node.setAttribute("href", im.href);
    }
  }
  if (!res.bounds) { stats.textContent = "Nothing to burn (no objects on enabled layers)"; return; }
  const [x0, y0, x1, y1] = res.bounds;
  const outside = x0 < -0.01 || y0 < -0.01 || x1 > bed.w + 0.01 || y1 > bed.h + 0.01;
  stats.innerHTML = `Est. time <b>${fmtTime(res.est_seconds)}</b> · burn ${(res.cut_mm / 1000).toFixed(2)} m · ` +
    `${(x1 - x0).toFixed(1)} × ${(y1 - y0).toFixed(1)} mm` +
    (outside ? ` · <span style="color:#ff8b8b">extends outside the bed!</span>` : "");
}

$("#btn-preview").onclick = () => {
  previewOn = !previewOn;
  $("#btn-preview").classList.toggle("active", previewOn);
  $(".stage-wrap").classList.toggle("previewing", previewOn);
  if (previewOn) runPreview();
  else { $("#toolpaths").innerHTML = ""; $("#stats").classList.add("hidden"); }
};

// ------------------------------------------------------------ save / open
async function saveDesign() {
  design.name = $("#design-name").value.trim() || "untitled";
  try {
    const r = await api("PUT", `/api/designs/${encodeURIComponent(design.name)}`, design);
    design.name = r.name;
    $("#design-name").value = r.name;
    dirty = false;
    toast(`Saved design "${r.name}"`, "info");
  } catch {}
}
$("#btn-save").onclick = saveDesign;

$("#btn-new").onclick = () => {
  if (dirty && design.objects.length && !confirm("Discard unsaved changes?")) return;
  checkpoint();
  design = defaultDesign();
  selectedId = null;
  afterLoad();
  changed({ layers: true });
  dirty = false;
};

$("#btn-open").onclick = async () => {
  let list = [];
  try { list = await api("GET", "/api/designs"); } catch { return; }
  const tbody = $("#design-list tbody");
  tbody.innerHTML = list.length ? "" : `<tr><td class="dim">No saved designs yet.</td></tr>`;
  for (const d of list) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><div></div><div class="small dim">${new Date(d.modified * 1000).toLocaleString()}</div></td>
      <td class="actions"><button class="open">Open</button> <button class="danger del">Delete</button></td>`;
    tr.querySelector("div").textContent = d.name;
    tr.querySelector(".open").onclick = async () => {
      if (dirty && design.objects.length && !confirm("Discard unsaved changes?")) return;
      try {
        const loaded = await api("GET", `/api/designs/${encodeURIComponent(d.name)}`);
        checkpoint();
        design = loaded;
        selectedId = null;
        afterLoad();
        changed({ layers: true });
        dirty = false;
        $("#open-dialog").close();
      } catch {}
    };
    tr.querySelector(".del").onclick = async () => {
      if (!confirm(`Delete design "${d.name}"?`)) return;
      try { await api("DELETE", `/api/designs/${encodeURIComponent(d.name)}`); tr.remove(); } catch {}
    };
    tbody.appendChild(tr);
  }
  $("#open-dialog").showModal();
};
$("#btn-close-open").onclick = () => $("#open-dialog").close();

// ----------------------------------------------------------------- import
$("#import-svg").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api("POST", "/api/import/svg", fd);
    let { w, h } = r;
    // Scale down to fit the bed if needed.
    const k = Math.min(1, (bed.w * 0.9) / (w || 1), (bed.h * 0.9) / (h || 1));
    w *= k; h *= k;
    addObject({ type: "path", paths: r.paths, w: r3(w), h: r3(h), x: r1((bed.w - w) / 2), y: r1((bed.h - h) / 2) });
    toast(`Imported ${r.name} (${w.toFixed(1)} × ${h.toFixed(1)} mm${k < 1 ? ", scaled to fit" : ""})`, "info");
  } catch {}
});

function imageLayer() {
  const cur = layerById(currentLayer);
  if (isImageLayer(cur)) return cur.id;
  const l = design.layers.find(isImageLayer);
  if (l) return l.id;
  const nl = newLayer(design.layers.length, { name: "Image", mode: "image", power: 60, min_power: 0,
    speed: 6000, hatch: 0.1, dither: "floyd", overscan: 2, bidirectional: true });
  design.layers.push(nl);
  return nl.id;
}

$("#import-image").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  toast(`Uploading ${file.name}…`, "info");
  try {
    const r = await api("POST", "/api/images", fd);
    let w = r.mm_width || 100;
    let h = r.mm_height || (100 * r.px_height) / r.px_width;
    const k = Math.min(1, (bed.w * 0.9) / w, (bed.h * 0.9) / h);
    w *= k; h *= k;
    const before = JSON.stringify(design);
    const layer = imageLayer();
    addObject({ type: "image", image: r.image || r.id, layer, w: r3(w), h: r3(h),
      x: r1((bed.w - w) / 2), y: r1((bed.h - h) / 2), invert: false, brightness: 0, contrast: 0 });
    undoStack[undoStack.length - 1] = before;
    currentLayer = layer;
    renderLayers();
    toast(`Imported ${r.name} (${w.toFixed(1)} × ${h.toFixed(1)} mm). Image settings are on the "${layerById(layer).name}" layer.`, "info");
  } catch {}
});

// ------------------------------------------------------------- make G-code
$("#btn-send").onclick = async () => {
  const btn = $("#btn-send");
  if (btn.disabled) return;
  design.name = $("#design-name").value.trim() || "untitled";
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Generating…`;
  document.body.classList.add("busy");
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    btn.innerHTML = `<span class="spinner"></span>Generating… ${s}s`;
  }, 1000);
  try {
    await api("PUT", `/api/designs/${encodeURIComponent(design.name)}`, design);
    dirty = false;
    const r = await api("POST", "/api/design/gcode", { design, filename: design.name });
    toast(`Created <b>${r.name}</b> · est. ${fmtTime(r.est_seconds)} <a href="/">Go to Control to frame &amp; run →</a>`, "info", true);
  } catch {} finally {
    clearInterval(tick);
    btn.disabled = false;
    btn.innerHTML = label;
    document.body.classList.remove("busy");
  }
};

// ----------------------------------------------------------- test pattern
const TEST_KEY = "laser-server.testPattern";
const TEST_DEFAULTS = { mode: "fill", power_min: 10, power_max: 100, power_steps: 5, speed_min: 20, speed_max: 100,
  speed_steps: 5, cell: 10, gap: 2, passes: 1, interval: 0.1, x: 10, y: 10, labels: true, label_power: 20, label_speed: 40 };
let testTimer = null;
let testSeq = 0;

function testParams() {
  const f = $("#test-form");
  const p = {};
  for (const [k, def] of Object.entries(TEST_DEFAULTS)) {
    const inp = f.elements[k];
    if (typeof def === "boolean") p[k] = inp.checked;
    else if (typeof def === "number") { const v = parseFloat(inp.value); p[k] = Number.isFinite(v) ? v : def; }
    else p[k] = inp.value;
  }
  p.power_steps = Math.round(p.power_steps);
  p.speed_steps = Math.round(p.speed_steps);
  p.passes = Math.round(p.passes);
  return p;
}

function loadTestForm() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(TEST_KEY) || "{}"); } catch {}
  const p = { ...TEST_DEFAULTS, ...saved };
  const f = $("#test-form");
  for (const [k, v] of Object.entries(p)) {
    const inp = f.elements[k];
    if (!inp) continue;
    if (inp.type === "checkbox") inp.checked = !!v; else inp.value = v;
  }
  $("#label-opts").classList.toggle("hidden", !p.labels);
}

function clearTestPreview() { $("#testpreview").innerHTML = ""; }

async function updateTestPreview() {
  const p = testParams();
  try { localStorage.setItem(TEST_KEY, JSON.stringify(p)); } catch {}
  $("#label-opts").classList.toggle("hidden", !p.labels);
  const seq = ++testSeq;
  const sum = $("#test-summary");
  sum.innerHTML = `<span class="spinner"></span>Calculating…`;
  let r;
  try { r = await api("POST", "/api/test-pattern/preview", p, { quiet: true }); }
  catch (e) { if (seq === testSeq) sum.textContent = e.message; return; }
  if (seq !== testSeq || !$("#test-dialog").open) return;
  const g = $("#testpreview");
  g.innerHTML = "";
  for (const tp of r.toolpaths) if (tp.paths.length) el("path", { d: polyD(tp.paths), stroke: tp.color }, g);
  if (r.bounds) {
    const [x0, y0, x1, y1] = r.bounds;
    el("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }, g);
    const outside = x0 < 0 || y0 < 0 || x1 > bed.w || y1 > bed.h;
    sum.innerHTML = `Size <b>${(x1 - x0).toFixed(1)} × ${(y1 - y0).toFixed(1)} mm</b> · ${p.power_steps * p.speed_steps} squares · ` +
      `est. <b>${fmtTime(r.est_seconds)}</b>` + (outside ? `<br><span style="color:#ff8b8b">Extends outside the bed — move or shrink it.</span>` : "");
  }
}

function scheduleTestPreview() {
  clearTimeout(testTimer);
  testTimer = setTimeout(updateTestPreview, 300);
}

$("#btn-test").onclick = () => {
  const dlg = $("#test-dialog");
  if (dlg.open) { dlg.close(); return; }
  loadTestForm();
  dlg.show();
  updateTestPreview();
};
$("#test-dialog").addEventListener("close", () => { clearTestPreview(); testSeq++; });
$("#btn-test-close").onclick = () => $("#test-dialog").close();
$("#test-form").addEventListener("input", scheduleTestPreview);
$("#test-form").addEventListener("change", scheduleTestPreview);
$("#test-form").addEventListener("submit", (e) => e.preventDefault());

$("#btn-test-make").onclick = async () => {
  const btn = $("#btn-test-make");
  if (btn.disabled) return;
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Generating…`;
  try {
    const r = await api("POST", "/api/test-pattern/gcode", testParams());
    toast(`Created <b>${r.name}</b> (${r.size[0].toFixed(0)} × ${r.size[1].toFixed(0)} mm, est. ${fmtTime(r.est_seconds)}) ` +
          `<a href="/">Go to Control to frame &amp; run →</a>`, "info", true);
  } catch {} finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
};

$("#btn-test-preset").onclick = () => {
  const p = testParams();
  openPresetDialog({ settings: { mode: p.mode, power: p.power_max, speed: fromMmS(p.speed_min), passes: p.passes, hatch: p.interval } });
  toast("Set the power and speed of the square you liked, then save.", "info");
};

$("#btn-test-open").onclick = async () => {
  if (dirty && design.objects.length && !confirm("Replace the current design? Unsaved changes will be lost.")) return;
  try {
    const d = await api("POST", "/api/test-pattern/design", testParams());
    delete d.size; delete d.extent;
    checkpoint();
    design = d;
    selectedId = null;
    afterLoad();
    changed({ layers: true });
    $("#test-dialog").close();
    toast("Test pattern opened as a design. Each square is its own layer.", "info");
  } catch {}
};

// -------------------------------------------------------------------- init
async function init() {
  await loadPresets();
  try { machine = await api("GET", "/api/machine"); bed = { w: machine.bed_width, h: machine.bed_height }; } catch {}
  try { fonts = await api("GET", "/api/fonts"); } catch {}
  const fs = $("#p-font");
  for (const f of fonts.fonts) {
    const opt = document.createElement("option");
    opt.value = opt.textContent = f;
    fs.appendChild(opt);
  }
  if (machine) {
    const note = $("#machine-note");
    note.textContent = `Bed ${bed.w} × ${bed.h} mm · 100% power = S${machine.s_max}`;
    if (machine.laser_mode_known && !machine.laser_mode) {
      note.innerHTML += `<br><span style="color:#ffb3b3">GRBL laser mode ($32) is off. Jobs will switch the laser off for every travel move, which is slower. Set $32=1 for diode lasers.</span>`;
    }
  }
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch {}
  design = saved && saved.layers ? saved : defaultDesign();
  afterLoad();
  renderBed();
  setTool("select");
  bindSnapUI();
  renderLayers();
  renderObjects();
  renderProps();
  fitView();
  dirty = false;
}
window.addEventListener("resize", () => applyView());
new ResizeObserver(() => drawRulers()).observe($("#ruler-top"));
init();
