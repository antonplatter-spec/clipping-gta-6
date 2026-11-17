"""Settings and secrets registry: every key the dashboard can change, its default, type, limits and German texts.

Pure module (no DB): store.py uses it to fill defaults and to validate every write, and the API serves `schema()` so
the dashboard renders its forms from it (format = `SettingSchema` in automate/web/js/api.js).

    REGISTRY: {key: Setting}  (display order)   SECRETS: {name: {label, help, env, prefix?}}
    GROUPS: {group id: German label}
    default(key) -> value (a deep copy; KeyError for an unknown key) ; defaults() -> {key: value}
    schema() -> {key: {type, default, label, help, group, group_label, choices?: [{value, label, help?}], min?, max?,
                       step?, unit?, auto?, hidden?, advanced?, nullable?}}
    validate(changes: dict) -> dict     normalized values; raises SettingsError({key: German message}) on any bad value
    validate_secret(name, value) -> str normalized; raises SettingsError({name: message})
    validate_overrides(overrides, base=None, what="...") -> dict   (watchlist / profile.yaml overrides; ValueError)

Types: "boolean"; "integer" (with `auto`: the string "auto" too); "number" (int or float; "0,9" accepted);
"choice" (one of the choices' values); "time_list" (["HH:MM", ...], normalized to sorted, unique, zero-padded);
"string_list" (choices are suggestions; dub.auto_languages takes language codes like "es", "pt-BR"); "text"
(multi-line; `nullable`: blank means null = "use the repo file"); "string" (ui.default_profile: a folder in
profiles/); "json" (claude.prices: {model: {input, output, cache_read, cache_write}} USD per 1M tokens;
watchlist.overrides: a mapping merged over watchlist.yaml).
"""
import copy
import re
from dataclasses import dataclass

import yaml

from . import paths


class SettingsError(ValueError):
    """Invalid values; `errors` maps each bad key to a German message for the dashboard."""

    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


@dataclass(frozen=True)
class Setting:
    type: str
    default: object
    label: str
    help: str
    group: str
    choices: tuple = ()  # (value, label) or (value, label, help)
    min: float = None
    max: float = None
    step: float = None
    unit: str = None
    auto: bool = False
    hidden: bool = False  # edited on its own page, not in the generic forms
    advanced: bool = False
    nullable: bool = False
    check: str = None  # extra validation: "lang" | "profile" | "prices" | "overrides"


GROUPS = {
    "automation": "Zeitplan & Automatik",
    "clips": "Clips",
    "approval": "Freigabe",
    "claude": "Claude & Kosten",
    "research": "Recherche & Prompt",
    "analysis": "Analyse",
    "ui": "Oberfläche",
}

# The selectable models, plus the ones a refusal fallback ("fallbacks": "default") can route a request to: their
# usage is logged under the model that served it and must not be costed at the Sonnet 5.5 rate. A row added here
# reaches databases that already store their own claude.prices once, at startup (importer.run).
DEFAULT_PRICES = {
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
}
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
LANGUAGES = (("es", "Spanisch"), ("ru", "Russisch"), ("pt", "Portugiesisch"), ("de", "Deutsch"), ("fr", "Französisch"),
             ("it", "Italienisch"), ("pl", "Polnisch"), ("tr", "Türkisch"), ("ar", "Arabisch"), ("hi", "Hindi"),
             ("ja", "Japanisch"), ("ko", "Koreanisch"), ("zh", "Chinesisch"))

S = Setting
REGISTRY = {
    "automation.enabled": S(
        "boolean", False, "Automatik eingeschaltet",
        "Hauptschalter: Wenn an, sucht der Server zu den Suchzeiten nach neuen GTA-6-Videos deiner Creator und clippt "
        "sie ohne Rückfragen.", "automation"),
    "automation.live_enabled": S(
        "boolean", False, "Live-Aufnahme eingeschaltet",
        "Prüft die Live-Streams regelmäßig, nimmt GTA-6-Streams in Stücken auf und clippt jedes fertige Stück.",
        "automation"),
    "schedule.scan_times": S(
        "time_list", ["09:00"], "Suchzeiten",
        "Uhrzeiten (Ortszeit dieses PCs), zu denen täglich nach neuen Videos gesucht wird, z. B. 09:00 und 18:00.",
        "automation", min=1, max=24),
    "schedule.live_watch_minutes": S(
        "integer", 5, "Live-Check alle", "Wie oft geprüft wird, ob ein Stream live ist und GTA 6 spielt.",
        "automation", min=1, max=60, unit="Min."),
    "schedule.live_chunk_minutes": S(
        "integer", 30, "Länge eines Aufnahme-Stücks",
        "Jedes fertige Stück wird sofort analysiert und geclippt. Gilt für neu gestartete Aufnahmen.", "automation",
        min=5, max=120, unit="Min."),
    "schedule.health_minutes": S(
        "integer", 5, "Systemprüfung alle",
        "Prüft Aufnahmen, Speicherplatz, Werkzeuge, Schlüssel und Budget und meldet Probleme in der Übersicht.",
        "automation", min=1, max=60, unit="Min."),
    "worker.concurrency": S(
        "integer", 1, "Parallele Jobs",
        "Wie viele schwere Jobs (Laden, Analyse, Auswahl) gleichzeitig laufen. Rendern läuft immer einzeln.",
        "automation", min=1, max=8, advanced=True),
    "approval.mode": S(
        "choice", "manual", "Freigabe", "Ob Claudes Auswahl auf dich wartet oder sofort gerendert wird.", "approval",
        choices=(("manual", "Manuell", "Jeder Clip wartet in der Freigabe-Queue, bis du ihn freigibst."),
                 ("auto", "Automatisch", "Claudes Auswahl wird sofort gerendert."))),
    "prefs.voice": S(
        "choice", "auto", "Voiceover", "Ob die Clips eine Erzählstimme bekommen.", "clips",
        choices=(("off", "Aus", "Nie eine Erzählstimme."), ("all", "Immer", "Jeder Clip bekommt eine Erzählstimme."),
                 ("auto", "Auto", "Claude entscheidet pro Video, z. B. bei Info-Clips ohne Sprecher."))),
    "prefs.videos_per_run": S(
        "integer", "auto", "Videos pro Durchlauf",
        "Auto: so viele, wie die Analyse für lohnend hält (höchstens das Maximum unten), oder eine feste Zahl.",
        "clips", min=1, max=20, auto=True),
    "prefs.max_videos_per_run": S("integer", 10, "Höchstens Videos pro Durchlauf", "Obergrenze für „Auto“.", "clips",
                                  min=1, max=30),
    "prefs.hooks_per_video": S(
        "integer", 5, "Hooks pro Video",
        "Wie viele Hook-Varianten (je ein MP4) pro Clip behalten werden. Auto: die Zahl, die die Strategie-Simulation "
        "empfiehlt.", "clips", min=1, max=8, auto=True),
    "prefs.sources_per_day": S("integer", 2, "Neue Quellvideos pro Tag",
                               "Wie viele neue Uploads eine tägliche Suche höchstens analysiert.", "clips",
                               min=1, max=10),
    "prefs.daily_max_minutes": S(
        "integer", 90, "Maximal analysierte Länge pro Video",
        "Von langen Streams wird nur der Anfang bis zu dieser Länge geladen und analysiert.", "clips",
        min=10, max=600, unit="Min."),
    "render.top_hooks": S("integer", 0, "Nur die besten Hooks rendern",
                          "0 = ein MP4 pro Hook. Sonst nur die N am besten bewerteten Hooks pro Clip.", "clips",
                          min=0, max=8),
    "render.allow_system_voice": S(
        "boolean", True, "Systemstimme als Ersatz",
        "Ohne Fish-Audio-Schlüssel wird das Voiceover mit der Systemstimme gesprochen statt den Clip anzuhalten.",
        "clips"),
    "dub.auto_languages": S(
        "string_list", [], "Automatisch synchronisieren in",
        "Sprachcodes wie es oder ru: Jedes fertige Video wird automatisch in diese Sprachen synchronisiert (braucht "
        "den Fish-Audio-Schlüssel).", "clips", choices=LANGUAGES, max=10, check="lang"),
    "claude.model": S(
        "choice", "claude-sonnet-5-5", "Claude-Modell", "Welches Modell die Momente auswählt.", "claude",
        choices=(("claude-sonnet-5-5", "Claude Sonnet 5.5", "Schnell und günstig, trifft gute Entscheidungen."),
                 ("claude-opus-5-5", "Claude Opus 5.5", "Urteilt gründlicher, kostet etwa das Doppelte."))),
    "claude.effort": S(
        "choice", "medium", "Denkaufwand", "Wie gründlich Claude nachdenkt. Höher = bessere Urteile, mehr Tokens.",
        "claude", choices=(("low", "Niedrig"), ("medium", "Mittel"), ("high", "Hoch"))),
    "claude.max_turns": S("integer", 40, "Höchstens Schritte pro Auswahl",
                          "Nach so vielen Anfragen an Claude bricht eine Auswahl ab.", "claude", min=1, max=200,
                          advanced=True),
    "claude.max_fix_rounds": S("integer", 3, "Höchstens Korrekturrunden",
                               "Wie oft Claude Picks nachbessern darf, die die Qualitätsprüfung nicht bestehen.",
                               "claude", min=0, max=10, advanced=True),
    "claude.daily_limit_eur": S(
        "number", 5.0, "Tageslimit",
        "Ist das Limit erreicht, warten neue Claude-Anfragen bis zum nächsten Tag. 0 = kein Limit.", "claude",
        min=0, max=10000, step=0.5, unit="€"),
    "claude.usd_to_eur": S(
        "number", 0.92, "Wechselkurs USD → EUR",
        "Claude rechnet in US-Dollar ab; mit diesem Kurs werden die Kosten in Euro angezeigt.", "claude",
        min=0.01, max=10, step=0.01),
    "claude.prices": S(
        "json", DEFAULT_PRICES, "Preise (USD pro 1 Mio. Tokens)",
        "Eingabe, Ausgabe, Cache-Lesen und Cache-Schreiben (5 Minuten) je Modell. Nur ändern, wenn Anthropic die "
        "Preise ändert.", "claude", advanced=True, check="prices"),
    "claude.extra_instructions": S(
        "text", "", "Zusätzliche Anweisungen an Claude",
        "Wird an den System-Prompt angehängt, z. B. „Bevorzuge Comedy-Clips“.", "research", max=20000, hidden=True),
    "research.override": S(
        "text", None, "Eigenes Playbook",
        "Ersetzt research/viral-gta-shorts.md im System-Prompt. Leer = das Playbook aus dem Repository.", "research",
        max=500000, hidden=True, nullable=True),
    "watchlist.overrides": S(
        "json", {}, "Analyse-Einstellungen",
        "Änderungen gegenüber watchlist.yaml (Signal-Gewichte, Hype-Wörter, Cliplängen, Live-Limits). Nicht genannte "
        "Werte kommen aus der Datei im Repository.", "analysis", hidden=True, check="overrides"),
    "ui.default_profile": S("string", "gta6", "Standard-Profil", "Das Profil, das das Dashboard vorauswählt.", "ui",
                            hidden=True, check="profile"),
}
del S

SECRETS = {
    "anthropic_api_key": {"label": "Anthropic-API-Schlüssel", "env": "ANTHROPIC_API_KEY", "prefix": "sk-ant-",
                          "help": "Für Claudes Auswahl der Momente. Erstellen unter console.anthropic.com → "
                                  "API Keys (beginnt mit „sk-ant-“)."},
    "fish_api_key": {"label": "Fish-Audio-API-Schlüssel", "env": "FISH_API_KEY",
                     "help": "Optional: für Voiceover und Synchronisation in der geklonten Stimme. Erstellen unter "
                             "fish.audio → API Keys."},
    "youtube_client_id": {"label": "YouTube OAuth-Client-ID", "env": None,
                          "help": "Aus deinem Google-Cloud-Projekt (OAuth-Client vom Typ Webanwendung)."},
    "youtube_client_secret": {"label": "YouTube OAuth-Clientschlüssel", "env": None,
                              "help": "Gehört zur Client-ID oben."},
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][\w.-]*$")


def default(key):
    return copy.deepcopy(REGISTRY[key].default)


def defaults():
    return {k: default(k) for k in REGISTRY}


def schema():
    out = {}
    for key, s in REGISTRY.items():
        entry = {"type": s.type, "default": default(key), "label": s.label, "help": s.help, "group": s.group,
                 "group_label": GROUPS[s.group]}
        if s.choices:
            entry["choices"] = [{"value": c[0], "label": c[1], **({"help": c[2]} if len(c) > 2 else {})}
                                for c in s.choices]
        for name in ("min", "max", "step", "unit"):
            if getattr(s, name) is not None:
                entry[name] = getattr(s, name)
        for name in ("auto", "hidden", "advanced", "nullable"):
            if getattr(s, name):
                entry[name] = True
        out[key] = entry
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v):
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _range(v, s):
    if s.min is not None and v < s.min or s.max is not None and v > s.max:
        lo = "" if s.min is None else f"{s.min:g}"
        hi = "" if s.max is None else f"{s.max:g}"
        raise ValueError(f"muss zwischen {lo} und {hi} liegen" if lo and hi else
                         f"muss mindestens {lo} sein" if lo else f"darf höchstens {hi} sein")
    return v


def _check(key, value):
    s = REGISTRY[key]
    t = s.type
    if t == "boolean":
        if not isinstance(value, bool):
            raise ValueError("muss an oder aus sein")
        return value
    if t == "integer":
        if s.auto and value == "auto":
            return value
        v = _int(value)
        if v is None:
            raise ValueError(f"muss „auto“ oder eine Zahl von {s.min:g} bis {s.max:g} sein" if s.auto else
                             "muss eine ganze Zahl sein")
        return _range(v, s)
    if t == "number":
        if isinstance(value, str):
            try:
                value = float(value.replace(",", "."))
            except ValueError:
                pass
        if not _num(value) or value != value:
            raise ValueError("muss eine Zahl sein")
        return _range(float(value), s)
    if t == "choice":
        if value not in [c[0] for c in s.choices]:
            raise ValueError(f"muss eins von {', '.join(c[1] for c in s.choices)} sein")
        return value
    if t == "time_list":
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
            raise ValueError("muss eine Liste von Uhrzeiten (HH:MM) sein")
        items = [("0" + x if re.fullmatch(r"\d:\d\d", x) else x) for x in (x.strip() for x in items)]
        bad = [x for x in items if not TIME_RE.match(x)]
        if bad:
            raise ValueError(f"ungültige Uhrzeit: {', '.join(bad)} (Format HH:MM, z. B. 09:00)")
        items = sorted(set(items))
        if not s.min <= len(items) <= s.max:
            raise ValueError(f"braucht {s.min:g} bis {s.max:g} Uhrzeiten")
        return items
    if t == "string_list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("muss eine Liste sein")
        items = list(dict.fromkeys(x.strip() for x in value if x.strip()))
        bad = [x for x in items if s.check == "lang" and not LANG_RE.match(x)]
        if bad:
            raise ValueError(f"ungültiger Sprachcode: {', '.join(bad)} (z. B. es, ru, pt-BR)")
        if s.max is not None and len(items) > s.max:
            raise ValueError(f"höchstens {s.max:g} Einträge")
        return items
    if t == "text":
        if value is None or (isinstance(value, str) and not value.strip()):
            return None if s.nullable else ""
        if not isinstance(value, str):
            raise ValueError("muss Text sein")
        if s.max is not None and len(value) > s.max:
            raise ValueError(f"ist zu lang (höchstens {s.max:g} Zeichen)")
        return value
    if t == "string":  # ui.default_profile
        if not isinstance(value, str) or not PROFILE_RE.match(value.strip()):
            raise ValueError("muss ein Profilname sein (Buchstaben, Ziffern, - _ .)")
        name = value.strip()
        if not (paths.repo_profiles_dir() / name).is_dir():
            raise ValueError(f"kein Profil „{name}“ im Ordner profiles/")
        return name
    if s.check == "prices":
        return _prices(value)
    if s.check == "overrides":
        return validate_overrides(value, _repo_watchlist(), "watchlist.yaml")
    raise AssertionError(f"no validation for {key}")


def _prices(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("braucht mindestens ein Modell mit Preisen")
    out = {}
    for model, row in value.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(row, dict):
            raise ValueError("je Modell: {input, output, cache_read, cache_write}")
        missing = [f for f in PRICE_FIELDS if f not in row]
        if missing:
            raise ValueError(f"{model}: es fehlt {', '.join(missing)}")
        clean = {}
        for f in PRICE_FIELDS:
            if not _num(row[f]) or row[f] < 0 or row[f] != row[f]:
                raise ValueError(f"{model}.{f} muss eine Zahl ≥ 0 sein")
            clean[f] = float(row[f])
        out[model.strip()] = clean
    return out


def _repo_watchlist():
    try:
        return yaml.safe_load(paths.repo_watchlist().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def validate_overrides(overrides, base=None, what="der Datei"):
    """A mapping to merge over a YAML config (one level per key, like clipper.config.load): string keys, values
    YAML can store, and a mapping wherever the base has a mapping (e.g. `weights`), else the clipper would crash."""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("muss eine Zuordnung Schlüssel → Wert sein")
    base = base or {}
    for k, v in overrides.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError("jeder Schlüssel muss ein Name sein")
        if isinstance(base.get(k), dict) and not isinstance(v, dict):
            raise ValueError(f"„{k}“ ist in {what} eine Gruppe von Werten; hier muss auch eine Gruppe stehen")
    try:
        yaml.safe_dump(overrides)
    except yaml.YAMLError as e:
        raise ValueError(f"enthält Werte, die nicht gespeichert werden können ({e})") from None
    return copy.deepcopy(overrides)


def validate(changes):
    """{key: value} -> the same keys with normalized values; raises SettingsError listing every bad key."""
    if not isinstance(changes, dict):
        raise SettingsError({"_": "Erwartet eine Zuordnung Schlüssel → Wert"})
    out, errors = {}, {}
    for key, value in changes.items():
        if key not in REGISTRY:
            errors[key] = "unbekannte Einstellung"
            continue
        try:
            out[key] = _check(key, value)
        except ValueError as e:
            errors[key] = f"{REGISTRY[key].label}: {e}"
    if errors:
        raise SettingsError(errors)
    return out


def validate_secret(name, value):
    if name not in SECRETS:
        raise SettingsError({name: "unbekannter Schlüssel"})
    v = (value or "").strip() if isinstance(value, str) else None
    if not v:
        raise SettingsError({name: f"{SECRETS[name]['label']}: darf nicht leer sein"})
    if len(v) > 4096 or any(ch.isspace() for ch in v):
        raise SettingsError({name: f"{SECRETS[name]['label']}: enthält Leerzeichen oder ist zu lang – bitte nur den "
                                   f"Schlüssel einfügen"})
    prefix = SECRETS[name].get("prefix")
    if prefix and not v.startswith(prefix):
        raise SettingsError({name: f"{SECRETS[name]['label']}: muss mit „{prefix}“ beginnen"})
    return v
