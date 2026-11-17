"""Übersicht, automation switches, alerts, jobs, "Jetzt suchen" and the system report.

/api/overview is polled every few seconds by every open dashboard: it runs a handful of indexed queries, reads the
live status the live service cached (health.live_status()) and the last system report this process computed
(tools), and never starts a subprocess. GET /api/system computes a fresh report (health.system_report(), which may
run tool version checks) and refreshes that cache; the server also warms it once at startup.
"""
import logging
import platform
import re
import shutil
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Body, Query

from .. import __version__, costs, paths, runtime, social, store
from .common import Form, changed, conflict, http_url, invalid, mapped, need, not_found, optional, page
from .picks import enrich_many

log = logging.getLogger("automate.api")
router = APIRouter()

JOB_KEYS = ("id", "kind", "profile", "status", "group", "stage", "progress", "error", "title", "channel", "source_url",
            "clipper_job", "attempts", "created_at", "updated_at", "started_at", "finished_at", "parent_id", "run_id",
            "section", "priority")
LIVE_KEYS = ("live", "on_topic", "recording", "title", "checked_at")
SCAN_STALE_S = 3 * 3600  # an unfinished scan run older than this is treated as crashed
SECTION_RE = r"^\d{1,2}:\d{2}(:\d{2})?-\d{1,2}:\d{2}(:\d{2})?$"

_system = {"report": None, "at": 0.0}
_system_lock = threading.Lock()


PRIVATE_PARAMS = ("upload_session",)  # a YouTube resumable session URI works like a token for that upload


def job_out(job, detail=False):
    out = {k: job.get(k) for k in JOB_KEYS}
    if detail:
        out["params"] = {k: v for k, v in (job.get("params") or {}).items() if k not in PRIVATE_PARAMS}
        out["result"] = job.get("result")
    return out


def _job(job_id):
    job = store.get_job(job_id)
    if job is None:
        raise not_found("Diesen Job gibt es nicht.")
    return job


def default_profile():
    enabled = store.enabled_profiles()
    want = store.get_setting("ui.default_profile")
    return want if want in enabled or not enabled else enabled[0]


def profile_field(form, name="profile", required=False):
    """A known profile name from the body (422 otherwise); the default profile when absent."""
    p = form.str(name, required=required, max_len=64)
    if p and store.get_profile(p) is None:
        form.error(name, f"Unbekanntes Profil „{p}“.")
    return p or None


# ---------------------------------------------------------------- live status, tools (cached elsewhere)

def live_status():
    """({stream id: status}, recorders) from the live service's cache; empty when it isn't running."""
    health = optional("health")
    st = None
    if health is not None and hasattr(health, "live_status"):
        try:
            st = health.live_status()
        except Exception:
            log.exception("health.live_status failed")
    st = st if isinstance(st, dict) else {}
    by_key = {}
    for s in st.get("streams") or []:
        if isinstance(s, dict):
            by_key[("id", s.get("id"))] = s
            by_key[("url", s.get("url"))] = s
    return by_key, int(st.get("recorders") or 0)


def with_live(stream, by_key):
    s = dict(stream)
    st = by_key.get(("id", stream["id"])) or by_key.get(("url", stream["url"])) or {}
    for k in LIVE_KEYS:
        s[k] = st.get(k)
    return s


def _disk_gb(path):
    try:
        u = shutil.disk_usage(path)
        return round(u.free / 1e9, 1), round(u.total / 1e9, 1)
    except OSError:
        return None, None


def _paths():
    return {"root": str(paths.ROOT), "data": str(paths.data_dir()), "database": str(paths.db_path()),
            "runtime": str(paths.runtime_dir()), "jobs": str(paths.jobs_dir()), "exports": str(paths.exports_dir()),
            "logs": str(paths.logs_dir())}


def _fallback_report(note):
    free, total = _disk_gb(paths.ROOT)
    try:
        min_free = runtime.watchlist_config()["effective"].get("min_free_gb")
    except Exception:
        min_free = None
    return {"version": __version__, "python": platform.python_version(), "platform": platform.platform(),
            "paths": _paths(), "disk": {"path": str(paths.ROOT), "free_gb": free, "total_gb": total,
                                        "min_free_gb": min_free},
            "tools": [], "setup_report": note}


def refresh_system_report():
    health = optional("health")
    if health is None or not hasattr(health, "system_report"):
        return _fallback_report("Die Werkzeug-Prüfung ist noch nicht verfügbar (automate.health fehlt).")
    try:
        try:
            report = dict(health.system_report(refresh=True))  # "Erneut prüfen" must not get a cached answer
        except TypeError:
            report = dict(health.system_report())
    except Exception:
        log.exception("health.system_report failed")
        return _fallback_report("Die Werkzeug-Prüfung ist fehlgeschlagen. Details stehen im Server-Log.")
    report.setdefault("version", __version__)
    report["paths"] = {**_paths(), **(report.get("paths") or {})}
    with _system_lock:
        _system.update(report=report, at=time.time())
    return report


def tools_state():
    """(tools_ok, missing names) from the last system report; before the first one, from the health alert."""
    with _system_lock:
        report = _system["report"]
    if report is not None:
        missing = [t.get("name") for t in report.get("tools") or []
                   if isinstance(t, dict) and not t.get("ok") and t.get("required", True)]
        return not missing, missing
    alert = store.get_alert("tools.missing")
    if alert and alert.get("resolved_at") is None:
        details = alert.get("details") or {}
        return False, list(details.get("missing") or []) if isinstance(details, dict) else []
    return True, []


# ---------------------------------------------------------------- overview, automation, alerts

def _scan_running():
    """pipeline.scan_running() (its in-process scan locks); without the pipeline, an unfinished recent scan run."""
    pipeline = optional("pipeline")
    if pipeline is not None and hasattr(pipeline, "scan_running"):
        try:
            return bool(pipeline.scan_running())
        except Exception:
            log.exception("pipeline.scan_running failed")
    run = store.last_run("scan")
    return bool(run and run.get("finished_at") is None and time.time() - run["started_at"] < SCAN_STALE_S)


def automation_block(values=None):
    s = values or store.all_settings()
    last = store.last_run("scan", finished_only=True)
    scheduler = optional("scheduler")
    next_at = None
    if scheduler is not None and hasattr(scheduler, "next_scan_time"):
        try:
            next_at = scheduler.next_scan_time()
        except Exception:
            log.exception("scheduler.next_scan_time failed")
    return {"enabled": bool(s["automation.enabled"]), "live_enabled": bool(s["automation.live_enabled"]),
            "next_scan_at": next_at, "scan_running": _scan_running(),
            "last_scan": {"at": last["finished_at"], "ok": bool(last["ok"]), "summary": last.get("summary")}
            if last else None}


def _day_start():
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@router.get("/overview")
def overview():
    s = store.all_settings()
    groups = store.job_group_counts()
    statuses = store.job_counts()
    enabled = set(store.enabled_profiles())
    by_key, recorders = live_status()
    streams = [with_live(x, by_key) for x in store.list_streams(include_paused=False) if x["profile"] in enabled]
    creators = [c for c in store.list_creators(include_paused=False) if c["profile"] in enabled]
    today = store.usage_today()
    tools_ok, missing = tools_state()
    free, _ = _disk_gb(paths.jobs_dir() if paths.jobs_dir().exists() else paths.ROOT)
    # waiting_approval jobs at stage "render" wait for their own renders (auto approval), not for the owner
    for_owner = sum(1 for j in store.list_jobs(status="waiting_approval", limit=10000)[0] if j["stage"] == "approval") \
        if statuses["waiting_approval"] else 0
    return {
        "jobs": {"waiting": groups["waiting"], "running": groups["running"], "failed": groups["failed"],
                 "waiting_approval": for_owner,
                 "done_today": store.count_jobs(status=list(store.JOB_GROUPS["done"]), finished_since=_day_start())},
        "automation": automation_block(s),
        "live": {"streams": [{k: x.get(k) for k in ("id", "name", "url", "profile", "platform", *LIVE_KEYS)}
                             for x in streams], "recorders": recorders},
        "alerts": store.list_alerts(active_only=True),
        "costs": {"today_eur": today["eur"], "limit_eur": s["claude.daily_limit_eur"], "tokens_today": today["tokens"]},
        "approvals_pending": store.pick_counts()["proposed"],
        "videos_today": store.list_videos(day=costs.local_day(), archived=None, limit=0)[1],
        "system": {"disk_free_gb": free, "tools_ok": tools_ok, "missing": missing},
        "recent_jobs": [job_out(j) for j in store.list_jobs(limit=8)[0]],
        "ready": {"anthropic_key": store.secret_info("anthropic_api_key")["set"],
                  "fish_key": store.secret_info("fish_api_key")["set"],
                  "youtube_connected": social.youtube_connected(), "tools_ok": tools_ok,
                  "sources": len(streams) + len(creators)},
    }


@router.post("/automation")
def set_automation(body: dict | None = Body(None)):
    form = Form(body)
    changes = {}
    for key in ("enabled", "live_enabled"):
        v = form.bool(key)
        if v is not None:
            changes[f"automation.{key}"] = v
    if not changes and not form.errors:
        form.error("enabled", "Bitte enabled oder live_enabled angeben.")
    form.check()
    store.set_settings(changes)
    changed(list(changes), overlay=False)
    return automation_block()


@router.get("/alerts")
def list_alerts(active: int = Query(1)):
    items = store.list_alerts(active_only=bool(active))
    return {"items": items, "total": len(items)}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    if not store.ack_alert(alert_id):
        raise not_found("Diesen Alarm gibt es nicht.")
    return next(a for a in store.list_alerts(active_only=False) if a["id"] == alert_id)


@router.get("/system")
def system():
    return refresh_system_report()


# ---------------------------------------------------------------- jobs, scan

@router.get("/jobs")
def list_jobs(group: str | None = None, kind: str | None = None, profile: str | None = None,
              limit: int | None = Query(None), offset: int | None = Query(None)):
    limit, offset = page(limit, offset)
    errors = {}
    if group and group not in store.JOB_GROUPS:
        errors["group"] = f"Unbekannte Gruppe (eine von {', '.join(store.JOB_GROUPS)})."
    if kind and kind not in store.JOB_KINDS:
        errors["kind"] = f"Unbekannte Art (eine von {', '.join(store.JOB_KINDS)})."
    if errors:
        raise invalid(errors)
    rows, total = store.list_jobs(kind=kind or None, profile=profile or None, limit=limit, offset=offset,
                                  group=group or None)
    return {"items": [job_out(j) for j in rows], "total": total,
            "counts": store.job_group_counts(kind=kind or None, profile=profile or None)}


@router.get("/jobs/{job_id}")
def get_job(job_id: int):
    job = _job(job_id)
    return {**job_out(job, detail=True), "events": store.job_events(job_id, 200),
            "picks": enrich_many(store.list_picks(job_id=job_id, limit=200)[0]),
            "children": [job_out(j) for j in store.list_jobs(parent_id=job_id, limit=100)[0]]}


@router.post("/jobs")
def create_job(body: dict | None = Body(None)):
    form = Form(body)
    url = form.str("url", required=True, max_len=2000, label="Die Video-URL")
    if url and not http_url(url):
        form.error("url", "Bitte eine vollständige Video-URL angeben (https://…).")
    section = form.str("section", max_len=40)
    if section:
        if not re.match(SECTION_RE, section.replace(" ", "")):
            form.error("section", "Format H:MM:SS-H:MM:SS, z. B. 0:00:00-1:30:00.")
        section = section.replace(" ", "")
    profile = profile_field(form)
    form.check()
    pipeline = need("pipeline")
    with mapped():
        job_id = pipeline.enqueue_url(url, profile or default_profile(), section or None, "dashboard")
    return job_out(_job(job_id))


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int):
    _job(job_id)
    pipeline = need("pipeline")
    with mapped():
        pipeline.retry_job(job_id)
    return job_out(_job(job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    _job(job_id)
    pipeline = need("pipeline")
    with mapped():
        pipeline.cancel_job(job_id)
    return job_out(_job(job_id))


@router.post("/scan")
def scan(body: dict | None = Body(None)):
    form = Form(body)
    profile = profile_field(form)
    form.check()
    if _scan_running():
        raise conflict("scan_running", "Es läuft schon eine Suche. Bitte warten, bis sie fertig ist.")
    if not profile and not store.enabled_profiles():
        raise conflict("no_profiles", "Kein Profil ist aktiv. Bitte unter Quellen ein Profil einschalten.")
    pipeline = need("pipeline")
    with mapped():
        pipeline.request_scan(profile, "dashboard")
    where = f" im Profil {profile}" if profile else ""
    return {"ok": True, "message": f"Suche{where} gestartet – neue Videos erscheinen gleich unter Jobs."}
