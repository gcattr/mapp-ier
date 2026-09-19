/*
 * test_console.js — offline harness for map2model-console.html
 *
 * `node --check` only validates syntax, so a call to a function that no longer
 * exists passes cleanly. That has bitten this file twice. This harness stubs
 * the DOM, Leaflet and three.js, evaluates the console's inline script for
 * real, and then INVOKES the top-level functions.
 *
 *     node test_console.js
 *
 * Exit code 0 = every check passed. No network, no browser, no dependencies.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0;
const fails = [];
function check(name, fn) {
  try { fn(); pass++; console.log('  ok   ' + name); }
  catch (e) { fails.push(name); console.log('  FAIL ' + name + '\n         ' + e.message); }
}
function eq(a, b, what) {
  if (a !== b) throw new Error((what || 'value') + ': expected ' + b + ', got ' + a);
}
function ok(c, what) { if (!c) throw new Error(what || 'expected truthy'); }

/* ---------------- DOM stub ---------------- */
function makeEl(id) {
  const el = {
    id, value: '', textContent: '', innerHTML: '', checked: false,
    style: {}, dataset: {}, clientWidth: 640, clientHeight: 420,
    listeners: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c))
                                       : (on ? this._s.add(c) : this._s.delete(c)); },
    },
    addEventListener(t, f) { (el.listeners[t] = el.listeners[t] || []).push(f); },
    removeEventListener() {},
    dispatch(t, ev) { (el.listeners[t] || []).forEach(f => f.call(el, ev || {})); },
    click() { el.dispatch('click', {}); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    setAttribute(k, v) { el[k] = String(v); },
    getAttribute(k) { return el[k] === undefined ? null : el[k]; },
    appendChild() {}, remove() {}, focus() {}, blur() {},
    getContext() { return {}; },
    getBoundingClientRect() { return {left:0, top:0, right:640, bottom:420, width:640, height:420}; },
    scrollIntoView() {},
    offsetWidth: 380, offsetHeight: 300,
    setPointerCapture() {},
  };
  return el;
}
const els = {};
const document = {
  _els: els,
  getElementById(id) { return (els[id] = els[id] || makeEl(id)); },
  querySelector(s) { return document.getElementById(String(s).replace(/^#/, '')); },
  querySelectorAll() { return []; },
  createElement(t) { return makeEl('created:' + t); },
  addEventListener(t, f) { (document._l = document._l || {}), (document._l[t] = document._l[t] || []).push(f); },
  dispatch(t, ev) { ((document._l || {})[t] || []).forEach(f => f(ev || {})); },
  body: makeEl('body'),
};

/* ---------------- Leaflet stub ---------------- */
const mapLayers = new Set();
function latlng(lat, lng) { return { lat, lng }; }
const leafletMap = {
  handlers: {},
  setView() { return leafletMap; },
  on(t, f) { (leafletMap.handlers[t] = leafletMap.handlers[t] || []).push(f); },
  fire(t, ev) { (leafletMap.handlers[t] || []).forEach(f => f(ev)); },
  removeLayer(l) { mapLayers.delete(l); },
  addLayer(l) { mapLayers.add(l); },
  fitBounds() {}, getZoom() { return 14; }, invalidateSize() {},
  dragging: { enable() {}, disable() {} },
};
function layer() {
  const o = {
    _bounds: null, _latlngs: null, _latlng: {lat:0,lng:0}, _handlers: {},
    addTo(m) { m.addLayer ? m.addLayer(o) : mapLayers.add(o); return o; },
    setBounds(b) { o._bounds = b; return o; },
    getBounds() { return o._bounds || [[0,0],[0,0]]; },
    setLatLngs(p) { o._latlngs = p; return o; },
    setLatLng(ll) { o._latlng = ll; return o; },
    getLatLng() { return o._latlng; },
    getElement() { return o._el || (o._el = makeEl('layer-el')); },
    setStyle() { return o; }, remove() { mapLayers.delete(o); },
    on(t, f) { (o._handlers[t] = o._handlers[t] || []).push(f); return o; },
    fire(t, ev) { (o._handlers[t] || []).forEach(f => f(ev)); },
  };
  return o;
}
const L = {
  map() { return leafletMap; },
  tileLayer() { return layer(); },
  rectangle() { return layer(); },
  polygon() { return layer(); },
  polyline() { return layer(); },
  circle() { return layer(); },
  marker() { return layer(); },
  divIcon(opts) { return opts; },
  latLng: latlng,
  // the zoom control is added by hand so it does not land on the search box
  control: { zoom(opts) { return { opts, addTo() { return this; } }; } },
  DomEvent: { stopPropagation() {} },
};

/* ---------------- three.js stub ---------------- */
/* Color carries the real sRGB->linear transfer function so the colour-pipeline
   check below is a genuine test, not a tautology. */
class Color {
  constructor(hex) {
    if (typeof hex === 'string') hex = parseInt(hex.replace('#', ''), 16);
    hex = hex || 0;
    this.r = ((hex >> 16) & 255) / 255;
    this.g = ((hex >> 8) & 255) / 255;
    this.b = (hex & 255) / 255;
  }
  _s2l(c) { return c < 0.04045 ? c * 0.0773993808 : Math.pow(c * 0.9478672986 + 0.0521327014, 2.4); }
  convertSRGBToLinear() {
    this.r = this._s2l(this.r); this.g = this._s2l(this.g); this.b = this._s2l(this.b);
    return this;
  }
  getHex() {
    const f = c => Math.round(Math.min(1, Math.max(0, c)) * 255);
    return (f(this.r) << 16) | (f(this.g) << 8) | f(this.b);
  }
}
function node() {
  return { children: [], position: { set() {} }, rotation: { x: 0, y: 0, z: 0 },
           add(c) { this.children.push(c); }, remove(c) {
             const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); } };
}
const materials = [];
const THREE = {
  WebGLRenderer: function () {
    return { setPixelRatio() {}, setSize() {}, render() {}, domElement: makeEl('gl'),
             outputEncoding: null, toneMapping: null, toneMappingExposure: 1 };
  },
  Scene: node, Group: node,
  PerspectiveCamera: function () {
    return { position: { set() {} }, lookAt() {}, updateProjectionMatrix() {}, aspect: 1 };
  },
  HemisphereLight: node, AmbientLight: node, DirectionalLight: node,
  Vector3: function () { return { set() {}, x: 0, y: 0, z: 0 }; },
  Color,
  BufferGeometry: function () {
    return { setAttribute() {}, setIndex() {}, addGroup() {}, computeVertexNormals() {},
             dispose() {}, groups: [] };
  },
  BufferAttribute: function () { return {}; },
  Mesh: function (g, m) { const n = node(); n.geometry = g; n.material = m; return n; },
  MeshPhongMaterial: function (o) { materials.push(o); return Object.assign({ dispose() {} }, o); },
  DoubleSide: 2, FrontSide: 0,
  sRGBEncoding: 3001, LinearEncoding: 3000,
  ACESFilmicToneMapping: 4, NoToneMapping: 0,
};

/* ---------------- load the console's inline script ---------------- */
// `node test_console.js [other-console.html]` — the argument lets you point the
// harness at an older copy to confirm a check really does catch the old bug.
const target = process.argv[2] || path.join(__dirname, 'map2model-console.html');
const html = fs.readFileSync(target, 'utf8');
const blocks = html.match(/<script>([\s\S]*?)<\/script>/g) || [];
if (!blocks.length) { console.error('no inline <script> found'); process.exit(1); }
const src = blocks.map(b => b.replace(/^<script>/, '').replace(/<\/script>$/, '')).join('\n');

/* vm.runInContext puts `function` and `var` declarations on the sandbox global,
   but NOT top-level `const`/`let` — and much of this console is written as
   `const name = (…) => …`. Re-export those explicitly so the harness can reach
   them; the list is derived from the source, so a renamed function shows up
   here as a load error rather than silently going untested. */
const topConsts = [...new Set([...src.matchAll(/^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=/gm)]
  .map(m => m[1]))];
const srcWithExports = src + ';\nvar __C = {' + topConsts.join(', ') + '};\n';

const sandbox = {
  document, L, THREE, console,
  window: null, navigator: { clipboard: { writeText: async () => {} }, userAgent: 'node' },
  location: { protocol: 'http:', host: 'localhost:8000' },
  devicePixelRatio: 1,
  performance: { now: () => Date.now() },
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: f => setTimeout(f, 0),
  cancelAnimationFrame: t => clearTimeout(t),
  matchMedia: () => ({ matches: false }),
  addEventListener() {}, removeEventListener() {},
  fetch: async () => ({ ok: false, json: async () => ({}) }),
  AbortController, URLSearchParams, JSZip: function () {},
  alert() {}, Math, Date, JSON,
  // Real enough to test "shown once" against. store=null simulates a browser
  // that throws on access (private mode, blocked site data), which the console
  // has to survive rather than fail to load.
  localStorage: {
    store: {},
    getItem(k) { if (!this.store) throw new Error('blocked'); return this.store[k] || null; },
    setItem(k, v) { if (!this.store) throw new Error('blocked'); this.store[k] = String(v); },
    removeItem(k) { if (!this.store) throw new Error('blocked'); delete this.store[k]; },
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

console.log('evaluating console script (' + src.length + ' chars)…');
try {
  vm.runInContext(srcWithExports, sandbox, { filename: 'map2model-console.html' });
} catch (e) {
  console.error('THREW at top level: ' + e.message + '\n' + e.stack);
  process.exit(1);
}
console.log('top level evaluated cleanly\n');

/* One lookup table: `function` declarations live on the sandbox global,
   `const` arrows come back through G. */
const G = new Proxy(sandbox, {
  get: (t, k) => (k in t ? t[k] : (t.__C || {})[k]),
  has: (t, k) => k in t || k in (t.__C || {}),
});

/* ---------------- 1. every top-level function is callable ---------------- */
console.log('every top-level function actually resolves:');
const declared = [...src.matchAll(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/gm)].map(m => m[1]);
check('declared functions are all defined (' + declared.length + ')', () => {
  const missing = declared.filter(n => typeof G[n] !== 'function');
  ok(!missing.length, 'not defined: ' + missing.join(', '));
});
check('const-declared helpers resolve too (' + topConsts.length + ')', () => {
  const missing = topConsts.filter(n => typeof G[n] === 'undefined');
  ok(!missing.length, 'not defined: ' + missing.join(', '));
});
check('no function calls a name that no longer exists', () => {
  /* The real check the doc asks for: INVOKE everything. `node --check` happily
     accepts a call to a deleted function; only running it catches that. Any
     other exception (a missing DOM stub, bad arguments) is not our concern —
     we fail only on a name that cannot be resolved at all. */
  const broken = [];
  for (const n of declared.concat(topConsts)) {
    const f = G[n];
    if (typeof f !== 'function') continue;
    try {
      const r = f();
      if (r && typeof r.catch === 'function') r.catch(e => {
        if (/is not defined|is not a function/.test(String(e && e.message)))
          broken.push(n + ': ' + e.message);
      });
    } catch (e) {
      if (/is not defined|is not a function/.test(String(e.message)))
        broken.push(n + ': ' + e.message);
    }
  }
  ok(!broken.length, 'dead call(s):\n         ' + broken.join('\n         '));
});

/* ---------------- 2. the elapsed clock ---------------- */
console.log('\nelapsed clock does not leak between previews:');
check('startClock returns a generation and stopClock is generation-aware', () => {
  eq(typeof G.startClock, 'function', 'startClock');
  eq(typeof G.stopClock, 'function', 'stopClock');
  const g1 = G.startClock();
  ok(typeof g1 === 'number', 'startClock must return a generation token');
  const g2 = G.startClock();                    // a NEW preview starts
  ok(g2 !== g1, 'a new build must take a new generation');
  G.stopClock(g1);                              // the OLD build unwinds
  ok(G.isCurrent(g2), 'the new build must still be current');
  G.stopClock(g2);
});
check('a stale stopClock leaves the running clock alone', () => {
  const g1 = G.startClock();
  const g2 = G.startClock();
  G.stopClock(g1);                              // stale cleanup
  const el = document.getElementById('elapsed');
  el.textContent = 'stale';
  return new Promise(() => {}), (() => {
    // the interval for g2 must still be live: prove it by ticking it manually
    ok(G.isCurrent(g2), 'generation g2 must remain current after a stale stop');
    G.stopClock(g2);
    ok(true);
  })();
});
check('a new preview resets elapsed to ~0 rather than continuing the old count', () => {
  const g1 = G.startClock();
  const before = G.elapsed();
  const g2 = G.startClock();
  const after = G.elapsed();
  ok(after <= before + 0.01, 'elapsed did not reset: ' + before + ' -> ' + after);
  G.stopClock(g2);
});

/* ---------------- the per-layer build status ---------------- */
console.log('\nthe build preview shows each layer\'s state:');
check('parseLayers reads [layer] lines, survives Ticker \\r spam, and accumulates', () => {
  eq(typeof G.parseLayers, 'function', 'parseLayers');
  const acc = {};
  // poll 1: elevation loading, three layers announced fetching
  G.parseLayers([
    '[  0.0s] -> load terrain',
    '   downloading 6 elevation tiles: 3/6 (50.0%)  eta 1s',
    '[layer] water: fetching',
    '[layer] buildings: fetching',
    '[layer] roads: fetching',
  ], acc);
  eq(acc.water.state, 'now', 'water should be fetching');
  eq(acc.elevation.state, 'now', 'elevation should be loading');
  eq(acc.elevation.det, '3/6', 'elevation progress not picked up');
  // poll 2: the water "done" line is buried at the tail of a Ticker progress line
  G.parseLayers([
    'draping roads: 4/12 ( 33%)  eta 0.0s   [layer] water: 26 rows in 12s',
    '[layer] buildings: 1266 rows in 71s',
  ], acc);
  eq(acc.water.state, 'done', 'a [layer] line mid-Ticker-spam was missed');
  ok(/26 rows/.test(acc.water.det), 'water detail lost: ' + acc.water.det);
  eq(acc.buildings.state, 'done', 'buildings not marked done');
  // poll 3: roads never reported done, but meshing has started -> infer done
  eq(acc.roads.state, 'now', 'roads should still be fetching before meshing');
  G.parseLayers(['[ 80.1s] -> mesh buildings'], acc);
  eq(acc.roads.state, 'done', 'meshing started but roads still shows fetching');
  // an empty layer reads as empty, not done
  G.parseLayers(['[layer] land_cover: empty in 5s'], acc);
  eq(acc.land_cover.state, 'empty', 'an empty layer should read empty');
  const rows = G.layerRows(acc);
  ok(rows.length >= 4 && rows.every(r => r.label && r.state), 'layerRows shape wrong');
  eq(rows[0].label, 'elevation', 'elevation should sort first');
});

/* ---------------- 3. the draw-a-tile ghost rectangle ---------------- */
console.log('\ndragging a tile never leaves a second box behind:');
function layersOnMap() { return mapLayers.size; }
check('clearGhost and finishDraw exist', () => {
  eq(typeof G.clearGhost, 'function', 'clearGhost');
  eq(typeof G.finishDraw, 'function', 'finishDraw');
});
check('a drag released ON the map leaves no ghost', () => {
  const base = layersOnMap();
  document.getElementById('btnDraw').click();            // enter draw mode
  leafletMap.fire('mousedown', { latlng: latlng(43.64, -79.39) });
  leafletMap.fire('mousemove', { latlng: latlng(43.65, -79.38) });
  eq(layersOnMap(), base + 1, 'ghost should be on the map mid-drag');
  leafletMap.fire('mouseup', { latlng: latlng(43.65, -79.38) });
  eq(layersOnMap(), base, 'ghost should be gone after mouseup');
});
check('a drag released OFF the map leaves no ghost (the two-box bug)', () => {
  const base = layersOnMap();
  document.getElementById('btnDraw').click();
  leafletMap.fire('mousedown', { latlng: latlng(43.64, -79.39) });
  leafletMap.fire('mousemove', { latlng: latlng(43.65, -79.38) });
  document.dispatch('mouseup', {});                      // released outside the map
  eq(layersOnMap(), base, 'ghost survived a release outside the map');
});
check('starting a second drag never stacks two ghosts', () => {
  const base = layersOnMap();
  document.getElementById('btnDraw').click();
  leafletMap.fire('mousedown', { latlng: latlng(43.64, -79.39) });
  leafletMap.fire('mousemove', { latlng: latlng(43.65, -79.38) });
  // the user wanders off the map and comes back without ever releasing on it
  leafletMap.fire('mousedown', { latlng: latlng(43.60, -79.40) });
  leafletMap.fire('mousemove', { latlng: latlng(43.61, -79.39) });
  eq(layersOnMap(), base + 1, 'a second mousedown stacked another ghost');
  leafletMap.fire('mouseup', { latlng: latlng(43.61, -79.39) });
  eq(layersOnMap(), base, 'ghost left behind after the second drag');
});

/* ---------------- 4. the colour pipeline ---------------- */
console.log('\npreview colours are not washed out:');
check('the renderer writes sRGB', () => {
  const g = G.glInit();
  eq(g.renderer.outputEncoding, THREE.sRGBEncoding, 'renderer.outputEncoding');
});
check('FIL() converts a filament hex into linear space', () => {
  eq(typeof G.FIL, 'function', 'FIL');
  const raw = new Color('#61C680');
  const lin = G.FIL('#61C680');
  ok(lin.r < raw.r && lin.g < raw.g && lin.b < raw.b,
     'FIL must darken toward linear, got ' + JSON.stringify(lin));
  // green must stay the dominant channel: the hue is preserved, not greyed
  ok(lin.g > lin.r && lin.g > lin.b, 'FIL flattened the hue');
});
check('every preview material colour goes through FIL', () => {
  materials.length = 0;
  G.buildExactScene([
    { name: 'terrain',   positions: [0,0,0, 10,0,0, 10,10,0], indices: [0,1,2], size: [10,10,1] },
    { name: 'buildings', positions: [0,0,0, 5,0,0, 5,5,9],    indices: [0,1,2], size: [5,5,9] },
    { name: 'water',     positions: [0,0,0, 8,0,0, 8,8,0],    indices: [0,1,2], size: [8,8,1] },
  ]);
  ok(materials.length >= 3, 'expected materials, got ' + materials.length);
  const raw = materials.filter(m => typeof m.color === 'string' || typeof m.color === 'number');
  ok(!raw.length, raw.length + ' material(s) took a raw hex instead of FIL()');
  materials.forEach(m => ok(m.color instanceof Color, 'material colour is not a THREE.Color'));
});
check('every preview material draws both faces', () => {
  // Exporter winding is not canonical (GEOS vertex order shifts between
  // builds), so a Mac render with the default FrontSide culled the outward
  // faces and the model looked inside-out. Every buildExactScene material
  // must set side: DoubleSide.
  materials.length = 0;
  G.buildExactScene([
    { name: 'terrain',   positions: [0,0,0, 10,0,0, 10,10,0], indices: [0,1,2], size: [10,10,1] },
    { name: 'buildings', positions: [0,0,0, 5,0,0, 5,5,9],    indices: [0,1,2], size: [5,5,9] },
    { name: 'water',     positions: [0,0,0, 8,0,0, 8,8,0],    indices: [0,1,2], size: [8,8,1] },
  ]);
  ok(materials.length >= 3, 'expected materials, got ' + materials.length);
  const flat = materials.filter(m => m.side !== THREE.DoubleSide);
  ok(!flat.length, flat.length + ' material(s) left at FrontSide (inside-out on Mac)');
});

/* ---------------- 4b. building a second preview ---------------- */
console.log('\npreviewing twice in a row does not throw:');
check('a second build disposes the water mesh (array of materials) cleanly', () => {
  const scene = () => G.buildExactScene([
    { name: 'terrain', positions: [0,0,0, 10,0,0, 10,10,0], indices: [0,1,2], size: [10,10,1] },
    { name: 'water',   positions: [0,0,0, 8,0,0, 8,8,0],    indices: [0,1,2], size: [8,8,1] },
  ]);
  scene();
  scene();   // the dispose loop runs over the previous scene's meshes
  scene();
});

/* ---------------- 5. the cover is measured, not drawn ---------------- */
console.log('\nthe cover plate is measured against the build volume, never drawn:');
function sceneWithCover(coverZ) {
  materials.length = 0;
  const g = G.glInit();
  const before = g.root.children.length;
  G.buildExactScene([
    { name: 'terrain',    positions: [0,0,0, 100,0,0, 100,100,0], indices: [0,1,2], size: [100,100,2] },
    { name: 'buildings',  positions: [0,0,0, 20,0,0, 20,20,40],   indices: [0,1,2], size: [20,20,40] },
    { name: 'cover_left', positions: [0,0,0, 120,0,0, 120,120,coverZ],
      indices: [0,1,2], size: [120,120,coverZ] },
    { name: 'cover_right',positions: [0,0,0, 120,0,0, 120,120,coverZ],
      indices: [0,1,2], size: [120,120,coverZ] },
  ]);
  return { g, before };
}
check('cover layers are not added to the scene', () => {
  const { g } = sceneWithCover(60);
  eq(g.root.children.length, 2, 'meshes drawn (terrain + buildings expected)');
});
check('cover height does not become "tallest"', () => {
  sceneWithCover(240);
  const hud = document.getElementById('prevHud').innerHTML;
  ok(/tallest 40\.0 mm/.test(hud), 'tallest came from the cover, not the model: ' + hud);
});
check('an over-tall cover trips the build-volume warning', () => {
  sceneWithCover(260);                       // past the 250 mm ceiling
  const note = document.getElementById('sizeNote');
  ok(/will not print/i.test(note.innerHTML),
     'no warning for a 260 mm cover: ' + note.innerHTML);
  ok(/cover/i.test(note.innerHTML), 'the warning does not name the cover');
});
check('a cover genuinely near the ceiling warns without blocking', () => {
  sceneWithCover(238);                       // past 0.92*250, under 250
  const note = document.getElementById('sizeNote');
  ok(/close to the limit/i.test(note.innerHTML),
     'no near-limit warning for a 238 mm cover: ' + note.innerHTML);
});
check('a normal cube cover does not warn', () => {
  // ~219 mm is the cube cover for a 200 mm tile - normal, not near-limit.
  document.getElementById('sizeNote').innerHTML = '';
  sceneWithCover(219);
  const note = document.getElementById('sizeNote');
  ok(!/close to the limit/i.test(note.innerHTML),
     'a 219 mm cube cover was flagged as near-limit: ' + note.innerHTML);
});
check('a comfortable cover produces no warning at all', () => {
  document.getElementById('sizeNote').innerHTML = '';
  sceneWithCover(90);
  const note = document.getElementById('sizeNote');
  ok(!/will not print|close to the limit/i.test(note.innerHTML),
     'spurious warning for a 90 mm cover: ' + note.innerHTML);
});

/* ---------------- filaments ----------------
   The console's stock list and map2model.py's FILAMENTS are two copies of one
   table. A buyer can only pick what the console offers, and the exporter can
   only fill what it knows, so a drift between them is an order that cannot be
   produced -- silently, because --filaments would reject the key only when the
   seller finally runs the command, days after the sale. Parse the Python and
   compare. */
function pythonFilaments() {
  const py = fs.readFileSync(path.join(__dirname, 'map2model.py'), 'utf8');
  const block = py.slice(py.indexOf('FILAMENTS = {'));
  const body = block.slice(0, block.indexOf('\n}'));
  const out = {};
  for (const m of body.matchAll(
      /"([a-z0-9_]+)":\s*\("([^"]+)",\s*"([A-Z]+)",\s*"([A-Z0-9]+)",\s*"(#[0-9A-Fa-f]{6})"\)/g)) {
    out[m[1]] = { nm: m[2], type: m[3], id: m[4], hex: m[5].toUpperCase() };
  }
  return out;
}
function pythonDefaults() {
  const py = fs.readFileSync(path.join(__dirname, 'map2model.py'), 'utf8');
  const block = py.slice(py.indexOf('DEFAULT_FILAMENTS = {'));
  const body = block.slice(0, block.indexOf('\n}'));
  const out = {};
  for (const m of body.matchAll(/"([a-z_]+)":\s*"([a-z0-9_]+)"/g)) out[m[1]] = m[2];
  return out;
}

check('the console stocks exactly the filaments the exporter knows', () => {
  const py = pythonFilaments();
  const js = G.FILAMENTS;
  ok(js, 'the console has no FILAMENTS table');
  // petg_cover is the shipping shell. It is deliberately NOT offered to a
  // buyer, so it is the one key the exporter has and the console must not.
  const pyKeys = Object.keys(py).filter(k => k !== 'petg_cover').sort();
  const jsKeys = Object.keys(js).sort();
  eq(jsKeys.join(','), pyKeys.join(','), 'stock lists differ');
  ok(pyKeys.length >= 18, 'expected the full 18-colour range, got ' + pyKeys.length);
});

check('every hex matches Bambu\'s table on both sides', () => {
  const py = pythonFilaments();
  for (const [k, f] of Object.entries(G.FILAMENTS)) {
    eq(f.hex.toUpperCase(), py[k].hex, 'hex differs for ' + k);
  }
});

check('the console defaults are the exporter defaults', () => {
  const py = pythonDefaults();
  for (const [layer, key] of Object.entries(G.DEFAULT_FILAMENTS)) {
    eq(key, py[layer], 'default differs for ' + layer);
  }
  // the cover is the exporter's business only
  eq(py.cover, 'petg_cover', 'the cover stopped being PETG');
  ok(!('cover' in G.DEFAULT_FILAMENTS), 'the console offers the cover as a choice');
});

check('a picked filament reaches the copied command', () => {
  G.pick.buildings = 'basic_red';
  G.colFromPick();
  const cmd = G.buildCmd();
  ok(/--filaments /.test(cmd), 'no --filaments in the command: ' + cmd);
  ok(/buildings=basic_red/.test(cmd), 'the pick is missing: ' + cmd);
  // every enabled layer is named, defaults included -- the command is read by
  // a human days later and must not depend on what the exporter would guess
  ok(/terrain=matte_grass_green/.test(cmd), 'defaults are not spelled out: ' + cmd);
  G.pick.buildings = G.DEFAULT_FILAMENTS.buildings;
  G.colFromPick();
});

check('picking a filament repaints the swatch and the preview colour', () => {
  G.pick.terrain = 'matte_caramel';
  G.colFromPick();
  eq(G.col.terrain, '#AE835B', 'col did not follow pick');
  G.drawFilms();
  const html = document.getElementById('films').innerHTML;
  ok(/#AE835B/i.test(html), 'the swatch did not repaint: ' + html.slice(0, 200));
  G.pick.terrain = G.DEFAULT_FILAMENTS.terrain;
  G.colFromPick();
});

check('the picker offers no colour the shop does not stock', () => {
  G.openPickerFor('terrain');
  const html = document.getElementById('films').innerHTML;
  const offered = [...html.matchAll(/data-key="([a-z0-9_]+)"/g)].map(m => m[1]);
  eq(offered.length, Object.keys(G.FILAMENTS).length, 'not every stocked colour is offered');
  for (const key of offered) {
    ok(G.FILAMENTS[key], 'the picker offers an unstocked filament: ' + key);
  }
  G.closePicker();
});

check('every colour in the grid is shown as a colour, not just a name', () => {
  G.openPickerFor('buildings');
  const html = document.getElementById('films').innerHTML;
  for (const [key, f] of Object.entries(G.FILAMENTS)) {
    ok(html.includes('data-key="' + key + '"'), 'missing swatch for ' + key);
    ok(html.toUpperCase().includes(f.hex.toUpperCase()),
       'no colour swatch rendered for ' + key);
  }
  G.closePicker();
});

check('only one palette is open at a time', () => {
  G.openPickerFor('terrain');
  G.openPickerFor('water');
  const html = document.getElementById('films').innerHTML;
  eq((html.match(/data-key="basic_red"/g) || []).length, 1,
     'two palettes were open at once');
  G.closePicker();
});

/* ---------------- the walkthrough ---------------- */
console.log('\nthe first-visit walkthrough:');
check('there is a tour, and it never talks like a manual', () => {
  ok(Array.isArray(G.TOUR) && G.TOUR.length >= 5, 'no tour steps');
  const words = G.TOUR.map(s => s.t + ' ' + s.b).join(' ').toLowerCase();
  // an Etsy buyer does not know, and must not have to learn, any of this
  for (const jargon of ['3mf', 'filament slot', 'ams', 'exporter', 'stl',
                        'bounding box', 'lod', 'cli', 'terminal', 'command line']) {
    ok(!words.includes(jargon), 'the tour uses jargon: ' + jargon);
  }
  ok(/etsy/.test(words), 'the tour never mentions the Etsy order it exists for');
});

check('the tour says the size must match the Etsy order', () => {
  const sizeStep = G.TOUR.find(s => /size/i.test(s.t));
  ok(sizeStep, 'no step about the size');
  const flat = sizeStep.b.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
  ok(/same size you picked on etsy/i.test(flat),
     'the size step does not tell the buyer to match their order: ' + flat);
});

check('the tour warns that a preview takes minutes', () => {
  const words = G.TOUR.map(s => s.b).join(' ')
    .replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').toLowerCase();
  ok(/minute/.test(words), 'nothing warns the preview is slow');
  ok(/do not have to wait/.test(words),
     'the tour does not say the wait is optional -- buyers will think it is required');
});

check('the tour steps forward, back, and closes at the end', () => {
  G.openTour(0);
  ok(!document.getElementById('tour').classList.contains('hidden'), 'tour did not open');
  eq(document.getElementById('tourTitle').textContent, G.TOUR[0].t, 'wrong first step');
  G.tourNext();
  eq(document.getElementById('tourTitle').textContent, G.TOUR[1].t, 'next did not advance');
  G.tourBack();
  eq(document.getElementById('tourTitle').textContent, G.TOUR[0].t, 'back did not return');
  G.tourBack();                                  // must not run off the start
  eq(document.getElementById('tourTitle').textContent, G.TOUR[0].t, 'back ran past step 1');
  for (let i = 0; i < G.TOUR.length; i++) G.tourNext();
  ok(document.getElementById('tour').classList.contains('hidden'),
     'the tour never closes');
});

check('every spotlight step points at an element that exists', () => {
  // the coachmark can only be verified for real in a browser (the DOM stub
  // gives every element the same fixed rect); here we pin that each step names
  // a target the page actually has, and that placing it never throws.
  eq(typeof G.positionTour, 'function', 'positionTour');
  const fullHtml = fs.readFileSync(path.join(__dirname, 'map2model-console.html'), 'utf8');
  let withTarget = 0;
  for (const st of G.TOUR) {
    if (!('target' in st)) throw new Error('a tour step has no target field: ' + st.t);
    if (st.target == null) continue;
    withTarget++;
    ok(/^#[\w-]+$/.test(st.target), 'target is not a simple #id selector: ' + st.target);
    const id = st.target.slice(1);
    // the stub auto-creates any id, so check the real HTML source instead
    ok(fullHtml.includes('id="' + id + '"'), 'tour target not in the page HTML: ' + st.target);
  }
  ok(withTarget >= 4, 'almost every step should spotlight something: ' + withTarget);
  // stepping through must not throw while it measures and positions
  G.openTour(0);
  for (let i = 0; i < G.TOUR.length; i++) { G.positionTour(); G.tourNext(); }
  G.openTour(0); G.closeTour();
});

check('the tour is actually opened on page load', () => {
  // maybeOpenTour() can exist, be correct, and never run -- which is exactly
  // what happened. Only the startup sequence proves it is wired up.
  const startup = src.slice(src.lastIndexOf('placeRect(true)'));
  ok(/maybeOpenTour\s*\(/.test(startup),
     'maybeOpenTour is never called at startup, so no visitor ever sees the tour');
});

check('the tour opens on every visit, not just the first', () => {
  sandbox.localStorage.store = {};
  G.maybeOpenTour();
  ok(!document.getElementById('tour').classList.contains('hidden'),
     'a first-time visitor was not shown the tour');
  G.closeTour();
  ok(document.getElementById('tour').classList.contains('hidden'),
     'closing did not close it');
  // a returning visitor gets it again: they arrive from an Etsy listing months
  // apart, on whatever device is to hand
  G.maybeOpenTour();
  ok(!document.getElementById('tour').classList.contains('hidden'),
     'a returning visitor was dropped into an unexplained map');
  G.closeTour();
});

check('skip is a real button, not a grey word', () => {
  // As a borderless grey caption it did not read as a control, so the only
  // obvious way out of the card was six taps of Next.
  const css = html.slice(html.indexOf('.tournav'), html.indexOf('</style>'));
  const ghost = css.slice(css.indexOf('.tournav .ghost'));
  const rule = ghost.slice(0, ghost.indexOf('}'));
  ok(!/border-color:\s*transparent/.test(rule),
     'skip still has no border: ' + rule);
  ok(!/color:\s*var\(--ink-40\)/.test(rule),
     'skip is still greyed out: ' + rule);
});

check('the map zoom buttons do not sit on the search box', () => {
  // Leaflet defaults zoom to the top left, which is where the search box and
  // "Drag a tile" are -- the +/- landed on top of them.
  ok(/zoomControl\s*:\s*false/.test(src),
     'the default top-left zoom control is still enabled');
  const m = src.match(/L\.control\.zoom\(\{position\s*:\s*'([a-z]+)'/);
  ok(m, 'no zoom control is added back');
  ok(m[1] !== 'topleft', 'zoom was put back where the map tools are');
});

check('place search requests a shortlist instead of accepting one ambiguous match', () => {
  ok(/limit=8/.test(src), 'search still requests only one result');
  ok(/addressdetails=1/.test(src), 'search results have no location details');
  ok(/wikipedia\.org\/w\/api\.php/.test(src), 'notable landmarks have no alias-aware source');
  ok(!/centre=\{lat:\+j\[0\]/.test(src), 'search still jumps directly to the first result');
});

check('place suggestions show useful location context', () => {
  const bigBen = {
    name: 'Big Ben', type: 'clock', lat: '51.5007', lon: '-0.1246',
    display_name: 'Big Ben, Westminster, London, England, United Kingdom',
    address: {city: 'London', state: 'England', country: 'United Kingdom'},
  };
  const australian = {
    name: 'Big Ben', type: 'peak', lat: '-36.8', lon: '147.1',
    address: {municipality: 'Alpine Shire', state: 'Victoria', country: 'Australia'},
  };
  eq(G.placeContext(bigBen), 'London, England, United Kingdom', 'London context');
  eq(G.placeContext(australian), 'Alpine Shire, Victoria, Australia', 'Australia context');
  G.showSearchResults([bigBen, australian]);
  const list = document.getElementById('searchResults');
  ok(/London, England, United Kingdom/.test(list.innerHTML), 'London label missing');
  ok(/Alpine Shire, Victoria, Australia/.test(list.innerHTML), 'Australia label missing');
  eq(document.getElementById('q').getAttribute('aria-expanded'), 'true', 'list not announced');
});

check('address suggestions show the street and ask for city and region', () => {
  const nearby={name:'26',type:'building',lat:'43.86',lon:'-79.31',
    address:{house_number:'26',road:'Luzon Avenue',city:'Markham',state:'Ontario',country:'Canada'}};
  eq(G.placeTitle(nearby), '26 Luzon Avenue', 'street was hidden behind the house number');
  const hint=G.addressGuidance('30 Luzon Ave',[nearby]);
  ok(/add the city and region or country/i.test(hint), 'missing specificity advice: '+hint);
  ok(/30 Luzon Ave, Markham, Ontario/.test(hint), 'missing concrete example: '+hint);
  ok(/different parts of the world/i.test(hint), 'worldwide ambiguity is not explained');
  G.showSearchResults([nearby],hint);
  const list=document.getElementById('searchResults').innerHTML;
  ok(/26 Luzon Avenue/.test(list), 'result does not name the street');
  ok(/search-tip/.test(list), 'guidance is not shown with the suggestions');
});

check('a specific address warns when only a nearby house number is found', () => {
  const nearby={name:'26',lat:'43.86',lon:'-79.31',
    address:{house_number:'26',road:'Luzon Avenue',city:'Markham',state:'Ontario',country:'Canada'}};
  const hint=G.addressGuidance('30 Luzon Ave, Markham, Ontario',[nearby]);
  ok(/Exact house number 30 was not found/.test(hint), 'nearby match is presented as exact: '+hint);
  ok(/check the street and location/i.test(hint), 'nearby warning lacks a safety check');
});

check('a grouped building address can still contain the exact house number', () => {
  const group={address:{house_number:'26,28,30,32,34',road:'Luzon Avenue',
    city:'Markham',state:'Ontario',country:'Canada'}};
  ok(G.resultHasAddressNumber(group,'30'), '30 was missed inside the grouped building address');
  const hint=G.addressGuidance('30 Luzon Ave, Markham, Ontario',[group]);
  ok(!/was not found/.test(hint), 'an exact grouped address was incorrectly called nearby: '+hint);
});

check('choosing a suggestion is the step that moves the map', () => {
  G.showSearchResults([{name:'Big Ben',type:'clock',lat:'51.5007',lon:'-0.1246',
    address:{city:'London',state:'England',country:'United Kingdom'}}]);
  G.selectSearchResult(0);
  ok(/Centred on Big Ben.*London/.test(document.getElementById('areaHint').textContent),
     'selected location was not confirmed clearly');
  ok(document.getElementById('searchResults').classList.contains('hidden'),
     'suggestion list stayed open after a choice');
});

check('notable landmark aliases are merged ahead of generic map matches', () => {
  const wiki = G.wikipediaPlaces({query:{pages:{'1':{
    index:1,title:'Big Ben',description:'Clock bell in London, England',
    coordinates:[{lat:51.5007,lon:-0.1246}],
  }}}});
  eq(wiki.length, 1, 'Wikipedia coordinate was not converted');
  const peaks=[{name:'Big Ben',type:'peak',lat:'-29.48',lon:'151.66',
    address:{state:'New South Wales',country:'Australia'}}];
  const merged=G.mergePlaceMatches(wiki,peaks);
  eq(merged[0].lat, '51.5007', 'the notable landmark was not ranked first');
  eq(G.placeContext(merged[0]), 'Clock bell in London, England', 'landmark context lost');
  eq(merged.length, 2, 'related map matches were discarded');
});

check('a browser that blocks storage still loads the page', () => {
  sandbox.localStorage.store = null;            // every access now throws
  G.maybeOpenTour();                            // must not throw
  G.closeTour();                                // must not throw
  sandbox.localStorage.store = {};
});

/* ---------------- panes ---------------- */
console.log('\nthe three tabs:');
check('showTab drives three panes, not two', () => {
  G.showTab('print');
  ok(!document.getElementById('panePrint').classList.contains('hidden'),
     'the print-it-yourself pane did not open');
  ok(document.getElementById('paneMap').classList.contains('hidden'), 'map still shown');
  ok(document.getElementById('panePrev').classList.contains('hidden'), 'preview still shown');
  G.showTab('map');
  ok(!document.getElementById('paneMap').classList.contains('hidden'), 'map did not come back');
  ok(document.getElementById('panePrint').classList.contains('hidden'), 'print pane stuck open');
});

check('the old boolean showTab calls still work', () => {
  // buildPreview and the resize handler call showTab(true)
  G.showTab(true);
  ok(!document.getElementById('panePrev').classList.contains('hidden'), 'showTab(true)');
  G.showTab(false);
  ok(!document.getElementById('paneMap').classList.contains('hidden'), 'showTab(false)');
});

check('the customer console is City-only while Terrain and Circuit are paused', () => {
  const terrain=html.match(/<button data-mode="terrain"[^>]*>/);
  const route=html.match(/<button data-mode="route"[^>]*>/);
  ok(terrain&&/disabled/.test(terrain[0]),'Terrain button is still enabled');
  ok(route&&/disabled/.test(route[0]),'Circuit button is still enabled');
  const startup=src.slice(src.lastIndexOf('DISCLAIMER_HTML'));
  ok(!/setMode\s*\(\s*m\s*\)/.test(startup),'an old ?mode= deep-link bypasses the pause');
});

check('the FAQ contains quick fixes and Etsy support instructions', () => {
  ok(/FAQ &amp; quick fixes/.test(html),'FAQ section missing');
  ok(/The map found the wrong place/.test(html),'place-search quick fix missing');
  ok(/One layer covers most of my model/.test(html),'layer quick fix missing');
  ok(/The preview will not build/.test(html),'preview quick fix missing');
  ok(/Message me on Etsy for any issues/.test(html),'Etsy support message missing');
});

/* ---------------- the detail panel is gone but its settings are not --------- */
check('removing the Detail panel did not change what gets built', () => {
  eq(G.DETAIL.roofs, 'all', 'roof mode changed');
  eq(G.DETAIL.lod, '2', 'LOD changed');
  eq(G.DETAIL.source, 'overture', 'data source changed');
  eq(G.DETAIL.ridge, 0.98, 'ridge fraction changed');
  const cmd = G.buildCmd();
  ok(/--roofs all/.test(cmd), 'the command lost --roofs: ' + cmd);
  ok(/--max-ridge-frac 0.98/.test(cmd), 'the command lost the ridge fraction: ' + cmd);
  ok(!/--source osm|--lod 1/.test(cmd), 'the command picked up a non-default: ' + cmd);
});

check('the command caps building height at the print size', () => {
  // Building stretch must not push a skyline past the size the buyer paid
  // for, and the cube cover would grow with it. --max-building-mm == --size.
  const cmd = G.buildCmd();
  const size = (cmd.match(/--size (\d+)/) || [])[1];
  ok(size, 'no --size in the command: ' + cmd);
  ok(new RegExp('--max-building-mm ' + size + '(\\s|$)').test(cmd),
     '--max-building-mm is missing or does not match --size: ' + cmd);
});

/* ---------------- tile shapes ---------------- */
console.log('\nthe tile can be a hexagon or a circle:');
check('setShape drives the copied command and the preview params', () => {
  eq(typeof G.setShape, 'function', 'setShape');

  G.setShape('square');
  ok(!/--tile-shape/.test(G.buildCmd()), 'square must not spell out a shape');
  ok(/(^|&)shape=square(&|$)/.test(G.exactParams()), 'exactParams lost shape=square');

  G.setShape('hex');
  ok(/--tile-shape hex(\s|$)/.test(G.buildCmd()), 'hex missing from the command');
  ok(/(^|&)shape=hex(&|$)/.test(G.exactParams()), 'exactParams lost shape=hex');

  G.setShape('circle');
  ok(/--tile-shape circle(\s|$)/.test(G.buildCmd()), 'circle missing from the command');

  G.setShape('nonsense');                        // anything unknown falls back
  ok(!/--tile-shape/.test(G.buildCmd()), 'an unknown shape must fall back to square');

  G.setShape('square');                          // leave the fixture clean
});
check('the shape outline has the right corner count', () => {
  eq(typeof G.shapeLatLngs, 'function', 'shapeLatLngs');
  G.setShape('hex');
  eq(G.shapeLatLngs().length, 6, 'a hexagon needs six points');
  G.setShape('circle');
  ok(G.shapeLatLngs().length >= 48, 'a circle needs many points: ' + G.shapeLatLngs().length);
  G.setShape('square');
});

/* ---------------- terrain mode ---------------- */
console.log('\nterrain mode: elevation instead of buildings:');
check('the band ramp mirrors the exporter', () => {
  const js = G.TERRAIN_RAMP;
  ok(Array.isArray(js) && js.length === 5, 'TERRAIN_RAMP should be 5 keys: ' + js);
  const py = fs.readFileSync(path.join(__dirname, 'map2model.py'), 'utf8');
  const m = py.match(/TERRAIN_RAMP\s*=\s*\[([^\]]+)\]/);
  ok(m, 'no TERRAIN_RAMP in map2model.py');
  const pyKeys = [...m[1].matchAll(/"([a-z0-9_]+)"/g)].map(x => x[1]);
  eq(js.join(','), pyKeys.join(','), 'the console and exporter ramps disagree');
  // every ramp colour must be a stocked filament
  for (const k of js) ok(G.FILAMENTS[k], 'ramp colour not stocked: ' + k);
  // and bands must NOT pollute DEFAULT_FILAMENTS (the mirror check pins that)
  ok(!('terrain_2' in G.DEFAULT_FILAMENTS), 'a band key leaked into DEFAULT_FILAMENTS');
});
check('setMode terrain rewrites the command and the preview params', () => {
  eq(typeof G.setMode, 'function', 'setMode');

  G.setMode('city');
  ok(!/--mode|--terrain-bands/.test(G.buildCmd()), 'city must not spell out a mode');

  document.getElementById('bands').value = 4;    // the stub has no HTML default
  G.setMode('terrain');
  const cmd = G.buildCmd();
  ok(/--mode terrain(\s|$)/.test(cmd), '--mode terrain missing: ' + cmd);
  ok(/--terrain-bands 4(\s|$)/.test(cmd), '--terrain-bands missing: ' + cmd);
  ok(/terrain_2=/.test(cmd), 'band 2 filament not spelled out: ' + cmd);
  ok(!/buildings=/.test(cmd), 'terrain mode still ships a buildings filament: ' + cmd);
  const ep = G.exactParams();
  ok(/(^|&)mode=terrain(&|$)/.test(ep), 'exactParams lost mode=terrain');
  ok(/(^|&)terrain_bands=\d(&|$)/.test(ep), 'exactParams lost terrain_bands');
  ok(/(^|&)buildings=0(&|$)/.test(ep), 'terrain preview still asks for buildings');

  G.setMode('city');                             // leave the fixture clean
  ok(!/--mode/.test(G.buildCmd()), 'setMode(city) did not undo terrain');
});

/* ---------------- route mode ---------------- */
console.log('\nroute mode: a path instead of a city:');
check('the route filament default mirrors the exporter', () => {
  eq(G.DEFAULT_FILAMENTS.route, 'basic_red', 'console route default is not basic_red');
  const py = fs.readFileSync(path.join(__dirname, 'map2model.py'), 'utf8');
  const block = py.slice(py.indexOf('DEFAULT_FILAMENTS = {'));
  const body = block.slice(0, block.indexOf('\n}'));
  const m = body.match(/"route":\s*"([a-z0-9_]+)"/);
  ok(m, 'no route default in map2model.py DEFAULT_FILAMENTS');
  eq(G.DEFAULT_FILAMENTS.route, m[1], 'the console and exporter route colour disagree');
  ok(G.FILAMENTS[G.DEFAULT_FILAMENTS.route], 'route colour is not a stocked filament');
});
check('parseGpx reads trkpt in either attribute order, and a plain list', () => {
  eq(typeof G.parseGpx, 'function', 'parseGpx');
  const gpx = '<gpx><trkseg>'
    + '<trkpt lat="50.44" lon="5.97"></trkpt>'
    + '<trkpt lon="5.98" lat="50.45"/>'
    + '</trkseg></gpx>';
  const a = G.parseGpx(gpx);
  eq(a.length, 2, 'expected two points, got ' + a.length);
  eq(a[0].join(','), '50.44,5.97', 'first trkpt wrong: ' + a[0]);
  eq(a[1].join(','), '50.45,5.98', 'lon-first trkpt not handled: ' + a[1]);
  const b = G.parseGpx('50.44,5.97\n50.45,5.98\n');
  eq(b.length, 2, 'plain lat,lon list not parsed');
  eq(G.parseGpx('').length, 0, 'empty input should give no points');
});
check('simplifyRoute caps the point count for the copied command', () => {
  eq(typeof G.simplifyRoute, 'function', 'simplifyRoute');
  const wig = [];
  for (let i = 0; i < 400; i++)
    wig.push([50.4 + i * 0.0005, 5.9 + (i % 2 ? 0.0004 : -0.0004)]);
  const out = G.simplifyRoute(wig, 40);
  ok(out.length <= 40, 'did not simplify to the cap: ' + out.length);
  ok(out.length >= 2, 'simplified away the whole path');
  eq(out[0].join(','), wig[0].join(','), 'the start point moved');
});
check('stitchOsmWays joins touching ways into one ordered path', () => {
  eq(typeof G.stitchOsmWays, 'function', 'stitchOsmWays');
  const els = [
    { type: 'way', geometry: [{lat:0,lon:0},{lat:0,lon:1},{lat:0,lon:2}] },
    { type: 'way', geometry: [{lat:0,lon:2},{lat:0,lon:3},{lat:0,lon:4}] },
  ];
  const p = G.stitchOsmWays(els);
  eq(p.length, 5, 'ways were not stitched end to end: ' + JSON.stringify(p));
  eq(p[4].join(','), '0,4', 'stitched path ends in the wrong place');
  eq(G.stitchOsmWays([]).length, 0, 'empty element list should give no path');
});
check('circuit lookup shows location-labelled suggestions before loading geometry', () => {
  const road={name:'Silverstone Circuit',type:'raceway',osm_type:'way',osm_id:123,
    address:{village:'Silverstone',state:'England',country:'United Kingdom'}};
  const town={name:'Silverstone',type:'village',osm_type:'relation',osm_id:456,
    address:{state:'England',country:'United Kingdom'}};
  const ranked=G.rankCircuitMatches([town,road]);
  eq(ranked[0].name,'Silverstone Circuit','raceway was not ranked ahead of a related place');
  eq(ranked.length,1,'an unrelated place was mixed into the circuit matches');
  eq(G.circuitKind(road),'race circuit','circuit has an unhelpful generic type label');
  G.showCircuitResults(ranked);
  const list=document.getElementById('circuitResults');
  ok(/Silverstone, England, United Kingdom/.test(list.innerHTML),'circuit location missing');
  ok(/Circuit names can repeat/.test(list.innerHTML),'ambiguity warning missing');
  eq(document.getElementById('routeQ').getAttribute('aria-expanded'),'true','suggestions not announced');
});
check('choosing a circuit targets its exact OpenStreetMap object', () => {
  eq(G.circuitOverpassQuery({osm_type:'way',osm_id:123}),
     '[out:json][timeout:25];way(123);out geom;','way query');
  eq(G.circuitOverpassQuery({osm_type:'relation',osm_id:456}),
     '[out:json][timeout:25];relation(456);out geom;','relation query');
});
check('setMode route rewrites the command and the preview params', () => {
  G.setMode('route');
  // a drawn path -> the command carries coordinates
  document.getElementById('routeWidth').value = 12;
  G.setRoute([[50.44, 5.97], [50.45, 5.98], [50.46, 5.99]], '');
  let cmd = G.buildCmd();
  ok(/--mode route(\s|$)/.test(cmd), '--mode route missing: ' + cmd);
  ok(/--route "50\.\d+,5\.\d+;/.test(cmd), '--route coordinates missing: ' + cmd);
  ok(!/--route-name/.test(cmd), 'a drawn path must not claim a name: ' + cmd);
  ok(!/buildings=|greenery=|roads=/.test(cmd), 'route mode still ships city filaments: ' + cmd);
  ok(/route=basic_red/.test(cmd), 'route filament not spelled out: ' + cmd);
  let ep = G.exactParams();
  ok(/(^|&)mode=route(&|$)/.test(ep), 'exactParams lost mode=route');
  ok(/(^|&)route=50/.test(ep), 'exactParams lost the route coordinates');
  ok(/(^|&)buildings=0(&|$)/.test(ep), 'route preview still asks for buildings');
  // a named circuit -> the command ships just the name
  G.setRoute([[50.44, 5.97], [50.45, 5.98]], 'Circuit de Spa-Francorchamps');
  cmd = G.buildCmd();
  ok(/--route-name "Circuit de Spa-Francorchamps"/.test(cmd), '--route-name missing: ' + cmd);
  ok(!/--route "/.test(cmd), 'a named circuit must not also carry coordinates: ' + cmd);
  ep = G.exactParams();
  ok(/route_name=Circuit/.test(ep), 'exactParams lost route_name');

  G.setMode('city');
  G.setRoute([], '');
  ok(!/--mode|--route/.test(G.buildCmd()), 'setMode(city) did not undo route');
});
check('the span slider opens up for a circuit and wide open for a relief map', () => {
  G.setMode('route');
  eq(String(document.getElementById('span').max), '8000', 'route mode did not widen the span slider');
  G.setMode('terrain');
  ok(+document.getElementById('span').max >= 100000,
    'terrain mode must allow a whole-park tile: ' + document.getElementById('span').max);
  G.setMode('city');
  // city reaches 8 km = 1:40,000 at the 200 mm print size
  eq(String(document.getElementById('span').max), '8000', 'city mode did not restore the span slider');
});

/* ---------------- camera ---------------- */
console.log('\nthe preview camera:');
function pointer(el, type, id, x, y, extra) {
  el.dispatch(type, Object.assign({ pointerId: id, clientX: x, clientY: y,
    button: 0, shiftKey: false, preventDefault() {} }, extra || {}));
}
check('one finger orbits', () => {
  const g = G.glInit();
  const cv = document.getElementById('gl');
  const az0 = g.cam.az, el0 = g.cam.el;
  pointer(cv, 'pointerdown', 1, 100, 100);
  pointer(cv, 'pointermove', 1, 160, 130);
  pointer(cv, 'pointerup', 1, 160, 130);
  ok(g.cam.az !== az0 || g.cam.el !== el0, 'dragging did not orbit');
});
check('two fingers pan and pinch', () => {
  const g = G.glInit();
  const cv = document.getElementById('gl');
  g.cam.tgt.set(0, 0, 0); g.cam.r = 400;
  const r0 = g.cam.r;
  pointer(cv, 'pointerdown', 1, 100, 100);
  pointer(cv, 'pointerdown', 2, 200, 100);      // second finger -> pinch
  pointer(cv, 'pointermove', 1, 60, 140);       // spread apart and slide down
  pointer(cv, 'pointermove', 2, 240, 140);
  ok(g.cam.r < r0, 'spreading two fingers did not zoom in: ' + g.cam.r);
  ok(g.cam.tgt.x !== 0 || g.cam.tgt.z !== 0, 'two fingers did not pan');
  pointer(cv, 'pointerup', 2, 240, 140);
  pointer(cv, 'pointerup', 1, 60, 140);
});
check('right-drag pans without spinning', () => {
  const g = G.glInit();
  const cv = document.getElementById('gl');
  g.cam.tgt.set(0, 0, 0);
  const az0 = g.cam.az;
  pointer(cv, 'pointerdown', 3, 100, 100, { button: 2 });
  pointer(cv, 'pointermove', 3, 150, 100);
  pointer(cv, 'pointerup', 3, 150, 100);
  eq(g.cam.az, az0, 'a right-drag rotated the model instead of panning');
  ok(g.cam.tgt.x !== 0 || g.cam.tgt.z !== 0, 'a right-drag did not pan');
});
check('lifting one of two fingers does not make the model jump', () => {
  const g = G.glInit();
  const cv = document.getElementById('gl');
  pointer(cv, 'pointerdown', 1, 100, 100);
  pointer(cv, 'pointerdown', 2, 300, 100);
  const az0 = g.cam.az;
  pointer(cv, 'pointerup', 2, 300, 100);        // drop to one finger
  pointer(cv, 'pointermove', 1, 102, 100);      // a 2px twitch
  ok(Math.abs(g.cam.az - az0) < 0.1,
     'the camera jumped when the second finger lifted: ' + (g.cam.az - az0));
  pointer(cv, 'pointerup', 1, 102, 100);
});
check('the canvas claims its own touch gestures', () => {
  G.glInit();
  eq(document.getElementById('gl').style.touchAction, 'none',
     'without touch-action:none the browser scrolls the page instead');
});

/* ---------------- results ---------------- */

check('the exact preview trusts the colour in the 3MF over the panel', () => {
  // serve.py reads the colour back off basematerials. If the file and the
  // panel ever disagree the file wins, because the file is what prints.
  G.pick.buildings = 'basic_blue';
  G.colFromPick();
  materials.length = 0;
  G.buildExactScene([
    { name: 'terrain', positions: [0, 0, 0, 10, 0, 0, 0, 10, 0],
      indices: [0, 1, 2], size: [10, 10, 1], color: '#FF6A13' },
    { name: 'buildings', positions: [0, 0, 0, 5, 0, 0, 0, 5, 8],
      indices: [0, 1, 2], size: [5, 5, 8] },
  ]);
  // materials carry a THREE.Color already converted to linear, so compare
  // against FIL() of the colour the file claimed rather than a raw hex
  const want = G.FIL('#FF6A13');
  const hit = materials.some(m => m.color instanceof Color
    && Math.abs(m.color.r - want.r) < 1e-6
    && Math.abs(m.color.g - want.g) < 1e-6
    && Math.abs(m.color.b - want.b) < 1e-6);
  ok(hit, 'the 3MF colour was ignored in favour of the panel: '
     + JSON.stringify(materials.map(m => m.color)));
  G.pick.buildings = G.DEFAULT_FILAMENTS.buildings;
  G.colFromPick();
});

/* ---------------- tweak without rebuilding ---------------- */
console.log('\nchanging a built preview:');
/* `let` state (exactLayers, centre...) is not on the sandbox global, but
   later scripts in the same context share the script scope, so read and
   write it by evaluating in-context. */
const R = code => vm.runInContext(code, sandbox);
const TILE = () => [
  { name: 'terrain', positions: [0, 0, 0, 10, 0, 0, 0, 10, 0], indices: [0, 1, 2],
    size: [10, 10, 1], color: '#61C680' },
  { name: 'greenery', positions: [0, 0, 1, 4, 0, 1, 0, 4, 1], indices: [0, 1, 2],
    size: [4, 4, 1], color: '#FFFFFF' },
  { name: 'roads', positions: [1, 1, 1, 3, 1, 1, 1, 3, 1], indices: [0, 1, 2],
    size: [2, 2, 1], color: '#000000' },
];
function built() {
  // exactly what buildExact() does once the mesh lands
  R(`showTab('map'); exactLayers = __T; exactKey = geomKey();
     builtPick = Object.assign({}, pick); inflightKey = '';
     buildExactScene(exactLayers); restyleExact();`.replace('__T', JSON.stringify(TILE())));
}
const meshOf = n => R(`three.root.children.find(m => m.userData && m.userData.layer === '${n}')`);
let fetches = 0;
const realFetch = sandbox.fetch;
sandbox.fetch = async (...a) => { fetches++; return realFetch(...a); };

check('the preview always asks for every layer, whatever is switched off', () => {
  R(`on.greenery = false; on.roads = false;`);
  const q = new URLSearchParams(G.exactParams());
  eq(q.get('greenery'), '1', 'greenery'); eq(q.get('roads'), '1', 'roads');
  ok(/greenery=/.test(q.get('filaments') || ''), 'every pick rides the build: ' + q.get('filaments'));
  R(`on.greenery = true; on.roads = true;`);
});

check('a new colour after a build recolours in place, with no download', () => {
  built(); fetches = 0;
  R(`pick.roads = 'basic_blue'; colFromPick(); repaint();`);
  const want = G.FIL(G.FILAMENTS.basic_blue.hex), c = meshOf('roads').material.color;
  ok(Math.abs(c.r - want.r) < 1e-6 && Math.abs(c.b - want.b) < 1e-6,
     'roads were not recoloured: ' + JSON.stringify(c));
  ok(R('exactLayers') !== null, 'a colour change threw the model away');
  eq(fetches, 0, 'fetches');
  R(`pick.roads = DEFAULT_FILAMENTS.roads; colFromPick();`);
});

check('an unchanged pick keeps the colour the file carries', () => {
  built();
  const want = G.FIL('#61C680'), c = meshOf('terrain').material.color;
  ok(Math.abs(c.g - want.g) < 1e-6, 'terrain lost its file colour');
});

check('switching greenery off hides it without a rebuild', () => {
  built(); fetches = 0;
  R(`on.greenery = false; repaint();`);
  eq(meshOf('greenery').visible, false, 'greenery visible');
  eq(meshOf('roads').visible, true, 'roads visible');
  ok(R('exactLayers') !== null, 'hiding a layer threw the model away');
  eq(fetches, 0, 'fetches');
  R(`on.greenery = true; repaint();`);
  eq(meshOf('greenery').visible, true, 'greenery back');
});

check('moving the tile after a build says so and offers Build preview', () => {
  built();
  R(`centre.lat += 0.02; tileChanged();`);
  eq(R('exactLayers'), null, 'the stale model was kept');
  const html = document.getElementById('prevOverlay').innerHTML;
  ok(/tile moved/i.test(html), 'no reason given: ' + html.slice(0, 120));
  ok(/btnBuild/.test(html), 'no Build preview button');
  R(`centre.lat -= 0.02;`);
});

check('a changed building stretch is named as the reason', () => {
  built();
  const z = document.getElementById('bscale'), old = z.value;
  z.value = String((+old || 1) + 0.5);
  R('repaint()');
  ok(/building stretch/i.test(document.getElementById('prevOverlay').innerHTML),
     'reason missing');
  z.value = old;
});

check('Cancel goes back to the Build preview button', () => {
  R(`exactLayers = null; inflightKey = 'x'; logSteps = []; drawLog('note', true);`);
  document.getElementById('btnCancel').click();
  const html = document.getElementById('prevOverlay').innerHTML;
  ok(/btnBuild/.test(html) && /Build preview/.test(html), 'no way back: ' + html.slice(0, 120));
  ok(/cancelled/i.test(html), 'does not say it was cancelled');
  eq(R('inflightKey'), '', 'inflightKey');
});

check('Cancel with a still-current preview shows that preview again', () => {
  built();
  R(`drawLog('note', true);`);
  document.getElementById('prevOverlay').classList.remove('hidden');
  document.getElementById('btnCancel').click();
  ok(document.getElementById('prevOverlay').classList.contains('hidden'),
     'the old preview stayed covered');
});

check('the tab title reports a build only while the page is away', () => {
  const base = R('BASE_TITLE');
  document.hidden = true;
  R(`titleStatus('⏳ Building 1:05')`);
  ok(String(document.title).startsWith('⏳ Building 1:05'), 'title: ' + document.title);
  R(`titleStatus('✓ Preview ready', true)`);
  ok(/Preview ready/.test(document.title), 'finished note missing');
  document.hidden = false;
  R('titleBack()');
  eq(document.title, base, 'title after coming back');
  eq(R('titleMsg'), '', 'a read note should clear');
  delete document.hidden;
  eq(G.fmtMin(125), '2:05', 'fmtMin');
});

check('a look-only change says what changed and that no rebuild happened', () => {
  built();
  R(`lastLook = lookNow(); showTab('prev');`);
  R(`pick.roads = 'basic_blue'; colFromPick(); repaint();`);
  const t = document.getElementById('prevToast');
  ok(!t.classList.contains('hidden'), 'no note shown');
  ok(/Roads/.test(t.innerHTML) && /Blue/.test(t.innerHTML), 'note does not name it: ' + t.innerHTML);
  ok(/no rebuild/i.test(t.innerHTML), 'note does not say no rebuild');
  ok(!document.getElementById('prevVeil').classList.contains('hidden'), 'no refresh veil');
  R(`pick.roads = DEFAULT_FILAMENTS.roads; colFromPick(); showTab('map');`);
});

check('an out-of-date page is detected from the server\'s file stamp', () => {
  R(`lastPing = {ok: true, console: 2000000000};`);
  document.lastModified = 'Mon, 01 Jan 2024 00:00:00 GMT';
  eq(R('pageOutdated()'), true, 'stale page not noticed');
  R(`lastPing = {ok: true, console: Math.floor(Date.parse(document.lastModified)/1000)};`);
  eq(R('pageOutdated()'), false, 'a current page flagged');
  delete document.lastModified;
  R('lastPing = null');
});

check('an out-of-date preview server is named and nothing is built', () => {
  R(`lastPing = {ok: true, console: 0};`);                 // no api field: old server
  eq(R('serverOutdated()'), true, 'old server not noticed');
  R(`lastPing = {ok: true, api: API_WANT, console: 0};`);
  eq(R('serverOutdated()'), false, 'current server flagged');
  R('lastPing = null');
});

console.log('\nturning the tile:');
check('a turned tile is written as centre, radius and turn; a straight one as --bbox', () => {
  R('angle = 30');
  const c = G.buildCmd();
  ok(/--center \S+ \S+ --radius \d+ --rotate 30\b/.test(c) && !/--bbox/.test(c), c);
  R('angle = 0');
  ok(/--bbox /.test(G.buildCmd()) && !/--rotate/.test(G.buildCmd()), 'straight tile lost --bbox');
});

check('resizing a turned tile keeps the opposite corner where it was', () => {
  R('angle = 30; placeRect(false)');
  const a = R('tileCorners().sw'), anchor = {lat: a[0], lng: a[1]};
  const drag = {lat: anchor.lat + 0.012, lng: anchor.lng + 0.021};
  const sq = R(`squareFromCornerRot(${JSON.stringify(anchor)}, ${JSON.stringify(drag)})`);
  R(`centre = {lat: ${sq.lat}, lng: ${sq.lng}}; spanM = ${sq.span};`);
  // one of the new corners must sit on the anchor
  const cs = Object.values(R('tileCorners()'));
  const d = Math.min(...cs.map(c => Math.hypot((c[0]-anchor.lat)*111320,
    (c[1]-anchor.lng)*111320*Math.cos(anchor.lat*Math.PI/180))));
  ok(d < 0.5, 'the anchor corner moved ' + d.toFixed(2) + ' m');
  R('angle = 0; centre = {lat:43.6455, lng:-79.3860}; spanM = 1600; placeRect(false)');
});

console.log('\nthe name on the frame:');
check('a name is cleaned, measured and cut before it would not fit', () => {
  R(`setPlate('spa  francorchamps')`);
  eq(R('plate'), 'SPA FRANCORCHAMPS', 'cleaned name');
  R(`setPlate('W'.repeat(60))`);
  ok(R('plateWidth(plate) <= plateRoom()'), 'a name wider than the frame side got through');
  ok(R('plate.length') > 5 && R('plate.length') < 60, 'not trimmed sensibly: ' + R('plate.length'));
  ok(/longest name/.test(document.getElementById('plateNote').innerHTML), 'no note about the cut');
  R(`setPlate('café & bar')`);
  ok(!/[&É]/.test(R('plate')), 'unprintable characters kept: ' + R('plate'));
  R(`setPlate('SPA')`);
  const c = G.buildCmd();
  ok(/--nameplate "SPA" --nameplate-side s/.test(c), c);
  ok(/name=basic_black/.test(c), 'the name colour is not in the code: ' + c);
  R(`setPlate('')`);
  ok(!/--nameplate/.test(G.buildCmd()), 'an empty name still in the code');
});

check('the name follows the frame colour until it gets its own', () => {
  R(`setPlate('SPA'); nameFollowsFrame = true; pick.name = pick.frame;`);
  const pickTo = (layer, key) => R(`(() => { const b = {dataset:{pickfor:'${layer}', key:'${key}'}};
      pick[b.dataset.pickfor] = b.dataset.key;
      if(b.dataset.pickfor === 'name') nameFollowsFrame = false;
      if(b.dataset.pickfor === 'frame' && nameFollowsFrame) pick.name = pick.frame; })()`);
  pickTo('frame', 'basic_blue');
  eq(R('pick.name'), 'basic_blue', 'name did not follow the frame');
  pickTo('name', 'basic_red'); pickTo('frame', 'basic_black');
  eq(R('pick.name'), 'basic_red', 'the buyer\'s own name colour was overwritten');
  R(`pick.frame = DEFAULT_FILAMENTS.frame; pick.name = pick.frame; nameFollowsFrame = true; setPlate('')`);
});

console.log('\nopening and remembering a code:');
check('a copied code opens back into exactly the same order', () => {
  R(`angle = 25; centre = {lat: 50.4372, lng: 5.9714}; spanM = 1650; setPlate('SPA-FRANCORCHAMPS');
     setPlateSide('n'); pick.roads = 'basic_blue'; on.greenery = false; placeRect(false)`);
  const s1 = G.buildCmd();
  R(`angle = 0; centre = {lat: 1, lng: 1}; spanM = 900; setPlate(''); pick.roads = 'basic_black'; on.greenery = true;`);
  const r = R(`parseCmd(${JSON.stringify(s1)})`);
  ok(!r.error, r.error);
  R(`applyCmd(parseCmd(${JSON.stringify(s1)}).state)`);
  eq(G.buildCmd(), s1, 'round trip');
  // a straight tile round-trips too (through --bbox)
  R('angle = 0; placeRect(false)');
  const s2 = G.buildCmd();
  R(`applyCmd(parseCmd(${JSON.stringify(s2)}).state)`);
  const s3 = G.buildCmd();
  R(`applyCmd(parseCmd(${JSON.stringify(s3)}).state)`);
  eq(G.buildCmd(), s3, 'straight round trip is not stable');
  R(`pick.roads = DEFAULT_FILAMENTS.roads; on.greenery = true; setPlate(''); setPlateSide('s');
     centre = {lat:43.6455, lng:-79.3860}; spanM = 1600; placeRect(false)`);
});

check('a code with one bad part changes nothing and says which part', () => {
  const before = G.buildCmd();
  const r = R(`parseCmd('python map2model.py --bbox 5.95 50.42 5.98 50.44 --size 200 --filaments roads=basic_rainbow')`);
  ok(r.error && /basic_rainbow/.test(r.error), 'error does not name the bad colour: ' + r.error);
  eq(G.buildCmd(), before, 'a rejected code still changed the page');
  ok(R(`parseCmd('hello there')`).error, 'nonsense accepted');
  ok(/tile/.test(R(`parseCmd('python map2model.py --size 200')`).error), 'a code with no tile accepted');
  const c = R(`parseCmd('python map2model.py --center 50.4 5.9 --size 150')`);
  ok(!c.error && c.state.span === 2000, '--center without --radius should use the 1000 m default');
});

check('the last order is restored from this device', () => {
  const code = 'python map2model.py --center 50.43720 5.97140 --radius 800 --rotate 12 --size 150 -o spa.3mf';
  sandbox.localStorage.setItem('m2m.last.v1', code);
  ok(R('restoreLast()'), 'nothing restored');
  eq(R('angle'), 12, 'angle'); eq(R('sizeMM'), 150, 'size'); eq(R('spanM'), 1600, 'span');
  sandbox.localStorage.store = null;                      // blocked storage
  eq(R('restoreLast()'), false, 'blocked storage should just skip');
  sandbox.localStorage.store = {};
  R(`angle = 0; sizeMM = 200; centre = {lat:43.6455, lng:-79.3860}; spanM = 1600; placeRect(false)`);
});

console.log('\npreview extras:');
check('view buttons move the camera; Save picture names the file after the tile', () => {
  built();
  R(`setView('top')`); ok(R('three.cam.el') > 1.4, 'top view is not from above');
  R(`setView('front')`); ok(Math.abs(R('three.cam.az') - Math.PI/2) < 1e-9, 'front view is not the front');
  R(`three.renderer.domElement.toDataURL = () => 'data:image/png;base64,AA'; setPlate('Big Ben')`);
  eq(R('savePicture()'), 'big-ben-preview.png', 'file name');
  R(`setPlate('')`);
});

sandbox.fetch = realFetch;

/* ---------------- results ---------------- */
console.log('\n' + '-'.repeat(52));
console.log(pass + ' passed, ' + fails.length + ' failed');
if (fails.length) { console.log('failed: ' + fails.join(', ')); process.exit(1); }
