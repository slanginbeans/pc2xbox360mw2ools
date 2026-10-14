#!/usr/bin/env python3
"""Where is MW2 stuck? Read every thread's registers and call stack over XBDM.

Usage (on a PC on the same network as the console, Python 3, nothing to install):
    python xbdm_stacks.py 192.168.1.50

Run it while the console is frozen (a freeze raises no exception, so Watson shows nothing),
or right after Watson reports an exception (before restarting: XBDM holds the game stopped
at a first-chance exception). It stops the game's threads, lists the loaded modules (game,
system, plugins like codxe, with their addresses), reads each thread's instruction address,
link register and return addresses from its stack, and writes them to mw2_stacks.txt next
to this script. A thread stopped inside a plugin also gets that plugin's code saved
(<plugin>.bin, next to the script), so the crashing function can be named. Send the .txt and
any .bin files back. Nothing in memory is changed; the console stays stopped afterwards
(restart it as usual).
"""
import os
import socket
import struct
import sys

PORT = 730
DEPTH = 40
SCAN = 0x1000       # bytes of each thread's stack scanned for code addresses
AROUND = 0x100      # bytes of code saved on each side of a thread's instruction address
PLUGIN_MAX = 0x400000   # plugins (outside the game and the system) up to this size are saved


class Xbdm:
    def __init__(self, host):
        self.s = socket.create_connection((host, PORT), timeout=15)
        self.buf = b""
        banner = self.line()
        if not banner.startswith("201"):
            raise RuntimeError("unexpected greeting from XBDM: %r" % banner)

    def line(self):
        while b"\r\n" not in self.buf:
            data = self.s.recv(65536)
            if not data:
                raise RuntimeError("console closed the connection")
            self.buf += data
        ln, self.buf = self.buf.split(b"\r\n", 1)
        return ln.decode("latin-1")

    def cmd(self, text):
        """(status line, [lines of a multi-line answer])."""
        self.s.sendall((text + "\r\n").encode())
        status = self.line()
        lines = []
        if status.startswith("202"):
            while True:
                ln = self.line()
                if ln == ".":
                    break
                lines.append(ln)
        return status, lines

    def block(self, addr, length):
        """(bytes read from addr on, up to length: stops at the first unreadable byte,
        XBDM's status line for the first chunk)."""
        out, first = bytearray(), None
        while length > 0:
            n = min(0x400, length)
            status, lines = self.cmd("getmem addr=0x%08x length=0x%x" % (addr, n))
            first = first or status
            hexs = "".join(lines)
            if not status.startswith("2"):
                break
            cut = hexs.find("?")
            if cut >= 0:
                out += bytes.fromhex(hexs[:cut - cut % 2])
                break
            out += bytes.fromhex(hexs)
            addr += n
            length -= n
        return bytes(out), first

    def u32(self, addr):
        status, lines = self.cmd("getmem addr=0x%08x length=4" % addr)
        hexs = "".join(lines)
        if not status.startswith("2") or "?" in hexs or len(hexs) < 8:
            return None
        return struct.unpack(">I", bytes.fromhex(hexs[:8]))[0]


def fields(text):
    out = {}
    for part in text.replace("200- ", "").replace('"', "").split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.lower()] = v
    return out


def num(v):
    """0x1234 or XBDM's 64-bit 0q000000007a01f8f0 (the low 32 bits: pointers are 32-bit)."""
    try:
        if v.lower().startswith("0q"):
            return int(v[2:], 16) & 0xFFFFFFFF
        return int(v, 0)
    except (AttributeError, TypeError, ValueError):
        return None


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    x = Xbdm(sys.argv[1])
    out = []
    status, _ = x.cmd("stop")
    out.append("stop: %s" % status)
    status, lines = x.cmd("modules")
    mods = []
    out.append("modules: %s" % status)
    for ln in lines:
        f = fields(ln)
        base, size = num(f.get("base")), num(f.get("size"))
        if base is not None and size:
            mods.append((f.get("name", "?"), base, size))
            out.append("  %-24s 0x%08x..0x%08x" % (f.get("name", "?"), base, base + size))

    def module_of(addr):
        for m in mods:
            if addr is not None and m[1] <= addr < m[1] + m[2]:
                return m
        return None

    def where(addr):
        m = module_of(addr)
        return "%s+0x%x" % (m[0], addr - m[1]) if m else "?"

    saved = set()
    status, ids = x.cmd("threads")
    if not status.startswith("2"):
        sys.exit("XBDM didn't list threads: %s" % status)
    out.append("%d threads" % len(ids))
    for tid in ids:
        # (XBDM lists thread ids as signed decimals, -83886072; it takes them back in hex)
        tid = "0x%08x" % (int(tid.strip(), 0) & 0xFFFFFFFF)
        st, info = x.cmd("threadinfo thread=%s" % tid)
        raw_info = [st] + info
        info = fields(" ".join(raw_info))
        st, ctx = x.cmd("getcontext thread=%s control int" % tid)
        regs = {}
        for ln in [st] + ctx:
            regs.update(fields(ln))
        if "iar" not in regs:
            out.append("")
            out.append("thread %s: XBDM said %r / %r" % (tid, raw_info[:3], ([st] + ctx)[:3]))
            continue
        iar, lr, sp = num(regs.get("iar")), num(regs.get("lr")), num(regs.get("gpr1"))
        out.append("")
        out.append("thread %s  start=%s  priority=%s  suspend=%s" % (
            tid, info.get("start", "?"), info.get("priority", "?"), info.get("suspend", "?")))
        out.append("  iar=%s  lr=%s  sp=%s  ctr=%s" % (
            regs.get("iar", "?"), regs.get("lr", "?"), regs.get("gpr1", "?"), regs.get("ctr", "?")))
        out.append("  iar in %s, lr in %s" % (where(iar), where(lr)))
        out.append("  " + " ".join("r%d=%s" % (i, regs.get("gpr%d" % i, "?")) for i in range(3, 12)))
        if iar:
            data, status = x.block(iar - AROUND, 2 * AROUND)
            out.append("  code at iar-0x%x: %s" % (AROUND, data.hex()))
            m = module_of(iar)
            if m and not 0x82000000 <= m[1] < 0x84000000 and m[1] >= 0x90000000 \
                    and m[2] <= PLUGIN_MAX and m[0] not in saved:
                saved.add(m[0])
                data, status = x.block(m[1], m[2])
                name = os.path.basename(m[0]).replace(".", "_") + ".bin"
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "wb") as f:
                    f.write(data)
                out.append("  saved %s (0x%x bytes from 0x%08x, %s)" % (name, len(data), m[1], status))
                print("saved %s: send it with mw2_stacks.txt" % name)
        frames = []
        frame = sp
        for _ in range(DEPTH):
            if not frame:
                break
            back = x.u32(frame)
            if back == 0:
                back = x.u32(frame + 4)     # (a 64-bit back chain: its low word)
            if not back or back <= frame or back - frame > 0x100000:
                break
            ret = x.u32(back - 8)
            if ret:
                frames.append("0x%08x" % ret if module_of(ret) is None or 0x82000000 <= ret < 0x84000000
                              else "0x%08x(%s)" % (ret, where(ret)))
            frame = back
        out.append("  stack: " + " ".join(frames))
        # Whatever the frame layout: every word on the top of the stack that points into code
        # (the game at 0x82..., the kernel and system at 0x80... / 0x9...), in stack order.
        if sp:
            data, status = x.block(sp, SCAN)
            out.append("  stack read: %d bytes (%s)" % (len(data), status))
            out.append("  stack head: " + data[:64].hex())
            code = []
            for i in range(0, len(data) - 3, 4):
                v = struct.unpack_from(">I", data, i)[0]
                if 0x80000000 <= v < 0x84000000 or 0x90000000 <= v < 0x92000000:
                    code.append("+%x:%08x" % (i, v))
            out.append("  code words: " + " ".join(code))
        print("thread %s: iar=%s lr=%s" % (tid, regs.get("iar", "?"), regs.get("lr", "?")))
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mw2_stacks.txt")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print("done: send %s" % path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as e:
        sys.exit("error: %s" % e)
