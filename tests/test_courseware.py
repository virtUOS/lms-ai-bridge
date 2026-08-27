"""Tests for Stud.IP Courseware extraction.

The API is stubbed with payloads copied from a live module (2026-08-25), so
these run with no credentials and still exercise the real shapes: the
element → container → block hierarchy, HTML-marked text, and a `test` block
that references an assignment rather than carrying it.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import re
import sys
import unittest
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.studip_courseware import fetch_courseware  # noqa: E402

_TAG = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html or ""))).strip()


# Shapes taken from the live "Bild- und videogenerative KI" module.
FIXTURES = {
    "/courses/C/courseware-units?page[limit]=20": {
        "data": [{"id": "13775", "relationships": {
            "structural-element": {"data": {"id": "273923"}}}}]},
    "/courseware-structural-elements/273923": {
        "data": {"id": "273923", "attributes": {
            "title": "Bild- und videogenerative KI - Expedition KI",
            "payload": {"description": "Wie funktionieren bild- und "
                                       "videogenerative KI-Tools und wofür sind "
                                       "sie im Studium konkret nützlich?",
                        "color": "studip-blue"}}}},
    "/courseware-structural-elements/273923/descendants?page[limit]=200": {
        "data": [
            {"id": "273926", "attributes": {
                "position": 0, "title": "1. Bildgenerative Künstliche Intelligenz",
                "payload": {}}},
            {"id": "273962", "attributes": {
                "position": 1, "title": "2. Videogenerative Künstliche Intelligenz",
                "payload": {}}},
        ]},
    "/courseware-structural-elements/273926/containers?page[limit]=50": {
        "data": [{"id": "288860"}]},
    "/courseware-structural-elements/273962/containers?page[limit]=50": {
        "data": [{"id": "288861"}]},
    "/courseware-containers/288860/blocks?page[limit]=100": {
        "data": [
            {"id": "764990", "attributes": {
                "position": 2, "block-type": "test", "title": "Quiz Kapitel 1",
                "payload": {"assignment": "83876"}}},
            {"id": "764993", "attributes": {
                "position": 0, "block-type": "text", "title": "",
                "payload": {"text": "<!--HTML--><h1>Willkommen in der "
                                    "fantastischen Welt der KI-Bildgeneration!"
                                    "</h1><p>Text-to-Image erzeugt Bilder aus "
                                    "Beschreibungen.</p>"}}},
            {"id": "764996", "attributes": {
                "position": 1, "block-type": "text", "title": "",
                "payload": {"text": '<!--HTML--><figure class="image">'
                                    '<img src="https://x/sendfile.php?file_id=9" />'
                                    "</figure>"}}},
        ]},
    "/courseware-containers/288861/blocks?page[limit]=100": {
        "data": [
            {"id": "9", "attributes": {
                "position": 0, "block-type": "text", "title": "Videomodelle",
                "payload": {"text": "<p>Text-to-Video erzeugt kurze Clips aus "
                                    "einer Beschreibung.</p>"}}},
            {"id": "10", "attributes": {
                "position": 1, "block-type": "text", "visible": False,
                "title": "Entwurf",
                "payload": {"text": "<p>Noch nicht veröffentlichter Entwurf.</p>"}}},
        ]},
}


def fake_get(path):
    if path in FIXTURES:
        return FIXTURES[path]
    raise RuntimeError(f"Stud.IP 404 on {path}")


class TestCoursewareExtraction(unittest.TestCase):
    def setUp(self):
        self.docs = fetch_courseware(fake_get, strip_html, "C")

    def test_one_document_per_chapter_plus_the_overview(self):
        locators = [d["locator"] for d in self.docs]
        self.assertEqual(locators, [
            "Überblick",
            "1. Bildgenerative Künstliche Intelligenz",
            "2. Videogenerative Künstliche Intelligenz",
        ])

    def test_chapter_title_is_the_locator(self):
        # "Kapitel 2: Videogenerative KI" is a citation a reader can act on;
        # "Courseware-Einheit 13775" is not.
        chapter = self.docs[2]
        self.assertIn("Videogenerative", chapter["locator"])
        self.assertIn("Text-to-Video", chapter["text"])

    def test_module_description_is_indexed(self):
        # The one place that says what the module is *about* — which is what
        # "Worum geht es in dieser Veranstaltung?" needs.
        self.assertIn("KI-Tools", self.docs[0]["text"])

    def test_html_is_stripped_not_dumped(self):
        body = self.docs[1]["text"]
        self.assertIn("Willkommen", body)
        self.assertNotIn("<h1>", body)
        self.assertNotIn("<!--HTML-->", body)

    def test_layout_keys_are_not_indexed_as_content(self):
        # The previous implementation serialised the whole payload, indexing
        # "studip-blue" as though it were course material.
        self.assertNotIn("studip-blue", self.docs[0]["text"])
        self.assertNotIn("colspan", " ".join(d["text"] for d in self.docs))

    def test_quiz_is_noted_but_its_content_is_not_followed(self):
        # A test block references an assignment id. Indexing the questions and
        # answers is a governance decision, not an implementation detail, so
        # only the title is recorded.
        body = self.docs[1]["text"]
        self.assertIn("Selbsttest: Quiz Kapitel 1", body)
        self.assertNotIn("83876", body)

    def test_invisible_blocks_are_skipped(self):
        # An unpublished draft is not course material.
        self.assertNotIn("Entwurf", self.docs[2]["text"])

    def test_blocks_are_ordered_by_position(self):
        # The test block sits at position 2 and must come after both texts.
        body = self.docs[1]["text"]
        self.assertLess(body.index("Willkommen"), body.index("Selbsttest"))

    def test_a_course_without_courseware_is_not_an_error(self):
        def empty(path):
            raise RuntimeError("404")
        self.assertEqual(fetch_courseware(empty, strip_html, "X"), [])


if __name__ == "__main__":
    unittest.main()
