"""The six tools Claude gets in the select loop, executed locally by the worker, and the validation of its picks.

    TOOLS                                   the tool definitions (constant and in a fixed order: they open the cached
                                            prompt prefix, so they never change between requests or jobs)
    JobContext.load(job_row, ...)           everything a tool needs about the job (dir, time base, prefs, limits)
    run_tool(ctx, name, input) -> ToolResult     grep_transcript, view_frame, find_line, check, simulate
    submit(ctx, input, round_no=, max_rounds=) -> Submission | ToolResult   submit_picks: validation + `clipper check`
                                            (ToolError when the check itself can't run: the caller decides)
    validate_picks(ctx, picks, draft=False) -> (clean, reasons, triggers, errors)
    resolve_name(name) -> declared tool name | None   (accepts "Submit-Picks"-style near misses)
    sheet_image(path) / image_block(path, max_side)   contact sheets and frames as downscaled base64 image blocks
    candidates(ctx) / music_moods(ctx)       facts for the first message

Validation beyond `clipper check`: types and enums of clip.md step 7, start < end, the 60 s cap, hooks paired with
titles, `why` + a named `click_trigger` per pick, max_picks, the voiceover preference, no footage shared between the
picks of one submission (> 3 s), and no hook or title opening with the same three words as another video's.

Tools run as subprocesses (`runtime.run_clipper` / `runtime.run_bridge`), never by importing clipper here. Results
are compact and bounded (match caps, window caps, truncation notes, downscaled images, a frame budget per job) so a
long stream can't blow up the context or the bill. A failure the model can act on is a ToolResult with is_error.
`ToolCanceled` is raised when the job's cancel event stops a tool's subprocess.
"""
import base64
import io
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, runtime, store

CLIP_TYPES = ("reaction", "comedy", "news", "gameplay", "showcase", "scenario")
HOOK_STYLES = ("bold", "box", "label", "none")
CAPTION_STYLES = ("boxed", "bold", "comic")
FX_TYPES = ("flash", "zoom", "hit")
MOODS = ("suspense", "sneaky", "epic", "sad", "funny")
VOICE_PREFS = ("off", "all", "auto")
MAX_CLIP_S = 60.0  # clipper's hooks.MAX_CLIP_S
MIN_SEGMENT_S = 0.5  # clipper's hooks.check_segments
REUSE_TOLERANCE_S = 3.0  # clipper's render.REUSE_TOLERANCE_S, applied between the picks of one submission
MAX_HOOKS = 12
MAX_VOICEOVER_WORDS = 10
OPENER_WORDS = 3
# The click triggers of research "Outlier study"; a pick's click_trigger has to name one (by any of these stems).
TRIGGERS = ("curiosity gap", "viewer as judge", "play-along / streak", "suspense", "irony against the persona",
            "shock confession", "status / celebrity", "absurd one-line premise")
TRIGGER_STEMS = ("curiosity", "judge", "play-along", "play along", "streak", "suspense", "irony", "confession",
                 "status", "celebrity", "premise")

GREP_WINDOW_S = 30.0  # around_s: +-30 s
GREP_MAX_SPAN_S = 180.0  # longest window with word timings
GREP_MAX_MATCHES = 40
FIND_MAX_HITS = 25
MAX_RESULT_CHARS = 12000
MAX_VIEW_CALLS = 24  # view_frame calls per job (each adds ~1-1.5k image tokens to every later request)
FRAME_SIDE = 1280
SHEET_SIDE = 1568
JPEG_QUALITY = 82
TIMEOUTS = {"transcript": 120, "frame": 120, "find": 120, "check": 300, "rank": 600, "ffprobe": 30, "music": 60}

PICK_KEYS = ("start", "end", "segments", "clip_type", "layout", "hook_style", "caption_style", "hooks", "titles",
             "description", "hashtags", "voiceover", "labels", "caption_fixes", "fx", "music", "why", "click_trigger")
NOTE_KEYS = ("why", "click_trigger")  # ours, not clipper's: moved out of the pick into Selection.reasons/click_triggers


class ToolCanceled(Exception):
    """The job's cancel event stopped a tool."""


class ToolError(Exception):
    """A failure the model can act on: sent back as an is_error tool result."""


@dataclass
class ToolResult:
    content: object  # str or a list of content blocks (text/image)
    is_error: bool = False


@dataclass
class Submission:
    picks: list
    reasons: list
    click_triggers: list
    no_clip_reason: object
    check_report: dict


# ---------------------------------------------------------------- tool definitions

def _obj(props, required=(), **extra):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False, **extra}


NUM = {"type": "number"}
STR = {"type": "string"}
STRS = {"type": "array", "items": STR}
LAYOUT = _obj({"type": {"type": "string", "enum": ["fit", "split"]},
               "face": {"type": "array", "items": NUM,
                        "description": "split: the facecam box [x, y, w, h] as fractions of the frame"},
               "game_x": {"type": "number", "description": "split: where the game crop sits sideways, 0-1 (0.5)"}},
              ["type"])
SEGMENT = _obj({"start": NUM, "end": NUM,
                "job": {"type": "string", "description": "another job's id (multi-streamer edit); omit for this job"},
                "layout": LAYOUT}, ["start", "end"])
PICK_PROPS = {
    "start": {"type": "number", "description": "single clip: source seconds of this job"},
    "end": NUM,
    "segments": {"type": "array", "items": SEGMENT, "description": "story cut / multi-streamer edit, in play order "
                                                                   "(instead of start/end)"},
    "clip_type": {"type": "string", "enum": list(CLIP_TYPES)},
    "layout": LAYOUT,
    "hook_style": {"type": "string", "enum": list(HOOK_STYLES)},
    "caption_style": {"type": "string", "enum": list(CAPTION_STYLES)},
    "hooks": {**STRS, "description": "variants, each a different structure, best first; one *keyword*"},
    "titles": {**STRS, "description": "one per hook, same order and structure type"},
    "description": STR,
    "hashtags": STRS,
    "voiceover": {"type": "string", "description": "only when chosen: one line, 10 words or fewer"},
    "labels": {"type": "array", "items": _obj({"s": NUM, "e": NUM, "text": STR}, ["s", "e", "text"])},
    "caption_fixes": {"type": "object", "additionalProperties": STR,
                      "description": "misheard word -> correct spelling (whole words, any case)"},
    "fx": {"type": "array", "items": _obj({"t": NUM, "type": {"type": "string", "enum": list(FX_TYPES)},
                                          "color": STR, "sound": STR}, ["t", "type"])},
    "music": {"type": "array", "items": _obj({"s": NUM, "e": NUM, "mood": {"type": "string", "enum": list(MOODS)}},
                                             ["s", "e", "mood"])},
    "why": {"type": "string", "description": "German: why it works and what you decided"},
    "click_trigger": {"type": "string", "description": "trigger name, a colon, then in German what it is here"},
}
PICK = _obj(PICK_PROPS, ["clip_type", "layout", "hook_style", "caption_style", "hooks", "titles", "description",
                         "hashtags", "why", "click_trigger"])
DRAFT_PICK = _obj(PICK_PROPS, ["clip_type", "layout", "hook_style", "caption_style", "hooks", "titles", "description",
                               "hashtags"])
HOOK_SET = _obj({"hooks": STRS, "titles": STRS, "hashtags": STRS, "clip_type": {"type": "string",
                                                                                "enum": list(CLIP_TYPES)}},
                ["hooks"])

# Strict schemas only on the three small read-only tools: strict needs additionalProperties false on every object,
# which a pick's free-form caption_fixes map can't meet, and the pick schema is large with many optional parts. The
# pick-carrying tools are validated in code (validate_picks) and by `clipper check` instead.
TOOLS = [
    {"name": "grep_transcript", "strict": True,
     "description": (
         "Read the transcript of this job (or another recent job) with source-second timings. Use it to place cuts "
         "precisely and to read what is said. With around_s (+-30 s) or from_s/to_s (a window up to 180 s) you get "
         "the sentences plus the start time of every word, so you can start a clip on the first meaningful word and "
         "end it just after the payoff. With pattern (a case-insensitive regex) you get the matching sentences of "
         "the whole transcript (up to 40), e.g. to find a setup line, a name, or whether the stream shows the "
         "outcome later; combine pattern with from_s/to_s to search one stretch. Speech recognition mishears "
         "names: fix those in the pick's caption_fixes."),
     "input_schema": _obj({"pattern": {"type": "string", "description": "case-insensitive regex"},
                           "around_s": {"type": "number", "description": "centre of a +-30 s window"},
                           "from_s": NUM, "to_s": NUM,
                           "job": {"type": "string", "description": "another job's id; omit for this job"}})},
    {"name": "view_frame", "strict": True,
     "description": (
         "Look at the source video. Use it where the words can't tell you something: what happens at the payoff, "
         "whether a facecam is on screen, how a moment looks. With grid=true the frame carries a labelled 10 % grid: "
         "use it at a moment where the facecam is clearly visible to read layout.face as fractions [x, y, w, h]. "
         "With sheet (e.g. \"cand_07\" or \"scen_02\") you get a contact sheet that wasn't attached to the first "
         "message (six frames across that window, left to right, top to bottom); seconds isn't needed then. job "
         "looks at another recent job (a multi-streamer segment's facecam). Each call adds an image to the "
         "conversation and there is a limit per job, so look on purpose."),
     "input_schema": _obj({"seconds": {"type": "number", "description": "source seconds"},
                           "grid": {"type": "boolean", "description": "overlay the 10 % grid (for layout.face)"},
                           "sheet": {"type": "string", "description": "a contact sheet, e.g. cand_07 or scen_02"},
                           "job": {"type": "string", "description": "another job's id; omit for this job"}})},
    {"name": "find_line", "strict": True,
     "description": (
         "Search every job recorded or fetched in the last `hours` (default 6; every stream and creator) for "
         "sentences matching a case-insensitive regex. Use it for multi-streamer edits: GTA cutscene dialogue and "
         "shared events sound the same in every stream, so one distinctive line (several words, unusual names) "
         "finds the same moment in each stream. Each hit gives the job, channel, wall-clock time for live "
         "recordings, the sentence's seconds in that job and its text. Use a hit from another job as a segment "
         "with \"job\": \"<job>\"; hits marked footage deleted can't be used."),
     "input_schema": _obj({"pattern": {"type": "string", "description": "case-insensitive regex"},
                           "hours": {"type": "number", "description": "how far back (default 6)"}}, ["pattern"])},
    {"name": "check",
     "description": (
         "Dry-run the quality gate on a draft without submitting: the same format validation and `clipper check` "
         "report submit_picks uses (fails and notes per video, footage an exported clip already uses). why and "
         "click_trigger may be left out. Use it when you want feedback on a draft mid-way; submit_picks runs the "
         "same checks, so don't call check right before submitting the same picks."),
     "input_schema": _obj({"picks": {"type": "array", "items": DRAFT_PICK}}, ["picks"])},
    {"name": "simulate",
     "description": (
         "Rank hook variants with the swipe simulator (it resamples the measured hook effects of 3,481 gaming "
         "Shorts; about 10 seconds). Pass each video's hooks with their paired titles, plus its hashtags (a "
         "streamer's name tag counts) and clip_type; the rest of the pick isn't needed. Returns per video the hooks "
         "ranked by simulated views vs the channel norm with a 90 % range, the structures detected, check failures, "
         "and the keep line: the owner's number of variants, each a different structure, best first, with their "
         "titles. Draft every structure that fits (at least three) before you simulate, then submit the kept hooks "
         "in that order."),
     "input_schema": _obj({"picks": {"type": "array", "items": HOOK_SET}}, ["picks"])},
    {"name": "submit_picks",
     "description": (
         "Submit the final picks for this job. It is the only way your work is saved, and the job ends when it is "
         "accepted. The worker validates the format (see the instructions), rejects picks that share footage with "
         "each other or open with the same words as another video, and runs `clipper check` against this job; any "
         "fail comes back as an error with the report, and you submit the complete corrected list again. When "
         "nothing in this source is worth posting, submit an empty list with no_clip_reason (German)."),
     "input_schema": _obj({"picks": {"type": "array", "items": PICK},
                           "no_clip_reason": {"type": "string",
                                              "description": "German; only with an empty picks list"}},
                          ["picks"])},
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)


# ---------------------------------------------------------------- job context

@dataclass
class JobContext:
    job: dict
    profile: str
    clipper_job: str
    dir: Path
    meta: dict
    duration: object  # float | None: seconds of this job's source.mp4
    max_picks: int
    avoid_openers: list
    voice: str
    hooks_per_video: object  # int | "auto"
    cancel: object = None
    log: object = None
    views: int = 0
    scratch: Path = None
    sheets_attached: set = field(default_factory=set)
    reserved: list = field(default_factory=list)  # footage other pending picks hold (see run_check)

    @classmethod
    def load(cls, job, *, max_picks, avoid_openers=(), cancel=None, log=None, reserved=()):
        """Raises LookupError (German) when the job has no analysis to select from."""
        cj = job.get("clipper_job")
        if not cj:
            raise LookupError(f"Job {job.get('id')} hat noch keinen Clipper-Job (nicht geladen/analysiert).")
        d = job_dir(cj)
        if not (d / "candidates.md").exists() or not (d / "transcript.json").exists():
            raise LookupError(f"Für {cj} fehlt die Analyse (candidates.md / transcript.json).")
        meta = _read_json(d / "job.json") or {}
        voice = store.get_setting("prefs.voice")
        ctx = cls(job=job, profile=job["profile"], clipper_job=cj, dir=d, meta=meta, duration=None,
                  max_picks=int(max_picks), avoid_openers=[str(x) for x in avoid_openers or () if str(x).strip()],
                  voice=voice if voice in VOICE_PREFS else "auto",
                  hooks_per_video=store.get_setting("prefs.hooks_per_video"), cancel=cancel, log=log,
                  reserved=[r for r in reserved or () if isinstance(r, dict)])
        ctx.scratch = paths.tmp_dir() / "agent" / f"job-{job.get('id')}"
        ctx.duration = source_duration(ctx)
        return ctx

    @property
    def offset(self):
        return float(self.meta.get("offset") or 0)

    def say(self, line):
        if self.log:
            try:
                self.log(line)
            except Exception:  # a broken progress callback must not stop the job
                pass


def job_dir(ref):
    """A job folder by id (under paths.jobs_dir()); never outside it."""
    if not re.fullmatch(r"[\w.\-]+", str(ref or "")) or str(ref).strip(".") == "":
        raise ValueError(f"bad job id {ref!r}")
    return paths.jobs_dir() / str(ref)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def source_duration(ctx):
    """Seconds of the job's source.mp4 (ffprobe), else the transcript's last word; None when neither is known.
    job.json's duration is the whole original video, which is wrong for a section download."""
    src = ctx.dir / "source.mp4"
    if src.exists():
        try:
            res = runtime.run_clipper([], ctx.profile, overlay=False, timeout=TIMEOUTS["ffprobe"], cancel=ctx.cancel,
                                      argv=["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                            "csv=p=0", str(src)])
            if res.returncode == 0 and res.stdout.strip():
                return round(float(res.stdout.strip().splitlines()[0]), 2)
        except (OSError, ValueError):
            pass
    words = (_read_json(ctx.dir / "transcript.json") or {}).get("words") or []
    return round(float(words[-1]["e"]), 2) if words else None


# ---------------------------------------------------------------- helpers

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clip(text, limit=MAX_RESULT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} characters; narrow the request]"


def _bridge(ctx, op, payload, timeout_key):
    try:
        return runtime.run_bridge(op, payload, ctx.profile, timeout=TIMEOUTS[timeout_key], cancel=ctx.cancel)
    except runtime.BridgeError as e:
        if e.canceled:
            raise ToolCanceled() from None
        raise ToolError(f"{op} failed: {e.message}") from None


def _clipper(ctx, args, timeout_key, argv=None):
    try:
        res = runtime.run_clipper(args, ctx.profile, timeout=TIMEOUTS[timeout_key], cancel=ctx.cancel, argv=argv,
                                  overlay=None if argv is None else False)
    except OSError as e:  # ffmpeg missing on this machine
        raise ToolError(f"{(argv or ['clipper'])[0]} couldn't run: {e}") from None
    if res.canceled:
        raise ToolCanceled()
    if res.timed_out:
        raise ToolError(f"timed out after {TIMEOUTS[timeout_key]} s")
    return res


def _other_job(ctx, ref):
    """This job's folder, or another job's by id (it must exist and have a transcript)."""
    if ref in (None, "", ctx.clipper_job):
        return ctx.dir, ctx.clipper_job
    try:
        d = job_dir(ref)
    except ValueError:
        raise ToolError(f"unknown job {ref!r}: use a job id exactly as find_line printed it") from None
    if not (d / "transcript.json").exists():
        raise ToolError(f"job {ref!r} doesn't exist or has no transcript: use a job id as find_line printed it")
    return d, str(ref)


def image_block(data, max_side, quality=JPEG_QUALITY):
    """JPEG bytes (or a path) -> an image content block, downscaled to `max_side` and re-encoded when larger."""
    from PIL import Image
    raw = Path(data).read_bytes() if isinstance(data, (str, Path)) else data
    img = Image.open(io.BytesIO(raw))
    w, h = img.size
    if max(w, h) > max_side or img.format != "JPEG" or len(raw) > 1_500_000:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        raw, (w, h) = buf.getvalue(), img.size
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(raw).decode("ascii")}}, (w, h)


def sheet_image(path):
    return image_block(path, SHEET_SIDE)


# ---------------------------------------------------------------- read-only tools

def _fmt_s(x):
    return f"{float(x):.2f}".rstrip("0").rstrip(".") if float(x) != int(float(x)) else f"{int(float(x))}"


def grep_transcript(ctx, inp):
    d, name = _other_job(ctx, inp.get("job"))
    pattern = (inp.get("pattern") or "").strip() or None
    around, lo, hi = inp.get("around_s"), inp.get("from_s"), inp.get("to_s")
    for k, v in (("around_s", around), ("from_s", lo), ("to_s", hi)):
        if v is not None and not _num(v):
            raise ToolError(f"{k} must be a number of seconds")
    if pattern is None and around is None and lo is None and hi is None:
        raise ToolError("give a pattern, around_s, or from_s/to_s")
    if pattern:
        try:
            re.compile(pattern, re.I)
        except re.error as e:
            raise ToolError(f"bad regex {pattern!r}: {e}") from None
    note = ""
    if around is not None:
        lo, hi = max(0.0, around - GREP_WINDOW_S), around + GREP_WINDOW_S
    elif lo is not None or hi is not None:
        lo = max(0.0, float(lo or 0.0))
        hi = float(hi) if hi is not None else lo + GREP_MAX_SPAN_S
        if hi <= lo:
            raise ToolError("to_s must be after from_s")
        if not pattern and hi - lo > GREP_MAX_SPAN_S:
            note = f"(window cut to {GREP_MAX_SPAN_S:.0f} s: ask for the rest separately)\n"
            hi = lo + GREP_MAX_SPAN_S
    payload = {"job": str(d), "limit": GREP_MAX_MATCHES if pattern else 400}
    if pattern:
        payload["pattern"] = pattern
    if lo is not None:
        payload.update(from_s=lo, to_s=hi)
    out = _bridge(ctx, "transcript", payload, "transcript")
    sents, words = out.get("sentences") or [], out.get("words") or []
    where = f"{_fmt_s(lo)}-{_fmt_s(hi)} s" if lo is not None else "whole transcript"
    head = f"job {name} · {where}" + (f" · pattern /{pattern}/" if pattern else "") + \
        f" · {len(sents)} of {out.get('total', len(sents))} sentences (source seconds)\n"
    if not sents:
        return ToolResult(head + note + "no speech here" if not pattern else head + note + "no match")
    lines = []
    for s in sents:
        lines.append(f"[{s['s']:.2f}-{s['e']:.2f}] {s['text']}")
        mine = [w for w in words if s["s"] - 0.01 <= w["s"] <= s["e"] + 0.01] if words else []
        if mine:
            lines.append("  " + " · ".join(f"{w['s']:.1f} {w['w']}" for w in mine))
    more = ""
    if out.get("truncated"):
        more = f"\n[{out['total'] - len(sents)} more matches: narrow the pattern or add from_s/to_s]"
    return ToolResult(_clip(head + note + "\n".join(lines) + more))


def view_frame(ctx, inp):
    if ctx.views >= MAX_VIEW_CALLS:
        raise ToolError(f"frame limit reached ({MAX_VIEW_CALLS} per job): decide with what you have")
    d, name = _other_job(ctx, inp.get("job")) if inp.get("job") else (ctx.dir, ctx.clipper_job)
    sheet = (inp.get("sheet") or "").strip()
    if sheet:
        stem = re.sub(r"\.jpg$", "", sheet.strip().lower())
        if not re.fullmatch(r"(cand|scen)_\d{2}", stem):
            raise ToolError("sheet must look like cand_07 or scen_02 (as candidates.md names it)")
        f = d / "review" / f"{stem}.jpg"
        if not f.exists():
            raise ToolError(f"job {name} has no contact sheet {stem}")
        block, (w, h) = sheet_image(f)
        ctx.views += 1
        return ToolResult([{"type": "text", "text": f"Contact sheet {stem} of job {name} ({w}x{h}): six frames "
                                                     "across the window, left to right, top to bottom."}, block])
    sec = inp.get("seconds")
    if not _num(sec) or sec < 0:
        raise ToolError("seconds must be a number of source seconds (or pass sheet)")
    if d == ctx.dir and ctx.duration and sec > ctx.duration:
        raise ToolError(f"{sec} s is past the end of this job's video ({ctx.duration:.1f} s)")
    if not (d / "source.mp4").exists():
        raise ToolError(f"the video of job {name} is gone (deleted by cleanup): use its contact sheets "
                        "(sheet=...) instead")
    if inp.get("grid"):
        res = _clipper(ctx, ["frame", "--", str(d), f"{float(sec):.2f}"], "frame")
        last = (res.stdout.strip().splitlines() or [""])[-1].strip()
        if res.returncode != 0 or not last or not Path(last).exists():
            raise ToolError(f"no frame at {sec} s: " + (res.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        block, (w, h) = image_block(last, FRAME_SIDE)
        if Path(last).resolve().parent == (d / "review").resolve():  # not a contact sheet: keep review/ to those
            Path(last).unlink(missing_ok=True)
        text = (f"Frame at {_fmt_s(sec)} s of job {name} ({w}x{h}) with a 10 % grid (lines labelled .1-.9 from the "
                "left and the top). For a split layout give the facecam box as fractions [x, y, w, h], e.g. "
                "[0.70, 0.05, 0.28, 0.32].")
    else:
        ctx.scratch.mkdir(parents=True, exist_ok=True)
        out = ctx.scratch / f"frame_{name}_{float(sec):.2f}.jpg"
        res = _clipper(ctx, [], "frame", argv=["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{float(sec):.3f}",
                                               "-i", str(d / "source.mp4"), "-frames:v", "1", "-vf",
                                               f"scale={FRAME_SIDE}:-2", "-q:v", "3", str(out)])
        if res.returncode != 0 or not out.exists():
            raise ToolError(f"no frame at {sec} s: " + (res.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        block, (w, h) = image_block(out, FRAME_SIDE)
        out.unlink(missing_ok=True)
        text = f"Frame at {_fmt_s(sec)} s of job {name} ({w}x{h})."
    ctx.views += 1
    return ToolResult([{"type": "text", "text": text}, block])


def find_line(ctx, inp):
    pattern = (inp.get("pattern") or "").strip()
    if not pattern:
        raise ToolError("pattern is required")
    try:
        re.compile(pattern, re.I)
    except re.error as e:
        raise ToolError(f"bad regex {pattern!r}: {e}") from None
    hours = inp.get("hours")
    hours = 6.0 if hours is None else hours
    if not _num(hours) or hours <= 0:
        raise ToolError("hours must be a positive number")
    hours = min(float(hours), 72.0)
    res = _clipper(ctx, ["find", "--hours", f"{hours:g}", "--", pattern], "find")  # a pattern may start with "-"
    if res.returncode != 0:
        raise ToolError("find failed: " + (res.stderr.strip().splitlines() or ["?"])[-1])
    hits = [x.split("\t") for x in res.stdout.splitlines() if x.count("\t") >= 4]
    if not hits:
        return ToolResult(f"no line matching /{pattern}/ in the last {hours:g} h")
    lines = [f"{len(hits)} hit(s) for /{pattern}/ in the last {hours:g} h (job · channel · wall clock · seconds in "
             "that job · text):"]
    for job, channel, at, span, text in (h[:4] + ["\t".join(h[4:])] for h in hits[:FIND_MAX_HITS]):
        mine = " (this job)" if job == ctx.clipper_job else ""
        lines.append(f"{job}{mine} · {channel} · {at} · {span} · {text}")
    if len(hits) > FIND_MAX_HITS:
        lines.append(f"[{len(hits) - FIND_MAX_HITS} more: use a more distinctive line]")
    return ToolResult(_clip("\n".join(lines)))


# ---------------------------------------------------------------- picks: validation

def _plain_words(text):
    return re.findall(r"[a-z0-9']+", re.sub(r"\*", "", str(text or "")).lower())


def opener(text):
    """The first OPENER_WORDS words of a hook/title (lowercase, no markup); None when it is shorter."""
    words = _plain_words(text)
    return " ".join(words[:OPENER_WORDS]) if len(words) >= OPENER_WORDS else None


def _check_layout(layout, where, errs):
    if not isinstance(layout, dict):
        errs.append(f"{where}: layout must be {{\"type\": \"fit\"}} or {{\"type\": \"split\", \"face\": [x, y, w, h]}}")
        return
    extra = set(layout) - {"type", "face", "game_x"}
    if extra:
        errs.append(f"{where}: layout has unknown keys {sorted(extra)}")
    kind = layout.get("type")
    if kind == "fit":
        if "face" in layout:
            errs.append(f"{where}: a fit layout takes no face (use type split for a facecam)")
    elif kind == "split":
        face = layout.get("face")
        if not (isinstance(face, list) and len(face) == 4 and all(_num(v) for v in face)):
            errs.append(f"{where}: split needs face [x, y, w, h] as four fractions (read them off view_frame "
                        "grid=true)")
        else:
            x, y, w, h = face
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
                errs.append(f"{where}: face {face} must be fractions 0-1 with x+w and y+h at most 1")
        if "game_x" in layout and not (_num(layout["game_x"]) and 0 <= layout["game_x"] <= 1):
            errs.append(f"{where}: game_x must be between 0 and 1")
    else:
        errs.append(f"{where}: layout type must be fit or split")


def _spans(ctx, pick):
    """[(job, start, end)] of a schema-valid pick (source seconds per job)."""
    if pick.get("segments"):
        return [(s.get("job") or ctx.clipper_job, float(s["start"]), float(s["end"])) for s in pick["segments"]]
    return [(ctx.clipper_job, float(pick["start"]), float(pick["end"]))]


def _check_pick(ctx, pick, n, draft, errs):
    v = f"video {n}"
    if not isinstance(pick, dict):
        errs.append(f"{v}: each pick must be an object")
        return False
    before = len(errs)
    extra = [k for k in pick if k not in PICK_KEYS]
    if extra:
        errs.append(f"{v}: unknown keys {extra} (allowed: {', '.join(PICK_KEYS)})")
    has_seg = "segments" in pick
    if has_seg and ("start" in pick or "end" in pick):
        errs.append(f"{v}: give either start/end (single clip) or segments (story cut), not both")
    elif has_seg:
        segs = pick["segments"]
        if not isinstance(segs, list) or not segs:
            errs.append(f"{v}: segments must be a non-empty list of {{start, end}}")
        else:
            for i, s in enumerate(segs, 1):
                w = f"{v} segment {i}"
                if not isinstance(s, dict):
                    errs.append(f"{w}: must be an object {{start, end}}")
                    continue
                bad = set(s) - {"start", "end", "job", "layout"}
                if bad:
                    errs.append(f"{w}: unknown keys {sorted(bad)}")
                if not (_num(s.get("start")) and _num(s.get("end"))):
                    errs.append(f"{w}: start and end must be numbers (source seconds of its job)")
                    continue
                if s["start"] < 0 or s["end"] - s["start"] < MIN_SEGMENT_S:
                    errs.append(f"{w}: end must be at least {MIN_SEGMENT_S} s after start, both >= 0")
                if s.get("job") not in (None, ctx.clipper_job):
                    try:
                        ok = (job_dir(s["job"]) / "transcript.json").exists()
                    except ValueError:
                        ok = False
                    if not ok:
                        errs.append(f"{w}: job {s['job']!r} doesn't exist (use a job id exactly as find_line "
                                    "printed it)")
                elif ctx.duration and s["end"] > ctx.duration + 0.5:
                    errs.append(f"{w}: ends at {s['end']} s, after the video's end ({ctx.duration:.1f} s)")
                if "layout" in s:
                    _check_layout(s["layout"], w, errs)
    else:
        a, b = pick.get("start"), pick.get("end")
        if not (_num(a) and _num(b)):
            errs.append(f"{v}: needs start and end (numbers, source seconds) or segments")
        elif a < 0 or b - a < MIN_SEGMENT_S:
            errs.append(f"{v}: end must be after start (both source seconds >= 0)")
        elif ctx.duration and b > ctx.duration + 0.5:
            errs.append(f"{v}: ends at {b} s, after the video's end ({ctx.duration:.1f} s)")
    if len(errs) == before:
        dur = sum(e - s for _, s, e in _spans(ctx, pick))
        if dur > MAX_CLIP_S:
            errs.append(f"{v}: {dur:.1f} s long; the maximum is {MAX_CLIP_S:.0f} s across all segments")
    if pick.get("clip_type") not in CLIP_TYPES:
        errs.append(f"{v}: clip_type must be one of {', '.join(CLIP_TYPES)}")
    if "layout" not in pick:
        errs.append(f"{v}: layout is required ({{\"type\": \"fit\"}} or a split with the facecam box)")
    else:
        _check_layout(pick["layout"], v, errs)
    if pick.get("hook_style") not in HOOK_STYLES:
        errs.append(f"{v}: hook_style must be one of {', '.join(HOOK_STYLES)}")
    if pick.get("caption_style") not in CAPTION_STYLES:
        errs.append(f"{v}: caption_style must be one of {', '.join(CAPTION_STYLES)}")
    hooks, titles = pick.get("hooks"), pick.get("titles")
    if not (isinstance(hooks, list) and 1 <= len(hooks) <= MAX_HOOKS
            and all(isinstance(h, str) and h.strip() for h in hooks)):
        errs.append(f"{v}: hooks must be a list of 1-{MAX_HOOKS} non-empty strings")
    else:
        seen = [" ".join(_plain_words(h)) for h in hooks]
        if len(set(seen)) < len(seen):
            errs.append(f"{v}: two hooks are the same text (each becomes its own MP4 and file name)")
        if not (isinstance(titles, list) and len(titles) == len(hooks)
                and all(isinstance(t, str) and t.strip() for t in titles)):
            errs.append(f"{v}: titles must be a list of non-empty strings, one per hook in the same order "
                        f"({len(hooks)} hooks)")
    if not (isinstance(pick.get("description"), str) and pick["description"].strip()):
        errs.append(f"{v}: description must be one sentence")
    tags = pick.get("hashtags")
    if not (isinstance(tags, list) and 1 <= len(tags) <= 8
            and all(isinstance(t, str) and re.fullmatch(r"#\w+", t) for t in tags)):
        errs.append(f"{v}: hashtags must be a list of 1-8 tags like \"#gta6\"")
    if "voiceover" in pick:
        vo = pick["voiceover"]
        if not (isinstance(vo, str) and vo.strip()):
            errs.append(f"{v}: voiceover must be a line of text (leave the key out for none)")
        elif len(vo.split()) > MAX_VOICEOVER_WORDS:
            errs.append(f"{v}: voiceover has {len(vo.split())} words; keep it to {MAX_VOICEOVER_WORDS} or fewer")
        if ctx.voice == "off":
            errs.append(f"{v}: the owner turned voiceovers off: remove the voiceover")
    elif ctx.voice == "all":
        errs.append(f"{v}: the owner wants a voiceover on every video: add one (10 words or fewer)")
    for key, fields in (("labels", ("s", "e", "text")), ("music", ("s", "e", "mood"))):
        items = pick.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            errs.append(f"{v}: {key} must be a list")
            continue
        for i, it in enumerate(items, 1):
            if not (isinstance(it, dict) and set(it) == set(fields) and _num(it["s"]) and _num(it["e"])
                    and it["s"] < it["e"]):
                errs.append(f"{v} {key}[{i}]: needs exactly {', '.join(fields)} with s < e (seconds)")
            elif key == "labels" and not (isinstance(it["text"], str) and it["text"].strip()):
                errs.append(f"{v} labels[{i}]: text must be 1-3 words like *GETS CLOSER*")
            elif key == "music" and it["mood"] not in MOODS:
                errs.append(f"{v} music[{i}]: mood must be one of {', '.join(MOODS)}")
    fixes = pick.get("caption_fixes")
    if fixes is not None and not (isinstance(fixes, dict) and all(
            isinstance(a, str) and a.strip() and isinstance(b, str) and b.strip() for a, b in fixes.items())):
        errs.append(f"{v}: caption_fixes must map misheard words to their spelling, e.g. {{\"swerver\": \"Trevor\"}}")
    fx = pick.get("fx")
    if fx is not None:
        if not isinstance(fx, list):
            errs.append(f"{v}: fx must be a list")
        else:
            for i, f in enumerate(fx, 1):
                ok = isinstance(f, dict) and _num(f.get("t")) and f.get("type") in FX_TYPES \
                    and not set(f) - {"t", "type", "color", "sound"}
                if not ok:
                    errs.append(f"{v} fx[{i}]: needs t (seconds) and type ({', '.join(FX_TYPES)}), optional color "
                                "(flash) or sound (hit)")
                elif "color" in f and (f["type"] != "flash" or not isinstance(f["color"], str)):
                    errs.append(f"{v} fx[{i}]: color only goes on a flash")
                elif "sound" in f and (f["type"] != "hit" or not isinstance(f["sound"], str)):
                    errs.append(f"{v} fx[{i}]: sound only goes on a hit")
    if not draft:
        if not (isinstance(pick.get("why"), str) and pick["why"].strip()):
            errs.append(f"{v}: why is required (German: why this moment works and what you decided)")
        trig = pick.get("click_trigger")
        if not (isinstance(trig, str) and trig.strip()):
            errs.append(f"{v}: click_trigger is required. No nameable trigger, no clip: drop the pick if you can't "
                        f"name one of: {', '.join(TRIGGERS)}")
        elif not any(stem in trig.split(":")[0].lower() for stem in TRIGGER_STEMS):
            errs.append(f"{v}: click_trigger must start with the trigger's name ({', '.join(TRIGGERS)}), then a "
                        "colon and what it is in this clip")
    return len(errs) == before


def validate_picks(ctx, picks, draft=False):
    """(clean clipper picks, reasons, click_triggers, errors). `draft` (the check tool) doesn't need why and
    click_trigger. Errors name the video ("video 2: ...") so the model can fix them all at once."""
    if isinstance(picks, str):
        try:
            picks = json.loads(picks)
        except ValueError:
            return [], [], [], ["picks must be a JSON list of pick objects, not a string"]
    if not isinstance(picks, list):
        return [], [], [], ["picks must be a list of pick objects"]
    errs = []
    if len(picks) > ctx.max_picks:
        errs.append(f"{len(picks)} picks, but at most {ctx.max_picks} video(s) may come from this source: keep the "
                    "strongest")
    valid = [_check_pick(ctx, p, n, draft, errs) for n, p in enumerate(picks, 1)]
    spans = {n: _spans(ctx, p) for n, (p, ok) in enumerate(zip(picks, valid), 1) if ok}
    for a in spans:
        for b in spans:
            if a < b:
                shared = sum(max(0.0, min(e1, e2) - max(s1, s2)) for j1, s1, e1 in spans[a] for j2, s2, e2 in spans[b]
                             if j1 == j2)
                if shared > REUSE_TOLERANCE_S:
                    errs.append(f"video {a} and video {b} share {shared:.0f} s of footage: never use footage twice")
    owners = {}
    for x in ctx.avoid_openers:
        if opener(x):
            owners.setdefault(opener(x), "an earlier video")
    for n, (p, ok) in enumerate(zip(picks, valid), 1):
        if not ok and not isinstance(p, dict):
            continue
        mine = set()
        for t in [*(p.get("hooks") or []), *(p.get("titles") or [])]:
            o = opener(t) if isinstance(t, str) else None
            if o and o in owners and owners[o] != f"video {n}":
                errs.append(f"video {n}: \"{t}\" opens with \"{o}\" like {owners[o]}: vary the wording across videos")
            if o:
                mine.add(o)
        for o in mine:
            owners.setdefault(o, f"video {n}")
    clean, reasons, triggers = [], [], []
    for p in picks:
        if isinstance(p, dict):
            clean.append({k: v for k, v in p.items() if k not in NOTE_KEYS})
            reasons.append(str(p.get("why") or "").strip())
            triggers.append(str(p.get("click_trigger") or "").strip())
    return clean, reasons, triggers, errs


def _report_text(report):
    return _clip(json.dumps(report, ensure_ascii=False, separators=(",", ":")), 8000) if report else "{}"


def run_check(ctx, clean):
    """`clipper check --job` on the picks, also failing footage other pending (not yet rendered) picks hold."""
    payload = {"picks": clean, "job": str(ctx.dir)}
    if ctx.reserved:
        payload["reserved"] = ctx.reserved
    out = _bridge(ctx, "check", payload, "check")
    return out.get("report") or {}, bool(out.get("fails"))


# ---------------------------------------------------------------- pick tools

def check(ctx, inp):
    clean, _, _, errs = validate_picks(ctx, inp.get("picks"), draft=True)
    if errs:
        return ToolResult("Format problems (the check didn't run):\n- " + "\n- ".join(errs), is_error=True)
    report, fails = run_check(ctx, clean)
    verdict = "FAILS: fix every 'fails' entry (and footage/length/labels/variety/fx entries) before submitting" \
        if fails else "no fails" + (" (the notes are judgment calls)" if report else "")
    return ToolResult(f"{verdict}\nreport: {_report_text(report)}")


def simulate(ctx, inp):
    picks = inp.get("picks")
    if not (isinstance(picks, list) and picks and all(isinstance(p, dict) for p in picks)):
        raise ToolError("picks must be a non-empty list of {hooks, titles, hashtags, clip_type}")
    light = []
    for n, p in enumerate(picks, 1):
        hs = p.get("hooks")
        if not (isinstance(hs, list) and hs and all(isinstance(h, str) and h.strip() for h in hs)):
            raise ToolError(f"video {n}: hooks must be a non-empty list of strings")
        light.append({k: p[k] for k in ("hooks", "titles", "hashtags", "clip_type") if k in p})
    keep = ctx.hooks_per_video if ctx.hooks_per_video == "auto" else int(ctx.hooks_per_video)
    out = _bridge(ctx, "rank", {"picks": light, "keep": keep}, "rank")
    lines = []
    for r, p, ranked in zip(out.get("ranking") or [], light, out.get("picks") or []):
        lines.append(f"video {r['video']} ({p.get('clip_type') or 'no clip_type'}): {r['keep_line']}")
        for row in r.get("ranking") or []:
            flag = f" · FAILS check: {'; '.join(row['check'])}" if row.get("check") else ""
            lines.append(f"  {row['x']:.2f}x ({row['low']:.2f}-{row['high']:.2f}) wins {row['wins']:.0%} · "
                         f"{row['hook']} · {', '.join(row['why']) or 'no structure'}{flag}")
        if r.get("ok"):
            kept = {"hooks": ranked.get("hooks"), "titles": ranked.get("titles")}
            lines.append("  kept, in order: " + json.dumps(kept, ensure_ascii=False))
            if r.get("titles_dropped"):
                lines.append(f"  titles without a kept hook (dropped): {r['titles_dropped']}")
        else:
            lines.append("  no hook qualifies: every hook fails check or there are none; rewrite them")
    lines.append("Effects are fitted on titles of 3,481 gaming Shorts: they rank hooks, they don't forecast views.")
    return ToolResult(_clip("\n".join(lines)))


def submit(ctx, inp, *, round_no, max_rounds):
    """A Submission when accepted, else an is_error ToolResult that tells the model what to fix."""
    picks, reason = inp.get("picks"), inp.get("no_clip_reason")
    reason = reason.strip() if isinstance(reason, str) else None
    left = f"(fix round {round_no} of {max_rounds}; the job fails if the next try is rejected too)" \
        if round_no >= max_rounds else f"(fix round {round_no} of {max_rounds})"
    if picks == [] or picks is None:
        if not reason:
            return ToolResult("An empty list needs no_clip_reason (German): say why nothing in this source is worth "
                              "posting. Or submit the picks you mean to make.", is_error=True)
        return Submission([], [], [], reason, {})
    if reason:
        return ToolResult("Give either picks or no_clip_reason, not both: leave no_clip_reason out when you submit "
                          "picks.", is_error=True)
    clean, reasons, triggers, errs = validate_picks(ctx, picks)
    if errs:
        return ToolResult(f"Not accepted {left}. Fix these and call submit_picks again with the complete corrected "
                          "list:\n- " + "\n- ".join(errs), is_error=True)
    report, fails = run_check(ctx, clean)
    if fails:
        return ToolResult(f"Not accepted {left}: `clipper check` fails. Fix every 'fails' entry and every "
                          "footage/length/labels/variety/fx entry, keep or fix the notes, and call submit_picks again "
                          f"with the complete corrected list.\nreport: {_report_text(report)}", is_error=True)
    return Submission(clean, reasons, triggers, None, report)


READ_TOOLS = {"grep_transcript": grep_transcript, "view_frame": view_frame, "find_line": find_line, "check": check,
              "simulate": simulate}


def resolve_name(name):
    """The declared tool a call means: exact, or an unambiguous case/separator variant ("Submit-Picks")."""
    if name in TOOL_NAMES:
        return name
    key = re.sub(r"[^a-z]", "", str(name or "").lower())
    hits = [t for t in TOOL_NAMES if t.replace("_", "") == key]
    return hits[0] if len(hits) == 1 else None


def run_tool(ctx, name, inp):
    """Run a read-only tool; failures the model can act on come back as is_error results."""
    fn = READ_TOOLS.get(name)
    if fn is None:
        return ToolResult(f"unknown tool {name!r}; the tools are {', '.join(TOOL_NAMES)}", is_error=True)
    if not isinstance(inp, dict):
        return ToolResult("the input must be a JSON object", is_error=True)
    try:
        return fn(ctx, inp)
    except ToolError as e:
        return ToolResult(str(e), is_error=True)


# ---------------------------------------------------------------- facts for the first message

def candidates(ctx):
    return _read_json(ctx.dir / "candidates.json") or {}


def music_moods(ctx):
    """(moods with an approved music track, hit sound ids) or None when the library can't be read."""
    try:
        tracks = runtime.run_bridge("music_library", {}, ctx.profile, timeout=TIMEOUTS["music"],
                                    cancel=ctx.cancel).get("tracks") or []
    except runtime.BridgeError as e:
        if e.canceled:
            raise ToolCanceled() from None
        return None
    usable = [t for t in tracks if t.get("usable")]
    return (sorted({t["mood"] for t in usable if t.get("kind") == "music" and t.get("mood")}),
            sorted({t["id"] for t in usable if t.get("kind") == "hit" and t.get("id")}))

