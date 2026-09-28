"""MW2 Xbox 360 texture tool: list, export and replace textures in a fastfile.

Needs Python 3 (and Pillow for the put command). Works on map fastfiles (like mp_rust.ff) and on common_mp.ff and
ui_mp.ff, which hold camos, titles and emblems. Commands:

  python mw2tex.py list    common_mp.ff
      Writes common_mp.ff.images.csv listing every texture: name, format, size, and
      where its pixels are stored ("pak" or "ff").

  python mw2tex.py extract common_mp.ff PAKDIR NAME [OUT.dds]
      Exports one texture as a DDS. Textures stored in the .ff need nothing else.
      Textures stored in paks need PAKDIR, a folder holding the game's imagefile1.pak ..
      imagefile4.pak (use . for the current folder); you get the largest size you have.

  python mw2tex.py put     ui_mp.ff NAME PICTURE OUTDIR
      Replaces texture NAME with any picture (PNG, JPG, DDS, ...). The picture is resized,
      given mipmaps and compressed to match the game texture automatically. Needs Pillow:
          python -m pip install pillow
      Writes OUTDIR\\ui_mp.ff (and OUTDIR\\imagefile5.pak when a pak texture changed).
      Run it again with the same OUTDIR to change more textures; each run keeps the
      earlier changes. Copy what it writes to _codxe\\zone\\ on the console.

  python mw2tex.py grow    ui_mp.ff NAME WIDTH HEIGHT PICTURE OUTDIR
      Like put, but first makes texture NAME bigger (or smaller), e.g. 512 256 to give a
      64x64 emblem room for 32 animation frames. Only for textures stored in the .ff
      (titles, emblems, menu pictures). WIDTH and HEIGHT must be powers of two.

  python mw2tex.py animate ui_mp.ff MATERIAL ROWS COLUMNS OUTDIR
      Makes material MATERIAL (an emblem name like cardicon_expert_ak47) play its texture
      as a flipbook: the texture is cut into ROWS x COLUMNS frames, read left to right, top
      to bottom. Put a picture laid out that way on the texture first (for a 64x64 emblem,
      2 2 gives four 32x32 frames). 1 1 turns the animation off again.

  python mw2tex.py replace ui_mp.ff PICDIR OUTDIR
      Same as put for every picture in PICDIR named after a texture (NAME.png, NAME.dds...).
      A DDS that already matches the game texture exactly is used as-is, without Pillow.

imagefile5.pak: if OUTDIR already has one (from another fastfile, or a stock one you copied
there), new textures are added to the end of it and old contents are kept.

The game texture decides the size; a picture with a different shape gets stretched to fit.
DXT1, DXT3, DXT5 and uncompressed 32-bit textures are tested; DXN and DXT5A (normal and
single-channel maps) are untested.

How it works (checked against airport.ff, imagefile3.pak, common_mp.ff and ui_mp.ff):
- The .ff starts "IWffu100" (unsigned) or "IWff0100" (signed), then a table of 12-byte
  entries (pak number, start, end), then two size words. Unsigned files follow with one
  zlib stream. Signed files have an 8 KB "IWffs100" header and an 8 KB hash block before
  every 2 MB of the zlib stream. Rebuilt files are always written unsigned; codxe turns
  off the signature check.
- Texture records are 0x70 bytes: D3D header, format word at 0x34, map type at 0x38,
  size at 0x40, pixel pointer at 0x48, four pak levels at 0x4C, name pointer at 0x6C.
- Pak textures: the n-th one uses table entries n*4 .. n*4+3, one per quality level. Each
  entry is one zlib chunk in imagefile<pak>.pak: the full-size level, each mip in its own
  4 KB aligned slice, and mips 16 pixels and smaller packed into one last slice.
- Textures stored in the .ff (menus, titles, emblems, camo previews) have one level, and
  their pixels follow the record (and its name) directly.
"""
import csv
import os
import re
import struct
import sys
import zlib

# gpu format -> (name, block size in pixels, bytes per block, DDS fourCCs)
FORMATS = {
    0x12: ("DXT1", 4, 8, (b"DXT1",)),
    0x13: ("DXT3", 4, 16, (b"DXT3", b"DXT2")),
    0x14: ("DXT5", 4, 16, (b"DXT5", b"DXT4")),
    0x31: ("DXN", 4, 16, (b"ATI2", b"BC5U")),
    0x3B: ("DXT5A", 4, 8, (b"ATI1", b"BC4U")),
    0x06: ("ARGB8", 1, 4, ()),  # uncompressed 32-bit; DDS A8R8G8B8
}
LEVELS_PER_IMAGE = 4
NEW_PAK = 5


# ---------------------------------------------------------------- fastfile


class FastFile:
    def __init__(self, path):
        self.path = path
        self.raw = open(path, "rb").read()
        magic = self.raw[:8]
        if magic not in (b"IWffu100", b"IWff0100"):
            sys.exit("%s is not an MW2 360 fastfile" % path)
        self.count = struct.unpack(">I", self.raw[0x19:0x1D])[0]
        self.table = [list(struct.unpack(">III", self.raw[0x1D + i * 12:0x29 + i * 12])) for i in range(self.count)]
        self.header_end = 0x1D + self.count * 12
        self.sizes = struct.unpack(">II", self.raw[self.header_end:self.header_end + 8])
        body = self.raw[self.header_end + 8:]
        self.signed = body[:8] == b"IWffs100"
        if self.signed:
            stream, i = bytearray(), 0x2000
            while i < len(body):
                i += 0x2000
                stream += body[i:i + 0x200000]
                i += 0x200000
            body = bytes(stream)
        self.zone = bytearray(zlib.decompressobj().decompress(body))
        self.zone_changed = False
        self._scan()

    def _scan(self):
        z = self.zone
        self.images = []
        streamed = 0
        # Every texture record has the same 16 bytes at 0x3C, except byte 0x47 (1 or 2).
        for m in re.finditer(rb"\x00{5}\x01\x00\x01\x00\x01\x01[\x01\x02]\x00{4}", z):
            o = m.start() - 0x3C
            if z[o + 0x38] not in (3, 4, 5):
                continue  # same bytes by chance, not a texture record
            self._add(o, pak=True, index=streamed)
            streamed += 1
        if streamed * LEVELS_PER_IMAGE != self.count:
            print("warning: %d pak textures but %d pak entries; pak textures may be mismatched"
                  % (streamed, self.count))
        # Textures stored in the .ff start with a live D3D texture header and point at their pixels.
        for m in re.finditer(re.escape(b"\x03\0\0\0\x01\0\0\0"), z):
            o = m.start()
            if o + 0x70 > len(z) or z[o + 0x48:o + 0x4C] != b"\xff" * 4 or z[o + 0x38] not in (3, 5) or z[o + 0x3B]:
                continue
            self._add(o, pak=False)

    def _add(self, o, pak, index=0):
        z = self.zone
        fmt = struct.unpack(">I", z[o + 0x34:o + 0x38])[0] & 0x3F
        data = o + 0x70
        if z[o + 0x6C:o + 0x70] == b"\xff" * 4:
            end = z.index(b"\0", data)
            name = z[data:end].decode("latin1")
            data = end + 1
        else:
            name = self._material_name(o)
        image = {"offset": o, "format": fmt, "map_type": z[o + 0x38], "pak": pak, "levels": []}
        if pak:
            image["name"] = name or "#%d" % index
            for k in range(LEVELS_PER_IMAGE):
                w, h, info = struct.unpack(">HHI", z[o + 0x4C + k * 8:o + 0x54 + k * 8])
                if w:
                    image["levels"].append({"level": k, "width": w, "height": h, "mips": info >> 26,
                                            "entry": index * LEVELS_PER_IMAGE + k})
        else:
            if name is None:
                return  # no name found; can't be picked by name
            w, h = struct.unpack(">HH", z[o + 0x40:o + 0x44])
            image["name"] = name
            image["levels"].append({"level": 0, "width": w, "height": h, "mips": z[o + 0x46], "data": data,
                                    "size": struct.unpack(">I", z[o + 0x3C:o + 0x40])[0]})
        self.images.append(image)

    def _material_name(self, o):
        """Name of the material that owns the texture, for textures whose own name isn't stored inline.

        Such a texture sits right after its material's texture slot (12 bytes ending in ffffffff),
        which sits right after the material's name. UI materials and their textures share names.
        """
        z = self.zone
        if o < 16 or z[o - 4:o] != b"\xff" * 4 or z[o - 13] != 0:
            return None
        end = o - 13
        start = end
        while start > 0 and 32 <= z[start - 1] < 127:
            start -= 1
        return z[start:end].decode("latin1") or None

    def find(self, name):
        for image in self.images:
            if image["name"].lower() == name.lower():
                return image
        return None

    def save(self, path):
        if not self.zone_changed and not self.signed:
            out = bytearray(self.raw)
            for i, entry in enumerate(self.table):
                struct.pack_into(">III", out, 0x1D + i * 12, *entry)
            open(path, "wb").write(bytes(out))
            return
        head = bytearray(self.raw[:self.header_end])
        head[:8] = b"IWffu100"
        for i, entry in enumerate(self.table):
            struct.pack_into(">III", head, 0x1D + i * 12, *entry)
        stream = zlib.compress(bytes(self.zone), 9)
        total = len(head) + 8 + len(stream)
        head += struct.pack(">II", total, total + self.sizes[1] - self.sizes[0])
        open(path, "wb").write(bytes(head) + stream)


# ---------------------------------------------------------------- xbox tiling (port of src/image/xenos_texture.cpp)


def _up(v, d):
    return (v + d - 1) // d


def _align(v, a):
    return _up(v, a) * a


def _pow2(v):
    r = 1
    while r < v:
        r <<= 1
    return r


def _log2ceil(v):
    r, c = 0, v - 1
    while c:
        c >>= 1
        r += 1
    return r


def _layout(width, height, mip, fmt):
    """Returns (width blocks, height blocks, stored width blocks, slice bytes)."""
    _, bw, bpb, _ = FORMATS[fmt]
    wb = max(1, _up(max(width >> mip, 1), bw))
    hb = max(1, _up(max(height >> mip, 1), bw))
    if mip == 0:
        pitch = _align(wb, 32) // 8 if bw > 1 else _align(width, 32) // 32
        row = max(1, _up(pitch << 5, bw)) * bpb
        sw, sh = row // bpb, _align(hb, 32)
    else:
        sw = _align(_up(max(_pow2(width) >> mip, 1), bw), 32)
        sh = _align(_up(max(_pow2(height) >> mip, 1), bw), 32)
        row = sw * bpb
    return wb, hb, sw, _align(row * sh, 4096)


def _l2(bpb):
    return (bpb // 4) + ((bpb // 2) >> (bpb // 4))


def _block_offset(x, y, sw, bpb):
    l2 = _l2(bpb)
    macro = ((y // 32) * (sw // 32)) << (l2 + 7)
    micro = ((y & 6) << 2) << l2
    base = macro + ((micro & ~0xF) << 1) + (micro & 0xF) + ((y & 8) << (3 + l2)) + ((y & 1) << 4)
    macro = (x // 32) << (l2 + 7)
    micro = (x & 7) << l2
    off = base + macro + ((micro & ~0xF) << 1) + (micro & 0xF)
    off = ((off & ~0x1FF) << 3) + ((off & 0x1C0) << 2) + (off & 0x3F) + ((y & 16) << 7) + \
        (((((y & 8) >> 2) + (x >> 3)) & 3) << 6)
    return (off >> l2) * bpb


def _mip_count(width, height):
    return _log2ceil(max(width, height)) + 1


def _plan(width, height, fmt):
    """Yields (mip, slice offset, stored width, x block offset, y block offset) for every mip."""
    _, bw, _, _ = FORMATS[fmt]
    tail = max(0, _log2ceil(min(width, height)) - 4)
    offsets, total = [], 0
    for mip in range(tail + 1):
        offsets.append(total)
        total += _layout(width, height, mip, fmt)[3]
    tail_sw = _layout(width, height, tail, fmt)[2]
    wide = _log2ceil(width) > _log2ceil(height)
    plan = []
    for mip in range(_mip_count(width, height)):
        if mip < tail:
            plan.append((mip, offsets[mip], _layout(width, height, mip, fmt)[2], 0, 0))
            continue
        packed = mip - tail
        if packed < 3:
            ox, oy = (0, (16 >> packed) // bw) if wide else ((16 >> packed) // bw, 0)
        else:
            # The smallest mips sit one block each along the long side, 1x1 nearest the corner.
            step = 1 << (_mip_count(width, height) - 1 - mip)
            ox, oy = (step, 0) if wide else (0, step)
        plan.append((mip, offsets[tail], tail_sw, ox, oy))
    return plan, total


def _swap(data, fmt):
    """Undo (or apply) the console's byte order: 16-bit swaps for DXT, 32-bit for ARGB8."""
    b = bytearray(data)
    if FORMATS[fmt][2] == 4 and FORMATS[fmt][1] == 1:
        b[0::4], b[1::4], b[2::4], b[3::4] = data[3::4], data[2::4], data[1::4], data[0::4]
    else:
        b[0::2], b[1::2] = data[1::2], data[0::2]
    return bytes(b)


def _single(width, height, fmt):
    sw, size = _layout(width, height, 0, fmt)[2:]
    return [(0, 0, sw, 0, 0)], size


def untile(blob, width, height, fmt, single=False):
    """Tiled level chunk -> list of linear mips (little-endian, like a DDS)."""
    _, _, bpb, _ = FORMATS[fmt]
    blob = _swap(blob, fmt)
    plan, _ = _single(width, height, fmt) if single else _plan(width, height, fmt)
    mips = []
    for mip, base, sw, ox, oy in plan:
        wb, hb, _, _ = _layout(width, height, mip, fmt)
        out = bytearray(wb * hb * bpb)
        for y in range(hb):
            for x in range(wb):
                src = base + _block_offset(x + ox, y + oy, sw, bpb)
                dst = (y * wb + x) * bpb
                out[dst:dst + bpb] = blob[src:src + bpb]
        mips.append(bytes(out))
    return mips


def tile(mips, width, height, fmt, single=False):
    """List of linear mips -> tiled level chunk (inverse of untile)."""
    _, _, bpb, _ = FORMATS[fmt]
    plan, total = _single(width, height, fmt) if single else _plan(width, height, fmt)
    blob = bytearray(total)
    for (mip, base, sw, ox, oy), data in zip(plan, mips):
        wb, hb, _, _ = _layout(width, height, mip, fmt)
        for y in range(hb):
            for x in range(wb):
                dst = base + _block_offset(x + ox, y + oy, sw, bpb)
                src = (y * wb + x) * bpb
                blob[dst:dst + bpb] = data[src:src + bpb]
    return _swap(bytes(blob), fmt)


# ---------------------------------------------------------------- DDS


def read_dds(path):
    data = open(path, "rb").read()
    if data[:4] != b"DDS " or len(data) < 128:
        raise ValueError("not a DDS file")
    height, width = struct.unpack("<II", data[12:20])
    mips = max(1, struct.unpack("<I", data[28:32])[0])
    fourcc = data[84:88]
    offset = 128
    if fourcc == b"DX10":
        raise ValueError("DX10 DDS files are not supported; save as legacy DXT1/DXT3/DXT5")
    pf_flags, bits = struct.unpack("<I", data[80:84])[0], struct.unpack("<I", data[88:92])[0]
    if not pf_flags & 4 and bits == 32 and data[92:108] == struct.pack("<4I", 0xFF0000, 0xFF00, 0xFF, 0xFF000000):
        fmt = 0x06
    else:
        fmt = next((f for f, v in FORMATS.items() if fourcc in v[3]), None)
    if fmt is None:
        raise ValueError("unsupported DDS compression %r" % fourcc)
    _, bw, bpb, _ = FORMATS[fmt]
    levels = []
    for mip in range(mips):
        size = max(1, _up(max(width >> mip, 1), bw)) * max(1, _up(max(height >> mip, 1), bw)) * bpb
        levels.append(data[offset:offset + size])
        offset += size
    if offset > len(data):
        raise ValueError("DDS file is truncated")
    return width, height, fmt, levels


def write_dds(path, width, height, fmt, mips):
    header = b"DDS " + struct.pack("<7I", 124, 0x000A1007, height, width, len(mips[0]), 0, len(mips))
    if fmt == 0x06:
        header += b"\0" * 44 + struct.pack("<2I4s5I", 32, 0x41, b"\0" * 4, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000)
    else:
        header += b"\0" * 44 + struct.pack("<2I4s5I", 32, 4, FORMATS[fmt][3][0], 0, 0, 0, 0, 0)
    header += struct.pack("<5I", 0x401008, 0, 0, 0, 0)
    open(path, "wb").write(header + b"".join(mips))


# ---------------------------------------------------------------- commands


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _supported(image):
    """Cube maps, and uncompressed textures with mipmaps, aren't handled (their smallest mips don't round-trip)."""
    if image["format"] not in FORMATS or image["map_type"] != 3:
        return False
    return not (FORMATS[image["format"]][1] == 1 and max(lv["mips"] for lv in image["levels"]) > 1)


def cmd_list(ff_path):
    ff = FastFile(ff_path)
    out = ff_path + ".images.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "format", "stored", "supported", "level", "width", "height", "mips", "pak", "start", "end"])
        for image in ff.images:
            for lv in image["levels"]:
                pak, start, end = ff.table[lv["entry"]] if image["pak"] else ("", "", "")
                w.writerow([image["name"], FORMATS.get(image["format"], ("?",))[0], "pak" if image["pak"] else "ff",
                            "yes" if _supported(image) else "no", lv["level"], lv["width"], lv["height"], lv["mips"],
                            pak, start, end])
    print("%d textures written to %s" % (len(ff.images), out))


def cmd_extract(ff_path, pak_dir, name, out_path=None):
    ff = FastFile(ff_path)
    image = ff.find(name)
    if not image:
        sys.exit("no texture named %s (run the list command to see names)" % name)
    fmt = image["format"]
    if not _supported(image):
        sys.exit("%s uses a format this tool can't handle yet" % name)
    out_path = out_path or _safe(image["name"]) + ".dds"
    if not image["pak"]:
        lv = image["levels"][0]
        blob = bytes(ff.zone[lv["data"]:lv["data"] + lv["size"]])
        mips = untile(blob, lv["width"], lv["height"], fmt, single=lv["mips"] == 1)
        write_dds(out_path, lv["width"], lv["height"], fmt, mips)
        print("wrote %s (%dx%d %s, %d mipmaps)" % (out_path, lv["width"], lv["height"], FORMATS[fmt][0], len(mips)))
        return
    for lv in sorted(image["levels"], key=lambda l: -l["width"] * l["height"]):
        pak, start, end = ff.table[lv["entry"]]
        pak_path = os.path.join(pak_dir, "imagefile%d.pak" % pak)
        if not os.path.exists(pak_path):
            print("skipping %dx%d: %s not found" % (lv["width"], lv["height"], pak_path))
            continue
        with open(pak_path, "rb") as fh:
            fh.seek(start)
            blob = zlib.decompress(fh.read(end - start))
        mips = untile(blob, lv["width"], lv["height"], fmt, single=lv["mips"] == 1)
        write_dds(out_path, lv["width"], lv["height"], fmt, mips)
        print("wrote %s (%dx%d %s, %d mipmaps)" % (out_path, lv["width"], lv["height"], FORMATS[fmt][0], len(mips)))
        return
    sys.exit("none of the paks for %s were found in %s" % (name, pak_dir))


# ---------------------------------------------------------------- converting any picture (needs Pillow)


def _rgb565(c):
    return ((c[0] * 31 + 127) // 255) << 11 | ((c[1] * 63 + 127) // 255) << 5 | ((c[2] * 31 + 127) // 255)


def _unpack565(v):
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _color_block(pixels, punch_through=False):
    """16 RGBA pixels -> 8-byte DXT1-style color block."""
    use = [p for p in pixels if not (punch_through and p[3] < 128)] or pixels
    n = float(len(use))
    mean = [sum(p[i] for p in use) / n for i in range(3)]
    cov = [[sum((p[i] - mean[i]) * (p[j] - mean[j]) for p in use) for j in range(3)] for i in range(3)]
    axis = [1.0, 1.0, 1.0]
    for _ in range(4):
        axis = [sum(cov[i][j] * axis[j] for j in range(3)) for i in range(3)]
        norm = max(abs(a) for a in axis) or 1.0
        axis = [a / norm for a in axis]
    proj = [sum((p[i] - mean[i]) * axis[i] for i in range(3)) for p in use]
    lo, hi = use[proj.index(min(proj))], use[proj.index(max(proj))]
    c0, c1 = _rgb565(hi), _rgb565(lo)
    transparent = punch_through and any(p[3] < 128 for p in pixels)
    if transparent:
        if c0 > c1:
            c0, c1 = c1, c0
    else:
        if c0 < c1:
            c0, c1 = c1, c0
        if c0 == c1:
            return struct.pack("<HHI", c0, c1, 0)
    a, b = _unpack565(c0), _unpack565(c1)
    if transparent or c0 <= c1:
        palette = [a, b, tuple((a[i] + b[i]) // 2 for i in range(3))]
    else:
        palette = [a, b, tuple((2 * a[i] + b[i]) // 3 for i in range(3)), tuple((a[i] + 2 * b[i]) // 3 for i in range(3))]
    bits = 0
    for k, p in enumerate(pixels):
        if transparent and p[3] < 128:
            index = 3
        else:
            index = min(range(len(palette)), key=lambda i: sum((p[c] - palette[i][c]) ** 2 for c in range(3)))
        bits |= index << (2 * k)
    return struct.pack("<HHI", c0, c1, bits)


def _alpha_block(values):
    """16 values -> 8-byte DXT5/BC4-style block."""
    a0, a1 = max(values), min(values)
    if a0 == a1:
        return struct.pack("<BB6s", a0, a1, b"\0" * 6)
    palette = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    bits = 0
    for k, v in enumerate(values):
        bits |= min(range(8), key=lambda i: abs(v - palette[i])) << (3 * k)
    return struct.pack("<BB", a0, a1) + bits.to_bytes(6, "little")


def _encode(rgba, width, height, fmt):
    """RGBA bytes -> linear data in the game's format (same layout a DDS stores)."""
    if fmt == 0x06:
        out = bytearray(rgba)
        out[0::4], out[2::4] = rgba[2::4], rgba[0::4]  # RGBA -> BGRA (A8R8G8B8 in memory)
        return bytes(out)
    out = bytearray()
    for by in range(0, max(height, 1), 4):
        for bx in range(0, max(width, 1), 4):
            pixels = []
            for y in range(4):
                for x in range(4):
                    i = (min(by + y, height - 1) * width + min(bx + x, width - 1)) * 4
                    pixels.append(tuple(rgba[i:i + 4]))
            if fmt == 0x12:
                out += _color_block(pixels, punch_through=True)
            elif fmt == 0x13:
                alpha = 0
                for k, p in enumerate(pixels):
                    alpha |= ((p[3] * 15 + 127) // 255) << (4 * k)
                out += alpha.to_bytes(8, "little") + _color_block(pixels)
            elif fmt == 0x14:
                out += _alpha_block([p[3] for p in pixels]) + _color_block(pixels)
            elif fmt == 0x3B:
                out += _alpha_block([p[0] for p in pixels])
            elif fmt == 0x31:
                out += _alpha_block([p[0] for p in pixels]) + _alpha_block([p[1] for p in pixels])
    return bytes(out)


def convert_picture(path, width, height, fmt, mip_count):
    """Any picture Pillow can open (PNG, JPG, DDS, ...) -> list of mips in the game's format."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("converting pictures needs Pillow; install it with:  python -m pip install pillow")
    picture = Image.open(path).convert("RGBA")
    if picture.size != (width, height):
        print("  resizing %s from %dx%d to %dx%d" % (os.path.basename(path), picture.size[0], picture.size[1],
                                                     width, height))
        picture = picture.resize((width, height), Image.LANCZOS)
    mips = []
    for mip in range(mip_count):
        w, h = max(width >> mip, 1), max(height >> mip, 1)
        level = picture if mip == 0 else picture.resize((w, h), Image.BOX)
        mips.append(_encode(level.tobytes(), w, h, fmt))
    return mips


# ---------------------------------------------------------------- replacing


class Output:
    """The rebuilt fastfile and imagefile5.pak in OUTDIR; keeps earlier changes made there."""

    def __init__(self, ff_path, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        self.ff_out = os.path.join(out_dir, os.path.basename(ff_path))
        if os.path.exists(self.ff_out) and os.path.abspath(self.ff_out) != os.path.abspath(ff_path):
            print("adding to the %s already in %s" % (os.path.basename(ff_path), out_dir))
            ff_path = self.ff_out
        self.ff = FastFile(ff_path)
        self.pak_path = os.path.join(out_dir, "imagefile%d.pak" % NEW_PAK)
        self.pak = bytearray(open(self.pak_path, "rb").read()) if os.path.exists(self.pak_path) \
            else bytearray(b"IWffu100\0\0\x01\x0d")
        self.pak_changed = False
        self.replaced = 0

    def put(self, image, source, convert=False):
        """Replaces one game texture with a DDS (exact match) or, with convert, any picture."""
        label = os.path.basename(source)
        if not _supported(image):
            print("skip %s: %s uses a format this tool can't handle yet" % (label, image["name"]))
            return False
        fmt = image["format"]
        largest = max(image["levels"], key=lambda l: l["width"] * l["height"])
        width, height = largest["width"], largest["height"]
        needed = 1 if largest["mips"] == 1 else _mip_count(width, height)
        mips = None
        if source.lower().endswith(".dds"):
            try:
                w, h, f, m = read_dds(source)
                if (w, h, f) == (width, height, fmt) and len(m) >= needed:
                    mips = m
                elif not convert:
                    print("  %s isn't %dx%d %s with %d mipmaps; converting it" % (label, width, height,
                                                                                FORMATS[fmt][0], needed))
            except ValueError:
                pass
        if mips is None:
            mips = convert_picture(source, width, height, fmt, needed)
        if not image["pak"]:
            blob = tile(mips, width, height, fmt, single=largest["mips"] == 1)
            if len(blob) != largest["size"]:
                print("skip %s: this texture's layout isn't supported (size %d, expected %d)"
                      % (label, len(blob), largest["size"]))
                return False
            data = largest["data"]
            self.ff.zone[data:data + len(blob)] = blob
            self.ff.zone_changed = True
        else:
            for lv in image["levels"]:
                first = _log2ceil(width // lv["width"])
                blob = tile(mips[first:], lv["width"], lv["height"], fmt, single=lv["mips"] == 1)
                start = len(self.pak)
                self.pak += zlib.compress(blob, 9)
                self.ff.table[lv["entry"]] = [NEW_PAK, start, len(self.pak)]
            self.pak_changed = True
        self.replaced += 1
        print("replaced %s with %s" % (image["name"], label))
        return True

    def save(self):
        if not self.replaced:
            sys.exit("nothing replaced; no files written")
        self.ff.save(self.ff_out)
        written = [self.ff_out]
        if self.pak_changed:
            open(self.pak_path, "wb").write(bytes(self.pak))
            written.append(self.pak_path)
        print("wrote %s; copy to _codxe\\zone\\ on the console" % " and ".join(written))


PICTURE_TYPES = (".dds", ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".gif", ".webp")


def cmd_replace(ff_path, pic_dir, out_dir):
    out = Output(ff_path, out_dir)
    for file_name in sorted(os.listdir(pic_dir)):
        base, ext = os.path.splitext(file_name)
        if ext.lower() not in PICTURE_TYPES:
            continue
        image = out.ff.find(base)
        if not image:
            print("skip %s: no texture with that name in %s" % (file_name, os.path.basename(ff_path)))
            continue
        out.put(image, os.path.join(pic_dir, file_name))
    out.save()


def cmd_put(ff_path, name, picture, out_dir):
    out = Output(ff_path, out_dir)
    image = out.ff.find(name)
    if not image:
        sys.exit("no texture named %s (run the list command to see names)" % name)
    out.put(image, picture, convert=True)
    out.save()


PHYSICAL_BLOCK = 1  # XFile block that holds in-file texture pixels (4 KB aligned per texture)


def grow_texture(ff, image, width, height):
    """Resizes an in-file texture's record and pixel data in place, shifting everything after it.

    The pixels live in the zone's physical block, which nothing else points into, so moving later
    pixel data only needs the zone size and the physical block size in the XFile header updated.
    The record's D3D fetch constant is stored little-endian: dword0 bits 22-30 = pitch in 32-texel
    units (width rounded up to 128 for DXT), dword2 = width-1 | (height-1) << 13.
    """
    if image["pak"]:
        sys.exit("%s is stored in a pak file; grow only works on textures stored in the .ff" % image["name"])
    if width & (width - 1) or height & (height - 1) or not (4 <= width <= 2048 and 4 <= height <= 2048):
        sys.exit("width and height must be powers of two from 4 to 2048")
    z, o, fmt = ff.zone, image["offset"], image["format"]
    lv = image["levels"][0]
    if lv["mips"] != 1:
        sys.exit("%s has mipmaps; grow only handles single-level textures" % image["name"])
    new_size = len(tile([b"\0" * (_up(width, FORMATS[fmt][1]) * _up(height, FORMATS[fmt][1]) * FORMATS[fmt][2])],
                        width, height, fmt, single=True))
    old_size = lv["size"]
    dword0, = struct.unpack("<I", z[o + 0x1C:o + 0x20])
    pitch = _align(width, 128 if FORMATS[fmt][1] == 4 else 32) // 32
    dword0 = (dword0 & ~(0x1FF << 22)) | (pitch << 22)
    struct.pack_into("<I", z, o + 0x1C, dword0)
    dword2, = struct.unpack("<I", z[o + 0x24:o + 0x28])
    dword2 = (dword2 & ~0x3FFFFFF) | (width - 1) | ((height - 1) << 13)
    struct.pack_into("<I", z, o + 0x24, dword2)
    struct.pack_into(">I", z, o + 0x3C, new_size)
    struct.pack_into(">HH", z, o + 0x40, width, height)
    data = lv["data"]
    z[data:data + old_size] = b"\0" * new_size
    delta = new_size - old_size
    physical = _align(new_size, 0x1000) - _align(old_size, 0x1000)
    total, = struct.unpack(">I", z[0:4])
    struct.pack_into(">I", z, 0, total + delta)
    block, = struct.unpack(">I", z[8 + PHYSICAL_BLOCK * 4:12 + PHYSICAL_BLOCK * 4])
    struct.pack_into(">I", z, 8 + PHYSICAL_BLOCK * 4, block + physical)
    for other in ff.images:
        for olv in other["levels"]:
            if "data" in olv and olv["data"] > data:
                olv["data"] += delta
        if other["offset"] > data:
            other["offset"] += delta
    lv.update(width=width, height=height, size=new_size)
    ff.zone_changed = True


def cmd_grow(ff_path, name, width, height, picture, out_dir):
    out = Output(ff_path, out_dir)
    image = out.ff.find(name)
    if not image:
        sys.exit("no texture named %s (run the list command to see names)" % name)
    grow_texture(out.ff, image, int(width), int(height))
    print("%s is now %sx%s" % (name, width, height))
    out.put(image, picture, convert=True)
    out.save()


def find_material(zone, name):
    """Offset of UI material NAME's header, which starts 0x58 bytes before its inline name (header plus
    its texture slot). The header starts with an inline name marker (ffffffff), then gameFlags,
    sortKey, texture atlas rows and columns."""
    hits = []
    for m in re.finditer(re.escape(b"\0" + name.encode("latin1") + b"\0"), zone):
        o = m.start() + 1 - 0x58
        if o >= 0 and zone[o:o + 4] == b"\xff" * 4 and 1 <= zone[o + 6] <= 16 and 1 <= zone[o + 7] <= 16:
            hits.append(o)
    return hits


def cmd_animate(ff_path, name, rows, columns, out_dir):
    rows, columns = int(rows), int(columns)
    if not (1 <= rows <= 16 and 1 <= columns <= 16):
        sys.exit("rows and columns must be 1 to 16")
    out = Output(ff_path, out_dir)
    hits = find_material(out.ff.zone, name)
    if not hits:
        sys.exit("no material named %s found" % name)
    for o in hits:
        out.ff.zone[o + 6] = rows
        out.ff.zone[o + 7] = columns
    out.ff.zone_changed = True
    out.replaced += 1
    print("%s now plays as %d x %d frames" % (name, rows, columns))
    out.save()


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "list":
        cmd_list(args[1])
    elif len(args) in (4, 5) and args[0] == "extract":
        cmd_extract(*args[1:])
    elif len(args) == 4 and args[0] == "replace":
        cmd_replace(*args[1:])
    elif len(args) == 7 and args[0] == "grow":
        cmd_grow(*args[1:])
    elif len(args) == 6 and args[0] == "animate":
        cmd_animate(*args[1:])
    elif len(args) == 5 and args[0] == "put":
        cmd_put(*args[1:])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
