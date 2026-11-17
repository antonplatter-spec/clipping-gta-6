"""Kosten & Schlüssel: Claude spend (today, per day, per purpose, recent requests) and the API keys.

Keys are write-only: the API returns `set`, the masked form (first 4 + "…" + last 4) and when it was saved, never
the key. A test result is kept in meta `keytest.<name>` and dropped when the key changes. The Anthropic key is tested
by agent.test_key; the Fish key by the bridge op `fish_key_check` (the same one-word request `clipper voice setup`
makes, run in a subprocess with the key in its environment, never on a command line).
"""
import time

from fastapi import APIRouter, Body, Query

from .. import costs, runtime, settings, store
from .common import ApiError, Form, changed, conflict, invalid, mapped, need, not_found

router = APIRouter()

KEYS = {"anthropic": ("anthropic_api_key", "Anthropic (Claude API)"), "fish": ("fish_api_key", "Fish Audio")}


def _key(name):
    if name not in KEYS:
        raise not_found("Diesen Schlüssel gibt es nicht (anthropic oder fish).")
    return KEYS[name]


def key_info(name):
    secret, label = _key(name)
    info = store.secret_info(secret)
    return {"name": name, "label": label, "set": info["set"], "masked": info["masked"] or None,
            "updated_at": info["updated_at"], "last_test": store.get_meta(f"keytest.{name}")}


@router.get("/costs")
def get_costs(days: int = Query(30)):
    if not 1 <= days <= 366:
        raise invalid({"days": "Zwischen 1 und 366 Tagen."})
    today = store.usage_today()
    return {"today": {k: today[k] for k in ("eur", "usd", "requests", "input_tokens", "output_tokens",
                                             "cache_read_tokens", "cache_creation_tokens")},
            "limit_eur": store.get_setting("claude.daily_limit_eur"),
            "selection_limit_eur": store.get_setting("claude.selection_limit_eur"),
            "reserved_eur": costs.reserved_eur(),
            "usd_to_eur": store.get_setting("claude.usd_to_eur"),
            "days": [{k: d[k] for k in ("day", "eur", "usd", "requests", "tokens")} for d in store.usage_by_day(days)],
            "by_purpose": store.usage_by_purpose(days), "recent": store.recent_usage(50)}


@router.get("/keys")
def list_keys():
    items = [key_info(n) for n in KEYS]
    return {"items": items, "total": len(items)}


@router.put("/keys/{name}")
def set_key(name: str, body: dict | None = Body(None)):
    secret, _ = _key(name)
    form = Form(body)
    value = form.str("value", required=True, max_len=4096, label="Der Schlüssel")
    form.check()
    try:
        store.set_secret(secret, value)
    except settings.SettingsError as e:
        raise invalid({"value": next(iter(e.errors.values()))}) from None
    store.set_meta(f"keytest.{name}", None)
    changed([f"secrets.{secret}"])
    return key_info(name)


@router.delete("/keys/{name}")
def delete_key(name: str):
    secret, _ = _key(name)
    store.set_secret(secret, None)
    store.set_meta(f"keytest.{name}", None)
    changed([f"secrets.{secret}"])
    return {"ok": True}


@router.post("/keys/{name}/test")
def test_key(name: str):
    secret, _ = _key(name)
    value = store.get_secret(secret)
    if not value:
        raise conflict("key_missing", "Es ist noch kein Schlüssel gespeichert.")
    with mapped():
        if name == "anthropic":
            ok, message = need("agent").test_key(value)
        else:
            res = runtime.run_bridge("fish_key_check", {}, store.get_setting("ui.default_profile"), timeout=90)
            ok, message = bool(res.get("ok")), str(res.get("message") or "")
    if not isinstance(message, str) or not message:
        raise ApiError(502, "test_failed", "Der Test hat kein Ergebnis geliefert.")
    result = {"ok": bool(ok), "message": message}
    store.set_meta(f"keytest.{name}", {**result, "at": time.time()})
    return result
