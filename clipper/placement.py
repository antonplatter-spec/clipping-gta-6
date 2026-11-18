"""Where captions and the hook sit on a Short, how big, in which look, and how long the hook stays. Decided once per
clip from a few sampled frames of the composited video, before any hook variant renders, so every variant (and
`clipper preview`) shares one placement and a re-render gets the same one.

    plan(pick, cfg, frames, src_size, hooks, dur, key) -> JSON-safe dict: `captions` and `hook` (what was chosen,
        why, in German too: `text_de`) plus the drawing parameters captions.render_state/render_hook take
    analyse(frames) -> per-pixel maps (motion, detail, static overlays, brightness, skin) the choice is made from
    validate(pick) -> errors for `clipper check`

Pick keys (each one a config default in watchlist.yaml too; the pick wins; anything left out is `auto`):
    caption_position  auto | high | seam | low | classic
    caption_size      auto | small | medium | large
    caption_look      auto | classic | a look id from captions.LOOKS     (config `caption_looks`: what auto may use)
    hook_size         auto | small | medium | large
    hook_seconds      auto | whole | 1-30 (seconds, then a 0.4 s fade)

The canvas is 1080x1920. `fit`: the game picture is centred (y 656-1264 for 16:9) over a blurred copy, the hook on
the blur above it. `split`: the facecam fills y 0-768, the game y 768-1920, the hook on the seam. Slots:
    high     fit: on the blur just above the game     split: on the game just under the seam (and the hook)
    seam     fit: on the game's lower edge            split: on the seam between facecam and game (no hook there)
    low      fit: on the blur under the game          split: on the game just above the platform's bottom bar
    classic  the fixed pre-adaptive line (fit y 1420, split y 1380), still kept inside the safe area
auto takes the layout's default (fit: low, split: seam without a hook, else high) unless another slot measures
clearly calmer (less motion, less detail, no static overlay such as a HUD or chat box).
"""
import hashlib

import numpy as np

from . import captions

W, H = 1080, 1920
# Platform UI over a vertical video (YouTube Shorts, Instagram Reels, TikTok). These are approximations of the
# commonly published 1080x1920 safe-zone templates, rounded outward to cover all three; every app moves its
# buttons a little between versions, so treat them as a margin, not exact figures.
SAFE_TOP = 200        # status bar, the Shorts / Reels / "For You" tabs and the search and camera icons
SAFE_BOTTOM = 1480    # below: channel or username, title or caption, the sound row (TikTok's caption reaches highest)
SAFE_SIDE = 60        # rounded screen corners
BUTTONS_X, BUTTONS_Y = 930, 820  # the like / comment / share column: right of x 930, from y 820 down
SEAM = 768            # split: facecam above, game below
FACE_GUARD = 630      # split: nothing is drawn above this line (the face sits in the facecam's top ~80%)
FIT_HOOK_Y = 400      # fit: the hook's centre line, as before

SIZES = {"small": 0.7, "medium": 0.85, "large": 1.0}
POSITIONS = ("auto", "high", "seam", "low", "classic")
SIZE_VALUES = ("auto", "small", "medium", "large")
LOOK_VALUES = ("auto", "classic", *captions.LOOKS)
HOOK_SECONDS_MAX = 30
# split hooks sit on the seam over game and face, so by default they leave after a few seconds; news/showcase hooks
# are the title card (MrBossFTW pins them all video) and stay. fit hooks sit on the blur and always stay.
HOOK_SECONDS = {"reaction": 3.0, "gameplay": 4.0, "comedy": 5.0, "scenario": 5.0, "news": None, "showcase": None}
HOOK_SECONDS_OTHER = 5.0
HOOK_FADE_S = 0.4
MARGIN = 0.25         # how much calmer (cost units) another slot must measure to beat the layout's default
# Known before looking: in third-person games (GTA) the player character stands in the lower middle of the
# picture, which in split is right where `low` sits, so `low` starts this much behind.
PRIOR = {("split", "low"): 0.35}
AUTO_SLOTS = {"fit": ("high", "low"), "split": ("seam", "high", "low")}  # fit's seam covers the game: asked only
SCALE = 4             # analysis frames are 270x480: one analysis pixel = 4 canvas pixels
FRAME_SIZE = (W // SCALE, H // SCALE)

POS_DE = {"fit": {"high": "über dem Spielbild", "seam": "an der Unterkante des Spielbilds",
                  "low": "unter dem Spielbild", "classic": "alte Position"},
          "split": {"high": "oben im Spielbild", "seam": "auf der Naht zwischen Kamera und Spiel",
                    "low": "unten im Spielbild", "classic": "alte Position (Spielmitte)"}}
SIZE_DE = {"small": "klein", "medium": "mittel", "large": "groß"}
REASON_DE = {"quietest": "ruhigster Bereich", "default": "Standard für dieses Layout (kein Bereich klar ruhiger)",
             "no_frames": "Standard für dieses Layout (Bild nicht analysiert)", "pick": "im Clip festgelegt",
             "config": "Voreinstellung", "seam_taken": "die Naht gehört dem Hook, daher oben im Spielbild"}
TYPE_DE = {"reaction": "Reaktion: das Gesicht ist der Hook", "gameplay": "Gameplay: die Aktion ist die Pointe",
           "comedy": "Comedy", "scenario": "Szenario", "news": "News: der Hook ist die Titelkarte",
           "showcase": "Showcase: der Hook ist die Titelkarte"}


# ---------------------------------------------------------------- settings

def _seconds(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 1 <= x <= HOOK_SECONDS_MAX else None


def valid(key, v):
    if key == "hook_seconds":
        return v in ("auto", "whole") or (not isinstance(v, bool) and _seconds(v) is not None)
    return v in {"caption_position": POSITIONS, "caption_size": SIZE_VALUES, "caption_look": LOOK_VALUES,
                 "hook_size": SIZE_VALUES}[key]


def allowed(key):
    if key == "hook_seconds":
        return f"auto, whole or a number of seconds 1-{HOOK_SECONDS_MAX}"
    return ", ".join({"caption_position": POSITIONS, "caption_size": SIZE_VALUES, "caption_look": LOOK_VALUES,
                      "hook_size": SIZE_VALUES}[key])


KEYS = ("caption_position", "caption_size", "caption_look", "hook_size", "hook_seconds")


def setting(pick, cfg, key):
    """(value, source): the pick's value, else the config default, else auto; an invalid value counts as auto
    (`clipper check` reports it)."""
    for src, where in (("pick", pick), ("config", cfg or {})):
        v = where.get(key)
        if v is not None and valid(key, v):
            return v, (src if v != "auto" else "auto")
    return "auto", "auto"


def validate(pick):
    """`clipper check` errors for the pick's caption/hook keys: an unknown value names the allowed ones."""
    out = []
    for key in KEYS:
        if key in pick and not valid(key, pick[key]):
            out.append(f"{key} {pick[key]!r} isn't one of: {allowed(key)}")
    looks = pick.get("caption_look")
    if looks in captions.LOOKS and pick.get("caption_style") and \
            captions.LOOKS[looks]["family"] != pick["caption_style"]:
        out.append(f"caption_look '{looks}' belongs to caption_style '{captions.LOOKS[looks]['family']}', not "
                   f"'{pick['caption_style']}': pick a look of that style ({', '.join(family_looks(pick['caption_style']))}) "
                   "or auto")
    return out


def family_looks(style):
    return [k for k, v in captions.LOOKS.items() if v["family"] == style]


# ---------------------------------------------------------------- geometry

def layout_name(pick):
    """fit or split: which geometry captions and hook use (a story cut uses split when any segment shows a facecam,
    like render.build_segment_job)."""
    lay = pick.get("layout") or {}
    if pick.get("segments"):
        types = [(s.get("layout") or lay or {"type": "fit"}).get("type", "fit") for s in pick["segments"]]
        return lay.get("as") or ("split" if "split" in types else "fit")
    if lay.get("type") == "vertical":
        return lay.get("as", "fit")
    return "split" if lay.get("type") == "split" else "fit"


def geometry(layout, src_size=(1920, 1080), face_guard=FACE_GUARD):
    """Where the game picture (and in split the seam) sits on the canvas."""
    if layout == "split":
        return {"layout": "split", "game": (SEAM, H), "seam": SEAM, "face_guard": int(face_guard)}
    sw, sh = src_size or (1920, 1080)
    gh = min(H, max(2, int(round(W * sh / max(1, sw) / 2)) * 2))
    gy0 = (H - gh) // 2
    return {"layout": "fit", "game": (gy0, gy0 + gh), "seam": None, "face_guard": 0}


def max_width(y0, y1):
    """Widest a centred text block may be between y0 and y1: clear of the screen edges, and of the button column
    where it reaches the button rows."""
    return 2 * (BUTTONS_X - W // 2) if y1 > BUTTONS_Y else W - 2 * SAFE_SIDE


def line_height(look, scale):
    return int(round(captions.LOOKS[look]["size"] * scale) * 1.18)


def nominal_lines(look):
    return 1 if captions.LOOKS[look]["words"] <= 2 else 2


def slots(geo, reserve):
    """{name: slot} this layout offers. A slot: anchor (top/centre/bottom) at y, the room [top, bottom] the block
    may use, and what it covers ("game", "blur" or "seam"). `reserve` = the hook banner's (top, bottom) or None."""
    split = geo["layout"] == "split"
    floor = max(SAFE_TOP, geo["face_guard"] + 8 if split else SAFE_TOP)
    under_hook = max(floor, reserve[1] + 16) if reserve else floor
    gy0, gy1 = geo["game"]
    out = {}
    if split:
        if not reserve:
            out["seam"] = {"anchor": "centre", "y": SEAM, "top": floor, "bottom": SAFE_BOTTOM, "on": "seam"}
        out["high"] = {"anchor": "top", "y": max(SEAM + 24, under_hook + 4), "top": under_hook,
                       "bottom": SAFE_BOTTOM, "on": "game"}
        out["low"] = {"anchor": "bottom", "y": SAFE_BOTTOM - 8, "top": under_hook, "bottom": SAFE_BOTTOM,
                      "on": "game"}
        out["classic"] = {"anchor": "centre", "y": captions.LAYOUTS["split"]["caption_y"], "top": under_hook,
                          "bottom": SAFE_BOTTOM, "on": "game"}
    else:
        out["high"] = {"anchor": "bottom", "y": gy0 - 16, "top": under_hook, "bottom": SAFE_BOTTOM, "on": "blur"}
        out["seam"] = {"anchor": "centre", "y": gy1, "top": under_hook, "bottom": SAFE_BOTTOM, "on": "game"}
        out["low"] = {"anchor": "centre", "y": (gy1 + SAFE_BOTTOM) // 2, "top": under_hook, "bottom": SAFE_BOTTOM,
                      "on": "blur"}
        out["classic"] = {"anchor": "centre", "y": captions.LAYOUTS["fit"]["caption_y"], "top": under_hook,
                          "bottom": SAFE_BOTTOM, "on": "blur"}
    for name, s in out.items():  # extra speakers stack away from the platform's bottom bar and the face
        s["stack"] = "up" if name == "classic" or s["anchor"] == "bottom" or not split else "down"
    return out


def default_slot(geo, reserve):
    if geo["layout"] == "split":
        return "high" if reserve else "seam"
    return "low"


def block_span(slot, h):
    """(y0, y1) of a block h px tall in the slot, moved inside the slot's room."""
    y = slot["y"]
    y0 = y - h // 2 if slot["anchor"] == "centre" else y if slot["anchor"] == "top" else y - h
    y0 = min(max(y0, slot["top"]), slot["bottom"] - h)
    return max(0, y0), min(H, y0 + h)


# ---------------------------------------------------------------- analysis

def analyse(frames, boundaries=()):
    """Per-pixel maps over the sampled frames ((n, 480, 270, 3) uint8, composited canvas at 1/4 scale): mean luma,
    its 90th percentile, edge detail, change between frames (motion), static detail (HUD, overlays: edges that stay
    put) and skin (YCbCr rule) per frame. `boundaries`: canvas rows where the layout itself joins two pictures (the
    split seam, the fit game's edges); their edge isn't content, so it doesn't count. None when there are no frames."""
    if frames is None or len(frames) == 0:
        return None
    f = frames.astype(np.float32) / 255.0
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    dx = np.abs(np.diff(luma, axis=2, append=luma[:, :, -1:]))
    dy = np.abs(np.diff(luma, axis=1, append=luma[:, -1:, :]))
    edges = dx + dy
    for y in boundaries:
        edges[:, max(0, y // SCALE - 1):y // SCALE + 1] = 0
    motion = np.abs(np.diff(luma, axis=0)).mean(0) if len(luma) > 1 else np.zeros(luma.shape[1:], np.float32)
    static = (((edges > 0.12).mean(0) >= 0.85) & (luma.std(0) < 0.04)).astype(np.float32)
    cb = 128 - 37.797 * r - 74.203 * g + 112.0 * b
    cr = 128 + 112.0 * r - 93.786 * g - 18.214 * b
    skin = (cr >= 135) & (cr <= 180) & (cb >= 80) & (cb <= 135) & (luma > 0.3)
    return {"luma": luma.mean(0), "luma90": np.percentile(luma, 90, axis=0), "detail": edges.mean(0),
            "motion": motion, "static": static, "skin": skin, "n": len(frames)}


def face_guard(maps, base=FACE_GUARD):
    """split: the lowest canvas row the face may reach. Skin found lower in the facecam (a tight crop) moves the
    guard down, never up, so the analysis can only make it safer."""
    if maps is None:
        return base
    rows = SEAM // SCALE
    w = maps["skin"].shape[2]
    centre = maps["skin"][:, :rows, w // 5: w - w // 5].mean(2)  # (n, rows): skin share per row
    found = np.nonzero(np.median(centre, axis=0) >= 0.25)[0]
    if not len(found):
        return base
    return int(min(SEAM, max(base, (found[-1] + 1) * SCALE + 12)))


def region_cost(maps, ref, y0, y1, x0, x1):
    """How much a caption block hides there (0 = nothing moves or has detail): motion and detail relative to the
    game area's own average, plus any static overlay (HUD, chat box) it would sit on."""
    a, b = max(0, y0 // SCALE), max(y0 // SCALE + 1, y1 // SCALE)
    c, d = x0 // SCALE, max(x0 // SCALE + 1, x1 // SCALE)
    m = float(maps["motion"][a:b, c:d].mean()) / ref["motion"]
    t = float(maps["detail"][a:b, c:d].mean()) / ref["detail"]
    s = float(maps["static"][a:b, c:d].mean())
    return {"cost": round(0.5 * min(m, 3) + 0.3 * min(t, 3) + 1.5 * s, 3), "motion": round(m, 2),
            "detail": round(t, 2), "static": round(s, 2), "luma": round(float(maps["luma90"][a:b, c:d].mean()), 2)}


def references(maps, geo):
    gy0, gy1 = geo["game"]
    a, b = gy0 // SCALE, max(gy0 // SCALE + 1, min(gy1, SAFE_BOTTOM) // SCALE)
    eps = 1e-3
    return {"motion": max(eps, float(maps["motion"][a:b].mean())), "detail": max(eps, float(maps["detail"][a:b].mean()))}


# ---------------------------------------------------------------- the plan

def clip_key(key):
    return int(hashlib.sha1(f"look:{key}".encode()).hexdigest()[:8], 16)


def auto_size(slot):
    return "small" if slot["on"] == "game" else "medium"


def look_for(pick, cfg, family, key):
    """(look id, source, candidates): the pick's or config's look; auto = one of the family's allowed looks by a
    hash of the clip (the same clip always gets the same look); classic = the family's own look."""
    v, src = setting(pick, cfg, "caption_look")
    if v == "classic":
        return family, src, [family]
    if v in captions.LOOKS:
        return v, src, [v]
    allowed_ = cfg.get("caption_looks") if isinstance((cfg or {}).get("caption_looks"), list) else None
    fam = [k for k in family_looks(family) if allowed_ is None or k in allowed_] or [family]
    return fam[clip_key(key) % len(fam)], "auto", fam


def hook_plan(pick, cfg, geo, hooks, style, dur):
    """Size, place and time of the hook banner: one centre line for every variant; `reserve` = the tallest
    variant's (top, bottom), which captions keep clear of for the whole clip."""
    size, size_src = setting(pick, cfg, "hook_size")
    size = size if size != "auto" else ("large" if geo["layout"] == "fit" else "small")
    scale = SIZES[size]
    split = geo["layout"] == "split"
    plan = {"size": size, "size_source": size_src, "scale": scale, "top": SAFE_TOP, "bottom": SAFE_BOTTOM}
    if split:
        plan.update(centre=SEAM, top=geo["face_guard"] + 8, max_width=2 * (BUTTONS_X - W // 2) - 80,
                    box=[W - BUTTONS_X, BUTTONS_X])
    else:
        plan.update(centre=FIT_HOOK_Y, max_width=900, box=[50, W - 50])
    heights = [hl["h"] for hl in (captions.hook_layout(h, style, scale, plan["max_width"]) for h in hooks or [])
               if hl]
    tall = max(heights, default=0)
    if not split and tall:  # stay above the game picture when there's room, never under the top bar
        top = max(SAFE_TOP, min(FIT_HOOK_Y - tall // 2, geo["game"][0] - 12 - tall))
        plan["centre"] = top + tall // 2
    if tall:
        tops = [captions.hook_top(plan, h) for h in heights]
        plan["reserve"] = [min(tops), max(t + h for t, h in zip(tops, heights))]
    else:
        plan["reserve"] = None
    secs, sec_src = setting(pick, cfg, "hook_seconds")
    kind = pick.get("clip_type")
    if secs == "auto":
        secs = None if not split else HOOK_SECONDS.get(kind, HOOK_SECONDS_OTHER)
    elif secs == "whole":
        secs = None
    else:
        secs = _seconds(secs)
    if secs is not None and dur is not None and secs >= dur - HOOK_FADE_S:
        secs = None
    plan.update(seconds=secs, seconds_source=sec_src)
    if not tall:
        plan["text_de"] = "Hook: keiner (ohne Banner)"
    else:
        when = "ganzer Clip" if secs is None else f"{secs:g} s, dann ausblenden"
        why = ("im Clip festgelegt" if sec_src == "pick" else "Voreinstellung" if sec_src == "config"
               else "liegt über der Unschärfe, verdeckt nichts" if not split and secs is None
               else TYPE_DE.get(kind, "Standard"))
        where = "auf der Naht" if split else "über dem Spielbild"
        plan["text_de"] = f"Hook: {where}, {SIZE_DE[size]}, {when} – {why}"
    return plan


def caption_plan(pick, cfg, geo, reserve, maps, key):
    family = pick.get("caption_style") if pick.get("caption_style") in captions.CAPTION_STYLES else \
        (cfg or {}).get("caption_style") if (cfg or {}).get("caption_style") in captions.CAPTION_STYLES else "bold"
    look, look_src, looks = look_for(pick, cfg, family, key)
    pos, pos_src = setting(pick, cfg, "caption_position")
    size_v, size_src = setting(pick, cfg, "caption_size")
    offered = slots(geo, reserve)
    default = default_slot(geo, reserve)
    ref = references(maps, geo) if maps is not None else None
    scores = {}

    def size_for(name):
        if size_v != "auto":
            return size_v
        return "large" if name == "classic" else auto_size(offered[name])

    def measure(name):
        sl = offered[name]
        h = nominal_lines(look) * line_height(look, SIZES[size_for(name)])
        y0, y1 = block_span(sl, h)
        mw = max_width(y0, sl["bottom"] if sl["stack"] == "down" else y1)
        r = region_cost(maps, ref, y0, y1, (W - mw) // 2, (W + mw) // 2)
        r["cost"] = round(r["cost"] + PRIOR.get((geo["layout"], name), 0.0), 3)
        gy0, gy1 = geo["game"]
        if sl["on"] == "blur":  # a blur slot that had to move onto the game hides that part of it
            r["cost"] = round(r["cost"] + max(0, min(y1, gy1) - max(y0, gy0)) / h, 3)
        return r

    if pos == "auto":
        names = [n for n in AUTO_SLOTS[geo["layout"]] if n in offered]
        if ref is None:
            chosen, reason = default, "no_frames"
        else:
            scores = {n: measure(n) for n in names}
            best = min(names, key=lambda n: (scores[n]["cost"], n != default))
            if best == default:
                clear = all(scores[n]["cost"] - scores[best]["cost"] >= MARGIN for n in names if n != best)
                chosen, reason = best, "quietest" if clear else "default"
            elif scores[default]["cost"] - scores[best]["cost"] < MARGIN:  # inconclusive: keep the default
                chosen, reason = default, "default"
            else:
                chosen, reason = best, "quietest"
    else:
        chosen, reason = pos, pos_src
        if chosen not in offered:  # the seam with a hook on it
            chosen, reason = "high", "seam_taken"
        if ref is not None:
            scores = {chosen: measure(chosen)}
    sl = offered[chosen]
    size = size_for(chosen)
    scale = SIZES[size]
    calm = False
    if ref is not None:
        r = scores.get(chosen) or measure(chosen)
        calm = r["luma"] < 0.7 and r["detail"] < 0.9
    if captions.LOOKS[look].get("needs") == "calm" and not calm and look_src == "auto":
        look = family  # a thin outline over bright or busy footage: the family's classic look instead
    h = nominal_lines(look) * line_height(look, scale)
    y0, y1 = block_span(sl, h)
    plan = {"position": chosen, "position_source": pos_src, "size": size, "size_source": size_src, "look": look,
            "look_source": look_src, "reason": reason, "scale": scale, "anchor": sl["anchor"], "y": int(sl["y"]),
            "top": int(sl["top"]), "bottom": int(sl["bottom"]), "stack": sl["stack"], "on": sl["on"],
            "max_width": int(max_width(y0, sl["bottom"] if sl["stack"] == "down" else y1)),
            "span": [int(y0), int(y1)], "scores": {n: s["cost"] for n, s in scores.items()},
            "measured": scores.get(chosen), "looks": looks}
    plan["text_de"] = (f"Untertitel: {POS_DE[geo['layout']][chosen]}, {SIZE_DE[size]} – {REASON_DE[reason]}"
                       f" · Look: {captions.LOOK_DE.get(look, look)}")
    return plan


def plan(pick, cfg, frames=None, src_size=(1920, 1080), hooks=(), dur=None, key=""):
    """The clip's placement (see the module doc). `frames`: sampled composited frames (render.clip_frames), None
    = no analysis (layout defaults). `hooks`: the banner texts that will be rendered ([] or [""] = none). `key`:
    the clip's identity (its footage) so the look is the same on every re-render."""
    cfg = cfg or {}
    layout = layout_name(pick)
    geo = geometry(layout, src_size)
    maps = analyse(frames, [SEAM] if layout == "split" else list(geo["game"]))
    guard = face_guard(maps) if layout == "split" else 0
    geo = geometry(layout, src_size, guard if layout == "split" else FACE_GUARD)
    style = pick.get("hook_style", "box")
    hp = hook_plan(pick, cfg, geo, [h for h in hooks if (h or "").strip()], style, dur)
    cp = caption_plan(pick, cfg, geo, hp["reserve"], maps, key)
    return {"layout": layout, "face_guard": guard if layout == "split" else None, "game": list(geo["game"]),
            "frames": 0 if maps is None else maps["n"], "captions": cp, "hook": hp,
            "text_de": f"{cp['text_de']} · {hp['text_de']}"}


def summary(p):
    """The decision without the drawing parameters: what the render result and upload.json carry."""
    c, h = p["captions"], p["hook"]
    return {"layout": p["layout"],
            "captions": {k: c[k] for k in ("position", "size", "look", "reason", "position_source", "size_source",
                                           "look_source", "span", "scores")},
            "hook": {"size": h["size"], "seconds": h["seconds"], "span": h["reserve"]},
            "face_guard": p["face_guard"], "frames": p["frames"], "text_de": p["text_de"]}
