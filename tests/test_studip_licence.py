"""The licence a Stud.IP file arrived under is stored, not only logged.

Stud.IP reports `terms-of-use` per file-ref. Until 2026-09-23 the adapter
printed it to stderr and dropped it; a data-protection review asks exactly this
question of an index, so it now travels with every unit of the file. It is
empty — never guessed — when Stud.IP reports nothing.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters.studip_adapter as A  # noqa: E402

FILES = {
    "data": [
        {"id": "f1", "attributes": {
            "name": "Skript.pdf", "mime-type": "application/pdf",
            "is-downloadable": True},
            "relationships": {"terms-of-use": {"data": {"id": "UNDEF_LICENSE"}}}},
        {"id": "f2", "attributes": {
            "name": "Folien.pdf", "mime-type": "application/pdf",
            "is-downloadable": True}},
    ]
}


class TestFileLicence(unittest.TestCase):
    def setUp(self):
        self._saved = (A.studip_get, A.studip_download, A.extract_pdf_pages_with_engine)
        A.studip_get = lambda path: FILES if "file-refs" in path else {"data": []}
        A.studip_download = lambda ref_id: b"%PDF-fake"
        A.extract_pdf_pages_with_engine = lambda blob: (
            ["Genug Text fuer eine Seite, die nicht verworfen wird."], "poppler")

    def tearDown(self):
        A.studip_get, A.studip_download, A.extract_pdf_pages_with_engine = self._saved

    def test_reported_licence_is_stored_on_every_unit(self):
        docs = A.fetch_course_files("C")
        skript = [d for d in docs if d["title"] == "Skript.pdf"]
        self.assertTrue(skript)
        self.assertTrue(all(d["licence"] == "UNDEF_LICENSE" for d in skript))

    def test_missing_licence_is_empty_not_invented(self):
        docs = A.fetch_course_files("C")
        folien = [d for d in docs if d["title"] == "Folien.pdf"]
        self.assertTrue(folien)
        self.assertTrue(all(d["licence"] == "" for d in folien))


if __name__ == "__main__":
    unittest.main()
