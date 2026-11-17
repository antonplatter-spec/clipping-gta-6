"""The scheduler: daily scans, the live service, the daily cleanup and the health checks, as daemon threads that
re-read the settings on every tick (a dashboard change applies without a restart). Replaces the launchd/systemd
timers of clipper/schedule.py (nothing is installed in the OS).

    scheduler       every TICK_S: resume jobs waiting for a key/budget; per enabled profile, when
                    automation.enabled, run the scan of the latest passed slot of schedule.scan_times if no scan
                    started since (a slot missed while the PC slept or was off runs once on wake, never once per
                    missed slot; clock changes only move which slot is "latest"). A scan that failed (no network,
                    no source reachable) is retried every SCAN_RETRY_S, up to SCAN_RETRIES times per slot, instead of
                    losing the day's slot. No scan starts within NETWORK_GRACE_S after the server started or the PC
                    woke up (the network may not be back yet). Once a day `clipper cleanup`, pruning of data/tmp
                    (agent conversation copies, one-off picks/translation files older than TMP_KEEP_DAYS) and of old
                    DB rows (prune_db: job output lines, health/live/scan runs)
    live-service    every schedule.live_watch_minutes when automation.live_enabled: per enabled profile with active
                    streams the bridge op `live_tick` (= `clipper live watch --chunk N` + cleanup + `live health`
                    with one status probe of the streams; no new recorder while the disk is below min_free_gb),
                    then a live_chunk job per newly analyzed chunk; the stream status is cached for
                    health.live_status(). Recorders of streams that are no longer active (paused, deleted, profile
                    off, live switched off) are stopped: stop_inactive_recorders(), also called by
                    pipeline.on_settings_changed right after such a change.
    health-monitor  every schedule.health_minutes: health.check_all()
A watchdog restarts any of these threads that dies (alert service.restarted). All runs are recorded in `runs`
(kind scan | live_watch | cleanup | health) with a German summary.

    start_scheduler(stop) -> [threads] ; next_scan_time(now=None) -> float | None ; run_scan_now(profile=None)
    scan_running(profile=None) -> bool   a scan (scheduled or "Jetzt suchen") is in progress
    due_scan(profile, now=None) -> slot timestamp | None ; tick(now=None) ; live_tick(trigger="schedule")
    run_cleanup(trigger="schedule", dry_run=False) -> summary ; prune_tmp(days=TMP_KEEP_DAYS) -> files
    prune_db(now=None) -> {what: rows} ; wake() ; hold_scans(seconds=NETWORK_GRACE_S)
    recorders() -> [{pid, name, url, profile}] ; stop_inactive_recorders() -> [stopped names]
"""
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db, health, paths, pipeline, runtime, store

log = logging.getLogger("automate.scheduler")

TICK_S = 20.0
SLOT_GRACE_S = 15 * 60       # a scan started up to this long before a slot counts for it
CATCH_UP_AFTER_S = 10 * 60   # a slot run later than this is labelled "catch_up" (e.g. after sleep)
SCAN_RETRY_S = 15 * 60       # a failed scan (no network, no source reachable) runs again after this long ...
SCAN_RETRIES = 8             # ... at most this many times per slot (two hours), then the next slot
NETWORK_GRACE_S = 120        # no scan this soon after the server started or the PC woke up
WAKE_JUMP_S = 60             # the wall clock ran this much further than the monotonic one: the PC slept
WATCHDOG_S = 30.0
CLEANUP_EVERY_S = 24 * 3600
T_LIVE_TICK = 20 * 60
TMP_KEEP_DAYS = 5
DAY_S = 86400
# DB retention, applied by the daily cleanup. A job keeps its info/warning/error events as long as it exists (they
# are its story in the dashboard); its raw command output (debug) goes two weeks after it ended.
EVENT_DEBUG_KEEP_S, EVENT_KEEP_S = 14 * DAY_S, 365 * DAY_S
RUN_KEEP_S = {"health": 3 * DAY_S, "live_watch": 14 * DAY_S, "scan": 180 * DAY_S, "cleanup": 180 * DAY_S}

_wake_event = threading.Event()
_cleanup_lock = threading.Lock()
_cleanup_runner = None
_live_lock = threading.Lock()
_hold = {"until": 0.0}


def wake():
    _wake_event.set()


def hold_scans(seconds=NETWORK_GRACE_S, now=None):
    """No scheduled scan starts for `seconds` (after a start or a wake the network may not be up yet)."""
    _hold["until"] = (time.time() if now is None else now) + seconds


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


def _scan_plan(profile, now):
    """(slot, retry_at) for the latest of today's passed slots: retry_at is None when no scan of `profile` started
    since the slot (minus SLOT_GRACE_S): it is due now; the time the failed scan of that slot runs again; or
    float("inf") when the slot is done (a scan succeeded, is running, or failed SCAN_RETRIES + 1 times). (None, None)
    before the first slot of the day."""
    passed = [ts for ts in _slots(datetime.fromtimestamp(now).date()) if ts <= now]
    if not passed:
        return None, None
    slot = passed[-1]
    runs = [r for r in store.list_runs("scan", limit=200) if r["profile"] == profile
            and not (r.get("summary") or {}).get("interrupted") and r["started_at"] >= slot - SLOT_GRACE_S]
    if not runs:
        return slot, None
    last = runs[0]
    failed = [r for r in runs if r.get("finished_at") and not r.get("ok") and (r.get("summary") or {}).get("retry")]
    if last.get("finished_at") and not last.get("ok") and (last.get("summary") or {}).get("retry") \
            and len(failed) <= SCAN_RETRIES:
        return slot, last["started_at"] + SCAN_RETRY_S
    return slot, float("inf")


def due_scan(profile, now=None):
    """The latest of today's slots that has passed, if no scan of `profile` started since (minus SLOT_GRACE_S), or
    if the last one failed and its retry time has come."""
    now = time.time() if now is None else now
    slot, retry_at = _scan_plan(profile, now)
    if slot is None or (retry_at is not None and retry_at > now):
        return None
    return slot


def next_scan_time(now=None):
    """When the next scheduled scan runs (now, if one is due; a failed scan's retry), None when automation is off or
    no profile is on."""
    if not store.get_setting("automation.enabled"):
        return None
    profiles = store.enabled_profiles()
    if not profiles:
        return None
    now = time.time() if now is None else now
    plans = [_scan_plan(p, now) for p in profiles]
    if any(slot is not None and (retry is None or retry <= now) for slot, retry in plans):
        return max(now, _hold["until"])
    today = datetime.fromtimestamp(now).date()
    upcoming = [ts for ts in _slots(today) if ts > now] or _slots(today + timedelta(days=1))
    retries = [retry for _, retry in plans if retry is not None and retry != float("inf")]
    times = [t for t in [upcoming[0] if upcoming else None, *retries] if t is not None]
    return min(times) if times else None


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


def prune_db(now=None):
    """Old job output lines and old runs (health and live checks add hundreds a day) -> {what: rows deleted}."""
    out = {"job_events": store.prune_job_events(EVENT_DEBUG_KEEP_S, levels=("debug",), now=now)
           + store.prune_job_events(EVENT_KEEP_S, now=now)}
    for kind, keep in RUN_KEEP_S.items():
        out[f"runs.{kind}"] = store.prune_runs(kind, keep, now=now)
    return out


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
    pruned = 0 if dry_run else prune_tmp()
    db_rows = {} if dry_run else prune_db()  # also when `clipper cleanup` failed: the DB must not grow forever
    if not isinstance(data, dict):
        summary = {"message": f"Aufräumen fehlgeschlagen: {pipeline._tail(res)}", "tmp_files": pruned,
                   "db_rows": db_rows}
        store.finish_run(rid, False, summary)
        return summary
    files, gb = int(data.get("files") or 0), float(data.get("gb") or 0)
    verb = "würde löschen" if dry_run else "gelöscht"
    msg = (f"{files} Quelldatei(en) {verb} ({health.gb_text(gb)})" if files else "Nichts zu löschen") \
        + (f", {pruned} alte Hilfsdatei(en) entfernt" if pruned else "")
    summary = {"message": msg, **data, "tmp_files": pruned, "db_rows": db_rows}
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
    if store.get_setting("automation.enabled") and now >= _hold["until"]:
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


class WakeDetector:
    """True from woke() when the wall clock ran WAKE_JUMP_S further than the monotonic clock since the last call: the
    monotonic clock stops while the PC sleeps (Linux, macOS), the wall clock doesn't."""

    def __init__(self):
        self.wall, self.mono = time.time(), time.monotonic()

    def woke(self):
        wall, mono = time.time(), time.monotonic()
        jumped = (wall - self.wall) - (mono - self.mono) > WAKE_JUMP_S
        self.wall, self.mono = wall, mono
        return jumped


def _scheduler_loop(stop):
    try:
        try:
            _mark_interrupted_runs()
        except Exception:
            log.exception("closing interrupted runs failed")
        hold_scans()  # just started: the network may not be up yet (a PC that booted, a laptop that woke)
        sleep = WakeDetector()
        while not stop.is_set():
            if sleep.woke():
                log.info("woke up from sleep: scans wait %s s for the network", NETWORK_GRACE_S)
                hold_scans()
            try:
                tick(stop=stop)
            except Exception:
                log.exception("scheduler tick failed")
            _wake_event.wait(TICK_S)
            _wake_event.clear()
    finally:
        db.close()


# ---------------------------------------------------------------- live service

def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40]


def _proc_profile(pid):
    """CLIPPER_PROFILE of a process (Linux: /proc/<pid>/environ), else None."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    for item in env:
        if item.startswith(b"CLIPPER_PROFILE="):
            return item.split(b"=", 1)[1].decode("utf-8", "replace") or None
    return None


def _ours(pid, name):
    """A recorder of this checkout: its stream's pid file in our jobs/_live names it, or (Linux) it runs here."""
    try:
        if int((paths.jobs_dir() / "_live" / _slug(name) / "pid").read_text().strip()) == pid:
            return True
    except (OSError, ValueError):
        pass
    try:
        return Path(f"/proc/{pid}/cwd").resolve() == paths.ROOT.resolve()
    except OSError:
        return False


def recorders():
    """[{pid, name, url, profile}] of the running `python -m clipper live record <name> <url> …` processes of this
    checkout (by command line, and by pid file or working directory; profile None where it can't be read)."""
    from . import worker
    out = []
    for pid, _pgid, argv in worker._processes():
        try:
            i = argv.index("-m")
        except ValueError:
            continue
        tail = argv[i + 1:]
        if tail[:3] != ["clipper", "live", "record"] or len(tail) < 5 or pid == os.getpid():
            continue
        if _ours(pid, tail[3]):
            out.append({"pid": pid, "name": tail[3], "url": tail[4], "profile": _proc_profile(pid)})
    return out


def stop_inactive_recorders():
    """SIGTERM every recorder whose stream is no longer active: paused, deleted, its profile switched off, or live
    recording switched off. `clipper live record` then stops ffmpeg, saves its partial chunk and exits (that chunk
    is not clipped: pipeline.enqueue_live_chunks drops chunks of inactive streams). Only processes verified as a
    recorder of this checkout (recorders()) are signalled. Returns the stopped stream names."""
    active = set()
    if store.get_setting("automation.live_enabled"):
        for p in store.enabled_profiles():
            for s in store.list_streams(p, include_paused=False):
                active |= {(p, pipeline._norm_url(s["url"])), (p, "name:" + _slug(s["name"]))}
    profiles = {p for p, _ in active}
    stopped = []
    for r in recorders():
        keys = {pipeline._norm_url(r["url"]), "name:" + _slug(r["name"])}
        wanted = any((p, k) in active for p in ([r["profile"]] if r["profile"] else profiles) for k in keys)
        if wanted:
            continue
        try:
            os.kill(r["pid"], signal.SIGTERM)
        except OSError:
            continue
        log.info("stopped the recorder of %s (pid %s): its stream is no longer active", r["name"], r["pid"])
        stopped.append(r["name"])
    return stopped


def live_tick(trigger="schedule", stop=None):
    """Watch every enabled profile's active streams once (see the module doc). Returns {profile: summary}."""
    if not _live_lock.acquire(blocking=False):
        return {}
    try:
        try:
            stop_inactive_recorders()
        except Exception:
            log.exception("stopping inactive recorders failed")
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
    low = pipeline.disk_low(profile)
    try:
        data = runtime.run_bridge("live_tick", {"chunk": chunk, "health": True, "restart_stalled": True,
                                                "start": low is None}, profile, timeout=T_LIVE_TICK, cancel=stop)
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
    dropped = []
    new = pipeline.enqueue_live_chunks(profile, data.get("ready"), dropped=dropped)
    closed = pipeline.close_live_chunks(profile, data.get("ready"))
    unreachable = data.get("probe_error")
    if unreachable:
        unreachable = pipeline.yt_dlp_detail(unreachable)
    health.store_live(profile, data, error=f"Streams nicht erreichbar: {unreachable}" if unreachable else None)
    streams = data.get("streams") or []
    live = [s for s in streams if s.get("live") and s.get("on_topic")]
    parts = [f"{len(live)} von {len(streams)} Stream(s) live mit GTA 6" if streams else "Keine Streams"]
    if unreachable:
        parts = [f"Live-Check: Plattformen nicht erreichbar ({unreachable})"]
    if data.get("started"):
        parts.append(f"Aufnahme gestartet: {', '.join(data['started'])}")
    if data.get("restarted"):
        parts.append(f"hängende Aufnahme neu gestartet: {', '.join(data['restarted'])}")
    if new:
        parts.append(f"{len(new)} neue(s) Stück(e) zum Clippen")
    if dropped:
        parts.append(f"{len(dropped)} Stück(e) nicht geclippt (Stream pausiert/entfernt oder unter "
                     f"{pipeline.MIN_LIVE_CHUNK_S // 60} Min.)")
    if low:
        parts.append(f"keine neue Aufnahme: nur {health.gb_text(low[0])} frei (mindestens {health.gb_text(low[1])})")
    if data.get("transcribing"):
        parts.append(f"{len(data['transcribing'])} Stück(e) in Analyse")
    cleaned = data.get("cleaned") or {}
    if cleaned.get("files"):
        parts.append(f"{cleaned['files']} alte Datei(en) gelöscht ({health.gb_text(cleaned.get('gb') or 0)})")
    summary = {"message": ", ".join(parts), "started": data.get("started") or [], "new_jobs": new,
               "closed": closed, "dropped": dropped, "recorders": data.get("recorders"),
               "issues": data.get("issues") or {}, "restarted": data.get("restarted") or [],
               "disk_low": bool(low)}
    store.finish_run(rid, not unreachable, summary)
    return summary


def _live_loop(stop):
    last = swept = None
    try:
        while not stop.wait(5):
            try:
                if not store.get_setting("automation.live_enabled"):
                    last = None
                    if swept is None or time.monotonic() - swept >= 60:  # live off: no recorder keeps running
                        swept = time.monotonic()
                        stop_inactive_recorders()
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


SERVICES = (("scheduler", "_scheduler_loop", "Zeitplan"), ("live-service", "_live_loop", "Live-Dienst"),
            ("health-monitor", "_health_loop", "Systemprüfung"))


def _start(name, fn_name, stop):
    t = threading.Thread(target=globals()[fn_name], args=(stop,), name=name, daemon=True)
    t.start()
    return t


def _watchdog(stop, threads):
    """Restart a service thread that died (its loop catches errors, so this is for what slips through) and say so."""
    try:
        while not stop.wait(WATCHDOG_S):
            for name, fn_name, label in SERVICES:
                if threads[name].is_alive() or stop.is_set():
                    continue
                log.error("the %s thread died: restarting it", name)
                threads[name] = _start(name, fn_name, stop)
                try:
                    store.raise_alert("service.restarted", "warning",
                                      f"Ein Hintergrunddienst des Servers ({label}) war abgestürzt und wurde neu "
                                      "gestartet. Passiert das öfter, stehen die Details im Server-Log "
                                      "(logs/automate/).", {"service": name, "at": time.time()})
                except Exception:
                    log.exception("raising the service alert failed")
    finally:
        db.close()


def start_scheduler(stop):
    """Start the scheduler, live-service and health-monitor threads and their watchdog (daemons; they end soon after
    `stop`)."""
    threads = {name: _start(name, fn_name, stop) for name, fn_name, _ in SERVICES}
    dog = threading.Thread(target=_watchdog, args=(stop, threads), name="scheduler-watchdog", daemon=True)
    dog.start()

    def unblock():
        stop.wait()
        wake()
    threading.Thread(target=unblock, name="scheduler-stop", daemon=True).start()
    return [*threads.values(), dog]
