"""Tests for the ILIAS adapter's SOAP parsing and document assembly.

Only the pure part is tested: turning ILIAS's SOAP replies into documents.
The transport is patched, so these run offline like the rest of the suite.

The reply shapes are taken from a live ILIAS 10.11 (2026-09-22), not
invented: the empty `<Objects/>` tree, the `getCourseXML` fault on a
non-course, and the per-operation result element names.
"""

import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ilias_adapter  # noqa: E402
from test_extract import make_pdf  # noqa: E402


def envelope(operation, element, payload):
    return (
        '<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope '
        'xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ns1="urn:ilUserAdministration"><SOAP-ENV:Body>'
        f"<ns1:{operation}Response><{element} xsi:type=\"xsd:string\">{payload}"
        f"</{element}></ns1:{operation}Response></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


COURSE_XML = (
    '&lt;Course exportVersion="7"&gt;&lt;MetaData&gt;&lt;General&gt;'
    '&lt;Title Language="de"&gt;Generative KI&lt;/Title&gt;'
    '&lt;Description Language="de"&gt;Ein Kurs über Sprachmodelle, ihre Grenzen '
    'und ihren Einsatz in der Hochschullehre.&lt;/Description&gt;'
    '&lt;/General&gt;&lt;/MetaData&gt;&lt;Settings&gt;&lt;Syllabus&gt;'
    'Woche 1: Tokenisierung. Woche 2: Aufmerksamkeit. Woche 3: Halluzinationen.'
    '&lt;/Syllabus&gt;&lt;/Settings&gt;&lt;/Course&gt;'
)


def tree_xml(objects):
    body = "".join(
        f'&lt;Object type="{t}" obj_id="{o}"&gt;&lt;Title&gt;{title}&lt;/Title&gt;'
        f'&lt;Description&gt;{desc}&lt;/Description&gt;&lt;Owner&gt;6&lt;/Owner&gt;'
        f'&lt;References ref_id="{r}" parent_id="10" accessInfo="granted"/&gt;&lt;/Object&gt;'
        for t, o, r, title, desc in objects
    )
    return f"&lt;Objects&gt;{body}&lt;/Objects&gt;"


def file_xml(name, blob):
    b64 = base64.b64encode(blob).decode()
    return (
        f'&lt;File obj_id="il_0_file_7" version="1"&gt;&lt;Filename&gt;{name}&lt;/Filename&gt;'
        f"&lt;Title&gt;{name[:-4]}&lt;/Title&gt;&lt;Versions&gt;"
        f'&lt;Version version="1" max_version="1" date="1" usr_id="6" action="create" mode="PLAIN"&gt;'
        f"{b64}&lt;/Version&gt;&lt;/Versions&gt;&lt;/File&gt;"
    )


class TestSoapReplies(unittest.TestCase):
    def test_result_element_is_read_by_position_not_name(self):
        self.assertEqual(
            ilias_adapter.parse_soap_result("login", envelope("login", "sid", "abc::hsos")),
            "abc::hsos",
        )
        self.assertEqual(
            ilias_adapter.parse_soap_result("logout", envelope("logout", "success", "true")),
            "true",
        )

    def test_fault_carries_the_server_message(self):
        reply = (
            "<SOAP-ENV:Envelope><SOAP-ENV:Body><SOAP-ENV:Fault><faultcode>Client</faultcode>"
            "<faultstring>Wrong type root for id. Expected: crs</faultstring>"
            "</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"
        )
        with self.assertRaises(ilias_adapter.IliasSoapError) as ctx:
            ilias_adapter.parse_soap_result("getCourseXML", reply)
        self.assertIn("Expected: crs", str(ctx.exception))


class TestCourseAssembly(unittest.TestCase):
    """A course with a description, a folder holding a PDF, and a forum."""

    def _fetch(self, pdf_blob):
        calls = []

        def call(operation, params, timeout=120):
            calls.append(operation)
            p = dict(params)
            if operation == "getCourseXML":
                return ilias_adapter.parse_soap_result(
                    operation, envelope(operation, "xml", COURSE_XML))
            if operation == "getTreeChilds":
                if p["ref_id"] == "42":
                    objs = [("fold", 101, 43, "Skripte", ""),
                            ("frm", 102, 44, "Forum", "Fragen zur Vorlesung")]
                else:
                    objs = [("file", 103, 45, "Skript", "")]
                return ilias_adapter.parse_soap_result(
                    operation, envelope(operation, "xml", tree_xml(objs)))
            if operation == "getFileXML":
                self.assertEqual(p["attachment_mode"], 1)
                return ilias_adapter.parse_soap_result(
                    operation, envelope(operation, "xml", file_xml("skript.pdf", pdf_blob)))
            raise AssertionError(operation)

        with mock.patch.object(ilias_adapter, "soap_call", side_effect=call):
            course_ref, docs = ilias_adapter.fetch_course_documents("42", sid="s")
        return course_ref, docs, calls

    def test_description_syllabus_and_file_pages_become_documents(self):
        pdf = make_pdf(["Tokenisierung zerlegt Text in Einheiten.",
                        "Aufmerksamkeit gewichtet Kontext."])
        course_ref, docs, calls = self._fetch(pdf)
        self.assertEqual(course_ref, "ilias:42")
        refs = [d["activity_ref"] for d in docs]
        self.assertIn("ilias:42:description", refs)
        self.assertIn("ilias:42:syllabus", refs)
        pages = [d for d in docs if d["activity_ref"].startswith("ilias:42:file:45:")]
        self.assertEqual([d["locator"] for d in pages], ["S. 1", "S. 2"])
        self.assertEqual(pages[0]["folder"], "Skripte")
        self.assertEqual(pages[0]["course_name"], "Generative KI")
        # The forum's short description is below the floor and is not a document.
        self.assertNotIn("ilias:42:obj:44", refs)
        # One tree read per container, one file read, no login (session supplied).
        self.assertNotIn("login", calls)
        self.assertEqual(calls.count("getTreeChilds"), 2)

    def test_unreadable_file_costs_its_own_document_not_the_course(self):
        _, docs, _ = self._fetch(b"%PDF-1.4 not really a pdf")
        self.assertTrue(any(d["activity_ref"] == "ilias:42:description" for d in docs))
        self.assertFalse(any(":file:" in d["activity_ref"] for d in docs))


class TestFileXml(unittest.TestCase):
    def test_last_version_wins_and_non_plain_is_refused(self):
        xml = (
            '<File obj_id="il_0_file_1"><Filename>a.pdf</Filename><Versions>'
            f'<Version version="1" mode="PLAIN">{base64.b64encode(b"old").decode()}</Version>'
            f'<Version version="2" mode="PLAIN">{base64.b64encode(b"new").decode()}</Version>'
            "</Versions></File>"
        )
        self.assertEqual(ilias_adapter.parse_file_xml(xml), ("a.pdf", "a.pdf", b"new"))
        gz = xml.replace('version="2" mode="PLAIN"', 'version="2" mode="GZIP"')
        with self.assertRaises(ilias_adapter.IliasSoapError):
            ilias_adapter.parse_file_xml(gz)


if __name__ == "__main__":
    unittest.main()
