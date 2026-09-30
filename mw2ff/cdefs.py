"""Parse the C struct definitions in defs/iw4_assets.h and lay them out the way
the Xbox 360 compiler does (32-bit pointers, MSVC alignment rules).

Only the subset of C that the definitions use is supported: structs, unions,
enums, typedefs, arrays, pointers, bitfields and the alignment macros from
OpenAssetTools' TypeAlignment.h.
"""

import re

PRIMS = {
    "char": (1, "b"), "signed char": (1, "b"), "unsigned char": (1, "B"),
    "bool": (1, "B"), "int8_t": (1, "b"), "uint8_t": (1, "B"),
    "short": (2, "h"), "unsigned short": (2, "H"), "int16_t": (2, "h"), "uint16_t": (2, "H"),
    "int": (4, "i"), "unsigned int": (4, "I"), "int32_t": (4, "i"), "uint32_t": (4, "I"),
    "long": (4, "i"), "unsigned long": (4, "I"), "unsigned": (4, "I"),
    "float": (4, "f"), "double": (8, "d"),
    "int64_t": (8, "q"), "uint64_t": (8, "Q"), "long long": (8, "q"), "unsigned long long": (8, "Q"),
    "void": (0, None),
}
POINTER_SIZE = 4

PTR = "*"


class Type:
    kind = None
    name = None
    size = 0
    align = 1

    def __repr__(self):
        return "<%s %s>" % (self.kind, self.name)


class Prim(Type):
    kind = "prim"

    def __init__(self, name, size, fmt):
        self.name, self.size, self.align, self.fmt = name, size, max(size, 1), fmt


class Enum(Type):
    kind = "enum"

    def __init__(self, name):
        self.name, self.size, self.align, self.fmt = name, 4, 4, "i"
        self.values = {}


class Compound(Type):
    def __init__(self, kind, name):
        self.kind, self.name = kind, name  # kind: struct | union
        self.members = []
        self.alignas = 0
        self.defined = False
        self.parent = None  # enclosing compound for anonymous/nested types


class Member:
    def __init__(self, name, type_, mods, bits=None, is_const=False):
        self.name = name
        self.type = type_          # element type after all modifiers
        self.mods = mods           # declarator modifiers, outermost first: "*" or int (array size)
        self.bits = bits
        self.is_const = is_const
        self.offset = 0
        self.bitpos = 0            # for bitfields: bit offset inside the storage unit (from LSB)
        self.unit = 0              # bitfield storage unit size
        self.size = 0
        self.align = 1

    def __repr__(self):
        return "<member %s %s %s @%d>" % (self.type.name, self.name, self.mods, self.offset)


class Defs:
    def __init__(self, path):
        self.types = {}
        self.consts = {}
        for n, (s, f) in PRIMS.items():
            self.types[n] = Prim(n, s, f)
        self.typedef_align = {}
        text = open(path, encoding="utf-8").read()
        self._parse(self._preprocess(text))
        for t in list(self.types.values()):
            if isinstance(t, Compound) and t.defined:
                self.layout(t)

    # ------------------------------------------------------------ lexing

    @staticmethod
    def _preprocess(text):
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        out, skip = [], []
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("#"):
                if s.startswith("#ifndef __zonecodegenerator"):
                    skip.append(True)
                elif s.startswith("#if"):
                    skip.append(False)
                elif s.startswith("#endif"):
                    skip.pop()
                continue
            if any(skip):
                continue
            out.append(line)
        text = "\n".join(out)
        text = re.sub(r"\b(gcc_align|gcc_align32|gcc_align64|type_align64|tdef_align64)\s*\(\s*\d+\s*\)", "", text)
        text = re.sub(r"\b(type_align|type_align32|tdef_align|tdef_align32)\s*\(\s*(\d+)\s*\)", r"alignas(\2)", text)
        return text

    def _tokens(self, text):
        return re.findall(r"[A-Za-z_]\w*|0x[0-9A-Fa-f]+|\d+\.?\d*[fF]?|::|<<|>>|[{}()\[\];,:=*&|+\-~<>/%^!?.]", text)

    # ------------------------------------------------------------ parsing

    def _parse(self, text):
        self.tok = self._tokens(text)
        self.i = 0
        while self.i < len(self.tok):
            self._top()

    def peek(self, k=0):
        j = self.i + k
        return self.tok[j] if j < len(self.tok) else None

    def next(self):
        t = self.tok[self.i]
        self.i += 1
        return t

    def expect(self, t):
        n = self.next()
        if n != t:
            raise SyntaxError("expected %r got %r near %r" % (t, n, self.tok[self.i - 8:self.i + 4]))

    def _top(self):
        t = self.peek()
        if t == ";":
            self.next()
        elif t in ("struct", "union"):
            self._compound_decl(None)
            self.expect(";")
        elif t == "enum":
            self._enum()
            self.expect(";")
        elif t == "typedef":
            self._typedef()
        elif t == "namespace":
            self.next(); self.next(); self.expect("{")
        elif t == "}":
            self.next()
        else:
            raise SyntaxError("unexpected %r near %r" % (t, self.tok[self.i:self.i + 10]))

    def _alignas(self):
        a = 0
        while self.peek() == "alignas":
            self.next(); self.expect("(")
            a = max(a, int(self.next(), 0))
            self.expect(")")
        return a

    def _compound_decl(self, parent):
        kind = self.next()
        align = self._alignas()
        name = None
        if self.peek() not in ("{",):
            name = self.next()
            align = max(align, self._alignas())
        if self.peek() != "{":
            # forward declaration or use as a type
            return self._get_compound(kind, name)
        c = self._get_compound(kind, name) if name else Compound(kind, None)
        c.alignas = max(c.alignas, align)
        c.parent = parent
        self.expect("{")
        while self.peek() != "}":
            self._member(c)
        self.expect("}")
        c.defined = True
        return c

    def _get_compound(self, kind, name):
        t = self.types.get(name)
        if t is None:
            t = Compound(kind, name)
            self.types[name] = t
        return t

    def _enum(self):
        self.expect("enum")
        name = self.next() if self.peek() != "{" else None
        under = None
        if self.peek() == ":":
            self.next()
            under, _ = self._base_type(None)
        e = Enum(name)
        if under is not None:
            e.size = e.align = under.size
            e.fmt = under.fmt
        if name:
            self.types[name] = e
        if self.peek() != "{":
            return e
        self.expect("{")
        v = -1
        while self.peek() != "}":
            n = self.next()
            if self.peek() == "=":
                self.next()
                expr = []
                while self.peek() not in (",", "}"):
                    expr.append(self.next())
                v = self.eval(expr)
            else:
                v += 1
            e.values[n] = v
            self.consts[n] = v
            if self.peek() == ",":
                self.next()
        self.expect("}")
        return e

    def _base_type(self, parent):
        is_const = False
        while self.peek() in ("const", "volatile", "struct", "union", "enum") and not (
                self.peek() in ("struct", "union") and self.peek(1) in ("{",) or
                self.peek() in ("struct", "union") and self.peek(1) == "alignas"):
            t = self.next()
            if t == "const":
                is_const = True
            elif t == "enum":
                n = self.next()
                return self.types.setdefault(n, Enum(n)), is_const
            elif t in ("struct", "union"):
                n = self.next()
                return self._get_compound(t, n), is_const
        if self.peek() in ("struct", "union"):
            return self._compound_decl(parent), is_const
        if self.peek() == "enum":
            return self._enum(), is_const
        words = []
        while True:
            t = self.peek()
            if t in ("unsigned", "signed", "long", "short", "int", "char") and (not words or " ".join(words) in (
                    "unsigned", "signed", "long", "unsigned long", "short", "unsigned short", "signed long", "long long")):
                words.append(self.next())
                continue
            if not words:
                words.append(self.next())
            break
        while self.peek() == "const":
            self.next()
            is_const = True
        name = " ".join(words)
        if name == "unsigned long long":
            name = "uint64_t"
        t = self.types.get(name)
        if t is None:
            raise SyntaxError("unknown type %r near %r" % (name, self.tok[self.i - 5:self.i + 5]))
        return t, is_const

    def _declarator(self):
        """Returns (name, mods) with mods outermost first."""
        ptrs = 0
        while self.peek() in ("*", "const"):
            if self.next() == "*":
                ptrs += 1
        inner = None
        if self.peek() == "(":
            self.next()
            inner = self._declarator()
            self.expect(")")
            name = inner[0]
        else:
            name = self.next() if self.peek() not in (";", ",", ":", "[") else None
        arrays = []
        while self.peek() == "[":
            self.next()
            expr = []
            while self.peek() != "]":
                expr.append(self.next())
            self.expect("]")
            arrays.append(self.eval(expr))
        # C declarator: arrays bind tighter than pointers.
        mods_here = arrays + [PTR] * ptrs
        if inner:
            return name, inner[1] + mods_here
        return name, mods_here

    def _member(self, c):
        if self.peek() in ("struct", "union") and (self.peek(1) == "{" or self.peek(1) == "alignas"):
            sub = self._compound_decl(c)
            if self.peek() == ";":
                self.next()
                c.members.append(Member("", sub, []))
                return
            base, is_const = sub, False
        else:
            base, is_const = self._base_type(c)
        while True:
            self._alignas()
            name, mods = self._declarator()
            bits = None
            if self.peek() == ":":
                self.next()
                bits = int(self.next(), 0)
            c.members.append(Member(name or "", base, mods, bits, is_const))
            if self.peek() == ",":
                self.next()
                continue
            break
        self.expect(";")

    def _typedef(self):
        self.expect("typedef")
        align = self._alignas()
        base, is_const = self._base_type(None)
        align = max(align, self._alignas())
        name, mods = self._declarator()
        self.expect(";")
        if not mods and not align:
            self.types[name] = base
            return
        # typedef with arrays or alignment: model as a transparent wrapper
        t = Compound("typedef", name)
        t.members.append(Member("", base, mods, None, is_const))
        t.alignas = align
        t.defined = True
        self.types[name] = t

    def eval(self, toks):
        s = " ".join(toks)
        s = re.sub(r"\b(\d+)[fF]\b", r"\1", s)

        def rep(m):
            w = m.group(0)
            if w in self.consts:
                return str(self.consts[w])
            if w == "sizeof":
                return w
            raise SyntaxError("unknown constant %r" % w)
        s = re.sub(r"\b[A-Za-z_]\w*\b", rep, s)
        return int(eval(s))

    # ------------------------------------------------------------ layout

    def member_layout(self, m):
        """Size and alignment of a member given its modifiers."""
        if m.mods and m.mods[0] == PTR:
            return POINTER_SIZE, POINTER_SIZE
        n = 1
        k = 0
        while k < len(m.mods) and m.mods[k] != PTR:
            n *= m.mods[k]
            k += 1
        if k < len(m.mods):
            return n * POINTER_SIZE, POINTER_SIZE
        t = m.type
        if isinstance(t, Compound) and not getattr(t, "laid_out", False):
            self.layout(t)
        return n * t.size, t.align

    def layout(self, c):
        if getattr(c, "laid_out", False):
            return
        c.laid_out = True
        off = 0
        size = 0
        align = 1
        unit_off = unit_size = unit_used = None
        for m in c.members:
            msize, malign = self.member_layout(m)
            m.size, m.align = msize, malign
            align = max(align, malign)
            if c.kind == "union":
                m.offset = 0
                if m.bits is not None:
                    m.unit, m.bitpos = msize, 0
                size = max(size, msize)
                continue
            if m.bits is not None:
                if unit_size == msize and unit_used + m.bits <= msize * 8:
                    m.offset, m.unit, m.bitpos = unit_off, msize, unit_used
                    unit_used += m.bits
                    continue
                off = (off + malign - 1) // malign * malign
                unit_off, unit_size, unit_used = off, msize, m.bits
                m.offset, m.unit, m.bitpos = off, msize, 0
                off += msize
                continue
            unit_size = None
            off = (off + malign - 1) // malign * malign
            m.offset = off
            off += msize
        if c.kind != "union":
            size = off
        align = max(align, c.alignas or 1)
        c.align = align
        if c.kind == "typedef":
            # An over-aligned typedef only changes where the data starts; the game streams
            # arrays of it with the unpadded element size.
            c.size = size
        else:
            c.size = (size + align - 1) // align * align
