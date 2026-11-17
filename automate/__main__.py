"""`python -m automate [command]`: the local server and its maintenance commands (German output).

    serve (default)  [--host 127.0.0.1] [--port 8765] [--no-browser] [--public-url URL]
                     dashboard + API + background services; one instance per data dir (lock file); Ctrl+C or
                     SIGTERM shut it down cleanly (jobs' subprocesses stop, detached live recorders keep going)
    reset-login [--yes]   forget password, 2FA and sessions so the dashboard's setup wizard runs again (the way back
                     in when both password and 2FA are lost: shell access on this PC is the proof of ownership)
    doctor           health.system_report() in German; exit 1 when something required is missing
    import-yaml      import creators/streams/prefs/.env from the repo files again (only adds what's missing)
"""
import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import urlsplit

from . import __version__, db, paths

COMMANDS = ("serve", "reset-login", "doctor", "import-yaml")
LOG_FILE = "server.log"


def _serve_parser(prog):
    p = argparse.ArgumentParser(prog=prog, description="GTA Clipper – Automatik: lokaler Server mit Dashboard.")
    p.add_argument("--host", default="127.0.0.1",
                   help="Adresse, an die der Server bindet (Standard 127.0.0.1 = nur dieser PC; 0.0.0.0 = ganzes Netz)")
    p.add_argument("--port", type=int, default=8765, help="Port (Standard 8765)")
    p.add_argument("--no-browser", action="store_true", help="den Browser nicht automatisch öffnen")
    p.add_argument("--public-url", help="eigene Adresse, unter der das Dashboard erreichbar ist (z. B. hinter einem "
                                        "Reverse-Proxy mit HTTPS)")
    return p


# ---------------------------------------------------------------- serve

class _AccessFilter(logging.Filter):
    """Keep the access log useful and free of secrets: no query strings (the OAuth callback carries a code), and
    only changes and errors (the dashboard polls GETs every few seconds)."""

    def filter(self, record):
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, target, status = args[1], str(args[2]), args[4]
        if method in ("GET", "HEAD") and isinstance(status, int) and status < 400:
            return False
        record.args = (*args[:2], target.split("?", 1)[0], *args[3:])
        return True


def _setup_logging():
    d = paths.logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(d / LOG_FILE, maxBytes=5_000_000, backupCount=3,
                                                        encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [file_handler, console]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").addFilter(_AccessFilter())
    return d / LOG_FILE


def _lock(url):
    """An exclusive lock on <data>/server.lock for the process lifetime; (file, None) or (None, holder info)."""
    f = open(paths.ensure_data_dir() / "server.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        try:
            info = json.loads(f.read() or "{}")
        except ValueError:
            info = {}
        f.close()
        return None, info
    f.seek(0)
    f.truncate()
    f.write(json.dumps({"pid": os.getpid(), "url": url, "started_at": time.time()}))
    f.flush()
    return f, None


def _port_free(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _wsl():
    return "microsoft" in platform.release().lower()


def _display_likely():
    if sys.platform == "darwin":
        return True
    if _wsl():
        return bool(shutil.which("wslview") or shutil.which("explorer.exe"))
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_browser_when_up(url, host, port):
    probe = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host.strip("[]")

    def run():
        for _ in range(150):
            try:
                with socket.create_connection((probe, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            return
        try:
            if _wsl():
                opener = shutil.which("wslview") or shutil.which("explorer.exe")
                subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=run, name="open-browser", daemon=True).start()


def serve(argv):
    args = _serve_parser("python -m automate").parse_args(argv)
    import uvicorn
    from .server import ServerConfig, create_app
    if args.public_url:
        u = urlsplit(args.public_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            print(f"--public-url braucht eine vollständige Adresse wie https://clips.example.de (bekommen: "
                  f"{args.public_url})", file=sys.stderr)
            return 2
    if not 1 <= args.port <= 65535:
        print("--port muss zwischen 1 und 65535 liegen.", file=sys.stderr)
        return 2
    cfg = ServerConfig(host=args.host, port=args.port, public_url=args.public_url)
    url = cfg.base_url()
    lock, holder = _lock(url)
    if lock is None:
        print(f"Der Server läuft schon für diesen Datenordner ({paths.data_dir()})"
              + (f" – Prozess {holder.get('pid')}, {holder.get('url')}" if holder.get("pid") else "")
              + ". Öffne das Dashboard dort oder beende den anderen Server zuerst (Strg+C in seinem Terminal).",
              file=sys.stderr)
        return 1
    if not _port_free(args.host, args.port):
        print(f"Port {args.port} ist schon belegt (ein anderes Programm?). Starte mit einem anderen Port, z. B. "
              f"„uv run python -m automate --port {args.port + 1}“.", file=sys.stderr)
        return 1
    log_file = _setup_logging()
    print(f"\nGTA Clipper – Automatik {__version__}\n"
          f"  Dashboard: {url}\n  Daten:     {paths.data_dir()}\n  Log:       {log_file}\n"
          f"  Beenden mit Strg+C.\n")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        others = ", ".join(f"http://{h}" for h in cfg.allowed_hosts()
                           if h.rsplit(":", 1)[0].strip("[]") not in ("localhost", "127.0.0.1", "::1"))
        print("ACHTUNG: Der Server ist im Netzwerk erreichbar (--host " + args.host + "). Jeder im selben WLAN/LAN "
              "kann die Anmeldeseite öffnen; geschützt ist alles nur durch Passwort und 2FA, und die Verbindung ist "
              "unverschlüsselt (HTTP). Nur im eigenen, vertrauenswürdigen Heimnetz verwenden – nie über eine "
              "Portweiterleitung ins Internet." + (f"\n  Im Netz erreichbar unter: {others}" if others else "") + "\n",
              file=sys.stderr)
    if not args.no_browser and _display_likely():
        _open_browser_when_up(url, args.host, args.port)
    config = uvicorn.Config(create_app(config=cfg), host=args.host, port=args.port, log_config=None,
                            proxy_headers=True, forwarded_allow_ips="127.0.0.1", server_header=False,
                            timeout_graceful_shutdown=10, lifespan="on")
    # uvicorn shuts down gracefully on SIGINT/SIGTERM, then raises the signal again: end quietly instead of with a
    # KeyboardInterrupt traceback (Ctrl+C) or a kill by the default SIGTERM action
    previous = signal.signal(signal.SIGTERM, _stopped)
    try:
        uvicorn.Server(config).run()
    except (KeyboardInterrupt, _Stopped):
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        db.close()
        lock.close()
    print("Server beendet.")
    return 0


class _Stopped(Exception):
    pass


def _stopped(signum, frame):
    raise _Stopped()


# ---------------------------------------------------------------- maintenance

def reset_login(argv):
    p = argparse.ArgumentParser(prog="python -m automate reset-login",
                                description="Login zurücksetzen (Passwort, 2FA, Sitzungen).")
    p.add_argument("--yes", action="store_true", help="ohne Rückfrage")
    args = p.parse_args(argv)
    from . import store
    user = store.get_user()
    if user is None or not user.get("password_hash"):
        print("Es gibt kein eingerichtetes Konto – beim nächsten Öffnen des Dashboards startet die Einrichtung.")
        return 0
    print(f"Das setzt den Login „{user['username']}“ zurück: Passwort, 2FA und Wiederherstellungscodes werden "
          f"gelöscht, alle Sitzungen abgemeldet. Quellen, Videos, Schlüssel und Einstellungen bleiben erhalten. "
          f"Danach legst du im Dashboard ein neues Konto mit neuer 2FA an.")
    if not args.yes:
        try:
            answer = input("Zum Bestätigen RESET eingeben: ")
        except EOFError:
            answer = ""
        if answer.strip() != "RESET":
            print("Abgebrochen – nichts geändert.")
            return 1
    with store.transaction():
        store.delete_user(user["id"])  # its sessions go with it
        store.set_meta("setup.pending", None)
        store.set_meta("totp.pending", None)
    print("Login zurückgesetzt. Öffne das Dashboard und lege das Konto neu an (ein laufender Server muss dafür nicht "
          "neu starten).")
    return 0


def doctor(argv):
    argparse.ArgumentParser(prog="python -m automate doctor", description="Systemcheck").parse_args(argv)
    from . import store
    try:
        from . import health
        report = health.system_report()
    except Exception as e:
        print(f"Der Systemcheck ist nicht verfügbar (automate.health: {e}).", file=sys.stderr)
        return 1
    print(f"GTA Clipper – Automatik {report.get('version', __version__)} · Python {report.get('python')} · "
          f"{report.get('platform')}")
    problems = []
    disk = report.get("disk") or {}
    if disk.get("free_gb") is not None:
        low = disk.get("min_free_gb") is not None and disk["free_gb"] < disk["min_free_gb"]
        print(f"Speicher: {disk['free_gb']:g} GB frei von {disk.get('total_gb', 0):g} GB"
              + (f" – unter {disk['min_free_gb']:g} GB!" if low else ""))
        if low:
            problems.append("zu wenig freier Speicher")
    print("Werkzeuge:")
    for t in report.get("tools") or []:
        required = t.get("required", True)
        mark = "✓" if t.get("ok") else ("✗" if required else "–")
        extra = f" {t['version']}" if t.get("ok") and t.get("version") else ("" if t.get("ok") else
                                                                             " fehlt" + ("" if required else
                                                                                         " (optional)"))
        print(f"  {mark} {t.get('name')}{extra}" + (f" – {t['purpose']}" if t.get("purpose") else ""))
        if not t.get("ok") and t.get("hint"):
            print(f"      → {t['hint']}")
        if not t.get("ok") and required:
            problems.append(t.get("name"))
    print("Schlüssel:")
    for name, label, required in (("anthropic_api_key", "Anthropic", True), ("fish_api_key", "Fish Audio", False)):
        info = store.secret_info(name)
        print(f"  {'✓' if info['set'] else ('✗' if required else '–')} {label}: "
              + (info["masked"] if info["set"] else "nicht eingetragen" + ("" if required else " (optional)")))
    print("Pfade:")
    for k, v in (report.get("paths") or {}).items():
        print(f"  {k}: {v}")
    if problems:
        print(f"\nEs fehlt etwas: {', '.join(map(str, problems))}. Fehlende Werkzeuge installiert "
              f"„uv run python -m clipper setup --install“.")
        return 1
    print("\nAlles Nötige ist da.")
    return 0


def import_yaml(argv):
    argparse.ArgumentParser(prog="python -m automate import-yaml",
                            description="Creator, Streams, Prefs und .env aus dem Repo übernehmen").parse_args(argv)
    from . import importer, runtime
    s = importer.run(force=True)
    runtime.materialize()
    print("Übernommen:")
    print(f"  Profile: {', '.join(s['profiles']) or '–'}")
    print(f"  Creator: {', '.join(f'{p}: {n}' for p, n in s['creators'].items()) or '–'}")
    print(f"  Streams: {', '.join(f'{p}: {n}' for p, n in s['streams'].items()) or '–'}")
    print(f"  Einstellungen: {', '.join(s['settings']) or '–'}")
    print(f"  Schlüssel: {', '.join(s['secrets']) or '–'}")
    for note in s["skipped"]:
        print(f"  übersprungen: {note}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv and argv[0] in COMMANDS else "serve"
    try:
        return {"serve": serve, "reset-login": reset_login, "doctor": doctor, "import-yaml": import_yaml}[cmd](argv)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
