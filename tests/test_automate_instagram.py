"""automate Instagram connection and Reels publishing (automate/instagram.py, the Instagram parts of automate/social.py
and the /api/social/instagram routes): token paste (professional vs. personal account, bad and malformed tokens, the
Facebook-Page path), OAuth with single-use session-bound `state` (only behind an HTTPS public URL), token refresh
(due / too young / failure keeps the token / dead token -> disconnected + alert), the publish flow with every resume
point after a simulated crash, a lost publish answer, cancel before and after the publish request, container
ERROR/EXPIRED, rate and publishing limits, the caption rules and the Reels file check. Meta is faked completely
through instagram.transport; nothing here talks to the network."""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from automate import db, instagram, security, server, social, store

CSRF = {"X-Requested-With": "automate"}
V = instagram.GRAPH_VERSION
IG_TOKEN = "IGAAgoodtoken0123456789abcdefXYZ"
FB_SHORT = "EAAshortlivedtoken0123456789abcdef"
META_ID, META_SECRET = "1234567890123456", "0123456789abcdef0123456789abcdef"
APP_ID, APP_SECRET = "9876543210987654", "fedcba9876543210fedcba9876543210"


# ---------------------------------------------------------------- an MP4 good enough for the Reels check

def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind.encode("latin-1")) + payload


def _full(kind, payload, version=0):
    return _box(kind, bytes([version, 0, 0, 0]) + payload)


def make_mp4(duration=20.0, width=1080, height=1920, fps=30, vcodec="avc1", acodec="mp4a", rate=48000, channels=2,
             faststart=True, payload=b"\0" * 4000):
    def trak(handler, entry, scale, samples, delta, wh=(0, 0)):
        tkhd = _full("tkhd", b"\0" * 72 + struct.pack(">II", wh[0] << 16, wh[1] << 16))
        mdhd = _full("mdhd", struct.pack(">IIII", 0, 0, scale, int(duration * scale)) + b"\0" * 4)
        hdlr = _full("hdlr", b"\0" * 4 + handler.encode() + b"\0" * 12 + b"x\0")
        stbl = _box("stbl", _full("stsd", struct.pack(">I", 1) + entry) +
                    _full("stts", struct.pack(">III", 1, samples, delta)))
        return _box("trak", tkhd + _box("mdia", mdhd + hdlr + _box("minf", stbl)))
    visual = _box(vcodec, b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 16 + struct.pack(">HH", width, height) +
                  b"\0" * 50)
    audio = _box(acodec, b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 8 + struct.pack(">HHHH", channels, 16, 0, 0) +
                 struct.pack(">I", rate << 16))
    mvhd = _full("mvhd", struct.pack(">IIII", 0, 0, 1000, int(duration * 1000)) + b"\0" * 80)
    moov = _box("moov", mvhd + trak("vide", visual, fps * 1000, int(duration * fps), 1000, (width, height)) +
                trak("soun", audio, rate, int(duration * rate / 1024), 1024))
    ftyp, mdat = _box("ftyp", b"isom\0\0\x02\0isomiso2avc1mp41"), _box("mdat", payload)
    return ftyp + (moov + mdat if faststart else mdat + moov)


# ---------------------------------------------------------------- Meta, faked

class Clock:
    def __init__(self):
        self.now, self.mono, self.sleeps = 1_800_000_000.0, 1000.0, []

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono

    def sleep(self, seconds, cancel=None):
        self.sleeps.append(seconds)
        self.now += seconds
        self.mono += seconds


class FakeMeta:
    """Instagram Login, Facebook Login and the Reels publishing endpoints, scripted per test."""

    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.ig_tokens = {IG_TOKEN: {"user_id": "17841400000000001", "username": "gta6clips",
                                     "account_type": "MEDIA_CREATOR"}}
        self.fb_tokens, self.page_tokens = {}, {}
        self.dead, self.refresh_plan, self.permissions = set(), [], list(instagram.FB_PERMISSIONS)
        self.pages = [{"id": "PAGE1", "name": "GTA Clips", "ig": ("17841400000000002", "fbclips")}]
        self.containers, self.media = {}, []
        self.create_plan, self.upload_plan, self.publish_plan, self.status_plan = [], [], [], []
        self.processing_polls, self.quota = 2, (3, 100)
        self.on_publish = None
        self.n = 0

    # -- helpers
    @staticmethod
    def res(status, data, headers=None):
        return instagram.HttpResponse(status, {"content-type": "application/json", **(headers or {})},
                                      json.dumps(data).encode())

    @staticmethod
    def err(code, status=400, sub=None, message="error"):
        e = {"message": message, "type": "OAuthException", "code": code}
        if sub:
            e["error_subcode"] = sub
        return FakeMeta.res(status, {"error": e})

    def bearer(self, headers):
        auth = headers.get("Authorization") or ""
        return auth.split(" ", 1)[1] if " " in auth else None

    def identity(self, token):
        if token in self.dead:
            return None
        return self.ig_tokens.get(token) or self.page_tokens.get(token)

    def __call__(self, method, url, *, headers=None, body=None, timeout=60):
        headers = dict(headers or {})
        self.calls.append((method, url, headers, body))
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        form = {k: v[0] for k, v in parse_qs((body or b"").decode("utf-8", "replace")).items()} \
            if headers.get("Content-Type") == "application/x-www-form-urlencoded" else {}
        if u.hostname == "api.instagram.com":
            if form.get("code") != "good-code" or form.get("client_secret") != APP_SECRET:
                return self.res(400, {"error_type": "OAuthException", "code": 400,
                                      "error_message": "Invalid authorization code"})
            self.ig_tokens["IGshortlived0123456789abcdefgh"] = self.ig_tokens[IG_TOKEN]
            return self.res(200, {"data": [{"access_token": "IGshortlived0123456789abcdefgh",
                                            "user_id": "17841400000000001",
                                            "permissions": ",".join(instagram.SCOPES)}]})
        if u.hostname == "graph.instagram.com" and u.path == "/access_token":
            assert (q["grant_type"], q["client_secret"]) == ("ig_exchange_token", APP_SECRET)
            who = self.ig_tokens.get(q["access_token"])
            if not who:
                return self.err(190)
            self.ig_tokens["IGAAlonglived0123456789abcdefgh"] = who
            return self.res(200, {"access_token": "IGAAlonglived0123456789abcdefgh", "token_type": "bearer",
                                  "expires_in": 5183944})
        if u.hostname == "graph.instagram.com" and u.path == "/refresh_access_token":
            plan = self.refresh_plan.pop(0) if self.refresh_plan else "ok"
            if isinstance(plan, BaseException):
                raise plan
            if plan != "ok":
                return plan
            who = self.identity(q["access_token"])
            if not who:
                return self.err(190)
            self.n += 1
            new = f"IGAArefreshed{self.n:04d}0123456789abcdef"
            self.ig_tokens[new] = who
            return self.res(200, {"access_token": new, "token_type": "bearer", "expires_in": 5184000})
        if u.hostname == "graph.facebook.com" and u.path == f"/{V}/oauth/access_token":
            assert (q["client_id"], q["client_secret"]) == (META_ID, META_SECRET)
            if q["fb_exchange_token"] != FB_SHORT:
                return self.err(190)
            self.fb_tokens["EAAlonglived0123456789abcdefgh"] = True
            return self.res(200, {"access_token": "EAAlonglived0123456789abcdefgh", "token_type": "bearer",
                                  "expires_in": 5183944})
        if u.hostname == "rupload.facebook.com":
            return self.upload(u.path, headers, body)
        assert "access_token" not in q, f"a token in the URL of {url}"
        token = self.bearer(headers)
        path = u.path[len(f"/{V}/"):]
        if u.hostname == "graph.facebook.com" and path in ("me/permissions", "me/accounts"):
            if token not in self.fb_tokens:
                return self.err(190)
            if path == "me/permissions":
                return self.res(200, {"data": [{"permission": p, "status": "granted"} for p in self.permissions]})
            data = []
            for p in self.pages:
                pt = f"EAApage{p['id']}0123456789abcdef"
                self.page_tokens[pt] = {"user_id": p["ig"][0], "username": p["ig"][1]}
                data.append({"id": p["id"], "name": p["name"], "access_token": pt,
                             "instagram_business_account": {"id": p["ig"][0], "username": p["ig"][1]}})
            return self.res(200, {"data": data})
        who = self.identity(token)
        if who is None:
            return self.err(190, message="Error validating access token")
        if path == "me":
            return self.res(200, {"id": "app-scoped-1", **who})
        return self.publishing(method, path, q, form, who)

    def publishing(self, method, path, q, form, who):
        ig = who["user_id"]
        if method == "POST" and path == f"{ig}/media":
            plan = self.create_plan.pop(0) if self.create_plan else "ok"
            if isinstance(plan, BaseException):
                raise plan
            if plan != "ok":
                return plan
            self.n += 1
            cid = f"C{self.n}"
            self.containers[cid] = {"form": form, "data": b"", "polls": 0, "published": None, "force": None}
            return self.res(200, {"id": cid, "uri": f"https://rupload.facebook.com/ig-api-upload/{V}/{cid}"})
        if method == "POST" and path == f"{ig}/media_publish":
            return self.publish(form["creation_id"])
        if path == f"{ig}/content_publishing_limit":
            return self.res(200, {"data": [{"quota_usage": self.quota[0],
                                            "config": {"quota_total": self.quota[1], "quota_duration": 86400}}]})
        if path == f"{ig}/media":
            return self.res(200, {"data": list(reversed(self.media))})
        if path == ig:
            return self.res(200, {"id": ig, "username": who["username"]})
        if path in self.containers:
            c = self.containers[path]
            if self.status_plan:
                code = self.status_plan.pop(0)
            elif c["force"]:
                code = c["force"]
            elif c["published"]:
                code = "PUBLISHED"
            elif c["data"] and c["polls"] >= self.processing_polls:
                code = "FINISHED"
            else:
                c["polls"] += 1 if c["data"] else 0
                code = "IN_PROGRESS"
            return self.res(200, {"id": path, "status_code": code, "status": code.title()})
        m = next((m for m in self.media if m["id"] == path), None)
        if m:
            return self.res(200, {"id": path, "permalink": m["permalink"]})
        if method == "GET" and "/" not in path:
            return self.err(100, sub=33, message="Unsupported get request. Object does not exist")
        raise AssertionError(f"unexpected {method} {path}")

    def upload(self, path, headers, body):
        cid = path.rsplit("/", 1)[1]
        assert path == f"/ig-api-upload/{V}/{cid}", path
        assert headers["Authorization"].startswith("OAuth ") and self.identity(headers["Authorization"][6:])
        assert (headers["offset"], headers["file_size"]) == ("0", str(len(body)))
        plan = self.upload_plan.pop(0) if self.upload_plan else "ok"
        if plan == "drop":
            raise ConnectionResetError("connection reset by peer")
        if plan != "ok":
            return plan
        self.containers[cid]["data"] = body
        return self.res(200, {"success": True})

    def publish(self, cid):
        c = self.containers.get(cid)
        plan = self.publish_plan.pop(0) if self.publish_plan else "ok"
        if self.on_publish:
            self.on_publish(cid)
        if plan == "500":
            return self.err(1, status=500)
        if plan == "lost-before":  # the request never reached Meta
            raise ConnectionResetError("connection reset by peer")
        if c is None or c["published"]:
            return self.err(100, message="The media has already been published")
        self.n += 1
        mid = f"M{self.n}"
        c["published"] = mid
        ts = datetime.fromtimestamp(self.clock.now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.media.append({"id": mid, "caption": c["form"]["caption"], "timestamp": ts,
                           "permalink": f"https://www.instagram.com/reel/{mid}/"})
        if plan == "lost":  # Meta published it, the answer never arrived
            raise TimeoutError("timed out")
        return self.res(200, {"id": mid})

    # -- what tests look at
    def count(self, method, needle):
        return sum(1 for c in self.calls if c[0] == method and needle in c[1])

    def containers_created(self):
        return sum(1 for c in self.calls if c[0] == "POST" and urlsplit(c[1]).path.endswith("/media"))


class InstagramCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"AUTOMATE_DATA_DIR": str(self.root / "data"),
                                           "AUTOMATE_JOBS_DIR": str(self.root / "jobs"),
                                           "AUTOMATE_EXPORTS_DIR": str(self.root / "exports"),
                                           "AUTOMATE_LOGS_DIR": str(self.root / "logs")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(db.close)
        mods = mock.patch.dict(sys.modules, {"automate.pipeline": types.ModuleType("automate.pipeline"),
                                             "automate.health": None, "automate.scheduler": None,
                                             "automate.agent": None, "automate.worker": None})
        mods.start()
        self.addCleanup(mods.stop)
        store.ensure_profile("gta6", True)
        self.clock = Clock()
        self.meta = FakeMeta(self.clock)
        for p in (mock.patch.object(instagram, "transport", self.meta),
                  mock.patch.object(instagram, "_sleep", self.clock.sleep),
                  mock.patch.object(instagram, "time", self.clock)):
            p.start()
            self.addCleanup(p.stop)

    def app(self, public_url=None):
        return server.create_app(background=False, config=server.ServerConfig(public_url=public_url))

    def login(self, app, base="http://127.0.0.1:8765"):
        if store.get_user() is None:
            store.create_user("owner", "scrypt$x", security.new_totp_secret(), totp_enabled=True)
        token, h = security.new_session_token()
        store.create_session(h, store.get_user()["id"], time.time() + 3600)
        c = TestClient(app, base_url=base, headers=CSRF)
        c.cookies.set("automate_session", token)
        return c

    def connected(self, token=IG_TOKEN, **extra):
        rows = instagram.connect_token(token)
        aid = rows[0]["id"]
        if extra:
            creds = store.get_account(aid, with_credentials=True)["credentials"]
            store.update_account(aid, credentials={**creds, **extra})
        return aid

    def creds(self, aid):
        return store.get_account(aid, with_credentials=True)["credentials"]

    def variant(self, data=None, **fields):
        folder = self.root / "exports" / "gta6" / "2026-11-17" / "video 1"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"hook{len(list(folder.iterdir()))}.mp4"
        f.write_bytes(make_mp4() if data is None else data)
        vid = store.add_video("gta6", folder, title="Video")
        fields = {"hook": "Hook", "title": "Kai Did WHAT In GTA 6?",
                  "description": "Kai found a secret in GTA 6\n\nClip from Kai Cenat: https://youtu.be/x\n\n"
                                 "#gta6 #gtavi #shorts", "hashtags": ["#gta6", "#kaicenat", "#shorts"], **fields}
        return store.add_variant(vid, f, **fields)


# ---------------------------------------------------------------- connecting

class TokenTests(InstagramCase):
    def test_pasting_a_professional_accounts_token_connects_it(self):
        rows = instagram.connect_token(f"  {IG_TOKEN}\n")
        a = rows[0]
        self.assertEqual((a["platform"], a["label"], a["handle"], a["external_id"], a["connected"]),
                         ("instagram", "@gta6clips", "@gta6clips", "17841400000000001", True))
        c = self.creds(a["id"])
        self.assertEqual((c["api"], c["access_token"], c["expiry_known"]), ("instagram", IG_TOKEN, False))
        self.assertEqual(c["refresh_after"], self.clock.now + instagram.REFRESH_MIN_AGE_S)  # as soon as Meta allows
        self.assertEqual(c["expires_at"], self.clock.now + instagram.TOKEN_LIFETIME_S)
        me = self.meta.calls[-1]
        self.assertEqual(me[2]["Authorization"], f"Bearer {IG_TOKEN}")  # the header, not the URL
        self.assertNotIn(IG_TOKEN, me[1])
        pub = social.public(store.get_account(a["id"]))
        self.assertNotIn(IG_TOKEN, json.dumps(pub))
        self.assertEqual((pub["connected"], pub["can_upload"], pub["connection"]["username"],
                          pub["connection"]["expires_estimated"]), (True, True, "gta6clips", True))
        again = instagram.connect_token(IG_TOKEN)  # the same account again: updated, not duplicated
        self.assertEqual(again[0]["id"], a["id"])
        self.assertEqual(len(store.list_accounts("instagram")), 1)

    def test_a_personal_account_a_dead_and_a_malformed_token_are_refused(self):
        self.meta.ig_tokens["IGAApersonal0123456789abcdefgh"] = {"user_id": "5", "username": "me",
                                                                 "account_type": "PERSONAL"}
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token("IGAApersonal0123456789abcdefgh")
        self.assertEqual(e.exception.code, "instagram_not_professional")
        self.assertIn("professionelle Konten", str(e.exception))
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token("IGAAunknowntoken0123456789abcdef")
        self.assertEqual(e.exception.code, "instagram_token_invalid")
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token("zzzunknowntoken0123456789abcdef")  # neither shape: said plainly
        self.assertIn("beginnt meist mit „IG“", str(e.exception))
        for bad in ("IGAA with space 0123456789", "short", ""):
            with self.assertRaises(instagram.InstagramError) as e:
                instagram.connect_token(bad)
            self.assertEqual(e.exception.code, "instagram_token_format")
        self.assertEqual(store.list_accounts(), [])

    def test_the_facebook_page_path_needs_the_meta_app_and_stores_page_tokens(self):
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token(FB_SHORT)
        self.assertEqual(e.exception.code, "instagram_app_missing")
        store.set_secret("meta_app_id", META_ID)
        store.set_secret("meta_app_secret", META_SECRET)
        self.meta.permissions = ["instagram_basic", "pages_show_list"]
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token(FB_SHORT)
        self.assertIn("instagram_content_publish", str(e.exception))
        self.meta.permissions = list(instagram.FB_PERMISSIONS)
        self.meta.pages = [{"id": "P0", "name": "No IG", "ig": ("", "")}]
        self.meta.pages[0]["ig"] = (None, None)
        self.meta.pages = []
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token(FB_SHORT)
        self.assertEqual(e.exception.code, "instagram_no_account")
        self.meta.pages = [{"id": "PAGE1", "name": "GTA Clips", "ig": ("17841400000000002", "fbclips")}]
        rows = instagram.connect_token(FB_SHORT)
        self.assertEqual([(r["label"], r["external_id"]) for r in rows], [("@fbclips", "17841400000000002")])
        c = self.creds(rows[0]["id"])
        self.assertEqual((c["api"], c["access_token"], c["page_name"], c["expires_at"]),
                         ("facebook", "EAApagePAGE10123456789abcdef", "GTA Clips", None))
        exchange = next(x for x in self.meta.calls if "oauth/access_token" in x[1])
        self.assertIn("fb_exchange_token", exchange[1])  # Meta documents the exchange with query parameters

    def test_a_token_of_another_account_is_not_stored_on_this_one(self):
        aid = store.add_account("instagram", "Zweitkonto", external_id="999")
        with self.assertRaises(instagram.InstagramError) as e:
            instagram.connect_token(IG_TOKEN, account_id=aid)
        self.assertEqual(e.exception.code, "instagram_wrong_account")
        manual = store.add_account("instagram", "Mein IG", handle="@GTA6Clips")  # marked by hand before: adopted
        self.assertEqual(instagram.connect_token(IG_TOKEN)[0]["id"], manual)
        self.assertFalse(store.get_account(aid)["connected"])


class OAuthTests(InstagramCase):
    def setUp(self):
        super().setUp()
        store.set_secret("instagram_app_id", APP_ID)
        store.set_secret("instagram_app_secret", APP_SECRET)

    def callback(self, app, query, host="clips.example.de"):
        c = TestClient(app, base_url=f"https://{host}")  # Meta's redirect: no cookie, cross-site
        return c.get(f"/api/social/instagram/callback?{query}", headers={"Sec-Fetch-Site": "cross-site"})

    def test_oauth_is_offered_only_behind_an_https_public_url(self):
        local = self.login(self.app())
        d = local.get("/api/social/instagram/app").json()
        self.assertEqual((d["oauth_available"], d["redirect_uri"]), (False, None))
        self.assertIn("HTTPS", d["oauth_note"])
        r = local.post("/api/social/instagram/connect", json={})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "instagram_needs_https"))
        for url in ("http://clips.example.de", "https://192.168.1.20"):
            d = self.login(self.app(url), base=url).get("/api/social/instagram/app").json()
            self.assertFalse(d["oauth_available"], url)

    def test_consent_url_callback_long_lived_token_and_single_use_state(self):
        app = self.app("https://clips.example.de")
        client = self.login(app, base="https://clips.example.de")
        d = client.get("/api/social/instagram/app").json()
        self.assertEqual(d["redirect_uri"], "https://clips.example.de/api/social/instagram/callback")
        self.assertTrue(d["oauth_available"] and d["app_id_set"] and d["app_secret_set"])
        self.assertNotIn(APP_SECRET, json.dumps(d))
        url = urlsplit(client.post("/api/social/instagram/connect", json={}).json()["auth_url"])
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", instagram.AUTH_URL)
        self.assertEqual((q["client_id"], q["redirect_uri"], q["response_type"], q["scope"]),
                         (APP_ID, d["redirect_uri"], "code", ",".join(instagram.SCOPES)))
        self.assertGreaterEqual(len(q["state"]), 40)
        r = self.callback(app, f"code=good-code&state={q['state']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("https://clips.example.de/#/konten?instagram=connected&amp;account=", r.text)
        a = store.list_accounts("instagram")[0]
        c = self.creds(a["id"])
        self.assertEqual((c["access_token"], c["expiry_known"]), ("IGAAlonglived0123456789abcdefgh", True))
        self.assertAlmostEqual(c["expires_at"], self.clock.now + 5183944)
        again = self.callback(app, f"code=good-code&state={q['state']}")  # replayed
        self.assertEqual(again.status_code, 400)
        self.assertIn("abgelaufen oder wurde schon benutzt", again.text)

    def test_bad_expired_foreign_and_orphaned_states_and_a_bad_code(self):
        app = self.app("https://clips.example.de")
        client = self.login(app, base="https://clips.example.de")

        def state():
            url = urlsplit(client.post("/api/social/instagram/connect", json={}).json()["auth_url"])
            return parse_qs(url.query)["state"][0]
        for query in ("code=good-code", "code=good-code&state=" + "x" * 43, "code=good-code&state=short"):
            self.assertIn("instagram=error&amp;message=", self.callback(app, query).text)
        s = state()
        with mock.patch.object(social.time, "time", return_value=time.time() + social.STATE_TTL_S + 1):
            self.assertIn("abgelaufen", self.callback(app, f"code=good-code&state={s}").text)
        yt_state = social.new_state(client.cookies.get("automate_session"), account_id=None, return_origin="x",
                                    redirect_uri="y")  # a YouTube state is no Instagram state
        self.assertEqual(self.callback(app, f"code=good-code&state={yt_state}").status_code, 400)
        self.assertIn("nicht erlaubt", self.callback(app, f"error=access_denied&state={state()}").text)
        self.assertIn("abgelaufen oder wurde schon benutzt", self.callback(app, f"code=bad&state={state()}").text)
        s = state()
        client.post("/api/logout")
        self.assertIn("Sitzung", self.callback(app, f"code=good-code&state={s}").text)
        self.assertEqual(store.list_accounts(), [])

    def test_app_ids_are_validated_and_write_only(self):
        client = self.login(self.app())
        r = client.put("/api/social/instagram/app", json={"meta_app_id": "my app", "meta_app_secret": "has space"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"meta_app_id", "meta_app_secret"})
        self.assertEqual(client.put("/api/social/instagram/app", json={}).status_code, 422)
        r = client.put("/api/social/instagram/app", json={"meta_app_id": META_ID, "meta_app_secret": META_SECRET})
        d = r.json()
        self.assertTrue(d["meta_app_id_set"] and d["meta_app_secret_set"])
        self.assertNotIn(META_SECRET, r.text)
        self.assertNotIn(META_ID, r.text)
        self.assertEqual(client.put("/api/social/instagram/app", json={"meta_app_secret": None}).json()
                         ["meta_app_secret_set"], False)


class TokenApiTests(InstagramCase):
    def test_paste_check_and_disconnect_routes(self):
        client = self.login(self.app())
        r = client.post("/api/social/instagram/token", json={"access_token": "IGAAunknowntoken0123456789abcdef"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "instagram_token_invalid"))
        self.assertIn("access_token", r.json()["error"]["fields"])
        self.assertNotIn("IGAAunknowntoken", r.text)
        r = client.post("/api/social/instagram/token", json={"access_token": IG_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)
        acc = r.json()["items"][0]
        self.assertEqual((acc["label"], acc["connected"], acc["connection"]["kind"]), ("@gta6clips", True,
                                                                                        "instagram"))
        self.assertNotIn(IG_TOKEN, r.text)
        listed = client.get("/api/social/accounts").text
        self.assertNotIn(IG_TOKEN, listed)
        r = client.post(f"/api/social/instagram/accounts/{acc['id']}/check")
        self.assertEqual((r.status_code, r.json()["connected"]), (200, True))
        self.meta.dead.add(IG_TOKEN)
        r = client.post(f"/api/social/instagram/accounts/{acc['id']}/check")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "instagram_disconnected"))
        self.assertTrue(store.get_alert(f"instagram.disconnected.{acc['id']}"))
        r = client.post("/api/social/instagram/token", json={"access_token": "IGAAother0123456789abcdefgh",
                                                             "account_id": store.add_account("tiktok", "TT")})
        self.assertEqual(r.status_code, 422)
        self.meta.ig_tokens["IGAAnew0123456789abcdefghijk"] = self.meta.ig_tokens[IG_TOKEN]
        r = client.post("/api/social/instagram/token", json={"access_token": "IGAAnew0123456789abcdefghijk",
                                                             "account_id": acc["id"]})
        self.assertEqual(r.json()["items"][0]["id"], acc["id"])
        self.assertIsNotNone(store.get_alert(f"instagram.disconnected.{acc['id']}")["resolved_at"])
        r = client.post(f"/api/social/accounts/{acc['id']}/disconnect")
        self.assertEqual((r.json()["connected"], self.creds(acc["id"])), (False, None))

    def test_meta_unreachable_is_said_in_german(self):
        client = self.login(self.app())
        self.meta.ig_tokens = {}

        def offline(method, url, **kw):
            raise OSError("getaddrinfo failed")
        with mock.patch.object(instagram, "transport", offline):
            r = client.post("/api/social/instagram/token", json={"access_token": IG_TOKEN})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "instagram_unreachable"))
        self.assertIn("nicht erreichbar", r.json()["error"]["message"])


# ---------------------------------------------------------------- keeping the token alive

class RefreshTests(InstagramCase):
    def test_a_young_token_is_left_alone_and_a_due_one_is_refreshed(self):
        aid = self.connected()
        self.assertEqual(instagram.maintain(self.clock.now + 3600), [{"account_id": aid, "action": "fresh"}])
        self.assertEqual(self.meta.count("GET", "refresh_access_token"), 0)  # too young: no request at all
        later = self.clock.now + instagram.REFRESH_MIN_AGE_S + 1
        self.assertEqual(instagram.maintain(later)[0]["action"], "refreshed")
        c = self.creds(aid)
        self.assertTrue(c["access_token"].startswith("IGAArefreshed"))
        self.assertEqual((c["expiry_known"], c["expires_at"], c["refresh_error"]), (True, later + 5184000, None))
        self.assertEqual(c["refresh_after"], later + instagram.REFRESH_EVERY_S)
        self.assertEqual(instagram.maintain(later + 3600)[0]["action"], "fresh")
        self.assertEqual(self.meta.count("GET", "refresh_access_token"), 1)

    def test_a_failed_refresh_keeps_the_token_and_waits_before_the_next_try(self):
        aid = self.connected()
        t = self.clock.now + instagram.REFRESH_MIN_AGE_S + 1
        self.meta.refresh_plan = [FakeMeta.res(503, {"error": {"code": 2, "message": "busy"}})] * 3
        self.assertEqual(instagram.maintain(t)[0]["action"], "failed")
        c = self.creds(aid)
        self.assertEqual((c["access_token"], c["refresh_tried_at"]), (IG_TOKEN, t))
        self.assertIn("Fehler 503", c["refresh_error"])
        self.assertTrue(store.get_account(aid)["connected"])
        for dt in (60, 600, instagram.REFRESH_RETRY_S - 1):  # the health tick every few minutes: no new request
            self.assertEqual(instagram.maintain(t + dt)[0]["action"], "fresh")
        self.assertEqual(self.meta.count("GET", "refresh_access_token"), 3)  # the 3 tries of that one attempt
        self.meta.refresh_plan = [OSError("no network")] * 3  # offline: still kept

        self.assertEqual(instagram.maintain(t + instagram.REFRESH_RETRY_S)[0]["action"], "failed")
        self.assertEqual(self.creds(aid)["access_token"], IG_TOKEN)
        self.assertIsNone(store.get_alert(f"instagram.expiring.{aid}"))  # far from expiry: no alarm yet
        near = self.creds(aid)["expires_at"] - 2 * 86400
        self.meta.refresh_plan = [FakeMeta.res(400, {"error": {"code": 100, "message": "token too new"}})]
        self.assertEqual(instagram.maintain(near)[0]["action"], "failed")
        alert = store.get_alert(f"instagram.expiring.{aid}")
        self.assertIn("läuft spätestens am", alert["message"])
        self.assertEqual(self.creds(aid)["access_token"], IG_TOKEN)
        # Meta said no (not a network problem): the next try waits a day, not just REFRESH_RETRY_S
        self.assertEqual(instagram.maintain(near + instagram.REFRESH_RETRY_S + 1)[0]["action"], "fresh")
        self.assertEqual(instagram.maintain(near + instagram.REFRESH_MIN_AGE_S + 1)[0]["action"], "refreshed")
        self.assertIsNotNone(store.get_alert(f"instagram.expiring.{aid}")["resolved_at"])

    def test_a_dead_token_disconnects_the_account_with_an_alert(self):
        aid = self.connected()
        self.meta.dead.add(IG_TOKEN)
        self.assertEqual(instagram.maintain(self.clock.now + 2 * 86400)[0]["action"], "disconnected")
        a = store.get_account(aid, with_credentials=True)
        self.assertEqual((a["connected"], a["credentials"]), (False, None))
        alert = store.get_alert(f"instagram.disconnected.{aid}")
        self.assertIn("Konten", alert["message"])
        self.assertEqual(alert["details"], {"account_id": aid})
        self.assertEqual(instagram.maintain(self.clock.now + 3 * 86400), [])  # nothing left to do
        instagram.connect_token(self.meta_new_token())
        self.assertIsNotNone(store.get_alert(f"instagram.disconnected.{aid}")["resolved_at"])

    def meta_new_token(self):
        self.meta.ig_tokens["IGAAfresh0123456789abcdefghijk"] = self.meta.ig_tokens[IG_TOKEN]
        return "IGAAfresh0123456789abcdefghijk"

    def test_refused_refresh_of_a_working_token_and_a_known_expiry(self):
        aid = self.connected()
        self.meta.refresh_plan = [FakeMeta.err(190)]  # refresh says 190, but /me still works: keep it
        self.assertEqual(instagram.maintain(self.clock.now + 2 * 86400)[0]["action"], "failed")
        self.assertTrue(store.get_account(aid)["connected"])
        creds = self.creds(aid)
        store.update_account(aid, credentials={**creds, "expiry_known": True, "expires_at": self.clock.now + 10})
        self.meta.dead.add(IG_TOKEN)
        self.assertEqual(instagram.maintain(self.clock.now + 20)[0]["action"], "disconnected")

    def test_facebook_tokens_are_checked_daily(self):
        store.set_secret("meta_app_id", META_ID)
        store.set_secret("meta_app_secret", META_SECRET)
        aid = instagram.connect_token(FB_SHORT)[0]["id"]
        n = len(self.meta.calls)
        self.assertEqual(instagram.maintain(self.clock.now + 3600)[0]["action"], "fresh")
        self.assertEqual(len(self.meta.calls), n)
        self.assertEqual(instagram.maintain(self.clock.now + 86401)[0]["action"], "checked")
        self.meta.dead.add(self.creds(aid)["access_token"])
        self.assertEqual(instagram.maintain(self.clock.now + 2 * 86402)[0]["action"], "disconnected")
        self.assertIn("nicht mehr verbunden", store.get_alert(f"instagram.disconnected.{aid}")["message"])


# ---------------------------------------------------------------- publishing

class PublishTests(InstagramCase):
    def setUp(self):
        super().setUp()
        self.aid = self.connected()
        self.var = self.variant()
        self.state, self.lines, self.progress = {}, [], []

    def on_state(self, changes):
        self.state.update(changes)
        self.saved.append(dict(changes))

    def publish(self, **kw):
        self.saved = []
        kw.setdefault("state", dict(self.state))
        return instagram.publish(self.var, self.aid, {"share_to_feed": True}, on_state=self.on_state,
                                 on_progress=lambda d, t: self.progress.append((d, t)), log=self.lines.append, **kw)

    def test_the_full_flow(self):
        res = self.publish()
        self.assertEqual(res["media_id"], "M2")
        self.assertEqual(res["url"], "https://www.instagram.com/reel/M2/")
        create = next(c for c in self.meta.calls if c[0] == "POST" and c[1].endswith("/media"))
        form = parse_qs(create[3].decode())
        self.assertEqual((form["media_type"], form["upload_type"], form["share_to_feed"]),
                         (["REELS"], ["resumable"], ["true"]))
        self.assertTrue(form["caption"][0].startswith("Kai Did WHAT In GTA 6?\n\nKai found a secret"))
        self.assertNotIn("#shorts", form["caption"][0].lower())
        up = next(c for c in self.meta.calls if "rupload" in c[1])
        self.assertEqual(up[1], f"https://rupload.facebook.com/ig-api-upload/{V}/C1")
        self.assertEqual(up[3], make_mp4())  # the whole file, as it is on disk
        self.assertEqual(self.meta.containers["C1"]["data"], up[3])
        self.assertEqual(self.meta.count("POST", "media_publish"), 1)
        self.assertEqual([list(s) for s in self.saved],
                         [["ig_container", "ig_container_at", "ig_uploaded", "ig_publish_at"], ["ig_uploaded"],
                          ["ig_publish_at"], ["ig_media"]])
        self.assertIn("Instagram verarbeitet das Video …", self.lines)
        self.assertEqual(self.progress[-1][0], self.progress[-1][1])
        self.assertTrue(all("access_token" not in c[1] for c in self.meta.calls))
        self.assertTrue(any(s >= 5 for s in self.clock.sleeps))  # polled with pauses, not in a loop

    def test_a_crash_after_creating_the_container_reuses_it(self):
        self.meta.upload_plan = ["drop"] * instagram.UPLOAD_TRIES
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertTrue(e.exception.retryable)
        self.assertEqual((self.state["ig_container"], self.state["ig_uploaded"]), ("C1", False))
        res = self.publish()
        self.assertEqual(res["media_id"], "M2")
        self.assertEqual(self.meta.containers_created(), 1)  # no second container

    def test_a_crash_after_the_upload_does_not_send_the_bytes_again(self):
        self.meta.status_plan = ["IN_PROGRESS"] * 200  # processing outlasts the attempt
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual(e.exception.code, "instagram_processing_timeout")
        self.assertTrue(e.exception.retryable)
        self.assertTrue(self.state["ig_uploaded"])
        self.meta.status_plan = []
        uploads = self.meta.count("POST", "rupload")
        self.assertEqual(self.publish()["media_id"], "M2")
        self.assertEqual(self.meta.count("POST", "rupload"), uploads)

    def test_a_crash_after_the_publish_request_finds_the_reel_instead_of_posting_again(self):
        orig = self.meta.publish

        def publish_then_die(cid):
            orig(cid)
            raise SystemExit("power cut")
        with mock.patch.object(self.meta, "publish", publish_then_die), self.assertRaises(SystemExit):
            self.publish()
        self.assertTrue(self.state["ig_publish_at"])
        n = self.meta.count("POST", "media_publish")
        res = self.publish()
        self.assertEqual((res["media_id"], res["url"]), ("M2", "https://www.instagram.com/reel/M2/"))
        self.assertEqual(self.meta.count("POST", "media_publish"), n)  # looked it up: no second publish
        self.assertEqual(len(self.meta.media), 1)
        self.assertIn("Der Reel war schon veröffentlicht – kein zweiter Beitrag.", self.lines)

    def test_a_publish_request_that_never_arrived_is_sent_again_once(self):
        def before(cid):
            raise SystemExit("power cut")
        with mock.patch.object(self.meta, "publish", before), self.assertRaises(SystemExit):
            self.publish()
        res = self.publish()  # the container is FINISHED, not PUBLISHED: publish it now
        self.assertEqual(res["media_id"], "M2")
        self.assertEqual(len(self.meta.media), 1)

    def test_a_lost_publish_answer_is_looked_up_in_the_same_call(self):
        self.meta.publish_plan = ["lost"]
        res = self.publish()
        self.assertEqual(res["media_id"], "M2")
        self.assertEqual(self.meta.count("POST", "media_publish"), 1)
        self.assertTrue(any("trotzdem online" in line for line in self.lines))
        self.meta.publish_plan = ["lost-before", "ok"]  # lost before Meta got it: the container stays FINISHED
        self.state = {}
        self.var = self.variant(title="Second one")
        res = self.publish()
        self.assertEqual(len(self.meta.media), 2)
        self.assertEqual(self.meta.count("POST", "media_publish"), 3)
        self.assertGreaterEqual(self.clock.mono - 1000, instagram.LOST_GRACE_S)  # waited before sending again
        self.meta.publish_plan = ["500", "500"]
        self.state, self.var = {}, self.variant(title="Third one")
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable), ("instagram_publish_unknown", True))

    def test_cancel_counts_until_the_publish_request_goes_out(self):
        cancel = threading.Event()
        self.meta.status_plan = ["IN_PROGRESS", "IN_PROGRESS"]
        orig = self.meta.publishing

        def cancel_while_processing(method, path, q, form, who):
            if path.startswith("C") and method == "GET":
                cancel.set()
            return orig(method, path, q, form, who)
        with mock.patch.object(self.meta, "publishing", cancel_while_processing), \
                self.assertRaises(instagram.InstagramError) as e:
            self.publish(cancel=cancel)
        self.assertEqual(e.exception.code, "canceled")
        self.assertEqual(self.meta.count("POST", "media_publish"), 0)
        cancel = threading.Event()
        self.meta.on_publish = lambda cid: cancel.set()  # the owner clicks "Abbrechen" while the publish is out
        res = self.publish(cancel=cancel)
        self.assertEqual(res["media_id"], "M2")  # too late: the Reel exists, it is returned, not forgotten

    def test_container_error_and_expiry_start_over_with_a_new_container(self):
        self.meta.status_plan = ["ERROR"]
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable), ("instagram_processing_error", True))
        self.assertIsNone(self.state["ig_container"])
        self.meta.status_plan = ["EXPIRED"]
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual(e.exception.code, "instagram_container_expired")
        res = self.publish()
        self.assertEqual((res["container_id"], self.meta.containers_created()), ("C3", 3))
        self.meta.status_plan = []
        self.state, self.var = {}, self.variant(title="Other")
        orig = self.meta.publishing

        def broken_format(method, path, q, form, who):
            if method == "GET" and path.startswith("C"):
                return FakeMeta.res(200, {"status_code": "ERROR", "status": "Error: 2207026 unsupported"})
            return orig(method, path, q, form, who)
        with mock.patch.object(self.meta, "publishing", broken_format), \
                self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable), ("instagram_unsupported_media", False))

    def test_a_container_meta_no_longer_knows_is_replaced(self):
        self.state = {"ig_container": "C99", "ig_container_at": self.clock.now - 60, "ig_uploaded": True}
        res = self.publish()
        self.assertNotEqual(res["container_id"], "C99")
        self.assertEqual((self.meta.containers_created(), len(self.meta.media)), (1, 1))

    def test_an_old_container_is_not_reused(self):
        self.state = {"ig_container": "C99", "ig_container_at": self.clock.now - instagram.CONTAINER_TTL_S - 1,
                      "ig_uploaded": True}
        self.assertNotEqual(self.publish()["container_id"], "C99")
        self.assertEqual(self.meta.containers_created(), 1)

    def test_rate_limits_publishing_limit_and_permission(self):
        self.meta.create_plan = [FakeMeta.err(4, status=400, message="Application request limit reached")]
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable, e.exception.slow),
                         ("instagram_rate_limit", True, True))
        self.meta.quota = (100, 100)
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable), ("instagram_publish_limit", False))
        self.assertIn("100 von 100", str(e.exception))
        self.meta.quota = (0, 100)
        self.meta.create_plan = [FakeMeta.err(10, message="Application does not have permission")]
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable), ("instagram_permission", False))
        self.meta.create_plan = [FakeMeta.err(100, message="Unsupported request: upload_type")]
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertIn("Facebook-Login", str(e.exception))  # Instagram Login: Meta documents resumable for FB only
        self.meta.create_plan = [FakeMeta.err(9, sub=2207042)]
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual(e.exception.code, "instagram_publish_limit")

    def test_a_dead_token_while_publishing_disconnects_with_an_alert(self):
        self.meta.dead.add(IG_TOKEN)
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual((e.exception.code, e.exception.retryable), ("instagram_disconnected", False))
        self.assertFalse(store.get_account(self.aid)["connected"])
        self.assertTrue(store.get_alert(f"instagram.disconnected.{self.aid}"))
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual(e.exception.code, "not_connected")

    def test_a_file_instagram_would_refuse_is_never_sent(self):
        self.var = self.variant(make_mp4(duration=2.0, faststart=False))
        with self.assertRaises(instagram.InstagramError) as e:
            self.publish()
        self.assertEqual(e.exception.code, "instagram_unsupported_media")
        self.assertIn("mindestens 3 s", str(e.exception))
        self.assertIn("faststart", str(e.exception))
        self.assertEqual(self.meta.containers_created(), 0)

    def test_the_facebook_page_path_publishes_on_graph_facebook_com(self):
        store.set_secret("meta_app_id", META_ID)
        store.set_secret("meta_app_secret", META_SECRET)
        self.aid = instagram.connect_token(FB_SHORT)[0]["id"]
        self.assertEqual(self.publish()["media_id"], "M2")
        create = next(c for c in self.meta.calls if c[0] == "POST" and c[1].endswith("/media"))
        self.assertTrue(create[1].startswith(f"https://graph.facebook.com/{V}/17841400000000002/media"))
        self.assertEqual(create[2]["Authorization"], "Bearer EAApagePAGE10123456789abcdef")


# ---------------------------------------------------------------- caption and file

class CaptionTests(unittest.TestCase):
    def test_caption_reads_well_on_instagram(self):
        v = {"title": "Kai Did WHAT In GTA 6?", "description": "Kai found a secret #wow\n\nClip from Kai Cenat: "
             "https://youtu.be/x\n\n#gta6 #gtavi #shorts", "hashtags": ["#gta6", "#KaiCenat", "#Shorts", "gta"]}
        self.assertEqual(instagram.caption(v), "Kai Did WHAT In GTA 6?\n\nKai found a secret #wow\n\nClip from Kai "
                                               "Cenat: https://youtu.be/x\n\n#gta6 #gtavi #KaiCenat #gta")
        self.assertEqual(instagram.caption({"title": "Same", "description": "Same as the title\n#gta6 #shorts"}),
                         "Same as the title\n\n#gta6")
        self.assertEqual(instagram.caption({"title": "", "description": "", "hashtags": []}, {"title": "Video"}),
                         "Video")

    def test_limits(self):
        tags = [f"#tag{i}" for i in range(40)]
        c = instagram.caption({"title": "T", "description": "Body", "hashtags": tags})
        self.assertEqual(len([w for w in c.split() if w.startswith("#")]), 30)
        mentions = " ".join(f"@user{i}" for i in range(25))
        c = instagram.caption({"title": "T", "description": f"Hi {mentions}"})
        self.assertEqual(len([w for w in c.split() if w.startswith("@")]), 20)
        self.assertIn(" user24", c)
        long = "wirklich " * 400
        c = instagram.caption({"title": "T", "description": f"{long}\n\nClip from Kai: https://youtu.be/x\n\n#gta6"})
        self.assertLessEqual(len(c.encode("utf-16-le")) // 2, instagram.CAPTION_MAX)
        self.assertTrue(c.endswith("…\n\nClip from Kai: https://youtu.be/x\n\n#gta6"))  # credit and tags stay
        emoji = instagram.caption({"title": "😂" * 1500, "description": ""})
        self.assertLessEqual(len(emoji.encode("utf-16-le")) // 2, instagram.CAPTION_MAX)


class FileTests(unittest.TestCase):
    def check(self, data):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.mp4"
            f.write_bytes(data)
            return instagram.check_file(f)

    def test_our_exports_pass(self):
        r = self.check(make_mp4(duration=45.0))
        self.assertEqual(r["problems"], [])
        self.assertEqual((r["width"], r["height"], r["fps"], r["video_codec"], r["audio_codec"], r["sample_rate"],
                          r["channels"], r["faststart"]), (1080, 1920, 30.0, "avc1", "mp4a", 48000, 2, True))
        self.assertAlmostEqual(r["duration"], 45.0)

    def test_what_instagram_refuses_is_named_in_german(self):
        cases = [(dict(duration=2.5), "mindestens 3 s"), (dict(duration=901), "länger als 15 Minuten"),
                 (dict(width=2160, height=3840), "höchstens 1920"), (dict(fps=120), "23 bis 60"),
                 (dict(vcodec="mp4v"), "H.264 oder HEVC"), (dict(acodec="Opus"), "AAC"),
                 (dict(rate=64000), "48 kHz"), (dict(channels=6), "Mono oder Stereo"),
                 (dict(faststart=False), "faststart"), (dict(width=4000, height=100), "1920")]
        for kw, needle in cases:
            problems = self.check(make_mp4(**kw))["problems"]
            self.assertTrue(any(needle in p for p in problems), (kw, problems))
        self.assertEqual(self.check(b"not an mp4 at all")["problems"], ["Das ist keine gültige MP4-Datei."])
        r = self.check(make_mp4(duration=4.0, payload=b"\0" * (14 * 1000 * 1000)))
        self.assertTrue(any("Bitrate" in p for p in r["problems"]))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_a_real_ffmpeg_render_like_ours(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "ours.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30", "-f",
                            "lavfi", "-i", "sine=f=440:sample_rate=48000", "-t", "4", "-c:v", "libx264", "-pix_fmt",
                            "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                            str(out)], check=True, timeout=120)
            r = instagram.check_file(out)
        self.assertEqual(r["problems"], [])
        self.assertEqual((r["width"], r["height"], round(r["fps"]), r["sample_rate"]), (1080, 1920, 30, 48000))


class TransportTests(unittest.TestCase):
    def test_errors_never_carry_a_token(self):
        r = instagram.HttpResponse(400, {}, json.dumps({"error": {
            "code": 100, "message": f"Bad token IGAAsecret0123456789abcdefghij and access_token={IG_TOKEN}"}}).encode())
        text = str(instagram._graph_error(r))
        self.assertNotIn("IGAAsecret", text)
        self.assertNotIn(IG_TOKEN, text)
        self.assertEqual(instagram.token_kind("EAAx"), "facebook")
        self.assertEqual(instagram.token_kind("IGQVx"), "instagram")
        self.assertIsNone(instagram.token_kind("abc"))


if __name__ == "__main__":
    unittest.main()
