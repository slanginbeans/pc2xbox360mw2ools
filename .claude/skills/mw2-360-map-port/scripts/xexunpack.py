"""Unpack an Xbox 360 XEX2 executable to its memory image (loaded at 0x82000000), for the
disassembly tools. Handles unencrypted and AES-encrypted files (retail or development key)
with basic (uncompressed) blocks; LZX-compressed ones are refused. Usage:
    xexunpack.py default_mp.xex image.bin"""
import struct
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEYS = {"retail": bytes.fromhex("20B185A59D28FDC340583FBB0896BF91"), "devkit": bytes(16)}


def unpack(d):
    magic, flags, pe_off, _, sec_off, nhdr = struct.unpack(">4sIIIII", d[:24])
    if magic != b"XEX2":
        raise ValueError("not an XEX2 file")
    hdrs = dict(struct.unpack(">II", d[24 + 8 * i:32 + 8 * i]) for i in range(nhdr))
    ff = hdrs[0x3FF]
    size, enc, comp = struct.unpack(">IHH", d[ff:ff + 8])
    if comp != 1:
        raise ValueError("compression type %d (LZX) isn't supported" % comp)
    blocks = [struct.unpack(">II", d[ff + 8 + 8 * i:ff + 16 + 8 * i]) for i in range((size - 8) // 8)]
    body = d[pe_off:]
    tries = [None] if enc == 0 else list(KEYS.values())
    for key in tries:
        plain = body
        if key is not None:
            e = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            sess = e.update(d[sec_off + 0x150:sec_off + 0x160]) + e.finalize()
            c = Cipher(algorithms.AES(sess), modes.CBC(bytes(16))).decryptor()
            plain = c.update(body[:len(body) // 16 * 16]) + c.finalize()
        img, pos = bytearray(), 0
        for ds, zs in blocks:
            img += plain[pos:pos + ds] + bytes(zs)
            pos += ds
        if img[:2] == b"MZ":
            return bytes(img)
    raise ValueError("no key gave a valid image")


if __name__ == "__main__":
    img = unpack(open(sys.argv[1], "rb").read())
    open(sys.argv[2], "wb").write(img)
    print("%s: %d bytes" % (sys.argv[2], len(img)))
