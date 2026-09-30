"""Fix up a zone after pieces of it changed size.

The zone is one stream: each piece of data the loader reads is copied into a memory block
(virtual, physical, temp...) at the next free, aligned place. Pointers that refer back to
earlier data hold that data's block and place, so when a piece grows or shrinks, everything
after it moves and those pointers, and the block sizes in the zone header, have to follow.

relocate(new_zone, old_reader) walks the new zone the way the game will, pairs every piece of
block memory with the same piece in the original walk (asset by asset, in order), and rewrites
each back-pointer to the new place of what it pointed at.
"""

import bisect
import struct
import zlib

import zone as zone_mod

BLOCK_SHIFT = 28
OFFSET_MASK = (1 << BLOCK_SHIFT) - 1
# Blocks whose place only moves forward during a load (temp is reused, so it is not).
GROWING = (zone_mod.PHYSICAL, zone_mod.RUNTIME, zone_mod.VIRTUAL, zone_mod.LARGE, zone_mod.CALLBACK,
           zone_mod.VERTEX, zone_mod.INDEX)


class RelocateError(Exception):
    pass


def _by_asset(spans):
    out = {}
    for sp in spans:
        if sp[0] in GROWING:
            out.setdefault(sp[3], []).append(sp)
    return out


def _table(old_r, new_r, index_map=None):
    """Per block: sorted old starts, and for each the (old start, old size, new start, new size).
    index_map: new asset index -> old asset index (None for an added asset), when assets were
    added or removed."""
    old_a, new_a = _by_asset(old_r.spans), _by_asset(new_r.spans)
    if index_map is not None:
        new_a = {(-1 if a == -1 else index_map[a]): v for a, v in new_a.items()
                 if a == -1 or index_map[a] is not None}
    pairs = {}
    for a, olds in old_a.items():
        news = new_a.get(a)
        if news is None:
            # Asset removed: nothing may point into it any more (checked in _map).
            for o in olds:
                if o[0] in GROWING:
                    pairs.setdefault(o[0], []).append((o[1], o[2], None, a))
            continue
        if len(news) != len(olds):
            raise RelocateError("asset %d changed shape (%d pieces, was %d); only sizes may change"
                                % (a, len(news), len(olds)))
        for o, n in zip(olds, news):
            if o[0] != n[0]:
                raise RelocateError("asset %d: a piece moved to another memory block" % a)
            if o[0] in GROWING:
                pairs.setdefault(o[0], []).append((o[1], o[2], n[1], n[2]))
    table = {}
    for b, rows in pairs.items():
        rows.sort()
        table[b] = ([r[0] for r in rows], rows)
    return table


def _map(table, val):
    raw = val - 1
    b, off = raw >> BLOCK_SHIFT, raw & OFFSET_MASK
    if b not in table:
        raise RelocateError("pointer 0x%08x points into memory block %d, which can't be moved" % (val, b))
    starts, rows = table[b]
    i = bisect.bisect_right(starts, off) - 1
    if i < 0:
        return val

    def at(o):
        return ((b << BLOCK_SHIFT) | o) + 1
    ostart, osize, nstart, nsize = rows[i]
    rel = off - ostart
    if rel < osize:
        if nstart is None:
            raise RelocateError("something still points into asset %d, which was removed" % nsize)
        if rel >= nsize:
            raise RelocateError("a pointer points into data that got shorter (block %d, offset %d)" % (b, off))
        return at(nstart + rel)
    # Between pieces (alignment padding or the end of one): keep the distance to the next piece.
    for j in range(i + 1, len(rows)):
        if rows[j][2] is not None:
            return at(rows[j][2] - (rows[j][0] - off))
    for j in range(i, -1, -1):
        if rows[j][2] is not None:
            return at(rows[j][2] + rows[j][3] + off - (rows[j][0] + rows[j][1]))
    return val


class Layout:
    """What relocate needs to know about the original zone: where each piece of block memory
    was (only the blocks pointers can refer to), and how far each block got."""

    def __init__(self, spans, block_pos, block_max):
        self.spans, self.block_pos, self.block_max = spans, list(block_pos), list(block_max)

    @classmethod
    def of(cls, r):
        return cls([sp[:4] for sp in r.spans if sp[0] in GROWING], r.block_pos, r.block_max)

    def dumps(self):
        nb = len(self.block_pos)
        head = struct.pack(">II%dI" % (2 * nb), len(self.spans), nb, *(self.block_pos + self.block_max))
        body = b"".join(struct.pack(">BIIi", *sp) for sp in self.spans)
        return zlib.compress(head + body, 6)

    @classmethod
    def loads(cls, data):
        data = zlib.decompress(data)
        n, nb = struct.unpack_from(">II", data, 0)
        v = struct.unpack_from(">%dI" % (2 * nb), data, 8)
        at = 8 + 8 * nb
        spans = [struct.unpack_from(">BIIi", data, at + 13 * i) for i in range(n)]
        return cls(spans, v[:nb], v[nb:])


def relocate(new_zone, old_r, sch=None, index_map=None):
    """new_zone: the rebuilt zone with the original back-pointer values and header.
    old_r: a Reader that walked the original zone, or its Layout. Returns (fixed zone bytes,
    number of pointers moved)."""
    new_zone = bytearray(new_zone)
    new_r = zone_mod.Reader(bytes(new_zone), sch)
    new_r.walk()
    if new_r.pos != len(new_zone):
        raise RelocateError("the rebuilt zone doesn't read back cleanly (stopped at %d of %d)"
                            % (new_r.pos, len(new_zone)))
    table = _table(old_r, new_r, index_map)
    E, nb = new_r.E, new_r.plat.blocks
    moved = 0
    for zp, val in new_r.alias_list:
        if zp is None:
            continue
        nv = _map(table, val)
        if nv != val:
            struct.pack_into(E + "I", new_zone, zp, nv)
            moved += 1
    fmt = E + "%dI" % (2 + nb)
    hdr = list(struct.unpack_from(fmt, new_zone, 0))
    hdr[0] = len(new_zone) - new_r.plat.header_bytes
    for b in range(nb):
        if b == zone_mod.TEMP:
            delta = new_r.block_max[b] - old_r.block_max[b]
        else:
            delta = new_r.block_pos[b] - old_r.block_pos[b]
        hdr[2 + b] += delta
    struct.pack_into(fmt, new_zone, 0, *hdr)
    return bytes(new_zone), moved
