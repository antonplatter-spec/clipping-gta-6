"""The scheduler: daily scans, the live service, the daily cleanup and the health checks, as daemon threads that
re-read the settings on every tick (a dashboard change applies without a restart). Replaces the launchd/systemd
timers of clipper/schedule.py (nothing is installed in the OS).

    scheduler       every TICK_S: resume jobs waiting for a key/budget; per enabled profile, when
                    automation.enabled, run the scan of the latest passed slot of schedule.scan_times if no scan
                    started since (a slot missed while the PC slept or was off runs once on wake, never once per
                    missed slot; clock changes only move which slot is "latest"); once a day `clipper cleanup`
                    and pruning of data/tmp (agent conversation copies, one-off picks/translation files older
                    than TMP_KEEP_DAYS)
    live-service    every schedule.live_watch_minutes when automation.live_enabled: per enabled profile with active
                    streams the bridge op `live_tick` (= `clipper live watch --chunk N` + cleanup + `live health`
                    with one status probe of the streams), then a live_chunk job per newly analyzed chunk; the
                    stream status is cached for health.live_status()
    health-monitor  every schedule.health_minutes: health.check_all()
All runs are recorded in `runs` (kind scan | live_watch | cleanup | health) with a German summary.

    start_scheduler(stop) -> [threads] ; next_scan_time(now=None) -> float | None ; run_scan_now(profile=None)
    scan_running(profile=None) -> bool   a scan (scheduled or "Jetzt suchen") is in progress
    due_scan(profile, now=None) -> slot timestamp | None ; tick(now=None) ; live_tick(trigger="schedule")
    run_cleanup(trigger="schedule", dry_run=False) -> summary ; prune_tmp(days=TMP_KEEP_DAYS) -> files ; wake()
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from . import db, health, paths, pipeline, runtime, store

log = logging.getLogger("automate.scheduler")

TICK_S = 20.0
SLOT_GRACE_S = 15 * 60       # a scan started up to this long before a slot counts for it
CATCH_UP_AFTER_S = 10 * 60   # a slot run later than this is labelled "catch_up" (e.g. after sleep)
CLEANUP_EVERY_S = 24 * 3600
T_LIVE_TICK = 20 * 60
TMP_KEEP_DAYS = 5

_wake_event = threading.Event()
_cleanup_lock = threading.Lock()
_cleanup_runner = None
_live_lock = threading.Lock()


def wake():
    _wake_event.set()


# ---------------------------------------------------------------- scan slots

def _slot_ts(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m).timestamp()


def _slots(day):
    return sorted(_slot_ts(day, t) for t in store.get_setting("schedule.scan_times") or [])


def _last_scan(profile):
    """The newest scan of `profile` that counts (an interrupted one, cut off by a restart, doesn't)."""
    for r in store.list_runs("scan", limit=100):
        if r["profile"] == profile and not (r.get("summary") or {}).get("interrupted"):
            return r
    return None


def due_scan(profile, now=None):
    """The latest of today's slots that has passed, if no scan of `profile` started since (minus SLOT_GRACE_S)."""
    now = time.time() if now is None else now
    passed = [ts for ts in _slots(datetime.fromtimestamp(now).date()) if ts <= now]
    if not passed:
        return None
    slot = passed[-1]
    last = _last_scan(profile)
    if last and last["started_at"] >= slot - SLOT_GRACE_S:
        return None
    return slot


def next_scan_time(now=None):
    """When the next scheduled scan runs (now, if one is due), None when automation is off or no profile is on."""
    if not store.get_setting("automation.enabled"):
        return None
    profiles = store.enabled_profiles()
    if not profiles:
        return None
    now = time.time() if now is None else now
    if any(due_scan(p, now) is not None for p in profiles):
        return now
    today = datetime.fromtimestamp(now).date()
    upcoming = [ts for ts in _slots(today) if ts > now] or _slots(today + timedelta(days=1))
    return upcoming[0] if upcoming else None


def scan_running(profile=None):
    return pipeline.scan_running(profile)


def run_scan_now(profile=None):
    """Dashboard "Jetzt suchen" (same as pipeline.request_scan)."""
    return pipeline.request_scan(profile, trigger="dashboard")


# ---------------------------------------------------------------- cleanup

def prune_tmp(days=TMP_KEEP_DAYS, now=None):
    """Scratch files under data/tmp older than `days` (the agent's conversation copies, one-off picks and translation
    files; a stage that still needs one writes it again), and directories left empty that long."""
    now = time.time() if now is None else now
    root, removed = paths.tmp_dir(), 0
    if not root.is_dir():
        return 0
    ages = {}
    for f in root.rglob("*"):  # every age first: deleting a file refreshes its folder's mtime
        try:
            ages[f] = now - f.lstat().st_mtime
        except OSError:
            continue
    for f in sorted(ages, key=lambda p: len(p.parts), reverse=True):  # deepest first
        try:
            old = ages[f] > days * 86400
            if f.is_dir() and not f.is_symlink():
                if old and not any(f.iterdir()):
                    f.rmdir()
            elif old:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def run_cleanup(trigger="schedule", dry_run=False, profile=None):
    """`clipper cleanup`: source.mp4/audio.wav past their keep time (transcripts, picks and exports stay)."""
    profile = profile or store.get_setting("ui.default_profile")
    rid = store.start_run("cleanup", profile, trigger)
    res = runtime.run_clipper(["cleanup"] + (["--dry-run"] if dry_run else []), profile,
                              timeout=pipeline.T_CLEANUP)
    try:
        data = pipeline.trailing_json(res.stdout) if res.returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        summary = {"message": f"Aufräumen fehlgeschlagen: {pipeline._tail(res)}"}
        store.finish_run(rid, False, summary)
        return summary
    files, gb = int(data.get("files") or 0), float(data.get("gb") or 0)
    pruned = 0 if dry_run else prune_tmp()
    verb = "würde löschen" if dry_run else "gelöscht"
    msg = (f"{files} Quelldatei(en) {verb} ({gb:.1f} GB)" if files else "Nichts zu löschen") \
        + (f", {pruned} alte Hilfsdatei(en) entfernt" if pruned else "")
    summary = {"message": msg, **data, "tmp_files": pruned}
    store.finish_run(rid, True, summary)
    return summary


def _cleanup_due(now):
    last = store.last_run("cleanup")
    return last is None or now - last["started_at"] >= CLEANUP_EVERY_S


def _cleanup_thread(trigger):
    try:
        run_cleanup(trigger)
    except Exception:
        log.exception("cleanup failed")
    finally:
        _cleanup_lock.release()
        db.close()


# ---------------------------------------------------------------- the scheduler tick

def tick(now=None, stop=None):
    """One pass: resume waiting jobs, start due scans, start the daily cleanup. Returns the started run ids."""
    now = time.time() if now is None else now
    started = []
    try:
        pipeline.resume_waiting()
    except Exception:
        log.exception("resume_waiting failed")
    if store.get_setting("automation.enabled"):
        for p in store.enabled_profiles():
            slot = due_scan(p, now)
            if slot is None or pipeline.scan_running(p):
                continue
            trigger = "schedule" if now - slot < CATCH_UP_AFTER_S else "catch_up"
            try:
                started += pipeline.request_scan(p, trigger=trigger, stop=stop)
            except pipeline.PipelineError:
                pass
    if _cleanup_due(now) and _cleanup_lock.acquire(blocking=False):
        global _cleanup_runner
        _cleanup_runner = threading.Thread(target=_cleanup_thread, args=("schedule",), name="cleanup", daemon=True)
        _cleanup_runner.start()
    return started


def _mark_interrupted_runs():
    """Runs a dead server left open: closed as interrupted, so the scan of that slot runs again."""
    for r in store.list_runs(limit=200):
        if r.get("finished_at") is None and not pipeline.scan_running(r.get("profile")):
            store.finish_run(r["id"], False, {**(r.get("summary") or {}), "interrupted": True,
                                              "message": "Unterbrochen (Server wurde beendet)"})


def _scheduler_loop(stop):
    try:
        _mark_interrupted_runs()
        while not stop.is_set():
            try:
                tick(stop=stop)
            except Exception:
                log.exception("scheduler tick failed")
            _wake_event.wait(TICK_S)
            _wake_event.clear()
    finally:
        db.close()


# ---------------------------------------------------------------- live service

def live_tick(trigger="schedule", stop=None):
    """Watch every enabled profile's active streams once (see the module doc). Returns {profile: summary}."""
    if not _live_lock.acquire(blocking=False):
        return {}
    try:
        chunk = int(store.get_setting("schedule.live_chunk_minutes"))
        out = {}
        for p in store.enabled_profiles():
            if stop is not None and stop.is_set():
                break
            if not store.list_streams(p, include_paused=False):
                health.forget_live(p)
                continue
            out[p] = _live_profile(p, chunk, trigger, stop)
        return out
    finally:
        _live_lock.release()


def _live_profile(profile, chunk, trigger, stop):
    rid = store.start_run("live_watch", profile, trigger)
    try:
        data = runtime.run_bridge("live_tick", {"chunk": chunk, "health": True, "restart_stalled": True}, profile,
                                  timeout=T_LIVE_TICK, cancel=stop)
    except runtime.BridgeError as e:
        if e.canceled:
            msg = "Live-Check unterbrochen"
        elif "yt-dlp" in e.message and ("No such file" in e.message or "not found" in e.message):
            msg = f"Live-Check fehlgeschlagen: {pipeline.YTDLP_MISSING}"
        else:
            msg = f"Live-Check fehlgeschlagen: {e.message}"
        health.store_live(profile, None, error=msg)
        store.finish_run(rid, False, {"message": msg, "interrupted": bool(e.canceled)})
        return {"message": msg}
    new = pipeline.enqueue_live_chunks(profile, data.get("ready"))
    closed = pipeline.close_live_chunks(profile, data.get("ready"))
    health.store_live(profile, data)
    streams = data.get("streams") or []
    live = [s for s in streams if s.get("live") and s.get("on_topic")]
    parts = [f"{len(live)} von {len(streams)} Stream(s) live mit GTA 6" if streams else "Keine Streams"]
    if data.get("started"):
        parts.append(f"Aufnahme gestartet: {', '.join(data['started'])}")
    if data.get("restarted"):
        parts.append(f"hängende Aufnahme neu gestartet: {', '.join(data['restarted'])}")
    if new:
        parts.append(f"{len(new)} neue(s) Stück(e) zum Clippen")
    if data.get("transcribing"):
        parts.append(f"{len(data['transcribing'])} Stück(e) in Analyse")
    cleaned = data.get("cleaned") or {}
    if cleaned.get("files"):
        parts.append(f"{cleaned['files']} alte Datei(en) gelöscht ({float(cleaned.get('gb') or 0):.1f} GB)")
    summary = {"message": ", ".join(parts), "started": data.get("started") or [], "new_jobs": new,
               "closed": closed, "recorders": data.get("recorders"), "issues": data.get("issues") or {},
               "restarted": data.get("restarted") or []}
    store.finish_run(rid, True, summary)
    return summary


def _live_loop(stop):
    last = None
    try:
        while not stop.wait(5):
            try:
                if not store.get_setting("automation.live_enabled"):
                    last = None
                    continue
                interval = 60 * int(store.get_setting("schedule.live_watch_minutes"))
                if last is None or time.monotonic() - last >= interval:
                    last = time.monotonic()
                    live_tick(stop=stop)
            except Exception:
                log.exception("live tick failed")
    finally:
        db.close()


# ---------------------------------------------------------------- health

def _health_loop(stop):
    last = None
    try:
        if stop.wait(10):  # let the workers and the first ticks start
            return
        while not stop.is_set():
            try:
                interval = 60 * int(store.get_setting("schedule.health_minutes"))
                if last is None or time.monotonic() - last >= interval:
                    last = time.monotonic()
                    health.check_all()
            except Exception:
                log.exception("health check failed")
            stop.wait(10)
    finally:
        db.close()


def start_scheduler(stop):
    """Start the scheduler, live-service and health-monitor threads (daemons; they end soon after `stop`)."""
    threads = [threading.Thread(target=fn, args=(stop,), name=name, daemon=True)
               for name, fn in (("scheduler", _scheduler_loop), ("live-service", _live_loop),
                                ("health-monitor", _health_loop))]
    for t in threads:
        t.start()

    def unblock():
        stop.wait()
        wake()
    threading.Thread(target=unblock, name="scheduler-stop", daemon=True).start()
    return threads
