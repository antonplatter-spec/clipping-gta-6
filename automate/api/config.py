"""Einstellungen: the settings registry (generic forms), the watchlist.yaml overrides (also the caption, hook and
sound defaults of "Untertitel & Sound": one source of truth, validated by style.config_errors), the research playbook
and Claude's extra instructions, fonts and the music library with the owner's own sounds.

Every write is validated by settings.validate (422 with per-key German messages, nothing saved on any error), then
the overlay is rewritten and the pipeline told (common.changed).

Own sounds: POST /assets/music?name=&kind=hit|music&role=|mood=&claimable=0|1&approved=0|1 with the raw file as the
body (no multipart: no extra dependency; at most MAX_SOUND_BYTES, server.py lets this one route past MAX_BODY). The
body is streamed to data/tmp/uploads/<random>.upload, then bridge `add_sound` checks it with ffprobe and files it
under a server-chosen name in assets/music/local/; the upload is always deleted. DELETE /assets/music/{id} removes an
own sound (bridge `remove_sound`). GET /assets/music/{id}/file streams any library sound (Range support) found by id
in library.yaml or local/library.yaml, only from inside the music folder.
"""
import re
import secrets

import anyio
import yaml
from fastapi import APIRouter, Body, Query, Request

from .. import paths, runtime, settings, store, style
from . import files
from .common import ApiError, Form, changed, invalid, mapped, not_found, optional
from .sources import check_overrides

router = APIRouter()

PLAYBOOK = "viral-gta-shorts.md"
FONT_EXT = (".ttf", ".otf", ".woff2", ".woff")
MAX_SOUND_BYTES = 25 * 1024 * 1024
SOUND_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}
TRACK_ID = r"[\w.\-]{1,100}"


@router.get("/settings")
def get_settings():
    return {"values": store.all_settings(), "schema": settings.schema()}


@router.patch("/settings")
def patch_settings(body: dict | None = Body(None)):
    if not isinstance(body, dict) or not body:
        raise invalid({"_": "Keine Einstellung angegeben."})
    values = store.set_settings(body)  # SettingsError -> 422 with every bad key (server.py)
    changed(list(body))
    return {"values": values}


@router.get("/settings/watchlist")
def get_watchlist():
    return runtime.watchlist_config()


@router.put("/settings/watchlist")
def put_watchlist(body: dict | None = Body(None)):
    overrides = check_overrides(body, runtime.watchlist_config()["base"])
    store.set_settings({"watchlist.overrides": overrides})
    changed(["watchlist.overrides"])
    return runtime.watchlist_config()


def _research():
    f = paths.research_dir() / PLAYBOOK
    try:
        playbook = f.read_text(encoding="utf-8")
    except OSError:
        playbook = ""
    s = store.all_settings()
    override, extra = s["research.override"], s["claude.extra_instructions"] or ""
    chars = None
    agent = optional("agent")
    if agent is not None and hasattr(agent, "system_prompt_chars"):
        try:
            chars = int(agent.system_prompt_chars(store.get_setting("ui.default_profile")))
        except Exception:
            chars = None
    if chars is None:  # without the agent module: at least the parts the owner controls
        chars = len(override if override is not None else playbook) + len(extra)
    return {"playbook": playbook, "override": override, "extra_instructions": extra, "system_prompt_chars": chars}


@router.get("/research")
def get_research():
    return _research()


@router.put("/research")
def put_research(body: dict | None = Body(None)):
    form = Form(body)
    changes = {}
    if form.has("override"):
        changes["research.override"] = form.body.get("override")
    if form.has("extra_instructions"):
        changes["claude.extra_instructions"] = form.body.get("extra_instructions")
    if not changes:
        raise invalid({"_": "override oder extra_instructions angeben."})
    store.set_settings(changes)
    changed(list(changes), overlay=False)
    return _research()


def _fonts():
    d = paths.ROOT / "assets" / "fonts"
    if not d.is_dir():
        return []
    texts = [p for p in d.iterdir() if p.suffix.lower() == ".txt"]
    out = []
    for f in sorted(p for p in d.iterdir() if p.suffix.lower() in FONT_EXT):
        family = re.split(r"[-_.]", f.stem)[0]
        lic = next((t for t in texts if t.stem.lower().startswith(family.lower())), None)
        label = None
        if lic is not None:
            label = "SIL Open Font License" if "ofl" in lic.stem.lower() else None
            if label is None:
                try:
                    head = next((ln.strip() for ln in lic.read_text(encoding="utf-8", errors="ignore").splitlines()
                                 if ln.strip()), "")
                except OSError:
                    head = ""
                label = head[:80] or lic.name
        out.append({"name": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family), "file": f"assets/fonts/{f.name}",
                    "license": label})
    return out


def _music():
    with mapped():
        data = runtime.run_bridge("music_library", {}, store.get_setting("ui.default_profile"), timeout=120)
    return [t for t in data.get("tracks") or [] if isinstance(t, dict)]


@router.get("/assets")
def get_assets():
    return {"fonts": _fonts(), "music": _music()}


@router.patch("/assets/music/{track_id}")
def update_music(track_id: str, body: dict | None = Body(None)):
    form = Form(body)
    approved = form.bool("approved", required=True)
    form.check()
    if not re.fullmatch(TRACK_ID, track_id) or not any(t.get("id") == track_id for t in _music()):
        raise not_found("Diesen Track gibt es nicht.")
    with mapped():
        res = runtime.run_bridge("set_music", {"id": track_id, "approved": approved},
                                 store.get_setting("ui.default_profile"), timeout=120)
    changed(["assets.music"], overlay=False)
    return res.get("track") or {}


def _profile():
    return store.get_setting("ui.default_profile")


@router.post("/assets/music")
async def upload_sound(request: Request, name: str = Query(""), kind: str = Query(""), role: str = Query(""),
                       mood: str = Query(""), claimable: int = Query(0), approved: int = Query(1)):
    """An own sound or music track; the body is the file itself (see the module doc)."""
    errors = {}
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    ext = ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""
    if not base or len(name) > 200 or any(ord(c) < 32 for c in name):
        errors["name"] = "Dateiname fehlt oder ist ungültig."
    elif ext not in SOUND_TYPES:
        errors["name"] = "Nur WAV-, MP3-, M4A- oder OGG-Dateien."
    if kind not in ("hit", "music"):
        errors["kind"] = "Sound-Effekt oder Musik?"
    elif kind == "hit" and role not in style.SOUND_ROLES:
        errors["role"] = f"Rolle: eins von {', '.join(style.SOUND_ROLES)}."
    elif kind == "music" and mood not in style.MOODS:
        errors["mood"] = f"Stimmung: eins von {', '.join(style.MOODS)}."
    for key, v in (("claimable", claimable), ("approved", approved)):
        if v not in (0, 1):
            errors[key] = "0 oder 1."
    if errors:
        raise invalid(errors)
    inbox = paths.ensure_data_dir() / "tmp" / "uploads"
    inbox.mkdir(mode=0o700, exist_ok=True)
    tmp = inbox / f"{secrets.token_hex(12)}.upload"
    try:
        size = 0
        with open(tmp, "xb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_SOUND_BYTES:
                    raise ApiError(413, "too_large", f"Die Datei ist zu groß (höchstens "
                                                     f"{MAX_SOUND_BYTES // (1024 * 1024)} MB).")
                f.write(chunk)
        if not size:
            raise invalid({"file": "Die Datei ist leer."}, "Die Datei ist leer.")
        payload = {"path": str(tmp), "name": base, "kind": kind, "role": role or None, "mood": mood or None,
                   "claimable": bool(claimable), "approved": bool(approved)}
        with mapped():
            res = await anyio.to_thread.run_sync(
                lambda: runtime.run_bridge("add_sound", payload, _profile(), timeout=300))
    finally:
        tmp.unlink(missing_ok=True)
    if res.get("problem"):
        raise invalid({"file": res["problem"]}, res["problem"])
    changed(["assets.music"], overlay=False)
    return res.get("track") or {}


@router.delete("/assets/music/{track_id}")
def delete_sound(track_id: str):
    if not re.fullmatch(TRACK_ID, track_id):
        raise not_found("Diesen Sound gibt es nicht.")
    with mapped():
        res = runtime.run_bridge("remove_sound", {"id": track_id}, _profile(), timeout=120)
    if res.get("problem"):
        raise ApiError(409, "not_removable", res["problem"])
    changed(["assets.music"], overlay=False)
    return {"ok": True}


def _entries(f):
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [t for t in (data.get("tracks") or []) if isinstance(t, dict)] if isinstance(data, dict) else []


def sound_file(track_id):
    """(folder, file) of a library sound by id, as clipper.music.load_library resolves it: an entry of
    local/library.yaml with its own `file` wins (relative to local/), else the shipped entry; None when unknown."""
    shipped, local = paths.music_dir(), paths.music_local_dir()
    found = None
    for folder, f in ((shipped, shipped / "library.yaml"), (local, local / "library.yaml")):
        for t in _entries(f):
            if str(t.get("id")) == track_id and t.get("file"):
                found = (folder, folder / str(t["file"]))
    return found


@router.get("/assets/music/{track_id}/file")
def sound_audio(request: Request, track_id: str):
    hit = sound_file(track_id) if re.fullmatch(TRACK_ID, track_id) else None
    if hit is None:
        raise not_found("Diesen Sound gibt es nicht.")
    folder, f = hit
    media = SOUND_TYPES.get(f.suffix.lower())
    if media is None:
        raise not_found("Diesen Sound gibt es nicht.")
    files.safe_name(f.name, r"[^/\\]+")
    fd, st = files.open_under(folder, f)
    return files.file_response(request, fd, st, media, cache="private, max-age=3600")
