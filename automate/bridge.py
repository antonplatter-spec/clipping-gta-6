"""`python -m automate.bridge <op>`: one JSON object on stdin -> one JSON object on stdout, exit 0; on failure
`{"error": "..."}` on stdout (traceback on stderr), exit 1. Runs as a subprocess with runtime.clipper_env(profile)
(runtime.run_bridge), so it may import clipper.* (the server process never does). Like the CLI it calls
clipper.config.load_env() and config.load() first; the op gets the payload and that cfg.

Anything an op prints (clipper's progress lines, a child process) goes to stderr: stdout carries only the result.

Ops (payload -> result):
  check             {picks, job?} -> {report, fails}      exactly `clipper check picks.json --job <job>`:
                    hooks.check_picks + the reused-footage fails; `job` is a job id or path (omit: no transcript notes)
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
  music_library     {} -> {tracks: [{id, file, kind, mood, claimable, approved, usable, local, exists}]}
  set_music         {id, approved} -> {track}   writes assets/music/local/library.yaml (gitignored), which
                    clipper.music.load_library merges over the shipped library
  resolve_creator   {ref, lookup=true} -> {url, name, found}   "@handle" / channel URL -> /videos URL (+ yt-dlp name)
  resolve_stream    {ref} -> {url, name}   "twitch:x" | "kick:x" | "youtube:@x" | URL -> live URL + recorder name

Other modules add ops by defining `automate/bridge_ext_<area>.py` that registers functions in `automate.bridge.OPS`
(`OPS[name] = fn(payload, cfg) -> dict`); they are imported when an op isn't found here.
"""
import importlib
import json
import os
import pkgutil
import re
import sys
import traceback
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


@op("check")
def check(payload, cfg):
    from clipper import __main__ as cli
    from clipper import hooks
    picks, job = _picks(payload), payload.get("job") or None
    report = hooks.check_picks(picks, cfg, cli.job_words(job) if job else None, cli.job_words)
    for n, reused in cli.reused_footage(picks, job, cfg).items():
        report.setdefault(f"video {n}", {})["footage"] = reused
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
    return {"id": t.get("id"), "file": str(t.get("file")), "kind": t.get("kind"), "mood": t.get("mood"),
            "claimable": bool(t.get("claimable", True)), "approved": t.get("approved") is True,
            "usable": music.usable(t, cfg), "local": bool(t.get("local")), "exists": t["path"].exists()}


@op("music_library")
def music_library(payload, cfg):
    from clipper import music
    return {"tracks": [_track_info(t, cfg) for t in music.load_library()]}


@op("set_music")
def set_music(payload, cfg):
    from clipper import music
    tid, approved = payload.get("id"), payload.get("approved")
    if not isinstance(approved, bool):
        raise ValueError("approved must be true or false")
    if not any(t.get("id") == tid for t in music.load_library()):
        raise ValueError(f"no music track with id {tid!r}")
    f = music.MUSIC / "local" / "library.yaml"
    data = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}
    tracks = [t for t in data.get("tracks") or [] if isinstance(t, dict)]
    entry = next((t for t in tracks if t.get("id") == tid), None)
    if entry is None:
        tracks.append({"id": tid, "approved": approved})
    else:
        entry["approved"] = approved
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".library.yaml.{os.getpid()}.tmp")
    tmp.write_text(LOCAL_HEADER + yaml.safe_dump({**data, "tracks": tracks}, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, f)
    return {"track": next(_track_info(t, cfg) for t in music.load_library() if t.get("id") == tid)}


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
