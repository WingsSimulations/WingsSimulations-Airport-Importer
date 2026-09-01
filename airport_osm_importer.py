# ------------------------------------------------------------------------------
# Airport OSM & WSAirports Importer for Blender
#
# Copyright (C) 2026 Martin F, Wings Simulations, and Contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# DATASET ATTRIBUTIONS AND MANDATORY LEGAL NOTICE (GPLv2 & ODbL):
# 1. X-Plane Airport Scenery Gateway: (C) Laminar Research & Community Authors
#    Upstream: https://gateway.x-plane.com/ (GPLv2)
# 2. Wings Simulations Dataset: (C) 2026 Wings Simulations (GPLv2)
# 3. OpenStreetMap Buildings: (C) OpenStreetMap contributors (ODbL / CC-BY-SA)
#
# MANDATORY REQUIREMENT: Any derivative 3D scenery, renders, game mods, or files 
# produced using this dataset MUST include clear attribution to BOTH Laminar Research 
# (X-Plane Airport Scenery Gateway) and Wings Simulations under the terms of GPLv2. 
# Unattributed commercial redistribution or license violations are subject to DMCA takedown.
# ------------------------------------------------------------------------------

bl_info = {
    "name": "Airport OSM & WSAirports Importer",
    "author": "Martin F & Wings Simulations",
    "version": (2, 5, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Airport Importer",
    "description": "Generate high-accuracy base 3D models using chunked .wscairport data with OSM 3D buildings, extended markings, and island/hole polygon support",
    "category": "Import-Export",
    "license": "GPL-2.0-or-later",
}

import bpy
import bmesh
import math
import json
import gzip
import os
import webbrowser
import urllib.request
import urllib.error
from bpy.props import StringProperty, FloatProperty, BoolProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup

EARTH_RADIUS = 6378137.0
GITHUB_REPO_URL = "https://github.com/WingsSimulations/wsairports-database"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

BUILDING_DEFAULT_HEIGHT = 6.0
LEVEL_HEIGHT = 3.0

_WSA_INDEX = {}
_LOADED_DB_PATH = ""

SURFACE_COLORS = {
    "runway_asphalt": (0.04, 0.04, 0.045),
    "runway_concrete": (0.45, 0.44, 0.42),
    "runway_turf": (0.12, 0.28, 0.08),
    "runway_dirt": (0.28, 0.20, 0.12),
    "runway_gravel": (0.35, 0.33, 0.30),
    "taxiway_asphalt": (0.07, 0.07, 0.08),
    "taxiway_concrete": (0.50, 0.49, 0.47),
    "apron": (0.58, 0.57, 0.54),
    "road": (0.03, 0.03, 0.03),
    "paint_white": (0.92, 0.92, 0.92),
    "paint_yellow": (0.95, 0.75, 0.05),
    "paint_red": (0.85, 0.05, 0.05),
    "paint_orange": (0.95, 0.45, 0.05),
    "paint_blue": (0.05, 0.35, 0.85),
    "paint_green": (0.05, 0.65, 0.15),
    "paint_black": (0.02, 0.02, 0.02),
    "helipad": (0.20, 0.20, 0.22),
    "windsock": (0.95, 0.30, 0.05),
    "beacon": (0.05, 0.85, 0.20),
    "building": (0.60, 0.60, 0.62),
}


# --- Coordinate Math ---

def latlon_to_local_xy(lat, lon, origin_lat, origin_lon, scale=1.0):
    origin_lat_rad = math.radians(origin_lat)
    x = math.radians(lon - origin_lon) * EARTH_RADIUS * math.cos(origin_lat_rad) * scale
    y = math.radians(lat - origin_lat) * EARTH_RADIUS * scale
    return x, y


# --- Overpass OSM Building Fetcher ---

def run_overpass_query(query, timeout=60):
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            payload = ("data=" + urllib.request.quote(query)).encode("utf-8")
            req = urllib.request.Request(endpoint, data=payload, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("User-Agent", "Blender-Airport-WSA-OSM-Importer/2.5")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Overpass endpoints failed: {last_err}")


def fetch_osm_buildings(lat, lon, radius_m):
    query = f"""
[out:json][timeout:60];
(
  way["building"](around:{radius_m},{lat},{lon});
  relation["building"](around:{radius_m},{lat},{lon});
);
out body;
>;
out skel qt;
"""
    return run_overpass_query(query.strip())


def get_building_height(tags, scale=1.0):
    if "height" in tags:
        try:
            return float(str(tags["height"]).split()[0]) * scale
        except Exception:
            pass
    if "building:levels" in tags:
        try:
            return float(tags["building:levels"]) * LEVEL_HEIGHT * scale
        except Exception:
            pass
    return BUILDING_DEFAULT_HEIGHT * scale


# --- Dataset & Chunk Index Resolution ---

def load_master_index(db_folder):
    global _WSA_INDEX, _LOADED_DB_PATH
    db_path = bpy.path.abspath(db_folder).strip()
    if not db_path or not os.path.isdir(db_path):
        return 0

    index_path = os.path.join(db_path, "airports_index.json")
    if not os.path.exists(index_path):
        return 0

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _WSA_INDEX = {entry["icao"].upper(): entry for entry in data if "icao" in entry}
        _LOADED_DB_PATH = db_path
        return len(_WSA_INDEX)
    except Exception as e:
        print(f"[!] WSAirports Error loading airports_index.json: {e}")
        _WSA_INDEX = {}
        return 0


def fetch_airport_from_chunk(db_folder, icao):
    icao = icao.strip().upper()
    if not _WSA_INDEX:
        load_master_index(db_folder)

    entry = _WSA_INDEX.get(icao)
    if not entry:
        return None, f"Airport '{icao}' not found in master index."

    chunk_key = entry.get("chunk")
    if not chunk_key:
        return None, f"No chunk reference found for '{icao}'."

    candidate_dirs = [db_folder, os.path.join(db_folder, "chunks")]
    extensions = [".wscairport", ".chunk.json.gz", ".chunk.json", ".json", ".gz"]

    target_file = None
    for c_dir in candidate_dirs:
        for ext in extensions:
            p = os.path.join(c_dir, f"{chunk_key}{ext}")
            if os.path.exists(p):
                target_file = p
                break
        if target_file:
            break

    if not target_file:
        return None, f"Chunk file for '{chunk_key}' not found in database."

    try:
        if target_file.endswith(".gz"):
            with gzip.open(target_file, "rt", encoding="utf-8") as f:
                chunk_data = json.load(f)
        else:
            with open(target_file, "r", encoding="utf-8") as f:
                chunk_data = json.load(f)

        apt = chunk_data.get(icao)
        if not apt:
            return None, f"Airport '{icao}' missing from chunk '{chunk_key}'."
        return apt, None
    except Exception as e:
        return None, f"Error reading chunk '{target_file}': {e}"


# --- Geometry & Hierarchy Generation ---

def get_or_create_material(name, color, roughness=0.9):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (*color, 1.0)
            if "Roughness" in bsdf.inputs:
                bsdf.inputs["Roughness"].default_value = roughness
    return mat


def make_collection(name, parent=None):
    if name in bpy.data.collections:
        col = bpy.data.collections[name]
    else:
        col = bpy.data.collections.new(name)
        if parent is None:
            bpy.context.scene.collection.children.link(col)
        else:
            parent.children.link(col)
    return col


def prepare_airport_hierarchy(root_name="Airport"):
    root_col = make_collection(root_name)
    return {
        "root": root_col,
        "runways": make_collection("Runways", root_col),
        "taxiways": make_collection("Taxiways", root_col),
        "aprons": make_collection("Aprons_Pavements", root_col),
        "roads": make_collection("Service_Roads", root_col),
        "markings": make_collection("Markings", root_col),
        "helipads": make_collection("Helipads", root_col),
        "windsocks": make_collection("Windsocks", root_col),
        "beacons": make_collection("Beacons", root_col),
        "gates": make_collection("Gates_Stands", root_col),
        "jetways": make_collection("Jetways", root_col),
        "buildings": make_collection("Buildings_OSM", root_col),
    }


def build_complex_polygon(name, rings_xy, elevation, collection, material=None, extrude=0.0):
    valid_rings = []
    for ring in rings_xy:
        clean = ring[:-1] if (len(ring) > 3 and ring[0] == ring[-1]) else list(ring)
        if len(clean) >= 3:
            valid_rings.append(clean)

    if not valid_rings:
        return None

    # Use 2D Curve Tessellation to support arbitrary holes and islands
    curve_data = bpy.data.curves.new(name=name, type='CURVE')
    curve_data.dimensions = '2D'
    curve_data.fill_mode = 'BOTH'

    for ring in valid_rings:
        spline = curve_data.splines.new(type='POLY')
        spline.points.add(len(ring) - 1)
        for i, (x, y) in enumerate(ring):
            spline.points[i].co = (x, y, 0.0, 1.0)
        spline.use_cyclic_u = True

    temp_obj = bpy.data.objects.new(name + "_curve", curve_data)
    collection.objects.link(temp_obj)

    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = temp_obj.evaluated_get(depsgraph)
    mesh_from_curve = bpy.data.meshes.new_from_object(eval_obj)

    # Cleanup temporary curve object
    bpy.data.objects.remove(temp_obj, do_unlink=True)
    bpy.data.curves.remove(curve_data)

    if not mesh_from_curve or len(mesh_from_curve.polygons) == 0:
        if mesh_from_curve:
            bpy.data.meshes.remove(mesh_from_curve)
        return None

    # Handle elevation and optional extrusion
    bm = bmesh.new()
    bm.from_mesh(mesh_from_curve)
    bmesh.ops.translate(bm, vec=(0, 0, elevation), verts=bm.verts)

    if extrude > 0.0 and bm.faces:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        ret = bmesh.ops.extrude_face_region(bm, geom=list(bm.faces))
        extruded_verts = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, vec=(0, 0, extrude), verts=extruded_verts)

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    final_mesh = bpy.data.meshes.new(name)
    bm.to_mesh(final_mesh)
    bm.free()
    bpy.data.meshes.remove(mesh_from_curve)

    obj = bpy.data.objects.new(name, final_mesh)
    collection.objects.link(obj)

    if material:
        obj.data.materials.append(material)
    return obj


def build_line_strip_ribbon(name, coords_xy, elevation, width, collection, material=None, dash_pattern=None):
    if len(coords_xy) < 2:
        return None

    segments = []
    if dash_pattern:
        dash_len, gap_len = dash_pattern
        current_len = 0.0
        drawing = True
        active_seg = [coords_xy[0]]

        for i in range(len(coords_xy) - 1):
            p1, p2 = coords_xy[i], coords_xy[i + 1]
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            seg_len = math.hypot(dx, dy)
            if seg_len == 0:
                continue

            ux, uy = dx / seg_len, dy / seg_len
            consumed = 0.0

            while consumed < seg_len:
                target_len = dash_len if drawing else gap_len
                remain_target = target_len - current_len
                step = min(seg_len - consumed, remain_target)

                consumed += step
                current_len += step
                interp_pt = (p1[0] + ux * consumed, p1[1] + uy * consumed)

                if drawing:
                    active_seg.append(interp_pt)

                if current_len >= target_len:
                    if drawing and len(active_seg) >= 2:
                        segments.append(active_seg)
                    drawing = not drawing
                    current_len = 0.0
                    active_seg = [interp_pt] if drawing else []

        if drawing and len(active_seg) >= 2:
            segments.append(active_seg)
    else:
        segments = [coords_xy]

    if not segments:
        return None

    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    half_w = width / 2.0

    for seg in segments:
        left_verts, right_verts = [], []
        for i, (x, y) in enumerate(seg):
            if i == 0:
                dx, dy = seg[1][0] - x, seg[1][1] - y
            elif i == len(seg) - 1:
                dx, dy = x - seg[i - 1][0], y - seg[i - 1][1]
            else:
                dx, dy = seg[i + 1][0] - seg[i - 1][0], seg[i + 1][1] - seg[i - 1][1]

            length = math.hypot(dx, dy)
            nx, ny = (0, 1) if length == 0 else (-dy / length, dx / length)
            left_verts.append(bm.verts.new((x + nx * half_w, y + ny * half_w, elevation)))
            right_verts.append(bm.verts.new((x - nx * half_w, y - ny * half_w, elevation)))

        for i in range(len(seg) - 1):
            try:
                bm.faces.new((left_verts[i], left_verts[i + 1], right_verts[i + 1], right_verts[i]))
            except ValueError:
                continue

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()

    if material:
        obj.data.materials.append(material)
    return obj


def resolve_surface_material(surf_id, mats, is_runway=False):
    surf_map = {
        1: ("rwy_asphalt" if is_runway else "taxi_asphalt"),
        2: ("rwy_concrete" if is_runway else "taxi_concrete"),
        3: "rwy_turf",
        4: "rwy_dirt",
        5: "rwy_gravel",
        12: "rwy_asphalt",
        13: "rwy_concrete",
        14: "rwy_turf",
        15: "rwy_dirt",
    }
    key = surf_map.get(surf_id, "rwy_asphalt" if is_runway else "taxi_asphalt")
    return mats.get(key, mats["rwy_asphalt"])


def resolve_marking_spec(m_type, scale, mats):
    base_w = 0.35 * scale
    wide_w = 0.60 * scale
    hold_w = 0.90 * scale

    if m_type == 1:
        return mats["paint_yellow"], base_w, None
    elif m_type == 2:
        return mats["paint_yellow"], base_w, (3.0 * scale, 3.0 * scale)
    elif m_type == 3:
        return mats["paint_white"], base_w, None
    elif m_type in (4, 52):
        return mats["paint_white"], base_w, (3.0 * scale, 3.0 * scale)
    elif m_type in (5, 53):
        return mats["paint_yellow"], wide_w, None
    elif m_type in (6, 7):
        return mats["paint_red"], hold_w, None
    elif m_type in (8, 9):
        return mats["paint_orange"], base_w, None
    elif m_type in (10, 11):
        return mats["paint_blue"], base_w, None
    elif m_type == 50:
        return mats["paint_green"], base_w, None
    elif m_type == 51:
        return mats["paint_black"], wide_w, None
    return mats["paint_yellow"], base_w, None


def build_accurate_runway(rwy_data, origin_lat, origin_lon, scale, collection, mats):
    ends = rwy_data.get("ends", [])
    if len(ends) < 2:
        return None

    r1, r2 = ends[0], ends[1]
    name = f"Runway_{r1.get('name', '01')}_{r2.get('name', '19')}"
    width = float(rwy_data.get("width_m", 45.0)) * scale

    p1 = latlon_to_local_xy(float(r1["lat"]), float(r1["lon"]), origin_lat, origin_lon, scale)
    p2 = latlon_to_local_xy(float(r2["lat"]), float(r2["lon"]), origin_lat, origin_lon, scale)

    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return None

    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    half_w = width / 2.0

    c1 = (p1[0] + nx * half_w, p1[1] + ny * half_w)
    c2 = (p2[0] + nx * half_w, p2[1] + ny * half_w)
    c3 = (p2[0] - nx * half_w, p2[1] - ny * half_w)
    c4 = (p1[0] - nx * half_w, p1[1] - ny * half_w)

    surf_type = rwy_data.get("surface", 1)
    mat = resolve_surface_material(surf_type, mats, is_runway=True)
    return build_complex_polygon(name, [[c1, c2, c3, c4]], 0.002 * scale, collection, mat)


def generate_wsairport_model(apt_data, scale=0.1, props=None):
    center = apt_data.get("center", [0.0, 0.0])
    origin_lat, origin_lon = float(center[0]), float(center[1])
    icao = apt_data.get("icao") or apt_data.get("id") or "Airport"
    cols = prepare_airport_hierarchy(f"Airport_{icao}")

    mats = {
        "rwy_asphalt": get_or_create_material("WSA_Runway_Asphalt", SURFACE_COLORS["runway_asphalt"]),
        "rwy_concrete": get_or_create_material("WSA_Runway_Concrete", SURFACE_COLORS["runway_concrete"]),
        "rwy_turf": get_or_create_material("WSA_Runway_Turf", SURFACE_COLORS["runway_turf"]),
        "rwy_dirt": get_or_create_material("WSA_Runway_Dirt", SURFACE_COLORS["runway_dirt"]),
        "rwy_gravel": get_or_create_material("WSA_Runway_Gravel", SURFACE_COLORS["runway_gravel"]),
        "taxi_asphalt": get_or_create_material("WSA_Taxiway_Asphalt", SURFACE_COLORS["taxiway_asphalt"]),
        "taxi_concrete": get_or_create_material("WSA_Taxiway_Concrete", SURFACE_COLORS["taxiway_concrete"]),
        "apron": get_or_create_material("WSA_Apron", SURFACE_COLORS["apron"]),
        "road": get_or_create_material("WSA_Road", SURFACE_COLORS["road"]),
        "paint_white": get_or_create_material("WSA_Line_White", SURFACE_COLORS["paint_white"]),
        "paint_yellow": get_or_create_material("WSA_Line_Yellow", SURFACE_COLORS["paint_yellow"]),
        "paint_red": get_or_create_material("WSA_Line_Red", SURFACE_COLORS["paint_red"]),
        "paint_orange": get_or_create_material("WSA_Line_Orange", SURFACE_COLORS["paint_orange"]),
        "paint_blue": get_or_create_material("WSA_Line_Blue", SURFACE_COLORS["paint_blue"]),
        "paint_green": get_or_create_material("WSA_Line_Green", SURFACE_COLORS["paint_green"]),
        "paint_black": get_or_create_material("WSA_Line_Black", SURFACE_COLORS["paint_black"]),
        "helipad": get_or_create_material("WSA_Helipad", SURFACE_COLORS["helipad"]),
        "windsock": get_or_create_material("WSA_Windsock", SURFACE_COLORS["windsock"]),
        "beacon": get_or_create_material("WSA_Beacon", SURFACE_COLORS["beacon"]),
        "building": get_or_create_material("OSM_Building_Generic", SURFACE_COLORS["building"]),
    }

    counts = {
        "runways": 0, "taxiways": 0, "markings": 0, "gates": 0,
        "jetways": 0, "helipads": 0, "windsocks": 0, "beacons": 0, "buildings": 0
    }

    # 1. Runways
    if props.import_runways:
        for rwy in apt_data.get("runways", []):
            if build_accurate_runway(rwy, origin_lat, origin_lon, scale, cols["runways"], mats):
                counts["runways"] += 1

    # 2. Taxiways, Aprons & Service Roads
    if props.import_taxiways:
        for idx, poly in enumerate(apt_data.get("taxiways", [])):
            rings_xy = []
            for ring in poly.get("rings", []):
                ring_xy = [latlon_to_local_xy(float(pt[0]), float(pt[1]), origin_lat, origin_lon, scale) for pt in ring if len(pt) >= 2]
                if len(ring_xy) >= 3:
                    rings_xy.append(ring_xy)

            if not rings_xy:
                continue

            kind = poly.get("kind", "taxiway")
            surf = poly.get("surface", 1)
            name = poly.get("name") or f"{kind.capitalize()}_{idx + 1}"

            if kind == "apron":
                mat, target_col, elev = mats["apron"], cols["aprons"], 0.001 * scale
            elif kind == "road":
                if not props.import_roads:
                    continue
                mat, target_col, elev = mats["road"], cols["roads"], 0.003 * scale
            else:
                mat = resolve_surface_material(surf, mats, is_runway=False)
                target_col, elev = cols["taxiways"], 0.0015 * scale

            if build_complex_polygon(name, rings_xy, elev, target_col, mat):
                counts["taxiways"] += 1

    # 3. Markings
    if props.import_markings:
        for idx, line in enumerate(apt_data.get("markings", [])):
            pts_xy = [latlon_to_local_xy(float(p[0]), float(p[1]), origin_lat, origin_lon, scale) for p in line.get("points", []) if len(p) >= 2]
            if len(pts_xy) < 2:
                continue
            paint_type = line.get("type", 1)
            mat, width, dash = resolve_marking_spec(paint_type, scale, mats)
            m_name = line.get("name") or f"Marking_{idx + 1}_Type{paint_type}"
            if build_line_strip_ribbon(m_name, pts_xy, 0.004 * scale, width, cols["markings"], mat, dash_pattern=dash):
                counts["markings"] += 1

    # 4. Helipads
    if props.import_helipads:
        for idx, heli in enumerate(apt_data.get("helipads", [])):
            if not isinstance(heli, dict) or "lat" not in heli or "lon" not in heli:
                continue
            cx, cy = latlon_to_local_xy(float(heli["lat"]), float(heli["lon"]), origin_lat, origin_lon, scale)
            length, width = float(heli.get("length_m", 20.0)) * scale, float(heli.get("width_m", 20.0)) * scale
            hdg_rad = math.radians(float(heli.get("heading", 0.0)))
            hw, hl = width / 2.0, length / 2.0
            corners = [(-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl)]
            rot_corners = [
                (cx + px * math.cos(hdg_rad) - py * math.sin(hdg_rad), cy + px * math.sin(hdg_rad) + py * math.cos(hdg_rad))
                for px, py in corners
            ]
            if build_complex_polygon(f"Helipad_{heli.get('name', idx + 1)}", [rot_corners], 0.003 * scale, cols["helipads"], mats["helipad"]):
                counts["helipads"] += 1

    # 5. Windsocks
    if props.import_windsocks:
        for idx, sock in enumerate(apt_data.get("windsocks", [])):
            if not isinstance(sock, dict) or "lat" not in sock or "lon" not in sock:
                continue
            sx, sy = latlon_to_local_xy(float(sock["lat"]), float(sock["lon"]), origin_lat, origin_lon, scale)
            emp = bpy.data.objects.new(f"Windsock_{idx + 1}", None)
            emp.empty_display_type = 'CONE'
            emp.empty_display_size = 2.0 * scale
            emp.location = (sx, sy, 0.005 * scale)
            emp["lit"] = bool(sock.get("lit", False))
            cols["windsocks"].objects.link(emp)
            counts["windsocks"] += 1

    # 6. Beacons
    if props.import_beacons:
        for idx, bcn in enumerate(apt_data.get("beacons", [])):
            if not isinstance(bcn, dict) or "lat" not in bcn or "lon" not in bcn:
                continue
            bx, by = latlon_to_local_xy(float(bcn["lat"]), float(bcn["lon"]), origin_lat, origin_lon, scale)
            emp = bpy.data.objects.new(f"Beacon_{idx + 1}", None)
            emp.empty_display_type = 'SPHERE'
            emp.empty_display_size = 2.5 * scale
            emp.location = (bx, by, 0.005 * scale)
            cols["beacons"].objects.link(emp)
            counts["beacons"] += 1

    # 7. Gates (1300/1301)
    if props.import_gates:
        for gate in apt_data.get("gates", []):
            if not isinstance(gate, dict) or "lat" not in gate or "lon" not in gate:
                continue
            gx, gy = latlon_to_local_xy(float(gate["lat"]), float(gate["lon"]), origin_lat, origin_lon, scale)
            emp = bpy.data.objects.new(str(gate.get("name", "Gate")), None)
            emp.empty_display_type = 'ARROWS'
            emp.empty_display_size = 2.5 * scale
            emp.location = (gx, gy, 0.005 * scale)
            emp.rotation_euler = (0, 0, math.radians(-float(gate.get("heading", 0.0))))
            emp["gate_type"] = str(gate.get("type", "gate"))
            emp["aircraft"] = str(gate.get("aircraft", "all"))
            emp["airlines"] = str(gate.get("airlines", ""))
            cols["gates"].objects.link(emp)
            counts["gates"] += 1

    # 8. Jetways (1500)
    if props.import_jetways:
        for jw in apt_data.get("jetways", []):
            if not isinstance(jw, dict) or "lat" not in jw or "lon" not in jw:
                continue
            jx, jy = latlon_to_local_xy(float(jw["lat"]), float(jw["lon"]), origin_lat, origin_lon, scale)
            emp = bpy.data.objects.new(str(jw.get("name", "Jetway")), None)
            emp.empty_display_type = 'SINGLE_ARROW'
            emp.empty_display_size = 4.0 * scale
            emp.location = (jx, jy, 0.005 * scale)
            emp.rotation_euler = (0, 0, math.radians(-float(jw.get("heading", 0.0))))
            cols["jetways"].objects.link(emp)
            counts["jetways"] += 1

    # 9. OSM 3D Buildings Query & Generation
    if props.import_osm_buildings:
        try:
            osm_res = fetch_osm_buildings(origin_lat, origin_lon, props.osm_radius)
            nodes = {}
            for el in osm_res.get("elements", []):
                if el["type"] == "node":
                    nodes[el["id"]] = (el["lat"], el["lon"])

            for el in osm_res.get("elements", []):
                if el["type"] == "way" and "building" in el.get("tags", {}):
                    tags = el.get("tags", {})
                    way_nodes = el.get("nodes", [])
                    coords_xy = []
                    for nid in way_nodes:
                        if nid in nodes:
                            nlat, nlon = nodes[nid]
                            coords_xy.append(latlon_to_local_xy(nlat, nlon, origin_lat, origin_lon, scale))

                    if len(coords_xy) >= 3:
                        is_closed = way_nodes[0] == way_nodes[-1]
                        if is_closed:
                            b_name = tags.get("name", f"Building_{el['id']}")
                            b_height = get_building_height(tags, scale)
                            if build_complex_polygon(b_name, [coords_xy], 0.0, cols["buildings"], mats["building"], extrude=b_height):
                                counts["buildings"] += 1
        except Exception as e:
            print(f"[!] OSM Building import encountered an error: {e}")

    return counts


# --- UI, Settings & Operators ---

class AirportOSMSettings(PropertyGroup):
    dataset_directory: StringProperty(
        name="Database Folder",
        description="Select folder containing airports_index.json and chunks/",
        subtype='DIR_PATH',
    )
    search_term: StringProperty(
        name="Target ICAO",
        description="4-letter ICAO identifier (e.g. KJFK, EGLL, LFPG)",
        default="KJFK",
    )
    scale: FloatProperty(
        name="Scale",
        description="1.0 = Full scale meters, 0.1 = default viewport scale",
        default=0.1,
        min=0.0001,
        max=10.0,
    )
    license_accepted: BoolProperty(
        name="I accept GPLv2 terms & mandatory attribution conditions",
        description="Acknowledge dataset licenses and credit requirements to unlock generation",
        default=False,
    )
    import_runways: BoolProperty(name="Runways", default=True)
    import_taxiways: BoolProperty(name="Taxiways & Aprons", default=True)
    import_roads: BoolProperty(name="Service Roads", default=True)
    import_markings: BoolProperty(name="Markings & Lines", default=True)
    import_gates: BoolProperty(name="Gates / Ramp Starts", default=True)
    import_jetways: BoolProperty(name="Jetways (Active Row 1500)", default=True)
    import_helipads: BoolProperty(name="Helipads", default=True)
    import_windsocks: BoolProperty(name="Windsocks", default=True)
    import_beacons: BoolProperty(name="Airport Beacons", default=True)

    # OSM Buildings Integration Toggle
    import_osm_buildings: BoolProperty(
        name="OSM 3D Buildings",
        description="Download and extrude building footprints in 3D from OpenStreetMap",
        default=True,
    )
    osm_radius: FloatProperty(
        name="Building Radius (m)",
        description="Search radius around airport center for terminal and hangar buildings",
        default=3500.0,
        min=500.0,
        max=15000.0,
    )
    status_text: StringProperty(default="")


class AIRPORTOSM_OT_open_github(Operator):
    bl_idname = "airportosm.open_github"
    bl_label = "Open Dataset GitHub"
    bl_description = "Open the official Wings Simulations Airport Database repository in your browser"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        webbrowser.open(GITHUB_REPO_URL)
        return {"FINISHED"}


class AIRPORTOSM_OT_show_tutorial(Operator):
    bl_idname = "airportosm.show_tutorial"
    bl_label = "How to Download Database"
    bl_description = "Step-by-step setup guide for obtaining the global dataset"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="HOW TO DOWNLOAD & CONFIGURE DATABASE", icon="HELP")

        col = box.column(align=True)
        col.label(text="Step 1: Download the Database Repository")
        col.label(text="  - Option A: Click 'Open GitHub Repo' below, click 'Code' > 'Download ZIP'")
        col.label(text="  - Option B: git clone https://github.com/WingsSimulations/wsairports-database.git")
        col.separator()
        col.label(text="Step 2: Extract & Verify Folder Structure")
        col.label(text="  Make sure your extracted directory contains:")
        col.label(text="    • airports_index.json  (Master index file)")
        col.label(text="    • chunks/              (Folder containing .wscairport chunks)")
        col.separator()
        col.label(text="Step 3: Link in Blender")
        col.label(text="  In the sidebar panel, click the folder icon on 'Database Folder' and select")
        col.label(text="  the directory containing 'airports_index.json'.")

        box.separator()
        box.operator("airportosm.open_github", text="Open GitHub Repository in Browser", icon="URL")


class AIRPORTOSM_OT_show_license(Operator):
    bl_idname = "airportosm.show_license"
    bl_label = "Dataset License & DMCA Terms"
    bl_description = "View data origin licenses, terms of use, and legal attribution requirements"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="MANDATORY LEGAL NOTICE & GPLv2 TERMS", icon="LOCKED")

        col = box.column(align=True)
        col.label(text="This dataset contains compiled scenery information derived from:")
        col.label(text="1. X-Plane Airport Scenery Gateway (C) Laminar Research (GPLv2)")
        col.label(text="2. Wings Simulations Airport Database (C) 2026 Wings Simulations (GPLv2)")
        col.label(text="3. OpenStreetMap Buildings (C) OpenStreetMap contributors (ODbL)")
        col.separator()
        col.label(text="ATTRIBUTION REQUIREMENT & DMCA WARNING:", icon="ERROR")
        col.label(text="Any 3D scenery, exports, game levels, or derivative assets generated")
        col.label(text="with this addon MUST give clear credit in documentation/metadata to:")
        col.label(text="  - Laminar Research / Gateway Scenery Authors")
        col.label(text="  - Wings Simulations")
        col.label(text="  - OpenStreetMap contributors")
        col.separator()
        col.label(text="Unattributed redistribution or commercial packaging without attribution")
        col.label(text="violates the GPLv2 license and is subject to immediate DMCA takedown.")


class AIRPORTOSM_OT_generate(Operator):
    bl_idname = "airportosm.generate"
    bl_label = "Generate Airport"
    bl_description = "Locate airport in chunked database and generate 3D model with OSM buildings"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.airport_osm_settings

        if not settings.license_accepted:
            self.report({"ERROR"}, "You must accept the License & DMCA Attribution Terms to generate models")
            return {"CANCELLED"}

        db_folder = bpy.path.abspath(settings.dataset_directory).strip()
        icao = settings.search_term.strip().upper()

        if not db_folder or not os.path.isdir(db_folder):
            self.report({"ERROR"}, "Please select the root folder containing airports_index.json")
            return {"CANCELLED"}

        if not icao:
            self.report({"ERROR"}, "Please enter a valid airport ICAO code")
            return {"CANCELLED"}

        apt_data, err = fetch_airport_from_chunk(db_folder, icao)
        if err:
            self.report({"ERROR"}, err)
            settings.status_text = err
            return {"CANCELLED"}

        counts = generate_wsairport_model(apt_data, scale=settings.scale, props=settings)
        msg = (
            f"Generated {icao}: {counts['runways']} runways, {counts['taxiways']} taxiways, "
            f"{counts['markings']} markings, {counts['gates']} gates, {counts['windsocks']} windsocks, "
            f"{counts['beacons']} beacons, {counts['buildings']} OSM buildings"
        )
        settings.status_text = msg
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class AIRPORTOSM_PT_panel(Panel):
    bl_label = "Airport Importer Pro"
    bl_idname = "AIRPORTOSM_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Airport Pro"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.airport_osm_settings

        guide_box = layout.box()
        guide_box.label(text="Setup & Database Guide:", icon="HELP")
        row = guide_box.row(align=True)
        row.operator("airportosm.show_tutorial", text="Setup Tutorial", icon="INFO")
        row.operator("airportosm.open_github", text="Download DB", icon="URL")

        box = layout.box()
        box.label(text="WSAirports Database:", icon="WORLD_DATA")
        box.prop(settings, "dataset_directory")

        if _WSA_INDEX and _LOADED_DB_PATH == bpy.path.abspath(settings.dataset_directory).strip():
            box.label(text=f"Index Ready: {len(_WSA_INDEX):,} airports", icon="CHECKMARK")
        else:
            box.label(text="Select folder with airports_index.json", icon="INFO")

        box.separator()
        box.prop(settings, "search_term")

        layer_box = layout.box()
        layer_box.label(text="Include Layers:")
        col = layer_box.column(align=True)
        col.prop(settings, "import_runways")
        col.prop(settings, "import_taxiways")
        col.prop(settings, "import_roads")
        col.prop(settings, "import_markings")
        col.prop(settings, "import_gates")
        col.prop(settings, "import_jetways")
        col.prop(settings, "import_helipads")
        col.prop(settings, "import_windsocks")
        col.prop(settings, "import_beacons")

        osm_box = layout.box()
        osm_box.label(text="OpenStreetMap Integration:", icon="COMMUNITY")
        osm_box.prop(settings, "import_osm_buildings")
        if settings.import_osm_buildings:
            osm_box.prop(settings, "osm_radius")

        layout.separator()
        layout.prop(settings, "scale")

        legal_box = layout.box()
        legal_box.alert = not settings.license_accepted
        legal_box.label(text="Mandatory Attribution Notice:", icon="GHOST_ENABLED")

        row = legal_box.row()
        row.operator("airportosm.show_license", text="View Full License & Terms", icon="TEXT")

        legal_box.prop(settings, "license_accepted")
        if not settings.license_accepted:
            legal_box.label(text="* Unattributed work is subject to DMCA takedown", icon="ERROR")

        layout.separator()

        gen_row = layout.row()
        gen_row.enabled = settings.license_accepted
        gen_row.operator("airportosm.generate", icon="MESH_GRID", text="Generate Airport Model")

        if settings.status_text:
            layout.label(text=settings.status_text)


classes = (
    AirportOSMSettings,
    AIRPORTOSM_OT_open_github,
    AIRPORTOSM_OT_show_tutorial,
    AIRPORTOSM_OT_show_license,
    AIRPORTOSM_OT_generate,
    AIRPORTOSM_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.airport_osm_settings = PointerProperty(type=AirportOSMSettings)


def unregister():
    del bpy.types.Scene.airport_osm_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()