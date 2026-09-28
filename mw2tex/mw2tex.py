"""MW2 Xbox 360 texture tool: list, export and replace textures in a fastfile.

Needs Python 3 only. Works on map fastfiles (like mp_rust.ff) and on common_mp.ff and
ui_mp.ff, which hold camos, titles and emblems. Commands:

  python mw2tex.py list    common_mp.ff
      Writes common_mp.ff.images.csv listing every texture: name, format, size, and
      where its pixels are stored ("pak" or "ff").

  python mw2tex.py extract common_mp.ff PAKDIR NAME [OUT.dds]
      Exports one texture as a DDS. Textures stored in the .ff need nothing else.
      Textures stored in paks need PAKDIR, a folder holding the game's imagefile1.pak ..
      imagefile4.pak (use . for the current folder); you get the largest size you have.

  python mw2tex.py replace common_mp.ff DDSDIR OUTDIR
      For every NAME.dds in DDSDIR that matches a texture in the fastfile, writes a new
      OUTDIR\\common_mp.ff (and OUTDIR\\imagefile5.pak when a pak texture changed).
      Copy what it writes to _codxe\\zone\\ on the console.
      If OUTDIR already has an imagefile5.pak (from another fastfile, or a stock one you
      copied there), new textures are added to the end of it and old contents are kept.

Replacement DDS rules: same width, height and compression as the original (export it first
to check), and with mipmaps if the exported file had them. DXT1, DXT3, DXT5 and uncompressed
32-bit (A8R8G8B8) are tested; DXN and DXT5A (normal and single-channel maps) are untested.

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
        # Every texture record has the same 16 bytes at 0x3C.
        for m in re.finditer(re.escape(b"\0\0\0\0\0\x01\0\x01\0\x01\x01\x01\0\0\0\0"), z):
            o = m.start() - 0x3C
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


def cmd_replace(ff_path, dds_dir, out_dir):
    ff = FastFile(ff_path)
    os.makedirs(out_dir, exist_ok=True)
    pak_path = os.path.join(out_dir, "imagefile%d.pak" % NEW_PAK)
    pak = bytearray(open(pak_path, "rb").read()) if os.path.exists(pak_path) else bytearray(b"IWffu100\0\0\x01\x0d")
    pak_changed = False
    replaced = 0
    for file_name in sorted(os.listdir(dds_dir)):
        if not file_name.lower().endswith(".dds"):
            continue
        name = file_name[:-4]
        image = ff.find(name)
        if not image:
            print("skip %s: no texture with that name in %s" % (file_name, os.path.basename(ff_path)))
            continue
        try:
            width, height, fmt, mips = read_dds(os.path.join(dds_dir, file_name))
        except ValueError as e:
            print("skip %s: %s" % (file_name, e))
            continue
        if not _supported(image):
            print("skip %s: the game texture uses a format this tool can't handle yet" % file_name)
            continue
        if fmt != image["format"]:
            print("skip %s: DDS is %s but the game texture is %s" % (
                file_name, FORMATS[fmt][0], FORMATS.get(image["format"], ("?",))[0]))
            continue
        largest = max(image["levels"], key=lambda l: l["width"] * l["height"])
        if (width, height) != (largest["width"], largest["height"]):
            print("skip %s: DDS is %dx%d but the game texture is %dx%d" % (
                file_name, width, height, largest["width"], largest["height"]))
            continue
        needed = 1 if largest["mips"] == 1 else _mip_count(width, height)
        if len(mips) < needed:
            print("skip %s: save it with mipmaps (has %d, needs %d)" % (file_name, len(mips), needed))
            continue
        if not image["pak"]:
            data = largest["data"]
            blob = tile(mips, width, height, fmt, single=largest["mips"] == 1)
            if len(blob) != largest["size"]:
                print("skip %s: this texture's layout isn't supported (size %d, expected %d)"
                      % (file_name, len(blob), largest["size"]))
                continue
            ff.zone[data:data + len(blob)] = blob
            ff.zone_changed = True
            replaced += 1
            print("replaced %s" % name)
            continue
        for lv in image["levels"]:
            first = _log2ceil(width // lv["width"])
            blob = tile(mips[first:], lv["width"], lv["height"], fmt, single=lv["mips"] == 1)
            chunk = zlib.compress(blob, 9)
            start = len(pak)
            pak += chunk
            ff.table[lv["entry"]] = [NEW_PAK, start, len(pak)]
        pak_changed = True
        replaced += 1
        print("replaced %s (%d sizes)" % (name, len(image["levels"])))
    if not replaced:
        sys.exit("nothing replaced; no files written")
    ff_out = os.path.join(out_dir, os.path.basename(ff_path))
    ff.save(ff_out)
    written = [ff_out]
    if pak_changed:
        open(pak_path, "wb").write(bytes(pak))
        written.append(pak_path)
    print("wrote %s; copy to _codxe\\zone\\ on the console" % " and ".join(written))


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "list":
        cmd_list(args[1])
    elif len(args) in (4, 5) and args[0] == "extract":
        cmd_extract(*args[1:])
    elif len(args) == 4 and args[0] == "replace":
        cmd_replace(*args[1:])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
