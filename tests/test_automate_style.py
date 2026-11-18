"""automate.style mirrors the renderer's vocabulary (the server never imports clipper): this keeps the two in step and
checks the config validation the dashboard's "Untertitel & Sound" settings rely on."""
import unittest

import yaml

from automate import paths, style
from clipper import captions, music, placement, render, sound


class SyncTests(unittest.TestCase):
    def test_the_vocabulary_matches_the_renderer(self):
        self.assertEqual(style.CHOICES["caption_position"], placement.POSITIONS)
        self.assertEqual(style.CHOICES["caption_size"], placement.SIZE_VALUES)
        self.assertEqual(style.CHOICES["hook_size"], placement.SIZE_VALUES)
        self.assertEqual(style.CHOICES["caption_look"], placement.LOOK_VALUES)
        self.assertEqual(style.CHOICES["sound_level"], sound.LEVELS)
        self.assertEqual(style.HOOK_SECONDS_MAX, placement.HOOK_SECONDS_MAX)
        self.assertEqual(set(style.KEYS), set(placement.KEYS) | {"sound_level"})
        self.assertEqual(style.LOOKS, {k: v["family"] for k, v in captions.LOOKS.items()})
        self.assertEqual(style.LOOK_DE, captions.LOOK_DE)
        self.assertEqual(style.CAPTION_STYLES, tuple(captions.CAPTION_STYLES))
        self.assertEqual(style.SOUND_ROLES, music.ROLES)
        self.assertEqual(style.MOODS, music.MOODS)
        self.assertEqual(style.FX_TYPES, render.FX_TYPES)
        self.assertEqual(set(style.FLASH_COLOURS), set(render.FLASH_COLOURS))
        self.assertEqual(style.DEFAULTS["sound_level"], sound.DEFAULT_LEVEL)

    def test_valid_agrees_with_the_renderer(self):
        for key in placement.KEYS:
            for v in (*style.CHOICES[key], 1, 4.5, 30, 31, 0, "5", "middle", True, None, ["auto"]):
                with self.subTest(key=key, v=v):
                    want = placement.valid(key, v) and not (key == "hook_seconds" and isinstance(v, str)
                                                            and v not in ("auto", "whole"))
                    self.assertEqual(style.valid(key, v), want)  # the server is stricter on "5" (a string)
        for v in ("off", "subtle", "normal", "strong", "loud", None):
            self.assertEqual(style.valid("sound_level", v), v in sound.LEVELS)
        for c in ("white", "RED", "#ff0000", "0x9EFF00", "pink", "#ff00", 5):
            ok = True
            try:
                render.flash_colour(c)
            except (ValueError, AttributeError):
                ok = False
            self.assertEqual(style.valid_colour(c), ok, c)

    def test_the_repo_defaults_and_the_classic_settings_are_valid(self):
        repo = yaml.safe_load(paths.repo_watchlist().read_text(encoding="utf-8"))
        self.assertEqual(style.config_errors(repo), {})
        self.assertEqual({k: repo[k] for k in style.KEYS}, style.DEFAULTS)
        self.assertEqual(style.config_errors(style.CLASSIC), {})
        self.assertEqual(placement.config_errors(style.CLASSIC), [])

    def test_config_errors_name_each_bad_key_in_german(self):
        bad = style.config_errors({"caption_position": "middle", "hook_seconds": 0, "caption_looks": ["neon"],
                                   "caption_style": "fancy", "sound_level": "max", "weights": {"loudness": 1}})
        self.assertEqual(set(bad), {"caption_position", "hook_seconds", "caption_looks", "caption_style",
                                    "sound_level"})
        self.assertIn("Untertitel-Position: muss eins von auto, high, seam, low, classic sein", bad["caption_position"])
        self.assertIn("1–30 Sekunden", bad["hook_seconds"])
        self.assertIn("mindestens einen Look", style.config_errors({"caption_looks": []})["caption_looks"])
        self.assertEqual(style.look_mismatch("comic-word", "boxed"), "comic")
        self.assertIsNone(style.look_mismatch("boxed-word", "boxed"))
        self.assertIsNone(style.look_mismatch("classic", "boxed"))


if __name__ == "__main__":
    unittest.main()
