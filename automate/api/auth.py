"""Login for the one owner: first-run setup (password + TOTP), login with rate limiting, the session cookie and the
account changes (password, 2FA re-enrolment).

Session: cookie `automate_session` = a random token (HttpOnly, SameSite=Strict, Secure on HTTPS, Path=/, 14 days);
the DB stores only its sha256. Every authenticated request slides the expiry (written at most every TOUCH_S). A
session is valid only while the account's 2FA is confirmed; logout deletes it; a password change or a new 2FA
secret deletes every other session.

Setup (only while no account with confirmed 2FA exists): POST /setup creates the account with a fresh TOTP secret
(inside one write transaction, so two browsers can't both create one) and sets a setup cookie; POST /setup/confirm
with a valid code enables 2FA, returns the recovery codes once and logs in. An unconfirmed account can be set up
again (frontend: reload mid-setup) by the browser holding the setup cookie, by anyone repeating its username and
password, or by anyone once it has been idle for SETUP_ABANDONED_S; `python -m automate reset-login` deletes the
account (and its sessions) so the setup starts over.

Login: constant-time checks (a dummy hash for unknown names), one generic error, failures counted per username and
per client in login_attempts (RATE_WINDOW_S window; German wait time on 429), TOTP codes can't be replayed
(totp_last_counter, compared and written in one transaction), recovery codes work once.

    authenticate(request, response=None) -> {"token_hash", "session", "user"} | None
    setup_step(user) -> "account" | "totp" | None
"""
import hmac
import math
import re
import time

from fastapi import APIRouter, Body, Request, Response

from .. import __version__, security, store
from .common import ApiError, Form, conflict, invalid, public

COOKIE = "automate_session"
SETUP_COOKIE = "automate_setup"
SESSION_TTL_S = 14 * 86400
TOUCH_S = 300
SETUP_ABANDONED_S = 15 * 60
TOTP_PENDING_S = 15 * 60
RATE_WINDOW_S = 15 * 60
LIMIT_USER, LIMIT_CLIENT, LIMIT_ACCOUNT, LIMIT_SETUP = 8, 20, 8, 10
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
BAD_LOGIN = "Benutzername, Passwort oder Code ist falsch."

router = APIRouter()
_dummy_hash = []


# ---------------------------------------------------------------- helpers

def _https(request):
    return request.url.scheme == "https"


def client_key(request):
    return request.client.host if request.client else "unknown"


def _set_cookie(request, response, name, value, max_age, path="/"):
    response.set_cookie(name, value, max_age=max_age, path=path, httponly=True, samesite="strict",
                        secure=_https(request))


def _clear_cookie(request, response, name, path="/"):
    response.delete_cookie(name, path=path, httponly=True, samesite="strict", secure=_https(request))


def setup_step(user):
    if user is None or not user.get("password_hash"):
        return "account"
    return None if user.get("totp_enabled") else "totp"


def authenticate(request, response=None):
    token = request.cookies.get(COOKIE)
    if not token or not TOKEN_RE.match(token):
        return None
    h = security.hash_token(token)
    sess = store.get_session(h)
    if sess is None:
        return None
    user = store.get_user()
    if user is None or user["id"] != sess["user_id"] or setup_step(user) is not None:
        return None
    now = time.time()
    if now - (sess.get("last_seen") or 0) > TOUCH_S:  # sliding expiry, without a write on every poll
        store.touch_session(h, now + SESSION_TTL_S, now=now)
        if response is not None:
            _set_cookie(request, response, COOKIE, token, SESSION_TTL_S)
    return {"token_hash": h, "session": sess, "user": user}


def _start_session(request, response, user):
    token, h = security.new_session_token()
    store.purge_sessions()
    store.create_session(h, user["id"], time.time() + SESSION_TTL_S, request.headers.get("user-agent"))
    _set_cookie(request, response, COOKIE, token, SESSION_TTL_S)
    return h


def _state(user, authenticated):
    step = setup_step(user)
    return {"setup_required": step is not None, "authenticated": bool(authenticated),
            "username": user["username"] if authenticated else None,
            "totp_enabled": bool(authenticated and user.get("totp_enabled")), "version": __version__,
            "setup_step": step}


def _totp_setup(username, secret):
    uri = security.otpauth_uri(username, secret)
    return {"otpauth_uri": uri, "totp_secret": secret, "qr_svg": security.qr_svg(uri)}


def _duration(seconds):
    if seconds < 60:
        return f"{seconds} Sekunden"
    m = math.ceil(seconds / 60)
    return "1 Minute" if m == 1 else f"{m} Minuten"


def _rate_limit(keys):
    """429 when any key had `limit` failures within the window; the wait is until enough of them age out."""
    now = time.time()
    for key, limit in keys:
        if store.recent_failures(key, now - RATE_WINDOW_S) < limit:
            continue
        lo, hi = 1, RATE_WINDOW_S
        while lo < hi:
            mid = (lo + hi) // 2
            if store.recent_failures(key, now - RATE_WINDOW_S + mid) < limit:
                hi = mid
            else:
                lo = mid + 1
        raise ApiError(429, "rate_limited", f"Zu viele fehlgeschlagene Versuche. Bitte warte {_duration(lo)} und "
                                            f"versuche es dann erneut.", headers={"Retry-After": str(lo)},
                       retry_after=lo)


def _record(keys, ok):
    for key, _ in keys:
        store.record_login_attempt(key, ok)


def _check_password(password, user):
    """Constant work whether or not the user exists."""
    if not _dummy_hash:
        _dummy_hash.append(security.hash_password(security.new_totp_secret()))
    stored = user["password_hash"] if user and user.get("password_hash") else _dummy_hash[0]
    ok = security.verify_password(password or "", stored)
    return ok and bool(user and user.get("password_hash"))


def second_factor(user_id, code):
    """'totp' | 'recovery' | None. Checks and consumes the TOTP step or the recovery code in one write transaction,
    so the same code can't log in twice even in parallel requests."""
    code = (code or "").strip()
    digits = "".join(code.split())
    with store.transaction():
        u = store.get_user()
        if u is None or u["id"] != user_id or not u.get("totp_secret"):
            return None
        if digits.isdigit() and len(digits) == security.TOTP_DIGITS:
            counter = security.verify_totp(u["totp_secret"], digits, u.get("totp_last_counter"))
            if counter is None:
                return None
            store.update_user(u["id"], totp_last_counter=counter)
            return "totp"
        remaining = security.use_recovery_code(code, u.get("recovery_codes") or [])
        if remaining is None:
            return None
        store.update_user(u["id"], recovery_codes=remaining)
        return "recovery"


def _username(form):
    name = form.str("username", required=True, max_len=64, label="Der Benutzername")
    if name and not re.fullmatch(r"[^\s\x00-\x1f\x7f](.*[^\s\x00-\x1f\x7f])?", name):
        form.error("username", "Ungültige Zeichen im Benutzernamen.")
    return name


def _new_password(form, field):
    pw = form.body.get(field)
    if not isinstance(pw, str) or not pw:
        form.error(field, "Bitte ein Passwort eingeben.")
        return None
    try:
        return security.hash_password(pw)
    except ValueError as e:
        form.error(field, str(e))
        return None


# ---------------------------------------------------------------- session, setup, login

@router.get("/state")
@public
def get_state(request: Request, response: Response):
    user = store.get_user()
    auth = authenticate(request, response) if setup_step(user) is None else None
    return _state(user, auth is not None)


def _may_restart(user, request, username, password, pending, now):
    if not user.get("password_hash"):
        return True  # reset-login
    cookie = request.cookies.get(SETUP_COOKIE)
    if pending and cookie and TOKEN_RE.match(cookie) and hmac.compare_digest(security.hash_token(cookie),
                                                                              pending.get("token_hash") or ""):
        return True
    if not pending or now - float(pending.get("at") or 0) > SETUP_ABANDONED_S:
        return True
    return username == user["username"] and security.verify_password(password, user["password_hash"])


@router.post("/setup")
@public
def setup(request: Request, response: Response, body: dict | None = Body(None)):
    keys = [(f"setup.client:{client_key(request)}", LIMIT_SETUP)]
    _rate_limit(keys)
    form = Form(body)
    username = _username(form)
    pw_hash = _new_password(form, "password")
    form.check()
    secret = security.new_totp_secret()
    token, token_hash = security.new_session_token()
    refused = False
    with store.transaction():
        user = store.get_user()
        now = time.time()
        if user is not None and setup_step(user) is None:
            raise conflict("setup_done", "Die Einrichtung ist schon abgeschlossen. Bitte anmelden.")
        if user is not None and not _may_restart(user, request, username, body.get("password"),
                                                 store.get_meta("setup.pending"), now):
            refused = True
        elif user is not None:
            store.update_user(user["id"], username=username, password_hash=pw_hash, totp_secret=secret,
                              totp_enabled=False, totp_last_counter=0, recovery_codes=[])
            uid = user["id"]
        else:
            uid = store.create_user(username, pw_hash, secret)
        if not refused:
            store.delete_user_sessions(uid)
            store.set_meta("setup.pending", {"token_hash": token_hash, "user_id": uid, "at": now})
    if refused:
        _record(keys, False)
        raise conflict("setup_in_progress",
                       "Die Einrichtung wurde schon in einem anderen Browser begonnen. Gib dort weiter den Code ein, "
                       "oder hier denselben Benutzernamen und dasselbe Passwort. Sonst geht es nach 15 Minuten ohne "
                       "Bestätigung wieder, oder im Terminal mit „uv run python -m automate reset-login“.")
    _set_cookie(request, response, SETUP_COOKIE, token, 3600, path="/api/setup")
    return _totp_setup(username, secret)


@router.post("/setup/confirm")
@public
def setup_confirm(request: Request, response: Response, body: dict | None = Body(None)):
    keys = [(f"setup.client:{client_key(request)}", LIMIT_SETUP)]
    _rate_limit(keys)
    form = Form(body)
    code = form.str("code", required=True, max_len=16, label="Der Code")
    form.check()
    result = None
    with store.transaction():
        user = store.get_user()
        step = setup_step(user)
        if step == "account":
            raise conflict("setup_not_started", "Bitte zuerst Benutzername und Passwort festlegen.")
        if step is None:
            raise conflict("setup_done", "Die Einrichtung ist schon abgeschlossen. Bitte anmelden.")
        counter = security.verify_totp(user["totp_secret"], code, None)
        if counter is not None:
            plain, hashed = security.new_recovery_codes()
            store.update_user(user["id"], totp_enabled=True, totp_last_counter=counter, recovery_codes=hashed)
            store.set_meta("setup.pending", None)
            user = store.get_user()
            _start_session(request, response, user)
            result = {"recovery_codes": plain}
    if result is None:
        _record(keys, False)
        raise ApiError(422, "invalid_code", "Der Code stimmt nicht. Prüfe die Uhrzeit des Handys und nimm den "
                                            "aktuellen Code.", fields={"code": "Der Code stimmt nicht."})
    _clear_cookie(request, response, SETUP_COOKIE, path="/api/setup")
    return result


@router.post("/login")
@public
def login(request: Request, response: Response, body: dict | None = Body(None)):
    form = Form(body)
    username = form.str("username", required=True, max_len=64, label="Der Benutzername")
    password = form.str("password", required=True, max_len=security.MAX_PASSWORD, strip=False, label="Das Passwort")
    code = form.str("code", required=True, max_len=64, label="Der Code")
    form.check()
    keys = [(f"login.user:{username.lower()}", LIMIT_USER), (f"login.client:{client_key(request)}", LIMIT_CLIENT)]
    _rate_limit(keys)
    user = store.get_user(username)
    ok = _check_password(password, user) and setup_step(user) is None
    factor = second_factor(user["id"], code) if ok else None
    if not factor:
        _record(keys, False)
        raise ApiError(401, "invalid_credentials", BAD_LOGIN)
    _record(keys, True)
    if factor == "recovery":
        left = len(store.get_user()["recovery_codes"] or [])
        store.raise_alert("login.recovery_code", "warning",
                          f"Zur Anmeldung wurde ein Wiederherstellungscode benutzt (noch {left} übrig). Warst du das "
                          f"nicht, ändere sofort Passwort und 2FA unter System.", {"left": left})
    _start_session(request, response, user)
    return _state(store.get_user(), True)


@router.post("/logout")
def logout(request: Request, response: Response):
    store.delete_session(request.state.auth["token_hash"])
    _clear_cookie(request, response, COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------- account

@router.post("/account/password")
def change_password(request: Request, body: dict | None = Body(None)):
    auth = request.state.auth
    user = auth["user"]
    form = Form(body)
    current = form.str("current_password", required=True, strip=False, max_len=security.MAX_PASSWORD,
                       label="Das aktuelle Passwort")
    code = form.str("code", required=True, max_len=64, label="Der Code")
    new_hash = _new_password(form, "new_password")
    form.check()
    keys = [(f"account:{user['id']}", LIMIT_ACCOUNT)]
    _rate_limit(keys)
    if not _check_password(current, user):
        _record(keys, False)
        raise invalid({"current_password": "Das aktuelle Passwort stimmt nicht."})
    if not second_factor(user["id"], code):
        _record(keys, False)
        raise invalid({"code": "Der Code stimmt nicht."})
    store.update_user(user["id"], password_hash=new_hash)
    store.delete_user_sessions(user["id"], keep=auth["token_hash"])
    return {"ok": True}


@router.post("/account/totp/reset")
def totp_reset(request: Request, body: dict | None = Body(None)):
    user = request.state.auth["user"]
    form = Form(body)
    password = form.str("password", required=True, strip=False, max_len=security.MAX_PASSWORD, label="Das Passwort")
    form.check()
    keys = [(f"account:{user['id']}", LIMIT_ACCOUNT)]
    _rate_limit(keys)
    if not _check_password(password, user):
        _record(keys, False)
        raise invalid({"password": "Das Passwort stimmt nicht."})
    secret = security.new_totp_secret()
    store.set_meta("totp.pending", {"user_id": user["id"], "secret": secret, "at": time.time()})
    return _totp_setup(user["username"], secret)


@router.post("/account/totp/confirm")
def totp_confirm(request: Request, body: dict | None = Body(None)):
    auth = request.state.auth
    user = auth["user"]
    form = Form(body)
    code = form.str("code", required=True, max_len=16, label="Der Code")
    form.check()
    keys = [(f"account:{user['id']}", LIMIT_ACCOUNT)]
    _rate_limit(keys)
    pending = store.get_meta("totp.pending") or {}
    if pending.get("user_id") != user["id"] or time.time() - float(pending.get("at") or 0) > TOTP_PENDING_S:
        raise conflict("totp_not_started", "Bitte zuerst „2FA neu einrichten“ starten (der QR-Code gilt 15 Minuten).")
    counter = security.verify_totp(pending["secret"], code, None)
    if counter is None:
        _record(keys, False)
        raise ApiError(422, "invalid_code", "Der Code stimmt nicht.", fields={"code": "Der Code stimmt nicht."})
    plain, hashed = security.new_recovery_codes()
    with store.transaction():
        store.update_user(user["id"], totp_secret=pending["secret"], totp_enabled=True, totp_last_counter=counter,
                          recovery_codes=hashed)
        store.set_meta("totp.pending", None)
        store.delete_user_sessions(user["id"], keep=auth["token_hash"])
    return {"recovery_codes": plain}
