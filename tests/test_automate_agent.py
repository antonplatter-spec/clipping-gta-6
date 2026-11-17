"""automate.agent / agent_tools / prompts: the Claude select loop against a scripted fake SDK client (no network),
the tools against a small fixture job, pick validation, the dub translation and the key test.

The fake client also simulates prompt caching (an entry per breakpoint, read when a later request starts with the
same bytes), so the tests check that the prefix stays byte-stable within a job and across jobs of a profile."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2

from automate import agent, agent_tools as T, costs, db, prompts, runtime, store

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
KEY = "sk-ant-api03-" + "x" * 40
JOB = "agent-fixture"

W = lambda w, s, e: {"w": w, "s": s, "e": e}  # noqa: E731
WORDS = [W("um", 0.5, 0.8), W("so", 0.9, 1.1), W("chat", 1.2, 1.5),
         W("Yo", 10.0, 10.3), W("look", 10.4, 10.7), W("at", 10.7, 10.8), W("this", 10.8, 11.0), W("car!", 11.0, 11.6),
         W("Let", 14.0, 14.2), W("me", 14.2, 14.3), W("show", 14.3, 14.6), W("you", 14.6, 14.8), W("my", 14.8, 15.0),
         W("new", 15.0, 15.2), W("getaway", 15.2, 15.7), W("car.", 15.7, 16.1),
         W("Oh", 40.0, 40.2), W("my", 40.2, 40.4), W("god", 40.4, 40.8), W("he's", 40.9, 41.1), W("behind", 41.1, 41.5),
         W("me!", 41.5, 41.9), W("Trever", 45.0, 45.4), W("just", 45.4, 45.6), W("died!", 45.6, 46.2)]


def write_transcript(path, words):
    """The repo's transcript.json layout: one word per line."""
    path.write_text('{"model": "test", "events": [], "words": [\n' + ",\n".join(json.dumps(w) for w in words)
                    + "\n]}\n", encoding="utf-8")


def jpeg(path, size=(1440, 540), color=(40, 90, 160)):
    from PIL import Image
    Image.new("RGB", size, color).save(path, "JPEG", quality=70)


def make_job(root, name=JOB, video=False, picks=None):
    d = root / name
    (d / "review").mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps({"id": name, "url": f"https://www.youtube.com/watch?v={name}",
                                            "title": "Kai Cenat plays GTA 6 for the first time",
                                            "channel": "Kai Cenat Live", "duration": 7200, "offset": 0.0}))
    write_transcript(d / "transcript.json", WORDS)
    cands = [{"id": 1, "start": 10.0, "end": 31.0, "duration": 21.0, "score": 71, "tier": "strong", "peak": 11,
              "timestamp": "0:10-0:31", "link": "x", "signals": {"jump_lu": 9.1, "peak_lufs": -12.0,
                                                                  "typical_lufs": -24.0, "most_replayed": None,
                                                                  "hype_hits": ["oh my god"]},
              "hook": "Yo look at this car!", "first_line": "Yo look at this car!", "opens_with": "action cold open",
              "transcript": "Yo look at this car! Let me show you my new getaway car.",
              "contact_sheet": "review/cand_01.jpg"},
             {"id": 2, "start": 38.0, "end": 50.0, "duration": 12.0, "score": 40, "tier": "maybe", "peak": 41,
              "timestamp": "0:38-0:50", "link": "x", "signals": {"jump_lu": 4.0, "peak_lufs": -15.0,
                                                                  "typical_lufs": -24.0, "most_replayed": None,
                                                                  "hype_hits": []},
              "hook": "Oh my god he's behind me!", "first_line": "Oh my god he's behind me!",
              "opens_with": "in-scene cold open", "transcript": "Oh my god he's behind me! Trever just died!",
              "contact_sheet": "review/cand_02.jpg"}]
    scen = [{"id": "S1", "start": 14.0, "end": 30.0, "duration": 16.0, "cue": "let me show you",
             "timestamp": "0:14-0:30", "link": "x", "transcript": "Let me show you my new getaway car.",
             "contact_sheet": "review/scen_01.jpg"}]
    (d / "candidates.json").write_text(json.dumps({
        "video": {"id": name, "title": "Kai Cenat plays GTA 6 for the first time", "channel": "Kai Cenat Live",
                  "url": f"https://www.youtube.com/watch?v={name}"},
        "recommended": 2, "clip_type": "reaction", "profile": "reaction", "clip_length": {"target": 22, "min": 9,
                                                                                            "max": 35},
        "has_heatmap": False, "hook_plan": "reaction: ...", "ad_breaks": [], "candidates": cands, "scenarios": scen}))
    (d / "candidates.md").write_text("# Clip candidates: Kai Cenat plays GTA 6 for the first time\n\n"
                                     "**Worth exporting: 2 video(s)** · content: reaction\n\n"
                                     "## #1 · strong · 0:10-0:31\n"
                                     "> Yo look at this car! Let me show you my new getaway car.\n", encoding="utf-8")
    for n in ("cand_01", "cand_02", "scen_01"):
        jpeg(d / "review" / f"{n}.jpg")
    if picks is not None:
        (d / "picks.json").write_text(json.dumps(picks))
    if video and FFMPEG:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=5",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "60", "-c:v", "libx264", "-preset",
                        "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "source.mp4")],
                       check=True)
    return d


def pick(**over):
    p = {"start": 10.0, "end": 31.0, "clip_type": "reaction",
         "layout": {"type": "split", "face": [0.7, 0.05, 0.28, 0.3]}, "hook_style": "bold", "caption_style": "boxed",
         "hooks": ["Why did Kai *build* this?", "Nobody expected Kai's *getaway car*", "Me showing my mom my *car*"],
         "titles": ["Why Did Kai Build This In GTA 6? 😭", "Nobody Expected Kai's GTA 6 Getaway Car 💀",
                    "Me Showing My Mom My GTA 6 Car 😂"],
         "description": "Kai shows off his new getaway car in GTA 6.", "hashtags": ["#gta6", "#shorts", "#kaicenat"],
         "fx": [{"t": 30.0, "type": "flash", "color": "white"}, {"t": 30.0, "type": "hit", "sound": "boom"}],
         "music": [],
         "why": "Kai zeigt sein neues Fluchtauto; die Enthüllung am Ende zahlt die Neugier aus.",
         "click_trigger": "curiosity gap: Was für ein Auto hat Kai gebaut?"}
    p.update(over)
    return p


# ---------------------------------------------------------------- the fake SDK client

def text(t):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def call(name, inp, cid):
    return SimpleNamespace(type="tool_use", id=cid, name=name, input=inp)


def msg(content, stop="tool_use", usage=None, model="claude-sonnet-5-5", stop_details=None):
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model=model, stop_details=stop_details,
                           id="msg_test")


def usage(inp=100, out=50, write=0, read=0, iterations=None):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=write,
                           cache_read_input_tokens=read, cache_creation=None, iterations=iterations)


class FakeStream:
    def __init__(self, message, events=3, on_event=None):
        self.message, self.events, self.on_event = message, events, on_event
        self.request_id = "req_test"
        self.current_message_snapshot = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for i in range(self.events):
            if self.on_event:
                self.on_event(i)
            yield SimpleNamespace(type="content_block_delta")

    def get_final_message(self):
        return self.message


class FakeClient:
    """Scripted responses for client.beta.messages.stream / messages.create / models.retrieve. A script item is a
    message, an exception to raise, or a callable(params) -> message. Usage left as None is filled in by a prompt
    cache simulation: one entry per breakpoint (the system block with cache_control, and the whole request for the
    top-level automatic one); a request reads the longest entry it starts with, byte for byte."""

    def __init__(self, script, cache=None, on_event=None):
        self.script, self.calls, self.cache = list(script), [], cache if cache is not None else set()
        self.on_event = on_event
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    def _next(self, params):
        if not self.script:
            raise AssertionError("the fake client ran out of scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(params) if callable(item) else item

    def _segments(self, params):
        system = params.get("system")
        if isinstance(system, list):
            system = [{k: v for k, v in b.items() if k != "cache_control"} for b in system]
        head = json.dumps([params.get("tools"), system], sort_keys=True, ensure_ascii=False)
        return (head, *(json.dumps(agent._jsonable(m), sort_keys=True, ensure_ascii=False)
                        for m in params["messages"]))

    def _stream(self, **params):
        self.calls.append({**params, "messages": list(params["messages"]),
                           "rendered": self._segments(params)})
        message = self._next(params)
        if message.usage is None:
            segs = self._segments(params)
            best = max((e for e in self.cache if segs[:len(e)] == e), key=len, default=())
            read, total = sum(map(len, best)) // 4, sum(map(len, segs)) // 4
            if isinstance(params.get("system"), list) and any(b.get("cache_control") for b in params["system"]):
                self.cache.add(segs[:1])
            if params.get("cache_control"):
                self.cache.add(segs)
            message.usage = usage(inp=3, out=120, write=total - read, read=read)
        return FakeStream(message, on_event=self.on_event)

    def _create(self, **params):
        self.calls.append(params)
        message = self._next(params)
        message._request_id = "req_create"
        return message

    def _retrieve(self, model):
        self.calls.append({"retrieve": model})
        return self._next({})


def http_error(cls, status, message="error"):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=req), body=None)


# ---------------------------------------------------------------- test bases

class TempData(unittest.TestCase):
    """A temp data dir with the gta6 profile, the API key and the fixture job (+ its DB row)."""
    video = False

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
        self.dir = make_job(self.root / "jobs", video=self.video)
        self.job_id = store.create_job("source", "gta6", clipper_job=JOB, title="Kai Cenat plays GTA 6",
                                       channel="Kai Cenat Live", source_url=f"https://www.youtube.com/watch?v={JOB}")


class FakeTools(TempData):
    """No subprocesses: the bridge and the clipper runner are scripted; `self.check_fails` makes `check` fail."""

    def setUp(self):
        super().setUp()
        self.check_fails, self.bridge_calls = False, []
        p1 = mock.patch.object(runtime, "run_bridge", side_effect=self.fake_bridge)
        p2 = mock.patch.object(runtime, "run_clipper", side_effect=self.fake_clipper)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def fake_bridge(self, op, payload, profile, **kw):
        self.bridge_calls.append((op, payload))
        if op == "music_library":
            return {"tracks": [{"id": "boom", "kind": "hit", "usable": True},
                               {"id": "quirky", "kind": "music", "mood": "funny", "usable": True},
                               {"id": "skyrim", "kind": "music", "mood": "epic", "usable": False}]}
        if op == "check":
            report = {"video 1": {"hooks": {"Wait...": {"fails": ["vague"]}}}} if self.check_fails else \
                {"video 1": {"playbook": ["a note"]}}
            return {"report": report, "fails": self.check_fails}
        if op == "transcript":
            return {"job": JOB, "offset": 0, "total": 1, "truncated": False,
                    "sentences": [{"s": 10.0, "e": 11.6, "text": "Yo look at this car!"}],
                    "words": [{"w": "Yo", "s": 10.0, "e": 10.3}, {"w": "car!", "s": 11.0, "e": 11.6}]}
        if op == "rank":
            return {"picks": payload["picks"], "ranking": [{"video": 1, "keep": 3, "ok": True, "kept": [],
                                                            "ranking": [], "keep_line": "keep (top 3)",
                                                            "titles_dropped": []}]}
        raise AssertionError(f"unexpected bridge op {op}")

    def fake_clipper(self, args, profile, **kw):
        if kw.get("argv") and kw["argv"][0] == "ffprobe":
            return runtime.ProcResult(0, "60.0\n", "", 0.01)
        raise AssertionError(f"unexpected clipper call {args} {kw.get('argv')}")

    def run_select(self, script, max_picks=3, cache=None, **kw):
        client = FakeClient(script, cache=cache, on_event=kw.pop("on_event", None))
        with mock.patch.object(agent, "make_client", return_value=client):
            try:
                return agent.select_picks(self.job_id, max_picks=max_picks, **kw), client
            finally:
                self.client = client


# ---------------------------------------------------------------- prompts

class PromptTests(TempData):
    def test_system_blocks_are_byte_stable_and_cache_the_research_for_an_hour(self):
        a, b = agent.build_system_prompt("gta6"), agent.build_system_prompt("gta6")
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertEqual(len(a), 2)
        self.assertEqual(a[1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertNotIn("cache_control", a[0])
        self.assertIn("What makes GTA Shorts go viral", a[1]["text"])
        self.assertEqual(agent.system_prompt_chars("gta6"), len(a[0]["text"]) + len(a[1]["text"]))
        brief = a[0]["text"]
        for must in ("submit_picks", "no_clip_reason", "grep_transcript", "view_frame", "grid=true", "find_line",
                     "simulate", "click trigger", "60 seconds", "three words", "German", "curiosity gap",
                     "End on the result", "caption_fixes", "Never reuse footage", "Profile notes: gta6", "GTA VI"):
            self.assertIn(must, brief)

    def test_owner_instructions_go_after_the_breakpoint_and_the_override_replaces_the_research(self):
        store.set_settings({"claude.extra_instructions": "Bevorzuge Comedy-Clips.",
                            "research.override": "# My playbook\nShort clips only."})
        blocks = agent.build_system_prompt("gta6")
        self.assertEqual(len(blocks), 3)
        self.assertIn("Short clips only.", blocks[1]["text"])
        self.assertNotIn("What makes GTA Shorts go viral", blocks[1]["text"])
        self.assertIn("Bevorzuge Comedy-Clips.", blocks[2]["text"])
        self.assertNotIn("cache_control", blocks[2])

    def test_profile_notes_carry_the_rp_topic_rule_and_playbook_overrides(self):
        notes = prompts.profile_notes("gta-rp")
        self.assertIn("Profile notes: gta-rp", notes)
        self.assertIn("no ?pixel", notes)
        self.assertIn("POV: you're the hostage in *NoPixel*", notes)
        self.assertIn("chopp -> Chop", notes)
        self.assertNotIn("GTA VI", notes)
        self.assertIn("trever -> Trevor", prompts.profile_notes("gta6"))

    def test_tools_are_the_six_with_strict_only_on_the_small_read_only_ones(self):
        self.assertEqual(T.TOOL_NAMES, ("grep_transcript", "view_frame", "find_line", "check", "simulate",
                                        "submit_picks"))
        strict = [t["name"] for t in T.TOOLS if t.get("strict")]
        self.assertEqual(strict, ["grep_transcript", "view_frame", "find_line"])

        def objects(schema):
            if isinstance(schema, dict):
                if schema.get("type") == "object":
                    yield schema
                for v in schema.values():
                    yield from objects(v)
            elif isinstance(schema, list):
                for v in schema:
                    yield from objects(v)
        for t in T.TOOLS:
            if t.get("strict"):  # strict schemas: every object closed, no numeric/string constraints
                for o in objects(t["input_schema"]):
                    self.assertIs(o["additionalProperties"], False)
                self.assertNotRegex(json.dumps(t["input_schema"]), r'"(minimum|maximum|minLength|maxLength)"')

    def test_dub_schema_is_strict_compatible(self):
        self.assertEqual(set(prompts.DUB_SCHEMA["required"]), set(prompts.DUB_SCHEMA["properties"]))
        self.assertIs(prompts.DUB_SCHEMA["additionalProperties"], False)


# ---------------------------------------------------------------- pick validation

class ValidationTests(TempData):
    def ctx(self, **over):
        ctx = T.JobContext(job={"id": self.job_id, "kind": "source"}, profile="gta6", clipper_job=JOB, dir=self.dir,
                           meta={}, duration=60.0, max_picks=3, avoid_openers=[], voice="auto",
                           hooks_per_video=3, scratch=self.root / "scratch")
        for k, v in over.items():
            setattr(ctx, k, v)
        return ctx

    def errors(self, picks, ctx=None, draft=False):
        return T.validate_picks(ctx or self.ctx(), picks, draft=draft)[3]

    def test_a_good_pick_passes_and_comes_back_as_a_clipper_pick(self):
        clean, reasons, triggers, errs = T.validate_picks(self.ctx(), [pick()])
        self.assertEqual(errs, [])
        self.assertNotIn("why", clean[0])
        self.assertNotIn("click_trigger", clean[0])
        self.assertEqual(clean[0]["hooks"], pick()["hooks"])
        self.assertTrue(reasons[0].startswith("Kai zeigt"))
        self.assertTrue(triggers[0].startswith("curiosity gap"))
        seg = pick(segments=[{"start": 10, "end": 20}, {"start": 40, "end": 46.5}], labels=[{"s": 0, "e": 2,
                                                                                              "text": "*LOOKS*"}])
        del seg["start"], seg["end"]
        self.assertEqual(self.errors([seg]), [])

    def test_no_nameable_trigger_no_clip(self):
        self.assertIn("click_trigger is required", "".join(self.errors([pick(click_trigger="")])))
        self.assertIn("must start with the trigger's name", "".join(self.errors([pick(click_trigger="cool car")])))
        self.assertIn("why is required", "".join(self.errors([pick(why=" ")])))
        self.assertEqual(self.errors([pick(why="", click_trigger="")], draft=True), [])

    def test_shape_errors_name_the_video_and_the_fix(self):
        both = pick(segments=[{"start": 1, "end": 5}])
        cases = [
            (pick(hook_styles="bold"), "unknown keys ['hook_styles']"),
            (both, "not both"),
            (pick(end=80.0), "after the video's end"),
            (pick(start=0.0, end=59.0, segments=None), "unknown"),
            (pick(clip_type="vlog"), "clip_type must be one of"),
            (pick(layout={"type": "split"}), "split needs face"),
            (pick(layout={"type": "split", "face": [0.8, 0.1, 0.4, 0.3]}), "x+w and y+h"),
            (pick(hook_style="huge"), "hook_style"),
            (pick(titles=["Only one GTA 6"]), "one per hook"),
            (pick(hooks=["Same *hook*", "same hook"], titles=["A GTA 6", "B GTA 6"]), "same text"),
            (pick(hashtags=["gta6"]), "hashtags"),
            (pick(voiceover="one two three four five six seven eight nine ten eleven"), "10 or fewer"),
            (pick(fx=[{"t": 12, "type": "zoom", "color": "red"}]), "color only goes on a flash"),
            (pick(music=[{"s": 12, "e": 15, "mood": "happy"}]), "mood must be one of"),
            (pick(labels=[{"s": 15, "e": 12, "text": "*X*"}]), "s < e"),
            (pick(caption_fixes={"swerver": 3}), "caption_fixes"),
        ]
        for p, want in cases[:3] + cases[4:]:
            with self.subTest(want=want):
                self.assertIn(want, "\n".join(self.errors([p])))
        long = pick(segments=[{"start": 0, "end": 35}, {"start": 36, "end": 59}])
        del long["start"], long["end"]
        self.assertEqual(self.errors([long]), [])  # 58 s: under the cap
        longer = dict(long, segments=[{"start": 0, "end": 40}, {"start": 10, "end": 35}])
        self.assertIn("65.0 s long", "\n".join(self.errors([longer])))
        other = dict(long, segments=[{"start": 0, "end": 5}, {"start": 0, "end": 5, "job": "no-such-job"}])
        self.assertIn("doesn't exist", "\n".join(self.errors([other])))

    def test_voice_preference_is_enforced(self):
        self.assertIn("turned voiceovers off", "\n".join(self.errors([pick(voiceover="He waited years for this")],
                                                                     self.ctx(voice="off"))))
        self.assertIn("voiceover on every video", "\n".join(self.errors([pick()], self.ctx(voice="all"))))
        self.assertEqual(self.errors([pick(voiceover="He waited years for this")], self.ctx(voice="all")), [])

    def test_picks_never_share_footage_or_openers_and_respect_max_picks(self):
        second = pick(start=25.0, end=45.0, hooks=["What did Kai *see*?", "Kai can't stop *running*",
                                                  "POV: Kai is *behind* you"],
                      titles=["What Did Kai See In GTA 6?", "Kai Can't Stop Running In GTA 6",
                              "POV: Kai Is Behind You In GTA 6"])
        self.assertIn("share 6 s of footage", "\n".join(self.errors([pick(), second])))
        apart = dict(second, start=33.0, end=50.0)
        self.assertEqual(self.errors([pick(), apart]), [])
        echo = dict(apart, hooks=["Why did Kai *build* that?"] + apart["hooks"][1:])
        self.assertIn("opens with \"why did kai\" like video 1", "\n".join(self.errors([pick(), echo])))
        ctx = self.ctx(avoid_openers=["Nobody expected Kai's reaction"])
        self.assertIn("like an earlier video", "\n".join(self.errors([pick()], ctx)))
        self.assertIn("at most 1 video", "\n".join(self.errors([pick(), apart], self.ctx(max_picks=1))))

    def test_submit_needs_a_reason_for_an_empty_list(self):
        ctx = self.ctx()
        self.assertTrue(T.submit(ctx, {"picks": []}, round_no=1, max_rounds=3).is_error)
        out = T.submit(ctx, {"picks": [], "no_clip_reason": "Nichts mit Auslöser."}, round_no=1, max_rounds=3)
        self.assertEqual((out.picks, out.no_clip_reason), ([], "Nichts mit Auslöser."))
        self.assertTrue(T.submit(ctx, {"picks": [pick()], "no_clip_reason": "x"}, round_no=1, max_rounds=3).is_error)

    def test_near_miss_tool_names_resolve(self):
        self.assertEqual(T.resolve_name("Submit-Picks"), "submit_picks")
        self.assertEqual(T.resolve_name("viewframe"), "view_frame")
        self.assertIsNone(T.resolve_name("render"))


# ---------------------------------------------------------------- the select loop

class LoopTests(FakeTools):
    def test_tool_calls_then_an_accepted_submit(self):
        script = [msg([thinking(), text("Looking first."), call("grep_transcript", {"around_s": 11}, "t1"),
                       call("view_frame", {"sheet": "cand_02"}, "t2")]),
                  msg([thinking(), call("submit_picks", {"picks": [pick()]}, "t3")])]
        lines = []
        sel, client = self.run_select(script, log=lines.append)
        self.assertEqual(len(sel.picks), 1)
        self.assertNotIn("why", sel.picks[0])
        self.assertEqual(sel.reasons, [pick()["why"]])
        self.assertEqual(sel.click_triggers, [pick()["click_trigger"]])
        self.assertIsNone(sel.no_clip_reason)
        self.assertEqual(sel.check_report, {"video 1": {"playbook": ["a note"]}})
        self.assertEqual(sel.turns, 2)
        first, second = client.calls
        # the request: adaptive thinking, effort, tool_choice auto (never forced), refusal fallback, caching
        self.assertEqual(first["model"], "claude-sonnet-5-5")
        self.assertEqual(first["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(first["output_config"], {"effort": "medium"})
        self.assertEqual(first["tool_choice"], {"type": "auto"})
        self.assertEqual((first["betas"], first["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertEqual([t["name"] for t in first["tools"]], list(T.TOOL_NAMES))
        content = first["messages"][0]["content"]
        facts = content[0]["text"]
        for want in ("at most 3", "recommends 2", "Voiceover: auto", "moods with an approved track: funny",
                     "hit sounds: boom", "40 in total", "46 s of video", "Fix rounds: 3 rejected"):
            self.assertIn(want, facts)
        self.assertIn("#1 strong · 10.0-31.0 s (21 s)", content[1]["text"])
        self.assertIn("S1 scenario", content[1]["text"])
        self.assertEqual(sum(1 for b in content if b["type"] == "image"), 3)  # cand_01, scen_01, cand_02
        # parallel calls: both results in one user message, in order, the frame as an image block
        results = second["messages"][2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2"])
        self.assertIn("10.0 Yo", results[0]["content"])
        self.assertEqual(results[1]["content"][1]["type"], "image")
        self.assertNotIn("is_error", results[0])
        self.assertIs(second["messages"][1]["content"][0], script[0].content[0])  # sent back unchanged (thinking)
        # usage rows with the served model and the token fields; the conversation prefix was read from cache
        rows = store.recent_usage(10)[::-1]
        self.assertEqual([(r["purpose"], r["model"], r["job_id"]) for r in rows],
                         [("select", "claude-sonnet-5-5", self.job_id)] * 2)
        self.assertEqual(rows[0]["cache_read_tokens"], 0)
        self.assertGreater(rows[0]["cache_creation_tokens"], 5000)
        self.assertGreaterEqual(rows[1]["cache_read_tokens"], rows[0]["cache_creation_tokens"])
        self.assertEqual((rows[1]["input_tokens"], rows[1]["output_tokens"]), (3, 120))
        self.assertEqual(sel.usage["cache_read_tokens"], rows[1]["cache_read_tokens"])
        self.assertAlmostEqual(sel.usage["cost_eur"], sum(r["cost_eur"] for r in rows), places=6)
        self.assertTrue(any("Claude liest das Transkript" in x for x in lines))
        self.assertTrue((self.root / "data" / "tmp" / "agent" / f"job-{self.job_id}" / "select.json").exists())

    def test_a_second_job_of_the_profile_reads_the_cached_tools_and_system(self):
        cache = set()
        self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "a")])], cache=cache)
        system_tokens = len(self.client.calls[0]["rendered"][0]) // 4
        make_job(self.root / "jobs", "agent-fixture-2")
        self.job_id = store.create_job("source", "gta6", clipper_job="agent-fixture-2")
        self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "b")])], cache=cache)
        self.assertEqual(store.recent_usage(1)[0]["cache_read_tokens"], system_tokens)
        self.assertGreater(system_tokens, 15000)

    def test_schema_rejection_then_fix_then_accept(self):
        bad = pick(click_trigger="")
        sel, client = self.run_select([msg([call("submit_picks", {"picks": [bad]}, "s1")]),
                                       msg([call("submit_picks", {"picks": [pick()]}, "s2")])])
        self.assertEqual(len(sel.picks), 1)
        result = client.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("Not accepted", result["content"])
        self.assertIn("click_trigger is required", result["content"])
        self.assertEqual([r["purpose"] for r in store.recent_usage(10)[::-1]], ["select", "fix"])
        self.assertEqual([op for op, _ in self.bridge_calls].count("check"), 1)  # not on the broken pick

    def test_check_fails_go_back_until_the_fix_rounds_run_out(self):
        store.set_settings({"claude.max_fix_rounds": 2})
        self.check_fails = True
        script = [msg([call("submit_picks", {"picks": [pick()]}, f"s{i}")]) for i in range(3)]
        with self.assertRaises(agent.AgentError) as ctx:
            self.run_select(script)
        self.assertEqual(ctx.exception.code, "check_failed")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("2 Korrekturrunde", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 3)
        last = self.client.calls[2]["messages"][-1]["content"][0]
        self.assertIn("fix round 2 of 2", last["content"])
        self.assertIn('"fails":["vague"]', last["content"])

    def test_a_turn_without_a_tool_call_gets_a_nudge_then_fails_clearly(self):
        sel, client = self.run_select([msg([text("Here are my picks: ...")], stop="end_turn"),
                                       msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertEqual(client.calls[1]["messages"][-1]["content"][0]["text"], agent.NUDGE)
        self.assertEqual(len(sel.picks), 1)
        with self.assertRaises(agent.AgentError) as ctx:
            self.run_select([msg([text("thinking aloud")], stop="end_turn")] * 3)
        self.assertEqual(ctx.exception.code, "no_submission")

    def test_max_turns_ends_the_loop_with_a_reminder_before(self):
        store.set_settings({"claude.max_turns": 3})
        script = [msg([call("grep_transcript", {"pattern": "car"}, f"g{i}")]) for i in range(3)]
        with self.assertRaises(agent.AgentError) as ctx:
            self.run_select(script)
        self.assertEqual(ctx.exception.code, "max_turns")
        self.assertEqual(len(self.client.calls), 3)
        notes = [b["text"] for b in self.client.calls[2]["messages"][-1]["content"] if b.get("type") == "text"]
        self.assertIn("1 request is left", notes[0])

    def test_budget_stops_before_the_first_request(self):
        store.set_settings({"claude.daily_limit_eur": 0.5})
        store.add_usage(None, "select", "claude-sonnet-5-5", input_tokens=1_000_000)
        with self.assertRaises(agent.BudgetExceeded) as ctx:
            self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertIn("Tageslimit", str(ctx.exception))
        self.assertEqual(self.client.calls, [])

    def test_the_daily_limit_gates_the_start_but_never_kills_a_running_selection(self):
        # (replaces a test of the old behaviour: the limit was checked before every request, so a selection that
        # crossed it was killed mid-way and its paid turns thrown away)
        store.set_settings({"claude.daily_limit_eur": 1.0, "claude.selection_limit_eur": 10.0})
        expensive = msg([call("grep_transcript", {"around_s": 11}, "g")], usage=usage(inp=1_000_000))  # 1,84 €
        sel, client = self.run_select([expensive, msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertEqual((len(sel.picks), len(client.calls)), (1, 2))  # finished past the limit, under its cap
        self.assertGreater(costs.spent_today_eur(), 1.0)
        make_job(self.root / "jobs", "agent-fixture-2")
        self.job_id = store.create_job("source", "gta6", clipper_job="agent-fixture-2")
        with self.assertRaises(agent.BudgetExceeded):  # a new selection waits for tomorrow
            self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertEqual(self.client.calls, [])

    def test_the_selection_cap_warns_claude_then_stops_before_the_request_that_would_cross_it(self):
        store.set_settings({"claude.selection_limit_eur": 0.5})
        step = lambda i: msg([call("grep_transcript", {"around_s": 11}, f"g{i}")], usage=usage(inp=100_000))  # noqa
        with self.assertRaises(agent.AgentError) as ctx:  # 0,184 € a request: two fit under 0,50 €, a third not
            self.run_select([step(i) for i in range(5)])
        self.assertIsInstance(ctx.exception, agent.SelectionCapReached)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("selection_cap", False))
        self.assertIn("Kostenlimit", str(ctx.exception))
        self.assertEqual(len(self.client.calls), 2)
        notes = [b["text"] for b in self.client.calls[1]["messages"][-1]["content"] if b.get("type") == "text"]
        self.assertTrue(any("spending limit for this selection is almost used up" in n for n in notes), notes)
        self.assertLessEqual(sum(r["cost_eur"] for r in store.recent_usage(10)), 0.5)
        checkpoint = self.root / "data" / "tmp" / "agent" / f"job-{self.job_id}" / "select.resume.json"
        self.assertFalse(checkpoint.exists())  # a manual retry starts a new selection with a new cap
        self.assertEqual(costs.reserved_eur(), 0)

    def test_the_cap_counts_what_the_selection_paid_before_a_restart(self):
        store.set_settings({"claude.selection_limit_eur": 0.5})
        step = lambda i: msg([call("grep_transcript", {"around_s": 11}, f"g{i}")], usage=usage(inp=100_000))  # noqa
        with self.assertRaises(agent.AgentError) as ctx:  # one paid turn, then a 429: the job retries later
            self.run_select([step(0), http_error(anthropic.RateLimitError, 429)])
        self.assertTrue(ctx.exception.retryable)
        with self.assertRaises(agent.SelectionCapReached):
            self.run_select([step(1), step(2), step(3)])
        self.assertEqual(len(self.client.calls), 1)  # 0,184 € carried over + one more; a third would cross 0,50 €

    def test_a_running_selection_reserves_its_cap_against_the_daily_limit(self):
        store.set_settings({"claude.daily_limit_eur": 2.5, "claude.selection_limit_eur": 2.0})
        store.add_usage(None, "select", "claude-sonnet-5-5", input_tokens=326_087)  # 0,60 € spent today
        seen = {}

        def during(params):
            seen.update(reserved=costs.reserved_eur(), can_start=costs.can_start(),
                        own=costs.can_start(exclude=f"select-{self.job_id}"), message=agent._budget_message())
            return msg([call("submit_picks", {"picks": [pick()]}, "s")], usage=usage(inp=10))
        self.assertTrue(costs.can_start())
        self.run_select([during])
        self.assertAlmostEqual(seen["reserved"], 2.0, places=3)
        self.assertFalse(seen["can_start"])  # 0,60 € + 2,00 € may still be spent: nothing new starts (limit 2,50 €)
        self.assertTrue(seen["own"])
        self.assertIn("für laufende Auswahlen verplant", seen["message"])
        self.assertEqual(costs.reserved_eur(), 0)
        self.assertTrue(costs.can_start())

    def test_a_check_that_cannot_run_is_not_a_paid_retry_of_the_whole_selection(self):
        def broken_check(op, payload, profile, **kw):
            if op == "check":
                raise runtime.BridgeError("check: Zeitüberschreitung nach 300 s", timed_out=True)
            return self.fake_bridge(op, payload, profile, **kw)
        script = [msg([call("submit_picks", {"picks": [pick()]}, "s1")]),
                  msg([call("submit_picks", {"picks": [pick()]}, "s2")])]
        with mock.patch.object(runtime, "run_bridge", side_effect=broken_check), \
                self.assertRaises(agent.AgentError) as ctx:
            self.run_select(script)
        # the first failure goes back to Claude (not the picks' fault), the second ends this run as retryable
        result = self.client.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("couldn't run", result["content"])
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("check_unavailable", True))
        checkpoint = self.root / "data" / "tmp" / "agent" / f"job-{self.job_id}" / "select.resume.json"
        self.assertTrue(checkpoint.exists())  # the retry continues the conversation instead of starting over
        sel, client = self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s3")])])
        self.assertEqual((len(sel.picks), len(client.calls)), (1, 1))

    def test_anything_unexpected_is_a_final_agent_error_that_keeps_the_paid_turns(self):
        first = msg([call("grep_transcript", {"around_s": 11}, "g1")])
        with mock.patch.object(T, "submit", side_effect=KeyError("boom")), \
                self.assertLogs("automate.agent", "ERROR"), self.assertRaises(agent.AgentError) as ctx:
            self.run_select([first, msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("internal", False))
        self.assertIn("Interner Fehler", str(ctx.exception))
        sel, client = self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertEqual(len(client.calls[0]["messages"]), 3)  # resumed after the paid grep turn
        with mock.patch.object(T.JobContext, "load", side_effect=RuntimeError("disk")), \
                self.assertLogs("automate.agent", "ERROR"), self.assertRaises(agent.AgentError) as ctx:
            self.run_select([])
        self.assertEqual(ctx.exception.code, "internal")

    def test_footage_other_pending_picks_hold_reaches_claudes_own_checks_but_not_the_request(self):
        held = [{"pick": {"start": 10.0, "end": 31.0}, "job": "other-job", "label": "Clip 1 von Job #3 (freigegeben)"}]
        self.run_select([msg([call("check", {"picks": [pick()]}, "c")]),
                         msg([call("submit_picks", {"picks": [pick()]}, "s")])], reserved=held)
        checks = [p for op, p in self.bridge_calls if op == "check"]
        self.assertEqual([p["reserved"] for p in checks], [held, held])  # the check tool and submit_picks
        self.assertNotIn("Clip 1 von Job #3", json.dumps(agent._jsonable(self.client.calls[0]["messages"])))

    def test_no_credit_and_a_changed_prompt(self):
        first = msg([call("grep_transcript", {"around_s": 11}, "g1")])
        with self.assertRaises(agent.AgentError) as ctx:  # the account ran dry mid-selection: the job waits
            self.run_select([first, http_error(anthropic.BadRequestError, 400, "Your credit balance is too low")])
        self.assertEqual(ctx.exception.code, "billing")
        checkpoint = self.root / "data" / "tmp" / "agent" / f"job-{self.job_id}" / "select.resume.json"
        self.assertTrue(checkpoint.exists())
        # a changed system prompt would make Anthropic reject the replayed thinking blocks: start over instead
        store.set_settings({"claude.extra_instructions": "Bevorzuge Comedy-Clips."})
        _, client = self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        self.assertEqual(len(client.calls[0]["messages"]), 1)

    def test_no_clip_reason_is_a_valid_result(self):
        sel, _ = self.run_select([msg([call("submit_picks", {"picks": []}, "s1")]),
                                  msg([call("submit_picks", {"picks": [], "no_clip_reason": "Kein Moment hat einen "
                                                                                            "Auslöser."}, "s2")])])
        self.assertEqual((sel.picks, sel.no_clip_reason, sel.check_report),
                         ([], "Kein Moment hat einen Auslöser.", {}))

    def test_cancel_before_a_request_during_a_stream_and_during_a_tool(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(agent.AgentCanceled):
            self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])], cancel=cancel)
        self.assertEqual(self.client.calls, [])

        cancel = threading.Event()
        with self.assertRaises(agent.AgentCanceled):
            self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")], usage=usage(out=7))],
                            cancel=cancel, on_event=lambda i: i == 1 and cancel.set())
        self.assertEqual(store.recent_usage(1)[0]["output_tokens"], 7)  # what the stream had generated is logged

        cancel = threading.Event()

        def canceled_bridge(op, payload, profile, **kw):
            if op == "transcript":
                cancel.set()
                raise runtime.BridgeError("abgebrochen", canceled=True)
            return self.fake_bridge(op, payload, profile, **kw)
        with mock.patch.object(runtime, "run_bridge", side_effect=canceled_bridge), \
                self.assertRaises(agent.AgentCanceled) as ctx:
            self.run_select([msg([call("grep_transcript", {"around_s": 11}, "g")])], cancel=cancel)
        self.assertIsInstance(ctx.exception, agent.AgentError)
        self.assertEqual(ctx.exception.code, "canceled")

    def test_the_gates_check_feedback_goes_into_the_first_message(self):
        report = {"video 1": {"hooks": {"Wait for it...": {"fails": ["vague: says nothing concrete"],
                                                           "notes": ["a note"]}},
                              "titles": {"GTA": {"fails": ["needs \"GTA 6\""]}}, "variety": "2 hooks of one type",
                              "playbook": ["only a note"]}}
        params = {**store.get_job(self.job_id)["params"], "check_feedback": report, "gate_rounds": 1}
        store.update_job(self.job_id, params=params)
        self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        content = self.client.calls[0]["messages"][0]["content"]
        gate = [b["text"] for b in content if b.get("type") == "text" and "failed the quality gate" in b["text"]]
        self.assertEqual(len(gate), 1)
        for want in ('video 1, hook "Wait for it...": vague: says nothing concrete', 'video 1, title "GTA": needs',
                     "video 1, variety: 2 hooks of one type"):
            self.assertIn(want, gate[0])
        self.assertNotIn("only a note", gate[0])
        self.assertEqual(content[-1]["text"].split(":")[0], "Work through this source now and finish with submit_picks")
        self.assertEqual(agent.gate_feedback(None), [])

    def test_an_interrupted_selection_continues_after_its_last_paid_turn(self):
        first = msg([thinking(), text("Reading."), call("grep_transcript", {"around_s": 11}, "g1")])
        with self.assertRaises(agent.AgentError) as ctx:  # the next request hits a 429 (or the server stops)
            self.run_select([first, http_error(anthropic.RateLimitError, 429)])
        self.assertTrue(ctx.exception.retryable)
        transcript_calls = [op for op, _ in self.bridge_calls].count("transcript")
        lines = []
        sel, client = self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])], log=lines.append)
        self.assertEqual(len(sel.picks), 1)
        self.assertEqual(sel.turns, 2)  # one paid turn carried over, one new
        resumed = client.calls[0]["messages"]
        self.assertEqual([m["role"] for m in resumed], ["user", "assistant", "user"])
        self.assertEqual(resumed[1]["content"][0]["type"], "thinking")
        self.assertEqual(resumed[1]["content"][2]["id"], "g1")
        self.assertEqual(resumed[2]["content"][0]["tool_use_id"], "g1")
        self.assertEqual([op for op, _ in self.bridge_calls].count("transcript"), transcript_calls)  # not re-run
        self.assertTrue(any("unterbrochene Auswahl" in x for x in lines))
        checkpoint = self.root / "data" / "tmp" / "agent" / f"job-{self.job_id}" / "select.resume.json"
        self.assertFalse(checkpoint.exists())  # accepted: nothing to resume any more
        # a different first message (here: other max_picks) starts over; a non-retryable end drops the checkpoint
        with self.assertRaises(agent.AgentError):
            self.run_select([first, http_error(anthropic.RateLimitError, 429)])
        self.assertTrue(checkpoint.exists())
        _, client = self.run_select([msg([call("submit_picks", {"picks": [pick()]}, "s")])], max_picks=1)
        self.assertEqual(len(client.calls[0]["messages"]), 1)
        details = SimpleNamespace(category="cyber", explanation=None)
        with self.assertRaises(agent.AgentError):
            self.run_select([first, msg([], stop="refusal", stop_details=details)])
        self.assertFalse(checkpoint.exists())

    def test_refusal_and_unexpected_stops_end_the_job(self):
        details = SimpleNamespace(category="general_harms", explanation=None)
        with self.assertRaises(agent.AgentError) as ctx:
            self.run_select([msg([], stop="refusal", stop_details=details)])
        self.assertEqual(ctx.exception.code, "refusal")
        self.assertIn("general_harms", str(ctx.exception))
        with self.assertRaises(agent.AgentError) as ctx:
            self.run_select([msg([text("...")], stop="model_context_window_exceeded")])
        self.assertEqual(ctx.exception.code, "stop_reason")

    def test_a_reply_cut_off_at_max_tokens_is_answered_and_continued(self):
        cut = msg([thinking(), call("submit_picks", {"picks": [{"start": 10}]}, "cut")], stop="max_tokens")
        sel, client = self.run_select([cut, msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        reply = client.calls[1]["messages"][-1]["content"]
        self.assertEqual((reply[0]["tool_use_id"], reply[0]["is_error"]), ("cut", True))
        self.assertEqual(reply[1]["text"], agent.CUT_OFF)
        self.assertEqual(len(sel.picks), 1)

    def test_a_fallback_logs_each_model_and_echoes_only_what_the_fallback_model_said(self):
        iters = [SimpleNamespace(type="message", model="claude-sonnet-5-5", input_tokens=50, output_tokens=10,
                                 cache_creation_input_tokens=0, cache_read_input_tokens=0, cache_creation=None),
                 SimpleNamespace(type="fallback_message", model="claude-sonnet-5", input_tokens=60, output_tokens=40,
                                 cache_creation_input_tokens=0, cache_read_input_tokens=0, cache_creation=None)]
        declined = call("view_frame", {"sheet": "cand_02"}, "declined")
        fb = SimpleNamespace(type="fallback", from_=SimpleNamespace(model="claude-sonnet-5-5"),
                             to=SimpleNamespace(model="claude-sonnet-5"))
        first = msg([thinking(), declined, fb, text("Continuing."), call("grep_transcript", {"around_s": 11}, "ok")],
                    usage=usage(inp=60, out=40, iterations=iters), model="claude-sonnet-5")
        sel, client = self.run_select([first, msg([call("submit_picks", {"picks": [pick()]}, "s")])])
        echoed = client.calls[1]["messages"][1]["content"]
        self.assertEqual([b.type for b in echoed], ["fallback", "text", "tool_use"])
        self.assertEqual([r["tool_use_id"] for r in client.calls[1]["messages"][2]["content"]], ["ok"])
        rows = store.recent_usage(10)[::-1]
        self.assertEqual([(r["model"], r["input_tokens"]) for r in rows[:2]],
                         [("claude-sonnet-5-5", 50), ("claude-sonnet-5", 60)])

    def test_missing_and_rejected_keys(self):
        store.set_secret("anthropic_api_key", None)
        with self.assertRaises(agent.MissingApiKey):
            agent.select_picks(self.job_id, max_picks=1)
        store.set_secret("anthropic_api_key", KEY)
        with self.assertRaises(agent.MissingApiKey) as ctx:
            self.run_select([http_error(anthropic.AuthenticationError, 401)])
        self.assertIsInstance(ctx.exception, agent.InvalidApiKey)
        self.assertIsInstance(ctx.exception, agent.AgentError)
        self.assertIn("abgelehnt", str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_api_errors_map_to_retryable_or_not(self):
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [(http_error(anthropic.RateLimitError, 429), True, "rate_limited"),
                 (http_error(anthropic.InternalServerError, 529), True, "server_error"),
                 (anthropic.APIConnectionError(request=req), True, "connection"),
                 (anthropic.APITimeoutError(request=req), True, "connection"),
                 (http_error(anthropic.BadRequestError, 400, "messages: bad"), False, "api_error"),
                 (http_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), False, "billing"),
                 (http_error(anthropic.NotFoundError, 404), False, "model_not_found"),
                 (http_error(anthropic.PermissionDeniedError, 403), False, "permission")]
        for exc, retryable, code in cases:
            with self.subTest(code=code), self.assertRaises(agent.AgentError) as ctx:
                self.run_select([exc])
            self.assertEqual((ctx.exception.retryable, ctx.exception.code), (retryable, code))

    def test_tool_failures_come_back_as_errors_the_model_can_act_on(self):
        sel, client = self.run_select([msg([call("grep_transcript", {}, "a"), call("view_frame", {"sheet": "x"}, "b"),
                                            call("render", {}, "c"), call("Submit_Picks", {"picks": [pick()]}, "d")])])
        self.assertEqual(len(sel.picks), 1)  # the near-miss name was accepted
        sel, client = self.run_select([msg([call("grep_transcript", {}, "a"), call("view_frame", {"sheet": "x"}, "b"),
                                            call("render", {}, "c")]),
                                       msg([call("submit_picks", {"picks": [pick()]}, "d")])])
        results = client.calls[1]["messages"][-1]["content"]
        self.assertTrue(all(r["is_error"] for r in results))
        self.assertIn("give a pattern", results[0]["content"])
        self.assertIn("cand_07", results[1]["content"])
        self.assertIn("unknown tool 'render'", results[2]["content"])

    def test_no_analysis_is_a_clear_error(self):
        (self.dir / "candidates.md").unlink()
        with self.assertRaises(agent.AgentError) as ctx:
            self.run_select([])
        self.assertEqual(ctx.exception.code, "no_analysis")


# ---------------------------------------------------------------- tools on the fixture job

class ToolTests(TempData):
    video = True

    def ctx(self):
        return T.JobContext.load(store.get_job(self.job_id), max_picks=3)

    def test_grep_transcript_reads_word_timings_and_searches(self):
        ctx = self.ctx()
        out = T.run_tool(ctx, "grep_transcript", {"around_s": 12})
        self.assertFalse(out.is_error)
        self.assertIn("[10.00-11.60] Yo look at this car!", out.content)
        self.assertIn("10.0 Yo · 10.4 look", out.content)
        self.assertIn("14.3 show", out.content)
        hits = T.run_tool(ctx, "grep_transcript", {"pattern": "trever|getaway"})
        self.assertIn("getaway car.", hits.content)
        self.assertIn("Trever just died!", hits.content)
        self.assertNotIn(" · ", hits.content.split("\n", 1)[1])  # a search lists sentences only
        wide = T.run_tool(ctx, "grep_transcript", {"from_s": 0, "to_s": 600})
        self.assertIn("window cut to 180 s", wide.content)
        self.assertTrue(T.run_tool(ctx, "grep_transcript", {"pattern": "(("}).is_error)
        self.assertIn("doesn't exist", T.run_tool(ctx, "grep_transcript", {"around_s": 1, "job": "nope"}).content)

    def test_view_frame_returns_downscaled_images_and_respects_the_budget(self):
        ctx = self.ctx()
        jpeg(self.dir / "review" / "cand_02.jpg", size=(3000, 1100))
        out = T.run_tool(ctx, "view_frame", {"sheet": "cand_02"})
        self.assertEqual(out.content[1]["type"], "image")
        self.assertIn("(1568x575)", out.content[0]["text"])
        self.assertTrue(T.run_tool(ctx, "view_frame", {"sheet": "../job"}).is_error)
        self.assertTrue(T.run_tool(ctx, "view_frame", {}).is_error)
        ctx.views = T.MAX_VIEW_CALLS
        self.assertIn("frame limit", T.run_tool(ctx, "view_frame", {"sheet": "cand_01"}).content)

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_view_frame_grabs_frames_and_gridded_frames(self):
        ctx = self.ctx()
        self.assertEqual(ctx.duration, 60.0)
        plain = T.run_tool(ctx, "view_frame", {"seconds": 12.5})
        self.assertFalse(plain.is_error, plain.content)
        self.assertIn("Frame at 12.5 s", plain.content[0]["text"])
        self.assertEqual(plain.content[1]["source"]["media_type"], "image/jpeg")
        grid = T.run_tool(ctx, "view_frame", {"seconds": 5, "grid": True})
        self.assertFalse(grid.is_error, grid.content)
        self.assertIn("10 % grid", grid.content[0]["text"])
        self.assertEqual(sorted(p.name for p in (self.dir / "review").iterdir()),
                         ["cand_01.jpg", "cand_02.jpg", "scen_01.jpg"])  # the gridded frame isn't left as a sheet
        self.assertIn("past the end", T.run_tool(ctx, "view_frame", {"seconds": 90}).content)
        (self.dir / "source.mp4").unlink()
        self.assertIn("gone", T.run_tool(ctx, "view_frame", {"seconds": 5}).content)

    def test_find_line_formats_hits_from_other_streams(self):
        ctx = self.ctx()
        stdout = (f"{JOB}\tKai Cenat Live\t-\t40.0-41.9s\tOh my god he's behind me!\n"
                  "live-xqc-1\txQc\t21:04:10\t812.2-815.0s\t(footage deleted) Oh my god he's behind me!\n")
        with mock.patch.object(runtime, "run_clipper", return_value=runtime.ProcResult(0, stdout, "", 0.1)) as rc:
            out = T.run_tool(ctx, "find_line", {"pattern": "he's behind me", "hours": 3})
        self.assertEqual(rc.call_args.args[0], ["find", "--hours", "3", "--", "he's behind me"])
        with mock.patch.object(runtime, "run_clipper", return_value=runtime.ProcResult(0, "", "", 0.1)) as rc:
            T.run_tool(ctx, "find_line", {"pattern": "-x?behind", "hours": 3})  # looks like an option to argparse
        self.assertEqual(rc.call_args.args[0][-2:], ["--", "-x?behind"])
        self.assertIn(f"{JOB} (this job) · Kai Cenat Live", out.content)
        self.assertIn("live-xqc-1 · xQc · 21:04:10 · 812.2-815.0s · (footage deleted)", out.content)
        self.assertTrue(T.run_tool(ctx, "find_line", {"pattern": "[" }).is_error)

    def test_check_runs_clipper_check_on_the_job(self):
        ctx = self.ctx()
        bad = T.run_tool(ctx, "check", {"picks": [pick(clip_type="vlog")]})
        self.assertTrue(bad.is_error)  # format problems come back before the check runs
        self.assertIn("didn't run", bad.content)
        draft = {k: v for k, v in pick().items() if k not in ("why", "click_trigger")}
        out = T.run_tool(ctx, "check", {"picks": [draft]})
        self.assertFalse(out.is_error, out.content)
        self.assertTrue(out.content.startswith("no fails"), out.content)
        vague = dict(draft, hooks=["Wait for it...", "Watch this", "Kai can't stop *laughing*"])
        self.assertIn("FAILS", T.run_tool(ctx, "check", {"picks": [vague]}).content)

    def test_submit_runs_the_real_gate_and_returns_render_ready_picks(self):
        ctx = self.ctx()
        out = T.submit(ctx, {"picks": [pick()]}, round_no=1, max_rounds=3)
        self.assertIsInstance(out, T.Submission, getattr(out, "content", None))
        self.assertEqual(set(out.picks[0]) - set(T.PICK_KEYS), set())
        self.assertNotIn("why", out.picks[0])
        bad = T.submit(ctx, {"picks": [pick(hooks=["Wait for it...", "Watch this", "Kai can't stop *laughing*"])]},
                       round_no=2, max_rounds=3)
        self.assertTrue(bad.is_error)
        self.assertIn("fix round 2 of 3", bad.content)
        self.assertIn("vague", bad.content)

    def test_dub_validate_is_reachable_through_the_bridge_subprocess(self):
        job = make_job(self.root / "jobs", "agent-dub-run", picks=[{"start": 10, "end": 31, **{
            k: SEGMENTS["english"][k] for k in ("hooks", "titles", "description")}}])
        out = runtime.run_bridge("dub_validate", {"job": str(job), "pick": 1, "lang": "es", "n_lines": 1,
                                                  "translation": {"lang": "es", **GOOD_ES}}, "gta6")
        self.assertEqual(out["errors"], ["need 1 lines (one per segment), got 2"])

    def test_simulate_formats_the_ranking_and_keep_line(self):
        ctx = self.ctx()
        ranking = {"picks": [{"hooks": ["B", "A"], "titles": ["tb", "ta"]}],
                   "ranking": [{"video": 1, "keep": 3, "ok": True, "keep_line": "keep (top 3, different types): B "
                                                                                "[question] | A [tease]",
                                "ranking": [{"x": 1.31, "low": 1.1, "high": 1.6, "wins": 0.55, "hook": "B",
                                             "why": ["question"], "check": []},
                                            {"x": 1.1, "low": 0.9, "high": 1.3, "wins": 0.45, "hook": "A",
                                             "why": ["tease"], "check": ["vague"]}], "titles_dropped": ["tc"]}]}
        with mock.patch.object(runtime, "run_bridge", return_value=ranking) as rb:
            out = T.run_tool(ctx, "simulate", {"picks": [{"hooks": ["A", "B"], "titles": ["ta", "tb"],
                                                          "clip_type": "reaction", "start": 1}]})
        op, payload = rb.call_args.args[:2]
        self.assertEqual((op, payload["keep"]), ("rank", 5))
        self.assertNotIn("start", payload["picks"][0])
        self.assertIn("video 1 (reaction): keep (top 3", out.content)
        self.assertIn("1.31x (1.10-1.60) wins 55% · B · question", out.content)
        self.assertIn("FAILS check: vague", out.content)
        self.assertIn('"hooks": ["B", "A"]', out.content)
        self.assertIn("dropped", out.content)


# ---------------------------------------------------------------- dub translation

SEGMENTS = {"segments": [{"i": 0, "s": 0.0, "e": 1.5, "text": "Oh my god!", "spk": 0, "voice": "low",
                          "energy": "loud", "hz": 120, "db": -10},
                         {"i": 1, "s": 1.6, "e": 3.0, "text": "[screaming] He's behind me!", "spk": 0, "voice": "low",
                          "energy": "loud", "hz": 120, "db": -8}],
            "english": {"hooks": ["Why did Kai *run*?", "Kai NEVER saw it", "Me when *GTA 6* drops"],
                        "titles": ["Why Did Kai Run In GTA 6?", "Kai NEVER Saw It In GTA 6", "Me When GTA 6 Drops"],
                        "description": "Kai runs."},
            "template": {}}
GOOD_ES = {"lines": ["[screaming] ¡Dios mío!", "[screaming] ¡Está detrás de mí!"],
           "hooks": ["¿Por qué *corrió* Kai?", "Kai NUNCA lo vio", "Yo cuando sale *GTA 6*"],
           "titles": ["¿Por Qué Corrió Kai En GTA 6?", "Kai NUNCA Lo Vio En GTA 6", "Yo Cuando Sale GTA 6"],
           "description": "Kai sale corriendo.", "hashtags": ["#gta6", "#shorts", "#gta6español"],
           "credit": "Clip de", "disclosure": "Doblaje con IA a partir del audio original."}


class DubTests(FakeTools):
    def translate(self, replies, validator=None):
        client = FakeClient([msg([thinking(), text(json.dumps(r))], stop="end_turn", usage=usage())
                             for r in replies])
        calls = []

        def bridge(op, payload, profile, **kw):
            calls.append((op, payload))
            return {"errors": validator.pop(0) if validator else []}
        with mock.patch.object(agent, "make_client", return_value=client), \
                mock.patch.object(runtime, "run_bridge", side_effect=bridge):
            out = agent.translate_dub(self.job_id, 1, "es", SEGMENTS)
        return out, client, calls

    def test_a_valid_translation_comes_back_in_the_render_shape(self):
        out, client, calls = self.translate([GOOD_ES])
        self.assertEqual(out, {"lang": "es", **GOOD_ES})
        params = client.calls[0]
        self.assertEqual(params["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("tools", params)
        self.assertIn("Spanish", params["system"])
        self.assertIn('"energy": "loud"', params["messages"][0]["content"])
        self.assertEqual(calls[0][0], "dub_validate")
        self.assertEqual((calls[0][1]["pick"], calls[0][1]["n_lines"]), (1, 2))
        self.assertEqual(store.recent_usage(1)[0]["purpose"], "dub_translate")

    def test_an_invalid_translation_is_retried_once_with_the_validator_message(self):
        short = dict(GOOD_ES, lines=["¡Dios mío!"])
        out, client, _ = self.translate([short, GOOD_ES])
        self.assertEqual(out["lines"], GOOD_ES["lines"])
        retry = client.calls[1]["messages"][-1]["content"]
        self.assertIn("need exactly 2", retry)
        out, client, _ = self.translate([GOOD_ES, GOOD_ES], validator=[["hook is still the English text"]])
        self.assertIn("hook is still the English text", client.calls[1]["messages"][-1]["content"])
        with self.assertRaises(agent.AgentError) as ctx:
            self.translate([short, dict(GOOD_ES, lines=["¡Dios mío!", "¡Está detrás de mí!"])])
        self.assertEqual(ctx.exception.code, "dub_invalid")
        self.assertIn("[screaming]", str(ctx.exception))

    def test_local_checks_catch_english_text_and_missing_hooks(self):
        errs = agent.dub_errors(dict(GOOD_ES, hooks=["Why did Kai *run*?", "x", "y"], titles=["a", "b"]),
                                SEGMENTS["segments"], SEGMENTS["english"])
        self.assertTrue(any("one per hook" in e for e in errs))
        errs = agent.dub_errors(dict(GOOD_ES, hooks=["Why did Kai *run*?", "x", "y"]), SEGMENTS["segments"],
                                SEGMENTS["english"])
        self.assertTrue(any("still the English" in e for e in errs))

    def test_the_bridge_op_runs_clippers_validator(self):
        from automate import bridge
        from automate.bridge_ext_agent import dub_validate
        from clipper import config
        self.assertIs(bridge.OPS["dub_validate"], dub_validate)  # registered on import
        job = make_job(self.root / "jobs", "agent-dub", picks=[{"start": 10, "end": 31, **{
            k: SEGMENTS["english"][k] for k in ("hooks", "titles", "description")}}])
        out = bridge.OPS["dub_validate"]({"job": str(job), "pick": 1, "lang": "es", "n_lines": 2,
                                          "translation": {"lang": "es", **GOOD_ES}}, config.load(profile="gta6"))
        self.assertEqual(out, {"errors": []})
        english = {"lang": "es", **dict(GOOD_ES, hooks=SEGMENTS["english"]["hooks"])}
        bad = bridge.OPS["dub_validate"]({"job": str(job), "pick": 1, "lang": "es", "n_lines": 3,
                                          "translation": english}, config.load(profile="gta6"))
        self.assertTrue(any("need 3 lines" in e for e in bad["errors"]))
        self.assertTrue(any("still the English" in e for e in bad["errors"]))


# ---------------------------------------------------------------- key test

class KeyTests(TempData):
    def run_test(self, script, key=None):
        client = FakeClient(script)
        with mock.patch.object(agent, "make_client", return_value=client):
            return agent.test_key(key), client

    def test_outcomes(self):
        (ok, text_), client = self.run_test([msg([text("OK")], stop="end_turn", usage=usage(inp=12, out=3))])
        self.assertTrue(ok)
        self.assertIn("funktioniert", text_)
        self.assertEqual(client.calls[0]["max_tokens"], agent.TEST_MAX_TOKENS)
        row = store.recent_usage(1)[0]
        self.assertEqual((row["purpose"], row["job_id"], row["input_tokens"], row["request_id"]),
                         ("test", None, 12, "req_create"))
        (ok, text_), _ = self.run_test([http_error(anthropic.AuthenticationError, 401)], key="sk-ant-wrong")
        self.assertFalse(ok)
        self.assertIn("abgelehnt", text_)
        (ok, text_), _ = self.run_test([http_error(anthropic.RateLimitError, 429)])
        self.assertTrue(ok)
        (ok, text_), _ = self.run_test([http_error(anthropic.InternalServerError, 500)])
        self.assertFalse(ok)
        store.set_secret("anthropic_api_key", None)
        self.assertEqual(agent.test_key(), (False, "Kein Anthropic-API-Schlüssel gespeichert."))

    def test_with_the_budget_spent_only_the_free_lookup_runs(self):
        store.set_settings({"claude.daily_limit_eur": 0.5})
        store.add_usage(None, "select", "claude-sonnet-5-5", input_tokens=1_000_000)
        (ok, text_), client = self.run_test([SimpleNamespace(id="claude-sonnet-5-5")])
        self.assertTrue(ok)
        self.assertEqual(client.calls, [{"retrieve": "claude-sonnet-5-5"}])
        self.assertIn("Tageslimit", text_)


# ---------------------------------------------------------------- the real SDK on a mock transport

def sse(blocks, stop="tool_use", usage_=None, model="claude-sonnet-5-5"):
    """A Messages API event stream for `blocks` ({"type": "thinking"} / text / tool_use with its input)."""
    u = usage_ or {"input_tokens": 12, "output_tokens": 1, "cache_creation_input_tokens": 300,
                   "cache_read_input_tokens": 0}
    events = [{"type": "message_start", "message": {"id": "msg_w", "type": "message", "role": "assistant",
                                                    "model": model, "content": [], "stop_reason": None,
                                                    "stop_sequence": None, "usage": u}}]
    for i, b in enumerate(blocks):
        if b["type"] == "thinking":
            events += [{"type": "content_block_start", "index": i,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                       {"type": "content_block_delta", "index": i,
                        "delta": {"type": "signature_delta", "signature": f"sig{i}"}}]
        elif b["type"] == "text":
            events += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}]
        else:
            events += [{"type": "content_block_start", "index": i, "content_block": {
                           "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}},
                       {"type": "content_block_delta", "index": i, "delta": {
                           "type": "input_json_delta", "partial_json": json.dumps(b["input"])}}]
        events.append({"type": "content_block_stop", "index": i})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 140}}, {"type": "message_stop"}]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
    return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_wire"}, content=body)


def sdk_client(responses, seen):
    def handler(request):
        seen.append((request.headers, json.loads(request.content)))
        return responses.pop(0)
    return anthropic.Anthropic(api_key=KEY, max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


class SdkWireTests(FakeTools):
    """The installed SDK builds, sends and parses what the loop expects (no network: a mock transport)."""

    def test_the_select_loop_through_the_sdk(self):
        seen = []
        client = sdk_client([sse([{"type": "thinking"}, {"type": "text", "text": "Reading."},
                                  {"type": "tool_use", "id": "toolu_a", "name": "grep_transcript",
                                   "input": {"around_s": 11}}]),
                             sse([{"type": "thinking"}, {"type": "tool_use", "id": "toolu_b", "name": "submit_picks",
                                                         "input": {"picks": [pick()]}}])], seen)
        with mock.patch.object(agent, "make_client", return_value=client):
            sel = agent.select_picks(self.job_id, max_picks=2)
        self.assertEqual(sel.picks[0]["hooks"], pick()["hooks"])
        (h1, b1), (h2, b2) = seen
        self.assertIn("server-side-fallback-2026-07-01", h1["anthropic-beta"])
        self.assertEqual(h1["authorization"], f"Bearer {KEY}")  # the stored key, passed explicitly
        self.assertTrue(b1["stream"])
        self.assertEqual((b1["fallbacks"], b1["thinking"], b1["output_config"], b1["tool_choice"]),
                         ("default", agent.THINKING, {"effort": "medium"}, {"type": "auto"}))
        self.assertEqual(b1["cache_control"], {"type": "ephemeral"})
        self.assertEqual(b1["system"][1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual(b1["tools"][0]["strict"], True)
        self.assertEqual(b1["messages"][0]["content"][4]["type"], "image")
        # the assistant turn goes back exactly as it came (thinking signature included), then the tool result
        self.assertEqual(b2["messages"][1]["content"], [
            {"type": "thinking", "thinking": "", "signature": "sig0"}, {"type": "text", "text": "Reading."},
            {"type": "tool_use", "id": "toolu_a", "name": "grep_transcript", "input": {"around_s": 11}}])
        self.assertEqual(b2["messages"][2]["content"][0]["tool_use_id"], "toolu_a")
        self.assertEqual(json.dumps(b1["messages"][0]), json.dumps(b2["messages"][0]))  # append-only history
        self.assertEqual(json.dumps([b1["tools"], b1["system"]]), json.dumps([b2["tools"], b2["system"]]))
        rows = store.recent_usage(5)[::-1]
        self.assertEqual([(r["request_id"], r["input_tokens"], r["output_tokens"], r["cache_creation_tokens"])
                          for r in rows], [("req_wire", 12, 140, 300)] * 2)

    def test_the_dub_translation_through_the_sdk(self):
        seen = []
        client = sdk_client([sse([{"type": "thinking"}, {"type": "text", "text": json.dumps(GOOD_ES)}],
                                 stop="end_turn")], seen)
        with mock.patch.object(agent, "make_client", return_value=client), \
                mock.patch.object(runtime, "run_bridge", return_value={"errors": []}):
            out = agent.translate_dub(self.job_id, 1, "es", SEGMENTS)
        self.assertEqual(out["lines"], GOOD_ES["lines"])
        body = seen[0][1]
        self.assertEqual(body["output_config"]["format"], {"type": "json_schema", "schema": prompts.DUB_SCHEMA})

    def test_the_key_test_through_the_sdk(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx2.Response(200, headers={"request-id": "req_key"}, json={
                "id": "msg_k", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 14, "output_tokens": 2}})
        client = anthropic.Anthropic(api_key=KEY, max_retries=0,
                                     http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with mock.patch.object(agent, "make_client", return_value=client):
            self.assertEqual(agent.test_key()[0], True)
        self.assertEqual(seen[0]["max_tokens"], agent.TEST_MAX_TOKENS)
        row = store.recent_usage(1)[0]
        self.assertEqual((row["purpose"], row["request_id"], row["input_tokens"]), ("test", "req_key", 14))

        def unauthorized(request):
            return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                         "message": "invalid x-api-key"}})
        client = anthropic.Anthropic(api_key=KEY, max_retries=0,
                                     http_client=httpx2.Client(transport=httpx2.MockTransport(unauthorized)))
        with mock.patch.object(agent, "make_client", return_value=client):
            ok, message = agent.test_key()
        self.assertFalse(ok)
        self.assertIn("abgelehnt", message)


# ---------------------------------------------------------------- --dry-run

class DryRunTests(FakeTools):
    def test_prints_the_request_without_a_client(self):
        out = StringIO()
        dump = self.root / "request.json"
        with mock.patch.object(agent, "make_client", side_effect=AssertionError("no client in a dry run")), \
                mock.patch("sys.stdout", out):
            self.assertEqual(agent.main(["--dry-run", JOB, "--dump", str(dump), "--avoid", "Kai was NOT ready"]), 0)
        text_ = out.getvalue()
        for want in ("model claude-sonnet-5-5", "grep_transcript (strict)", "cache_control {'type': 'ephemeral', "
                     "'ttl': '1h'}", "image 3:", "prefix byte-stable across builds: yes", "fallbacks 'default'"):
            self.assertIn(want, text_)
        req = json.loads(dump.read_text())
        self.assertIn("base64 chars", json.dumps(req["messages"]))
        self.assertIn('"Kai was NOT ready" (don\'t start a hook', req["messages"][0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
