"""Tests for the Moodle adapter's document assembly.

Only the pure part is tested: turning a Moodle API response into documents.
The HTTP call is patched, so these run offline like the rest of the suite.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))

import moodle_adapter  # noqa: E402


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
                {"id": 22, "name": "Skript", "description": "",
                 "contents": [{"type": "file", "filename": "skript.pdf"}]},
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


if __name__ == "__main__":
    unittest.main()
