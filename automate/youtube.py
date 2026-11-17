"""YouTube: OAuth 2.0 with the owner's own Google Cloud OAuth client ("Web application" type, client id + secret
entered in the dashboard) and the resumable upload of a rendered variant (YouTube Data API v3 `videos.insert`).
Stdlib HTTP only.

Every HTTP request goes through `transport(method, url, headers=, body=, timeout=)` -> HttpResponse; it never
follows redirects (the upload protocol answers 308) and returns 4xx/5xx as responses. Tests replace it
(`mock.patch.object(youtube, "transport", fake)`) to fake Google completely; `_sleep` is the retry backoff.

Tokens live in social_accounts.credentials ({access_token, refresh_token, expires_at, scope, token_type}); they are
never returned by the API and never logged.

    SCOPES, AUTH_URL, CALLBACK_PATH
    class YouTubeError(Exception)   .retryable: bool, .code: snake_case; str(e) is German, fit for the dashboard
    auth_url(client_id, redirect_uri, state) -> consent URL (offline access, prompt=consent, incremental scopes)
    exchange_code(code, redirect_uri) -> credentials        (raises YouTubeError, e.g. missing upload scope)
    fetch_channel(access_token) -> {id, title, handle}      channels?part=snippet&mine=true
    access_token(account_id, force=False) -> str            refreshes ahead of expiry; `invalid_grant` marks the
                                                            account disconnected and raises an alert
    revoke(credentials) -> bool                             best effort
    video_resource(variant, video, privacy, title=None, description=None) -> {snippet, status}
    upload_variant(variant_id, account_id, privacy, *, title=None, description=None, on_progress=None,
                   cancel=None) -> {"video_id", "url"}
        Resumable upload in CHUNK pieces (Content-Range); a dropped connection or a 5xx asks the session how far
        it got and continues from there (backoff, MAX_RETRIES in a row), a lost session starts a new one, a 401
        refreshes the token. `on_progress(done_bytes, total_bytes)` after every confirmed chunk; `cancel` (a
        threading.Event) is checked between chunks and while waiting.
"""
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from . import paths, store

log = logging.getLogger("automate.youtube")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPES = (SCOPE_UPLOAD, SCOPE_READONLY)
CALLBACK_PATH = "/api/social/youtube/callback"
PRIVACY = ("private", "unlisted", "public")
CHUNK = 8 * 1024 * 1024  # a multiple of 256 KiB, as the protocol wants for every chunk but the last
MAX_RETRIES = 8  # failed attempts in a row before an upload gives up (retryable)
MAX_SESSIONS = 3  # upload sessions per call (a session can expire or be lost)
TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
GAMING_CATEGORY = "20"
REFRESH_AHEAD_S = 120

_locks = {}
_locks_guard = threading.Lock()


class YouTubeError(Exception):
    """German message; `retryable` = trying again later can work (network, 5xx, quota); `code` for the API."""

    def __init__(self, message, *, retryable=False, code="youtube_error"):
        super().__init__(message)
        self.retryable, self.code = retryable, code


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
        return None  # a 3xx (the upload protocol's 308) comes back as a response


_opener = urllib.request.build_opener(_NoRedirect)


def transport(method, url, *, headers=None, body=None, timeout=60):
    """One HTTP request. Network failures raise OSError (URLError, timeouts, resets); HTTP errors are responses."""
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

REASONS = {
    "quotaExceeded": ("Das Tageskontingent der YouTube-API deines Google-Projekts ist aufgebraucht (ein Upload kostet "
                      "viel davon). Es wird um Mitternacht pazifischer Zeit zurückgesetzt.", True),
    "rateLimitExceeded": ("YouTube bremst gerade (zu viele Anfragen). Bitte später erneut versuchen.", True),
    "userRateLimitExceeded": ("YouTube bremst gerade (zu viele Anfragen). Bitte später erneut versuchen.", True),
    "uploadLimitExceeded": ("YouTube nimmt von diesem Kanal gerade keine weiteren Uploads an (Upload-Limit). Bitte "
                            "später erneut versuchen.", True),
    "insufficientPermissions": ("Der Zugriff reicht nicht zum Hochladen. Bitte das Konto unter Konten trennen und neu "
                                "verbinden und dabei alle Häkchen setzen.", False),
    "forbidden": ("YouTube hat den Upload abgelehnt (keine Berechtigung für diesen Kanal).", False),
    "youtubeSignupRequired": ("Dieses Google-Konto hat noch keinen YouTube-Kanal.", False),
    "invalidTitle": ("YouTube hat den Titel abgelehnt.", False),
    "invalidDescription": ("YouTube hat die Beschreibung abgelehnt.", False),
    "invalidTags": ("YouTube hat die Tags abgelehnt.", False),
    "defaultLanguageNotSet": ("YouTube verlangt eine Standardsprache für das Video.", False),
}


def _api_error(r, what="YouTube"):
    """A YouTubeError for an error response (German; Google's own text only as a short hint)."""
    data = r.json() or {}
    err = data.get("error") if isinstance(data, dict) else None
    reason, detail = None, ""
    if isinstance(err, dict):
        reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)]
        reason = next((x for x in reasons if x in REASONS), reasons[0] if reasons else None)
        detail = str(err.get("message") or "")[:200]
    elif isinstance(err, str):
        reason, detail = err, str(data.get("error_description") or "")[:200]
    if reason in REASONS:
        msg, retry = REASONS[reason]
        return YouTubeError(msg, retryable=retry, code=f"youtube_{reason}")
    retry = r.status >= 500 or r.status == 429
    hint = f": {detail}" if detail else ""
    return YouTubeError(f"{what} hat mit Fehler {r.status} geantwortet{hint}", retryable=retry,
                        code="youtube_http_error")


# ---------------------------------------------------------------- OAuth

def _client():
    cid, secret = store.get_secret("youtube_client_id"), store.get_secret("youtube_client_secret")
    if not cid or not secret:
        raise YouTubeError("Der Google-OAuth-Client ist nicht eingerichtet (Client-ID und Client-Secret unter Konten "
                           "eintragen).", code="youtube_app_missing")
    return cid, secret


def auth_url(client_id, redirect_uri, state):
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def _token_request(form):
    body = urllib.parse.urlencode(form).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    last = None
    for attempt in range(3):
        try:
            r = transport("POST", TOKEN_URL, headers=headers, body=body, timeout=30)
        except OSError as e:
            last = YouTubeError(f"Google ist nicht erreichbar ({e.__class__.__name__}). Bitte später erneut "
                                f"versuchen.", retryable=True, code="youtube_unreachable")
        else:
            if r.status < 500:
                return r
            last = _api_error(r, "Google")
        _sleep(1.5 * (attempt + 1))
    raise last


def _credentials(data, old=None):
    if not isinstance(data, dict) or not data.get("access_token"):
        raise YouTubeError("Google hat kein Zugriffstoken geliefert.", code="youtube_token_missing")
    now = time.time()
    return {"access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or (old or {}).get("refresh_token"),
            "expires_at": now + float(data.get("expires_in") or 3600),
            "scope": data.get("scope") or (old or {}).get("scope") or "",
            "token_type": data.get("token_type") or "Bearer"}


def exchange_code(code, redirect_uri):
    cid, secret = _client()
    r = _token_request({"code": code, "client_id": cid, "client_secret": secret, "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code"})
    if r.status != 200:
        data = r.json() or {}
        if isinstance(data, dict) and data.get("error") == "redirect_uri_mismatch":
            raise YouTubeError(f"Die Weiterleitungs-URI {redirect_uri} ist im Google-OAuth-Client nicht eingetragen.",
                               code="youtube_redirect_mismatch")
        if isinstance(data, dict) and data.get("error") == "invalid_client":
            raise YouTubeError("Google kennt diese Client-ID oder dieses Client-Secret nicht. Bitte unter Konten "
                               "prüfen.", code="youtube_invalid_client")
        if isinstance(data, dict) and data.get("error") == "invalid_grant":
            raise YouTubeError("Der Anmeldecode von Google ist abgelaufen oder wurde schon benutzt. Bitte erneut "
                               "verbinden.", code="youtube_invalid_grant")
        raise _api_error(r, "Google")
    creds = _credentials(r.json())
    if SCOPE_UPLOAD not in creds["scope"].split():
        raise YouTubeError("Beim Zustimmen fehlte das Häkchen für „Videos hochladen“. Bitte erneut verbinden und alle "
                           "Häkchen setzen.", code="youtube_scope_missing")
    if not creds["refresh_token"]:
        log.warning("Google returned no refresh token: the connection ends when the access token expires")
    return creds


def fetch_channel(token):
    try:
        r = transport("GET", f"{API_URL}/channels?part=snippet&mine=true",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=30)
    except OSError as e:
        raise YouTubeError(f"YouTube ist nicht erreichbar ({e.__class__.__name__}).", retryable=True,
                           code="youtube_unreachable") from None
    if r.status != 200:
        raise _api_error(r)
    items = (r.json() or {}).get("items") or []
    if not items:
        raise YouTubeError("Dieses Google-Konto hat keinen YouTube-Kanal. Bitte beim Verbinden den Kanal wählen, auf "
                           "den hochgeladen werden soll.", code="youtube_no_channel")
    ch = items[0]
    sn = ch.get("snippet") or {}
    return {"id": ch.get("id"), "title": sn.get("title") or ch.get("id"), "handle": sn.get("customUrl") or None}


def _disconnected(account, why):
    store.update_account(account["id"], connected=False, credentials=None)
    msg = (f"YouTube-Konto „{account['label']}“ ist nicht mehr verbunden ({why}). Bitte unter Konten neu verbinden.")
    store.raise_alert(f"youtube.disconnected.{account['id']}", "warning", msg, {"account_id": account["id"]})
    return YouTubeError(msg, code="youtube_disconnected")


def access_token(account_id, force=False):
    """A valid access token for the account (refreshed when it expires within REFRESH_AHEAD_S, or when `force`)."""
    with _lock(account_id):
        account = store.get_account(account_id, with_credentials=True)
        if account is None:
            raise YouTubeError("Das Konto gibt es nicht mehr.", code="not_found")
        creds = account.get("credentials") or {}
        if account["platform"] != "youtube" or not account["connected"] or not creds.get("access_token"):
            raise YouTubeError(f"„{account['label']}“ ist nicht mit YouTube verbunden.", code="not_connected")
        if not force and float(creds.get("expires_at") or 0) - time.time() > REFRESH_AHEAD_S:
            return creds["access_token"]
        if not creds.get("refresh_token"):
            raise _disconnected(account, "der Zugriff ist abgelaufen")
        cid, secret = _client()
        r = _token_request({"client_id": cid, "client_secret": secret, "refresh_token": creds["refresh_token"],
                            "grant_type": "refresh_token"})
        data = r.json() or {}
        if r.status != 200:
            if isinstance(data, dict) and data.get("error") == "invalid_grant":
                raise _disconnected(account, "Google hat den Zugriff widerrufen oder er ist abgelaufen")
            raise _api_error(r, "Google")
        new = _credentials(data, creds)
        store.update_account(account_id, credentials=new)
        store.resolve_alert(f"youtube.disconnected.{account_id}")
        return new["access_token"]


def revoke(creds):
    token = (creds or {}).get("refresh_token") or (creds or {}).get("access_token")
    if not token:
        return False
    try:
        r = transport("POST", REVOKE_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
                      body=urllib.parse.urlencode({"token": token}).encode("ascii"), timeout=15)
        return r.status == 200
    except OSError:
        return False


# ---------------------------------------------------------------- metadata

def clean_title(title):
    """YouTube titles: at most 100 characters, no '<' or '>', not empty."""
    t = " ".join(str(title or "").replace("<", "").replace(">", "").split())
    if len(t) > TITLE_MAX:
        cut = t[:TITLE_MAX]
        space = cut.rfind(" ")
        t = (cut[:space] if space >= TITLE_MAX * 0.6 else cut).rstrip(" ,;:-–—|")
    return t or "GTA 6"


def _cut_bytes(text, limit):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[:limit].decode("utf-8", errors="ignore")
    br = max(cut.rfind("\n"), cut.rfind(" "))
    return (cut[:br] if br >= len(cut) * 0.8 else cut).rstrip()


def clean_description(description, title=""):
    """At most 5000 bytes, no angle brackets; #Shorts added once unless title or description already has it (the
    manifest's description ends with its hashtags line, so it joins that line)."""
    d = str(description or "").replace("<", "‹").replace(">", "›").strip()
    has = "#shorts" in f"{title} {d}".lower()
    tag = "" if has else "#Shorts"
    d = _cut_bytes(d, DESCRIPTION_MAX_BYTES - (len(tag) + 2 if tag else 0))
    if tag:
        last = d.rsplit("\n", 1)[-1].split()
        if last and all(w.startswith("#") for w in last):
            d = f"{d} {tag}"
        else:
            d = f"{d}\n\n{tag}" if d else tag
    return d


def _tags(hashtags):
    out, used = [], 0
    for h in hashtags or []:
        t = str(h).lstrip("#").strip().replace("<", "").replace(">", "")
        if not t or t.lower() in (x.lower() for x in out):
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if used + cost > TAGS_MAX_CHARS:
            break
        out.append(t)
        used += cost
    return out


def video_resource(variant, video, privacy, title=None, description=None):
    if privacy not in PRIVACY:
        raise YouTubeError(f"Unbekannte Sichtbarkeit „{privacy}“ (private, unlisted oder public).", code="invalid")
    t = clean_title(title if title is not None else variant.get("title") or (video or {}).get("title"))
    desc = description if description is not None else variant.get("description") or (video or {}).get("description")
    snippet = {"title": t, "description": clean_description(desc, t), "categoryId": GAMING_CATEGORY}
    tags = _tags(variant.get("hashtags"))
    if tags:
        snippet["tags"] = tags
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if (video or {}).get("lang") not in (None, "", "en"):
        status["containsSyntheticMedia"] = True  # AI dub in cloned voices: YouTube's altered-content disclosure
    return {"snippet": snippet, "status": status}


# ---------------------------------------------------------------- upload

def _variant_file(variant):
    """The variant's MP4 - only inside the exports folder (no traversal, no symlink out of it)."""
    root = paths.exports_dir().resolve()
    p = paths.from_rel(variant.get("file") or "").resolve()
    if p == root or root not in p.parents or p.suffix.lower() != ".mp4" or not p.is_file():
        raise YouTubeError("Die MP4-Datei dieser Variante fehlt (gelöscht oder verschoben?).", code="file_missing")
    return p


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise YouTubeError("Upload abgebrochen.", code="canceled")


class _Transient(Exception):
    """A 5xx/429 from the upload endpoint: retried like a dropped connection."""


def _start_session(account_id, resource, size):
    """POST the metadata -> the session URI (the `Location` header). OSError/_Transient: try again later."""
    url = f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status"
    force = False
    for _ in range(3):
        headers = {"Authorization": f"Bearer {access_token(account_id, force=force)}",
                   "Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(size),
                   "X-Upload-Content-Type": "video/mp4"}
        r = transport("POST", url, headers=headers, body=json.dumps(resource).encode("utf-8"), timeout=60)
        if r.status == 401 and not force:
            force = True
            continue
        if r.status == 400 and b"containsSyntheticMedia" in r.body and "containsSyntheticMedia" in resource["status"]:
            # an API revision that doesn't know the disclosure field: upload without it rather than not at all
            resource = {**resource, "status": {k: v for k, v in resource["status"].items()
                                               if k != "containsSyntheticMedia"}}
            continue
        if r.status in (200, 201) and r.headers.get("location"):
            return r.headers["location"]
        break
    if r.status >= 500 or r.status == 429:
        raise _Transient(r.status)
    raise _api_error(r)


def _put(session, account_id, data, content_range, force=False):
    headers = {"Authorization": f"Bearer {access_token(account_id, force=force)}", "Content-Length": str(len(data)),
               "Content-Range": content_range, "Content-Type": "video/mp4"}
    return transport("PUT", session, headers=headers, body=data, timeout=300)


def _next_offset(r):
    """The first byte the server still needs after a 308 (no Range header: nothing arrived yet)."""
    rng = r.headers.get("range") or ""
    try:
        return int(rng.rsplit("-", 1)[1]) + 1 if rng.startswith("bytes=") else 0
    except (IndexError, ValueError):
        return 0


def _done(r):
    data = r.json() or {}
    vid = data.get("id") if isinstance(data, dict) else None
    if not vid:
        raise YouTubeError("YouTube hat den Upload ohne Video-ID beendet.", retryable=True, code="youtube_no_id")
    return {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}"}


def upload_variant(variant_id, account_id, privacy, *, title=None, description=None, on_progress=None, cancel=None):
    variant = store.get_variant(variant_id)
    if variant is None:
        raise YouTubeError("Diese Variante gibt es nicht mehr.", code="not_found")
    video = store.get_video(variant["video_id"])
    path = _variant_file(variant)
    size = path.stat().st_size
    if size <= 0:
        raise YouTubeError("Die MP4-Datei ist leer.", code="file_missing")
    resource = video_resource(variant, video, privacy, title, description)
    _check_cancel(cancel)
    access_token(account_id)  # fails early when the account isn't connected

    def progress(done):
        if on_progress:
            on_progress(done, size)

    sessions = failures = 0
    session, offset, ask = None, 0, False
    with open(path, "rb") as f:
        while True:
            _check_cancel(cancel)
            r = None
            try:
                if session is None:
                    sessions += 1
                    if sessions > MAX_SESSIONS:
                        raise YouTubeError("YouTube hat die Upload-Sitzung mehrfach verloren. Bitte später erneut "
                                           "versuchen.", retryable=True, code="youtube_session_lost")
                    session, offset, ask = _start_session(account_id, resource, size), 0, False
                if ask or offset >= size:  # after a failure, or all sent but unconfirmed: where did it stop?
                    r = _put(session, account_id, b"", f"bytes */{size}")
                else:
                    f.seek(offset)
                    data = f.read(min(CHUNK, size - offset))
                    r = _put(session, account_id, data, f"bytes {offset}-{offset + len(data) - 1}/{size}")
            except (OSError, _Transient) as e:
                log.info("upload of variant %s: %s, resuming", variant_id, e.__class__.__name__)
            if r is not None and r.status in (200, 201):
                progress(size)
                return _done(r)
            if r is not None and r.status == 308:
                new = _next_offset(r)
                if new > offset:
                    failures = 0
                elif not ask:  # a chunk that moved nothing counts as a failed attempt
                    failures += 1
                    if failures > MAX_RETRIES:
                        raise YouTubeError("YouTube nimmt die Daten nicht an. Bitte später erneut versuchen.",
                                           retryable=True, code="youtube_stalled")
                offset, ask = new, False
                progress(offset)
                continue
            if r is not None and r.status in (404, 410):  # the session expired (about a week) or is gone
                session = None
                continue
            if r is not None and r.status == 401:
                access_token(account_id, force=True)
            elif r is not None and not (r.status >= 500 or r.status == 429):
                raise _api_error(r)
            failures += 1
            if failures > MAX_RETRIES:
                raise YouTubeError("Der Upload zu YouTube bricht immer wieder ab. Bitte später erneut versuchen.",
                                   retryable=True, code="youtube_unreachable")
            if r is None or r.status != 401:
                _sleep(min(64.0, 2 ** (failures - 1)) + random.random(), cancel)
            ask = session is not None
