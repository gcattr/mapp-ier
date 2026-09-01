#!/usr/bin/env python3
"""
Offline checks for the 3MF writer and the filament table.

No network, no DuckDB, no Overture: the meshes are throwaway tetrahedra. What
is under test is everything that happens AFTER geometry -- which filament each
layer gets, what lands in the file, and whether a slicer can read it back.

    python test_export.py

Every check here is a bug that shipped. Read the failure message, not the
assertion.
"""
import io
import json
import os
import re
import sys
import tempfile
import zipfile

import numpy as np

import map2model as M

PASS, FAILS = 0, []


def check(name, fn):
    global PASS
    try:
        fn()
        PASS += 1
        print("  ok   " + name)
    except Exception as e:                                  # noqa: BLE001
        FAILS.append(name)
        print("  FAIL " + name)
        print("         " + str(e).strip().splitlines()[0])


def tetra():
    V = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], float)
    F = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    return V, F


def write(layers, cfg=None):
    cfg = cfg or M.Config(bbox=(0, 0, 1, 1))
    path = os.path.join(tempfile.gettempdir(), "m2m_test.3mf")
    M.write_3mf(path, [(n, tetra()) for n in layers], cfg)
    return zipfile.ZipFile(path)


def model_xml(z):
    return z.read("3D/3dmodel.model").decode()


# ---------------------------------------------------------------- filaments
print("\nthe filament table:")


def _hexes_are_bambus():
    # Spot-checked against the two Bambu hex-table PDFs in this folder. If one
    # of these drifts, the preview and the print stop matching the order.
    known = {"matte_grass_green": "#61C680", "matte_marine_blue": "#0078BF",
             "basic_gray": "#8E9089", "basic_black": "#000000",
             "basic_jade_white": "#FFFFFF", "matte_caramel": "#AE835B",
             "matte_nardo_gray": "#757575", "basic_bambu_green": "#00AE42"}
    for k, hexc in known.items():
        assert M.FILAMENTS[k][3] == hexc, f"{k} is {M.FILAMENTS[k][3]}, expected {hexc}"


check("hex codes still match Bambu's table", _hexes_are_bambus)


def _defaults_resolve():
    for layer, key in M.DEFAULT_FILAMENTS.items():
        assert key in M.FILAMENTS, f"default for {layer} names a filament that does not exist: {key}"


check("every default names a real filament", _defaults_resolve)


def _cover_is_petg():
    cfg = M.Config(bbox=(0, 0, 1, 1))
    # The cover ships as TWO objects but is one part. They matched no key in
    # DEFAULT_FILAMENTS and fell through to basic_gray, so the shipping shell
    # was specced in a buyer's PLA instead of PETG.
    for name in ("cover", "cover_left", "cover_right"):
        nm, typ, fid, _ = M.filament_of(cfg, name)
        assert typ == "PETG", f"{name} resolved to {typ} ({nm}), not PETG"


check("the cover is PETG, both halves of it", _cover_is_petg)


def _square(side=200.0):
    from shapely.geometry import box
    return box(-side / 2, -side / 2, side / 2, side / 2)


def _half_dims(left):
    """out_x, out_y, total_z of the assembled cover, read off one half.

    build_box lays the two halves side by side with a print gap, so a half
    spans out_x/2 in x, out_y in y, total_z in z.
    """
    V = left[0]
    return (2 * (V[:, 0].max() - V[:, 0].min()),
            V[:, 1].max() - V[:, 1].min(),
            V[:, 2].max() - V[:, 2].min())


def _cover_is_a_cube():
    # A flat model (10 mm tall) would give a stubby ~25 mm lid; box_cube grows
    # it to a cube so it still fills a cube shipping box.
    cfg = M.Config(bbox=(0, 0, 1, 1))
    assert cfg.box_cube, "box_cube should default on"
    out_x, out_y, total_z = _half_dims(M.build_box(cfg, _square(), 10.0)[0])
    cube = min(max(out_x, out_y), cfg.build_volume_mm)
    assert abs(total_z - cube) < 0.5, (out_x, out_y, total_z, cube)
    assert total_z <= cfg.build_volume_mm + 1e-6, total_z


def _cube_never_shrinks_a_tall_cover():
    # A 300 mm model already needs a cover taller than its footprint; the cube
    # rule must not pull it back down (it would clip the model).
    plain = _half_dims(M.build_box(M.Config(bbox=(0, 0, 1, 1), box_cube=False),
                                   _square(), 300.0)[0])[2]
    cubed = _half_dims(M.build_box(M.Config(bbox=(0, 0, 1, 1)),
                                   _square(), 300.0)[0])[2]
    assert abs(plain - cubed) < 0.5, (plain, cubed)


def _no_box_cube_stays_short():
    cfg = M.Config(bbox=(0, 0, 1, 1), box_cube=False)
    total_z = _half_dims(M.build_box(cfg, _square(), 10.0)[0])[2]
    assert total_z < 60, total_z          # lip + 10 + headroom + wall, no cube


check("the cover grows to a cube for a flat model", _cover_is_a_cube)
check("the cube rule never shrinks a tall cover", _cube_never_shrinks_a_tall_cover)
check("--no-box-cube leaves the cover its natural height", _no_box_cube_stays_short)


# ------------------------------------------------------------- tile shapes
print("\ntile shapes:")


class _Proj:
    """Enough of Projector for tile_poly: a square metre-space bbox."""
    def __init__(self, half=800.0):
        self.minx = self.miny = -half
        self.maxx = self.maxy = half


def _open_edges(vf):
    """Boundary edges in a (V, F) soup. 0 => every solid in it is closed."""
    F = np.asarray(vf[1])
    e = np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    e.sort(axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return int((counts == 1).sum())


def _tile_poly_shapes():
    proj = _Proj(800.0)
    sq = M.tile_poly(M.Config(bbox=(0, 0, 1, 1), tile_shape="square"), proj)
    assert abs(sq.area - 1600.0 * 1600.0) < 1.0, sq.area
    hexg = M.tile_poly(M.Config(bbox=(0, 0, 1, 1), tile_shape="hex"), proj)
    assert len(hexg.exterior.coords) - 1 == 6, len(hexg.exterior.coords) - 1
    # a flat-top hex inscribed in r=800: area = 3*sqrt(3)/2 * r^2
    import math
    assert abs(hexg.area - 3 * math.sqrt(3) / 2 * 800.0 ** 2) < 50.0, hexg.area
    circ = M.tile_poly(M.Config(bbox=(0, 0, 1, 1), tile_shape="circle"), proj)
    assert abs(circ.area - math.pi * 800.0 ** 2) / (math.pi * 800.0 ** 2) < 0.01
    # every shape clips inside the bbox
    for g in (hexg, circ):
        assert g.difference(sq).area < 1.0, "shape spills past the bbox"


def _square_shape_is_unchanged():
    # box(-W/2..W/2) through the new buffer path must give the same frame and
    # cover bounds the old W/H rectangle maths did.
    from shapely.geometry import box
    cfg = M.Config(bbox=(0, 0, 1, 1))
    W = 200.0
    c, fw = cfg.frame_clearance_mm, cfg.frame_width_mm
    walls, _ = M.build_frame(cfg, box(-W / 2, -W / 2, W / 2, W / 2))
    fx = walls[0][:, 0]
    assert abs((fx.max() - fx.min()) - (W + 2 * c + 2 * fw)) < 0.05, \
        (fx.max() - fx.min(), W + 2 * c + 2 * fw)
    ox, oy, oz = _half_dims(M.build_box(cfg, box(-W / 2, -W / 2, W / 2, W / 2), 40.0)[0])
    want = W + 2 * c + 2 * fw + 2 * cfg.box_clearance_mm + 2 * cfg.box_wall_mm
    assert abs(ox - want) < 0.05 and abs(oy - want) < 0.05, (ox, oy, want)


def _hex_and_circle_frames_are_closed():
    from shapely.geometry import Point, Polygon
    import math
    cfg = M.Config(bbox=(0, 0, 1, 1))
    hexg = Polygon([(100 * math.cos(math.radians(a)), 100 * math.sin(math.radians(a)))
                    for a in range(0, 360, 60)])
    circ = Point(0, 0).buffer(100.0, quad_segs=96)
    for name, shp in (("hex", hexg), ("circle", circ)):
        walls, _ = M.build_frame(cfg, shp)
        assert walls is not None and len(walls[1]) > 0, name + " frame empty"
        assert _open_edges(walls) == 0, name + " frame is not closed"
        left, right = M.build_box(cfg, shp, 40.0)
        for h in (left, right):
            assert h is not None and len(h[1]) > 0, name + " cover half empty"
            assert _open_edges(h) == 0, name + " cover half is not closed"


def _cover_z_bands():
    # _open_edges and bounds cannot see a collapsed or missing groove ring,
    # and the cover is filtered out of the preview - so pin the vertical
    # structure: exactly six z-planes, and wall geometry spanning every band.
    from shapely.geometry import box
    cfg = M.Config(bbox=(0, 0, 1, 1))
    left, right = M.build_box(cfg, box(-100, -100, 100, 100), 40.0)
    lip_t, clr, wall = cfg.cover_lip_thickness_mm, cfg.box_clearance_mm, cfg.box_wall_mm
    frame_tall = cfg.frame_floor_mm + cfg.frame_depth_mm
    groove_lo = lip_t
    groove_hi = lip_t + frame_tall + 2 * clr
    rib_hi = groove_hi + lip_t
    total_z = _half_dims(left)[2]                 # the cube height
    levels = [0.0, groove_lo, groove_hi, rib_hi, total_z - wall, total_z]
    for h in (left, right):
        z = h[0][:, 2]
        zu = sorted({round(float(t), 3) for t in z})
        assert len(zu) == 6, f"expected 6 z-planes, got {zu}"
        for want in levels:
            assert any(abs(u - want) < 0.05 for u in zu), \
                f"z-plane {want:.2f} missing from {zu}"
        for za, zb in zip(levels[:-1], levels[1:]):
            spans = any(z[tri].min() <= za + 0.05 and z[tri].max() >= zb - 0.05
                        for tri in h[1])
            assert spans, f"no cover geometry spans z {za:.2f}..{zb:.2f}"


check("tile_poly makes a square, a 6-gon and a circle inside the bbox", _tile_poly_shapes)
check("a square outline still frames with the same outer envelope", _square_shape_is_unchanged)
check("hex and circle frames and covers are watertight", _hex_and_circle_frames_are_closed)
check("the cover keeps its six z-planes and every band has walls", _cover_z_bands)


# ------------------------------------------------------------- terrain mode
print("\nterrain mode:")


class _Dome:
    """Synthetic Terrain: a smooth dome peaking at the bbox centre."""
    flat = False

    def __init__(self, proj, peak=300.0):
        self.cx = (proj.minx + proj.maxx) / 2
        self.cy = (proj.miny + proj.maxy) / 2
        self.r = min(proj.maxx - proj.minx, proj.maxy - proj.miny) / 2
        self.peak = peak

    def elev_xy(self, x, y):
        import numpy as _np
        d = _np.hypot(_np.asarray(x) - self.cx, _np.asarray(y) - self.cy) / self.r
        return _np.maximum(self.peak * (1.0 - d * d), 0.0)


def _band_keys_recognised():
    assert M._band_index("terrain_2") == 2
    assert M._band_index("terrain_5") == 5
    assert M._band_index("terrain") == 0          # band 1 is just "terrain"
    assert M._band_index("terrain_6") == 0        # ramp is 5 long
    assert M._band_index("terrain_x") == 0


def _band_filaments_default_down_the_ramp():
    cfg = M.Config(bbox=(0, 0, 1, 1))
    assert M.filament_of(cfg, "terrain_3")[3] == M.FILAMENTS[M.TERRAIN_RAMP[2]][3]
    cfg2 = M.Config(bbox=(0, 0, 1, 1), filaments={"terrain_3": "basic_red"})
    assert M.filament_of(cfg2, "terrain_3")[3] == "#C12E1F", "a band pick was ignored"
    M.parse_filaments("terrain_4=basic_blue")     # must not raise


def _slices_follow_the_relief():
    """The bands are colour SLICES of a smooth relief, not stepped plateaus:
    each slice is watertight, solid from the bed, its footprint shrinks and
    its lid rises going up - and its top surface DRAPES over the terrain
    instead of being one flat plateau value."""
    from shapely.geometry import box
    proj = _Proj(800.0)
    M.terr_min[0] = 0.0
    M.terr_relief_scale[0] = 1.0
    cfg = M.Config(bbox=(0, 0, 1, 1), mode="terrain", terrain_bands=4)
    S = M.Scale(xy=200.0 / 1600.0, z=200.0 / 1600.0)
    bbox_poly = box(proj.minx, proj.miny, proj.maxx, proj.maxy)
    out = M.build_terrain_bands(cfg, proj, _Dome(proj), S, bbox_poly, [])
    names = [n for n, _ in out]
    assert names[0] == "terrain" and names[1:] == ["terrain_2", "terrain_3", "terrain_4"], names
    spans, tops = [], []
    for n, vf in out:
        V = vf[0]
        assert _open_edges(vf) == 0, f"{n} slice is not closed"
        assert V[:, 2].min() <= 1e-6, f"{n} is not solid from the bed"
        spans.append((V[:, 0].max() - V[:, 0].min()) *
                     (V[:, 1].max() - V[:, 1].min()))
        tops.append(V[:, 2].max())
        # the top surface (non-bed vertices) is not one flat value - it drapes
        surf = V[V[:, 2] > 1e-6, 2]
        assert surf.max() - surf.min() > 3.0, \
            f"{n} looks like a flat plateau ({surf.max() - surf.min():.2f} mm of relief)"
    for i in range(1, len(spans)):
        assert spans[i] <= spans[i - 1] + 1.0, ("footprint grew going up", i, spans)
        assert tops[i] > tops[i - 1] - 1e-6, ("lid did not rise", i, tops)


class _Ramp:
    """Synthetic Terrain: elevation rises linearly west -> east, so a band
    threshold cuts a clean north-south line and the y-extent of every band is
    the same but for the plan inset applied to the upper slices."""
    flat = False

    def __init__(self, proj, hi=200.0):
        self.x0, self.w = proj.minx, (proj.maxx - proj.minx)
        self.hi = hi

    def elev_xy(self, x, y):
        import numpy as _np
        return _np.clip((_np.asarray(x) - self.x0) / self.w, 0.0, 1.0) * self.hi


def _slices_do_not_zfight():
    """The nested slices must not share coincident faces: upper slices are
    inset in plan (walls sit inside the slice below) and buried lids drop
    below the contour they sit at (never coplanar with the surface above)."""
    from shapely.geometry import box
    proj = _Proj(800.0)
    M.terr_min[0] = 0.0
    M.terr_relief_scale[0] = 1.0
    cfg = M.Config(bbox=(0, 0, 1, 1), mode="terrain", terrain_bands=3)
    S = M.Scale(xy=200.0 / 1600.0, z=200.0 / 1600.0)
    bbox_poly = box(proj.minx, proj.miny, proj.maxx, proj.maxy)
    out = M.build_terrain_bands(cfg, proj, _Ramp(proj, hi=200.0), S, bbox_poly, [])
    ys = {n: (vf[0][:, 1].max() - vf[0][:, 1].min()) for n, vf in out}
    # base slice spans the tile; upper slices are pulled in by ~2 x 0.3 mm
    base = ys["terrain"]
    for n in ("terrain_2", "terrain_3"):
        pull = base - ys[n]
        assert 0.3 < pull < 2.5, f"{n} inset looks wrong: pulled in {pull:.2f} mm"
    # the buried lid of slice 1 sits below its contour, not on it
    zpm = M.terr_relief_scale[0] * S.z
    edges = [0.0, 200.0 / 3, 400.0 / 3, 200.0]
    lid = out[0][1][0][:, 2].max()
    contour = cfg.base_mm + edges[1] * zpm
    assert lid < contour - 1e-3, f"slice 1 lid {lid:.3f} not dropped below contour {contour:.3f}"


class _Plateau:
    """Constant high ground: a water cut-out has a full-height wall around it
    unless the shore ramp pulls the edge down."""
    flat = False

    def __init__(self, h=150.0):
        self.h = h

    def elev_xy(self, x, y):
        import numpy as _np
        return _np.full(_np.shape(x), float(self.h))


def _shore_ramps_not_a_seawall():
    """`shore=` ramps the plinth edge down to the water datum over a beach
    instead of dropping it as a vertical wall - and a genuine cliff keeps its
    height but its face is pulled back to a printable slope, never a sheet."""
    from shapely.geometry import box
    proj = _Proj(800.0)
    M.terr_min[0] = 0.0
    M.terr_relief_scale[0] = 1.0
    S = M.Scale(xy=200.0 / 1600.0, z=200.0 / 1600.0)
    bbox_poly = box(proj.minx, proj.miny, proj.maxx, proj.maxy)
    lake = box(-200, -200, 200, 200)
    land = bbox_poly.difference(lake)
    cfg = M.Config(bbox=(0, 0, 1, 1), shore_ramp_mm=6.0)
    beach_m = cfg.shore_ramp_mm / S.xy                       # 6 mm -> 48 m
    hard = M.build_drape(cfg, proj, _Plateau(150.0), S, [land], 0.0, 0.0,
                         flat_bottom=0.0)
    soft = M.build_drape(cfg, proj, _Plateau(150.0), S, [land], 0.0, 0.0,
                         flat_bottom=0.0, shore=(lake, beach_m, cfg.base_mm))
    full = 150.0 * S.z + cfg.base_mm
    rng = full - cfg.base_mm

    def tops_and_dist(vf):
        V = vf[0]
        top = V[V[:, 2] > cfg.base_mm - 1e-6]
        mx, my = top[:, 0] / S.xy, top[:, 1] / S.xy         # back to metres
        d = np.maximum(np.maximum(np.abs(mx) - 200.0, np.abs(my) - 200.0), 0.0)
        return top[:, 2], d

    hz, _ = tops_and_dist(hard)
    sz, sd = tops_and_dist(soft)
    assert hz.min() > full - 1.0, ("hard cut should be a seawall", hz.min(), full)

    shore = sz[sd < 5.0]
    assert len(shore) and shore.max() < cfg.base_mm + 0.2 * rng, \
        ("shore not ramped down to the water", shore.max() if len(shore) else None)
    inland = sz[sd > 1.5 * beach_m]
    assert len(inland) and inland.min() > full - 1.0, \
        ("true height lost away from the water", inland.min() if len(inland) else None)
    mid = sz[(sz > cfg.base_mm + 0.1 * rng) & (sz < full - 0.05 * rng)]
    assert len(mid) >= 3, \
        ("shore is a cliff face, not a ramp: heights " + str(sorted(sz.round(1))))


def _shore_ramp_reaches_the_bands():
    """build_terrain_bands wires the ramp through: the base slice's shore is
    pulled down to the datum, not left as a wall."""
    from shapely.geometry import box
    proj = _Proj(800.0)
    M.terr_min[0] = 0.0
    M.terr_relief_scale[0] = 1.0
    S = M.Scale(xy=200.0 / 1600.0, z=200.0 / 1600.0)
    bbox_poly = box(proj.minx, proj.miny, proj.maxx, proj.maxy)
    lake = box(-160, -160, 160, 160)
    cfg = M.Config(bbox=(0, 0, 1, 1), mode="terrain", terrain_bands=3,
                   shore_ramp_mm=6.0)
    out = M.build_terrain_bands(cfg, proj, _Plateau(150.0), S, bbox_poly, [lake])
    base = dict(out)["terrain"][0]
    top = base[base[:, 2] > cfg.base_mm - 1e-6]
    mx, my = top[:, 0] / S.xy, top[:, 1] / S.xy
    d = np.maximum(np.maximum(np.abs(mx) - 160.0, np.abs(my) - 160.0), 0.0)
    shore = top[d < 5.0, 2]
    assert len(shore) and shore.min() < cfg.base_mm + 3.0, \
        ("bands shore not ramped", shore.min() if len(shore) else None)


check("elevation band keys terrain_2..5 are recognised", _band_keys_recognised)
check("bands default down the ramp and take a pick", _band_filaments_default_down_the_ramp)
check("terrain slices drape on the relief and nest, not stepped plateaus", _slices_follow_the_relief)
check("terrain slices are inset and lid-dropped so they do not Z-fight", _slices_do_not_zfight)
check("the shore ramps to a beach, not a seawall, and a cliff keeps a falloff", _shore_ramps_not_a_seawall)
check("the shore ramp reaches the elevation bands", _shore_ramp_reaches_the_bands)


# ------------------------------------------------------------- route mode
print("\nroute mode:")


def _route_parses_forgivingly():
    got = M.parse_route("50.40,5.90; 50.41,5.92 ;\n50.42,5.93;")
    assert got == [(5.90, 50.40), (5.92, 50.41), (5.93, 50.42)], got
    assert M.parse_route("") == []
    for bad in ("nope", "1,2,3", "a,b"):
        try:
            M.parse_route(bad)
        except ValueError:
            pass
        else:
            assert False, f"parse_route({bad!r}) should have raised"


def _route_file_reads_gpx_and_plain_text():
    import tempfile
    import os as _os
    gpx = ('<?xml version="1.0"?><gpx><trk><trkseg>'
           '<trkpt lat="50.40" lon="5.90"></trkpt>'
           '<trkpt lon="5.92" lat="50.41"/>'
           '</trkseg></trk></gpx>')
    d = tempfile.mkdtemp()
    gp = _os.path.join(d, "ride.gpx")
    open(gp, "w").write(gpx)
    assert M.read_route_file(gp) == [(5.90, 50.40), (5.92, 50.41)]
    tp = _os.path.join(d, "ride.txt")
    open(tp, "w").write("50.40,5.90\n50.41,5.92\n")
    assert M.read_route_file(tp) == [(5.90, 50.40), (5.92, 50.41)]


def _route_ribbon_drapes_and_is_watertight():
    from shapely.geometry import box
    proj = M.Projector((0.0, 0.0, 0.02, 0.02))          # ~2.2 km tile
    M.terr_min[0] = 0.0
    M.terr_relief_scale[0] = 1.0
    bbox_poly = box(proj.minx, proj.miny, proj.maxx, proj.maxy)
    span = proj.maxx - proj.minx
    S = M.Scale(xy=200.0 / span, z=200.0 / span)
    # a straight west-east path across the middle third of the tile
    pts = [(0.010, 0.006 + 0.0004 * i) for i in range(21)]      # (lat, lon)
    cfg = M.Config(bbox=(0.0, 0.0, 0.02, 0.02), mode="route",
                   route=";".join(f"{a},{b}" for a, b in pts))
    vf = M.build_route(cfg, proj, _Dome(proj, peak=250.0), S, bbox_poly)
    assert vf is not None and len(vf[1]) > 0, "route ribbon is empty"
    assert _open_edges(vf) == 0, "route ribbon is not watertight"
    V = vf[0]
    # stays inside the tile footprint (200 mm, centred on the origin)
    assert V[:, 0].min() > -101 and V[:, 0].max() < 101, (V[:, 0].min(), V[:, 0].max())
    # follows the dome: the top is far from flat
    assert V[:, 2].max() - V[:, 2].min() > 3.0, "ribbon draped flat"
    # a west-east ribbon is about route_width_m wide in Y
    wide = (V[:, 1].max() - V[:, 1].min()) / S.xy
    assert abs(wide - cfg.route_width_m) < cfg.route_width_m * 0.35, wide


def _route_off_the_tile_is_rejected():
    from shapely.geometry import box
    proj = M.Projector((0.0, 0.0, 0.02, 0.02))
    bbox_poly = box(proj.minx, proj.miny, proj.maxx, proj.maxy)
    S = M.Scale(xy=1.0, z=1.0)
    cfg = M.Config(bbox=(0.0, 0.0, 0.02, 0.02), mode="route",
                   route="40.0,-70.0;40.1,-70.1")
    try:
        M.build_route(cfg, proj, _Dome(proj), S, bbox_poly)
    except RuntimeError:
        return
    assert False, "a route that misses the tile should raise"


def _route_layer_takes_a_filament():
    cfg = M.Config(bbox=(0, 0, 1, 1))
    assert M.filament_of(cfg, "route")[3] == "#C12E1F", "route lost its red default"
    cfg2 = M.Config(bbox=(0, 0, 1, 1), filaments={"route": "basic_yellow"})
    assert M.filament_of(cfg2, "route")[0] == "PLA Basic Yellow", "a route pick was ignored"
    M.parse_filaments("route=basic_black")            # must not raise
    assert "route" in M.PICKABLE


check("parse_route is forgiving and rejects junk", _route_parses_forgivingly)
check("read_route_file handles a .gpx track and a plain list", _route_file_reads_gpx_and_plain_text)
check("the route ribbon drapes on the relief and is watertight", _route_ribbon_drapes_and_is_watertight)
check("a route outside the tile is rejected", _route_off_the_tile_is_rejected)
check("the route layer defaults red and takes a pick", _route_layer_takes_a_filament)


def _picks_are_honoured():
    cfg = M.Config(bbox=(0, 0, 1, 1), filaments={"buildings": "basic_red"})
    assert M.filament_of(cfg, "buildings")[3] == "#C12E1F", "a pick was ignored"
    assert M.filament_of(cfg, "terrain")[3] == "#61C680", "an unpicked layer lost its default"


check("a pick wins, and only for the layer picked", _picks_are_honoured)


def _bad_input_is_rejected():
    for spec in ("terrain=no_such_colour", "no_such_layer=basic_red", "terrain"):
        try:
            M.parse_filaments(spec)
        except ValueError:
            continue
        raise AssertionError(f"{spec!r} was accepted; a bad pick must never "
                             f"silently fall back to a default")


check("an unknown layer or colour is refused, not defaulted", _bad_input_is_rejected)


# ------------------------------------------------------- the parallel fetch
print("\nfetching six layers at once:")


class _FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def close(self):
        self.owner.closed_cursors += 1


class _FakeCon:
    """Stands in for a DuckDB connection, and counts what threads asked of it."""

    def __init__(self):
        self.cursors = 0
        self.closed_cursors = 0
        self.closed = False

    def cursor(self):
        self.cursors += 1
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def _fetch_all_parallel():
    import threading
    import time

    con = _FakeCon()
    seen, threads, lock = [], set(), threading.Lock()

    def fake_fetch(cfg, cur, release, theme, typ, cols, where=""):
        assert isinstance(cur, _FakeCursor), \
            "a worker used the shared connection; DuckDB is not thread-safe"
        with lock:
            threads.add(threading.get_ident())
            seen.append(typ)
        time.sleep(0.02)                       # long enough to overlap
        return [{"typ": typ}]

    old = (M._con, M.fetch, M.resolve_release)
    M._con = lambda cfg: con
    M.fetch = fake_fetch
    M.resolve_release = lambda cfg: "test"
    try:
        cfg = M.Config(bbox=(0, 0, 1, 1), verbose=False)
        b, parts, water, green, roads = M.fetch_all_overture(cfg)
    finally:
        M._con, M.fetch, M.resolve_release = old

    assert b and parts and water and roads, (b, parts, water, roads)
    # greenery is two Overture types concatenated, not one
    assert len(green) == 2, green
    assert sorted(seen) == sorted(["building", "building_part", "water",
                                   "land_cover", "land_use", "segment"]), seen
    assert len(threads) > 1, "the layers ran one after another, not at once"
    assert con.cursors == 6, f"{con.cursors} cursors for 6 layers"
    assert con.closed_cursors == 6, "a cursor was leaked"
    assert con.closed, "the connection was left open"


check("every layer runs at once, each on its own cursor", _fetch_all_parallel)


def _fetch_all_sequential():
    import threading

    con = _FakeCon()
    threads = set()

    def fake_fetch(cfg, cur, release, theme, typ, cols, where=""):
        threads.add(threading.get_ident())
        return []

    old = (M._con, M.fetch, M.resolve_release)
    M._con = lambda cfg: con
    M.fetch = fake_fetch
    M.resolve_release = lambda cfg: "test"
    try:
        M.fetch_all_overture(M.Config(bbox=(0, 0, 1, 1), fetch_workers=1,
                                      verbose=False))
    finally:
        M._con, M.fetch, M.resolve_release = old
    assert len(threads) == 1, "fetch_workers=1 still spawned threads"


check("--fetch-workers 1 stays on one thread", _fetch_all_sequential)


def _lod1_skips_parts():
    con = _FakeCon()
    asked = []

    def fake_fetch(cfg, cur, release, theme, typ, cols, where=""):
        asked.append(typ)
        return []

    old = (M._con, M.fetch, M.resolve_release)
    M._con = lambda cfg: con
    M.fetch = fake_fetch
    M.resolve_release = lambda cfg: "test"
    try:
        M.fetch_all_overture(M.Config(bbox=(0, 0, 1, 1), lod=1, verbose=False))
    finally:
        M._con, M.fetch, M.resolve_release = old
    assert "building_part" not in asked, "LOD1 still paid for a parts scan"


check("LOD1 does not scan for building parts", _lod1_skips_parts)


# ------------------------------------------------------------- floating parts
print("\nparts that hang in mid-air:")


def _shapes():
    from shapely.geometry import box, Point
    return box, Point


def _overhangs_survive():
    box, Point = _shapes()
    cfg = M.Config(bbox=(0, 0, 1, 1))
    # The CN Tower: the pod overlaps the shaft it sits on, and the mast
    # overlaps the pod. Every one of these is a legitimate overhang and must
    # come through untouched -- this is the case the whole rule exists to spare.
    items = [("shaft", [box(-11, -11, 11, 11)], 0.0, 340.0),
             ("pod", [Point(0, 0).buffer(24)], 330.0, 360.0),
             ("mast", [box(-2, -2, 2, 2)], 360.0, 553.0)]
    out, grounded, dropped = M.resolve_floating(cfg, items)
    assert (grounded, dropped) == (0, 0), (grounded, dropped)
    assert [o[2] for o in out] == [0.0, 330.0, 360.0], [o[2] for o in out]


check("a real overhang is never touched", _overhangs_survive)


def _aerials_are_dropped():
    box, Point = _shapes()
    cfg = M.Config(bbox=(0, 0, 1, 1))
    # 2 m wide, starting 300 m up, overlapping nothing below. Grounding it
    # would draw a 300 m column one nozzle wide, which also becomes the
    # tallest thing in the model and sets the cover height.
    items = [("base", [box(-50, -50, 50, 50)], 0.0, 115.0),
             ("aerial", [box(115, 115, 117, 117)], 300.0, 324.0)]
    out, grounded, dropped = M.resolve_floating(cfg, items)
    assert dropped == 1 and grounded == 0, (grounded, dropped)
    assert len(out) == 1 and out[0][0] == "base", out


check("a detached aerial is dropped, not drawn as a needle", _aerials_are_dropped)


def _low_floaters_are_grounded():
    box, Point = _shapes()
    cfg = M.Config(bbox=(0, 0, 1, 1))
    items = [("base", [box(-50, -50, 50, 50)], 0.0, 115.0),
             ("stub", [box(115, 115, 117, 117)], 10.0, 24.0)]
    out, grounded, dropped = M.resolve_floating(cfg, items)
    assert (grounded, dropped) == (1, 0), (grounded, dropped)
    assert out[1][2] == 0.0, "it did not come down to the ground: " + str(out[1][2])


check("a low floater sits down instead of being dropped", _low_floaters_are_grounded)


def _part_lands_on_what_is_below():
    box, Point = _shapes()
    cfg = M.Config(bbox=(0, 0, 1, 1))
    # floats at 60 but there is a 0..20 podium under it: it lands on 20, not 0
    items = [("podium", [box(-30, -30, 30, 30)], 0.0, 20.0),
             ("slab", [box(-10, -10, 10, 10)], 60.0, 75.0)]
    out, grounded, dropped = M.resolve_floating(cfg, items)
    assert grounded == 1, (grounded, dropped)
    assert out[1][2] == 20.0, "landed at " + str(out[1][2]) + ", expected 20"


check("a floater lands on what is under it, not on the ground",
      _part_lands_on_what_is_below)


def _modes():
    box, Point = _shapes()
    items = [("base", [box(-50, -50, 50, 50)], 0.0, 115.0),
             ("stub", [box(115, 115, 117, 117)], 10.0, 24.0)]
    out, g, d = M.resolve_floating(
        M.Config(bbox=(0, 0, 1, 1), floating_parts="drop"), items)
    assert (g, d) == (0, 1), (g, d)
    out, g, d = M.resolve_floating(
        M.Config(bbox=(0, 0, 1, 1), floating_parts="keep"), items)
    assert (g, d) == (0, 0) and out[1][2] == 10.0, "keep changed the data"


check("--floating-parts drop / keep do what they say", _modes)


# --------------------------------------------------------------------- terrain
def _terrain_zoom_follows_the_tile():
    small = M.Config(bbox=(139.7420, 35.6560, 139.7500, 35.6620))   # 700 m
    big = M.Config(bbox=(139.70, 35.64, 139.74, 35.67))             # ~3.5 km
    zs, zb = M.terrain_zoom_for(small), M.terrain_zoom_for(big)
    # A fixed z14 is ~8 m per sample at Tokyo, so a 700 m tile was interpolating
    # detail that was never in the data and came out faceted.
    assert zs > zb, f"small tile got z{zs}, big tile z{zb}"
    assert zs <= small.terrain_zoom_max, f"z{zs} is past what terrarium holds"
    assert zb >= 12, f"big tile fell to z{zb}"


check("terrain resolution follows the tile size", _terrain_zoom_follows_the_tile)


# ------------------------------------------------------------------ the file
print("\nwhat lands in the 3MF:")

LAND = ["terrain", "greenery", "roads", "buildings"]


def _colours_in_order():
    z = write(LAND)
    x = model_xml(z)
    bases = re.findall(r'<base name="([^"]*)" displaycolor="(#[0-9A-F]{6})', x, re.I)
    assert [b[1] for b in bases] == ["#61C680", "#FFFFFF", "#000000", "#8E9089"], bases
    # Bambu maps colour groups to AMS slots BY ORDER, so the group order and
    # the object order must not drift apart.
    groups = re.findall(r'<m:color color="(#[0-9A-F]{6})', x, re.I)
    assert groups == [b[1] for b in bases], "colorgroup and basematerials disagree"


check("basematerials and colorgroup agree, in object order", _colours_in_order)


def _pindex_points_at_the_right_base():
    z = write(LAND)
    x = model_xml(z)
    objs = re.findall(r'<object id="\d+" name="([^"]*)"[^>]*pindex="(\d+)"', x)
    assert [o[0] for o in objs] == LAND, objs
    assert [int(o[1]) for o in objs] == [0, 1, 2, 3], "pindex is not object order"


check("every object points at its own material", _pindex_points_at_the_right_base)


def _parts_are_slot_order():
    z = write(LAND)
    ms = z.read("Metadata/model_settings.config").decode()
    # one object, one part per layer -- four separate objects occupy the same
    # space and Bambu's arrange spreads them onto four plates
    assert ms.count("<object ") == 1, "expected a single object, got " + str(ms.count("<object "))
    assert ms.count("<plate>") == 1, "expected a single plate"
    pairs = re.findall(r'key="name" value="([^"]*)"/>\s*<metadata key="matrix"[^/]*/>'
                       r'\s*<metadata key="extruder" value="(\d+)"', ms)
    assert pairs == [(n, str(i)) for i, n in enumerate(LAND, 1)], pairs


check("each part gets its own extruder, in slot order", _parts_are_slot_order)


def _frame_plate_is_two_slots():
    z = write(["frame", "water"])
    ms = z.read("Metadata/model_settings.config").decode()
    pairs = re.findall(r'key="name" value="([^"]*)"/>\s*<metadata key="matrix"[^/]*/>'
                       r'\s*<metadata key="extruder" value="(\d+)"', ms)
    assert pairs == [("frame", "1"), ("water", "2")], pairs


check("the frame plate is frame=1, water=2", _frame_plate_is_two_slots)


def _materials_are_never_required():
    # A slicer that understands no MATERIAL extension must still load the
    # geometry rather than refuse the file. The production extension ("p") is
    # a different matter: a Bambu project genuinely requires it.
    x = model_xml(write(LAND))
    req = re.search(r'requiredextensions="([^"]*)"', x)
    assert not req or "m" not in req.group(1).split(),         "the materials extension was marked required: " + (req.group(1) if req else "")
    plain = model_xml(write(LAND, M.Config(bbox=(0, 0, 1, 1), bambu_project=False)))
    assert "requiredextensions" not in plain,         "the portable file demands an extension it does not need"


check("the materials extension is never required", _materials_are_never_required)


def _opt_out_works():
    cfg = M.Config(bbox=(0, 0, 1, 1), bambu_project=False)
    z = write(LAND, cfg)
    assert "Metadata/project_settings.config" not in z.namelist(), z.namelist()
    assert "Metadata/model_settings.config" not in z.namelist(), z.namelist()
    # colour still travels in the standard form
    assert "<basematerials" in model_xml(z), "opting out dropped the colours too"


check("--no-bambu-project drops the metadata, keeps the colours", _opt_out_works)


def _xml_is_wellformed():
    import xml.etree.ElementTree as ET
    z = write(LAND + ["frame", "water"])
    for entry in z.namelist():
        if entry.endswith((".model", ".config", ".xml", ".rels")):
            if entry.endswith("project_settings.config"):
                json.loads(z.read(entry))          # that one is JSON
                continue
            ET.fromstring(z.read(entry))


check("every XML and JSON part parses", _xml_is_wellformed)


def _slots_are_per_filament():
    cfg = M.Config(bbox=(0, 0, 1, 1))
    fils = [M.filament_of(cfg, n) for n in ("cover_left", "cover_right")]
    assign, distinct = M.slot_map(fils)
    # One PETG shell in two halves is ONE spool. Giving it two slots makes
    # Bambu treat the plate as multi-colour: prime tower and filament swaps for
    # a shipping shell.
    assert assign == [1, 1], assign
    assert len(distinct) == 1, distinct

    fils = [M.filament_of(cfg, n) for n in LAND]
    assign, distinct = M.slot_map(fils)
    assert assign == [1, 2, 3, 4] and len(distinct) == 4, (assign, distinct)

    # a buyer who picks one colour twice needs one spool, not two
    cfg = M.Config(bbox=(0, 0, 1, 1), filaments={"buildings": "basic_jade_white"})
    fils = [M.filament_of(cfg, n) for n in LAND]
    assign, distinct = M.slot_map(fils)
    assert assign == [1, 2, 3, 2], assign
    assert len(distinct) == 3, distinct


check("two parts in one filament share a slot", _slots_are_per_filament)


def _project_config_is_consistent():
    if M._bambu_template() is None:
        raise AssertionError("bambu_p1s_0.4.json is missing; the plate cannot "
                             "be written as a Bambu project")
    cfg = M.Config(bbox=(0, 0, 1, 1))
    fils = [M.filament_of(cfg, n) for n in LAND]
    d = M._project_settings(cfg, fils)
    n = len(M.slot_map(fils)[1])
    # THE BUG THIS EXISTS FOR: a config where some per-filament list is still
    # length 1 is internally inconsistent, and Bambu does not repair it or
    # merge it -- it discards the whole config and falls back to one filament,
    # so every part prints in the same colour. Five of the twenty per-filament
    # keys do not start with "filament", which is how they got missed.
    for k in M.PER_FILAMENT:
        # An empty list stays empty: that is how Bambu writes filament_notes,
        # and a plate carrying it round-trips fine. Only a SHORT list is the
        # inconsistency that gets the config thrown away.
        if k in d and d[k]:
            assert len(d[k]) == n, (f"{k} has {len(d[k])} entries, expected {n}"
                                    f" -- Bambu will discard the whole config")
    for k in M.PER_PRESET:
        if k in d:
            assert len(d[k]) == n + 2, f"{k} has {len(d[k])}, expected {n + 2}"
    assert d["filament_colour"] == [f[3] for f in M.slot_map(fils)[1]]
    assert d["filament_self_index"] == [str(i) for i in range(1, n + 1)]
    assert d.get("printer_settings_id"), "the printer preset went missing"


check("the project config is internally consistent", _project_config_is_consistent)


def _plate_sits_on_the_bed():
    x = model_xml(write(LAND))
    item = re.search(r'<item objectid="\d+"[^>]*transform="([^"]*)"', x)
    assert item, "no build item"
    nums = [float(v) for v in item.group(1).split()]
    dx, dy, dz = nums[9], nums[10], nums[11]
    # Geometry is built around the origin with negative Z -- that is the bed's
    # CORNER, and below the plate. The offset rides in the build item so the
    # mesh itself is untouched and serve.py still reads what it always did.
    assert dx > 0 and dy > 0, f"the plate was not centred on the bed: {dx}, {dy}"
    assert dz >= 0, f"the plate still starts below the bed: {dz}"


check("the plate is placed on the bed, not through it", _plate_sits_on_the_bed)


def _one_object_many_parts():
    x = model_xml(write(LAND))
    # four objects that occupy the same space get arranged onto four separate
    # plates; as components of one object they stay a single tile
    assert "<components>" in x, "the layers were not assembled into one object"
    assert x.count("<component ") == len(LAND), x.count("<component ")
    assert 'requiredextensions="p"' in x, "the production extension is not declared"
    assert "BambuStudio:3mfVersion" in x, "the file does not identify as a Bambu project"


check("the layers are one object with a part each", _one_object_many_parts)


def _output_routing():
    assert M.model_out_path("city.3mf") == os.path.join("3Dmodels", "city", "city.3mf")
    # an explicit directory is the caller's choice; serve.py depends on this
    assert M.model_out_path(os.path.join("out", "city.3mf")) == os.path.join("out", "city.3mf")


check("a bare name routes into 3Dmodels/<name>/", _output_routing)


print("\n" + "-" * 52)
print(f"{PASS} passed, {len(FAILS)} failed")
if FAILS:
    print("failed: " + ", ".join(FAILS))
    sys.exit(1)
