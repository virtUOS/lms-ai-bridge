"""The MCP server over HTTP: `POST /mcp` on the bridge's own server.

Runs the real HTTP server on an ephemeral port with the built-in retrieval
provider on a temp store, and speaks to it the way HAWKI's MCP client does —
one JSON-RPC message per POST, plain JSON back, a bearer token in the header.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import server as bridge_server  # noqa: E402
from bridge.contract import IndexRequest  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402
from bridge.providers.openai_chat import EchoChat  # noqa: E402


class TestMcpOverHttp(unittest.TestCase):
    token = "secret-for-tests"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        retrieval = BuiltinRetrieval(Path(cls.tmp.name) / "index.json")
        retrieval.index(IndexRequest.from_dict({
            "course_ref": "ilias:86",
            "documents": [{"activity_ref": "ilias:86:file:87:skript.pdf", "title": "Skript.pdf",
                           "locator": "S. 12", "course_name": "Generative KI",
                           "text": "Tokenisierung zerlegt Text in Einheiten, die das Modell verarbeitet."}],
        }))
        bridge_server.Handler.retrieval_provider = retrieval
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

    def post(self, message, token=None):
        """Exactly what HAWKI's MCPSSEClient does: POST JSON, Bearer, read JSON."""
        headers = {"Content-Type": "application/json", "accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url, data=json.dumps(message).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)

    def test_initialize_then_list_then_call_like_hawki_does(self):
        status, init = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "hawki", "version": "2"}}},
                                 token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(init["result"]["serverInfo"]["name"], "lms-ai-bridge")

        _, tools = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                             token=self.token)
        self.assertIn("search_course", [t["name"] for t in tools["result"]["tools"]])

        _, reply = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "search_course",
                                         "arguments": {"course_ref": "ilias:86", "query": "Tokenisierung"}}},
                             token=self.token)
        body = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(body["passages"][0]["citation"], "Skript.pdf, S. 12 (Generative KI)")
        self.assertIn("Tokenisierung", body["passages"][0]["text"])

    def test_missing_or_wrong_bearer_is_401(self):
        for token in (None, "wrong"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post({"jsonrpc": "2.0", "id": 4, "method": "ping"}, token=token)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

    def test_notification_gets_202_and_no_body(self):
        status, body = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                 token=self.token)
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_get_stream_is_declined_with_405_and_delete_is_acknowledged(self):
        """The MCP PHP SDK (HAWKI 2.5.2) opens a GET stream after initialize
        and sends DELETE on close; the spec lets a server decline the first
        and the SDK only logs the second. Neither may be a 404 or a 501."""
        for method, expected in (("GET", 405), ("DELETE", 200)):
            req = urllib.request.Request(self.url, method=method,
                                         headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    self.assertEqual(r.status, expected)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, expected)
                e.close()

    def test_rest_endpoints_still_work_beside_mcp(self):
        req = urllib.request.Request(self.url.replace("/mcp", "/v1/index/status?course_ref=ilias:86"),
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertTrue(json.loads(r.read())["indexed"])


if __name__ == "__main__":
    unittest.main()
