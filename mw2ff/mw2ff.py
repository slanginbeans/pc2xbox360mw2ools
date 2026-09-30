"""mw2ff: read MW2 (TU6, Xbox 360) fastfiles asset by asset, and write them back.

    python mw2ff.py list   <file.ff>                  every asset in the file, in load order
    python mw2ff.py unpack <file.ff> <folder>         one folder per asset with its fields as JSON
    python mw2ff.py pack   <folder> <new.ff>          build a fastfile from an unpacked folder
    python mw2ff.py verify <file.ff> [...]            unpack and pack in memory, check it is identical

An unpacked folder also has scripts\ (every script as a plain file) and localize.txt (every
in-game text). Edit them in any text editor, at any length, and pack: changed scripts and
texts go back in, and a new file under scripts\ becomes a new script in the fastfile.

The reader follows the same steps as the game's own loader (checked against the TU6 code),
so every byte of the file belongs to a known field of a known asset.
"""

import json
import os
import re
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "mw2tex"))

import codec as codec_mod  # noqa: E402
import relocate as relocate_mod  # noqa: E402
import schema as schema_mod  # noqa: E402
import text as text_mod  # noqa: E402
import zone as zone_mod  # noqa: E402

HEADER_BYTES = 48      # zone header (8 words) + asset list header (4 words)
NAME_MEMBERS = ("name", "szInternalName", "aliasName", "filename", "szDisplayName")


# ---------------------------------------------------------------- fastfile container

def read_fastfile(path):
    """Returns (container info, decompressed zone)."""
    import mw2tex
    ff = mw2tex.FastFile(path)
    return ff, bytes(ff.zone)


# ---------------------------------------------------------------- zone <-> records

def walk(zone):
    r = zone_mod.Reader(zone, record=True)
    r.walk()
    if r.pos != len(zone):
        raise zone_mod.ZoneError("walk stopped at %d of %d bytes" % (r.pos, len(zone)))
    return r


def asset_name(r, index):
    strings = [(p, r.string_at(v) if isinstance(v, int) else v) for p, v in r.asset_strings.get(index, [])]
    strings = [(p, v) for p, v in strings if v is not None]
    for want in NAME_MEMBERS:
        for path, s in strings:
            last = path.split(" > ")[-1]
            if last.startswith(want + "("):
                return s.decode("utf-8", "replace")
    for path, s in strings:
        return s.decode("utf-8", "replace")
    return ""


def listing(r):
    """[(index, type name, asset name, first byte, byte count)] in load order."""
    out = []
    by_index = {}
    for rec in r.records:
        a = rec[3]
        if a < 0:
            continue
        lo, hi = by_index.get(a, (rec[0], rec[0]))
        by_index[a] = (min(lo, rec[0]), max(hi, rec[0] + rec[1]))
    for i, (t, p) in enumerate(r.entries):
        lo, hi = by_index.get(i, (0, 0))
        out.append((i, zone_mod.ASSET_TYPES[t], asset_name(r, i), lo, hi - lo))
    return out


def decode(zone, r=None):
    """Zone bytes -> (document, blobs). The document holds every streamed record, split
    into the zone part (script strings, asset list) and one list per asset."""
    r = r or walk(zone)
    c = codec_mod.Codec(r.s)
    blobs = {-1: bytearray()}
    records = {-1: []}
    pos = HEADER_BYTES
    for (at, n, desc, a, path) in r.records:
        if at != pos:
            raise zone_mod.ZoneError("gap in the walk at %d (next record at %d)" % (pos, at))
        pos = at + n
        buf = blobs.setdefault(a, bytearray())
        rec = c.decode(desc, zone[at:at + n], buf)
        if path:
            rec["at"] = path
        records.setdefault(a, []).append(rec)
    if pos != len(zone):
        raise zone_mod.ZoneError("walk ended at %d of %d" % (pos, len(zone)))
    assets = []
    for i, (t, p) in enumerate(r.entries):
        assets.append({"index": i, "type": zone_mod.ASSET_TYPES[t], "type_id": t,
                       "name": asset_name(r, i), "records": records.get(i, [])})
    doc = {"format": "mw2ff-1", "header": zone[:HEADER_BYTES].hex(), "zone": records[-1], "assets": assets}
    return doc, blobs


def encode(doc, blobs, sch=None):
    c = codec_mod.Codec(sch or schema_mod.load())
    out = bytearray(bytes.fromhex(doc["header"]))
    for rec in doc["zone"]:
        out += c.encode(rec, blobs[-1])
    for a in doc["assets"]:
        b = blobs.get(a["index"], bytearray())
        for rec in a["records"]:
            out += c.encode(rec, b)
    return bytes(out)


def build(doc, blobs, layout, sch=None):
    """Encode a document whose pieces may have changed size (or had assets added or removed)
    and lay the zone out again: layout is relocate.Layout of the original zone (or its Reader)."""
    sch = sch or schema_mod.load()
    raw = encode(doc, blobs, sch)
    zone, _ = relocate_mod.relocate(raw, layout, sch, text_mod.index_map(doc))
    return zone


# ---------------------------------------------------------------- folders

def _safe(name):
    name = re.sub(r"[^A-Za-z0-9_.,@#+-]+", "_", name).strip("._")
    return (name or "unnamed")[:80]


SCRIPTS_DIR = "scripts"
LOCALIZE_FILE = "localize.txt"
LOCALIZE_HEAD = ("# In-game text: one line per entry, KEY = text. Change the text after the = sign;\n"
                 "# it may be any length. \\n is a new line.\n")


def _script_path(name):
    """Where a script goes under scripts/: its own name, with only the characters Windows
    can't have in a file name replaced."""
    parts = []
    for p in name.replace("\\", "/").split("/"):
        p = re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).rstrip(". ")
        if p and p != "..":
            parts.append(p)
    return "/".join(parts) or "unnamed"


def unpack(ff_path, out_dir, log=print):
    ff, zone = read_fastfile(ff_path)
    r = walk(zone)
    doc, blobs = decode(zone, r)
    sch = r.s
    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    index = {"format": doc["format"], "source": os.path.basename(ff_path), "header": doc["header"],
             "zone": doc["zone"], "assets": []}
    open(os.path.join(out_dir, "zone.bin"), "wb").write(bytes(blobs[-1]))
    for a in doc["assets"]:
        folder = "%05d_%s_%s" % (a["index"], a["type"], _safe(a["name"]))
        d = os.path.join(out_dir, "assets", folder)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "asset.json"), "w", encoding="utf-8") as fh:
            json.dump({"index": a["index"], "type": a["type"], "name": a["name"], "records": a["records"]},
                      fh, indent=1, ensure_ascii=False)
        b = blobs.get(a["index"])
        if b:
            open(os.path.join(d, "data.bin"), "wb").write(bytes(b))
        index["assets"].append({"index": a["index"], "type": a["type"], "type_id": a["type_id"],
                                "name": a["name"], "folder": folder})
    index["scripts"] = {}
    lines = []
    for a in doc["assets"]:
        if text_mod.is_script(a):
            rel = _script_path(a["name"])
            while rel in index["scripts"]:
                rel += "_"
            path = os.path.join(out_dir, SCRIPTS_DIR, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(text_mod.script_text(a, blobs, sch))
            index["scripts"][rel] = a["name"]
        elif text_mod.is_localize(a):
            v = text_mod.localize_value(a)
            if v is not None:
                lines.append("%s = %s" % (a["name"], text_mod.escape(v)))
    if lines:
        with open(os.path.join(out_dir, LOCALIZE_FILE), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(LOCALIZE_HEAD + "\n".join(lines) + "\n")
    open(os.path.join(out_dir, "layout.bin"), "wb").write(relocate_mod.Layout.of(r).dumps())
    with open(os.path.join(out_dir, "zone.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1, ensure_ascii=False)
    # keep the container header so pack can write a complete file
    open(os.path.join(out_dir, "container.bin"), "wb").write(ff.raw[:ff.header_end + 8])
    log("unpacked %d assets from %s into %s" % (len(doc["assets"]), ff_path, out_dir))


def pack(in_dir, ff_path, log=print):
    index = json.load(open(os.path.join(in_dir, "zone.json"), encoding="utf-8"))
    blobs = {-1: bytearray(open(os.path.join(in_dir, "zone.bin"), "rb").read())}
    doc = {"header": index["header"], "zone": index["zone"], "assets": []}
    for a in index["assets"]:
        d = os.path.join(in_dir, "assets", a["folder"])
        data = json.load(open(os.path.join(d, "asset.json"), encoding="utf-8"))
        doc["assets"].append({"index": a["index"], "type": a["type"], "type_id": a["type_id"],
                              "name": a["name"], "records": data["records"]})
        p = os.path.join(d, "data.bin")
        blobs[a["index"]] = bytearray(open(p, "rb").read()) if os.path.exists(p) else bytearray()
    sch = schema_mod.load()
    for note in import_texts(in_dir, index, doc, blobs, sch):
        log(note)
    layout_path = os.path.join(in_dir, "layout.bin")
    if os.path.exists(layout_path):
        zone = build(doc, blobs, relocate_mod.Layout.loads(open(layout_path, "rb").read()), sch)
    else:
        zone = encode(doc, blobs, sch)     # folder from an older mw2ff: sizes must not change
    head = bytearray(open(os.path.join(in_dir, "container.bin"), "rb").read())
    write_container(head, zone, ff_path)
    log("packed %d assets into %s" % (len(doc["assets"]), ff_path))
    return zone


def import_texts(in_dir, index, doc, blobs, sch):
    """Take changed scripts (and new ones) and changed texts from an unpacked folder."""
    notes = []
    known = index.get("scripts", {})
    by_name = {a["name"]: a for a in doc["assets"] if text_mod.is_script(a)}
    seen = set()
    sdir = os.path.join(in_dir, SCRIPTS_DIR)
    for root, _dirs, files in os.walk(sdir):
        for fn in sorted(files):
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, sdir).replace(os.sep, "/")
            data = open(path, "rb").read()
            name = known.get(rel, rel)
            if name in seen:
                raise ValueError("two files under scripts are both %s; keep only one" % name)
            seen.add(name)
            a = by_name.get(name)
            if a is None:
                asset, blob = text_mod.new_script(name, data, 0)
                text_mod.add_asset(doc, blobs, asset, blob)
                notes.append("added new script %s" % name)
            elif text_mod.script_text(a, blobs, sch) != data:
                text_mod.set_script_text(a, blobs, data)
                notes.append("changed script %s" % name)
    lpath = os.path.join(in_dir, LOCALIZE_FILE)
    if os.path.exists(lpath):
        loc = {a["name"]: a for a in doc["assets"] if text_mod.is_localize(a)}
        for line in open(lpath, encoding="utf-8-sig").read().split("\n"):
            line = line.rstrip("\r")
            if not line.strip() or line.startswith("#") or " = " not in line:
                continue
            key, value = line.split(" = ", 1)
            a = loc.get(key.strip())
            if a is None:
                continue
            value = text_mod.unescape(value)
            if text_mod.localize_value(a) != value:
                text_mod.set_localize_value(a, value)
                notes.append("changed text %s" % key.strip())
    return notes


def write_container(container, zone, path):
    """container: the original file up to and including its two size words. The zone is
    recompressed and written unsigned, the same way mw2tex writes files."""
    head = bytearray(container[:-8])
    size0, size1 = struct.unpack(">II", container[-8:])
    head[:8] = b"IWffu100"
    stream = zlib.compress(zone, 9)
    total = len(head) + 8 + len(stream)
    with open(path, "wb") as fh:
        fh.write(bytes(head) + struct.pack(">II", total, total + size1 - size0) + stream)


# ---------------------------------------------------------------- commands

def cmd_list(path):
    ff, zone = read_fastfile(path)
    r = walk(zone)
    rows = listing(r)
    for i, t, name, at, n in rows:
        print("%5d  %-18s %-60s %10d bytes" % (i, t, name, n))
    counts = {}
    for row in rows:
        counts[row[1]] = counts.get(row[1], 0) + 1
    print("\n%d assets: %s" % (len(rows), ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items()))))


def cmd_verify(paths):
    ok = True
    for path in paths:
        t0 = time.time()
        ff, zone = read_fastfile(path)
        doc, blobs = decode(zone)
        doc = json.loads(json.dumps(doc))   # make sure only JSON survives
        rebuilt = encode(doc, blobs)
        same = rebuilt == zone
        ok &= same
        print("%-28s %4s  %6d assets  %11d bytes  %.0fs" % (
            os.path.basename(path), "OK" if same else "DIFF", len(doc["assets"]), len(zone), time.time() - t0))
        if not same:
            first = next(i for i in range(min(len(zone), len(rebuilt))) if zone[i] != rebuilt[i])
            print("   first difference at byte %d" % first)
    return ok


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == "list" and len(argv) == 3:
        cmd_list(argv[2])
    elif cmd == "unpack" and len(argv) == 4:
        unpack(argv[2], argv[3])
    elif cmd == "pack" and len(argv) == 4:
        pack(argv[2], argv[3])
    elif cmd == "verify" and len(argv) >= 3:
        return 0 if cmd_verify(argv[2:]) else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
