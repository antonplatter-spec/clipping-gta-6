"""All data access: typed helper functions over the SQLite DB. The only module with SQL.

Rows come back as plain dicts with JSON columns decoded and flag columns as bool. Each thread uses its own
connection (db.conn()); every write helper runs in its own short IMMEDIATE transaction, and `transaction()` groups
several helpers into one atomic step (helpers called inside it join it), e.g.

    with store.transaction():
        if store.transition_pick(pid, ("proposed",), "approved"):
            store.create_job("render", profile, params={"pick_id": pid})

Errors: ValueError (German message, safe to show) for bad input such as an unknown profile, a duplicate creator or
any UNIQUE / foreign-key conflict;
settings.SettingsError (a ValueError with per-key `errors`) for invalid settings, secrets or overrides; KeyError for
an unknown setting key. Timestamps are unix seconds (float); `day` strings are the local day (costs.local_day).

Settings and secrets
    get_setting(key) -> value (the default when unset)      all_settings() -> {key: value} for every registry key
    set_settings({key: value}, only_unset=False) -> all_settings()   validates everything first (SettingsError)
    reset_settings(keys) -> all_settings()                  back to the defaults
    get_secret(name) -> str | None    set_secret(name, value | None)   (None/"" deletes; validated)
    secret_info(name) -> {name, label, set, masked, updated_at}
    stored_setting_keys() -> {keys with a stored value}
    get_meta(key, default=None) / set_meta(key, value)      small JSON values (import markers, last key test, ...)

Profiles, creators, streams
    list_profiles() / get_profile(name) -> rows + `creators` / `streams` counts ; enabled_profiles() -> [name]
    ensure_profile(name, enabled=False) -> bool (inserted)
    update_profile(name, enabled=None, overrides=None) -> row   (overrides validated against watchlist + profile.yaml)
    list_creators(profile=None, include_paused=True) -> rows by (profile, priority, id)   get_creator(id)
    add_creator(profile, name, url, match="any", *, paused=False, priority=100) -> id
    update_creator(id, **f) -> row      (name, url, match, paused, priority)      delete_creator(id) -> bool
    list_streams(profile=None, include_paused=True) / get_stream(id)   rows + `platform` (stream_platform(url):
                                        "twitch" | "kick" | "youtube" | "other")
    add_stream(profile, name, url, topic_only=True, *, paused=False, priority=100) -> id
    update_stream(id, **f) -> row       (name, url, topic_only, paused, priority)  delete_stream(id) -> bool

Jobs (kinds JOB_KINDS, statuses JOB_STATUSES, stages JOB_STAGES; every job row also carries `group`)
    create_job(kind, profile, **fields) -> id          default status "queued", stage per kind (DEFAULT_STAGE);
                                                       the profile must have a row (ValueError otherwise)
    get_job(id) -> row | None
    list_jobs(status=None, kind=None, profile=None, limit=50, offset=0, *, group=None, parent_id=None,
              run_id=None) -> (rows newest first, total)       status/kind may be a str or a list
    update_job(id, **fields) -> row      leaving "running" clears the lock; a final status (FINAL_STATUSES) sets
                                         finished_at, any other clears it; JSON fields (params, result) are replaced
    transition_job(id, from_statuses, **fields) -> row | None  compare-and-set on status (None: it had moved on)
    add_job_event(job_id, message, level="info") -> id ; job_events(job_id, limit=200, after_id=None) -> oldest first
    prune_job_events(older_than_s, *, levels=None, now=None) -> n   events (of `levels`, default all) of jobs that
                                         ended more than older_than_s ago; a job still active keeps all its events
    claim_next_job(worker_id, kinds=None, *, serial_kinds=SERIAL_KINDS, now=None) -> row | None
        atomic: the queued job with not_before <= now first by (priority, created_at, id) -> running + locked;
        a kind in serial_kinds is skipped while another job of that kind is running (renders number videos)
    heartbeat_job(job_id, worker_id, now=None) -> bool   refresh locked_at; False when the lock is gone
    release_stale_jobs(older_than_s, now=None) -> [ids]  running jobs whose lock is older go back to queued
    find_job_by_clipper_job(clipper_job, kinds=("source", "live_chunk")) -> newest row | None
    job_counts(profile=None) -> {status: n} for every status ; count_jobs(status=None, kind=None, profile=None,
              finished_since=None) -> int ; job_group(status) -> "waiting" | "running" | "done" | "failed" | "canceled"
    job_group_counts(kind=None, profile=None) -> {waiting, running, done, failed, canceled}

Picks (statuses PICK_STATUSES; rows carry `job`: {id, kind, profile, title, channel, source_url, clipper_job, status}
       and `video_id`: the first video rendered from the pick, or None)
    replace_picks(job_id, picks, *, status="proposed") -> [ids by idx]
        picks: clipper pick dicts, or {"data": pick, "reason"?, "click_trigger"?, "check_report"?,
        "simulate_report"?, "status"?}; upserts by idx (ids stay stable), drops idx beyond the list; refuses
        (ValueError) while a pick of the job is approved/rendering/rendered
    list_picks(status=None, profile=None, job_id=None, limit=50, offset=0, *, order="job") -> (rows, total)
        order "job": by job newest first, then idx; "queue": oldest first (the approval queue);
        "decided": newest decision first (decided_at, else updated_at)
    get_pick(id) ; update_pick(id, **f) -> row   (approved/rejected stamp decided_at) ; pick_counts(profile=None)
    transition_pick(id, from_statuses, to_status, **f) -> row | None   compare-and-set

Videos, variants, posts, social accounts
    add_video(profile, folder, *, pick_id, job_id, lang="en", day, number, title, description, sources) -> id
        idempotent on folder (stored relative to ROOT; day/number parsed from ".../<day>/video N" when not given)
    add_variant(video_id, file, *, rank=0, hook, hook_type, arm, title, description, hashtags, predicted,
                duration) -> id      idempotent on (video_id, file)
    list_videos(profile=None, day=None, posted=None, q=None, archived=False, limit=50, offset=0, *, lang=None,
                job_id=None, pick_id=None) -> (rows by day, then number, newest first; total)   archived=None: both
    get_video(id) ; get_variant(id) ; update_video(id, **f)    video rows embed `variants`, variants embed `posts`
    add_post(variant_id, platform, *, account_id, status="posted", external_id, url, privacy, note, error,
             posted_at) -> id ; get_post(id) ; update_post(id, **f) ; delete_post(id) -> bool ; list_posts(variant_id)
    list_accounts(platform=None) ; get_account(id, with_credentials=False)   rows never carry `credentials` unless
             asked; they carry `has_credentials`
    add_account(platform, label, *, handle, profile, notes, connected=False, external_id, credentials) -> id
    update_account(id, **f) -> row ; delete_account(id) -> bool

Usage and costs (cost computed with costs.py at insert time)
    add_usage(job_id, purpose, model, input_tokens=0, output_tokens=0, cache_creation_tokens=0,
              cache_read_tokens=0, request_id=None, *, cache_creation_1h_tokens=0, ts=None) -> row (+ known_model)
    usage_today() -> {day, eur, usd, requests, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                      tokens}
    usage_by_day(days=30) -> one such dict per local day, oldest first, zero days included
    usage_by_purpose(days=30) -> [{purpose, requests, usd, eur, tokens}] ; recent_usage(limit=50) -> newest first

Alerts and runs
    raise_alert(key, level, message, details=None) -> row + {"new": bool}  (a resolved alert re-opens, unacknowledged)
    resolve_alert(key) -> bool ; get_alert(key) ; list_alerts(active_only=True) (errors first) ; ack_alert(id) -> bool
    start_run(kind, profile=None, trigger=None) -> id ; finish_run(id, ok, summary=None)
    last_run(kind, profile=None, finished_only=False) -> row | None ; list_runs(kind=None, limit=20)
    prune_runs(kind, older_than_s, now=None) -> n   finished runs of `kind` that started longer ago; the newest and
                                         the newest finished of each profile stay ("last scan" / "last cleanup")

Login
    get_user(username=None) -> row | None (the first user when no name) ; create_user(username, password_hash,
    totp_secret=None, *, totp_enabled=False, recovery_codes=None) -> id ; update_user(user_id=None, **f) -> row
    delete_user(user_id=None) -> bool   (the first user when no id; its sessions go with it)
    create_session(token_hash, user_id, expires_at, user_agent=None, now=None)
    get_session(token_hash, now=None) -> row | None (expired ones are deleted) ; touch_session(token_hash, expires_at)
    delete_session(token_hash) ; delete_user_sessions(user_id, keep=None) -> n ; purge_sessions(now=None) -> n
    record_login_attempt(key, ok, ts=None) ; recent_failures(key, since) -> n
"""
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, timedelta

import yaml

from . import costs, db, paths, security, settings

transaction = db.transaction

JOB_KINDS = ("source", "live_chunk", "render", "dub", "upload")
JOB_STATUSES = ("queued", "running", "waiting_approval", "waiting_budget", "waiting_key", "done", "failed", "skipped",
                "canceled")
JOB_STAGES = ("fetch", "analyze", "select", "gate", "approval", "render", "dub", "upload", "finished")
FINAL_STATUSES = ("done", "failed", "skipped", "canceled")
JOB_GROUPS = {"waiting": ("queued", "waiting_approval", "waiting_budget", "waiting_key"), "running": ("running",),
              "done": ("done", "skipped"), "failed": ("failed",), "canceled": ("canceled",)}
DEFAULT_STAGE = {"source": "fetch", "live_chunk": "select", "render": "render", "dub": "dub", "upload": "upload"}
SERIAL_KINDS = ("render", "dub")
PICK_STATUSES = ("proposed", "approved", "rendering", "rendered", "rejected", "failed")
PICK_LOCKED = ("approved", "rendering", "rendered")
POST_STATUSES = ("posted", "uploading", "failed")
ALERT_LEVELS = ("error", "warning", "info")
RUN_KINDS = ("scan", "live_watch", "cleanup", "health")
CREATOR_MATCH = ("any", "keywords")

JSON_COLS = {"settings": {"value"}, "users": {"recovery_codes"}, "profiles": {"overrides"}, "runs": {"summary"},
             "jobs": {"params", "result"}, "picks": {"data", "check_report", "simulate_report"},
             "videos": {"sources"}, "variants": {"hashtags"}, "social_accounts": {"credentials"},
             "alerts": {"details"}, "meta": {"value"}}
BOOL_COLS = {"users": {"totp_enabled"}, "profiles": {"enabled"}, "creators": {"paused"},
             "streams": {"topic_only", "paused"}, "runs": {"ok"}, "videos": {"archived"},
             "social_accounts": {"connected"}, "alerts": {"acknowledged"}, "login_attempts": {"ok"}}
EDITABLE = {
    "users": {"username", "password_hash", "totp_secret", "totp_enabled", "totp_last_counter", "recovery_codes"},
    "creators": {"name", "url", "match", "paused", "priority"},
    "streams": {"name", "url", "topic_only", "paused", "priority"},
    "jobs": {"kind", "profile", "run_id", "parent_id", "clipper_job", "source_url", "title", "channel", "section",
             "status", "stage", "progress", "error", "attempts", "priority", "params", "result", "started_at",
             "finished_at", "not_before", "locked_by", "locked_at"},
    "picks": {"idx", "data", "status", "reason", "click_trigger", "check_report", "simulate_report", "error",
              "decided_at"},
    "videos": {"pick_id", "job_id", "profile", "lang", "day", "number", "title", "description", "sources", "archived"},
    "posts": {"account_id", "platform", "status", "external_id", "url", "privacy", "note", "error", "posted_at"},
    "social_accounts": {"platform", "label", "handle", "profile", "connected", "external_id", "credentials", "notes"},
}


# ---------------------------------------------------------------- helpers

def _now():
    return time.time()


def _c():
    return db.conn()


def _dump(value):
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(text):
    return None if text is None else json.loads(text)


def _enc(table, col, value):
    if col in JSON_COLS.get(table, ()):
        return _dump(value)
    if col in BOOL_COLS.get(table, ()):
        return None if value is None else int(bool(value))
    return value


def _row(table, row):
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLS.get(table, ()):
        if col in d:
            d[col] = _load(d[col])
    for col in BOOL_COLS.get(table, ()):
        if col in d and d[col] is not None:
            d[col] = bool(d[col])
    return d


def _in(col, values):
    values = [values] if isinstance(values, str) else list(values)
    return f"{col} IN ({','.join('?' * len(values))})", values


def _where(conds):
    sql = [c for c, _ in conds]
    args = [a for _, vals in conds for a in vals]
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


def _update(table, key_col, key, fields, extra=None):
    bad = set(fields) - EDITABLE[table]
    if bad:
        raise ValueError(f"unknown {table} field(s): {', '.join(sorted(bad))}")
    cols = {k: _enc(table, k, v) for k, v in fields.items()}
    cols.update(extra or {})
    if not cols:
        return 0
    sets = ", ".join(f"{k} = ?" for k in cols)
    with transaction() as c, _conflicts():
        return c.execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", [*cols.values(), key]).rowcount


def _insert(table, cols):
    names = list(cols)
    with transaction() as c, _conflicts():
        cur = c.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        [_enc(table, k, cols[k]) for k in names])
        return cur.lastrowid


@contextmanager
def _conflicts():
    """A UNIQUE/foreign-key violation as the module's ValueError (German, shown to the user)."""
    try:
        yield
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if "UNIQUE" in msg:
            raise ValueError(f"Das gibt es schon ({msg.split(':', 1)[-1].strip()})") from None
        raise ValueError(f"Ungültiger Verweis oder fehlender Wert ({msg})") from None


def _check_choice(value, choices, what):
    if value not in choices:
        raise ValueError(f"{what} '{value}' unknown (one of {', '.join(choices)})")


# ---------------------------------------------------------------- settings, secrets, meta

def get_setting(key):
    if key not in settings.REGISTRY:
        raise KeyError(key)
    row = _c().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return settings.default(key) if row is None else _load(row[0])


def all_settings():
    stored = {r["key"]: _load(r["value"]) for r in _c().execute("SELECT key, value FROM settings")}
    return {k: stored[k] if k in stored else settings.default(k) for k in settings.REGISTRY}


def set_settings(changes, only_unset=False):
    clean = settings.validate(changes)
    now = _now()
    with transaction() as c:
        for k, v in clean.items():
            c.execute(("INSERT OR IGNORE" if only_unset else "INSERT OR REPLACE")
                      + " INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (k, json.dumps(v), now))
    return all_settings()


def reset_settings(keys):
    unknown = [k for k in keys if k not in settings.REGISTRY]
    if unknown:
        raise KeyError(unknown[0])
    with transaction() as c:
        for k in keys:
            c.execute("DELETE FROM settings WHERE key = ?", (k,))
    return all_settings()


def stored_setting_keys():
    return {r[0] for r in _c().execute("SELECT key FROM settings")}


def get_secret(name):
    if name not in settings.SECRETS:
        raise KeyError(name)
    row = _c().execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def set_secret(name, value):
    if name not in settings.SECRETS:
        raise KeyError(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        with transaction() as c:
            c.execute("DELETE FROM secrets WHERE name = ?", (name,))
        return
    value = settings.validate_secret(name, value)
    with transaction() as c:
        c.execute("INSERT OR REPLACE INTO secrets (name, value, updated_at) VALUES (?, ?, ?)", (name, value, _now()))


def secret_info(name):
    if name not in settings.SECRETS:
        raise KeyError(name)
    row = _c().execute("SELECT value, updated_at FROM secrets WHERE name = ?", (name,)).fetchone()
    return {"name": name, "label": settings.SECRETS[name]["label"], "set": row is not None,
            "masked": security.mask(row[0]) if row else "", "updated_at": row[1] if row else None}


def get_meta(key, default=None):
    row = _c().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return _load(row[0])
    except ValueError:  # schema_version is stored as plain text
        return row[0]


def set_meta(key, value):
    if key == "schema_version":
        raise ValueError("schema_version is managed by db.py")
    with transaction() as c:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, _dump(value)))


# ---------------------------------------------------------------- profiles, creators, streams

_PROFILE_SELECT = ("SELECT profiles.*, "
                   "(SELECT COUNT(*) FROM creators WHERE creators.profile = profiles.name) AS creators, "
                   "(SELECT COUNT(*) FROM streams WHERE streams.profile = profiles.name) AS streams FROM profiles")


def list_profiles():
    return [_row("profiles", r) for r in _c().execute(f"{_PROFILE_SELECT} ORDER BY name")]


def get_profile(name):
    return _row("profiles", _c().execute(f"{_PROFILE_SELECT} WHERE name = ?", (name,)).fetchone())


def enabled_profiles():
    return [r[0] for r in _c().execute("SELECT name FROM profiles WHERE enabled = 1 ORDER BY name")]


def ensure_profile(name, enabled=False):
    with transaction() as c:
        return c.execute("INSERT OR IGNORE INTO profiles (name, enabled, overrides, updated_at) VALUES (?, ?, '{}', ?)",
                         (name, int(bool(enabled)), _now())).rowcount == 1


def _yaml_file(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def update_profile(name, enabled=None, overrides=None):
    if not get_profile(name):
        raise ValueError(f"Unbekanntes Profil „{name}“")
    cols = {"updated_at": _now()}
    if enabled is not None:
        cols["enabled"] = int(bool(enabled))
    if overrides is not None:
        base = {**_yaml_file(paths.repo_watchlist()), **_yaml_file(paths.repo_profiles_dir() / name / "profile.yaml")}
        try:
            cols["overrides"] = _dump(settings.validate_overrides(overrides, base, f"profiles/{name}/profile.yaml"))
        except ValueError as e:
            raise settings.SettingsError({"overrides": str(e)}) from None
    with transaction() as c:
        c.execute(f"UPDATE profiles SET {', '.join(f'{k} = ?' for k in cols)} WHERE name = ?", [*cols.values(), name])
    return get_profile(name)


def _need_profile(profile):
    if not get_profile(profile):
        raise ValueError(f"Unbekanntes Profil „{profile}“")


def list_creators(profile=None, include_paused=True):
    conds = ([("profile = ?", [profile])] if profile else []) + ([] if include_paused else [("paused = 0", [])])
    where, args = _where(conds)
    return [_row("creators", r) for r in
            _c().execute(f"SELECT * FROM creators{where} ORDER BY profile, priority, id", args)]


def get_creator(creator_id):
    return _row("creators", _c().execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone())


def _creator_fields(f):
    if "match" in f:
        _check_choice(f["match"], CREATOR_MATCH, "match")
    for k in ("name", "url"):
        if k in f and not str(f[k] or "").strip():
            raise ValueError("Name und URL dürfen nicht leer sein")
        if k in f:
            f[k] = str(f[k]).strip()
    if "priority" in f:
        f["priority"] = int(f["priority"])
    return f


def add_creator(profile, name, url, match="any", *, paused=False, priority=100):
    _need_profile(profile)
    f = _creator_fields({"name": name, "url": url, "match": match, "priority": priority})
    with transaction() as c:
        if c.execute("SELECT 1 FROM creators WHERE profile = ? AND lower(url) = lower(?)",
                     (profile, f["url"])).fetchone():
            raise ValueError(f"{f['url']} wird in „{profile}“ schon beobachtet")
        return _insert("creators", {"profile": profile, **f, "paused": paused, "created_at": _now()})


def update_creator(creator_id, **f):
    _update("creators", "id", creator_id, _creator_fields(f))
    return get_creator(creator_id)


def delete_creator(creator_id):
    with transaction() as c:
        return c.execute("DELETE FROM creators WHERE id = ?", (creator_id,)).rowcount == 1


def stream_platform(url):
    m = re.search(r"(twitch\.tv|kick\.com|youtube\.com|youtu\.be)", url or "")
    return {"twitch.tv": "twitch", "kick.com": "kick"}.get(m.group(1), "youtube") if m else "other"


def _stream(row):
    d = _row("streams", row)
    if d is not None:
        d["platform"] = stream_platform(d["url"])
    return d


def list_streams(profile=None, include_paused=True):
    conds = ([("profile = ?", [profile])] if profile else []) + ([] if include_paused else [("paused = 0", [])])
    where, args = _where(conds)
    return [_stream(r) for r in _c().execute(f"SELECT * FROM streams{where} ORDER BY profile, priority, id", args)]


def get_stream(stream_id):
    return _stream(_c().execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone())


def add_stream(profile, name, url, topic_only=True, *, paused=False, priority=100):
    _need_profile(profile)
    f = _creator_fields({"name": name, "url": url, "priority": priority})
    with transaction() as c:
        if c.execute("SELECT 1 FROM streams WHERE profile = ? AND (name = ? OR lower(url) = lower(?))",
                     (profile, f["name"], f["url"])).fetchone():
            raise ValueError(f"Der Stream {f['name']} ({f['url']}) ist in „{profile}“ schon eingetragen")
        return _insert("streams", {"profile": profile, **f, "topic_only": topic_only, "paused": paused,
                                   "created_at": _now()})


def update_stream(stream_id, **f):
    _update("streams", "id", stream_id, _creator_fields(f))
    return get_stream(stream_id)


def delete_stream(stream_id):
    with transaction() as c:
        return c.execute("DELETE FROM streams WHERE id = ?", (stream_id,)).rowcount == 1


# ---------------------------------------------------------------- jobs

def job_group(status):
    return next((g for g, sts in JOB_GROUPS.items() if status in sts), "waiting")


def _job(row):
    d = _row("jobs", row)
    if d is not None:
        d["group"] = job_group(d["status"])
    return d


def _job_fields(fields, now):
    if "params" in fields and fields["params"] is None:
        fields = {**fields, "params": {}}
    if "kind" in fields:
        _check_choice(fields["kind"], JOB_KINDS, "job kind")
    if "status" in fields:
        _check_choice(fields["status"], JOB_STATUSES, "job status")
        extra = {}
        if fields["status"] != "running":
            extra.update(locked_by=None, locked_at=None)
        extra["finished_at"] = now if fields["status"] in FINAL_STATUSES else None
        fields = {**extra, **fields}
    if fields.get("stage") is not None:
        _check_choice(fields["stage"], JOB_STAGES, "job stage")
    return fields


def create_job(kind, profile, **fields):
    _check_choice(kind, JOB_KINDS, "job kind")
    _need_profile(profile)
    now = _now()
    cols = {"status": "queued", "stage": DEFAULT_STAGE[kind], "params": {}, **fields}
    cols = _job_fields(cols, now)
    bad = set(cols) - EDITABLE["jobs"]
    if bad:
        raise ValueError(f"unknown jobs field(s): {', '.join(sorted(bad))}")
    return _insert("jobs", {"kind": kind, "profile": profile, **cols, "created_at": now, "updated_at": now})


def get_job(job_id):
    return _job(_c().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def _job_conds(status=None, kind=None, profile=None, group=None, parent_id=None, run_id=None):
    conds = []
    if status:
        conds.append(_in("status", status))
    if group:
        if group not in JOB_GROUPS:
            raise ValueError(f"job group '{group}' unknown (one of {', '.join(JOB_GROUPS)})")
        conds.append(_in("status", JOB_GROUPS[group]))
    if kind:
        conds.append(_in("kind", kind))
    if profile:
        conds.append(("profile = ?", [profile]))
    if parent_id is not None:
        conds.append(("parent_id = ?", [parent_id]))
    if run_id is not None:
        conds.append(("run_id = ?", [run_id]))
    return conds


def list_jobs(status=None, kind=None, profile=None, limit=50, offset=0, *, group=None, parent_id=None, run_id=None):
    where, args = _where(_job_conds(status, kind, profile, group, parent_id, run_id))
    c = _c()
    total = c.execute(f"SELECT COUNT(*) FROM jobs{where}", args).fetchone()[0]
    rows = c.execute(f"SELECT * FROM jobs{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                     [*args, int(limit), int(offset)])
    return [_job(r) for r in rows], total


def update_job(job_id, **fields):
    now = _now()
    _update("jobs", "id", job_id, _job_fields(fields, now) if fields else {}, {"updated_at": now})
    return get_job(job_id)


def transition_job(job_id, from_statuses, **fields):
    with transaction() as c:
        row = c.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row[0] not in ([from_statuses] if isinstance(from_statuses, str) else from_statuses):
            return None
        return update_job(job_id, **fields)


def add_job_event(job_id, message, level="info"):
    return _insert("job_events", {"job_id": job_id, "ts": _now(), "level": level, "message": str(message)})


def job_events(job_id, limit=200, after_id=None):
    if after_id is not None:
        rows = _c().execute("SELECT * FROM job_events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?",
                            (job_id, after_id, int(limit))).fetchall()
    else:
        rows = _c().execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                            (job_id, int(limit))).fetchall()[::-1]
    return [dict(r) for r in rows]


def prune_job_events(older_than_s, *, levels=None, now=None):
    cutoff = (_now() if now is None else now) - older_than_s
    final, fargs = _in("status", FINAL_STATUSES)
    conds = [(f"job_id IN (SELECT id FROM jobs WHERE {final} AND finished_at IS NOT NULL AND finished_at < ?)",
              [*fargs, cutoff]), ("ts < ?", [cutoff])]
    if levels:
        conds.append(_in("level", levels))
    where, args = _where(conds)
    with transaction() as c:
        return c.execute(f"DELETE FROM job_events{where}", args).rowcount


def claim_next_job(worker_id, kinds=None, *, serial_kinds=SERIAL_KINDS, now=None):
    now = _now() if now is None else now
    if kinds is not None and not kinds:
        return None
    with transaction() as c:
        conds = [("status = 'queued'", []), ("not_before <= ?", [now])]
        if kinds is not None:
            conds.append(_in("kind", kinds))
        busy = [k for k in serial_kinds
                if c.execute("SELECT 1 FROM jobs WHERE status = 'running' AND kind = ? LIMIT 1", (k,)).fetchone()]
        if busy:
            sql, vals = _in("kind", busy)
            conds.append(("NOT " + sql, vals))
        where, args = _where(conds)
        row = c.execute(f"SELECT id FROM jobs{where} ORDER BY priority, created_at, id LIMIT 1", args).fetchone()
        if row is None:
            return None
        c.execute("UPDATE jobs SET status = 'running', locked_by = ?, locked_at = ?, "
                  "started_at = COALESCE(started_at, ?), finished_at = NULL, updated_at = ? WHERE id = ?",
                  (str(worker_id), now, now, now, row[0]))
        return get_job(row[0])


def heartbeat_job(job_id, worker_id, now=None):
    with transaction() as c:
        return c.execute("UPDATE jobs SET locked_at = ? WHERE id = ? AND locked_by = ? AND status = 'running'",
                         (_now() if now is None else now, job_id, str(worker_id))).rowcount == 1


def release_stale_jobs(older_than_s, now=None):
    now = _now() if now is None else now
    with transaction() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM jobs WHERE status = 'running' AND "
                                       "(locked_at IS NULL OR locked_at <= ?)", (now - older_than_s,))]
        for jid in ids:
            c.execute("UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL, updated_at = ? "
                      "WHERE id = ?", (now, jid))
            add_job_event(jid, "Job wurde unterbrochen (Neustart oder hängender Worker) und wieder eingereiht",
                          "warning")
    return ids


def find_job_by_clipper_job(clipper_job, kinds=("source", "live_chunk")):
    sql, args = _in("kind", kinds)
    return _job(_c().execute(f"SELECT * FROM jobs WHERE clipper_job = ? AND {sql} ORDER BY id DESC LIMIT 1",
                             [clipper_job, *args]).fetchone())


def job_counts(profile=None):
    where, args = _where([("profile = ?", [profile])] if profile else [])
    counts = dict.fromkeys(JOB_STATUSES, 0)
    for status, n in _c().execute(f"SELECT status, COUNT(*) FROM jobs{where} GROUP BY status", args):
        counts[status] = n
    return counts


def job_group_counts(kind=None, profile=None):
    by_status = {}
    where, args = _where(_job_conds(kind=kind, profile=profile))
    for status, n in _c().execute(f"SELECT status, COUNT(*) FROM jobs{where} GROUP BY status", args):
        by_status[status] = n
    return {g: sum(by_status.get(st, 0) for st in sts) for g, sts in JOB_GROUPS.items()}


def count_jobs(status=None, kind=None, profile=None, finished_since=None):
    conds = _job_conds(status, kind, profile)
    if finished_since is not None:
        conds.append(("finished_at >= ?", [finished_since]))
    where, args = _where(conds)
    return _c().execute(f"SELECT COUNT(*) FROM jobs{where}", args).fetchone()[0]


# ---------------------------------------------------------------- picks

_PICK_SELECT = ("SELECT picks.*, "
                "(SELECT id FROM videos WHERE videos.pick_id = picks.id ORDER BY id LIMIT 1) AS video_id, "
                "jobs.kind AS j_kind, jobs.profile AS j_profile, jobs.title AS j_title, jobs.channel AS j_channel, "
                "jobs.source_url AS j_source_url, jobs.clipper_job AS j_clipper_job, jobs.status AS j_status "
                "FROM picks JOIN jobs ON jobs.id = picks.job_id")


def _pick(row):
    if row is None:
        return None
    d = dict(row)
    job = {k[2:]: d.pop(k) for k in list(d) if k.startswith("j_")}
    d = _row("picks", d)
    d["job"] = {"id": d["job_id"], **job}
    return d


def replace_picks(job_id, picks, *, status="proposed"):
    _check_choice(status, PICK_STATUSES, "pick status")
    now = _now()
    with transaction() as c:
        job = c.execute("SELECT profile FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise ValueError(f"Job {job_id} gibt es nicht")
        existing = {r["idx"]: dict(r) for r in
                    c.execute("SELECT id, idx, status FROM picks WHERE job_id = ?", (job_id,))}
        locked = sorted(r["idx"] for r in existing.values() if r["status"] in PICK_LOCKED)
        if locked:
            raise ValueError(f"Job {job_id}: Pick(s) {', '.join(map(str, locked))} sind schon freigegeben oder "
                             f"gerendert und werden nicht ersetzt")
        ids = []
        for idx, item in enumerate(picks, 1):
            meta = item if isinstance(item.get("data"), dict) else {"data": item}
            st = meta.get("status", status)
            _check_choice(st, PICK_STATUSES, "pick status")
            cols = {"data": _dump(meta["data"]), "status": st, "reason": meta.get("reason"),
                    "click_trigger": meta.get("click_trigger"), "check_report": _dump(meta.get("check_report")),
                    "simulate_report": _dump(meta.get("simulate_report")), "error": None, "decided_at": None,
                    "updated_at": now}
            if idx in existing:
                c.execute(f"UPDATE picks SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                          [*cols.values(), existing[idx]["id"]])
                ids.append(existing[idx]["id"])
            else:
                cols.update(job_id=job_id, idx=idx, profile=job["profile"], created_at=now)
                cur = c.execute(f"INSERT INTO picks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                list(cols.values()))
                ids.append(cur.lastrowid)
        c.execute("DELETE FROM picks WHERE job_id = ? AND idx > ?", (job_id, len(picks)))
    return ids


_PICK_ORDER = {"job": "picks.job_id DESC, picks.idx",
               "queue": "picks.created_at, picks.job_id, picks.idx",
               "decided": "COALESCE(picks.decided_at, picks.updated_at, picks.created_at) DESC, picks.id DESC"}


def list_picks(status=None, profile=None, job_id=None, limit=50, offset=0, *, order="job"):
    _check_choice(order, tuple(_PICK_ORDER), "pick order")
    conds = []
    if status:
        conds.append(_in("picks.status", status))
    if profile:
        conds.append(("picks.profile = ?", [profile]))
    if job_id is not None:
        conds.append(("picks.job_id = ?", [job_id]))
    where, args = _where(conds)
    c = _c()
    total = c.execute(f"SELECT COUNT(*) FROM picks{where}", args).fetchone()[0]
    rows = c.execute(f"{_PICK_SELECT}{where} ORDER BY {_PICK_ORDER[order]} LIMIT ? OFFSET ?",
                     [*args, int(limit), int(offset)])
    return [_pick(r) for r in rows], total


def pick_counts(profile=None):
    where, args = _where([("profile = ?", [profile])] if profile else [])
    counts = dict.fromkeys(PICK_STATUSES, 0)
    for status, n in _c().execute(f"SELECT status, COUNT(*) FROM picks{where} GROUP BY status", args):
        counts[status] = n
    return counts


def get_pick(pick_id):
    return _pick(_c().execute(f"{_PICK_SELECT} WHERE picks.id = ?", (pick_id,)).fetchone())


def update_pick(pick_id, **f):
    now = _now()
    if "status" in f:
        _check_choice(f["status"], PICK_STATUSES, "pick status")
        if f["status"] in ("approved", "rejected") and "decided_at" not in f:
            f["decided_at"] = now
    _update("picks", "id", pick_id, f, {"updated_at": now})
    return get_pick(pick_id)


def transition_pick(pick_id, from_statuses, to_status, **f):
    with transaction() as c:
        row = c.execute("SELECT status FROM picks WHERE id = ?", (pick_id,)).fetchone()
        if row is None or row[0] not in ([from_statuses] if isinstance(from_statuses, str) else from_statuses):
            return None
        return update_pick(pick_id, status=to_status, **f)


# ---------------------------------------------------------------- videos, variants, posts

_FOLDER_RE = re.compile(r"(\d{4}-\d{2}-\d{2})/video (\d+)/?$")


def add_video(profile, folder, *, pick_id=None, job_id=None, lang="en", day=None, number=None, title=None,
              description=None, sources=None):
    folder = paths.rel(folder)
    m = _FOLDER_RE.search(folder)
    cols = {"pick_id": pick_id, "job_id": job_id, "profile": profile, "lang": lang or "en",
            "day": day or (m.group(1) if m else None), "number": number if number is not None else
            (int(m.group(2)) if m else None), "title": title, "description": description,
            "sources": _dump(sources or [])}
    with transaction() as c:
        c.execute(f"INSERT INTO videos (folder, {', '.join(cols)}, created_at) VALUES "
                  f"(?, {', '.join('?' * len(cols))}, ?) ON CONFLICT(folder) DO UPDATE SET "
                  + ", ".join(f"{k} = excluded.{k}" for k in cols), [folder, *cols.values(), _now()])
        return c.execute("SELECT id FROM videos WHERE folder = ?", (folder,)).fetchone()[0]


def add_variant(video_id, file, *, rank=0, hook=None, hook_type=None, arm=None, title=None, description=None,
                hashtags=None, predicted=None, duration=None):
    file = paths.rel(file)
    cols = {"rank": rank, "hook": hook, "hook_type": hook_type, "arm": arm, "title": title,
            "description": description, "hashtags": _dump(hashtags or []), "predicted": predicted,
            "duration": duration}
    with transaction() as c:
        c.execute(f"INSERT INTO variants (video_id, file, {', '.join(cols)}, created_at) VALUES "
                  f"(?, ?, {', '.join('?' * len(cols))}, ?) ON CONFLICT(video_id, file) DO UPDATE SET "
                  + ", ".join(f"{k} = excluded.{k}" for k in cols), [video_id, file, *cols.values(), _now()])
        return c.execute("SELECT id FROM variants WHERE video_id = ? AND file = ?", (video_id, file)).fetchone()[0]


def _variants_for(video_ids):
    if not video_ids:
        return {}
    sql, args = _in("video_id", video_ids)
    variants = [_row("variants", r) for r in
                _c().execute(f"SELECT * FROM variants WHERE {sql} ORDER BY video_id, rank, id", args)]
    posts = {}
    if variants:
        psql, pargs = _in("variant_id", [v["id"] for v in variants])
        for p in _c().execute(f"SELECT * FROM posts WHERE {psql} ORDER BY id", pargs):
            posts.setdefault(p["variant_id"], []).append(dict(p))
    out = {}
    for v in variants:
        v["posts"] = posts.get(v["id"], [])
        out.setdefault(v["video_id"], []).append(v)
    return out


def list_videos(profile=None, day=None, posted=None, q=None, archived=False, limit=50, offset=0, *, lang=None,
                job_id=None, pick_id=None):
    conds = []
    for col, val in (("profile", profile), ("day", day), ("lang", lang), ("job_id", job_id), ("pick_id", pick_id)):
        if val is not None:
            conds.append((f"videos.{col} = ?", [val]))
    if archived is not None:
        conds.append(("videos.archived = ?", [int(bool(archived))]))
    if posted is not None:
        conds.append((("" if posted else "NOT ") + "EXISTS (SELECT 1 FROM variants v JOIN posts p ON "
                      "p.variant_id = v.id WHERE v.video_id = videos.id AND p.status = 'posted')", []))
    if q:
        like = f"%{q}%"
        conds.append(("(videos.title LIKE ? OR videos.description LIKE ? OR EXISTS (SELECT 1 FROM variants v "
                      "WHERE v.video_id = videos.id AND (v.hook LIKE ? OR v.title LIKE ?)))", [like] * 4))
    where, args = _where(conds)
    c = _c()
    total = c.execute(f"SELECT COUNT(*) FROM videos{where}", args).fetchone()[0]
    rows = [_row("videos", r) for r in c.execute(f"SELECT * FROM videos{where} ORDER BY day DESC, number DESC, "
                                                 f"created_at DESC, id DESC LIMIT ? OFFSET ?",
                                                 [*args, int(limit), int(offset)])]
    by_video = _variants_for([r["id"] for r in rows])
    for r in rows:
        r["variants"] = by_video.get(r["id"], [])
    return rows, total


def get_video(video_id):
    row = _row("videos", _c().execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone())
    if row:
        row["variants"] = _variants_for([video_id]).get(video_id, [])
    return row


def get_variant(variant_id):
    row = _row("variants", _c().execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone())
    if row:
        row["posts"] = list_posts(variant_id)
    return row


def update_video(video_id, **f):
    if "folder" in f:
        raise ValueError("a video's folder can't change")
    _update("videos", "id", video_id, f)
    return get_video(video_id)


def add_post(variant_id, platform, *, account_id=None, status="posted", external_id=None, url=None, privacy=None,
             note=None, error=None, posted_at=None):
    _check_choice(status, POST_STATUSES, "post status")
    now = _now()
    if posted_at is None and status == "posted":
        posted_at = now
    return _insert("posts", {"variant_id": variant_id, "account_id": account_id, "platform": platform,
                             "status": status, "external_id": external_id, "url": url, "privacy": privacy,
                             "note": note, "error": error, "created_at": now, "posted_at": posted_at})


def get_post(post_id):
    row = _c().execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return dict(row) if row else None


def update_post(post_id, **f):
    if "status" in f:
        _check_choice(f["status"], POST_STATUSES, "post status")
        if f["status"] == "posted" and "posted_at" not in f:
            f["posted_at"] = _now()
    _update("posts", "id", post_id, f)
    return get_post(post_id)


def delete_post(post_id):
    with transaction() as c:
        return c.execute("DELETE FROM posts WHERE id = ?", (post_id,)).rowcount == 1


def list_posts(variant_id):
    return [dict(r) for r in _c().execute("SELECT * FROM posts WHERE variant_id = ? ORDER BY id", (variant_id,))]


def _account(row, with_credentials=False):
    d = _row("social_accounts", row)
    if d is not None:
        d["has_credentials"] = bool(d.get("credentials"))
        if not with_credentials:
            d.pop("credentials", None)
    return d


def list_accounts(platform=None):
    where, args = _where([("platform = ?", [platform])] if platform else [])
    return [_account(r) for r in
            _c().execute(f"SELECT * FROM social_accounts{where} ORDER BY platform, label, id", args)]


def get_account(account_id, with_credentials=False):
    return _account(_c().execute("SELECT * FROM social_accounts WHERE id = ?", (account_id,)).fetchone(),
                    with_credentials)


def add_account(platform, label, *, handle=None, profile=None, notes=None, connected=False, external_id=None,
                credentials=None):
    if not re.fullmatch(r"[a-z0-9_-]{2,32}", str(platform or "")):
        raise ValueError("Plattform: Kleinbuchstaben wie youtube, tiktok, instagram")
    if not str(label or "").strip():
        raise ValueError("Bitte einen Namen für das Konto angeben")
    now = _now()
    return _insert("social_accounts", {"platform": platform, "label": label.strip(), "handle": handle,
                                       "profile": profile, "connected": connected, "external_id": external_id,
                                       "credentials": credentials, "notes": notes, "created_at": now,
                                       "updated_at": now})


def update_account(account_id, **f):
    _update("social_accounts", "id", account_id, f, {"updated_at": _now()})
    return get_account(account_id)


def delete_account(account_id):
    with transaction() as c:
        return c.execute("DELETE FROM social_accounts WHERE id = ?", (account_id,)).rowcount == 1


# ---------------------------------------------------------------- usage

def add_usage(job_id, purpose, model, input_tokens=0, output_tokens=0, cache_creation_tokens=0, cache_read_tokens=0,
              request_id=None, *, cache_creation_1h_tokens=0, ts=None):
    ts = _now() if ts is None else ts
    usage = {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0,
             "cache_creation_tokens": cache_creation_tokens or 0, "cache_read_tokens": cache_read_tokens or 0,
             "cache_creation_1h_tokens": cache_creation_1h_tokens or 0}
    est = costs.estimate(model, usage)
    eur = costs.to_eur(est["usd"])
    uid = _insert("usage", {"ts": ts, "day": costs.local_day(ts), "job_id": job_id, "purpose": purpose,
                            "model": model, "input_tokens": usage["input_tokens"],
                            "output_tokens": usage["output_tokens"],
                            "cache_creation_tokens": usage["cache_creation_tokens"],
                            "cache_read_tokens": usage["cache_read_tokens"], "cost_usd": est["usd"],
                            "cost_eur": eur, "request_id": request_id})
    row = dict(_c().execute("SELECT * FROM usage WHERE id = ?", (uid,)).fetchone())
    row["known_model"] = est["known_model"]
    return row


_SUMS = ("COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS usd, COALESCE(SUM(cost_eur), 0) AS eur, "
         "COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens, "
         "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
         "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens")


def _sums(d):
    d = dict(d)
    d["usd"], d["eur"] = round(d["usd"], 6), round(d["eur"], 6)
    d["tokens"] = d["input_tokens"] + d["output_tokens"] + d["cache_creation_tokens"] + d["cache_read_tokens"]
    return d


def usage_today():
    day = costs.local_day()
    return {"day": day, **_sums(_c().execute(f"SELECT {_SUMS} FROM usage WHERE day = ?", (day,)).fetchone())}


def _last_days(days):
    today = date.fromisoformat(costs.local_day())
    return [(today - timedelta(days=i)).isoformat() for i in range(int(days) - 1, -1, -1)]


def usage_by_day(days=30):
    want = _last_days(days)
    got = {r["day"]: _sums({k: r[k] for k in r.keys() if k != "day"}) for r in
           _c().execute(f"SELECT day, {_SUMS} FROM usage WHERE day >= ? GROUP BY day", (want[0],))}
    empty = _sums({"requests": 0, "usd": 0.0, "eur": 0.0, "input_tokens": 0, "output_tokens": 0,
                   "cache_creation_tokens": 0, "cache_read_tokens": 0})
    return [{"day": d, **got.get(d, empty)} for d in want]


def usage_by_purpose(days=30):
    first = _last_days(days)[0]
    rows = _c().execute(f"SELECT purpose, {_SUMS} FROM usage WHERE day >= ? GROUP BY purpose ORDER BY eur DESC",
                        (first,))
    return [{"purpose": r["purpose"], **_sums({k: r[k] for k in r.keys() if k != "purpose"})} for r in rows]


def recent_usage(limit=50):
    return [dict(r) for r in _c().execute("SELECT * FROM usage ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),))]


# ---------------------------------------------------------------- alerts, runs

def get_alert(key):
    return _row("alerts", _c().execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone())


def raise_alert(key, level, message, details=None):
    _check_choice(level, ALERT_LEVELS, "alert level")
    now = _now()
    with transaction() as c:
        old = c.execute("SELECT resolved_at FROM alerts WHERE key = ?", (key,)).fetchone()
        new = old is None or old[0] is not None
        if old is None:
            c.execute("INSERT INTO alerts (key, level, message, details, first_seen, last_seen) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (key, level, message, _dump(details), now, now))
        elif new:
            c.execute("UPDATE alerts SET level = ?, message = ?, details = ?, first_seen = ?, last_seen = ?, "
                      "resolved_at = NULL, acknowledged = 0 WHERE key = ?",
                      (level, message, _dump(details), now, now, key))
        else:
            c.execute("UPDATE alerts SET level = ?, message = ?, details = ?, last_seen = ? WHERE key = ?",
                      (level, message, _dump(details), now, key))
        return {**get_alert(key), "new": new}


def resolve_alert(key):
    with transaction() as c:
        return c.execute("UPDATE alerts SET resolved_at = ? WHERE key = ? AND resolved_at IS NULL",
                         (_now(), key)).rowcount == 1


def list_alerts(active_only=True):
    where = " WHERE resolved_at IS NULL" if active_only else ""
    order = "CASE level WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen DESC"
    return [_row("alerts", r) for r in _c().execute(f"SELECT * FROM alerts{where} ORDER BY {order}")]


def ack_alert(alert_id):
    with transaction() as c:
        return c.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,)).rowcount == 1


def start_run(kind, profile=None, trigger=None):
    _check_choice(kind, RUN_KINDS, "run kind")
    return _insert("runs", {"kind": kind, "profile": profile, "trigger": trigger, "started_at": _now()})


def finish_run(run_id, ok, summary=None):
    with transaction() as c:
        c.execute("UPDATE runs SET finished_at = ?, ok = ?, summary = ? WHERE id = ?",
                  (_now(), int(bool(ok)), _dump(summary), run_id))


def last_run(kind, profile=None, finished_only=False):
    conds = [("kind = ?", [kind])] + ([("profile = ?", [profile])] if profile else []) \
        + ([("finished_at IS NOT NULL", [])] if finished_only else [])
    where, args = _where(conds)
    return _row("runs", _c().execute(f"SELECT * FROM runs{where} ORDER BY started_at DESC, id DESC LIMIT 1",
                                     args).fetchone())


def list_runs(kind=None, limit=20):
    where, args = _where([("kind = ?", [kind])] if kind else [])
    return [_row("runs", r) for r in
            _c().execute(f"SELECT * FROM runs{where} ORDER BY started_at DESC, id DESC LIMIT ?", [*args, int(limit)])]


def prune_runs(kind, older_than_s, now=None):
    _check_choice(kind, RUN_KINDS, "run kind")
    cutoff = (_now() if now is None else now) - older_than_s
    with transaction() as c:
        newest = "SELECT MAX(id) FROM runs WHERE kind = ?{} GROUP BY COALESCE(profile, '')"
        return c.execute(f"DELETE FROM runs WHERE kind = ? AND started_at < ? AND finished_at IS NOT NULL "
                         f"AND id NOT IN ({newest.format('')}) "
                         f"AND id NOT IN ({newest.format(' AND finished_at IS NOT NULL')})",
                         (kind, cutoff, kind, kind)).rowcount


# ---------------------------------------------------------------- login

def get_user(username=None):
    if username is None:
        row = _c().execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    else:
        row = _c().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row("users", row)


def create_user(username, password_hash, totp_secret=None, *, totp_enabled=False, recovery_codes=None):
    if not str(username or "").strip():
        raise ValueError("Bitte einen Benutzernamen angeben")
    return _insert("users", {"username": username.strip(), "password_hash": password_hash, "totp_secret": totp_secret,
                             "totp_enabled": totp_enabled, "recovery_codes": recovery_codes or [],
                             "created_at": _now()})


def update_user(user_id=None, **f):
    user = get_user() if user_id is None else _row("users", _c().execute("SELECT * FROM users WHERE id = ?",
                                                                         (user_id,)).fetchone())
    if user is None:
        raise ValueError("Es gibt noch keinen Benutzer")
    _update("users", "id", user["id"], f)
    return get_user(f.get("username", user["username"]))


def delete_user(user_id=None):
    with transaction() as c:
        if user_id is None:
            row = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            user_id = row[0]
        return c.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount == 1


def create_session(token_hash, user_id, expires_at, user_agent=None, now=None):
    now = _now() if now is None else now
    _insert("sessions", {"token_hash": token_hash, "user_id": user_id, "created_at": now, "expires_at": expires_at,
                         "last_seen": now, "user_agent": (user_agent or "")[:300] or None})


def get_session(token_hash, now=None):
    now = _now() if now is None else now
    row = _c().execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= now:
        delete_session(token_hash)
        return None
    return dict(row)


def touch_session(token_hash, expires_at=None, now=None):
    now = _now() if now is None else now
    with transaction() as c:
        if expires_at is None:
            c.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, token_hash))
        else:
            c.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                      (now, expires_at, token_hash))


def delete_session(token_hash):
    with transaction() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(user_id, keep=None):
    with transaction() as c:
        return c.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?", (user_id, keep)).rowcount


def purge_sessions(now=None):
    with transaction() as c:
        return c.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now() if now is None else now,)).rowcount


def record_login_attempt(key, ok, ts=None):
    ts = _now() if ts is None else ts
    with transaction() as c:
        c.execute("INSERT INTO login_attempts (ts, key, ok) VALUES (?, ?, ?)", (ts, key, int(bool(ok))))
        c.execute("DELETE FROM login_attempts WHERE ts < ?", (ts - 30 * 86400,))


def recent_failures(key, since):
    return _c().execute("SELECT COUNT(*) FROM login_attempts WHERE key = ? AND ok = 0 AND ts >= ?",
                        (key, since)).fetchone()[0]
