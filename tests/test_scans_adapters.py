"""The adapters collect scans: which files and pages, and only when asked.

Every platform is stubbed. What is pinned down: a scanned PDF and an image file
end up in `scans` instead of being skipped; a normal PDF sends only its short
pages; nothing is collected (or downloaded) when the bridge cannot read images;
and a course of scans alone still gets indexed.
"""
from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ilias_adapter  # noqa: E402
import moodle_adapter  # noqa: E402
import studip_adapter  # noqa: E402
from pdf_fixtures import make_pdf  # noqa: E402
from test_ilias_adapter import COURSE_XML, envelope, file_xml, tree_xml  # noqa: E402

_POPPLER = bool(shutil.which("pdftotext"))

MIXED = make_pdf([
    (["Kapitel 1: Einleitung in die Statistik"], False),
    ([], True),
    (["Teil 2"], False),
    (["Abbildung 4"], True),
])
SCAN = make_pdf([([], True), ([], True), ([], True)])
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _titles(scans: list) -> dict:
    return {s["title"]: s.get("pages") for s in scans}


class TestStudip(unittest.TestCase):
    FILES = {"data": [
        {"id": "s1", "attributes": {"name": "Skript.pdf", "mime-type": "application/pdf"}},
        {"id": "s2", "attributes": {"name": "Scan.pdf", "mime-type": "application/pdf"}},
        {"id": "s3", "attributes": {"name": "Tafel.png", "mime-type": "image/png"}},
        {"id": "s4", "attributes": {"name": "Gesperrt.png", "mime-type": "image/png",
                                    "is-downloadable": False}},
        {"id": "s5", "attributes": {"name": "Logo.svg", "mime-type": "image/svg+xml"}},
    ]}
    BLOBS = {"s1": MIXED, "s2": SCAN, "s3": PNG, "s5": b"<svg/>"}

    def setUp(self):
        self.downloads: list[str] = []

        def download(ref_id):
            self.downloads.append(ref_id)
            return self.BLOBS[ref_id]

        get = lambda path: self.FILES if "file-refs" in path else {}  # noqa: E731
        self.patches = [mock.patch.object(studip_adapter, "studip_get", get),
                        mock.patch.object(studip_adapter, "studip_download", download)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_scans_and_images_are_collected_with_their_pages(self):
        scans: list = []
        studip_adapter.fetch_course_files("C", scans=scans)
        found = _titles(scans)
        self.assertEqual(set(found), {"Skript.pdf", "Scan.pdf", "Tafel.png"})
        self.assertEqual(found["Skript.pdf"], [2, 3, 4])
        self.assertIn(found["Scan.pdf"], (None, [1, 2, 3]))
        self.assertIsNone(found["Tafel.png"])
        self.assertNotIn("s4", self.downloads)       # the API said no
        self.assertNotIn("s5", self.downloads)       # SVG is not read

    def test_the_scan_keeps_its_place_in_the_course(self):
        scans: list = []
        studip_adapter.fetch_course_files("C", scans=scans, course_name="Statistik I")
        entry = next(s for s in scans if s["title"] == "Tafel.png")
        self.assertEqual(entry["activity_ref"], "studip:C:file:s3")
        self.assertEqual(entry["course_name"], "Statistik I")

    def test_without_a_reader_images_are_not_even_downloaded(self):
        studip_adapter.fetch_course_files("C")
        self.assertNotIn("s3", self.downloads)


class TestIndexCourse(unittest.TestCase):
    """The same rule on every platform: ask the bridge, then send scans."""

    def _index(self, adapter, course_id, offers: set[str], docs: list):
        sent: dict = {}

        def fetch(cid, scans=None, **kw):
            if scans is not None:
                scans.append({"title": "Scan.pdf", "content_base64": "JVBERg=="})
            return f"x:{cid}", list(docs)

        with mock.patch.object(adapter, "_bridge_offers", lambda cap: cap in offers), \
             mock.patch.object(adapter, "fetch_course_documents", fetch), \
             mock.patch.object(adapter, "bridge_post",
                               lambda path, payload: sent.update(payload) or {}):
            adapter.index_course(course_id)
        return sent

    def test_scans_are_sent_when_the_bridge_reads_images(self):
        for adapter in (studip_adapter, moodle_adapter, ilias_adapter):
            sent = self._index(adapter, "7", {"image_text"},
                               [{"title": "T", "text": "Genug Text für ein Dokument."}])
            self.assertEqual([s["title"] for s in sent["scans"]], ["Scan.pdf"], adapter.__name__)

    def test_a_course_of_scans_alone_is_indexed(self):
        for adapter in (studip_adapter, moodle_adapter, ilias_adapter):
            sent = self._index(adapter, "7", {"image_text"}, [])
            self.assertEqual(sent["documents"], [], adapter.__name__)
            self.assertTrue(sent["scans"], adapter.__name__)

    def test_without_a_reader_nothing_is_collected_and_an_empty_course_still_fails(self):
        for adapter in (studip_adapter, moodle_adapter, ilias_adapter):
            with self.assertRaises(RuntimeError, msg=adapter.__name__):
                self._index(adapter, "7", set(), [])


class TestMoodle(unittest.TestCase):
    def test_scans_and_images_are_collected(self):
        sections = [{"id": 1, "name": "Woche 1", "summary": "", "modules": [{
            "id": 22, "name": "Material", "modname": "resource", "description": "",
            "contents": [
                {"type": "file", "filename": f,
                 "fileurl": f"https://moodle.example/pluginfile.php/1/{f}?forcedownload=1"}
                for f in ("scan.pdf", "tafel.png")],
        }]}]
        blobs = {"scan.pdf": SCAN, "tafel.png": PNG}

        def call(function, **params):
            return sections if function == "core_course_get_contents" else []

        def download(url):
            return blobs[url.split("/")[-1].split("?")[0]]

        scans: list = []
        with mock.patch.object(moodle_adapter, "moodle_call", side_effect=call), \
             mock.patch.object(moodle_adapter, "moodle_download", download):
            moodle_adapter.fetch_course_documents(5, scans=scans)
        found = _titles(scans)
        self.assertEqual(set(found), {"scan.pdf", "tafel.png"})
        self.assertTrue(scans[0]["activity_ref"].startswith("moodle:5:file:22:resource:"))
        self.assertEqual(scans[0]["folder"], "Woche 1")


class TestIlias(unittest.TestCase):
    def test_a_scanned_file_is_collected(self):
        def call(operation, params, timeout=120):
            p = dict(params)
            if operation == "getCourseXML":
                return ilias_adapter.parse_soap_result(
                    operation, envelope(operation, "xml", COURSE_XML))
            if operation == "getTreeChilds":
                objs = ([("fold", 101, 43, "Skripte", "")] if p["ref_id"] == "42"
                        else [("file", 103, 45, "Scan", "")])
                return ilias_adapter.parse_soap_result(
                    operation, envelope(operation, "xml", tree_xml(objs)))
            if operation == "getFileXML":
                return ilias_adapter.parse_soap_result(
                    operation, envelope(operation, "xml", file_xml("scan.pdf", SCAN)))
            raise AssertionError(operation)

        scans: list = []
        with mock.patch.object(ilias_adapter, "soap_call", side_effect=call):
            ilias_adapter.fetch_course_documents("42", sid="s", scans=scans)
        self.assertEqual([s["activity_ref"] for s in scans], ["ilias:42:file:45:scan.pdf"])
        self.assertEqual(scans[0]["folder"], "Skripte")


if __name__ == "__main__":
    unittest.main()
