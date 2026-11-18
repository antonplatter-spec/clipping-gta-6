"""automate.pipeline + automate.worker: whole job lifecycles through the real store, with the clipper subprocesses,
the bridge, Claude, YouTube and Instagram faked (tests/test_automate_pipeline_fakes.py). Covers approval paths,
waiting states, retry/backoff, cancel, restart recovery at every stage, live chunks, dubs, uploads to every platform
(one flow, dispatched on the account's platform) and concurrency."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automate_pipeline_fakes import KEY, PipelineCase, make_pick, write_live_chunk  # noqa: E402

from automate import costs, db, paths, pipeline, store, worker  # noqa: E402
from automate.runtime import ProcResult  # noqa: E402

URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"


class Helpers(PipelineCase):
    def gated(self, url=URL, **kw):
        jid = pipeline.enqueue_url(url, "gta6", **kw)
        self.drive(kinds=["source"])
        return jid

    def rendered_video(self):
        jid = self.gated()
        pick = self.picks(jid)[0]
        pipeline.approve_pick(pick["id"])
        self.drive()
        return jid, pick, store.list_videos(pick_id=pick["id"])[0][0]


class LifecycleTests(Helpers):
    def test_a_video_id_starting_with_a_dash_goes_through_every_stage(self):
        jid = self.gated("https://youtu.be/-k9QxYzAbCd")  # clipper's argparse would take "-k9..." for an option
        self.assertEqual(self.job(jid)["status"], "waiting_approval", self.job(jid)["error"])
        self.assertEqual(self.clip.args_of("fetch"), [["fetch", "--", "https://www.youtube.com/watch?v=-k9QxYzAbCd"]])
        self.assertEqual(self.clip.args_of("analyze"), [["analyze", "--", "-k9QxYzAbCd"]])
        pipeline.approve_pick(self.picks(jid)[0]["id"])
        self.drive()
        self.assertEqual(self.clip.args_of("render")[0][-2:], ["--", "-k9QxYzAbCd"])
        self.assertEqual(store.list_videos()[1], 1)

    def test_url_job_manual_approval_then_render_stores_video_and_variants(self):
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual((job["status"], job["stage"]), ("waiting_approval", "approval"))
        self.assertEqual(job["clipper_job"], "AAAAAAAAAAA")
        self.assertEqual(job["title"], "Probed GTA 6 title")  # probed before the download
        self.assertIn("2 von 2 Clips offen", job["progress"])
        self.assertEqual(self.clip.args_of("fetch"), [["fetch", "--", URL]])  # short video: no section
        picks = self.picks(jid)
        self.assertEqual([p["status"] for p in picks], ["proposed", "proposed"])
        self.assertEqual(picks[0]["click_trigger"], "curiosity gap 0")
        self.assertEqual(picks[0]["simulate_report"]["keep"], 5)
        self.assertEqual(picks[0]["check_report"], {"playbook": ["a note"]})
        on_disk = json.loads((paths.jobs_dir() / "AAAAAAAAAAA" / "picks.json").read_text())
        self.assertEqual(on_disk, [p["data"] for p in picks])
        events = [e["message"] for e in store.job_events(jid)]
        self.assertLess(sum("[download]" in m for m in events), 5, "progress lines are throttled")
        # Claude's status lines are the select stage's story in the dashboard: every one kept, as info
        claude = [(e["message"], e["level"]) for e in store.job_events(jid) if e["message"].startswith(("Claude ",
                                                                                                         "Auswahl"))]
        self.assertEqual([m for m, _ in claude], ["Claude wählt bis zu 2 Momente (empfohlen 2)",
                                                  "Claude liest das Transkript (um 11 s)",
                                                  "Claude sieht sich ein Bild bei 12 s an", "Claude prüft einen Entwurf",
                                                  "Auswahl angenommen: 2 Pick(s) nach 4 Anfragen (0.05 €)"])
        self.assertEqual({lvl for _, lvl in claude}, {"info"})
        self.assertEqual(self.agent.calls[0]["max_picks"], 2)  # candidates.json recommended

        render_id = pipeline.approve_pick(picks[0]["id"])
        self.assertEqual(self.job(render_id)["parent_id"], jid)
        self.drive()
        render = self.job(render_id)
        self.assertEqual(render["status"], "done", render["error"])
        args = self.clip.args_of("render")[0]
        self.assertEqual(args[-2:], ["--", "AAAAAAAAAAA"])
        self.assertEqual(Path(args[args.index("--out") + 1]),
                         paths.exports_dir() / "gta6" / date.today().isoformat())
        self.assertNotIn("--allow-say", args)
        videos = store.list_videos(pick_id=picks[0]["id"])[0]
        self.assertEqual(len(videos), 1)
        v = videos[0]
        self.assertEqual((v["number"], v["job_id"], v["lang"]), (1, render_id, "en"))
        self.assertEqual(len(v["variants"]), 5)
        self.assertEqual(v["variants"][0]["hook"], picks[0]["data"]["hooks"][0])
        self.assertEqual(store.get_pick(picks[0]["id"])["status"], "rendered")
        self.assertIn("1 von 2 Clips offen", self.job(jid)["progress"])

        pipeline.reject_pick(picks[1]["id"], "zu schwach")
        job = self.job(jid)
        self.assertEqual((job["status"], job["stage"]), ("done", "finished"))
        self.assertIn("1 gerendert, 1 abgelehnt", job["progress"])
        self.assertEqual(store.get_pick(picks[1]["id"])["error"], "zu schwach")

    def test_auto_mode_renders_every_pick_at_once(self):
        store.set_settings({"approval.mode": "auto"})
        jid = pipeline.enqueue_url(URL, "gta6")
        self.drive()
        self.assertEqual(self.job(jid)["status"], "done")
        videos = store.list_videos(job_id=None)[0]
        self.assertEqual(sorted(v["number"] for v in videos), [1, 2])
        self.assertEqual({p["status"] for p in self.picks(jid)}, {"rendered"})
        self.assertEqual(len(store.list_jobs(kind="render", parent_id=jid)[0]), 2)

    def test_switching_to_auto_leaves_proposed_picks_in_the_queue(self):
        jid = self.gated()
        store.set_settings({"approval.mode": "auto"})
        pipeline.on_settings_changed(["approval.mode"])
        self.drive()
        self.assertEqual({p["status"] for p in self.picks(jid)}, {"proposed"})
        self.assertEqual(self.job(jid)["status"], "waiting_approval")

    def test_long_video_is_cut_to_daily_max_minutes(self):
        self.clip.duration = 3 * 3600
        jid = self.gated()
        self.assertEqual(self.job(jid)["section"], "0:00-1:30:00")
        self.assertEqual(self.clip.args_of("fetch"), [["fetch", "--section", "0:00-1:30:00", "--", "https://www.youtube.com/watch?v=AAAAAAAAAAA"]])

    def test_no_picks_means_skipped_with_the_reason(self):
        self.agent.script.append(0)
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual(job["status"], "skipped")
        self.assertIn("Nichts lohnt sich", job["error"])
        self.clip.recommended = 0
        jid2 = self.gated("https://youtu.be/BBBBBBBBBBB")
        self.assertEqual(self.job(jid2)["status"], "skipped")
        self.assertIn("empfohlen: 0", self.job(jid2)["error"])
        self.assertEqual(len(self.agent.calls), 1)  # no Claude call for a source the analysis rejects

    def test_gate_fail_goes_back_to_claude_once_then_fails(self):
        self.clip.check_fails = [True, True]
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual(job["status"], "failed")
        self.assertIn("auch nach einer Korrektur", job["error"])
        self.assertEqual(len(self.agent.calls), 2)
        self.assertIn("video 1", self.agent.calls[1]["params"]["check_feedback"])
        self.assertIsNotNone(store.get_alert(f"job.failed.{jid}"))
        self.clip.check_fails = [True]
        jid2 = self.gated("https://youtu.be/BBBBBBBBBBB")
        self.assertEqual(self.job(jid2)["status"], "waiting_approval")

    def test_approve_reject_race_has_one_winner(self):
        jid = self.gated()
        a, b = self.picks(jid)
        pipeline.approve_pick(a["id"])
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.reject_pick(a["id"])
        self.assertEqual(e.exception.status, 409)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.approve_pick(a["id"])
        self.assertEqual(len(store.list_jobs(kind="render")[0]), 1)
        pipeline.reject_pick(b["id"])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.approve_pick(b["id"])

    def test_concurrent_approvals_create_one_render_job(self):
        jid = self.gated()
        pid = self.picks(jid)[0]["id"]
        results, errors = [], []

        def go():
            try:
                results.append(pipeline.approve_pick(pid))
            except pipeline.PipelineError as e:
                errors.append(e.code)
            finally:
                db.close()
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(store.list_jobs(kind="render")[0]), 1)

    def test_edit_pick_rechecks_reranks_and_rewrites_picks_json(self):
        jid = self.gated()
        pick = self.picks(jid)[0]
        hooks = ["Why is Kai *SHAKING*?", "Kai can't stop shaking", "3 reasons Kai froze"]
        row, report = pipeline.update_pick_data(pick["id"], {"hooks": hooks, "titles": ["A GTA 6", "B GTA 6",
                                                                                        "C GTA 6"],
                                                             "voiceover": ""})
        self.assertEqual(row["data"]["hooks"], hooks)
        self.assertEqual(report, {"playbook": ["a note"]})
        self.assertEqual(row["simulate_report"]["ranking"][0]["hook"], hooks[0])
        self.assertEqual(self.clip.bridge_calls[-1][0], "rank")
        on_disk = json.loads((paths.jobs_dir() / "AAAAAAAAAAA" / "picks.json").read_text())
        self.assertEqual(on_disk[0]["hooks"], hooks)
        n_rank = sum(op == "rank" for op, _ in self.clip.bridge_calls)
        pipeline.update_pick_data(pick["id"], {"description": "neu"})
        self.assertEqual(sum(op == "rank" for op, _ in self.clip.bridge_calls), n_rank)  # hooks unchanged

        self.clip.check_fails = [True]
        with self.assertRaises(pipeline.PickCheckFailed) as e:
            pipeline.update_pick_data(pick["id"], {"hooks": ["Wait for it..."]})
        self.assertEqual(e.exception.status, 422)
        self.assertIn("hooks", e.exception.report)
        self.assertEqual(store.get_pick(pick["id"])["data"]["hooks"], hooks)  # nothing saved
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.update_pick_data(pick["id"], {"end": pick["data"]["start"] - 1, "colour": "red"})
        self.assertEqual(set(e.exception.fields), {"end", "colour"})
        pipeline.approve_pick(pick["id"])
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.update_pick_data(pick["id"], {"description": "x"})
        self.assertEqual(e.exception.status, 409)

    def test_approve_refuses_a_pick_with_fails(self):
        jid = self.gated()
        pick = self.picks(jid)[0]
        store.update_pick(pick["id"], check_report={"hooks": {"x": {"fails": ["vague"]}}})
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.approve_pick(pick["id"])
        self.assertEqual(e.exception.status, 422)

    def test_enqueue_url_validates_and_dedupes(self):
        jid = pipeline.enqueue_url("https://youtu.be/AAAAAAAAAAA?t=10", "gta6")
        for url in (URL, "AAAAAAAAAAA", "https://m.youtube.com/watch?v=AAAAAAAAAAA&list=x"):
            with self.assertRaises(pipeline.PipelineError) as e:
                pipeline.enqueue_url(url, "gta6")
            self.assertEqual((e.exception.status, e.exception.code), (409, "already_queued"))
        for bad in ("https://www.twitch.tv/xqc", "hello", "https://example.com/watch?v=AAAAAAAAAAA"):
            with self.assertRaises(pipeline.PipelineError) as e:
                pipeline.enqueue_url(bad, "gta6")
            self.assertEqual(e.exception.status, 422)
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_url("https://youtu.be/CCCCCCCCCCC", "gta6", section="2:00-1:00")
        self.assertEqual(e.exception.fields, {"section": mock.ANY})
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enqueue_url("https://youtu.be/CCCCCCCCCCC", "nope")
        pipeline.cancel_job(jid)
        self.assertNotEqual(pipeline.enqueue_url(URL, "gta6"), jid)  # a canceled one may be queued again

    def test_avoid_openers_and_max_picks_follow_the_run(self):
        store.set_settings({"prefs.max_videos_per_run": 3})
        self.clip.recommended = 2
        rid = store.start_run("scan", "gta6", "schedule")
        a = store.create_job("source", "gta6", clipper_job="AAAAAAAAAAA", source_url=URL, run_id=rid)
        store.create_job("source", "gta6", clipper_job="BBBBBBBBBBB", source_url="https://youtu.be/BBBBBBBBBBB",
                         run_id=rid, params={"duration": 600})
        store.update_job(a, params={"duration": 600})
        self.drive()
        self.assertEqual([c["max_picks"] for c in self.agent.calls], [2, 1])  # 3 per run, 2 already made
        self.assertIn("Why did Kai", self.agent.calls[1]["avoid_openers"])  # first three words, without *
        self.assertEqual(self.agent.calls[0]["avoid_openers"], [])
        store.set_settings({"prefs.videos_per_run": 4})
        rid2 = store.start_run("scan", "gta6", "schedule")
        jobs = [store.create_job("source", "gta6", clipper_job=c * 11, source_url=f"https://youtu.be/{c * 11}",
                                 run_id=rid2, params={"duration": 600}) for c in "CD"]
        self.assertEqual(pipeline.max_picks_for(self.job(jobs[0]), 9), 2)  # 4 shared by two open sources


class WaitingTests(Helpers):
    def test_missing_key_waits_after_fetch_and_analyze_then_resumes(self):
        store.set_secret("anthropic_api_key", None)
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual((job["status"], job["stage"]), ("waiting_key", "select"))
        self.assertTrue((paths.jobs_dir() / "AAAAAAAAAAA" / "candidates.json").exists())
        alert = store.get_alert("key.anthropic_missing")
        self.assertIsNone(alert["resolved_at"])
        self.assertIn("1 Job(s) warten", alert["message"])
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_key")
        store.set_secret("anthropic_api_key", KEY)
        pipeline.on_settings_changed(["anthropic_api_key"])
        self.assertEqual(self.job(jid)["status"], "queued")
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        self.assertIsNotNone(store.get_alert("key.anthropic_missing")["resolved_at"])
        self.assertEqual(len(self.clip.args_of("analyze")), 1)  # nothing redone

    def test_agent_missing_key_and_budget_exceptions_park_the_job(self):
        self.agent.script.append(self.agent.MissingApiKey("kein Schlüssel"))
        jid = self.gated()
        self.assertEqual(self.job(jid)["status"], "waiting_key")
        pipeline.resume_waiting()
        self.agent.script.append(self.agent.BudgetExceeded("Limit"))
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_budget")
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_budget")
        self.assertEqual(pipeline.resume_waiting(), [jid])  # the budget is fine again (new day / higher limit)
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")

    def test_budget_reached_parks_before_asking_claude_and_raises_the_alert(self):
        with mock.patch.object(costs, "budget_ok", return_value=False):
            jid = self.gated()
            self.assertEqual(self.job(jid)["status"], "waiting_budget")
            self.assertEqual(self.agent.calls, [])
            self.assertIsNone(store.get_alert("budget.reached")["resolved_at"])
            self.assertIn("Tageslimit von 5,00 € ist erreicht (0,00 € heute)", store.get_alert("budget.reached")["message"])
            self.assertEqual(pipeline.resume_waiting(), [])
        self.assertEqual(pipeline.resume_waiting(), [jid])
        self.assertIsNotNone(store.get_alert("budget.reached")["resolved_at"])


    def test_a_rejected_key_waits_for_a_new_one_instead_of_looping(self):
        self.agent.script.append(self.agent.InvalidApiKey("Anthropic hat den API-Schlüssel abgelehnt"))
        jid = self.gated()
        self.assertEqual(self.job(jid)["status"], "waiting_key")
        self.assertIn("abgelehnt", store.get_alert("key.anthropic_missing")["message"])
        self.assertEqual(pipeline.resume_waiting(), [])  # the same key would only be rejected again
        store.set_secret("anthropic_api_key", KEY + "new")
        pipeline.on_settings_changed(["anthropic_api_key"])
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        self.assertIsNotNone(store.get_alert("key.anthropic_missing")["resolved_at"])

    def test_no_anthropic_credit_waits_and_retries_hourly(self):
        self.agent.script.append(self.agent.AgentError("Das Anthropic-Konto hat kein Guthaben mehr.", code="billing"))
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual((job["status"], job["params"]["wait"]), ("waiting_budget", "billing"))
        alert = store.get_alert("anthropic.billing")
        self.assertIn("kein Guthaben", alert["message"])
        self.assertIsNone(store.get_alert("budget.reached"))  # not the daily limit
        self.assertEqual(pipeline.resume_waiting(), [])
        with mock.patch.object(time, "time", return_value=time.time() + 3700):
            self.assertEqual(pipeline.resume_waiting(), [jid])
        self.assertNotIn("wait", self.job(jid)["params"])
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        self.assertIsNotNone(store.get_alert("anthropic.billing")["resolved_at"])

    def test_agent_errors_retry_only_when_the_agent_says_so(self):
        self.agent.script += [self.agent.AgentError("429", retryable=True, code="rate_limited"),
                              self.agent.AgentError("Claude lehnt ab", code="refusal")]
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))
        self.assertGreater(job["not_before"], time.time() + 800)  # rate limits back off longer
        self.drive(now=time.time() + 1000)
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("failed", 2))
        self.assertEqual(job["error"], "Claude: Claude lehnt ab")


class FailureTests(Helpers):
    def test_retry_with_backoff_then_failed_with_alert_then_manual_retry(self):
        self.clip.fail["fetch"] = [{"stderr": "ERROR: HTTP Error 503: Service Unavailable"}] * 3
        jid = pipeline.enqueue_url(URL, "gta6")
        self.drive()
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))
        self.assertGreater(job["not_before"], time.time() + 30)
        self.assertIn("Versuch 2 von 3", job["progress"])
        self.drive(now=time.time() + 120)  # past the first backoff (1 min), not the second (5 min)
        self.assertEqual(self.job(jid)["attempts"], 2)
        self.drive(now=time.time() + 4000)
        job = self.job(jid)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Download fehlgeschlagen", job["error"])
        alert = store.get_alert(f"job.failed.{jid}")
        self.assertIn(f"Job #{jid}", alert["message"])
        row = pipeline.retry_job(jid)
        self.assertEqual((row["status"], row["attempts"], row["stage"]), ("queued", 0, "fetch"))
        self.assertIsNotNone(store.get_alert(f"job.failed.{jid}")["resolved_at"])
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.retry_job(jid)

    def test_rate_limit_backs_off_longer(self):
        self.clip.fail["fetch"] = [{"stderr": "ERROR: HTTP Error 429: Too Many Requests"}]
        jid = pipeline.enqueue_url(URL, "gta6")
        self.drive()
        self.assertGreater(self.job(jid)["not_before"], time.time() + 800)

    def test_private_video_fails_a_dashboard_job_and_skips_a_scan_job(self):
        err = {"stderr": "ERROR: [youtube] AAAAAAAAAAA: Private video. Sign in if you've been granted access"}
        self.clip.fail["fetch"] = [err, err]
        jid = pipeline.enqueue_url(URL, "gta6")
        self.drive()
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("failed", 1))
        self.assertIn("privat", job["error"])
        rid = store.start_run("scan", "gta6", "schedule")
        sid = store.create_job("source", "gta6", clipper_job="BBBBBBBBBBB", source_url="https://youtu.be/BBBBBBBBBBB",
                               run_id=rid, params={"duration": 600})
        self.drive()
        self.assertEqual(self.job(sid)["status"], "skipped")
        self.assertIsNone(store.get_alert(f"job.failed.{sid}"))

    def test_missing_ytdlp_fails_at_once_with_the_install_hint(self):
        self.clip.fail["fetch"] = [{"stderr": "FileNotFoundError: [Errno 2] No such file or directory: 'yt-dlp'"}]
        jid = pipeline.enqueue_url(URL, "gta6")
        with mock.patch.object(self.clip, "run_bridge", wraps=self.clip.run_bridge):
            store.update_job(jid, params={"duration": 100})
            self.drive()
        self.assertEqual(self.job(jid)["status"], "failed")
        self.assertIn("yt-dlp ist nicht installiert", self.job(jid)["error"])

    def test_render_exit_3_and_voiceover_without_any_voice(self):
        jid = self.gated()
        pick = self.picks(jid)[0]
        pipeline.update_pick_data(pick["id"], {"voiceover": "He had no idea"})
        rid = pipeline.approve_pick(pick["id"])
        self.drive()
        self.assertIn("--allow-say", self.clip.args_of("render")[0])  # no Fish key: the system voice speaks
        self.assertEqual(self.job(rid)["status"], "done")
        store.set_settings({"render.allow_system_voice": False})
        pick2 = self.picks(jid)[1]
        pipeline.update_pick_data(pick2["id"], {"voiceover": "He had no idea"})
        rid2 = pipeline.approve_pick(pick2["id"])
        self.drive()
        self.assertEqual(self.job(rid2)["status"], "failed")
        self.assertIn("Fish-Audio-Schlüssel", self.job(rid2)["error"])
        self.assertEqual(store.get_pick(pick2["id"])["status"], "failed")
        self.assertEqual(self.job(jid)["status"], "done")  # every pick decided and finished
        store.set_secret("fish_api_key", "fishkey1234567")
        pipeline.retry_job(rid2)
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        self.drive()
        self.assertEqual(store.get_pick(pick2["id"])["status"], "rendered")
        self.assertEqual(self.job(jid)["status"], "done")


class CancelTests(Helpers):
    def run_in_thread(self, kinds=None):
        t = threading.Thread(target=lambda: (worker.process_next("bg-worker", kinds), db.close()))
        t.start()
        return t

    def test_cancel_mid_stage_stops_the_command_and_keeps_the_job_canceled(self):
        self.clip.block["analyze"], self.clip.reached["analyze"] = threading.Event(), threading.Event()
        jid = pipeline.enqueue_url(URL, "gta6")
        t = self.run_in_thread()
        self.assertTrue(self.clip.reached["analyze"].wait(10))
        row = pipeline.cancel_job(jid)
        self.assertEqual(row["status"], "canceled")
        t.join(10)
        self.assertFalse(t.is_alive(), "the worker did not stop")
        job = self.job(jid)
        self.assertEqual((job["status"], job["stage"]), ("canceled", "analyze"))
        with self.assertRaises(pipeline.PipelineError):
            pipeline.cancel_job(jid)
        pipeline.retry_job(jid)
        self.clip.block.clear()
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")

    def test_canceling_a_render_gives_the_pick_back_and_discards_partial_output(self):
        jid = self.gated()
        pick = self.picks(jid)[0]
        out = paths.exports_dir() / "gta6" / date.today().isoformat()
        started = threading.Event()
        orig = self.clip.run_clipper

        def run_clipper(args, profile, **kw):
            if args[0] != "render":
                return orig(args, profile, **kw)
            (out / "video 1").mkdir(parents=True)  # one MP4 written, then the cancel arrives
            (out / "video 1" / "half.mp4").write_bytes(b"x")
            started.set()
            self.assertTrue(kw["cancel"].wait(10))
            return ProcResult(-15, "", "", 0.1, canceled=True)
        rid = pipeline.approve_pick(pick["id"])
        with mock.patch("automate.runtime.run_clipper", run_clipper):
            t = self.run_in_thread(["render"])
            self.assertTrue(started.wait(10))
            pipeline.cancel_job(rid)
            t.join(10)
        self.assertEqual(self.job(rid)["status"], "canceled")
        self.assertEqual(store.get_pick(pick["id"])["status"], "proposed")
        self.assertFalse((out / "video 1").exists(), "the half-made folder was not discarded")
        self.assertEqual(self.job(jid)["stage"], "approval")

    def test_canceling_a_parent_rejects_open_picks_and_cancels_its_renders(self):
        jid = self.gated()
        a, b = self.picks(jid)
        rid = pipeline.approve_pick(a["id"])
        pipeline.cancel_job(jid)
        self.assertEqual(self.job(rid)["status"], "canceled")
        self.assertEqual({p["status"] for p in self.picks(jid)}, {"rejected"})

    def test_shutdown_puts_the_running_job_back_in_the_queue(self):
        self.clip.block["fetch"], self.clip.reached["fetch"] = threading.Event(), threading.Event()
        stop = threading.Event()
        jid = pipeline.enqueue_url(URL, "gta6")
        t = threading.Thread(target=lambda: (worker.process_next("w", None, stop=stop), db.close()))
        t.start()
        self.assertTrue(self.clip.reached["fetch"].wait(10))
        stop.set()
        t.join(10)
        job = self.job(jid)
        self.assertEqual((job["status"], job["stage"], job["attempts"]), ("queued", "fetch", 0))
        self.assertIn("Unterbrochen", job["progress"])


class RestartTests(Helpers):
    """A server killed at any point: the job's lock goes stale, the next start re-queues it at its stage."""

    def crash(self, jid, **fields):
        store.update_job(jid, status="running", locked_by="dead-worker", locked_at=time.time() - 3600, **fields)
        self.assertEqual(store.release_stale_jobs(0), [jid])

    def test_killed_mid_fetch_downloads_again_and_continues(self):
        jid = pipeline.enqueue_url(URL, "gta6")
        jd = paths.jobs_dir() / "AAAAAAAAAAA"
        jd.mkdir(parents=True)
        (jd / "source.mp4.part").write_bytes(b"half")
        self.crash(jid, params={"duration": 600})
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        self.assertEqual(len(self.clip.args_of("fetch")), 1)

    def test_killed_mid_analyze_drops_the_truncated_audio_first(self):
        jid = pipeline.enqueue_url(URL, "gta6")
        self.clip._fetch(["fetch", "--", "https://www.youtube.com/watch?v=AAAAAAAAAAA"], lambda line: None, "gta6")
        jd = paths.jobs_dir() / "AAAAAAAAAAA"
        (jd / "audio.wav").write_bytes(b"trunc")
        (jd / "transcript.json").write_text('{"words": [')  # half written
        self.crash(jid, stage="analyze")
        self.drive()
        self.assertFalse(self.clip.seen_audio, "the truncated audio.wav was reused")
        self.assertEqual(self.clip.args_of("fetch"), [])
        self.assertEqual(self.job(jid)["status"], "waiting_approval")

    def test_killed_between_select_and_picks_does_not_ask_claude_again(self):
        jid = pipeline.enqueue_url(URL, "gta6")
        self.clip._fetch(["fetch", "--", "https://www.youtube.com/watch?v=AAAAAAAAAAA"], lambda line: None, "gta6")
        self.clip._analyze(["analyze", "--", "AAAAAAAAAAA"], lambda line: None, "gta6")
        sel = {"picks": [make_pick(1), make_pick(2)], "reasons": ["a", "b"], "click_triggers": ["x", "y"],
               "no_clip_reason": None, "check_report": {}, "turns": 2, "usage": {}}
        self.crash(jid, stage="gate", result={"selection": sel})
        self.drive()
        self.assertEqual(self.agent.calls, [])
        self.assertEqual([p["reason"] for p in self.picks(jid)], ["a", "b"])
        self.assertEqual(self.job(jid)["status"], "waiting_approval")

    def render_job_with_leftover(self, manifest):
        jid = self.gated()
        pick = self.picks(jid)[0]
        rid = pipeline.approve_pick(pick["id"])
        out = paths.exports_dir() / "gta6" / date.today().isoformat()
        folder = out / "video 1"
        folder.mkdir(parents=True)
        (folder / "first hook.mp4").write_bytes(b"mp4")
        if manifest:
            self.clip._manifest(folder, pick["data"])
        store.transition_pick(pick["id"], ("approved",), "rendering")
        self.crash(rid, params={"pick_id": pick["id"], "out": {"dirs": [str(out)], "before": {str(out): []}}})
        return jid, pick, rid, folder

    def test_killed_mid_render_after_one_mp4_renders_again_with_the_same_number(self):
        jid, pick, rid, folder = self.render_job_with_leftover(manifest=False)
        self.drive()
        self.assertEqual(self.job(rid)["status"], "done")
        self.assertEqual(len(self.clip.args_of("render")), 1)
        video = store.list_videos(pick_id=pick["id"])[0][0]
        self.assertEqual(video["number"], 1)
        self.assertFalse((folder / "first hook.mp4").exists())
        self.assertEqual(len(video["variants"]), 5)

    def test_a_render_left_running_by_a_killed_server_is_stopped_before_its_folder_is_touched(self):
        jid, pick, rid, folder = self.render_job_with_leftover(manifest=False)
        seen = []

        def reap(module, args):  # what the stage asks to stop, and whether the half-written folder still existed
            seen.append((module, list(args[:3]), folder.exists()))
            return [4242] if args[0] == "render" and not seen[1:] else []
        with mock.patch.object(worker, "reap_orphans", reap):
            self.drive()
        self.assertEqual(self.job(rid)["status"], "done")
        self.assertEqual(seen[0], ("clipper", ["render", "--picks", str(paths.tmp_dir() / f"render-{rid}.json")], True))
        events = [e["message"] for e in store.job_events(rid)]
        stopped = next(i for i, m in enumerate(events) if "PID 4242" in m)
        removed = next(i for i, m in enumerate(events) if "Unvollständiges video 1" in m)
        self.assertLess(stopped, removed)  # the leftover process can't write into the folder while it's cleaned up

    def test_killed_after_render_before_the_db_write_adopts_the_folder(self):
        jid, pick, rid, folder = self.render_job_with_leftover(manifest=True)
        self.drive()
        self.assertEqual(self.clip.args_of("render"), [])
        self.assertEqual(self.job(rid)["status"], "done")
        video = store.list_videos(pick_id=pick["id"])[0][0]
        self.assertEqual(video["folder"], paths.rel(folder))
        self.assertEqual(store.get_pick(pick["id"])["status"], "rendered")
        self.drive()
        self.assertEqual(store.list_videos(pick_id=pick["id"])[1], 1)

    def test_a_folder_that_is_not_ours_is_left_alone(self):
        jid, pick, rid, folder = self.render_job_with_leftover(manifest=True)
        entries = json.loads((folder / "upload.json").read_text())
        (folder / "upload.json").write_text(json.dumps([{**e, "hook": "other", "title": "other"} for e in entries]))
        self.drive()
        self.assertTrue(folder.exists())
        self.assertEqual(store.list_videos(pick_id=pick["id"])[0][0]["number"], 2)


class SourceTests(Helpers):
    def test_upload_source_deleted_by_cleanup_is_fetched_again_for_the_render(self):
        self.clip.duration = 3 * 3600
        jid = self.gated()
        (paths.jobs_dir() / "AAAAAAAAAAA" / "source.mp4").unlink()
        self.assertIsNone(pipeline.source_expires_at(self.job(jid)))
        pipeline.approve_pick(self.picks(jid)[0]["id"])
        self.drive()
        self.assertEqual(self.clip.args_of("fetch")[-1], ["fetch", "--section", "0:00-1:30:00", "--", "https://www.youtube.com/watch?v=AAAAAAAAAAA"])
        self.assertEqual(store.list_videos()[1], 1)

    def test_source_expires_at_follows_the_cleanup_rules(self):
        jid = self.gated()
        exp = pipeline.source_expires_at(self.job(jid))
        self.assertAlmostEqual(exp, time.time() + 2 * 86400, delta=60)
        (paths.jobs_dir() / "AAAAAAAAAAA" / "live_done").write_text("x")
        self.assertAlmostEqual(pipeline.source_expires_at(jid), time.time() + 3 * 3600, delta=60)


class LiveChunkTests(Helpers):
    def test_live_chunk_flow_delays_live_done_until_every_pick_is_finished(self):
        cj = "live-xqc-twitch-20261117-120000"
        write_live_chunk(cj)
        [jid] = pipeline.enqueue_live_chunks("gta6", [cj])
        self.assertEqual(pipeline.enqueue_live_chunks("gta6", [cj]), [])  # never twice
        job = self.job(jid)
        self.assertEqual((job["kind"], job["stage"], job["title"]), ("live_chunk", "select", "GTA 6 RP day 1"))
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")
        self.assertEqual(self.clip.args_of("live done"), [])
        exp = pipeline.source_expires_at(self.job(jid))
        self.assertAlmostEqual(exp, time.time() + 2 * 86400, delta=120)  # no 3-hour timer while undecided
        a, b = self.picks(jid)
        pipeline.approve_pick(a["id"])
        pipeline.reject_pick(b["id"])
        self.assertEqual(self.job(jid)["stage"], "render")
        self.assertEqual(self.clip.args_of("live done"), [])
        self.drive()
        self.assertEqual(self.clip.args_of("live done"), [["live", "done", "--", cj]])
        job = self.job(jid)
        self.assertEqual((job["status"], job["stage"]), ("done", "finished"))
        self.assertTrue(job["result"]["live_done"])
        self.assertIn("1 gerendert, 1 abgelehnt", job["progress"])

    def test_live_chunk_without_clips_is_released_at_once(self):
        cj = "live-xqc-twitch-20261117-123000"
        write_live_chunk(cj)
        self.agent.script.append(0)
        [jid] = pipeline.enqueue_live_chunks("gta6", [cj])
        self.drive()
        self.assertEqual(self.job(jid)["status"], "skipped")
        self.assertEqual(self.clip.args_of("live done"), [["live", "done", "--", cj]])

    def test_deleted_live_source_blocks_approval_and_fails_the_render_clearly(self):
        cj = "live-xqc-twitch-20261117-130000"
        jd = write_live_chunk(cj)
        [jid] = pipeline.enqueue_live_chunks("gta6", [cj])
        self.drive()
        a, b = self.picks(jid)
        rid = pipeline.approve_pick(a["id"])
        (jd / "source.mp4").unlink()
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.approve_pick(b["id"])
        self.assertEqual(e.exception.code, "source_gone")
        self.drive()
        render = self.job(rid)
        self.assertEqual(render["status"], "failed")
        self.assertIn("Quellvideo dieses Live-Stücks wurde bereits gelöscht", render["error"])
        self.assertEqual(render["attempts"], 1)  # not retried

    def test_close_live_chunks_releases_failed_and_canceled_chunks(self):
        cj = "live-xqc-twitch-20261117-140000"
        write_live_chunk(cj)
        [jid] = pipeline.enqueue_live_chunks("gta6", [cj])
        pipeline.cancel_job(jid)
        self.assertEqual(pipeline.close_live_chunks("gta6", [cj]), [cj])
        self.assertTrue(self.job(jid)["result"]["live_done"])
        self.assertEqual(pipeline.close_live_chunks("gta6", [cj]), [])


class DubAndUploadTests(Helpers):
    def test_dub_job_translates_and_stores_a_video_in_the_language(self):
        jid, pick, video = self.rendered_video()
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_dub(video["id"], "es")
        self.assertEqual(e.exception.code, "fish_key_missing")
        store.set_secret("fish_api_key", "fishkey1234567")
        did = pipeline.enqueue_dub(video["id"], "es")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enqueue_dub(video["id"], "es")
        self.drive()
        self.assertEqual(self.job(did)["status"], "done", self.job(did)["error"])
        self.assertEqual(self.agent.dub_calls, [(did, 1, "es")])
        self.assertEqual(self.clip.args_of("dub segments"), [["dub", "segments", "--pick", "1", "--", "AAAAAAAAAAA"]])
        dub = store.list_videos(lang="es")[0][0]
        self.assertEqual((dub["pick_id"], dub["job_id"]), (pick["id"], did))
        self.assertTrue(dub["folder"].endswith(f"gta6-es/{date.today().isoformat()}/video 1"))
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enqueue_dub(dub["id"], "ru")

    def test_auto_languages_queue_dubs_after_each_render(self):
        store.set_secret("fish_api_key", "fishkey1234567")
        store.set_settings({"dub.auto_languages": ["es", "ru"]})
        jid, pick, video = self.rendered_video()
        self.assertEqual(sorted(v["lang"] for v in store.list_videos(pick_id=pick["id"])[0]), ["en", "es", "ru"])

    def test_upload_job_success_failure_and_retry(self):
        jid, pick, video = self.rendered_video()
        variant = video["variants"][0]
        acc = store.add_account("youtube", "Main", connected=True)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enqueue_upload(variant["id"], acc, "secret")
        uid = pipeline.enqueue_upload(variant["id"], acc, "unlisted")
        self.assertEqual(store.list_posts(variant["id"])[0]["status"], "uploading")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.enqueue_upload(variant["id"], acc, "unlisted")
        self.drive()
        post = store.list_posts(variant["id"])[0]
        self.assertEqual((post["status"], post["external_id"], post["url"]), ("posted", "yt123", "https://youtu.be/yt123"))
        self.assertEqual(self.job(uid)["status"], "done")

        self.yt.script += [self.yt.YouTubeError("Kontingent erschöpft", retryable=True),
                           self.yt.YouTubeError("Video zu lang für das Konto", retryable=False)]
        uid2 = pipeline.enqueue_upload(video["variants"][1]["id"], acc, "private")
        self.drive()
        self.assertEqual((self.job(uid2)["status"], self.job(uid2)["attempts"]), ("queued", 1))
        self.drive(now=time.time() + 400)
        self.assertEqual(self.job(uid2)["status"], "failed")
        post = store.list_posts(video["variants"][1]["id"])[0]
        self.assertEqual((post["status"], post["error"]), ("failed", "Video zu lang für das Konto"))
        pipeline.retry_job(uid2)
        self.assertEqual(store.list_posts(video["variants"][1]["id"])[0]["status"], "uploading")
        self.drive()
        self.assertEqual(store.list_posts(video["variants"][1]["id"])[0]["status"], "posted")
        # every attempt after the first hands YouTube the session the first one opened (no second upload)
        self.assertEqual(self.yt.sessions[-3:], [None, f"https://upload.example/session-{len(self.yt.calls) - 2}",
                                                 f"https://upload.example/session-{len(self.yt.calls) - 2}"])
        self.assertEqual(self.job(uid2)["params"]["upload_session"], self.yt.sessions[-1])


class MoneyTests(Helpers):
    """Claude is never paid twice for the same work without the owner knowing (reliability findings #4, #5, #9)."""

    def interrupted(self, stop):
        def select(job_id, max_picks, cancel):
            stop.set()  # the server is stopped while Claude works
            cancel.wait(5)
            raise self.agent.AgentCanceled()
        return select

    def test_an_unexpected_error_from_a_claude_call_fails_at_once_instead_of_paying_again(self):
        from automate import agent_tools as T
        self.agent.script = [T.ToolError("check failed: Zeitüberschreitung")] * 3  # was retried as "Interner Fehler"
        with self.assertLogs("automate.pipeline", "ERROR"):
            jid = self.gated()
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"], len(self.agent.calls)), ("failed", 1, 1))
        self.assertIn("Interner Fehler bei der Anfrage an Claude", job["error"])

    def test_an_interrupted_selection_counts_as_an_attempt_and_continues_with_the_same_inputs(self):
        store.set_settings({"prefs.max_videos_per_run": 3})
        stop = threading.Event()
        jid = pipeline.enqueue_url(URL, "gta6")
        for i in range(2):
            self.agent.script.append(self.interrupted(stop))
            stop.clear()
            worker.process_next(f"w{i}", stop=stop)
            job = self.job(jid)
            self.assertEqual((job["status"], job["stage"], job["attempts"]), ("queued", "select", i + 1))
            self.assertIn(f"Versuch {i + 2} von 3", job["progress"])
            store.set_settings({"prefs.max_videos_per_run": 1})  # would change max_picks, and so the first message
        self.assertEqual([c["max_picks"] for c in self.agent.calls], [2, 2])  # pinned: the checkpoint still fits
        self.agent.script.append(self.interrupted(stop))
        stop.clear()
        worker.process_next("w3", stop=stop)
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("failed", 3))
        self.assertIn("3-mal mitten in der Arbeit unterbrochen", job["error"])
        self.assertIsNotNone(store.get_alert(f"job.failed.{jid}"))
        pipeline.retry_job(jid)  # the owner's retry continues the same selection
        self.drive()
        self.assertEqual(self.agent.calls[-1]["max_picks"], 2)
        job = self.job(jid)
        self.assertEqual(job["status"], "waiting_approval")
        self.assertNotIn("select", job["params"])

    def test_a_dead_worker_counts_as_an_attempt_and_the_third_one_fails_the_job(self):
        jid = pipeline.enqueue_url(URL, "gta6")
        for i in range(3):
            self.assertEqual(store.claim_next_job(f"dead-{i}")["id"], jid)  # claimed, then the server dies hard
            self.assertEqual(pipeline.release_stale(0), [jid])
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("failed", 3))
        self.assertIn("3-mal mitten in der Arbeit unterbrochen", job["error"])
        self.assertIsNotNone(store.get_alert(f"job.failed.{jid}"))
        # a render that keeps killing the server: its pick fails and the parent finishes
        jid2 = self.gated("https://youtu.be/BBBBBBBBBBB")
        a, b = self.picks(jid2)
        rid = pipeline.approve_pick(a["id"])
        pipeline.reject_pick(b["id"])
        for i in range(3):
            store.claim_next_job(f"dead-r{i}", ["render"])
            pipeline.release_stale(0)
        self.assertEqual(self.job(rid)["status"], "failed")
        self.assertEqual(store.get_pick(a["id"])["status"], "failed")
        self.assertEqual(self.job(jid2)["status"], "done")

    def test_the_daily_limit_parks_a_new_selection_but_not_one_that_already_started(self):
        stop = threading.Event()
        started = pipeline.enqueue_url(URL, "gta6")
        self.agent.script.append(self.interrupted(stop))
        worker.process_next("w", stop=stop)  # its selection started, then the server stopped
        fresh = pipeline.enqueue_url("https://youtu.be/BBBBBBBBBBB", "gta6")
        with mock.patch.object(costs, "budget_ok", return_value=False):
            self.drive()
            self.assertEqual(self.job(fresh)["status"], "waiting_budget")
            self.assertEqual(self.job(started)["status"], "waiting_approval")  # finished under its own cap
        self.assertEqual({c["job_id"] for c in self.agent.calls}, {started})

    def test_a_selection_paused_by_a_restart_keeps_its_room_in_the_daily_limit(self):
        store.set_settings({"claude.daily_limit_eur": 3.0, "claude.selection_limit_eur": 2.0})
        stop = threading.Event()
        started = pipeline.enqueue_url(URL, "gta6")
        self.agent.script.append(self.interrupted(stop))
        worker.process_next("w", stop=stop)  # started, then the server stopped: no reservation in memory any more
        store.add_usage(None, "select", "claude-sonnet-5-5", input_tokens=600_000)  # 1,10 € spent today
        fresh = pipeline.enqueue_url("https://youtu.be/BBBBBBBBBBB", "gta6")
        store.update_job(fresh, priority=1)  # the new one is claimed first
        worker.process_next("w2")
        self.assertEqual(self.job(fresh)["status"], "waiting_budget")  # 1,10 € + 2,00 € the paused one may spend
        self.assertEqual(pipeline.held_by_paused_selections(), 2.0)
        self.drive()
        self.assertEqual(self.job(started)["status"], "waiting_approval")  # resumed without asking the limit
        self.assertEqual(pipeline.held_by_paused_selections(), 0.0)
        self.assertEqual(pipeline.resume_waiting(), [fresh])
        self.drive()
        self.assertEqual(self.job(fresh)["status"], "waiting_approval")

    def test_what_running_selections_may_still_spend_holds_back_a_new_one(self):
        costs.reserve("select-999", 5.0)  # a selection elsewhere may still spend the whole 5,00 € limit
        self.addCleanup(costs.release, "select-999")
        jid = self.gated()
        job = self.job(jid)
        self.assertEqual(job["status"], "waiting_budget")
        self.assertIn("für laufende Auswahlen verplant", job["progress"])
        self.assertIsNone(store.get_alert("budget.reached"))  # the limit itself isn't reached
        self.assertEqual(pipeline.resume_waiting(), [])
        costs.release("select-999")
        self.assertEqual(pipeline.resume_waiting(), [jid])
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")

    def test_parallel_selections_of_one_run_share_max_videos_per_run(self):
        store.set_settings({"prefs.max_videos_per_run": 3})
        rid = store.start_run("scan", "gta6", "schedule")
        a = store.create_job("source", "gta6", clipper_job="AAAAAAAAAAA", source_url=URL, run_id=rid,
                             stage="select", params={"select": {"max_picks": 2, "avoid_openers": []}})
        b = store.create_job("source", "gta6", clipper_job="BBBBBBBBBBB", source_url="https://youtu.be/BBBBBBBBBBB",
                             run_id=rid)
        # a's selection runs on another worker (worker.concurrency 2) and may still make 2 of the 3
        self.assertEqual(pipeline.max_picks_for(self.job(b), 5), 1)
        store.update_job(a, stage="gate", params={}, result={"selection": {"picks": [make_pick(1)]}})
        self.assertEqual(pipeline.max_picks_for(self.job(b), 5), 2)


class LiveStopTests(Helpers):
    """Paused, deleted or switched-off streams: no more chunks for Claude (finding #3; the recorders: scheduler)."""

    def test_chunks_of_paused_deleted_or_switched_off_streams_are_not_clipped(self):
        cj = "live-xqc-twitch-20261117-100000"
        write_live_chunk(cj)
        sid = store.list_streams("gta6")[0]["id"]
        store.update_stream(sid, paused=True)
        dropped = []
        self.assertEqual(pipeline.enqueue_live_chunks("gta6", [cj], dropped=dropped), [])
        self.assertEqual(dropped, [cj])
        self.assertEqual(self.clip.args_of("live done"), [["live", "done", "--", cj]])  # cleanup deletes it in 3 h
        store.update_stream(sid, paused=False)
        short = "live-xqc-twitch-20261117-103000"
        write_live_chunk(short, duration=45)  # a stream that drops every minute makes a chunk per drop
        self.assertEqual(pipeline.enqueue_live_chunks("gta6", [short]), [])
        # a chunk queued before its stream was deleted: skipped at ③, Claude is never asked
        queued = "live-xqc-twitch-20261117-110000"
        write_live_chunk(queued)
        [jid] = pipeline.enqueue_live_chunks("gta6", [queued])
        store.delete_stream(sid)
        self.drive(kinds=("live_chunk",))
        job = self.job(jid)
        self.assertEqual(job["status"], "skipped")
        self.assertIn("pausiert, entfernt", job["error"])
        self.assertEqual(self.agent.calls, [])
        self.assertIn(["live", "done", "--", queued], self.clip.args_of("live done"))
        # a switched-off profile, and a recording the dashboard doesn't know at all
        off = "live-xqc-twitch-20261117-113000"
        write_live_chunk(off)
        store.update_profile("gta6", enabled=False)
        self.assertEqual(pipeline.enqueue_live_chunks("gta6", [off]), [])
        store.update_profile("gta6", enabled=True)
        stranger = "live-someone-kick-20261117-113000"
        write_live_chunk(stranger, stream=False)
        jd = paths.jobs_dir() / stranger
        (jd / "job.json").write_text(json.dumps({**json.loads((jd / "job.json").read_text()),
                                                 "url": "https://kick.com/someone"}))
        self.assertEqual(pipeline.enqueue_live_chunks("gta6", [stranger]), [])
        self.assertFalse(pipeline.live_stream_active("gta6", stranger))
        self.assertTrue(pipeline.live_stream_active("gta6", off))


class FootageTests(Helpers):
    """The same footage is never rendered twice, also while a pick is still waiting (finding #1)."""

    def overlapping(self, jid):
        a, b = self.picks(jid)
        store.update_pick(b["id"], data={**b["data"], "start": a["data"]["start"] + 2, "end": a["data"]["end"]})
        return a, store.get_pick(b["id"])

    def test_a_moment_another_pending_pick_holds_goes_back_to_claude_at_the_gate(self):
        a_cj, b_cj = "live-xqc-twitch-20261117-100000", "live-xqc-twitch-20261117-103000"
        write_live_chunk(a_cj)
        write_live_chunk(b_cj)
        [ja] = pipeline.enqueue_live_chunks("gta6", [a_cj])
        self.drive(kinds=("live_chunk",))
        held = self.picks(ja)[0]["data"]  # chunk A's own pick waits for approval
        edit = {k: v for k, v in make_pick(5).items() if k not in ("start", "end")}
        edit["segments"] = [{"start": 0.0, "end": 8.0}, {"start": held["start"], "end": held["start"] + 10,
                                                          "job": a_cj}]  # chunk B's multi-streamer edit
        self.agent.script = [lambda job_id, n, cancel: self.agent.Selection([edit], ["w"], ["curiosity: x"])]
        [jb] = pipeline.enqueue_live_chunks("gta6", [b_cj])
        self.drive(kinds=("live_chunk",))
        calls = [c for c in self.agent.calls if c["job_id"] == jb]
        self.assertEqual(len(calls), 2)  # back to Claude once, with the reason
        self.assertIn(a_cj, [r["job"] for r in calls[0]["reserved"]])  # Claude's own check knew it too
        reason = calls[1]["params"]["check_feedback"]["video 1"]["footage"][0]
        self.assertIn(f"Clip 1 von Job #{ja} (wartet auf Freigabe) nutzt schon 10 s", reason)
        self.assertEqual(self.job(jb)["status"], "waiting_approval")
        self.assertNotIn("segments", self.picks(jb)[0]["data"])

    def test_of_two_overlapping_picks_only_one_can_be_approved_and_approval_checks_again(self):
        jid = self.gated()
        a, b = self.overlapping(jid)
        with self.assertRaises(pipeline.PickCheckFailed) as e:
            pipeline.approve_pick(b["id"])  # a is older: a goes first
        self.assertIn(f"Clip 1 von Job #{jid}", str(e.exception))
        self.assertIn("footage", store.get_pick(b["id"])["check_report"])  # shown in the approval queue
        rid = pipeline.approve_pick(a["id"])  # b is newer and only proposed: it doesn't hold a back
        with self.assertRaises(pipeline.PickCheckFailed):
            pipeline.approve_pick(b["id"])
        pipeline.cancel_job(rid)
        pipeline.reject_pick(a["id"])
        pipeline.approve_pick(b["id"])  # free now: the fresh check clears the stale footage entry
        self.assertNotIn("footage", store.get_pick(b["id"])["check_report"])
        with self.assertRaises(pipeline.PickCheckFailed):  # an edit onto footage a held would be refused too
            c = self.gated("https://youtu.be/BBBBBBBBBBB")
            first = self.picks(c)[0]
            pipeline.approve_pick(first["id"])
            pipeline.update_pick_data(self.picks(c)[1]["id"], {"start": first["data"]["start"],
                                                               "end": first["data"]["start"] + 20})

    def test_a_render_whose_footage_was_taken_meanwhile_sends_the_pick_back_to_approval(self):
        jid = self.gated()
        a, b = self.overlapping(jid)
        with store.transaction():  # both approved before either was checked against the other (a race)
            ra = pipeline._approve_in_tx(a["id"], self.job(jid))
            pipeline._approve_in_tx(b["id"], self.job(jid))
        store.update_job(ra, priority=99)  # b's render comes first: b wins, a must not render the same moment
        self.drive(kinds=["render"])
        self.assertEqual(store.get_pick(b["id"])["status"], "rendered")
        pa = store.get_pick(a["id"])
        self.assertEqual(pa["status"], "proposed")
        self.assertIn("Nicht gerendert", pa["error"])
        self.assertIn("already uses 18s of this footage", pa["error"])
        self.assertIn("footage", pa["check_report"])
        self.assertEqual(self.job(ra)["status"], "skipped")
        self.assertEqual(len(self.clip.args_of("render")), 1)  # only b was rendered
        self.assertIsNotNone(store.get_alert(f"pick.footage.{a['id']}"))
        self.assertEqual(self.job(jid)["stage"], "approval")
        with self.assertRaises(pipeline.PickCheckFailed):  # approving it again is refused while b's clip exists
            pipeline.approve_pick(a["id"])
        pipeline.reject_pick(a["id"])
        self.assertIsNotNone(store.get_alert(f"pick.footage.{a['id']}")["resolved_at"])
        self.assertEqual(self.job(jid)["status"], "done")


class RepairTests(Helpers):
    def test_a_crash_between_a_render_and_settling_its_parent_is_repaired(self):
        store.set_settings({"approval.mode": "auto"})
        cj = "live-xqc-twitch-20261117-100000"
        write_live_chunk(cj)
        [jid] = pipeline.enqueue_live_chunks("gta6", [cj])
        self.agent.script = [1]
        self.drive(kinds=("live_chunk",))

        class PowerCut(BaseException):
            pass
        with mock.patch.object(pipeline, "settle_parent", side_effect=PowerCut()), self.assertRaises(PowerCut):
            self.drive(kinds=("render",))  # the render is committed, then the process dies
        pipeline.release_stale(0)
        self.drive()
        self.assertEqual(self.job(jid)["status"], "waiting_approval")  # nothing else would ever settle it
        self.assertEqual(pipeline.reconcile(), [jid])
        self.drive()
        job = self.job(jid)
        self.assertEqual((job["status"], job["result"].get("live_done")), ("done", True))
        self.assertEqual(pipeline.reconcile(), [])

    def test_a_broken_render_is_discarded_and_rendered_again(self):
        jid = self.gated()
        pick = self.picks(jid)[0]
        self.clip.bad_videos = [[pick["data"]["hooks"][0].replace("*", "").replace("?", "") + ".mp4"]]
        rid = pipeline.approve_pick(pick["id"])
        self.drive()
        job = self.job(rid)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))
        self.assertIn("fehlerhaft", job["error"])
        self.assertIn("kein Ton", job["error"])
        self.assertEqual(list((paths.exports_dir() / "gta6" / date.today().isoformat()).glob("video *")), [])
        self.assertEqual(store.list_videos(pick_id=pick["id"])[1], 0)
        self.drive(now=time.time() + 120)
        self.assertEqual(self.job(rid)["status"], "done")
        self.assertEqual(store.list_videos(pick_id=pick["id"])[1], 1)

    def test_downloads_wait_while_the_disk_is_below_min_free_gb(self):
        jid = pipeline.enqueue_url(URL, "gta6")
        with mock.patch.object(pipeline, "disk_low", return_value=(12.3, 30.0)):
            self.drive()
            worker.process_next("w", now=time.time() + 1000)  # a quarter of an hour later: still too full
        job = self.job(jid)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 0))
        self.assertIn("nur 12,3 GB frei", job["progress"])
        self.assertEqual(self.clip.args_of("fetch"), [])
        self.assertEqual(sum("der Download wartet" in e["message"] for e in store.job_events(jid)), 1)
        self.drive(now=time.time() + 1000)
        self.assertEqual(self.job(jid)["status"], "waiting_approval")


class UploadTests(Helpers):
    """A finished upload is never forgotten, and a variant is not posted twice to one channel by accident (#2)."""

    def setup_variant(self):
        _, _, video = self.rendered_video()
        acc = store.add_account("youtube", "Main", connected=True, credentials={"access_token": "x"})
        return video["variants"][0]["id"], acc

    def test_a_cancel_during_the_last_chunk_keeps_the_finished_upload(self):
        var, acc = self.setup_variant()
        up = pipeline.enqueue_upload(var, acc, "public")
        real = self.yt.upload_variant

        def upload(variant_id, account_id, privacy, *, on_session, **kw):
            def started(uri):
                on_session(uri)
                pipeline.cancel_job(up)  # the owner clicks "Abbrechen" while the bytes go out ...
            return real(variant_id, account_id, privacy, on_session=started, **kw)  # ... and YouTube takes them
        self.yt.upload_variant = upload
        self.drive(kinds=("upload",))
        post = store.list_posts(var)[0]
        self.assertEqual((post["status"], post["external_id"], post["error"]), ("posted", "yt123", None))
        self.assertEqual(self.job(up)["status"], "done")
        self.assertTrue(any("Abbruch kam zu spät" in e["message"] for e in store.job_events(up)))

    def test_a_posted_variant_goes_to_the_same_channel_again_only_when_forced(self):
        var, acc = self.setup_variant()
        pipeline.enqueue_upload(var, acc, "public")
        self.drive(kinds=("upload",))
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_upload(var, acc, "public")
        self.assertEqual((e.exception.code, e.exception.status), ("already_posted", 409))
        self.assertIn("schon auf „Main“ hochgeladen (https://youtu.be/yt123)", e.exception.message)
        other = store.add_account("youtube", "Zweitkanal", connected=True, credentials={"access_token": "y"})
        pipeline.enqueue_upload(var, other, "public")  # another channel: fine
        pipeline.enqueue_upload(var, acc, "public", force=True)  # the owner confirmed a second copy
        self.drive(kinds=("upload",))
        self.assertEqual([p["status"] for p in store.list_posts(var)], ["posted"] * 3)

    def test_a_new_upload_asks_the_session_of_an_earlier_failed_one_first(self):
        var, acc = self.setup_variant()
        self.yt.script = [self.yt.YouTubeError("Verbindung verloren", retryable=False)]
        up = pipeline.enqueue_upload(var, acc, "private")
        self.drive(kinds=("upload",))
        self.assertEqual(self.job(up)["status"], "failed")
        up2 = pipeline.enqueue_upload(var, acc, "private")  # maybe the first one reached YouTube after all
        self.assertEqual(self.job(up2)["params"]["upload_session"], "https://upload.example/session-1")
        self.drive(kinds=("upload",))
        self.assertEqual(self.yt.sessions[-1], "https://upload.example/session-1")
        self.assertEqual(store.list_posts(var)[-1]["status"], "posted")


class InstagramUploadTests(Helpers):
    """The same upload flow for Instagram: dispatch on the account's platform, its own options and file check, the
    resume state in the job's params, and the same never-twice guarantees as YouTube's."""

    def setup_variant(self, n=0):
        _, _, video = self.rendered_video()
        acc = store.add_account("instagram", "@gta6clips", connected=True, credentials={"access_token": "IGx"})
        return video["variants"][n]["id"], acc

    def test_an_instagram_account_gets_the_instagram_publisher(self):
        var, acc = self.setup_variant()
        up = pipeline.enqueue_upload(var, acc, {"share_to_feed": False})
        job = self.job(up)
        self.assertEqual((job["params"]["platform"], job["params"]["options"]), ("instagram", {"share_to_feed": False}))
        self.assertNotIn("privacy", job["params"])
        self.assertEqual(store.list_posts(var)[0]["platform"], "instagram")
        self.drive(kinds=("upload",))
        post = store.list_posts(var)[0]
        self.assertEqual((post["status"], post["external_id"], post["url"], post["privacy"]),
                         ("posted", "M1", "https://www.instagram.com/reel/M1/", None))
        self.assertEqual(self.ig.calls, [(var, acc, {"share_to_feed": False})])
        self.assertEqual(self.yt.calls, [])
        job = self.job(up)
        self.assertEqual((job["status"], job["progress"]), ("done", "Hochgeladen: https://www.instagram.com/reel/M1/"))
        self.assertEqual((job["params"]["ig_container"], bool(job["params"]["ig_publish_at"])), ("C1", True))
        messages = [e["message"] for e in store.job_events(up)]
        self.assertIn("Instagram verarbeitet das Video …", messages)
        self.assertIn("Auf Instagram: https://www.instagram.com/reel/M1/", messages)

    def test_options_file_and_connection_are_checked_before_queueing(self):
        var, acc = self.setup_variant()
        for options, field in (("public", "privacy"), ({"privacy": "public"}, "privacy"),
                               ({"share_to_feed": "yes"}, "share_to_feed")):
            with self.assertRaises(pipeline.PipelineError) as e:
                pipeline.enqueue_upload(var, acc, options)
            self.assertEqual((e.exception.status, list(e.exception.fields)), (422, [field]))
        self.assertEqual(pipeline.enqueue_upload(var, acc, None) and self.job(
            store.list_jobs(kind="upload")[0][0]["id"])["params"]["options"], {"share_to_feed": True})  # default
        self.drive(kinds=("upload",))
        var2 = store.get_video(store.get_variant(var)["video_id"])["variants"][1]["id"]
        self.ig.problems = ["Das Video ist 2 s lang – ein Reel muss mindestens 3 s lang sein."]
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_upload(var2, acc, {})
        self.assertEqual((e.exception.code, e.exception.status), ("unsupported_media", 422))
        self.assertIn("Instagram nimmt diese Datei nicht an: Das Video ist 2 s lang", e.exception.message)
        self.assertEqual(store.list_posts(var2), [])
        self.ig.problems = []
        off = store.add_account("instagram", "Aus")
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_upload(var2, off, {})
        self.assertEqual(e.exception.code, "not_connected")
        tt = store.add_account("tiktok", "TT", connected=True)
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_upload(var2, tt, {})
        self.assertEqual(e.exception.code, "not_supported")

    def test_a_posted_variant_goes_to_the_same_instagram_account_again_only_when_forced(self):
        var, acc = self.setup_variant()
        pipeline.enqueue_upload(var, acc, {})
        self.drive(kinds=("upload",))
        with self.assertRaises(pipeline.PipelineError) as e:
            pipeline.enqueue_upload(var, acc, {})
        self.assertEqual(e.exception.code, "already_posted")
        self.assertIn("(https://www.instagram.com/reel/M1/)", e.exception.message)
        yt = store.add_account("youtube", "Main", connected=True, credentials={"access_token": "x"})
        pipeline.enqueue_upload(var, yt, "public")  # the same variant on YouTube: fine
        forced = pipeline.enqueue_upload(var, acc, {}, force=True)
        self.assertTrue(self.job(forced)["params"]["forced"])
        self.assertNotIn("ig_container", self.job(forced)["params"])  # a new Reel on purpose: nothing carried
        self.drive(kinds=("upload",))
        self.assertEqual([p["status"] for p in store.list_posts(var)], ["posted"] * 3)
        self.assertEqual(len(self.ig.published), 2)

    def test_a_power_cut_after_the_publish_request_resumes_without_a_second_reel(self):
        var, acc = self.setup_variant()

        class PowerCut(BaseException):
            after_publish = True
        self.ig.script = [PowerCut()]
        up = pipeline.enqueue_upload(var, acc, {})
        with self.assertRaises(PowerCut):
            self.drive(kinds=("upload",))
        params = self.job(up)["params"]
        self.assertTrue(params["ig_container"] and params["ig_publish_at"])  # written before the request left
        pipeline.release_stale(0)  # the next server start
        self.drive(kinds=("upload",))
        self.assertEqual(self.ig.states[-1]["ig_container"], params["ig_container"])
        self.assertEqual(len(self.ig.published), 1)
        post = store.list_posts(var)[0]
        self.assertEqual((post["status"], post["external_id"]), ("posted", "M1"))
        self.assertTrue(any("kein doppelter Reel" in e["message"] for e in store.job_events(up)))

    def test_a_new_upload_carries_what_an_earlier_failed_one_left(self):
        var, acc = self.setup_variant()
        lost = self.ig.InstagramError("Verbindung verloren")
        lost.after_publish = True
        self.ig.script = [lost]  # the publish went out, the answer never came, the job failed
        first = pipeline.enqueue_upload(var, acc, {"share_to_feed": True})
        self.drive(kinds=("upload",))
        self.assertEqual(self.job(first)["status"], "failed")
        again = pipeline.enqueue_upload(var, acc, {"share_to_feed": False})  # other options: still looked up first
        self.assertEqual(self.job(again)["params"]["ig_publish_at"], self.job(first)["params"]["ig_publish_at"])
        self.drive(kinds=("upload",))
        self.assertEqual((len(self.ig.published), store.list_posts(var)[-1]["status"]), (1, "posted"))
        # a container that was only created (no publish sent) is reused with the same options, not with others
        var2 = store.get_video(store.get_variant(var)["video_id"])["variants"][1]["id"]
        self.ig.script = [self.ig.InstagramError("Instagram verarbeitet zu lange", retryable=False)]
        failed = pipeline.enqueue_upload(var2, acc, {"share_to_feed": True})
        self.drive(kinds=("upload",))
        cid = self.job(failed)["params"]["ig_container"]
        other = pipeline.enqueue_upload(var2, acc, {"share_to_feed": False})
        self.assertNotIn("ig_container", self.job(other)["params"])
        pipeline.cancel_job(other)
        same = pipeline.enqueue_upload(var2, acc, {"share_to_feed": True})
        self.assertEqual(self.job(same)["params"]["ig_container"], cid)

    def test_a_cancel_after_the_publish_request_keeps_the_reel(self):
        var, acc = self.setup_variant()
        up = pipeline.enqueue_upload(var, acc, {})
        real = self.ig.publish

        def publish(*a, on_state, **kw):
            def saved(changes):
                on_state(changes)
                if "ig_publish_at" in changes:
                    pipeline.cancel_job(up)  # "Abbrechen" a moment after the publish request left
            return real(*a, on_state=saved, **kw)
        self.ig.publish = publish
        self.drive(kinds=("upload",))
        post = store.list_posts(var)[0]
        self.assertEqual((post["status"], post["external_id"], post["error"]), ("posted", "M1", None))
        self.assertEqual(self.job(up)["status"], "done")
        self.assertTrue(any("der Reel war schon veröffentlicht" in e["message"] for e in store.job_events(up)))

    def test_a_cancel_before_the_publish_request_publishes_nothing(self):
        var, acc = self.setup_variant()
        up = pipeline.enqueue_upload(var, acc, {})
        self.ig.script = [lambda: pipeline.cancel_job(up)]
        self.drive(kinds=("upload",))
        self.assertEqual((self.job(up)["status"], self.ig.published), ("canceled", {}))
        self.assertEqual(store.list_posts(var)[0]["status"], "failed")

    def test_errors_retry_or_fail_with_the_platform_in_the_alert(self):
        var, acc = self.setup_variant()
        self.ig.script = [self.ig.InstagramError("Instagram bremst gerade", retryable=True, slow=True),
                          self.ig.InstagramError("Dem Zugriff fehlt eine Berechtigung", retryable=False)]
        up = pipeline.enqueue_upload(var, acc, {})
        before = time.time()
        self.drive(kinds=("upload",))
        job = self.job(up)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))
        self.assertGreaterEqual(job["not_before"], before + pipeline.SLOW_BACKOFF_S[0])  # a rate limit waits longer
        self.drive(kinds=("upload",), now=time.time() + 3600)
        self.assertEqual(self.job(up)["status"], "failed")
        alert = store.get_alert(f"job.failed.{up}")
        self.assertIn("(Instagram-Upload", alert["message"])
        self.assertEqual(store.list_posts(var)[0]["error"], "Dem Zugriff fehlt eine Berechtigung")


class ConcurrencyTests(Helpers):
    def test_two_workers_never_render_at_the_same_time_and_number_videos_uniquely(self):
        store.set_settings({"approval.mode": "auto", "prefs.videos_per_run": 4})
        jid = pipeline.enqueue_url(URL, "gta6")
        self.drive(kinds=["source"])
        self.assertEqual(len(store.list_jobs(kind="render", status="queued")[0]), 4)
        self.clip.render_sleep = 0.05

        def lane(name):
            try:
                while worker.process_next(name, ["render", "dub"]) is not None or store.count_jobs("running", "render"):
                    time.sleep(0.005)
            finally:
                db.close()
        threads = [threading.Thread(target=lane, args=(f"w{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(self.clip.render_max, 1)
        self.assertEqual(sorted(v["number"] for v in store.list_videos()[0]), [1, 2, 3, 4])
        self.assertEqual(self.job(jid)["status"], "done")

    def test_started_workers_run_jobs_and_stop_promptly(self):
        stop = threading.Event()
        with mock.patch.object(worker, "POLL_S", 0.05), mock.patch.object(worker, "HEARTBEAT_S", 0.05):
            threads = worker.start_workers(stop)
            self.assertEqual({t.name for t in threads}, {"worker-supervisor", "worker-live", "worker-render",
                                                         "worker-upload", "worker-source-1"})
            jid = pipeline.enqueue_url(URL, "gta6")
            for _ in range(500):
                if self.job(jid)["status"] == "waiting_approval":
                    break
                time.sleep(0.01)
            self.assertEqual(self.job(jid)["status"], "waiting_approval")
            stop.set()
            for t in threads:
                t.join(5)
                self.assertFalse(t.is_alive(), t.name)


class UnitTests(unittest.TestCase):
    def test_youtube_ids_and_sections(self):
        for url in ("https://www.youtube.com/watch?v=AAAAAAAAAAA", "https://youtu.be/AAAAAAAAAAA?si=x",
                    "https://www.youtube.com/shorts/AAAAAAAAAAA", "https://youtube.com/live/AAAAAAAAAAA",
                    "AAAAAAAAAAA", "https://m.youtube.com/watch?feature=x&v=AAAAAAAAAAA"):
            self.assertEqual(pipeline.youtube_id(url), "AAAAAAAAAAA", url)
        for bad in ("https://www.twitch.tv/videos/123", "https://evil.com/watch?v=AAAAAAAAAAA", "AAAA",
                    "https://www.youtube.com/@KaiCenat"):
            self.assertIsNone(pipeline.youtube_id(bad), bad)
        self.assertEqual(pipeline.daily_section(3 * 3600, 90), "0:00-1:30:00")
        self.assertEqual(pipeline.daily_section(100 * 60, 45), "0:00-0:45:00")
        self.assertIsNone(pipeline.daily_section(80 * 60, 90))

    def test_trailing_json_after_progress_lines(self):
        text = "[render] video 3 (1 of 1)\n[render]   video 3/x.mp4\n" + json.dumps([{"video": 3}], indent=2)
        self.assertEqual(pipeline.trailing_json(text), [{"video": 3}])
        self.assertEqual(pipeline.trailing_json('[download] 5%\n{\n  "id": "x"\n}\n'), {"id": "x"})
        with self.assertRaises(ValueError):
            pipeline.trailing_json("[render] nothing")

    def test_fetch_errors_are_classified(self):
        e = pipeline._fetch_error("ERROR: [youtube] x: Video unavailable. This video has been removed", scan=True)
        self.assertEqual((e.retryable, e.skip), (False, True))
        e = pipeline._fetch_error("ERROR: Sign in to confirm you’re not a bot", scan=True)
        self.assertEqual((e.retryable, e.slow, e.skip), (True, True, False))
        e = pipeline._fetch_error("ERROR: Sign in to confirm your age", scan=False)
        self.assertEqual((e.retryable, e.skip), (False, False))
        self.assertTrue(pipeline._fetch_error("ERROR: unable to download webpage: timed out", scan=True).retryable)

    def test_progress_parsers(self):
        self.assertEqual(pipeline._fetch_progress("[download]  42.3% of ~1.2GiB at 2MiB/s ETA 07:12"),
                         "Lade Video … 42 % (noch 07:12)")
        parse = pipeline._render_progress(3)
        self.assertIn("trenne Stimmen", parse("[render] video 4 (1 of 1): separating voices + diarizing ..."))
        self.assertEqual(parse("[render]   video 4/Why did Kai run.mp4"), "Rendere Video 4: Hook-Variante 1 von 3 …")

    def test_a_commands_json_result_is_not_stored_as_log_lines(self):
        for line in ('[', '  {', '    "titles": [', '      "3 Things Kai Noticed In GTA 6 \\ud83d\\ude2d",',
                     '      "POV: You Finally Play GTA 6"', '    "rank": 2,', '      12.5,', '      null', '  },', ']'):
            self.assertTrue(worker.JSON_LINE_RE.match(line), line)
        for line in ("[render] video 1/Why did Kai run.mp4", "ERROR: boom", '"quoted" said Kai', "Fertig: video 4"):
            self.assertFalse(worker.JSON_LINE_RE.match(line), line)

    def test_reap_orphans_stops_a_leftover_of_the_same_command(self):
        marker = f"orphan-test-{os.getpid()}"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "clipper", "render",
                                 marker, "--picks", "/x"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(0.2)
        self.assertEqual(worker.reap_orphans("clipper", ["render", "other-job"]), [])
        self.assertEqual(worker.reap_orphans("clipper", ["render", marker, "--picks", "/x"]), [proc.pid])
        self.assertEqual(proc.wait(10), -signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
