"""Check a written (360) fastfile's alias pointers, the two ways they crash the console.

    python check_aliases.py OUT.ff [OUT2.ff ...]

- forward: an alias to a place not loaded yet (at or past the block's current position);
- temp: an alias into the temporary block, which the game reuses for every asset, so the
  data it names is gone by then (stock files have none: mp_rust 0 of 68,943). Pointers to a
  temp asset written earlier must name its slot (in the virtual block) instead.

Prints the counts and the struct paths of the bad ones. Run from anywhere; finds mw2ff next
to the repo root.
"""
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "..", "mw2ff"))

import mw2ff  # noqa: E402
import port  # noqa: E402
import tree  # noqa: E402
import zone  # noqa: E402


def check(path):
    counts, where = Counter(), Counter()
    orig = zone.Reader.alias

    def alias(self, buf, loc, val):
        if val not in (0, 0xFFFFFFFF, 0xFFFFFFFE):
            v = val - 1
            blk, off = v >> 28, v & 0x0FFFFFFF
            pos = self.block_pos[blk] if blk < len(self.block_pos) else None
            bad = None
            if pos is None:
                bad = "bad block"
            elif off >= pos:
                bad = "forward"
            elif blk == zone.TEMP:
                bad = "temp"
            counts[bad or "ok"] += 1
            if bad:
                where[(bad, " > ".join(getattr(self, "path", ["?"])))] += 1
        return orig(self, buf, loc, val)

    zone.Reader.alias = alias
    try:
        ff, z = mw2ff.read_fastfile(path)
        with port.no_gc():
            tree.read_tree(z, mw2ff.schema_for(ff.platform))
    finally:
        zone.Reader.alias = orig
    print("%s: %s" % (path, dict(counts)))
    for (kind, p), n in where.most_common(10):
        print("    %-8s %5d  %s" % (kind, n, p))
    return not where


if __name__ == "__main__":
    ok = all([check(p) for p in sys.argv[1:]])
    sys.exit(0 if ok else 1)
