"""PNG rendering of strokes (x-height units, y down from baseline), with Pillow."""
from PIL import Image, ImageDraw, ImageFont

PX = 48     # pixels per x-height, the capture page's default


def _font():
    for name in ("DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, 16)
        except OSError:
            pass
    return ImageFont.load_default()


def rows_png(rows, path, width_xh=None, label_w=0, px=PX):
    """rows: list of (label, strokes) or (label, strokes, guides). Each row shows baseline and x-height guides."""
    font = _font()
    pad = 0.6
    row_h = int(4.2 * px)
    w_xh = width_xh or max([2.0] + [max(float(s[:, 0].max()) for s in r[1]) for r in rows if r[1]]) + 2 * pad
    W = int(label_w + w_xh * px)
    im = Image.new("RGB", (W, row_h * len(rows)), "white")
    dr = ImageDraw.Draw(im)
    for i, r in enumerate(rows):
        label, strokes = r[0], r[1]
        y0 = i * row_h + int(2.6 * px)
        x0 = label_w + int(pad * px)
        dr.line([(label_w, y0), (W, y0)], fill=(200, 200, 230))
        dr.line([(label_w, y0 - px), (W, y0 - px)], fill=(230, 230, 240))
        dr.text((6, i * row_h + 6), label, fill=(120, 60, 60), font=font)
        for s in strokes:
            pts = [(x0 + float(x) * px, y0 + float(y) * px) for x, y in s]
            if len(pts) == 1:
                x, y = pts[0]
                dr.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill="black")
            else:
                dr.line(pts, fill="black", width=2, joint="curve")
        dr.line([(0, (i + 1) * row_h - 1), (W, (i + 1) * row_h - 1)], fill=(235, 235, 235))
    im.save(path)
    return path
