"""Social-media accounts and the YouTube connection around them (the REST layer in automate/api/social.py stays thin).

Accounts are records the owner marks posts against ("als gepostet markieren"); YouTube accounts can also be
connected through OAuth (automate/youtube.py) for direct uploads. Credentials (OAuth tokens) never leave this module
and youtube.py: `public(account)` is the only shape the API returns.

    PLATFORMS                                   {id: German label}
    public(account) -> dict                     {id, platform, label, handle, profile, connected, external_id, notes}
    youtube_connected() -> bool                 any YouTube account with tokens
    youtube_app(redirect_uri) -> {client_id_set, client_secret_set, client_id_hint, redirect_uri, redirect_note}
    new_state(session_hash, *, account_id, return_origin, redirect_uri) -> state   (random, single-use, STATE_TTL_S)
    take_state(state) -> entry | None           consumes it (a second call returns None)
    connect_url(session_hash, *, account_id, return_origin, redirect_uri) -> Google consent URL
    finish_connect(entry, code) -> account row  exchanges the code, reads the channel, stores the tokens
    disconnect(account_id) -> account row       revokes (best effort) and forgets the tokens

The OAuth `state` lives in this process only (lost on restart: the owner just clicks "verbinden" again). It is bound
to the dashboard session that asked for it, but the callback does not need that session's cookie: Google sends the
browser to http://localhost:<port>/..., which may not be the host the owner browses on (127.0.0.1, a LAN name), and
a SameSite=Strict cookie is not sent on that cross-site redirect anyway. The state itself (256 random bits, used
once, 10 minutes) is the proof; the callback still checks that the session that created it is alive.
"""
import hashlib
import secrets
import threading
import time
from urllib.parse import urlsplit

from . import security, store, youtube

PLATFORMS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "x": "X", "facebook": "Facebook",
             "other": "Andere"}
PUBLIC_KEYS = ("id", "platform", "label", "handle", "profile", "connected", "external_id", "notes")
STATE_TTL_S = 600
MAX_STATES = 20

_states = {}
_states_lock = threading.Lock()


def public(account):
    out = {k: account.get(k) for k in PUBLIC_KEYS}
    out["connected"] = bool(account.get("connected")) and account.get("platform") == "youtube"
    return out


def youtube_connected():
    return any(a["connected"] and a.get("has_credentials") for a in store.list_accounts("youtube"))


def redirect_note(redirect_uri):
    host = urlsplit(redirect_uri).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return (f"Google schickt dich nach dem Erlauben zu {redirect_uri} zurück – also zu diesem PC selbst. Starte "
                f"„Mit YouTube verbinden“ deshalb im Browser auf dem PC, auf dem der Clipper läuft (nicht vom Handy). "
                f"Ob du das Dashboard dort über localhost, 127.0.0.1 oder den Rechnernamen geöffnet hast, ist egal: "
                f"Der Server erkennt die Rückkehr am einmaligen Code und schickt dich danach zur Adresse zurück, von "
                f"der du gestartet bist. Der Vorgang muss innerhalb von {STATE_TTL_S // 60} Minuten abgeschlossen "
                f"sein.")
    return (f"Google schickt dich nach dem Erlauben zu {redirect_uri} zurück. Diese Adresse muss genau so im "
            f"Google-OAuth-Client eingetragen und von deinem Browser aus erreichbar sein. Der Vorgang muss innerhalb "
            f"von {STATE_TTL_S // 60} Minuten abgeschlossen sein.")


def youtube_app(redirect_uri):
    cid = store.get_secret("youtube_client_id")
    return {"client_id_set": bool(cid), "client_secret_set": bool(store.get_secret("youtube_client_secret")),
            "client_id_hint": _hint(cid), "redirect_uri": redirect_uri, "redirect_note": redirect_note(redirect_uri)}


def _hint(client_id):
    """A recognisable but useless form of the client id: its first digits and the Google suffix."""
    if not client_id:
        return None
    head, _, tail = client_id.partition("-")
    return f"{head[:6]}…{'.apps.googleusercontent.com' if client_id.endswith('.apps.googleusercontent.com') else ''}" \
        if tail else security.mask(client_id)


def _key(state):
    return hashlib.sha256(state.encode("utf-8", "replace")).hexdigest()


def new_state(session_hash, *, account_id, return_origin, redirect_uri):
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _states_lock:
        for k in [k for k, v in _states.items() if v["expires_at"] <= now]:
            del _states[k]
        while len(_states) >= MAX_STATES:
            del _states[min(_states, key=lambda k: _states[k]["expires_at"])]
        _states[_key(state)] = {"session_hash": session_hash, "account_id": account_id,
                                "return_origin": return_origin, "redirect_uri": redirect_uri,
                                "expires_at": now + STATE_TTL_S}
    return state


def take_state(state):
    if not isinstance(state, str) or not 20 <= len(state) <= 200:
        return None
    with _states_lock:
        entry = _states.pop(_key(state), None)
    if entry is None or entry["expires_at"] <= time.time():
        return None
    return entry


def connect_url(session_hash, *, account_id, return_origin, redirect_uri):
    cid, _ = youtube._client()
    state = new_state(session_hash, account_id=account_id, return_origin=return_origin, redirect_uri=redirect_uri)
    return youtube.auth_url(cid, redirect_uri, state)


def finish_connect(entry, code):
    """Tokens from `code`, the channel they belong to, stored on the requested account (or a matching / new one)."""
    if store.get_session(entry["session_hash"]) is None:
        raise youtube.YouTubeError("Deine Dashboard-Sitzung ist inzwischen abgelaufen. Bitte anmelden und erneut "
                                   "verbinden.", code="session_expired")
    creds = youtube.exchange_code(code, entry["redirect_uri"])
    channel = youtube.fetch_channel(creds["access_token"])
    target = store.get_account(entry["account_id"]) if entry.get("account_id") else None
    if target is None:
        target = next((a for a in store.list_accounts("youtube") if a.get("external_id") == channel["id"]), None)
    fields = {"connected": True, "external_id": channel["id"], "credentials": creds}
    if target is None:
        aid = store.add_account("youtube", channel["title"] or "YouTube", handle=channel["handle"], **fields)
    else:
        aid = target["id"]
        if not target.get("handle") and channel["handle"]:
            fields["handle"] = channel["handle"]
        store.update_account(aid, **fields)
    store.resolve_alert(f"youtube.disconnected.{aid}")
    return store.get_account(aid)


def disconnect(account_id):
    account = store.get_account(account_id, with_credentials=True)
    if account is None:
        return None
    if account.get("credentials"):
        youtube.revoke(account["credentials"])
    store.update_account(account_id, connected=False, credentials=None)
    store.resolve_alert(f"youtube.disconnected.{account_id}")
    return store.get_account(account_id)
