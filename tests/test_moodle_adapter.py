"""Tests for the Moodle adapter's document assembly.

Only the pure part is tested: turning a Moodle API response into documents.
The HTTP call is patched, so these run offline like the rest of the suite.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import moodle_adapter  # noqa: E402
from test_extract import make_pdf  # noqa: E402


class TestCourseSummaryIsIndexed(unittest.TestCase):
    """The course description is material a lecturer wrote, so it is indexed.

    It was invisible for as long as the adapter read only *section* summaries:
    `core_course_get_contents` never returns the course-level summary, and on a
    seeded test course the whole German description — the one text a lecturer
    is most likely to have filled in — produced no document at all.
    """

    SECTIONS = [
        {
            "id": 13,
            "name": "General",
            "summary": "",
            "modules": [
                {"id": 22, "name": "Skript",
                 "description": "Begleitendes Skript zur Vorlesung.",
                 "contents": []},
            ],
        }
    ]

    def _fetch(self, courses, sections=None):
        def call(function, **params):
            if function == "core_course_get_contents":
                return sections if sections is not None else self.SECTIONS
            if function == "core_course_get_courses":
                return courses
            raise AssertionError(f"unexpected call {function}")

        with mock.patch.object(moodle_adapter, "moodle_call", side_effect=call):
            return moodle_adapter.fetch_course_documents(5)

    def test_course_summary_becomes_a_document(self):
        _, docs = self._fetch([
            {"id": 5, "fullname": "Generative KI",
             "summary": "<h3>Modul 1</h3><p>Grundlagen der Sprachmodelle.</p>"},
        ])
        summaries = [d for d in docs if d["activity_ref"].endswith(":course")]
        self.assertEqual(len(summaries), 1, docs)
        self.assertIn("Grundlagen der Sprachmodelle", summaries[0]["text"])
        self.assertNotIn("<h3>", summaries[0]["text"])

    def test_empty_course_summary_adds_nothing(self):
        """An unfilled description is not a document with no text."""
        _, docs = self._fetch([{"id": 5, "fullname": "Leer", "summary": ""}])
        self.assertEqual([d for d in docs if d["activity_ref"].endswith(":course")], [])

    def test_a_failed_lookup_does_not_lose_the_rest_of_the_course(self):
        """A token without core_course_get_courses still indexes the modules.

        Least-privilege tokens are the norm for a read-only adapter, and one
        missing function should cost its own document, not the whole run.
        """
        def call(function, **params):
            if function == "core_course_get_contents":
                return self.SECTIONS
            raise RuntimeError("Moodle error: accessexception")

        with mock.patch.object(moodle_adapter, "moodle_call", side_effect=call):
            _, docs = moodle_adapter.fetch_course_documents(5)
        self.assertTrue(docs)
        self.assertEqual([d for d in docs if d["activity_ref"].endswith(":course")], [])


class TestFileExtraction(unittest.TestCase):
    """Files are downloaded and their contents indexed, as on Stud.IP.

    Until 2026-08-27 the adapter recorded `[Datei: name]` and stopped there, so
    a Moodle course whose substance sits in attachments — which is most of them
    — indexed as a list of filenames. The Stud.IP adapter had done the real
    thing since the start; this brings the two to parity.
    """

    def _sections(self, filename="skript.pdf", url=None):
        return [{
            "id": 13, "name": "General", "summary": "",
            "modules": [{
                "id": 22, "name": "Skript", "description": "",
                "contents": [{
                    "type": "file",
                    "filename": filename,
                    "fileurl": url or f"https://moodle.example/webservice/"
                                      f"pluginfile.php/1/mod_resource/content/1/"
                                      f"{filename}?forcedownload=1",
                }],
            }],
        }]

    def _run(self, sections, blob=b"", downloader=None):
        def call(function, **params):
            if function == "core_course_get_contents":
                return sections
            return []

        dl = downloader or mock.Mock(return_value=blob)
        with mock.patch.object(moodle_adapter, "moodle_call", side_effect=call), \
             mock.patch.object(moodle_adapter, "moodle_download", dl):
            return moodle_adapter.fetch_course_documents(5), dl

    def test_pdf_contents_are_indexed_one_document_per_page(self):
        pdf = make_pdf(["Erste Seite über Monaden", "Zweite Seite über Funktoren"])
        (_, docs), _ = self._run(self._sections(), blob=pdf)
        pages = [d for d in docs if ":file:" in d["activity_ref"]]
        self.assertEqual(len(pages), 2, docs)
        self.assertEqual(pages[0]["locator"], "S. 1")
        self.assertIn("Monaden", pages[0]["text"])
        self.assertIn("Funktoren", pages[1]["text"])

    def test_the_token_is_appended_with_ampersand(self):
        """fileurl already carries ?forcedownload=1.

        Appending `?token=` there makes a second query string, and Moodle
        answers **HTTP 200 with a JSON error body** saying the token is
        missing while one is plainly being sent — the same shape as the Stud.IP
        Accept-header trap. Verified against a live instance 2026-08-27.
        """
        url = "https://moodle.example/pluginfile.php/1/x/skript.pdf?forcedownload=1"
        with mock.patch.dict(os.environ, {"MOODLE_TOKEN": "abc", "MOODLE_URL": "https://moodle.example"}):
            built = moodle_adapter.with_token(url)
        self.assertIn("&token=abc", built)
        self.assertEqual(built.count("?"), 1)

    def test_a_url_without_a_query_string_still_gets_one(self):
        with mock.patch.dict(os.environ, {"MOODLE_TOKEN": "abc", "MOODLE_URL": "https://moodle.example"}):
            built = moodle_adapter.with_token("https://moodle.example/pluginfile.php/1/x/s.pdf")
        self.assertIn("?token=abc", built)

    def test_an_unreadable_file_does_not_fail_the_course(self):
        """One scanned PDF should cost its own document, not the whole run."""
        (_, docs), _ = self._run(self._sections(), blob=b"%PDF-1.4 no streams here")
        self.assertEqual([d for d in docs if ":file:" in d["activity_ref"]], [])

    def test_a_failed_download_does_not_fail_the_course(self):
        dl = mock.Mock(side_effect=RuntimeError("403 Forbidden"))
        (_, docs), _ = self._run(self._sections(), downloader=dl)
        self.assertEqual([d for d in docs if ":file:" in d["activity_ref"]], [])

    def test_formats_with_no_extractor_are_skipped_not_guessed(self):
        (_, docs), dl = self._run(self._sections(filename="video.mp4"),
                                  blob=b"\x00\x00\x00 ftypmp42")
        self.assertEqual([d for d in docs if ":file:" in d["activity_ref"]], [])
        dl.assert_not_called()

    def test_the_filename_placeholder_is_gone(self):
        """The old behaviour indexed "[Datei: x.pdf]" as if it were content."""
        pdf = make_pdf(["Inhalt"])
        (_, docs), _ = self._run(self._sections(), blob=pdf)
        self.assertFalse([d for d in docs if "[Datei:" in d["text"]], docs)


if __name__ == "__main__":
    unittest.main()