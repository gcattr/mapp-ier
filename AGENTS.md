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

**Nothing may hang in mid-air.** A `building:part` carries its own
`min_height` and nothing guarantees anything is underneath it. Overture is full
of parts that start a hundred metres up with no volume below — masts, aerials,
upper platforms — and they came out as debris floating over the city, worst
around the Eiffel Tower.

The distinction that matters is **overhang vs. floating**, and the test is
overlap in plan: a part is supported when another part of the same building
starts lower *and* overlaps it from above. That keeps the CN Tower's
observation pod (it overlaps the shaft) and catches an aerial hanging in space
(it overlaps nothing). `resolve_floating()`, `--floating-parts`:

- `ground` (default) — lower it onto whatever is below, or to the ground.
- `drop` — discard it.
- `keep` — emit it floating, as the data says.

Grounding has a limit. A 2 m aerial 300 m up becomes a 300 m column one nozzle
wide: fragile, ugly, and it sets `tallest`, which sets the cover height for the
whole model. Past `--floating-aspect` (12 : 1 height to width) the part is
dropped instead. Both counts are in the funnel.

**Synthesised tapers are opt-in.** `spire_taper=False` by default. Turning it on
needles the top of *every* parts-bearing building, which spikes the whole city.
The CN Tower's flare only exists via `--landmarks`; OSM/Overture model the legs
as constant-section prisms, so every faithful renderer draws them straight.

**DuckDB geometry.** Use `ST_AsWKB(geometry)`, not the bare column — loading the
`spatial` extension converts GeoParquet geometry to DuckDB's internal GEOMETRY
type on some builds, which shapely can't parse ("Input buffer is smaller than
requested object size"). There are fallbacks and a tolerant WKB reader.

**Terrain resolution follows the tile.** `terrain_zoom` defaults to 0, meaning
`terrain_zoom_for()` picks one so the DEM is about as fine as the mesh grid. A
fixed z14 is a 156543·cos(lat)/2¹⁴ pixel — roughly 8 m at Tokyo — so a 700 m
tile got ~90 real samples across and looked faceted no matter how high
`terrain_grid` went: the mesh was interpolating detail that was never in the
data. Now a 700 m tile gets z15 (~3.9 m) and a 3 km tile stays at z14, so big
tiles do not download hundreds of PNGs for detail they cannot show.
`terrain_grid` went 260 → 400 to match. Terrarium has nothing useful past z15,
hence `terrain_zoom_max`.

Stair-stepping on a steep slope is the DEM's own quantisation, not the grid;
finer zoom reduces it but does not remove it.

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

Each plate is written as a **real Bambu Studio project**, so it opens with the
right colour and filament already in every AMS slot. Getting there took four
things, and leaving out any one of them silently breaks it:

**1. It must identify as a Bambu project.** `xmlns:BambuStudio`,
`<metadata name="BambuStudio:3mfVersion">1</metadata>`, the production
extension (`xmlns:p`, `requiredextensions="p"`) and a `p:UUID` on every object,
component, build and item. Without the marker Bambu ignores
`project_settings.config` entirely; *with* the marker but without the rest, it
refuses to load the file at all — no output, no error.

**2. One object, one part per layer.** Not one object per layer. Our four
layers occupy the same space, and Bambu's arrange treats four colliding objects
as four things to spread out — it put a plate on **four separate plates**. As
components of a single object they stay put and the tile prints as one piece.
Extruders are set per `<part>` in `model_settings.config`.

**3. The project config must be Bambu's own, and internally consistent.** A
partial config is not merged, it is *discarded*, and the plate falls back to a
single filament — which is exactly how four objects on extruders 1-4 all came
out one colour. `bambu_p1s_0.4.json` is a config Bambu Studio wrote, shipped
alongside the exporter; `_project_settings()` widens the per-slot lists and
writes our filaments in.

The trap is which keys are per-slot. Twenty of them are, and **five do not
start with `filament`** — `nozzle_temperature`,
`nozzle_temperature_initial_layer`, `long_retractions_when_ec`,
`retraction_distances_when_ec`, `slow_down_min_speed`. Widen only the
`filament*` keys and the config is inconsistent, Bambu throws all of it away,
and the plate prints in one colour with nothing in the file looking wrong.
`PER_FILAMENT` and `PER_PRESET` were derived, not guessed: export the same
model from Bambu twice, once with one filament and once with four, and diff.
Keys going 1→4 are per-filament; keys going 3→6 are per-preset (process +
printer + each filament).

**4. Put the plate on the bed.** Our geometry is built around the origin with
negative Z — that is the bed's *corner*, and below the plate. The offset rides
in the build item's transform, so the mesh is untouched and `serve.py` still
reads what it always did.

**Slots are per filament, not per part.** Two parts in the same filament share
a slot: you cannot load one spool into two trays, and pretending otherwise
makes Bambu treat the plate as multi-colour — prime tower and filament swaps
for nothing. The cover is the obvious case, two halves of one PETG shell.
A buyer who picks the same colour twice needs three spools, not four.
`slot_map()` is the one place this is decided, and the console mirrors it.

`--no-bambu-project` writes a plain 3MF instead: geometry plus the standard
materials extension (`<basematerials>` with per-object `pid`/`pindex`, and
`<m:colorgroup>`), for any other slicer. Neither is in `requiredextensions`, so
a slicer that knows no material extension still loads the geometry.

The Bambu SKU codes (`GFA00` = PLA Basic, `GFA01` = PLA Matte, `GFG02` =
PETG HF) and the preset names are not guesses: they were read out of the
profiles that ship with Bambu Studio, at
`resources/profiles/BBL/filament/<name> @base.json`.

### Verifying it, because none of this is visible in the file

A plate with a discarded config **looks perfect**. Every colour is in it, every
extruder is assigned; the failure only appears in the slicer. So there is a
check that asks Bambu Studio itself:

```bash
python verify_bambu.py                     # all three plate shapes
python verify_bambu.py 3Dmodels/x/x.3mf    # a plate you already have
```

It loads a plate through the Bambu Studio CLI, has it re-export the project,
and reads the filaments and per-part extruders back out of what Bambu wrote.
For a supplied file the expectation comes from the file's own
`project_settings.config` — whatever it asked for is what Bambu must read back.
It skips cleanly when Bambu Studio is not installed.

The control matters: a real MakerWorld 4-colour project survives the identical
round-trip with all four colours and its printer. Without that, a passing
round-trip would prove nothing about the method.

Regenerate the template (paths shift between Bambu versions):

```bash
bambu-studio.exe any.3mf \
  --load-settings "<profiles>/BBL/machine/Bambu Lab P1S 0.4 nozzle.json;\
<profiles>/BBL/process/0.20mm Standard @BBL X1C.json" \
  --load-filaments "<profiles>/BBL/filament/Bambu PLA Basic @BBL P1S 0.4 nozzle.json" \
  --export-3mf out.3mf
```

then take `Metadata/project_settings.config` out of `out.3mf`. Load **one**
filament, so every per-slot list has length 1 and widening is deterministic.

## Fetching

The six Overture scans run **at once** (`fetch_workers`, default 6). Almost all
of a build is waiting on S3, not meshing: one London tile measured 4m10s on
buildings and 4m17s on roads against ~15s of actual geometry. Nothing depends
on anything else, so the wall time becomes the slowest single query instead of
the sum of six.

Each worker gets `con.cursor()`, **not** the shared connection. A DuckDB
connection is not safe across threads; cursors off one connection are, and they
share the loaded httpfs/spatial extensions and the S3 credentials, so this
costs nothing per query. `--fetch-workers 1` puts it back to sequential.

This does not change the empty-result handling: `_confirm_empty()` still runs
per layer, and `FETCH_ERRORS` is appended under the same lock-free pattern it
always used (each worker touches a different layer key).

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

**The map's zoom buttons are on the right.** Leaflet defaults them to the top
left, which is exactly where the search box and "Drag a tile" are — the +/−
sat on top of them. `zoomControl: false`, then added back at `topright`; the
attribution is bottom right and the tile controls bottom left, so that corner
is the free one.

**The picker is a stock list, not a colour wheel.** It used to be an
`<input type="color">` per layer, which let a buyer choose any of 16 million
colours, approximately nine of which the shop can print. It is now a chip
showing the current colour, which opens an inline grid of the stocked
filaments grouped by finish.

**Every colour is shown as a colour.** A list of 18 names asks a buyer to know
what "Nardo Gray" looks like, which nobody does. Each option is a swatch plus
its name, and the chip carries the swatch too, so the panel can be read at a
glance. The grid is inline and toggled open rather than a floating menu: no
positioning to get wrong near the bottom of a phone screen, and it cannot open
off-screen. One grid open at a time, and picking closes it — one tap, done.

`.ggrid` uses `repeat(3, minmax(0, 1fr))`, not `repeat(3, 1fr)`. Plain `1fr` is
`minmax(auto, 1fr)`, so a long name like "Bambu Green" widens its own column
and the swatches stop lining up.

**`col` is derived, never set.** `pick` (layer -> filament key) is the buyer's
order; `col` is only its rendering, recomputed by `colFromPick()`. Setting a
colour directly is how the swatch, the preview and the copied command drift
apart.

**The preview trusts the file, not the panel.** `serve.py` reads each object's
colour back off `basematerials` and sends it with the mesh, and
`buildExactScene()` prefers that over the panel's own idea. If the two ever
disagree the file wins, because the file is what prints.

**The preview is honest about what it is.** The disclaimer used to read "the
same geometry the slicer receives", which is true and still misleading: it is
the real geometry, but it is not a photograph of the finished print. Layer
lines, gloss, and how a matte filament catches the light all change how it
looks in the hand, and a screen cannot show any of them. Say so, or the first
complaint is that the model "doesn't look like the picture".

**Three tabs, and `showTab` takes a name.** Map / 3D preview / Print it
yourself. It used to take a boolean, which stopped working the moment there
were three panes; it still accepts `true`/`false` because `buildPreview()` and
the resize handler call it that way.

**The camera orbits, pans and pinches.** One finger spins, two fingers slide
and pinch, right-drag (or shift-drag) slides on a mouse. Pointers are tracked
in a `Map` rather than a single `drag`, for two reasons that are both bugs if
you get them wrong: a second finger landing mid-drag has to upgrade the gesture
instead of being ignored, and lifting one of two fingers must continue from
where the remaining finger is — otherwise the model jumps by the distance
between them. The canvas also sets `touch-action: none`; without it the browser
scrolls the page and on a phone the model simply will not move.

**The server-side job is not cancelled.** Starting a new preview abandons the old
job rather than stopping it; it runs to completion, holding a slot in the
6-job cap. There is no `/api/cancel`.

## The walkthrough

Six steps, skippable at every one, and shown on **every** visit — not just the
first. Buyers arrive from an Etsy listing months apart and on whatever device
is to hand, so "you saw this once in a browser you no longer use" is not a
reason to drop someone into an unexplained map; it is one tap to leave.
"? Help" in the tab bar reopens it. `localStorage['m2m.tour.v1']` is still
written but not acted on — it is the one signal that someone has been here
before, if that is ever wanted — and every storage access is wrapped in
try/catch, because a page that will not load is worse than a forgotten flag.

**Skip has to look like a button.** As a borderless grey word it read as a
caption, so the only obvious way out of the card was six taps of Next.

It is written for someone who has never used anything like this — an Etsy
buyer, on a phone, who wants a model of their street. **No jargon.** A test
fails the build if the copy contains "3mf", "filament slot", "AMS", "exporter",
"STL", "LOD", "CLI", "terminal" or "command line". Two more tests pin the
things that cost money if they are missed:

- the size step must say **the same size you picked on Etsy** — if it does not
  match the order, the buyer gets a model they did not pay for;
- the preview step must say it takes **minutes** *and* that you do not have to
  wait, or buyers sit watching a spinner believing the order depends on it.

`maybeOpenTour()` is called last in the startup sequence, so a first-time
visitor sees a drawn page behind the card rather than a blank one. There is a
test that the startup sequence actually calls it: the function existed, was
correct, and was never wired up, so nobody would have seen the tour at all.

## What is deliberately not in the console

- **A "Detail" panel** (data source, LOD, roof mode, ridge height). A buyer
  ordering a souvenir has no opinion about LOD2 versus LOD1, and every extra
  control was one more way to order something they did not mean. The values
  live in the `DETAIL` constant and are exactly what the panel defaulted to, so
  nothing about the output changed. Change them there, not in `buildCmd()`.
- **"Open exported 3MF"**. It was for the seller, not the buyer, and it sat in
  the tab bar of a customer-facing page. `openExact()` and `renderExact()` are
  still in the source but nothing reaches them — dead like the tile path.
- **The cover colour.** Not a buyer choice; see Filaments.

## Print it yourself

A third tab points at the tools that hand you a file, for visitors who have a
printer at home: MiniSkyline, TerraPrinter, TrailPrint3D, Topo Trail,
TouchTerrain, Touch Mapper, Terrain2STL and Blosm for Blender. All eight URLs
were opened and confirmed to resolve to the right site. Sending someone
elsewhere costs nothing and beats a bounce; the page says plainly that none of
them are endorsed.

## Mobile

Buyers arrive from an Etsy listing, which is overwhelmingly a phone, so the
narrow layout is the common case and not an afterthought. Two breakpoints:
940 px stacks the map above the panel; 560 px is the phone pass — 44 px touch
targets (Apple's minimum) on the chips, swatches and tour buttons, a two-column
swatch grid instead of three, and a shorter map that still leaves room to aim a
tile.

Verified in a real browser at desktop width: tour, picker, tab switching and
the copied command. The 560 px block could not be exercised — the window would
not resize on this display — so it was checked by reading the parsed
stylesheet back out of the browser and confirming all 19 rules survived.

## Testing the exporter

`test_export.py` — no network, no DuckDB, throwaway tetrahedra for meshes.
What it covers is everything *after* geometry: which filament each layer gets,
what lands in the 3MF, and whether a slicer can read it back.

```bash
python test_export.py                   # 26 checks, exit 0 = clean
```

Every check in it is a bug that shipped. The cover one in particular: the cover
is written as `cover_left` and `cover_right`, which matched no key in
`DEFAULT_FILAMENTS`, so both silently fell through to `basic_gray` — the
shipping shell was being specced in a buyer's PLA instead of PETG.
`filament_of()` folds `cover_*` to `cover` now.

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
node test_console.js                    # 45 checks, exit 0 = clean
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
- **A preview is still slow, just no longer additive.** The six Overture scans
  now run at once (see **Fetching**), so a build costs the slowest query rather
  than the sum — a 1.6 km London tile was 11m15s end to end, of which 4m10s was
  buildings and 4m17s was roads. That is the big win taken; what is left is the
  single slowest scan, and it is still minutes. Not yet tried: caching
  `available_columns` across runs, narrowing the released columns, or a local
  parquet mirror. **Measured before the change, not after** — nobody has timed
  a real tile since the layers went parallel.
- **Stair-stepping on steep ground** is the DEM's own quantisation, not the
  mesh grid. `terrain_zoom_for()` picks a finer zoom for small tiles, which
  reduces it; terrarium has nothing past z15, so removing it entirely would
  mean smoothing the heightmap and trading away real relief. Deliberately not
  done — the trade is a judgement call, not a bug fix.
- **The Bambu project template is version-coupled.** `bambu_p1s_0.4.json` was
  written by Bambu Studio 02.05.00.66. If Bambu changes its settings schema the
  config may stop being accepted, and the failure is silent in the file — the
  plate just reverts to one filament. `verify_bambu.py` is what catches it;
  run it after a Bambu update. The regeneration command is in **Bambu Studio
  project metadata**.
- Cover seam and print orientation, above.
- **The 560 px phone layout has never been seen on a phone.** The rules were
  confirmed to parse by reading the stylesheet back out of a browser, but the
  window would not resize on the development display, so nothing was rendered
  at that width. Worth ten seconds on a real handset.
- **`--floating-parts` has only been exercised on synthetic shapes.** The rule
  is unit-tested against CN-Tower-shaped and Eiffel-shaped inputs, but no real
  tile has been exported since. Watch the two funnel counts ("floating parts
  sat down…", "…dropped") on the next Tokyo or Paris run: a large drop count on
  an ordinary city block means the support test is too strict.
- Railway deployment: `serve.py` should move over unchanged. Jobs are in-memory
  and capped at 6, so a restart loses them.
- Attribution obligations: ODbL for OpenStreetMap, CDLA for Overture,
  OpenFreeMap/OpenMapTiles if the tile path is ever re-enabled. Currently shown
  in the console footer. Selling printed landmark models (the CN Tower has a
  licensed merch programme) is a question for a lawyer, not for this file.
