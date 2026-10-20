"""Static audit of the React source: no dead links, no handler-less buttons, no placeholder pages."""
import re, unittest
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase

SRC = Path(settings.BASE_DIR) / "frontend" / "src"
FILES = sorted(p for p in SRC.rglob("*.tsx") if not p.name.endswith(".test.tsx"))

def routes():
    t = (SRC / "main.tsx").read_text(encoding="utf-8")
    pats = [p for p in re.findall(r'<Route\s+path="([^"]+)"', t) if p != "*"]
    return [re.compile("^" + re.sub(r":[A-Za-z_]+", "[^/]+", p) + "$") for p in pats], pats

def tag_end(s, i):
    """Index of the '>' that closes the JSX opening tag starting at i (ignores > inside {...} and quotes)."""
    depth, q = 0, None
    while i < len(s):
        c = s[i]
        if q: q = None if c == q and s[i - 1] != "\\" else q
        elif c in "\"'`": q = c
        elif c == "{": depth += 1
        elif c == "}": depth -= 1
        elif c == ">" and depth == 0: return i
        i += 1
    return len(s)

@unittest.skipUnless(SRC.exists(), "frontend sources not present (backend-only checkout)")
class FrontendStaticAudit(SimpleTestCase):
    def test_every_internal_link_and_navigation_target_is_a_real_route(self):
        rx, pats = routes(); self.assertGreater(len(pats), 15)
        LINK = re.compile(r"""(?:\bto[=:]\s*\{?|\bnav(?:igate)?\(\s*|href=\{?)(?:"(/[^"]*)"|`(/[^`]*)`)""")
        bad = []
        for f in FILES:
            for m in LINK.finditer(f.read_text(encoding="utf-8")):
                raw = m.group(1) or m.group(2)
                if raw.startswith(("/api/", "/media")) or raw == "/": continue
                p = re.sub(r"\$\{[^}]*\}", "x", raw).split("?")[0].split("#")[0].rstrip("/") or "/"
                if not any(r.match(p) for r in rx): bad.append(f"{f.name}: {raw}")
        self.assertEqual(bad, [], "links to routes that don't exist:\n" + "\n".join(bad))

    def test_every_button_has_an_action(self):
        bad = []
        for f in FILES:
            s = f.read_text(encoding="utf-8")
            for m in re.finditer(r"<button\b", s):
                tag = s[m.start():tag_end(s, m.end())]; before = s[:m.start()]
                in_form = before.rfind("<form") > before.rfind("</form>")
                if not (re.search(r"\bonClick=", tag) or 'type="submit"' in tag or re.search(r"\bdisabled\b(?!=)", tag) or in_form): bad.append(f"{f.name}: {tag[:90]}")
        self.assertEqual(bad, [], "buttons without a click handler:\n" + "\n".join(bad))

    def test_no_placeholder_pages_or_empty_links(self):
        for f in FILES:
            s = f.read_text(encoding="utf-8")
            for word in ("under development", "coming soon", "lorem ipsum", "not implemented"): self.assertNotIn(word, s.lower(), f"{f.name} contains placeholder text '{word}'")
            self.assertNotRegex(s, r'href="#"', f"{f.name} has an empty link")

    def test_unknown_urls_get_a_real_404_page_not_a_silent_redirect(self):
        t = (SRC / "main.tsx").read_text(encoding="utf-8"); self.assertIn("<NotFound />", t); self.assertNotRegex(t, r'path="\*"\s+element=\{<Navigate')
        self.assertIn("/status", t); self.assertTrue((Path(settings.BASE_DIR) / "frontend/public/_redirects").exists())
