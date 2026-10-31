"""MCP revision 2026-07-28 next to the older ones ("dual-era").

2026-07-28 drops the `initialize` handshake: every request carries its
protocol version in `params._meta`, and over HTTP also in headers that must
match the body. The bridge keeps answering `initialize` for clients on
2025-11-25 and earlier (LibreChat's SDK 1.32 is one), and serves a request
that carries the new `_meta` statelessly. HAWKI's PHP SDK v2 probes
`server/discover` first and switches to the new revision as soon as it gets
an answer.

Spec: modelcontextprotocol.io/specification/2026-07-28, basic/versioning
(compatibility matrix) and basic/transports/streamable-http (request
headers, server validation).
"""

import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import mcp_server  # noqa: E402
from bridge import server as bridge_server  # noqa: E402
from bridge.contract import IndexRequest  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402
from bridge.providers.openai_chat import EchoChat  # noqa: E402

MODERN = "2026-07-28"


def envelope(version=MODERN):
    """The per-request `_meta` a 2026-07-28 client sends."""
    return {"io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}


def modern_message(rid, method, params=None, version=MODERN):
    params = dict(params or {})
    params["_meta"] = envelope(version)
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def course_store(directory):
    retrieval = BuiltinRetrieval(Path(directory) / "index.json")
    retrieval.index(IndexRequest.from_dict({
        "course_ref": "ilias:86",
        "documents": [{"activity_ref": "ilias:86:file:87:skript.pdf", "title": "Skript.pdf",
                       "locator": "S. 12", "course_name": "Generative KI",
                       "text": "Tokenisierung zerlegt Text in Einheiten, die das Modell verarbeitet."}],
    }))
    return retrieval


class TestModernOverHttp(unittest.TestCase):
    token = "secret-for-tests"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        bridge_server.Handler.retrieval_provider = course_store(cls.tmp.name)
        bridge_server.Handler.chat_provider = EchoChat()
        bridge_server.Handler.transcription_provider = None
        bridge_server.Handler.auth_token = cls.token
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), bridge_server.Handler)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/mcp"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        bridge_server.Handler.auth_token = ""
        cls.tmp.cleanup()

    def send(self, message, headers):
        """POST one message; (status, JSON body) for 2xx and 4xx alike."""
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {self.token}"}
        h.update({k: v for k, v in headers.items() if v is not None})
        req = urllib.request.Request(self.url, data=json.dumps(message).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read()
            e.close()
            return e.code, (json.loads(body) if body else None)

    def modern(self, rid, method, params=None, version=MODERN, headers=None):
        """A request as a 2026-07-28 client sends it: the `_meta` envelope
        plus the headers mirroring it. `headers` overrides them; None drops one."""
        message = modern_message(rid, method, params, version)
        h = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if method == "tools/call":
            h["Mcp-Name"] = message["params"]["name"]
        h.update(headers or {})
        return self.send(message, h)

    def test_discover_answers_without_initialize_and_names_both_eras(self):
        status, body = self.modern(1, "server/discover")
        self.assertEqual(status, 200)
        result = body["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "lms-ai-bridge")
        self.assertIsInstance(result["ttlMs"], int)
        self.assertIn(result["cacheScope"], ("public", "private"))

    def test_list_and_call_without_initialize(self):
        status, body = self.modern(2, "tools/list")
        self.assertEqual(status, 200)
        result = body["result"]
        self.assertIn("search_course", [t["name"] for t in result["tools"]])
        self.assertEqual(result["resultType"], "complete")
        self.assertIsInstance(result["ttlMs"], int)
        # A user sees fewer tools than the service token, so a shared cache
        # must never hand one caller's list to another.
        self.assertEqual(result["cacheScope"], "private")

        status, body = self.modern(3, "tools/call", {
            "name": "search_course",
            "arguments": {"course_ref": "ilias:86", "query": "Tokenisierung"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["resultType"], "complete")
        payload = json.loads(body["result"]["content"][0]["text"])
        self.assertEqual(payload["passages"][0]["citation"], "Skript.pdf, S. 12 (Generative KI)")

    def test_unsupported_version_is_400_and_names_what_is_supported(self):
        status, body = self.modern(4, "tools/list", version="2099-01-01")
        self.assertEqual(status, 400)
        self.assertEqual(body["id"], 4)
        self.assertEqual(body["error"]["code"], -32022)
        self.assertEqual(body["error"]["data"]["requested"], "2099-01-01")
        self.assertIn("2026-07-28", body["error"]["data"]["supported"])

    def test_headers_that_do_not_match_the_body_are_400_header_mismatch(self):
        call = {"name": "list_indexed_courses", "arguments": {}}
        cases = [
            ("no MCP-Protocol-Version", "tools/list", None, {"MCP-Protocol-Version": None}),
            ("version header differs", "tools/list", None, {"MCP-Protocol-Version": "2025-11-25"}),
            ("no Mcp-Method", "tools/list", None, {"Mcp-Method": None}),
            ("Mcp-Method differs", "tools/list", None, {"Mcp-Method": "tools/call"}),
            ("no Mcp-Name on tools/call", "tools/call", call, {"Mcp-Name": None}),
            ("Mcp-Name differs", "tools/call", call, {"Mcp-Name": "search_course"}),
        ]
        for rid, (label, method, params, headers) in enumerate(cases, start=10):
            with self.subTest(label):
                status, body = self.modern(rid, method, params, headers=headers)
                self.assertEqual(status, 400)
                self.assertEqual(body["id"], rid)
                self.assertEqual(body["error"]["code"], -32020)

    def test_mcp_name_in_base64_form_is_decoded_before_comparing(self):
        encoded = base64.b64encode(b"list_indexed_courses").decode("ascii")
        status, body = self.modern(20, "tools/call", {"name": "list_indexed_courses", "arguments": {}},
                                   headers={"Mcp-Name": f"=?base64?{encoded}?="})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["resultType"], "complete")

    def test_a_method_the_bridge_lacks_is_404_for_modern_requests(self):
        # ping is gone from 2026-07-28.
        status, body = self.modern(21, "ping")
        self.assertEqual(status, 404)
        self.assertEqual(body["id"], 21)
        self.assertEqual(body["error"]["code"], -32601)

    def test_older_clients_see_no_change(self):
        legacy = {"Mcp-Method": None, "MCP-Protocol-Version": None}
        status, body = self.send({"jsonrpc": "2.0", "id": 30, "method": "initialize",
                                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                             "clientInfo": {"name": "librechat", "version": "0.8.9"}}},
                                 legacy)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("resultType", body["result"])

        status, body = self.send({"jsonrpc": "2.0", "id": 31, "method": "tools/list", "params": {}},
                                 {"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 200)
        self.assertNotIn("resultType", body["result"])
        self.assertNotIn("ttlMs", body["result"])

        status, body = self.send({"jsonrpc": "2.0", "id": 32, "method": "no/such", "params": {}},
                                 {"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 200)
        self.assertEqual(body["error"]["code"], -32601)

    def test_initialize_never_answers_with_a_modern_version(self):
        """`initialize` means the legacy era. Echoing 2026-07-28 back would
        claim a revision in which `initialize` does not exist."""
        status, body = self.send({"jsonrpc": "2.0", "id": 33, "method": "initialize",
                                  "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                                             "clientInfo": {"name": "x", "version": "1"}}}, {})
        self.assertEqual(status, 200)
        self.assertIn(body["result"]["protocolVersion"],
                      ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"))


class TestModernHandler(unittest.TestCase):
    """The same revision without HTTP: the stdio server and the resources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.retrieval = course_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_works_on_stdio_too(self):
        out = io.StringIO()
        mcp_server.serve(io.StringIO(json.dumps(modern_message(1, "server/discover")) + "\n"),
                         out, self.retrieval)
        reply = json.loads(out.getvalue())
        self.assertIn("2026-07-28", reply["result"]["supportedVersions"])

    def test_resources_are_cacheable_and_a_missing_one_is_invalid_params(self):
        with mock.patch.dict(os.environ, {"BRIDGE_MCP_APPS": "1"}):
            listed = mcp_server.handle(modern_message(1, "resources/list"), self.retrieval)
            missing = mcp_server.handle(modern_message(2, "resources/read", {"uri": "ui://nope"}),
                                        self.retrieval)
            legacy_missing = mcp_server.handle(
                {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "ui://nope"}},
                self.retrieval)
        self.assertEqual(listed["result"]["resultType"], "complete")
        self.assertIsInstance(listed["result"]["ttlMs"], int)
        self.assertIn(listed["result"]["cacheScope"], ("public", "private"))
        self.assertEqual(listed["result"]["resources"][0]["uri"], "ui://lms-ai-bridge/course-view.html")
        # 2026-07-28 moved "resource not found" to Invalid Params; older
        # revisions keep their own code.
        self.assertEqual(missing["error"]["code"], -32602)
        self.assertEqual(legacy_missing["error"]["code"], -32002)


if __name__ == "__main__":
    unittest.main()
