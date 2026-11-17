"""First-run import of the repo's YAML (and .env keys) into the DB, so the dashboard starts with everything the CLI
setup already had. Safe to run on every start.

    run(force=False, env_file=None) -> {"profiles": [added], "creators": {profile: n}, "streams": {profile: n},
                                        "settings": [keys], "secrets": [names], "skipped": [German notes]}

Never overwrites DB state:
  - profiles: a row for every folder in profiles/ that has none (gta6 enabled, the others disabled);
  - creators / streams of a profile: from profiles/<p>/creators.yaml / live.yaml (gta6 falls back to the old
    root-level files like the CLI) once per profile (marker meta "import.creators.<p>" / "import.streams.<p>"), and
    only while the DB has none for that profile; a profile folder that appears later is imported on the next start,
    and creators the owner deleted are not brought back. File order becomes insertion order (= the CLI's order);
  - prefs.yaml -> prefs.* settings and schedule_time -> schedule.scan_times, once, only keys without a stored value;
  - .env (default ROOT/.env) -> secrets fish_api_key / anthropic_api_key and setting render.top_hooks
    (CLIPPER_TOP_HOOKS), once, only when unset there;
  - price rows a release adds to settings.DEFAULT_PRICES -> a stored claude.prices, once per row (meta
    "prices.defaults" lists the rows already offered): rows the owner edited stay, a row the owner deleted after it
    was offered stays deleted.
`force=True` ignores the markers but still only adds what's missing (import-yaml command).
"""
from pathlib import Path

import yaml

from . import paths, settings, store

DEFAULT_PROFILE = "gta6"
PREF_KEYS = ("voice", "videos_per_run", "max_videos_per_run", "hooks_per_video", "sources_per_day", "daily_max_minutes")
ENV_SECRETS = {"FISH_API_KEY": "fish_api_key", "ANTHROPIC_API_KEY": "anthropic_api_key"}


def _yaml(path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _repo_file(profile, name):
    """profiles/<p>/<name>; the default profile falls back to the pre-profile file at the repo root (like the CLI)."""
    f = paths.repo_profiles_dir() / profile / name
    if not f.exists() and profile == DEFAULT_PROFILE and (paths.ROOT / name).exists():
        return paths.ROOT / name
    return f


def _prefs_file():
    f = paths.repo_profiles_dir() / DEFAULT_PROFILE / "prefs.yaml"
    return f if f.exists() else paths.ROOT / "prefs.yaml"


def _read_dotenv(path):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def _import_list(profile, kind, force, summary):
    marker = f"import.{kind}.{profile}"
    have = store.list_creators(profile) if kind == "creators" else store.list_streams(profile)
    if not force and (store.get_meta(marker) or have):
        return
    f = _repo_file(profile, "creators.yaml" if kind == "creators" else "live.yaml")
    if not f.exists():
        return
    entries = _yaml(f).get("creators" if kind == "creators" else "streams") or []
    n = 0
    for e in entries:
        if not isinstance(e, dict) or not e.get("url"):
            continue
        try:
            if kind == "creators":
                store.add_creator(profile, e.get("name") or e["url"], e["url"], e.get("match") or "any")
            else:
                store.add_stream(profile, e.get("name") or e["url"], e["url"],
                                 bool(e.get("topic_only", e.get("gta_only", True))),
                                 priority=int(e.get("priority", 100)))
            n += 1
        except ValueError as err:  # a duplicate (force) or a bad entry: keep going
            if force:
                continue
            summary["skipped"].append(f"{f.name} ({profile}): {err}")
    store.set_meta(marker, {"file": paths.rel(f), "count": n})
    if n:
        summary[kind][profile] = n


def _import_prefs(force, summary):
    if store.get_meta("import.prefs") and not force:
        return
    f = _prefs_file()
    prefs = _yaml(f)
    stored = store.stored_setting_keys()
    wanted = {f"prefs.{k}": prefs[k] for k in PREF_KEYS if k in prefs}
    if prefs.get("schedule_time"):
        wanted["schedule.scan_times"] = [str(prefs["schedule_time"])]
    for key, value in wanted.items():
        if key in stored:
            continue
        try:
            store.set_settings({key: value}, only_unset=True)
            summary["settings"].append(key)
        except settings.SettingsError as e:
            summary["skipped"].append(f"{f.name}: {e.errors[key]}")
    store.set_meta("import.prefs", {"file": paths.rel(f) if f.exists() else None})


def _import_env(env_file, force, summary):
    if store.get_meta("import.env") and not force:
        return
    env = _read_dotenv(env_file)
    for var, name in ENV_SECRETS.items():
        if env.get(var) and not store.get_secret(name):
            try:
                store.set_secret(name, env[var])
                summary["secrets"].append(name)
            except settings.SettingsError as e:
                summary["skipped"].append(f".env {var}: {e.errors[name]}")
    if env.get("CLIPPER_TOP_HOOKS") and "render.top_hooks" not in store.stored_setting_keys():
        try:
            store.set_settings({"render.top_hooks": env["CLIPPER_TOP_HOOKS"]}, only_unset=True)
            summary["settings"].append("render.top_hooks")
        except settings.SettingsError as e:
            summary["skipped"].append(f".env CLIPPER_TOP_HOOKS: {e.errors['render.top_hooks']}")
    store.set_meta("import.env", {"file": paths.rel(env_file) if env_file.exists() else None})


def _new_price_rows(summary):
    offered = set(store.get_meta("prices.defaults") or [])
    new = [m for m in settings.DEFAULT_PRICES if m not in offered]
    if not new:
        return
    if "claude.prices" in store.stored_setting_keys():  # unset: the defaults apply as they are
        prices = store.get_setting("claude.prices")
        added = {m: settings.DEFAULT_PRICES[m] for m in new if m not in prices}
        if added:
            store.set_settings({"claude.prices": {**prices, **added}})
            summary["settings"].append("claude.prices")
    store.set_meta("prices.defaults", sorted(offered | set(new)))


def run(force=False, env_file=None):
    summary = {"profiles": [], "creators": {}, "streams": {}, "settings": [], "secrets": [], "skipped": []}
    repo = paths.repo_profiles_dir()
    names = sorted(p.name for p in repo.iterdir() if p.is_dir()) if repo.is_dir() else []
    for name in names:
        if store.ensure_profile(name, enabled=name == DEFAULT_PROFILE):
            summary["profiles"].append(name)
    for name in names:
        _import_list(name, "creators", force, summary)
        _import_list(name, "streams", force, summary)
    _import_prefs(force, summary)
    _import_env(Path(env_file) if env_file else paths.ROOT / ".env", force, summary)
    _new_price_rows(summary)
    return summary
