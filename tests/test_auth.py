"""Who is calling: the static bridge token, or a user's own OIDC access token.

The static token is how HAWKI and the demos authenticate today and must keep
full access. A user's token — forwarded by LibreChat as
`Bearer {{LIBRECHAT_OPENID_ACCESS_TOKEN}}` — is checked by asking the identity
provider (token introspection), against a stand-in for Keycloak running on a
real port.
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge.auth import (  # noqa: E402
    SERVICE, IdentityProviderError, OidcIssuer, authenticate, issuers_from_env,
)
from fake_idp import BRIDGE_CLIENT, BRIDGE_SECRET, CHAT_CLIENT, FakeIdp, jwt_for  # noqa: E402


class TestUserinfoMode(unittest.TestCase):
    """No bridge client at the identity provider: ask its userinfo endpoint.

    Userinfo accepts a token only if the provider issued it and it is still
    live, so after a 200 the token's own claims can be believed. What this mode
    cannot check is that the token names the bridge as its audience — it accepts
    any genuine token from an allowed application, which is why introspection
    stays the recommended mode.
    """

    @classmethod
    def setUpClass(cls):
        cls.idp = FakeIdp()
        cls.issuer = OidcIssuer(cls.idp.issuer, allowed_clients=(CHAT_CLIENT,))

    @classmethod
    def tearDownClass(cls):
        cls.idp.stop()

    def check(self, token):
        return authenticate(f"Bearer {token}", "", [self.issuer])

    def test_a_genuine_token_from_an_allowed_client_is_accepted_without_the_bridge_audience(self):
        before = len(self.idp.introspected)
        token = self.idp.access_token(aud=["account"], preferred_username="rgarita")
        caller = self.check(token)
        self.assertEqual((caller.kind, caller.username, caller.client), ("user", "rgarita", CHAT_CLIENT))
        self.assertIn(token, self.idp.userinfo_asked)
        self.assertEqual(len(self.idp.introspected), before)

    def test_a_forged_token_with_perfect_claims_is_refused(self):
        """The payload alone proves nothing; only the provider's answer does."""
        forged = jwt_for(self.idp.issuer, typ="Bearer", azp=CHAT_CLIENT, preferred_username="mallory")
        self.assertIsNone(self.check(forged))

    def test_tokens_refused_in_userinfo_mode(self):
        now = int(time.time())
        cases = {
            "inactive (expired, revoked, logged out)": self.idp.inactive_token(),
            "from a client not on the allow-list": self.idp.access_token(azp="some-other-app"),
            "an ID token, not an access token": self.idp.access_token(typ="ID"),
            "no username claim": self.idp.access_token(drop=("preferred_username",)),
            "past exp": self.idp.access_token(exp=now - 5),
            "no openid scope (userinfo answers 403)": self.idp.access_token(scope="profile email"),
        }
        for label, token in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.check(token))

    def test_an_unreachable_idp_is_an_error_not_a_refusal(self):
        down = OidcIssuer("http://127.0.0.1:9/realms/uni", allowed_clients=(CHAT_CLIENT,))
        with self.assertRaises(IdentityProviderError):
            authenticate(f"Bearer {jwt_for(down.issuer)}", "", [down])


class TestUserTokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idp = FakeIdp()
        cls.issuer = OidcIssuer(cls.idp.issuer, BRIDGE_CLIENT, BRIDGE_SECRET,
                                allowed_clients=(CHAT_CLIENT,))

    @classmethod
    def tearDownClass(cls):
        cls.idp.stop()

    def check(self, token):
        return authenticate(f"Bearer {token}", "", [self.issuer])

    def test_an_active_token_for_the_bridge_identifies_the_user(self):
        caller = self.check(self.idp.access_token(preferred_username="rgarita"))
        self.assertEqual(caller.kind, "user")
        self.assertEqual(caller.username, "rgarita")
        self.assertEqual(caller.issuer, self.idp.issuer)
        self.assertEqual(caller.client, "chat-app")

    def test_an_audience_given_as_one_string_is_accepted(self):
        self.assertIsNotNone(self.check(self.idp.access_token(aud="lms-ai-bridge")))

    def test_tokens_the_bridge_must_not_accept(self):
        now = int(time.time())
        cases = {
            "inactive (expired, revoked, logged out)": self.idp.inactive_token(),
            "issued for other audiences only": self.idp.access_token(aud=["account"]),
            "other audience as one string": self.idp.access_token(aud="account"),
            "no audience at all": self.idp.access_token(drop=("aud",)),
            "an ID token, not an access token": self.idp.access_token(typ="ID"),
            "a refresh token": self.idp.access_token(typ="Refresh"),
            "from a client not on the allow-list": self.idp.access_token(azp="some-other-app"),
            "no username claim": self.idp.access_token(drop=("preferred_username",)),
            "active but already past exp": self.idp.access_token(exp=now - 5),
            "introspection names another issuer": self.idp.access_token(
                iss="https://elsewhere.example/realms/x"),
        }
        for label, token in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.check(token))

    def test_a_token_from_an_unconfigured_issuer_is_never_sent_to_the_idp(self):
        """Forwarding it would hand one realm's token to another realm's server."""
        before = len(self.idp.introspected)
        self.assertIsNone(self.check(jwt_for("https://login.example.org/realms/other")))
        self.assertEqual(len(self.idp.introspected), before)

    def test_a_bearer_that_is_not_a_jwt_is_refused_without_asking(self):
        before = len(self.idp.introspected)
        for token in ("not-a-jwt", "a.b.c", ""):
            with self.subTest(token=token):
                self.assertIsNone(self.check(token))
        self.assertEqual(len(self.idp.introspected), before)

    def test_an_unreachable_idp_is_an_error_not_a_refusal(self):
        """A 401 would send LibreChat into refresh-and-retry and then tell the
        user to sign in again, for a fault that is neither theirs nor fixable
        by signing in."""
        down = OidcIssuer("http://127.0.0.1:9/realms/uni", BRIDGE_CLIENT, BRIDGE_SECRET)
        with self.assertRaises(IdentityProviderError):
            authenticate(f"Bearer {jwt_for(down.issuer)}", "", [down])

    def test_a_wrong_bridge_client_secret_is_an_error_not_a_refusal(self):
        misconfigured = OidcIssuer(self.idp.issuer, BRIDGE_CLIENT, "wrong-secret")
        with self.assertRaises(IdentityProviderError):
            authenticate(f"Bearer {self.idp.access_token()}", "", [misconfigured])


class TestStaticToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idp = FakeIdp()
        cls.issuer = OidcIssuer(cls.idp.issuer, BRIDGE_CLIENT, BRIDGE_SECRET)

    @classmethod
    def tearDownClass(cls):
        cls.idp.stop()

    def test_the_static_token_is_the_service_caller_and_asks_no_one(self):
        before = len(self.idp.introspected)
        self.assertIs(authenticate("Bearer s3cret", "s3cret", [self.issuer]), SERVICE)
        self.assertEqual(len(self.idp.introspected), before)

    def test_a_wrong_static_token_is_refused(self):
        for header in ("Bearer nope", "s3cret", "Basic czNjcmV0", ""):
            with self.subTest(header=header):
                self.assertIsNone(authenticate(header, "s3cret", []))

    def test_with_nothing_configured_everyone_is_the_service_caller(self):
        """The prototype default, unchanged: no BRIDGE_TOKEN, no issuer, no auth."""
        self.assertIs(authenticate("", "", []), SERVICE)

    def test_configuring_only_an_issuer_still_requires_a_token(self):
        """No BRIDGE_TOKEN must not mean "open" once user tokens are expected —
        otherwise leaving the header off would be full access."""
        self.assertIsNone(authenticate("", "", [self.issuer]))


class TestIssuerFromEnvironment(unittest.TestCase):
    def test_no_issuer_configured_means_no_user_tokens(self):
        self.assertEqual(issuers_from_env({}), [])

    def test_an_issuer_is_read_with_its_client_and_allowed_clients(self):
        [issuer] = issuers_from_env({
            "BRIDGE_OIDC_ISSUER": "https://idp.example.org/realms/uni/",
            "BRIDGE_OIDC_CLIENT_ID": "lms-ai-bridge",
            "BRIDGE_OIDC_CLIENT_SECRET": "x",
            "BRIDGE_OIDC_ALLOWED_CLIENTS": "chat-app, chat-app-staging",
        })
        # Introspection reports `iss` without a trailing slash; a configured one
        # must not make every token look foreign.
        self.assertEqual(issuer.issuer, "https://idp.example.org/realms/uni")
        self.assertEqual(issuer.audience, "lms-ai-bridge")
        self.assertEqual(issuer.allowed_clients, ("chat-app", "chat-app-staging"))
        self.assertEqual(issuer.mode, "introspection")

    def test_an_issuer_without_a_client_uses_userinfo(self):
        [issuer] = issuers_from_env({"BRIDGE_OIDC_ISSUER": "https://idp.example.org/realms/uni",
                                     "BRIDGE_OIDC_ALLOWED_CLIENTS": "chat-app"})
        self.assertEqual(issuer.mode, "userinfo")

    def test_userinfo_mode_without_allowed_clients_is_refused_at_startup(self):
        """With no audience check, the allow-list is the only thing standing
        between the bridge and every application's tokens in the realm."""
        with self.assertRaises(ValueError):
            issuers_from_env({"BRIDGE_OIDC_ISSUER": "https://idp.example.org/realms/uni"})

    def test_half_a_bridge_client_is_refused_at_startup(self):
        for missing in ("BRIDGE_OIDC_CLIENT_ID", "BRIDGE_OIDC_CLIENT_SECRET"):
            env = {"BRIDGE_OIDC_ISSUER": "https://idp.example/realms/r",
                   "BRIDGE_OIDC_CLIENT_ID": "lms-ai-bridge", "BRIDGE_OIDC_CLIENT_SECRET": "x"}
            del env[missing]
            with self.subTest(missing), self.assertRaises(ValueError):
                issuers_from_env(env)


if __name__ == "__main__":
    unittest.main()
