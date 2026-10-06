"""PC (IW4) animations (XAnimParts) to the Xbox 360 (TU6) layout.

Worked out from the 36 animations PC (IW4x) mp_derail, mp_estate, mp_highrise, mp_invasion and
mp_quarry share with the stock 360 files (fans, foliage sway, fence tarps, lockers, generators):
the 360 data this makes matches theirs value for value on 99.35% of 51,051 values, the rest by
the last unit (rounding of the source data, or of a float's last bit).

The data streams hold each bone's parts in part type order. Per bone:
- rotation (half: about one axis, two values; full: four), animated: dataShort its key count - 1
  ("size"), its frame indices, then its keys in randomDataShort;
- rotation, one pose (NO_SIZE): its values in dataShort;
- translation (small / full), animated: dataByte the bone, dataShort size, frame indices,
  dataInt its range (min[3], size[3]), keys in randomDataByte (small, 3 bytes) or
  randomDataShort (3 values);
- translation, one pose: dataByte the bone, dataInt x, y, z; no translation: dataByte the bone.
Frame indices: dataByte (fewer than 256 frames) or dataShort; with 256 frames or more and 64 keys
or more, the separate indices array, with a table of size / 256 + 2 entries in dataShort.

The 360 differs in:
- boneCount has 12 part types: two more (unused by every animation compared) at 3 and 6;
- a full rotation key is one 32-bit value in randomDataInt (one pose: dataInt): bit 31 the sign
  of the largest component, bits 30-29 3 minus its place (x y z w), then the other three going
  down from it (place - 1, - 2, - 3, wrapping) over the largest: x 511 rounded in 10 bits, x 511
  rounded in 10 bits, x 255 rounded up in 9 bits;
- a half rotation key (z, w) is one 16-bit value (in place of the two): bit 15 the sign of the
  larger, bit 14 set when z is the larger, the smaller over the larger x 8191 rounded in 14 bits.
Everything else (translations, frame indices, notifies) is the same, in big-endian.
"""
import math
import struct


class AnimError(Exception):
    pass


def pack_full(v):
    """One full rotation key (four values, any scale) as the 360's 32-bit value."""
    big_at = max(range(4), key=lambda k: abs(v[k]))
    big = float(v[big_at])
    if not big:
        raise AnimError("a rotation key is all zero")
    f = []
    for j, k in enumerate(((big_at - 1) % 4, (big_at - 2) % 4, (big_at - 3) % 4)):
        r = v[k] / big
        f.append(round(r * 511) if j < 2 else math.ceil(r * 255))
    return (((1 if big < 0 else 0) << 31) | ((3 - big_at) << 29) | ((f[0] & 0x3FF) << 19)
            | ((f[1] & 0x3FF) << 9) | (f[2] & 0x1FF))


def pack_half(z, w):
    """One half rotation key (z, w) as the 360's 16-bit value."""
    z_big = abs(z) > abs(w)
    big, small = (z, w) if z_big else (w, z)
    if not big:
        raise AnimError("a rotation key is all zero")
    return ((1 if big < 0 else 0) << 15) | ((1 if z_big else 0) << 14) | (round(small / big * 8191) & 0x3FFF)


def convert(bone_count, numframes, data_short, data_int, random_short):
    """The PC streams that change (lists of ints, as signed values) to the 360's.
    Returns (360 boneCount bytes, dataShort, dataInt, randomDataShort, randomDataInt), the
    lists as unsigned 16/32-bit values; raises AnimError when the streams don't add up."""
    if len(bone_count) != 10:
        raise AnimError("expected 10 part types, found %d" % len(bone_count))
    many = numframes >= 256
    pos = {"ds": 0, "di": 0, "rs": 0}
    src = {"ds": data_short, "di": data_int, "rs": random_short}
    ds, di, rs, ri = [], [], [], []

    def take(k, n):
        if pos[k] + n > len(src[k]):
            raise AnimError("the animation's data is shorter than its parts need")
        v = src[k][pos[k]:pos[k] + n]
        pos[k] += n
        return v

    def indices(size):
        if many and size >= 64:
            ds.extend(take("ds", (size >> 8) + 2))
        elif many:
            ds.extend(take("ds", size + 1))

    for t, count in enumerate(bone_count[:9]):
        for _ in range(count):
            if t in (1, 2):                 # rotation, animated
                size = take("ds", 1)[0]
                ds.append(size)
                indices(size)
                for _k in range(size + 1):
                    if t == 1:
                        rs.append(pack_half(*take("rs", 2)))
                    else:
                        ri.append(pack_full(take("rs", 4)))
            elif t == 3:                    # half rotation, one pose
                ds.append(pack_half(*take("ds", 2)))
            elif t == 4:                    # full rotation, one pose
                di.append(pack_full(take("ds", 4)))
            elif t in (5, 6):               # translation, animated
                size = take("ds", 1)[0]
                ds.append(size)
                indices(size)
                di.extend(take("di", 6))
                if t == 6:
                    rs.extend(take("rs", 3 * (size + 1)))
            elif t == 7:                    # translation, one pose
                di.extend(take("di", 3))
    for k in pos:
        if pos[k] != len(src[k]):
            raise AnimError("the animation's data is longer than its parts need")
    bc = bytes(bone_count[:3]) + b"\0" + bytes(bone_count[3:5]) + b"\0" + bytes(bone_count[5:])
    return (bc, [x & 0xFFFF for x in ds], [x & 0xFFFFFFFF for x in di],
            [x & 0xFFFF for x in rs], [x & 0xFFFFFFFF for x in ri])
