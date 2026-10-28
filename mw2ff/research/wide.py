"""64-bit registers for the 32-bit emulator (emu.Emu), for game code that uses them.

The 360's CPU has 64-bit registers and the game uses them for packed 64-bit fields (material
sort keys, draw surfaces: ld, rldicl, rldimi, std). unicorn's 64-bit PowerPC can't load or store
64 bits, so the emulator runs a 32-bit CPU (registers hold the low words) and this keeps the high
word of each register beside it: every instruction in the image is looked at before it runs;
the ones whose result depends on, or sets, the high words (64-bit loads, stores, rotates, shifts,
compares, multiplies and divides; add, subtract and logic of registers whose high word may matter)
are run here in full and skipped, the rest run on the CPU and the high word of what they write is
set the way the 64-bit CPU would (zero after a 32-bit load, the sign after an arithmetic shift,
...). Only for calls made with Wide installed (Wide.call); slow (every instruction comes through
Python), so meant for the load-time steps run after a zone is loaded, not the zone load itself.
"""
import struct

from unicorn import UC_HOOK_CODE
from unicorn.ppc_const import UC_PPC_REG_0, UC_PPC_REG_CR, UC_PPC_REG_PC, UC_PPC_REG_XER, UC_PPC_REG_FPR0

M64 = (1 << 64) - 1
M32 = 0xFFFFFFFF
B = 0x82000000


def _s64(v):
    return v - (1 << 64) if v >> 63 else v


def _sx32(v):
    return (v | 0xFFFFFFFF00000000) if v & 0x80000000 else v


def _rotl64(v, n):
    n &= 63
    return ((v << n) | (v >> (64 - n))) & M64 if n else v


def _mask(mb, me):
    """PowerPC MASK(mb, me) of 64 bits (bit 0 the highest)."""
    if mb <= me:
        return ((M64 >> mb) & (M64 << (63 - me))) & M64
    return ((M64 >> mb) | (M64 << (63 - me))) & M64


# 32-bit loads (primary opcode) and the high word they leave
_LOADS = {32: "z", 33: "z", 34: "z", 35: "z", 40: "z", 41: "z", 42: "s16", 43: "s16"}
_LOADX = {20: "z", 23: "z", 55: "z", 87: "z", 119: "z", 279: "z", 311: "z", 343: "s16", 375: "s16",
          534: "z", 790: "z", 339: "z", 19: "z", 83: "z", 371: "z"}    # (incl. lwbrx, lhbrx, mf*)


def is64(w):
    """Whether an instruction's result depends on or sets a register's high word (a 64-bit
    load or store not on the stack, a 64-bit rotate, shift, compare, multiply or divide)."""
    op = w >> 26
    if op in (58, 62):
        return ((w >> 16) & 31) != 1        # (saving and restoring registers on the stack: no)
    if op == 30:
        return True
    if op == 31:
        x10, x9 = (w >> 1) & 0x3FF, (w >> 1) & 0x1FF
        if x10 in (21, 53, 149, 181, 341, 373, 27, 539, 794, 58, 986) or x10 >> 1 == 413:
            return True
        if x9 in (233, 73, 9, 489, 457):
            return True
        if x10 in (0, 32) and (w >> 21) & 1:
            return True
    return op in (10, 11) and bool((w >> 21) & 1)


def wide_functions(pe, entries, stop=()):
    """[(start, end)] of the functions reachable by calls from entries (not through stop) that
    have a 64-bit instruction (functions are taken as running from one call target to the next)."""
    import bisect
    from xref import calls, T0, T1
    starts = sorted(t for t, cs in calls.items() if T0 <= t < T1 and any(lk for a, lk in cs))

    def span(f):
        i = bisect.bisect_right(starts, f) - 1
        s = starts[i] if i >= 0 else f
        e = starts[i + 1] if i + 1 < len(starts) else T1
        return s, min(e, s + 0x20000)
    out, seen, todo = [], set(), [span(f) for f in entries]
    while todo:
        s, e = todo.pop()
        if s in seen:
            continue
        seen.add(s)
        ws = struct.unpack_from(">%dI" % ((e - s) // 4), pe, s - B)
        if any(is64(w) for w in ws):
            out.append((s, e))
        for i, w in enumerate(ws):
            if w >> 26 == 18 and w & 1:     # bl
                li = w & 0x3FFFFFC
                li -= 0x4000000 if li & 0x2000000 else 0
                t = (li if w & 2 else s + 4 * i + li) & M32
                if T0 <= t < T1 and t not in stop:
                    todo.append(span(t))
    return sorted(out)


VOLATILE = (0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)


class Wide:
    """entries: run only the functions wide_functions finds from them through Python (much
    faster; the rest of the code runs on the CPU alone: its results' high words are taken as
    zero where a wide function starts and where a call it makes returns). Without entries,
    every instruction in lo..hi."""

    def __init__(self, emu, pe, lo=B, hi=B + 0x1E40000, entries=None, stop=()):
        self.e, self.u, self.pe = emu, emu.u, pe
        self.hi = [0] * 32
        self.fix = None         # [(register, kind, high word)] to set after the CPU ran the instruction
        self.cache = {}
        self.count = 0
        self.unknown = {}
        self.resets = set()     # where volatile registers' high words go back to zero
        if entries:
            self.ranges = wide_functions(pe, entries, stop)
            starts = {s for s, e in self.ranges}
            for s, e in self.ranges:
                self.resets.add(s)
                for a in range(s, e, 4):
                    w = struct.unpack_from(">I", pe, a - B)[0]
                    if w >> 26 == 18 and w & 1:
                        li = w & 0x3FFFFFC
                        li -= 0x4000000 if li & 0x2000000 else 0
                        if ((a + li) & M32) not in starts:
                            self.resets.add(a + 4)
        else:
            self.ranges = [(lo, hi)]

    # registers
    def g(self, r):
        return (self.hi[r] << 32) | (self.u.reg_read(UC_PPC_REG_0 + r) & M32)

    def s(self, r, v):
        v &= M64
        self.hi[r] = v >> 32
        self.u.reg_write(UC_PPC_REG_0 + r, v & M32)

    def ga(self, ra):
        return 0 if ra == 0 else self.g(ra)

    def cr0(self, v):
        v = _s64(v & M64)
        bits = 8 if v < 0 else 4 if v > 0 else 2
        bits |= (self.u.reg_read(UC_PPC_REG_XER) >> 31) & 1
        cr = self.u.reg_read(UC_PPC_REG_CR) & M32
        self.u.reg_write(UC_PPC_REG_CR, (cr & 0x0FFFFFFF) | (bits << 28))

    def crf(self, f, a, b, signed):
        if signed:
            a, b = _s64(a), _s64(b)
        bits = 8 if a < b else 4 if a > b else 2
        bits |= (self.u.reg_read(UC_PPC_REG_XER) >> 31) & 1
        sh = 28 - 4 * f
        cr = self.u.reg_read(UC_PPC_REG_CR) & M32
        self.u.reg_write(UC_PPC_REG_CR, (cr & ~(0xF << sh) & M32) | (bits << sh))

    def ca(self, c):
        x = self.u.reg_read(UC_PPC_REG_XER) & M32
        self.u.reg_write(UC_PPC_REG_XER, (x | 0x20000000) if c else (x & ~0x20000000 & M32))

    def getca(self):
        return (self.u.reg_read(UC_PPC_REG_XER) >> 29) & 1

    # memory
    def r64(self, a):
        return struct.unpack(">Q", bytes(self.u.mem_read(a & M32, 8)))[0]

    def w64(self, a, v):
        self.u.mem_write(a & M32, struct.pack(">Q", v & M64))

    def install(self):
        self.h = [self.u.hook_add(UC_HOOK_CODE, self._hook, begin=s, end=e - 1) for s, e in self.ranges]

    def remove(self):
        for h in self.h:
            self.u.hook_del(h)

    def call(self, fn, *args):
        self.hi = [0] * 32
        self.fix = None
        self.install()
        try:
            return self.e.call(fn, *args)
        finally:
            self.remove()

    def _hook(self, u, addr, size, ud):
        self.count += 1
        if addr in self.resets:
            for r in VOLATILE:
                self.hi[r] = 0
            self.fix = None
        if self.fix is not None:
            for r, k, h in self.fix:
                lo = u.reg_read(UC_PPC_REG_0 + r) & M32
                self.hi[r] = h if k == "v" else 0 if k == "z" else (M32 if lo & 0x8000 else 0)
            self.fix = None
        w = struct.unpack_from(">I", self.pe, addr - B)[0]
        f = self.cache.get(addr)
        if f is None:
            f = self.cache[addr] = self._decode(w)
        if f is not None:
            if f(w) is not False:
                u.reg_write(UC_PPC_REG_PC, addr + 4)

    # ------------------------------------------------------------ decoding
    def _decode(self, w):
        op = w >> 26
        if op in _LOADS:
            return self._load32
        if op in (58, 62):
            return self._ldst64
        if op in (14, 15):
            return self._addi
        if op in (12, 13, 8):
            return self._addic
        if op == 7:
            return self._mulli
        if op in (24, 25, 26, 27, 28, 29):
            return self._logimm
        if op in (10, 11):
            return self._cmpi
        if op in (20, 21, 23):
            return self._rlw
        if op == 30:
            return self._rld
        if op == 31:
            return self._x31
        if op == 63 and ((w >> 1) & 0x3FF) in (814, 815, 846):
            return self._fpint
        return None

    def _load32(self, w):
        self.fix = [((w >> 21) & 31, _LOADS[w >> 26], 0)]
        if (w >> 26) & 1:       # update form: rA becomes the address
            ra = (w >> 16) & 31
            d = w & 0xFFFF
            d -= 0x10000 if d & 0x8000 else 0
            self.fix.append((ra, "v", ((self.g(ra) + d) & M64) >> 32))
        return False

    def _ldst64(self, w):
        op, rt, ra, ds, xo = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFC, w & 3
        ds -= 0x10000 if ds & 0x8000 else 0
        ea = (self.ga(ra) + ds) & M64
        if op == 58:
            if xo == 2:         # lwa
                v = struct.unpack(">i", bytes(self.u.mem_read(ea & M32, 4)))[0]
                self.s(rt, v)
            else:
                self.s(rt, self.r64(ea))
        else:
            self.w64(ea, self.g(rt))
        if xo == 1:
            self.s(ra, ea)

    def _addi(self, w):
        rt, ra, d = (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
        d -= 0x10000 if d & 0x8000 else 0
        if w >> 26 == 15:
            d <<= 16
        self.s(rt, self.ga(ra) + d)

    def _addic(self, w):
        op, rt, ra, d = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
        d -= 0x10000 if d & 0x8000 else 0
        a = self.g(ra)
        if op == 8:     # subfic
            r = (~a & M64) + (d & M64) + 1
        else:
            r = a + (d & M64)
        self.ca(r >> 64)
        self.s(rt, r)
        if op == 13:
            self.cr0(r)

    def _mulli(self, w):
        rt, ra, d = (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
        d -= 0x10000 if d & 0x8000 else 0
        self.s(rt, _s64(self.g(ra)) * d)

    def _logimm(self, w):
        op, rs, ra, ui = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
        v = self.g(rs)
        r = {24: v | ui, 25: v | (ui << 16), 26: v ^ ui, 27: v ^ (ui << 16),
             28: v & ui, 29: v & (ui << 16)}[op]
        self.s(ra, r)
        if op in (28, 29):
            self.cr0(r)

    def _cmpi(self, w):
        bf, l, ra, i = (w >> 23) & 7, (w >> 21) & 1, (w >> 16) & 31, w & 0xFFFF
        a = self.g(ra)
        if w >> 26 == 11:
            i -= 0x10000 if i & 0x8000 else 0
            if l:
                self.crf(bf, a, i & M64, True)
            else:
                self.crf(bf, _sx32(a & M32), i & M64, True)
        else:
            self.crf(bf, a if l else a & M32, i, False)

    def _rlw(self, w):
        op, rs, ra, sh, mb, me, rc = (w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31,
                                      (w >> 6) & 31, (w >> 1) & 31, w & 1)
        if op == 23:
            sh = self.g(sh) & 31
        lo = self.g(rs) & M32
        r32 = ((lo << sh) | (lo >> (32 - sh))) & M32 if sh else lo
        rot = (r32 << 32) | r32
        m = _mask(mb + 32, me + 32)
        r = rot & m
        if op == 20:
            r |= self.g(ra) & ~m & M64
        self.s(ra, r)
        if rc:
            self.cr0(r)

    def _rld(self, w):
        rs, ra, rc = (w >> 21) & 31, (w >> 16) & 31, w & 1
        xo = (w >> 2) & 7
        v = self.g(rs)
        m6 = (w >> 5) & 63
        mbe = ((m6 & 1) << 5) | (m6 >> 1)
        if xo in (0, 1, 2, 3):
            sh = ((w >> 11) & 31) | (((w >> 1) & 1) << 5)
        else:
            sub = (w >> 1) & 15
            if sub not in (8, 9):
                self.unknown[w] = self.unknown.get(w, 0) + 1
                return False
            sh = self.g((w >> 11) & 31) & 63
            xo = 0 if sub == 8 else 1
        r = _rotl64(v, sh)
        if xo == 0:
            r &= _mask(mbe, 63)
        elif xo == 1:
            r &= _mask(0, mbe)
        elif xo == 2:
            r &= _mask(mbe, 63 - sh)
        else:
            m = _mask(mbe, 63 - sh)
            r = (r & m) | (self.g(ra) & ~m & M64)
        self.s(ra, r)
        if rc:
            self.cr0(r)

    def _fpint(self, w):
        frt, frb, xo = (w >> 21) & 31, (w >> 11) & 31, (w >> 1) & 0x3FF
        b = self.u.reg_read(UC_PPC_REG_FPR0 + frb)
        if xo == 846:   # fcfid: the int64 in frb as a double
            i = _s64(b & M64)
            out = struct.unpack(">Q", struct.pack(">d", float(i)))[0]
        else:
            d = struct.unpack(">d", struct.pack(">Q", b & M64))[0]
            if d != d:
                i = -(1 << 63)
            else:
                i = int(d) if xo == 815 else int(round(d))
                i = max(-(1 << 63), min((1 << 63) - 1, i))
            out = i & M64
        self.u.reg_write(UC_PPC_REG_FPR0 + frt, out)

    def _x31(self, w):
        rt, ra, rb, rc = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 1
        x10 = (w >> 1) & 0x3FF
        x9 = (w >> 1) & 0x1FF
        if x10 in _LOADX:
            self.fix = [(rt, _LOADX[x10], 0)]
            if x10 in (55, 119, 311, 375):      # update forms
                self.fix.append((ra, "v", ((self.g(ra) + self.g(rb)) & M64) >> 32))
            return False
        if x10 in (21, 53, 341, 373, 149, 181):     # ldx ldux lwax lwaux stdx stdux
            ea = (self.ga(ra) + self.g(rb)) & M64
            if x10 in (149, 181):
                self.w64(ea, self.g(rt))
            elif x10 in (341, 373):
                self.s(rt, struct.unpack(">i", bytes(self.u.mem_read(ea & M32, 4)))[0])
            else:
                self.s(rt, self.r64(ea))
            if x10 in (53, 181, 373):
                self.s(ra, ea)
            return
        if x10 in (0, 32):      # cmp, cmpl
            bf, l = (w >> 23) & 7, (w >> 21) & 1
            a, b = self.g(ra), self.g(rb)
            if not l:
                a, b = (_sx32(a & M32), _sx32(b & M32)) if x10 == 0 else (a & M32, b & M32)
            self.crf(bf, a, b, x10 == 0)
            return
        logic = {28: lambda a, b: a & b, 60: lambda a, b: a & ~b, 444: lambda a, b: a | b,
                 316: lambda a, b: a ^ b, 124: lambda a, b: ~(a | b), 412: lambda a, b: a | ~b,
                 284: lambda a, b: ~(a ^ b), 476: lambda a, b: ~(a & b)}
        if x10 in logic:        # X-form: rS is bits 21-25, rA the target
            r = logic[x10](self.g(rt), self.g(rb)) & M64
            self.s(ra, r)
            if rc:
                self.cr0(r)
            return
        if x10 in (24, 536, 792, 27, 539, 794, 824) or x10 >> 1 == 413:
            v = self.g(rt)
            if x10 >> 1 == 413:     # sradi
                n = rb | ((w >> 1) & 1) << 5
                sv = _s64(v)
                r = sv >> n
                self.ca(sv < 0 and (v & ((1 << n) - 1)) != 0)
            elif x10 == 824:        # srawi
                n = rb
                sv = _sx32(v & M32) - (1 << 64) if v & 0x80000000 else v & M32
                r = sv >> n
                self.ca(sv < 0 and (v & ((1 << n) - 1)) != 0)
            else:
                n = self.g(rb)
                if x10 == 24:       # slw
                    n &= 63
                    r = 0 if n > 31 else ((v & M32) << n) & M32
                elif x10 == 536:    # srw
                    n &= 63
                    r = 0 if n > 31 else (v & M32) >> n
                elif x10 == 792:    # sraw
                    n &= 63
                    sv = _sx32(v & M32) - (1 << 64) if v & 0x80000000 else v & M32
                    r = sv >> min(n, 63)
                    self.ca(sv < 0 and (n > 31 or (v & ((1 << n) - 1)) != 0))
                elif x10 == 27:     # sld
                    n &= 127
                    r = 0 if n > 63 else v << n
                elif x10 == 539:    # srd
                    n &= 127
                    r = 0 if n > 63 else v >> n
                else:               # srad
                    n &= 127
                    sv = _s64(v)
                    r = sv >> min(n, 63)
                    self.ca(sv < 0 and (n > 63 or (v & ((1 << n) - 1)) != 0))
            self.s(ra, r)
            if rc:
                self.cr0(r)
            return
        if x10 in (986, 922, 954, 26, 58):  # extsw extsh extsb cntlzw cntlzd
            v = self.g(rt)
            if x10 == 986:
                r = _sx32(v & M32)
            elif x10 == 922:
                r = (v & 0xFFFF) - (0x10000 if v & 0x8000 else 0)
            elif x10 == 954:
                r = (v & 0xFF) - (0x100 if v & 0x80 else 0)
            elif x10 == 26:
                r = 32 - (v & M32).bit_length()
            else:
                r = 64 - v.bit_length()
            self.s(ra, r)
            if rc:
                self.cr0(r)
            return
        if x9 in (266, 10, 138, 40, 8, 136, 104, 202, 234, 200, 232):
            a, b = self.g(ra), self.g(rb)
            if x9 == 266:
                r = a + b
            elif x9 == 10:
                r = a + b
                self.ca(r >> 64)
            elif x9 == 138:
                r = a + b + self.getca()
                self.ca(r >> 64)
            elif x9 == 40:
                r = b - a
            elif x9 == 8:
                r = (~a & M64) + b + 1
                self.ca(r >> 64)
            elif x9 == 136:
                r = (~a & M64) + b + self.getca()
                self.ca(r >> 64)
            elif x9 == 104:
                r = -a
            elif x9 == 202:
                r = a + self.getca()
                self.ca(r >> 64)
            elif x9 == 234:
                r = a + self.getca() + M64
                self.ca(r >> 64)
            elif x9 == 200:
                r = (~a & M64) + self.getca()
                self.ca(r >> 64)
            else:   # subfme
                r = (~a & M64) + self.getca() + M64
                self.ca(r >> 64)
            self.s(rt, r)
            if rc:
                self.cr0(r)
            return
        if x9 in (235, 233, 75, 11, 73, 9, 491, 459, 489, 457):
            a, b = self.g(ra), self.g(rb)
            al, bl = _sx32(a & M32), _sx32(b & M32)
            al, bl = _s64(al), _s64(bl)
            if x9 == 235:       # mullw: the 64-bit product of the low words
                r = al * bl
            elif x9 == 233:
                r = _s64(a) * _s64(b)
            elif x9 == 75:
                r = (al * bl) >> 32
            elif x9 == 11:
                r = ((a & M32) * (b & M32)) >> 32
            elif x9 == 73:
                r = (_s64(a) * _s64(b)) >> 64
            elif x9 == 9:
                r = (a * b) >> 64
            elif x9 == 491:     # divw
                r = 0 if bl == 0 else int(al / bl) & M32
            elif x9 == 459:
                r = 0 if (b & M32) == 0 else (a & M32) // (b & M32)
            elif x9 == 489:
                sa, sb = _s64(a), _s64(b)
                r = 0 if sb == 0 else int(sa / sb)
            else:
                r = 0 if b == 0 else a // b
            self.s(rt, r)
            if rc:
                self.cr0(r)
            return
        return False
