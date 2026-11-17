"""automate YouTube connection and upload (automate/youtube.py, automate/social.py, the /api/social routes): OAuth
consent URL, callback with single-use session-bound `state` (also when the dashboard runs on another host name than
the localhost redirect), token refresh and `invalid_grant`, the resumable upload with dropped connections, 5xx, a
lost session, 401 and cancel, and the YouTube metadata limits. Google is faked completely through
youtube.transport; one test runs the real stdlib transport against a local HTTP server."""
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from automate import db, security, server, social, store, youtube

CSRF = {"X-Requested-With": "automate"}
CLIENT_ID = "123456789012-abcdefg.apps.googleusercontent.com"
SESSION_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=S"


class FakeGoogle:
    """Google's OAuth, channels and resumable upload endpoints, scripted per test."""

    def __init__(self):
        self.calls, self.access, self.refresh = [], "ya29.first", "1//refresh"
        self.scope = f"{youtube.SCOPE_UPLOAD} {youtube.SCOPE_READONLY}"
        self.refresh_error = None
        self.plan = []  # per chunk PUT: ok | drop:N (keep N bytes, then reset the connection) | 503 | 404 | 401
        self.init_errors = []  # responses for the session POST before it succeeds
        self.sessions, self.data, self.total, self.complete, self.metadata = 0, bytearray(), None, False, None

    def __call__(self, method, url, *, headers=None, body=None, timeout=60):
        headers = dict(headers or {})
        self.calls.append((method, url, headers, body))
        if url == youtube.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form["grant_type"] == "authorization_code":
                if form["code"] != "good-code":
                    return self.res(400, {"error": "invalid_grant"})
                return self.res(200, {"access_token": self.access, "refresh_token": self.refresh, "expires_in": 3599,
                                      "scope": self.scope, "token_type": "Bearer"})
            if self.refresh_error:
                return self.res(400, {"error": self.refresh_error})
            self.access = f"ya29.refreshed{len(self.calls)}"
            return self.res(200, {"access_token": self.access, "expires_in": 3599, "token_type": "Bearer"})
        if url == youtube.REVOKE_URL:
            return self.res(200, {})
        if headers.get("Authorization") != f"Bearer {self.access}":
            return self.res(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
        if url.startswith(youtube.API_URL + "/channels"):
            channel = {"id": "UC123", "snippet": {"title": "Mein Kanal", "customUrl": "@meinkanal"}}
            return self.res(200, {"items": [channel]})
        if method == "POST" and url.startswith(youtube.UPLOAD_URL):
            if self.init_errors:
                return self.init_errors.pop(0)
            self.sessions += 1
            self.metadata, self.total = json.loads(body), int(headers["X-Upload-Content-Length"])
            self.data, self.complete = bytearray(), False
            return youtube.HttpResponse(200, {"location": f"{SESSION_URL}{self.sessions}"}, b"")
        if method == "PUT":
            if url != f"{SESSION_URL}{self.sessions}":
                return self.res(404, {})
            cr = headers["Content-Range"]
            if cr.startswith("bytes */"):
                return self.done() if self.complete else self.incomplete()
            start, end, total = map(int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr).groups())
            assert start == len(self.data) and len(body) == end - start + 1 and total == self.total, cr
            action = self.plan.pop(0) if self.plan else "ok"
            if action.startswith("drop:"):
                self.data += body[:int(action[5:])]
                raise ConnectionResetError("connection reset by peer")
            if action == "503":
                return self.res(503, {"error": {"code": 503, "message": "backend error"}})
            if action == "404":
                self.sessions += 1  # the old session URL is gone
                return self.res(404, {})
            if action == "401":
                self.access = "ya29.rotated"
                return self.res(401, {})
            self.data += body
            if len(self.data) == self.total:
                self.complete = True
                return self.done()
            return self.incomplete()
        raise AssertionError(f"unexpected {method} {url}")

    def incomplete(self):
        h = {"range": f"bytes=0-{len(self.data) - 1}"} if self.data else {}
        return youtube.HttpResponse(308, h, b"")

    def done(self):
        return self.res(200, {"id": "VID123", "snippet": self.metadata["snippet"]})

    @staticmethod
    def res(status, data):
        return youtube.HttpResponse(status, {"content-type": "application/json"}, json.dumps(data).encode())


class YoutubeCase(unittest.TestCase):
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
        self.google = FakeGoogle()
        p1 = mock.patch.object(youtube, "transport", self.google)
        p2 = mock.patch.object(youtube, "_sleep", self.slept)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        self.waits = []
        store.set_secret("youtube_client_id", CLIENT_ID)
        store.set_secret("youtube_client_secret", "GOCSPX-test-secret")
        self.app = server.create_app(background=False)

    def slept(self, seconds, cancel=None):
        self.waits.append(seconds)

    def login(self, base="http://127.0.0.1:8765"):
        if store.get_user() is None:
            store.create_user("owner", "scrypt$x", security.new_totp_secret(), totp_enabled=True)
        token, h = security.new_session_token()
        store.create_session(h, store.get_user()["id"], time.time() + 3600)
        c = TestClient(self.app, base_url=base, headers=CSRF)
        c.cookies.set("automate_session", token)
        return c

    def connected_account(self, expires_in=3600):
        creds = {"access_token": self.google.access, "refresh_token": self.google.refresh,
                 "expires_at": time.time() + expires_in, "scope": self.google.scope, "token_type": "Bearer"}
        return store.add_account("youtube", "Main", connected=True, external_id="UC123", credentials=creds)

    def variant(self, size=2600, lang="en", **fields):
        folder = self.root / "exports" / "gta6" / "2026-11-17" / f"video {lang}"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / "hook.mp4"
        self.payload = os.urandom(size)
        f.write_bytes(self.payload)
        vid = store.add_video("gta6", folder, lang=lang, title="Video")
        fields = {"hook": "Hook", "title": "GTA 6 is insane", "hashtags": ["#gta6", "#shorts"],
                  "description": "Look at this\n\nClip from X: https://y\n\n#gta6", **fields}
        return store.add_variant(vid, f, **fields)


# ---------------------------------------------------------------- OAuth

class OAuthTests(YoutubeCase):
    def connect(self, client, **body):
        r = client.post("/api/social/youtube/connect", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        url = urlsplit(r.json()["auth_url"])
        return url, {k: v[0] for k, v in parse_qs(url.query).items()}

    def callback(self, query, host="localhost:8765"):
        c = TestClient(self.app, base_url=f"http://{host}")  # Google's redirect: no cookie, cross-site
        return c.get(f"/api/social/youtube/callback?{query}", headers={"Sec-Fetch-Site": "cross-site"})

    def test_consent_url_and_callback_create_the_account(self):
        client = self.login()  # the owner browses on 127.0.0.1; Google comes back to localhost
        url, q = self.connect(client)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", youtube.AUTH_URL)
        self.assertEqual(q["scope"].split(), list(youtube.SCOPES))
        self.assertEqual((q["access_type"], q["prompt"], q["include_granted_scopes"], q["response_type"]),
                         ("offline", "consent", "true", "code"))
        self.assertEqual((q["client_id"], q["redirect_uri"]),
                         (CLIENT_ID, "http://localhost:8765/api/social/youtube/callback"))
        self.assertGreaterEqual(len(q["state"]), 40)
        r = self.callback(f"code=good-code&state={q['state']}&scope=x")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("http://127.0.0.1:8765/#/konten?youtube=connected&amp;account=", r.text)
        self.assertIn('http-equiv="refresh"', r.text)
        self.assertEqual((r.headers["referrer-policy"], r.headers["cache-control"]), ("no-referrer", "no-store"))
        token_call = next(c for c in self.google.calls if c[1] == youtube.TOKEN_URL)
        form = parse_qs(token_call[3].decode())
        self.assertEqual((form["redirect_uri"][0], form["client_secret"][0]),
                         ("http://localhost:8765/api/social/youtube/callback", "GOCSPX-test-secret"))
        accounts = client.get("/api/social/accounts").json()["items"]
        self.assertEqual([(a["platform"], a["label"], a["handle"], a["external_id"], a["connected"]) for a in accounts],
                         [("youtube", "Mein Kanal", "@meinkanal", "UC123", True)])
        self.assertNotIn("ya29", json.dumps(accounts))
        creds = store.get_account(accounts[0]["id"], with_credentials=True)["credentials"]
        self.assertEqual((creds["access_token"], creds["refresh_token"]), ("ya29.first", "1//refresh"))
        self.assertTrue(client.get("/api/overview").json()["ready"]["youtube_connected"])
        again = self.callback(f"code=good-code&state={q['state']}")  # the state worked once
        self.assertEqual(again.status_code, 400)
        self.assertIn("abgelaufen oder wurde schon benutzt", again.text)
        _, q2 = self.connect(client)
        self.callback(f"code=good-code&state={q2['state']}")  # the same channel again: no duplicate account
        self.assertEqual(len(store.list_accounts("youtube")), 1)

    def test_state_must_be_known_fresh_and_its_session_alive(self):
        client = self.login()
        for query in ("code=good-code", "code=good-code&state=" + "x" * 43, "code=good-code&state=short"):
            r = self.callback(query)
            self.assertEqual(r.status_code, 400, query)
            self.assertIn("youtube=error&amp;message=", r.text)
        _, q = self.connect(client)
        with mock.patch.object(social.time, "time", return_value=time.time() + social.STATE_TTL_S + 1):
            self.assertIn("abgelaufen", self.callback(f"code=good-code&state={q['state']}").text)
        _, q = self.connect(client)
        client.post("/api/logout")
        r = self.callback(f"code=good-code&state={q['state']}")
        self.assertIn("Sitzung", r.text)
        self.assertEqual(store.list_accounts(), [])
        client = self.login()
        _, q = self.connect(client)
        r = self.callback(f"error=access_denied&state={q['state']}")
        self.assertIn("nicht erlaubt", r.text)
        _, q = self.connect(client)
        self.assertIn("abgelaufen oder wurde schon benutzt", self.callback(f"code=bad&state={q['state']}").text)

    def test_reconnecting_an_account_and_a_missing_upload_scope(self):
        client = self.login()
        aid = store.add_account("youtube", "Zweitkanal")
        _, q = self.connect(client, account_id=aid)
        self.google.scope = youtube.SCOPE_READONLY  # the owner unticked "upload" on Google's consent screen
        r = self.callback(f"code=good-code&state={q['state']}")
        self.assertIn("Häkchen", r.text)
        self.assertFalse(store.get_account(aid)["connected"])
        self.google.scope = " ".join(youtube.SCOPES)
        _, q = self.connect(client, account_id=aid)
        self.assertEqual(self.callback(f"code=good-code&state={q['state']}").status_code, 200)
        a = store.get_account(aid)
        self.assertEqual((a["label"], a["connected"], a["external_id"], a["handle"]),
                         ("Zweitkanal", True, "UC123", "@meinkanal"))
        tt = store.add_account("tiktok", "TT")
        self.assertEqual(client.post("/api/social/youtube/connect", json={"account_id": tt}).status_code, 422)

    def test_disconnect_revokes_and_forgets_the_tokens(self):
        client = self.login()
        aid = self.connected_account()
        r = client.post(f"/api/social/accounts/{aid}/disconnect")
        self.assertEqual(r.json()["connected"], False)
        revoke = next(c for c in self.google.calls if c[1] == youtube.REVOKE_URL)
        self.assertEqual(parse_qs(revoke[3].decode())["token"], ["1//refresh"])
        self.assertIsNone(store.get_account(aid, with_credentials=True)["credentials"])

    def test_tokens_refresh_and_invalid_grant_disconnects(self):
        aid = self.connected_account(expires_in=30)  # expires within REFRESH_AHEAD_S
        token = youtube.access_token(aid)
        self.assertTrue(token.startswith("ya29.refreshed"))
        creds = store.get_account(aid, with_credentials=True)["credentials"]
        self.assertEqual((creds["access_token"], creds["refresh_token"]), (token, "1//refresh"))
        self.assertGreater(creds["expires_at"], time.time() + 3000)
        n = len(self.google.calls)
        self.assertEqual(youtube.access_token(aid), token)  # still valid: no request
        self.assertEqual(len(self.google.calls), n)
        self.google.refresh_error = "invalid_grant"
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.access_token(aid, force=True)
        self.assertEqual(e.exception.code, "youtube_disconnected")
        self.assertFalse(e.exception.retryable)
        a = store.get_account(aid, with_credentials=True)
        self.assertEqual((a["connected"], a["credentials"]), (False, None))
        alert = store.get_alert(f"youtube.disconnected.{aid}")
        self.assertIn("neu verbinden", alert["message"])
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.access_token(aid)
        self.assertEqual(e.exception.code, "not_connected")


# ---------------------------------------------------------------- upload

class UploadTests(YoutubeCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(youtube, "CHUNK", 1024)
        p.start()
        self.addCleanup(p.stop)
        self.aid = self.connected_account()

    def test_resumable_upload_survives_drops_and_server_errors(self):
        var = self.variant(2600)
        self.google.plan = ["ok", "drop:300", "503", "ok", "ok"]
        progress = []
        res = youtube.upload_variant(var, self.aid, "unlisted", on_progress=lambda d, t: progress.append((d, t)))
        self.assertEqual(res, {"video_id": "VID123", "url": "https://youtube.com/shorts/VID123"})
        self.assertEqual(bytes(self.google.data), self.payload)
        self.assertEqual(progress[-1], (2600, 2600))
        self.assertIn((1324, 2600), progress)  # resumed where the server stopped, not at the chunk start
        ranges = [c[2]["Content-Range"] for c in self.google.calls if c[0] == "PUT"]
        self.assertEqual(ranges, ["bytes 0-1023/2600", "bytes 1024-2047/2600", "bytes */2600", "bytes 1324-2347/2600",
                                  "bytes */2600", "bytes 1324-2347/2600", "bytes 2348-2599/2600"])
        self.assertEqual(len(self.waits), 2)
        meta = self.google.metadata
        self.assertEqual(meta["status"], {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False})
        self.assertEqual((meta["snippet"]["title"], meta["snippet"]["categoryId"]), ("GTA 6 is insane", "20"))
        self.assertTrue(meta["snippet"]["description"].endswith("#gta6 #Shorts"))
        self.assertEqual(meta["snippet"]["tags"], ["gta6", "shorts"])
        init = next(c for c in self.google.calls if c[0] == "POST" and c[1].startswith(youtube.UPLOAD_URL))
        self.assertIn("uploadType=resumable", init[1])
        self.assertEqual((init[2]["X-Upload-Content-Length"], init[2]["X-Upload-Content-Type"]), ("2600", "video/mp4"))

    def test_lost_session_and_expired_token_mid_upload(self):
        var = self.variant(2600)
        self.google.plan = ["ok", "404", "ok", "401", "ok", "ok"]
        res = youtube.upload_variant(var, self.aid, "private", title="Eigener Titel", description="Eigene Beschreibung")
        self.assertEqual(res["video_id"], "VID123")
        posts = [c for c in self.google.calls if c[0] == "POST" and c[1].startswith(youtube.UPLOAD_URL)]
        self.assertEqual(len(posts), 2)  # the lost session was replaced once
        self.assertEqual(bytes(self.google.data), self.payload)
        self.assertEqual(self.google.metadata["snippet"]["title"], "Eigener Titel")
        self.assertEqual(self.google.metadata["snippet"]["description"], "Eigene Beschreibung\n\n#Shorts")
        refreshed = [c for c in self.google.calls if c[1] == youtube.TOKEN_URL]
        self.assertEqual(len(refreshed), 1)

    def test_a_retry_asks_the_saved_session_first_and_never_uploads_twice(self):
        var = self.variant(2600)
        seen = []
        # 1) the server dies right after YouTube accepted the last byte (before the DB write): the session was kept
        self.assertEqual(youtube.upload_variant(var, self.aid, "private", on_session=seen.append)["video_id"],
                         "VID123")
        self.assertEqual(seen, [f"{SESSION_URL}1"])
        before = len(self.google.calls)
        res = youtube.upload_variant(var, self.aid, "private", session=seen[0], on_session=seen.append)
        self.assertEqual(res["video_id"], "VID123")
        retry = self.google.calls[before:]
        self.assertEqual([(c[0], c[2].get("Content-Range")) for c in retry if c[0] in ("PUT", "POST")],
                         [("PUT", "bytes */2600")])  # one status question, no new session, no bytes
        self.assertEqual((self.google.sessions, len(seen)), (1, 1))
        # 2) it died half-way: the retry continues the same session where YouTube stopped
        self.google.plan = ["ok", "drop:0"]
        with mock.patch.object(youtube, "MAX_RETRIES", 0), self.assertRaises(youtube.YouTubeError):
            youtube.upload_variant(var, self.aid, "private", on_session=seen.append)
        self.assertEqual(len(self.google.data), 1024)
        res = youtube.upload_variant(var, self.aid, "private", session=seen[-1], on_session=seen.append)
        self.assertEqual((res["video_id"], bytes(self.google.data)), ("VID123", self.payload))
        self.assertEqual(self.google.sessions, 2)  # the second attempt opened no third session
        # 3) the saved session expired (about a week): a new one starts
        res = youtube.upload_variant(var, self.aid, "private", session=f"{SESSION_URL}1", on_session=seen.append)
        self.assertEqual((res["video_id"], self.google.sessions, seen[-1]), ("VID123", 3, f"{SESSION_URL}3"))

    def test_gives_up_after_repeated_failures_and_honours_cancel(self):
        var = self.variant(2600)
        self.google.plan = ["503"] * 30
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.upload_variant(var, self.aid, "private")
        self.assertTrue(e.exception.retryable)
        self.assertEqual(len(self.waits), youtube.MAX_RETRIES)
        self.assertLessEqual(max(self.waits), 65)
        cancel = threading.Event()
        self.google.plan = []
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.upload_variant(var, self.aid, "private", cancel=cancel, on_progress=lambda d, t: cancel.set())
        self.assertEqual(e.exception.code, "canceled")
        self.assertEqual(len(self.google.data), 1024)  # stopped after the first chunk

    def test_quota_and_refused_inputs(self):
        var = self.variant(100)
        self.google.init_errors = [FakeGoogle.res(403, {"error": {"code": 403, "message": "quota", "errors": [
            {"reason": "quotaExceeded", "domain": "youtube.quota"}]}})]
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.upload_variant(var, self.aid, "public")
        self.assertIn("kontingent", str(e.exception))
        self.assertTrue(e.exception.retryable)
        with self.assertRaises(youtube.YouTubeError):
            youtube.upload_variant(var, self.aid, "everyone")
        outside = self.root / "elsewhere.mp4"
        outside.write_bytes(b"x")
        bad = store.add_variant(store.get_variant(var)["video_id"], outside, hook="x")
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.upload_variant(bad, self.aid, "private")
        self.assertEqual(e.exception.code, "file_missing")
        other = store.add_account("youtube", "Nicht verbunden")
        with self.assertRaises(youtube.YouTubeError) as e:
            youtube.upload_variant(var, other, "private")
        self.assertEqual(e.exception.code, "not_connected")

    def test_metadata_follows_youtube_limits(self):
        long = "GTA 6 <leak> " + "wirklich " * 20
        res = youtube.video_resource({"title": long, "description": "ä" * 3000 + "\n#gta6", "hashtags": ["#gta 6"] * 3},
                                     {"lang": "es"}, "public")
        t, d = res["snippet"]["title"], res["snippet"]["description"]
        self.assertLessEqual(len(t), 100)
        self.assertNotIn("<", t + d)
        self.assertFalse(t.endswith(" "))
        self.assertLessEqual(len(d.encode("utf-8")), 5000)
        self.assertTrue(d.endswith("#Shorts"))
        self.assertEqual(res["snippet"]["tags"], ["gta 6"])
        self.assertTrue(res["status"]["containsSyntheticMedia"])  # an AI dub
        self.assertEqual(youtube.clean_description("x #Shorts here"), "x #Shorts here")
        self.assertEqual(youtube.clean_title(""), "GTA 6")
        var = self.variant(100, lang="ru")
        self.google.init_errors = [FakeGoogle.res(400, {"error": {"message": "Unknown name containsSyntheticMedia"}})]
        self.assertEqual(youtube.upload_variant(var, self.aid, "private")["video_id"], "VID123")
        self.assertNotIn("containsSyntheticMedia", self.google.metadata["status"])


class TransportTests(unittest.TestCase):
    def test_the_stdlib_transport_returns_308_and_errors_as_responses(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                status = 308 if self.path == "/resume" else 404
                self.send_response(status)
                if status == 308:
                    self.send_header("Range", "bytes=0-99")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            r = youtube.transport("PUT", f"{base}/resume", headers={"Content-Range": "bytes 0-99/200"}, body=b"x" * 100)
            self.assertEqual((r.status, r.headers.get("range"), r.body), (308, "bytes=0-99", b"{}"))
            self.assertEqual(youtube.transport("PUT", f"{base}/gone", body=b"").status, 404)
            with self.assertRaises(OSError):
                youtube.transport("GET", "http://127.0.0.1:1/", timeout=2)


if __name__ == "__main__":
    unittest.main()
