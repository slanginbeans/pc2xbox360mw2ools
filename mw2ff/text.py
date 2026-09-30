"""Scripts (rawfiles) and in-game text (localize entries) as plain text, at any length.

These work on a decoded document (see mw2ff.decode): they change the records, and
mw2ff.build then lays the zone out again and fixes every pointer.
"""

import struct
import zlib

import codec as codec_mod

FOLLOW = "follow"
RAWFILE_TYPE = 34
ASSET_LIST_RECORD = "u32"


class TextError(Exception):
    pass


def _codec(sch):
    return codec_mod.Codec(sch)


def _raw(c, rec, blob):
    return c.encode(rec, blob)


def _set_bytes(rec, data, blob):
    """Point a byte-array record at new bytes (kept in the asset's blob)."""
    rec.pop("hex", None)
    rec.pop("fix", None)
    rec["n"] = len(data)
    rec["blob"] = [len(blob), len(data)]
    blob += data


# ---------------------------------------------------------------- scripts

def is_script(asset):
    return asset["type"] == "rawfile"


def script_text(asset, blobs, sch):
    """The rawfile's contents as bytes (decompressed)."""
    c = _codec(sch)
    recs = asset["records"]
    head = recs[0]["v"]
    blob = blobs.get(asset["index"], bytearray())
    body = [r for r in recs[1:] if r["k"] == "type" and r.get("t") == "char"]
    if not body:
        return b""
    data = _raw(c, body[-1], blob)
    if head["compressedLen"] > 0:
        try:
            return zlib.decompress(data)
        except zlib.error:
            return data      # not compressed after all (IW4x ZoneBuilder writes a note like this)
    return data[:head["len"]]


def set_script_text(asset, blobs, text, compress=None):
    """Replace a rawfile's contents. Compressed stays compressed (the game inflates it with
    zlib); a file that was stored plain stays plain."""
    if isinstance(text, str):
        text = text.encode("utf-8")
    recs = asset["records"]
    head = recs[0]["v"]
    if compress is None:
        compress = head["compressedLen"] > 0
    blob = blobs.setdefault(asset["index"], bytearray())
    body = [r for r in recs[1:] if r["k"] == "type" and r.get("t") == "char"]
    data = zlib.compress(text, 9) if compress else text + b"\0"
    if not body:
        raise TextError("%s has no contents to replace" % asset["name"])
    head["compressedLen"] = len(data) if compress else 0
    head["len"] = len(text)
    _set_bytes(body[-1], data, blob)


def new_script(name, text, index):
    """Records for a new rawfile asset (compressed, like the game's own scripts)."""
    if isinstance(text, str):
        text = text.encode("utf-8")
    data = zlib.compress(text, 9)
    blob = bytearray(data)
    recs = [
        {"k": "type", "t": "RawFile", "n": 1,
         "v": {"name": FOLLOW, "compressedLen": len(data), "len": len(text), "data": {"union": "ffffffff"}}},
        {"k": "string", "s": name, "at": "name(single)"},
        {"k": "type", "t": "char", "n": len(data), "blob": [0, len(data)],
         "at": "data(embedded) > RawFileBuffer > compressedBuffer(array)"},
    ]
    return {"index": index, "type": "rawfile", "type_id": RAWFILE_TYPE, "name": name, "records": recs,
            "new": True}, blob


# ---------------------------------------------------------------- localize

def is_localize(asset):
    return asset["type"] == "localize"


def _string_rec(asset, member):
    for r in asset["records"]:
        if r["k"] == "string" and r.get("at", "").split("(")[0] == member:
            return r
    return None


def localize_value(asset):
    r = _string_rec(asset, "value")
    if r is None:
        return None
    return r["s"] if "s" in r else bytes.fromhex(r["hex"]).decode("utf-8", "replace")


def set_localize_value(asset, value):
    r = _string_rec(asset, "value")
    if r is None:
        raise TextError("%s has no text to change" % asset["name"])
    if "\0" in value:
        raise TextError("text can't contain a zero character")
    r.pop("hex", None)
    r["s"] = value


def escape(s):
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def unescape(s):
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------- asset list

def asset_list(doc):
    for r in doc["zone"]:
        if r["k"] == ASSET_LIST_RECORD:
            return r
    raise TextError("the file has no asset list")


def _set_count(doc, count):
    head = bytearray(bytes.fromhex(doc["header"]))
    E = "<" if doc.get("platform") == "pc" else ">"
    struct.pack_into(E + "I", head, len(head) - 8, count)
    doc["header"] = head.hex()


def add_asset(doc, blobs, asset, blob):
    """Append an asset to the end of the file. Its blob key is a fresh number."""
    key = max([a["index"] for a in doc["assets"]] + [max(k for k in blobs) if blobs else 0]) + 1
    asset["index"] = key
    blobs[key] = blob
    doc["assets"].append(asset)
    lst = asset_list(doc)
    lst["v"] = lst["v"] + [asset["type_id"], 0xFFFFFFFF]
    _set_count(doc, len(doc["assets"]))
    return asset


def remove_asset(doc, index):
    """Take an asset out of the file (by its index key)."""
    pos = next(i for i, a in enumerate(doc["assets"]) if a["index"] == index)
    del doc["assets"][pos]
    lst = asset_list(doc)
    lst["v"] = lst["v"][:2 * pos] + lst["v"][2 * pos + 2:]
    _set_count(doc, len(doc["assets"]))


def index_map(doc):
    """New asset position -> original asset index (None for added assets), for relocate."""
    return [None if a.get("new") else a["index"] for a in doc["assets"]]
