import re
def clean_interests(v):
    """Accept a list or comma string; return up to 10 unique lowercase tags (a-z, 0-9, space, dash; <=24 chars)."""
    items = v.split(",") if isinstance(v, str) else (v if isinstance(v, (list, tuple)) else [])
    out = []
    for i in items:
        t = re.sub(r"[^a-z0-9 -]", "", str(i).strip().lower())[:24].strip()
        if t and t not in out: out.append(t)
    return out[:10]
