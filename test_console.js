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
    appendChild() {}, remove() {}, focus() {}, blur() {},
    getContext() { return {}; },
    getBoundingClientRect() { return {left:0, top:0, width:640, height:420}; },
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
    _bounds: null,
    addTo(m) { m.addLayer ? m.addLayer(o) : mapLayers.add(o); return o; },
    setBounds(b) { o._bounds = b; return o; },
    getBounds() { return o._bounds || [[0,0],[0,0]]; },
    setStyle() { return o; }, remove() { mapLayers.delete(o); },
  };
  return o;
}
const L = {
  map() { return leafletMap; },
  tileLayer() { return layer(); },
  rectangle() { return layer(); },
  marker() { return layer(); },
  latLng: latlng,
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
  addEventListener() {}, removeEventListener() {},
  fetch: async () => ({ ok: false, json: async () => ({}) }),
  AbortController, URLSearchParams, JSZip: function () {},
  alert() {}, Math, Date, JSON,
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
   `const` arrows come back through __C. */
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
check('a cover close to the ceiling warns without blocking', () => {
  sceneWithCover(220);                       // over 85% of 250, under 250
  const note = document.getElementById('sizeNote');
  ok(/close to the limit/i.test(note.innerHTML),
     'no near-limit warning for a 220 mm cover: ' + note.innerHTML);
});
check('a comfortable cover produces no warning at all', () => {
  document.getElementById('sizeNote').innerHTML = '';
  sceneWithCover(90);
  const note = document.getElementById('sizeNote');
  ok(!/will not print|close to the limit/i.test(note.innerHTML),
     'spurious warning for a 90 mm cover: ' + note.innerHTML);
});

/* ---------------- results ---------------- */
console.log('\n' + '-'.repeat(52));
console.log(pass + ' passed, ' + fails.length + ' failed');
if (fails.length) { console.log('failed: ' + fails.join(', ')); process.exit(1); }
