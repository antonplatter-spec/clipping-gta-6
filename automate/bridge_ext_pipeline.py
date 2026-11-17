"""Bridge ops the orchestrator needs (registered into automate.bridge.OPS; run in the clipper subprocess env).

  system_tools  {} -> {is_mac, asr, which: {tool: bool}, versions: {tool: str | None}, modules_missing: [import
                names], model_missing, report}   clipper.setup.missing() + setup.run(dry_run=True) (lists, installs
                nothing; the lines about installing agent commands are left out)
  probe_video   {url} -> {ok, id, title, channel, duration, live_status, availability, error}   one `yt-dlp -J`
                without download: duration for the daily_max_minutes section, a live stream or a private video
                before anything is downloaded
  live_tick     {chunk, health=true, restart_stalled=true} -> {started, ready, transcribing, cleaned, streams,
                issues, stalled, restarted, recorders}
                exactly `clipper live watch --chunk N` (live.watch + cleanup) and `clipper live health`, sharing ONE
                probe of the streams (each probe is a yt-dlp call per stream); `streams` = live.status() rows with
                `recording` re-read after the watch. A stalled recorder is replaced without a gap (live.restart) at
                most once per RESTART_COOLDOWN_S. `probe_error`: when no stream came back live, one more probe asks
                why; a network / bot-check / proxy failure is returned here (clipper's probe reads every failure as
                "offline", which would hide that the PC can't reach the platforms at all), else None.
"""
import json
import shutil
import subprocess
import time
from importlib import metadata

from automate import bridge

RESTART_COOLDOWN_S = 30 * 60
UNREACHABLE = ("unable to connect", "connection refused", "failed to resolve", "name or service not known",
               "temporary failure in name resolution", "timed out", "proxyerror", "tunnel connection failed",
               "network is unreachable", "sign in to confirm", "not a bot", "http error 429", "http error 5",
               "certificate verify failed", "unable to download")
TOOLS = ("ffmpeg", "ffprobe", "yt-dlp", "whisper-cli", "whisperkit-cli", "demucs", "espeak-ng", "say")
VERSION_ARGS = {"ffmpeg": ["-version"], "ffprobe": ["-version"], "yt-dlp": ["--version"], "espeak-ng": ["--version"]}


def _version(tool):
    args = VERSION_ARGS.get(tool)
    if not args or not shutil.which(tool):
        return None
    try:
        out = subprocess.run([tool, *args], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.splitlines()[0] if out else ""
    if tool in ("ffmpeg", "ffprobe") and " version " in first:
        return first.split(" version ", 1)[1].split()[0]
    if tool == "espeak-ng" and ":" in first:
        return first.split(":", 1)[1].split()[0]
    return first[:40] or None


def _pkg_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


@bridge.op("system_tools")
def system_tools(payload, cfg):
    from clipper import config, setup
    tools, mods, demucs, model = setup.missing()
    report = setup.run(False, True)
    skip = ("would install", "up to date", "installed ")
    report = "\n".join(line for line in report.splitlines() if not line.startswith(skip))
    versions = {t: _version(t) for t in TOOLS}
    versions["onnx_asr"] = _pkg_version("onnx-asr")
    versions["demucs"] = _pkg_version("demucs")
    return {"is_mac": config.IS_MAC, "asr": cfg.get("asr", "parakeet"),
            "which": {t: bool(shutil.which(t)) for t in TOOLS}, "versions": versions,
            "missing_tools": tools, "modules_missing": mods, "demucs_missing": demucs, "model_missing": bool(model),
            "report": report}


@bridge.op("probe_video")
def probe_video(payload, cfg):
    url = str(payload.get("url") or "").strip()
    if not url:
        raise ValueError("url fehlt")
    try:
        res = subprocess.run(["yt-dlp", "-J", "--skip-download", "--no-playlist", "--no-warnings", "--", url],
                             capture_output=True, text=True, timeout=150)
    except FileNotFoundError:
        return {"ok": False, "error": "No such file or directory: 'yt-dlp'"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "yt-dlp: timed out"}
    if res.returncode != 0 or not res.stdout.strip():
        lines = [x for x in res.stderr.strip().splitlines() if x.strip()]
        return {"ok": False, "error": lines[-1] if lines else f"yt-dlp exited with {res.returncode}"}
    info = json.loads(res.stdout)
    return {"ok": True, "id": info.get("id"), "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"), "duration": info.get("duration"),
            "live_status": info.get("live_status"), "is_live": bool(info.get("is_live")),
            "availability": info.get("availability"), "error": None}


def _stalled(live, now):
    """Names of streams whose recorder writes no video (the rule of live.health's `stalled` issue)."""
    out = []
    for s in live.status(None):
        d = live.WORK / live.slug(s["name"])
        if not (s["live"] and s["recording"] and (d / "pid").exists()):
            continue
        last = max((f.stat().st_mtime for f in d.glob("*.ts")), default=(d / "pid").stat().st_mtime)
        if now - last > live.STALL_MINUTES * 60:
            out.append(s["name"])
    return out


def _unreachable(url):
    """The reason a platform can't be reached at all (yt-dlp's last error line), or None (offline is no error)."""
    try:
        res = subprocess.run(["yt-dlp", "-J", "--no-warnings", "--skip-download", "--no-playlist", "--", url],
                             capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "yt-dlp: Zeitüberschreitung"
    if res.returncode == 0:
        return None
    lines = [x.strip() for x in res.stderr.splitlines() if x.strip()]
    last = lines[-1] if lines else ""
    return last[:300] if any(k in last.lower() for k in UNREACHABLE) else None


@bridge.op("live_tick")
def live_tick(payload, cfg):
    from clipper import cleanup, live
    chunk = int(payload.get("chunk") or 30)
    seen = {}
    probe_status = live.status

    def status(c):
        """The first call probes the streams; later ones only re-read which recorders run."""
        if "streams" not in seen:
            seen["streams"] = probe_status(c)
        else:
            seen["streams"] = [{**s, "recording": live.recorder_running(s["name"])} for s in seen["streams"]]
        return seen["streams"]

    live.status = status
    try:
        started = live.watch(cfg, chunk)
        cleaned = cleanup.run(cfg)
        issues = live.health(cfg) if payload.get("health", True) else {}
        now = time.time()
        stalled = _stalled(live, now) if "stalled" in issues else []
        streams = status(cfg)
    finally:
        live.status = probe_status
    restarted = []
    if payload.get("restart_stalled", True):
        for name in stalled:
            mark = live.WORK / live.slug(name) / "automate-restarted"
            try:
                last = float(mark.read_text())
            except (OSError, ValueError):
                last = 0.0
            if now - last < RESTART_COOLDOWN_S:
                continue
            try:
                live.restart(name, chunk)
                mark.write_text(str(now))
                restarted.append(name)
            except SystemExit:
                continue
    ready = live.ready_jobs(True)
    probe_error = _unreachable(streams[0]["url"]) if streams and not any(s.get("live") for s in streams) else None
    return {"probe_error": probe_error, "started": started, "ready": ready, "transcribing": sorted(set(live.ready_jobs()) - set(ready)),
            "cleaned": {"files": cleaned["files"], "gb": cleaned["gb"]},
            "streams": [{"name": s.get("name"), "url": s.get("url"), "live": bool(s.get("live")),
                         "on_topic": bool(s.get("on_topic")), "recording": bool(s.get("recording")),
                         "title": s.get("title"), "priority": s.get("priority", 100),
                         "topic_only": live.topic_only(s)} for s in streams],
            "issues": issues, "stalled": stalled, "restarted": restarted, "recorders": live.alive_recorders()}
