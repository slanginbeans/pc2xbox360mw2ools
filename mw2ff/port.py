"""Convert a PC (IW4x / 2009 PC, version 276) fastfile to Xbox 360 TU6 layout.

    python port.py mp_geometric.ff OUT.ff [--iwd mp_geometric.iwd] [--ref360 FILE.ff ...]

The PC file is read as a tree (tree.py). Every struct is carried over to the 360's struct
of the same name field by field (numbers in big-endian, fields matched by name); the pieces
the two platforms store differently are converted by the hooks below:

  - map vertices: normals and tangents repacked (PC 8-bit + scale -> 360 10:10:10)
  - techsets (shaders): PC shaders can't run on the 360, so each techset is swapped for the
    360's own techset of the same name, from a stock 360 file (--ref360) or, when the game
    always has it loaded (code_post_gfx_mp), a reference to it by name (",name")
  - materials: the 360 has fewer techniques per material; render state comes from a 360
    material using the same techset
  - images: pixels from the .iwd (.iwi files) or from the fastfile are tiled for the 360 GPU
    and stored in the fastfile with their mipmaps
"""

import argparse
import os
import re
import struct
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "mw2tex"))

import codec as codec_mod
import mw2ff
import schema as schema_mod
import tree
from cdefs import PTR, Compound, Enum, Prim
from tree import Leaf, PtrList, Ref, Str

X_DEFAULT_REFS = ["code_post_gfx_mp.ff", "mp_favela.ff"]
# Unions whose members are separate bytes on both platforms (the "packed" number is only a
# way to copy them): their bytes stay as they are. Checked against stock Favela.
BYTE_UNIONS = {"GfxSurfaceLightingAndFlags"}
# Zones the 360 keeps loaded the whole time: their assets can be used by name.
RESIDENT = ("code_post_gfx_mp", "common_mp")


class PortError(Exception):
    pass


def _swap_words(raw, size):
    if size <= 1:
        return bytes(raw)
    b = bytearray(len(raw))
    for i in range(size):
        b[i::size] = raw[size - 1 - i::size]
    return bytes(b)


def _uniform(m):
    """Element size when member m is made only of numbers (or pointers) of one size, else None."""
    if PTR in m.mods:
        return 4
    t = m.type
    if isinstance(t, (Prim, Enum)):
        return t.size if m.bits is None else None
    if isinstance(t, Compound):
        sizes = set()
        for x in t.members:
            u = _uniform(x)
            if u is None:
                return None
            sizes.add(u)
        return sizes.pop() if len(sizes) == 1 else None
    return None


def _has_ptr(m):
    if PTR in m.mods:
        return True
    return isinstance(m.type, Compound) and any(_has_ptr(x) for x in m.type.members)


def _asset_in_slot(r):
    """If alias r points at a pointer member (inside a struct or array of structs) that holds
    an asset, that asset (or Ref), else None."""
    t, tgt, rel = r.t, r.target, r.rel
    if isinstance(tgt, tree.AssetEntry) and rel == 4:
        return tgt[1]
    if isinstance(tgt, tree.InsertSlot):
        return tgt.asset
    if not isinstance(t, Compound) or t.kind != "struct":
        return None
    if isinstance(tgt, list):
        i, rel = divmod(rel, t.size)
        if i >= len(tgt):
            return None
        tgt = tgt[i]
    if not isinstance(tgt, dict):
        return None
    for m in t.members:
        if m.offset == rel and m.mods and m.mods[0] == PTR:
            c = tgt.get("@", {}).get((tree.mkey(t, m), ()))
            if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
                return c
    return None


def deref(c):
    """An asset child or the asset an alias refers to."""
    if isinstance(c, Ref):
        a = _asset_in_slot(c)
        if a is not None:
            return deref(a)
        if c.rel == 0 and isinstance(c.target, dict):
            return c.target
        return None
    return c


def asset_name(d):
    """The name string of an asset dict (Str), or None."""
    ch = d.get("@", {})
    c = ch.get(("name", ()))
    if c is None and isinstance(d.get("info"), dict):
        c = d["info"].get("@", {}).get(("name", ()))
    return c.b if isinstance(c, Str) else None


def iter_objects(root):
    """Every dict/list/Leaf in a tree, once."""
    seen = set()
    stack = [root]
    while stack:
        o = stack.pop()
        if id(o) in seen or o is None or isinstance(o, (Str, Ref, str, int, float)):
            continue
        seen.add(id(o))
        yield o
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "@":
                    stack.extend(v.values())
                elif isinstance(v, (dict, list, Leaf)):
                    stack.append(v)
        elif isinstance(o, list):
            stack.extend(o)


def asset_index(root):
    """{(struct name, asset name bytes): dict} for every asset in a tree, top-level or inline."""
    out = {}
    for o in iter_objects(root):
        if isinstance(o, dict) and "_asset" in o:
            n = asset_name(o)
            if n is not None:
                out.setdefault((o["_asset"], n), o)
    return out


# ================================================================ vertices

def _pc_unit(b):
    s = (b[3] + 192) / 32385.0
    return [(b[i] - 127) * s for i in range(3)]


def _dec3n(v):
    u = 0
    for k in range(3):
        x = int(round(max(-1.0, min(1.0, v[k])) * 511))
        u |= (x & 0x3FF) << (10 * k)
    return u


def convert_world_vertices(leaf):
    """GfxWorldVertex: floats to big-endian, color as the same 32-bit value, normal and
    tangent repacked as 10:10:10 signed (checked against stock Favela: 99% within 0.02)."""
    raw = leaf.raw
    out = bytearray(len(raw))
    cache = {}
    for o in range(0, len(raw), 44):
        f = struct.unpack_from("<4f", raw, o)
        t = struct.unpack_from("<4f", raw, o + 20)
        struct.pack_into(">4f", out, o, *f)
        out[o + 16:o + 20] = raw[o + 16:o + 20][::-1]
        struct.pack_into(">4f", out, o + 20, *t)
        for k in (36, 40):
            key = raw[o + k:o + k + 4]
            u = cache.get(key)
            if u is None:
                u = cache[key] = _dec3n(_pc_unit(key))
            struct.pack_into(">I", out, o + k, u)
    return Leaf(leaf.t, leaf.n, out, ">")


# ================================================================ images

# PC D3DFORMAT / fourcc -> (mw2tex gpu format, 360 image format word from a stock image)
IWI_FORMATS = {0x0B: "DXT1", 0x0C: "DXT3", 0x0D: "DXT5", 0x01: "ARGB8"}


IWI_NOMIPMAPS, IWI_CUBE = 0x2, 0x10000


def read_iwi(data):
    """IW4 .iwi (version 8): returns (format name, width, height, mips largest first, is cube).
    For a cube map, mips holds the six faces' top levels instead."""
    if data[:3] != b"IWi" or data[3] != 8:
        raise PortError("not an IW4 image (version %r)" % data[3:4])
    flags, = struct.unpack_from("<I", data, 4)
    fmt = data[8]
    w, h, d = struct.unpack_from("<3H", data, 10)
    name = IWI_FORMATS.get(fmt)
    if name is None:
        raise PortError("image format %d isn't supported yet" % fmt)
    bw, bpb = (1, 4) if name == "ARGB8" else (4, 8 if name == "DXT1" else 16)
    sizes = []
    lw, lh = w, h
    while True:
        sizes.append(max(1, (lw + bw - 1) // bw) * max(1, (lh + bw - 1) // bw) * bpb)
        if (lw == 1 and lh == 1) or flags & IWI_NOMIPMAPS:
            break
        lw, lh = max(1, lw >> 1), max(1, lh >> 1)
    body = data[32:]
    cube = bool(flags & IWI_CUBE)
    faces = 6 if cube else 1
    if len(body) < sum(sizes) * faces:
        sizes = sizes[:1]
    # Smallest mip first in the file (each mip holds all faces of a cube map).
    levels, pos = [None] * len(sizes), len(body) - sum(sizes) * faces
    for i in range(len(sizes) - 1, -1, -1):
        levels[i] = body[pos:pos + sizes[i] * faces]
        pos += sizes[i] * faces
    if cube:
        return name, w, h, [levels[0][f * sizes[0]:(f + 1) * sizes[0]] for f in range(6)], True
    return name, w, h, levels, False


def fit_picture(fmt_name, w, h, mips, limit=2048):
    """Pictures the 360 can't take as they are (sides not a power of two, or too big) are
    resized: decoded, scaled down to powers of two, and compressed again (top level only)."""
    def p2(v):
        r = 1
        while r * 2 <= v:
            r *= 2
        return min(r, limit)
    nw, nh = p2(w), p2(h)
    if (nw, nh) == (w, h):
        return w, h, mips
    import io
    import mw2tex
    from PIL import Image
    gpu = {v[0]: k for k, v in mw2tex.FORMATS.items()}[fmt_name]
    pic = Image.open(io.BytesIO(mw2tex.dds_bytes(w, h, gpu, mips[:1]))).convert("RGBA")
    pic = pic.resize((nw, nh), Image.LANCZOS)
    return nw, nh, [mw2tex._encode(pic.tobytes(), nw, nh, gpu)]


class ImageMaker:
    """Builds 360 GfxImage dicts with their pixels in the fastfile."""

    def __init__(self, xcodec, templates):
        import mw2tex
        mw2tex.FORMATS.setdefault(0x02, ("L8", 1, 1, ()))
        mw2tex.FORMATS.setdefault(0x3A, ("DXT3A", 4, 8, ()))
        self.tex = mw2tex
        self.xc = xcodec
        self.t = xcodec.type_by_name("GfxImage")
        self.templates = templates      # gpu format -> stock 360 image dict

    def build(self, d, fmt_name, width, height, mips, cube=False, tpl=None):
        """Fill image dict d (in place) as a 360 image with these linear little-endian mips.
        tpl: the stock image to copy settings from (default: any of this format)."""
        tex = self.tex
        gpu = {v[0]: k for k, v in tex.FORMATS.items()}[fmt_name]
        if tpl is None:
            tpl = self.templates.get((gpu, cube))
        make_cube = False
        if tpl is None and cube:
            # No stock cube map of this format inside a file: take a flat one and make it a cube.
            tpl = self.templates.get((gpu, False))
            make_cube = True
        if tpl is None:
            raise PortError("no stock 360 %s%s image to copy settings from" % (fmt_name, " cube" if cube else ""))
        if cube:
            # Six faces, top level only (each face tiled on its own, one after the other).
            levels = 1
            pixels = b"".join(tex.tile([m], width, height, gpu, single=True) for m in mips)
        else:
            levels = len(mips)
            pixels = tex.tile(mips, width, height, gpu, single=(levels == 1))
        if fmt_name == "L8":
            pixels = tex._swap(pixels, gpu)     # 8-bit data isn't byte-swapped on the 360
        hdr = bytearray(bytes.fromhex(tpl["texture"]))
        dw = list(struct.unpack_from("<6I", hdr, 28))
        bw = tex.FORMATS[gpu][1]
        pitch = tex._align(width, 128 if bw == 4 else 32) // 32
        dw[0] = (dw[0] & ~(0x1FF << 22)) | (pitch << 22)
        dw[2] = (dw[2] & ~0x3FFFFFF) | (width - 1) | ((height - 1) << 13)
        dw[4] = (dw[4] & ~(0xF << 6)) | ((levels - 1) << 6)
        mip_addr = 0
        if levels > 1:
            mip_addr = tex._layout(width, height, 0, gpu)[3] >> 12
        dw[5] = (dw[5] & 0xFFF & ~(1 << 11)) | (mip_addr << 12) | ((1 << 11) if levels > 1 else 0)
        if make_cube:
            dw[2] = (dw[2] & 0x3FFFFFF) | (5 << 26)          # six faces
            dw[5] = (dw[5] & ~(3 << 9)) | (3 << 9)            # dimension: cube
        struct.pack_into("<6I", hdr, 28, *dw)
        name = d.get("@", {}).get(("name", ()))
        new = dict(tpl)
        new.pop("_fix", None)
        new.update(texture=hdr.hex(), cardMemory=len(pixels), width=width, height=height, depth=1,
                   levelCount=levels, streaming=0, pixels="follow", name="follow",
                   streams=[{"width": 0, "height": 0, "info": 0} for _ in range(4)])
        for k in ("mapType", "semantic", "category"):
            if k in d:
                new[k] = d[k]
        if cube:
            new["mapType"] = 5
        new["@"] = {("name", ()): name, ("pixels", ()): Leaf(self.xc.type_by_name("unsigned char"), len(pixels),
                                                              pixels, ">")}
        new["_asset"] = "GfxImage"
        keep_slot = d.get("_slot")
        d.clear()
        d.update(new)
        if keep_slot is not None:
            d["_slot"] = keep_slot


def encode_dxt3a(lum, width, height):
    """8-bit single channel -> DXT3A blocks (4 bits a pixel, DXT3's alpha block), linear."""
    bw, bh = max(1, width // 4), max(1, height // 4)
    out = bytearray(bw * bh * 8)
    q = bytes(min(15, (v * 15 + 127) // 255) for v in range(256))
    for by in range(bh):
        for bx in range(bw):
            o = (by * bw + bx) * 8
            for y in range(4):
                row = lum[(by * 4 + y) * width + bx * 4:(by * 4 + y) * width + bx * 4 + 4]
                out[o + 2 * y] = q[row[0]] | (q[row[1]] << 4)
                out[o + 2 * y + 1] = q[row[2]] | (q[row[3]] << 4)
    return bytes(out)


# ================================================================ converter

class Porter:
    def __init__(self, pc_root, x_refs, iwd=None, log=print):
        self.root = pc_root
        self.log = log
        self.P = schema_mod.load("pc")
        self.X = schema_mod.load("xbox")
        self.pc = codec_mod.Codec(self.P)
        self.xc = codec_mod.Codec(self.X)
        self.done = set()
        self.iwd = iwd
        # Stock 360 assets: resident ones (use by ",name") and others to copy in.
        self.resident = {}
        self.library = {}
        for name, root in x_refs:
            idx = asset_index(root)
            base = os.path.splitext(os.path.basename(name))[0]
            for k, v in idx.items():
                if k[1].startswith(b","):
                    continue
                if base in RESIDENT:
                    self.resident.setdefault(k, v)
                else:
                    self.library.setdefault(k, v)
        self.templates = {}
        for (typ, n), v in list(self.resident.items()) + list(self.library.items()):
            if typ == "GfxImage" and v.get("pixels") == "follow" and v.get("streaming") == 0:
                fmt = struct.unpack_from("<I", bytes.fromhex(v["texture"]), 32)[0] & 0x3F
                self.templates.setdefault((fmt, v.get("mapType") == 5), v)
        self.images = ImageMaker(self.xc, self.templates)
        # Stock lightmaps: their channel order (swizzle) differs from other pictures'.
        self.lightmap_templates = {}
        for (typ, n), v in list(self.library.items()):
            if typ == "GfxImage" and n.startswith(b"*lightmap") and v.get("pixels") == "follow":
                self.lightmap_templates.setdefault(n.rsplit(b"_", 1)[-1], v)
        # Stock 360 materials by the techset they use (render state templates).
        self.material_templates = {}
        for (typ, n), v in list(self.library.items()) + list(self.resident.items()):
            if typ != "Material":
                continue
            ts = deref(v.get("@", {}).get(("techniqueSet", ())))
            tn = asset_name(ts) if isinstance(ts, dict) else None
            if tn is not None:
                self.material_templates.setdefault(tn.lstrip(b","), v)
        self.warnings = []
        self.moved_images = []
        self.report = set()     # (type, "dropped" | "defaulted", member) seen while converting

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)
            self.log("  note: " + msg)

    # ------------------------------------------------------------ generic

    def zero(self, t):
        raw = bytes(t.size)
        return self.xc._decode_one(t, raw, 0, t.size)

    def conv_value(self, v, tp, tx, dims_p=(), dims_x=(), track=True):
        """An embedded value (not a pointer target) of PC type tp to 360 type tx."""
        if dims_p or dims_x:
            if not isinstance(v, list):
                return v
            n = dims_x[0] if dims_x else len(v)
            out = [self.conv_value(v[i], tp, tx, dims_p[1:], dims_x[1:], track) if i < len(v) else
                   self._zero_dims(tx, dims_x[1:]) for i in range(n)]
            return out
        if isinstance(v, Leaf):
            return self.conv_leaf(v, tx)
        if isinstance(tx, Compound) and isinstance(v, dict):
            return self.conv_dict(v, tp, tx, track)
        if isinstance(v, str) and isinstance(tx, Prim) and tx.size == 1:
            return v
        return v

    def _zero_dims(self, t, dims):
        if not dims:
            return self.zero(t) if isinstance(t, Compound) else (0 if not (isinstance(t, Prim) and t.fmt == "f") else 0.0)
        return [self._zero_dims(t, dims[1:]) for _ in range(dims[0])]

    def conv_union(self, d, tp, tx, track=True):
        """A union's bytes: converted as the member that is in use. That is the member the load
        followed pointers in (expanded into d), else the only member without pointers, else
        (members all plain numbers of one size) any of them."""
        h = bytes.fromhex(d["union"])
        size = len(h)
        out = dict(d)
        um = d.get("_um")
        if tx.name in BYTE_UNIONS:
            self.report.add((tx.name, "union", "bytes"))
            return out
        if um:
            names = um
        else:
            words = set(_uniform(m) for m in tx.members)
            if len(words) == 1 and None not in words:
                w = words.pop()
                self.report.add((tx.name, "union", "words of %d" % w))
                out["union"] = _swap_words(h, w).hex()
                return out
            full = [m for m in tx.members if not m.mods and m.bits is None and
                    isinstance(m.type, (Prim, Enum)) and m.type.size == size]
            if full:
                # A "packed" member holding the whole thing as one number: keep the number.
                self.report.add((tx.name, "union", "packed %d" % size))
                out["union"] = _swap_words(h, size).hex()
                return out
            plain = [tree.mkey(tx, m) for m in tx.members if not _has_ptr(m)]
            if len(plain) != 1:
                raise PortError("don't know which member of union %s is in use" % tx.name)
            names = plain
        b = bytearray(size)
        pm = {tree.mkey(tp, m): m for m in tp.members}
        for i, mx in enumerate(tx.members):
            k = tree.mkey(tx, mx)
            if k not in names:
                continue
            mp = pm[k]
            v = d[k] if k in d else self.pc._decode_member(mp, h, 0)
            v = self.conv_value(v, mp.type, mx.type, [q for q in mp.mods if q != PTR],
                                [q for q in mx.mods if q != PTR], track) if PTR not in mx.mods else v
            if k in d:
                out[k] = v
            self.xc._encode_member(mx, v, b, 0)
        out["union"] = b.hex()
        return out

    def conv_dict(self, d, tp, tx, track=True):
        """Convert struct dict d in place (keeps its identity for pointers that refer to it).
        track=False for throwaway dicts (their ids get reused)."""
        if track:
            if id(d) in self.done:
                return d
            self.done.add(id(d))
        # A typedef'd struct (GfxCellTree128 = GfxCellTree) is stored as the struct itself.
        while tp.kind == "typedef" and isinstance(tp.members[0].type, Compound) and not tp.members[0].mods:
            tp = tp.members[0].type
        while tx.kind == "typedef" and isinstance(tx.members[0].type, Compound) and not tx.members[0].mods:
            tx = tx.members[0].type
        hook = getattr(self, "pre_" + tx.name, None)
        if hook is not None and hook(d, tp, tx) is not None:
            return d
        if tx.kind == "union":
            new = self.conv_union(d, tp, tx, track)
            ch = d.get("@")
            d.clear()
            d.update(new)
            if ch:
                d["@"] = self.conv_children(ch, tp, tx)
            return d
        if tx.kind == "typedef":
            return d
        pm = {}
        for i, m in enumerate(tp.members):
            pm[tree.mkey(tp, m)] = m
        xk = set(tree.mkey(tx, m) for m in tx.members)
        for k in pm:
            if k not in xk and k in d:
                self.report.add((tx.name, "dropped", k))
        new = {}
        ch = d.get("@", {})
        for i, mx in enumerate(tx.members):
            kx = tree.mkey(tx, mx)
            mp = pm.get(kx)
            if mp is None and kx in d:
                # Set by a pre_ hook in the 360 layout already.
                new[kx] = d[kx]
                continue
            if mp is None or kx not in d or (PTR in mp.mods) != (PTR in mx.mods):
                new[kx] = self._default_member(mx)
                self.report.add((tx.name, "defaulted", kx))
                continue
            v = d[kx]
            if PTR in mx.mods:
                new[kx] = v
                continue
            dims_p = [q for q in mp.mods if q != PTR]
            dims_x = [q for q in mx.mods if q != PTR]
            if mx.bits is not None:
                new[kx] = v
            elif isinstance(v, (Leaf, list, dict)):
                new[kx] = self.conv_value(v, mp.type, mx.type, dims_p, dims_x, track)
            else:
                new[kx] = v
        new["@"] = self.conv_children(ch, tp, tx)
        for k in ("_asset", "_slot", "_template"):
            if k in d:
                new[k] = d[k]
        d.clear()
        d.update(new)
        post = getattr(self, "post_" + tx.name, None)
        if post is not None:
            post(d, tx)
        return d

    def _default_member(self, m):
        if m.mods and m.mods[0] == PTR:
            return None
        dims = [q for q in m.mods if q != PTR]
        if PTR in m.mods:
            n = 1
            for q in dims:
                n *= q
            return [None] * n
        if isinstance(m.type, Prim) and m.type.size == 1 and len(dims) == 1:
            return "00" * dims[0]
        if m.bits is not None:
            return 0
        return self._zero_dims(m.type, dims)

    def conv_children(self, ch, tp, tx):
        out = {}
        pm = {tree.mkey(tp, m): m for m in tp.members}
        xm = {tree.mkey(tx, m): m for m in tx.members}
        for (k, idx), c in ch.items():
            mx, mp = xm.get(k), pm.get(k)
            if mx is None:
                continue
            if mp is None:
                mp = mx     # put there by a pre_ hook; already in 360 terms or plain data
            out[(k, idx)] = self.conv_child(c, mp.type, mx.type)
        return out

    def conv_child(self, c, tp, tx):
        if c is None or isinstance(c, (Str, Ref)):
            return c
        if isinstance(c, PtrList):
            for i, x in enumerate(c):
                c[i] = self.conv_child(x, tp, tx)
            return c
        if isinstance(c, list):
            for x in c:
                self.conv_dict(x, tp, tx)
            return c
        if isinstance(c, dict):
            if "_asset" in c:
                return self.conv_asset(c)
            return self.conv_dict(c, tp, tx)
        if isinstance(c, Leaf):
            return self.conv_leaf(c, tx)
        return c

    def conv_leaf(self, lf, tx):
        """Convert plain data in place (pointers may refer to it)."""
        if lf.E == ">":
            return lf
        new = self._conv_leaf(lf, tx)
        lf.t, lf.n, lf.raw, lf.E = new.t, new.n, new.raw, new.E
        return lf

    def _conv_leaf(self, lf, tx):
        hook = getattr(self, "leaf_" + getattr(tx, "name", ""), None)
        if hook is not None:
            return hook(lf, tx)
        tp = lf.t
        if isinstance(tp, (Prim, Enum)):
            if tp.size != tx.size:
                raise PortError("%s changes size" % tp.name)
            return Leaf(tx, lf.n, _swap_words(lf.raw, tp.size), ">")
        if not isinstance(tp, Compound):
            raise PortError("can't convert %r" % tp)
        vals = [self.pc._decode_one(tp, lf.raw, i * tp.size) for i in range(lf.n)]
        out = bytearray(tx.size * lf.n)
        for i, v in enumerate(vals):
            if isinstance(v, dict):
                v = self.conv_dict(v, tp, tx, track=False)
            self.xc._encode_one(tx, v, out, i * tx.size)
        return Leaf(tx, lf.n, out, ">")

    # ------------------------------------------------------------ assets

    def conv_asset(self, d):
        if id(d) in self.done:
            return d
        name = d["_asset"]
        tp = self.P.infos[name].ctype
        if name not in self.X.infos:
            raise PortError("the 360 has no %s assets" % name)
        tx = self.X.infos[name].ctype
        return self.conv_dict(d, tp, tx)

    def convert(self):
        ents = self.root["assets"]
        self.world_checksum = next((e[1].get("checksum", 0) for e in ents
                                    if e[0] == "gfx_map" and isinstance(e[1], dict)), 0)
        # Models placed only by single-player entities (IW4x maps often carry one) aren't
        # used in multiplayer; the 360 model format isn't converted yet, so leave them out.
        referenced = set()
        for o in iter_objects(self.root):
            if isinstance(o, dict):
                for c in o.get("@", {}).values():
                    for x in (c if isinstance(c, list) else [c]):
                        if isinstance(x, Ref) and isinstance(x.target, tree.AssetEntry):
                            referenced.add(id(x.target))
        keep = []
        for e in ents:
            if e[0] == "xmodel" and id(e) not in referenced:
                self.warn("left out model %s (the 360 model format isn't converted yet)"
                          % asset_name(e[1]).decode())
                continue
            keep.append(e)
        ents[:] = keep
        for e in ents:
            if e[0] in ("vertexshader", "vertexdecl"):
                raise PortError("top-level %s assets can't be converted" % e[0])
            if isinstance(e[1], dict):
                self.conv_asset(e[1])
        ss = self.root.get("script_strings")
        self.root["platform"] = "xbox"
        return self.root

    # ------------------------------------------------------------ hooks

    def pre_GfxWorldDpvsDynamic(self, d, tp, tx):
        """PC: dynEntVisData[2][3]; 360: six separate pointers dynEntVisData0_0 .. 1_2."""
        v = d.pop("dynEntVisData", None)
        ch = d.get("@", {})
        for i in range(2):
            pl = ch.pop(("dynEntVisData", (i,)), None)
            for j in range(3):
                k = "dynEntVisData%d_%d" % (i, j)
                d[k] = v[i * 3 + j] if v else None
                if pl is not None and pl[j] is not None:
                    ch[(k, ())] = pl[j]
        return None

    def post_GfxAabbTree(self, d, tx):
        """childrenOffset is a byte distance to the node's first child: nodes are 44 bytes on
        the PC and 40 on the 360."""
        tp = self.P.defs.types["GfxAabbTree"]
        d["childrenOffset"] = d["childrenOffset"] // tp.size * tx.size

    def post_GfxWorldDpvsStatic(self, d, tx):
        """The PC sorts surfaces twice (all, then without decals); the 360 keeps one list."""
        lf = d["@"].get(("sortedSurfIndex", ()))
        n = d["staticSurfaceCount"]
        if isinstance(lf, Leaf) and lf.n > n:
            lf.raw, lf.n = lf.raw[:n * lf.t.size], n

    def post_GfxWorldDraw(self, d, tx):
        """IW4x ZoneBuilder fills the lightmap override pointers with the sky and $outdoor
        pictures. Stock 360 maps leave them empty; set, the game would light every surface
        with them (a cube map where a flat lightmap belongs)."""
        for k in ("lightmapOverridePrimary", "lightmapOverrideSecondary"):
            c = d["@"].pop((k, ()), None)
            d[k] = None
            if isinstance(c, dict):
                # The picture was written here first; later pointers to it now get it instead.
                self.moved_images.append(c)

    def post_GfxWorld(self, d, tx):
        ch = d["@"]
        for key, c in list(ch.items()):
            t = deref(c) if isinstance(c, Ref) else None
            if t is not None and any(t is m for m in self.moved_images):
                ch[key] = t
                d[key[0]] = "insert"
                self.moved_images = [m for m in self.moved_images if m is not t]
        if self.moved_images:
            raise PortError("a picture from the lightmap override is still needed elsewhere")

    def post_FxGlassSystem(self, d, tx):
        """firstFreePiece is really a 16-bit number (then padding): 0xFFFF, "no free piece",
        reads 0x0000FFFF on the PC and 0xFFFF0000 on the 360."""
        v = d.get("firstFreePiece")
        if isinstance(v, int) and v <= 0xFFFF:
            d["firstFreePiece"] = v << 16

    def post_clipMap_t(self, d, tx):
        """IW4x ZoneBuilder leaves the collision checksum 0 but writes 0xDEADBEEF into the
        world's: stock maps have the same number in both, so use the world's."""
        if not d.get("checksum"):
            d["checksum"] = self.world_checksum

    def post_MapEnts(self, d, tx):
        """Single-player actors (IW4x maps often keep one) have no spawn code in
        multiplayer and name models this file doesn't carry: leave them out."""
        lf = d["@"].get(("entityString", ()))
        if not isinstance(lf, Leaf):
            return
        text = lf.raw.rstrip(b"\0").decode("latin-1")
        kept, dropped = [], 0
        for ent in re.findall(r"\{[^{}]*\}", text):
            m = re.search(r'"classname"\s+"([^"]*)"', ent)
            if m and m.group(1).startswith("actor_"):
                dropped += 1
                continue
            kept.append(ent)
        if not dropped:
            return
        self.warn("left out %d single-player actor entities" % dropped)
        raw = ("\n".join(kept) + "\n").encode("latin-1") + b"\0"
        lf.raw, lf.n = raw, len(raw)
        d["numEntityChars"] = len(raw)

    def leaf_GfxWorldVertex(self, lf, tx):
        return convert_world_vertices(lf)

    def _replace(self, d, new):
        slot = d.get("_slot")
        d.clear()
        d.update(new)
        if slot is not None:
            d["_slot"] = slot
        self.done.add(id(d))
        return True

    def reference(self, typ, name):
        """A 360 asset that only names an asset the game already has loaded."""
        tx = self.X.infos[typ].ctype
        d = self.zero(tx)
        if typ == "Material":
            d["info"]["name"] = "follow"
            d["info"]["@"] = {("name", ()): Str(b"," + name)}
        else:
            d["name"] = "follow"
            d["@"] = {("name", ()): Str(b"," + name)}
        d["_asset"] = typ
        return d

    def pre_MaterialTechniqueSet(self, d, tp, tx):
        name = asset_name(d)
        if ("MaterialTechniqueSet", name) in self.resident:
            return self._replace(d, self.reference("MaterialTechniqueSet", name))
        src = self.library.get(("MaterialTechniqueSet", name))
        if src is None:
            raise PortError("no 360 techset %s in the stock files given" % name.decode())
        return self._replace(d, self.copy_in(src))

    def in_iwd(self, name):
        if self.iwd is None:
            return False
        try:
            self.iwd.getinfo("images/%s.iwi" % name.decode())
            return True
        except KeyError:
            return False

    def pre_GfxImage(self, d, tp, tx):
        name = asset_name(d)
        # Built-in pictures ($identitynormalmap, ...) stay the game's own even when an IW4x
        # .iwd carries a copy: a second asset with the name would replace the resident one.
        if ("GfxImage", name) in self.resident and (name.startswith(b"$") or not self.in_iwd(name)):
            return self._replace(d, self.reference("GfxImage", name))
        tex = d.get("texture", {})
        ld = tex.get("@", {}).get(("loadDef", ())) if isinstance(tex, dict) else None
        if isinstance(ld, dict) and ld.get("resourceSize"):
            self.image_from_loaddef(d, ld)
        else:
            self.image_from_iwd(d, name)
        self.done.add(id(d))
        return True

    def pre_Material(self, d, tp, tx):
        name = asset_name(d)
        tt = d.get("@", {}).get(("textureTable", ()))
        images = [deref(t.get("@", {}).get(("image", ()))) for t in (tt if isinstance(tt, list) else [])
                  if isinstance(t, dict) and isinstance(t.get("u"), dict)]
        images += [deref(t["u"].get("@", {}).get(("image", ()))) for t in (tt if isinstance(tt, list) else [])
                   if isinstance(t, dict) and isinstance(t.get("u"), dict)]
        own = any(isinstance(i, dict) and self.in_iwd(asset_name(i) or b"") for i in images)
        if ("Material", name) in self.resident and not own:
            # The game has this material loaded already and the map brings no pictures of its
            # own for it (a map's $levelbriefing does, so it gets its own copy).
            return self._replace(d, self.reference("Material", name))
        ts = deref(d.get("@", {}).get(("techniqueSet", ())))
        tsname = asset_name(ts) if isinstance(ts, dict) else None
        tpl = self.material_templates.get(tsname)
        if tpl is None:
            raise PortError("material %s: no stock 360 material uses techset %s to copy render "
                            "settings from" % (name.decode(), tsname))
        # PC render state (D3D9) means nothing to the 360: it comes from the template instead.
        d["@"].pop(("stateBitsTable", ()), None)
        d["_template"] = tpl
        return None

    def post_Material(self, d, tx):
        tpl = d.pop("_template")
        for k in ("stateBitsEntry", "stateBitsCount", "stateFlags", "cameraRegion", "stateBitsTable", "unknown"):
            d[k] = tpl[k]
        d["info"]["sortKey"] = tpl["info"]["sortKey"]
        # The game builds drawSurf itself when it registers the material; stock 360 files
        # always hold zero here (the PC keeps its own bit layout).
        d["info"]["drawSurf"] = tpl["info"]["drawSurf"]
        c = tpl.get("@", {}).get(("stateBitsTable", ()))
        if isinstance(c, Ref):
            c = c.target if c.rel == 0 else None
        if c is None:
            raise PortError("template material's render state can't be copied")
        d["@"][("stateBitsTable", ())] = c
        d["stateBitsTable"] = "follow"

    # ------------------------------------------------------------ images

    def image_from_iwd(self, d, name):
        if self.iwd is None:
            raise PortError("image %s needs the map's .iwd" % name.decode())
        path = "images/%s.iwi" % name.decode()
        try:
            data = self.iwd.read(path)
        except KeyError:
            raise PortError("image %s isn't in the .iwd" % name.decode())
        fmt, w, h, mips, cube = read_iwi(data)
        limit = 1024 if name.startswith(b"loadscreen") else 2048
        if not cube:
            w, h, mips = fit_picture(fmt, w, h, mips, limit)
        self.images.build(d, fmt, w, h, mips, cube)

    def image_from_loaddef(self, d, ld):
        """Images the PC keeps in the fastfile: lightmaps, reflection probes, $outdoor."""
        name = asset_name(d)
        fmt = ld["format"]
        data = ld["data"].raw if isinstance(ld.get("data"), Leaf) else b""
        w, h = d["width"], d["height"]
        cube = bool(ld["flags"] & 4) or d.get("mapType") == 5
        if fmt == 21:        # A8R8G8B8: same byte order as a DDS, which is what mw2tex tiles
            bpp, out_fmt = 4, "ARGB8"
        elif fmt == 50:      # L8
            bpp = 1
            # Lightmaps: the 360 stores the primary lightmap as DXT3A (4-bit single channel,
            # like the stock maps); other single-channel images as plain 8-bit.
            out_fmt = "DXT3A" if name.startswith(b"*lightmap") else "L8"
        else:
            raise PortError("image %s: PC format %d isn't supported yet" % (name.decode(), fmt))
        faces = 6 if cube else 1
        face = len(data) // faces
        top = w * h * bpp
        mips = [data[i * face:i * face + top] for i in range(faces)]
        if out_fmt == "DXT3A":
            mips = [encode_dxt3a(m, w, h) for m in mips]
        tpl = None
        if name.startswith(b"*lightmap"):
            tpl = self.lightmap_templates.get(name.rsplit(b"_", 1)[-1])
        self.images.build(d, out_fmt, w, h, mips if cube else mips[:1], cube, tpl)

    # ------------------------------------------------------------ copying 360 assets in

    def copy_in(self, src):
        """A copy of a stock 360 asset subtree, safe to write into another file: pointers back
        to things outside the subtree are replaced by the things themselves."""
        inside = set(id(o) for o in iter_objects(src))
        memo = {}

        def cp(o):
            if isinstance(o, Ref):
                if o.target is not None and id(o.target) in inside:
                    return o
                asset = _asset_in_slot(o)
                if asset is not None:
                    # An asset pointer that points at another struct's pointer to the asset
                    # (how zones refer to an asset loaded earlier): use the asset itself.
                    if isinstance(asset, Ref):
                        return cp(asset)
                    if id(asset) not in inside:
                        for y in iter_objects(asset):
                            inside.add(id(y))
                    return asset
                if isinstance(o.target, list) and isinstance(o.t, Compound) and o.rel % o.t.size == 0:
                    # Points at a later element of another array (stock files share the tail of
                    # one argument list with another): give it its own copy of that tail.
                    tail = tree.Tail(o.target[o.rel // o.t.size:])
                    for x in tail:
                        for y in iter_objects(x):
                            inside.add(id(y))
                    inside.add(id(tail))
                    return tail
                if o.target is not None and o.rel == 0:
                    inside.add(id(o.target))
                    for x in iter_objects(o.target):
                        inside.add(id(x))
                    return o.target
                raise PortError("stock asset points outside itself (%r -> %s of %s rel %d)" % (o, type(o.target).__name__, getattr(o.t, "name", o.t), o.rel))
            return o

        def marker(n):
            return "insert" if isinstance(n, dict) and "_slot" in n else "follow"

        def fix(o):
            if not isinstance(o, dict):
                return
            ch = o.get("@")
            if not ch:
                return
            for (name, idx), c in list(ch.items()):
                if isinstance(c, Ref):
                    n = cp(c)
                    if n is not c:
                        ch[(name, idx)] = n
                        if idx == () and not isinstance(o.get(name), list):
                            o[name] = marker(n)
                        elif idx:
                            o[name][idx[0]] = marker(n)
                elif isinstance(c, PtrList):
                    for i, x in enumerate(c):
                        if isinstance(x, Ref):
                            n = cp(x)
                            if n is not x:
                                c[i] = n
                                if c.raw is not None:
                                    c.raw[i] = marker(n)
                                if isinstance(o.get(name), list):
                                    o[name][i] = marker(n)

        seen = set()
        stack = [src]
        while stack:
            o = stack.pop()
            if id(o) in seen:
                continue
            seen.add(id(o))
            fix(o)
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "@":
                        stack.extend(x for x in v.values() if isinstance(x, (dict, list)))
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(o, list):
                stack.extend(x for x in o if isinstance(x, (dict, list)))
        new = dict(src)
        new.pop("_slot", None)
        return new


# ================================================================ command line

def load_tree(path):
    ff, zone = mw2ff.read_fastfile(path)
    sch = mw2ff.schema_for(ff.platform)
    root, r = tree.read_tree(zone, sch)
    return ff, zone, root


def port(pc_path, out_path, iwd_path=None, ref_paths=(), log=print):
    ff, zone, root = load_tree(pc_path)
    if ff.platform != "pc":
        raise PortError("%s is not a PC fastfile" % pc_path)
    refs = []
    for p in ref_paths:
        log("reading stock 360 file %s" % os.path.basename(p))
        refs.append((p, load_tree(p)[2]))
    iwd = zipfile.ZipFile(iwd_path) if iwd_path else None
    log("converting %s" % os.path.basename(pc_path))
    porter = Porter(root, refs, iwd, log)
    porter.convert()
    xs = schema_mod.load("xbox")
    w = tree.TreeWriter(root, xs, keep_fixes=False)
    w.map_rel = lambda r: map_rel(r, porter.P, porter.X)
    out = w.write()
    _write_x360(out, out_path)
    log("wrote %s (%d bytes of zone)" % (out_path, len(out)))
    return out, porter


def map_rel(r, P, X):
    t = r.t
    if not isinstance(t, Compound):
        return r.rel
    tx = X.defs.types.get(t.name)
    if tx is None or tx.size == t.size and [m.offset for m in tx.members] == [m.offset for m in t.members]:
        return r.rel
    i, rem = divmod(r.rel, t.size)
    return i * tx.size + _member_off(t, tx, rem)


def _member_off(tp, tx, rem):
    if rem == 0:
        return 0
    for m in tp.members:
        if m.offset <= rem < m.offset + m.size:
            mx = next((x for x in tx.members if x.name == m.name), None)
            if mx is None:
                raise PortError("a pointer points at %s.%s, which the 360 doesn't have" % (tp.name, m.name))
            inner = rem - m.offset
            if isinstance(m.type, Compound) and not [q for q in m.mods if q != PTR]:
                return mx.offset + _member_off(m.type, mx.type, inner)
            return mx.offset + inner
    raise PortError("a pointer points into padding of %s" % tp.name)


# 360 container header of a stock file (magic, version 0x10D, flags, build time), then the
# pak table count (0: every image is inside this file) and the file size twice.
X360_HEAD = bytes.fromhex("49576666753130300000010d0101ca3ec038c2e4a000000001")


def _write_x360(zone, path):
    import zlib
    stream = zlib.compress(zone, 9)
    head = X360_HEAD + struct.pack(">I", 0)
    total = len(head) + 8 + len(stream)
    with open(path, "wb") as f:
        f.write(head + struct.pack(">II", total, total) + stream)


def main(argv):
    ap = argparse.ArgumentParser(description="Convert a PC fastfile to Xbox 360 TU6 layout")
    ap.add_argument("pc_ff")
    ap.add_argument("out_ff")
    ap.add_argument("--iwd")
    ap.add_argument("--ref360", nargs="*", default=[])
    a = ap.parse_args(argv)
    port(a.pc_ff, a.out_ff, a.iwd, a.ref360)


if __name__ == "__main__":
    main(sys.argv[1:])
