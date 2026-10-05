#!/usr/bin/env python3
"""Dump the loaded map's culling data from a Jtag/RGH Xbox 360 running MW2 TU6, over XBDM.

Usage (on a PC on the same network as the console, Python 3, nothing to install):
    python xbdm_dump.py 192.168.1.50

The console must be running XBDM (xbdm.xex, usually loaded by DashLaunch) and be in a
match on the map to inspect. The script follows the game's own pointers and writes:
    world.bin    the GfxWorld record (0x2C8 bytes)
    trees.bin    one culling tree pointer per room
    counts.bin   the node count of each room's tree
    nodes0.bin   room 0's culling tree (40 bytes a node)
    insts.bin    every placed model's box record (36 bytes each)
    draws.bin    every placed model's draw record (44 bytes each)
    lists.bin    every room-0 node's model index list, back to back (see manifest.txt)
    manifest.txt the addresses everything was read from
into a folder named mw2_dump next to this script. Send the whole folder back.
Nothing is written to the console; the script only reads memory.
"""
import os
import socket
import struct
import sys

PORT = 730
WORLD_PTR = 0x83B3CA88      # TU6: pointer to the loaded GfxWorld
DPVS_PTR = 0x83B3CBE4       # TU6: pointer to the world's dpvs block (should be world + 0x220)
CHUNK = 0x400


class Xbdm:
    def __init__(self, host):
        self.s = socket.create_connection((host, PORT), timeout=10)
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

    def getmem(self, addr, length):
        out = bytearray()
        while length > 0:
            n = min(CHUNK, length)
            self.s.sendall(("getmem addr=0x%08x length=0x%x\r\n" % (addr, n)).encode())
            status = self.line()
            if not status.startswith("2"):
                raise RuntimeError("getmem 0x%08x failed: %s" % (addr, status))
            got = bytearray()
            while True:
                ln = self.line()
                if ln == ".":
                    break
                for i in range(0, len(ln) - 1, 2):
                    pair = ln[i:i + 2]
                    if pair == "??":
                        raise RuntimeError("memory at 0x%08x isn't readable (is the map loaded?)"
                                           % (addr + len(got)))
                    got.append(int(pair, 16))
            if len(got) != n:
                raise RuntimeError("asked for %d bytes at 0x%08x, got %d" % (n, addr, len(got)))
            out += got
            addr += n
            length -= n
        return bytes(out)

    def u32(self, addr):
        return struct.unpack(">I", self.getmem(addr, 4))[0]

    def string(self, addr, limit=128):
        raw = self.getmem(addr, limit)
        return raw.split(b"\0", 1)[0].decode("latin-1")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    x = Xbdm(sys.argv[1])
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mw2_dump")
    os.makedirs(out_dir, exist_ok=True)
    manifest = []

    def save(name, addr, data):
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(data)
        manifest.append("%-11s 0x%08x  %6d bytes" % (name, addr, len(data)))
        print("  saved %s (%d bytes from 0x%08x)" % (name, len(data), addr))

    W = x.u32(WORLD_PTR)
    if not W:
        sys.exit("No map loaded (world pointer is 0). Load the map, then run this again.")
    dpvs = x.u32(DPVS_PTR)
    name = x.string(x.u32(W))
    print("world at 0x%08x: %s" % (W, name))
    manifest.append("map %s" % name)
    manifest.append("world pointer 0x%08x -> 0x%08x" % (WORLD_PTR, W))
    manifest.append("dpvs pointer  0x%08x -> 0x%08x (%s)" % (
        DPVS_PTR, dpvs, "= world + 0x220, as expected" if dpvs == W + 0x220 else "NOT world + 0x220"))
    if dpvs != W + 0x220:
        print("  warning: the dpvs pointer isn't world + 0x220; dumping anyway")

    save("world.bin", W, x.getmem(W, 0x2C8))
    cells = x.u32(W + 0x34)
    trees = x.u32(W + 0x48)
    counts = x.u32(W + 0x44)
    print("rooms: %d" % cells)
    save("trees.bin", trees, x.getmem(trees, 4 * cells))
    save("counts.bin", counts, x.getmem(counts, 4 * cells))
    root0 = x.u32(trees)
    nodes = x.u32(counts)
    print("room 0 tree: %d nodes at 0x%08x" % (nodes, root0))
    node_data = x.getmem(root0, 40 * nodes)
    save("nodes0.bin", root0, node_data)

    smodels = x.u32(W + 0x220)
    print("placed models: %d" % smodels)
    insts = x.u32(W + 0x220 + 0x4C)
    draws = x.u32(W + 0x220 + 0x58)
    save("insts.bin", insts, x.getmem(insts, 36 * smodels))
    save("draws.bin", draws, x.getmem(draws, 0x2C * smodels))

    lists = bytearray()
    manifest.append("lists.bin: per node, node index, list address, count, offset in lists.bin")
    for i in range(nodes):
        count, ptr = struct.unpack_from(">HI", node_data, 40 * i + 0x1E)
        if count and ptr:
            manifest.append("  node %3d  0x%08x  %4d  %6d" % (i, ptr, count, len(lists)))
            lists += x.getmem(ptr, 2 * count)
    save("lists.bin", 0, bytes(lists))

    with open(os.path.join(out_dir, "manifest.txt"), "w") as f:
        f.write("\n".join(manifest) + "\n")
    print("done: send the folder %s" % out_dir)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as e:
        sys.exit("error: %s" % e)
