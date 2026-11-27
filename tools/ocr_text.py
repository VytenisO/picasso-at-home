"""Text boxes in an image, for the text fix-up of technical drawings. Runs in the OCR environment (PaddleOCR),
not the plotter's: the server starts it as `$PLOTTER_OCR_PYTHON tools/ocr_text.py IMAGE` and reads the JSON it
prints, {"boxes": [{"quad": [[x, y] x4], "text": str, "conf": 0..1}]} in image pixels.

Setup on the plotter host (own folder, the system Python untouched):
    python3 -m venv ~/picasso-ocr && ~/picasso-ocr/bin/pip install paddlepaddle paddleocr
    echo PLOTTER_OCR_PYTHON=$HOME/picasso-ocr/bin/python >> ~/.config/plotter-web.env
"""
import json
import os
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR
    from PIL import Image
    with Image.open(argv[0]) as im:
        side = max(im.size)
    # oneDNN in Paddle 3 fails on Windows ("ConvertPirAttribute2RuntimeAttribute"); elsewhere it is the fast path
    ocr = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=True,
                    text_det_limit_side_len=side, text_det_limit_type="max", enable_mkldnn=os.name != "nt")
    r = ocr.predict(argv[0])[0]
    boxes = [{"quad": [[round(float(x), 1), round(float(y), 1)] for x, y in poly], "text": str(t), "conf": round(float(c), 3)}
             for poly, t, c in zip(r["rec_polys"], r["rec_texts"], r["rec_scores"])]
    sys.stdout.write(json.dumps({"boxes": boxes}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
