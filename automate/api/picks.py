"""Freigabe-Queue: picks (list, edit, approve, reject, bulk) and the contact sheets of a job.

A pick row from the store is enriched for the dashboard (`Pick` in api.js): `duration` (all segments), `source_link`
(the source at the pick's start, from jobs/<id>/job.json's url + offset), `sheets` (contact sheets of the
candidates/scenarios the pick overlaps, most overlap first), `source_expires_at` (pipeline.source_expires_at: when
`clipper cleanup` deletes the source video; None while the pipeline module is missing).
Edits and decisions go through automate.pipeline (it rewrites picks.json and re-checks).
"""
import json
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, Body, Query, Request

from .. import paths, store
from . import files
from .common import ApiError, Form, invalid, mapped, need, not_found, optional, page

router = APIRouter()

CLIP_TYPES = ("reaction", "comedy", "news", "gameplay", "showcase", "scenario")
HOOK_STYLES = ("bold", "box", "label", "none")
CAPTION_STYLES = ("boxed", "bold", "comic")
SHEET_RE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.jpe?g"
JOB_DIR_RE = re.compile(r"^[A-Za-z0-9][\w.@-]{0,200}$")
MAX_SHEETS = 6
_json_cache = {}


def job_dir(clipper_job):
    """jobs/<clipper_job> for a DB-known folder name, or None (never a path outside the jobs folder)."""
    if not clipper_job or not JOB_DIR_RE.match(clipper_job) or ".." in clipper_job:
        return None
    return paths.jobs_dir() / clipper_job


def _json(path):
    """A small JSON file, cached by mtime (job.json and candidates.json are read for every pick of a list)."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    hit = _json_cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if len(_json_cache) > 500:
        _json_cache.clear()
    _json_cache[str(path)] = (mtime, data)
    return data


def timestamp_link(url, seconds):
    """Like clipper.config.timestamp_link, plus youtu.be and Twitch VOD links."""
    s = max(0, int(seconds))
    if "youtube.com/watch" in url:
        return f"{url}&t={s}s"
    if "youtu.be/" in url:
        return f"{url}{'&' if '?' in url else '?'}t={s}"
    if re.search(r"twitch\.tv/videos/\d+", url):
        return f"{url}{'&' if '?' in url else '?'}t={s // 3600}h{s % 3600 // 60}m{s % 60}s"
    return url


def _segments(data):
    if isinstance(data.get("segments"), list) and data["segments"]:
        return [s for s in data["segments"] if isinstance(s, dict)]
    if data.get("start") is not None and data.get("end") is not None:
        return [{"start": data["start"], "end": data["end"]}]
    return []


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sheets(job_id, d, own):
    cands = _json(d / "candidates.json") or {}
    windows = [c for c in (cands.get("candidates") or []) + (cands.get("scenarios") or []) if isinstance(c, dict)]
    scored = []
    for c in windows:
        name = os.path.basename(str(c.get("contact_sheet") or ""))
        s, e = _num(c.get("start")), _num(c.get("end"))
        if not name or s is None or e is None or not re.fullmatch(SHEET_RE, name) \
                or not (d / "review" / name).is_file():
            continue
        overlap = sum(max(0.0, min(e, b) - max(s, a)) for a, b in own)
        gap = min((max(0.0, a - e, s - b) for a, b in own), default=float("inf"))
        scored.append((overlap, -gap, name))
    hits = [x for x in scored if x[0] > 0] or sorted(scored, key=lambda x: x[1], reverse=True)[:1]
    names = [n for _, _, n in sorted(hits, key=lambda x: x[0], reverse=True)][:MAX_SHEETS]
    if not names and not windows and (d / "review").is_dir():  # no candidates.json: whatever sheets there are
        names = sorted(p.name for p in (d / "review").glob("*.jpg") if re.fullmatch(SHEET_RE, p.name))[:MAX_SHEETS]
    return [f"/api/jobs/{job_id}/sheets/{quote(n)}" for n in names]


def _expires_fn():
    pipeline = optional("pipeline")
    fn = getattr(pipeline, "source_expires_at", None)

    def expires(job):
        try:
            return fn(job) if fn else None
        except Exception:
            return None
    return expires


def enrich(pick, expires=None):
    expires = expires or _expires_fn()
    data = pick.get("data") or {}
    job = pick.get("job") or {}
    cj = job.get("clipper_job")
    segs = _segments(data)
    duration = sum(max(0.0, (_num(s.get("end")) or 0) - (_num(s.get("start")) or 0)) for s in segs)
    own = [(_num(s.get("start")) or 0, _num(s.get("end")) or 0) for s in segs if not s.get("job") or s.get("job") == cj]
    d = job_dir(cj)
    meta = (_json(d / "job.json") if d else None) or {}
    url = job.get("source_url") or meta.get("url")
    link = timestamp_link(url, own[0][0] + float(meta.get("offset") or 0)) if url and own else url
    out = dict(pick)
    out.update(duration=round(duration, 2), source_link=link,
               sheets=_sheets(pick["job_id"], d, own) if d else [],
               source_expires_at=expires(job) if d else None)
    return out


def enrich_many(picks):
    expires = _expires_fn()
    return [enrich(p, expires) for p in picks]


def _get(pick_id):
    p = store.get_pick(pick_id)
    if p is None:
        raise not_found("Diesen Pick gibt es nicht (mehr).")
    return p


# ---------------------------------------------------------------- routes

@router.get("/picks")
def list_picks(status: str | None = None, profile: str | None = None, limit: int | None = Query(None),
               offset: int | None = Query(None)):
    limit, offset = page(limit, offset)
    statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
    bad = [s for s in statuses if s not in store.PICK_STATUSES]
    if bad:
        raise invalid({"status": f"Unbekannter Status: {', '.join(bad)}."})
    rows, total = store.list_picks(status=statuses or None, profile=profile or None, limit=limit, offset=offset,
                                   order="queue" if statuses == ["proposed"] else "decided")
    return {"items": enrich_many(rows), "total": total, "counts": store.pick_counts(profile or None)}


@router.get("/picks/{pick_id}")
def get_pick(pick_id: int):
    return enrich(_get(pick_id))


@router.patch("/picks/{pick_id}")
def update_pick(pick_id: int, body: dict | None = Body(None)):
    _get(pick_id)
    form = Form(body)
    changes = {}
    for key in ("hooks", "titles", "hashtags"):
        if form.has(key):
            v = form.str_list(key, max_items=20, max_len=300)
            if key in ("hooks", "titles") and v is not None and not [x for x in v if x.strip()]:
                form.error(key, "Mindestens ein Eintrag.")
            changes[key] = v
    if form.has("description"):
        changes["description"] = form.str("description", max_len=5000, strip=False) or ""
    if form.has("voiceover"):
        changes["voiceover"] = form.str("voiceover", max_len=2000, nullable=True) or None
    for key in ("start", "end"):
        if form.has(key):
            changes[key] = form.number(key, min=0, max=86400 * 2)
    for key, choices in (("clip_type", CLIP_TYPES), ("hook_style", HOOK_STYLES), ("caption_style", CAPTION_STYLES)):
        if form.has(key):
            changes[key] = form.choice(key, choices)
    if not changes:
        form.error("_", "Keine Änderung angegeben.")
    form.check()
    pipeline = need("pipeline")
    with mapped():
        pick, report = pipeline.update_pick_data(pick_id, changes)
    return {"pick": enrich(store.get_pick(pick_id) or pick), "check_report": report}


@router.post("/picks/{pick_id}/approve")
def approve_pick(pick_id: int):
    _get(pick_id)
    pipeline = need("pipeline")
    with mapped():
        job_id = pipeline.approve_pick(pick_id)
    return {"pick": enrich(_get(pick_id)), "job_id": job_id}


@router.post("/picks/{pick_id}/reject")
def reject_pick(pick_id: int, body: dict | None = Body(None)):
    _get(pick_id)
    form = Form(body)
    reason = form.str("reason", max_len=500)
    form.check()
    pipeline = need("pipeline")
    with mapped():
        pipeline.reject_pick(pick_id, reason or None)
    return {"pick": enrich(_get(pick_id))}


@router.post("/picks/bulk")
def bulk(body: dict | None = Body(None)):
    form = Form(body)
    ids = form.id_list("ids")
    action = form.choice("action", ("approve", "reject"), required=True, labels=("approve", "reject"))
    reason = form.str("reason", max_len=500)
    form.check()
    pipeline = need("pipeline")
    results = []
    for pid in ids:
        try:
            if store.get_pick(pid) is None:
                raise not_found("Diesen Pick gibt es nicht (mehr).")
            with mapped():
                if action == "approve":
                    results.append({"id": pid, "ok": True, "job_id": pipeline.approve_pick(pid)})
                else:
                    pipeline.reject_pick(pid, reason or None)
                    results.append({"id": pid, "ok": True})
        except ApiError as e:
            results.append({"id": pid, "ok": False, "error": {"code": e.code, "message": e.message}})
    return {"results": results}


@router.get("/jobs/{job_id}/sheets/{name}")
def sheet(request: Request, job_id: int, name: str):
    job = store.get_job(job_id)
    d = job_dir(job and job.get("clipper_job"))
    if d is None:
        raise not_found("Kontaktbogen nicht gefunden.")
    files.safe_name(name, SHEET_RE)
    fd, st = files.open_under(d / "review", d / "review" / name)
    return files.file_response(request, fd, st, "image/jpeg", cache="private, max-age=3600")
