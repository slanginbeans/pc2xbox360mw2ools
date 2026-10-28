"""Check converted 360 maps offline, in one go, and show what changed since the last good one.

    python check.py OUT.ff|FOLDER [...] [--stock DIR] [--baseline DIR [--accept]] [--emu|--no-emu]

Each file is read back once and checked the ways a converted map has failed on the console:

  read back   the zone reader reads the whole file (it stops where the game would);
  aliases     no pointer to data not loaded yet (forward) or into the temporary block (temp);
  emulator    (--emu, or whenever unicorn and the TU6 image are there) the game's own zone
              loader, run in an emulator (research/), reads the file to the same end, asset
              for asset and read for read, as the zone reader;
  pools       the map and the files loaded with it fit the game's asset pools (needs common_mp.ff
              and code_post_gfx_mp.ff in --stock DIR);
  placement   each kind of data loads into the memory block stock maps put it in, aligned as
              well (against up to PLACEMENT_MAPS stock maps in --stock DIR);
  stock rules the rules mined from the stock maps in --stock DIR (scripts/stock_rules.py);
  measures    the map's measures against the 16 stock 360 maps' highest.

Read back, aliases, emulator, pools and block placement fail a file (exit status 1); stock
rules and measures are reported for you to judge (some differences are known and harmless).

A summary of each file (results, measures and a hash of every asset and of each part of the
big ones) is written next to it as OUT.ff.check.json. With --baseline DIR, the summary is
compared with DIR/OUT.ff.check.json, the last good conversion of that map: it lists the assets
added, gone and changed (and which parts of the world, collision, ... changed), the measures
that moved and any check that got worse, so a change can be seen to touch only what it means
to. --accept makes this conversion the good one (copies its summary into DIR).

What is read from the stock files (mined rules, placement) is kept in DIR/mw2port_cache, so
only the first check is slow.
"""
import argparse
import hashlib
import json
import os
import pickle
import shutil
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", ".claude", "skills", "mw2-360-map-port", "scripts")
RESEARCH = os.path.join(HERE, "research")
sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS)

import mw2ff  # noqa: E402
import port  # noqa: E402
import tree  # noqa: E402
import zone  # noqa: E402
import check_aliases  # noqa: E402
import placement  # noqa: E402
import stock_rules  # noqa: E402

PLACEMENT_MAPS = 3      # stock maps the placement profile is taken from (each is read raw once)
SUMMARY = ".check.json"
CACHE_VERSION = 1       # change when what's cached from the stock files changes


# ------------------------------------------------------------ hashing a tree

class Hasher:
    """A short hash of each asset's own data: pointer values (which move with the layout) are
    left out, an asset held inside another counts as its type and name only (it is hashed on its
    own), and an alias counts as what it points at (an asset by name, other data by its hash)."""

    def __init__(self):
        self.memo = {}
        self.names = {}         # id(asset dict) -> its key

    def asset_key(self, o):
        return self.names.get(id(o)) or "%s %s" % (o.get("_asset"), _label(o))

    def h(self, o, top=None):
        i = id(o)
        if i in self.memo:
            return self.memo[i]
        if isinstance(o, dict) and "_asset" in o and o is not top:
            return ("asset " + self.asset_key(o)).encode()
        self.memo[i] = b"cycle"
        d = hashlib.blake2b(digest_size=8)
        u = d.update
        if isinstance(o, dict):
            u(b"{")
            for k in sorted(o, key=str):
                if k in ("@", "_um"):
                    continue
                u(str(k).encode() + b":" + self.v(o[k]))
            for k, c in sorted(o.get("@", {}).items(), key=lambda kv: str(kv[0])):
                u(b"@" + str(k).encode() + b":" + self.v(c))
        elif isinstance(o, list):
            u(b"[")
            for x in o:
                u(self.v(x))
        elif isinstance(o, tree.Leaf):
            u(b"L" + str(getattr(o.t, "name", o.t)).encode() + o.raw)
        else:
            u(repr(o).encode())
        r = self.memo[i] = d.digest()
        return r

    def v(self, x):
        if x is None or isinstance(x, (bool, int, float)):
            return repr(x).encode()
        if isinstance(x, str):
            return b"p" if x.startswith("0x") else x.encode()
        if isinstance(x, (bytes, bytearray)):
            return bytes(x)
        if isinstance(x, tree.Str):
            return b"s" + x.b
        if isinstance(x, tree.Ref):
            return b"r%d>" % x.rel + (self.v(x.target) if x.target is not None else b"?")
        if isinstance(x, tree.InsertSlot):
            return b"slot>" + self.v(x.asset)
        if isinstance(x, (tuple, set, frozenset)):
            return b"(" + b",".join(self.v(y) for y in x) + b")"
        return self.h(x)


def _label(o):
    n = port.alias_name(o) if o.get("_asset") == "snd_alias_list_t" else (port.asset_name(o) or port._name(o))
    return (n or b"?").decode("latin-1")


def fingerprint(root):
    """{asset key: {"h": hash, "f": {part: hash}}} of every asset in the tree (inline ones too).
    Keys are "<type> <name>", with " #n" on a second asset of one type and name."""
    hs = Hasher()
    assets, seen = [], Counter()
    for o in port.iter_objects(root["assets"]):
        if isinstance(o, dict) and "_asset" in o:
            k = "%s %s" % (o["_asset"], _label(o))
            seen[k] += 1
            if seen[k] > 1:
                k += " #%d" % seen[k]
            hs.names[id(o)] = k
            assets.append((k, o))
    out = {}
    for k, o in assets:
        e = {"h": hs.h(o, top=o).hex()}
        if len(o) > 12:     # the world, collision and other big ones: each part apart
            parts = {}
            for f, v in o.items():
                if f not in ("@", "_um"):
                    parts[f] = hs.v(v)
            for (f, idx), c in o.get("@", {}).items():
                parts[f] = parts.get(f, b"") + hs.v(c)
            e["f"] = {f: hashlib.blake2b(b, digest_size=8).hexdigest() for f, b in parts.items()}
        out[k] = e
    return out


# ------------------------------------------------------------ what's kept from the stock files

def _cache(stock_dir, what, files, make):
    """make(), kept in stock_dir/mw2port_cache as long as files and the reading code stay the same."""
    key = [CACHE_VERSION, port._stock_cache_tag()] + [port._file_key(f) for f in files]
    d = os.path.join(stock_dir, port.STOCK_CACHE_DIR)
    path = os.path.join(d, "check_%s.pickle" % what)
    try:
        with open(path, "rb") as fh:
            k, v = pickle.load(fh)
        if k == key:
            return v
    except Exception:
        pass
    v = make()
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump((key, v), fh, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError:
        pass
    return v


def stock_maps(stock_dir):
    return sorted(os.path.join(stock_dir, f) for f in os.listdir(stock_dir)
                  if f.startswith("mp_") and f.endswith(".ff") and not f.endswith("_load.ff"))


def mined_rules(stock_dir):
    files = stock_maps(stock_dir) + [p for p in [os.path.join(stock_dir, "common_mp.ff")] if os.path.exists(p)]
    return _cache(stock_dir, "rules", files, lambda: stock_rules.mine(stock_dir))


def stock_placement(stock_dir):
    files = stock_maps(stock_dir)[:PLACEMENT_MAPS]
    return _cache(stock_dir, "placement", files, lambda: placement.merge(placement.profile(f) for f in files))


def resident_refs(stock_dir):
    """[(path, tree)] of the always-loaded files in stock_dir, for port.check_pools."""
    out = []
    for n in port.RESIDENT:
        p = os.path.join(stock_dir, n + ".ff")
        if os.path.exists(p):
            out.append((p, port.load_stock(p)))
    return out


# ------------------------------------------------------------ the emulator

def emulator_ready():
    """Why the emulator check can't run, or None when it can."""
    try:
        import unicorn  # noqa: F401
        import capstone  # noqa: F401
    except ImportError:
        return "needs unicorn and capstone (python -m pip install unicorn capstone)"
    if RESEARCH not in sys.path:
        sys.path.insert(0, RESEARCH)
    for p in (os.environ.get("TU6_PE"), os.path.join(RESEARCH, "default_mp_tu6.pe")):
        if p and os.path.exists(p):
            return None
    return "needs the decrypted TU6 default_mp image (set TU6_PE to default_mp_tu6.pe)"


def emulator_check(path):
    """(ok, message) of the game's own zone loader reading the file against the zone reader's."""
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import cmp
        r, o, first_bad, err = cmp.compare(path, verbose=False)
    msgs = []
    if o.get("err"):
        msgs.append("the game's loader stopped: %s" % o["err"])
    if o["end"] != r.pos:
        msgs.append("the game's loader read to %d, the zone reader to %d" % (o["end"], r.pos))
    if len(o["assets"]) != len(r.assets):
        msgs.append("assets: game %d, zone reader %d" % (len(o["assets"]), len(r.assets)))
    if first_bad:
        msgs.append("first difference: %s" % (first_bad,))
    if err:
        msgs.append("zone reader: %s" % err)
    if msgs:
        return False, "; ".join(msgs)
    return True, "the game's loader reads the file as the zone reader does (%d assets, %d bytes)" % (
        len(r.assets), r.pos)


# ------------------------------------------------------------ one file

def check_file(path, stock_dir=None, emu=None, log=print):
    """The summary of one file (also written next to it as <path>.check.json)."""
    t0 = time.time()
    s = {"file": os.path.basename(path), "size": os.path.getsize(path), "fail": [], "checks": {}}

    def result(name, ok, msg, fatal=True):
        s["checks"][name] = {"ok": ok, "msg": msg}
        log("  %-12s %s  %s" % (name, "ok  " if ok else ("FAIL" if fatal else "note"), msg))
        if not ok and fatal:
            s["fail"].append(name)

    log("== %s" % path)
    counts, where, reads = Counter(), Counter(), []
    orig_alias, zone.Reader.alias = check_aliases.count_aliases(counts, where)
    orig_read, zone.Reader.read = placement.record_reads(reads)
    root = None
    try:
        ff, z = mw2ff.read_fastfile(path)
        if ff.platform != "xbox":
            raise port.PortError("not a 360 fastfile (%s)" % ff.platform)
        with port.no_gc():
            root, _ = tree.read_tree(z, mw2ff.schema_for(ff.platform))
        result("read back", True, "%d assets listed, %d bytes of zone" % (len(root["assets"]), len(z)))
    except Exception as ex:
        result("read back", False, "%s: %s" % (type(ex).__name__, ex))
    finally:
        zone.Reader.alias, zone.Reader.read = orig_alias, orig_read
    if root is None:
        return s
    bad = {k: n for k, n in counts.items() if k != "ok"}
    s["aliases"] = dict(counts)
    result("aliases", not bad, "%d good%s" % (counts["ok"], "".join(
        "; %d %s" % (n, k) for k, n in sorted(bad.items()))))
    for (kind, p), n in where.most_common(5):
        log("      %-8s %5d  %s" % (kind, n, p[-150:]))
    s["alias_paths"] = ["%s %d %s" % (k, n, p) for (k, p), n in where.most_common(50)]

    if emu is None:
        emu = emulator_ready() is None
    if emu:
        why = emulator_ready()
        if why:
            result("emulator", False, why)
        else:
            try:
                result("emulator", *emulator_check(path))
            except Exception as ex:
                result("emulator", False, "%s: %s" % (type(ex).__name__, ex))

    if stock_dir:
        refs = resident_refs(stock_dir)
        if len(refs) == len(port.RESIDENT):
            msgs, warns = [], []
            try:
                port.check_pools(root, refs, msgs.append, warns.append)
                result("pools", True, (msgs[-1].strip() if msgs else "") + "".join("; " + w for w in warns))
            except port.PortError as ex:
                result("pools", False, str(ex))
        else:
            log("  %-12s skip  needs common_mp.ff and code_post_gfx_mp.ff in %s" % ("pools", stock_dir))
        if stock_maps(stock_dir):
            types = [e[0] for e in root["assets"]]
            blocks, aligns, unseen = placement.compare(placement.summarize(reads, types),
                                                       stock_placement(stock_dir))
            s["placement"] = {"blocks": len(blocks), "aligns": len(aligns), "unseen": unseen}
            result("placement", not blocks and not aligns, "%d block differences, %d alignments below stock; "
                   "%d kinds of data the stock maps read never have" % (len(blocks), len(aligns), sum(unseen.values())))
            for (t, p), b, sb, n in blocks[:5]:
                log("      %-12s %s  ours %s, stock %s (x%d)" % (t, p[-110:], b, sb, n))
            for (t, p), a, sa, n in aligns[:5]:
                log("      %-12s %s  ours %d, stock at least %d (x%d)" % (t, p[-110:], a, sa, n))
            broken = stock_rules.check(root, mined_rules(stock_dir))
            s["stock_rules"] = ["%s | %s" % (k, w) for k, w, n, d in broken]
            s["stock_rule_counts"] = {"%s | %s" % (k, w): n for k, w, n, d in broken}
            result("stock rules", not broken, "%d broken" % len(broken) if broken else "none broken", fatal=False)
            for kind, what, n, detail in broken[:15]:
                log("      %-8s %-50s x%-5d %s" % (kind, what[:50], n, detail[:100]))
        else:
            log("  %-12s skip  no stock mp_*.ff in %s" % ("stock rules", stock_dir))
    reads = None

    m = port.map_measures(root)
    s["measures"] = m
    over = ["%s %s (stock highest %s, %s)" % (n, m[n], hi, who) for n, hi, med, who in port.STOCK_MAP_HIGHS
            if n in m and m[n] > hi]
    if m:
        result("measures", not over, "; ".join(over) if over else "all within the stock maps' highest", fatal=False)
    s["assets"] = fingerprint(root)
    s["types"] = dict(Counter(k.split(" ", 1)[0] for k in s["assets"]))
    s["seconds"] = round(time.time() - t0, 1)
    log("  %s (%d assets hashed, %.0f s)" % ("FAILED: " + ", ".join(s["fail"]) if s["fail"] else "passed",
                                             len(s["assets"]), s["seconds"]))
    with open(path + SUMMARY, "w") as fh:
        json.dump(s, fh, indent=0, sort_keys=True)
    return s


# ------------------------------------------------------------ against the last good one

def diff(old, new, log=print, limit=12):
    """Log what changed from summary old to summary new; returns the checks that got worse."""
    log("  against the last good %s:" % old["file"])
    worse = [c for c, r in new["checks"].items() if not r["ok"] and old["checks"].get(c, {}).get("ok", True)]
    better = [c for c, r in new["checks"].items() if r["ok"] and not old["checks"].get(c, {}).get("ok", True)]
    for c in worse:
        log("    WORSE    %s: %s" % (c, new["checks"][c]["msg"]))
    for c in better:
        log("    better   %s: %s" % (c, new["checks"][c]["msg"]))
    orules, nrules = old.get("stock_rule_counts", {}), new.get("stock_rule_counts", {})
    for r in sorted(set(nrules) - set(orules)):
        log("    new stock rule broken: %s (x%d)" % (r, nrules[r]))
    for r in sorted(set(orules) - set(nrules)):
        log("    stock rule no longer broken: %s" % r)
    for r in sorted(set(orules) & set(nrules)):
        if orules[r] != nrules[r]:
            log("    stock rule %s: x%d -> x%d" % (r, orules[r], nrules[r]))
    om, nm = old.get("measures", {}), new.get("measures", {})
    moved = ["%s %s -> %s" % (k, om.get(k), nm.get(k)) for k in sorted(set(om) | set(nm)) if om.get(k) != nm.get(k)]
    if moved:
        log("    measures: " + "; ".join(moved))
    oa, na = old.get("assets", {}), new.get("assets", {})
    added = sorted(set(na) - set(oa))
    gone = sorted(set(oa) - set(na))
    changed = sorted(k for k in set(na) & set(oa) if na[k]["h"] != oa[k]["h"])

    def show(what, keys):
        if not keys:
            return
        by = Counter(k.split(" ", 1)[0] for k in keys)
        log("    %s %d: %s" % (what, len(keys), ", ".join("%s %d" % kv for kv in by.most_common())))
        for k in keys[:limit]:
            parts = ""
            if what == "changed" and "f" in na[k] and "f" in oa[k]:
                fs = sorted(f for f in set(na[k]["f"]) | set(oa[k]["f"]) if na[k]["f"].get(f) != oa[k]["f"].get(f))
                parts = "  (%s)" % ", ".join(fs[:limit]) + (" ..." if len(fs) > limit else "")
            log("      %s%s" % (k, parts))
        if len(keys) > limit:
            log("      ... %d more" % (len(keys) - limit))
    show("added", added)
    show("gone", gone)
    show("changed", changed)
    if not (worse or moved or added or gone or changed or set(orules) ^ set(nrules)):
        log("    the same")
    return worse


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="converted 360 .ff files, or folders of them")
    ap.add_argument("--stock", help="folder with the stock 360 files (mp_*.ff, common_mp.ff, code_post_gfx_mp.ff)")
    ap.add_argument("--baseline", help="folder of the last good conversions' summaries to compare with")
    ap.add_argument("--accept", action="store_true", help="make these the good ones (copy their summaries to --baseline)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--emu", dest="emu", action="store_true", default=None, help="run the emulator check (fail if it can't)")
    g.add_argument("--no-emu", dest="emu", action="store_false", help="skip the emulator check")
    a = ap.parse_args()
    if a.accept and not a.baseline:
        ap.error("--accept needs --baseline")
    if a.emu is None and emulator_ready():
        print("(emulator check skipped: %s)" % emulator_ready())
    files = []
    for f in a.files:       # a folder: every .ff in it and below (mw2port_out)
        if os.path.isdir(f):
            files += sorted(os.path.join(top, n) for top, dirs, names in os.walk(f) for n in names
                            if n.endswith(".ff"))
        else:
            files.append(f)
    failed = 0
    for path in files:
        s = check_file(path, a.stock, a.emu)
        worse = []
        if a.baseline:
            b = os.path.join(a.baseline, os.path.basename(path) + SUMMARY)
            if os.path.exists(b):
                with open(b) as fh:
                    worse = diff(json.load(fh), s)
            else:
                print("  (no last good summary in %s yet: --accept to make this one it)" % a.baseline)
            if a.accept and not s["fail"]:
                os.makedirs(a.baseline, exist_ok=True)
                shutil.copyfile(path + SUMMARY, b)
                print("  accepted as the good one")
            elif a.accept:
                print("  not accepted: it failed")
        failed += bool(s["fail"] or worse)
    if len(files) > 1:
        print("\n%d of %d files passed" % (len(files) - failed, len(files)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
