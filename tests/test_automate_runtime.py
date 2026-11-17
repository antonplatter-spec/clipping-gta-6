"""automate.runtime: the YAML overlay seen through the real clipper CLI (no network), the clipper env, and the
subprocess runner (streaming, cancel/timeout of the whole process group, big and non-UTF-8 output, stdin)."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from automate import db, importer, paths, runtime, store

TRACKED = ["watchlist.yaml", "prefs.yaml", "profiles/gta6/creators.yaml", "profiles/gta6/live.yaml",
           "profiles/gta6/profile.yaml", "profiles/gta-rp/creators.yaml", "profiles/gta-rp/live.yaml",
           "profiles/gta-rp/profile.yaml"]


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


def py(code):
    return [sys.executable, "-c", textwrap.dedent(code)]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def wait_dead(pid, timeout=5):
    end = time.time() + timeout
    while time.time() < end and alive(pid):
        time.sleep(0.05)
    return not alive(pid)


def git_status():
    if not shutil.which("git") or not (paths.ROOT / ".git").exists():
        return None
    return subprocess.run(["git", "status", "--porcelain", "--", *TRACKED], cwd=paths.ROOT, capture_output=True,
                          text=True).stdout


class OverlayTests(TempData):
    def test_the_cli_sees_the_db_and_the_repo_files_stay_untouched(self):
        before = {f: (paths.ROOT / f).read_bytes() for f in TRACKED}
        status = git_status()
        importer.run(env_file=self.root / "no.env")
        creators = store.list_creators("gta6")
        store.update_creator(creators[0]["id"], paused=True)
        store.add_creator("gta6", "Test Creator", "https://www.youtube.com/@TestCreator/videos", "keywords")
        store.update_stream(store.list_streams("gta6")[0]["id"], paused=True)
        store.add_stream("gta6", "tester-kick", "https://kick.com/tester", topic_only=False, priority=5)
        store.set_settings({"prefs.voice": "off", "prefs.hooks_per_video": 3, "schedule.scan_times": ["18:30", "07:15"],
                            "watchlist.overrides": {"min_free_gb": 12, "weights": {"jump": 0.25}}})
        store.update_profile("gta6", overrides={"hashtags": ["#gta6", "#automate"]})

        res = runtime.run_clipper(["creators", "list"], "gta6")
        self.assertEqual(res.returncode, 0, res.stderr)
        names = [line.split("\t")[0] for line in res.stdout.splitlines()]
        self.assertNotIn(creators[0]["name"], names)
        self.assertEqual(names, [c["name"] for c in creators[1:]] + ["Test Creator"])

        prefs = json.loads(runtime.run_clipper(["prefs", "show"], "gta6").stdout)
        self.assertEqual((prefs["voice"], prefs["hooks_per_video"], prefs["schedule_time"]), ("off", 3, "07:15"))

        streams = json.loads(runtime.run_clipper(["live", "list"], "gta6").stdout)
        repo_streams = store.list_streams("gta6")
        self.assertEqual([s["name"] for s in streams],
                         [s["name"] for s in repo_streams if not s["paused"]])
        self.assertEqual(streams[0], {"name": "tester-kick", "url": "https://kick.com/tester", "topic_only": False,
                                      "priority": 5})

        cfg = runtime.run_clipper([], "gta6", overlay=True, argv=py("""
            import json
            from clipper import config, library
            cfg = config.load()
            print(json.dumps({k: cfg.get(k) for k in ("min_free_gb", "weights", "hashtags", "profile", "channels")}))
            print(library.hooks_per_video())
            print(config.PROFILES)
            """))
        out = cfg.stdout.splitlines()
        eff = json.loads(out[0])
        self.assertEqual((eff["min_free_gb"], eff["weights"]["jump"], eff["hashtags"], eff["profile"]),
                         (12, 0.25, ["#gta6", "#automate"], "gta6"))
        self.assertEqual(eff["weights"]["loudness"], 0.30)  # merged one level, not replaced
        self.assertIn({"name": "Test Creator", "url": "https://www.youtube.com/@TestCreator/videos",
                       "match": "keywords"}, eff["channels"])
        self.assertEqual((out[1], out[2]), ("3", str(paths.runtime_profiles_dir())))

        # `clipper prefs set` under the overlay changes only the overlay; the DB wins on the next materialize()
        overlay_prefs = paths.runtime_profiles_dir() / "gta6" / "prefs.yaml"
        self.assertEqual(runtime.run_clipper(["prefs", "set", "voice=all"], "gta6", overlay=False).returncode, 0)
        self.assertIn("voice: all", overlay_prefs.read_text())
        self.assertEqual(runtime.materialize(), ["profiles/gta6/prefs.yaml"])
        self.assertIn("voice: 'off'", overlay_prefs.read_text())

        self.assertEqual({f: (paths.ROOT / f).read_bytes() for f in TRACKED}, before)
        self.assertEqual(git_status(), status)

    def test_materialize_writes_only_changes_and_mirrors_extra_profile_files(self):
        repo = self.root / "repo-profiles"
        shutil.copytree(paths.repo_profiles_dir(), repo)
        (repo / "gta6" / "learned-weights.yaml").write_text("weights: {jump: 0.3}\n")
        with mock.patch.object(paths, "repo_profiles_dir", return_value=repo):
            first = runtime.materialize()
            self.assertIn("profiles/gta6/learned-weights.yaml", first)
            self.assertIn("watchlist.yaml", first)
            self.assertEqual(runtime.materialize(), [])
            # no overrides: the repo files are copied verbatim (comments and all)
            self.assertEqual((paths.runtime_profiles_dir() / "gta6" / "profile.yaml").read_bytes(),
                             (repo / "gta6" / "profile.yaml").read_bytes())
            self.assertEqual(paths.runtime_watchlist().read_bytes(), paths.repo_watchlist().read_bytes())
            (repo / "gta6" / "learned-weights.yaml").unlink()
            shutil.rmtree(repo / "gta-rp")
            self.assertEqual(runtime.materialize(), ["profiles/gta6/learned-weights.yaml", "profiles/gta-rp"])
            self.assertFalse((paths.runtime_profiles_dir() / "gta-rp").exists())
        self.assertEqual(runtime.watchlist_config()["base"]["min_free_gb"], 30)
        store.update_profile("gta6", overrides={"search": []})
        self.assertEqual(runtime.profile_config("gta6")["effective"]["search"], [])

    def test_clipper_env(self):
        with mock.patch.dict(os.environ, {"FISH_API_KEY": ""}):
            os.environ.pop("FISH_API_KEY")
            env = runtime.clipper_env("gta-rp")
            self.assertEqual(env["FISH_API_KEY"], "")  # blocks a stale key in .env: the dashboard has none
            with mock.patch.dict(os.environ, {"FISH_API_KEY": "from-shell"}):
                self.assertEqual(runtime.clipper_env("gta6")["FISH_API_KEY"], "from-shell")
                store.set_secret("fish_api_key", "fish-1234567890abcdef")
                self.assertEqual(runtime.clipper_env("gta6")["FISH_API_KEY"], "fish-1234567890abcdef")
        store.set_settings({"render.top_hooks": 2})
        env = runtime.clipper_env("gta-rp")
        self.assertEqual((env["CLIPPER_PROFILE"], env["CLIPPER_TOP_HOOKS"], env["PYTHONUNBUFFERED"]),
                         ("gta-rp", "2", "1"))
        self.assertEqual(env["CLIPPER_CONFIG"], str(paths.runtime_watchlist()))
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(Path(sys.executable).parent))


class RunClipperTests(TempData):
    def test_lines_stream_while_running_and_cancel_stops_the_whole_group(self):
        lines, seen, cancel, result = [], threading.Event(), threading.Event(), {}

        def on_line(line):
            lines.append(line)
            if line.startswith("grandchild"):
                seen.set()
        code = py("""
            import subprocess, sys, time
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print("grandchild", g.pid, flush=True)
            time.sleep(60)
            """)
        t = threading.Thread(target=lambda: result.update(r=runtime.run_clipper([], "gta6", argv=code, on_line=on_line,
                                                                                cancel=cancel)))
        t.start()
        self.assertTrue(seen.wait(15), lines)
        self.assertTrue(t.is_alive())  # the line arrived while the command still ran
        cancel.set()
        t.join(20)
        res = result["r"]
        self.assertTrue(res.canceled)
        self.assertEqual(res.returncode, -signal.SIGTERM)
        self.assertTrue(wait_dead(int(lines[0].split()[1])), "the grandchild survived the cancel")

    def test_timeout(self):
        res = runtime.run_clipper([], "gta6", argv=py("import time; time.sleep(60)"), timeout=0.5)
        self.assertTrue(res.timed_out)
        self.assertFalse(res.canceled)
        self.assertLess(res.duration, 10)

    def test_large_output_on_both_pipes_never_deadlocks(self):
        res = runtime.run_clipper([], "gta6", argv=py("""
            import sys
            for i in range(4000):
                sys.stdout.write("o" * 2047 + "\\n")
                sys.stderr.write("e" * 1023 + "\\n")
            sys.stderr.write("LAST\\n")
            """), timeout=60)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(len(res.stdout), 4000 * 2048)
        self.assertLessEqual(len(res.stderr), runtime.STDERR_KEEP)
        self.assertTrue(res.stderr.endswith("LAST\n"))

    def test_bytes_that_are_not_utf8_and_progress_lines(self):
        lines = []
        res = runtime.run_clipper([], "gta6", on_line=lines.append, argv=py("""
            import sys
            sys.stdout.buffer.write(b"bad \\xff\\xfe bytes\\n")
            sys.stdout.flush()
            sys.stderr.write("10%\\r20%\\r30%\\n")
            sys.stdout.write("no newline at the end")
            """))
        self.assertEqual(res.returncode, 0)
        self.assertIn("bad �� bytes", res.stdout)
        self.assertIn("bad �� bytes", lines)
        self.assertEqual([x for x in lines if x.endswith("%")], ["10%", "20%", "30%"])
        self.assertIn("no newline at the end", lines)  # flushed at EOF (stdout/stderr order varies)

    def test_stdin_exit_code_and_a_broken_callback(self):
        def boom(line):
            raise RuntimeError("callback bug")
        res = runtime.run_clipper([], "gta6", on_line=boom, input='{"x": 1}', argv=py("""
            import json, sys
            print(json.load(sys.stdin)["x"] + 1)
            sys.exit(3)
            """))
        self.assertEqual((res.returncode, res.stdout.strip()), (3, "2"))

    def test_detached_children_survive_and_leftovers_holding_the_pipe_are_killed(self):
        res = runtime.run_clipper([], "gta6", argv=py("""
            import subprocess, sys
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(g.pid)
            """), timeout=30)
        pid = int(res.stdout)
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertTrue(alive(pid), "a detached recorder must keep running")
        self.assertLess(res.duration, 5)
        with mock.patch.object(runtime, "DRAIN_S", 0.3):
            res = runtime.run_clipper([], "gta6", argv=py("""
                import subprocess, sys
                g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                print(g.pid, flush=True)
                """), timeout=30)
        self.assertLess(res.duration, 10)
        self.assertTrue(wait_dead(int(res.stdout)))


class BridgeRunTests(TempData):
    def test_errors_come_back_as_bridge_error(self):
        with self.assertRaises(runtime.BridgeError) as e:
            runtime.run_bridge("no-such-op", {}, "gta6")
        self.assertIn("unknown op", e.exception.message)
        self.assertIn("Traceback", e.exception.stderr)
        with self.assertRaises(runtime.BridgeError) as e:
            runtime.run_bridge("check", {"picks": "nope"}, "gta6")
        self.assertIn("list of pick objects", e.exception.message)

    def test_stdout_carries_only_the_result(self):
        res = runtime.run_clipper([], "gta6", overlay=True, input="{}", argv=py("""
            import subprocess, sys
            from automate import bridge
            def noisy(payload, cfg):
                print("progress from clipper code")
                subprocess.run(["echo", "a child process"])
                return {"ok": True, "profile": cfg["profile"]}
            bridge.OPS["noisy"] = noisy
            sys.exit(bridge.main(["noisy"]))
            """))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"ok": True, "profile": "gta6"})
        self.assertIn("progress from clipper code", res.stderr)
        self.assertIn("a child process", res.stderr)


if __name__ == "__main__":
    unittest.main()
