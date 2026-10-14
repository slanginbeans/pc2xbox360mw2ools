"""Carry function names from a debug build (PDB) over to another build of the same game by
matching masked instruction sequences, then along call graphs. Usage:
    match.py ALPHA_IMAGE ALPHA_PDB TARGET_IMAGE OUT.txt"""
import struct, sys, hashlib
from collections import defaultdict
sys.path.insert(0, __import__("os").path.dirname(__file__))
import pdbread
B = 0x82000000

def sections(img):
    pe = struct.unpack_from("<I", img, 0x3C)[0]
    nsec = struct.unpack_from("<H", img, pe + 6)[0]
    opt = struct.unpack_from("<H", img, pe + 20)[0]
    out = {}
    for i in range(nsec):
        o = pe + 24 + opt + 40 * i
        name = img[o:o + 8].rstrip(b"\0").decode()
        vsize, va = struct.unpack_from("<II", img, o + 8)
        out[name] = (B + va, vsize)
    return out

def pdata_funcs(img):
    """{start: length in bytes} from the .pdata table (BeginAddress, then prolog/len bits)."""
    a, n = sections(img)[".pdata"]
    out = {}
    for o in range(a - B, a - B + n, 8):
        beg, info = struct.unpack_from(">II", img, o)
        if not beg: continue
        length = ((info >> 8) & 0x3FFFFF) * 4
        out[beg] = length
    return out

def words(img, a, n):
    return struct.unpack_from(">%dI" % (n // 4), img, a - B)

DFORM = set(range(7, 16)) | set(range(24, 30)) | set(range(32, 56)) | {58, 62}
def mask(w):
    op = w >> 26
    if op == 18: return w & 0xFC000003
    if op == 16: return w & 0xFFFF0003
    if op in DFORM: return w & 0xFFFF0000
    return w

def calls(img, a, n):
    out = []
    for i, w in enumerate(words(img, a, n)):
        if w >> 26 == 18 and w & 1:
            li = w & 0x3FFFFFC
            if li & 0x2000000: li -= 0x4000000
            out.append(a + 4 * i + li if not w & 2 else li)
    return out

def sig(img, a, n):
    return hashlib.sha1(struct.pack(">%dI" % (n // 4), *map(mask, words(img, a, n)))).digest()

def main(aimg_p, apdb_p, timg_p, out_p):
    aimg = open(aimg_p, "rb").read(); timg = open(timg_p, "rb").read()
    funcs, pubs, _ = pdbread.read(apdb_p)
    names = {a: v[0] for a, v in funcs.items()}
    for a, (n, fl) in pubs.items(): names.setdefault(a, n)
    afun = pdata_funcs(aimg); tfun = pdata_funcs(timg)
    print("alpha functions (pdata) %d, named %d; target functions %d" % (len(afun), sum(1 for a in afun if a in names), len(tfun)))
    tindex = defaultdict(list)
    for a, n in tfun.items():
        if n: tindex[(n, sig(timg, a, n))].append(a)
    aindex = defaultdict(list)
    for a, n in afun.items():
        if n: aindex[(n, sig(aimg, a, n))].append(a)
    match = {}   # target -> alpha
    how = {}
    for k, alist in aindex.items():
        tl = tindex.get(k)
        if tl and len(tl) == 1 and len(alist) == 1:
            match[tl[0]] = alist[0]; how[tl[0]] = "exact"
    print("exact unique matches:", len(match))
    # Along calls: a matched pair whose call lists line up names the callees pairwise.
    changed = True
    while changed:
        changed = False
        rev = {v: k for k, v in match.items()}
        for t, a in list(match.items()):
            ca = calls(aimg, a, afun[a]); ct = calls(timg, t, tfun.get(t, 0))
            if len(ca) != len(ct) or not ca: continue
            for x, y in zip(ca, ct):
                if y in match or x in rev: continue
                if x in afun and y in tfun:
                    match[y] = x; rev[x] = y; how[y] = "call"; changed = True
    print("after following calls:", len(match))
    named = 0
    with open(out_p, "w") as fh:
        for t in sorted(match):
            n = names.get(match[t])
            if n:
                named += 1
                fh.write("%08X %s %s %08X\n" % (t, n, how[t], match[t]))
    print("target functions named:", named, "of", len(tfun), "->", out_p)

if __name__ == "__main__":
    main(*sys.argv[1:5])
