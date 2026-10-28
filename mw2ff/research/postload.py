"""Run what the game does to a map once its file is loaded, in the emulator, and check it.

    python postload.py MAP.ff [--resident code_post_gfx_mp.ff common_mp.ff]

The zone loader (emu.py, run1.py) only reads a file. When a map loads, the game then sorts and
rebuilds parts of it (engine notes in the skill): it sorts every loaded material (R_SortMaterials,
0x824069f8, the material compare 0x82406418 that crashed on a lit model with an unlit sort key),
then sets up the world (0x82405198): sorts the world's surfaces (0x824047c8), reorders the placed
models (0x82404cc8) and renumbers only each room's root model list, and rebuilds every light's
shadow lists (0x82404268) without checking their length. Each of these broke converted maps on
the console. This runs the game's own code for them on the loaded files (with 64-bit registers,
wide.py) and reports:

  - any bad memory access or CPU fault in them (a crash on the console);
  - draw ranges (lit opaque, lit see-through, shadow casters, emissive) that cover other surfaces
    after the sort than in the file (the game reads them as surface numbers after sorting);
  - model lists in the culling trees and shadow lists that name other models after the reorder
    (models that vanish or draw in the wrong place);
  - shadow lists the game fills past the length the file gave them (memory written over);
  - effects, sounds, ... the file looks up by a name it has no data for yet (an empty name: a
    pointer to data not loaded yet), and named references (",name") nothing loaded has.

The always-loaded files (--resident, in the order the game loads them) give the materials and
other assets the map names but doesn't carry; without them those are reported as missing and
their materials sort as empty ones.
"""
import os
import struct
import sys
import time
from collections import Counter

import tu6  # noqa: F401  (sets the paths)
import mw2tex
import mw2ff
import zone as Z
import run1
import wide
from emu import Emu, STACK, RET, PE, UC_PPC_REG_LR, UcError
from unicorn.ppc_const import UC_PPC_REG_MSR

SCH = mw2ff.schema_for("xbox")
T = SCH.defs.types
SIZES, NAMEOFF = {}, {}
for _t, _n in Z.ASSET_STRUCTS.items():
    _c = T[SCH.assets[_n].name]
    SIZES[_t] = _c.size
    _offs = {m.name: m.offset for m in _c.members}
    for _k in ("name", "aliasName", "info", "filename"):
        if _k in _offs:
            NAMEOFF[_t] = _offs[_k]
            break

MATERIAL, TECHSET, GFX_WORLD = 5, 7, 19
STAND_IN_TYPES = (MATERIAL, TECHSET)
MAT_POOL = 0x82922598           # the game's material pool (Material_GetIndex 0x821df7c8), 0x58 each
MAT_POOL_SIZE = 4096
RGP = 0x83B3A980
WORLD_PTR = RGP + 0x2108        # the loaded GfxWorld
SORTED_MATERIALS, SORTED_COUNT = RGP, RGP + 0x2084
POOL = 0x5C000000               # copies of the other asset headers (the game's other pools)
POOL_SIZE = 0x3000000
R_SORT_MATERIALS, R_SETUP_WORLD = 0x824069F8, 0x82405198
DB_ENUM, DB_ADD, DB_FIND = 0x822FDDB0, 0x821DE528, 0x821E25B0
MEMSET, MEMCPY = 0x8241A890, 0x8241A9F0
NO_FOLLOW = {0x8233D008}      # error reporting (Com_Error): everything is reachable from it
ENUM_MATERIAL_CB = 0x82406718


def off(t, path):
    """Byte offset of a member path ("dpvs.surfaces") in struct t."""
    o = 0
    for name in path.split("."):
        m = next(x for x in T[t].members if x.name == name)
        o += m.offset
        t = getattr(m.type, "name", None)
    return o


class Loader(Emu):
    """The emulator with the game's asset tables filled in as DB_AddXAsset would (asset headers
    copied out of the temporary block into pools, named references pointed at what they name),
    and several zones loaded one after another."""

    def __init__(self, zone):
        self.byname = {}            # (type, lower name) -> header
        self.assets = []            # (type, name, header, zone index)
        self.materials = []         # material pool slots in use, in order: [header, active]
        self.missing = []           # (type, name) named references nothing loaded has
        self.empty_finds = []       # (type, zone pos) look-ups of an empty name
        self.finds = []
        self.enum_unknown = []
        self.zone_index = 0
        self.wide = None
        super().__init__(zone, {0x82281FA0: 0, 0x8233D008: run1.pr, DB_ADD: self._add,
                                0x822A2E18: run1.slget, DB_FIND: self._find, DB_ENUM: self._enum,
                                MEMSET: self._memset, MEMCPY: self._memcpy}, trace=False)
        self.u.mem_map(POOL, POOL_SIZE)
        self.u.reg_write(UC_PPC_REG_MSR, self.u.reg_read(UC_PPC_REG_MSR) | 0x2000)    # floating point on
        self.pool_next = POOL
        self.heap_next = self.zm + 0x20000
        self.err = None

    def _ret_to_lr(self):
        if self.wide is not None:
            self.wide.hi[3] = 0
        super()._ret_to_lr()

    # ---------------------------------------------------------- zones
    def load(self, zone=None):
        """Load a zone (the first one: the one the emulator was made with). Returns an error or None."""
        u = self.u
        if zone is not None:
            self.zone, self.pos = zone, 32
            sizes = struct.unpack_from(">8I", zone, 0)[2:]
            self.blocksizes, self.blocks = sizes, []
            a = self.heap_next
            for sz in sizes:
                sz2 = (sz + 0xFFFF) & ~0xFFFF or 0x10000
                if a + sz2 + 0x20000 > POOL:
                    return "the zones don't fit in the emulator's memory"
                u.mem_map(a, sz2 + 0x10000)
                self.blocks.append(a)
                a += sz2 + 0x20000
            self.zm = a
            u.mem_map(a, 0x10000)
            for i, b in enumerate(self.blocks):
                u.mem_write(a + i * 8, struct.pack(">II", b, sizes[i]))
            self.heap_next = a + 0x20000
        self.err = None
        self.call(0x821E2C70, self.zm)
        self.setreg(30, 0x825CE4C0)
        self.setreg(21, self.zm)
        self.setreg(23, 1)
        self.setreg(25, 0x82440000)
        self.setreg(1, STACK - 0x1000)
        self.setreg(13, 0x7E008000)
        u.reg_write(UC_PPC_REG_LR, RET)
        try:
            u.emu_start(0x821B0024, 0x821B00CC)
        except UcError as ex:
            self.err = self.err or "%s at %s" % (ex, self.bt())
        if not self.err and self.pos != len(self.zone):
            self.err = "the loader stopped at %d of %d" % (self.pos, len(self.zone))
        self.zone_index += 1
        return self.err

    # ---------------------------------------------------------- the asset tables
    def cstr(self, a):
        try:
            return bytes(self.u.mem_read(a, 256)).split(b"\0")[0].decode("latin-1")
        except UcError:
            return None

    def word(self, a):
        return struct.unpack(">I", bytes(self.u.mem_read(a, 4)))[0]

    def name_of(self, t, hdr):
        if t not in NAMEOFF:
            return None
        p = self.word(hdr + NAMEOFF[t])
        return self.cstr(p) if p else None

    def _copy(self, t, h):
        sz = SIZES.get(t, 16)
        if t == MATERIAL and len(self.materials) < MAT_POOL_SIZE:
            a = MAT_POOL + len(self.materials) * 0x58
            self.materials.append([a, True])
        else:
            a = self.pool_next
            self.pool_next += (sz + 15) & ~15
        self.u.mem_write(a, bytes(self.u.mem_read(h, sz)))
        return a

    def _stand_in(self, t, h):
        """What a named reference nothing loaded has points at here: for a material or shader
        set (which the sorts look inside), the first full one loaded, else a copy of the name."""
        if t in STAND_IN_TYPES:
            for t2, n, hdr, zi in self.assets:
                if t2 == t and n and not n.startswith(","):
                    return hdr
        a = self.pool_next
        sz = SIZES.get(t, 16)
        self.pool_next += (sz + 15) & ~15
        self.u.mem_write(a, bytes(self.u.mem_read(h, sz)))
        return a

    def _add(self, e):
        t = e.reg(3)
        h = self.word(e.reg(4))
        n = self.name_of(t, h)
        if n and n.startswith(","):
            k = (t, n[1:].lower())
            hdr = self.byname.get(k)
            if hdr is None:
                self.missing.append((t, n[1:]))
                hdr = self._stand_in(t, h)
        else:
            hdr = self._copy(t, h)
            if n:
                k = (t, n.lower())
                old = self.byname.get(k)
                if old is not None and t == MATERIAL:
                    for m in self.materials:
                        if m[0] == old:
                            m[1] = False        # the new copy takes the name over
                self.byname[k] = hdr
        self.assets.append((t, n, hdr, self.zone_index))
        ent = self.pool_next
        self.pool_next += 16
        self.u.mem_write(ent, struct.pack(">II", t, hdr))
        return ent

    def _find(self, e):
        t = e.reg(3)
        n = self.cstr(e.reg(4)) or ""
        self.finds.append((t, n))
        if not n:
            self.empty_finds.append((t, self.pos))
        hdr = self.byname.get((t, n.lower()))
        if hdr is None:
            if n:
                self.missing.append((t, n))
            hdr = 0x7E004000
        return hdr

    def _enum(self, e):
        t, fn = e.reg(3), e.reg(4)
        if t == MATERIAL and fn == ENUM_MATERIAL_CB:
            n = self.word(SORTED_COUNT)
            for a, active in self.materials:
                if active:
                    self.u.mem_write(SORTED_MATERIALS + 2 * n, struct.pack(">H", (a - MAT_POOL) // 0x58))
                    n += 1
            self.u.mem_write(SORTED_COUNT, struct.pack(">I", n))
        else:
            self.enum_unknown.append((t, fn))
        return 0

    def _memset(self, e):
        d, c, n = e.reg(3), e.reg(4) & 0xFF, e.reg(5)
        self.u.mem_write(d, bytes([c]) * n)
        return d

    def _memcpy(self, e):
        d, s, n = e.reg(3), e.reg(4), e.reg(5)
        self.u.mem_write(d, bytes(self.u.mem_read(s, n)))
        return d

    def run(self, fn):
        """Call fn (no arguments) with 64-bit registers; returns an error or None."""
        self.err = None
        self.wide = wide.Wide(self, PE, entries=[fn], stop=set(self.stubs) | NO_FOLLOW)
        try:
            self.wide.call(fn)
        finally:
            self.count = self.wide.count
            self.wide = None
        return self.err


# ------------------------------------------------------------ what the world holds

class World:
    """The parts of the loaded GfxWorld that the load-time steps move, read from memory."""

    def __init__(self, ld, w):
        self.ld, self.w = ld, w
        rd = ld.u.mem_read
        word = ld.word
        dp = w + off("GfxWorld", "dpvs")
        D = lambda name: dp + off("GfxWorldDpvsStatic", name)       # noqa: E731
        self.surf_count = word(D("staticSurfaceCount"))
        self.smodel_count = word(D("smodelCount"))
        self.ranges = {n: (word(D(n + "SurfsBegin")), word(D(n + "SurfsEnd")))
                       for n in ("litOpaque", "litTrans", "shadowCaster", "emissive")}
        sp = word(D("surfaces"))
        raw = bytes(rd(sp, 24 * self.surf_count)) if self.surf_count else b""
        self.surfaces = [raw[24 * i:24 * i + 24] for i in range(self.surf_count)]
        self.surf_material = [struct.unpack_from(">I", s, 0x10)[0] for s in self.surfaces]
        dq = word(D("smodelDrawInsts"))
        raw = bytes(rd(dq, 44 * self.smodel_count)) if self.smodel_count else b""
        self.smodels = [raw[44 * i:44 * i + 0x20] for i in range(self.smodel_count)]
        # culling trees: (room, node) -> model numbers listed
        self.lists = {}
        cells = word(w + off("GfxWorld", "dpvsPlanes.cellCount"))
        counts, trees = word(w + off("GfxWorld", "aabbTreeCounts")), word(w + off("GfxWorld", "aabbTrees"))
        for c in range(cells if counts and trees else 0):
            n = word(counts + 4 * c)
            base = word(trees + 4 * c)
            for i in range(n):
                node = bytes(rd(base + 40 * i, 40))
                cnt, ptr = struct.unpack_from(">H", node, 0x1E)[0], struct.unpack_from(">I", node, 0x20)[0]
                if cnt and ptr:
                    self.lists[("room %d node %d" % (c, i))] = list(struct.unpack(">%dH" % cnt, bytes(rd(ptr, 2 * cnt))))
        # shadow lists per light
        self.shadow = []
        lights = word(w + off("GfxWorld", "primaryLightCount"))
        sg = word(w + off("GfxWorld", "shadowGeom"))
        for i in range(lights if sg else 0):
            sc, mc, sp_, mp = struct.unpack(">HHII", bytes(rd(sg + 12 * i, 12)))
            ms = list(struct.unpack(">%dH" % mc, bytes(rd(mp, 2 * mc)))) if mc and mp else []
            self.shadow.append((sc, mc, ms))


def compare(before, after):
    """[(check, ok, message)] of what the load-time steps did to the world."""
    out = []
    for name, (b, e) in before.ranges.items():
        if e <= b:
            continue
        was = Counter(before.surfaces[b:e])
        now = Counter(after.surfaces[b:e])
        other = sum((now - was).values())
        out.append(("draw range " + name, not other,
                    "surfaces %d-%d: %d of %d are other surfaces once the game has sorted them" % (b, e, other, e - b)
                    if other else "surfaces %d-%d hold the same surfaces after the sort" % (b, e)))
    moved = sum(1 for x, y in zip(before.surfaces, after.surfaces) if x != y)
    out.append(("surface sort", True, "%d of %d surfaces moved" % (moved, before.surf_count)))
    stale = bad_lists = 0
    examples = []
    for k, idx in before.lists.items():
        now = after.lists.get(k, [])
        wrong = sum(1 for i, j in zip(idx, now)
                    if i >= len(before.smodels) or j >= len(after.smodels) or before.smodels[i] != after.smodels[j])
        if wrong:
            stale += wrong
            bad_lists += 1
            if len(examples) < 3:
                examples.append(k)
    out.append(("model lists", not stale,
                "%d model numbers in %d culling tree lists name other models after the game reorders them (%s)"
                % (stale, bad_lists, ", ".join(examples)) if stale else
                "every culling tree list names the same models after the reorder (%d lists)" % len(before.lists)))
    wrong = 0
    for (sc0, mc0, ms0), (sc1, mc1, ms1) in zip(before.shadow, after.shadow):
        wrong += sum(1 for i, j in zip(ms0, ms1)
                     if i >= len(before.smodels) or j >= len(after.smodels) or before.smodels[i] != after.smodels[j])
    out.append(("shadow models", not wrong, "%d shadow list model numbers name other models after the reorder" % wrong
                if wrong else "shadow lists name the same models after the reorder"))
    over = [(i, sc0, sc1) for i, ((sc0, _, _), (sc1, _, _)) in enumerate(zip(before.shadow, after.shadow)) if sc1 > sc0]
    out.append(("shadow lists", not over,
                "%d lights: the game fills their surface lists past the file's length, writing over what follows "
                "(%s)" % (len(over), ", ".join("light %d: %d in the file, %d filled" % o for o in over[:4]))
                if over else "every light's surface list fits what the game puts in it (%d lights)" % len(before.shadow)))
    return out


def run(map_path, resident=(), log=print):
    """Load the resident files and the map, run the load-time steps; returns
    {"ok": bool, "checks": [(name, ok, message)], ...}."""
    t0 = time.time()
    checks = []
    zones = [mw2tex.FastFile(p).zone for p in list(resident) + [map_path]]
    run1.SS.clear()
    run1.MSGS.clear()
    ld = Loader(zones[0])
    for i, z in enumerate(zones):
        err = ld.load(None if i == 0 else z)
        name = os.path.basename((list(resident) + [map_path])[i])
        if err:
            checks.append(("load " + name, False, err))
            return {"ok": False, "checks": checks}
    map_zone = len(zones) - 1
    names = Counter(t for t, n in ld.missing)
    if ld.missing and not resident:
        checks.append(("named assets", True, "%d named assets the always-loaded files would give (not given: "
                       "materials and shader sets among them stand in as the map's first)" % len(ld.missing)))
    else:
        checks.append(("named assets", not ld.missing,
                       ("%d named assets nothing loaded has (%s), e.g. %s" % (
                           len(ld.missing), ", ".join("%s %d" % (Z.ASSET_TYPES[t], n) for t, n in names.most_common()),
                           ", ".join("%s %s" % (Z.ASSET_TYPES[t], n) for t, n in ld.missing[:4])))
                       if ld.missing else "every named asset found"))
    if ld.empty_finds:
        checks.append(("empty names", False, "%d look-ups of an empty name (%s): pointers to names not loaded yet"
                       % (len(ld.empty_finds), ", ".join(sorted(set(Z.ASSET_TYPES[t] for t, p in ld.empty_finds))))))
    worlds = [h for t, n, h, zi in ld.assets if t == GFX_WORLD and zi == map_zone]
    err = ld.run(R_SORT_MATERIALS)
    checks.append(("material sort", not err, err or "%d materials sorted" % ld.word(SORTED_COUNT)))
    if err or not worlds:
        return {"ok": all(c[1] for c in checks), "checks": checks}
    w = worlds[0]
    ld.u.mem_write(WORLD_PTR, struct.pack(">I", w))
    before = World(ld, w)
    err = ld.run(R_SETUP_WORLD)
    checks.append(("world setup", not err, err or "ran (%d instructions)" % ld.count))
    if not err:
        checks += compare(before, World(ld, w))
    if ld.enum_unknown:
        checks.append(("emulator", False, "asset enumerations it can't stand in for: %s" % ld.enum_unknown[:4]))
    return {"ok": all(c[1] for c in checks), "checks": checks, "seconds": time.time() - t0}


def main():
    args = sys.argv[1:]
    resident = []
    if "--resident" in args:
        i = args.index("--resident")
        resident = args[i + 1:]
        args = args[:i]
    r = run(args[0], resident)
    for name, ok, msg in r["checks"]:
        print("  %-24s %s  %s" % (name, "ok  " if ok else "FAIL", msg))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
