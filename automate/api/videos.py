"""Videos: rendered shorts with their hook variants, the MP4 preview/download, "als gepostet markieren", YouTube
upload and dub jobs.

Variant files are served only when the DB knows them and they resolve (symlinks included) to a regular .mp4 file
below paths.exports_dir().
"""
import re

from fastapi import APIRouter, Body, Query, Request

from .. import paths, settings, social, store
from . import files
from .common import Form, conflict, http_url, invalid, mapped, need, not_found, page

router = APIRouter()

VARIANT_KEYS = ("id", "video_id", "rank", "hook", "hook_type", "arm", "title", "description", "hashtags", "predicted",
                "duration", "posts")
VIDEO_KEYS = ("id", "profile", "lang", "day", "number", "title", "description", "sources", "archived", "created_at",
              "job_id", "pick_id")
PRIVACY = ("private", "unlisted", "public")


def variant_out(v):
    out = {k: v.get(k) for k in VARIANT_KEYS}
    out["file_url"] = f"/api/variants/{v['id']}/file"
    return out


def video_out(v):
    out = {k: v.get(k) for k in VIDEO_KEYS}
    out["variants"] = [variant_out(x) for x in v.get("variants") or []]
    return out


def _video(video_id):
    v = store.get_video(video_id)
    if v is None:
        raise not_found("Dieses Video gibt es nicht.")
    return v


def _variant(variant_id):
    v = store.get_variant(variant_id)
    if v is None:
        raise not_found("Diese Variante gibt es nicht.")
    return v


@router.get("/videos")
def list_videos(profile: str | None = None, day: str | None = None, posted: int | None = Query(None),
                lang: str | None = None, q: str | None = None, archived: int = Query(0),
                limit: int | None = Query(None), offset: int | None = Query(None)):
    limit, offset = page(limit, offset)
    errors = {}
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        errors["day"] = "Format JJJJ-MM-TT."
    if posted not in (None, 0, 1):
        errors["posted"] = "0 oder 1."
    if errors:
        raise invalid(errors)
    rows, total = store.list_videos(profile=profile or None, day=day or None,
                                    posted=None if posted is None else bool(posted), q=(q or "").strip()[:200] or None,
                                    archived=bool(archived), limit=limit, offset=offset, lang=lang or None)
    return {"items": [video_out(v) for v in rows], "total": total}


@router.get("/videos/{video_id}")
def get_video(video_id: int):
    return video_out(_video(video_id))


@router.patch("/videos/{video_id}")
def update_video(video_id: int, body: dict | None = Body(None)):
    _video(video_id)
    form = Form(body)
    archived = form.bool("archived", required=True)
    form.check()
    return video_out(store.update_video(video_id, archived=archived))


@router.get("/variants/{variant_id}/file")
def variant_file(request: Request, variant_id: int, download: int = Query(0)):
    v = _variant(variant_id)
    target = paths.from_rel(v.get("file") or "")
    files.safe_name(target.name, r"[^/\\]+\.mp4")
    fd, st = files.open_under(paths.exports_dir(), target)
    return files.file_response(request, fd, st, "video/mp4", download_name=target.name if download else None)


@router.post("/variants/{variant_id}/posts")
def mark_posted(variant_id: int, body: dict | None = Body(None)):
    _variant(variant_id)
    form = Form(body)
    platform = form.choice("platform", tuple(social.PLATFORMS), required=True, labels=tuple(social.PLATFORMS.values()))
    account_id = form.int("account_id", nullable=True)
    url = form.str("url", max_len=2000)
    note = form.str("note", max_len=1000)
    if url and not http_url(url):
        form.error("url", "Bitte einen vollständigen Link angeben (https://…).")
    if account_id is not None:
        a = store.get_account(account_id)
        if a is None:
            form.error("account_id", "Dieses Konto gibt es nicht.")
        elif platform and a["platform"] != platform:
            form.error("account_id", "Das Konto gehört zu einer anderen Plattform.")
    form.check()
    pid = store.add_post(variant_id, platform, account_id=account_id, status="posted", url=url or None,
                         note=note or None)
    return store.get_post(pid)


@router.delete("/posts/{post_id}")
def delete_post(post_id: int):
    if not store.delete_post(post_id):
        raise not_found("Diese Markierung gibt es nicht.")
    return {"ok": True}


@router.post("/variants/{variant_id}/upload")
def upload(variant_id: int, body: dict | None = Body(None)):
    _variant(variant_id)
    form = Form(body)
    account_id = form.int("account_id", required=True)
    privacy = form.choice("privacy", PRIVACY, required=True, labels=("Privat", "Nicht gelistet", "Öffentlich"))
    force = form.bool("force", default=False)
    form.check()
    account = store.get_account(account_id)
    if account is None:
        raise invalid({"account_id": "Dieses Konto gibt es nicht."})
    if account["platform"] != "youtube" or not account["connected"] or not account.get("has_credentials"):
        raise conflict("not_connected", f"„{account['label']}“ ist nicht mit YouTube verbunden. Bitte unter Konten "
                                        f"verbinden.")
    pipeline = need("pipeline")
    with mapped():
        job_id = pipeline.enqueue_upload(variant_id, account_id, privacy, force=bool(force))
    return {"job_id": job_id}


@router.post("/videos/{video_id}/dub")
def dub(video_id: int, body: dict | None = Body(None)):
    _video(video_id)
    form = Form(body)
    lang = form.str("lang", required=True, max_len=10, label="Die Sprache")
    if lang and (not settings.LANG_RE.match(lang) or lang == "en"):
        form.error("lang", "Ein Sprachcode wie es, ru oder pt-BR (nicht en).")
    form.check()
    if not store.secret_info("fish_api_key")["set"]:
        raise conflict("fish_key_missing", "Für Dubs fehlt der Fish-Audio-Schlüssel. Bitte unter „Kosten & "
                                           "Schlüssel“ eintragen.")
    pipeline = need("pipeline")
    with mapped():
        job_id = pipeline.enqueue_dub(video_id, lang)
    return {"job_id": job_id}
