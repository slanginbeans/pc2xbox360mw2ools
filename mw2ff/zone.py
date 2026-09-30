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

# Memory blocks on the 360 (the zone header lists six sizes).
TEMP, PHYSICAL, RUNTIME, VIRTUAL, LARGE, CALLBACK = range(6)
BLOCK_MAP = {"XFILE_BLOCK_TEMP": TEMP, "XFILE_BLOCK_PHYSICAL": PHYSICAL, "XFILE_BLOCK_RUNTIME": RUNTIME,
             "XFILE_BLOCK_VIRTUAL": VIRTUAL, "XFILE_BLOCK_LARGE": LARGE, "XFILE_BLOCK_CALLBACK": CALLBACK,
             "XFILE_BLOCK_VERTEX": PHYSICAL, "XFILE_BLOCK_INDEX": PHYSICAL}


class ZoneError(Exception):
    pass


class Inst:
    """One struct (or union) instance: a view into a byte buffer."""
    __slots__ = ("info", "buf", "off", "index", "where")

    def __init__(self, info, buf, off=0, index=0, where=None):
        self.info, self.buf, self.off, self.index, self.where = info, buf, off, index, where

    def field(self, chain, idx=()):
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
            return struct.unpack_from(">I", self.buf, off)[0]
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
            v = struct.unpack_from(">" + t.fmt, self.buf, off)[0]
            if m.bits is not None:
                v = (v >> m.bitpos) & ((1 << m.bits) - 1)
            return v
        raise ZoneError("cannot read %s as a number" % mi)

    def __repr__(self):
        return "<%s @%s>" % (self.info.name, self.where)


class Reader:
    def __init__(self, zone, sch=None, trace=False, record=False):
        self.s = sch or schema_mod.load()
        self.zone = zone
        self.pos = 0
        self.trace = [] if trace else None
        hdr = struct.unpack_from(">8I", zone, 0)
        self.header = hdr
        self.block_sizes = list(hdr[2:8])
        self.block_pos = [0] * 6
        self.block = TEMP
        self.stack = []
        self.vars = {}
        self.assets = []
        self.aliases = 0
        self.inserts = 0
        self.path = []
        self.pointed = {}   # MemberInfo id -> bytes most recently loaded for that pointer
        # With record=True every piece of streamed data is kept as (pos, n, desc, asset index, path)
        # so it can be decoded into fields and written back (see codec.py).
        self.records = [] if record else None
        self.cur_asset = None
        self.asset_strings = {}
        self.pos = 32

    # ------------------------------------------------------------ stream

    def read(self, n, desc=("raw",)):
        """Stream n bytes. desc says what they are: ("type", ctype, count), ("partial", ctype),
        ("ptrs", count), ("string",) or ("raw",)."""
        if self.block == RUNTIME:
            data = bytes(n)
        else:
            data = self.zone[self.pos:self.pos + n]
            if len(data) < n:
                raise ZoneError("read past end of zone at %d (+%d) in %s" % (self.pos, n, " > ".join(self.path)))
            if self.trace is not None and n:
                self.trace.append((self.pos, n, self.block, self.block_pos[self.block], " > ".join(self.path)))
            if self.records is not None and n:
                self.records.append((self.pos, n, desc, self.cur_asset[0] if self.cur_asset else -1,
                                     " > ".join(self.path[1:])))
            self.pos += n
        where = (self.block, self.block_pos[self.block])
        self.block_pos[self.block] += n
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
        self.block_pos[VIRTUAL] += 4
        self.pop()
        self.inserts += 1

    # ------------------------------------------------------------ helpers

    def lookup(self, ref, idx):
        if ref[0] == "index":
            return self.vars[ref[1]].index
        tname, names = ref
        inst = self.vars.get(tname)
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
            return struct.unpack_from(">" + t.fmt, data, int(idx[0]) * t.size)[0]
        return inst.field(chain, idx)

    def block_of(self, mi):
        return BLOCK_MAP[mi.block.name] if mi.block else None

    @staticmethod
    def align_of(info):
        return info.alloc_align or info.ctype.align

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
            inst = Inst(info, bytearray(data) if dyn is not None else data, 0, 0, where)
        elif at_start:
            # A union whose members are only partly known: each member streams itself.
            inst = Inst(info, bytearray(), 0, 0, (self.block, self.block_pos[self.block]))
        self.vars[info.name] = inst
        pushed = False
        if info.asset:
            self.push(VIRTUAL)
            pushed = True
        elif info.block:
            self.push(BLOCK_MAP[info.block.name])
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
        try:
            self.load_block(inst, mi, mod_pos, combined, kind, loc)
        finally:
            self.path.pop()

    def load_block(self, inst, mi, mod_pos, combined, kind, loc):
        b = mi.block
        if mi.block_cond is not None:
            cond, bt, bf = mi.block_cond
            b = bt if cond(self.lookup) else bf
        push = b is not None and not (b.kind == "normal" and b.default)
        if push:
            self.push(BLOCK_MAP[b.name])
        self.pointer_check(inst, mi, mod_pos, combined, kind, loc)
        if push:
            self.pop()

    def pointer_check(self, inst, mi, mod_pos, combined, kind, loc):
        is_array = mi.m.mods[mod_pos] != PTR if mod_pos < len(mi.m.mods) else False
        check = kind in ("array", "ptrarray", "single") and not (kind == "ptrarray" and is_array) \
            and not (kind == "single" and mi.is_string)
        if check:
            val = struct.unpack_from(">I", inst.buf, loc)[0]
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
            val = struct.unpack_from(">I", inst.buf, loc)[0]
        in_temp = mi.block is not None and mi.block.kind == "temp"
        if val == FOLLOWING or (in_temp and val == INSERT):
            return self.alloc_member(inst, mi, mod_pos, combined, kind, loc, val)
        self.aliases += 1

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
            val = struct.unpack_from(">I", inst.buf, loc)[0]
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
                        inst.buf.extend(sub.buf[sub.off:sub.off + t.ctype.size] if not isinstance(sub.buf, bytearray) else sub.buf)
                else:
                    self.load_struct(t, Inst(t, inst.buf, loc, 0, inst.where), False)
            elif after:
                data, _ = self.read(t.ctype.size if t else mi.m.type.size, ("type", t.ctype if t else mi.m.type, 1))
                if isinstance(inst.buf, bytearray):
                    inst.buf.extend(data)
        elif kind == "dynamic":
            n = mi.array_size[mod_pos](self.lookup)
            if t is not None and not t.is_leaf:
                buf = self.load_array(t, True, n)
            else:
                buf, _ = self.read(self.elem_size_embedded(mi) * n, self.elem_desc(mi, 0, n))
            if isinstance(inst.buf, bytearray):
                inst.buf.extend(buf)

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
            val = struct.unpack_from(">I", buf, loc + 4 * i)[0]
            if not val:
                continue
            if t is not None and t.asset:
                self.load_asset_ptr(t, buf, loc + 4 * i)
                continue
            if mi.is_reusable and val != FOLLOWING:
                self.aliases += 1
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
        val = struct.unpack_from(">I", buf, loc)[0]
        if not val:
            return None
        if val != FOLLOWING:
            self.aliases += 1
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
        val = struct.unpack_from(">I", buf, loc)[0]
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
                self.aliases += 1
        if in_temp:
            self.pop()
        return inst

    # ------------------------------------------------------------ zone

    def walk(self, types=None):
        ss_count, ss_ptr, count, ptr = struct.unpack_from(">4I", self.zone, self.pos)
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
                t, p = struct.unpack_from(">II", buf, 8 * i)
                entries.append((t, p))
            for i, (t, p) in enumerate(entries):
                name = ASSET_STRUCTS.get(t)
                if name is None:
                    raise ZoneError("asset %d has unsupported type %d (%s) at %d" % (
                        i, t, ASSET_TYPES[t] if t < len(ASSET_TYPES) else "?", self.pos))
                info = self.s.assets[name]
                self.cur_asset = (i, t)
                self.load_asset_ptr(info, buf, 8 * i + 4)
        self.pop()
        self.entries = entries
        return self
