"use strict";
// Handwriting capture page: one prompt at a time, ruled guides, pen strokes with pressure and time.

const $ = (id) => document.getElementById(id);
const pad = $("pad"), ctx = pad.getContext("2d");
const INK = () => getComputedStyle(document.documentElement).getPropertyValue("--ink").trim() || "#000";
const GUIDE = () => getComputedStyle(document.documentElement).getPropertyValue("--guide").trim() || "#ccc";
const GUIDE2 = () => getComputedStyle(document.documentElement).getPropertyValue("--guide-strong").trim() || "#999";

let prompts = [], done = {}, order = [], cur = 0;
let tBase = 0, strokes = [], live = null, t0 = null, dirty = false, sawPen = false, pointerKind = "";
let xh = 48, guide = null;

function store(k, v) { try { localStorage.setItem("hw." + k, String(v)); } catch (e) { /* private mode */ } }
function recall(k) { try { return localStorage.getItem("hw." + k); } catch (e) { return null; } }

function say(text, bad) { const m = $("msg"); m.textContent = text; m.className = bad ? "err" : ""; }

// ---- layout: a ruled line per text line, sized from the x-height setting
function layout() {
  const p = order[cur];
  const lines = p ? (p.lines || 1) : 1;
  const asc = xh * 1.0, desc = xh * 0.8, gap = xh * 0.7;
  const pitch = asc + xh + desc + gap;
  const w = Math.max(320, pad.parentElement.clientWidth - 34);
  const h = Math.round(lines * pitch + gap);
  const dpr = window.devicePixelRatio || 1;
  pad.style.width = w + "px"; pad.style.height = h + "px";
  pad.width = Math.round(w * dpr); pad.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const baselines = [];
  for (let i = 0; i < lines; i++) baselines.push(Math.round(gap / 2 + asc + xh + i * pitch));
  guide = { x_height: xh, ascender: asc, descender: desc, pitch, baselines, left: 24 };
  redraw();
}

function redraw() {
  const w = pad.clientWidth, h = pad.clientHeight;
  ctx.clearRect(0, 0, w, h);
  if (guide) {
    ctx.lineWidth = 1;
    for (const b of guide.baselines) {
      ctx.strokeStyle = GUIDE(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(0, b - guide.x_height - guide.ascender); ctx.lineTo(w, b - guide.x_height - guide.ascender); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, b + guide.descender); ctx.lineTo(w, b + guide.descender); ctx.stroke();
      ctx.strokeStyle = GUIDE2(); ctx.setLineDash([6, 6]);
      ctx.beginPath(); ctx.moveTo(0, b - guide.x_height); ctx.lineTo(w, b - guide.x_height); ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(0, b); ctx.lineTo(w, b); ctx.stroke();
      ctx.lineWidth = 1;
    }
    ctx.strokeStyle = GUIDE2();
    ctx.beginPath(); ctx.moveTo(guide.left, 0); ctx.lineTo(guide.left, h); ctx.stroke();
  }
  ctx.strokeStyle = INK(); ctx.fillStyle = INK(); ctx.lineCap = "round"; ctx.lineJoin = "round";
  for (const s of strokes) drawStroke(s);
}

function drawStroke(s) {
  if (s.length === 1) {
    ctx.beginPath(); ctx.arc(s[0][0], s[0][1], 1.2 + 2.2 * s[0][3], 0, 6.2832); ctx.fill(); return;
  }
  for (let i = 1; i < s.length; i++) {
    ctx.lineWidth = 1.2 + 3.2 * (s[i][3] + s[i - 1][3]) / 2;
    ctx.beginPath(); ctx.moveTo(s[i - 1][0], s[i - 1][1]); ctx.lineTo(s[i][0], s[i][1]); ctx.stroke();
  }
}

// ---- pen input
function accept(e) {
  if (e.pointerType === "pen") { sawPen = true; return true; }
  if (e.pointerType === "touch") return !(sawPen || $("penonly").checked);
  return e.button === 0; // mouse
}

function addPoint(e) {
  const r = pad.getBoundingClientRect();
  if (t0 === null) t0 = e.timeStamp - tBase;
  const pr = e.pointerType === "mouse" ? 0.5 : (e.pressure > 0 ? e.pressure : 0.05);
  const pt = [+(e.clientX - r.left).toFixed(2), +(e.clientY - r.top).toFixed(2), +(e.timeStamp - t0).toFixed(1), +pr.toFixed(3)];
  const last = live[live.length - 1];
  if (last && last[0] === pt[0] && last[1] === pt[1]) return;
  live.push(pt);
  if (live.length > 1) { const prev = live[live.length - 2]; drawStroke([prev, pt]); }
}

pad.addEventListener("pointerdown", (e) => {
  if (!accept(e)) return;
  e.preventDefault();
  pad.setPointerCapture(e.pointerId);
  pointerKind = e.pointerType;
  live = []; strokes.push(live); dirty = true;
  addPoint(e);
  if (live.length === 1) { ctx.beginPath(); ctx.arc(live[0][0], live[0][1], 1.2 + 2.2 * live[0][3], 0, 6.2832); ctx.fill(); }
});
pad.addEventListener("pointermove", (e) => {
  if (!live) return;
  e.preventDefault();
  const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
  for (const c of (evs.length ? evs : [e])) addPoint(c);
});
function end(e) {
  if (!live) return;
  live = null;
  say("");
}
pad.addEventListener("pointerup", end);
pad.addEventListener("pointercancel", end);
pad.addEventListener("contextmenu", (e) => e.preventDefault());

// ---- prompts and progress
function sectionList() { return [...new Set(prompts.map((p) => p.section))]; }

function buildOrder() {
  const sec = $("section").value;
  order = prompts.filter((p) => sec === "all" || p.section === sec);
}

function showProgress() {
  const n = order.filter((p) => done[p.id]).length;
  $("progress").textContent = `${cur + 1} of ${order.length}  (${n} saved in this section, ${Object.keys(done).length} of ${prompts.length} overall)`;
}

async function show(i) {
  if (dirty && !confirm("This prompt has unsaved writing. Leave it?")) return;
  cur = Math.max(0, Math.min(order.length - 1, i));
  const p = order[cur];
  strokes = []; live = null; t0 = null; tBase = 0; dirty = false;
  $("prompt").textContent = p.text;
  $("note").textContent = (done[p.id] ? "Already saved. Writing again replaces it. " : "") + (p.note || "");
  $("prev").disabled = cur === 0;
  layout();
  showProgress();
  store("last", p.id);
  if (done[p.id]) {
    try {
      const r = await fetch(BASE + "/api/sample/" + p.id);
      if (r.ok && order[cur] === p) {
        const s = await r.json();
        strokes = s.strokes; t0 = null; redraw();
        const lastS = strokes[strokes.length - 1]; tBase = lastS ? lastS[lastS.length - 1][2] + 500 : 0;
      }
    } catch (e) { /* offline: leave blank */ }
  }
}

function nextUnsaved(from) {
  for (let k = 1; k <= order.length; k++) {
    const j = (from + k) % order.length;
    if (!done[order[j].id]) return j;
  }
  return Math.min(order.length - 1, from + 1);
}

async function save() {
  const p = order[cur];
  if (!strokes.length) { say("Nothing written yet.", true); return; }
  $("save").disabled = true;
  try {
    const r = await fetch(BASE + "/api/sample/" + p.id, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pointer: pointerKind || (sawPen ? "pen" : ""), strokes, guide,
                              canvas: { w: pad.clientWidth, h: pad.clientHeight, dpr: window.devicePixelRatio || 1 } }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    done[p.id] = { strokes: strokes.length };
    dirty = false;
    say("Saved.");
    await show(nextUnsaved(cur));
  } catch (e) {
    say("Save failed: " + e.message, true);
  } finally {
    $("save").disabled = false;
  }
}

$("save").onclick = save;
$("skip").onclick = () => show(cur + 1);
$("prev").onclick = () => show(cur - 1);
$("undo").onclick = () => { strokes.pop(); dirty = strokes.length > 0 || dirty; redraw(); };
$("clear").onclick = () => { if (!strokes.length || confirm("Clear this prompt?")) { strokes = []; dirty = false; t0 = null; tBase = 0; redraw(); } };
$("size").oninput = () => {
  xh = +$("size").value; $("sizeout").textContent = xh + " px"; store("xh", xh);
  if (!strokes.length) layout();
};
$("size").onchange = () => { if (strokes.length) say("Size applies from the next prompt; the strokes here keep their size."); };
$("section").onchange = () => { store("section", $("section").value); buildOrder(); cur = 0; cur = nextUnsaved(-1); show(cur); };
window.addEventListener("resize", () => { if (!strokes.length) layout(); else redraw(); });
window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
  if (e.key === "Enter") { e.preventDefault(); save(); }
  else if ((e.ctrlKey || e.metaKey) && e.key === "z") { e.preventDefault(); $("undo").click(); }
});

const BASE = (location.pathname.match(/^\/w\/[^/]+/) || [""])[0];     // /w/<writer> when several people share one server

async function init() {
  if (BASE) document.title += " - " + BASE.slice(3);
  const saved = +recall("xh"); if (saved >= 28 && saved <= 90) xh = saved;
  $("size").value = xh; $("sizeout").textContent = xh + " px";
  try {
    prompts = (await (await fetch(BASE + "/api/prompts")).json()).prompts;
    done = await (await fetch(BASE + "/api/samples")).json();
  } catch (e) { say("Cannot reach the server.", true); return; }
  const sel = $("section");
  for (const s of ["all", ...sectionList()]) {
    const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o);
  }
  const rs = recall("section"); if (rs && [...sel.options].some((o) => o.value === rs)) sel.value = rs;
  buildOrder();
  const want = new URLSearchParams(location.search).get("id");   // deep link: ?id=w012
  const wantAt = order.findIndex((p) => p.id === want);
  const lastId = recall("last"); const at = order.findIndex((p) => p.id === lastId);
  await show(wantAt >= 0 ? wantAt : (at >= 0 && !done[lastId] ? at : Math.max(0, nextUnsaved(-1))));
}
init();
