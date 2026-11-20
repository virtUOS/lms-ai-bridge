"""Scans: material whose text has to be read from pixels.

The adapter side decides *what* to send, and those decisions are the ones that
cost money: every page sent is a model call on a shared GPU, or a paid one. So
the tests pin down which pages and files qualify, and that nothing else does.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.scans import is_image, scan_entry, short_pages  # noqa: E402


class TestImageDetection(unittest.TestCase):
    def test_common_image_formats_are_recognised(self):
        for name in ("Tafelbild.png", "scan.JPG", "foto.jpeg", "a.webp",
                     "alt.tif", "alt.tiff", "b.bmp", "c.gif"):
            self.assertTrue(is_image(name), name)

    def test_mime_type_alone_is_enough(self):
        # Moodle and Stud.IP names can lack a suffix; the MIME type still says it.
        self.assertTrue(is_image("Tafelbild", "image/png"))

    def test_documents_and_svg_are_not_images_to_read(self):
        # SVG is text already, and a PDF has its own path.
        for name, mime in (("Skript.pdf", "application/pdf"),
                           ("logo.svg", "image/svg+xml"),
                           ("Folien.pptx", "")):
            self.assertFalse(is_image(name, mime), name)


class TestShortPages(unittest.TestCase):
    def test_pages_below_the_indexing_floor_are_returned_one_based(self):
        pages = ["Einführung in die Statistik", "", "  12 ", "Lineare Regression"]
        self.assertEqual(short_pages(pages), [2, 3])

    def test_whitespace_does_not_count_as_text(self):
        self.assertEqual(short_pages(["\n\t   \n"]), [1])

    def test_a_document_with_text_on_every_page_needs_nothing(self):
        self.assertEqual(short_pages(["Kapitel eins, Einleitung", "Kapitel zwei, Methoden"]), [])


class TestScanEntry(unittest.TestCase):
    def test_carries_what_the_bridge_needs_to_index_the_result(self):
        e = scan_entry(b"%PDF-1.4 bytes", title="Skript.pdf",
                       activity_ref="studip:C:file:f1", licence="CC_BY",
                       course_name="Statistik I", folder="Folien", pages=[2, 5])
        self.assertEqual(base64.b64decode(e["content_base64"]), b"%PDF-1.4 bytes")
        self.assertEqual(e["title"], "Skript.pdf")
        self.assertEqual(e["activity_ref"], "studip:C:file:f1")
        self.assertEqual(e["licence"], "CC_BY")
        self.assertEqual(e["course_name"], "Statistik I")
        self.assertEqual(e["folder"], "Folien")
        self.assertEqual(e["pages"], [2, 5])

    def test_no_page_list_means_the_whole_file(self):
        # A scanned PDF or an image: every page, and the bridge counts them.
        e = scan_entry(b"\x89PNG", title="Tafel.png", activity_ref="r")
        self.assertNotIn("pages", e)


import shutil  # noqa: E402

from bridge.scans import (  # noqa: E402
    can_render_pdf, pages_with_images, pdf_page_count, render_pdf_page,
)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_fixtures import make_pdf  # noqa: E402

_POPPLER = bool(shutil.which("pdftoppm") and shutil.which("pdfimages")
                and shutil.which("pdfinfo"))

MIXED = make_pdf([
    (["Kapitel 1: Einleitung in die Statistik"], False),   # text
    ([], True),                                             # screenshot slide
    (["Teil 2"], False),                                    # divider, no image
    (["Abbildung 4"], True),                                # short caption + image
])


@unittest.skipUnless(_POPPLER, "poppler-utils not installed")
class TestPdfPages(unittest.TestCase):
    def test_counts_pages(self):
        self.assertEqual(pdf_page_count(MIXED), 4)

    def test_finds_the_pages_that_carry_an_image(self):
        self.assertEqual(pages_with_images(MIXED), {2, 4})

    def test_renders_one_page_to_png_at_the_requested_size(self):
        png = render_pdf_page(MIXED, 2, longest_edge=400)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        # PNG IHDR: width and height, big-endian, at bytes 16..24.
        w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        self.assertEqual(max(w, h), 400)

    def test_rendering_is_available_here(self):
        self.assertTrue(can_render_pdf())


class TestAddScan(unittest.TestCase):
    def test_adds_an_entry_and_says_so(self):
        from bridge.scans import add_scan
        scans: list = []
        self.assertTrue(add_scan(scans, b"%PDF-1.4", title="Scan.pdf",
                                 activity_ref="r", pages=[1]))
        self.assertEqual([s["title"] for s in scans], ["Scan.pdf"])

    def test_a_file_that_is_not_really_an_image_is_skipped(self):
        # Named .png, holds HTML: a login page saved under the wrong name.
        from bridge.scans import add_scan
        scans: list = []
        self.assertFalse(add_scan(scans, b"<html>", title="Tafel.png", activity_ref="r"))
        self.assertEqual(scans, [])

    def test_oversized_files_are_skipped_before_they_cost_anything(self):
        from bridge import scans as S
        scans: list = []
        big_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * (S.MAX_IMAGE_BYTES + 1)
        self.assertFalse(S.add_scan(scans, big_image, title="Foto.png", activity_ref="r"))
        big_pdf = b"%PDF" + b"\x00" * (S.MAX_PDF_BYTES + 1)
        self.assertFalse(S.add_scan(scans, big_pdf, title="Scan.pdf", activity_ref="r"))
        self.assertEqual(scans, [])


class TestScannedPdfIsNamed(unittest.TestCase):
    @unittest.skipUnless(_POPPLER, "poppler-utils not installed")
    def test_a_scan_raises_its_own_exception(self):
        # The adapters queue a scan for reading and skip any other unreadable
        # PDF, so they must be able to tell the two apart.
        from bridge.extract import PdfExtractionError, ScannedPdfError, \
            extract_pdf_pages_with_engine
        scan = make_pdf([([], True), ([], True)])
        with self.assertRaises(ScannedPdfError):
            extract_pdf_pages_with_engine(scan)
        self.assertTrue(issubclass(ScannedPdfError, PdfExtractionError))


class TestImageMime(unittest.TestCase):
    def test_types_come_from_the_bytes_not_the_name(self):
        from bridge.scans import image_mime
        cases = {
            b"\x89PNG\r\n\x1a\nrest": "image/png",
            b"\xff\xd8\xff\xe0rest": "image/jpeg",
            b"GIF89arest": "image/gif",
            b"RIFF\x00\x00\x00\x00WEBPrest": "image/webp",
            b"II*\x00rest": "image/tiff",
            b"MM\x00*rest": "image/tiff",
            b"BMrest": "image/bmp",
        }
        for blob, mime in cases.items():
            self.assertEqual(image_mime(blob), mime, blob[:8])

    def test_anything_else_is_not_an_image(self):
        from bridge.scans import image_mime
        self.assertEqual(image_mime(b"%PDF-1.4"), "")
        self.assertEqual(image_mime(b"<svg"), "")


class TestTextCache(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "cache.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_result_survives_a_restart(self):
        from bridge.scans import TextCache, cache_key
        key = cache_key(b"same file", 3, "ocr:m")
        TextCache(self.path).put(key, "Seite drei")
        self.assertEqual(TextCache(self.path).get(key), "Seite drei")

    def test_an_empty_page_is_remembered_too(self):
        # "Nothing on it" cost a model call to learn, same as text did.
        from bridge.scans import TextCache, cache_key
        key = cache_key(b"f", 1, "ocr:m")
        TextCache(self.path).put(key, "")
        self.assertEqual(TextCache(self.path).get(key), "")

    def test_the_key_changes_with_file_page_and_model(self):
        from bridge.scans import cache_key
        base = cache_key(b"f", 1, "ocr:m")
        self.assertNotEqual(base, cache_key(b"g", 1, "ocr:m"))
        self.assertNotEqual(base, cache_key(b"f", 2, "ocr:m"))
        self.assertNotEqual(base, cache_key(b"f", 1, "vision:m"))

    def test_a_missing_or_damaged_file_starts_empty(self):
        from bridge.scans import TextCache
        self.assertIsNone(TextCache(self.path).get("k"))
        self.path.write_text("{not json")
        self.assertIsNone(TextCache(self.path).get("k"))


class TestWithoutPoppler(unittest.TestCase):
    def test_rendering_reports_unavailable_rather_than_failing_later(self):
        from unittest import mock
        import bridge.scans as S
        with mock.patch.object(S.shutil, "which", lambda name: None):
            self.assertFalse(S.can_render_pdf())


if __name__ == "__main__":
    unittest.main()
