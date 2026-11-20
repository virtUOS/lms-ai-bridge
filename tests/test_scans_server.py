"""`/v1/index` with scans: queued, read in the background, indexed by page.

A fake reader stands in for the model, so this runs without a GPU and counts
exactly which pages would have cost a call. Needs Poppler for the PDF cases,
like the extractor tests.
"""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import server as bridge_server  # noqa: E402
from bridge.jobs import JobRunner  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402
from bridge.providers.openai_chat import EchoChat  # noqa: E402
from bridge.scans import TextCache, scan_entry  # noqa: E402
from pdf_fixtures import make_pdf  # noqa: E402

_POPPLER = bool(shutil.which("pdftoppm") and shutil.which("pdfimages")
                and shutil.which("pdfinfo"))

MIXED = make_pdf([
    (["Kapitel 1: Einleitung in die Statistik"], False),
    ([], True),                      # screenshot slide: read it
    (["Teil 2"], False),             # divider without an image: not worth a call
    (["Abbildung 4"], True),         # short caption plus image: read it
])
SCAN = make_pdf([([], True), ([], True), ([], True)])
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


class FakeReader:
    name = "ocr:fake"
    mode = "ocr"
    max_concurrent = 2
    longest_edge = 300

    def __init__(self, max_pages: int = 200) -> None:
        self.max_pages = max_pages
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def read(self, image: bytes, mime: str = "image/png") -> str:
        with self._lock:
            self.calls.append(mime)
            n = len(self.calls)
        return f"Gelesener Text Nummer {n} mit Regressionsgeraden und Residuen"


class _Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.retrieval = BuiltinRetrieval(tmp / "index.json")
        self.reader = FakeReader()
        H = bridge_server.Handler
        H.retrieval_provider = self.retrieval
        H.chat_provider = EchoChat()
        H.transcription_provider = None
        H.image_text_provider = self.reader
        H.image_text_cache = TextCache(tmp / "cache.json")
        H.jobs = JobRunner(max_workers=1, limits={"scan": 2})
        H.auth_token = ""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        H = bridge_server.Handler
        H.image_text_provider = None
        H.image_text_cache = None
        H.jobs = None
        self.tmp.cleanup()

    def post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def index(self, scans: list[dict], documents: list[dict] | None = None) -> dict:
        docs = [{"activity_ref": "studip:c:file:t", "title": "Text.pdf",
                 "text": "Lineare Regression mit kleinsten Quadraten."}]
        reply = self.post("/v1/index", {"course_ref": "studip:c",
                                        "documents": docs if documents is None else documents,
                                        "scans": scans})
        self.assertTrue(bridge_server.Handler.jobs.wait("studip:c", timeout=10))
        return reply

    def located(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for source, _text in self.retrieval.search("studip:c", "Gelesener Text Regressionsgeraden", k=50):
            found.setdefault(source.title, []).append(source.locator)
        return {t: sorted(v) for t, v in found.items()}


class TestCapability(_Server):
    def test_advertised_with_the_reader_named(self):
        caps = self.get("/v1/capabilities")
        self.assertIn("image_text", caps["capabilities"])
        self.assertEqual(caps["providers"]["image_text"], "ocr:fake")

    def test_absent_when_no_reader_is_configured(self):
        bridge_server.Handler.image_text_provider = None
        self.assertNotIn("image_text", self.get("/v1/capabilities")["capabilities"])


@unittest.skipUnless(_POPPLER, "poppler-utils not installed")
class TestReadingPdfPages(_Server):
    def test_only_short_pages_with_an_image_are_read_and_cited_by_page(self):
        reply = self.index([scan_entry(MIXED, title="Folien.pdf", activity_ref="studip:c:file:f",
                                       folder="Folien", pages=[2, 3, 4])])
        self.assertEqual(reply["image_text_queued"], 1)
        self.assertEqual(len(self.reader.calls), 2)          # pages 2 and 4
        self.assertEqual(self.located()["Folien.pdf"], ["S. 2", "S. 4"])

    def test_a_scanned_pdf_is_read_on_every_page(self):
        self.index([scan_entry(SCAN, title="Scan.pdf", activity_ref="studip:c:file:s")])
        self.assertEqual(self.located()["Scan.pdf"], ["S. 1", "S. 2", "S. 3"])

    def test_reindexing_does_not_pay_twice(self):
        entry = scan_entry(SCAN, title="Scan.pdf", activity_ref="studip:c:file:s")
        self.index([entry])
        self.index([entry])
        self.assertEqual(len(self.reader.calls), 3)
        self.assertEqual(self.located()["Scan.pdf"], ["S. 1", "S. 2", "S. 3"])

    def test_the_page_cap_holds_and_is_reported(self):
        self.reader.max_pages = 2
        reply = self.index([scan_entry(SCAN, title="Scan.pdf", activity_ref="studip:c:file:s")])
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(reply["image_text_pages"], 2)
        self.assertEqual(reply["image_text_skipped"], 1)

    def test_a_course_of_scans_alone_can_be_indexed(self):
        reply = self.index([scan_entry(SCAN, title="Scan.pdf", activity_ref="studip:c:file:s")],
                           documents=[])
        self.assertEqual(reply["documents"], 0)
        self.assertEqual(self.located()["Scan.pdf"], ["S. 1", "S. 2", "S. 3"])


class TestReadingImages(_Server):
    def test_an_image_file_is_read_whole_with_its_type(self):
        self.index([scan_entry(PNG, title="Tafelbild.png", activity_ref="studip:c:file:p")])
        self.assertEqual(self.reader.calls, ["image/png"])
        self.assertEqual(self.located()["Tafelbild.png"], [""])

    def test_a_failed_read_shows_in_the_job_status(self):
        def broken(image, mime="image/png"):
            raise RuntimeError("HTTP 400 At most 0 image(s)")
        self.reader.read = broken
        self.index([scan_entry(PNG, title="Tafelbild.png", activity_ref="studip:c:file:p")])
        status = self.get("/v1/jobs?course_ref=studip:c")
        self.assertEqual(status["failed"], 1)
        self.assertIn("At most 0 image", status["errors"][0])


if __name__ == "__main__":
    unittest.main()
