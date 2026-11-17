#!/usr/bin/env python3
"""3D mesh -> hidden-line-removed line-art SVG (pure Python, no numpy).

Inspired by fogleman/ln: use the mesh triangles only for visibility testing,
and draw the triangle edges as the "vector texture". Each sample point on an
edge casts a ray toward an orthographic camera; if no triangle is hit before
the camera, the point is visible. Visible runs become SVG polylines.

Supports ASCII/binary STL and OBJ. Usage:
    python3 tools/mesh_to_svg.py model.stl --out model.svg --yaw 35 --pitch 25
"""
import argparse
import math
import pathlib
import struct

MAX_TRIANGLES = 1500
MAX_SAMPLES = 250
SVGNS = "http://www.w3.org/2000/svg"


# ------------------------------------------------------------------- parsing
def parse_obj(text):
    verts, tris = [], []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            verts.append(tuple(float(x) for x in parts[1:4]))
        elif parts[0] == "f":
            idx = []
            for p in parts[1:]:
                n = int(p.split("/")[0])
                if n < 0:
                    n = len(verts) + n
                idx.append(n - 1)
            for k in range(1, len(idx) - 1):
                tris.append((verts[idx[0]], verts[idx[k]], verts[idx[k + 1]]))
    return tris


def parse_stl(path):
    data = pathlib.Path(path).read_bytes()
    if len(data) < 84:
        raise ValueError("STL file too short")
    count = struct.unpack_from("<I", data, 80)[0]
    if len(data) >= 84 + count * 50:
        tris = []
        for i in range(count):
            v = struct.unpack_from("<12fH", data, 84 + i * 50)
            tris.append((v[3:6], v[6:9], v[9:12]))
        return tris
    # ASCII STL
    text = data.decode("latin-1")
    verts, tris = [], []
    for line in text.splitlines():
        s = line.strip().split()
        if s and s[0].lower() == "vertex" and len(s) >= 4:
            verts.append(tuple(float(x) for x in s[1:4]))
            if len(verts) == 3:
                tris.append(tuple(verts))
                verts = []
    return tris


def load_mesh(path):
    ext = pathlib.Path(path).suffix.lower()
    if ext == ".obj":
        tris = parse_obj(pathlib.Path(path).read_text(errors="replace"))
    elif ext == ".stl":
        tris = parse_stl(path)
    else:
        raise ValueError("expected .obj or .stl")
    if len(tris) < 1:
        raise ValueError("no triangles found in mesh")
    if len(tris) > MAX_TRIANGLES:
        step = len(tris) / MAX_TRIANGLES
        tris = [tris[int(i * step)] for i in range(MAX_TRIANGLES)]
    return tris


# --------------------------------------------------------------------- math
def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    n = math.sqrt(dot(a, a))
    return tuple(v / n for v in a) if n else (0.0, 0.0, 1.0)


def ray_triangle(origin, direction, tri):
    """Möller–Trumbore. -> distance t>epsilon or None."""
    e1, e2 = sub(tri[1], tri[0]), sub(tri[2], tri[0])
    p = cross(direction, e2)
    det = dot(e1, p)
    if abs(det) < 1e-12:
        return None
    inv = 1.0 / det
    tvec = sub(origin, tri[0])
    u = dot(tvec, p) * inv
    if u < 0 or u > 1:
        return None
    q = cross(tvec, e1)
    v = dot(direction, q) * inv
    if v < 0 or u + v > 1:
        return None
    t = dot(e2, q) * inv
    return t if t > 1e-6 else None


# ------------------------------------------------------------------ renderer
def render(tris, yaw=35.0, pitch=25.0, width_mm=120.0, max_height_mm=180.0,
           step_mm=1.2, max_dist=None):
    """Render edges with hidden-line removal. -> (svg string, stats)."""
    pts = [p for tri in tris for p in tri]
    centre = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
    tris = [tuple(sub(p, centre) for p in tri) for tri in tris]

    ya, pa = math.radians(yaw), math.radians(pitch)
    eye = (math.cos(pa) * math.cos(ya), math.cos(pa) * math.sin(ya), math.sin(pa))
    world_up = (0.0, 0.0, 1.0)
    right = norm(cross(eye, world_up)) if abs(dot(eye, world_up)) < 0.99 else (1.0, 0.0, 0.0)
    up = cross(right, eye)

    def proj(p):
        return (dot(p, right), -dot(p, up))

    projected = [proj(p) for p in pts]
    xs = [q[0] for q in projected]
    ys = [q[1] for q in projected]
    bw, bh = max(xs) - min(xs), max(ys) - min(ys)
    if bw <= 0 or bh <= 0:
        raise ValueError("mesh has no projected extent")
    scale = min(width_mm / bw, max_height_mm / bh)
    ox = -(min(xs) + max(xs)) / 2 * scale
    oy = -(min(ys) + max(ys)) / 2 * scale

    # Unique edges, dropping coplanar triangulation diagonals (they are not
    # real edges of the model — only silhouettes and creases should be drawn).
    def face_normal(tri):
        return norm(cross(sub(tri[1], tri[0]), sub(tri[2], tri[0])))

    normals = [face_normal(t) for t in tris]
    edge_map = {}
    for i, tri in enumerate(tris):
        for a in range(3):
            ka = tuple(round(v, 6) for v in tri[a])
            kb = tuple(round(v, 6) for v in tri[(a + 1) % 3])
            key = tuple(sorted((ka, kb)))
            edge_map.setdefault(key, []).append((i, tri[a], tri[(a + 1) % 3], ka, kb))
    edges = []
    for key, group in edge_map.items():
        if len(group) > 1 and len({j for j, *_ in group}) == 2:
            n1, n2 = normals[group[0][0]], normals[group[1][0]]
            if abs(dot(n1, n2)) > 0.995:
                continue
        _, a, b, ka, kb = group[0]
        edges.append((a, b, ka, kb))
    # map vertex key -> triangles touching it
    touching = {}
    for i, tri in enumerate(tris):
        for p in tri:
            touching.setdefault(tuple(round(v, 6) for v in p), set()).add(i)

    far = max(math.sqrt(dot(p, p)) for p in pts) * 3 + 1
    if max_dist:
        far = min(far, max_dist)
    paths, visible_samples = [], 0
    for a, b, ka, kb in edges:
        q0, q1 = proj(a), proj(b)
        length = math.dist(q0, q1) * scale
        n = max(1, min(MAX_SAMPLES, math.ceil(length / step_mm)))
        ignore = touching.get(ka, set()) | touching.get(kb, set())
        runs, current = [], []
        for k in range(n + 1):
            t = k / n
            p = tuple(a[i] + t * (b[i] - a[i]) for i in range(3))
            origin = tuple(p[i] + eye[i] * 1e-4 for i in range(3))
            hidden = False
            for j, tri in enumerate(tris):
                if j in ignore:
                    continue
                hit = ray_triangle(origin, eye, tri)
                if hit is not None and hit < far:
                    hidden = True
                    break
            if hidden:
                if len(current) >= 2:
                    runs.append(current)
                current = []
            else:
                q = proj(p)
                current.append((q[0] * scale + ox, q[1] * scale + oy))
                visible_samples += 1
        if len(current) >= 2:
            runs.append(current)
        for run in runs:
            if math.dist(run[0], run[-1]) < 0.5:
                continue
            paths.append(run)

    if not paths:
        raise ValueError("no visible edges — try different view angles")
    d = "\n".join('<path d="M ' + " L ".join(f"{x:.3f},{y:.3f}" for x, y in run) + '"/>'
                  for run in paths)
    w = bw * scale
    h = bh * scale
    svg = (f'<svg xmlns="{SVGNS}" width="{w:.3f}mm" height="{h:.3f}mm" '
           f'viewBox="{-w/2:.3f} {-h/2:.3f} {w:.3f} {h:.3f}">\n{d}\n</svg>\n')
    return svg, {"triangles": len(tris), "edges": len(edges), "paths": len(paths),
                 "points": visible_samples, "width_mm": round(w, 2), "height_mm": round(h, 2),
                 "yaw": yaw, "pitch": pitch}


def convert(src, dst, yaw=35.0, pitch=25.0, width_mm=120.0, max_height_mm=180.0):
    tris = load_mesh(src)
    svg, stats = render(tris, yaw, pitch, width_mm, max_height_mm)
    pathlib.Path(dst).write_text(svg, encoding="utf-8")
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mesh")
    ap.add_argument("--out", required=True)
    ap.add_argument("--yaw", type=float, default=35.0)
    ap.add_argument("--pitch", type=float, default=25.0)
    ap.add_argument("--width-mm", type=float, default=120.0)
    ap.add_argument("--max-height-mm", type=float, default=180.0)
    a = ap.parse_args(argv)
    try:
        s = convert(a.mesh, a.out, a.yaw, a.pitch, a.width_mm, a.max_height_mm)
    except Exception as ex:  # noqa: BLE001
        raise SystemExit(f"conversion failed: {ex}")
    print(f"Wrote {a.out}: {s['triangles']} triangles, {s['paths']} visible paths, "
          f"{s['points']} points, {s['width_mm']} x {s['height_mm']} mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
