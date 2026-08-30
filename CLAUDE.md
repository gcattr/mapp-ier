# ECM Model 64 · City Planner

Turns a lat/lon square into a multi-colour 3MF city model for a Bambu Lab P1S.
Built for an Etsy shop: the buyer picks a tile in a web console, copies a command
into the Etsy personalisation field, and the seller runs it to produce the print
files.

## Files

| File | What it is |
|---|---|
| `map2model.py` | The exporter. ~2500 lines, version `0.9.0-osm`. Does everything. |
| `serve.py` | Local HTTP server: serves the console AND runs the exporter behind it. |
| `map2model-console.html` | The web console. Single file, no build step. |
| `test_console.js` | Offline harness for the console: stubs the DOM, Leaflet and three.js and invokes every top-level function. `node test_console.js`. |
| `landmarks.json` | Per-building shape overrides (CN Tower legs and mast). The console asks for it on every build (`landmarks: 'landmarks.json'`) and `serve.py` skips it **silently** when it is absent — so if it goes missing the CN Tower renders as straight prisms in preview *and* export, with no warning anywhere. It is tracked in git for exactly that reason. |
| `Bambu_PLA_Basic_Hex_Code.pdf`, `Bambu_PLA_Matte_Hex_Code.pdf` | Bambu's own filament hex tables. The source of truth for every colour in `FILAMENTS`; keep them, and re-read them rather than trusting a hex you remember. |
| `test_offline.py` | **MISSING from this folder.** Offline fixture — stubs `fetch_all()` so you can test without network. Never committed and not in the Recycle Bin, so it has to be rewritten; the Testing section below cannot run until it is. |

## Running it

```bash
python serve.py            # Windows: python   Mac: python3
# -> http://localhost:8000/map2model-console.html
```

`map2model.py`, `serve.py` and `map2model-console.html` must sit in the same
folder; `landmarks.json` too, once it exists. `serve.py` reads `PORT` from the
environment (for Railway etc.) and is stdlib-only beyond what `map2model.py`
already needs.

Direct CLI use:

```bash
python map2model.py --bbox W S E N --size 200 --roofs all \
  --split --box --water-in-frame --frame --frame-width 6 --frame-depth 7 \
  --frame-clearance 0.3 -o city.3mf
```

Dependencies: `duckdb shapely numpy pyproj trimesh mapbox_earcut pillow requests`

## Architecture

```
Overture Maps (S3 parquet, via DuckDB)  ─┐
AWS terrarium elevation tiles            ├─> map2model.py ─> N × 3MF
landmarks.json                          ─┘
                                              ▲
                      serve.py ───────────────┘
                          │  /api/start, /api/status, /api/mesh
                          ▼
                    console.html  (three.js r128)
```

The console's 3D preview is **the real exporter output**, not an approximation.
`serve.py` runs `map2model.run()` in a thread, parses the 3MF it just wrote, and
streams vertex/index arrays to the browser. A tile-based fast preview still
exists in the JS but is dead code — the exact path is the only one wired up.

## Output plates (`--split`)

One AMS = 4 filaments per plate, so the model is split:

| File | Objects | Filaments |
|---|---|---|
| `name.3mf` | terrain, greenery, roads, buildings | 4 |
| `name_frame.3mf` | frame + water | 2 |
| `name_cover.3mf` | cover_left, cover_right | 1–2 |

`name_water.3mf` exists only when `--water-in-frame` is **off**; with it on
(which is what the console and the copied command both use) water rides the
frame plate and no `_water` file is written. Anything reading the split output
back — `serve.py` does — must look for `_frame`, `_water` *and* `_cover`.

### Where the files land

A bare `-o name.3mf` is routed to **`3Dmodels/name/`**, so a model's 2–3 plates
sit together in one folder named after the model:

```
3Dmodels/toronto/toronto.3mf          terrain, greenery, roads, buildings
3Dmodels/toronto/toronto_frame.3mf    frame + water
3Dmodels/toronto/toronto_cover.3mf    cover_left, cover_right
```

`--split` writes its siblings next to whatever file it is handed, so without
this the plates scatter through the working directory and only belong together
by name. `model_out_path()` does the routing and **leaves any path that already
names a directory alone** — `serve.py` hands `run()` an absolute temp path, and
an explicit `-o out/city.3mf` means the caller has already chosen.
`3Dmodels/` is build output; `*.3mf` in `.gitignore` already covers it.

Assembly: water prints as the frame's floor; the land plate has the water
regions **cut clean through** so the blue shows; land glues on top; the two
cover halves slide on from opposite ends and tape at the seam.

Filament slots (order matters — the slicer assigns by object order):
terrain 1, greenery 2, roads 3, buildings 4; water rides the frame plate.

Colours are filament choices now, not constants - see **Filaments and colour**.

## Hard-won details — read before changing geometry

These were each a multi-round debugging session. Don't undo them.

**Roof direction.** `roof:direction` is the SLOPE bearing, not the ridge — the
ridge is perpendicular. `roof:orientation=across` puts the ridge on the SHORTER
edge. Getting either wrong rotates every tagged roof 90°. OSMBuildings had the
identical bug.

**Axis sign must be canonicalised.** `minimum_rotated_rectangle` vertex order
varies between GEOS versions, which flips the surface normal, which flips which
side of a skillion is high. This is why the same file rendered differently on
macOS and Windows. The axis is forced into a half-plane.

**Ridge roofs are axis-fragile.** A 1 m change in footprint flips the ridge 90°
on a near-square building. Hence `--roofs` modes:
`none` / `symmetric` (default — domes and cones only, footprint-derived so they
can't face wrong) / `safe` (adds ridges only where tagged or clearly oblong) /
`all`.

**Two roof clamps, not one.** `--max-roof-frac 0.95` for pointed roofs (a spire
really is mostly roof) and `--max-ridge-frac` for ridge roofs. They must stay
separate: one shared clamp is what turned entire buildings into wedges — the
"triangular prisms facing the wrong way" bug.

`--max-ridge-frac` now defaults to **0.98**, the top of the console's slider
(`min 0.2 / max 0.98`), so the CLI and the console agree out of the box. It used
to default to 0.45. At 0.98 a gabled roof may eat almost the whole building, so
a mis-detected ridge shows up as a full-height wedge rather than a small hat —
that is the trade for matching the console. `--roofs symmetric` (the default)
sidesteps it entirely by never emitting ridge roofs.

**Scaling anchor must be the true axis.** `representative_point()` returns any
interior point — on a 3-leg tower it lands ~5 m off-axis and the legs slide
sideways as they taper (the "fan"). The parent outline's centroid is worse
(40 m off, because of the podium). Use the TOPMOST part's centroid. And for
concave L/U footprints the centroid is OUTSIDE the polygon, so fall back to
`representative_point()` there.

**Landmark profiles are anchored to absolute height**, not to a fraction of each
part's height. A per-part fraction cannot express "flare only near the ground" —
that was the "blade" silhouette.

**Synthesised tapers are opt-in.** `spire_taper=False` by default. Turning it on
needles the top of *every* parts-bearing building, which spikes the whole city.
The CN Tower's flare only exists via `--landmarks`; OSM/Overture model the legs
as constant-section prisms, so every faithful renderer draws them straight.

**DuckDB geometry.** Use `ST_AsWKB(geometry)`, not the bare column — loading the
`spatial` extension converts GeoParquet geometry to DuckDB's internal GEOMETRY
type on some builds, which shapely can't parse ("Input buffer is smaller than
requested object size"). There are fallbacks and a tolerant WKB reader.

**Layer heights** match between exporter and preview: greenery 0.8 mm proud,
roads 0.5 mm, water 0.6 mm, all embedded 0.35 mm into what's below.

**Blanket greenery.** `base/land_cover` is derived from a coarse global raster
and dissolved into continent-sized multipolygons. The `forest` feature covering
Monaco is 1.4 million km² — **544,000× a 1.6 km tile** — and clipped to the tile
it is a solid green sheet over the entire city. That was the "layer of greenery
covering Monaco" bug.

`to_green_polys()` drops a greenery feature only when **both** hold: it covers
≥ `green_blanket_frac` (0.85) of the tile *and* its source polygon is
≥ `green_blanket_scale` (25×) the tile. One test alone is not enough — clipped
area alone would throw away a park that genuinely fills the tile, and source
size alone would throw away a real national forest. Measured effect: Monaco
111.8% → 11.8% coverage, one polygon dropped; Big Ben (48 polys) and Toronto
(57 polys) completely unchanged. Every drop prints a `[warn]` naming the ratio.

**A failed query and an empty tile both return `[]`.** `fetch()` swallows S3 and
DuckDB errors and returns an empty list, which is indistinguishable from a tile
that genuinely has nothing in it. The exporter used to sail on and write a bare
terrain slab — that is how `BIGBEN.3mf` came out as 8 vertices of flat ground
while central London actually holds 1266 buildings. It is a transient failure
(DNS/S3), so the same tile succeeds minutes later.

Failures are now recorded in the module-level `FETCH_ERRORS` and `run()` refuses
to write a model with no buildings, roads, greenery *or* water, raising a
message that distinguishes the two cases — retry versus move the tile. A partial
failure (some layers fetched, one failed) still builds, but prints a `[warn]`
saying the layer is missing because the **query failed**, not because the tile
is empty. Do not paper over this by making an empty fetch look like success:
an empty plate is a plate a customer can be sent.

**Worse: a query can return zero rows and no error at all.** The same Big Ben
run fetched **0 roads in 4m17s with no warning**, while the identical query run
minutes later returned 562 (232 residential, 228 primary, …). Nothing raised;
`fetch()` simply got an empty result set. The suspect is the glob:
`read_parquet('s3://…/*')` expands through an S3 LIST, and when that LIST fails
or comes back short DuckDB scans fewer files — or none — and reports success on
whatever it did read. The listing is normally stable and fast (128 files for
transportation, 512 for buildings, ~0.5 s, identical across repeats), so this is
intermittent, which is exactly why the console "works in downtown Toronto" and
seems broken elsewhere: people retry Toronto and don't retry the rest.

`_confirm_empty()` handles it, and both its checks are paid for **only when the
result was already empty**. It counts the files the glob resolves (~0.5 s): zero
files means a listing failure, recorded in `FETCH_ERRORS`, not an empty tile.
Otherwise it re-runs the query once — if the retry returns rows, it prints a
loud `[warn]` that the first scan dropped data silently. A city tile with no
roads is nearly always a lie; a second query is cheaper than a roadless plate.
Set `retry_empty_fetch=False` to skip the retry.

## Filaments and colour

The buyer picks a filament for every part they can see, in the console, and the
choice rides all the way through: preview -> copied command -> the 3MF that is
actually printed.

### The stock list

Nine PLA Basic and nine PLA Matte, and nothing else. `FILAMENTS` in
`map2model.py` is the source of truth; the console holds a mirror of it, and
`test_console.js` fails if the two drift, because a buyer picking a colour the
exporter does not know is an order that cannot be filled — and it would only
fail days later, when the seller finally runs the command.

Hex codes come from Bambu's own *Filament Hex Code Table* PDFs, which are in
this folder (`Bambu_PLA_Basic_Hex_Code.pdf`, `Bambu_PLA_Matte_Hex_Code.pdf`).
**Do not eyeball a colour** — the preview, the 3MF and the printed part all key
off this table. `python map2model.py --list-filaments` prints it.

Two pairs collide by hex and differ only in finish: Matte Ivory White and Basic
Jade White are both `#FFFFFF`; Matte Charcoal and Basic Black are both
`#000000` (Bambu's table really does say that). They render identically in the
preview, which is why the picker names them instead of relying on a swatch.

### Defaults

| Layer | Filament | |
|---|---|---|
| terrain | PLA Matte Grass Green | `#61C680` |
| greenery | PLA Basic Jade White | `#FFFFFF` |
| roads | PLA Basic Black | `#000000` |
| buildings | PLA Basic Gray | `#8E9089` |
| water | PLA Matte Marine Blue | `#0078BF` |
| frame | PLA Basic Black | `#000000` |
| cover | PETG | not a buyer choice |

The cover is a shipping shell, printed in whatever PETG is on the shelf for
toughness. It is deliberately absent from the console — it is the one key the
exporter has and the picker must not offer, and a test pins that.

Buildings moved from `#9BA3A8` to `#8E9089` and roads from `#1E2124` to
`#000000`: the old values were plausible greys that no filament actually is.

### How colour reaches the print

`--filaments terrain=matte_grass_green,buildings=basic_gray,...` — the console
writes this into the copied command with **every enabled layer spelled out**,
defaults included. That string is pasted into an Etsy order and run days later
by a human: it has to say what the buyer chose, not what the exporter would
guess on the day it runs. `parse_filaments()` rejects an unknown layer or
colour outright rather than falling back to a default, for the same reason.

Each plate is written with colour twice, because no single form is read by
everything:

- **`<basematerials>` + per-object `pid`/`pindex`** — the portable form. Any
  3MF-aware tool opens the model in the right colours.
- **`<m:colorgroup>`** — what Bambu Studio's "standard 3MF" reader actually
  looks at. It maps groups to AMS slots **by order, not by hex**, which is why
  object order is load-bearing (see the plate table above).

Neither is listed in `requiredextensions`: a slicer that understands no
material extension should still load the geometry rather than refuse the file.

### Bambu Studio project metadata

On top of that, every plate carries `Metadata/model_settings.config` and
`Metadata/project_settings.config`, so it opens in Bambu Studio as a project
with the right filament already in each AMS slot rather than as bare geometry
with everything on slot 1. `--no-bambu-project` turns this off.

- `model_settings.config` pins object *i* to extruder *i*.
- `project_settings.config` carries `filament_colour`, `filament_type`,
  `filament_ids` and `filament_settings_id`, plus a `map2model_slots` list the
  slicer ignores — it is there so whoever loads the AMS can read what each slot
  is for without opening the console.

The Bambu SKU codes (`GFA00` = PLA Basic, `GFA01` = PLA Matte, `GFG02` =
PETG HF) and the preset names are not guesses: they were read out of the
profiles that ship with Bambu Studio, at
`resources/profiles/BBL/filament/<name> @base.json`.

**Not yet verified on the machine.** The 3MF is well-formed and the metadata
matches a real MakerWorld project file field for field, but nobody has opened
one of these in Bambu Studio and confirmed the AMS slots come up right. That is
the one thing to check before the first real order.

## Print-volume rules

Target machine is a **Bambu Lab P1S**. The machine is 256 mm in every axis, but
**250 mm is the working limit for everything printed here**, in all three axes,
on every plate — one number, with margin built in. `Config.build_volume_mm =
250.0`. There is **no CLI flag**; it is settable only in code. It used to say
256, which let the exporter pass plates the console warned about.

The model itself lands well inside that: the largest print-size button is
200 mm, and frame and cover are wider than the model because they wrap it
(200 mm model + 2×clearance + 2×6 mm frame ≈ 213 mm).

The console *warns*, it does not block: over 250 mm it writes a
customer-readable `sizeNote` ("This will not print"), and past `250 × 0.85` a
"close to the limit" note, but the build still runs. `test_console.js` pins
that behaviour ("a cover close to the ceiling warns without blocking").

Cover height = `2.4 + tallest building + 10.0 + 3.0` mm, so it overflows once
the model passes ~240 mm. Building stretch is what usually causes this.

Resolution: aim under 8 m/mm. `--min-feature 0.9` widens anything thinner so it
prints, which is why a 2 m mast comes out proportionally fat on a coarse tile.
The console shows this as a 1:N ratio with a warning past the threshold.

## The cover

Two half-shells, each closed on three sides, with a retaining groove along all
of them: a lip under the frame rim and a rib over it, so the rim is captured
with only the 0.4 mm slip fit to move in. Four corner posts carry the ceiling so
a knock can't press it onto a spire. Defaults: 3.0 mm walls, 2.4 mm lip/rib,
6.0 mm reach, 10.0 mm headroom.

**Not yet done:** the halves butt at the seam (a lapped joint would resist
shear better), and each half prints as a shell with a ~219 mm ceiling to bridge
— it needs rotating onto a side wall in the slicer, or in the exporter.

## The console — hard-won details

Same rule as the geometry section: these were each a real bug. Don't undo them.
`test_console.js` has a regression check for every one of them.

**The colour pipeline must be told it is sRGB.** three r128 defaults
`renderer.outputEncoding` to `LinearEncoding`, so a filament hex like `#61C680`
was lit as though it were already linear and written straight to the canvas.
Every colour came out pale and chalky and the legend swatch never matched the
model. Two halves to the fix, and both are needed: `outputEncoding =
sRGBEncoding` on the renderer, and **every material colour built through
`FIL()`** (`new THREE.Color(hex).convertSRGBToLinear()`). Setting a bare hex on
a material skips the conversion and that one layer goes chalky again. Lighting
summed to ~2.1 of near-white, which parks every surface at the top of its range
where hue washes out regardless; it is now ~1.5, exposure 1.12 → 0.95.

**The ghost rectangle must be cleared on every exit path.** Releasing the mouse
outside the map container never fires Leaflet's `mouseup`, so the dashed drag
rectangle stayed on the map and the next drag drew a second one — the "two boxes
appear when dragging a new tile" bug. `clearGhost()` runs on mousedown, on the
draw-mode toggle, and from a `document`-level mouseup that finishes the drag
from the last seen position. Leaflet's own handler clears `origin` first, so the
document listener cannot double-fire.

**A cancelled build outlives the build that replaced it.** `buildPreview()`
aborts the previous run, but that run only notices at its next `await` — up to a
700 ms poll later, by which time the new build owns the overlay, the clock and
the status line. Its cleanup then landed on the new run: `stopClock()` killed the
*new* interval and its status write replaced the *new* build's status, so the
elapsed counter froze or carried the old job's number. `startClock()` now returns
a generation token, `stopClock(gen)` ignores a stale one, and a run that finds
itself cancelled returns without touching the panel at all. A genuine user
cancel stops the clock from the Cancel handler instead.

**Dispose materials as an array.** The water mesh carries *two* materials (blue
top, dark sides), so `mesh.material` is an array and an array has no `.dispose`.
The scene-clearing loop called it directly, so the **second** preview of any
tile containing water — most of them — died with "c.material.dispose is not a
function". Normalise with `[].concat(c.material)` before disposing.

**The cover is measured, never drawn.** `--split` writes `_frame` and `_cover`
siblings (`_water` only when `water_in_frame` is off, which the console never
does). `serve.py` was looking for `_frame` and `_water`, so **no `cover_` layer
ever reached the browser** and the console's cover-versus-250 mm check — the
thing that stops an unprintable cover reaching a customer — passed on every
tile by measuring an empty list. `serve.py` now loads all three suffixes.

That makes the console's own filter matter: it skipped `box_` only, a name the
exporter no longer emits. Drawn, the cover is a shell wider and taller than the
whole city — it hides the model, and it lands in the bounds loop where it
becomes `tallest` and frames the camera around a box. `packaging()` now covers
`box_` *and* `cover_`, and excludes both from the scene, the bounds and the
object count, while the print-volume check still measures them.

**The picker is a stock list, not a colour wheel.** It used to be an
`<input type="color">` per layer, which let a buyer choose any of 16 million
colours, approximately nine of which the shop can print. It is now a `<select>`
of the stocked filaments, grouped by finish. A native `<select>` and not a
custom swatch grid on purpose: on a phone it opens as a full-screen list with
real touch targets, which nothing hand-rolled matches.

**`col` is derived, never set.** `pick` (layer -> filament key) is the buyer's
order; `col` is only its rendering, recomputed by `colFromPick()`. Setting a
colour directly is how the swatch, the preview and the copied command drift
apart.

**The preview trusts the file, not the panel.** `serve.py` reads each object's
colour back off `basematerials` and sends it with the mesh, and
`buildExactScene()` prefers that over the panel's own idea. If the two ever
disagree the file wins, because the file is what prints.

**The server-side job is not cancelled.** Starting a new preview abandons the old
job rather than stopping it; it runs to completion, holding a slot in the
6-job cap. There is no `/api/cancel`.

## Mobile

Buyers arrive from an Etsy listing, which is overwhelmingly a phone, so the
narrow layout is the common case and not an afterthought. Two breakpoints:
940 px stacks the map above the panel; 560 px is the phone pass — 44 px touch
targets (Apple's minimum), a shorter map that still leaves room to aim a tile,
and **16 px font on the filament `<select>`**, because iOS silently zooms the
whole page when a focused control is smaller than that.

## Testing

`test_offline.py` stubs `map2model.fetch_all()` so the whole pipeline runs with
no network:

```python
import map2model as M, test_offline as T
M.fetch_all = lambda cfg: (T.buildings, T.parts, T.water, T.green, T.roads)
M.run(M.Config(bbox=..., size_mm=200., verbose=True), 'out.3mf')
```

Every mesh is checked watertight and the funnel prints per-stage feature counts
— watch for a cliff.

For the console, **`node --check` is not enough**. It only validates syntax, so
a call to a function that no longer exists passes cleanly. This bit twice
(deleted `drawLog`/`step`, deleted `abortAll`).

`test_console.js` is that harness. No dependencies, no network, no browser:

```bash
node test_console.js                    # 26 checks, exit 0 = clean
node test_console.js old-console.html   # point it at an older copy
```

It stubs the DOM, Leaflet and three.js, evaluates the console's inline script in
a `vm` context, and then **invokes** every top-level function, failing only on
`is not defined` / `is not a function` — the dead-call class that `node --check`
misses. On top of that it regression-tests the ghost rectangle, the elapsed
clock, the colour pipeline, the repeat-preview dispose and the cover handling.
Verified against the pre-fix console: 14 of the original 19 fail there, and all
7 filament checks fail against the pre-picker console, so they are real tests
and not tautologies. The repeat-preview crash was *found* by this
harness rather than fixed into it — calling `buildExactScene` twice is not
something you would think to do by hand.

Two things to know if you edit it. `vm.runInContext` puts `function` and `var`
declarations on the sandbox global but **not** top-level `const`/`let`, and much
of this console is `const name = (…) => …`; the harness appends a `var __C = {…}`
export block built from the source to reach those. And the stub `THREE.Color`
implements the real sRGB→linear transfer function, so the colour check measures
something instead of asserting against itself.

## Known gaps / next steps

- `test_offline.py` is still missing. It was never committed and is not in the
  Recycle Bin, so it has to be rewritten from the snippet in Testing, below.
- **Untracked files in this folder are not safe.** `landmarks.json` and every
  `.3mf` were deleted out from under the project and only came back from the
  Recycle Bin; `git log --all` has no record of either, because neither was
  ever committed. `landmarks.json` is tracked now. `test_offline.py` was not so
  lucky. Anything that is source and not output belongs in git the day it is
  written.
- **A preview is slow enough to look broken.** One 1.6 km London tile measured
  11m15s end to end: 4m10s fetching buildings and 4m17s fetching roads, against
  ~15s of actual meshing. The console's copy promises "usually a few minutes".
  Almost all of it is Overture round-trips over S3, and it is the main reason
  the preview "doesn't work" anywhere unfamiliar — people give up, or a
  transient S3/DNS failure lands inside that window. Worth a look: caching
  `available_columns` across runs, narrowing the released columns, or a local
  parquet mirror.
- Cover seam and print orientation, above.
- Railway deployment: `serve.py` should move over unchanged. Jobs are in-memory
  and capped at 6, so a restart loses them.
- Attribution obligations: ODbL for OpenStreetMap, CDLA for Overture,
  OpenFreeMap/OpenMapTiles if the tile path is ever re-enabled. Currently shown
  in the console footer. Selling printed landmark models (the CN Tower has a
  licensed merch programme) is a question for a lawyer, not for this file.
