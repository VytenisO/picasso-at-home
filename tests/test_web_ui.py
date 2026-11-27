"""Static checks of the web page: every element the scripts look up exists in index.html (or is built by a script),
and the page keeps to its content security policy. Moving controls around the page must not break their wiring.

    python -m unittest discover -s tests -p test_web_ui.py -v
"""
import pathlib
import re
import unittest

WEB = pathlib.Path(__file__).resolve().parent.parent / "tools" / "web"


def page():
    return (WEB / "index.html").read_text(encoding="utf-8")


def scripts():
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((WEB / "js").glob("*.js"))}


def html_ids(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


def built_ids(js):
    """Ids a script gives to elements it creates."""
    out = set()
    for t in js.values():
        out |= set(re.findall(r"\bid:\s*'([A-Za-z][\w-]*)'", t))
        out |= set(re.findall(r"\.id\s*=\s*'([A-Za-z][\w-]*)'", t))
    return out


class WebIds(unittest.TestCase):
    def setUp(self):
        self.html, self.js = page(), scripts()
        self.ids = html_ids(self.html) | built_ids(self.js)

    def test_ids_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        dup = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dup, [], "duplicate ids in index.html")

    def test_lookups_exist(self):
        missing = []
        for name, t in self.js.items():
            for m in re.finditer(r"(?:\$|getElementById)\(\s*'([A-Za-z][\w-]*)'\s*\)", t):
                if m.group(1) not in self.ids:
                    missing.append(f"{name}: {m.group(1)}")
        self.assertEqual(missing, [])

    def test_selectors_exist(self):
        """'#id ...' selectors in scripts: the id exists, and a [data-act=x] inside it exists in the page."""
        missing = []
        for name, t in self.js.items():
            for m in re.finditer(r"['`](#[A-Za-z][\w-]*)([^'`\n]*)['`]", t):
                sid, rest = m.group(1)[1:], m.group(2)
                if re.fullmatch(r"[0-9a-fA-F]{3,8}", sid):          # a colour, not a selector
                    continue
                if "${" in m.group(0):
                    continue
                if sid not in self.ids:
                    missing.append(f"{name}: {m.group(0)}")
                for act in re.findall(r"\[data-act=([\w-]+)\]", rest):
                    if f'data-act="{act}"' not in self.html:
                        missing.append(f"{name}: data-act {act}")
        self.assertEqual(missing, [])

    def test_tab_panels(self):
        """Every tab names a panel that exists and points back at it; no tab button can submit a form."""
        tabs = re.findall(r'<button type="button" role="tab" id="tabBtn-([\w-]+)" aria-controls="([\w-]+)"', self.html)
        self.assertGreater(len(tabs), 10)
        for name, panel in tabs:
            self.assertEqual(panel, f"tab-{name}")
            self.assertIn(f'id="{panel}" role="tabpanel"', self.html)
        # a tab inside a form must not submit it
        self.assertEqual(re.findall(r'<button role="tab"', self.html), [])

    def test_no_inline_styles(self):
        """The server sends style-src 'self': inline style attributes are blocked."""
        self.assertEqual(re.findall(r'<[^>]+\sstyle="[^"]*"', self.html), [])


if __name__ == "__main__":
    unittest.main()
