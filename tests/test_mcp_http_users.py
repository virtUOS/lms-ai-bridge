"""Per-user tokens over HTTP, beside the static bridge token.

The shape LibreChat v0.8.8 speaks: `Authorization: Bearer <the user's Keycloak
access token>` on every request to `/mcp`, with one forced token refresh and
retry when the server answers 401. The static token keeps working for HAWKI.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import server as bridge_server  # noqa: E402
from bridge.auth import CoursePolicy, OidcIssuer  # noqa: E402
from bridge.contract import IndexRequest  # noqa: E402
from bridge.providers.builtin_retrieval import BuiltinRetrieval  # noqa: E402
from bridge.providers.openai_chat import EchoChat  # noqa: E402
from fake_idp import BRIDGE_CLIENT, BRIDGE_SECRET, FakeIdp, jwt_for  # noqa: E402


class TestUserTokensOverHttp(unittest.TestCase):
    static = "static-token-for-tests"

    @classmethod
    def setUpClass(cls):
        cls.idp = FakeIdp()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.retrieval = BuiltinRetrieval(Path(cls.tmp.name) / "index.json")
        cls.retrieval.index(IndexRequest.from_dict({
            "course_ref": "studip:0a1b2c",
            "documents": [{"activity_ref": "studip:0a1b2c:file:1:folien.pdf", "title": "Folien.pdf",
                           "locator": "Folie 3", "course_name": "Einführung",
                           "text": "Tokenisierung zerlegt Text in Einheiten."}],
        }))
        H = bridge_server.Handler
        H.retrieval_provider = cls.retrieval
        H.chat_provider = EchoChat()
        H.transcription_provider = None
        H.auth_token = cls.static
        H.identity_issuers = [OidcIssuer(cls.idp.issuer, BRIDGE_CLIENT, BRIDGE_SECRET)]
        H.course_policy = CoursePolicy("all")
        H.mcp_keepalive_seconds = 0.05
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        H = bridge_server.Handler
        H.auth_token = ""
        H.identity_issuers = []
        H.course_policy = CoursePolicy("none")
        H.mcp_keepalive_seconds = 15.0
        cls.idp.stop()
        cls.tmp.cleanup()

    def request(self, path, token=None, body=None, method="POST"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)

    def refused(self, path, token=None, body=None, method="POST"):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request(path, token, body, method)
        ctx.exception.close()
        return ctx.exception

    def tools(self, token):
        _, reply = self.request("/mcp", token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return [t["name"] for t in reply["result"]["tools"]]

    def test_a_user_token_reaches_the_read_tools(self):
        token = self.idp.access_token()
        self.assertEqual(self.tools(token), ["list_indexed_courses", "search_course"])
        _, reply = self.request("/mcp", token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "search_course",
                       "arguments": {"course_ref": "studip:0a1b2c", "query": "Tokenisierung"}}})
        body = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(body["passages"][0]["citation"], "Folien.pdf, Folie 3 (Einführung)")

    def test_the_static_token_keeps_every_tool(self):
        """HAWKI 2.5.2 sends one static key per MCP server; nothing changes for it."""
        self.assertEqual(self.tools(self.static),
                         ["list_indexed_courses", "index_course", "search_course", "forget_course"])

    def test_a_user_token_is_refused_on_the_rest_endpoints(self):
        err = self.refused("/v1/forget", self.idp.access_token(), {"course_ref": "studip:0a1b2c"})
        self.assertEqual(err.code, 403)
        self.assertGreater(self.retrieval.count("studip:0a1b2c"), 0)

    def test_a_rejected_token_gets_401_with_a_bearer_challenge(self):
        """LibreChat refreshes the user's token and retries only on a 401."""
        for label, token in (("inactive", self.idp.inactive_token()),
                             ("unknown issuer", jwt_for("https://login.example.org/realms/x")),
                             ("none", None)):
            with self.subTest(label):
                err = self.refused("/mcp", token, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
                self.assertEqual(err.code, 401)
                self.assertTrue(err.headers.get("WWW-Authenticate", "").startswith("Bearer"))

    def test_the_get_stream_and_delete_accept_a_user_token(self):
        """LibreChat's MCP SDK opens the GET stream once per user connection."""
        token = self.idp.access_token()
        req = urllib.request.Request(self.base + "/mcp", method="GET",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.readline(), b": keepalive\n")
        status, _ = self.request("/mcp", token, method="DELETE")
        self.assertEqual(status, 200)

    def test_an_unreachable_idp_is_503_not_401(self):
        H = bridge_server.Handler
        saved = H.identity_issuers
        H.identity_issuers = [OidcIssuer("http://127.0.0.1:9/realms/uni",
                                         BRIDGE_CLIENT, BRIDGE_SECRET)]
        try:
            err = self.refused("/mcp", jwt_for("http://127.0.0.1:9/realms/uni"),
                               {"jsonrpc": "2.0", "id": 4, "method": "ping"})
            self.assertEqual(err.code, 503)
        finally:
            H.identity_issuers = saved


if __name__ == "__main__":
    unittest.main()
