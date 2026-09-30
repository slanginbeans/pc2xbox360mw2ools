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

  python mw2tex.py flipbook ui_mp.ff NAME ANIMATION.gif OUTDIR [FRAMES]
      Turns menu picture NAME (a title, emblem or other picture stored in the .ff) into an
      animation of the GIF (or animated WebP/PNG). FRAMES is 2, 4, 8 ... 256; without it, the
      smallest count that fits every GIF frame. The texture is made bigger to hold the frames
      and its material is set to play them. Frames keep the picture's size while they fit in
      2048x2048, then get smaller. The game's frame speed is fixed: more frames, longer loop.

  python mw2tex.py frames  ui_mp.ff NAME
      Lists the frame counts NAME can play, with the frame size and texture size of each.

  python mw2tex.py grow    ui_mp.ff NAME WIDTH HEIGHT PICTURE OUTDIR
      Like put, but first makes texture NAME bigger (or smaller), e.g. 512 256 to give a
      64x64 emblem room for 32 animation frames. Only for textures stored in the .ff
      (titles, emblems, menu pictures). WIDTH and HEIGHT must be powers of two.

  python mw2tex.py animate ui_mp.ff MATERIAL ROWS COLUMNS OUTDIR
      Makes material MATERIAL (an emblem name like cardicon_expert_ak47; a texture name
      works too) play its texture as a flipbook: the texture is cut into ROWS x COLUMNS
      frames, read left to right, top to bottom. Put a picture laid out that way on the texture first (for a 64x64 emblem,
      2 2 gives four 32x32 frames). 1 1 turns the animation off again.

  python mw2tex.py maps    ui_mp.ff OUTDIR [mp_rust.ff ...]
      In a match, emblems and titles come from each map's own copy, not ui_mp.ff. This copies
      every texture you changed in OUTDIR\\ui_mp.ff into the map files (all mp_*.ff next to
      ui_mp.ff if none are named) and writes them to OUTDIR with imagefile5.pak. Animated
      titles and emblems are copied whole and set to animate in the maps too.

  python mw2tex.py replace ui_mp.ff PICDIR OUTDIR
      Same as put for every picture in PICDIR named after a texture (NAME.png, NAME.dds...).
      A DDS that already matches the game texture exactly is used as-is, without Pillow.

  python mw2tex.py tables  code_post_gfx_mp.ff [OUTDIR]
  python mw2tex.py buildtables TABLEDIR [OUT.ff]
      Export the game's tables (calling card titles, emblems, unlocks...) as .csv files, and
      pack edited ones into codxe_patch_mp.ff. Same as mw2zone.py tables / build.

  python mw2tex.py pictures ui_mp.ff code_post_gfx_mp.ff
      Lists which titles and emblems share a picture, and the pictures no title or emblem
      uses. To give one of the sharing titles its own picture, point its table row at an
      unused picture (the table editor in mw2tex_gui.py does this for you).

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
import glob
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
        self.stream = body
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
            image["paks"] = sorted({self.table[lv["entry"]][0] for lv in image["levels"]
                                    if lv["entry"] < len(self.table)})
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
        # Only the pak table changed: reuse the compressed data as is (fast for big map files).
        stream = zlib.compress(bytes(self.zone), 9) if self.zone_changed else self.stream
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


def top_mip_only(image, lv):
    """Pak textures stream in levels. The smallest level holds its whole mip chain, but each
    bigger level holds only its own top mip: the smaller mips come from the levels before it.
    (Writing a whole chain there overflows the game's buffer: "disc unreadable" on the console.)"""
    return image["pak"] and lv is not image["levels"][0]


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
    open(path, "wb").write(dds_bytes(width, height, fmt, mips))


def dds_bytes(width, height, fmt, mips):
    header = b"DDS " + struct.pack("<7I", 124, 0x000A1007, height, width, len(mips[0]), 0, len(mips))
    if fmt == 0x06:
        header += b"\0" * 44 + struct.pack("<2I4s5I", 32, 0x41, b"\0" * 4, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000)
    else:
        header += b"\0" * 44 + struct.pack("<2I4s5I", 32, 4, FORMATS[fmt][3][0], 0, 0, 0, 0, 0)
    header += struct.pack("<5I", 0x401008, 0, 0, 0, 0)
    return header + b"".join(mips)


# ---------------------------------------------------------------- commands


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name)


CUBE = 5  # map type of cube maps (skyboxes): six square faces stored one after another


def is_skybox(image):
    """A map's sky: a cube map without mipmaps. (Reflection probes are cube maps too, but have
    mipmaps and names starting with *.)"""
    return image["map_type"] == CUBE and not image["name"].startswith("*") \
        and all(lv["mips"] == 1 for lv in image["levels"])


def _supported(image):
    """Uncompressed textures with mipmaps aren't handled (their smallest mips don't round-trip), nor
    are cube maps other than skyboxes."""
    if image["format"] not in FORMATS:
        return False
    if image["map_type"] == CUBE:
        return is_skybox(image)
    if image["map_type"] != 3:
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
        mips = untile(blob, lv["width"], lv["height"], fmt, single=lv["mips"] == 1 or top_mip_only(image, lv))
        write_dds(out_path, lv["width"], lv["height"], fmt, mips)
        print("wrote %s (%dx%d %s, %d mipmaps)" % (out_path, lv["width"], lv["height"], FORMATS[fmt][0], len(mips)))
        return
    sys.exit("none of the paks for %s were found in %s" % (name, pak_dir))


def decode_texture(ff, image, pak_dir=".", max_side=256, unpack=True):
    """Pillow RGBA picture of a texture, or None when its format or pak file isn't available.

    Pak textures use the largest level no bigger than max_side that's in a pak we have (else the
    smallest we have). DXT1 textures with an empty blue channel keep gray in red and
    transparency in green; they're shown that way.
    """
    from PIL import Image
    import io
    fmt = image["format"]
    if fmt not in FORMATS:
        return None
    if image["pak"]:
        have = []
        for lv in image["levels"]:
            pak, start, end = ff.table[lv["entry"]]
            path = os.path.join(pak_dir, "imagefile%d.pak" % pak)
            if os.path.exists(path):
                have.append((lv, path, start, end))
        if not have:
            return None
        small = [h for h in have if max(h[0]["width"], h[0]["height"]) <= max_side]
        lv, path, start, end = max(small, key=lambda h: h[0]["width"]) if small else \
            min(have, key=lambda h: h[0]["width"])
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = zlib.decompress(fh.read(end - start))
    else:
        lv = image["levels"][0]
        blob = bytes(ff.zone[lv["data"]:lv["data"] + lv["size"]])
    if image["map_type"] == CUBE:
        return skybox_strip(blob, lv["width"], lv["height"], fmt, max_side)
    mips = untile(blob, lv["width"], lv["height"], fmt, single=lv["mips"] == 1 or top_mip_only(image, lv))
    picture = Image.open(io.BytesIO(dds_bytes(lv["width"], lv["height"], fmt, mips[:1]))).convert("RGBA")
    if unpack and gray_alpha_packed(image, picture):
        r, g, b, a = picture.split()
        picture = Image.merge("RGBA", (r, r, r, g))
    return picture


def gray_alpha_packed(image, picture):
    """Some DXT1 menu pictures (cardicon_skull_black, cardtitle_camo_arctic, ...) keep gray in red
    and transparency in green, with blue empty; the menu shader unpacks them."""
    return image["format"] == 0x12 and not image["pak"] and picture.getchannel("B").getextrema()[1] == 0


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


def skybox_strip(blob, width, height, fmt, max_side=256):
    """The six faces of a skybox side by side, as the game stores them (each face turned the way
    the game keeps it: the 5th face is the sky straight up, the 6th the ground straight down)."""
    from PIL import Image
    import io
    face = len(blob) // 6
    side = min(width, max_side)
    strip = Image.new("RGBA", (6 * side, side))
    for f in range(6):
        mips = untile(blob[f * face:(f + 1) * face], width, height, fmt, single=True)
        picture = Image.open(io.BytesIO(dds_bytes(width, height, fmt, mips[:1]))).convert("RGBA")
        strip.paste(picture.resize((side, side), Image.LANCZOS), (f * side, 0))
    return strip


def skybox_faces(path, work_dir):
    """A dropped picture -> six face pictures. A strip six times as wide as it is tall is cut into
    the six faces in the order the picker shows them; any other picture goes on all six sides."""
    from PIL import Image
    picture = Image.open(path).convert("RGBA")
    w, h = picture.size
    if abs(w / float(h) - 6) < 0.2:
        faces = [picture.crop((f * w // 6, 0, (f + 1) * w // 6, h)) for f in range(6)]
    else:
        faces = [picture] * 6
    paths = []
    for f, face in enumerate(faces):
        paths.append(os.path.join(work_dir, "skybox_face%d.png" % f))
        face.save(paths[-1])
    return paths


def convert_picture(path, width, height, fmt, mip_count, packed=False):
    """Any picture Pillow can open (PNG, JPG, DDS, ...) -> list of mips in the game's format.

    packed: store gray in red and transparency in green (see gray_alpha_packed)."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("converting pictures needs Pillow; install it with:  python -m pip install pillow")
    picture = Image.open(path).convert("RGBA")
    if packed:
        zero = Image.new("L", picture.size, 0)
        picture = Image.merge("RGBA", (picture.convert("L"), picture.getchannel("A"), zero,
                                       Image.new("L", picture.size, 255)))
        print("  %s keeps gray in red and transparency in green; converted the picture to match"
              % os.path.basename(path))
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
        if image["map_type"] == CUBE:
            face_paths = skybox_faces(source, os.path.dirname(self.pak_path))
            for lv in image["levels"]:
                blob = b"".join(tile(convert_picture(f, lv["width"], lv["height"], fmt, 1), lv["width"], lv["height"],
                                     fmt, single=True) for f in face_paths)
                if image["pak"]:
                    start = len(self.pak)
                    self.pak += zlib.compress(blob, 9)
                    self.ff.table[lv["entry"]] = [NEW_PAK, start, len(self.pak)]
                    self.pak_changed = True
                else:
                    self.ff.zone[lv["data"]:lv["data"] + len(blob)] = blob
                    self.ff.zone_changed = True
            for f in face_paths:
                os.remove(f)
            self.replaced += 1
            print("replaced %s with %s" % (image["name"], label))
            return True
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
            packed = image.get("packed", False)
            if fmt == 0x12 and not image["pak"] and "packed" not in image:
                try:
                    packed = gray_alpha_packed(image, decode_texture(self.ff, image, unpack=False))
                except Exception:
                    packed = False
            mips = convert_picture(source, width, height, fmt, needed, packed)
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
                top = top_mip_only(image, lv)
                blob = tile(mips[first:first + 1] if top else mips[first:], lv["width"], lv["height"], fmt,
                            single=lv["mips"] == 1 or top)
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
    if fmt == 0x12 and "packed" not in image:  # decide now: the old pixels are cleared below
        try:
            image["packed"] = gray_alpha_packed(image, decode_texture(ff, image, unpack=False))
        except Exception:
            image["packed"] = False
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


def material_of_image(ff, image):
    """Header offset of the UI material whose texture slot holds this in-file texture, or None.

    The texture record follows its material's inline name and 12-byte texture slot. Usually the
    names match; cardicon_nvg_star belongs to material cardicon_iw.
    """
    name = ff._material_name(image["offset"])
    if not name:
        return None
    o = image["offset"] - 12 - len(name) - 1 - 0x58
    z = ff.zone
    if o >= 0 and z[o:o + 4] == b"\xff" * 4 and 1 <= z[o + 6] <= 16 and 1 <= z[o + 7] <= 16:
        return o
    return None


def material_images(ff):
    """{material name: texture name} for the UI materials in FF. Each UI material stores its own
    texture right after it, so the texture's material is the name just before it."""
    out = {}
    for image in ff.images:
        if not image["pak"]:
            name = ff._material_name(image["offset"])
            if name and material_of_image(ff, image) is not None:
                out.setdefault(name, image["name"])
    return out


def set_atlas(ff, name, rows, columns):
    """Sets texture atlas rows/columns on material NAME, or on the material that uses texture NAME."""
    hits = find_material(ff.zone, name)
    if not hits:
        image = ff.find(name)
        o = material_of_image(ff, image) if image and not image["pak"] else None
        hits = [o] if o is not None else []
    for o in hits:
        ff.zone[o + 6] = rows
        ff.zone[o + 7] = columns
    if hits:
        ff.zone_changed = True
    return len(hits)


FLIPBOOK = (4, 8, 64)  # rows, columns, frame size: the layout of the stock animated emblems
FRAME_COUNTS = (2, 4, 8, 16, 32, 64, 128, 256)
MAX_SHEET = 2048  # largest texture side the game takes; rows and columns go up to 16 each


def flipbook_base(ff, image):
    """Frame size to start from: the texture's size (one frame of it when it already animates),
    rounded up to powers of two so the whole sheet stays a power of two."""
    lv = image["levels"][0]
    width, height = lv["width"], lv["height"]
    o = material_of_image(ff, image)
    if o is not None:
        width, height = max(4, width // ff.zone[o + 7]), max(4, height // ff.zone[o + 6])
    return max(4, _pow2(width)), max(4, _pow2(height))


def flipbook_options(ff, image):
    """Frame counts this texture can play, each as {frames, rows, columns, frame, sheet, bytes}.

    Frames keep the texture's own size while the sheet fits in 2048x2048; beyond that they're
    halved (then quartered) to make room. Empty when the texture can't animate: it has to be a
    menu picture stored in the .ff with its own material (titles, emblems, other menu pictures).
    """
    if image["pak"] or image["levels"][0]["mips"] != 1 or image["format"] not in FORMATS \
            or material_of_image(ff, image) is None:
        return []
    fw, fh = flipbook_base(ff, image)
    _, block, block_bytes, _ = FORMATS[image["format"]]
    options = []
    for count in FRAME_COUNTS:
        for scale in (1, 2, 4):
            w, h = max(4, fw // scale), max(4, fh // scale)
            layouts = []
            for rows in (1, 2, 4, 8, 16):
                columns = count // rows
                if 1 <= columns <= 16 and columns * w <= MAX_SHEET and rows * h <= MAX_SHEET:
                    layouts.append((max(columns * w, rows * h), rows * h, rows, columns))
            if layouts:
                _, _, rows, columns = min(layouts)
                sw, sh = columns * w, rows * h
                options.append({"frames": count, "rows": rows, "columns": columns, "frame": (w, h),
                                "sheet": (sw, sh), "bytes": _up(sw, block) * _up(sh, block) * block_bytes})
                break
    return options


def flipbook_option(ff, image, frames):
    """The option for FRAMES frames (or the nearest one below it)."""
    options = flipbook_options(ff, image)
    fits = [o for o in options if o["frames"] <= int(frames)]
    return fits[-1] if fits else (options[0] if options else None)


def make_flipbook(picture, rows=FLIPBOOK[0], columns=FLIPBOOK[1], frame=FLIPBOOK[2]):
    """An animated GIF/WebP/PNG -> Pillow image laid out as rows x columns frames.

    FRAME is the frame size: one number for square frames or (width, height).
    The game plays every frame for the same time, so the source frames are spread evenly over all
    the slots (a 4-frame GIF shows each frame 8 times in a 32-slot sheet).
    """
    from PIL import Image, ImageSequence
    fw, fh = (frame, frame) if isinstance(frame, int) else frame
    frames = [f.convert("RGBA").copy() for f in ImageSequence.Iterator(Image.open(picture))]
    slots = rows * columns
    sheet = Image.new("RGBA", (columns * fw, rows * fh))
    for i in range(slots):
        f = frames[i * len(frames) // slots].resize((fw, fh), Image.LANCZOS)
        sheet.paste(f, ((i % columns) * fw, (i // columns) * fh))
    return sheet


def frame_count(picture):
    try:
        from PIL import Image
        return getattr(Image.open(picture), "n_frames", 1)
    except Exception:
        return 1


def put_flipbook(out, image, picture, work_dir, frames=FLIPBOOK[0] * FLIPBOOK[1]):
    """Turns an in-file menu picture into a FRAMES-frame animation of PICTURE (an animated GIF,
    WebP or PNG). The texture is resized to hold every frame and its material set to play them.
    Returns the option used, or None when the texture can't animate or its format isn't supported."""
    option = flipbook_option(out.ff, image, frames)
    if option is None:
        return None
    width, height = option["sheet"]
    lv = image["levels"][0]
    if (lv["width"], lv["height"]) != (width, height):
        grow_texture(out.ff, image, width, height)
    sheet_path = os.path.join(work_dir, _safe(image["name"]) + "_flipbook.png")
    make_flipbook(picture, option["rows"], option["columns"], option["frame"]).save(sheet_path)
    if not out.put(image, sheet_path, convert=True):
        return None
    if not set_atlas(out.ff, image["name"], option["rows"], option["columns"]):
        print("  warning: couldn't find the material for %s, so it won't animate" % image["name"])
    return option


def cmd_flipbook(ff_path, name, picture, out_dir, frames=None):
    out = Output(ff_path, out_dir)
    image = out.ff.find(name)
    if not image:
        sys.exit("no texture named %s (run the list command to see names)" % name)
    options = flipbook_options(out.ff, image)
    if not options:
        sys.exit("%s can't animate: only menu pictures stored in the .ff (titles, emblems...) can" % name)
    if frames is None:
        have = frame_count(picture)
        frames = next((o["frames"] for o in options if o["frames"] >= have), options[-1]["frames"])
    option = put_flipbook(out, image, picture, out_dir, frames)
    if option is None:
        sys.exit("%s's format isn't supported" % name)
    print("%s now plays %s as %d frames of %dx%d (texture %dx%d)" % (
        name, os.path.basename(picture), option["frames"], option["frame"][0], option["frame"][1],
        option["sheet"][0], option["sheet"][1]))
    out.save()


def cmd_frames(ff_path, name):
    ff = FastFile(ff_path)
    image = ff.find(name)
    if not image:
        sys.exit("no texture named %s (run the list command to see names)" % name)
    options = flipbook_options(ff, image)
    if not options:
        sys.exit("%s can't animate: only menu pictures stored in the .ff (titles, emblems...) can" % name)
    for o in options:
        print("%4d frames: %dx%d each, texture %dx%d (%d rows x %d columns), %.1f MB" % (
            o["frames"], o["frame"][0], o["frame"][1], o["sheet"][0], o["sheet"][1], o["rows"], o["columns"],
            o["bytes"] / 1048576.0))


def cmd_animate(ff_path, name, rows, columns, out_dir):
    rows, columns = int(rows), int(columns)
    if not (1 <= rows <= 16 and 1 <= columns <= 16):
        sys.exit("rows and columns must be 1 to 16")
    out = Output(ff_path, out_dir)
    if not set_atlas(out.ff, name, rows, columns):
        sys.exit("no material or emblem texture named %s found" % name)
    out.replaced += 1
    print("%s now plays as %d x %d frames" % (name, rows, columns))
    out.save()


# ---------------------------------------------------------------- copying changes to map files

# Card pictures (emblems, titles) in a match come from the map's own copies, not ui_mp.ff.


def changed_textures(stock, built):
    """Textures stored in the .ff whose pixels differ between the stock and the rebuilt fastfile."""
    changed = []
    for image in built.images:
        if image["pak"]:
            continue
        lv = image["levels"][0]
        pixels = bytes(built.zone[lv["data"]:lv["data"] + lv["size"]])
        old = stock.find(image["name"])
        if old and not old["pak"]:
            o = old["levels"][0]
            if (o["width"], o["height"], old["format"]) == (lv["width"], lv["height"], image["format"]) \
                    and bytes(stock.zone[o["data"]:o["data"] + o["size"]]) == pixels:
                continue
        changed.append(image)
    return changed


def resize_pak_level(ff, image, level, width, height, size):
    """Sets the size of one pak level of a texture. Pak texture records keep each level's width,
    height and info (mip count in the top 6 bits, tiled data size in the rest) at 0x4C + level * 8;
    the game builds the D3D header from these when it loads the texture."""
    o = image["offset"] + 0x4C + level["level"] * 8
    info, = struct.unpack(">I", ff.zone[o + 4:o + 8])
    struct.pack_into(">HHI", ff.zone, o, width, height, (info & ~0x3FFFFFF) | size)
    level.update(width=width, height=height)
    ff.zone_changed = True


def _copy_blobs(built, image, target, work_dir, map_ff=None):
    """Tiled pixel data for each level of TARGET, made from BUILT's version of the texture.

    Returns (list of (level, blob), note). Same size and layout: the pixels are copied exactly.
    An animated menu picture (a flipbook) is copied whole when the map's copy is a single-level pak
    texture: that level is resized to the flipbook's size and the map's own copy of the material is
    set to play the same rows and columns. Otherwise the picture is decoded and converted to fit,
    and a flipbook keeps its first frame.
    """
    lv = image["levels"][0]
    fmt = target["format"]
    note = None
    o = material_of_image(built, image)
    atlas = (built.zone[o + 6], built.zone[o + 7]) if o is not None else (1, 1)
    if map_ff is not None and atlas != (1, 1) and target["pak"] and len(target["levels"]) == 1 \
            and target["levels"][0]["mips"] == 1 and lv["mips"] == 1 and fmt == image["format"]:
        material = built._material_name(image["offset"])
        hits = find_material(map_ff.zone, material) if material else []
        if hits:
            tl = target["levels"][0]
            blob = bytes(built.zone[lv["data"]:lv["data"] + lv["size"]])
            resize_pak_level(map_ff, target, tl, lv["width"], lv["height"], len(blob))
            for h in hits:
                map_ff.zone[h + 6], map_ff.zone[h + 7] = atlas
            return [(tl, blob)], "animated (%d frames)" % (atlas[0] * atlas[1])
    exact = []
    for tl in target["levels"]:
        if (tl["width"], tl["height"], tl["mips"], fmt) == (lv["width"], lv["height"], lv["mips"], image["format"]):
            exact.append((tl, bytes(built.zone[lv["data"]:lv["data"] + lv["size"]])))
    if len(exact) == len(target["levels"]):
        return exact, note
    picture = decode_texture(built, image, max_side=4096, unpack=False)
    if picture is None:
        return None, "format not supported"
    largest = max(target["levels"], key=lambda l: l["width"] * l["height"])
    o = material_of_image(built, image)
    if o is not None and (built.zone[o + 6] > 1 or built.zone[o + 7] > 1):
        rows, columns = built.zone[o + 6], built.zone[o + 7]
        picture = picture.crop((0, 0, picture.size[0] // columns, picture.size[1] // rows))
        note = "shows the first frame of the animation"
    path = os.path.join(work_dir, _safe(image["name"]) + "_map.png")
    picture.save(path)
    width, height = largest["width"], largest["height"]
    needed = 1 if largest["mips"] == 1 else _mip_count(width, height)
    mips = convert_picture(path, width, height, fmt, needed)
    os.remove(path)
    blobs = []
    for tl in target["levels"]:
        first = _log2ceil(width // tl["width"])
        top = top_mip_only(target, tl)
        blobs.append((tl, tile(mips[first:first + 1] if top else mips[first:], tl["width"], tl["height"], fmt,
                               single=tl["mips"] == 1 or top)))
    return blobs, note


def sync_maps(stock_path, built_path, map_paths, out_dir, log=print):
    """Copies every texture changed in BUILT_PATH (vs STOCK_PATH) into the map fastfiles that carry it.

    Map copies live in imagefile paks, so the new pixels go into OUTDIR/imagefile5.pak once and
    each map's table is pointed at them. Returns the files written.
    """
    built = FastFile(built_path)
    changed = changed_textures(FastFile(stock_path), built)
    if not changed or not map_paths:
        return []
    pak_path = os.path.join(out_dir, "imagefile%d.pak" % NEW_PAK)
    pak = bytearray(open(pak_path, "rb").read()) if os.path.exists(pak_path) \
        else bytearray(b"IWffu100\0\0\x01\x0d")
    chunks = {}
    notes = {}
    written = []
    for map_path in map_paths:
        dst = os.path.join(out_dir, os.path.basename(map_path))
        source = dst if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(map_path) else map_path
        ff = FastFile(source)
        count = 0
        for image in changed:
            target = ff.find(image["name"])
            if not target or not _supported(target):
                continue
            blobs, note = _copy_blobs(built, image, target, out_dir, ff)
            if blobs is None:
                notes[image["name"]] = note
                continue
            if note:
                notes[image["name"]] = note
            if target["pak"]:
                for tl, blob in blobs:
                    if blob not in chunks:
                        start = len(pak)
                        pak += zlib.compress(blob, 9)
                        chunks[blob] = (start, len(pak))
                    ff.table[tl["entry"]] = [NEW_PAK, chunks[blob][0], chunks[blob][1]]
            else:
                tl, blob = blobs[0]
                if len(blob) != tl["size"]:
                    notes[image["name"]] = "layout not supported"
                    continue
                ff.zone[tl["data"]:tl["data"] + len(blob)] = blob
                ff.zone_changed = True
            count += 1
        if count:
            ff.save(dst)
            written.append(dst)
            log("%s: %d changed texture%s" % (os.path.basename(map_path), count, "" if count == 1 else "s"))
    if chunks:
        open(pak_path, "wb").write(bytes(pak))
        written.append(pak_path)
    for name, note in sorted(notes.items()):
        log("  %s in matches: %s" % (name, note))
    return written


def cmd_maps(stock_path, out_dir, *map_paths):
    built_path = os.path.join(out_dir, os.path.basename(stock_path))
    if not os.path.exists(built_path):
        sys.exit("no %s in %s; build or put something first" % (os.path.basename(stock_path), out_dir))
    if not map_paths:
        folder = os.path.dirname(os.path.abspath(stock_path))
        map_paths = sorted(p for p in glob.glob(os.path.join(folder, "mp_*.ff"))
                           if not p.lower().endswith("_load.ff"))
    if not map_paths:
        sys.exit("no mp_*.ff map files next to %s; copy them from the console first" % stock_path)
    written = sync_maps(stock_path, built_path, map_paths, out_dir)
    if not written:
        print("nothing to copy: no changed textures are in those maps")
    else:
        print("wrote %d files; copy them to _codxe\\zone\\ on the console" % len(written))


PICTURE_COLUMNS = {"mp/cardtitletable.csv": 2, "mp/cardicontable.csv": 1}
NOT_SPARE = {"cardtitle_locked", "cardicon_locked", "cardtitle_248x48"}


def cmd_pictures(ui_path, tables_path):
    import mw2zone
    materials = material_images(FastFile(ui_path))
    tables = {t["name"]: t["rows"] for t in mw2zone.read_tables(FastFile(tables_path).zone)}
    uses = {}
    for name, column in PICTURE_COLUMNS.items():
        for row in tables.get(name, []):
            if column < len(row) and row[column]:
                uses.setdefault(row[column].lower(), []).append(row[0])
    for kind, label in (("cardtitle_", "titles"), ("cardicon_", "emblems")):
        shared = sorted(((m, ids) for m, ids in uses.items() if m.startswith(kind) and len(ids) > 1),
                        key=lambda item: -len(item[1]))
        print("Pictures shared by several %s:" % label)
        for material, ids in shared:
            print("  %-34s %3d: %s%s" % (material, len(ids), ", ".join(ids[:4]), ", ..." if len(ids) > 4 else ""))
        if not shared:
            print("  none")
        spare = sorted(m for m in materials if m.lower().startswith(kind) and m.lower() not in NOT_SPARE
                       and m.lower() not in uses)
        print("Unused %s pictures (material in the table -> texture to put your picture on):" % label[:-1])
        for material in spare:
            print("  %-34s -> %s" % (material, materials[material]))
        print()


def main():
    args = sys.argv[1:]
    if len(args) in (2, 3) and args[0] == "tables":
        import mw2zone
        mw2zone.cmd_tables(*args[1:])
    elif len(args) in (2, 3) and args[0] == "buildtables":
        import mw2zone
        mw2zone.cmd_build(*args[1:])
    elif len(args) == 3 and args[0] == "pictures":
        cmd_pictures(*args[1:])
    elif len(args) == 2 and args[0] == "list":
        cmd_list(args[1])
    elif len(args) in (4, 5) and args[0] == "extract":
        cmd_extract(*args[1:])
    elif len(args) == 4 and args[0] == "replace":
        cmd_replace(*args[1:])
    elif len(args) == 3 and args[0] == "frames":
        cmd_frames(*args[1:])
    elif len(args) in (5, 6) and args[0] == "flipbook":
        cmd_flipbook(*args[1:])
    elif len(args) == 7 and args[0] == "grow":
        cmd_grow(*args[1:])
    elif len(args) == 6 and args[0] == "animate":
        cmd_animate(*args[1:])
    elif len(args) >= 3 and args[0] == "maps":
        cmd_maps(*args[1:])
    elif len(args) == 5 and args[0] == "put":
        cmd_put(*args[1:])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
