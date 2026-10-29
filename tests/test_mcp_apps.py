"""MCP Apps view: off by default (so HAWKI sees nothing new), with
BRIDGE_MCP_APPS the view is wired up, and its two filters work."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import mcp_server  # noqa: E402
from bridge.contract import IndexDocument, IndexRequest  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402


def call(msg, provider, user=None):
    return mcp_server.handle(dict(msg, jsonrpc="2.0", id=1), provider, user=user,
                             may_see=(lambda u, ref: True) if user else None)["result"]


class McpAppsTest(unittest.TestCase):
    def setUp(self):
        self.p = BuiltinRetrieval()
        self.p.index(IndexRequest(course_ref="studip:abc", documents=[
            IndexDocument(activity_ref="studip:abc:file:f1", title="Skript.pdf", locator="S. 1",
                          text="Monaden sind eine algebraische Struktur der funktionalen Programmierung.",
                          course_name="FP", folder="Skripte"),
            IndexDocument(activity_ref="studip:abc:file:f2", title="Folien.pdf", locator="S. 1",
                          text="Funktoren und Monaden in Haskell, algebraische Struktur erklärt.",
                          course_name="FP", folder="Folien"),
        ]))

    def test_off_by_default_nothing_changes(self):
        with mock.patch.dict(os.environ, {"BRIDGE_MCP_APPS": ""}):
            init = call({"method": "initialize", "params": {}}, self.p)
            tools = call({"method": "tools/list"}, self.p)["tools"]
            res = call({"method": "tools/call", "params": {"name": "list_indexed_courses"}}, self.p)
        self.assertNotIn("resources", init["capabilities"])
        self.assertFalse(any("_meta" in t for t in tools))
        self.assertNotIn("structuredContent", res)

    def test_on_view_resource_and_data(self):
        env = {"BRIDGE_MCP_APPS": "1", "STUDIP_URL": "https://studip.example"}
        with mock.patch.dict(os.environ, env):
            tools = call({"method": "tools/list"}, self.p, user="u")["tools"]
            html = call({"method": "resources/read", "params": {"uri": mcp_server.VIEW_URI}}, self.p, user="u")
            res = call({"method": "tools/call", "params": {"name": "list_indexed_courses"}}, self.p, user="u")
        self.assertEqual({t["name"]: t.get("_meta", {}).get("ui", {}).get("resourceUri") for t in tools},
                         {"list_indexed_courses": mcp_server.VIEW_URI, "search_course": mcp_server.VIEW_URI})
        content = html["contents"][0]
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("ui/initialize", content["text"])
        self.assertEqual(content["_meta"]["ui"]["csp"]["frameDomains"], ["https://studip.example"])
        course = res["structuredContent"]["courses"][0]
        self.assertEqual([d["title"] for d in course["documents"]], ["Skript.pdf", "Folien.pdf"])
        self.assertEqual(course["documents"][0]["link"],
                         "https://studip.example/dispatch.php/file/details/f1?cid=abc")
        # the model's text stays the short listing
        self.assertNotIn("documents", res["content"][0]["text"])

    def test_platform_filter_reaches_the_view(self):
        self.p.index(IndexRequest(course_ref="moodle:5", documents=[
            IndexDocument(activity_ref="moodle:5:course", title="KI", text="Kursbeschreibung Moodle")]))
        with mock.patch.dict(os.environ, {"BRIDGE_MCP_APPS": "1"}):
            res = call({"method": "tools/call", "params": {"name": "list_indexed_courses",
                                                          "arguments": {"platform": "studip"}}}, self.p, user="u")
        self.assertEqual([c["course_ref"] for c in res["structuredContent"]["courses"]], ["studip:abc"])
        self.assertNotIn("moodle:5", res["content"][0]["text"])

    def test_documents_by_name_as_the_view_sends_them(self):
        res = call({"method": "tools/call", "params": {"name": "search_course", "arguments": {
            "course_ref": "studip:abc", "query": "Monaden algebraische Struktur",
            "documents": ["folien.PDF"]}}}, self.p, user="u")
        self.assertIn("Folien.pdf", res["content"][0]["text"])
        self.assertNotIn("Skript.pdf", res["content"][0]["text"])
        bad = call({"method": "tools/call", "params": {"name": "search_course", "arguments": {
            "course_ref": "studip:abc", "query": "Monaden", "documents": ["Nichtda.pdf"]}}},
            self.p, user="u")
        self.assertTrue(bad.get("isError"))

    def test_courseware_chapter_link_needs_the_unit(self):
        from bridge.documents import lms_link
        with mock.patch.dict(os.environ, {"STUDIP_URL": "https://s.example"}):
            self.assertEqual(lms_link("studip:abc:courseware:273929:13775"),
                             "https://s.example/dispatch.php/course/courseware/courseware/13775"
                             "?cid=abc#/structural_element/273929")
            self.assertEqual(lms_link("studip:abc:courseware:273929"),
                             "https://s.example/dispatch.php/course/courseware?cid=abc")

    def test_search_only_selected_documents(self):
        only = mcp_server.doc_id("studip:abc:file:f2")
        res = call({"method": "tools/call", "params": {"name": "search_course", "arguments": {
            "course_ref": "studip:abc", "query": "Monaden algebraische Struktur", "documents": [only]}}},
            self.p, user="u")
        self.assertIn("Folien.pdf", res["content"][0]["text"])
        self.assertNotIn("Skript.pdf", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
