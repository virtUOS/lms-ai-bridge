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
  the issuing identity provider whether the token is genuine, and learns the
  username. With a client of its own there, it asks by token introspection
  (RFC 7662) and also checks the token was meant for the bridge; without one,
  it falls back to the userinfo endpoint, which cannot show that — see
  `OidcIssuer` for the two modes and why introspection is the one to aim for.

Asking the provider rather than checking the JWT signature locally, because
the standard library cannot verify RS256 and this component depends on nothing
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


def _unverified_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _unverified_issuer(token: str) -> str:
    iss = _unverified_claims(token).get("iss")
    return iss.rstrip("/") if isinstance(iss, str) else ""


class OidcIssuer:
    """One identity provider whose users' tokens the bridge accepts.

    Two modes, chosen by whether the bridge has its own client there:

    - **introspection** (recommended): `client_id`/`client_secret` are the
      bridge's own confidential client, used only to call introspection. The
      token must name `audience` in its `aud` — in Keycloak, an audience mapper
      on the calling application's client adds it. That is the check the MCP
      authorisation spec asks for: a token issued *for this server*.
    - **userinfo** (no client): the provider's userinfo endpoint accepts the
      token only if the provider issued it and it is still live, so after a 200
      the token's own claims can be believed. It cannot show that the token was
      meant for the bridge, so it accepts any genuine token from the
      applications in `allowed_clients` — which is therefore required. **Not
      what the MCP spec asks for**; a stopgap for when the provider's
      administrators have not created a client for the bridge yet.

    `allowed_clients` restricts which applications' tokens are accepted
    (`azp`). In introspection mode, empty accepts any application whose tokens
    name the bridge as audience.
    """

    def __init__(self, issuer: str, client_id: str = "", client_secret: str = "", *,
                 audience: str = "", allowed_clients: tuple[str, ...] = (),
                 username_claim: str = "preferred_username", timeout: float = 10.0):
        if bool(client_id) != bool(client_secret):
            raise ValueError("the bridge's client needs both an id and a secret "
                             "(or neither, for userinfo mode)")
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.mode = "introspection" if client_id else "userinfo"
        self.audience = audience or client_id
        self.allowed_clients = tuple(allowed_clients)
        if self.mode == "userinfo" and not self.allowed_clients:
            raise ValueError("userinfo mode checks no audience, so it needs the allowed "
                             "clients (BRIDGE_OIDC_ALLOWED_CLIENTS); otherwise every "
                             "application's tokens in the realm would be accepted")
        self.username_claim = username_claim
        self.timeout = timeout
        self._endpoints: dict[str, str] = {}

    def _fetch(self, req: urllib.request.Request, refusals: tuple[int, ...] = ()) -> dict | None:
        """The provider's JSON answer, or None if it answered with one of
        `refusals` — its way of saying "not a token I vouch for"."""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            e.close()
            if e.code in refusals:
                return None
            raise IdentityProviderError(
                f"{self.issuer} answered {e.code} to {req.full_url}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise IdentityProviderError(f"{self.issuer} unreachable: {e}") from e
        if not isinstance(body, dict):
            raise IdentityProviderError(f"{self.issuer} sent a non-object from {req.full_url}")
        return body

    def _endpoint(self, name: str) -> str:
        # From discovery, once, rather than configured: the path differs between
        # providers (and between Keycloak versions), and discovery is the one
        # place that states it.
        if name not in self._endpoints:
            doc = self._fetch(urllib.request.Request(
                f"{self.issuer}/.well-known/openid-configuration"))
            endpoint = doc.get(name)
            if not isinstance(endpoint, str) or not endpoint:
                raise IdentityProviderError(f"{self.issuer} advertises no {name}")
            self._endpoints[name] = endpoint
        return self._endpoints[name]

    def verify(self, token: str) -> Caller | None:
        """The user this token belongs to, or None if the bridge must refuse it."""
        if self.mode == "introspection":
            credentials = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
            claims = self._fetch(urllib.request.Request(
                self._endpoint("introspection_endpoint"),
                data=urllib.parse.urlencode(
                    {"token": token, "token_type_hint": "access_token"}).encode("ascii"),
                headers={"Authorization": f"Basic {credentials}",
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json"},
            ))
            if claims.get("active") is not True:
                return None
        else:
            # 401: expired, revoked or not an access token. 403: an access token
            # without the `openid` scope. Either way, not one to accept.
            answer = self._fetch(urllib.request.Request(
                self._endpoint("userinfo_endpoint"),
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ), refusals=(401, 403))
            if answer is None:
                return None
            # The provider has just accepted this exact token, so what it says
            # about itself is now worth reading.
            claims = _unverified_claims(token)
        return self._accept(claims)

    def _accept(self, claims: dict) -> Caller | None:
        if str(claims.get("iss") or "").rstrip("/") != self.issuer:
            return None
        # Keycloak introspects ID and refresh tokens too, and reports them as
        # active. Only an access token is a credential for calling the bridge.
        for key in ("typ", "token_type"):
            if key in claims and str(claims[key]).lower() != "bearer":
                return None
        if self.mode == "introspection":
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
