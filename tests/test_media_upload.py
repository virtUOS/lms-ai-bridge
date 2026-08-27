"""The adapter's media path: which files are queued, and which are refused.

Stubs the Stud.IP API, so this runs without credentials and without an ASR
server. What matters here is the *filtering* — a lecture recording is expensive
to move and expensive to transcribe, so the decisions made before the download
are the ones worth testing.
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters.studip_adapter as A  # noqa: E402

FILES = {
    "data": [
        {"id": "a1", "attributes": {
            "name": "Vorlesung.mp3", "mime-type": "audio/mpeg",
            "filesize": 5_000_000, "is-downloadable": True},
            "relationships": {"terms-of-use": {"data": {"id": "SELFMADE_NONPUB"}}}},
        {"id": "a2", "attributes": {
            "name": "Skript.pdf", "mime-type": "application/pdf",
            "filesize": 1_000_000, "is-downloadable": True}},
        {"id": "a3", "attributes": {
            "name": "Riesig.mp4", "mime-type": "video/mp4",
            "filesize": 900_000_000, "is-downloadable": True}},
        {"id": "a4", "attributes": {
            "name": "Gesperrt.wav", "mime-type": "audio/wav",
            "filesize": 2_000_000, "is-downloadable": False}},
    ]
}


class TestMediaCollection(unittest.TestCase):
    def setUp(self):
        self._get, self._dl = A.studip_get, A.studip_download
        A.studip_get = lambda path: FILES if "file-refs" in path else {}
        A.studip_download = lambda ref_id: b"AUDIOBYTES-" + ref_id.encode()

    def tearDown(self):
        A.studip_get, A.studip_download = self._get, self._dl

    def test_only_audio_and_video_are_queued(self):
        media = A.fetch_course_media("C")
        self.assertEqual([m["title"] for m in media], ["Vorlesung.mp3"])

    def test_documents_are_left_to_the_extractors(self):
        # Sending a PDF to a GPU would be waste; it has a real parser.
        self.assertNotIn("Skript.pdf",
                         [m["title"] for m in A.fetch_course_media("C")])

    def test_oversized_files_are_skipped_not_attempted(self):
        # Base64 in a JSON body cannot carry a 900 MB video, and failing loudly
        # before the download beats exhausting memory on both ends.
        self.assertNotIn("Riesig.mp4",
                         [m["title"] for m in A.fetch_course_media("C")])

    def test_the_size_cap_is_adjustable(self):
        media = A.fetch_course_media("C", max_mb=1000)
        self.assertIn("Riesig.mp4", [m["title"] for m in media])

    def test_undownloadable_files_are_not_attempted(self):
        self.assertNotIn("Gesperrt.wav",
                         [m["title"] for m in A.fetch_course_media("C")])

    def test_content_is_base64_and_round_trips(self):
        media = A.fetch_course_media("C")
        self.assertEqual(base64.b64decode(media[0]["content_base64"]),
                         b"AUDIOBYTES-a1")

    def test_activity_ref_points_back_into_the_lms(self):
        media = A.fetch_course_media("C")
        self.assertEqual(media[0]["activity_ref"], "studip:C:file:a1")


class TestCapabilityHandshake(unittest.TestCase):
    """The adapter asks the bridge before uploading anything.

    Uploading a lecture recording to a deployment with no transcription
    provider would waste the bandwidth of both — and this is exactly what
    /v1/capabilities exists for: one adapter, many deployments.
    """

    def test_no_media_is_collected_when_the_bridge_cannot_transcribe(self):
        calls = []
        saved_caps, saved_media, saved_docs, saved_post = (
            A._bridge_can_transcribe, A.fetch_course_media,
            A.fetch_course_documents, A.bridge_post)
        A._bridge_can_transcribe = lambda: False
        A.fetch_course_media = lambda *a, **k: calls.append("collected") or []
        A.fetch_course_documents = lambda cid: ("studip:C", [{"text": "x"}])
        A.bridge_post = lambda path, payload: payload
        try:
            payload = A.index_course("C")
        finally:
            (A._bridge_can_transcribe, A.fetch_course_media,
             A.fetch_course_documents, A.bridge_post) = (
                saved_caps, saved_media, saved_docs, saved_post)
        self.assertEqual(calls, [])
        self.assertNotIn("media", payload)

    def test_media_is_sent_when_the_bridge_can(self):
        saved_caps, saved_media, saved_docs, saved_post = (
            A._bridge_can_transcribe, A.fetch_course_media,
            A.fetch_course_documents, A.bridge_post)
        A._bridge_can_transcribe = lambda: True
        A.fetch_course_media = lambda *a, **k: [{"title": "V.mp3",
                                                 "content_base64": "eA=="}]
        A.fetch_course_documents = lambda cid: ("studip:C", [{"text": "x"}])
        A.bridge_post = lambda path, payload: payload
        try:
            payload = A.index_course("C")
        finally:
            (A._bridge_can_transcribe, A.fetch_course_media,
             A.fetch_course_documents, A.bridge_post) = (
                saved_caps, saved_media, saved_docs, saved_post)
        self.assertEqual(len(payload["media"]), 1)

    def test_an_unreachable_bridge_means_no_media_not_a_crash(self):
        saved = A.BRIDGE_URL
        A.BRIDGE_URL = "http://127.0.0.1:9"
        try:
            self.assertFalse(A._bridge_can_transcribe())
        finally:
            A.BRIDGE_URL = saved


class TestBodySizeLimit(unittest.TestCase):
    """A request carrying audio is far larger than one carrying text.

    Found live: an 18.4 MB .wav became ~25 MB of base64 and hit an 8 MB ceiling
    sized for documents. Worse than the rejection was *how* it failed — the
    check ran before the body was read, so the client saw a broken pipe
    mid-upload with a stack trace, not an error it could act on.
    """

    def test_the_limit_accommodates_media_by_default(self):
        import importlib                                     # noqa: PLC0415
        import bridge.server as S                            # noqa: PLC0415
        importlib.reload(S)
        # 512 MB default: a lecture recording should fit, a video usually will
        # not — which is what the adapter's own size cap is for.
        self.assertGreaterEqual(S.MAX_BODY, 256 * 1024 * 1024)

    def test_the_limit_is_configurable(self):
        import importlib                                     # noqa: PLC0415
        import os                                            # noqa: PLC0415
        os.environ["BRIDGE_MAX_BODY_MB"] = "16"
        try:
            import bridge.server as S                        # noqa: PLC0415
            importlib.reload(S)
            self.assertEqual(S.MAX_BODY, 16 * 1024 * 1024)
        finally:
            os.environ.pop("BRIDGE_MAX_BODY_MB", None)
            import bridge.server as S2                       # noqa: PLC0415
            importlib.reload(S2)

    def test_the_oversize_path_drains_before_refusing(self):
        # The behaviour that turns a broken pipe into a readable 400: the body
        # is consumed, then rejected. Verified here on the source, since the
        # HTTP-level check needs a live server.
        source = (Path(__file__).resolve().parent.parent
                  / "bridge" / "server.py").read_text()
        guard = source[source.index("def _read_json"):]
        guard = guard[:guard.index("return json.loads")]
        self.assertIn("self.rfile.read(min(", guard,
                      "oversized bodies must be drained before refusing")
        self.assertIn("BRIDGE_MAX_BODY_MB", guard,
                      "the error should name the setting that fixes it")


if __name__ == "__main__":
    unittest.main()
