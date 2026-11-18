"""Sound on the clip's epic beats, when a pick doesn't spell out its `fx`: find the real peaks (a story cut's payoff,
the biggest loudness jump, a shouted line), score at most a few of them, and build into the strongest one.

    envelope(wav) -> (levels dB per 50 ms, hop)
    jumps(levels, hop) -> [(t, jump dB)]   onsets where the audio leaps above the seconds before it
    plan_beats(words, dur, payoff, level, levels, hop, music_ok) -> {fx, windows, beats, level}

Sparse, like the reference channel (research "Zylok channel survey": a boom about every 19 s, never more than 3 a
clip, on the payoff or a big reaction): `sound_level` caps the beats per clip and the gap between them, and only
the main beat gets a build-up (a riser that ends on the beat, then the impact) and, with a speech-free run-up of
a few seconds, a suspense bed. Speech stays on top: risers and beds duck under it (clipper.music.mix).

Levels (pick key `sound_level`, config default `sound_level`):
    off     no automatic sound (a pick's own fx and music still play)
    subtle  one impact on the strongest beat (a story cut's payoff, or a jump of 12 dB+), no build-up, no bed
    normal  up to 2 beats 8 s+ apart (jumps of 10 dB+); the main one gets a riser of up to 2 s and an impact, the
            other a boom; a suspense bed under a 3 s+ speech-free run-up into the main beat
    strong  up to 3 beats 6 s+ apart (8 dB+); risers up to 3 s; beds from 2 s of run-up; everything 2 dB louder
"""
import wave

import numpy as np

LEVELS = ("off", "subtle", "normal", "strong")
DEFAULT_LEVEL = "normal"
LIMITS = {
    "subtle": {"beats": 1, "gap": 10.0, "jump": 12.0, "riser": 0.0, "bed": None, "db": -3.0},
    "normal": {"beats": 2, "gap": 8.0, "jump": 10.0, "riser": 2.0, "bed": 3.0, "db": 0.0},
    "strong": {"beats": 3, "gap": 6.0, "jump": 8.0, "riser": 3.0, "bed": 2.0, "db": 2.0},
}
HOP = 0.05
EDGE_S = 0.4          # no beat in the first or last 0.4 s
SHOUT_DB = 8.0        # what a shouted word ("!") counts as when no jump is measured there (enough for strong only)
MERGE_S = 0.5         # a shout and a jump this close are one beat
WHY_DE = {"payoff": "Pointe der Story", "jump": "Lautstärkesprung", "shout": "geschriener Satz"}
LEVEL_DE = {"off": "aus", "subtle": "dezent", "normal": "normal", "strong": "stark"}


def level_of(pick, cfg):
    """(level, source): the pick's sound_level, else the config's, else normal."""
    for src, where in (("pick", pick), ("config", cfg or {})):
        if where.get("sound_level") in LEVELS:
            return where["sound_level"], src
    return DEFAULT_LEVEL, "default"


def envelope(wav):
    """RMS level (dBFS) per 50 ms of a WAV file, mono-summed; (levels, hop). Empty on an unreadable file."""
    try:
        with wave.open(str(wav)) as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        return np.zeros(0), HOP
    dtype = {2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        return np.zeros(0), HOP
    x = np.frombuffer(raw, dtype=dtype).astype(np.float32) / float(np.iinfo(dtype).max)
    x = x[: len(x) // ch * ch].reshape(-1, ch).mean(1)
    n = int(sr * HOP)
    frames = x[: len(x) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-6), HOP


def smooth(levels, hop=HOP, window=0.4):
    """The level over a sliding 400 ms window (like momentary loudness): single syllables don't count as jumps."""
    x = np.asarray(levels, dtype=float)
    k = max(1, int(round(window / hop)))
    e = np.convolve(10 ** (x / 10), np.ones(k) / k, mode="same")
    return 10 * np.log10(e + 1e-12)


def jumps(levels, hop=HOP, lookback=3.0, gap=0.5, min_db=6.0, join=0.3):
    """[(t, dB)]: onsets where the level (400 ms windows) leaps above the seconds before it (a scream after talk, a
    blast). The baseline never drops below the clip's typical level, so talk starting after a pause isn't a jump
    (signals.jump_per_second does the same per second). One per event: frames above the baseline by min_db, runs
    less than `join` s apart merged; its onset is where the fine 50 ms curve rose half way to the event's peak."""
    x = np.asarray(levels, dtype=float)
    if len(x) < int(1.5 / hop):
        return []
    sm = smooth(x, hop)
    voiced = sm[sm > -60]
    floor = float(np.median(voiced)) if len(voiced) else -60.0
    lb, g = int(lookback / hop), int(gap / hop)
    start = int(1.0 / hop)
    base = np.full(len(sm), np.inf)
    for i in range(start, len(sm)):
        base[i] = max(float(np.median(sm[max(0, i - lb):i - g])), floor)
    over = np.nonzero(sm - base >= min_db)[0]
    events = []
    for i in over:
        if events and i - events[-1][-1] <= int(join / hop):
            events[-1].append(i)
        else:
            events.append([i])
    out = []
    for ev in events:
        first = ev[0]
        peak = max(range(first, ev[-1] + 1), key=lambda k: sm[k] - base[first])
        d = float(sm[peak] - base[first])
        half = base[first] + d / 2
        j = max(0, first - int(0.25 / hop))  # the centred 400 ms window rises up to 0.2 s before the level does
        while j < peak and x[j] < half:
            j += 1
        out.append((round(float(j * hop), 2), round(d, 1)))
    return out


def candidates(words, dur, payoff, found):
    """[{t, strength, why}] for the clip: the payoff (always strongest), measured jumps, shouted words."""
    out = []
    if payoff is not None and EDGE_S <= payoff < dur - EDGE_S:
        out.append({"t": round(float(payoff), 3), "strength": 99.0, "why": "payoff"})
    for t, d in found:
        if EDGE_S <= t < dur - EDGE_S:
            out.append({"t": float(t), "strength": float(d), "why": "jump"})
    for w in words or []:
        if w.get("label") or not str(w["w"]).rstrip("\"')").endswith("!") or not EDGE_S <= w["s"] < dur - EDGE_S:
            continue
        near = [c for c in out if abs(c["t"] - w["s"]) <= MERGE_S]
        if near:
            for c in near:
                c["shout"] = True
        else:
            out.append({"t": round(w["s"], 3), "strength": SHOUT_DB, "why": "shout"})
    return out


def select(cands, level):
    """The beats to score, strongest first: at most the level's count, its gap apart, strong enough."""
    lim = LIMITS[level]
    chosen = []
    for c in sorted(cands, key=lambda c: (-(c["strength"] + (2.0 if c.get("shout") else 0.0)), c["t"])):
        # a jump under a shouted word counts 2 dB more
        if c["why"] != "payoff" and c["strength"] + (2.0 if c.get("shout") else 0.0) < lim["jump"]:
            continue
        if any(abs(c["t"] - x["t"]) < lim["gap"] for x in chosen):
            continue
        chosen.append(c)
        if len(chosen) >= lim["beats"]:
            break
    return chosen


def speech_free_before(words, t, pad=0.15, own=0.3):
    """(seconds, since): how long nobody talked before the beat at t, and from when. Words starting within `own`
    seconds of t belong to the beat itself (the scream that makes it)."""
    ends = [w["e"] for w in words or [] if not w.get("label") and w["s"] < t - own]
    since = max(ends) + pad if ends else 0.0
    return max(0.0, t - since), min(since, t)


def plan_beats(words, dur, payoff, level, levels=None, hop=HOP, music_ok=True):
    """What to add: `fx` (hit entries with role/tag, auto=True, plus a white flash on a main beat that isn't the
    payoff), `windows` (a run-up bed, clip seconds), `beats` (what was scored and why, for the render result)."""
    if level not in LIMITS:
        return {"fx": [], "windows": [], "beats": [], "level": level}
    lim = LIMITS[level]
    found = jumps(levels, hop) if levels is not None and len(levels) else []
    beats = select(candidates(words, dur, payoff, found), level)
    fx, windows, info = [], [], []
    for k, b in enumerate(beats):
        main = k == 0
        t = b["t"]
        entry = {"t": t, "why": b["why"], "strength": round(b["strength"], 1) if b["why"] == "jump" else None,
                 "roles": []}
        if main and lim["riser"]:
            fx.append({"t": t, "type": "hit", "role": "riser", "auto": True, "max_pre": lim["riser"]})
            entry["roles"].append("riser")
        tag = "big" if main and level != "subtle" else "boom"
        fx.append({"t": t, "type": "hit", "role": "impact", "tag": tag, "auto": True})
        entry["roles"].append("impact")
        if main and b["why"] != "payoff":
            fx.append({"t": t, "type": "flash", "color": "0xFFFFFF", "auto": True})
        if main and lim["bed"] and music_ok:
            quiet, since = speech_free_before(words, t)
            if quiet >= lim["bed"]:
                windows.append({"s": round(since + 0.1, 3), "e": round(t + 0.15, 3), "mood": "suspense",
                                "fallback": "tension"})
                entry["bed"] = [round(since + 0.1, 2), round(t, 2)]
        info.append(entry)
    return {"fx": fx, "windows": windows, "beats": info, "level": level}


def describe(level, beats):
    """One German line for the dashboard: 'Sound: normal – Riser + Impact bei 9,5 s (Pointe der Story), …'."""
    if level == "off":
        return "Sound: aus (keine automatischen Effekte)"
    if not beats:
        return f"Sound: {LEVEL_DE.get(level, level)} – kein Moment laut genug für einen Effekt"
    parts = []
    for b in beats:
        what = " + ".join({"riser": "Riser", "impact": "Impact"}[r] for r in b["roles"])
        bed = " mit Spannungs-Bett" if b.get("bed") else ""
        why = WHY_DE[b["why"]] + (f" +{b['strength']:.0f} dB" if b.get("strength") else "")
        parts.append(f"{what}{bed} bei {b['t']:.1f} s".replace(".", ",") + f" ({why})")
    return f"Sound: {LEVEL_DE.get(level, level)} – " + "; ".join(parts)
