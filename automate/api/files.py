"""Serving files from disk safely: only below a given root, no symlink out of it, regular files only, streamed in
chunks (never read whole) with single-range HTTP Range support so <video> can seek.

    open_under(root, path) -> (fd, stat)        ApiError 404 for anything outside `root`, missing or not a file
    file_response(request, fd, st, media_type, *, download_name=None, cache="private, no-cache") -> Response
        200 / 206 (one range: "a-b", "a-", "-n") / 304 (If-None-Match) / 416 (unsatisfiable or malformed);
        several ranges are answered with the whole file (allowed by RFC 9110). Accept-Ranges, ETag, Last-Modified,
        Content-Length always; Content-Range on 206/416. The fd is closed when the response is done.
    parse_range(header, size) -> (start, end) | None (ignore: whole file) | "unsatisfiable"
"""
import os
import re
import stat as stat_mod
from email.utils import formatdate
from urllib.parse import quote

from starlette.responses import Response, StreamingResponse

from .common import not_found

CHUNK = 256 * 1024
_RANGE_RE = re.compile(r"^\s*(\d*)\s*-\s*(\d*)\s*$")


def open_under(root, path):
    """Open `path` read-only if it really lies below `root` (after resolving every symlink) and is a regular file."""
    root_real = os.path.realpath(root)
    target = os.path.realpath(path)
    try:
        inside = os.path.commonpath([root_real, target]) == root_real and target != root_real
    except ValueError:  # different drives / mixed absolute and relative
        inside = False
    if not inside:
        raise not_found("Datei nicht gefunden.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        raise not_found("Datei nicht gefunden.") from None
    st = os.fstat(fd)
    if not stat_mod.S_ISREG(st.st_mode):
        os.close(fd)
        raise not_found("Datei nicht gefunden.")
    return fd, st


def parse_range(header, size):
    if not header or "=" not in header:
        return None
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes":
        return None
    if "," in spec:
        return None  # several ranges: send the whole file
    m = _RANGE_RE.match(spec)
    if not m or (not m.group(1) and not m.group(2)):
        return "unsatisfiable"
    first, last = m.group(1), m.group(2)
    if size <= 0:
        return "unsatisfiable"
    if not first:  # suffix: the last n bytes
        n = int(last)
        if n <= 0:
            return "unsatisfiable"
        return max(0, size - n), size - 1
    start = int(first)
    end = size - 1 if not last else min(int(last), size - 1)
    if start >= size or (last and int(last) < start):
        return "unsatisfiable"
    return start, end


def _chunks(fd, start, length):
    try:
        pos, left = start, length
        while left > 0:
            data = os.pread(fd, min(CHUNK, left), pos)
            if not data:
                break
            pos += len(data)
            left -= len(data)
            yield data
    finally:
        os.close(fd)


def file_response(request, fd, st, media_type, *, download_name=None, cache="private, no-cache"):
    size = st.st_size
    etag = f'"{st.st_mtime_ns:x}-{size:x}"'
    headers = {"Accept-Ranges": "bytes", "ETag": etag, "Last-Modified": formatdate(st.st_mtime, usegmt=True),
               "Cache-Control": cache}
    if download_name:
        safe = re.sub(r'[^\w.\- ]', "_", download_name).strip() or "download"
        headers["Content-Disposition"] = f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(download_name)}"
    status, start, length = 200, 0, size
    rng, if_range = request.headers.get("range"), request.headers.get("if-range")
    if rng and (if_range is None or if_range.strip() in (etag, headers["Last-Modified"])):
        r = parse_range(rng, size)
        if r == "unsatisfiable":
            os.close(fd)
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        if r is not None:
            start, end = r
            status, length = 206, end - start + 1
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    elif not rng and request.headers.get("if-none-match", "").strip() == etag:
        os.close(fd)
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        os.close(fd)
        return Response(status_code=status, headers=headers, media_type=media_type)
    return StreamingResponse(_chunks(fd, start, length), status_code=status, headers=headers, media_type=media_type)


def safe_name(name, pattern):
    """`name` when it is a plain file name matching `pattern` (no separators, no dot-dot), else a 404."""
    if not isinstance(name, str) or "/" in name or "\\" in name or name in (".", "..") or "\x00" in name \
            or not re.fullmatch(pattern, name):
        raise not_found("Datei nicht gefunden.")
    return name
