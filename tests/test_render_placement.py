"""Caption and hook placement (clipper.placement), caption looks, `clipper check` of the new keys, and an end-to-end
render + preview on a tiny synthetic source (skipped without ffmpeg, like CI)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clipper import captions, hooks, placement as P

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FH, FW = P.FRAME_SIZE[1], P.FRAME_SIZE[0]


def canvas_frames(n=8, busy=(), skin_rows=(10, 130), seed=0, base=90):
    """Synthetic split canvases at the analysis scale: a facecam (wall + skin-coloured face) above the seam, flat
    game below, and noise that changes every frame (motion and detail) in each canvas-row band of `busy`."""
    rng = np.random.default_rng(seed)
    out = np.full((n, FH, FW, 3), base, np.uint8)
    out[:, :P.SEAM // P.SCALE] = (60, 48, 70)
    a, b = skin_rows
    out[:, a:b, FW // 4: 3 * FW // 4] = (196, 140, 108)
    for y0, y1 in busy:
        out[:, y0 // P.SCALE:y1 // P.SCALE] = rng.integers(0, 255, (n, y1 // P.SCALE - y0 // P.SCALE, FW, 3),
                                                             dtype=np.uint8)
    return out


def split_pick(**extra):
    return {"start": 0.0, "end": 20.0, "layout": {"type": "split", "face": [0.7, 0.6, 0.3, 0.35]},
            "clip_type": "reaction", "hook_style": "bold", "caption_style": "boxed",
            "hooks": ["Kai was NOT ready for *this*"], **extra}


class GeometryTests(unittest.TestCase):
    def test_fit_game_rect_follows_the_source_shape(self):
        self.assertEqual(P.geometry("fit", (1920, 1080))["game"], (656, 1264))
        self.assertEqual(P.geometry("fit", (1440, 1080))["game"], (555, 1365))
        self.assertEqual(P.geometry("split")["game"], (768, 1920))

    def test_layout_name_matches_the_segment_join(self):
        self.assertEqual(P.layout_name({"layout": {"type": "split"}}), "split")
        self.assertEqual(P.layout_name({"layout": {"type": "vertical", "as": "split"}}), "split")
        self.assertEqual(P.layout_name({"segments": [{"start": 0, "end": 1},
                                                     {"start": 2, "end": 3, "layout": {"type": "split"}}]}), "split")
        self.assertEqual(P.layout_name({"segments": [{"start": 0, "end": 1}]}), "fit")

    def test_width_clears_the_button_column_only_where_it_reaches(self):
        self.assertEqual(P.max_width(400, 600), 960)
        self.assertEqual(P.max_width(900, 1000), 2 * (P.BUTTONS_X - 540))

    def test_worst_case_block_stays_in_the_safe_area_and_off_the_face(self):
        """Two speakers, two long lines each, the largest size, every slot of every layout, with and without a
        hook: never under the platform bars or buttons, never above the face guard, never off the canvas."""
        state = ((0, ("UNBELIEVABLE", "ABSOLUTELY", "INCREDIBLE"), 1), (1, ("WHAT", "ARE", "YOU", "DOING"), 0))
        for layout in ("fit", "split"):
            for reserve in (None, [640, 900] if layout == "split" else [250, 520]):
                geo = P.geometry(layout)
                for name, slot in P.slots(geo, reserve).items():
                    for look in captions.LOOKS:
                        plan = {"look": look, "scale": 1.0, "anchor": slot["anchor"], "y": slot["y"],
                                "top": slot["top"], "bottom": slot["bottom"], "stack": slot["stack"],
                                "max_width": P.max_width(slot["top"], slot["bottom"])}
                        blocks, tops = captions.layout_state(state, plan, True)
                        for b, t in zip(blocks, tops):
                            where = f"{layout} {name} {look} hook={reserve}"
                            self.assertGreaterEqual(t, P.SAFE_TOP, where)
                            self.assertLessEqual(t + b["h"], P.SAFE_BOTTOM, where)
                            if layout == "split":
                                self.assertGreater(t, P.FACE_GUARD, where)
                            if reserve:
                                self.assertGreater(t, reserve[1], where)
                            for line in b["lines"]:
                                w = b["font"].getlength(" ".join(x for _, x in line))
                                self.assertLessEqual(w, plan["max_width"] + 1, where)
                                if t + b["h"] > P.BUTTONS_Y:
                                    self.assertLessEqual(540 + w / 2, P.BUTTONS_X + 1, where)


class DecisionTests(unittest.TestCase):
    def plan(self, pick, frames, cfg=None, hooks=None):
        return P.plan(pick, cfg or {}, frames, (1920, 1080), pick.get("hooks") if hooks is None else hooks, 20.0,
                      "clip-a")

    def test_split_takes_the_calm_band_under_the_seam(self):
        p = self.plan(split_pick(), canvas_frames(busy=[(1250, 1480)]))
        self.assertEqual((p["captions"]["position"], p["captions"]["reason"]), ("high", "quietest"))
        self.assertEqual(p["captions"]["size"], "small")  # on the game: small
        self.assertIn("oben im Spielbild, klein – ruhigster Bereich", p["text_de"])

    def test_a_busy_top_moves_captions_down_despite_the_player_prior(self):
        p = self.plan(split_pick(), canvas_frames(busy=[(900, 1250)]))
        self.assertEqual((p["captions"]["position"], p["captions"]["reason"]), ("low", "quietest"))

    def test_without_a_hook_the_seam_is_the_default_and_wins_ties(self):
        p = self.plan(split_pick(), canvas_frames(), hooks=[])
        self.assertEqual(p["captions"]["position"], "seam")
        self.assertIsNone(p["hook"]["reserve"])
        self.assertEqual(p["captions"]["size"], "medium")
        lo, hi = p["captions"]["span"]
        self.assertGreater(lo, p["face_guard"])

    def test_no_frames_falls_back_to_the_layout_default(self):
        p = self.plan(split_pick(), None)
        self.assertEqual((p["captions"]["position"], p["captions"]["reason"]), ("high", "no_frames"))
        f = self.plan({**split_pick(), "layout": {"type": "fit"}}, None)
        self.assertEqual((f["captions"]["position"], f["captions"]["reason"]), ("low", "no_frames"))

    def test_the_same_clip_always_gets_the_same_plan(self):
        frames = canvas_frames(busy=[(1250, 1480)], seed=3)
        self.assertEqual(self.plan(split_pick(), frames), self.plan(split_pick(), frames.copy()))

    def test_explicit_position_and_size_win_over_auto(self):
        frames = canvas_frames(busy=[(1250, 1480)])
        p = self.plan(split_pick(caption_position="low", caption_size="large"), frames)
        self.assertEqual((p["captions"]["position"], p["captions"]["size"], p["captions"]["reason"]),
                         ("low", "large", "pick"))
        p = self.plan(split_pick(), frames, cfg={"caption_position": "low"})
        self.assertEqual((p["captions"]["position"], p["captions"]["reason"]), ("low", "config"))
        p = self.plan(split_pick(caption_position="auto"), frames, cfg={"caption_position": "low"})
        self.assertEqual(p["captions"]["position"], "high")  # the pick's auto beats the config default
        p = self.plan(split_pick(caption_position="seam"), frames)  # the hook sits on the seam
        self.assertEqual((p["captions"]["position"], p["captions"]["reason"]), ("high", "seam_taken"))

    def test_a_static_overlay_counts_against_a_band(self):
        frames = canvas_frames()
        hud = np.random.default_rng(1).integers(0, 255, (60 // P.SCALE * 4, FW, 3), dtype=np.uint8)
        frames[:, 940 // P.SCALE:(940 + 240) // P.SCALE] = np.resize(hud, (240 // P.SCALE, FW, 3))  # same every frame
        maps = P.analyse(frames)
        self.assertGreater(maps["static"][950 // P.SCALE:1100 // P.SCALE].mean(), 0.5)
        p = self.plan(split_pick(), frames)
        self.assertEqual(p["captions"]["position"], "low")

    def test_face_guard_moves_down_for_a_low_face_and_never_up(self):
        low = P.analyse(canvas_frames(skin_rows=(20, 180)))
        self.assertGreater(P.face_guard(low), P.FACE_GUARD)
        self.assertLessEqual(P.face_guard(low), P.SEAM)
        high = P.analyse(canvas_frames(skin_rows=(10, 60)))
        self.assertEqual(P.face_guard(high), P.FACE_GUARD)
        p = self.plan(split_pick(), canvas_frames(skin_rows=(20, 180)))
        self.assertGreater(p["hook"]["reserve"][0], P.face_guard(low))

    def test_fit_captions_stay_on_the_blur_and_the_hook_above_the_game(self):
        frames = canvas_frames()
        p = self.plan({"start": 0, "end": 20, "layout": {"type": "fit"}, "hooks": ["How did the *plane* explode?"],
                       "hook_style": "label", "caption_style": "comic", "clip_type": "gameplay"}, frames)
        self.assertEqual(p["captions"]["position"], "low")
        lo, hi = p["captions"]["span"]
        self.assertGreaterEqual(lo, 1264)
        self.assertLessEqual(hi, P.SAFE_BOTTOM)
        self.assertLessEqual(p["hook"]["reserve"][1], 656)
        self.assertGreaterEqual(p["hook"]["reserve"][0], P.SAFE_TOP)
        self.assertIsNone(p["hook"]["seconds"])  # on the blur it covers nothing: pinned
        self.assertEqual(p["hook"]["size"], "large")


class HookTimingTests(unittest.TestCase):
    def seconds(self, cfg=None, layout="split", dur=20.0, **pick):
        base = split_pick(**pick)
        if layout == "fit":
            base["layout"] = {"type": "fit"}
        return P.plan(base, cfg or {}, None, (1920, 1080), base["hooks"], dur, "k")["hook"]["seconds"]

    def test_auto_follows_the_clip_type_in_split_and_pins_in_fit(self):
        self.assertEqual(self.seconds(clip_type="reaction"), 3.0)
        self.assertEqual(self.seconds(clip_type="gameplay"), 4.0)
        self.assertEqual(self.seconds(clip_type="comedy"), 5.0)
        self.assertIsNone(self.seconds(clip_type="news"))
        self.assertIsNone(self.seconds(clip_type="reaction", layout="fit"))

    def test_explicit_and_config_values(self):
        self.assertEqual(self.seconds(hook_seconds=6), 6.0)
        self.assertIsNone(self.seconds(hook_seconds="whole"))
        self.assertEqual(self.seconds(cfg={"hook_seconds": 2.5}), 2.5)
        self.assertIsNone(self.seconds(hook_seconds=6, dur=5.0))  # longer than the clip: the whole clip

    def test_reserve_is_the_tallest_variant_and_none_without_a_banner(self):
        short, long_ = "Why did Kai *run*?", "Kai was NOT ready for the giant exploding *bridge* tonight"
        base = split_pick(hooks=[short, long_])
        both = P.plan(base, {}, None, (1920, 1080), [short, long_], 20.0, "k")["hook"]["reserve"]
        one = P.plan(base, {}, None, (1920, 1080), [short], 20.0, "k")["hook"]["reserve"]
        self.assertGreater(both[1] - both[0], one[1] - one[0])
        self.assertIsNone(P.plan(base, {}, None, (1920, 1080), [""], 20.0, "k")["hook"]["reserve"])
        self.assertIsNone(P.plan({**base, "hook_style": "none"}, {}, None, (1920, 1080), [short], 20.0,
                                 "k")["hook"]["reserve"])


class LookTests(unittest.TestCase):
    def look(self, key, cfg=None, **pick):
        return P.plan(split_pick(**pick), cfg or {}, None, (1920, 1080), [], 20.0, key)["captions"]["look"]

    def test_deterministic_per_clip_and_varied_across_clips(self):
        self.assertEqual(self.look("vid:12.00"), self.look("vid:12.00"))
        seen = {self.look(f"vid:{s}.00") for s in range(40)}
        self.assertEqual(seen, set(P.family_looks("boxed")))  # every boxed look shows up, nothing else

    def test_config_restricts_or_turns_variation_off(self):
        self.assertEqual({self.look(f"v:{s}", {"caption_looks": ["boxed-word"]}) for s in range(10)}, {"boxed-word"})
        self.assertEqual({self.look(f"v:{s}", {"caption_look": "classic"}) for s in range(10)}, {"boxed"})
        self.assertEqual(self.look("v:1", caption_look="comic-word"), "comic-word")

    def test_thin_outline_only_over_calm_dark_footage(self):
        pick = {**split_pick(caption_style="bold"), "caption_look": None, "caption_position": "high"}
        keys = [k for k in (f"v:{s}" for s in range(60)) if
                P.look_for(pick, {}, "bold", k)[0] == "bold-clean"]
        self.assertTrue(keys)
        busy = canvas_frames(busy=[(780, 1480)], base=230)
        p = P.plan(pick, {}, busy, (1920, 1080), [], 20.0, keys[0])
        self.assertEqual(p["captions"]["look"], "bold")
        calm = canvas_frames(base=40)
        self.assertEqual(P.plan(pick, {}, calm, (1920, 1080), [], 20.0, keys[0])["captions"]["look"], "bold-clean")

    def test_one_word_looks_chunk_one_word_at_a_time(self):
        words = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in enumerate("one two three four".split())]
        states = captions.caption_states(words, 0.0, 2.0, captions.LOOKS["boxed-word"]["words"])
        self.assertTrue(all(len(ws) <= 1 for _, _, st in states for _, ws, _ in st))
        self.assertEqual(max(len(ws) for _, _, st in captions.caption_states(words, 0.0, 2.0)
                             for _, ws, _ in st), 3)


class ClassicTests(unittest.TestCase):
    def test_classic_settings_restore_the_old_line_and_size(self):
        cfg = {"caption_position": "classic", "caption_size": "large", "caption_look": "classic",
               "hook_size": "large", "hook_seconds": "whole"}
        for layout, y in (("split", 1380), ("fit", 1420)):
            pick = {**split_pick(), "layout": {"type": layout}, "hooks": []}
            c = P.plan(pick, cfg, None, (1920, 1080), [], 20.0, "k")
            self.assertEqual((c["captions"]["y"], c["captions"]["anchor"], c["captions"]["scale"],
                              c["captions"]["look"], c["captions"]["stack"]), (y, "centre", 1.0, "boxed", "up"))
            self.assertEqual((c["hook"]["scale"], c["hook"]["seconds"]), (1.0, None))

    def test_no_plan_draws_exactly_the_classic_place(self):
        plan = captions.classic_caption_plan("split", "boxed")
        self.assertEqual((plan["y"], plan["scale"], plan["max_width"]), (1380, 1.0, 960))
        with tempfile.TemporaryDirectory() as d:
            spans = captions.render_state(((0, ("NO", "WAY"), 0),), Path(d) / "a.png", plan, False)
        h = int(116 * 1.18)
        self.assertEqual(spans, [(1380 - h // 2, 1380 - h // 2 + h)])


class CheckTests(unittest.TestCase):
    def report(self, **extra):
        return hooks.check_picks([{"start": 0, "end": 10, "hooks": ["Why did Kai *run*?"], **extra}],
                                 {"research": "gta"})

    def test_new_keys_are_validated_with_their_allowed_values(self):
        for key, bad in (("caption_position", "middle"), ("caption_size", "huge"), ("caption_look", "neon"),
                         ("hook_size", 3), ("hook_seconds", 0), ("hook_seconds", "long"), ("sound_level", "max")):
            rep = self.report(**{key: bad})
            self.assertTrue(hooks.has_fails(rep), key)
            self.assertIn(key, rep["video 1"]["style"][0])
            self.assertIn("isn't one of: " + ("off" if key == "sound_level" else "auto"), rep["video 1"]["style"][0])
        rep = self.report(caption_position="seam", caption_size="small", caption_look="boxed-word",
                          hook_size="medium", hook_seconds=4, sound_level="strong", caption_style="boxed")
        self.assertNotIn("style", rep.get("video 1", {}))

    def test_look_from_another_family_and_bad_fx_sound_fail(self):
        rep = self.report(caption_style="boxed", caption_look="comic-word")
        self.assertIn("belongs to caption_style 'comic'", rep["video 1"]["style"][0])
        rep = self.report(fx=[{"t": 5, "type": "hit", "sound": "airhorn"}, {"t": 6, "type": "hit", "sound": "riser"},
                              {"t": 7, "type": "hit", "sound": "braam"}, {"t": 8, "type": "spin"}])
        self.assertEqual(len(rep["video 1"]["fx"]), 2)
        self.assertIn("'airhorn' isn't a sound id", rep["video 1"]["fx"][0])
        self.assertIn("impact, riser, whoosh, tension", rep["video 1"]["fx"][0])
        self.assertIn("'spin'", rep["video 1"]["fx"][1])
        rep = self.report(music=[{"s": 1, "e": 5, "mood": "happy"}])
        self.assertIn("mood 'happy'", rep["video 1"]["style"][0])


def make_job(root, seconds=6.0):
    """A tiny job: 640x360 moving test picture with a coloured 'facecam' box, speech-like tone bursts and a loud
    burst at 3.5 s; the transcript has a shout on it."""
    job = root / "tiny"
    job.mkdir()
    audio = ("aevalsrc='(0.08*sin(2*PI*180*t)*gt(sin(2*PI*3*t),0)*lt(t,3.4)"
             "+0.8*sin(2*PI*90*t)*exp(-2*(t-3.5))*gte(t,3.5))':s=44100:d=" + str(seconds))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:d={seconds}",
                    "-f", "lavfi", "-i", audio, "-vf", "drawbox=x=460:y=230:w=170:h=120:color=0xC48C6C:t=fill",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(job / "source.mp4")], check=True)
    (job / "job.json").write_text(json.dumps({"id": "tiny", "channel": "Kai", "url": "https://www.youtube.com/watch?v=t",
                                              "title": "t"}))
    words = [{"w": w, "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i, w in enumerate("yo look at this car".split())]
    words += [{"w": "NO", "s": 3.5, "e": 3.8}, {"w": "WAY!", "s": 3.8, "e": 4.2}]
    (job / "transcript.json").write_text(json.dumps({"words": words}))
    return job


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class EndToEndTests(unittest.TestCase):
    """One split render and its preview on a tiny synthetic source: the output contract (1080x1920, audio, about
    -14 LUFS), the hook leaving after its seconds, and the preview making the render's own decision."""

    @classmethod
    def setUpClass(cls):
        from clipper import render
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.job = make_job(root)
        cls.pick = {"start": 0.0, "end": 6.0, "clip_type": "reaction", "hook_style": "box", "caption_style": "boxed",
                    "layout": {"type": "split", "face": [0.72, 0.64, 0.26, 0.33]},
                    "hooks": ["Why did Kai *scream*?"], "titles": ["Why Did Kai Scream In GTA 6?"],
                    "description": "d", "hashtags": ["#gta6"]}
        cls.cfg = {"no_hook_share": 0, "separate_voices": False, "caption_look": "boxed-word"}
        cls.res = render.render(cls.job, [cls.pick], cls.cfg, out_dir=root / "out")[0]
        cls.mp4 = cls.res["variants"][0]["file"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def frame(self, t):
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", self.mp4, "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)

    def test_output_contract(self):
        check = self.res["variants"][0]["check"]
        self.assertEqual((check["width"], check["height"]), (1080, 1920))
        self.assertAlmostEqual(check["lufs"], -14.0, delta=1.0)
        manifest = json.loads((Path(self.res["folder"]) / "upload.json").read_text())
        self.assertEqual(manifest[0]["placement"], self.res["placement"])
        self.assertIn("text_de", manifest[0]["sound"])
        self.assertEqual(self.res["placement"]["hook"]["seconds"], 3.0)

    def test_the_hook_box_leaves_after_its_seconds(self):
        top, bottom = self.res["placement"]["hook"]["span"]
        band = slice(top + 4, bottom - 4), slice(P.W - P.BUTTONS_X + 10, P.BUTTONS_X - 10)
        early, late = self.frame(1.0)[band], self.frame(4.5)[band]
        self.assertGreater((early > 240).all(2).mean(), 0.5)  # the white meme box
        self.assertLess((late > 240).all(2).mean(), 0.2)

    def test_the_shout_gets_a_scored_beat(self):
        beats = self.res["sound"]["beats"]
        self.assertEqual(len(beats), 1)
        self.assertAlmostEqual(beats[0]["t"], 3.5, delta=0.15)
        roles = {l["role"] for l in self.res["sound"]["layers"]}
        self.assertEqual(roles, {"riser", "impact"})
        riser = next(l for l in self.res["sound"]["layers"] if l["role"] == "riser")
        self.assertAlmostEqual(riser["end"], beats[0]["t"], places=2)  # the build-up ends on the beat

    def test_preview_makes_the_same_decision(self):
        from PIL import Image
        from clipper import preview
        out = Path(self.tmp.name) / "p.png"
        r = preview.preview(self.job, [self.pick], 1, None, 1, out, self.cfg)
        self.assertEqual(r["placement"], self.res["placement"])
        self.assertTrue(r["hook_visible"])
        with Image.open(out) as im:
            self.assertEqual(im.size, (1080, 1920))
        late = preview.preview(self.job, [self.pick], 1, 5.0, 1, out, self.cfg)
        self.assertFalse(late["hook_visible"])


if __name__ == "__main__":
    unittest.main()
