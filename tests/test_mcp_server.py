"""Tests for the stdio MCP server.

The JSON-RPC loop is driven over in-memory streams with a fake retrieval
provider, so these run offline. The wire shapes follow the MCP specification's
stdio transport: one JSON-RPC message per line, `initialize` → `tools/list` →
`tools/call`, tool results as a `content` list of text parts.
"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import mcp_server  # noqa: E402
from bridge.contract import Source  # noqa: E402


class FakeRetrieval:
    name = "fake"

    def __init__(self):
        self.store = {"ilias:86": 37, "moodle:5": 12}
        self.indexed = []
        self.forgotten = []

    def courses(self):
        return dict(self.store)

    def count(self, course_ref):
        return self.store.get(course_ref, 0)

    def retrieve(self, course_ref, query, k=4):
        if course_ref not in self.store or "Studiengebühr" in query:
            return []
        return [
            Source(title="Skript.pdf", locator="S. 12", activity_ref="ilias:86:file:87:Skript.pdf",
                   course_name="Generative KI", folder="Skripte", score=0.81),
        ][:k]

    def search(self, course_ref, query, k=4):
        return [(s, "Ein Monad ist eine algebraische Struktur …") for s in self.retrieve(course_ref, query, k)]

    def index(self, req):
        self.indexed.append(req)
        self.store[req.course_ref] = len(req.documents)
        return len(req.documents)

    def forget(self, course_ref):
        self.forgotten.append(course_ref)
        return self.store.pop(course_ref, 0)


def run(messages, provider=None):
    """Feed JSON-RPC messages through the loop, return the replies as dicts."""
    provider = provider or FakeRetrieval()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout, provider)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def call(name, arguments, rid=7):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def payload(reply):
    """The JSON a tool wrote into its first text part."""
    return json.loads(reply["result"]["content"][0]["text"])


class TestHandshake(unittest.TestCase):
    def test_initialize_answers_with_tools_capability_and_one_line_per_message(self):
        replies = run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        self.assertEqual(len(replies), 2, "a notification gets no reply")
        init = replies[0]
        self.assertEqual(init["id"], 1)
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertEqual(init["result"]["serverInfo"]["name"], "lms-ai-bridge")
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_unknown_method_is_a_json_rpc_error_not_a_crash(self):
        replies = run([{"jsonrpc": "2.0", "id": 3, "method": "resources/list"}])
        self.assertEqual(replies[0]["error"]["code"], -32601)

    def test_garbage_line_is_a_parse_error_and_the_loop_continues(self):
        stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":4,"method":"ping"}\n')
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout, FakeRetrieval())
        replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 4)


class TestTools(unittest.TestCase):
    def test_tools_list_names_the_four_tools_with_schemas(self):
        replies = run([{"jsonrpc": "2.0", "id": 5, "method": "tools/list"}])
        tools = replies[0]["result"]["tools"]
        self.assertEqual(
            sorted(t["name"] for t in tools),
            ["forget_course", "index_course", "list_indexed_courses", "search_course"],
        )
        for t in tools:
            self.assertIn("description", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
        search = next(t for t in tools if t["name"] == "search_course")
        self.assertEqual(search["inputSchema"]["required"], ["course_ref", "query"])

    def test_list_indexed_courses(self):
        reply = run([call("list_indexed_courses", {})])[0]
        body = payload(reply)
        self.assertEqual(body["provider"], "fake")
        self.assertEqual(
            {c["course_ref"]: c["chunks"] for c in body["courses"]},
            {"ilias:86": 37, "moodle:5": 12},
        )

    def test_search_returns_passages_with_citation_fields_and_no_answer(self):
        reply = run([call("search_course", {"course_ref": "ilias:86", "query": "Tokenisierung"})])[0]
        self.assertFalse(reply["result"].get("isError", False))
        body = payload(reply)
        self.assertNotIn("answer", body, "the host model answers; the tool only retrieves")
        hit = body["passages"][0]
        self.assertEqual(hit["title"], "Skript.pdf")
        self.assertEqual(hit["locator"], "S. 12")
        self.assertEqual(hit["course_name"], "Generative KI")
        self.assertEqual(hit["folder"], "Skripte")
        self.assertEqual(hit["citation"], "Skript.pdf, S. 12 (Generative KI / Skripte)")
        # The first live run returned citations without the passage text — the
        # host model had page numbers and nothing to read. Never again.
        self.assertTrue(hit["text"].startswith("Ein Monad"))

    def test_search_flags_loosely_related_passages(self):
        provider = FakeRetrieval()
        weak = Source(title="x.pdf", locator="S. 7", score=0.45)
        with mock.patch.object(provider, "search", return_value=[(weak, "…")]):
            body = payload(run([call("search_course", {"course_ref": "ilias:86", "query": "Studiengebühr?"})], provider)[0])
        self.assertEqual(body["confidence"], "low")
        self.assertIn("does not cover", body["note"])
        strong = payload(run([call("search_course", {"course_ref": "ilias:86", "query": "Tokenisierung"})])[0])
        self.assertEqual(strong["confidence"], "ok")
        self.assertNotIn("note", strong)

    def test_search_with_no_hits_says_so_instead_of_inventing(self):
        reply = run([call("search_course", {"course_ref": "ilias:86", "query": "Studiengebühr"})])[0]
        body = payload(reply)
        self.assertEqual(body["passages"], [])
        self.assertEqual(body["confidence"], "none")
        self.assertIn("nothing", body["note"].lower())

    def test_search_on_unindexed_course_is_a_tool_error_with_guidance(self):
        reply = run([call("search_course", {"course_ref": "studip:abc", "query": "x"})])[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("index_course", reply["result"]["content"][0]["text"])

    def test_unknown_tool_and_missing_argument_are_tool_errors(self):
        replies = run([
            call("make_coffee", {}, rid=8),
            call("search_course", {"course_ref": "ilias:86"}, rid=9),
        ])
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn("query", replies[1]["result"]["content"][0]["text"])

    def test_forget_course_removes_and_reports(self):
        provider = FakeRetrieval()
        reply = run([call("forget_course", {"course_ref": "moodle:5"})], provider)[0]
        self.assertEqual(payload(reply), {"course_ref": "moodle:5", "removed_chunks": 12})
        self.assertEqual(provider.forgotten, ["moodle:5"])

    def test_index_course_runs_the_platform_adapter_and_indexes(self):
        provider = FakeRetrieval()
        fake_docs = [{"activity_ref": "ilias:86:description", "title": "Kurs — Beschreibung",
                      "text": "Ein Kurs über Sprachmodelle und ihre Grenzen in der Lehre."}]
        with mock.patch.object(mcp_server, "_fetch_documents",
                               return_value=("ilias:86", fake_docs)) as fetch:
            reply = run([call("index_course", {"platform": "ilias", "course_id": "86"})], provider)[0]
        fetch.assert_called_once_with("ilias", "86")
        body = payload(reply)
        self.assertEqual(body, {"course_ref": "ilias:86", "documents": 1, "chunks": 1,
                                "provider": "fake"})
        self.assertEqual(provider.indexed[0].course_ref, "ilias:86")
        self.assertTrue(provider.indexed[0].replace)

    def test_index_course_rejects_unknown_platform(self):
        reply = run([call("index_course", {"platform": "blackboard", "course_id": "1"})])[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("moodle", reply["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
