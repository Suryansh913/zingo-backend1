"""Frontend <-> backend contract audit: every API path the React code calls must resolve in Django's URLconf and allow the HTTP method used.
Catches missing routes, trailing-slash mismatches and wrong methods (the usual causes of API 404/405 in production)."""
import itertools, re, unittest
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve

SRC = Path(settings.BASE_DIR) / "frontend" / "src"
CALL = re.compile(r"""(?:\bapi(?:<[^>()]*>)?|\bupload)\(\s*([`"'])(/api/[^`"']*)\1""")
FETCH = re.compile(r"""fetch\(\s*`\$\{API\}(/api/[^`]*)`""")
NEXT_CALL = re.compile(r"\b(?:api(?:<[^>()]*>)?|upload|fetch)\(")
GUESS = ["1", "x", "like", "pass", "accept", "decline", "skip", "end", "report"]

def methods_of(func):
    if hasattr(func, "actions"): return {m.upper() for m in func.actions}
    cls = getattr(func, "cls", None) or getattr(func, "view_class", None)
    return {m.upper() for m in cls().allowed_methods} if cls else set()

def collect():
    for f in sorted(SRC.rglob("*.ts*")):
        t = f.read_text(encoding="utf-8")
        for rx in (CALL, FETCH):
            for m in rx.finditer(t):
                raw = m.group(2) if rx is CALL else m.group(1); is_upload = rx is CALL and m.group(0).lstrip().startswith("upload"); tail = t[m.end():m.end() + 220]; nxt = NEXT_CALL.search(tail); tail = tail[:nxt.start()] if nxt else tail
                mm = re.search(r"method:\s*([^,}]+)", tail); meths = set(re.findall(r"[A-Z]{3,6}", mm.group(1))) if mm else ({"POST"} if is_upload else {"GET"})   # upload() is always a multipart POST
                yield f.relative_to(SRC), raw, meths

@unittest.skipUnless(SRC.exists(), "frontend sources not present (backend-only checkout)")
class FrontendContractTests(SimpleTestCase):
    def test_every_frontend_api_call_resolves_with_an_allowed_method(self):
        calls = list(collect()); self.assertGreater(len(calls), 60, "audit found too few calls - parser broken?"); problems = []
        for f, raw, meths in calls:
            path = raw.split("?")[0]; holes = re.findall(r"\$\{[^}]*\}", path); ok_methods = None
            for combo in itertools.product(GUESS, repeat=len(holes)):
                p = path
                for h, g in zip(holes, combo): p = p.replace(h, g, 1)
                try: ok_methods = methods_of(resolve(p).func); break
                except Resolver404: continue
            if ok_methods is None: problems.append(f"{f}: {raw}  -> NOT FOUND in Django URLconf"); continue
            bad = {m for m in meths if m not in ok_methods and m not in ("HEAD", "OPTIONS")}
            if bad: problems.append(f"{f}: {raw}  -> method {sorted(bad)} not allowed (allowed: {sorted(ok_methods)})")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))
