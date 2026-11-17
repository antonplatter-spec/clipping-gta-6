"""automate.health: the system report built from clipper's own setup check (through the bridge, with a fallback),
the readiness checklist, the cached live status, and check_all raising and resolving every alert it owns."""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automate_pipeline_fakes import KEY, PipelineCase  # noqa: E402

from automate import health, pipeline, runtime, store  # noqa: E402


class SystemReportTests(PipelineCase):
    def tools(self, **kw):
        return {t["name"]: t for t in health.system_report(refresh=True, **kw)["tools"]}

    def test_tools_with_versions_hints_and_flags(self):
        self.clip.system["which"]["yt-dlp"] = False
        with mock.patch.object(health, "_is_mac", return_value=False):
            rep = health.system_report(refresh=True)
        tools = {t["name"]: t for t in rep["tools"]}
        self.assertEqual((tools["ffmpeg"]["ok"], tools["ffmpeg"]["version"], tools["ffmpeg"]["hint"]),
                         (True, "6.1.1", None))
        self.assertFalse(tools["yt-dlp"]["ok"])
        self.assertTrue(tools["yt-dlp"]["required"])
        self.assertIn("uv lock --upgrade-package yt-dlp", tools["yt-dlp"]["hint"])  # a project dependency now
        self.assertTrue(tools["parakeet"]["ok"])
        self.assertNotIn("whisper.cpp", tools)
        self.assertFalse(tools["demucs"]["required"])
        self.assertIn("espeak-ng", tools)
        for t in rep["tools"]:
            self.assertTrue(t["purpose"], t["name"])
        self.assertEqual(set(rep["paths"]), {"root", "data", "database", "jobs", "exports", "logs"})
        self.assertEqual(set(rep["disk"]), {"path", "free_gb", "total_gb", "min_free_gb"})
        self.assertEqual(rep["disk"]["min_free_gb"], 30.0)
        self.assertEqual(rep["setup_report"], "ok       ffmpeg")
        self.assertIsNone(rep["asr_model"])
        self.clip.system["asr_model"] = {"name": "nemo-parakeet-tdt-0.6b-v3", "quantization": "int8", "cached": False}
        self.assertFalse(health.system_report(refresh=True)["asr_model"]["cached"])
        n = len(self.clip.bridge_calls)
        health.system_report()
        self.assertEqual(len(self.clip.bridge_calls), n)  # cached

    def test_whisper_backend_and_mac_hints(self):
        self.clip.system.update(asr="whisper", model_missing=True, is_mac=True)
        with mock.patch.object(health, "_is_mac", return_value=True):
            tools = self.tools()
        self.assertFalse(tools["whisper.cpp"]["ok"])
        self.assertEqual(tools["whisper.cpp"]["hint"], "brew install whisper-cpp")
        self.assertFalse(tools["whisper-modell"]["ok"])
        self.assertNotIn("parakeet", tools)
        self.assertTrue(tools["say"]["ok"])
        self.assertEqual(tools["yt-dlp"]["hint"], None)

    def test_a_failing_bridge_falls_back_to_an_in_process_check(self):
        self.clip.system = runtime.BridgeError("clipper import failed")
        rep = health.system_report(refresh=True)
        self.assertIn("clipper import failed", rep["setup_report"])
        self.assertIn("ffmpeg", {t["name"] for t in rep["tools"]})

    def test_readiness(self):
        store.add_creator("gta6", "Kai", "https://www.youtube.com/@KaiCenat/videos")
        store.add_stream("gta6", "xqc-twitch", "https://www.twitch.tv/xqc")
        store.add_stream("gta-rp", "other", "https://www.twitch.tv/other")  # profile off: not counted
        store.add_account("youtube", "Main", connected=True)
        r = health.readiness()
        self.assertEqual(r, {"anthropic_key": True, "fish_key": False, "youtube_connected": True, "tools_ok": True,
                             "sources": 2, "automation": False})
        self.clip.system["which"]["ffmpeg"] = False
        health.system_report(refresh=True)
        self.assertFalse(health.readiness()["tools_ok"])


class CheckAllTests(PipelineCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(health.shutil, "disk_usage", return_value=mock.Mock(free=500e9, total=1000e9))
        p.start()  # the machine's real free space must not decide whether disk.low is raised
        self.addCleanup(p.stop)

    def active(self):
        return {a["key"]: a for a in store.list_alerts()}

    def test_alerts_are_raised_with_german_hints_and_resolved_when_fixed(self):
        store.set_settings({"automation.enabled": True, "watchlist.overrides": {"min_free_gb": 10 ** 9}})
        pipeline.on_settings_changed(["watchlist.overrides"])
        store.set_secret("anthropic_api_key", None)
        self.clip.system["which"]["yt-dlp"] = False
        changes = health.check_all()
        raised = {c["key"] for c in changes if c["action"] == "raised"}
        self.assertEqual(raised, {"disk.low", "tools.missing", "key.anthropic_missing"})
        active = self.active()
        self.assertIn("yt-dlp", active["tools.missing"]["message"])
        self.assertIn("System", active["tools.missing"]["message"])
        self.assertIn("Kosten & Schlüssel", active["key.anthropic_missing"]["message"])
        self.assertIn("GB frei", active["disk.low"]["message"])
        self.assertEqual((health.gb_text(29.6), health.gb_text(30), health.gb_text(1.25)),
                         ("29,6 GB", "30 GB", "1,2 GB"))  # 29.6 free next to a 30 GB minimum must not read "30 GB"
        self.assertEqual(health.check_all(), [])  # nothing new the second time
        run = store.last_run("health")
        self.assertIn("aktiv", run["summary"]["message"])

        store.set_settings({"watchlist.overrides": {}})
        pipeline.on_settings_changed(["watchlist.overrides"])
        store.set_secret("anthropic_api_key", KEY)
        self.clip.system["which"]["yt-dlp"] = True  # installed: the active alert makes the next check look again
        changes = health.check_all()
        self.assertEqual({c["key"] for c in changes if c["action"] == "resolved"},
                         {"disk.low", "tools.missing", "key.anthropic_missing"})
        self.assertEqual(self.active(), {})
        self.assertEqual(store.last_run("health")["summary"]["message"], "Alles in Ordnung")

    def test_voice_and_fish_key_warnings(self):
        self.clip.system["which"]["espeak-ng"] = False
        store.set_settings({"dub.auto_languages": ["es"]})
        with mock.patch.object(health, "_is_mac", return_value=False):
            health.check_all()
        self.assertEqual(set(self.active()), {"tools.voice", "key.fish_missing"})
        self.assertIn("espeak-ng", self.active()["tools.voice"]["message"])
        store.set_secret("fish_api_key", "fishkey1234567")
        health.check_all()
        self.assertEqual(self.active(), {})

    def test_the_old_clipper_schedule_timers_are_reported(self):
        timer = Path("/home/x/.config/systemd/user/clipper-gta6-daily.timer")
        with mock.patch.object(health, "_old_schedules", return_value=[timer]):
            health.check_all()
        alert = self.active()["schedule.old"]
        self.assertIn("clipper schedule remove", alert["message"])
        self.assertEqual(alert["details"]["files"], [str(timer)])
        health.check_all()  # removed (PipelineCase: none installed)
        self.assertNotIn("schedule.old", self.active())

    def test_usage_of_a_model_without_a_price_is_reported_until_it_has_one(self):
        store.add_usage(None, "select", "claude-future-9", input_tokens=1000)
        alert = self.active()["prices.unknown.claude-future-9"]
        self.assertIn("kein Preis hinterlegt", alert["message"])
        self.assertIn("Einstellungen → Claude → Preise", alert["message"])
        health.check_all()
        self.assertIn("prices.unknown.claude-future-9", self.active())
        prices = store.get_setting("claude.prices")
        store.set_settings({"claude.prices": {**prices, "claude-future-9": {"input": 1, "output": 5,
                                                                            "cache_read": 0.1, "cache_write": 1.25}}})
        health.check_all()
        self.assertNotIn("prices.unknown.claude-future-9", self.active())
        store.add_usage(None, "select", "claude-sonnet-5-5", input_tokens=1000)  # a known model: no alert
        self.assertEqual([k for k in self.active() if k.startswith("prices.")], [])

    def test_failed_job_alerts_are_resolved_once_the_job_moved_on(self):
        jid = store.create_job("source", "gta6", clipper_job="AAAAAAAAAAA", status="failed", error="x")
        pipeline.raise_failed_alert(store.get_job(jid), "Download fehlgeschlagen")
        health.check_all()
        self.assertIn(f"job.failed.{jid}", self.active())
        store.update_job(jid, status="canceled")
        health.check_all()
        self.assertNotIn(f"job.failed.{jid}", self.active())

    def test_live_alerts_from_the_cached_live_tick(self):
        store.add_stream("gta6", "xqc-twitch", "https://www.twitch.tv/xqc")
        store.set_settings({"automation.live_enabled": True})
        health.check_all()
        self.assertIn("live.service", self.active())  # enabled, but never checked
        health.store_live("gta6", {
            "streams": [{"name": "xqc-twitch", "url": "https://www.twitch.tv/xqc", "live": True, "on_topic": True,
                         "recording": False, "title": "GTA 6"}],
            "issues": {"not_recording": "live on-topic streams not being recorded: xqc-twitch (run `live watch`)",
                       "stalled": "recorders running but writing no video: xqc-twitch (12 min)",
                       "analysis_failed": "chunks whose analysis failed 3 times (see jobs/_live/reanalyze.log): "
                                          "live-xqc-twitch-20261117-100000"},
            "stalled": ["xqc-twitch"], "restarted": ["xqc-twitch"], "recorders": 0})
        health.check_all()
        active = self.active()
        self.assertNotIn("live.service", active)
        self.assertIn("xqc-twitch", active["live.not_recording"]["message"])
        self.assertEqual(active["live.not_recording"]["level"], "error")
        self.assertIn("automatisch neu gestartet", active["live.stalled"]["message"])
        self.assertIn("live-xqc-twitch-20261117-100000", active["live.analysis_failed"]["message"])
        with mock.patch.object(time, "time", return_value=time.time() + 3600):
            health.check_all()
        self.assertIn("live.service", self.active())  # the live service stopped reporting
        store.set_settings({"automation.live_enabled": False})
        health.check_all()
        self.assertFalse([k for k in self.active() if k.startswith("live.")])

    def test_live_status_survives_a_restart_through_meta(self):
        sid = store.add_stream("gta6", "xqc-twitch", "https://www.twitch.tv/xqc")
        health.store_live("gta6", {"streams": [{"name": "xqc-twitch", "url": "https://www.twitch.tv/xqc", "live": False,
                                                "on_topic": False, "recording": False, "title": None}],
                                   "recorders": 0})
        with mock.patch.object(health, "_live", None):
            st = health.live_status()
        row = next(s for s in st["streams"] if s["id"] == sid)
        self.assertEqual((row["live"], row["recording"]), (False, False))
        self.assertIsNotNone(st["checked_at"])
