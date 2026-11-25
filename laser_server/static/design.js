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

// ----------------------------------------------------------------- helpers
const uid = () => Math.random().toString(36).slice(2, 10);
const r1 = (v) => Math.round(v * 10) / 10;
const r3 = (v) => Math.round(v * 1000) / 1000;
const deg = (r) => (r * 180) / Math.PI;
const rad = (d) => (d * Math.PI) / 180;

function toast(msg, kind = "error", html = false) {
  const t = $("#toast");
  if (html) t.innerHTML = msg; else t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), html ? 8000 : 4000);
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : `${res.status} ${res.statusText}`;
    toast(msg);
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
  if (o.type === "rect") return [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]];
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
  if (drag?.mode === "draw" && drag.cur) {
    const [x0, y0] = drag.start, [x1, y1] = drag.cur;
    if (drag.tool === "line") el("line", { x1: x0, y1: y0, x2: x1, y2: y1, class: "rubber" }, g);
    else if (drag.tool === "ellipse")
      el("ellipse", { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, rx: Math.abs(x1 - x0) / 2, ry: Math.abs(y1 - y0) / 2, class: "rubber" }, g);
    else el("rect", { x: Math.min(x0, x1), y: Math.min(y0, y1), width: Math.abs(x1 - x0), height: Math.abs(y1 - y0), class: "rubber" }, g);
  }
}

function layerSummary(l) {
  const mode = { line: "Line", fill: "Fill", "fill+line": "Fill+Line" }[l.mode];
  return `${mode} · ${l.power}% · ${l.speed} · ${l.passes}×`;
}

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
      body.innerHTML = `
        <label class="full">Name <input data-k="name" type="text"></label>
        <div class="grid2">
          <label>Mode <select data-k="mode">
            <option value="line">Line (cut/score)</option><option value="fill">Fill (engrave)</option>
            <option value="fill+line">Fill + Line</option></select></label>
          <label>Power % <input data-k="power" type="number" min="0" max="100" step="1"></label>
          <label>Speed mm/min <input data-k="speed" type="number" min="1" step="50"></label>
          <label>Passes <input data-k="passes" type="number" min="1" max="100" step="1"></label>
          ${fill ? `<label>Line interval mm <input data-k="hatch" type="number" min="0.01" step="0.01"></label>
          <label>Fill angle° <input data-k="angle" type="number" step="15"></label>` : ""}
        </div>
        <div class="row"><button class="small-btn danger del">Delete layer</button></div>`;
      body.querySelectorAll("[data-k]").forEach((inp) => {
        inp.value = l[inp.dataset.k];
        inp.addEventListener("change", () => {
          const k = inp.dataset.k;
          let v = inp.type === "number" ? parseFloat(inp.value) : inp.value;
          if (inp.type === "number" && !Number.isFinite(v)) { inp.value = l[k]; return; }
          if (k === "power") v = Math.min(100, Math.max(0, v));
          if (k === "passes") v = Math.min(100, Math.max(1, Math.round(v)));
          if (k === "speed") v = Math.max(1, v);
          if (k === "hatch") v = Math.max(0.01, v);
          checkpoint();
          l[k] = v;
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
    const opt = document.createElement("option");
    opt.value = l.id; opt.textContent = l.name;
    ls.appendChild(opt);
  }
  ls.value = o.layer;
  const active = document.activeElement;
  for (const [id, v] of [["#p-x", o.x], ["#p-y", o.y], ["#p-w", o.w], ["#p-h", o.h], ["#p-rot", o.rot || 0]]) {
    if ($(id) !== active) $(id).value = r1(v);
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

  if (tool === "text") { addText(r1(p[0]), r1(p[1])); return; }
  if (tool !== "select") { drag = { mode: "draw", tool, start: p, cur: null }; return; }

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
      drag = { mode: "resize", orig, hu, hv, au, av, anchor, moved: false };
    }
    return;
  }
  const hit = e.target.closest("[data-id]");
  if (hit) {
    selectedId = hit.dataset.id;
    const so = selected();
    currentLayer = so.layer;
    checkpoint();
    drag = { mode: "move", start: p, ox: so.x, oy: so.y, moved: false };
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
  const p = toWorld(e);
  $("#coords").textContent = `X ${p[0].toFixed(1)}  Y ${p[1].toFixed(1)} mm`;
  if (!drag) return;
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
    o.x = r1(drag.ox + dx);
    o.y = r1(drag.oy + dy);
    drag.moved = true;
    renderObjects();
    renderProps();
  } else if (drag.mode === "rotate" && o) {
    const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
    let a = deg(Math.atan2(p[1] - cy, p[0] - cx)) - 90;
    a = e.shiftKey ? Math.round(a / 15) * 15 : Math.round(a);
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
}
svg.addEventListener("pointerup", endDrag);
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

async function runPreview() {
  const g = $("#toolpaths");
  let res;
  try { res = await api("POST", "/api/design/preview", design); } catch { return; }
  if (!previewOn) return;
  g.innerHTML = "";
  for (const tp of res.toolpaths) el("path", { d: polyD(tp.paths), stroke: tp.color }, g);
  const stats = $("#stats");
  stats.classList.remove("hidden");
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

// ------------------------------------------------------------- make G-code
$("#btn-send").onclick = async () => {
  design.name = $("#design-name").value.trim() || "untitled";
  try {
    await api("PUT", `/api/designs/${encodeURIComponent(design.name)}`, design);
    dirty = false;
    const r = await api("POST", "/api/design/gcode", { design, filename: design.name });
    toast(`Created <b>${r.name}</b> · est. ${fmtTime(r.est_seconds)} <a href="/">Go to Control to frame &amp; run →</a>`, "info", true);
  } catch {}
};

// -------------------------------------------------------------------- init
async function init() {
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
  renderLayers();
  renderObjects();
  renderProps();
  fitView();
  dirty = false;
}
window.addEventListener("resize", () => applyView());
init();
