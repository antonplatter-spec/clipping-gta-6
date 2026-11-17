"""automate.scheduler: scan slots with an injected clock (a slot missed while the PC slept runs once; clock
changes; no double runs), scans the way `clipper daily` does them (creators first, dedupe, sections, the daily
quota, a failing watch), the live service (chunks queued once, status cached, live done for ended chunks), the daily
cleanup and the threads' start/stop."""
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automate_pipeline_fakes import PipelineCase, write_live_chunk  # noqa: E402

from automate import db, health, paths, pipeline, runtime, scheduler, store, worker  # noqa: E402


def at(hhmm, days=0):
    d = datetime.now().date() + timedelta(days=days)
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m).timestamp()


def clock(ts):
    return mock.patch.object(store, "_now", lambda: ts)


def join_scans():
    for t in list(pipeline._scan_threads.values()):
        t.join(10)


class SlotTests(PipelineCase):
    def setUp(self):
        super().setUp()
        store.set_settings({"schedule.scan_times": ["09:00", "18:00"], "automation.enabled": True})

    def test_due_slot_and_a_missed_slot_after_sleep_runs_once(self):
        self.assertIsNone(scheduler.due_scan("gta6", at("08:00")))
        self.assertEqual(scheduler.due_scan("gta6", at("09:05")), at("09:00"))
        with clock(at("09:05")):
            store.start_run("scan", "gta6", "schedule")
        self.assertIsNone(scheduler.due_scan("gta6", at("17:59")))
        # the PC slept from 10:00 to 20:30: only the latest slot (18:00) is due, and only once
        with clock(at("20:30")):
            started = scheduler.tick(now=at("20:30"))
            join_scans()
            self.assertEqual(len(started), 1)
            self.assertEqual(scheduler.tick(now=at("20:31")), [])
            join_scans()
        run = store.last_run("scan", "gta6")
        self.assertEqual((run["trigger"], run["ok"]), ("catch_up", True))
        self.assertEqual(len([r for r in store.list_runs("scan") if r["profile"] == "gta6"]), 2)

    def test_a_scan_shortly_before_the_slot_counts_and_clock_changes_do_not_double_run(self):
        with clock(at("08:50")):
            store.start_run("scan", "gta6", "dashboard")
        self.assertIsNone(scheduler.due_scan("gta6", at("09:01")))
        with clock(at("09:02")):
            store.start_run("scan", "gta6", "schedule")
        self.assertIsNone(scheduler.due_scan("gta6", at("08:30")))  # clock set back an hour
        self.assertIsNone(scheduler.due_scan("gta6", at("09:30")))

    def test_an_interrupted_scan_does_not_count(self):
        with clock(at("09:01")):
            rid = store.start_run("scan", "gta6", "schedule")
        scheduler._mark_interrupted_runs()
        self.assertTrue(store.list_runs("scan")[0]["summary"]["interrupted"])
        self.assertEqual(scheduler.due_scan("gta6", at("10:00")), at("09:00"))
        self.assertIsNotNone(store.list_runs("scan")[0]["finished_at"])
        self.assertEqual(store.list_runs("scan")[0]["id"], rid)

    def test_next_scan_time(self):
        with clock(at("09:05")):
            store.start_run("scan", "gta6", "schedule")
        self.assertEqual(scheduler.next_scan_time(at("12:00")), at("18:00"))
        self.assertEqual(scheduler.next_scan_time(at("18:30")), at("18:30"))  # due now
        with clock(at("18:31")):
            store.start_run("scan", "gta6", "schedule")
        self.assertEqual(scheduler.next_scan_time(at("19:00")), at("09:00", days=1))
        store.set_settings({"automation.enabled": False})
        self.assertIsNone(scheduler.next_scan_time(at("19:00")))

    def test_settings_are_read_on_every_tick(self):
        store.set_settings({"automation.enabled": False})
        self.assertEqual(scheduler.tick(now=at("09:30")), [])
        store.set_settings({"automation.enabled": True})
        with clock(at("09:30")):
            self.assertEqual(len(scheduler.tick(now=at("09:30"))), 1)
            join_scans()


class ScanTests(PipelineCase):
    def video(self, vid, source="Kai Cenat", duration=600, processed=False):
        return {"id": vid, "title": f"GTA 6 {vid}", "duration": duration, "channel": source, "source": source,
                "url": f"https://www.youtube.com/watch?v={vid}", "processed": processed, "keywords": []}

    def test_scan_queues_creators_first_dedupes_and_cuts_long_videos(self):
        known = pipeline.enqueue_url("https://youtu.be/KKKKKKKKKKK", "gta6")
        self.clip.watch = {"videos": [self.video("SSSSSSSSSSS", "search: GTA 6 reaction"),
                                      self.video("LLLLLLLLLLL", duration=4 * 3600),
                                      self.video("PPPPPPPPPPP", processed=True),
                                      self.video("KKKKKKKKKKK", "xQc"), self.video("XXXXXXXXXXX", "xQc")],
                           "skipped_off_topic": 3, "errors": [{"source": "Gone", "error": "404"}]}
        [rid] = pipeline.request_scan("gta6", wait=True)
        self.assertEqual(self.clip.calls[:2], [["cleanup"], ["watch", "--per-channel", "6"]])
        jobs = sorted(store.list_jobs(kind="source", run_id=rid)[0], key=lambda j: j["id"])
        self.assertEqual([j["clipper_job"] for j in jobs], ["LLLLLLLLLLL", "XXXXXXXXXXX"])  # creators first, 2/day
        self.assertEqual(jobs[0]["section"], "0:00-1:30:00")
        self.assertIsNone(jobs[1]["section"])
        run = store.last_run("scan", "gta6")
        self.assertTrue(run["ok"])
        msg = run["summary"]["message"]
        for part in ("2 neue Videos eingereiht", "5 geprüft", "3 nicht zu GTA 6", "1 schon bekannt",
                     "1 Quelle(n) nicht erreichbar"):
            self.assertIn(part, msg)
        self.assertNotIn(known, [j["id"] for j in jobs])
        [rid2] = pipeline.request_scan("gta6", wait=True)
        self.assertEqual(store.list_jobs(kind="source", run_id=rid2)[1], 1)  # the search hit, the rest known
        [rid3] = pipeline.request_scan("gta6", wait=True)
        self.assertEqual(store.list_jobs(kind="source", run_id=rid3)[1], 0)
        self.assertIn("Keine neuen Videos", store.last_run("scan", "gta6")["summary"]["message"])

    def test_scheduled_scans_share_the_daily_quota_and_skip_undownloadable_videos(self):
        self.clip.watch = {"videos": [self.video(c * 11) for c in "ABCD"], "skipped_off_topic": 0, "errors": []}
        pipeline.request_scan("gta6", trigger="schedule", wait=True)
        pipeline.request_scan("gta6", trigger="schedule", wait=True)
        self.assertEqual(store.count_jobs(kind="source"), 2)
        self.assertIn("Tageslimit", store.last_run("scan", "gta6")["summary"]["message"])
        pipeline.request_scan("gta6", trigger="dashboard", wait=True)  # "Jetzt suchen" is one more run
        self.assertEqual(store.count_jobs(kind="source"), 4)
        self.clip.fail["fetch"] = [{"stderr": "ERROR: [youtube] AAAAAAAAAAA: Video unavailable"}]
        self.drive(kinds=["source"])
        self.assertEqual(store.find_job_by_clipper_job("AAAAAAAAAAA")["status"], "skipped")
        self.assertEqual(store.find_job_by_clipper_job("BBBBBBBBBBB")["status"], "waiting_approval")

    def test_a_failing_watch_fails_the_run_with_an_alert_that_the_next_scan_resolves(self):
        self.clip.fail["watch"] = [{"stderr": "FileNotFoundError: [Errno 2] No such file or directory: 'yt-dlp'"}]
        pipeline.request_scan("gta6", wait=True)
        run = store.last_run("scan", "gta6")
        self.assertFalse(run["ok"])
        self.assertIn("yt-dlp ist nicht installiert", run["summary"]["message"])
        self.assertIsNone(store.get_alert("scan.failed.gta6")["resolved_at"])
        pipeline.request_scan("gta6", wait=True)
        self.assertIsNotNone(store.get_alert("scan.failed.gta6")["resolved_at"])

    def test_a_scan_that_reaches_no_source_is_a_failed_run_with_an_alert(self):
        proxy = ("ERROR: [youtube:tab] @KaiCenat/videos: Unable to download API page: ('Unable to connect to proxy', "
                 "OSError('Tunnel connection failed: 403 Forbidden'))")
        self.clip.watch = {"videos": [], "skipped_off_topic": 0,
                           "errors": [{"source": "Kai Cenat", "error": proxy}, {"source": "search: GTA 6", "error": proxy}]}
        pipeline.request_scan("gta6", wait=True)
        run = store.last_run("scan", "gta6")
        self.assertFalse(run["ok"])
        self.assertIn("Keine Quelle erreichbar (2 von 2)", run["summary"]["message"])
        alert = store.get_alert("scan.failed.gta6")
        self.assertIsNone(alert["resolved_at"])
        self.assertIn("Internetverbindung", alert["message"])
        self.assertIn("yt-dlp meldet: ERROR: [youtube:tab] @KaiCenat/videos: Unable to download API page", alert["message"])
        self.assertEqual(pipeline.yt_dlp_detail("ERROR: x: boom; please report this issue on  https://github.com/…"),
                         "ERROR: x: boom")
        # one source answering (even with nothing new) is a working scan: the alert goes
        self.clip.watch = {"videos": [{"id": "AAAAAAAAAAA", "processed": True, "source": "Kai Cenat"}],
                           "skipped_off_topic": 0, "errors": [{"source": "search: GTA 6", "error": proxy}]}
        pipeline.request_scan("gta6", wait=True)
        self.assertTrue(store.last_run("scan", "gta6")["ok"])
        self.assertIsNotNone(store.get_alert("scan.failed.gta6")["resolved_at"])

    def test_two_scans_of_a_profile_never_overlap(self):
        lock = pipeline._scan_locks["gta6"]
        lock.acquire()
        try:
            self.assertTrue(scheduler.scan_running())
            with self.assertRaises(pipeline.PipelineError) as e:
                pipeline.request_scan("gta6")
            self.assertEqual((e.exception.code, e.exception.status), ("scan_running", 409))
            store.set_settings({"automation.enabled": True, "schedule.scan_times": ["00:00"]})
            self.assertEqual(scheduler.tick(), [])
        finally:
            lock.release()
        self.assertFalse(scheduler.scan_running())
        store.update_profile("gta6", enabled=False)
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.request_scan()
        self.assertEqual(e.exception.code, "no_profile")


class LiveServiceTests(PipelineCase):
    def setUp(self):
        super().setUp()
        self.sid = store.add_stream("gta6", "xqc-twitch", "https://www.twitch.tv/xqc")
        store.add_stream("gta6", "kai-youtube", "https://www.youtube.com/@KaiCenat/live")
        store.set_settings({"automation.live_enabled": True})

    def tick_data(self, ready):
        return {"started": ["xqc-twitch"], "ready": ready, "transcribing": ["live-xqc-twitch-x"],
                "cleaned": {"files": 2, "gb": 3.2}, "recorders": 1, "issues": {}, "stalled": [], "restarted": [],
                "streams": [{"name": "xqc-twitch", "url": "https://www.twitch.tv/xqc", "live": True, "on_topic": True,
                             "recording": True, "title": "GTA 6 RP", "priority": 100, "topic_only": True}]}

    def test_live_tick_queues_ready_chunks_once_and_caches_the_status(self):
        cj = "live-xqc-twitch-20261117-150000"
        write_live_chunk(cj)
        self.clip.live_ticks = [self.tick_data([cj]), self.tick_data([cj])]
        scheduler.live_tick()
        scheduler.live_tick()
        self.assertEqual(store.count_jobs(kind="live_chunk"), 1)
        op, payload = self.clip.bridge_calls[0]
        self.assertEqual((op, payload["chunk"], payload["health"]), ("live_tick", 30, True))
        st = health.live_status()
        by_id = {s["id"]: s for s in st["streams"]}
        self.assertEqual((by_id[self.sid]["live"], by_id[self.sid]["recording"]), (True, True))
        self.assertIsNotNone(by_id[self.sid]["checked_at"])
        other = next(s for s in st["streams"] if s["name"] == "kai-youtube")
        self.assertEqual((other["live"], other["checked_at"]), (None, None))
        self.assertEqual(st["recorders"], 1)
        runs = store.list_runs("live_watch")
        self.assertEqual(len(runs), 2)
        self.assertIn("Aufnahme gestartet: xqc-twitch", runs[1]["summary"]["message"])
        self.assertIn("1 neue(s) Stück(e)", runs[1]["summary"]["message"])
        self.drive()
        self.assertEqual(store.list_jobs(kind="live_chunk")[0][0]["status"], "waiting_approval")

    def test_ended_chunks_get_live_done_and_a_failed_tick_is_recorded(self):
        cj = "live-xqc-twitch-20261117-160000"
        write_live_chunk(cj)
        [jid] = pipeline.enqueue_live_chunks("gta6", [cj])
        store.update_job(jid, status="failed", error="x")
        self.clip.live_ticks = [self.tick_data([cj]),
                                runtime.BridgeError("[Errno 2] No such file or directory: 'yt-dlp'")]
        scheduler.live_tick()
        self.assertEqual(self.clip.args_of("live done"), [["live", "done", "--", cj]])
        scheduler.live_tick()
        run = store.list_runs("live_watch")[0]
        self.assertFalse(run["ok"])
        self.assertIn("yt-dlp ist nicht installiert", run["summary"]["message"])
        self.assertIn("yt-dlp", health._cache()["gta6"]["error"])

    def test_streams_that_cannot_be_reached_are_a_failed_run_and_an_alert_not_offline(self):
        store.set_settings({"automation.live_enabled": True})
        proxy = "ERROR: [twitch:stream] xqc: Unable to download JSON metadata: Tunnel connection failed: 403 Forbidden"
        self.clip.live_ticks = [{**self.tick_data([]), "probe_error": proxy}, self.tick_data([])]
        scheduler.live_tick()
        run = store.list_runs("live_watch")[0]
        self.assertFalse(run["ok"])
        self.assertIn("Plattformen nicht erreichbar", run["summary"]["message"])
        health.check_all()
        alert = store.get_alert("live.service")
        self.assertIsNone(alert["resolved_at"])
        self.assertIn("erreicht die Streams nicht", alert["message"])
        self.assertIn("Tunnel connection failed", alert["message"])
        scheduler.live_tick()  # reachable again (offline is no error)
        health.check_all()
        self.assertIsNotNone(store.get_alert("live.service")["resolved_at"])

    def test_profiles_without_active_streams_are_not_watched(self):
        for s in store.list_streams("gta6"):
            store.update_stream(s["id"], paused=True)
        self.assertEqual(scheduler.live_tick(), {})
        self.assertEqual(self.clip.bridge_calls, [])


class HousekeepingTests(PipelineCase):
    def test_cleanup_run_is_recorded_once_a_day(self):
        summary = scheduler.run_cleanup(dry_run=True)
        self.assertEqual(self.clip.args_of("cleanup"), [["cleanup", "--dry-run"]])
        self.assertIn("1 Quelldatei(en) würde löschen (1,5 GB)", summary["message"])
        self.assertTrue(store.last_run("cleanup")["ok"])
        self.assertFalse(scheduler._cleanup_due(time.time()))
        self.assertTrue(scheduler._cleanup_due(time.time() + 25 * 3600))

    def test_daily_cleanup_prunes_old_scratch_files(self):
        old_dir = paths.tmp_dir() / "agent" / "job-7"
        old_dir.mkdir(parents=True)
        (old_dir / "select.json").write_text("{}")
        (paths.tmp_dir() / "render-3.json").write_text("[]")
        fresh = paths.tmp_dir() / "render-4.json"
        fresh.write_text("[]")
        past = time.time() - 6 * 86400
        for f in (old_dir / "select.json", paths.tmp_dir() / "render-3.json", old_dir):
            os.utime(f, (past, past))
        summary = scheduler.run_cleanup()
        self.assertEqual(summary["tmp_files"], 2)
        self.assertIn("2 alte Hilfsdatei(en) entfernt", summary["message"])
        self.assertFalse(old_dir.exists())
        self.assertTrue(fresh.exists())

    def test_daily_cleanup_prunes_old_runs_and_job_output_even_when_clipper_cleanup_fails(self):
        for _ in range(3):
            store.finish_run(store.start_run("health", None, "schedule"), True, {"message": "Alles in Ordnung"})
        job = store.create_job("source", "gta6")
        store.add_job_event(job, "[download] 50%", "debug")
        store.add_job_event(job, "Video geladen")
        store.update_job(job, status="done")
        old = time.time() - 30 * 86400
        db.conn().execute("UPDATE runs SET started_at = ?", (old,))
        db.conn().execute("UPDATE job_events SET ts = ?", (old,))
        db.conn().execute("UPDATE jobs SET finished_at = ?", (old,))
        with mock.patch.object(runtime, "run_clipper", return_value=runtime.ProcResult(1, "", "boom", 0.1)):
            summary = scheduler.run_cleanup()
        self.assertFalse(store.last_run("cleanup", finished_only=True)["ok"])
        self.assertEqual((summary["db_rows"]["runs.health"], summary["db_rows"]["job_events"]), (2, 1))
        self.assertEqual([e["message"] for e in store.job_events(job)], ["Video geladen"])
        self.assertEqual(len(store.list_runs("health")), 1)

    def test_threads_start_and_stop_promptly(self):
        stop = threading.Event()
        with mock.patch.object(worker, "POLL_S", 0.05):
            threads = scheduler.start_scheduler(stop) + worker.start_workers(stop)
            self.assertTrue({t.name for t in threads} >= {"scheduler", "live-service", "health-monitor"})
            for _ in range(500):  # the first tick starts the daily cleanup
                if store.last_run("cleanup", finished_only=True):
                    break
                time.sleep(0.01)
            stop.set()
            for t in threads:
                t.join(6)
                self.assertFalse(t.is_alive(), t.name)
        self.assertTrue(store.last_run("cleanup", finished_only=True)["ok"])
