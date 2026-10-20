"""Local media serving (with HTTP Range, which browsers need to play/seek video) and upload content sniffing."""
import asyncio, mimetypes, re
from pathlib import Path
from django.conf import settings
from django.http import Http404, HttpResponse, StreamingHttpResponse

CHUNK = 256 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")

def sniff_video(head: bytes):
    """Identify real video containers from their first bytes: 'mp4', 'mov' (ISO base media) or 'webm'. None = not an accepted video."""
    if len(head) >= 12 and head[4:8] == b"ftyp": return "mov" if head[8:12] == b"qt  " else "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3": return "webm"
    return None

async def _iter_file(path, start, length):
    with open(path, "rb") as f:
        f.seek(start); remaining = length
        while remaining > 0:
            chunk = await asyncio.to_thread(f.read, min(CHUNK, remaining))
            if not chunk: break
            remaining -= len(chunk); yield chunk

async def serve_media(request, path):
    """GET/HEAD /media/<path> from MEDIA_ROOT. Supports a single `Range: bytes=a-b` (206) so seeking/Safari work. Path traversal is blocked."""
    if request.method not in ("GET", "HEAD"): return HttpResponse(status=405)
    root = Path(settings.MEDIA_ROOT).resolve(); full = (root / path).resolve()
    try: full.relative_to(root)
    except ValueError: raise Http404("Not found")
    if not full.is_file(): raise Http404("Not found")
    size = full.stat().st_size; ctype = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
    start, end, status = 0, size - 1, 200; m = _RANGE.match((request.headers.get("Range") or "").strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1): start = int(m.group(1)); end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else: start = max(size - int(m.group(2)), 0)          # suffix range: last N bytes
        if start >= size or start > end: return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    length = end - start + 1
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Cache-Control": "public, max-age=86400"}
    if status == 206: headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD": return HttpResponse(status=status, content_type=ctype, headers=headers)
    return StreamingHttpResponse(_iter_file(full, start, length), status=status, content_type=ctype, headers=headers)
