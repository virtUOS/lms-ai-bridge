"""Asking the index what it holds, rather than inferring it from a query.

The demo reported a fully indexed course as empty. It had probed with a nonsense
question and treated "no results" as "nothing indexed" — but embeddings
correctly found nothing similar to "__probe__", and a course can be indexed and
still hold nothing relevant to a given question. Inference was the wrong tool;
the contract was missing a way to just ask.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.contract import IndexRequest, RetrievalProvider  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402


class TestCountIsPartOfTheContract(unittest.TestCase):
    def test_the_interface_declares_it(self):
        # Every provider must answer it, including ones written elsewhere —
        # ByCS's or OSKI's engine behind this same seam.
        self.assertTrue(hasattr(RetrievalProvider, "count"))

    def test_the_default_is_zero_not_an_error(self):
        self.assertEqual(RetrievalProvider().count("studip:x"), 0)


class TestBuiltinCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.r = BuiltinRetrieval(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_zero_before_indexing(self):
        self.assertEqual(self.r.count("studip:x"), 0)

    def test_counts_chunks_after_indexing(self):
        self.r.index(IndexRequest.from_dict({
            "course_ref": "studip:x",
            "documents": [{"title": "H.pdf", "locator": "S. 5",
                           "text": "Einführung, Recht, Prävention, Abschluss."}],
        }))
        self.assertGreater(self.r.count("studip:x"), 0)

    def test_an_indexed_course_counts_even_when_a_query_finds_nothing(self):
        # The exact case that produced the wrong report: material is present,
        # this particular question matches none of it.
        self.r.index(IndexRequest.from_dict({
            "course_ref": "studip:x",
            "documents": [{"title": "H.pdf", "text": "Beratung und Prävention."}],
        }))
        self.assertEqual(self.r.retrieve("studip:x", "__probe__"), [])
        self.assertGreater(self.r.count("studip:x"), 0)

    def test_forget_returns_it_to_zero(self):
        self.r.index(IndexRequest.from_dict({
            "course_ref": "studip:x",
            "documents": [{"title": "H.pdf", "text": "Etwas Text hier drin."}],
        }))
        self.r.forget("studip:x")
        self.assertEqual(self.r.count("studip:x"), 0)


class TestDemoUsesTheRouteNotAProbe(unittest.TestCase):
    def test_the_page_asks_the_index_directly(self):
        from bridge.demo_page import PAGE                     # noqa: PLC0415
        self.assertIn("/v1/index/status", PAGE)
        # The old probe must be gone from the code. It still appears in a
        # comment explaining the bug, which is deliberate — so check that it is
        # not sent anywhere rather than that the string is absent.
        self.assertNotIn('content: "__probe__"', PAGE)
        self.assertNotIn("content: '__probe__'", PAGE)

    def test_the_server_serves_that_route(self):
        server = (Path(__file__).resolve().parent.parent
                  / "bridge" / "server.py").read_text()
        self.assertIn('route == "/v1/index/status"', server)


if __name__ == "__main__":
    unittest.main()
