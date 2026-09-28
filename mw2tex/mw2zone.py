"""MW2 Xbox 360 (TU6) string tables: export them from a fastfile, and build codxe_patch_mp.ff with your own.

Needs only Python 3 and mw2tex.py in the same folder. Commands:

  python mw2zone.py tables code_post_gfx_mp.ff [OUTDIR]
      Writes every string table in the fastfile as a .csv under OUTDIR (default: tables),
      keeping the game's path, e.g. tables\\mp\\cardTitleTable.csv. Open them in Excel or
      Notepad and edit them.

  python mw2zone.py build TABLEDIR [OUT.ff]
      Packs every .csv under TABLEDIR into a new fastfile (default: codxe_patch_mp.ff). Each
      table is named by its path inside TABLEDIR, so tables\\mp\\cardTitleTable.csv becomes
      mp/cardTitleTable.csv and replaces the game's table with that name.
      Copy the result to _codxe\\zone\\codxe_patch_mp.ff on the console. codxe loads it right
      after patch_mp, so its tables win. Delete it to go back to the stock tables.

  python mw2zone.py check OUT.ff
      Reads a built file back and lists the tables in it.

Keep every row the same number of columns; the first row is data, not a header (the game's
tables have no header row). Save CSVs as plain comma-separated text.

How it works:
- A zone (the unpacked part of a fastfile) starts with its size, 0, and six memory block sizes,
  then the asset list: script string count and pointer, asset count and pointer, then one
  (type, pointer) pair per asset. Pointers of ffffffff mean "the data follows right here".
- String tables are asset type 35: name pointer, column count, row count, cell pointer, the
  name, one (string pointer, hash) pair per cell, then the cell strings. The hash is the
  lowercase string run through h = c + 31 * h (checked on every cell of two stock tables).
- Stock files point repeated strings back at earlier copies ((block << 28 | offset) + 1).
  Export follows those where it can; built files always store each string in place.
"""

import csv
import os
import re
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mw2tex  # noqa: E402

ASSET_STRINGTABLE = 35
INLINE = 0xFFFFFFFF
BLOCK_TEMP = 0
BLOCK_VIRTUAL = 3
BLOCK_COUNT = 6
# Header of stock patch_mp.ff (TU6): unsigned magic, version 0x10D, online flag, file time, region, 0 pak entries.
FF_HEAD = bytes.fromhex("49576666753130300000010d0101caecc16ebe9ef00000000100000000")


def string_hash(text):
    value = 0
    for c in text.lower().encode("latin1"):
        value = (c + 31 * value) & 0xFFFFFFFF
    return value


# ---------------------------------------------------------------- reading


TABLE_RE = re.compile(rb"\xff\xff\xff\xff(.{4})(.{4})\xff\xff\xff\xff([ -~]{1,120}?\.csv)\x00", re.S)


def _string_at(zone, pos):
    end = zone.index(b"\0", pos)
    return zone[pos:end].decode("latin1"), end + 1


def read_tables(zone):
    """Every string table stored in place in ZONE, as (name, rows) with rows a list of lists."""
    tables = []
    for m in TABLE_RE.finditer(zone):
        columns, rows = struct.unpack(">I", m.group(1))[0], struct.unpack(">I", m.group(2))[0]
        if not (0 < columns <= 256 and 0 < rows <= 100000):
            continue
        name = m.group(3).decode("latin1")
        cells_at = m.end()
        count = columns * rows
        if cells_at + count * 8 > len(zone):
            continue
        cells = [struct.unpack(">Ii", zone[cells_at + i * 8:cells_at + i * 8 + 8]) for i in range(count)]
        pos = cells_at + count * 8
        values, starts, pointers = [], [], []
        try:
            for ptr, cell_hash in cells:
                if ptr == INLINE:
                    starts.append(pos)
                    text, pos = _string_at(zone, pos)
                    if string_hash(text) != cell_hash & 0xFFFFFFFF:
                        raise ValueError("hash mismatch")
                    values.append(text)
                elif ptr == 0:
                    values.append("")
                else:
                    values.append(None)
                    pointers.append((len(values) - 1, ptr, cell_hash & 0xFFFFFFFF))
        except ValueError:
            continue
        unresolved = _resolve(zone, values, pointers, starts)
        grid = [values[r * columns:(r + 1) * columns] for r in range(rows)]
        tables.append({"name": name, "columns": columns, "rows": grid, "unresolved": unresolved})
    return tables


_hash_index = {}


def _strings_by_hash(zone):
    """Every printable string in ZONE by its hash (first copy wins), built once per zone."""
    key = id(zone)
    if key not in _hash_index:
        _hash_index.clear()
        index = {}
        for m in re.finditer(rb"(?<![\x20-\x7e])[\x20-\x7e]{1,255}(?=\x00)", zone):
            text = m.group().decode("latin1")
            index.setdefault(string_hash(text), text)
            if len(text) <= 64:  # a pointer can land inside a string and use its tail
                for i in range(1, len(text)):
                    index.setdefault(string_hash(text[i:]), text[i:])
        _hash_index[key] = index
    return _hash_index[key]


def _resolve(zone, values, pointers, starts):
    """Fills cells that point at an earlier copy of their string. Returns how many stayed unknown.

    A pointer holds a memory offset, not a file position; within one table the two differ by a
    fixed amount, found by trying the offsets that land on this table's own strings. Strings
    stored in earlier assets are found by their hash instead. Every candidate string is checked
    against the cell's hash, so a wrong guess is never used.
    """
    if not pointers:
        return 0
    from collections import Counter
    offsets = [(ptr - 1) & 0x0FFFFFFF for _, ptr, _ in pointers]
    votes = Counter()
    sample = sorted(set(offsets))
    sample = sample[:: max(1, len(sample) // 200)]
    for off in sample:
        for s in starts:
            if s >= off:
                votes[s - off] += 1
    deltas = [d for d, _ in votes.most_common(16)]
    unresolved = 0
    for (index, ptr, cell_hash), off in zip(pointers, offsets):
        found = None
        for d in deltas:
            pos = off + d
            if 0 < pos < len(zone) and zone[pos - 1] == 0:
                try:
                    text, _ = _string_at(zone, pos)
                except ValueError:
                    continue
                if string_hash(text) == cell_hash:
                    found = text
                    break
        if found is None and cell_hash == 0:
            found = ""  # the empty string hashes to 0
        if found is None:
            found = _strings_by_hash(zone).get(cell_hash)
        if found is None:
            unresolved += 1
            found = ""
        values[index] = found
    return unresolved


def cmd_tables(ff_path, out_dir="tables"):
    zone = mw2tex.FastFile(ff_path).zone
    tables = read_tables(zone)
    if not tables:
        sys.exit("no string tables found in %s" % ff_path)
    for table in tables:
        path = os.path.join(out_dir, *table["name"].split("/"))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="latin1") as fh:
            csv.writer(fh).writerows(table["rows"])
        note = "" if not table["unresolved"] else \
            " (%d cells couldn't be read and were left empty)" % table["unresolved"]
        print("%s: %d rows x %d columns%s" % (path, len(table["rows"]), table["columns"], note))


# ---------------------------------------------------------------- writing


def load_csv(path):
    with open(path, newline="", encoding="latin1") as fh:
        rows = [row for row in csv.reader(fh)]
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        raise ValueError("%s is empty" % path)
    columns = max(len(r) for r in rows)
    return [r + [""] * (columns - len(r)) for r in rows], columns


def table_asset(name, rows, columns):
    out = bytearray(struct.pack(">IIII", INLINE, columns, len(rows), INLINE))
    out += name.encode("latin1") + b"\0"
    cells = [value for row in rows for value in row]
    for value in cells:
        out += struct.pack(">II", INLINE, string_hash(value))
    for value in cells:
        out += value.encode("latin1") + b"\0"
    return out


def build_zone(tables):
    """Zone bytes holding TABLES, a list of (name, rows, columns)."""
    body = bytearray(struct.pack(">IIII", 0, 0, len(tables), INLINE))
    for _ in tables:
        body += struct.pack(">II", ASSET_STRINGTABLE, INLINE)
    for name, rows, columns in tables:
        body += table_asset(name, rows, columns)
    blocks = [0] * BLOCK_COUNT
    # The loader reads the asset list through the temp block and keeps tables in the virtual
    # block. Both are sized to hold everything; they are only upper bounds for this small file.
    blocks[BLOCK_TEMP] = 0x1000 + 16 + 8 * len(tables)
    blocks[BLOCK_VIRTUAL] = (len(body) + 0xFFF) & ~0xFFF
    return struct.pack(">II6I", len(body), 0, *blocks) + bytes(body)


def write_fastfile(path, zone):
    stream = zlib.compress(zone, 9)
    total = len(FF_HEAD) + 8 + len(stream)
    with open(path, "wb") as fh:
        fh.write(FF_HEAD + struct.pack(">II", total, total) + stream)


def cmd_build(table_dir, out_path="codxe_patch_mp.ff"):
    tables = []
    for root, _, files in os.walk(table_dir):
        for file_name in sorted(files):
            if not file_name.lower().endswith(".csv"):
                continue
            path = os.path.join(root, file_name)
            name = os.path.relpath(path, table_dir).replace(os.sep, "/")
            rows, columns = load_csv(path)
            tables.append((name, rows, columns))
            print("%s: %d rows x %d columns" % (name, len(rows), columns))
    if not tables:
        sys.exit("no .csv files under %s" % table_dir)
    write_fastfile(out_path, build_zone(tables))
    print("wrote %s; copy it to _codxe\\zone\\codxe_patch_mp.ff on the console" % out_path)


def cmd_check(ff_path):
    zone = mw2tex.FastFile(ff_path).zone
    size = struct.unpack(">I", zone[:4])[0]
    if size != len(zone) - 32:
        print("warning: zone size says %d but holds %d bytes" % (size, len(zone) - 32))
    for table in read_tables(zone):
        print("%s: %d rows x %d columns" % (table["name"], len(table["rows"]), table["columns"]))


def main():
    args = sys.argv[1:]
    if len(args) in (2, 3) and args[0] == "tables":
        cmd_tables(*args[1:])
    elif len(args) in (2, 3) and args[0] == "build":
        cmd_build(*args[1:])
    elif len(args) == 2 and args[0] == "check":
        cmd_check(args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
