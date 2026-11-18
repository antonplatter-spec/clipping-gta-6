"""Layout previews for the Freigabe page: one composited still of a pick (`clipper preview`: the video frame with the
layout, the hook banner and the caption on screen at that moment, placed by the render's own code), cached per pick.

    still(pick_row, at=None, hook=1) -> {ok: True, key, image, at, hook, hook_visible, no_hook_arm, caption,
        placement, seconds, cached} | {ok: False, reason: "source_gone" | "busy" | "failed", message: German}
    image_path(pick_id, key) -> Path | None     the cached PNG of that pick and key (only ones this module made)

A still is keyed by everything that changes it: the pick's data, the time, the hook variant, the clipper config the
overlay gives the profile (caption defaults, looks, censoring, no-hook share) and the source files' mtimes; an edited
pick therefore gets a new key and the next request draws it. Each pick keeps its newest KEEP stills. At most
MAX_RUNNING previews run at once (the dashboard asks for every visible card); a request that can't get a slot within
WAIT_S answers "busy" instead of piling up ffmpegs, and two requests for the same key draw it once.
"""
import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import paths, runtime

MAX_RUNNING = 2
WAIT_S = 60
TIMEOUT_S = 180
KEEP = 12
KEY_RE = re.compile(r"^[0-9a-f]{24}$")
SOURCE_GONE = ("Das Quellvideo ist schon gelöscht (Aufräumen nach 2 Tagen, bei Live-Aufnahmen 3 Stunden nach dem "
               "Clippen) – eine Vorschau ist nicht mehr möglich.")
BUSY = "Gerade entstehen andere Vorschauen – gleich noch einmal versuchen."

_slots = threading.BoundedSemaphore(MAX_RUNNING)
_key_locks = defaultdict(threading.Lock)


def _dir(pick_id):
    return paths.tmp_dir() / "previews" / f"pick-{int(pick_id)}"


def _job_dir(ref):
    if not ref or not re.fullmatch(r"[\w.\-@]{1,200}", str(ref)) or str(ref).strip(".") == "":
        return None
    return paths.jobs_dir() / str(ref)


def _sources(pick):
    """The source.mp4 files the pick needs (a story cut can use other jobs), None when a job id is unusable."""
    data, cj = pick.get("data") or {}, (pick.get("job") or {}).get("clipper_job")
    refs = {s.get("job") or cj for s in data.get("segments") or [] if isinstance(s, dict)} or {cj}
    out = []
    for ref in sorted(map(str, refs)):
        d = _job_dir(ref)
        if d is None:
            return None
        out.append(d / "source.mp4")
    return out


def _config(profile):
    """The config the clipper sees for this profile now (watchlist + profile.yaml, each with the dashboard's
    overrides): a changed default gives every pick a new key at once."""
    return runtime.merge_config(runtime.watchlist_config()["effective"], runtime.profile_config(profile)["effective"])


def _lock(key):
    if len(_key_locks) > 2000:  # forget idle locks now and then
        for k in [k for k, v in _key_locks.items() if not v.locked()]:
            _key_locks.pop(k, None)
    return _key_locks[key]


def _key(pick, at, hook, sources):
    stamp = [(str(f), f.stat().st_mtime_ns) for f in sources]
    raw = json.dumps({"data": pick.get("data"), "at": at, "hook": hook, "cfg": _config(pick["profile"]),
                      "src": stamp}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def image_path(pick_id, key):
    if not isinstance(key, str) or not KEY_RE.match(key):
        return None
    f = _dir(pick_id) / f"{key}.png"
    return f if f.is_file() else None


def _prune(folder):
    pngs = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in pngs[KEEP:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def _trailing_json(text):
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    raise ValueError("no JSON")


def _result(pick_id, key, meta, cached):
    return {"ok": True, "key": key, "image": f"/api/picks/{int(pick_id)}/preview/file?key={key}", "cached": cached,
            **{k: meta.get(k) for k in ("at", "hook", "hook_visible", "no_hook_arm", "caption", "placement",
                                        "seconds")}}


def still(pick, at=None, hook=1):
    """The preview still of a pick row (store.get_pick: data, profile, job.clipper_job) at clip second `at` (None =
    the first caption while the hook shows), drawing `hook` (1 = the first hook)."""
    sources = _sources(pick)
    if not sources or not all(f.is_file() for f in sources):
        return {"ok": False, "reason": "source_gone", "message": SOURCE_GONE}
    at = None if at is None else round(max(0.0, float(at)), 1)
    key = _key(pick, at, int(hook), sources)
    folder = _dir(pick["id"])
    png, meta_file = folder / f"{key}.png", folder / f"{key}.json"
    with _lock(key):
        if png.is_file() and meta_file.is_file():
            try:
                return _result(pick["id"], key, json.loads(meta_file.read_text(encoding="utf-8")), True)
            except ValueError:
                pass
        if not _slots.acquire(timeout=WAIT_S):
            return {"ok": False, "reason": "busy", "message": BUSY}
        picks_file = folder / f"{key}.picks.json"
        try:
            paths.ensure_data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            picks_file.write_text(json.dumps([pick.get("data") or {}], ensure_ascii=False), encoding="utf-8")
            args = ["preview", "--picks", str(picks_file), "--pick", "1", "--hook", str(int(hook)), "--out", str(png)]
            if at is not None:
                args += ["--at", f"{at:.2f}"]
            t0 = time.monotonic()
            res = runtime.run_clipper(args + ["--", pick["job"]["clipper_job"]], pick["profile"], timeout=TIMEOUT_S)
        finally:
            picks_file.unlink(missing_ok=True)
            _slots.release()
        try:
            meta = _trailing_json(res.stdout) if res.returncode == 0 and png.is_file() else None
        except ValueError:
            meta = None
        if meta is None:
            png.unlink(missing_ok=True)
            tail = [x for x in (res.stderr or "").splitlines() if x.strip()]
            return {"ok": False, "reason": "failed",
                    "message": "Die Vorschau ließ sich nicht erstellen" + (f": {tail[-1][:200]}" if tail else ".")}
        meta["seconds"] = round(time.monotonic() - t0, 2)
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        _prune(folder)
        return _result(pick["id"], key, meta, False)
