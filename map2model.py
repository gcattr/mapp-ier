#!/usr/bin/env python3
"""
map2model.py — turn any map coordinates into a printable, 5-layer 3MF city model.

Layers (separate 3MF objects, so a slicer can assign a filament to each):
    terrain | water | greenery | roads | buildings

Key differences from a naive footprint-extrusion pipeline:
  * reads BOTH `building` and `building_part` from Overture, so landmarks with
    real 3D decomposition (CN Tower, Eiffel Tower, Sagrada Familia, ...) come out
    with their actual silhouette instead of a cylinder
  * honours min_height (floating / stacked parts) and roof_shape / roof_height
  * bbox test is an OVERLAP test, not containment -> nothing on the tile edge is lost
  * height fallback chain, MultiPolygon handling, make_valid, and a per-stage
    drop counter so you can see exactly where features die
  * printability pass: thin spires inflated to nozzle width, everything embedded
    into terrain, watertight output

Install:
    pip install duckdb shapely numpy pyproj trimesh mapbox_earcut pillow requests

Usage:
    python map2model.py --center 43.6426 -79.3871 --radius 1200 -o toronto.3mf
    python map2model.py --bbox -79.40 43.63 -79.37 43.66 -o toronto.3mf

    # four-filament land model + a separate flat water insert + a shipping frame
    python map2model.py --center 43.6426 -79.3871 --radius 1000 --size 200 \
        --frame --frame-depth 7 --frame-width 6 --frame-clearance 0.3 \
        -o toronto.3mf

Water is a FLAT plate at sea level and the terrain beneath it is carved into a
basin, so the water can never stand proud of the land and the plate can be
printed on its own and dropped in.

With --frame you get a sixth object: a tray sized to the model plus clearance.
Print terrain + roads + greenery + buildings together (4 filaments), then the
water insert, then the frame. --frame-depth controls how far the model sinks in
(raise it if the model has to survive shipping); --frame-clearance is the fit
tolerance per side.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import urllib.parse
import urllib.request
import math
import os
import sys
import zipfile
from dataclasses import dataclass, field
from collections import Counter, defaultdict

import numpy as np

__version__ = "0.9.0-osm"

# ----------------------------------------------------------------------------
# Timing / progress
# ----------------------------------------------------------------------------

import time

_T0 = time.time()
_STAGES = []


def _hms(s):
    return f"{int(s)//60:d}m{s % 60:04.1f}s" if s >= 60 else f"{s:.1f}s"


class Stage:
    """Context manager that announces a stage and records how long it took."""

    def __init__(self, name, verbose=True):
        self.name = name
        self.verbose = verbose

    def __enter__(self):
        self.t = time.time()
        if self.verbose:
            print(f"[{time.time()-_T0:6.1f}s] -> {self.name}", file=sys.stderr, flush=True)
        return self

    def __exit__(self, *a):
        d = time.time() - self.t
        _STAGES.append((self.name, d))
        if self.verbose:
            print(f"[{time.time()-_T0:6.1f}s]    {self.name}: {_hms(d)}",
                  file=sys.stderr, flush=True)
        return False


class Ticker:
    """Live progress for long loops, with an ETA. Prints at most every `every` s."""

    def __init__(self, total, label, verbose=True, every=2.0):
        self.total = max(int(total), 1)
        self.label = label
        self.verbose = verbose
        self.every = every
        self.t0 = time.time()
        self.last = 0.0

    def tick(self, i):
        if not self.verbose:
            return
        now = time.time()
        if now - self.last < self.every and i + 1 < self.total:
            return
        self.last = now
        frac = (i + 1) / self.total
        el = now - self.t0
        eta = el / max(frac, 1e-9) - el
        print(f"\r[{now-_T0:6.1f}s]    {self.label}: {i+1}/{self.total} "
              f"({frac*100:4.1f}%)  eta {_hms(eta)}   ",
              end="" if i + 1 < self.total else "\n", file=sys.stderr, flush=True)


def print_timings():
    print("\n--- timings ---", file=sys.stderr)
    for name, d in _STAGES:
        print(f"  {name:<34s} {_hms(d):>10s}", file=sys.stderr)
    print(f"  {'TOTAL':<34s} {_hms(time.time()-_T0):>10s}\n", file=sys.stderr)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# 1b. Filaments
# ----------------------------------------------------------------------------
# The shop stocks these and only these. Hex codes are Bambu's own, read off
# "Bambu Lab Filament Hex Code Table" for PLA Basic and PLA Matte -- the two
# PDFs in this folder. Do not eyeball a colour: the preview, the 3MF and the
# printed part all key off this table, so a wrong hex is a wrong sale.
#
# filament_id is Bambu's SKU code and is what Bambu Studio matches an AMS tray
# against: GFA00 = PLA Basic, GFA01 = PLA Matte, GFG02 = PETG HF. It is read
# straight from the profiles that ship with Bambu Studio
# (resources/profiles/BBL/filament/<name> @base.json).
#
# NOTE two pairs collide by hex: Matte Ivory White and Basic Jade White are
# both #FFFFFF, and Matte Charcoal and Basic Black are both #000000 (that is
# what Bambu's table says). They render identically in the preview and differ
# only in finish, which is why the picker shows the name, not just a swatch.

FILAMENTS = {
    # key                  display name              type  bambu id  hex
    "basic_jade_white":  ("PLA Basic Jade White",   "PLA", "GFA00", "#FFFFFF"),
    "basic_gray":        ("PLA Basic Gray",         "PLA", "GFA00", "#8E9089"),
    "basic_red":         ("PLA Basic Red",          "PLA", "GFA00", "#C12E1F"),
    "basic_orange":      ("PLA Basic Orange",       "PLA", "GFA00", "#FF6A13"),
    "basic_yellow":      ("PLA Basic Yellow",       "PLA", "GFA00", "#F4EE2A"),
    "basic_bambu_green": ("PLA Basic Bambu Green",  "PLA", "GFA00", "#00AE42"),
    "basic_cocoa_brown": ("PLA Basic Cocoa Brown",  "PLA", "GFA00", "#6F5034"),
    "basic_blue":        ("PLA Basic Blue",         "PLA", "GFA00", "#0A2989"),
    "basic_black":       ("PLA Basic Black",        "PLA", "GFA00", "#000000"),
    "matte_grass_green": ("PLA Matte Grass Green",  "PLA", "GFA01", "#61C680"),
    "matte_marine_blue": ("PLA Matte Marine Blue",  "PLA", "GFA01", "#0078BF"),
    "matte_ice_blue":    ("PLA Matte Ice Blue",     "PLA", "GFA01", "#A3D8E1"),
    "matte_desert_tan":  ("PLA Matte Desert Tan",   "PLA", "GFA01", "#E8DBB7"),
    "matte_dark_green":  ("PLA Matte Dark Green",   "PLA", "GFA01", "#68724D"),
    "matte_ivory_white": ("PLA Matte Ivory White",  "PLA", "GFA01", "#FFFFFF"),
    "matte_charcoal":    ("PLA Matte Charcoal",     "PLA", "GFA01", "#000000"),
    "matte_nardo_gray":  ("PLA Matte Nardo Gray",   "PLA", "GFA01", "#757575"),
    "matte_caramel":     ("PLA Matte Caramel",      "PLA", "GFA01", "#AE835B"),
    # The cover is not a customer choice. It is a shipping shell, printed in
    # whatever PETG is on the shelf for toughness, and it is never on a plate
    # with a colour the buyer picked.
    "petg_cover":        ("PETG (cover)",           "PETG", "GFG02", "#7F8285"),
}

# Which filament each layer gets when the buyer does not say. These are the
# shop defaults and they are what the console starts on.
DEFAULT_FILAMENTS = {
    "terrain":   "matte_grass_green",
    "greenery":  "basic_jade_white",
    "roads":     "basic_black",
    "buildings": "basic_gray",
    "water":     "matte_marine_blue",
    "frame":     "basic_black",
    "route":     "basic_red",
    "cover":     "petg_cover",
}

# Elevation bands in --mode terrain. terrain_1 is the layer "terrain" (so the
# single-colour default is unchanged); terrain_2..terrain_5 default down this
# ramp - lowland green, upland green, tan, grey rock, snow. All stock keys.
TERRAIN_RAMP = ["matte_grass_green", "matte_dark_green", "matte_desert_tan",
                "matte_nardo_gray", "basic_jade_white"]


def _band_index(layer):
    """2..5 if `layer` is an elevation band key (terrain_2..terrain_5), else 0."""
    if layer.startswith("terrain_") and layer[8:].isdigit():
        n = int(layer[8:])
        if 2 <= n <= len(TERRAIN_RAMP):
            return n
    return 0

# Buyer-pickable layers, in the order the console shows them.
PICKABLE = ("terrain", "greenery", "roads", "buildings", "route", "water", "frame")


def filament_of(cfg, layer):
    """
    (name, type, bambu_id, hex) for a layer, falling back to the default.

    The cover is written as two objects, `cover_left` and `cover_right`, but it
    is one part with one setting. Without this fold they matched no key in
    DEFAULT_FILAMENTS and silently fell through to basic_gray -- the shipping
    shell was being specced in a buyer's PLA instead of PETG.
    """
    if layer.startswith("cover_"):
        layer = "cover"
    bi = _band_index(layer)
    if bi:
        # terrain_2..terrain_5: a pick if the buyer made one, else the ramp
        key = (cfg.filaments or {}).get(layer) or TERRAIN_RAMP[bi - 1]
        return FILAMENTS.get(key) or FILAMENTS[TERRAIN_RAMP[bi - 1]]
    key = (cfg.filaments or {}).get(layer) or DEFAULT_FILAMENTS.get(layer)
    return FILAMENTS.get(key) or FILAMENTS[DEFAULT_FILAMENTS.get(layer, "basic_gray")]


def parse_filaments(spec):
    """
    'terrain=matte_grass_green,roads=basic_black' -> dict.

    Unknown layer names and unknown filament keys are hard errors: silently
    ignoring one means a customer is quoted a colour and shipped another.
    """
    out = {}
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--filaments: expected layer=filament, got {chunk!r}")
        layer, key = (x.strip() for x in chunk.split("=", 1))
        if layer not in DEFAULT_FILAMENTS and not _band_index(layer):
            raise ValueError(f"--filaments: unknown layer {layer!r}; "
                             f"pick from {', '.join(sorted(DEFAULT_FILAMENTS))}"
                             f" (or terrain_2..terrain_5 for elevation bands)")
        if key not in FILAMENTS:
            raise ValueError(f"--filaments: unknown filament {key!r}; "
                             f"pick from {', '.join(sorted(FILAMENTS))}")
        out[layer] = key
    return out


@dataclass
class Config:
    # geography
    bbox: tuple = (-79.400, 43.636, -79.370, 43.655)   # minlon, minlat, maxlon, maxlat

    # what the tile is
    mode: str = "city"           # city | terrain | route. terrain and route
                                 # both drop buildings / roads / greenery.
                                 # terrain cuts the relief into elevation
                                 # colour bands; route lays a raised ribbon
                                 # along a GPS track, draped on the terrain.
    terrain_bands: int = 1       # 1 = smooth single-colour relief; 2..5 = that
                                 # many stepped elevation bands, each its own
                                 # filament (see TERRAIN_RAMP). Band 5 spills
                                 # to its own plate.
    # --mode route. The path arrives one of three ways; route_name keeps the
    # Etsy command short by resolving the geometry at build time instead of
    # baking hundreds of coordinates into a string a human has to paste.
    route: str = ""              # 'lat,lon;lat,lon;...' in order (drawn on the
                                 # console map, or a simplified GPX)
    route_name: str = ""         # OSM name to resolve instead, e.g.
                                 # 'Circuit de Spa-Francorchamps'
    route_file: str = ""         # file of 'lat,lon' lines, or a .gpx track
    route_width_m: float = 12.0  # ribbon width on the ground
    route_height_mm: float = 1.6  # how far the ribbon stands proud of the land
    # print size
    size_mm: float = 150.0        # longest horizontal dimension of the finished print
    tile_shape: str = "square"    # square | hex | circle. The vector layers clip
                                  # to it for free (clean_polys already intersects
                                  # bbox_poly); the frame and cover offset the
                                  # outline. hex is flat-top, circle inscribes the
                                  # square drag.
    z_exaggeration: float = 1.0   # 1.0 = true scale. 1.3-1.8 makes small towns read better
    base_mm: float = 3.0          # solid plinth under the terrain
    max_building_mm: float = 0.0  # clamp tallest building (0 = no clamp)

    # water is a FLAT plate at sea level, and the terrain under it is carved
    # into a basin so land never sits below the water surface
    water_depth_mm: float = 1.2      # how far the basin is cut below the datum
    sea_level_mm: float = 0.0        # water surface, relative to the land datum
    shore_ramp_mm: float = 3.0       # beach: the land plinth ramps DOWN to the
                                     # water datum over this much PRINT width at
                                     # the shore instead of dropping as a
                                     # vertical seawall. A real cliff keeps a
                                     # falloff at least this wide, so no shore
                                     # edge is ever a paper-thin sheet. 0 = hard
                                     # vertical cut (the old behaviour).

    # optional display frame + separable water insert
    frame: bool = False
    frame_width_mm: float = 5.0      # border wall thickness
    frame_depth_mm: float = 5.0      # how deep the model sits into the frame
    frame_floor_mm: float = 2.0      # tray floor thickness
    frame_lip_mm: float = 4.0        # ledge width when the floor is cut out
    frame_open: bool = False         # cut the floor out, leaving just a ledge
    frame_clearance_mm: float = 0.30  # fit tolerance per side
    # layer switches
    want_water: bool = True
    want_greenery: bool = True
    want_roads: bool = True
    want_buildings: bool = True
    building_scale: float = 1.0      # stretch buildings only, not the land
    # layer -> FILAMENTS key. Empty means "all defaults"; see DEFAULT_FILAMENTS.
    filaments: dict = field(default_factory=dict)
    bambu_project: bool = True       # also write the Bambu Studio project
                                     # metadata, so the plate opens with the
                                     # right filament in each AMS slot
    printer_model: str = "Bambu Lab P1S"
    nozzle_mm: float = 0.4

    box: bool = False                # two-part shipping box around the frame
    box_wall_mm: float = 3.0
    box_floor_mm: float = 2.0
    box_clearance_mm: float = 0.4    # slip fit between box parts
    cover_lip_mm: float = 6.0        # how far the lips reach under the frame
    cover_lip_thickness_mm: float = 2.4
    box_headroom_mm: float = 10.0    # clear air above the tallest building
    box_cube: bool = True            # grow the cover up to a cube so a flat
                                     # tile still fills a cube shipping box;
                                     # never shrinks a tall cover, clamped to
                                     # build_volume_mm. --no-box-cube opts out.
    # P1S. The machine is 256 mm in all three axes, but 250 is the working
    # limit for anything printed here - it leaves margin and it is one number
    # to remember. The console has always used 250; this used to say 256, so
    # the exporter passed plates the console warned about.
    build_volume_mm: float = 250.0   # every plate, every axis

    water_in_frame: bool = False     # water becomes the frame's floor and
                                     # the land model is cut through where
                                     # water should show
    split: bool = False              # write model / water / frame as separate
                                     # files, one plate each
    frame_inplace: bool = False      # nest the frame for preview instead of
                                     # parking it beside the model for printing

    # terrain
    terrain: bool = True
    # 0 = pick from the tile size so the DEM is about as fine as the mesh
    # grid; a fixed 14 is ~8 m per sample at mid latitudes, which is why a
    # small tile came out visibly faceted. See terrain_zoom_for().
    terrain_zoom: int = 0         # AWS terrarium tile zoom (0 = auto, 12-15)
    terrain_zoom_max: int = 15    # terrarium has no useful detail past this
    terrain_grid: int = 400       # heightmap samples along the long axis
    terrain_relief_mm: float = 0.0  # 0 = true relief; >0 = normalise relief to this

    # layer thicknesses (mm, above the terrain surface)
    water_mm: float = 0.6
    greenery_mm: float = 0.8
    roads_mm: float = 0.5

    # BLANKET GREENERY. base/land_cover is derived from a coarse global raster
    # and dissolved into continent-sized multipolygons: the 'forest' feature
    # over Monaco is 1.4 million km2, half a million times the tile, and it
    # buries the whole city under one green sheet. A real park is a fraction of
    # the tile; nothing legitimate is both tile-covering AND vastly larger than
    # the tile. Drop a greenery feature only when it is both.
    green_blanket_frac: float = 0.85    # of the tile it would cover, clipped
    green_blanket_scale: float = 25.0   # source polygon vs. tile area

    # An empty Overture result can mean "nothing here" or "the S3 scan quietly
    # dropped files". Re-run a query that returned nothing before believing it.
    retry_empty_fetch: bool = True
    # Overture scans run at once; the wall time becomes the slowest query
    # rather than the sum of six. 1 = sequential.
    fetch_workers: int = 6
    road_width_m: dict = field(default_factory=lambda: {
        "motorway": 22.0, "trunk": 18.0, "primary": 15.0, "secondary": 12.0,
        "tertiary": 10.0, "residential": 8.0, "living_street": 7.0,
        "unclassified": 8.0, "service": 5.0, "pedestrian": 5.0,
        "footway": 3.0, "steps": 3.0, "path": 3.0, "cycleway": 3.0,
    })
    road_classes: tuple = ("motorway", "trunk", "primary", "secondary",
                           "tertiary", "residential", "living_street",
                           "unclassified", "pedestrian")

    # buildings
    default_floor_height_m: float = 3.2
    default_building_height_m: float = 8.0   # last-resort fallback, never drop a row
    min_building_height_mm: float = 0.45     # anything shorter is bumped up so it prints
    min_footprint_m2: float = 6.0            # only drops genuine slivers

    # FLOATING PARTS. A building:part carries its own min_height, and nothing
    # guarantees anything is underneath it. Overture is full of parts that
    # start 100 m up with no parent volume below - masts, aerials, upper
    # platforms - and they come out as debris hanging in mid-air. An OVERHANG
    # is fine and prints (the CN Tower pod sits on the shaft); a part with
    # nothing at all beneath it is not.
    #   ground - lower it onto whatever is below, or to the ground  (default)
    #   drop   - discard it
    #   keep   - emit it floating, as the data says
    floating_parts: str = "ground"
    # ...except when grounding would make a needle. A 2 m aerial 300 m up
    # becomes a 300 m column one nozzle wide: fragile, ugly, and it sets the
    # cover height for the whole model. Past this height-to-width ratio the
    # part is dropped instead.
    floating_max_aspect: float = 12.0
    floating_tol_m: float = 0.5              # slop before calling it a gap
    source: str = "overture"     # 'overture' or 'osm' (Overpass)
    lod: int = 2                 # 1 = one prism per building outline
                                 # 2 = building parts + roof shapes
    use_building_parts: bool = True
    roof_shapes: bool = True
    # Roof policy. Rotationally symmetric roofs (dome, cone, pyramidal, onion)
    # are derived from the footprint alone, so they cannot face the wrong way.
    # Ridge roofs (gabled, hipped, skillion...) need an AXIS, which comes from
    # minimum_rotated_rectangle - and on a near-square footprint a 1 m
    # difference flips that axis 90 degrees, turning the building into a wedge
    # pointing the wrong way. They are therefore off by default.
    roof_mode: str = "symmetric"   # 'none'|'symmetric'|'safe'|'all'

    # vertical taper (aesthetic, not from data - Overture parts are prisms)
    tower_batter: float = 0.0    # OFF by default. A blanket shape rule cannot
                                 # tell a landmark from an office tower, so this
                                 # is opt-in and landmark-scoped (see below).
    batter_aspect: float = 4.0   # only parts at least this tall vs. wide
    taper_landmarks_only: bool = True   # batter only touches buildings that
                                        # carry building_part data
    landmarks: tuple = ()        # per-building taper overrides (see --landmarks)
    ridge_roof_max_frac: float = 0.98   # gabled/hipped/skillion cap
    roof_height_max_frac: float = 0.95  # pointed roofs (spires) cap
    _unused_roof_note: str = ""         # how much of a part's height roof_height
                                        # may occupy. THIS is where real tapers
                                        # live: S3DB expresses a cone/pyramid as
                                        # a roof, and clamping it flattens them.
    taper_power: float = 0.7     # <1 narrows fast low down, like a real tower
    taper_steps: int = 6
    spire_taper: bool = False    # OFF. Synthesising a taper on every
                                 # slender top part spikes the whole city;
                                 # real tapers come from roof_shape or a
                                 # landmark rule.
    spire_aspect: float = 7.0
    spire_tip_frac: float = 0.35  # fraction of height that becomes the needle
    spire_tip_ratio: float = 0.30  # needle tip width, relative to the shaft

    # printability
    min_feature_mm: float = 0.9   # ~2 perimeters at a 0.4 nozzle; inflates thin spires
    embed_mm: float = 0.35        # how far layers sink into what's below them

    # data
    duckdb_memory: str = "6GB"
    duckdb_threads: int = 0       # 0 = DuckDB default
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    osm_timeout: int = 180
    overture_release: str = ""    # "" -> resolve latest from the STAC catalog
    verbose: bool = True


# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------

STATS = Counter()


def log(cfg, *a):
    if cfg.verbose:
        print(*a, file=sys.stderr, flush=True)


def stage(name, n):
    STATS[name] = n


def print_funnel():
    print("\n--- feature funnel (watch for the cliff) ---", file=sys.stderr)
    for k, v in STATS.items():
        print(f"  {k:<34s} {v:>8d}", file=sys.stderr)
    print("", file=sys.stderr)


# ----------------------------------------------------------------------------
# 1. Overture fetch
# ----------------------------------------------------------------------------


def resolve_release(cfg) -> str:
    """Ask Overture's STAC catalog for the current release so paths never go stale."""
    if cfg.overture_release:
        return cfg.overture_release
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    try:
        for q in ("SELECT latest FROM 'https://stac.overturemaps.org/catalog.json'",
                  "SELECT latest FROM read_json_auto('https://stac.overturemaps.org/catalog.json')"):
            try:
                return str(con.execute(q).fetchone()[0])
            except Exception:
                continue
        raise RuntimeError("no usable STAC response")
    except Exception as e:
        fallback = "2026-07-22.0"
        print(f"[warn] STAC lookup failed ({e}); using {fallback}", file=sys.stderr)
        return fallback


def _con(cfg):
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    # The transportation partition is enormous. Without these, a wide bbox can
    # blow the memory budget and DuckDB aborts with "Query interrupted".
    for pragma in (f"SET memory_limit='{cfg.duckdb_memory}'",
                   "SET preserve_insertion_order=false",
                   "SET enable_progress_bar=false"):
        try:
            con.execute(pragma)
        except Exception:
            pass
    if cfg.duckdb_threads:
        try:
            con.execute(f"SET threads={int(cfg.duckdb_threads)}")
        except Exception:
            pass
    return con


_COLCACHE = {}


def available_columns(con, path):
    """Ask the parquet what it actually has. Schemas differ per type and per
    release - land_cover has no `class`, for instance - and guessing wrong
    aborts the whole query."""
    if path in _COLCACHE:
        return _COLCACHE[path]
    try:
        rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning=1) LIMIT 0"
        ).fetchall()
        cols = {r[0] for r in rows}
    except Exception:
        cols = None
    _COLCACHE[path] = cols
    return cols


# Every fetch that failed, as (theme/type, message). A failed fetch returns []
# exactly like an empty tile does, so without this the exporter cheerfully
# writes a bare terrain slab and the console reports success - which is how an
# empty Big Ben plate got produced. run() reads this to tell the two apart.
FETCH_ERRORS = []

# One empty-result retry per run; see _confirm_empty().
_EMPTY_RETRIED = [False]


def _confirm_empty(cfg, con, sql, path, theme, typ):
    """A zero-row result is not proof the tile is empty. Check, and retry once.

    `read_parquet('s3://…/*')` expands its glob with an S3 LIST. When that LIST
    fails or comes back short, DuckDB scans fewer files - or none - and returns
    ZERO ROWS WITH NO ERROR, which is indistinguishable from a tile that has
    nothing in it. Central London returned 0 roads this way, in 4m17s, silently;
    the identical query returned 562 a few minutes later.

    Two checks, both paid for only when the result was already empty. The glob
    costs about half a second and settles the listing question outright. The
    retry costs a full query, but a city tile with no roads at all is almost
    always a lie, and shipping a customer a roadless plate costs more.
    """
    try:
        files = int(con.execute(f"SELECT count(*) FROM glob('{path}')").fetchone()[0])
    except Exception as e:
        files = -1
        print(f"[warn] {theme}/{typ}: could not list {path} ({e})", file=sys.stderr)
    if files == 0:
        print(f"[warn] {theme}/{typ}: the S3 listing resolved to NO files, so the "
              f"empty result is a listing failure, not an empty tile.",
              file=sys.stderr)
        FETCH_ERRORS.append((f"{theme}/{typ}", "S3 glob resolved to no files"))
        return []
    if not cfg.retry_empty_fetch:
        return []
    # At most ONE retry per run. Every layer of a genuinely empty tile (mid
    # ocean, say) would otherwise retry, doubling a fetch that is already the
    # slowest part of the job - and once one layer has been confirmed empty the
    # tile really is bare, so the rest need no second opinion.
    if _EMPTY_RETRIED[0]:
        print(f"[info] {theme}/{typ} is empty too; not retrying again - this "
              f"tile looks genuinely bare", file=sys.stderr)
        return []
    _EMPTY_RETRIED[0] = True
    print(f"[info] {theme}/{typ} came back empty over {files} file(s); retrying "
          f"once before believing it", file=sys.stderr)
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"[warn] {theme}/{typ}: retry failed: {e}", file=sys.stderr)
        FETCH_ERRORS.append((f"{theme}/{typ}", str(e).strip()))
        return []
    if rows:
        print(f"[warn] {theme}/{typ}: the FIRST query returned nothing and the "
              f"retry returned {len(rows)} rows. The scan dropped data silently "
              f"- treat any empty layer here as suspect.", file=sys.stderr)
    return rows


def fetch(cfg, con, release, theme, typ, columns, extra_where=""):
    """
    Pull one Overture type inside the bbox.

    NOTE the predicate: this is an OVERLAP test. The classic bug is writing
        bbox.xmin > minx AND bbox.xmax < maxx ...
    which is a CONTAINMENT test and silently deletes every feature that crosses
    a tile edge — which is most large buildings.

    Returns [] both when the tile really is empty and when the query failed;
    a failure is recorded in FETCH_ERRORS so the caller can distinguish them.
    """
    minx, miny, maxx, maxy = cfg.bbox
    path = (f"s3://overturemaps-us-west-2/release/{release}/"
            f"theme={theme}/type={typ}/*")

    avail = available_columns(con, path)
    if avail is not None:
        missing = [c for c in columns if c not in avail]
        columns = [c for c in columns if c in avail] or ["id"]
        if missing:
            print(f"[info] {theme}/{typ}: no {', '.join(missing)} in this release "
                  f"- continuing without", file=sys.stderr)

    sel = ", ".join(f'"{c}"' for c in columns)

    # Geometry must come back as STANDARD WKB. Depending on the DuckDB build,
    # loading `spatial` turns a GeoParquet geometry column into DuckDB's
    # internal GEOMETRY type, whose binary layout is NOT WKB - shapely then
    # fails with "Input buffer is smaller than requested object size".
    # ST_AsWKB() forces the standard encoding. The bare column is the fallback
    # for builds that hand back a plain BLOB (where ST_AsWKB would not apply).
    geom_exprs = ["ST_AsWKB(geometry)", "ST_AsWKB(ST_GeomFromWKB(geometry))",
                  "geometry"]
    last = None
    for gexpr in geom_exprs:
        sql = f"""
            SELECT {sel}, {gexpr} AS wkb
            FROM read_parquet('{path}', hive_partitioning=1)
            WHERE bbox.xmin <= {maxx} AND bbox.xmax >= {minx}
              AND bbox.ymin <= {maxy} AND bbox.ymax >= {miny}
              {extra_where}
        """
        try:
            rows = con.execute(sql).fetchall()
            names = [d[0] for d in con.description]
            if not rows:
                rows = _confirm_empty(cfg, con, sql, path, theme, typ)
                names = [d[0] for d in con.description] if rows else names
            return [dict(zip(names, r)) for r in rows]
        except Exception as e:
            last = e
            msg = str(e).lower()
            if "interrupt" in msg or "memory" in msg:
                print(f"[warn] {theme}/{typ} failed: {e}", file=sys.stderr)
                print("       try a smaller --bbox, or raise --duckdb-memory",
                      file=sys.stderr)
                FETCH_ERRORS.append((f"{theme}/{typ}", str(e).strip()))
                return []
            continue
    print(f"[warn] {theme}/{typ} failed: {last}", file=sys.stderr)
    FETCH_ERRORS.append((f"{theme}/{typ}", str(last).strip()))
    return []


def rec_name(rec):
    """Overture stores names as a struct; pull the primary label if present."""
    n = rec.get("names")
    if isinstance(n, dict):
        return (n.get("primary") or "") or ""
    if isinstance(n, str):
        return n
    return ""


def landmark_rule(cfg, rec):
    """First matching override for this building, or None.

    A rule matches on a case-insensitive substring of the name, or an exact
    GERS id. Scoping the taper to named landmarks is the whole point: it can
    never leak onto the rest of the city the way an aspect-ratio rule does.
    """
    if not cfg.landmarks:
        return None
    name = rec_name(rec).lower()
    rid = str(rec.get("id") or "")
    for rule in cfg.landmarks:
        want_id = rule.get("id")
        want_name = (rule.get("name") or "").lower()
        if want_id and want_id == rid:
            return rule
        if want_name and name and want_name in name:
            return rule
    return None


_WKB_WARNED = [False]


def to_shape(blob):
    """Overture geometry arrives as WKB blobs; tolerate WKT and memoryviews too."""
    from shapely import wkb, wkt
    if blob is None:
        return None
    if isinstance(blob, (bytes, bytearray, memoryview)):
        raw = bytes(blob)
        try:
            return wkb.loads(raw)
        except Exception:
            # some builds prefix the payload; retry from the first plausible
            # WKB byte order marker rather than dropping the feature
            for off in (1, 4, 8):
                if len(raw) > off and raw[off] in (0, 1):
                    try:
                        return wkb.loads(raw[off:])
                    except Exception:
                        pass
            if not _WKB_WARNED[0]:
                _WKB_WARNED[0] = True
                print("[warn] could not decode a geometry as WKB; skipping "
                      "affected features", file=sys.stderr)
            return None
    if isinstance(blob, str):
        try:
            return wkt.loads(blob)
        except Exception:
            return wkb.loads(bytes.fromhex(blob))
    return None


OVERPASS = "https://overpass-api.de/api/interpreter"


def fetch_osm(cfg):
    """
    Plain OpenStreetMap buildings via Overpass.

    Deliberately reads only `building=*` outlines and ignores `building:part`
    entirely, which is what makes the CN Tower a single cylinder. This is
    CityGML LOD1: one footprint, one height, no parts, no landmark shaping.
    Slower and less detailed than Overture, but there is nothing here that can
    produce a stray wedge or a mis-oriented part.
    """
    from shapely.geometry import Polygon, MultiPolygon

    minx, miny, maxx, maxy = cfg.bbox
    q = (f"[out:json][timeout:{cfg.osm_timeout}];"
         f'(way["building"]({miny},{minx},{maxy},{maxx});'
         f' relation["building"]({miny},{minx},{maxy},{maxx}););'
         f"out geom;")
    req = urllib.request.Request(
        cfg.overpass_url, data=("data=" + urllib.parse.quote(q)).encode(),
        headers={"User-Agent": f"map2model/{__version__}"})
    with urllib.request.urlopen(req, timeout=cfg.osm_timeout + 30) as r:
        data = json.loads(r.read().decode())

    def ring(geom):
        pts = [(p["lon"], p["lat"]) for p in geom or []]
        return pts if len(pts) >= 3 else None

    def num(tags, *keys):
        for k in keys:
            v = tags.get(k)
            if v is None:
                continue
            try:
                return float(str(v).split()[0].replace(",", "."))
            except Exception:
                continue
        return None

    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        poly = None
        if el.get("type") == "way":
            r0 = ring(el.get("geometry"))
            if r0:
                poly = Polygon(r0)
        elif el.get("type") == "relation":
            outers, inners = [], []
            for mem in el.get("members", []) or []:
                r0 = ring(mem.get("geometry"))
                if not r0:
                    continue
                (outers if mem.get("role") != "inner" else inners).append(r0)
            if outers:
                try:
                    poly = MultiPolygon([Polygon(o, inners) for o in outers]) \
                        if len(outers) > 1 else Polygon(outers[0], inners)
                except Exception:
                    poly = Polygon(outers[0])
        if poly is None or poly.is_empty:
            continue
        h = num(tags, "height", "building:height")
        lv = num(tags, "building:levels", "levels")
        out.append(dict(id=el.get("id"), names={"primary": tags.get("name", "")},
                        height=h, min_height=num(tags, "min_height"),
                        num_floors=lv, roof_shape=tags.get("roof:shape"),
                        roof_height=num(tags, "roof:height"),
                        roof_direction=num(tags, "roof:direction"),
                        roof_orientation=tags.get("roof:orientation"),
                        has_parts=False, subtype=tags.get("building"),
                        **{"class": tags.get("building")},
                        wkb=poly.wkb))
    stage("buildings fetched (osm)", len(out))
    return out


def parse_route(spec):
    """'lat,lon;lat,lon;...' -> [(lon, lat), ...] in shapely x,y order.

    Tolerant of whitespace, newlines (a pasted list) and a trailing ';'. An
    empty spec is an empty list, not an error - the caller decides whether a
    route was required.
    """
    out = []
    for chunk in (spec or "").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = chunk.split(",")
        if len(bits) != 2:
            raise ValueError(f"--route: expected 'lat,lon', got {chunk!r}")
        try:
            lat, lon = float(bits[0]), float(bits[1])
        except ValueError:
            raise ValueError(f"--route: expected 'lat,lon', got {chunk!r}")
        out.append((lon, lat))
    return out


def read_route_file(path):
    """A .gpx track, or a plain text file of 'lat,lon' lines -> [(lon, lat)]."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    looks_xml = path.lower().endswith(".gpx") or (
        "<" in text and re.search(r"<(?:trkpt|rtept|wpt)\b", text, re.I))
    if looks_xml:
        # <trkpt lat="50.4" lon="5.9"> - trk / route / waypoint, either order
        pts = []
        for m in re.finditer(r"<(?:trkpt|rtept|wpt)\b[^>]*>", text, re.I):
            tag = m.group(0)
            la = re.search(r'lat\s*=\s*"([-0-9.]+)"', tag)
            lo = re.search(r'lon\s*=\s*"([-0-9.]+)"', tag)
            if la and lo:
                pts.append((float(lo.group(1)), float(la.group(1))))
        if not pts:
            raise ValueError(f"{path}: no <trkpt>/<rtept>/<wpt> points found")
        return pts
    return parse_route(text)


def fetch_route_osm(name, overpass_url=OVERPASS, timeout=60):
    """Resolve an OSM feature name to an ordered [(lon, lat), ...] path.

    Built for named circuits - `highway=raceway` ways and relations - but any
    named way or route relation resolves, so a famous climb or a canal towpath
    works too. Keeps the Etsy command to a name instead of a coordinate blob:
    the geometry is fetched here, at build time.
    """
    safe = name.replace('"', '').replace("\\", "")
    q = (f"[out:json][timeout:{int(timeout)}];"
         f'('
         f'  way["name"="{safe}"]["highway"="raceway"];'
         f'  relation["name"="{safe}"]["highway"="raceway"];'
         f'  way["name"="{safe}"][highway];'
         f'  relation["name"="{safe}"][route];'
         f');'
         f"out geom;")
    req = urllib.request.Request(
        overpass_url, data=("data=" + urllib.parse.quote(q)).encode(),
        headers={"User-Agent": f"map2model/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout + 30) as r:
        data = json.loads(r.read().decode())

    ways = []          # each a list of (lon, lat)
    for el in data.get("elements", []):
        if el.get("type") == "way" and el.get("geometry"):
            ways.append([(p["lon"], p["lat"]) for p in el["geometry"]])
        elif el.get("type") == "relation":
            for mem in el.get("members", []) or []:
                if mem.get("type") == "way" and mem.get("geometry"):
                    ways.append([(p["lon"], p["lat"]) for p in mem["geometry"]])
    if not ways:
        raise RuntimeError(
            f"OSM has no feature named {name!r}. Check the spelling against "
            f"openstreetmap.org, or draw the path on the map instead.")

    # Stitch the ways into one path: start from the longest, then repeatedly
    # append whichever loose way touches either open end. A closed loop (a
    # racing circuit) comes back as one ring and skips the stitching.
    def d2(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    ways.sort(key=len, reverse=True)
    path = list(ways.pop(0))
    changed = True
    while ways and changed:
        changed = False
        for i, w in enumerate(ways):
            for cand in (w, w[::-1]):
                if d2(path[-1], cand[0]) < 1e-8:
                    path.extend(cand[1:]); ways.pop(i); changed = True; break
                if d2(path[0], cand[-1]) < 1e-8:
                    path[:0] = cand[:-1]; ways.pop(i); changed = True; break
            if changed:
                break
    return path


def fetch_all(cfg):
    if cfg.source == "osm":
        with Stage("fetch OSM buildings (Overpass)", cfg.verbose):
            try:
                b = fetch_osm(cfg)
            except Exception as e:
                print(f"[warn] Overpass failed: {e}", file=sys.stderr)
                print("       try again shortly, or use the default "
                      "Overture source", file=sys.stderr)
                b = []
        con = _con(cfg)
        release = resolve_release(cfg)
        with Stage("fetch water", cfg.verbose):
            water = fetch(cfg, con, release, "base", "water",
                          ["id", "subtype", "class"])
        green = []
        for typ, cols, where in (
                ("land_cover", ["id", "subtype"],
                 " AND subtype IN ('forest','grass','wetland','shrub')"),
                ("land_use", ["id", "subtype", "class"],
                 " AND subtype IN ('park','recreation','cemetery','forest',"
                 "'golf','protected')")):
            with Stage(f"fetch base/{typ}", cfg.verbose):
                green += fetch(cfg, con, release, "base", typ, cols, where)
        cls = ", ".join(f"'{c}'" for c in cfg.road_classes)
        with Stage("fetch roads", cfg.verbose):
            roads = fetch(cfg, con, release, "transportation", "segment",
                          ["id", "subtype", "class"],
                          f" AND subtype = 'road' AND class IN ({cls})")
        con.close()
        return b, [], water, green, roads
    return fetch_all_overture(cfg)


def fetch_all_overture(cfg):
    """
    Six independent S3 scans. Run them at once.

    Nearly all of a build is spent waiting on Overture, not meshing: one London
    tile measured 4m10s on buildings and 4m17s on roads against ~15s of actual
    geometry. Those two do not depend on each other, and neither do the rest,
    so the wall time is the slowest single query rather than their sum.

    Each worker gets `con.cursor()`, not the shared connection. A DuckDB
    connection is not safe to use from several threads at once, but cursors off
    one connection are, and they share the loaded httpfs/spatial extensions and
    the S3 credentials -- so this costs no extra setup per query.
    """
    from concurrent.futures import ThreadPoolExecutor

    con = _con(cfg)
    release = resolve_release(cfg)
    log(cfg, f"[overture] release {release}")

    cls = ", ".join(f"'{c}'" for c in cfg.road_classes)
    jobs = [
        ("buildings", "buildings", "building",
         ["id", "height", "min_height", "num_floors", "roof_shape",
          "roof_height", "roof_direction", "roof_orientation",
          "has_parts", "subtype", "class", "names"], ""),
        ("water", "base", "water", ["id", "subtype", "class"], ""),
        ("land_cover", "base", "land_cover", ["id", "subtype"],
         " AND subtype IN ('forest','grass','wetland','shrub')"),
        ("land_use", "base", "land_use", ["id", "subtype", "class"],
         " AND subtype IN ('park','recreation','cemetery','forest','golf','protected')"),
        ("roads", "transportation", "segment", ["id", "subtype", "class"],
         f" AND subtype = 'road' AND class IN ({cls})"),
    ]
    if cfg.use_building_parts and cfg.lod >= 2:
        jobs.insert(1, ("parts", "buildings", "building_part",
                        ["id", "building_id", "height", "min_height",
                         "num_floors", "roof_shape", "roof_height",
                         "roof_direction", "roof_orientation"], ""))

    # Do not scan S3 for layers this build will throw away. --mode terrain and
    # --no-buildings etc. each drop ~2 min of Overture fetch.
    want = {"buildings": cfg.want_buildings, "parts": cfg.want_buildings,
            "roads": cfg.want_roads, "water": cfg.want_water,
            "land_cover": cfg.want_greenery, "land_use": cfg.want_greenery}
    jobs = [j for j in jobs if want.get(j[0], True)]

    out = {}

    def run(job):
        # One line per layer, start and finish, so the console can show each
        # scan's state instead of just the newest log line. Printed from INSIDE
        # the worker (pool.map yields in job order, not completion order), with
        # a fixed `[layer] <name>: ` prefix the browser greps for anywhere in
        # the line -- a Ticker \r can prepend progress text to it.
        name, theme, typ, cols, where = job
        t0 = time.time()
        print(f"[layer] {name}: fetching", file=sys.stderr, flush=True)
        cur = con.cursor()
        try:
            rows = fetch(cfg, cur, release, theme, typ, cols, where)
            dt = time.time() - t0
            n = len(rows)
            state = f"{n} rows in {dt:.0f}s" if n else f"empty in {dt:.0f}s"
            print(f"[layer] {name}: {state}", file=sys.stderr, flush=True)
            return name, rows
        except Exception as e:
            print(f"[layer] {name}: failed ({str(e).splitlines()[0][:80]})",
                  file=sys.stderr, flush=True)
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass

    workers = max(1, min(cfg.fetch_workers, len(jobs)))
    with Stage(f"fetch {len(jobs)} layers ({workers} at a time)", cfg.verbose):
        if workers == 1:
            for j in jobs:
                n, rows = run(j)
                out[n] = rows
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for n, rows in pool.map(run, jobs):
                    out[n] = rows

    b = out.get("buildings", [])
    parts = out.get("parts", [])
    water = out.get("water", [])
    green = out.get("land_cover", []) + out.get("land_use", [])
    roads = out.get("roads", [])
    stage("buildings fetched", len(b))
    stage("building_parts fetched", len(parts))
    stage("water fetched", len(water))
    stage("greenery fetched", len(green))
    stage("roads fetched", len(roads))

    con.close()
    return b, parts, water, green, roads


# ----------------------------------------------------------------------------
# 2. Projection
# ----------------------------------------------------------------------------


class Projector:
    """Local transverse-mercator centred on the tile: metres, minimal distortion."""

    def __init__(self, bbox):
        from pyproj import CRS, Transformer
        minx, miny, maxx, maxy = bbox
        self.lon0 = (minx + maxx) / 2
        self.lat0 = (miny + maxy) / 2
        crs = CRS.from_proj4(
            f"+proj=tmerc +lat_0={self.lat0} +lon_0={self.lon0} "
            f"+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
        self.fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        self.inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        (self.minx, self.maxx) = self.fwd.transform([minx, maxx], [miny, maxy])[0]
        (self.miny, self.maxy) = self.fwd.transform([minx, maxx], [miny, maxy])[1]

    def geom(self, g):
        from shapely.ops import transform
        return transform(lambda x, y, z=None: self.fwd.transform(x, y), g)

    def to_lonlat(self, x, y):
        return self.inv.transform(x, y)


# ----------------------------------------------------------------------------
# 3. Terrain (AWS terrarium tiles — public, no key)
# ----------------------------------------------------------------------------


def _lonlat_to_tile(lon, lat, z):
    """Slippy-tile coords. Vectorised: lon/lat may be scalars or arrays."""
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_r = np.radians(np.clip(lat, -85.05112878, 85.05112878))
    y = (1.0 - np.log(np.tan(lat_r) + 1.0 / np.cos(lat_r)) / np.pi) / 2.0 * n
    return x, y


def terrain_zoom_for(cfg):
    """
    Pick a terrarium zoom so the elevation data is about as fine as the mesh.

    A fixed zoom is wrong at both ends. At z14 a pixel is
    156543 * cos(lat) / 2^14 metres -- roughly 8 m at Tokyo -- so a 700 m tile
    got about 90 samples across and looked faceted no matter how fine
    terrain_grid was: the mesh was interpolating data that was not there.
    Bigger tiles do not need the detail and would download hundreds of tiles
    for it, so this scales with the tile instead of being cranked up globally.
    The floor is z8 (~600 m/px), not z12: a relief map of a whole national
    park is a legitimate tile, and z12 over 150 km is thousands of PNGs.
    """
    minx, miny, maxx, maxy = cfg.bbox
    lat = math.radians((miny + maxy) / 2.0)
    span_m = max((maxx - minx) * 111320.0 * math.cos(lat),
                 (maxy - miny) * 110540.0)
    want_m = max(span_m / max(cfg.terrain_grid, 2), 1.0)   # metres per sample
    z = math.log2(156543.03392 * math.cos(lat) / want_m)
    return int(max(8, min(cfg.terrain_zoom_max, round(z))))


class Terrain:
    """Bilinear elevation sampler in projected metres. Falls back to flat."""

    def __init__(self, cfg, proj):
        self.proj = proj
        self.flat = True
        self.grid = None
        if not cfg.terrain:
            return
        try:
            self._load(cfg, proj)
            self.flat = False
        except Exception as e:
            print(f"[warn] terrain unavailable ({e}); using a flat base", file=sys.stderr)

    def _load(self, cfg, proj):
        import requests
        from PIL import Image
        z = cfg.terrain_zoom or terrain_zoom_for(cfg)
        minx, miny, maxx, maxy = cfg.bbox
        x0, y1 = _lonlat_to_tile(minx, miny, z)
        x1, y0 = _lonlat_to_tile(maxx, maxy, z)
        tx0, tx1 = int(np.floor(x0)), int(np.floor(x1))
        ty0, ty1 = int(np.floor(y0)), int(np.floor(y1))
        W = (tx1 - tx0 + 1) * 256
        H = (ty1 - ty0 + 1) * 256
        canvas = np.zeros((H, W), dtype=np.float32)
        s = requests.Session()
        ntiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
        tk = Ticker(ntiles, f"downloading {ntiles} elevation tiles", cfg.verbose)
        done = 0
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                tk.tick(done)
                done += 1
                url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{tx}/{ty}.png"
                r = s.get(url, timeout=30)
                r.raise_for_status()
                a = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"), dtype=np.float32)
                elev = a[:, :, 0] * 256.0 + a[:, :, 1] + a[:, :, 2] / 256.0 - 32768.0
                canvas[(ty - ty0) * 256:(ty - ty0 + 1) * 256,
                       (tx - tx0) * 256:(tx - tx0 + 1) * 256] = elev
        self.dem = canvas
        self.z = z
        self.tx0, self.ty0 = tx0, ty0

    def elev_lonlat(self, lon, lat):
        if self.flat:
            return np.zeros(np.shape(lon))
        z = self.z
        fx, fy = _lonlat_to_tile(np.asarray(lon), np.asarray(lat), z)
        px = (fx - self.tx0) * 256.0
        py = (fy - self.ty0) * 256.0
        H, W = self.dem.shape
        px = np.clip(px, 0, W - 1.001)
        py = np.clip(py, 0, H - 1.001)
        x0, y0 = np.floor(px).astype(int), np.floor(py).astype(int)
        dx, dy = px - x0, py - y0
        d = self.dem
        return (d[y0, x0] * (1 - dx) * (1 - dy) + d[y0, x0 + 1] * dx * (1 - dy)
                + d[y0 + 1, x0] * (1 - dx) * dy + d[y0 + 1, x0 + 1] * dx * dy)

    def elev_xy(self, x, y):
        lon, lat = self.proj.to_lonlat(np.asarray(x, dtype=float),
                                       np.asarray(y, dtype=float))
        return self.elev_lonlat(lon, lat)


# ----------------------------------------------------------------------------
# 4. Geometry helpers
# ----------------------------------------------------------------------------


def clean_polys(geom, bbox_poly, min_area):
    """
    MultiPolygon-safe, hole-safe, invalid-safe. Clips to the tile instead of
    dropping anything that crosses the edge. Returns a list of valid Polygons.
    """
    from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
    from shapely.validation import make_valid
    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
    try:
        geom = geom.intersection(bbox_poly)
    except Exception:
        geom = make_valid(geom).intersection(bbox_poly)
    out = []
    stack = [geom]
    while stack:
        g = stack.pop()
        if g.is_empty:
            continue
        if isinstance(g, Polygon):
            if g.area >= min_area:
                # strip collinear/duplicate vertices: they ear-clip into
                # zero-area triangles, which punch holes in the shell later
                s = g.simplify(0.02, preserve_topology=True)
                out.append(s if (not s.is_empty and s.geom_type == "Polygon"
                                 and s.area >= min_area) else g)
        elif isinstance(g, (MultiPolygon, GeometryCollection)):
            stack.extend(list(g.geoms))
    return out


def ensure_min_feature(poly, min_w):
    """Inflate anything the nozzle can't print (spires, masts, thin wings).

    Uses ROUND joins and a binary search for the smallest inflation that works.
    Mitre joins were the old behaviour and they explode into long spikes on
    small polygons - that is what made the CN Tower's mast look folded and fat.
    """
    if min_w <= 0:
        return poly
    half = min_w / 2.0
    try:
        if not poly.buffer(-half).is_empty:
            return poly
    except Exception:
        return poly

    def ok(d):
        try:
            g = poly.buffer(d, join_style=1, quad_segs=8)
            return (not g.is_empty) and (not g.buffer(-half).is_empty)
        except Exception:
            return False

    hi = half
    for _ in range(8):
        if ok(hi):
            break
        hi *= 1.6
    else:
        return poly.buffer(hi, join_style=1, quad_segs=8)
    lo = 0.0
    for _ in range(14):                      # tighten to ~0.01% of hi
        mid = (lo + hi) / 2.0
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return poly.buffer(hi, join_style=1, quad_segs=8)


def tri2d(poly):
    """
    Ear-clip a polygon with holes.
    Returns (Nx2 verts, Mx3 faces, ring_lengths) — ring_lengths is what the wall
    builders index off, so the cap and the walls can never disagree.
    """
    import mapbox_earcut as earcut
    ext = np.asarray(poly.exterior.coords[:-1], dtype=np.float64)
    if len(ext) < 3:
        return None, None, None
    verts = [ext]
    lens = [len(ext)]
    for ring in poly.interiors:
        c = np.asarray(ring.coords[:-1], dtype=np.float64)
        if len(c) < 3:
            continue
        verts.append(c)
        lens.append(len(c))
    V = np.concatenate(verts, axis=0)
    rings = np.cumsum(lens).astype(np.uint32)
    idx = earcut.triangulate_float64(V, rings)
    if len(idx) == 0:
        return None, None, None
    return V, np.asarray(idx, dtype=np.int64).reshape(-1, 3), lens


def _walls(faces, lens, top_offset):
    """Side quads for every ring: bottom index i -> top index top_offset + i."""
    s = 0
    for L in lens:
        for i in range(L):
            a = s + i
            b = s + (i + 1) % L
            faces.append(np.array([[a, b, top_offset + b],
                                   [a, top_offset + b, top_offset + a]]))
        s += L


def _rings_complete(F, lens):
    """True if every ring edge appears exactly once in the cap triangulation.

    Ear-clipping can silently skip boundary edges on self-touching polygons
    (roads meeting at a corner, a hole tangent to the outline). If that happens
    the cap no longer matches the walls and the solid leaks.
    """
    if F is None or len(F) == 0:
        return False
    parts, s = [], 0
    for L in lens:
        i = np.arange(L)
        a, b = s + i, s + (i + 1) % L
        parts.append(np.stack([np.minimum(a, b), np.maximum(a, b)], 1))
        s += L
    ring = np.concatenate(parts)
    e = np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    e = np.stack([e.min(1), e.max(1)], 1)
    eu, ec = np.unique(e, axis=0, return_counts=True)
    k = lambda arr: arr[:, 0].astype(np.int64) * (1 << 32) + arr[:, 1]
    d = dict(zip(k(eu).tolist(), ec.tolist()))
    return all(d.get(x, 0) == 1 for x in k(ring).tolist())


def robust_pieces(poly, cell, depth=0):
    """Return polygons that are guaranteed to mesh into a closed shell."""
    from shapely.geometry import box
    if poly.is_empty or poly.area <= 0:
        return []
    V2, F, lens = tri2d(poly)
    if V2 is not None and _rings_complete(F, lens):
        return [poly]
    if depth >= 4:
        # last resort: fill the holes. At print scale a filled sliver is
        # invisible; a leaking shell is not.
        from shapely.geometry import Polygon as _P
        solid = _P(poly.exterior)
        V3, F3, l3 = tri2d(solid)
        if V3 is not None and _rings_complete(F3, l3):
            return [solid]
        return []
    x0, y0, x1, y1 = poly.bounds
    nx = max(2, int(math.ceil((x1 - x0) / max(cell, 1e-9))))
    ny = max(2, int(math.ceil((y1 - y0) / max(cell, 1e-9))))
    nx, ny = min(nx, 64), min(ny, 64)
    gx = np.linspace(x0, x1, nx + 1)
    gy = np.linspace(y0, y1, ny + 1)
    out = []
    for i in range(nx):
        for j in range(ny):
            try:
                inter = poly.intersection(box(gx[i], gy[j], gx[i + 1], gy[j + 1]))
            except Exception:
                continue
            if inter.is_empty:
                continue
            geoms = (list(inter.geoms)
                     if inter.geom_type in ("MultiPolygon", "GeometryCollection")
                     else [inter])
            for g in geoms:
                if g.geom_type == "Polygon" and g.area > 1e-9:
                    out.extend(robust_pieces(g, cell / 2.0, depth + 1))
    return out


def prism(poly, z0, z1, cap_fn=None):
    """
    Watertight solid between z0 and z1.
    cap_fn(exterior_ring_xy, z1) -> (verts, faces) replaces the flat top.
    Custom caps are only used on hole-free footprints (see roof_cap).
    """
    V2, F, lens = tri2d(poly)
    if V2 is None:
        return None
    n = len(V2)
    bottom = np.column_stack([V2, np.full(n, z0)])
    verts = [bottom]
    faces = [F[:, ::-1]]

    cap = cap_fn(np.asarray(poly.exterior.coords[:-1]), z1) if cap_fn else None
    if cap is None or cap[0] is None:
        top = np.column_stack([V2, np.full(n, z1)])
        verts.append(top)
        faces.append(F + n)
        _walls(faces, lens, n)
    else:
        tv, tf = cap
        # cap vertices 0..L-1 are the exterior ring at the eaves, by construction
        verts.append(tv)
        faces.append(tf + n)
        _walls(faces, lens[:1], n)

    V = np.concatenate(verts, axis=0)
    Fout = np.concatenate([f.reshape(-1, 3) for f in faces], axis=0)
    return V, Fout


def short_side(poly):
    """Width of the narrow axis - the thing that decides if this is a tower."""
    try:
        r = np.asarray(poly.minimum_rotated_rectangle.exterior.coords[:-1])
        if len(r) < 4:
            raise ValueError
        e = [float(np.linalg.norm(r[(i + 1) % 4] - r[i])) for i in range(4)]
        return max(min(e), 1e-6)
    except Exception:
        b = poly.bounds
        return max(min(b[2] - b[0], b[3] - b[1]), 1e-6)


def part_spec(rule, top_m, base_m, width_m):
    """First matching entry in rule["parts"] for this part, or None."""
    if not rule:
        return None
    for spec in rule.get("parts", []) or []:
        if "top_max" in spec and top_m > float(spec["top_max"]):
            continue
        if "top_min" in spec and top_m < float(spec["top_min"]):
            continue
        if "base_max" in spec and base_m > float(spec["base_max"]):
            continue
        if "base_min" in spec and base_m < float(spec["base_min"]):
            continue
        if "width_max" in spec and width_m > float(spec["width_max"]):
            continue
        if "width_min" in spec and width_m < float(spec["width_min"]):
            continue
        return spec
    return None


def part_profile(rule, top_m, base_m, width_m):
    """
    Pick the profile for ONE part of a landmark.

    A single multiplier shared by every part cannot express "the legs taper
    down to meet the shaft" - shrinking the legs would shrink the shaft too.
    So a rule may carry a `parts` list whose entries match on the part's own
    top height, base height, or footprint width (all in metres). First match
    wins; `profile` at the rule level is the fallback.
    """
    if not rule:
        return None
    spec = part_spec(rule, top_m, base_m, width_m)
    if spec is not None:
        return spec.get("profile")
    return rule.get("profile")


def profile_scale(prof, h):
    """Piecewise-linear radius multiplier at height `h` (metres above ground).
    Clamps outside the control range, so a profile ending at 1.0 leaves
    everything above it untouched."""
    if not prof:
        return 1.0
    if h <= prof[0][0]:
        return float(prof[0][1])
    if h >= prof[-1][0]:
        return float(prof[-1][1])
    for i in range(len(prof) - 1):
        ha, sa = prof[i]
        hb, sb = prof[i + 1]
        if ha <= h <= hb:
            t = (h - ha) / max(hb - ha, 1e-9)
            return float(sa + (sb - sa) * t)
    return float(prof[-1][1])


def world_profile(prof_m, z0, z1, ground_mm, mm_per_m, dens=4,
                  min_scale=0.0):
    """
    Convert an absolute (metres, scale) profile into the (t, scale) pairs
    tapered_prism wants, for a part spanning z0..z1 mm.

    This is the fix for the "blade" silhouette. A per-part taper is a fraction
    of THAT part's height, so it always narrows across the whole part - which
    is why a shaft running 0..335 m came out narrowest right under the pod. A
    real tower flares only near the ground, so the profile must be anchored to
    absolute height and shared by every part of the building.
    """
    if not prof_m or z1 <= z0:
        return None
    edges = {z0, z1}
    for h, _ in prof_m:
        zw = ground_mm + float(h) * mm_per_m
        if z0 < zw < z1:
            edges.add(zw)
    edges = sorted(edges)
    zs = []
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        for k in range(dens):
            zs.append(a + (b - a) * k / dens)
    zs.append(edges[-1])
    # Clamp rather than pre-inflate. Pre-inflating the footprint so the TIP
    # clears the nozzle makes the whole part fat (a 4 m mast ballooning to
    # 4 mm). Clamping keeps the part true and just stops the taper once it
    # reaches the thinnest printable width.
    out = [((z - z0) / (z1 - z0),
            max(profile_scale(prof_m, (z - ground_mm) / mm_per_m), min_scale))
           for z in zs]
    if all(abs(s - 1.0) < 1e-6 for _, s in out):
        return None                    # flat multiplier: keep the plain prism
    return out


def profile_extent(prof_m, z0, z1, ground_mm, mm_per_m):
    """Largest multiplier this part will see, for sizing checks."""
    p = world_profile(prof_m, z0, z1, ground_mm, mm_per_m)
    return max((s for _, s in p), default=1.0) if p else 1.0


def taper_profile(cfg, aspect, is_top, rule=None, allowed=True,
                  min_scale=0.0, force_spire=False):
    """
    (t, scale) pairs from base to top, or None for a plain prism.

    `allowed` is the scope gate: a blanket batter keyed on aspect ratio cannot
    distinguish a landmark from an ordinary tower, so by default nothing is
    tapered unless a landmark rule opts it in.
    """
    g = (lambda k, d: float(rule[k]) if rule and k in rule else d)
    batter_max = g("batter", cfg.tower_batter)
    aspect_min = g("batter_aspect", cfg.batter_aspect)
    power = g("taper_power", cfg.taper_power)
    tip_ratio = g("spire_tip", cfg.spire_tip_ratio)
    tip_frac = g("spire_tip_frac", cfg.spire_tip_frac)
    spire_min = g("spire_aspect", cfg.spire_aspect)
    want_spire = cfg.spire_taper if rule is None else bool(rule.get("spire", True))

    if not (allowed or rule):
        return None

    batter = batter_max if aspect >= aspect_min else 0.0
    needle = bool(want_spire and is_top and (force_spire or aspect >= spire_min))
    if force_spire:
        batter = max(batter, batter_max)
    if batter <= 0.0 and not needle:
        return None

    K = max(int(cfg.taper_steps), 2)
    body_top = 1.0 - tip_frac if needle else 1.0
    body_top = min(max(body_top, 0.05), 1.0)
    s_top = 1.0 - batter

    prof = []
    for k in range(K + 1):
        u = k / K
        prof.append((u * body_top, 1.0 + (s_top - 1.0) * (u ** power)))
    if needle:
        s0 = prof[-1][1]
        # never taper below what the nozzle can print, or the slicer drops
        # the tip and a needle comes out blunt
        s1 = max(tip_ratio * s0, min_scale, 1e-3)
        for k in range(1, 4):
            u = k / 3.0
            prof.append((body_top + (1.0 - body_top) * u, s0 + (s1 - s0) * u))
    return prof


def tapered_prism(poly, z0, z1, profile, center=None):
    """
    Solid whose cross-section is `poly` uniformly scaled about `center`
    (default: an interior point of `poly`), following `profile`. Passing a
    shared centre keeps every part of one landmark concentric. Uniform scaling is a similarity transform, so a
    simple polygon stays simple - concave footprints can't self-fold here.
    Returns None for polygons with holes; the caller falls back to a prism.
    """
    V2, F, lens = tri2d(poly)
    if V2 is None or len(lens) != 1:
        return None
    if center is not None:
        c = np.asarray(center, dtype=float)
    else:
        # centroid is the true axis for a symmetric tower, but for an L- or
        # U-shaped footprint it lies OUTSIDE the polygon, and scaling about an
        # exterior point translates the building sideways. Fall back to a
        # guaranteed-interior point in that case.
        ctr = poly.centroid
        c = np.asarray((ctr if poly.contains(ctr)
                        else poly.representative_point()).coords[0], dtype=float)
    rel = V2 - c
    n = len(V2)
    L = lens[0]

    verts, faces = [], [F[:, ::-1]]
    for t, s in profile:
        verts.append(np.column_stack([c + rel * s, np.full(n, z0 + (z1 - z0) * t)]))
    for k in range(len(profile) - 1):
        o0, o1 = k * n, (k + 1) * n
        for i in range(L):
            j = (i + 1) % L
            faces.append(np.array([[o0 + i, o0 + j, o1 + j],
                                   [o0 + i, o1 + j, o1 + i]]))
    faces.append(F + (len(profile) - 1) * n)
    return (np.concatenate(verts, axis=0),
            np.concatenate([f.reshape(-1, 3) for f in faces], axis=0))


# ---- roof shapes -----------------------------------------------------------


def _fan(ring, z_base, apex_z, centroid):
    """Cone / pyramid: exterior ring -> single apex."""
    n = len(ring)
    verts = np.column_stack([ring, np.full(n, z_base)])
    apex = np.array([[centroid[0], centroid[1], apex_z]])
    V = np.vstack([verts, apex])
    F = np.array([[i, (i + 1) % n, n] for i in range(n)])
    return V, F


def _revolve(ring, z_base, h, centroid, profile, steps=7):
    """
    Stacked shrinking rings following a radius profile in t=0..1.
    Used for dome / spherical / round / onion.
    """
    n = len(ring)
    c = np.asarray(centroid, dtype=float)
    rel = np.asarray(ring, dtype=float) - c
    layers = [np.column_stack([ring, np.full(n, z_base)])]
    for k in range(1, steps + 1):
        t = k / steps
        r, dz = profile(t)
        pts = c + rel * r
        layers.append(np.column_stack([pts, np.full(n, z_base + h * dz)]))
    V = np.vstack(layers)
    F = []
    for k in range(steps):
        o0, o1 = k * n, (k + 1) * n
        for i in range(n):
            j = (i + 1) % n
            F.append([o0 + i, o0 + j, o1 + j])
            F.append([o0 + i, o1 + j, o1 + i])
    apex_i = len(V)
    V = np.vstack([V, [[c[0], c[1], z_base + h]]])
    o = steps * n
    for i in range(n):
        F.append([o + i, o + (i + 1) % n, apex_i])
    return V, np.array(F)


def _ridge(ring, z_base, h, poly, hipped=False, skillion=False,
           direction=None, orientation=None):
    """Gabled / hipped / skillion, oriented on the minimum-rotated-rectangle axis."""
    from shapely.geometry import Polygon
    mrr = np.asarray(poly.minimum_rotated_rectangle.exterior.coords[:-1])
    if len(mrr) < 4:
        return None
    edges = sorted((float(np.linalg.norm(mrr[(i + 1) % 4] - mrr[i])), i)
                   for i in range(4))
    # roof:orientation describes the RIDGE. 'along' (the default) puts the
    # ridge parallel to the longer edge; 'across' puts it on the shorter one.
    li = edges[0][1] if str(orientation or "").lower() == "across" else edges[-1][1]
    axis = mrr[(li + 1) % 4] - mrr[li]
    nrm = np.linalg.norm(axis)
    if nrm < 1e-9:
        return None
    axis = axis / nrm
    # CANONICALISE the sign. minimum_rotated_rectangle's vertex order varies
    # between GEOS versions, so this vector can come out either way round for
    # the same building - which flips `normal`, which flips which side of a
    # skillion is high. That is why the same file rendered differently on
    # macOS and Windows. Force a half-plane so the result is version-stable.
    if axis[0] < -1e-12 or (abs(axis[0]) <= 1e-12 and axis[1] < 0):
        axis = -axis

    slope_vec = None
    if direction is not None:
        try:
            a = math.radians(float(direction))
            # roof:direction is the SLOPE direction (which way the roof
            # surfaces face), NOT the ridge. For a gabled/hipped roof the
            # ridge is perpendicular to it. Treating it as the ridge rotates
            # every tagged roof by 90 degrees - the same bug OSMBuildings had.
            slope = np.array([math.sin(a), math.cos(a)])
            slope_vec = slope
            axis = np.array([slope[1], -slope[0]])   # ridge perpendicular
        except Exception:
            pass
    normal = np.array([-axis[1], axis[0]])

    ring = np.asarray(ring, dtype=float)
    n = len(ring)
    c = ring.mean(axis=0)
    t = (ring - c) @ axis
    s = (ring - c) @ normal

    if skillion:
        rng = max(s.max() - s.min(), 1e-9)
        # roof:direction on a skillion is the direction the surface FACES,
        # i.e. the way it slopes DOWN. So the low edge is the one furthest
        # along `slope_vec` and the high edge is opposite it. Getting this
        # backwards tips every mono-pitch roof through 180 degrees.
        if slope_vec is not None:
            d = (ring - c) @ slope_vec
            z = z_base + h * (d.max() - d) / max(d.max() - d.min(), 1e-9)
        else:
            z = z_base + h * (s - s.min()) / rng
        V = np.column_stack([ring, z])
        V2, F, _ = tri2d(Polygon(ring))
        if V2 is None:
            return None
        return V, F

    tmin, tmax, tmid = t.min(), t.max(), (t.min() + t.max()) / 2
    inset = 0.25 * (tmax - tmin) if hipped else 0.0
    r0 = c + axis * (tmin + inset)
    r1 = c + axis * (tmax - inset)
    V = np.vstack([
        np.column_stack([ring, np.full(n, z_base)]),
        [[r0[0], r0[1], z_base + h], [r1[0], r1[1], z_base + h]],
    ])
    A, B = n, n + 1                      # ridge vertex indices
    owner = [A if t[i] < tmid else B for i in range(n)]
    F = []
    for i in range(n):
        j = (i + 1) % n
        ki, kj = owner[i], owner[j]
        if ki == kj:
            F.append([i, j, ki])
        else:
            F.append([i, j, kj])         # quad i-j-kj-ki
            F.append([i, kj, ki])
    return V, np.array(F, dtype=np.int64)


RIDGE_MIN_ASPECT = 1.25

RIDGE_ROOFS = ('gabled', 'hipped', 'half_hipped', 'skillion', 'saltbox', 'sawtooth', 'mansard', 'gambrel')


def roof_cap(poly, shape, roof_h, direction, min_feature,
             mode="symmetric", orientation=None):
    """Return a cap_fn for prism(), or None for a flat roof."""
    if not shape or shape == "flat" or roof_h <= 1e-6:
        return None
    if mode == "none":
        return None
    if mode != "all" and shape in RIDGE_ROOFS:
        if mode != "safe":
            return None
        # 'safe': allow a ridge roof only when the axis is NOT a guess -
        # either the mapper tagged it, or the footprint is clearly oblong.
        if shape in ("skillion", "saltbox", "sawtooth") and direction is None:
            # a mono-pitch roof IS its direction; without the tag we would be
            # guessing 1-in-4, and a tall one becomes a blade pointing anywhere
            return None
        tagged = (direction is not None) or bool(orientation)
        if not tagged:
            try:
                r = np.asarray(poly.minimum_rotated_rectangle
                               .exterior.coords[:-1])
                e = sorted(float(np.linalg.norm(r[(i + 1) % 4] - r[i]))
                           for i in range(4))
                if e[0] < 1e-9 or (e[-1] / e[0]) < RIDGE_MIN_ASPECT:
                    return None          # near-square: axis is a coin flip
            except Exception:
                return None
    if len(poly.interiors) > 0:
        return None   # custom caps only on simple footprints; holes stay flat
    # Fan/revolve caps scale the ring toward a single interior point. On a
    # concave footprint (L-shapes, courtyards, inflated spires) that folds the
    # surface through itself. Only cap shapes that are close to convex.
    try:
        hull = poly.convex_hull.area
        if hull <= 0 or poly.area / hull < 0.90:
            return None
    except Exception:
        return None
    c = np.asarray(poly.representative_point().coords[0])

    def cap(ring, z1):
        ring = np.asarray(ring, dtype=float)
        z0 = z1 - roof_h
        if shape in ("pyramidal",):
            return _fan(ring, z0, z1, c)
        if shape in ("onion",):
            def prof(t):
                # bulge then pinch
                return (max(0.02, (1 - t ** 2.2) * (1 + 0.35 * math.sin(math.pi * t))),
                        t)
            return _revolve(ring, z0, roof_h, c, prof)
        if shape in ("dome", "spherical", "round"):
            def prof(t):
                return (max(0.02, math.cos(t * math.pi / 2)), math.sin(t * math.pi / 2))
            return _revolve(ring, z0, roof_h, c, prof)
        if shape in ("mansard", "gambrel"):
            def prof(t):
                return (1 - 0.55 * t if t < 0.6 else 0.67 - 0.62 * (t - 0.6) / 0.4, t)
            return _revolve(ring, z0, roof_h, c, prof, steps=3)
        if shape in ("skillion",):
            return _ridge(ring, z0, roof_h, poly, skillion=True, direction=direction,
                          orientation=orientation)
        if shape in ("hipped", "half_hipped"):
            return _ridge(ring, z0, roof_h, poly, hipped=True, direction=direction,
                          orientation=orientation)
        if shape in ("gabled", "saltbox", "sawtooth"):
            return _ridge(ring, z0, roof_h, poly, hipped=False, direction=direction,
                          orientation=orientation)
        return None

    return cap


# ----------------------------------------------------------------------------
# 5. Height resolution
# ----------------------------------------------------------------------------


def resolve_height(rec, cfg):
    """
    Never returns None. This is the single most important fix for missing
    buildings: a null `height` must fall back, not delete the row.
    """
    h = rec.get("height")
    if h is not None and h > 0:
        return float(h), "height"
    nf = rec.get("num_floors")
    if nf is not None and nf > 0:
        return float(nf) * cfg.default_floor_height_m, "num_floors"
    mh = rec.get("min_height")
    if mh is not None and mh > 0:
        return float(mh) + cfg.default_building_height_m, "min_height"
    sub = (rec.get("subtype") or "")
    by_type = {"outbuilding": 3.0, "service": 3.5, "residential": 7.5,
               "agricultural": 6.0, "commercial": 12.0, "industrial": 10.0,
               "education": 12.0, "medical": 15.0, "religious": 12.0}
    return float(by_type.get(sub, cfg.default_building_height_m)), "default"


# ----------------------------------------------------------------------------
# 6. Layer builders
# ----------------------------------------------------------------------------


class MeshAccum:
    def __init__(self):
        self.V = []
        self.F = []
        self.n = 0

    def add(self, vf):
        if vf is None:
            return False
        V, F = vf
        if V is None or len(F) == 0:
            return False
        self.V.append(np.asarray(V, dtype=np.float64))
        self.F.append(np.asarray(F, dtype=np.int64) + self.n)
        self.n += len(V)
        return True

    def result(self):
        if not self.V:
            return None
        return np.concatenate(self.V), np.concatenate(self.F)


def build_terrain(cfg, proj, terr, S, water_polys=None):
    """Heightmap grid + skirt + bottom plate = one watertight plinth.

    Grid points inside water are pushed down to the basin floor, so the water
    plate drops in flush instead of standing proud of the land.
    """
    x0, x1 = proj.minx, proj.maxx
    y0, y1 = proj.miny, proj.maxy
    ar = (y1 - y0) / (x1 - x0)
    nx = cfg.terrain_grid
    ny = max(4, int(nx * ar))
    xs = np.linspace(x0, x1, nx)
    ys = np.linspace(y0, y1, ny)
    X, Y = np.meshgrid(xs, ys)
    Z = terr.elev_xy(X, Y).astype(float) - terr_min[0]
    Z = np.maximum(Z, 0.0)
    if cfg.terrain_relief_mm > 0 and Z.max() > 1e-6:
        Z = Z / Z.max() * (cfg.terrain_relief_mm / S.z)
    zz = Z * S.z + cfg.base_mm

    if water_polys:
        from shapely.ops import unary_union
        import shapely
        wu = unary_union(water_polys)
        try:
            inside = shapely.contains_xy(wu, X.ravel(), Y.ravel()).reshape(X.shape)
        except Exception:                     # shapely < 2.0
            from shapely.geometry import Point
            inside = np.array([wu.contains(Point(a, b))
                               for a, b in zip(X.ravel(), Y.ravel())]).reshape(X.shape)
        bed = cfg.base_mm - cfg.water_depth_mm
        zz = np.where(inside, np.minimum(zz, bed), zz)

    px = (X - (x0 + x1) / 2) * S.xy
    py = (Y - (y0 + y1) / 2) * S.xy

    V = np.column_stack([px.ravel(), py.ravel(), zz.ravel()])
    F = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a = j * nx + i
            b = a + 1
            c = a + nx
            d = c + 1
            F.append([a, b, d])
            F.append([a, d, c])
    F = np.array(F)

    # bottom = mirror of the top grid at z=0, joined by a perimeter skirt.
    # (A fan over a rectangular ring is entirely collinear -> all-degenerate.)
    nv = len(V)
    Vb = np.column_stack([V[:, 0], V[:, 1], np.zeros(nv)])
    Fb = F[:, ::-1] + nv
    per = ([i for i in range(nx)]
           + [j * nx + nx - 1 for j in range(1, ny)]
           + [(ny - 1) * nx + i for i in range(nx - 2, -1, -1)]
           + [j * nx for j in range(ny - 2, 0, -1)])
    P = len(per)
    walls = []
    for k in range(P):
        a, b = per[k], per[(k + 1) % P]
        walls.append([a, nv + b, b])
        walls.append([a, nv + a, nv + b])
    V = np.vstack([V, Vb])
    F = np.vstack([F, Fb, np.array(walls)])
    return V, F


def build_drape(cfg, proj, terr, S, polys, thickness_mm, embed_mm,
                cell_m=None, label="draping", flat_bottom=None, top_cap_mm=None,
                shore=None):
    """
    Slab that follows the terrain: bottom sunk `embed` into it, top `thickness`
    above. Large polygons are grid-split first so they actually follow relief.
    `flat_bottom` pins the underside to a constant z instead, which is how the
    cut-out terrain plinth is built. `top_cap_mm` clamps the top DOWN to a
    constant z where the relief rises past it - that is how an elevation slice
    keeps a smooth terrain-following surface within its own band and a flat lid
    above (hidden under the next slice up).

    `shore` = (water_union, beach_m, water_z_mm): where the slab's edge meets
    that water it ramps the top DOWN to `water_z_mm` over `beach_m` metres
    instead of walling straight off - a printed beach. Because the ramp is
    `min(true relief, blend)`, a genuine cliff keeps its height but its face is
    pulled back to a `beach_m`-wide slope, so the edge is never a paper sheet.
    """
    from shapely.geometry import box
    import shapely
    acc = MeshAccum()
    cell = cell_m or max(30.0, (proj.maxx - proj.minx) / 40.0)
    tk = Ticker(len(polys), label, cfg.verbose)
    for _pi, poly in enumerate(polys):
        tk.tick(_pi)
        pieces = [poly]
        if not terr.flat and poly.area > cell * cell * 2:
            pieces = []
            bx0, by0, bx1, by1 = poly.bounds
            gx = np.arange(bx0, bx1 + cell, cell)
            gy = np.arange(by0, by1 + cell, cell)
            for i in range(len(gx) - 1):
                for j in range(len(gy) - 1):
                    cellpoly = box(gx[i], gy[j], gx[i + 1], gy[j + 1])
                    inter = poly.intersection(cellpoly)
                    if inter.is_empty:
                        continue
                    if inter.geom_type == "Polygon":
                        pieces.append(inter)
                    elif inter.geom_type == "MultiPolygon":
                        pieces.extend(list(inter.geoms))
        safe = []
        for p in pieces:
            safe.extend(robust_pieces(p, cell))
        for p in safe:
            V2, F, lens = tri2d(p)
            if V2 is None:
                continue
            gz = terr.elev_xy(V2[:, 0], V2[:, 1])
            gz = (gz - terr_min[0]) * terr_relief_scale[0] * S.z + cfg.base_mm
            px = (V2[:, 0] - (proj.minx + proj.maxx) / 2) * S.xy
            py = (V2[:, 1] - (proj.miny + proj.maxy) / 2) * S.xy
            n = len(V2)
            bot = np.column_stack([px, py,
                                   (np.full(n, flat_bottom)
                                    if flat_bottom is not None
                                    else gz - embed_mm)])
            top_z = gz + thickness_mm
            if top_cap_mm is not None:
                top_z = np.minimum(top_z, top_cap_mm)
            if shore is not None:
                wu_s, beach_m, wz = shore
                if beach_m > 1e-6 and p.distance(wu_s) < beach_m:
                    try:
                        dd = shapely.distance(
                            wu_s, shapely.points(V2[:, 0], V2[:, 1]))
                    except Exception:            # shapely < 2.0
                        from shapely.geometry import Point
                        dd = np.array([wu_s.distance(Point(a, b))
                                       for a, b in zip(V2[:, 0], V2[:, 1])])
                    f = np.clip(dd / beach_m, 0.0, 1.0)
                    f = f * f * (3.0 - 2.0 * f)          # smoothstep
                    top_z = np.minimum(top_z, wz + f * (top_z - wz))
            top = np.column_stack([px, py, top_z])
            V = np.vstack([bot, top])
            faces = [F[:, ::-1], F + n]
            _walls(faces, lens, n)
            acc.add((V, np.concatenate([f.reshape(-1, 3) for f in faces])))
    return acc.result()


def build_flat(cfg, proj, S, polys, z0, z1):
    """A layer with flat top and flat bottom - a separately printable insert."""
    acc = MeshAccum()
    for poly in polys:
        pmm = scale_poly(poly, proj, S)
        span = max(pmm.bounds[2] - pmm.bounds[0], pmm.bounds[3] - pmm.bounds[1])
        for ch in robust_pieces(pmm, max(span / 4.0, 0.5)):
            acc.add(prism(ch, z0, z1, None))
    return acc.result()


def prism_any(poly, z0, z1):
    """prism() over a Polygon or every part of a MultiPolygon / collection."""
    from shapely.geometry import Polygon
    if poly is None or poly.is_empty:
        return None
    geoms = [poly] if isinstance(poly, Polygon) else list(getattr(poly, "geoms", []))
    acc = MeshAccum()
    for g in geoms:
        if isinstance(g, Polygon) and not g.is_empty and g.area > 1e-9:
            acc.add(prism(g, z0, z1, None))
    return acc.result()


def _rle_union(mask, xe, ye):
    """Union of the grid cells where `mask` is true, one run-length box per
    horizontal run so a 160-wide row is a handful of boxes, not 160. `xe`/`ye`
    are the (nx+1)/(ny+1) cell edges."""
    from shapely.geometry import box as _box, Polygon
    from shapely.ops import unary_union
    ny, nx = mask.shape
    boxes = []
    for j in range(ny):
        row = mask[j]
        i = 0
        while i < nx:
            if not row[i]:
                i += 1
                continue
            k = i + 1
            while k < nx and row[k]:
                k += 1
            boxes.append(_box(xe[i], ye[j], xe[k], ye[j + 1]))
            i = k
    return unary_union(boxes) if boxes else Polygon()


def _flatten_polys(geom):
    """A Polygon / MultiPolygon / collection -> list of non-trivial Polygons."""
    from shapely.geometry import Polygon
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 1e-9 else []
    return [g for g in getattr(geom, "geoms", [])
            if g.geom_type == "Polygon" and not g.is_empty and g.area > 1e-9]


def build_terrain_bands(cfg, proj, terr, S, bbox_poly, water_polys):
    """Relief cut into `cfg.terrain_bands` colour SLICES - a relief map, not a
    staircase. Each slice is its own object so it gets its own filament
    (TERRAIN_RAMP / a buyer pick).

    Slice k covers every point at least `edges[k]` high and is a solid draped
    slab from the bed up to `min(terrain, edges[k+1])`: within its own band the
    top follows the real relief, above it the top is a flat lid that the next
    slice up hides. So the finished model's visible surface is the true smooth
    terrain with the colour changing at elevation contours - not stepped
    plateaus. Slices nest and lean on each other like a layered contour model;
    water is cut clean through every one, exactly like the single-colour plinth.

    Two nudges stop the nested slices from Z-fighting in a viewer (and remove
    the ambiguity for the slicer): every slice above the base is inset in plan
    by ~0.3 mm of print so its vertical walls sit *inside* the slice below
    rather than exactly on its surface, and each buried flat lid is dropped a
    hair below the contour it sits at so it never lands coplanar with the
    surface of the slice above.
    """
    from shapely.ops import unary_union
    n = max(1, min(int(cfg.terrain_bands), len(TERRAIN_RAMP)))
    x0, x1, y0, y1 = proj.minx, proj.maxx, proj.miny, proj.maxy
    # The contour grid. Finer than the old fixed 160 so the colour boundaries
    # do not look blocky on a big tile (a whole national park is a valid tile).
    G = int(np.clip(round(cfg.terrain_grid * 0.7), 160, 320))
    ny = max(8, int(G * (y1 - y0) / (x1 - x0)))
    xs = np.linspace(x0, x1, G)
    ys = np.linspace(y0, y1, ny)
    X, Y = np.meshgrid(xs, ys)
    E = np.maximum(terr.elev_xy(X, Y).astype(float) - terr_min[0], 0.0)
    emax = float(E.max())

    xe = np.empty(G + 1)
    xe[1:-1] = (xs[:-1] + xs[1:]) / 2
    xe[0], xe[-1] = xs[0] - (xs[1] - xs[0]) / 2, xs[-1] + (xs[-1] - xs[-2]) / 2
    ye = np.empty(ny + 1)
    ye[1:-1] = (ys[:-1] + ys[1:]) / 2
    ye[0], ye[-1] = ys[0] - (ys[1] - ys[0]) / 2, ys[-1] + (ys[-1] - ys[-2]) / 2

    wu = unary_union(water_polys) if water_polys else None
    simp = (xs[1] - xs[0]) / 2.0
    zpm = terr_relief_scale[0] * S.z             # metres of relief -> mm
    inset_m = 0.3 / S.xy                         # ~0.3 mm of print, in metres

    # Beach: where a slice meets water its edge ramps down to the datum instead
    # of dropping as a wall (see build_drape). Simplify the water outline for
    # the per-vertex distance test - sub-(beach/8) wobble is invisible.
    shore = None
    if wu is not None and cfg.shore_ramp_mm > 0:
        beach_m = cfg.shore_ramp_mm / S.xy
        shore = (wu.simplify(max(1.0, beach_m / 8.0), preserve_topology=False),
                 beach_m, cfg.base_mm)

    def slab(region, cap_mm, label, inset=False):
        """Draped solid: flat on the bed, top = min(relief, cap)."""
        region = region.intersection(bbox_poly)
        if wu is not None:
            region = region.difference(wu)
        if inset:
            # mitre join: a plain round buffer puts 8 arc points on every step
            # of the blocky RLE outline and quadruples the mesh. No re-simplify
            # afterwards - that would move the contour tens of metres and eat
            # the lid drop on a slope. `region` is already simplified.
            region = region.buffer(-inset_m, join_style=2, mitre_limit=2.0)
        polys = _flatten_polys(region)
        if not polys:
            return None
        return build_drape(cfg, proj, terr, S, polys, 0.0, 0.0,
                           label=label, flat_bottom=0.0, top_cap_mm=cap_mm,
                           shore=shore)

    # a flat tile, or one slice asked for: a single smooth relief surface
    if emax < 1.0 or n == 1:
        vf = slab(bbox_poly, None, "relief surface")
        return [("terrain", vf)] if vf is not None else []

    edges = np.linspace(0.0, emax, n + 1)
    out = []
    for k in range(n):
        region = _rle_union(E >= edges[k] - 1e-9, xe, ye).simplify(simp)
        if region.is_empty or region.area < 1.0:
            continue
        if k == n - 1:
            cap = None
        else:
            # Drop the buried lid well clear of the slice above. The RLE region
            # boundary sits up to half a contour cell downhill of the true
            # contour, so on a slope the slice above dips ~half_cell*slope below
            # its own threshold - the drop has to cover that or the two still
            # meet. Bounded so a thin band stays a distinct slice.
            band_mm = (edges[k + 1] - edges[k]) * zpm
            half_cell_m = (xs[1] - xs[0]) / 2.0
            drop = min(band_mm * 0.45, 0.3 + half_cell_m * zpm * 0.7)
            cap = cfg.base_mm + edges[k + 1] * zpm - drop
        vf = slab(region, cap, f"slicing relief {k + 1}/{n}", inset=(k > 0))
        if vf is None:
            continue
        name = "terrain" if k == 0 else f"terrain_{k + 1}"
        out.append((name, vf))
        if cfg.verbose:
            hi = "peak" if cap is None else f"{cap:.1f} mm lid"
            print(f"  slice {k + 1}/{n}: >= {edges[k] * zpm:.1f} mm, {hi}, "
                  f"{region.area / bbox_poly.area * 100:.0f}% of the tile",
                  file=sys.stderr)
    return out


def _densify(line, step):
    """Add points along `line` so no gap is longer than `step`. earcut adds no
    interior vertices, so a ribbon only follows the ground as finely as its
    OWN edge is sampled - a 200 m straight buffered from two points drapes as
    one flat plank over Eau Rouge. Sampling the centreline every ~step makes
    the ribbon boundary that fine too."""
    from shapely.geometry import LineString
    xy = list(line.coords)
    out = [xy[0]]
    for a, b in zip(xy[:-1], xy[1:]):
        d = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        k = max(1, int(d // step))
        for i in range(1, k):
            t = i / k
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return LineString(out)


def _chop_line(line, step):
    """Split a LineString into sub-lines about `step` long. build_drape
    grid-splits a polygon over its *bounding box*, so one ribbon spanning the
    tile pays an nx*ny cell scan to find mostly-empty cells. Buffering short
    pieces keeps every bbox small and the drape cost tracks route length, not
    tile area. Overlapping draped slabs at the joins are fine - that is how
    roads already behave at junctions, and finalize() checks each is
    watertight on its own."""
    from shapely.geometry import LineString
    xy = list(line.coords)
    out, cur, run = [], [xy[0]], 0.0
    for a, b in zip(xy[:-1], xy[1:]):
        seg = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        run += seg
        cur.append(b)
        if run >= step:
            out.append(LineString(cur))
            cur, run = [b], 0.0
    if len(cur) >= 2:
        out.append(LineString(cur))
    return out or [line]


def build_route(cfg, proj, terr, S, bbox_poly):
    """A raised ribbon that follows a GPS track, draped on the terrain.

    The path (cfg.route / cfg.route_name / cfg.route_file, resolved in run())
    is projected, clipped to the tile, buffered to cfg.route_width_m and
    draped like a wide road - so it climbs Eau Rouge and drops down the
    Kemmel straight with the ground, which is the whole point of a circuit.
    """
    from shapely.geometry import LineString
    pts = getattr(cfg, "_route_pts", None) or parse_route(cfg.route)
    if len(pts) < 2:
        raise RuntimeError("--mode route needs a path of at least two points "
                           "(--route, --route-name or --route-file)")
    line = proj.geom(LineString(pts))
    try:
        line = line.intersection(bbox_poly)
    except Exception:
        pass
    segs = ([line] if line.geom_type == "LineString"
            else [g for g in getattr(line, "geoms", [])
                  if g.geom_type == "LineString"])
    segs = [s for s in segs if s.length > 1e-6]
    if not segs:
        raise RuntimeError("the route does not cross the tile. Move or widen "
                           "the tile so it covers the path.")
    half = max(cfg.route_width_m, 0.5) / 2.0
    span = proj.maxx - proj.minx
    fine = max(12.0, span / 400.0)          # centreline sample ~ the mesh grid
    chunk = max(120.0, span / 12.0)         # drape piece size
    ribbons = []
    for s in segs:
        for piece in _chop_line(_densify(s, fine), chunk):
            rib = piece.buffer(half, cap_style=1, join_style=1)
            if rib.geom_type == "Polygon" and not rib.is_empty:
                ribbons.append(rib)
            else:
                ribbons.extend(g for g in getattr(rib, "geoms", [])
                               if g.geom_type == "Polygon" and not g.is_empty)
    total_m = sum(s.length for s in segs)
    log(cfg, f"[route] {len(pts)} points, {total_m / 1000:.2f} km on the tile, "
             f"{cfg.route_width_m:.0f} m wide")
    return build_drape(cfg, proj, terr, S, ribbons,
                       cfg.route_height_mm, cfg.embed_mm,
                       label="draping route")


def _shift_vf(vf, dx):
    if vf is None:
        return None
    V, F = vf
    V = np.array(V, dtype=np.float64, copy=True)
    V[:, 0] += dx
    return V, F


# Buffer joins: mitre so a square outline offsets to a square and a hexagon
# keeps its corners; the limit is generous because a 120 deg hex corner needs
# more mitre than a 90 deg one.
_J = dict(join_style=2, mitre_limit=8.0)


def _buf(poly, dist):
    """poly.buffer(dist) with the mitre join, tolerant of an empty result."""
    p = poly.buffer(dist, **_J)
    return p if (not p.is_empty and p.is_valid) else poly


def _cover_posts(shape, cfg, reach):
    """Small columns over the frame border that carry the cover ceiling.

    One at each outline vertex (4 for a square, 6 for a hex), 12 spaced round
    a circle. Every post is clipped to the border ring, so it can never sit
    over the model - same guarantee the old rectangular corner posts had.
    """
    from shapely.geometry import box as _box
    from shapely.ops import unary_union
    c, fw = cfg.frame_clearance_mm, cfg.frame_width_mm
    border = _buf(shape, c + fw).difference(_buf(shape, c))
    ring = _buf(shape, c + fw * 0.5).exterior
    pts = list(ring.coords[:-1])
    if len(pts) > 10:                                    # circle: sample evenly
        n = 12
        pts = [ring.interpolate(i / n, normalized=True).coords[0] for i in range(n)]
    size = max(min(reach, fw - cfg.box_clearance_mm), 1.5)
    posts = [_box(x - size, y - size, x + size, y + size).intersection(border)
             for x, y in pts]
    posts = [p for p in posts if not p.is_empty]
    return unary_union(posts) if posts else border


def build_box(cfg, shape, model_top_mm):
    """
    Two half-covers that slide on from opposite ends and meet in the middle.
    `shape` is the tile outline in mm, centred on the origin; every ring below
    is that outline buffered outward, so a square outline reproduces the old
    rectangular cover to the millimetre and a hex or circle follows its edge.

    Each half is a shell closed on three sides, with the retaining groove
    running along ALL of them. Cross-section through any closed side:

            |                     wall
            |__                   rib   ) frame rim rides
            |                           ) in the gap
            |__                   lip   )

    Slide the two halves together, tape the seam, and the framed model is
    boxed on every face with only the slip fit to move in.
    """
    from shapely.geometry import box as _box

    wall = cfg.box_wall_mm
    clr = cfg.box_clearance_mm
    lip_reach = cfg.cover_lip_mm
    lip_t = cfg.cover_lip_thickness_mm

    frame_outer = _buf(shape, cfg.frame_clearance_mm + cfg.frame_width_mm)
    frame_tall = cfg.frame_floor_mm + cfg.frame_depth_mm

    clear = _buf(frame_outer, clr)            # air gap around the frame
    inner = clear                            # cover cavity
    outer = _buf(clear, wall)                # cover outer wall

    groove_lo = lip_t
    groove_hi = lip_t + frame_tall + 2 * clr
    rib_hi = groove_hi + lip_t
    # the rib sits over the frame's border, never over the model
    rib_reach = min(lip_reach, cfg.frame_width_mm - clr)
    clear_z = model_top_mm + cfg.box_headroom_mm
    top_z = lip_t + clear_z
    total_z = top_z + wall

    minx, miny, maxx, maxy = outer.bounds
    out_x, out_y = maxx - minx, maxy - miny
    cx = (minx + maxx) / 2.0
    pad = max(out_x, out_y) + 2.0

    # A cube cover. The model footprint is capped (the console passes
    # --max-building-mm), so without this a flat tile ships as a stubby lid
    # with a fistful of dead air above it inside a cube shipping box. Raise
    # the cover to a cube - never shrink it, a taller model keeps its taller
    # cover - clamped to the build volume; the walls and ceiling rise, the
    # groove and rib stay pinned to the frame.
    if cfg.box_cube:
        cube = min(max(out_x, out_y), cfg.build_volume_mm)
        if cube > total_z:
            total_z = cube
            top_z = total_z - wall
            clear_z = top_z - lip_t

    def ring_at(reach):
        """The groove/wall ring `reach` wide on the three closed sides, open
        along the seam so the halves can slide together. `band` is only as
        wide as one wall - just enough to clear the seam face where the ring
        crosses it (top and bottom edges); the old rectangular cover tapered
        the groove to nothing there instead."""
        band = wall
        cav = _buf(clear, -reach)
        seam = clear.intersection(_box(cx - band, miny - pad, cx + band, maxy + pad))
        return outer.difference(cav.union(seam))

    lip_ring = ring_at(lip_reach)
    wall_ring = ring_at(0.0)
    rib_ring = ring_at(rib_reach)
    posts = _cover_posts(shape, cfg, rib_reach)

    def half(sign):
        hp = (_box(minx - pad, miny - pad, cx, maxy + pad) if sign < 0
              else _box(cx, miny - pad, maxx + pad, maxy + pad))
        acc = MeshAccum()
        acc.add(prism_any(lip_ring.intersection(hp), 0.0, groove_lo))
        acc.add(prism_any(wall_ring.intersection(hp), groove_lo, groove_hi))
        acc.add(prism_any(rib_ring.intersection(hp), groove_hi, rib_hi))
        acc.add(prism_any(wall_ring.union(posts).intersection(hp), rib_hi, top_z))
        acc.add(prism_any(outer.intersection(hp), top_z, total_z))
        return acc.result()

    left = half(-1)                          # the x <= cx shell
    right = half(+1)                         # the x >= cx shell

    # Park the two halves side by side, clear of the model, so the plate stays
    # inside the printer instead of spread out where the pair would overflow it.
    gap = 8.0
    hw = out_x / 2.0
    P = maxx + 20.0
    left = _shift_vf(left, P - minx)
    right = _shift_vf(right, (P + hw + gap) - cx)

    if cfg.verbose:
        kind = "cube" if cfg.box_cube else "box"
        print(f"  cover: two halves, each {hw:.1f} x {out_y:.1f} x "
              f"{total_z:.1f} mm", file=sys.stderr)
        print(f"         groove all round: lip {lip_reach:.1f} mm under, "
              f"rib {rib_reach:.1f} mm over, {wall:.1f} mm walls",
              file=sys.stderr)
        print(f"         closed {kind}: {out_x:.1f} x {out_y:.1f} x "
              f"{total_z:.1f} mm, {2 * clr:.2f} mm play in every direction",
              file=sys.stderr)
        air = top_z - lip_t - model_top_mm
        print(f"         {air:.1f} mm of air above the tallest point "
              f"(>= {cfg.box_headroom_mm:.1f} mm headroom); corner posts carry "
              f"the ceiling", file=sys.stderr)
        BV = cfg.build_volume_mm
        if max(out_y, hw) > BV or total_z > BV:
            print(f"  [warn] the cover does not fit a {BV:.0f} mm build volume "
                  f"({hw:.0f} x {out_y:.0f} x {total_z:.0f} mm per half)",
                  file=sys.stderr)
    return left, right


def build_frame(cfg, shape):
    """
    A tray the finished model drops into. `shape` is the tile outline in mm,
    centred on the origin; the tray is that outline offset outward.

        cavity = shape + clearance          (the model drops in here)
        outer  = shape + clearance + width
        floor  = `floor_mm` thick, optionally cut back to a `lip` ledge

    A square outline offsets to exactly the rectangle the old W/H math made,
    so square tiles are unchanged; hex and circle follow their own edge.
    """
    from shapely.affinity import translate
    c = cfg.frame_clearance_mm
    fw = cfg.frame_width_mm
    d = cfg.frame_depth_mm
    t = max(cfg.frame_floor_mm, 0.0)
    lip = cfg.frame_lip_mm

    x0, _, x1, _ = shape.bounds
    if not cfg.frame_inplace:
        shape = translate(shape, xoff=(x1 - x0) + c + fw + 10.0)

    cavity = _buf(shape, c)
    outer = _buf(shape, c + fw)

    acc = MeshAccum()
    floor_acc = MeshAccum()
    if t > 0:
        floor = outer
        if cfg.frame_open and lip > 0:
            back = _buf(shape, c - lip)
            if back.is_valid and not back.is_empty and back.area > 1.0:
                floor = outer.difference(back)
        # In water-in-frame mode the floor is the WATER surface: a second
        # filament fused into the same print, which the cut-out land model is
        # glued on top of so the water shows through.
        (floor_acc if cfg.water_in_frame else acc).add(prism_any(floor, 0.0, t))
    acc.add(prism_any(outer.difference(cavity), t, t + d))
    return acc.result(), floor_acc.result()


def resolve_floating(cfg, items):
    """
    Sit every part on something.

    `items` is [(rec, polys, base_m, top_m)] for one building, footprints in
    projected metres. A part is supported when some other part of the same
    building starts lower AND overlaps it in plan -- that is exactly the test
    that keeps a real overhang (the observation pod overlaps the shaft) and
    catches an aerial hanging in space (it overlaps nothing below).

    Returns (items, grounded, dropped).
    """
    if cfg.floating_parts == "keep" or not items:
        return items, 0, 0
    tol = cfg.floating_tol_m
    out, grounded, dropped = [], 0, 0
    for i, (rec, polys, base, top) in enumerate(items):
        if base <= tol:
            out.append((rec, polys, base, top))
            continue
        # the highest point of anything that starts below this part and
        # overlaps it in plan; 0 (the ground) when nothing does
        support = 0.0
        for j, (_r2, polys2, base2, top2) in enumerate(items):
            if i == j or base2 >= base - tol:
                continue
            if any(a.intersects(b) for a in polys for b in polys2):
                support = max(support, min(top2, base))
        gap = base - support
        if gap <= tol:
            out.append((rec, polys, base, top))     # a genuine overhang
            continue
        if cfg.floating_parts == "drop":
            dropped += 1
            continue
        width = min((short_side(p) for p in polys), default=0.0)
        if width <= 1e-6 or gap / width > cfg.floating_max_aspect:
            dropped += 1                            # would become a needle
            continue
        out.append((rec, polys, support, top))
        grounded += 1
    return out, grounded, dropped


def build_buildings(cfg, proj, terr, S, buildings, parts, bbox_poly):
    from shapely.geometry import Point
    from shapely.ops import unary_union
    """
    The landmark fix lives here: when a building has parts, we extrude the PARTS
    (each with its own min_height..height and roof) and discard the parent
    outline. That is what turns the CN Tower from a cylinder into a CN Tower.
    """
    acc = MeshAccum()
    by_parent = defaultdict(list)
    for p in parts:
        by_parent[p.get("building_id")].append(p)

    used_parent = 0
    n_landmark = 0
    n_spire_merged = 0
    n_ok = n_tri_fail = 0
    n_grounded = n_floating_dropped = 0
    n_no_geom = n_off_tile = n_too_small = 0
    tk = Ticker(len(buildings), "extruding buildings", cfg.verbose)
    tallest = 0.0
    made = 0

    def emit(rec, poly, base_m, top_m, is_top=True,
             rule=None, allowed=False, force_spire=False,
             center=None):
        nonlocal n_tri_fail, made, tallest
        gz = terr.elev_xy(np.asarray(poly.exterior.coords)[:, 0],
                          np.asarray(poly.exterior.coords)[:, 1])
        ground = float(np.min(gz))
        z_ground = (ground - terr_min[0]) * S.z + cfg.base_mm

        bs = cfg.building_scale
        z0 = z_ground + base_m * S.z * bs - (cfg.embed_mm if base_m <= 0.01 else 0.0)
        z1 = z_ground + top_m * S.z * bs
        if cfg.max_building_mm > 0:
            z1 = min(z1, z_ground + cfg.max_building_mm)
        if z1 - z0 < cfg.min_building_height_mm:
            z1 = z0 + cfg.min_building_height_mm

        spec = part_spec(rule, float(top_m), float(base_m), short_side(poly))
        prof_m = (spec or {}).get("profile") if spec else (rule or {}).get("profile")
        spin = float((spec or {}).get("rotate", 0.0))

        pmm_true = scale_poly(poly, proj, S)          # before inflation
        if spin and center is not None:
            from shapely import affinity
            pmm_true = affinity.rotate(pmm_true, spin,
                                       origin=(float(center[0]), float(center[1])))
        want = cfg.min_feature_mm
        mf = float((spec or {}).get("min_feature", cfg.min_feature_mm))
        want = mf
        if prof_m:
            pass          # profiles clamp instead of inflating (see world_profile)
        elif force_spire:
            tipr = float((rule or {}).get("spire_tip", cfg.spire_tip_ratio))
            want = cfg.min_feature_mm / max(tipr, 0.05)
        pmm = ensure_min_feature(pmm_true, want)
        pmm = pmm.simplify(0.004, preserve_topology=True)
        if pmm.is_empty or pmm.geom_type != "Polygon":
            pmm = pmm_true

        cap = None
        if cfg.roof_shapes and cfg.lod >= 2 and cfg.roof_mode != "none":
            rh = rec.get("roof_height")
            shape = rec.get("roof_shape")
            if shape and shape != "flat":
                rh_mm = (float(rh) * S.z) if rh else min(0.35 * (z1 - z0),
                                                         3.5 * S.z)
                # Clamp by roof FAMILY. A pyramidal/cone/dome spire really is
                # mostly roof, so it needs headroom. A gabled or hipped roof
                # that eats 98% of the building turns the whole volume into a
                # wedge - which is what a single global clamp did to the city.
                pointed = shape in ("pyramidal", "cone", "dome", "spherical",
                                    "round", "onion")
                frac = cfg.roof_height_max_frac if pointed else cfg.ridge_roof_max_frac
                rh_mm = max(0.15, min(rh_mm, frac * (z1 - z0)))
                cap = roof_cap(pmm, shape, rh_mm, rec.get("roof_direction"),
                               cfg.min_feature_mm, mode=cfg.roof_mode,
                               orientation=rec.get("roof_orientation"))
        span = max(pmm.bounds[2] - pmm.bounds[0], pmm.bounds[3] - pmm.bounds[1])
        chunks = robust_pieces(pmm, max(span / 4.0, 0.5))

        prof = None
        if cap is None and len(chunks) == 1:
            if prof_m:
                # explicit silhouette, anchored to absolute height
                prof = world_profile(
                    prof_m, z0, z1, z_ground, S.z,
                    min_scale=mf / max(short_side(pmm), 1e-6))
            else:
                # measure slenderness on the REAL footprint. A 5 m mast is a
                # spire even after we fatten it to something the nozzle can lay.
                aspect = (z1 - z0) / short_side(pmm_true)
                prof = taper_profile(cfg, aspect, is_top, rule, allowed,
                                     min_scale=cfg.min_feature_mm /
                                     max(short_side(pmm), 1e-6),
                                     force_spire=force_spire)

        ok = False
        for ch in chunks:
            use_cap = cap if len(chunks) == 1 else None
            vf = (tapered_prism(ch, z0, z1, prof, center)
                  if prof else None)
            if vf is None:
                vf = prism(ch, z0, z1, use_cap)
            if vf is None and use_cap is not None:
                vf = prism(ch, z0, z1, None)   # roof failed -> flat, never drop
            ok |= acc.add(vf)
        if ok:
            made += 1
            tallest = max(tallest, z1 - z0)
        else:
            n_tri_fail += 1

    for _bi, rec in enumerate(buildings):
        tk.tick(_bi)
        geom = to_shape(rec.get("wkb"))
        if geom is None or geom.is_empty:
            n_no_geom += 1
            continue
        geom = proj.geom(geom)
        polys = clean_polys(geom, bbox_poly, cfg.min_footprint_m2)
        if not polys:
            if clean_polys(geom, bbox_poly, 0.0):
                n_too_small += 1
            else:
                n_off_tile += 1
            continue
        n_ok += 1

        rule = landmark_rule(cfg, rec)
        kids = by_parent.get(rec.get("id"), []) if cfg.lod >= 2 else []
        if cfg.use_building_parts and kids:
            used_parent += 1
            parent_h, _ = resolve_height(rec, cfg)

            def _kh(p):
                h = p.get("height")
                return float(h) if h else float(p.get("min_height") or 0.0)

            top_part = max(kids, key=_kh)

            # every part of a profiled landmark scales about the SAME point,

            # or the stack drifts sideways as it narrows

            shared_c = None
            if rule and polys:
                # MUST be the area centroid. representative_point() returns any
                # convenient interior point - on a symmetric 3-leg outline it
                # lands metres off-axis, so the legs converge toward a point
                # beside the tower and swing sideways as they narrow (the "fan").
                if rule.get("center"):
                    cx, cy = rule["center"]
                    px, py = proj.fwd.transform(cx, cy)
                    shared_c = np.asarray(
                        scale_poly(Point(px, py).buffer(1e-6), proj, S)
                        .centroid.coords[0], dtype=float)
                else:
                    # Anchor on the TOPMOST part, not the parent outline. The
                    # outline includes the base podium, so its centroid sits
                    # off the tower axis - and scaling about an off-axis point
                    # slides each leg around the tower (its own orientation is
                    # preserved, so the inward face ends up pointing sideways).
                    # The highest part is the mast: on-axis by construction.
                    anchor = None
                    try:
                        ag = proj.geom(to_shape(top_part.get("wkb")))
                        ap = clean_polys(ag, bbox_poly, 0.0)
                        if ap:
                            anchor = unary_union([scale_poly(p, proj, S)
                                                  for p in ap]).centroid
                    except Exception:
                        anchor = None
                    if anchor is None or anchor.is_empty:
                        anchor = unary_union([scale_poly(p, proj, S)
                                              for p in polys]).centroid
                    shared_c = np.asarray(anchor.coords[0], dtype=float)

            # Each Overture part is a prism, so a mast modelled as 3-4 stacked
            # parts renders as a stepped column with a blunt cap - the rings
            # visible on a tall tower. Collapse the topmost run of slender
            # parts into a single smooth taper instead.
            spire_stack = []
            if rule is not None and rule.get("merge_spire", True):
                ordered = sorted(kids, key=lambda p: float(p.get("min_height") or 0.0))
                thin = float(rule.get("spire_merge_width", 12.0))   # metres
                for k in reversed(ordered):
                    try:
                        g = proj.geom(to_shape(k.get("wkb")))
                        w = short_side(g)
                    except Exception:
                        break
                    if w <= thin:
                        spire_stack.append(k)
                    else:
                        break
                spire_stack.reverse()
            if len(spire_stack) < 2:
                spire_stack = []
            else:
                merged_ids = {id(k) for k in spire_stack}
                base_k = spire_stack[0]
                z_lo = float(base_k.get("min_height") or 0.0)
                z_hi = max(_kh(k) for k in spire_stack)
                n_spire_merged += 1
            n_landmark += 1 if rule else 0

            prepared = []
            for k in kids:
                kg = to_shape(k.get("wkb"))
                kg = proj.geom(kg) if kg is not None else None
                kpolys = clean_polys(kg, bbox_poly, cfg.min_footprint_m2 * 0.25)
                if not kpolys:
                    continue
                kh = k.get("height")
                if kh is None or kh <= 0:
                    kh, _ = resolve_height(k, cfg)
                    kh = min(kh, parent_h)
                kmin = float(k.get("min_height") or 0.0)
                if kh <= kmin:
                    kh = kmin + 2.0
                prepared.append((k, kpolys, kmin, float(kh)))

            # The merged spire is one solid standing on the part below it, so
            # it is resolved as a single item rather than as its members.
            if spire_stack:
                prepared = [it for it in prepared if id(it[0]) not in merged_ids
                            or it[0] is base_k]
                prepared = [(k, kp, z_lo, z_hi) if k is base_k else (k, kp, b, t)
                            for k, kp, b, t in prepared]

            prepared, g, d = resolve_floating(cfg, prepared)
            n_grounded += g
            n_floating_dropped += d

            for k, kpolys, kmin, kh in prepared:
                if spire_stack and k is base_k:
                    for kp in kpolys:
                        emit(k, kp, kmin, kh, is_top=True,
                             rule=rule, allowed=True, force_spire=True,
                             center=shared_c)
                    continue
                for kp in kpolys:
                    emit(k, kp, kmin, kh, is_top=(k is top_part),
                         center=shared_c,
                         rule=rule, allowed=True)
        else:
            h, _src = resolve_height(rec, cfg)
            mh = float(rec.get("min_height") or 0.0)
            top = max(h, mh + 2.0)
            if mh > cfg.floating_tol_m:
                # No parts, so by definition there is nothing underneath: a
                # min_height here is a building starting in mid-air.
                one, g, d = resolve_floating(cfg, [(rec, polys, mh, top)])
                n_grounded += g
                n_floating_dropped += d
                if not one:
                    continue
                _r, polys, mh, top = one[0]
            for p in polys:
                emit(rec, p, mh, top, rule=rule,
                     allowed=not cfg.taper_landmarks_only)

    stage("buildings with valid geometry", n_ok)
    stage("dropped: no/empty geometry", n_no_geom)
    stage("dropped: outside tile after clip", n_off_tile)
    stage(f"dropped: footprint < {cfg.min_footprint_m2:g} m2", n_too_small)
    stage("buildings expanded into parts", used_parent)
    stage("landmark taper rules matched", n_landmark)
    stage("spire stacks merged", n_spire_merged)
    stage("floating parts sat down on what is below", n_grounded)
    stage("floating parts dropped (would print as a needle)", n_floating_dropped)
    stage("solids written", made)
    stage("solids dropped (triangulation)", n_tri_fail)
    return acc.result()


def scale_poly(poly, proj, S):
    from shapely.affinity import affine_transform
    cx = (proj.minx + proj.maxx) / 2
    cy = (proj.miny + proj.maxy) / 2
    return affine_transform(poly, [S.xy, 0, 0, S.xy, -cx * S.xy, -cy * S.xy])


def tile_poly(cfg, proj):
    """The tile outline in projected metres: the clip every layer is cut to.

    square is the whole bbox. hex (flat-top) and circle are inscribed in the
    shorter side and centred, so a not-quite-square drag still fits. Everything
    downstream keys off this: clean_polys() intersects each layer with it, the
    terrain plinth is built from it minus water, and the frame and cover offset
    it.
    """
    from shapely.geometry import box, Point, Polygon
    x0, y0, x1, y1 = proj.minx, proj.miny, proj.maxx, proj.maxy
    shape = (cfg.tile_shape or "square").lower()
    if shape == "square":
        return box(x0, y0, x1, y1)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    r = min(x1 - x0, y1 - y0) / 2.0
    if shape == "circle":
        return Point(cx, cy).buffer(r, 96)   # positional: quad_segs (2.x) / resolution (1.x)
    if shape == "hex":
        # flat-top: two vertices on the horizontal axis, spans 2r across
        pts = [(cx + r * math.cos(math.radians(a)),
                cy + r * math.sin(math.radians(a)))
               for a in range(0, 360, 60)]
        return Polygon(pts)
    raise ValueError(f"unknown tile_shape {cfg.tile_shape!r} "
                     "(square | hex | circle)")


def finalize(vf, name=""):
    """Drop degenerate faces, make winding consistent, report watertightness.

    Deliberately does NOT weld vertices across separate solids: two buildings
    that happen to touch would become one non-manifold blob.
    """
    if vf is None:
        return None
    import trimesh
    V, F = vf
    m = trimesh.Trimesh(vertices=np.asarray(V, dtype=np.float64),
                        faces=np.asarray(F, dtype=np.int64), process=False)
    # NOTE: only drop faces that are degenerate by index. Area-based culling
    # removes slivers that are load-bearing for the topology and opens the shell.
    keep = ~((m.faces[:, 0] == m.faces[:, 1]) | (m.faces[:, 1] == m.faces[:, 2])
             | (m.faces[:, 0] == m.faces[:, 2]))
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    try:
        trimesh.repair.fix_normals(m, multibody=True)
    except Exception:
        pass
    fa = np.asarray(m.faces)
    e = np.vstack([fa[:, [0, 1]], fa[:, [1, 2]], fa[:, [2, 0]]])
    e.sort(axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    open_edges = int((counts == 1).sum())
    nm = int((counts > 2).sum())
    flag = "watertight" if open_edges == 0 and nm == 0 else \
           f"OPEN({open_edges}) NONMANIFOLD({nm})"
    print(f"  [mesh] {name:<10s} {len(m.vertices):>7d} v {len(m.faces):>7d} f  {flag}",
          file=sys.stderr)
    return np.asarray(m.vertices), np.asarray(m.faces)


# ----------------------------------------------------------------------------
# 7. 3MF writer
# ----------------------------------------------------------------------------

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_M = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"

# Bambu SKU -> the filament preset that ships with Bambu Studio. The full
# preset name is "<this> @BBL <printer> <nozzle> nozzle".
PRESET_BY_ID = {
    "GFA00": "Bambu PLA Basic",
    "GFA01": "Bambu PLA Matte",
    "GFG02": "Bambu PETG HF",
}


def _xml_attr(v):
    return (str(v).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


BAMBU_TEMPLATE = "bambu_p1s_0.4.json"
NS_P = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_B = "http://schemas.bambulab.com/package/2021"


def _bambu_template():
    """
    Bambu Studio's own project settings, as a base to overwrite.

    A PARTIAL config is not merged -- Bambu throws the whole thing away and
    falls back to one filament, which is how four objects on extruders 1-4 all
    came out the same colour. Even a complete-looking config assembled by hand
    is rejected; only settings Bambu itself wrote survive. So the template is
    generated by Bambu and shipped alongside this file.

    Regenerate with (adjust the version paths):

        bambu-studio.exe any.3mf \
          --load-settings "<profiles>/BBL/machine/Bambu Lab P1S 0.4 nozzle.json;\
<profiles>/BBL/process/0.20mm Standard @BBL X1C.json" \
          --load-filaments "<profiles>/BBL/filament/Bambu PLA Basic @BBL P1S 0.4 nozzle.json" \
          --export-3mf out.3mf

    then take Metadata/project_settings.config out of out.3mf. Load it with ONE
    filament so every per-filament list has length 1 and widening below is
    deterministic.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BAMBU_TEMPLATE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# Settings that are one-entry-per-filament, and the two that are
# one-entry-per-PRESET (process + printer + each filament, so n + 2).
#
# Derived, not guessed: export the same model from Bambu Studio twice, once
# with one filament loaded and once with four, and diff the two configs. The
# keys that go 1 -> 4 are per-filament; the keys that go 3 -> 6 are per-preset.
# Fifteen of the twenty start with "filament" and five do not, which is exactly
# the trap -- widening only the "filament*" keys leaves nozzle_temperature at
# length 1, the config is internally inconsistent, and Bambu throws all of it
# away and silently prints in one colour.
PER_FILAMENT = (
    "filament_colour", "filament_cost", "filament_density", "filament_diameter",
    "filament_flow_ratio", "filament_ids", "filament_is_support",
    "filament_max_volumetric_speed", "filament_minimal_purge_on_wipe_tower",
    "filament_notes", "filament_self_index", "filament_settings_id",
    "filament_soluble", "filament_type", "filament_vendor",
    "long_retractions_when_ec", "nozzle_temperature",
    "nozzle_temperature_initial_layer", "retraction_distances_when_ec",
    "slow_down_min_speed",
)
PER_PRESET = ("different_settings_to_system", "inherits_group")


def _project_settings(cfg, fils):
    """The template, widened to one entry per filament and given our colours."""
    tpl = _bambu_template()
    if tpl is None:
        return None
    out = dict(tpl)
    _, fils = slot_map(fils)          # the distinct filaments, in slot order
    n = len(fils)

    def widen(key, want):
        v = out.get(key)
        if not isinstance(v, list) or not v:
            return
        out[key] = (v * want)[:want] if len(v) < want else v[:want]

    for k in PER_FILAMENT:
        widen(k, n)
    for k in PER_PRESET:
        widen(k, n + 2)
    machine = cfg.printer_model.replace("Bambu Lab ", "")
    nozzle = f"{cfg.nozzle_mm:g}"
    out["filament_colour"] = [f[3] for f in fils]
    out["filament_type"] = [f[1] for f in fils]
    out["filament_ids"] = [f[2] for f in fils]
    out["filament_settings_id"] = [
        f"{PRESET_BY_ID.get(f[2], 'Bambu PLA Basic')} @BBL {machine} {nozzle} nozzle"
        for f in fils]
    out["filament_is_support"] = ["0"] * n
    # filament_self_index is 1..n, not n copies of "1".
    out["filament_self_index"] = [str(i) for i in range(1, n + 1)]
    # No extra keys of our own in here. The config is validated as a whole and
    # a stray key is not worth the risk; the slot list is printed to the
    # console at the end of a run instead.
    return out


def _uuid(a, b=0):
    return "%08x-%04x-4c03-9d28-80fed5dfa1dc" % (a, b)


def slot_map(fils):
    """
    Which AMS slot each part goes in, and the distinct filaments to load.

    Slots are per FILAMENT, not per part. Two parts asking for the same
    filament share a slot -- you cannot load one spool into two trays, and
    pretending otherwise makes Bambu treat the plate as a multi-colour print:
    it builds a prime tower and swaps filament for nothing. The cover is the
    obvious case, two halves of one PETG shell.
    """
    slots, order = {}, []
    for f in fils:
        if f not in slots:
            slots[f] = len(order) + 1
            order.append(f)
    return [slots[f] for f in fils], order


def _bambu_model_settings(cid, layers, fils):
    """
    One object, one part per layer, each part on its own extruder.

    NOT one object per layer: those occupy the same space, and Bambu's arrange
    treats four colliding objects as four things to spread out -- it put our
    plate on four separate plates. As parts of a single object they stay put
    and the tile prints as one piece.
    """
    assign, _ = slot_map(fils)
    parts = []
    for i, ((name, vf), _f) in enumerate(zip(layers, fils), start=1):
        nfaces = len(vf[1]) if vf is not None else 0
        parts.append(
            f'    <part id="{i}" subtype="normal_part">\n'
            f'      <metadata key="name" value="{_xml_attr(name)}"/>\n'
            f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            f'      <metadata key="extruder" value="{assign[i - 1]}"/>\n'
            f'      <mesh_stat face_count="{nfaces}" edges_fixed="0" '
            f'degenerate_facets="0" facets_removed="0" facets_reversed="0" '
            f'backwards_edges="0"/>\n    </part>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
            f'  <object id="{cid}">\n'
            '    <metadata key="name" value="map2model"/>\n'
            '    <metadata key="extruder" value="1"/>\n'
            + "\n".join(parts) + '\n  </object>\n'
            '  <plate>\n'
            '    <metadata key="plater_id" value="1"/>\n'
            '    <metadata key="plater_name" value=""/>\n'
            '    <metadata key="locked" value="false"/>\n'
            '    <model_instance>\n'
            f'      <metadata key="object_id" value="{cid}"/>\n'
            '      <metadata key="instance_id" value="0"/>\n'
            '    </model_instance>\n  </plate>\n</config>\n')


def write_3mf(path, layers, cfg=None):
    """
    layers: list of (name, (V, F)). Each becomes one coloured part.

    Two shapes come out of here:

    * a Bambu Studio PROJECT (the default) -- production extension, one object
      whose components are the layers, per-part extruders, and Bambu's own
      project settings with our filaments written in. This is the only shape
      that actually opens with the right colour and filament in each AMS slot;
      everything short of it is silently reduced to a single filament.
    * a plain 3MF (`--no-bambu-project`) -- geometry plus the standard
      materials extension, for any other slicer.

    Colour is written the standard way in BOTH: <basematerials> with per-object
    pid/pindex, and <m:colorgroup>, which is what Bambu's non-project reader
    looks at. Neither is in requiredextensions, so a slicer that knows no
    material extension still loads the geometry.
    """
    layers = [(n, vf) for n, vf in layers if vf is not None]
    if not layers:
        return
    fils = [filament_of(cfg, n) if cfg is not None
            else FILAMENTS[DEFAULT_FILAMENTS.get(n, "basic_gray")]
            for n, _ in layers]
    project = (cfg is not None and cfg.bambu_project
               and _bambu_template() is not None)

    bases, colors, objs, comps, items = [], [], [], [], []
    for i, ((name, (V, F)), fil) in enumerate(zip(layers, fils), start=1):
        bases.append(f'<base name="{_xml_attr(fil[0])}" '
                     f'displaycolor="{fil[3]}FF"/>')
        colors.append(f'<m:color color="{fil[3]}FF"/>')
        buf = io.StringIO()
        buf.write(f'<object id="{i}" name="{_xml_attr(name)}" type="model" '
                  f'pid="900" pindex="{i - 1}"'
                  + (f' p:UUID="{_uuid(i)}"' if project else '')
                  + '><mesh><vertices>')
        for x, y, z in V:
            buf.write(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>')
        buf.write('</vertices><triangles>')
        for a, b, c in F:
            if a == b or b == c or a == c:
                continue
            buf.write(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')
        buf.write('</triangles></mesh></object>')
        objs.append(buf.getvalue())
        comps.append(f'<component objectid="{i}" p:UUID="{_uuid(i, 1)}" '
                     f'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
        items.append(f'<item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 0 0 0" '
                     f'partnumber="{_xml_attr(name)}"/>')

    res = (f'<basematerials id="900">{"".join(bases)}</basematerials>'
           f'<m:colorgroup id="901">{"".join(colors)}</m:colorgroup>'
           + "".join(objs))

    if project:
        cid = len(layers) + 1
        # Sit the plate ON the bed and centred. Our geometry is built around
        # the origin with negative Z, which is the bed's corner and below the
        # plate; the offset rides in the build item so the mesh is untouched
        # and serve.py still reads what it always did.
        allv = np.vstack([vf[0] for _, vf in layers])
        lo, hi = allv.min(axis=0), allv.max(axis=0)
        bed = cfg.build_volume_mm
        dx = bed / 2 - (lo[0] + hi[0]) / 2
        dy = bed / 2 - (lo[1] + hi[1]) / 2
        dz = -lo[2]
        res += (f'<object id="{cid}" p:UUID="{_uuid(cid)}" type="model">'
                f'<components>{"".join(comps)}</components></object>')
        build = (f'<build p:UUID="{_uuid(0xB1, 3)}">'
                 f'<item objectid="{cid}" p:UUID="{_uuid(cid, 2)}" '
                 f'transform="1 0 0 0 1 0 0 0 1 {dx:.6f} {dy:.6f} {dz:.6f}" '
                 f'printable="1"/></build>')
        head = (f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS}" '
                f'xmlns:BambuStudio="{NS_B}" xmlns:p="{NS_P}" xmlns:m="{NS_M}" '
                f'requiredextensions="p">'
                f'<metadata name="Application">BambuStudio-02.05.00.66</metadata>'
                f'<metadata name="BambuStudio:3mfVersion">1</metadata>'
                f'<metadata name="Title">map2model</metadata>')
    else:
        build = '<build>' + "".join(items) + '</build>'
        head = (f'<model unit="millimeter" xmlns="{NS}" xmlns:m="{NS_M}">'
                '<metadata name="Application">map2model</metadata>')

    xml = ('<?xml version="1.0" encoding="UTF-8"?>' + head
           + '<resources>' + res + '</resources>' + build + '</model>')

    ct = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
          '<Default Extension="png" ContentType="image/png"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", xml)
        if project:
            z.writestr("Metadata/model_settings.config",
                       _bambu_model_settings(len(layers) + 1, layers, fils))
            z.writestr("Metadata/project_settings.config",
                       json.dumps(_project_settings(cfg, fils), indent=1,
                                  sort_keys=True))


# ----------------------------------------------------------------------------
# 8. Orchestration
# ----------------------------------------------------------------------------


@dataclass
class Scale:
    xy: float
    z: float


terr_min = [0.0]   # module-level so the drape/emit helpers can reach it
# In --mode terrain with "tallest point = N mm" set, this scales metres of
# real relief so the highest sample lands at terrain_relief_mm. 1.0 (true
# scale) everywhere else, so the city path is untouched.
terr_relief_scale = [1.0]


MODELS_DIR = "3Dmodels"


def model_out_path(out_path):
    """
    Route a bare filename into 3Dmodels/<name>/<name>.3mf.

    --split writes its siblings next to whatever file it is handed, so one
    model is 2-3 .3mf files that only belong together by their names. Giving
    each model its own folder under 3Dmodels/ keeps a set together instead of
    scattering plates through the working directory.

    A path that already names a directory is returned untouched: serve.py
    hands run() an absolute temp path, and an explicit `-o out/city.3mf` means
    the caller has already chosen where it goes.
    """
    if os.path.dirname(out_path):
        return out_path
    stem = os.path.splitext(os.path.basename(out_path))[0]
    return os.path.join(MODELS_DIR, stem, out_path)


def run(cfg, out_path):
    from shapely.geometry import box

    out_path = model_out_path(out_path)
    print(f"map2model {__version__}  source={cfg.source}  lod={cfg.lod}"
          + (f"  mode={cfg.mode}" if cfg.mode != "city" else ""),
          file=sys.stderr)
    print(f"  writing to {os.path.dirname(os.path.abspath(out_path))}",
          file=sys.stderr)
    if cfg.mode in ("terrain", "route"):
        # A relief map, or a circuit on the relief, is the land and the water.
        # The city layers only get in the way of reading it.
        cfg.want_buildings = cfg.want_roads = cfg.want_greenery = False
    if cfg.mode == "route":
        # Resolve the path now - from a name (OSM), a file, or the inline
        # string - so the blank-tile guard below can see it and so a bad name
        # fails before the S3 scans, not after. main() may have resolved it
        # already to fit the bbox; reuse that rather than hit Overpass twice.
        pts = getattr(cfg, "_route_pts", None)
        if not pts:
            if cfg.route_name:
                with Stage("resolve route (Overpass)", cfg.verbose):
                    pts = fetch_route_osm(cfg.route_name, cfg.overpass_url,
                                          cfg.osm_timeout)
            elif cfg.route_file:
                pts = read_route_file(cfg.route_file)
            else:
                pts = parse_route(cfg.route)
        if len(pts) < 2:
            raise RuntimeError(
                "--mode route needs a path: --route-name \"...\", "
                "--route-file PATH, or --route \"lat,lon;lat,lon;...\"")
        cfg._route_pts = pts
    terr_relief_scale[0] = 1.0
    proj = Projector(cfg.bbox)
    span_x = proj.maxx - proj.minx
    span_y = proj.maxy - proj.miny
    S = Scale(xy=cfg.size_mm / max(span_x, span_y),
              z=cfg.size_mm / max(span_x, span_y) * cfg.z_exaggeration)
    log(cfg, f"[scale] {span_x:.0f} x {span_y:.0f} m -> {cfg.size_mm} mm "
             f"(1 mm = {1/S.xy:.1f} m)")

    with Stage("load terrain", cfg.verbose):
        terr = Terrain(cfg, proj)
        # The tile outline. square is the whole bbox; hex/circle inscribe it.
        # Every layer clips to this (clean_polys intersects it), so a hex tile
        # is a hex of city without another line of clipping code.
        bbox_poly = tile_poly(cfg, proj)
        if cfg.tile_shape != "square":
            full = box(proj.minx, proj.miny, proj.maxx, proj.maxy).area
            log(cfg, f"[shape] {cfg.tile_shape} tile, "
                     f"{bbox_poly.area / full * 100:.0f}% of the bbox")
            if not cfg.water_in_frame and cfg.mode == "city":
                # The land plinth only follows a non-rectangular outline on the
                # water-in-frame path (build_drape); the plain build_terrain
                # grid stays rectangular, so its corners would foul the shaped
                # frame and the model would not drop in. The console and the
                # copied command always pass --water-in-frame. (Terrain mode
                # builds bands, which clip to bbox_poly, so it is fine.)
                print(f"[warn] --tile-shape {cfg.tile_shape} without "
                      f"--water-in-frame: the terrain plinth stays rectangular "
                      f"and will not fit the {cfg.tile_shape} frame. Add "
                      f"--water-in-frame.", file=sys.stderr)
        gx = np.linspace(proj.minx, proj.maxx, 60)
        gy = np.linspace(proj.miny, proj.maxy, 60)
        GX, GY = np.meshgrid(gx, gy)
        _samp = terr.elev_xy(GX, GY)
        terr_min[0] = float(np.min(_samp))
        # "tallest point = N mm": scale real relief so the highest sample lands
        # at terrain_relief_mm. terrain and route modes; the city path stays
        # 1.0 (a skyline needs true ground under it).
        if cfg.mode in ("terrain", "route") and cfg.terrain_relief_mm > 0:
            span_m = float(np.max(_samp)) - terr_min[0]
            if span_m > 1e-6:
                terr_relief_scale[0] = (cfg.terrain_relief_mm / S.z) / span_m
                log(cfg, f"[relief] tallest point {span_m:.0f} m -> "
                         f"{cfg.terrain_relief_mm:.0f} mm "
                         f"(x{terr_relief_scale[0] * S.z:.2f} of true)")

    del FETCH_ERRORS[:]
    _EMPTY_RETRIED[0] = False
    buildings, parts, water, green, roads = fetch_all(cfg)

    # NEVER write a blank tile. A failed query returns [] exactly like an empty
    # tile does, so the exporter used to sail on and produce a bare terrain slab
    # - a plate a customer could actually be sent. Stop here instead, and say
    # which it was, because the two need opposite responses: retry versus move
    # the tile.
    #
    # terrain and route modes have their own content: real relief for terrain,
    # the drawn path for route. Only block those when even that is missing - a
    # flat DEM (terrarium fetch failed, or genuinely open ocean) for terrain,
    # or a path that turned out empty for route.
    have_vectors = bool(buildings or roads or green or water)
    route_ok = (cfg.mode == "route"
                and len(getattr(cfg, "_route_pts", []) or []) >= 2)
    terrain_ok = cfg.mode == "terrain" and not terr.flat
    if not (have_vectors or route_ok or terrain_ok):
        if FETCH_ERRORS:
            detail = "; ".join(f"{w}: {m.splitlines()[0][:160]}"
                               for w, m in FETCH_ERRORS)
            raise RuntimeError(
                "every data query failed, so there is nothing to build. This is "
                "a fetch problem, not an empty tile - try again in a minute. "
                + detail)
        if cfg.mode == "terrain":
            raise RuntimeError(
                "no elevation data for this tile, so a relief model would be a "
                "flat slab. The terrain server may be down - try again shortly.")
        if cfg.mode == "route":
            raise RuntimeError(
                "no route path - pass --route-name, --route-file or --route.")
        raise RuntimeError(
            "this tile has no buildings, roads, greenery or water in Overture, "
            "so there is nothing to build but bare ground. Move the tile over "
            "somewhere built up, or widen it.")
    if FETCH_ERRORS:
        for what, msg in FETCH_ERRORS:
            print(f"[warn] {what} came back empty because the query FAILED, not "
                  f"because the tile is empty - this layer is missing from the "
                  f"model: {msg.splitlines()[0][:160]}", file=sys.stderr)

    # Scale advisory. Below ~8 m/mm a landmark mast is a few tenths of a mm and
    # has to be fattened to nozzle width, which wrecks its proportions far more
    # than any taper setting can repair.
    m_per_mm = 1.0 / S.xy
    if m_per_mm > 8.0:
        print(f"[note] {m_per_mm:.1f} m per mm. Features under "
              f"{cfg.min_feature_mm * m_per_mm:.0f} m get widened to "
              f"{cfg.min_feature_mm:.2f} mm so they print, so masts and spires "
              f"come out proportionally too thick.\n"
              f"       For landmark shape, aim for <8 m/mm: a "
              f"{cfg.size_mm:.0f} mm print wants a tile under "
              f"{cfg.size_mm * 8 / 1000:.1f} km across.", file=sys.stderr)

    def to_polys(recs, min_area):
        out = []
        for r in recs:
            g = to_shape(r.get("wkb"))
            if g is None:
                continue
            out.extend(clean_polys(proj.geom(g), bbox_poly, min_area))
        return out

    def to_green_polys(recs, min_area):
        """Greenery, minus the raster blankets.

        A land_cover feature can be a continent-sized dissolve of a coarse
        global raster. Clipped to a city tile it is a solid green sheet over
        everything - that was Monaco, buried under one 1.4-million-km2 'forest'
        polygon. Judging it by the clipped area alone would also throw away a
        park that genuinely fills the tile, so require BOTH tests: it covers
        the tile, and its source polygon dwarfs the tile.
        """
        tile_area = bbox_poly.area
        out, dropped = [], []
        for r in recs:
            g = to_shape(r.get("wkb"))
            if g is None:
                continue
            gp = proj.geom(g)
            polys = clean_polys(gp, bbox_poly, min_area)
            if polys and tile_area > 0:
                covered = sum(p.area for p in polys) / tile_area
                try:
                    scale = gp.area / tile_area
                except Exception:
                    scale = 0.0
                if (covered >= cfg.green_blanket_frac
                        and scale >= cfg.green_blanket_scale):
                    dropped.append((covered, scale, r.get("subtype")))
                    continue
            out.extend(polys)
        for covered, scale, subtype in dropped:
            print(f"[warn] dropped a blanket greenery feature "
                  f"(subtype={subtype}): it covers {covered * 100:.0f}% of the "
                  f"tile and its source polygon is {scale:,.0f}x the tile area. "
                  f"That is a coarse global land_cover dissolve, not a park.",
                  file=sys.stderr)
        return out

    def to_road_polys(recs):
        out = []
        for r in recs:
            g = to_shape(r.get("wkb"))
            if g is None:
                continue
            g = proj.geom(g)
            w = cfg.road_width_m.get(r.get("class") or "", 8.0)
            try:
                buf = g.buffer(w / 2.0, cap_style=2, join_style=1)
            except Exception:
                continue
            out.extend(clean_polys(buf, bbox_poly, 1.0))
        return out

    with Stage("project + clean vectors", cfg.verbose):
        water_p = to_polys(water, 20.0)
        green_p = to_green_polys(green, 40.0)
        road_p = to_road_polys(roads)
        stage("water polygons", len(water_p))
        stage("greenery polygons", len(green_p))
        stage("road polygons", len(road_p))

    # dissolve overlapping features, then re-simplify: unary_union reintroduces
    # collinear vertices at every intersection, which ear-clip to zero area.
    from shapely.ops import unary_union

    def dissolve(polys, tol=0.05):
        if not polys:
            return []
        u = unary_union(polys)
        if u.is_empty:
            return []
        # self-touching rings (roads meeting at a corner) make ear-clipping skip
        # boundary edges, which punches holes in the shell. An epsilon
        # open-then-close separates them.
        try:
            u = u.buffer(0.002, join_style=2).buffer(-0.002, join_style=2)
        except Exception:
            pass
        u = u.simplify(tol, preserve_topology=True)
        geoms = list(u.geoms) if u.geom_type == "MultiPolygon" else [u]
        return [g for g in geoms if g.geom_type == "Polygon" and not g.is_empty]

    with Stage("dissolve overlaps", cfg.verbose):
        road_p = dissolve(road_p)
        water_p = dissolve(water_p)
        green_p = dissolve(green_p)

    # keep greenery and roads out of the water
    if water_p:
        wu = unary_union(water_p)

        def subtract(polys):
            out = []
            for p in polys:
                try:
                    d = p.difference(wu)
                except Exception:
                    out.append(p)
                    continue
                if d.is_empty:
                    continue
                # difference usually returns a MultiPolygon - flatten it,
                # don't discard it
                geoms = (list(d.geoms)
                         if d.geom_type in ("MultiPolygon", "GeometryCollection")
                         else [d])
                out.extend(g for g in geoms
                           if g.geom_type == "Polygon" and g.area > 1e-9)
            return out

        green_p = subtract(green_p)
        road_p = subtract(road_p)

    raw = []
    if cfg.mode == "terrain" and cfg.terrain_bands > 1:
        # Relief cut into stepped elevation bands, each its own filament. The
        # bands clip to bbox_poly themselves, so this works for hex/circle too.
        with Stage("mesh terrain bands", cfg.verbose):
            raw.extend(build_terrain_bands(cfg, proj, terr, S, bbox_poly, water_p))
    elif cfg.water_in_frame or cfg.mode == "route":
        # The land plinth is the tile MINUS the water, cut clean through, so
        # the water surface printed into the frame shows from underneath.
        # Route mode always builds this plinth - it is what the ribbon drapes
        # onto - whether or not water rides the frame.
        with Stage("mesh terrain (water cut out)", cfg.verbose):
            land = bbox_poly
            shore = None
            if water_p:
                try:
                    wu = unary_union(water_p)
                    land = bbox_poly.difference(wu)
                    if cfg.shore_ramp_mm > 0:
                        # ramp the plinth edge down to the datum at the water,
                        # not a vertical seawall - see build_drape
                        bm = cfg.shore_ramp_mm / S.xy
                        shore = (wu.simplify(max(1.0, bm / 8.0),
                                             preserve_topology=False),
                                 bm, cfg.base_mm)
                except Exception:
                    land = bbox_poly
            lands = (list(land.geoms)
                     if land.geom_type in ("MultiPolygon", "GeometryCollection")
                     else [land])
            lands = [g for g in lands if g.geom_type == "Polygon" and not g.is_empty]
            raw.append(("terrain",
                        build_drape(cfg, proj, terr, S, lands, 0.0, 0.0,
                                    label="building land plinth",
                                    flat_bottom=0.0, shore=shore)))
    else:
        with Stage("mesh terrain", cfg.verbose):
            raw.append(("terrain", build_terrain(cfg, proj, terr, S, water_p)))
        with Stage("mesh water", cfg.verbose):
            # FLAT plate: bottom on the carved basin floor, top at sea level.
            # Never follows the land, so it can never stand above it.
            raw.append(("water", None if not cfg.want_water else
                        build_flat(cfg, proj, S, water_p,
                                   cfg.base_mm - cfg.water_depth_mm,
                                   cfg.base_mm + cfg.sea_level_mm)))
    with Stage("mesh greenery", cfg.verbose):
        raw.append(("greenery", None if not cfg.want_greenery else
                    build_drape(cfg, proj, terr, S, green_p,
                                cfg.greenery_mm, cfg.embed_mm,
                                label="draping greenery")))
    with Stage("mesh roads", cfg.verbose):
        raw.append(("roads", None if not cfg.want_roads else
                    build_drape(cfg, proj, terr, S, road_p,
                                cfg.roads_mm, cfg.embed_mm,
                                label="draping roads")))
    with Stage("mesh buildings", cfg.verbose):
        raw.append(("buildings", None if not cfg.want_buildings else
                    build_buildings(cfg, proj, terr, S, buildings, parts, bbox_poly)))
    if cfg.mode == "route":
        with Stage("mesh route", cfg.verbose):
            raw.append(("route", build_route(cfg, proj, terr, S, bbox_poly)))
    # The tile outline in mm, centred on the origin: what the frame and cover
    # offset. A square tile_poly gives box(-W/2,-H/2,W/2,H/2), so the frame
    # and cover come out identical to the old W/H rectangle maths.
    shape_mm = scale_poly(bbox_poly, proj, S)
    if cfg.frame:
        with Stage("mesh frame", cfg.verbose):
            walls_vf, floor_vf = build_frame(cfg, shape_mm)
            raw.append(("frame", walls_vf))
            if cfg.water_in_frame and floor_vf is not None:
                raw.append(("water", floor_vf))
    if cfg.box:
        with Stage("mesh shipping box", cfg.verbose):
            # size the lid from what was actually built, not a guess
            top = 0.0
            for nm, vf in raw:
                if nm in ("frame", "water") or vf is None:
                    continue
                V = vf[0]
                if len(V):
                    top = max(top, float(np.max(V[:, 2])))
            left_vf, right_vf = build_box(cfg, shape_mm, top)
            raw.append(("cover_left", left_vf))
            raw.append(("cover_right", right_vf))

    with Stage("repair + check", cfg.verbose):
        layers = [(n, finalize(vf, n)) for n, vf in raw]

    with Stage("write 3mf", cfg.verbose):
        if cfg.split:
            # One AMS = 4 filaments per plate. In the city that is exactly
            # terrain + roads + greenery + buildings; in terrain mode it is
            # elevation bands 1-4; in route mode it is terrain + route. A 5th
            # band spills to its own plate. Water and frame share a plate; the
            # cover is on its own.
            import os as _os
            stem, ext = _os.path.splitext(out_path)
            ext = ext or ".3mf"
            model_order = ["terrain", "terrain_2", "terrain_3", "terrain_4",
                           "terrain_5", "route", "roads", "greenery", "buildings"]
            have = {n for n, vf in layers if vf is not None}
            present = [n for n in model_order if n in have]
            main, spill = present[:4], present[4:]
            if cfg.water_in_frame:
                groups = [("", main), ("_frame", ["frame", "water"])]
            else:
                groups = [("", main), ("_water", ["water"]), ("_frame", ["frame"])]
            for i, nm in enumerate(spill, start=2):
                groups.append((f"_plate{i}", [nm]))
            groups.append(("_cover", ["cover_left", "cover_right"]))
            d = _os.path.dirname(_os.path.abspath(out_path))
            if d:
                _os.makedirs(d, exist_ok=True)
            written = []
            plate_size = {}
            for suffix, names in groups:
                sub = [(n, vf) for n, vf in layers if n in names and vf is not None]
                if not sub:
                    continue
                path = f"{stem}{suffix}{ext}"
                write_3mf(path, sub, cfg)
                written.append((path, [n for n, _ in sub]))
                allv = np.vstack([vf[0] for _, vf in sub if vf is not None
                                  and len(vf[0])])
                plate_size[path] = (float(allv[:, 0].max() - allv[:, 0].min()),
                                    float(allv[:, 1].max() - allv[:, 1].min()),
                                    float(allv[:, 2].max() - allv[:, 2].min()))
            print("", file=sys.stderr)
            BV = cfg.build_volume_mm
            over = []
            for path, names in written:
                dims = plate_size.get(path)
                print(f"  {path}", file=sys.stderr)
                print(f"      {len(names)} filament(s): {', '.join(names)}",
                      file=sys.stderr)
                if dims:
                    w, d, hgt = dims
                    fits = max(w, d) <= BV and hgt <= BV
                    print(f"      {w:.1f} x {d:.1f} x {hgt:.1f} mm"
                          + ("" if fits else "   DOES NOT FIT"), file=sys.stderr)
                    if not fits:
                        over.append((path, w, d, hgt))
            if over:
                print("", file=sys.stderr)
                print(f"  [warn] {len(over)} plate(s) exceed the {BV:.0f} mm build "
                      f"volume and cannot be printed:", file=sys.stderr)
                for path, w, d, hgt in over:
                    axis = "height" if hgt > BV else "footprint"
                    print(f"         {os.path.basename(path)}  {w:.0f} x {d:.0f} "
                          f"x {hgt:.0f} mm  ({axis})", file=sys.stderr)
                print("         Reduce the print size, or lower the building "
                      "stretch.", file=sys.stderr)
            print("\n  AMS slots, per plate. Bambu assigns by object order, so"
                  "\n  load the slots in exactly this order:", file=sys.stderr)
            for path, names in written:
                print(f"    {os.path.basename(path)}", file=sys.stderr)
                fils = [filament_of(cfg, n) for n in names]
                assign, distinct = slot_map(fils)
                for i, fil in enumerate(distinct, start=1):
                    parts = [n for n, a in zip(names, assign) if a == i]
                    print(f"      slot {i}  {fil[3]}  {fil[0]:<24} "
                          f"({', '.join(parts)})", file=sys.stderr)

            if cfg.water_in_frame:
                print("\n  Print the land model with your 4 filaments, and the"
                      "\n  frame+water plate with 2. Glue the land on top of the"
                      "\n  water so it shows through the cut-outs.",
                      file=sys.stderr)
            else:
                print("\n  Print the first file with your 4 filaments loaded,"
                      "\n  then water and frame as single-colour plates.",
                      file=sys.stderr)
        else:
            d = os.path.dirname(os.path.abspath(out_path))
            if d:
                os.makedirs(d, exist_ok=True)
            write_3mf(out_path, layers, cfg)

    if cfg.verbose:
        print_funnel()
    for name, vf in layers:
        if vf is None:
            print(f"  {name:<10s} EMPTY", file=sys.stderr)
        else:
            print(f"  {name:<10s} {len(vf[0]):>7d} verts  {len(vf[1]):>7d} tris",
                  file=sys.stderr)
    if cfg.frame:
        c, fw, d = cfg.frame_clearance_mm, cfg.frame_width_mm, cfg.frame_depth_mm
        fo = _buf(shape_mm, c + fw).bounds
        print(f"\n  frame ({cfg.tile_shape}): outer {fo[2]-fo[0]:.1f} x "
              f"{fo[3]-fo[1]:.1f} mm, "
              f"cavity {d:.1f} mm deep, {c:.2f} mm clearance per side",
              file=sys.stderr)
        if not cfg.frame_inplace and not cfg.split:
            print("  (frame is parked beside the model so it prints flat; "
                  "use --frame-inplace to nest it for preview)", file=sys.stderr)
    if cfg.verbose:
        print_timings()
    print(f"wrote {out_path}", file=sys.stderr)


def load_landmarks(path):
    if not path:
        return ()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rules = data if isinstance(data, list) else data.get("landmarks", [])
        print(f"[info] loaded {len(rules)} landmark rule(s) from {path}",
              file=sys.stderr)
        return tuple(rules)
    except Exception as e:
        print(f"[warn] could not read {path}: {e}", file=sys.stderr)
        return ()


def _list_filaments():
    """
    Printed before parse_args, because --bbox/--center is a required mutually
    exclusive group and argparse would reject a bare --list-filaments.
    """
    w = max(len(k) for k in FILAMENTS)
    print("stocked filaments (use with --filaments layer=key):\n")
    for key, (nm, typ, fid, hexc) in FILAMENTS.items():
        dflt = [L for L, k in DEFAULT_FILAMENTS.items() if k == key]
        print(f"  {key:<{w}}  {hexc}  {nm:<24} {typ:<4} {fid}"
              + (f"   default for {', '.join(dflt)}" if dflt else ""))
    print("\nlayers:", ", ".join(sorted(DEFAULT_FILAMENTS)))
    print("        plus terrain_2..terrain_5 for elevation bands (--mode terrain);"
          f"\n        those default down the ramp {' -> '.join(TERRAIN_RAMP)}")


def main():
    if "--list-filaments" in sys.argv:
        _list_filaments()
        return

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--bbox", nargs=4, type=float,
                   metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    g.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"))
    ap.add_argument("--radius", type=float, default=1000.0,
                    help="half-width in metres when using --center")
    ap.add_argument("-o", "--out", default="city.3mf")
    ap.add_argument("--size", type=float, default=150.0, help="print size in mm")
    ap.add_argument("--mode", default="city", choices=["city", "terrain", "route"],
                    help="city (default) = buildings, roads, greenery. terrain "
                         "= relief and water only, optionally cut into "
                         "elevation colour bands (--terrain-bands). route = a "
                         "raised ribbon along a GPS track, draped on the "
                         "relief (--route-name / --route-file / --route).")
    ap.add_argument("--terrain-bands", type=int, default=1,
                    help="--mode terrain: cut the relief into this many stepped "
                         "elevation bands, each its own filament (1 = smooth "
                         "single colour; max 5, the 5th on its own plate)")
    ap.add_argument("--terrain-relief-mm", type=float, default=0.0,
                    help="--mode terrain/route: scale the relief so the tallest "
                         "point is this many mm (0 = true scale)")
    ap.add_argument("--route", default="",
                    help="--mode route: the path as 'lat,lon;lat,lon;...' in "
                         "order (what the console writes for a drawn or "
                         "uploaded track)")
    ap.add_argument("--route-name", default="",
                    help="--mode route: an OSM feature name to resolve to a "
                         "path instead, e.g. 'Circuit de Spa-Francorchamps' - "
                         "keeps the command short, geometry fetched at build")
    ap.add_argument("--route-file", default="",
                    help="--mode route: a .gpx track, or a text file of "
                         "'lat,lon' lines")
    ap.add_argument("--route-width-m", type=float, default=12.0,
                    help="--mode route: ribbon width on the ground (default 12)")
    ap.add_argument("--route-height-mm", type=float, default=1.6,
                    help="--mode route: how far the ribbon stands proud of the "
                         "land (default 1.6)")
    ap.add_argument("--tile-shape", default="square",
                    choices=["square", "hex", "circle"],
                    help="outline the tile is cut to (default square). hex is "
                         "flat-top; circle inscribes the square drag. The frame "
                         "and cover follow the shape.")
    ap.add_argument("--zexag", type=float, default=1.0)
    ap.add_argument("--no-terrain", action="store_true")
    ap.add_argument("--no-water", action="store_true", help="omit the water layer")
    ap.add_argument("--no-greenery", action="store_true",
                    help="omit parks, forest and grass")
    ap.add_argument("--no-roads", action="store_true", help="omit the road layer")
    ap.add_argument("--no-buildings", action="store_true",
                    help="omit buildings (terrain and roads only)")
    ap.add_argument("--building-scale", type=float, default=1.0,
                    help="stretch building heights only, leaving terrain "
                         "relief alone (1.0 = true; 1.5 = 50%% taller)")
    ap.add_argument("--no-parts", action="store_true",
                    help="disable building_part support (reproduces the cylinder bug)")
    ap.add_argument("--no-roofs", action="store_true",
                    help="shorthand for --roofs none")
    ap.add_argument("--roofs", default="symmetric",
                    choices=("none", "symmetric", "safe", "all"),
                    help="'symmetric' (default): only domes, cones, pyramids "
                         "and onions - these come from the footprint alone and "
                         "cannot face the wrong way. 'all' also enables gabled/"
                         "hipped/skillion, which pick a ridge axis from the "
                         "footprint and flip 90 degrees on near-square "
                         "buildings. 'none' gives flat tops everywhere.")
    ap.add_argument("--release", default="")
    ap.add_argument("--max-building-mm", type=float, default=0.0)
    ap.add_argument("--min-feature", type=float, default=0.9,
                    help="thinnest printable feature in mm (default 0.9; "
                         "lower it if spires look fat, raise it if they snap)")
    ap.add_argument("--landmarks", default="",
                    help="JSON file of per-building taper overrides; this is "
                         "the safe way to shape one landmark without touching "
                         "the rest of the city")
    ap.add_argument("--tower-batter", type=float, default=0.0,
                    help="GLOBAL taper for slender parts. Off by default - it "
                         "cannot tell a landmark from an office tower. Use "
                         "--landmarks instead unless you want a stylised city.")
    ap.add_argument("--batter-aspect", type=float, default=4.0,
                    help="with --tower-batter, only taper parts this tall vs. wide")
    ap.add_argument("--taper-everything", action="store_true",
                    help="let --tower-batter reach buildings with no part data")
    ap.add_argument("--max-roof-frac", type=float, default=0.95,
                    help="how much of a part's height a POINTED roof (cone, "
                         "pyramidal, dome) may fill (default 0.95)")
    ap.add_argument("--max-ridge-frac", type=float, default=0.98,
                    help="same, for RIDGE roofs (gabled, hipped, skillion). "
                         "1.0 makes the whole building a wedge; the console "
                         "slider tops out at 0.98 (default 0.98)")
    ap.add_argument("--taper-power", type=float, default=0.7,
                    help="<1 narrows fast near the base, like a real tower")
    ap.add_argument("--source", default="overture", choices=("overture", "osm"),
                    help="'overture' (default, richer, has building parts) or "
                         "'osm' (plain OpenStreetMap via Overpass: outlines "
                         "only, no building parts, so the CN Tower is a single "
                         "cylinder and nothing can be mis-oriented)")
    ap.add_argument("--overpass-url",
                    default="https://overpass-api.de/api/interpreter")
    ap.add_argument("--lod", type=int, default=2, choices=(1, 2),
                    help="1 = one flat-topped prism per building outline "
                         "(CityGML LOD1: no parts, no roofs, zero artifacts). "
                         "2 = building parts + roof shapes (default).")
    ap.add_argument("--spire-taper", action="store_true",
                    help="synthesise a needle on the topmost slender part of "
                         "EVERY parts-bearing building. Off by default: it "
                         "spikes ordinary towers as readily as landmarks.")
    ap.add_argument("--spire-tip", type=float, default=0.30,
                    help="needle tip width relative to the mast (default 0.30)")
    ap.add_argument("--water-depth", type=float, default=1.2,
                    help="how deep the water basin is cut into the land (mm)")
    ap.add_argument("--shore-ramp-mm", type=float, default=3.0,
                    help="beach: the land plinth ramps down to the water datum "
                         "over this much print width at the shore instead of a "
                         "vertical wall. A real cliff keeps a falloff at least "
                         "this wide so no shore edge prints paper-thin. "
                         "0 = hard vertical cut.")
    ap.add_argument("--floating-parts", default="ground",
                    choices=("ground", "drop", "keep"),
                    help="what to do with a building part that has nothing "
                         "underneath it. 'ground' (default) lowers it onto "
                         "whatever is below; 'drop' discards it; 'keep' emits "
                         "it hanging in mid-air, as the data says. Real "
                         "overhangs are never touched - they overlap "
                         "something below.")
    ap.add_argument("--floating-aspect", type=float, default=12.0,
                    help="a grounded part taller than this many times its own "
                         "width is dropped instead of drawn as a needle")
    ap.add_argument("--fetch-workers", type=int, default=6,
                    help="Overture layers to scan at once (1 = sequential)")
    ap.add_argument("--terrain-zoom", type=int, default=0,
                    help="elevation tile zoom; 0 (default) picks one from the "
                         "tile size so small tiles stop looking faceted")
    ap.add_argument("--terrain-grid", type=int, default=400,
                    help="heightmap samples along the long axis")
    ap.add_argument("--filaments", default="",
                    help="per-layer filament, e.g. "
                         "'terrain=matte_grass_green,buildings=basic_gray'. "
                         "Layers: " + ", ".join(sorted(DEFAULT_FILAMENTS)) +
                         ". Run --list-filaments for the colours.")
    ap.add_argument("--list-filaments", action="store_true",
                    help="print the stocked filaments and exit")
    ap.add_argument("--no-bambu-project", action="store_true",
                    help="write a plain 3MF only. By default each plate also "
                         "carries Bambu Studio project metadata so it opens "
                         "with the right filament in each AMS slot.")
    ap.add_argument("--printer", default="Bambu Lab P1S",
                    help="printer preset name for the Bambu project metadata")
    ap.add_argument("--nozzle", type=float, default=0.4,
                    help="nozzle diameter for the Bambu project metadata")
    ap.add_argument("--sea-level", type=float, default=0.0,
                    help="water surface relative to the lowest land (mm); "
                         "negative sits it lower")
    ap.add_argument("--duckdb-memory", default="6GB")
    ap.add_argument("--duckdb-threads", type=int, default=0)

    fr = ap.add_argument_group("frame + water insert")
    fr.add_argument("--frame", action="store_true",
                    help="also emit a tray the model drops into")
    fr.add_argument("--frame-width", type=float, default=5.0,
                    help="border wall thickness in mm (default 5)")
    fr.add_argument("--frame-depth", type=float, default=5.0,
                    help="how deep the model sits into the frame in mm "
                         "(default 5; raise it for shipping)")
    fr.add_argument("--frame-floor", type=float, default=2.0,
                    help="tray floor thickness in mm (default 2)")
    fr.add_argument("--frame-open", action="store_true",
                    help="cut the floor out, leaving only a ledge")
    fr.add_argument("--frame-lip", type=float, default=4.0,
                    help="ledge width in mm when --frame-open (default 4)")
    fr.add_argument("--frame-clearance", type=float, default=0.30,
                    help="fit tolerance per side in mm (default 0.30)")
    fr.add_argument("--box", action="store_true",
                    help="also build a two-piece sliding cover. The halves "
                         "slide on from opposite ends and meet in the middle; a "
                         "groove on all four sides captures the frame rim so "
                         "nothing shifts. Separate plate under --split.")
    fr.add_argument("--cover-lip", type=float, default=6.0,
                    help="how far the lips reach under the frame (default 6.0)")
    fr.add_argument("--box-wall", type=float, default=3.0,
                    help="cover wall thickness (default 3.0)")
    fr.add_argument("--box-clearance", type=float, default=0.4,
                    help="slip fit between the box halves (default 0.4)")
    fr.add_argument("--box-headroom", type=float, default=10.0,
                    help="clear air above the tallest building (default 10.0)")
    fr.add_argument("--no-box-cube", action="store_true",
                    help="do not grow the cover up to a cube; leave it just "
                         "tall enough to clear the model plus headroom")
    fr.add_argument("--water-in-frame", action="store_true",
                    help="print the water as the frame's floor (2 filaments in "
                         "one plate) and cut the water clean out of the land "
                         "model, so the land glues on top and the water shows "
                         "through. Leaves the land model at exactly 4 filaments.")
    fr.add_argument("--split", action="store_true",
                    help="write three files instead of one: the 4-filament "
                         "land model (terrain+roads+greenery+buildings), the "
                         "water insert, and the frame. Use this if your printer "
                         "has one AMS - water and frame would otherwise be "
                         "filaments 5 and 6 on the same plate.")
    fr.add_argument("--frame-inplace", action="store_true",
                    help="nest the frame around the model for preview instead "
                         "of parking it beside for printing")
    a = ap.parse_args()

    if a.list_filaments:          # unreachable via argparse (see _list_filaments)
        w = max(len(k) for k in FILAMENTS)
        for key, (nm, typ, fid, hexc) in FILAMENTS.items():
            dflt = [L for L, k in DEFAULT_FILAMENTS.items() if k == key]
            print(f"  {key:<{w}}  {hexc}  {nm:<24} {typ:<4} {fid}"
                  + (f"   default for {', '.join(dflt)}" if dflt else ""))
        return

    route_pts = None
    if a.mode == "route" and not a.bbox and not a.center:
        # No tile given: fit the square to the path. The console does this for
        # the buyer; a hand-run --route-file / --route-name needs it too.
        if a.route_name:
            route_pts = fetch_route_osm(a.route_name, a.overpass_url, a.osm_timeout)
        elif a.route_file:
            route_pts = read_route_file(a.route_file)
        elif a.route:
            route_pts = parse_route(a.route)
        else:
            ap.error("--mode route needs --route-name, --route-file or --route "
                     "(or pass --bbox / --center yourself)")
        lons = [p[0] for p in route_pts]
        lats = [p[1] for p in route_pts]
        cx, cy = (min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0
        half = max((max(lons) - min(lons)) * 111320.0 * math.cos(math.radians(cy)),
                   (max(lats) - min(lats)) * 111320.0) / 2.0 * 1.15
        half = max(half, 200.0)
        dlat = half / 111320.0
        dlon = half / (111320.0 * math.cos(math.radians(cy)))
        bbox = (cx - dlon, cy - dlat, cx + dlon, cy + dlat)
    elif a.bbox:
        bbox = tuple(a.bbox)
    elif a.center:
        lat, lon = a.center
        dlat = a.radius / 111320.0
        dlon = a.radius / (111320.0 * math.cos(math.radians(lat)))
        bbox = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
    else:
        ap.error("one of the arguments --bbox --center is required")

    cfg = Config(bbox=bbox, size_mm=a.size, tile_shape=a.tile_shape,
                 mode=a.mode, terrain_bands=a.terrain_bands,
                 terrain_relief_mm=a.terrain_relief_mm,
                 route=a.route, route_name=a.route_name, route_file=a.route_file,
                 route_width_m=a.route_width_m, route_height_mm=a.route_height_mm,
                 z_exaggeration=a.zexag,
                 terrain=not a.no_terrain, use_building_parts=not a.no_parts,
                 roof_shapes=not a.no_roofs,
                 roof_mode=("none" if a.no_roofs else a.roofs), overture_release=a.release,
                 max_building_mm=a.max_building_mm,
                 min_feature_mm=a.min_feature,
                 tower_batter=a.tower_batter,
                 taper_landmarks_only=not a.taper_everything,
                 roof_height_max_frac=a.max_roof_frac,
                 ridge_roof_max_frac=a.max_ridge_frac,
                 landmarks=load_landmarks(a.landmarks),
                 batter_aspect=a.batter_aspect,
                 taper_power=a.taper_power,
                 spire_taper=a.spire_taper, lod=a.lod,
                 source=a.source, overpass_url=a.overpass_url,
                 spire_tip_ratio=a.spire_tip,
                 water_depth_mm=a.water_depth, sea_level_mm=a.sea_level,
                 shore_ramp_mm=a.shore_ramp_mm,
                 duckdb_memory=a.duckdb_memory, duckdb_threads=a.duckdb_threads,
                 frame=a.frame, frame_width_mm=a.frame_width,
                 frame_depth_mm=a.frame_depth, frame_floor_mm=a.frame_floor,
                 frame_open=a.frame_open, frame_lip_mm=a.frame_lip,
                 frame_clearance_mm=a.frame_clearance,
                 frame_inplace=a.frame_inplace or a.split,
                 split=a.split,
                 want_water=not a.no_water,
                 want_greenery=not a.no_greenery,
                 want_roads=not a.no_roads,
                 want_buildings=not a.no_buildings,
                 building_scale=a.building_scale,
                 water_in_frame=a.water_in_frame,
                 box=a.box, box_wall_mm=a.box_wall,
                 cover_lip_mm=a.cover_lip,
                 box_clearance_mm=a.box_clearance,
                 box_headroom_mm=a.box_headroom,
                 box_cube=not a.no_box_cube,
                 filaments=parse_filaments(a.filaments),
                 bambu_project=not a.no_bambu_project,
                 printer_model=a.printer, nozzle_mm=a.nozzle,
                 floating_parts=a.floating_parts,
                 floating_max_aspect=a.floating_aspect,
                 fetch_workers=a.fetch_workers,
                 terrain_zoom=a.terrain_zoom, terrain_grid=a.terrain_grid)
    if route_pts is not None:
        cfg._route_pts = route_pts        # already resolved to fit the bbox
    run(cfg, a.out)


if __name__ == "__main__":
    main()
