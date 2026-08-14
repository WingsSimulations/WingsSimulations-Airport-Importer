bl_info = {
    "name": "Airport OSM Importer",
    "author": "Martin F",
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Airport OSM",
    "description": "Generate a base 3D model of an airport (runways, taxiways, aprons/tarmac, buildings) from OpenStreetMap data via the Overpass API",
    "category": "Import-Export",
}

import bpy
import bmesh
import math
import json
import csv
import io
import os
import time
import threading
import queue
import urllib.request
import urllib.error
from bpy.props import StringProperty, FloatProperty, CollectionProperty, BoolProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup

AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

_AIRPORT_DB = {}

EARTH_RADIUS = 6378137.0

DEFAULT_WIDTHS = {
    "runway": 45.0,
    "taxiway": 23.0,
    "taxilane": 18.0,
}
BUILDING_DEFAULT_HEIGHT = 6.0
LEVEL_HEIGHT = 3.0


def get_data_dir():
    return bpy.utils.user_resource('CONFIG', path="airport_osm", create=True)


def get_cache_path():
    return os.path.join(get_data_dir(), "airports.json")


def make_display_string(rec):
    code = rec.get("icao") or rec.get("iata") or ""
    label = f"{rec['name']}"
    if code:
        label += f" ({code})"
    if rec.get("country"):
        label += f" - {rec['country']}"
    return label


def parse_airport_csv(raw_text):
    reader = csv.DictReader(io.StringIO(raw_text))
    records = {}
    allowed_types = {"large_airport", "medium_airport", "small_airport"}
    for row in reader:
        try:
            if row.get("type") not in allowed_types:
                continue
            icao = (row.get("icao_code") or row.get("gps_code") or row.get("ident") or "").strip()
            iata = (row.get("iata_code") or "").strip()
            if not icao and not iata:
                continue
            name = (row.get("name") or "").strip()
            lat = float(row.get("latitude_deg"))
            lon = float(row.get("longitude_deg"))
            country = (row.get("iso_country") or "").strip()
            rec = {
                "icao": icao,
                "iata": iata,
                "name": name,
                "lat": lat,
                "lon": lon,
                "country": country,
                "type": row.get("type"),
            }
            records[make_display_string(rec)] = rec
        except (ValueError, TypeError, KeyError):
            continue
    return records


def load_airport_database():
    global _AIRPORT_DB
    path = get_cache_path()
    if not os.path.exists(path):
        _AIRPORT_DB = {}
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        _AIRPORT_DB = cache.get("airports", {})
        return len(_AIRPORT_DB)
    except (json.JSONDecodeError, OSError):
        _AIRPORT_DB = {}
        return 0


def database_last_updated_str():
    path = get_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        ts = cache.get("downloaded_at")
        if ts:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (json.JSONDecodeError, OSError):
        pass
    return None


def refresh_search_collection(context):
    wm = context.window_manager
    wm.airport_osm_items.clear()
    for key in sorted(_AIRPORT_DB.keys()):
        item = wm.airport_osm_items.add()
        item.name = key


def build_query(search_term):
    escaped = search_term.replace('"', '\\"')
    query = f"""
[out:json][timeout:180];
(
  node["aeroway"="aerodrome"]["name"~"{escaped}",i];
  way["aeroway"="aerodrome"]["name"~"{escaped}",i];
  relation["aeroway"="aerodrome"]["name"~"{escaped}",i];
  node["aeroway"="aerodrome"]["icao"="{escaped}"];
  way["aeroway"="aerodrome"]["icao"="{escaped}"];
  relation["aeroway"="aerodrome"]["icao"="{escaped}"];
  node["aeroway"="aerodrome"]["iata"="{escaped}"];
  way["aeroway"="aerodrome"]["iata"="{escaped}"];
  relation["aeroway"="aerodrome"]["iata"="{escaped}"];
);
out center 1;
"""
    return query.strip()


def build_detail_query(lat, lon, radius_m):
    query = f"""
[out:json][timeout:180];
(
  way["aeroway"](around:{radius_m},{lat},{lon});
  relation["aeroway"](around:{radius_m},{lat},{lon});
  way["building"](around:{radius_m},{lat},{lon});
  relation["building"](around:{radius_m},{lat},{lon});
);
out body;
>;
out skel qt;
"""
    return query.strip()


def run_overpass_query(query, timeout=180):
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            payload = ("data=" + urllib.request.quote(query)).encode("utf-8")
            req = urllib.request.Request(endpoint, data=payload, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("User-Agent", "Blender-Airport-OSM-Importer/1.0")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def latlon_to_local_xy(lat, lon, origin_lat, origin_lon, scale=1.0):
    origin_lat_rad = math.radians(origin_lat)
    x = math.radians(lon - origin_lon) * EARTH_RADIUS * math.cos(origin_lat_rad) * scale
    y = math.radians(lat - origin_lat) * EARTH_RADIUS * scale
    return x, y


class OSMData:
    def __init__(self, elements):
        self.nodes = {}
        self.ways = {}
        self.relations = {}
        for el in elements:
            if el["type"] == "node":
                self.nodes[el["id"]] = (el["lat"], el["lon"])
            elif el["type"] == "way":
                self.ways[el["id"]] = el
            elif el["type"] == "relation":
                self.relations[el["id"]] = el

    def way_coords(self, way):
        coords = []
        for nid in way.get("nodes", []):
            if nid in self.nodes:
                coords.append(self.nodes[nid])
        return coords


def make_collection(name):
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    return col


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


def build_flat_polygon_mesh(name, coords_xy, elevation, collection, material=None, extrude=0.0):
    if len(coords_xy) < 3:
        return None
    if coords_xy[0] == coords_xy[-1]:
        coords_xy = coords_xy[:-1]
    if len(coords_xy) < 3:
        return None

    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    verts = [bm.verts.new((x, y, elevation)) for x, y in coords_xy]
    try:
        face = bm.faces.new(verts)
    except ValueError:
        bm.free()
        return None

    if extrude > 0.0:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        ret = bmesh.ops.extrude_face_region(bm, geom=[face])
        extruded_verts = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, vec=(0, 0, extrude), verts=extruded_verts)

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()

    if material:
        obj.data.materials.append(material)

    return obj


def build_line_strip_as_ribbon(name, coords_xy, elevation, width, collection, material=None):
    if len(coords_xy) < 2:
        return None

    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    left_verts = []
    right_verts = []
    half_w = width / 2.0

    for i, (x, y) in enumerate(coords_xy):
        if i == 0:
            dx = coords_xy[1][0] - x
            dy = coords_xy[1][1] - y
        elif i == len(coords_xy) - 1:
            dx = x - coords_xy[i - 1][0]
            dy = y - coords_xy[i - 1][1]
        else:
            dx = coords_xy[i + 1][0] - coords_xy[i - 1][0]
            dy = coords_xy[i + 1][1] - coords_xy[i - 1][1]
        length = math.hypot(dx, dy)
        if length == 0:
            nx, ny = 0, 1
        else:
            nx, ny = -dy / length, dx / length
        left_verts.append(bm.verts.new((x + nx * half_w, y + ny * half_w, elevation)))
        right_verts.append(bm.verts.new((x - nx * half_w, y - ny * half_w, elevation)))

    for i in range(len(coords_xy) - 1):
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


def get_way_width(tags, feature_type, scale=1.0):
    if "width" in tags:
        try:
            return float(tags["width"].split()[0]) * scale
        except Exception:
            pass
    return DEFAULT_WIDTHS.get(feature_type, 20.0) * scale


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


def prepare_collections():
    root_col = make_collection("Airport")
    col_runways = make_collection("Runways")
    col_taxiways = make_collection("Taxiways")
    col_aprons = make_collection("Aprons_Tarmac")
    col_buildings = make_collection("Buildings")
    for c in (col_runways, col_taxiways, col_aprons, col_buildings):
        if c.name not in root_col.children:
            try:
                root_col.children.link(c)
            except Exception:
                pass
        if c.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(c)
    return root_col, col_runways, col_taxiways, col_aprons, col_buildings


def build_task_list(osm, origin_lat, origin_lon, scale=1.0):
    tasks = []
    for way_id, way in osm.ways.items():
        tags = way.get("tags", {})
        coords_ll = osm.way_coords(way)
        if len(coords_ll) < 2:
            continue
        coords_xy = [latlon_to_local_xy(lat, lon, origin_lat, origin_lon, scale) for lat, lon in coords_ll]
        is_closed = coords_ll[0] == coords_ll[-1] if len(coords_ll) > 2 else False
        aeroway = tags.get("aeroway")

        if aeroway == "runway":
            tasks.append(("runway", way_id, tags, coords_xy, is_closed))
        elif aeroway in ("taxiway", "taxilane"):
            tasks.append(("taxiway", way_id, tags, coords_xy, is_closed))
        elif aeroway in ("apron", "tarmac"):
            tasks.append(("apron", way_id, tags, coords_xy, is_closed))
        elif "building" in tags:
            tasks.append(("building", way_id, tags, coords_xy, is_closed))
    return tasks


def execute_task(task, cols, mats, scale=1.0):
    kind, way_id, tags, coords_xy, is_closed = task
    _, col_runways, col_taxiways, col_aprons, col_buildings = cols
    mat_runway, mat_taxiway, mat_apron, mat_building = mats

    if kind == "runway":
        name = f"Runway_{tags.get('ref', way_id)}"
        width = get_way_width(tags, "runway", scale)
        if is_closed and len(coords_xy) >= 4:
            obj = build_flat_polygon_mesh(name, coords_xy, 0.0, col_runways, mat_runway)
        else:
            obj = build_line_strip_as_ribbon(name, coords_xy, 0.0, width, col_runways, mat_runway)
        return "runway" if obj else None

    if kind == "taxiway":
        name = f"Taxiway_{tags.get('ref', way_id)}"
        width = get_way_width(tags, tags.get("aeroway"), scale)
        elev = 0.005 * scale
        if is_closed and len(coords_xy) >= 4:
            obj = build_flat_polygon_mesh(name, coords_xy, elev, col_taxiways, mat_taxiway)
        else:
            obj = build_line_strip_as_ribbon(name, coords_xy, elev, width, col_taxiways, mat_taxiway)
        return "taxiway" if obj else None

    if kind == "apron":
        name = f"Apron_{way_id}"
        elev = 0.01 * scale
        if is_closed and len(coords_xy) >= 3:
            obj = build_flat_polygon_mesh(name, coords_xy, elev, col_aprons, mat_apron)
            return "apron" if obj else None
        return None

    if kind == "building":
        name = tags.get("name", f"Building_{way_id}")
        height = get_building_height(tags, scale)
        if is_closed and len(coords_xy) >= 3:
            obj = build_flat_polygon_mesh(name, coords_xy, 0.0, col_buildings, mat_building, extrude=height)
            return "building" if obj else None
        return None

    return None


class AirportSearchItem(PropertyGroup):
    pass


class AirportOSMSettings(PropertyGroup):
    search_term: StringProperty(
        name="Airport",
        description="Start typing a name, ICAO (e.g. KSFO) or IATA (e.g. SFO) code and pick from the list",
        default="",
    )
    radius: FloatProperty(
        name="Search Radius (m)",
        description="Radius around the airport center to pull features from",
        default=3000.0,
        min=200.0,
        max=20000.0,
    )
    scale: FloatProperty(
        name="Scale",
        description="Scale factor for generated meshes (1.0 = full scale in meters, 0.1 = default)",
        default=0.1,
        min=0.0001,
        max=100.0,
    )
    status_text: StringProperty(default="")
    is_running: BoolProperty(default=False)
    progress: FloatProperty(default=0.0, min=0.0, max=1.0)


class AIRPORTOSM_OT_download_db(Operator):
    bl_idname = "airportosm.download_db"
    bl_label = "Update Airport Database"
    bl_description = "Download/refresh the offline airport database from OurAirports without freezing Blender"
    bl_options = {"REGISTER"}

    _timer = None
    _thread = None
    _result_queue = None

    def invoke(self, context, event):
        settings = context.scene.airport_osm_settings
        settings.is_running = True
        settings.status_text = "Downloading airport database..."
        self._result_queue = queue.Queue()
        self._thread = threading.Thread(target=self._download_worker, daemon=True)
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _download_worker(self):
        try:
            req = urllib.request.Request(
                AIRPORTS_CSV_URL, headers={"User-Agent": "Blender-Airport-OSM-Importer/1.0"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            records = parse_airport_csv(raw)
            self._result_queue.put(("done", records))
        except Exception as e:
            self._result_queue.put(("error", str(e)))

    def modal(self, context, event):
        settings = context.scene.airport_osm_settings

        if event.type == 'TIMER':
            try:
                status, payload = self._result_queue.get_nowait()
            except queue.Empty:
                return {"RUNNING_MODAL"}

            self._finish(context)

            if status == "error":
                settings.status_text = f"Download failed: {payload}"
                self.report({"ERROR"}, f"Failed to download airport database: {payload}")
                return {"CANCELLED"}

            records = payload
            cache = {"downloaded_at": time.time(), "count": len(records), "airports": records}
            with open(get_cache_path(), "w", encoding="utf-8") as f:
                json.dump(cache, f)

            load_airport_database()
            refresh_search_collection(context)
            settings.status_text = f"Airport database ready: {len(records)} airports"
            self.report({"INFO"}, settings.status_text)
            return {"FINISHED"}

        return {"RUNNING_MODAL"}

    def _finish(self, context):
        settings = context.scene.airport_osm_settings
        settings.is_running = False
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None


class AIRPORTOSM_OT_generate(Operator):
    bl_idname = "airportosm.generate"
    bl_label = "Generate Airport Model"
    bl_description = "Look up the airport and build a base 3D model, assembling objects live in the viewport"
    bl_options = {"REGISTER", "UNDO"}

    _timer = None
    _thread = None
    _result_queue = None
    _tasks = None
    _task_index = 0
    _cols = None
    _mats = None
    _counts = None
    _phase = "resolving"
    _origin_lat = None
    _origin_lon = None
    _scale = 0.1

    TASKS_PER_TICK = 4

    def invoke(self, context, event):
        settings = context.scene.airport_osm_settings
        term = settings.search_term.strip()
        if not term:
            self.report({"ERROR"}, "Pick an airport from the search field")
            return {"CANCELLED"}

        if not _AIRPORT_DB:
            load_airport_database()

        rec = _AIRPORT_DB.get(term)
        if rec is None:
            term_lower = term.lower()
            for key, r in _AIRPORT_DB.items():
                if (
                    term_lower == r.get("icao", "").lower()
                    or term_lower == r.get("iata", "").lower()
                    or term_lower in r.get("name", "").lower()
                ):
                    rec = r
                    break

        self._tasks = None
        self._task_index = 0
        self._counts = {"runway": 0, "taxiway": 0, "apron": 0, "building": 0}
        self._result_queue = queue.Queue()
        self._scale = settings.scale
        settings.is_running = True
        settings.progress = 0.0

        if rec is not None:
            self._origin_lat, self._origin_lon = rec["lat"], rec["lon"]
            self._phase = "fetching_detail"
            settings.status_text = f"Fetching features around {rec['name']}..."
            self._thread = threading.Thread(
                target=self._detail_worker, args=(self._origin_lat, self._origin_lon, settings.radius), daemon=True
            )
        else:
            self._phase = "resolving"
            settings.status_text = f"Resolving '{term}' via Overpass..."
            self._thread = threading.Thread(target=self._resolve_worker, args=(term,), daemon=True)

        self._thread.start()
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _resolve_worker(self, term):
        try:
            find_query = build_query(term)
            result = run_overpass_query(find_query)
            elements = result.get("elements", [])
            if not elements:
                self._result_queue.put(("error", f"No airport found matching '{term}'"))
                return
            el = elements[0]
            if el["type"] == "node":
                lat, lon = el["lat"], el["lon"]
            else:
                center = el.get("center")
                if not center:
                    self._result_queue.put(("error", "Could not resolve airport center coordinates"))
                    return
                lat, lon = center["lat"], center["lon"]
            self._result_queue.put(("resolved", (lat, lon)))
        except Exception as e:
            self._result_queue.put(("error", str(e)))

    def _detail_worker(self, lat, lon, radius):
        try:
            detail_query = build_detail_query(lat, lon, radius)
            detail_result = run_overpass_query(detail_query)
            self._result_queue.put(("detail", detail_result))
        except Exception as e:
            self._result_queue.put(("error", str(e)))

    def modal(self, context, event):
        settings = context.scene.airport_osm_settings

        if event.type == 'ESC':
            self._finish(context)
            settings.status_text = "Cancelled"
            return {"CANCELLED"}

        if event.type != 'TIMER':
            return {"PASS_THROUGH"}

        if self._phase in ("resolving", "fetching_detail"):
            try:
                status, payload = self._result_queue.get_nowait()
            except queue.Empty:
                return {"RUNNING_MODAL"}

            if status == "error":
                self._finish(context)
                settings.status_text = f"Failed: {payload}"
                self.report({"ERROR"}, payload)
                return {"CANCELLED"}

            if status == "resolved":
                lat, lon = payload
                self._origin_lat, self._origin_lon = lat, lon
                self._phase = "fetching_detail"
                settings.status_text = "Fetching airport features..."
                self._thread = threading.Thread(
                    target=self._detail_worker, args=(lat, lon, settings.radius), daemon=True
                )
                self._thread.start()
                return {"RUNNING_MODAL"}

            if status == "detail":
                osm = OSMData(payload.get("elements", []))
                if not osm.ways:
                    self._finish(context)
                    settings.status_text = "No aeroway/building features found in range"
                    self.report({"WARNING"}, settings.status_text)
                    return {"CANCELLED"}

                self._tasks = build_task_list(osm, self._origin_lat, self._origin_lon, self._scale)
                self._cols = prepare_collections()
                self._mats = (
                    get_or_create_material("Runway_Asphalt", (0.05, 0.05, 0.055)),
                    get_or_create_material("Taxiway_Asphalt", (0.08, 0.08, 0.09)),
                    get_or_create_material("Apron_Concrete", (0.55, 0.54, 0.5)),
                    get_or_create_material("Building_Generic", (0.6, 0.6, 0.62)),
                )
                self._task_index = 0
                self._phase = "building"
                settings.status_text = f"Building {len(self._tasks)} features..."
                return {"RUNNING_MODAL"}

            return {"RUNNING_MODAL"}

        if self._phase == "building":
            total = len(self._tasks)
            if total == 0:
                self._finish(context)
                settings.status_text = "No buildable features found"
                self.report({"WARNING"}, settings.status_text)
                return {"CANCELLED"}

            end = min(self._task_index + self.TASKS_PER_TICK, total)
            for i in range(self._task_index, end):
                kind = execute_task(self._tasks[i], self._cols, self._mats, self._scale)
                if kind:
                    self._counts[kind] += 1
            self._task_index = end
            settings.progress = self._task_index / total

            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

            if self._task_index >= total:
                self._finish(context)
                c = self._counts
                settings.status_text = (
                    f"Done: {c['runway']} runways, {c['taxiway']} taxiways, "
                    f"{c['apron']} apron pieces, {c['building']} buildings"
                )
                self.report({"INFO"}, settings.status_text)
                return {"FINISHED"}

            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}

    def _finish(self, context):
        settings = context.scene.airport_osm_settings
        settings.is_running = False
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None


class AIRPORTOSM_PT_panel(Panel):
    bl_label = "Airport OSM Importer"
    bl_idname = "AIRPORTOSM_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Airport OSM"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.airport_osm_settings
        wm = context.window_manager

        db_count = len(_AIRPORT_DB)
        box = layout.box()
        if db_count:
            last_updated = database_last_updated_str()
            box.label(text=f"Database: {db_count} airports", icon="CHECKMARK")
            if last_updated:
                box.label(text=f"Updated: {last_updated}")
        else:
            box.label(text="No offline database yet", icon="ERROR")
        box.operator(
            "airportosm.download_db",
            icon="IMPORT",
            text="Update Airport Database" if db_count else "Download Airport Database",
        )

        layout.separator()
        layout.label(text="Airport:")
        layout.prop_search(settings, "search_term", wm, "airport_osm_items", text="", icon="VIEWZOOM")
        layout.label(text="Type a name, ICAO or IATA code to search", icon="INFO")

        layout.prop(settings, "radius")
        layout.prop(settings, "scale")

        layout.separator()
        row = layout.row()
        row.enabled = not settings.is_running
        row.operator("airportosm.generate", icon="MESH_GRID")

        if settings.is_running:
            layout.prop(settings, "progress", text="Progress", slider=True)
        if settings.status_text:
            layout.label(text=settings.status_text)

        layout.separator()
        box = layout.box()
        box.label(text="Output collections:")
        box.label(text="• Runways")
        box.label(text="• Taxiways")
        box.label(text="• Aprons_Tarmac")
        box.label(text="• Buildings")


classes = (
    AirportSearchItem,
    AirportOSMSettings,
    AIRPORTOSM_OT_download_db,
    AIRPORTOSM_OT_generate,
    AIRPORTOSM_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.airport_osm_settings = PointerProperty(type=AirportOSMSettings)
    bpy.types.WindowManager.airport_osm_items = CollectionProperty(type=AirportSearchItem)

    count = load_airport_database()
    if count:
        try:
            refresh_search_collection(bpy.context)
        except Exception:
            pass


def unregister():
    del bpy.types.WindowManager.airport_osm_items
    del bpy.types.Scene.airport_osm_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()