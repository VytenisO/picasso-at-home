#!/usr/bin/env python3
"""Inkscape extension (Extensions, Export, Send to the pen plotter): uploads the current drawing, or only the
selection, to the plotter's library, and optionally puts it on the bed. The drawing is not changed.

Install: copy send_to_plotter.inx and send_to_plotter.py into Inkscape's user extensions folder
(Edit, Preferences, System: User extensions) and restart Inkscape. The upload part needs only the standard
library, so it can be used from the command line too:

    python send_to_plotter.py --server http://robotlab:8080 --standalone drawing.svg
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def upload(server, svg_bytes, name, password=None, bed=False):
    """-> the library entry. With bed=True the drawing becomes the only object on the bed, at the canvas corner."""
    base = server.rstrip("/")
    cookie = {}

    def call(method, path, body=None, raw=None, headers=None):
        h = dict(headers or {})
        if raw is None and body is not None:
            h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = cookie["c"]
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.headers.get("Set-Cookie"):
                    cookie["c"] = r.headers["Set-Cookie"].split(";", 1)[0]
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as ex:
            try:
                msg = json.loads(ex.read()).get("error")
            except ValueError:
                msg = None
            raise RuntimeError(f"The plotter said: {msg or ex.reason} ({ex.code})")
        except urllib.error.URLError as ex:
            raise RuntimeError(f"Cannot reach the plotter at {base}: {ex.reason}")

    if password:
        call("POST", "/api/login", {"password": password})
    fname = (name or "inkscape").strip()
    fname = fname if fname.lower().endswith(".svg") else fname + ".svg"
    f = call("POST", "/api/files/upload", raw=svg_bytes,
             headers={"X-Filename": urllib.parse.quote(fname), "X-Params": "%7B%7D"})["file"]
    if bed:
        g = call("GET", f"/api/files/natural?id={urllib.parse.quote(f['id'])}")["geom"]
        canvas = call("GET", "/api/me")["machine"]["canvas"]
        obj = {"id": "ink1", "file": f["id"], "x": canvas[0], "y": canvas[1], "w": g["raw_w"], "h": g["raw_h"],
               "rot": 0, "fx": False, "fy": False, "g": g.get("svg_hash", "")}
        call("POST", "/api/doc", {"objects": [obj], "settings": {}, "name": f["name"]})
    return f


def _standalone(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--password")
    ap.add_argument("--bed", action="store_true")
    ap.add_argument("--standalone", required=True, metavar="SVG")
    a = ap.parse_args(argv)
    with open(a.standalone, "rb") as fh:
        f = upload(a.server, fh.read(), a.standalone.replace("\\", "/").rsplit("/", 1)[-1], a.password, a.bed)
    print(f"Sent: {f['name']}")


def _inkscape():
    import copy

    import inkex

    class Send(inkex.EffectExtension):
        def add_arguments(self, pars):
            pars.add_argument("--server", default="http://robotlab:8080")
            pars.add_argument("--name", default="")
            pars.add_argument("--selection", type=inkex.Boolean, default=False)
            pars.add_argument("--bed", type=inkex.Boolean, default=False)
            pars.add_argument("--password", default="")

        def effect(self):
            doc = self.document
            if self.options.selection:
                if not self.svg.selected:
                    raise inkex.AbortExtension("Nothing is selected.")
                doc = copy.deepcopy(self.document)
                keep = {el.get("id") for el in self.svg.selected.values()}
                root = doc.getroot()
                for el in list(root.iter()):
                    eid = el.get("id")
                    # drop drawable elements outside the selection; keep their groups (layers) and defs
                    if eid and eid not in keep and el.tag.split("}")[-1] in ("path", "rect", "circle", "ellipse",
                                                                             "line", "polyline", "polygon", "text",
                                                                             "image", "use") \
                            and not any(a.get("id") in keep for a in el.iterancestors()):
                        el.getparent().remove(el)
            raw = inkex.etree.tostring(doc, xml_declaration=True, encoding="utf-8")
            name = self.options.name or (self.svg.get("sodipodi:docname") or "inkscape").rsplit(".", 1)[0]
            try:
                f = upload(self.options.server, raw, name, self.options.password or None, self.options.bed)
            except RuntimeError as ex:
                raise inkex.AbortExtension(str(ex))
            self.msg(f"Sent to the plotter as {f['name']}" + (" and put on the bed." if self.options.bed else "."))

        def save(self, stream):          # the drawing itself is not changed
            pass

    Send().run()


if __name__ == "__main__":
    if "--standalone" in sys.argv:
        _standalone(sys.argv[1:])
    else:
        _inkscape()
