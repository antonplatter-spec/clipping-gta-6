"""`clipper preview`: one composited still of a pick (the video frame, the hook banner and the caption on screen at
that moment) without rendering the clip, so the layout can be judged before approving it.

It runs the render's own code: render.clip_plan (the same frames, the same placement decision, the same look),
render.video_filter for the frame, captions.render_state / render_hook for the overlays, and render.no_hook_arm for
whether the clip posts without a banner. Captions come from the source transcript (the render may use a diarized
one, so a second speaker can show differently); zoom and flash effects aren't drawn.

    preview(job, picks, n, at=None, hook=1, out=None, cfg=None) -> {png, pick, at, hook, hook_visible,
        no_hook_arm, caption, placement, seconds}
"""
import json
import subprocess
import tempfile
import time
from pathlib import Path

from . import captions, config, placement, render, transcribe
from .hooks import clip_words, pick_duration


def source_words(job_dir):
    f = Path(job_dir) / "transcript.json"
    return transcribe.drop_repeats(json.loads(f.read_text(encoding="utf-8"))["words"]) if f.exists() else []


def clip_caption_words(pick, job, cfg):
    """The clip's caption words in clip seconds, as the render builds them from the source transcript: profile then
    pick caption_fixes, the action labels, swears masked."""
    words = clip_words(pick, source_words(job), lambda ref: source_words(config.job_dir(ref))) or []
    words = captions.apply_fixes(words, {**(cfg.get("caption_fixes") or {}), **(pick.get("caption_fixes") or {})})
    if pick.get("labels"):  # like prepare_clip: a story cut's labels are already in clip seconds
        words = render.action_labels({**pick, "start": 0.0, "end": pick_duration(pick)} if pick.get("segments")
                                     else pick, words)
    return captions.censor_words(words) if cfg.get("censor_captions", True) else words


def at_source(pick, job, meta, at):
    """(source file, source seconds, layout) of clip second `at`."""
    srcs = render.pick_sources(pick, job, meta)
    lays = [s.get("layout") or pick.get("layout") or {"type": "fit"} for s in pick["segments"]] \
        if pick.get("segments") else [pick.get("layout") or {"type": "fit"}]
    t = at
    for i, (src, lay) in enumerate(zip(srcs, lays)):
        d = src["end"] - src["start"]
        if t < d or i == len(srcs) - 1:
            return Path(src["job"]) / "source.mp4", src["start"] + max(0.0, min(t, d - 0.04)), lay
        t -= d


def preview(job, picks, n, at=None, hook=1, out=None, cfg=None):
    t0 = time.time()
    cfg = cfg or {}
    if not 1 <= n <= len(picks):
        raise SystemExit(f"[preview] pick {n} doesn't exist ({len(picks)} picks)")
    pick = picks[n - 1]
    meta = render.load_meta(job)
    every = pick.get("hooks") or [pick.get("hook", "")]
    arm = render.no_hook_arm(pick, job, meta, cfg.get("no_hook_share", 0.33))
    plan = render.clip_plan(pick, job, meta, cfg, [] if arm else every)
    dur = pick_duration(pick)
    words = clip_caption_words(pick, job, cfg)
    look = captions.LOOKS[plan["captions"]["look"]]
    states = captions.caption_states(words, 0.0, dur, look["words"])
    text = "" if arm else (every[min(max(hook, 1), len(every)) - 1] or "")
    secs = plan["hook"]["seconds"]
    if at is None:  # the first caption while the banner still shows, else the first caption
        shown = [a for a, b, st in states if st]
        early = [a for a in shown if secs is None or a < secs - placement.HOOK_FADE_S]
        at = (early or shown or [min(1.0, dur / 2)])[0] + 0.05
    at = min(max(0.0, float(at)), max(0.0, dur - 0.05))
    state = next((st for a, b, st in states if a <= at < b), ())
    out = Path(out) if out else Path(job) / "review" / f"preview_{n}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    alpha = 1.0
    if secs is not None:
        alpha = max(0.0, min(1.0, (secs - at) / placement.HOOK_FADE_S))
    with tempfile.TemporaryDirectory() as tmp:
        cap, hk = Path(tmp) / "cap.png", Path(tmp) / "hook.png"
        multi = len({w.get("spk", 0) for w in words if not w.get("label")}) > 1
        captions.render_state(state, cap, plan["captions"], multi, {w["w"] for w in words if w.get("label")})
        banner = captions.render_hook(text, hk, pick.get("hook_style", "box"), placement.layout_name(pick),
                                      plan["hook"])
        src, t, lay = at_source(pick, job, meta, at)
        vf = (render.video_filter(lay, render.source_size(src)) + ";[v1][1:v]overlay=0:0[v2];"
              f"[2:v]format=rgba,colorchannelmixer=aa={alpha:.3f}[hk];[v2][hk]overlay=0:0[v]")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(src), "-i", str(cap),
                        "-i", str(hk), "-filter_complex", vf, "-map", "[v]", "-frames:v", "1", str(out)], check=True)
    return {"png": str(out), "pick": n, "at": round(at, 2), "hook": text or None,
            "hook_visible": bool(banner) and alpha > 0, "no_hook_arm": arm,
            "caption": " ".join(w for _, ws, _ in state for w in ws), "placement": placement.summary(plan),
            "seconds": round(time.time() - t0, 2)}
