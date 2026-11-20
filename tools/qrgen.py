#!/usr/bin/env python3
"""QR codes for the plotter, stdlib only: byte mode, error correction level M, versions 1 to 10 (up to 213 bytes).

  qrgen.py "https://example.org" --module 1.0     an SVG on stdout: each dark row of modules hatched with the pen

The layout follows the QR specification (ISO/IEC 18004): finder, timing and alignment patterns, format and version
information, Reed-Solomon error correction over GF(256), interleaved blocks, the zigzag data placement and the
mask with the lowest penalty. A plotted code needs a dark, crisp pen: the modules are filled with lines
`pen` mm apart, so a 0.5 mm pen on 1 mm modules gives solid squares.
"""
import argparse
import sys

# per version 1..10, error correction level M: (error correction codewords per block, number of blocks, raw codewords)
ECC_M = {1: (10, 1, 26), 2: (16, 1, 44), 3: (26, 1, 70), 4: (18, 2, 100), 5: (24, 2, 134), 6: (16, 4, 172),
         7: (18, 4, 196), 8: (22, 4, 242), 9: (22, 5, 292), 10: (26, 5, 346)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}


# ------------------------------------------------------------------ Reed-Solomon over GF(256), polynomial 0x11D
def _mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _divisor(degree):
    r = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            r[j] = _mul(r[j], root)
            if j + 1 < degree:
                r[j] ^= r[j + 1]
        root = _mul(root, 0x02)
    return r


def _remainder(data, divisor):
    r = [0] * len(divisor)
    for b in data:
        f = b ^ r.pop(0)
        r.append(0)
        for i, c in enumerate(divisor):
            r[i] ^= _mul(c, f)
    return r


# ------------------------------------------------------------------ encoding
def _codewords(data, version):
    ec, nblocks, raw = ECC_M[version]
    cap = raw - ec * nblocks
    bits = []

    def put(v, n):
        bits.extend((v >> i) & 1 for i in range(n - 1, -1, -1))
    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    if len(bits) > cap * 8:
        return None
    put(0, min(4, cap * 8 - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = [0xEC, 0x11]
    k = 0
    while len(bits) < cap * 8:
        put(pad[k % 2], 8)
        k += 1
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    # split into blocks (the short ones first), add each block's error correction, interleave
    short = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _divisor(ec)
    blocks, k = [], 0
    for i in range(nblocks):
        n = short_len - ec + (0 if i < short else 1)
        d = words[k:k + n]
        k += n
        blocks.append((d, _remainder(d, div)))
    out = []
    for i in range(short_len - ec + 1):
        for j, (d, _) in enumerate(blocks):
            if i < len(d):
                out.append(d[i])
    for i in range(ec):
        for _, e in blocks:
            out.append(e[i])
    return out


def _matrix(version, words):
    size = version * 4 + 17
    m = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        m[y][x] = dark
        fn[y][x] = True
    for i in range(size):                       # timing patterns
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):          # finders with their separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    d = max(abs(dx), abs(dy))
                    setf(x, y, d not in (2, 4))
    al = ALIGN[version]
    for i, ax in enumerate(al):                 # alignment patterns, not over the finders
        for j, ay in enumerate(al):
            if (i == 0 and j == 0) or (i == 0 and j == len(al) - 1) or (i == len(al) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    _format(m, fn, size, 0, setf)              # reserve the format areas (redrawn with the mask)
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = version << 12 | rem
        for i in range(18):
            bit = (bits >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            setf(a, b, bit)
            setf(b, a, bit)
    i = 0                                       # data in the zigzag, two columns at a time, from the bottom right
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = size - 1 - vert if up else vert
                if not fn[y][x] and i < len(words) * 8:
                    m[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2
    return m, fn, size, setf


def _format(m, fn, size, mask, setf):
    data = (0 << 3) | mask                      # level M is 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412
    bit = lambda i: (bits >> i) & 1 == 1       # noqa: E731
    for i in range(6):
        setf(8, i, bit(i))
    setf(8, 7, bit(6))
    setf(8, 8, bit(7))
    setf(7, 8, bit(8))
    for i in range(9, 15):
        setf(14 - i, 8, bit(i))
    for i in range(8):
        setf(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        setf(8, size - 15 + i, bit(i))
    setf(8, size - 8, True)                     # the dark module


MASKS = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
         lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
         lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
         lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]


def _penalty(m, size):
    p = 0
    for rows in (m, [list(c) for c in zip(*m)]):          # runs of five or more of one colour
        for r in rows:
            run = 1
            for a, b in zip(r, r[1:]):
                if a == b:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run = 1
            if run >= 5:
                p += run - 2
    for y in range(size - 1):                              # 2x2 blocks of one colour
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                p += 3
    dark = sum(map(sum, m))                                # balance of dark and light
    p += (abs(dark * 20 - size * size * 10) // (size * size)) * 10
    return p


def encode(text):
    """-> the module matrix (list of rows of bools), smallest version that fits."""
    data = text.encode("utf-8")
    for version in range(1, 11):
        words = _codewords(data, version)
        if words is not None:
            break
    else:
        raise ValueError("Too long for a QR code here: at most 213 bytes.")
    best = None
    for mk in range(8):
        m, fn, size, setf = _matrix(version, words)
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and MASKS[mk](x, y):
                    m[y][x] = not m[y][x]
        _format(m, fn, size, mk, setf)
        score = _penalty(m, size)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]


def svg(text, module=1.0, pen=0.4, quiet=2):
    """A plottable SVG (mm): each run of dark modules in a row is filled with back-and-forth lines pen mm apart."""
    m = encode(text)
    size = len(m)
    total = (size + 2 * quiet) * module
    lines = max(1, round(module / pen))
    paths = []
    for y, row in enumerate(m):
        x = 0
        while x < size:
            if not row[x]:
                x += 1
                continue
            x1 = x
            while x1 < size and row[x1]:
                x1 += 1
            ax, bx = (x + quiet) * module, (x1 + quiet) * module
            pts = []
            for k in range(lines):
                yy = (y + quiet) * module + (k + 0.5) * module / lines
                pts += [(ax, yy), (bx, yy)] if k % 2 == 0 else [(bx, yy), (ax, yy)]
            paths.append("M" + " L".join(f"{px:.3f} {py:.3f}" for px, py in pts))
            x = x1
    body = " ".join(paths)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total:.3f}mm" height="{total:.3f}mm" '
            f'viewBox="0 0 {total:.3f} {total:.3f}"><path d="{body}" fill="none" stroke="#000000" '
            f'stroke-width="{pen:.3f}" stroke-linecap="square"/></svg>'), size


# ------------------------------------------------------------------ Code 128 (set B: printable ASCII)
C128 = ("212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 "
        "113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
        "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 "
        "113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
        "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 "
        "241211 221114 413111 241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
        "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 211214 "
        "211232").split()
C128_STOP = "2331112"


def code128_bars(text):
    """-> bar widths in modules, starting with a bar (start B, data, checksum, stop)."""
    if not text or any(not 32 <= ord(c) <= 126 for c in text) or len(text) > 60:
        raise ValueError("A bar code here holds 1 to 60 plain characters (letters, digits, punctuation).")
    vals = [104] + [ord(c) - 32 for c in text]
    check = (104 + sum(i * v for i, v in enumerate(vals[1:], 1))) % 103
    widths = "".join(C128[v] for v in vals + [check]) + C128_STOP
    return [int(w) for w in widths]


def barcode_svg(text, module=0.4, height=12.0, pen=0.4, quiet=10):
    """A plottable Code 128 bar code (mm): each bar filled with vertical lines pen mm apart."""
    widths = code128_bars(text)
    x = quiet * module
    paths = []
    for i, w in enumerate(widths):
        bw = w * module
        if i % 2 == 0:                       # bars on even positions
            n = max(1, round(bw / pen))
            pts = []
            for k in range(n):
                xx = x + (k + 0.5) * bw / n
                pts += [(xx, 0.0), (xx, height)] if k % 2 == 0 else [(xx, height), (xx, 0.0)]
            paths.append("M" + " L".join(f"{px:.3f} {py:.3f}" for px, py in pts))
        x += bw
    total = x + quiet * module
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total:.3f}mm" height="{height:.3f}mm" '
            f'viewBox="0 0 {total:.3f} {height:.3f}"><path d="{" ".join(paths)}" fill="none" stroke="#000000" '
            f'stroke-width="{pen:.3f}"/></svg>'), len(widths)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--module", type=float, default=1.0, help="module size in mm")
    ap.add_argument("--pen", type=float, default=0.4, help="pen line width in mm (the fill spacing)")
    a = ap.parse_args(argv)
    s, _ = svg(a.text, a.module, a.pen)
    sys.stdout.write(s)


if __name__ == "__main__":
    main()
