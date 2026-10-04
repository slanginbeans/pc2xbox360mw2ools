"""Decal layers merged into the ground under them, as the 360 map compiler does.

The PC draws a blend decal (a dirt patch, a rust streak) as its own see-through surface laid
over the ground's triangles. The 360 tools instead give those ground triangles a composite
material (stock mp_rust: 130 of them, "*42n_34n") whose shader set draws the ground and up to
two decal layers in one pass, blended per vertex. Converted maps kept the PC's way: mp_rust
had 2,291 see-through surfaces where stock has 101.

What a composite is (worked out from stock mp_rust against PC mp_rust, the same map):
  - shader set: the ground's without "wc_" and a trailing "p0", then one part per layer, the
    decal's feature codes with its layer number (wc_l_sm_b0c0n0s0 -> _b1c1n1s1; the multiply
    decal wc_unlit_multiply_lin -> _m1c1), then "p0" if the ground or a layer had it;
  - textures and constants: the ground's, then each layer's, renamed with its number
    (colorMap1, envMapParms2; name hash x = x * 33 ^ lowercase c);
  - draw order, flags, culling: the ground's; render state: any stock material with the same
    shader set (all of one set share it); surface types: the ground's and the layers' together;
  - each vertex: its colour bytes 2 and 3 hold layer 1's and layer 2's weight (the decal
    vertex's alpha); the world's layer data holds, per vertex, every layer's texture
    coordinates (2 floats), then for each normal-mapped layer 4 signed bytes (v * 127 + 127):
    the layer's texture directions in the ground's, [m11, m00, m10, m01] of the per-vertex
    average of d(layer uv) / d(ground uv), each row normalized.
"""
import re
import struct
from collections import defaultdict

import tree
from tree import Leaf, Ref, Str

VERTEX = 44                 # GfxWorldVertex: xyz, binormal sign, colour, uv, lightmap uv, normal, tangent
TEX_NAMES = (b"colorMap", b"normalMap", b"specularMap", b"detailMap")
MAX_LAYERS = 2


def name_hash(s):
    x = 0
    for c in s.lower():
        x = ((x * 33) & 0xFFFFFFFF) ^ c
    return x


TEX_BY_HASH = {name_hash(n): n for n in TEX_NAMES}


def _tgt(c):
    return c.target if isinstance(c, Ref) else c


def ground_code(ts):
    """wc_l_sm_r0c0n0s0p0 -> (b"l_sm_r0c0n0s0", True); None if not a lit world shader set."""
    m = re.match(rb"^wc_(l_sm_(?:[a-z]0)+?)(p0)?$", ts or b"")
    return (m.group(1), bool(m.group(2))) if m else None


def layer_code(ts, i):
    """A decal's shader set -> its part of a composite shader set for layer i, and whether it
    carries p0; None if it can't be a layer."""
    if ts == b"wc_unlit_multiply_lin":
        return b"m%dc%d" % (i, i), False
    m = re.match(rb"^wc_l_sm_([bt]0(?:[a-z]0)*?)(p0)?$", ts or b"")
    if not m:
        return None
    return m.group(1).replace(b"0", b"%d" % i), bool(m.group(2))


class DecalMerger:
    def __init__(self, porter, world, deref, asset_name):
        self.P, self.world = porter, world
        self.deref, self.asset_name = deref, asset_name
        self.plans = {}             # (ground material id, layer material ids) -> plan or None
        self.composites = {}        # same key -> composite material
        self.techsets = {}          # shader set name -> pointer target
        self.serial = 0

    # ------------------------------------------------------------ materials

    def mat(self, s):
        c = s.get("@", {}).get(("material", ())) if isinstance(s, dict) else None
        # Only materials written before the world (pointed at): a composite points at their
        # pictures, which must already be in the file.
        return self.deref(c) if isinstance(c, Ref) else None

    def tsname(self, m):
        ts = self.deref(m.get("@", {}).get(("techniqueSet", ())))
        return (self.asset_name(ts) or b"").lstrip(b",") if isinstance(ts, dict) else b""

    @staticmethod
    def sort_key(m):
        return (m.get("info") or {}).get("sortKey") or 0

    def have_techset(self, name):
        P = self.P
        key = ("MaterialTechniqueSet", name)
        return name in P.material_templates and (key in P.resident or key in P.library or key in P.named)

    @staticmethod
    def textures(m):
        tt = _tgt(m.get("@", {}).get(("textureTable", ())))
        return [e for e in tt if isinstance(e, dict)] if isinstance(tt, list) else []

    @staticmethod
    def constants(m):
        """[(name, literal raw)], constant Leaf (or None)."""
        ct = _tgt(m.get("@", {}).get(("constantTable", ())))
        if not isinstance(ct, Leaf) or not ct.raw:
            return [], None
        size = ct.t.size
        out = []
        for i in range(len(ct.raw) // size):
            e = ct.raw[i * size:(i + 1) * size]
            out.append((e[4:16].rstrip(b"\0"), e[16:32]))
        return out, ct

    def plan(self, gm, layers):
        """(shader set, its stock template material) for ground material gm with these layer
        materials, or None if the 360 has no such composite."""
        key = (id(gm), tuple(id(m) for m in layers))
        if key in self.plans:
            return self.plans[key]
        res = None
        g = ground_code(self.tsname(gm))
        if g is not None and 0 < len(layers) <= MAX_LAYERS:
            name, p0, ok = g[0], g[1], True
            for i, lm in enumerate(layers, 1):
                lc = layer_code(self.tsname(lm), i)
                # Every picture and constant needs a name the composite can number.
                if lc is None or any(e.get("nameHash") not in TEX_BY_HASH for e in self.textures(lm)) \
                        or any(len(n) >= 12 for n, _ in self.constants(lm)[0]):
                    ok = False
                    break
                name += b"_" + lc[0]
                p0 = p0 or lc[1]
            if ok:
                name += b"p0" if p0 else b""
                tpl = self.P.material_templates.get(name) if self.have_techset(name) else None
                if tpl is not None and self.template_state(tpl) is not None:
                    res = (name, tpl)
        self.plans[key] = res
        return res

    @staticmethod
    def template_state(tpl):
        state = tpl.get("@", {}).get(("stateBitsTable", ()))
        if isinstance(state, Ref):
            state = state.target if state.rel == 0 else None
        return state if isinstance(state, Leaf) else None

    def techset_ref(self, name):
        if name not in self.techsets:
            P = self.P
            key = ("MaterialTechniqueSet", name)
            if key in P.resident or key in P.named:
                d = P.reference("MaterialTechniqueSet", name)
            else:
                d = P.stock_copy(P.library[key])
            d["_asset"] = "MaterialTechniqueSet"
            d["_slot"] = tree.InsertSlot(d)
            d["_forward"] = True
            self.techsets[name] = d
        return self.pointer_to(self.techsets[name])

    @staticmethod
    def pointer_to(asset):
        """An alias to asset, which its first pointer writes (it has no other place)."""
        r = Ref(0)
        r.target, r.rel = asset["_slot"], 0
        return r

    @staticmethod
    def image_pointer(c):
        """A pointer to the picture a texture entry points at (written before the world)."""
        if isinstance(c, Ref):
            r = Ref(c.val)
            r.target, r.rel, r.t = c.target, c.rel, c.t
            return r
        if isinstance(c, dict):
            r = Ref(0)
            r.target, r.rel = (c["_slot"], 0) if "_slot" in c else (c, 0)
            return r
        return c

    def entry_copy(self, e, number=None):
        new = {k: (dict(v) if isinstance(v, dict) else v) for k, v in e.items() if k != "u"}
        u = e.get("u")
        if isinstance(u, dict):
            new["u"] = {k: v for k, v in u.items() if k != "@"}
            new["u"]["@"] = {k: self.image_pointer(v) for k, v in u.get("@", {}).items()}
            if new["u"].get("union") in ("ffffffff", "fffffffe"):
                new["u"]["union"] = "00000001"
        if number is not None:
            base = TEX_BY_HASH[e["nameHash"]]
            new["nameHash"] = name_hash(base + b"%d" % number)
            new["nameEnd"] = ord(b"%d" % number)
            ss = new.get("samplerState")
            if isinstance(ss, dict) and ss.get("mipMap"):
                # As the stock composites have them (the ground's): specular 1, others 2.
                ss["mipMap"] = 1 if e.get("semantic") == 8 else 2
        return new

    def composite(self, gm, layers):
        key = (id(gm), tuple(id(m) for m in layers))
        if key in self.composites:
            return self.composites[key]
        tsname, tpl = self.plan(gm, layers)
        table = [self.entry_copy(e) for e in self.textures(gm)]
        for i, lm in enumerate(layers, 1):
            table += [self.entry_copy(e, i) for e in self.textures(lm)]
        table.sort(key=lambda e: e["nameHash"])
        consts, ct = self.constants(gm)
        entries = [(name_hash(n), n, lit) for n, lit in consts]
        for i, lm in enumerate(layers, 1):
            lc, lct = self.constants(lm)
            ct = ct or lct
            entries += [(name_hash(n + b"%d" % i), n + b"%d" % i, lit) for n, lit in lc]
        entries.sort()
        cleaf = None
        if entries:
            raw = b"".join(struct.pack(ct.E + "I", h) + n.ljust(12, b"\0") + lit for h, n, lit in entries)
            cleaf = Leaf(ct.t, len(entries), raw, ct.E)
        state = self.template_state(tpl)
        stb = 0
        for m in [gm] + list(layers):
            stb |= (m.get("info") or {}).get("surfaceTypeBits") or 0
        self.serial += 1
        name = b"*dl%d" % self.serial
        d = {k: v for k, v in gm.items() if k not in ("@", "_slot", "_forward", "_template", "info")}
        d["info"] = {k: v for k, v in gm["info"].items() if k != "@"}
        d["info"]["@"] = {("name", ()): Str(name)}
        d["info"]["name"] = "follow"
        d["info"]["surfaceTypeBits"] = stb
        for k in ("stateBitsEntry", "stateBitsCount"):
            d[k] = tpl[k]
        d["textureCount"] = len(table)
        d["constantCount"] = len(entries)
        d["techniqueSet"] = "0x00000001"
        d["textureTable"] = "follow"
        d["constantTable"] = "follow" if cleaf else None
        d["stateBitsTable"] = "follow"
        d["@"] = {("techniqueSet", ()): self.techset_ref(tsname), ("textureTable", ()): table,
                  ("stateBitsTable", ()): state}
        if cleaf:
            d["@"][("constantTable", ())] = cleaf
        d["_asset"] = "Material"
        d["_slot"] = tree.InsertSlot(d)
        d["_forward"] = True
        self.composites[key] = d
        return d

    # ------------------------------------------------------------ geometry

    def run(self):
        P, world = self.P, self.world
        dpvs = world.get("dpvs") or {}
        dch = dpvs.get("@", {})
        draw = world.get("draw") or {}
        surfs = _tgt(dch.get(("surfaces", ())))
        n = dpvs.get("staticSurfaceCount") or 0
        vd = draw.get("vd") or {}
        V = _tgt(vd.get("@", {}).get(("vertices", ())))
        I = _tgt(draw.get("@", {}).get(("indices", ())))
        vld = draw.get("vld") or {}
        LD = _tgt(vld.get("@", {}).get(("data", ())))
        if not (isinstance(surfs, list) and isinstance(V, Leaf) and isinstance(I, Leaf) and isinstance(LD, Leaf)):
            P.warn("decal layers couldn't be merged (world data not found)")
            return
        if (draw.get("vertexLayerDataSize") or 0) > 4:
            return              # already has layer data (a 360 file)
        E, vraw = V.E, V.raw
        idx = list(struct.unpack(I.E + "%dH" % (len(I.raw) // 2), I.raw))

        def tris(s):
            t = s["tris"]
            fv, b = t["firstVertex"], t["baseIndex"]
            return [(fv + idx[b + 3 * k], fv + idx[b + 3 * k + 1], fv + idx[b + 3 * k + 2])
                    for k in range(t["triCount"])]

        pos_cache = {}

        def pos(v):
            p = pos_cache.get(v)
            if p is None:
                p = pos_cache[v] = tuple(round(x, 1) for x in struct.unpack_from(E + "3f", vraw, VERTEX * v))
            return p

        mats = [self.mat(s) for s in surfs[:n]]
        ground_tris = {}            # triangle (3 positions) -> (surface, triangle)
        gtris = {}
        for i in range(n):
            m = mats[i]
            if m is None or self.sort_key(m) >= 6 or ground_code(self.tsname(m)) is None:
                continue
            gtris[i] = tris(surfs[i])
            for k, t in enumerate(gtris[i]):
                ground_tris.setdefault(frozenset(pos(v) for v in t), (i, k))
        cover = defaultdict(dict)   # (ground surface, triangle) -> {decal surface: decal triangle}
        decals, blocked = set(), set()
        for i in range(n):
            m = mats[i]
            if m is None or self.sort_key(m) < 6 or layer_code(self.tsname(m), 1) is None:
                continue
            decals.add(i)
            for t in tris(surfs[i]):
                g = ground_tris.get(frozenset(pos(v) for v in t))
                if g is None or i in cover[g]:
                    blocked.add(i)
                    break
                cover[g][i] = t
        order = lambda j: (self.sort_key(mats[j]), j)

        def layers_of(g):
            return sorted((j for j in cover[g] if j not in blocked), key=order)

        while True:
            new = set()
            for g in list(cover):
                js = layers_of(g)
                if js and self.plan(mats[g[0]], [mats[j] for j in js]) is None:
                    new.update(js)
            if not new - blocked:
                break
            blocked |= new
        merged = {j for j in decals if j not in blocked}
        if not merged:
            P.log("  decal layers: none could be merged")
            return

        # New surfaces for every ground surface a merged decal lies on.
        vout = bytearray(vraw)
        lout = bytearray()
        nverts = len(vraw) // VERTEX
        split = {}
        for gi in sorted({g[0] for g in cover if layers_of(g)}):
            s = surfs[gi]
            groups = defaultdict(list)          # layer surfaces -> triangle numbers
            for k in range(len(gtris[gi])):
                groups[tuple(layers_of((gi, k)))].append(k)
            out = []
            for js, ks in groups.items():
                ns = {k: (dict(v) if isinstance(v, dict) else v) for k, v in s.items() if k not in ("tris", "@")}
                ns["@"] = dict(s.get("@", {}))
                t = dict(s["tris"])
                t["baseIndex"], t["triCount"] = len(idx), len(ks)
                if not js:
                    fv = t["firstVertex"]
                    for k in ks:
                        idx.extend(v - fv for v in gtris[gi][k])
                    ns["tris"] = t
                    out.append(ns)
                    continue
                lms = [mats[j] for j in js]
                plan = self.plan(mats[gi], lms)
                normal = [i for i, lm in enumerate(lms) if b"n" in layer_code(self.tsname(lm), i + 1)[0][2:]]
                verts = {}              # (ground vertex, decal vertex per layer) -> new number
                rot = defaultdict(lambda: [[0.0] * 4 for _ in normal])
                tri_new = []
                for k in ks:
                    gt = gtris[gi][k]
                    dts = [cover[(gi, k)][j] for j in js]
                    corner = []
                    for gv in gt:
                        p = pos(gv)
                        dvs = tuple(next(dv for dv in dt if pos(dv) == p) for dt in dts)
                        corner.append(verts.setdefault((gv, dvs), len(verts)))
                    tri_new.append(corner)
                    if normal:
                        guv = [struct.unpack_from(E + "2f", vraw, VERTEX * gv + 20) for gv in gt]
                        d1 = (guv[1][0] - guv[0][0], guv[1][1] - guv[0][1])
                        d2 = (guv[2][0] - guv[0][0], guv[2][1] - guv[0][1])
                        det = d1[0] * d2[1] - d2[0] * d1[1]
                        for q, li in enumerate(normal):
                            dt = dts[li]
                            luv = [struct.unpack_from(E + "2f", vraw, VERTEX * next(dv for dv in dt if pos(dv) == pos(gv)) + 20)
                                   for gv in gt]
                            e1 = (luv[1][0] - luv[0][0], luv[1][1] - luv[0][1])
                            e2 = (luv[2][0] - luv[0][0], luv[2][1] - luv[0][1])
                            if abs(det) < 1e-12:
                                continue
                            # A = dL * inverse(dB), dB's columns the ground's uv edges.
                            a00 = (e1[0] * d2[1] - e2[0] * d1[1]) / det
                            a01 = (e2[0] * d1[0] - e1[0] * d2[0]) / det
                            a10 = (e1[1] * d2[1] - e2[1] * d1[1]) / det
                            a11 = (e2[1] * d1[0] - e1[1] * d2[0]) / det
                            for c in corner:
                                acc = rot[c][q]
                                acc[0] += a00
                                acc[1] += a01
                                acc[2] += a10
                                acc[3] += a11
                if nverts + len(verts) > 0xFFFFFFFF or len(verts) > 0xFFFF:
                    raise ValueError("decal merge: vertex block too big")
                fv = nverts
                layer_at = len(lout)
                for (gv, dvs), _ in sorted(verts.items(), key=lambda kv: kv[1]):
                    rec = bytearray(vraw[VERTEX * gv:VERTEX * gv + VERTEX])
                    for li, dv in enumerate(dvs):
                        rec[16 + 2 + li] = vraw[VERTEX * dv + 16]       # the decal's alpha
                    vout += rec
                for (gv, dvs), num in sorted(verts.items(), key=lambda kv: kv[1]):
                    for dv in dvs:
                        lout += vraw[VERTEX * dv + 20:VERTEX * dv + 28]
                    for q in range(len(normal)):
                        a = rot[num][q] if num in rot else [1.0, 0.0, 0.0, 1.0]
                        r0 = (a[0] * a[0] + a[1] * a[1]) ** 0.5 or 1.0
                        r1 = (a[2] * a[2] + a[3] * a[3]) ** 0.5 or 1.0
                        if not any(a):
                            a, r0, r1 = [1.0, 0.0, 0.0, 1.0], 1.0, 1.0
                        m = (a[3] / r1, a[0] / r0, a[2] / r1, a[1] / r0)
                        lout += bytes(max(0, min(254, int(round(max(-1.0, min(1.0, x)) * 127)) + 127)) for x in m)
                nverts += len(verts)
                for c in tri_new:
                    idx.extend(c)
                t["firstVertex"], t["vertexCount"], t["vertexLayerData"] = fv, len(verts), layer_at
                ns["tris"] = t
                ns["material"] = "0x00000001"
                ns["@"][("material", ())] = self.pointer_to(self.composite(mats[gi], lms))
                out.append(ns)
            split[gi] = out
        V.raw, V.n = bytes(vout), nverts
        I.raw, I.n = struct.pack(I.E + "%dH" % len(idx), *idx), len(idx)
        self.rebuild(surfs, n, split, merged)
        if not lout:
            lout = bytearray(LD.raw)
        LD.raw, LD.n = bytes(lout), len(lout)
        draw["vertexCount"], draw["indexCount"], draw["vertexLayerDataSize"] = nverts, len(idx), len(lout)
        P.log("  decal layers: %d decal surfaces merged into the ground under them (%d composite "
              "materials, %d ground surfaces split; %d decals left as they were)"
              % (len(merged), len(self.composites), len(split), len(decals) - len(merged)))

    # ------------------------------------------------------------ renumbering

    def rebuild(self, surfs, n, split, merged):
        """Every surface table and surface number after the surfaces changed: merged decals go,
        split ground surfaces become their parts."""
        world, dpvs = self.world, self.world["dpvs"]
        dch = dpvs["@"]
        new_static, expand = [], []
        for i in range(n):
            if i in merged:
                parts = []
            elif i in split:
                parts = split[i]
            else:
                parts = [surfs[i]]
            expand.append(list(range(len(new_static), len(new_static) + len(parts))))
            new_static += parts
        n2 = len(new_static)
        src = []                    # new surface -> the old one it came from
        for i in range(n):
            src += [i] * len(expand[i])
        surfs[:n] = new_static
        # Bounds: kept, or worked out for the parts (middle, half size).
        bl = _tgt(dch.get(("surfacesBounds", ())))
        if isinstance(bl, Leaf):
            size, out = bl.t.size, bytearray()
            V = _tgt(world["draw"]["vd"]["@"][("vertices", ())])
            I = _tgt(world["draw"]["@"][("indices", ())])
            for j, i in enumerate(src):
                old = bl.raw[i * size:(i + 1) * size]
                if i in split:
                    s = new_static[j]["tris"]
                    lo, hi = [1e30] * 3, [-1e30] * 3
                    for v in self._surface_vertices(s, V, I):
                        for q in range(3):
                            lo[q], hi[q] = min(lo[q], v[q]), max(hi[q], v[q])
                    old = struct.pack(bl.E + "6f", *[(lo[q] + hi[q]) / 2 for q in range(3)],
                                      *[(hi[q] - lo[q]) / 2 for q in range(3)]) + bytes(size - 24)
                out += old
            bl.raw, bl.n = bytes(out) + bl.raw[n * size:], n2 + max(0, bl.n - n)
        sm = _tgt(dch.get(("surfaceMaterials", ())))
        if isinstance(sm, Leaf):
            sm.raw, sm.n = bytes(sm.t.size * n2) + sm.raw[n * sm.t.size:], n2 + max(0, sm.n - n)
        words = ((n2 + 31) // 32 + 3) // 4 * 4
        for k, c in list(dch.items()):
            if k[0] == "surfaceVisData" and isinstance(_tgt(c), Leaf):
                lf = _tgt(c)
                lf.raw, lf.n = bytes(n2), n2
        dpvs["surfaceVisDataCount"] = words
        bits = _tgt(dch.get(("surfaceCastsSunShadow", ())))
        if isinstance(bits, Leaf) and bits.raw:
            old = struct.unpack(bits.E + "%dI" % (len(bits.raw) // 4), bits.raw)
            new = [0] * words
            for j, i in enumerate(src):
                if i >> 5 < len(old) and old[i >> 5] >> (i & 31) & 1:
                    new[j >> 5] |= 1 << (j & 31)
            unit = len(bits.raw) // bits.n if bits.n else 4
            bits.raw = struct.pack(bits.E + "%dI" % words, *new)
            bits.n = len(bits.raw) // unit
        # The sorted list (culling tree nodes hold ranges of it) and the lists holding numbers.
        so = _tgt(dch.get(("sortedSurfIndex", ())))
        if isinstance(so, Leaf):
            old = struct.unpack(so.E + "%dH" % so.n, so.raw[:2 * so.n])
            at, new = [0], []
            for i in old:
                new += expand[i] if i < n else [i - n + n2]
                at.append(len(new))
            so.raw, so.n = struct.pack(so.E + "%dH" % len(new), *new), len(new)
            for ct in _tgt(world["@"].get(("aabbTrees", ()))) or []:
                for nd in _tgt(ct.get("@", {}).get(("aabbTree", ()))) or []:
                    a, c = nd["startSurfIndex"], nd["surfaceCount"]
                    if a + c <= len(old):
                        nd["startSurfIndex"], nd["surfaceCount"] = at[a], at[a + c] - at[a]
        for gm in _tgt(world["@"].get(("shadowGeom", ()))) or []:
            lf = _tgt(gm.get("@", {}).get(("sortedSurfIndex", ()))) if isinstance(gm, dict) else None
            if isinstance(lf, Leaf) and gm.get("surfaceCount"):
                old = struct.unpack(lf.E + "%dH" % gm["surfaceCount"], lf.raw[:2 * gm["surfaceCount"]])
                new = [x for i in old for x in (expand[i] if i < n else [i - n + n2])]
                lf.raw, lf.n = struct.pack(lf.E + "%dH" % len(new), *new), len(new)
                gm["surfaceCount"] = len(new)
        models = _tgt(world["@"].get(("models", ())))
        if isinstance(models, Leaf):
            raw = bytearray(models.raw)
            size = models.t.size
            off = {m.name: m.offset for m in models.t.members}
            for k in range(models.n):
                c, a = struct.unpack_from(models.E + "HH", raw, k * size + off["surfaceCount"])
                if a == 0 and c == n:
                    c = n2
                elif a != 0xFFFF and c and a < n:
                    a = expand[a][0] if expand[a] else a
                struct.pack_into(models.E + "HH", raw, k * size + off["surfaceCount"], c, a)
            models.raw = bytes(raw)
        for sky in _tgt(world["@"].get(("skies", ()))) or []:
            lf = _tgt(sky.get("@", {}).get(("skyStartSurfs", ()))) if isinstance(sky, dict) else None
            if isinstance(lf, Leaf):
                v = list(struct.unpack(lf.E + "%di" % lf.n, lf.raw))
                v = [expand[x][0] if 0 <= x < n and expand[x] else x for x in v]
                lf.raw = struct.pack(lf.E + "%di" % lf.n, *v)
        dpvs["staticSurfaceCount"] = n2
        world["surfaceCount"] = (world.get("surfaceCount") or n) - n + n2

    @staticmethod
    def _surface_vertices(t, V, I):
        idx = struct.unpack_from(I.E + "%dH" % (3 * t["triCount"]), I.raw, 2 * t["baseIndex"])
        for v in set(idx):
            yield struct.unpack_from(V.E + "3f", V.raw, VERTEX * (t["firstVertex"] + v))


def merge_decal_layers(porter, world, deref, asset_name):
    DecalMerger(porter, world, deref, asset_name).run()
