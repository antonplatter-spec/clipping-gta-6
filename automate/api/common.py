"""Shared pieces of the REST layer: the error type every route raises, body validation with German per-field
messages, lazy access to the modules other agents own, and the "something changed" hook.

    ApiError(status, code, message, *, fields=None, headers=None, **extra)   -> {"error": {code, message, ...}}
    @public                       marks a handler that needs no session (the guard in api/__init__.py checks it)
    not_found(message=None) ; conflict(code, message) ; invalid(fields, message=None)
    Form(body)                    reads a JSON object field by field; .check() raises a 422 with every bad field
    optional(name) -> module|None automate.<name> or None when it can't be imported (pipeline, health, ...)
    need(name) -> module          automate.<name> or ApiError 503 (German) when it can't be imported
    mapped()                      context manager: PipelineError / PickCheckFailed / YouTubeError / BridgeError /
                                  agent errors -> ApiError (German message, fitting status)
    changed(keys, overlay=True)   runtime.materialize() + pipeline.on_settings_changed(keys) (best effort)
    page(limit, offset)           validated pagination
"""
import importlib
import logging
import math
import re
from contextlib import contextmanager

from .. import runtime

log = logging.getLogger("automate.api")

MAX_LIMIT = 200


class ApiError(Exception):
    def __init__(self, status, code, message, *, fields=None, headers=None, **extra):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.fields, self.headers, self.extra = fields, headers, extra

    def body(self):
        err = {"code": self.code, "message": self.message}
        if self.fields:
            err["fields"] = self.fields
        err.update(self.extra)
        return {"error": err}


def public(handler):
    handler.public_route = True
    return handler


def not_found(message=None):
    return ApiError(404, "not_found", message or "Nicht gefunden.")


def conflict(code, message):
    return ApiError(409, code, message)


def invalid(fields, message=None):
    return ApiError(422, "invalid", message or "Bitte prüfe die Eingaben.", fields=fields)


class Form:
    """A JSON object read field by field. Missing optional fields return the default; every problem is collected
    (German, per field) and `check()` raises them together as one 422."""

    def __init__(self, body):
        self.body = body if isinstance(body, dict) else {}
        self.errors = {}

    def has(self, name):
        return name in self.body

    def error(self, name, message):
        self.errors.setdefault(name, message)

    def check(self, message=None):
        if self.errors:
            raise invalid(self.errors, message)
        return self

    def str(self, name, *, required=False, default=None, max_len=200, strip=True, nullable=False, label=None):
        v = self.body.get(name)
        if v is None:
            if required:
                self.error(name, f"{label or 'Dieses Feld'} fehlt.")
            return None if nullable and name in self.body else default
        if not isinstance(v, str):
            self.error(name, "Muss Text sein.")
            return default
        v = v.strip() if strip else v
        if required and not v:
            self.error(name, f"{label or 'Dieses Feld'} darf nicht leer sein.")
        if len(v) > max_len:
            self.error(name, f"Höchstens {max_len} Zeichen.")
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in v):
            self.error(name, "Enthält Steuerzeichen.")
        return v

    def bool(self, name, *, required=False, default=None):
        v = self.body.get(name)
        if v is None:
            if required:
                self.error(name, "Dieses Feld fehlt.")
            return default
        if not isinstance(v, bool):
            self.error(name, "Muss an oder aus sein.")
            return default
        return v

    def int(self, name, *, required=False, default=None, min=None, max=None, nullable=False):
        v = self.body.get(name)
        if v is None:
            if required:
                self.error(name, "Dieses Feld fehlt.")
            return None if nullable and name in self.body else default
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if not isinstance(v, int) or isinstance(v, bool):
            self.error(name, "Muss eine ganze Zahl sein.")
            return default
        if (min is not None and v < min) or (max is not None and v > max):
            self.error(name, f"Muss zwischen {min} und {max} liegen.")
        return v

    def number(self, name, *, default=None, min=None, max=None):
        v = self.body.get(name)
        if v is None:
            return default
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            self.error(name, "Muss eine Zahl sein.")
            return default
        if (min is not None and v < min) or (max is not None and v > max):
            self.error(name, f"Muss zwischen {min:g} und {max:g} liegen.")
        return v

    def choice(self, name, choices, *, required=False, default=None, labels=None):
        v = self.body.get(name)
        if v is None:
            if required:
                self.error(name, "Bitte auswählen.")
            return default
        if v not in choices:
            self.error(name, f"Muss eins von {', '.join(labels or map(str, choices))} sein.")
            return default
        return v

    def str_list(self, name, *, max_items=50, max_len=500, default=None):
        v = self.body.get(name)
        if v is None:
            return default
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            self.error(name, "Muss eine Liste von Texten sein.")
            return default
        if len(v) > max_items:
            self.error(name, f"Höchstens {max_items} Einträge.")
        if any(len(x) > max_len for x in v):
            self.error(name, f"Ein Eintrag ist zu lang (höchstens {max_len} Zeichen).")
        return v

    def id_list(self, name, *, max_items=200):
        v = self.body.get(name)
        if not isinstance(v, list) or not v or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
            self.error(name, "Muss eine Liste von IDs sein.")
            return []
        if len(v) > max_items:
            self.error(name, f"Höchstens {max_items} auf einmal.")
        return list(dict.fromkeys(v))


def page(limit, offset, default=50):
    errors = {}
    limit = default if limit is None else limit
    offset = 0 if offset is None else offset
    if not 0 <= limit <= MAX_LIMIT:
        errors["limit"] = f"Muss zwischen 0 und {MAX_LIMIT} liegen."
    if offset < 0:
        errors["offset"] = "Darf nicht negativ sein."
    if errors:
        raise invalid(errors)
    return limit, offset


def http_url(value):
    """True for an absolute http(s) URL with a host (what yt-dlp and the dashboard links can use)."""
    return bool(re.match(r"^https?://[^\s/?#]+\.[^\s/?#]+(/\S*)?$|^https?://localhost(:\d+)?(/\S*)?$", value or "", re.I))


# ---------------------------------------------------------------- modules of the other agents

def optional(name):
    try:
        return importlib.import_module(f"automate.{name}")
    except Exception:  # missing or broken while it's being written: callers degrade
        log.debug("automate.%s not importable", name, exc_info=True)
        return None


def need(name):
    try:
        return importlib.import_module(f"automate.{name}")
    except Exception as e:
        log.warning("automate.%s not importable: %s", name, e)
        raise ApiError(503, "unavailable", f"Dieser Teil des Servers ist noch nicht verfügbar (automate.{name} fehlt "
                                           f"oder hat einen Fehler). Details im Server-Log.") from None


def _names(exc):
    return {c.__name__ for c in type(exc).__mro__}


def map_exception(e):
    """An ApiError for the known failure types of the other modules, else None."""
    names = _names(e)
    msg = str(e) or e.__class__.__name__
    if "PickCheckFailed" in names:
        return ApiError(int(getattr(e, "status", 422) or 422), getattr(e, "code", None) or "check_failed", msg,
                        check_report=getattr(e, "report", None))
    if "PipelineError" in names:
        status = int(getattr(e, "status", 409) or 409)
        return ApiError(status if 400 <= status < 600 else 409, getattr(e, "code", None) or "pipeline_error", msg,
                        **({"fields": e.fields} if isinstance(getattr(e, "fields", None), dict) else {}))
    if "YouTubeError" in names:
        code = getattr(e, "code", "youtube_error")
        status = {"not_found": 404, "not_connected": 409, "youtube_app_missing": 409, "invalid": 422}.get(code, 502)
        return ApiError(status, code, msg)
    if "BridgeError" in names:
        if getattr(e, "timed_out", False):
            return ApiError(504, "timeout", "Der Hilfsprozess hat zu lange gebraucht. Bitte erneut versuchen.")
        return ApiError(502, "bridge_error", f"Der Hilfsprozess ist fehlgeschlagen: {msg[:300]}")
    if names & {"MissingApiKey"}:
        return ApiError(409, "key_missing", msg)
    if names & {"BudgetExceeded"}:
        return ApiError(409, "budget_reached", msg)
    if names & {"AgentError"}:
        return ApiError(502, "agent_error", msg)
    return None


@contextmanager
def mapped():
    try:
        yield
    except ApiError:
        raise
    except Exception as e:
        err = map_exception(e)
        if err is None:
            raise
        raise err from e


def changed(keys, overlay=True):
    """After a settings / secret / source change: rewrite the clipper overlay and let the pipeline react (wake
    waiting jobs, re-read the schedule). Never fails the request that made the change."""
    if overlay:
        try:
            runtime.materialize()
        except Exception:
            log.exception("materialize after %s failed", keys)
    pipeline = optional("pipeline")
    if pipeline is not None and hasattr(pipeline, "on_settings_changed"):
        try:
            pipeline.on_settings_changed(list(keys))
        except Exception:
            log.exception("pipeline.on_settings_changed(%s) failed", keys)
