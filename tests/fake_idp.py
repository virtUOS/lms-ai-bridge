"""A stand-in for Keycloak: OIDC discovery and token introspection, nothing else.

Runs a real HTTP server on an ephemeral port so the bridge's own urllib code is
what gets tested. Introspection answers mirror Keycloak's shape (checked against a
university realm, 2026-09-29): the full claim set for an active token, and only
`{"active": false}` for anything else — Keycloak says nothing more about a token
it will not vouch for.

Tokens are JWT-shaped but unsigned. The bridge reads the unverified `iss` only
to decide which identity provider to ask; the answer comes from introspection.
"""

import base64
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_CLIENT = "lms-ai-bridge"
BRIDGE_SECRET = "bridge-secret-for-tests"
CHAT_CLIENT = "chat-app"


def _b64(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwt_for(issuer: str, **claims) -> str:
    """A JWT-shaped string whose payload names `issuer`. Not signed."""
    payload = {"iss": issuer, "sub": "f3b1c6e2", "exp": int(time.time()) + 300, **claims}
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT', 'kid': 'test'})}.{_b64(payload)}.c2ln"


class FakeIdp:
    def __init__(self, realm: str = "uni"):
        self.tokens: dict[str, dict] = {}
        self.introspected: list[str] = []
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path == f"/realms/{realm}/.well-known/openid-configuration":
                    return self._json(200, {
                        "issuer": idp.issuer,
                        "introspection_endpoint":
                            f"{idp.issuer}/protocol/openid-connect/token/introspect",
                    })
                return self._json(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if self.path != f"/realms/{realm}/protocol/openid-connect/token/introspect":
                    return self._json(404, {"error": "not found"})
                expected = "Basic " + base64.b64encode(
                    f"{BRIDGE_CLIENT}:{BRIDGE_SECRET}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    return self._json(401, {"error": "invalid_client",
                                            "error_description": "Invalid client credentials"})
                length = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                token = (form.get("token") or [""])[0]
                idp.introspected.append(token)
                return self._json(200, idp.tokens.get(token, {"active": False}))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.httpd.server_address[1]}/realms/{realm}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def access_token(self, drop: tuple = (), **overrides) -> str:
        """Register a token and what introspection says about it.

        Defaults describe a healthy chat application's access token with the bridge added to
        its audience; `overrides` replace claims, `drop` removes them.
        """
        now = int(time.time())
        username = overrides.get("preferred_username", "student1")
        claims = {
            "exp": now + 300, "iat": now, "auth_time": now - 60,
            "jti": f"onrtac:{len(self.tokens)}", "iss": self.issuer,
            "aud": [BRIDGE_CLIENT, "account"], "sub": "f3b1c6e2-0000-4000-8000-000000000001",
            "typ": "Bearer", "azp": CHAT_CLIENT, "sid": "5a2c0d1e", "acr": "1",
            "scope": "openid profile email", "email_verified": True,
            "name": "Student One", "preferred_username": username,
            "given_name": "Student", "family_name": "One",
            "email": f"{username}@uni.example.org",
            "client_id": CHAT_CLIENT, "username": username,
            "token_type": "Bearer", "active": True,
        }
        claims.update(overrides)
        for key in drop:
            claims.pop(key, None)
        token = jwt_for(self.issuer, jti=claims["jti"])
        self.tokens[token] = claims
        return token

    def inactive_token(self) -> str:
        """A token Keycloak no longer vouches for — expired, revoked, logged out."""
        token = jwt_for(self.issuer, jti=f"gone:{len(self.tokens)}")
        self.tokens[token] = {"active": False}
        return token

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
