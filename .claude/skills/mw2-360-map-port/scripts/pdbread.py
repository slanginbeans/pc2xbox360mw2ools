"""Minimal PDB (MSF 7.00) reader: functions (name, address, length) and public symbols, for
match.py. Usage: pdbread.py FILE.pdb (prints sections and a few lookups)."""
import struct, sys

class MSF:
    def __init__(self, path):
        d = self.d = open(path, "rb").read()
        assert d.startswith(b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0")
        self.bs, _, _, dirbytes, _, bmap = struct.unpack_from("<6I", d, 32)
        nb = (dirbytes + self.bs - 1) // self.bs
        dblocks = struct.unpack_from("<%dI" % nb, d, bmap * self.bs)
        dd = b"".join(d[b * self.bs:(b + 1) * self.bs] for b in dblocks)[:dirbytes]
        n = struct.unpack_from("<I", dd, 0)[0]
        sizes = struct.unpack_from("<%dI" % n, dd, 4)
        pos = 4 + 4 * n
        self.streams = []
        for s in sizes:
            if s == 0xFFFFFFFF:
                self.streams.append(None); continue
            k = (s + self.bs - 1) // self.bs
            blocks = struct.unpack_from("<%dI" % k, dd, pos); pos += 4 * k
            self.streams.append((s, blocks))
    def stream(self, i):
        s = self.streams[i]
        if s is None: return b""
        size, blocks = s
        return b"".join(self.d[b * self.bs:(b + 1) * self.bs] for b in blocks)[:size]

def cstr(b, o):
    e = b.index(b"\0", o); return b[o:e].decode("latin-1"), e + 1

def read(path):
    m = MSF(path)
    dbi = m.stream(3)
    (sig, ver, age, gsi, build, psi, dllver, symrec, rbld, modsz, secconsz, secmapsz,
     srcsz, tsmsz, mfc, dbgsz, ecsz, flags, machine, pad) = struct.unpack_from("<iIIHHHHHHiiiiiIiiHHI", dbi, 0)
    off = 64
    mods = []
    end = off + modsz
    while off < end:
        # ModInfo: 4 unused, SectionContribution(28), flags u16, modSymStream u16, symByteSize u32, c11 u32, c13 u32, srcfilecount u16, pad u16, unused u32, srcfilenameidx u32, pdbfilepathidx u32, names
        ms, symsz = struct.unpack_from("<HI", dbi, off + 34)
        name, o = cstr(dbi, off + 64); obj, o = cstr(dbi, o)
        mods.append((ms, symsz, name))
        off = (o + 3) & ~3
    dbg_off = 64 + modsz + secconsz + secmapsz + srcsz + tsmsz + ecsz
    dbg = struct.unpack_from("<%dH" % (dbgsz // 2), dbi, dbg_off)
    sechdr = m.stream(dbg[5])
    secs = [struct.unpack_from("<8sIIIIIIHHI", sechdr, i) for i in range(0, len(sechdr) - 39, 40)]
    def addr(seg, offs):
        if 1 <= seg <= len(secs): return 0x82000000 + secs[seg - 1][2] + offs
        return None
    funcs = {}
    for ms, symsz, mname in mods:
        if ms == 0xFFFF: continue
        s = m.stream(ms)
        o = 4
        while o < symsz:
            ln, kind = struct.unpack_from("<HH", s, o)
            if kind in (0x1110, 0x110F, 0x1147, 0x1146):     # S_GPROC32, S_LPROC32 (+ _ID)
                plen, = struct.unpack_from("<I", s, o + 16)
                poff, pseg = struct.unpack_from("<IH", s, o + 32)
                name, _ = cstr(s, o + 39)
                a = addr(pseg, poff)
                if a: funcs[a] = (name, plen, mname)
            o += ln + 2
    pubs = {}
    rec = m.stream(symrec)
    o = 0
    while o + 4 <= len(rec):
        ln, kind = struct.unpack_from("<HH", rec, o)
        if kind == 0x110E:   # S_PUB32
            fl, poff, pseg = struct.unpack_from("<IIH", rec, o + 4)
            name, _ = cstr(rec, o + 14)
            a = addr(pseg, poff)
            if a: pubs[a] = (name, fl)
        o += ln + 2
    return funcs, pubs, secs

if __name__ == "__main__":
    funcs, pubs, secs = read(sys.argv[1])
    for s in secs: print(s[0].rstrip(b"\0"), hex(0x82000000 + s[2]), hex(s[1]))
    print("functions with lengths:", len(funcs), " public symbols:", len(pubs))
    for a in sorted(funcs)[:5]: print(hex(a), funcs[a])
    for n in ("R_SortMaterials", "Material_Compare", "DB_LoadXFile", "Image_", "R_DrawLit"):
        hits = [(hex(a), v[0]) for a, v in funcs.items() if n.lower() in v[0].lower()][:4]
        print(n, hits)
