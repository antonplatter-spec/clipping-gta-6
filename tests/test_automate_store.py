"""automate foundation: DB schema/migrations, settings registry, store helpers (incl. job claiming under real threads),
costs and the first-run importer. Every test runs on its own temp data dir."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from automate import costs, db, importer, paths, settings, store


class TempData(unittest.TestCase):
    """Points AUTOMATE_DATA_DIR (and the jobs/exports/logs dirs) at a fresh temp dir for each test."""

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
        store.ensure_profile("gta6", True)
        store.ensure_profile("gta-rp", False)


class DbTests(TempData):
    def test_paths_follow_the_env_and_files_are_private(self):
        self.assertEqual(paths.db_path(), (self.root / "data" / "automate.db").resolve())
        self.assertEqual(paths.jobs_dir(), (self.root / "jobs").resolve())
        self.assertEqual(paths.exports_dir(), (self.root / "exports").resolve())
        self.assertEqual(os.stat(paths.db_path()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(paths.data_dir()).st_mode & 0o777, 0o700)
        c = db.conn()
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(db.schema_version(c), db.SCHEMA_VERSION)

    def test_switching_the_data_dir_reopens_the_connection(self):
        store.set_settings({"worker.concurrency": 3})
        with tempfile.TemporaryDirectory() as other, mock.patch.dict(os.environ, {"AUTOMATE_DATA_DIR": other}):
            self.assertEqual(store.get_setting("worker.concurrency"), 1)
        self.assertEqual(store.get_setting("worker.concurrency"), 3)

    def test_a_later_migration_adds_a_column_and_keeps_the_data(self):
        jid = store.create_job("source", "gta6", source_url="https://youtu.be/x")
        db.close()
        steps = db.MIGRATIONS + [(2, ["ALTER TABLE jobs ADD COLUMN note TEXT"])]
        with mock.patch.object(db, "MIGRATIONS", steps), mock.patch.object(db, "SCHEMA_VERSION", 2):
            c = db.conn()
            self.assertEqual(db.schema_version(c), 2)
            self.assertIn("note", [r[1] for r in c.execute("PRAGMA table_info(jobs)")])
            self.assertEqual(store.get_job(jid)["source_url"], "https://youtu.be/x")
            db.close()
            self.assertEqual(db.schema_version(db.conn()), 2)  # applying again is a no-op
            db.close()
        with self.assertRaises(RuntimeError) as e:  # an older build refuses a newer DB instead of corrupting it
            db.conn()
        self.assertIn("neueren Version", str(e.exception))

    def test_transaction_rolls_back_and_nests(self):
        with self.assertRaises(ZeroDivisionError):
            with store.transaction():
                store.create_job("source", "gta6")
                with store.transaction():
                    store.create_job("source", "gta6")
                1 / 0
        self.assertEqual(store.list_jobs()[1], 0)


class SettingsTests(TempData):
    def test_defaults_match_the_spec(self):
        d = settings.defaults()
        keys = ("automation.enabled", "schedule.scan_times", "approval.mode", "prefs.hooks_per_video", "claude.model",
                "claude.daily_limit_eur", "render.top_hooks", "worker.concurrency")
        self.assertEqual([d[k] for k in keys], [False, ["09:00"], "manual", 5, "claude-sonnet-5-5", 5.0, 0, 1])
        self.assertEqual(d["claude.prices"]["claude-opus-5-5"]["cache_read"], 0.20)
        schema = settings.schema()
        self.assertEqual(set(schema), set(settings.REGISTRY))
        for key, entry in schema.items():
            self.assertTrue(entry["label"] and entry["help"] and entry["group"], key)
        self.assertEqual([c["value"] for c in schema["approval.mode"]["choices"]], ["manual", "auto"])
        self.assertEqual((schema["prefs.hooks_per_video"]["type"], schema["prefs.hooks_per_video"]["auto"]),
                         ("integer", True))
        self.assertTrue(schema["watchlist.overrides"]["hidden"] and schema["claude.prices"]["advanced"])
        self.assertEqual({e["type"] for e in schema.values()} - {"boolean", "integer", "number", "string", "text",
                                                                 "choice", "string_list", "time_list", "json"}, set())

    def test_validation_normalizes_and_reports_every_bad_key_in_german(self):
        clean = settings.validate({"schedule.scan_times": ["18:30", "9:00", "18:30"], "prefs.videos_per_run": "4",
                                   "claude.usd_to_eur": "0,9", "research.override": "  ",
                                   "dub.auto_languages": ["es", "pt-BR"]})
        self.assertEqual(clean["schedule.scan_times"], ["09:00", "18:30"])
        self.assertEqual([clean[k] for k in ("prefs.videos_per_run", "claude.usd_to_eur", "research.override")],
                         [4, 0.9, None])
        bad = {"prefs.hooks_per_video": 12, "approval.mode": "sometimes", "schedule.scan_times": ["25:00"],
               "automation.enabled": "yes", "nope": 1, "claude.prices": {"x": {"input": 1}}}
        with self.assertRaises(settings.SettingsError) as e:
            settings.validate(bad)
        errs = e.exception.errors
        self.assertEqual(set(errs), set(bad))
        self.assertIn("zwischen 1 und 8", errs["prefs.hooks_per_video"])
        self.assertIn("ungültige Uhrzeit", errs["schedule.scan_times"])

    def test_overrides_keep_the_shape_of_the_base(self):
        settings.validate({"watchlist.overrides": {"min_free_gb": 12, "weights": {"jump": 0.3}}})
        with self.assertRaises(settings.SettingsError) as e:
            settings.validate({"watchlist.overrides": {"weights": 5}})
        self.assertIn("Gruppe", e.exception.errors["watchlist.overrides"])
        with self.assertRaises(settings.SettingsError):
            store.update_profile("gta6", overrides={"caption_fixes": "Trevor"})  # a mapping in profile.yaml
        self.assertEqual(store.update_profile("gta6", overrides={"hashtags": ["#gta6"]})["overrides"],
                         {"hashtags": ["#gta6"]})

    def test_store_settings_and_secrets(self):
        self.assertEqual(store.get_setting("prefs.voice"), "auto")
        store.set_settings({"prefs.voice": "off", "approval.mode": "auto"})
        self.assertEqual(store.all_settings()["prefs.voice"], "off")
        store.set_settings({"prefs.voice": "all"}, only_unset=True)
        self.assertEqual(store.get_setting("prefs.voice"), "off")
        store.reset_settings(["prefs.voice"])
        self.assertEqual(store.get_setting("prefs.voice"), "auto")
        store.set_settings({"research.override": "# Mein Playbook"})
        store.set_settings({"research.override": ""})  # cleared: null, i.e. the repo's playbook again
        self.assertIsNone(store.get_setting("research.override"))
        self.assertIn("research.override", store.stored_setting_keys())
        with self.assertRaises(KeyError):
            store.get_setting("nope")
        self.assertEqual(store.secret_info("fish_api_key"), {"name": "fish_api_key", "set": False, "masked": "",
                                                             "label": "Fish-Audio-API-Schlüssel", "updated_at": None})
        with self.assertRaises(settings.SettingsError):
            store.set_secret("anthropic_api_key", "not-a-key")
        store.set_secret("anthropic_api_key", "  sk-ant-api03-abcdefghijklmnop  ")
        self.assertEqual(store.get_secret("anthropic_api_key"), "sk-ant-api03-abcdefghijklmnop")
        info = store.secret_info("anthropic_api_key")
        self.assertEqual((info["set"], info["masked"]), (True, "sk-a…mnop"))
        store.set_secret("anthropic_api_key", None)
        self.assertIsNone(store.get_secret("anthropic_api_key"))
        store.set_meta("last_key_test", {"ok": True})
        self.assertEqual(store.get_meta("last_key_test"), {"ok": True})


class SourceTests(TempData):
    def test_creators_and_streams(self):
        a = store.add_creator("gta6", "Kai Cenat", "https://www.youtube.com/@KaiCenat/videos")
        b = store.add_creator("gta6", "xQc", "https://www.youtube.com/@xQcOW/videos", "keywords", priority=10)
        with self.assertRaises(ValueError):
            store.add_creator("gta6", "Kai again", "https://www.youtube.com/@kaicenat/videos")
        with self.assertRaises(ValueError):
            store.add_creator("nope", "Kai", "https://www.youtube.com/@KaiCenat/videos")
        self.assertEqual([c["id"] for c in store.list_creators("gta6")], [b, a])  # priority first
        store.update_creator(b, paused=True)
        self.assertEqual([c["name"] for c in store.list_creators("gta6", include_paused=False)], ["Kai Cenat"])
        self.assertIs(store.get_creator(b)["paused"], True)
        with self.assertRaises(ValueError) as e:
            store.update_creator(b, url="https://www.youtube.com/@KaiCenat/videos")
        self.assertIn("gibt es schon", str(e.exception))
        self.assertTrue(store.delete_creator(a))
        self.assertFalse(store.delete_creator(a))
        s = store.add_stream("gta6", "xqc-twitch", "https://www.twitch.tv/xqc")
        self.assertEqual(store.get_stream(s)["platform"], "twitch")
        self.assertEqual({p["name"]: (p["creators"], p["streams"]) for p in store.list_profiles()},
                         {"gta6": (1, 1), "gta-rp": (0, 0)})
        with self.assertRaises(ValueError):
            store.add_stream("gta6", "other", "https://www.twitch.tv/xqc")
        self.assertIs(store.update_stream(s, topic_only=False)["topic_only"], False)


class JobTests(TempData):
    def test_create_update_list_and_groups(self):
        a = store.create_job("source", "gta6", source_url="u1", params={"section": None})
        b = store.create_job("render", "gta6", parent_id=a, params={"pick_id": 1})
        job = store.get_job(a)
        self.assertEqual((job["status"], job["stage"], job["group"], job["params"]), ("queued", "fetch", "waiting",
                                                                                      {"section": None}))
        with self.assertRaises(ValueError):
            store.create_job("source", "nope")
        with self.assertRaises(ValueError):
            store.update_job(a, status="exploded")
        with self.assertRaises(ValueError):
            store.update_job(a, bogus=1)
        done = store.update_job(b, status="done", result={"videos": [1]})
        self.assertTrue(done["finished_at"] and done["group"] == "done")
        self.assertIsNone(store.update_job(b, status="queued")["finished_at"])
        rows, total = store.list_jobs(group="waiting")
        self.assertEqual(total, 2)
        self.assertEqual(store.list_jobs(parent_id=a)[0][0]["id"], b)
        self.assertEqual(store.list_jobs(kind=["render"], limit=1)[1], 1)
        self.assertEqual(store.job_counts()["queued"], 2)
        self.assertEqual(store.job_group_counts(), {"waiting": 2, "running": 0, "done": 0, "failed": 0, "canceled": 0})
        self.assertEqual(store.transition_job(a, ("running",), status="done"), None)
        self.assertEqual(store.transition_job(a, ("queued",), status="canceled")["status"], "canceled")
        store.update_job(a, clipper_job="dQw4w9WgXcQ")
        self.assertEqual(store.find_job_by_clipper_job("dQw4w9WgXcQ")["id"], a)
        for i in range(5):
            store.add_job_event(a, f"line {i}")
        ev = store.job_events(a, limit=3)
        self.assertEqual([e["message"] for e in ev], ["line 2", "line 3", "line 4"])
        self.assertEqual([e["message"] for e in store.job_events(a, after_id=ev[0]["id"])], ["line 3", "line 4"])

    def test_claim_order_not_before_and_stale_locks(self):
        now = time.time()
        late = store.create_job("source", "gta6", priority=1, not_before=now + 3600)
        low = store.create_job("source", "gta6", priority=200)
        first = store.create_job("source", "gta6", priority=50)
        self.assertEqual(store.claim_next_job("w1", now=now)["id"], first)
        claimed = store.claim_next_job("w2", now=now)
        self.assertEqual((claimed["id"], claimed["status"], claimed["locked_by"]), (low, "running", "w2"))
        self.assertIsNone(store.claim_next_job("w3", now=now))
        self.assertEqual(store.claim_next_job("w3", now=now + 7200)["id"], late)
        self.assertIsNone(store.claim_next_job("w3", kinds=()))
        self.assertTrue(store.heartbeat_job(first, "w1", now=now + 7250))
        self.assertFalse(store.heartbeat_job(first, "someone else"))
        self.assertEqual(sorted(store.release_stale_jobs(60, now=now + 7300)), sorted([low, late]))
        self.assertEqual(store.get_job(low)["status"], "queued")
        self.assertIsNone(store.get_job(low)["locked_by"])
        self.assertEqual(store.get_job(first)["status"], "running")
        self.assertIn("wieder eingereiht", store.job_events(low)[-1]["message"])

    def test_no_job_is_claimed_twice_by_concurrent_workers(self):
        n = 120
        with store.transaction():
            for i in range(n):
                store.create_job("source" if i % 2 else "live_chunk", "gta6", priority=i % 7)
        claimed, errors = [], []
        lock = threading.Lock()

        def worker(name):
            try:
                while True:
                    job = store.claim_next_job(name)
                    if job is None:
                        break
                    store.add_job_event(job["id"], f"claimed by {name}")
                    store.update_job(job["id"], status="done")
                    with lock:
                        claimed.append(job["id"])
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)
            finally:
                db.close()
        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(len(claimed), n)
        self.assertEqual(len(set(claimed)), n)
        self.assertEqual(store.job_counts()["done"], n)
        self.assertTrue(all(len(store.job_events(j)) == 1 for j in claimed[:20]))

    def test_serial_kinds_run_one_at_a_time_even_when_claimed_concurrently(self):
        for _ in range(4):
            store.create_job("render", "gta6")
        store.create_job("source", "gta6")
        barrier, got = threading.Barrier(5), []

        def worker(i):
            barrier.wait()
            got.append(store.claim_next_job(f"w{i}"))
            db.close()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        kinds = sorted(j["kind"] for j in got if j)
        self.assertEqual(kinds, ["render", "source"])
        self.assertIsNone(store.claim_next_job("w9", kinds=["render"]))
        self.assertEqual(store.claim_next_job("w9", kinds=["render"], serial_kinds=())["kind"], "render")


class PickVideoTests(TempData):
    PICK = {"start": 10, "end": 30, "hooks": ["Why did Kai *run*?", "Kai NEVER saw it coming"],
            "titles": ["Why Did Kai Run In GTA 6?", "Kai NEVER Saw It Coming In GTA 6"]}

    def test_replace_picks_keeps_ids_and_refuses_after_approval(self):
        job = store.create_job("source", "gta6", title="Kai plays GTA 6", clipper_job="abc")
        ids = store.replace_picks(job, [self.PICK, {"data": {**self.PICK, "start": 50, "end": 70}, "reason": "laugh",
                                                    "click_trigger": "Kai screams", "check_report": {}}])
        pick = store.get_pick(ids[1])
        self.assertEqual((pick["idx"], pick["reason"], pick["data"]["start"], pick["job"]["title"], pick["profile"]),
                         (2, "laugh", 50, "Kai plays GTA 6", "gta6"))
        again = store.replace_picks(job, [self.PICK])
        self.assertEqual(again, ids[:1])
        self.assertIsNone(store.get_pick(ids[1]))
        rows, total = store.list_picks(status="proposed")
        self.assertEqual((total, rows[0]["job"]["clipper_job"], rows[0]["video_id"]), (1, "abc", None))
        self.assertEqual(store.pick_counts()["proposed"], 1)
        self.assertIsNone(store.transition_pick(ids[0], ("approved",), "rendering"))
        approved = store.transition_pick(ids[0], ("proposed",), "approved")
        self.assertTrue(approved["decided_at"])
        with self.assertRaises(ValueError):
            store.replace_picks(job, [self.PICK])

    def test_videos_variants_posts_and_accounts(self):
        job = store.create_job("render", "gta6")
        folder = paths.exports_dir() / "gta6" / "2026-11-17" / "video 3"
        source = store.create_job("source", "gta6")
        pick = store.replace_picks(source, [{"start": 0, "end": 10, "hooks": ["h"]}])[0]
        vid = store.add_video("gta6", folder, job_id=job, pick_id=pick, title="Why Did Kai Run In GTA 6?",
                              sources=["https://x"])
        self.assertEqual(store.get_pick(pick)["video_id"], vid)
        self.assertEqual(store.add_video("gta6", folder, job_id=job, pick_id=pick, title="t2"), vid)  # same folder
        video = store.get_video(vid)
        self.assertEqual([video[k] for k in ("day", "number", "title", "archived")], ["2026-11-17", 3, "t2", False])
        v1 = store.add_variant(vid, folder / "why did kai run.mp4", rank=0, hook="Why did Kai *run*?",
                               hashtags=["#gta6"])
        self.assertEqual(store.add_variant(vid, folder / "why did kai run.mp4", rank=0, predicted=1.4), v1)
        v2 = store.add_variant(vid, folder / "kai never.mp4", rank=1)
        acc = store.add_account("youtube", "Main channel", handle="@me", credentials={"refresh_token": "secret"})
        self.assertNotIn("credentials", store.list_accounts()[0])
        self.assertTrue(store.list_accounts()[0]["has_credentials"])
        self.assertEqual(store.get_account(acc, with_credentials=True)["credentials"], {"refresh_token": "secret"})
        p = store.add_post(v2, "youtube", account_id=acc, url="https://youtube.com/shorts/x")
        self.assertTrue(store.get_post(p)["posted_at"])
        self.assertEqual(store.list_videos(posted=True)[1], 1)
        self.assertEqual(store.list_videos(posted=False)[1], 0)
        self.assertEqual(store.list_videos(q="kai run")[1], 0)
        self.assertEqual(store.list_videos(q="t2")[1], 1)
        rows, _ = store.list_videos()
        self.assertEqual([v["id"] for v in rows[0]["variants"]], [v1, v2])
        self.assertEqual(rows[0]["variants"][1]["posts"][0]["platform"], "youtube")
        self.assertTrue(store.get_variant(v1)["file"].endswith("video 3/why did kai run.mp4"))
        store.update_video(vid, archived=True)
        self.assertEqual(store.list_videos()[1], 0)
        self.assertEqual(store.list_videos(archived=None)[1], 1)
        self.assertTrue(store.delete_account(acc))
        self.assertIsNone(store.get_post(p)["account_id"])

    def test_videos_come_by_day_then_number_and_picks_in_the_requested_order(self):
        root = paths.exports_dir() / "gta6"
        for day, n in (("2026-11-16", 9), ("2026-11-17", 2), ("2026-11-17", 10), ("2026-11-15", 1)):
            store.add_video("gta6", root / day / f"video {n}")
        store.add_video("gta6", paths.exports_dir() / "gta6-es" / "2026-11-14" / "video 1", lang="es")
        self.assertEqual([(v["day"], v["number"]) for v in store.list_videos()[0]],
                         [("2026-11-17", 10), ("2026-11-17", 2), ("2026-11-16", 9), ("2026-11-15", 1),
                          ("2026-11-14", 1)])  # newest day first, the day's last video first (not insertion order)
        old = store.create_job("source", "gta6")
        new = store.create_job("source", "gta6")
        a, b = store.replace_picks(old, [self.PICK, {**self.PICK, "start": 40}])
        c, = store.replace_picks(new, [self.PICK])
        db.conn().execute("UPDATE picks SET created_at = created_at - 100 WHERE job_id = ?", (old,))
        ids = lambda **kw: [p["id"] for p in store.list_picks(**kw)[0]]  # noqa: E731
        self.assertEqual(ids(), [c, a, b])                       # by job, newest first
        self.assertEqual(ids(order="queue"), [a, b, c])          # the approval queue: oldest first
        self.assertEqual(ids(order="queue", limit=1, offset=1), [b])
        store.transition_pick(c, ("proposed",), "rejected")
        store.update_pick(b, status="approved", decided_at=time.time() + 50)
        self.assertEqual(ids(order="decided"), [b, c, a])        # newest decision first
        with self.assertRaises(ValueError):
            store.list_picks(order="random")


class UsageAlertRunLoginTests(TempData):
    def test_costs(self):
        prices = settings.DEFAULT_PRICES
        self.assertAlmostEqual(costs.cost_usd("claude-sonnet-5-5", {"input_tokens": 1_000_000}, prices), 2.0)
        self.assertAlmostEqual(costs.cost_usd("claude-opus-5-5", {"output_tokens": 1_000_000, "cache_read_input_tokens":
                                                                  1_000_000}, prices), 20.2)
        api_usage = {"input_tokens": 0, "cache_creation_input_tokens": 2_000_000,
                     "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}
        self.assertAlmostEqual(costs.cost_usd("claude-sonnet-5-5", api_usage, prices), 2.5 + 4.0)
        self.assertTrue(costs.estimate("claude-sonnet-5-5-20261101", {"input_tokens": 10}, prices)["known_model"])
        with self.assertLogs("automate.costs", "WARNING"):
            est = costs.estimate("claude-mystery", {"input_tokens": 1_000_000}, prices)
        self.assertEqual((est["usd"], est["known_model"]), (2.0, False))

    def test_refusal_fallback_models_are_costed_at_their_own_rate(self):
        old_table = {"claude-sonnet-5-5": settings.DEFAULT_PRICES["claude-sonnet-5-5"]}  # a DB from before the rows
        for model, usd in (("claude-opus-4-8", 25.0), ("claude-opus-5", 25.0), ("claude-sonnet-5", 10.0),
                           ("claude-opus-5-20261001", 25.0)):
            est = costs.estimate(model, {"output_tokens": 1_000_000}, old_table)
            self.assertEqual((est["usd"], est["known_model"]), (usd, True), model)
        # "claude-opus-5" is no prefix of "claude-opus-5-5": each is costed at its own row
        self.assertEqual(costs.cost_usd("claude-opus-5-5", {"input_tokens": 1_000_000}, settings.DEFAULT_PRICES), 4.0)
        self.assertEqual(costs.cost_usd("claude-opus-5-5-20261101", {"input_tokens": 1_000_000},
                                        settings.DEFAULT_PRICES), 4.0)
        self.assertEqual(set(settings.validate({"claude.prices": settings.DEFAULT_PRICES})["claude.prices"]),
                         set(settings.DEFAULT_PRICES))
        self.assertEqual([c["value"] for c in settings.schema()["claude.model"]["choices"]],
                         ["claude-sonnet-5-5", "claude-opus-5-5"])  # costing rows only, not selectable
        self.assertLessEqual(settings.schema()["claude.daily_limit_eur"]["step"], 0.01)  # 2,75 € must be enterable

    def test_db_retention_keeps_active_jobs_and_the_newest_runs(self):
        now = time.time()
        done = store.create_job("source", "gta6")
        active = store.create_job("source", "gta6")
        for jid in (done, active):
            store.add_job_event(jid, "Eingereiht")
            store.add_job_event(jid, "[analyze] line", "debug")
        store.update_job(done, status="done")
        db.conn().execute("UPDATE job_events SET ts = ?", (now - 20 * 86400,))
        db.conn().execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (now - 20 * 86400, done))
        self.assertEqual(store.prune_job_events(14 * 86400, levels=("debug",)), 1)
        self.assertEqual([e["message"] for e in store.job_events(done)], ["Eingereiht"])  # its story stays
        self.assertEqual(len(store.job_events(active)), 2)  # still active: everything stays
        self.assertEqual(store.prune_job_events(30 * 86400), 0)
        self.assertEqual(store.prune_job_events(10 * 86400), 1)
        self.assertEqual(len(store.job_events(active)), 2)
        ids = {}
        for profile in ("gta6", "gta-rp", None):
            for _ in range(3):
                rid = store.start_run("health" if profile is None else "scan", profile, "schedule")
                store.finish_run(rid, True, {"message": "ok"})
                ids.setdefault(profile, []).append(rid)
        open_run = store.start_run("scan", "gta6", "schedule")
        db.conn().execute("UPDATE runs SET started_at = ?", (now - 400 * 86400,))
        self.assertEqual(store.prune_runs("scan", 180 * 86400), 4)  # two old ones per profile
        self.assertEqual(sorted(r["id"] for r in store.list_runs("scan")),  # the newest (finished) of each stay
                         sorted([ids["gta-rp"][-1], ids["gta6"][-1], open_run]))
        self.assertEqual(store.last_run("scan", "gta6", finished_only=True)["id"], ids["gta6"][-1])
        self.assertEqual(store.prune_runs("health", 3 * 86400), 2)
        self.assertEqual([r["id"] for r in store.list_runs("health")], [ids[None][-1]])
        self.assertEqual(store.last_run("health")["id"], ids[None][-1])

    def test_usage_and_daily_budget(self):
        store.set_settings({"claude.daily_limit_eur": 1.0, "claude.usd_to_eur": 0.5})
        self.assertTrue(costs.budget_ok())
        self.assertEqual(costs.budget_left_eur(), 1.0)
        job = store.create_job("source", "gta6")
        row = store.add_usage(job, "select", "claude-sonnet-5-5", 500_000, 100_000, 0, 200_000, "req_1")
        self.assertAlmostEqual(row["cost_usd"], 1.0 + 1.0 + 0.02)
        self.assertAlmostEqual(row["cost_eur"], 1.01)
        self.assertEqual(row["day"], costs.local_day())
        self.assertFalse(costs.budget_ok())
        self.assertEqual(costs.budget_left_eur(), 0.0)
        store.set_settings({"claude.daily_limit_eur": 0})
        self.assertIsNone(costs.budget_left_eur())
        self.assertTrue(costs.budget_ok())
        store.add_usage(None, "test", "claude-opus-5-5", 10, 1, ts=time.time() - 86400 * 2)
        today = store.usage_today()
        self.assertEqual((today["requests"], today["tokens"]), (1, 800_000))
        days = store.usage_by_day(7)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1]["day"], costs.local_day())
        self.assertEqual(days[-3]["requests"], 1)
        self.assertEqual({p["purpose"] for p in store.usage_by_purpose(7)}, {"select", "test"})
        self.assertEqual(store.recent_usage(1)[0]["request_id"], "req_1")

    def test_alerts_reopen_after_resolve(self):
        a = store.raise_alert("disk.low", "warning", "Nur 12 GB frei", {"free_gb": 12})
        self.assertTrue(a["new"])
        self.assertFalse(store.raise_alert("disk.low", "error", "Nur 8 GB frei")["new"])
        store.raise_alert("key.anthropic_missing", "warning", "Kein Schlüssel")
        self.assertEqual([x["key"] for x in store.list_alerts()], ["disk.low", "key.anthropic_missing"])
        self.assertTrue(store.ack_alert(a["id"]))
        self.assertTrue(store.resolve_alert("disk.low"))
        self.assertFalse(store.resolve_alert("disk.low"))
        self.assertEqual(len(store.list_alerts()), 1)
        again = store.raise_alert("disk.low", "warning", "Nur 10 GB frei")
        self.assertEqual((again["new"], again["acknowledged"], again["resolved_at"]), (True, False, None))

    def test_runs(self):
        r1 = store.start_run("scan", "gta6", "schedule")
        store.finish_run(r1, True, {"new": 2})
        r2 = store.start_run("scan", "gta6", "dashboard")
        self.assertEqual(store.last_run("scan")["id"], r2)
        self.assertEqual(store.last_run("scan", finished_only=True)["summary"], {"new": 2})
        self.assertIsNone(store.last_run("health"))

    def test_user_sessions_and_login_attempts(self):
        self.assertIsNone(store.get_user())
        uid = store.create_user("owner", "scrypt$hash", "BASE32", recovery_codes=["h1", "h2"])
        with self.assertRaises(ValueError):
            store.create_user("owner", "x")
        user = store.update_user(totp_enabled=True, totp_last_counter=42)
        self.assertEqual((user["id"], user["totp_enabled"], user["totp_last_counter"], user["recovery_codes"]),
                         (uid, True, 42, ["h1", "h2"]))
        now = time.time()
        store.create_session("t1", uid, now + 100, "Firefox")
        store.create_session("t2", uid, now - 1)
        self.assertEqual(store.get_session("t1")["user_agent"], "Firefox")
        self.assertIsNone(store.get_session("t2"))
        store.touch_session("t1", now + 1000)
        self.assertEqual(store.get_session("t1")["expires_at"], now + 1000)
        store.create_session("t3", uid, now + 100)
        self.assertEqual(store.delete_user_sessions(uid, keep="t3"), 1)
        self.assertIsNone(store.get_session("t1"))
        for ok in (False, False, True, False):
            store.record_login_attempt("ip:127.0.0.1", ok)
        self.assertEqual(store.recent_failures("ip:127.0.0.1", now - 60), 3)
        self.assertTrue(store.delete_user())  # reset-login: the row and its sessions go
        self.assertIsNone(store.get_user())
        self.assertIsNone(store.get_session("t3"))
        self.assertFalse(store.delete_user())


class ImporterTests(TempData):
    def test_import_is_idempotent_and_never_resurrects_deleted_rows(self):
        env = self.root / ".env"
        env.write_text("FISH_API_KEY=fish-key-1234567890\nCLIPPER_TOP_HOOKS=2\n# ANTHROPIC_API_KEY=sk-ant-x\n")
        self.assertEqual(importer.run(env_file=env)["secrets"], [])  # no login yet: keys wait for the setup
        self.assertIsNone(store.get_secret("fish_api_key"))
        store.create_user("owner", "scrypt$x", "BASE32", totp_enabled=True)
        for c in store.list_creators("gta6"):  # (the first run already took the creators)
            store.delete_creator(c["id"])
        store.set_meta("import.creators.gta6", None)
        first = importer.run(env_file=env)
        repo_creators = importer._yaml(paths.repo_profiles_dir() / "gta6" / "creators.yaml")["creators"]
        self.assertEqual(first["creators"]["gta6"], len(repo_creators))
        self.assertEqual([c["url"] for c in store.list_creators("gta6")], [c["url"] for c in repo_creators])
        self.assertEqual(store.get_setting("prefs.hooks_per_video"), 5)
        self.assertEqual(store.get_secret("fish_api_key"), "fish-key-1234567890")
        self.assertEqual(store.get_setting("render.top_hooks"), 2)
        self.assertEqual({p["name"]: p["enabled"] for p in store.list_profiles()}, {"gta6": True, "gta-rp": False})
        streams = store.list_streams("gta6")
        self.assertTrue(streams and all(s["topic_only"] for s in streams))
        second = importer.run(env_file=env)
        self.assertEqual([second[k] for k in ("creators", "streams", "settings", "secrets")], [{}, {}, [], []])
        for c in store.list_creators("gta6"):
            store.delete_creator(c["id"])
        store.set_settings({"prefs.hooks_per_video": 3})
        importer.run(env_file=env)
        self.assertEqual(store.list_creators("gta6"), [])
        self.assertEqual(store.get_setting("prefs.hooks_per_video"), 3)
        self.assertEqual(importer.run(force=True, env_file=env)["creators"]["gta6"], len(repo_creators))

    def test_new_default_price_rows_reach_a_stored_table_once_without_touching_edited_rows(self):
        importer.run(env_file=self.root / "none")
        self.assertNotIn("claude.prices", store.stored_setting_keys())  # never stored: the defaults apply as they are
        edited = {"claude-sonnet-5-5": {"input": 9.0, "output": 9.0, "cache_read": 9.0, "cache_write": 9.0},
                  "claude-opus-5-5": settings.DEFAULT_PRICES["claude-opus-5-5"]}
        store.set_settings({"claude.prices": edited})
        store.set_meta("prices.defaults", ["claude-opus-5-5", "claude-sonnet-5-5"])  # a DB from the older release
        out = importer.run(env_file=self.root / "none")
        self.assertEqual(out["settings"], ["claude.prices"])
        prices = store.get_setting("claude.prices")
        self.assertEqual(prices["claude-sonnet-5-5"]["input"], 9.0)  # the owner's edit stays
        self.assertEqual(prices["claude-opus-4-8"], settings.DEFAULT_PRICES["claude-opus-4-8"])
        self.assertEqual(set(prices), set(settings.DEFAULT_PRICES))
        del prices["claude-sonnet-5"]  # the owner removes an offered row: it doesn't come back
        store.set_settings({"claude.prices": prices})
        self.assertEqual(importer.run(env_file=self.root / "none")["settings"], [])
        self.assertNotIn("claude-sonnet-5", store.get_setting("claude.prices"))

    def test_a_new_profile_folder_is_imported_on_the_next_start(self):
        repo = self.root / "profiles"
        (repo / "gta6").mkdir(parents=True)
        (repo / "gta6" / "creators.yaml").write_text("creators:\n- {name: A, url: https://www.youtube.com/@a/videos}\n")
        with mock.patch.object(paths, "repo_profiles_dir", return_value=repo):
            importer.run(env_file=self.root / "none")
            (repo / "fortnite").mkdir()
            (repo / "fortnite" / "live.yaml").write_text("streams:\n- {name: n-twitch, url: https://www.twitch.tv/n, "
                                                         "gta_only: false, priority: 5}\n")
            out = importer.run(env_file=self.root / "none")
        self.assertEqual((out["profiles"], out["streams"]), (["fortnite"], {"fortnite": 1}))
        s = store.list_streams("fortnite")[0]
        self.assertEqual((s["topic_only"], s["priority"]), (False, 5))
        self.assertFalse(store.get_profile("fortnite")["enabled"])


if __name__ == "__main__":
    unittest.main()
