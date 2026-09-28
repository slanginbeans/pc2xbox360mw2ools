"""MW2 Xbox 360 texture tool: list, export and replace textures in a fastfile.

Needs Python 3 only. Commands:

  python mw2tex.py list    airport.ff
      Writes airport.ff.images.csv listing every texture, its format and sizes.

  python mw2tex.py extract airport.ff PAKDIR NAME [OUT.dds]
      Exports one texture as a DDS (largest size you have the pak for), with its mipmaps.
      PAKDIR is a folder holding the game's imagefile1.pak .. imagefile4.pak.

  python mw2tex.py replace airport.ff DDSDIR OUTDIR
      For every NAME.dds in DDSDIR that matches a texture in the fastfile, writes the new
      texture into OUTDIR\\imagefile5.pak and writes OUTDIR\\airport.ff pointing at it.
      Copy both files to _codxe\\zone\\ on the console.
      If OUTDIR already has an imagefile5.pak (from another map, or a stock one you copied
      there), new textures are added to the end of it and old contents are kept.

Replacement DDS rules: same width, height and compression as the original (export it first
to check), saved with a full mipmap chain. DXT1, DXT3 and DXT5 are tested; DXN and DXT5A
(normal and single-channel maps) are untested.

How it works (checked against airport.ff and imagefile3.pak):
- The .ff starts "IWffu100", then a table of 12-byte entries (pak number, start, end).
  Texture i, quality level k uses entry i*4+k. The rest of the .ff is one zlib stream.
- Each entry is one zlib chunk in imagefile<pak>.pak holding the texture at that size
  in Xbox tiled layout: the full-size level, each mip in its own 4 KB aligned slice,
  and mips 16 pixels and smaller packed together into one last slice.
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
}
LEVELS_PER_IMAGE = 4
NEW_PAK = 5


# ---------------------------------------------------------------- fastfile


class FastFile:
    def __init__(self, path):
        self.path = path
        self.raw = open(path, "rb").read()
        if self.raw[:8] != b"IWffu100":
            sys.exit("%s is not an unsigned MW2 360 fastfile" % path)
        self.count = struct.unpack(">I", self.raw[0x19:0x1D])[0]
        self.table = [list(struct.unpack(">III", self.raw[0x1D + i * 12:0x29 + i * 12])) for i in range(self.count)]
        zone = zlib.decompress(self.raw[0x1D + self.count * 12 + 8:])
        pattern = re.compile(re.escape(b"\0" * 0x34) + b"(.{56})\xff\xff\xff\xff([ -~]{2,64})\x00", re.S)
        self.images = []
        for m in pattern.finditer(zone):
            body = m.group(1)
            if body[4] not in (1, 2, 3, 4, 5, 6):
                continue
            index = len(self.images)
            fmt = struct.unpack(">I", body[0:4])[0] & 0x3F
            levels = []
            for k in range(LEVELS_PER_IMAGE):
                w, h, info = struct.unpack(">HHI", body[0x18 + k * 8:0x20 + k * 8])
                if w:
                    levels.append({"level": k, "width": w, "height": h, "size": info & 0xFFFFFF,
                                   "entry": index * LEVELS_PER_IMAGE + k})
            self.images.append({"index": index, "name": m.group(2).decode(), "format": fmt, "map_type": body[4],
                                "levels": levels})
        if len(self.images) * LEVELS_PER_IMAGE != self.count:
            print("warning: %d textures but %d pak entries; this fastfile may not be supported"
                  % (len(self.images), self.count))

    def find(self, name):
        for image in self.images:
            if image["name"].lower() == name.lower():
                return image
        return None

    def save(self, path):
        out = bytearray(self.raw)
        for i, entry in enumerate(self.table):
            struct.pack_into(">III", out, 0x1D + i * 12, *entry)
        open(path, "wb").write(bytes(out))


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
        pitch = _align(wb, 32) // 8
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


def _swap16(data):
    b = bytearray(data)
    b[0::2], b[1::2] = data[1::2], data[0::2]
    return bytes(b)


def untile(blob, width, height, fmt):
    """Tiled level chunk -> list of linear mips (little-endian, like a DDS)."""
    _, _, bpb, _ = FORMATS[fmt]
    blob = _swap16(blob)
    plan, _ = _plan(width, height, fmt)
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


def tile(mips, width, height, fmt):
    """List of linear mips -> tiled level chunk (inverse of untile)."""
    _, _, bpb, _ = FORMATS[fmt]
    plan, total = _plan(width, height, fmt)
    blob = bytearray(total)
    for (mip, base, sw, ox, oy), data in zip(plan, mips):
        wb, hb, _, _ = _layout(width, height, mip, fmt)
        for y in range(hb):
            for x in range(wb):
                dst = base + _block_offset(x + ox, y + oy, sw, bpb)
                src = (y * wb + x) * bpb
                blob[dst:dst + bpb] = data[src:src + bpb]
    return _swap16(bytes(blob))


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
    fourcc = FORMATS[fmt][3][0]
    header = b"DDS " + struct.pack("<7I", 124, 0x000A1007, height, width, len(mips[0]), 0, len(mips))
    header += b"\0" * 44 + struct.pack("<2I4s5I", 32, 4, fourcc, 0, 0, 0, 0, 0)
    header += struct.pack("<5I", 0x401008, 0, 0, 0, 0)
    open(path, "wb").write(header + b"".join(mips))


# ---------------------------------------------------------------- commands


def cmd_list(ff_path):
    ff = FastFile(ff_path)
    out = ff_path + ".images.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["index", "name", "format", "level", "width", "height", "pak", "start", "end"])
        for image in ff.images:
            for lv in image["levels"]:
                pak, start, end = ff.table[lv["entry"]]
                w.writerow([image["index"], image["name"], FORMATS.get(image["format"], ("?",))[0], lv["level"],
                            lv["width"], lv["height"], pak, start, end])
    print("%d textures written to %s" % (len(ff.images), out))


def cmd_extract(ff_path, pak_dir, name, out_path=None):
    ff = FastFile(ff_path)
    image = ff.find(name)
    if not image:
        sys.exit("no texture named %s (run the list command to see names)" % name)
    if image["format"] not in FORMATS:
        sys.exit("%s uses a format this tool can't handle yet" % name)
    for lv in sorted(image["levels"], key=lambda l: -l["width"] * l["height"]):
        pak, start, end = ff.table[lv["entry"]]
        pak_path = os.path.join(pak_dir, "imagefile%d.pak" % pak)
        if not os.path.exists(pak_path):
            print("skipping %dx%d: %s not found" % (lv["width"], lv["height"], pak_path))
            continue
        with open(pak_path, "rb") as fh:
            fh.seek(start)
            blob = zlib.decompress(fh.read(end - start))
        mips = untile(blob, lv["width"], lv["height"], image["format"])
        out_path = out_path or re.sub(r'[<>:"/\\|?*]', "_", image["name"]) + ".dds"
        write_dds(out_path, lv["width"], lv["height"], image["format"], mips)
        print("wrote %s (%dx%d %s, %d mipmaps)" % (out_path, lv["width"], lv["height"],
                                                 FORMATS[image["format"]][0], len(mips)))
        return
    sys.exit("none of the paks for %s were found in %s" % (name, pak_dir))


def cmd_replace(ff_path, dds_dir, out_dir):
    ff = FastFile(ff_path)
    os.makedirs(out_dir, exist_ok=True)
    pak_path = os.path.join(out_dir, "imagefile%d.pak" % NEW_PAK)
    pak = bytearray(open(pak_path, "rb").read()) if os.path.exists(pak_path) else bytearray(ff.raw[:12])
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
        if fmt != image["format"]:
            print("skip %s: DDS is %s but the game texture is %s" % (
                file_name, FORMATS[fmt][0], FORMATS.get(image["format"], ("?",))[0]))
            continue
        largest = max(image["levels"], key=lambda l: l["width"] * l["height"])
        if (width, height) != (largest["width"], largest["height"]):
            print("skip %s: DDS is %dx%d but the game texture is %dx%d" % (
                file_name, width, height, largest["width"], largest["height"]))
            continue
        if len(mips) < _mip_count(width, height):
            print("skip %s: save it with mipmaps (has %d, needs %d)" % (file_name, len(mips), _mip_count(width, height)))
            continue
        for lv in image["levels"]:
            first = _log2ceil(width // lv["width"])
            blob = tile(mips[first:], lv["width"], lv["height"], fmt)
            chunk = zlib.compress(blob, 9)
            start = len(pak)
            pak += chunk
            ff.table[lv["entry"]] = [NEW_PAK, start, len(pak)]
        replaced += 1
        print("replaced %s (%d sizes)" % (name, len(image["levels"])))
    if not replaced:
        sys.exit("nothing replaced; no files written")
    open(pak_path, "wb").write(bytes(pak))
    ff_out = os.path.join(out_dir, os.path.basename(ff_path))
    ff.save(ff_out)
    print("wrote %s and %s; copy both to _codxe\\zone\\ on the console" % (ff_out, pak_path))


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
