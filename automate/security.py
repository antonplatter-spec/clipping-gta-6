"""Login security: password hashing, TOTP 2FA, recovery codes, session tokens, secret masking. Stdlib + segno only.

    hash_password(password) -> "scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>"   (ValueError, German, when < 10 chars)
    verify_password(password, stored) -> bool                                (constant-time; False for a bad hash)
    new_totp_secret() -> base32 str (160 bits, no padding)
    totp_now(secret, t=None) -> "123456"
    verify_totp(secret, code, last_counter) -> counter | None   RFC 6238 (SHA-1, 30 s, 6 digits), window ±1 step;
        a code whose counter is <= last_counter is refused (replay); store the returned counter as totp_last_counter
    otpauth_uri(username, secret, issuer="GTA Clipper") -> "otpauth://totp/..."
    qr_svg(text) -> inline <svg> (viewBox, black on white, scales with CSS)
    new_recovery_codes(n=10) -> (plain ["abcde-fghij", ...], hashed [sha256 hex, ...])   show plain once, store hashed
    use_recovery_code(code, hashed) -> remaining hashed list | None   (None = no match; store the list on success)
    hash_recovery_code(code) -> sha256 hex of the normalized code (case, dashes and spaces ignored)
    new_session_token() -> (token, token_hash)    token goes in the cookie, sha256 hex token_hash in the DB
    hash_token(token) -> sha256 hex
    mask(secret) -> "sk-a…f3Xz" (first 4 + "…" + last 4; "…" alone for short values, "" for none)
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

MIN_PASSWORD = 10
MAX_PASSWORD = 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
TOTP_STEP, TOTP_DIGITS = 30, 6
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read off paper


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * r * n + 16 * 2 ** 20,
                          dklen=32)


def hash_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"Das Passwort muss mindestens {MIN_PASSWORD} Zeichen lang sein.")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Das Passwort darf höchstens {MAX_PASSWORD} Zeichen lang sein.")
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return (f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode('ascii')}"
            f"${base64.b64encode(digest).decode('ascii')}")


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt" or not isinstance(password, str) or len(password) > MAX_PASSWORD:
            return False
        want = base64.b64decode(digest)
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret):
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def _hotp(key, counter):
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return f"{code:0{TOTP_DIGITS}d}"


def totp_now(secret, t=None):
    return _hotp(_key(secret), int((time.time() if t is None else t) // TOTP_STEP))


def verify_totp(secret, code, last_counter=None, t=None):
    code = "".join(str(code or "").split())
    if len(code) != TOTP_DIGITS or not code.isdigit() or not secret:
        return None
    key = _key(secret)
    now = int((time.time() if t is None else t) // TOTP_STEP)
    last = -1 if last_counter is None else int(last_counter)
    for counter in (now - 1, now, now + 1):
        if counter > last and hmac.compare_digest(_hotp(key, counter), code):
            return counter
    return None


def otpauth_uri(username, secret, issuer="GTA Clipper"):
    label = quote(f"{issuer}:{username}", safe=":@")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1"
            f"&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def qr_svg(text):
    import segno
    return segno.make(text, error="m").svg_inline(scale=5, border=4, dark="#000", light="#fff", omitsize=True)


def _norm_code(code):
    return "".join(ch for ch in str(code or "").lower() if ch.isalnum())


def hash_recovery_code(code):
    return hashlib.sha256(_norm_code(code).encode("ascii", "ignore")).hexdigest()


def new_recovery_codes(n=10):
    plain = []
    for _ in range(n):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        plain.append(f"{raw[:5]}-{raw[5:]}")
    return plain, [hash_recovery_code(c) for c in plain]


def use_recovery_code(code, hashed):
    if len(_norm_code(code)) != 10:
        return None
    h = hash_recovery_code(code)
    hit = None
    for i, stored in enumerate(hashed or []):  # compare every entry: the time doesn't tell which one matched
        if hmac.compare_digest(stored, h) and hit is None:
            hit = i
    if hit is None:
        return None
    return [x for i, x in enumerate(hashed) if i != hit]


def hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def new_session_token():
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def mask(secret):
    if not secret:
        return ""
    s = str(secret)
    return f"{s[:4]}…{s[-4:]}" if len(s) >= 12 else "…"
