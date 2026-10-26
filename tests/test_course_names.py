"""The builtin retrieval provider remembers each course's name for the listing."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.contract import IndexRequest  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402


def request(course_ref, course_name):
    return IndexRequest.from_dict({
        "course_ref": course_ref,
        "documents": [{"activity_ref": f"{course_ref}:file:1:a.pdf", "title": "a.pdf",
                       "locator": "S. 1", "course_name": course_name,
                       "text": "Tokenisierung zerlegt Text in Einheiten."}],
    })


class TestCourseNames(unittest.TestCase):
    def test_names_come_from_the_indexed_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = BuiltinRetrieval(Path(tmp) / "index.json")
            provider.index(request("studip:0a1b2c", "Einführung"))
            provider.index(request("ilias:86", ""))
            self.assertEqual(provider.course_names(), {"studip:0a1b2c": "Einführung", "ilias:86": ""})

    def test_the_name_survives_a_reload_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            BuiltinRetrieval(Path(tmp) / "index.json").index(request("studip:0a1b2c", "Einführung"))
            self.assertEqual(BuiltinRetrieval(Path(tmp) / "index.json").course_names(),
                             {"studip:0a1b2c": "Einführung"})


if __name__ == "__main__":
    unittest.main()
