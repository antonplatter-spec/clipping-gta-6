import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
# Fonts the top GTA Shorts use (research/viral-gta-shorts.md#fonts): name -> (file, variable-font instance)
FONTS = {
    "montserrat-black": ("Montserrat.ttf", "Black"),       # MrBossFTW hooks (51M views)
    "bebas": ("BebasNeue-Regular.ttf", None),              # Kai Cenat Live word captions (6.8M)
    "tiktok-bold": ("TikTokSans.ttf", "Bold"),             # RDCworld1 meme box (5M)
    "tiktok-semibold": ("TikTokSans.ttf", "SemiBold"),     # DarkViperAU labels / captions
    "luckiest": ("LuckiestGuy-Regular.ttf", None),         # Typical Gamer comic captions
}
SYSTEM_FALLBACK = ["/System/Library/Fonts/Supplemental/Arial Black.ttf",
                   "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]

CAPTION_STYLES = {
    "bold": {"font": "montserrat-black", "size": 84, "stroke": 8, "box": False},
    "boxed": {"font": "bebas", "size": 116, "stroke": 0, "box": True},
    "comic": {"font": "luckiest", "size": 96, "stroke": 11, "box": False},
}
# Each extra speaker gets its own colour and a different font so crosstalk is easy to follow.
SPEAKER_COLOURS = [((255, 255, 255), (255, 225, 60)), ((110, 215, 255), (255, 255, 255)),
                   ((255, 140, 205), (255, 255, 255)), ((150, 255, 140), (255, 255, 255))]
SPEAKER_FONTS = ["bebas", "luckiest", "tiktok-bold", "montserrat-black"]
FONT_SIZES = {"montserrat-black": 84, "bebas": 116, "luckiest": 96, "tiktok-bold": 90, "tiktok-semibold": 90}

HOOK_STYLES = {
    "bold": {"font": "montserrat-black", "size": 88, "upper": True, "fill": (255, 255, 255),
             "keyword": (255, 225, 60), "stroke": 3, "shadow": True, "box": False},
    "box": {"font": "tiktok-bold", "size": 66, "upper": False, "fill": (15, 15, 15),
            "keyword": (30, 120, 255), "stroke": 0, "shadow": False, "box": True},
    "label": {"font": "tiktok-semibold", "size": 70, "upper": False, "fill": (255, 255, 255),
              "keyword": (200, 90, 255), "stroke": 5, "shadow": False, "box": False},
}
# The pre-adaptive positions ("classic"): "fit" = 16:9 game centred; "split" = facecam top 768px, game below.
# In split the hook sits on the seam so it never covers the streamer's face. clipper.placement decides per clip.
LAYOUTS = {"fit": {"hook_y": 400, "caption_y": 1420}, "split": {"hook_y": 768, "caption_y": 1380}}

WHITE, YELLOW, LIME, PURPLE, ORANGE = (255, 255, 255), (255, 225, 60), (158, 255, 0), (200, 90, 255), (255, 150, 30)
# Caption looks: a family per caption_style, each built only from what research/viral-gta-shorts.md#fonts and the
# Zylok survey show (one word at a time on 25/25 Zylok Shorts, lime action labels, Kai Cenat's 1-word Bebas on black,
# DarkViperAU's TikTok Sans with a purple keyword). The look named like its family is the classic one (the only
# look before looks varied). size = px at caption_size "large"; words = words per chunk; base/active/label = colour
# of a word, of the word being spoken, of an action label; needs "calm": thin outline, only over calm, darker footage.
LOOKS = {
    "bold": {"family": "bold", "font": "montserrat-black", "size": 84, "stroke": 8, "box": False, "words": 3,
             "upper": True, "base": WHITE, "active": YELLOW, "label": YELLOW},
    "bold-word": {"family": "bold", "font": "montserrat-black", "size": 84, "stroke": 8, "box": False, "words": 1,
                  "upper": True, "base": WHITE, "active": WHITE, "label": LIME},
    "bold-lime": {"family": "bold", "font": "montserrat-black", "size": 84, "stroke": 8, "box": False, "words": 2,
                  "upper": True, "base": WHITE, "active": LIME, "label": LIME},
    "bold-clean": {"family": "bold", "font": "tiktok-semibold", "size": 84, "stroke": 5, "box": False, "words": 3,
                   "upper": False, "base": WHITE, "active": PURPLE, "label": PURPLE, "shadow": True, "needs": "calm"},
    "boxed": {"family": "boxed", "font": "bebas", "size": 116, "stroke": 0, "box": True, "words": 3,
              "upper": True, "base": WHITE, "active": YELLOW, "label": YELLOW},
    "boxed-word": {"family": "boxed", "font": "bebas", "size": 116, "stroke": 0, "box": True, "words": 1,
                   "upper": True, "base": WHITE, "active": WHITE, "label": YELLOW},
    "boxed-lime": {"family": "boxed", "font": "bebas", "size": 116, "stroke": 0, "box": True, "words": 2,
                   "upper": True, "base": WHITE, "active": LIME, "label": LIME},
    "comic": {"family": "comic", "font": "luckiest", "size": 96, "stroke": 11, "box": False, "words": 3,
              "upper": True, "base": WHITE, "active": YELLOW, "label": YELLOW},
    "comic-word": {"family": "comic", "font": "luckiest", "size": 96, "stroke": 11, "box": False, "words": 1,
                   "upper": True, "base": WHITE, "active": WHITE, "label": LIME},
    "comic-orange": {"family": "comic", "font": "luckiest", "size": 96, "stroke": 11, "box": False, "words": 2,
                     "upper": True, "base": WHITE, "active": ORANGE, "label": ORANGE},
}
LOOK_DE = {"bold": "Montserrat, 3 Wörter, Gelb", "bold-word": "Montserrat, ein Wort", "bold-lime": "Montserrat, 2 Wörter, Lime",
           "bold-clean": "TikTok Sans, normale Schreibung, Lila", "boxed": "Bebas auf Schwarz, 3 Wörter, Gelb",
           "boxed-word": "Bebas auf Schwarz, ein Wort", "boxed-lime": "Bebas auf Schwarz, 2 Wörter, Lime",
           "comic": "Comic, 3 Wörter, Gelb", "comic-word": "Comic, ein Wort", "comic-orange": "Comic, 2 Wörter, Orange"}
_fonts = {}


def font(name, size):
    if (name, size) not in _fonts:
        file, instance = FONTS.get(name, FONTS["montserrat-black"])
        path = FONT_DIR / file
        if path.exists():
            f = ImageFont.truetype(str(path), size)
            if instance:
                f.set_variation_by_name(instance)
        else:
            f = next((ImageFont.truetype(p, size) for p in SYSTEM_FALLBACK if Path(p).exists()),
                     ImageFont.load_default(size))
        _fonts[(name, size)] = f
    return _fonts[(name, size)]


def covers(f, text):
    """Whether a font draws every character of text: a missing glyph renders as the font's .notdef box."""
    def shape(c):
        m = f.getmask(c)
        return m.size, bytes(m)
    missing = shape("\ue000")  # private use: no font draws it
    return all(c.isspace() or shape(c) != missing for c in set(text))


def font_for(name, size, text):
    """The style's font, or the nearest bundled one that can draw the text (Bebas and Luckiest Guy have no
    Cyrillic, Greek, ...), scaled to the same visual size; else the system fallback."""
    f = font(name, size)
    if covers(f, text):
        return f
    for alt in ("montserrat-black", "tiktok-bold"):
        g = font(alt, round(size * FONT_SIZES[alt] / FONT_SIZES.get(name, size)))
        if covers(g, text):
            return g
    return next((ImageFont.truetype(p, size) for p in SYSTEM_FALLBACK if Path(p).exists()), f)


# Whole words only (so "class" or "Dickens" stay); the vowel is masked like Zylok's captions: F*CK, SH*T, @SSHOLE
SWEARS = re.compile(r"^((mother)?f+u+c+k+\w*|f+u+k+|sh+i+t+\w*|bull ?shit|b+i+t+c+h+\w*|ass(hole|holes|es)?|"
                    r"dumbass|jackass|bastards?|dick(s|head)?|puss(y|ies)|cunts?|whores?|sluts?|damn(it)?|goddamn)$")
DROPPED = re.compile(r"^n+[i1]+g+(a|ah|as|az|er|ers|guh|gas)$")  # slurs leave the captions entirely


def censor(word):
    """'fucking!' -> 'F*CKING!' (case kept), 'asshole' -> '@SSHOLE'; a slur -> None; anything else unchanged."""
    m = re.match(r"^([^\w]*)([\w']+)([^\w]*)$", word or "")
    if not m:
        return word
    pre, body, post = m.groups()
    low = body.lower().replace("'", "")
    if DROPPED.match(low):
        return None
    if not SWEARS.match(low):
        return word
    stem = re.search(r"(?i)f(u)+c?k|sh(i)+t|b(i)+tch|d(i)ck|p(u)ss|c(u)nt|wh(o)re|sl(u)t|d(a)mn|b(a)stard|(a)ss",
                     body)
    if stem:
        i = next(stem.start(g) for g in range(1, stem.re.groups + 1) if stem.group(g))
        masked = body[:i] + ("@" if body[i:i + 3].lower() == "ass" else "*") + body[i + 1:]
    else:
        i = next((i for i, c in enumerate(body) if i and c.lower() in "aeiou"), None)
        masked = body if i is None else body[:i] + "*" + body[i + 1:]
    return pre + masked + post


def censor_words(words):
    """Caption words with swears masked and slurs dropped (labels are left as written)."""
    out = []
    for w in words:
        if w.get("label"):
            out.append(w)
            continue
        c = censor(str(w["w"]))
        if c:
            out.append(dict(w, w=c))
    return out


def apply_fixes(words, fixes):
    """Replace misheard words, whole words and any case: {"swerver": "Trevor"} (punctuation around it stays)."""
    table = {str(k).lower(): str(v) for k, v in (fixes or {}).items() if str(k).strip()}
    if not table:
        return words
    out = []
    for w in words:
        m = re.match(r"^([^\w]*)([\w']+)([^\w]*)$", str(w["w"]))
        if m and m.group(2).lower() in table and not w.get("label"):
            w = dict(w, w=m.group(1) + table[m.group(2).lower()] + m.group(3))
        out.append(w)
    return out


def chunk_words(words, max_words=3, max_gap=0.45):
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (len(cur) >= max_words or nxt is None or nxt["s"] - w["e"] > max_gap
                or w["w"].endswith((".", ",", "?", "!")) or w.get("label") or nxt.get("label")):
            chunks.append(cur)
            cur = []
    return chunks


def timeline(words, clip_start, clip_end, linger=0.5, max_words=3):
    """(t0, t1, chunk, active_index, chunk_words) per word, relative to the clip start."""
    inside = [w for w in words if w["s"] >= clip_start and w["e"] <= clip_end + 0.3]
    chunks = chunk_words(inside, max_words)
    dur = clip_end - clip_start
    out = []
    for ci, ch in enumerate(chunks):
        nxt_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else None
        ch_end = min(ch[-1]["e"] + linger, nxt_start) if nxt_start else ch[-1]["e"] + linger
        for wi, w in enumerate(ch):
            t0 = w["s"] - clip_start
            t1 = (ch[wi + 1]["s"] if wi + 1 < len(ch) else ch_end) - clip_start
            t0, t1 = max(0.0, t0), min(dur, t1)
            if t1 - t0 >= 0.04:
                out.append((round(t0, 3), round(t1, 3), ci, wi, [x["w"] for x in ch]))
    return out


def caption_states(words, clip_start, clip_end, max_words=3):
    """[(t0, t1, ((speaker, words, active), ...))]: what's on screen when; overlapping speakers stack."""
    by_spk = {}
    for w in words:
        by_spk.setdefault(w.get("spk", 0), []).append(w)
    tls = {spk: timeline(ws, clip_start, clip_end, max_words=max_words) for spk, ws in by_spk.items()}
    dur = round(clip_end - clip_start, 3)
    cuts = sorted({0.0, dur} | {t for tl in tls.values() for e in tl for t in e[:2]})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 0.02:
            continue
        mid = (a + b) / 2
        state = tuple((spk, tuple(e[4]), e[3]) for spk, tl in sorted(tls.items())
                      for e in tl if e[0] <= mid < e[1])
        if out and out[-1][2] == state:
            out[-1] = (out[-1][0], b, state)
        else:
            out.append((a, b, state))
    return out


def _wrap(words, f, max_w):
    lines, cur = [], []
    for i, w in enumerate(words):
        test = " ".join(x for _, x in cur + [(i, w)])
        if cur and f.getlength(test) > max_w:
            lines.append(cur)
            cur = []
        cur.append((i, w))
    if cur:
        lines.append(cur)
    return lines


def speaker_look(spk, style, multi):
    """(font name, size, base colour, active colour) for a speaker."""
    st = CAPTION_STYLES.get(style, CAPTION_STYLES["bold"])
    base, active = SPEAKER_COLOURS[spk % len(SPEAKER_COLOURS)] if multi else SPEAKER_COLOURS[0]
    if not multi or spk == 0:
        return st["font"], st["size"], base, active
    others = [f for f in SPEAKER_FONTS if f != st["font"]]
    name = others[(spk - 1) % len(others)]
    return name, FONT_SIZES[name], base, active


def classic_caption_plan(layout="fit", style="bold"):
    """The fixed placement captions had before clipper.placement: the style's own look and size, centred on the
    layout's caption line, other speakers stacked above, no safe-zone limits."""
    return {"look": style if style in LOOKS else "bold", "scale": 1.0, "anchor": "centre", "stack": "up",
            "y": LAYOUTS.get(layout, LAYOUTS["fit"])["caption_y"], "top": 0, "bottom": H, "max_width": 960}


def classic_hook_plan(layout="fit"):
    return {"scale": 1.0, "centre": LAYOUTS.get(layout, LAYOUTS["fit"])["hook_y"], "top": 0, "bottom": H,
            "max_width": 900, "box": [50, W - 50]}


def _speaker_style(look, spk, multi, scale):
    """(font name, px, base colour, active colour) for a speaker: speaker 0 wears the look, every other speaker keeps
    its own colour and a different font so crosstalk is easy to follow (one-word looks colour the word itself)."""
    lk = LOOKS[look]
    if not multi or spk == 0:
        return lk["font"], lk["size"] * scale, lk["base"], lk["active"]
    colour = SPEAKER_COLOURS[spk % len(SPEAKER_COLOURS)][0]
    name = [f for f in SPEAKER_FONTS if f != lk["font"]][(spk - 1) % (len(SPEAKER_FONTS) - 1)]
    return name, FONT_SIZES[name] * scale, colour, colour if lk["words"] == 1 else WHITE


def _shown(word, upper):
    w = word.upper() if upper else word
    return w.strip(",.") or w


def state_blocks(state, plan, multi, shrink=1.0):
    """How each speaker's chunk sets in the plan's look and size: font, wrapped lines and height. A word wider than
    the plan's max_width shrinks that speaker's font until it fits (long words never leave the safe area)."""
    lk = LOOKS[plan["look"]]
    out = []
    for spk, words, active in state:
        name, px, base, act = _speaker_style(plan["look"], spk, multi, plan["scale"] * shrink)
        shown = [_shown(w, lk["upper"]) for w in words]
        size = max(12, round(px))
        f = font_for(name, size, " ".join(shown))
        widest = max((f.getlength(w) for w in shown), default=0)
        if widest > plan["max_width"]:
            size = max(12, int(size * plan["max_width"] / widest))
            f = font_for(name, size, " ".join(shown))
        lines = _wrap(shown, f, plan["max_width"])
        lh = int(size * 1.18)
        out.append({"spk": spk, "font": f, "size": size, "lines": lines, "lh": lh, "h": lh * len(lines),
                    "active": active, "base": base, "act": act, "words": words})
    return out


def place_blocks(blocks, plan):
    """Top y of each speaker block: speaker 0 at the plan's anchor, the others stacked toward plan['stack'], the
    whole stack moved inside [top, bottom]. Returns (tops, total height)."""
    if not blocks:
        return [], 0
    gap = round(28 * plan["scale"])
    h0, y = blocks[0]["h"], plan["y"]
    tops = [y - h0 // 2 if plan["anchor"] == "centre" else y if plan["anchor"] == "top" else y - h0]
    for b in blocks[1:]:
        prev = tops[-1]
        tops.append(prev - gap - b["h"] if plan["stack"] == "up" else prev + blocks[len(tops) - 1]["h"] + gap)
    lo, hi = min(tops), max(t + b["h"] for t, b in zip(tops, blocks))
    shift = plan["bottom"] - hi if hi > plan["bottom"] else 0
    if lo + shift < plan["top"]:
        shift = plan["top"] - lo
    return [t + shift for t in tops], hi - lo


def layout_state(state, plan, multi):
    """(blocks, tops): blocks as state_blocks, tops as place_blocks; a stack taller than the plan's room (four
    speakers at once) is set smaller until it fits."""
    shrink = 1.0
    for _ in range(4):
        blocks = state_blocks(state, plan, multi, shrink)
        tops, total = place_blocks(blocks, plan)
        room = plan["bottom"] - plan["top"]
        if total <= room:
            break
        shrink *= room / total * 0.97
    return blocks, tops


def render_state(state, path, plan, multi, labels=()):
    """One caption PNG (full canvas, transparent) for what's on screen: `plan` from clipper.placement (or
    classic_caption_plan), `labels` the action-label texts (drawn in the look's label colour)."""
    lk = LOOKS[plan["look"]]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    blocks, tops = layout_state(state, plan, multi)
    k = plan["scale"]
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0)) if lk.get("shadow") and blocks else None
    d = ImageDraw.Draw(img)
    sd = ImageDraw.Draw(shadow) if shadow else None
    stroke = max(2, round(lk["stroke"] * k)) if lk["stroke"] else 0
    for b, y in zip(blocks, tops):
        f, space = b["font"], b["font"].getlength(" ")
        for line in b["lines"]:
            line_w = f.getlength(" ".join(w for _, w in line))
            x = (W - line_w) / 2
            if lk["box"]:
                asc, desc = f.getmetrics()
                d.rectangle((x - round(18 * k), y - round(6 * k), x + line_w + round(18 * k), y + asc + desc - round(4 * k)),
                            fill=(0, 0, 0, 235))
            for i, w in line:
                colour = lk["label"] if b["words"][i] in labels else b["act"] if i == b["active"] else b["base"]
                kw = {"stroke_width": stroke, "stroke_fill": (0, 0, 0)} if stroke else {}
                if sd:
                    sd.text((x + round(4 * k), y + round(5 * k)), w, font=f, fill=(0, 0, 0, 210), **kw)
                d.text((x, y), w, font=f, fill=colour, **kw)
                x += f.getlength(w) + space
            y += b["lh"]
    if shadow:
        top, bottom = max(0, min(tops) - 40), min(H, max(t + b["h"] for t, b in zip(tops, blocks)) + 60)
        region = shadow.crop((0, top, W, bottom)).filter(ImageFilter.GaussianBlur(max(2, round(4 * k))))
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow.paste(region, (0, top))
        img = Image.alpha_composite(shadow, img)
    img.save(path)
    return [(t, t + b["h"]) for t, b in zip(tops, blocks)]


def parse_markup(text):
    """'I Can Play *GTA 6 NOW*' -> [(word, highlighted)]."""
    out, on = [], False
    for tok in text.split():
        body = tok.rstrip("?!.,;:…")  # "*THIS*?": the marker closes before trailing punctuation
        if body.startswith("*"):
            on = True
        clean = body.strip("*") + tok[len(body):]
        if clean:
            out.append((clean, on))
        if body.endswith("*") and (len(body) > 1 or not body.startswith("*")):
            on = False
    return out


def hook_layout(text, style="box", scale=1.0, max_width=900):
    """How a hook banner sets at this scale: {font, tokens, lines, lh, pad, h} (h = the banner's height), or None
    when there's no banner (no text, or hook_style none). At most 3 lines, as always."""
    st = HOOK_STYLES.get(style)
    if st is None or not (text or "").strip():
        return None
    tokens = parse_markup(text)
    words = [w.upper() if st["upper"] else w for w, _ in tokens]
    size = max(12, round(st["size"] * scale))
    f = font_for(st["font"], size, " ".join(words))
    widest = max((f.getlength(w) for w in words), default=0)
    if widest > max_width:
        size = max(12, int(size * max_width / widest))
        f = font_for(st["font"], size, " ".join(words))
    lines = _wrap(words, f, max_width)[:3]
    lh = int(size * 1.2)
    pad = round(25 * scale)
    return {"font": f, "tokens": tokens, "lines": lines, "lh": lh, "pad": pad, "h": lh * len(lines) + 2 * pad,
            "size": size}


def hook_top(plan, h):
    """Top y of a banner h px tall: centred on the plan's line, kept inside [top, bottom]."""
    return int(min(max(plan["centre"] - h // 2, plan["top"]), plan["bottom"] - h))


def render_hook(text, path, style="box", layout="fit", plan=None):
    """The hook banner PNG (full canvas, transparent). `plan` from clipper.placement; None = the classic place.
    Returns the banner's (top, bottom), or None for no banner."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    plan = plan or classic_hook_plan(layout)
    st = HOOK_STYLES.get(style)
    hl = hook_layout(text, style, plan["scale"], plan["max_width"])
    if hl is None:
        img.save(path)
        return None
    k, f, tokens = plan["scale"], hl["font"], hl["tokens"]
    top = hook_top(plan, hl["h"])
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    if st["box"]:
        d.rounded_rectangle((plan["box"][0], top, plan["box"][1], top + hl["h"]), radius=round(30 * k),
                            fill=(255, 255, 255, 248))
    y = top + hl["pad"]
    space = f.getlength(" ")
    stroke = max(1, round(st["stroke"] * k)) if st["stroke"] else 0
    for line in hl["lines"]:
        x = (W - f.getlength(" ".join(w for _, w in line))) / 2
        for i, w in line:
            colour = st["keyword"] if tokens[i][1] else st["fill"]
            kw = {"stroke_width": stroke, "stroke_fill": (0, 0, 0)} if stroke else {}
            d.text((x, y), w, font=f, fill=colour, **kw)
            if st["shadow"]:
                sd.text((x + round(5 * k), y + round(7 * k)), w, font=f, fill=(0, 0, 0, 200))
            x += f.getlength(w) + space
        y += hl["lh"]
    if st["shadow"]:
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(max(2, round(5 * k)))))
    Image.alpha_composite(img, layer).save(path)
    return top, top + hl["h"]


def write_overlay(words, clip_start, clip_end, out_dir, layout="fit", style="bold", censored=True, plan=None):
    """Render caption PNGs and an FFmpeg concat list that shows each for as long as it's on screen. `plan` (from
    clipper.placement) sets the look, size and place; None keeps the classic place and the style's own look."""
    if censored:
        words = censor_words(words)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = plan or classic_caption_plan(layout, style)
    labels = {w["w"] for w in words if w.get("label")}
    multi = len({w.get("spk", 0) for w in words if clip_start <= w["s"] <= clip_end}) > 1
    cache, entries = {}, []
    for a, b, state in caption_states(words, clip_start, clip_end, LOOKS[plan["look"]]["words"]):
        if state not in cache:
            png = out_dir / f"cap_{len(cache):04d}.png"
            render_state(state, png, plan, multi, labels)
            cache[state] = png
        entries.append((cache[state], b - a))
    lines = ["ffconcat version 1.0"]
    for png, dur in entries:
        lines += [f"file '{png.name}'", f"duration {dur:.3f}"]
    lines.append(f"file '{entries[-1][0].name}'")
    lst = out_dir / "captions.txt"
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lst
