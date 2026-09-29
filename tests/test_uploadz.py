import json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uploadz as u

MB = u.MB

class ChunkPlan(unittest.TestCase):
    def test_small_and_up_to_64mb_is_one_chunk(self):
        self.assertEqual(u.chunk_plan(3 * MB), (3 * MB, 1))
        self.assertEqual(u.chunk_plan(64 * MB), (64 * MB, 1))

    def test_large_file_floor_count_last_chunk_absorbs_rest(self):
        size = 105 * MB + 123
        cs, n = u.chunk_plan(size)
        self.assertEqual((cs, n), (10 * MB, 10))
        ranges = list(u.chunk_ranges(size, cs, n))
        self.assertEqual(ranges[0], (0, 10 * MB - 1))
        self.assertEqual(ranges[-1][1], size - 1)
        self.assertEqual(sum(e - s + 1 for s, e in ranges), size)
        self.assertLessEqual(ranges[-1][1] - ranges[-1][0] + 1, 128 * MB)

class PastedCode(unittest.TestCase):
    def test_line_from_callback_page(self):
        self.assertEqual(u.parse_pasted_code(" abc*1!x st8 \n"), ("abc*1!x", "st8"))

    def test_full_url_is_decoded(self):
        url = "https://italovinicius18.github.io/uploadz/callback.html?code=a%2Ab%211&scopes=x&state=s1"
        self.assertEqual(u.parse_pasted_code(url), ("a*b!1", "s1"))

    def test_error_url(self):
        with self.assertRaises(u.TikTokError):
            u.parse_pasted_code("https://x/callback.html?error=access_denied")

    def test_authorize_url_has_redirect_and_scopes(self):
        url = u.authorize_url("ck", "st")
        self.assertIn("client_key=ck", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fitalovinicius18.github.io%2Fuploadz%2Fcallback.html", url)
        self.assertIn("video.publish", url)

class PostFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(u, "CONFIG_DIR", d), mock.patch.object(u, "TOKEN_FILE", d / "token.json")]
        for p in self.patches: p.start()
        (d / "token.json").write_text(json.dumps({"access_token": "AT", "expires_at": time.time() + 3600}))
        self.video = d / "clip.mp4"; self.video.write_bytes(b"x" * 1000)
        self.calls = []

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def fake(self, method, url, data=None, headers=None):
        self.calls.append((method, url, data, headers))
        ok = {"error": {"code": "ok"}}
        if url.endswith("creator_info/query/"):
            return 200, json.dumps({**ok, "data": {"creator_username": "me", "privacy_level_options": ["SELF_ONLY"],
                                                    "duet_disabled": True}}).encode()
        if url.endswith("/video/init/"):
            return 200, json.dumps({**ok, "data": {"publish_id": "p1", "upload_url": "https://up/1"}}).encode()
        if url == "https://up/1":
            return 201, b""
        if url.endswith("status/fetch/"):
            return 200, json.dumps({**ok, "data": {"status": "PUBLISH_COMPLETE"}}).encode()
        raise AssertionError(url)

    def test_direct_post(self):
        with mock.patch.object(u, "_request", self.fake):
            u.main(["post", str(self.video), "--privacy", "SELF_ONLY", "--caption", "oi #podpah"])
        init = next(c for c in self.calls if c[1].endswith("/video/init/"))
        body = json.loads(init[2])
        self.assertEqual(body["source_info"], {"source": "FILE_UPLOAD", "video_size": 1000,
                                               "chunk_size": 1000, "total_chunk_count": 1})
        self.assertEqual(body["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertTrue(body["post_info"]["disable_duet"])  # account setting wins
        self.assertEqual(init[3]["Authorization"], "Bearer AT")
        put = next(c for c in self.calls if c[0] == "PUT")
        self.assertEqual(put[3]["Content-Range"], "bytes 0-999/1000")

    def test_privacy_not_allowed_is_refused_before_upload(self):
        with mock.patch.object(u, "_request", self.fake), self.assertRaises(SystemExit):
            u.main(["post", str(self.video), "--privacy", "PUBLIC_TO_EVERYONE"])
        self.assertFalse(any(c[1].endswith("/video/init/") for c in self.calls))

    def test_privacy_is_required(self):
        with mock.patch.object(u, "_request", self.fake), self.assertRaises(SystemExit):
            u.main(["post", str(self.video)])

if __name__ == "__main__":
    unittest.main()
