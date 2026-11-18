"""The dashboard side of adaptive captions and epic-moment sound (round 2): the pick edit contract for the caption,
hook and sound keys, the layout preview stills (safe, cached, bounded), the render decisions on Videos, the caption
and sound defaults in the watchlist overlay, and the owner's own sounds (listen, upload with abuse cases, delete).
Same harness as tests/test_automate_api.py: a temp data dir, the app without background services, the other
modules faked; nothing here needs ffmpeg (the bridge and `clipper preview` are faked)."""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_automate_api import ApiCase, CSRF, BASE  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from automate import paths, previews, runtime, store  # noqa: E402
from automate.api import common  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"
PLACEMENT = {"layout": "split", "captions": {"position": "high", "size": "small", "look": "boxed-word"},
             "hook": {"size": "small", "seconds": 3.0}, "text_de": "Untertitel: oben im Spielbild, klein"}


class PreviewClipper:
    """`clipper preview` as run_clipper sees it: writes the PNG, prints the JSON; counts how many run at once."""

    def __init__(self, sleep=0.0):
        self.calls, self.running, self.most, self.sleep = [], 0, 0, sleep
        self.lock = threading.Lock()

    def __call__(self, args, profile, **kw):
        with self.lock:
            self.calls.append(list(args))
            self.running += 1
            self.most = max(self.most, self.running)
        try:
            time.sleep(self.sleep)
            out = Path(args[args.index("--out") + 1])
            out.write_bytes(PNG)
            at = float(args[args.index("--at") + 1]) if "--at" in args else 1.05
            meta = {"png": str(out), "pick": 1, "at": at, "hook": "h1", "hook_visible": True, "no_hook_arm": False,
                    "caption": "LOOK AT THIS", "placement": PLACEMENT, "seconds": 1.1}
            return runtime.ProcResult(0, "[preview] warming up\n" + json.dumps(meta, indent=2), "", 1.1)
        finally:
            with self.lock:
                self.running -= 1


class PickStyleTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.c = self.client()
        self.pipeline = self.fakes["automate.pipeline"]
        d = self.root / "jobs" / "job1"
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({"url": "https://www.youtube.com/watch?v=abc"}))
        (d / "source.mp4").write_bytes(b"v")
        self.dir = d
        self.job = store.create_job("source", "gta6", clipper_job="job1", title="Stream")
        self.ids = store.replace_picks(self.job, [
            {"start": 12, "end": 30.5, "clip_type": "reaction", "layout": {"type": "split", "face": [.7, .6, .3, .4]},
             "caption_style": "boxed", "hooks": ["h1", "h2"], "titles": ["GTA 6 a", "b"]},
            {"start": 100, "end": 120, "hooks": ["x"], "titles": ["y"]}])
        self.clip = PreviewClipper()
        p = mock.patch.object(runtime, "run_clipper", side_effect=self.clip)
        p.start()
        self.addCleanup(p.stop)

    def test_the_edit_contract_passes_style_keys_fx_and_music_on_null_included(self):
        body = {"caption_position": "high", "caption_size": None, "caption_look": "boxed-word", "hook_size": "small",
                "hook_seconds": 4, "sound_level": "strong", "music": [],
                "fx": [{"t": 20, "type": "hit", "sound": "riser"}, {"t": 20, "type": "hit", "sound": "impact"}]}
        r = self.c.patch(f"/api/picks/{self.ids[0]}", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.pipeline.update_pick_data.assert_called_with(self.ids[0], body)
        r = self.c.patch(f"/api/picks/{self.ids[0]}", json={"fx": None})
        self.pipeline.update_pick_data.assert_called_with(self.ids[0], {"fx": None})
        for bad in ({"fx": "lots"}, {"music": {"s": 1}}, {"fx": [{"t": 1, "type": "zoom"}] * 31}):
            r = self.c.patch(f"/api/picks/{self.ids[0]}", json=bad)
            self.assertEqual((r.status_code, set(r.json()["error"]["fields"])), (422, set(bad)), bad)
        self.pipeline.update_pick_data.side_effect = self.pipeline.PipelineError(
            "Bitte prüfe die Eingaben.", "invalid", 422)
        self.pipeline.update_pick_data.side_effect.fields = {"caption_look": "Der Look gehört zum Stil „comic“"}
        r = self.c.patch(f"/api/picks/{self.ids[0]}", json={"caption_look": "comic-word"})
        self.assertEqual(r.json()["error"]["fields"], {"caption_look": "Der Look gehört zum Stil „comic“"})

    def test_preview_is_drawn_once_per_pick_content_and_served_only_for_that_pick(self):
        r = self.c.get(f"/api/picks/{self.ids[0]}/preview")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual((d["caption"], d["placement"]["text_de"], d["cached"]), ("LOOK AT THIS", PLACEMENT["text_de"],
                                                                                   False))
        self.assertNotIn("png", d)  # no server path goes to the browser
        args = self.clip.calls[0]
        self.assertEqual(args[:2], ["preview", "--picks"])
        self.assertEqual(args[-2:], ["--", "job1"])
        self.assertNotIn("--at", args)  # default moment: the first caption while the hook shows
        self.assertFalse(Path(args[2]).exists())  # the one-pick file is gone
        img = self.c.get(d["image"])
        self.assertEqual((img.status_code, img.headers["content-type"], img.content), (200, "image/png", PNG))
        self.assertIn("max-age", img.headers["cache-control"])
        again = self.c.get(f"/api/picks/{self.ids[0]}/preview").json()
        self.assertEqual((again["key"], again["cached"], len(self.clip.calls)), (d["key"], True, 1))
        at = self.c.get(f"/api/picks/{self.ids[0]}/preview?at=7.04&hook=2").json()
        self.assertNotEqual(at["key"], d["key"])
        self.assertEqual(self.clip.calls[-1][self.clip.calls[-1].index("--at") + 1], "7.00")  # rounded to 0.1 s
        self.assertEqual(self.clip.calls[-1][self.clip.calls[-1].index("--hook") + 1], "2")
        # an edit changes the key: the next request draws again
        store.update_pick(self.ids[0], data={**store.get_pick(self.ids[0])["data"], "caption_position": "low"})
        self.assertNotEqual(self.c.get(f"/api/picks/{self.ids[0]}/preview").json()["key"], d["key"])
        # so does a changed default in Einstellungen (the watchlist overlay)
        store.set_settings({"watchlist.overrides": {"caption_size": "large"}})
        before = len(self.clip.calls)
        self.c.get(f"/api/picks/{self.ids[0]}/preview")
        self.assertEqual(len(self.clip.calls), before + 1)
        # only stills of that pick, only by key
        for url in (f"/api/picks/{self.ids[1]}/preview/file?key={d['key']}",
                    f"/api/picks/{self.ids[0]}/preview/file?key=../../automate.db",
                    f"/api/picks/{self.ids[0]}/preview/file?key=" + "0" * 24,
                    f"/api/picks/{self.ids[0]}/preview/file", "/api/picks/999/preview/file?key=" + d["key"]):
            self.assertEqual(self.c.get(url).status_code, 404, url)
        outside = self.root / "secret.png"
        outside.write_bytes(b"secret")
        link = previews._dir(self.ids[0]) / ("a" * 24 + ".png")
        link.symlink_to(outside)
        self.assertEqual(self.c.get(f"/api/picks/{self.ids[0]}/preview/file?key={'a' * 24}").status_code, 404)
        bad = self.c.get(f"/api/picks/{self.ids[0]}/preview?at=-1&hook=0")
        self.assertEqual(set(bad.json()["error"]["fields"]), {"at", "hook"})
        self.assertEqual(self.client(csrf=False).get(d["image"]).status_code, 200)  # an <img> sends no header
        self.assertEqual(self.anon.get(d["image"]).status_code, 401)

    def test_a_deleted_source_is_a_german_note_not_an_error(self):
        (self.dir / "source.mp4").unlink()
        r = self.c.get(f"/api/picks/{self.ids[0]}/preview")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["ok"], r.json()["reason"]), (False, "source_gone"))
        self.assertIn("Quellvideo ist schon gelöscht", r.json()["message"])
        self.assertEqual(self.clip.calls, [])
        seg = store.replace_picks(store.create_job("source", "gta6", clipper_job="job2"), [
            {"segments": [{"start": 1, "end": 5}, {"start": 1, "end": 5, "job": "../job1"}], "hooks": ["a"],
             "titles": ["b"]}])[0]
        self.assertEqual(self.c.get(f"/api/picks/{seg}/preview").json()["reason"], "source_gone")

    def test_a_failed_preview_says_so_and_leaves_nothing(self):
        def broken(args, profile, **kw):
            return runtime.ProcResult(1, "", "Traceback ...\nSystemExit: [preview] pick 1 doesn't exist", 0.2)
        with mock.patch.object(runtime, "run_clipper", side_effect=broken):
            d = self.c.get(f"/api/picks/{self.ids[0]}/preview").json()
        self.assertEqual((d["ok"], d["reason"]), (False, "failed"))
        self.assertIn("pick 1 doesn't exist", d["message"])
        self.assertEqual(list(previews._dir(self.ids[0]).glob("*.png")), [])

    def test_at_most_two_previews_run_at_once_and_a_full_queue_says_busy(self):
        self.clip.sleep = 0.3
        results = []

        def ask(at):
            results.append(self.c.get(f"/api/picks/{self.ids[0]}/preview?at={at}").json()["ok"])
        threads = [threading.Thread(target=ask, args=(t,)) for t in (1, 2, 3, 4, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((results.count(True), self.clip.most), (5, previews.MAX_RUNNING))
        for _ in range(previews.MAX_RUNNING):
            previews._slots.acquire()
        try:
            with mock.patch.object(previews, "WAIT_S", 0.05):
                d = self.c.get(f"/api/picks/{self.ids[0]}/preview?at=9").json()
        finally:
            for _ in range(previews.MAX_RUNNING):
                previews._slots.release()
        self.assertEqual((d["ok"], d["reason"]), (False, "busy"))
        self.assertIn("gleich noch einmal", d["message"])

    def test_old_stills_of_a_pick_are_pruned(self):
        with mock.patch.object(previews, "KEEP", 3):
            for at in range(6):
                self.assertTrue(self.c.get(f"/api/picks/{self.ids[0]}/preview?at={at}").json()["ok"])
        self.assertEqual(len(list(previews._dir(self.ids[0]).glob("*.png"))), 3)


class VideoMadeTests(ApiCase):
    def test_videos_carry_the_placement_and_sound_the_render_chose(self):
        c = self.client()
        folder = self.root / "exports" / "gta6" / "2026-11-18" / "video 1"
        folder.mkdir(parents=True)
        (folder / "a.mp4").write_bytes(b"mp4")
        sound = {"level": "normal", "auto": True, "beats": [], "text_de": "Sound: normal – Riser + Impact bei 9,5 s",
                 "layers": [{"id": "noise-riser", "role": "riser", "start": 7.5, "end": 9.5, "gain_db": -1.0}]}
        (folder / "upload.json").write_text(json.dumps([{"file": "a.mp4", "placement": PLACEMENT, "sound": sound}]))
        vid = store.add_video("gta6", folder, title="T")
        store.add_variant(vid, folder / "a.mp4", hook="a")
        v = c.get(f"/api/videos/{vid}").json()
        self.assertEqual(v["made"], {"placement": PLACEMENT["text_de"], "sound": sound["text_de"],
                                     "sound_level": "normal",
                                     "sound_layers": [{"id": "noise-riser", "role": "riser", "start": 7.5,
                                                       "end": 9.5}]})
        self.assertEqual(c.get("/api/videos").json()["items"][0]["made"]["sound"], sound["text_de"])
        old = store.add_video("gta6", self.root / "exports" / "gta6" / "2026-11-18" / "video 2", title="alt")
        self.assertIsNone(c.get(f"/api/videos/{old}").json()["made"])  # rendered before: nothing to say
        outside = store.add_video("gta6", self.root / "elsewhere", title="x")
        (self.root / "elsewhere").mkdir()
        (self.root / "elsewhere" / "upload.json").write_text(json.dumps([{"placement": PLACEMENT}]))
        self.assertIsNone(c.get(f"/api/videos/{outside}").json()["made"])  # only manifests inside exports/


class StyleSettingsTests(ApiCase):
    def test_caption_and_sound_defaults_go_through_the_watchlist_overrides_into_the_overlay(self):
        c = self.client()
        classic = {"caption_position": "classic", "caption_size": "large", "caption_look": "classic",
                   "hook_size": "large", "hook_seconds": "whole", "sound_level": "off",
                   "caption_looks": ["boxed", "boxed-word"]}
        r = c.put("/api/settings/watchlist", json={"overrides": {**classic, "hype_words": ["sheesh"]}})
        self.assertEqual(r.status_code, 200, r.text)
        overlay = yaml.safe_load(paths.runtime_watchlist().read_text())
        for k, v in classic.items():
            self.assertEqual(overlay[k], v)
        self.assertEqual(overlay["hype_words"], ["sheesh"])
        self.assertEqual(r.json()["effective"]["sound_level"], "off")
        bad = {"caption_position": "middle", "hook_seconds": 99, "caption_looks": ["neon"], "sound_level": True,
               "caption_style": "fancy", "caption_size": "huge"}
        r = c.put("/api/settings/watchlist", json={"overrides": bad})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.json()["error"]["fields"]), set(bad))
        self.assertIn("Untertitel-Position", r.json()["error"]["fields"]["caption_position"])
        self.assertEqual(c.put("/api/settings/watchlist", json={"overrides": {"caption_looks": []}}).status_code, 422)
        r = c.patch("/api/settings", json={"watchlist.overrides": {"hook_seconds": "soon"}})  # the other way in
        self.assertEqual(r.status_code, 422)
        self.assertIn("Hook-Anzeigedauer", r.json()["error"]["fields"]["watchlist.overrides"])
        r = c.put("/api/profiles/gta6/config", json={"overrides": {"caption_position": "middle"}})
        self.assertEqual(set(r.json()["error"]["fields"]), {"caption_position"})
        self.assertEqual(store.get_setting("watchlist.overrides")["caption_position"], "classic")  # kept
        c.put("/api/settings/watchlist", json={"overrides": {"hook_seconds": 6}})  # back to the repo's defaults
        overlay = yaml.safe_load(paths.runtime_watchlist().read_text())
        self.assertEqual((overlay["hook_seconds"], overlay["caption_position"]), (6, "auto"))


class SoundTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.music = self.root / "music"
        (self.music / "local").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"AUTOMATE_MUSIC_DIR": str(self.music)})
        env.start()
        self.addCleanup(env.stop)
        (self.music / "library.yaml").write_text(
            "tracks:\n  - {id: boom, file: boom.wav, kind: hit, role: impact}\n"
            "  - {id: evil, file: ../secret.wav, kind: hit}\n  - {id: theme, file: theme.m4a, kind: music}\n")
        (self.music / "boom.wav").write_bytes(b"RIFF" + bytes(range(256)) * 8)
        (self.music / "theme.m4a").write_bytes(b"m4a")
        (self.root / "secret.wav").write_bytes(b"secret")
        (self.music / "local" / "library.yaml").write_text(
            "tracks:\n  - {id: boom, approved: false}\n  - {id: mine, file: mine.mp3, kind: hit, role: riser}\n"
            "  - {id: sneaky, file: link.wav, kind: hit}\n")
        (self.music / "local" / "mine.mp3").write_bytes(b"ID3mp3")
        (self.music / "local" / "link.wav").symlink_to(self.root / "secret.wav")
        self.c = self.client()
        self.added = []
        self.bridge_mock.side_effect = self.sound_bridge

    def sound_bridge(self, op, payload, profile, **kw):
        if op == "add_sound":
            path = Path(payload["path"])
            self.added.append({**payload, "bytes": path.read_bytes(), "inbox": path.parent})
            if b"text" in path.read_bytes():
                return {"problem": "Die Datei ist keine lesbare Audiodatei."}
            return {"track": {"id": "my-boom", "kind": payload["kind"], "role": payload["role"], "source": "own"}}
        if op == "remove_sound":
            return {"removed": payload["id"]} if payload["id"] == "my-boom" else \
                {"problem": "Mitgelieferte Sounds lassen sich nicht löschen – nur sperren."}
        return self.fake_bridge(op, payload, profile, **kw)

    def upload(self, data, client=None, **params):
        q = {"name": "Big Boom.wav", "kind": "hit", "role": "impact", "claimable": 0, "approved": 1, **params}
        return (client or self.c).post("/api/assets/music", params=q, content=data,
                                       headers={"Content-Type": "audio/wav"})

    def test_listen_to_any_library_sound_with_range(self):
        r = self.c.get("/api/assets/music/boom/file")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "audio/wav"))
        r = self.c.get("/api/assets/music/boom/file", headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status_code, len(r.content), r.headers["content-range"]), (206, 100, "bytes 0-99/2052"))
        r = self.c.get("/api/assets/music/mine/file")  # an own sound, from local/
        self.assertEqual((r.status_code, r.headers["content-type"], r.content), (200, "audio/mpeg", b"ID3mp3"))
        self.assertEqual(self.c.get("/api/assets/music/theme/file").headers["content-type"], "audio/mp4")
        for tid in ("evil", "sneaky", "nope", "..", "boom%2F..%2F..%2Fsecret"):
            self.assertEqual(self.c.get(f"/api/assets/music/{tid}/file").status_code, 404, tid)
        self.assertEqual(self.anon.get("/api/assets/music/boom/file").status_code, 401)

    def test_upload_files_a_sound_through_the_bridge_and_deletes_the_upload(self):
        data = b"RIFF" + b"\x00" * 5000
        r = self.upload(data, name="../../Big Boom.wav")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], "my-boom")
        sent = self.added[-1]
        self.assertEqual((sent["name"], sent["kind"], sent["role"], sent["mood"]), ("Big Boom.wav", "hit", "impact",
                                                                                    None))
        self.assertEqual((sent["claimable"], sent["approved"], sent["bytes"]), (False, True, data))
        self.assertEqual(sent["inbox"], paths.tmp_dir() / "uploads")
        self.assertEqual(list((paths.tmp_dir() / "uploads").iterdir()), [])  # the upload never stays
        self.upload(data, kind="music", role="", mood="epic", name="Theme.MP3", claimable=1)
        self.assertEqual((self.added[-1]["mood"], self.added[-1]["claimable"]), ("epic", True))
        self.assertEqual(self.c.delete("/api/assets/music/my-boom").json(), {"ok": True})
        r = self.c.delete("/api/assets/music/boom")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_removable"))
        self.assertIn("nur sperren", r.json()["error"]["message"])

    def test_upload_abuse_is_refused_in_german(self):
        r = self.upload(b"this is just text, not audio", name="fake.wav")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["fields"]["file"], "Die Datei ist keine lesbare Audiodatei.")
        cases = [({"name": "song.exe"}, "name"), ({"name": "noext"}, "name"), ({"name": ""}, "name"),
                 ({"kind": "video"}, "kind"), ({"role": "bang"}, "role"),
                 ({"kind": "music", "role": "", "mood": "happy"}, "mood"), ({"approved": 5}, "approved"),
                 ({"name": "a\x01.wav"}, "name")]
        for params, field in cases:
            with self.subTest(params=params):
                r = self.upload(b"RIFFxxxx", **params)
                self.assertEqual((r.status_code, set(r.json()["error"]["fields"])), (422, {field}))
        self.assertEqual(self.upload(b"").json()["error"]["fields"], {"file": "Die Datei ist leer."})
        from automate.api import config  # server.py lets only this route past MAX_BODY, up to MAX_SOUND_BYTES
        r = self.upload(b"x" * (config.MAX_SOUND_BYTES + 1))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"))
        self.assertIn("höchstens 25 MB", r.json()["error"]["message"])

        def chunked():
            for _ in range(26):
                yield b"x" * (1024 * 1024)
        r = self.c.post("/api/assets/music", params={"name": "a.wav", "kind": "hit", "role": "impact"},
                        content=chunked())
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "too_large"))
        self.assertIn("25 MB", r.json()["error"]["message"])
        r = self.c.post("/api/jobs", content=b"x" * (5 * 1024 * 1024), headers={"Content-Type": "application/json"})
        self.assertIn("höchstens 4 MB", r.json()["error"]["message"])  # every other route keeps the small limit
        self.assertEqual(self.upload(b"RIFFxxxx" * 1000, client=self.client(csrf=False)).status_code, 403)
        self.assertEqual(self.upload(b"RIFFxxxx", client=TestClient(self.app, base_url=BASE, headers=CSRF))
                         .status_code, 401)
        self.assertEqual(len(self.added), 1)  # only the text file reached the content check
        self.assertEqual(list((paths.tmp_dir() / "uploads").iterdir()), [])


class InstagramErrorMappingTests(unittest.TestCase):
    def test_instagram_errors_become_german_api_errors_with_a_fitting_status(self):
        from automate.instagram import InstagramError
        for code, status in (("instagram_token_invalid", 422), ("instagram_publish_limit", 429),
                             ("instagram_unreachable", 502), ("instagram_disconnected", 409)):
            err = common.map_exception(InstagramError("Instagram sagt nein.", code=code))
            self.assertEqual((err.status, err.code, err.message), (status, code, "Instagram sagt nein."))


if __name__ == "__main__":
    unittest.main()
