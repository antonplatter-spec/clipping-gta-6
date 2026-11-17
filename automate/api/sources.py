"""Quellen: profiles, YouTube creators, live streams and each profile's profile.yaml overrides.

New sources are resolved the way the CLI does it (through the bridge: clipper.library.channel_url + a yt-dlp lookup
for creators, clipper.live.stream_url/stream_name for streams); the server never imports clipper. Every change
rewrites the clipper overlay (runtime.materialize) and tells the pipeline.
"""
import re

from fastapi import APIRouter, Body

from .. import runtime, store
from .common import ApiError, Form, changed, conflict, invalid, mapped, not_found
from .dashboard import live_status, with_live

router = APIRouter()

PROFILE_KEYS = ("name", "enabled", "creators", "streams", "updated_at")
CREATOR_KEYS = ("id", "profile", "name", "url", "match", "paused", "priority", "created_at")
STREAM_KEYS = ("id", "profile", "name", "url", "platform", "topic_only", "paused", "priority", "created_at")


def _profile(name):
    p = store.get_profile(name)
    if p is None:
        raise not_found(f"Unbekanntes Profil „{name}“.")
    return p


def profile_out(p):
    return {k: p.get(k) for k in PROFILE_KEYS}


def _profile_field(form):
    name = form.str("profile", required=True, max_len=64, label="Das Profil")
    if name and store.get_profile(name) is None:
        form.error("profile", f"Unbekanntes Profil „{name}“.")
    return name


def _bridge(op, payload, profile, timeout=150):
    with mapped():
        return runtime.run_bridge(op, payload, profile, timeout=timeout)


def _resolve(op, payload, profile, field="ref"):
    """A bridge resolve op; its failure is a field error (the ref couldn't be understood or checked)."""
    try:
        return _bridge(op, payload, profile)
    except ApiError as e:
        raise invalid({field: f"Konnte die Quelle nicht prüfen: {e.message}"}) from None


# ---------------------------------------------------------------- profiles

@router.get("/profiles")
def list_profiles():
    items = [profile_out(p) for p in store.list_profiles()]
    return {"items": items, "total": len(items)}


@router.patch("/profiles/{name}")
def update_profile(name: str, body: dict | None = Body(None)):
    _profile(name)
    form = Form(body)
    enabled = form.bool("enabled", required=True)
    form.check()
    p = store.update_profile(name, enabled=enabled)
    changed(["profiles"])
    return profile_out(p)


def _type_errors(overrides, base, prefix=""):
    """Per dotted path: values that would crash the clipper (a number where the repo has a number, etc.) and
    regular expressions (`*patterns` lists) that don't compile."""
    errors = {}
    for k, v in overrides.items():
        path, b = f"{prefix}{k}", base.get(k) if isinstance(base, dict) else None
        if isinstance(b, dict) and isinstance(v, dict) and not prefix:
            errors.update(_type_errors(v, b, f"{path}."))
            continue
        if isinstance(b, bool) and not isinstance(v, bool):
            errors[path] = "Muss an oder aus sein (true/false)."
        elif isinstance(b, (int, float)) and not isinstance(b, bool) and \
                (not isinstance(v, (int, float)) or isinstance(v, bool)):
            errors[path] = "Muss eine Zahl sein."
        elif isinstance(b, list) and not isinstance(v, list):
            errors[path] = "Muss eine Liste sein."
        elif isinstance(b, dict) and not isinstance(v, dict):
            errors[path] = "Muss eine Gruppe von Werten sein."
        if str(k).endswith("patterns") and isinstance(v, list):
            for i, rx in enumerate(v):
                try:
                    re.compile(str(rx))
                except re.error as e:
                    errors[f"{path}.{i}"] = f"Ungültiger regulärer Ausdruck: {e}"
    return errors


def check_overrides(body, base):
    form = Form(body)
    overrides = form.body.get("overrides")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise invalid({"overrides": "Muss eine Zuordnung Schlüssel → Wert sein."})
    errors = _type_errors(overrides, base)
    if errors:
        raise invalid(errors)
    return overrides


@router.get("/profiles/{name}/config")
def get_profile_config(name: str):
    _profile(name)
    return runtime.profile_config(name)


@router.put("/profiles/{name}/config")
def put_profile_config(name: str, body: dict | None = Body(None)):
    _profile(name)
    base = runtime.merge_config(runtime.watchlist_config()["base"], runtime.profile_config(name)["base"])
    overrides = check_overrides(body, base)
    store.update_profile(name, overrides=overrides)
    changed([f"profiles.{name}.overrides"])
    return runtime.profile_config(name)


# ---------------------------------------------------------------- creators

def creator_out(c):
    return {k: c.get(k) for k in CREATOR_KEYS}


def _creator(cid):
    c = store.get_creator(cid)
    if c is None:
        raise not_found("Diesen Creator gibt es nicht.")
    return c


@router.get("/creators")
def list_creators(profile: str | None = None):
    items = [creator_out(c) for c in store.list_creators(profile or None)]
    return {"items": items, "total": len(items)}


@router.post("/creators")
def add_creator(body: dict | None = Body(None)):
    form = Form(body)
    profile = _profile_field(form)
    ref = form.str("ref", required=True, max_len=500, label="@Handle oder Kanal-URL")
    name = form.str("name", max_len=100)
    if ref and not re.fullmatch(r"@?[\w.\-]{2,100}|https?://(www\.|m\.)?youtube\.com/\S+", ref):
        form.error("ref", "Bitte ein @Handle (z. B. @KaiCenat) oder eine YouTube-Kanal-URL angeben.")
    form.check()
    res = _resolve("resolve_creator", {"ref": ref, "lookup": True}, profile)
    if not res.get("found"):
        raise invalid({"ref": f"Unter {res.get('url') or ref} wurde kein YouTube-Kanal gefunden (yt-dlp prüft das; "
                              f"stimmt der Name und ist das Internet erreichbar?)."})
    try:
        cid = store.add_creator(profile, name or res.get("name") or ref, res["url"])
    except ValueError as e:
        raise conflict("duplicate", str(e)) from None
    changed(["creators"])
    return creator_out(store.get_creator(cid))


@router.patch("/creators/{creator_id}")
def update_creator(creator_id: int, body: dict | None = Body(None)):
    _creator(creator_id)
    form = Form(body)
    fields = {"name": form.str("name", max_len=100), "paused": form.bool("paused"),
              "priority": form.int("priority", min=0, max=9999), "match": form.choice("match", ("any", "keywords"))}
    if form.has("name") and not fields["name"]:
        form.error("name", "Der Name darf nicht leer sein.")
    form.check()
    fields = {k: v for k, v in fields.items() if v is not None}
    c = store.update_creator(creator_id, **fields) if fields else _creator(creator_id)
    changed(["creators"])
    return creator_out(c)


@router.delete("/creators/{creator_id}")
def delete_creator(creator_id: int):
    if not store.delete_creator(creator_id):
        raise not_found("Diesen Creator gibt es nicht.")
    changed(["creators"])
    return {"ok": True}


# ---------------------------------------------------------------- streams

def stream_out(s, by_key=None):
    out = {k: s.get(k) for k in STREAM_KEYS}
    return with_live(out, by_key or {})


def _stream(sid):
    s = store.get_stream(sid)
    if s is None:
        raise not_found("Diesen Stream gibt es nicht.")
    return s


@router.get("/streams")
def list_streams(profile: str | None = None):
    by_key, _ = live_status()
    items = [stream_out(s, by_key) for s in store.list_streams(profile or None)]
    return {"items": items, "total": len(items)}


@router.post("/streams")
def add_stream(body: dict | None = Body(None)):
    form = Form(body)
    profile = _profile_field(form)
    ref = form.str("ref", required=True, max_len=500, label="Der Stream")
    topic_only = form.bool("topic_only", default=True)
    if ref and not re.fullmatch(r"(twitch|kick|youtube):@?[\w.\-]{2,100}|https?://\S+", ref):
        form.error("ref", "Format: twitch:NAME, kick:NAME, youtube:@NAME oder eine Stream-URL.")
    form.check()
    res = _resolve("resolve_stream", {"ref": ref}, profile)
    try:
        sid = store.add_stream(profile, res.get("name") or ref, res["url"], topic_only)
    except ValueError as e:
        raise conflict("duplicate", str(e)) from None
    changed(["streams"])
    return stream_out(store.get_stream(sid))


@router.patch("/streams/{stream_id}")
def update_stream(stream_id: int, body: dict | None = Body(None)):
    _stream(stream_id)
    form = Form(body)
    fields = {"paused": form.bool("paused"), "priority": form.int("priority", min=0, max=9999),
              "topic_only": form.bool("topic_only")}
    form.check()
    fields = {k: v for k, v in fields.items() if v is not None}
    s = store.update_stream(stream_id, **fields) if fields else _stream(stream_id)
    changed(["streams"])
    by_key, _ = live_status()
    return stream_out(s, by_key)


@router.delete("/streams/{stream_id}")
def delete_stream(stream_id: int):
    if not store.delete_stream(stream_id):
        raise not_found("Diesen Stream gibt es nicht.")
    changed(["streams"])
    return {"ok": True}
