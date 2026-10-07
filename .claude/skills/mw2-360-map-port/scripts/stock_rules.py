"""Mine "stock always does" rules from stock 360 files and check converted maps against them.

    python stock_rules.py --stock DIR [--maps N] CONVERTED.ff [CONVERTED2.ff ...]

DIR holds the stock files (mp_*.ff, common_mp.ff). Rules are mined from the first N stock
maps (default all) and are of four kinds:
  value     a field of an asset type that takes one value, or a few, in every stock asset of
            that type (at least MIN_SEEN of them): a converted asset with another value breaks it;
  listing   how stock stores an asset type: always as an asset list entry of its own, never
            in full inside another asset (or the other way round);
  names     no two full assets of one type and name in a file; nothing under a name the
            always-loaded common_mp.ff also has;
  refs      every name-only reference (",name") names something the file or the always-loaded
            files have.
Prints each broken rule with how many assets break it and examples. Read-only.
"""
import argparse
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "..", "mw2ff"))

import port  # noqa: E402
import tree  # noqa: E402

MIN_SEEN = 30           # stock assets of a type before a value rule counts
MAX_VALUES = 4          # a field with more distinct stock values than this makes no value rule
SKIP_WORDS = ("name", "Name", "count", "Count", "size", "Size", "bounds", "Bounds", "origin",
              "checksum", "hash", "Hash", "memory", "Memory", "offset", "Offset", "index", "Index",
              "texture", "streams", "pixels", "union", "radius", "mins", "maxs", "midPoint",
              "halfSize", "first", "base", "Base", "frac", "Frac", "loop", "Loop", "time", "Time",
              "msec", "life", "Life", "scale", "Scale")


def asset_type(o):
    return o.get("_asset") if isinstance(o, dict) else None


def name_of(o):
    return port.asset_name(o) or port._name(o) or port.alias_name(o) or b""


def scalars(d, pre="", depth=0, out=None):
    out = {} if out is None else out
    if depth > 3:
        return out
    for k, v in d.items():
        if k == "@" or k.startswith("_") or any(w in k for w in SKIP_WORDS):
            continue
        p = pre + k
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            out[p] = v
        elif isinstance(v, str) and not v.startswith("0x") and v not in ("follow", "insert"):
            if len(v) <= 24:
                out[p] = v
        elif isinstance(v, dict) and not v.get("_asset"):
            scalars(v, p + ".", depth + 1, out)
    return out


def load(path):
    base = os.path.basename(path)
    return port.load_stock(path) if not os.path.dirname(os.path.abspath(path)).endswith(
        tuple(["mw2port_out"])) and base.startswith(("mp_", "common_mp")) and "stock" in path \
        else port.load_tree(path)[2]


def survey(root):
    """(value stats, listing stats, full names) of one file."""
    vals = collections.defaultdict(collections.Counter)
    seen = collections.Counter()
    listing = collections.defaultdict(collections.Counter)     # type -> {top, inline, named}
    names = collections.Counter()
    top_ids = set(id(e[1]) for e in root["assets"] if isinstance(e[1], dict))
    for o in port.iter_objects(root["assets"]):
        t = asset_type(o)
        if not t:
            continue
        n = name_of(o)
        if n.startswith(b","):
            listing[t]["named"] += 1
            continue
        listing[t]["top" if id(o) in top_ids else "inline"] += 1
        if n:
            names[(t, n.lower())] += 1
        seen[t] += 1
        for f, v in scalars(o).items():
            vals[(t, f)][v] += 1
    return vals, seen, listing, names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stock", required=True)
    ap.add_argument("--maps", type=int, default=0)
    ap.add_argument("converted", nargs="+")
    a = ap.parse_args()
    files = sorted(f for f in os.listdir(a.stock) if f.startswith("mp_") and f.endswith(".ff")
                   and not f.endswith("_load.ff"))
    if a.maps:
        files = files[:a.maps]
    vals = collections.defaultdict(collections.Counter)
    seen = collections.Counter()
    listing = collections.defaultdict(collections.Counter)
    dup_stock = 0
    common = set()
    cpath = os.path.join(a.stock, "common_mp.ff")
    if os.path.exists(cpath):
        for o in port.iter_objects(port.load_stock(cpath)["assets"]):
            t = asset_type(o)
            n = name_of(o) if t else b""
            if t and n and not n.startswith(b","):
                common.add((t, n.lower()))
    clash_stock = collections.Counter()     # type -> stock full assets under a common_mp name
    for f in files:
        v, s, l, n = survey(port.load_stock(os.path.join(a.stock, f)))
        for k in n:
            if k in common:
                clash_stock[k[0]] += 1
        for k, c in v.items():
            vals[k].update(c)
        seen.update(s)
        for k, c in l.items():
            listing[k].update(c)
        dup_stock += sum(1 for x in n.values() if x > 1)
    rules = {k: set(c) for k, c in vals.items() if seen[k[0]] >= MIN_SEEN and len(c) <= MAX_VALUES}
    print("mined from %d stock maps: %d value rules over %d asset types; listing per type:"
          % (len(files), len(rules), len(seen)))
    for t, c in sorted(listing.items()):
        print("   %-24s top %-6d inline %-6d named %d" % (t, c["top"], c["inline"], c["named"]))
    print("   stock files with two full assets of one type and name: %d" % dup_stock)
    print("   stock full copies of common_mp assets, by type: %s" % dict(clash_stock))
    for path in a.converted:
        root = port.load_tree(path)[2]
        v, s, l, n = survey(root)
        print("\n== %s" % path)
        broken = []
        for k, allowed in rules.items():
            if k in v:
                bad = {x: c for x, c in v[k].items() if x not in allowed}
                if bad:
                    broken.append(("value", "%s.%s" % k, sum(bad.values()),
                                   "ours %s / stock %s" % (sorted(map(str, bad))[:4], sorted(map(str, allowed)))))
        for t, c in l.items():
            st = listing.get(t)
            if not st:
                continue
            if c["inline"] and not st["inline"] and st["top"]:
                broken.append(("listing", t, c["inline"], "stock always lists it on its own (%d), ours has it inside other assets" % st["top"]))
            if c["top"] and not st["top"] and st["inline"]:
                broken.append(("listing", t, c["top"], "stock never lists it on its own (%d inside other assets)" % st["inline"]))
        dups = [k for k, x in n.items() if x > 1]
        if dups:
            broken.append(("names", "two full copies of one name", len(dups), str([(t, nm.decode("latin-1")) for t, nm in dups[:5]])))
        clash = [k for k in n if k in common and not clash_stock[k[0]]]
        if clash:
            broken.append(("names", "full copy of a common_mp asset", len(clash), str([(t, nm.decode("latin-1")) for t, nm in clash[:5]])))
        for kind, what, count, detail in sorted(broken, key=lambda b: (b[0], -b[2])):
            print("  %-8s %-55s x%-5d %s" % (kind, what, count, detail[:200]))
        if not broken:
            print("  no stock rule broken")


if __name__ == "__main__":
    main()
