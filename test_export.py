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


def _extruders_are_slot_order():
    z = write(LAND)
    ms = z.read("Metadata/model_settings.config").decode()
    pairs = re.findall(r'key="name" value="([^"]*)"/>\s*<metadata key="extruder" value="(\d+)"', ms)
    assert pairs == [(n, str(i)) for i, n in enumerate(LAND, 1)], pairs


check("model_settings pins object i to extruder i", _extruders_are_slot_order)


def _frame_plate_is_two_slots():
    z = write(["frame", "water"])
    ms = z.read("Metadata/model_settings.config").decode()
    pairs = re.findall(r'key="name" value="([^"]*)"/>\s*<metadata key="extruder" value="(\d+)"', ms)
    assert pairs == [("frame", "1"), ("water", "2")], pairs


check("the frame plate is frame=1, water=2", _frame_plate_is_two_slots)


def _no_required_extension():
    x = model_xml(write(LAND))
    # A slicer that understands no material extension must still load the
    # geometry rather than refuse the file.
    assert "requiredextensions" not in x, "the materials extension was marked required"


check("the materials extension is not marked required", _no_required_extension)


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
