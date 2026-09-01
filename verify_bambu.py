#!/usr/bin/env python3
"""
Prove a written plate opens in Bambu Studio with the right filaments.

This is the only check that actually answers the question "will the colours be
right on the printer". It needs Bambu Studio installed; it loads a plate
through the Bambu Studio CLI, has it re-export the project, and reads the
filaments and extruder assignments back out of what Bambu itself wrote.

    python verify_bambu.py                     # writes a synthetic plate
    python verify_bambu.py 3Dmodels/x/x.3mf    # re-checks a plate you have

Why bother: a PARTIAL project_settings.config is not merged by Bambu, it is
discarded, and the plate silently falls back to a single filament. That failure
is invisible in the file -- it looks perfect -- and only shows up in the
slicer. Hence a round-trip, and hence a control.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

CANDIDATES = [
    r"C:\Program Files\Bambu Studio\bambu-studio.exe",
    r"C:\Program Files (x86)\Bambu Studio\bambu-studio.exe",
    # macOS ships the app as "BambuStudio.app" on older builds and
    # "Bambu Studio.app" (with a space) on newer ones.
    "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
    "/Applications/Bambu Studio.app/Contents/MacOS/BambuStudio",
]

# Globs, tried in order after the exact paths miss. Covers a differently named
# macOS app bundle, a Windows install outside the default folder, and a Linux
# AppImage/package.
GLOBS = [
    "/Applications/*[Bb]ambu*[Ss]tudio*.app/Contents/MacOS/*[Ss]tudio",
    os.path.expanduser(
        "~/Applications/*[Bb]ambu*[Ss]tudio*.app/Contents/MacOS/*[Ss]tudio"),
    r"C:\Program Files\*\bambu-studio.exe",
    "/usr/bin/bambu-studio", "/usr/local/bin/bambu-studio",
]


def find_bambu():
    for c in CANDIDATES:
        if os.path.exists(c):
            return c
    for pat in GLOBS:
        hit = glob.glob(pat)
        if hit:
            return hit[0]
    # Last resort: anything on PATH (Linux packages, or a user symlink).
    return shutil.which("bambu-studio") or shutil.which("bambu_studio") \
        or shutil.which("BambuStudio")


# The three plates --split actually writes. Each has a different filament
# count, and the count is what the config has to agree with.
PLATES = {
    "land":  ("terrain", "greenery", "roads", "buildings"),
    "frame": ("frame", "water"),
    "cover": ("cover_left", "cover_right"),
}


def synthetic(path, names, picks=None):
    """A plate with the real layer names, so the slot order is the real one."""
    import numpy as np
    import map2model as M
    V = np.array([[0, 0, 0], [40, 0, 0], [0, 40, 0], [0, 0, 25]], float)
    F = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    cfg = M.Config(bbox=(0, 0, 1, 1), filaments=picks or {})
    M.write_3mf(path, [(n, (V, F)) for n in names], cfg)
    fils = [M.filament_of(cfg, n) for n in names]
    # Slots are per distinct filament: two parts in the same filament share a
    # slot rather than making Bambu build a prime tower for nothing.
    assign, distinct = M.slot_map(fils)
    return {"colours": [f[3] for f in distinct], "assign": [str(a) for a in assign]}


def expected_from(path):
    """Read a plate's own intent, to compare against what Bambu makes of it."""
    z = zipfile.ZipFile(path)
    if "Metadata/project_settings.config" not in z.namelist():
        return None
    d = json.loads(z.read("Metadata/project_settings.config"))
    ms = z.read("Metadata/model_settings.config").decode()
    assign = re.findall(r'key="extruder" value="(\d+)"', ms)
    return {"colours": d.get("filament_colour") or [],
            "assign": assign[1:] if len(assign) > 1 else assign}


def roundtrip(bambu, src):
    out = os.path.join(tempfile.gettempdir(), "m2m_verify_rt.3mf")
    if os.path.exists(out):
        os.remove(out)
    subprocess.run([bambu, src, "--export-3mf", out],
                   capture_output=True, text=True)
    if not os.path.exists(out):
        return None
    return zipfile.ZipFile(out)


def main():
    bambu = find_bambu()
    if not bambu:
        print("Bambu Studio not found - skipping (this check is optional).")
        return 0
    print("using", bambu)

    if len(sys.argv) > 1:
        # For a plate we did not write, the expectation comes from the plate
        # itself: whatever it asked for is what Bambu must read back.
        return check_one(bambu, sys.argv[1], expected_from(sys.argv[1]),
                         sys.argv[1])
    bad = 0
    # A buyer's picks, not the defaults: the defaults could be right by
    # accident if the template happened to carry them.
    picks = {"terrain": "matte_desert_tan", "buildings": "basic_red",
             "water": "matte_ice_blue"}
    for tag, names in PLATES.items():
        src = os.path.join(tempfile.gettempdir(), "m2m_verify_%s.3mf" % tag)
        expect = synthetic(src, names, picks)
        bad += check_one(bambu, src, expect, tag)
    return 1 if bad else 0


def check_one(bambu, src, expect, tag):
    print()
    print("=" * 58)
    print("plate:", tag)
    z = roundtrip(bambu, src)
    if z is None:
        print("\nFAIL  Bambu Studio refused to load the plate - no output at all.")
        print("      That is what happens when the file claims to be a Bambu")
        print("      project without the structure to back it up.")
        return 1

    d = json.loads(z.read("Metadata/project_settings.config"))
    ms = z.read("Metadata/model_settings.config").decode()
    colours = d.get("filament_colour") or []
    types = d.get("filament_type") or []
    presets = d.get("filament_settings_id") or []
    parts = re.findall(r'key="name" value="([^"]*)"/>\s*<metadata key="matrix"[^/]*/>'
                       r'(?:\s*<metadata[^/]*/>)*?\s*<metadata key="extruder" value="(\d+)"', ms)
    plates = len(re.findall(r"<plate>", ms))

    print("\n=== what Bambu Studio read back ===")
    print("  printer :", d.get("printer_settings_id"))
    print("  plates  :", plates)
    for i, c in enumerate(colours, 1):
        t = types[i - 1] if i <= len(types) else "?"
        pre = presets[i - 1] if i <= len(presets) else "?"
        print(f"  slot {i}  {c}  {t:<5} {pre}")
    for name, ext in parts:
        print(f"  part    {name:<12} -> extruder {ext}")

    bad = []
    want_n = len(expect["colours"]) if expect else 2
    if len(colours) != want_n:
        bad.append(f"{len(colours)} filament(s) survived, expected {want_n} - "
                   f"the project settings were discarded, so every part prints "
                   f"in one colour")
    if not d.get("printer_settings_id"):
        bad.append("the printer preset was lost")
    if plates != 1:
        bad.append(f"{plates} plates - the parts were split up instead of "
                   f"staying one tile")
    if expect:
        if colours != expect["colours"]:
            bad.append(f"colours are {colours}, expected {expect['colours']}")
        if [e for _, e in parts] != expect["assign"]:
            bad.append(f"extruders are {[e for _, e in parts]}, "
                       f"expected {expect['assign']}")

    print()
    if bad:
        for b in bad:
            print("FAIL  " + b)
        return 1
    print("PASS  the plate opens with the right filament in every slot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
