import hashlib
import re
import subprocess

import yaml

from .config import ROOT

MUSIC = ROOT / "assets" / "music"
MOODS = ("suspense", "sneaky", "epic", "sad", "funny")
UNDER_SPEECH_DB, FADE_S, SPEECH_PAD_S = -18.0, 0.3, 0.15
# What a sound effect (kind: hit) is for. A hit lands its beat on the fx time: an impact or a whoosh starts on it,
# a riser ENDS on it (its beat is the end of the file, or `beat_at` seconds in), a tension bed runs up to it.
ROLES = ("impact", "riser", "whoosh", "tension")
LEGACY_ROLES = {"hit": "impact", "boom": "impact", "whoosh": "whoosh"}  # the first three, from before roles
# Level of each sound relative to the clip's own loudness (LU above the bed's integrated loudness, measured at the
# loudest 400 ms of an effect, over the whole of a music track): an impact lands about as loud as the talking peaks,
# a riser builds to just under it, music sits under the talk (and ducks 18 dB more while someone talks). Measured
# on the stand-ins: hotter than this and the final -14 LUFS pass has to turn the talking down to make room.
ROLE_LU = {"impact": 2.0, "riser": -2.0, "whoosh": -3.0, "tension": -10.0, "music": -5.0}
SFX_DUCK_DB = -10.0       # risers and tension beds under speech (music ducks -18 dB)
MAX_GAIN_DB, MIN_GAIN_DB = 6.0, -24.0
HIT_TAIL_S = 2.0          # a one-shot's length when the file's can't be read
RISER_FADE_S = 0.15       # a riser cut short at its start fades in
MIN_RISE_S = 0.5          # a build-up with less room than this before its beat is left out


def _entries(f):
    if not f.exists():
        return []
    return [t for t in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("tracks") or [] if isinstance(t, dict)]


def load_library(folder=None):
    """Tracks from library.yaml, each with its `path` resolved inside the music folder. The shipped library also takes
    local/library.yaml (gitignored: your approvals and your own tracks, which the dashboard writes): an entry with a
    known `id` changes that track (e.g. `approved`), any other entry adds a track; a `file` given there is relative
    to local/ and marks the track `local`."""
    shipped = folder is None
    folder = folder or MUSIC
    tracks = [{**t, "path": folder / str(t["file"])} for t in _entries(folder / "library.yaml") if t.get("file")]
    if shipped:
        local = folder / "local"
        by_id = {t["id"]: i for i, t in enumerate(tracks) if t.get("id")}
        for t in _entries(local / "library.yaml"):
            own = {"path": local / str(t["file"]), "local": True} if t.get("file") else {}
            if t.get("id") and t["id"] in by_id:
                i = by_id[t["id"]]
                tracks[i] = {**tracks[i], **t, **own}
            elif own:
                if t.get("id"):
                    by_id[t["id"]] = len(tracks)
                tracks.append({**t, **own})
    return tracks


def usable(track, cfg):
    """Approved by the user, and not Content ID claimable unless `music.allow_claimable` is on, and on disk."""
    allow = bool((cfg.get("music") or {}).get("allow_claimable", False))
    return track.get("approved") is True and (not track.get("claimable", True) or allow) and track["path"].exists()


def role(track):
    """A sound effect's role (impact | riser | whoosh | tension); None for music. Untagged hits are impacts."""
    if track.get("kind") != "hit":
        return None
    r = track.get("role") or LEGACY_ROLES.get(track.get("id"))
    return r if r in ROLES else "impact"


def tags(track):
    t = track.get("tags") or []
    return {str(x) for x in (t if isinstance(t, list) else [t])}


def _pick(fits, seed):
    return fits[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(fits)] if fits else None


def choose(lib, cfg, kind, mood=None, seed="", want=None, role_=None, tag=None):
    """One usable track of that kind (and mood, for music), the same one for the same seed; the `want` id when it's
    usable. A hit asked for by a role (`want` = impact/riser/whoosh/tension, or `role_`) is one of that role,
    preferring `tag` (a role name always means the role, even where a sound has that id, like whoosh); a wanted id
    that can't play gives way to another of its role. A bare hit is an impact. None when none fits."""
    fits = [t for t in lib if t.get("kind") == kind and usable(t, cfg)
            and (kind != "music" or t.get("mood") == mood)]
    if kind == "hit" and want in ROLES:
        role_, want = want, None
    if want and any(t.get("id") == want for t in fits):
        return next(t for t in fits if t.get("id") == want)
    if kind != "hit":
        return _pick(fits, f"{seed}:{kind}:{mood}")
    if want:
        known = next((t for t in lib if t.get("id") == want), None)
        role_ = role(known) if known else role_
    role_ = role_ or "impact"
    same = [t for t in fits if role(t) == role_]
    tagged = [t for t in same if tag and tag in tags(t)]
    return _pick(tagged or same, f"{seed}:{kind}:{role_}")


_durations = {}


def duration(track):
    """Seconds of audio in the track's file (library `length` first), None when it can't be read."""
    if track.get("length"):
        return float(track["length"])
    p = track["path"]
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key not in _durations:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                  str(p)], capture_output=True, text=True, timeout=30).stdout.strip()
            _durations[key] = float(out) if out else None
        except (OSError, ValueError, subprocess.SubprocessError):
            _durations[key] = None
    return _durations[key]


def beat_at(track):
    """Seconds into the file where its beat is: `beat_at` if the library says, a riser's end, else its start."""
    if track.get("beat_at") is not None:
        return float(track["beat_at"])
    if role(track) == "riser":
        return duration(track) or 0.0
    return 0.0


_peaks = {}
M_RE = re.compile(r"M:\s*(-?[\d.]+)")


def peak_loudness(track):
    """The loudest 400 ms of the sound (max momentary LUFS): library `peak_lufs` first, else measured once."""
    if track.get("peak_lufs") is not None:
        return float(track["peak_lufs"])
    p = track["path"]
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key not in _peaks:
        try:
            res = subprocess.run(["ffmpeg", "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=60)
            vals = [float(m) for m in M_RE.findall(res.stderr) if float(m) > -70]
            _peaks[key] = max(vals) if vals else None
        except (OSError, ValueError, subprocess.SubprocessError):
            _peaks[key] = None
    return _peaks[key]


_lufs = {}


def track_loudness(track):
    """A music track's integrated loudness (library `lufs` first, else measured once)."""
    if track.get("lufs") is not None:
        return float(track["lufs"])
    p = track["path"]
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key not in _lufs:
        try:
            res = subprocess.run(["ffmpeg", "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=120)
            found = re.findall(r"I:\s+(-?[\d.]+) LUFS", res.stderr)
            _lufs[key] = float(found[-1]) if found else None
        except (OSError, ValueError, subprocess.SubprocessError):
            _lufs[key] = None
    return _lufs[key]


def level_gain(track, bed_lufs, offset_db=0.0):
    """dB that puts a sound at its role's level relative to the bed (0 when either loudness is unknown)."""
    if bed_lufs is None:
        return 0.0
    own = track_loudness(track) if track.get("kind") == "music" else peak_loudness(track)
    if own is None:
        return 0.0
    want = bed_lufs + ROLE_LU.get(role(track) or "music", 0.0) + offset_db
    return round(min(max(want - own, MIN_GAIN_DB), MAX_GAIN_DB), 1)


def clip_windows(pick, dur):
    """The pick's `music` windows ({s, e, mood}; source seconds, clip seconds for a segment pick) in clip seconds."""
    off = float(pick.get("start") or 0)
    out = []
    for m in pick.get("music") or []:
        try:
            a, b = max(0.0, float(m["s"]) - off), min(dur, float(m["e"]) - off)
        except (KeyError, TypeError, ValueError):
            continue
        if b - a >= 1.0 and m.get("mood") in MOODS:
            out.append({"s": round(a, 3), "e": round(b, 3), "mood": m["mood"]})
    return out


def speech_spans(words):
    """Merged (start, end) clip seconds where someone talks, padded a little so the music ducks before a word."""
    spans = []
    for w in sorted((w for w in words or [] if not w.get("label")), key=lambda w: w["s"]):
        a, b = max(0.0, w["s"] - SPEECH_PAD_S), w["e"] + SPEECH_PAD_S
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def gain_expr(spans, db=UNDER_SPEECH_DB):
    """FFmpeg volume expression: -18 dB (or `db`) while someone talks, full level otherwise."""
    if not spans:
        return "1"
    talk = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in spans)
    return f"if(gt({talk},0),{10 ** (db / 20):.4f},1)"


def plan(pick, words, fx, dur, cfg, seed="", lib=None, extra=None, bed_lufs=None, level_db=0.0):
    """[(track, start, length, kind)] to mix: music under the pick's windows (and `extra` auto windows: {s, e,
    mood} in clip seconds, or {s, e, sound} for a tension bed), and a sound at every `hit` beat. A placed track is a
    copy carrying `trim_from` (seconds into its file, for a riser that starts late) and `gain_db` (its level against
    the bed: `bed_lufs` = the bed's integrated loudness; `level_db` lifts or lowers the automatic ones)."""
    lib = load_library() if lib is None else lib
    out = []
    for w in clip_windows(pick, dur) + list(extra or []):
        t = choose(lib, cfg, "music", w["mood"], f"{seed}:{w['s']}") if w.get("mood") else None
        if t:
            gain = level_gain(t, bed_lufs, level_db if w.get("fallback") else 0.0)
            out.append(({**t, "gain_db": gain} if gain else t, w["s"], w["e"] - w["s"], "music"))
        elif w.get("sound") or w.get("fallback"):  # a tension bed, or no approved track in that mood
            t = choose(lib, cfg, "hit", seed=f"{seed}:{w['s']}", want=w.get("sound"), role_=w.get("fallback"))
            if t and role(t) == "tension":
                out.append(({**t, "gain_db": level_gain(t, bed_lufs, level_db)}, w["s"], w["e"] - w["s"], "bed"))
    hits = [f for f in fx or [] if f["type"] == "hit" and f["t"] < dur]
    picked = [(f, choose(lib, cfg, "hit", seed=f"{seed}:{f['t']}", want=f.get("sound"), role_=f.get("role"),
                         tag=f.get("tag"))) for f in hits]
    onsets = sorted(f["t"] for f, t in picked if t and beat_at(t) == 0)
    for f, t in picked:
        if not t:
            continue
        b, file_len = beat_at(t), duration(t)
        pre = 0.0
        if b > 0:  # a build-up: it ends on the beat, starting no earlier than the clip or the beat before
            lo = max([0.0] + [x + 0.3 for x in onsets if x < f["t"] - 1e-6])
            pre = min(b, f["t"] - lo, float(f.get("max_pre") or b))
            if pre < MIN_RISE_S:
                continue
        start = f["t"] - pre
        tail = max(0.0, file_len - b) if file_len else HIT_TAIL_S
        length = min(pre + tail, dur - start)
        if length <= 0.05:
            continue
        gain = level_gain(t, bed_lufs, level_db if f.get("auto") else 0.0)
        out.append(({**t, "trim_from": round(b - pre, 3), "gain_db": gain}, round(start, 3), round(length, 3), "hit"))
    return out


def mix(bed, words, layers, dur, out):
    """Mix the planned layers into the bed (music ducked under speech, faded 0.3s at each end; risers and tension
    beds ducked less). Written as 32-bit float so loud beats can't clip before the final loudness pass. Returns
    the bed to use and the track ids used."""
    if not layers:
        return bed, []
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(bed)]
    chains, labels = [], []
    spans = speech_spans(words)
    gain = gain_expr(spans)
    for i, (track, start, length, kind) in enumerate(layers, 1):
        loop = kind in ("music", "bed")
        cmd += (["-stream_loop", "-1"] if loop else []) + ["-i", str(track["path"])]
        a = float(track.get("trim_from") or 0.0)
        f = f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim={a:.3f}:{a + length:.3f}" \
            if a else f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{length:.3f}"
        f += ",asetpts=PTS-STARTPTS"
        if loop:
            f += f",afade=t=in:d={FADE_S},afade=t=out:st={max(0.0, length - FADE_S):.3f}:d={FADE_S}"
        elif a:
            f += f",afade=t=in:d={min(RISER_FADE_S, length / 2):.3f}"
        if track.get("gain_db"):
            f += f",volume={track['gain_db']:.1f}dB"
        f += f",adelay={int(round(start * 1000))}:all=1"
        if kind == "music":  # after the delay, t is clip time, so it lines up with the words
            f += f",volume='{gain}':eval=frame"
        elif kind == "bed" or role(track) == "riser":
            f += f",volume='{gain_expr(spans, SFX_DUCK_DB)}':eval=frame"
        chains.append(f + f"[m{i}]")
        labels.append(f"[m{i}]")
    graph = ";".join(chains) + f";[0:a]{''.join(labels)}amix=inputs={len(layers) + 1}:duration=first:normalize=0"
    subprocess.run(cmd + ["-filter_complex", graph, "-t", f"{dur:.3f}", "-c:a", "pcm_f32le", str(out)], check=True)
    return out, list(dict.fromkeys(str(t.get("id") or t["file"]) for t, *_ in layers))
