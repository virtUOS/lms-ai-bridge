"""What a per-user caller may do through the MCP tools.

A user authenticated by their own token (e.g. forwarded by LibreChat) gets the read
tools only, and only for the courses a course policy lets them see. The service
caller — stdio, or the static bridge token — keeps everything, unchanged.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge import mcp_server  # noqa: E402
from bridge.auth import Caller, CoursePolicy, parse_allowlist, policy_from_env  # noqa: E402
from test_mcp_server import FakeRetrieval  # noqa: E402

USER = Caller(kind="user", username="student1",
              issuer="https://idp.example.org/realms/uni",
              client="chat-app")


def rpc(provider, method, params=None, **kw):
    return mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params or {}}, provider, **kw)


def tool(provider, name, args, **kw):
    result = rpc(provider, "tools/call", {"name": name, "arguments": args}, **kw)["result"]
    return result.get("isError", False), json.loads(result["content"][0]["text"])


class TestUserAccess(unittest.TestCase):
    def test_a_user_is_offered_only_the_read_tools(self):
        reply = rpc(FakeRetrieval(), "tools/list", user=USER, may_see=CoursePolicy("all"))
        self.assertEqual([t["name"] for t in reply["result"]["tools"]],
                         ["list_indexed_courses", "search_course"])

    def test_the_service_caller_keeps_all_four_tools(self):
        reply = rpc(FakeRetrieval(), "tools/list")
        self.assertEqual([t["name"] for t in reply["result"]["tools"]],
                         ["list_indexed_courses", "index_course", "search_course", "forget_course"])

    def test_by_default_a_user_sees_no_courses(self):
        _, body = tool(FakeRetrieval(), "list_indexed_courses", {},
                       user=USER, may_see=CoursePolicy("none"))
        self.assertEqual(body["courses"], [])

    def test_with_no_policy_given_a_user_sees_no_courses(self):
        """Fail closed: forgetting to pass a policy must not open the index."""
        _, body = tool(FakeRetrieval(), "list_indexed_courses", {}, user=USER)
        self.assertEqual(body["courses"], [])
        is_error, _ = tool(FakeRetrieval(), "search_course",
                           {"course_ref": "ilias:86", "query": "Monade"}, user=USER)
        self.assertTrue(is_error)

    def test_a_user_sees_course_names_in_the_listing(self):
        _, body = tool(FakeRetrieval(), "list_indexed_courses", {},
                       user=USER, may_see=CoursePolicy("all"))
        self.assertEqual([c["course_name"] for c in body["courses"]],
                         ["LernKI Testkurs", "Generative KI"])

    def test_the_listing_and_search_follow_the_policy_per_course(self):
        only_moodle = lambda user, course_ref: course_ref == "moodle:5"  # noqa: E731
        provider = FakeRetrieval()
        _, listing = tool(provider, "list_indexed_courses", {}, user=USER, may_see=only_moodle)
        self.assertEqual([c["course_ref"] for c in listing["courses"]], ["moodle:5"])

        is_error, body = tool(provider, "search_course", {"course_ref": "moodle:5", "query": "Monade"},
                              user=USER, may_see=only_moodle)
        self.assertFalse(is_error)
        self.assertEqual(body["passages"][0]["citation"], "Skript.pdf, S. 12 (Generative KI / Skripte)")

    def test_a_hidden_course_looks_exactly_like_one_never_indexed(self):
        """Otherwise the error message tells a user which courses exist."""
        provider, policy = FakeRetrieval(), CoursePolicy("none")
        hidden = tool(provider, "search_course", {"course_ref": "ilias:86", "query": "Monade"},
                      user=USER, may_see=policy)
        absent = tool(provider, "search_course", {"course_ref": "ilias:999", "query": "Monade"},
                      user=USER, may_see=policy)
        self.assertTrue(hidden[0])
        self.assertEqual(hidden[1]["error"].replace("ilias:86", "X"),
                         absent[1]["error"].replace("ilias:999", "X"))

    def test_a_user_cannot_forget_or_index_a_course(self):
        provider = FakeRetrieval()
        for name, args in (("forget_course", {"course_ref": "ilias:86"}),
                           ("index_course", {"platform": "ilias", "course_id": "86"})):
            with self.subTest(name):
                is_error, _ = tool(provider, name, args, user=USER, may_see=CoursePolicy("all"))
                self.assertTrue(is_error)
        self.assertEqual(provider.forgotten, [])
        self.assertEqual(provider.indexed, [])
        self.assertEqual(provider.count("ilias:86"), 37)

    def test_an_unknown_course_policy_is_refused(self):
        with self.assertRaises(ValueError):
            CoursePolicy("members")


class TestAllowlistPolicy(unittest.TestCase):
    """`allowlist`: the operator names, per user, the courses that user may search.

    A stand-in for the membership check against the LMS, which does not exist
    yet: explicit, fail-closed for everyone unlisted, and nothing more.
    """

    def test_an_allowlisted_user_sees_only_their_courses(self):
        policy = CoursePolicy("allowlist", allowlist={"student1": {"ilias:86"}})
        provider = FakeRetrieval()
        _, listing = tool(provider, "list_indexed_courses", {}, user=USER, may_see=policy)
        self.assertEqual([c["course_ref"] for c in listing["courses"]], ["ilias:86"])
        is_error, _ = tool(provider, "search_course",
                           {"course_ref": "moodle:5", "query": "Monade"}, user=USER, may_see=policy)
        self.assertTrue(is_error)

    def test_a_user_not_on_the_list_sees_nothing(self):
        policy = CoursePolicy("allowlist", allowlist={"student1": {"ilias:86"}})
        stranger = Caller(kind="user", username="student2", issuer=USER.issuer, client=USER.client)
        _, listing = tool(FakeRetrieval(), "list_indexed_courses", {}, user=stranger, may_see=policy)
        self.assertEqual(listing["courses"], [])

    def test_usernames_match_regardless_of_case(self):
        """Keycloak lowercases usernames; a hand-written list may not."""
        policy = CoursePolicy("allowlist", allowlist=parse_allowlist("Student1=ilias:86"))
        self.assertTrue(policy(USER, "ilias:86"))

    def test_the_allowlist_is_parsed_from_its_environment_form(self):
        parsed = parse_allowlist("student1=ilias:86, moodle:5; tutor = studip:abc;")
        self.assertEqual(parsed, {"student1": frozenset({"ilias:86", "moodle:5"}),
                                  "tutor": frozenset({"studip:abc"})})

    def test_a_malformed_or_empty_allowlist_is_refused(self):
        """A typo must stop the server, not silently hide every course."""
        with self.assertRaises(ValueError):
            parse_allowlist("student1")
        with self.assertRaises(ValueError):
            parse_allowlist("student1=")
        with self.assertRaises(ValueError):
            CoursePolicy("allowlist")
        with self.assertRaises(ValueError):
            CoursePolicy("allowlist", allowlist={})

    def test_the_policy_comes_from_the_environment(self):
        policy = policy_from_env({"BRIDGE_USER_COURSES": "allowlist",
                                  "BRIDGE_USER_COURSE_ALLOWLIST": "student1=ilias:86"})
        self.assertEqual(policy.mode, "allowlist")
        self.assertTrue(policy(USER, "ilias:86"))
        self.assertFalse(policy(USER, "moodle:5"))
        self.assertEqual(policy_from_env({}).mode, "none")
        with self.assertRaises(ValueError):
            policy_from_env({"BRIDGE_USER_COURSES": "allowlist"})

    def test_all_and_none_ignore_the_allowlist(self):
        self.assertTrue(CoursePolicy("all", allowlist={"x": {"ilias:86"}})(USER, "moodle:5"))
        self.assertFalse(CoursePolicy("none", allowlist={"student1": {"ilias:86"}})(USER, "ilias:86"))

if __name__ == "__main__":
    unittest.main()
