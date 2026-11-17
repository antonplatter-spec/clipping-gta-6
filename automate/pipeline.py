"""The orchestrator's job logic: what every stage of every job kind does, how jobs, picks and runs change state, and
the actions the dashboard triggers. Worker threads (worker.py) claim jobs and call `run_job`; the scheduler
(scheduler.py) starts scans and enqueues live chunks through the functions here; the API calls the actions.

Job kinds and stages (jobs.stage; after a crash or restart a job continues at its stage, and every stage first looks
at what is already on disk, so finished work is never redone):

    source      fetch -> analyze -> select -> gate -> approval | render (status waiting_approval) -> finished (done)
    live_chunk  select -> gate -> approval | render (waiting_approval) -> finished (`clipper live done`, then done)
    render      render (one pick -> one `video N` folder -> videos + variants rows)
    dub         dub (`dub segments` -> agent.translate_dub -> `dub render` -> a video with `lang`)
    upload      upload (youtube.upload_variant -> the posts row)

A parent (source / live_chunk) stays `waiting_approval` while any pick is undecided (stage approval) or an approved
pick is still rendering (stage render); `settle_parent` finishes it once every pick is decided and every approved
one is rendered or failed. A live chunk then gets `clipper live done` (which starts the 3-hour deletion timer of its
recording), so its source stays while picks are undecided.

Waiting: no Anthropic key -> `waiting_key`, daily budget spent -> `waiting_budget` (both with an alert);
`resume_waiting()` (scheduler tick, on_settings_changed) puts them back in the queue. Fetch and analyze never need
the key. Failures: StageError.retryable -> queued again with backoff (BACKOFF_S, MAX_ATTEMPTS tries), otherwise or
after the last try -> `failed` + alert `job.failed.<id>`; a scan's undownloadable video is `skipped`, not failed.

Approval mode: manual -> picks `proposed`; auto -> every gated pick is approved and gets its render job at once.
Switching manual -> auto leaves already proposed picks in the queue (auto applies to picks gated after the switch).

Every state change that can race (approve / reject / edit, cancel vs. a worker finishing, two scans) is a
compare-and-set (store.transition_job / transition_pick, or an update inside `run.owned()`, which checks the worker
still holds the job's lock in the same transaction).

Public (API):
    PipelineError(message, code, status, fields=None) ; PickCheckFailed(message, report)        status 404/409/422
    enqueue_url(url, profile, section=None, trigger="dashboard") -> job id
    request_scan(profile=None, trigger="dashboard") -> [run ids]   the scan runs in a background thread
    scan_running(profile=None) -> bool
    approve_pick(pick_id) -> render job id ; reject_pick(pick_id, reason=None) -> pick row
    update_pick_data(pick_id, changes) -> (pick row, check_report)       raises PickCheckFailed on fails
    enqueue_dub(video_id, lang) -> job id ; enqueue_upload(variant_id, account_id, privacy) -> job id
    retry_job(job_id) -> job row ; cancel_job(job_id) -> job row
    on_settings_changed(keys) ; resume_waiting(force=False) -> [job ids]
    source_expires_at(job) -> float | None   when `clipper cleanup` deletes the job's source.mp4 (None: gone / never)
    write_picks_file(job_id)                 jobs/<clipper_job>/picks.json = the job's picks in idx order
Worker / scheduler side:
    run_job(run) ; settle_parent(job_id) ; enqueue_live_chunks(profile, ready) -> [ids]
    close_live_chunks(profile, ready) -> [clipper jobs] ; run_scan(run_id, profile, trigger, cancel=None) -> summary
    youtube_id(url) ; trailing_json(text) ; profile_config(profile)
"""
import hashlib
import importlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import costs, paths, runtime, store

log = logging.getLogger("automate.pipeline")

MAX_ATTEMPTS = 3
BACKOFF_S = (60, 300, 900)            # after the 1st, 2nd, ... failure
SLOW_BACKOFF_S = (900, 1800, 3600)    # rate limits and YouTube's bot check
PRIORITY = {"source": 100, "url": 90, "live_chunk": 50, "render": 40, "dub": 60, "upload": 30}
PARENT_KINDS = ("source", "live_chunk")
ACTIVE = ("queued", "running", "waiting_approval", "waiting_budget", "waiting_key")
CANCELABLE = ACTIVE
RETRYABLE = ("failed", "canceled", "skipped")
SCHEDULED_TRIGGERS = ("schedule", "catch_up")

# timeouts (seconds) sized to the work; `STALL_*` = no output line for that long means the command hangs
T_PROBE = 180
T_FETCH, STALL_FETCH = 6 * 3600, 30 * 60
T_ANALYZE_BASE, T_ANALYZE_PER_S, T_ANALYZE_MAX = 1800, 2.0, 12 * 3600
T_SELECT = 60 * 60
T_CHECK, T_RANK = 300, 900
T_RENDER, STALL_RENDER = 3 * 3600, 90 * 60
T_DUB_SEGMENTS, T_TRANSLATE, T_DUB_RENDER = 3600, 30 * 60, 3 * 3600
T_UPLOAD = 4 * 3600
T_LIVE_DONE, T_CLEANUP, T_WATCH = 300, 1800, 3600

KIND_DE = {"source": "Quelle", "live_chunk": "Live-Stück", "render": "Rendern", "dub": "Synchronisation",
           "upload": "YouTube-Upload"}
CLIP_TYPES = ("reaction", "comedy", "news", "gameplay", "showcase", "scenario")
HOOK_STYLES = ("bold", "box", "label", "none")
CAPTION_STYLES = ("boxed", "bold", "comic")
PRIVACY = ("private", "unlisted", "public")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
SECTION_RE = re.compile(r"^(\d{1,2}(?::\d{1,2}){0,2})-(\d{1,2}(?::\d{1,2}){0,2})$")
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})(?![\w-])")

KEY_MISSING = ("Der Anthropic-API-Schlüssel fehlt. Trage ihn unter „Kosten & Schlüssel“ ein – wartende Jobs laufen "
               "dann von selbst weiter.")
FISH_MISSING = ("Für die Synchronisation wird der Fish-Audio-Schlüssel gebraucht (Stimmen klonen). Trage ihn unter "
                "„Kosten & Schlüssel“ ein und starte den Job dann erneut.")
VOICE_KEY_MISSING = ("Dieser Clip hat ein Voiceover, aber es ist kein Fish-Audio-Schlüssel gespeichert und die "
                     "Systemstimme ist ausgeschaltet. Trage den Schlüssel unter „Kosten & Schlüssel“ ein oder erlaube "
                     "die Systemstimme unter Einstellungen → Clips, dann „Erneut versuchen“.")
LIVE_SOURCE_GONE = ("Das Quellvideo dieses Live-Stücks wurde bereits gelöscht (Live-Aufnahmen bleiben nach dem Clippen "
                    "3 Stunden, sonst 2 Tage ohne Änderung). Der Clip kann nicht mehr gerendert werden – gib Clips aus "
                    "Live-Streams bitte früher frei.")
KEY_REJECTED = ("Anthropic hat den gespeicherten API-Schlüssel abgelehnt (ungültig oder widerrufen). Trage unter "
                "„Kosten & Schlüssel“ einen gültigen ein – wartende Jobs laufen dann von selbst weiter.")
BILLING = ("Das Anthropic-Konto hat kein Guthaben mehr oder sein Ausgabenlimit erreicht "
           "(console.anthropic.com → Billing).")
BILLING_RETRY_S = 3600
YTDLP_MISSING = ("yt-dlp ist nicht installiert, deshalb kann kein Video geladen werden. Unter System steht, wie du es "
                 "installierst (z. B. „uv run python -m clipper setup --install“).")


# ---------------------------------------------------------------- errors

class PipelineError(Exception):
    """An action the dashboard asked for can't be done: `code` snake_case, `status` the HTTP hint, str(e) German."""

    def __init__(self, message, code="conflict", status=409, fields=None):
        super().__init__(message)
        self.message, self.code, self.status, self.fields = message, code, status, fields


class PickCheckFailed(PipelineError):
    """An edit breaks a rule of `clipper check`; `report` is the pick's check report (nothing was saved)."""

    def __init__(self, message, report):
        super().__init__(message, "check_failed", 422)
        self.report = report


class StageError(Exception):
    """A stage failed. `retryable`: trying again later can help (network, 429); `slow`: back off longer;
    `skip`: the job ends `skipped` (a scan's undownloadable video), not failed."""

    def __init__(self, message, retryable=True, slow=False, skip=False):
        super().__init__(message)
        self.message, self.retryable, self.slow, self.skip = message, retryable, slow, skip


class JobLost(Exception):
    """The worker no longer holds the job (canceled, or its lock was released): stop without writing."""


class Canceled(Exception):
    """The running command was stopped: reason "cancel" | "lost" | "shutdown" | "timeout" | "stall"."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- small helpers

def _agent():
    return importlib.import_module("automate.agent")


def _youtube():
    return importlib.import_module("automate.youtube")


def _wake():
    try:
        from . import worker
        worker.wake()
    except Exception:  # workers not running (tests, CLI): the queue is polled anyway
        pass


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def trailing_json(text):
    """The JSON value a clipper command prints last (progress lines may come before it)."""
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(("{", "[")):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    raise ValueError("keine JSON-Ausgabe gefunden")


def _tail(res, n=1):
    """The last meaningful output lines of a ProcResult (stderr first: that's where tracebacks and errors go)."""
    lines = [x.strip() for x in ((res.stderr or "") + "\n" + (res.stdout or "")).splitlines() if x.strip()]
    errs = [x for x in lines if re.search(r"error|exception|failed|not found|no such|refused|SystemExit", x, re.I)]
    pick = (errs or lines)[-n:]
    return " · ".join(x[:300] for x in pick) or f"Exit-Code {res.returncode}"


def youtube_id(url):
    """The 11-character video id of a YouTube URL or a bare id (clipper's own rule), else None."""
    url = (url or "").strip()
    if re.fullmatch(r"[\w-]{11}", url):
        return url
    if not re.search(r"(^|[/.])(youtube\.com|youtu\.be)(/|$)", re.sub(r"^https?://", "", url).split("?")[0] + "/"):
        return None
    m = YT_ID_RE.search(url)
    return m.group(1) if m else None


def _section_seconds(ts):
    secs = 0.0
    for part in ts.split(":"):
        secs = secs * 60 + float(part)
    return secs


def daily_section(duration_s, max_minutes):
    """`clipper daily`'s rule: uploads longer than daily_max_minutes only get their first daily_max_minutes."""
    max_s = int(max_minutes) * 60
    if not duration_s or duration_s <= max_s:
        return None
    return f"0:00-{max_s // 3600}:{max_s % 3600 // 60:02d}:00"


def _hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _midnight(now=None):
    d = datetime.fromtimestamp(time.time() if now is None else now)
    return datetime(d.year, d.month, d.day).timestamp()


def _plain(text):
    return re.sub(r"\s+", " ", str(text or "").replace("*", "")).strip()


def _job_dir(cj):
    return paths.jobs_dir() / cj


_cfg_cache = {}


def profile_config(profile):
    """watchlist.yaml + profile.yaml as the clipper sees them (repo files + dashboard overrides), cached 60 s."""
    hit = _cfg_cache.get(profile)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    cfg = runtime.merge_config(runtime.watchlist_config()["effective"], runtime.profile_config(profile)["effective"])
    _cfg_cache[profile] = (time.monotonic(), cfg)
    return cfg


def _all_picks(job_id):
    return sorted(store.list_picks(job_id=job_id, limit=100000)[0], key=lambda p: p["idx"])


def _report_fails(entry):
    """`clipper.hooks.has_fails` for one pick's report entry."""
    entry = entry or {}
    if any(v.get("fails") for k in ("hooks", "titles") for v in (entry.get(k) or {}).values()):
        return True
    return any(k in entry for k in ("variety", "length", "labels", "footage", "fx"))


def _fail_summary(report):
    """The first few fails of a check report (one pick entry or the whole {video N: entry}) as one line."""
    entries = report.values() if any(k.startswith("video ") for k in report) else [report]
    out = []
    for e in entries:
        for kind in ("hooks", "titles"):
            for text, v in (e.get(kind) or {}).items():
                out += [f"„{text}“: {f}" for f in v.get("fails") or []]
        for k in ("variety", "length", "labels", "footage", "fx"):
            if k in e:
                v = e[k]
                out.append(f"{k}: {'; '.join(map(str, v)) if isinstance(v, list) else v}")
    return " · ".join(out[:4]) or "Regelverstoß"


# ---------------------------------------------------------------- picks.json and sources

_file_locks = defaultdict(threading.Lock)


def write_picks_file(job_id):
    """jobs/<clipper_job>/picks.json = the job's picks in idx order (`clipper dub … --pick N` reads it)."""
    job = store.get_job(job_id)
    if not job or not job.get("clipper_job"):
        return None
    path = _job_dir(job["clipper_job"]) / "picks.json"
    with _file_locks[str(path)]:
        _write_atomic(path, json.dumps([p["data"] for p in _all_picks(job_id)], indent=2, ensure_ascii=False) + "\n")
    return path


def _is_live_dir(cj):
    meta = _read_json(_job_dir(cj) / "job.json", {}) or {}
    return bool(meta.get("live")) or cj.startswith("live-")


def source_expires_at(job):
    """When `clipper cleanup` deletes this job's source.mp4 (its own rules: a live chunk 3 h after `live done`,
    anything 2 days after its folder last changed); None when there is no source or nothing would delete it."""
    if isinstance(job, int):
        job = store.get_job(job)
    cj = (job or {}).get("clipper_job")
    if not cj or not (_job_dir(cj) / "source.mp4").exists():
        return None
    cfg = profile_config(job.get("profile") or "gta6")
    live_h, days = cfg.get("keep_live_sources_hours"), cfg.get("keep_sources_days")
    jd, out = _job_dir(cj), []
    done = jd / "live_done"
    try:
        if done.exists() and live_h is not None:
            out.append(done.stat().st_mtime + float(live_h) * 3600)
        if days is not None:
            out.append(max(f.stat().st_mtime for f in jd.iterdir() if f.is_file()) + float(days) * 86400)
    except (OSError, ValueError):
        return None
    return min(out) if out else None


def _source_jobs(parent, data):
    """Job folders a pick's footage comes from: its job and every segment's own `job`."""
    names = [parent["clipper_job"]]
    for seg in data.get("segments") or []:
        if seg.get("job") and seg["job"] not in names:
            names.append(seg["job"])
    return names


def _ensure_sources(run, parent, data):
    """Every source.mp4 a render/dub needs: an upload is fetched again when cleanup deleted it (same section, so the
    pick's seconds still match); a live recording can't be, which fails the job with a clear message."""
    for cj in _source_jobs(parent, data):
        jd = _job_dir(cj)
        if (jd / "source.mp4").exists():
            continue
        if _is_live_dir(cj) or (cj == parent["clipper_job"] and parent["kind"] == "live_chunk"):
            raise StageError(LIVE_SOURCE_GONE if cj == parent["clipper_job"] else
                             f"Das Quellvideo des Live-Stücks {cj} (eine Szene dieses Clips) wurde bereits gelöscht. "
                             "Der Clip kann nicht mehr gerendert werden.", retryable=False)
        owner = parent if cj == parent["clipper_job"] else store.find_job_by_clipper_job(cj)
        section = (owner or {}).get("section")
        meta = _read_json(jd / "job.json", {}) or {}
        if not section and float(meta.get("offset") or 0):
            raise StageError(f"Das Quellvideo {cj} wurde gelöscht und sein Abschnitt ist unbekannt – lade es über "
                             "„URL laden“ mit demselben Abschnitt neu.", retryable=False)
        run.progress(f"Quellvideo wurde aufgeräumt – lade {cj} neu …", force=True)
        run.event(f"source.mp4 von {cj} fehlt (aufgeräumt): lade es neu", "warning")
        res = run.clipper(["fetch", cj] + (["--section", section] if section else []), timeout=T_FETCH,
                          stall=STALL_FETCH, progress=_fetch_progress)
        if res.returncode != 0 or not (jd / "source.mp4").exists():
            raise _fetch_error(res, scan=False)


# ---------------------------------------------------------------- alerts for waiting jobs

def _key_id(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else None


def _key_state():
    """"missing" | "rejected" (Anthropic refused exactly the stored key) | "ok"."""
    key = store.get_secret("anthropic_api_key")
    if not key:
        return "missing"
    return "rejected" if store.get_meta("anthropic.rejected_key") == _key_id(key) else "ok"


def _waiting_for_credit():
    return [j for j in store.list_jobs(status="waiting_budget", limit=10000)[0]
            if (j.get("params") or {}).get("wait") == "billing"]


def sync_waiting_alerts():
    """key.anthropic_missing, anthropic.billing and budget.reached from the current state (waiting jobs, automation
    switched on)."""
    auto = store.get_setting("automation.enabled") or store.get_setting("automation.live_enabled")
    counts = store.job_counts()
    state, n = _key_state(), counts["waiting_key"]
    if state != "ok" and (n or auto):
        store.raise_alert("key.anthropic_missing", "error" if n else "warning",
                          (f"{n} Job(s) warten auf Claude. " if n else "")
                          + (KEY_REJECTED if state == "rejected" else KEY_MISSING), {"waiting_jobs": n, "key": state})
    else:
        store.resolve_alert("key.anthropic_missing")
    credit = _waiting_for_credit()
    if credit:
        store.raise_alert("anthropic.billing", "error",
                          f"{store.get_meta('anthropic.billing_message') or BILLING} {len(credit)} Job(s) warten; der "
                          "Server versucht es stündlich erneut (sofort, sobald du unter „Kosten & Schlüssel“ etwas "
                          "speicherst).", {"waiting_jobs": len(credit)})
    else:
        store.resolve_alert("anthropic.billing")
    daily = counts["waiting_budget"] - len(credit)
    if not costs.budget_ok() and (daily or auto):
        limit = float(store.get_setting("claude.daily_limit_eur") or 0)
        spent = costs.spent_today_eur()
        store.raise_alert("budget.reached", "warning",
                          f"Das Claude-Tageslimit von {limit:.2f} € ist erreicht ({spent:.2f} € heute). "
                          + (f"{daily} Job(s) warten und laufen morgen automatisch weiter" if daily else
                             "Neue Auswahlen warten bis morgen")
                          + " – oder erhöhe das Limit unter „Kosten & Schlüssel“.",
                          {"waiting_jobs": daily, "limit_eur": limit, "spent_eur": spent})
    else:
        store.resolve_alert("budget.reached")


def resume_waiting(force=False):
    """waiting_key -> queued once a key is stored (not the one Anthropic rejected), waiting_budget -> queued once the
    daily budget allows (new day, higher limit); jobs waiting for Anthropic credit retry hourly, or at once with
    `force` (a settings/key change). Compare-and-set, so a job canceled meanwhile stays canceled."""
    resumed = []
    if _key_state() == "ok":
        for job in store.list_jobs(status="waiting_key", limit=10000)[0]:
            if store.transition_job(job["id"], ("waiting_key",), status="queued", not_before=0,
                                    progress="Schlüssel ist da – geht weiter …"):
                resumed.append(job["id"])
    credit_due = force or time.time() - float(store.get_meta("anthropic.billing_at") or 0) >= BILLING_RETRY_S
    budget = costs.budget_ok()
    for job in store.list_jobs(status="waiting_budget", limit=10000)[0]:
        params = dict(job.get("params") or {})
        if not (credit_due if params.pop("wait", None) == "billing" else budget):
            continue
        if store.transition_job(job["id"], ("waiting_budget",), status="queued", not_before=0, params=params,
                                progress="Geht weiter …"):
            resumed.append(job["id"])
    for jid in resumed:
        store.add_job_event(jid, "Wartezeit vorbei, wieder eingereiht")
    sync_waiting_alerts()
    if resumed:
        _wake()
    return resumed


def _park_key(run, message=None, rejected=False):
    if rejected:
        store.set_meta("anthropic.rejected_key", _key_id(store.get_secret("anthropic_api_key")))
    run.update(status="waiting_key", progress="Wartet: Anthropic hat den Schlüssel abgelehnt" if rejected else
               "Wartet auf den Anthropic-Schlüssel (Kosten & Schlüssel)")
    run.event(message or "Kein Anthropic-Schlüssel gespeichert: der Job wartet", "warning")
    sync_waiting_alerts()


def _park_budget(run, billing=None):
    """Daily limit reached, or (`billing` = the agent's German message) the Anthropic account has no credit."""
    params = dict((run.job or {}).get("params") or {})
    params.pop("wait", None)
    if billing:
        params["wait"] = "billing"
        store.set_meta("anthropic.billing_at", time.time())
        store.set_meta("anthropic.billing_message", billing)
    run.update(status="waiting_budget", params=params,
               progress="Wartet: das Anthropic-Konto hat kein Guthaben" if billing else
               "Wartet: Claude-Tageslimit erreicht (geht morgen weiter)")
    run.event(billing or "Tageslimit für Claude erreicht: der Job wartet", "warning")
    sync_waiting_alerts()


def _claude_ready(run):
    """False (and the job parked) when Claude can't be asked now."""
    state = _key_state()
    if state != "ok":
        _park_key(run, rejected=state == "rejected")
        return False
    if not costs.budget_ok():
        _park_budget(run)
        return False
    return True


def _ask_claude(run, fn, *args, timeout, **kwargs):
    """agent.select_picks / translate_dub in the run's helper thread; None when the job was parked instead (no or a
    rejected key, daily limit, no Anthropic credit). Other AgentErrors fail the stage, retried only when the agent
    says trying again can help."""
    if not _claude_ready(run):
        return None
    agent = _agent()
    try:
        return run.call(fn, *args, log=run.log, cancel=run.cancel, timeout=timeout, **kwargs)
    except agent.MissingApiKey as e:  # InvalidApiKey (401) is both: the stored key was rejected
        _park_key(run, str(e), rejected=isinstance(e, agent.AgentError))
    except agent.BudgetExceeded:
        _park_budget(run)
    except agent.AgentError as e:
        code = getattr(e, "code", None)
        if code == "billing":
            _park_budget(run, str(e))
            return None
        raise StageError(f"Claude: {e}", retryable=bool(getattr(e, "retryable", False)),
                         slow=code == "rate_limited") from None
    return None


# ---------------------------------------------------------------- error classification

_PERMANENT_FETCH = (
    ("private video", "Das Video ist privat."),
    ("members-only", "Das Video ist nur für Kanalmitglieder."),
    ("join this channel", "Das Video ist nur für Kanalmitglieder."),
    ("confirm your age", "Das Video ist altersbeschränkt (YouTube verlangt eine Anmeldung)."),
    ("age-restricted", "Das Video ist altersbeschränkt."),
    ("inappropriate for some users", "Das Video ist altersbeschränkt."),
    ("has been terminated", "Der Kanal des Videos wurde gesperrt."),
    ("copyright", "Das Video wurde wegen eines Urheberrechtsanspruchs gesperrt."),
    ("not available in your country", "Das Video ist in diesem Land nicht verfügbar."),
    ("has been removed", "Das Video wurde entfernt."),
    ("video unavailable", "Das Video ist nicht (mehr) verfügbar."),
    ("premieres in", "Die Premiere hat noch nicht begonnen."),
    ("live event will begin", "Der Livestream hat noch nicht begonnen."),
    ("is not a valid url", "Das ist kein gültiger Videolink."),
    ("unsupported url", "Diese Adresse wird nicht unterstützt."),
)
_SLOW_FETCH = ("http error 429", "too many requests", "not a bot", "rate-limit", "rate limit")


def _fetch_error(res_or_text, scan):
    """A failed download (ProcResult or an error text) as a StageError: private/removed/age-restricted videos fail
    at once (a scan skips them, like `clipper daily`), rate limits back off longer, the rest is retried."""
    if isinstance(res_or_text, str):
        text = res_or_text
        detail = ([x.strip() for x in text.splitlines() if x.strip()] or [""])[-1][:300]
    else:
        text, detail = f"{res_or_text.stderr}\n{res_or_text.stdout}", _tail(res_or_text)
    low = text.lower()
    if re.search(r"no such file or directory: '?yt-dlp|yt-dlp: (command )?not found", low):
        return StageError(YTDLP_MISSING, retryable=False)
    if any(k in low for k in ("not a bot", "sign in to confirm you")) and "your age" not in low:
        return StageError("YouTube verlangt gerade eine Bot-Prüfung. Der Server versucht es später erneut "
                          f"(yt-dlp: {detail}).", retryable=True, slow=True)
    for marker, msg in _PERMANENT_FETCH:
        if marker in low:
            return StageError(f"{'Übersprungen: ' if scan else ''}{msg} (yt-dlp: {detail})", retryable=False,
                              skip=scan)
    slow = any(k in low for k in _SLOW_FETCH)
    return StageError(f"Download fehlgeschlagen: {detail}", retryable=True, slow=slow)


def _analyze_error(res):
    low = f"{res.stderr}\n{res.stdout}".lower()
    detail = _tail(res)
    if "no module named 'onnx_asr'" in low or "no module named 'onnxruntime'" in low:
        return StageError("Die Transkription (Parakeet/onnx-asr) ist nicht installiert: „uv sync“ im Projektordner "
                          "ausführen.", retryable=False)
    if "no ggml whisper model" in low or "whisper model not found" in low:
        return StageError("Kein whisper.cpp-Modell gefunden: „uv run python -m clipper setup --install“ lädt es "
                          "herunter (oder stelle asr: parakeet ein).", retryable=False)
    if re.search(r"no such file or directory: '?(ffmpeg|ffprobe)", low):
        return StageError("ffmpeg fehlt: unter System steht der Installationsbefehl.", retryable=False)
    if any(k in low for k in ("huggingface", "hf_hub", "connectionerror", "max retries exceeded", "name resolution")):
        return StageError(f"Das Sprachmodell konnte nicht geladen werden (Internetverbindung zu huggingface.co?): "
                          f"{detail}", retryable=True)
    return StageError(f"Analyse fehlgeschlagen: {detail}", retryable=True)


# ---------------------------------------------------------------- progress parsers (output line -> German line)

_DL_RE = re.compile(r"\[download\]\s+([\d.]+)%(?:.*?ETA\s+([\d:]+))?")
_FF_TIME_RE = re.compile(r"time=(\d+):(\d\d):(\d\d)")


def _fetch_progress(line):
    m = _DL_RE.search(line)
    if m:
        eta = f" (noch {m.group(2)})" if m.group(2) and m.group(2) != "00:00" else ""
        return f"Lade Video … {float(m.group(1)):.0f} %{eta}"
    m = _FF_TIME_RE.search(line)
    if m:
        return f"Lade Abschnitt … {int(m.group(1))}:{m.group(2)}:{m.group(3)} geladen"
    if "[Merger]" in line or "Merging formats" in line:
        return "Lade Video … füge Bild und Ton zusammen"
    return None


def _analyze_progress(line):
    if "[analyze] hot mode" in line:
        return "Transkribiere (nur die lautesten Stellen) …"
    if "[analyze] transcribing" in line:
        return "Transkribiere …"
    if "[analyze] measuring loudness" in line:
        return "Bewerte Lautstärke, Sprünge und Hype-Wörter …"
    return None


def _render_progress(n_hooks):
    """`clipper render` prints a line per phase and one per hook MP4 it starts."""
    state = {"i": 0, "total": n_hooks}

    def parse(line):
        m = re.search(r"\[render\]\s+video (\d+)(/)?", line)
        if not m:
            return None
        n = m.group(1)
        if m.group(2):
            state["i"] += 1
            return f"Rendere Video {n}: Hook-Variante {state['i']} von {max(state['total'], state['i'])} …"
        if "joining" in line:
            return f"Rendere Video {n}: füge die Szenen zusammen …"
        if "separating voices" in line:
            return f"Rendere Video {n}: trenne Stimmen und erkenne Sprecher …"
        if "no-hook arm" in line:
            state["total"] = 1
            return f"Rendere Video {n}: Variante ohne Hook (A/B-Test) …"
        return None
    return parse


def _dub_progress(lang):
    def parse(line):
        m = re.search(r"\[dub\] line (\d+)/(\d+)", line)
        if m:
            return f"Synchronisation ({lang}): Stimme Zeile {m.group(1)} von {m.group(2)} …"
        if "[dub]   video" in line:
            return f"Synchronisation ({lang}): rendere Hook-Varianten …"
        if "isolating" in line or "separating" in line:
            return f"Synchronisation ({lang}): trenne die Stimmen …"
        return None
    return parse


# ---------------------------------------------------------------- the job runner

def run_job(run):
    """Run the claimed job's stages until it parks, ends or is stopped. `run` is a worker.Run."""
    while True:
        job = run.job
        if not job or job["status"] != "running" or job.get("locked_by") != run.worker_id:
            return
        fn = _STAGES.get((job["kind"], job["stage"]))
        try:
            run.check()
            if fn is None:
                raise StageError(f"Unbekannte Stufe „{job['stage']}“ für {KIND_DE.get(job['kind'], job['kind'])}",
                                 retryable=False)
            out = fn(run, job)
            if out is None:
                return
            status = out.pop("status", "running")
            if status in ("running", "done"):
                out.setdefault("error", None)
            run.update(status=status, **out)
            if status != "running":
                return
        except JobLost:
            _discard(run, job)
            return
        except Canceled as c:
            _on_canceled(run, job, c.reason)
            return
        except StageError as e:
            _on_error(run, store.get_job(job["id"]) or job, e)
            return
        except Exception as e:  # a bug must not kill the worker; the job retries and then fails visibly
            log.exception("job %s (%s/%s) crashed", job["id"], job["kind"], job["stage"])
            _on_error(run, store.get_job(job["id"]) or job, StageError(f"Interner Fehler: {e!r}"[:500]))
            return


def _on_canceled(run, job, reason):
    if reason == "shutdown":
        try:
            run.update(status="queued", progress="Unterbrochen (Server wurde beendet) – geht beim nächsten Start "
                                                 "weiter")
        except JobLost:
            pass
        return
    if reason in ("timeout", "stall"):
        _on_error(run, store.get_job(job["id"]) or job, StageError(
            "Zeitüberschreitung: der Schritt hing zu lange und wurde abgebrochen" if reason == "timeout" else
            "Der Befehl gab zu lange keine Ausgabe mehr aus und wurde abgebrochen", retryable=True))
        return
    _discard(run, job)


def _discard(run, job):
    """The job was canceled while this worker ran it: remove outputs of the interrupted attempt."""
    now = store.get_job(job["id"])
    if not now or now["status"] == "running":  # someone else owns it now: leave their files alone
        return
    if job["kind"] in ("render", "dub"):
        for folder, _ in _leftovers(now):
            shutil.rmtree(folder, ignore_errors=True)
            store.add_job_event(job["id"], f"Unvollständige Ausgabe entfernt: {paths.rel(folder)}", "warning")


def _on_error(run, job, e):
    attempts = int(job.get("attempts") or 0) + 1
    try:
        if e.skip:
            run.update(status="skipped", attempts=attempts, error=e.message, progress="Übersprungen")
            run.event(e.message, "warning")
            if job["kind"] == "live_chunk":
                _live_done_quiet(run, job)
            return
        if e.retryable and attempts < MAX_ATTEMPTS:
            delay = (SLOW_BACKOFF_S if e.slow else BACKOFF_S)[min(attempts, len(BACKOFF_S)) - 1]
            when = time.time() + delay
            run.update(status="queued", attempts=attempts, not_before=when, error=e.message,
                       progress=f"Fehler – neuer Versuch um {_hhmm(when)} (Versuch {attempts + 1} von "
                                f"{MAX_ATTEMPTS})")
            run.event(f"{e.message} – neuer Versuch in {delay // 60} Min.", "warning")
            return
        with run.owned():
            store.update_job(job["id"], status="failed", attempts=attempts, error=e.message,
                             progress="Fehlgeschlagen")
            _after_failure(job, e.message)
        run.event(e.message, "error")
    except JobLost:
        return
    raise_failed_alert(store.get_job(job["id"]) or job, e.message)
    if job["kind"] == "render":
        pick = store.get_pick((job.get("params") or {}).get("pick_id") or 0)
        if pick:
            settle_parent(pick["job_id"])


def _after_failure(job, message):
    """Kind-specific bookkeeping inside the failing transaction."""
    params = job.get("params") or {}
    if job["kind"] == "render" and params.get("pick_id"):
        store.transition_pick(params["pick_id"], ("approved", "rendering"), "failed", error=message)
    elif job["kind"] == "upload" and params.get("post_id"):
        store.update_post(params["post_id"], status="failed", error=message)


def raise_failed_alert(job, message):
    what = job.get("title") or job.get("clipper_job") or job.get("source_url") or ""
    store.raise_alert(f"job.failed.{job['id']}", "error",
                      f"Job #{job['id']} ({KIND_DE.get(job['kind'], job['kind'])}"
                      + (f" „{what[:80]}“" if what else "") + f") ist fehlgeschlagen: {message} "
                      f"Unter Jobs → #{job['id']} kannst du ihn erneut versuchen.",
                      {"job_id": job["id"], "kind": job["kind"], "stage": job.get("stage")})


# ---------------------------------------------------------------- stage: fetch

def _stage_fetch(run, job):
    params = dict(job.get("params") or {})
    vid, section = job["clipper_job"], job.get("section")
    scan = bool(job.get("run_id"))
    jd = _job_dir(vid)
    if (jd / "source.mp4").exists() and (jd / "job.json").exists():
        meta = _read_json(jd / "job.json", {}) or {}
        run.event("Quellvideo liegt schon vor: kein neuer Download")
        return {"stage": "analyze", "title": job.get("title") or meta.get("title"),
                "channel": job.get("channel") or meta.get("channel"), "progress": "Video liegt vor"}
    if not params.get("duration") and not params.get("probed"):
        run.progress("Prüfe das Video …", force=True)
        try:
            info = run.bridge("probe_video", {"url": job["source_url"] or vid}, timeout=T_PROBE)
        except StageError as e:
            raise _fetch_error(e.message, scan) from None
        if not info.get("ok"):
            raise _fetch_error(info.get("error") or "yt-dlp: unbekannter Fehler", scan)
        if info.get("live_status") == "is_live" or info.get("is_live"):
            raise StageError("Das ist ein laufender Livestream. Trage den Kanal unter Quellen → Live-Streams ein; als "
                             "einzelnes Video geht er erst, wenn der Stream vorbei ist.", retryable=False, skip=scan)
        if info.get("live_status") == "is_upcoming":
            raise StageError("Die Premiere bzw. der Livestream hat noch nicht begonnen.", retryable=False, skip=scan)
        params.update(probed=True, duration=info.get("duration"))
        if not section and params.get("auto_section", True):
            section = daily_section(info.get("duration"), store.get_setting("prefs.daily_max_minutes"))
        job = run.update(params=params, section=section, title=job.get("title") or info.get("title"),
                         channel=job.get("channel") or info.get("channel"))
        if section:
            run.event(f"Langes Video ({(info.get('duration') or 0) / 60:.0f} Min.): lade nur {section}")
    run.progress("Lade Video …", force=True)
    res = run.clipper(["fetch", vid] + (["--section", section] if section else []), timeout=T_FETCH,
                      stall=STALL_FETCH, progress=_fetch_progress)
    if res.returncode != 0:
        raise _fetch_error(res, scan)
    try:
        meta = trailing_json(res.stdout)
    except ValueError:
        meta = _read_json(jd / "job.json", {}) or {}
    if not (jd / "source.mp4").exists():
        raise StageError(f"Download meldete Erfolg, aber {vid}/source.mp4 fehlt", retryable=True)
    return {"stage": "analyze", "title": job.get("title") or meta.get("title"),
            "channel": job.get("channel") or meta.get("channel"), "progress": "Video geladen"}


# ---------------------------------------------------------------- stage: analyze

def _repair_partial_analysis(jd):
    """An analysis killed mid-way can leave a truncated audio.wav or transcript.json that `clipper analyze` would
    reuse: without a readable transcript both go (extracting the audio again takes a minute)."""
    t = jd / "transcript.json"
    if t.exists() and _read_json(t) is None:
        t.unlink(missing_ok=True)
    if not t.exists():
        (jd / "audio.wav").unlink(missing_ok=True)


def _stage_analyze(run, job):
    cj = job["clipper_job"]
    jd = _job_dir(cj)
    if (jd / "candidates.json").exists() and _read_json(jd / "candidates.json") is not None:
        return {"stage": "select", "result": {**(job.get("result") or {}), "analysis": _analysis_summary(jd)},
                "progress": "Analyse liegt vor"}
    if not (jd / "source.mp4").exists():
        params = dict(job.get("params") or {})
        params["refetches"] = int(params.get("refetches") or 0) + 1
        if params["refetches"] > 2:
            raise StageError("Das Quellvideo verschwindet immer wieder vor der Analyse", retryable=False)
        return {"stage": "fetch", "params": params, "progress": "Quellvideo fehlt – lade neu …"}
    _repair_partial_analysis(jd)
    dur = float((job.get("params") or {}).get("duration") or (_read_json(jd / "job.json", {}) or {}).get("duration")
                or 3600)
    if job.get("section"):
        a, _, b = job["section"].partition("-")
        dur = max(60.0, _section_seconds(b) - _section_seconds(a))
    timeout = min(T_ANALYZE_MAX, T_ANALYZE_BASE + T_ANALYZE_PER_S * dur)
    run.progress("Analysiere: extrahiere den Ton …", force=True)
    res = run.clipper(["analyze", cj], timeout=timeout, progress=_analyze_progress)
    if res.returncode != 0:
        raise _analyze_error(res)
    if _read_json(jd / "candidates.json") is None:
        raise StageError("Analyse lief durch, aber candidates.json fehlt", retryable=True)
    summary = _analysis_summary(jd)
    return {"stage": "select", "result": {**(job.get("result") or {}), "analysis": summary},
            "progress": f"Analyse fertig: {summary['candidates']} Kandidaten, empfohlen {summary['recommended']}"}


def _analysis_summary(jd):
    data = _read_json(jd / "candidates.json", {}) or {}
    return {"recommended": int(data.get("recommended") or 0), "clip_type": data.get("clip_type"),
            "candidates": len(data.get("candidates") or []), "scenarios": len(data.get("scenarios") or []),
            "profile": data.get("profile")}


# ---------------------------------------------------------------- stage: select

def max_picks_for(job, recommended):
    """How many picks Claude may make for this source, as `clipper daily` / clip-daily decide it: `auto` = the
    analysis' recommended count, capped by max_videos_per_run minus what the same scan run already produced; a fixed
    videos_per_run is shared out over the run's sources that haven't been clipped yet."""
    want = store.get_setting("prefs.videos_per_run")
    cap = int(store.get_setting("prefs.max_videos_per_run"))
    produced, open_sources = 0, 1
    if job.get("run_id"):
        others = [j for j in store.list_jobs(kind="source", run_id=job["run_id"], limit=1000)[0]
                  if j["id"] != job["id"]]
        for other in others:
            produced += sum(1 for p in _all_picks(other["id"]) if p["status"] != "rejected")
            if other["status"] in ("queued", "running", "waiting_key", "waiting_budget") \
                    and other["stage"] in ("fetch", "analyze", "select", "gate"):
                open_sources += 1
    if want == "auto":
        return max(0, min(int(recommended), cap - produced))
    total = int(want) - (produced if job.get("run_id") else 0)
    return max(0, math.ceil(total / open_sources)) if total > 0 else 0


def avoid_openers(job, limit=40):
    """The opening words (first three) of hooks and titles already made today in this profile, so no two videos open
    the same way (clip.md: "Vary the wording across videos")."""
    since, seen, out = _midnight(), set(), []
    for p in store.list_picks(profile=job["profile"], limit=500)[0]:
        if p["job_id"] == job["id"] or p["status"] == "rejected" or (p.get("created_at") or 0) < since:
            continue
        data = p.get("data") or {}
        for text in list(data.get("hooks") or []) + list(data.get("titles") or []):
            opener = " ".join(_plain(text).split()[:3])
            if opener and opener.lower() not in seen:
                seen.add(opener.lower())
                out.append(opener)
    return out[:limit]


def _selection(sel):
    get = (lambda k: sel.get(k)) if isinstance(sel, dict) else (lambda k: getattr(sel, k, None))
    picks = [p for p in (get("picks") or []) if isinstance(p, dict)]

    def per(k):
        v = list(get(k) or [])
        return (v + [None] * len(picks))[:len(picks)]
    return {"picks": picks, "reasons": per("reasons"), "click_triggers": per("click_triggers"),
            "no_clip_reason": get("no_clip_reason"), "check_report": get("check_report") or {},
            "turns": get("turns") or 0, "usage": get("usage") or {}}


def _stage_select(run, job):
    cj = job["clipper_job"]
    jd = _job_dir(cj)
    analysis = _read_json(jd / "candidates.json")
    if analysis is None:
        if job["kind"] == "source":
            return {"stage": "analyze", "progress": "Analyse fehlt – analysiere …"}
        raise StageError(f"Das Live-Stück {cj} ist nicht analysiert (candidates.json fehlt)", retryable=False)
    if job["kind"] == "live_chunk" and not (jd / "source.mp4").exists():
        raise StageError(LIVE_SOURCE_GONE, retryable=False)
    recommended = int(analysis.get("recommended") or 0)
    n = max_picks_for(job, recommended)
    result = dict(job.get("result") or {})
    if n <= 0:
        reason = ("Die Analyse hält keinen Moment für lohnend (empfohlen: 0)." if recommended <= 0 else
                  "Für diesen Durchlauf sind schon genug Videos ausgewählt (Videos pro Durchlauf).")
        if job["kind"] == "live_chunk":
            _live_done_quiet(run, job)
        return {"status": "skipped", "error": reason, "progress": "Keine Clips", "result": result}
    agent = _agent()
    openers = avoid_openers(job)
    run.progress("Claude wählt Momente …", force=True)
    run.event(f"Claude wählt bis zu {n} Momente (empfohlen {recommended})"
              + (f", vermeidet Anfänge: {', '.join(openers[:8])}" if openers else ""))
    sel = _ask_claude(run, agent.select_picks, job["id"], max_picks=n, avoid_openers=openers, timeout=T_SELECT)
    if sel is None:
        return None
    sel = _selection(sel)
    if len(sel["picks"]) > n:
        run.event(f"Claude lieferte {len(sel['picks'])} Picks, erlaubt sind {n}: die ersten {n} bleiben", "warning")
        for k in ("picks", "reasons", "click_triggers"):
            sel[k] = sel[k][:n]
    result["selection"] = sel
    if not sel["picks"]:
        reason = sel.get("no_clip_reason") or "Claude fand keinen Moment, der sich lohnt."
        if job["kind"] == "live_chunk":
            _live_done_quiet(run, job)
        return {"status": "skipped", "error": f"Keine Clips: {reason}", "progress": "Keine Clips", "result": result}
    return {"stage": "gate", "result": result, "progress": f"Claude hat {len(sel['picks'])} Momente gewählt"}


# ---------------------------------------------------------------- stage: gate (deterministic ④)

def _hooks_keep():
    v = store.get_setting("prefs.hooks_per_video")
    return v if v == "auto" else int(v)


def _stage_gate(run, job):
    cj = job["clipper_job"]
    sel = (job.get("result") or {}).get("selection") or {}
    picks = sel.get("picks") or []
    if not picks:
        return {"stage": "select", "progress": "Auswahl fehlt – frage Claude erneut …"}
    run.progress("Prüfe Hooks, Titel und Footage …", force=True)
    chk = run.bridge("check", {"picks": picks, "job": cj}, timeout=T_CHECK)
    if chk.get("fails"):
        return _gate_failed(run, job, chk.get("report") or {})
    run.progress("Bewerte die Hooks (Swipe-Test mit 3.481 Shorts) …", force=True)
    ranked = run.bridge("rank", {"picks": picks, "keep": _hooks_keep()}, timeout=T_RANK)
    picks2, ranking = ranked.get("picks") or picks, ranked.get("ranking") or []
    chk2 = run.bridge("check", {"picks": picks2, "job": cj}, timeout=T_CHECK)
    if chk2.get("fails"):
        return _gate_failed(run, job, chk2.get("report") or {})
    report = chk2.get("report") or {}

    def nth(seq, i):
        return seq[i] if isinstance(seq, list) and i < len(seq) else None
    items = [{"data": p, "reason": nth(sel.get("reasons"), i), "click_trigger": nth(sel.get("click_triggers"), i),
              "check_report": report.get(f"video {i + 1}") or {}, "simulate_report": nth(ranking, i)}
             for i, p in enumerate(picks2)]
    auto = store.get_setting("approval.mode") == "auto"
    path = _job_dir(cj) / "picks.json"
    with _file_locks[str(path)]:
        _write_atomic(path, json.dumps([it["data"] for it in items], indent=2, ensure_ascii=False) + "\n")
    with run.owned():
        ids = store.replace_picks(job["id"], items, status="proposed")
        renders = [_approve_in_tx(pid, store.get_job(job["id"])) for pid in ids] if auto else []
        store.update_job(job["id"], status="waiting_approval", stage="render" if auto else "approval", error=None,
                         progress=(f"Rendere: 0 von {len(ids)} Clips fertig" if auto else
                                   f"Wartet auf Freigabe: {len(ids)} von {len(ids)} Clips offen"))
    run.event(f"{len(ids)} Pick(s) bestehen die Prüfung"
              + (f"; Freigabe automatisch, Render-Jobs {', '.join(f'#{r}' for r in renders)}" if auto else
                 "; warten auf Freigabe"))
    if auto:
        _wake()
    return None


def _gate_failed(run, job, report):
    params = dict(job.get("params") or {})
    rounds = int(params.get("gate_rounds") or 0)
    summary = _fail_summary(report)
    if rounds < 1:
        params.update(gate_rounds=rounds + 1, check_feedback=report)
        run.event(f"Qualitätsprüfung nicht bestanden ({summary}): zurück an Claude", "warning")
        return {"stage": "select", "params": params,
                "progress": "Qualitätsprüfung: Fehler gefunden – Claude bessert nach …"}
    raise StageError(f"Die Picks bestehen die Qualitätsprüfung auch nach einer Korrektur nicht: {summary}",
                     retryable=False)


# ---------------------------------------------------------------- approval, settle

def _approve_in_tx(pick_id, parent):
    """proposed -> approved + its render job (inside the caller's transaction); returns the render job id."""
    pick = store.transition_pick(pick_id, ("proposed",), "approved", error=None)
    if pick is None:
        cur = store.get_pick(pick_id)
        raise PipelineError(f"Der Clip ist nicht mehr offen (Status: {cur['status'] if cur else 'gelöscht'}).",
                            "not_proposed", 409)
    titles = (pick.get("data") or {}).get("titles") or []
    return store.create_job("render", pick["profile"], parent_id=parent["id"], clipper_job=parent["clipper_job"],
                            source_url=parent.get("source_url"), channel=parent.get("channel"),
                            title=(titles[0] if titles else parent.get("title")), priority=PRIORITY["render"],
                            params={"pick_id": pick_id}, progress="Wartet aufs Rendern")


def settle_parent(job_id):
    """Recompute a parent's approval/render state; finish it once every pick is decided and every approved one is
    rendered or failed (a live chunk goes back to the queue for `clipper live done`)."""
    job = store.get_job(job_id)
    if not job or job["kind"] not in PARENT_KINDS or job["status"] != "waiting_approval":
        return job
    c = Counter(p["status"] for p in _all_picks(job_id))
    total = sum(c.values())
    pending = c["approved"] + c["rendering"]
    if c["proposed"]:
        return store.transition_job(job_id, ("waiting_approval",), stage="approval",
                                    progress=f"Wartet auf Freigabe: {c['proposed']} von {total} Clips offen")
    if pending:
        return store.transition_job(job_id, ("waiting_approval",), stage="render",
                                    progress=f"Rendere: {c['rendered']} von {c['rendered'] + pending} Clips fertig")
    parts = [f"{c['rendered']} gerendert" if c["rendered"] else "", f"{c['rejected']} abgelehnt" if c["rejected"]
             else "", f"{c['failed']} fehlgeschlagen" if c["failed"] else ""]
    summary = "Fertig: " + (", ".join(x for x in parts if x) or "keine Clips")
    if job["kind"] == "live_chunk":
        row = store.transition_job(job_id, ("waiting_approval",), status="queued", stage="finished", not_before=0,
                                   progress="Gibt die Live-Aufnahme frei …", result={**(job.get("result") or {}),
                                                                                    "summary": summary})
        _wake()
        return row
    return store.transition_job(job_id, ("waiting_approval",), status="done", stage="finished", progress=summary)


def _live_done_quiet(run, job):
    """`clipper live done` for a chunk that ends without clips; a failure is left to the live service's sweep."""
    try:
        res = run.clipper(["live", "done", job["clipper_job"]], timeout=T_LIVE_DONE)
        if res.returncode == 0:
            store.add_job_event(job["id"], "Live-Aufnahme freigegeben (live done)")
    except (StageError, Canceled):
        pass


def _stage_finished(run, job):
    result = dict(job.get("result") or {})
    if job["kind"] == "live_chunk" and not result.get("live_done"):
        run.progress("Gibt die Live-Aufnahme frei …", force=True)
        if (_job_dir(job["clipper_job"])).is_dir():
            res = run.clipper(["live", "done", job["clipper_job"]], timeout=T_LIVE_DONE)
            if res.returncode != 0:
                raise StageError(f"„clipper live done“ fehlgeschlagen: {_tail(res)}")
        result["live_done"] = True
    return {"status": "done", "result": result, "progress": result.get("summary") or "Fertig"}


# ---------------------------------------------------------------- render / dub outputs

def _export_dir(profile, day=None):
    return paths.exports_dir() / profile / (day or date.today()).isoformat()


def _snapshot(dirs):
    return {"dirs": [str(d) for d in dirs],
            "before": {str(d): sorted(p.name for p in Path(d).glob("video *")) if Path(d).is_dir() else []
                       for d in dirs}}


def _leftovers(job):
    """[(folder, manifest | None)] of `video N` folders the job's last attempt created that no video row holds yet."""
    snap = (job.get("params") or {}).get("out")
    if not snap:
        return []
    pick_id = (job.get("params") or {}).get("pick_id")
    known = ({v["folder"] for v in store.list_videos(pick_id=pick_id, archived=None, limit=1000)[0]}
             if pick_id else set())
    out = []
    for d in snap.get("dirs") or []:
        before = set((snap.get("before") or {}).get(d) or [])
        for f in sorted(Path(d).glob("video *")) if Path(d).is_dir() else []:
            if f.is_dir() and f.name not in before and paths.rel(f) not in known:
                out.append((f, _read_json(f / "upload.json")))
    return out


def _manifest_matches(entries, data, lang):
    if not isinstance(entries, list) or not entries:
        return False
    if lang != "en":
        return all(e.get("lang") == lang for e in entries if isinstance(e, dict))
    hooks = {_plain(h) for h in data.get("hooks") or []}
    titles = {_plain(t)[:100] for t in data.get("titles") or []}
    return any(_plain(e.get("hook")) in hooks or _plain(e.get("title"))[:100] in titles
               for e in entries if isinstance(e, dict))


def _adopt_leftovers(run, job, pick, lang):
    """After a crash: a finished folder (upload.json written) of the last attempt is registered instead of rendering
    again; a half-written one is deleted so the numbering stays tidy. Returns the adopted folder or None."""
    adopted = None
    for folder, entries in _leftovers(job):
        if entries is None:
            shutil.rmtree(folder, ignore_errors=True)
            run.event(f"Unvollständiges {folder.name} vom abgebrochenen Versuch entfernt", "warning")
        elif adopted is None and _manifest_matches(entries, pick.get("data") or {}, lang):
            adopted = folder
            run.event(f"{folder.name} war schon fertig gerendert: übernommen statt neu zu rendern")
        else:
            run.event(f"{paths.rel(folder)} gehört nicht zu diesem Clip – bleibt liegen", "warning")
    return adopted


def _register_video(job, pick, folder, lang):
    entries = _read_json(Path(folder) / "upload.json")
    if not isinstance(entries, list) or not entries:
        raise StageError(f"{paths.rel(folder)}/upload.json fehlt oder ist leer", retryable=True)
    first = entries[0]
    sources = first.get("sources") or ([first["source"]] if first.get("source") else [])
    vid = store.add_video(pick["profile"], folder, pick_id=pick["id"], job_id=job["id"], lang=lang,
                          title=first.get("title"), description=first.get("description"), sources=sources)
    for i, e in enumerate(entries):
        store.add_variant(vid, Path(folder) / Path(str(e.get("file") or "")).name, rank=e.get("rank", i),
                          hook=e.get("hook"), hook_type=e.get("hook_type"), arm=e.get("arm"), title=e.get("title"),
                          description=e.get("description"), hashtags=e.get("hashtags"),
                          predicted=e.get("predicted"), duration=e.get("duration"))
    return vid, len(entries)


def _finish_with_video(run, job, pick, folder, lang):
    folder = Path(folder)
    if not folder.is_absolute():
        folder = paths.from_rel(folder)
    with run.owned():
        vid, n = _register_video(job, pick, folder, lang)
        dubs = []
        if job["kind"] == "render":
            store.transition_pick(pick["id"], ("approved", "rendering"), "rendered", error=None)
            dubs = _auto_dubs(vid, pick)
        store.update_job(job["id"], status="done", stage="finished", error=None,
                         progress=f"Fertig: {folder.name} mit {n} Variante(n)",
                         result={**(job.get("result") or {}), "video_id": vid, "folder": paths.rel(folder)})
    run.event(f"{paths.rel(folder)} gespeichert ({n} Variante(n))"
              + (f"; Synchronisation eingereiht: {', '.join(dubs)}" if dubs else ""))
    for key in ("picks_file", "translation_file"):
        f = (job.get("params") or {}).get(key)
        if f:
            Path(f).unlink(missing_ok=True)
    if job["kind"] == "render":
        settle_parent(pick["job_id"])
    if dubs:
        _wake()
    return None


def _auto_dubs(video_id, pick):
    langs = [x for x in store.get_setting("dub.auto_languages") or [] if x != "en"]
    if not langs or not store.get_secret("fish_api_key") or (pick.get("data") or {}).get("segments"):
        return []
    made = []
    for lang in langs:
        _create_dub(video_id, lang, pick)
        made.append(lang)
    return made


# ---------------------------------------------------------------- stage: render

def _stage_render(run, job):
    params = dict(job.get("params") or {})
    pick = store.get_pick(params.get("pick_id") or 0)
    if not pick:
        raise StageError("Der Clip zu diesem Render-Job existiert nicht mehr", retryable=False)
    if pick["status"] == "rendered":
        return {"status": "done", "progress": "Schon gerendert"}
    if pick["status"] not in ("approved", "rendering"):
        raise StageError(f"Der Clip ist nicht freigegeben (Status: {pick['status']})", retryable=False)
    parent = store.get_job(pick["job_id"])
    store.transition_pick(pick["id"], ("approved",), "rendering")
    folder = _adopt_leftovers(run, job, pick, "en")
    if folder:
        return _finish_with_video(run, job, pick, folder, "en")
    data = pick["data"] or {}
    _ensure_sources(run, parent, data)
    if not (_job_dir(parent["clipper_job"]) / "transcript.json").exists():
        raise StageError(f"transcript.json von {parent['clipper_job']} fehlt – analysiere die Quelle neu",
                         retryable=False)
    allow_say = []
    if data.get("voiceover") and not store.get_secret("fish_api_key"):
        if not store.get_setting("render.allow_system_voice"):
            raise StageError(VOICE_KEY_MISSING, retryable=False)
        allow_say = ["--allow-say"]
        run.event("Kein Fish-Audio-Schlüssel: das Voiceover spricht die Systemstimme", "warning")
    paths.ensure_data_dir()
    picks_file = paths.tmp_dir() / f"render-{job['id']}.json"
    picks_file.write_text(json.dumps([data], indent=2, ensure_ascii=False), encoding="utf-8")
    out_dir = _export_dir(pick["profile"])
    params.update(out=_snapshot([out_dir]), picks_file=str(picks_file))
    run.update(params=params)
    job = run.job
    hooks = data.get("hooks") or [data.get("hook", "")]
    top = int(store.get_setting("render.top_hooks") or 0)
    run.progress("Rendere: schneide den Clip …", force=True)
    res = run.clipper(["render", parent["clipper_job"], "--picks", str(picks_file), "--out", str(out_dir)] + allow_say,
                      timeout=T_RENDER, stall=STALL_RENDER,
                      progress=_render_progress(min(len(hooks), top) if top else len(hooks)))
    if res.returncode == 3:
        raise StageError(VOICE_KEY_MISSING, retryable=False)
    if res.returncode != 0:
        low = f"{res.stderr}\n{res.stdout}".lower()
        permanent = "over the length cap" in low or "no job '" in low
        raise StageError(f"Rendern fehlgeschlagen: {_tail(res)}", retryable=not permanent)
    try:
        results = trailing_json(res.stdout)
        folder = Path(results[0]["folder"])
    except (ValueError, LookupError, TypeError):
        done = [f for f, entries in _leftovers(job) if entries]
        if not done:
            raise StageError("Rendern lief durch, aber es kam kein Ergebnis zurück", retryable=True) from None
        folder = done[0]
    return _finish_with_video(run, job, pick, folder, "en")


# ---------------------------------------------------------------- stage: dub

def _stage_dub(run, job):
    params, result = dict(job.get("params") or {}), dict(job.get("result") or {})
    lang = params.get("lang")
    video = store.get_video(params.get("video_id") or 0)
    pick = store.get_pick((video or {}).get("pick_id") or params.get("pick_id") or 0)
    if not video or not pick:
        raise StageError("Das Video oder sein Clip existiert nicht mehr", retryable=False)
    data = pick.get("data") or {}
    if data.get("segments"):
        raise StageError("Synchronisieren geht nur für Einzelclips, nicht für Story-Cuts oder Multi-Streamer-Edits",
                         retryable=False)
    if not store.get_secret("fish_api_key"):
        raise StageError(FISH_MISSING, retryable=False)
    parent = store.get_job(pick["job_id"])
    cj, idx = parent["clipper_job"], str(pick["idx"])
    folder = _adopt_leftovers(run, job, pick, lang)
    if folder:
        return _finish_with_video(run, job, pick, folder, lang)
    write_picks_file(parent["id"])
    _ensure_sources(run, parent, data)
    if "segments" not in result:
        run.progress(f"Synchronisation ({lang}): erkenne Zeilen und Sprecher …", force=True)
        res = run.clipper(["dub", "segments", cj, "--pick", idx], timeout=T_DUB_SEGMENTS)
        if res.returncode != 0:
            raise StageError(f"Zeilen für die Synchronisation nicht ermittelt: {_tail(res)}", retryable=True)
        try:
            result["segments"] = trailing_json(res.stdout)
        except ValueError:
            raise StageError("„dub segments“ lieferte kein JSON", retryable=True) from None
        run.update(result=result)
    if "translation" not in result:
        run.progress(f"Claude übersetzt ins {lang} …", force=True)
        tr = _ask_claude(run, _agent().translate_dub, job["id"], int(idx), lang, result["segments"],
                         timeout=T_TRANSLATE)
        if tr is None:
            return None
        result["translation"] = tr
        run.update(result=result)
    paths.ensure_data_dir()
    tr_file = paths.tmp_dir() / f"dub-{job['id']}.json"
    tr_file.write_text(json.dumps(result["translation"], ensure_ascii=False, indent=1), encoding="utf-8")
    today = date.today()
    dirs = [paths.exports_dir() / f"{pick['profile']}-{lang}" / d.isoformat() for d in (today, today + timedelta(1))]
    params.update(out=_snapshot(dirs), translation_file=str(tr_file), pick_id=pick["id"])
    run.update(params=params)
    job = run.job
    run.progress(f"Synchronisation ({lang}): klone die Stimmen und rendere …", force=True)
    res = run.clipper(["dub", "render", cj, "--pick", idx, "--lang", lang, "--translation", str(tr_file)],
                      timeout=T_DUB_RENDER, stall=STALL_RENDER, progress=_dub_progress(lang))
    if res.returncode != 0:
        text = f"{res.stderr}\n{res.stdout}"
        if "translation rejected" in text:
            result.pop("translation", None)
            run.update(result=result)
            raise StageError(f"Claudes Übersetzung wurde abgelehnt: {_tail(res, 3)}", retryable=True)
        if "FISH_API_KEY" in text:
            raise StageError(FISH_MISSING, retryable=False)
        raise StageError(f"Synchronisation fehlgeschlagen: {_tail(res)}", retryable=True)
    try:
        folder = Path(trailing_json(res.stdout)["folder"])
    except (ValueError, KeyError, TypeError):
        done = [f for f, entries in _leftovers(job) if entries]
        if not done:
            raise StageError("Synchronisation lief durch, aber es kam kein Ergebnis zurück", retryable=True) from None
        folder = done[0]
    return _finish_with_video(run, job, pick, folder, lang)


# ---------------------------------------------------------------- stage: upload

def _stage_upload(run, job):
    params = job.get("params") or {}
    post = store.get_post(params.get("post_id") or 0)
    if post and post["status"] == "posted":
        return {"status": "done", "progress": "Schon hochgeladen"}
    yt = _youtube()
    run.progress("Lade auf YouTube hoch …", force=True)

    def keep_session(uri):  # before the first byte: a retry after a crash asks this session instead of re-uploading
        run.update(params={**(run.job.get("params") or {}), "upload_session": uri})
    if params.get("upload_session"):
        run.event("Frage YouTube zuerst, wie weit der letzte Versuch kam (kein doppelter Upload)")
    try:
        res = run.call(yt.upload_variant, params["variant_id"], params["account_id"], params["privacy"],
                       on_progress=run.on_progress, cancel=run.cancel, timeout=T_UPLOAD,
                       session=params.get("upload_session"), on_session=keep_session)
    except yt.YouTubeError as e:
        if getattr(e, "code", "") == "canceled" and run.cancel.is_set():
            raise Canceled(run.reason or "cancel") from None
        raise StageError(str(e), retryable=bool(getattr(e, "retryable", False))) from None
    with run.owned():
        if params.get("post_id"):
            store.update_post(params["post_id"], status="posted", external_id=res.get("video_id"), url=res.get("url"),
                              error=None)
        store.update_job(job["id"], status="done", stage="finished", error=None,
                         progress=f"Hochgeladen: {res.get('url') or res.get('video_id')}",
                         result={**(job.get("result") or {}), **res})
    run.event(f"Auf YouTube: {res.get('url') or res.get('video_id')}")
    return None


_STAGES = {
    ("source", "fetch"): _stage_fetch, ("source", "analyze"): _stage_analyze, ("source", "select"): _stage_select,
    ("source", "gate"): _stage_gate, ("source", "finished"): _stage_finished,
    ("live_chunk", "select"): _stage_select, ("live_chunk", "gate"): _stage_gate,
    ("live_chunk", "finished"): _stage_finished,
    ("render", "render"): _stage_render, ("dub", "dub"): _stage_dub, ("upload", "upload"): _stage_upload,
}


# ---------------------------------------------------------------- actions: enqueue

def _check_profile(profile):
    if not profile or not store.get_profile(profile):
        raise PipelineError(f"Unbekanntes Profil „{profile}“", "unknown_profile", 422,
                            {"profile": "Unbekanntes Profil"})


def _normalize_section(section):
    if section in (None, ""):
        return None
    m = SECTION_RE.fullmatch(str(section).strip().replace(" ", ""))
    if not m or _section_seconds(m.group(1)) >= _section_seconds(m.group(2)):
        raise PipelineError("Abschnitt bitte als Start-Ende angeben, z. B. 1:00:00-1:30:00", "invalid_section", 422,
                            {"section": "Format H:MM:SS-H:MM:SS, Start vor Ende"})
    return m.group(0)


def _create_source(profile, vid, *, url, title=None, channel=None, section=None, run_id=None, trigger, priority,
                   params=None, allow_retry_of_final=True):
    """One source job per video: inside one transaction, so two scans (or a scan and a click) can't both queue it.
    Returns (job id, None) or (None, the existing job)."""
    with store.transaction():
        old = store.find_job_by_clipper_job(vid)
        if old and (not allow_retry_of_final or old["status"] not in RETRYABLE):
            return None, old
        jid = store.create_job("source", profile, clipper_job=vid, source_url=url, title=title, channel=channel,
                               section=section, run_id=run_id, priority=priority,
                               params={"trigger": trigger, **(params or {})}, progress="Wartet")
    return jid, None


def enqueue_url(url, profile, section=None, trigger="dashboard"):
    """"URL aus Dashboard: direkt laden": a source job for one YouTube video (dedupe on the video id)."""
    _check_profile(profile)
    vid = youtube_id(url)
    if not vid:
        raise PipelineError("Bitte einen YouTube-Videolink angeben (youtube.com/watch?v=… oder youtu.be/…). "
                            "Twitch- und Kick-Streams trägst du unter Quellen → Live-Streams ein.", "invalid_url", 422,
                            {"url": "Kein YouTube-Videolink"})
    section = _normalize_section(section)
    jid, old = _create_source(profile, vid, url=f"https://www.youtube.com/watch?v={vid}", section=section,
                              trigger=trigger, priority=PRIORITY["url"], params={"auto_section": section is None})
    if old:
        raise PipelineError(f"Dieses Video ist schon in Job #{old['id']} ({old['status']}).", "already_queued", 409)
    store.add_job_event(jid, f"Eingereiht ({trigger}): {url}")
    _wake()
    return jid


def _create_dub(video_id, lang, pick):
    parent = store.get_job(pick["job_id"])
    video = store.get_video(video_id)
    return store.create_job("dub", pick["profile"], parent_id=pick["job_id"], clipper_job=parent["clipper_job"],
                            source_url=parent.get("source_url"), channel=parent.get("channel"),
                            title=f"{(video or {}).get('title') or parent.get('title') or ''} ({lang})".strip(),
                            priority=PRIORITY["dub"],
                            params={"video_id": video_id, "lang": lang, "pick_id": pick["id"]},
                            progress="Wartet")


def enqueue_dub(video_id, lang):
    lang = str(lang or "").strip()
    if not LANG_RE.match(lang) or lang == "en":
        raise PipelineError("Bitte einen Sprachcode wie es, ru oder pt-BR angeben.", "invalid_lang", 422,
                            {"lang": "Sprachcode wie es, ru, pt-BR"})
    video = store.get_video(video_id)
    if not video:
        raise PipelineError("Dieses Video gibt es nicht.", "not_found", 404)
    if video.get("lang") not in (None, "en"):
        raise PipelineError("Eine Synchronisation lässt sich nicht noch einmal synchronisieren.", "is_dub", 409)
    pick = store.get_pick(video.get("pick_id") or 0)
    if not pick:
        raise PipelineError("Zu diesem Video gehört kein Clip mehr.", "no_pick", 409)
    if (pick.get("data") or {}).get("segments"):
        raise PipelineError("Synchronisieren geht nur für Einzelclips, nicht für Story-Cuts oder Multi-Streamer-Edits.",
                            "not_supported", 422)
    if not store.get_secret("fish_api_key"):
        raise PipelineError(FISH_MISSING, "fish_key_missing", 409)
    with store.transaction():
        if store.list_videos(pick_id=pick["id"], lang=lang, archived=None, limit=1)[1]:
            raise PipelineError(f"Dieses Video gibt es schon auf {lang}.", "already_dubbed", 409)
        for j in store.list_jobs(kind="dub", parent_id=pick["job_id"], status=list(ACTIVE), limit=1000)[0]:
            if (j.get("params") or {}).get("video_id") == video_id and (j.get("params") or {}).get("lang") == lang:
                raise PipelineError(f"Die Synchronisation auf {lang} läuft schon (Job #{j['id']}).", "already_queued",
                                    409)
        jid = _create_dub(video_id, lang, pick)
    _wake()
    return jid


def enqueue_upload(variant_id, account_id, privacy):
    if privacy not in PRIVACY:
        raise PipelineError("Sichtbarkeit: privat, nicht gelistet oder öffentlich.", "invalid_privacy", 422,
                            {"privacy": "private, unlisted oder public"})
    variant = store.get_variant(variant_id)
    if not variant:
        raise PipelineError("Diese Variante gibt es nicht.", "not_found", 404)
    account = store.get_account(account_id)
    if not account:
        raise PipelineError("Dieses Konto gibt es nicht.", "not_found", 404)
    if account["platform"] != "youtube" or not account.get("connected"):
        raise PipelineError("Das Konto ist nicht mit YouTube verbunden (Konten → YouTube verbinden).",
                            "not_connected", 409)
    video = store.get_video(variant["video_id"])
    pick = store.get_pick((video or {}).get("pick_id") or 0)
    with store.transaction():
        if any(p["status"] == "uploading" and p.get("account_id") == account_id for p in store.list_posts(variant_id)):
            raise PipelineError("Diese Variante wird gerade auf dieses Konto hochgeladen.", "already_uploading", 409)
        post_id = store.add_post(variant_id, "youtube", account_id=account_id, status="uploading", privacy=privacy)
        jid = store.create_job("upload", (video or {}).get("profile") or store.get_setting("ui.default_profile"),
                               parent_id=(pick or {}).get("job_id"), title=variant.get("title"),
                               priority=PRIORITY["upload"], progress="Wartet",
                               params={"variant_id": variant_id, "account_id": account_id, "privacy": privacy,
                                       "post_id": post_id})
    _wake()
    return jid


# ---------------------------------------------------------------- actions: picks

def _pick_or_404(pick_id):
    pick = store.get_pick(pick_id)
    if not pick:
        raise PipelineError("Diesen Clip gibt es nicht.", "not_found", 404)
    return pick


def approve_pick(pick_id):
    """proposed -> approved + a render job (atomic: approving twice, or approve racing reject, makes one winner)."""
    pick = _pick_or_404(pick_id)
    if pick["status"] != "proposed":
        raise PipelineError(f"Der Clip ist nicht mehr offen (Status: {pick['status']}).", "not_proposed", 409)
    if _report_fails(pick.get("check_report")):
        raise PipelineError(f"Der Clip verletzt Regeln der Qualitätsprüfung: {_fail_summary(pick['check_report'])}. "
                            "Bitte erst bearbeiten.", "check_failed", 422)
    parent = store.get_job(pick["job_id"])
    if parent["kind"] == "live_chunk" and not (_job_dir(parent["clipper_job"]) / "source.mp4").exists():
        raise PipelineError(LIVE_SOURCE_GONE, "source_gone", 409)
    with store.transaction():
        jid = _approve_in_tx(pick_id, parent)
    store.add_job_event(parent["id"], f"Clip {pick['idx']} freigegeben → Render-Job #{jid}")
    settle_parent(parent["id"])
    _wake()
    return jid


def reject_pick(pick_id, reason=None):
    pick = _pick_or_404(pick_id)
    reason = (str(reason).strip()[:500] if reason else None) or None
    row = store.transition_pick(pick_id, ("proposed",), "rejected", error=reason)
    if row is None:
        cur = store.get_pick(pick_id)
        raise PipelineError(f"Der Clip ist nicht mehr offen (Status: {cur['status']}).", "not_proposed", 409)
    store.add_job_event(pick["job_id"], f"Clip {pick['idx']} abgelehnt" + (f": {reason}" if reason else ""))
    settle_parent(pick["job_id"])
    return store.get_pick(pick_id)


def _edit_errors(data, changes):
    """{field: German message} for a PATCH body of /api/picks/{id}."""
    allowed = {"hooks", "titles", "description", "hashtags", "voiceover", "start", "end", "clip_type", "hook_style",
               "caption_style"}
    errs = {k: "Dieses Feld lässt sich nicht bearbeiten" for k in changes if k not in allowed}

    def strings(key, lo, hi):
        v = changes[key]
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v) or not lo <= len(v) <= hi:
            errs[key] = f"Liste mit {lo} bis {hi} nicht leeren Texten"
    if "hooks" in changes:
        strings("hooks", 1, 8)
        if "hooks" not in errs and len({_plain(h).lower() for h in changes["hooks"]}) != len(changes["hooks"]):
            errs["hooks"] = "Jeder Hook muss anders lauten (er wird zum Dateinamen)"
    if "titles" in changes:
        strings("titles", 1, 8)
    if "hashtags" in changes:
        v = changes["hashtags"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or len(v) > 30:
            errs["hashtags"] = "Liste von Hashtags"
    for key in ("description",):
        if key in changes and not isinstance(changes[key], str):
            errs[key] = "Text"
    if "voiceover" in changes and changes["voiceover"] is not None and not isinstance(changes["voiceover"], str):
        errs["voiceover"] = "Text (leer = kein Voiceover)"
    for key, choices in (("clip_type", CLIP_TYPES), ("hook_style", HOOK_STYLES), ("caption_style", CAPTION_STYLES)):
        if key in changes and changes[key] not in choices:
            errs[key] = f"Eins von: {', '.join(choices)}"
    for key in ("start", "end"):
        if key in changes:
            if data.get("segments"):
                errs[key] = "Story-Cuts haben keine einzelne Start-/Endzeit"
            elif not isinstance(changes[key], (int, float)) or isinstance(changes[key], bool) or changes[key] < 0:
                errs[key] = "Sekunden (Zahl ≥ 0)"
    if ("start" in changes or "end" in changes) and not data.get("segments") and not {"start", "end"} & set(errs):
        s, e = float(changes.get("start", data.get("start", 0))), float(changes.get("end", data.get("end", 0)))
        if e <= s:
            errs["end"] = "Ende muss nach dem Start liegen"
        elif e - s > 60:
            errs["end"] = "Höchstens 60 Sekunden"
    return errs


def update_pick_data(pick_id, changes):
    """Edit a proposed pick: re-run `clipper check` on it (fails -> PickCheckFailed, nothing saved), re-rank its hooks
    when they changed (simulate_report only; the owner's hooks and their order stay), save, rewrite picks.json."""
    pick = _pick_or_404(pick_id)
    if not isinstance(changes, dict) or not changes:
        raise PipelineError("Keine Änderungen angegeben.", "invalid", 422)
    if pick["status"] != "proposed":
        raise PipelineError(f"Nur offene Clips lassen sich bearbeiten (Status: {pick['status']}).", "not_proposed",
                            409)
    base = pick["data"] or {}
    errs = _edit_errors(base, changes)
    if errs:
        raise PipelineError("Bitte prüfe die Eingaben.", "invalid", 422, errs)
    new = dict(base)
    for k, v in changes.items():
        if k == "voiceover" and not (v or "").strip():
            new.pop("voiceover", None)
        else:
            new[k] = [x.strip() for x in v] if k in ("hooks", "titles") else v
    job = store.get_job(pick["job_id"])
    profile = pick["profile"]
    try:
        chk = runtime.run_bridge("check", {"picks": [new], "job": job["clipper_job"]}, profile, timeout=T_CHECK)
    except runtime.BridgeError as e:
        raise PipelineError(f"Die Qualitätsprüfung lief nicht: {e.message}", "check_error", 409) from None
    report = (chk.get("report") or {}).get("video 1") or {}
    if chk.get("fails") or _report_fails(report):
        raise PickCheckFailed(f"Die Änderung verletzt Regeln der Qualitätsprüfung: {_fail_summary(report)}", report)
    simulate = pick.get("simulate_report")
    if new.get("hooks") != base.get("hooks"):
        try:
            ranked = runtime.run_bridge("rank", {"picks": [new], "keep": _hooks_keep()}, profile, timeout=T_RANK)
            simulate = (ranked.get("ranking") or [simulate])[0]
        except runtime.BridgeError as e:
            log.warning("rank after edit of pick %s failed: %s", pick_id, e.message)
    with store.transaction():
        cur = store.get_pick(pick_id)
        if cur["status"] != "proposed":
            raise PipelineError(f"Der Clip wurde inzwischen entschieden (Status: {cur['status']}).", "not_proposed",
                                409)
        if cur["data"] != base:
            raise PipelineError("Der Clip wurde inzwischen anders bearbeitet. Bitte neu laden.", "conflict", 409)
        row = store.update_pick(pick_id, data=new, check_report=report, simulate_report=simulate)
    write_picks_file(pick["job_id"])
    store.add_job_event(pick["job_id"], f"Clip {pick['idx']} bearbeitet: {', '.join(sorted(changes))}")
    return row, report


# ---------------------------------------------------------------- actions: retry / cancel

def _job_or_404(job_id):
    job = store.get_job(job_id)
    if not job:
        raise PipelineError("Diesen Job gibt es nicht.", "not_found", 404)
    return job


def retry_job(job_id):
    """failed / canceled / skipped -> queued, at the stage where it stopped (a parent past the gate starts again at
    select; a render job approves its pick again)."""
    job = _job_or_404(job_id)
    if job["status"] not in RETRYABLE:
        raise PipelineError("Nur fehlgeschlagene, abgebrochene oder übersprungene Jobs lassen sich wiederholen.",
                            "not_retryable", 409)
    fields = {"status": "queued", "attempts": 0, "error": None, "not_before": 0, "progress": "Erneut eingereiht"}
    params = dict(job.get("params") or {})
    with store.transaction():
        if job["kind"] in PARENT_KINDS:
            locked = [p for p in _all_picks(job_id) if p["status"] in store.PICK_LOCKED]
            if locked:
                raise PipelineError("Dieser Job hat schon freigegebene oder gerenderte Clips – wiederhole stattdessen "
                                    "den einzelnen Render-Job.", "has_rendered_picks", 409)
            if job["stage"] in ("approval", "render"):  # canceled while waiting: ask Claude again
                fields["stage"] = "select"
            params.pop("gate_rounds", None)
            params.pop("check_feedback", None)
            fields["params"] = params
        elif job["kind"] == "render":
            pick = store.get_pick(params.get("pick_id") or 0)
            if not pick or pick["status"] in ("rendered", "rejected"):
                raise PipelineError("Der Clip ist schon gerendert oder abgelehnt.", "not_retryable", 409)
            store.transition_pick(pick["id"], ("failed", "proposed", "rendering"), "approved", error=None)
            parent = store.get_job(pick["job_id"])
            if parent and parent["status"] == "done":  # it had settled with this pick failed: open it again
                store.transition_job(parent["id"], ("done",), status="waiting_approval", stage="render")
        elif job["kind"] == "upload" and params.get("post_id"):
            store.update_post(params["post_id"], status="uploading", error=None)
        row = store.transition_job(job_id, RETRYABLE, **fields)
    if row is None:
        raise PipelineError("Der Job hat sich inzwischen geändert. Bitte neu laden.", "conflict", 409)
    store.resolve_alert(f"job.failed.{job_id}")
    store.add_job_event(job_id, "Erneut eingereiht")
    if job["kind"] == "render":
        settle_parent((store.get_pick(params.get("pick_id") or 0) or {}).get("job_id") or 0)
    _wake()
    return store.get_job(job_id)


def cancel_job(job_id, _pick_status="proposed"):
    """queued / running / waiting_* -> canceled (compare-and-set: a worker that finishes first wins, and a worker that
    loses stops its command and discards its half-made output). A render's pick goes back to the approval queue; a
    parent's undecided picks are rejected and its pending renders canceled."""
    from . import worker
    job = _job_or_404(job_id)
    with store.transaction():
        row = store.transition_job(job_id, CANCELABLE, status="canceled", progress="Abgebrochen")
        if row is None:
            raise PipelineError(f"Der Job ist schon beendet (Status: {store.get_job(job_id)['status']}).",
                                "not_cancelable", 409)
        params = job.get("params") or {}
        if job["kind"] == "render" and params.get("pick_id"):
            store.transition_pick(params["pick_id"], ("approved", "rendering"), _pick_status,
                                  error=None if _pick_status == "proposed" else "Job abgebrochen",
                                  **({"decided_at": None} if _pick_status == "proposed" else {}))
        elif job["kind"] == "upload" and params.get("post_id"):
            store.update_post(params["post_id"], status="failed", error="Upload abgebrochen")
    worker.request_cancel(job_id)
    store.add_job_event(job_id, "Abgebrochen", "warning")
    store.resolve_alert(f"job.failed.{job_id}")
    if job["kind"] in PARENT_KINDS:
        for p in _all_picks(job_id):
            if p["status"] == "proposed":
                store.transition_pick(p["id"], ("proposed",), "rejected", error="Job abgebrochen")
        for child in store.list_jobs(kind="render", parent_id=job_id, status=list(CANCELABLE), limit=1000)[0]:
            try:
                cancel_job(child["id"], _pick_status="rejected")
            except PipelineError:
                pass
    elif job["kind"] == "render" and params.get("pick_id"):
        pick = store.get_pick(params["pick_id"])
        if pick:
            settle_parent(pick["job_id"])
    return store.get_job(job_id)


def on_settings_changed(keys=()):
    """The API calls this after any settings / secret / source change: re-materialize the clipper overlay, resume
    jobs that waited for a key or budget, and wake the workers and the scheduler (they re-read settings anyway)."""
    _cfg_cache.clear()
    try:
        runtime.materialize()
    except Exception:
        log.exception("materialize after settings change failed")
    keys = [str(k) for k in keys or ()]
    if not keys or any("anthropic" in k for k in keys):
        store.set_meta("anthropic.rejected_key", None)  # saved again: give it another try
    resume_waiting(force=not keys or any(k.startswith(("secrets.", "claude.")) or "anthropic" in k for k in keys))
    _wake()
    try:
        from . import scheduler
        scheduler.wake()
    except Exception:
        pass


# ---------------------------------------------------------------- scans (①)

_scan_locks = defaultdict(threading.Lock)
_scan_threads = {}


def scan_running(profile=None):
    if profile:
        return _scan_locks[profile].locked()
    return any(lock.locked() for lock in list(_scan_locks.values()))


def request_scan(profile=None, trigger="dashboard", *, stop=None, wait=False):
    """Start ① for one profile (or every enabled one) in background threads; returns the run ids. A profile whose
    scan is still running is left out; when that leaves nothing, PipelineError(scan_running)."""
    if profile:
        _check_profile(profile)
        profiles = [profile]
    else:
        profiles = store.enabled_profiles()
        if not profiles:
            raise PipelineError("Kein Profil ist eingeschaltet (Quellen → Profile).", "no_profile", 409)
    started = []
    for p in profiles:
        lock = _scan_locks[p]
        if not lock.acquire(blocking=False):
            continue
        try:
            rid = store.start_run("scan", p, trigger)
        except BaseException:
            lock.release()
            raise
        t = threading.Thread(target=_scan_thread, args=(rid, p, trigger, lock, stop), name=f"scan-{p}", daemon=True)
        _scan_threads[p] = t
        t.start()
        started.append((rid, t))
    if not started:
        raise PipelineError("Die Suche läuft schon.", "scan_running", 409)
    if wait:
        for _, t in started:
            t.join()
    return [rid for rid, _ in started]


def _scan_thread(rid, profile, trigger, lock, stop):
    from . import db
    try:
        run_scan(rid, profile, trigger, cancel=stop)
    except Exception as e:
        log.exception("scan of %s crashed", profile)
        store.finish_run(rid, False, {"message": f"Suche abgebrochen: {e!r}"[:300]})
    finally:
        lock.release()
        db.close()


def _scheduled_today(profile, now=None):
    """Source jobs today's scheduled scans of `profile` queued (sources_per_day is a daily budget for them)."""
    since, n = _midnight(now), 0
    for r in store.list_runs("scan", limit=200):
        if r["profile"] == profile and r["trigger"] in SCHEDULED_TRIGGERS and r["started_at"] >= since:
            n += store.list_jobs(kind="source", run_id=r["id"], limit=1)[1]
    return n


def run_scan(rid, profile, trigger, cancel=None):
    """`clipper daily` step by step: cleanup, `clipper watch`, then the newest unprocessed on-topic uploads (creators
    before searches, sources_per_day of them, long ones cut to daily_max_minutes) become source jobs. Records the
    run with a German summary and returns it."""
    summary = {"new_jobs": 0, "checked": 0, "skipped": 0, "job_ids": []}
    clean = runtime.run_clipper(["cleanup"], profile, timeout=T_CLEANUP, cancel=cancel)
    try:
        summary["cleaned"] = trailing_json(clean.stdout) if clean.returncode == 0 else None
    except ValueError:
        summary["cleaned"] = None
    res = runtime.run_clipper(["watch", "--per-channel", "6"], profile, timeout=T_WATCH, cancel=cancel)
    if res.canceled:
        summary.update(message="Suche unterbrochen (Server wurde beendet)", interrupted=True)
        store.finish_run(rid, False, summary)
        return summary
    try:
        data = trailing_json(res.stdout) if res.returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        err = _fetch_error(res, scan=False).message if res.returncode else "„clipper watch“ lieferte kein Ergebnis"
        summary["message"] = f"Suche fehlgeschlagen: {err}"
        store.finish_run(rid, False, summary)
        store.raise_alert(f"scan.failed.{profile}", "warning", f"Die Suche nach neuen Videos ({profile}) ist "
                          f"fehlgeschlagen: {err}", {"run_id": rid})
        return summary
    store.resolve_alert(f"scan.failed.{profile}")
    videos = data.get("videos") or []
    errors = data.get("errors") or []
    summary.update(checked=len(videos), off_topic=int(data.get("skipped_off_topic") or 0), errors=errors)
    fresh = [v for v in videos if not v.get("processed")]
    fresh.sort(key=lambda v: str(v.get("source") or "").startswith("search:"))  # creators first, then searches
    limit = int(store.get_setting("prefs.sources_per_day"))
    if trigger in SCHEDULED_TRIGGERS:
        limit = max(0, limit - _scheduled_today(profile))
    max_minutes = store.get_setting("prefs.daily_max_minutes")
    known = 0
    for v in fresh:
        if summary["new_jobs"] >= limit:
            break
        vid = v.get("id")
        if not vid:
            continue
        jid, old = _create_source(profile, vid, url=v.get("url") or f"https://www.youtube.com/watch?v={vid}",
                                  title=v.get("title"), channel=v.get("channel"), run_id=rid, trigger=trigger,
                                  section=daily_section(v.get("duration"), max_minutes), priority=PRIORITY["source"],
                                  params={"duration": v.get("duration"), "found_via": v.get("source")},
                                  allow_retry_of_final=False)
        if old:
            known += 1
            continue
        store.add_job_event(jid, f"Gefunden über {v.get('source')}: {v.get('title')}")
        summary["new_jobs"] += 1
        summary["job_ids"].append(jid)
    summary["skipped"] = len(videos) - summary["new_jobs"]
    parts = [f"{summary['new_jobs']} neue Videos eingereiht" if summary["new_jobs"] else "Keine neuen Videos",
             f"{len(videos)} geprüft"]
    if summary["off_topic"]:
        parts.append(f"{summary['off_topic']} nicht zu GTA 6")
    if known:
        parts.append(f"{known} schon bekannt")
    if errors:
        parts.append(f"{len(errors)} Quelle(n) nicht erreichbar")
    if limit == 0 and trigger in SCHEDULED_TRIGGERS and len(fresh) > known:
        parts.append("Tageslimit „Neue Quellvideos pro Tag“ erreicht")
    summary["message"] = parts[0] + " (" + ", ".join(parts[1:]) + ")"
    store.finish_run(rid, True, summary)
    if summary["new_jobs"]:
        _wake()
    return summary


# ---------------------------------------------------------------- live chunks

def enqueue_live_chunks(profile, ready):
    """A live_chunk job for every analyzed chunk (`live watch`'s `ready`) that has none yet."""
    made = []
    for cj in ready or []:
        jd = _job_dir(cj)
        meta = _read_json(jd / "job.json", {}) or {}
        if not (jd / "candidates.json").exists() or (meta.get("profile") or profile) != profile:
            continue
        with store.transaction():
            if store.find_job_by_clipper_job(cj, kinds=("live_chunk", "source")):
                continue
            jid = store.create_job("live_chunk", profile, clipper_job=cj, source_url=meta.get("url"),
                                   title=meta.get("title"), channel=meta.get("channel"),
                                   priority=PRIORITY["live_chunk"],
                                   params={"recorded_at": meta.get("recorded_at"), "duration": meta.get("duration")},
                                   progress="Wartet")
        store.add_job_event(jid, f"Live-Stück aufgenommen und analysiert: {cj}")
        made.append(jid)
    if made:
        _wake()
    return made


def close_live_chunks(profile, ready):
    """`clipper live done` for chunks still listed as ready whose job already ended without it (failed, canceled,
    skipped when the inline call failed), so their recordings don't wait for the 2-day rule."""
    stale = []
    for cj in ready or []:
        job = store.find_job_by_clipper_job(cj, kinds=("live_chunk",))
        if job and job["status"] in store.FINAL_STATUSES and not (job.get("result") or {}).get("live_done"):
            stale.append((job, cj))
    if not stale:
        return []
    res = runtime.run_clipper(["live", "done", *[cj for _, cj in stale]], profile, timeout=T_LIVE_DONE)
    if res.returncode != 0:
        log.warning("live done for %s failed: %s", [cj for _, cj in stale], _tail(res))
        return []
    for job, _ in stale:
        cur = store.get_job(job["id"])
        if cur and cur["status"] in store.FINAL_STATUSES:
            store.update_job(job["id"], result={**(cur.get("result") or {}), "live_done": True})
    return [cj for _, cj in stale]
