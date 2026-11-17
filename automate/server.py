"""The FastAPI app: the REST API under /api (automate.api), the dashboard (automate/web) at /, the security layer
around both, and the lifespan that starts and stops the background services.

    ServerConfig(host="127.0.0.1", port=8765, public_url=None)
        .host_allowed(host_header, scheme) ; .origin_allowed(origin, scheme, host_header) ; .redirect_uri()
        .base_url() ; .allowed_hosts() (for the banner)
    create_app(*, background=True, config=None) -> FastAPI      background=False: no workers/scheduler (tests)
    route_table(app) -> [(method, path)]                         every API route (the auth sweep test walks it)

Security, enforced in one pure ASGI middleware so it covers the API, the static files and streamed responses:
  - Host allow-list (DNS rebinding): localhost / 127.0.0.1 / [::1] on the server's port; with --host 0.0.0.0 also
    this machine's names and IPs; the --public-url host. Anything else -> 403.
  - Origin, when a browser sends one, must be this same origin (or the public URL) -> else 403; for /api, a
    `Sec-Fetch-Site` of cross-site/same-site is refused too (another site, or another port on localhost, can't
    embed or drive the API), except the YouTube OAuth callback, which is a top-level navigation from Google.
  - CSRF: POST/PUT/PATCH/DELETE under /api need `X-Requested-With: automate` (a cross-origin page can't set it
    without a CORS preflight, which this server never allows) -> else 403.
  - Request bodies above MAX_BODY -> 413.
  - Headers on every response: a CSP that fits the no-CDN dashboard (scripts/styles/fonts from 'self', images also
    data:/blob: for the QR code, no framing), X-Content-Type-Options, Referrer-Policy, X-Frame-Options,
    Cross-Origin-Opener/Resource-Policy, Permissions-Policy; `Cache-Control: no-store` on API responses unless the
    route set one; `no-cache` (revalidate) on the dashboard files.
Errors are always `{"error": {"code", "message"}}` with a German message; tracebacks go to the log only.

Startup order (lifespan, background=True): DB connect + migrate -> importer.run() -> runtime.materialize() ->
store.release_stale_jobs(0) -> worker.start_workers(stop) + scheduler.start_scheduler(stop) (+ a one-off system
report so the overview knows the tools). Shutdown sets `stop` and joins those threads (SHUTDOWN_JOIN_S).
"""
import ipaddress
import json
import logging
import mimetypes
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.staticfiles import StaticFiles

from . import __version__, db, settings
from .api import build_router
from .api.common import ApiError, map_exception
from .youtube import CALLBACK_PATH

log = logging.getLogger("automate.server")

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 4 * 1024 * 1024  # research.override may hold a long playbook
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOOPBACK = ("localhost", "127.0.0.1", "::1")
SHUTDOWN_JOIN_S = 20
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
STATUS_MESSAGES = {400: "Die Anfrage war ungültig.", 401: "Bitte melde dich an.",
                   403: "Diese Aktion ist nicht erlaubt.",
                   404: "Nicht gefunden.", 405: "Diese Methode ist hier nicht erlaubt.",
                   413: f"Die Daten sind zu groß (höchstens {MAX_BODY // (1024 * 1024)} MB).",
                   429: "Zu viele Anfragen. Bitte warte kurz."}

mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


# ---------------------------------------------------------------- config

def _split_host(value):
    """'Host' header -> (lower-case name without brackets/trailing dot, port | None); None when malformed."""
    h = (value or "").strip().lower()
    if not h:
        return None
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return None
        name, rest = h[1:end], h[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port = rest[1:] if rest else None
    elif h.count(":") == 1:
        name, port = h.split(":")
    elif ":" in h:
        return None
    else:
        name, port = h, None
    if port is not None and not port.isdigit():
        return None
    return name.rstrip("."), int(port) if port else None


def machine_names():
    """This machine's host names and IP addresses (for --host 0.0.0.0); no packets are sent."""
    names = set()
    try:
        hn = socket.gethostname()
        names |= {hn.lower(), f"{hn.lower()}.local", socket.getfqdn().lower()}
        names |= set(socket.gethostbyname_ex(hn)[2])
    except OSError:
        pass
    for family, target in ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect(target)  # UDP connect only picks the outgoing interface
                names.add(s.getsockname()[0].split("%")[0].lower())
        except OSError:
            pass
    return {n for n in names if n}


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    public_url: str | None = None
    names: set = field(default_factory=set)  # computed: the host names the server answers to

    def __post_init__(self):
        self.names = set(LOOPBACK)
        h = (self.host or "").strip("[]").lower()
        if h in ("0.0.0.0", "::", ""):
            self.names |= machine_names()
        else:
            self.names.add(h)
        self.public = urlsplit(self.public_url.rstrip("/")) if self.public_url else None

    @property
    def public_origin(self):
        if not self.public:
            return None
        return f"{self.public.scheme}://{self.public.netloc}".lower()

    def _public_pair(self):
        p = self.public
        return p.hostname.lower(), p.port or (443 if p.scheme == "https" else 80)

    def host_allowed(self, value, scheme="http"):
        parsed = _split_host(value)
        if parsed is None:
            return False
        name, port = parsed
        port = port or (443 if scheme == "https" else 80)
        if self.public and (name, port) == self._public_pair():
            return True
        return name in self.names and port == self.port

    def origin_allowed(self, origin, scheme, host):
        o = (origin or "").strip().rstrip("/").lower()
        return o == f"{scheme}://{(host or '').strip().lower()}" or (self.public_origin is not None and
                                                                   o == self.public_origin)

    def base_url(self):
        if self.public_origin:
            return self.public_origin
        return f"http://localhost:{self.port}"

    def redirect_uri(self):
        """What Google sends the browser back to: Google accepts http only for localhost, so a public URL counts only
        when it is https on a real host name (not an IP address)."""
        if self.public and self.public.scheme == "https" and not _is_ip(self.public.hostname or ""):
            return f"{self.public_origin}{CALLBACK_PATH}"
        return f"http://localhost:{self.port}{CALLBACK_PATH}"

    def allowed_hosts(self):
        out = sorted(f"[{n}]:{self.port}" if ":" in n else f"{n}:{self.port}" for n in self.names)
        if self.public:
            out.append(self.public.netloc)
        return out


def _is_ip(name):
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- security middleware

def _error_body(status, code, message, **extra):
    return json.dumps({"error": {"code": code, "message": message, **extra}}, ensure_ascii=False).encode("utf-8")


class SecurityMiddleware:
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def _reject(self, send, status, code, message):
        body = _error_body(status, code, message)
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"),
                                (b"x-content-type-options", b"nosniff"),
                                (b"content-security-policy", CSP.encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        scheme, path, method = scope.get("scheme", "http"), scope.get("path", ""), scope.get("method", "GET")
        host = headers.get("host", "")
        if not self.config.host_allowed(host, scheme):
            log.warning("refused request for host %r", host[:100])
            return await self._reject(send, 403, "forbidden_host",
                                      f"Unbekannte Adresse. Öffne das Dashboard über {self.config.base_url()} (oder "
                                      f"starte den Server mit --public-url für eine eigene Adresse).")
        origin = headers.get("origin")
        if origin is not None and not self.config.origin_allowed(origin, scheme, host):
            return await self._reject(send, 403, "forbidden_origin",
                                      "Diese Anfrage kommt von einer fremden Webseite und wurde blockiert.")
        api = path == "/api" or path.startswith("/api/")
        if api:
            site = headers.get("sec-fetch-site")
            if site in ("cross-site", "same-site") and not (path == CALLBACK_PATH and method == "GET"):
                return await self._reject(send, 403, "cross_site",
                                          "Diese Anfrage kommt von einer fremden Webseite und wurde blockiert.")
            if method in MUTATING and headers.get("x-requested-with") != "automate":
                return await self._reject(send, 403, "csrf", "Anfrage ohne Dashboard-Kennung (X-Requested-With) "
                                                             "abgelehnt.")
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY:  # chunked bodies: RequestBodyLimitMiddleware -> http_error
            return await self._reject(send, 413, "too_large", STATUS_MESSAGES[413])

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "same-origin")
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
                if api:
                    h.setdefault("Cache-Control", "no-store")
                else:
                    h["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------- errors

def _error(status, code, message, headers=None, **extra):
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status, headers=headers)


def _validation_error(request, exc):
    fields, message = {}, "Bitte prüfe die Eingaben."
    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        kind = err.get("type", "")
        if loc[:1] == ("path",):
            return _error(404, "not_found", "Nicht gefunden.")
        if kind == "json_invalid":
            return _error(422, "invalid_json", "Die Anfrage enthält kein gültiges JSON.")
        if loc[:1] == ("body",):
            return _error(422, "invalid_body", "Erwartet ein JSON-Objekt.")
        fields[str(loc[-1]) if loc else "_"] = "Ungültiger Wert."
    return _error(422, "invalid", message, fields=fields)


def _install_handlers(app):
    @app.exception_handler(ApiError)
    def api_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    app.add_exception_handler(RequestValidationError, _validation_error)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request, exc):  # incl. RequestBodyLimitMiddleware's 413 for a chunked body over MAX_BODY
        msg = STATUS_MESSAGES.get(exc.status_code, f"Fehler {exc.status_code}.")
        code = {404: "not_found", 413: "too_large"}.get(exc.status_code, f"http_{exc.status_code}")
        return _error(exc.status_code, code, msg, headers=getattr(exc, "headers", None))

    @app.exception_handler(settings.SettingsError)
    def settings_error(request, exc):
        return _error(422, "invalid", "Bitte prüfe die Eingaben.", fields=exc.errors)

    @app.exception_handler(ValueError)
    def value_error(request, exc):  # the store's ValueErrors are German and meant for the user
        err = map_exception(exc)
        if err is not None:
            return JSONResponse(err.body(), status_code=err.status)
        return _error(422, "invalid", str(exc)[:300] or "Ungültige Eingabe.")

    @app.exception_handler(Exception)
    def internal_error(request, exc):
        err = map_exception(exc)
        if err is not None:
            return JSONResponse(err.body(), status_code=err.status)
        log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
        return _error(500, "internal_error", "Interner Serverfehler. Details stehen im Server-Log (logs/automate/).")


# ---------------------------------------------------------------- lifespan

def _startup():
    from . import importer, runtime, store
    db.conn()
    summary = importer.run()
    if any(summary.get(k) for k in ("profiles", "creators", "streams", "settings", "secrets")):
        log.info("imported from the repo files: %s", {k: v for k, v in summary.items() if v})
    runtime.materialize()
    released = store.release_stale_jobs(0)
    if released:
        log.info("jobs interrupted by the last shutdown are queued again: %s", released)


def _start_services(stop):
    threads = []
    for name, starter in (("worker", "start_workers"), ("scheduler", "start_scheduler")):
        try:
            module = __import__(f"automate.{name}", fromlist=[starter])
            threads += list(getattr(module, starter)(stop) or [])
        except Exception:
            log.exception("automate.%s could not start: the dashboard works, but jobs won't run until it does", name)

    def warm():
        from .api.dashboard import refresh_system_report
        try:
            refresh_system_report()
        finally:
            db.close()
    threading.Thread(target=warm, name="system-report", daemon=True).start()
    return threads


def _join(threads, timeout):
    for t in threads:
        t.join(timeout)
        if t.is_alive():
            log.warning("thread %s did not stop in time", t.name)


def _lifespan(background):
    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        app.state.stop = stop
        threads = []
        if background:
            await anyio.to_thread.run_sync(_startup)
            threads = await anyio.to_thread.run_sync(_start_services, stop)
        try:
            yield
        finally:
            stop.set()
            if threads:
                await anyio.to_thread.run_sync(_join, threads, SHUTDOWN_JOIN_S)
    return lifespan


# ---------------------------------------------------------------- app

def create_app(*, background=True, config=None):
    config = config or ServerConfig()
    app = FastAPI(title="GTA Clipper – Automatik", version=__version__, lifespan=_lifespan(background),
                  openapi_url=None, docs_url=None, redoc_url=None)
    app.state.config = config
    _install_handlers(app)
    app.include_router(build_router())
    # the dashboard: only regular files inside automate/web (Starlette resolves symlinks and refuses escapes)
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY)
    app.add_middleware(SecurityMiddleware, config=config)
    return app


def route_table(app):
    """[(method, path)] of every API route (HEAD left out)."""
    try:
        from fastapi.routing import iter_route_contexts
        contexts = [(c.path, c.methods) for c in iter_route_contexts(app.routes)]
    except ImportError:  # older FastAPI: routes are flat
        contexts = [(getattr(r, "path", ""), getattr(r, "methods", None)) for r in app.routes]
    return sorted({(m, p) for p, methods in contexts if p and p.startswith("/api") for m in methods or ()
                   if m != "HEAD"})
