"""The REST API under /api (spec §7; automate/web/js/api.js is the consumer of record).

Every route lives on the one router `build_router()` returns, and that router carries the auth guard as a
dependency: a route is protected unless its handler is marked `@public` (common.py), so a new route can't be
forgotten. PUBLIC_ROUTES lists the ones that are (spec §7); a test checks the two agree and that every other route
answers 401 without a session. Handlers are plain `def`s (FastAPI runs them in its threadpool): store calls, bridge
subprocesses and HTTP to Google never block the event loop.

Modules: auth (state, setup, login, account), dashboard (overview, automation, alerts, jobs, scan, system), picks,
videos, sources (profiles, creators, streams), config (settings, watchlist, research, assets), keys (costs, API
keys), social (accounts, YouTube, Instagram); common (errors, validation), files (safe file serving with Range).
"""
from fastapi import APIRouter, Depends, Request, Response

from . import auth, config, dashboard, keys, picks, social, sources, videos
from .common import ApiError, not_found

PUBLIC_ROUTES = frozenset({("GET", "/api/state"), ("POST", "/api/setup"), ("POST", "/api/setup/confirm"),
                           ("POST", "/api/login"), ("GET", "/api/social/youtube/callback"),
                           ("GET", "/api/social/instagram/callback")})


def guard(request: Request, response: Response):
    if getattr(getattr(request.scope.get("route"), "endpoint", None), "public_route", False):
        return
    found = auth.authenticate(request, response)
    if found is None:
        raise ApiError(401, "unauthorized", "Bitte melde dich an.")
    request.state.auth = found


def build_router():
    api = APIRouter(prefix="/api", dependencies=[Depends(guard)])
    for module in (auth, dashboard, picks, videos, sources, config, keys, social):
        api.include_router(module.router)

    @api.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def unknown(rest: str):
        raise not_found("Diese API-Adresse gibt es nicht.")

    return api
