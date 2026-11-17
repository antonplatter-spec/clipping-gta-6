"""What Claude costs: price table lookup, usage -> USD/EUR, today's spend and the daily budget.

Prices come from the `claude.prices` setting (USD per 1M tokens; `cache_write` is the 5-minute TTL write, a 1-hour
write costs 2x input). "Today" is the local day of this PC (`local_day()`), the same day string store.add_usage writes.

    local_day(ts=None) -> "YYYY-MM-DD"
    price_row(model, prices=None) -> (row, known)   exact id, else the longest price key the id starts with plus "-"
                                                     (a dated snapshot); the same in settings.DEFAULT_PRICES; else
                                                     the claude-sonnet-5-5 row with known=False
    normalize_usage(usage) -> {input, output, cache_write_5m, cache_write_1h, cache_read}
                              accepts an SDK Usage object or a dict with the API's names (input_tokens,
                              cache_creation_input_tokens, cache_read_input_tokens, cache_creation:
                              {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}) or the DB's
                              (cache_creation_tokens, cache_read_tokens, cache_creation_1h_tokens)
    estimate(model, usage, prices=None) -> {usd, known_model}
    cost_usd(model, usage, prices=None) -> float
    to_eur(usd, rate=None) -> float                  rate default: setting claude.usd_to_eur
    spent_today_eur() -> float
    budget_left_eur() -> float | None                None = no limit (claude.daily_limit_eur == 0)
    budget_ok() -> bool                              False once today's spend reached the limit
"""
import logging
import time
from datetime import datetime

from . import settings

log = logging.getLogger("automate.costs")
FALLBACK_MODEL = "claude-sonnet-5-5"
_warned = set()


def local_day(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts).strftime("%Y-%m-%d")


def _prices(prices):
    if prices is not None:
        return prices
    from . import store
    return store.get_setting("claude.prices")


def _lookup(model, prices):
    if model in prices:
        return prices[model]
    prefixed = [k for k in prices if model.startswith(k + "-")]
    return prices[max(prefixed, key=len)] if prefixed else None


def price_row(model, prices=None):
    prices = _prices(prices)
    model = model or ""
    # the defaults also cover a model the owner's stored table predates (e.g. a refusal fallback's)
    row = _lookup(model, prices) or _lookup(model, settings.DEFAULT_PRICES)
    if row is not None:
        return row, True
    row = prices.get(FALLBACK_MODEL) or settings.DEFAULT_PRICES[FALLBACK_MODEL]
    if model not in _warned:
        _warned.add(model)
        log.warning("no price for model %r: costed at the %s rate", model, FALLBACK_MODEL)
    return row, False


def _get(usage, name):
    v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return v


def normalize_usage(usage):
    def n(*names):
        for name in names:
            v = _get(usage, name)
            if v is not None:
                return int(v)
        return 0
    write = n("cache_creation_input_tokens", "cache_creation_tokens")
    split = _get(usage, "cache_creation")
    w1h = n("cache_creation_1h_tokens")
    if split is not None:
        w1h = int(_get(split, "ephemeral_1h_input_tokens") or 0)
        w5m = _get(split, "ephemeral_5m_input_tokens")
        write = max(write, int(w5m or 0) + w1h)
    return {"input": n("input_tokens"), "output": n("output_tokens"), "cache_write_5m": max(0, write - w1h),
            "cache_write_1h": w1h, "cache_read": n("cache_read_input_tokens", "cache_read_tokens")}


def estimate(model, usage, prices=None):
    row, known = price_row(model, prices)
    u = normalize_usage(usage)
    usd = (u["input"] * row["input"] + u["output"] * row["output"] + u["cache_write_5m"] * row["cache_write"]
           + u["cache_write_1h"] * 2 * row["input"] + u["cache_read"] * row["cache_read"]) / 1e6
    return {"usd": round(usd, 6), "known_model": known}


def cost_usd(model, usage, prices=None):
    return estimate(model, usage, prices)["usd"]


def to_eur(usd, rate=None):
    if rate is None:
        from . import store
        rate = store.get_setting("claude.usd_to_eur")
    return round(float(usd) * float(rate), 6)


def spent_today_eur():
    from . import store
    return store.usage_today()["eur"]


def budget_left_eur():
    from . import store
    limit = float(store.get_setting("claude.daily_limit_eur") or 0)
    if limit <= 0:
        return None
    return max(0.0, round(limit - spent_today_eur(), 6))


def budget_ok():
    left = budget_left_eur()
    return left is None or left > 0
