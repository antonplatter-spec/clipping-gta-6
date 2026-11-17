"""automate REST API (automate/server.py, automate/api/): login (setup, 2FA, rate limits, sessions), the security layer
(auth on every route, CSRF, Host/Origin, headers, secret leaks), file serving with Range, and every endpoint of
automate/web/js/api.js with the pipeline / health / agent / scheduler modules faked. Each test runs on its own temp
data dir."""
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from automate import db, importer, paths, runtime, security, server, store
from automate.api import PUBLIC_ROUTES, auth

PASSWORD = "correct horse battery"
CSRF = {"X-Requested-With": "automate"}
BASE = "http://127.0.0.1:8765"
_PW_HASH = []


def pw_hash():
    if not _PW_HASH:
        _PW_HASH.append(security.hash_password(PASSWORD))
    return _PW_HASH[0]


class PipelineError(Exception):
    def __init__(self, message, code="conflict", status=409):
        super().__init__(message)
        self.code, self.status = code, status


class PickCheckFailed(PipelineError):
    def __init__(self, message, report):
        super().__init__(message, "check_failed", 422)
        self.report = report


def fake_modules():
    """Stand-ins for the modules the other agents write (pipeline, health, agent, scheduler)."""
    pipeline = types.ModuleType("automate.pipeline")
    pipeline.PipelineError, pipeline.PickCheckFailed = PipelineError, PickCheckFailed
    pipeline.enqueue_url = mock.Mock(side_effect=lambda url, profile, section=None, trigger="dashboard":
                                     store.create_job("source", profile, source_url=url, section=section))
    pipeline.request_scan = mock.Mock(return_value=[1])
    pipeline.approve_pick = mock.Mock(side_effect=lambda pid: (store.update_pick(pid, status="approved"),
                                                               store.create_job("render", "gta6"))[1])
    pipeline.reject_pick = mock.Mock(side_effect=lambda pid, reason=None:
                                     store.update_pick(pid, status="rejected", error=reason))
    pipeline.update_pick_data = mock.Mock(side_effect=lambda pid, changes: (
        store.update_pick(pid, data={**store.get_pick(pid)["data"], **changes}), {}))
    pipeline.enqueue_dub = mock.Mock(side_effect=lambda vid, lang: store.create_job("dub", "gta6"))
    pipeline.enqueue_upload = mock.Mock(side_effect=lambda vid, aid, privacy: store.create_job("upload", "gta6"))
    pipeline.retry_job = mock.Mock(side_effect=lambda jid: store.update_job(jid, status="queued"))
    pipeline.cancel_job = mock.Mock(side_effect=lambda jid: store.update_job(jid, status="canceled"))
    pipeline.on_settings_changed = mock.Mock()
    pipeline.source_expires_at = mock.Mock(return_value=1234.5)
    health = types.ModuleType("automate.health")
    health.live_status = mock.Mock(return_value={"streams": [], "recorders": 0, "checked_at": None})
    health.system_report = mock.Mock(return_value={
        "version": "0.1.0", "python": "3.12", "platform": "Linux", "paths": {"root": "/x"},
        "disk": {"path": "/x", "free_gb": 100.0, "total_gb": 500.0, "min_free_gb": 30},
        "tools": [{"name": "ffmpeg", "ok": True, "version": "7.1", "hint": None},
                  {"name": "demucs", "ok": False, "version": None, "hint": "uv run python -m clipper setup --install",
                   "required": True}], "setup_report": "ok"})
    agent = types.ModuleType("automate.agent")
    agent.test_key = mock.Mock(return_value=(True, "Der Schlüssel funktioniert."))
    agent.system_prompt_chars = mock.Mock(return_value=12345)
    scheduler = types.ModuleType("automate.scheduler")
    scheduler.next_scan_time = mock.Mock(return_value=1900000000.0)
    return {"automate.pipeline": pipeline, "automate.health": health, "automate.agent": agent,
            "automate.scheduler": scheduler}


class ApiCase(unittest.TestCase):
    """Temp data dir, the app without background services, fakes for the other agents' modules."""
    modules = True

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
        quiet = logging.getLogger("automate")
        self.addCleanup(quiet.setLevel, quiet.level)
        quiet.setLevel(logging.CRITICAL)  # refused hosts etc. are logged on purpose; keep the test output clean
        store.ensure_profile("gta6", True)
        store.ensure_profile("gta-rp", False)
        self.fakes = fake_modules() if self.modules else {n: None for n in ("automate.pipeline", "automate.health",
                                                                             "automate.agent", "automate.scheduler")}
        mods = mock.patch.dict(sys.modules, self.fakes)
        mods.start()
        self.addCleanup(mods.stop)
        self.bridge = mock.patch.object(runtime, "run_bridge", side_effect=self.fake_bridge)
        self.bridge_mock = self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.app = server.create_app(background=False)
        self.anon = TestClient(self.app, base_url=BASE, headers=CSRF)
        self.token = auth.issue_setup_token()  # what `python -m automate` prints while the setup is open

    def fake_bridge(self, op, payload, profile, **kw):
        if op == "resolve_creator":
            ref = payload["ref"]
            if "nobody" in ref:
                return {"url": f"https://www.youtube.com/@{ref.lstrip('@')}/videos", "name": None, "found": False}
            return {"url": f"https://www.youtube.com/@{ref.lstrip('@')}/videos", "name": ref.lstrip("@").title(),
                    "found": True}
        if op == "resolve_stream":
            site, _, name = payload["ref"].partition(":")
            if site not in ("twitch", "kick", "youtube"):
                raise runtime.BridgeError(f"unknown stream ref {payload['ref']!r}")
            return {"url": f"https://www.{site}.tv/{name}", "name": f"{name}-{site}"}
        if op == "music_library":
            return {"tracks": [{"id": "epic1", "file": "assets/music/epic1.m4a", "kind": "music", "mood": "epic",
                                "claimable": False, "approved": False, "usable": False, "local": False,
                                "exists": True}]}
        if op == "set_music":
            return {"track": {"id": payload["id"], "file": "assets/music/epic1.m4a", "kind": "music", "mood": "epic",
                              "claimable": False, "approved": payload["approved"], "usable": payload["approved"],
                              "local": False, "exists": True}}
        if op == "fish_key_check":
            return {"ok": False, "message": "Fish Audio hat den Schlüssel abgelehnt (HTTP 401)."}
        raise AssertionError(f"unexpected bridge op {op}")

    # ---- login helpers
    def make_user(self, *, confirmed=True):
        secret = security.new_totp_secret()
        plain, hashed = security.new_recovery_codes()
        uid = store.create_user("owner", pw_hash(), secret, totp_enabled=confirmed, recovery_codes=hashed)
        self.secret, self.recovery, self.uid = secret, plain, uid
        return uid

    def client(self, *, csrf=True, base=BASE):
        """A logged-in client (session created directly, no TOTP step used up)."""
        if store.get_user() is None:
            self.make_user()
        token, h = security.new_session_token()
        store.create_session(h, store.get_user()["id"], time.time() + 3600)
        c = TestClient(self.app, base_url=base, headers=CSRF if csrf else {})
        c.cookies.set("automate_session", token)
        c.token_hash = h
        return c


# ---------------------------------------------------------------- login

class AuthFlowTests(ApiCase):
    def test_setup_confirm_login_logout_with_real_totp_codes(self):
        st = self.anon.get("/api/state").json()
        self.assertEqual((st["setup_required"], st["setup_step"], st["authenticated"], st["setup_token_required"]),
                         (True, "account", False, True))
        r = self.anon.post("/api/setup", json={"username": "owner", "password": PASSWORD, "token": self.token})
        self.assertEqual(r.status_code, 200, r.text)
        setup = r.json()
        self.assertTrue(setup["otpauth_uri"].startswith("otpauth://totp/GTA%20Clipper:owner?secret="))
        self.assertIn("<svg", setup["qr_svg"])
        self.assertEqual(self.anon.get("/api/state").json()["setup_step"], "totp")
        self.assertEqual(self.anon.get("/api/overview").status_code, 401)  # no session before 2FA is confirmed
        r = self.anon.post("/api/setup/confirm", json={"code": security.totp_now(setup["totp_secret"]),
                                                       "token": self.token.lower().replace("-", " ")})  # as typed
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["recovery_codes"]), 10)
        cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("automate_session="))
        for flag in ("HttpOnly", "SameSite=strict", "Path=/", "Max-Age=1209600"):
            self.assertIn(flag.lower(), cookie.lower())
        self.assertNotIn("secure", cookie.lower())  # plain http on localhost
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertIsNone(store.get_session(token))  # stored hashed only
        self.assertIsNotNone(store.get_session(security.hash_token(token)))
        st = self.anon.get("/api/state").json()
        self.assertEqual((st["authenticated"], st["username"], st["totp_enabled"], st["setup_required"],
                          st["setup_token_required"]), (True, "owner", True, False, False))
        self.assertIsNone(store.get_meta("setup.token"))  # used up
        self.assertEqual(self.anon.get("/api/overview").status_code, 200)
        r = self.anon.post("/api/setup", json={"username": "x", "password": PASSWORD, "token": self.token})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "setup_done"))

        self.assertEqual(self.anon.post("/api/logout").json(), {"ok": True})
        self.assertIsNone(store.get_session(security.hash_token(token)))
        self.assertEqual(self.anon.get("/api/overview").status_code, 401)
        bad = self.anon.post("/api/login", json={"username": "owner", "password": PASSWORD, "code": "000000"})
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]), (401, "invalid_credentials"))
        self.assertEqual(bad.json()["error"]["message"], "Benutzername, Passwort oder Code ist falsch.")
        later = security.totp_now(setup["totp_secret"], time.time() + 30)  # the confirm used the current step
        r = self.anon.post("/api/login", json={"username": "owner", "password": PASSWORD, "code": later})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["authenticated"])
        self.assertEqual(self.anon.get("/api/overview").status_code, 200)

    def test_https_requests_get_a_secure_cookie(self):
        c = TestClient(self.app, base_url="https://localhost:8765", headers=CSRF)
        secret = c.post("/api/setup", json={"username": "owner", "password": PASSWORD,
                                            "token": self.token}).json()["totp_secret"]
        r = c.post("/api/setup/confirm", json={"code": security.totp_now(secret), "token": self.token})
        cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("automate_session="))
        self.assertIn("secure", cookie.lower())

    def test_setup_validation_and_wrong_confirm_code(self):
        r = self.anon.post("/api/setup", json={"username": "", "password": "short", "token": self.token})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.json()["error"]["fields"]), {"username", "password"})
        self.assertIn("10 Zeichen", r.json()["error"]["fields"]["password"])
        self.anon.post("/api/setup", json={"username": "owner", "password": PASSWORD, "token": self.token})
        r = self.anon.post("/api/setup/confirm", json={"code": "123456", "token": self.token})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "invalid_code"))
        self.assertIn("code", r.json()["error"]["fields"])
        r = self.anon.post("/api/setup/confirm", json={"code": "١٢٣٤٥٦", "token": self.token})  # non-ASCII digits
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "invalid_code"))
        self.assertFalse(store.get_user()["totp_enabled"])

    def test_setup_needs_the_terminal_code_and_confirm_needs_the_same_browser(self):
        for body, field in (({}, "Bitte den Einrichtungs-Code"), ({"token": "ABCD-EFGH-IJKL-MNOP-QRST"}, "stimmt nicht")):
            r = self.anon.post("/api/setup", json={"username": "mallory", "password": PASSWORD, **body})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "setup_token_invalid"))
            self.assertIn(field, r.json()["error"]["fields"]["token"])
            self.assertIn("Terminal", r.json()["error"]["message"])
        self.assertIsNone(store.get_user())  # nothing was created, no password was hashed
        first = self.anon.post("/api/setup", json={"username": "owner", "password": PASSWORD,
                                                   "token": self.token}).json()
        other = TestClient(self.app, base_url=BASE, headers=CSRF)  # no setup code: no restart, at any time
        with mock.patch("automate.api.auth.time.time", return_value=time.time() + 3600):
            r = other.post("/api/setup", json={"username": "intruder", "password": "another password"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(store.get_user()["username"], "owner")
        r = other.post("/api/setup/confirm", json={"code": security.totp_now(first["totp_secret"]),
                                                   "token": self.token})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "setup_restart"))  # not its setup cookie
        self.assertFalse(store.get_user()["totp_enabled"])
        again = other.post("/api/setup", json={"username": "owner", "password": PASSWORD + "!", "token": self.token})
        self.assertEqual(again.status_code, 200)  # the code's holder may start over in another browser
        old = self.anon.post("/api/setup/confirm", json={"code": security.totp_now(again.json()["totp_secret"]),
                                                         "token": self.token})
        self.assertEqual(old.status_code, 409)  # the first browser's cookie is no longer the pending one
        r = other.post("/api/setup/confirm", json={"code": security.totp_now(again.json()["totp_secret"]),
                                                   "token": self.token})
        self.assertEqual(r.status_code, 200, r.text)
        store.set_meta("setup.token", None)
        store.delete_user()
        r = self.anon.post("/api/setup", json={"username": "a", "password": PASSWORD, "token": self.token})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "setup_token_missing"))

    def test_setup_failures_are_capped_per_client_and_globally(self):
        self.make_user()  # setup done: refused at once, before any password hashing, and counted
        with mock.patch.object(security, "hash_password", side_effect=AssertionError("no scrypt")):
            codes = [self.anon.post("/api/setup", json={"username": "x", "password": PASSWORD}).status_code
                     for _ in range(auth.LIMIT_SETUP + 1)]
        self.assertEqual(codes, [409] * auth.LIMIT_SETUP + [429])
        store.delete_user()
        db.conn().execute("DELETE FROM login_attempts")
        clients = iter(f"10.0.0.{i}" for i in range(1000))
        with mock.patch.object(auth, "client_key", side_effect=lambda request: next(clients)):
            codes = [self.anon.post("/api/setup/confirm", json={"code": "000000", "token": "WRONG"}).status_code
                     for _ in range(auth.LIMIT_SETUP_ALL + 1)]
        self.assertEqual(codes[-1], 429)  # a new client address per request doesn't help
        self.assertEqual(set(codes[:-1]), {403})

    def test_two_simultaneous_setups_create_one_account(self):
        results, barrier = [], threading.Barrier(2)

        def go(name):
            c = TestClient(self.app, base_url=BASE, headers=CSRF)
            barrier.wait()
            results.append(c.post("/api/setup", json={"username": name, "password": PASSWORD + name,
                                                      "token": self.token}).status_code)
            db.close()
        threads = [threading.Thread(target=go, args=(n,)) for n in ("alice", "bobby")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), [200, 200])  # both hold the code: the later one replaces the first
        self.assertEqual(db.conn().execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_login_is_rate_limited_per_username_and_per_client(self):
        self.make_user()
        for _ in range(8):
            r = self.anon.post("/api/login", json={"username": "owner", "password": "wrong password", "code": "1"})
            self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/login", json={"username": "owner", "password": PASSWORD,
                                                "code": security.totp_now(self.secret)})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "rate_limited"))
        self.assertRegex(r.json()["error"]["message"], r"Bitte warte (1[45]|\d) Minute")
        self.assertTrue(1 <= int(r.headers["retry-after"]) <= 900)
        for i in range(12):  # other names from the same client: the client limit (20) kicks in too
            self.anon.post("/api/login", json={"username": f"guess{i}", "password": "x" * 10, "code": "123456"})
        r = self.anon.post("/api/login", json={"username": "fresh", "password": "x" * 10, "code": "123456"})
        self.assertEqual(r.status_code, 429)
        store.record_login_attempt("login.user:owner", False, ts=time.time() - 3600)  # old failures don't count
        self.assertEqual(store.recent_failures("login.user:owner", time.time() - 900), 8)

    def test_totp_codes_cannot_be_replayed_and_recovery_codes_work_once(self):
        self.make_user()
        code = security.totp_now(self.secret)
        body = {"username": "owner", "password": PASSWORD, "code": code}
        self.assertEqual(self.anon.post("/api/login", json=body).status_code, 200)
        self.assertEqual(self.anon.post("/api/login", json=body).status_code, 401)  # same step again
        rec = {"username": "owner", "password": PASSWORD, "code": self.recovery[3].upper()}
        self.assertEqual(self.anon.post("/api/login", json=rec).status_code, 200)
        self.assertEqual(self.anon.post("/api/login", json=rec).status_code, 401)
        self.assertEqual(len(store.get_user()["recovery_codes"]), 9)
        self.assertEqual(store.get_alert("login.recovery_code")["details"], {"left": 9})
        wrong_pw = {"username": "owner", "password": "not the password", "code": self.recovery[4]}
        self.assertEqual(self.anon.post("/api/login", json=wrong_pw).status_code, 401)
        self.assertEqual(len(store.get_user()["recovery_codes"]), 9)  # a wrong password doesn't burn a code

    def test_password_change_kills_other_sessions(self):
        a, b = self.client(), self.client()
        r = a.post("/api/account/password", json={"current_password": "nope nope nope", "new_password": PASSWORD + "2",
                                                  "code": security.totp_now(self.secret)})
        self.assertEqual(set(r.json()["error"]["fields"]), {"current_password"})
        r = a.post("/api/account/password", json={"current_password": PASSWORD, "new_password": "short",
                                                  "code": security.totp_now(self.secret)})
        self.assertEqual(set(r.json()["error"]["fields"]), {"new_password"})
        r = a.post("/api/account/password", json={"current_password": PASSWORD, "new_password": PASSWORD + "2",
                                                  "code": "000000"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"code"})
        r = a.post("/api/account/password", json={"current_password": PASSWORD, "new_password": PASSWORD + "2",
                                                  "code": security.totp_now(self.secret)})
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(a.get("/api/overview").status_code, 200)
        self.assertEqual(b.get("/api/overview").status_code, 401)
        self.assertTrue(security.verify_password(PASSWORD + "2", store.get_user()["password_hash"]))

    def test_totp_reenrolment_replaces_secret_and_recovery_codes(self):
        a, b = self.client(), self.client()
        self.assertEqual(set(a.post("/api/account/totp/reset", json={"password": "wrong!!!!!!"}).json()["error"]
                             ["fields"]), {"password"})
        self.assertEqual(a.post("/api/account/totp/confirm", json={"code": "123456"}).status_code, 409)
        new = a.post("/api/account/totp/reset", json={"password": PASSWORD}).json()
        self.assertNotEqual(new["totp_secret"], self.secret)
        self.assertEqual(store.get_user()["totp_secret"], self.secret)  # the old one stays until confirmed
        r = a.post("/api/account/totp/confirm", json={"code": security.totp_now(self.secret)})
        self.assertEqual(r.status_code, 422)
        r = a.post("/api/account/totp/confirm", json={"code": security.totp_now(new["totp_secret"])})
        codes = r.json()["recovery_codes"]
        self.assertEqual(len(codes), 10)
        user = store.get_user()
        self.assertEqual(user["totp_secret"], new["totp_secret"])
        self.assertIsNone(security.use_recovery_code(self.recovery[0], user["recovery_codes"]))
        self.assertIsNotNone(security.use_recovery_code(codes[0], user["recovery_codes"]))
        self.assertEqual(b.get("/api/overview").status_code, 401)
        self.assertEqual(a.get("/api/overview").status_code, 200)

    def test_sessions_slide_and_expire(self):
        c = self.client()
        store.touch_session(c.token_hash, time.time() + 60, now=time.time() - 1000)
        r = c.get("/api/overview")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(store.get_session(c.token_hash)["expires_at"], time.time() + 13 * 86400)
        self.assertTrue(any(h.startswith("automate_session=") for h in r.headers.get_list("set-cookie")))
        r = c.get("/api/overview")  # just touched: no write, no cookie
        self.assertFalse(r.headers.get_list("set-cookie"))
        store.touch_session(c.token_hash, time.time() - 1)
        self.assertEqual(c.get("/api/overview").status_code, 401)
        self.assertIsNone(store.get_session(c.token_hash))

    def test_reset_login_command_restarts_the_setup(self):
        from io import StringIO
        from automate import __main__ as cli
        c = self.client()
        auth.clear_setup_token()  # the setup is done: no code exists
        with mock.patch("builtins.input", return_value="nein"), mock.patch("sys.stdout"):
            self.assertEqual(cli.reset_login([]), 1)
        self.assertEqual(c.get("/api/overview").status_code, 200)
        store.set_secret("anthropic_api_key", "sk-ant-api03-kept-key-0123456789")
        out = StringIO()
        with mock.patch("builtins.input", return_value="RESET"), mock.patch("sys.stdout", out):
            self.assertEqual(cli.reset_login([]), 0)
        text = out.getvalue()
        self.assertIn("BLEIBEN erhalten", text)  # says clearly that keys and YouTube stay
        self.assertEqual(c.get("/api/overview").status_code, 401)
        self.assertIsNone(store.get_user())  # the row is gone, not just emptied
        self.assertIsNone(store.get_session(c.token_hash))
        self.assertTrue(store.secret_info("anthropic_api_key")["set"])
        st = self.anon.get("/api/state").json()
        self.assertEqual((st["setup_required"], st["setup_step"]), (True, "account"))
        login = self.anon.post("/api/login", json={"username": "owner", "password": PASSWORD,
                                                    "code": security.totp_now(self.secret)})
        self.assertEqual(login.status_code, 401)
        fresh = TestClient(self.app, base_url=BASE, headers=CSRF)
        r = fresh.post("/api/setup", json={"username": "neu", "password": PASSWORD})
        self.assertEqual(r.status_code, 403)  # no code, no setup: the next visitor can't take the keys
        code = text.split("Einrichtungs-Code: ")[1].split()[0]
        self.assertIn(f"/#/einrichtung?token={code}", text)
        r = fresh.post("/api/setup", json={"username": "neu", "password": PASSWORD, "token": code})
        self.assertEqual(r.status_code, 200)

    def test_reset_login_can_also_wipe_the_keys_and_youtube(self):
        from automate import __main__ as cli
        self.client()
        store.set_secret("anthropic_api_key", "sk-ant-api03-kept-key-0123456789")
        aid = store.add_account("youtube", "Kanal", connected=True, credentials={"refresh_token": "1//x"})
        with mock.patch("sys.stdout"), mock.patch("automate.youtube.revoke", return_value=True) as revoke:
            self.assertEqual(cli.reset_login(["--yes", "--wipe-keys"]), 0)
        self.assertFalse(store.secret_info("anthropic_api_key")["set"])
        a = store.get_account(aid, with_credentials=True)
        self.assertEqual((a["connected"], a["credentials"]), (False, None))
        revoke.assert_called_once()

    def test_env_keys_are_imported_only_once_the_login_is_set_up(self):
        env = self.root / ".env"
        env.write_text("ANTHROPIC_API_KEY=sk-ant-api03-from-dotenv-0123456789\n")
        with mock.patch.object(paths, "ROOT", self.root):
            importer.run()  # first start: nobody has proven to own this install yet
            self.assertFalse(store.secret_info("anthropic_api_key")["set"])
            secret = self.anon.post("/api/setup", json={"username": "owner", "password": PASSWORD,
                                                        "token": self.token}).json()["totp_secret"]
            self.assertFalse(store.secret_info("anthropic_api_key")["set"])
            r = self.anon.post("/api/setup/confirm", json={"code": security.totp_now(secret), "token": self.token})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(store.get_secret("anthropic_api_key"), "sk-ant-api03-from-dotenv-0123456789")

    def test_login_second_factor_oddities_are_counted_failures_not_errors(self):
        self.make_user()
        anon = TestClient(self.app, base_url=BASE, headers=CSRF, raise_server_exceptions=False)
        for pw in ("wrong password!!", PASSWORD):  # Arabic-Indic digits: same answer whether the password is right
            r = anon.post("/api/login", json={"username": "owner", "password": pw, "code": "١٢٣٤٥٦"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "invalid_credentials"))
        with mock.patch.object(security, "verify_totp", side_effect=TypeError("boom")):
            r = anon.post("/api/login", json={"username": "owner", "password": PASSWORD, "code": "123456"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(store.recent_failures("login.user:owner", time.time() - 60), 3)
        self.assertIsNone(security.verify_totp(self.secret, "١٢٣٤٥٦"))


# ---------------------------------------------------------------- security layer

class SecurityTests(ApiCase):
    def test_every_route_but_the_public_ones_needs_a_session(self):
        routes = server.route_table(self.app)
        self.assertGreater(len(routes), 60)
        public = {(m, p) for m, p in routes if (m, p) in PUBLIC_ROUTES}
        self.assertEqual(public, set(PUBLIC_ROUTES))
        checked = 0
        for method, path in routes:
            url = path
            for name in ("{job_id}", "{pick_id}", "{video_id}", "{variant_id}", "{post_id}", "{creator_id}",
                         "{stream_id}", "{account_id}", "{alert_id}"):
                url = url.replace(name, "1")
            url = url.replace("{name}", "gta6").replace("{track_id}", "epic1").replace("{rest:path}", "x/y")
            r = self.anon.request(method, url, json={})
            if (method, path) in PUBLIC_ROUTES:
                self.assertNotEqual(r.status_code, 401, f"{method} {path} is public")
            else:
                self.assertEqual(r.status_code, 401, f"{method} {path} answered {r.status_code} without a session")
                self.assertEqual(r.json()["error"]["code"], "unauthorized")
            checked += 1
        self.assertEqual(checked, len(routes))

    def test_mutations_need_the_csrf_header(self):
        raw = TestClient(self.app, base_url=BASE)
        r = raw.post("/api/login", json={"username": "a", "password": "b", "code": "c"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "csrf"))
        c = self.client(csrf=False)
        self.assertEqual(c.post("/api/automation", json={"enabled": True}).status_code, 403)
        self.assertEqual(c.get("/api/overview").status_code, 200)  # reads don't need it
        self.assertFalse(store.get_setting("automation.enabled"))

    def test_host_header_allow_list_blocks_dns_rebinding(self):
        for host in ("evil.example:8765", "127.0.0.1:9999", "localhost.evil.example:8765", "", "a:b:c"):
            r = self.anon.get("/api/state", headers={"Host": host})
            self.assertEqual(r.status_code, 403, host)
            self.assertEqual(r.json()["error"]["code"], "forbidden_host")
        self.assertEqual(self.anon.get("/", headers={"Host": "evil.example:8765"}).status_code, 403)
        for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765", "LOCALHOST:8765"):
            self.assertEqual(self.anon.get("/api/state", headers={"Host": host}).status_code, 200, host)
        cfg = server.ServerConfig(host="0.0.0.0", port=8765, public_url="https://clips.example.de")
        with mock.patch.object(server, "machine_names", return_value={"192.168.1.20", "gamer-pc"}):
            lan = server.ServerConfig(host="0.0.0.0", port=8765)
        self.assertTrue(lan.host_allowed("192.168.1.20:8765") and lan.host_allowed("gamer-pc:8765"))
        self.assertFalse(lan.host_allowed("192.168.1.21:8765"))
        self.assertTrue(cfg.host_allowed("clips.example.de", "https"))
        self.assertFalse(cfg.host_allowed("clips.example.de:8443", "https"))
        self.assertEqual(cfg.redirect_uri(), "https://clips.example.de/api/social/youtube/callback")
        self.assertEqual(server.ServerConfig(port=9000).redirect_uri(),
                         "http://localhost:9000/api/social/youtube/callback")
        self.assertEqual(server.ServerConfig(public_url="http://192.168.1.20:8765").redirect_uri(),
                         "http://localhost:8765/api/social/youtube/callback")  # Google refuses IPs / plain http

    def test_lan_names_come_from_the_machine_not_from_reverse_dns(self):
        with mock.patch("socket.gethostname", return_value="Gamer-PC"), \
                mock.patch("socket.getfqdn", return_value="evil.example"), \
                mock.patch("socket.gethostbyname_ex", return_value=("gamer-pc", [], ["192.168.1.20"])):
            names = server.machine_names()
        self.assertIn("gamer-pc", names)
        self.assertIn("gamer-pc.local", names)
        self.assertIn("192.168.1.20", names)
        self.assertNotIn("evil.example", names)

    def test_serve_prints_a_setup_code_and_trusts_proxy_headers_only_behind_a_public_url(self):
        import uvicorn
        from io import StringIO
        from automate import __main__ as cli
        seen = []

        def config(app, **kw):
            seen.append(kw)
            return mock.MagicMock()
        for argv, proxied in ((["--port", "18999", "--no-browser"], False),
                              (["--port", "18999", "--no-browser", "--public-url", "https://clips.example.de"], True)):
            out = StringIO()
            with mock.patch.object(uvicorn, "Config", side_effect=config), mock.patch.object(uvicorn, "Server"), \
                    mock.patch("sys.stdout", out), mock.patch.object(cli, "_setup_logging",
                                                                     return_value=self.root / "server.log"):
                self.assertEqual(cli.serve(argv), 0)
            self.assertEqual(seen[-1]["proxy_headers"], proxied)
            code = out.getvalue().split("Einrichtungs-Code: ")[1].split()[0]
            self.assertTrue(auth._setup_token_ok(code))  # each start: a fresh code, the old one is void

    def test_cross_origin_and_cross_site_requests_are_refused(self):
        c = self.client()
        for origin in ("http://evil.example", "null", "http://localhost:8765", "http://127.0.0.1:3000"):
            r = c.post("/api/automation", json={"enabled": True}, headers={"Origin": origin})
            self.assertEqual(r.status_code, 403, origin)
        self.assertEqual(c.post("/api/automation", json={"enabled": True}, headers={"Origin": BASE}).status_code, 200)
        r = c.get("/api/videos", headers={"Sec-Fetch-Site": "same-site"})  # another port on this machine
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "cross_site"))
        self.assertEqual(c.get("/api/videos", headers={"Sec-Fetch-Site": "same-origin"}).status_code, 200)
        cb = self.anon.get("/api/social/youtube/callback?state=x&code=y", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cb.status_code, 400)  # Google's redirect is allowed in; the bad state is what fails
        self.assertIn("text/html", cb.headers["content-type"])

    def test_security_headers_and_caching(self):
        page = self.anon.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<script type=\"module\" src=\"js/main.js\">", page.text)
        csp = page.headers["content-security-policy"]
        for part in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "img-src 'self' data:"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["referrer-policy"], "same-origin")
        self.assertEqual(page.headers["cache-control"], "no-cache")
        js = self.anon.get("/js/api.js")
        self.assertIn("javascript", js.headers["content-type"])
        self.assertEqual(js.headers["cache-control"], "no-cache")
        for f in (server.WEB_DIR / "vendor").glob("*.*js"):  # ES modules need a JavaScript type under nosniff
            self.assertIn("javascript", self.anon.get(f"/vendor/{f.name}").headers["content-type"], f.name)
        api = self.anon.get("/api/state")
        self.assertEqual(api.headers["cache-control"], "no-store")
        self.assertEqual(self.anon.get("/../automate/server.py").status_code, 404)
        self.assertEqual(self.anon.get("/%2e%2e/automate/server.py").status_code, 404)

    def test_errors_are_german_json(self):
        c = self.client()
        r = c.get("/api/does/not/exist")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
        r = c.post("/api/jobs", content=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "invalid_json"))
        r = c.post("/api/jobs", json=[1, 2])
        self.assertEqual(r.json()["error"]["message"], "Erwartet ein JSON-Objekt.")
        self.assertEqual(c.get("/api/jobs/abc").status_code, 404)
        r = c.get("/api/jobs?limit=500")
        self.assertEqual(set(r.json()["error"]["fields"]), {"limit"})
        r = c.post("/api/jobs", content=b"x" * (5 * 1024 * 1024), headers={"Content-Type": "application/json"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"))

        def chunked():  # no Content-Length: only the body limit middleware sees the size
            for _ in range(5):
                yield b"x" * (1024 * 1024)
        for method, path in (("POST", "/api/jobs"), ("PUT", "/api/research"), ("POST", "/api/login")):
            r = c.request(method, path, content=chunked(), headers={"Content-Type": "application/json"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"), path)
            self.assertIn("höchstens 4 MB", r.json()["error"]["message"])
        r = c.post("/js/api.js", content=b"x" * (5 * 1024 * 1024))  # not under /api: German JSON all the same
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"))

    def test_no_secret_in_any_get_response_or_log(self):
        c = self.client()
        secrets = {"anthropic": "sk-ant-api03-SECRETanthropicKEY0123456789", "fish": "fishSECRETkey0123456789",
                   "cid": "123456789012-secretclientid.apps.googleusercontent.com", "csecret": "GOCSPX-SECRETclient99"}
        store.set_secret("anthropic_api_key", secrets["anthropic"])
        store.set_secret("fish_api_key", secrets["fish"])
        store.set_secret("youtube_client_id", secrets["cid"])
        store.set_secret("youtube_client_secret", secrets["csecret"])
        creds = {"access_token": "ya29.SECRETaccess", "refresh_token": "1//SECRETrefresh", "expires_at": 1e10,
                 "scope": "x", "token_type": "Bearer"}
        aid = store.add_account("youtube", "Main", connected=True, external_id="UC1", credentials=creds)
        store.set_meta("totp.pending", {"user_id": self.uid, "secret": "PENDINGTOTPSECRETXYZ", "at": time.time()})
        user = store.get_user()
        jid = store.create_job("source", "gta6", clipper_job="job1", source_url="https://www.youtube.com/watch?v=x")
        store.replace_picks(jid, [{"start": 1, "end": 20, "hooks": ["a"], "titles": ["GTA 6 t"]}])
        vid = store.add_video("gta6", self.root / "exports/gta6/2026-11-17/video 1", job_id=jid, title="T")
        store.add_variant(vid, self.root / "exports/gta6/2026-11-17/video 1/a.mp4", hook="a", title="t")
        store.raise_alert("disk.low", "warning", "wenig Platz")
        store.add_usage(jid, "select", "claude-sonnet-5-5", 100, 10)
        values = list(secrets.values()) + [creds["access_token"], creds["refresh_token"], user["password_hash"],
                                           user["totp_secret"], "PENDINGTOTPSECRETXYZ", c.token_hash,
                                           *user["recovery_codes"], "GOCSPX", "ya29."]
        records = []
        logging.getLogger("automate").setLevel(logging.DEBUG)
        handler = logging.Handler()
        handler.emit = lambda rec: records.append(rec.getMessage())
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        ids = {"{job_id}": jid, "{pick_id}": 1, "{video_id}": vid, "{variant_id}": 1, "{account_id}": aid,
               "{name}": "gta6"}
        swept = 0
        for method, path in server.route_table(self.app):
            if method != "GET" or "{rest" in path or path.endswith("/file") or "sheets" in path or "callback" in path:
                continue
            url = path
            for k, v in ids.items():
                url = url.replace(k, str(v))
            r = c.get(url)
            self.assertEqual(r.status_code, 200, f"{url}: {r.text[:200]}")
            for v in values:
                self.assertNotIn(v, r.text, f"{url} leaks a secret")
            swept += 1
        self.assertGreaterEqual(swept, 20)
        keys = c.get("/api/keys").json()["items"]
        self.assertEqual({k["masked"] for k in keys}, {"sk-a…6789", "fish…6789"})
        self.assertTrue(c.get("/api/social/youtube/app").json()["client_id_set"])
        for line in records:
            for v in values:
                self.assertNotIn(v, line)


# ---------------------------------------------------------------- files

class FileTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        folder = self.root / "exports" / "gta6" / "2026-11-17" / "video 1"
        folder.mkdir(parents=True)
        self.data = bytes(range(256)) * 4  # 1024 bytes
        (folder / "hook one.mp4").write_bytes(self.data)
        vid = store.add_video("gta6", folder, title="T")
        self.var = store.add_variant(vid, folder / "hook one.mp4", hook="hook one")
        self.vid, self.folder = vid, folder

    def test_range_requests(self):
        url = f"/api/variants/{self.var}/file"
        r = self.c.get(url)
        self.assertEqual((r.status_code, r.content, r.headers["accept-ranges"]), (200, self.data, "bytes"))
        self.assertEqual((r.headers["content-type"], r.headers["content-length"]), ("video/mp4", "1024"))
        for rng, start, end in (("bytes=0-99", 0, 99), ("bytes=500-599", 500, 599), ("bytes=-100", 924, 1023),
                                ("bytes=1000-", 1000, 1023), ("bytes=1000-5000", 1000, 1023), ("bytes=-5000", 0, 1023)):
            r = self.c.get(url, headers={"Range": rng})
            self.assertEqual(r.status_code, 206, rng)
            self.assertEqual(r.headers["content-range"], f"bytes {start}-{end}/1024")
            self.assertEqual(r.content, self.data[start:end + 1])
            self.assertEqual(r.headers["content-length"], str(end - start + 1))
        for rng in ("bytes=1024-", "bytes=2000-3000", "bytes=50-10", "bytes=-0", "bytes=abc", "bytes=-"):
            r = self.c.get(url, headers={"Range": rng})
            self.assertEqual(r.status_code, 416, rng)
            self.assertEqual(r.headers["content-range"], "bytes */1024")
        r = self.c.get(url, headers={"Range": "bytes=0-1,5-6"})  # several ranges: the whole file
        self.assertEqual((r.status_code, len(r.content)), (200, 1024))
        r = self.c.get(url, headers={"Range": "bytes=0-9", "If-Range": '"stale"'})
        self.assertEqual(r.status_code, 200)
        etag = self.c.get(url).headers["etag"]
        self.assertEqual(self.c.get(url, headers={"Range": "bytes=0-9", "If-Range": etag}).status_code, 206)
        self.assertEqual(self.c.get(url, headers={"If-None-Match": etag}).status_code, 304)
        r = self.c.get(url + "?download=1")
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("hook%20one.mp4", r.headers["content-disposition"])

    def test_only_db_known_files_inside_exports(self):
        outside = self.root / "secret.mp4"
        outside.write_bytes(b"secret")
        bad = store.add_variant(self.vid, outside, hook="x")
        self.assertEqual(self.c.get(f"/api/variants/{bad}/file").status_code, 404)
        dotdot = store.add_variant(self.vid, self.folder / ".." / ".." / ".." / ".." / "secret.mp4", hook="y")
        self.assertEqual(self.c.get(f"/api/variants/{dotdot}/file").status_code, 404)
        link = self.folder / "link.mp4"
        link.symlink_to(outside)
        sym = store.add_variant(self.vid, link, hook="z")
        self.assertEqual(self.c.get(f"/api/variants/{sym}/file").status_code, 404)
        inner = self.folder / "inner.mp4"
        inner.symlink_to(self.folder / "hook one.mp4")
        ok = store.add_variant(self.vid, inner, hook="w")
        self.assertEqual(self.c.get(f"/api/variants/{ok}/file").status_code, 200)
        txt = self.folder / "notes.txt"
        txt.write_text("x")
        other = store.add_variant(self.vid, txt, hook="v")
        self.assertEqual(self.c.get(f"/api/variants/{other}/file").status_code, 404)
        self.assertEqual(self.c.get("/api/variants/999/file").status_code, 404)

    def test_contact_sheets(self):
        review = self.root / "jobs" / "job1" / "review"
        review.mkdir(parents=True)
        (review / "cand_01.jpg").write_bytes(b"\xff\xd8jpeg")
        (self.root / "jobs" / "job1" / "job.json").write_text("{}")
        (self.root / "secret.jpg").write_bytes(b"secret")
        (review / "evil.jpg").symlink_to(self.root / "secret.jpg")
        jid = store.create_job("source", "gta6", clipper_job="job1")
        r = self.c.get(f"/api/jobs/{jid}/sheets/cand_01.jpg")
        self.assertEqual((r.status_code, r.headers["content-type"], r.content), (200, "image/jpeg", b"\xff\xd8jpeg"))
        for name in ("evil.jpg", "..%2Fjob.json", "%2E%2E%2F%2E%2E%2Fsecret.jpg", "job.json", "missing.jpg", ".jpg"):
            self.assertEqual(self.c.get(f"/api/jobs/{jid}/sheets/{name}").status_code, 404, name)
        bad = store.create_job("source", "gta6", clipper_job="../job1")
        self.assertEqual(self.c.get(f"/api/jobs/{bad}/sheets/cand_01.jpg").status_code, 404)
        self.assertEqual(self.c.get("/api/jobs/999/sheets/cand_01.jpg").status_code, 404)


# ---------------------------------------------------------------- endpoints

class OverviewJobTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.pipeline, self.health = self.fakes["automate.pipeline"], self.fakes["automate.health"]

    def test_overview_is_complete_and_cheap(self):
        sid = store.add_stream("gta6", "xqc-twitch", "https://www.twitch.tv/xqc")
        store.add_stream("gta-rp", "other-kick", "https://kick.com/other")  # disabled profile: not counted
        store.add_creator("gta6", "Kai", "https://www.youtube.com/@Kai/videos")
        store.add_creator("gta6", "Old", "https://www.youtube.com/@Old/videos", paused=True)
        self.health.live_status.return_value = {"streams": [{"id": sid, "live": True, "on_topic": True,
                                                             "recording": True, "title": "GTA 6 RP",
                                                             "checked_at": 5.0}], "recorders": 1}
        store.create_job("source", "gta6")
        store.create_job("source", "gta6", status="waiting_approval", stage="approval")  # waits for the owner
        store.create_job("source", "gta6", status="waiting_approval", stage="render")  # waits for its renders only
        done = store.create_job("source", "gta6")
        store.update_job(done, status="done")
        failed = store.create_job("render", "gta6")
        store.update_job(failed, status="failed", error="kaputt")
        store.raise_alert("disk.low", "warning", "Wenig Platz")
        store.set_secret("anthropic_api_key", "sk-ant-api03-abcdefghijklmnop")
        store.add_account("youtube", "Main", connected=True, credentials={"access_token": "a"})
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess in /api/overview")):
            d = self.c.get("/api/overview").json()
        self.assertEqual(d["jobs"], {"waiting": 3, "running": 0, "failed": 1, "waiting_approval": 1, "done_today": 1})
        self.assertEqual(d["live"]["recorders"], 1)
        self.assertEqual(d["live"]["streams"][0]["recording"], True)
        self.assertEqual(len(d["live"]["streams"]), 1)
        self.assertEqual(d["ready"], {"anthropic_key": True, "fish_key": False, "youtube_connected": True,
                                      "tools_ok": True, "sources": 2})
        self.assertEqual(d["automation"]["next_scan_at"], 1900000000.0)
        self.assertEqual([a["key"] for a in d["alerts"]], ["disk.low"])
        self.assertEqual(len(d["recent_jobs"]), 5)
        self.assertEqual(set(d["costs"]), {"today_eur", "limit_eur", "tokens_today"})
        self.health.system_report.assert_not_called()
        system = self.c.get("/api/system").json()  # computes the report and caches the tool state
        self.assertEqual(system["tools"][1]["name"], "demucs")
        self.assertIn("database", system["paths"])
        d = self.c.get("/api/overview").json()
        self.assertEqual((d["system"]["tools_ok"], d["system"]["missing"]), (False, ["demucs"]))

    def test_automation_switches_and_alerts(self):
        r = self.c.post("/api/automation", json={"enabled": True})
        self.assertEqual((r.json()["enabled"], r.json()["live_enabled"]), (True, False))
        self.pipeline.on_settings_changed.assert_called_with(["automation.enabled"])
        self.assertEqual(self.c.post("/api/automation", json={}).status_code, 422)
        self.assertEqual(self.c.post("/api/automation", json={"enabled": "yes"}).status_code, 422)
        a = store.raise_alert("live.stalled", "error", "Recorder hängt")
        store.raise_alert("old", "info", "x")
        store.resolve_alert("old")
        self.assertEqual(self.c.get("/api/alerts").json()["total"], 1)
        self.assertEqual(self.c.get("/api/alerts?active=0").json()["total"], 2)
        r = self.c.post(f"/api/alerts/{a['id']}/ack")
        self.assertTrue(r.json()["acknowledged"])
        self.assertEqual(self.c.post("/api/alerts/999/ack").status_code, 404)

    def test_jobs(self):
        parent = store.create_job("source", "gta6", title="Video", clipper_job="job1")
        child = store.create_job("render", "gta6", parent_id=parent)
        store.add_job_event(parent, "Lade Video …")
        store.update_job(child, status="failed")
        d = self.c.get("/api/jobs").json()
        self.assertEqual((d["total"], d["counts"]), (2, {"waiting": 1, "running": 0, "done": 0, "failed": 1,
                                                         "canceled": 0}))
        self.assertEqual([j["id"] for j in self.c.get("/api/jobs?group=failed").json()["items"]], [child])
        self.assertEqual(set(self.c.get("/api/jobs?group=nope&kind=x").json()["error"]["fields"]), {"group", "kind"})
        detail = self.c.get(f"/api/jobs/{parent}").json()
        self.assertEqual([c["id"] for c in detail["children"]], [child])
        self.assertEqual(detail["events"][0]["message"], "Lade Video …")
        self.assertIn("params", detail)
        up = store.create_job("upload", "gta6", params={"variant_id": 1, "upload_session": "https://upload/x?id=S"})
        self.assertEqual(self.c.get(f"/api/jobs/{up}").json()["params"], {"variant_id": 1})  # a session is a token
        self.assertEqual(self.c.get("/api/jobs/999").status_code, 404)
        r = self.c.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=abc", "section": "0:00:00-1:30:00"})
        self.assertEqual(r.status_code, 200, r.text)
        self.pipeline.enqueue_url.assert_called_with("https://www.youtube.com/watch?v=abc", "gta6", "0:00:00-1:30:00",
                                                     "dashboard")
        self.assertEqual(r.json()["kind"], "source")
        r = self.c.post("/api/jobs", json={"url": "not a url", "section": "soon", "profile": "nope"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"url", "section", "profile"})
        self.assertEqual(self.c.post(f"/api/jobs/{child}/retry").json()["status"], "queued")
        self.assertEqual(self.c.post(f"/api/jobs/{child}/cancel").json()["status"], "canceled")
        self.pipeline.retry_job.side_effect = PipelineError("Nur fehlgeschlagene Jobs lassen sich wiederholen.",
                                                            "not_retryable", 409)
        r = self.c.post(f"/api/jobs/{child}/retry")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], {"code": "not_retryable",
                                             "message": "Nur fehlgeschlagene Jobs lassen sich wiederholen."})

    def test_scan(self):
        r = self.c.post("/api/scan", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIn("gestartet", r.json()["message"])
        self.pipeline.request_scan.assert_called_with(None, "dashboard")
        run = store.start_run("scan", "gta6", "schedule")
        self.assertTrue(self.c.get("/api/overview").json()["automation"]["scan_running"])
        r = self.c.post("/api/scan", json={"profile": "gta6"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "scan_running"))
        store.finish_run(run, True, {"message": "2 neue Videos"})
        last = self.c.get("/api/overview").json()["automation"]["last_scan"]
        self.assertEqual((last["ok"], last["summary"]), (True, {"message": "2 neue Videos"}))
        self.assertEqual(self.c.post("/api/scan", json={"profile": "nope"}).status_code, 422)

    def test_pages_degrade_without_the_pipeline_modules(self):
        with mock.patch.dict(sys.modules, {"automate.pipeline": None, "automate.health": None,
                                           "automate.scheduler": None, "automate.agent": None}):
            r = self.c.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=abc"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "unavailable"))
            self.assertIn("noch nicht verfügbar", r.json()["error"]["message"])
            self.assertEqual(self.c.get("/api/overview").status_code, 200)
            sysrep = self.c.get("/api/system").json()
            self.assertEqual(sysrep["tools"], [])
            self.assertIn("health", sysrep["setup_report"])
            self.assertEqual(self.c.get("/api/research").status_code, 200)
            self.assertEqual(self.c.post("/api/automation", json={"enabled": True}).status_code, 200)


class PickTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.pipeline = self.fakes["automate.pipeline"]
        d = self.root / "jobs" / "job1"
        (d / "review").mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({"url": "https://www.youtube.com/watch?v=abc", "offset": 100}))
        (d / "candidates.json").write_text(json.dumps({"candidates": [
            {"start": 10, "end": 40, "contact_sheet": "review/cand_01.jpg"},
            {"start": 300, "end": 330, "contact_sheet": "review/cand_02.jpg"}]}))
        for n in ("cand_01.jpg", "cand_02.jpg"):
            (d / "review" / n).write_bytes(b"jpg")
        (d / "source.mp4").write_bytes(b"v")
        self.job = store.create_job("source", "gta6", clipper_job="job1", title="Stream",
                                    source_url="https://www.youtube.com/watch?v=abc")
        self.ids = store.replace_picks(self.job, [
            {"start": 12, "end": 30.5, "clip_type": "reaction", "hooks": ["h1", "h2"], "titles": ["GTA 6 a", "b"]},
            {"segments": [{"start": 305, "end": 310}, {"start": 320, "end": 325}], "hooks": ["x"], "titles": ["y"]}])

    def test_list_and_enrichment(self):
        d = self.c.get("/api/picks?status=proposed").json()
        self.assertEqual((d["total"], d["counts"]["proposed"]), (2, 2))
        p = d["items"][0]
        self.assertEqual((p["duration"], p["source_link"]), (18.5, "https://www.youtube.com/watch?v=abc&t=112s"))
        self.assertEqual(p["sheets"], [f"/api/jobs/{self.job}/sheets/cand_01.jpg"])
        self.assertEqual(p["source_expires_at"], 1234.5)
        self.assertEqual(self.pipeline.source_expires_at.call_args[0][0]["clipper_job"], "job1")
        self.assertEqual((p["job"]["title"], p["video_id"]), ("Stream", None))
        second = d["items"][1]
        self.assertEqual((second["duration"], second["sheets"]), (10.0, [f"/api/jobs/{self.job}/sheets/cand_02.jpg"]))
        store.update_pick(self.ids[0], status="approved")
        self.assertEqual(self.c.get("/api/picks?status=approved,rendering").json()["total"], 1)
        self.assertEqual(set(self.c.get("/api/picks?status=bogus").json()["error"]["fields"]), {"status"})
        self.assertEqual(self.c.get(f"/api/picks/{self.ids[1]}").json()["idx"], 2)
        self.assertEqual(self.c.get("/api/picks/999").status_code, 404)

    def test_edit_approve_reject_bulk(self):
        r = self.c.patch(f"/api/picks/{self.ids[0]}", json={"hooks": ["neu"], "voiceover": "", "start": 11})
        self.assertEqual(r.status_code, 200, r.text)
        self.pipeline.update_pick_data.assert_called_with(self.ids[0], {"hooks": ["neu"], "voiceover": None,
                                                                         "start": 11})
        self.assertEqual(r.json()["pick"]["data"]["hooks"], ["neu"])
        self.assertEqual(self.c.patch(f"/api/picks/{self.ids[0]}", json={"clip_type": "weird"}).status_code, 422)
        self.assertEqual(self.c.patch(f"/api/picks/{self.ids[0]}", json={"hooks": [""]}).status_code, 422)
        report = {"hooks": {"neu": {"fails": ["too short"]}}}
        self.pipeline.update_pick_data.side_effect = PickCheckFailed("Der Check hat Fehler gefunden.", report)
        r = self.c.patch(f"/api/picks/{self.ids[0]}", json={"hooks": ["x"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "check_failed")
        self.assertEqual(r.json()["error"]["check_report"], report)
        r = self.c.post(f"/api/picks/{self.ids[0]}/approve")
        self.assertEqual(r.json()["pick"]["status"], "approved")
        self.assertIsInstance(r.json()["job_id"], int)
        self.pipeline.approve_pick.side_effect = PipelineError("Dieser Pick ist nicht mehr offen.", "not_proposed")
        self.assertEqual(self.c.post(f"/api/picks/{self.ids[0]}/approve").status_code, 409)
        r = self.c.post(f"/api/picks/{self.ids[1]}/reject", json={"reason": "schwach"})
        self.assertEqual((r.json()["pick"]["status"], r.json()["pick"]["error"]), ("rejected", "schwach"))
        r = self.c.post("/api/picks/bulk", json={"ids": [self.ids[0], 999], "action": "approve"}).json()
        self.assertEqual([(x["id"], x["ok"]) for x in r["results"]], [(self.ids[0], False), (999, False)])
        self.assertEqual(r["results"][0]["error"]["code"], "not_proposed")
        self.pipeline.approve_pick.side_effect = lambda pid: 77
        r = self.c.post("/api/picks/bulk", json={"ids": [self.ids[1]], "action": "approve"}).json()
        self.assertEqual(r["results"], [{"id": self.ids[1], "ok": True, "job_id": 77}])
        self.assertEqual(self.c.post("/api/picks/bulk", json={"ids": [], "action": "x"}).status_code, 422)


class VideoTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.pipeline = self.fakes["automate.pipeline"]
        folder = self.root / "exports" / "gta6" / "2026-11-17" / "video 2"
        folder.mkdir(parents=True)
        (folder / "a.mp4").write_bytes(b"mp4")
        self.vid = store.add_video("gta6", folder, title="Titel", sources=["https://youtu.be/x"])
        self.var = store.add_variant(self.vid, folder / "a.mp4", hook="Hook *eins*", title="GTA 6 Titel",
                                     hashtags=["#gta6"], predicted=1.4)

    def test_list_get_archive(self):
        d = self.c.get("/api/videos").json()
        self.assertEqual(d["total"], 1)
        v = d["items"][0]
        self.assertEqual((v["day"], v["number"], v["title"]), ("2026-11-17", 2, "Titel"))
        self.assertEqual(v["variants"][0]["file_url"], f"/api/variants/{self.var}/file")
        self.assertNotIn("file", v["variants"][0])
        self.assertEqual(self.c.get("/api/videos?posted=1").json()["total"], 0)
        self.assertEqual(self.c.get("/api/videos?q=eins").json()["total"], 1)
        self.assertEqual(self.c.get("/api/videos?day=17.11.").status_code, 422)
        self.assertTrue(self.c.patch(f"/api/videos/{self.vid}", json={"archived": True}).json()["archived"])
        self.assertEqual(self.c.get("/api/videos").json()["total"], 0)
        self.assertEqual(self.c.get("/api/videos?archived=1").json()["total"], 1)
        self.assertEqual(self.c.get("/api/videos/999").status_code, 404)

    def test_posts_upload_dub(self):
        tt = store.add_account("tiktok", "TT")
        r = self.c.post(f"/api/variants/{self.var}/posts", json={"platform": "tiktok", "account_id": tt,
                                                                  "url": "https://tiktok.com/@me/video/1"})
        self.assertEqual((r.status_code, r.json()["status"], r.json()["platform"]), (200, "posted", "tiktok"))
        self.assertEqual(self.c.get("/api/videos?posted=1").json()["total"], 1)
        r = self.c.post(f"/api/variants/{self.var}/posts", json={"platform": "myspace", "url": "ftp://x"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"platform", "url"})
        r = self.c.post(f"/api/variants/{self.var}/posts", json={"platform": "youtube", "account_id": tt})
        self.assertEqual(set(r.json()["error"]["fields"]), {"account_id"})
        post = store.list_posts(self.var)[0]["id"]
        self.assertEqual(self.c.delete(f"/api/posts/{post}").json(), {"ok": True})
        self.assertEqual(self.c.delete(f"/api/posts/{post}").status_code, 404)
        yt = store.add_account("youtube", "YT")
        r = self.c.post(f"/api/variants/{self.var}/upload", json={"account_id": yt, "privacy": "private"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_connected"))
        store.update_account(yt, connected=True, credentials={"access_token": "a", "refresh_token": "r"})
        r = self.c.post(f"/api/variants/{self.var}/upload", json={"account_id": yt, "privacy": "unlisted"})
        self.assertIsInstance(r.json()["job_id"], int)
        self.pipeline.enqueue_upload.assert_called_with(self.var, yt, "unlisted")
        self.assertEqual(self.c.post(f"/api/variants/{self.var}/upload",
                                     json={"account_id": yt, "privacy": "secret"}).status_code, 422)
        r = self.c.post(f"/api/videos/{self.vid}/dub", json={"lang": "es"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "fish_key_missing"))
        store.set_secret("fish_api_key", "fishkey0123456789")
        r = self.c.post(f"/api/videos/{self.vid}/dub", json={"lang": "es"})
        self.assertIsInstance(r.json()["job_id"], int)
        self.pipeline.enqueue_dub.assert_called_with(self.vid, "es")
        self.assertEqual(self.c.post(f"/api/videos/{self.vid}/dub", json={"lang": "spanish!"}).status_code, 422)


class SourceConfigTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.pipeline = self.fakes["automate.pipeline"]

    def overlay(self, profile, name):
        import yaml
        return yaml.safe_load((paths.runtime_profiles_dir() / profile / name).read_text())

    def test_profiles(self):
        items = self.c.get("/api/profiles").json()["items"]
        self.assertEqual({p["name"]: p["enabled"] for p in items}, {"gta6": True, "gta-rp": False})
        self.assertNotIn("overrides", items[0])
        r = self.c.patch("/api/profiles/gta-rp", json={"enabled": True})
        self.assertTrue(r.json()["enabled"])
        self.assertEqual(self.c.patch("/api/profiles/nope", json={"enabled": True}).status_code, 404)

    def test_creators_crud_is_reflected_in_the_overlay(self):
        r = self.c.post("/api/creators", json={"profile": "gta6", "ref": "@KaiCenat"})
        self.assertEqual(r.status_code, 200, r.text)
        c = r.json()
        self.assertEqual((c["name"], c["url"], c["paused"]), ("Kaicenat", "https://www.youtube.com/@KaiCenat/videos",
                                                              False))
        self.assertEqual(self.overlay("gta6", "creators.yaml")["creators"],
                         [{"name": "Kaicenat", "url": "https://www.youtube.com/@KaiCenat/videos"}])
        self.pipeline.on_settings_changed.assert_called_with(["creators"])
        r = self.c.post("/api/creators", json={"profile": "gta6", "ref": "@KaiCenat"})
        self.assertEqual(r.status_code, 409)
        r = self.c.post("/api/creators", json={"profile": "gta6", "ref": "@nobody"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"ref"})
        r = self.c.post("/api/creators", json={"profile": "nope", "ref": "not a handle!"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"profile", "ref"})
        r = self.c.patch(f"/api/creators/{c['id']}", json={"paused": True, "priority": 5, "match": "keywords"})
        self.assertEqual((r.json()["paused"], r.json()["priority"], r.json()["match"]), (True, 5, "keywords"))
        self.assertEqual(self.overlay("gta6", "creators.yaml")["creators"], [])
        self.assertEqual(self.c.patch(f"/api/creators/{c['id']}", json={"match": "all"}).status_code, 422)
        self.assertEqual(self.c.delete(f"/api/creators/{c['id']}").json(), {"ok": True})
        self.assertEqual(self.c.delete(f"/api/creators/{c['id']}").status_code, 404)
        self.assertEqual(self.c.get("/api/creators?profile=gta6").json()["total"], 0)

    def test_streams_crud_with_live_status(self):
        r = self.c.post("/api/streams", json={"profile": "gta6", "ref": "twitch:xqc", "topic_only": False})
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertEqual((s["name"], s["platform"], s["topic_only"], s["live"]), ("xqc-twitch", "twitch", False, None))
        self.assertEqual(self.overlay("gta6", "live.yaml")["streams"][0]["name"], "xqc-twitch")
        self.fakes["automate.health"].live_status.return_value = {
            "streams": [{"id": s["id"], "live": True, "on_topic": False, "recording": False, "title": "Just chatting",
                         "checked_at": 1.0}], "recorders": 0}
        listed = self.c.get("/api/streams?profile=gta6").json()["items"][0]
        self.assertEqual((listed["live"], listed["title"]), (True, "Just chatting"))
        r = self.c.post("/api/streams", json={"profile": "gta6", "ref": "myspace:xqc"})
        self.assertEqual(r.status_code, 422)
        r = self.c.post("/api/streams", json={"profile": "gta6", "ref": "https://example.com/live"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("Konnte die Quelle nicht prüfen", r.json()["error"]["fields"]["ref"])
        r = self.c.patch(f"/api/streams/{s['id']}", json={"paused": True})
        self.assertTrue(r.json()["paused"])
        self.assertEqual(self.overlay("gta6", "live.yaml")["streams"], [])
        self.assertEqual(self.c.delete(f"/api/streams/{s['id']}").json(), {"ok": True})

    def test_profile_config_and_watchlist(self):
        doc = self.c.get("/api/profiles/gta6/config").json()
        self.assertEqual(set(doc), {"base", "overrides", "effective"})
        r = self.c.put("/api/profiles/gta6/config", json={"overrides": {"topic_patterns": ["gta", "ok", "(bad"]}})
        self.assertEqual(set(r.json()["error"]["fields"]), {"topic_patterns", "topic_patterns.2"})
        r = self.c.put("/api/profiles/gta6/config", json={"overrides": {"hashtags": ["#gta6"], "require_topic": 1}})
        self.assertEqual(set(r.json()["error"]["fields"]), {"require_topic"})
        r = self.c.put("/api/profiles/gta6/config", json={"overrides": {"hashtags": ["#gta6", "#x"]}})
        self.assertEqual(r.json()["effective"]["hashtags"], ["#gta6", "#x"])
        self.assertEqual(self.overlay("gta6", "profile.yaml")["hashtags"], ["#gta6", "#x"])
        self.assertEqual(self.c.get("/api/profiles/nope/config").status_code, 404)
        r = self.c.put("/api/settings/watchlist", json={"overrides": {"weights": {"loudness": "loud"}}})
        self.assertEqual(set(r.json()["error"]["fields"]), {"weights.loudness"})
        r = self.c.put("/api/settings/watchlist", json={"overrides": {"weights": 3, "min_free_gb": -5}})
        self.assertEqual(set(r.json()["error"]["fields"]), {"weights", "min_free_gb"})
        r = self.c.put("/api/settings/watchlist", json={"overrides": {"min_free_gb": 50}})
        self.assertEqual(r.json()["effective"]["min_free_gb"], 50)
        import yaml
        self.assertEqual(yaml.safe_load(paths.runtime_watchlist().read_text())["min_free_gb"], 50)
        self.assertEqual(self.c.put("/api/settings/watchlist", json={"overrides": {}}).json()["overrides"], {})

    def test_settings(self):
        d = self.c.get("/api/settings").json()
        self.assertEqual(d["values"]["approval.mode"], "manual")
        self.assertEqual(d["schema"]["approval.mode"]["type"], "choice")
        r = self.c.patch("/api/settings", json={"prefs.voice": "all", "schedule.scan_times": ["9:00", "18:30"]})
        self.assertEqual(r.json()["values"]["schedule.scan_times"], ["09:00", "18:30"])
        self.pipeline.on_settings_changed.assert_called_with(["prefs.voice", "schedule.scan_times"])
        self.assertEqual(self.overlay("gta6", "prefs.yaml")["voice"], "all")
        r = self.c.patch("/api/settings", json={"prefs.voice": "loud", "claude.daily_limit_eur": -1, "x.y": 1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.json()["error"]["fields"]), {"prefs.voice", "claude.daily_limit_eur", "x.y"})
        self.assertEqual(store.get_setting("prefs.voice"), "all")  # nothing saved
        self.assertEqual(self.c.patch("/api/settings", json={}).status_code, 422)

    def test_research_and_assets(self):
        d = self.c.get("/api/research").json()
        self.assertGreater(len(d["playbook"]), 100)
        self.assertEqual((d["override"], d["system_prompt_chars"]), (None, 12345))
        r = self.c.put("/api/research", json={"override": "Mein Playbook", "extra_instructions": "Mehr Comedy"})
        self.assertEqual((r.json()["override"], r.json()["extra_instructions"]), ("Mein Playbook", "Mehr Comedy"))
        self.assertEqual(self.c.put("/api/research", json={"override": None}).json()["override"], None)
        a = self.c.get("/api/assets").json()
        self.assertTrue(any(f["name"] == "Bebas Neue" and f["license"] for f in a["fonts"]))
        self.assertEqual(a["music"][0]["id"], "epic1")
        r = self.c.patch("/api/assets/music/epic1", json={"approved": True})
        self.assertTrue(r.json()["approved"])
        self.assertEqual(self.c.patch("/api/assets/music/nope", json={"approved": True}).status_code, 404)
        self.assertEqual(self.c.patch("/api/assets/music/epic1", json={"approved": "ja"}).status_code, 422)


class CostKeyTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()

    def test_costs(self):
        store.add_usage(None, "select", "claude-sonnet-5-5", 1_000_000, 100_000)
        store.add_usage(None, "test", "claude-sonnet-5-5", 10, 1)
        d = self.c.get("/api/costs?days=14").json()
        self.assertEqual(d["today"]["requests"], 2)
        self.assertAlmostEqual(d["today"]["usd"], 3.0, places=3)
        self.assertEqual(len(d["days"]), 14)
        self.assertEqual(set(d["days"][0]), {"day", "eur", "usd", "requests", "tokens"})
        self.assertEqual(d["by_purpose"][0]["purpose"], "select")
        self.assertEqual(len(d["recent"]), 2)
        self.assertEqual((d["limit_eur"], d["usd_to_eur"]), (5.0, 0.92))
        self.assertEqual(self.c.get("/api/costs?days=0").status_code, 422)

    def test_keys(self):
        items = self.c.get("/api/keys").json()["items"]
        self.assertEqual([(k["name"], k["set"], k["masked"]) for k in items],
                         [("anthropic", False, None), ("fish", False, None)])
        self.assertEqual(self.c.post("/api/keys/anthropic/test").json()["error"]["code"], "key_missing")
        r = self.c.put("/api/keys/anthropic", json={"value": "not-a-key"})
        self.assertEqual(r.json()["error"]["fields"], {"value": "Anthropic-API-Schlüssel: muss mit „sk-ant-“ beginnen"})
        r = self.c.put("/api/keys/anthropic", json={"value": " sk-ant-api03-abcdefghijklmnopqrstuvwxyz "})
        self.assertEqual((r.json()["set"], r.json()["masked"], r.json()["last_test"]), (True, "sk-a…wxyz", None))
        self.fakes["automate.pipeline"].on_settings_changed.assert_called_with(["secrets.anthropic_api_key"])
        r = self.c.post("/api/keys/anthropic/test")
        self.assertEqual(r.json(), {"ok": True, "message": "Der Schlüssel funktioniert."})
        self.fakes["automate.agent"].test_key.assert_called_with("sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
        self.assertTrue(self.c.get("/api/keys").json()["items"][0]["last_test"]["ok"])
        self.c.put("/api/keys/fish", json={"value": "fishkey0123456789abc"})
        r = self.c.post("/api/keys/fish/test")
        self.assertEqual(r.json()["ok"], False)
        self.bridge_mock.assert_called_with("fish_key_check", {}, "gta6", timeout=90)
        self.assertEqual(self.c.delete("/api/keys/fish").json(), {"ok": True})
        self.assertEqual(self.c.get("/api/keys").json()["items"][1]["set"], False)
        self.assertEqual(self.c.put("/api/keys/openai", json={"value": "x"}).status_code, 404)


class SocialApiTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()

    def test_accounts_crud(self):
        r = self.c.post("/api/social/accounts", json={"platform": "tiktok", "label": "Haupt", "handle": "@me"})
        a = r.json()
        self.assertEqual(set(a), {"id", "platform", "label", "handle", "profile", "connected", "external_id", "notes"})
        self.assertFalse(a["connected"])
        r = self.c.post("/api/social/accounts", json={"platform": "myspace", "label": "", "profile": "nope"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"platform", "label", "profile"})
        r = self.c.patch(f"/api/social/accounts/{a['id']}", json={"label": "Neu", "profile": "gta6", "notes": "x"})
        self.assertEqual((r.json()["label"], r.json()["profile"], r.json()["notes"]), ("Neu", "gta6", "x"))
        self.assertEqual(self.c.patch(f"/api/social/accounts/{a['id']}", json={"label": ""}).status_code, 422)
        self.assertEqual(self.c.get("/api/social/accounts").json()["total"], 1)
        self.assertEqual(self.c.delete(f"/api/social/accounts/{a['id']}").json(), {"ok": True})
        self.assertEqual(self.c.delete(f"/api/social/accounts/{a['id']}").status_code, 404)

    def test_youtube_app(self):
        d = self.c.get("/api/social/youtube/app").json()
        self.assertEqual((d["client_id_set"], d["client_secret_set"], d["client_id_hint"]), (False, False, None))
        self.assertEqual(d["redirect_uri"], "http://localhost:8765/api/social/youtube/callback")
        self.assertIn("localhost", d["redirect_note"])
        r = self.c.post("/api/social/youtube/connect", json={})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "youtube_app_missing"))
        r = self.c.put("/api/social/youtube/app", json={"client_id": "nonsense", "client_secret": "has space"})
        self.assertEqual(set(r.json()["error"]["fields"]), {"client_id", "client_secret"})
        r = self.c.put("/api/social/youtube/app", json={"client_id": "123456789012-abc.apps.googleusercontent.com"})
        self.assertEqual((r.json()["client_id_set"], r.json()["client_secret_set"]), (True, False))
        self.assertEqual(r.json()["client_id_hint"], "123456….apps.googleusercontent.com")
        r = self.c.put("/api/social/youtube/app", json={"client_secret": "GOCSPX-abcdef"})
        self.assertTrue(r.json()["client_secret_set"])
        r = self.c.put("/api/social/youtube/app", json={"client_secret": None})
        self.assertFalse(r.json()["client_secret_set"])
        self.assertEqual(self.c.put("/api/social/youtube/app", json={}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
