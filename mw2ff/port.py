"""Convert a PC (IW4x / 2009 PC, version 276) fastfile to Xbox 360 TU6 layout.

    python port.py mp_geometric.ff OUT.ff [--iwd mp_geometric.iwd] [--ref360 FILE.ff ...]
                   [--teams ALLIES AXIS]

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
  - teams: IW4x loads team assets from its own files, the 360 only from the map's file, so the
    two teams the map's .arena names (soldiers, flags, crates, icons) are copied in from stock
    360 maps given with --ref360 that have them
"""

import argparse
import contextlib
import io
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
LEVELS = 4      # pak table entries per picture

# Per team (mp/factionTable.csv + the character scripts in common_mp): the models and
# icons a map carries for it. The 360 keeps these in each map's own file.
TEAM_ASSETS = {
    'us_army': {
        "models": ['head_allies_us_army_sniper', 'head_us_army_a', 'head_us_army_b', 'head_us_army_c', 'head_us_army_d', 'head_us_army_e', 'head_us_army_f', 'mp_body_army_sniper', 'mp_body_us_army_assault_a', 'mp_body_us_army_assault_b', 'mp_body_us_army_assault_c', 'mp_body_us_army_lmg', 'mp_body_us_army_lmg_b', 'mp_body_us_army_lmg_c', 'mp_body_us_army_riot', 'mp_body_us_army_shotgun', 'mp_body_us_army_shotgun_b', 'mp_body_us_army_shotgun_c', 'mp_body_us_army_smg', 'mp_body_us_army_smg_b', 'mp_body_us_army_smg_c', 'viewhands_sniper_us_army', 'viewhands_us_army', 'prop_flag_ranger', 'prop_flag_ranger_carry', 'com_plasticcase_rangers'],
        "materials": ['faction_128_rangers', 'faction_128_rangers_fade', 'objpoint_flag_rangers', 'headicon_rangers'],
    },
    'opforce_composite': {
        "models": ['head_op_arab_sniper', 'head_opforce_arab_a', 'head_opforce_arab_b', 'head_opforce_arab_c', 'head_opforce_arab_d_hat', 'head_opforce_arab_e', 'head_riot_op_arab', 'mp_body_op_arab_sniper', 'mp_body_opforce_arab_assault_a', 'mp_body_opforce_arab_lmg_a', 'mp_body_opforce_arab_shotgun_a', 'mp_body_opforce_arab_smg_a', 'mp_body_riot_op_arab', 'viewhands_militia', 'viewhands_sniper_op_arab', 'prop_flag_opforce', 'prop_flag_opforce_carry', 'com_plasticcase_arab'],
        "materials": ['faction_128_arab', 'faction_128_arab_fade', 'objpoint_flag_arab', 'headicon_arab'],
    },
    'opforce_arctic': {
        "models": ['head_op_arctic_sniper', 'head_opforce_arctic_a', 'head_opforce_arctic_b', 'head_opforce_arctic_c', 'head_opforce_arctic_d', 'head_riot_op_arctic', 'mp_body_op_arctic_sniper', 'mp_body_opforce_arctic_assault_a', 'mp_body_opforce_arctic_assault_b', 'mp_body_opforce_arctic_assault_c', 'mp_body_opforce_arctic_lmg', 'mp_body_opforce_arctic_lmg_b', 'mp_body_opforce_arctic_lmg_c', 'mp_body_opforce_arctic_shotgun', 'mp_body_opforce_arctic_shotgun_b', 'mp_body_opforce_arctic_shotgun_c', 'mp_body_opforce_arctic_smg', 'mp_body_opforce_arctic_smg_b', 'mp_body_opforce_arctic_smg_c', 'mp_body_riot_op_arctic', 'viewhands_arctic_opforce', 'viewhands_sniper_op_arctic', 'prop_flag_speznas', 'prop_flag_speznas_carry', 'com_plasticcase_ussr'],
        "materials": ['faction_128_ussr', 'faction_128_ussr_fade', 'objpoint_flag_ussr', 'headicon_ussr'],
    },
    'opforce_airborne': {
        "models": ['head_airborne_a', 'head_airborne_b', 'head_airborne_c', 'head_airborne_d', 'head_airborne_e', 'head_op_airborne_sniper', 'head_riot_op_airborne', 'mp_body_airborne_assault_a', 'mp_body_airborne_assault_b', 'mp_body_airborne_assault_c', 'mp_body_airborne_lmg', 'mp_body_airborne_lmg_b', 'mp_body_airborne_lmg_c', 'mp_body_airborne_shotgun', 'mp_body_airborne_shotgun_b', 'mp_body_airborne_shotgun_c', 'mp_body_airborne_smg', 'mp_body_airborne_smg_b', 'mp_body_airborne_smg_c', 'mp_body_op_airborne_sniper', 'mp_body_riot_op_airborne', 'viewhands_russian_airborne', 'viewhands_sniper_op_airborne', 'prop_flag_speznas', 'prop_flag_speznas_carry', 'com_plasticcase_ussr'],
        "materials": ['faction_128_ussr', 'faction_128_ussr_fade', 'objpoint_flag_ussr', 'headicon_ussr'],
    },
    'militia': {
        "models": ['head_militia_a_wht', 'head_militia_ba_blk', 'head_militia_bb_blk_hat', 'head_militia_bc_blk', 'head_militia_bd_blk', 'head_op_militia_sniper', 'head_riot_op_militia', 'mp_body_militia_assault_aa_blk', 'mp_body_militia_assault_aa_wht', 'mp_body_militia_assault_ab_blk', 'mp_body_militia_assault_ac_blk', 'mp_body_militia_lmg_aa_blk', 'mp_body_militia_lmg_ab_blk', 'mp_body_militia_lmg_ac_blk', 'mp_body_militia_smg_aa_blk', 'mp_body_militia_smg_aa_wht', 'mp_body_militia_smg_ab_blk', 'mp_body_militia_smg_ac_blk', 'mp_body_op_miltia_sniper', 'mp_body_riot_op_militia', 'viewhands_militia', 'prop_flag_militia', 'prop_flag_militia_carry', 'com_plasticcase_militia'],
        "materials": ['faction_128_militia', 'faction_128_militia_fade', 'objpoint_flag_militia', 'headicon_militia'],
    },
    'socom_141': {
        "models": ['head_seal_soccom_a', 'head_seal_soccom_ba', 'head_seal_soccom_ca', 'head_seal_soccom_da', 'mp_body_seal_soccom_assault_a', 'mp_body_seal_soccom_assault_b', 'mp_body_seal_soccom_assault_b_blk', 'mp_body_seal_soccom_assault_c', 'mp_body_seal_soccom_assault_c_blk', 'mp_body_seal_soccom_assault_d', 'viewhands_us_army', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'socom_141_arctic': {
        "models": ['head_allies_tf141_arctic_sniper', 'head_riot_tf141_arctic', 'head_tf141_arctic_a', 'head_tf141_arctic_b', 'head_tf141_arctic_c', 'head_tf141_arctic_d', 'mp_body_riot_tf141_arctic', 'mp_body_tf141_arctic_sniper', 'mp_body_tf141_assault_a', 'mp_body_tf141_assault_b', 'mp_body_tf141_lmg', 'mp_body_tf141_shotgun', 'mp_body_tf141_smg', 'viewhands_arctic', 'viewhands_sniper_tf141_arctic', 'viewhands_tf141', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'socom_141_desert': {
        "models": ['head_allies_tf141_desert_sniper', 'head_riot_tf141_desert', 'head_tf141_desert_a', 'head_tf141_desert_b', 'head_tf141_desert_c', 'head_tf141_desert_d', 'mp_body_desert_tf141_assault_a', 'mp_body_desert_tf141_assault_b', 'mp_body_desert_tf141_lmg', 'mp_body_desert_tf141_shotgun', 'mp_body_desert_tf141_smg', 'mp_body_riot_tf141_desert', 'mp_body_tf141_desert_sniper', 'viewhands_sniper_tf141_desert', 'viewhands_tf141', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'socom_141_forest': {
        "models": ['head_allies_tf141_forest_sniper', 'head_riot_tf141_forest', 'head_tf141_forest_a', 'head_tf141_forest_b', 'head_tf141_forest_c', 'head_tf141_forest_d', 'mp_body_forest_tf141_assault_a', 'mp_body_forest_tf141_assault_b', 'mp_body_forest_tf141_lmg', 'mp_body_forest_tf141_shotgun', 'mp_body_forest_tf141_smg', 'mp_body_riot_tf141_forest', 'mp_body_tf141_forest_sniper', 'viewhands_sniper_tf141_forest', 'viewhands_tf141', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'seals_udt': {
        "models": ['head_allies_seal_udt_sniper', 'head_riot_udt', 'head_seal_udt_a', 'head_seal_udt_c', 'head_seal_udt_d', 'head_seal_udt_e', 'mp_body_riot_udt', 'mp_body_seal_udt_assault_a', 'mp_body_seal_udt_assault_b', 'mp_body_seal_udt_lmg', 'mp_body_seal_udt_smg', 'mp_body_seal_udt_sniper', 'viewhands_sniper_udt', 'viewhands_udt', 'prop_flag_seal', 'prop_flag_seal_carry', 'com_plasticcase_seals'],
        "materials": ['faction_128_seals', 'faction_128_seals_fade', 'objpoint_flag_seals', 'headicon_seals'],
    },
}
SIDE_TEAMS = {
    "allies": ("us_army", "seals_udt", "socom_141", "socom_141_desert", "socom_141_forest",
               "socom_141_arctic"),
    "axis": ("opforce_composite", "opforce_airborne", "opforce_arctic", "militia"),
}
# Stock maps whose file carries each team (mp/basemaps.arena in code_post_gfx_mp).
STOCK_MAP_TEAMS_BY_TEAM = {
    'militia': ['mp_favela', 'mp_quarry', 'mp_underpass', 'mp_rundown'],
    'opforce_airborne': ['mp_highrise', 'mp_nightshift', 'mp_brecourt', 'mp_estate', 'mp_terminal'],
    'opforce_arctic': ['mp_derail', 'mp_subbase'],
    'opforce_composite': ['mp_invasion', 'mp_checkpoint', 'mp_boneyard', 'mp_afghan', 'mp_rust'],
    'seals_udt': ['mp_checkpoint', 'mp_subbase'],
    'socom_141_arctic': ['mp_derail'],
    'socom_141_desert': ['mp_favela', 'mp_quarry', 'mp_rundown', 'mp_boneyard', 'mp_afghan', 'mp_rust'],
    'socom_141_forest': ['mp_brecourt', 'mp_underpass', 'mp_estate'],
    'us_army': ['mp_invasion', 'mp_highrise', 'mp_nightshift', 'mp_terminal'],
}


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
    if isinstance(tgt, PtrList) and rel % 4 == 0 and rel // 4 < len(tgt):
        # An element of another asset's pointer array (XModel.materialHandles).
        c = tgt[rel // 4]
        if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
            return c
    if not isinstance(t, Compound) or t.kind != "struct":
        return None
    if isinstance(tgt, list):
        i, rel = divmod(rel, t.size)
        if i >= len(tgt):
            return None
        tgt = tgt[i]
    return _asset_in_member(t, tgt, rel)


def _asset_in_member(t, tgt, rel):
    if not isinstance(tgt, dict):
        return None
    for m in t.members:
        if m.offset == rel and m.mods and m.mods[0] == PTR:
            c = tgt.get("@", {}).get((tree.mkey(t, m), ()))
            if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
                return c
        elif (not m.mods and isinstance(m.type, Compound)
              and m.offset <= rel < m.offset + m.type.size):
            # A pointer inside an inline struct or union (MaterialTextureDef.u.image).
            c = _asset_in_member(m.type, tgt.get(m.name), rel - m.offset)
            if c is not None:
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


HIMIP_RADIUS = 1238     # the median of stock Favela's models


def convert_model_vertices(leaf):
    """GfxPackedVertex: floats to big-endian, color and packed texture coordinates as the
    same 32-bit value, normal and tangent repacked as 10:10:10 signed (checked against stock
    mil_tntbomb_mp, whose vertices match exactly)."""
    raw = leaf.raw
    out = bytearray(len(raw))
    cache = {}
    for o in range(0, len(raw), 32):
        struct.pack_into(">4f", out, o, *struct.unpack_from("<4f", raw, o))
        out[o + 16:o + 20] = raw[o + 16:o + 20][::-1]
        out[o + 20:o + 24] = raw[o + 20:o + 24][::-1]
        for k in (24, 28):
            key = raw[o + k:o + k + 4]
            u = cache.get(key)
            if u is None:
                u = cache[key] = _dec3n(_pc_unit(key))
            struct.pack_into(">I", out, o + k, u)
    return Leaf(leaf.t, leaf.n, out, ">")


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
    expand = IWI_EXPAND.get(fmt)
    if expand is not None:
        name = "ARGB8"      # the few uncompressed kinds without a 360 twin become 32-bit
    if name is None:
        raise PortError("image format %d isn't supported yet" % fmt)
    bw, bpb = (1, 4) if name == "ARGB8" else (4, 8 if name == "DXT1" else 16)
    if expand is not None:
        bpb = expand[0]
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
    if expand is not None:
        levels = [expand[1](lv) for lv in levels]
        sizes = [n // expand[0] * 4 for n in sizes]
    if cube:
        return name, w, h, [levels[0][f * sizes[0]:(f + 1) * sizes[0]] for f in range(6)], True
    return name, w, h, levels, False


def _rgb24_to_argb8(b):
    """B,G,R -> B,G,R,A (as in a DDS / format 1 .iwi)."""
    out = bytearray(len(b) // 3 * 4)
    out[0::4], out[1::4], out[2::4] = b[0::3], b[1::3], b[2::3]
    out[3::4] = b"\xff" * (len(b) // 3)
    return bytes(out)


def _la16_to_argb8(b):
    """Luminance, alpha -> gray with that alpha."""
    out = bytearray(len(b) * 2)
    lum, alpha = b[0::2], b[1::2]
    out[0::4], out[1::4], out[2::4], out[3::4] = lum, lum, lum, alpha
    return bytes(out)


def _a8_to_argb8(b):
    """Alpha only -> black with that alpha (what the PC's A8 samples as)."""
    out = bytearray(len(b) * 4)
    out[3::4] = b
    return bytes(out)


# .iwi kinds turned into 32-bit pixels: format -> (bytes a pixel, converter)
IWI_EXPAND = {0x02: (3, _rgb24_to_argb8), 0x03: (2, _la16_to_argb8), 0x04: (1, _a8_to_argb8)}


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
            # Six faces, each tiled on its own. mips: per face, its top mip or its whole chain.
            # With mips: every face's top level, then each further mip slice for all six faces
            # (as in stock reflection probes).
            chains = [m if isinstance(m, list) else [m] for m in mips]
            levels = min(len(c) for c in chains)
            if levels == 1:
                pixels = b"".join(tex.tile(c[:1], width, height, gpu, single=True) for c in chains)
            else:
                plan, total = tex._plan(width, height, gpu)
                cuts = sorted(set(p[1] for p in plan[:levels])) + [total]
                tiled = [tex.tile(c[:levels], width, height, gpu) for c in chains]
                pixels = b"".join(t[a:b] for a, b in zip(cuts, cuts[1:]) for t in tiled)
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
            mip_addr = tex._layout(width, height, 0, gpu)[3] * (6 if cube else 1) >> 12
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
        keep = {k: d[k] for k in ("_slot", "_forward") if k in d}
        d.clear()
        d.update(new)
        d.update(keep)


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
    def __init__(self, pc_root, x_refs, iwd=None, log=print, game_iwds=()):
        self.root = pc_root
        self.log = log
        self.P = schema_mod.load("pc")
        self.X = schema_mod.load("xbox")
        self.pc = codec_mod.Codec(self.P)
        self.xc = codec_mod.Codec(self.X)
        self.done = set()
        self.iwd = iwd
        self.missing_images = []
        # The PC game's own .iwd files (iw_00.iwd ...): pictures a map borrows from the game.
        # Later files win, as in the game.
        self.game_pictures = {}
        for zf in game_iwds:
            for n in zf.namelist():
                low = n.lower()
                if low.startswith("images/") and low.endswith(".iwi"):
                    self.game_pictures[low[7:-4]] = (zf, n)
        self.from_game = 0
        self.techset_swaps = {}
        # Stock 360 assets: resident ones (use by ",name") and others to copy in.
        self.resident = {}
        self.library = {}
        # Assets stock maps only name (",name"): the game has them loaded from its always-loaded
        # files (common_mp, ...) whenever a map loads, so a ported map can name them too.
        self.named = set()
        for name, root in x_refs:
            idx = asset_index(root)
            base = os.path.splitext(os.path.basename(name))[0]
            for k, v in idx.items():
                if k[1].startswith(b","):
                    self.named.add((k[0], k[1][1:]))
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
        self.picked = {}        # stock file -> its asset list entries to copy in
        self.remapped = set()
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
        for k in ("_asset", "_slot", "_template", "_forward"):
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
        gfx = next((e[1] for e in ents if e[0] == "gfx_map" and isinstance(e[1], dict)), {})
        self.map_name = re.sub(rb"^maps/mp/|\.d3dbsp$", b"", _name(gfx) or b"")
        self.world_checksum = next((e[1].get("checksum", 0) for e in ents
                                    if e[0] == "gfx_map" and isinstance(e[1], dict)), 0)
        # Materials and pictures a left-out asset brought in first are only pointed at from
        # then on: the writer puts each where the first remaining pointer to it is.
        reached = set(id(o) for o in iter_objects(ents))
        orphans = {}
        for o in iter_objects(ents):
            if not isinstance(o, dict):
                continue
            for c in o.get("@", {}).values():
                for x in (c if isinstance(c, list) else [c]):
                    if isinstance(x, Ref) and isinstance(x.target, tree.InsertSlot):
                        a = x.target.asset
                        if isinstance(a, dict) and id(a) not in reached:
                            orphans[id(a)] = a
        inner = set()
        for a in orphans.values():
            inner.update(id(o) for o in iter_objects(a) if o is not a)
        orphans = [a for k, a in orphans.items() if k not in inner]
        for a in orphans:
            a["_forward"] = True
        for e in ents:
            if e[0] in ("vertexshader", "vertexdecl"):
                raise PortError("top-level %s assets can't be converted" % e[0])
            if isinstance(e[1], dict):
                self.conv_asset(e[1])
        for a in orphans:
            self.conv_asset(a)
            a["_forward"] = True
        if self.from_game:
            self.log("  %d pictures come from the PC game's own .iwd files" % self.from_game)
        if self.missing_images:
            self.warn("%d pictures aren't in the map's .iwd%s, so plain gray ones stand in (%s%s)"
                      % (len(self.missing_images),
                         " or the PC game's files" if self.game_pictures else "",
                         ", ".join(self.missing_images[:5]),
                         ", ..." if len(self.missing_images) > 5 else ""))
        for a, b in sorted(self.techset_swaps.items()):
            self.warn("shader set %s isn't in the stock files given, so %s is used"
                      % (a.decode(), b.decode()))
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

    def leaf_GfxPackedVertex(self, lf, tx):
        return convert_model_vertices(lf)

    def post_XSurface(self, d, tx):
        # The PC's buffer handle means nothing here; the 360 builds its vertex and index
        # buffers from verts0 / triIndices when the file loads (both zero in stock files).
        d["zoneHandle"] = 0
        d["unknown"] = 0
        d["vertexBuffer"] = [0] * 8
        d["indexBuffer"] = [0] * 8

    def post_XModel(self, d, tx):
        """The 360 keeps a number per surface for texture streaming (himipRadii); the game
        reads it for every model when the file loads. Stock models use about 1240."""
        m = next(m for m in tx.members if m.name == "himipRadii")
        n = d["numsurfs"]
        d["himipRadii"] = "follow" if n else None
        if n:
            d["@"][("himipRadii", ())] = Leaf(m.type, n, struct.pack(">%dH" % n, *[HIMIP_RADIUS] * n), ">")

    def _replace(self, d, new):
        slot = d.get("_slot")
        fwd = d.get("_forward")
        d.clear()
        d.update(new)
        if slot is not None:
            d["_slot"] = slot
        if fwd:
            d["_forward"] = fwd
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

    def nearest_techset(self, name):
        """The stock techset whose name shares the most leading parts with name (same kind:
        effect_, wc_, mc_, ...), or None."""
        if name in self.techset_swaps:
            return self.techset_swaps[name]
        want = name.split(b"_")
        best, score = None, (0, 0)
        for cand in self.material_templates:
            key = ("MaterialTechniqueSet", cand)
            if cand is None or not (key in self.resident or key in self.library or key in self.named):
                continue
            parts = cand.split(b"_")
            lead = 0
            while lead < min(len(want), len(parts)) and want[lead] == parts[lead]:
                lead += 1
            sc = (lead, len(set(want) & set(parts)) - len(set(parts) - set(want)))
            if lead and sc > score:
                best, score = cand, sc
        if best is not None:
            self.techset_swaps[name] = best
        return best

    def pre_MaterialTechniqueSet(self, d, tp, tx):
        name = asset_name(d)
        if name.startswith(b","):          # the PC file only names it too
            name = name[1:]
        if ("MaterialTechniqueSet", name) in self.resident or ("MaterialTechniqueSet", name) in self.named:
            return self._replace(d, self.reference("MaterialTechniqueSet", name))
        src = self.library.get(("MaterialTechniqueSet", name))
        if src is None:
            near = self.nearest_techset(name)
            if near is not None and near != name:
                d["@"][("name", ())] = Str(near)
                return self.pre_MaterialTechniqueSet(d, tp, tx)
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
        if name.startswith(b","):
            # The PC file only names it (the PC game has it loaded already). Name it on the 360
            # too when the 360 has it loaded; else it becomes a picture of the map's own.
            name = name[1:]
            if ("GfxImage", name) in self.resident or ("GfxImage", name) in self.named:
                return self._replace(d, self.reference("GfxImage", name))
            d.setdefault("@", {})[("name", ())] = Str(name)
        # Built-in pictures ($identitynormalmap, ...) stay the game's own even when an IW4x
        # .iwd carries a copy: a second asset with the name would replace the resident one.
        if ("GfxImage", name) in self.resident and (name.startswith(b"$") or not self.in_iwd(name)):
            return self._replace(d, self.reference("GfxImage", name))
        tex = d.get("texture", {})
        ld = tex.get("@", {}).get(("loadDef", ())) if isinstance(tex, dict) else None
        if isinstance(ld, dict) and ld.get("resourceSize"):
            self.image_from_loaddef(d, ld)
        elif not self.in_iwd(name) and name.decode().lower() in self.game_pictures:
            zf, path = self.game_pictures[name.decode().lower()]
            self.image_from_iwd(d, name, zf, path)
            self.from_game += 1
        elif not self.in_iwd(name):
            # Not in the map's .iwd (none given) nor the PC game's files given:
            # a plain built-in picture stands in so the map still loads.
            self.missing_images.append(name.decode())
            return self._replace(d, self.reference("GfxImage", self.stand_in(d, name)))
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
        if tsname and tsname.startswith(b","):     # already converted to a reference
            tsname = tsname[1:]
        tpl = self.material_templates.get(tsname)
        if tpl is None and tsname:
            # No stock file given has this shader set: use the closest one that is there.
            near = self.nearest_techset(tsname)
            if near is not None:
                ts["@"][("name", ())] = Str(near)
                tsname, tpl = near, self.material_templates[near]
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

    @staticmethod
    def stand_in(d, name):
        """The built-in picture used for one the map doesn't bring: flat for normal maps,
        black (no shine) for specular maps, gray otherwise."""
        semantic = d.get("semantic")
        if semantic == 5 or name.endswith((b"_nml", b"_n")):
            return b"$identitynormalmap"
        if semantic == 8 or name.endswith((b"_spc", b"_s")):
            return b"$black"
        return b"$gray"

    def image_from_iwd(self, d, name, iwd=None, path=None):
        iwd = iwd or self.iwd
        if iwd is None:
            raise PortError("image %s needs the map's .iwd" % name.decode())
        path = path or "images/%s.iwi" % name.decode()
        try:
            data = iwd.read(path)
        except KeyError:
            raise PortError("image %s isn't in the .iwd" % name.decode())
        fmt, w, h, mips, cube = read_iwi(data)
        limit = 1024 if name.startswith(b"loadscreen") else 2048
        if not cube:
            w, h, mips = fit_picture(fmt, w, h, mips, limit)
        self.images.build(d, fmt, w, h, mips, cube)
        # Pictures from files are "load from file" on the 360 (the PC leaves them unknown);
        # stock loading screens are plain 2D pictures.
        d["category"] = 3
        if name.startswith(b"loadscreen"):
            d["semantic"] = 0

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
        if cube and out_fmt == "ARGB8":
            # Reflection probes: keep every face's mips. The smaller mips are blurrier copies
            # the game uses for less glossy surfaces; with the top mip alone every gun and
            # shiny surface reflects the sharp picture (too shiny).
            chains = []
            for i in range(faces):
                chain, off, mw, mh = [], i * face, w, h
                while off + mw * mh * bpp <= (i + 1) * face:
                    chain.append(data[off:off + mw * mh * bpp])
                    off += mw * mh * bpp
                    if mw == 1 and mh == 1:
                        break
                    mw, mh = max(1, mw // 2), max(1, mh // 2)
                chains.append(chain)
            mips = chains
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
        self.localize(src)
        new = dict(src)
        new.pop("_slot", None)
        return new

    def localize(self, src):
        """Make src (an asset, or a list of asset list entries) self-contained, in place."""
        inside = set(id(o) for o in iter_objects(src))
        # The slot a temp-block asset reserves counts as part of it (later assets point there).
        inside |= set(id(o["_slot"]) for o in iter_objects(src) if isinstance(o, dict) and "_slot" in o)
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

    # ------------------------------------------------------------ teams

    def add_teams(self, x_refs, teams):
        """Copy what the two teams need (soldier bodies, heads and arms, flag and crate models,
        team icons) from stock 360 maps that have them, and make the map's script use those
        teams. IW4x loads team assets from its own files; on the 360 each map carries them.
        teams: (allies, axis) as wanted (the map's .arena); a team no stock file given has is
        swapped for one that is there. Returns the teams used."""
        arena = None
        for n, r in x_refs:
            for e in r["assets"]:
                if e[0] == "rawfile" and _name(e[1]) == b"mp/basemaps.arena":
                    arena = arena or _rawfile_text(e[1])
        donors = []         # (file name, root, team)
        for n, r in x_refs:
            have = set((e[0], _name(e[1])) for e in r["assets"] if isinstance(e[1], dict))
            for team, need in TEAM_ASSETS.items():
                if all(("xmodel", m.encode()) in have for m in need["models"]) and \
                        all(("material", m.encode()) in have for m in need["materials"]):
                    donors.append((n, r, team))
        used = []
        for side, want in zip(("allies", "axis"), teams):
            want = (want or "").lower()
            pick = next((d for d in donors if d[2] == want), None)
            if pick is None:
                pick = next((d for d in donors if d[2] in SIDE_TEAMS[side] and d[2] not in
                             [u[2] for u in used]), None) or next(
                    (d for d in donors if d[2] not in [u[2] for u in used]), None)
                if pick is None:
                    raise PortError("no stock map given (--ref360) has the %s team" % want)
                self.warn("the map wants the %s team for %s; using %s instead (for %s, add one of "
                          "these stock 360 maps: %s)" % (want or "(none)", side, pick[2], want,
                                                         ", ".join(_team_maps(arena, want)) or "?"))
            used.append(pick)
        for n, r, team in used:
            self._pick_team(r, team)
        for n, r, team in used:
            self._copy_picked(r)
        # The 360 only knows the teams of its own maps (mp/basemaps.arena); set them in the
        # map's script so the game uses the assets copied in.
        gsc = b"maps/mp/%s.gsc" % self.map_name
        for e in self.root["assets"]:
            if e[0] == "rawfile" and _name(e[1]) == gsc:
                text = _rawfile_text(e[1]).decode("latin-1")
                line = '\tgame[ "allies" ] = "%s";\n\tgame[ "axis" ] = "%s";\n' % (used[0][2], used[1][2])
                text, n = re.subn(r"(main\s*\(\s*\)\s*\{[^\n]*\n)", lambda m: m.group(1) + line, text, 1)
                if n:
                    _set_rawfile_text(e[1], text.encode("latin-1"))
                    break
        else:
            self.warn("couldn't set the teams in %s" % gsc.decode())
        self.log("  teams: %s (from %s) vs %s (from %s)" % (
            used[0][2], os.path.basename(used[0][0]), used[1][2], os.path.basename(used[1][0])))
        return used[0][2], used[1][2]

    def _pick_team(self, src, team):
        need = TEAM_ASSETS[team]
        names = set(("xmodel", m.encode()) for m in need["models"]) | \
            set(("material", m.encode()) for m in need["materials"])
        ents = src["assets"]
        pos = {id(e): i for i, e in enumerate(ents)}
        owner = {}
        for i, e in enumerate(ents):
            for o in iter_objects(e[1]):
                if isinstance(o, dict) and "_asset" in o:
                    owner.setdefault(id(o), i)
        sel = set(i for i, e in enumerate(ents) if isinstance(e[1], dict) and (e[0], _name(e[1])) in names)
        todo = list(sel)
        while todo:
            for o in iter_objects(ents[todo.pop()][1]):
                if not isinstance(o, dict):
                    continue
                for c in o.get("@", {}).values():
                    for x in (c if isinstance(c, list) else [c]):
                        if not isinstance(x, Ref):
                            continue
                        a = x.target if isinstance(x.target, tree.AssetEntry) else _asset_in_slot(x)
                        j = None
                        if isinstance(a, tree.AssetEntry):
                            j = pos[id(a)]
                        elif isinstance(a, dict):
                            j = owner.get(id(a))
                        if j is not None and j not in sel:
                            sel.add(j)
                            todo.append(j)
        self.picked.setdefault(id(src), set()).update(sel)

    def _copy_picked(self, src):
        """Copy the entries picked from src in their stock order (both teams of one file
        together: a later model can point into an earlier one's parts)."""
        sel = self.picked.pop(id(src), None)
        if not sel:
            return
        ents = src["assets"]
        new = [ents[i] for i in sorted(sel)]
        self.localize(new)
        # Bone names are indexes into the file's script string list: move them to ours.
        ss = self.root.get("script_strings")
        if ss is None:
            ss = self.root["script_strings"] = [None]
        where = {(s.b if isinstance(s, Str) else None): k for k, s in enumerate(ss)}
        theirs = src["script_strings"]
        for o in iter_objects(new):
            if isinstance(o, dict) and o.get("_asset") == "XModel":
                lf = o.get("@", {}).get(("boneNames", ()))
                if isinstance(lf, Ref):
                    lf = lf.target
                if not isinstance(lf, Leaf) or id(lf) in self.remapped:
                    continue
                self.remapped.add(id(lf))
                out = []
                for v in struct.unpack(lf.E + "%dH" % lf.n, lf.raw):
                    b = theirs[v].b if isinstance(theirs[v], Str) else None
                    if b not in where:
                        where[b] = len(ss)
                        ss.append(Str(b))
                    out.append(where[b])
                lf.raw = struct.pack(lf.E + "%dH" % lf.n, *out)
        self.root["assets"][:0] = new


def _name(d):
    c = d.get("@", {}).get(("name", ()))
    if c is None and isinstance(d.get("info"), dict):
        c = d["info"].get("@", {}).get(("name", ()))
    if isinstance(c, Ref) and isinstance(c.target, Str) and c.rel == 0:
        c = c.target
    return c.b if isinstance(c, Str) else None


def _rawfile_text(d):
    import zlib
    ch = d["data"]["@"]
    lf = ch.get(("buffer", ()), ch.get(("compressedBuffer", ())))
    if isinstance(lf, Ref):
        lf = lf.target
    raw = lf.raw
    if d.get("compressedLen"):
        raw = zlib.decompress(raw[:d["compressedLen"]])
    return raw[:d["len"]]


def _set_rawfile_text(d, data):
    ch = d["data"]["@"]
    old = ch.pop(("buffer", ()), None) or ch.pop(("compressedBuffer", ()))
    d["compressedLen"] = 0
    d["len"] = len(data)
    d["data"]["@"][("buffer", ())] = Leaf(old.t, len(data) + 1, data + b"\0", old.E)


def _team_maps(arena, team):
    """Stock maps whose file carries team (from mp/basemaps.arena)."""
    out = []
    for block in re.findall(rb"\{(.*?)\}", arena or b"", re.S):
        kv = dict(re.findall(rb'(\w+)\s+"?([^"\s]*)"?', block))
        if team.encode() in (kv.get(b"allieschar"), kv.get(b"axischar")):
            out.append(kv[b"map"].decode())
    return out


def _arena_teams(text, map_name):
    for block in re.findall(rb"\{(.*?)\}", text, re.S):
        kv = dict(re.findall(rb'(\w+)\s+"?([^"\s]*)"?', block))
        if kv.get(b"map") == map_name.encode():
            if b"allieschar" in kv and b"axischar" in kv:
                return kv[b"allieschar"].decode(), kv[b"axischar"].decode()
    return None


# ================================================================ command line

def load_tree(path):
    ff, zone = mw2ff.read_fastfile(path)
    sch = mw2ff.schema_for(ff.platform)
    root, r = tree.read_tree(zone, sch)
    return ff, zone, root


def map_teams(pc_path, teams=None):
    """(allies, axis) the map wants: given, or from its .arena file next to the .ff, or the
    game's defaults for a map it doesn't know."""
    if teams:
        return tuple(teams)
    arena = os.path.splitext(pc_path)[0] + ".arena"
    if os.path.exists(arena):
        text = open(arena, "rb").read()
        kv = dict(re.findall(rb'(\w+)[ \t]+"?([^"\s]*)"?', text))
        if b"allieschar" in kv and b"axischar" in kv:
            return kv[b"allieschar"].decode(), kv[b"axischar"].decode()
    return "us_army", "opforce_composite"


def load_stock(path):
    """A stock 360 file's tree, with each picture whose pixels are in the disc's
    imagefile*.pak files marked with its container entries ("_pak")."""
    import mw2tex
    ff, zone = mw2ff.read_fastfile(path)
    root, r = tree.read_tree(zone, mw2ff.schema_for(ff.platform))
    with contextlib.redirect_stdout(io.StringIO()):
        f = mw2tex.FastFile(path)
    paks = {i["offset"]: k for k, i in enumerate(x for x in f.images if x["pak"])}
    for info, inst, start, end in r.assets:
        if info.name == "GfxImage" and start in paks:
            k = paks[start]
            r.reg[id(inst)][1]["_pak"] = [tuple(t) for t in f.table[k * LEVELS:(k + 1) * LEVELS]]
    return root


def game_iwd_files(folder):
    """The .iwd files in a folder (the PC game's main folder or a copy of its iw_*.iwd), in
    the order the game loads them."""
    if not folder or not os.path.isdir(folder):
        return []
    return sorted((os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".iwd")),
                  key=lambda p: os.path.basename(p).lower())


def port(pc_path, out_path, iwd_path=None, ref_paths=(), log=print, teams=None, loaded=None,
         game_iwds=()):
    """loaded: {path: tree} of stock files already read (load_stock), to reuse.
    game_iwds: the PC game's .iwd files, for pictures the map's own .iwd doesn't have."""
    ff, zone, root = load_tree(pc_path)
    if ff.platform != "pc":
        raise PortError("%s is not a PC fastfile" % pc_path)
    refs = []
    loaded = {} if loaded is None else loaded
    for p in ref_paths:
        if p not in loaded:
            log("reading stock 360 file %s" % os.path.basename(p))
            loaded[p] = load_stock(p)
        refs.append((p, loaded[p]))
    iwd = zipfile.ZipFile(iwd_path) if iwd_path else None
    log("converting %s" % os.path.basename(pc_path))
    porter = Porter(root, refs, iwd, log, [zipfile.ZipFile(p) for p in game_iwds])
    porter.convert()
    if porter.map_name:
        porter.add_teams(refs, map_teams(pc_path, teams))
    xs = schema_mod.load("xbox")
    w = tree.TreeWriter(root, xs, keep_fixes=False)
    w.map_rel = lambda r: map_rel(r, porter.P, porter.X)
    out = w.write()
    out = reserve_callback_block(out, porter)
    _write_x360(out, out_path, pak_table(out, out_path, porter.root, w.starts))
    log("wrote %s (%d bytes of zone)" % (out_path, len(out)))
    return out, porter


def port_map(pc_path, out_dir, stock_paths, teams=None, log=print, game_iwds=()):
    """Convert a PC map (its .ff, and _load.ff / .iwd / .arena next to it when there) into
    out_dir, picking what it needs from the stock 360 files given: code_post_gfx_mp.ff, a
    stock map (render settings, shaders) and the stock maps that carry the map's teams.
    Returns the paths written."""
    stock = {os.path.basename(p).lower(): p for p in stock_paths}
    cpg = stock.get("code_post_gfx_mp.ff")
    if cpg is None:
        raise PortError("code_post_gfx_mp.ff (from the console) is needed next to the stock maps")
    maps = sorted(p for n, p in stock.items() if n.startswith("mp_") and not n.endswith("_load.ff"))
    if not maps:
        raise PortError("at least one stock 360 map (for example mp_favela.ff) is needed")
    template = stock.get("mp_favela.ff") or maps[0]
    loads = sorted(p for n, p in stock.items() if n.endswith("_load.ff"))
    base = os.path.splitext(pc_path)[0]
    name = os.path.basename(base)
    iwd = base + ".iwd" if os.path.exists(base + ".iwd") else None
    os.makedirs(out_dir, exist_ok=True)
    loaded = {}
    log("reading stock 360 file code_post_gfx_mp.ff")
    loaded[cpg] = load_stock(cpg)
    arena = next((_rawfile_text(e[1]) for e in loaded[cpg]["assets"]
                  if e[0] == "rawfile" and _name(e[1]) == b"mp/basemaps.arena"), b"")
    want = map_teams(pc_path, teams)
    refs = [cpg, template]
    for team in want:
        carriers = set(_team_maps(arena, team.lower()))
        donor = next((p for p in maps if os.path.splitext(os.path.basename(p))[0].lower() in carriers), None)
        if donor and donor not in refs:
            refs.append(donor)
    written = []
    if os.path.exists(base + "_load.ff"):
        tpl_load = os.path.splitext(template)[0] + "_load.ff"
        load_ref = tpl_load if tpl_load in loads else (loads[0] if loads else None)
        if load_ref is None:
            log("  note: no stock *_load.ff given; the loading screen file is left out")
        else:
            out = os.path.join(out_dir, name + "_load.ff")
            try:
                port(base + "_load.ff", out, iwd, [cpg, template, load_ref], log, loaded=loaded,
                     game_iwds=game_iwds)
                written.append(out)
            except (ValueError, PortError, mw2ff.zone_mod.ZoneError) as e:
                # The map works without it: the game shows a plain loading screen.
                with open(base + "_load.ff", "rb") as fh:
                    head = fh.read(16)
                log("  note: %s_load.ff couldn't be converted, so it's left out (the map still "
                    "works, with a plain loading screen): %s [file starts %s]"
                    % (name, e, head.hex(" ")))
    out = os.path.join(out_dir, name + ".ff")
    port(pc_path, out, iwd, refs, log, teams=want, loaded=loaded, game_iwds=game_iwds)
    written.append(out)
    return written


def reserve_callback_block(zone, porter):
    """The 360 game fills block 5 (callback) at load time: a bump allocator there takes, for
    every material, 4 bytes per texture (0x821e7658), 4 per model surface (0x821e7908) and
    16 per some other assets (0x821dd2b0). The file carries no data for it, so a PC file
    converts to size 0 and the game writes through a null pointer (console-confirmed crash at
    0x821E7774). Stock files reserve 1-72 KB."""
    need = 0
    for o in iter_objects(porter.root):
        if isinstance(o, dict) and "_asset" in o:
            need += 16
            if o["_asset"] == "Material":
                need += 4 * (o.get("textureCount") or 0)
            elif o["_asset"] == "XModel":
                need += 4 * (o.get("numsurfs") or 0)
    # Other load-time allocations can land here too (0x82312da8 during asset callbacks), so
    # keep at least what a stock map reserves.
    need = max((need * 2 + 4096 + 0xFFF) & ~0xFFF, 0x10000)
    zone = bytearray(zone)
    if struct.unpack_from(">I", zone, 28)[0] < need:
        struct.pack_into(">I", zone, 28, need)
    return bytes(zone)


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


def _write_x360(zone, path, table=()):
    import zlib
    stream = zlib.compress(zone, 9)
    head = X360_HEAD + struct.pack(">I", len(table)) + b"".join(struct.pack(">III", *t) for t in table)
    total = len(head) + 8 + len(stream)
    # The second size also counts the pictures read from imagefile1.pak while loading.
    extra = sum(t[2] - t[1] for t in table if t[0] == 1)
    with open(path, "wb") as f:
        f.write(head + struct.pack(">II", total, total + extra) + stream)


def pak_table(zone, out_path, root, starts):
    """Stock pictures copied in whose pixels live in the disc's imagefile*.pak files: the
    container lists where, 4 entries per picture in the order the zone has them."""
    import mw2tex
    paks = [o for o in iter_objects(root) if isinstance(o, dict) and "_pak" in o and id(o) in starts]
    paks.sort(key=lambda o: starts[id(o)])
    _write_x360(zone, out_path)
    with contextlib.redirect_stdout(io.StringIO()):
        found = sum(1 for i in mw2tex.FastFile(out_path).images if i["pak"])
    if found != len(paks):
        raise PortError("%d pak pictures written but %d found in the file" % (len(paks), found))
    return [t for o in paks for t in o["_pak"]]


def main(argv):
    ap = argparse.ArgumentParser(description="Convert a PC fastfile to Xbox 360 TU6 layout")
    ap.add_argument("pc_ff")
    ap.add_argument("out_ff")
    ap.add_argument("--iwd")
    ap.add_argument("--game", help="folder with the PC game's .iwd files (its main folder)")
    ap.add_argument("--ref360", nargs="*", default=[])
    ap.add_argument("--teams", nargs=2, metavar=("ALLIES", "AXIS"),
                    help="teams to use (default: from the map's .arena next to the .ff)")
    a = ap.parse_args(argv)
    port(a.pc_ff, a.out_ff, a.iwd, a.ref360, teams=a.teams, game_iwds=game_iwd_files(a.game))


if __name__ == "__main__":
    main(sys.argv[1:])
