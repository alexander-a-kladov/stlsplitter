"""splitter_core.py - geometry logic for STL Splitter (no GUI dependencies).

Units are whatever the STL uses (normally millimetres).
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field, replace
from typing import Any
from xml.sax.saxutils import quoteattr

import numpy as np
import trimesh
from shapely.geometry import Point, box as shapely_box
from shapely.ops import polylabel

ENGINE = "manifold"  # robust boolean backend (pip install manifold3d)


# --------------------------------------------------------------------------- data
@dataclass
class Part:
    uid: int
    name: str
    mesh: trimesh.Trimesh
    color: tuple = (176, 184, 192)
    visible: bool = True
    # runtime / viewer-only fields
    actor: Any = field(default=None, repr=False, compare=False)
    poly: Any = field(default=None, repr=False, compare=False)
    offset: np.ndarray = field(default_factory=lambda: np.zeros(3), repr=False, compare=False)


@dataclass
class ConnectorParams:
    diameter: float = 3.0      # pin diameter
    length: float = 6.0        # how far the pin sticks out of the cut face
    tolerance: float = 0.15    # radial clearance of the hole
    extra_depth: float = 0.5   # hole is this much deeper than the pin is long
    embed: float = 1.5         # how far the pin continues inside its own part
    min_wall: float = 0.8      # keep this much material around the hole
    center_mode: str = "section"   # "section" (centre of section) or "cursor"
    all_sections: bool = False     # connectors on every section loop of the plane
    swap: bool = False             # pin on the other half
    chamfer: float = 0.8           # bevel on the pin tip (0 = none)
    key: bool = False              # flat side on pin/hole so the part fits only one way
    # --- snap-in (rotating joint) pin: shaft with a thicker head, hole with a cavity behind a narrower neck
    snap: bool = False
    head_bulge: float = 0.6        # head is this much bigger than the shaft (per side)
    head_length: float = 2.6       # length of the head (from the start of its 45-degree shoulder to the tip)
    interference: float = 0.3      # neck of the hole is this much narrower (per side) than the head


# --------------------------------------------------------------------------- helpers
def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return v / n


def load_mesh(path: str):
    """Load an STL; returns (mesh, watertight_flag)."""
    m = trimesh.load(path, force="mesh")
    m.process(validate=True)
    if not m.is_watertight:
        try:
            trimesh.repair.fill_holes(m)
            trimesh.repair.fix_normals(m)
        except Exception:
            pass
    return m, bool(m.is_watertight)


def tangent_frame(n0):
    """Two unit vectors spanning the tangent plane of a surface with normal n0."""
    n0 = unit(n0)
    ref = np.array([0.0, 0.0, 1.0])
    if abs(n0 @ ref) > 0.95:
        ref = np.array([1.0, 0.0, 0.0])
    t1 = unit(np.cross(n0, ref))
    t2 = np.cross(n0, t1)
    return t1, t2


def plane_normal(n0, spin_deg: float, tilt_deg: float = 0.0) -> np.ndarray:
    """Cut-plane normal. With tilt 0 the plane contains the surface normal n0
    (i.e. it is perpendicular to the local surface); 'spin' picks the direction
    around n0, 'tilt' leans the plane away from that."""
    t1, t2 = tangent_frame(n0)
    s, t = np.radians(spin_deg), np.radians(tilt_deg)
    tang = np.cos(s) * t1 + np.sin(s) * t2
    return unit(np.cos(t) * tang + np.sin(t) * unit(n0))


def _loop_perimeter(mesh, point, normal) -> float:
    sec = mesh.section(plane_origin=point, plane_normal=normal)
    if sec is None:
        return np.inf
    best, best_d = np.inf, np.inf
    for loop in sec.discrete:
        d = np.min(np.linalg.norm(loop - point, axis=1))
        if d < best_d:
            best_d = d
            best = float(np.sum(np.linalg.norm(np.diff(loop, axis=0), axis=1)))
    return best


def snap_spin(mesh, point, n0, coarse: float = 15.0, fine: float = 2.5) -> float:
    """Find the spin angle whose cut through `point` has the shortest outline,
    i.e. the cross-section across a limb / neck / ankle."""
    point = np.asarray(point, float)
    angles = np.arange(0.0, 180.0, coarse)
    per = [_loop_perimeter(mesh, point, plane_normal(n0, a)) for a in angles]
    a0 = angles[int(np.argmin(per))]
    cand = np.arange(a0 - coarse + fine, a0 + coarse, fine)
    per2 = [_loop_perimeter(mesh, point, plane_normal(n0, a)) for a in cand]
    return float(cand[int(np.argmin(per2))] % 180.0)


# --------------------------------------------------------------------------- cutting
def _half_space(origin, normal, size):
    box = trimesh.creation.box(extents=(size, size, size))
    box.apply_translation([0, 0, size / 2.0])          # occupies z in [0, size]
    T = trimesh.geometry.align_vectors([0, 0, 1], normal)
    T[:3, 3] = origin
    box.apply_transform(T)
    return box


def split(mesh, origin, normal):
    """Split a watertight mesh by a plane; returns (negative_side, positive_side)."""
    size = float(np.linalg.norm(mesh.extents)) * 2.0 + 10.0
    box = _half_space(origin, normal, size)
    pos = trimesh.boolean.intersection([mesh, box], engine=ENGINE)
    neg = trimesh.boolean.difference([mesh, box], engine=ENGINE)
    if len(pos.faces) == 0 or len(neg.faces) == 0:
        raise ValueError("The cut plane does not pass through this part.")
    return neg, pos


def _place(mesh, direction, center):
    """Move a z-axis mesh so its axis points along `direction` and its origin sits at `center`."""
    T = trimesh.geometry.align_vectors([0, 0, 1], direction)
    T[:3, 3] = center
    mesh.apply_transform(T)
    return mesh


def _d_prism(flat_x, z0, z1, big=60.0):
    """Big half-disc (everything with x <= flat_x), extruded z0..z1: used to cut the flat key."""
    poly = Point(0, 0).buffer(big, 16).intersection(shapely_box(-big, -big, flat_x, big))
    m = trimesh.creation.extrude_polygon(poly, z1 - z0)
    m.apply_translation([0, 0, z0])
    return m


def _snap_dims(r, cp):
    """Shared dimensions of the snap-in pin and its hole (all in the pin's local z-axis frame,
    z = 0 at the cut face, tip at z = length)."""
    L, b = cp.length, cp.head_bulge
    hl = min(cp.head_length, L - 1.0)
    if hl < b + 0.9:
        raise ValueError(f"Pin length {L:.1f} mm is too short for a snap-in head "
                         f"(needs about {b + 1.9:.1f} mm or more, or a smaller head bulge).")
    rh = r + b                                          # head radius
    c = max(0.0, min(cp.chamfer, 0.6 * rh, hl - b - 0.4))   # lead-in bevel on the tip
    zs = L - hl                                         # where the 45-degree shoulder starts
    rn = max(rh - cp.interference, r + cp.tolerance + 0.05)  # neck radius (head must be forced past it)
    rc = rh + cp.tolerance                              # cavity radius
    zn = zs + (rn - r) - cp.tolerance                   # lip: end of the neck / start of the cavity
    D = L + cp.extra_depth                              # hole depth
    h = max(0.0, min(0.8 * rc, D - (zs + b) - 0.2, cp.extra_depth + cp.tolerance + c - 0.1))  # roof slope height
    return dict(L=L, b=b, rh=rh, c=c, zs=zs, rn=rn, rc=rc, zn=zn, D=D, h=h)


def _pin_mesh(r, cp):
    L, e = cp.length, cp.embed
    if cp.snap:
        d = _snap_dims(r, cp)
        rh, c, zs, b = d["rh"], d["c"], d["zs"], d["b"]
        prof = [(0, -e), (r, -e), (r, zs), (rh, zs + b), (rh, L - c)]
        prof.append((rh - c, L) if c > 0.05 else (rh, L))
        prof.append((0, L))
        m = trimesh.creation.revolve(np.array(prof, float), sections=64)
        if cp.key:   # snap-in + flat key: head and shaft get the same flat side
            m = trimesh.boolean.intersection([m, _d_prism(0.6 * r, -e - 1, L + 1)], engine=ENGINE)
        return m
    c = min(cp.chamfer, 0.8 * r, 0.5 * L)
    prof = [(0, -e), (r, -e), (r, L - c), (r - c, L), (0, L)] if c > 0.01 else [(0, -e), (r, -e), (r, L), (0, L)]
    m = trimesh.creation.revolve(np.array(prof, float), sections=64)
    if cp.key:
        m = trimesh.boolean.intersection([m, _d_prism(0.6 * r, -e - 1, L + 1)], engine=ENGINE)
    return m


def _hole_mesh(r, cp):
    if cp.snap:
        d = _snap_dims(r, cp)
        rn, rc, zn, D, h = d["rn"], d["rc"], d["zn"], d["D"], d["h"]
        z0 = -0.05
        lead = max(0.0, min(0.3, 0.5 * (zn - z0)))       # small funnel at the mouth
        prof = [(0, z0), (rn + lead, z0), (rn, z0 + lead), (rn, zn), (rc, zn + (rc - rn)), (rc, D - h)]
        prof.append((rc - h, D) if h > 0.05 else (rc, D))
        prof.append((0, D))
        m = trimesh.creation.revolve(np.array(prof, float), sections=64)
        if cp.key:   # matching flat in neck and cavity, with the same clearance as the round part
            m = trimesh.boolean.intersection([m, _d_prism(0.6 * r + cp.tolerance, z0 - 1, D + 1)], engine=ENGINE)
        return m
    z0, z1 = -0.05, cp.length + cp.extra_depth
    m = trimesh.creation.cylinder(radius=r + cp.tolerance, height=z1 - z0, sections=64)
    m.apply_translation([0, 0, (z0 + z1) / 2.0])
    if cp.key:
        m = trimesh.boolean.intersection([m, _d_prism(0.6 * r + cp.tolerance, z0 - 1, z1 + 1)], engine=ENGINE)
    return m


def section_polygons(src, origin, normal):
    """Closed section outlines of the plane as shapely polygons (planar coords) + transform to 3D."""
    sec = src.section(plane_origin=origin, plane_normal=normal)
    if sec is None:
        raise ValueError("The cut plane does not pass through this part.")
    planar, to3d = sec.to_planar()
    polys = list(planar.polygons_full)
    if not polys:
        raise ValueError("The section is not a closed outline - is the model watertight?")
    return polys, to3d


def nearest_polygon(polys, to3d, cursor):
    inv = np.linalg.inv(to3d)
    pt = Point((inv @ np.append(np.asarray(cursor, float), 1.0))[:2])
    return min(polys, key=lambda g: g.distance(pt)), pt


def section_loops(mesh, origin, normal):
    """3D polylines of every section outline (for drawing the preview)."""
    sec = mesh.section(plane_origin=origin, plane_normal=normal)
    return [] if sec is None else [np.asarray(l) for l in sec.discrete]


def connector_sites(src, origin, normal, cursor, cp: ConnectorParams):
    """Return [(centre_3d, max_inscribed_radius)] for the section(s) of the plane."""
    polys, to3d = section_polygons(src, origin, normal)
    near, pt = nearest_polygon(polys, to3d, cursor)
    if not cp.all_sections:
        polys = [near]
    sites = []
    for poly in polys:
        if cp.center_mode == "cursor" and not cp.all_sections and poly.contains(pt):
            c = pt
        else:
            c = polylabel(poly, tolerance=0.02)
        r = poly.boundary.distance(c)
        sites.append(((to3d @ np.array([c.x, c.y, 0.0, 1.0]))[:3], float(r)))
    return sites


def add_connectors(src, neg, pos, origin, normal, cursor, cp: ConnectorParams):
    sites = connector_sites(src, origin, normal, cursor, cp)
    if cp.swap:
        pin_part, hole_part, d = pos, neg, -normal
    else:
        pin_part, hole_part, d = neg, pos, normal
    pins, holes, notes = [], [], []
    for center, rmax in sites:
        allowed = rmax - cp.min_wall - cp.tolerance - (cp.head_bulge if cp.snap else 0.0)
        if allowed < 0.5:
            notes.append(f"section too small for a pin (inscribed radius {rmax:.1f} mm)")
            continue
        r = cp.diameter / 2.0
        if r > allowed:
            notes.append(f"pin diameter reduced {cp.diameter:.1f} -> {2 * allowed:.1f} mm to fit")
            r = allowed
        pins.append(_place(_pin_mesh(r, cp), d, center))
        holes.append(_place(_hole_mesh(r, cp), d, center))
    if not pins:
        raise ValueError("No section is large enough for a connector: " + "; ".join(notes))
    pin_part = trimesh.boolean.union([pin_part] + pins, engine=ENGINE)
    hole_part = trimesh.boolean.difference([hole_part] + holes, engine=ENGINE)
    if cp.swap:
        pos, neg = pin_part, hole_part
    else:
        neg, pos = pin_part, hole_part
    return neg, pos, f"{len(pins)} connector(s) added" + (" (" + "; ".join(notes) + ")" if notes else "")


def _components(mesh):
    parts = mesh.split(only_watertight=False)
    parts = [p for p in parts if len(p.faces) >= 4]
    return parts or [mesh]


def local_split(mesh, origin, normal, cursor, kerf=0.02, margin=0.5):
    """Cut ONLY the outline (section loop) that lies under the cursor.

    A very thin disc shaped like that outline (grown by `margin`) is subtracted from the
    mesh - like a thread pulled tight around the limb. Nothing else on the plane is touched.
    The pieces on either side are then told apart by which face of the disc they touch.
    Returns (neg_pieces, pos_pieces)."""
    polys, to3d = section_polygons(mesh, origin, normal)
    poly, _ = nearest_polygon(polys, to3d, cursor)
    grown = poly.buffer(margin)
    if grown.geom_type == "MultiPolygon":
        grown = max(grown.geoms, key=lambda g: g.area)
    cutter = trimesh.creation.extrude_polygon(grown, kerf)
    cutter.apply_translation([0, 0, -kerf / 2.0])
    cutter.apply_transform(to3d)
    rest = trimesh.boolean.difference([mesh, cutter], engine=ENGINE)
    comps = [c for c in rest.split(only_watertight=False) if len(c.faces) >= 4]
    half, tol = kerf / 2.0, kerf * 0.25
    neg, pos = [], []
    for c in comps:
        d = (np.asarray(c.vertices) - origin) @ normal
        n_neg = int(np.sum(np.abs(d + half) < tol))
        n_pos = int(np.sum(np.abs(d - half) < tol))
        if n_neg > n_pos:
            neg.append(c)
        elif n_pos > n_neg:
            pos.append(c)
        else:  # touches neither face: put it on the side its centre of mass is on
            (neg if (c.centroid - origin) @ normal < 0 else pos).append(c)
    if not neg or not pos:
        raise ValueError(
            "This outline does not separate the model into two pieces (it is probably a ring or "
            "handle, or the outline is only one of two connections). Make a second cut elsewhere on "
            "the ring, or switch the cut mode to 'Infinite plane'.")
    return neg, pos


def _cap_count(c, origin, normal, side, kerf):
    d = (np.asarray(c.vertices) - origin) @ normal
    return int(np.sum(np.abs(d - side * kerf / 2.0) < kerf * 0.25))


def cut_part(mesh, origin, normal, cursor, connector: ConnectorParams | None = None,
             mode: str = "local", kerf: float = 0.02, margin: float = 0.5):
    """Cut `mesh`. mode='local' cuts only the outline under the cursor, 'plane' cuts everything
    the infinite plane touches. Returns (neg_pieces, pos_pieces, note)."""
    origin = np.asarray(origin, float)
    normal = unit(normal)
    note = ""
    if mode == "local":
        neg, pos = local_split(mesh, origin, normal, cursor, kerf, margin)
        if connector is not None:
            cp = replace(connector, all_sections=False)
            i = max(range(len(neg)), key=lambda k: _cap_count(neg[k], origin, normal, -1, kerf))
            j = max(range(len(pos)), key=lambda k: _cap_count(pos[k], origin, normal, +1, kerf))
            neg[i], pos[j], note = add_connectors(mesh, neg[i], pos[j], origin, normal, cursor, cp)
            neg, pos = list(neg), list(pos)
        return neg, pos, note
    neg, pos = split(mesh, origin, normal)
    if connector is not None:
        neg, pos, note = add_connectors(mesh, neg, pos, origin, normal, cursor, connector)
    return _components(neg), _components(pos), note


# --------------------------------------------------------------------------- files
def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_") or "part"


def plan_collection_files(parts):
    """The STL file names save_collection() will write (unique, filesystem-safe)."""
    names, used = [], set()
    for p in parts:
        base, n = _safe(p.name), 1
        fn = f"{base}.stl"
        while fn in used:
            n += 1
            fn = f"{base}_{n}.stl"
        used.add(fn)
        names.append(fn)
    return names


def save_collection(folder: str, parts) -> int:
    """Write every part as <name>.stl plus collection.json (names + colours).
    Existing files with the same names are replaced."""
    os.makedirs(folder, exist_ok=True)
    meta = []
    for p, fn in zip(parts, plan_collection_files(parts)):
        p.mesh.export(os.path.join(folder, fn))
        meta.append({"name": p.name, "file": fn, "color": list(p.color), "visible": p.visible})
    with open(os.path.join(folder, "collection.json"), "w", encoding="utf-8") as f:
        json.dump({"parts": meta}, f, indent=2)
    return len(meta)


def load_collection(folder: str):
    """Returns [(name, mesh, color, visible)]. Falls back to plain *.stl files."""
    js = os.path.join(folder, "collection.json")
    out = []
    if os.path.exists(js):
        with open(js, encoding="utf-8") as f:
            for m in json.load(f)["parts"]:
                mesh = trimesh.load(os.path.join(folder, m["file"]), force="mesh")
                out.append((m["name"], mesh, tuple(m["color"]), m.get("visible", True)))
    else:
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(".stl"):
                mesh = trimesh.load(os.path.join(folder, fn), force="mesh")
                out.append((os.path.splitext(fn)[0], mesh, (176, 184, 192), True))
    return out


def export_3mf(path: str, parts) -> None:
    """Single 3MF with one object per part and a base colour per part,
    all kept in their assembled positions."""
    mats = "".join(
        f'<base name={quoteattr(p.name)} displaycolor="#{p.color[0]:02X}{p.color[1]:02X}{p.color[2]:02X}FF"/>'
        for p in parts)
    objs, items = [], []
    for i, p in enumerate(parts):
        oid = i + 2
        v = "".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in p.mesh.vertices)
        t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in p.mesh.faces)
        objs.append(f'<object id="{oid}" type="model" pid="1" pindex="{i}" name={quoteattr(p.name)}>'
                    f'<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><basematerials id="1">{mats}</basematerials>{"".join(objs)}</resources>'
             f'<build>{"".join(items)}</build></model>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
              '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
