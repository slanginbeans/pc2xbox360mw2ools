"""Drag-and-drop texture replacer for MW2 Xbox 360 fastfiles, running in your web browser.

Put this file and mw2tex.py in the folder with your .ff files (and imagefile .pak files), then run:
    python mw2tex_gui.py
Your browser opens the texture picker. Tables at the top opens the table editor (mw2zone_gui.py;
keep mw2zone.py and mw2zone_gui.py in the same folder). Pick a fastfile, drop pictures onto textures (or drop
many pictures at once, named after the textures), then press Build. The rebuilt files go in
the mw2tex_out folder; copy them to _codxe\\zone\\ on the console.

Needs Python 3 and Pillow (python -m pip install pillow). Nothing is sent anywhere: the page
talks only to this program on your own PC.
"""
import glob
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mw2tex  # noqa: E402
import mw2zone_gui  # noqa: E402

if not hasattr(mw2tex, "flipbook_options"):
    sys.exit("mw2tex.py at %s is older than mw2tex_gui.py. Copy mw2tex.py, mw2tex_gui.py, mw2zone.py and "
             "mw2zone_gui.py from the same download into one folder." % mw2tex.__file__)

try:
    from PIL import Image
except ImportError:
    sys.exit("The texture picker needs Pillow. Install it with:  python -m pip install pillow")

FOLDER = os.getcwd()
OUT_DIR = os.path.join(FOLDER, "mw2tex_out")
WORK_DIR = tempfile.mkdtemp(prefix="mw2tex_gui_")
PORT = 8360
MAX_UPLOAD = 64 * 1024 * 1024

lock = threading.Lock()
state = {"ff_path": None, "ff": None, "images": {}, "pending": {}, "thumbs": {}, "gray": set(),
         "materials": {}, "image_material": {}, "assigns": [],
         "maps": {}}  # all-maps view: texture name -> {map file name: that map's texture record}
ALL_MAPS = "*maps"  # the file picker's "All maps" entry
# Pictures put on map textures, kept between runs (outside mw2tex_out, which goes to the console).
MAP_CHANGES = os.path.join(FOLDER, "mw2tex_map_changes")
ff_cache = {}  # map file name -> FastFile, the last two opened for previews
# Emblems and titles: their pictures are listed in tables (mw2zone_gui.PICTURE_COLUMNS).
CARD_PREFIXES = ("cardtitle_", "cardicon_")

# Categories for finding textures, by words in their names. The first match wins, so the more
# specific ones come first. "Detail maps" are the bump, shine and lighting textures that go with
# the visible ones; they're hidden unless picked.
CATEGORIES = [
    ("Titles", ("cardtitle_",)),
    ("Emblems", ("cardicon_",)),
    ("Camos", ()),  # names starting weapon_camo, unlock_camo or camo; see category()
    ("Menu & load screens", ()),  # load screens and menu backgrounds; see category()
    ("Detail maps", ()),
    ("Skyboxes", ("sky",)),
    ("Graffiti", ("graffiti", "graff")),
    ("Signs & posters", ("sign", "poster", "flyer", "billboard", "banner", "advert", "logo", "flag", "menu_board")),
    ("Boxes & crates", ("box", "crate", "container", "cargo", "pallet", "barrel", "drum", "dumpster", "carton")),
    ("Books & paper", ("book", "paper", "magazine", "newspaper", "document", "folder", "notebook", "poster")),
    ("Characters", ("ally_", "tf141", "tf_141", "seal_", "militia", "airborne", "russian_", "us_army", "riot_",
                    "opforce", "_head", "head_", "body", "glove", "hands", "headgear", "loadout", "ghillie", "face")),
    ("Weapons", ("weapon", "gun", "rifle", "ammo", "knife", "grenade", "scope", "pistol", "rpg", "javelin")),
    ("Vehicles", ("vehicle", "car_", "_car", "policecar", "taxi", "van_", "truck", "bus_", "heli", "tank",
                  "bmp", "humvee", "hatchback", "sedan", "snowcat", "snowmobile", "boat", "plane", "jet",
                  "bike", "tire", "wheel", "pickup", "suburban", "uaz", "stryker")),
    ("Plants & trees", ("foliage", "tree_", "_tree", "trees", "bush", "grass", "plant", "leaf", "leaves", "ivy", "vine", "flower",
                        "hedge", "palm", "fern", "moss", "weed")),
    ("Water & effects", ("water", "fx_", "_fx", "smoke", "fire", "explosion", "glass", "blood", "spark",
                         "puddle", "rain", "snowflake", "burnt")),
    ("Decals", ("decal", "stain", "crack")),
    ("Furniture & props", ("furniture", "table", "chair", "couch", "sofa", "bed", "cabinet", "shelf", "desk",
                           "rug", "carpet", "curtain", "pillow", "lamp", "light", "tv_", "monitor", "computer",
                           "laptop", "phone", "kitchen", "sink", "toilet", "food", "soda", "can_", "bottle",
                           "trash", "bag", "tool", "machinery", "electric", "fan", "clock", "pot", "tarp",
                           "cloth", "shower", "fridge", "atm", "vending")),
    ("Walls & buildings", ("wall", "brick", "plaster", "concrete", "facade", "roof", "trim", "window", "door",
                           "tile", "ceiling", "stair", "cinderblock", "building", "house", "fence", "gate",
                           "pillar", "column", "awning", "garage", "shutter", "balcony")),
    ("Ground", ("ground_", "_ground", "dirt", "mud", "sand", "rock", "gravel", "terrain", "snow", "asphalt",
                "road", "sidewalk", "curb", "floor", "street")),
    ("Wood & metal", ("wood", "metal", "mtl", "steel", "rust", "pipe", "beam", "iron", "tin", "plank",
                      "plastic", "rubber")),
]
DETAIL_ENDINGS = ("_nml", "_n", "_norm", "_nrm", "_normal", "bump", "_spc", "_spec", "_s", "_cos", "_gls",
                  "_dtl", "_detail", "_d", "_occ", "_ao", "_mask")


def category(name):
    lower = name.lower()
    if lower.startswith(CARD_PREFIXES):
        return "Titles" if lower.startswith("cardtitle_") else "Emblems"
    if lower.startswith(("weapon_camo", "unlock_camo", "camo")):
        return "Camos"
    if lower.startswith(("loadscreen_", "menu_background", "menu_mp_image", "menu_sp_image", "menu_co_image",
                         "bg_blur")):
        return "Menu & load screens"
    if lower[:1] in "~*#$" or lower.endswith(DETAIL_ENDINGS) or "_spc" in lower or "_spec" in lower \
            or "_nml" in lower or "_nrml" in lower or "reflection_probe" in lower or "lightmap" in lower:
        return "Detail maps"
    for label, words in CATEGORIES:
        if any(w in lower for w in words):
            return label
    return "Other"


def map_label(path):
    """mp_favela.ff -> favela"""
    base = os.path.splitext(os.path.basename(path))[0]
    return base[3:] if base.lower().startswith("mp_") else base


def is_map(path):
    """mp_favela.ff is a map; mp_favela_load.ff only holds its load screen."""
    base = os.path.basename(path).lower()
    return base.startswith("mp_") and not base.endswith("_load.ff")


def map_files():
    return sorted((p for p in glob.glob(os.path.join(FOLDER, "mp_*.ff")) if is_map(p)),
                  key=lambda p: os.path.basename(p).lower())


def fastfile_for(image):
    """The FastFile an image record belongs to: the open file, or (all-maps view) its first map."""
    if state["ff"] is not None:
        return state["ff"], image
    maps = state["maps"].get(image["name"])
    if not maps:
        return None, image
    map_name = sorted(maps)[0]
    if map_name not in ff_cache:
        while len(ff_cache) >= 2:
            ff_cache.pop(next(iter(ff_cache)))
        ff_cache[map_name] = mw2tex.FastFile(os.path.join(FOLDER, map_name))
    return ff_cache[map_name], maps[map_name]
NOT_SPARE = {"cardtitle_locked", "cardicon_locked", "cardtitle_248x48"}


def animate_options(image):
    """Frame counts a menu picture stored in the .ff can play (see mw2tex.flipbook_options);
    empty for pak textures, which the game can't animate."""
    if image["pak"] or state["ff"] is None:
        return []
    return [{"frames": o["frames"], "frame": "%dx%d" % o["frame"], "sheet": "%dx%d" % o["sheet"],
             "mb": round(o["bytes"] / 1048576.0, 2)} for o in mw2tex.flipbook_options(state["ff"], image)]


def default_frames(image, frames):
    """The smallest frame count that shows every frame of the animation, else the most there is."""
    counts = [o["frames"] for o in animate_options(image)] if frames > 1 else []
    return next((c for c in counts if c >= frames), counts[-1] if counts else 0)


def table_uses():
    with mw2zone_gui.lock:
        return mw2zone_gui.picture_uses() if mw2zone_gui.state["ff_path"] else None


def is_spare(material, uses):
    return (material is not None and material.lower().startswith(CARD_PREFIXES)
            and material.lower() not in NOT_SPARE and not uses.get(material.lower()))


def texture_list():
    rows = []
    uses = table_uses()
    assigned = live_assigns() if state["assigns"] else []
    for name, image in state["images"].items():
        largest = max(image["levels"], key=lambda l: l["width"] * l["height"])
        material = state["image_material"].get(name.lower())
        used = (uses or {}).get(material.lower(), []) if material else []
        maps = state["maps"].get(name)
        rows.append({
            "category": category(name),
            "maps": [map_label(m) for m in sorted(maps)] if maps else [],
            "assigned": [a for a in assigned if material and a["new"].lower() == material.lower()],
            "material": material,
            "uses": len(used),
            "used_by": [u["id"] for u in used[:6]],
            "spare": uses is not None and is_spare(material, uses),
            "name": name,
            "format": mw2tex.FORMATS.get(image["format"], ("unsupported",))[0],
            "supported": mw2tex._supported(image),
            "stored": "pak" if image["pak"] else "ff",
            "width": largest["width"],
            "height": largest["height"],
            "animate_options": animate_options(image),
            "gray": name in state["gray"],
        })
    rows.sort(key=lambda r: r["name"])
    return rows


def open_fastfile(filename):
    path = os.path.join(FOLDER, os.path.basename(filename))
    if not os.path.exists(path):
        raise ValueError("%s is not in %s" % (filename, FOLDER))
    ff = mw2tex.FastFile(path)
    images = {}
    for image in ff.images:
        images.setdefault(image["name"], image)
    gray = set()
    for name, image in images.items():
        if image["format"] == 0x12 and not image["pak"]:
            try:
                if mw2tex.gray_alpha_packed(image, mw2tex.decode_texture(ff, image, unpack=False)):
                    gray.add(name)
            except Exception:
                pass
    for item in state["pending"].values():
        undo_redirect(item)  # the queued pictures are dropped, so their table changes go too
    materials = mw2tex.material_images(ff)
    ff_cache.clear()
    state.update(ff_path=path, ff=ff, images=images, pending={}, thumbs={}, gray=gray, materials=materials,
                 image_material={v.lower(): k for k, v in materials.items()}, maps={})
    if is_map(path):
        state["pending"] = load_map_changes(os.path.basename(path))
    with mw2zone_gui.lock:
        mw2zone_gui.ensure_open()  # code_post_gfx_mp.ff, for which titles and emblems use each picture
    return texture_list()


def open_all_maps():
    """Every mp_*.ff in the folder as one list. A texture several maps carry shows once and
    lists its maps; a picture put on it goes into all of them."""
    paths = map_files()
    if not paths:
        raise ValueError("there are no mp_*.ff map files in %s" % FOLDER)
    for item in state["pending"].values():
        undo_redirect(item)
    images, maps = {}, {}
    for path in paths:
        ff = mw2tex.FastFile(path)  # read one at a time: each map unpacks to about 90 MB
        seen = set()
        for image in ff.images:
            if image["name"].startswith("#") or image["name"].lower() in seen:
                continue  # "#123" = a pak texture with no name
            seen.add(image["name"].lower())
            key = images.get(image["name"].lower(), image)["name"]
            images.setdefault(key.lower(), image)
            maps.setdefault(key, {})[os.path.basename(path)] = image
        del ff
    ff_cache.clear()
    state.update(ff_path=ALL_MAPS, ff=None, images={i["name"]: i for i in images.values()}, pending={},
                 thumbs={}, gray=set(), materials={}, image_material={}, maps=maps)
    state["pending"] = load_map_changes(None)
    return texture_list()


# ---------------------------------------------------------------- map changes
# Pictures put on map textures are saved in mw2tex_map_changes, so every Build (from the maps
# or from ui_mp.ff) rebuilds the maps from the stock files with all of them, plus the title and
# emblem changes from ui_mp.ff.


def _changes_file():
    return os.path.join(MAP_CHANGES, "changes.json")


def read_map_changes():
    try:
        with open(_changes_file()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def load_map_changes(map_name):
    """Queued pictures from earlier builds: for one map, or (None) for the all-maps view."""
    pending = {}
    for entry in read_map_changes():
        if map_name and map_name not in entry["maps"]:
            continue
        path = os.path.join(MAP_CHANGES, entry["file"])
        if entry["name"] in state["images"] and os.path.exists(path):
            pending[entry["name"]] = {"path": path, "source": entry["source"],
                                      "frames": 1, "animate": 0, "saved": True}
    return pending


def save_map_changes():
    """Writes the queued map pictures to mw2tex_map_changes, replacing what was saved for
    the open map (or for every map, in the all-maps view)."""
    open_map = None if state["ff_path"] == ALL_MAPS else os.path.basename(state["ff_path"])
    entries = []
    for entry in read_map_changes():
        if open_map is None:
            continue
        entry["maps"] = [m for m in entry["maps"] if m != open_map]
        if entry["maps"]:
            entries.append(entry)
    os.makedirs(MAP_CHANGES, exist_ok=True)
    for name, item in sorted(state["pending"].items()):
        data = open(item["path"], "rb").read()
        file_name = hashlib.sha1(data).hexdigest()[:16] + os.path.splitext(item["path"])[1].lower()
        target = os.path.join(MAP_CHANGES, file_name)
        if not os.path.exists(target):
            with open(target, "wb") as fh:
                fh.write(data)
        maps = sorted(state["maps"].get(name, {})) if open_map is None else [open_map]
        entries.append({"name": name, "file": file_name, "source": item["source"], "maps": maps})
    with open(_changes_file(), "w") as fh:
        json.dump(entries, fh, indent=1)
    used = {e["file"] for e in entries} | {"changes.json"}
    for file_name in os.listdir(MAP_CHANGES):
        if file_name not in used:
            os.remove(os.path.join(MAP_CHANGES, file_name))


def rebuild_maps(log):
    """Rebuilds every map in the folder from the stock file: saved map pictures first, then the
    title/emblem changes from the rebuilt ui_mp.ff (and any other rebuilt non-map file)."""
    maps = map_files()
    written = []
    for path in maps:
        old = os.path.join(OUT_DIR, os.path.basename(path))
        if os.path.exists(old):
            os.remove(old)
    by_map = {}
    for entry in read_map_changes():
        for map_name in entry["maps"]:
            by_map.setdefault(map_name, []).append(entry)
    for path in maps:
        entries = by_map.get(os.path.basename(path))
        if not entries:
            continue
        out = mw2tex.Output(path, OUT_DIR)
        for entry in entries:
            image = out.ff.find(entry["name"])
            if image is not None and out.put(image, os.path.join(MAP_CHANGES, entry["file"]), convert=True):
                pass
            else:
                log.append("%s: %s skipped (not in this map or format not supported)" % (map_label(path), entry["name"]))
        if out.replaced:
            out.save()
            written.append(out.ff_out)
            if out.pak_changed:
                written.append(out.pak_path)
            log.append("%s: %d picture%s" % (os.path.basename(path), out.replaced, "" if out.replaced == 1 else "s"))
    for source in sorted(glob.glob(os.path.join(OUT_DIR, "*.ff"))):
        base = os.path.basename(source)
        stock = os.path.join(FOLDER, base)
        if base.lower().startswith("mp_") or base.lower() == mw2zone_gui.OUT_NAME or not os.path.exists(stock):
            continue
        written += mw2tex.sync_maps(stock, source, maps, OUT_DIR, log.append)
    return written


def share_plan(name):
    """For a texture whose picture several table rows share: those rows, and the unused pictures of
    the same kind one of them could be moved to, best match first."""
    image = state["images"].get(name)
    material = state["image_material"].get(name.lower())
    uses = table_uses()
    if image is None or material is None or uses is None:
        return {"material": material, "uses": [], "spares": []}
    size = (image["levels"][0]["width"], image["levels"][0]["height"])
    kind = "cardtitle_" if material.lower().startswith("cardtitle_") else "cardicon_"
    spares = []
    for spare, spare_image in sorted(state["materials"].items()):
        target = state["images"].get(spare_image)
        if (not spare.lower().startswith(kind) or not is_spare(spare, uses) or target is None
                or not mw2tex._supported(target) or spare_image in state["pending"]):
            continue
        spare_size = (target["levels"][0]["width"], target["levels"][0]["height"])
        spares.append({"material": spare, "image": spare_image, "same_size": spare_size == size,
                       "width": spare_size[0], "height": spare_size[1]})
    spares.sort(key=lambda s: not s["same_size"])  # same size first: nothing gets squashed
    others = [{"material": m, "image": i, "uses": len(uses.get(m.lower(), []))}
              for m, i in sorted(state["materials"].items())
              if m.lower().startswith(kind) and m.lower() not in NOT_SPARE and m != material
              and uses.get(m.lower())]
    return {"material": material, "uses": uses.get(material.lower(), []), "spares": spares, "others": others}


def picture_rows(kind):
    """Every title (kind cardtitle_) or emblem (cardicon_) row, with the picture it uses now."""
    rows = []
    for material, used in (table_uses() or {}).items():
        if material.startswith(kind):
            rows += [dict(u, material=material) for u in used]
    return sorted(rows, key=lambda r: r["id"])


def assign(material, targets):
    """Points table rows TARGETS ([{"table", "row"}]) at picture MATERIAL, an existing picture
    (usually an unused one). Nothing about the textures changes."""
    lower = {m.lower(): m for m in state["materials"]}
    if material.lower() not in lower:
        raise ValueError("there's no picture named %s in the open file" % material)
    material = lower[material.lower()]
    done = []
    with mw2zone_gui.lock:
        for target in targets:
            name, row = target["table"], int(target["row"])
            column = mw2zone_gui.PICTURE_COLUMNS.get(name)
            rows = mw2zone_gui.current_rows(name)
            if column is None or rows is None or not 0 <= row < len(rows):
                raise ValueError("no row %s in %s" % (row, name))
            old = rows[row][column]
            if old == material:
                continue
            mw2zone_gui.set_cell(name, row, column, material)
            entry = {"table": name, "row": row, "column": column, "id": rows[row][0], "old": old, "new": material}
            state["assigns"].append(entry)
            done.append(entry)
    return done


def live_assigns():
    """Assignments still in effect (the table editor may have undone some)."""
    live = []
    with mw2zone_gui.lock:
        for index, a in enumerate(state["assigns"]):
            rows = mw2zone_gui.current_rows(a["table"])
            if rows and a["row"] < len(rows) and rows[a["row"]][a["column"]] == a["new"]:
                live.append(dict(a, index=index))
    return live


def unassign(index):
    a = state["assigns"][index]
    with mw2zone_gui.lock:
        rows = mw2zone_gui.current_rows(a["table"])
        if rows and rows[a["row"]][a["column"]] == a["new"]:
            mw2zone_gui.set_cell(a["table"], a["row"], a["column"], a["old"])


MAX_FETCH = MAX_UPLOAD


def fetch_picture(url):
    """Downloads a picture from a web link for the page, which can't fetch other sites itself.
    Returns (bytes, file name)."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("paste a link that starts with http:// or https://")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (mw2tex texture picker)"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_FETCH + 1)
    except Exception as e:
        raise ValueError("couldn't download that link: %s" % e)
    if len(data) > MAX_FETCH:
        raise ValueError("that picture is too big")
    try:
        picture = Image.open(io.BytesIO(data))
        kind = (picture.format or "").lower()
    except Exception:
        raise ValueError("that link isn't a picture. Right-click the picture and use 'Copy image address'.")
    stem = os.path.splitext(os.path.basename(urllib.parse.urlparse(url).path))[0] or "web_picture"
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:60] or "web_picture"
    ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp", "bmp": ".bmp", "dds": ".dds",
           "tga": ".tga"}.get(kind)
    if ext is None:  # a format the tool can't read directly: hand it over as PNG
        buf = io.BytesIO()
        picture.convert("RGBA").save(buf, "PNG")
        data, ext = buf.getvalue(), ".png"
    return data, stem + ext


def upload_for_one(name, index, spare, filename, data):
    """Puts the picture on SPARE's texture and points only row INDEX of NAME's users at SPARE,
    so every other title or emblem keeps the original picture."""
    plan = share_plan(name)
    if not 0 <= index < len(plan["uses"]):
        raise ValueError("pick which title or emblem should get the new picture")
    choice = [s for s in plan["spares"] if s["material"] == spare]
    if not choice:
        raise ValueError("%s isn't free any more; pick another spare picture" % spare)
    use = plan["uses"][index]
    item = save_upload(choice[0]["image"], filename, data)
    with mw2zone_gui.lock:
        rows = mw2zone_gui.current_rows(use["table"])
        old = rows[use["row"]][use["column"]]
        mw2zone_gui.set_cell(use["table"], use["row"], use["column"], spare)
    item["redirect"] = dict(use, old=old, new=spare, shared=plan["material"])
    return item


def undo_redirect(item):
    """Points the row back at its original picture when a spare-picture upload is removed."""
    r = item.get("redirect")
    if not r:
        return
    with mw2zone_gui.lock:
        rows = mw2zone_gui.current_rows(r["table"])
        if rows and r["row"] < len(rows) and rows[r["row"]][r["column"]] == r["new"]:
            mw2zone_gui.set_cell(r["table"], r["row"], r["column"], r["old"])


def thumbnail(name):
    if name in state["thumbs"]:
        return state["thumbs"][name]
    image = state["images"].get(name)
    picture = None
    if image is not None:
        try:
            ff, image = fastfile_for(image)
            picture = mw2tex.decode_texture(ff, image, FOLDER, max_side=256) if ff else None
        except Exception:
            picture = None
    if picture is None:
        return None
    buf = io.BytesIO()
    picture.save(buf, "PNG")
    state["thumbs"][name] = buf.getvalue()
    return state["thumbs"][name]


def save_upload(name, filename, data):
    if name not in state["images"]:
        raise ValueError("no texture named %s" % name)
    ext = os.path.splitext(filename)[1].lower() or ".png"
    if ext not in mw2tex.PICTURE_TYPES:
        raise ValueError("%s isn't a picture type the tool can read" % filename)
    path = os.path.join(WORK_DIR, mw2tex._safe(name) + ext)
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        Image.open(path).verify()
    except Exception:
        os.remove(path)
        raise ValueError("%s couldn't be opened as a picture" % filename)
    frames = mw2tex.frame_count(path)
    state["pending"][name] = {"path": path, "source": filename, "frames": frames,
                              "animate": default_frames(state["images"][name], frames)}
    return state["pending"][name]


def build():
    with mw2zone_gui.lock:
        table_edits = bool(mw2zone_gui.state["edits"])
    on_map = state["ff_path"] == ALL_MAPS or is_map(state["ff_path"] or "")
    if not state["pending"] and not table_edits and not (on_map and read_map_changes()):
        raise ValueError("nothing to build yet: drop a picture on a texture or change a table first")
    if on_map:
        result = build_maps()
    else:
        result = build_textures() if state["pending"] else {"log": [], "written": [], "folder": OUT_DIR}
    if table_edits:
        with mw2zone_gui.lock:
            tables = mw2zone_gui.build()
        result["log"] += tables["log"]
        result["written"] += tables["written"]
    return result


def build_textures():
    log = []
    os.makedirs(OUT_DIR, exist_ok=True)
    ff_out = os.path.join(OUT_DIR, os.path.basename(state["ff_path"]))
    if os.path.exists(ff_out):
        os.remove(ff_out)  # always rebuild from the stock file, so the pending list is the whole change
    out = mw2tex.Output(state["ff_path"], OUT_DIR)
    for name, item in sorted(state["pending"].items()):
        image = out.ff.find(name)
        if item["animate"] and animate_options(state["images"][name]):
            option = mw2tex.put_flipbook(out, image, item["path"], WORK_DIR, item["animate"])
            log.append("%s: %s as %d frames of %dx%d" % ((name, item["source"], option["frames"]) + option["frame"])
                       if option else "%s: skipped (format not supported)" % name)
        else:
            ok = out.put(image, item["path"], convert=True)
            log.append("%s: %s" % (name, item["source"]) if ok else "%s: skipped (format not supported)" % name)
        if item.get("redirect"):
            r = item["redirect"]
            log.append("  %s now shows %s instead of %s" % (r["id"], r["new"], r["old"]))
    out.save()
    written = [os.path.relpath(ff_out, FOLDER)]
    if out.pak_changed:
        written.append(os.path.relpath(out.pak_path, FOLDER))
    # In a match, emblems and titles come from each map's own copy, so copy the changes into
    # every map file in the folder too (along with pictures put on the maps themselves).
    if map_files():
        for path in rebuild_maps(log):
            rel = os.path.relpath(path, FOLDER)
            if rel not in written:
                written.append(rel)
    else:
        log.append("No mp_*.ff map files in this folder, so emblems and titles only change in menus. "
                   "Copy your maps here and build again to see them in matches and killcams.")
    return {"log": log, "written": written, "folder": OUT_DIR}


def build_maps():
    """All-maps view, or one map open: save the queued pictures, then rebuild the maps."""
    save_map_changes()
    for name, item in state["pending"].items():
        item["saved"] = True
    log = []
    written = [os.path.relpath(p, FOLDER) for p in dict.fromkeys(rebuild_maps(log))]
    if not written:
        log.append("No map pictures queued, so the map files were left out.")
    return {"log": log, "written": written, "folder": OUT_DIR}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, message):
        self.reply(400, {"error": message})

    def tables(self, method, url, query, body=b""):
        """Hands /tables addresses to the table editor. Returns True when it answered."""
        with mw2zone_gui.lock:
            result = mw2zone_gui.handle(method, url.path, query, body)
        if result is None:
            return False
        code, reply_body, kind = result
        self.reply(code, reply_body, kind or "application/json")
        return True

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if self.tables("GET", url, query):
            return
        if url.path == "/api/fetch":  # outside the lock: a slow download mustn't freeze the page
            try:
                data, filename = fetch_picture(query.get("url", [""])[0])
            except ValueError as e:
                return self.fail(str(e))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Filename", filename)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        with lock:
            if url.path == "/":
                self.reply(200, PAGE, "text/html; charset=utf-8")
            elif url.path == "/api/files":
                files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FOLDER, "*.ff")))
                paks = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FOLDER, "imagefile*.pak")))
                self.reply(200, {"folder": FOLDER, "files": files, "paks": paks, "maps": len(map_files()),
                                 "open": os.path.basename(state["ff_path"]) if state["ff_path"] else None})
            elif url.path == "/api/textures":
                self.reply(200, {"textures": texture_list(), "pending": self.pending(), "tables": self.table_state()})
            elif url.path == "/api/plan":
                self.reply(200, share_plan(query.get("name", [""])[0]))
            elif url.path == "/api/rows":
                self.reply(200, {"rows": picture_rows(query.get("kind", ["cardtitle_"])[0])})
            elif url.path == "/api/materials":
                if state["ff_path"] is None and os.path.exists(os.path.join(FOLDER, "ui_mp.ff")):
                    try:
                        open_fastfile("ui_mp.ff")
                    except Exception:
                        pass
                self.reply(200, {"file": os.path.basename(state["ff_path"]) if state["ff_path"] else None,
                                 "materials": state["materials"],
                                 "pending": {n: p["source"] for n, p in state["pending"].items()}})
            elif url.path == "/api/matthumb":
                image = state["materials"].get(query.get("m", [""])[0])
                if image is None:
                    lower = {k.lower(): v for k, v in state["materials"].items()}
                    image = lower.get(query.get("m", [""])[0].lower())
                png = thumbnail(image) if image else None
                if png is None:
                    self.reply(404, b"", "image/png")
                else:
                    self.reply(200, png, "image/png")
            elif url.path == "/api/thumb":
                png = thumbnail(query.get("name", [""])[0])
                if png is None:
                    self.reply(404, b"", "image/png")
                else:
                    self.reply(200, png, "image/png")
            elif url.path == "/api/upload":
                name = query.get("name", [""])[0]
                item = state["pending"].get(name)
                if not item:
                    self.reply(404, b"", "image/png")
                elif item["path"].lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    self.reply(200, open(item["path"], "rb").read(), "application/octet-stream")
                else:  # DDS, TGA: browsers can't show these, so send a PNG copy
                    buf = io.BytesIO()
                    Image.open(item["path"]).convert("RGBA").save(buf, "PNG")
                    self.reply(200, buf.getvalue(), "image/png")
            elif url.path == "/favicon.ico":
                self.reply(204, b"", "image/x-icon")
            else:
                self.reply(404, {"error": "not found"})

    def pending(self):
        return {n: {"source": p["source"], "frames": p["frames"], "animate": p["animate"],
                    "redirect": p.get("redirect")}
                for n, p in state["pending"].items()}

    def table_state(self):
        with mw2zone_gui.lock:
            z = mw2zone_gui.state
            return {"file": os.path.basename(z["ff_path"]) if z["ff_path"] else None, "changed": sorted(z["edits"])}

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.fail("that file is too big")
        body = self.rfile.read(length)
        if self.tables("POST", url, query, body):
            return
        with lock:
            try:
                if url.path == "/api/open":
                    file_name = json.loads(body)["file"]
                    textures = open_all_maps() if file_name == ALL_MAPS else open_fastfile(file_name)
                    self.reply(200, {"textures": textures, "pending": self.pending()})
                elif url.path == "/api/upload":
                    name = query.get("name", [""])[0]
                    filename = query.get("filename", ["picture.png"])[0]
                    if "only" in query:
                        item = upload_for_one(name, int(query["only"][0]), query.get("spare", [""])[0], filename, body)
                        self.reply(200, {"pending": self.pending(), "textures": texture_list(),
                                         "tables": self.table_state(), "redirect": item["redirect"]})
                    else:
                        old = state["pending"].get(name)
                        save_upload(name, filename, body)
                        if old and old.get("redirect"):  # a new picture for the same spare keeps its row
                            state["pending"][name]["redirect"] = old["redirect"]
                        self.reply(200, {"pending": self.pending()})
                elif url.path == "/api/option":
                    req = json.loads(body)
                    if req["name"] in state["pending"]:
                        state["pending"][req["name"]]["animate"] = max(0, int(req["animate"]))
                    self.reply(200, {"pending": self.pending()})
                elif url.path == "/api/remove":
                    req = json.loads(body)
                    names = list(state["pending"]) if req.get("all") else [req["name"]]
                    for name in names:
                        item = state["pending"].pop(name, None)
                        if item:
                            undo_redirect(item)
                    self.reply(200, {"pending": self.pending(), "textures": texture_list(), "tables": self.table_state()})
                elif url.path == "/api/assign":
                    req = json.loads(body)
                    done = assign(req["material"], req["targets"])
                    self.reply(200, {"done": done, "textures": texture_list(), "tables": self.table_state()})
                elif url.path == "/api/unassign":
                    unassign(int(json.loads(body)["index"]))
                    self.reply(200, {"textures": texture_list(), "tables": self.table_state()})
                elif url.path == "/api/build":
                    self.reply(200, build())
                else:
                    self.reply(404, {"error": "not found"})
            except SystemExit as e:
                self.fail(str(e))
            except (ValueError, KeyError) as e:
                self.fail(str(e))
            except Exception as e:  # keep the page usable and show what broke
                self.fail("%s: %s" % (type(e).__name__, e))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mw2tex texture picker</title>
<style>
:root{--bg:#17191d;--panel:#22252b;--card:#2a2e35;--line:#3a3f48;--text:#e6e8ec;--dim:#9aa1ad;--accent:#d6aa46;--ok:#5cb87a;--bad:#e06c6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}select,input,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#1b1b1b;border-color:var(--accent);font-weight:600}button:disabled{opacity:.5;cursor:default}
.chips{display:flex;gap:6px;flex-wrap:wrap;flex-basis:100%}.chip small{opacity:.7;margin-left:4px}.chip.flag{border-style:dashed}.chip{padding:4px 10px;border-radius:999px}.chip.on{background:var(--accent);color:#1b1b1b;border-color:var(--accent)}
a.tab{color:var(--dim);text-decoration:none;padding:6px 4px}a.tab.on{color:var(--text);font-weight:600;border-bottom:2px solid var(--accent)}
#search{min-width:220px;flex:1}.info{color:var(--dim);font-size:12px;padding:8px 16px}
main{padding:0 16px 40px;display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px;position:relative}
.card.drag{outline:2px dashed var(--accent)}.card.changed{border-color:var(--ok)}
.pics{display:flex;gap:6px;align-items:center;justify-content:center;height:120px;border-radius:6px;background:repeating-conic-gradient(#30343b 0 25%,#383c44 0 50%) 0 0/16px 16px}
.pics img{max-width:100%;max-height:120px;image-rendering:pixelated;object-fit:contain}.pics.two img{max-width:47%}
.arrow{color:var(--dim)}.name{font-weight:600;word-break:break-all;font-size:13px}.meta{color:var(--dim);font-size:12px}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:12px}.row button{padding:3px 8px;font-size:12px}
.badge{position:absolute;top:6px;right:6px;background:var(--ok);color:#10210f;font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px}
.none{color:var(--dim);font-size:12px}
#drop{position:fixed;inset:0;background:rgba(214,170,70,.12);border:4px dashed var(--accent);display:none;align-items:center;justify-content:center;font-size:22px;z-index:10;pointer-events:none}
#toast{position:fixed;left:16px;right:16px;bottom:16px;max-width:760px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:none;z-index:20;white-space:pre-wrap}
#toast.bad{border-color:var(--bad)}#toast.ok{border-color:var(--ok)}
.empty{grid-column:1/-1;color:var(--dim);padding:40px 0;text-align:center}
.uses{font-size:12px;color:var(--dim)}.uses b{color:var(--text);font-weight:600}.uses.free{color:var(--ok)}.uses.shared{color:var(--accent)}
.redir{font-size:12px;color:var(--ok)}.redir button{margin-left:6px;padding:1px 6px;font-size:11px}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:16px;max-width:560px;width:calc(100% - 32px)}
dialog::backdrop{background:rgba(0,0,0,.6)}dialog h2{font-size:16px;margin:0 0 8px}dialog p{margin:6px 0;color:var(--dim)}
dialog label.opt{display:block;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0;cursor:pointer}
dialog label.opt:has(input:checked){border-color:var(--accent)}dialog select{width:100%;margin-top:6px}
.spare{display:flex;gap:10px;align-items:center;margin-top:8px}.spare img{height:40px;max-width:200px;object-fit:contain;background:#30343b;border-radius:4px}
.acts{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
</style></head><body>
<header>
  <h1>mw2tex</h1>
  <a class="tab on" href="/">Textures</a><a class="tab" href="/tables">Tables</a>
  <select id="file"></select><button id="openBtn">Open</button>
  <input id="search" placeholder="Search textures, e.g. cardicon_ or camo">
  <div class="chips" id="chips"></div>
  <button id="clearBtn">Clear changes</button>
  <button id="buildBtn" class="primary" disabled>Build</button>
</header>
<div class="info" id="info">Loading…</div>
<main id="grid"></main>
<div id="drop">Drop pictures named after textures to replace them all at once</div>
<div id="toast"></div>
<input type="file" id="picker" accept="image/*,.dds,.tga" hidden>
<dialog id="share"><form method="dialog" id="shareForm"></form></dialog>
<script>
const $=s=>document.querySelector(s);let textures=[],pending={},tables={file:null,changed:[]},pickFor=null,shown=0;
const CATS=["Titles","Emblems","Camos","Skyboxes","Graffiti","Signs & posters","Boxes & crates","Books & paper","Characters","Weapons","Vehicles","Plants & trees","Water & effects","Decals","Furniture & props","Ground","Walls & buildings","Wood & metal","Other","Detail maps"];
let cats=new Set(),onlyUnused=false,onlyChanged=false;
const LIMIT=400;
const el=(tag,props,...kids)=>{const e=Object.assign(document.createElement(tag),props||{});for(const k of kids)e.append(k);return e};
const kindOf=name=>name.startsWith("cardtitle_")?"title":"emblem";
function toast(msg,kind){const t=$("#toast");t.textContent=msg;t.className=kind||"";t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",kind==="bad"?9000:6000)}
async function api(path,opts){const r=await fetch(path,opts);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||("request failed: "+r.status));return j}
// Category chips toggle on and off; with none on, everything but detail maps shows.
function chips(){const box=$("#chips");box.innerHTML="";const count={};for(const t of textures)count[t.category]=(count[t.category]||0)+1;
 const chip=(label,on,click,n,cls)=>{const b=el("button",{className:"chip"+(on?" on":"")+(cls?" "+cls:""),title:label==="Detail maps"?"Bump, shine and lighting textures that go with the visible ones":""},label);if(n!==undefined)b.append(el("small",{textContent:n}));b.onclick=click;box.append(b)};
 chip("All",!cats.size&&!onlyUnused&&!onlyChanged,()=>{cats.clear();onlyUnused=onlyChanged=false;render()});
 for(const c of CATS)if(count[c])chip(c,cats.has(c),()=>{cats.has(c)?cats.delete(c):cats.add(c);render()},count[c]);
 if(textures.some(t=>t.spare))chip("Unused",onlyUnused,()=>{onlyUnused=!onlyUnused;render()},undefined,"flag");
 chip("Changed",onlyChanged,()=>{onlyChanged=!onlyChanged;render()},Object.keys(pending).length,"flag")}
function visible(){const q=$("#search").value.trim().toLowerCase();return textures.filter(t=>{if(onlyChanged&&!pending[t.name])return false;if(onlyUnused&&!t.spare)return false;
 if(cats.size?!cats.has(t.category):(t.category==="Detail maps"&&!q&&!onlyChanged))return false;return !q||t.name.toLowerCase().includes(q)})}
function render(){chips();const g=$("#grid");g.innerHTML="";const list=visible();shown=Math.min(list.length,LIMIT);
 const n=Object.keys(pending).length+tables.changed.length;$("#buildBtn").disabled=!n;$("#buildBtn").textContent="Build"+(n?" ("+n+")":"");
 if(!textures.length){g.innerHTML='<div class="empty">Pick a fastfile above and press Open.</div>';return}
 if(!list.length){g.innerHTML='<div class="empty">No textures match.</div>';return}
 for(const t of list.slice(0,LIMIT))g.appendChild(card(t));
 if(list.length>LIMIT){const d=document.createElement("div");d.className="empty";d.textContent="Showing "+LIMIT+" of "+list.length+". Type in the search box to narrow it down.";g.appendChild(d)}}
function card(t){const c=document.createElement("div");c.className="card"+(pending[t.name]?" changed":"");const p=pending[t.name];
 const pics=document.createElement("div");pics.className="pics"+(p?" two":"");
 const orig=new Image();orig.loading="lazy";orig.alt="";orig.src="/api/thumb?name="+encodeURIComponent(t.name);orig.onerror=()=>{orig.replaceWith(Object.assign(document.createElement("span"),{className:"none",textContent:t.stored==="pak"?"no preview (pak file missing)":"no preview"}))};pics.appendChild(orig);
 if(p){pics.appendChild(Object.assign(document.createElement("span"),{className:"arrow",textContent:"→"}));const n=new Image();n.src="/api/upload?name="+encodeURIComponent(t.name)+"&v="+encodeURIComponent(p.source);pics.appendChild(n);c.appendChild(Object.assign(document.createElement("div"),{className:"badge",textContent:"NEW"}))}
 c.appendChild(pics);c.appendChild(Object.assign(document.createElement("div"),{className:"name",textContent:t.name}));
 c.appendChild(Object.assign(document.createElement("div"),{className:"meta",textContent:t.width+"x"+t.height+" "+t.format+" · "+(t.stored==="ff"?"in the .ff":"pak texture")+(t.gray?" · shows in gray only":"")}));
 const kind=t.name.startsWith("cardtitle_")?"title":"emblem";
 if(t.maps&&t.maps.length&&(t.name.startsWith("cardtitle_")||t.name.startsWith("cardicon_")))c.appendChild(el("div",{className:"uses",textContent:"Tip: change titles and emblems in ui_mp.ff. Its Build copies them into every map."}));
 if(t.maps&&t.maps.length>1)c.appendChild(el("div",{className:"uses shared",textContent:"In "+t.maps.length+" maps: "+t.maps.join(", ")}));
 else if(t.maps&&t.maps.length===1)c.appendChild(el("div",{className:"uses",textContent:"Only in "+t.maps[0]}));
 if(t.uses){const u=document.createElement("div");u.className="uses"+(t.uses>1?" shared":"");u.textContent=t.uses>1?"Shared by "+t.uses+" "+kind+"s: "+t.used_by.join(", ")+(t.uses>t.used_by.length?"…":""):"Used by "+t.used_by[0];c.appendChild(u)}
 else if(t.spare){c.appendChild(Object.assign(document.createElement("div"),{className:"uses free",textContent:"Unused: no "+kind+" shows this picture"}))}
 if(p&&p.redirect){c.appendChild(Object.assign(document.createElement("div"),{className:"redir",textContent:"For "+p.redirect.id+" only (was "+p.redirect.old+")"}))}
 for(const a of t.assigned||[]){const u=el("button",{textContent:"Undo"});u.onclick=()=>unassign(a.index);c.appendChild(el("div",{className:"redir"},"Now shown by "+a.id+" (was "+a.old+")",u))}
 const row=document.createElement("div");row.className="row";
 if(!t.supported){row.appendChild(Object.assign(document.createElement("span"),{className:"none",textContent:"This format can't be replaced yet"}))}
 else{const b=document.createElement("button");b.textContent=p?"Change":"Choose picture";b.onclick=()=>{pickFor=t.name;$("#picker").click()};row.appendChild(b);
  const l=el("button",{textContent:"From link",title:"Use a picture from a web address"});l.onclick=()=>linkFor(t);row.appendChild(l);
  if(p){const u=document.createElement("button");u.textContent="Undo";u.onclick=()=>remove(t.name);row.appendChild(u);
   if(p.frames>1&&t.animate_options&&t.animate_options.length)row.appendChild(framePicker(t,p));
   else if(p.frames>1){row.appendChild(Object.assign(document.createElement("span"),{className:"none",textContent:"Animated file: only the first frame is used here"}))}}}
 c.appendChild(row);
 if(t.material&&tables.file&&(t.name.startsWith("cardtitle_")||t.name.startsWith("cardicon_"))&&(t.uses||t.spare)){const r2=el("div",{className:"row"});
  if(t.uses){const a=el("button",{textContent:"Use another picture",title:"Point this picture's "+kindOf(t.name)+"s at a picture already in the game, like an unused one"});a.onclick=()=>assignFor(t);r2.append(a)}
  const g=el("button",{textContent:"Show on a "+kindOf(t.name),title:"Make another "+kindOf(t.name)+" use this picture"});g.onclick=()=>giveTo(t);r2.append(g);c.appendChild(r2)}
 if(t.supported){c.ondragover=e=>{e.preventDefault();e.stopPropagation();c.classList.add("drag")};c.ondragleave=()=>c.classList.remove("drag");
  c.ondrop=e=>{e.preventDefault();e.stopPropagation();c.classList.remove("drag");$("#drop").style.display="none";const f=e.dataTransfer.files[0];if(f)return choose(t,f);
  const u=droppedLink(e);if(u)fetchUrl(u).then(f=>choose(t,f)).catch(err=>toast(err.message,"bad"))}}
 return c}
async function upload(name,file){const j=await api("/api/upload?name="+encodeURIComponent(name)+"&filename="+encodeURIComponent(file.name),{method:"POST",body:file});pending=j.pending}
async function remove(name){const j=await api("/api/remove",{method:"POST",body:JSON.stringify({name})});pending=j.pending;textures=j.textures;tables=j.tables;render()}
function framePicker(t,p){const s=el("select",{title:"The game plays every frame for the same time, so more frames make a longer, slower loop. Your file has "+p.frames+" frames; they're spread evenly over the ones you pick."});
 s.appendChild(el("option",{value:"0",textContent:"Still picture (no animation)"}));
 for(const o of t.animate_options)s.appendChild(el("option",{value:String(o.frames),textContent:"▶ "+o.frames+" frames, "+o.frame+(o.mb>=0.5?", "+o.mb+" MB":"")+(o.frames>=p.frames&&!t.animate_options.some(x=>x.frames>=p.frames&&x.frames<o.frames)?" (fits your "+p.frames+")":""),title:"Texture becomes "+o.sheet+" ("+o.mb+" MB)"}));
 s.value=String(p.animate||0);s.onchange=()=>option(t.name,+s.value);s.style.maxWidth="100%";return el("label",{style:"max-width:100%"},s)}
async function option(name,animate){pending=(await api("/api/option",{method:"POST",body:JSON.stringify({name,animate})})).pending;render()}
$("#picker").onchange=async e=>{const f=e.target.files[0];e.target.value="";if(!f||!pickFor)return;const t=textures.find(x=>x.name===pickFor);if(t)choose(t,f)};
function droppedLink(e){const u=(e.dataTransfer.getData("text/uri-list")||e.dataTransfer.getData("text/plain")||"").split("\n").map(x=>x.trim()).find(x=>/^https?:\/\//i.test(x));return u||null}
async function fetchUrl(url){toast("Downloading "+url+"…");const r=await fetch("/api/fetch?url="+encodeURIComponent(url));if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||"download failed")}
 const blob=await r.blob();return new File([blob],r.headers.get("X-Filename")||"web_picture.png")}
async function linkFor(t){const url=prompt("Paste the picture's web address.\nTip: right-click a picture on a website and choose 'Copy image address'.");if(!url||!url.trim())return;
 try{choose(t,await fetchUrl(url.trim()))}catch(err){toast(err.message,"bad")}}
function after(j){if(j.textures)textures=j.textures;if(j.tables)tables=j.tables;if(j.pending)pending=j.pending;render()}
async function unassign(index){try{after(await api("/api/unassign",{method:"POST",body:JSON.stringify({index})}));toast("Put back","ok")}catch(err){toast(err.message,"bad")}}
function dialog(build,onOk){const F=$("#shareForm");F.innerHTML="";build(F);const d=$("#share");d.onclose=()=>{if(d.returnValue==="ok")onOk()};d.returnValue="";d.showModal()}
function acts(label){return el("div",{className:"acts"},el("button",{value:"cancel",textContent:"Cancel"}),el("button",{className:"primary",value:"ok",textContent:label}))}
// Point a picture's titles (one or all) at another picture already in the game, e.g. an unused one.
async function assignFor(t){let plan;try{plan=await api("/api/plan?name="+encodeURIComponent(t.name))}catch(err){return toast(err.message,"bad")}
 const kind=kindOf(t.name),n=plan.uses.length,who=el("select"),pick=el("select"),img=el("img",{alt:""});plan.uses.forEach((u,i)=>who.append(el("option",{value:i,textContent:u.id})));
 const g1=el("optgroup",{label:"Unused pictures"}),g2=el("optgroup",{label:"Pictures other "+kind+"s use"}),imgs={};
 for(const s of plan.spares){imgs[s.material]=s.image;g1.append(el("option",{value:s.material,textContent:s.material}))}
 for(const s of plan.others){imgs[s.material]=s.image;g2.append(el("option",{value:s.material,textContent:s.material+" (used by "+s.uses+")"}))}
 if(g1.children.length)pick.append(g1);if(g2.children.length)pick.append(g2);const show=()=>{img.src="/api/thumb?name="+encodeURIComponent(imgs[pick.value]||"")};pick.onchange=show;
 const one=el("input",{type:"radio",name:"mode",value:"one",checked:n>1}),all=el("input",{type:"radio",name:"mode",value:"all",checked:n<=1});who.onfocus=()=>one.checked=true;
 dialog(F=>{F.append(el("h2",{textContent:"Use another picture instead of "+plan.material}),el("p",{textContent:"The "+kind+"'s table row is changed to show a picture that's already in the game. No texture is changed."}));
  if(n>1)F.append(el("label",{className:"opt"},one," Only one "+kind,el("div",{className:"uses",textContent:"Which "+kind+":"}),who));
  F.append(el("label",{className:"opt"},all,n>1?" All "+n+" "+kind+"s":" "+plan.uses[0].id),el("div",{className:"uses",textContent:"Picture to show:"}),pick,el("div",{className:"spare"},img),acts("Use this picture"));show()},
  async()=>{const targets=(one.checked&&n>1)?[plan.uses[+who.value]]:plan.uses;
   try{const j=await api("/api/assign",{method:"POST",body:JSON.stringify({material:pick.value,targets})});after(j);toast(j.done.length+" "+kind+(j.done.length===1?" now shows ":"s now show ")+pick.value+". Build to write codxe_patch_mp.ff.","ok")}catch(err){toast(err.message,"bad")}})}
// Make one title show this picture (handy for unused pictures).
async function giveTo(t){const kind=kindOf(t.name);let rows;try{rows=(await api("/api/rows?kind="+(kind==="title"?"cardtitle_":"cardicon_"))).rows}catch(err){return toast(err.message,"bad")}
 const who=el("select");rows.filter(r=>r.material!==t.material.toLowerCase()).forEach((r,i)=>who.append(el("option",{value:r.table+"|"+r.row,textContent:r.id+"  (now "+r.material+")"})));
 dialog(F=>F.append(el("h2",{textContent:"Show "+t.material+" on which "+kind+"?"}),el("p",{textContent:"That "+kind+"'s table row is changed to use this picture. Other "+kind+"s keep theirs."}),who,acts("Show it there")),
  async()=>{const[table,row]=who.value.split("|");try{const j=await api("/api/assign",{method:"POST",body:JSON.stringify({material:t.material,targets:[{table,row:+row}]})});after(j);toast(j.done.map(d=>d.id+" now shows "+d.new).join("\n")+"\nBuild to write codxe_patch_mp.ff.","ok")}catch(err){toast(err.message,"bad")}})}
// A picture several titles share: ask whether the new picture is for all of them or just one.
async function choose(t,f){if(t.uses<2||pending[t.name]){try{await upload(t.name,f);render();toast("Queued "+f.name+" for "+t.name,"ok")}catch(err){toast(err.message,"bad")}return}
 let plan;try{plan=await api("/api/plan?name="+encodeURIComponent(t.name))}catch(err){return toast(err.message,"bad")}
 const kind=t.name.startsWith("cardtitle_")?"title":"emblem",F=$("#shareForm");F.innerHTML="";
 const el=(tag,props,...kids)=>{const e=Object.assign(document.createElement(tag),props||{});for(const k of kids)e.append(k);return e};
 F.append(el("h2",{textContent:plan.uses.length+" "+kind+"s share this picture"}),el("p",{textContent:t.name+" is shown by "+plan.uses.map(u=>u.id).slice(0,8).join(", ")+(plan.uses.length>8?" and "+(plan.uses.length-8)+" more":"")+"."}));
 const all=el("label",{className:"opt"},el("input",{type:"radio",name:"mode",value:"all"})," Change all "+plan.uses.length+" "+kind+"s");
 const one=el("label",{className:"opt"});const radio=el("input",{type:"radio",name:"mode",value:"one",checked:true});
 const who=el("select");plan.uses.forEach((u,i)=>who.append(el("option",{value:i,textContent:u.id})));
 const spare=el("select");for(const s of plan.spares)spare.append(el("option",{value:s.material,textContent:s.material+(s.same_size?"":" ("+s.width+"x"+s.height+", picture gets resized)")}));
 const img=el("img",{alt:""});const showSpare=()=>{const s=plan.spares.find(x=>x.material===spare.value);if(s)img.src="/api/thumb?name="+encodeURIComponent(s.image)};spare.onchange=showSpare;who.onfocus=spare.onfocus=()=>radio.checked=true;
 if(plan.spares.length){one.append(radio," Only one "+kind+", the rest keep the old picture",el("div",{className:"uses",textContent:"Which "+kind+":"}),who,
   el("div",{className:"uses",textContent:"Your picture goes on this unused "+kind+" picture, and that "+kind+"'s table row is changed to use it:"}),spare,el("div",{className:"spare"},img));showSpare()}
 else{one.append(el("span",{className:"none",textContent:"No unused "+kind+" pictures are left, so only 'change all' is possible."}));all.querySelector("input").checked=true}
 F.append(one,all);const go=el("button",{className:"primary",value:"ok",textContent:"Queue picture"}),no=el("button",{value:"cancel",textContent:"Cancel"});F.append(el("div",{className:"acts"},no,go));
 const d=$("#share");d.onclose=async()=>{if(d.returnValue!=="ok")return;const mode=F.querySelector("input[name=mode]:checked").value;
  try{if(mode==="all"){await upload(t.name,f);render();toast("Queued "+f.name+" for all "+plan.uses.length+" "+kind+"s","ok")}
   else{const j=await api("/api/upload?name="+encodeURIComponent(t.name)+"&filename="+encodeURIComponent(f.name)+"&only="+who.value+"&spare="+encodeURIComponent(spare.value),{method:"POST",body:f});
    pending=j.pending;textures=j.textures;tables=j.tables;render();toast("Queued "+f.name+" on "+j.redirect.new+"\n"+j.redirect.id+" now uses it (table "+j.redirect.table+", row "+(j.redirect.row+1)+"). The other "+(plan.uses.length-1)+" keep "+j.redirect.old+".","ok")}}catch(err){toast(err.message,"bad")}};
 d.returnValue="";d.showModal()}
async function loadFiles(){const j=await api("/api/files");const s=$("#file");s.innerHTML="";if(!j.files.length){s.innerHTML="<option>no .ff files here</option>";$("#info").textContent="No .ff files in "+j.folder+". Put your fastfiles (like ui_mp.ff) in that folder and reload this page.";return}
 if(j.maps>1)s.appendChild(el("option",{value:"*maps",textContent:"All maps ("+j.maps+" mp_*.ff)"}));for(const f of j.files){const o=document.createElement("option");o.textContent=f;s.appendChild(o)}s.value=j.open||(j.files.includes("ui_mp.ff")?"ui_mp.ff":j.files[0]);
 $("#info").textContent="Folder: "+j.folder+" · pak files here: "+(j.paks.join(", ")||"none (pak textures show no preview)");if(j.open){const t=await api("/api/textures");textures=t.textures;pending=t.pending;tables=t.tables;render()}else render()}
$("#openBtn").onclick=async()=>{$("#openBtn").disabled=true;$("#openBtn").textContent="Opening…";try{const j=await api("/api/open",{method:"POST",body:JSON.stringify({file:$("#file").value})});textures=j.textures;pending=j.pending;cats.clear();onlyUnused=onlyChanged=false;const t=await api("/api/textures");tables=t.tables;render();
 const card=textures.some(x=>x.name.startsWith("cardtitle_")||x.name.startsWith("cardicon_")),allMaps=$("#file").value==="*maps";
 toast((allMaps?"Opened all maps: "+textures.length+" textures, "+textures.filter(x=>x.maps.length>1).length+" of them in more than one map":"Opened "+$("#file").value+": "+textures.length+" textures")
  +(Object.keys(pending).length?"\nKept "+Object.keys(pending).length+" map picture"+(Object.keys(pending).length>1?"s":"")+" from your last Build.":"")+(card&&!tables.file&&!allMaps?"\nPut code_post_gfx_mp.ff in this folder to see which titles and emblems use each picture.":""),"ok")}catch(e){toast(e.message,"bad")}$("#openBtn").disabled=false;$("#openBtn").textContent="Open"};
$("#search").oninput=()=>render();
$("#clearBtn").onclick=async()=>{if(!Object.keys(pending).length)return;if(!confirm("Clear all queued replacements?"))return;const j=await api("/api/remove",{method:"POST",body:JSON.stringify({all:true})});pending=j.pending;textures=j.textures;tables=j.tables;render()};
$("#buildBtn").onclick=async()=>{const b=$("#buildBtn");b.disabled=true;b.textContent="Building…";try{const j=await api("/api/build",{method:"POST",body:"{}"});toast("Built:\n"+j.log.join("\n")+"\n\nWrote "+j.written.join(", ")+".\nCopy "+(j.written.length>1?"them":"it")+" to _codxe\\zone\\ on your console.","ok")}catch(e){toast(e.message,"bad")}try{tables=(await api("/api/textures")).tables}catch(e){}render()};
let depth=0;window.addEventListener("dragenter",e=>{if(e.dataTransfer.types.includes("Files")&&textures.length){depth++;$("#drop").style.display="flex"}});
window.addEventListener("dragleave",()=>{if(--depth<=0){depth=0;$("#drop").style.display="none"}});
window.addEventListener("dragover",e=>e.preventDefault());
window.addEventListener("drop",async e=>{e.preventDefault();depth=0;$("#drop").style.display="none";if(!textures.length)return;if(!e.dataTransfer.files.length&&droppedLink(e))return toast("Drop a web picture onto the texture card it should replace.","bad");const names=new Set(textures.filter(t=>t.supported).map(t=>t.name.toLowerCase()));const byLower={};for(const t of textures)byLower[t.name.toLowerCase()]=t.name;
 const ok=[],miss=[];for(const f of e.dataTransfer.files){const stem=f.name.replace(/\.[^.]+$/,"").toLowerCase();if(names.has(stem)){try{await upload(byLower[stem],f);ok.push(byLower[stem])}catch(err){miss.push(f.name+" ("+err.message+")")}}else miss.push(f.name)}
 render();toast((ok.length?"Queued "+ok.length+": "+ok.join(", "):"Nothing matched.")+(miss.length?"\nNot matched (name the file after the texture, or drop it on a card): "+miss.join(", "):""),miss.length&&!ok.length?"bad":"ok")});
loadFiles().catch(e=>toast(e.message,"bad"));
</script></body></html>
"""


def main():
    port = PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("couldn't find a free port for the texture picker")
    url = "http://127.0.0.1:%d/" % port
    print("mw2tex texture picker running at %s" % url)
    print("Working folder: %s" % FOLDER)
    print("Leave this window open while you use it. Press Ctrl+C here to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
