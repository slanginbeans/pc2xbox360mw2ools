"""Load the zone-loading rules (OpenAssetTools ZoneCode command files) on top of
the parsed C definitions: which pointers are strings, how many elements an
array pointer has, which union member is live, which memory block data goes
to, and so on.

The rules mirror OpenAssetTools' ZoneCodeGenerator so the reader can follow the
exact order in which the game streams data out of a zone.
"""

import os
import re

import cdefs
from cdefs import PTR, Compound

HERE = os.path.dirname(os.path.abspath(__file__))
DEFS = os.path.join(HERE, "defs")


class Block:
    def __init__(self, kind, name, index, default):
        self.kind, self.name, self.index, self.default = kind, name, index, default


class Expr:
    """A compiled C expression over struct members and enum constants."""

    def __init__(self, text, root, schema):
        self.text = text
        self.refs = []
        self.static = None
        py = self._compile(text, root, schema)
        self.code = compile(py, "<%s>" % text, "eval")
        if not self.refs:
            self.static = int(eval(self.code, {"_r": None}))

    def _compile(self, text, root, schema):
        toks = re.findall(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*|0x[0-9A-Fa-f]+|\d+|&&|\|\||==|!=|<=|>=|<<|>>|[-+*/%()<>!&|^~\[\]]", text)
        return self._expr(toks, 0, len(toks), root, schema)

    def _expr(self, toks, i, end, root, schema):
        out = []
        while i < end:
            t = toks[i]
            if re.match(r"[A-Za-z_]", t):
                if t in schema.defs.consts:
                    out.append(str(schema.defs.consts[t]))
                elif t == "never":
                    out.append("0")
                elif t in schema.infos and i + 2 < end and toks[i + 1] == "-" and "::" in toks[i + 2]:
                    # 'Type - Owner::array': index of the current Type element inside that array
                    self.refs.append(("index", t))
                    out.append("_r(%d)" % (len(self.refs) - 1))
                    i += 3
                    continue
                else:
                    info, chain = schema.resolve(t, root)
                    ref = len(self.refs)
                    self.refs.append((info.name, [m.name for m in chain]))
                    idx = []
                    while i + 1 < end and toks[i + 1] == "[":
                        depth, j = 0, i + 1
                        while True:
                            depth += {"[": 1, "]": -1}.get(toks[j], 0)
                            if depth == 0:
                                break
                            j += 1
                        idx.append("(" + self._expr(toks, i + 2, j, root, schema) + ")")
                        i = j
                    out.append("_r(%d%s)" % (ref, "".join(", " + x for x in idx)))
            elif t == "&&":
                out.append(" and ")
            elif t == "||":
                out.append(" or ")
            elif t == "!":
                out.append(" not ")
            elif t == "/":
                out.append("//")
            else:
                out.append(t)
            i += 1
        return " ".join(out)

    def __call__(self, lookup):
        if self.static is not None:
            return self.static
        return int(eval(self.code, {"_r": lambda k, *idx: lookup(self.refs[k], idx)}))


class MemberInfo:
    def __init__(self, parent, member):
        self.parent = parent            # StructInfo
        self.m = member                 # cdefs.Member
        self.name = member.name
        self.is_string = False
        self.is_script_string = False
        self.is_reusable = False
        self.condition = None
        self.block = None
        self.alloc_align = None
        self.asset_ref = None
        self.block_cond = None          # (Expr, block if true, block if false)
        # per pointer modifier (index into mods): count Expr, or {combined_index: Expr}
        self.counts = {}
        self.counts_by_index = {}
        # per array modifier index: dynamic size / count Expr
        self.array_size = {}
        self.array_count = {}
        self.type = None                # StructInfo when the element type is a struct/union
        self.is_leaf = True

    def __repr__(self):
        return "<%s.%s>" % (self.parent.name, self.name)

    # pointer count helpers -----------------------------------------------
    def count_expr(self, mod_index, combined=0):
        byidx = self.counts_by_index.get(mod_index)
        if byidx and combined in byidx and byidx[combined] is not None:
            return byidx[combined]
        return self.counts.get(mod_index)

    @staticmethod
    def _is_array_count(e):
        return e is not None and (e.static is None or e.static > 1)

    def count_is_array(self, mod_index, combined=0):
        return self._is_array_count(self.count_expr(mod_index, combined))

    def any_count_is_array(self, mod_index):
        if self._is_array_count(self.counts.get(mod_index)):
            return True
        return any(self._is_array_count(e) for e in self.counts_by_index.get(mod_index, {}).values())

    def count_static_zero(self, mod_index):
        e = self.counts.get(mod_index)
        return e is not None and e.static == 0 and not self.counts_by_index.get(mod_index)


class StructInfo:
    def __init__(self, ctype):
        self.ctype = ctype
        self.name = ctype.name
        self.members = []               # declaration order
        self.ordered = []               # load order
        self.block = None
        self.asset = None               # asset enum name when this is an asset
        self.alloc_align = None
        self.is_leaf = True
        self.is_union = ctype.kind == "union"

    def __repr__(self):
        return "<info %s>" % self.name

    def member(self, name):
        for m in self.members:
            if m.name == name:
                return m
        return None


PLATFORM_DEFS = {"xbox": DEFS, "pc": os.path.join(HERE, "defs_pc")}
ENDIAN = {"xbox": ">", "pc": "<"}


class Schema:
    def __init__(self, header=None, commands=None, platform="xbox"):
        d = PLATFORM_DEFS[platform]
        header = header or os.path.join(d, "iw4_assets.h")
        commands = commands or os.path.join(d, "commands.txt")
        self.platform = platform
        self.endian = ENDIAN[platform]
        self.defs = cdefs.Defs(header)
        self.infos = {}
        self.blocks = []
        self.assets = {}                # asset enum name -> StructInfo
        self.anon = 0
        for t in list(self.defs.types.values()):
            if isinstance(t, Compound) and t.defined:
                self._info(t)
        self._parse_commands(commands)
        self._post_process()

    def _info(self, t):
        if id(t) in self.infos:
            return self.infos[id(t)]
        if t.name is None:
            self.anon += 1
            t.name = "__anon%d" % self.anon
        info = StructInfo(t)
        self.infos[id(t)] = info
        self.infos[t.name] = info
        for m in t.members:
            mi = MemberInfo(info, m)
            if isinstance(m.type, Compound) and m.type.defined:
                mi.type = self._info(m.type)
            info.members.append(mi)
        info.ordered = list(info.members)
        return info

    def get(self, name):
        return self.infos[name]

    # ------------------------------------------------------------ commands

    def resolve(self, path, root=None):
        """'Type::a::b' or 'a::b' (relative to root) -> (root StructInfo, [MemberInfo chain])."""
        parts = path.split("::")
        info = None
        if parts[0] in self.infos and not isinstance(parts[0], int):
            cand = self.infos[parts[0]]
            if root is None or root.member(parts[0]) is None:
                info = cand
                parts = parts[1:]
        if info is None:
            info = root
        if info is None:
            raise KeyError(path)
        chain = []
        cur = info
        for p in parts:
            m = self._find_member(cur, p)
            if m is None:
                raise KeyError("%s: no member %s in %s" % (path, p, cur.name))
            chain.extend(m)
            cur = m[-1].type
        return info, chain

    def _find_member(self, info, name):
        if info is None:
            return None
        m = info.member(name)
        if m is not None:
            return [m]
        for a in info.members:
            if a.name == "" and a.type is not None:
                sub = self._find_member(a.type, name)
                if sub is not None:
                    return [a] + sub
        return None

    def _parse_commands(self, path):
        text = self._read_with_includes(path)
        text = re.sub(r"//[^\n]*", "", text)
        stmts = [s.strip() for s in text.split(";")]
        use = None
        block_index = 0
        for s in stmts:
            if not s:
                continue
            w = s.split()
            if w[0] in ("game", "wordsize"):
                continue
            if w[0] == "asset":
                info = self.get(w[1])
                info.asset = w[2]
                self.assets[w[2]] = info
                continue
            if w[0] == "block":
                self.blocks.append(Block(w[1], w[2], block_index, len(w) > 3 and w[3] == "default"))
                block_index += 1
                continue
            if w[0] == "use":
                use = self.get(w[1])
                continue
            if w[0].startswith("reorder"):
                self._reorder(s, use)
                continue
            if w[0] != "set":
                raise SyntaxError("unknown command: %s" % s)
            cmd = w[1]
            rest = s.split(None, 2)[2] if len(w) > 2 else ""
            if cmd == "block":
                a = rest.split()
                if len(a) == 1:
                    use.block = self.block(a[0])
                else:
                    info, chain = self.resolve(a[0], use)
                    chain[-1].block = self.block(a[1])
            elif cmd in ("string", "scriptstring", "reusable"):
                info, chain = self.resolve(rest.strip(), use)
                setattr(chain[-1], {"string": "is_string", "scriptstring": "is_script_string",
                                    "reusable": "is_reusable"}[cmd], True)
            elif cmd == "allocalign":
                a = rest.split()
                info, chain = self.resolve(a[0], use)
                if chain:
                    chain[-1].alloc_align = int(a[1])
                else:
                    info.alloc_align = int(a[1])
            elif cmd == "assetref":
                a = rest.split()
                info, chain = self.resolve(a[0], use)
                chain[-1].asset_ref = a[1]
            elif cmd == "action":
                continue
            elif cmd == "condition":
                target, expr = rest.split(None, 1)
                info, chain = self.resolve(target, use)
                chain[-1].condition = Expr(expr, info, self)
            elif cmd == "count":
                self._count(rest, use)
            elif cmd == "arraysize":
                target, expr = rest.split(None, 1)
                info, chain = self.resolve(target, use)
                m = chain[-1]
                k = next(i for i, x in enumerate(m.m.mods) if x != PTR)
                m.array_size[k] = Expr(expr, info, self)
            elif cmd == "blockcond":
                target, rest2 = rest.split(None, 1)
                m = re.match(r"(.*)\s+(\w+)\s+(\w+)$", rest2.strip(), re.S)
                info, chain = self.resolve(target, use)
                chain[-1].block_cond = (Expr(m.group(1), info, self), self.block(m.group(2)), self.block(m.group(3)))
                chain[-1].block = self.block(m.group(3))
            elif cmd == "name":
                continue
            else:
                raise SyntaxError("unknown set command: %s" % s)

    def _read_with_includes(self, path):
        out = []
        for line in open(path, encoding="utf-8"):
            m = re.match(r'\s*#include\s+"([^"]+)"', line)
            if m:
                inc = os.path.join(os.path.dirname(path), m.group(1))
                if not os.path.exists(inc):
                    inc = os.path.join(os.path.dirname(path), "assets", os.path.basename(m.group(1)))
                out.append(self._read_with_includes(inc))
            else:
                out.append(line)
        return "".join(out)

    def block(self, name):
        for b in self.blocks:
            if b.name == name:
                return b
        raise KeyError(name)

    def _count(self, rest, use):
        m = re.match(r"(\**)\s*([\w:]+)((?:\s*\[[^\]]*\])*)\s+(.*)", rest, re.S)
        stars, target, idx, expr = m.groups()
        info, chain = self.resolve(target, use)
        member = chain[-1]
        resolve = len(stars)
        ptr_index = None
        for k, mod in enumerate(member.m.mods):
            if mod == PTR:
                if resolve:
                    resolve -= 1
                else:
                    ptr_index = k
                    break
        if ptr_index is None:
            raise SyntaxError("no pointer for count: %s" % rest)
        e = Expr(expr, info, self)
        indices = [x.strip() for x in re.findall(r"\[([^\]]*)\]", idx)]
        if not indices:
            member.counts[ptr_index] = e
            return
        sizes = [x for x in member.m.mods if x != PTR]
        depth = []
        cur = 1
        for i in range(len(sizes), 0, -1):
            if i < len(sizes):
                cur *= sizes[i]
            depth.append(cur)
        depth.reverse()
        combined = 0
        for d, ix in zip(depth, indices):
            v = int(ix) if re.match(r"\d", ix) else self.defs.consts[ix]
            combined += d * v
        member.counts_by_index.setdefault(ptr_index, {})[combined] = e

    def _reorder(self, s, use):
        head, body = s.split(":", 1)
        head = head.split()
        info = use
        if len(head) > 1:
            info, chain = self.resolve(head[1], None)
            if chain:
                info = chain[-1].type
        names = body.split()
        # "reorder: ... anchor a b": everything up to and including the anchor keeps its
        # place, then the named members, then the rest (OpenAssetTools SequenceReorder).
        anchor = None
        if names and names[0] == "...":
            anchor = names[1]
            names = names[2:]
        old = list(info.ordered)
        new = []
        for n in names:
            for m in old:
                if m.name == n:
                    new.append(m)
                    old.remove(m)
                    break
            else:
                raise SyntaxError("reorder: no member %s in %s" % (n, info.name))
        head = []
        while anchor is not None and old:
            m = old.pop(0)
            head.append(m)
            if m.name == anchor:
                break
        info.ordered = head + new + old

    # ------------------------------------------------------------ post processing

    def _post_process(self):
        infos = {id(i): i for i in self.infos.values()}.values()
        memo = {}

        def is_leaf(info, stack=()):
            if id(info) in memo:
                return memo[id(info)]
            if info in stack:
                return True
            leaf = True
            for m in info.ordered:
                if m.condition is not None and m.condition.static == 0:
                    continue
                if m.is_script_string or m.is_string:
                    leaf = False
                    break
                if any(mod == PTR and not m.count_static_zero(k) for k, mod in enumerate(m.m.mods)):
                    leaf = False
                    break
                if m.array_size:
                    leaf = False
                    break
                if m.type is not None and m.type is not info and not is_leaf(m.type, stack + (info,)):
                    leaf = False
                    break
            memo[id(info)] = leaf
            return leaf

        for info in infos:
            info.is_leaf = is_leaf(info)
        for info in infos:
            for m in info.ordered:
                m.is_leaf = not (m.is_string or m.is_script_string or (m.type is not None and not m.type.is_leaf)
                                 or any(mod == PTR and not m.count_static_zero(k) for k, mod in enumerate(m.m.mods))
                                 or bool(m.array_size))
        # unions: the single member without a condition is loaded last
        for info in infos:
            if not info.is_union:
                continue
            free = [m for m in info.ordered if m.condition is None and not m.is_leaf]
            if len(free) == 1:
                info.ordered.remove(free[0])
                info.ordered.append(free[0])

    # ------------------------------------------------------------ member classification

    def is_dynamic_member(self, m):
        if m.array_size:
            return True
        return PTR not in m.m.mods and m.type is not None and self.dynamic_member(m.type) is not None

    def dynamic_member(self, info):
        for m in info.ordered:
            if self.is_dynamic_member(m):
                return m
        return None

    def ignored(self, m):
        return m.condition is not None and m.condition.static == 0

    def after_partial_load(self, m):
        if self.is_dynamic_member(m):
            return True
        return m.parent.is_union and self.dynamic_member(m.parent) is not None

    def needs_treatment(self, m):
        if self.ignored(m):
            return False
        return m.is_string or PTR in m.m.mods or (m.type is not None and not m.type.is_leaf) or self.after_partial_load(m)

    def used_members(self, info):
        if info.is_union and self.dynamic_member(info) is not None:
            return [m for m in info.ordered if not self.ignored(m)]
        return [m for m in info.ordered if not m.is_leaf and not self.ignored(m)]


_schemas = {}


def load(platform="xbox"):
    """The definitions for the Xbox 360 (TU6) or the PC (version 276) game."""
    if platform not in _schemas:
        _schemas[platform] = Schema(platform=platform)
    return _schemas[platform]
