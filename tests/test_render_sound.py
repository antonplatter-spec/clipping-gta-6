"""Sound on epic beats: finding the beats (clipper.sound), placing sounds by role so a build-up ends on its beat
(clipper.music), the level settings, owner sounds, and the shipped sound set (loudness checks need ffmpeg)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from clipper import hooks, music, render, sound

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def W(text, s, e, **k):
    return {"w": text, "s": s, "e": e, **k}


def talk(seconds, hop=sound.HOP, level=-30.0, seed=0):
    """A 50 ms level curve of ordinary talk: syllables swing +-8 dB around `level`."""
    rng = np.random.default_rng(seed)
    n = int(seconds / hop)
    return level + 8 * np.sin(np.arange(n) * 1.9) + rng.normal(0, 2, n)


def lib(folder, extra=""):
    folder = Path(folder)
    for name in ("rise.wav", "slam.wav", "boom.wav", "pulse.wav"):
        (folder / name).write_bytes(b"x")
    (folder / "library.yaml").write_text(
        "tracks:\n"
        "  - {id: rise, file: rise.wav, kind: hit, role: riser, length: 3.0, peak_lufs: -12, claimable: false, approved: true}\n"
        "  - {id: slam, file: slam.wav, kind: hit, role: impact, tags: [big], length: 2.2, peak_lufs: -10, claimable: false, approved: true}\n"
        "  - {id: boom, file: boom.wav, kind: hit, tags: [boom], length: 1.6, peak_lufs: -10, claimable: false, approved: true}\n"
        "  - {id: pulse, file: pulse.wav, kind: hit, role: tension, length: 3.2, peak_lufs: -18, claimable: false, approved: true}\n"
        + extra, encoding="utf-8")
    return music.load_library(folder)


class BeatFindingTests(unittest.TestCase):
    def test_talk_is_not_a_jump_but_a_scream_is(self):
        x = talk(12.0)
        self.assertEqual(sound.jumps(x), [])
        x[int(8.0 / sound.HOP):int(9.0 / sound.HOP)] = -10.0  # a scream 20 dB over the talk at 8.0 s
        found = sound.jumps(x)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0][0], 8.0, delta=0.1)
        self.assertGreater(found[0][1], 15)

    def test_talk_after_a_pause_is_not_a_jump(self):
        x = np.concatenate([np.full(int(4 / sound.HOP), -70.0), talk(6.0)])
        self.assertEqual(sound.jumps(x), [])

    def test_levels_cap_the_beats_and_keep_them_apart(self):
        cands = [{"t": 2.0, "strength": 14.0, "why": "jump"}, {"t": 5.0, "strength": 12.0, "why": "jump"},
                 {"t": 12.0, "strength": 11.0, "why": "jump"}, {"t": 19.0, "strength": 9.0, "why": "jump"},
                 {"t": 25.0, "strength": 99.0, "why": "payoff"}]
        pick = lambda lv: [c["t"] for c in sound.select(cands, lv)]
        self.assertEqual(pick("subtle"), [25.0])                 # the payoff, nothing else
        self.assertEqual(pick("normal"), [25.0, 2.0])            # 2 beats, 8 s+ apart, 10 dB+
        self.assertEqual(pick("strong"), [25.0, 2.0, 12.0])      # 3 beats, 6 s+ apart (5.0 is too close to 2.0)
        for lv in ("subtle", "normal", "strong"):
            self.assertLessEqual(len(pick(lv)), sound.LIMITS[lv]["beats"])

    def test_a_shout_on_a_jump_counts_more_and_alone_only_at_strong(self):
        words = [W("NO", 6.0, 6.3), W("WAY!", 6.3, 6.8)]
        merged = sound.candidates(words, 20.0, None, [(6.05, 9.0)])
        self.assertTrue(merged[0].get("shout"))
        self.assertEqual(len(sound.select(merged, "normal")), 1)  # 9 dB + 2 for the shout clears normal's 10
        alone = sound.candidates(words, 20.0, None, [])
        self.assertEqual(sound.select(alone, "normal"), [])
        self.assertEqual(len(sound.select(alone, "strong")), 1)

    def test_plan_builds_into_the_main_beat_only(self):
        words = [W("go", 1.0, 1.4), W("now", 2.0, 2.4), W("WHAT!", 9.0, 9.5)]
        x = talk(20.0)
        x[int(9.0 / sound.HOP):int(10.0 / sound.HOP)] = -8.0
        x[int(17.5 / sound.HOP):int(18.0 / sound.HOP)] = -14.0
        plan = sound.plan_beats(words, 20.0, None, "normal", x)
        main, other = [b["t"] for b in plan["beats"]]
        self.assertAlmostEqual(main, 9.0, delta=0.15)
        self.assertAlmostEqual(other, 17.5, delta=0.15)  # a loud syllable right before it can start the rise
        roles = [(f["t"], f.get("role"), f.get("tag")) for f in plan["fx"] if f["type"] == "hit"]
        self.assertEqual(roles, [(main, "riser", None), (main, "impact", "big"), (other, "impact", "boom")])
        self.assertEqual([f["t"] for f in plan["fx"] if f["type"] == "flash"], [main])  # white flash on the main beat
        self.assertEqual(plan["windows"], [{"s": 2.65, "e": 9.15, "mood": "suspense", "fallback": "tension"}])
        sub = sound.plan_beats(words, 20.0, None, "subtle", x)
        self.assertEqual([(f.get("role"), f.get("tag")) for f in sub["fx"] if f["type"] == "hit"], [("impact", "boom")])
        self.assertEqual(sub["windows"], [])
        self.assertEqual(sound.plan_beats(words, 20.0, None, "normal", x, music_ok=False)["windows"], [])

    def test_no_bed_when_someone_talks_into_the_beat(self):
        words = [W(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(18)]
        plan = sound.plan_beats(words, 20.0, 9.0, "strong", None)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["beats"][0]["why"], "payoff")

    def test_level_comes_from_the_pick_then_the_config(self):
        self.assertEqual(sound.level_of({}, {}), ("normal", "default"))
        self.assertEqual(sound.level_of({}, {"sound_level": "subtle"}), ("subtle", "config"))
        self.assertEqual(sound.level_of({"sound_level": "off"}, {"sound_level": "strong"}), ("off", "pick"))
        self.assertIn("aus", sound.describe("off", []))
        self.assertIn("bei 9,5 s (Pointe der Story)",
                      sound.describe("normal", [{"t": 9.5, "why": "payoff", "roles": ["riser", "impact"]}]))


class PlacingTests(unittest.TestCase):
    def test_a_riser_ends_exactly_on_its_beat(self):
        with tempfile.TemporaryDirectory() as d:
            L = lib(d)
            fx = [{"t": 10.0, "type": "hit", "sound": "rise", "max_pre": 2.0},
                  {"t": 10.0, "type": "hit", "sound": "slam"}]
            layers = music.plan({"start": 0, "end": 20}, [], fx, 20.0, {}, lib=L)
            by_role = music.plan({}, [], [{"t": 10.0, "type": "hit", "sound": "riser"}], 20.0, {}, lib=L)
        (r, rs, rl, _), (i, is_, il, _) = layers
        self.assertEqual((r["id"], i["id"]), ("rise", "slam"))
        self.assertEqual(by_role[0][0]["id"], "rise")  # a role name asks for any sound of that role
        self.assertAlmostEqual(rs + rl, 10.0)            # the riser ends on the beat ...
        self.assertAlmostEqual(r["trim_from"], 1.0)      # ... playing its last 2 s
        self.assertEqual((is_, il), (10.0, 2.2))         # the impact starts on it and plays its tail

    def test_a_riser_never_starts_before_the_previous_beat_and_is_dropped_without_room(self):
        with tempfile.TemporaryDirectory() as d:
            L = lib(d)
            fx = [{"t": 9.0, "type": "hit", "sound": "slam"}, {"t": 10.0, "type": "hit", "sound": "rise"}]
            (_, _, _, _), (r, rs, rl, _) = music.plan({}, [], fx, 20.0, {}, lib=L)
            self.assertAlmostEqual(rs, 9.3)
            self.assertAlmostEqual(rs + rl, 10.0)
            tight = [{"t": 9.8, "type": "hit", "sound": "slam"}, {"t": 10.0, "type": "hit", "sound": "rise"}]
            self.assertEqual([t["id"] for t, *_ in music.plan({}, [], tight, 20.0, {}, lib=L)], ["slam"])
            early = music.plan({}, [], [{"t": 1.2, "type": "hit", "sound": "rise"}], 20.0, {}, lib=L)
            self.assertEqual((early[0][1], early[0][2]), (0.0, 1.2))  # from the clip's first frame

    def test_roles_tags_and_ids(self):
        with tempfile.TemporaryDirectory() as d:
            L = lib(d)
            self.assertEqual(music.choose(L, {}, "hit", want="rise")["id"], "rise")         # an id
            self.assertEqual(music.choose(L, {}, "hit", want="tension")["id"], "pulse")     # a role
            self.assertEqual(music.choose(L, {}, "hit", tag="big", seed="s")["id"], "slam")
            self.assertEqual(music.choose(L, {}, "hit", tag="boom", seed="s")["id"], "boom")  # untagged role: impact
            self.assertIn(music.choose(L, {}, "hit", seed="s")["id"], ("slam", "boom"))     # a bare hit: an impact
            self.assertEqual(music.role(next(t for t in L if t["id"] == "boom")), "impact")

    def test_sounds_are_levelled_against_the_bed(self):
        t = {"kind": "hit", "role": "impact", "peak_lufs": -10.0, "path": Path("x")}
        self.assertEqual(music.level_gain(t, -24.0), -12.0)        # impact 2 LU over a -24 LUFS bed: -22
        self.assertEqual(music.level_gain(t, -24.0, 2.0), -10.0)   # strong
        song = {"kind": "music", "lufs": -17.0, "path": Path("x")}
        self.assertEqual(music.level_gain(song, -24.0), -12.0)     # music 5 LU under the bed: -29
        self.assertEqual(music.level_gain(t, None), 0.0)
        self.assertEqual(music.level_gain(t, -60.0), music.MIN_GAIN_DB)
        self.assertEqual(music.level_gain(t, 0.0), music.MAX_GAIN_DB)

    def test_mix_trims_fades_levels_and_ducks_a_riser(self):
        with tempfile.TemporaryDirectory() as d:
            L = {t["id"]: t for t in lib(d)}
            layers = [({**L["rise"], "trim_from": 1.0, "gain_db": -6.0}, 8.0, 2.0, "hit"),
                      ({**L["slam"], "gain_db": -3.5}, 10.0, 2.2, "hit")]
            with mock.patch.object(music.subprocess, "run") as run:
                music.mix("bed.wav", [W("go", 8.5, 9.0)], layers, 20.0, "out.wav")
            cmd = run.call_args[0][0]
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("atrim=1.000:3.000,asetpts=PTS-STARTPTS,afade=t=in:d=0.150,volume=-6.0dB,adelay=8000:all=1,"
                      "volume='if(gt(between(t,8.35,9.15),0),0.3162,1)':eval=frame[m1]", graph)
        self.assertIn("atrim=0:2.200,asetpts=PTS-STARTPTS,volume=-3.5dB,adelay=10000:all=1[m2]", graph)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "pcm_f32le")  # loud beats can't clip before loudnorm

    def test_owner_sounds_in_local_serve_every_role(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            lib(folder)
            (folder / "local").mkdir()
            for name in ("myriser.wav", "myhit.wav", "mywhoosh.wav", "mypulse.wav"):
                (folder / "local" / name).write_bytes(b"x")
            (folder / "local" / "library.yaml").write_text(
                "tracks:\n"
                "  - {id: my-riser, file: myriser.wav, kind: hit, role: riser, length: 4.0, beat_at: 3.5, claimable: false, approved: true}\n"
                "  - {id: my-hit, file: myhit.wav, kind: hit, role: impact, tags: [big], claimable: false, approved: true}\n"
                "  - {id: my-whoosh, file: mywhoosh.wav, kind: hit, role: whoosh, claimable: false, approved: true}\n"
                "  - {id: my-pulse, file: mypulse.wav, kind: hit, role: tension, claimable: false, approved: true}\n"
                "  - {id: rise, approved: false}\n", encoding="utf-8")
            with mock.patch.object(music, "MUSIC", folder):
                L = music.load_library()
            self.assertEqual(music.choose(L, {}, "hit", want="riser")["id"], "my-riser")  # the shipped one is off
            for sid, role in (("my-riser", "riser"), ("my-hit", "impact"), ("my-whoosh", "whoosh"),
                              ("my-pulse", "tension")):
                t = music.choose(L, {}, "hit", want=sid)
                self.assertTrue(t["local"])
                self.assertEqual(music.role(t), role)
                self.assertIn(sid, {music.choose(L, {}, "hit", want=role, seed=str(k))["id"] for k in range(30)})
            layers = music.plan({}, [], [{"t": 10.0, "type": "hit", "sound": "my-riser"}], 20.0, {}, lib=L)
            t, start, length, _ = layers[0]
            self.assertAlmostEqual(start, 6.5)                 # its beat sits 3.5 s in ...
            self.assertAlmostEqual(start + 3.5, 10.0)          # ... and lands on t, the tail plays on
            self.assertAlmostEqual(length, 4.0)
            self.assertEqual(hooks.check_fx({"fx": [{"t": 1, "type": "hit", "sound": "my-pulse"}]}, L), [])


class PrepareClipTests(unittest.TestCase):
    """The sound decisions prepare_clip makes for each kind of pick (no audio is read: subprocess is mocked)."""

    def prep(self, pick, cfg=None, words=()):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(render.subprocess, "run"), \
                mock.patch.object(render.speakers, "diarized_words", return_value=None), \
                mock.patch.object(render.captions, "write_overlay", return_value="caps"):
            return render.prepare_clip(Path(d), {"start": 0.0, "end": 20.0, **pick}, list(words),
                                       {"separate_voices": False, **(cfg or {})}, Path(d))

    def test_a_story_cut_builds_into_its_payoff(self):
        prep = self.prep({"payoff_at": 9.5})
        roles = {l["role"]: (l["start"], l["end"]) for l in prep["sound"]["layers"]}
        self.assertEqual(roles["riser"][1], 9.5)          # a shipped riser (2 s noise riser or 1.6 s swell) ...
        self.assertGreaterEqual(roles["riser"][0], 7.5)   # ... capped at 2 s at normal
        self.assertEqual(roles["impact"][0], 9.5)
        self.assertEqual(roles["music"], (0.1, 9.65))     # nobody talks before the payoff: a suspense bed into it
        self.assertEqual([f["type"] for f in prep["fx"] if f["t"] == 9.5].count("flash"), 1)  # one white flash
        self.assertIn("Pointe der Story", prep["sound"]["text_de"])

    def test_none_on_purpose_and_off_mean_no_added_sound(self):
        for pick, cfg in (({"payoff_at": 9.5, "fx": []}, {}), ({"payoff_at": 9.5, "music": []}, {}),
                          ({"payoff_at": 9.5}, {"sound_level": "off"}), ({"payoff_at": 9.5, "sound_level": "off"}, {})):
            prep = self.prep(pick, cfg)
            self.assertEqual(prep["sound"]["layers"], [], (pick, cfg))
            self.assertFalse(any(f["type"] == "hit" for f in prep["fx"]))

    def test_explicit_fx_stay_authoritative(self):
        prep = self.prep({"payoff_at": 9.5, "fx": [{"t": 4.0, "type": "hit", "sound": "boom"},
                                                   {"t": 12.0, "type": "hit", "sound": "riser"}]})
        layers = [(l["id"], l["start"], l["end"]) for l in prep["sound"]["layers"]]
        self.assertEqual(layers[0], ("boom", 4.0, 5.6))
        self.assertIn(layers[1][0], ("noise-riser", "reverse-swell"))
        self.assertAlmostEqual(layers[1][2], 12.0)
        self.assertEqual(prep["sound"]["beats"], [])
        self.assertIn("wie im Clip festgelegt", prep["sound"]["text_de"])


class ShippedSoundTests(unittest.TestCase):
    def test_every_role_has_a_shipped_cc0_sound_with_its_recipe(self):
        lib = music.load_library(music.MUSIC)
        licence = (music.MUSIC / "LICENSE.txt").read_text(encoding="utf-8")
        for role in music.ROLES:
            t = music.choose(lib, {}, "hit", want=role)
            self.assertIsNotNone(t, role)
            self.assertFalse(t["claimable"])
        for t in lib:
            if t["kind"] == "hit":
                self.assertIn(t["file"], licence)
                self.assertLess(t["path"].stat().st_size, 400_000, t["file"])  # small files
                self.assertIsNotNone(t.get("peak_lufs"), t["file"])
        for tag in ("big", "boom"):
            self.assertTrue(any(tag in music.tags(t) for t in lib), tag)

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_shipped_levels_match_the_library(self):
        for t in music.load_library(music.MUSIC):
            if t["kind"] != "hit":
                continue
            measured = music.peak_loudness({k: v for k, v in t.items() if k != "peak_lufs"})
            self.assertAlmostEqual(measured, t["peak_lufs"], delta=0.6, msg=t["id"])
            self.assertAlmostEqual(music.duration({k: v for k, v in t.items() if k != "length"}), t["length"],
                                   delta=0.02)

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_the_synthesizer_reproduces_the_shipped_files(self):
        from clipper import sfx
        with tempfile.TemporaryDirectory() as d:
            for name in ("noise-riser", "heartbeat"):
                sfx.build(name, d)
                self.assertEqual((Path(d) / f"{name}.wav").read_bytes(), (music.MUSIC / f"{name}.wav").read_bytes())


if __name__ == "__main__":
    unittest.main()
