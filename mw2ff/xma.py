"""XMA encoder for Xbox 360 MW2 (TU6) sounds: 16-bit PCM in, the 360's XMA packets out.

The 360 game plays XMA only (every one of the 4,149 sounds in the stock files is XMA). XMA is
WMA Pro's bitstream in 2,048-byte packets; this encoder writes the subset FFmpeg's decoder
(libavcodec/wmaprodec.c) reads and the stock files use:

- frames of 512 samples, each one subframe (a 1,024-point MDCT with a sine window);
- per-band scale factors (DPCM coded) and a quantization step, the coefficients run-level
  coded (no vector coding: the "number of vector coded coefficients" is sent as 0);
- the stock files' packet header (4-bit sequence number, the bits of the frame carried over
  from the packet before), first frame trimming 512 samples, no other trims.

Bit rate: bands far below the frame's loudest one get coarser steps. On the 73 sounds of PC
mp_showdown that stock 360 files also carry, this encodes to about 0.7 of the stock files' size
with more signal-to-noise against the PC original than the stock copies have.

Copyright (C) the pc2xbox360mw2ools authors. Derived from FFmpeg's WMA Pro decoder (Copyright
(c) 2007 Baptiste Coudurier, Benjamin Larsson, Ulion; (c) 2008 - 2011 Sascha Sommer, Benjamin
Larsson): free software under the GNU Lesser General Public License, version 2.1 or (at your
option) any later version, WITHOUT ANY WARRANTY; see LICENSE.LGPL next to this file.
"""
import math

import numpy as np

import xmatables as T

FRAME = 512                 # samples a frame
PACKET = 2048               # bytes a packet
HEADER_BITS = 32
PAYLOAD_BITS = PACKET * 8 - HEADER_BITS
LEAD = FRAME + 64           # samples before the sound: the stock files' alignment
QUALITY = 4.0               # band step = band level / QUALITY
MASK_DB = 30                # bands this far below the frame's loudest get coarser steps
FLOOR = 1e-4                # smallest step (silence)
GAIN = -32342.0             # MDCT scale (and sign) the decoder's inverse transform expects
TRANSFORM_BATCH = 1024      # frames transformed together (8 MB of samples a batch)


def _codes(lens, syms):
    """Huffman codes as FFmpeg's ff_vlc_init_from_lengths hands them out (table order)."""
    out, code = {}, 0
    for ln, sym in zip(lens, syms):
        if ln > 0:
            out[sym] = (code >> (32 - ln), ln)
            code += 1 << (32 - ln)
    return out


SF_CODES = _codes([ln for _, ln in T.SCALE_TABLE], [s - 60 for s, _ in T.SCALE_TABLE])
COEF_CODES = (_codes(T.COEF0_LENS, T.COEF0_SYMS),
              _codes([ln for _, ln in T.COEF1_TABLE], [s for s, _ in T.COEF1_TABLE]))
RUN_LEVEL = []              # per coefficient table: (run, level) -> symbol
for _run, _level in ((T.COEF0_RUN, T.COEF0_LEVEL), (T.COEF1_RUN, T.COEF1_LEVEL)):
    _m = {}
    for _sym in range(2, len(_run)):
        _m.setdefault((_run[_sym], int(_level[_sym])), _sym)
    RUN_LEVEL.append(_m)

_N = 2 * FRAME
WINDOW = np.sin(np.pi * (np.arange(_N) + 0.5) / _N)
MDCT = np.cos(np.pi / FRAME * (np.arange(_N)[None, :] + 0.5 + FRAME / 2)
              * (np.arange(FRAME)[:, None] + 0.5)) * GAIN


class _Bits:
    __slots__ = ("parts", "n")

    def __init__(self):
        self.parts, self.n = [], 0

    def put(self, val, n):
        if n:
            self.parts.append(format(val & ((1 << n) - 1), "0%db" % n))
            self.n += n

    def add(self, other):
        self.parts += other.parts
        self.n += other.n

    def __str__(self):
        return "".join(self.parts)


def band_offsets(rate):
    """Scale factor band edges of a 512-sample subframe, as the decoder works them out."""
    r = 48000 if rate > 44100 else 44100 if rate > 32000 else 32000 if rate > 24000 else 24000
    offs = [0]
    for f in T.CRITICAL_FREQ:
        o = ((FRAME * 2 * f) // r + 2) & ~3
        if o > offs[-1]:
            offs.append(o)
        if o >= FRAME:
            break
    offs[-1] = FRAME
    return offs


def _large(b, v):
    if v < 1 << 8:
        b.put(0, 1), b.put(v, 8)
    elif v < 1 << 16:
        b.put(2, 2), b.put(v, 16)
    elif v < 1 << 24:
        b.put(6, 3), b.put(v, 24)
    else:
        b.put(7, 3), b.put(v, 31)


def _coefficients(q, t):
    """Run-level coding of the integer coefficients q with coefficient table t."""
    b = _Bits()
    codes, rl = COEF_CODES[t], RUN_LEVEL[t]
    nz = np.flatnonzero(q)
    prev = -1
    for i in nz:
        x = int(q[i])
        run, lv = int(i) - prev - 1, abs(x)
        sym = rl.get((run, lv))
        if sym is not None:
            b.put(*codes[sym])
        else:
            b.put(*codes[0])            # escape: the level, then the run
            _large(b, lv)
            if run == 0:
                b.put(0, 1)
            elif run <= 4:
                b.put(2, 2), b.put(run - 1, 2)
            else:
                b.put(6, 3), b.put(run - 4, 9)
        b.put(1 if x > 0 else 0, 1)
        prev = int(i)
    if prev + 1 < len(q):
        b.put(*codes[1])                # end of block
    return b


def _transform(padded, nframes):
    """Each frame's 512 coefficients (the MDCT of its 1,024 windowed samples), as rows. Done in
    batches of frames, one large matrix product each: a product per frame made numpy's math
    library hand every small one to all the processor's threads, which crawled (20 seconds of
    sound took 16 seconds on 4 cores, 1.3 with one thread) and with more cores could look hung."""
    out = np.empty((nframes, FRAME))
    frames = np.lib.stride_tricks.as_strided(
        padded, (nframes, _N), (padded.strides[0] * FRAME, padded.strides[0]), writeable=False)
    for a in range(0, nframes, TRANSFORM_BATCH):
        out[a:a + TRANSFORM_BATCH] = (frames[a:a + TRANSFORM_BATCH] * WINDOW) @ MDCT.T
    return out


def _frame(X, offs, first, quality, mask):
    """One frame's bits (after its length, before its two end bits) for its 512 coefficients."""
    levels = [math.sqrt(float(np.mean(X[a:b] ** 2))) for a, b in zip(offs, offs[1:])]
    top = max(levels)
    d = [int(round(20 * math.log10(max(lv / quality, top * mask, FLOOR)))) for lv in levels]
    for i in range(1, len(d)):          # a scale factor may move by at most 60 a band
        d[i] = max(d[i - 1] - 60, min(d[i - 1] + 60, d[i]))
    Q = max(d)
    q = np.zeros(FRAME, dtype=np.int64)
    for (a, b), v in zip(zip(offs, offs[1:]), d):
        q[a:b] = np.round(X[a:b] / 10 ** (v / 20.0))
    f = _Bits()
    f.put(1, 1)                         # all channels share the subframe layout
    f.put(0, 1)                         # one 512-sample subframe
    f.put(0, 8)                         # dynamic range gain
    if first:
        f.put(1, 1), f.put(1, 1), f.put(512, 10), f.put(0, 1)   # trim 512 at the start
    else:
        f.put(0, 1)
    f.put(0, 1)                         # no extended subframe header
    f.put(0, 1)                         # reserved
    if not q.any():
        f.put(0, 1)                     # no coefficients
        return f
    f.put(1, 1)
    f.put(1, 1), f.put(0, 8)            # 0 vector coded coefficients: all run-level coded
    step = Q - 90
    if -31 <= step <= 30:
        f.put(step, 6)
    else:
        rest = 58 - Q if step < 0 else Q - 121
        f.put(-32 if step < 0 else 31, 6)
        while rest >= 31:
            f.put(31, 5)
            rest -= 31
        f.put(rest, 5)
    f.put(0, 2)                         # scale factor step 1 (dB)
    val = 45
    for v in d:
        s = v - Q + 60
        f.put(*SF_CODES[s - val])
        val = s
    c0, c1 = _coefficients(q, 0), _coefficients(q, 1)
    f.put(0 if c0.n <= c1.n else 1, 1)
    f.add(c0 if c0.n <= c1.n else c1)
    return f


class Encoded:
    """data: the packets; seek: samples before each packet; end_bit: where the last frame ends
    (packet headers counted); samples, rate, channels (1); ms: the sound's length."""

    def __init__(self, data, seek, end_bit, samples, rate):
        self.data, self.seek, self.end_bit = data, seek, end_bit
        self.samples, self.rate, self.channels = samples, rate, 1
        self.ms = samples * 1000 // rate


def encode(pcm, rate, channels=1, quality=QUALITY, mask_db=MASK_DB):
    """pcm: 16-bit samples (bytes, little-endian, interleaved, or an int16 array). Stereo is
    mixed to mono."""
    a = np.frombuffer(pcm, dtype="<i2") if isinstance(pcm, (bytes, bytearray, memoryview)) \
        else np.asarray(pcm, dtype=np.int16)
    x = a.astype(np.float64) / 32768.0
    if channels > 1:
        x = x[:len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    n = len(x)
    nframes = (LEAD + n + FRAME - 1) // FRAME + 1
    padded = np.concatenate([np.zeros(LEAD), x, np.zeros(nframes * FRAME + FRAME - LEAD - n)])
    offs = band_offsets(rate)
    mask = 10 ** (-mask_db / 20.0)
    X = _transform(padded, nframes)
    frames = [str(_frame(X[i], offs, i == 0, quality, mask)) for i in range(nframes)]
    return _pack(frames, n, rate)


def _pack(frames, samples, rate):
    """Frames to packets: each frame is its 15-bit length, its bits, a 1 and a trailer bit (1
    when the next frame ends in the same packet too)."""
    starts, pos = [], 0
    for s in frames:
        starts.append(pos)
        pos += 15 + len(s) + 2
    ends = starts[1:] + [pos]
    parts = []
    for i, s in enumerate(frames):
        more = i + 1 < len(frames) and (ends[i] - 1) // PAYLOAD_BITS == (ends[i + 1] - 1) // PAYLOAD_BITS
        parts.append(format(15 + len(s) + 2, "015b") + s + "1" + ("1" if more else "0"))
    stream = "".join(parts)
    npk = (len(stream) + PAYLOAD_BITS - 1) // PAYLOAD_BITS
    stream = stream.ljust(npk * PAYLOAD_BITS, "0")
    out, seek, k = bytearray(), [], 0
    for p in range(npk):
        a = p * PAYLOAD_BITS
        while k < len(starts) and starts[k] < a:
            k += 1
        seek.append(FRAME * k)
        prev = (starts[k] - a) if k < len(starts) and starts[k] < a + PAYLOAD_BITS else PAYLOAD_BITS
        hdr = ((p & 15) << 28) | (2 << 26) | (min(prev, 0x7FFF) << 11)
        out += hdr.to_bytes(4, "big")
        out += int(stream[a:a + PAYLOAD_BITS], 2).to_bytes(PAYLOAD_BITS // 8, "big")
    end_bit = pos + HEADER_BITS * ((pos - 1) // PAYLOAD_BITS + 1)
    return Encoded(bytes(out), seek, end_bit, samples, rate)
