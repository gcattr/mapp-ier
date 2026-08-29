#!/usr/bin/env python3
"""
ECM Model 64 - local preview server.

Serves the console AND runs the real exporter behind it, so the 3D preview is
the actual model rather than an approximation of it. The browser gets the same
geometry that ends up in the 3MF, because it IS the 3MF.

    python serve.py
    -> http://localhost:8000/map2model-console.html

Everything is stdlib apart from what map2model.py already needs.
"""
import http.server
import io
import json
import mimetypes
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import map2model
except Exception as exc:                                    # pragma: no cover
    print("Could not import map2model.py - keep it beside serve.py")
    print("  ", exc)
    raise SystemExit(1)

PORT = int(os.environ.get("PORT", "8000"))
JOBS = {}
JOBS_LOCK = threading.Lock()
KEEP_JOBS = 6


# ----------------------------------------------------------------- job plumbing
class LineSink(io.TextIOBase):
    """Collects the exporter's progress output line by line for the browser."""

    def __init__(self, job):
        self.job = job
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                with JOBS_LOCK:
                    self.job["lines"].append(line)
                    del self.job["lines"][:-400]
        return len(s)


def cfg_from(q):
    """Build a Config from the console's query string."""
    def num(k, d):
        try:
            return float(q.get(k, [d])[0])
        except (TypeError, ValueError):
            return d

    def flag(k, d=False):
        v = q.get(k, [None])[0]
        return d if v is None else v not in ("0", "false", "no", "")

    bbox = [float(v) for v in q.get("bbox", ["0,0,0,0"])[0].split(",")]
    if len(bbox) != 4:
        raise ValueError("bbox needs four numbers: west,south,east,north")

    kw = dict(
        bbox=tuple(bbox),
        size_mm=num("size", 200.0),
        z_exaggeration=num("zexag", 1.0),
        building_scale=num("building_scale", 1.0),
        terrain=flag("terrain", True),
        want_water=flag("water", True),
        want_greenery=flag("greenery", True),
        want_roads=flag("roads", True),
        want_buildings=flag("buildings", True),
        roof_mode=q.get("roofs", ["all"])[0],
        ridge_roof_max_frac=num("ridge", 0.98),
        lod=int(num("lod", 2)),
        source=q.get("source", ["overture"])[0],
        frame=True,
        water_in_frame=True,
        # the preview nests the frame around the model; the command the
        # customer copies leaves it parked beside, which is how it prints
        frame_inplace=True,
        frame_width_mm=num("frame_width", 6.0),
        frame_depth_mm=num("frame_depth", 7.0),
        frame_clearance_mm=num("frame_clearance", 0.3),
        frame_floor_mm=num("frame_floor", 2.0),
        verbose=True,
    )
    lm = q.get("landmarks", [""])[0]
    if lm:
        path = os.path.join(HERE, os.path.basename(lm))
        if os.path.exists(path):
            with open(path) as fh:
                kw["landmarks"] = tuple(json.load(fh))
    return map2model.Config(**kw)


def mesh_from_3mf(path):
    """Pull every object out of the 3MF as plain vertex/triangle arrays."""
    out = []
    with zipfile.ZipFile(path) as z:
        xml = z.read("3D/3dmodel.model").decode("utf-8", "replace")
    for chunk in xml.split("<object ")[1:]:
        name = re.search(r'name="([^"]*)"', chunk)
        body = chunk.split("</object>")[0]
        verts = re.findall(
            r'<vertex x="([-\d.eE+]+)" y="([-\d.eE+]+)" z="([-\d.eE+]+)"', body)
        tris = re.findall(
            r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"', body)
        if not verts or not tris:
            continue
        pos = []
        for x, y, z in verts:
            pos += [float(x), float(y), float(z)]
        idx = []
        for a, b, c in tris:
            idx += [int(a), int(b), int(c)]
        xs = pos[0::3]; ys = pos[1::3]; zs = pos[2::3]
        out.append({
            "name": name.group(1) if name else "object",
            "positions": pos,
            "indices": idx,
            # the console needs these to police the build volume
            "size": [round(max(xs) - min(xs), 2),
                     round(max(ys) - min(ys), 2),
                     round(max(zs) - min(zs), 2)],
        })
    return out


def run_job(job, cfg):
    sink = LineSink(job)
    tmp = os.path.join(tempfile.gettempdir(), "ecm64_%s.3mf" % job["id"])
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            map2model.run(cfg, tmp)
        # --split writes siblings; the land plate keeps the base name.
        # _cover MUST be in this list. The console measures the cover against
        # the 250 mm build volume, and the cover is the tall part - it overflows
        # long before the model does. While this only looked for _frame and
        # _water (which water_in_frame means is never written), no cover_ layer
        # ever reached the browser, so that check silently passed on every tile.
        produced = [tmp]
        stem, ext = os.path.splitext(tmp)
        for suffix in ("_frame", "_water", "_cover"):
            p = stem + suffix + ext
            if os.path.exists(p):
                produced.append(p)
        layers = []
        for p in produced:
            layers += mesh_from_3mf(p)
        with JOBS_LOCK:
            job["layers"] = layers
            job["files"] = produced
            job["done"] = True
            job["lines"].append("preview ready: %d objects" % len(layers))
    except Exception:
        with JOBS_LOCK:
            job["error"] = traceback.format_exc(limit=4)
            job["done"] = True


def reap():
    with JOBS_LOCK:
        if len(JOBS) <= KEEP_JOBS:
            return
        for jid in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:-KEEP_JOBS]:
            for p in JOBS[jid].get("files", []):
                try:
                    os.remove(p)
                except OSError:
                    pass
            del JOBS[jid]


# ----------------------------------------------------------------------- server
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        if "/api/status" not in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/api/ping":
            return self._json({"ok": True, "version": getattr(
                map2model, "__version__", "?")})

        if u.path == "/api/start":
            try:
                cfg = cfg_from(q)
            except Exception as exc:
                return self._json({"error": str(exc)}, 400)
            jid = uuid.uuid4().hex[:10]
            job = {"id": jid, "lines": [], "done": False, "error": None,
                   "layers": None, "files": [], "started": time.time()}
            with JOBS_LOCK:
                JOBS[jid] = job
            threading.Thread(target=run_job, args=(job, cfg), daemon=True).start()
            reap()
            return self._json({"id": jid})

        if u.path == "/api/status":
            jid = q.get("id", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if not job:
                    return self._json({"error": "no such job"}, 404)
                return self._json({
                    "done": job["done"], "error": job["error"],
                    "lines": job["lines"][-40:],
                    "elapsed": round(time.time() - job["started"], 1),
                })

        if u.path == "/api/mesh":
            jid = q.get("id", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if not job:
                return self._json({"error": "no such job"}, 404)
            if job["error"]:
                return self._json({"error": job["error"]}, 500)
            if not job["done"]:
                return self._json({"error": "still building"}, 409)
            return self._json({"layers": job["layers"]})

        if u.path == "/api/file":
            jid = q.get("id", [""])[0]
            which = q.get("which", ["0"])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            try:
                path = job["files"][int(which)]
            except Exception:
                return self._json({"error": "no such file"}, 404)
            with open(path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "model/3mf")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(path))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        return super().do_GET()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    mimetypes.add_type("model/3mf", ".3mf")
    console = os.path.join(HERE, "map2model-console.html")
    print("ECM Model 64 - preview server")
    print("  exporter : map2model %s" % getattr(map2model, "__version__", "?"))
    print("  folder   : %s" % HERE)
    if not os.path.exists(console):
        print("  ! map2model-console.html not found here")
    print()
    print("  open  http://localhost:%d/map2model-console.html" % PORT)
    print("  stop  Ctrl+C")
    print()
    with Server(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
