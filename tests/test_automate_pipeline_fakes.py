"""Fakes for the orchestrator tests (no tests in here): a stand-in for runtime.run_clipper / run_bridge that plays
each clipper command on the temp jobs/exports dirs, fake automate.agent and automate.youtube modules, and the
temp-data-dir base class. Imported by tests/test_automate_{pipeline,scheduler,health}.py."""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from unittest import mock

from automate import db, paths, runtime, store, worker
from automate.runtime import ProcResult

KEY = "sk-ant-test-0123456789abcdef"


def make_pick(n, start=10.0):
    s = start + 40 * n
    return {"start": s, "end": s + 20, "clip_type": "reaction", "hook_style": "box", "caption_style": "boxed",
            "hooks": [f"Why did Kai *run* {n}?", f"Kai NEVER saw {n} coming", f"3 things Kai missed {n}",
                      f"Nobody expected round {n}", f"Kai was not ready {n}"],
            "titles": [f"Why Did Kai Run {n} In GTA 6?", f"Kai NEVER Saw {n} Coming In GTA 6",
                       f"3 Things Kai Missed {n} In GTA 6", f"Nobody Expected Round {n} In GTA 6",
                       f"Kai Was Not Ready {n} In GTA 6"],
            "description": f"Kai in GTA 6 ({n})", "hashtags": ["#gta6", "#kaicenat"]}


def write_live_chunk(cj, profile="gta6", source=True, analyzed=True):
    jd = paths.jobs_dir() / cj
    (jd / "review").mkdir(parents=True, exist_ok=True)
    meta = {"id": cj, "url": "https://www.twitch.tv/xqc", "title": "GTA 6 RP day 1", "channel": "xQc",
            "duration": 1800, "offset": 0.0, "live": True, "recorded_at": time.time() - 3600, "profile": profile}
    (jd / "job.json").write_text(json.dumps(meta))
    (jd / "live_ready").write_text("x")
    if source:
        (jd / "source.mp4").write_bytes(b"mp4")
    (jd / "transcript.json").write_text(json.dumps({"words": []}))
    if analyzed:
        (jd / "candidates.json").write_text(json.dumps({"recommended": 2, "candidates": [{"id": 1}, {"id": 2}],
                                                        "scenarios": [], "clip_type": "reaction"}))
    return jd


class FakeClipper:
    """runtime.run_clipper / run_bridge for tests: records every call and simulates its files and output."""

    def __init__(self):
        self.calls, self.bridge_calls = [], []
        self.lock = threading.Lock()
        self.fail = {}            # command ("fetch", "live done", ...) -> [spec dict | None] consumed in order
        self.block = {}           # command -> Event: the command waits until it is set or canceled
        self.reached = {}         # command -> Event set when the command starts
        self.check_fails = []     # one bool per `check` bridge call
        self.recommended = 2
        self.duration = 1200
        self.watch = {"videos": [], "skipped_off_topic": 0, "errors": []}
        self.live_ticks = []
        self.system = {"is_mac": False, "asr": "parakeet",
                       "which": {"ffmpeg": True, "ffprobe": True, "yt-dlp": True, "demucs": False,
                                 "espeak-ng": True, "whisperkit-cli": False, "whisper-cli": False, "say": False},
                       "versions": {"ffmpeg": "6.1.1", "ffprobe": "6.1.1", "yt-dlp": "2026.10.01"},
                       "modules_missing": [], "model_missing": False, "report": "ok       ffmpeg"}
        self.render_sleep = 0.0
        self.render_active = self.render_max = 0
        self.seen_audio = None

    def args_of(self, command):
        key = command.split()
        return [c for c in self.calls if c[:len(key)] == key]

    # ------------------------------------------------------------ clipper
    def run_clipper(self, args, profile, *, timeout=None, on_line=None, cancel=None, module="clipper", argv=None,
                    input=None, env=None, overlay=None):
        args = [str(a) for a in args]
        with self.lock:
            self.calls.append(args)
        key = " ".join(args[:2]) if args[0] in ("live", "dub") else args[0]
        if key in self.reached:
            self.reached[key].set()
        if key in self.block:
            while not self.block[key].is_set():
                if cancel is not None and cancel.is_set():
                    return ProcResult(-15, "", "terminated", 0.1, canceled=True)
                time.sleep(0.01)
        if self.fail.get(key):
            spec = self.fail[key].pop(0)
            if spec is not None:
                return ProcResult(spec.get("rc", 1), spec.get("stdout", ""), spec.get("stderr", "ERROR: boom"), 0.1)
        emit = on_line or (lambda line: None)
        return getattr(self, "_" + key.replace(" ", "_"))(args, emit, profile)

    def _fetch(self, args, emit, profile):
        vid = args[1]
        jd = paths.jobs_dir() / vid
        jd.mkdir(parents=True, exist_ok=True)
        section = args[args.index("--section") + 1] if "--section" in args else None
        meta = {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": f"GTA 6 video {vid}",
                "channel": "Kai Cenat", "duration": self.duration, "offset": 0.0}
        lines = ["[youtube] Extracting URL"] + [f"[download]  {p:.1f}% of 1.00GiB at 2.00MiB/s ETA 0{p // 20:.0f}:00"
                                               for p in range(0, 100, 5)] + ["[Merger] Merging formats"]
        for line in lines:
            emit(line)
        (jd / "source.mp4").write_bytes(b"mp4")
        (jd / "source.info.json").write_text(json.dumps({"title": meta["title"], "channel": meta["channel"]}))
        (jd / "job.json").write_text(json.dumps(meta))
        return ProcResult(0, "\n".join(lines) + "\n" + json.dumps({"job": str(jd), **meta, "section": section},
                                                                  indent=2) + "\n", "", 0.1)

    def _analyze(self, args, emit, profile):
        jd = paths.jobs_dir() / args[1]
        self.seen_audio = (jd / "audio.wav").exists()
        emit("[analyze] transcribing locally (parakeet) ...")
        (jd / "audio.wav").write_bytes(b"wav")
        (jd / "transcript.json").write_text(json.dumps({"words": [{"w": "hi", "s": 1.0, "e": 1.2}]}))
        emit("[analyze] measuring loudness ...")
        (jd / "review").mkdir(exist_ok=True)
        (jd / "review" / "cand_01.jpg").write_bytes(b"jpg")
        cands = [{"id": i, "start": 10 + 40 * i, "end": 30 + 40 * i, "tier": "strong"} for i in range(3)]
        (jd / "candidates.json").write_text(json.dumps({"recommended": self.recommended, "candidates": cands,
                                                        "scenarios": [], "clip_type": "reaction",
                                                        "profile": "reaction"}))
        (jd / "candidates.md").write_text("# candidates\n")
        return ProcResult(0, "# Clip candidates\n", "", 0.1)

    def _manifest(self, folder, pick, lang=None):
        entries = []
        for i, (h, t) in enumerate(zip(pick["hooks"], pick["titles"])):
            mp4 = folder / f"{h.replace('*', '').replace('?', '')}.mp4"
            mp4.write_bytes(b"mp4")
            entries.append({"file": str(mp4), "rank": i, "hook": h, "hook_type": "question", "arm": "hook",
                            "title": t, "description": pick.get("description", "") + "\n\nClip from Kai",
                            "hashtags": pick.get("hashtags") or [], "duration": 20.0, "predicted": 1.3 - i / 10,
                            "source": "https://www.youtube.com/watch?v=x&t=10s",
                            "sources": ["https://www.youtube.com/watch?v=x&t=10s"],
                            **({"lang": lang} if lang else {})})
        (folder / "upload.json").write_text(json.dumps(entries))
        return entries

    def _render(self, args, emit, profile):
        pick = json.loads(Path(args[args.index("--picks") + 1]).read_text())[0]
        out = Path(args[args.index("--out") + 1])
        with self.lock:
            self.render_active += 1
            self.render_max = max(self.render_max, self.render_active)
        try:
            time.sleep(self.render_sleep)
            out.mkdir(parents=True, exist_ok=True)
            with self.lock:
                n = max([int(p.name.split()[1]) for p in out.glob("video *")], default=0) + 1
                folder = out / f"video {n}"
                folder.mkdir()
            emit(f"[render] video {n} (1 of 1): separating voices + diarizing ...")
            for h in pick["hooks"]:
                emit(f"[render]   video {n}/{h}.mp4")
            self._manifest(folder, pick)
            return ProcResult(0, f"[render] video {n}\n" + json.dumps([{"video": n, "folder": str(folder)}],
                                                                       indent=2), "", 0.1)
        finally:
            with self.lock:
                self.render_active -= 1

    def _dub_segments(self, args, emit, profile):
        return ProcResult(0, json.dumps({"segments": [{"i": 0, "s": 0.0, "e": 2.0, "text": "run!", "spk": 0}],
                                         "english": {}, "template": {}}, indent=2), "[dub] separating ...", 0.1)

    def _dub_render(self, args, emit, profile):
        lang = args[args.index("--lang") + 1]
        tr = json.loads(Path(args[args.index("--translation") + 1]).read_text())
        out = paths.exports_dir() / f"{profile}-{lang}" / date.today().isoformat()
        out.mkdir(parents=True, exist_ok=True)
        n = max([int(p.name.split()[1]) for p in out.glob("video *")], default=0) + 1
        folder = out / f"video {n}"
        folder.mkdir()
        emit("[dub] line 1/1 (speaker 0): ¡corre!")
        self._manifest(folder, {"hooks": tr["hooks"], "titles": tr["titles"], "description": tr["description"]}, lang)
        return ProcResult(0, "[dub] isolating\n" + json.dumps({"video": n, "folder": str(folder), "lang": lang},
                                                              indent=2), "", 0.1)

    def _live_done(self, args, emit, profile):
        for cj in args[2:]:
            jd = paths.jobs_dir() / cj
            if jd.is_dir():
                (jd / "live_done").write_text("x")
        return ProcResult(0, json.dumps(args[2:]), "", 0.1)

    def _cleanup(self, args, emit, profile):
        return ProcResult(0, json.dumps({"dry_run": "--dry-run" in args, "files": 1, "gb": 1.5, "jobs": ["old"]},
                                        indent=2), "", 0.1)

    def _watch(self, args, emit, profile):
        return ProcResult(0, json.dumps(self.watch, indent=2), "", 0.1)

    # ------------------------------------------------------------ bridge
    def run_bridge(self, op, payload, profile, *, timeout=600, on_line=None, cancel=None):
        with self.lock:
            self.bridge_calls.append((op, payload))
        if op == "check":
            fails = self.check_fails.pop(0) if self.check_fails else False
            report = {"video 1": {"hooks": {"Wait for it...": {"fails": ["vague hook"]}}}} if fails else \
                {"video 1": {"playbook": ["a note"]}}
            return {"report": report, "fails": fails}
        if op == "rank":
            keep = payload.get("keep")
            out, ranking = [], []
            for n, p in enumerate(payload["picks"], 1):
                k = len(p["hooks"]) if keep == "auto" else int(keep)
                out.append({**p, "hooks": p["hooks"][:k], "titles": p["titles"][:k]})
                ranking.append({"video": n, "keep": k, "ok": True, "kept": [{"index": i, "hook": h, "type": "question"}
                                                                           for i, h in enumerate(p["hooks"][:k])],
                                "ranking": [{"index": i, "hook": h, "type": "question", "x": 1.2, "low": 1.0,
                                             "high": 1.4, "wins": 0.5, "why": [], "check": [], "kept": i < k}
                                            for i, h in enumerate(p["hooks"])],
                                "keep_line": "keep", "titles_dropped": []})
            return {"picks": out, "ranking": ranking}
        if op == "probe_video":
            url = payload["url"]
            return {"ok": True, "id": url[-11:], "title": "Probed GTA 6 title", "channel": "Probe channel",
                    "duration": self.duration, "live_status": "not_live", "error": None}
        if op == "live_tick":
            if not self.live_ticks:
                return {"started": [], "ready": [], "transcribing": [], "cleaned": {"files": 0, "gb": 0},
                        "streams": [], "issues": {}, "stalled": [], "restarted": [], "recorders": 0}
            item = self.live_ticks.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if op == "system_tools":
            if isinstance(self.system, Exception):
                raise self.system
            return self.system
        raise runtime.BridgeError(f"unknown op {op!r}")


def make_agent():
    mod = types.ModuleType("automate.agent")

    class MissingApiKey(Exception):
        pass

    class BudgetExceeded(Exception):
        pass

    class AgentError(Exception):
        def __init__(self, msg, retryable=False):
            super().__init__(msg)
            self.retryable = retryable

    @dataclass
    class Selection:
        picks: list
        reasons: list
        click_triggers: list
        no_clip_reason: str = None
        check_report: dict = field(default_factory=dict)
        turns: int = 1
        usage: dict = field(default_factory=dict)

    mod.MissingApiKey, mod.BudgetExceeded, mod.AgentError, mod.Selection = (MissingApiKey, BudgetExceeded,
                                                                            AgentError, Selection)
    mod.calls, mod.script, mod.dub_calls = [], [], []

    def select_picks(job_id, *, max_picks, avoid_openers=(), log=None, cancel=None):
        job = store.get_job(job_id)
        mod.calls.append({"job_id": job_id, "max_picks": max_picks, "avoid_openers": list(avoid_openers),
                          "params": dict(job.get("params") or {})})
        if log:
            log("lese die Kandidaten")
        item = mod.script.pop(0) if mod.script else None
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(job_id, max_picks, cancel)
        n = max_picks if item is None else item
        picks = [make_pick(i + 10 * job_id) for i in range(n)]
        return Selection(picks, [f"why {i}" for i in range(n)], [f"curiosity gap {i}" for i in range(n)],
                         None if picks else "Nichts lohnt sich", {}, 3, {"input_tokens": 10})

    def translate_dub(job_id, pick_idx, lang, segments_report, *, log=None, cancel=None):
        mod.dub_calls.append((job_id, pick_idx, lang))
        return {"lang": lang, "lines": ["¡corre!"], "hooks": ["¿Por qué *corre*?", "Kai NUNCA lo vio", "3 cosas"],
                "titles": ["¿Por qué corre Kai en GTA 6?"], "description": "Kai en GTA 6"}

    mod.select_picks, mod.translate_dub = select_picks, translate_dub
    return mod


def make_youtube():
    mod = types.ModuleType("automate.youtube")

    class YouTubeError(Exception):
        def __init__(self, message, *, retryable=False, code="youtube_error"):
            super().__init__(message)
            self.retryable, self.code = retryable, code

    mod.YouTubeError, mod.script, mod.calls = YouTubeError, [], []

    def upload_variant(variant_id, account_id, privacy, *, title=None, description=None, on_progress=None,
                       cancel=None):
        mod.calls.append((variant_id, account_id, privacy))
        if on_progress:
            on_progress(50, 100)
        item = mod.script.pop(0) if mod.script else None
        if isinstance(item, BaseException):
            raise item
        return {"video_id": "yt123", "url": "https://youtu.be/yt123"}

    mod.upload_variant = upload_variant
    return mod


class PipelineCase(unittest.TestCase):
    """Temp data/jobs/exports dirs, the gta6 profile, an Anthropic key, and every outside layer faked."""

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
        store.set_secret("anthropic_api_key", KEY)
        self.clip = FakeClipper()
        self.agent, self.yt = make_agent(), make_youtube()
        for patcher in (mock.patch.object(runtime, "run_clipper", self.clip.run_clipper),
                        mock.patch.object(runtime, "run_bridge", self.clip.run_bridge),
                        mock.patch.object(worker, "reap_orphans", lambda module, args: []),
                        mock.patch.dict(sys.modules, {"automate.agent": self.agent, "automate.youtube": self.yt})):
            patcher.start()
            self.addCleanup(patcher.stop)
        from automate import pipeline
        pipeline._cfg_cache.clear()

    def drive(self, kinds=None, limit=100, now=None):
        """Run queued jobs in this thread until none is claimable; returns the job ids run."""
        ran = []
        for _ in range(limit):
            jid = worker.process_next("test-worker", kinds, now=now)
            if jid is None:
                return ran
            ran.append(jid)
        self.fail(f"the queue never drained: {ran[-10:]}")

    def job(self, jid):
        return store.get_job(jid)

    def picks(self, jid):
        return sorted(store.list_picks(job_id=jid)[0], key=lambda p: p["idx"])
