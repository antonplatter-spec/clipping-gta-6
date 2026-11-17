"""Einstellungen: the settings registry (generic forms), the watchlist.yaml overrides, the research playbook and
Claude's extra instructions, fonts and the music library.

Every write is validated by settings.validate (422 with per-key German messages, nothing saved on any error), then
the overlay is rewritten and the pipeline told (common.changed).
"""
import re

from fastapi import APIRouter, Body

from .. import paths, runtime, settings, store
from .common import Form, changed, invalid, mapped, not_found, optional
from .sources import check_overrides

router = APIRouter()

PLAYBOOK = "viral-gta-shorts.md"
FONT_EXT = (".ttf", ".otf", ".woff2", ".woff")


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
    if not re.fullmatch(r"[\w.\-]{1,100}", track_id) or not any(t.get("id") == track_id for t in _music()):
        raise not_found("Diesen Track gibt es nicht.")
    with mapped():
        res = runtime.run_bridge("set_music", {"id": track_id, "approved": approved},
                                 store.get_setting("ui.default_profile"), timeout=120)
    changed(["assets.music"], overlay=False)
    return res.get("track") or {}
