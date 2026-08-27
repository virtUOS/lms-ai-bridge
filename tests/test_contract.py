"""Tests for the bridge contract and the built-in retrieval provider.

Run:  python3 -m unittest discover -s tests -v
No dependencies beyond the standard library.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.contract import (  # noqa: E402
    ChatRequest, ChatResponse, IndexRequest, Source, namespace_of,
)
from bridge.providers.builtin_retrieval import BuiltinRetrieval, chunk, tokenize  # noqa: E402
from bridge.providers.external_stub import NullRetrieval  # noqa: E402
from bridge.providers.openai_chat import EchoChat  # noqa: E402


class TestReferences(unittest.TestCase):
    def test_namespace_extraction(self):
        self.assertEqual(namespace_of("moodle:1234"), "moodle")
        self.assertEqual(namespace_of("studip:a1b2:wiki:9"), "studip")
        self.assertEqual(namespace_of("ilias:crs_99"), "ilias")

    def test_unknown_namespace_is_not_an_error(self):
        # The bridge must never crash on a malformed ref; it is opaque data.
        self.assertEqual(namespace_of(""), "unknown")
        self.assertEqual(namespace_of("nonsense"), "unknown")


class TestChatRequest(unittest.TestCase):
    def test_parses_minimal_request(self):
        r = ChatRequest.from_dict({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.messages[0].content, "hi")
        self.assertEqual(r.course_ref, "")      # ungrounded is valid
        self.assertEqual(r.locale, "de")

    def test_rejects_empty_messages(self):
        with self.assertRaises(ValueError):
            ChatRequest.from_dict({"messages": []})

    def test_rejects_malformed_message(self):
        with self.assertRaises(ValueError):
            ChatRequest.from_dict({"messages": [{"role": "user"}]})


class TestChatResponse(unittest.TestCase):
    def test_sources_always_present_even_when_empty(self):
        # Adapters must not need to special-case deployments without retrieval.
        d = ChatResponse(answer="x").to_dict()
        self.assertIn("sources", d)
        self.assertEqual(d["sources"], [])

    def test_sources_serialise_fully(self):
        d = ChatResponse(
            answer="x", sources=[Source(title="V3.pdf", locator="S. 12")]
        ).to_dict()
        self.assertEqual(d["sources"][0]["title"], "V3.pdf")
        self.assertEqual(d["sources"][0]["locator"], "S. 12")


class TestIndexRequest(unittest.TestCase):
    def test_requires_course_ref_and_documents(self):
        with self.assertRaises(ValueError):
            IndexRequest.from_dict({"documents": [{"text": "x"}]})
        with self.assertRaises(ValueError):
            IndexRequest.from_dict({"course_ref": "moodle:1", "documents": []})

    def test_document_requires_text(self):
        with self.assertRaises(ValueError):
            IndexRequest.from_dict(
                {"course_ref": "moodle:1", "documents": [{"title": "no text"}]}
            )


class TestChunking(unittest.TestCase):
    def test_splits_on_paragraphs(self):
        text = "\n\n".join(["Absatz eins." * 30, "Absatz zwei." * 30])
        self.assertGreater(len(chunk(text, size=400)), 1)

    def test_short_text_stays_whole(self):
        self.assertEqual(len(chunk("kurz")), 1)

    def test_stopwords_removed(self):
        self.assertNotIn("der", tokenize("der Monad"))
        self.assertIn("monad", tokenize("der Monad"))


class TestBuiltinRetrieval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.r = BuiltinRetrieval(self.tmp.name)
        self.r.index(IndexRequest.from_dict({
            "course_ref": "studip:hpc",
            "documents": [
                {"activity_ref": "studip:hpc:wiki:1", "title": "SLURM",
                 "text": "Jobs werden mit sbatch über SLURM eingereicht. "
                         "Ein Jobskript beschreibt die angeforderten Ressourcen."},
                {"activity_ref": "studip:hpc:wiki:2", "title": "Conda",
                 "text": "Mit conda lassen sich Umgebungen auf dem Cluster "
                         "verwalten. Module werden per spack geladen."},
            ],
        }))

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_retrieves_the_relevant_document(self):
        s = self.r.retrieve("studip:hpc", "Wie reiche ich einen Job mit SLURM ein?")
        self.assertTrue(s)
        self.assertEqual(s[0].title, "SLURM")

    def test_unrelated_query_returns_nothing(self):
        # The behaviour that makes grounding worth having: no invented answer.
        self.assertEqual(self.r.retrieve("studip:hpc", "Studiengebühren Bafög"), [])

    def test_unknown_course_is_empty_not_an_error(self):
        self.assertEqual(self.r.retrieve("moodle:999", "irgendwas"), [])

    def test_passages_align_with_sources(self):
        q = "conda Umgebungen"
        self.assertEqual(
            len(self.r.retrieve("studip:hpc", q)), len(self.r.passages("studip:hpc", q))
        )

    def test_forget_removes_everything_for_a_course(self):
        self.assertGreater(self.r.forget("studip:hpc"), 0)
        self.assertEqual(self.r.retrieve("studip:hpc", "SLURM"), [])

    def test_reindex_replaces_rather_than_duplicates(self):
        before = len(self.r.retrieve("studip:hpc", "SLURM sbatch"))
        self.r.index(IndexRequest.from_dict({
            "course_ref": "studip:hpc",
            "documents": [{"activity_ref": "studip:hpc:wiki:1", "title": "SLURM",
                           "text": "Jobs werden mit sbatch über SLURM eingereicht."}],
            "replace": True,
        }))
        self.assertLessEqual(len(self.r.retrieve("studip:hpc", "SLURM sbatch")), before)

    def test_index_survives_restart(self):
        again = BuiltinRetrieval(self.tmp.name)
        self.assertTrue(again.retrieve("studip:hpc", "SLURM"))


class TestNullRetrieval(unittest.TestCase):
    def test_degrades_quietly(self):
        # A deployment with no retrieval still serves ungrounded chat.
        n = NullRetrieval()
        self.assertEqual(n.retrieve("moodle:1", "x"), [])
        self.assertEqual(n.forget("moodle:1"), 0)


class TestEchoChat(unittest.TestCase):
    def test_works_without_any_model_configured(self):
        answer, usage = EchoChat().complete(
            ChatRequest.from_dict(
                {"messages": [{"role": "user", "content": "Was ist SLURM?"}]}
            ).messages
        )
        self.assertIn("SLURM", answer)
        self.assertEqual(usage["prompt_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
