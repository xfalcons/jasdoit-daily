"""Tests for tts-upload's live decisions with fake R2 clients and a
monkeypatched render: head classification, hash-metadata reuse, and the
independent render/upload retries. No boto3, no edge-tts, no network."""
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("tts_upload", ROOT / "tools" / "tts-upload.py")
tts_upload = importlib.util.module_from_spec(_spec)
sys.modules["tts_upload"] = tts_upload
_spec.loader.exec_module(tts_upload)

if "loop_support" in sys.modules:
    loop_support = sys.modules["loop_support"]
else:
    _ls = importlib.util.spec_from_file_location("loop_support", ROOT / "tools" / "loop_support.py")
    loop_support = importlib.util.module_from_spec(_ls)
    sys.modules["loop_support"] = loop_support
    _ls.loader.exec_module(loop_support)

SPOKEN = "こんにちは"
SPOKEN_SHA = loop_support.sha256_hex(SPOKEN)
POST = ROOT / "tests" / "fixtures" / "day-019-chiisana-shouri-wo-iwau.md"


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeClient:
    def __init__(self, head=None, head_exc=None, upload_errors=None):
        self.head = head
        self.head_exc = head_exc
        self.upload_errors = list(upload_errors or [])
        self.uploads = []

    def head_object(self, **kwargs):
        if self.head_exc:
            raise self.head_exc
        if self.head is None:
            raise FakeClientError("404")
        return self.head

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append({"path": path, "bucket": bucket, "key": key,
                             "ExtraArgs": ExtraArgs})
        if self.upload_errors:
            raise self.upload_errors.pop(0)


class HeadStateTest(unittest.TestCase):
    def test_not_found_codes_read_absent(self):
        for code in ("404", "NoSuchKey", "NotFound"):
            with self.subTest(code=code):
                client = FakeClient(head_exc=FakeClientError(code))
                self.assertEqual(tts_upload.head_state(client, 19, SPOKEN_SHA), "absent")

    def test_denied_and_server_errors_stay_errors(self):
        for code in ("403", "500", "InternalError", "AccessDenied"):
            with self.subTest(code=code):
                client = FakeClient(head_exc=FakeClientError(code))
                with quiet():
                    state = tts_upload.head_state(client, 19, SPOKEN_SHA)
                self.assertEqual(state, "error")

    def test_connection_failure_without_response_is_error(self):
        client = FakeClient(head_exc=OSError("connection timed out"))
        with quiet():
            state = tts_upload.head_state(client, 19, SPOKEN_SHA)
        self.assertEqual(state, "error")

    def test_matching_hash_and_length_reuses(self):
        client = FakeClient(head={"ContentLength": 4096, "Metadata": {"spoken-sha256": SPOKEN_SHA}})
        self.assertEqual(tts_upload.head_state(client, 19, SPOKEN_SHA), "match")

    def test_zero_length_or_wrong_hash_is_stale(self):
        zero = FakeClient(head={"ContentLength": 0, "Metadata": {"spoken-sha256": SPOKEN_SHA}})
        wrong = FakeClient(head={"ContentLength": 4096, "Metadata": {"spoken-sha256": "deadbeef"}})
        none = FakeClient(head={"ContentLength": 4096, "Metadata": {}})
        self.assertEqual(tts_upload.head_state(zero, 19, SPOKEN_SHA), "stale")
        self.assertEqual(tts_upload.head_state(wrong, 19, SPOKEN_SHA), "stale")
        self.assertEqual(tts_upload.head_state(none, 19, SPOKEN_SHA), "stale")


class PublishAudioTest(unittest.TestCase):
    def publish(self, client, out_dir):
        with quiet():
            return tts_upload.publish_audio(client, 19, SPOKEN, out_dir)

    def render_factory(self, calls, errors):
        def fake_render(spoken, day, out_dir):
            calls.append("render")
            if errors:
                raise errors.pop(0)
            out = out_dir / f"day-{day:03d}.mp3"
            out.write_bytes(b"fake-mp3-bytes")
            return out
        return fake_render

    def test_absent_object_renders_once_and_uploads_with_hash_metadata(self):
        calls, client = [], FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(tts_upload, "render_mp3",
                                            self.render_factory(calls, [])):
                with unittest.mock.patch.dict("os.environ", {"R2_BUCKET": "jasdoit"}):
                    outcome, rendered = self.publish(client, Path(tmp))
            self.assertEqual(outcome, "uploaded")
            self.assertTrue(rendered.is_file())
            self.assertEqual(calls, ["render"])
            self.assertEqual(client.uploads[0]["key"], "audio/day-019.mp3")
            self.assertEqual(client.uploads[0]["ExtraArgs"]["Metadata"]["spoken-sha256"],
                             SPOKEN_SHA)

    def test_matching_object_skips_render_and_upload(self):
        calls, client = [], FakeClient(
            head={"ContentLength": 4096, "Metadata": {"spoken-sha256": SPOKEN_SHA}})
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(tts_upload, "render_mp3",
                                            self.render_factory(calls, [])):
                outcome, rendered = self.publish(client, Path(tmp))
        self.assertEqual(outcome, "reused")
        self.assertIsNone(rendered)
        self.assertEqual(calls, [])
        self.assertEqual(client.uploads, [])

    def test_upload_retry_preserves_the_render(self):
        calls, client = [], FakeClient(upload_errors=[FakeClientError("500")])
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(tts_upload, "render_mp3",
                                            self.render_factory(calls, [])):
                with unittest.mock.patch.dict("os.environ", {"R2_BUCKET": "jasdoit"}):
                    outcome, _ = self.publish(client, Path(tmp))
        self.assertEqual(outcome, "uploaded")
        self.assertEqual(calls, ["render"])
        self.assertEqual(len(client.uploads), 2)
        self.assertEqual(client.uploads[0]["path"], client.uploads[1]["path"])

    def test_upload_failure_after_retry_is_tts_error(self):
        calls, client = [], FakeClient(upload_errors=[FakeClientError("500"),
                                                      FakeClientError("500")])
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(tts_upload, "render_mp3",
                                            self.render_factory(calls, [])):
                with unittest.mock.patch.dict("os.environ", {"R2_BUCKET": "jasdoit"}):
                    with self.assertRaises(tts_upload.TtsError):
                        self.publish(client, Path(tmp))
        self.assertEqual(calls, ["render"])
        self.assertEqual(len(client.uploads), 2)

    def test_render_gets_its_own_retry(self):
        calls, client = [], FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                    tts_upload, "render_mp3",
                    self.render_factory(calls, [RuntimeError("edge-tts boom")])):
                with unittest.mock.patch.dict("os.environ", {"R2_BUCKET": "jasdoit"}):
                    outcome, _ = self.publish(client, Path(tmp))
        self.assertEqual(outcome, "uploaded")
        self.assertEqual(calls, ["render", "render"])
        self.assertEqual(len(client.uploads), 1)

    def test_render_failure_after_retry_is_tts_error(self):
        calls, client = [], FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                    tts_upload, "render_mp3",
                    self.render_factory(calls, [RuntimeError("a"), RuntimeError("b")])):
                with self.assertRaises(tts_upload.TtsError):
                    self.publish(client, Path(tmp))
        self.assertEqual(calls, ["render", "render"])
        self.assertEqual(client.uploads, [])

    def test_stale_object_is_overwritten_with_a_fresh_render(self):
        calls, client = [], FakeClient(
            head={"ContentLength": 4096, "Metadata": {"spoken-sha256": "deadbeef"}})
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(tts_upload, "render_mp3",
                                            self.render_factory(calls, [])):
                with unittest.mock.patch.dict("os.environ", {"R2_BUCKET": "jasdoit"}):
                    outcome, _ = self.publish(client, Path(tmp))
        self.assertEqual(outcome, "uploaded")
        self.assertEqual(len(client.uploads), 1)

    def test_rendered_file_reused_when_spoken_text_unchanged(self):
        calls, client = [], FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(tts_upload, "render_mp3",
                                            self.render_factory(calls, [])):
                with unittest.mock.patch.dict("os.environ", {"R2_BUCKET": "jasdoit"}):
                    self.publish(client, Path(tmp))
                    self.publish(FakeClient(), Path(tmp))
        self.assertEqual(calls, ["render"])


class MainLiveModeTest(unittest.TestCase):
    def run_main(self, client):
        with quiet():
            with unittest.mock.patch.object(tts_upload, "r2_client", return_value=client), \
                    unittest.mock.patch.dict("os.environ",
                                             {"R2_BUCKET": "jasdoit",
                                              "R2_PUBLIC_BASE": "https://pub.example"}), \
                    unittest.mock.patch.object(tts_upload, "render_mp3", self.render_ok()):
                return tts_upload.main([str(POST)])

    def render_ok(self):
        def fake_render(spoken, day, out_dir):
            out = out_dir / f"day-{day:03d}.mp3"
            out.write_bytes(b"fake-mp3-bytes")
            return out
        return fake_render

    def test_head_error_exits_retryable(self):
        rc = self.run_main(FakeClient(head_exc=FakeClientError("403")))
        self.assertEqual(rc, tts_upload.RETRYABLE)

    def test_zero_length_object_rerenders_and_exits_zero(self):
        rc = self.run_main(FakeClient(head={"ContentLength": 0, "Metadata": {}}))
        self.assertEqual(rc, 0)

    def test_matching_object_exits_zero(self):
        sha = loop_support.sha256_hex(loop_support.extract_spoken(POST.read_text("utf-8")).spoken)
        rc = self.run_main(FakeClient(head={"ContentLength": 4096,
                                            "Metadata": {"spoken-sha256": sha}}))
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
