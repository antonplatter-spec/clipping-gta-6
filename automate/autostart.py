"""Start the server with the computer: `python -m automate.autostart [install | remove | status | restart]`
(German output; exit 1 when something failed).

    Linux     a systemd user service (~/.config/systemd/user/gta-clipper.service), enabled for the next boot and
              (re)started at once; `loginctl enable-linger` so it runs without anyone logged in (tried without sudo,
              else the sudo command is printed). Logs: `journalctl --user -u gta-clipper` and logs/automate/.
    macOS     a LaunchAgent (~/Library/LaunchAgents/com.gtaclipper.server.plist): starts at login, relaunched after
              a crash. Logs: logs/automate/launchd.log and server.log.
    WSL2      WSL runs only while Windows keeps it running, so Windows starts it: a "GTA Clipper.cmd" in the Windows
              Startup folder opens a minimized window that runs start.sh at every Windows login (its wsl.exe process
              is what keeps WSL up). No admin rights needed.

Every variant runs `start.sh --no-browser`: each (re)start first brings the Python packages up to date (uv sync,
keeping the voice extra when installed), then the shell is replaced by the server, so stop signals reach it directly.
Only the server is signalled on stop/restart (systemd KillMode=process, launchd AbandonProcessGroup): it stops its
jobs' subprocesses itself, and the live recorders `clipper live watch` starts detached keep recording through a
restart, as after Ctrl+C. After a crash the next server reaps leftover job processes (worker.reap_orphans).

The `clipper schedule` timers/agents of the Claude Code workflow (clipper/schedule.py) start Claude Code and a live
watcher of their own; `install` and `status` warn while they exist.

Everything takes `home` and `run` (subprocess.run's signature) so tests use a temp dir and a fake runner:
    detect() -> "linux" | "mac" | "wsl" | "other"
    unit_text(root, path, args=()) -> str    plist_bytes(root, path, log, args=()) -> bytes
    wsl_cmd(root, distro, args=()) -> str     (args: extra server flags, e.g. ["--host", "0.0.0.0"])
    old_schedules(home, kind) -> [Path]    service_path(home, kind) -> Path | None        server_running() -> dict|None
    install(kind=None, home=None, run=subprocess.run, root=ROOT, args=()) -> (ok: bool, text: str)
    remove / status / restart(kind=None, home=None, run=subprocess.run) -> (ok, text)
"""
import argparse
import fcntl
import getpass
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

ROOT = paths.ROOT
UNIT = "gta-clipper.service"
LABEL = "com.gtaclipper.server"
WSL_FILE = "GTA Clipper.cmd"
SERVICE_ARGS = ["--no-browser"]
RESTART_SEC = 30
STOP_TIMEOUT_S = 90  # the server's own shutdown: uvicorn 10 s + joining workers/scheduler up to 20 s, with headroom
REMOVE_OLD = "uv run python -m clipper schedule remove"


# ---------------------------------------------------------------- where we are

def detect():
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "wsl" if "microsoft" in platform.release().lower() else "linux"
    return "other"


def _home(home):
    return Path(home) if home is not None else Path.home()


def unit_dir(home):
    xdg = os.environ.get("XDG_CONFIG_HOME") if home is None else None
    return (Path(xdg) if xdg else _home(home) / ".config") / "systemd" / "user"


def agents_dir(home):
    return _home(home) / "Library" / "LaunchAgents"


def service_path(home=None, kind=None):
    kind = kind or detect()
    if kind == "linux":
        return unit_dir(home) / UNIT
    if kind == "mac":
        return agents_dir(home) / f"{LABEL}.plist"
    return None


def old_schedules(home=None, kind=None):
    """The `clipper schedule install|live` timers (Linux, also WSL with systemd) or LaunchAgents (macOS)."""
    kind = kind or detect()
    if kind == "mac":
        return sorted(agents_dir(home).glob("com.clipper.gta6.*.plist"))
    if kind in ("linux", "wsl"):
        return sorted(unit_dir(home).glob("clipper-gta6-*.timer"))
    return []


def server_running():
    """{pid, url, started_at} of the server holding the data dir's lock, or None (nothing is created)."""
    lock = paths.data_dir() / "server.lock"
    if not lock.is_file():
        return None
    with open(lock, encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # held: the server is up
            try:
                return json.loads(f.read() or "{}") or {"pid": None}
            except ValueError:
                return {"pid": None}
        fcntl.flock(f, fcntl.LOCK_UN)
    return None


def _run(run, cmd):
    """run(cmd) -> CompletedProcess; a missing program comes back as returncode 127 instead of raising."""
    try:
        return run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _out(res):
    return " ".join(((res.stderr or "") + " " + (res.stdout or "")).split())[:300]


def service_path_env(home=None, kind=None, current=None, uv_dir=None):
    """PATH for the service: uv's folder and ~/.local/bin first (where uv's installer puts uv), Homebrew on a Mac,
    then this shell's PATH (minus the project's .venv: start.sh runs the venv's python by path anyway)."""
    kind = kind or detect()
    home = _home(home)
    venv = str(ROOT / ".venv")
    parts = [uv_dir, str(home / ".local/bin")]
    if kind == "mac":
        parts += ["/opt/homebrew/bin", "/opt/homebrew/sbin"]
    parts += (current if current is not None else os.environ.get("PATH", "")).split(os.pathsep)
    parts += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    out = []
    for p in parts:
        if p and os.path.isabs(p) and not p.startswith(venv) and p not in out:
            out.append(p)
    return os.pathsep.join(out)


def _uv_dir():
    uv = os.environ.get("UV") or shutil.which("uv")  # `uv run` sets UV to its own binary
    if not uv:
        for d in (Path.home() / ".local/bin", Path.home() / ".cargo/bin"):
            if (d / "uv").exists():
                uv = str(d / "uv")
                break
    return str(Path(uv).resolve().parent) if uv else None


# ---------------------------------------------------------------- the service definitions

def systemd_quote(arg):
    """One ExecStart= argument: '%' and '$' escaped (specifiers, variable expansion), quoted when it has blanks."""
    s = str(arg).replace("%", "%%").replace("$", "$$")
    if any(c in s for c in " \t\"'\\;") or not s:
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _command(root, args):
    return ["/bin/sh", str(Path(root) / "start.sh"), *SERVICE_ARGS, *map(str, args)]


def _dashboard_url(args):
    args = list(args)
    port = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--port"), None)
    port = port or next((a.split("=", 1)[1] for a in args if a.startswith("--port=")), "8765")
    return f"http://localhost:{port}"


def unit_text(root, path, args=()):
    root = Path(root)
    cmd = " ".join(systemd_quote(a) for a in _command(root, args))
    env_path = path.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f"""[Unit]
Description=GTA Clipper – Automatik (Dashboard, Zeitplan, Worker)
Documentation={(root / "docs" / "INSTALLATION.md").as_uri().replace("%", "%%")}

[Service]
Type=simple
WorkingDirectory={str(root).replace("%", "%%")}
Environment="PATH={env_path}"
Environment=PYTHONUNBUFFERED=1
ExecStart={cmd}
Restart=on-failure
RestartSec={RESTART_SEC}
# Only the server gets SIGTERM: it stops its jobs' subprocesses itself, and the live recorders it started detached
# keep recording through a restart (as after Ctrl+C).
KillMode=process
TimeoutStopSec={STOP_TIMEOUT_S}

[Install]
WantedBy=default.target
"""


def plist_bytes(root, path, log, args=()):
    root = Path(root)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": _command(root, args),
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # relaunch after a crash, not after a clean stop
        "ThrottleInterval": RESTART_SEC,
        "ExitTimeOut": STOP_TIMEOUT_S,
        "AbandonProcessGroup": True,  # live recorders outlive a restart
        "ProcessType": "Interactive",  # unset means "light resource limits": CPU and I/O throttled while rendering
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    })


def wsl_cmd(root, distro, args=()):
    """The Windows batch file for the Startup folder (CRLF, ASCII only: cmd.exe reads it in the OEM code page)."""
    root = str(root)
    flags = " ".join(f'"{a}"' if any(c in str(a) for c in ' &|<>^"') else str(a) for a in [*SERVICE_ARGS, *args])
    lines = ["@echo off",
             "rem GTA Clipper - Automatik: starts the server in WSL at every Windows login.",
             "rem Created by: python -m automate.autostart install (remove it with: ... autostart remove).",
             "rem The minimized window IS the server: closing it stops the server.",
             f'start "GTA Clipper" /min wsl.exe -d "{distro}" --cd "{root}" --exec /bin/sh "{root}/start.sh" {flags}']
    return "\r\n".join(lines) + "\r\n"


def _wsl_startup_dir(run):
    """The Windows Startup folder as a Linux path (through WSL interop), or None."""
    res = _run(run, ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "[Environment]::GetFolderPath('Startup')"])
    win = (res.stdout or "").strip() if res.returncode == 0 else ""
    if not win:
        res = _run(run, ["cmd.exe", "/c", "echo %APPDATA%"])
        appdata = (res.stdout or "").strip() if res.returncode == 0 else ""
        if not appdata or "%" in appdata:
            return None
        win = appdata + r"\Microsoft\Windows\Start Menu\Programs\Startup"
    res = _run(run, ["wslpath", "-u", win])
    out = (res.stdout or "").strip()
    return Path(out) if res.returncode == 0 and out else None


# ---------------------------------------------------------------- shared text

def _old_warning(home, kind):
    old = old_schedules(home, kind)
    if not old:
        return []
    return ["", "ACHTUNG: Die alten Zeitpläne von „clipper schedule“ sind noch eingerichtet:",
            *[f"  {p}" for p in old],
            "Sie starten Claude Code und einen eigenen Live-Watcher neben dem Server (doppelte Arbeit und Kosten).",
            f"Entfernen mit: {REMOVE_OLD}"]


def _running_line():
    info = server_running()
    if not info:
        return "Server: läuft gerade nicht."
    return (f"Server: läuft (Prozess {info.get('pid') or '?'}"
            + (f", {info['url']}" if info.get("url") else "") + ").")


def _manual_start():
    return "Ohne Autostart startest du den Server mit ./start.sh im Ordner des Clippers."


# ---------------------------------------------------------------- Linux (systemd)

def _systemd_ready(run):
    res = _run(run, ["systemctl", "--user", "show-environment"])
    return res.returncode == 0, _out(res)


def _linger(run, user):
    res = _run(run, ["loginctl", "show-user", user, "--property=Linger", "--value"])
    return (res.stdout or "").strip() == "yes"


def _install_linux(home, run, root, args):
    ok, why = _systemd_ready(run)
    if not ok:
        return False, ("systemd-Benutzerdienste sind hier nicht verfügbar"
                       + (f" ({why})" if why else "") + ". " + _manual_start()
                       + " Unter WSL: siehe docs/INSTALLATION.md, Abschnitt „Dauerbetrieb“.")
    unit = service_path(home, "linux")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(root, service_path_env(home, "linux", uv_dir=_uv_dir()), args), encoding="utf-8")
    lines = [f"Autostart eingerichtet: {unit}"]
    # restart, not `enable --now`: a second install (new flags) must replace a running server, a first one starts it
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", UNIT],
                ["systemctl", "--user", "restart", UNIT]):
        res = _run(run, cmd)
        if res.returncode != 0:
            return False, "\n".join(lines + [f"Fehler bei „{' '.join(cmd)}“: {_out(res)}"])
    lines.append("Der Server startet ab jetzt beim Hochfahren und nach einem Absturz von selbst neu.")
    info = server_running()
    if info:
        lines.append(f"Gerade läuft schon ein Server (Prozess {info.get('pid') or '?'}): Der Dienst übernimmt, sobald du "
                     f"ihn beendest (Strg+C in seinem Terminal). Bis dahin versucht er es alle {RESTART_SEC} Sekunden.")
    user = getpass.getuser()
    if not _linger(run, user):
        res = _run(run, ["loginctl", "enable-linger", user])
        if res.returncode == 0 and _linger(run, user):
            lines.append("Läuft auch ohne Anmeldung (loginctl enable-linger ist eingeschaltet).")
        else:
            lines.append(f"Damit er auch ohne Anmeldung läuft, einmal ausführen: sudo loginctl enable-linger {user}")
    lines += ["", f"Dashboard: {_dashboard_url(args)} (nach ein paar Sekunden)",
              "Status:    uv run python -m automate.autostart status",
              f"Live-Log:  journalctl --user -u {UNIT.removesuffix('.service')} -f   (und logs/automate/server.log)"]
    return True, "\n".join(lines + _old_warning(home, "linux"))


def _remove_linux(home, run):
    unit = service_path(home, "linux")
    if not unit.exists():
        return True, "Kein Autostart eingerichtet."
    res = _run(run, ["systemctl", "--user", "disable", "--now", UNIT])
    unit.unlink()
    _run(run, ["systemctl", "--user", "daemon-reload"])
    lines = [f"Autostart entfernt ({unit}); der Server ist beendet."]
    if res.returncode != 0:
        lines.append(f"Hinweis: systemctl meldete: {_out(res)}")
    lines.append("Laufende Live-Aufnahmen enden von selbst, wenn der Stream offline geht. " + _manual_start())
    return True, "\n".join(lines)


def _status_linux(home, run):
    unit = service_path(home, "linux")
    lines = [f"Autostart (systemd-Benutzerdienst {UNIT}): " + ("eingerichtet" if unit.exists() else "nicht eingerichtet")]
    if unit.exists():
        enabled = (_run(run, ["systemctl", "--user", "is-enabled", UNIT]).stdout or "").strip() or "unbekannt"
        active = (_run(run, ["systemctl", "--user", "is-active", UNIT]).stdout or "").strip() or "unbekannt"
        lines += [f"  beim Hochfahren: {'ja' if enabled == 'enabled' else enabled}",
                  f"  Zustand: {'läuft' if active == 'active' else active}"]
        user = getpass.getuser()
        lines.append("  ohne Anmeldung (Linger): " + ("ja" if _linger(run, user) else
                                                       f"nein → sudo loginctl enable-linger {user}"))
    lines.append(_running_line())
    return True, "\n".join(lines + _old_warning(home, "linux"))


def _restart_linux(home, run):
    if not service_path(home, "linux").exists():
        return False, "Kein Autostart eingerichtet. " + _manual_start()
    res = _run(run, ["systemctl", "--user", "restart", UNIT])
    if res.returncode != 0:
        return False, f"Neustart fehlgeschlagen: {_out(res)}"
    return True, "Server wird neu gestartet (Pakete werden dabei auf den Stand gebracht)."


# ---------------------------------------------------------------- macOS (launchd)

def _domain():
    return f"gui/{os.getuid()}"


def _install_mac(home, run, root, args):
    plist = service_path(home, "mac")
    log = paths.logs_dir() / "launchd.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    _run(run, ["launchctl", "bootout", f"{_domain()}/{LABEL}"])  # a previous version, if any
    plist.write_bytes(plist_bytes(root, service_path_env(home, "mac", uv_dir=_uv_dir()), log, args))
    _run(run, ["launchctl", "enable", f"{_domain()}/{LABEL}"])
    res = _run(run, ["launchctl", "bootstrap", _domain(), str(plist)])
    if res.returncode != 0:
        return False, f"Autostart-Datei geschrieben ({plist}), aber launchctl meldete: {_out(res)}"
    lines = [f"Autostart eingerichtet: {plist}",
             ("Der Server startet ab jetzt bei deiner Anmeldung und nach einem Absturz von selbst neu. Der Mac muss "
              "dafür angemeldet und wach bleiben (Systemeinstellungen → Energie)."),
             "", f"Dashboard: {_dashboard_url(args)} (nach ein paar Sekunden)",
             "Status:    uv run python -m automate.autostart status",
             f"Log:       {log} und {paths.logs_dir() / 'server.log'}"]
    if server_running():
        lines.insert(1, "Gerade läuft schon ein Server: Der Dienst übernimmt, sobald du ihn beendest (Strg+C).")
    return True, "\n".join(lines + _old_warning(home, "mac"))


def _remove_mac(home, run):
    plist = service_path(home, "mac")
    if not plist.exists():
        return True, "Kein Autostart eingerichtet."
    _run(run, ["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    plist.unlink()
    return True, (f"Autostart entfernt ({plist}); der Server ist beendet. Laufende Live-Aufnahmen enden von selbst, "
                  "wenn der Stream offline geht. " + _manual_start())


def _status_mac(home, run):
    plist = service_path(home, "mac")
    lines = [f"Autostart (LaunchAgent {LABEL}): " + ("eingerichtet" if plist.exists() else "nicht eingerichtet")]
    if plist.exists():
        res = _run(run, ["launchctl", "print", f"{_domain()}/{LABEL}"])
        if res.returncode != 0:
            lines.append("  nicht geladen (startet bei der nächsten Anmeldung)")
        else:
            state = next((ln.split("=", 1)[1].strip() for ln in res.stdout.splitlines()
                          if ln.strip().startswith("state =")), "unbekannt")
            lines.append(f"  Zustand: {'läuft' if state == 'running' else state}")
    lines.append(_running_line())
    return True, "\n".join(lines + _old_warning(home, "mac"))


def _restart_mac(home, run):
    if not service_path(home, "mac").exists():
        return False, "Kein Autostart eingerichtet. " + _manual_start()
    res = _run(run, ["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"])
    if res.returncode != 0:
        return False, f"Neustart fehlgeschlagen: {_out(res)}"
    return True, "Server wird neu gestartet (Pakete werden dabei auf den Stand gebracht)."


# ---------------------------------------------------------------- Windows (WSL2)

def _wsl_manual(root, distro, args):
    return ("So richtest du es von Hand ein: Windows-Taste + R → shell:startup → dort eine Datei „GTA Clipper.cmd“ "
            "mit diesem Inhalt anlegen:\n\n" + wsl_cmd(root, distro, args).replace("\r\n", "\n"))


def _install_wsl(home, run, root, args):
    distro = os.environ.get("WSL_DISTRO_NAME") or "Ubuntu"
    startup = _wsl_startup_dir(run)
    if startup is None or not startup.is_dir():
        return False, "Den Windows-Autostart-Ordner habe ich nicht gefunden. " + _wsl_manual(root, distro, args)
    target = startup / WSL_FILE
    target.write_bytes(wsl_cmd(root, distro, args).encode("ascii", errors="replace"))
    lines = [f"Autostart eingerichtet: {target}",
             (f"Ab der nächsten Windows-Anmeldung startet ein minimiertes Fenster „GTA Clipper“ den Server in WSL "
              f"({distro}). Das Fenster ist der Server: offen lassen. Damit er rund um die Uhr läuft, darf Windows "
              "nicht in den Energiesparmodus gehen."),
             "Jetzt sofort starten: ./start.sh (oder dich ab- und wieder anmelden)."]
    return True, "\n".join(lines + _old_warning(home, "wsl"))


def _remove_wsl(home, run):
    startup = _wsl_startup_dir(run)
    target = startup / WSL_FILE if startup else None
    if target is None or not target.exists():
        return True, ("Kein Autostart gefunden" + (f" ({target})" if target else "") + ". Liegt er woanders: "
                      "Windows-Taste + R → shell:startup → „GTA Clipper.cmd“ löschen.")
    target.unlink()
    return True, (f"Autostart entfernt ({target}). Ein laufender Server läuft weiter, bis du sein Fenster schließt "
                  "(oder Strg+C drückst).")


def _status_wsl(home, run):
    startup = _wsl_startup_dir(run)
    target = startup / WSL_FILE if startup else None
    if target is None:
        head = "Autostart (Windows-Autostart-Ordner): Ordner nicht gefunden (WSL-Interop aus?)"
    else:
        head = "Autostart (Windows-Autostart-Ordner): " + ("eingerichtet" if target.exists() else "nicht eingerichtet")
    return True, "\n".join([head, _running_line()] + _old_warning(home, "wsl"))


def _restart_wsl(home, run):
    return False, ("Unter WSL startet der Server in seinem eigenen Fenster: dort Strg+C drücken (oder das Fenster "
                   "schließen) und ./start.sh neu starten – oder dich bei Windows ab- und wieder anmelden.")


# ---------------------------------------------------------------- dispatch

ACTIONS = {
    "linux": {"install": _install_linux, "remove": _remove_linux, "status": _status_linux, "restart": _restart_linux},
    "mac": {"install": _install_mac, "remove": _remove_mac, "status": _status_mac, "restart": _restart_mac},
    "wsl": {"install": _install_wsl, "remove": _remove_wsl, "status": _status_wsl, "restart": _restart_wsl},
}


def _table(kind):
    return ACTIONS.get(kind or detect())


UNSUPPORTED = (False, "Autostart gibt es nur unter Linux, macOS und Windows mit WSL2.")


def install(kind=None, home=None, run=subprocess.run, root=ROOT, args=()):
    table = _table(kind)
    return table["install"](home, run, root, list(args)) if table else UNSUPPORTED


def remove(kind=None, home=None, run=subprocess.run):
    table = _table(kind)
    return table["remove"](home, run) if table else UNSUPPORTED


def status(kind=None, home=None, run=subprocess.run):
    table = _table(kind)
    return table["status"](home, run) if table else UNSUPPORTED


def restart(kind=None, home=None, run=subprocess.run):
    table = _table(kind)
    return table["restart"](home, run) if table else UNSUPPORTED


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m automate.autostart",
                                description="Den Server mit dem Computer starten (Linux: systemd, macOS: launchd, "
                                            "Windows: WSL-Autostart).")
    p.add_argument("action", nargs="?", default="status", choices=("install", "remove", "status", "restart"),
                   help="install = einrichten und starten, remove = entfernen und beenden, status = anzeigen "
                        "(Standard), restart = neu starten (z. B. nach einem Update)")
    p.add_argument("server_args", nargs=argparse.REMAINDER,
                   help="nur bei install: weitere Server-Optionen für ./start.sh, z. B. --host 0.0.0.0 --port 8765")
    args = p.parse_args(argv)
    if args.server_args and args.action != "install":
        p.error("Server-Optionen gehen nur mit install.")
    if args.action == "install":
        ok, text = install(args=args.server_args)
    else:
        ok, text = {"remove": remove, "status": status, "restart": restart}[args.action]()
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
