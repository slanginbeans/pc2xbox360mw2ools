"""A zone as a tree of values, and a tree written back out as a zone.

TreeReader walks a zone like zone.Reader and keeps every struct as a dict of its fields
(codec.py values). What a pointer member loaded hangs under the struct's "@" key, by
(member name, array indices):

    dict         a struct the pointer loaded
    [dict, ...]  an array of structs
    Leaf         plain data (numbers, vertices, pixels) as raw bytes
    Str          a string
    PtrList      an array of pointers (each item is any of these, or None)
    Ref          a pointer back to something loaded earlier (an alias)

Members embedded in a struct after a partial load (and dynamic arrays) are stored as the
member's own value instead. Asset roots have "_asset" (their struct name).

TreeWriter walks a schema (either platform) over such a tree and produces the zone: the
same walk the game's loader does, so every count, block and alignment comes out right, and
aliases are recomputed from where their targets were written. Written with the tree's
own schema it gives back the original zone byte for byte; written with the other
platform's schema, after port.py has turned the values into that platform's structs, it
converts the file.
"""

import bisect
import struct

import codec as codec_mod
import zone as zone_mod
from cdefs import PTR, Compound, Enum, Prim
from zone import CALLBACK, FOLLOWING, INSERT, RUNTIME, TEMP, VIRTUAL, ZB, Inst, ZoneError

BLOCK_SHIFT = 28
OFFSET_MASK = (1 << BLOCK_SHIFT) - 1
NONE = object()          # "nothing loaded" (different from a null pointer child)


class Str:
    __slots__ = ("b",)

    def __init__(self, b):
        self.b = bytes(b)

    def __repr__(self):
        return "Str(%r)" % self.b


class Leaf:
    """Plain data: n elements of ctype t, as raw bytes in endian E."""
    __slots__ = ("t", "n", "raw", "E")

    def __init__(self, t, n, raw, E):
        self.t, self.n, self.raw, self.E = t, n, bytes(raw), E

    def __repr__(self):
        return "Leaf(%s x%d)" % (getattr(self.t, "name", self.t), self.n)


class PtrList(list):
    """Items a pointer array loaded; raw = the pointer values as streamed (codec markers)."""
    raw = None


class Tail(list):
    """A copy of the end of another array, written with as many items as the file asks for."""


class Ref:
    """An alias: val as streamed; target/rel/t are filled in once the walk has finished
    (the object it points into, the byte offset inside it, and that object's element type)."""
    __slots__ = ("val", "target", "rel", "t")

    def __init__(self, val):
        self.val, self.target, self.rel, self.t = val, None, 0, None

    def __repr__(self):
        return "Ref(0x%08x)" % self.val


class AssetEntry(list):
    """[type name, asset] in the zone's asset list."""


class InsertSlot:
    """The pointer a temp-block asset reserves for itself in virtual memory."""
    __slots__ = ("asset",)

    def __init__(self, asset):
        self.asset = asset


def mkey(t, m):
    """The key a member has in its struct's dict (codec.py names unnamed members _<n>)."""
    if m.name:
        return m.name
    return "_%d" % next(i for i, x in enumerate(t.members) if x is m)


def member_value(d, t, m, codec):
    """d[member]; for a member of a union, decode it out of the union's bytes first (and
    remember that, so the writer puts it back)."""
    if t.kind == "typedef":
        return d
    k = mkey(t, m)
    if k not in d and "union" in d:
        d[k] = codec._decode_member(m, bytes.fromhex(d["union"]), 0)
        d.setdefault("_um", []).append(k)
    return d[k]


class ZBW(bytearray):
    """Written bytes that can still be patched (a pointer turned from alias into data)."""
    zpos = None


def slot_asset(r):
    """If alias r points at a pointer to an asset (an asset list entry, a temp asset's own
    slot, or a pointer member of a struct), that asset (or Ref), else None."""
    t, tgt, rel = r.t, r.target, r.rel
    if isinstance(tgt, AssetEntry) and rel == 4:
        return tgt[1]
    if isinstance(tgt, InsertSlot):
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
            c = tgt.get("@", {}).get((mkey(t, m), ()))
            if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
                return c
    return None


def nav(v, idx):
    for i in idx:
        v = v[i]
    return v


def _set_nav(d, name, idx, value):
    if not idx:
        d[name] = value
        return
    v = d[name]
    for i in idx[:-1]:
        v = v[i]
    v[idx[-1]] = value


# ================================================================ reading

class TreeReader(zone_mod.Reader):
    def __init__(self, zone, sch=None, lenient=False):
        super().__init__(zone, sch)
        self.lenient = lenient    # keep going past what can be read but not written back
        self.codec = codec_mod.Codec(self.s)
        self.reg = {}        # id(inst) -> (inst, dict)
        self.locs = []       # (block, offset, size, object, element type or size)
        self.refs = []
        self._cap = NONE
        self._slot = None
        self._asset_insert = False
        self._member_slots = []     # slots reserved by struct members (not assets), innermost last

    # ---------------------------------------------------------------- values

    def _decode(self, t, raw):
        c = self.codec
        size = len(raw)
        v = c._decode_one(t, raw, 0, size)
        if not isinstance(v, dict):
            return v
        fixed = c._fixes(raw, lambda out: c._encode_one(t, v, out, 0, size))
        if fixed:
            v["_fix"] = fixed
        return v

    def read(self, n, desc=("raw",)):
        data, where = super().read(n, desc)
        if desc[0] == "type":
            self._cap = Leaf(desc[1], desc[2], data, self.E)
            self._where = where
        return data, where

    def alias(self, buf, loc, val):
        super().alias(buf, loc, val)
        r = Ref(val)
        self.refs.append(r)
        self._cap = r
        return r

    def on_inst(self, inst, at_start):
        if id(inst) in self.reg:
            return
        if at_start:
            if isinstance(inst.buf, zone_mod.ZBA) and not len(inst.buf):
                if not self.lenient:
                    raise ZoneError("%s: unions streamed member by member aren't supported yet" % inst.info.name)
                # Only for files that are looked things up in, never written (an animation's
                # translation data): the walk stays in step, the union itself isn't kept.
                self.reg[id(inst)] = (inst, {})
                return
            raw = bytes(inst.buf[inst.off:])
            d = self._decode(inst.info.ctype, raw)
            self.locs.append((inst.where[0], inst.where[1], len(raw), d, inst.info.ctype))
        else:
            pinst, mi, idx, kind = self.ctx[-1]
            d = nav(member_value(self.reg[id(pinst)][1], pinst.info.ctype, mi.m, self.codec), idx)
        self.reg[id(inst)] = (inst, d)

    def load_struct(self, info, inst, at_start):
        inst = super().load_struct(info, inst, at_start)
        if at_start:
            self._cap = self.reg[id(inst)][1]
        return inst

    def load_array(self, info, at_start, count, buf=None, base=0):
        size = info.ctype.size
        where = None
        if at_start:
            buf, where = zone_mod.Reader.read(self, size * count, ("type", info.ctype, count))
            base = 0
            lst = [self._decode(info.ctype, bytes(buf[i * size:(i + 1) * size])) for i in range(count)]
            self.locs.append((where[0], where[1], size * count, lst, info.ctype))
        else:
            pinst, mi, idx, kind = self.ctx[-1]
            lst = nav(member_value(self.reg[id(pinst)][1], pinst.info.ctype, mi.m, self.codec), idx)
        for i in range(count):
            e = Inst(info, buf, base + i * size, i, where)
            self.reg[id(e)] = (e, lst[i])
            self.vars[info.name] = e
            self.load_struct(info, e, False)
        self._cap = lst if at_start else NONE
        return buf

    def load_block(self, inst, mi, mod_pos, combined, kind, loc):
        self._cap = NONE
        depth = len(self._member_slots)
        super().load_block(inst, mi, mod_pos, combined, kind, loc)
        cap, self._cap = self._cap, NONE
        if len(self._member_slots) > depth:
            # This member reserved a pointer slot (XModelLodInfo.modelSurfs): later pointers
            # to what it holds point at the slot, so the slot must be something they can find.
            slot = self._member_slots.pop()
            if cap is not NONE:
                mark = InsertSlot(cap)
                if isinstance(cap, dict):
                    cap["_slot"] = mark
                self.locs.append((VIRTUAL, slot, 4, mark, 4))
        if cap is NONE:
            return
        if isinstance(cap, Leaf):
            self.locs.append((self._where[0], self._where[1], len(cap.raw), cap, cap.t))
        d = self.reg[id(inst)][1]
        idx = self.ctx[-1][2]
        k = mkey(inst.info.ctype, mi.m)
        if kind in ("embedded", "embarray", "dynamic"):
            _set_nav(d, k, idx, cap)
        else:
            d.setdefault("@", {})[(k, idx)] = cap

    def load_ptr_array(self, mi, at_start, count, buf=None, loc=0):
        where = None
        if at_start:
            buf, where = zone_mod.Reader.read(self, 4 * count, ("ptrs", count))
            loc = 0
        pl = PtrList()
        pl.raw = [codec_mod.ptr_value(struct.unpack_from(self.E + "I", buf, loc + 4 * i)[0]) for i in range(count)]
        if at_start:
            self.locs.append((where[0], where[1], 4 * count, pl, 4))
        t = mi.type
        for i in range(count):
            val = struct.unpack_from(self.E + "I", buf, loc + 4 * i)[0]
            if not val:
                pl.append(None)
                continue
            if t is not None and t.asset:
                self._cap = NONE
                self.load_asset_ptr(t, buf, loc + 4 * i)
                pl.append(self._cap)
                continue
            if mi.is_reusable and val != FOLLOWING:
                pl.append(self.alias(buf, loc + 4 * i, val))
                continue
            align = self.align_of(t) if t is not None else mi.m.type.align
            self.alloc(align)
            if t is not None and not t.is_leaf:
                inst = self.load_struct(t, None, True)
                pl.append(self.reg[id(inst)][1])
            else:
                ct = t.ctype if t is not None else mi.m.type
                self.read(ct.size, ("type", ct, 1))
                lf = self._cap
                self.locs.append((self._where[0], self._where[1], len(lf.raw), lf, ct))
                pl.append(lf)
        self._cap = pl

    def load_xstring(self, buf, loc):
        val = struct.unpack_from(self.E + "I", buf, loc)[0]
        if not val:
            self._cap = NONE
            return None
        if val != FOLLOWING:
            r = self.alias(buf, loc, val)
            if self.cur_asset is not None and len(self.path) <= 4:
                self.asset_strings.setdefault(self.cur_asset[0], []).append((" > ".join(self.path), val))
            self._cap = r
            return None
        end = self.zone.index(b"\0", self.pos)
        data, where = zone_mod.Reader.read(self, end + 1 - self.pos, ("string",))
        if self.cur_asset is not None and len(self.path) <= 4:
            self.asset_strings.setdefault(self.cur_asset[0], []).append((" > ".join(self.path), data[:-1]))
        st = Str(data[:-1])
        self.locs.append((where[0], where[1], len(data), st, 1))
        self._cap = st
        return data[:-1]

    def load_xstring_array(self, at_start, count, buf=None, loc=0):
        where = None
        if at_start:
            buf, where = zone_mod.Reader.read(self, 4 * count, ("ptrs", count))
            loc = 0
        pl = PtrList()
        pl.raw = [codec_mod.ptr_value(struct.unpack_from(self.E + "I", buf, loc + 4 * i)[0]) for i in range(count)]
        if at_start:
            self.locs.append((where[0], where[1], 4 * count, pl, 4))
        for i in range(count):
            self.load_xstring(buf, loc + 4 * i)
            pl.append(None if self._cap is NONE else self._cap)
        self._cap = pl

    def insert_pointer(self):
        self.push(VIRTUAL)
        self.alloc(4)
        self._slot = self.block_pos[VIRTUAL]
        self.pop()
        super().insert_pointer()
        if not self._asset_insert:
            self._member_slots.append(self._slot)

    def load_asset_ptr(self, info, buf, loc):
        val = struct.unpack_from(self.E + "I", buf, loc)[0]
        in_temp = info.block is not None and info.block.kind == "temp"
        if in_temp:
            self.push(TEMP)
        inst = None
        res = NONE
        if val:
            if val == FOLLOWING or (in_temp and val == INSERT):
                self.alloc(self.align_of(info))
                slot = None
                if in_temp and val == INSERT:
                    self._asset_insert = True
                    self.insert_pointer()
                    self._asset_insert = False
                    slot = self._slot
                start = self.pos
                inst = self.load_struct(info, None, True)
                self.assets.append((info, inst, start, self.pos))
                res = self.reg[id(inst)][1]
                res["_asset"] = info.name
                if slot is not None:
                    res["_slot"] = InsertSlot(res)
                    self.locs.append((VIRTUAL, slot, 4, res["_slot"], 4))
            else:
                res = self.alias(buf, loc, val)
        if in_temp:
            self.pop()
        self._cap = res
        return inst

    # ---------------------------------------------------------------- zone

    def walk(self, types=None):
        ss_count, ss_ptr, count, ptr = struct.unpack_from(self.E + "4I", self.zone, self.pos)
        self.list_header = (ss_count, ss_ptr, count, ptr)
        self.pos += 16
        root = {"platform": self.plat.name, "header": self.zone[:self.plat.header_bytes],
                "script_strings": None, "assets": []}
        self.script_strings = []
        self.push(VIRTUAL)
        if ss_ptr:
            self.alloc(4)
            self.load_xstring_array(True, ss_count)
            root["script_strings"] = self._cap
            self.script_strings = [s.b if isinstance(s, Str) else None for s in self._cap]
        self.pop()
        self.push(VIRTUAL)
        entries = []
        if ptr:
            self.alloc(4)
            buf, where = zone_mod.Reader.read(self, 8 * count, ("u32", 2 * count))
            for i in range(count):
                t, p = struct.unpack_from(self.E + "II", buf, 8 * i)
                entries.append((t, p))
                e = AssetEntry([self.plat.types[t], None])
                root["assets"].append(e)
                self.locs.append((where[0], where[1] + 8 * i, 8, e, 8))
            for i, (t, p) in enumerate(entries):
                name = self.plat.structs.get(t)
                if name is None:
                    raise ZoneError("asset %d has unsupported type %d at %d" % (i, t, self.pos))
                info = self.s.assets[name]
                self.cur_asset = (i, t)
                self._cap = NONE
                self.load_asset_ptr(info, buf, 8 * i + 4)
                root["assets"][i][1] = None if self._cap is NONE else self._cap
        self.pop()
        self.entries = entries
        self._resolve()
        self.root = root
        return self

    def _resolve(self):
        """Point every alias at the object it refers into."""
        by_block = {}
        for b, off, n, obj, t in self.locs:
            if n:
                by_block.setdefault(b, []).append((off, n, obj, t))
        index = {}
        for b, rows in by_block.items():
            rows.sort(key=lambda r: r[0])
            index[b] = ([r[0] for r in rows], rows)
        for r in self.refs:
            raw = r.val - 1
            b, off = raw >> BLOCK_SHIFT, raw & OFFSET_MASK
            if b not in index:
                continue
            starts, rows = index[b]
            i = bisect.bisect_right(starts, off) - 1
            # Several things can start at one place (an asset list entry and the asset it
            # points at share nothing, but a struct and its first member's array can): take
            # the last one that contains the offset.
            while i >= 0:
                s, n, obj, t = rows[i]
                if s <= off < s + n:
                    r.target, r.rel, r.t = obj, off - s, t
                    break
                if s + n <= off and i > 0 and rows[i - 1][0] + rows[i - 1][1] <= s:
                    break
                i -= 1


def read_tree(zone, sch, lenient=False):
    r = TreeReader(zone, sch, lenient)
    r.walk()
    if r.pos != len(zone):
        raise ZoneError("walk stopped at %d of %d bytes" % (r.pos, len(zone)))
    return r.root, r


# ================================================================ writing

class TreeWriter(zone_mod.Reader):
    def __init__(self, root, sch, keep_fixes=True):
        plat = zone_mod.PLATFORMS[sch.platform]
        super().__init__(bytes(plat.header_bytes + 16), sch)
        self.root = root
        self.codec = codec_mod.Codec(self.s)
        self.zone = bytearray(plat.header_bytes)
        self.starts = {}     # id(asset dict) -> where it was written
        self.pos = plat.header_bytes
        self.keep_fixes = keep_fixes
        self.reg = {}
        self.loc = {}          # id(object) -> (block, offset)
        self._obj = NONE
        self._last = None
        self._slot = None
        self._ptr_cache = {}
        self.map_rel = None    # optional f(ref) -> new byte offset inside the target (port.py)

    # ---------------------------------------------------------------- values

    def _has_ptrs(self, t):
        if not isinstance(t, Compound):
            return False
        r = self._ptr_cache.get(t.name)
        if r is None:
            self._ptr_cache[t.name] = False
            r = any(PTR in m.mods or self._has_ptrs(m.type) for m in t.members)
            self._ptr_cache[t.name] = r
        return r

    def ref_val(self, r, strict=False):
        """Where an alias's target now is. Targets written later than the pointer itself (a
        struct's pointers are streamed before what they point at) get a placeholder here and
        the real value in alias(), when the load reaches that pointer."""
        if r.target is None:
            raise ZoneError("a pointer (0x%08x) points at something that isn't in the file" % r.val)
        at = self.loc.get(id(r.target))
        if at is None:
            if strict:
                raise ZoneError("a pointer refers to %r before it is written" % (r.target,))
            return r.val
        rel = self.map_rel(r) if self.map_rel else r.rel
        return ((at[0] << BLOCK_SHIFT) | (at[1] + rel)) + 1

    def _forward(self, r, asset=False):
        """What to write in place of alias r when its target isn't written yet (it comes later
        in this file's order, e.g. after copying stock assets in): the target itself, else
        None."""
        if not isinstance(r, Ref) or r.target is None:
            return None
        if asset:
            a = slot_asset(r)
            if a is not None:
                if isinstance(a, Ref):
                    return self._forward(a, True) if id(a.target) not in self.loc else None
                # A temp asset with its own slot is written where it sits, unless nothing
                # holds it any more but pointers ("_forward": its first pointer takes it).
                return a if id(a) not in self.loc and ("_slot" not in a or a.get("_forward")) else None
        if id(r.target) in self.loc or r.rel:
            return None
        return r.target

    def _patch(self, buf, loc, val):
        struct.pack_into(self.E + "I", buf, loc, val)
        zp = zone_mod.zpos_of(buf, loc)
        if zp is not None:
            struct.pack_into(self.E + "I", self.zone, zp, val)

    def reuse(self, inst, mi, mod_pos, combined, kind, loc, val):
        if isinstance(self._obj, Ref) and not self.is_asset(mi) and not mi.is_string:
            t = self._forward(self._obj)
            if t is not None:
                self._patch(inst.buf, loc, FOLLOWING)
                self._obj = t
                return self.alloc_member(inst, mi, mod_pos, combined, kind, loc, FOLLOWING)
        return super().reuse(inst, mi, mod_pos, combined, kind, loc, val)

    def alias(self, buf, loc, val):
        r = self._obj
        if isinstance(r, Ref):
            val = self.ref_val(r, strict=True)
            zp = zone_mod.zpos_of(buf, loc)
            if zp is not None:
                struct.pack_into(self.E + "I", self.zone, zp, val)
        super().alias(buf, loc, val)

    def _fixed(self, t, d):
        """d with every alias pointer set to where its target now is."""
        if not isinstance(d, dict) or not self._has_ptrs(t):
            return d
        ch = d.get("@")
        if t.kind == "union":
            um = d.get("_um")
            if not ch and not um:
                return d
            b = bytearray.fromhex(d["union"])
            for m in t.members:
                k = mkey(t, m)
                if um and k in um:
                    self.codec._encode_member(m, self._fixed_arr(m.type, d[k]) if isinstance(m.type, Compound)
                                              else d[k], b, 0)
                c = ch.get((k, ())) if ch else None
                if isinstance(c, Ref) and m.mods and m.mods[0] == PTR:
                    struct.pack_into(self.E + "I", b, m.offset, self.ref_val(c))
            return dict(d, union=b.hex())
        out = None
        for i, m in enumerate(t.members):
            name = m.name or "_%d" % i
            if name not in d:
                continue
            v = d[name]
            nv = v
            if PTR in m.mods:
                if not ch:
                    continue
                if m.mods[0] == PTR:
                    c = ch.get((name, ()))
                    if isinstance(c, Ref):
                        nv = "0x%08x" % self.ref_val(c)
                else:
                    pl = ch.get((name, ()))
                    nv = list(v)
                    for k in range(len(nv)):
                        c = ch.get((name, (k,)))
                        if c is None and isinstance(pl, list) and k < len(pl):
                            c = pl[k]
                        if isinstance(c, Ref):
                            nv[k] = "0x%08x" % self.ref_val(c)
                    if nv == v:
                        nv = v
            elif isinstance(m.type, Compound) and self._has_ptrs(m.type):
                nv = self._fixed_arr(m.type, v)
            if nv is not v:
                if out is None:
                    out = dict(d)
                out[name] = nv
        return d if out is None else out

    def _fixed_arr(self, t, v):
        if isinstance(v, list):
            nv = [self._fixed_arr(t, x) for x in v]
            return v if all(a is b for a, b in zip(nv, v)) else nv
        return self._fixed(t, v)

    def _enc_struct(self, t, d, out, off, size):
        try:
            self.codec._encode_one(t, self._fixed(t, d), out, off, size)
        except (KeyError, TypeError, IndexError, ValueError, struct.error) as e:
            raise ZoneError("can't write %s at %s: %s %s" % (t.name, " > ".join(self.path), type(e).__name__, e))
        if self.keep_fixes and isinstance(d, dict):
            for o, h in d.get("_fix", ()):
                b = bytes.fromhex(h)
                out[off + o:off + o + len(b)] = b

    def _ptr_raw(self, pl, i):
        c = pl[i]
        if isinstance(c, Ref):
            return self.ref_val(c)
        if pl.raw is not None and i < len(pl.raw):
            return codec_mod.ptr_raw(pl.raw[i])
        return 0 if c is None else FOLLOWING

    def _encode(self, obj, n, desc):
        kind = desc[0]
        if obj is NONE:
            raise ZoneError("nothing to write for %s at %s" % (desc, " > ".join(self.path)))
        if kind == "string":
            return obj.b + b"\0"
        if kind == "ptrs":
            return struct.pack(self.E + "%dI" % len(obj), *[self._ptr_raw(obj, i) for i in range(len(obj))])
        if kind == "u32":
            vals = []
            for e in obj:
                a = e[1]
                if isinstance(a, Ref):
                    p = self.ref_val(a)
                elif a is None:
                    p = 0
                elif "_slot" in a:
                    p = INSERT
                else:
                    p = FOLLOWING
                vals += [self.plat.types.index(e[0]), p]
            return struct.pack(self.E + "%dI" % len(vals), *vals)
        t = desc[1]
        if isinstance(obj, Leaf):
            if obj.E != self.E and obj.t.size > 1:
                raise ZoneError("%s data is still in the other platform's byte order" % obj.t.name)
            return obj.raw
        out = bytearray(n)
        if kind == "partial":
            self._enc_struct(t, obj, out, 0, n)
            return bytes(out)
        count = desc[2]
        items = [obj] if (count == 1 and not isinstance(obj, list)) else obj
        if isinstance(items, Tail) and len(items) > count:
            items = items[:count]
        if len(items) != count:
            raise ZoneError("%s: %d items to write, the file says %d (at %s)"
                            % (t.name, len(items), count, " > ".join(self.path)))
        for i, v in enumerate(items):
            if isinstance(t, Compound) and t.kind not in ("typedef",):
                self._enc_struct(t, v, out, i * t.size, t.size)
            else:
                self.codec._encode_one(t, v, out, i * t.size)
        return bytes(out)

    def read(self, n, desc=("raw",)):
        obj, self._obj = self._obj, NONE
        if self.block == RUNTIME:
            data = ZB(bytes(n))
            zp = None
        else:
            raw = self._encode(obj, n, desc)
            if desc[0] != "string" and len(raw) != n:
                raise ZoneError("%s: wrote %d bytes, expected %d (at %s)" % (desc[0], len(raw), n, " > ".join(self.path)))
            n = len(raw)
            zp = self.pos
            data = ZBW(raw)
            data.zpos = zp
            self.zone += raw
            if self.records is not None and n:
                self.records.append((self.pos, n, desc, self.cur_asset[0] if self.cur_asset else -1,
                                     " > ".join(self.path[1:])))
            self.pos += n
        where = (self.block, self.block_pos[self.block])
        if obj is not NONE and obj is not None and id(obj) not in self.loc:
            self.loc[id(obj)] = where
        if n:
            self.spans.append((self.block, where[1], n, self.cur_asset[0] if self.cur_asset else -1, zp))
        self.block_pos[self.block] += n
        if self.block_pos[self.block] > self.block_max[self.block]:
            self.block_max[self.block] = self.block_pos[self.block]
        self._last = obj
        return data, where

    def on_inst(self, inst, at_start):
        if id(inst) in self.reg:
            return
        if at_start:
            d = self._last
        else:
            pinst, mi, idx, kind = self.ctx[-1]
            d = nav(member_value(self.reg[id(pinst)][1], pinst.info.ctype, mi.m, self.codec), idx)
        self.reg[id(inst)] = (inst, d)

    def load_array(self, info, at_start, count, buf=None, base=0):
        size = info.ctype.size
        where = None
        if at_start:
            lst = self._obj
            buf, where = self.read(size * count, ("type", info.ctype, count))
            base = 0
        else:
            pinst, mi, idx, kind = self.ctx[-1]
            lst = nav(member_value(self.reg[id(pinst)][1], pinst.info.ctype, mi.m, self.codec), idx)
        for i in range(count):
            e = Inst(info, buf, base + i * size, i, where)
            self.reg[id(e)] = (e, lst[i])
            self.vars[info.name] = e
            self.load_struct(info, e, False)
        return buf

    def load_block(self, inst, mi, mod_pos, combined, kind, loc):
        d = self.reg[id(inst)][1]
        idx = self.ctx[-1][2]
        k = mkey(inst.info.ctype, mi.m)
        if kind in ("embedded", "embarray", "dynamic"):
            obj = nav(d[k], idx) if k in d else NONE
        else:
            obj = d.get("@", {}).get((k, idx), NONE)
        self._obj = obj
        super().load_block(inst, mi, mod_pos, combined, kind, loc)
        self._obj = NONE

    def load_ptr_array(self, mi, at_start, count, buf=None, loc=0):
        pl = self._obj
        if at_start:
            buf, _ = self.read(4 * count, ("ptrs", count))
            loc = 0
        t = mi.type
        for i in range(count):
            val = struct.unpack_from(self.E + "I", buf, loc + 4 * i)[0]
            if not val:
                continue
            c = pl[i]
            if t is not None and t.asset:
                self._obj = c
                self.load_asset_ptr(t, buf, loc + 4 * i)
                continue
            if mi.is_reusable and val != FOLLOWING and self._forward(c) is not None:
                c = self._forward(c)
                val = FOLLOWING
                self._patch(buf, loc + 4 * i, val)
            if mi.is_reusable and val != FOLLOWING:
                self._obj = c
                self.alias(buf, loc + 4 * i, val)
                continue
            align = self.align_of(t) if t is not None else mi.m.type.align
            self.alloc(align)
            self._obj = c
            if t is not None and not t.is_leaf:
                self.load_struct(t, None, True)
            else:
                ct = t.ctype if t is not None else mi.m.type
                self.read(ct.size, ("type", ct, 1))
        self._obj = NONE

    def load_xstring(self, buf, loc):
        obj, self._obj = self._obj, NONE
        val = struct.unpack_from(self.E + "I", buf, loc)[0]
        if not val:
            return None
        if val != FOLLOWING and self._forward(obj) is not None:
            obj = self._forward(obj)
            val = FOLLOWING
            self._patch(buf, loc, val)
        if val != FOLLOWING:
            self._obj = obj
            self.alias(buf, loc, val)
            self._obj = NONE
            return None
        self._obj = obj
        data, _ = self.read(len(obj.b) + 1, ("string",))
        return data[:-1]

    def load_xstring_array(self, at_start, count, buf=None, loc=0):
        pl = self._obj
        if at_start:
            buf, _ = self.read(4 * count, ("ptrs", count))
            loc = 0
        for i in range(count):
            self._obj = pl[i]
            self.load_xstring(buf, loc + 4 * i)
        self._obj = NONE

    def insert_pointer(self):
        self.push(VIRTUAL)
        self.alloc(4)
        self._slot = self.block_pos[VIRTUAL]
        self.pop()
        super().insert_pointer()
        # A struct member's slot (XModelLodInfo.modelSurfs); an asset's is registered in
        # load_asset_ptr.
        o = self._obj
        if isinstance(o, dict) and "_slot" in o and id(o["_slot"]) not in self.loc:
            self.loc[id(o["_slot"])] = (VIRTUAL, self._slot)

    def load_asset_ptr(self, info, buf, loc):
        d, self._obj = self._obj, NONE
        val = struct.unpack_from(self.E + "I", buf, loc)[0]
        in_temp = info.block is not None and info.block.kind == "temp"
        if val and val not in (FOLLOWING, INSERT) and self._forward(d, asset=True) is not None:
            d = self._forward(d, asset=True)
            val = INSERT if in_temp and "_slot" in d else FOLLOWING
            self._patch(buf, loc, val)
        if in_temp:
            self.push(TEMP)
        inst = None
        if val:
            if val == FOLLOWING or (in_temp and val == INSERT):
                self.alloc(self.align_of(info))
                if in_temp and val == INSERT:
                    self.insert_pointer()
                    if "_slot" in d:
                        self.loc[id(d["_slot"])] = (VIRTUAL, self._slot)
                start = self.pos
                self.starts[id(d)] = start
                self._obj = d
                inst = self.load_struct(info, None, True)
                self.assets.append((info, inst, start, self.pos))
            else:
                self._obj = d
                self.alias(buf, loc, val)
                self._obj = NONE
        if in_temp:
            self.pop()
        return inst

    # ---------------------------------------------------------------- zone

    def write(self, header=None):
        """Walk the tree and return the finished zone bytes. header: the original header, to
        keep its second word (PC: size of external data)."""
        root = self.root
        ss = root.get("script_strings")
        assets = root["assets"]
        self.zone += struct.pack(self.E + "4I", len(ss or ()), FOLLOWING if ss else 0,
                                 len(assets), FOLLOWING if assets else 0)
        self.pos += 16
        self.push(VIRTUAL)
        if ss:
            self.alloc(4)
            self._obj = ss
            self.load_xstring_array(True, len(ss))
        self.pop()
        self.push(VIRTUAL)
        if assets:
            self.alloc(4)
            self._obj = assets
            buf, where = self.read(8 * len(assets), ("u32", 2 * len(assets)))
            for i, e in enumerate(assets):
                self.loc[id(e)] = (where[0], where[1] + 8 * i)
            for i, e in enumerate(assets):
                t = self.plat.types.index(e[0])
                info = self.s.assets[self.plat.structs[t]]
                self.cur_asset = (i, t)
                self._obj = e[1]
                self.load_asset_ptr(info, buf, 8 * i + 4)
        self.pop()
        nb = self.plat.blocks
        hdr = [len(self.zone) - self.plat.header_bytes, 0]
        if header is not None:
            hdr[1] = struct.unpack_from(self.E + "I", header, 4)[0]
        for b in range(nb):
            hdr.append(self.block_max[b] if b == TEMP else self.block_pos[b])
        if header is not None:
            # The temp and callback sizes in the header are allowances the walk alone doesn't
            # show (loader scratch space, data set up by callbacks): keep the original's when
            # they are bigger.
            src_nb = (len(header) - 8) // 4
            src_E = self.E if src_nb == nb else ("<" if self.E == ">" else ">")
            for b in (TEMP, CALLBACK):
                hdr[2 + b] = max(hdr[2 + b], struct.unpack_from(src_E + "I", header, 8 + 4 * b)[0])
        struct.pack_into(self.E + "%dI" % (2 + nb), self.zone, 0, *hdr)
        return bytes(self.zone)


def write_tree(root, sch, keep_fixes=True, header=None):
    w = TreeWriter(root, sch, keep_fixes)
    return w.write(header), w
