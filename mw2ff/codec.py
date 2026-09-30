"""Turn the pieces of data a zone streams into editable values and back.

Every read the walk in zone.py makes is kept as a record with a descriptor that says
what the bytes are (a struct, an array of numbers, a string, a list of pointers). This
module decodes each record into plain Python/JSON values and encodes them back.

Encoding is exact: anything the field decoding does not reproduce by itself (struct
padding, unused bitfield bits, unusual float bit patterns) is kept as a small list of
byte fixes, so decode followed by encode always gives the original bytes.
"""

import math
import struct

from cdefs import PTR, Compound, Enum, Prim

FOLLOWING = 0xFFFFFFFF
INSERT = 0xFFFFFFFE

# Records bigger than this whose values are plain numbers (vertices, pixels, sound, index
# lists) are stored as binary blobs instead of JSON numbers.
BLOB_BYTES = 256


def ptr_value(v):
    if v == 0:
        return None
    if v == FOLLOWING:
        return "follow"
    if v == INSERT:
        return "insert"
    return "0x%08x" % v


def ptr_raw(v):
    if v is None:
        return 0
    if v == "follow":
        return FOLLOWING
    if v == "insert":
        return INSERT
    return int(v, 16)


def _is_bytes_type(t):
    return isinstance(t, Prim) and t.size == 1


def _float(raw):
    v = struct.unpack(">f", raw)[0]
    if math.isnan(v) or math.isinf(v) or struct.pack(">f", v) != raw:
        return {"f32": raw.hex()}
    return v


def _float_raw(v):
    if isinstance(v, dict):
        return bytes.fromhex(v["f32"])
    return struct.pack(">f", v)


class Codec:
    def __init__(self, schema):
        self.schema = schema
        self.types = dict(schema.defs.types)
        for info in schema.infos.values():
            self.types.setdefault(info.ctype.name, info.ctype)

    # ------------------------------------------------------------ type lookup

    def type_by_name(self, name):
        return self.types[name]

    # ------------------------------------------------------------ scalars

    def _decode_scalar(self, t, raw):
        if isinstance(t, Prim) and t.fmt == "f":
            return _float(raw)
        if isinstance(t, Prim) and t.fmt == "d":
            v = struct.unpack(">d", raw)[0]
            return {"f64": raw.hex()} if (math.isnan(v) or math.isinf(v)) else v
        return struct.unpack(">" + t.fmt, raw)[0]

    def _encode_scalar(self, t, v):
        if isinstance(t, Prim) and t.fmt == "f":
            return _float_raw(v)
        if isinstance(t, Prim) and t.fmt == "d":
            return bytes.fromhex(v["f64"]) if isinstance(v, dict) else struct.pack(">d", v)
        return struct.pack(">" + t.fmt, v)

    # ------------------------------------------------------------ members

    def _decode_array(self, t, dims, raw, off):
        """dims: array sizes (outermost first) of elements of type t starting at off."""
        if not dims:
            return self._decode_one(t, raw, off)
        if len(dims) == 1 and _is_bytes_type(t):
            return raw[off:off + dims[0]].hex()
        step = t.size
        for d in dims[1:]:
            step *= d
        return [self._decode_array(t, dims[1:], raw, off + i * step) for i in range(dims[0])]

    def _encode_array(self, t, dims, v, out, off):
        if not dims:
            return self._encode_one(t, v, out, off)
        if len(dims) == 1 and _is_bytes_type(t):
            b = bytes.fromhex(v)
            out[off:off + len(b)] = b
            return
        step = t.size
        for d in dims[1:]:
            step *= d
        for i in range(dims[0]):
            self._encode_array(t, dims[1:], v[i], out, off + i * step)

    def _decode_member(self, m, raw, off):
        mods = m.mods
        if mods and mods[0] == PTR:
            return ptr_value(struct.unpack_from(">I", raw, off + m.offset)[0])
        dims = []
        for x in mods:
            if x == PTR:
                break
            dims.append(x)
        if len(dims) < len(mods):
            # array of pointers
            n = 1
            for d in dims:
                n *= d
            return [ptr_value(struct.unpack_from(">I", raw, off + m.offset + 4 * i)[0]) for i in range(n)]
        if m.bits is not None:
            unit = struct.unpack_from(">" + {1: "B", 2: "H", 4: "I", 8: "Q"}[m.unit], raw, off + m.offset)[0]
            return (unit >> m.bitpos) & ((1 << m.bits) - 1)
        return self._decode_array(m.type, dims, raw, off + m.offset)

    def _encode_member(self, m, v, out, off):
        mods = m.mods
        if mods and mods[0] == PTR:
            struct.pack_into(">I", out, off + m.offset, ptr_raw(v))
            return
        dims = []
        for x in mods:
            if x == PTR:
                break
            dims.append(x)
        if len(dims) < len(mods):
            for i, p in enumerate(v):
                struct.pack_into(">I", out, off + m.offset + 4 * i, ptr_raw(p))
            return
        if m.bits is not None:
            fmt = ">" + {1: "B", 2: "H", 4: "I", 8: "Q"}[m.unit]
            unit = struct.unpack_from(fmt, out, off + m.offset)[0]
            mask = ((1 << m.bits) - 1) << m.bitpos
            unit = (unit & ~mask) | ((v << m.bitpos) & mask)
            struct.pack_into(fmt, out, off + m.offset, unit)
            return
        self._encode_array(m.type, dims, v, out, off + m.offset)

    # ------------------------------------------------------------ one element

    def _decode_one(self, t, raw, off, size=None):
        if isinstance(t, (Prim, Enum)):
            if not t.size:
                return None
            return self._decode_scalar(t, raw[off:off + t.size])
        if not isinstance(t, Compound):
            raise TypeError(t)
        if t.kind == "union":
            return {"union": raw[off:off + (size or t.size)].hex()}
        if t.kind == "typedef":
            return self._decode_member(t.members[0], raw, off)
        size = t.size if size is None else size
        out = {}
        for i, m in enumerate(t.members):
            if m.offset + m.size > size:
                break
            out[m.name or "_%d" % i] = self._decode_member(m, raw, off)
        return out

    def _encode_one(self, t, v, out, off, size=None):
        if isinstance(t, (Prim, Enum)):
            if t.size:
                out[off:off + t.size] = self._encode_scalar(t, v)
            return
        if t.kind == "union":
            b = bytes.fromhex(v["union"])
            out[off:off + len(b)] = b
            return
        if t.kind == "typedef":
            self._encode_member(t.members[0], v, out, off)
            return
        size = t.size if size is None else size
        for i, m in enumerate(t.members):
            if m.offset + m.size > size:
                break
            self._encode_member(m, v[m.name or "_%d" % i], out, off)

    # ------------------------------------------------------------ records

    def decode(self, desc, raw, blobs):
        """Returns a JSON-able dict for one streamed record. Large numeric data goes to blobs
        (a bytearray); the dict then points into it."""
        kind = desc[0]
        rec = {"k": kind}
        if kind == "string":
            try:
                rec["s"] = raw[:-1].decode("utf-8")
                if rec["s"].encode("utf-8") + b"\0" != raw:
                    raise UnicodeError
            except UnicodeError:
                rec.pop("s", None)
                rec["hex"] = raw[:-1].hex()
            return rec
        if kind == "ptrs":
            rec["v"] = [ptr_value(x) for x in struct.unpack(">%dI" % desc[1], raw)]
            return rec
        if kind == "u32":
            rec["v"] = list(struct.unpack(">%dI" % desc[1], raw))
            return rec
        if kind == "raw":
            return self._blob(rec, raw, blobs)
        t = desc[1]
        rec["t"] = t.name
        if kind == "partial":
            rec["n"] = len(raw)
            value = self._decode_one(t, raw, 0, len(raw))
            fixed = self._fixes(raw, lambda out: self._encode_one(t, value, out, 0, len(raw)))
            rec["v"] = value
            if fixed:
                rec["fix"] = fixed
            return rec
        n = desc[2]
        rec["n"] = n
        leafy = isinstance(t, (Prim, Enum)) or (isinstance(t, Compound) and t.kind == "union")
        if len(raw) > BLOB_BYTES and (leafy or n > 16):
            return self._blob(rec, raw, blobs)
        if isinstance(t, Prim) and t.size == 1:
            rec["hex"] = raw.hex()
            return rec
        values = [self._decode_one(t, raw, i * t.size) for i in range(n)] if t.size else []

        def enc(out):
            for i, v in enumerate(values):
                self._encode_one(t, v, out, i * t.size)
        fixed = self._fixes(raw, enc)
        rec["v"] = values[0] if n == 1 else values
        if fixed:
            rec["fix"] = fixed
        return rec

    @staticmethod
    def _blob(rec, raw, blobs):
        rec["blob"] = [len(blobs), len(raw)]
        blobs += raw
        return rec

    @staticmethod
    def _fixes(raw, encode):
        out = bytearray(len(raw))
        encode(out)
        if out == raw:
            return None
        fixes = []
        i = 0
        while i < len(raw):
            if out[i] != raw[i]:
                j = i
                while j < len(raw) and out[j] != raw[j]:
                    j += 1
                fixes.append([i, raw[i:j].hex()])
                i = j
            else:
                i += 1
        return fixes

    def encode(self, rec, blobs):
        kind = rec["k"]
        if "blob" in rec:
            off, n = rec["blob"]
            return bytes(blobs[off:off + n])
        if kind == "string":
            if "s" in rec:
                return rec["s"].encode("utf-8") + b"\0"
            return bytes.fromhex(rec["hex"]) + b"\0"
        if kind == "ptrs":
            return struct.pack(">%dI" % len(rec["v"]), *[ptr_raw(v) for v in rec["v"]])
        if kind == "u32":
            return struct.pack(">%dI" % len(rec["v"]), *rec["v"])
        t = self.type_by_name(rec["t"])
        if "hex" in rec:
            return bytes.fromhex(rec["hex"])
        if kind == "partial":
            out = bytearray(rec["n"])
            self._encode_one(t, rec["v"], out, 0, rec["n"])
        else:
            n = rec["n"]
            out = bytearray(t.size * n)
            values = [rec["v"]] if n == 1 else rec["v"]
            for i, v in enumerate(values):
                self._encode_one(t, v, out, i * t.size)
        for off, h in rec.get("fix", ()):
            b = bytes.fromhex(h)
            out[off:off + len(b)] = b
        return bytes(out)

