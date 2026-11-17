"""automate.autostart: the generated systemd unit / LaunchAgent / WSL batch file, and the commands install, remove,
status and restart run (a fake runner and a temp home: nothing is enabled on this machine)."""
import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from automate import autostart


class FakeRun:
    """subprocess.run stand-in: answers by the longest matching command prefix, records every call."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(list(cmd))
        best = None
        for prefix, answer in self.answers.items():
            if tuple(cmd[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, answer)
        if best is None:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        answer = best[1]
        if callable(answer):
            answer = answer(cmd)
        if isinstance(answer, BaseException):
            raise answer
        rc, out = answer
        return subprocess.CompletedProcess(cmd, rc, out, "" if rc == 0 else out)

    def ran(self, *prefix):
        return any(tuple(c[:len(prefix)]) == prefix for c in self.calls)


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.root = base / "mein clipper"  # a space, as in many real paths
        self.root.mkdir()
        patches = [mock.patch.dict(os.environ, {"AUTOMATE_DATA_DIR": str(base / "data"),
                                                "AUTOMATE_LOGS_DIR": str(base / "logs")}),
                   mock.patch.object(autostart.getpass, "getuser", return_value="alex"),
                   mock.patch.object(autostart, "_uv_dir", return_value="/opt/uv/bin")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()


class UnitTextTests(unittest.TestCase):
    def test_quoting(self):
        self.assertEqual(autostart.systemd_quote("/bin/sh"), "/bin/sh")
        self.assertEqual(autostart.systemd_quote("/home/a b/start.sh"), '"/home/a b/start.sh"')
        self.assertEqual(autostart.systemd_quote('/x/"q"\\'), '"/x/\\"q\\"\\\\"')
        self.assertEqual(autostart.systemd_quote("/data/100%"), "/data/100%%")
        self.assertEqual(autostart.systemd_quote("$HOME"), "$$HOME")

    def test_unit(self):
        text = autostart.unit_text(Path("/home/alex/mein clipper"), "/opt/uv/bin:/usr/bin:/bin")
        lines = text.splitlines()
        self.assertIn('ExecStart=/bin/sh "/home/alex/mein clipper/start.sh" --no-browser', lines)
        self.assertIn("WorkingDirectory=/home/alex/mein clipper", lines)
        self.assertIn('Environment="PATH=/opt/uv/bin:/usr/bin:/bin"', lines)
        for line in ("Restart=on-failure", "KillMode=process", "WantedBy=default.target", "Type=simple"):
            self.assertIn(line, lines)
        self.assertTrue(any(ln.startswith("TimeoutStopSec=") for ln in lines))

    def test_plist(self):
        data = plistlib.loads(autostart.plist_bytes(Path("/Users/alex/mein clipper"), "/opt/homebrew/bin:/usr/bin",
                                                    Path("/Users/alex/mein clipper/logs/automate/launchd.log")))
        self.assertEqual(data["Label"], autostart.LABEL)
        self.assertEqual(data["ProgramArguments"], ["/bin/sh", "/Users/alex/mein clipper/start.sh", "--no-browser"])
        self.assertEqual(data["WorkingDirectory"], "/Users/alex/mein clipper")
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["AbandonProcessGroup"])
        self.assertEqual(data["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin")
        self.assertTrue(data["StandardErrorPath"].endswith("launchd.log"))

    def test_wsl_cmd(self):
        text = autostart.wsl_cmd("/home/alex/mein clipper", "Ubuntu-24.04")
        self.assertTrue(text.endswith("\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        text.encode("ascii")  # cmd.exe reads batch files in the OEM code page
        last = text.splitlines()[-1]
        self.assertEqual(last, 'start "GTA Clipper" /min wsl.exe -d "Ubuntu-24.04" --cd "/home/alex/mein clipper" '
                               '--exec /bin/sh "/home/alex/mein clipper/start.sh" --no-browser')

    def test_service_path_env(self):
        venv_bin = str(autostart.ROOT / ".venv" / "bin")
        path = autostart.service_path_env(Path("/home/alex"), "linux", current=f"{venv_bin}:/usr/bin:relative::/usr/bin",
                                          uv_dir="/opt/uv/bin").split(":")
        self.assertEqual(path[:3], ["/opt/uv/bin", "/home/alex/.local/bin", "/usr/bin"])
        self.assertNotIn(venv_bin, path)
        self.assertNotIn("relative", path)
        self.assertEqual(len(path), len(set(path)))
        mac = autostart.service_path_env(Path("/Users/alex"), "mac", current="/usr/bin", uv_dir=None).split(":")
        self.assertIn("/opt/homebrew/bin", mac)


class OldSchedulesTests(TempHome):
    def test_found_per_platform(self):
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        for name in ("clipper-gta6-daily.timer", "clipper-gta6-live-watch.timer", "clipper-gta6-daily.service",
                     "other.timer"):
            (units / name).touch()
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        for name in ("com.clipper.gta6.daily.plist", "com.clipper.gta6.live-watch-gta-rp.plist",
                     f"{autostart.LABEL}.plist"):
            (agents / name).touch()
        self.assertEqual([p.name for p in autostart.old_schedules(self.home, "linux")],
                         ["clipper-gta6-daily.timer", "clipper-gta6-live-watch.timer"])
        self.assertEqual([p.name for p in autostart.old_schedules(self.home, "mac")],
                         ["com.clipper.gta6.daily.plist", "com.clipper.gta6.live-watch-gta-rp.plist"])
        self.assertEqual(autostart.old_schedules(self.home, "other"), [])


class ServerRunningTests(TempHome):
    def test_lock(self):
        self.assertIsNone(autostart.server_running())  # no data dir yet
        data = Path(os.environ["AUTOMATE_DATA_DIR"])
        data.mkdir()
        lock = data / "server.lock"
        lock.write_text(json.dumps({"pid": 4242, "url": "http://localhost:8765"}), encoding="utf-8")
        self.assertIsNone(autostart.server_running())  # file there, nobody holds it
        with open(lock, "a+", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(autostart.server_running()["pid"], 4242)
        self.assertIsNone(autostart.server_running())


class LinuxTests(TempHome):
    def test_install(self):
        run = FakeRun({("loginctl", "show-user"): (0, "no\n")})
        ok, text = autostart.install("linux", self.home, run, self.root)
        self.assertTrue(ok, text)
        unit = self.home / ".config/systemd/user/gta-clipper.service"
        self.assertIn(f'ExecStart=/bin/sh "{self.root}/start.sh" --no-browser', unit.read_text(encoding="utf-8"))
        self.assertIn("/opt/uv/bin", unit.read_text(encoding="utf-8"))
        self.assertTrue(run.ran("systemctl", "--user", "daemon-reload"))
        self.assertTrue(run.ran("systemctl", "--user", "enable", "--now", "gta-clipper.service"))
        self.assertTrue(run.ran("loginctl", "enable-linger", "alex"))
        self.assertIn("sudo loginctl enable-linger alex", text)  # enable-linger "worked" but Linger still says no
        self.assertNotIn("ACHTUNG", text)

    def test_install_linger_and_old_schedule_warning(self):
        state = {"linger": "no"}

        def enable(cmd):
            state["linger"] = "yes"
            return 0, ""
        run = FakeRun({("loginctl", "show-user"): lambda cmd: (0, state["linger"] + "\n"),
                       ("loginctl", "enable-linger"): enable})
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        (units / "clipper-gta6-daily.timer").touch()
        ok, text = autostart.install("linux", self.home, run, self.root)
        self.assertTrue(ok)
        self.assertIn("ohne Anmeldung", text)
        self.assertNotIn("sudo", text)
        self.assertIn("ACHTUNG", text)
        self.assertIn(autostart.REMOVE_OLD, text)

    def test_install_without_systemd(self):
        run = FakeRun({("systemctl", "--user", "show-environment"): (1, "Failed to connect to bus")})
        ok, text = autostart.install("linux", self.home, run, self.root)
        self.assertFalse(ok)
        self.assertIn("./start.sh", text)
        self.assertFalse((self.home / ".config/systemd/user/gta-clipper.service").exists())
        self.assertFalse(run.ran("systemctl", "--user", "enable"))

    def test_install_missing_systemctl(self):
        ok, text = autostart.install("linux", self.home, FakeRun({("systemctl",): FileNotFoundError("systemctl")}),
                                     self.root)
        self.assertFalse(ok)

    def test_enable_failure_is_reported(self):
        run = FakeRun({("systemctl", "--user", "enable"): (1, "Unit file is masked.")})
        ok, text = autostart.install("linux", self.home, run, self.root)
        self.assertFalse(ok)
        self.assertIn("masked", text)

    def test_remove_status_restart(self):
        run = FakeRun({("loginctl", "show-user"): (0, "yes\n"),
                       ("systemctl", "--user", "is-enabled"): (0, "enabled\n"),
                       ("systemctl", "--user", "is-active"): (0, "active\n")})
        ok, text = autostart.restart("linux", self.home, run)
        self.assertFalse(ok)  # nothing installed yet
        autostart.install("linux", self.home, run, self.root)
        ok, text = autostart.status("linux", self.home, run)
        self.assertTrue(ok)
        self.assertIn("eingerichtet", text)
        self.assertIn("läuft", text)
        self.assertIn("Linger): ja", text)
        ok, text = autostart.restart("linux", self.home, run)
        self.assertTrue(ok)
        self.assertTrue(run.ran("systemctl", "--user", "restart", "gta-clipper.service"))
        ok, text = autostart.remove("linux", self.home, run)
        self.assertTrue(ok)
        self.assertTrue(run.ran("systemctl", "--user", "disable", "--now", "gta-clipper.service"))
        self.assertFalse((self.home / ".config/systemd/user/gta-clipper.service").exists())
        self.assertIn("nicht eingerichtet", autostart.status("linux", self.home, run)[1])
        self.assertEqual(autostart.remove("linux", self.home, run), (True, "Kein Autostart eingerichtet."))


class MacTests(TempHome):
    def test_install_status_restart_remove(self):
        run = FakeRun({("launchctl", "print"): (0, "gui/501/com.gtaclipper.server = {\n\tstate = running\n}\n")})
        ok, text = autostart.install("mac", self.home, run, self.root)
        self.assertTrue(ok, text)
        plist = self.home / "Library/LaunchAgents/com.gtaclipper.server.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["ProgramArguments"][1], str(self.root / "start.sh"))
        self.assertTrue(Path(data["StandardOutPath"]).parent.is_dir())
        domain = f"gui/{os.getuid()}"
        self.assertTrue(run.ran("launchctl", "bootstrap", domain, str(plist)))
        self.assertLess(run.calls.index(["launchctl", "bootout", f"{domain}/{autostart.LABEL}"]),
                        run.calls.index(["launchctl", "bootstrap", domain, str(plist)]))
        self.assertIn("Zustand: läuft", autostart.status("mac", self.home, run)[1])
        self.assertTrue(autostart.restart("mac", self.home, run)[0])
        self.assertTrue(run.ran("launchctl", "kickstart", "-k", f"{domain}/{autostart.LABEL}"))
        self.assertTrue(autostart.remove("mac", self.home, run)[0])
        self.assertFalse(plist.exists())

    def test_bootstrap_failure(self):
        ok, text = autostart.install("mac", self.home, FakeRun({("launchctl", "bootstrap"): (5, "Input/output error")}),
                                     self.root)
        self.assertFalse(ok)
        self.assertIn("Input/output error", text)


class WslTests(TempHome):
    def setUp(self):
        super().setUp()
        self.startup = Path(self.tmp.name) / "Startup"
        self.startup.mkdir()
        env = mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-24.04"})
        env.start()
        self.addCleanup(env.stop)

    def test_install_status_remove(self):
        win = r"C:\Users\Alex\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"
        run = FakeRun({("powershell.exe",): (0, win + "\r\n"), ("wslpath", "-u", win): (0, f"{self.startup}\n")})
        ok, text = autostart.install("wsl", self.home, run, self.root)
        self.assertTrue(ok, text)
        batch = (self.startup / "GTA Clipper.cmd").read_bytes()
        self.assertIn(b'wsl.exe -d "Ubuntu-24.04"', batch)
        self.assertIn(b"\r\n", batch)
        self.assertIn("eingerichtet", autostart.status("wsl", self.home, run)[1])
        self.assertFalse(autostart.restart("wsl", self.home, run)[0])
        self.assertTrue(autostart.remove("wsl", self.home, run)[0])
        self.assertFalse((self.startup / "GTA Clipper.cmd").exists())

    def test_cmd_fallback_and_not_found(self):
        appdata = r"C:\Users\Alex\AppData\Roaming"
        run = FakeRun({("powershell.exe",): FileNotFoundError("powershell.exe"),
                       ("cmd.exe",): (0, appdata + "\r\n"),
                       ("wslpath",): (0, f"{self.startup}\n")})
        self.assertTrue(autostart.install("wsl", self.home, run, self.root)[0])
        self.assertTrue(run.ran("wslpath", "-u", appdata + r"\Microsoft\Windows\Start Menu\Programs\Startup"))
        ok, text = autostart.install("wsl", self.home, FakeRun({("powershell.exe",): (1, ""), ("cmd.exe",): (1, "")}),
                                     self.root)
        self.assertFalse(ok)
        self.assertIn("shell:startup", text)
        self.assertIn("wsl.exe", text)


class MainTests(TempHome):
    def test_unsupported_platform_and_cli(self):
        self.assertFalse(autostart.install("other", self.home, FakeRun(), self.root)[0])
        with mock.patch.object(autostart, "status", return_value=(True, "Autostart: nicht eingerichtet")), \
                mock.patch("builtins.print") as out:
            self.assertEqual(autostart.main([]), 0)
        out.assert_called_once_with("Autostart: nicht eingerichtet")
        with self.assertRaises(SystemExit):
            autostart.main(["start"])


if __name__ == "__main__":
    unittest.main()
