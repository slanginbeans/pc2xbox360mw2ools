"""Walk every asset in a decompressed MW2 (IW4) Xbox 360 TU6 zone.

The walk follows the same order and memory-block bookkeeping as the game's
own loader, so every byte of the zone is accounted for and every pointer that
refers back to earlier data can be resolved.
"""

import struct

import schema as schema_mod
from cdefs import PTR, Compound, Enum, Prim

FOLLOWING = 0xFFFFFFFF
INSERT = 0xFFFFFFFE

# Asset type numbers used by the TU6 executable (DB_GetXAssetTypeName table).
ASSET_TYPES = [
    "physpreset", "phys_collmap", "xanim", "xmodelsurfs", "xmodel", "material", "pixelshader",
    "techset", "image", "sound", "sndcurve", "loaded_sound", "col_map_sp", "col_map_mp",
    "com_map", "game_map_sp", "game_map_mp", "map_ents", "fx_map", "gfx_map", "lightdef",
    "ui_map", "font", "menufile", "menu", "localize", "weapon", "snddriverglobals", "fx",
    "impactfx", "aitype", "mptype", "character", "xmodelalias", "rawfile", "stringtable",
    "leaderboarddef", "structureddatadef", "tracer", "vehicle", "addon_map_ents",
]

# Asset type number -> the definition's asset enum name.
ASSET_STRUCTS = {
    0: "AssetPhysPreset", 1: "AssetPhysCollMap", 2: "AssetXAnim", 3: "AssetXModelSurfs", 4: "AssetXModel",
    5: "AssetMaterial", 6: "AssetPixelShader", 7: "AssetTechniqueSet", 8: "AssetImage", 9: "AssetSound",
    10: "AssetSoundCurve", 11: "AssetLoadedSound", 12: "AssetClipMapMp", 13: "AssetClipMapMp", 14: "AssetComWorld",
    15: "AssetGameWorldSp", 16: "AssetGameWorldMp", 17: "AssetMapEnts", 18: "AssetFxWorld", 19: "AssetGfxWorld",
    20: "AssetLightDef", 22: "AssetFont", 23: "AssetMenuList", 24: "AssetMenu", 25: "AssetLocalize",
    26: "AssetWeapon", 27: "AssetSndDriverGlobals", 28: "AssetFx", 29: "AssetImpactFx", 34: "AssetRawFile", 35: "AssetStringTable",
    36: "AssetLeaderboard", 37: "AssetStructuredDataDef", 38: "AssetTracer", 39: "AssetVehicle",
    40: "AssetAddonMapEnts",
}

# PC (version 276) asset types: the 360 list plus vertex shaders and vertex declarations.
PC_ASSET_TYPES = [
    "physpreset", "phys_collmap", "xanim", "xmodelsurfs", "xmodel", "material", "pixelshader",
    "vertexshader", "vertexdecl", "techset", "image", "sound", "sndcurve", "loaded_sound",
    "col_map_sp", "col_map_mp", "com_map", "game_map_sp", "game_map_mp", "map_ents", "fx_map",
    "gfx_map", "lightdef", "ui_map", "font", "menufile", "menu", "localize", "weapon",
    "snddriverglobals", "fx", "impactfx", "aitype", "mptype", "character", "xmodelalias",
    "rawfile", "stringtable", "leaderboarddef", "structureddatadef", "tracer", "vehicle",
    "addon_map_ents",
]
PC_ASSET_STRUCTS = {
    0: "AssetPhysPreset", 1: "AssetPhysCollMap", 2: "AssetXAnim", 3: "AssetXModelSurfs", 4: "AssetXModel",
    5: "AssetMaterial", 6: "AssetPixelShader", 7: "AssetVertexShader", 8: "AssetVertexDecl", 9: "AssetTechniqueSet",
    10: "AssetImage", 11: "AssetSound", 12: "AssetSoundCurve", 13: "AssetLoadedSound", 14: "AssetClipMapMp",
    15: "AssetClipMapMp", 16: "AssetComWorld", 17: "AssetGameWorldSp", 18: "AssetGameWorldMp",
    19: "AssetMapEnts", 20: "AssetFxWorld", 21: "AssetGfxWorld", 22: "AssetLightDef", 24: "AssetFont",
    25: "AssetMenuList", 26: "AssetMenu", 27: "AssetLocalize", 28: "AssetWeapon", 30: "AssetFx",
    31: "AssetImpactFx", 36: "AssetRawFile", 37: "AssetStringTable", 38: "AssetLeaderboard",
    39: "AssetStructuredDataDef", 40: "AssetTracer", 41: "AssetVehicle", 42: "AssetAddonMapEnts",
}

# Memory blocks. The 360 zone header lists six sizes (vertex and index data go in the physical
# block); the PC one lists eight.
TEMP, PHYSICAL, RUNTIME, VIRTUAL, LARGE, CALLBACK, VERTEX, INDEX = range(8)
BLOCK_MAP = {"XFILE_BLOCK_TEMP": TEMP, "XFILE_BLOCK_PHYSICAL": PHYSICAL, "XFILE_BLOCK_RUNTIME": RUNTIME,
             "XFILE_BLOCK_VIRTUAL": VIRTUAL, "XFILE_BLOCK_LARGE": LARGE, "XFILE_BLOCK_CALLBACK": CALLBACK,
             "XFILE_BLOCK_VERTEX": PHYSICAL, "XFILE_BLOCK_INDEX": PHYSICAL}
PC_BLOCK_MAP = dict(BLOCK_MAP, XFILE_BLOCK_VERTEX=VERTEX, XFILE_BLOCK_INDEX=INDEX)


class Platform:
    def __init__(self, name, endian, blocks, types, structs, block_map):
        self.name, self.E, self.blocks = name, endian, blocks
        self.types, self.structs, self.block_map = types, structs, block_map
        self.header_bytes = 8 + 4 * blocks            # size, external size (PC) / 0, block sizes
        self.list_bytes = 16                          # script string count+ptr, asset count+ptr


PLATFORMS = {
    "xbox": Platform("xbox", ">", 6, ASSET_TYPES, ASSET_STRUCTS, BLOCK_MAP),
    "pc": Platform("pc", "<", 8, PC_ASSET_TYPES, PC_ASSET_STRUCTS, PC_BLOCK_MAP),
}


class ZoneError(Exception):
    pass


class ZB(bytes):
    """Bytes streamed from the zone, remembering where they start in it (zpos, or None for
    runtime data that is not in the file)."""
    zpos = None


class ZBA(bytearray):
    """A struct built from several streamed pieces: segs lists (offset in here, zone pos)."""
    def __init__(self, *a):
        super().__init__(*a)
        self.segs = []


def zpos_of(buf, loc):
    """Zone position of byte loc of a streamed buffer, or None if unknown."""
    if isinstance(buf, ZBA):
        best = None
        for off, zp in buf.segs:
            if off <= loc:
                best = (off, zp)
        if best is None or best[1] is None:
            return None
        return best[1] + loc - best[0]
    zp = getattr(buf, "zpos", None)
    return None if zp is None else zp + loc


def zextend(dst, src, off=0, n=None):
    """dst.extend(src[off:off+n]) keeping track of where the bytes came from."""
    if n is None:
        n = len(src) - off
    base = len(dst)
    dst.extend(src[off:off + n])
    if isinstance(src, ZBA):
        for so, zp in src.segs:
            if so < off + n:
                rel = max(so, off)
                dst.segs.append((base + rel - off, None if zp is None else zp + rel - so))
    else:
        dst.segs.append((base, zpos_of(src, off)))


class Inst:
    """One struct (or union) instance: a view into a byte buffer."""
    __slots__ = ("info", "buf", "off", "index", "where")

    def __init__(self, info, buf, off=0, index=0, where=None):
        self.info, self.buf, self.off, self.index, self.where = info, buf, off, index, where

    def field(self, chain, idx=(), E=">"):
        """Value of a member chain (list of MemberInfo), with optional array indices."""
        off = self.off
        for mi in chain[:-1]:
            off += mi.m.offset
        mi = chain[-1]
        m = mi.m
        off += m.offset
        t = m.type
        dims = [x for x in m.mods if x != PTR]
        if m.mods and m.mods[0] == PTR:
            return struct.unpack_from(E + "I", self.buf, off)[0]
        if idx:
            stride = t.size
            for d in dims[len(idx):]:
                stride *= d
            for k, i in enumerate(idx):
                sub = stride
                for d in dims[k + 1:len(idx)]:
                    sub *= d
                off += int(i) * sub
        if isinstance(t, (Prim, Enum)):
            v = struct.unpack_from(E + t.fmt, self.buf, off)[0]
            if m.bits is not None:
                v = (v >> m.bitpos) & ((1 << m.bits) - 1)
            return v
        raise ZoneError("cannot read %s as a number" % mi)

    def __repr__(self):
        return "<%s @%s>" % (self.info.name, self.where)


class Reader:
    def __init__(self, zone, sch=None, trace=False, record=False):
        self.s = sch or schema_mod.load()
        self.plat = PLATFORMS[self.s.platform]
        self.E = self.plat.E
        nb = self.plat.blocks
        self.zone = zone
        self.pos = 0
        self.trace = [] if trace else None
        hdr = struct.unpack_from(self.E + "%dI" % (2 + nb), zone, 0)
        self.header = hdr
        self.block_sizes = list(hdr[2:2 + nb])
        self.block_pos = [0] * nb
        self.block = TEMP
        self.stack = []
        self.vars = {}
        self.assets = []
        self.aliases = 0
        self.inserts = 0
        self.path = []
        self.ctx = []       # (instance, member, indices, kind) of each pointer/member being loaded
        self.pointed = {}   # MemberInfo id -> bytes most recently loaded for that pointer
        # With record=True every piece of streamed data is kept as (pos, n, desc, asset index, path)
        # so it can be decoded into fields and written back (see codec.py).
        self.records = [] if record else None
        self.cur_asset = None
        self.asset_strings = {}
        # Layout tracking for relocation (see relocate.py): every piece of block memory the load
        # uses, in order, as (block, block offset, size, asset index, zone pos or None), and
        # every pointer that refers back to earlier data as (zone pos of the pointer, value).
        self.spans = []
        self.alias_list = []
        self.block_max = [0] * nb
        self.pos = self.plat.header_bytes

    # ------------------------------------------------------------ stream

    def read(self, n, desc=("raw",)):
        """Stream n bytes. desc says what they are: ("type", ctype, count), ("partial", ctype),
        ("ptrs", count), ("string",) or ("raw",)."""
        zp = None
        if self.block == RUNTIME:
            data = ZB(bytes(n))
        else:
            zp = self.pos
            data = ZB(self.zone[self.pos:self.pos + n])
            data.zpos = zp
            if len(data) < n:
                raise ZoneError("read past end of zone at %d (+%d) in %s" % (self.pos, n, " > ".join(self.path)))
            if self.trace is not None and n:
                self.trace.append((self.pos, n, self.block, self.block_pos[self.block], " > ".join(self.path)))
            if self.records is not None and n:
                self.records.append((self.pos, n, desc, self.cur_asset[0] if self.cur_asset else -1,
                                     " > ".join(self.path[1:])))
            self.pos += n
        where = (self.block, self.block_pos[self.block])
        if n:
            self.spans.append((self.block, where[1], n, self.cur_asset[0] if self.cur_asset else -1, zp))
        self.block_pos[self.block] += n
        if self.block_pos[self.block] > self.block_max[self.block]:
            self.block_max[self.block] = self.block_pos[self.block]
        return data, where

    def alloc(self, align):
        p = self.block_pos[self.block]
        self.block_pos[self.block] = (p + align - 1) // align * align

    def push(self, b):
        self.stack.append((self.block_pos[b], self.block))
        self.block = b

    def pop(self):
        saved, prev = self.stack.pop()
        if self.block == TEMP:
            self.block_pos[TEMP] = saved
        self.block = prev

    def insert_pointer(self):
        self.push(VIRTUAL)
        self.alloc(4)
        self.spans.append((VIRTUAL, self.block_pos[VIRTUAL], 4, self.cur_asset[0] if self.cur_asset else -1, None))
        self.block_pos[VIRTUAL] += 4
        self.pop()
        self.inserts += 1

    def string_at(self, val):
        """The string a back-pointer refers to (bytes), or None if it can't be found."""
        import bisect
        if getattr(self, "_vspans", None) is None:
            rows = sorted((sp[1], sp[2], sp[4]) for sp in self.spans if sp[0] == VIRTUAL and sp[4] is not None)
            self._vspans = ([r[0] for r in rows], rows)
        starts, rows = self._vspans
        off = (val - 1) & 0x0FFFFFFF
        i = bisect.bisect_right(starts, off) - 1
        if i < 0 or (val - 1) >> 28 != VIRTUAL:
            return None
        start, n, zp = rows[i]
        if off - start >= n:
            return None
        p = zp + off - start
        end = self.zone.find(b"\0", p)
        return None if end < 0 else self.zone[p:end]

    def alias(self, buf, loc, val):
        self.aliases += 1
        self.alias_list.append((zpos_of(buf, loc), val))

    # ------------------------------------------------------------ helpers

    def lookup(self, ref, idx):
        if ref[0] == "index":
            return self.vars[ref[1]].index
        tname, names = ref
        inst = self.vars.get(tname)
        if tname == "XModelLodInfo" and self.cur_asset is not None and self.cur_asset[1] == 3:
            # A model surfaces asset listed on its own (not reached through a model's LOD
            # entry) carries the surface count itself; an earlier model's LOD entry is stale.
            inst = self.vars.get("XModelSurfs", inst)
        if inst is None:
            raise ZoneError("expression needs %s but none is loaded" % tname)
        chain = []
        info = inst.info
        for n in names:
            mi = self.s._find_member(info, n)
            chain.extend(mi)
            info = mi[-1].type
        m = chain[-1].m
        if idx and m.mods and m.mods[0] == PTR:
            # Indexing through a pointer: use the data that was loaded for it.
            data = self.pointed.get(id(chain[-1]))
            if data is None:
                raise ZoneError("expression needs %s data but none is loaded" % chain[-1])
            t = m.type
            return struct.unpack_from(self.E + t.fmt, data, int(idx[0]) * t.size)[0]
        return inst.field(chain, idx, self.E)

    def block_of(self, mi):
        return self.plat.block_map[mi.block.name] if mi.block else None

    @staticmethod
    def align_of(info):
        return info.alloc_align or info.ctype.align

    def on_inst(self, inst, at_start):
        """Called for every struct instance as its members are about to load (see tree.py)."""

    # ------------------------------------------------------------ structs

    def load_struct(self, info, inst, at_start):
        """Load_X(atStreamStart). Returns the instance (new when at_start)."""
        self.path.append(info.name)
        try:
            return self._load_struct(info, inst, at_start)
        finally:
            self.path.pop()

    def _load_struct(self, info, inst, at_start):
        s = self.s
        dyn = s.dynamic_member(info)
        if at_start and not (info.is_union and dyn is not None):
            size = dyn.m.offset if dyn is not None else info.ctype.size
            data, where = self.read(size, ("partial", info.ctype) if dyn is not None else ("type", info.ctype, 1))
            if dyn is not None:
                buf = ZBA()
                zextend(buf, data)
            else:
                buf = data
            inst = Inst(info, buf, 0, 0, where)
        elif at_start:
            # A union whose members are only partly known: each member streams itself.
            inst = Inst(info, ZBA(), 0, 0, (self.block, self.block_pos[self.block]))
        self.vars[info.name] = inst
        self.on_inst(inst, at_start)
        pushed = False
        if info.asset:
            self.push(VIRTUAL)
            pushed = True
        elif info.block:
            self.push(self.plat.block_map[info.block.name])
            pushed = True
        if info.is_union:
            for mi in s.used_members(info):
                if not s.needs_treatment(mi):
                    continue
                if mi.condition is None or mi.condition(self.lookup):
                    self.vars[info.name] = inst
                    self.load_reference(inst, mi, 0, [])
                    break
        else:
            for mi in info.ordered:
                if not s.needs_treatment(mi):
                    continue
                if mi.condition is not None and not mi.condition(self.lookup):
                    continue
                self.vars[info.name] = inst
                self.load_reference(inst, mi, 0, [])
        if pushed:
            self.pop()
        return inst

    def load_array(self, info, at_start, count, buf=None, base=0):
        """LoadArray_X: count elements of a non-leaf struct."""
        where = None
        if at_start:
            buf, where = self.read(info.ctype.size * count, ("type", info.ctype, count))
            base = 0
        out = []
        for i in range(count):
            e = Inst(info, buf, base + i * info.ctype.size, i, where)
            self.vars[info.name] = e
            self.load_struct(info, e, False)
            out.append(e)
        return buf

    # ------------------------------------------------------------ members

    def _member_loc(self, inst, mi, indices):
        """Byte offset (in inst.buf) of the member element selected by indices."""
        m = mi.m
        dims = []
        for x in m.mods:
            if x == PTR:
                break
            dims.append(x)
        elem = 4 if PTR in m.mods else m.type.size
        off = inst.off + m.offset
        for k, i in enumerate(indices):
            stride = elem
            for d in dims[k + 1:]:
                stride *= d
            off += i * stride
        return off

    def load_reference(self, inst, mi, mod_pos, indices):
        s = self.s
        mods = mi.m.mods
        cur = mods[mod_pos] if mod_pos < len(mods) else None
        nxt = mods[mod_pos + 1] if mod_pos + 1 < len(mods) else None
        combined = 0
        if indices:
            dims = [x for x in mods if x != PTR]
            for k, i in enumerate(indices):
                st = 1
                for d in dims[k + 1:]:
                    st *= d
                combined += i * st
        following_ptr = PTR in mods[mod_pos + 1:]
        if cur is not None and cur != PTR and mod_pos in mi.array_size:
            kind = "dynamic"
        elif cur == PTR and not mi.count_is_array(mod_pos, combined) and not following_ptr:
            kind = "single"
        elif cur == PTR and mi.count_is_array(mod_pos, combined) and not following_ptr:
            kind = "array"
        elif (cur == PTR and mi.count_is_array(mod_pos, combined) or cur not in (None, PTR)) and nxt == PTR \
                and not mi.any_count_is_array(mod_pos + 1):
            kind = "ptrarray"
        elif cur not in (None, PTR) and nxt is None:
            kind = "embarray"
        elif cur is None:
            kind = "embedded"
        elif cur != PTR:
            for i in range(cur):
                self.load_reference(inst, mi, mod_pos + 1, indices + [i])
            return
        else:
            raise ZoneError("unsupported member %s" % mi)
        loc = self._member_loc(inst, mi, indices)
        self.path.append("%s%s(%s)" % (mi.name, "".join("[%d]" % i for i in indices), kind))
        self.ctx.append((inst, mi, tuple(indices), kind))
        try:
            self.load_block(inst, mi, mod_pos, combined, kind, loc)
        finally:
            self.ctx.pop()
            self.path.pop()

    def load_block(self, inst, mi, mod_pos, combined, kind, loc):
        b = mi.block
        if mi.block_cond is not None:
            cond, bt, bf = mi.block_cond
            b = bt if cond(self.lookup) else bf
        push = b is not None and not (b.kind == "normal" and b.default)
        if push:
            self.push(self.plat.block_map[b.name])
        self.pointer_check(inst, mi, mod_pos, combined, kind, loc)
        if push:
            self.pop()

    def pointer_check(self, inst, mi, mod_pos, combined, kind, loc):
        is_array = mi.m.mods[mod_pos] != PTR if mod_pos < len(mi.m.mods) else False
        check = kind in ("array", "ptrarray", "single") and not (kind == "ptrarray" and is_array) \
            and not (kind == "single" and mi.is_string)
        if check:
            val = struct.unpack_from(self.E + "I", inst.buf, loc)[0]
            if not val:
                return
            self.reuse(inst, mi, mod_pos, combined, kind, loc, val)
        else:
            self.reuse(inst, mi, mod_pos, combined, kind, loc, None)

    def reuse(self, inst, mi, mod_pos, combined, kind, loc, val):
        is_array = mi.m.mods[mod_pos] != PTR if mod_pos < len(mi.m.mods) else False
        make = kind in ("array", "single", "ptrarray") and not (kind == "ptrarray" and is_array)
        if not make or not mi.is_reusable:
            return self.alloc_member(inst, mi, mod_pos, combined, kind, loc, val)
        if val is None:
            val = struct.unpack_from(self.E + "I", inst.buf, loc)[0]
        in_temp = mi.block is not None and mi.block.kind == "temp"
        if val == FOLLOWING or (in_temp and val == INSERT):
            return self.alloc_member(inst, mi, mod_pos, combined, kind, loc, val)
        self.alias(inst.buf, loc, val)

    def is_asset(self, mi):
        return mi.type is not None and mi.type.asset is not None

    def alloc_member(self, inst, mi, mod_pos, combined, kind, loc, val):
        s = self.s
        is_array = mi.m.mods[mod_pos] != PTR if mod_pos < len(mi.m.mods) else False
        make = kind in ("array", "ptrarray", "single")
        if kind == "ptrarray":
            make = not is_array
        elif mi.is_string or self.is_asset(mi):
            make = False
        if not make:
            return self.type_check(inst, mi, mod_pos, combined, kind, loc)
        following = mi.m.mods[mod_pos + 1:]
        if mi.alloc_align:
            align = mi.alloc_align
        elif PTR in following:
            align = 4
        else:
            align = mi.type.ctype.align if mi.type is not None else mi.m.type.align
            if mi.type is not None and mi.type.alloc_align:
                align = mi.type.alloc_align
        self.alloc(align)
        in_temp = mi.block is not None and mi.block.kind == "temp"
        if val is None:
            val = struct.unpack_from(self.E + "I", inst.buf, loc)[0]
        if in_temp and val == INSERT:
            self.insert_pointer()
        self.type_check(inst, mi, mod_pos, combined, kind, loc)

    def elem_size(self, mi, mod_pos):
        """Size of what a pointer at mod_pos points to (one element)."""
        size = mi.m.type.size
        for x in mi.m.mods[mod_pos + 1:]:
            if x == PTR:
                return 4
            size *= x
        return size

    def type_check(self, inst, mi, mod_pos, combined, kind, loc):
        s = self.s
        in_runtime = mi.block is not None and mi.block.kind == "runtime"
        if mi.is_string:
            if kind == "single":
                self.load_xstring(inst.buf, loc)
            elif kind == "ptrarray":
                if mi.m.mods[mod_pos] != PTR:
                    self.load_xstring_array(False, mi.m.mods[mod_pos], inst.buf, loc)
                else:
                    n = mi.count_expr(mod_pos, combined)(self.lookup)
                    self.load_xstring_array(True, n)
            return
        if self.is_asset(mi):
            if kind == "single":
                self.load_asset_ptr(mi.type, inst.buf, loc)
            elif kind == "ptrarray":
                self._ptrarray(inst, mi, mod_pos, combined, loc)
            return
        t = mi.type
        if kind == "array":
            n = mi.count_expr(mod_pos, combined)(self.lookup)
            if t is not None and not t.is_leaf and not in_runtime:
                self.load_array(t, True, n)
            else:
                data, _ = self.read(self.elem_size(mi, mod_pos) * n, self.elem_desc(mi, mod_pos, n))
                self.pointed[id(mi)] = data
        elif kind == "single":
            if t is not None and not t.is_leaf and not in_runtime:
                self.load_struct(t, None, True)
            else:
                self.read(self.elem_size(mi, mod_pos), self.elem_desc(mi, mod_pos, 1))
        elif kind == "ptrarray":
            self._ptrarray(inst, mi, mod_pos, combined, loc)
        elif kind == "embarray":
            n = mi.m.mods[mod_pos]
            if mod_pos in mi.array_count:
                n = mi.array_count[mod_pos](self.lookup)
            after = s.after_partial_load(mi)
            if not mi.is_leaf and t is not None:
                if after:
                    self.load_array(t, True, n)
                else:
                    self.load_array(t, False, n, inst.buf, loc)
            elif after:
                self.read(t.ctype.size * n if t else mi.m.type.size * n, ("type", t.ctype if t else mi.m.type, n))
        elif kind == "embedded":
            after = s.after_partial_load(mi)
            if not mi.is_leaf and t is not None:
                if after:
                    sub = self.load_struct(t, None, True)
                    if isinstance(inst.buf, bytearray) and sub is not None:
                        if isinstance(sub.buf, bytearray):
                            zextend(inst.buf, sub.buf)
                        else:
                            zextend(inst.buf, sub.buf, sub.off, t.ctype.size)
                else:
                    # A struct wrapping another at offset 0 (GfxCellTree128 around GfxCellTree)
                    # keeps its array index, for counts written as pointer differences.
                    index = inst.index if loc == inst.off else 0
                    self.load_struct(t, Inst(t, inst.buf, loc, index, inst.where), False)
            elif after:
                data, _ = self.read(t.ctype.size if t else mi.m.type.size, ("type", t.ctype if t else mi.m.type, 1))
                if isinstance(inst.buf, bytearray):
                    zextend(inst.buf, data)
        elif kind == "dynamic":
            n = mi.array_size[mod_pos](self.lookup)
            if t is not None and not t.is_leaf:
                buf = self.load_array(t, True, n)
            else:
                buf, _ = self.read(self.elem_size_embedded(mi) * n, self.elem_desc(mi, 0, n))
            if isinstance(inst.buf, bytearray):
                zextend(inst.buf, buf)

    def elem_desc(self, mi, mod_pos, n):
        """Descriptor for n elements of what a member points to (or of a dynamic array)."""
        k = 1
        for x in mi.m.mods[mod_pos + 1:]:
            if x == PTR:
                return ("ptrs", n * k)
            k *= x
        return ("type", mi.m.type, n * k)

    def elem_size_embedded(self, mi):
        size = mi.m.type.size
        for x in mi.m.mods[1:]:
            size *= x
        return size

    def _ptrarray(self, inst, mi, mod_pos, combined, loc):
        cur = mi.m.mods[mod_pos]
        if cur != PTR:
            self.load_ptr_array(mi, False, cur, inst.buf, loc)
        else:
            n = mi.count_expr(mod_pos, combined)(self.lookup)
            self.load_ptr_array(mi, True, n)

    def load_ptr_array(self, mi, at_start, count, buf=None, loc=0):
        if at_start:
            buf, _ = self.read(4 * count, ("ptrs", count))
            loc = 0
        t = mi.type
        for i in range(count):
            val = struct.unpack_from(self.E + "I", buf, loc + 4 * i)[0]
            if not val:
                continue
            if t is not None and t.asset:
                self.load_asset_ptr(t, buf, loc + 4 * i)
                continue
            if mi.is_reusable and val != FOLLOWING:
                self.alias(buf, loc + 4 * i, val)
                continue
            align = self.align_of(t) if t is not None else mi.m.type.align
            self.alloc(align)
            if t is not None and not t.is_leaf:
                self.load_struct(t, None, True)
            else:
                self.read(t.ctype.size if t is not None else mi.m.type.size,
                          ("type", t.ctype if t is not None else mi.m.type, 1))

    # ------------------------------------------------------------ strings and assets

    def load_xstring(self, buf, loc):
        val = struct.unpack_from(self.E + "I", buf, loc)[0]
        if not val:
            return None
        if val != FOLLOWING:
            self.alias(buf, loc, val)
            if self.cur_asset is not None and len(self.path) <= 4:
                self.asset_strings.setdefault(self.cur_asset[0], []).append((" > ".join(self.path), val))
            return None
        end = self.zone.index(b"\0", self.pos)
        data, _ = self.read(end + 1 - self.pos, ("string",))
        if self.cur_asset is not None and len(self.path) <= 4:
            self.asset_strings.setdefault(self.cur_asset[0], []).append((" > ".join(self.path), data[:-1]))
        return data[:-1]

    def load_xstring_array(self, at_start, count, buf=None, loc=0):
        if at_start:
            buf, _ = self.read(4 * count, ("ptrs", count))
            loc = 0
        for i in range(count):
            self.load_xstring(buf, loc + 4 * i)

    def load_asset_ptr(self, info, buf, loc):
        val = struct.unpack_from(self.E + "I", buf, loc)[0]
        in_temp = info.block is not None and info.block.kind == "temp"
        if in_temp:
            self.push(TEMP)
        inst = None
        if val:
            if val == FOLLOWING or (in_temp and val == INSERT):
                self.alloc(self.align_of(info))
                if in_temp and val == INSERT:
                    self.insert_pointer()
                start = self.pos
                inst = self.load_struct(info, None, True)
                self.assets.append((info, inst, start, self.pos))
            else:
                self.alias(buf, loc, val)
        if in_temp:
            self.pop()
        return inst

    # ------------------------------------------------------------ zone

    def walk(self, types=None):
        ss_count, ss_ptr, count, ptr = struct.unpack_from(self.E + "4I", self.zone, self.pos)
        self.list_header = (ss_count, ss_ptr, count, ptr)
        self.pos += 16
        self.script_strings = []
        self.push(VIRTUAL)
        if ss_ptr:
            self.alloc(4)
            buf, _ = self.read(4 * ss_count, ("ptrs", ss_count))
            for i in range(ss_count):
                self.script_strings.append(self.load_xstring(buf, 4 * i))
        self.pop()
        self.push(VIRTUAL)
        entries = []
        if ptr:
            self.alloc(4)
            buf, _ = self.read(8 * count, ("u32", 2 * count))
            for i in range(count):
                t, p = struct.unpack_from(self.E + "II", buf, 8 * i)
                entries.append((t, p))
            for i, (t, p) in enumerate(entries):
                name = self.plat.structs.get(t)
                if name is None:
                    raise ZoneError("asset %d has unsupported type %d (%s) at %d" % (
                        i, t, self.plat.types[t] if t < len(self.plat.types) else "?", self.pos))
                info = self.s.assets[name]
                self.cur_asset = (i, t)
                self.load_asset_ptr(info, buf, 8 * i + 4)
        self.pop()
        self.entries = entries
        return self
