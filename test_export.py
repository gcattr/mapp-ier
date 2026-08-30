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
