"""Konten: social-media accounts and the YouTube connection (Google OAuth client, connect, callback, disconnect).

The OAuth callback is a public route (Google's redirect carries no session cookie: see automate/social.py); it is
validated by the single-use `state` and answers with a small German HTML page that sends the browser back to the
dashboard origin the owner started from: `/#/konten?youtube=connected` or `?youtube=error&message=…`.
"""
import html
import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse

from .. import settings, social, store, youtube
from .common import Form, conflict, invalid, mapped, not_found, public

log = logging.getLogger("automate.api")
router = APIRouter()

CLIENT_ID_RE = re.compile(r"^[\w.\-]{10,200}\.apps\.googleusercontent\.com$")


def _account(account_id):
    a = store.get_account(account_id)
    if a is None:
        raise not_found("Dieses Konto gibt es nicht.")
    return a


def _profile_ok(form):
    profile = form.str("profile", max_len=64, nullable=True)
    if profile and store.get_profile(profile) is None:
        form.error("profile", f"Unbekanntes Profil „{profile}“.")
    return profile or None


def redirect_uri(request):
    return request.app.state.config.redirect_uri()


@router.get("/social/accounts")
def list_accounts():
    items = [social.public(a) for a in store.list_accounts()]
    return {"items": items, "total": len(items)}


@router.post("/social/accounts")
def create_account(body: dict | None = Body(None)):
    form = Form(body)
    platform = form.choice("platform", tuple(social.PLATFORMS), required=True,
                           labels=tuple(social.PLATFORMS.values()))
    label = form.str("label", required=True, max_len=100, label="Die Bezeichnung")
    handle = form.str("handle", max_len=100)
    notes = form.str("notes", max_len=2000)
    profile = _profile_ok(form)
    form.check()
    aid = store.add_account(platform, label, handle=handle or None, profile=profile, notes=notes or None)
    return social.public(store.get_account(aid))


@router.patch("/social/accounts/{account_id}")
def update_account(account_id: int, body: dict | None = Body(None)):
    _account(account_id)
    form = Form(body)
    fields = {}
    if form.has("label"):
        fields["label"] = form.str("label", required=True, max_len=100, label="Die Bezeichnung")
    for key, n in (("handle", 100), ("notes", 2000)):
        if form.has(key):
            fields[key] = form.str(key, max_len=n, nullable=True) or None
    if form.has("profile"):
        fields["profile"] = _profile_ok(form)
    form.check()
    a = store.update_account(account_id, **fields) if fields else _account(account_id)
    return social.public(a)


@router.delete("/social/accounts/{account_id}")
def delete_account(account_id: int):
    social.disconnect(account_id)  # revoke the tokens at Google first (best effort)
    if not store.delete_account(account_id):
        raise not_found("Dieses Konto gibt es nicht.")
    return {"ok": True}


@router.post("/social/accounts/{account_id}/disconnect")
def disconnect(account_id: int):
    _account(account_id)
    return social.public(social.disconnect(account_id))


@router.get("/social/youtube/app")
def get_app(request: Request):
    return social.youtube_app(redirect_uri(request))


@router.put("/social/youtube/app")
def put_app(request: Request, body: dict | None = Body(None)):
    form = Form(body)
    if not form.has("client_id") and not form.has("client_secret"):
        form.error("client_id", "Client-ID und/oder Client-Secret angeben.")
    changes = {}
    for field, secret in (("client_id", "youtube_client_id"), ("client_secret", "youtube_client_secret")):
        if not form.has(field):
            continue
        value = form.str(field, max_len=300, nullable=True) or None
        if value:
            try:
                value = settings.validate_secret(secret, value)
            except settings.SettingsError as e:
                form.error(field, next(iter(e.errors.values())))
        if field == "client_id" and value and not CLIENT_ID_RE.match(value):
            form.error(field, "Sieht nicht wie eine Google-Client-ID aus (…apps.googleusercontent.com).")
        changes[secret] = value
    form.check()
    for name, value in changes.items():
        store.set_secret(name, value)
    return social.youtube_app(redirect_uri(request))


@router.post("/social/youtube/connect")
def connect(request: Request, body: dict | None = Body(None)):
    form = Form(body)
    account_id = form.int("account_id", nullable=True)
    form.check()
    if account_id is not None:
        a = _account(account_id)
        if a["platform"] != "youtube":
            raise invalid({"account_id": "Nur YouTube-Konten lassen sich verbinden."})
    if not store.get_secret("youtube_client_id") or not store.get_secret("youtube_client_secret"):
        raise conflict("youtube_app_missing", "Bitte zuerst Client-ID und Client-Secret des Google-OAuth-Clients "
                                              "eintragen.")
    origin = f"{request.url.scheme}://{request.headers.get('host')}"  # the Host passed the allow-list
    with mapped():
        url = social.connect_url(request.state.auth["token_hash"], account_id=account_id, return_origin=origin,
                                 redirect_uri=redirect_uri(request))
    return {"auth_url": url}


def _page(target, ok, message):
    title = "YouTube verbunden" if ok else "YouTube-Verbindung fehlgeschlagen"
    t = html.escape(target, quote=True)
    body = (f"<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<meta http-equiv=\"refresh\" content=\"1; url={t}\"><title>{html.escape(title)}</title>"
            f"<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:36em;margin:4em auto;padding:0 1em;"
            f"color:#222}}a{{color:#c45500}}</style></head><body><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(message)}</p><p><a href=\"{t}\">Zurück zum Dashboard</a></p></body></html>")
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                                          "form-action 'none'; frame-ancestors 'none'"}
    return HTMLResponse(body, status_code=200 if ok else 400, headers=headers)


@router.get("/social/youtube/callback")
@public
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    entry = social.take_state(state) if state else None
    origin = (entry or {}).get("return_origin") or f"{request.url.scheme}://{request.headers.get('host')}"

    def fail(message):
        return _page(f"{origin}/#/konten?youtube=error&message={quote(message)}", False, message)

    if entry is None:
        return fail("Der Verbindungslink ist abgelaufen oder wurde schon benutzt. Bitte im Dashboard erneut „Mit "
                    "YouTube verbinden“ klicken.")
    if error:
        return fail("Du hast den Zugriff bei Google nicht erlaubt." if error == "access_denied"
                    else f"Google hat die Anmeldung abgebrochen ({error[:60]}).")
    if not code or len(code) > 2000:
        return fail("Google hat keinen Anmeldecode geschickt.")
    try:
        account = social.finish_connect(entry, code)
    except youtube.YouTubeError as e:
        return fail(str(e))
    except Exception:
        log.exception("YouTube OAuth callback failed")
        return fail("Interner Fehler beim Verbinden. Details stehen im Server-Log.")
    label = account.get("label") or "YouTube"
    return _page(f"{origin}/#/konten?youtube=connected&account={account['id']}", True,
                 f"„{label}“ ist jetzt verbunden. Du wirst zum Dashboard zurückgeleitet …")
