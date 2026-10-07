#!/usr/bin/env python3
"""Where is MW2 stuck? Read every thread's registers and call stack over XBDM.

Usage (on a PC on the same network as the console, Python 3, nothing to install):
    python xbdm_stacks.py 192.168.1.50

Run it while the console is frozen (a freeze raises no exception, so Watson shows nothing).
It stops the game's threads (the game is stuck anyway), reads each thread's instruction
address, link register and up to 24 return addresses from its stack, and writes them to
mw2_stacks.txt next to this script. Send that file back. Nothing in memory is changed; the
console stays stopped afterwards (restart it as usual).
"""
import os
import socket
import struct
import sys

PORT = 730
DEPTH = 40


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

    def u32(self, addr):
        status, lines = self.cmd("getmem addr=0x%08x length=4" % addr)
        hexs = "".join(lines)
        if not status.startswith("2") or "?" in hexs or len(hexs) < 8:
            return None
        return struct.unpack(">I", bytes.fromhex(hexs[:8]))[0]


def fields(text):
    out = {}
    for part in text.replace("200- ", "").split():
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
        out.append("  " + " ".join("r%d=%s" % (i, regs.get("gpr%d" % i, "?")) for i in range(3, 12)))
        frames = []
        frame = sp
        for _ in range(DEPTH):
            if not frame:
                break
            back = x.u32(frame)
            if not back or back <= frame or back - frame > 0x100000:
                break
            ret = x.u32(back - 8)
            if ret:
                frames.append("0x%08x" % ret)
            frame = back
        out.append("  stack: " + " ".join(frames))
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
