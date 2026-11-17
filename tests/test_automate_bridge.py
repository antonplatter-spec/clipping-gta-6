"""automate.bridge ops on realistic picks: `check` reproduces `clipper check --job` exactly, `rank` keeps what
`clipper simulate` keeps (titles moving with their hooks), and the file ops (analysis, transcript, manifests, music)."""
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automate import bridge, db, paths, runtime, store
from clipper import config, library, music, render, simulate

W = lambda w, s, e: {"w": w, "s": s, "e": e}  # noqa: E731


def make_job(root, jid="automate-test-job"):
    job = root / jid
    (job / "review").mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"id": jid, "url": f"https://www.youtube.com/watch?v={jid}",
                                              "channel": "Kai Cenat", "title": "Kai plays GTA 6", "offset": 3600.0}))
    words = [W("um", 10.1, 10.4), W("Kai", 10.5, 10.8), W("runs", 10.9, 11.3), W("away!", 11.4, 12.0),
             W("Why", 20.0, 20.3), W("is", 20.3, 20.5), W("Kai", 20.5, 20.8), W("shaking?", 20.8, 21.5),
             W("Oh", 29.0, 29.2), W("my", 29.2, 29.4), W("god", 29.4, 29.9), W("run!", 30.0, 30.6)]
    (job / "transcript.json").write_text(json.dumps({"words": words}))
    (job / "candidates.json").write_text(json.dumps({
        "video": {"id": jid, "title": "Kai plays GTA 6"}, "recommended": 2, "clip_type": "reaction",
        "profile": "reaction",
        "clip_length": {"target": 22, "min": 9, "max": 35}, "has_heatmap": False, "hook_plan": "plan",
        "ad_breaks": [], "scenarios": [],
        "candidates": [{"id": 1, "start": 10, "end": 31, "tier": "strong", "transcript": "x" * 1000,
                        "contact_sheet": "review/cand_01.jpg"}]}))
    (job / "review" / "cand_01.jpg").write_bytes(b"jpg")
    return job


PICKS = [
    {"start": 10, "end": 31, "clip_type": "reaction", "hashtags": ["#gta6", "#kaicenat"],
     "hooks": ["Why did Kai *run*?", "Kai NEVER saw it coming", "Wait for it...", "Me when *GTA 6* drops",
               "3 things Kai missed"],
     "titles": ["Why Did Kai Run In GTA 6?", "Kai NEVER Saw It Coming In GTA 6", "Wait For It In GTA 6",
                "Me When GTA 6 Drops", "3 Things Kai Missed In GTA 6"]},
    {"start": 18, "end": 28, "music": [], "hooks": ["Why is Kai shaking?", "Kai can't stop shaking", "Kai is *SHAKING*",
                                                    "Nobody calmed Kai down", "The scariest GTA 6 moment"],
     "titles": ["Why Is Kai Shaking In GTA 6?", "Kai Can't Stop Shaking In GTA 6", "Kai Is SHAKING In GTA 6"]},
]


class OpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load(profile="gta6")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.job = make_job(self.root / "jobs")
        exports = self.root / "exports"
        exports.mkdir()
        p = mock.patch.object(render, "EXPORTS", exports)
        p.start()
        self.addCleanup(p.stop)

    def test_check_reproduces_clipper_check(self):
        out = bridge.check({"picks": PICKS, "job": str(self.job)}, self.cfg)
        f = self.root / "picks.json"
        f.write_text(json.dumps(PICKS))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLIPPER_", "AUTOMATE_"))}
        cli = subprocess.run([sys.executable, "-m", "clipper", "check", str(f), "--job", str(self.job)], cwd=paths.ROOT,
                             capture_output=True, text=True, env=env)
        report, _ = json.JSONDecoder().raw_decode(cli.stdout)
        self.assertEqual(out["report"], report)
        self.assertEqual(out["fails"], cli.returncode == 1)
        self.assertTrue(out["fails"])  # "Wait for it..." is vague
        self.assertIn("edit", out["report"]["video 1"])  # transcript notes need the job's words: opens on "um"

        done = render.EXPORTS / "gta6" / "2026-11-10" / "video 7"
        done.mkdir(parents=True)
        # job.json has offset 3600 (a section download): footage is in video seconds, so 18-28 s is 3618-3628
        (done / "upload.json").write_text(json.dumps([{"footage": [{"source": "automate-test-job", "from": 3605,
                                                                    "to": 3625}]}]))
        clean = [{**PICKS[1], "hooks": PICKS[1]["hooks"][:1], "titles": PICKS[1]["titles"][:1]}]
        self.assertIn("footage", bridge.check({"picks": clean, "job": str(self.job)}, self.cfg)["report"]["video 1"])
        self.assertNotIn("footage", bridge.check({"picks": clean}, self.cfg)["report"].get("video 1", {}))

    def test_check_fails_footage_a_pick_holds_that_is_not_rendered_yet(self):
        t0 = 1_800_000_000.0
        jobs = self.root / "jobs"
        for name in ("live-xqc-twitch-20261117-100000", "live-kai-twitch-20261117-100000"):
            d = jobs / name
            d.mkdir(parents=True)
            streamer = name.split("-")[1]
            (d / "job.json").write_text(json.dumps({"id": name, "url": f"https://www.twitch.tv/{streamer}",
                                                    "title": "GTA 6", "channel": streamer, "duration": 1800,
                                                    "offset": 0.0, "live": True, "recorded_at": t0}))
            (d / "transcript.json").write_text(json.dumps({"words": [W("run", 100.0, 100.4)]}))
        xqc, kai = jobs / "live-xqc-twitch-20261117-100000", jobs / "live-kai-twitch-20261117-100000"
        base = {"clip_type": "reaction", "hashtags": ["#gta6"], "hooks": ["Why did xQc *crash*?"],
                "titles": ["Why Did xQc Crash In GTA 6?"]}
        own = {**base, "start": 100.0, "end": 118.0}  # chunk A's own pick, still waiting for approval
        multi = {**base, "segments": [{"start": 100.0, "end": 110.0},  # chunk B's multi-streamer edit uses it
                                      {"start": 100.0, "end": 112.0, "job": str(xqc)}]}
        held = [{"pick": own, "job": str(xqc), "label": "Clip 1 von Job #7 (wartet auf Freigabe)"}]
        out = bridge.check({"picks": [multi], "job": str(kai), "reserved": held}, self.cfg)
        footage = out["report"]["video 1"]["footage"]
        self.assertTrue(out["fails"])
        self.assertEqual(footage[0], "Clip 1 von Job #7 (wartet auf Freigabe) nutzt schon 12 s dieser Szenen "
                                     "(noch nicht gerendert)")
        self.assertEqual(footage[-1], bridge.RESERVED_NOTE)
        # what the gate saw before: nothing exported, so nothing
        plain = bridge.check({"picks": [multi], "job": str(kai)}, self.cfg)["report"].get("video 1", {})
        self.assertNotIn("footage", plain)
        # another moment, and a held pick whose job folder is gone: free
        later = {**base, "start": 200.0, "end": 215.0}
        gone = [*held, {"pick": own, "job": str(jobs / "deleted-job"), "label": "x"}]
        self.assertNotIn("footage", bridge.check({"picks": [later], "job": str(xqc), "reserved": gone},
                                                 self.cfg)["report"].get("video 1", {}))
        # rendered meanwhile: clipper's own exported-footage line comes first, the held one after it
        done = render.EXPORTS / "gta6" / "2026-11-17" / "video 1"
        done.mkdir(parents=True)
        (done / "upload.json").write_text(json.dumps([{"footage": [{"source": "twitch:xqc", "from": t0 + 100,
                                                                    "to": t0 + 118}]}]))
        footage = bridge.check({"picks": [multi], "job": str(kai), "reserved": held},
                               self.cfg)["report"]["video 1"]["footage"]
        self.assertTrue(footage[0].startswith("Clip 1 von Job #7"))
        self.assertIn("2026-11-17/video 1 already uses 12s of this footage", footage[1])

    def test_the_system_report_says_whether_the_parakeet_model_is_downloaded(self):
        cache = self.root / "hf"
        repo = cache / "models--istupakov--parakeet-tdt-0.6b-v3-onnx"
        code = "import json; from automate import bridge_ext_pipeline as e; print(json.dumps(e._asr_model({})))"

        def cached():
            env = {**os.environ, "HF_HUB_CACHE": str(cache), "HF_HUB_OFFLINE": "1"}
            out = subprocess.run([sys.executable, "-c", code], cwd=paths.ROOT, env=env, capture_output=True,
                                 text=True, timeout=120)
            return json.loads(out.stdout)["cached"]
        self.assertIs(cached(), False)  # the first analysis downloads it (~650 MB)
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("abc123")
        snap = repo / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        for f in ("config.json", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"):
            (snap / f).write_text("x")
        self.assertIs(cached(), False)  # half a download is no model
        (snap / "vocab.txt").write_text("x")
        self.assertIs(cached(), True)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_verify_videos_reads_size_sound_and_length(self):
        from automate import bridge_ext_pipeline as ext
        good, mute, wide = (self.root / n for n in ("good.mp4", "mute.mp4", "wide.mp4"))
        src = ["-f", "lavfi", "-i", "testsrc=size={}:rate=5", "-t", "2"]
        snd = ["-f", "lavfi", "-i", "sine=frequency=440", "-shortest"]
        for f, size, audio in ((good, "1080x1920", True), (mute, "1080x1920", False), (wide, "640x360", True)):
            args = [a.format(size) for a in src] + (snd if audio else [])
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args, "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", str(f)], check=True)
        broken = self.root / "broken.mp4"
        broken.write_bytes(b"mp4")
        rows = {Path(r["file"]).name: r for r in ext.verify_videos({"files": [str(good), str(mute), str(wide),
                                                                             str(broken)]}, self.cfg)["videos"]}
        self.assertEqual(rows["good.mp4"]["ok"], True)
        self.assertEqual(rows["mute.mp4"]["problems"], ["kein Ton"])
        self.assertEqual(rows["wide.mp4"]["problems"], ["640x360 statt 1080x1920"])
        self.assertFalse(rows["broken.mp4"]["ok"])
        self.assertIn("kein lesbares Video", rows["broken.mp4"]["problems"][0])

    def test_rank_keeps_what_clipper_simulate_keeps_and_titles_follow_their_hooks(self):
        single = {"start": 0, "end": 10, "hook": "Why did Kai *run*?", "title": "Why Did Kai Run In GTA 6?"}
        vague = {"start": 0, "end": 10, "hooks": ["Wait for it...", "Watch this"], "titles": ["A", "B"]}
        fewer_runs = functools.partial(simulate.simulate, runs=40)  # the same for both sides; 200 runs take seconds
        with mock.patch.object(simulate, "simulate", fewer_runs):
            res = bridge.rank({"picks": PICKS + [single, vague], "keep": 3}, self.cfg)
            text = simulate.report(PICKS + [vague], keep=3, **simulate.profile_kw(self.cfg))
            auto = bridge.rank({"picks": [PICKS[0]]}, self.cfg)
        keep_lines = [x[2:] for x in text.splitlines() if x.startswith("  keep (top")]
        r = res["ranking"]
        self.assertEqual([r[0]["keep_line"], r[1]["keep_line"], r[3]["keep_line"]], keep_lines)

        a, b, s, v = res["picks"]
        self.assertEqual(len(a["hooks"]), 3)
        self.assertNotIn("Wait for it...", a["hooks"])
        for k, entry in enumerate(r[0]["kept"]):  # equal counts: each title stays with its hook
            self.assertEqual((a["hooks"][k], a["titles"][k]), (PICKS[0]["hooks"][entry["index"]],
                                                               PICKS[0]["titles"][entry["index"]]))
        self.assertEqual(len({e["type"] for e in r[0]["kept"]}), 3)
        self.assertEqual(r[0]["titles_dropped"], [t for i, t in enumerate(PICKS[0]["titles"])
                                                  if i not in {e["index"] for e in r[0]["kept"]}])
        # 5 hooks, 3 titles: hook i keeps title i; a kept hook without one takes a title no kept hook owns
        idx = [e["index"] for e in r[1]["kept"]]
        self.assertEqual(len(b["titles"]), min(len(idx), 3))
        for k, i in enumerate(idx):
            if i < 3:
                self.assertEqual(b["titles"][k], PICKS[1]["titles"][i])
        self.assertEqual(sorted(b["titles"] + r[1]["titles_dropped"]), sorted(PICKS[1]["titles"]))
        self.assertEqual((s, r[2]["ok"], r[2]["kept"][0]["index"]), (single, True, 0))
        self.assertEqual((v, r[3]["ok"]), (vague, False))  # nothing qualifies: left as is, flagged
        self.assertEqual(auto["ranking"][0]["keep"], library.hooks_per_video())

    def test_title_pairing_rules(self):
        self.assertEqual(bridge._titles(["t0", "t1", "t2"], 3, [2, 0]), (["t2", "t0"], ["t1"]))
        self.assertEqual(bridge._titles(["t0", "t1", "t2"], 5, [4, 0, 2]), (["t1", "t0", "t2"], []))
        self.assertEqual(bridge._titles(["t0"], 5, [3, 4, 0]), (["t0"], []))
        self.assertEqual(bridge._titles(["t0", "t1", "t2", "t3"], 2, [1]), (["t1"], ["t0", "t2", "t3"]))

    def test_analysis_transcript_and_manifests(self):
        a = bridge.analysis({"job": str(self.job)}, self.cfg)
        self.assertEqual((a["job"], a["recommended"], a["clip_type"], a["sheets"]),
                         ("automate-test-job", 2, "reaction", ["cand_01.jpg"]))
        self.assertEqual(len(a["candidates"][0]["transcript"]), 400)
        t = bridge.transcript({"job": str(self.job), "pattern": "shak"}, self.cfg)
        self.assertEqual((t["total"], t["sentences"][0]["s"], t["offset"]), (1, 20.0, 3600.0))
        self.assertNotIn("words", t)
        t = bridge.transcript({"job": str(self.job), "around_s": 30, "window_s": 2}, self.cfg)
        self.assertEqual([w["w"] for w in t["words"]], ["Oh", "my", "god", "run!"])
        with self.assertRaises(ValueError):
            bridge.transcript({"job": str(self.job), "pattern": "("}, self.cfg)
        folder = self.root / "exports" / "gta6" / "2026-11-17" / "video 1"
        folder.mkdir(parents=True)
        (folder / "upload.json").write_text(json.dumps([{"file": "a.mp4", "hook": "h"}]))
        m = bridge.upload_manifests({"folders": [str(folder), "exports/none/video 9"]}, self.cfg)["manifests"]
        self.assertEqual((m[0]["entries"][0]["hook"], m[1]["entries"], bool(m[1]["error"])), ("h", None, True))

    def test_music_library_merges_local_approvals_and_tracks(self):
        folder = self.root / "music"
        (folder / "local").mkdir(parents=True)
        for name in ("hit.wav", "meme.m4a", "local/own.m4a"):
            (folder / name).write_bytes(b"x")
        (folder / "library.yaml").write_text(
            "tracks:\n  - {id: hit, file: hit.wav, kind: hit, claimable: false, approved: true}\n"
            "  - {id: meme, file: meme.m4a, kind: music, mood: funny, claimable: true, approved: false}\n")
        (folder / "local" / "library.yaml").write_text(
            "tracks:\n  - {id: own, file: own.m4a, kind: music, mood: epic, claimable: false, approved: true}\n")
        with mock.patch.object(music, "MUSIC", folder):
            tracks = {t["id"]: t for t in bridge.music_library({}, self.cfg)["tracks"]}
            self.assertEqual(set(tracks), {"hit", "meme", "own"})
            self.assertEqual((tracks["own"]["local"], tracks["own"]["usable"], tracks["meme"]["usable"]),
                             (True, True, False))
            out = bridge.set_music({"id": "hit", "approved": False}, self.cfg)
            self.assertEqual((out["track"]["approved"], out["track"]["usable"]), (False, False))
            self.assertFalse(next(t for t in music.load_library() if t["id"] == "hit")["approved"])
            self.assertEqual(music.load_library(folder)[0]["approved"], True)  # an explicit folder: no local merge
            with self.assertRaises(ValueError):
                bridge.set_music({"id": "nope", "approved": True}, self.cfg)
        self.assertIn("own", (folder / "local" / "library.yaml").read_text())

    def test_refs(self):
        self.assertEqual(bridge.resolve_stream({"ref": "kick:xqc"}, self.cfg),
                         {"url": "https://kick.com/xqc", "name": "xqc-kick"})
        with self.assertRaises(ValueError):
            bridge.resolve_stream({"ref": "myspace:xqc"}, self.cfg)
        self.assertEqual(bridge.resolve_creator({"ref": "@KaiCenat", "lookup": False}, self.cfg),
                         {"url": "https://www.youtube.com/@KaiCenat/videos", "name": None, "found": False})


class RunBridgeTests(unittest.TestCase):
    def test_check_through_the_subprocess_matches_the_in_process_op(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"AUTOMATE_DATA_DIR": str(Path(d) / "data")}):
            self.addCleanup(db.close)
            store.ensure_profile("gta6", True)
            job = make_job(Path(d) / "jobs", "automate-bridge-run")
            out = runtime.run_bridge("check", {"picks": PICKS, "job": str(job)}, "gta6")
            with mock.patch.object(render, "EXPORTS", Path(d) / "no-exports"):
                want = bridge.check({"picks": PICKS, "job": str(job)}, config.load(profile="gta6"))
        self.assertEqual(out, want)


class LiveProbeTests(unittest.TestCase):
    def test_an_unreachable_platform_is_told_apart_from_an_offline_stream(self):
        from automate import bridge_ext_pipeline as ext

        def run(stderr, rc=1):
            return mock.patch.object(ext.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, "", stderr))
        with run("ERROR: [twitch:stream] xqc: The channel is not currently live"):
            self.assertIsNone(ext._unreachable("https://www.twitch.tv/xqc"))
        with run("ERROR: [twitch:stream] xqc: Unable to download JSON metadata: <urlopen error Tunnel connection "
                 "failed: 403 Forbidden>"):
            self.assertIn("Tunnel connection failed", ext._unreachable("https://www.twitch.tv/xqc"))
        with run("ERROR: [youtube] x: Sign in to confirm you’re not a bot"):
            self.assertIn("not a bot", ext._unreachable("https://www.youtube.com/@x/live"))
        with run("", rc=0):
            self.assertIsNone(ext._unreachable("https://kick.com/xqc"))
        with mock.patch.object(ext.subprocess, "run", side_effect=subprocess.TimeoutExpired("yt-dlp", 60)):
            self.assertIn("Zeitüberschreitung", ext._unreachable("https://kick.com/xqc"))


if __name__ == "__main__":
    unittest.main()
