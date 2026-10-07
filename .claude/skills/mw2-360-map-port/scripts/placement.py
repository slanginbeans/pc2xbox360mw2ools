"""Where a converted file puts each kind of data, against stock 360 files.

    python placement.py CONVERTED.ff STOCK.ff [STOCK2.ff ...]

Reads each file with the zone reader and records, for every piece of data (by its struct path,
array indices dropped, and the asset type it belongs to), the memory block it loads into and
the largest power of two its block offset is a multiple of. Reports:
  - block differences: a kind of data that lands in a block no stock file puts it in (fatal for
    data the graphics chip reads: vertices, indices, pixels, shader code belong in PHYSICAL);
  - alignment below stock: data in PHYSICAL, or any data below 4 bytes, aligned less than every
    stock file aligns it;
  - how many kinds of data the stock files given never have (written inline where stock lists
    the asset on its own: see stock_rules.py's listing rules).
Read-only; caches nothing.
"""
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "..", "mw2ff"))

import mw2ff  # noqa: E402
import port  # noqa: E402
import tree  # noqa: E402
import zone  # noqa: E402

BLOCKS = {zone.TEMP: "TEMP", zone.PHYSICAL: "PHYSICAL", zone.VIRTUAL: "VIRTUAL",
          zone.LARGE: "LARGE", zone.CALLBACK: "CALLBACK"}


def profile(path):
    """{(asset type, path): [blocks, smallest alignment, reads]} of one file."""
    ff, z = mw2ff.read_fastfile(path)
    reads = []
    orig = zone.Reader.read

    def read(self, n, desc=("raw",)):
        if n and self.block != zone.RUNTIME:
            reads.append((self.block, self.block_pos[self.block], self.cur_asset[0] if self.cur_asset else -1,
                          " > ".join(self.path)))
        return orig(self, n, desc)

    zone.Reader.read = read
    try:
        with port.no_gc():
            root, _ = tree.read_tree(z, mw2ff.schema_for(ff.platform))
    finally:
        zone.Reader.read = orig
    types = [e[0] for e in root["assets"]]
    out = defaultdict(lambda: [set(), 4096, 0])
    for block, off, asset, p in reads:
        e = out[(types[asset] if 0 <= asset < len(types) else "?", re.sub(r"\[\d+\]", "[]", p))]
        e[0].add(BLOCKS.get(block, str(block)))
        align = 1
        while off % (align * 2) == 0 and align < 4096:
            align *= 2
        e[1] = min(e[1], align)
        e[2] += 1
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    ours = profile(sys.argv[1])
    stock = defaultdict(lambda: [set(), 4096, 0])
    for p in sys.argv[2:]:
        for k, v in profile(p).items():
            e = stock[k]
            e[0] |= v[0]
            e[1] = min(e[1], v[1])
            e[2] += v[2]
    blocks, aligns, unseen = [], [], defaultdict(int)
    for k, (bl, al, n) in ours.items():
        if k not in stock:
            unseen[k[0]] += 1
            continue
        if not bl <= stock[k][0]:
            blocks.append((k, sorted(bl), sorted(stock[k][0]), n))
        if al < stock[k][1] and ("PHYSICAL" in bl or al < 4):
            aligns.append((k, al, stock[k][1], n))
    print("%s: %d kinds of data; %d the stock files never have (%s)" % (
        sys.argv[1], len(ours), sum(unseen.values()), dict(unseen)))
    print("block differences: %d" % len(blocks))
    for (t, p), b, sb, n in blocks:
        print("   %-12s %s  ours %s, stock %s (x%d)" % (t, p[-120:], b, sb, n))
    print("alignment below stock (physical data, or under 4 bytes): %d" % len(aligns))
    for (t, p), a, sa, n in aligns:
        print("   %-12s %s  ours %d, stock at least %d (x%d)" % (t, p[-120:], a, sa, n))


if __name__ == "__main__":
    main()
