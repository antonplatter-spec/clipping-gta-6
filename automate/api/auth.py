"""Login for the one owner: first-run setup (password + TOTP), login with rate limiting, the session cookie and the
account changes (password, 2FA re-enrolment).

Session: cookie `automate_session` = a random token (HttpOnly, SameSite=Strict, Secure on HTTPS, Path=/, 14 days);
the DB stores only its sha256. Every authenticated request slides the expiry (written at most every TOUCH_S). A
session is valid only while the account's 2FA is confirmed; logout deletes it; a password change or a new 2FA
secret deletes every other session.

Setup (only while no account with confirmed 2FA exists) needs the one-time setup code: whoever reaches the server
first must not own the install (with --host 0.0.0.0 that may be anyone on the Wi-Fi). `python -m automate` issues a
fresh code whenever it starts with the setup open, and `reset-login` issues one; only its sha256 is stored (meta
"setup.token"), the code itself is printed in the terminal and put in the dashboard link the server opens. POST
/setup {username, password, token} creates (or, for the code's holder, re-creates) the account with a fresh TOTP
secret inside one write transaction and sets a setup cookie; POST /setup/confirm {code, token} needs that cookie as
well, enables 2FA, returns the recovery codes once, logs in and invalidates the setup code. Failures (wrong setup
code, wrong TOTP code, a setup request after setup) count per client and globally. Keys in the repo's .env are
imported only once the setup is complete (importer.run), so nothing is handed to an account before it is proven.

Login: constant-time checks (a dummy hash for unknown names), one generic error, failures counted per username and
per client in login_attempts (RATE_WINDOW_S window; German wait time on 429), TOTP codes can't be replayed
(totp_last_counter, compared and written in one transaction), recovery codes work once; anything odd in the second
factor counts as a failed attempt.

    authenticate(request, response=None) -> {"token_hash", "session", "user"} | None
    setup_step(user) -> "account" | "totp" | None ; setup_open() -> bool
    issue_setup_token() -> the new setup code (plain; print it, never log it) ; clear_setup_token()
    setup_url(base_url, token) -> the dashboard link that carries the code
"""
import base64
import hashlib
import hmac
import logging
import math
import re
import secrets
import time

from fastapi import APIRouter, Body, Request, Response

from .. import __version__, security, store
from .common import ApiError, Form, conflict, invalid, public

log = logging.getLogger("automate.api")

COOKIE = "automate_session"
SETUP_COOKIE = "automate_setup"
SESSION_TTL_S = 14 * 86400
TOUCH_S = 300
TOTP_PENDING_S = 15 * 60
RATE_WINDOW_S = 15 * 60
LIMIT_USER, LIMIT_CLIENT, LIMIT_ACCOUNT, LIMIT_SETUP, LIMIT_SETUP_ALL = 8, 20, 8, 10, 40
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
CODE_RE = re.compile(r"[0-9]{6}")  # ASCII digits only: str.isdigit() also takes e.g. Arabic-Indic digits
BAD_LOGIN = "Benutzername, Passwort oder Code ist falsch."
SETUP_TOKEN_META = "setup.token"
BAD_SETUP_TOKEN = ("Der Einrichtungs-Code stimmt nicht. Er steht im Terminal, in dem der Server läuft (Zeile "
                   "„Einrichtungs-Code“); nach jedem Neustart gilt ein neuer.")
NO_SETUP_TOKEN = ("Es gibt keinen gültigen Einrichtungs-Code. Starte den Server mit „uv run python -m automate“ neu – "
                  "der Code steht dann im Terminal.")

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


def setup_open():
    return setup_step(store.get_user()) is not None


def _norm_setup_token(token):
    return re.sub(r"[^A-Z0-9]", "", str(token or "").upper())


def _setup_token_hash(token):
    return hashlib.sha256(_norm_setup_token(token).encode("ascii", "ignore")).hexdigest()


def issue_setup_token():
    """A new setup code (100 random bits, typed from the terminal as XXXX-XXXX-XXXX-XXXX-XXXX); replaces any older
    one. Only its hash is stored."""
    raw = base64.b32encode(secrets.token_bytes(15)).decode("ascii")[:20]
    store.set_meta(SETUP_TOKEN_META, {"hash": _setup_token_hash(raw), "at": time.time()})
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def clear_setup_token():
    store.set_meta(SETUP_TOKEN_META, None)


def setup_url(base_url, token):
    return f"{base_url.rstrip('/')}/#/einrichtung?token={token}"


def _setup_token_ok(token):
    stored = (store.get_meta(SETUP_TOKEN_META) or {}).get("hash")
    if not stored:
        return None  # nothing to compare with: the server wasn't started through `python -m automate`
    return hmac.compare_digest(stored, _setup_token_hash(token)) and len(_norm_setup_token(token)) == 20


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
            "setup_step": step, "setup_token_required": step is not None}


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
    so the same code can't log in twice even in parallel requests. Any error in here is a failed check (never a 500
    that would tell a correct password apart from a wrong one)."""
    try:
        return _second_factor(user_id, code)
    except Exception:
        log.warning("second factor check failed", exc_info=True)
        return None


def _second_factor(user_id, code):
    code = (code or "").strip()
    digits = "".join(code.split())
    with store.transaction():
        u = store.get_user()
        if u is None or u["id"] != user_id or not u.get("totp_secret"):
            return None
        if CODE_RE.fullmatch(digits):
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


def _setup_keys(request):
    return [(f"setup.client:{client_key(request)}", LIMIT_SETUP), ("setup.all", LIMIT_SETUP_ALL)]


def _setup_done(keys):
    _record(keys, False)
    return conflict("setup_done", "Die Einrichtung ist schon abgeschlossen. Bitte anmelden.")


def _check_setup_token(form, keys):
    """The setup code from the body; a wrong or missing one is a counted failure (403)."""
    token = form.str("token", max_len=100)
    ok = _setup_token_ok(token) if token else False
    if ok is None:
        raise ApiError(409, "setup_token_missing", NO_SETUP_TOKEN)
    if not ok:
        _record(keys, False)
        raise ApiError(403, "setup_token_invalid", BAD_SETUP_TOKEN,
                       fields={"token": "Der Einrichtungs-Code stimmt nicht." if token else
                               "Bitte den Einrichtungs-Code aus dem Terminal eingeben."})


@router.post("/setup")
@public
def setup(request: Request, response: Response, body: dict | None = Body(None)):
    keys = _setup_keys(request)
    _rate_limit(keys)
    if not setup_open():  # before any password hashing: a finished install answers at once (and counts it)
        raise _setup_done(keys)
    form = Form(body)
    username = _username(form)
    pw = form.body.get("password")
    if not isinstance(pw, str) or not pw:
        form.error("password", "Bitte ein Passwort eingeben.")
    elif len(pw) < security.MIN_PASSWORD:  # hash_password's length rules, checked before any scrypt work
        form.error("password", f"Das Passwort muss mindestens {security.MIN_PASSWORD} Zeichen lang sein.")
    elif len(pw) > security.MAX_PASSWORD:
        form.error("password", f"Das Passwort darf höchstens {security.MAX_PASSWORD} Zeichen lang sein.")
    form.check()
    _check_setup_token(form, keys)
    pw_hash = _new_password(form, "password")  # scrypt only for the holder of the setup code
    form.check()
    secret = security.new_totp_secret()
    token, token_hash = security.new_session_token()
    with store.transaction():
        user = store.get_user()
        if user is not None and setup_step(user) is None:
            raise _setup_done(keys)
        if user is not None:  # the code's holder starts over (reload mid-setup, another browser)
            store.update_user(user["id"], username=username, password_hash=pw_hash, totp_secret=secret,
                              totp_enabled=False, totp_last_counter=0, recovery_codes=[])
            uid = user["id"]
        else:
            uid = store.create_user(username, pw_hash, secret)
        store.delete_user_sessions(uid)
        store.set_meta("setup.pending", {"token_hash": token_hash, "user_id": uid, "at": time.time()})
    _set_cookie(request, response, SETUP_COOKIE, token, 3600, path="/api/setup")
    return _totp_setup(username, secret)


def _setup_cookie_ok(request, pending):
    cookie = request.cookies.get(SETUP_COOKIE)
    return bool(pending and cookie and TOKEN_RE.match(cookie)
                and hmac.compare_digest(security.hash_token(cookie), pending.get("token_hash") or ""))


@router.post("/setup/confirm")
@public
def setup_confirm(request: Request, response: Response, body: dict | None = Body(None)):
    keys = _setup_keys(request)
    _rate_limit(keys)
    if not setup_open():
        raise _setup_done(keys)
    form = Form(body)
    code = form.str("code", required=True, max_len=16, label="Der Code")
    form.check()
    _check_setup_token(form, keys)
    result = None
    with store.transaction():
        user = store.get_user()
        step = setup_step(user)
        if step == "account":
            raise conflict("setup_not_started", "Bitte zuerst Benutzername und Passwort festlegen.")
        if step is None:
            raise _setup_done(keys)
        if not _setup_cookie_ok(request, store.get_meta("setup.pending")):
            _record(keys, False)
            raise conflict("setup_restart", "Die Einrichtung wurde in einem anderen Browser (neu) begonnen oder ist "
                                            "abgelaufen. Bitte hier Benutzername und Passwort noch einmal eingeben – "
                                            "es gibt dann einen neuen QR-Code.")
        digits = "".join(code.split())
        counter = security.verify_totp(user["totp_secret"], digits, None) if CODE_RE.fullmatch(digits) else None
        if counter is not None:
            plain, hashed = security.new_recovery_codes()
            store.update_user(user["id"], totp_enabled=True, totp_last_counter=counter, recovery_codes=hashed)
            store.set_meta("setup.pending", None)
            clear_setup_token()
            user = store.get_user()
            _start_session(request, response, user)
            result = {"recovery_codes": plain}
    if result is None:
        _record(keys, False)
        raise ApiError(422, "invalid_code", "Der Code stimmt nicht. Prüfe die Uhrzeit des Handys und nimm den "
                                            "aktuellen Code.", fields={"code": "Der Code stimmt nicht."})
    _clear_cookie(request, response, SETUP_COOKIE, path="/api/setup")
    _import_after_setup()
    return result


def _import_after_setup():
    """The repo's .env keys go into the DB only now that the owner is proven (importer.run skips them before)."""
    from .. import importer
    try:
        summary = importer.run()
        if summary.get("secrets"):
            log.info("after setup, imported from .env: %s", summary["secrets"])
    except Exception:
        log.exception("import after setup failed")


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
