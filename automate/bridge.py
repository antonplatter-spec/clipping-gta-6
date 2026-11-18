"""`python -m automate.bridge <op>`: one JSON object on stdin -> one JSON object on stdout, exit 0; on failure
`{"error": "..."}` on stdout (traceback on stderr), exit 1. Runs as a subprocess with runtime.clipper_env(profile)
(runtime.run_bridge), so it may import clipper.* (the server process never does). Like the CLI it calls
clipper.config.load_env() and config.load() first; the op gets the payload and that cfg.

Anything an op prints (clipper's progress lines, a child process) goes to stderr: stdout carries only the result.

Ops (payload -> result):
  check             {picks, job?, reserved?} -> {report, fails}      exactly `clipper check picks.json --job <job>`:
                    hooks.check_picks + the reused-footage fails; `job` is a job id or path (omit: no transcript notes).
                    `reserved` [{pick, job, label}]: picks that aren't rendered yet (no upload.json to find them by);
                    a pick sharing more than render.REUSE_TOLERANCE_S of footage with one of them (clipper's own
                    footage()/footage_overlap()) gets a `footage` fail "<label> nutzt schon N s dieser Szenen …"
  rank              {picks, keep?} -> {picks, ranking}    keep: int | "auto" (default: library.hooks_per_video()).
                    Per pick, simulate.simulate's ranking and the hooks `clipper simulate` keeps (best first, at most
                    `keep`, one per hook type, check-clean: the same rule as simulate.report's keep line). Hooks are
                    trimmed/reordered to the kept ones and titles move with them by index. With unequal hook/title
                    counts, title i still belongs to hook i; a kept hook without one takes the next title no kept hook
                    owns, so every kept hook keeps a title while there are titles left, and the rest are dropped
                    (`titles_dropped`). A single `hook`/`title` pick is ranked as one hook and left as is. A pick
                    where no hook qualifies is returned unchanged with ranking.ok = false.
                    ranking[n] (store it as the pick's simulate_report): {video, keep, ok, kept: [{index, hook, type}],
                    ranking: [{index, hook, type, x, low, high, wins, why, check, kept}], keep_line, titles_dropped}
  analysis          {job} -> candidates.json summary: {job, video, recommended, clip_type, profile, clip_length,
                    has_heatmap, hook_plan, ad_breaks, candidates (transcript cut to 400 chars), scenarios, sheets}
  transcript        {job, pattern?, from_s?, to_s?, around_s?, window_s=30, limit=200} -> {job, offset, total,
                    truncated, sentences: [{s, e, text}], words?: [{w, s, e}]} (source seconds; words only for a
                    time window without pattern; pattern = case-insensitive regex)
  upload_manifests  {folders} -> {manifests: [{folder, entries | null, error | null}]}  (folders relative to ROOT)
  music_library     {} -> {tracks: [track]}   track: {id, file, kind, mood, role (hits: impact | riser | whoosh |
                    tension, clipper.music.role), tags, beat_at, length (seconds, None when unreadable), claimable,
                    approved, usable, local, source ("own" | "built-in"), title, exists}
  set_music         {id, approved} -> {track}   writes assets/music/local/library.yaml (gitignored), which
                    clipper.music.load_library merges over the shipped library
  add_sound         {path, name, kind, role?, mood?, claimable, approved} -> {track} | {problem: German}
                    an uploaded file (`path` must lie in data/tmp/uploads, where the API streamed it) becomes an own
                    sound: ffprobe must find audio of a format matching its extension (SOUND_FORMATS) and a sane
                    duration (SOUND_SECONDS by kind); it is copied to assets/music/local/<id><ext> under a new id made
                    from `name` (never an existing id or a role name), its length and loudness are measured, and the
                    entry is added to local/library.yaml as the renderer expects (a hit with its role, music with its
                    mood). The upload itself is left for the caller to delete.
  remove_sound      {id} -> {removed: id} | {problem: German}   only own sounds (an entry of local/library.yaml with
                    its own file): the entry and its file go; built-in sounds and their approvals stay
  resolve_creator   {ref, lookup=true} -> {url, name, found}   "@handle" / channel URL -> /videos URL (+ yt-dlp name)
  resolve_stream    {ref} -> {url, name}   "twitch:x" | "kick:x" | "youtube:@x" | URL -> live URL + recorder name

Other modules add ops by defining `automate/bridge_ext_<area>.py` that registers functions in `automate.bridge.OPS`
(`OPS[name] = fn(payload, cfg) -> dict`); they are imported when an op isn't found here.
"""
import fcntl
import importlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

import yaml

OPS = {}
LOCAL_HEADER = ("# Local music approvals and your own tracks (gitignored; written by the dashboard). Entries with a\n"
                "# shipped track's id change it; others add a track whose `file` is relative to this folder.\n")


def op(name):
    def register(fn):
        OPS[name] = fn
        return fn
    return register


def _picks(payload):
    picks = payload.get("picks")
    if not isinstance(picks, list) or not all(isinstance(p, dict) for p in picks):
        raise ValueError("picks must be a list of pick objects")
    return picks


RESERVED_NOTE = "andere Momente wählen: dieselben Szenen bringen beim zweiten Post keine Reichweite"


def _footage_of(pick, ref):
    from clipper import config
    from clipper import render
    jd = config.job_dir(ref)
    return render.footage(pick, jd, render.load_meta(jd))


def _reserved_hits(picks, job_ref, reserved):
    """{video n: [German lines]} for picks sharing footage with a reserved (not yet rendered) pick."""
    from clipper import render
    held = []
    for r in reserved or []:
        try:
            held.append((str(r.get("label") or r["job"]), _footage_of(r["pick"], r["job"])))
        except (KeyError, TypeError, ValueError, OSError, SystemExit):  # its job folder is gone: it holds nothing
            continue
    out = {}
    for n, pick in enumerate(picks, 1):
        ref = pick.get("job") or job_ref or ((pick.get("segments") or [{}])[0].get("job"))
        if not held or not ref:
            continue
        try:
            fp = _footage_of(pick, ref)
        except (KeyError, TypeError, ValueError, OSError, SystemExit):
            continue
        hits = []
        for label, other in held:
            shared = render.footage_overlap(fp, other)
            if shared > render.REUSE_TOLERANCE_S:
                hits.append(f"{label} nutzt schon {shared:.0f} s dieser Szenen (noch nicht gerendert)")
        if hits:
            out[n] = hits
    return out


@op("check")
def check(payload, cfg):
    from clipper import __main__ as cli
    from clipper import hooks
    picks, job = _picks(payload), payload.get("job") or None
    report = hooks.check_picks(picks, cfg, cli.job_words(job) if job else None, cli.job_words)
    for n, reused in cli.reused_footage(picks, job, cfg).items():
        report.setdefault(f"video {n}", {})["footage"] = reused
    for n, hits in _reserved_hits(picks, job, payload.get("reserved")).items():
        entry = report.setdefault(f"video {n}", {})
        entry["footage"] = hits + (entry.get("footage") or [RESERVED_NOTE])
    return {"report": report, "fails": hooks.has_fails(report)}


def _titles(ts, n_hooks, kept):
    """Titles for the kept hook indices (see the module doc), and the ones dropped."""
    if len(ts) == n_hooks:
        out = [ts[i] for i in kept]
    else:
        spare = [t for j, t in enumerate(ts) if j not in kept]
        out = []
        for i in kept:
            if i < len(ts):
                out.append(ts[i])
            elif spare:
                out.append(spare.pop(0))
    used = list(out)
    dropped = []
    for t in ts:
        if t in used:
            used.remove(t)
        else:
            dropped.append(t)
    return out, dropped


@op("rank")
def rank(payload, cfg):
    from clipper import hooks, library, simulate
    picks = _picks(payload)
    keep = payload.get("keep", "auto")
    keep = library.hooks_per_video() if keep in (None, "auto") else int(keep)
    kw = simulate.profile_kw(cfg)
    table = simulate.effect_table(kw.get("niche", "gta"))
    out_picks, ranking = [], []
    for n, pick in enumerate(picks, 1):
        hs = pick.get("hooks") or ([pick["hook"]] if pick.get("hook") else [])
        rows, kept, seen, used = [], [], set(), set()
        for r in simulate.simulate({**pick, "hooks": hs}, **kw) if hs else []:
            i = next(j for j, h in enumerate(hs) if h == r["hook"] and j not in used)
            used.add(i)
            structs = [k for k in r["why"] if k not in simulate.MODIFIERS]
            main = max(structs, key=lambda k: table[k][0]) if structs else None  # same rule as simulate.report
            take = not r["check"] and len(kept) < keep and main not in seen
            if take:
                kept.append((i, main))
                seen.add(main)
            rows.append({"index": i, "hook": r["hook"], "type": main, "x": round(r["x"], 3),
                         "low": round(r["low"], 3), "high": round(r["high"], 3), "wins": round(r["wins"], 3),
                         "why": r["why"], "check": r["check"], "kept": take})
        idx = [i for i, _ in kept]
        entry = {"video": n, "keep": keep, "ok": bool(kept), "kept": [{"index": i, "hook": hs[i], "type": t}
                                                                     for i, t in kept],
                 "ranking": rows, "titles_dropped": [],
                 "keep_line": f"keep (top {keep}, different types): "
                              + " | ".join(f"{hooks.plain(hs[i])} [{t}]" for i, t in kept)}
        new = dict(pick)
        if kept and pick.get("hooks"):
            new["hooks"] = [hs[i] for i in idx]
            ts = pick.get("titles")
            if isinstance(ts, list) and ts:
                new["titles"], entry["titles_dropped"] = _titles(ts, len(hs), idx)
        out_picks.append(new)
        ranking.append(entry)
    return {"picks": out_picks, "ranking": ranking}


@op("analysis")
def analysis(payload, cfg):
    from clipper import config
    job = config.job_dir(payload["job"])
    data = json.loads((job / "candidates.json").read_text(encoding="utf-8"))
    cands = [{**c, "transcript": (c.get("transcript") or "")[:400]} for c in data.get("candidates", [])]
    keys = ("video", "recommended", "clip_type", "profile", "clip_length", "has_heatmap", "hook_plan", "ad_breaks",
            "scenarios")
    return {"job": job.name, **{k: data.get(k) for k in keys}, "candidates": cands,
            "sheets": sorted(p.name for p in (job / "review").glob("*.jpg"))}


@op("transcript")
def transcript(payload, cfg):
    from clipper import config, transcribe
    job = config.job_dir(payload["job"])
    words = transcribe.drop_repeats(json.loads((job / "transcript.json").read_text(encoding="utf-8"))["words"])
    meta = json.loads((job / "job.json").read_text(encoding="utf-8")) if (job / "job.json").exists() else {}
    lo, hi = payload.get("from_s"), payload.get("to_s")
    if payload.get("around_s") is not None:
        w = float(payload.get("window_s") or 30)
        lo, hi = float(payload["around_s"]) - w, float(payload["around_s"]) + w
    lo = float("-inf") if lo is None else float(lo)
    hi = float("inf") if hi is None else float(hi)
    sents = [s for s in transcribe.group_sentences(words) if s["e"] > lo and s["s"] < hi]
    pattern = payload.get("pattern")
    if pattern:
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            raise ValueError(f"bad pattern {pattern!r}: {e}") from None
        sents = [s for s in sents if rx.search(s["text"])]
    limit = int(payload.get("limit") or 200)
    out = {"job": job.name, "offset": float(meta.get("offset") or 0), "total": len(sents),
           "truncated": len(sents) > limit,
           "sentences": [{"s": round(s["s"], 2), "e": round(s["e"], 2), "text": s["text"]} for s in sents[:limit]]}
    if not pattern and (lo > float("-inf") or hi < float("inf")):
        out["words"] = [{"w": x["w"], "s": round(x["s"], 2), "e": round(x["e"], 2)} for x in words
                        if x["e"] > lo and x["s"] < hi][:3000]
    return out


@op("upload_manifests")
def upload_manifests(payload, cfg):
    from clipper import config
    out = []
    for folder in payload.get("folders") or []:
        f = Path(folder) if Path(folder).is_absolute() else config.ROOT / folder
        try:
            out.append({"folder": folder, "entries": json.loads((f / "upload.json").read_text(encoding="utf-8")),
                        "error": None})
        except (OSError, ValueError) as e:
            out.append({"folder": folder, "entries": None, "error": str(e)})
    return {"manifests": out}


def _track_info(t, cfg):
    from clipper import music
    exists = t["path"].exists()
    length = music.duration(t) if exists else None
    return {"id": t.get("id"), "file": str(t.get("file")), "kind": t.get("kind"), "mood": t.get("mood"),
            "role": music.role(t), "tags": sorted(music.tags(t)) if t.get("kind") == "hit" else [],
            "beat_at": t.get("beat_at"), "length": round(float(length), 2) if length else None,
            "claimable": bool(t.get("claimable", True)), "approved": t.get("approved") is True,
            "usable": music.usable(t, cfg), "local": bool(t.get("local")),
            "source": "own" if t.get("local") else "built-in", "title": t.get("title"), "exists": exists}


@op("music_library")
def music_library(payload, cfg):
    from clipper import music
    return {"tracks": [_track_info(t, cfg) for t in music.load_library()]}


def _local_file():
    from clipper import music
    return music.MUSIC / "local" / "library.yaml"


@contextmanager
def _local_library():
    """local/library.yaml read under an exclusive lock (uploads, deletes and approvals can run at once) -> a dict the
    block changes in place; written back atomically when the block ends without an error."""
    f = _local_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f.parent / ".library.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}
        data = data if isinstance(data, dict) else {}
        data["tracks"] = [t for t in data.get("tracks") or [] if isinstance(t, dict)]
        yield data
        tmp = f.with_name(f".library.yaml.{os.getpid()}.tmp")
        tmp.write_text(LOCAL_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.replace(tmp, f)


@op("set_music")
def set_music(payload, cfg):
    from clipper import music
    tid, approved = payload.get("id"), payload.get("approved")
    if not isinstance(approved, bool):
        raise ValueError("approved must be true or false")
    if not any(t.get("id") == tid for t in music.load_library()):
        raise ValueError(f"no music track with id {tid!r}")
    with _local_library() as data:
        entry = next((t for t in data["tracks"] if t.get("id") == tid), None)
        if entry is None:
            data["tracks"].append({"id": tid, "approved": approved})
        else:
            entry["approved"] = approved
    return {"track": next(_track_info(t, cfg) for t in music.load_library() if t.get("id") == tid)}


# what ffprobe's format_name must contain for each extension an own sound may have
SOUND_FORMATS = {".wav": "wav", ".mp3": "mp3", ".m4a": "mp4", ".ogg": "ogg"}
SOUND_SECONDS = {"hit": (0.1, 30.0), "music": (3.0, 600.0)}
FORMAT_DE = {".wav": "WAV", ".mp3": "MP3", ".m4a": "M4A", ".ogg": "OGG"}


def _probe_audio(path):
    """(format names, duration, audio streams, other streams) of a file, or None when ffprobe can't read it."""
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration:stream="
                              "codec_type:stream_disposition=attached_pic", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout or "{}") if res.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not data or not data.get("format"):
        return None
    streams = data.get("streams") or []
    audio = sum(1 for x in streams if x.get("codec_type") == "audio")
    other = sum(1 for x in streams if x.get("codec_type") != "audio"
                and not (x.get("disposition") or {}).get("attached_pic"))  # an MP3's cover art is fine
    try:
        dur = float(data["format"].get("duration"))
    except (TypeError, ValueError):
        dur = None
    return set(str(data["format"].get("format_name") or "").split(",")), dur, audio, other


def _new_id(name, taken):
    from clipper import music
    stem = re.sub(r"[^a-z0-9]+", "-", Path(str(name)).stem.lower()).strip("-")[:40].strip("-") or "sound"
    sid, n = stem, 2
    while sid in taken or sid in music.ROLES:
        sid, n = f"{stem}-{n}", n + 1
    return sid


@op("add_sound")
def add_sound(payload, cfg):
    from automate import paths
    from clipper import music
    src = Path(str(payload.get("path") or ""))
    inbox = (paths.tmp_dir() / "uploads").resolve()
    if src.is_symlink() or not src.is_file() or src.resolve().parent != inbox:
        raise ValueError("the upload must be a file in data/tmp/uploads")
    name = str(payload.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = Path(name).suffix.lower()
    kind = payload.get("kind")
    if ext not in SOUND_FORMATS:
        return {"problem": "Nur WAV-, MP3-, M4A- oder OGG-Dateien."}
    if kind not in SOUND_SECONDS:
        return {"problem": "Bitte wählen: Sound-Effekt oder Musik."}
    role, mood = payload.get("role"), payload.get("mood")
    if kind == "hit" and role not in music.ROLES:
        return {"problem": f"Rolle muss eins von {', '.join(music.ROLES)} sein."}
    if kind == "music" and mood not in music.MOODS:
        return {"problem": f"Stimmung muss eins von {', '.join(music.MOODS)} sein."}
    probe = _probe_audio(src)
    if probe is None:
        return {"problem": "Die Datei ist keine lesbare Audiodatei."}
    formats, dur, audio, other = probe
    if SOUND_FORMATS[ext] not in formats:
        return {"problem": f"Die Datei ist keine echte {FORMAT_DE[ext]}-Datei (Inhalt: {', '.join(sorted(formats))})."}
    if not audio or other:
        return {"problem": "Die Datei muss genau Ton enthalten (kein Video)."}
    lo, hi = SOUND_SECONDS[kind]
    if dur is None or not lo <= dur <= hi:
        what = "Ein Sound-Effekt" if kind == "hit" else "Musik"
        return {"problem": f"{what} muss zwischen {lo:g} und {hi:g} Sekunden lang sein".replace(".", ",")
                           + (f" (diese Datei: {dur:.1f} s).".replace(".", ",", 1) if dur is not None else ".")}
    with _local_library() as data:
        taken = {str(t.get("id")) for t in music.load_library()} | {str(t.get("id")) for t in data["tracks"]}
        sid = _new_id(name, taken)
        folder = music.MUSIC / "local"
        dest = folder / f"{sid}{ext}"
        with open(dest, "xb") as out, open(src, "rb") as inp:  # x: never overwrite anything
            shutil.copyfileobj(inp, out)
        try:
            entry = {"id": sid, "file": dest.name, "kind": kind, "mood": mood if kind == "music" else "any",
                     "claimable": bool(payload.get("claimable")), "approved": bool(payload.get("approved")),
                     "title": re.sub(r"[\x00-\x1f]", "", Path(name).stem)[:80], "length": round(dur, 3)}
            if kind == "hit":
                entry["role"] = role
            placed = {**entry, "path": dest}
            level = music.peak_loudness(placed) if kind == "hit" else music.track_loudness(placed)
            if level is not None:
                entry["peak_lufs" if kind == "hit" else "lufs"] = round(level, 1)
            data["tracks"].append(entry)
        except BaseException:  # no file without its entry
            dest.unlink(missing_ok=True)
            raise
    return {"track": next(_track_info(t, cfg) for t in music.load_library() if t.get("id") == sid)}


@op("remove_sound")
def remove_sound(payload, cfg):
    from clipper import music
    tid = payload.get("id")
    folder = (music.MUSIC / "local").resolve()
    with _local_library() as data:
        entry = next((t for t in data["tracks"] if t.get("id") == tid and t.get("file")), None)
        if entry is None:
            shipped = any(t.get("id") == tid for t in music.load_library(music.MUSIC))
            return {"problem": "Mitgelieferte Sounds lassen sich nicht löschen – nur sperren." if shipped
                    else "Diesen Sound gibt es nicht."}
        data["tracks"].remove(entry)
        f = folder / str(entry["file"])
        if f.parent.resolve() == folder and (f.is_file() or f.is_symlink()):
            f.unlink()
    return {"removed": tid}


@op("resolve_creator")
def resolve_creator(payload, cfg):
    from clipper import library
    url = library.channel_url(str(payload["ref"]))
    name = library.lookup_channel(url) if payload.get("lookup", True) else None
    return {"url": url, "name": name, "found": bool(name)}


@op("resolve_stream")
def resolve_stream(payload, cfg):
    from clipper import live
    ref = str(payload["ref"]).strip()
    try:
        url = live.stream_url(ref)
    except KeyError:
        raise ValueError(f"unknown stream ref {ref!r} (twitch:NAME, kick:NAME, youtube:@NAME or a URL)") from None
    return {"url": url, "name": live.stream_name(url)}


def _load_extensions():
    here = Path(__file__).resolve().parent
    for m in pkgutil.iter_modules([str(here)]):
        if m.name.startswith("bridge_ext_"):
            importlib.import_module(f"automate.{m.name}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")  # the result channel; fd 1 itself now goes to stderr
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        if len(argv) != 1:
            raise ValueError("usage: python -m automate.bridge <op> < payload.json")
        if argv[0] not in OPS:
            _load_extensions()
        fn = OPS.get(argv[0])
        if fn is None:
            raise ValueError(f"unknown op {argv[0]!r} (have: {', '.join(sorted(OPS))})")
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("the payload must be a JSON object")
        from clipper import config
        config.load_env()
        result = fn(payload, config.load())
        out.write(json.dumps(result, ensure_ascii=False, default=str))
        code = 0
    except BaseException as e:  # SystemExit from clipper code is an error here too
        traceback.print_exc()
        msg = str(e) or e.__class__.__name__
        out.write(json.dumps({"error": msg}, ensure_ascii=False))
        code = 1
    out.flush()
    return code


if __name__ == "__main__":
    from automate import bridge  # the canonical module: extensions register into automate.bridge.OPS, not __main__'s
    sys.exit(bridge.main())
