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
        bridge_server.Handler.mcp_keepalive_seconds = 0.05
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), bridge_server.Handler)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/mcp"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        bridge_server.Handler.auth_token = ""
        bridge_server.Handler.mcp_keepalive_seconds = 15.0
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

    def test_get_opens_a_live_stream_that_stays_open(self):
        """HAWKI 2.5.2's MCP client (logiscape PHP SDK) opens a standalone GET
        stream in a *forked copy of the PHP-FPM worker*. If the server declines
        with 405, that child exits normally and, being a fork of the worker,
        finishes the FastCGI request it inherited — the browser's answer stream
        ends right after the tool call with no error anywhere (observed
        2026-09-22, three models). So the GET must be a live SSE stream that
        stays open until the client goes away; the child then blocks in curl
        until HAWKI kills it at the end of the turn."""
        req = urllib.request.Request(self.url, method="GET",
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers.get("Content-Type", "").startswith("text/event-stream"))
            self.assertEqual(r.readline(), b": keepalive\n")
            # A second keepalive proves the stream is held open, not closed
            # after one comment.
            self.assertEqual(r.readline(), b"\n")
            self.assertEqual(r.readline(), b": keepalive\n")

    def test_get_stream_requires_the_token_and_delete_is_acknowledged(self):
        """DELETE is what the SDK sends on close; it only logs the reply, but
        the reply must not be a 404 or 501. A GET without the token is a 401,
        like every other route."""
        req = urllib.request.Request(self.url, method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        req = urllib.request.Request(self.url, method="DELETE",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_rest_endpoints_still_work_beside_mcp(self):
        req = urllib.request.Request(self.url.replace("/mcp", "/v1/index/status?course_ref=ilias:86"),
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertTrue(json.loads(r.read())["indexed"])


if __name__ == "__main__":
    unittest.main()
