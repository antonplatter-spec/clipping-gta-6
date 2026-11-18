"""Instagram: connect a professional Instagram account and publish a rendered variant as a Reel through Meta's
official Graph API (no browser automation, no passwords). Stdlib HTTP only.

Meta's documentation (developers.facebook.com) could not be read from the machine this was written on; every host,
path, parameter and field name below comes from Meta's own Postman collection (github.com/fbsamples/messenger-
platform-samples, "Instagram Platform API"), search excerpts of Meta's pages and open-source clients, as of
2026-11-18. Each one is marked: [M] seen in Meta's sample collection or quoted from Meta's pages, [3] only from
third-party clients/guides, [?] assumed. Change them here and only here.

    GRAPH_VERSION = "v26.0"                                                      one constant for every versioned call
    Instagram Login ("Weg B", professional account without a Facebook Page)
      authorize   GET  https://www.instagram.com/oauth/authorize ?client_id&redirect_uri&response_type=code
                       &scope=instagram_business_basic,instagram_business_content_publish&state                [M]
      code        POST https://api.instagram.com/oauth/access_token  client_id, client_secret,
                       grant_type=authorization_code, redirect_uri, code -> {data: [{access_token, user_id,
                       permissions}]} (older shape: the same keys at the top level)                           [M]
      long-lived  GET  https://graph.instagram.com/access_token ?grant_type=ig_exchange_token&client_secret
                       &access_token -> {access_token, token_type, expires_in} (60 days)                      [M]
      refresh     GET  https://graph.instagram.com/refresh_access_token ?grant_type=ig_refresh_token
                       &access_token -> {access_token, token_type, expires_in}; the token must be at least
                       24 h old and not expired                                                               [M]
      me          GET  graph.instagram.com/<v>/me ?fields=user_id,username,account_type
                       (account_type BUSINESS | MEDIA_CREATOR; a personal account can't publish)             [M][?]
    Instagram API with Facebook Login ("Weg A", the account is linked to a Facebook Page)
      long-lived  GET  graph.facebook.com/<v>/oauth/access_token ?grant_type=fb_exchange_token&client_id
                       &client_secret&fb_exchange_token -> {access_token, expires_in}                          [M]
      grants      GET  graph.facebook.com/<v>/me/permissions -> {data: [{permission, status}]}                 [M]
      pages       GET  graph.facebook.com/<v>/me/accounts ?fields=id,name,access_token,
                       instagram_business_account{id,username} (a Page token made from a long-lived user
                       token does not expire)                                                                 [M]
      permissions instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement
                       (+ business_management for a Page in a business portfolio)                             [M]
    Publishing (both logins; host graph.instagram.com or graph.facebook.com)
      container   POST <host>/<v>/<ig-user-id>/media  media_type=REELS, upload_type=resumable, caption,
                       share_to_feed -> {id, uri}. Meta's guide says resumable uploads are "only for apps that
                       have implemented Facebook Login for Business"; with an Instagram Login token they are
                       tried anyway (Meta's own Instagram Login sample lists upload_type=resumable)           [M]
      bytes       POST https://rupload.facebook.com/ig-api-upload/<v>/<container-id>  headers
                       Authorization: OAuth <token>, offset: 0, file_size: <bytes>; body = the file           [M]
      status      GET  <host>/<v>/<container-id> ?fields=status_code,status  status_code EXPIRED | ERROR |
                       FINISHED | IN_PROGRESS | PUBLISHED; poll about once a minute; a container expires
                       after 24 h                                                                             [M]
      publish     POST <host>/<v>/<ig-user-id>/media_publish  creation_id -> {id: <media id>}                  [M]
      limit       GET  <host>/<v>/<ig-user-id>/content_publishing_limit ?fields=quota_usage,config
                       -> {data: [{quota_usage, config: {quota_total, quota_duration}}]} (100 per 24 h)   [M][3]
      media list  GET  <host>/<v>/<ig-user-id>/media ?fields=id,caption,timestamp,permalink                   [M][?]
      permalink   GET  <host>/<v>/<media-id> ?fields=permalink                                                [M]
      auth        Authorization: Bearer <token> on Graph calls (Meta's sample collection sets bearer auth);
                  the token endpoints above take the token as a query parameter (documented that way): those
                  URLs are built only inside this module and never logged or stored                          [M]
    Errors        {error: {message, type, code, error_subcode, is_transient, error_user_msg}}; code 190 = token
                  invalid/expired; 4, 17, 32, 613 = rate limits; 10, 200-299 = permission; subcodes 2207xxx
                  per Meta's error table (see SUBCODES)                                                   [M][3]
    Reels file    MP4/MOV, moov atom first, H.264 or HEVC, AAC <= 48 kHz mono/stereo, 23-60 fps, <= 1920 px wide,
                  aspect 0.01:1 - 10:1, 3 s - 15 min, <= 300 MB, <= 25 Mbps (see REELS)                  [M][3]
    Caption       <= 2200 characters, <= 30 hashtags, <= 20 @-mentions                                     [M][3]

Every HTTP request goes through `transport(method, url, headers=, body=, timeout=)` -> HttpResponse (no redirects;
4xx/5xx come back as responses, network failures raise OSError); tests replace it, and `_sleep` (the backoff).

Credentials (social_accounts.credentials; never returned by the API, never logged):
    {api: "instagram" | "facebook", access_token, ig_user_id, username, page_id?, page_name?, obtained_at,
     expires_at (None = no expiry known), expiry_known, refresh_after, refresh_tried_at, refreshed_at,
     refresh_error, checked_at}

    class InstagramError(Exception)   .retryable, .slow, .code; str(e) is German, fit for the dashboard
    token_kind(token) -> "instagram" | "facebook" | None
    connect_token(token, *, account_id=None) -> [account rows]     paste path (validates, stores)
    auth_url(app_id, redirect_uri, state) ; connect_oauth(code, redirect_uri, *, account_id=None) -> account row
    check_account(account_id) -> account row      validates the token now (and refreshes it when due)
    maintain(now=None) -> [{account_id, action}]  the health tick: refresh / check / alerts, never in a loop
    connection_info(account) -> {kind, username, expires_at, ...}    non-secret metadata for the dashboard
    caption(variant, video=None) -> str ; check_file(path) -> {problems: [German], ...}
    publish(variant_id, account_id, options, *, state=None, on_state=None, on_progress=None, cancel=None,
            log=None) -> {external_id, media_id, url, container_id}
        Persists each step through on_state(changes) the moment it happens (STATE_KEYS: container id and age,
        bytes uploaded, publish sent, media id). A later call with that state resumes: a publish that was sent is
        looked up first (container PUBLISHED -> the media is found by caption and time, never published twice);
        an unexpired container is reused; uploaded bytes are not sent again. `cancel` (threading.Event) counts
        until the publish request goes out; after that the call finishes and returns the Reel.
"""
import json
import logging
import random
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import NamedTuple

from . import paths, store

log = logging.getLogger("automate.instagram")

# ---------------------------------------------------------------- API facts (see the table above)

GRAPH_VERSION = "v26.0"
GRAPH = {"instagram": "https://graph.instagram.com", "facebook": "https://graph.facebook.com"}
RUPLOAD_URL = "https://rupload.facebook.com/ig-api-upload"
AUTH_URL = "https://www.instagram.com/oauth/authorize"
CODE_URL = "https://api.instagram.com/oauth/access_token"
IG_EXCHANGE_URL = "https://graph.instagram.com/access_token"
IG_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")
FB_REQUIRED = ("instagram_basic", "instagram_content_publish", "pages_show_list")
FB_PERMISSIONS = (*FB_REQUIRED, "pages_read_engagement")
PROFESSIONAL = ("BUSINESS", "MEDIA_CREATOR", "CREATOR")
CALLBACK_PATH = "/api/social/instagram/callback"
STATUS_FIELDS = "status_code,status"
RATE_CODES = {4, 17, 32, 613, 80001, 80002}
CAPTION_MAX = 2200
HASHTAGS_MAX = 30
MENTIONS_MAX = 20
REELS = {"min_s": 3.0, "max_s": 15 * 60.0, "max_bytes": 300 * 1000 * 1000, "max_width": 1920, "fps": (23.0, 60.0),
         "aspect": (0.01, 10.0), "max_rate": 48000, "max_channels": 2, "max_mbps": 25.0,
         "video": ("avc1", "avc3", "hvc1", "hev1"), "audio": ("mp4a",)}
YOUTUBE_ONLY_TAGS = ("#shorts", "#short", "#youtubeshorts", "#ytshorts", "#shortsfeed", "#shortvideo")

# ---------------------------------------------------------------- our own timing
TOKEN_LIFETIME_S = 60 * 86400   # a long-lived token's lifetime; a pasted token is at most this old-new
REFRESH_MIN_AGE_S = 25 * 3600   # Meta refreshes a token only once it is 24 h old
REFRESH_EVERY_S = 7 * 86400     # refreshed weekly: a PC that is off for weeks keeps a working token
REFRESH_RETRY_S = 6 * 3600      # after a failed refresh: never sooner than this (no loop)
EXPIRY_WARN_S = 5 * 86400       # alert when a token that can't be refreshed expires within this
CHECK_EVERY_S = 86400           # Facebook-login tokens: validated once a day
CONTAINER_TTL_S = 23 * 3600     # Meta: a container expires after 24 h
PROCESS_WAIT_S = 12 * 60        # waiting for Instagram's processing within one attempt
POLL_S = (5, 10, 15, 30, 60)    # then every minute
UPLOAD_TRIES = 4
PUBLISH_TRIES = 2
LOST_GRACE_S = 45               # after a publish without answer: re-check this long before trying again
MATCH_SLACK_S = 300             # a found media may be this much older than our publish request (clock skew)
STATE_KEYS = ("ig_container", "ig_container_at", "ig_uploaded", "ig_publish_at", "ig_media")

_locks = {}
_locks_guard = threading.Lock()


class InstagramError(Exception):
    """German message; `retryable` = trying again later can work; `slow` = back off longer (rate limits)."""

    def __init__(self, message, *, retryable=False, code="instagram_error", slow=False):
        super().__init__(message)
        self.retryable, self.code, self.slow = retryable, code, slow


class HttpResponse(NamedTuple):
    status: int
    headers: dict  # lower-case names
    body: bytes

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except ValueError:
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def transport(method, url, *, headers=None, body=None, timeout=60):
    """One HTTP request. Network failures raise OSError; HTTP errors are responses."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return HttpResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        with e:
            return HttpResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")


def _sleep(seconds, cancel=None):
    if cancel is not None:
        cancel.wait(seconds)
    else:
        time.sleep(seconds)


def _lock(account_id):
    with _locks_guard:
        return _locks.setdefault(account_id, threading.Lock())


# ---------------------------------------------------------------- errors

SUBCODES = {  # Meta's error table (subcode -> German, retryable, our code); [M][3], see the module table
    2207042: ("Instagram-Tageslimit für Beiträge über die API ist erreicht. Bitte morgen erneut versuchen.", False,
              "instagram_publish_limit"),
    2207026: ("Instagram unterstützt das Videoformat nicht.", False, "instagram_unsupported_media"),
    2207023: ("Instagram kennt diesen Medientyp nicht.", False, "instagram_unsupported_media"),
    2207050: ("Das Instagram-Konto ist eingeschränkt oder inaktiv. Bitte in der Instagram-App prüfen.", False,
              "instagram_account_restricted"),
    2207051: ("Instagram hat den Beitrag als möglichen Spam blockiert.", False, "instagram_blocked"),
    2207020: ("Der Instagram-Container ist abgelaufen.", True, "instagram_container_expired"),
    2207008: ("Instagram findet den Container nicht mehr (abgelaufen).", True, "instagram_container_expired"),
    2207006: ("Instagram findet das Medium nicht (abgelaufen oder keine Berechtigung).", False,
              "instagram_media_missing"),
    2207001: ("Instagram hatte einen internen Fehler.", True, "instagram_server_error"),
    2207003: ("Instagram hat beim Verarbeiten zu lange gebraucht.", True, "instagram_server_error"),
    2207032: ("Instagram konnte den Beitrag nicht anlegen.", True, "instagram_server_error"),
    2207053: ("Beim Hochladen zu Instagram ist ein unbekannter Fehler aufgetreten.", True, "instagram_server_error"),
}
TOKEN_RE = re.compile(r"\b(?:EAA|IG[A-Z]{2})[A-Za-z0-9_\-]{16,}")


def _scrub(text):
    """Meta's own message text, short and without anything that looks like a token."""
    text = TOKEN_RE.sub("…", str(text or ""))
    text = re.sub(r"(access_token|client_secret)=[^&\s]+", r"\1=…", text)
    return " ".join(text.split())[:200]


def _graph_error(r, what="Instagram"):
    data = r.json()
    err = data.get("error") if isinstance(data, dict) else None
    code = sub = None
    transient, detail = False, ""
    if isinstance(err, dict):
        code, sub = err.get("code"), err.get("error_subcode")
        transient = bool(err.get("is_transient"))
        detail = _scrub(err.get("error_user_msg") or err.get("message") or "")
    elif isinstance(data, dict) and (data.get("error_type") or data.get("error_message")):  # the oauth host's shape
        code = data.get("code")
        detail = _scrub(data.get("error_message") or "")
    hint = f" (Meta: {detail})" if detail else ""
    if code == 190:
        return InstagramError("Meta hat das Zugriffstoken abgelehnt (ungültig, widerrufen oder abgelaufen)." + hint,
                              code="instagram_token_invalid")
    if sub in SUBCODES:
        msg, retry, ours = SUBCODES[sub]
        return InstagramError(msg + hint, retryable=retry, code=ours)
    if code in RATE_CODES or r.status == 429:
        return InstagramError("Instagram bremst gerade (zu viele Anfragen). Bitte etwas warten.", retryable=True,
                              slow=True, code="instagram_rate_limit")
    if code == 10 or (isinstance(code, int) and 200 <= code < 300):
        return InstagramError("Dem Zugriff fehlt eine Berechtigung (Inhalte veröffentlichen). Bitte das Token mit "
                              "allen Berechtigungen neu erzeugen und unter Konten neu verbinden." + hint,
                              code="instagram_permission")
    if r.status >= 500 or transient or code in (1, 2):
        return InstagramError(f"{what} hat mit Fehler {r.status} geantwortet{hint}.", retryable=True,
                              code="instagram_http_error")
    return InstagramError(f"{what} hat die Anfrage abgelehnt (Fehler {r.status}" + (f", Code {code}" if code else "")
                          + f"){hint}.", code="instagram_rejected")


class _Unreachable(Exception):
    pass


def _unreachable(e=None):
    name = e.args[0] if isinstance(e, _Unreachable) and e.args else "Netzwerkfehler"
    return InstagramError(f"Instagram ist nicht erreichbar ({name}). Bitte die Internetverbindung prüfen.",
                          retryable=True, code="instagram_unreachable")


# ---------------------------------------------------------------- HTTP helpers

def _url(api, path, params=None):
    url = f"{GRAPH[api]}/{GRAPH_VERSION}/{path.lstrip('/')}"
    return f"{url}?{urllib.parse.urlencode(params)}" if params else url


def _call(method, url, *, token=None, scheme="Bearer", form=None, body=None, headers=None, timeout=60):
    h = {"Accept": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"{scheme} {token}"
    data = body
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        return transport(method, url, headers=h, body=data, timeout=timeout)
    except OSError as e:
        raise _Unreachable(e.__class__.__name__) from None


def _get_json(api, path, token, params=None, *, tries=3, what="Instagram"):
    """GET a Graph object; network failures and 5xx are retried (idempotent)."""
    url = _url(api, path, params)
    last = None
    for attempt in range(tries):
        try:
            r = _call("GET", url, token=token, timeout=30)
        except _Unreachable as e:
            last = _unreachable(e)
        else:
            if r.status == 200:
                data = r.json()
                return data if isinstance(data, dict) else {}
            last = _graph_error(r, what)
            if not last.retryable or last.slow:
                raise last
        if attempt + 1 < tries:
            _sleep(1.5 * (attempt + 1))
    raise last


def _token_get(url, *, what="Meta"):
    """GET one of the token endpoints (their documented form carries secrets in the query: never logged)."""
    last = None
    for attempt in range(3):
        try:
            r = _call("GET", url, timeout=30)
        except _Unreachable as e:
            last = _unreachable(e)
        else:
            if r.status == 200 and isinstance(r.json(), dict):
                return r.json()
            last = _graph_error(r, what)
            if not last.retryable:
                raise last
        if attempt < 2:
            _sleep(1.5 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- app credentials

def _secret(name):
    try:
        return store.get_secret(name)
    except KeyError:
        return None


def instagram_app():
    """(app id, app secret) of the owner's app for Instagram Login (OAuth), or raises."""
    app_id, secret = _secret("instagram_app_id"), _secret("instagram_app_secret")
    if not app_id or not secret:
        raise InstagramError("Instagram-App-ID und Instagram-App-Geheimcode fehlen (unter Konten eintragen).",
                             code="instagram_app_missing")
    return app_id, secret


def meta_app():
    """(app id, app secret) of the owner's Meta app for the Facebook-Page path, or raises."""
    app_id, secret = _secret("meta_app_id"), _secret("meta_app_secret")
    if not app_id or not secret:
        raise InstagramError("Für den Weg über die Facebook-Seite fehlen App-ID und App-Geheimcode deiner Meta-App "
                             "(unter Konten → Instagram eintragen): damit macht der Server aus dem kurzlebigen Token "
                             "eines, das nicht nach einer Stunde abläuft.", code="instagram_app_missing")
    return app_id, secret


# ---------------------------------------------------------------- connecting

def token_kind(token):
    """Facebook user/Page tokens start with "EAA", Instagram-Login tokens with "IG" (observed format, not a
    documented contract: a token of neither shape is tried as an Instagram-Login token first)."""
    t = str(token or "")
    if t.startswith("EAA"):
        return "facebook"
    if t.startswith("IG"):
        return "instagram"
    return None


def _clean_token(token):
    t = str(token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if not t or len(t) > 4096 or any(ch.isspace() for ch in t) or not t.isprintable():
        raise InstagramError("Das sieht nicht wie ein Zugriffstoken aus (nur das Token einfügen, ohne Leerzeichen).",
                             code="instagram_token_format")
    if len(t) < 20:
        raise InstagramError("Das Token ist zu kurz – bitte das ganze Token kopieren.", code="instagram_token_format")
    return t


NOT_PROFESSIONAL = ("Das ist ein privates Instagram-Konto. Über die API können nur professionelle Konten (Creator oder "
                    "Business) posten: in der Instagram-App unter Einstellungen → Kontotyp und Tools → „Zu "
                    "professionellem Konto wechseln“ (kostenlos), dann ein neues Token erzeugen.")


def _instagram_me(token):
    d = _get_json("instagram", "me", token, {"fields": "user_id,username,account_type"})
    uid = str(d.get("user_id") or d.get("id") or "")
    if not uid:
        raise InstagramError("Instagram hat zu diesem Token kein Konto genannt.", code="instagram_no_account")
    kind = str(d.get("account_type") or "").upper()
    if kind and kind not in PROFESSIONAL:
        raise InstagramError(NOT_PROFESSIONAL, code="instagram_not_professional")
    return {"ig_user_id": uid, "username": d.get("username") or uid, "account_type": kind or None}


def _fb_long_lived(token):
    app_id, secret = meta_app()
    q = urllib.parse.urlencode({"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
                                "fb_exchange_token": token})
    data = _token_get(f"{GRAPH['facebook']}/{GRAPH_VERSION}/oauth/access_token?{q}", what="Facebook")
    if not data.get("access_token"):
        raise InstagramError("Facebook hat kein langlebiges Token geliefert.", code="instagram_token_missing")
    expires = data.get("expires_in")
    return data["access_token"], (time.time() + float(expires)) if expires else None


NO_PAGE = ("Zu diesem Token gibt es keine Facebook-Seite mit verknüpftem Instagram-Profikonto. Prüfe: Ist das "
           "Instagram-Konto ein Creator- oder Business-Konto und mit deiner Facebook-Seite verknüpft? Hast du beim "
           "Erzeugen des Tokens die Seite und das Instagram-Konto ausgewählt?")


def _facebook_accounts(user_token):
    """[{ig_user_id, username, page_id, page_name, access_token}] for every Page with a linked professional
    Instagram account (the Page tokens come from the long-lived user token, so they don't expire)."""
    perms = _get_json("facebook", "me/permissions", user_token, what="Facebook")
    granted = {p.get("permission") for p in perms.get("data") or [] if isinstance(p, dict)
               and p.get("status") == "granted"}
    missing = [p for p in FB_REQUIRED if p not in granted]
    if missing:
        raise InstagramError(f"Dem Token fehlen Berechtigungen: {', '.join(missing)}. Im Graph API Explorer diese "
                             "Berechtigungen hinzufügen und das Token neu erzeugen.", code="instagram_permission")
    pages = _get_json("facebook", "me/accounts", user_token,
                      {"fields": "id,name,access_token,instagram_business_account{id,username}", "limit": "100"},
                      what="Facebook")
    out = []
    for p in pages.get("data") or []:
        ig = (p or {}).get("instagram_business_account") or {}
        if not ig.get("id") or not p.get("access_token"):
            continue
        username = ig.get("username")
        if not username:
            username = _get_json("facebook", str(ig["id"]), p["access_token"], {"fields": "username"},
                                 what="Facebook").get("username")
        out.append({"ig_user_id": str(ig["id"]), "username": username or str(ig["id"]), "page_id": str(p.get("id")),
                    "page_name": p.get("name"), "access_token": p["access_token"]})
    if not out:
        raise InstagramError(NO_PAGE, code="instagram_no_account")
    return out


def _new_creds(api, token, found, *, expires_at, expiry_known, now=None):
    now = time.time() if now is None else now
    creds = {"api": api, "access_token": token, "ig_user_id": found["ig_user_id"], "username": found["username"],
             "obtained_at": now, "expires_at": expires_at, "expiry_known": bool(expiry_known),
             "refresh_after": None, "refresh_tried_at": None, "refreshed_at": None, "refresh_error": None,
             "checked_at": now}
    if api == "instagram":
        # a pasted token may be up to 60 days old already: refresh it as soon as Meta allows (24 h after it was
        # made, which is at the latest 25 h from now); a fresh OAuth token is refreshed after a week
        creds["refresh_after"] = now + (REFRESH_EVERY_S if expiry_known else REFRESH_MIN_AGE_S)
    for k in ("page_id", "page_name"):
        if found.get(k):
            creds[k] = found[k]
    return creds


def _store_account(found, creds, account_id=None):
    """The account row for this Instagram account: the one asked for, else the one with its id, else a manual
    Instagram account with its @handle, else a new one."""
    handle = f"@{found['username']}"
    target = None
    if account_id is not None:
        target = store.get_account(account_id)
        if target is None or target["platform"] != "instagram":
            raise InstagramError("Dieses Konto ist kein Instagram-Konto.", code="invalid")
        if target.get("external_id") and target["external_id"] != found["ig_user_id"]:
            raise InstagramError(f"Das Token gehört zu {handle}, nicht zu „{target['label']}“. Bitte das Token des "
                                 "richtigen Kontos einfügen (oder ohne Kontoauswahl verbinden).",
                                 code="instagram_wrong_account")
    if target is None:
        accounts = store.list_accounts("instagram")
        target = next((a for a in accounts if a.get("external_id") == found["ig_user_id"]), None) or \
            next((a for a in accounts if not a.get("external_id")
                  and str(a.get("handle") or "").lstrip("@").lower() == found["username"].lower()), None)
    fields = {"connected": True, "external_id": found["ig_user_id"], "credentials": creds, "handle": handle}
    if target is None:
        aid = store.add_account("instagram", handle, **fields)
    else:
        aid = target["id"]
        store.update_account(aid, **fields)
    _resolve_alerts(aid)
    return store.get_account(aid)


def connect_token(token, *, account_id=None):
    """The paste path: a long-lived Instagram-Login token (from the app dashboard) or a Facebook user token (Graph
    API Explorer; exchanged for a long-lived one with the Meta app's id and secret). Validated before anything is
    stored. Returns the connected account rows."""
    token = _clean_token(token)
    kind = token_kind(token)
    if kind == "facebook":
        long_token, user_expires = _fb_long_lived(token)
        found = _facebook_accounts(long_token)
        if account_id is not None:
            target = store.get_account(account_id)
            ext = (target or {}).get("external_id")
            match = [f for f in found if f["ig_user_id"] == ext] if ext else found[:1] if len(found) == 1 else []
            if not match:
                names = ", ".join(f"@{f['username']}" for f in found)
                raise InstagramError(f"Das Token gehört zu {names}. Wähle beim Verbinden kein Konto aus (dann werden "
                                     "alle gefundenen angelegt) oder nimm das Token des richtigen Kontos.",
                                     code="instagram_wrong_account")
            found = match
        rows = []
        for f in found:
            creds = _new_creds("facebook", f["access_token"], f, expires_at=None, expiry_known=True)
            creds["user_token_expires_at"] = user_expires
            rows.append(_store_account(f, creds, account_id))
        return rows
    try:
        found = _instagram_me(token)
    except InstagramError as e:
        if kind is None and e.code == "instagram_token_invalid":
            raise InstagramError("Meta kennt dieses Token nicht. Bitte ein Instagram-Token (beginnt meist mit „IG“) "
                                 "oder ein Facebook-Token (beginnt mit „EAA“) einfügen.",
                                 code="instagram_token_invalid") from None
        raise
    now = time.time()
    creds = _new_creds("instagram", token, found, expires_at=now + TOKEN_LIFETIME_S, expiry_known=False, now=now)
    return [_store_account(found, creds, account_id)]


def auth_url(app_id, redirect_uri, state):
    q = {"client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": ",".join(SCOPES),
         "state": state}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def connect_oauth(code, redirect_uri, *, account_id=None):
    """Instagram Login: code -> short-lived token -> long-lived token (60 days) -> the account."""
    app_id, secret = instagram_app()
    form = {"client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri, "code": code}
    r = None
    for attempt in range(3):
        try:
            r = _call("POST", CODE_URL, form=form, timeout=30)
        except _Unreachable as e:
            if attempt == 2:
                raise _unreachable(e) from None
            _sleep(1.5 * (attempt + 1))
            continue
        if r.status < 500:
            break
        _sleep(1.5 * (attempt + 1))
    data = r.json() if r is not None else None
    if r is None or r.status != 200 or not isinstance(data, dict):
        err = (data or {}).get("error") if isinstance(data, dict) else None
        msg = str((err or {}).get("message") if isinstance(err, dict) else (data or {}).get("error_message")
                  if isinstance(data, dict) else "").lower()
        if "redirect" in msg:
            raise InstagramError(f"Die Weiterleitungs-URI {redirect_uri} ist in der Meta-App nicht eingetragen.",
                                 code="instagram_redirect_mismatch")
        if "authorization code" in msg or "code has" in msg or "code was" in msg:
            raise InstagramError("Der Anmeldecode von Instagram ist abgelaufen oder wurde schon benutzt. Bitte erneut "
                                 "verbinden.", code="instagram_invalid_grant")
        if "client" in msg or "app" in msg.split():
            raise InstagramError("Meta kennt diese Instagram-App-ID oder diesen App-Geheimcode nicht. Bitte unter "
                                 "Konten prüfen.", code="instagram_invalid_client")
        raise _graph_error(r, "Instagram") if r is not None else _unreachable()
    row = data["data"][0] if isinstance(data.get("data"), list) and data["data"] else data
    short = row.get("access_token") if isinstance(row, dict) else None
    if not short:
        raise InstagramError("Instagram hat kein Zugriffstoken geliefert.", code="instagram_token_missing")
    perms = row.get("permissions")
    perms = perms.split(",") if isinstance(perms, str) else perms if isinstance(perms, list) else None
    if perms is not None and "instagram_business_content_publish" not in [p.strip() for p in perms]:
        raise InstagramError("Beim Zustimmen fehlte die Berechtigung „Inhalte veröffentlichen“. Bitte erneut "
                             "verbinden und alles erlauben.", code="instagram_permission")
    q = urllib.parse.urlencode({"grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short})
    long = _token_get(f"{IG_EXCHANGE_URL}?{q}", what="Instagram")
    if not long.get("access_token"):
        raise InstagramError("Instagram hat kein langlebiges Token geliefert.", code="instagram_token_missing")
    found = _instagram_me(long["access_token"])
    now = time.time()
    lifetime = float(long.get("expires_in") or TOKEN_LIFETIME_S)
    creds = _new_creds("instagram", long["access_token"], found, expires_at=now + lifetime, expiry_known=True,
                       now=now)
    return _store_account(found, creds, account_id)


# ---------------------------------------------------------------- keeping the token alive

def connection_info(account):
    """What the dashboard may show about a connection (never the token)."""
    c = (account or {}).get("credentials") or {}
    if not c:
        return None
    return {"kind": c.get("api") or "instagram", "username": c.get("username"),
            "expires_at": c.get("expires_at"), "expires_estimated": not c.get("expiry_known", True),
            "refreshed_at": c.get("refreshed_at"), "checked_at": c.get("checked_at"),
            "refresh_error": c.get("refresh_error"), "page_name": c.get("page_name")}


def _resolve_alerts(account_id):
    store.resolve_alert(f"instagram.disconnected.{account_id}")
    store.resolve_alert(f"instagram.expiring.{account_id}")


def _disconnected(account, why):
    store.update_account(account["id"], connected=False, credentials=None)
    store.resolve_alert(f"instagram.expiring.{account['id']}")
    msg = (f"Instagram-Konto „{account['label']}“ ist nicht mehr verbunden ({why}). Bitte unter Konten ein neues "
           f"Token einfügen bzw. neu verbinden.")
    store.raise_alert(f"instagram.disconnected.{account['id']}", "warning", msg, {"account_id": account["id"]})
    return InstagramError(msg, code="instagram_disconnected")


def _verify(creds):
    """True when the token still works, False when Meta says it is dead (190); raises InstagramError otherwise."""
    try:
        if creds.get("api") == "facebook":
            _get_json("facebook", str(creds.get("ig_user_id")), creds["access_token"], {"fields": "username"},
                      tries=2)
        else:
            _instagram_me(creds["access_token"])
        return True
    except InstagramError as e:
        if e.code == "instagram_token_invalid":
            return False
        raise


def _refresh(creds, now):
    q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": creds["access_token"]})
    data = _token_get(f"{IG_REFRESH_URL}?{q}", what="Instagram")
    if not data.get("access_token"):
        raise InstagramError("Instagram hat beim Erneuern kein Token geliefert.", retryable=True,
                             code="instagram_token_missing")
    lifetime = float(data.get("expires_in") or TOKEN_LIFETIME_S)
    return {**creds, "access_token": data["access_token"], "obtained_at": now, "expires_at": now + lifetime,
            "expiry_known": True, "refreshed_at": now, "refresh_error": None, "checked_at": now,
            "refresh_after": now + min(REFRESH_EVERY_S, max(REFRESH_MIN_AGE_S, lifetime / 2))}


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def _maintain_one(account_id, now, force=False):
    """Refresh (Instagram Login) or check (Facebook login) one account's token when due. Returns an action word."""
    with _lock(account_id):
        account = store.get_account(account_id, with_credentials=True)
        creds = dict((account or {}).get("credentials") or {})
        if not account or account["platform"] != "instagram" or not account["connected"] or not creds:
            return "skipped"
        exp = creds.get("expires_at")
        if creds.get("api") == "facebook":
            if not force and (now - float(creds.get("checked_at") or 0) < CHECK_EVERY_S
                              or now - float(creds.get("refresh_tried_at") or 0) < REFRESH_RETRY_S):
                return "fresh"
            creds["refresh_tried_at"] = now
            store.update_account(account_id, credentials=creds)
            try:
                alive = _verify(creds)
            except InstagramError as e:
                store.update_account(account_id, credentials={**creds, "refresh_error": str(e)})
                return "unchecked"
            if not alive:
                _disconnected(account, "Meta hat den Zugriff widerrufen oder er ist abgelaufen")
                return "disconnected"
            store.update_account(account_id, credentials={**creds, "checked_at": now, "refresh_error": None})
            _resolve_alerts(account_id)
            return "checked"
        if exp and now >= float(exp) and creds.get("expiry_known"):
            try:
                alive = _verify(creds)
            except InstagramError:
                return "unchecked"
            if not alive:
                _disconnected(account, "das Token ist abgelaufen")
                return "disconnected"
            creds.update(expires_at=None, expiry_known=False, refresh_after=now)  # our expiry was wrong
        due = now >= float(creds.get("refresh_after") or 0)
        rested = now - float(creds.get("refresh_tried_at") or 0) >= REFRESH_RETRY_S
        if not due or not rested:
            return "fresh"
        # the attempt is recorded before the request: a crash or a hang can't turn this into a loop
        creds["refresh_tried_at"] = now
        store.update_account(account_id, credentials=creds)
        try:
            new = _refresh(creds, now)
        except InstagramError as e:
            if e.code == "instagram_token_invalid":
                try:
                    alive = _verify(creds)
                except InstagramError:
                    alive = True  # can't tell now: keep the token, try again later
                if not alive:
                    _disconnected(account, "Meta hat den Zugriff widerrufen oder das Token ist abgelaufen")
                    return "disconnected"
            # the old token stays: it still works (or can't be checked); the next try comes after REFRESH_RETRY_S
            creds["refresh_error"] = str(e)
            if not e.retryable and e.code != "instagram_token_invalid":
                creds["refresh_after"] = max(float(creds.get("refresh_after") or 0), now + REFRESH_MIN_AGE_S)
            store.update_account(account_id, credentials=creds)
            if exp and float(exp) - now < EXPIRY_WARN_S:
                store.raise_alert(f"instagram.expiring.{account_id}", "warning",
                                  f"Der Instagram-Zugriff für „{account['label']}“ läuft "
                                  f"{'spätestens ' if not creds.get('expiry_known') else ''}am {_day(float(exp))} ab "
                                  f"und ließ sich nicht automatisch erneuern ({e}). Bitte unter Konten ein neues "
                                  f"Token einfügen.", {"account_id": account_id})
            return "failed"
        store.update_account(account_id, credentials=new)
        _resolve_alerts(account_id)
        return "refreshed"


def maintain(now=None):
    """The health tick: every connected Instagram account's token is refreshed or checked when it is due. Each
    failure waits REFRESH_RETRY_S before the next try; nothing here raises."""
    now = time.time() if now is None else now
    out = []
    for a in store.list_accounts("instagram"):
        if not a.get("connected") or not a.get("has_credentials"):
            continue
        try:
            out.append({"account_id": a["id"], "action": _maintain_one(a["id"], now)})
        except Exception:
            log.exception("instagram token upkeep of account %s failed", a["id"])
    return out


def check_account(account_id):
    """"Verbindung prüfen": validates the token now (and refreshes it when it is due). Raises InstagramError."""
    account = store.get_account(account_id, with_credentials=True)
    if account is None:
        raise InstagramError("Dieses Konto gibt es nicht.", code="not_found")
    creds = account.get("credentials") or {}
    if account["platform"] != "instagram" or not account["connected"] or not creds.get("access_token"):
        raise InstagramError(f"„{account['label']}“ ist nicht mit Instagram verbunden.", code="not_connected")
    with _lock(account_id):
        alive = _verify(creds)
        if not alive:
            raise _disconnected(account, "Meta hat das Token abgelehnt")
        fresh = {**creds, "checked_at": time.time()}
        if creds.get("api") == "facebook":
            fresh["refresh_error"] = None
        store.update_account(account_id, credentials=fresh)
        store.resolve_alert(f"instagram.disconnected.{account_id}")
    _maintain_one(account_id, time.time())  # refreshes an Instagram-Login token that is due
    return store.get_account(account_id)


# ---------------------------------------------------------------- caption

def _u16(text):
    return len(text.encode("utf-16-le")) // 2  # how Meta probably counts (emoji = 2); the safe side either way


def _cut(text, limit):
    """At most `limit` UTF-16 units including the "…", cut at a word boundary when one is near."""
    if _u16(text) <= limit:
        return text
    units, end = 0, len(text)
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit - 1:
            end = i
            break
    out = text[:end]
    space = out.rfind(" ")
    if space >= len(out) * 0.7:
        out = out[:space]
    return out.rstrip(" ,;:-–—|") + "…"


def caption(variant, video=None):
    """The Reel's caption from the variant: its title as the first line, the description body (credit line
    included), then its hashtags in one line. YouTube-only tags (#Shorts …) are dropped; at most 30 hashtags and
    20 @-mentions, at most 2200 characters (the body is shortened, title, credit and tags stay)."""
    title = " ".join(str(variant.get("title") or (video or {}).get("title") or "").split())
    desc = str(variant.get("description") or (video or {}).get("description") or "").replace("\r\n", "\n").strip()
    tags, seen = [], set()

    def add(tag):
        t = "#" + tag.lstrip("#").strip()
        if len(t) > 1 and re.fullmatch(r"#\w+", t) and t.lower() not in seen and t.lower() not in YOUTUBE_ONLY_TAGS:
            seen.add(t.lower())
            tags.append(t)
    lines = desc.split("\n")
    while lines and (not lines[-1].strip() or all(w.startswith("#") for w in lines[-1].split())):
        for w in lines.pop().split():
            add(w)
    for h in variant.get("hashtags") or []:
        add(str(h))
    body = "\n".join(lines)
    body = re.sub(r"(?i)(^|\s)(" + "|".join(re.escape(t) for t in YOUTUBE_ONLY_TAGS) + r")\b", r"\1", body)
    body = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", body)).strip()
    inline = {t.lower() for t in re.findall(r"#\w+", body)}
    tags = [t for t in tags if t.lower() not in inline][:max(0, HASHTAGS_MAX - len(inline))]
    if len(inline) > HASHTAGS_MAX:  # more inline hashtags than Instagram takes: the rest lose their "#"
        keep = set(list(dict.fromkeys(t.lower() for t in re.findall(r"#\w+", body)))[:HASHTAGS_MAX])
        body = re.sub(r"#(\w+)", lambda m: m.group(0) if m.group(0).lower() in keep else m.group(1), body)
    head = title if title and not body.lower().startswith(title.lower()) else ""
    tail = " ".join(tags)
    room = CAPTION_MAX - _u16(head) - _u16(tail) - (2 if head else 0) - (2 if tail else 0)
    if _u16(body) > room:
        paragraphs = body.split("\n\n")
        credit = paragraphs[-1] if len(paragraphs) > 1 and re.search(r"https?://", paragraphs[-1]) else ""
        main = "\n\n".join(paragraphs[:-1]) if credit else body
        main = _cut(main, max(0, room - _u16(credit) - (2 if credit else 0)))
        body = "\n\n".join(x for x in (main, credit) if x)
        if _u16(body) > room:
            body = _cut(body, max(0, room))
    text = "\n\n".join(x for x in (head, body, tail) if x)
    mentions = re.findall(r"(?<![\w@])@[\w.]+", text)
    if len(mentions) > MENTIONS_MAX:
        n = {"i": 0}

        def keep_or_strip(m):
            n["i"] += 1
            return m.group(0) if n["i"] <= MENTIONS_MAX else m.group(0)[1:]
        text = re.sub(r"(?<![\w@])@[\w.]+", keep_or_strip, text)
    return _cut(text, CAPTION_MAX) if _u16(text) > CAPTION_MAX else text


# ---------------------------------------------------------------- the file

def _boxes(buf, start=0, end=None):
    """[(type, body_start, body_end)] of the MP4 boxes in buf[start:end]."""
    end = len(buf) if end is None else end
    out, pos = [], start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
        head = 8
        if size == 1:
            if pos + 16 > end:
                break
            size, head = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            break
        out.append((kind.decode("latin-1"), pos + head, pos + size))
        pos += size
    return out


def _child(buf, parent, kind):
    return next(((s, e) for k, s, e in _boxes(buf, *parent) if k == kind), None)


def _mvhd_like(buf, box):
    """(timescale, duration) of an mvhd or mdhd box."""
    s, _ = box
    version = buf[s]
    if version == 1:
        return struct.unpack(">IQ", buf[s + 20:s + 32])
    return struct.unpack(">II", buf[s + 12:s + 20])


def _track(buf, trak):
    mdia = _child(buf, trak, "mdia")
    if not mdia:
        return None
    hdlr, mdhd = _child(buf, mdia, "hdlr"), _child(buf, mdia, "mdhd")
    minf = _child(buf, mdia, "minf")
    stbl = _child(buf, minf, "stbl") if minf else None
    stsd = _child(buf, stbl, "stsd") if stbl else None
    if not hdlr or not mdhd or not stsd:
        return None
    t = {"handler": buf[hdlr[0] + 8:hdlr[0] + 12].decode("latin-1")}
    t["timescale"], t["duration"] = _mvhd_like(buf, mdhd)
    entries = _boxes(buf, stsd[0] + 8, stsd[1])
    if entries:
        kind, s, _ = entries[0]
        t["codec"] = kind
        if t["handler"] == "vide" and s + 28 <= len(buf):
            t["width"], t["height"] = struct.unpack(">HH", buf[s + 24:s + 28])
        if t["handler"] == "soun" and s + 28 <= len(buf) and struct.unpack(">H", buf[s + 8:s + 10])[0] == 0:
            # ISO audio sample entry (a QuickTime v1/v2 sound description keeps these elsewhere: not read)
            t["channels"] = struct.unpack(">H", buf[s + 16:s + 18])[0]
            t["sample_rate"] = struct.unpack(">I", buf[s + 24:s + 28])[0] >> 16
    stts = _child(buf, stbl, "stts")
    if stts:
        n = struct.unpack(">I", buf[stts[0] + 4:stts[0] + 8])[0]
        t["samples"] = sum(struct.unpack(">I", buf[stts[0] + 8 + 8 * i:stts[0] + 12 + 8 * i])[0]
                           for i in range(min(n, (stts[1] - stts[0] - 8) // 8)))
    tkhd = _child(buf, trak, "tkhd")
    if tkhd and t["handler"] == "vide" and tkhd[1] - tkhd[0] >= 8:
        w, h = struct.unpack(">II", buf[tkhd[1] - 8:tkhd[1]])
        if w and h:
            t["width"], t["height"] = w >> 16, h >> 16
    return t


def _probe(path):
    """Top-level box order and the parsed moov of an MP4/MOV file (no ffprobe needed)."""
    order, moov = [], None
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        pos = 0
        while pos + 8 <= size and len(order) < 64:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                break
            box, kind = struct.unpack(">I4s", head[:8])
            hl = 8
            if box == 1 and len(head) == 16:
                box, hl = struct.unpack(">Q", head[8:16])[0], 16
            elif box == 0:
                box = size - pos
            if box < hl:
                break
            name = kind.decode("latin-1")
            order.append(name)
            if name == "moov" and box <= 64 * 1024 * 1024:
                f.seek(pos)
                moov = f.read(box)
            pos += box
    return order, moov


def _de(x):
    return f"{x:.1f}".replace(".", ",").replace(",0", "")


def check_file(path):
    """The variant's MP4 against Instagram's Reels requirements (REELS, see the table). {problems: [German], size,
    duration, width, height, fps, video_codec, audio_codec, sample_rate, channels, faststart}."""
    out = {"problems": []}
    p = out["problems"]
    try:
        size = path.stat().st_size
        order, moov = _probe(path)
    except OSError:
        p.append("Die MP4-Datei lässt sich nicht lesen.")
        return out
    out["size"] = size
    if "ftyp" not in order[:2] or moov is None:
        p.append("Das ist keine gültige MP4-Datei.")
        return out
    out["faststart"] = "mdat" not in order or order.index("moov") < order.index("mdat")
    if not out["faststart"]:
        p.append("Die Videodaten stehen vor dem Inhaltsverzeichnis (moov) – Instagram verlangt es am Anfang der Datei "
                 "(„faststart“).")
    root = (8, len(moov))
    mvhd = _child(moov, root, "mvhd")
    tracks = [t for k, s, e in _boxes(moov, *root) if k == "trak" for t in [_track(moov, (s, e))] if t]
    if mvhd:
        scale, dur = _mvhd_like(moov, mvhd)
        out["duration"] = dur / scale if scale else None
    video = next((t for t in tracks if t["handler"] == "vide"), None)
    audio = next((t for t in tracks if t["handler"] == "soun"), None)
    if video is None:
        p.append("Die Datei hat keine Videospur.")
        return out
    out["video_codec"], out["width"], out["height"] = video.get("codec"), video.get("width"), video.get("height")
    vdur = video["duration"] / video["timescale"] if video.get("timescale") else None
    if not out.get("duration"):
        out["duration"] = vdur
    if video.get("samples") and vdur:
        out["fps"] = round(video["samples"] / vdur, 2)
    if video.get("codec") not in REELS["video"]:
        p.append(f"Videocodec „{video.get('codec')}“ – Instagram nimmt H.264 oder HEVC.")
    if audio:
        out["audio_codec"], out["sample_rate"], out["channels"] = (audio.get("codec"), audio.get("sample_rate"),
                                                                   audio.get("channels"))
        if audio.get("codec") not in REELS["audio"]:
            p.append(f"Audiocodec „{audio.get('codec')}“ – Instagram nimmt AAC.")
        if (audio.get("sample_rate") or 0) > REELS["max_rate"]:
            p.append(f"Abtastrate {audio['sample_rate']} Hz – Instagram nimmt höchstens 48 kHz.")
        if (audio.get("channels") or 0) > REELS["max_channels"]:
            p.append(f"{audio['channels']} Tonkanäle – Instagram nimmt Mono oder Stereo.")
    d = out.get("duration") or 0
    if d < REELS["min_s"]:
        p.append(f"Das Video ist {_de(d)} s lang – ein Reel muss mindestens 3 s lang sein.")
    if d > REELS["max_s"]:
        p.append("Das Video ist länger als 15 Minuten.")
    if size > REELS["max_bytes"]:
        p.append(f"Die Datei ist {size / 1e6:.0f} MB groß – Instagram nimmt höchstens 300 MB.")
    if d and size * 8 / d / 1e6 > REELS["max_mbps"]:
        p.append(f"Die Bitrate ({size * 8 / d / 1e6:.0f} Mbit/s) liegt über Instagrams 25 Mbit/s.")
    w, h = out.get("width") or 0, out.get("height") or 0
    if w > REELS["max_width"]:
        p.append(f"Das Video ist {w} Pixel breit – Instagram nimmt höchstens 1920.")
    if w and h and not REELS["aspect"][0] <= w / h <= REELS["aspect"][1]:
        p.append(f"Seitenverhältnis {w}×{h} wird nicht angenommen.")
    fps = out.get("fps")
    if fps and not REELS["fps"][0] - 0.5 <= fps <= REELS["fps"][1] + 0.5:
        p.append(f"{_de(fps)} Bilder pro Sekunde – Instagram nimmt 23 bis 60.")
    return out


def _variant_file(variant):
    """The variant's MP4 - only inside the exports folder (no traversal, no symlink out of it)."""
    root = paths.exports_dir().resolve()
    p = paths.from_rel(variant.get("file") or "").resolve()
    if p == root or root not in p.parents or p.suffix.lower() != ".mp4" or not p.is_file():
        raise InstagramError("Die MP4-Datei dieser Variante fehlt (gelöscht oder verschoben?).", code="file_missing")
    return p


def file_problems(variant):
    """German reasons Instagram would refuse this variant's file ([] = fine), checked before anything is sent."""
    try:
        path = _variant_file(variant)
    except InstagramError as e:
        return [str(e)]
    return check_file(path)["problems"]


# ---------------------------------------------------------------- publishing

def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InstagramError("Upload abgebrochen.", code="canceled")


def _publishing_account(account_id):
    a = store.get_account(account_id, with_credentials=True)
    if a is None:
        raise InstagramError("Das Konto gibt es nicht mehr.", code="not_found")
    c = a.get("credentials") or {}
    if a["platform"] != "instagram" or not a["connected"] or not c.get("access_token"):
        raise InstagramError(f"„{a['label']}“ ist nicht mit Instagram verbunden.", code="not_connected")
    return {"id": a["id"], "label": a["label"], "api": c.get("api") or "instagram",
            "ig": str(c.get("ig_user_id") or a.get("external_id")), "username": c.get("username")}


def _token(acct):
    """The account's current token (read for every request: a refresh in the health tick may have replaced it)."""
    a = store.get_account(acct["id"], with_credentials=True)
    c = (a or {}).get("credentials") or {}
    if not a or not a["connected"] or not c.get("access_token"):
        raise InstagramError(f"„{acct['label']}“ ist nicht mehr mit Instagram verbunden.", code="not_connected")
    return c["access_token"]


def _token_failed(account_id, err):
    """A 190 during publishing: dead (-> disconnected + alert) or a token that works but lacks something."""
    account = store.get_account(account_id, with_credentials=True)
    creds = (account or {}).get("credentials") or {}
    if not account or not creds:
        return err
    try:
        alive = _verify(creds)
    except InstagramError:
        return InstagramError(str(err), retryable=True, code=err.code)
    return err if alive else _disconnected(account, "Meta hat das Token abgelehnt")


def _status(acct, cid):
    d = _get_json(acct["api"], cid, _token(acct), {"fields": STATUS_FIELDS})
    return str(d.get("status_code") or "").upper() or None, _scrub(d.get("status") or "")


def _ts(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return None


def _norm(text):
    return " ".join(str(text or "").split()).lower()


def _find_media(acct, text, since):
    """The media this caption produced since `since` (newest of the account's last 25), or None."""
    try:
        d = _get_json(acct["api"], f"{acct['ig']}/media", _token(acct),
                      {"fields": "id,caption,timestamp,permalink", "limit": "25"})
    except InstagramError as e:
        log.info("looking up the published media failed: %s", e.code)
        return None
    want = _norm(text)
    for m in d.get("data") or []:
        ts = _ts(m.get("timestamp"))
        got = _norm(m.get("caption"))
        if m.get("id") and (ts is None or ts >= since) and got and (got == want or (len(got) >= 40 and
                                                                                   want.startswith(got[:100]))):
            return {"id": str(m["id"]), "permalink": m.get("permalink")}
    return None


def _permalink(acct, media_id):
    for _ in range(2):
        try:
            return _get_json(acct["api"], media_id, _token(acct), {"fields": "permalink"}, tries=2).get("permalink")
        except InstagramError as e:
            log.info("permalink of %s: %s", media_id, e.code)
    return None


def _result(acct, media, cid):
    mid = (media or {}).get("id")
    link = (media or {}).get("permalink") or (_permalink(acct, mid) if mid else None)
    return {"external_id": mid, "media_id": mid, "url": link, "container_id": cid}


def _published_state(acct, cid, text, since):
    """What happened to a container a publish request was sent for: ("published", result) | ("ready", None: not
    published, can be published) | ("processing", None) | ("gone", None: expired or failed, not published)."""
    code, _ = _status(acct, cid)
    if code == "PUBLISHED":
        return "published", _result(acct, _find_media(acct, text, since), cid)
    if code == "FINISHED":
        return "ready", None
    if code == "IN_PROGRESS":
        return "processing", None
    media = _find_media(acct, text, since)  # an expired container says nothing more: look at the account itself
    return ("published", _result(acct, media, cid)) if media else ("gone", None)


def _quota(acct):
    """Fails early when today's publishing limit is used up (best effort: an unreadable limit is ignored)."""
    try:
        d = _get_json(acct["api"], f"{acct['ig']}/content_publishing_limit", _token(acct),
                      {"fields": "quota_usage,config"}, tries=1)
    except InstagramError as e:
        if e.code in ("instagram_token_invalid", "instagram_permission"):
            raise
        return
    row = (d.get("data") or [{}])[0] if isinstance(d.get("data"), list) else {}
    used, total = row.get("quota_usage"), (row.get("config") or {}).get("quota_total")
    if isinstance(used, int) and isinstance(total, int) and total > 0 and used >= total:
        raise InstagramError(f"Instagram-Tageslimit erreicht: {used} von {total} Beiträgen über die API in 24 Stunden. "
                             "Bitte morgen erneut versuchen.", code="instagram_publish_limit")


RESUMABLE_HINT = (" Laut Meta funktioniert das direkte Hochladen einer Videodatei nur mit einer Meta-App mit "
                  "Facebook-Login (Weg A unter Konten: Instagram-Konto mit einer Facebook-Seite verknüpft).")


def _create_container(acct, text, share):
    form = {"media_type": "REELS", "upload_type": "resumable", "caption": text,
            "share_to_feed": "true" if share else "false"}
    last = None
    for attempt in range(3):
        try:
            r = _call("POST", _url(acct["api"], f"{acct['ig']}/media"), token=_token(acct), form=form, timeout=60)
        except _Unreachable as e:
            last = _unreachable(e)
        else:
            data = r.json() if r.status == 200 else None
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"])
            last = _graph_error(r) if r.status != 200 else InstagramError(
                "Instagram hat keinen Container angelegt.", retryable=True, code="instagram_no_container")
            if not last.retryable or last.slow:
                if acct["api"] == "instagram" and last.code == "instagram_rejected":
                    last = InstagramError(str(last) + RESUMABLE_HINT, code="instagram_rejected")
                raise last
        _sleep(2 * (attempt + 1))
    raise last


def _upload(acct, cid, path, size, on_progress, cancel, say):
    """The whole file in one request to the resumable endpoint (offset 0). A dropped connection or a 5xx sends it
    again from byte 0 to the same container: Meta documents no way to ask a container how many bytes arrived."""
    url = f"{RUPLOAD_URL}/{GRAPH_VERSION}/{cid}"
    data = path.read_bytes()
    if on_progress:
        on_progress(0, size)
    say(f"Lade das Video zu Instagram hoch ({_de(size / 1e6)} MB) …")
    last = None
    for attempt in range(UPLOAD_TRIES):
        _check_cancel(cancel)
        headers = {"offset": "0", "file_size": str(size), "Content-Type": "application/octet-stream"}
        try:
            r = _call("POST", url, token=_token(acct), scheme="OAuth", body=data, headers=headers, timeout=900)
        except _Unreachable as e:
            last, r = _unreachable(e), None
        answer = r.json() if r is not None else None
        if r is not None and r.status == 200 and not (isinstance(answer, dict) and answer.get("success") is False):
            if on_progress:
                on_progress(size, size)
            return
        if r is not None:
            last = _graph_error(r)
            if not last.retryable or last.slow:
                code, _ = _status(acct, cid)  # a repeated upload after a lost answer: did the first one arrive?
                if code in ("FINISHED", "PUBLISHED"):
                    if on_progress:
                        on_progress(size, size)
                    return
                raise last
        log.info("instagram upload of container %s: attempt %s failed, retrying", cid, attempt + 1)
        _sleep(min(60.0, 4 * 2 ** attempt) + random.random(), cancel)
    raise InstagramError("Der Upload zu Instagram bricht immer wieder ab.",
                         retryable=True, code=getattr(last, "code", "instagram_unreachable"))


def _wait_ready(acct, cid, cancel, say, save):
    """Poll the container until Instagram has processed the video (FINISHED) within PROCESS_WAIT_S."""
    say("Instagram verarbeitet das Video …")
    start, step, told, last_err = time.monotonic(), 0, 0, None
    while True:
        _check_cancel(cancel)
        try:
            code, status = _status(acct, cid)
            last_err = None
        except InstagramError as e:
            if not e.retryable:
                raise
            code, status, last_err = None, "", e
        if code in ("FINISHED", "PUBLISHED"):
            return code
        if code == "ERROR":
            save(ig_container=None, ig_uploaded=False)
            m = re.search(r"22070\d\d", status)
            sub = SUBCODES.get(int(m.group(0))) if m else None
            if sub and not sub[1]:
                raise InstagramError(f"Instagram konnte das Video nicht verarbeiten: {sub[0]}", code=sub[2])
            raise InstagramError("Instagram konnte das Video nicht verarbeiten" + (f" ({status})" if status else "")
                                 + ". Der nächste Versuch legt einen neuen Container an.", retryable=True,
                                 code="instagram_processing_error")
        if code == "EXPIRED":
            save(ig_container=None, ig_uploaded=False)
            raise InstagramError("Der Instagram-Container ist abgelaufen (nach 24 Stunden). Der nächste Versuch legt "
                                 "einen neuen an.", retryable=True, code="instagram_container_expired")
        waited = time.monotonic() - start
        if waited > PROCESS_WAIT_S:
            if last_err is not None:
                raise last_err
            raise InstagramError(f"Instagram verarbeitet das Video seit über {PROCESS_WAIT_S // 60} Minuten. Der nächste "
                                 "Versuch fragt denselben Container erneut (kein zweiter Upload).", retryable=True,
                                 code="instagram_processing_timeout")
        if waited - told >= 60:
            told = waited
            say(f"Instagram verarbeitet das Video … (seit {int(waited // 60)} Min.)")
        _sleep(POLL_S[min(step, len(POLL_S) - 1)], cancel)
        step += 1


def _publish_container(acct, cid, text, since, say):
    """media_publish once; without an answer, look whether the container got published before trying again."""
    url = _url(acct["api"], f"{acct['ig']}/media_publish")
    for attempt in range(PUBLISH_TRIES):
        try:
            r = _call("POST", url, token=_token(acct), form={"creation_id": cid}, timeout=120)
        except _Unreachable:
            r = None
        answer = r.json() if r is not None else None
        if r is not None and r.status == 200 and isinstance(answer, dict) and answer.get("id"):
            return _result(acct, {"id": str(answer["id"])}, cid)
        if r is not None and 400 <= r.status < 500 and r.status != 429:
            err = _graph_error(r)
            if err.code != "instagram_token_invalid":
                kind, res = _published_state(acct, cid, text, since)  # refused, but maybe published already
                if kind == "published":
                    return res
            raise err
        say("Keine Antwort von Instagram beim Veröffentlichen – prüfe, ob der Reel trotzdem online ist …")
        deadline, kind = time.monotonic() + LOST_GRACE_S, None
        while True:
            try:
                kind, res = _published_state(acct, cid, text, since)
            except InstagramError:
                kind, res = None, None
            if kind == "published":
                return res
            if kind == "gone" or time.monotonic() >= deadline:
                break
            _sleep(5)  # no cancel here: the request is out
        if kind != "ready":
            break  # unknown or gone: never publish again in this attempt
    raise InstagramError("Instagram hat das Veröffentlichen nicht bestätigt. Der nächste Versuch prüft zuerst, ob der "
                         "Reel schon online ist (kein doppelter Reel).", retryable=True,
                         code="instagram_publish_unknown")


def publish(variant_id, account_id, options=None, *, state=None, on_state=None, on_progress=None, cancel=None,
            log=None):
    try:
        return _publish(variant_id, account_id, options or {}, dict(state or {}), on_state, on_progress, cancel,
                        log or (lambda text: None))
    except InstagramError as e:
        if e.code == "instagram_token_invalid":
            raise _token_failed(account_id, e) from None
        raise


def _publish(variant_id, account_id, options, st, on_state, on_progress, cancel, say):
    def save(**changes):
        st.update(changes)
        if on_state:
            on_state(dict(changes))

    variant = store.get_variant(variant_id)
    if variant is None:
        raise InstagramError("Diese Variante gibt es nicht mehr.", code="not_found")
    video = store.get_video(variant["video_id"])
    text = caption(variant, video)
    acct = _publishing_account(account_id)

    # 1. a publish request already went out (crash, lost answer, late cancel): look first, never publish twice
    if st.get("ig_publish_at") and st.get("ig_container"):
        say("Prüfe zuerst, ob Instagram den Reel des letzten Versuchs schon veröffentlicht hat …")
        kind, res = _published_state(acct, st["ig_container"], text, float(st["ig_publish_at"]) - MATCH_SLACK_S)
        if kind == "published":
            say("Der Reel war schon veröffentlicht – kein zweiter Beitrag.")
            return res
        if kind == "gone":
            save(ig_container=None, ig_uploaded=False, ig_publish_at=None)
        else:
            save(ig_publish_at=None)  # the container is ready but was not published

    path = _variant_file(variant)
    size = path.stat().st_size
    problems = check_file(path)["problems"]
    if problems:
        raise InstagramError("Instagram nimmt diese Datei nicht an: " + " ".join(problems),
                             code="instagram_unsupported_media")

    # 2. the container: an unexpired one from an earlier attempt is reused
    cid = st.get("ig_container")
    if cid and time.time() - float(st.get("ig_container_at") or 0) > CONTAINER_TTL_S:
        cid = None
    if cid:
        try:
            code, _ = _status(acct, cid)
        except InstagramError as e:  # a container Meta no longer knows (no publish was sent for it): start over
            if e.retryable or e.code in ("instagram_token_invalid", "instagram_permission"):
                raise
            code = None
        if code == "PUBLISHED":  # published, though no request of ours is on record: never again
            return _result(acct, _find_media(acct, text, float(st.get("ig_container_at") or 0) - MATCH_SLACK_S), cid)
        if code in ("FINISHED", "IN_PROGRESS"):
            say("Setze beim Instagram-Container des letzten Versuchs fort …")
            if code == "FINISHED" and not st.get("ig_uploaded"):
                save(ig_uploaded=True)
        else:
            cid = None
    if not cid:
        if st.get("ig_container"):
            save(ig_container=None, ig_uploaded=False, ig_publish_at=None)
        _check_cancel(cancel)
        _quota(acct)
        say("Lege den Reel bei Instagram an …")
        cid = _create_container(acct, text, bool(options.get("share_to_feed", True)))
        save(ig_container=cid, ig_container_at=time.time(), ig_uploaded=False, ig_publish_at=None)

    # 3. the bytes
    if not st.get("ig_uploaded"):
        _check_cancel(cancel)
        _upload(acct, cid, path, size, on_progress, cancel, say)
        save(ig_uploaded=True)
    elif on_progress:
        on_progress(size, size)

    # 4. Instagram's processing
    if _wait_ready(acct, cid, cancel, say, save) == "PUBLISHED":
        return _result(acct, _find_media(acct, text, float(st.get("ig_container_at") or 0) - MATCH_SLACK_S), cid)

    # 5. publish: the last moment a cancel counts; the marker is saved before the request goes out
    _check_cancel(cancel)
    save(ig_publish_at=time.time())
    say("Veröffentliche den Reel …")
    res = _publish_container(acct, cid, text, float(st["ig_publish_at"]) - MATCH_SLACK_S, say)
    try:
        save(ig_media=res.get("media_id"))
    except Exception:  # the job may be canceled by now: the post records the Reel all the same
        logging.getLogger("automate.instagram").info("saving the media id of container %s failed", cid)
    return res
