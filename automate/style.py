"""The renderer's caption, hook and sound vocabulary, as the server needs it to validate picks and settings. Mirrored
from clipper.placement, clipper.captions.LOOKS, clipper.sound, clipper.music and clipper.render because the server
never imports clipper; tests/test_automate_style.py fails when the two drift apart.

    KEYS                  the six pick keys, each also a same-named default in watchlist.yaml (the pick wins)
    CHOICES[key]          what each key takes (hook_seconds also takes 1..HOOK_SECONDS_MAX seconds)
    valid(key, value) -> bool
    LOOKS {look id: caption_style family} ; LOOK_DE (clipper's own German look names) ; family_looks(style)
    look_mismatch(look, caption_style) -> the look's family when it doesn't belong to that style, else None
    SOUND_ROLES, MOODS, FX_TYPES, FLASH_COLOURS ; valid_colour(value)
    DEFAULTS               what each key falls back to without any setting ; CLASSIC: the settings that bring back
                          the look from before these keys
    config_errors(overrides) -> {key: German message}   watchlist / profile overrides that set these keys (plus
                          caption_looks and caption_style), for the settings forms and settings.validate_overrides

Semantics the dashboard explains (clipper.placement.setting, clipper.sound.level_of): a key missing from a pick uses
the config default; a pick's "auto" decides per clip even when the config says otherwise. A named look as the config
default applies to every clip whatever its caption_style (only a pick's own look must match its style). `fx` missing
= the render scores the clip itself (zooms on shouted lines, sound on its epic beats up to sound_level);
`"fx": []` = no effects; `"music": []` = no music and no automatic sound at all.
"""
import re

KEYS = ("caption_position", "caption_size", "caption_look", "hook_size", "hook_seconds", "sound_level")
CAPTION_STYLES = ("bold", "boxed", "comic")
LOOKS = {"bold": "bold", "bold-word": "bold", "bold-lime": "bold", "bold-clean": "bold",
         "boxed": "boxed", "boxed-word": "boxed", "boxed-lime": "boxed",
         "comic": "comic", "comic-word": "comic", "comic-orange": "comic"}
LOOK_DE = {"bold": "Montserrat, 3 Wörter, Gelb", "bold-word": "Montserrat, ein Wort",
           "bold-lime": "Montserrat, 2 Wörter, Lime", "bold-clean": "TikTok Sans, normale Schreibung, Lila",
           "boxed": "Bebas auf Schwarz, 3 Wörter, Gelb", "boxed-word": "Bebas auf Schwarz, ein Wort",
           "boxed-lime": "Bebas auf Schwarz, 2 Wörter, Lime", "comic": "Comic, 3 Wörter, Gelb",
           "comic-word": "Comic, ein Wort", "comic-orange": "Comic, 2 Wörter, Orange"}
HOOK_SECONDS_MAX = 30
CHOICES = {"caption_position": ("auto", "high", "seam", "low", "classic"),
           "caption_size": ("auto", "small", "medium", "large"),
           "caption_look": ("auto", "classic", *LOOKS),
           "hook_size": ("auto", "small", "medium", "large"),
           "hook_seconds": ("auto", "whole"),
           "sound_level": ("off", "subtle", "normal", "strong")}
SOUND_ROLES = ("impact", "riser", "whoosh", "tension")
MOODS = ("suspense", "sneaky", "epic", "sad", "funny")
FX_TYPES = ("flash", "zoom", "hit")
FLASH_COLOURS = ("lime", "white", "red", "orange")
DEFAULTS = {"caption_position": "auto", "caption_size": "auto", "caption_look": "auto", "hook_size": "auto",
            "hook_seconds": "auto", "sound_level": "normal"}  # without any setting (clipper's own fallbacks)
CLASSIC = {"caption_position": "classic", "caption_size": "large", "caption_look": "classic", "hook_size": "large",
           "hook_seconds": "whole", "sound_level": "off"}
KEY_DE = {"caption_position": "Untertitel-Position", "caption_size": "Untertitel-Größe",
          "caption_look": "Untertitel-Look", "hook_size": "Hook-Größe", "hook_seconds": "Hook-Anzeigedauer",
          "sound_level": "Sound-Stärke", "caption_looks": "Looks für „Automatisch“", "caption_style": "Untertitel-Stil"}


def _seconds(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= HOOK_SECONDS_MAX


def valid(key, value):
    if key == "hook_seconds" and _seconds(value):
        return True
    return isinstance(value, str) and value in CHOICES[key]


def family_looks(style):
    return [k for k, fam in LOOKS.items() if fam == style]


def look_mismatch(look, caption_style):
    """The look's own family when a named look doesn't belong to `caption_style` (clipper check fails that)."""
    fam = LOOKS.get(look)
    return fam if fam and caption_style in CAPTION_STYLES and fam != caption_style else None


def valid_colour(value):
    return isinstance(value, str) and (value.strip().lower() in FLASH_COLOURS
                                       or re.fullmatch(r"(?:#|0x)?[0-9a-fA-F]{6}", value.strip()) is not None)


def allowed_de(key):
    if key == "hook_seconds":
        return f"auto, whole oder 1–{HOOK_SECONDS_MAX} Sekunden"
    return ", ".join(CHOICES[key])


def config_errors(overrides):
    """{key: German message} for the style keys a watchlist or profile override sets to something the renderer would
    ignore (it falls back to auto silently, so the owner would never know)."""
    out = {}
    if not isinstance(overrides, dict):
        return out
    for key in KEYS:
        if key in overrides and not valid(key, overrides[key]):
            out[key] = f"{KEY_DE[key]}: muss eins von {allowed_de(key)} sein."
    if "caption_style" in overrides and overrides["caption_style"] not in CAPTION_STYLES:
        out["caption_style"] = f"{KEY_DE['caption_style']}: muss eins von {', '.join(CAPTION_STYLES)} sein."
    if "caption_looks" in overrides:
        v = overrides["caption_looks"]
        if not isinstance(v, list) or not all(isinstance(x, str) and x in LOOKS for x in v):
            out["caption_looks"] = f"{KEY_DE['caption_looks']}: eine Liste aus {', '.join(LOOKS)}."
        elif not v:
            out["caption_looks"] = (f"{KEY_DE['caption_looks']}: mindestens einen Look wählen (sonst nimmt "
                                    "„Automatisch“ immer den Grundlook des Stils).")
    return out
