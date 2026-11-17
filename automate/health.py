"""What the owner has to know about this machine and the running service: the system report (tools, disk,
versions, with German install hints per platform), the "Bereit zum Start" checklist, the live stream status the live
service cached, and the health checks that raise and resolve dashboard alerts.

    system_report(refresh=False) -> {version, python, platform, paths, disk: {path, free_gb, total_gb, min_free_gb},
                                     tools: [{name, ok, version, hint, required, purpose}], setup_report}
        tools come from clipper.setup.missing()/run(dry_run) through the bridge op `system_tools` (cached 10 min)
    readiness() -> {anthropic_key, fish_key, youtube_connected, tools_ok, sources (number), automation}
    live_status() -> {streams: [{id, name, url, profile, platform, live, on_topic, recording, title, checked_at}],
                      recorders, checked_at}     (None fields = never checked; nothing here calls yt-dlp)
    check_all(now=None) -> [{key, action: "raised" | "resolved", level, message}]   (also recorded as a `health` run)
    gb_text(gb) -> "29,6 GB" (German, one decimal)
    store_live(profile, data, error=None) / forget_live(profile)     the live service's cache (also kept in meta)

Alert keys (stable; messages German and say what to do): disk.low, tools.missing, tools.voice, key.anthropic_missing,
key.fish_missing, budget.reached, live.not_recording, live.stalled, live.analysis_failed, live.service,
job.failed.<id> (raised by the pipeline, resolved here once the job was retried or canceled), scan.failed.<profile>
(raised and resolved by the scan itself).
"""
import copy
import importlib.util
import platform
import shutil
import sys
import threading
import time
from pathlib import Path

from . import __version__, paths, pipeline, runtime, store

SYSTEM_TTL_S = 600
LIVE_KEYS = ("live.not_recording", "live.stalled", "live.analysis_failed", "live.service")

_sys_lock = threading.Lock()
_sys_cache = {"at": 0.0, "data": None}
_live_lock = threading.Lock()
_live = None


# ---------------------------------------------------------------- system report

def _is_mac():
    return sys.platform == "darwin"


def _is_wsl():
    return "microsoft" in platform.release().lower()


def _platform_label():
    if _is_mac():
        return f"macOS {platform.mac_ver()[0]}".strip()
    name = "Linux"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{name} (Windows, WSL2)" if _is_wsl() else name


def _hint(name):
    mac = _is_mac()
    setup = "uv run python -m clipper setup --install"
    return {
        "ffmpeg": "brew install ffmpeg" if mac else "sudo apt install ffmpeg",
        "ffprobe": "kommt mit ffmpeg: " + ("brew install ffmpeg" if mac else "sudo apt install ffmpeg"),
        "yt-dlp": "./start.sh (yt-dlp gehört zu den Python-Paketen); scheitern Downloads: uv lock --upgrade-package "
                  "yt-dlp, dann den Server neu starten",
        "parakeet": "./start.sh (onnx-asr gehört zum Projekt); das Modell (~650 MB) lädt die erste Analyse von "
                    "huggingface.co",
        "whisper.cpp": "brew install whisper-cpp" if mac else f"{setup} (baut whisper.cpp)",
        "whisper-modell": f"{setup} (lädt das Modell, ~550 MB)",
        "demucs": "Server beenden, dann ./start.sh --stimmen (lädt PyTorch: Linux rund 3 GB, Mac rund 250 MB)",
        "whisperkit": "brew install whisperkit-cli" if mac else "nur auf macOS – unter Linux/WSL haben die Untertitel "
                                                                "keine Sprechererkennung",
        "espeak-ng": "sudo apt install espeak-ng",
        "say": "in macOS eingebaut",
        "python-pakete": "Server beenden, dann ./start.sh (installiert die Python-Pakete)",
        "python": "./start.sh (uv installiert ein passendes Python, wenn keins da ist)",
    }.get(name)


def _local_tools():
    """Fallback when the bridge can't run (e.g. clipper itself fails to import): the same names, found in-process."""
    path = runtime.clipper_env(store.get_setting("ui.default_profile"))["PATH"]
    which = {n: bool(shutil.which(n, path=path)) for n in ("ffmpeg", "ffprobe", "yt-dlp", "whisper-cli",
                                                             "whisperkit-cli", "demucs", "espeak-ng", "say")}
    mods = [m for m in ("yaml", "PIL", "numpy", "onnx_asr") if importlib.util.find_spec(m) is None]
    asr = pipeline.profile_config(store.get_setting("ui.default_profile")).get("asr", "parakeet")
    return {"which": which, "versions": {}, "asr": asr, "modules_missing": mods, "model_missing": False,
            "is_mac": _is_mac(), "report": None}


def _tools(data):
    which, ver = data.get("which") or {}, data.get("versions") or {}
    mods = set(data.get("modules_missing") or [])

    def row(name, ok, required, purpose, version=None):
        return {"name": name, "ok": bool(ok), "version": version, "hint": None if ok else _hint(name),
                "required": required, "purpose": purpose}
    rows = [
        row("ffmpeg", which.get("ffmpeg"), True, "Ton und Bild schneiden, Kontaktbögen, Rendern", ver.get("ffmpeg")),
        row("ffprobe", which.get("ffprobe"), True, "Videolänge und -format lesen", ver.get("ffprobe")),
        row("yt-dlp", which.get("yt-dlp"), True, "Videos und Live-Streams laden (YouTube, Twitch, Kick)",
            ver.get("yt-dlp")),
    ]
    if data.get("asr") == "whisper":
        rows.append(row("whisper.cpp", which.get("whisper-cli"), True, "Transkription (whisper.cpp, lokal)",
                        ver.get("whisper-cli")))
        rows.append(row("whisper-modell", not data.get("model_missing"), True, "Sprachmodell für whisper.cpp"))
    else:
        rows.append(row("parakeet", "onnx_asr" not in mods, True,
                        "Transkription (NVIDIA Parakeet über onnx-asr, lokal auf der CPU)", ver.get("onnx_asr")))
    py_missing = sorted(mods - {"onnx_asr"})
    rows.append(row("python-pakete", not py_missing, True,
                    "PyYAML, Pillow, NumPy" + (f" (fehlt: {', '.join(py_missing)})" if py_missing else "")))
    rows.append(row("python", sys.version_info >= (3, 10), True, "Python für Server und Clipper",
                    platform.python_version()))
    rows.append(row("demucs", which.get("demucs"), False,
                    "trennt Stimmen vom Spielsound: sauberere Untertitel; die Synchronisation braucht es",
                    ver.get("demucs")))
    rows.append(row("whisperkit", which.get("whisperkit-cli"), False, "erkennt, wer spricht (nur macOS)",
                    ver.get("whisperkit-cli")))
    if data.get("is_mac", _is_mac()):
        rows.append(row("say", True, False, "Systemstimme fürs Voiceover ohne Fish-Audio-Schlüssel"))
    else:
        rows.append(row("espeak-ng", which.get("espeak-ng"), False,
                        "Systemstimme fürs Voiceover ohne Fish-Audio-Schlüssel", ver.get("espeak-ng")))
    return rows


def _disk():
    target = paths.jobs_dir() if paths.jobs_dir().exists() else paths.ROOT
    try:
        du = shutil.disk_usage(target)
        free, total = du.free / 1e9, du.total / 1e9
    except OSError:
        free = total = None
    need = pipeline.profile_config(store.get_setting("ui.default_profile")).get("min_free_gb") or 0
    return {"path": str(target), "free_gb": None if free is None else round(free, 1),
            "total_gb": None if total is None else round(total, 1), "min_free_gb": float(need)}


def system_report(refresh=False):
    """What is installed on this machine and how to get what's missing (German hints per platform)."""
    with _sys_lock:
        if not refresh and _sys_cache["data"] and time.monotonic() - _sys_cache["at"] < SYSTEM_TTL_S:
            cached = copy.deepcopy(_sys_cache["data"])
            cached["disk"] = _disk()
            return cached
        try:
            data = runtime.run_bridge("system_tools", {}, store.get_setting("ui.default_profile"), timeout=120)
            report = data.get("report")
        except runtime.BridgeError as e:
            data = _local_tools()
            report = f"`clipper setup` lief nicht: {e.message}"
        out = {"version": __version__, "python": platform.python_version(), "platform": _platform_label(),
               "paths": {"root": str(paths.ROOT), "data": str(paths.data_dir()), "database": str(paths.db_path()),
                         "jobs": str(paths.jobs_dir()), "exports": str(paths.exports_dir()),
                         "logs": str(paths.logs_dir())},
               "disk": _disk(), "tools": _tools(data), "setup_report": report, "asr": data.get("asr")}
        _sys_cache.update(at=time.monotonic(), data=out)
        return copy.deepcopy(out)


def readiness():
    """The "Bereit zum Start" checklist (sources = active creators + streams of enabled profiles)."""
    rep = system_report()
    profiles = store.enabled_profiles()
    sources = sum(len(store.list_creators(p, include_paused=False)) + len(store.list_streams(p, include_paused=False))
                  for p in profiles)
    return {"anthropic_key": bool(store.get_secret("anthropic_api_key")),
            "fish_key": bool(store.get_secret("fish_api_key")),
            "youtube_connected": any(a.get("connected") for a in store.list_accounts("youtube")),
            "tools_ok": all(t["ok"] for t in rep["tools"] if t.get("required")),
            "sources": sources, "automation": bool(store.get_setting("automation.enabled"))}


# ---------------------------------------------------------------- live status cache

def _cache():
    global _live
    if _live is None:
        _live = store.get_meta("live.status") or {}
    return _live


def store_live(profile, data, error=None):
    """The live service's result for one profile (`live_tick` bridge output), or the error of a failed tick."""
    with _live_lock:
        cache = _cache()
        old = cache.get(profile) or {}
        if data is None:
            cache[profile] = {**old, "error": error, "failed_at": time.time()}
        else:
            cache[profile] = {"streams": data.get("streams") or [], "issues": data.get("issues") or {},
                              "stalled": data.get("stalled") or [], "restarted": data.get("restarted") or [],
                              "recorders": int(data.get("recorders") or 0), "checked_at": time.time(), "error": None}
        store.set_meta("live.status", cache)


def forget_live(profile):
    with _live_lock:
        cache = _cache()
        if cache.pop(profile, None) is not None:
            store.set_meta("live.status", cache)


def live_status():
    with _live_lock:
        cache = copy.deepcopy(_cache())
    streams = []
    for row in store.list_streams():
        entry = cache.get(row["profile"]) or {}
        st = next((s for s in entry.get("streams") or [] if s.get("name") == row["name"] or s.get("url") == row["url"]),
                  None)
        streams.append({"id": row["id"], "name": row["name"], "url": row["url"], "profile": row["profile"],
                        "platform": row.get("platform"), "live": st.get("live") if st else None,
                        "on_topic": st.get("on_topic") if st else None,
                        "recording": st.get("recording") if st else None, "title": st.get("title") if st else None,
                        "checked_at": entry.get("checked_at") if st else None})
    checked = [e.get("checked_at") for e in cache.values() if e.get("checked_at")]
    return {"streams": streams, "recorders": max([e.get("recorders") or 0 for e in cache.values()] or [0]),
            "checked_at": max(checked) if checked else None}


# ---------------------------------------------------------------- health checks

def gb_text(value):
    """German, with one decimal (29.6 free must not read "30 GB" next to a 30 GB minimum)."""
    return f"{float(value):.1f} GB".replace(".", ",").replace(",0 GB", " GB")


def _names(items):
    return ", ".join(items[:8]) + (f" und {len(items) - 8} weitere" if len(items) > 8 else "")


def _live_checks(put, now):
    if not store.get_setting("automation.live_enabled"):
        for key in LIVE_KEYS:
            put(key, False)
        return
    with _live_lock:
        cache = copy.deepcopy(_cache())
    interval = 60 * int(store.get_setting("schedule.live_watch_minutes"))
    stale, errors, missed, stalled, restarted, failed, details = [], [], [], [], [], [], {}
    for p in store.enabled_profiles():
        if not store.list_streams(p, include_paused=False):
            continue
        entry = cache.get(p) or {}
        if entry.get("error"):
            errors.append(f"{p}: {entry['error']}")
        if not entry.get("checked_at") or now - entry["checked_at"] > 3 * interval + 120:
            stale.append(p)
        issues = entry.get("issues") or {}
        details[p] = issues
        if "not_recording" in issues:
            missed += [s["name"] for s in entry.get("streams") or []
                       if s.get("live") and s.get("on_topic") and not s.get("recording")] or [p]
        if "stalled" in issues:
            stalled += entry.get("stalled") or [p]
            restarted += entry.get("restarted") or []
        if "analysis_failed" in issues:
            failed.append(issues["analysis_failed"].split(": ", 1)[-1])
    put("live.service", bool(stale), "warning",
        "Der Live-Dienst hat " + (f"für {_names(stale)} " if stale else "") + "länger keine Streams geprüft"
        + (f" ({'; '.join(errors)[:300]})" if errors else "") + ". Unter System siehst du, ob yt-dlp und ffmpeg "
        "installiert sind; die Server-Logs liegen unter logs/automate/.", {"profiles": stale, "errors": errors})
    put("live.not_recording", bool(missed), "error",
        f"Live-Streams mit GTA 6 werden nicht aufgenommen: {_names(missed)}. Häufige Ursachen: yt-dlp oder ffmpeg "
        "fehlt (siehe System) oder die Höchstzahl gleichzeitiger Aufnahmen (max_recorders) ist erreicht.",
        {"streams": missed, "issues": details})
    put("live.stalled", bool(stalled), "warning",
        f"Aufnahme hängt (seit über 10 Minuten kein neues Video): {_names(stalled)}. "
        + ("Sie wurde automatisch neu gestartet. " if restarted else "")
        + "Hilft das nicht, pausiere den Stream unter Quellen kurz und schalte ihn wieder ein.",
        {"streams": stalled, "restarted": restarted})
    put("live.analysis_failed", bool(failed), "warning",
        f"Die Analyse von Live-Stücken ist 3-mal gescheitert: {'; '.join(failed)[:300]}. Prüfe unter System, ob die "
        "Transkription installiert ist; Details stehen in jobs/_live/reanalyze.log.", {"chunks": failed})


def check_all(now=None):
    """Raise or resolve every alert this module owns; returns what changed (and records a `health` run)."""
    now = time.time() if now is None else now
    rid = store.start_run("health", None, "schedule")
    before = {a["key"] for a in store.list_alerts()}
    problems = []

    def put(key, active, level="warning", message="", details=None):
        if active:
            store.raise_alert(key, level, message, details)
        else:
            store.resolve_alert(key)

    try:
        disk = _disk()
        low = disk["free_gb"] is not None and disk["free_gb"] < disk["min_free_gb"]
        put("disk.low", low, "error" if low and disk["free_gb"] < 5 else "warning",
            f"Nur noch {gb_text(disk['free_gb'] or 0)} frei auf der Festplatte (empfohlen mindestens "
            f"{gb_text(disk['min_free_gb'])}). Alte Quellvideos löscht das Aufräumen automatisch; lösche sonst alte "
            "Exporte unter exports/ oder senke min_free_gb in den Analyse-Einstellungen.", disk)

        rep = system_report(refresh=bool({"tools.missing", "tools.voice"} & before))  # an install shows at once
        missing = [t["name"] for t in rep["tools"] if t.get("required") and not t["ok"]]
        put("tools.missing", bool(missing), "error",
            f"Es fehlen Programme, ohne die der Server nicht arbeiten kann: {', '.join(missing)}. Unter System steht, "
            "wie du sie installierst.", {"missing": missing,
                                          "hints": {t["name"]: t["hint"] for t in rep["tools"] if not t["ok"]}})
        fish = bool(store.get_secret("fish_api_key"))
        voice_tool = next((t for t in rep["tools"] if t["name"] in ("say", "espeak-ng")), {"ok": True})
        no_voice = (not fish and store.get_setting("render.allow_system_voice") and not voice_tool["ok"]
                    and store.get_setting("prefs.voice") != "off")
        put("tools.voice", no_voice, "warning",
            "Für Voiceovers fehlt eine Stimme: trage den Fish-Audio-Schlüssel ein oder installiere espeak-ng "
            "(„sudo apt install espeak-ng“). Bis dahin scheitern Clips mit Voiceover beim Rendern.")
        langs = store.get_setting("dub.auto_languages") or []
        put("key.fish_missing", bool(langs) and not fish, "warning",
            f"Automatische Synchronisation ({', '.join(langs)}) ist eingestellt, aber der Fish-Audio-Schlüssel fehlt "
            "– es wird nichts synchronisiert. Trage ihn unter „Kosten & Schlüssel“ ein.")

        pipeline.resume_waiting()  # also syncs key.anthropic_missing / budget.reached

        _live_checks(put, now)

        for a in store.list_alerts():
            if a["key"].startswith("job.failed."):
                try:
                    job = store.get_job(int(a["key"].rsplit(".", 1)[1]))
                except ValueError:
                    job = None
                if not job or job["status"] != "failed":
                    store.resolve_alert(a["key"])
    except Exception as e:  # a broken check must not stop the others next time; it shows in the run
        problems.append(repr(e)[:300])
    active = store.list_alerts()
    after = {a["key"] for a in active}
    changes = [{"key": a["key"], "action": "raised", "level": a["level"], "message": a["message"]}
               for a in active if a["key"] not in before]
    changes += [{"key": k, "action": "resolved", "level": None, "message": None} for k in sorted(before - after)]
    counts = {lvl: sum(1 for a in active if a["level"] == lvl) for lvl in ("error", "warning", "info")}
    if problems:
        msg = f"Prüfung unvollständig: {problems[0]}"
    elif not active:
        msg = "Alles in Ordnung"
    else:
        msg = ", ".join(f"{n} {label}" for n, label in ((counts["error"], "Fehler"), (counts["warning"], "Warnung(en)"),
                                                       (counts["info"], "Hinweis(e)")) if n) + " aktiv"
    store.finish_run(rid, not problems, {"message": msg, "active": sorted(after), "changes": changes,
                                         "problems": problems})
    return changes
