"""Social-media accounts, the connections behind them (YouTube OAuth, Instagram token/OAuth) and the one interface the
upload job uses for every platform it can publish to (the REST layer in automate/api/social.py stays thin).

Accounts are records the owner marks posts against ("als gepostet markieren"); YouTube and Instagram accounts can
also be connected (automate/youtube.py, automate/instagram.py) for direct uploads. Credentials never leave this
module and those two: `public(account)` is the only shape the API returns.

    PLATFORMS                                   {id: German label}
    public(account) -> dict                     {id, platform, label, handle, profile, connected, external_id, notes,
                                                 can_upload, connection}   connection: non-secret token facts | None
    youtube_connected() -> bool                 any YouTube account with tokens
    youtube_app(redirect_uri) -> {client_id_set, client_secret_set, client_id_hint, redirect_uri, redirect_note}
    new_state(session_hash, *, account_id, return_origin, redirect_uri, platform="youtube") -> state
                                                (random, single-use, STATE_TTL_S, bound to the session and platform)
    take_state(state, platform="youtube") -> entry | None   consumes it (a second call returns None)
    connect_url(...) -> Google consent URL ; finish_connect(entry, code) -> account row   (YouTube)
    instagram_app(redirect_uri) -> {app_id_set, …, oauth_available, oauth_note}
    instagram_connect_url(session_hash, *, account_id, return_origin, redirect_uri) -> Instagram consent URL
    finish_instagram(entry, code) -> account row
    disconnect(account_id) -> account row       revokes (YouTube, best effort) and forgets the tokens

Publishing (one flow for every platform; pipeline.enqueue_upload / _stage_upload and the API dispatch on the
account's platform):
    PUBLISHERS {platform: Publisher} ; publisher(platform) -> Publisher | None ; platforms() -> dashboard schema
    Publisher:
        platform, label, STATE_KEYS (job params a resumed attempt hands back), TIMEOUT (s, the whole call)
        option_schema() -> [{key, type: "choice" | "boolean", label, default, choices?, help?}]
        options(raw) -> dict                    validated per-platform options (OptionsError with German fields)
        job_params(options) / options_of(params) where the options live in the upload job's params
        connected(account) -> bool              can upload now
        problems(variant) -> [German]           why the platform would refuse this variant (checked before queueing)
        preview(variant) -> dict                what will be posted (title/description or caption) + problems
        carry(params, options) -> dict          state of an earlier failed/canceled attempt worth asking first
        publish(variant_id, account_id, options, *, state, on_state, on_progress, cancel, log) -> {external_id, url}
            on_state(changes) persists resume state the moment it exists; `state` hands it back on the next try
        errors() -> exception classes publish raises (each with .retryable, .code, German str())
        texts: uploading (progress prefix), resume_note(state), done(url), too_late

The OAuth `state` lives in this process only (lost on restart: the owner just clicks "verbinden" again). It is bound
to the dashboard session that asked for it, but the callback does not need that session's cookie: the provider
sends the browser to the redirect URI, which may not be the host the owner browses on, and a SameSite=Strict cookie
is not sent on that cross-site redirect anyway. The state itself (256 random bits, used once, 10 minutes, for one
platform) is the proof; the callback still checks that the session that created it is alive.
"""
import hashlib
import importlib
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


def _module(name):
    """automate.<name> at call time (the pipeline tests swap youtube/instagram for fakes in sys.modules)."""
    return importlib.import_module(f"automate.{name}")


def public(account):
    out = {k: account.get(k) for k in PUBLIC_KEYS}
    pub = PUBLISHERS.get(account.get("platform"))
    out["connected"] = bool(account.get("connected")) and pub is not None
    out["can_upload"] = out["connected"] and bool(account.get("has_credentials", True))
    out["connection"] = None
    if out["connected"] and account.get("platform") == "instagram":
        full = store.get_account(account["id"], with_credentials=True)
        out["connection"] = _module("instagram").connection_info(full)
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


def new_state(session_hash, *, account_id, return_origin, redirect_uri, platform="youtube"):
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _states_lock:
        for k in [k for k, v in _states.items() if v["expires_at"] <= now]:
            del _states[k]
        while len(_states) >= MAX_STATES:
            del _states[min(_states, key=lambda k: _states[k]["expires_at"])]
        _states[_key(state)] = {"session_hash": session_hash, "account_id": account_id, "platform": platform,
                                "return_origin": return_origin, "redirect_uri": redirect_uri,
                                "expires_at": now + STATE_TTL_S}
    return state


def take_state(state, platform="youtube"):
    """The entry of a state this server issued for `platform`, consumed (single use), or None."""
    if not isinstance(state, str) or not 20 <= len(state) <= 200:
        return None
    with _states_lock:
        entry = _states.pop(_key(state), None)
    if entry is None or entry["expires_at"] <= time.time() or entry.get("platform", "youtube") != platform:
        return None
    return entry


def _session_alive(entry, error):
    if store.get_session(entry["session_hash"]) is None:
        raise error("Deine Dashboard-Sitzung ist inzwischen abgelaufen. Bitte anmelden und erneut verbinden.",
                    code="session_expired")


def connect_url(session_hash, *, account_id, return_origin, redirect_uri):
    cid, _ = youtube._client()
    state = new_state(session_hash, account_id=account_id, return_origin=return_origin, redirect_uri=redirect_uri)
    return youtube.auth_url(cid, redirect_uri, state)


def finish_connect(entry, code):
    """Tokens from `code`, the channel they belong to, stored on the requested account (or a matching / new one)."""
    _session_alive(entry, youtube.YouTubeError)
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


# ---------------------------------------------------------------- Instagram connection

OAUTH_NEEDS_HTTPS = ("Die Anmeldung über Instagram braucht eine HTTPS-Adresse, unter der dieser Server erreichbar ist: "
                     "Meta nimmt als Weiterleitung laut allen Quellen, die wir prüfen konnten, nur HTTPS-Adressen an, "
                     "also kein http://localhost (laut Meta, bitte dort prüfen). Starte den Server dafür mit "
                     "--public-url https://… – oder füge einfach ein Token ein, das geht ganz ohne Weiterleitung.")


def instagram_app(redirect_uri):
    """What the Instagram card needs to know about the owner's Meta app(s); the ids only as hints."""
    get = store.get_secret
    app_id, meta_id = get("instagram_app_id"), get("meta_app_id")
    oauth = bool(redirect_uri)
    return {"app_id_set": bool(app_id), "app_secret_set": bool(get("instagram_app_secret")),
            "app_id_hint": security.mask(app_id) if app_id else None,
            "meta_app_id_set": bool(meta_id), "meta_app_secret_set": bool(get("meta_app_secret")),
            "meta_app_id_hint": security.mask(meta_id) if meta_id else None,
            "redirect_uri": redirect_uri, "oauth_available": oauth,
            "oauth_note": (f"Instagram schickt dich nach dem Erlauben zu {redirect_uri} zurück. Diese Adresse muss "
                           f"genau so in der Meta-App unter den OAuth-Weiterleitungs-URIs stehen. Der Vorgang muss "
                           f"innerhalb von {STATE_TTL_S // 60} Minuten abgeschlossen sein.") if oauth
            else OAUTH_NEEDS_HTTPS,
            "graph_version": _module("instagram").GRAPH_VERSION, "permissions": list(_module("instagram").SCOPES),
            "fb_permissions": list(_module("instagram").FB_PERMISSIONS)}


def instagram_connect_url(session_hash, *, account_id, return_origin, redirect_uri):
    ig = _module("instagram")
    app_id, _ = ig.instagram_app()
    state = new_state(session_hash, account_id=account_id, return_origin=return_origin, redirect_uri=redirect_uri,
                      platform="instagram")
    return ig.auth_url(app_id, redirect_uri, state)


def finish_instagram(entry, code):
    ig = _module("instagram")
    _session_alive(entry, ig.InstagramError)
    return ig.connect_oauth(code, entry["redirect_uri"], account_id=entry.get("account_id"))


def disconnect(account_id):
    account = store.get_account(account_id, with_credentials=True)
    if account is None:
        return None
    if account.get("credentials") and account["platform"] == "youtube":
        youtube.revoke(account["credentials"])  # Instagram: nothing to revoke without removing the whole app grant
    store.update_account(account_id, connected=False, credentials=None)
    for key in ("youtube.disconnected", "instagram.disconnected", "instagram.expiring"):
        store.resolve_alert(f"{key}.{account_id}")
    return store.get_account(account_id)


# ---------------------------------------------------------------- publishing: the common interface

class OptionsError(ValueError):
    """Upload options a platform can't take: German `message`, per-field `fields`, snake_case `code`."""

    def __init__(self, message, fields, code="invalid_options"):
        super().__init__(message)
        self.message, self.fields, self.code = message, fields, code


class Publisher:
    platform = label = module_name = None
    STATE_KEYS = ()
    TIMEOUT = 4 * 3600

    def module(self):
        return _module(self.module_name)

    def connected(self, account):
        """Marked connected (the API also wants the credentials present; the job finds out either way)."""
        return bool(account and account.get("platform") == self.platform and account.get("connected"))

    def option_schema(self):
        return []

    def options(self, raw):
        return {}

    def job_params(self, options):
        return {"options": options}

    def options_of(self, params):
        return dict(params.get("options") or {})

    def problems(self, variant):
        return []

    def carry(self, params, options):
        return {}

    def resume_note(self, state):
        return None

    def done(self, url):
        return f"Auf {self.label}: {url}"

    def schema(self):
        return {"platform": self.platform, "label": self.label, "options": self.option_schema()}


class YouTubePublisher(Publisher):
    platform, label, module_name = "youtube", "YouTube", "youtube"
    STATE_KEYS = ("upload_session",)
    TIMEOUT = 4 * 3600  # a big file on a slow line
    PRIVACY = {"private": "Privat", "unlisted": "Nicht gelistet", "public": "Öffentlich"}
    uploading = "Lade auf YouTube hoch"
    too_late = "Der Abbruch kam zu spät: das Video war schon vollständig bei YouTube"

    def errors(self):
        return (self.module().YouTubeError,)

    def option_schema(self):
        return [{"key": "privacy", "type": "choice", "label": "Sichtbarkeit", "default": "private",
                 "choices": [{"value": k, "label": v} for k, v in self.PRIVACY.items()],
                 "help": "Tipp: erst „Privat“ hochladen, in YouTube Studio prüfen, dann veröffentlichen."}]

    def options(self, raw):
        """{"privacy": …} (a bare string is the privacy, as before the platforms were generalised)."""
        raw = {"privacy": raw} if isinstance(raw, str) else dict(raw or {})
        unknown = sorted(set(raw) - {"privacy"})
        privacy = raw.get("privacy")
        fields = {k: "Gibt es bei YouTube nicht." for k in unknown}
        if privacy is None:
            fields["privacy"] = "Bitte auswählen."
        elif privacy not in self.PRIVACY:
            fields["privacy"] = f"Muss eins von {', '.join(self.PRIVACY.values())} sein."
        if fields:
            raise OptionsError("Sichtbarkeit: privat, nicht gelistet oder öffentlich.", fields, "invalid_privacy")
        return {"privacy": privacy}

    def job_params(self, options):
        return {"privacy": options["privacy"]}  # the shape upload jobs had before Instagram

    def options_of(self, params):
        return {"privacy": params.get("privacy") or "private"}

    def carry(self, params, options):
        """The resumable session of an earlier attempt with the same privacy: if that one actually finished, YouTube
        returns its video instead of a second copy being created."""
        if params.get("privacy") == options["privacy"] and params.get("upload_session"):
            return {"upload_session": params["upload_session"]}
        return {}

    def resume_note(self, state):
        return "Frage YouTube zuerst, wie weit der letzte Versuch kam (kein doppelter Upload)" \
            if state.get("upload_session") else None

    def preview(self, variant):
        yt = self.module()
        video = store.get_video(variant["video_id"])
        res = yt.video_resource(variant, video, "private")
        return {"title": res["snippet"]["title"], "description": res["snippet"]["description"], "problems": []}

    def publish(self, variant_id, account_id, options, *, state=None, on_state=None, on_progress=None, cancel=None,
                log=None):
        state = state or {}
        res = self.module().upload_variant(
            variant_id, account_id, options["privacy"], on_progress=on_progress, cancel=cancel,
            session=state.get("upload_session"),
            on_session=lambda uri: on_state({"upload_session": uri}) if on_state else None)
        return {**res, "external_id": res.get("video_id")}


class InstagramPublisher(Publisher):
    platform, label, module_name = "instagram", "Instagram", "instagram"
    TIMEOUT = 2 * 3600
    uploading = "Lade zu Instagram hoch"
    too_late = "Der Abbruch kam zu spät: der Reel war schon veröffentlicht"

    @property
    def STATE_KEYS(self):  # noqa: N802 - the same name as on the other publishers
        return self.module().STATE_KEYS

    def errors(self):
        return (self.module().InstagramError,)

    def option_schema(self):
        return [{"key": "share_to_feed", "type": "boolean", "label": "Auch im Feed zeigen", "default": True,
                 "help": "Der Reel erscheint dann auch in deinem Profil-Raster und im Feed deiner Follower, nicht nur "
                         "im Reels-Bereich (laut Meta)."}]

    def options(self, raw):
        if isinstance(raw, str):
            raise OptionsError("Instagram kennt keine Sichtbarkeit wie YouTube.",
                               {"privacy": "Gibt es bei Instagram nicht."})
        raw = dict(raw or {})
        fields = {k: "Gibt es bei Instagram nicht." for k in sorted(set(raw) - {"share_to_feed"})}
        share = raw.get("share_to_feed", True)
        if not isinstance(share, bool):
            fields["share_to_feed"] = "Muss an oder aus sein."
        if fields:
            raise OptionsError("Diese Einstellung gibt es bei Instagram nicht.", fields)
        return {"share_to_feed": share}

    def problems(self, variant):
        return self.module().file_problems(variant)

    def carry(self, params, options):
        """A publish an earlier attempt sent is always looked up first (never published twice); an unexpired
        container with the same options is reused (its upload and processing are done already)."""
        state = {k: params[k] for k in self.STATE_KEYS if params.get(k) is not None}
        if not state.get("ig_container"):
            return {}
        if state.get("ig_publish_at"):
            return state
        ig = self.module()
        fresh = time.time() - float(state.get("ig_container_at") or 0) < ig.CONTAINER_TTL_S
        return state if fresh and self.options_of(params) == options else {}

    def resume_note(self, state):
        if state.get("ig_publish_at"):
            return "Prüfe zuerst bei Instagram, ob der letzte Versuch den Reel schon veröffentlicht hat (kein " \
                   "doppelter Reel)"
        if state.get("ig_container"):
            return "Setze beim Instagram-Container des letzten Versuchs fort (kein zweiter Upload)"
        return None

    def preview(self, variant):
        video = store.get_video(variant["video_id"])
        ig = self.module()
        return {"caption": ig.caption(variant, video), "problems": ig.file_problems(variant)}

    def publish(self, variant_id, account_id, options, *, state=None, on_state=None, on_progress=None, cancel=None,
                log=None):
        return self.module().publish(variant_id, account_id, options, state=state, on_state=on_state,
                                     on_progress=on_progress, cancel=cancel, log=log)


PUBLISHERS = {"youtube": YouTubePublisher(), "instagram": InstagramPublisher()}


def publisher(platform):
    return PUBLISHERS.get(platform)


def platforms():
    """The dashboard's upload dialog: per platform its label and option fields."""
    return [p.schema() for p in PUBLISHERS.values()]
