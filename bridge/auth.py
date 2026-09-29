"""Who is calling the bridge: the service, or one user with their own token.

Two kinds of caller, told apart by the bearer token alone:

- **The service caller** holds the static `BRIDGE_TOKEN`: HAWKI's one API key
  per MCP server, the demos, an operator's scripts. Full access, exactly as
  before per-user tokens existed. The stdio MCP server is the service caller
  too — it runs as the person who started it.
- **A user caller** presents their own OIDC access token. LibreChat forwards
  the logged-in user's Keycloak token as `Bearer
  {{LIBRECHAT_OPENID_ACCESS_TOKEN}}`; another host that exchanges its user's
  login token would send the same kind of token from its own realm. The bridge asks
  the issuing identity provider whether the token is active and meant for the
  bridge (RFC 7662 token introspection), and learns the username from the
  answer.

Introspection rather than checking the JWT signature locally, because the
standard library cannot verify RS256 and this component depends on nothing
else. It costs one round trip to the identity provider per request, and buys
revocation for free: a user who logs out of Keycloak is refused on their next
call, not when a cached key or token expires.

The token's own `iss` is read *unverified*, only to pick which configured
provider to ask. A token naming an issuer that is not configured is refused
without being sent anywhere — forwarding it would hand one realm's token to
another realm's server.

Knowing who the user is does not give the bridge access to the LMS as that
user. Which courses a user may search is `CoursePolicy`'s question, and for
now it has no per-user answer; see there.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Caller:
    kind: str           # "service" or "user"
    username: str = ""
    issuer: str = ""
    client: str = ""    # `azp`: the application the user signed in to


SERVICE = Caller(kind="service")


class IdentityProviderError(RuntimeError):
    """The identity provider could not answer — unreachable, or it refused the
    bridge's own client credentials. Not the user's fault, so not a 401: a 401
    makes LibreChat refresh the user's token, retry, and then ask them to sign
    in again, which fixes nothing."""


def _unverified_issuer(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return ""
    iss = claims.get("iss") if isinstance(claims, dict) else None
    return iss.rstrip("/") if isinstance(iss, str) else ""


class OidcIssuer:
    """One identity provider whose users' tokens the bridge accepts.

    `client_id`/`client_secret` are the bridge's own confidential client at
    that provider, used only to call introspection. `audience` is what must
    appear in a token's `aud` — in Keycloak, an audience mapper on the calling
    application's client adds it. `allowed_clients` restricts which
    applications' tokens are accepted (`azp`); empty accepts any application
    whose tokens name the bridge as audience.
    """

    def __init__(self, issuer: str, client_id: str, client_secret: str, *,
                 audience: str = "", allowed_clients: tuple[str, ...] = (),
                 username_claim: str = "preferred_username", timeout: float = 10.0):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience = audience or client_id
        self.allowed_clients = tuple(allowed_clients)
        self.username_claim = username_claim
        self.timeout = timeout
        self._introspection_endpoint = ""

    def _fetch(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            e.close()
            raise IdentityProviderError(
                f"{self.issuer} answered {e.code} to {req.full_url}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise IdentityProviderError(f"{self.issuer} unreachable: {e}") from e
        if not isinstance(body, dict):
            raise IdentityProviderError(f"{self.issuer} sent a non-object from {req.full_url}")
        return body

    def _endpoint(self) -> str:
        # From discovery, once, rather than configured: the path differs between
        # providers (and between Keycloak versions), and discovery is the one
        # place that states it.
        if not self._introspection_endpoint:
            doc = self._fetch(urllib.request.Request(
                f"{self.issuer}/.well-known/openid-configuration"))
            endpoint = doc.get("introspection_endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise IdentityProviderError(f"{self.issuer} advertises no introspection endpoint")
            self._introspection_endpoint = endpoint
        return self._introspection_endpoint

    def verify(self, token: str) -> Caller | None:
        """The user this token belongs to, or None if the bridge must refuse it."""
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        claims = self._fetch(urllib.request.Request(
            self._endpoint(),
            data=urllib.parse.urlencode(
                {"token": token, "token_type_hint": "access_token"}).encode("ascii"),
            headers={"Authorization": f"Basic {credentials}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
        ))

        if claims.get("active") is not True:
            return None
        if str(claims.get("iss") or "").rstrip("/") != self.issuer:
            return None
        # Keycloak introspects ID and refresh tokens too, and reports them as
        # active. Only an access token is a credential for calling the bridge.
        for key in ("typ", "token_type"):
            if key in claims and str(claims[key]).lower() != "bearer":
                return None
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else list(aud or [])
        if self.audience not in audiences:
            return None
        client = str(claims.get("azp") or "")
        if self.allowed_clients and client not in self.allowed_clients:
            return None
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp <= time.time():
            return None
        username = claims.get(self.username_claim)
        if not isinstance(username, str) or not username.strip():
            return None
        return Caller(kind="user", username=username.strip(), issuer=self.issuer, client=client)


def authenticate(header: str, static_token: str, issuers: list[OidcIssuer]) -> Caller | None:
    """The caller behind an `Authorization` header, or None to refuse.

    With neither a static token nor an issuer configured there is no
    authentication at all — the prototype default, unchanged. Configuring only
    an issuer does *not* mean open: leaving the header off would otherwise be
    full access.
    """
    if not static_token and not issuers:
        return SERVICE
    scheme, _, token = (header or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    if static_token and hmac.compare_digest(token.encode("utf-8"), static_token.encode("utf-8")):
        return SERVICE
    iss = _unverified_issuer(token)
    for issuer in issuers:
        if iss and iss == issuer.issuer:
            return issuer.verify(token)
    return None


def issuers_from_env(env=os.environ) -> list[OidcIssuer]:
    """The identity providers whose users' tokens are accepted.

    One for now. A second host whose users sign in to another realm would be a
    second entry here, not a second mechanism.
    """
    issuer = env.get("BRIDGE_OIDC_ISSUER", "").strip()
    if not issuer:
        return []
    client_id = env.get("BRIDGE_OIDC_CLIENT_ID", "").strip()
    client_secret = env.get("BRIDGE_OIDC_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("BRIDGE_OIDC_ISSUER is set, so BRIDGE_OIDC_CLIENT_ID and "
                         "BRIDGE_OIDC_CLIENT_SECRET are needed too (the bridge's own "
                         "client, for token introspection)")
    allowed = tuple(c.strip() for c in env.get("BRIDGE_OIDC_ALLOWED_CLIENTS", "").split(",")
                    if c.strip())
    return [OidcIssuer(
        issuer, client_id, client_secret,
        audience=env.get("BRIDGE_OIDC_AUDIENCE", "").strip(),
        allowed_clients=allowed,
        username_claim=env.get("BRIDGE_OIDC_USERNAME_CLAIM", "").strip() or "preferred_username",
    )]


class CoursePolicy:
    """Which indexed courses a user caller may search.

    `none` (the default) hides every course: an authenticated user sees an
    empty index. `all` shows every indexed course to every authenticated user —
    for testing the login path end to end, never for real course material,
    which is licensed to a course's members and nobody else.

    The real answer is per user: "is this user a member of this course", asked
    of the LMS through the bridge's read-only service account. Which Stud.IP
    route can answer that for a non-root account is not known yet (2026-09-29),
    so that mode does not exist yet, and nothing here pretends it does.
    """

    MODES = ("none", "all")

    def __init__(self, mode: str = "none"):
        mode = (mode or "none").strip().lower()
        if mode not in self.MODES:
            raise ValueError(f"BRIDGE_USER_COURSES={mode!r}: one of {', '.join(self.MODES)}")
        self.mode = mode

    def __call__(self, user: Caller, course_ref: str) -> bool:
        return self.mode == "all"
